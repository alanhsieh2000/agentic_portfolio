"""Tests for `src/agentic_portfolio/agents/report_summary.py`, the prose half of the monthly
report summary.

Per AGENTS.md, no test here calls any LLM. `ReportSummaryCrew` is
monkeypatched to a hand-written fake returning a hand-built `MonthNarrative`,
using the three-level duck type `tests/test_llm_f.py` established for
`LLMFCrew`: a task object carrying `.pydantic`, a crew object with
`kickoff(inputs)`, and a holder with `crew()`. Patching the name as imported
into the module under test - `report_summary.ReportSummaryCrew` - rather than
its definition site is what makes the substitution take effect.

`verify_narrative` is the reason this module has tests at all. The instruction
not to state an uncomputed figure lives in three places (the schema field
descriptions, the agent backstory, and the verifier), and only the third can be
tested without spending money. So most of what follows is about the verifier.
"""

import pytest

from agentic_portfolio.agents import report_summary
from agentic_portfolio.agents.report_summary import (
    FALLBACKS,
    NarrativeUnavailable,
    api_key_problem,
    generate_narrative,
    stated_figures,
    verify_narrative,
)
from agentic_portfolio.agents.summary_schema import NOT_APPLICABLE, MonthNarrative
from agentic_portfolio.config.settings import settings

FACTS = """MONTH: 2026-09
REPORTS: 9 total, 4 portfolio, 5 whatif
LEADERBOARD
  1. portfolio MSR: return=0.2513 volatility=0.1056 sharpe=2.0207
  2. portfolio GMV: return=0.0480 volatility=0.0416 sharpe=0.2394
HELD BOOK whatif PFF+PFFA+VZ (held): sharpe=-0.1253 annual_dividend=27,834.00
"""


def _narrative(**overrides) -> MonthNarrative:
    fields = {
        "headline": "The runs you saved this month all beat the book you hold.",
        "exploration_story": "You swept the three objectives on one pool, then tried two changes.",
        "risk_return_read": "The highest-Sharpe run took the most risk of the three.",
        "income_read": "The book you hold yields more than any run the optimizer found.",
        "holdings_gap_read": "Every run this month improved on the book you hold.",
        "consensus_read": "A core of seven tickers survived every objective.",
        "methodology_caution": "Two returns windows appear, so some rows cannot be read together.",
        "next_runs": ["uv run portfolio --objective MSR - to confirm the ranking holds"],
    }
    fields.update(overrides)
    return MonthNarrative(**fields)


class _FakeTask:
    def __init__(self, narrative):
        self.pydantic = narrative


class _FakeCrew:
    def __init__(self, narrative, raises=None):
        self._narrative = narrative
        self._raises = raises
        self.inputs = None

    def kickoff(self, inputs):
        self.inputs = inputs
        if self._raises:
            raise self._raises
        return _FakeTask(self._narrative)


class _FakeReportSummaryCrew:
    def __init__(self, narrative, model=None, raises=None):
        self.model = model
        self._crew = _FakeCrew(narrative, raises)

    def crew(self):
        return self._crew


def _install(monkeypatch, narrative, raises=None) -> list:
    """Patch in a fake crew and hand back the list its instances land in, so a
    test can read the model it was built with and the inputs it received."""
    built: list = []

    def factory(model):
        fake = _FakeReportSummaryCrew(narrative, model=model, raises=raises)
        built.append(fake)
        return fake

    monkeypatch.setattr(report_summary, "ReportSummaryCrew", factory)
    return built


# --------------------------------------------------------------------------
# The model and its key
# --------------------------------------------------------------------------

def test_the_model_defaults_to_the_configured_quick_one(monkeypatch):
    monkeypatch.setattr(settings, "llm_quick", "openai/gpt-5-nano")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    built = _install(monkeypatch, _narrative())

    generate_narrative("2026-09", 9, FACTS)

    assert built[0].model == "openai/gpt-5-nano"


def test_an_explicit_model_argument_wins_over_the_setting(monkeypatch):
    monkeypatch.setattr(settings, "llm_quick", "openai/gpt-5-nano")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    built = _install(monkeypatch, _narrative())

    generate_narrative("2026-09", 9, FACTS, model="anthropic/claude-sonnet-4-5")

    assert built[0].model == "anthropic/claude-sonnet-4-5"


def test_a_missing_key_is_reported_before_any_crew_is_built(monkeypatch):
    monkeypatch.setattr(settings, "openai_api_key", None)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    def explode(model):
        raise AssertionError("the crew must not be built without a key")

    monkeypatch.setattr(report_summary, "ReportSummaryCrew", explode)

    with pytest.raises(NarrativeUnavailable) as error:
        generate_narrative("2026-09", 9, FACTS, model="openai/gpt-5-nano")

    assert "OPENAI_API_KEY is not set" in str(error.value)
    assert "--no-llm" in str(error.value)


def test_a_key_in_settings_alone_is_enough(monkeypatch):
    """`pydantic_settings` reads `.env` without exporting it, so a check
    against the environment alone would refuse on this very repository."""
    monkeypatch.setattr(settings, "openai_api_key", "key-from-dotenv")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    assert api_key_problem("openai/gpt-5-nano") is None


def test_a_key_in_the_environment_alone_is_enough(monkeypatch):
    monkeypatch.setattr(settings, "openai_api_key", None)
    monkeypatch.setenv("OPENAI_API_KEY", "key-from-environment")

    assert api_key_problem("openai/gpt-5-nano") is None


def test_a_model_from_another_provider_needs_no_openai_key(monkeypatch):
    monkeypatch.setattr(settings, "openai_api_key", None)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(settings, "anthropic_api_key", "anthropic-key")

    assert api_key_problem("anthropic/claude-sonnet-4-5") is None


# --------------------------------------------------------------------------
# The call
# --------------------------------------------------------------------------

def test_the_facts_reach_the_task_verbatim_with_the_month_and_the_count(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    built = _install(monkeypatch, _narrative())

    generate_narrative("2026-09", 9, FACTS)

    assert built[0].crew().inputs == {
        "month": "2026-09",
        "report_count": 9,
        "facts": FACTS,
    }


def test_a_verified_narrative_comes_back_unchanged(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    narrative = _narrative()
    _install(monkeypatch, narrative)

    result, replaced = generate_narrative("2026-09", 9, FACTS)

    assert replaced == ()
    assert result.headline == narrative.headline
    assert result.next_runs == narrative.next_runs


def test_a_month_of_one_report_makes_no_call_at_all(monkeypatch):
    def explode(model):
        raise AssertionError("one report is nothing to compare, so nothing should be called")

    monkeypatch.setattr(report_summary, "ReportSummaryCrew", explode)

    with pytest.raises(NarrativeUnavailable) as error:
        generate_narrative("2026-09", 1, FACTS)

    assert "nothing to compare it with" in str(error.value)


def test_a_provider_failure_surfaces_as_narrative_unavailable(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    _install(monkeypatch, _narrative(), raises=RuntimeError("429 rate limited"))

    with pytest.raises(NarrativeUnavailable) as error:
        generate_narrative("2026-09", 9, FACTS, model="openai/gpt-5-nano")

    assert "openai/gpt-5-nano failed" in str(error.value)
    assert "429 rate limited" in str(error.value)


def test_free_text_instead_of_a_narrative_is_refused(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    class _Unstructured:
        def crew(self):
            return self

        def kickoff(self, inputs):
            return object()  # carries no .pydantic

    monkeypatch.setattr(report_summary, "ReportSummaryCrew", lambda model: _Unstructured())

    with pytest.raises(NarrativeUnavailable) as error:
        generate_narrative("2026-09", 9, FACTS)

    assert "no structured narrative" in str(error.value)


# --------------------------------------------------------------------------
# The verifier
# --------------------------------------------------------------------------

def test_stated_figures_finds_decimals_negatives_and_thousands_separators():
    found = stated_figures("sharpe 2.0207, -0.1253 and $27,834.00 on 60 months")

    assert found == {"2.0207", "0.1253", "27,834.00", "60"}


def test_a_field_quoting_a_figure_from_the_facts_is_kept():
    narrative = _narrative(risk_return_read="MSR reached a Sharpe of 2.0207 this month.")

    result, replaced = verify_narrative(narrative, FACTS)

    assert replaced == ()
    assert "2.0207" in result.risk_return_read


def test_a_field_stating_a_figure_nobody_computed_is_replaced(monkeypatch):
    narrative = _narrative(
        holdings_gap_read="Your book trails the frontier by 3.9981 Sharpe points."
    )

    result, replaced = verify_narrative(narrative, FACTS)

    assert replaced == ("holdings_gap_read",)
    assert result.holdings_gap_read == FALLBACKS["holdings_gap_read"]


def test_every_prose_field_is_checked_independently():
    narrative = _narrative(
        headline="A 9.8765 Sharpe was reached.",
        income_read="Another invented 4.4444 figure.",
    )

    result, replaced = verify_narrative(narrative, FACTS)

    assert sorted(replaced) == ["headline", "income_read"]
    assert result.risk_return_read == narrative.risk_return_read


def test_a_not_applicable_field_is_left_exactly_as_it_is():
    narrative = _narrative(consensus_read=NOT_APPLICABLE)

    result, replaced = verify_narrative(narrative, FACTS)

    assert result.consensus_read == NOT_APPLICABLE
    assert replaced == ()


def test_one_unsupported_suggestion_is_dropped_without_taking_the_others():
    narrative = _narrative(
        next_runs=[
            "uv run portfolio --objective GMV - to see the floor of the frontier",
            "uv run portfolio --min-dividend-yield 0.9999 - an invented threshold",
            "uv run portfolio-holdings whatif - to re-measure the book",
        ]
    )

    result, replaced = verify_narrative(narrative, FACTS)

    assert len(result.next_runs) == 2
    assert all("0.9999" not in entry for entry in result.next_runs)
    assert replaced == ("next_runs (1 of 3 dropped)",)


def test_a_suggestion_quoting_a_computed_figure_survives():
    narrative = _narrative(
        next_runs=["uv run portfolio --target-return 0.2513 - to pin the best run's return"]
    )

    result, replaced = verify_narrative(narrative, FACTS)

    assert result.next_runs == narrative.next_runs
    assert replaced == ()


def test_prose_with_no_figures_at_all_always_passes():
    narrative = _narrative(
        headline="Every run you saved beat the portfolio you hold.",
        next_runs=["uv run portfolio-holdings whatif - to try dropping the weakest holding"],
    )

    result, replaced = verify_narrative(narrative, FACTS)

    assert replaced == ()
    assert result.headline == narrative.headline
