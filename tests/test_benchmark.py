"""Tests for src/agentic_portfolio/optimizer/benchmark.py.

Per AGENTS.md's testing guidance these are hermetic: the pure estimator and
windowing logic is exercised against hand-built `pd.Series` fixtures, and the
one function that reads a database reads a tiny fixture DuckDB file built in
`tmp_path` rather than the real `data/portfolio.duckdb`, so nothing here
depends on the dataset build or touches the network.

Two tests exist specifically to protect the point of the whole module - that
a benchmark's figures are computed the same way the portfolio's are, so the
two can be read side by side:
`test_a_one_asset_portfolio_equals_its_own_benchmark` and
`test_volatility_is_not_the_sample_standard_deviation`.
"""

from datetime import date

import duckdb
import numpy as np
import pandas as pd
import pytest
from pypfopt import expected_returns, risk_models

from agentic_portfolio.optimizer.benchmark import (
    BENCHMARK_MIN_MONTHS,
    DEFAULT_BENCHMARKS,
    BenchmarkSource,
    BenchmarkStats,
    annualized_return_and_volatility,
    benchmark_stats_for_window,
    load_benchmark_returns,
    resolve_benchmark_ticker,
    DEFAULT_OBJECTIVE_WITHOUT_BENCHMARK,
    objective_from_benchmark,
)
from agentic_portfolio.optimizer.portfolio import compute_weights_and_stats


def _monthly_returns(n: int, ticker: str = "SPY", start: str = "2020-01-01") -> pd.Series:
    """`n` deterministic, non-degenerate monthly returns on the month-start
    grid, oscillating so the series has genuine variance without being
    perfectly regular in a way that would make the variance trivially zero.
    """
    idx = pd.date_range(start, periods=n, freq="MS", name="rebalance_date")
    values = [0.01 + (0.03 if i % 2 == 0 else -0.02) + 0.001 * (i % 5) for i in range(n)]
    return pd.Series(values, index=idx, name=ticker)


def _source(series: pd.Series, currency: str = "USD", reason: str | None = None) -> BenchmarkSource:
    return BenchmarkSource(
        ticker=str(series.name), currency=currency, monthly_returns=series, unavailable_reason=reason
    )


def _make_returns_db(db_path: str, rows: list[tuple[str, str, float | None]]) -> None:
    """rows: list of (rebalance_date, ticker, monthly_return) tuples."""
    con = duckdb.connect(db_path)
    try:
        con.execute("CREATE TABLE returns (rebalance_date DATE, ticker VARCHAR, monthly_return DOUBLE)")
        if rows:
            con.executemany("INSERT INTO returns VALUES (?, ?, ?)", rows)
    finally:
        con.close()


def test_a_one_asset_portfolio_equals_its_own_benchmark():
    """The comparability guarantee: a benchmark is a one-asset portfolio, so
    its reported figures must equal what `compute_weights_and_stats` reports
    for that same series at full weight. If this ever fails, the benchmark
    line and the portfolio line above it are measuring different things.
    """
    series = _monthly_returns(60)
    portfolio = compute_weights_and_stats(series.to_frame(), "GMV", risk_free_rate=0.02)
    benchmark = benchmark_stats_for_window(
        _source(series), date(2020, 1, 1), date(2024, 12, 1), risk_free_rate=0.02
    )

    assert portfolio.weights == {"SPY": 1.0}
    assert benchmark.annual_return == pytest.approx(portfolio.portfolio_expected_return, abs=1e-9)
    assert benchmark.annual_volatility == pytest.approx(portfolio.portfolio_volatility, abs=1e-9)
    assert benchmark.sharpe == pytest.approx(portfolio.portfolio_sharpe, abs=1e-9)


def test_volatility_is_not_the_sample_standard_deviation():
    """`ledoit_wolf()` hands scikit-learn a ddof=0 sample covariance, so the
    annualized volatility is deliberately NOT `series.std() * sqrt(12)`. The
    two differ by the ddof correction alone - small, but enough to make a
    benchmark look better or worse than the portfolio for no reason but a
    convention mismatch, which is why this is pinned rather than left to a
    future "simplification".
    """
    series = _monthly_returns(60)
    _, annual_volatility = annualized_return_and_volatility(series)
    sample_convention = float(series.std() * 12**0.5)

    assert annual_volatility != pytest.approx(sample_convention, abs=1e-9)
    assert annual_volatility == pytest.approx(float(series.std(ddof=0) * 12**0.5), rel=1e-9)


def test_annualized_figures_match_the_portfolios_own_estimators():
    series = _monthly_returns(48)
    annual_return, annual_volatility = annualized_return_and_volatility(series)

    expected_mu = expected_returns.mean_historical_return(
        series.to_frame(), returns_data=True, frequency=12
    ).iloc[0]
    expected_cov = risk_models.CovarianceShrinkage(
        series.to_frame(), returns_data=True, frequency=12
    ).ledoit_wolf().to_numpy()[0, 0]

    assert annual_return == pytest.approx(float(expected_mu), rel=1e-12)
    assert annual_volatility == pytest.approx(float(np.sqrt(expected_cov)), rel=1e-12)


def test_sharpe_uses_the_given_risk_free_rate():
    """Raising the rate must move the Sharpe ratio by exactly the rate change
    divided by the (unchanged) volatility - proof the benchmark is measured
    against the same rate the report names, not a hardcoded one.
    """
    series = _monthly_returns(60)
    low = benchmark_stats_for_window(_source(series), date(2020, 1, 1), date(2024, 12, 1), 0.02)
    high = benchmark_stats_for_window(_source(series), date(2020, 1, 1), date(2024, 12, 1), 0.04)

    assert high.annual_volatility == pytest.approx(low.annual_volatility)
    assert low.sharpe - high.sharpe == pytest.approx(0.02 / low.annual_volatility)
    assert high.risk_free_rate == 0.04


def test_window_is_sliced_to_the_portfolios_window():
    """A benchmark with far more history than the portfolio's window reports
    only the window's own months, and reports figures identical to computing
    them from that slice alone.
    """
    series = _monthly_returns(96)
    window_start, window_end = date(2022, 1, 1), date(2023, 12, 1)

    stats = benchmark_stats_for_window(_source(series), window_start, window_end, 0.02)
    slice_return, slice_volatility = annualized_return_and_volatility(
        series.loc[pd.Timestamp(window_start) : pd.Timestamp(window_end)]
    )

    assert stats.window_months == 24
    assert (stats.window_start, stats.window_end) == (window_start, window_end)
    assert stats.annual_return == pytest.approx(slice_return, rel=1e-12)
    assert stats.annual_volatility == pytest.approx(slice_volatility, rel=1e-12)


def test_a_shorter_benchmark_history_reports_the_months_it_actually_used():
    """A benchmark that listed partway into the portfolio's window is used
    for the months it has, and says how many those were.
    """
    series = _monthly_returns(30, start="2022-07-01")

    stats = benchmark_stats_for_window(_source(series), date(2020, 1, 1), date(2024, 12, 1), 0.02)

    assert stats.unavailable_reason is None
    assert stats.window_months == 30
    assert stats.window_start == date(2022, 7, 1)


def test_too_little_history_is_unavailable_with_a_reason():
    series = _monthly_returns(12)

    stats = benchmark_stats_for_window(_source(series), date(2020, 1, 1), date(2020, 12, 1), 0.02)

    assert stats.annual_return is None and stats.sharpe is None
    assert stats.window_months == 0
    assert "12 month(s)" in stats.unavailable_reason
    assert str(BENCHMARK_MIN_MONTHS) in stats.unavailable_reason
    assert "SPY" in stats.unavailable_reason


def test_no_rows_at_all_is_unavailable():
    empty = pd.Series(dtype=float, name="SPY", index=pd.DatetimeIndex([], name="rebalance_date"))

    stats = benchmark_stats_for_window(_source(empty), date(2020, 1, 1), date(2024, 12, 1), 0.02)

    assert stats.unavailable_reason is not None
    assert stats.annual_volatility is None


def test_an_unavailable_source_reports_its_own_reason_verbatim():
    """A refusal decided upstream (a wrong currency, an unresolvable symbol)
    must reach the report unchanged rather than being re-worded here.
    """
    source = _source(_monthly_returns(60), reason="SPY trades in USD but this portfolio is JPY")

    stats = benchmark_stats_for_window(source, date(2020, 1, 1), date(2024, 12, 1), 0.02)

    assert stats.unavailable_reason == "SPY trades in USD but this portfolio is JPY"
    assert stats.ticker == "SPY"
    assert stats.annual_return is None


def test_zero_volatility_is_unavailable_rather_than_dividing_by_zero():
    idx = pd.date_range("2020-01-01", periods=36, freq="MS", name="rebalance_date")
    constant = pd.Series([0.01] * 36, index=idx, name="FLAT")

    stats = benchmark_stats_for_window(_source(constant), date(2020, 1, 1), date(2022, 12, 1), 0.02)

    assert stats.sharpe is None
    assert "volatility" in stats.unavailable_reason


def test_benchmark_stats_for_window_with_no_source_is_none():
    """`--benchmark none` and "nobody named one" both arrive here as `None`,
    and must print nothing at all rather than an apology.
    """
    assert benchmark_stats_for_window(None, date(2020, 1, 1), date(2024, 12, 1), 0.02) is None


def test_resolve_benchmark_ticker_precedence():
    assert resolve_benchmark_ticker("USD", override="qqq", saved="VOO") == "QQQ"
    assert resolve_benchmark_ticker("USD", override=None, saved="voo") == "VOO"
    assert resolve_benchmark_ticker("USD") == "SPY"
    assert resolve_benchmark_ticker("JPY") is None
    assert resolve_benchmark_ticker("JPY", saved="1306.t") == "1306.T"
    assert resolve_benchmark_ticker("USD", override="   ") == "SPY"


def test_default_benchmarks_covers_usd_only():
    """Every other currency is asked for rather than guessed - see
    DEFAULT_BENCHMARKS' own docstring for why.
    """
    assert DEFAULT_BENCHMARKS == {"USD": "SPY"}


def test_load_benchmark_returns_stops_at_as_of(tmp_path):
    db_path = str(tmp_path / "returns.duckdb")
    _make_returns_db(
        db_path,
        [
            ("2024-01-02", "SPY", 0.01),
            ("2024-02-01", "SPY", 0.02),
            ("2024-03-01", "SPY", 0.03),
            ("2024-04-01", "SPY", 0.04),
            ("2024-02-01", "AAPL", 0.99),
        ],
    )

    series = load_benchmark_returns("SPY", date(2024, 3, 1), db_path)

    assert list(series.index.date) == [date(2024, 1, 2), date(2024, 2, 1), date(2024, 3, 1)]
    assert series.name == "SPY"
    assert series.tolist() == [0.01, 0.02, 0.03]


def test_load_benchmark_returns_drops_null_months(tmp_path):
    """The earliest month of any returns build is null by construction (there
    is no priced month before it), so a benchmark must not carry it.
    """
    db_path = str(tmp_path / "returns.duckdb")
    _make_returns_db(
        db_path, [("2024-01-02", "SPY", None), ("2024-02-01", "SPY", 0.02)]
    )

    series = load_benchmark_returns("SPY", date(2024, 4, 1), db_path)

    assert list(series.index.date) == [date(2024, 2, 1)]


def test_load_benchmark_returns_is_empty_for_an_absent_ticker(tmp_path):
    db_path = str(tmp_path / "returns.duckdb")
    _make_returns_db(db_path, [("2024-02-01", "AAPL", 0.02)])

    assert load_benchmark_returns("SPY", date(2024, 4, 1), db_path).empty


def test_load_benchmark_returns_ignores_a_missing_file_without_creating_it(tmp_path):
    """Asking about a database that is not there must not materialize one -
    the same rule `migrate_candidate_pools` follows for a missing pool file.
    """
    missing = tmp_path / "absent.duckdb"

    assert load_benchmark_returns("SPY", date(2024, 4, 1), str(missing)).empty
    assert not missing.exists()


def test_load_benchmark_returns_ignores_a_database_without_a_returns_table(tmp_path):
    db_path = str(tmp_path / "no_returns.duckdb")
    con = duckdb.connect(db_path)
    try:
        con.execute("CREATE TABLE prices (date DATE, ticker VARCHAR)")
    finally:
        con.close()

    assert load_benchmark_returns("SPY", date(2024, 4, 1), db_path).empty


def test_load_benchmark_returns_leaves_the_database_untouched(tmp_path):
    """Opened read-only, so reading a benchmark out of the shared historical
    cache cannot modify it.
    """
    db_path = tmp_path / "returns.duckdb"
    _make_returns_db(str(db_path), [("2024-02-01", "SPY", 0.02)])
    before = db_path.stat().st_mtime_ns

    load_benchmark_returns("SPY", date(2024, 4, 1), str(db_path))

    assert db_path.stat().st_mtime_ns == before


def test_the_benchmark_is_not_shrunk_against_a_pool():
    """The benchmark's volatility is its own, not a function of whatever a
    portfolio happens to hold.

    Ledoit-Wolf shrinkage pulls each variance toward the average of the
    matrix it is applied to, so the SAME series reports a different
    volatility standalone than it does as one column among several - here the
    difference is several percent. This test pins which of the two the report
    uses: the standalone one. Folding the benchmark into the pool's matrix so
    that both sides were shrunk alike would make the benchmark's printed
    figures change when a candidate is added, which is exactly what a
    reference point must not do (see the module docstring).
    """
    bench = _monthly_returns(60, "SPY")
    # Independent series with spread-out variances. Scalar multiples of one
    # series will NOT do: perfectly correlated columns drive the shrinkage
    # constant to nearly zero, and the fixture would prove nothing.
    rng = np.random.default_rng(7)
    pool_with_benchmark = pd.DataFrame(
        {
            "SPY": bench,
            "A": rng.normal(0.01, 0.09, 60),
            "B": rng.normal(0.005, 0.01, 60),
            "C": rng.normal(0.02, 0.05, 60),
        },
        index=bench.index,
    )

    standalone = annualized_return_and_volatility(bench)[1]
    in_matrix = float(
        np.sqrt(
            risk_models.CovarianceShrinkage(pool_with_benchmark, returns_data=True, frequency=12)
            .ledoit_wolf()
            .to_numpy()[0, 0]
        )
    )
    stats = benchmark_stats_for_window(_source(bench), date(2020, 1, 1), date(2024, 12, 1), 0.02)

    assert in_matrix != pytest.approx(standalone, rel=1e-3), "fixture must actually provoke shrinkage"
    assert stats.annual_volatility == pytest.approx(standalone, rel=1e-12)
    assert stats.annual_volatility != pytest.approx(in_matrix, rel=1e-3)


# ---------------------------------------------------------------------------
# Deriving the objective from the pool's benchmark
# ---------------------------------------------------------------------------


def _bench(**overrides) -> BenchmarkStats:
    fields = {
        "ticker": "1321.T", "currency": "JPY", "annual_return": 0.0911,
        "annual_volatility": 0.1770, "sharpe": 1.1065, "risk_free_rate": 0.02,
        "window_start": date(2021, 9, 1), "window_end": date(2026, 9, 1),
        "window_months": 60, "unavailable_reason": None,
    }
    return BenchmarkStats(**{**fields, **overrides})


def test_an_explicit_objective_wins_over_the_benchmark():
    """Nothing changes for anyone already passing flags."""
    resolved = objective_from_benchmark(_bench(), "MSR", None)
    assert resolved.objective == "MSR"
    assert resolved.target_annual_return is None
    assert resolved.origin == "--objective"


def test_an_explicit_objective_of_mv_keeps_its_own_target():
    resolved = objective_from_benchmark(_bench(), "MV", 0.07)
    assert (resolved.objective, resolved.target_annual_return) == ("MV", 0.07)


def test_a_target_return_without_an_objective_implies_mv():
    """The only reading available: no other objective consumes a target."""
    resolved = objective_from_benchmark(_bench(), None, 0.07)
    assert resolved.objective == "MV"
    assert resolved.target_annual_return == pytest.approx(0.07)
    assert "--target-return" in resolved.origin


def test_a_measurable_benchmark_gives_mv_at_its_own_return():
    """The point of the feature: match what the benchmark returned, at the
    least risk that does so.
    """
    resolved = objective_from_benchmark(_bench(), None, None)
    assert resolved.objective == "MV"
    assert resolved.target_annual_return == pytest.approx(0.0911)
    assert "1321.T" in resolved.origin
    assert resolved.clamped_from is None


@pytest.mark.parametrize(
    "benchmark,expected_origin",
    [
        (None, "no benchmark for this pool"),
        (
            {"annual_return": None, "unavailable_reason": "does not trade in JPY"},
            "benchmark unavailable: does not trade in JPY",
        ),
    ],
)
def test_no_usable_benchmark_gives_gmv_and_names_which_reason(benchmark, expected_origin):
    """GMV is the only objective needing no external input - MV wants a
    target and MSR a rate to beat - so it is the honest fallback. The reasons
    are different situations and the report should say which.
    """
    stats = None if benchmark is None else _bench(**benchmark)
    resolved = objective_from_benchmark(stats, None, None)
    assert resolved.objective == DEFAULT_OBJECTIVE_WITHOUT_BENCHMARK == "GMV"
    assert resolved.target_annual_return is None
    assert resolved.origin == expected_origin
