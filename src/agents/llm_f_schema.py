"""Structured output schema for the LLM-F news sentiment screening agent.

`SentimentSignal` is what one `generate_signal` call (src/agents/llm_f.py)
produces: a buy/sell/hold judgment call for a single ticker and month,
derived from that month's headlines, with a confidence level and a
rationale. This is deliberately a holistic judgment by the LLM rather than
a decomposition into positive/negative probability scores the way FinBERT
itself would produce them, per README.md's framing of LLM-F as replacing
FinBERT's role rather than reimplementing FinBERT's specific mechanism.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class SentimentSignal(BaseModel):
    """One ticker/month's LLM-F sentiment screening result, as produced by
    a single `generate_signal` call.
    """

    ticker: str = Field(description="The stock ticker this signal applies to, e.g. 'AAPL'.")
    month: str = Field(description="The ISO 'YYYY-MM' month this signal applies to, e.g. '2024-03'.")
    signal: Literal["buy", "sell", "hold"] = Field(
        description="The agent's aggregate buy/sell/hold call based on that month's headlines."
    )
    confidence: float = Field(description="The agent's confidence in the signal, from 0 to 1.")
    rationale: str = Field(
        description=(
            "The agent's plain-English explanation of the call, grounded in the specific "
            "headline content it read for this ticker and month."
        )
    )
