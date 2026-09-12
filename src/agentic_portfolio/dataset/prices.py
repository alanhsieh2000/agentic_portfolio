"""Daily price history cache for every ticker that was ever an S&P 500
member in this project's 2020-2024 backtest window.

Fetches from 2015-01-01 (60 months before the earliest 2020-01-01 rebalance
date, covering plan 5's 60-month returns lookback) through 2024-04-30, using
yfinance, and caches the result in the same DuckDB file membership.py writes
to, so momentum/fundamentals/returns (future work) never need to talk to
yfinance for prices themselves.
"""

from __future__ import annotations

import logging
import time
from datetime import date
from pathlib import Path

import duckdb
import pandas as pd
import yfinance as yf

from agentic_portfolio.config.settings import settings

logger = logging.getLogger(__name__)

UNRESOLVED_REASON = (
    "yfinance returned no non-null close/adj_close values for this ticker "
    "(or its yfinance-mapped symbol) across the full {start}..{end} fetch "
    "window; yfinance does not distinguish 'ticker never resolved' from "
    "'ticker resolved but had no data in range' at the download() level, so "
    "this reason is intentionally generic."
)


class TickerUniverseEmptyError(RuntimeError):
    """Raised when data/portfolio.duckdb has no sp500_membership table, or
    the table exists but has zero rows. prices.py depends on membership.py's
    output already existing; this fails loudly instead of silently fetching
    prices for an empty universe.
    """


class PriceFieldMissingError(RuntimeError):
    """Raised when a yfinance batch's returned columns don't include both
    'Close' and 'Adj Close' — a sign yfinance's auto_adjust=False column
    contract has changed and this module's reshape logic is now wrong.
    """


class PriceFetchFailedError(RuntimeError):
    """Raised when fetching yielded zero usable rows for a non-empty ticker
    universe — almost certainly a network/config problem, not genuine total
    delisting of every ticker at once.
    """


KNOWN_EXCHANGE_SUFFIXES = frozenset({
    "T", "L", "HK", "TO", "PA", "DE", "MI", "AS", "SW", "SI", "AX",
    "KS", "SS", "SZ", "BO", "NS", "MC", "BR", "ST", "OL", "CO", "HE", "SA",
})
"""Yahoo Finance's own suffix codes for non-US exchanges, which it writes
after a literal dot ('7203.T' for Tokyo, 'BARC.L' for London).

`to_yfinance_symbol` uses this to tell such a suffix apart from Wikipedia's
US share-class notation ('BRK.B'), which needs a dash instead. Only
single-letter codes could ever be ambiguous, since every other code is two
or more letters and cannot collide with a share class. So the invariant that
makes this safe is: the only single letters here are 'T' and 'L', and 'A',
'B', 'C' and 'K' are deliberately absent, because those are the letters US
listings actually use as share classes ('BRK.B' and 'BF.B' are the only
dotted tickers this project's membership scrape has ever produced; 'HEI.A'
and 'MOG.A' are other real examples). Extend this set one exchange at a
time, and never add a single letter beyond these two.

'V' (TSX Venture) and 'F' (Frankfurt) are consciously excluded for that
reason: they carry the same collision risk with no offsetting need, and
their markets stay reachable by other suffixes ('SAP.DE' resolves, 'SAP.F'
does not).
"""


def to_yfinance_symbol(ticker: str) -> str:
    """Translate a ticker to the symbol yfinance expects.

    Two conventions both use a dot and mean different things. Wikipedia's
    share-class tickers ('BRK.B', 'BF.B') correspond to a dash on Yahoo
    Finance ('BRK-B', 'BF-B'). Yahoo Finance's own exchange suffixes
    ('7203.T', 'BARC.L') keep the dot exactly as written. So a ticker whose
    text after the LAST dot is in `KNOWN_EXCHANGE_SUFFIXES` (compared
    case-insensitively, though the string itself is returned untouched)
    passes through unchanged, and any other dotted ticker is dash-converted
    as before - which is also the right default for a typo, since the
    mangled symbol then fails loudly through `detect_unresolved_tickers`
    instead of being fetched as something unintended. A ticker with no dot
    passes through unchanged.

    This is a fetch-boundary concern only — the `prices` table is keyed by
    the original ticker string (see _build_symbol_map / reshape_prices_long),
    so later modules that join against sp500_membership never need to know
    this translation happened.

    Note this decides only which symbol to FETCH, not what currency the
    result is in. A non-US listing is priced in its local currency, which
    `src/agentic_portfolio/dataset/ticker_currency.py` records and normalizes at ingestion so
    one portfolio can never mix units.
    """
    stripped = ticker.strip()
    if "." not in stripped:
        return stripped
    if stripped.rsplit(".", 1)[-1].upper() in KNOWN_EXCHANGE_SUFFIXES:
        return stripped
    return stripped.replace(".", "-")


def _build_symbol_map(tickers: list[str]) -> dict[str, str]:
    """Return {original_ticker: yfinance_symbol}. Raises ValueError if two
    distinct original tickers collide on the same yfinance symbol (would
    make the fetch result ambiguous to translate back).
    """
    mapping = {t: to_yfinance_symbol(t) for t in tickers}
    seen: dict[str, str] = {}
    for original, symbol in mapping.items():
        if symbol in seen and seen[symbol] != original:
            raise ValueError(
                f"Tickers {seen[symbol]!r} and {original!r} both map to yfinance "
                f"symbol {symbol!r} — cannot unambiguously translate results back."
            )
        seen[symbol] = original
    return mapping


def reshape_prices_long(raw: pd.DataFrame, symbol_to_ticker: dict[str, str]) -> pd.DataFrame:
    """Reshape yfinance's multi-ticker download() output into long format.

    `raw` has 2-level MultiIndex columns (field, symbol), field in {"Close",
    "Adj Close", ...} — yfinance's shape when called with auto_adjust=False
    and group_by="column" (the defaults this module relies on). Rows where
    both close and adj_close are null are dropped (a ticker with zero
    non-null rows across the whole range is "unresolved", handled by
    detect_unresolved_tickers, not represented here at all). Symbol columns
    are translated back to the original ticker string via `symbol_to_ticker`
    so the result can be joined against sp500_membership directly.

    Returns columns ['date', 'ticker', 'close', 'adj_close'], no I/O.
    """
    if raw.empty:
        return pd.DataFrame(columns=["date", "ticker", "close", "adj_close"])

    top_level = set(raw.columns.get_level_values(0))
    missing = {"Close", "Adj Close"} - top_level
    if missing:
        raise PriceFieldMissingError(
            f"Expected 'Close' and 'Adj Close' columns, got top-level fields "
            f"{sorted(top_level)}. yfinance's auto_adjust=False column "
            f"contract may have changed."
        )

    close_s = raw["Close"].stack()
    adj_s = raw["Adj Close"].stack()
    long = pd.DataFrame({"close": close_s, "adj_close": adj_s}).reset_index()
    long.columns = ["date", "symbol", "close", "adj_close"]
    long = long.dropna(subset=["close", "adj_close"], how="all")

    long["ticker"] = long["symbol"].map(symbol_to_ticker)
    unmapped = long["ticker"].isna()
    if unmapped.any():
        logger.warning(
            "dropping %d rows with unmapped symbols: %s",
            int(unmapped.sum()),
            sorted(long.loc[unmapped, "symbol"].unique()),
        )
        long = long[~unmapped]

    long["date"] = pd.to_datetime(long["date"]).dt.normalize()
    return (
        long[["date", "ticker", "close", "adj_close"]]
        .sort_values(["ticker", "date"])
        .reset_index(drop=True)
    )


def detect_unresolved_tickers(
    all_tickers: list[str],
    long_prices: pd.DataFrame,
    start: str = settings.fetch_start,
    end: str = settings.fetch_end,
) -> pd.DataFrame:
    """Return columns ['ticker', 'reason'] for every ticker in `all_tickers`
    absent from `long_prices['ticker']` — i.e. yfinance never produced a
    single non-null close/adj_close row for it. Pure, no I/O; always
    returns a correctly-typed DataFrame, even when empty. `start`/`end`
    default to the module-wide fetch window but should be passed explicitly
    by a caller fetching a different window (e.g. for a single arbitrary
    ticker), so the reason string names the window that was actually used.
    """
    present = set(long_prices["ticker"]) if not long_prices.empty else set()
    missing = sorted(set(all_tickers) - present)
    reason = UNRESOLVED_REASON.format(start=start, end=end)
    return pd.DataFrame({"ticker": missing, "reason": [reason] * len(missing)})


def _fetch_batch(symbols: list[str], start: str, end: str) -> pd.DataFrame:
    """Download one batch of symbols. The only function in this module
    that performs network I/O.
    """
    return yf.download(
        symbols,
        start=start,
        end=end,
        auto_adjust=False,
        group_by="column",
        threads=True,
        progress=False,
    )


def fetch_price_history(
    symbols: list[str],
    start: str = settings.fetch_start,
    end: str = settings.fetch_end,
    batch_size: int = settings.price_batch_size,
    pause_seconds: float = settings.yfinance_price_pause_seconds,
) -> pd.DataFrame:
    """Fetch `symbols` in chunks of `batch_size`, pausing briefly between
    batches as cheap insurance against rate-limiting, and concatenate the
    results column-wise (each batch contributes disjoint symbol columns
    under the same (field, symbol) MultiIndex shape).
    """
    chunks = [symbols[i : i + batch_size] for i in range(0, len(symbols), batch_size)]
    frames = []
    for i, chunk in enumerate(chunks, start=1):
        logger.info("fetching batch %d/%d (%d symbols)", i, len(chunks), len(chunk))
        raw = _fetch_batch(chunk, start, end)
        if raw is not None and not raw.empty:
            frames.append(raw)
        if i < len(chunks):
            time.sleep(pause_seconds)
    return pd.concat(frames, axis=1) if frames else pd.DataFrame()


def load_latest_close(
    tickers: list[str], as_of: date, db_path: str = settings.db_path
) -> pd.Series:
    """Most recent raw `close` on or before `as_of` for each of `tickers`,
    indexed by ticker, NaN where there is no such row.

    THE market-price lookup for this project: the one answer to "what is a
    share of this worth?", used both to turn a budget into share counts
    (`src/agentic_portfolio/optimizer/portfolio.py`'s `allocate_shares`) and to turn a share
    count into a market value (`src/agentic_portfolio/optimizer/holdings.py`'s
    `weights_from_positions`), and to divide a dividend by
    (`src/agentic_portfolio/dataset/dividends.py`). Those three must agree, so they read this.

    Why `close` and not `adj_close`, which this project reached for first.
    `adj_close` is BACK-ADJUSTED: every historical price is scaled down so
    that reinvested dividends make the series a total-return index, which is
    exactly right for computing a return and exactly wrong for pricing a
    trade, because nobody can transact at it. Dividing a budget by it buys
    more shares than the money can pay for - measured on the shipped
    `data/portfolio.duckdb`, a 100,000 allocation across five high-dividend
    names produced share counts that really cost 118,088.76, an 18%
    overspend, while the report claimed 17.69 of leftover cash.

    The original choice of `adj_close` was made to obtain SPLIT adjustment,
    and that reasoning was mistaken: `plans/01_dataset.md` records that
    yfinance's `Close` is always split-adjusted regardless of
    `auto_adjust`, so in this table the two columns differ by dividend
    adjustment alone. AAPL closed near 300 on 2020-01-02, before its 4:1
    split of 2020-08-31, and this table holds `close` 75.0875 - already
    divided by four - against `adj_close` 72.3339. So `close` carries every
    split correction an allocation needs and adds no dividend distortion.

    Reuses `attach_nearest_price` (src/agentic_portfolio/dataset/fundamentals.py), the same
    nearest-on-or-before-per-ticker join `src/agentic_portfolio/dataset/returns.py` and
    `src/agentic_portfolio/dataset/momentum.py` use. That import is function-local rather than
    module-level because `fundamentals.py` itself imports
    `to_yfinance_symbol` from this module, so a module-level import here
    would be circular - the same idiom `src/agentic_portfolio/dataset/holdings_cache.py` uses
    for the same reason. `attach_nearest_price` names its output column
    `adj_close`, so `close` is aliased to that name at the SQL boundary and
    the result renamed back; the alias is confined to these few lines.

    A missing price is logged rather than raised, matching
    `attach_nearest_price`'s own behavior, so a silent NaN does not surface
    only much later inside `DiscreteAllocation`.
    """
    from agentic_portfolio.dataset.fundamentals import attach_nearest_price

    if not tickers:
        return pd.Series(dtype=float, name="close", index=pd.Index([], name="ticker"))

    prices = _load_closes_up_to(tickers, as_of, db_path)
    grid = pd.DataFrame(
        {
            "rebalance_date": pd.to_datetime([as_of] * len(tickers)).astype("datetime64[us]"),
            "ticker": pd.array(tickers, dtype=str),
        }
    )
    merged = attach_nearest_price(grid, prices)
    result = merged.set_index("ticker")["adj_close"].reindex(tickers)
    result.index.name = "ticker"

    missing = result[result.isna()].index.tolist()
    if missing:
        logger.warning("no price on or before %s for ticker(s): %s", as_of, sorted(missing))

    return result.rename("close")


def _load_closes_up_to(tickers: list[str], as_of: date, db_path: str) -> pd.DataFrame:
    """Long-format rows (['date', 'ticker', 'adj_close']) holding each
    ticker's raw `close` up to `as_of` - only the rows
    `attach_nearest_price` could possibly need, not the whole table.

    The column is ALIASED to `adj_close` because that is the name
    `attach_nearest_price` reads and writes; the values are `close`. Opened
    read-only, and a missing file or missing `prices` table yields the
    correctly-shaped empty frame rather than raising, so asking about a
    database never creates one.
    """
    if not tickers:
        return pd.DataFrame(columns=["date", "ticker", "adj_close"])

    placeholders = ", ".join(["?"] * len(tickers))
    try:
        con = duckdb.connect(db_path, read_only=True)
    except duckdb.IOException:
        return pd.DataFrame(columns=["date", "ticker", "adj_close"])
    try:
        df = con.execute(
            f"SELECT date, ticker, close AS adj_close FROM prices "
            f"WHERE ticker IN ({placeholders}) AND date <= ?",
            [*tickers, pd.Timestamp(as_of).date()],
        ).fetchdf()
    except duckdb.CatalogException:
        return pd.DataFrame(columns=["date", "ticker", "adj_close"])
    finally:
        con.close()

    if not df.empty:
        df["date"] = pd.to_datetime(df["date"])
        df["ticker"] = df["ticker"].astype(str)
    return df


def load_ticker_universe(db_path: str = settings.db_path) -> list[str]:
    """Return the sorted, deduplicated list of every ticker that appears in
    sp500_membership at any rebalance date — original Wikipedia-style
    ticker strings, not yfinance symbols.
    """
    con = duckdb.connect(db_path)
    try:
        rows = con.execute("SELECT DISTINCT ticker FROM sp500_membership ORDER BY ticker").fetchall()
    except duckdb.CatalogException as e:
        raise TickerUniverseEmptyError(
            f"No 'sp500_membership' table found in {db_path!r}. Run "
            "`uv run python -m agentic_portfolio.dataset.membership` first."
        ) from e
    finally:
        con.close()
    tickers = [r[0] for r in rows]
    if not tickers:
        raise TickerUniverseEmptyError(f"'sp500_membership' table in {db_path!r} exists but has zero rows.")
    return tickers


def write_prices_tables(prices_df: pd.DataFrame, unresolved_df: pd.DataFrame, db_path: str = settings.db_path) -> None:
    """Write `prices_df` to table `prices` and `unresolved_df` to table
    `unresolved_tickers` in the DuckDB file at `db_path`, creating the
    parent directory if needed. Drops any pre-existing tables first, so
    re-running this is always safe.
    """
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(db_path)
    try:
        con.register("prices_df", prices_df)
        con.execute("DROP TABLE IF EXISTS prices")
        con.execute(
            "CREATE TABLE prices AS "
            "SELECT date::DATE AS date, "
            "ticker::VARCHAR AS ticker, "
            "close::DOUBLE AS close, "
            "adj_close::DOUBLE AS adj_close "
            "FROM prices_df"
        )
        con.unregister("prices_df")

        con.register("unresolved_df", unresolved_df)
        con.execute("DROP TABLE IF EXISTS unresolved_tickers")
        con.execute(
            "CREATE TABLE unresolved_tickers AS "
            "SELECT ticker::VARCHAR AS ticker, reason::VARCHAR AS reason "
            "FROM unresolved_df"
        )
        con.unregister("unresolved_df")
    finally:
        con.close()


def upsert_prices_tables(
    prices_df: pd.DataFrame,
    unresolved_df: pd.DataFrame,
    tickers: list[str],
    db_path: str = settings.db_path,
) -> None:
    """Merge `prices_df`/`unresolved_df` into the `prices`/`unresolved_tickers`
    tables at `db_path`, replacing only the rows for `tickers` — every other
    ticker's existing rows are left untouched, unlike `write_prices_tables`'s
    full drop-and-recreate. `tickers` (not the dataframes' own content) drives
    which rows get deleted first, so this is correct even when a ticker flips
    from resolved to unresolved (or back) between calls: it may be present in
    one dataframe and absent from the other, but its stale row in *both*
    tables is cleared before the new rows are inserted.
    """
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(db_path)
    try:
        con.execute(
            "CREATE TABLE IF NOT EXISTS prices "
            "(date DATE, ticker VARCHAR, close DOUBLE, adj_close DOUBLE)"
        )
        con.execute(
            "CREATE TABLE IF NOT EXISTS unresolved_tickers (ticker VARCHAR, reason VARCHAR)"
        )

        placeholders = ", ".join(["?"] * len(tickers))
        if tickers:
            con.execute(f"DELETE FROM prices WHERE ticker IN ({placeholders})", tickers)
            con.execute(f"DELETE FROM unresolved_tickers WHERE ticker IN ({placeholders})", tickers)

        con.register("prices_df", prices_df)
        con.execute(
            "INSERT INTO prices SELECT date::DATE, ticker::VARCHAR, "
            "close::DOUBLE, adj_close::DOUBLE FROM prices_df"
        )
        con.unregister("prices_df")

        con.register("unresolved_df", unresolved_df)
        con.execute(
            "INSERT INTO unresolved_tickers SELECT ticker::VARCHAR, reason::VARCHAR FROM unresolved_df"
        )
        con.unregister("unresolved_df")
    finally:
        con.close()


def fetch_and_reshape_for_tickers(
    tickers: list[str],
    start: str,
    end: str,
    batch_size: int = settings.price_batch_size,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Fetch, reshape, and detect unresolved tickers for an arbitrary
    `tickers` list (not necessarily S&P 500 members) over `[start, end]`.
    Pure orchestration, no writes — the same fetch/reshape/detect sequence
    `build_price_history` runs for the full membership universe, factored
    out here so it's reusable against any ticker list. Returns
    `(long_prices, unresolved)`.
    """
    ticker_to_symbol = _build_symbol_map(tickers)
    symbol_to_ticker = {v: k for k, v in ticker_to_symbol.items()}

    raw = fetch_price_history(list(symbol_to_ticker.keys()), start, end, batch_size)
    long_prices = reshape_prices_long(raw, symbol_to_ticker)
    unresolved = detect_unresolved_tickers(tickers, long_prices, start=start, end=end)
    return long_prices, unresolved


def build_price_history(
    db_path: str = settings.db_path,
    start: str = settings.fetch_start,
    end: str = settings.fetch_end,
    batch_size: int = settings.price_batch_size,
) -> pd.DataFrame:
    """Fetch, reshape, detect unresolved tickers, write to DuckDB, and
    return the resulting long-format prices DataFrame.
    """
    tickers = load_ticker_universe(db_path)
    long_prices, unresolved = fetch_and_reshape_for_tickers(tickers, start, end, batch_size)

    if long_prices.empty and tickers:
        raise PriceFetchFailedError(
            f"Fetched zero usable rows for all {len(tickers)} tickers; this "
            "almost certainly indicates a network/config problem, not "
            "genuine total delisting. Check connectivity before proceeding."
        )

    write_prices_tables(long_prices, unresolved, db_path)
    logger.info(
        "wrote %d rows to %s::prices, %d unresolved tickers",
        len(long_prices),
        db_path,
        len(unresolved),
    )
    return long_prices


def main() -> None:
    """Console-script entry point (`portfolio-build-prices`), also used by
    `python -m agentic_portfolio.dataset.prices`.
    """
    logging.basicConfig(level=logging.INFO)
    build_price_history()


if __name__ == "__main__":
    main()
