"""Tests for `src/agentic_portfolio/flow/summary_cli.py`, the `uv run portfolio-summary` command.

Per AGENTS.md, no test here calls any LLM or any network. `generate_narrative`
is monkeypatched at `agentic_portfolio.flow.summary_cli.generate_narrative` - the name as
imported into the module under test - so both the success and the failure path
are exercised without a provider. Argv goes in through
`monkeypatch.setattr(sys, "argv", ...)` and output comes out through `capsys`,
the pattern the fifty-odd `main()` tests in `tests/test_cli.py` already use,
and every path is pointed at `tmp_path` with `--output-dir`.

`tests/conftest.py`'s autouse fixture already redirects `settings.output_dir`
into `tmp_path` for the whole suite, so even a test that forgot the flag could
not write into the repository's real `output/`.
"""

import sys
from datetime import date

import pytest

from agentic_portfolio.agents.report_summary import NarrativeUnavailable
from agentic_portfolio.agents.summary_schema import NOT_APPLICABLE, MonthNarrative
from agentic_portfolio.config.settings import settings
from agentic_portfolio.flow import summary_cli
from agentic_portfolio.flow.report_archive import ReportArchive, load_report, save_report
from agentic_portfolio.flow.summary_cli import census, default_month, main, parse_month, provenance

MONTH = "2026-09"
AS_OF = date(2026, 9, 11)

WINDOW = {"window_start": "2021-10-01", "window_end": "2026-09-01", "window_months": 60}


def _archive(tmp_path, kind):
    return ReportArchive(
        output_dir=str(tmp_path / "output"),
        enabled=True,
        kind=kind,
        as_of=AS_OF,
        command=f"portfolio-{kind} --currency USD",
    )


def _portfolio(tmp_path, objective, sharpe, weights, annual_return):
    body = "\n".join(
        ["Weights:"]
        + [f"  {ticker}: {weight:.4f}" for ticker, weight in weights]
        + [
            "",
            f"Portfolio expected return: {annual_return:.4f}  Portfolio Sharpe: {sharpe:.4f}",
            "Benchmark SPY: return=0.1225  volatility=0.1481  Sharpe=0.5704  (60 of 60 month(s))",
        ]
    )
    return save_report(
        body,
        _archive(tmp_path, "portfolio"),
        {
            "variant": "edit", "currency": "USD", "objective": objective,
            "selection": "user_provided", "value": 100000.0,
            "candidates": ["AMLP", "BIL", "BOXX", "NVDA", "QQQI"],
            "benchmark": "SPY", "benchmark_return": 0.1225, "risk_free_rate": 0.0380,
            "annual_return": annual_return, "annual_volatility": 0.05, "sharpe": sharpe,
            "annual_dividend": 4000.0, "dividend_yield": 0.04, **WINDOW,
        },
    )


def _two_reports(tmp_path):
    _portfolio(tmp_path, "MSR", 2.0207, [("NVDA", 0.6), ("QQQI", 0.4)], 0.2513)
    _portfolio(tmp_path, "GMV", 0.2394, [("BIL", 0.7), ("BOXX", 0.3)], 0.0480)


def _narrative(**overrides) -> MonthNarrative:
    fields = {
        "headline": "Both runs beat the benchmark on risk-adjusted terms.",
        "exploration_story": "You compared the two extremes of the frontier.",
        "risk_return_read": "The higher-Sharpe run is also the more concentrated one.",
        "income_read": "Both runs land on the same dividend yield.",
        "holdings_gap_read": NOT_APPLICABLE,
        "consensus_read": "The two runs share no holding at all.",
        "methodology_caution": "Both runs share one window, so they are comparable.",
        "next_runs": ["uv run portfolio-holdings whatif - to price what you actually hold"],
    }
    fields.update(overrides)
    return MonthNarrative(**fields)


def _run(monkeypatch, tmp_path, *extra):
    monkeypatch.setattr(
        sys, "argv", ["portfolio-summary", MONTH, "--output-dir", str(tmp_path / "output"), *extra]
    )
    main()


def _summaries(tmp_path):
    folder = tmp_path / "output" / MONTH
    return sorted(p for p in folder.glob("*summary*.md")) if folder.is_dir() else []


def _restamp(path, saved_at: str) -> None:
    """Rewrite one saved report's `saved_at` line.

    `save_report` stamps it from the clock to the nearest second, so two files
    written by one test carry the same time. A test about which of them is newer
    has to set the times itself.
    """
    lines = path.read_text(encoding="utf-8").splitlines()
    for index, line in enumerate(lines):
        if line.startswith("saved_at:"):
            lines[index] = f"saved_at: {saved_at}"
            break
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _fake_narrative(monkeypatch, narrative=None, replaced=(), raises=None):
    """Patch `generate_narrative` and hand back the list of calls it received."""
    calls: list = []

    def fake(month, report_count, facts, model=None):
        calls.append({"month": month, "report_count": report_count, "facts": facts,
                      "model": model})
        if raises:
            raise raises
        return (narrative or _narrative()), replaced

    monkeypatch.setattr(summary_cli, "generate_narrative", fake)
    return calls


# --------------------------------------------------------------------------
# Arguments
# --------------------------------------------------------------------------

def test_the_month_defaults_to_the_current_calendar_one():
    assert default_month(date(2026, 9, 11)) == "2026-09"
    assert default_month(date(2026, 12, 31)) == "2026-12"


def test_parse_month_accepts_a_real_month():
    assert parse_month("2026-09") == "2026-09"


@pytest.mark.parametrize("bad", ["2026-13", "2026-00", "sept", "2026/09", "2026-9", ""])
def test_parse_month_refuses_anything_else(bad):
    with pytest.raises(ValueError, match="is not a month"):
        parse_month(bad)


def test_a_malformed_month_exits_two_without_reading_anything(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(sys, "argv", ["portfolio-summary", "2026-13"])

    with pytest.raises(SystemExit) as exit_info:
        main()

    assert exit_info.value.code == 2
    assert "is not a month" in capsys.readouterr().err


def test_a_bare_invocation_summarizes_this_month(monkeypatch, tmp_path, capsys):
    _two_reports(tmp_path)
    _fake_narrative(monkeypatch)
    monkeypatch.setattr(summary_cli, "default_month", lambda: MONTH)
    monkeypatch.setattr(
        sys, "argv", ["portfolio-summary", "--output-dir", str(tmp_path / "output")]
    )

    main()

    assert "Portfolio archive summary - 2026-09" in capsys.readouterr().out


# --------------------------------------------------------------------------
# Nothing to summarize
# --------------------------------------------------------------------------

def test_a_missing_month_exits_one_and_names_the_folder(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(
        sys, "argv",
        ["portfolio-summary", "2026-10", "--output-dir", str(tmp_path / "output")],
    )

    with pytest.raises(SystemExit) as exit_info:
        main()

    out = capsys.readouterr().out
    assert exit_info.value.code == 1
    assert "2026-10" in out
    assert "kind: portfolio or kind: whatif" in out


def test_a_month_of_only_unreadable_files_exits_one_and_names_each(monkeypatch, tmp_path, capsys):
    folder = tmp_path / "output" / MONTH
    folder.mkdir(parents=True)
    (folder / "notes.md").write_text("a note I left here\n", encoding="utf-8")

    with pytest.raises(SystemExit):
        _run(monkeypatch, tmp_path)

    out = capsys.readouterr().out
    assert "Skipped notes.md" in out
    assert "No reports to summarize" in out


def test_nothing_is_created_when_there_is_nothing_to_summarize(monkeypatch, tmp_path):
    monkeypatch.setattr(
        sys, "argv",
        ["portfolio-summary", "2026-10", "--output-dir", str(tmp_path / "output")],
    )

    with pytest.raises(SystemExit):
        main()

    assert not (tmp_path / "output" / "2026-10").exists()


# --------------------------------------------------------------------------
# Saving, and recognizing a repeat
# --------------------------------------------------------------------------

def test_a_summary_is_written_and_named(monkeypatch, tmp_path, capsys):
    _two_reports(tmp_path)
    _fake_narrative(monkeypatch)

    _run(monkeypatch, tmp_path)

    files = _summaries(tmp_path)
    assert len(files) == 1
    assert f"Saved report: {files[0]}" in capsys.readouterr().out


def test_the_front_matter_records_the_kind_month_count_and_source_set(monkeypatch, tmp_path):
    _two_reports(tmp_path)
    _fake_narrative(monkeypatch)

    _run(monkeypatch, tmp_path)
    facts, body = load_report(_summaries(tmp_path)[0])

    assert facts["kind"] == "summary"
    assert facts["month"] == MONTH
    assert facts["report_count"] == "2"
    assert len(facts["sources_digest"]) == 64
    assert facts["narrative_status"] == "written"
    assert "Risk/return leaderboard" in body


def test_a_repeat_run_writes_nothing_and_makes_no_model_call(monkeypatch, tmp_path, capsys):
    _two_reports(tmp_path)
    calls = _fake_narrative(monkeypatch)
    _run(monkeypatch, tmp_path)
    capsys.readouterr()

    _run(monkeypatch, tmp_path)

    out = capsys.readouterr().out
    assert "Summary already saved for these 2 reports" in out
    assert len(_summaries(tmp_path)) == 1
    assert len(calls) == 1  # the second run stopped before the prose pass


def test_force_writes_a_second_summary(monkeypatch, tmp_path):
    _two_reports(tmp_path)
    _fake_narrative(monkeypatch)
    _run(monkeypatch, tmp_path)

    _fake_narrative(monkeypatch, _narrative(headline="A different sentence entirely."))
    _run(monkeypatch, tmp_path, "--force")

    assert len(_summaries(tmp_path)) == 2


def test_adding_a_report_changes_the_source_set_and_writes_again(monkeypatch, tmp_path, capsys):
    _two_reports(tmp_path)
    _fake_narrative(monkeypatch)
    _run(monkeypatch, tmp_path)
    first = load_report(_summaries(tmp_path)[0])[0]["sources_digest"]
    capsys.readouterr()

    _portfolio(tmp_path, "MV", 1.6347, [("BOXX", 0.5), ("QQQI", 0.5)], 0.1225)
    _run(monkeypatch, tmp_path)

    files = _summaries(tmp_path)
    assert len(files) == 2
    digests = {load_report(path)[0]["sources_digest"] for path in files}
    assert first in digests and len(digests) == 2
    assert "Saved report:" in capsys.readouterr().out


def test_a_summary_never_summarizes_an_earlier_summary(monkeypatch, tmp_path, capsys):
    _two_reports(tmp_path)
    _fake_narrative(monkeypatch)
    _run(monkeypatch, tmp_path)
    capsys.readouterr()

    _portfolio(tmp_path, "MV", 1.6347, [("BOXX", 0.5), ("QQQI", 0.5)], 0.1225)
    _run(monkeypatch, tmp_path)

    out = capsys.readouterr().out
    # Three reports, not four: the first summary sits in the same folder and is
    # skipped by kind rather than counted as a report about a portfolio. The
    # filenames carry digests, so the two summaries are identified by what they
    # counted rather than by their order on disk.
    assert "3 reports (3 portfolio, 0 whatif)" in out
    counted = {load_report(path)[0]["report_count"] for path in _summaries(tmp_path)}
    assert counted == {"2", "3"}


def test_stdout_prints_the_briefing_and_writes_nothing(monkeypatch, tmp_path, capsys):
    _two_reports(tmp_path)
    _fake_narrative(monkeypatch)

    _run(monkeypatch, tmp_path, "--stdout")

    out = capsys.readouterr().out
    assert "Portfolio archive summary" in out
    assert "Saved report:" not in out
    assert _summaries(tmp_path) == []


# --------------------------------------------------------------------------
# The prose, and doing without it
# --------------------------------------------------------------------------

def test_the_narrative_is_placed_between_the_computed_sections(monkeypatch, tmp_path, capsys):
    _two_reports(tmp_path)
    _fake_narrative(monkeypatch)

    _run(monkeypatch, tmp_path, "--stdout")
    out = capsys.readouterr().out

    assert "Both runs beat the benchmark on risk-adjusted terms." in out
    assert "The higher-Sharpe run is also the more concentrated one." in out
    assert "## What to run next" in out
    assert "- uv run portfolio-holdings whatif - to price what you actually hold" in out


def test_a_not_applicable_field_places_no_prose_over_the_computed_reason(
    monkeypatch, tmp_path, capsys
):
    _two_reports(tmp_path)
    _fake_narrative(monkeypatch)

    _run(monkeypatch, tmp_path, "--stdout")
    out = capsys.readouterr().out

    assert NOT_APPLICABLE not in out
    assert "n/a - this month holds no 'whatif' baseline" in out


def test_no_llm_never_calls_the_model_and_says_so(monkeypatch, tmp_path, capsys):
    _two_reports(tmp_path)
    calls = _fake_narrative(monkeypatch)

    _run(monkeypatch, tmp_path, "--no-llm")
    out = capsys.readouterr().out

    assert calls == []
    assert "Prose: none, by --no-llm" in out
    assert load_report(_summaries(tmp_path)[0])[0]["narrative_status"] == "skipped"
    assert "llm_model" not in load_report(_summaries(tmp_path)[0])[0]


def test_an_unreachable_model_still_produces_the_whole_briefing(monkeypatch, tmp_path, capsys):
    _two_reports(tmp_path)
    _fake_narrative(
        monkeypatch, raises=NarrativeUnavailable("OPENAI_API_KEY is not set")
    )

    _run(monkeypatch, tmp_path)
    captured = capsys.readouterr()

    assert "Warning: OPENAI_API_KEY is not set" in captured.err
    assert "Risk/return leaderboard" in captured.out
    assert "Prose: none - OPENAI_API_KEY is not set" in captured.out
    facts = load_report(_summaries(tmp_path)[0])[0]
    assert facts["narrative_status"].startswith("failed: OPENAI_API_KEY")


def test_a_replaced_field_is_reported_in_the_saved_briefing(monkeypatch, tmp_path, capsys):
    _two_reports(tmp_path)
    _fake_narrative(monkeypatch, replaced=("risk_return_read",))

    _run(monkeypatch, tmp_path, "--stdout")

    assert "1 section(s) replaced by machine-written text" in capsys.readouterr().out


def test_the_model_flag_overrides_the_configured_one(monkeypatch, tmp_path):
    _two_reports(tmp_path)
    calls = _fake_narrative(monkeypatch)

    _run(monkeypatch, tmp_path, "--stdout", "--model", "openai/gpt-4.1-nano")

    assert calls[0]["model"] == "openai/gpt-4.1-nano"


def test_the_configured_model_is_used_by_default(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "llm_quick", "openai/gpt-5-nano")
    _two_reports(tmp_path)
    calls = _fake_narrative(monkeypatch)

    _run(monkeypatch, tmp_path, "--stdout")

    assert calls[0]["model"] == "openai/gpt-5-nano"


def test_the_model_is_handed_the_computed_facts_and_not_the_raw_reports(
    monkeypatch, tmp_path
):
    _two_reports(tmp_path)
    calls = _fake_narrative(monkeypatch)

    _run(monkeypatch, tmp_path, "--stdout")
    facts = calls[0]["facts"]

    assert calls[0]["report_count"] == 2
    assert "LEADERBOARD" in facts
    assert "sharpe=2.0207" in facts
    assert "Share allocation" not in facts  # the report bodies never reach it


# --------------------------------------------------------------------------
# Small pieces
# --------------------------------------------------------------------------

def test_the_census_line_counts_both_kinds_and_the_currencies(tmp_path):
    _two_reports(tmp_path)
    from agentic_portfolio.flow.report_summary import load_month

    records, _ = load_month(tmp_path / "output", MONTH)

    assert census(records) == "2 reports (2 portfolio, 0 whatif), 1 currency."


def test_provenance_distinguishes_every_way_the_prose_can_be_absent():
    assert "by --no-llm" in provenance(None, (), None)
    assert "none - 429" in provenance("openai/gpt-5-nano", (), "429 rate limited")
    assert "2 section(s) replaced" in provenance("m", ("a", "b"), None)
    assert "not written by the model" in provenance("m", (), None)


def test_the_saved_summary_carries_the_source_reports_section(monkeypatch, tmp_path):
    """The appendix has to survive the round trip through the archive, because
    the saved file is the one a reader comes back to days later."""
    _two_reports(tmp_path)
    _fake_narrative(monkeypatch)

    _run(monkeypatch, tmp_path)
    facts, body = load_report(_summaries(tmp_path)[0])

    assert "## Source reports" in body
    covered = sorted(
        p.name for p in (tmp_path / "output" / MONTH).glob("*.md")
        if "summary" not in p.name
    )
    assert len(covered) == 2
    for name in covered:
        assert name in body
    assert facts["report_count"] == "2"


def test_no_line_of_the_appendix_looks_like_a_front_matter_fence(monkeypatch, tmp_path, capsys):
    """A new section is the likeliest thing to break the promise that the body
    never contains a bare `---`, which would read as a fence to any parser less
    careful than `load_report`."""
    _two_reports(tmp_path)
    _fake_narrative(monkeypatch)

    _run(monkeypatch, tmp_path, "--stdout")
    out = capsys.readouterr().out

    assert "## Source reports" in out
    assert not any(line.strip() == "---" for line in out.splitlines())


def test_the_appendix_lets_a_leaderboard_row_be_traced_to_a_file(monkeypatch, tmp_path, capsys):
    """The whole point of the section, asserted end to end: a label read off the
    leaderboard appears in the source list beside a filename that exists."""
    _two_reports(tmp_path)
    _fake_narrative(monkeypatch)

    _run(monkeypatch, tmp_path, "--stdout")
    out = capsys.readouterr().out
    appendix = out[out.index("## Source reports"):]

    row = next(line for line in appendix.splitlines() if "portfolio MSR" in line)
    digest8, _saved, *_rest = row.split()
    filename = row.split()[-1]

    assert len(digest8) == 8
    assert filename.endswith(f"-{digest8}.md")
    assert (tmp_path / "output" / MONTH / filename).is_file()


def test_after_force_a_repeat_run_names_the_newer_summary(monkeypatch, tmp_path, capsys):
    """Two summaries can cover one set of reports, and the reader wants the
    later one.

    `--force` is the way to get a changed briefing format into a saved file, so
    it leaves two summaries with the same `sources_digest`. Pointing at whichever
    sorted first by filename would name the older one, because those filenames
    carry content digests and so carry no order at all.

    The two are restamped an hour apart because `save_report` stamps `saved_at`
    from the clock to the nearest second, and two summaries written in one test
    tie - which is the one case where this ordering has nothing to work with.
    """
    _two_reports(tmp_path)
    _fake_narrative(monkeypatch)
    _run(monkeypatch, tmp_path)
    older = _summaries(tmp_path)[0]

    _fake_narrative(monkeypatch, _narrative(headline="A later briefing entirely."))
    _run(monkeypatch, tmp_path, "--force")
    newer = next(path for path in _summaries(tmp_path) if path != older)
    _restamp(older, "2026-09-11T10:00:00Z")
    _restamp(newer, "2026-09-11T11:00:00Z")
    capsys.readouterr()

    _run(monkeypatch, tmp_path)
    out = capsys.readouterr().out

    assert len(_summaries(tmp_path)) == 2
    assert f"Summary already saved for these 2 reports: {newer}" in out
    assert str(older) not in out


def test_the_older_of_two_summaries_wins_only_if_it_is_actually_newer(
    monkeypatch, tmp_path, capsys
):
    """The mirror of the test above, so it cannot pass by accident: with the
    timestamps swapped, the OTHER file is the one named."""
    _two_reports(tmp_path)
    _fake_narrative(monkeypatch)
    _run(monkeypatch, tmp_path)
    first = _summaries(tmp_path)[0]

    _fake_narrative(monkeypatch, _narrative(headline="A different briefing."))
    _run(monkeypatch, tmp_path, "--force")
    second = next(path for path in _summaries(tmp_path) if path != first)
    _restamp(first, "2026-09-11T11:00:00Z")
    _restamp(second, "2026-09-11T10:00:00Z")
    capsys.readouterr()

    _run(monkeypatch, tmp_path)

    assert f"Summary already saved for these 2 reports: {first}" in capsys.readouterr().out


# --- --language ---------------------------------------------------------------
#
# `translate_briefing` is patched at `summary_cli.translate_briefing` - the name
# as imported into the module under test - so no test below reaches a model. The
# fake returns a document built from the REAL briefing it was handed, with only
# the unindented prose lines altered, which is what a faithful translator would
# produce. That way the assertions about tables surviving are assertions about
# the CLI's own wiring and not about a hand-written fixture.


def _fake_translation(monkeypatch, raises=None, kept_english=(), mangle_tables=False):
    """Patch `translate_briefing` and hand back the list of calls it received."""
    calls: list = []

    def fake(body, language, literals=(), model=None, month=""):
        calls.append(
            {
                "body": body,
                "language": language,
                "literals": literals,
                "model": model,
                "month": month,
            }
        )
        if raises:
            raise raises
        out = []
        for line in body.split("\n"):
            if mangle_tables and line.startswith("  "):
                out.append("  MANGLED")
            elif line.strip() and not line.startswith("  "):
                out.append(f"[{language}] {line}")
            else:
                out.append(line)
        return "\n".join(out), "這是機器翻譯。", tuple(kept_english)

    monkeypatch.setattr(summary_cli, "translate_briefing", fake)
    return calls


def _translations(tmp_path, language="zh-TW"):
    folder = tmp_path / "output" / MONTH
    if not folder.is_dir():
        return []
    return sorted(p for p in folder.glob(f"*summary-{language}-*.md"))


def test_language_writes_a_second_file_beside_the_english_one(monkeypatch, tmp_path):
    _two_reports(tmp_path)
    _fake_narrative(monkeypatch, _narrative())
    _fake_translation(monkeypatch)
    _run(monkeypatch, tmp_path, "--language", "zh-TW")
    assert len(_summaries(tmp_path)) == 2
    assert len(_translations(tmp_path)) == 1


def test_the_english_summary_is_still_written_when_a_translation_is_asked_for(
    monkeypatch, tmp_path
):
    _two_reports(tmp_path)
    _fake_narrative(monkeypatch, _narrative())
    _fake_translation(monkeypatch)
    _run(monkeypatch, tmp_path, "--language", "zh-TW")
    english = [p for p in _summaries(tmp_path) if p not in _translations(tmp_path)]
    assert len(english) == 1
    assert "language:" not in load_report(english[0])[0]


def test_the_translated_file_carries_its_language_and_provenance(monkeypatch, tmp_path):
    _two_reports(tmp_path)
    _fake_narrative(monkeypatch, _narrative())
    _fake_translation(monkeypatch)
    _run(monkeypatch, tmp_path, "--language", "zh-TW")
    facts, _ = load_report(_translations(tmp_path)[0])
    assert facts["language"] == "zh-TW"
    assert facts["translation_status"] == "written"
    assert facts["translated_from"]
    assert facts["sources_digest"]


def test_the_translated_filename_carries_the_language_token(monkeypatch, tmp_path):
    _two_reports(tmp_path)
    _fake_narrative(monkeypatch, _narrative())
    _fake_translation(monkeypatch)
    _run(monkeypatch, tmp_path, "--language", "zh-TW")
    assert "summary-zh-TW-" in _translations(tmp_path)[0].name


def test_the_translated_document_keeps_every_table_line_byte_for_byte(monkeypatch, tmp_path):
    # The assertion the whole translation design exists for.
    _two_reports(tmp_path)
    _fake_narrative(monkeypatch, _narrative())
    _fake_translation(monkeypatch)
    _run(monkeypatch, tmp_path, "--language", "zh-TW")
    english = [p for p in _summaries(tmp_path) if p not in _translations(tmp_path)][0]
    english_tables = [l for l in load_report(english)[1].split("\n") if l.startswith("  ")]
    chinese_tables = [
        l for l in load_report(_translations(tmp_path)[0])[1].split("\n") if l.startswith("  ")
    ]
    assert english_tables
    assert english_tables == chinese_tables


def test_the_translator_is_given_the_months_protected_literals(monkeypatch, tmp_path):
    _two_reports(tmp_path)
    _fake_narrative(monkeypatch, _narrative())
    calls = _fake_translation(monkeypatch)
    _run(monkeypatch, tmp_path, "--language", "zh-TW")
    assert "NVDA" in calls[0]["literals"]
    assert "USD" in calls[0]["literals"]


def test_an_english_summary_does_not_block_a_translation_of_the_same_reports(
    monkeypatch, tmp_path
):
    _two_reports(tmp_path)
    _fake_narrative(monkeypatch, _narrative())
    _fake_translation(monkeypatch)
    _run(monkeypatch, tmp_path)
    assert len(_summaries(tmp_path)) == 1
    _run(monkeypatch, tmp_path, "--language", "zh-TW")
    assert len(_summaries(tmp_path)) == 2


def test_a_translation_does_not_block_recognizing_the_english_summary(monkeypatch, tmp_path):
    _two_reports(tmp_path)
    _fake_narrative(monkeypatch, _narrative())
    _fake_translation(monkeypatch)
    _run(monkeypatch, tmp_path, "--language", "zh-TW")
    before = len(_summaries(tmp_path))
    _run(monkeypatch, tmp_path)
    assert len(_summaries(tmp_path)) == before


def test_language_on_an_already_summarized_month_needs_no_prose_call(monkeypatch, tmp_path):
    # The valuable middle case: the English briefing is on disk, so only the
    # translation is missing and a prose pass would be wasted spend.
    _two_reports(tmp_path)
    _fake_narrative(monkeypatch, _narrative())
    _fake_translation(monkeypatch)
    _run(monkeypatch, tmp_path)

    def refuse(*args, **kwargs):
        raise AssertionError("the prose pass must not run when the English briefing exists")

    monkeypatch.setattr(summary_cli, "generate_narrative", refuse)
    calls = _fake_translation(monkeypatch)
    _run(monkeypatch, tmp_path, "--language", "zh-TW")
    assert len(calls) == 1
    assert len(_translations(tmp_path)) == 1


def test_reusing_the_saved_english_translates_the_bytes_on_disk(monkeypatch, tmp_path):
    _two_reports(tmp_path)
    _fake_narrative(monkeypatch, _narrative())
    _fake_translation(monkeypatch)
    _run(monkeypatch, tmp_path)
    english = _summaries(tmp_path)[0]
    saved_body = load_report(english)[1]

    monkeypatch.setattr(summary_cli, "generate_narrative", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("no prose pass")
    ))
    calls = _fake_translation(monkeypatch)
    _run(monkeypatch, tmp_path, "--language", "zh-TW")
    assert calls[0]["body"].strip() == saved_body.strip()


def test_a_repeat_run_in_the_same_language_writes_nothing(monkeypatch, tmp_path, capsys):
    _two_reports(tmp_path)
    _fake_narrative(monkeypatch, _narrative())
    _fake_translation(monkeypatch)
    _run(monkeypatch, tmp_path, "--language", "zh-TW")
    before = _summaries(tmp_path)
    capsys.readouterr()
    _run(monkeypatch, tmp_path, "--language", "zh-TW")
    out = capsys.readouterr().out
    assert _summaries(tmp_path) == before
    assert "already saved" in out
    assert "zh-TW" in out


def test_two_languages_of_one_month_produce_two_translations(monkeypatch, tmp_path):
    _two_reports(tmp_path)
    _fake_narrative(monkeypatch, _narrative())
    _fake_translation(monkeypatch)
    _run(monkeypatch, tmp_path, "--language", "zh-TW")
    _fake_translation(monkeypatch)
    _run(monkeypatch, tmp_path, "--language", "ja")
    assert len(_translations(tmp_path, "zh-TW")) == 1
    assert len(_translations(tmp_path, "ja")) == 1
    assert len(_summaries(tmp_path)) == 3


def test_force_with_language_rewrites_both(monkeypatch, tmp_path):
    _two_reports(tmp_path)
    _fake_narrative(monkeypatch, _narrative())
    _fake_translation(monkeypatch)
    _run(monkeypatch, tmp_path, "--language", "zh-TW")
    _fake_narrative(monkeypatch, _narrative(headline="A different headline entirely."))
    _fake_translation(monkeypatch)
    _run(monkeypatch, tmp_path, "--language", "zh-TW", "--force")
    assert len(_summaries(tmp_path)) == 4


def test_stdout_with_language_prints_both_and_writes_nothing(monkeypatch, tmp_path, capsys):
    _two_reports(tmp_path)
    _fake_narrative(monkeypatch, _narrative())
    _fake_translation(monkeypatch)
    _run(monkeypatch, tmp_path, "--language", "zh-TW", "--stdout")
    out = capsys.readouterr().out
    assert _summaries(tmp_path) == []
    assert "[zh-TW]" in out
    assert "Portfolio archive summary" in out


def test_language_with_no_llm_exits_two_and_names_both_flags(monkeypatch, tmp_path, capsys):
    _two_reports(tmp_path)
    with pytest.raises(SystemExit) as exit_info:
        _run(monkeypatch, tmp_path, "--language", "zh-TW", "--no-llm")
    assert exit_info.value.code == 2
    err = capsys.readouterr().err
    assert "--language" in err and "--no-llm" in err


def test_a_failed_translation_keeps_the_english_and_exits_zero(monkeypatch, tmp_path, capsys):
    _two_reports(tmp_path)
    _fake_narrative(monkeypatch, _narrative())
    _fake_translation(
        monkeypatch, raises=summary_cli.TranslationUnavailable("model refused the job")
    )
    _run(monkeypatch, tmp_path, "--language", "zh-TW")
    captured = capsys.readouterr()
    assert len(_summaries(tmp_path)) == 1
    assert _translations(tmp_path) == []
    assert "Warning: model refused the job" in captured.err
    assert "Portfolio archive summary" in captured.out


def test_a_partial_translation_records_how_many_blocks_stayed_english(monkeypatch, tmp_path):
    _two_reports(tmp_path)
    _fake_narrative(monkeypatch, _narrative())
    _fake_translation(monkeypatch, kept_english=("block 3: dropped marker(s) [1]",))
    _run(monkeypatch, tmp_path, "--language", "zh-TW")
    facts, body = load_report(_translations(tmp_path)[0])
    assert facts["translation_status"].startswith("partial: 1 block")
    assert "1 block(s) were kept in English" in body


def test_the_translated_document_names_the_english_file_it_came_from(monkeypatch, tmp_path):
    _two_reports(tmp_path)
    _fake_narrative(monkeypatch, _narrative())
    _fake_translation(monkeypatch)
    _run(monkeypatch, tmp_path, "--language", "zh-TW")
    english = [p for p in _summaries(tmp_path) if p not in _translations(tmp_path)][0]
    body = load_report(_translations(tmp_path)[0])[1]
    assert english.name in body
    assert "zh-TW" in body


def test_the_translated_document_carries_the_disclaimer_in_the_target_language(
    monkeypatch, tmp_path
):
    _two_reports(tmp_path)
    _fake_narrative(monkeypatch, _narrative())
    _fake_translation(monkeypatch)
    _run(monkeypatch, tmp_path, "--language", "zh-TW")
    assert "這是機器翻譯。" in load_report(_translations(tmp_path)[0])[1]


def test_a_translated_summary_is_not_read_back_as_a_source_report(monkeypatch, tmp_path, capsys):
    _two_reports(tmp_path)
    _fake_narrative(monkeypatch, _narrative())
    _fake_translation(monkeypatch)
    _run(monkeypatch, tmp_path, "--language", "zh-TW")
    capsys.readouterr()
    _run(monkeypatch, tmp_path, "--force")
    out = capsys.readouterr().out
    # Still two source reports, not four: both summaries were skipped.
    assert "2 reports (2 portfolio, 0 whatif)" in out


def test_a_free_form_language_name_still_saves_one_file(monkeypatch, tmp_path):
    _two_reports(tmp_path)
    _fake_narrative(monkeypatch, _narrative())
    _fake_translation(monkeypatch)
    _run(monkeypatch, tmp_path, "--language", "Traditional Chinese")
    assert len(_summaries(tmp_path)) == 2


def test_a_language_name_that_slugs_to_nothing_still_saves_one_file(monkeypatch, tmp_path):
    # `_slug` yields "unknown" here. The file is still correct and unique
    # because its name ends in a digest of the body, and the `language` fact
    # carries the truth.
    _two_reports(tmp_path)
    _fake_narrative(monkeypatch, _narrative())
    _fake_translation(monkeypatch)
    _run(monkeypatch, tmp_path, "--language", "繁體中文")
    assert len(_summaries(tmp_path)) == 2
    saved = [p for p in _summaries(tmp_path) if "unknown" in p.name]
    assert len(saved) == 1
    assert load_report(saved[0])[0]["language"] == "繁體中文"


# --- --pdf --------------------------------------------------------------------
#
# These run WeasyPrint for real. It is installed, needs no network, and renders
# one of these small briefings in about a tenth of a second - and the question
# worth asking here is whether the CLI hands it the right file, which a fake
# would answer by construction.


def _pdfs(tmp_path):
    folder = tmp_path / "output" / MONTH
    return sorted(p for p in folder.glob("*.pdf")) if folder.is_dir() else []


def test_pdf_writes_one_pdf_beside_the_saved_summary(monkeypatch, tmp_path):
    _two_reports(tmp_path)
    _fake_narrative(monkeypatch, _narrative())
    _run(monkeypatch, tmp_path, "--pdf")
    pdfs = _pdfs(tmp_path)
    assert len(pdfs) == 1
    assert pdfs[0].stem == _summaries(tmp_path)[0].stem
    assert pdfs[0].read_bytes().startswith(b"%PDF-")


def test_without_pdf_no_pdf_is_written(monkeypatch, tmp_path):
    _two_reports(tmp_path)
    _fake_narrative(monkeypatch, _narrative())
    _run(monkeypatch, tmp_path)
    assert _pdfs(tmp_path) == []


def test_the_pdf_notice_names_the_file_after_the_report_notice(monkeypatch, tmp_path, capsys):
    _two_reports(tmp_path)
    _fake_narrative(monkeypatch, _narrative())
    _run(monkeypatch, tmp_path, "--pdf")
    out = capsys.readouterr().out
    assert "Saved PDF: " in out
    assert out.index("Saved report: ") < out.index("Saved PDF: ")


def test_pdf_with_no_llm_renders_without_calling_the_model(monkeypatch, tmp_path):
    _two_reports(tmp_path)
    calls = _fake_narrative(monkeypatch, _narrative())
    _run(monkeypatch, tmp_path, "--pdf", "--no-llm")
    assert calls == []
    assert len(_pdfs(tmp_path)) == 1


def test_pdf_with_stdout_exits_two_and_says_which_flag_to_drop(monkeypatch, tmp_path, capsys):
    _two_reports(tmp_path)
    with pytest.raises(SystemExit) as exit_info:
        _run(monkeypatch, tmp_path, "--pdf", "--stdout")
    assert exit_info.value.code == 2
    err = capsys.readouterr().err
    assert "--pdf" in err and "--stdout" in err
    assert "Drop --stdout" in err


def test_a_repeat_run_with_pdf_renders_beside_the_summary_already_saved(
    monkeypatch, tmp_path, capsys
):
    # The "I forgot --pdf" case: no --force, no second Markdown file, no model
    # call, and a PDF at the end of it.
    _two_reports(tmp_path)
    _fake_narrative(monkeypatch, _narrative())
    _run(monkeypatch, tmp_path)
    assert _pdfs(tmp_path) == []
    capsys.readouterr()
    _run(monkeypatch, tmp_path, "--pdf")
    out = capsys.readouterr().out
    assert len(_summaries(tmp_path)) == 1
    assert len(_pdfs(tmp_path)) == 1
    assert "already saved" in out
    assert "Saved PDF: " in out


def test_a_repeat_run_with_pdf_leaves_an_existing_pdf_alone(monkeypatch, tmp_path, capsys):
    _two_reports(tmp_path)
    _fake_narrative(monkeypatch, _narrative())
    _run(monkeypatch, tmp_path, "--pdf")
    before = _pdfs(tmp_path)[0].read_bytes()
    capsys.readouterr()
    _run(monkeypatch, tmp_path, "--pdf")
    out = capsys.readouterr().out
    assert _pdfs(tmp_path)[0].read_bytes() == before
    assert "PDF already saved beside the report" in out


def test_force_with_pdf_writes_a_second_summary_and_a_second_pdf(monkeypatch, tmp_path):
    _two_reports(tmp_path)
    _fake_narrative(monkeypatch, _narrative())
    _run(monkeypatch, tmp_path, "--pdf")
    _fake_narrative(monkeypatch, _narrative(headline="An entirely different headline."))
    _run(monkeypatch, tmp_path, "--pdf", "--force")
    assert len(_summaries(tmp_path)) == 2
    assert len(_pdfs(tmp_path)) == 2
    assert {p.stem for p in _pdfs(tmp_path)} == {p.stem for p in _summaries(tmp_path)}


def test_language_with_pdf_writes_four_files(monkeypatch, tmp_path):
    """The headline behaviour: an English pair and a translated pair.

    `cjk_font_family` is patched to report a font present, because this
    assertion is about the CLI rendering a PDF beside EACH file it saved and not
    about which fonts the machine running the tests happens to have. The
    unpatched behaviour on a font-less machine is the next test, and it is what
    this container actually does.
    """
    from agentic_portfolio.flow import report_pdf

    monkeypatch.setattr(report_pdf, "cjk_font_family", lambda *a, **k: "Noto Sans CJK TC")
    _two_reports(tmp_path)
    _fake_narrative(monkeypatch, _narrative())
    _fake_translation(monkeypatch)
    _run(monkeypatch, tmp_path, "--language", "zh-TW", "--pdf")
    assert len(_summaries(tmp_path)) == 2
    assert len(_pdfs(tmp_path)) == 2
    assert {p.stem for p in _pdfs(tmp_path)} == {p.stem for p in _summaries(tmp_path)}


def test_a_translated_pdf_is_refused_without_a_cjk_font_and_the_english_one_is_not(
    monkeypatch, tmp_path, capsys
):
    """What this container really does, and what it must do.

    Both Markdown files are saved and the English PDF is rendered; only the
    Chinese PDF is refused, by name, because every character of it would print
    as an empty box. The refusal is per file rather than per run, so forgetting
    to install a font costs the translation's PDF and nothing else.
    """
    from agentic_portfolio.flow import report_pdf

    monkeypatch.setattr(report_pdf, "cjk_font_family", lambda *a, **k: None)
    _two_reports(tmp_path)
    _fake_narrative(monkeypatch, _narrative())
    _fake_translation(monkeypatch)
    _run(monkeypatch, tmp_path, "--language", "zh-TW", "--pdf")
    captured = capsys.readouterr()
    assert len(_summaries(tmp_path)) == 2
    assert len(_pdfs(tmp_path)) == 1
    assert "fonts-noto-cjk" in captured.err
    assert "Saved PDF: " in captured.out


def test_a_failed_pdf_keeps_the_briefing_and_warns(monkeypatch, tmp_path, capsys):
    _two_reports(tmp_path)
    _fake_narrative(monkeypatch, _narrative())

    def fail(path):
        raise summary_cli.PdfUnavailable("WeasyPrint could not be loaded (no libpango)")

    monkeypatch.setattr(summary_cli, "write_pdf", fail)
    _run(monkeypatch, tmp_path, "--pdf")
    captured = capsys.readouterr()
    assert len(_summaries(tmp_path)) == 1
    assert _pdfs(tmp_path) == []
    assert "Warning: no PDF" in captured.err
    assert "Portfolio archive summary" in captured.out


def test_a_pdf_in_the_month_folder_is_not_read_as_a_report(monkeypatch, tmp_path, capsys):
    _two_reports(tmp_path)
    _fake_narrative(monkeypatch, _narrative())
    _run(monkeypatch, tmp_path, "--pdf")
    capsys.readouterr()
    _run(monkeypatch, tmp_path, "--force")
    out = capsys.readouterr().out
    assert "2 reports (2 portfolio, 0 whatif)" in out
    assert ".pdf" not in out.split("Reading")[1].split("\n")[0]


def test_reusing_the_saved_english_records_that_files_digest(monkeypatch, tmp_path):
    """`translated_from` must name the English file actually translated.

    On the reuse path the English summary was written by an earlier run, so the
    digest has to come from reading that file rather than from a `SavedReport`
    this run produced - and it must be the digest of the very bytes handed to
    the translator.
    """
    _two_reports(tmp_path)
    _fake_narrative(monkeypatch, _narrative())
    _fake_translation(monkeypatch)
    _run(monkeypatch, tmp_path)
    english = _summaries(tmp_path)[0]
    english_digest = load_report(english)[0]["digest"]

    monkeypatch.setattr(
        summary_cli,
        "generate_narrative",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no prose pass")),
    )
    _fake_translation(monkeypatch)
    _run(monkeypatch, tmp_path, "--language", "zh-TW")
    assert load_report(_translations(tmp_path)[0])[0]["translated_from"] == english_digest


def test_reusing_the_saved_english_keeps_its_prose_model_on_the_translation(
    monkeypatch, tmp_path
):
    # The translated file's `llm_model` describes the prose it carries, which
    # came from the earlier run, not from this one.
    _two_reports(tmp_path)
    _fake_narrative(monkeypatch, _narrative())
    _fake_translation(monkeypatch)
    _run(monkeypatch, tmp_path, "--model", "openai/first-model")
    monkeypatch.setattr(
        summary_cli,
        "generate_narrative",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no prose pass")),
    )
    _fake_translation(monkeypatch)
    _run(monkeypatch, tmp_path, "--language", "zh-TW", "--model", "openai/second-model")
    facts = load_report(_translations(tmp_path)[0])[0]
    assert facts["llm_model"] == "openai/first-model"
    assert facts["translation_model"] == "openai/second-model"


def test_stdout_with_language_names_no_source_file(monkeypatch, tmp_path, capsys):
    # Nothing was saved, so there is no English file to point at and the
    # provenance line must not invent one.
    _two_reports(tmp_path)
    _fake_narrative(monkeypatch, _narrative())
    _fake_translation(monkeypatch)
    _run(monkeypatch, tmp_path, "--language", "zh-TW", "--stdout")
    out = capsys.readouterr().out
    assert "Translation: zh-TW" in out
    assert ", from " not in out


def test_the_month_is_passed_to_the_translator(monkeypatch, tmp_path):
    _two_reports(tmp_path)
    _fake_narrative(monkeypatch, _narrative())
    calls = _fake_translation(monkeypatch)
    _run(monkeypatch, tmp_path, "--language", "zh-TW")
    assert calls[0]["month"] == MONTH
