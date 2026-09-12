"""`uv run portfolio-summary`: turn one month of the saved report archive into
a single decision briefing.

`plans/18_saved_report_archive.md` made both report-printing commands persist
their output under `output/<YYYY-MM>/`, and said plainly that the command which
reads those files back was not part of it. This is that command.

What it adds is the facts no single saved report can state: which of a month's
runs won on which axis, which tickers every optimization agreed on and which
candidates none of them wanted, how the portfolio actually held compares with
the frontier just mapped, and where two of the month's reports are not
comparable at all.

Every figure in the output is computed by `src/agentic_portfolio/flow/report_summary.py` from the
saved front matter. The prose between the tables is written by
`src/agentic_portfolio/agents/report_summary.py` on the cheap model named by `LLM_QUICK`, and is
verified against those same figures before it is printed. `--no-llm` omits the
prose and makes no network call of any kind, which is also what happens - with
a warning, never a failure - when the model cannot be reached. The tables are
the value here; the sentences are the garnish.

The summary is saved into the month folder it describes, carrying
`kind: summary` front matter. Two consequences follow and both are deliberate.
`load_month` reads only `kind: portfolio` and `kind: whatif`, so a summary
never summarizes an earlier summary. And a repeat is recognized by a digest
over the SET of source reports rather than over the summary text, because the
prose differs between runs and a text digest would write a new file every time.
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import date
from pathlib import Path
from typing import Sequence

from agentic_portfolio.agents.report_summary import NarrativeUnavailable, generate_narrative
from agentic_portfolio.agents.summary_schema import MonthNarrative
from agentic_portfolio.config.settings import settings
from agentic_portfolio.flow.report_archive import (
    ReportArchive,
    command_line,
    format_save_notice,
    load_report,
    save_report,
)
from agentic_portfolio.flow.report_summary import (
    MonthDigest,
    ReportRecord,
    build_month_digest,
    digest_for_llm,
    load_month,
    render_digest,
)

SUMMARY_KIND = "summary"
_MONTH = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def default_month(today: date | None = None) -> str:
    """The month a bare invocation summarizes: the current calendar one."""
    return (today or date.today()).strftime("%Y-%m")


def parse_month(text: str) -> str:
    """`text` as a `YYYY-MM` month, or a `ValueError` naming what was wrong.

    Validated rather than passed through, because an unrecognized month would
    otherwise become a folder that does not exist and be reported as an empty
    archive - which is a true statement about the wrong question.
    """
    if not _MONTH.match(text):
        raise ValueError(
            f"{text!r} is not a month. Give it as YYYY-MM, for example 2026-09, "
            "or leave it out to summarize the current month"
        )
    return text


def narrative_prose(narrative: MonthNarrative | None) -> dict[str, str]:
    """One narrative mapped onto `render_digest`'s section keys.

    This mapping is the only place the two halves meet, and it lives on this
    side of the seam on purpose: `src/agentic_portfolio/flow/report_summary.py` renders text it
    is handed and knows nothing about where prose comes from.
    """
    if narrative is None:
        return {}
    return {
        "headline": narrative.headline,
        "exploration": narrative.exploration_story,
        "risk_return": narrative.risk_return_read,
        "income": narrative.income_read,
        "consensus": narrative.consensus_read,
        "holdings_gap": narrative.holdings_gap_read,
        "methodology": narrative.methodology_caution,
        "next_runs": "\n".join(f"- {entry}" for entry in narrative.next_runs),
    }


def provenance(
    model: str | None, replaced: tuple[str, ...], reason: str | None
) -> str:
    """The one line the briefing ends with, saying where its prose came from.

    A reader who cannot tell machine-written prose from a model's, or a
    complete narrative from one whose fields were rejected, has no way to judge
    how much weight to put on the sentences. So the document says.
    """
    if reason:
        return f"Prose: none - {reason}. Every figure above was computed regardless."
    if not model:
        return "Prose: none, by --no-llm. Every figure above was computed."
    if replaced:
        return (
            f"Prose: {model}, with {len(replaced)} section(s) replaced by machine-written "
            f"text for stating a figure that was never computed ({', '.join(replaced)})."
        )
    return f"Prose: {model}. Every figure above was computed, not written by the model."


def existing_summary(output_dir: str, month: str, sources_digest: str) -> Path | None:
    """The summary already covering exactly this set of source reports, if one
    was written.

    Found by reading each summary's `sources_digest` fact rather than by its
    filename, because the filename carries a digest of the summary's own text -
    which changes whenever the prose does, while the question being asked here
    is whether these same reports have already been summarized.

    The NEWEST match is returned when there are several, which happens after a
    `--force` run: the same reports then have two summaries, and the one the
    reader should be pointed at is the later one, since it is the one written by
    the current version of this command. Returning whichever sorted first by
    filename would have named the older briefing - the digests in those names
    are content hashes and carry no order at all.
    """
    folder = Path(output_dir) / month
    if not folder.is_dir():
        return None
    matches: list[tuple[str, Path]] = []
    for path in sorted(p for p in folder.glob("*.md") if not p.name.startswith(".")):
        try:
            facts, _ = load_report(path)
        except (ValueError, OSError, UnicodeDecodeError):
            continue
        if facts.get("kind") == SUMMARY_KIND and facts.get("sources_digest") == sources_digest:
            matches.append((facts.get("saved_at", ""), path))
    if not matches:
        return None
    # `saved_at` is UTC ISO-8601 to the second, so it sorts lexically; a summary
    # missing it sorts to the front and so loses to any dated one, which is the
    # safe way round. Two written within the same second tie, and the filename
    # then decides - arbitrary but stable, and there is nothing better to use.
    return max(matches, key=lambda match: (match[0], match[1].name))[1]


def _write(
    digest: MonthDigest,
    body: str,
    output_dir: str,
    month: str,
    model: str | None,
    status: str,
) -> None:
    archive = ReportArchive(
        output_dir=output_dir,
        enabled=True,
        kind=SUMMARY_KIND,
        # The month folder comes from the reports being summarized, not from
        # today, so a September archive summarized in October still files under
        # output/2026-09/ beside the reports it describes.
        as_of=digest.scope.as_of_last or date.fromisoformat(f"{month}-01"),
        command=command_line(),
    )
    saved = save_report(
        body,
        archive,
        {
            "month": month,
            "report_count": digest.scope.report_count,
            "sources_digest": digest.sources_digest,
            "llm_model": model,
            "narrative_status": status,
        },
    )
    notice = format_save_notice(saved)
    if notice:
        print(notice)


def census(records: Sequence[ReportRecord]) -> str:
    """The one-line census the `Reading ...` notice carries."""
    portfolio = sum(1 for r in records if r.kind == "portfolio")
    whatif = sum(1 for r in records if r.kind == "whatif")
    currencies = sorted({r.currency for r in records if r.currency})
    plural = "currency" if len(currencies) == 1 else "currencies"
    return (
        f"{len(records)} reports ({portfolio} portfolio, {whatif} whatif), "
        f"{len(currencies)} {plural}."
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Summarize every portfolio report saved in one month of the archive "
                    "into a single briefing: a leaderboard ranked within each returns "
                    "window, what every run agreed on, how the portfolio you hold compares, "
                    "and where two reports are not comparable.",
        epilog="Examples: portfolio-summary | portfolio-summary 2026-09 | "
               "portfolio-summary 2026-09 --no-llm --stdout",
    )
    parser.add_argument(
        "month",
        nargs="?",
        default=None,
        help="Which month of the archive to summarize, as YYYY-MM. Defaults to the current "
             "calendar month. This is the folder name under --output-dir, which comes from "
             "each report's own as-of date rather than from when it was run.",
    )
    parser.add_argument(
        "--output-dir",
        default=settings.output_dir,
        help="Directory the archive is read from and the summary is written back into, one "
             "subdirectory per month (so 'output/2026-09/'). Shared with 'uv run portfolio' "
             "and 'uv run portfolio-holdings whatif', so a month's folder holds every "
             "report those two printed.",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Model that writes the prose between the tables, overriding LLM_QUICK for this "
             f"run (currently {settings.llm_quick}). Every figure is computed either way; the "
             "model only writes sentences, and any sentence stating a figure that was not "
             "computed is replaced before you see it.",
    )
    parser.add_argument(
        "--no-llm",
        action="store_true",
        help="Print the computed tables with no prose and make no network call at all. This "
             "is also what happens automatically, with a warning, when the model cannot be "
             "reached - the figures never depend on it.",
    )
    parser.add_argument(
        "--stdout",
        action="store_true",
        help="Print the briefing and save nothing. Nothing is written under --output-dir and "
             "no directory is created, so this is how to look at a month without adding to "
             "its folder.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Write a new summary even when one already covers exactly these reports. "
             "Without it a repeat run recognizes the set of source reports by digest, says "
             "so, and writes nothing.",
    )
    args = parser.parse_args()

    try:
        month = parse_month(args.month) if args.month else default_month()
    except ValueError as error:
        parser.error(str(error))
        return

    records, skipped = load_month(args.output_dir, month)
    folder = Path(args.output_dir) / month
    for note in skipped:
        print(f"Skipped {note}")
    if not records:
        print(
            f"No reports to summarize in {folder}/ "
            "(looked for kind: portfolio or kind: whatif)."
        )
        raise SystemExit(1)

    print(f"Reading {folder}/ ... {census(records)}")

    digest = build_month_digest(records, month, skipped)

    if not args.force and not args.stdout:
        already = existing_summary(args.output_dir, month, digest.sources_digest)
        if already:
            print(
                f"Summary already saved for these {digest.scope.report_count} reports: {already}"
            )
            print("Re-run with --force to write a new one, or --stdout to print without saving.")
            return

    narrative: MonthNarrative | None = None
    replaced: tuple[str, ...] = ()
    reason: str | None = None
    model = None if args.no_llm else (args.model or settings.llm_quick)
    if not args.no_llm:
        try:
            narrative, replaced = generate_narrative(
                month, digest.scope.report_count, digest_for_llm(digest), model=model
            )
        except NarrativeUnavailable as error:
            reason = str(error)
            print(f"Warning: {reason}", file=sys.stderr)

    status = "skipped" if args.no_llm else (f"failed: {reason}" if reason else "written")
    body = render_digest(digest, narrative_prose(narrative))
    body = body.rstrip() + "\n\n" + provenance(model, replaced, reason) + "\n"

    print()
    print(body, end="")

    if args.stdout:
        return
    _write(digest, body, args.output_dir, month, model, status)

