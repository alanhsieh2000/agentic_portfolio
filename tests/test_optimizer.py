"""Tests for src/optimizer/portfolio.py.

Per AGENTS.md's testing guidance, the returns-matrix drop-logic is tested
against hand-built fixture DataFrames standing in for the `returns` table,
isolated from the actual DuckDB read (per
plans/05_optimizer_and_allocation.md's Validation and Acceptance section).
`load_latest_prices`'s DB-reading half is exercised against a tiny,
hand-built fixture DuckDB file (via `tmp_path`), not the real
`data/portfolio.duckdb`, so this test module stays hermetic and
deterministic regardless of whether the real dataset has been built.
"""

from datetime import date

import duckdb
import numpy as np
import pandas as pd
import pytest
from pypfopt import EfficientFrontier, expected_returns, risk_models

import cvxpy as cp
import inspect
from pypfopt.exceptions import OptimizationError

from src.config.settings import settings
from src.optimizer.dividends import (
    DividendFloor,
    DividendFloorError,
    DividendYieldUnavailableError,
)
from src.optimizer.portfolio import (
    MV_RETURN_TOLERANCE,
    _covariance_input,
    allocate_shares,
    apply_min_history_rule,
    compute_weights,
    compute_weights_and_stats,
    load_latest_prices,
    pivot_returns_matrix,
)


def _make_prices_db(db_path: str, rows: list[tuple[str, str, float, float]]) -> None:
    """rows: list of (date, ticker, close, adj_close) tuples."""
    con = duckdb.connect(db_path)
    try:
        con.execute("CREATE TABLE prices (date DATE, ticker VARCHAR, close DOUBLE, adj_close DOUBLE)")
        if rows:
            con.executemany("INSERT INTO prices VALUES (?, ?, ?, ?)", rows)
    finally:
        con.close()


def _dates(n: int) -> list[pd.Timestamp]:
    return list(pd.date_range("2019-04-01", periods=n, freq="MS"))


def _three_ticker_fixture() -> pd.DataFrame:
    """24 months, hand-built so the covariance structure is checkable by
    hand: STABLE has near-zero variance and near-zero covariance with the
    other two; RISKY_A and RISKY_B have substantial variance and, because
    they oscillate on different periods (2 months vs. 3 months over a
    24-month span - a common multiple, so the pattern is exact and
    deterministic), a small but genuinely non-zero covariance with each
    other rather than the (near-)perfect correlation an earlier draft of
    this fixture accidentally produced (verified empirically: a perfectly
    negatively-correlated RISKY_A/RISKY_B pair lets the optimizer cancel
    almost all variance with any 50/50 split of the two, making GMV's
    result degenerate/flat across many weight combinations rather than
    favoring STABLE - not what this fixture is meant to test).
    """
    idx = pd.date_range("2020-01-01", periods=24, freq="MS")
    stable = [0.005 + (0.0002 if i % 2 == 0 else -0.0002) for i in range(24)]
    risky_a = [0.01 + (0.05 if i % 2 == 0 else -0.05) for i in range(24)]
    risky_b = [0.01 + (0.04 if i % 3 == 0 else -0.02) for i in range(24)]
    return pd.DataFrame({"STABLE": stable, "RISKY_A": risky_a, "RISKY_B": risky_b}, index=idx)


def test_pivot_returns_matrix_reindexes_missing_ticker_to_all_null_column():
    window = _dates(3)
    long_df = pd.DataFrame(
        {
            "rebalance_date": [window[0], window[1], window[2]],
            "ticker": ["AAPL", "AAPL", "AAPL"],
            "monthly_return": [0.01, 0.02, 0.03],
        }
    )

    wide = pivot_returns_matrix(long_df, ["AAPL", "MISSING"], window)

    assert list(wide.columns) == ["AAPL", "MISSING"]
    assert wide["MISSING"].isna().all()
    assert wide["AAPL"].tolist() == [0.01, 0.02, 0.03]


def test_apply_min_history_rule_drops_ticker_below_min_months():
    window = _dates(60)
    wide = pd.DataFrame(index=pd.DatetimeIndex(window, name="rebalance_date"))
    wide["ENOUGH"] = [0.01] * 60
    wide["TOO_SHORT"] = [None] * 50 + [0.01] * 10  # 10 non-null, below min_months=24

    result = apply_min_history_rule(wide, min_months=24)

    assert list(result.columns) == ["ENOUGH"]


def test_apply_min_history_rule_keeps_ticker_with_recent_ipo_leading_nulls():
    window = _dates(60)
    wide = pd.DataFrame(index=pd.DatetimeIndex(window, name="rebalance_date"))
    wide["RECENT_IPO"] = [None] * 30 + [0.01] * 30  # 30 non-null, between 24 and 60

    result = apply_min_history_rule(wide, min_months=24)

    assert list(result.columns) == ["RECENT_IPO"]
    assert result["RECENT_IPO"].notna().sum() == 30


def test_apply_min_history_rule_drops_ticker_with_internal_gap():
    window = _dates(30)
    wide = pd.DataFrame(index=pd.DatetimeIndex(window, name="rebalance_date"))
    values = [0.01] * 30
    values[15] = None  # a single null sandwiched between non-null months
    wide["GAPPY"] = values

    result = apply_min_history_rule(wide, min_months=24)

    assert "GAPPY" not in result.columns


def test_apply_min_history_rule_keeps_ticker_delisted_before_as_of():
    window = _dates(30)
    wide = pd.DataFrame(index=pd.DatetimeIndex(window, name="rebalance_date"))
    wide["DELISTED"] = [0.01] * 25 + [None] * 5  # trailing nulls, not a gap

    result = apply_min_history_rule(wide, min_months=24)

    assert list(result.columns) == ["DELISTED"]
    assert result["DELISTED"].notna().sum() == 25


def test_covariance_input_unchanged_when_every_column_is_complete():
    df = _three_ticker_fixture()

    result = _covariance_input(df)

    pd.testing.assert_frame_equal(result, df)


def test_covariance_input_drops_incomplete_rows_for_partial_history_ticker():
    df = _three_ticker_fixture()
    partial = df.copy()
    partial.loc[partial.index[:10], "RISKY_B"] = None  # simulated recent IPO

    result = _covariance_input(partial)

    assert len(result) == len(df) - 10
    assert result.isna().sum().sum() == 0


def test_compute_weights_gmv_favors_near_zero_variance_ticker():
    df = _three_ticker_fixture()

    weights = compute_weights(df, "GMV")

    assert weights["STABLE"] > 0.8
    assert sum(weights.values()) == pytest.approx(1.0, abs=1e-3)


def test_compute_weights_invalid_objective_raises_value_error():
    df = _three_ticker_fixture()

    with pytest.raises(ValueError):
        compute_weights(df, "BOGUS")


def test_compute_weights_mv_below_gmv_return_matches_gmv_since_constraint_is_non_binding():
    """`efficient_return`'s constraint is `return >= target`, an inequality:
    when GMV's own unconstrained return already clears a low target, the
    constraint doesn't bind and MV legitimately returns the same weights
    as GMV - this is real, verified PyPortfolioOpt behavior (see
    plans/05_optimizer_and_allocation.md's Decision Log), not a bug.
    """
    df = _three_ticker_fixture()

    gmv_weights = compute_weights(df, "GMV")
    mv_weights = compute_weights(df, "MV", target_annual_return=0.03)

    assert mv_weights == pytest.approx(gmv_weights, abs=1e-6)


def test_compute_weights_mv_unreachable_target_raises_value_error():
    df = _three_ticker_fixture()

    with pytest.raises(ValueError):
        compute_weights(df, "MV", target_annual_return=1.0)


# ---------------------------------------------------------------------------
# compute_weights_and_stats
# ---------------------------------------------------------------------------


def _spy_on_max_sharpe(monkeypatch) -> dict:
    """Record the `risk_free_rate` PyPortfolioOpt's `max_sharpe` is actually
    called with, while still running the real optimization.
    """
    seen: dict[str, float] = {}
    real_max_sharpe = EfficientFrontier.max_sharpe

    def spy(self, risk_free_rate=0.0):
        seen["risk_free_rate"] = risk_free_rate
        return real_max_sharpe(self, risk_free_rate=risk_free_rate)

    monkeypatch.setattr("src.optimizer.portfolio.EfficientFrontier.max_sharpe", spy)
    return seen


def test_compute_weights_and_stats_msr_fits_at_the_configured_risk_free_rate(monkeypatch):
    """MSR maximizes a Sharpe ratio, which is only defined relative to a
    risk-free rate - so this project's configured rate must be the one
    PyPortfolioOpt optimizes against, not its library default of 0.0.
    """
    seen = _spy_on_max_sharpe(monkeypatch)

    stats = compute_weights_and_stats(_three_ticker_fixture(), "MSR")

    assert seen["risk_free_rate"] == settings.risk_free_rate
    assert stats.risk_free_rate == settings.risk_free_rate


def test_compute_weights_msr_still_fits_at_zero_risk_free_rate(monkeypatch):
    """The regression guard for src/flow/backtest.py: its 52-month run calls
    `compute_weights`, whose MSR results must not move just because the
    interactive path started fitting at a nonzero rate.
    """
    seen = _spy_on_max_sharpe(monkeypatch)

    compute_weights(_three_ticker_fixture(), "MSR")

    assert seen["risk_free_rate"] == 0.0


def test_compute_weights_and_stats_honors_an_explicit_risk_free_rate_override(monkeypatch):
    seen = _spy_on_max_sharpe(monkeypatch)

    stats = compute_weights_and_stats(_three_ticker_fixture(), "MSR", risk_free_rate=0.05)

    assert seen["risk_free_rate"] == 0.05
    assert stats.risk_free_rate == 0.05


def test_compute_weights_and_stats_sharpe_is_measured_against_the_risk_free_rate():
    """The reported Sharpe ratio must be the reported return and volatility
    combined with the reported rate - otherwise the four numbers printed
    together would not describe one another.
    """
    stats = compute_weights_and_stats(_three_ticker_fixture(), "GMV", risk_free_rate=0.05)

    expected = (stats.portfolio_expected_return - 0.05) / stats.portfolio_volatility
    assert stats.portfolio_sharpe == pytest.approx(expected)


def test_compute_weights_and_stats_per_ticker_figures_match_pypfopt_estimates():
    """Verify the reported per-ticker figures against their definition -
    annualized mean historical return, and the square root of the
    shrunk covariance matrix's diagonal - computed independently here.
    """
    df = _three_ticker_fixture()
    mu = expected_returns.mean_historical_return(df, returns_data=True, frequency=12)
    cov_matrix = risk_models.CovarianceShrinkage(
        _covariance_input(df), returns_data=True, frequency=12
    ).ledoit_wolf()
    expected_volatility = np.sqrt(np.diag(cov_matrix.to_numpy()))

    stats = compute_weights_and_stats(df, "GMV")

    assert stats.expected_returns == pytest.approx(mu.to_dict())
    assert stats.volatility == pytest.approx(dict(zip(cov_matrix.columns, expected_volatility)))
    # Every considered ticker is reported, including ones GMV gave no weight.
    assert set(stats.expected_returns) == {"STABLE", "RISKY_A", "RISKY_B"}


def test_compute_weights_and_stats_portfolio_figures_match_a_directly_fitted_frontier():
    df = _three_ticker_fixture()
    mu = expected_returns.mean_historical_return(df, returns_data=True, frequency=12)
    cov_matrix = risk_models.CovarianceShrinkage(
        _covariance_input(df), returns_data=True, frequency=12
    ).ledoit_wolf()
    ef = EfficientFrontier(mu, cov_matrix)
    ef.min_volatility()
    expected_return, expected_vol, expected_sharpe = ef.portfolio_performance(
        risk_free_rate=settings.risk_free_rate
    )

    stats = compute_weights_and_stats(df, "GMV")

    assert stats.portfolio_expected_return == pytest.approx(expected_return)
    assert stats.portfolio_volatility == pytest.approx(expected_vol)
    assert stats.portfolio_sharpe == pytest.approx(expected_sharpe)


def test_compute_weights_and_stats_gmv_weights_match_compute_weights():
    """GMV ignores expected returns and the risk-free rate entirely, so the
    two entry points must agree exactly - the check that extracting the
    shared `_fit_efficient_frontier` helper changed no arithmetic.
    """
    df = _three_ticker_fixture()

    assert compute_weights_and_stats(df, "GMV").weights == pytest.approx(compute_weights(df, "GMV"))


def test_compute_weights_and_stats_mv_reaches_its_target_return():
    df = _three_ticker_fixture()

    stats = compute_weights_and_stats(df, "MV", target_annual_return=0.10)

    assert stats.target_annual_return == 0.10
    assert stats.portfolio_expected_return == pytest.approx(0.10, abs=1e-4)


def test_compute_weights_and_stats_echoes_target_return_even_when_unused():
    """The field is always populated so a caller can report it beside the
    objective; it is meaningful only for MV.
    """
    stats = compute_weights_and_stats(_three_ticker_fixture(), "GMV", target_annual_return=0.07)

    assert stats.target_annual_return == 0.07


def test_compute_weights_and_stats_reports_the_actual_returns_window():
    """The window is read off the returns matrix's own index rather than the
    configured lookback, so it stays truthful when the available history is
    shorter than that - here 24 months, not 60.
    """
    stats = compute_weights_and_stats(_three_ticker_fixture(), "GMV")

    assert stats.returns_window_start == date(2020, 1, 1)
    assert stats.returns_window_end == date(2021, 12, 1)
    assert stats.returns_window_months == 24


def test_compute_weights_and_stats_returns_window_survives_a_min_history_drop():
    """`apply_min_history_rule` drops columns, never rows, so a dropped
    ticker must not shrink the reported window.
    """
    df = _three_ticker_fixture()
    df["NEWLY_LISTED"] = [np.nan] * 20 + [0.01, -0.01, 0.02, -0.02]

    stats = compute_weights_and_stats(apply_min_history_rule(df, min_months=24), "GMV")

    assert "NEWLY_LISTED" not in stats.expected_returns
    assert stats.returns_window_start == date(2020, 1, 1)
    assert stats.returns_window_months == 24


def test_compute_weights_and_stats_invalid_objective_raises_value_error():
    with pytest.raises(ValueError):
        compute_weights_and_stats(_three_ticker_fixture(), "BOGUS")


def test_load_latest_prices_returns_nearest_on_or_before(tmp_path):
    db_path = str(tmp_path / "prices.duckdb")
    _make_prices_db(
        db_path,
        [
            ("2024-01-01", "AAPL", 100.0, 100.0),
            ("2024-01-15", "AAPL", 110.0, 110.0),
            ("2024-02-01", "AAPL", 120.0, 120.0),
        ],
    )

    result = load_latest_prices(["AAPL"], date(2024, 1, 20), db_path=db_path)

    assert result["AAPL"] == pytest.approx(110.0)


def test_load_latest_prices_is_nan_for_ticker_with_no_price_on_or_before_as_of(tmp_path):
    db_path = str(tmp_path / "prices.duckdb")
    _make_prices_db(db_path, [("2024-02-01", "AAPL", 120.0, 120.0)])

    result = load_latest_prices(["AAPL"], date(2024, 1, 1), db_path=db_path)

    assert pd.isna(result["AAPL"])


def test_load_latest_prices_uses_adj_close_not_close(tmp_path):
    db_path = str(tmp_path / "prices.duckdb")
    _make_prices_db(db_path, [("2024-01-01", "AAPL", 100.0, 95.0)])  # split-adjusted, close != adj_close

    result = load_latest_prices(["AAPL"], date(2024, 1, 1), db_path=db_path)

    assert result["AAPL"] == pytest.approx(95.0)


def test_load_latest_prices_empty_tickers_returns_empty_series(tmp_path):
    db_path = str(tmp_path / "prices.duckdb")
    _make_prices_db(db_path, [])

    result = load_latest_prices([], date(2024, 1, 1), db_path=db_path)

    assert result.empty


def test_allocate_shares_matches_target_weights_within_one_share():
    """Per plans/05_optimizer_and_allocation.md's Validation and Acceptance
    section: one $100 stock, one $50 stock, 50/50 weights, $1000 total.
    """
    weights = {"A": 0.5, "B": 0.5}
    latest_prices = pd.Series({"A": 100.0, "B": 50.0})

    allocation, leftover_cash = allocate_shares(weights, latest_prices, 1000.0)

    implied_value = sum(shares * latest_prices[ticker] for ticker, shares in allocation.items())
    for ticker, weight in weights.items():
        target_value = weight * 1000.0
        actual_value = allocation.get(ticker, 0) * latest_prices[ticker]
        assert abs(actual_value - target_value) <= latest_prices[ticker]
    assert implied_value + leftover_cash == pytest.approx(1000.0)


def test_allocate_shares_exact_fit_leaves_zero_leftover_cash():
    allocation, leftover_cash = allocate_shares({"A": 0.5, "B": 0.5}, pd.Series({"A": 100.0, "B": 50.0}), 1000.0)

    assert allocation == {"A": 5, "B": 10}
    assert leftover_cash == pytest.approx(0.0)


def test_allocate_shares_raises_on_nan_price():
    with pytest.raises((TypeError, ValueError)):
        allocate_shares({"A": 1.0}, pd.Series({"A": float("nan")}), 1000.0)


# ==========================================================================
# The minimum expected-dividend constraint
# ==========================================================================


def _dividend_yields() -> dict[str, float]:
    """Trailing yields for `_three_ticker_fixture`'s three tickers, chosen so
    the constraint has something to trade against: RISKY_A is the best payer
    at 6%, so it sets the ceiling, while STABLE - the ticker GMV wants
    almost all of - pays only 1%. A floor above ~1.2% therefore has to pull
    weight out of STABLE, which is what makes these tests measure something.
    """
    return {"STABLE": 0.01, "RISKY_A": 0.06, "RISKY_B": 0.02}


def _dividends_per_share() -> dict[str, float]:
    return {"STABLE": 1.0, "RISKY_A": 6.0, "RISKY_B": 2.0}


def _floor(value: float, origin: str = "--min-dividend-yield") -> DividendFloor:
    return DividendFloor(yield_floor=value, origin=origin)


def test_compute_weights_signature_cannot_express_a_dividend_floor():
    """`compute_weights` feeds `src/flow/backtest.py`, whose published
    52-month figures must not move. Leaving its signature alone makes "the
    backtest cannot acquire a dividend floor" a property of the type system
    rather than a promise in a docstring.
    """
    assert list(inspect.signature(compute_weights).parameters) == [
        "returns_matrix",
        "objective",
        "target_annual_return",
    ]


@pytest.mark.parametrize("objective", ["GMV", "MV", "MSR"])
def test_compute_weights_and_stats_without_a_floor_reproduces_the_unconstrained_weights(objective):
    """The regression guard. Supplying yields must change what is REPORTED
    and nothing about what is SOLVED, so the report can be unconditional
    without making the optimizer conditional.
    """
    df = _three_ticker_fixture()
    baseline = compute_weights_and_stats(df, objective, target_annual_return=0.05)
    reported = compute_weights_and_stats(
        df,
        objective,
        target_annual_return=0.05,
        dividend_yields=_dividend_yields(),
        dividends_per_share=_dividends_per_share(),
    )
    assert reported.weights == baseline.weights
    assert reported.portfolio_expected_return == baseline.portfolio_expected_return
    assert reported.dividend_yield_floor is None


def test_supplying_yields_without_a_floor_adds_no_constraint(monkeypatch):
    """Counted rather than asserted-empty, because `EfficientFrontier` adds
    its own weight-bound constraints in `__init__` and `min_volatility` adds
    the fully-invested one. What matters is that supplying yields adds
    nothing BEYOND those.
    """
    counts: list[int] = []
    original = EfficientFrontier.add_constraint

    def spy(self, constraint):
        counts.append(1)
        return original(self, constraint)

    monkeypatch.setattr(EfficientFrontier, "add_constraint", spy)

    compute_weights_and_stats(_three_ticker_fixture(), "GMV")
    without_yields = len(counts)

    counts.clear()
    compute_weights_and_stats(
        _three_ticker_fixture(), "GMV", dividend_yields=_dividend_yields()
    )
    assert len(counts) == without_yields

    counts.clear()
    compute_weights_and_stats(
        _three_ticker_fixture(),
        "GMV",
        dividend_floor=_floor(0.04),
        dividend_yields=_dividend_yields(),
    )
    assert len(counts) == without_yields + 1


@pytest.mark.parametrize("objective", ["GMV", "MV", "MSR"])
def test_a_binding_dividend_floor_is_met_under_every_objective(objective):
    df = _three_ticker_fixture()
    stats = compute_weights_and_stats(
        df,
        objective,
        target_annual_return=0.05,
        dividend_floor=_floor(0.04),
        dividend_yields=_dividend_yields(),
        dividends_per_share=_dividends_per_share(),
    )
    assert stats.portfolio_dividend_yield >= 0.04 - 1e-6


def test_the_dividend_floor_survives_max_sharpes_variable_substitution():
    """The homogenization guard, and the reason
    `_fit_efficient_frontier` writes its constraint with the floor alone on
    one side.

    `max_sharpe` solves a transformed problem in which `w = y/k` and
    rebuilds every constraint by homogenizing it with `k`. Written as
    `vector @ w - floor >= 0` instead, the rebuild silently produces
    `vector @ w >= floor / k` - a different constraint, wrong under MSR
    only, with nothing raised. Measured on this fixture with a 0.04 floor,
    that form returns the UNCONSTRAINED MSR weights and a realized yield of
    roughly 0.0136. This test is what catches a rewrite of that line.
    """
    stats = compute_weights_and_stats(
        _three_ticker_fixture(),
        "MSR",
        dividend_floor=_floor(0.04),
        dividend_yields=_dividend_yields(),
    )
    assert stats.portfolio_dividend_yield == pytest.approx(0.04, abs=1e-6)

    unconstrained = compute_weights_and_stats(
        _three_ticker_fixture(), "MSR", dividend_yields=_dividend_yields()
    )
    assert unconstrained.portfolio_dividend_yield < 0.04
    assert stats.weights != unconstrained.weights


def test_the_dividend_constraint_puts_the_floor_alone_on_one_side(monkeypatch):
    """Pins the constraint's FORM structurally, not just its consequence.

    Both the correct and the broken spelling build a cvxpy `Inequality`
    whose `args[0]` is a `Constant`, so merely checking the type proves
    nothing - the difference is that constant's VALUE, which is the floor in
    the correct form and `0` in the broken one.
    """
    captured: list = []
    original = EfficientFrontier.add_constraint

    def spy(self, constraint):
        result = original(self, constraint)
        captured.append(self._constraints[-1])
        return result

    monkeypatch.setattr(EfficientFrontier, "add_constraint", spy)
    compute_weights_and_stats(
        _three_ticker_fixture(),
        "GMV",
        dividend_floor=_floor(0.04),
        dividend_yields=_dividend_yields(),
    )

    # `EfficientFrontier.__init__` adds the two weight-bound constraints
    # first, whose own constants are vectors; the dividend constraint is the
    # one whose left-hand constant is the scalar floor.
    scalars = [
        c
        for c in captured
        if isinstance(c.args[0], cp.expressions.constants.constant.Constant)
        and c.args[0].shape == ()
    ]
    assert len(scalars) == 1, [str(c) for c in captured]
    assert float(scalars[0].args[0].value) == pytest.approx(0.04)


def test_a_non_binding_dividend_floor_leaves_the_weights_untouched():
    df = _three_ticker_fixture()
    baseline = compute_weights_and_stats(df, "GMV", dividend_yields=_dividend_yields())
    constrained = compute_weights_and_stats(
        df,
        "GMV",
        dividend_floor=_floor(0.001),
        dividend_yields=_dividend_yields(),
    )
    assert constrained.weights == baseline.weights
    assert constrained.portfolio_dividend_yield > 0.001


def test_a_floor_above_the_ceiling_is_refused_before_the_solver_runs(monkeypatch):
    called: list[str] = []
    monkeypatch.setattr(
        EfficientFrontier,
        "min_volatility",
        lambda self: called.append("solved"),
    )
    with pytest.raises(DividendFloorError) as excinfo:
        compute_weights_and_stats(
            _three_ticker_fixture(),
            "GMV",
            dividend_floor=_floor(0.07),
            dividend_yields=_dividend_yields(),
        )
    assert "RISKY_A at 0.0600" in str(excinfo.value)
    assert called == []


def test_a_solver_infeasibility_under_a_floor_is_wrapped_as_a_value_error(monkeypatch):
    """Belt and braces. `OptimizationError` subclasses plain `Exception`, so
    an unwrapped one would sail past `src/flow/cli.py`'s `except ValueError`
    and destroy live mode's only snapshot.
    """
    def boom(self):
        raise OptimizationError("Solver status: infeasible")

    monkeypatch.setattr(EfficientFrontier, "min_volatility", boom)
    with pytest.raises(DividendFloorError) as excinfo:
        compute_weights_and_stats(
            _three_ticker_fixture(),
            "GMV",
            dividend_floor=_floor(0.02),
            dividend_yields=_dividend_yields(),
        )
    assert isinstance(excinfo.value, ValueError)
    assert "ceiling check did not predict" in str(excinfo.value)


def test_a_solver_infeasibility_with_no_floor_keeps_its_own_type(monkeypatch):
    def boom(self):
        raise OptimizationError("Solver status: infeasible")

    monkeypatch.setattr(EfficientFrontier, "min_volatility", boom)
    with pytest.raises(OptimizationError):
        compute_weights_and_stats(_three_ticker_fixture(), "GMV")


def test_mv_target_made_unreachable_by_a_floor_says_so_and_names_both_numbers():
    """A floor can only shrink the feasible set, so it can only lower the
    attainable return - and PyPortfolioOpt's own message for that names no
    dividend at all.
    """
    df = _three_ticker_fixture()
    # The target has to sit between the maximum return attainable UNDER the
    # floor and the unconstrained maximum, or the floor is not what blocks
    # it. Measured on this fixture: 0.1104 under a 0.0599 floor against
    # 0.1216 unconstrained, so 0.118 is blocked only by the floor.
    target = 0.118
    reachable_without_floor = compute_weights_and_stats(
        df, "MV", target_annual_return=target, dividend_yields=_dividend_yields()
    )
    assert reachable_without_floor.portfolio_expected_return >= target - MV_RETURN_TOLERANCE

    with pytest.raises(DividendFloorError) as excinfo:
        compute_weights_and_stats(
            df,
            "MV",
            target_annual_return=target,
            dividend_floor=_floor(0.0599),
            dividend_yields=_dividend_yields(),
        )
    message = str(excinfo.value)
    assert "0.1180" in message
    assert "dividend floor of 0.0599" in message
    assert "Lower --target-return" in message


def test_a_dividend_floor_requires_yields_to_enforce_it_against():
    with pytest.raises(ValueError, match="both or neither"):
        compute_weights_and_stats(
            _three_ticker_fixture(), "GMV", dividend_floor=_floor(0.02)
        )


def test_a_missing_yield_is_refused_under_a_floor_rather_than_read_as_zero():
    with pytest.raises(DividendYieldUnavailableError) as excinfo:
        compute_weights_and_stats(
            _three_ticker_fixture(),
            "GMV",
            dividend_floor=_floor(0.02),
            dividend_yields={"STABLE": 0.01, "RISKY_A": 0.06},
        )
    assert "RISKY_B" in str(excinfo.value)


def test_a_missing_yield_with_no_floor_reports_its_own_denominator():
    """`src/optimizer/holdings.py`'s rule applied to income: shrink the
    denominator and say so, rather than withhold a correct answer about the
    rest of the portfolio - or dilute the figure toward zero by reading an
    unknown as a zero.
    """
    stats = compute_weights_and_stats(
        _three_ticker_fixture(), "GMV", dividend_yields={"STABLE": 0.01, "RISKY_A": 0.06}
    )
    assert stats.dividend_yields_missing == ("RISKY_B",)
    assert 0.0 < stats.dividend_weight_covered < 1.0
    assert stats.portfolio_dividend_yield is not None


def test_the_dividend_yield_vector_is_aligned_after_min_history_drops_a_ticker():
    """`apply_min_history_rule` removes a short-history column, so the
    ceiling is the ceiling over the SURVIVORS. A high-yielding ticker that
    was dropped cannot carry weight and so must not raise it - and must
    never be the ticker a refusal names.
    """
    df = _three_ticker_fixture()
    df["HIGHPAY"] = [0.01] * 8 + [None] * 16
    filtered = apply_min_history_rule(df, min_months=24)
    assert "HIGHPAY" not in filtered.columns

    yields = {**_dividend_yields(), "HIGHPAY": 0.09}
    with pytest.raises(DividendFloorError) as excinfo:
        compute_weights_and_stats(
            filtered, "GMV", dividend_floor=_floor(0.07), dividend_yields=yields
        )
    message = str(excinfo.value)
    assert "RISKY_A at 0.0600" in message
    assert "HIGHPAY" not in message


def test_the_reported_dividend_yield_comes_from_the_raw_solved_weights():
    df = _three_ticker_fixture()
    stats = compute_weights_and_stats(
        df, "GMV", dividend_floor=_floor(0.04), dividend_yields=_dividend_yields()
    )
    # Recomputed from the rounded weights the report displays; the two agree
    # to well beyond display precision but are not the same computation.
    from_clean = sum(
        stats.weights[t] * _dividend_yields()[t] for t in stats.weights
    )
    assert stats.portfolio_dividend_yield == pytest.approx(from_clean, abs=1e-4)
    assert stats.portfolio_dividend_yield >= 0.04 - 1e-9


def test_dividend_fields_are_all_none_when_dividends_were_not_consulted():
    stats = compute_weights_and_stats(_three_ticker_fixture(), "GMV")
    assert stats.dividend_yields is None
    assert stats.dividends_per_share is None
    assert stats.portfolio_dividend_yield is None
    assert stats.dividend_yield_floor is None
    assert stats.dividend_floor_origin is None
    assert stats.dividend_yields_missing == ()


def test_the_floor_and_its_origin_are_echoed_back_for_the_report():
    stats = compute_weights_and_stats(
        _three_ticker_fixture(),
        "GMV",
        dividend_floor=_floor(0.04, "--min-annual-dividend $4,000.00 USD / --value $100,000.00 USD"),
        dividend_yields=_dividend_yields(),
    )
    assert stats.dividend_yield_floor == 0.04
    assert stats.dividend_floor_origin == (
        "--min-annual-dividend $4,000.00 USD / --value $100,000.00 USD"
    )
