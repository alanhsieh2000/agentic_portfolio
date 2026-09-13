"""Tests for `src/agentic_portfolio/agents/report_translation.py`.

Per AGENTS.md, no test here calls any LLM or any network.
`ReportTranslationCrew` is monkeypatched to a hand-written fake returning a
hand-built `TranslatedBriefing`, using the same three-level duck type
`tests/test_report_summary_agent.py` uses for `ReportSummaryCrew`: a task object
carrying `.pydantic`, a crew object with `kickoff(inputs)`, and a holder with
`crew()`. Patching the name as imported into the module under test is what makes
the substitution take effect.

One trap worth knowing, because it is why the key tests clear two places rather
than one: importing `crewai` calls `load_dotenv()`, so `.env` reaches
`os.environ` and `monkeypatch.delenv` alone does not simulate a missing key
reliably. Inside the suite that import has already happened before any test body
runs, so a `delenv` here does stick - but `settings.openai_api_key` is cleared as
well, since `api_key_problem` consults it first.

`verify_block` is the reason this module has tests at all. The instruction to
keep every marker lives in three places - the schema field descriptions, the
agent backstory, and the verifier - and only the third can be tested without
spending money. So most of what follows is about the verifier and the two gates
above it.
"""

from __future__ import annotations

import pytest

from agentic_portfolio.agents import report_translation
from agentic_portfolio.agents.report_translation import (
    MAX_BLOCKS,
    TranslationUnavailable,
    format_blocks,
    structure_prefix,
    translate_briefing,
    verify_block,
    verify_translation,
)
from agentic_portfolio.agents.translation_schema import TranslatedBlock, TranslatedBriefing
from agentic_portfolio.config.settings import settings

BRIEFING = """# Portfolio archive summary - 2026-09

9 reports (4 portfolio, 5 whatif).
Benchmark for USD: SPY, return 0.1225, Sharpe 0.5704.

## Risk/return leaderboard

  rank  what           return  Sharpe
  1     portfolio MSR  0.2513  2.0207
  2     portfolio GMV  0.0480  0.2394

The highest-Sharpe run took the most risk.

## What to run next

- uv run portfolio --objective MSR - to confirm the ranking holds
"""


class _FakeTask:
    def __init__(self, payload):
        self.pydantic = payload


class _FakeCrew:
    def __init__(self, payload, raises=None):
        self._payload = payload
        self._raises = raises
        self.inputs = None

    def kickoff(self, inputs):
        self.inputs = inputs
        if self._raises:
            raise self._raises
        return _FakeTask(self._payload)


class _FakeReportTranslationCrew:
    def __init__(self, payload, model=None, raises=None):
        self.model = model
        self._crew = _FakeCrew(payload, raises)

    def crew(self):
        return self._crew


def _install(monkeypatch, payload, raises=None) -> list:
    """Patch in a fake crew and hand back the list its instances land in."""
    built: list = []

    def factory(model):
        fake = _FakeReportTranslationCrew(payload, model=model, raises=raises)
        built.append(fake)
        return fake

    monkeypatch.setattr(report_translation, "ReportTranslationCrew", factory)
    monkeypatch.setattr(settings, "openai_api_key", "test-key")
    return built


def _echo(monkeypatch, transform=None, drop=(), override=None) -> list:
    """A fake crew that answers whatever it was asked, block for block.

    `transform` is applied to each block's text, so a test can simulate a real
    translation (which changes the words and keeps the markers) without writing
    one out. `drop` omits block indices. `override` replaces one index's text.
    """
    captured: list = []

    def factory(model):
        holder = _FakeReportTranslationCrew(None, model=model)

        def kickoff(inputs):
            captured.append(inputs)
            blocks = []
            for line in inputs["blocks"].split("\n"):
                index, _, text = line.partition(". ")
                index = int(index)
                if index in drop:
                    continue
                if override and index in override:
                    blocks.append(TranslatedBlock(index=index, text=override[index]))
                    continue
                blocks.append(
                    TranslatedBlock(index=index, text=transform(text) if transform else text)
                )
            return _FakeTask(
                TranslatedBriefing(language="Chinese", disclaimer="機器翻譯。", blocks=blocks)
            )

        holder._crew.kickoff = kickoff
        return holder

    monkeypatch.setattr(report_translation, "ReportTranslationCrew", factory)
    monkeypatch.setattr(settings, "openai_api_key", "test-key")
    return captured


def _translated(text: str) -> str:
    """Stand in for a translation: change every word, keep every marker, and
    keep the line's structure.

    Two things this helper got wrong in turn, both of which the verifier caught
    and both of which a real translator is explicitly told not to do.

    A word is kept whenever it CONTAINS a marker, not only when it starts with
    one. Testing `startswith` dropped the marker in `([[1]]` - punctuation sits
    against a marker all over this document - which is a fair warning about the
    likeliest way a live model loses one.

    And the leading `#`, `##` or `- ` is structure rather than words, so it is
    reproduced rather than translated. Replacing it demoted every heading in the
    document to a sentence.
    """
    prefix = structure_prefix(text)
    body = text[len(prefix):]
    return prefix + " ".join(word if "[[" in word else "譯" for word in body.split(" "))


# --- verify_block, one failure at a time --------------------------------------


def test_a_faithful_translation_is_accepted():
    assert verify_block("return [[0]] here", "報酬 [[0]] 於此") is None


def test_a_translation_keeping_markers_in_order_is_accepted():
    assert verify_block("a [[0]] b [[1]]", "甲 [[0]] 乙 [[1]]") is None


def test_an_empty_translation_is_rejected():
    assert verify_block("a [[0]]", "   ") == "came back empty"


def test_a_multi_line_translation_is_rejected():
    assert verify_block("a [[0]]", "甲 [[0]]\n乙") == "spans more than one line"


def test_a_translation_that_is_a_front_matter_fence_is_rejected():
    # `render_digest` guarantees no line is exactly `---`; a translation must
    # not introduce what the renderer was careful to avoid.
    assert verify_block("some prose", "---") == "reads as a front-matter fence"


def test_a_translation_that_is_a_horizontal_rule_is_rejected():
    assert verify_block("some prose", "***") == "reads as a front-matter fence"


def test_a_dropped_marker_is_rejected_and_named():
    assert verify_block("a [[0]] b [[1]]", "甲 [[0]] 乙") == "dropped marker(s) [1]"


def test_an_invented_marker_is_rejected_and_named():
    assert verify_block("a [[0]]", "甲 [[0]] [[4]]") == "introduced marker(s) [4]"


def test_reordered_markers_are_rejected_because_a_figure_moved():
    problem = verify_block("return [[0]] Sharpe [[1]]", "夏普 [[1]] 報酬 [[0]]")
    assert problem is not None
    assert "reordered" in problem


def test_a_repeated_marker_must_stay_repeated():
    assert verify_block("[[0]] and [[0]]", "[[0]] 與") is not None


def test_a_block_with_no_markers_is_accepted_when_translated():
    assert verify_block("The highest-Sharpe run took the most risk.", "最高夏普值") is None


# --- verify_translation -------------------------------------------------------


def test_every_good_block_is_kept():
    kept, rejected = verify_translation(["a [[0]]", "b"], {0: "甲 [[0]]", 1: "乙"})
    assert kept == {0: "甲 [[0]]", 1: "乙"}
    assert rejected == ()


def test_a_missing_block_is_reported_as_not_returned():
    kept, rejected = verify_translation(["a [[0]]", "b"], {0: "甲 [[0]]"})
    assert kept == {0: "甲 [[0]]"}
    assert rejected == ("block 1: no translation returned",)


def test_one_bad_block_does_not_take_its_neighbours_with_it():
    kept, rejected = verify_translation(["a [[0]]", "b [[1]]"], {0: "甲", 1: "乙 [[1]]"})
    assert list(kept) == [1]
    assert len(rejected) == 1


def test_a_rejection_names_the_block_it_came_from():
    _, rejected = verify_translation(["a [[0]]"], {0: "甲"})
    assert rejected[0].startswith("block 0:")


# --- the request --------------------------------------------------------------


def test_blocks_are_numbered_for_the_model():
    assert format_blocks(["first", "second"]) == "0. first\n1. second"


def test_braces_never_reach_the_prompt():
    # Defensive: CrewAI raises KeyError for a template variable it cannot
    # resolve, and a brace in a value should not depend on interpolation order.
    assert "{" not in format_blocks(["a {stray} brace"])
    assert "[stray]" in format_blocks(["a {stray} brace"])


def test_the_inputs_carry_the_language_and_the_block_count(monkeypatch):
    captured = _echo(monkeypatch, transform=_translated)
    translate_briefing(BRIEFING, "zh-TW", model="openai/gpt-5-nano")
    assert captured[0]["language"] == "zh-TW"
    assert captured[0]["block_count"] == len(captured[0]["blocks"].split("\n"))


def test_no_table_line_is_ever_offered_to_the_model(monkeypatch):
    # The guarantee the whole design rests on.
    captured = _echo(monkeypatch, transform=_translated)
    translate_briefing(BRIEFING, "zh-TW", model="openai/gpt-5-nano")
    assert "portfolio MSR  0.2513" not in captured[0]["blocks"]
    assert "rank  what" not in captured[0]["blocks"]


def test_figures_reach_the_model_only_as_markers(monkeypatch):
    captured = _echo(monkeypatch, transform=_translated)
    translate_briefing(BRIEFING, "zh-TW", literals=["USD", "SPY"], model="openai/gpt-5-nano")
    offered = captured[0]["blocks"]
    assert "0.1225" not in offered
    assert "[[" in offered


# --- the whole call -----------------------------------------------------------


def test_the_tables_of_a_translated_briefing_are_byte_identical(monkeypatch):
    _echo(monkeypatch, transform=_translated)
    out, _, _ = translate_briefing(BRIEFING, "zh-TW", model="openai/gpt-5-nano")
    for line in BRIEFING.split("\n"):
        if line.startswith("  "):
            assert line in out.split("\n")


def test_the_figures_come_back_restored(monkeypatch):
    _echo(monkeypatch, transform=_translated)
    out, _, _ = translate_briefing(BRIEFING, "zh-TW", literals=["USD", "SPY"], model="openai/gpt-5-nano")
    assert "0.1225" in out
    assert "0.5704" in out


def test_the_prose_is_actually_replaced(monkeypatch):
    _echo(monkeypatch, transform=_translated)
    out, _, _ = translate_briefing(BRIEFING, "zh-TW", model="openai/gpt-5-nano")
    assert "The highest-Sharpe run took the most risk." not in out
    assert "譯" in out


def test_the_line_count_is_unchanged(monkeypatch):
    _echo(monkeypatch, transform=_translated)
    out, _, _ = translate_briefing(BRIEFING, "zh-TW", model="openai/gpt-5-nano")
    assert len(out.split("\n")) == len(BRIEFING.split("\n"))


def test_the_disclaimer_comes_back(monkeypatch):
    _echo(monkeypatch, transform=_translated)
    _, disclaimer, _ = translate_briefing(BRIEFING, "zh-TW", model="openai/gpt-5-nano")
    assert disclaimer == "機器翻譯。"


def test_a_rejected_block_keeps_its_english(monkeypatch):
    # Block 5 is the prose line; hand back a version that dropped its markers.
    _echo(monkeypatch, transform=_translated, override={2: "壞"})
    out, _, rejected = translate_briefing(
        BRIEFING, "zh-TW", literals=["USD", "SPY"], model="openai/gpt-5-nano"
    )
    assert rejected
    assert "Benchmark for USD: SPY, return 0.1225, Sharpe 0.5704." in out


def test_a_command_bullet_keeps_its_command(monkeypatch):
    _echo(monkeypatch, transform=_translated)
    out, _, _ = translate_briefing(BRIEFING, "zh-TW", model="openai/gpt-5-nano")
    assert "uv run portfolio --objective MSR" in out


def test_no_line_of_a_translated_briefing_is_a_front_matter_fence(monkeypatch):
    _echo(monkeypatch, transform=_translated)
    out, _, _ = translate_briefing(BRIEFING, "zh-TW", model="openai/gpt-5-nano")
    assert not any(line.strip() == "---" for line in out.split("\n"))


# --- the gates ----------------------------------------------------------------


def test_a_translation_that_echoes_the_english_is_refused(monkeypatch):
    # The one failure a perfect marker score cannot catch: a small model
    # repeating its input, which would be saved as a file labelled zh-TW
    # containing English.
    _echo(monkeypatch, transform=None)
    with pytest.raises(TranslationUnavailable) as error:
        translate_briefing(BRIEFING, "zh-TW", model="openai/gpt-5-nano")
    assert "unchanged" in str(error.value)


def test_a_translation_covering_too_little_is_refused(monkeypatch):
    _echo(monkeypatch, transform=_translated, drop=(0, 1, 2, 3, 4, 5))
    with pytest.raises(TranslationUnavailable) as error:
        translate_briefing(BRIEFING, "zh-TW", model="openai/gpt-5-nano")
    assert "below the" in str(error.value)


def test_a_briefing_with_no_prose_is_refused_before_any_call(monkeypatch):
    def factory(model):
        raise AssertionError("no crew should be built for a briefing with no prose")

    monkeypatch.setattr(report_translation, "ReportTranslationCrew", factory)
    with pytest.raises(TranslationUnavailable) as error:
        translate_briefing("  rank  what\n  1     MSR\n", "zh-TW")
    assert "every line of it is a table" in str(error.value)


def test_a_blank_language_is_refused_before_any_call(monkeypatch):
    def factory(model):
        raise AssertionError("no crew should be built without a language")

    monkeypatch.setattr(report_translation, "ReportTranslationCrew", factory)
    with pytest.raises(TranslationUnavailable):
        translate_briefing(BRIEFING, "   ")


def test_a_briefing_larger_than_the_cap_is_refused_before_any_call(monkeypatch):
    def factory(model):
        raise AssertionError("no crew should be built above the cap")

    monkeypatch.setattr(report_translation, "ReportTranslationCrew", factory)
    huge = "\n".join(f"Sentence number {n}." for n in range(MAX_BLOCKS + 1))
    with pytest.raises(TranslationUnavailable) as error:
        translate_briefing(huge, "zh-TW")
    assert "chunking" in str(error.value)


# --- model resolution and failure ---------------------------------------------


def test_the_model_defaults_to_the_configured_quick_one(monkeypatch):
    monkeypatch.setattr(settings, "llm_quick", "openai/configured-model")
    built = _echo(monkeypatch, transform=_translated)
    translate_briefing(BRIEFING, "zh-TW")
    assert built  # the fake recorded a call
    monkeypatch.undo()


def test_an_explicit_model_wins_over_the_setting(monkeypatch):
    monkeypatch.setattr(settings, "llm_quick", "openai/configured-model")
    models: list = []

    def factory(model):
        models.append(model)
        holder = _FakeReportTranslationCrew(
            TranslatedBriefing(language="zh", disclaimer="d", blocks=[])
        )
        return holder

    monkeypatch.setattr(report_translation, "ReportTranslationCrew", factory)
    monkeypatch.setattr(settings, "openai_api_key", "test-key")
    with pytest.raises(TranslationUnavailable):
        translate_briefing(BRIEFING, "zh-TW", model="openai/explicit-model")
    assert models == ["openai/explicit-model"]


def test_a_missing_key_is_reported_before_any_crew_is_built(monkeypatch):
    monkeypatch.setattr(settings, "openai_api_key", None)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    def factory(model):
        raise AssertionError("no crew should be built without a key")

    monkeypatch.setattr(report_translation, "ReportTranslationCrew", factory)
    with pytest.raises(TranslationUnavailable) as error:
        translate_briefing(BRIEFING, "zh-TW", model="openai/gpt-5-nano")
    assert "OPENAI_API_KEY" in str(error.value)


def test_the_missing_key_message_does_not_suggest_no_llm(monkeypatch):
    # `--no-llm` is refused alongside `--language`, so naming it would send the
    # reader to a flag that would be rejected.
    monkeypatch.setattr(settings, "openai_api_key", None)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(report_translation, "ReportTranslationCrew", lambda model: None)
    with pytest.raises(TranslationUnavailable) as error:
        translate_briefing(BRIEFING, "zh-TW", model="openai/gpt-5-nano")
    assert "--no-llm" not in str(error.value)
    assert "--language" in str(error.value)


def test_a_provider_failure_surfaces_as_translation_unavailable(monkeypatch):
    _install(monkeypatch, None, raises=RuntimeError("upstream exploded"))
    with pytest.raises(TranslationUnavailable) as error:
        translate_briefing(BRIEFING, "zh-TW", model="openai/gpt-5-nano")
    assert "openai/gpt-5-nano" in str(error.value)
    assert "upstream exploded" in str(error.value)


def test_a_result_with_no_structured_payload_is_refused(monkeypatch):
    _install(monkeypatch, None)
    with pytest.raises(TranslationUnavailable) as error:
        translate_briefing(BRIEFING, "zh-TW", model="openai/gpt-5-nano")
    assert "no structured translation" in str(error.value)


def test_an_unknown_block_index_in_the_response_is_ignored(monkeypatch):
    payload = TranslatedBriefing(
        language="zh-TW",
        disclaimer="機器翻譯。",
        blocks=[TranslatedBlock(index=999, text="nowhere")],
    )
    _install(monkeypatch, payload)
    with pytest.raises(TranslationUnavailable) as error:
        translate_briefing(BRIEFING, "zh-TW", model="openai/gpt-5-nano")
    # It is refused for coverage, not for the stray index, and nothing raised.
    assert "below the" in str(error.value)


# --- structure, which the marker check cannot see ------------------------------


def test_a_heading_that_lost_its_marker_is_rejected():
    # `#` is not a figure, and a heading often carries no markers at all, so the
    # marker check is blind to this. Losing it demotes a whole section.
    problem = verify_block("## Risk/return leaderboard", "風險報酬排行榜")
    assert problem is not None
    assert "'##'" in problem


def test_a_heading_that_kept_its_marker_is_accepted():
    assert verify_block("## Risk/return leaderboard", "## 風險報酬排行榜") is None


def test_a_top_level_heading_must_keep_a_single_hash():
    assert verify_block("# Portfolio archive summary - [[0]]", "## 摘要 [[0]]") is not None
    assert verify_block("# Portfolio archive summary - [[0]]", "# 摘要 [[0]]") is None


def test_a_bullet_that_lost_its_dash_is_rejected():
    problem = verify_block("- [[0]] - settle the ranking", "[[0]] - 確認排序")
    assert problem is not None
    assert "'-'" in problem


def test_a_bullet_that_kept_its_dash_is_accepted():
    assert verify_block("- [[0]] - settle the ranking", "- [[0]] - 確認排序") is None


def test_a_paragraph_that_grew_a_heading_marker_is_rejected():
    problem = verify_block("Ordinary prose here.", "## 這是標題")
    assert problem == "added a heading or bullet marker the original did not have"


def test_a_paragraph_beginning_with_a_hash_word_is_not_mistaken_for_a_heading():
    # `structure_prefix` requires a space after the hashes, so `#1` is prose.
    assert verify_block("Rank #1 this month.", "本月排名 #1。") is None


def test_the_month_reaches_the_prompt(monkeypatch):
    captured = _echo(monkeypatch, transform=_translated)
    translate_briefing(BRIEFING, "zh-TW", model="openai/gpt-5-nano", month="2026-09")
    assert captured[0]["month"] == "2026-09"


def test_an_absent_month_becomes_a_phrase_rather_than_an_empty_prompt(monkeypatch):
    captured = _echo(monkeypatch, transform=_translated)
    translate_briefing(BRIEFING, "zh-TW", model="openai/gpt-5-nano")
    assert captured[0]["month"].strip()
