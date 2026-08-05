"""Log market value of equity (mve) and book-to-market ratio (bm) per
ticker per rebalance date.

`mom12m`/`mom12m_z` remain NULL placeholders in the `factors` table this
module writes — a separate, later checklist item. `build.py` (future work)
is expected to DROP and fully recreate this table once `momentum.py` exists.

CRITICAL: yfinance.download's close/adj_close (used by src/dataset/prices.py)
are always split-adjusted relative to today's real-world date, not the
fetch window's end date, and there is no way to get a raw split-unadjusted
price out of yfinance at all (see prices.py's Surprises & Discoveries entry
in plans/01_dataset.md). Ticker.get_shares_full, however, returns the RAW
point-in-time historical share count — it does NOT retroactively
split-adjust. Naively computing market value as shares_raw * price therefore
silently understates it by cumulative_split_ratio for any ticker that split
between rebalance date d and today. This module corrects for that using
Ticker.splits (each ticker's complete, unbounded split history) — see
cumulative_split_ratio_after(). Both mve and bm share this exact,
split-corrected market value via compute_market_cap(), computed once per
row (see compute_market_cap_column) rather than twice independently.

`bm` additionally depends on each ticker's balance sheet
(Ticker.quarterly_balance_sheet / Ticker.balance_sheet), which — verified
live — only ever exposes a short rolling window trailing from today's
real-world date, not this project's fetch window. Concretely,
quarterly_balance_sheet's oldest column is always far more recent than any
of this project's 52 rebalance dates could ever satisfy the required
3-month reporting lag against, so it can never actually fire for this
project's date range; only balance_sheet (annual) ever does, and only for
tickers/dates late enough in the window. See select_book_equity() and
most_recent_book_equity_before_lag().

Because that yfinance-based path leaves `bm` null for essentially all of
2020-2021, `build_factors` tries src/dataset/sec_edgar.py's SEC EDGAR-based
book equity FIRST for every ticker (deeper history, and a precise real
`filed` date rather than a 3-month approximation), falling back to the
yfinance-based `select_book_equity` path above only for tickers where SEC
coverage is missing or doesn't reach this project's rebalance window (see
sec_edgar.has_sufficient_coverage — this correctly catches, e.g., XOM's
current ticker->CIK mapping pointing to a newly-formed successor entity
with no pre-2025 filings, verified live and documented in
plans/01_dataset.md).
"""

from __future__ import annotations

import logging
import math
import time
from pathlib import Path

import duckdb
import pandas as pd
import yfinance as yf

from src.dataset import sec_edgar
from src.dataset.membership import DEFAULT_DB_PATH
from src.dataset.prices import DEFAULT_END, DEFAULT_START, to_yfinance_symbol

logger = logging.getLogger(__name__)


BOOK_EQUITY_ALIASES = [
    "Common Stock Equity",  # most specific to common shareholders
    "Stockholders Equity",  # equals Common Stock Equity absent minority interest
    "Total Equity Gross Minority Interest",  # last resort: overstates by minority interest
]
# Deliberately excludes "Other Equity Adjustments": verified live to be a
# small, separate AOCI-style line item (e.g. AAPL 2024-09-30: -$7,172M vs
# Common Stock Equity's $56,950M same date), not a valid book-equity
# substitute.


class SharesFetchFailedError(RuntimeError):
    """Raised when fetching fundamentals yielded zero usable data on every
    front (shares, splits, AND both balance sheets) for every ticker
    requested — almost certainly a network/config problem, not genuine
    total data absence.
    """


class BookEquityLineItemNotFoundError(RuntimeError):
    """Raised when a ticker's balance sheet has real content (at least one
    reporting-period column and at least one line-item row) but NONE of
    BOOK_EQUITY_ALIASES appear in its index — a structural break in
    yfinance's line-item labels, this module's own assumption, not
    ordinary per-ticker data absence. Matches MembershipTableNotFoundError's
    fail-loud-on-broken-structural-assumption pattern: deliberately left
    UNCAUGHT everywhere in this module, including the orchestration layer.
    A mismatched alias list is a systemic labeling change likely to affect
    most/all tickers, not a one-off worth silently skipping.
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


def most_recent_value_on_or_before(shares: pd.Series | None, as_of) -> float | None:
    """Pick the most recent value on or before `as_of` from a date-indexed
    Series. Returns None if `shares` is empty or every value in it postdates
    `as_of`. Defensively dedupes the index (get_shares_full can return two
    rows for the same date with different values) by keeping the last
    occurrence, then sorts before using Series.asof, which itself returns
    NaN (not an error) when `as_of` predates the earliest entry.

    Despite the parameter name (this function originated for shares
    outstanding), the logic is fully generic for any date-indexed float
    Series — reused as-is by src/dataset/momentum.py for price lookups.
    """
    if shares is None or shares.empty:
        return None
    s = shares.copy()
    s.index = _strip_tz(s.index).normalize()
    s = s[~s.index.duplicated(keep="last")].sort_index()
    as_of_ts = pd.Timestamp(as_of).tz_localize(None).normalize()
    value = s.asof(as_of_ts)
    return None if pd.isna(value) else float(value)


def compute_market_cap(
    price: float | None, shares_raw: float | None, splits: pd.Series | None, as_of
) -> float | None:
    """price * shares_raw * cumulative_split_ratio_after(splits, as_of) —
    the split-basis-corrected, UNLOGGED market value of equity.

    Extracted as its own function so mve's operand and bm's denominator are
    guaranteed identical, computed the same way — never two independent
    (and possibly subtly divergent) implementations of "market value."

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
    return market_cap


def compute_mve(
    price: float | None, shares_raw: float | None, splits: pd.Series | None, as_of
) -> float | None:
    """log(compute_market_cap(price, shares_raw, splits, as_of))."""
    market_cap = compute_market_cap(price, shares_raw, splits, as_of)
    return None if market_cap is None else math.log(market_cap)


def find_book_equity_row(balance_sheet: pd.DataFrame, ticker: str) -> pd.Series | None:
    """Return the row (one value per reporting-period column) for the first
    alias in BOOK_EQUITY_ALIASES present in `balance_sheet.index`, in
    preference order.

    Returns None (not a raise) if `balance_sheet` is entirely empty — no
    reporting-period columns and no line-item rows at all, verified live to
    be yfinance's actual shape for a ticker with zero balance-sheet data.
    This is ordinary per-ticker data absence, not a structural mismatch,
    since there is nothing in an empty index to mismatch against.

    Raises BookEquityLineItemNotFoundError if `balance_sheet` has real
    content but its index contains none of BOOK_EQUITY_ALIASES — yfinance's
    line-item labels have apparently changed. `ticker` exists purely to
    name it in this error message, since the DataFrame itself carries no
    ticker identity.
    """
    if balance_sheet is None or balance_sheet.empty:
        return None
    for alias in BOOK_EQUITY_ALIASES:
        if alias in balance_sheet.index:
            return balance_sheet.loc[alias]
    raise BookEquityLineItemNotFoundError(
        f"None of {BOOK_EQUITY_ALIASES} found in balance sheet index for "
        f"ticker {ticker!r}; actual index labels found: {list(balance_sheet.index)}"
    )


def most_recent_book_equity_before_lag(
    book_equity_row: pd.Series | None, as_of, lag_months: int = 3
) -> float | None:
    """Among `book_equity_row`'s columns (period-end Timestamps -> values)
    whose period-end date is <= `as_of` minus `lag_months` months (the
    "Book value timing" safeguard: a report isn't publicly knowable until
    roughly this long after its period ends), return the value at the
    latest such period-end date that is itself non-null.

    A null most-recent-eligible column (verified live: a ticker's oldest
    balance_sheet column can be entirely NaN, e.g. AAPL's 2021-09-30) is
    skipped in favor of an older eligible, populated column, rather than
    returning None outright — a null column here is yfinance's own
    reporting gap, not evidence book equity was genuinely unknown that
    date; every column considered still independently satisfies the lag
    rule on its own merits, so this widens which eligible column gets
    picked without imputing anything.

    balance_sheet columns are tz-naive, unlike get_shares_full/splits's
    tz-aware index — no tz handling needed here.
    """
    if book_equity_row is None or book_equity_row.empty:
        return None
    as_of_ts = pd.Timestamp(as_of)
    cutoff = as_of_ts - pd.DateOffset(months=lag_months)
    periods = pd.to_datetime(book_equity_row.index)
    eligible_mask = periods <= cutoff
    if not eligible_mask.any():
        return None
    eligible = pd.Series(book_equity_row.to_numpy(), index=periods)[eligible_mask]
    eligible = eligible.sort_index(ascending=False)
    for value in eligible:
        if pd.notna(value):
            return float(value)
    return None


def select_book_equity(
    quarterly_bs: pd.DataFrame, annual_bs: pd.DataFrame, as_of, ticker: str, lag_months: int = 3
) -> float | None:
    """Try quarterly first, fall back to annual.

    In this project's actual 2020-01-01..2024-04-01 rebalance window,
    quarterly_balance_sheet's oldest column is always well after any
    rebalance date's lag cutoff (verified live) — this quarterly branch
    will in practice never fire for any row this project computes; it is
    kept anyway because it costs nothing extra, is the theoretically more
    correct source when available, and protects this function if
    yfinance's rolling window ever widens to cover this project's dates.
    """
    quarterly_row = find_book_equity_row(quarterly_bs, ticker)
    value = most_recent_book_equity_before_lag(quarterly_row, as_of, lag_months)
    if value is not None:
        return value
    annual_row = find_book_equity_row(annual_bs, ticker)
    return most_recent_book_equity_before_lag(annual_row, as_of, lag_months)


def compute_bm(
    book_equity: float | None,
    price: float | None,
    shares_raw: float | None,
    splits: pd.Series | None,
    as_of,
) -> float | None:
    """book_equity / compute_market_cap(price, shares_raw, splits, as_of) —
    the same unlogged market value compute_mve uses internally.

    Returns None if book_equity is None/NaN or compute_market_cap returns
    None (missing price/shares, or non-positive market cap) — ordinary
    missing-data handling, never raises.
    """
    if book_equity is None or pd.isna(book_equity):
        return None
    market_cap = compute_market_cap(price, shares_raw, splits, as_of)
    return None if market_cap is None else book_equity / market_cap


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

    Returns a DataFrame indexed by `membership`'s ORIGINAL index labels
    (pd.merge_asof itself resets to a fresh RangeIndex, verified live — this
    reattaches `m`'s labels, which correspond 1:1 with the merged output's
    row order since merge_asof keeps every left row exactly once, in order).
    This matters when this function is called more than once against
    differently-shifted copies of the same `membership` (e.g.
    src/dataset/momentum.py calling it once for "1 month before" and once
    for "12 months before" each rebalance date): the two results can then be
    combined via ordinary pandas index-aligned arithmetic, without relying
    on both calls happening to produce matching row order.

    NOTE for future implementers of returns.py: reuse this function rather
    than reimplementing the same nearest-price-on-or-before-per-ticker join.
    """
    m = membership.sort_values("rebalance_date")
    p = prices[["date", "ticker", "adj_close"]].sort_values("date")
    merged = pd.merge_asof(m, p, left_on="rebalance_date", right_on="date", by="ticker", direction="backward")
    merged.index = m.index
    return merged


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

    Kept standalone (mve-only inputs) for symmetry/testability; the
    production path (build_factors) instead calls
    fetch_all_ticker_fundamentals, which fetches this AND balance sheets
    off one shared yf.Ticker(...) per ticker.
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


def fetch_balance_sheets(ticker: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Standalone bm-input fetch, symmetrical with fetch_shares_and_splits.

    Returns (quarterly_balance_sheet, balance_sheet) for one ticker — both
    yfinance properties take no start/end args (verified live: always a
    short rolling window trailing from today's real-world date). Production
    code (build_factors) does not call this directly — see
    fetch_all_ticker_fundamentals, which shares one yf.Ticker(...) per
    ticker across both this and fetch_shares_and_splits's concerns to avoid
    a second HTTP-level ticker resolution.
    """
    yf_symbol = to_yfinance_symbol(ticker)
    t = yf.Ticker(yf_symbol)
    quarterly_bs = t.quarterly_balance_sheet
    annual_bs = t.balance_sheet
    return (
        quarterly_bs if quarterly_bs is not None else pd.DataFrame(),
        annual_bs if annual_bs is not None else pd.DataFrame(),
    )


def fetch_all_ticker_fundamentals(
    tickers: list[str], pause_seconds: float = 0.25
) -> tuple[dict[str, pd.Series], dict[str, pd.Series], dict[str, pd.DataFrame], dict[str, pd.DataFrame]]:
    """Fetch-once-per-ticker for both mve's inputs (shares, splits) and
    bm's inputs (quarterly/annual balance sheet), sharing exactly one
    yf.Ticker(...) construction per ticker.

    Two independent try/except blocks per ticker, not one: a balance-sheet
    fetch failure must not null out mve for a ticker whose shares/splits
    fetch succeeded, and vice versa. A ticker absent from
    shares_by_ticker gets a null mve; absent from
    quarterly_bs_by_ticker/annual_bs_by_ticker gets a null bm — both
    degrade, neither crashes the build. Raises SharesFetchFailedError only
    if every ticker yielded nothing on every front.

    Tickers already in `unresolved_tickers` (from prices.py) should be
    excluded from `tickers` by the caller before this is called: both mve
    and bm need a price, and a ticker with no price data anywhere in the
    fetch window will always get null mve/bm regardless of whether this
    fetch succeeds for it, so skipping it here is a pure performance
    optimization with no effect on any output value. If this module is
    ever extended to compute a factor that does not depend on price, this
    shortcut must be revisited.
    """
    shares_by_ticker: dict[str, pd.Series] = {}
    splits_by_ticker: dict[str, pd.Series] = {}
    quarterly_bs_by_ticker: dict[str, pd.DataFrame] = {}
    annual_bs_by_ticker: dict[str, pd.DataFrame] = {}
    for i, ticker in enumerate(tickers, start=1):
        try:
            t = yf.Ticker(to_yfinance_symbol(ticker))
        except Exception:
            logger.warning("Ticker() construction failed for %s; mve and bm will be null for it", ticker, exc_info=True)
            continue

        try:
            shares = t.get_shares_full(start=DEFAULT_START, end=DEFAULT_END)
            shares_by_ticker[ticker] = shares if shares is not None else pd.Series(dtype="float64")
            splits_by_ticker[ticker] = t.splits
        except Exception:
            logger.warning("shares/splits fetch failed for %s; mve will be null for it", ticker, exc_info=True)

        try:
            qbs = t.quarterly_balance_sheet
            abs_ = t.balance_sheet
            quarterly_bs_by_ticker[ticker] = qbs if qbs is not None else pd.DataFrame()
            annual_bs_by_ticker[ticker] = abs_ if abs_ is not None else pd.DataFrame()
        except Exception:
            logger.warning("balance sheet fetch failed for %s; bm will be null for it", ticker, exc_info=True)

        if i % 50 == 0:
            logger.info("fetched fundamentals for %d/%d tickers", i, len(tickers))
        time.sleep(pause_seconds)

    if not shares_by_ticker and not quarterly_bs_by_ticker and not annual_bs_by_ticker and tickers:
        raise SharesFetchFailedError(f"Fetched zero usable data on every front for all {len(tickers)} tickers.")
    return shares_by_ticker, splits_by_ticker, quarterly_bs_by_ticker, annual_bs_by_ticker


def compute_market_cap_column(
    merged: pd.DataFrame,
    shares_by_ticker: dict[str, pd.Series],
    splits_by_ticker: dict[str, pd.Series],
) -> pd.Series:
    """Row-wise unlogged market value of equity. Both mve (via log of this
    column) and bm (via this column as denominator) read the result —
    shares/splits lookup happens exactly once per row, here.
    """
    def _row_market_cap(row):
        shares_raw = most_recent_value_on_or_before(shares_by_ticker.get(row.ticker), row.rebalance_date)
        return compute_market_cap(row.adj_close, shares_raw, splits_by_ticker.get(row.ticker), row.rebalance_date)

    return merged.apply(_row_market_cap, axis=1)


def compute_mve_column(market_cap: pd.Series) -> pd.Series:
    """log(market_cap) per row, None-preserving."""
    return market_cap.apply(lambda mc: None if mc is None or pd.isna(mc) else math.log(mc))


def compute_bm_column(
    merged: pd.DataFrame,
    market_cap: pd.Series,
    quarterly_bs_by_ticker: dict[str, pd.DataFrame],
    annual_bs_by_ticker: dict[str, pd.DataFrame],
    sec_book_equity_by_ticker: dict[str, pd.DataFrame] | None = None,
    lag_months: int = 3,
) -> pd.Series:
    """Row-wise bm = book_equity / market_cap[row]. book_equity is looked
    up once per row; market_cap is reused from compute_market_cap_column's
    already-computed Series, aligned by position with `merged`, rather than
    recomputed.

    If `sec_book_equity_by_ticker` has an entry for a row's ticker, that
    ticker uses sec_edgar.select_book_equity_asof for EVERY row (a
    whole-ticker decision, not mixed row-by-row within one ticker's
    timeline, so SEC's precise filed-date timing is never inconsistently
    blended with yfinance's 3-month approximation for the same ticker).
    Otherwise falls through to the existing select_book_equity path.
    """
    sec_book_equity_by_ticker = sec_book_equity_by_ticker or {}

    def _row_book_equity(row):
        sec_facts = sec_book_equity_by_ticker.get(row.ticker)
        if sec_facts is not None:
            return sec_edgar.select_book_equity_asof(sec_facts, row.rebalance_date)
        return select_book_equity(
            quarterly_bs_by_ticker.get(row.ticker, pd.DataFrame()),
            annual_bs_by_ticker.get(row.ticker, pd.DataFrame()),
            row.rebalance_date,
            row.ticker,
            lag_months,
        )

    book_equity = merged.apply(_row_book_equity, axis=1)
    return pd.Series(
        [
            None if be is None or pd.isna(be) or mc is None or pd.isna(mc) else be / mc
            for be, mc in zip(book_equity, market_cap)
        ],
        index=merged.index,
    )


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


def write_sec_fallback_table(reasons_by_ticker: dict[str, str], db_path: str = DEFAULT_DB_PATH) -> None:
    """Write `sec_fallback_tickers(ticker VARCHAR, reason VARCHAR)` — every
    ticker for which SEC EDGAR coverage was missing or insufficient and
    `bm` fell back to the yfinance-based path. Mirrors prices.py's
    `unresolved_tickers` schema and idempotent DROP-and-recreate pattern,
    for the same reason: make the gap visible and auditable, not hidden.
    """
    df = pd.DataFrame(
        {"ticker": list(reasons_by_ticker.keys()), "reason": list(reasons_by_ticker.values())}
    )
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(db_path)
    try:
        con.register("sec_fallback_df", df)
        con.execute("DROP TABLE IF EXISTS sec_fallback_tickers")
        con.execute(
            "CREATE TABLE sec_fallback_tickers AS "
            "SELECT ticker::VARCHAR AS ticker, reason::VARCHAR AS reason FROM sec_fallback_df"
        )
        con.unregister("sec_fallback_df")
    finally:
        con.close()


def build_factors(db_path: str = DEFAULT_DB_PATH) -> pd.DataFrame:
    """Fetch, join, compute mve and bm, standardize both, write to DuckDB,
    and return the resulting factors DataFrame (mom12m/mom12m_z still
    NULL — a separate, later checklist item).
    """
    membership = load_membership(db_path)
    prices = load_prices_for_join(db_path)
    unresolved = load_unresolved_tickers(db_path)

    fetchable = sorted(set(membership["ticker"]) - unresolved)
    latest_rebalance_date = membership["rebalance_date"].max()

    cik_map = sec_edgar.fetch_cik_map()
    sec_book_equity_by_ticker, sec_fallback_reasons = sec_edgar.fetch_all_sec_book_equity(
        fetchable, cik_map, latest_rebalance_date
    )
    write_sec_fallback_table(sec_fallback_reasons, db_path)

    shares_by_ticker, splits_by_ticker, quarterly_bs_by_ticker, annual_bs_by_ticker = (
        fetch_all_ticker_fundamentals(fetchable)
    )

    merged = attach_nearest_price(membership, prices)
    market_cap = compute_market_cap_column(merged, shares_by_ticker, splits_by_ticker)
    merged["mve"] = compute_mve_column(market_cap)
    merged["bm"] = compute_bm_column(
        merged, market_cap, quarterly_bs_by_ticker, annual_bs_by_ticker, sec_book_equity_by_ticker
    )
    merged["mom12m"] = None
    merged = add_cross_sectional_z(merged, "mve", "mve_z")
    merged = add_cross_sectional_z(merged, "bm", "bm_z")
    merged["mom12m_z"] = None

    factors = merged[["rebalance_date", "ticker", "mve", "bm", "mom12m", "mve_z", "bm_z", "mom12m_z"]]
    write_factors_table(factors, db_path)
    logger.info(
        "wrote %d rows to %s::factors (mve populated for %d rows; bm populated for %d rows via SEC for %d "
        "tickers, %d fell back to yfinance; mom12m pending)",
        len(factors),
        db_path,
        factors["mve"].notna().sum(),
        factors["bm"].notna().sum(),
        len(sec_book_equity_by_ticker),
        len(sec_fallback_reasons),
    )
    return factors


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    build_factors()
