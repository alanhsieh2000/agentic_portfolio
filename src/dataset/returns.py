"""Trailing-one-month realized return per ticker per month, for the shared
`returns` table used by plan 5's optimizer and plan 6's backtest scorer.

Unlike momentum.py (52 rebalance dates, only tickers that were actually
S&P 500 members that date) and fundamentals.py (same), this module spans
the WIDER 112-month sequence (2015-01-01 through 2024-04-30) for the FULL
ticker universe prices.py attempted to fetch (580 tickers, per
load_ticker_universe) — not just the 52 narrower rebalance dates, and not
only the tickers that were members on a given date. This wider coverage
exists so plan 5's 60-month trailing lookback has real observations even
for the earliest 2020-2021 backtest rebalances, whose lookback windows
reach back before the 2020-2024 backtest window itself starts (see
plans/01_dataset.md's Context and Orientation section). No new network
fetch is needed — this is computed entirely from the existing `prices`
table.

For month-end anchor date d and ticker t: find the adj_close nearest to
(and not after) d, and the adj_close nearest to (and not after) one
calendar month before d; monthly_return = (price_at_d / price_at_one_month_before) - 1.
This is the same calculation shape as one leg of momentum.py's mom12m,
anchored differently (ending AT d, not one month before d) — kept in a
separate module/function so a change to one calculation can never
accidentally alter the other.

The very first month of the 112-month sequence, 2015-01-01, always gets a
null monthly_return for every ticker by construction — there is no priced
month before it within this project's 2015-01-02-onward fetch window.
Expected and harmless, not a bug (see plans/01_dataset.md's Plan of Work
section, which documents this exact null and confirms it is never actually
needed by any of plan 5's lookback windows).
"""

from __future__ import annotations

import logging

import duckdb
import pandas as pd

from src.dataset.fundamentals import DEFAULT_DB_PATH, attach_nearest_price
from src.dataset.membership import compute_rebalance_dates
from src.dataset.prices import DEFAULT_END, DEFAULT_START, load_ticker_universe

logger = logging.getLogger(__name__)


def compute_monthly_return(price_at_d: float | None, price_one_month_before: float | None) -> float | None:
    """(price_at_d / price_one_month_before) - 1. Returns None (not NaN,
    not an exception) if either price is missing/NaN or
    price_one_month_before is non-positive — the expected, common case for
    the earliest months in the wide sequence, matching the null-preserving
    philosophy every other factor in this codebase follows.
    """
    if (
        price_at_d is None
        or pd.isna(price_at_d)
        or price_one_month_before is None
        or pd.isna(price_one_month_before)
        or price_one_month_before <= 0
    ):
        return None
    return (price_at_d / price_one_month_before) - 1.0


def load_prices_for_join(db_path: str = DEFAULT_DB_PATH) -> pd.DataFrame:
    """columns ['date', 'ticker', 'adj_close'] — identical shape to
    fundamentals.py's/momentum.py's own load_prices_for_join (duplicated
    here rather than imported, for the same reason momentum.py already
    duplicates it: both are tightly-scoped, three-column reads of the same
    `prices` table, not worth a shared module for this alone).
    """
    con = duckdb.connect(db_path)
    try:
        df = con.execute("SELECT date, ticker, adj_close FROM prices").fetchdf()
    finally:
        con.close()
    df["date"] = pd.to_datetime(df["date"])
    return df


def build_month_ticker_grid(tickers: list[str], months: list[pd.Timestamp]) -> pd.DataFrame:
    """Cross product of every ticker in `tickers` with every month in
    `months` — one row per (month, ticker) pair, columns
    ['rebalance_date', 'ticker']. Named 'rebalance_date' (matching
    attach_nearest_price's expected column name) even though, per this
    plan's own documented caveat, this column holds one of the 112
    wider-sequence months here, not necessarily one of the 52 narrower
    rebalance dates.
    """
    return pd.DataFrame(
        [(month, ticker) for month in months for ticker in tickers],
        columns=["rebalance_date", "ticker"],
    )


def compute_monthly_return_column(grid: pd.DataFrame, prices: pd.DataFrame) -> pd.Series:
    """Row-wise monthly_return for every (month, ticker) in `grid`.

    Reuses attach_nearest_price twice (once at the month itself, once one
    calendar month before) against two independently-shifted copies of
    `grid` — safe to combine positionally after an explicit reindex to
    `grid.index`, since attach_nearest_price preserves the original index
    of whichever frame it's given (see its docstring in fundamentals.py),
    but each call's own internal sort order isn't guaranteed to match the
    other's.
    """
    one_month_before = grid.assign(rebalance_date=grid["rebalance_date"] - pd.DateOffset(months=1))

    price_at_d = attach_nearest_price(grid, prices)["adj_close"].reindex(grid.index)
    price_before = attach_nearest_price(one_month_before, prices)["adj_close"].reindex(grid.index)

    return pd.Series(
        [compute_monthly_return(pd_, pb) for pd_, pb in zip(price_at_d, price_before)],
        index=grid.index,
    )


def write_returns_table(df: pd.DataFrame, db_path: str = DEFAULT_DB_PATH) -> None:
    """Write `df` to the `returns` table in the DuckDB file at `db_path`.
    Drops any pre-existing table first, so re-running this is always safe.
    """
    con = duckdb.connect(db_path)
    try:
        con.register("returns_df", df)
        con.execute("DROP TABLE IF EXISTS returns")
        con.execute(
            "CREATE TABLE returns AS "
            "SELECT rebalance_date::DATE AS rebalance_date, "
            "ticker::VARCHAR AS ticker, "
            "monthly_return::DOUBLE AS monthly_return "
            "FROM returns_df"
        )
        con.unregister("returns_df")
    finally:
        con.close()


def build_returns(db_path: str = DEFAULT_DB_PATH, start: str = DEFAULT_START, end: str = DEFAULT_END) -> pd.DataFrame:
    """Compute monthly_return for every (month, ticker) pair across the
    wider 112-month sequence and the full ticker universe, write to
    DuckDB, and return the resulting DataFrame.
    """
    tickers = load_ticker_universe(db_path)
    months = compute_rebalance_dates(start, end)
    prices = load_prices_for_join(db_path)

    grid = build_month_ticker_grid(tickers, months)
    grid["monthly_return"] = compute_monthly_return_column(grid, prices)

    returns = grid[["rebalance_date", "ticker", "monthly_return"]]
    write_returns_table(returns, db_path)
    logger.info(
        "wrote %d rows to %s::returns (%d non-null) across %d months and %d tickers",
        len(returns),
        db_path,
        returns["monthly_return"].notna().sum(),
        len(months),
        len(tickers),
    )
    return returns


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    build_returns()
