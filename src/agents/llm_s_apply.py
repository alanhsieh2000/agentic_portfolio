"""Deterministic application of an already-generated `ScreeningRule` to a
single stock's factor values.

This is the cheap, 100%-deterministic half of LLM-S (see
plans/02_llm_s_agent.md's Decision Log): no LLM call, just evaluating two
boolean expressions an LLM already produced against one row of numbers.
"""

from __future__ import annotations

from src.agents.condition_eval import evaluate_condition
from src.agents.llm_s_schema import ScreeningRule


def apply_rule(rule: ScreeningRule, factor_row: dict) -> str:
    """`"buy"` if `rule.buy_condition` holds for `factor_row` (keys
    `mve`, `bm`, `mom12m`), else `"sell"` if `rule.sell_condition` holds,
    else `"hold"`. Buy is checked first, so a row matching both an
    LLM-produced buy and sell condition (a rule the LLM should not have
    produced, but nothing stops it from doing so) resolves to buy rather
    than silently picking whichever branch happened to be checked last.
    """
    if evaluate_condition(rule.buy_condition, factor_row):
        return "buy"
    if evaluate_condition(rule.sell_condition, factor_row):
        return "sell"
    return "hold"
