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
from typing import NamedTuple

import duckdb
import numpy as np
import pandas as pd
from pypfopt import DiscreteAllocation, EfficientFrontier, expected_returns, risk_models
from pypfopt.base_optimizer import portfolio_performance

from src.config.settings import settings
from src.dataset.fundamentals import attach_nearest_price

logger = logging.getLogger(__name__)

VALID_OBJECTIVES = ("GMV", "MV", "MSR")
MV_RETURN_TOLERANCE = 1e-4
DEFAULT_TARGET_ANNUAL_RETURN = 0.12


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


RETURNS_LONG_COLUMNS = ["rebalance_date", "ticker", "monthly_return"]
"""The column shape every `load_returns_long` result has, including an empty
one - `pivot_returns_matrix` and every other consumer index into these names,
so an empty frame must still carry them rather than being column-less."""


def load_returns_long(
    tickers: list[str],
    window_start: pd.Timestamp | date | None,
    window_end: pd.Timestamp | date | None,
    db_path: str,
    read_only: bool = False,
) -> pd.DataFrame:
    """Long-format rows (['rebalance_date', 'ticker', 'monthly_return']) from
    the `returns` table for `tickers`, restricted to the closed date range
    [`window_start`, `window_end`] - equivalent to restricting to an explicit
    list of that range's months because the `returns` table is a full cross
    product of every ticker with every month in its own date sequence (see
    src/dataset/returns.py's build_month_ticker_grid), so every calendar
    month between the window's endpoints is guaranteed present with no
    gaps in the date sequence itself.

    `window_end=None` means "no window at all" and yields the correctly-shaped
    empty frame - what `load_returns_matrix` needs when the `returns` table
    holds no month on or before its `as_of`. `window_start=None` means "no
    lower bound", the whole available history up to `window_end`, which is
    what `src/optimizer/benchmark.py` reads once per session so that every
    window it reports afterwards is a pure in-memory slice.

    `read_only=True` opens `db_path` read-only and reports a missing file or a
    missing `returns` table as an empty frame instead of raising, so asking a
    question about a database neither creates one nor takes a write lock on
    it - the same discipline `src/dataset/ticker_currency.py`'s
    `load_ticker_currencies` already follows, and what lets a benchmark read
    the shared `data/portfolio.duckdb` cache with no risk of mutating it.
    """
    if window_end is None or not tickers:
        return pd.DataFrame(columns=RETURNS_LONG_COLUMNS)

    placeholders = ", ".join(["?"] * len(tickers))
    clauses = [f"ticker IN ({placeholders})", "rebalance_date <= ?"]
    params: list[object] = [*tickers, pd.Timestamp(window_end).date()]
    if window_start is not None:
        clauses.append("rebalance_date >= ?")
        params.append(pd.Timestamp(window_start).date())

    try:
        con = duckdb.connect(db_path, read_only=True) if read_only else duckdb.connect(db_path)
    except duckdb.IOException:
        return pd.DataFrame(columns=RETURNS_LONG_COLUMNS)
    try:
        df = con.execute(
            "SELECT rebalance_date, ticker, monthly_return FROM returns "
            f"WHERE {' AND '.join(clauses)}",
            params,
        ).fetchdf()
    except duckdb.CatalogException:
        return pd.DataFrame(columns=RETURNS_LONG_COLUMNS)
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


def load_returns_matrix_unfiltered(
    tickers: list[str],
    as_of: date,
    lookback_months: int = 60,
    db_path: str = settings.db_path,
) -> pd.DataFrame:
    """`load_returns_matrix` WITHOUT the minimum-history drop rule: the
    trailing `lookback_months`-month matrix exactly as the `returns` table
    holds it, with one column per requested ticker whether or not that
    ticker has enough usable history.

    Split out because a reporting caller needs the pre-rule matrix to say
    WHY a ticker was dropped. `apply_min_history_rule` removes a column
    without leaving any record of how many months it actually had, and a
    report that tells a user "NEWCO was excluded" without saying "it has 8
    months, and 24 are needed" is not actionable - see
    `src/optimizer/holdings.py`, which subtracts this matrix's columns from
    the filtered one's to name each exclusion and its month count.
    """
    window_dates = _load_window_dates(as_of, lookback_months, db_path)
    long_df = load_returns_long(
        tickers,
        window_dates[0] if window_dates else None,
        window_dates[-1] if window_dates else None,
        db_path,
    )
    return pivot_returns_matrix(long_df, tickers, window_dates)


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
    wide = load_returns_matrix_unfiltered(tickers, as_of, lookback_months, db_path)
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


def _estimate_mu_and_cov(returns_matrix: pd.DataFrame) -> tuple[pd.Series, pd.DataFrame]:
    """Annualized expected returns and an annualized shrunk covariance
    matrix for `returns_matrix` (monthly returns, as produced by
    `load_returns_matrix`).

    Extracted so that there is exactly ONE place in this project where these
    two estimates are made, and a second consumer cannot drift from the
    first. `_fit_efficient_frontier` uses it to solve an objective;
    `stats_for_weights` uses it to measure a weight vector somebody else
    chose (a user's actual holdings, see `src/optimizer/holdings.py`). Those
    two answer different questions and must not answer them with different
    estimators, or the figures they print could not be compared.

    `frequency=12` is PyPortfolioOpt's annualization multiplier for monthly
    data, so both come out annual rather than monthly.
    `mean_historical_return` compounds by default, making `mu` a geometric
    annual growth rate. See `_covariance_input` for why the covariance is
    estimated on the complete-overlap sub-window rather than the whole
    matrix.
    """
    mu = expected_returns.mean_historical_return(returns_matrix, returns_data=True, frequency=12)
    cov_matrix = risk_models.CovarianceShrinkage(
        _covariance_input(returns_matrix), returns_data=True, frequency=12
    ).ledoit_wolf()
    return mu, cov_matrix


def _validate_efficient_return_result(weights: dict[str, float], ef: EfficientFrontier, target_annual_return: float) -> None:
    """Positively verify an `efficient_return()` result rather than assuming
    silence means success. Per this plan's Decision Log, PyPortfolioOpt's
    own documentation warns that a technically-feasible but numerically
    "unreasonable" target return makes `efficient_return()` fail silently
    and return weird weights, with no exception raised - a real risk with
    this project's annual 12% target against a thin monthly sample
    annualized up to estimate it.

    The realized-return check is deliberately one-sided (>= target, not
    "close to" target): `efficient_return()`'s constraint is an inequality
    (`return >= target_return`), so whenever the unconstrained
    minimum-variance point's own return already clears the target, that
    constraint is non-binding and the correctly-returned weights are the
    GMV weights themselves, with a realized return legitimately *above*
    the target rather than equal to it. Only a realized return meaningfully
    *below* target indicates an actual problem.

    Raises `ValueError` describing whichever check failed.
    """
    if any(pd.isna(w) for w in weights.values()):
        raise ValueError(f"efficient_return produced NaN weight(s), likely a silent solver failure: {weights}")

    total = sum(weights.values())
    if abs(total - 1.0) > 1e-3:
        raise ValueError(f"efficient_return produced weights summing to {total!r}, not ~1.0: {weights}")

    realized_return, _, _ = ef.portfolio_performance()
    if realized_return < target_annual_return - MV_RETURN_TOLERANCE:
        raise ValueError(
            f"efficient_return's realized annual return {realized_return!r} is below "
            f"target_annual_return={target_annual_return!r} (tolerance {MV_RETURN_TOLERANCE}); "
            "PyPortfolioOpt may have failed silently on an unreasonable target."
        )


def _fit_efficient_frontier(
    returns_matrix: pd.DataFrame,
    objective: str,
    target_annual_return: float,
    risk_free_rate: float,
) -> tuple[EfficientFrontier, pd.Series, pd.DataFrame]:
    """Estimate `mu`/`cov_matrix` from `returns_matrix` and solve `objective`,
    returning the fitted `EfficientFrontier` alongside both estimates.

    Shared by `compute_weights` and `compute_weights_and_stats` so the
    estimation and the GMV/MV/MSR dispatch exist in exactly one place, even
    though those two callers deliberately pass different `risk_free_rate`
    values (see each one's docstring). Only the MSR branch consumes
    `risk_free_rate`: `min_volatility` ignores expected returns entirely and
    `efficient_return` is defined purely by its target, so neither takes such
    a parameter.

    `mu` and `cov_matrix` come from `_estimate_mu_and_cov`, which annualizes
    a monthly `returns_matrix` (as produced by `load_returns_matrix`) with
    `frequency=12` so its figures are directly comparable to
    `target_annual_return`. This makes every quantity
    `EfficientFrontier` reports (`mu`, `cov_matrix`, and `efficient_return`'s
    realized return) annual, not monthly - the realized per-month portfolio
    return used elsewhere in this project (e.g. backtest scoring) is computed
    separately, directly from the shared `returns` table's monthly figures,
    and is unaffected by this annualization.
    """
    if objective not in VALID_OBJECTIVES:
        raise ValueError(f"objective must be one of {VALID_OBJECTIVES}, got {objective!r}")

    mu, cov_matrix = _estimate_mu_and_cov(returns_matrix)

    ef = EfficientFrontier(mu, cov_matrix)
    if objective == "GMV":
        ef.min_volatility()
    elif objective == "MSR":
        ef.max_sharpe(risk_free_rate=risk_free_rate)
    else:
        ef.efficient_return(target_return=float(target_annual_return))

    return ef, mu, cov_matrix


def compute_weights(
    returns_matrix: pd.DataFrame,
    objective: str,
    target_annual_return: float = DEFAULT_TARGET_ANNUAL_RETURN,
) -> dict[str, float]:
    """GMV/MV/MSR portfolio weights from `returns_matrix` (as produced by
    `load_returns_matrix`), per plan 5's Decision Log and Plan of Work, and
    per README.md's Backtest Mode Stage 2 spec (annualized expected_returns
    and cov_matrix, 12% annual MV target return) as resolved by
    `plans/08_consistency_review.md` findings 6 and 7.

    MSR is fitted at `risk_free_rate=0.0`, passed explicitly to state what
    was previously implicit: this is PyPortfolioOpt's own default for
    `max_sharpe`, so this function's numerical behavior is unchanged from
    before `plans/10_performance_reporting_and_target_return.md`. Its caller
    `src/flow/backtest.py` scores a whole 52-month run by computing its own
    realized Sharpe ratio from actual monthly net returns against
    `settings.risk_free_rate` (see that module's `compute_sharpe_ratio`), so
    changing the rate the weights themselves are fitted at would silently
    move every published backtest figure. `compute_weights_and_stats` is the
    entry point that does fit at this project's configured rate, for the
    interactive CLI path that also reports the resulting Sharpe ratio.
    """
    ef, _mu, _cov_matrix = _fit_efficient_frontier(
        returns_matrix, objective, target_annual_return, risk_free_rate=0.0
    )
    weights = dict(ef.clean_weights())

    if objective == "MV":
        _validate_efficient_return_result(weights, ef, target_annual_return)

    return weights


class PortfolioStats(NamedTuple):
    """One optimization's weights plus every figure needed to explain them.

    `expected_returns` and `volatility` are annualized per-ticker estimates
    covering *every* ticker in the returns matrix the optimizer considered,
    not only the ones that received weight - narrowing to held tickers is a
    display decision, made by `src/flow/cli.py`. `risk_free_rate` and
    `target_annual_return` are echoed back verbatim so a caller can report
    the inputs beside the outputs without tracking them separately;
    `target_annual_return` is populated regardless of objective but is
    meaningful only for MV, the one objective defined by it.

    `returns_window_start`, `returns_window_end`, and `returns_window_months`
    describe the actual trailing window of monthly returns read from the
    `returns` table for this optimization - derived from the returns
    matrix's own date index, so they reflect the real months used even when
    that is fewer than the configured `lookback_months` (a recent listing,
    or a `returns` table that doesn't yet reach that far back). They are
    independent of `apply_min_history_rule`'s per-ticker drops, which remove
    columns, never rows.
    """

    weights: dict[str, float]
    expected_returns: dict[str, float]
    volatility: dict[str, float]
    portfolio_expected_return: float
    portfolio_volatility: float
    portfolio_sharpe: float
    risk_free_rate: float
    target_annual_return: float
    returns_window_start: date
    returns_window_end: date
    returns_window_months: int


def compute_weights_and_stats(
    returns_matrix: pd.DataFrame,
    objective: str,
    target_annual_return: float = DEFAULT_TARGET_ANNUAL_RETURN,
    risk_free_rate: float = settings.risk_free_rate,
) -> PortfolioStats:
    """`compute_weights`'s result plus the estimates behind it, for a caller
    that reports why a portfolio looks the way it does rather than only what
    to buy - see `src/flow/cli.py`'s `print_weights_and_allocation`.

    Unlike `compute_weights`, MSR is fitted at `risk_free_rate` (defaulting
    to this project's configured `settings.risk_free_rate`) rather than at
    zero, so the Sharpe ratio reported here is maximized against the same
    rate it is measured against. Two consequences are worth knowing. A pool
    whose every ticker has an expected annual return at or below
    `risk_free_rate` makes MSR genuinely undefined, and PyPortfolioOpt
    raises `ValueError` saying so - a real possibility once the rate is
    nonzero, and left to propagate rather than masked, since silently
    returning some other portfolio would misreport what was optimized. And
    per-ticker volatility is the square root of `cov_matrix`'s diagonal;
    `cov_matrix` comes from `_covariance_input`'s complete-overlap window,
    so a partial-history ticker's volatility is estimated only from months
    every ticker shares.

    The three portfolio-level figures come from PyPortfolioOpt's
    `portfolio_performance`, which reads the raw solved weights rather than
    the rounded ones `clean_weights` returns in `weights`. The two differ
    only below `clean_weights`'s 1e-4 cutoff and 5-decimal rounding, so the
    reported figures describe the same portfolio to well beyond display
    precision - but they are not recomputed from the rounded weights, which
    is why a holding printed as 0.0000 can still be reflected in them.
    """
    ef, mu, cov_matrix = _fit_efficient_frontier(
        returns_matrix, objective, target_annual_return, risk_free_rate
    )
    weights = dict(ef.clean_weights())

    if objective == "MV":
        _validate_efficient_return_result(weights, ef, target_annual_return)

    expected_returns, volatility = _per_ticker_figures(mu, cov_matrix)
    portfolio_return, portfolio_volatility, sharpe = ef.portfolio_performance(risk_free_rate=risk_free_rate)

    window_index = returns_matrix.index

    return PortfolioStats(
        weights=weights,
        expected_returns=expected_returns,
        volatility=volatility,
        portfolio_expected_return=float(portfolio_return),
        portfolio_volatility=float(portfolio_volatility),
        portfolio_sharpe=float(sharpe),
        risk_free_rate=float(risk_free_rate),
        target_annual_return=float(target_annual_return),
        returns_window_start=window_index.min().date(),
        returns_window_end=window_index.max().date(),
        returns_window_months=len(window_index),
    )


def _per_ticker_figures(
    mu: pd.Series, cov_matrix: pd.DataFrame
) -> tuple[dict[str, float], dict[str, float]]:
    """Per-ticker annualized expected return and volatility as
    `({ticker: mu}, {ticker: volatility})`.

    Volatility is the square root of `cov_matrix`'s diagonal - deliberately
    NOT each column's own `std * sqrt(12)`. `cov_matrix` comes from
    `_estimate_mu_and_cov`, so it is Ledoit-Wolf shrunk and estimated on
    `_covariance_input`'s complete-overlap window; taking the diagonal keeps
    a ticker's reported volatility consistent with the covariance the
    portfolio-level figure was computed from, rather than printing a number
    that cannot be reconciled with the one below it.

    Shared by `compute_weights_and_stats` and `stats_for_weights` so that an
    optimized pool and a held portfolio report per-ticker figures the same
    way - the same reason `_estimate_mu_and_cov` itself is shared.
    """
    volatility = pd.Series(np.sqrt(np.diag(cov_matrix.to_numpy())), index=cov_matrix.columns)
    return (
        {ticker: float(value) for ticker, value in mu.items()},
        {ticker: float(value) for ticker, value in volatility.items()},
    )


class WeightedStats(NamedTuple):
    """`stats_for_weights`'s result: the portfolio-level triplet for a given
    weight vector, plus the per-ticker estimates behind it.

    Field names deliberately match `PortfolioStats`' own, so a caller
    rendering an optimized pool and a caller rendering a held portfolio read
    the same names off the same-shaped record. `expected_returns` and
    `volatility` cover every ticker in the returns matrix, not only the
    weighted ones - narrowing to what actually carries weight is a display
    decision, the same division of labour `PortfolioStats` documents.
    """

    expected_returns: dict[str, float]
    volatility: dict[str, float]
    portfolio_expected_return: float
    portfolio_volatility: float
    portfolio_sharpe: float


def stats_for_weights(
    returns_matrix: pd.DataFrame,
    weights: dict[str, float],
    risk_free_rate: float = settings.risk_free_rate,
) -> WeightedStats:
    """The annualized return, annualized volatility and Sharpe ratio of a
    weight vector SOMEBODY ELSE chose, together with the per-ticker
    estimates behind them.

    The counterpart to `compute_weights_and_stats`, which picks the weights
    itself: here the weights are a given - a user's actual share holdings
    turned into value shares by `src/optimizer/holdings.py` - and only the
    measurement is this function's job. Both route through
    `_estimate_mu_and_cov`, `_per_ticker_figures`, and PyPortfolioOpt's own
    definition of the three portfolio-level figures
    (`portfolio_performance`, i.e. `w'mu`, `sqrt(w'Sigma w)` and
    `(w'mu - rf) / sqrt(w'Sigma w)`), so a held portfolio's figures are
    directly comparable with an optimized pool's and with a benchmark's
    rather than merely resembling them - per-ticker figures included, which
    is what lets one report print an "Expected return / volatility
    (annualized)" section for either kind of portfolio.

    Because `returns_matrix` is a genuine cross-section, its covariance is
    Ledoit-Wolf shrunk exactly as an optimized pool's is - see
    `src/optimizer/benchmark.py`'s module docstring for why a single-column
    benchmark is deliberately NOT shrunk, and why that asymmetry is correct
    rather than an inconsistency to fix.

    Raises `ValueError` if `weights` names a ticker that is not a column of
    `returns_matrix`. PyPortfolioOpt would silently drop such a weight,
    leaving the rest summing to less than one and misreporting all three
    figures as though the missing holding did not exist; the caller must
    decide what to do about a holding it has no history for (see
    `holdings_stats`, which excludes it by name) rather than have it
    disappear here.
    """
    unknown = sorted(set(weights) - set(returns_matrix.columns))
    if unknown:
        raise ValueError(
            f"weights name ticker(s) with no column in the returns matrix: {', '.join(unknown)}"
        )

    mu, cov_matrix = _estimate_mu_and_cov(returns_matrix)
    aligned = {ticker: float(weights.get(ticker, 0.0)) for ticker in returns_matrix.columns}
    annual_return, annual_volatility, sharpe = portfolio_performance(
        aligned, mu, cov_matrix, verbose=False, risk_free_rate=float(risk_free_rate)
    )
    expected_returns, volatility = _per_ticker_figures(mu, cov_matrix)

    return WeightedStats(
        expected_returns=expected_returns,
        volatility=volatility,
        portfolio_expected_return=float(annual_return),
        portfolio_volatility=float(annual_volatility),
        portfolio_sharpe=float(sharpe),
    )


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


def latest_price_date(tickers: list[str], as_of: date, db_path: str) -> date | None:
    """The date of the most recent price row `db_path` holds for `tickers` at
    or before `as_of`, or `None` when it holds none.

    Exists so a report can state how current the prices behind its money
    figures actually are. `load_latest_prices` returns the prices but not
    their date, which was fine while every holdings report fetched fresh
    data moments before printing. Once those prices can come from a
    persistent cache (see `src/dataset/holdings_cache.py`) the date stops
    being obvious and starts being the difference between a `Total value`
    the reader can trust and one they cannot.

    Read-only, and a missing file or table reports `None` rather than
    raising - the same discipline `load_returns_long(read_only=True)`
    follows, and what keeps asking about a cache from creating one.
    """
    if not tickers:
        return None

    placeholders = ", ".join(["?"] * len(tickers))
    try:
        con = duckdb.connect(db_path, read_only=True)
    except duckdb.IOException:
        return None
    try:
        row = con.execute(
            f"SELECT max(date) FROM prices WHERE ticker IN ({placeholders}) AND date <= ?",
            [*tickers, pd.Timestamp(as_of).date()],
        ).fetchone()
    except duckdb.CatalogException:
        return None
    finally:
        con.close()

    return row[0] if row and row[0] is not None else None


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


def allocate_shares(weights: dict[str, float], latest_prices: pd.Series, total_value: float) -> tuple[dict[str, int], float]:
    """Convert continuous `weights` (as produced by `compute_weights`) into
    a whole-share allocation spending as close to `total_value` as
    possible, via PyPortfolioOpt's `DiscreteAllocation.greedy_portfolio()`
    - its default, share-by-share greedy algorithm, preferred over its
    linear-programming alternative for typical portfolio sizes per this
    plan's Plan of Work. Returns `(shares_per_ticker, leftover_cash)`
    exactly as PyPortfolioOpt produces it.

    `latest_prices` should come from `load_latest_prices` called with the
    same `as_of` date used for the `load_returns_matrix` call that fed
    `weights`, so weights and allocation prices are anchored to the same
    date. `DiscreteAllocation` itself raises `TypeError`/`ValueError` for
    NaN weights or prices (e.g. a ticker `load_latest_prices` couldn't find
    a price for) - not caught here, per this plan's exception-propagation
    philosophy elsewhere in `compute_weights`.
    """
    allocation = DiscreteAllocation(weights, latest_prices, total_portfolio_value=total_value)
    return allocation.greedy_portfolio()
