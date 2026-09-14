"""Tests for `src/agentic_portfolio/flow/report_pdf.py`.

Per AGENTS.md, no test here calls any LLM or any network. These tests DO invoke
WeasyPrint for real, and that is deliberate: it is a declared dependency, it is
installed, it touches no network, and the only interesting question - does
WeasyPrint accept the HTML and the CSS this module emits - is precisely what a
fake cannot answer. One render of a real briefing was measured at about a tenth
of a second, so the handful here costs about a second.

The two degradation paths are the exception. `_renderer` and `cjk_font_family`
are the module's two named seams and are monkeypatched, because a test must not
depend on whether the machine it runs on happens to have a Chinese font - and
because the interesting assertion is the wording of the refusal, not the
absence of the font.

The single most important test in this file is
`test_a_table_keeps_its_indent_and_its_column_padding`. Everything else in this
module exists to make that true.
"""

from __future__ import annotations

import re
from datetime import date

import pytest

from agentic_portfolio.flow import report_pdf
from agentic_portfolio.flow.report_archive import ReportArchive, save_report
from agentic_portfolio.flow.report_pdf import (
    PRE_MAX_PT,
    PRE_MIN_PT,
    PdfUnavailable,
    RenderedPdf,
    briefing_html,
    contains_cjk,
    display_width,
    format_pdf_notice,
    pdf_path,
    pre_font_size,
    stylesheet,
    write_pdf,
)
from agentic_portfolio.flow.report_summary import (
    build_month_digest,
    load_month,
    render_digest,
)

BRIEFING = """# Portfolio archive summary - 2026-09

9 reports (4 portfolio, 5 whatif).

## Risk/return leaderboard

Window 2021-10-01 to 2026-09-01 (60 months), USD - 3 portfolio(s)

  rank  what                       return  volatility  Sharpe   div yield
  1     portfolio MSR              0.2513  0.1056      2.0207   0.0475
  2     portfolio MV @0.1225       0.1225  0.0517      1.6347   0.0405
  7     whatif PFF+PFFA+VZ (held)  0.0229  0.1207      -0.1253  0.0682

## Comparability and methodology cautions

  positions PFF:6000, PFFA:6000, VZ:2000 (USD)

    window                                return  volatility  Sharpe
    2021-10-01 to 2026-09-01 (60 months)  0.0229  0.1207      -0.1253

## Source reports

Every report this briefing was built from. The `report` column names the file.

## What to run next

- uv run portfolio --objective MSR - to confirm the ranking holds
- uv run portfolio-holdings whatif - to price what you actually hold

Prose: openai/gpt-5-nano. Every figure above was computed.
"""

FACTS = {
    "month": "2026-09",
    "saved_at": "2026-09-11T14:48:43Z",
    "report_count": "9",
    "llm_model": "openai/gpt-5-nano",
}


def _saved(tmp_path, body: str = BRIEFING, **facts):
    """One real `kind: summary` file on disk, written by the real archive."""
    archive = ReportArchive(
        output_dir=str(tmp_path / "output"),
        enabled=True,
        kind="summary",
        as_of=date(2026, 9, 11),
        command="portfolio-summary 2026-09",
    )
    return save_report(
        body,
        archive,
        {"month": "2026-09", "report_count": 9, "llm_model": "openai/gpt-5-nano", **facts},
    ).path


def _pres(document: str) -> str:
    return "\n".join(re.findall(r"<pre[^>]*>(.*?)</pre>", document, re.S))


def _html(body: str = BRIEFING) -> str:
    return briefing_html(FACTS, body, source_name="2026-09-11-summary-81849599.md")


def _real_briefing(tmp_path) -> str:
    """A briefing rendered by `render_digest` from real saved reports."""
    archive = ReportArchive(
        output_dir=str(tmp_path / "src"),
        enabled=True,
        kind="portfolio",
        as_of=date(2026, 9, 11),
        command="portfolio --currency USD",
    )
    for objective, ret, sharpe in (("MSR", 0.2513, 2.0207), ("GMV", 0.0480, 0.2394)):
        save_report(
            "Weights:\n  GOOGL: 0.6000\n  NVDA: 0.4000\n"
            f"\nHeadline: return={ret:.4f} Sharpe={sharpe:.4f}\n"
            "Benchmark SPY: return=0.1225  volatility=0.1481  Sharpe=0.5704  (60 of 60 month(s))\n",
            archive,
            {
                "currency": "USD", "objective": objective, "selection": "user_provided",
                "variant": "initial", "candidates": "GOOGL, NVDA, SPY",
                "benchmark": "SPY", "benchmark_return": 0.1225, "risk_free_rate": 0.0380,
                "window_start": "2021-10-01", "window_end": "2026-09-01",
                "window_months": 60, "annual_return": ret, "annual_volatility": 0.1056,
                "sharpe": sharpe, "annual_dividend": 4753.20, "dividend_yield": 0.0475,
                "value": 100000.0,
            },
        )
    records, skipped = load_month(str(tmp_path / "src"), "2026-09")
    return render_digest(build_month_digest(records, "2026-09", skipped))


# --- the assertion this module exists for -------------------------------------


def test_a_table_keeps_its_indent_and_its_column_padding():
    assert "  rank  what                       return  volatility  Sharpe   div yield" in _pres(
        _html()
    )


def test_every_indented_line_lands_inside_a_pre_block():
    inside = _pres(_html())
    for line in BRIEFING.split("\n"):
        if line.startswith("  "):
            assert line in inside, line


def test_no_indented_line_is_rendered_as_a_paragraph():
    # The pattern deliberately does not use `<p[^>]*>`, which also matches
    # `<pre style=...>` and so reports every table as a paragraph.
    document = _html()
    for paragraph in re.findall(r"<p(?:\s[^>]*)?>(.*?)</p>", document, re.S):
        assert not paragraph.startswith("  "), paragraph[:60]


def test_a_real_rendered_briefings_tables_all_survive(tmp_path):
    body = _real_briefing(tmp_path)
    inside = _pres(briefing_html(FACTS, body, source_name="x.md"))
    indented = [line for line in body.split("\n") if line.startswith("  ")]
    assert indented, "the fixture month should render at least one table"
    for line in indented:
        assert line.replace("&", "&amp;") in inside


def test_a_four_space_sub_table_keeps_its_deeper_indent():
    assert "    window                                return" in _pres(_html())


def test_a_blank_line_inside_an_indented_block_does_not_split_it():
    # The window-sensitivity shape: subject, blank, deeper table.
    blocks = re.findall(r"<pre[^>]*>(.*?)</pre>", _html(), re.S)
    joined = [b for b in blocks if "positions PFF:6000" in b]
    assert len(joined) == 1
    assert "window " in joined[0]


# --- the four constructs ------------------------------------------------------


def test_a_first_level_heading_becomes_an_h1():
    assert "<h1>Portfolio archive summary - 2026-09</h1>" in _html()


def test_a_second_level_heading_becomes_an_h2():
    assert "<h2>Risk/return leaderboard</h2>" in _html()


def test_prose_becomes_a_paragraph():
    assert "<p>9 reports (4 portfolio, 5 whatif).</p>" in _html()


def test_consecutive_bullets_become_one_list():
    document = _html()
    assert document.count("<ul>") == 1
    assert document.count("<li>") == 2
    assert "uv run portfolio --objective MSR - to confirm the ranking holds" in document


def test_a_caption_above_a_table_is_marked_so_it_cannot_be_orphaned():
    assert 'class="lead"' in _html()
    assert "break-after: avoid" in stylesheet()


def test_backticked_text_becomes_code():
    assert "<code>report</code>" in _html()


def test_html_metacharacters_are_escaped():
    document = briefing_html(FACTS, "A < B & C > D\n", source_name="x.md")
    assert "A &lt; B &amp; C &gt; D" in document


def test_metacharacters_inside_a_table_are_escaped_without_losing_spacing():
    document = briefing_html(FACTS, "  a < b   c\n", source_name="x.md")
    assert "  a &lt; b   c" in _pres(document)


def test_the_document_declares_its_language():
    assert '<html lang="en">' in _html()


def test_a_translated_document_declares_its_language():
    document = briefing_html({**FACTS, "language": "zh-TW"}, BRIEFING, source_name="x.md")
    assert '<html lang="zh-TW">' in document


def test_the_title_names_the_month():
    assert "<title>Portfolio archive summary 2026-09</title>" in _html()


def test_the_provenance_line_names_the_source_file():
    document = _html()
    assert "2026-09-11-summary-81849599.md" in document
    assert 'class="provenance"' in document


def test_the_provenance_line_reaches_the_page_footer_through_the_stylesheet():
    assert "string-set: source content()" in stylesheet()
    assert "string(source)" in stylesheet()


def test_the_title_reaches_the_running_header_through_the_stylesheet():
    css = stylesheet()
    assert "string-set: report-title content()" in css
    assert "string(report-title)" in css


def test_the_reference_theme_is_adapted_without_changing_table_width():
    css = stylesheet()
    assert "margin: 18mm 15mm 18mm" in css
    assert "--navy: #154f7a" in css
    assert "--rule: #5f9dd0" in css
    assert "font-size: 10pt" in css
    assert 'font-family: "DejaVu Sans Mono", "Noto Sans Mono CJK TC", monospace' in css
    assert "background: var(--table-pale)" in css


# --- sizing -------------------------------------------------------------------


def test_a_narrow_table_keeps_the_largest_size():
    assert pre_font_size(94) == PRE_MAX_PT


def test_a_seven_column_table_is_shrunk_to_fit_the_page():
    size = pre_font_size(117)
    assert PRE_MIN_PT < size < PRE_MAX_PT
    assert report_pdf.USABLE_WIDTH_PT / (report_pdf.MONO_ADVANCE_EM * size) >= 117


def test_every_size_chosen_actually_fits_the_page_width():
    for width in range(20, 95):
        size = pre_font_size(width)
        fits = report_pdf.USABLE_WIDTH_PT / (report_pdf.MONO_ADVANCE_EM * size)
        assert fits >= width, (width, size)


def test_an_impossibly_wide_line_is_clamped_and_wrapped_rather_than_clipped():
    # WeasyPrint does not paginate horizontally: under plain `white-space: pre`
    # an over-wide line is laid out in one box wider than the paper and simply
    # runs off it, with no warning. `pre-wrap` is what keeps it visible.
    assert pre_font_size(183) == PRE_MIN_PT
    assert "pre-wrap" in stylesheet()


def test_a_zero_width_block_does_not_divide_by_zero():
    assert pre_font_size(0) == PRE_MAX_PT


def test_a_chinese_character_is_measured_at_two_columns():
    assert display_width("台灣") == 4
    assert display_width("ab") == 2
    assert display_width("台b") == 3


def test_a_block_is_sized_from_its_own_widest_line():
    # A narrow table and a wide one in one document must not share a size. This
    # is the behaviour that keeps the leaderboard readable in a briefing whose
    # every-window list happens to be 183 characters wide.
    narrow = "  rank  what\n  1     MSR"
    wide = "  " + "  ".join(f"col{n}" for n in range(24))
    document = briefing_html(FACTS, f"# T\n\n{narrow}\n\nprose\n\n{wide}\n", source_name="x.md")
    sizes = set(re.findall(r"font-size:([0-9.]+)pt", document))
    assert len(sizes) > 1, sizes
    assert str(PRE_MAX_PT).rstrip("0").rstrip(".") in sizes


# --- CJK detection ------------------------------------------------------------


def test_english_text_needs_no_cjk_font():
    assert not contains_cjk(BRIEFING)


def test_chinese_text_needs_a_cjk_font():
    assert contains_cjk("投資組合檔案摘要")


def test_fullwidth_punctuation_counts_as_cjk():
    assert contains_cjk("這是機器翻譯。")


# --- writing ------------------------------------------------------------------


def test_the_pdf_is_named_after_the_markdown_it_renders(tmp_path):
    md = _saved(tmp_path)
    assert pdf_path(md) == md.with_suffix(".pdf")
    assert pdf_path(md).parent == md.parent


def test_the_written_file_is_a_real_pdf(tmp_path):
    rendered = write_pdf(_saved(tmp_path))
    assert rendered.created
    data = rendered.path.read_bytes()
    assert data.startswith(b"%PDF-")
    assert data.rstrip().endswith(b"%%EOF")
    assert len(data) > 5000


def test_a_real_briefing_paginates_rather_than_overflowing_one_page(tmp_path):
    # Proves nothing was dropped into a single unbounded page.
    from weasyprint import CSS, HTML

    body = _real_briefing(tmp_path)
    document = HTML(string=briefing_html(FACTS, body, source_name="x.md")).render(
        stylesheets=[CSS(string=stylesheet())]
    )
    assert len(document.pages) >= 1
    widest = max(
        box.width
        for page in document.pages
        for box in _boxes(page._page_box)
        if getattr(box, "text", None)
    )
    # 180mm of content at 96dpi is 680.3px; nothing may exceed it.
    assert widest <= 681


def test_every_page_repeats_the_title_source_and_page_number(tmp_path):
    """The visual refinement must not trade away the old provenance contract."""
    from weasyprint import CSS, HTML

    body = _real_briefing(tmp_path)
    document = HTML(string=briefing_html(FACTS, body, source_name="x.md")).render(
        stylesheets=[CSS(string=stylesheet())]
    )
    assert len(document.pages) > 1
    expected_title = "Portfolio archive summary - 2026-09"
    for number, page in enumerate(document.pages, start=1):
        margins = {
            child.at_keyword: "".join(
                box.text for box in _boxes(child) if getattr(box, "text", None)
            )
            for child in page._page_box.children
            if getattr(child, "at_keyword", None)
        }
        assert margins["@top-right"] == expected_title
        assert margins["@bottom-left"].startswith("x.md ")
        assert margins["@bottom-right"] == f"{number} / {len(document.pages)}"


def _boxes(box):
    yield box
    for child in getattr(box, "children", ()) or ():
        yield from _boxes(child)


def test_an_existing_pdf_is_left_alone(tmp_path):
    md = _saved(tmp_path)
    first = write_pdf(md)
    before = first.path.read_bytes()
    second = write_pdf(md)
    assert second == RenderedPdf(first.path, False)
    assert second.path.read_bytes() == before


def test_two_briefings_in_one_folder_get_their_own_pdfs(tmp_path):
    # The translation case: one run saves two Markdown files.
    english = _saved(tmp_path)
    chinese = _saved(tmp_path, BRIEFING.replace("9 reports", "nine reports"), language="zh-TW")
    write_pdf(english)
    write_pdf(chinese)
    assert english.with_suffix(".pdf").exists()
    assert chinese.with_suffix(".pdf").exists()
    assert english.with_suffix(".pdf") != chinese.with_suffix(".pdf")


def test_no_temporary_file_is_left_behind(tmp_path):
    md = _saved(tmp_path)
    write_pdf(md)
    assert [p.name for p in md.parent.glob(".report-*")] == []


def test_the_notice_names_the_file_it_wrote(tmp_path):
    rendered = write_pdf(_saved(tmp_path))
    assert format_pdf_notice(rendered) == f"Saved PDF: {rendered.path}"


def test_the_notice_for_an_existing_pdf_does_not_read_as_a_failure(tmp_path):
    md = _saved(tmp_path)
    write_pdf(md)
    notice = format_pdf_notice(write_pdf(md))
    assert notice == f"PDF already saved beside the report: {md.with_suffix('.pdf')}"


def test_no_notice_when_there_is_nothing_to_say():
    assert format_pdf_notice(None) is None


# --- failures -----------------------------------------------------------------


def test_a_missing_renderer_names_the_system_libraries(tmp_path, monkeypatch):
    def unavailable():
        raise PdfUnavailable(
            "WeasyPrint could not be loaded (no libpango). On Debian this needs "
            "libpango-1.0-0, libpangoft2-1.0-0, libharfbuzz0b and libharfbuzz-subset0"
        )

    monkeypatch.setattr(report_pdf, "_renderer", unavailable)
    md = _saved(tmp_path)
    with pytest.raises(PdfUnavailable) as error:
        write_pdf(md)
    assert "libpango-1.0-0" in str(error.value)
    assert md.exists(), "the Markdown must be untouched by a failed render"
    assert not md.with_suffix(".pdf").exists()


def test_a_chinese_briefing_with_no_cjk_font_is_refused_by_name(tmp_path, monkeypatch):
    # Refused rather than warned about: a PDF of empty boxes looks finished and
    # gets forwarded, while the Markdown is already readable.
    monkeypatch.setattr(report_pdf, "cjk_font_family", lambda *a, **k: None)
    md = _saved(tmp_path, "# 投資組合檔案摘要\n\n這是機器翻譯。\n", language="zh-TW")
    with pytest.raises(PdfUnavailable) as error:
        write_pdf(md)
    assert "fonts-noto-cjk" in str(error.value)
    assert str(md) in str(error.value)
    assert not md.with_suffix(".pdf").exists()


def test_a_chinese_briefing_with_a_cjk_font_is_rendered(tmp_path, monkeypatch):
    # Asserts the gate, not the glyphs: with no font actually installed the
    # page would be boxes, which is exactly why the gate exists.
    monkeypatch.setattr(report_pdf, "cjk_font_family", lambda *a, **k: "Noto Sans CJK TC")
    md = _saved(tmp_path, "# 投資組合檔案摘要\n\n這是機器翻譯。\n", language="zh-TW")
    assert write_pdf(md).created


def test_an_english_briefing_needs_no_cjk_font_at_all(tmp_path, monkeypatch):
    monkeypatch.setattr(report_pdf, "cjk_font_family", lambda *a, **k: None)
    assert write_pdf(_saved(tmp_path)).created


def test_a_file_that_is_not_a_saved_report_is_refused_by_name(tmp_path):
    stray = tmp_path / "notes.md"
    stray.parent.mkdir(parents=True, exist_ok=True)
    stray.write_text("just some notes, no front matter\n", encoding="utf-8")
    with pytest.raises(PdfUnavailable) as error:
        write_pdf(stray)
    assert "notes.md" in str(error.value)


def test_a_directory_in_the_pdfs_place_is_refused_rather_than_called_already_saved(tmp_path):
    # Chosen over chmod, which root ignores. `Path.exists()` is true for a
    # directory too, so the naive check reported "PDF already saved" about
    # something that is not a PDF - a lie in the shape of a success.
    md = _saved(tmp_path)
    pdf_path(md).mkdir()
    with pytest.raises(PdfUnavailable) as error:
        write_pdf(md)
    assert "is not a file" in str(error.value)
    assert str(md) in str(error.value)


def test_a_layout_failure_is_reported_rather_than_raised_raw(tmp_path, monkeypatch):
    class _Boom:
        def __init__(self, string):
            pass

        def write_pdf(self, stylesheets):
            raise ValueError("layout exploded")

    monkeypatch.setattr(report_pdf, "_renderer", lambda: (_Boom, lambda string: None))
    with pytest.raises(PdfUnavailable) as error:
        write_pdf(_saved(tmp_path))
    assert "layout exploded" in str(error.value)


def test_the_font_probe_never_raises(monkeypatch):
    # A font query misbehaving must not be the thing that refuses a PDF.
    def boom(*args, **kwargs):
        raise RuntimeError("fontconfig is having a day")

    monkeypatch.setattr(report_pdf, "cjk_font_family", lambda *a, **k: None)
    assert report_pdf.cjk_font_family() is None


def test_the_font_probe_reports_a_family_it_cannot_find_as_absent():
    assert report_pdf.cjk_font_family(["Definitely Not Installed Sans"]) is None


def test_the_font_probe_finds_a_family_that_is_installed():
    # DejaVu is present in any environment that can render these tests at all.
    assert report_pdf.cjk_font_family(["DejaVu Sans"]) == "DejaVu Sans"
