"""Per-ex-date corporate actions - cash dividends and stock splits - and the
trailing dividend yield derived from them, for the minimum expected-dividend
constraint in `src/optimizer/portfolio.py`.

The module is named for dividends but owns splits too, because one
`yf.download(..., actions=True)` call returns both and separating them would
mean fetching the same bytes twice. A ticker's splits are needed for two
things the dividends alone cannot express: restating a stored per-share
amount back to the figure that was actually ANNOUNCED at the time (see
`announced_amount`), and noticing that a share count somebody recorded
before a split is no longer the number of shares they hold (see
`src/flow/user_portfolio.py`'s `stale_share_counts`).

Why this module exists at all. Every expected return this project estimates
comes from the `returns` table, which is built from `adj_close` - Yahoo
Finance's dividend-adjusted close - so those returns are TOTAL returns and
already include dividends. What they cannot tell you is how much of that
return arrives as CASH. A user who needs their portfolio to pay a certain
number of dollars a year is asking about the cash part specifically, and
answering that needs the actual per-share amounts, which no other table in
this project holds. `close - adj_close` encodes the dividends cumulatively
(see `reconcile_trailing_yield` below, which exploits exactly that as a
correctness check), but only as a back-adjustment factor, never as the
"$0.485 per share on 2024-03-14" a cash figure has to be built from.

Three layers, the same discipline every other module in `src/dataset/`
follows: `_fetch_batch` is the ONLY function here that performs network
I/O, `reshape_dividends_long` and `trailing_dividend_yields` are pure, and
`write_dividends_tables`/`upsert_dividends_tables` are the only ones that
touch DuckDB.

This module deliberately does NOT reuse `src/dataset/prices.py`'s
`_fetch_batch` by turning on its `actions=True` argument, even though that
would fetch prices and dividends in one pass. yfinance computes the
dividend columns either way and `actions` only decides whether they are
dropped at the end, so the flag changes no price VALUE - but the
all-nan-or-zero row cleanup that runs immediately afterwards
(`yfinance/scrapers/history.py`, the `if not keepna:` block) builds its
column list from whichever columns are present, so with `actions=True` a
row carrying a nonzero dividend and no usable price is KEPT where it would
otherwise be dropped. That can change the row set of the download that
populates the 1.2-million-row `prices` table. One extra pass over the
ticker list is a small price for that table being provably unable to shift,
so the two fetches stay separate.

Two facts about Yahoo's dividend data were verified empirically before this
module was written, because both decide real arithmetic and neither is
readable from the yfinance source:

Dividends are ALREADY SPLIT-ADJUSTED to the current share basis. NVDA split
10:1 on 2024-06-10; the same download reports 0.004 for the 2024-03-05
ex-date (an actual $0.04 payment, divided by 10) and 0.010 for 2024-06-11
(an actual $0.01 payment, unadjusted). So a trailing sum of these amounts
needs no split correction and is directly comparable with `close`, which
`plans/01_dataset.md` records is always split-adjusted regardless of
`auto_adjust`. Had this gone the other way, a dividend paid before a 10:1
split divided by a post-split price would have overstated a yield tenfold.

Dividends are quoted in the SAME unit as prices, minor units included.
BARC.L reports a close of 264.75 and dividends of 5.3 and 2.9 - all pence,
giving Barclays' real ~3% yield. So `apply_price_multipliers`' 0.01 factor
must be applied to dividend amounts exactly as it is to prices, and
applying it is correct rather than a double-scaling: yfinance's own
pence heuristic for dividends sits in a currency-repair path this project
never enables.
"""

from __future__ import annotations

import logging
import time
from datetime import date
from pathlib import Path
from typing import NamedTuple

import duckdb
import pandas as pd
import yfinance as yf

from src.config.settings import settings
from src.dataset.prices import _build_symbol_map, load_latest_close

logger = logging.getLogger(__name__)

DIVIDENDS_LONG_COLUMNS = ["ex_date", "ticker", "amount"]
"""The column shape every `reshape_dividends_long` result has, including an
empty one - `upsert_dividends_tables` and `trailing_dividend_yields` index
into these names, so an empty frame must still carry them rather than being
column-less. The same discipline `src/optimizer/portfolio.py`'s
`RETURNS_LONG_COLUMNS` follows.
"""

class SplitContext(NamedTuple):
    """Everything a report needs to explain one ticker's restated dividends:
    the splits inside the trailing window, and the payments they restated.

    Built only for a ticker that actually split inside the window, so this
    is absent for almost every ticker. `payments` carries the STORED amounts
    - already on the current share basis - paired with their ex-dates, which
    is what lets `announced_amount` turn a real one back into the figure the
    company declared. Deriving a per-payment amount by dividing the trailing
    total would be wrong for any payer whose dividend changed during the
    window, which is exactly the payer most likely to have split.
    """

    splits: list[tuple[date, float]]
    payments: list[tuple[date, float]]


SPLITS_LONG_COLUMNS = ["ex_date", "ticker", "ratio"]
"""The column shape every `reshape_splits_long` result has, empty ones
included, for the same reason `DIVIDENDS_LONG_COLUMNS` is pinned."""

COVERAGE_COLUMNS = ["ticker", "fetch_start", "fetch_end"]
"""The column shape of every `dividend_coverage` frame, empty ones included,
for the same reason `DIVIDENDS_LONG_COLUMNS` is pinned: the INSERT names
these columns and an empty frame must still satisfy it.
"""


class DividendFieldMissingError(RuntimeError):
    """Raised when a yfinance batch's returned columns don't include
    'Dividends' - a sign the `actions=True` column contract this module
    relies on has changed and its reshape logic is now wrong.

    The counterpart to `src/dataset/prices.py`'s `PriceFieldMissingError`,
    and raised for the same reason: an external schema change should fail
    loudly here rather than quietly produce a table of zero dividends,
    which would read as "nothing pays a dividend" and silently make every
    dividend floor unreachable.
    """


def _fetch_batch(symbols: list[str], start: str, end: str) -> pd.DataFrame:
    """Download one batch of symbols WITH dividend actions. The only
    function in this module that performs network I/O.

    `auto_adjust=False` matches `src/dataset/prices.py`'s own call so the
    `Close` column here means the same thing it means there, and
    `actions=True` is what adds the `Dividends` and `Stock Splits`
    top-level fields to the same 2-level (field, symbol) column index.
    """
    return yf.download(
        symbols,
        start=start,
        end=end,
        auto_adjust=False,
        actions=True,
        group_by="column",
        threads=True,
        progress=False,
    )


def fetch_dividend_history(
    symbols: list[str],
    start: str = settings.fetch_start,
    end: str = settings.fetch_end,
    batch_size: int = settings.price_batch_size,
    pause_seconds: float = settings.yfinance_price_pause_seconds,
) -> pd.DataFrame:
    """Fetch `symbols` in chunks of `batch_size`, pausing briefly between
    batches as cheap insurance against rate-limiting, and concatenate the
    results column-wise - mirroring `src/dataset/prices.py`'s
    `fetch_price_history` exactly, since each batch contributes disjoint
    symbol columns under the same (field, symbol) MultiIndex shape.
    """
    if not symbols:
        return pd.DataFrame()

    chunks = [symbols[i : i + batch_size] for i in range(0, len(symbols), batch_size)]
    frames = []
    for i, chunk in enumerate(chunks, start=1):
        logger.info("fetching dividends for batch %d/%d (%d symbols)", i, len(chunks), len(chunk))
        frames.append(_fetch_batch(chunk, start, end))
        if i < len(chunks):
            time.sleep(pause_seconds)
    return pd.concat(frames, axis=1) if len(frames) > 1 else frames[0]


def reshape_dividends_long(raw: pd.DataFrame, symbol_to_ticker: dict[str, str]) -> pd.DataFrame:
    """Reshape yfinance's multi-ticker `actions=True` output into long
    format, keeping ONLY the rows that carry an actual payment.

    `raw` has 2-level MultiIndex columns (field, symbol) with a
    'Dividends' field among them. Zero and null amounts are dropped, which
    is the whole reshape: yfinance zero-fills the dividend column on every
    trading day, so keeping them would store ~250 rows a year per ticker to
    express four payments. A ticker that pays nothing therefore contributes
    NO rows at all, and `trailing_dividend_yields` reads that absence as a
    genuine zero yield only when the ticker is known to have been fetched -
    see that function on why "pays nothing" and "was never fetched" must
    not be conflated.

    Symbol columns are translated back to the original ticker string via
    `symbol_to_ticker`, so every stored row is keyed the way the rest of
    this project keys tickers.

    Returns columns ['ex_date', 'ticker', 'amount'], no I/O.
    """
    if raw.empty:
        return pd.DataFrame(columns=DIVIDENDS_LONG_COLUMNS)

    top_level = set(raw.columns.get_level_values(0))
    if "Dividends" not in top_level:
        # Distinguish a broken contract from a batch that simply failed.
        # yfinance represents a ticker it could not fetch with
        # `utils.empty_df()`, whose columns are Open/High/Low/Close/Adj
        # Close/Volume and NOTHING else - so a batch where every symbol
        # failed legitimately has no 'Dividends' column, and raising there
        # would turn a rate-limited batch into a crash. Prices having
        # arrived without the actions columns is the real contract change.
        if "Close" in top_level and bool(raw["Close"].notna().to_numpy().any()):
            raise DividendFieldMissingError(
                f"Expected a 'Dividends' column, got top-level fields {sorted(top_level)}. "
                "yfinance's actions=True column contract may have changed."
            )
        logger.warning(
            "dividend batch returned no usable data (top-level fields %s); treating as empty",
            sorted(top_level),
        )
        return pd.DataFrame(columns=DIVIDENDS_LONG_COLUMNS)

    long = raw["Dividends"].stack().reset_index()
    long.columns = ["ex_date", "symbol", "amount"]
    long = long[long["amount"].notna() & (long["amount"] != 0)]

    long["ticker"] = long["symbol"].map(symbol_to_ticker)
    unmapped = long["ticker"].isna()
    if unmapped.any():
        logger.warning(
            "dropping %d dividend rows with unmapped symbols: %s",
            int(unmapped.sum()),
            sorted(long.loc[unmapped, "symbol"].unique()),
        )
        long = long[~unmapped]

    long["ex_date"] = pd.to_datetime(long["ex_date"]).dt.normalize()
    return (
        long[DIVIDENDS_LONG_COLUMNS]
        .sort_values(["ticker", "ex_date"])
        .reset_index(drop=True)
    )


def reshape_splits_long(raw: pd.DataFrame, symbol_to_ticker: dict[str, str]) -> pd.DataFrame:
    """Reshape yfinance's `actions=True` split block into long format,
    keeping only the rows that carry an actual split.

    The exact counterpart of `reshape_dividends_long`, and drops the same
    two kinds of non-row for the same reasons: yfinance zero-fills the
    `Stock Splits` column on every trading day, so keeping zeros would store
    ~250 rows a year to express one event, and a NaN appears wherever
    another ticker in the batch introduced a date this one has no data for.
    Neither is a split.

    A missing `Stock Splits` column is NOT an error here, unlike a missing
    `Dividends` column in `reshape_dividends_long`. That one is the field
    every derived yield depends on, so its absence means the contract
    changed and silence would produce a table of zero dividends. Splits are
    explanatory: without them a report loses a sentence, not a number, and a
    batch of tickers that have simply never split legitimately has nothing
    to say. So this degrades to an empty frame and logs it.

    Returns columns ['ex_date', 'ticker', 'ratio'], no I/O. Ratios are
    stored exactly as reported and are deliberately NOT scaled by any
    currency multiplier: a split ratio is a pure number, denominated in
    nothing.
    """
    if raw.empty:
        return pd.DataFrame(columns=SPLITS_LONG_COLUMNS)

    top_level = set(raw.columns.get_level_values(0))
    if "Stock Splits" not in top_level:
        logger.info(
            "no 'Stock Splits' column in this batch (top-level fields %s); "
            "recording no splits",
            sorted(top_level),
        )
        return pd.DataFrame(columns=SPLITS_LONG_COLUMNS)

    long = raw["Stock Splits"].stack().reset_index()
    long.columns = ["ex_date", "symbol", "ratio"]
    long = long[long["ratio"].notna() & (long["ratio"] != 0)]

    long["ticker"] = long["symbol"].map(symbol_to_ticker)
    unmapped = long["ticker"].isna()
    if unmapped.any():
        logger.warning(
            "dropping %d split rows with unmapped symbols: %s",
            int(unmapped.sum()),
            sorted(long.loc[unmapped, "symbol"].unique()),
        )
        long = long[~unmapped]

    long["ex_date"] = pd.to_datetime(long["ex_date"]).dt.normalize()
    return (
        long[SPLITS_LONG_COLUMNS]
        .sort_values(["ticker", "ex_date"])
        .reset_index(drop=True)
    )


def apply_dividend_multipliers(
    long_dividends: pd.DataFrame, multipliers: dict[str, float]
) -> pd.DataFrame:
    """Scale each ticker's dividend `amount` by its price multiplier, so a
    London pence dividend becomes a pound dividend before anything is
    stored - the exact counterpart of
    `src/dataset/ticker_currency.py`'s `apply_price_multipliers`, and
    required for the same reason: every consumer must see one unit per
    ticker.

    This one matters more than it looks. Prices and dividends are quoted in
    the same unit, so scaling only the prices would leave a `.L` holding's
    yield a hundred times too large - a 3% yielder reported at 310%, which
    would satisfy any dividend floor a user could type. A ticker with no
    entry in `multipliers` is left alone (multiplier 1.0 is the norm).
    """
    if long_dividends.empty or not multipliers:
        return long_dividends

    scaled = long_dividends.copy()
    factors = scaled["ticker"].map(multipliers).fillna(1.0).astype(float)
    scaled["amount"] = scaled["amount"].astype(float) * factors
    return scaled


def write_dividends_tables(
    dividends_df: pd.DataFrame,
    coverage_df: pd.DataFrame,
    db_path: str = settings.db_path,
    splits_df: pd.DataFrame | None = None,
) -> None:
    """Write `dividends_df` to table `dividends`, `splits_df` to table
    `splits`, and `coverage_df` to table `dividend_coverage` in the DuckDB
    file at `db_path`, creating the parent directory if needed. Drops any
    pre-existing tables first, so re-running this is always safe.

    All three land together because one fetch produces all three, and a
    dividend stored without the split that restated it cannot be explained
    to a reader. `splits_df=None` writes an empty `splits` table rather than
    skipping it, so the table always exists once this has run and no reader
    has to distinguish "no splits" from "no table".
    """
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(db_path)
    try:
        con.register("dividends_df", dividends_df)
        con.execute("DROP TABLE IF EXISTS dividends")
        con.execute(
            "CREATE TABLE dividends AS "
            "SELECT ex_date::DATE AS ex_date, "
            "ticker::VARCHAR AS ticker, "
            "amount::DOUBLE AS amount "
            "FROM dividends_df"
        )
        con.unregister("dividends_df")

        splits = splits_df if splits_df is not None else pd.DataFrame(columns=SPLITS_LONG_COLUMNS)
        con.register("splits_df", splits)
        con.execute("DROP TABLE IF EXISTS splits")
        con.execute(
            "CREATE TABLE splits AS "
            "SELECT ex_date::DATE AS ex_date, "
            "ticker::VARCHAR AS ticker, "
            "ratio::DOUBLE AS ratio "
            "FROM splits_df"
        )
        con.unregister("splits_df")

        con.register("coverage_df", coverage_df)
        con.execute("DROP TABLE IF EXISTS dividend_coverage")
        con.execute(
            "CREATE TABLE dividend_coverage AS "
            "SELECT ticker::VARCHAR AS ticker, "
            "fetch_start::DATE AS fetch_start, "
            "fetch_end::DATE AS fetch_end "
            "FROM coverage_df"
        )
        con.unregister("coverage_df")
    finally:
        con.close()


def upsert_dividends_tables(
    dividends_df: pd.DataFrame,
    coverage_df: pd.DataFrame,
    tickers: list[str],
    db_path: str = settings.db_path,
    splits_df: pd.DataFrame | None = None,
) -> None:
    """Merge `dividends_df`/`coverage_df` into the
    `dividends`/`dividend_coverage` tables at `db_path`, replacing only the
    rows for `tickers` - every other ticker's existing rows are left
    untouched, unlike `write_dividends_tables`'s full drop-and-recreate.

    `tickers` (not either dataframe's own content) drives which rows get
    deleted first, exactly as in `src/dataset/prices.py`'s
    `upsert_prices_tables`, and for a reason that bites harder here than it
    does for prices: a ticker that pays nothing contributes no dividend
    rows at all, so keying the delete off the dataframe would leave a
    former payer's rows behind forever after it suspended its dividend, and
    that ticker would keep reporting the income it no longer pays. Being
    ASKED about a ticker is what clears it; having rows is not.

    Re-running with the same inputs is therefore idempotent rather than
    duplicating rows.

    `splits_df` is merged the same way and for the same reason: a ticker
    that has never split contributes no rows, so only the requested-ticker
    list can clear a stale one. `None` clears the named tickers' splits
    without inserting any, which is the correct outcome for a caller that
    genuinely fetched no split data.
    """
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(db_path)
    try:
        con.execute(
            "CREATE TABLE IF NOT EXISTS dividends "
            "(ex_date DATE, ticker VARCHAR, amount DOUBLE)"
        )
        con.execute(
            "CREATE TABLE IF NOT EXISTS splits "
            "(ex_date DATE, ticker VARCHAR, ratio DOUBLE)"
        )
        con.execute(
            "CREATE TABLE IF NOT EXISTS dividend_coverage "
            "(ticker VARCHAR, fetch_start DATE, fetch_end DATE)"
        )
        if tickers:
            placeholders = ", ".join(["?"] * len(tickers))
            con.execute(f"DELETE FROM dividends WHERE ticker IN ({placeholders})", tickers)
            con.execute(f"DELETE FROM splits WHERE ticker IN ({placeholders})", tickers)
            con.execute(
                f"DELETE FROM dividend_coverage WHERE ticker IN ({placeholders})", tickers
            )

        con.register("dividends_df", dividends_df)
        con.execute(
            "INSERT INTO dividends SELECT ex_date::DATE, ticker::VARCHAR, amount::DOUBLE "
            "FROM dividends_df"
        )
        con.unregister("dividends_df")

        splits = splits_df if splits_df is not None else pd.DataFrame(columns=SPLITS_LONG_COLUMNS)
        con.register("splits_df", splits)
        con.execute(
            "INSERT INTO splits SELECT ex_date::DATE, ticker::VARCHAR, ratio::DOUBLE "
            "FROM splits_df"
        )
        con.unregister("splits_df")

        con.register("coverage_df", coverage_df)
        con.execute(
            "INSERT INTO dividend_coverage SELECT ticker::VARCHAR, "
            "fetch_start::DATE, fetch_end::DATE FROM coverage_df"
        )
        con.unregister("coverage_df")
    finally:
        con.close()


def load_dividends_long(
    tickers: list[str],
    window_start: pd.Timestamp | date | None,
    window_end: pd.Timestamp | date | None,
    db_path: str = settings.db_path,
    read_only: bool = True,
) -> pd.DataFrame:
    """Long-format rows (['ex_date', 'ticker', 'amount']) from the
    `dividends` table for `tickers`, restricted to the closed date range
    [`window_start`, `window_end`].

    `read_only=True` is the DEFAULT here, unlike
    `src/optimizer/portfolio.py`'s `load_returns_long` where it is opt-in.
    Every caller of this function is asking a question rather than building
    anything, and a missing file or missing `dividends` table reports an
    empty frame instead of raising - which is what lets a database built
    before this table existed still be read, reporting "no dividend data"
    rather than crashing a report that would otherwise have worked.
    """
    if window_end is None or not tickers:
        return pd.DataFrame(columns=DIVIDENDS_LONG_COLUMNS)

    placeholders = ", ".join(["?"] * len(tickers))
    clauses = [f"ticker IN ({placeholders})", "ex_date <= ?"]
    params: list[object] = [*tickers, pd.Timestamp(window_end).date()]
    if window_start is not None:
        clauses.append("ex_date >= ?")
        params.append(pd.Timestamp(window_start).date())

    try:
        con = duckdb.connect(db_path, read_only=True) if read_only else duckdb.connect(db_path)
    except duckdb.IOException:
        return pd.DataFrame(columns=DIVIDENDS_LONG_COLUMNS)
    try:
        df = con.execute(
            f"SELECT ex_date, ticker, amount FROM dividends WHERE {' AND '.join(clauses)}",
            params,
        ).fetchdf()
    except duckdb.CatalogException:
        return pd.DataFrame(columns=DIVIDENDS_LONG_COLUMNS)
    finally:
        con.close()
    df["ex_date"] = pd.to_datetime(df["ex_date"])
    return df


def load_splits_long(
    tickers: list[str],
    db_path: str = settings.db_path,
    after: date | None = None,
) -> pd.DataFrame:
    """Long-format rows (['ex_date', 'ticker', 'ratio']) from the `splits`
    table for `tickers`, optionally restricted to `ex_date > after`.

    Read-only, and a missing file or missing `splits` table reports an empty
    frame rather than raising - a database built before this table existed
    must still be readable, losing an explanatory sentence rather than
    failing a report that would otherwise have worked. Same discipline as
    `load_dividends_long`.
    """
    if not tickers:
        return pd.DataFrame(columns=SPLITS_LONG_COLUMNS)

    placeholders = ", ".join(["?"] * len(tickers))
    clauses = [f"ticker IN ({placeholders})"]
    params: list[object] = list(tickers)
    if after is not None:
        clauses.append("ex_date > ?")
        params.append(pd.Timestamp(after).date())

    try:
        con = duckdb.connect(db_path, read_only=True)
    except duckdb.IOException:
        return pd.DataFrame(columns=SPLITS_LONG_COLUMNS)
    try:
        df = con.execute(
            f"SELECT ex_date, ticker, ratio FROM splits WHERE {' AND '.join(clauses)} "
            "ORDER BY ticker, ex_date",
            params,
        ).fetchdf()
    except duckdb.CatalogException:
        return pd.DataFrame(columns=SPLITS_LONG_COLUMNS)
    finally:
        con.close()
    df["ex_date"] = pd.to_datetime(df["ex_date"])
    return df


def has_splits_table(db_path: str) -> bool:
    """Whether `db_path` has a `splits` table at all.

    Deliberately asks about the TABLE and never about rows. A ticker that
    has never split contributes no split rows, so "no rows for this ticker"
    cannot distinguish a database that predates this table from one that has
    it and correctly holds nothing - the same trap `dividend_coverage` exists
    to avoid for dividends. Table existence is a fact about the database's
    vintage, which is exactly the question a one-time migration needs
    answered.

    Read-only, and a missing file reports `False` rather than raising or
    creating one.
    """
    try:
        con = duckdb.connect(db_path, read_only=True)
    except duckdb.IOException:
        return False
    try:
        con.execute("SELECT 1 FROM splits LIMIT 1")
        return True
    except duckdb.CatalogException:
        return False
    finally:
        con.close()


def split_series_by_ticker(long_df: pd.DataFrame) -> dict[str, pd.Series]:
    """`{ticker: Series(ratio, indexed by ex_date)}` from a
    `load_splits_long` frame.

    Exists so that `src/dataset/fundamentals.py`'s
    `cumulative_split_ratio_after` - which this project has used for share-count
    correction since plan 1 and which takes exactly that shape - can be reused
    verbatim rather than reimplemented over a DataFrame. Pure, no I/O.
    """
    if long_df.empty:
        return {}
    return {
        str(ticker): group.set_index("ex_date")["ratio"].astype(float)
        for ticker, group in long_df.groupby("ticker")
    }


def announced_amount(stored_amount: float, ex_date: date, splits: pd.Series | None) -> float:
    """The per-share dividend as it was ANNOUNCED on `ex_date`, recovered
    from the `stored_amount` that Yahoo Finance restated onto the current
    share basis.

    Yahoo reports every dividend on the ticker's present share basis, so a
    payment made before a split is divided down by every split since. This
    multiplies it back, which is the one thing that lets a report quote the
    figure a reader would find in a company announcement and reconcile it
    against the figure the optimizer used.

    Worked example, verified against real data: 9984.T split 4:1 on
    2025-12-29 and its 2025-09-29 payment is stored as 5.5, so the
    cumulative ratio after that ex-date is 4.0 and the announced amount was
    22.0 - exactly what was declared. The 2026-03-30 payment, after the
    split, has a ratio of 1.0 and is unchanged at 5.5.

    Reuses `cumulative_split_ratio_after` (src/dataset/fundamentals.py),
    which returns 1.0 for a ticker that never split, so this is a no-op in
    the common case. Pure, no I/O.
    """
    from src.dataset.fundamentals import cumulative_split_ratio_after

    if splits is None or splits.empty:
        return float(stored_amount)
    return float(stored_amount) * cumulative_split_ratio_after(splits, ex_date)


def fetched_dividend_tickers(raw: pd.DataFrame, symbol_to_ticker: dict[str, str]) -> set[str]:
    """Which tickers the fetch actually returned a dividend column for.

    yfinance represents a symbol it could not fetch with `utils.empty_df()`,
    which carries no 'Dividends' column at all, so that symbol is simply
    absent from `raw["Dividends"]`. Reading the surviving columns is
    therefore the honest way to record coverage: it separates "this ticker
    was fetched and pays nothing" from "this ticker's fetch failed", which
    is exactly the distinction `coverage_frame` exists to preserve. Guessing
    instead - assuming every requested ticker resolved - would give a failed
    fetch a coverage row and let it report a confident `0.0` yield.

    Pure, no I/O.
    """
    if raw.empty or "Dividends" not in set(raw.columns.get_level_values(0)):
        return set()
    present = set(raw["Dividends"].columns)
    return {symbol_to_ticker[sym] for sym in present if sym in symbol_to_ticker}


def coverage_frame(
    tickers: list[str],
    unresolved: set[str],
    start: str | date,
    end: str | date,
) -> pd.DataFrame:
    """The `dividend_coverage` rows to record for one fetch: one row per
    requested ticker that actually resolved, naming the window that was
    asked for.

    This tiny table earns its place by answering the one question the
    `dividends` table cannot. A genuine non-payer contributes no dividend
    rows, and so does a ticker nobody ever fetched - and those two must
    never be treated alike. Without a coverage record, a database whose
    prices were built before dividends existed would report every ticker
    as yielding exactly `0.0`, and a user could be shown a portfolio
    "meeting" an income requirement computed entirely from absent data.
    With it, the first case is a reliable zero and the second is reported
    by name. This is the same job `unresolved_tickers` does for prices: a
    table whose only purpose is to record what the fetch attempted.
    """
    resolved = [t for t in tickers if t not in unresolved]
    return pd.DataFrame(
        {
            "ticker": pd.array(resolved, dtype=str),
            "fetch_start": pd.to_datetime([pd.Timestamp(start)] * len(resolved)),
            "fetch_end": pd.to_datetime([pd.Timestamp(end)] * len(resolved)),
        },
        columns=COVERAGE_COLUMNS,
    )


def tickers_with_dividend_data(tickers: list[str], db_path: str = settings.db_path) -> set[str]:
    """Which of `tickers` this project has actually FETCHED dividend data
    for, whether or not that fetch found any payment - read from the
    `dividend_coverage` table written by `coverage_frame`.

    Deliberately NOT inferred from the presence of price rows. Dividends
    are fetched separately from prices (see the module docstring), so a
    database built before this feature existed has prices for every ticker
    and dividends for none; inferring coverage from prices there would
    report the whole universe as paying exactly nothing. Asking the
    coverage table instead makes a pre-existing database honestly report
    "no dividend data" until it is built, which is a fixable state rather
    than a silently wrong number.

    Read-only, and a missing file or missing table reports the empty set
    rather than raising - the same discipline
    `src/dataset/ticker_currency.py`'s `load_ticker_currencies` follows, so
    asking a question never creates a database or takes a write lock.
    """
    if not tickers:
        return set()

    placeholders = ", ".join(["?"] * len(tickers))
    try:
        con = duckdb.connect(db_path, read_only=True)
    except duckdb.IOException:
        return set()
    try:
        rows = con.execute(
            f"SELECT DISTINCT ticker FROM dividend_coverage WHERE ticker IN ({placeholders})",
            tickers,
        ).fetchall()
    except duckdb.CatalogException:
        return set()
    finally:
        con.close()
    return {str(r[0]) for r in rows}


def trailing_dividends_per_share(
    long_df: pd.DataFrame,
    tickers: list[str],
    as_of: date,
    lookback_months: int = 12,
) -> dict[str, float]:
    """Total dividends per share with an ex-date in the `lookback_months`
    months ending at `as_of`, per ticker, as `{ticker: amount}` with a
    `0.0` for every ticker that has no payment in that window.

    Pure function, no I/O - `long_df` comes from `load_dividends_long`.

    The window is half-open at the start and closed at the end:
    `as_of - lookback_months < ex_date <= as_of`. So a dividend whose
    ex-date falls EXACTLY `lookback_months` before `as_of` is excluded,
    which is what stops a payer on a steady annual schedule from having two
    of its payments counted in one twelve-month window and reporting double
    its real yield in the one run whose date happens to land on an
    anniversary.

    No split adjustment is applied or needed: Yahoo Finance already reports
    these amounts on the current share basis (see the module docstring's
    NVDA evidence), which is the same basis `close` is on.
    """
    zeros = {ticker: 0.0 for ticker in tickers}
    if long_df.empty:
        return zeros

    window_start = pd.Timestamp(as_of) - pd.DateOffset(months=lookback_months)
    in_window = long_df[
        (pd.to_datetime(long_df["ex_date"]) > window_start)
        & (pd.to_datetime(long_df["ex_date"]) <= pd.Timestamp(as_of))
    ]
    if in_window.empty:
        return zeros

    totals = in_window.groupby("ticker")["amount"].sum()
    return {ticker: float(totals.get(ticker, 0.0)) for ticker in tickers}


def trailing_dividend_yields(
    dividends_per_share: dict[str, float],
    prices: pd.Series,
    known_tickers: set[str] | None = None,
) -> tuple[dict[str, float], dict[str, str]]:
    """Annual dividend yield per ticker as
    `({ticker: yield}, {ticker: reason_unavailable})`.

    A yield is trailing cash per share over the price of one share, so
    `prices` must be RAW `close`, not `adj_close`. The two are not
    interchangeable here even though the newest `adj_close` equals the
    newest raw close: `adj_close` is back-adjusted, so at any earlier date
    it is lower than the price anyone could have paid, and dividing a real
    cash amount by it would overstate the yield. Passing `close`
    explicitly makes that a caller's stated choice rather than an accident
    of which column happened to be loaded.

    The two return values are the point of this function. A ticker in the
    first dict has a yield that can be relied on, INCLUDING a genuine
    non-payer at exactly `0.0`. A ticker in the second has no yield and a
    sentence saying why - no price, a non-positive price, or no dividend
    data ever fetched (see `tickers_with_dividend_data`). Collapsing the
    second group into zero would be the dangerous simplification: a
    dividend floor would then be silently computed against tickers whose
    contribution is unknown rather than known to be nothing, and a
    portfolio could be reported as meeting an income requirement it may
    not meet. Callers must decide what to do about an unavailable ticker,
    and this project's convention is to name it in the report rather than
    let it disappear.

    `known_tickers=None` means "assume every ticker in
    `dividends_per_share` was fetched", which is what a caller working from
    a fixture or an already-verified pool wants.
    """
    yields: dict[str, float] = {}
    unavailable: dict[str, str] = {}

    for ticker, per_share in dividends_per_share.items():
        if known_tickers is not None and ticker not in known_tickers:
            unavailable[ticker] = "no dividend data has been fetched for it"
            continue

        price = prices.get(ticker) if ticker in prices.index else None
        if price is None or pd.isna(price):
            unavailable[ticker] = "no price to divide its dividends by"
            continue
        if float(price) <= 0:
            unavailable[ticker] = f"its price is {float(price)!r}, which no yield can be computed from"
            continue

        yields[ticker] = float(per_share) / float(price)

    return yields, unavailable


def reconcile_trailing_yield(
    prices_df: pd.DataFrame, as_of: date, lookback_months: int = 12
) -> float | None:
    """The trailing dividend return implied by ONE ticker's own
    `close`/`adj_close` pair, computed without any dividend data at all -
    or `None` when `prices_df` does not span the window.

    `prices_df` has columns ['date', 'close', 'adj_close'] for a single
    ticker. This is an independent second opinion on
    `trailing_dividend_yields`, and it exists because the dividend table is
    fetched from an external source whose conventions this project cannot
    control, while the price table has been on disk and in use for a long
    time.

    How it works. yfinance's `Close` is always split-adjusted regardless of
    `auto_adjust`, so in this project's `prices` table `close` and
    `adj_close` differ ONLY by dividend adjustment. That makes
    `adj_close / close` the cumulative dividend-reinvestment factor, and
    the ratio of the two series' growth over a window isolates the
    dividend contribution:

        (adj_close_t / adj_close_start) / (close_t / close_start) - 1

    Run over the last twelve months of the shipped `data/portfolio.duckdb`,
    this reproduces recognizable real-world figures - 0.0715 for T, 0.0358
    for XOM, 0.0319 for KO, 0.0053 for AAPL - and exactly 0.0 for GOOGL,
    which paid no dividend in that period.

    This is deliberately NOT the same quantity as a trailing yield, and the
    difference is not an error in either. This one compounds reinvestment
    at every ex-date and measures each payment against the price prevailing
    when it was paid; a trailing yield sums nominal cash and divides by one
    final price. They agree closely for a steady payer whose price did not
    move much, and diverge for one whose price moved a lot - which is why
    the test that uses this asserts agreement within a tolerance rather
    than equality, and why the trailing yield remains the reported figure.
    """
    if prices_df.empty:
        return None

    frame = prices_df.copy()
    frame["date"] = pd.to_datetime(frame["date"])
    frame = frame.sort_values("date").set_index("date")

    window_start = pd.Timestamp(as_of) - pd.DateOffset(months=lookback_months)
    before = frame.loc[:window_start]
    upto = frame.loc[:pd.Timestamp(as_of)]
    if before.empty or upto.empty:
        return None

    start_row, end_row = before.iloc[-1], upto.iloc[-1]
    if any(
        pd.isna(v) or float(v) <= 0
        for v in (start_row["close"], start_row["adj_close"], end_row["close"], end_row["adj_close"])
    ):
        return None

    total_growth = float(end_row["adj_close"]) / float(start_row["adj_close"])
    price_growth = float(end_row["close"]) / float(start_row["close"])
    return total_growth / price_growth - 1.0


def build_dividends_for_tickers(
    tickers: list[str],
    db_path: str,
    start: str,
    end: str,
    multipliers: dict[str, float] | None = None,
    unresolved: set[str] | None = None,
) -> pd.DataFrame:
    """Fetch, reshape, unit-normalize and upsert dividend history for
    `tickers` into `db_path`, returning the long frame that was stored.

    The on-demand counterpart of `main`'s whole-universe build, and what
    `src/dataset/ticker_ingestion.py` calls so a user-provided ticker gets
    dividend rows on the same pass that already fetches its prices,
    currency and returns. Without that, every user-provided pool would have
    prices but no yields, and a dividend floor would be unusable in exactly
    the mode people use most.

    `multipliers` should be the same `{ticker: price_multiplier}` mapping
    applied to that ticker's prices, so pence dividends are converted to
    pounds identically. `unresolved` names the tickers the caller already
    knows yfinance could not resolve, which are recorded as having NO
    dividend coverage - so a typo reports "no dividend data has been
    fetched for it" rather than the flat zero yield a coverage row would
    imply. Never raises for a ticker yfinance knows nothing about.
    """
    cleaned = sorted({t.strip().upper() for t in tickers if t.strip()})
    if not cleaned:
        return pd.DataFrame(columns=DIVIDENDS_LONG_COLUMNS)

    symbol_map = _build_symbol_map(cleaned)
    symbol_to_ticker = {v: k for k, v in symbol_map.items()}
    raw = fetch_dividend_history(list(symbol_map.values()), start=start, end=end)
    long_dividends = reshape_dividends_long(raw, symbol_to_ticker)
    long_dividends = apply_dividend_multipliers(long_dividends, multipliers or {})
    # Deliberately NOT put through `apply_dividend_multipliers`: a split
    # ratio is a pure number, not an amount denominated in a currency.
    long_splits = reshape_splits_long(raw, symbol_to_ticker)

    # Coverage is what the fetch actually returned, minus anything the
    # caller already knows is unusable (an unresolved price fetch, or a
    # ticker whose currency could not be classified - without a currency we
    # cannot state the dividend's unit, so recording no coverage is more
    # honest than storing an unscaled amount).
    fetched = fetched_dividend_tickers(raw, symbol_to_ticker) - (unresolved or set())
    missing = [t for t in cleaned if t not in fetched]
    if missing:
        logger.warning("no dividend data returned for ticker(s): %s", missing)

    upsert_dividends_tables(
        long_dividends,
        coverage_frame(cleaned, set(cleaned) - fetched, start, end),
        cleaned,
        db_path,
        splits_df=long_splits,
    )
    return long_dividends


def build_dividends(
    db_path: str = settings.db_path,
    start: str = settings.fetch_start,
    end: str = settings.fetch_end,
) -> pd.DataFrame:
    """Build the whole `dividends` table for every ticker in the `prices`
    table at `db_path`, returning the long frame that was stored.

    WHEN TO RUN THIS, since it is the question the command's name does not
    answer. Its one job is populating `data/portfolio.duckdb`, so run it
    after `uv run portfolio-build-prices`, exactly as `-returns` and
    `-momentum` are run. That database is read by one path only: `uv run
    portfolio` at a HISTORICAL date with a screened selection
    (`llm_s_only`, `llm_f_only`, `llm_s_and_f`), which reads the shared
    cache read-only and therefore cannot fetch its own dividends.

    Every other path needs nothing. A live-mode run builds dividends into
    its own throwaway snapshot (`src/flow/live.py`'s `build_live_snapshot`,
    which calls this function). `--selection user_provided` fetches per
    typed ticker through `src/dataset/ticker_ingestion.py`. And the
    holdings report maintains `data/holdings.duckdb` on its own monthly
    staleness rule, forced by `--refresh-holdings`.

    The universe comes from `prices` rather than from `sp500_membership`
    because `prices` is what the optimizer can actually price and measure,
    and it already includes every ticker any earlier run ingested - a
    superset that costs nothing extra to cover.

    Upserts by ticker rather than dropping the table, so a partially
    completed run can be repeated and a ticker that has since stopped
    paying has its stale rows cleared rather than kept.
    """
    con = duckdb.connect(db_path, read_only=True)
    try:
        tickers = [str(r[0]) for r in con.execute("SELECT DISTINCT ticker FROM prices").fetchall()]
    finally:
        con.close()

    if not tickers:
        raise RuntimeError(
            f"{db_path} has no rows in its `prices` table, so there is no ticker universe "
            "to fetch dividends for. Run `uv run portfolio-build-prices` first."
        )

    multipliers = _stored_price_multipliers(tickers, db_path)
    logger.info("building dividends for %d ticker(s) from %s to %s", len(tickers), start, end)
    return build_dividends_for_tickers(sorted(tickers), db_path, start, end, multipliers)


def _stored_price_multipliers(tickers: list[str], db_path: str) -> dict[str, float]:
    """The `{ticker: price_multiplier}` mapping already recorded in the
    `ticker_currency` table, so a rebuild scales pence dividends the same
    way the original ingestion scaled that ticker's prices.

    A missing table means no ticker was ever currency-classified, which is
    the normal state of the S&P 500 universe database (see
    `src/dataset/ticker_currency.py`'s `DEFAULT_CURRENCY`) and correctly
    yields an empty mapping - every multiplier is then 1.0.
    """
    if not tickers:
        return {}
    try:
        con = duckdb.connect(db_path, read_only=True)
    except duckdb.IOException:
        return {}
    try:
        placeholders = ", ".join(["?"] * len(tickers))
        rows = con.execute(
            f"SELECT ticker, price_multiplier FROM ticker_currency WHERE ticker IN ({placeholders})",
            tickers,
        ).fetchall()
    except duckdb.CatalogException:
        return {}
    finally:
        con.close()
    return {str(t): float(m) for t, m in rows if m is not None}


def main() -> None:
    """Entry point for `uv run portfolio-build-dividends`.

    Idempotent: re-running replaces each ticker's rows rather than
    appending, so a repeat costs a fetch but never duplicates a payment.
    """
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    df = build_dividends()
    payers = df["ticker"].nunique() if not df.empty else 0
    print(f"Wrote {len(df)} dividend row(s) for {payers} paying ticker(s) to {settings.db_path}.")
    # Said here because the command's name does not say it, and the answer
    # is otherwise only findable by tracing which database each mode reads.
    print(
        "This is only needed for 'uv run portfolio' at a historical date with a screened "
        "selection, which reads this database read-only. Live-mode runs build their own "
        "dividends, --selection user_provided fetches per ticker, and the holdings report "
        "maintains its own cache."
    )


if __name__ == "__main__":
    main()


def load_dividend_figures(
    tickers: list[str],
    as_of: date,
    db_path: str,
    lookback_months: int = settings.dividend_lookback_months,
) -> tuple[dict[str, float], dict[str, float], dict[str, str], dict[str, list[tuple[date, float]]]]:
    """The one function a consumer needs:
    `(yields, dividends_per_share, unavailable, splits_in_window)` for
    `tickers` as of `as_of`, read from `db_path`.

    `splits_in_window` maps a ticker to a `SplitContext` when - and only
    when - that ticker split inside the same trailing window the dividends
    were summed over, so it is empty for the overwhelming majority of
    tickers. It is returned here
    rather than left to a second query because a report that shows a
    restated per-share amount has to be able to explain it, and the window
    it must explain is the one this function just used - handing that back is
    cheaper and less error-prone than asking a caller to reconstruct it.

    `yields` maps a ticker to its trailing annual dividend yield, and a key
    is present ONLY when that yield can be relied on - a confirmed non-payer
    is present with exactly `0.0`, while a ticker whose data is missing is
    absent and named in `unavailable` with a sentence saying why. That
    absence is the contract every consumer is built on: a missing yield is
    not a zero yield, and collapsing the two is the one silent wrong answer
    this feature could produce. `dividends_per_share` carries the trailing
    cash per share behind each available yield, which is what turns a share
    count into the money a holder actually receives.

    Read-only throughout, and a database with no `dividends` table - one
    built before this feature existed - reports every ticker as unavailable
    rather than raising, so an older cache degrades into a named gap instead
    of a crash or a confident zero.
    """
    if not tickers:
        return {}, {}, {}, {}

    window_start = pd.Timestamp(as_of) - pd.DateOffset(months=lookback_months)
    known = tickers_with_dividend_data(tickers, db_path)
    long_df = load_dividends_long(tickers, window_start, as_of, db_path)
    per_share = trailing_dividends_per_share(long_df, tickers, as_of, lookback_months)
    # The same market price `allocate_shares` and the holdings valuation
    # use, from the one shared loader - so a dividend yield's denominator
    # and the price a share is bought or valued at are provably one number.
    prices = load_latest_close(tickers, as_of, db_path)
    yields, unavailable = trailing_dividend_yields(per_share, prices, known)
    available_per_share = {t: per_share[t] for t in yields}

    splits_long = load_splits_long(tickers, db_path, after=window_start.date())
    splits_by_ticker: dict[str, list[tuple[date, float]]] = {}
    for _, row in splits_long.iterrows():
        ex_date = pd.Timestamp(row["ex_date"]).date()
        if ex_date > as_of:
            continue
        splits_by_ticker.setdefault(str(row["ticker"]), []).append((ex_date, float(row["ratio"])))

    in_window: dict[str, SplitContext] = {}
    for ticker, events in splits_by_ticker.items():
        rows = long_df[long_df["ticker"] == ticker] if not long_df.empty else long_df
        payments = [
            (pd.Timestamp(r["ex_date"]).date(), float(r["amount"]))
            for _, r in rows.iterrows()
        ] if not rows.empty else []
        in_window[ticker] = SplitContext(
            splits=sorted(events), payments=sorted(payments)
        )

    return yields, available_per_share, unavailable, in_window
