"""Take one rendered portfolio briefing apart into blocks, and put it back together.

`src/agentic_portfolio/flow/report_summary.py`'s `render_digest` is the only
thing that produces the text this module reads, and two later features both
needed the same question answered about it: which lines are prose, and which
lines are a table. `src/agentic_portfolio/agents/report_translation.py` asks so
it can translate the prose and never send a table to a language model;
`src/agentic_portfolio/flow/report_pdf.py` asks so it can wrap each table in a
`<pre>` element and keep its columns.

Answering it once, here, is what stops the two from drifting apart. Standard
library only: no LLM call, no network, no `crewai` and no `pydantic`, so either
caller can import this without pulling the other's dependencies in.

**Why an indented line is preformatted.** `render_digest` prints its tables as
space-aligned plain text indented by TWO spaces, padded with `str.ljust` - see
`_table` in `report_summary.py`, whose docstring records that these files are
read in a terminal at least as often as in a Markdown viewer. Markdown does not
agree: CommonMark needs a FOUR-space indent to make a code block, so a 2-space
indented line is a lazy continuation of the paragraph above it, and HTML then
collapses the very runs of spaces that ARE the alignment. So the rule here is
this project's own and not Markdown's: two spaces or more means "reproduce these
bytes exactly", and everything that depends on it says so.

**The round trip is a law, not an aspiration.** `join_briefing(split_briefing(t))
== t` for any text, and `tests/test_briefing_blocks.py` asserts it against real
rendered briefings. Both callers rebuild a document from these blocks, so a
splitter that lost a blank line or a trailing newline would corrupt a saved
report rather than merely render it oddly.
"""

from __future__ import annotations

import re
from typing import Iterable, NamedTuple, Sequence

#: A block's `kind`. `preformatted` is the one that carries more than one line.
HEADING = "heading"
PARAGRAPH = "paragraph"
BULLET = "bullet"
PREFORMATTED = "preformatted"
BLANK = "blank"

#: How a protected span appears in the text handed to a language model. ASCII
#: and doubled on purpose: a small model copies `[[3]]` far more reliably than an
#: unusual Unicode bracket, and no briefing `render_digest` produces contains a
#: doubled square bracket, so the marker cannot collide with real content.
_PLACEHOLDER = "[[{}]]"
_PLACEHOLDER_RE = re.compile(r"\[\[(\d+)\]\]")

#: The five narrow patterns for spans that must survive translation but are not
#: nouns the archive knows by name. Ordered longest-construct-first, because
#: overlap is resolved by preferring the span that starts earliest and, among
#: those, the one that reaches furthest - so a filename wins over the hex digest
#: inside it, and a whole command wins over each flag in it.
#:
#: Three of the alternatives exist to avoid a specific observed mistake rather
#: than on principle, and all three were found by masking the real archive:
#:
#: - The trailing-dot exclusion in the provider-name alternative keeps
#:   `Prose: openai/gpt-5-nano.` from losing its sentence period into the
#:   placeholder, which left the translated line an unterminated sentence.
#: - That same alternative is anchored on its left and requires a digit or
#:   hyphen after the slash, because without either the heading
#:   `## Risk/return leaderboard` matched `isk/return` and the section title
#:   came out as `## R[[0]] leaderboard`.
#: - `YYYY-MM` is matched as one span, because the figure alternative otherwise
#:   split the title's `2026-09` into two placeholders around a hyphen, and a
#:   translator asked to keep `[[0]]-[[1]]` adjacent has been handed a puzzle
#:   instead of a date.
_NARROW = re.compile(
    r"""
      uv\ run\ [a-z][a-z-]*
        (?:\ (?:--[a-z][a-z-]*|[A-Z0-9.+:]+|[a-z_]+|[0-9.]+))*   # a whole command
    | [^\s/]+(?:/[^\s/]+)*\.md                            # a saved report's filename
    | output/\d{4}-\d{2}/                                 # a month folder
    | \d{4}-\d{2}-\d{2}                                   # an ISO date
    | \d{4}-\d{2}(?!\d)                                   # a YYYY-MM month
    | [0-9a-f]{16,}                                       # a report or source digest
    | (?<![0-9A-Za-z])[a-z][a-z0-9]*/[a-z0-9]*[0-9-][a-z0-9-]*(?:\.[a-z0-9-]+)*
                                                          # a provider/model name
    | [A-Z][A-Z0-9.]*(?:\+[A-Z][A-Z0-9.]*)+                # a ticker combination
    | [0-9][0-9,]*(?:\.[0-9]+)?                           # a figure
    """,
    re.VERBOSE,
)


class Block(NamedTuple):
    """One run of a briefing that is handled as a unit.

    `text` holds the block's own lines joined by newlines and nothing else - no
    trailing newline, no added indentation - so `join_briefing` is a plain
    newline join and cannot introduce or lose whitespace.
    """

    kind: str
    text: str

    @property
    def lines(self) -> tuple[str, ...]:
        return tuple(self.text.split("\n"))

    @property
    def translatable(self) -> bool:
        """Whether this block's words may be sent to a language model.

        A `preformatted` block never may: its bytes are a table, and a table is
        reproduced rather than rewritten. A `blank` block has no words.
        """
        return self.kind in (HEADING, PARAGRAPH, BULLET)


def _indented(line: str) -> bool:
    """Whether `line` is part of a table or other preformatted run.

    Two spaces, not four, for the reason in the module docstring. A line of only
    whitespace is not indented content - it is a blank line, and the caller
    decides whether it falls inside a preformatted run or ends one.
    """
    return line.startswith("  ") and bool(line.strip())


def _next_content_is_indented(lines: Sequence[str], start: int) -> bool:
    """Whether the next non-blank line at or after `start` is indented.

    This is the lookahead that keeps a window-sensitivity subject line and the
    4-space table beneath it in ONE block even though `render_digest` separates
    them by a blank line. Splitting them would let a translator rewrite the
    subject while the table below it stayed English, which reads as a bug.
    """
    for line in lines[start:]:
        if line.strip():
            return _indented(line)
    return False


def split_briefing(body: str) -> tuple[Block, ...]:
    """`body` as blocks, losing nothing.

    Headings, bullets and paragraphs are one line each, deliberately. A
    line-for-line mapping is what lets the translator reject one bad sentence
    and keep its neighbours, and what lets a rejection be reported by position.
    Only `preformatted` groups, because a table is only a table as a whole.
    """
    # `split` rather than `splitlines`: a trailing newline becomes a final empty
    # element, which the join restores. `splitlines` would drop it and the round
    # trip would quietly shorten every document by one byte.
    lines = body.split("\n")
    blocks: list[Block] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if _indented(line):
            run = [line]
            index += 1
            while index < len(lines):
                if _indented(lines[index]):
                    run.append(lines[index])
                    index += 1
                elif not lines[index].strip() and _next_content_is_indented(lines, index + 1):
                    run.append(lines[index])
                    index += 1
                else:
                    break
            blocks.append(Block(PREFORMATTED, "\n".join(run)))
            continue
        if not line.strip():
            blocks.append(Block(BLANK, line))
        elif line.startswith("#"):
            blocks.append(Block(HEADING, line))
        elif line.startswith("- "):
            blocks.append(Block(BULLET, line))
        else:
            blocks.append(Block(PARAGRAPH, line))
        index += 1
    return tuple(blocks)


def join_briefing(blocks: Iterable[Block]) -> str:
    """The inverse of `split_briefing`, exactly."""
    return "\n".join(block.text for block in blocks)


def _spans(text: str, literals: Sequence[str]) -> list[tuple[int, int]]:
    """The character ranges of `text` that must survive translation.

    Literals are anchored on both sides against alphanumerics, and that is not
    tidiness. Single-letter tickers exist - `T` is one, and it is in the real
    September candidate pool - so an unanchored match masks the `T` inside
    `WITHIN`, `The` and `Two`, turning English the translator was supposed to
    render into an opaque token. Anchoring on alphanumerics rather than `\\b`
    also keeps `CSPX.L` and `PFF+PFFA+VZ` matchable, since a dot and a plus are
    not word characters.
    """
    found: list[tuple[int, int]] = []
    for literal in literals:
        if not literal:
            continue
        pattern = rf"(?<![0-9A-Za-z]){re.escape(literal)}(?![0-9A-Za-z])"
        found += [(m.start(), m.end()) for m in re.finditer(pattern, text)]
    found += [(m.start(), m.end()) for m in _NARROW.finditer(text)]

    # Earliest start wins; among equal starts the longest span wins, so a
    # filename beats the digest inside it. Then adjacent spans separated by at
    # most one space merge, so a whole command line becomes one placeholder
    # instead of a dozen the model has to keep in order.
    found.sort(key=lambda span: (span[0], -span[1]))
    merged: list[tuple[int, int]] = []
    for start, end in found:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        elif merged and text[merged[-1][1] : start] == " ":
            merged[-1] = (merged[-1][0], end)
        else:
            merged.append((start, end))
    return merged


def protect_tokens(
    text: str, literals: Sequence[str] = ()
) -> tuple[str, tuple[str, ...]]:
    """`text` with every span that must not be translated replaced by a
    placeholder, and those spans in the order they appeared.

    The placeholders are what make verification exact rather than approximate.
    Once a figure, ticker, date, digest, filename or command is an opaque
    `[[n]]`, checking a translation is comparing two short lists of markers for
    equality - which catches a dropped ticker, a localized date and a mangled
    digest with one rule, and needs no judgement about what a number means.

    `literals` should come from the month's own data rather than from a regex
    over capital letters. A `[A-Z]{2,}` rule looks equivalent and is not: it
    protects `CAUTION`, `WITHIN` and `SAME`, which are English words the caller
    exists to translate.
    """
    pieces: list[str] = []
    tokens: list[str] = []
    last = 0
    for start, end in _spans(text, literals):
        pieces.append(text[last:start])
        pieces.append(_PLACEHOLDER.format(len(tokens)))
        tokens.append(text[start:end])
        last = end
    pieces.append(text[last:])
    return "".join(pieces), tuple(tokens)


def placeholders(text: str) -> tuple[int, ...]:
    """Every placeholder index in `text`, in the order it appears.

    Order matters and is checked: a translation that keeps all the right markers
    but reorders them has moved a figure onto a different noun, which is a
    wrong statement rather than a stylistic difference.
    """
    return tuple(int(m.group(1)) for m in _PLACEHOLDER_RE.finditer(text))


def restore_tokens(text: str, tokens: Sequence[str]) -> str:
    """`text` with each `[[n]]` replaced by `tokens[n]`.

    An index with no token is left as written rather than raising. By the time
    this runs the translation has already been accepted by a verifier that
    compared the markers, so an out-of-range index cannot reach here from the
    normal path; leaving it visible makes a future bug obvious in the output
    instead of turning it into a traceback in front of a user.
    """

    def swap(match: re.Match[str]) -> str:
        index = int(match.group(1))
        return tokens[index] if 0 <= index < len(tokens) else match.group(0)

    return _PLACEHOLDER_RE.sub(swap, text)
