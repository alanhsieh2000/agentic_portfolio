"""Archive the portfolio reports the two entry points print, one file per
distinct report, under `output/<YYYY>-<MM>/`.

Why this exists. Both `uv run portfolio` (`src/agentic_portfolio/flow/cli.py`) and
`uv run portfolio-holdings whatif` (`src/agentic_portfolio/flow/holdings_cli.py`) answer "how
good is this portfolio?" by printing thirty to sixty lines. Choosing between
portfolios means running them many times - three objectives, several candidate
pools, a dividend floor at two levels, a returns window at 36 months and at
60 - and after a handful of runs the earlier reports have scrolled out of the
terminal. Re-running the first one to compare it against the fourth costs, in
live mode, the entire Yahoo Finance snapshot again. So every report is also
written here, grouped by month, and an analysis can be spread over several
sittings.

How the text is obtained, and why that way. Report text in this project is
produced by roughly 109 `print()` calls in `src/agentic_portfolio/flow/cli.py`; there is no
string-returning renderer, and rewriting one would touch every assertion in
`tests/test_cli.py`. More importantly this codebase repeatedly promises
byte-identical output (`src/agentic_portfolio/flow/holdings_cli.py` says a default run's report
is "byte-identical to what it printed before this existed"). So the text is
captured by TEEING `sys.stdout` - `_Tee` below forwards every write to the
real stream and keeps a copy - which makes that promise true by construction
rather than something to re-verify at twenty print sites. `record_report` is
the only thing either CLI calls.

What counts as one report. For `uv run portfolio` it is
`print_weights_and_allocation`'s block plus the concentration note: the
portfolio itself, from the currency line down to the leftover cash. That
section, and not the whole of `print_pipeline_result`, because it is the one
part BOTH report paths print - `print_pipeline_result` reaches it, and the
interactive edit loop calls it directly - so a digest over it recognizes an
edit that lands back on the initial portfolio as the same report. The header
facts printed above it (mode, date, objective, selection, candidate list) are
not lost; they are written as front-matter fields, which is where a later
side-by-side summary wants them anyway, as values rather than as a sentence.
Deliberately excluded: the holdings block, identical across every variant in a
session, and the stale-share-count warning, which is a fact about
`memory/portfolio.json` rather than about the portfolio and would otherwise
give one portfolio two digests depending on cache state.

Deduplication. The first eight characters of the body's SHA-256 are in the
filename, so "have I already saved this exact report this month?" is
`path.exists()`. There is deliberately no index file: an index would be a
second thing to write atomically, to repair when corrupt, and to keep
consistent with the directory it describes, and the digest-in-the-name scheme
is inherently scoped to the month folder, which is the scope asked for.

Front matter, not a sidecar. Each file is a `---`-delimited block of
`key: value` lines followed by the report body, so one report is one atomic
write with no metadata to orphan. It carries the headline figures
(`annual_return`, `annual_volatility`, `sharpe`, `dividend_yield`) as well as
the run's inputs, which is what turns the follow-up comparison command into a
table build rather than a prose parser. `saved_at` is metadata only and is NOT
part of the digest, so re-running an identical report is a no-op and the
surviving front matter records when that report was FIRST seen. Values are
scalars only, so no YAML library is needed - and none is used, in either
direction: `load_report` reads them back with plain string operations.

This module is the only writer under `output/`. `output/` is gitignored, which
has one consequence worth stating: anything that writes there by mistake leaves
`git status` clean and is therefore invisible, which is why `tests/conftest.py`
redirects `settings.output_dir` for every test in the suite rather than relying
on each test to pass `--output-dir`.
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import sys
import tempfile
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager, redirect_stdout
from datetime import date, datetime, timezone
from pathlib import Path
from typing import NamedTuple

from agentic_portfolio.config.settings import settings

DEFAULT_OUTPUT_DIR = settings.output_dir
"""Taken from `src/agentic_portfolio/config/settings.py`'s `output_dir`, which
is what both CLIs read as their `--output-dir` default.

This used to be the literal `"output"`, kept separate so the module needed no
settings import. It now comes from `Settings` for one reason: every default
path in this project is resolved against `AGENTIC_PORTFOLIO_HOME`, and a
constant that opted out of that would put reports somewhere else from the
database and the memory files whenever that setting is used. One rule for
every path is worth the import, which is cheap - `settings.py` depends on
nothing inside this project."""

FRONT_MATTER_FENCE = "---"

_UNSAFE_IN_FILENAME = re.compile(r"[^A-Za-z0-9_-]+")

#: Front-matter keys in the order they are written. A hand-chosen, stable
#: order rather than a dict's iteration order, because a stable order is what
#: makes two saved reports diffable against each other. A key absent from a
#: report's facts, or whose value is None, is omitted from that file entirely;
#: a key present in the facts but missing from this tuple is written after
#: these, sorted, so adding a fact at a call site can never silently drop it.
FACT_ORDER: tuple[str, ...] = (
    "digest",
    "saved_at",
    "as_of",
    "kind",
    "variant",
    "command",
    # Written only by `uv run portfolio-summary` (see `src/agentic_portfolio/flow/summary_cli.py`),
    # whose `kind` is "summary". Listed here rather than left to the sorted
    # append at the end of `_ordered_facts` so a summary's front matter reads in
    # the order a person would ask the questions. Purely additive: a key absent
    # from a report's facts is omitted from that file, so no existing call site
    # or saved file is affected, and digests cover the body alone.
    "month",
    "report_count",
    "sources_digest",
    "llm_model",
    "narrative_status",
    # Written only by a TRANSLATED summary, so absent from every other file.
    # `language` is deliberately absent rather than "en" on an English summary:
    # the summaries saved before translation existed carry no such fact, and a
    # reader should not have to learn that an absent fact and "en" mean the same
    # thing. `existing_summary` normalizes the two on the way in instead.
    "language",
    "translation_model",
    "translated_from",
    "translation_status",
    "currency",
    "objective",
    "objective_origin",
    "target_return",
    "clamped_from",
    "selection",
    "value",
    "candidates",
    "positions",
    "total_value",
    "priced_as_of",
    "benchmark",
    "benchmark_return",
    "risk_free_rate",
    "dividend_floor_yield",
    "dividend_floor_origin",
    "window_start",
    "window_end",
    "window_months",
    "annual_return",
    "annual_volatility",
    "sharpe",
    "annual_dividend",
    "dividend_yield",
)

#: Facts formatted as money, to two decimal places. Every other float is
#: formatted as a four-decimal fraction, matching the convention every ratio in
#: this project's reports already prints under (see `format_risk_free_rate` in
#: `src/agentic_portfolio/flow/cli.py`).
_MONEY_FACTS = frozenset({"value", "total_value", "annual_dividend"})


def command_line(argv: Sequence[str] | None = None) -> str:
    """This run's command line, as a string fit to record in a report.

    `argv[0]` is reduced to its basename, because the absolute path a console
    script is installed at (`/app/agentic_portfolio/.venv/bin/portfolio`) is a
    fact about this container rather than about the run - and a recorded
    command is worth recording only if a reader can retype it. Every other
    argument is passed through verbatim, so the recorded line reproduces the
    report.
    """
    parts = list(sys.argv if argv is None else argv)
    if not parts:
        return ""
    return " ".join([os.path.basename(parts[0]), *parts[1:]])


class ReportArchive(NamedTuple):
    """Where one run's reports are archived, and the facts common to all of
    them.

    One of these is built per RUN, not per report: `output_dir`, `kind`,
    `as_of` and `command` are properties of the invocation, and building it
    early - before any network work - means a malformed `--output-dir` is
    discovered before a live snapshot has been fetched.

    `enabled` is carried rather than expressed as a `None` archive so the
    `--no-save-reports` case still has somewhere to record where reports WOULD
    have gone, and so `record_report`'s disabled path is one branch instead of
    two.

    `as_of` is the run's as-of date (`--date`), which is what selects the month
    folder. Deliberately not the wall clock: a report is about a month of
    market data, so a `--date 2024-03-29` run belongs with the month it
    measured rather than the month somebody happened to execute it in.
    """

    output_dir: str
    enabled: bool
    kind: str
    as_of: date
    command: str


class SavedReport(NamedTuple):
    """One report's place in the archive.

    `created` is `False` when an identical report was already stored this
    month, in which case nothing was written and `path` names the file that
    was already there. That is a normal, expected outcome - it is what
    deduplication looks like - and not an error.
    """

    path: Path
    digest: str
    created: bool


class _Tee:
    """A stand-in for `sys.stdout` that forwards every write to the real
    stream and keeps a copy.

    `write` writes to the buffer FIRST and then to the wrapped stream,
    returning the wrapped stream's own return value so the object is
    substitutable for the stream it wraps. `__getattr__` delegates everything
    else - `flush`, `isatty`, `encoding`, `writelines` - because callers of
    `sys.stdout` reach for more than `write`, and a partial imitation would
    fail somewhere far from here.

    Writing through immediately, rather than replaying the buffer at the end,
    is what keeps output ordering identical and keeps a report that raises
    part-way visible on the terminal.
    """

    def __init__(self, stream) -> None:
        self._stream = stream
        self._buffer = io.StringIO()

    def write(self, text: str):
        self._buffer.write(text)
        return self._stream.write(text)

    def __getattr__(self, name: str):
        return getattr(self._stream, name)

    @property
    def text(self) -> str:
        return self._buffer.getvalue()


@contextmanager
def capture_report() -> Iterator[Callable[[], str]]:
    """Tee `sys.stdout` for the duration of the block, yielding a callable
    that returns the text captured so far.

    Wraps `sys.stdout` AS IT IS at entry, never `sys.__stdout__`: pytest's
    `capsys` replaces `sys.stdout` with an object of its own, and wrapping the
    current value is what makes this compose with it rather than bypass it.
    """
    tee = _Tee(sys.stdout)
    with redirect_stdout(tee):
        yield lambda: tee.text


def normalize_report(text: str) -> str:
    """`text` reduced to the form that is hashed and stored.

    Trailing whitespace is stripped from each line and surrounding blank lines
    are removed, so a stray trailing space or a leading newline - both of which
    a print site can acquire without any figure changing - cannot defeat
    deduplication.

    Leading whitespace on the first line is deliberately KEPT: report bodies
    happen to begin unindented today, but stripping it would silently alter a
    stored report's shape rather than only its incidental spacing, and the
    archive's job is to preserve what was printed.
    """
    lines = [line.rstrip() for line in text.splitlines()]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def report_digest(text: str) -> str:
    """The hex SHA-256 of `text` after `normalize_report`.

    SHA-256 rather than a shorter hash because the cost is irrelevant here and
    a digest that appears in a filename should not invite thinking about
    collisions. Only the first eight characters reach the filename; the full
    64 are recorded in the front matter, so a file can be checked against its
    own body.
    """
    return hashlib.sha256(normalize_report(text).encode("utf-8")).hexdigest()


def _slug(value: object) -> str:
    """`value` as a single filename-safe token.

    Objectives, selections and currency codes are already safe, so this
    normally changes nothing. It exists so that a future archive kind, or a
    currency read from a hand-edited file, cannot put a path separator into a
    filename and write outside the month folder.

    Dots are NOT in the safe set, so `../..` reduces to nothing rather than
    surviving as a traversal fragment. Nothing that reaches here legitimately
    contains one - the `.md` suffix is appended after the tokens are joined,
    and tickers, which do contain dots, never appear in a filename.
    """
    text = _UNSAFE_IN_FILENAME.sub("-", str(value)).strip("-")
    return text or "unknown"


def _format_value(key: str, value: object) -> str:
    """One front-matter value as text.

    Dates as `YYYY-MM-DD`, money to two decimals, every other float as a
    four-decimal fraction, a sequence of strings joined with `", "`, anything
    else via `str`. Ratios print as fractions rather than percentages for the
    same reason every ratio in this project's reports does: Yahoo Finance
    reports several of the same numbers as percentages, and one convention
    throughout is the only defence against reading 0.0341 as 3.41 basis points.
    """
    if isinstance(value, date) and not isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return f"{value:.2f}" if key in _MONEY_FACTS else f"{value:.4f}"
    if isinstance(value, (list, tuple)) or (
        isinstance(value, Sequence) and not isinstance(value, str)
    ):
        return ", ".join(str(item) for item in value)
    return str(value)


def _ordered_facts(facts: dict[str, object]) -> list[tuple[str, str]]:
    """`facts` as formatted `(key, value)` pairs in `FACT_ORDER`, with the
    `None`-valued ones dropped.

    A `None` fact is omitted rather than written as the word `None`, because an
    absent benchmark and a benchmark literally named "None" must not read the
    same in a file a later command parses. Keys not in `FACT_ORDER` are
    appended in sorted order rather than discarded, so adding a fact at a call
    site can never silently lose it.
    """
    known = [k for k in FACT_ORDER if k in facts]
    extra = sorted(k for k in facts if k not in FACT_ORDER)
    return [
        (key, _format_value(key, facts[key]))
        for key in (*known, *extra)
        if facts[key] is not None
    ]


def report_filename(archive: ReportArchive, facts: dict[str, object], digest: str) -> str:
    """The file one report is stored as, within its month folder.

    Tokens are the as-of date, the archive kind, whichever of the objective and
    selection are known, the currency, and the first eight characters of the
    digest - so the name says what the report is without being opened, and two
    reports of the same shape are told apart by their contents rather than by a
    counter. A counter would have been the alternative and is worse: it depends
    on what else is in the directory, so the same report saved into two
    different archives would get two different names.

    The `language` token is for a reader's benefit and is NOT a uniqueness
    mechanism - worth saying so, because it looks like one. Uniqueness is
    already guaranteed by the digest: the name ends in a hash of the body, and
    two languages cannot produce one body. The token is there so that
    `2026-09-11-summary-zh-TW-91c40e7b.md` can be told from its English sibling
    without opening either. Only a translated summary carries the fact, so no
    existing filename changes shape.

    A language named in its own script has no filename-safe characters at all,
    and `_slug` then yields `unknown`. That is accepted rather than fixed: the
    file is still correct and still unique, the `language` FACT carries the
    truth, and changing `_slug` to serve this case would loosen the safety rule
    that keeps a path separator out of every other filename.
    """
    tokens = [archive.as_of.isoformat(), archive.kind]
    for key in ("objective", "selection", "language"):
        value = facts.get(key)
        if value is not None:
            tokens.append(_slug(value))
    currency = facts.get("currency")
    if currency is not None:
        tokens.append(_slug(currency))
    tokens.append(digest[:8])
    return "-".join(_slug(token) for token in tokens) + ".md"


def month_dir(archive: ReportArchive) -> Path:
    """The folder this run's reports belong in: `<output_dir>/<YYYY-MM>` of the
    run's as-of date."""
    return Path(archive.output_dir) / archive.as_of.strftime("%Y-%m")


def _render(facts: dict[str, object], body: str) -> str:
    """One saved file's whole text: front matter, a blank line, the body."""
    lines = [FRONT_MATTER_FENCE]
    lines.extend(f"{key}: {value}" for key, value in _ordered_facts(facts))
    lines.append(FRONT_MATTER_FENCE)
    return "\n".join(lines) + "\n\n" + body + "\n"


def _write_atomically(path: Path, text: str) -> None:
    """Write `text` to `path` so that a concurrent reader sees either the old
    file or the whole new one.

    Copied from `src/agentic_portfolio/flow/rate_memory.py`'s `_write_rates`, the project's one
    existing atomic writer, including its temporary-file cleanup on any
    `BaseException` - `KeyboardInterrupt` included, since a run interrupted at
    the prompt is the likeliest way to reach that path. The temporary file is
    created in the destination directory rather than the system temp directory
    so `os.replace` stays within one filesystem and therefore stays atomic.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".report-", suffix=".md")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(text)
        os.replace(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def save_report(
    text: str,
    archive: ReportArchive,
    facts: dict[str, object],
) -> SavedReport | None:
    """Store one report body, or recognize that it is already stored.

    Returns `None` for a body that is empty after normalization: a report that
    printed nothing is not a report, and a file of front matter alone would be
    a row in the eventual comparison with nothing to compare.

    Returns `created=False`, having written nothing, when the digest's filename
    already exists in this month's folder. The already-stored file is left
    exactly as it was, so its `saved_at` keeps recording when that report was
    first seen rather than when it was last re-derived.
    """
    body = normalize_report(text)
    if not body:
        return None

    digest = report_digest(body)
    path = month_dir(archive) / report_filename(archive, facts, digest)
    if path.exists():
        return SavedReport(path=path, digest=digest, created=False)

    complete = dict(facts)
    complete["digest"] = digest
    complete["saved_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    complete["as_of"] = archive.as_of
    complete["kind"] = archive.kind
    complete["command"] = archive.command
    _write_atomically(path, _render(complete, body))
    return SavedReport(path=path, digest=digest, created=True)


def format_save_notice(saved: SavedReport | None) -> str | None:
    """The one line a command prints after archiving, or `None` when there is
    nothing to say.

    A command that writes a file must name the file, or the write is a side
    effect the user has to go looking for. The already-saved wording is
    deliberately not an apology: recognizing a repeat is the feature working.
    """
    if saved is None:
        return None
    if saved.created:
        return f"Saved report: {saved.path}"
    return f"Report already saved this month: {saved.path}"


@contextmanager
def record_report(archive: ReportArchive | None, **facts: object) -> Iterator[None]:
    """Archive whatever the block prints, then name the file it went to.

    A `None` or disabled `archive` yields immediately and does nothing else -
    no capture, no directory, no output - so a call site with archiving off
    behaves exactly as it did before this existed. That is what lets
    `print_pipeline_result` take an archive that defaults to `None` and leave
    every existing caller and test untouched.

    The save happens after the `yield` and deliberately NOT in a `finally`, so
    a block that raises archives nothing: a report that did not finish printing
    is not a report. The text printed before the exception has still reached the
    terminal, because `_Tee` writes through as it goes.

    The notice is printed after the capture has closed, which is why it never
    becomes part of the digest of the report it describes.
    """
    if archive is None or not archive.enabled:
        yield
        return

    with capture_report() as captured:
        yield
        text = captured()

    notice = format_save_notice(save_report(text, archive, dict(facts)))
    if notice is not None:
        print(notice)


def load_report(path: Path | str) -> tuple[dict[str, str], str]:
    """One saved report read back as `(facts, body)`.

    The inverse of `save_report`'s rendering, and the seam a later side-by-side
    summary command reads. Values come back as strings; interpreting them -
    `float(facts["sharpe"])`, `date.fromisoformat(facts["as_of"])` - is the
    caller's business, because this module has no opinion about what a
    comparison needs.

    Keys are split on the FIRST colon only, so a `command` value containing one
    survives the round trip. A file whose first line is not the fence is
    refused by name rather than parsed hopefully: the alternative is returning
    empty facts for a file somebody hand-edited, which a comparison would
    render as a row of blanks.
    """
    path = Path(path)
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0].strip() != FRONT_MATTER_FENCE:
        raise ValueError(
            f"{path} does not start with a {FRONT_MATTER_FENCE!r} front-matter fence, so it is "
            "not a report this archive wrote"
        )

    facts: dict[str, str] = {}
    body_start = len(lines)
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == FRONT_MATTER_FENCE:
            body_start = index + 1
            break
        key, separator, value = line.partition(":")
        if separator:
            facts[key.strip()] = value.strip()
    else:
        raise ValueError(f"{path} has an unterminated front-matter block")

    return facts, "\n".join(lines[body_start:]).strip()
