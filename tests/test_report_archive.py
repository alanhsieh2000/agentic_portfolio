"""Tests for `src/flow/report_archive.py`, the archive that stores each
printed portfolio report under `output/<YYYY-MM>/`.

Per `AGENTS.md` these are hermetic: nothing here touches Yahoo Finance, no
DuckDB database is opened, and every file is written under pytest's `tmp_path`,
so the repository's own `output/` is never involved. The module under test has
no third-party imports at all, so there is nothing to monkeypatch - the only
seam these tests use is `tmp_path` for the archive directory and `capsys` for
the notice line, exactly as `tests/test_cli.py` reads report text.
"""

from datetime import date
from pathlib import Path

import pytest

from src.flow.report_archive import (
    FRONT_MATTER_FENCE,
    command_line,
    ReportArchive,
    capture_report,
    format_save_notice,
    load_report,
    month_dir,
    normalize_report,
    record_report,
    report_digest,
    report_filename,
    save_report,
)

BODY = """Portfolio currency: USD
Returns window: 2021-10-31 to 2026-08-31 (59 months)

Weights:
  SPY  1.0000

Expected return / volatility (annualized):
  SPY  0.0871 / 0.0451
"""


def _archive(tmp_path: Path, **overrides) -> ReportArchive:
    """A `ReportArchive` pointed at `tmp_path`. Defaults match a live
    `uv run portfolio` run so each test overrides only what it is about."""
    fields = {
        "output_dir": str(tmp_path / "output"),
        "enabled": True,
        "kind": "portfolio",
        "as_of": date(2026, 9, 10),
        "command": "portfolio --date today --objective GMV --value 100000",
    }
    fields.update(overrides)
    return ReportArchive(**fields)


def _facts(**overrides) -> dict[str, object]:
    facts: dict[str, object] = {
        "variant": "initial",
        "currency": "USD",
        "objective": "GMV",
        "selection": "user_provided",
        "value": 100000.0,
        "candidates": ("AVB", "SPY"),
        "risk_free_rate": 0.02,
        "window_start": date(2021, 10, 31),
        "window_end": date(2026, 8, 31),
        "window_months": 59,
        "annual_return": 0.0871,
        "annual_volatility": 0.0451,
        "sharpe": 1.4878,
    }
    facts.update(overrides)
    return facts


def test_digest_ignores_trailing_whitespace_and_surrounding_blank_lines():
    """Cosmetic spacing a print site can acquire without any figure changing
    must not defeat deduplication."""
    assert report_digest(BODY) == report_digest("\n\n" + BODY + "   \n\n")
    assert report_digest(BODY) == report_digest(BODY.replace("Weights:", "Weights:   "))


def test_digest_changes_when_a_figure_changes():
    """The other half of the same contract: a different portfolio is a
    different report."""
    assert report_digest(BODY) != report_digest(BODY.replace("0.0871", "0.0872"))


def test_normalize_report_strips_per_line_trailing_space():
    assert normalize_report("\n\n  a  \n b \n\n") == "  a\n b"


def test_month_folder_comes_from_the_as_of_date_not_today(tmp_path):
    """A historical-date run files under the month it measured. This is the
    difference between an archive of market months and an archive of when
    somebody happened to be at the keyboard."""
    archive = _archive(tmp_path, as_of=date(2024, 3, 29))
    assert month_dir(archive).name == "2024-03"

    saved = save_report(BODY, archive, _facts())
    assert saved is not None
    assert saved.path.parent.name == "2024-03"


def test_filename_names_the_run_and_ends_with_eight_digest_characters(tmp_path):
    archive = _archive(tmp_path)
    facts = _facts()
    digest = report_digest(BODY)
    name = report_filename(archive, facts, digest)

    assert name == f"2026-09-10-portfolio-GMV-user_provided-USD-{digest[:8]}.md"


def test_filename_omits_facts_a_whatif_report_does_not_have(tmp_path):
    """`whatif` reports a held portfolio, which has no objective and no
    selection, so those tokens are absent rather than spelled `None`."""
    archive = _archive(tmp_path, kind="whatif")
    digest = report_digest(BODY)
    name = report_filename(archive, {"currency": "JPY"}, digest)

    assert name == f"2026-09-10-whatif-JPY-{digest[:8]}.md"


def test_filename_cannot_escape_the_month_folder(tmp_path):
    """A currency or kind carrying a path separator must not write outside the
    archive. Objectives and currency codes are safe in practice; this is the
    guard that keeps a hand-edited memory file from mattering."""
    archive = _archive(tmp_path)
    name = report_filename(archive, {"currency": "../../etc", "objective": "G/MV"}, "abcdef1234")

    assert "/" not in name
    assert ".." not in name


def test_front_matter_round_trips_through_load_report(tmp_path):
    archive = _archive(tmp_path)
    saved = save_report(BODY, archive, _facts())
    assert saved is not None

    facts, body = load_report(saved.path)

    assert body == normalize_report(BODY)
    assert facts["digest"] == saved.digest
    assert facts["as_of"] == "2026-09-10"
    assert facts["kind"] == "portfolio"
    assert facts["variant"] == "initial"
    assert facts["currency"] == "USD"
    assert facts["objective"] == "GMV"
    assert facts["selection"] == "user_provided"
    assert facts["candidates"] == "AVB, SPY"
    assert facts["window_months"] == "59"
    assert facts["saved_at"].endswith("Z")


def test_ratios_are_four_decimal_fractions_and_money_is_two(tmp_path):
    """One unit convention throughout, the same one every ratio in this
    project's reports already prints under."""
    saved = save_report(BODY, _archive(tmp_path), _facts(total_value=850722.0))
    assert saved is not None

    facts, _ = load_report(saved.path)

    assert facts["sharpe"] == "1.4878"
    assert facts["risk_free_rate"] == "0.0200"
    assert facts["value"] == "100000.00"
    assert facts["total_value"] == "850722.00"


def test_a_none_fact_is_omitted_rather_than_written_as_the_word_none(tmp_path):
    """An absent benchmark and a benchmark literally named "None" must not read
    the same to the command that parses these files later."""
    saved = save_report(BODY, _archive(tmp_path), _facts(benchmark=None, sharpe=None))
    assert saved is not None

    facts, _ = load_report(saved.path)

    assert "benchmark" not in facts
    assert "sharpe" not in facts


def test_a_fact_not_in_the_stable_order_is_still_written(tmp_path):
    """Adding a fact at a call site must never silently lose it."""
    saved = save_report(BODY, _archive(tmp_path), _facts(experiment="something new"))
    assert saved is not None

    facts, _ = load_report(saved.path)

    assert facts["experiment"] == "something new"


def test_a_value_containing_a_colon_survives_the_round_trip(tmp_path):
    """`command` and any provenance phrase can contain one, so keys are split
    on the first colon only."""
    archive = _archive(tmp_path, command="portfolio --date today  # note: a colon")
    saved = save_report(BODY, archive, _facts())
    assert saved is not None

    facts, _ = load_report(saved.path)

    assert facts["command"] == "portfolio --date today  # note: a colon"


def test_facts_are_written_in_a_stable_order(tmp_path):
    """A stable order is what makes two saved reports diffable."""
    saved = save_report(BODY, _archive(tmp_path), _facts())
    assert saved is not None

    lines = saved.path.read_text(encoding="utf-8").splitlines()
    keys = [line.split(":", 1)[0] for line in lines[1 : lines.index(FRONT_MATTER_FENCE, 1)]]

    assert keys.index("digest") < keys.index("as_of") < keys.index("currency")
    assert keys.index("window_start") < keys.index("annual_return")


def test_an_empty_body_is_refused(tmp_path):
    """A file of front matter alone would be a row in the eventual comparison
    with nothing to compare."""
    archive = _archive(tmp_path)

    assert save_report("   \n\n  ", archive, _facts()) is None
    assert not month_dir(archive).exists()


def test_saving_the_same_report_twice_leaves_one_file(tmp_path):
    """The deduplication requirement, and the idempotence proof."""
    archive = _archive(tmp_path)

    first = save_report(BODY, archive, _facts())
    second = save_report(BODY, archive, _facts())

    assert first is not None and second is not None
    assert first.created is True
    assert second.created is False
    assert first.path == second.path
    assert list(month_dir(archive).iterdir()) == [first.path]


def test_a_repeat_does_not_rewrite_the_stored_file(tmp_path):
    """`saved_at` records when a report was FIRST seen, so a repeat must not
    touch the file at all."""
    archive = _archive(tmp_path)
    first = save_report(BODY, archive, _facts())
    assert first is not None

    before = first.path.read_bytes()
    save_report(BODY, archive, _facts())

    assert first.path.read_bytes() == before


def test_a_different_report_gets_its_own_file(tmp_path):
    archive = _archive(tmp_path)

    save_report(BODY, archive, _facts())
    save_report(BODY.replace("0.0871", "0.0999"), archive, _facts(objective="MSR"))

    assert len(list(month_dir(archive).iterdir())) == 2


def test_no_temporary_files_are_left_behind(tmp_path):
    archive = _archive(tmp_path)
    save_report(BODY, archive, _facts())

    assert [p.name for p in month_dir(archive).iterdir() if p.name.startswith(".report-")] == []


def test_load_report_refuses_a_file_it_did_not_write(tmp_path):
    stray = tmp_path / "hand-written.md"
    stray.write_text("Portfolio currency: USD\n", encoding="utf-8")

    with pytest.raises(ValueError, match="front-matter fence"):
        load_report(stray)


def test_load_report_refuses_an_unterminated_front_matter_block(tmp_path):
    stray = tmp_path / "truncated.md"
    stray.write_text(f"{FRONT_MATTER_FENCE}\nkind: portfolio\n", encoding="utf-8")

    with pytest.raises(ValueError, match="unterminated"):
        load_report(stray)


def test_command_line_records_a_retypable_command():
    """The absolute path a console script is installed at is a fact about the
    container, not about the run."""
    assert command_line(
        ["/app/agentic_portfolio/.venv/bin/portfolio", "--date", "today", "--value", "100000"]
    ) == "portfolio --date today --value 100000"


def test_command_line_of_no_arguments_is_empty():
    assert command_line([]) == ""


def test_capture_report_is_byte_identical_to_what_reached_the_terminal(capsys):
    """The premise the whole design rests on: teeing `sys.stdout` yields the
    very bytes the reader saw, and composes with the stdout `capsys` installs.
    """
    with capture_report() as captured:
        print("Portfolio currency: USD")
        print("Weights:")
        text = captured()

    assert capsys.readouterr().out == text


def test_record_report_saves_what_the_block_printed_and_names_the_file(tmp_path, capsys):
    archive = _archive(tmp_path)

    with record_report(archive, **_facts()):
        print(BODY.rstrip())

    out = capsys.readouterr().out
    saved = next(month_dir(archive).iterdir())

    assert BODY.rstrip() in out
    assert f"Saved report: {saved}" in out
    _, body = load_report(saved)
    assert body == normalize_report(BODY)


def test_record_report_reports_a_repeat_rather_than_writing_again(tmp_path, capsys):
    archive = _archive(tmp_path)

    with record_report(archive, **_facts()):
        print(BODY.rstrip())
    capsys.readouterr()
    with record_report(archive, **_facts()):
        print(BODY.rstrip())

    assert "Report already saved this month:" in capsys.readouterr().out
    assert len(list(month_dir(archive).iterdir())) == 1


def test_the_notice_is_not_part_of_the_digest(tmp_path):
    """The notice prints after the capture closes, so archiving a report twice
    in one session cannot produce two different digests for it."""
    archive = _archive(tmp_path)

    with record_report(archive, **_facts()):
        print(BODY.rstrip())
    saved = next(month_dir(archive).iterdir())
    _, body = load_report(saved)

    assert "Saved report:" not in body


@pytest.mark.parametrize("archive_arg", ["none", "disabled"])
def test_record_report_without_an_archive_writes_and_prints_nothing(
    tmp_path, capsys, archive_arg
):
    """The `--no-save-reports` path, and the default for every existing caller
    of `print_pipeline_result`: byte-identical to life before this module."""
    archive = None if archive_arg == "none" else _archive(tmp_path, enabled=False)

    with record_report(archive, **_facts()):
        print(BODY.rstrip())

    assert capsys.readouterr().out == BODY.rstrip() + "\n"
    assert not (tmp_path / "output").exists()


def test_record_report_saves_nothing_when_the_block_raises(tmp_path, capsys):
    """A report that did not finish printing is not a report - but what did
    print has still reached the reader."""
    archive = _archive(tmp_path)

    with pytest.raises(ZeroDivisionError):
        with record_report(archive, **_facts()):
            print("Portfolio currency: USD")
            1 / 0

    assert "Portfolio currency: USD" in capsys.readouterr().out
    assert not month_dir(archive).exists()
