"""Entry point for translating one month's portfolio briefing.

`src/agentic_portfolio/flow/report_summary.py` computes every figure a briefing
contains and `render_digest` lays it out. This module takes that finished
English document and asks a language model for the same document in another
language - and refuses to let it change anything else.

The split is the same one `src/agentic_portfolio/agents/report_summary.py`
makes, applied one level up. There, the model writes prose around figures it was
handed and `verify_narrative` rejects any field stating a figure that was never
computed. Here, the model is not shown a figure at all: before any block leaves
this process, `briefing_blocks.protect_tokens` has replaced every figure, date,
ticker, filename, command and digest with an opaque marker. So the model's whole
obligation is to keep the markers, and checking that is exact rather than a
judgement call.

Two things follow that are worth stating plainly, because they are the reason
this design was chosen over translating the whole document as one string.

A TABLE IS NEVER SENT. `split_briefing` classifies every indented run as
preformatted, and only headings, paragraphs and bullets are translatable. The
leaderboard in a translated briefing is therefore the same bytes as the
leaderboard in the English one, not a reproduction of it. That is also what
keeps the columns aligned: `report_summary._table` pads with `str.ljust` while a
CJK glyph is double-width in a monospace face, so a translated table header
would break every row beneath it and the translated document would be harder to
read than the English one.

A FAILURE IS LOCAL. Each block is checked on its own, and one that fails keeps
its English text while its neighbours are translated. Verifying a whole
translated document instead would leave only one possible response to a single
bad digit - discard everything - and a feature that discards everything on one
slip is a feature that never fires.
"""

from __future__ import annotations

import logging
import re
from typing import Mapping, Sequence

from agentic_portfolio.agents.translate_crew.crew import ReportTranslationCrew
from agentic_portfolio.agents.translation_schema import TranslatedBriefing
from agentic_portfolio.config.preflight import api_key_problem
from agentic_portfolio.config.settings import settings
from agentic_portfolio.flow.briefing_blocks import (
    Block,
    join_briefing,
    placeholders,
    protect_tokens,
    restore_tokens,
    split_briefing,
)

logger = logging.getLogger(__name__)

#: Refuse rather than truncate above this many translatable blocks. `render_digest`
#: has no caps of its own - only `digest_for_llm` does - so a month holding two
#: hundred reports renders a document far larger than a small model's context.
#: Refusing with a sentence that names chunking as unimplemented is honest; a
#: silently half-translated briefing is not.
MAX_BLOCKS = 400

#: Below this share of blocks accepted, the whole translation is abandoned. A
#: document that is one fifth English reads as broken, and the English original
#: is right there and complete.
MIN_COVERAGE = 0.80

#: Below this share of ACCEPTED blocks actually differing from their English
#: source, the translation is abandoned as an echo. This is the one gate that
#: catches a small model repeating its input back: that output passes every
#: marker check perfectly and would otherwise be saved as a file labelled
#: `language: zh-TW` containing English. Script detection cannot substitute,
#: because `--language` is free-form and the target script is unknown.
MIN_CHANGED = 0.50


class TranslationUnavailable(RuntimeError):
    """The translation could not be produced, or was not worth keeping.

    Raised rather than returned so a caller cannot use a half-translated
    document by accident. `src/agentic_portfolio/flow/summary_cli.py` catches it,
    warns, and keeps the English briefing - which is the whole product with or
    without a translation.
    """


def structure_prefix(text: str) -> str:
    """The Markdown marker that makes `text` a heading or a list item.

    Returned as its own thing because it is structure rather than language, and
    a translation has to reproduce it byte for byte. `## ` translated away turns
    a section heading into an ordinary sentence, which is worse than a clumsy
    translation of the heading: the document silently loses a section.
    """
    heading = re.match(r"#{1,6} ", text)
    if heading:
        return heading.group(0)
    return "- " if text.startswith("- ") else ""


def verify_block(source: str, translated: str) -> str | None:
    """Why `translated` is not an acceptable translation of `source`, or `None`.

    Five checks, each the narrowest test of one real failure:

    - An empty translation is a dropped line dressed as a success.
    - A translation containing a newline has broken the one-line-per-block
      mapping that makes every other check and every rejection localizable.
    - A line that is exactly `---`, or reads as a horizontal rule, would be
      parsed as a front-matter fence by anything less careful than
      `load_report`. `render_digest` guarantees it never emits one, and a
      translation must not introduce what the renderer was careful to avoid.
    - A heading or a bullet must still be one. The marker is structure, not
      words, and a `## ` that got translated away demotes a whole section to a
      sentence - which the marker check below cannot see, because `#` is not a
      figure and a heading often carries no markers at all.
    - The markers must match exactly, in order. Once every figure, ticker, date
      and command is an opaque marker, this single comparison catches a dropped
      ticker, a localized date, a mangled digest and a figure moved onto a
      different noun - and it needs no opinion about what any number means.
    """
    if not translated.strip():
        return "came back empty"
    if "\n" in translated:
        return "spans more than one line"
    stripped = translated.strip()
    if stripped == "---" or (len(stripped) >= 3 and set(stripped) <= set("-=_*`")):
        return "reads as a front-matter fence"
    prefix = structure_prefix(source)
    if prefix and not translated.startswith(prefix):
        return f"dropped the leading {prefix.strip()!r}, which is the line's structure"
    if not prefix and structure_prefix(translated):
        return "added a heading or bullet marker the original did not have"
    wanted, got = placeholders(source), placeholders(translated)
    if wanted != got:
        if set(got) - set(wanted):
            return f"introduced marker(s) {sorted(set(got) - set(wanted))}"
        if set(wanted) - set(got):
            return f"dropped marker(s) {sorted(set(wanted) - set(got))}"
        return "reordered the markers, which moves a figure onto another noun"
    return None


def verify_translation(
    sources: Sequence[str], translated: Mapping[int, str]
) -> tuple[dict[int, str], tuple[str, ...]]:
    """The translations worth keeping, and one note per block left in English.

    `sources` are the MASKED blocks in the order they were offered; `translated`
    maps the offered index to what came back. An index with no entry is reported
    as missing rather than silently skipped, because a model that quietly
    stopped halfway and one that returned a bad line are different problems and
    the caller's message should say which.
    """
    kept: dict[int, str] = {}
    rejected: list[str] = []
    for index, source in enumerate(sources):
        candidate = translated.get(index)
        if candidate is None:
            rejected.append(f"block {index}: no translation returned")
            continue
        problem = verify_block(source, candidate)
        if problem:
            rejected.append(f"block {index}: {problem}")
        else:
            kept[index] = candidate
    return kept, tuple(rejected)


def _offer(blocks: Sequence[Block], literals: Sequence[str]) -> tuple[
    list[int], list[str], list[tuple[str, ...]]
]:
    """The translatable blocks, masked, with their positions and their tokens."""
    positions: list[int] = []
    masked: list[str] = []
    tokens: list[tuple[str, ...]] = []
    for position, block in enumerate(blocks):
        if not block.translatable:
            continue
        text, found = protect_tokens(block.text, literals)
        positions.append(position)
        masked.append(text)
        tokens.append(found)
    return positions, masked, tokens


def format_blocks(masked: Sequence[str]) -> str:
    """The numbered block list the task template interpolates.

    Braces are mapped to brackets. This is defensive rather than a fix for an
    observed bug: `crewai.utilities.string_utils.interpolate_only` raises
    `KeyError` for a `{name}` in the TEMPLATE it cannot resolve, and because
    interpolation runs once on the template, a brace arriving inside this value
    is safe today. It is one substitution, it changes nothing observable - a
    rendered briefing contains no braces - and it removes a dependence on the
    order in which a third party chooses to interpolate.
    """
    return "\n".join(
        f"{index}. {text.replace('{', '[').replace('}', ']')}"
        for index, text in enumerate(masked)
    )


def translate_briefing(
    body: str,
    language: str,
    literals: Sequence[str] = (),
    model: str | None = None,
    month: str = "",
) -> tuple[str, str, tuple[str, ...]]:
    """`body` with its prose in `language`, the disclaimer, and one note per
    block left in English.

    `month` reaches the prompt so the model knows which period it is writing
    about. Optional, because nothing about the translation itself depends on it.

    Raises `TranslationUnavailable` when the model cannot be reached, when it
    returns nothing usable, or when what it returned is not worth saving. Every
    refusal that can be decided without a model is decided before the crew is
    built, so an impossible request costs nothing.
    """
    if not language.strip():
        raise TranslationUnavailable("no language was given, so there is nothing to translate into")

    blocks = split_briefing(body)
    positions, masked, tokens = _offer(blocks, literals)
    if not positions:
        raise TranslationUnavailable(
            "this briefing has no prose to translate - every line of it is a table"
        )
    if len(positions) > MAX_BLOCKS:
        raise TranslationUnavailable(
            f"this briefing has {len(positions)} blocks of prose, more than the {MAX_BLOCKS} "
            "this command will send in one request; translating a month this large would need "
            "chunking, which is not implemented"
        )

    resolved_model = model or settings.llm_quick
    # The remedy here is NOT `--no-llm`, which this command refuses alongside
    # `--language`; naming a flag that would be rejected is worse than naming
    # none. Dropping `--language` is the actual way to get a briefing.
    problem = api_key_problem(
        resolved_model,
        purpose=f"this briefing cannot be translated into {language}",
        remedy="drop --language to save the English briefing on its own",
    )
    if problem:
        raise TranslationUnavailable(problem)

    try:
        result = ReportTranslationCrew(model=resolved_model).crew().kickoff(
            inputs={
                "language": language,
                # Named so the model knows the period it is writing about. An
                # empty argument would leave a dangling "covering ." in the
                # prompt, so it falls back to a phrase rather than to nothing.
                "month": month or "the month covered by this briefing",
                "block_count": len(masked),
                "blocks": format_blocks(masked),
            }
        )
    except Exception as error:  # noqa: BLE001 - any provider failure is the same to us
        logger.warning("translation failed with %s: %s", resolved_model, error)
        raise TranslationUnavailable(f"{resolved_model} failed: {error}") from error

    payload: TranslatedBriefing | None = getattr(result, "pydantic", None)
    if payload is None:
        raise TranslationUnavailable(
            f"{resolved_model} returned no structured translation, only free text"
        )

    returned = {block.index: block.text for block in payload.blocks}
    kept, rejected = verify_translation(masked, returned)

    coverage = len(kept) / len(masked)
    if coverage < MIN_COVERAGE:
        missing = sum(1 for note in rejected if "no translation returned" in note)
        detail = (
            f"the model returned only {len(returned)} of {len(masked)} blocks"
            if missing > len(rejected) / 2
            else f"{len(rejected)} of {len(masked)} blocks were rejected"
        )
        raise TranslationUnavailable(
            f"{resolved_model} translated {coverage:.0%} of the briefing into {language}, "
            f"below the {MIN_COVERAGE:.0%} needed to be worth saving ({detail})"
        )

    changed = sum(1 for index, text in kept.items() if text.strip() != masked[index].strip())
    if kept and changed / len(kept) < MIN_CHANGED:
        raise TranslationUnavailable(
            f"{resolved_model} returned the English text largely unchanged "
            f"({changed} of {len(kept)} blocks differ), so there is nothing to save as "
            f"{language}"
        )

    rebuilt = list(blocks)
    for offer_index, position in enumerate(positions):
        if offer_index in kept:
            rebuilt[position] = Block(
                blocks[position].kind,
                restore_tokens(kept[offer_index], tokens[offer_index]),
            )
    return join_briefing(rebuilt), payload.disclaimer.strip(), rejected
