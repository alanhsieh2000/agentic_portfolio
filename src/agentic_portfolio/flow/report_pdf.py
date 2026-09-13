"""Render a saved portfolio briefing as a PDF.

A briefing is Markdown, and Markdown asks the reader to have a viewer. This
module answers `uv run portfolio-summary --pdf` by turning one saved `.md` into
one `.pdf` beside it, with the tables still in columns.

**Why there is no Markdown library here.** `render_digest` prints its tables as
space-aligned plain text indented by TWO spaces and padded with `str.ljust`.
CommonMark needs FOUR spaces to make a code block, so a generic parser reads
every table in this document as prose, and HTML then collapses the very runs of
spaces that ARE the alignment. A library would therefore need a pre-processor
that already knew "an indented run is preformatted" - at which point the
pre-processor is the converter and the library renders the four constructs that
remain. `briefing_blocks.split_briefing` already is that rule, so this module
emits those four constructs directly and declares no new dependency.
`render_digest` is the only producer of this text and its construct set is
closed, so the mapping can be pinned by test.

**Why it renders from the file and not from memory.** `write_pdf` takes a path
and reads it back with `load_report`. Three things follow, and each is wanted:
the PDF provably matches the bytes that were saved rather than a parallel
rendering of them; `--pdf` works on the already-saved path, where this run
rendered no body at all, so someone who forgot the flag can get a PDF without
`--force` and without a second Markdown file; and it works for a translated
briefing whose text this module never saw, which is how one flag serves both.

**Why a missing CJK font is a refusal and not a warning.** WeasyPrint renders a
character it has no glyph for as an empty box, silently. A four-page PDF of
empty boxes looks finished, and that is the file that gets emailed, while the
Markdown it came from is already on disk and perfectly readable. So a briefing
containing CJK text is refused when no CJK font is installed, naming the package
to install. Every OTHER failure here degrades to a warning, because the Markdown
is the product and the PDF is a rendering of it.
"""

from __future__ import annotations

import html
import os
import re
import tempfile
import unicodedata
from importlib.resources import files
from pathlib import Path
from typing import Mapping, NamedTuple, Sequence

from agentic_portfolio.flow.briefing_blocks import (
    BLANK,
    BULLET,
    HEADING,
    PREFORMATTED,
    Block,
    split_briefing,
)
from agentic_portfolio.flow.report_archive import load_report

#: The packaged stylesheet, beside this module so hatchling ships it in the
#: wheel by the same convention that carries the crew prompt YAML files.
STYLESHEET = "report_pdf.css"

#: Content width of one A4 portrait page at the 15mm side margins
#: `report_pdf.css` sets: 210mm - 30mm = 180mm = 510pt. This constant and that
#: margin MUST agree, and both say so, because every table's font size is
#: derived from it.
USABLE_WIDTH_PT = 510.0

#: DejaVu Sans Mono's advance width, verified from the font rather than assumed:
#: `unitsPerEm` 2048 and an advance of 1233 for every glyph, so 0.6021 em. A
#: cross-check against WeasyPrint's own layout puts 94 characters at 9pt in
#: 679.1pt, inside 680.3pt of content. Switching the monospace family in the
#: stylesheet invalidates this number.
MONO_ADVANCE_EM = 0.6021

#: The largest and smallest size a table is set at. The maximum is the body size
#: rounded down; the minimum is where a reader stops being able to follow a row,
#: and a block that will not fit even there wraps instead of shrinking further.
PRE_MAX_PT = 9.0
PRE_MIN_PT = 6.5

#: CJK families worth looking for, most likely first. The stylesheet lists the
#: same set, so this is only used to decide whether to REFUSE - which is why it
#: can be a plain list rather than anything dynamic.
CJK_FAMILIES: tuple[str, ...] = (
    "Noto Sans CJK TC",
    "Noto Sans TC",
    "Source Han Sans TC",
    "Noto Sans CJK SC",
    "Noto Sans SC",
    "WenQuanYi Zen Hei",
    "AR PL UMing TW",
)

#: The Unicode blocks whose characters DejaVu has no glyph for and a CJK font
#: does: CJK symbols and punctuation, the unified ideographs and their
#: extension A, compatibility ideographs, and the fullwidth forms.
_CJK = re.compile(r"[　-〿㐀-䶿一-鿿豈-﫿＀-￯]")

#: One inline construct appears in a briefing, in the `Source reports` sentence.
_INLINE_CODE = re.compile(r"`([^`]+)`")


class PdfUnavailable(RuntimeError):
    """No PDF could be produced.

    Raised rather than returned so a caller cannot mistake a failure for a
    written file. `src/agentic_portfolio/flow/summary_cli.py` catches it, warns,
    and leaves the exit status alone - the Markdown is complete either way.
    """


class RenderedPdf(NamedTuple):
    """One PDF's place beside the briefing it renders.

    `created` is `False` when the file was already there, in which case nothing
    was rendered and `path` names what is on disk. That is a normal outcome and
    not an error, exactly as it is for `SavedReport`.
    """

    path: Path
    created: bool


def contains_cjk(text: str) -> bool:
    """Whether `text` needs a font DejaVu cannot provide."""
    return bool(_CJK.search(text))


def display_width(line: str) -> int:
    """How many monospace columns `line` occupies.

    A CJK glyph is double-width in a monospace face while `len` counts it as
    one, so sizing a block by `len` would under-measure a translated line and
    lay it off the page. `east_asian_width` returns `W` for wide and `F` for
    fullwidth; everything else is one column.
    """
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in line)


def pre_font_size(width: int) -> float:
    """The largest size, within the clamp, at which `width` columns fit a page.

    Arithmetic rather than taste: `USABLE_WIDTH_PT / (MONO_ADVANCE_EM * size)`
    columns fit at a given size, so the size that just fits is the quotient.
    Rounded down to a quarter point so the result is a tidy number and always
    errs narrow.

    A block wider than `PRE_MIN_PT` allows is clamped rather than shrunk
    further, and wraps. That is the deliberate choice: past this size a row is
    unreadable anyway, and `report_pdf.css`'s `pre-wrap` keeps the overflow
    visible instead of letting it run off the paper.
    """
    if width <= 0:
        return PRE_MAX_PT
    exact = USABLE_WIDTH_PT / (MONO_ADVANCE_EM * width)
    quantized = int(exact * 4) / 4
    return max(PRE_MIN_PT, min(PRE_MAX_PT, quantized))


def cjk_font_family(families: Sequence[str] = CJK_FAMILIES) -> str | None:
    """The first of `families` that is actually installed, or `None`.

    Asked of fontconfig through WeasyPrint's OWN already-loaded cffi handle, so
    this needs no additional distribution and no `fc-list` binary. That second
    point matters: fontconfig's LIBRARY is guaranteed wherever WeasyPrint can be
    imported at all, because WeasyPrint links it, while its command-line tools
    are a separate package the release image does not install.

    How the question is put: fontconfig always returns a match, substituting a
    default for a family it does not have, so a family counts as installed only
    when the match names the family that was asked for. Returns `None` rather
    than raising if the probe itself fails - a PDF should not be refused because
    a font query misbehaved, and the caller treats `None` as "no CJK font".
    """
    try:
        from weasyprint.text.ffi import ffi, fontconfig
    except (ImportError, OSError):  # pragma: no cover - exercised via _renderer
        return None
    try:
        config = fontconfig.FcInitLoadConfigAndFonts()
        for family in families:
            pattern = fontconfig.FcPatternCreate()
            fontconfig.FcPatternAddString(pattern, b"family", family.encode("utf-8"))
            fontconfig.FcConfigSubstitute(config, pattern, fontconfig.FcMatchPattern)
            fontconfig.FcDefaultSubstitute(pattern)
            result = ffi.new("FcResult *")
            matched = fontconfig.FcFontMatch(config, pattern, result)
            if matched == ffi.NULL:
                continue
            name = ffi.new("FcChar8 **")
            if fontconfig.FcPatternGetString(matched, b"family", 0, name) != 0:
                continue
            if ffi.string(name[0]).decode("utf-8").lower() == family.lower():
                return family
    except Exception:  # noqa: BLE001 - a font probe must never be the thing that fails
        return None
    return None


def stylesheet() -> str:
    """`report_pdf.css`, read from the installed package.

    `importlib.resources` rather than a path relative to this file, so it works
    from a wheel as well as a checkout - and so `tests/test_packaging.py` can
    prove the asset actually shipped.
    """
    return files(__package__).joinpath(STYLESHEET).read_text(encoding="utf-8")


def _inline(text: str) -> str:
    """One block's text as HTML: escaped, then the single inline construct."""
    escaped = html.escape(text, quote=False)
    return _INLINE_CODE.sub(r"<code>\1</code>", escaped)


def _pre(block: Block) -> str:
    """One indented run as a `<pre>`, sized from its own widest line.

    Nothing inside is touched but the HTML escaping: leading spaces, interior
    runs and `str.ljust` padding all survive, which is what `white-space:
    pre-wrap` then preserves on the page.
    """
    lines = block.lines
    size = pre_font_size(max(display_width(line) for line in lines))
    body = html.escape("\n".join(lines), quote=False)
    return f'<pre style="font-size:{size:g}pt">{body}</pre>'


def _provenance(facts: Mapping[str, str], source_name: str) -> str:
    """One line naming the file this PDF renders and how it was made.

    Not the whole front matter. Reproducing all fifteen facts would open a
    document meant for reading with a page of machine metadata, and they are one
    `cat` away in the Markdown. These four are the ones that let a reader tie
    the page in their hand back to the archive.
    """
    parts = [source_name]
    if facts.get("saved_at"):
        parts.append(facts["saved_at"])
    if facts.get("report_count"):
        parts.append(f"{facts['report_count']} source reports")
    if facts.get("language"):
        parts.append(f"language {facts['language']}")
    parts.append(f"prose: {facts.get('llm_model') or 'none'}")
    return html.escape(" · ".join(parts), quote=False)


def briefing_html(
    facts: Mapping[str, str], body: str, *, source_name: str
) -> str:
    """One saved briefing as a standalone HTML document.

    Consecutive bullets are gathered into one list, and a paragraph that sits
    immediately above an indented run is marked `lead` so the stylesheet can
    keep it on the same page as the table it introduces.
    """
    # Blank lines carry no meaning once the document is HTML: the stylesheet's
    # margins do that job, and keeping them would add empty paragraphs.
    blocks = [block for block in split_briefing(body) if block.kind != BLANK]
    language = facts.get("language") or "en"
    title = f"Portfolio archive summary {facts.get('month', '')}".strip()

    out: list[str] = [
        f'<!DOCTYPE html><html lang="{html.escape(language, quote=True)}"><head>',
        '<meta charset="utf-8">',
        f"<title>{html.escape(title, quote=False)}</title>",
        "</head><body>",
    ]

    heading_seen = False
    index = 0
    while index < len(blocks):
        block = blocks[index]
        if block.kind == HEADING:
            text = block.text.lstrip("#").strip()
            level = 1 if block.text.startswith("# ") else 2
            out.append(f"<h{level}>{_inline(text)}</h{level}>")
            if level == 1 and not heading_seen:
                heading_seen = True
                out.append(
                    f'<p class="provenance">{_provenance(facts, source_name)}</p>'
                )
            index += 1
        elif block.kind == BULLET:
            items = []
            while index < len(blocks) and blocks[index].kind == BULLET:
                items.append(f"<li>{_inline(blocks[index].text[2:])}</li>")
                index += 1
            out.append("<ul>" + "".join(items) + "</ul>")
        elif block.kind == PREFORMATTED:
            out.append(_pre(block))
            index += 1
        else:
            nxt = blocks[index + 1] if index + 1 < len(blocks) else None
            lead = ' class="lead"' if nxt and nxt.kind == PREFORMATTED else ""
            out.append(f"<p{lead}>{_inline(block.text)}</p>")
            index += 1

    out.append("</body></html>")
    return "".join(out)


def _renderer():
    """WeasyPrint's `HTML` and `CSS`, or a `PdfUnavailable` naming what is
    missing.

    Imported here rather than at module scope for two reasons. WeasyPrint
    `dlopen`s Pango through cffi at import time, so on a machine without those
    libraries the import raises `OSError` - and a module-level import would then
    break `portfolio-summary` entirely, including the plain Markdown run that
    needs no PDF at all. And it gives the tests one named seam to patch.
    """
    try:
        from weasyprint import CSS, HTML
    except (ImportError, OSError) as error:
        raise PdfUnavailable(
            f"WeasyPrint could not be loaded ({error}). On Debian this needs "
            "libpango-1.0-0, libpangoft2-1.0-0, libharfbuzz0b and libharfbuzz-subset0"
        ) from error
    return HTML, CSS


def pdf_path(md_path: Path | str) -> Path:
    """Where the PDF for one briefing goes: beside it, same name, `.pdf`.

    A suffix swap and nothing more. `report_filename` keeps sole ownership of
    how a briefing is named, and a PDF is a rendering of one rather than an
    archive entry of its own - it has no body to digest independently and no
    front matter. Sharing the stem means the pair sorts together and carries the
    same body digest in the name.
    """
    return Path(md_path).with_suffix(".pdf")


def render_pdf(facts: Mapping[str, str], body: str, *, source_name: str) -> bytes:
    """One briefing as PDF bytes."""
    HTML, CSS = _renderer()
    document = briefing_html(facts, body, source_name=source_name)
    try:
        return HTML(string=document).write_pdf(stylesheets=[CSS(string=stylesheet())])
    except PdfUnavailable:
        raise
    except Exception as error:  # noqa: BLE001 - any layout failure is the same to us
        raise PdfUnavailable(f"WeasyPrint could not lay out the briefing ({error})") from error


def write_pdf(md_path: Path | str) -> RenderedPdf:
    """Render the PDF for one saved briefing, or say why there is none.

    Idempotent: an existing PDF is left alone and reported as `created=False`,
    which is what makes `--pdf` safe to repeat and makes it the way to add a PDF
    to a month that was summarized before PDFs existed.
    """
    source = Path(md_path)
    target = pdf_path(source)
    if target.is_file():
        return RenderedPdf(target, False)
    if target.exists():
        # `exists()` is also true for a directory, and reporting "already saved"
        # about one would be a lie in the shape of a success.
        raise PdfUnavailable(
            f"{target} exists and is not a file, so no PDF can be written there. The briefing "
            f"itself is complete at {source}"
        )

    try:
        facts, body = load_report(source)
    except (ValueError, OSError, UnicodeDecodeError) as error:
        raise PdfUnavailable(
            f"{source} could not be read back as a saved report ({error})"
        ) from error

    if contains_cjk(body) and cjk_font_family() is None:
        raise PdfUnavailable(
            f"{source.name} contains characters that need a CJK font, and none is installed, "
            "so every one of them would print as an empty box. Install one (on Debian: "
            "apt-get install fonts-noto-cjk) and re-run with --pdf. The briefing itself is "
            f"complete at {source}"
        )

    content = render_pdf(facts, body, source_name=source.name)

    # Atomic for the same reason `report_archive._write_atomically` is: a reader
    # opening the folder sees either no PDF or a whole one, never a half-written
    # file that a viewer would reject. The temporary lives in the destination
    # directory so `os.replace` stays inside one filesystem.
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=".report-", suffix=".pdf")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(content)
        os.replace(tmp_path, target)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    return RenderedPdf(target, True)


def format_pdf_notice(rendered: RenderedPdf | None) -> str | None:
    """The one line a command prints after rendering, or `None`.

    Worded to match `format_save_notice`, including its refusal to apologize for
    a repeat: finding the PDF already there is the feature working.
    """
    if rendered is None:
        return None
    if rendered.created:
        return f"Saved PDF: {rendered.path}"
    return f"PDF already saved beside the report: {rendered.path}"
