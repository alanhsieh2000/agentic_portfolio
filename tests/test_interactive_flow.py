"""Tests for src/agentic_portfolio/flow/interactive.py's `edit_candidates`,
`validate_and_edit_candidates`, `open_pipeline_session` routing, and
`run_pipeline`/`run_scan` selection-mode branching, plus
src/agentic_portfolio/flow/backtest.py's turnover-cost/gross-return/Sharpe-ratio arithmetic.

The `user_provided` selection's tests monkeypatch `build_live_snapshot`
(that selection always routes through it, even for a backtest date, so it
never writes to the shared cache) and `validate_and_ingest_tickers` (the
one call that would otherwise reach yfinance).

Per AGENTS.md, no test here calls yfinance's live API, any LLM, or hits
`data/portfolio.duckdb`. `prepare_benchmark`'s tests monkeypatch the same
ingestion seam plus `build_scratch_snapshot`, so the "benchmark rows land in
their own database, never the session's" guarantee is asserted rather than
assumed. `run_pipeline`'s selection-mode tests exercise
the real `screen`/`scan_with_detail`/`load_returns_matrix`/
`compute_weights`/`load_latest_prices`/`allocate_shares` chain against a
small hand-built fixture DuckDB (mirroring `tests/test_llm_s.py`'s
`screen` fixture and `tests/test_optimizer.py`'s returns-matrix fixture),
monkeypatching only `generate_rule` and `screen_month` - the two calls
that would otherwise need a real LLM (or, for `screen_month`, real
network access too) - and asserting the one `selection` says to skip is,
via a spy, genuinely never called - not just result-discarded. Live mode
(any `rebalance_date` outside the stored 2020-2024 window) is not
exercised here at all: it always makes real Wikipedia/yfinance/SEC EDGAR
calls by design (see `src/agentic_portfolio/flow/live.py`), verified instead by the real,
manual runs recorded in `plans/06_interactive_flow.md`.
"""

from contextlib import contextmanager
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

import duckdb
import pandas as pd
from agentic_portfolio.optimizer.dividends import DividendFloorError
from agentic_portfolio.dataset.ticker_profile import RawTickerProfile
from agentic_portfolio.flow.interactive import (
    CLAMP_EPSILON,
    CONCENTRATION_THRESHOLD,
    _concentration_note,
    _optimize_clamping_an_unreachable_target,
)
from agentic_portfolio.optimizer.benchmark import ResolvedObjective
from agentic_portfolio.optimizer.portfolio import (
    UnreachableTargetReturnError,
    compute_weights_and_stats,
)
import pytest

from agentic_portfolio.agents.llm_s_schema import ScreeningRule
from agentic_portfolio.flow.backtest import _gross_return, _turnover_cost, compute_sharpe_ratio
from agentic_portfolio.dataset.ticker_currency import MixedCurrencyPoolError
from agentic_portfolio.flow.interactive import (
    compute_weights_and_allocation,
    edit_candidates,
    open_pipeline_session,
    prepare_benchmark,
    prepare_ticker_summary,
    run_pipeline,
    run_pipeline_against,
    run_scan,
    validate_and_edit_candidates,
)
from agentic_portfolio.flow.live import (
    _init_empty_price_and_returns_tables,
    build_live_snapshot,
    build_scratch_snapshot,
)
from agentic_portfolio.optimizer.benchmark import BENCHMARK_MIN_MONTHS, BenchmarkSource
from agentic_portfolio.optimizer.portfolio import DEFAULT_TARGET_ANNUAL_RETURN, PortfolioStats

# ---------------------------------------------------------------------------
# edit_candidates
# ---------------------------------------------------------------------------


def test_edit_candidates_adds_new_ticker():
    scan_result = {"candidates": ["AAPL", "MSFT"]}
    assert edit_candidates(scan_result, add=["NVDA"], remove=[]) == ["AAPL", "MSFT", "NVDA"]


def test_edit_candidates_removes_present_ticker():
    scan_result = {"candidates": ["AAPL", "MSFT", "NVDA"]}
    assert edit_candidates(scan_result, add=[], remove=["MSFT"]) == ["AAPL", "NVDA"]


def test_edit_candidates_add_and_remove_together():
    scan_result = {"candidates": ["AAPL", "MSFT"]}
    assert edit_candidates(scan_result, add=["NVDA"], remove=["MSFT"]) == ["AAPL", "NVDA"]


def test_edit_candidates_adding_already_present_ticker_is_a_no_op():
    scan_result = {"candidates": ["AAPL", "MSFT"]}
    assert edit_candidates(scan_result, add=["AAPL"], remove=[]) == ["AAPL", "MSFT"]


def test_edit_candidates_removing_absent_ticker_is_a_no_op():
    scan_result = {"candidates": ["AAPL", "MSFT"]}
    assert edit_candidates(scan_result, add=[], remove=["ZZZZ"]) == ["AAPL", "MSFT"]


def test_edit_candidates_result_is_sorted():
    scan_result = {"candidates": ["MSFT"]}
    assert edit_candidates(scan_result, add=["AAPL", "ZZZZ"], remove=[]) == ["AAPL", "MSFT", "ZZZZ"]


# ---------------------------------------------------------------------------
# run_pipeline selection-mode branching
# ---------------------------------------------------------------------------


def _rule(buy_condition: str, sell_condition: str) -> ScreeningRule:
    return ScreeningRule(year=2024, buy_condition=buy_condition, sell_condition=sell_condition, rationale="test")


def _build_fixture_db(db_path: str, include_factors: bool) -> None:
    """A single-ticker (AAA) fixture DuckDB with 24 months of `returns`
    ending 2024-03-01 (real, slightly-varying values - not a constant, so
    the covariance matrix isn't degenerately zero) and one `prices` row
    on that date - just enough for `load_returns_matrix`/`compute_weights`/
    `load_latest_prices`/`allocate_shares` to run for real end to end on a
    trivial one-asset optimization (weight=1.0 regardless of objective,
    since there is only one asset to choose). `factors` (needed only when
    LLM-S's real `screen` actually runs) is included only when
    `include_factors` is True.
    """
    con = duckdb.connect(db_path)
    try:
        if include_factors:
            con.execute(
                "CREATE TABLE factors (rebalance_date DATE, ticker VARCHAR, "
                "mve DOUBLE, bm DOUBLE, mom12m DOUBLE, mve_z DOUBLE, bm_z DOUBLE, mom12m_z DOUBLE)"
            )
            con.execute("INSERT INTO factors VALUES ('2024-03-01', 'AAA', 0, 0, 0, 0.0, 0.0, 1.0)")

        con.execute("CREATE TABLE returns (rebalance_date DATE, ticker VARCHAR, monthly_return DOUBLE)")
        months = pd.date_range(end="2024-03-01", periods=24, freq="MS")
        con.executemany(
            "INSERT INTO returns VALUES (?, 'AAA', ?)",
            [(m.date().isoformat(), 0.01 + (0.001 if i % 2 == 0 else -0.001)) for i, m in enumerate(months)],
        )

        con.execute("CREATE TABLE prices (date DATE, ticker VARCHAR, close DOUBLE, adj_close DOUBLE)")
        con.execute("INSERT INTO prices VALUES ('2024-03-01', 'AAA', 100.0, 100.0)")
    finally:
        con.close()


def test_run_pipeline_llm_s_only_never_calls_llm_f_signal_function(tmp_path, monkeypatch):
    db_path = str(tmp_path / "fixture.duckdb")
    _build_fixture_db(db_path, include_factors=True)

    fake_rule = _rule(buy_condition="mom12m > 0.5", sell_condition="mom12m < -0.5")
    screen_month_spy = MagicMock()
    monkeypatch.setattr("agentic_portfolio.flow.interactive.generate_rule", lambda year, model=None, db_path=None: fake_rule)
    monkeypatch.setattr("agentic_portfolio.flow.interactive.screen_month", screen_month_spy)

    result = run_pipeline(date(2024, 3, 1), "GMV", 1000.0, selection="llm_s_only", db_path=db_path)

    assert screen_month_spy.called is False
    assert result["llm_f_signals"] is None
    assert result["rule"] is fake_rule
    assert result["scan_detail"]["branch"] == "llm_s_only"
    assert result["weights"] == pytest.approx({"AAA": 1.0}, abs=1e-3)


def test_run_pipeline_llm_f_only_never_calls_llm_s_rule_generation(tmp_path, monkeypatch):
    db_path = str(tmp_path / "fixture.duckdb")
    _build_fixture_db(db_path, include_factors=False)

    fake_signals = pd.DataFrame({"ticker": ["AAA"], "signal": ["buy"]})
    generate_rule_spy = MagicMock()
    monkeypatch.setattr("agentic_portfolio.flow.interactive.generate_rule", generate_rule_spy)
    monkeypatch.setattr("agentic_portfolio.flow.interactive.screen_month", lambda year, month, db_path=None: fake_signals)

    result = run_pipeline(date(2024, 3, 1), "GMV", 1000.0, selection="llm_f_only", db_path=db_path)

    assert generate_rule_spy.called is False
    assert result["rule"] is None
    assert result["llm_s_signals"] is None
    assert result["scan_detail"]["branch"] == "llm_f_only"
    assert result["weights"] == pytest.approx({"AAA": 1.0}, abs=1e-3)


def test_run_pipeline_llm_s_and_f_calls_both_agents(tmp_path, monkeypatch):
    db_path = str(tmp_path / "fixture.duckdb")
    _build_fixture_db(db_path, include_factors=True)

    fake_rule = _rule(buy_condition="mom12m > 0.5", sell_condition="mom12m < -0.5")
    fake_signals = pd.DataFrame({"ticker": ["AAA"], "signal": ["buy"]})
    monkeypatch.setattr("agentic_portfolio.flow.interactive.generate_rule", lambda year, model=None, db_path=None: fake_rule)
    monkeypatch.setattr("agentic_portfolio.flow.interactive.screen_month", lambda year, month, db_path=None: fake_signals)

    result = run_pipeline(date(2024, 3, 1), "GMV", 1000.0, selection="llm_s_and_f", db_path=db_path)

    assert result["rule"] is fake_rule
    assert result["llm_s_signals"] is not None
    assert result["llm_f_signals"] is not None
    # The one shared candidate agrees on both sides, so the intersection has
    # cardinality 1 and scan_with_detail falls back to the union (still {AAA}).
    assert result["scan_detail"]["branch"] == "union"


def test_run_pipeline_invalid_selection_raises_value_error():
    with pytest.raises(ValueError, match="selection"):
        run_pipeline(date(2024, 3, 1), "GMV", 1000.0, selection="bogus")


# ---------------------------------------------------------------------------
# PortfolioStats and target-return threading
# ---------------------------------------------------------------------------


def test_compute_weights_and_allocation_returns_portfolio_stats(tmp_path):
    db_path = str(tmp_path / "fixture.duckdb")
    _build_fixture_db(db_path, include_factors=False)

    stats, allocation = compute_weights_and_allocation(["AAA"], "GMV", 1000.0, date(2024, 3, 1), db_path)

    assert isinstance(stats, PortfolioStats)
    assert stats.weights == pytest.approx({"AAA": 1.0}, abs=1e-3)
    # A single asset's portfolio figures are that asset's own figures.
    assert stats.portfolio_expected_return == pytest.approx(stats.expected_returns["AAA"])
    assert stats.portfolio_volatility == pytest.approx(stats.volatility["AAA"])
    assert allocation[0] == {"AAA": 10}


def _add_currency_table(db_path: str, rows: list[tuple[str, str]]) -> None:
    con = duckdb.connect(db_path)
    try:
        con.execute(
            "CREATE TABLE ticker_currency "
            "(ticker VARCHAR, currency VARCHAR, quoted_currency VARCHAR, price_multiplier DOUBLE)"
        )
        con.executemany(
            "INSERT INTO ticker_currency VALUES (?, ?, ?, 1.0)", [(t, c, c) for t, c in rows]
        )
    finally:
        con.close()


def test_compute_weights_and_allocation_refuses_a_mixed_currency_pool(tmp_path):
    """The last line of defense, reachable by a route that skipped the
    interactive refusal - a hand-edited memory file, or a direct
    `run_pipeline` call with no confirmation loop.
    """
    db_path = str(tmp_path / "fixture.duckdb")
    _build_fixture_db(db_path, include_factors=False)
    _add_currency_table(db_path, [("AAA", "JPY"), ("BBB", "USD")])

    with pytest.raises(MixedCurrencyPoolError, match="mixes currencies"):
        compute_weights_and_allocation(["AAA", "BBB"], "GMV", 1000.0, date(2024, 3, 1), db_path)


def test_compute_weights_and_allocation_is_unaffected_without_a_currency_table(tmp_path):
    """The regression that matters most: every LLM selection and the whole
    backtest path run against databases that have no `ticker_currency`
    table, so the guard must find one implicit US-dollar group and get out
    of the way.
    """
    db_path = str(tmp_path / "fixture.duckdb")
    _build_fixture_db(db_path, include_factors=False)

    stats, _allocation = compute_weights_and_allocation(["AAA"], "GMV", 1000.0, date(2024, 3, 1), db_path)

    assert stats.weights == pytest.approx({"AAA": 1.0}, abs=1e-3)


def test_run_pipeline_against_echoes_the_currency(tmp_path, monkeypatch):
    db_path = str(tmp_path / "fixture.duckdb")
    _build_fixture_db(db_path, include_factors=False)

    result = run_pipeline_against(
        date(2024, 3, 1), "GMV", 1000.0, "user_provided", db_path, "backtest",
        candidates=["AAA"], currency="JPY",
    )

    assert result["currency"] == "JPY"


def test_run_pipeline_stats_agree_with_the_flat_weights_key(tmp_path, monkeypatch):
    """`"weights"` stays a plain ticker-to-weight mapping for every existing
    reader; `"stats"` is additive and must describe the same portfolio.
    """
    db_path = str(tmp_path / "fixture.duckdb")
    _build_fixture_db(db_path, include_factors=False)

    @contextmanager
    def fake_snapshot(as_of, selection, source_db_path, **_kwargs):
        yield db_path

    monkeypatch.setattr("agentic_portfolio.flow.interactive.build_live_snapshot", fake_snapshot)

    result = run_pipeline(
        date(2024, 3, 1), "GMV", 1000.0, selection="user_provided", db_path=db_path, candidates=["AAA"]
    )

    assert isinstance(result["weights"], dict)
    assert result["stats"].weights == result["weights"]
    assert result["stats"].risk_free_rate == pytest.approx(0.02)


def test_run_pipeline_target_return_defaults_and_can_be_overridden(tmp_path, monkeypatch):
    db_path = str(tmp_path / "fixture.duckdb")
    _build_fixture_db(db_path, include_factors=False)

    @contextmanager
    def fake_snapshot(as_of, selection, source_db_path, **_kwargs):
        yield db_path

    monkeypatch.setattr("agentic_portfolio.flow.interactive.build_live_snapshot", fake_snapshot)

    defaulted = run_pipeline(
        date(2024, 3, 1), "MV", 1000.0, selection="user_provided", db_path=db_path, candidates=["AAA"]
    )
    overridden = run_pipeline(
        date(2024, 3, 1), "MV", 1000.0, selection="user_provided", db_path=db_path, candidates=["AAA"],
        target_annual_return=0.05,
    )

    assert defaulted["stats"].target_annual_return == DEFAULT_TARGET_ANNUAL_RETURN
    assert overridden["stats"].target_annual_return == 0.05


# ---------------------------------------------------------------------------
# user_provided selection
# ---------------------------------------------------------------------------


def test_run_scan_user_provided_never_calls_either_agent_or_the_scanner(monkeypatch):
    """The user chose these candidates, so there are no signals to generate
    or combine - LLM-S, LLM-F and the scanner must all be skipped outright,
    not merely have their results discarded.
    """
    generate_rule_spy = MagicMock()
    screen_spy = MagicMock()
    screen_month_spy = MagicMock()
    scan_with_detail_spy = MagicMock()
    monkeypatch.setattr("agentic_portfolio.flow.interactive.generate_rule", generate_rule_spy)
    monkeypatch.setattr("agentic_portfolio.flow.interactive.screen", screen_spy)
    monkeypatch.setattr("agentic_portfolio.flow.interactive.screen_month", screen_month_spy)
    monkeypatch.setattr("agentic_portfolio.flow.interactive.scan_with_detail", scan_with_detail_spy)

    scan = run_scan(date(2026, 9, 5), "user_provided", "unused.duckdb", candidates=["MSFT", "AAPL", "AAPL"])

    assert generate_rule_spy.called is False
    assert screen_spy.called is False
    assert screen_month_spy.called is False
    assert scan_with_detail_spy.called is False
    assert scan == {
        "rule": None,
        "llm_s_signals": None,
        "llm_f_signals": None,
        "scan_detail": {
            "candidates": ["AAPL", "MSFT"],
            "branch": "user_provided",
            "buy_s_size": None,
            "buy_f_size": None,
            "intersection_size": None,
            "union_size": None,
        },
    }


def test_run_scan_user_provided_with_no_candidates_yields_an_empty_list():
    scan = run_scan(date(2026, 9, 5), "user_provided", "unused.duckdb")
    assert scan["scan_detail"]["candidates"] == []


def test_open_pipeline_session_user_provided_uses_a_snapshot_even_for_a_backtest_date(monkeypatch):
    """`user_provided` writes its tickers' prices/returns into whichever
    database it is handed, so it must never be handed the shared historical
    cache - even for a date that cache covers.
    """

    @contextmanager
    def fake_snapshot(as_of, selection, source_db_path, **_kwargs):
        yield "/tmp/fake-snapshot.duckdb"

    snapshot_spy = MagicMock(side_effect=fake_snapshot)
    monkeypatch.setattr("agentic_portfolio.flow.interactive.build_live_snapshot", snapshot_spy)

    with open_pipeline_session(date(2024, 3, 1), "user_provided", "data/portfolio.duckdb") as (db_path, mode):
        assert db_path == "/tmp/fake-snapshot.duckdb"
        assert mode == "backtest"

    assert snapshot_spy.called is True


def test_open_pipeline_session_backtest_date_still_reads_the_cache_for_other_selections(monkeypatch):
    snapshot_spy = MagicMock()
    monkeypatch.setattr("agentic_portfolio.flow.interactive.build_live_snapshot", snapshot_spy)

    with open_pipeline_session(date(2024, 3, 1), "llm_s_only", "data/portfolio.duckdb") as (db_path, mode):
        assert db_path == "data/portfolio.duckdb"
        assert mode == "backtest"

    assert snapshot_spy.called is False


def _fake_ingest(monkeypatch, valid, invalid, currencies):
    monkeypatch.setattr(
        "agentic_portfolio.flow.interactive.validate_and_ingest_tickers",
        lambda tickers, as_of, db_path: (valid, invalid, currencies),
    )
    monkeypatch.setattr(
        "agentic_portfolio.flow.interactive.load_ticker_currencies",
        lambda tickers, db_path: {t: currencies.get(t, "USD") for t in tickers},
    )


def test_validate_and_edit_candidates_adds_only_the_valid_tickers(monkeypatch):
    _fake_ingest(monkeypatch, ["NVDA"], {"ZZZZ": "no data"}, {"NVDA": "USD"})

    edit = validate_and_edit_candidates(
        ["AAPL"], add=["NVDA", "ZZZZ"], remove=[], as_of=date(2026, 9, 5), db_path="unused.duckdb",
        pool_currency="USD",
    )

    assert edit.pool == ["AAPL", "NVDA"]
    assert edit.added == ["NVDA"]
    assert edit.invalid == {"ZZZZ": "no data"}
    assert edit.refused == {}
    assert edit.pool_currency == "USD"


def test_validate_and_edit_candidates_removes_without_validating(monkeypatch):
    """A removal needs no network round trip - and an empty `add` must not
    trigger one either.
    """
    ingest_spy = MagicMock()
    monkeypatch.setattr("agentic_portfolio.flow.interactive.validate_and_ingest_tickers", ingest_spy)
    monkeypatch.setattr(
        "agentic_portfolio.flow.interactive.load_ticker_currencies", lambda tickers, db_path: {"AAPL": "USD"}
    )

    edit = validate_and_edit_candidates(
        ["AAPL", "MSFT"], add=[], remove=["MSFT"], as_of=date(2026, 9, 5), db_path="unused.duckdb",
        pool_currency="USD",
    )

    assert edit.pool == ["AAPL"]
    assert edit.added == []
    assert edit.invalid == {}
    assert edit.refused == {}
    assert ingest_spy.called is False


def test_validate_and_edit_candidates_refuses_a_cross_currency_ticker(monkeypatch):
    """The headline behavior: a dollar ticker cannot join a yen pool, but a
    yen ticker typed on the same line still lands.
    """
    _fake_ingest(
        monkeypatch, ["6758.T", "AAPL"], {}, {"6758.T": "JPY", "AAPL": "USD"}
    )

    edit = validate_and_edit_candidates(
        ["7203.T"], add=["AAPL", "6758.T"], remove=[], as_of=date(2026, 9, 5), db_path="unused.duckdb",
        pool_currency="JPY",
    )

    assert edit.pool == ["6758.T", "7203.T"]
    assert edit.added == ["6758.T"]
    assert edit.refused == {"AAPL": "USD"}
    assert edit.pool_currency == "JPY"


def test_validate_and_edit_candidates_first_typed_ticker_sets_an_empty_pools_currency(monkeypatch):
    """`validate_and_ingest_tickers` returns its list sorted, and '7203.T'
    sorts before 'AAPL', so without re-ordering by what was typed this pool
    would silently become JPY.
    """
    _fake_ingest(
        monkeypatch, ["7203.T", "AAPL"], {}, {"7203.T": "JPY", "AAPL": "USD"}
    )

    edit = validate_and_edit_candidates(
        [], add=["AAPL", "7203.T"], remove=[], as_of=date(2026, 9, 5), db_path="unused.duckdb"
    )

    assert edit.pool_currency == "USD"
    assert edit.added == ["AAPL"]
    assert edit.refused == {"7203.T": "JPY"}


def test_run_pipeline_user_provided_optimizes_the_supplied_candidates(tmp_path, monkeypatch):
    """End to end for the new selection: no agent runs, and the user's own
    candidate list flows through the real optimizer chain.
    """
    db_path = str(tmp_path / "fixture.duckdb")
    _build_fixture_db(db_path, include_factors=False)

    @contextmanager
    def fake_snapshot(as_of, selection, source_db_path, **_kwargs):
        yield db_path

    generate_rule_spy = MagicMock()
    screen_month_spy = MagicMock()
    monkeypatch.setattr("agentic_portfolio.flow.interactive.build_live_snapshot", fake_snapshot)
    monkeypatch.setattr("agentic_portfolio.flow.interactive.generate_rule", generate_rule_spy)
    monkeypatch.setattr("agentic_portfolio.flow.interactive.screen_month", screen_month_spy)

    result = run_pipeline(
        date(2024, 3, 1), "GMV", 1000.0, selection="user_provided", db_path=db_path, candidates=["AAA"]
    )

    assert generate_rule_spy.called is False
    assert screen_month_spy.called is False
    assert result["rule"] is None
    assert result["scan_detail"]["branch"] == "user_provided"
    assert result["scan_detail"]["candidates"] == ["AAA"]
    assert result["weights"] == pytest.approx({"AAA": 1.0}, abs=1e-3)


# ---------------------------------------------------------------------------
# src/agentic_portfolio/flow/backtest.py's turnover-cost / gross-return / Sharpe-ratio arithmetic
# ---------------------------------------------------------------------------


def test_turnover_cost_matches_hand_computed_value():
    """A fixture pair of monthly weight vectors with disjoint-but-
    overlapping ticker sets - AAPL is shared, GOOG only in `w_prev`, MSFT
    only in `w_t` - so the union-alignment logic is actually exercised,
    not just a same-tickers-both-months shortcut.
    """
    w_t = {"AAPL": 0.6, "MSFT": 0.4}
    w_prev = {"AAPL": 0.5, "GOOG": 0.5}
    k = 10.0 / 10000  # settings.transaction_cost_bps default, 10 bps
    expected = k * (abs(0.6 - 0.5) + abs(0.0 - 0.5) + abs(0.4 - 0.0))

    assert _turnover_cost(w_t, w_prev) == pytest.approx(expected)


def test_turnover_cost_zero_for_identical_weight_vectors():
    weights = {"AAPL": 0.5, "MSFT": 0.5}
    assert _turnover_cost(weights, weights) == pytest.approx(0.0, abs=1e-9)


def test_gross_return_renormalizes_over_known_tickers_when_one_is_missing():
    """NVDA has no monthly_return at the following rebalance date (e.g.
    delisted mid-month) - it must be dropped from both the numerator AND
    the weight-sum denominator (renormalized), not merely from the
    numerator, since the latter is numerically indistinguishable from
    treating it as a zero return.
    """
    weights = {"AAPL": 0.5, "MSFT": 0.3, "NVDA": 0.2}
    next_month_returns = pd.Series({"AAPL": 0.10, "MSFT": 0.05})
    expected = (0.5 * 0.10 + 0.3 * 0.05) / 0.8

    result = _gross_return(weights, next_month_returns, rebalance_date=date(2024, 3, 1))

    assert result == pytest.approx(expected)


def test_gross_return_returns_none_when_every_candidate_lacks_forward_return():
    result = _gross_return({"AAPL": 1.0}, pd.Series(dtype=float), rebalance_date=date(2024, 3, 1))
    assert result is None


def test_compute_sharpe_ratio_matches_hand_computed_value():
    net_returns = pd.Series([0.02, -0.01, 0.03, 0.00, 0.015])
    risk_free_rate = 0.02
    monthly_risk_free_rate = risk_free_rate / 12
    expected = (net_returns.mean() - monthly_risk_free_rate) / net_returns.std() * (12**0.5)

    assert compute_sharpe_ratio(net_returns, risk_free_rate=risk_free_rate) == pytest.approx(expected)


def test_compute_sharpe_ratio_uses_settings_default_risk_free_rate():
    from agentic_portfolio.config.settings import settings

    net_returns = pd.Series([0.01, 0.02, -0.005])
    expected = (net_returns.mean() - settings.risk_free_rate / 12) / net_returns.std() * (12**0.5)

    assert compute_sharpe_ratio(net_returns) == pytest.approx(expected)


# ---------------------------------------------------------------------------
# prepare_benchmark / build_scratch_snapshot
# ---------------------------------------------------------------------------


def _bench_months(n: int = 24, end: str = "2024-03-01") -> list[tuple[str, float]]:
    """`n` (date, monthly_return) pairs on the month-start grid ending at
    `end`, varying so the covariance is not degenerately zero.
    """
    months = pd.date_range(end=end, periods=n, freq="MS")
    return [(m.date().isoformat(), 0.02 + (0.01 if i % 2 == 0 else -0.008)) for i, m in enumerate(months)]


def _insert_returns(db_path: str, ticker: str, rows: list[tuple[str, float]]) -> None:
    """One multi-row INSERT rather than `executemany`, which is five times
    slower here for no benefit: measured on this fixture's own row counts,
    `executemany` costs 4.7s/4.9s/9.0s for 24/36/60 rows against 1.0s/0.8s/2.8s
    for a single statement. These fixtures are built dozens of times across
    this file, so the difference is minutes of suite time.
    """
    if not rows:
        return
    values = ", ".join(f"('{d}', '{ticker}', {r!r})" for d, r in rows)
    con = duckdb.connect(db_path)
    try:
        con.execute("CREATE TABLE IF NOT EXISTS returns (rebalance_date DATE, ticker VARCHAR, monthly_return DOUBLE)")
        con.execute(f"INSERT INTO returns VALUES {values}")
    finally:
        con.close()


def _fake_benchmark_fetch(monkeypatch, scratch_db_path, rows, currency="USD", invalid=None, raises=None):
    """Stand in for the yfinance round trip `prepare_benchmark` makes when a
    benchmark is not already in the session database: `build_scratch_snapshot`
    yields `scratch_db_path`, and the ingestion writes `rows` into whichever
    database it is handed (which is how a test can prove those rows landed in
    the scratch database and not the session's).
    """

    @contextmanager
    def fake_scratch(prefix: str = "benchmark_snapshot_"):
        yield scratch_db_path

    def fake_ingest(tickers, as_of, db_path):
        if raises is not None:
            raise raises
        if invalid:
            return [], dict(invalid), {}
        _insert_returns(db_path, tickers[0], rows)
        return list(tickers), {}, {tickers[0]: currency}

    monkeypatch.setattr("agentic_portfolio.flow.interactive.build_scratch_snapshot", fake_scratch)
    monkeypatch.setattr("agentic_portfolio.flow.interactive.validate_and_ingest_tickers", fake_ingest)


def test_prepare_benchmark_reads_existing_returns_without_fetching(tmp_path, monkeypatch):
    """A benchmark the session database already has enough history for costs
    nothing - no scratch database, no network call.
    """
    db_path = str(tmp_path / "session.duckdb")
    _build_fixture_db(db_path, include_factors=False)
    ingest_spy = MagicMock()
    monkeypatch.setattr("agentic_portfolio.flow.interactive.validate_and_ingest_tickers", ingest_spy)

    source = prepare_benchmark("AAA", "USD", date(2024, 3, 1), db_path)

    assert ingest_spy.called is False
    assert source.unavailable_reason is None
    assert source.ticker == "AAA"
    assert len(source.monthly_returns) == BENCHMARK_MIN_MONTHS


def test_prepare_benchmark_fetches_into_a_scratch_database(tmp_path, monkeypatch):
    db_path = str(tmp_path / "session.duckdb")
    _build_fixture_db(db_path, include_factors=False)
    scratch_path = str(tmp_path / "scratch.duckdb")
    _fake_benchmark_fetch(monkeypatch, scratch_path, _bench_months())

    source = prepare_benchmark("SPY", "USD", date(2024, 3, 1), db_path)

    assert source.unavailable_reason is None
    assert source.currency == "USD"
    assert len(source.monthly_returns) == 24


def test_prepare_benchmark_never_writes_to_the_session_database(tmp_path, monkeypatch):
    """The structural guarantee: benchmark rows can never reach the database
    the portfolio's own returns window is derived from.
    """
    db_path = tmp_path / "session.duckdb"
    _build_fixture_db(str(db_path), include_factors=False)
    before_mtime = db_path.stat().st_mtime_ns
    scratch_path = str(tmp_path / "scratch.duckdb")
    _fake_benchmark_fetch(monkeypatch, scratch_path, _bench_months())

    prepare_benchmark("SPY", "USD", date(2024, 3, 1), str(db_path))

    con = duckdb.connect(str(db_path), read_only=True)
    try:
        assert con.execute("SELECT count(*) FROM returns WHERE ticker = 'SPY'").fetchone()[0] == 0
    finally:
        con.close()
    assert db_path.stat().st_mtime_ns == before_mtime


def test_prepare_benchmark_refuses_a_benchmark_in_another_currency(tmp_path, monkeypatch):
    db_path = str(tmp_path / "session.duckdb")
    _build_fixture_db(db_path, include_factors=False)
    _fake_benchmark_fetch(monkeypatch, str(tmp_path / "scratch.duckdb"), _bench_months(), currency="USD")

    source = prepare_benchmark("SPY", "JPY", date(2024, 3, 1), db_path)

    assert "USD" in source.unavailable_reason and "JPY" in source.unavailable_reason
    assert "exchange-rate" in source.unavailable_reason
    assert source.monthly_returns.empty


def test_prepare_benchmark_reports_an_unresolvable_benchmark_without_raising(tmp_path, monkeypatch):
    db_path = str(tmp_path / "session.duckdb")
    _build_fixture_db(db_path, include_factors=False)
    _fake_benchmark_fetch(
        monkeypatch, str(tmp_path / "scratch.duckdb"), [], invalid={"ZZZZ": "no price data found"}
    )

    source = prepare_benchmark("ZZZZ", "USD", date(2024, 3, 1), db_path)

    assert source.unavailable_reason == "no price data found"
    assert source.ticker == "ZZZZ"


def test_prepare_benchmark_survives_an_ingestion_exception(tmp_path, monkeypatch):
    """Losing a live session's fetched snapshot over a benchmark would cost
    far more than the benchmark is worth, so anything thrown becomes a line.
    """
    db_path = str(tmp_path / "session.duckdb")
    _build_fixture_db(db_path, include_factors=False)
    _fake_benchmark_fetch(
        monkeypatch, str(tmp_path / "scratch.duckdb"), [], raises=RuntimeError("yfinance exploded")
    )

    source = prepare_benchmark("SPY", "USD", date(2024, 3, 1), db_path)

    assert "yfinance exploded" in source.unavailable_reason
    assert source.ticker == "SPY"


def test_prepare_benchmark_with_no_ticker_points_at_the_benchmark_flag(tmp_path):
    source = prepare_benchmark(None, "SGD", date(2024, 3, 1), str(tmp_path / "absent.duckdb"))

    assert source.ticker is None
    assert "SGD" in source.unavailable_reason
    assert "--benchmark" in source.unavailable_reason


def test_prepare_benchmark_respects_allow_fetch_false(tmp_path, monkeypatch):
    db_path = str(tmp_path / "session.duckdb")
    _build_fixture_db(db_path, include_factors=False)
    ingest_spy = MagicMock()
    monkeypatch.setattr("agentic_portfolio.flow.interactive.validate_and_ingest_tickers", ingest_spy)

    source = prepare_benchmark("SPY", "USD", date(2024, 3, 1), db_path, allow_fetch=False)

    assert ingest_spy.called is False
    assert "--no-benchmark-fetch" in source.unavailable_reason


def test_run_pipeline_against_reports_the_benchmark_and_gives_it_no_weight(tmp_path):
    """The benchmark is reported over the portfolio's own window and is not a
    holding - it never appears among the candidates or the weights.
    """
    db_path = str(tmp_path / "fixture.duckdb")
    _build_fixture_db(db_path, include_factors=False)
    months = pd.date_range(end="2024-03-01", periods=24, freq="MS", name="rebalance_date")
    series = pd.Series([0.02 + (0.01 if i % 2 == 0 else -0.008) for i in range(24)], index=months, name="SPY")
    source = BenchmarkSource("SPY", "USD", series, None)

    result = run_pipeline_against(
        date(2024, 3, 1), "GMV", 1000.0, "user_provided", db_path, "backtest",
        candidates=["AAA"], benchmark=source,
    )

    benchmark = result["benchmark"]
    assert benchmark.unavailable_reason is None
    assert benchmark.ticker == "SPY"
    assert benchmark.window_months == result["stats"].returns_window_months
    assert benchmark.window_start == result["stats"].returns_window_start
    assert benchmark.window_end == result["stats"].returns_window_end
    assert benchmark.risk_free_rate == result["stats"].risk_free_rate
    assert "SPY" not in result["scan_detail"]["candidates"]
    assert "SPY" not in result["weights"]


def test_run_pipeline_against_without_a_benchmark_source_reports_none(tmp_path):
    """The library layer defaults to no benchmark, so no programmatic caller
    acquires a network fetch it did not ask for.
    """
    db_path = str(tmp_path / "fixture.duckdb")
    _build_fixture_db(db_path, include_factors=False)

    result = run_pipeline_against(
        date(2024, 3, 1), "GMV", 1000.0, "user_provided", db_path, "backtest", candidates=["AAA"]
    )

    assert result["benchmark"] is None


def test_build_scratch_snapshot_yields_empty_tables_and_always_deletes_the_file():
    """`src/agentic_portfolio/flow/live.py`'s scratch database: usable immediately by the
    ingestion path, and gone afterwards even when the body raises.
    """
    with build_scratch_snapshot() as db_path:
        con = duckdb.connect(db_path)
        try:
            for table in ("prices", "unresolved_tickers", "returns", "ticker_currency"):
                assert con.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
        finally:
            con.close()
        held_path = db_path

    assert not Path(held_path).exists()

    with pytest.raises(RuntimeError):
        with build_scratch_snapshot() as db_path:
            failed_path = db_path
            raise RuntimeError("boom")

    assert not Path(failed_path).exists()


# ---------------------------------------------------------------------------
# The live snapshot builds dividends
# ---------------------------------------------------------------------------


def _stats_stub():
    """Minimal stand-in for a `PortfolioStats` where only `weights` is read."""
    stub = MagicMock()
    stub.weights = {"AAA": 1.0}
    return stub


def _patch_snapshot_builds(monkeypatch, dividends_raises: Exception | None = None):
    """Patch every `build_*` call `build_live_snapshot` makes, returning the
    dividend spy. No test here may fetch anything.
    """
    calls: list[str] = []

    def dividends(*_a, **_k):
        calls.append("dividends")
        if dividends_raises is not None:
            raise dividends_raises
        return None

    monkeypatch.setattr("agentic_portfolio.flow.live.write_membership_table", lambda *a, **k: None)
    monkeypatch.setattr("agentic_portfolio.flow.live.build_price_history", lambda *a, **k: calls.append("prices"))
    monkeypatch.setattr("agentic_portfolio.flow.live.build_factors", lambda *a, **k: calls.append("factors"))
    monkeypatch.setattr(
        "agentic_portfolio.flow.live.build_momentum_factors", lambda *a, **k: calls.append("momentum")
    )
    monkeypatch.setattr("agentic_portfolio.flow.live.build_returns", lambda *a, **k: calls.append("returns"))
    monkeypatch.setattr("agentic_portfolio.flow.live.build_dividends", dividends)
    monkeypatch.setattr(
        "agentic_portfolio.flow.live.fetch_and_normalize_membership",
        lambda *a, **k: (pd.DataFrame({"ticker": ["AAA"], "company": ["A"]}), pd.DataFrame()),
    )
    monkeypatch.setattr("agentic_portfolio.flow.live.apply_changes_asof", lambda cur, ch, d: cur)
    # Timestamps, not dates: `_causal_masking_date` calls `.date()` on them.
    monkeypatch.setattr(
        "agentic_portfolio.flow.live.compute_rebalance_dates",
        lambda *a, **k: [pd.Timestamp("2025-12-01")],
    )
    return calls


@pytest.mark.parametrize("selection", ["llm_s_only", "llm_f_only", "llm_s_and_f"])
def test_build_live_snapshot_builds_dividends_for_every_screened_selection(
    monkeypatch, selection
):
    """The gap this closed: a live screened run had no `dividends` table at
    all, so every candidate reported its dividend data as unavailable and a
    floor was refused for the whole pool - and no build command could reach
    it, because the snapshot is discarded when the run ends.
    """
    calls = _patch_snapshot_builds(monkeypatch)
    monkeypatch.setattr("agentic_portfolio.flow.live._copy_news_archive", lambda *a, **k: None)

    with build_live_snapshot(date(2026, 9, 8), selection, "data/portfolio.duckdb"):
        pass

    assert "dividends" in calls
    # After the prices it reads to know which tickers to fetch.
    assert calls.index("prices") < calls.index("dividends")


def test_build_live_snapshot_skips_dividends_when_the_fetch_is_declined(monkeypatch):
    calls = _patch_snapshot_builds(monkeypatch)

    with build_live_snapshot(
        date(2026, 9, 8), "llm_s_only", "data/portfolio.duckdb", allow_dividend_fetch=False
    ):
        pass

    assert "dividends" not in calls
    assert "prices" in calls


def test_build_live_snapshot_survives_a_failing_dividend_build(monkeypatch):
    """By this point the snapshot has cost minutes of fetching, so a
    rate-limited dividend batch must not discard it. The report already
    names a ticker whose dividend data is missing.
    """
    calls = _patch_snapshot_builds(monkeypatch, dividends_raises=RuntimeError("rate limited"))

    with build_live_snapshot(date(2026, 9, 8), "llm_s_only", "data/portfolio.duckdb") as db_path:
        assert db_path is not None

    assert "dividends" in calls
    assert "returns" in calls


def test_build_live_snapshot_user_provided_never_builds_dividends(monkeypatch):
    """That selection fetches per typed ticker through
    `validate_and_ingest_tickers` instead, so the universe build would be
    pure waste.
    """
    calls = _patch_snapshot_builds(monkeypatch)

    with build_live_snapshot(date(2026, 9, 8), "user_provided", "data/portfolio.duckdb"):
        pass

    assert calls == []


def test_init_empty_tables_creates_the_corporate_action_tables(tmp_path):
    """`user_provided` fills these in incrementally; the upserts would
    create them anyway, but omitting them from this list would suggest
    dividends are not part of that flow.
    """
    db = str(tmp_path / "scratch.duckdb")
    _init_empty_price_and_returns_tables(db)

    con = duckdb.connect(db, read_only=True)
    try:
        tables = {t[0] for t in con.execute(
            "SELECT table_name FROM information_schema.tables"
        ).fetchall()}
    finally:
        con.close()
    assert {"dividends", "splits", "dividend_coverage"} <= tables


def test_open_pipeline_session_passes_the_dividend_fetch_choice_to_the_snapshot(monkeypatch):
    seen: dict = {}

    @contextmanager
    def snapshot_spy(as_of, selection, source_db_path, allow_dividend_fetch=True):
        seen["allow"] = allow_dividend_fetch
        yield "snapshot.duckdb"

    monkeypatch.setattr("agentic_portfolio.flow.interactive.build_live_snapshot", snapshot_spy)

    with open_pipeline_session(
        date(2026, 9, 8), "llm_s_only", "data/portfolio.duckdb", allow_dividend_fetch=False
    ):
        pass

    assert seen["allow"] is False


def test_compute_weights_and_allocation_can_skip_consulting_dividends(monkeypatch):
    """`None` means "not consulted", which is a different report line from
    `{}` meaning "consulted, nothing found" - so the flag must land on the
    first, not imply a lookup that never happened.
    """
    figures_spy = MagicMock()
    monkeypatch.setattr("agentic_portfolio.flow.interactive.load_dividend_figures", figures_spy)
    stats_spy = MagicMock(return_value=_stats_stub())
    monkeypatch.setattr("agentic_portfolio.flow.interactive.compute_weights_and_stats", stats_spy)
    monkeypatch.setattr("agentic_portfolio.flow.interactive._require_single_currency", lambda *a, **k: None)
    monkeypatch.setattr(
        "agentic_portfolio.flow.interactive.load_returns_matrix",
        lambda *a, **k: pd.DataFrame({"AAA": [0.01] * 24}),
    )
    monkeypatch.setattr(
        "agentic_portfolio.flow.interactive.load_latest_prices", lambda *a, **k: pd.Series({"AAA": 10.0})
    )
    monkeypatch.setattr("agentic_portfolio.flow.interactive.allocate_shares", lambda *a, **k: ({}, 0.0))

    compute_weights_and_allocation(
        ["AAA"], "GMV", 1000.0, date(2026, 9, 8), "db.duckdb", consult_dividends=False
    )

    figures_spy.assert_not_called()
    assert stats_spy.call_args.kwargs["dividend_yields"] is None
    assert stats_spy.call_args.kwargs["dividends_per_share"] is None


def test_compute_weights_and_allocation_forwards_the_requested_lookback(monkeypatch):
    returns_spy = MagicMock(return_value=pd.DataFrame({"AAA": [0.01] * 12}))
    monkeypatch.setattr("agentic_portfolio.flow.interactive.load_returns_matrix", returns_spy)
    monkeypatch.setattr(
        "agentic_portfolio.flow.interactive._require_single_currency", lambda *a, **k: None
    )
    monkeypatch.setattr(
        "agentic_portfolio.flow.interactive.load_dividend_figures",
        lambda *a, **k: ({}, {}, {}, {}),
    )
    monkeypatch.setattr(
        "agentic_portfolio.flow.interactive.compute_weights_and_stats",
        lambda *a, **k: _stats_stub(),
    )
    monkeypatch.setattr(
        "agentic_portfolio.flow.interactive.load_latest_prices",
        lambda *a, **k: pd.Series({"AAA": 10.0}),
    )
    monkeypatch.setattr(
        "agentic_portfolio.flow.interactive.allocate_shares", lambda *a, **k: ({}, 0.0)
    )

    compute_weights_and_allocation(
        ["AAA"],
        "GMV",
        1000.0,
        date(2026, 9, 8),
        "db.duckdb",
        lookback_months=12,
    )

    assert returns_spy.call_args.kwargs["lookback_months"] == 12
    assert returns_spy.call_args.kwargs["min_months"] == 12


# ---------------------------------------------------------------------------
# An unsatisfiable optimization is reported, not raised
# ---------------------------------------------------------------------------


def test_run_pipeline_against_reports_an_unsatisfiable_request_and_keeps_the_scan(monkeypatch):
    """Returned rather than raised, because `run_scan` above it has already
    run the LLM agents and those must not be asked twice. Everything the
    screening produced has to survive so the caller can offer a corrected
    retry without paying for it again.
    """
    scan_calls: list[int] = []

    def scan(*_a, **_k):
        scan_calls.append(1)
        return {
            "rule": None,
            "llm_s_signals": None,
            "llm_f_signals": None,
            "scan_detail": {
                "branch": "b", "buy_s_size": 1, "buy_f_size": 0,
                "intersection_size": 0, "union_size": 1, "candidates": ["AAA", "BBB"],
            },
        }

    monkeypatch.setattr("agentic_portfolio.flow.interactive.run_scan", scan)
    # The pipeline now loads the matrix itself, to know the window the
    # benchmark has to be measured over.
    monkeypatch.setattr(
        "agentic_portfolio.flow.interactive.load_returns_matrix", lambda *a, **k: _income_matrix()
    )
    monkeypatch.setattr("agentic_portfolio.flow.interactive._benchmark_over_matrix", lambda *a, **k: None)
    monkeypatch.setattr(
        "agentic_portfolio.flow.interactive.compute_weights_and_allocation",
        MagicMock(side_effect=DividendFloorError("floor 0.0300 is unreachable")),
    )
    benchmark_spy = MagicMock()
    monkeypatch.setattr("agentic_portfolio.flow.interactive.benchmark_stats_for_window", benchmark_spy)

    result = run_pipeline_against(
        date(2026, 9, 8), "MV", 100000.0, "llm_s_only", "db.duckdb", "live"
    )

    assert isinstance(result["unsatisfiable"], DividendFloorError)
    # Every figure None together, the discipline BenchmarkStats already uses.
    assert result["stats"] is None
    assert result["weights"] is None
    assert result["allocation"] is None
    assert result["benchmark"] is None
    # The expensive part survived, and was paid for exactly once.
    assert result["scan_detail"]["candidates"] == ["AAA", "BBB"]
    assert scan_calls == [1]
    # No benchmark fetched: there is no returns window to narrow it to.
    benchmark_spy.assert_not_called()


def test_run_pipeline_against_marks_a_satisfiable_run_as_unsatisfiable_none(monkeypatch):
    """So a caller reads one field either way rather than probing for a key."""
    monkeypatch.setattr(
        "agentic_portfolio.flow.interactive.load_returns_matrix", lambda *a, **k: _income_matrix()
    )
    monkeypatch.setattr("agentic_portfolio.flow.interactive._benchmark_over_matrix", lambda *a, **k: None)
    monkeypatch.setattr(
        "agentic_portfolio.flow.interactive.run_scan",
        lambda *a, **k: {
            "rule": None, "llm_s_signals": None, "llm_f_signals": None,
            "scan_detail": {"branch": "b", "buy_s_size": 1, "buy_f_size": 0,
                            "intersection_size": 0, "union_size": 1, "candidates": ["AAA"]},
        },
    )
    stats = _stats_stub()
    stats.returns_window_start = date(2021, 9, 1)
    stats.returns_window_end = date(2026, 9, 1)
    monkeypatch.setattr(
        "agentic_portfolio.flow.interactive.compute_weights_and_allocation",
        lambda *a, **k: (stats, ({}, 0.0)),
    )
    monkeypatch.setattr("agentic_portfolio.flow.interactive.benchmark_stats_for_window", lambda *a, **k: None)

    result = run_pipeline_against(
        date(2026, 9, 8), "GMV", 100000.0, "llm_s_only", "db.duckdb", "live"
    )

    assert result["unsatisfiable"] is None
    assert result["stats"] is stats


def test_run_pipeline_against_still_raises_anything_that_is_not_unsatisfiable(monkeypatch):
    """The narrow catch must not become a catch-all - a genuine bug has to
    keep escaping.
    """
    monkeypatch.setattr(
        "agentic_portfolio.flow.interactive.load_returns_matrix", lambda *a, **k: _income_matrix()
    )
    monkeypatch.setattr("agentic_portfolio.flow.interactive._benchmark_over_matrix", lambda *a, **k: None)
    monkeypatch.setattr(
        "agentic_portfolio.flow.interactive.run_scan",
        lambda *a, **k: {
            "rule": None, "llm_s_signals": None, "llm_f_signals": None,
            "scan_detail": {"branch": "b", "buy_s_size": 1, "buy_f_size": 0,
                            "intersection_size": 0, "union_size": 1, "candidates": ["AAA"]},
        },
    )
    monkeypatch.setattr(
        "agentic_portfolio.flow.interactive.compute_weights_and_allocation",
        MagicMock(side_effect=ValueError("objective must be one of ...")),
    )

    with pytest.raises(ValueError) as excinfo:
        run_pipeline_against(date(2026, 9, 8), "BOGUS", 100000.0, "llm_s_only", "db.duckdb", "live")
    assert not isinstance(excinfo.value, DividendFloorError)


# ---------------------------------------------------------------------------
# Clamping a derived target the pool cannot reach
# ---------------------------------------------------------------------------


def _income_matrix() -> pd.DataFrame:
    """Two tickers whose best expected return is well below any equity
    index's, which is the situation a benchmark-derived target collides
    with: a bond-and-preferred pool cannot reach what the market returned.
    """
    idx = pd.date_range("2021-10-01", periods=40, freq="MS", name="rebalance_date")
    low = [0.004 + (0.002 if i % 2 == 0 else -0.002) for i in range(40)]
    lower = [0.001 + (0.03 if i % 3 == 0 else -0.015) for i in range(40)]
    return pd.DataFrame({"STEADY": low, "SWINGY": lower}, index=idx)


def _resolved(target, origin="matching benchmark IDX's return"):
    return ResolvedObjective("MV", target, origin)


def test_a_derived_target_is_clamped_to_what_the_pool_can_reach(monkeypatch, tmp_path):
    db = str(tmp_path / "s.duckdb")
    matrix = _income_matrix()
    ceiling = max(compute_weights_and_stats(matrix, "GMV").expected_returns.values())

    monkeypatch.setattr("agentic_portfolio.flow.interactive._require_single_currency", lambda *a, **k: None)
    monkeypatch.setattr(
        "agentic_portfolio.flow.interactive.load_latest_prices",
        lambda tickers, **k: pd.Series({t: 100.0 for t in tickers}),
    )
    monkeypatch.setattr("agentic_portfolio.flow.interactive.allocate_shares", lambda *a, **k: ({}, 0.0))
    monkeypatch.setattr("agentic_portfolio.flow.interactive.load_dividend_figures", lambda *a, **k: ({}, {}, {}, {}))

    stats, _alloc, resolved = _optimize_clamping_an_unreachable_target(
        ["STEADY", "SWINGY"], _resolved(0.90), 100000.0, date(2026, 9, 8), db,
        risk_free_rate=0.02, dividend_floor=None, consult_dividends=False,
        returns_matrix=matrix,
    )

    assert resolved.clamped_from == pytest.approx(0.90)
    assert resolved.target_annual_return < ceiling
    assert resolved.target_annual_return == pytest.approx(ceiling, rel=1e-3)
    assert stats.portfolio_expected_return == pytest.approx(ceiling, rel=1e-3)


def test_clamping_to_the_unscaled_ceiling_would_be_refused(monkeypatch):
    """Why the clamp scales by `1 - CLAMP_EPSILON` rather than using the
    ceiling directly: `efficient_return` compares against its own
    `_max_return_value`, and a ceiling taken from `max(mu)` sits a float's
    width above it. Measured on real data as a target of 0.0544 refused
    against a stated maximum of 0.0544.
    """
    matrix = _income_matrix()
    ceiling = max(compute_weights_and_stats(matrix, "GMV").expected_returns.values())

    with pytest.raises(UnreachableTargetReturnError) as excinfo:
        compute_weights_and_stats(matrix, "MV", target_annual_return=ceiling)

    # And the solver's own figure, scaled, does solve.
    assert excinfo.value.reachable is not None
    compute_weights_and_stats(
        matrix, "MV", target_annual_return=excinfo.value.reachable * (1 - CLAMP_EPSILON)
    )


def test_a_target_the_user_typed_is_never_clamped(monkeypatch, tmp_path):
    """Silently moving a number somebody chose would be worse than telling
    them it cannot be met - which `origin` is what distinguishes.
    """
    monkeypatch.setattr("agentic_portfolio.flow.interactive._require_single_currency", lambda *a, **k: None)
    monkeypatch.setattr("agentic_portfolio.flow.interactive.load_dividend_figures", lambda *a, **k: ({}, {}, {}, {}))

    with pytest.raises(UnreachableTargetReturnError):
        _optimize_clamping_an_unreachable_target(
            ["STEADY", "SWINGY"], _resolved(0.90, "--target-return, which only MV consumes"),
            100000.0, date(2026, 9, 8), str(tmp_path / "s.duckdb"),
            risk_free_rate=0.02, dividend_floor=None, consult_dividends=False,
            returns_matrix=_income_matrix(),
        )


def test_the_concentration_note_fires_on_a_clamped_single_holding_result():
    """The clamp is reachable by typing no flags at all, so a default must
    never hand somebody one holding silently.
    """
    matrix = _income_matrix()
    ceiling = max(compute_weights_and_stats(matrix, "GMV").expected_returns.values())
    stats = compute_weights_and_stats(
        matrix, "MV", target_annual_return=ceiling * (1 - CLAMP_EPSILON)
    )

    note = _concentration_note(ResolvedObjective('MV', ceiling, 'derived', clamped_from=0.90), stats, matrix, 0.02)
    assert note is not None
    assert "sits at the pool's ceiling" in note
    assert "GMV over the same pool returns" in note


def test_the_concentration_note_is_silent_without_a_clamp():
    matrix = _income_matrix()
    stats = compute_weights_and_stats(matrix, "GMV")
    assert _concentration_note(_resolved(None), stats, matrix, 0.02) is None


def test_the_concentration_note_is_silent_for_a_diversified_clamped_result():
    """Even a clamped target is fine when the result still spreads the money
    - the note is about concentration, not about clamping.

    Needs a fixture whose GMV is genuinely diversified: `_income_matrix`'s
    near-riskless STEADY makes its own GMV 98% one holding, so three
    tickers of comparable variance are used here instead.
    """
    idx = pd.date_range("2021-10-01", periods=40, freq="MS", name="rebalance_date")
    matrix = pd.DataFrame(
        {
            "A": [0.01 + (0.04 if i % 2 == 0 else -0.04) for i in range(40)],
            "B": [0.01 + (0.04 if i % 3 == 0 else -0.02) for i in range(40)],
            "C": [0.01 + (0.04 if i % 5 == 0 else -0.01) for i in range(40)],
        },
        index=idx,
    )
    stats = compute_weights_and_stats(matrix, "GMV")
    assert max(stats.weights.values()) <= CONCENTRATION_THRESHOLD, stats.weights

    resolved = ResolvedObjective("MV", 0.03, "derived", clamped_from=0.9)
    assert _concentration_note(resolved, stats, matrix, 0.02) is None


# ---------------------------------------------------------------------------
# prepare_ticker_summary
# ---------------------------------------------------------------------------
#
# The structural guarantee under test here is the same one
# `prepare_benchmark`'s tests assert: rows fetched to describe a ticker can
# never reach the database the portfolio's own returns window is derived
# from. `_load_window_dates` runs `SELECT DISTINCT rebalance_date FROM
# returns` over the WHOLE table, so a summarized ticker with longer history
# than the pool could otherwise widen the window the portfolio is measured
# over - meaning that merely LOOKING at a candidate would change the numbers
# printed for the portfolio.


def _fake_profile_fetch(monkeypatch, quote_type: str = "ETF") -> MagicMock:
    spy = MagicMock(
        side_effect=lambda ticker, **kw: RawTickerProfile(
            info={"quoteType": quote_type, "longName": f"{ticker} Fund", "currency": "USD"},
            fund_profile=None,
            fund_performance=None,
            fund_data_reason="no fund data in this test",
        )
    )
    monkeypatch.setattr("agentic_portfolio.flow.interactive.fetch_ticker_profile", spy)
    return spy


def test_prepare_ticker_summary_reads_a_pool_ticker_from_the_session_database(
    tmp_path, monkeypatch
):
    """A ticker already in the pool was ingested by the add that put it
    there, so summarizing it must cost no scratch database and no ingestion
    call at all."""
    db_path = str(tmp_path / "session.duckdb")
    _insert_returns(db_path, "AAA", _bench_months(36))
    _fake_profile_fetch(monkeypatch)
    ingest_spy = MagicMock()
    monkeypatch.setattr("agentic_portfolio.flow.interactive.validate_and_ingest_tickers", ingest_spy)

    summary = prepare_ticker_summary(
        "AAA", date(2024, 3, 1), ["AAA"], db_path, rates_path=None
    )

    assert ingest_spy.called is False
    assert summary.stats.unavailable_reason is None
    assert summary.stats.window_months == 36
    assert summary.profile.long_name == "AAA Fund"


def test_prepare_ticker_summary_uses_the_requested_returns_window(tmp_path, monkeypatch):
    db_path = str(tmp_path / "session.duckdb")
    _insert_returns(db_path, "AAA", _bench_months(36))
    _fake_profile_fetch(monkeypatch)

    summary = prepare_ticker_summary(
        "AAA",
        date(2024, 3, 1),
        ["AAA"],
        db_path,
        rates_path=None,
        lookback_months=12,
    )

    assert summary.stats.unavailable_reason is None
    assert summary.stats.window_months == 12


def test_prepare_ticker_summary_ingests_a_non_pool_ticker_into_a_scratch_database(
    tmp_path, monkeypatch
):
    db_path = str(tmp_path / "session.duckdb")
    _insert_returns(db_path, "AAA", _bench_months(36))
    scratch_path = str(tmp_path / "scratch.duckdb")
    _fake_benchmark_fetch(monkeypatch, scratch_path, _bench_months(36))
    _fake_profile_fetch(monkeypatch)

    summary = prepare_ticker_summary(
        "SPY", date(2024, 3, 1), ["AAA"], db_path, rates_path=None
    )

    assert summary.stats.unavailable_reason is None
    assert summary.stats.window_months == 36
    assert summary.stats.currency == "USD"


def test_prepare_ticker_summary_never_writes_to_the_session_database(tmp_path, monkeypatch):
    """The structural guarantee. A summarized ticker's rows must not be able
    to move the window the portfolio is measured over."""
    db_path = tmp_path / "session.duckdb"
    _insert_returns(str(db_path), "AAA", _bench_months(36))
    before_mtime = db_path.stat().st_mtime_ns
    scratch_path = str(tmp_path / "scratch.duckdb")
    _fake_benchmark_fetch(monkeypatch, scratch_path, _bench_months(60))
    _fake_profile_fetch(monkeypatch)

    prepare_ticker_summary("SPY", date(2024, 3, 1), ["AAA"], str(db_path), rates_path=None)

    assert db_path.stat().st_mtime_ns == before_mtime
    rows = duckdb.connect(str(db_path), read_only=True).execute(
        "SELECT DISTINCT ticker FROM returns"
    ).fetchall()
    assert [r[0] for r in rows] == ["AAA"]


def test_prepare_ticker_summary_reports_an_unresolvable_symbol_by_name(tmp_path, monkeypatch):
    db_path = str(tmp_path / "session.duckdb")
    _insert_returns(db_path, "AAA", _bench_months(36))
    scratch_path = str(tmp_path / "scratch.duckdb")
    _fake_benchmark_fetch(
        monkeypatch, scratch_path, [], invalid={"ZZZZQQQ": "no price data in range"}
    )
    _fake_profile_fetch(monkeypatch)

    summary = prepare_ticker_summary(
        "ZZZZQQQ", date(2024, 3, 1), ["AAA"], db_path, rates_path=None
    )

    assert summary.stats.unavailable_reason == "no price data in range"
    assert summary.stats.annual_return is None


def test_prepare_ticker_summary_survives_an_ingestion_failure(tmp_path, monkeypatch):
    """Losing a live session's fetched snapshot over a summary would cost far
    more than the summary is worth."""
    db_path = str(tmp_path / "session.duckdb")
    _insert_returns(db_path, "AAA", _bench_months(36))
    scratch_path = str(tmp_path / "scratch.duckdb")
    _fake_benchmark_fetch(monkeypatch, scratch_path, [], raises=RuntimeError("network down"))
    _fake_profile_fetch(monkeypatch)

    summary = prepare_ticker_summary(
        "SPY", date(2024, 3, 1), ["AAA"], db_path, rates_path=None
    )

    assert "could not measure SPY" in summary.stats.unavailable_reason
    assert "network down" in summary.stats.unavailable_reason
    # The Yahoo half is untouched.
    assert summary.profile.long_name == "SPY Fund"


def test_prepare_ticker_summary_resolves_and_names_the_risk_free_rate(tmp_path, monkeypatch):
    """The confirm loop runs before `_settle_risk_free_rate`, so a summary
    printed there resolves its own rate - and must say where it came from,
    because a bare plausible 0.0200 is indistinguishable from a deliberate
    choice."""
    db_path = str(tmp_path / "session.duckdb")
    _insert_returns(db_path, "AAA", _bench_months(36))
    _fake_profile_fetch(monkeypatch)
    rates_path = tmp_path / "rates.json"
    rates_path.write_text(
        '{"rates": {"USD": {"risk_free_rate": 0.045, "updated_at": "2026-01-01T00:00:00Z"}}}'
    )

    summary = prepare_ticker_summary(
        "AAA", date(2024, 3, 1), ["AAA"], db_path, rates_path=str(rates_path)
    )

    assert summary.stats.risk_free_rate == 0.045
    assert summary.risk_free_rate_origin == "remembered for USD"


def test_prepare_ticker_summary_passes_a_settled_rate_straight_through(tmp_path, monkeypatch):
    """The post-run edit loop already carries the rate the report used, and a
    summary printed under that report must agree with it rather than
    resolving a second one."""
    db_path = str(tmp_path / "session.duckdb")
    _insert_returns(db_path, "AAA", _bench_months(36))
    _fake_profile_fetch(monkeypatch)

    summary = prepare_ticker_summary(
        "AAA", date(2024, 3, 1), ["AAA"], db_path,
        risk_free_rate=0.005, risk_free_rate_origin="remembered for JPY",
        rates_path="never-read.json",
    )

    assert summary.stats.risk_free_rate == 0.005
    assert summary.risk_free_rate_origin == "remembered for JPY"


def test_prepare_ticker_summary_with_fetching_disabled_neither_fetches_nor_ingests(
    tmp_path, monkeypatch
):
    db_path = str(tmp_path / "session.duckdb")
    _insert_returns(db_path, "AAA", _bench_months(36))
    fetch_spy = _fake_profile_fetch(monkeypatch)
    ingest_spy = MagicMock()
    monkeypatch.setattr("agentic_portfolio.flow.interactive.validate_and_ingest_tickers", ingest_spy)

    summary = prepare_ticker_summary(
        "SPY", date(2024, 3, 1), ["AAA"], db_path, rates_path=None, allow_fetch=False
    )

    assert fetch_spy.called is False
    assert ingest_spy.called is False
    assert "fetching" in summary.profile.unavailable_reason
    assert "not in this pool" in summary.stats.unavailable_reason


def test_prepare_ticker_summary_caches_the_profile_across_calls(tmp_path, monkeypatch):
    db_path = str(tmp_path / "session.duckdb")
    _insert_returns(db_path, "AAA", _bench_months(36))
    fetch_spy = _fake_profile_fetch(monkeypatch)
    cache = {}

    for _ in range(3):
        prepare_ticker_summary(
            "AAA", date(2024, 3, 1), ["AAA"], db_path, rates_path=None, profile_cache=cache
        )

    assert fetch_spy.call_count == 1
    assert set(cache) == {"AAA"}


def test_prepare_ticker_summary_upper_cases_and_strips_the_typed_symbol(tmp_path, monkeypatch):
    """The `[s]ummary` prompt reads free text, and every ticker in this
    project is stored upper-cased."""
    db_path = str(tmp_path / "session.duckdb")
    _insert_returns(db_path, "AAA", _bench_months(36))
    _fake_profile_fetch(monkeypatch)

    summary = prepare_ticker_summary(
        "  aaa  ", date(2024, 3, 1), ["AAA"], db_path, rates_path=None
    )

    assert summary.stats.ticker == "AAA"
    assert summary.stats.unavailable_reason is None
