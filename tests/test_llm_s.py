"""Tests for src/agents/condition_eval.py, src/agents/llm_s_apply.py,
src/agents/llm_s_signals.py, and the 4 tools in
src/agents/llm_s_crew/tools.py.

Per AGENTS.md, no test here calls an LLM (rule *generation* needs a real
LLM call and is exercised manually, per plans/02_llm_s_agent.md's
Concrete Steps, not here) - every function under test here is pure
Python/DataFrame logic against hand-written fixtures.
"""

from datetime import date

import duckdb
import pandas as pd
import pytest

from src.agents.condition_eval import evaluate_condition
from src.agents.llm_s_apply import apply_rule
from src.agents.llm_s_crew.tools import (
    GetExtremeFirmsTool,
    QueryFirmDatabaseTool,
    TestComplexConditionTool,
)
from src.agents.llm_s_schema import ScreeningRule
from src.agents.llm_s_signals import screen


def test_evaluate_condition_returns_correct_boolean():
    assert evaluate_condition("bm > 0.5", {"mve": 0, "bm": 1.0, "mom12m": 0}) is True
    assert evaluate_condition("bm > 0.5", {"mve": 0, "bm": 0.1, "mom12m": 0}) is False


def test_evaluate_condition_rejects_disallowed_name():
    with pytest.raises(ValueError, match="pe_ratio"):
        evaluate_condition("pe_ratio > 10", {"mve": 0, "bm": 0, "mom12m": 0})


def test_evaluate_condition_rejects_unsafe_syntax():
    """Proves the ast-based evaluator actually rejects arbitrary code
    rather than merely happening not to break on the happy path.
    """
    with pytest.raises(ValueError):
        evaluate_condition("__import__('os').system('echo hi')", {"mve": 0, "bm": 0, "mom12m": 0})


def _rule(buy_condition: str, sell_condition: str) -> ScreeningRule:
    return ScreeningRule(year=2024, buy_condition=buy_condition, sell_condition=sell_condition, rationale="test")


def test_apply_rule_buy_sell_hold():
    rule = _rule(buy_condition="bm > 0.5", sell_condition="mom12m < -0.5")
    assert apply_rule(rule, {"mve": 0, "bm": 1.0, "mom12m": 0}) == "buy"
    assert apply_rule(rule, {"mve": 0, "bm": 0.0, "mom12m": -1.0}) == "sell"
    assert apply_rule(rule, {"mve": 0, "bm": 0.0, "mom12m": 0.0}) == "hold"


def test_screen_excludes_null_rows_and_applies_rule(tmp_path):
    """A hand-built fixture `factors` table, one rebalance date, one row
    with a null mve_z - screen must exclude that row entirely, not impute
    or default it to "hold", and must correctly signal the rest.
    """
    db_path = str(tmp_path / "fixture.duckdb")
    con = duckdb.connect(db_path)
    con.execute(
        "CREATE TABLE factors (rebalance_date DATE, ticker VARCHAR, "
        "mve DOUBLE, bm DOUBLE, mom12m DOUBLE, mve_z DOUBLE, bm_z DOUBLE, mom12m_z DOUBLE)"
    )
    con.execute(
        "INSERT INTO factors VALUES "
        "('2024-03-01', 'AAA', 0, 0, 0, 0.0, 1.0, 0.5), "
        "('2024-03-01', 'BBB', 0, 0, 0, 0.0, -1.0, -0.7), "
        "('2024-03-01', 'CCC', 0, 0, 0, 0.0, 0.0, 0.0), "
        "('2024-03-01', 'DDD', 0, 0, 0, NULL, 1.0, 0.5)"
    )
    con.close()

    rule = _rule(buy_condition="bm > 0.4", sell_condition="mom12m < -0.5")
    result = screen(rule, date(2024, 3, 1), db_path=db_path)

    assert set(result["ticker"]) == {"AAA", "BBB", "CCC"}
    assert dict(zip(result["ticker"], result["signal"])) == {"AAA": "buy", "BBB": "sell", "CCC": "hold"}


def _snapshot_fixture() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "ticker": ["AAA", "BBB", "CCC", "DDD", "EEE"],
            "mve": [1.0, -1.0, 0.2, -0.3, 2.0],
            "bm": [0.5, -0.5, 1.2, -1.2, 0.0],
            "mom12m": [0.1, -0.1, 0.9, -0.9, 0.3],
        }
    )


def test_query_firm_database_tool_respects_sort_by_and_limit():
    tool = QueryFirmDatabaseTool(snapshot=_snapshot_fixture())
    rows = tool.run(sort_by="bm", ascending=False, limit=2)
    assert [row["ticker"] for row in rows] == ["CCC", "AAA"]


def test_query_firm_database_tool_looks_up_specific_tickers():
    tool = QueryFirmDatabaseTool(snapshot=_snapshot_fixture())
    rows = tool.run(tickers=["BBB", "ZZZ"])
    assert [row["ticker"] for row in rows] == ["BBB"]


def test_get_extreme_firms_tool_returns_highest_and_lowest():
    tool = GetExtremeFirmsTool(snapshot=_snapshot_fixture())
    result = tool.run(characteristic="mom12m", n=1)
    assert result["highest"][0]["ticker"] == "CCC"
    assert result["lowest"][0]["ticker"] == "DDD"


def test_test_complex_condition_tool_valid_condition():
    tool = TestComplexConditionTool(snapshot=_snapshot_fixture())
    result = tool.run(condition="bm > 0")
    assert result["matching_count"] == 2
    assert result["total_firms"] == 5
    assert set(result["sample_tickers"]) == {"AAA", "CCC"}


def test_test_complex_condition_tool_malformed_condition_returns_error_string():
    """A malformed condition during exploration is a normal, expected
    event the agent should see and correct - not a raised exception.
    """
    tool = TestComplexConditionTool(snapshot=_snapshot_fixture())
    result = tool.run(condition="pe_ratio > 10")
    assert isinstance(result, str)
    assert "pe_ratio" in result
