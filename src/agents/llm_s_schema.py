"""Structured output schema for the LLM-S fundamentals screening agent.

`ScreeningRule` is what one `generate_rule` call (src/agents/llm_s.py)
produces: a year, a buy condition, a sell condition, and the agent's
rationale for both. `buy_condition`/`sell_condition` are boolean
expressions over `mve`, `bm`, `mom12m` — the paper's own variable names
(arXiv:2603.23300v1, Appendix C.5) for the already cross-sectionally
standardized (mean 0, variance 1) factors, not a `_z`-suffixed naming
scheme, since the prompt shown to the agent (agents.yaml/tasks.yaml under
src/agents/llm_s/config/) is kept verbatim from the paper and never
mentions a `_z` suffix. Syntax/name validation of the condition strings
happens in src/agents/condition_eval.py, not here — this model only
captures the shape of the agent's structured output.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ScreeningRule(BaseModel):
    """One year's LLM-S screening rule, as produced by a single
    `generate_rule` call. `year` is the calendar year the rule applies to,
    not the year of the causally-masked snapshot it was derived from
    (which is December of `year - 1`, per src/agents/llm_s.py).
    """

    year: int = Field(description="The calendar year this rule is intended to screen (e.g. 2024).")
    buy_condition: str = Field(
        description=(
            "Boolean expression over mve, bm, mom12m (using >, <, >=, <=, and, or, not, and "
            "numeric literals) that signals BUY when true, e.g. "
            "'bm > 0.4 and mom12m > -0.1 and mve > 0.3'."
        )
    )
    sell_condition: str = Field(
        description=(
            "Boolean expression over mve, bm, mom12m, same syntax as buy_condition, that "
            "signals SELL when true, e.g. 'bm < -0.3 or mom12m < -0.6 or mve < -0.8'."
        )
    )
    rationale: str = Field(
        description=(
            "The agent's plain-English explanation of the economic intuition behind both "
            "conditions and the data patterns (extremes, clustering, correlations) it observed "
            "while exploring the causally-masked snapshot."
        )
    )
