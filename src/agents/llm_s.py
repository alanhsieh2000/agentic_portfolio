"""Entry point for producing one year's LLM-S screening rule.

`generate_rule` is the one public entry point this plan promises to
plans/04_candidate_scanner.md and plans/06_interactive_flow.md: resolve
the causally-masked snapshot date for a year, build `LLMSCrew` around it,
kick it off (making exactly one LLM call), and return the resulting
`ScreeningRule`.
"""

from __future__ import annotations

import logging
from datetime import date

import duckdb

from src.agents.llm_s_crew.crew import LLMSCrew
from src.agents.llm_s_crew.tools import load_snapshot
from src.agents.llm_s_schema import ScreeningRule
from src.config.settings import settings

logger = logging.getLogger(__name__)


def resolve_as_of_date(year: int, db_path: str = settings.db_path) -> date:
    """The causal-masking snapshot date for `year`: the most recent
    `factors` rebalance date in December of `year - 1` (matching the
    paper's own "December 2023 for test dates in 2024" framing, and
    README.md's Backtest Mode Stage 1 statement that S_2020 is generated
    from a 2019-12-31 snapshot). For `year=2020` this requires a real
    2019-12-31 row in `factors`, which `src/dataset/backfill_snapshot.py`
    adds on top of the regular 2020-01-01..2024-04-30 build (see
    plans/08_consistency_review.md finding 4 and plans/01_dataset.md's
    Decision Log) - run `uv run python -m src.dataset.backfill_snapshot`
    once, after the regular dataset build, to populate it. Falls back to
    the earliest available rebalance date if that backfill has not been
    run (or, in principle, for any other year with no prior-December
    snapshot), logging a warning because that year's rule is then not
    truly causally masked.
    """
    con = duckdb.connect(db_path, read_only=True)
    try:
        prior_december = con.execute(
            "SELECT max(rebalance_date) FROM factors WHERE rebalance_date < ?",
            [date(year, 1, 1)],
        ).fetchone()[0]
        if prior_december is not None:
            return prior_december
        earliest = con.execute("SELECT min(rebalance_date) FROM factors").fetchone()[0]
    finally:
        con.close()
    if earliest is None:
        raise ValueError(f"no rebalance dates found in {db_path!r}'s factors table")
    logger.warning(
        "generate_rule(year=%d): no factors rebalance date before %d-01-01; falling back to "
        "the earliest available date %s, which is NOT truly causally masked for this year",
        year,
        year,
        earliest,
    )
    return earliest


def generate_rule(year: int, model: str | None = None, db_path: str = settings.db_path) -> ScreeningRule:
    """Produce one year's LLM-S screening rule: resolve the causally-
    masked snapshot date, load it, build `LLMSCrew` around it, and kick
    off the crew (exactly one LLM call). Not deterministic across repeated
    calls for the same year, since the underlying LLM call isn't - running
    this twice may return two different, individually valid rules (see
    plans/02_llm_s_agent.md's Idempotence and Recovery).
    """
    as_of_date = resolve_as_of_date(year, db_path)
    snapshot = load_snapshot(db_path, as_of_date)
    resolved_model = model or settings.llm_s_model

    llm_s_crew = LLMSCrew(snapshot=snapshot, as_of_date=as_of_date, model=resolved_model)
    result = llm_s_crew.crew().kickoff(inputs={"as_of_date": as_of_date.isoformat(), "year": year})
    return result.pydantic
