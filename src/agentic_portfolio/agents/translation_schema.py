"""Structured output schema for the briefing translator.

The division of labour this schema enforces is the point of it, and it is the
same one `src/agentic_portfolio/agents/summary_schema.py` enforces one level
down. There, the model is handed figures already computed and asked only for
the sentences that say what they mean. Here, the model is handed sentences whose
figures have already been replaced by opaque markers, and asked only to render
those sentences in another language.

So the model never sees a number, a ticker, a filename or a command. It sees
`[[0]]` where each of those stood, and its whole obligation is to put the same
markers, in the same order, into a sentence in the target language. That makes
verification exact instead of a judgement call: two short lists of markers are
either equal or they are not.

Every field below carries a `Field(description=...)`, because a field
description reaches the model as part of the output schema and is therefore half
of the instruction rather than documentation of it. The other half is
`config/agents.yaml`'s backstory, and the third is mechanical:
`src/agentic_portfolio/agents/report_translation.py` rejects any block whose
markers changed and keeps the English for it.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class TranslatedBlock(BaseModel):
    """One translated line of the briefing, addressed by index."""

    index: int = Field(
        description=(
            "The index of the block you are translating, copied exactly from the numbered "
            "list you were given. Do not renumber, do not reorder, and do not invent an "
            "index that was not in the list."
        )
    )
    text: str = Field(
        description=(
            "That one block, in the target language, as a SINGLE line with no newline in "
            "it. Every [[0]], [[1]] marker in the original must appear in your text, the "
            "same number of times and in the same order, spelled exactly as it was - they "
            "stand for figures, dates, tickers, filenames and commands that must not "
            "change. Never translate, explain, reorder or remove a marker. Never add a "
            "marker that was not there. Keep any leading '#', '##' or '- ' exactly as it "
            "was, because it is the line's structure and not part of the sentence."
        )
    )


class TranslatedBriefing(BaseModel):
    """One month's briefing, translated block by block."""

    language: str = Field(
        description=(
            "The language you translated into, named in English, echoing what you were "
            "asked for. This is a check that the request was understood, not a choice."
        )
    )
    disclaimer: str = Field(
        description=(
            "One sentence IN THE TARGET LANGUAGE telling the reader that this document was "
            "machine-translated from an English briefing and that its tables and figures "
            "were not translated. No digits, and no markers."
        )
    )
    blocks: list[TranslatedBlock] = Field(
        description=(
            "One entry for every numbered block you were given, and no entries for "
            "anything else. If a block is only a command or only a marker, return it "
            "unchanged rather than omitting it."
        )
    )
