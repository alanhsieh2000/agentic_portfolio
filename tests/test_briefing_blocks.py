"""Tests for `src/agentic_portfolio/flow/briefing_blocks.py`.

Per AGENTS.md, no test here calls any LLM or any network. This module is pure
text handling and the tests are pure text too, but three of them build a REAL
briefing with `save_report` and `render_digest` rather than a hand-written
imitation. That is this project's own lesson, recorded in
`plans/19_monthly_report_summary.md`: a fixture less varied than the real
reports tests a smaller document than it claims, and the whole value of this
module is that it handles the document `render_digest` actually produces.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from agentic_portfolio.flow.briefing_blocks import (
    BLANK,
    BULLET,
    HEADING,
    PARAGRAPH,
    PREFORMATTED,
    Block,
    join_briefing,
    placeholders,
    protect_tokens,
    restore_tokens,
    split_briefing,
)
from agentic_portfolio.flow.report_archive import ReportArchive, save_report
from agentic_portfolio.flow.report_summary import (
    build_month_digest,
    load_month,
    render_digest,
)


def _kinds(text: str) -> list[str]:
    return [block.kind for block in split_briefing(text)]


def _of_kind(text: str, kind: str) -> list[Block]:
    return [block for block in split_briefing(text) if block.kind == kind]


def _archive(tmp_path, kind: str, as_of: str = "2026-09-11") -> ReportArchive:
    return ReportArchive(
        output_dir=str(tmp_path / "out"),
        enabled=True,
        kind=kind,
        as_of=date.fromisoformat(as_of),
        command=f"portfolio-{kind}",
    )


def _portfolio_body(objective: str, ret: str, sharpe: str) -> str:
    """A portfolio report body carrying the lines the parsers read.

    The headline-figures line and the weights block both matter: the archive
    dedupes by a digest of the body, so two fixtures that differ only in their
    front matter collapse onto one file and the month under test silently
    shrinks.
    """
    return (
        f"Objective: {objective}\n"
        f"Headline figures: return={ret}  volatility=0.1000  Sharpe={sharpe}\n"
        "Benchmark SPY: return=0.1225  volatility=0.1481  Sharpe=0.5704  (60 of 60 month(s))\n"
        "\n"
        "Weights:\n"
        "  GOOGL: 0.5000\n"
        "  T: 0.3000\n"
        "  NVDA: 0.2000\n"
    )


def _month(tmp_path) -> str:
    """Two real portfolio reports, rendered into a real briefing."""
    for objective, ret, sharpe in (("MSR", "0.2513", "2.0207"), ("GMV", "0.0480", "0.2394")):
        save_report(
            _portfolio_body(objective, ret, sharpe),
            _archive(tmp_path, "portfolio"),
            {
                "as_of": "2026-09-11",
                "currency": "USD",
                "objective": objective,
                "selection": "user_provided",
                "variant": "initial",
                "candidates": "GOOGL, NVDA, T, SPY",
                "benchmark": "SPY",
                "benchmark_return": 0.1225,
                "risk_free_rate": 0.0380,
                "window_start": "2021-10-01",
                "window_end": "2026-09-01",
                "window_months": 60,
                "annual_return": float(ret),
                "annual_volatility": 0.1000,
                "sharpe": float(sharpe),
                "annual_dividend": 4753.20,
                "dividend_yield": 0.0475,
                "value": 100000.0,
            },
        )
    records, skipped = load_month(str(tmp_path / "out"), "2026-09")
    return render_digest(build_month_digest(records, "2026-09", skipped))


# --- the round trip, which is the law everything else depends on --------------


def test_an_empty_document_round_trips():
    assert join_briefing(split_briefing("")) == ""


def test_a_trailing_newline_survives_the_round_trip():
    # `splitlines` would drop this and every rebuilt report would lose a byte.
    text = "# Title\n\nprose\n"
    assert join_briefing(split_briefing(text)) == text


def test_a_document_without_a_trailing_newline_round_trips():
    text = "# Title\n\nprose"
    assert join_briefing(split_briefing(text)) == text


def test_a_real_rendered_briefing_round_trips(tmp_path):
    body = _month(tmp_path)
    assert join_briefing(split_briefing(body)) == body


def test_consecutive_blank_lines_are_each_kept(tmp_path):
    text = "a\n\n\n\nb"
    assert _kinds(text) == [PARAGRAPH, BLANK, BLANK, BLANK, PARAGRAPH]
    assert join_briefing(split_briefing(text)) == text


# --- classification -----------------------------------------------------------


def test_a_hash_line_is_a_heading():
    assert _kinds("# Portfolio archive summary - 2026-09") == [HEADING]


def test_a_second_level_heading_is_also_a_heading():
    assert _kinds("## Risk/return leaderboard") == [HEADING]


def test_a_dash_prefixed_line_is_a_bullet():
    assert _kinds("- uv run portfolio --objective MSR - settle it") == [BULLET]


def test_a_two_space_indent_is_preformatted_even_though_markdown_needs_four():
    # The rule this module exists for. Markdown would read this as prose.
    assert _kinds("  rank  what   return") == [PREFORMATTED]


def test_contiguous_indented_lines_become_one_block():
    text = "  rank  what\n  1     MSR\n  2     GMV"
    blocks = split_briefing(text)
    assert len(blocks) == 1
    assert blocks[0].kind == PREFORMATTED
    assert len(blocks[0].lines) == 3


def test_a_blank_line_between_two_indented_runs_stays_inside_the_block():
    # A window-sensitivity subject and the table under it are separated by a
    # blank line; splitting them would let a translator rewrite one and not the
    # other.
    text = "  positions PFF:6000 (USD)\n\n    window   return\n    60 months  0.0229"
    blocks = split_briefing(text)
    assert len(blocks) == 1
    assert len(blocks[0].lines) == 4


def test_a_blank_line_before_unindented_text_closes_the_block():
    text = "  rank  what\n\nOrdinary prose."
    assert _kinds(text) == [PREFORMATTED, BLANK, PARAGRAPH]


def test_a_deeper_indent_keeps_its_own_leading_spaces():
    text = "    window   return"
    assert split_briefing(text)[0].text == "    window   return"


def test_a_whitespace_only_line_is_blank_and_not_preformatted():
    assert _kinds("   ") == [BLANK]


def test_no_indented_line_of_a_real_briefing_lands_outside_a_pre_block(tmp_path):
    # The assertion the PDF renderer's correctness rests on.
    for block in split_briefing(_month(tmp_path)):
        if block.kind != PREFORMATTED:
            assert not block.text.startswith("  "), block


def test_a_real_briefing_has_tables_and_prose_and_headings(tmp_path):
    kinds = set(_kinds(_month(tmp_path)))
    assert {HEADING, PARAGRAPH, PREFORMATTED, BLANK} <= kinds


def test_only_prose_blocks_are_translatable(tmp_path):
    for block in split_briefing(_month(tmp_path)):
        assert block.translatable == (block.kind in (HEADING, PARAGRAPH, BULLET))


def test_a_preformatted_block_is_never_translatable(tmp_path):
    blocks = _of_kind(_month(tmp_path), PREFORMATTED)
    assert blocks, "the fixture month should render at least one table"
    assert not any(block.translatable for block in blocks)


# --- token protection ---------------------------------------------------------


def test_a_figure_is_protected():
    masked, tokens = protect_tokens("Sharpe 2.0207 this month")
    assert masked == "Sharpe [[0]] this month"
    assert tokens == ("2.0207",)


def test_a_thousands_separator_stays_inside_one_token():
    _, tokens = protect_tokens("income 27,834.00 USD")
    assert "27,834.00" in tokens


def test_a_month_is_one_token_and_not_two_around_its_hyphen():
    masked, tokens = protect_tokens("# Portfolio archive summary - 2026-09")
    assert masked == "# Portfolio archive summary - [[0]]"
    assert tokens == ("2026-09",)


def test_an_iso_date_is_one_token():
    masked, tokens = protect_tokens("Measured as-of 2026-09-11.")
    assert masked == "Measured as-of [[0]]."
    assert tokens == ("2026-09-11",)


def test_a_whole_command_becomes_a_single_placeholder():
    masked, tokens = protect_tokens(
        "- uv run portfolio --objective MSR --currency USD - settle it"
    )
    assert masked == "- [[0]] - settle it"
    assert tokens == ("uv run portfolio --objective MSR --currency USD",)


def test_a_saved_report_filename_is_one_token_and_not_its_digest():
    masked, tokens = protect_tokens("Skipped: 2026-09-11-summary-473f6f15.md: skipped")
    assert masked == "Skipped: [[0]]: skipped"
    assert tokens == ("2026-09-11-summary-473f6f15.md",)


def test_a_long_hex_digest_is_one_token():
    digest = "a5276d115b6d4ab77675f21dfab3e623003bfdb0f00ddf4cb2a1ed991cba389a"
    masked, tokens = protect_tokens(f"Digest of this set: {digest}")
    assert masked == "Digest of this set: [[0]]"
    assert tokens == (digest,)


def test_a_provider_model_name_keeps_the_sentence_period_outside_the_token():
    # Without the trailing-dot exclusion the placeholder ate the full stop and
    # the translated line read as an unterminated sentence.
    masked, tokens = protect_tokens("Prose: openai/gpt-5-nano. Every figure was computed.")
    assert masked.startswith("Prose: [[0]].")
    assert tokens[0] == "openai/gpt-5-nano"


def test_a_slash_inside_an_ordinary_word_is_not_mistaken_for_a_model_name():
    # `## Risk/return leaderboard` once came out as `## R[[0]] leaderboard`.
    masked, tokens = protect_tokens("## Risk/return leaderboard")
    assert masked == "## Risk/return leaderboard"
    assert tokens == ()


def test_a_ticker_combination_is_one_token():
    masked, tokens = protect_tokens("the held PFF+PFFA+VZ book")
    assert masked == "the held [[0]] book"
    assert tokens == ("PFF+PFFA+VZ",)


def test_a_single_letter_ticker_is_protected_where_it_stands_alone():
    masked, tokens = protect_tokens("Held by every run: T, GOOGL", ["T", "GOOGL"])
    assert masked == "Held by every run: [[0]], [[1]]"
    assert tokens == ("T", "GOOGL")


def test_a_single_letter_ticker_is_not_protected_inside_an_english_word():
    # The bug this anchoring exists for: `WITHIN` became `WI[[0]]HIN`.
    masked, tokens = protect_tokens("Ranked WITHIN each window. The Two runs.", ["T"])
    assert masked == "Ranked WITHIN each window. The Two runs."
    assert tokens == ()


def test_a_ticker_with_a_dot_is_protected():
    masked, tokens = protect_tokens("never held CSPX.L at all", ["CSPX.L"])
    assert masked == "never held [[0]] at all"
    assert tokens == ("CSPX.L",)


def test_adjacent_protected_words_merge_into_one_placeholder():
    masked, _ = protect_tokens("pool GOOGL NVDA here", ["GOOGL", "NVDA"])
    assert masked == "pool [[0]] here"


def test_an_empty_literal_is_ignored():
    masked, tokens = protect_tokens("nothing to protect", ["", ""])
    assert masked == "nothing to protect"
    assert tokens == ()


def test_restoring_gives_back_the_original_text():
    original = "Benchmark for USD: SPY, return 0.1225, Sharpe 0.5704."
    masked, tokens = protect_tokens(original, ["USD", "SPY"])
    assert restore_tokens(masked, tokens) == original


def test_restoring_a_real_briefings_prose_gives_back_every_line(tmp_path):
    for block in split_briefing(_month(tmp_path)):
        if block.translatable:
            masked, tokens = protect_tokens(block.text, ["USD", "SPY", "GOOGL", "T", "NVDA"])
            assert restore_tokens(masked, tokens) == block.text


def test_placeholders_are_reported_in_the_order_they_appear():
    masked, _ = protect_tokens("return 0.1225 volatility 0.0517 Sharpe 1.6347")
    assert placeholders(masked) == (0, 1, 2)


def test_a_reordered_translation_is_visible_in_its_placeholder_order():
    # This is what makes the verifier able to reject a figure moved onto a
    # different noun rather than only a figure that vanished.
    assert placeholders("Sharpe [[2]], return [[0]], volatility [[1]]") == (2, 0, 1)


def test_restoring_leaves_an_unknown_index_visible_rather_than_raising():
    assert restore_tokens("a [[7]] b", ("only",)) == "a [[7]] b"


def test_a_translation_that_kept_its_placeholders_restores_to_real_figures():
    original = "Benchmark for USD: SPY, return 0.1225."
    masked, tokens = protect_tokens(original, ["USD", "SPY"])
    translated = masked.replace("Benchmark for", "基準").replace("return", "報酬")
    assert "0.1225" in restore_tokens(translated, tokens)
