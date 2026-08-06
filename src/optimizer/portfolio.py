"""GMV/MV/MSR portfolio optimization and discrete share allocation, per
plans/05_optimizer_and_allocation.md.

This module reads plan 1's shared `returns` table (monthly returns,
2015-01-01 through 2024-04-30, full ticker universe) to build the trailing
returns matrix that feeds PyPortfolioOpt's expected-return and
covariance-matrix estimation - never `prices` directly for that purpose,
per plan 5's Decision Log (one shared returns table, not an independently
recomputed one). `prices` is used only later, for allocation-time share
pricing (future work in this module).
"""

from __future__ import annotations

import logging
from datetime import date

import duckdb
import numpy as np
import pandas as pd

from src.config.settings import settings

logger = logging.getLogger(__name__)


def _load_window_dates(as_of: date, lookback_months: int, db_path: str) -> list[pd.Timestamp]:
    """The `lookback_months` distinct `rebalance_date` values in the
    `returns` table on or before `as_of`, ascending. Derived from the
    table's own distinct dates (not recomputed via membership.py's
    `compute_rebalance_dates`) so this always agrees with whatever months
    the `returns` table actually contains, regardless of that table's own
    date-window settings.
    """
    con = duckdb.connect(db_path)
    try:
        rows = con.execute(
            "SELECT DISTINCT rebalance_date FROM returns WHERE rebalance_date <= ? "
            "ORDER BY rebalance_date DESC LIMIT ?",
            [pd.Timestamp(as_of).date(), lookback_months],
        ).fetchall()
    finally:
        con.close()
    return sorted(pd.Timestamp(r[0]) for r in rows)


def _load_returns_long(tickers: list[str], window_dates: list[pd.Timestamp], db_path: str) -> pd.DataFrame:
    """Long-format rows (['rebalance_date', 'ticker', 'monthly_return']) from
    the `returns` table for `tickers`, restricted to the closed date range
    [window_dates[0], window_dates[-1]] - equivalent to restricting to
    exactly `window_dates` because the `returns` table is a full cross
    product of every ticker with every month in its own date sequence (see
    src/dataset/returns.py's build_month_ticker_grid), so every calendar
    month between the window's endpoints is guaranteed present with no
    gaps in the date sequence itself.
    """
    if not window_dates:
        return pd.DataFrame(columns=["rebalance_date", "ticker", "monthly_return"])

    placeholders = ", ".join(["?"] * len(tickers))
    con = duckdb.connect(db_path)
    try:
        df = con.execute(
            f"SELECT rebalance_date, ticker, monthly_return FROM returns "
            f"WHERE ticker IN ({placeholders}) AND rebalance_date BETWEEN ? AND ?",
            [*tickers, window_dates[0].date(), window_dates[-1].date()],
        ).fetchdf()
    finally:
        con.close()
    df["rebalance_date"] = pd.to_datetime(df["rebalance_date"])
    return df


def pivot_returns_matrix(
    long_df: pd.DataFrame, tickers: list[str], window_dates: list[pd.Timestamp]
) -> pd.DataFrame:
    """Pure pivot of `long_df` (columns ['rebalance_date', 'ticker',
    'monthly_return']) into a wide DataFrame indexed by `window_dates`
    (ascending) with one column per ticker in `tickers`, in that order.
    Reindexed against both axes explicitly, so a ticker with zero rows in
    `long_df` still appears as an all-null column rather than being
    silently absent - the min-history drop rule in
    `apply_min_history_rule` needs to see it to log it.
    """
    if long_df.empty:
        return pd.DataFrame(index=pd.DatetimeIndex(window_dates, name="rebalance_date"), columns=tickers, dtype=float)
    wide = long_df.pivot(index="rebalance_date", columns="ticker", values="monthly_return")
    wide.index.name = "rebalance_date"
    return wide.reindex(index=window_dates, columns=tickers)


def apply_min_history_rule(wide: pd.DataFrame, min_months: int) -> pd.DataFrame:
    """Drop any column (ticker) in `wide` with fewer than `min_months`
    non-null values, and any column with an internal gap - a null
    sandwiched between two non-null values, which would otherwise get
    silently skipped by PyPortfolioOpt's mean/covariance calculations
    rather than flagged as the missing-data problem it actually is. Leading
    nulls (a recent IPO, not yet listed for this window's earliest months)
    and trailing nulls (delisted before `as_of`) are not gaps - a kept
    ticker's own first-to-last non-null span must be fully populated, but
    it may be shorter than the full window. Every drop is logged with the
    ticker and the reason. Pure function, no I/O.
    """
    kept: list[str] = []
    for ticker in wide.columns:
        non_null = wide[ticker].notna()
        count = int(non_null.sum())
        if count < min_months:
            logger.info(
                "dropping %s from returns matrix: %d month(s) of history, below min_months=%d",
                ticker,
                count,
                min_months,
            )
            continue

        positions = np.flatnonzero(non_null.to_numpy())
        first_pos, last_pos = positions[0], positions[-1]
        if not non_null.iloc[first_pos : last_pos + 1].all():
            logger.info(
                "dropping %s from returns matrix: internal gap in monthly_return between %s and %s",
                ticker,
                wide.index[first_pos].date(),
                wide.index[last_pos].date(),
            )
            continue

        kept.append(ticker)

    return wide[kept]


def load_returns_matrix(
    tickers: list[str],
    as_of: date,
    lookback_months: int = 60,
    min_months: int = 24,
    db_path: str = settings.db_path,
) -> pd.DataFrame:
    """Trailing `lookback_months`-month returns matrix (months as rows,
    tickers as columns, `monthly_return` values) for `tickers`, ending on
    or before `as_of`, read from the shared `returns` table
    (src/dataset/returns.py) and filtered by the minimum-history drop rule
    in `apply_min_history_rule`. Feeds `compute_weights`'s expected-return
    and covariance-matrix estimation.
    """
    window_dates = _load_window_dates(as_of, lookback_months, db_path)
    long_df = _load_returns_long(tickers, window_dates, db_path)
    wide = pivot_returns_matrix(long_df, tickers, window_dates)
    return apply_min_history_rule(wide, min_months)
