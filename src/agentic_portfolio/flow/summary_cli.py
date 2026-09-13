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
from typing import Mapping, Sequence

from agentic_portfolio.agents.report_summary import NarrativeUnavailable, generate_narrative
from agentic_portfolio.agents.report_translation import (
    TranslationUnavailable,
    translate_briefing,
)
from agentic_portfolio.agents.summary_schema import MonthNarrative
from agentic_portfolio.config.settings import settings
from agentic_portfolio.flow.report_archive import (
    ReportArchive,
    SavedReport,
    command_line,
    format_save_notice,
    load_report,
    save_report,
)
from agentic_portfolio.flow.report_pdf import (
    PdfUnavailable,
    format_pdf_notice,
    write_pdf,
)
from agentic_portfolio.flow.report_summary import (
    MonthDigest,
    ReportRecord,
    build_month_digest,
    digest_for_llm,
    load_month,
    protected_literals,
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


def translation_provenance(
    language: str, model: str, source: str | None, kept_english: Sequence[str]
) -> str:
    """The audit line a translated briefing ends with, in English.

    Deliberately English even though the document above it is not, and it sits
    beneath the model's own disclaimer in the target language. The two readers
    are different people: the person reading the translation needs to know it
    was machine-translated, which the disclaimer tells them in their own
    language, while anyone checking the archive needs to know which model, which
    source file, and how much of the document is not actually translated - and
    that reader may only read English.

    Naming the count matters for the same reason `provenance` names replaced
    prose sections. A document that is nine tenths translated looks finished,
    and a reader who cannot tell that from a complete one has no way to judge
    the English sentences they hit.
    """
    parts = [f"Translation: {language} by {model}"]
    if source:
        parts.append(f"from {source}")
    line = ", ".join(parts) + "."
    if kept_english:
        line += (
            f" {len(kept_english)} block(s) were kept in English because the translation "
            "changed a figure, a ticker or a filename."
        )
    return line + " Every figure and every table above is the English original's."


def existing_summary(
    output_dir: str, month: str, sources_digest: str, language: str | None = None
) -> Path | None:
    """The summary already covering exactly this set of source reports IN THIS
    LANGUAGE, if one was written.

    `language` defaults to `None`, meaning the English summary, and an absent
    `language` fact is normalized to `None` on the way in. That is what lets the
    summaries written before translation existed keep being recognized, and it
    is why the English file carries no `language: en` - a reader should not have
    to learn that an absent fact and `en` mean the same thing.

    Matching on the PAIR is what stops an English summary from blocking a
    Chinese one, and vice versa: the two cover the same reports and are both
    legitimately present.

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
        if (
            facts.get("kind") == SUMMARY_KIND
            and facts.get("sources_digest") == sources_digest
            and (facts.get("language") or None) == (language or None)
        ):
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
    extra: Mapping[str, object] | None = None,
) -> SavedReport | None:
    """Save one briefing and name the file, returning where it went.

    The `SavedReport` is returned as well as announced because `--pdf` needs the
    path of the Markdown file that was just written, and because a translation
    needs to record the digest of the English summary it was derived from.
    `extra` carries the facts that only some summaries have - a translation's
    `language` and provenance - since a fact whose value is absent is omitted
    from the file entirely.
    """
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
    facts: dict[str, object] = {
        "month": month,
        "report_count": digest.scope.report_count,
        "sources_digest": digest.sources_digest,
        "llm_model": model,
        "narrative_status": status,
    }
    facts.update(extra or {})
    saved = save_report(body, archive, facts)
    notice = format_save_notice(saved)
    if notice:
        print(notice)
    return saved


def _render_pdf(path: Path | str | None) -> None:
    """Render the PDF beside one saved briefing, or say why there is none.

    Takes a PATH rather than a `SavedReport` so that the already-saved branch
    can use it too, where nothing was written this run and all that exists is a
    file on disk.

    Never raises. A PDF is a rendering of the briefing and the briefing is the
    product, so a failure here degrades to a warning and leaves the exit status
    alone - the same disposition `NarrativeUnavailable` and
    `TranslationUnavailable` get in `main`. The one thing this must not do is
    cost the reader a document they already have.
    """
    if path is None:
        return
    try:
        notice = format_pdf_notice(write_pdf(path))
    except PdfUnavailable as error:
        print(f"Warning: no PDF - {error}", file=sys.stderr)
        return
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
        help="Model for this run's language work, overriding LLM_QUICK (currently "
             f"{settings.llm_quick}). That means the prose between the tables and, with "
             "--language, the translation as well. Every figure is computed either way: the "
             "model writes sentences only, any sentence stating a figure that was not computed "
             "is replaced before you see it, and a translation is never shown a table at all.",
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
             "so, and writes nothing. With --language this rewrites both files, and costs a "
             "prose call and a translation call rather than one of each.",
    )
    parser.add_argument(
        "--language",
        default=None,
        metavar="LANG",
        help="Also save a translation of the briefing, as its own file beside the English "
             "one. Give a BCP-47 tag or a plain language name - 'zh-TW', 'Traditional "
             "Chinese' and 'Japanese' all work, and there is no list of accepted values. "
             "The English summary is always written first and the translation is derived "
             "from it, so the two cannot disagree. Every table and every figure is copied "
             "from the English original unchanged - the model that translates is never shown "
             "a table - and any sentence whose figures do not survive translation is left in "
             "English rather than guessed at, with the count said at the end.",
    )
    parser.add_argument(
        "--pdf",
        action="store_true",
        help="Also render a PDF beside every Markdown file this run saves - the same name "
             "with a .pdf suffix - so with --language you get one of each. The tables are "
             "rendered as fixed-width text, because that is the only thing that keeps seven "
             "columns in line on a page. No network call is involved. A briefing that "
             "contains Chinese needs a CJK font installed (on Debian: fonts-noto-cjk) and "
             "says so rather than printing a page of empty boxes. The Markdown is saved "
             "either way: a PDF that cannot be rendered is a warning, never a failure.",
    )
    args = parser.parse_args()

    # Refused rather than ignored, because the two flags ask for opposite
    # things: --no-llm promises no network call of any kind, and a translation
    # is written by a model. Silently dropping one of them would be the worst
    # option - this codebase refuses contradictions everywhere else.
    if args.language and args.no_llm:
        parser.error(
            "--language cannot be combined with --no-llm: a translation is written by a "
            "model, and --no-llm makes no network call at all. Drop one of them"
        )

    # Also a contradiction, and refused for the same reason: --stdout promises
    # to save nothing, so there is no Markdown file for a PDF to sit beside.
    # argparse's own mutually-exclusive group would say "not allowed with
    # argument --stdout", which names the conflict but not the way out; every
    # other refusal in this command is a sentence.
    if args.pdf and args.stdout:
        parser.error(
            "--pdf renders a PDF beside a saved Markdown file, and --stdout saves nothing. "
            "Drop --stdout to save both, or drop --pdf to just look at the month"
        )

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

    # Two lookups, because an English summary and a translation of it are both
    # legitimately present and neither should block the other. `--force` and
    # `--stdout` skip both: one is an explicit instruction to write anyway, the
    # other writes nothing at all.
    english_saved: Path | None = None
    translated_saved: Path | None = None
    if not args.force and not args.stdout:
        english_saved = existing_summary(args.output_dir, month, digest.sources_digest)
        if args.language:
            translated_saved = existing_summary(
                args.output_dir, month, digest.sources_digest, args.language
            )
        if english_saved and (translated_saved or not args.language):
            print(
                f"Summary already saved for these {digest.scope.report_count} reports: "
                f"{english_saved}"
            )
            if translated_saved:
                print(f"Translation into {args.language} already saved: {translated_saved}")
            print("Re-run with --force to write a new one, or --stdout to print without saving.")
            # `--pdf` still does its job here. "I already have the Markdown, now
            # give me a PDF" is a real request, and answering it costs one
            # render and no model call - which is the whole reason `write_pdf`
            # reads the file rather than a body this run rendered.
            if args.pdf:
                _render_pdf(english_saved)
                _render_pdf(translated_saved)
            return

    # The valuable middle case: the English briefing is already on disk and only
    # the translation is missing, so there is nothing for a prose pass to do.
    # Translating the SAVED bytes rather than a fresh rendering is also what
    # makes `translated_from` provably the digest of the text translated.
    reusing_english = bool(english_saved and args.language and not translated_saved)

    narrative: MonthNarrative | None = None
    replaced: tuple[str, ...] = ()
    reason: str | None = None
    model = None if args.no_llm else (args.model or settings.llm_quick)
    english: SavedReport | None = None

    reused_digest: str | None = None
    if reusing_english:
        english_facts, body = load_report(english_saved)
        model = english_facts.get("llm_model") or None
        status = english_facts.get("narrative_status", "written")
        # Kept from this one read rather than re-read below: the translation
        # records the digest of the text it was actually handed, and reading the
        # file twice would let those two drift if it changed in between.
        reused_digest = english_facts.get("digest")
        print(f"Reusing the English briefing already saved: {english_saved}")
    else:
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

        if not args.stdout:
            english = _write(digest, body, args.output_dir, month, model, status)
            if args.pdf and english:
                _render_pdf(english.path)

    if not args.language:
        return

    # Its own local rather than reusing `model`, which on the reuse path holds the
    # model that wrote the SAVED briefing's prose - an earlier run's, possibly a
    # different one. `--no-llm` cannot reach here, so this is always resolved.
    language_model = args.model or settings.llm_quick

    try:
        translated, disclaimer, kept_english = translate_briefing(
            body,
            args.language,
            literals=protected_literals(digest, records),
            model=language_model,
            month=month,
        )
    except TranslationUnavailable as error:
        # The same degradation the prose pass gets, for the same reason: the
        # briefing is the product and the translation is a second copy of it.
        print(f"Warning: {error}", file=sys.stderr)
        return

    # Whichever English file this translation came from: the one just written, or
    # the one already on disk that was reused. Under `--stdout` there is neither,
    # and the provenance line simply omits the source.
    source = english.path if english else (Path(english_saved) if english_saved else None)
    source_name = source.name if source else None
    translated = translated.rstrip() + "\n\n"
    if disclaimer:
        translated += disclaimer.rstrip() + "\n"
    translated += translation_provenance(
        args.language, language_model, source_name, kept_english
    ) + "\n"

    print()
    print(translated, end="")

    if args.stdout:
        return
    saved_translation = _write(
        digest,
        translated,
        args.output_dir,
        month,
        model,
        status,
        extra={
            "language": args.language,
            "translation_model": language_model,
            "translated_from": english.digest if english else reused_digest,
            "translation_status": (
                "written" if not kept_english
                else f"partial: {len(kept_english)} block(s) kept in English"
            ),
        },
    )
    if args.pdf and saved_translation:
        _render_pdf(saved_translation.path)

