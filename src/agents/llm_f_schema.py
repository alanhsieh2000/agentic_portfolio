"""Structured output schemas for the LLM-F news sentiment screening agent.

Per `plans/08_consistency_review.md` finding 5, LLM-F's mechanism now
matches README.md's Backtest Mode Stage 1 and Live Mode descriptions
literally: for one ticker/month, an LLM estimates a `positive_probability`
and `negative_probability` per headline (`HeadlineSentiment`, batched as
`HeadlineSentimentBatch` for one crew call), the same kind of per-headline
estimate a specialized sentiment classifier like FinBERT would produce.
`src/agents/llm_f.py`'s `compute_decayed_score` then combines those
per-headline estimates into one numeric `score` via an exponentially-
decreasing (toward month-end) weighted average of `positive_probability -
negative_probability`, and thresholds that score at +/-0.1 to produce
`SentimentSignal.signal` - the LLM never makes the buy/sell/hold call
itself; that is a mechanical function of the score, matching FinBERT's own
mechanism in the reference paper (arXiv:2603.23300, Section 5.2.1) rather
than a holistic LLM judgment call.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class HeadlineSentiment(BaseModel):
    """One headline's estimated sentiment, in isolation from every other
    headline. `index` identifies which input headline this is (its
    0-based position in the list of headlines given to the task), so
    `compute_decayed_score` can realign the LLM's output with the correct
    headline's publish date regardless of what order the LLM returns them
    in.
    """

    index: int = Field(description="0-based position of this headline in the input list given to the task.")
    positive_probability: float = Field(
        description="Probability, 0 to 1, that this headline alone conveys positive news for the stock."
    )
    negative_probability: float = Field(
        description="Probability, 0 to 1, that this headline alone conveys negative news for the stock."
    )


class HeadlineSentimentBatch(BaseModel):
    """One `sentiment_task` call's full output: exactly one
    `HeadlineSentiment` per headline given to the task.
    """

    headlines: list[HeadlineSentiment] = Field(
        description="Exactly one HeadlineSentiment per headline listed in the task, identified by index."
    )


class SentimentSignal(BaseModel):
    """One ticker/month's LLM-F sentiment screening result: the
    decay-weighted `score` computed by `compute_decayed_score`, and the
    `signal` mechanically derived from thresholding it at +/-0.1.
    """

    ticker: str = Field(description="The stock ticker this signal applies to, e.g. 'AAPL'.")
    month: str = Field(description="The ISO 'YYYY-MM' month this signal applies to, e.g. '2024-03'.")
    score: float = Field(
        description=(
            "Exponentially-decreasing (toward month-end) weighted average of "
            "(positive_probability - negative_probability) across that month's headlines."
        )
    )
    signal: Literal["buy", "sell", "hold"] = Field(
        description="'buy' if score > 0.1, 'sell' if score < -0.1, else 'hold'."
    )
