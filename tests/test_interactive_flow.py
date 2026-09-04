"""Tests for src/flow/interactive.py's `edit_candidates` and `run_pipeline`
selection-mode branching, and src/flow/backtest.py's turnover-cost/gross-
return/Sharpe-ratio arithmetic.

Per AGENTS.md, no test here calls yfinance's live API, any LLM, or hits
`data/portfolio.duckdb`. `run_pipeline`'s selection-mode tests exercise
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
calls by design (see `src/flow/live.py`), verified instead by the real,
manual runs recorded in `plans/06_interactive_flow.md`.
"""

from datetime import date
from unittest.mock import MagicMock

import duckdb
import pandas as pd
import pytest

from src.agents.llm_s_schema import ScreeningRule
from src.flow.backtest import _gross_return, _turnover_cost, compute_sharpe_ratio
from src.flow.interactive import edit_candidates, run_pipeline

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
    monkeypatch.setattr("src.flow.interactive.generate_rule", lambda year, model=None, db_path=None: fake_rule)
    monkeypatch.setattr("src.flow.interactive.screen_month", screen_month_spy)

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
    monkeypatch.setattr("src.flow.interactive.generate_rule", generate_rule_spy)
    monkeypatch.setattr("src.flow.interactive.screen_month", lambda year, month, db_path=None: fake_signals)

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
    monkeypatch.setattr("src.flow.interactive.generate_rule", lambda year, model=None, db_path=None: fake_rule)
    monkeypatch.setattr("src.flow.interactive.screen_month", lambda year, month, db_path=None: fake_signals)

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
# src/flow/backtest.py's turnover-cost / gross-return / Sharpe-ratio arithmetic
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
    from src.config.settings import settings

    net_returns = pd.Series([0.01, 0.02, -0.005])
    expected = (net_returns.mean() - settings.risk_free_rate / 12) / net_returns.std() * (12**0.5)

    assert compute_sharpe_ratio(net_returns) == pytest.approx(expected)
