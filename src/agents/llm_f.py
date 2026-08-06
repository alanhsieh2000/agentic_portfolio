"""Entry point for producing one ticker/month's LLM-F sentiment signal.

`generate_signal` plays the same functional role in this project's
pipeline that FinBERT plays in the reference paper (arXiv:2603.23300,
Section 5.2.1): given a month's news headlines about one firm, decide
buy/sell/hold for that firm that month. The paper's FinBERT agent
computes this decision mechanically (positive-minus-negative FinBERT
probability, exponentially time-decayed toward month-end, thresholded at
+/-0.1); this project's LLM-F instead has a general-purpose LLM read the
actual headlines and make a holistic judgment call with a rationale,
matching README.md's framing of LLM-F as replacing FinBERT's role rather
than reimplementing FinBERT's specific mechanism (see
plans/03_llm_f_agent.md's Plan of Work).
"""

from __future__ import annotations

import logging
import os

from src.agents.llm_f_crew.crew import LLMFCrew
from src.agents.llm_f_schema import SentimentSignal

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "anthropic/claude-sonnet-4-5"


def _format_headlines(headlines: list[dict]) -> str:
    return "\n".join(f"- ({h['publish_date']}) {h['title']}" for h in headlines)


def generate_signal(
    ticker: str, year: int, month: int, headlines: list[dict], model: str | None = None
) -> SentimentSignal:
    """Decide buy/sell/hold for `ticker` in `year`-`month` from `headlines`
    (as returned by `src.agents.news.fetch_headlines`). Makes no LLM call
    at all when `headlines` is empty — there is nothing to reason about,
    and calling an LLM anyway would either waste a call or invite it to
    fabricate sentiment from nothing.
    """
    month_str = f"{year:04d}-{month:02d}"

    if not headlines:
        return SentimentSignal(
            ticker=ticker,
            month=month_str,
            signal="hold",
            confidence=0.0,
            rationale=f"No news headlines were found for {ticker} in {month_str}.",
        )

    resolved_model = model or os.environ.get("LLM_F_MODEL", DEFAULT_MODEL)

    llm_f_crew = LLMFCrew(model=resolved_model)
    result = llm_f_crew.crew().kickoff(
        inputs={"ticker": ticker, "month": month_str, "headlines": _format_headlines(headlines)}
    )
    signal: SentimentSignal = result.pydantic
    return signal.model_copy(update={"ticker": ticker, "month": month_str})
