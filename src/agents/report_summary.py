"""Entry point for the prose half of one month's portfolio report summary.

`src/flow/report_summary.py` reads a month of the saved report archive and
computes every figure the briefing contains. This module takes those computed
figures and asks a language model for the sentences that say what they mean -
and for nothing else.

The split is the same one `src/agents/llm_f.py` makes and for the same reason.
There, the model estimates a probability per headline and
`compute_decayed_score` performs every calculation, because a holistic LLM
judgment was the wrong shape for the job (see
`plans/08_consistency_review.md` finding 5). Here the model writes prose and
Python does every sum, because the default model - `openai/gpt-5-nano`, from
`LLM_QUICK` - is the cheapest one available, and a briefing whose Sharpe
ratios were produced by a small model's arithmetic would be worse than no
briefing at all.

Three mechanisms keep that split, not one. The field descriptions in
`src/agents/summary_schema.py` reach the model as its output schema; the
backstory in `src/agents/summary_crew/config/agents.yaml` states the rule in
words; and `verify_narrative` below enforces it afterwards, replacing any
field that states a figure it was never given. Instructions alone would be a
hope. The verifier is the part that holds.
"""

from __future__ import annotations

import logging
import os
import re

from src.agents.summary_crew.crew import ReportSummaryCrew
from src.agents.summary_schema import NOT_APPLICABLE, MonthNarrative
from src.config.settings import settings

logger = logging.getLogger(__name__)

#: Any run of digits, allowing thousands separators and a decimal part. Used
#: to find the figures a narrative states so they can be checked against the
#: figures it was given.
_FIGURE = re.compile(r"[0-9][0-9,]*(?:\.[0-9]+)?")

#: What a rejected or absent field says instead. Deterministic sentences, so a
#: briefing whose prose pass failed or misbehaved still reads as a finished
#: document rather than as a hole.
FALLBACKS: dict[str, str] = {
    "headline": "See the leaderboard below for this month's ranking.",
    "exploration_story": "The sessions below list every report saved this month, in order.",
    "risk_return_read": "The leaderboard below ranks each portfolio within its own returns window.",
    "income_read": "The dividend yield and annual income of each portfolio are in the leaderboard.",
    "holdings_gap_read": "The comparison below sets the portfolio you hold against the best run of the month.",
    "consensus_read": "The lists below name the tickers every run held and the candidates none did.",
    "methodology_caution": "The cautions below name every reason two of this month's reports may not be comparable.",
}

PROSE_FIELDS: tuple[str, ...] = tuple(FALLBACKS)


class NarrativeUnavailable(RuntimeError):
    """The prose pass could not run or did not finish.

    Raised rather than returned so a caller cannot use a half-built narrative
    by accident; `src/flow/summary_cli.py` catches it and prints the computed
    briefing without prose, because the figures are the value here and the
    sentences are the garnish.
    """


def api_key_problem(model: str) -> str | None:
    """The reason this model cannot be reached, or `None` if it can.

    Checked before a crew is built, so an unreachable model costs nothing and
    fails with a sentence rather than from inside CrewAI's executor.

    The provider's key is looked for in `Settings` FIRST and the process
    environment second, and that order is not cosmetic: `pydantic_settings`
    loads `.env` without exporting it to `os.environ`, so a check against the
    environment alone would refuse to run on a machine whose key lives only in
    `.env` - which is how this repository is configured.
    """
    providers = {
        "openai/": ("OPENAI_API_KEY", settings.openai_api_key),
        "anthropic/": ("ANTHROPIC_API_KEY", settings.anthropic_api_key),
    }
    for prefix, (variable, configured) in providers.items():
        if model.startswith(prefix) and not (configured or os.environ.get(variable)):
            return (
                f"{variable} is not set, so the prose for this summary cannot be written "
                f"with {model}. Set it in .env or the environment, choose another model "
                "with LLM_QUICK or --model, or re-run with --no-llm to get the figures "
                "without the prose"
            )
    return None


def stated_figures(text: str) -> set[str]:
    """Every figure a piece of prose states."""
    return set(_FIGURE.findall(text))


def verify_narrative(
    narrative: MonthNarrative, facts: str
) -> tuple[MonthNarrative, tuple[str, ...]]:
    """A narrative with every unsupported field replaced, and the names of the
    fields replaced.

    A field is rejected when it states a figure that does not appear in the
    facts the model was given. That is a deliberately narrow test and it is the
    right one: quoting a figure from the facts is useful and allowed, while
    producing one that was never computed is the single failure this whole
    design exists to prevent. A rejected field falls back to a deterministic
    sentence, and the caller reports how many fell back - a silent substitution
    would be worse than the fabrication it replaces, because the reader would
    have no way to know the prose pass had misbehaved.

    `next_runs` entries are checked one at a time and an offending entry is
    dropped rather than taking the whole list with it, since one bad suggestion
    among four does not make the other three less runnable.
    """
    available = stated_figures(facts)
    replaced: list[str] = []
    corrected: dict[str, object] = {}

    for field in PROSE_FIELDS:
        text = getattr(narrative, field)
        if text.strip() == NOT_APPLICABLE:
            corrected[field] = NOT_APPLICABLE
            continue
        if stated_figures(text) - available:
            corrected[field] = FALLBACKS[field]
            replaced.append(field)
        else:
            corrected[field] = text

    kept = [
        entry
        for entry in narrative.next_runs
        if not (stated_figures(entry) - available)
    ]
    if len(kept) != len(narrative.next_runs):
        replaced.append(f"next_runs ({len(narrative.next_runs) - len(kept)} of "
                        f"{len(narrative.next_runs)} dropped)")
    corrected["next_runs"] = kept

    return MonthNarrative(**corrected), tuple(replaced)


def generate_narrative(
    month: str, report_count: int, facts: str, model: str | None = None
) -> tuple[MonthNarrative, tuple[str, ...]]:
    """The prose for one month's briefing, verified against `facts`.

    `facts` is `src/flow/report_summary.py`'s `digest_for_llm` output: the
    computed figures, compact, and the only thing the model is told.

    Raises `NarrativeUnavailable` when the model cannot be reached or the call
    does not produce a narrative. Makes no call at all for a month of fewer
    than two reports: there is nothing to compare, so there is nothing for
    prose to add that the single report does not already say - the same
    short-circuit `generate_signal` makes for an empty headline list, where
    calling an LLM anyway would either waste a call or invite it to invent
    something out of nothing.
    """
    if report_count < 2:
        raise NarrativeUnavailable(
            f"{month} holds {report_count} report(s), and a briefing about one report has "
            "nothing to compare it with"
        )

    resolved_model = model or settings.llm_quick
    problem = api_key_problem(resolved_model)
    if problem:
        raise NarrativeUnavailable(problem)

    try:
        result = ReportSummaryCrew(model=resolved_model).crew().kickoff(
            inputs={"month": month, "report_count": report_count, "facts": facts}
        )
    except Exception as error:  # noqa: BLE001 - any provider failure is the same to us
        logger.warning("prose pass failed with %s: %s", resolved_model, error)
        raise NarrativeUnavailable(f"{resolved_model} failed: {error}") from error

    narrative = getattr(result, "pydantic", None)
    if narrative is None:
        raise NarrativeUnavailable(
            f"{resolved_model} returned no structured narrative, only free text"
        )
    return verify_narrative(narrative, facts)
