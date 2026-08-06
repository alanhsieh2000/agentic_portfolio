"""GMV/MV/MSR portfolio optimization and discrete share allocation, per
plans/05_optimizer_and_allocation.md.

This module reads plan 1's shared `returns` table (monthly returns,
2015-01-01 through 2024-04-30, full ticker universe) to build the trailing
returns matrix that feeds PyPortfolioOpt's expected-return and
covariance-matrix estimation - never `prices` directly for that purpose,
per plan 5's Decision Log (one shared returns table, not an independently
recomputed one). `prices` is used only for allocation-time share pricing
(`load_latest_prices`), the one place in this module that still touches
raw daily prices rather than monthly returns.
"""

from __future__ import annotations

import logging
from datetime import date

import duckdb
import numpy as np
import pandas as pd
from pypfopt import EfficientFrontier, expected_returns, risk_models

from src.config.settings import settings
from src.dataset.fundamentals import attach_nearest_price

logger = logging.getLogger(__name__)

VALID_OBJECTIVES = ("GMV", "MV", "MSR")
MV_RETURN_TOLERANCE = 1e-4


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


def _covariance_input(returns_matrix: pd.DataFrame) -> pd.DataFrame:
    """The sub-window of `returns_matrix` where every column has real data.

    `risk_models.CovarianceShrinkage.ledoit_wolf()` calls `np.nan_to_num` on
    its input for every shrinkage target, silently treating a missing month
    as a zero return rather than a gap - fine for `mean_historical_return`
    (which uses each column's own non-null count) but a real fabrication
    risk here, since `load_returns_matrix` deliberately keeps a
    recent-IPO/pre-delisting ticker with leading/trailing nulls. Dropping to
    the complete-overlap window instead means every covariance term is
    computed only from genuinely paired observations. Logged when this
    actually shrinks the window; a no-op when every column has full history.
    """
    complete = returns_matrix.dropna()
    if len(complete) < len(returns_matrix):
        partial_tickers = sorted(returns_matrix.columns[returns_matrix.isna().any()])
        logger.info(
            "covariance window shrunk from %d to %d month(s) due to partial-history ticker(s) %s",
            len(returns_matrix),
            len(complete),
            partial_tickers,
        )
    return complete


def _validate_efficient_return_result(weights: dict[str, float], ef: EfficientFrontier, target_monthly_return: float) -> None:
    """Positively verify an `efficient_return()` result rather than assuming
    silence means success. Per this plan's Decision Log, PyPortfolioOpt's
    own documentation warns that a technically-feasible but numerically
    "unreasonable" target return makes `efficient_return()` fail silently
    and return weird weights, with no exception raised - a real risk with
    this project's monthly 1% target against a thin monthly sample.

    The realized-return check is deliberately one-sided (>= target, not
    "close to" target): `efficient_return()`'s constraint is an inequality
    (`return >= target_return`), so whenever the unconstrained
    minimum-variance point's own return already clears the target, that
    constraint is non-binding and the correctly-returned weights are the
    GMV weights themselves, with a realized return legitimately *above*
    the target rather than equal to it (verified empirically against real
    data: an 8-large-cap candidate set's GMV point alone already returns
    ~1.2%/month, comfortably above a 1%/month target). Only a realized
    return meaningfully *below* target indicates an actual problem.

    Raises `ValueError` describing whichever check failed.
    """
    if any(pd.isna(w) for w in weights.values()):
        raise ValueError(f"efficient_return produced NaN weight(s), likely a silent solver failure: {weights}")

    total = sum(weights.values())
    if abs(total - 1.0) > 1e-3:
        raise ValueError(f"efficient_return produced weights summing to {total!r}, not ~1.0: {weights}")

    realized_return, _, _ = ef.portfolio_performance()
    if realized_return < target_monthly_return - MV_RETURN_TOLERANCE:
        raise ValueError(
            f"efficient_return's realized monthly return {realized_return!r} is below "
            f"target_monthly_return={target_monthly_return!r} (tolerance {MV_RETURN_TOLERANCE}); "
            "PyPortfolioOpt may have failed silently on an unreasonable target."
        )


def compute_weights(
    returns_matrix: pd.DataFrame,
    objective: str,
    target_monthly_return: float = 0.01,
) -> dict[str, float]:
    """GMV/MV/MSR portfolio weights from `returns_matrix` (as produced by
    `load_returns_matrix`), per plan 5's Decision Log and Plan of Work.

    `mu` and `cov_matrix` are estimated with `frequency=1` (not `12`) so
    they stay in true monthly units, directly comparable to
    `target_monthly_return` - PyPortfolioOpt's `frequency` parameter is an
    annualization multiplier, not a period-of-input flag; passing `12` to
    already-monthly data would silently annualize it instead (see this
    plan's Decision Log for the empirical verification of this).
    """
    if objective not in VALID_OBJECTIVES:
        raise ValueError(f"objective must be one of {VALID_OBJECTIVES}, got {objective!r}")

    mu = expected_returns.mean_historical_return(returns_matrix, returns_data=True, frequency=1)
    cov_matrix = risk_models.CovarianceShrinkage(
        _covariance_input(returns_matrix), returns_data=True, frequency=1
    ).ledoit_wolf()

    ef = EfficientFrontier(mu, cov_matrix)
    if objective == "GMV":
        ef.min_volatility()
    elif objective == "MSR":
        ef.max_sharpe()
    else:
        ef.efficient_return(target_return=float(target_monthly_return))

    weights = dict(ef.clean_weights())

    if objective == "MV":
        _validate_efficient_return_result(weights, ef, target_monthly_return)

    return weights


def _load_prices_up_to(tickers: list[str], as_of: date, db_path: str) -> pd.DataFrame:
    """Long-format rows (['date', 'ticker', 'adj_close']) from the `prices`
    table for `tickers`, restricted to `date <= as_of` - only the rows
    `attach_nearest_price` could possibly need, not the whole table.
    """
    if not tickers:
        return pd.DataFrame(columns=["date", "ticker", "adj_close"])

    placeholders = ", ".join(["?"] * len(tickers))
    con = duckdb.connect(db_path)
    try:
        df = con.execute(
            f"SELECT date, ticker, adj_close FROM prices WHERE ticker IN ({placeholders}) AND date <= ?",
            [*tickers, pd.Timestamp(as_of).date()],
        ).fetchdf()
    finally:
        con.close()
    df["date"] = pd.to_datetime(df["date"])
    df["ticker"] = df["ticker"].astype(str)
    return df


def load_latest_prices(tickers: list[str], as_of: date, db_path: str = settings.db_path) -> pd.Series:
    """Most recent `adj_close` on or before `as_of` for each of `tickers`,
    read from the `prices` table (not `returns`) - `allocate_shares` needs
    a real per-share dollar price, which a monthly return has no unit for.

    Reuses `attach_nearest_price` (src/dataset/fundamentals.py), the same
    nearest-on-or-before-per-ticker join `src/dataset/returns.py` uses,
    rather than reimplementing it, per that function's own docstring note.
    Yields NaN (not an error) for a ticker with no price row on or before
    `as_of`, matching `attach_nearest_price`'s own missing-data behavior;
    logged so a silent NaN doesn't surface only much later at the
    allocation step.
    """
    if not tickers:
        return pd.Series(dtype=float, name="adj_close", index=pd.Index([], name="ticker"))

    grid = pd.DataFrame(
        {
            "rebalance_date": pd.to_datetime([as_of] * len(tickers)).astype("datetime64[us]"),
            "ticker": pd.array(tickers, dtype=str),
        }
    )
    prices = _load_prices_up_to(tickers, as_of, db_path)
    merged = attach_nearest_price(grid, prices)
    result = merged.set_index("ticker")["adj_close"].reindex(tickers)
    result.index.name = "ticker"

    missing = result[result.isna()].index.tolist()
    if missing:
        logger.warning("no price on or before %s for ticker(s): %s", as_of, sorted(missing))

    return result
