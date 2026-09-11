"""Structured output schema for the monthly report summarizer's prose pass.

The division of labour this schema enforces is the point of it.
`src/flow/report_summary.py` computes every figure a monthly briefing
contains - the rankings, the gaps, the window-sensitivity readings - from
the facts saved in the report archive. The language model is then handed
those figures already computed and asked for one thing only: the prose
that says what they mean.

So every field below is prose. None of them carries a number the model is
expected to produce, and each field's description says so, because a
field description reaches the model as part of the output schema and is
therefore half of the instruction rather than documentation of it. The
other half is `config/agents.yaml`'s backstory, and the third is
mechanical: `src/agents/report_summary.py`'s `verify_narrative` replaces
any field containing a figure that was not in the facts it was given.

This mirrors `src/agents/llm_f_schema.py`, where the model estimates a
per-headline probability and `compute_decayed_score` does all of the
arithmetic - the LLM never makes the call that the numbers make.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

#: The text a field must contain, and contain alone, when the facts it was
#: given say its section was not available. Asking for a fixed token rather
#: than an empty string means a model that ignored the instruction is visible
#: rather than indistinguishable from one that complied.
NOT_APPLICABLE = "NOT APPLICABLE"


class MonthNarrative(BaseModel):
    """One month's briefing prose. Every field is sentences, never figures."""

    headline: str = Field(
        description=(
            "One sentence naming the single most useful thing this month's reports show. "
            "Prose only. Do not open with a figure and do not rank anything yourself - "
            "the ranking is already done and printed above your sentence."
        )
    )
    exploration_story: str = Field(
        description=(
            "Two to four sentences on what the person appears to have been exploring and "
            "in what order, based only on the sessions listed in the facts. Do not infer "
            "a motive the facts do not support."
        )
    )
    risk_return_read: str = Field(
        description=(
            "Two to four sentences on what the leaderboard means. Refer to portfolios by "
            "name, not by their figures - 'the highest-Sharpe run', never 'the 2.0207 run'. "
            "Never compare two portfolios that the facts place in different returns windows."
        )
    )
    income_read: str = Field(
        description=(
            "Two to three sentences on the dividend picture across the month's portfolios. "
            "Say nothing about an income figure that is not in the facts, and remember that "
            "yields on different portfolio sizes are not the same statistic as income."
        )
    )
    holdings_gap_read: str = Field(
        description=(
            "Two to four sentences on how the portfolio the person actually holds compares "
            f"with the best one their runs found. Write exactly '{NOT_APPLICABLE}' if the "
            "facts say no such comparison was available."
        )
    )
    consensus_read: str = Field(
        description=(
            "Two to four sentences on what it means that some tickers were held by every "
            "run and some candidates by none. Name only tickers the facts name. Write "
            f"exactly '{NOT_APPLICABLE}' if the facts say no consensus was available."
        )
    )
    methodology_caution: str = Field(
        description=(
            "Two to three sentences warning the reader about whatever the facts list under "
            "comparability - most importantly, any portfolio measured over more than one "
            "returns window. Explain in words why reading across those measurements would "
            "mislead. Write exactly "
            f"'{NOT_APPLICABLE}' if the facts report nothing to caution about."
        )
    )
    next_runs: list[str] = Field(
        description=(
            "Two to five suggestions, each one line: a command to run, then a dash, then "
            "one short clause saying what it would settle. Each command must be a real "
            "invocation of 'uv run portfolio' or 'uv run portfolio-holdings whatif' using "
            "flags that exist. Suggest only runs the facts give a reason for."
        )
    )
