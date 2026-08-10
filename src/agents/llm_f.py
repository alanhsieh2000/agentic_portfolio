"""Entry point for producing one ticker/month's LLM-F sentiment signal.

`generate_signal` plays the same functional role in this project's
pipeline that FinBERT plays in the reference paper (arXiv:2603.23300,
Section 5.2.1): given a month's news headlines about one firm, decide
buy/sell/hold for that firm that month. Per `plans/08_consistency_review.md`
finding 5, this now matches README.md's Backtest Mode Stage 1 and Live
Mode descriptions literally, mirroring FinBERT's own mechanism rather than
a holistic LLM judgment call: the LLM (via `LLMFCrew`) estimates a
`positive_probability`/`negative_probability` per headline, in isolation;
`compute_decayed_score` then combines those into one numeric score as an
exponentially-decreasing (toward month-end, half-life `half_life_days`)
weighted average of `positive_probability - negative_probability`; and
`signal` is mechanically thresholded from that score at +/-0.1 - the LLM
never makes the buy/sell/hold call itself.
"""

from __future__ import annotations

import calendar
import logging
import os
from datetime import date

import pandas as pd

from src.agents.llm_f_crew.crew import LLMFCrew
from src.agents.llm_f_schema import HeadlineSentiment, SentimentSignal

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "anthropic/claude-sonnet-4-5"
BUY_THRESHOLD = 0.1
SELL_THRESHOLD = -0.1
HALF_LIFE_DAYS = 7.0


def _format_headlines(headlines: list[dict]) -> str:
    return "\n".join(f"- [{i}] ({h['publish_date']}) {h['title']}" for i, h in enumerate(headlines))


def _month_end(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


def compute_decayed_score(
    headlines: list[dict],
    sentiments: list[HeadlineSentiment],
    month_end: date,
    half_life_days: float = HALF_LIFE_DAYS,
) -> float:
    """Exponentially-decreasing (toward `month_end`) weighted average of
    `positive_probability - negative_probability` across `headlines`, one
    `HeadlineSentiment` expected per headline (matched by `.index`, not by
    list position, so the LLM's output order does not matter).

    A headline's weight is `0.5 ** (days_before_month_end / half_life_days)`
    - a standard half-life decay, 1.0 for a headline dated exactly on
    `month_end`, halving every `half_life_days` days further back.
    `days_before_month_end` is clamped to >= 0 (a headline dated after
    `month_end`, which should not occur given headlines are pre-filtered to
    the target month, is treated as no less stale than one dated exactly on
    `month_end`, rather than given more than full weight).

    Raises `ValueError` if `sentiments`'s indices are not exactly
    `{0, ..., len(headlines) - 1}` - a structural mismatch between what the
    task was given and what the LLM returned, not ordinary missing data.
    Returns 0.0 for an empty `headlines` list (nothing to score).
    """
    if not headlines:
        return 0.0

    by_index = {s.index: s for s in sentiments}
    expected_indices = set(range(len(headlines)))
    if set(by_index) != expected_indices:
        raise ValueError(
            f"expected exactly one HeadlineSentiment per headline (indices {sorted(expected_indices)}), "
            f"got indices {sorted(by_index)}"
        )

    weighted_sum = 0.0
    total_weight = 0.0
    for i, headline in enumerate(headlines):
        publish_date = pd.Timestamp(headline["publish_date"]).date()
        days_before_month_end = max((month_end - publish_date).days, 0)
        weight = 0.5 ** (days_before_month_end / half_life_days)
        sentiment = by_index[i]
        weighted_sum += weight * (sentiment.positive_probability - sentiment.negative_probability)
        total_weight += weight

    return weighted_sum / total_weight if total_weight else 0.0


def generate_signal(
    ticker: str, year: int, month: int, headlines: list[dict], model: str | None = None
) -> SentimentSignal:
    """Decide buy/sell/hold for `ticker` in `year`-`month` from `headlines`
    (as returned by `src.agents.news.fetch_headlines`). Makes no LLM call
    at all when `headlines` is empty — there is nothing to estimate a
    per-headline probability from, and calling an LLM anyway would either
    waste a call or invite it to fabricate sentiment from nothing.
    """
    month_str = f"{year:04d}-{month:02d}"

    if not headlines:
        return SentimentSignal(ticker=ticker, month=month_str, score=0.0, signal="hold")

    resolved_model = model or os.environ.get("LLM_F_MODEL", DEFAULT_MODEL)

    llm_f_crew = LLMFCrew(model=resolved_model)
    result = llm_f_crew.crew().kickoff(
        inputs={"ticker": ticker, "month": month_str, "headlines": _format_headlines(headlines)}
    )
    batch = result.pydantic

    score = compute_decayed_score(headlines, batch.headlines, month_end=_month_end(year, month))
    signal = "buy" if score > BUY_THRESHOLD else "sell" if score < SELL_THRESHOLD else "hold"

    return SentimentSignal(ticker=ticker, month=month_str, score=score, signal=signal)
