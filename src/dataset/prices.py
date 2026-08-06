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
from pathlib import Path

import duckdb
import pandas as pd
import yfinance as yf

from src.config.settings import settings

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


def to_yfinance_symbol(ticker: str) -> str:
    """Translate a Wikipedia-style ticker to the symbol yfinance expects.

    Wikipedia's share-class tickers use a literal dot ('BRK.B', 'BF.B');
    Yahoo Finance / yfinance expect a dash ('BRK-B', 'BF-B'). Any ticker
    with no dot passes through unchanged. This is a fetch-boundary concern
    only — the `prices` table is keyed by the original ticker string (see
    _build_symbol_map / reshape_prices_long), so later modules that join
    against sp500_membership never need to know this translation happened.
    """
    return ticker.strip().replace(".", "-")


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


def detect_unresolved_tickers(all_tickers: list[str], long_prices: pd.DataFrame) -> pd.DataFrame:
    """Return columns ['ticker', 'reason'] for every ticker in `all_tickers`
    absent from `long_prices['ticker']` — i.e. yfinance never produced a
    single non-null close/adj_close row for it. Pure, no I/O; always
    returns a correctly-typed DataFrame, even when empty.
    """
    present = set(long_prices["ticker"]) if not long_prices.empty else set()
    missing = sorted(set(all_tickers) - present)
    reason = UNRESOLVED_REASON.format(start=settings.fetch_start, end=settings.fetch_end)
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
            "`uv run python -m src.dataset.membership` first."
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
    ticker_to_symbol = _build_symbol_map(tickers)
    symbol_to_ticker = {v: k for k, v in ticker_to_symbol.items()}

    raw = fetch_price_history(list(symbol_to_ticker.keys()), start, end, batch_size)
    long_prices = reshape_prices_long(raw, symbol_to_ticker)

    if long_prices.empty and tickers:
        raise PriceFetchFailedError(
            f"Fetched zero usable rows for all {len(tickers)} tickers; this "
            "almost certainly indicates a network/config problem, not "
            "genuine total delisting. Check connectivity before proceeding."
        )

    unresolved = detect_unresolved_tickers(tickers, long_prices)
    write_prices_tables(long_prices, unresolved, db_path)
    logger.info(
        "wrote %d rows to %s::prices, %d unresolved tickers",
        len(long_prices),
        db_path,
        len(unresolved),
    )
    return long_prices


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    build_price_history()
