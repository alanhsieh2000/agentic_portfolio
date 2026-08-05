"""Log market value of equity (mve) per ticker per rebalance date.

This module will eventually also compute `bm` per plans/01_dataset.md's
Plan of Work section — that is a separate, later checklist item, out of
scope here. The `factors` table this module writes carries
`bm`/`mom12m`/`bm_z`/`mom12m_z` as NULL placeholders for exactly that
reason; `build.py` (future work) is expected to DROP and fully recreate
this table once `bm.py`/`momentum.py` exist.

CRITICAL: yfinance.download's close/adj_close (used by src/dataset/prices.py)
are always split-adjusted relative to today's real-world date, not the
fetch window's end date, and there is no way to get a raw split-unadjusted
price out of yfinance at all (see prices.py's Surprises & Discoveries entry
in plans/01_dataset.md). Ticker.get_shares_full, however, returns the RAW
point-in-time historical share count — it does NOT retroactively
split-adjust. Naively computing mve = log(shares_raw * price) therefore
silently understates mve by log(cumulative_split_ratio) for any ticker that
split between rebalance date d and today. This module corrects for that
using Ticker.splits (each ticker's complete, unbounded split history) — see
cumulative_split_ratio_after().
"""

from __future__ import annotations

import logging
import math
import time
from pathlib import Path

import duckdb
import pandas as pd
import yfinance as yf

from src.dataset.membership import DEFAULT_DB_PATH
from src.dataset.prices import DEFAULT_END, DEFAULT_START, to_yfinance_symbol

logger = logging.getLogger(__name__)


class SharesFetchFailedError(RuntimeError):
    """Raised when fetching shares/splits yielded zero usable series for
    every ticker requested — almost certainly a network/config problem,
    not genuine total data absence.
    """


def fetch_shares_and_splits(
    ticker: str, start: str = DEFAULT_START, end: str = DEFAULT_END
) -> tuple[pd.Series, pd.Series]:
    """The only function in this module that performs network I/O.

    Returns (shares_raw, splits) for one ticker: shares_raw is
    get_shares_full's raw historical series (tz-aware DatetimeIndex, may be
    empty); splits is Ticker.splits's complete, UNBOUNDED history (called
    with no start/end — a split after `end` is exactly what this module
    needs to correct for). Empty splits (never split) is the common case,
    not an error — returned as-is, not raised.
    """
    yf_symbol = to_yfinance_symbol(ticker)
    t = yf.Ticker(yf_symbol)
    shares = t.get_shares_full(start=start, end=end)
    if shares is None:
        shares = pd.Series(dtype="float64")
    splits = t.splits
    return shares, splits


def _strip_tz(idx: pd.DatetimeIndex) -> pd.DatetimeIndex:
    """Drop tz info without shifting wall-clock time. splits/shares indices
    are tz-aware (America/New_York); rebalance dates elsewhere in this
    codebase are naive. tz_localize(None) is a no-op on an already-naive
    index, so this is safe to call unconditionally.
    """
    return idx.tz_localize(None) if idx.tz is not None else idx


def cumulative_split_ratio_after(splits: pd.Series, as_of) -> float:
    """Product of every split ratio in `splits` whose ex-date is strictly
    after `as_of`. Empty splits (a ticker that never split — the common
    case) returns 1.0 cleanly, not an error. Compared at calendar-date
    granularity (normalize()), since intraday split timing is not
    meaningful at the monthly-rebalance-date granularity this project uses.
    """
    if splits is None or splits.empty:
        return 1.0
    as_of_ts = pd.Timestamp(as_of).tz_localize(None).normalize()
    ex_dates = _strip_tz(splits.index).normalize()
    mask = ex_dates > as_of_ts
    if not mask.any():
        return 1.0
    return float(splits.to_numpy()[mask].prod())


def most_recent_shares_on_or_before(shares: pd.Series | None, as_of) -> float | None:
    """Pick the most recent raw shares-outstanding value on or before `d`
    from get_shares_full's raw series. Returns None if `shares` is empty or
    every value in it postdates `as_of`. Defensively dedupes the index
    (get_shares_full can return two rows for the same date with different
    values) by keeping the last occurrence, then sorts before using
    Series.asof, which itself returns NaN (not an error) when `as_of`
    predates the earliest entry.
    """
    if shares is None or shares.empty:
        return None
    s = shares.copy()
    s.index = _strip_tz(s.index).normalize()
    s = s[~s.index.duplicated(keep="last")].sort_index()
    as_of_ts = pd.Timestamp(as_of).tz_localize(None).normalize()
    value = s.asof(as_of_ts)
    return None if pd.isna(value) else float(value)


def compute_mve(
    price: float | None, shares_raw: float | None, splits: pd.Series | None, as_of
) -> float | None:
    """log(price * shares_raw * cumulative_split_ratio_after(splits, as_of)).

    Returns None (not NaN, not an exception) if price or shares_raw is
    missing/NaN — the expected, common case for a ticker with no price row
    or no shares history that far back; the null-preserving philosophy this
    plan follows means this function must not raise for it. `splits`
    defaults to an empty Series (ratio 1.0) if None.
    """
    if price is None or pd.isna(price) or shares_raw is None or pd.isna(shares_raw):
        return None
    ratio = cumulative_split_ratio_after(
        splits if splits is not None else pd.Series(dtype="float64"), as_of
    )
    market_cap = price * shares_raw * ratio
    if market_cap <= 0:
        logger.warning("non-positive market_cap (%s) for as_of=%s; returning None", market_cap, as_of)
        return None
    return math.log(market_cap)


def load_membership(db_path: str = DEFAULT_DB_PATH) -> pd.DataFrame:
    """columns ['rebalance_date', 'ticker'] — every (date, ticker) pair
    that actually needs an mve value; this is already exactly the set of
    (ticker, rebalance_date) pairs that were real members, not the full
    ticker-universe x 52-date grid.
    """
    con = duckdb.connect(db_path)
    try:
        df = con.execute("SELECT rebalance_date, ticker FROM sp500_membership").fetchdf()
    finally:
        con.close()
    df["rebalance_date"] = pd.to_datetime(df["rebalance_date"])
    return df


def load_prices_for_join(db_path: str = DEFAULT_DB_PATH) -> pd.DataFrame:
    """columns ['date', 'ticker', 'adj_close'] — only what's needed for the
    nearest-price-on-or-before join, not the full `prices` table.
    """
    con = duckdb.connect(db_path)
    try:
        df = con.execute("SELECT date, ticker, adj_close FROM prices").fetchdf()
    finally:
        con.close()
    df["date"] = pd.to_datetime(df["date"])
    return df


def load_unresolved_tickers(db_path: str = DEFAULT_DB_PATH) -> set[str]:
    """Tickers prices.py already recorded as having zero usable price rows
    anywhere in the fetch window. Empty set if the table is absent.
    """
    con = duckdb.connect(db_path)
    try:
        rows = con.execute("SELECT ticker FROM unresolved_tickers").fetchall()
    except duckdb.CatalogException:
        rows = []
    finally:
        con.close()
    return {r[0] for r in rows}


def attach_nearest_price(membership: pd.DataFrame, prices: pd.DataFrame) -> pd.DataFrame:
    """Adds an 'adj_close' column via the nearest price on or before each
    rebalance date, per ticker. Yields NaN (not an error) for a ticker with
    no price row on or before that rebalance date.

    NOTE for future implementers of momentum.py/returns.py: if you need the
    identical nearest-price-on-or-before-per-ticker join, reuse this
    function rather than reimplementing it.
    """
    m = membership.sort_values("rebalance_date")
    p = prices[["date", "ticker", "adj_close"]].sort_values("date")
    return pd.merge_asof(m, p, left_on="rebalance_date", right_on="date", by="ticker", direction="backward")


def fetch_all_shares_and_splits(
    tickers: list[str], pause_seconds: float = 0.25
) -> tuple[dict[str, pd.Series], dict[str, pd.Series]]:
    """Fetch-once-per-ticker: one get_shares_full + one .splits call per
    ticker, reused across every rebalance date that ticker appears on.

    Per-ticker failures are caught, logged, and simply leave that ticker
    absent from the returned dicts — every rebalance date it needed then
    gets a null mve via compute_mve's missing-input handling, matching
    prices.py's per-symbol degrade-not-crash philosophy. Raises
    SharesFetchFailedError only if every ticker fails.

    Tickers already in `unresolved_tickers` (from prices.py) should be
    excluded from `tickers` by the caller before this is called: mve needs
    both a price and shares outstanding, and a ticker with no price data
    anywhere in the fetch window will always get a null mve regardless of
    whether this fetch succeeds for it, so skipping it here is a pure
    performance optimization with no effect on any output value. If this
    module is ever extended to compute a factor that does not depend on
    price, this shortcut must be revisited.
    """
    shares_by_ticker: dict[str, pd.Series] = {}
    splits_by_ticker: dict[str, pd.Series] = {}
    for i, ticker in enumerate(tickers, start=1):
        try:
            shares, splits = fetch_shares_and_splits(ticker)
            shares_by_ticker[ticker] = shares
            splits_by_ticker[ticker] = splits
        except Exception:
            logger.warning("fundamentals fetch failed for %s; mve will be null for it", ticker, exc_info=True)
        if i % 50 == 0:
            logger.info("fetched fundamentals for %d/%d tickers", i, len(tickers))
        time.sleep(pause_seconds)
    if not shares_by_ticker and tickers:
        raise SharesFetchFailedError(f"Fetched zero usable shares series for all {len(tickers)} tickers.")
    return shares_by_ticker, splits_by_ticker


def compute_mve_column(
    merged: pd.DataFrame,
    shares_by_ticker: dict[str, pd.Series],
    splits_by_ticker: dict[str, pd.Series],
) -> pd.Series:
    def _row_mve(row):
        shares_raw = most_recent_shares_on_or_before(shares_by_ticker.get(row.ticker), row.rebalance_date)
        splits = splits_by_ticker.get(row.ticker)
        return compute_mve(row.adj_close, shares_raw, splits, row.rebalance_date)

    return merged.apply(_row_mve, axis=1)


def add_cross_sectional_z(df: pd.DataFrame, value_col: str, z_col: str) -> pd.DataFrame:
    """Standardize `value_col` to mean 0, variance 1 within each
    rebalance_date group, computed only over non-null values that date.

    A group's std can be exactly 0 (e.g. only one non-null value, or
    multiple identical values); dividing by 0 would silently produce
    inf/nan, so those dates are logged and their z-values explicitly
    masked to NaN instead.
    """
    grouped = df.groupby("rebalance_date")[value_col]
    mean = grouped.transform("mean")
    std = grouped.transform("std")
    counts = grouped.transform("count")
    z = (df[value_col] - mean) / std
    zero_std = (std == 0) & (counts > 0)
    if zero_std.any():
        bad_dates = sorted(df.loc[zero_std, "rebalance_date"].unique())
        logger.warning(
            "%s: cross-sectional std is exactly 0 on rebalance_date(s) %s; setting %s to NaN there",
            value_col,
            bad_dates,
            z_col,
        )
        z = z.mask(zero_std)
    df[z_col] = z
    return df


def write_factors_table(df: pd.DataFrame, db_path: str = DEFAULT_DB_PATH) -> None:
    """Write `df` to the `factors` table in the DuckDB file at `db_path`,
    creating the parent directory if needed. Drops any pre-existing table
    first, so re-running this is always safe, and so a future build.py can
    freely DROP and fully recreate this table once bm/mom12m exist without
    needing to know this module ran standalone first.
    """
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(db_path)
    try:
        con.register("factors_df", df)
        con.execute("DROP TABLE IF EXISTS factors")
        con.execute(
            "CREATE TABLE factors AS "
            "SELECT rebalance_date::DATE AS rebalance_date, "
            "ticker::VARCHAR AS ticker, "
            "mve::DOUBLE AS mve, "
            "bm::DOUBLE AS bm, "
            "mom12m::DOUBLE AS mom12m, "
            "mve_z::DOUBLE AS mve_z, "
            "bm_z::DOUBLE AS bm_z, "
            "mom12m_z::DOUBLE AS mom12m_z "
            "FROM factors_df"
        )
        con.unregister("factors_df")
    finally:
        con.close()


def build_mve_factors(db_path: str = DEFAULT_DB_PATH) -> pd.DataFrame:
    """Fetch, join, compute mve, standardize, write to DuckDB, and return
    the resulting factors DataFrame (bm/mom12m/bm_z/mom12m_z as NULL).
    """
    membership = load_membership(db_path)
    prices = load_prices_for_join(db_path)
    unresolved = load_unresolved_tickers(db_path)

    fetchable = sorted(set(membership["ticker"]) - unresolved)
    shares_by_ticker, splits_by_ticker = fetch_all_shares_and_splits(fetchable)

    merged = attach_nearest_price(membership, prices)
    merged["mve"] = compute_mve_column(merged, shares_by_ticker, splits_by_ticker)
    merged["bm"] = None
    merged["mom12m"] = None
    merged["bm_z"] = None
    merged["mom12m_z"] = None
    merged = add_cross_sectional_z(merged, "mve", "mve_z")

    factors = merged[["rebalance_date", "ticker", "mve", "bm", "mom12m", "mve_z", "bm_z", "mom12m_z"]]
    write_factors_table(factors, db_path)
    logger.info(
        "wrote %d rows to %s::factors (mve populated for %d rows; bm/mom12m pending)",
        len(factors),
        db_path,
        factors["mve"].notna().sum(),
    )
    return factors


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    build_mve_factors()
