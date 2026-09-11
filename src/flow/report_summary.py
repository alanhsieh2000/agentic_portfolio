"""Read one month of the saved report archive back and reduce it to the
figures a decision actually needs.

`src/flow/report_archive.py` writes one file per printed report under
`output/<YYYY-MM>/`, each opening with a `---`-delimited block of plain
`key: value` facts. This module is the reader that block was built for.
It answers the questions no single report can: which of a month's runs
won on which axis, which tickers every optimization agreed on and which
candidates none of them wanted, how the portfolio someone actually holds
compares with the frontier they just mapped, and - the one that matters
most - where two saved reports are not comparable at all.

This module makes NO LLM call and reads NO network. That is deliberate
and it is the whole reason the work is split in two. A summary that
quietly miscalculated a Sharpe ratio would be worse than no summary, so
every figure in the finished briefing is computed here, from the saved
facts, by ordinary arithmetic. The language model that writes the prose
around those figures (`src/agents/report_summary.py`) is handed them
already computed and is forbidden from doing arithmetic of its own. That
is the same split `src/agents/llm_f.py` makes, where the model estimates
a per-headline probability and `compute_decayed_score` does all the math.

Two consequences worth stating. First, `--no-llm` is a real mode: this
module alone produces a complete, useful document. Second, every test of
this module is hermetic, because there is nothing here to mock.

A note on tolerance. Every front-matter key is optional - the writer
omits a fact whose value was `None`, so an absent benchmark and a
benchmark named "None" cannot look the same - and the files are plain
text that a person may have edited. So every accessor here returns
`None` rather than raising when a fact is missing or unparseable, and
every section that cannot be computed says why in one line instead of
vanishing.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from functools import cached_property
from pathlib import Path
from typing import Iterable, Mapping, NamedTuple, Sequence

from src.flow.report_archive import load_report, report_digest

#: The report kinds this module reads. An allow-list, not a deny-list: the
#: summary is written into the very folder it describes, carrying
#: `kind: summary`, so without this filter a second run would summarize the
#: first summary. An allow-list also means a future third report kind is
#: skipped with a note until this module is taught about it, rather than
#: being silently half-parsed as one of these two.
SOURCE_KINDS: tuple[str, ...] = ("portfolio", "whatif")

#: The kind the summary itself is saved as, and therefore the kind
#: `load_month` most importantly refuses.
SUMMARY_KIND = "summary"

#: A weight at or below this is reported as negligible rather than simply
#: "held". The real `output/2026-09/` archive contains an MV run that printed
#: `TLT: 0.0005` and allocated it one single share; counting that as TLT being
#: "held under two objectives" would overstate what the optimizer did.
NEGLIGIBLE_WEIGHT = 0.0050

#: Caps applied only by `digest_for_llm`, so a month with two hundred reports
#: cannot overflow a small model's context. `render_digest` is never capped -
#: a person reading the file wants all of it.
LLM_MAX_ROWS_PER_PARTITION = 12
LLM_MAX_TICKERS = 20
LLM_MAX_THREADS = 12
LLM_MAX_LEDGER = 12

_WEIGHT_LINE = re.compile(r"^\s{2,}([A-Za-z0-9._^-]+):\s*([0-9]*\.?[0-9]+)\s*$")
_BENCHMARK_LINE = re.compile(
    r"^Benchmark\s+(\S+):\s*return=(-?[0-9]*\.?[0-9]+)\s+"
    r"volatility=(-?[0-9]*\.?[0-9]+)\s+Sharpe=(-?[0-9]*\.?[0-9]+)"
    r"(?:\s*\((\d+)\s+of\s+(\d+)\s+month)?"
)
_SIGNED = r"([+-]?[0-9]*\.?[0-9]+)"
_DELTA_RETURN = re.compile(r"return\s+" + _SIGNED)
_DELTA_VOLATILITY = re.compile(r"volatility\s+" + _SIGNED)
_DELTA_SHARPE = re.compile(r"Sharpe\s+" + _SIGNED)
_DELTA_YIELD = re.compile(r"yield\s+" + _SIGNED)
_DELTA_INCOME = re.compile(r"annual income\s+([+-])?\$([0-9,]*\.?[0-9]+)")

_EPOCH = datetime(1, 1, 1, tzinfo=timezone.utc)


# --------------------------------------------------------------------------
# Coercion of the string facts
# --------------------------------------------------------------------------

def _text(facts: dict[str, str], key: str) -> str | None:
    value = facts.get(key)
    return value if value else None


def _float(facts: dict[str, str], key: str) -> float | None:
    value = facts.get(key)
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _int(facts: dict[str, str], key: str) -> int | None:
    value = facts.get(key)
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _date(facts: dict[str, str], key: str) -> date | None:
    value = facts.get(key)
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _moment(facts: dict[str, str], key: str) -> datetime | None:
    """One front-matter timestamp as an aware UTC `datetime`.

    The writer stores `saved_at` as `2026-09-11T12:42:31Z`. A naive value -
    which a hand-edited file could carry - is assumed UTC rather than rejected,
    so that sorting a month's reports never has to compare an aware datetime
    with a naive one.
    """
    value = facts.get(key)
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _items(facts: dict[str, str], key: str) -> tuple[str, ...]:
    value = facts.get(key)
    if not value:
        return ()
    return tuple(item.strip() for item in value.split(",") if item.strip())


# --------------------------------------------------------------------------
# Body parsers
# --------------------------------------------------------------------------

class BenchmarkLine(NamedTuple):
    """The one line a portfolio report's body prints about its benchmark.

    The front matter carries only the ticker and the annual return, so the
    volatility, the Sharpe ratio and the benchmark's own month coverage have to
    come from the body. `months_covered` below `months_total` means the
    benchmark was measured over less history than the portfolio it is printed
    beside, which is a comparability note rather than a footnote.
    """

    ticker: str
    annual_return: float
    annual_volatility: float
    sharpe: float
    months_covered: int | None
    months_total: int | None


def parse_weights(body: str) -> tuple[tuple[str, float], ...]:
    """The per-ticker weights a portfolio report printed, in file order.

    The report prints them descending by weight under a bare `Weights:` line,
    two spaces indented, and the block ends at the first line that is not one
    of them. Returns an empty tuple when there is no such block: a `whatif`
    report has none, and that is ordinary rather than an error.

    Only the tickers the optimizer actually allocated appear, so absence from
    this tuple is how "not held" is spelled.
    """
    lines = body.splitlines()
    try:
        start = next(i for i, line in enumerate(lines) if line.strip() == "Weights:")
    except StopIteration:
        return ()

    weights: list[tuple[str, float]] = []
    for line in lines[start + 1:]:
        match = _WEIGHT_LINE.match(line)
        if not match:
            break
        weights.append((match.group(1), float(match.group(2))))
    return tuple(weights)


def parse_benchmark(body: str) -> BenchmarkLine | None:
    """The body's `Benchmark <TICKER>: return=... volatility=... Sharpe=...`
    line, or `None` when there is none - every `whatif` report, and any
    portfolio run whose benchmark did not resolve.
    """
    for line in body.splitlines():
        match = _BENCHMARK_LINE.match(line.strip())
        if not match:
            continue
        covered = int(match.group(5)) if match.group(5) else None
        total = int(match.group(6)) if match.group(6) else None
        return BenchmarkLine(
            ticker=match.group(1),
            annual_return=float(match.group(2)),
            annual_volatility=float(match.group(3)),
            sharpe=float(match.group(4)),
            months_covered=covered,
            months_total=total,
        )
    return None


def parse_whatif_deltas(body: str) -> dict[str, float]:
    """The two delta lines a `whatif` variant prints against the saved book.

    Reads `Change from your saved portfolio: return +0.0359  volatility
    +0.0274  Sharpe +0.1932` and `Dividend change: yield +0.0113  annual income
    -$9,858.00 USD` into the keys `return`, `volatility`, `sharpe`,
    `dividend_yield` and `annual_income`. Only the keys actually found are
    returned, so a `baseline` report - which prints neither line - yields `{}`.
    """
    deltas: dict[str, float] = {}
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("Change from your saved portfolio:"):
            for key, pattern in (
                ("return", _DELTA_RETURN),
                ("volatility", _DELTA_VOLATILITY),
                ("sharpe", _DELTA_SHARPE),
            ):
                match = pattern.search(stripped)
                if match:
                    deltas[key] = float(match.group(1))
        elif stripped.startswith("Dividend change:"):
            match = _DELTA_YIELD.search(stripped)
            if match:
                deltas["dividend_yield"] = float(match.group(1))
            match = _DELTA_INCOME.search(stripped)
            if match:
                amount = float(match.group(2).replace(",", ""))
                deltas["annual_income"] = -amount if match.group(1) == "-" else amount
    return deltas


# --------------------------------------------------------------------------
# One saved report
# --------------------------------------------------------------------------

@dataclass(frozen=True, eq=False)
class ReportRecord:
    """One saved report, its front-matter facts as strings and its body text.

    Every accessor coerces on demand and returns `None` for a fact that is
    absent or does not parse. `eq=False` because `facts` is a dict and this is
    an identity-carrying record, not a value.
    """

    path: Path
    facts: dict[str, str]
    body: str

    # -- common to both kinds
    @property
    def digest(self) -> str | None:
        return _text(self.facts, "digest")

    @property
    def kind(self) -> str | None:
        return _text(self.facts, "kind")

    @property
    def variant(self) -> str | None:
        return _text(self.facts, "variant")

    @property
    def command(self) -> str | None:
        return _text(self.facts, "command")

    @property
    def currency(self) -> str | None:
        return _text(self.facts, "currency")

    @property
    def as_of(self) -> date | None:
        return _date(self.facts, "as_of")

    @property
    def saved_at(self) -> datetime | None:
        return _moment(self.facts, "saved_at")

    @property
    def risk_free_rate(self) -> float | None:
        return _float(self.facts, "risk_free_rate")

    @property
    def window_start(self) -> str | None:
        return _text(self.facts, "window_start")

    @property
    def window_end(self) -> str | None:
        return _text(self.facts, "window_end")

    @property
    def window_months(self) -> int | None:
        return _int(self.facts, "window_months")

    @property
    def annual_return(self) -> float | None:
        return _float(self.facts, "annual_return")

    @property
    def annual_volatility(self) -> float | None:
        return _float(self.facts, "annual_volatility")

    @property
    def sharpe(self) -> float | None:
        return _float(self.facts, "sharpe")

    @property
    def annual_dividend(self) -> float | None:
        return _float(self.facts, "annual_dividend")

    @property
    def dividend_yield(self) -> float | None:
        return _float(self.facts, "dividend_yield")

    # -- portfolio only
    @property
    def objective(self) -> str | None:
        return _text(self.facts, "objective")

    @property
    def objective_origin(self) -> str | None:
        return _text(self.facts, "objective_origin")

    @property
    def target_return(self) -> float | None:
        return _float(self.facts, "target_return")

    @property
    def selection(self) -> str | None:
        return _text(self.facts, "selection")

    @property
    def value(self) -> float | None:
        return _float(self.facts, "value")

    @property
    def candidates(self) -> tuple[str, ...]:
        return _items(self.facts, "candidates")

    @property
    def benchmark(self) -> str | None:
        return _text(self.facts, "benchmark")

    @property
    def benchmark_return(self) -> float | None:
        return _float(self.facts, "benchmark_return")

    @property
    def dividend_floor_yield(self) -> float | None:
        return _float(self.facts, "dividend_floor_yield")

    # -- whatif only
    @property
    def total_value(self) -> float | None:
        return _float(self.facts, "total_value")

    @property
    def priced_as_of(self) -> date | None:
        return _date(self.facts, "priced_as_of")

    @property
    def positions(self) -> tuple[tuple[str, int], ...]:
        """`positions: PFF:6000, PFFA:6000, VZ:2000` as pairs.

        An entry whose share count does not parse is kept with a count of `0`
        rather than dropped, because which tickers a book holds is the useful
        half and losing a ticker silently would corrupt an added/removed diff.
        """
        parsed: list[tuple[str, int]] = []
        for item in _items(self.facts, "positions"):
            ticker, _, shares = item.partition(":")
            try:
                count = int(shares)
            except ValueError:
                count = 0
            parsed.append((ticker.strip(), count))
        return tuple(parsed)

    # -- derived from the body
    @cached_property
    def weights(self) -> tuple[tuple[str, float], ...]:
        return parse_weights(self.body)

    @cached_property
    def benchmark_line(self) -> BenchmarkLine | None:
        return parse_benchmark(self.body)

    @cached_property
    def deltas(self) -> dict[str, float]:
        return parse_whatif_deltas(self.body)

    # -- convenience
    @property
    def tickers(self) -> tuple[str, ...]:
        """The tickers this report is about: its weights for a portfolio
        report, its positions for a `whatif` one."""
        if self.weights:
            return tuple(ticker for ticker, _ in self.weights)
        return tuple(ticker for ticker, _ in self.positions)

    @property
    def window_key(self) -> tuple[str | None, str | None, int | None]:
        return (self.window_start, self.window_end, self.window_months)

    @property
    def partition_key(self) -> tuple[str | None, str | None, str | None, int | None]:
        return (self.currency, self.window_start, self.window_end, self.window_months)

    @property
    def sort_key(self) -> tuple[datetime, str]:
        return (self.saved_at or _EPOCH, self.path.name)

    @property
    def stable_digest(self) -> str:
        return self.digest or report_digest(self.body)


def label(record: ReportRecord) -> str:
    """The short name a leaderboard row, thread step or ledger entry is called
    by.

    A portfolio report is named by its objective, with the target return
    appended when it has one - `portfolio MV @0.1225` - because two MV runs on
    the same pool differ by nothing else. A `whatif` report is named by the
    tickers it holds, with `(held)` marking the baseline, since that is the one
    row in the table describing a portfolio that actually exists.
    """
    kind = record.kind or "report"
    if kind == "portfolio":
        name = f"portfolio {record.objective or '?'}"
        if record.target_return is not None:
            name += f" @{record.target_return:.4f}"
        return name
    if kind == "whatif":
        tickers = [ticker for ticker, _ in record.positions]
        if len(tickers) > 4:
            shown = "+".join(tickers[:4]) + f"+{len(tickers) - 4} more"
        else:
            shown = "+".join(tickers) or "empty"
        name = f"whatif {shown}"
        if record.variant == "baseline":
            name += " (held)"
        return name
    return f"{kind} {record.variant or ''}".strip()


def window_label(start: str | None, end: str | None, months: int | None) -> str:
    if not start or not end:
        return "window unknown"
    if months is None:
        return f"{start} to {end}"
    return f"{start} to {end} ({months} months)"


# --------------------------------------------------------------------------
# Reading a month
# --------------------------------------------------------------------------

def load_month(
    output_dir: str | Path, month: str
) -> tuple[list[ReportRecord], list[str]]:
    """Every source report in one month folder, plus a note per file skipped.

    `month` is a `YYYY-MM` string; the folder is `<output_dir>/<month>`. A
    missing or empty folder returns `([], [])` rather than raising - saying so
    is the caller's job, because "no reports yet" is not an error at this
    level.

    A file that `load_report` refuses, or whose `kind` is not a source kind,
    produces a note and is skipped. Both matter: the folder holds the summaries
    this command itself writes, and a person may drop a hand-written note in
    beside them. Neither may break the command, and neither may be swallowed
    without the reader being told.
    """
    folder = Path(output_dir) / month
    if not folder.is_dir():
        return [], []

    records: list[ReportRecord] = []
    notes: list[str] = []
    # `Path.glob("*.md")` matches dotfiles, and `save_report` writes through a
    # `.report-*.md` temporary that a killed process can leave behind. Such a
    # file is a half-written report, not a report, so it is skipped silently
    # rather than reported as something the reader should look at.
    for path in sorted(p for p in folder.glob("*.md") if not p.name.startswith(".")):
        try:
            facts, body = load_report(path)
        except (ValueError, OSError, UnicodeDecodeError):
            notes.append(f"{path.name}: skipped, not a report this archive wrote")
            continue
        kind = facts.get("kind")
        if kind not in SOURCE_KINDS:
            notes.append(f"{path.name}: skipped, kind is {kind or 'absent'}")
            continue
        records.append(ReportRecord(path=path, facts=facts, body=body))

    records.sort(key=lambda record: record.sort_key)
    return records, notes


def sources_digest(records: Sequence[ReportRecord]) -> str:
    """A digest identifying exactly this SET of source reports.

    The question a repeated `portfolio-summary` run has to answer is "have I
    already summarized these reports", not "is this text the same as that
    text" - the prose differs between runs, so a digest of the summary itself
    would never repeat and every run would add a file. Sorting before hashing
    makes the value independent of the order the files happened to be read in;
    adding or removing one report changes it.
    """
    digests = sorted(record.stable_digest for record in records)
    return hashlib.sha256("\n".join(digests).encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# The analysis: plain data holders, no rendering
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class LeaderRow:
    """One portfolio's line in a leaderboard.

    `copies` is above 1 when a partition holds several reports with the same
    name and the same four figures. They are distinct files with distinct
    digests - the text around the figures differed - but printing them as
    separate rows would suggest the month explored more portfolios than it did.
    """

    label: str
    kind: str
    variant: str | None
    annual_return: float | None
    annual_volatility: float | None
    sharpe: float | None
    dividend_yield: float | None
    annual_dividend: float | None
    digest: str | None
    copies: int = 1


@dataclass(frozen=True)
class Partition:
    """Every report measured on one currency over one returns window.

    Reports are ranked only WITHIN a partition and never across partitions.
    The real `output/2026-09/` archive is the argument: the identical
    PFF/PFFA/VZ book scores an annual return of 0.0229 and a Sharpe of -0.1253
    over 60 months, and 0.0713 and 0.2808 over 48. Both are correct answers to
    different questions, and one ranked table containing both would invite a
    comparison the numbers do not support. Currency joins the key because a
    month folder is shared across currencies by design.
    """

    currency: str | None
    window_start: str | None
    window_end: str | None
    window_months: int | None
    rows: tuple[LeaderRow, ...]
    benchmark: BenchmarkLine | None

    @property
    def window(self) -> str:
        return window_label(self.window_start, self.window_end, self.window_months)

    @property
    def title(self) -> str:
        return f"Window {self.window}, {self.currency or 'currency unknown'}"


@dataclass(frozen=True)
class ThreadStep:
    """One report in an exploration thread, with what moved since the previous
    one."""

    label: str
    variant: str | None
    saved_at: datetime | None
    annual_return: float | None
    annual_volatility: float | None
    sharpe: float | None
    dividend_yield: float | None
    return_change: float | None
    volatility_change: float | None
    sharpe_change: float | None
    dividend_yield_change: float | None
    entered: tuple[str, ...]
    left: tuple[str, ...]


@dataclass(frozen=True)
class Thread:
    """One exploration session: the reports of one kind, on one currency, over
    one returns window, from one command against one candidate pool, in the
    order they were saved.

    The window is part of the grouping key on purpose. Without it, the five
    real `whatif` reports would read as a single five-step session whose
    step-to-step differences silently straddled a 60-month and a 48-month
    measurement.
    """

    kind: str
    title: str
    steps: tuple[ThreadStep, ...]


@dataclass(frozen=True)
class Consensus:
    """What a month's optimizer runs agreed and disagreed about, over one
    shared candidate pool.

    `reason` is set - and every other field empty - when there is no pool that
    two portfolio reports share, because one run cannot agree with anything.
    """

    reason: str | None
    runs: tuple[str, ...] = ()
    candidates: tuple[str, ...] = ()
    always_held: tuple[str, ...] = ()
    never_held: tuple[str, ...] = ()
    sometimes_held: tuple[tuple[str, tuple[tuple[str, float], ...]], ...] = ()
    #: Tickers exactly ONE run held, named with that run. "Run" and not
    #: "objective": two MV runs at different target returns are two runs, so a
    #: ticker only one of them wanted is specific to that run rather than to MV.
    run_specific: tuple[tuple[str, str, float], ...] = ()
    negligible: tuple[tuple[str, str, float], ...] = ()


@dataclass(frozen=True)
class BookComparison:
    """The portfolio someone actually holds, set against the best one the
    month's runs found and against the benchmark.

    `same_window` is reported rather than assumed. A gap between two figures
    measured over different histories is not a gap in the portfolios.
    """

    reason: str | None
    currency: str | None = None
    book: LeaderRow | None = None
    book_window: str | None = None
    best: LeaderRow | None = None
    best_window: str | None = None
    benchmark: BenchmarkLine | None = None
    same_window: bool = True
    return_gap: float | None = None
    volatility_gap: float | None = None
    sharpe_gap: float | None = None
    dividend_yield_gap: float | None = None
    annual_dividend_gap: float | None = None


@dataclass(frozen=True, eq=False)
class ScenarioEntry:
    """One hypothetical change tried against a held book, with the deltas the
    report itself printed and the positions that moved."""

    label: str
    partition: str
    baseline_label: str | None
    annual_return: float | None
    annual_volatility: float | None
    sharpe: float | None
    dividend_yield: float | None
    annual_dividend: float | None
    deltas: dict[str, float]
    added: tuple[str, ...]
    removed: tuple[str, ...]
    note: str | None = None


@dataclass(frozen=True)
class ScenarioLedger:
    reason: str | None
    entries: tuple[ScenarioEntry, ...] = ()


@dataclass(frozen=True)
class WindowSensitivity:
    """One subject - one set of positions, or one objective on one candidate
    pool - measured over more than one returns window, with every measurement.

    This is the finding the whole summary exists to make visible, and it is
    built from the saved figures rather than from any assumption about how much
    a window ought to matter.
    """

    subject: str
    measurements: tuple[tuple[str, float | None, float | None, float | None], ...]


@dataclass(frozen=True)
class Comparability:
    windows: tuple[tuple[str, tuple[str, ...]], ...] = ()
    rate_disagreements: tuple[str, ...] = ()
    lone_partitions: tuple[str, ...] = ()
    sensitivity: tuple[WindowSensitivity, ...] = ()
    benchmark_coverage: tuple[str, ...] = ()


@dataclass(frozen=True)
class SourceReport:
    """One saved report, named the way a reader has to name it to open it.

    `digest8` is the first eight characters of the report's digest, which is
    also the token `report_filename` in `src/flow/report_archive.py` builds the
    filename from - so it is not an opaque identifier but the part of the file's
    own name that tells it from its neighbours. It is unique within a month
    folder by construction: two reports sharing it would collide on one path,
    and `save_report` would recognize the second as already saved.

    `label` is qualified with the returns window when another report in the same
    month carries the same unqualified label, because without that four of the
    nine reports in a real month folder are indistinguishable - the two
    measurements of a held book share a label, and so do the two measurements of
    any variant of it.
    """

    digest8: str
    digest: str | None
    saved_at: datetime | None
    kind: str
    variant: str | None
    label: str
    filename: str


@dataclass(frozen=True)
class Scope:
    month: str
    report_count: int
    portfolio_count: int
    whatif_count: int
    currencies: tuple[str, ...]
    as_of_first: date | None
    as_of_last: date | None
    saved_first: datetime | None
    saved_last: datetime | None
    risk_free_rates: tuple[tuple[str, tuple[float, ...]], ...]
    benchmarks: tuple[tuple[str, BenchmarkLine], ...]
    commands: tuple[str, ...]
    skipped: tuple[str, ...]
    #: The directory the reports were read from, so the source list can state
    #: it once rather than repeating it on every row. Taken from the records
    #: themselves rather than from a parameter, because they know where they
    #: came from.
    folder: str | None = None


@dataclass(frozen=True)
class MonthDigest:
    scope: Scope
    partitions: tuple[Partition, ...]
    threads: tuple[Thread, ...]
    consensus: Consensus
    book: BookComparison
    ledger: ScenarioLedger
    comparability: Comparability
    sources_digest: str
    sources: tuple[SourceReport, ...] = ()


# --------------------------------------------------------------------------
# Building the analysis
# --------------------------------------------------------------------------

def _grouped(records: Iterable[ReportRecord], key) -> dict:
    groups: dict = {}
    for record in records:
        groups.setdefault(key(record), []).append(record)
    return groups


def _rank_key(row: LeaderRow) -> tuple[int, float, str]:
    """Sharpe descending, with an unparseable Sharpe last rather than treated
    as zero - a missing figure is not a mediocre one."""
    if row.sharpe is None:
        return (1, 0.0, row.label)
    return (0, -row.sharpe, row.label)


def _row(record: ReportRecord) -> LeaderRow:
    return LeaderRow(
        label=label(record),
        kind=record.kind or "report",
        variant=record.variant,
        annual_return=record.annual_return,
        annual_volatility=record.annual_volatility,
        sharpe=record.sharpe,
        dividend_yield=record.dividend_yield,
        annual_dividend=record.annual_dividend,
        digest=record.digest,
    )


def _collapse(rows: Sequence[LeaderRow]) -> tuple[LeaderRow, ...]:
    """Rows sharing a label and all four figures merged into one, counted.

    Note for whoever reads `LeaderRow.digest` next: a collapsed row keeps only
    the FIRST of its reports' digests, so that field does not identify the row's
    sources and nothing renders it. The `Source reports` section is built from
    the records instead (see `build_sources`), which is the per-file data.
    """
    collapsed: list[LeaderRow] = []
    for row in rows:
        for index, seen in enumerate(collapsed):
            same = (
                seen.label == row.label
                and seen.annual_return == row.annual_return
                and seen.annual_volatility == row.annual_volatility
                and seen.sharpe == row.sharpe
                and seen.dividend_yield == row.dividend_yield
            )
            if same:
                collapsed[index] = replace(seen, copies=seen.copies + 1)
                break
        else:
            collapsed.append(row)
    return tuple(collapsed)


def _build_partitions(records: Sequence[ReportRecord]) -> tuple[Partition, ...]:
    partitions: list[Partition] = []
    for key, group in _grouped(records, lambda r: r.partition_key).items():
        currency, start, end, months = key
        rows = _collapse(sorted((_row(r) for r in group), key=_rank_key))
        benchmark = next(
            (r.benchmark_line for r in group if r.benchmark_line is not None), None
        )
        partitions.append(
            Partition(
                currency=currency,
                window_start=start,
                window_end=end,
                window_months=months,
                rows=rows,
                benchmark=benchmark,
            )
        )
    partitions.sort(
        key=lambda p: (-len(p.rows), p.currency or "", -(p.window_months or 0))
    )
    return tuple(partitions)


def _build_threads(records: Sequence[ReportRecord]) -> tuple[Thread, ...]:
    def key(record: ReportRecord):
        return (
            record.kind,
            record.partition_key,
            record.selection or "",
            record.candidates,
            record.command or "",
        )

    threads: list[Thread] = []
    for group in _grouped(records, key).values():
        ordered = sorted(group, key=lambda r: r.sort_key)
        steps: list[ThreadStep] = []
        previous: ReportRecord | None = None
        for record in ordered:
            before = set(previous.tickers) if previous else set()
            now = set(record.tickers)
            steps.append(
                ThreadStep(
                    label=label(record),
                    variant=record.variant,
                    saved_at=record.saved_at,
                    annual_return=record.annual_return,
                    annual_volatility=record.annual_volatility,
                    sharpe=record.sharpe,
                    dividend_yield=record.dividend_yield,
                    return_change=_difference(
                        record.annual_return, previous.annual_return if previous else None
                    ),
                    volatility_change=_difference(
                        record.annual_volatility,
                        previous.annual_volatility if previous else None,
                    ),
                    sharpe_change=_difference(
                        record.sharpe, previous.sharpe if previous else None
                    ),
                    dividend_yield_change=_difference(
                        record.dividend_yield,
                        previous.dividend_yield if previous else None,
                    ),
                    entered=tuple(sorted(now - before)) if previous else (),
                    left=tuple(sorted(before - now)) if previous else (),
                )
            )
            previous = record
        first = ordered[0]
        pool = f", {len(first.candidates)}-ticker pool" if first.candidates else ""
        title = (
            f"{first.kind}, {first.currency or 'currency unknown'}, "
            f"{window_label(*first.window_key)}{pool}"
        )
        threads.append(Thread(kind=first.kind or "report", title=title, steps=tuple(steps)))
    threads.sort(key=lambda t: (t.steps[0].saved_at or _EPOCH, t.title))
    return tuple(threads)


def _difference(now: float | None, before: float | None) -> float | None:
    if now is None or before is None:
        return None
    return now - before


def _build_consensus(records: Sequence[ReportRecord]) -> Consensus:
    pools = _grouped(
        [r for r in records if r.kind == "portfolio" and r.candidates],
        lambda r: (r.currency, r.candidates, r.window_key),
    )
    if not pools:
        return Consensus(
            reason="no portfolio report in this month records a candidate pool, "
                   "so there is nothing for two runs to agree about"
        )
    key, group = max(pools.items(), key=lambda item: (len(item[1]), len(item[0][1])))
    if len(group) < 2:
        return Consensus(
            reason=f"only one portfolio report shares the {len(key[1])}-ticker pool "
                   f"{', '.join(key[1])}, and one run cannot agree with anything"
        )

    candidates = key[1]
    held: dict[str, list[tuple[str, float]]] = {}
    for record in sorted(group, key=lambda r: r.sort_key):
        name = label(record)
        for ticker, weight in record.weights:
            held.setdefault(ticker, []).append((name, weight))

    runs = tuple(label(r) for r in sorted(group, key=lambda r: r.sort_key))
    always = tuple(sorted(t for t, seen in held.items() if len(seen) == len(group)))
    never = tuple(sorted(set(candidates) - set(held)))
    sometimes = tuple(
        (ticker, tuple(held[ticker]))
        for ticker in sorted(held)
        if 0 < len(held[ticker]) < len(group)
    )
    specific = tuple(
        (ticker, held[ticker][0][0], held[ticker][0][1])
        for ticker in sorted(held)
        if len(held[ticker]) == 1
    )
    negligible = tuple(
        (ticker, name, weight)
        for ticker in sorted(held)
        for name, weight in held[ticker]
        if weight <= NEGLIGIBLE_WEIGHT
    )
    return Consensus(
        reason=None,
        runs=runs,
        candidates=candidates,
        always_held=always,
        never_held=never,
        sometimes_held=sometimes,
        run_specific=specific,
        negligible=negligible,
    )


def _build_book(
    records: Sequence[ReportRecord], partitions: Sequence[Partition]
) -> BookComparison:
    baselines = [r for r in records if r.kind == "whatif" and r.variant == "baseline"]
    portfolios = [r for r in records if r.kind == "portfolio"]
    if not baselines:
        return BookComparison(
            reason="this month holds no 'whatif' baseline, so there is no portfolio "
                   "you actually hold to compare the runs against"
        )
    if not portfolios:
        return BookComparison(
            reason="this month holds no portfolio report, so there is no frontier to "
                   "compare the book you hold against"
        )

    # The baseline measured over the most history, because that is the reading
    # with the least to be explained away; the shorter one still appears under
    # Comparability.
    book_record = max(
        baselines, key=lambda r: (r.window_months or 0, r.saved_at or _EPOCH)
    )
    same_currency = [r for r in portfolios if r.currency == book_record.currency]
    if not same_currency:
        return BookComparison(
            reason=f"no portfolio report is denominated in {book_record.currency}, "
                   "the currency of the book you hold, so the two cannot be compared"
        )

    same_window = [r for r in same_currency if r.window_key == book_record.window_key]
    pool = same_window or same_currency
    best_record = max(
        pool, key=lambda r: (r.sharpe if r.sharpe is not None else float("-inf"))
    )
    book_row, best_row = _row(book_record), _row(best_record)
    benchmark = next(
        (
            p.benchmark
            for p in partitions
            if p.benchmark is not None
            and p.currency == book_record.currency
            and (p.window_start, p.window_end, p.window_months) == best_record.window_key
        ),
        None,
    )
    return BookComparison(
        reason=None,
        currency=book_record.currency,
        book=book_row,
        book_window=window_label(*book_record.window_key),
        best=best_row,
        best_window=window_label(*best_record.window_key),
        benchmark=benchmark,
        same_window=bool(same_window),
        return_gap=_difference(best_row.annual_return, book_row.annual_return),
        volatility_gap=_difference(best_row.annual_volatility, book_row.annual_volatility),
        sharpe_gap=_difference(best_row.sharpe, book_row.sharpe),
        dividend_yield_gap=_difference(best_row.dividend_yield, book_row.dividend_yield),
        annual_dividend_gap=_difference(best_row.annual_dividend, book_row.annual_dividend),
    )


def _build_ledger(records: Sequence[ReportRecord]) -> ScenarioLedger:
    variants = [r for r in records if r.kind == "whatif" and r.variant != "baseline"]
    if not variants:
        return ScenarioLedger(
            reason="this month holds no 'whatif' variant, so no hypothetical change "
                   "was tried against the book you hold"
        )

    baselines = {
        r.partition_key: r
        for r in sorted(
            (r for r in records if r.kind == "whatif" and r.variant == "baseline"),
            key=lambda r: r.sort_key,
        )
    }
    entries: list[ScenarioEntry] = []
    for record in sorted(variants, key=lambda r: r.sort_key):
        # Pair each variant with the baseline measured the same way. The real
        # archive proves why: a 48-month variant's printed "+0.0359" is against
        # the 48-month baseline's 0.0713, while the 60-month variant's "+0.0269"
        # is against the 60-month baseline's 0.0229. Pairing across windows
        # would attach both deltas to the wrong starting point.
        baseline = baselines.get(record.partition_key)
        before = {t for t, _ in baseline.positions} if baseline else set()
        now = {t for t, _ in record.positions}
        entries.append(
            ScenarioEntry(
                label=label(record),
                partition=f"{record.currency or 'currency unknown'}, "
                          f"{window_label(*record.window_key)}",
                baseline_label=label(baseline) if baseline else None,
                annual_return=record.annual_return,
                annual_volatility=record.annual_volatility,
                sharpe=record.sharpe,
                dividend_yield=record.dividend_yield,
                annual_dividend=record.annual_dividend,
                deltas=dict(record.deltas),
                added=tuple(sorted(now - before)) if baseline else (),
                removed=tuple(sorted(before - now)) if baseline else (),
                note=None
                if baseline
                else "no baseline was saved on this currency and window, so the "
                     "deltas below are the ones the report itself printed and the "
                     "positions it changed are unknown",
            )
        )
    return ScenarioLedger(reason=None, entries=tuple(entries))


def _subject(record: ReportRecord) -> str | None:
    """What a window-sensitivity comparison is ABOUT: the thing held constant
    while the window changed."""
    if record.kind == "whatif" and record.positions:
        return "positions " + ", ".join(f"{t}:{n}" for t, n in record.positions)
    if record.kind == "portfolio" and record.candidates:
        return f"{label(record)} on {len(record.candidates)}-ticker pool {record.selection or ''}".strip()
    return None


def _build_comparability(
    records: Sequence[ReportRecord], partitions: Sequence[Partition]
) -> Comparability:
    windows = tuple(
        (
            f"{p.currency or 'currency unknown'}, {p.window}",
            tuple(row.label for row in p.rows),
        )
        for p in partitions
    )

    rates: dict[str, set[float]] = {}
    for record in records:
        if record.currency and record.risk_free_rate is not None:
            rates.setdefault(record.currency, set()).add(record.risk_free_rate)
    disagreements = tuple(
        f"{currency}: {', '.join(f'{r:.4f}' for r in sorted(seen))} - the Sharpe "
        "ratios in this month were not all measured against the same rate"
        for currency, seen in sorted(rates.items())
        if len(seen) > 1
    )

    lone = tuple(
        f"{p.currency or 'currency unknown'}, {p.window} holds one portfolio, so "
        "its ranking says nothing"
        for p in partitions
        if len(p.rows) == 1
    )

    by_subject = _grouped(
        [r for r in records if _subject(r)], lambda r: (r.currency, _subject(r))
    )
    sensitivity: list[WindowSensitivity] = []
    for (currency, subject), group in sorted(by_subject.items(), key=lambda i: str(i[0])):
        seen_windows = {r.window_key for r in group}
        if len(seen_windows) < 2:
            continue
        measurements = tuple(
            (
                window_label(*record.window_key),
                record.annual_return,
                record.annual_volatility,
                record.sharpe,
            )
            for record in sorted(
                group, key=lambda r: -(r.window_months or 0)
            )
        )
        sensitivity.append(
            WindowSensitivity(
                subject=f"{subject} ({currency or 'currency unknown'})",
                measurements=measurements,
            )
        )

    coverage = tuple(
        f"{p.currency or 'currency unknown'}, {p.window}: benchmark "
        f"{p.benchmark.ticker} covers {p.benchmark.months_covered} of "
        f"{p.benchmark.months_total} months, so it is measured over less history "
        "than the portfolios it is printed beside"
        for p in partitions
        if p.benchmark is not None
        and p.benchmark.months_covered is not None
        and p.benchmark.months_total is not None
        and p.benchmark.months_covered < p.benchmark.months_total
    )

    return Comparability(
        windows=windows,
        rate_disagreements=disagreements,
        lone_partitions=lone,
        sensitivity=tuple(sensitivity),
        benchmark_coverage=coverage,
    )


def build_sources(records: Sequence[ReportRecord]) -> tuple[SourceReport, ...]:
    """Every report the briefing was built from, in the order it was saved.

    Saved order rather than alphabetical, because that is the order the work
    happened in and therefore the order a reader remembers it in. A report whose
    `saved_at` cannot be read sorts last rather than first, so an unreadable
    timestamp cannot silently claim to be the start of the session.

    `digest8` comes from `stable_digest`, which falls back to hashing the body
    when the front-matter `digest` is absent. Worth knowing about that fallback:
    the identifier it produces is still stable and still unique, but it will NOT
    match the name of a file whose front matter was edited after it was written,
    because the name was built from the digest the writer recorded at the time.

    A label is qualified with its returns window only when another report shares
    the unqualified form. Qualifying unconditionally would be worse: the seven
    unambiguous labels in a real month folder match the leaderboard verbatim,
    which is how a reader gets from a row to this list at all.
    """
    seen_labels: dict[str, int] = {}
    for record in records:
        name = label(record)
        seen_labels[name] = seen_labels.get(name, 0) + 1

    # Not `record.sort_key`, which substitutes `_EPOCH` for a missing timestamp
    # and so sorts an unreadable one FIRST. Here it must sort last, so a report
    # whose `saved_at` cannot be read cannot pose as the start of the session.
    def order(record: ReportRecord) -> tuple[bool, datetime, str]:
        return (record.saved_at is None, record.saved_at or _EPOCH, record.path.name)

    sources: list[SourceReport] = []
    for record in sorted(records, key=order):
        name = label(record)
        if seen_labels.get(name, 0) > 1 and record.window_months is not None:
            name = f"{name} {record.window_months}mo"
        sources.append(
            SourceReport(
                digest8=record.stable_digest[:8],
                digest=record.digest,
                saved_at=record.saved_at,
                kind=record.kind or "report",
                variant=record.variant,
                label=name,
                filename=record.path.name,
            )
        )
    return tuple(sources)


def build_month_digest(
    records: Sequence[ReportRecord], month: str, skipped: Sequence[str] = ()
) -> MonthDigest:
    """Every cross-report fact one month's archive contains, computed.

    Safe on an empty `records`: every section reports its own reason for having
    nothing to say, which is what the caller prints.
    """
    partitions = _build_partitions(records)
    rates: dict[str, set[float]] = {}
    for record in records:
        if record.currency and record.risk_free_rate is not None:
            rates.setdefault(record.currency, set()).add(record.risk_free_rate)

    benchmarks: dict[str, BenchmarkLine] = {}
    for record in records:
        line = record.benchmark_line
        if line and record.currency and record.currency not in benchmarks:
            benchmarks[record.currency] = line

    as_ofs = sorted(r.as_of for r in records if r.as_of)
    saves = sorted(r.saved_at for r in records if r.saved_at)
    scope = Scope(
        month=month,
        report_count=len(records),
        portfolio_count=sum(1 for r in records if r.kind == "portfolio"),
        whatif_count=sum(1 for r in records if r.kind == "whatif"),
        currencies=tuple(sorted({r.currency for r in records if r.currency})),
        as_of_first=as_ofs[0] if as_ofs else None,
        as_of_last=as_ofs[-1] if as_ofs else None,
        saved_first=saves[0] if saves else None,
        saved_last=saves[-1] if saves else None,
        risk_free_rates=tuple(
            (currency, tuple(sorted(seen))) for currency, seen in sorted(rates.items())
        ),
        benchmarks=tuple(sorted(benchmarks.items())),
        commands=tuple(sorted({r.command for r in records if r.command})),
        skipped=tuple(skipped),
        folder=str(records[0].path.parent) if records else None,
    )
    return MonthDigest(
        scope=scope,
        partitions=partitions,
        threads=_build_threads(records),
        consensus=_build_consensus(records),
        book=_build_book(records, partitions),
        ledger=_build_ledger(records),
        comparability=_build_comparability(records, partitions),
        sources_digest=sources_digest(records),
        sources=build_sources(records),
    )


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

def _ratio(value: float | None) -> str:
    """Every ratio prints as a four-decimal fraction, the one convention every
    report in this project already uses. A percentage here and a fraction there
    is how 0.0341 gets read as 3.41."""
    return "n/a" if value is None else f"{value:.4f}"


def _signed(value: float | None) -> str:
    return "n/a" if value is None else f"{value:+.4f}"


def _money(value: float | None) -> str:
    return "n/a" if value is None else f"{value:,.2f}"


def _signed_money(value: float | None) -> str:
    return "n/a" if value is None else f"{value:+,.2f}"


def _table(headers: Sequence[str], rows: Sequence[Sequence[str]], indent: str = "  ") -> list[str]:
    """A space-aligned plain-text table, the shape every other report in this
    project prints. Not a pipe table: these files are read in a terminal at
    least as often as in a Markdown viewer."""
    widths = [len(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))
    lines = [indent + "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)).rstrip()]
    for row in rows:
        lines.append(
            indent + "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip()
        )
    return lines


def _leaderboard_rows(partition: Partition) -> list[list[str]]:
    rows: list[list[str]] = []
    for rank, row in enumerate(partition.rows, start=1):
        name = row.label + (f" (x{row.copies})" if row.copies > 1 else "")
        rows.append(
            [
                str(rank),
                name,
                _ratio(row.annual_return),
                _ratio(row.annual_volatility),
                _ratio(row.sharpe),
                _ratio(row.dividend_yield),
                _money(row.annual_dividend),
            ]
        )
    if partition.benchmark:
        bench = partition.benchmark
        rows.append(
            [
                "",
                f"benchmark {bench.ticker}",
                _ratio(bench.annual_return),
                _ratio(bench.annual_volatility),
                _ratio(bench.sharpe),
                "n/a",
                "n/a",
            ]
        )
    return rows


#: The section keys `render_digest` will place prose under, if it is given any.
PROSE_SECTIONS: tuple[str, ...] = (
    "headline",
    "exploration",
    "risk_return",
    "income",
    "consensus",
    "holdings_gap",
    "methodology",
    "next_runs",
)


def render_digest(digest: MonthDigest, prose: Mapping[str, str] | None = None) -> str:
    """The whole computed briefing as Markdown, optionally with prose placed
    between its sections.

    `prose` is a plain mapping from the section keys in `PROSE_SECTIONS` to
    paragraphs. It is deliberately typed as text and nothing more: this module
    computes figures and knows nothing about where sentences come from, so the
    prose pass can be a language model, a fixed string or absent entirely
    without this function changing. A key that is missing, empty, or equal to
    the string `NOT APPLICABLE` places nothing, which is how a section whose
    facts were unavailable keeps its own explanation instead of acquiring an
    invented one.

    A section with nothing to say prints one line naming the reason rather than
    disappearing: a reader who cannot tell "no what-if was tried" from "the
    what-if section is broken" cannot trust any of it.

    No line of this output is ever exactly `---`. The archive stores this text
    as the body beneath a `---`-fenced front-matter block, and a horizontal
    rule inside the body would read as a fence to anything less careful than
    `load_report`.
    """
    said = dict(prose or {})

    def paragraph(key: str) -> list[str]:
        text = (said.get(key) or "").strip()
        if not text or text == "NOT APPLICABLE":
            return []
        return [text, ""]

    scope = digest.scope
    out: list[str] = [f"# Portfolio archive summary - {scope.month}", ""]

    if not scope.report_count:
        out.append("No reports were read, so there is nothing to summarize.")
        return "\n".join(out) + "\n"

    kinds = f"{scope.portfolio_count} portfolio, {scope.whatif_count} whatif"
    out.append(f"{scope.report_count} reports ({kinds}).")
    if scope.as_of_first:
        span = (
            f"as-of {scope.as_of_first.isoformat()}"
            if scope.as_of_first == scope.as_of_last
            else f"as-of {scope.as_of_first.isoformat()} to {scope.as_of_last.isoformat()}"
        )
        out.append(f"Measured {span}.")
    if scope.currencies:
        out.append(f"Currencies: {', '.join(scope.currencies)}.")
    for currency, rates in scope.risk_free_rates:
        joined = ", ".join(_ratio(rate) for rate in rates)
        out.append(f"Risk-free rate used for {currency}: {joined}.")
    for currency, bench in scope.benchmarks:
        out.append(
            f"Benchmark for {currency}: {bench.ticker}, return {_ratio(bench.annual_return)}, "
            f"volatility {_ratio(bench.annual_volatility)}, Sharpe {_ratio(bench.sharpe)}."
        )
    for command in scope.commands:
        out.append(f"Command: {command}")
    for note in scope.skipped:
        out.append(f"Skipped: {note}")

    if paragraph("headline"):
        out += [""] + paragraph("headline")

    out += ["", "## Risk/return leaderboard", ""]
    out += paragraph("risk_return")
    out.append(
        "Ranked by Sharpe ratio WITHIN each returns window and currency only. Two "
        "portfolios measured over different windows are answers to different "
        "questions and are never ranked against each other."
    )
    for partition in digest.partitions:
        out += ["", f"{partition.title} - {len(partition.rows)} portfolio(s)", ""]
        out += _table(
            ["rank", "what", "return", "volatility", "Sharpe", "div yield", "div income"],
            _leaderboard_rows(partition),
        )
    if paragraph("income"):
        out += [""] + paragraph("income")

    out += ["", "## What you explored", ""]
    out += paragraph("exploration")
    if not digest.threads:
        out.append("n/a - no report could be grouped into a session.")
    for thread in digest.threads:
        out += [f"{thread.title} - {len(thread.steps)} step(s)", ""]
        for step in thread.steps:
            when = step.saved_at.strftime("%H:%M:%SZ") if step.saved_at else "time unknown"
            out.append(f"  {when}  {step.variant or 'step'}  {step.label}")
            out.append(
                f"      return {_ratio(step.annual_return)}  "
                f"volatility {_ratio(step.annual_volatility)}  "
                f"Sharpe {_ratio(step.sharpe)}  "
                f"div yield {_ratio(step.dividend_yield)}"
            )
            if step.return_change is not None or step.sharpe_change is not None:
                out.append(
                    f"      change: return {_signed(step.return_change)}  "
                    f"volatility {_signed(step.volatility_change)}  "
                    f"Sharpe {_signed(step.sharpe_change)}  "
                    f"div yield {_signed(step.dividend_yield_change)}"
                )
            if step.entered:
                out.append(f"      entered: {', '.join(step.entered)}")
            if step.left:
                out.append(f"      left: {', '.join(step.left)}")
        out.append("")

    out += ["## What every run agreed on", ""]
    out += paragraph("consensus")
    consensus = digest.consensus
    if consensus.reason:
        out.append(f"n/a - {consensus.reason}.")
    else:
        out.append(
            f"Across {len(consensus.runs)} runs on the same {len(consensus.candidates)}-ticker "
            f"pool ({', '.join(consensus.runs)}):"
        )
        out.append("")
        out.append(
            f"  Held by every run: {', '.join(consensus.always_held) or 'none'}"
        )
        out.append(
            f"  Never held by any run: {', '.join(consensus.never_held) or 'none'}"
        )
        if consensus.sometimes_held:
            out.append("  Held by some runs only:")
            for ticker, seen in consensus.sometimes_held:
                where = ", ".join(f"{name} {_ratio(weight)}" for name, weight in seen)
                out.append(f"    {ticker}: {where}")
        if consensus.run_specific:
            out.append("  Held by exactly one run:")
            for ticker, name, weight in consensus.run_specific:
                out.append(f"    {ticker}: {name} alone, weight {_ratio(weight)}")
        if consensus.negligible:
            out.append(
                "  Negligible weights (at or below "
                f"{_ratio(NEGLIGIBLE_WEIGHT)}, a token allocation rather than a holding):"
            )
            for ticker, name, weight in consensus.negligible:
                out.append(f"    {ticker}: {name}, weight {_ratio(weight)}")

    out += ["", "## Your book against the frontier", ""]
    out += paragraph("holdings_gap")
    book = digest.book
    if book.reason:
        out.append(f"n/a - {book.reason}.")
    else:
        out += _table(
            ["what", "return", "volatility", "Sharpe", "div yield", "div income"],
            [
                [
                    f"{book.book.label}",
                    _ratio(book.book.annual_return),
                    _ratio(book.book.annual_volatility),
                    _ratio(book.book.sharpe),
                    _ratio(book.book.dividend_yield),
                    _money(book.book.annual_dividend),
                ],
                [
                    f"best of the month: {book.best.label}",
                    _ratio(book.best.annual_return),
                    _ratio(book.best.annual_volatility),
                    _ratio(book.best.sharpe),
                    _ratio(book.best.dividend_yield),
                    _money(book.best.annual_dividend),
                ],
            ]
            + (
                [
                    [
                        f"benchmark {book.benchmark.ticker}",
                        _ratio(book.benchmark.annual_return),
                        _ratio(book.benchmark.annual_volatility),
                        _ratio(book.benchmark.sharpe),
                        "n/a",
                        "n/a",
                    ]
                ]
                if book.benchmark
                else []
            ),
        )
        out.append("")
        out.append(
            f"  Gap (best minus held): return {_signed(book.return_gap)}  "
            f"volatility {_signed(book.volatility_gap)}  "
            f"Sharpe {_signed(book.sharpe_gap)}  "
            f"div yield {_signed(book.dividend_yield_gap)}  "
            f"div income {_signed_money(book.annual_dividend_gap)}"
        )
        if book.same_window:
            out.append(f"  Both measured over {book.book_window}, so the gap is a real one.")
        else:
            out.append(
                f"  CAUTION: the book is measured over {book.book_window} and the best "
                f"portfolio over {book.best_window}. Part of this gap is the window, "
                "not the portfolios."
            )

    out += ["", "## What-if ledger", ""]
    ledger = digest.ledger
    if ledger.reason:
        out.append(f"n/a - {ledger.reason}.")
    for entry in ledger.entries:
        out.append(f"{entry.label} ({entry.partition})")
        against = entry.baseline_label or "unknown baseline"
        out.append(f"  against {against}")
        out.append(
            f"  return {_ratio(entry.annual_return)}  "
            f"volatility {_ratio(entry.annual_volatility)}  "
            f"Sharpe {_ratio(entry.sharpe)}  "
            f"div yield {_ratio(entry.dividend_yield)}  "
            f"div income {_money(entry.annual_dividend)}"
        )
        if entry.deltas:
            out.append(
                "  as the report printed it: "
                f"return {_signed(entry.deltas.get('return'))}  "
                f"volatility {_signed(entry.deltas.get('volatility'))}  "
                f"Sharpe {_signed(entry.deltas.get('sharpe'))}  "
                f"div yield {_signed(entry.deltas.get('dividend_yield'))}  "
                f"div income {_signed_money(entry.deltas.get('annual_income'))}"
            )
        if entry.added:
            out.append(f"  added: {', '.join(entry.added)}")
        if entry.removed:
            out.append(f"  removed: {', '.join(entry.removed)}")
        if entry.note:
            out.append(f"  note: {entry.note}")
        out.append("")

    out += ["## Comparability and methodology cautions", ""]
    out += paragraph("methodology")
    comparability = digest.comparability
    said_something = False
    if comparability.sensitivity:
        said_something = True
        out.append(
            "The SAME thing measured over more than one returns window. These are not "
            "different portfolios; they are one portfolio and two measurements of it."
        )
        for item in comparability.sensitivity:
            out.append("")
            out.append(f"  {item.subject}")
            out += _table(
                ["window", "return", "volatility", "Sharpe"],
                [
                    [window, _ratio(ret), _ratio(vol), _ratio(sharpe)]
                    for window, ret, vol, sharpe in item.measurements
                ],
                indent="    ",
            )
        out.append("")
    if comparability.rate_disagreements:
        said_something = True
        out.append("Risk-free rates that were not consistent within one currency:")
        for note in comparability.rate_disagreements:
            out.append(f"  {note}")
        out.append("")
    if comparability.lone_partitions:
        said_something = True
        out.append("Rankings that rank nothing:")
        for note in comparability.lone_partitions:
            out.append(f"  {note}")
        out.append("")
    if comparability.benchmark_coverage:
        said_something = True
        out.append("Benchmarks measured over less history than the portfolios beside them:")
        for note in comparability.benchmark_coverage:
            out.append(f"  {note}")
        out.append("")
    if len(comparability.windows) > 1:
        said_something = True
        out.append("Every window in this month, and which portfolios were measured on it:")
        for window, labels in comparability.windows:
            out.append(f"  {window}: {', '.join(labels)}")
        out.append("")
    if not said_something:
        out.append(
            "n/a - every report in this month shares one currency, one returns window "
            "and one risk-free rate, so they are directly comparable."
        )

    if said.get("next_runs"):
        out += ["", "## What to run next", ""]
        out += paragraph("next_runs")

    if digest.sources:
        out += ["", "## Source reports", ""]
        out.append(
            "Every report this briefing was built from. The `report` column is the eight "
            "characters that name the file, so a row worth following up can be opened."
        )
        out += ["", f"All under {scope.folder or 'the month folder'}/ :", ""]
        out += _table(
            ["report", "saved", "what", "file"],
            [
                [
                    source.digest8,
                    source.saved_at.strftime("%H:%M:%SZ") if source.saved_at else "n/a",
                    source.label,
                    source.filename,
                ]
                for source in digest.sources
            ],
        )

    out += [
        "",
        f"Digest of this set of {scope.report_count} report(s), which is what a repeated run "
        f"compares to recognize that it has already summarized them - not the digest of any "
        f"one file above: {digest.sources_digest}",
    ]
    return "\n".join(out).rstrip() + "\n"


def digest_for_llm(digest: MonthDigest) -> str:
    """The same facts, compact, for the prose agent's task input.

    Capped, because the model that reads this is the cheapest one available and
    a month with two hundred reports would not fit. Every cap that bites says
    so in the text, so the model is never silently shown a partial list and
    left to describe it as complete.
    """
    scope = digest.scope
    out: list[str] = [
        f"MONTH: {scope.month}",
        f"REPORTS: {scope.report_count} total, {scope.portfolio_count} portfolio, "
        f"{scope.whatif_count} whatif",
        f"CURRENCIES: {', '.join(scope.currencies) or 'unknown'}",
    ]
    if scope.as_of_first:
        out.append(
            f"AS_OF: {scope.as_of_first.isoformat()} to {scope.as_of_last.isoformat()}"
        )
    for currency, rates in scope.risk_free_rates:
        out.append(f"RISK_FREE {currency}: {', '.join(_ratio(r) for r in rates)}")
    for currency, bench in scope.benchmarks:
        out.append(
            f"BENCHMARK {currency}: {bench.ticker} return={_ratio(bench.annual_return)} "
            f"volatility={_ratio(bench.annual_volatility)} sharpe={_ratio(bench.sharpe)}"
        )

    out.append("")
    out.append("LEADERBOARD (ranked by Sharpe within each window; never across windows)")
    for partition in digest.partitions:
        out.append(f"- {partition.title}")
        for rank, row in enumerate(partition.rows[:LLM_MAX_ROWS_PER_PARTITION], start=1):
            out.append(
                f"  {rank}. {row.label}: return={_ratio(row.annual_return)} "
                f"volatility={_ratio(row.annual_volatility)} sharpe={_ratio(row.sharpe)} "
                f"dividend_yield={_ratio(row.dividend_yield)} "
                f"annual_dividend={_money(row.annual_dividend)}"
            )
        if len(partition.rows) > LLM_MAX_ROWS_PER_PARTITION:
            out.append(
                f"  (truncated: {len(partition.rows) - LLM_MAX_ROWS_PER_PARTITION} "
                "further portfolios in this window are not listed)"
            )

    out.append("")
    out.append("SESSIONS")
    for thread in digest.threads[:LLM_MAX_THREADS]:
        out.append(f"- {thread.title}")
        for step in thread.steps:
            line = f"  {step.variant or 'step'} {step.label}: sharpe={_ratio(step.sharpe)}"
            if step.sharpe_change is not None:
                line += f" (sharpe change {_signed(step.sharpe_change)})"
            if step.entered:
                line += f" entered {', '.join(step.entered)}"
            if step.left:
                line += f" left {', '.join(step.left)}"
            out.append(line)
    if len(digest.threads) > LLM_MAX_THREADS:
        out.append(f"  (truncated: {len(digest.threads) - LLM_MAX_THREADS} further sessions)")

    out.append("")
    consensus = digest.consensus
    if consensus.reason:
        out.append(f"CONSENSUS: none available - {consensus.reason}")
    else:
        out.append(f"CONSENSUS across runs: {', '.join(consensus.runs)}")
        out.append(f"  always_held: {', '.join(consensus.always_held[:LLM_MAX_TICKERS]) or 'none'}")
        out.append(f"  never_held: {', '.join(consensus.never_held[:LLM_MAX_TICKERS]) or 'none'}")
        for ticker, name, weight in consensus.run_specific[:LLM_MAX_TICKERS]:
            out.append(f"  held_by_one_run {name}: {ticker} weight={_ratio(weight)}")

    out.append("")
    book = digest.book
    if book.reason:
        out.append(f"HELD BOOK: none available - {book.reason}")
    else:
        out.append(
            f"HELD BOOK {book.book.label}: return={_ratio(book.book.annual_return)} "
            f"volatility={_ratio(book.book.annual_volatility)} "
            f"sharpe={_ratio(book.book.sharpe)} "
            f"dividend_yield={_ratio(book.book.dividend_yield)} "
            f"annual_dividend={_money(book.book.annual_dividend)}"
        )
        out.append(
            f"BEST OF MONTH {book.best.label}: return={_ratio(book.best.annual_return)} "
            f"volatility={_ratio(book.best.annual_volatility)} "
            f"sharpe={_ratio(book.best.sharpe)} "
            f"dividend_yield={_ratio(book.best.dividend_yield)}"
        )
        out.append(
            f"GAP best minus held: return={_signed(book.return_gap)} "
            f"volatility={_signed(book.volatility_gap)} sharpe={_signed(book.sharpe_gap)} "
            f"dividend_yield={_signed(book.dividend_yield_gap)} "
            f"annual_dividend={_signed_money(book.annual_dividend_gap)}"
        )
        out.append(
            f"GAP same window: {'yes' if book.same_window else 'NO - part of the gap is the window'}"
        )

    out.append("")
    ledger = digest.ledger
    if ledger.reason:
        out.append(f"WHAT-IFS: none - {ledger.reason}")
    for entry in ledger.entries[:LLM_MAX_LEDGER]:
        out.append(
            f"WHAT-IF {entry.label} ({entry.partition}) against "
            f"{entry.baseline_label or 'unknown baseline'}: "
            f"sharpe={_ratio(entry.sharpe)} dividend_yield={_ratio(entry.dividend_yield)} "
            f"printed deltas return={_signed(entry.deltas.get('return'))} "
            f"sharpe={_signed(entry.deltas.get('sharpe'))} "
            f"annual_income={_signed_money(entry.deltas.get('annual_income'))} "
            f"added={', '.join(entry.added) or 'none'} "
            f"removed={', '.join(entry.removed) or 'none'}"
        )
    if len(ledger.entries) > LLM_MAX_LEDGER:
        out.append(f"  (truncated: {len(ledger.entries) - LLM_MAX_LEDGER} further what-ifs)")

    out.append("")
    comparability = digest.comparability
    if not any(
        (
            comparability.sensitivity,
            comparability.rate_disagreements,
            comparability.lone_partitions,
            comparability.benchmark_coverage,
            len(comparability.windows) > 1,
        )
    ):
        out.append("COMPARABILITY: every report shares one currency, window and risk-free rate")
    for item in comparability.sensitivity:
        readings = "; ".join(
            f"{window}: return={_ratio(ret)} volatility={_ratio(vol)} sharpe={_ratio(sharpe)}"
            for window, ret, vol, sharpe in item.measurements
        )
        out.append(f"WINDOW SENSITIVITY {item.subject}: {readings}")
    for note in comparability.rate_disagreements:
        out.append(f"RATE DISAGREEMENT {note}")
    for note in comparability.lone_partitions:
        out.append(f"LONE PARTITION {note}")
    for note in comparability.benchmark_coverage:
        out.append(f"BENCHMARK COVERAGE {note}")

    return "\n".join(out).rstrip() + "\n"
