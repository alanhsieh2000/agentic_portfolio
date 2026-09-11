"""Tests for `src/flow/report_summary.py`, the deterministic half of the
monthly report summary.

Per AGENTS.md, no test here calls yfinance's live API or any LLM. There is
nothing in this module to mock: it reads saved report files and does
arithmetic. Inputs are built by writing real archive files with
`src/flow/report_archive.py`'s own `save_report`, so these tests exercise the
front-matter format the writer actually produces rather than a hand-rolled
imitation of it. The one exception is `saved_at`, which `save_report` stamps
from the clock; `_restamp` rewrites that single line afterwards, because the
ordering of a month's reports is a thing worth testing and the clock will not
cooperate.

The figures used throughout are the real ones from the nine reports in
`output/2026-09/`, so a regression here reads as a difference against a
briefing a person has already seen.
"""

from datetime import date

import pytest

from src.flow.report_archive import ReportArchive, load_report, save_report
from src.flow.report_summary import (
    NEGLIGIBLE_WEIGHT,
    build_month_digest,
    digest_for_llm,
    label,
    load_month,
    parse_benchmark,
    parse_weights,
    parse_whatif_deltas,
    render_digest,
    sources_digest,
)

MONTH = "2026-09"
AS_OF = date(2026, 9, 11)

WINDOW_60 = {"window_start": "2021-10-01", "window_end": "2026-09-01", "window_months": 60}
WINDOW_48 = {"window_start": "2022-10-03", "window_end": "2026-09-01", "window_months": 48}

#: The real 17-ticker `user_provided` pool from `output/2026-09/`.
CANDIDATES = (
    "AMLP, BIL, BOXX, CSPX.L, ENFR, GOOGL, MLPX, NVDA, PFF, PFFA, QQQI, SPY, T, "
    "TLT, TSM, VUAA.L, VZ"
)

#: The four real weight sets, keyed by the objective that produced them.
WEIGHTS = {
    "MV-benchmark": [
        ("BOXX", 0.2910), ("BIL", 0.2678), ("QQQI", 0.1325), ("MLPX", 0.0671),
        ("AMLP", 0.0600), ("T", 0.0564), ("ENFR", 0.0532), ("NVDA", 0.0375),
        ("GOOGL", 0.0345),
    ],
    "MSR": [
        ("MLPX", 0.1717), ("NVDA", 0.1698), ("QQQI", 0.1475), ("AMLP", 0.1366),
        ("ENFR", 0.1198), ("T", 0.1018), ("GOOGL", 0.0835), ("BOXX", 0.0693),
    ],
    "GMV": [
        ("BIL", 0.3155), ("BOXX", 0.3147), ("PFF", 0.0935), ("TLT", 0.0723),
        ("QQQI", 0.0598), ("PFFA", 0.0567), ("AMLP", 0.0318), ("T", 0.0176),
        ("GOOGL", 0.0144), ("ENFR", 0.0142), ("MLPX", 0.0095),
    ],
    "MV-0.1000": [
        ("BOXX", 0.3124), ("BIL", 0.2991), ("QQQI", 0.1192), ("AMLP", 0.0501),
        ("MLPX", 0.0487), ("T", 0.0440), ("PFFA", 0.0431), ("ENFR", 0.0404),
        ("GOOGL", 0.0246), ("NVDA", 0.0178), ("TLT", 0.0005),
    ],
}


def _portfolio_body(weights, benchmark=True, window=WINDOW_60, figures=None, shares=100) -> str:
    """A portfolio report body of the real shape.

    The headline-figures line and the share allocation are both included
    because both appear in the real reports and both are part of what a digest
    covers. That matters here: the archive dedupes by a digest of the BODY, so
    a helper that left them out would make two genuinely different runs collide
    into one file and quietly shrink the fixture.
    """
    lines = [
        "Portfolio currency: USD - --value is interpreted as USD",
        f"Returns window: {window['window_start']} to {window['window_end']} "
        f"({window['window_months']} month(s) of monthly returns)",
        "",
        "Weights:",
    ]
    lines += [f"  {ticker}: {weight:.4f}" for ticker, weight in weights]
    lines += ["", "Share allocation:"]
    lines += [f"  {ticker}: {shares}" for ticker, _ in weights]
    lines.append("")
    if figures:
        lines.append(
            f"Portfolio expected return: {figures[0]:.4f}  "
            f"Portfolio volatility: {figures[1]:.4f}  "
            f"Portfolio Sharpe: {figures[2]:.4f}"
        )
    if benchmark:
        lines.append(
            "Benchmark SPY: return=0.1225  volatility=0.1481  Sharpe=0.5704  "
            f"(60 of {window['window_months']} month(s))"
        )
    lines.append("Risk-free rate used: 0.0380 (remembered for USD)")
    return "\n".join(lines)


def _whatif_body(positions, deltas=None, window=WINDOW_60) -> str:
    lines = ["What if (USD) - not saved:" if deltas else "Your portfolio (USD):"]
    lines += [f"  {ticker}: {shares:,} shares" for ticker, shares in positions]
    # The window line is part of the real body, and it is what tells two
    # otherwise identical measurements of one book apart. Without it the
    # 60-month and 48-month baselines share a digest and the archive - quite
    # correctly - stores only one of them.
    lines.append(
        f"Returns window: {window['window_start']} to {window['window_end']} "
        f"({window['window_months']} month(s) of monthly returns)"
    )
    lines.append("Risk-free rate used: 0.0380 (remembered for USD)")
    if deltas:
        lines.append(
            f"Change from your saved portfolio: return {deltas['return']:+.4f}  "
            f"volatility {deltas['volatility']:+.4f}  Sharpe {deltas['sharpe']:+.4f}"
        )
        lines.append(
            f"Dividend change: yield {deltas['dividend_yield']:+.4f}  "
            f"annual income {'-' if deltas['annual_income'] < 0 else '+'}"
            f"${abs(deltas['annual_income']):,.2f} USD"
        )
    return "\n".join(lines)


def _archive(tmp_path, kind: str, as_of: date = AS_OF) -> ReportArchive:
    return ReportArchive(
        output_dir=str(tmp_path / "output"),
        enabled=True,
        kind=kind,
        as_of=as_of,
        command=f"portfolio-{kind} --currency USD",
    )


def _restamp(path, saved_at: str) -> None:
    """Rewrite one saved report's `saved_at` line.

    `save_report` stamps it from the clock, and several tests here are about
    the order a month's reports are read in. Rewriting the line keeps the real
    writer responsible for the rest of the file.
    """
    lines = path.read_text(encoding="utf-8").splitlines()
    for index, line in enumerate(lines):
        if line.startswith("saved_at:"):
            lines[index] = f"saved_at: {saved_at}"
            break
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _save_portfolio(tmp_path, objective, sharpe, saved_at, window=WINDOW_60,
                    shares=100, **overrides):
    facts = {
        "variant": overrides.pop("variant", "edit"),
        "currency": overrides.pop("currency", "USD"),
        "objective": objective,
        "selection": "user_provided",
        "value": 100000.0,
        "candidates": overrides.pop("candidates", CANDIDATES),
        "benchmark": "SPY",
        "benchmark_return": 0.1225,
        "risk_free_rate": 0.0380,
        "annual_return": overrides.pop("annual_return", 0.1),
        "annual_volatility": overrides.pop("annual_volatility", 0.05),
        "sharpe": sharpe,
        "annual_dividend": overrides.pop("annual_dividend", 4000.0),
        "dividend_yield": overrides.pop("dividend_yield", 0.04),
        **window,
        **overrides,
    }
    weights = WEIGHTS.get(objective, WEIGHTS["MSR"])
    body = _portfolio_body(
        weights,
        window=window,
        figures=(facts["annual_return"], facts["annual_volatility"], sharpe),
        shares=shares,
    )
    saved = save_report(body, _archive(tmp_path, "portfolio"), facts)
    _restamp(saved.path, saved_at)
    return saved


def _save_whatif(tmp_path, positions, sharpe, saved_at, variant="baseline",
                 window=WINDOW_60, deltas=None, **overrides):
    facts = {
        "variant": variant,
        "currency": overrides.pop("currency", "USD"),
        "positions": [f"{ticker}:{shares}" for ticker, shares in positions],
        "total_value": overrides.pop("total_value", 408200.0),
        "priced_as_of": AS_OF,
        "risk_free_rate": 0.0380,
        "annual_return": overrides.pop("annual_return", 0.02),
        "annual_volatility": overrides.pop("annual_volatility", 0.12),
        "sharpe": sharpe,
        "annual_dividend": overrides.pop("annual_dividend", 27834.0),
        "dividend_yield": overrides.pop("dividend_yield", 0.0682),
        **window,
        **overrides,
    }
    saved = save_report(
        _whatif_body(positions, deltas, window), _archive(tmp_path, "whatif"), facts
    )
    _restamp(saved.path, saved_at)
    return saved


def _real_month(tmp_path):
    """The nine real reports of `output/2026-09/`, rebuilt under `tmp_path`."""
    _save_portfolio(
        tmp_path, "MV-benchmark", 1.6347, "2026-09-11T12:42:31Z", variant="initial",
        objective_origin="matching benchmark SPY's 0.1225 return", target_return=0.1225,
        annual_return=0.1225, annual_volatility=0.0517, annual_dividend=4050.81,
        dividend_yield=0.0405,
    )
    _save_portfolio(
        tmp_path, "MSR", 2.0207, "2026-09-11T12:43:56Z",
        annual_return=0.2513, annual_volatility=0.1056, annual_dividend=4753.20,
        dividend_yield=0.0475,
    )
    _save_portfolio(
        tmp_path, "GMV", 0.2394, "2026-09-11T12:44:17Z",
        annual_return=0.0480, annual_volatility=0.0416, annual_dividend=3853.17,
        dividend_yield=0.0385,
    )
    _save_portfolio(
        tmp_path, "MV-0.1000", 1.3289, "2026-09-11T12:44:39Z", target_return=0.1000,
        annual_return=0.1000, annual_volatility=0.0467, annual_dividend=4156.13,
        dividend_yield=0.0416,
    )
    held = [("PFF", 6000), ("PFFA", 6000), ("VZ", 2000)]
    trimmed = [("PFFA", 6000), ("VZ", 2000)]
    with_cspx = [("CSPX.L", 200), ("PFFA", 6000), ("VZ", 2000)]
    _save_whatif(tmp_path, held, -0.1253, "2026-09-11T12:53:22Z",
                 annual_return=0.0229, annual_volatility=0.1207)
    _save_whatif(tmp_path, held, 0.2808, "2026-09-11T12:53:42Z", window=WINDOW_48,
                 annual_return=0.0713, annual_volatility=0.1187)
    _save_whatif(
        tmp_path, trimmed, 0.4740, "2026-09-11T12:54:06Z", variant="what-if",
        window=WINDOW_48, annual_return=0.1073, annual_volatility=0.1461,
        total_value=226039.99, annual_dividend=17976.0, dividend_yield=0.0795,
        deltas={"return": 0.0359, "volatility": 0.0274, "sharpe": 0.1932,
                "dividend_yield": 0.0113, "annual_income": -9858.0},
    )
    _save_whatif(
        tmp_path, trimmed, 0.0813, "2026-09-11T12:54:31Z", variant="what-if",
        annual_return=0.0498, annual_volatility=0.1447, total_value=226039.99,
        annual_dividend=17976.0, dividend_yield=0.0795,
        deltas={"return": 0.0269, "volatility": 0.0240, "sharpe": 0.2066,
                "dividend_yield": 0.0113, "annual_income": -9858.0},
    )
    _save_whatif(
        tmp_path, with_cspx, 0.3670, "2026-09-11T12:57:53Z", variant="what-if",
        annual_return=0.0839, annual_volatility=0.1250, total_value=390887.99,
        annual_dividend=17976.0, dividend_yield=0.0460,
        deltas={"return": 0.0610, "volatility": 0.0043, "sharpe": 0.4923,
                "dividend_yield": -0.0222, "annual_income": -9858.0},
    )
    return load_month(tmp_path / "output", MONTH)


# --------------------------------------------------------------------------
# load_month
# --------------------------------------------------------------------------

def test_load_month_on_a_missing_folder_returns_nothing_rather_than_raising(tmp_path):
    assert load_month(tmp_path / "output", "2026-10") == ([], [])


def test_load_month_on_an_empty_folder_returns_nothing(tmp_path):
    (tmp_path / "output" / MONTH).mkdir(parents=True)
    assert load_month(tmp_path / "output", MONTH) == ([], [])


def test_load_month_skips_a_summary_so_a_summary_never_summarizes_itself(tmp_path):
    _save_portfolio(tmp_path, "MSR", 2.0, "2026-09-11T12:00:00Z")
    save_report(
        "# Portfolio archive summary - 2026-09\n\nsome earlier briefing",
        _archive(tmp_path, "summary"),
        {"month": MONTH, "report_count": 1},
    )

    records, notes = load_month(tmp_path / "output", MONTH)

    assert [r.kind for r in records] == ["portfolio"]
    assert any("kind is summary" in note for note in notes)


def test_load_month_skips_an_unknown_kind_with_a_note(tmp_path):
    save_report("body", _archive(tmp_path, "experiment"), {"currency": "USD"})

    records, notes = load_month(tmp_path / "output", MONTH)

    assert records == []
    assert any("kind is experiment" in note for note in notes)


def test_load_month_skips_a_file_without_front_matter_instead_of_raising(tmp_path):
    folder = tmp_path / "output" / MONTH
    folder.mkdir(parents=True)
    (folder / "notes-to-self.md").write_text("just a note I left here\n", encoding="utf-8")

    records, notes = load_month(tmp_path / "output", MONTH)

    assert records == []
    assert notes == ["notes-to-self.md: skipped, not a report this archive wrote"]


def test_load_month_orders_records_by_when_they_were_saved(tmp_path):
    _save_portfolio(tmp_path, "GMV", 0.2, "2026-09-11T12:44:17Z")
    _save_portfolio(tmp_path, "MSR", 2.0, "2026-09-11T12:43:56Z")
    _save_portfolio(tmp_path, "MV-benchmark", 1.6, "2026-09-11T12:42:31Z")

    records, _ = load_month(tmp_path / "output", MONTH)

    assert [r.objective for r in records] == ["MV-benchmark", "MSR", "GMV"]


def test_load_month_reads_nine_reports_of_the_real_shape(tmp_path):
    records, notes = _real_month(tmp_path)

    assert notes == []
    assert len(records) == 9
    assert sum(1 for r in records if r.kind == "portfolio") == 4
    assert sum(1 for r in records if r.kind == "whatif") == 5


# --------------------------------------------------------------------------
# Body parsers
# --------------------------------------------------------------------------

def test_parse_weights_reads_a_real_weights_block_in_file_order(tmp_path):
    weights = parse_weights(_portfolio_body(WEIGHTS["MSR"]))

    assert weights[0] == ("MLPX", 0.1717)
    assert weights[-1] == ("BOXX", 0.0693)
    assert len(weights) == 8


def test_parse_weights_reads_a_ticker_carrying_a_dot(tmp_path):
    weights = parse_weights(_portfolio_body([("CSPX.L", 0.4217), ("VUAA.L", 0.1)]))

    assert weights == (("CSPX.L", 0.4217), ("VUAA.L", 0.1000))


def test_parse_weights_on_a_whatif_body_is_empty_not_an_error():
    assert parse_weights(_whatif_body([("PFF", 6000)])) == ()


def test_parse_weights_on_a_body_with_no_block_is_empty():
    assert parse_weights("Portfolio currency: USD\nRisk-free rate used: 0.0380") == ()


def test_parse_weights_stops_at_the_next_section_rather_than_reading_into_it():
    body = "Weights:\n  BOXX: 0.2910\nShare allocation:\n  BOXX: 246\n"

    assert parse_weights(body) == (("BOXX", 0.2910),)


def test_parse_benchmark_reads_the_line_and_its_month_coverage():
    line = parse_benchmark(_portfolio_body(WEIGHTS["MSR"]))

    assert line.ticker == "SPY"
    assert (line.annual_return, line.annual_volatility, line.sharpe) == (
        0.1225, 0.1481, 0.5704,
    )
    assert (line.months_covered, line.months_total) == (60, 60)


def test_parse_benchmark_on_a_whatif_body_is_none():
    assert parse_benchmark(_whatif_body([("PFF", 6000)])) is None


def test_parse_benchmark_on_a_portfolio_body_without_the_line_is_none():
    assert parse_benchmark(_portfolio_body(WEIGHTS["MSR"], benchmark=False)) is None


def test_parse_whatif_deltas_reads_both_printed_delta_lines():
    body = _whatif_body(
        [("PFFA", 6000), ("VZ", 2000)],
        {"return": 0.0359, "volatility": 0.0274, "sharpe": 0.1932,
         "dividend_yield": 0.0113, "annual_income": -9858.0},
    )

    assert parse_whatif_deltas(body) == {
        "return": 0.0359,
        "volatility": 0.0274,
        "sharpe": 0.1932,
        "dividend_yield": 0.0113,
        "annual_income": -9858.0,
    }


def test_parse_whatif_deltas_keeps_the_sign_of_a_lost_income():
    body = _whatif_body(
        [("PFFA", 6000)],
        {"return": 0.0610, "volatility": 0.0043, "sharpe": 0.4923,
         "dividend_yield": -0.0222, "annual_income": -9858.0},
    )

    deltas = parse_whatif_deltas(body)

    assert deltas["annual_income"] == -9858.0
    assert deltas["dividend_yield"] == -0.0222


def test_parse_whatif_deltas_on_a_baseline_body_is_empty():
    assert parse_whatif_deltas(_whatif_body([("PFF", 6000)])) == {}


# --------------------------------------------------------------------------
# Records
# --------------------------------------------------------------------------

def test_a_record_coerces_its_facts_and_returns_none_for_an_absent_one(tmp_path):
    _save_portfolio(tmp_path, "MSR", 2.0207, "2026-09-11T12:43:56Z")

    (record,) = load_month(tmp_path / "output", MONTH)[0]

    assert record.sharpe == 2.0207
    assert record.window_months == 60
    assert record.as_of == AS_OF
    assert record.candidates[0] == "AMLP"
    assert len(record.candidates) == 17
    assert record.target_return is None  # MSR has no target, so the fact is absent


def test_a_record_reads_positions_as_ticker_and_share_count(tmp_path):
    _save_whatif(tmp_path, [("PFF", 6000), ("PFFA", 6000), ("VZ", 2000)], -0.1253,
                 "2026-09-11T12:53:22Z")

    (record,) = load_month(tmp_path / "output", MONTH)[0]

    assert record.positions == (("PFF", 6000), ("PFFA", 6000), ("VZ", 2000))


def test_a_record_with_an_unparseable_figure_reports_none_rather_than_raising(tmp_path):
    saved = _save_portfolio(tmp_path, "MSR", 2.0, "2026-09-11T12:43:56Z")
    text = saved.path.read_text(encoding="utf-8").replace("sharpe: 2.0000", "sharpe: n/a")
    saved.path.write_text(text, encoding="utf-8")

    (record,) = load_month(tmp_path / "output", MONTH)[0]

    assert record.sharpe is None
    assert record.annual_return == 0.1


def test_label_names_a_portfolio_by_objective_and_target(tmp_path):
    _save_portfolio(tmp_path, "MV-benchmark", 1.6347, "2026-09-11T12:42:31Z",
                    target_return=0.1225)
    _save_portfolio(tmp_path, "MSR", 2.0207, "2026-09-11T12:43:56Z")

    records, _ = load_month(tmp_path / "output", MONTH)

    assert label(records[0]) == "portfolio MV-benchmark @0.1225"
    assert label(records[1]) == "portfolio MSR"


def test_label_names_a_whatif_by_its_positions_and_marks_the_held_book(tmp_path):
    _save_whatif(tmp_path, [("PFF", 6000), ("PFFA", 6000), ("VZ", 2000)], -0.1253,
                 "2026-09-11T12:53:22Z")
    _save_whatif(tmp_path, [("PFFA", 6000), ("VZ", 2000)], 0.0813,
                 "2026-09-11T12:54:31Z", variant="what-if")

    records, _ = load_month(tmp_path / "output", MONTH)

    assert label(records[0]) == "whatif PFF+PFFA+VZ (held)"
    assert label(records[1]) == "whatif PFFA+VZ"


def test_label_truncates_a_long_position_list(tmp_path):
    positions = [(t, 10) for t in ("AMLP", "BIL", "BOXX", "ENFR", "GOOGL", "MLPX")]
    _save_whatif(tmp_path, positions, 0.5, "2026-09-11T12:53:22Z", variant="what-if")

    (record,) = load_month(tmp_path / "output", MONTH)[0]

    assert label(record) == "whatif AMLP+BIL+BOXX+ENFR+2 more"


# --------------------------------------------------------------------------
# sources_digest
# --------------------------------------------------------------------------

def test_sources_digest_ignores_the_order_the_reports_were_read_in(tmp_path):
    records, _ = _real_month(tmp_path)

    assert sources_digest(records) == sources_digest(list(reversed(records)))


def test_sources_digest_changes_when_a_report_is_added(tmp_path):
    records, _ = _real_month(tmp_path)
    before = sources_digest(records)

    _save_portfolio(tmp_path, "GMV", 0.9999, "2026-09-11T13:00:00Z",
                    annual_return=0.07, annual_volatility=0.03)
    after_records, _ = load_month(tmp_path / "output", MONTH)

    assert len(after_records) == 10
    assert sources_digest(after_records) != before


# --------------------------------------------------------------------------
# The analysis
# --------------------------------------------------------------------------

def test_the_leaderboard_keeps_two_returns_windows_in_separate_tables(tmp_path):
    records, notes = _real_month(tmp_path)

    digest = build_month_digest(records, MONTH, notes)

    assert len(digest.partitions) == 2
    long_window, short_window = digest.partitions
    assert long_window.window_months == 60
    assert len(long_window.rows) == 7
    assert short_window.window_months == 48
    assert len(short_window.rows) == 2


def test_the_leaderboard_ranks_by_sharpe_within_a_window(tmp_path):
    records, notes = _real_month(tmp_path)

    digest = build_month_digest(records, MONTH, notes)
    rows = digest.partitions[0].rows

    assert [row.label for row in rows][:3] == [
        "portfolio MSR", "portfolio MV-benchmark @0.1225", "portfolio MV-0.1000 @0.1000",
    ]
    assert rows[0].sharpe == 2.0207
    assert rows[-1].label == "whatif PFF+PFFA+VZ (held)"
    assert rows[-1].sharpe == -0.1253


def test_a_report_with_an_unreadable_sharpe_ranks_last_not_as_a_zero(tmp_path):
    _save_portfolio(tmp_path, "MSR", 2.0207, "2026-09-11T12:43:56Z")
    saved = _save_portfolio(tmp_path, "GMV", -1.0, "2026-09-11T12:44:17Z")
    text = saved.path.read_text(encoding="utf-8").replace("sharpe: -1.0000", "sharpe: n/a")
    saved.path.write_text(text, encoding="utf-8")

    records, notes = load_month(tmp_path / "output", MONTH)
    rows = build_month_digest(records, MONTH, notes).partitions[0].rows

    assert rows[0].label == "portfolio MSR"
    assert rows[-1].label == "portfolio GMV"
    assert rows[-1].sharpe is None


def test_the_leaderboard_collapses_reports_with_the_same_name_and_figures(tmp_path):
    _save_portfolio(tmp_path, "MSR", 2.0207, "2026-09-11T12:43:56Z", shares=100)
    # The same portfolio re-priced: identical return, volatility, Sharpe and
    # yield, but a different share allocation, so a different body and a
    # different digest. Two saved files, one portfolio.
    _save_portfolio(tmp_path, "MSR", 2.0207, "2026-09-11T12:45:00Z", shares=101)

    records, notes = load_month(tmp_path / "output", MONTH)
    rows = build_month_digest(records, MONTH, notes).partitions[0].rows

    assert len(records) == 2
    assert len(rows) == 1
    assert rows[0].copies == 2


def test_the_four_real_runs_agree_on_seven_tickers_and_reject_five(tmp_path):
    records, notes = _real_month(tmp_path)

    consensus = build_month_digest(records, MONTH, notes).consensus

    assert consensus.reason is None
    assert consensus.always_held == ("AMLP", "BOXX", "ENFR", "GOOGL", "MLPX", "QQQI", "T")
    assert consensus.never_held == ("CSPX.L", "SPY", "TSM", "VUAA.L", "VZ")
    assert len(consensus.runs) == 4


def test_a_token_one_share_weight_is_reported_as_negligible(tmp_path):
    records, notes = _real_month(tmp_path)

    consensus = build_month_digest(records, MONTH, notes).consensus

    assert ("TLT", "portfolio MV-0.1000 @0.1000", 0.0005) in consensus.negligible
    assert 0.0005 < NEGLIGIBLE_WEIGHT


def test_a_ticker_only_one_run_wanted_is_named_with_that_run(tmp_path):
    records, notes = _real_month(tmp_path)

    consensus = build_month_digest(records, MONTH, notes).consensus

    assert ("PFF", "portfolio GMV", 0.0935) in consensus.run_specific


def test_consensus_says_why_when_only_one_run_shares_a_pool(tmp_path):
    _save_portfolio(tmp_path, "MSR", 2.0207, "2026-09-11T12:43:56Z")

    records, notes = load_month(tmp_path / "output", MONTH)
    consensus = build_month_digest(records, MONTH, notes).consensus

    assert consensus.always_held == ()
    assert "one run cannot agree with anything" in consensus.reason


def test_consensus_says_why_when_no_run_recorded_a_pool(tmp_path):
    _save_whatif(tmp_path, [("PFF", 6000)], -0.1253, "2026-09-11T12:53:22Z")

    records, notes = load_month(tmp_path / "output", MONTH)
    consensus = build_month_digest(records, MONTH, notes).consensus

    assert "nothing for two runs to agree about" in consensus.reason


def test_the_held_book_is_compared_with_the_best_run_on_the_same_window(tmp_path):
    records, notes = _real_month(tmp_path)

    book = build_month_digest(records, MONTH, notes).book

    assert book.reason is None
    assert book.book.label == "whatif PFF+PFFA+VZ (held)"
    assert book.book.sharpe == -0.1253
    assert book.best.label == "portfolio MSR"
    assert book.same_window is True
    assert book.sharpe_gap == pytest.approx(2.1460)
    assert book.return_gap == pytest.approx(0.2284)
    assert book.annual_dividend_gap == pytest.approx(4753.20 - 27834.0)


def test_the_book_comparison_says_why_when_no_baseline_was_saved(tmp_path):
    _save_portfolio(tmp_path, "MSR", 2.0207, "2026-09-11T12:43:56Z")

    records, notes = load_month(tmp_path / "output", MONTH)
    book = build_month_digest(records, MONTH, notes).book

    assert book.book is None
    assert "no 'whatif' baseline" in book.reason


def test_the_book_comparison_says_why_when_no_run_shares_its_currency(tmp_path):
    _save_whatif(tmp_path, [("1321.T", 50)], 0.1, "2026-09-11T12:53:22Z", currency="JPY")
    _save_portfolio(tmp_path, "MSR", 2.0207, "2026-09-11T12:43:56Z")

    records, notes = load_month(tmp_path / "output", MONTH)
    book = build_month_digest(records, MONTH, notes).book

    assert "no portfolio report is denominated in JPY" in book.reason


def test_every_whatif_variant_is_paired_with_the_baseline_of_its_own_window(tmp_path):
    records, notes = _real_month(tmp_path)

    ledger = build_month_digest(records, MONTH, notes).ledger

    assert ledger.reason is None
    assert len(ledger.entries) == 3
    by_window = {(e.label, e.partition.endswith("(48 months)")): e for e in ledger.entries}
    short = by_window[("whatif PFFA+VZ", True)]
    long = by_window[("whatif PFFA+VZ", False)]
    # The printed deltas differ because each was measured against the baseline
    # of its own window: +0.0359 against 0.0713, +0.0269 against 0.0229.
    assert short.deltas["return"] == 0.0359
    assert long.deltas["return"] == 0.0269
    assert short.removed == ("PFF",)
    assert long.removed == ("PFF",)


def test_a_variant_records_the_position_it_added(tmp_path):
    records, notes = _real_month(tmp_path)

    ledger = build_month_digest(records, MONTH, notes).ledger
    entry = next(e for e in ledger.entries if "CSPX.L" in e.label)

    assert entry.added == ("CSPX.L",)
    assert entry.removed == ("PFF",)
    assert entry.deltas["annual_income"] == -9858.0


def test_a_variant_with_no_baseline_on_its_window_says_so(tmp_path):
    _save_whatif(
        tmp_path, [("PFFA", 6000)], 0.2, "2026-09-11T12:54:06Z", variant="what-if",
        deltas={"return": 0.01, "volatility": 0.01, "sharpe": 0.01,
                "dividend_yield": 0.01, "annual_income": -100.0},
    )

    records, notes = load_month(tmp_path / "output", MONTH)
    (entry,) = build_month_digest(records, MONTH, notes).ledger.entries

    assert entry.baseline_label is None
    assert "no baseline was saved" in entry.note
    assert entry.added == ()


def test_the_ledger_says_why_when_no_variant_was_tried(tmp_path):
    _save_whatif(tmp_path, [("PFF", 6000)], -0.1253, "2026-09-11T12:53:22Z")

    records, notes = load_month(tmp_path / "output", MONTH)
    ledger = build_month_digest(records, MONTH, notes).ledger

    assert "no 'whatif' variant" in ledger.reason


def test_both_window_sensitivity_pairs_in_the_real_month_are_named(tmp_path):
    records, notes = _real_month(tmp_path)

    sensitivity = build_month_digest(records, MONTH, notes).comparability.sensitivity
    subjects = {item.subject: item for item in sensitivity}

    held = subjects["positions PFF:6000, PFFA:6000, VZ:2000 (USD)"]
    assert [(m[0].endswith("(60 months)"), m[1], m[3]) for m in held.measurements] == [
        (True, 0.0229, -0.1253),
        (False, 0.0713, 0.2808),
    ]
    trimmed = subjects["positions PFFA:6000, VZ:2000 (USD)"]
    assert [(m[1], m[3]) for m in trimmed.measurements] == [(0.0498, 0.0813), (0.1073, 0.4740)]


def test_a_month_measured_one_way_reports_nothing_to_caution_about(tmp_path):
    _save_portfolio(tmp_path, "MSR", 2.0207, "2026-09-11T12:43:56Z")
    _save_portfolio(tmp_path, "GMV", 0.2394, "2026-09-11T12:44:17Z")

    records, notes = load_month(tmp_path / "output", MONTH)
    comparability = build_month_digest(records, MONTH, notes).comparability

    assert comparability.sensitivity == ()
    assert comparability.rate_disagreements == ()
    assert len(comparability.windows) == 1


def test_two_risk_free_rates_on_one_currency_are_reported_as_a_disagreement(tmp_path):
    _save_portfolio(tmp_path, "MSR", 2.0207, "2026-09-11T12:43:56Z")
    _save_portfolio(tmp_path, "GMV", 0.2394, "2026-09-11T12:44:17Z", risk_free_rate=0.0425)

    records, notes = load_month(tmp_path / "output", MONTH)
    comparability = build_month_digest(records, MONTH, notes).comparability

    assert len(comparability.rate_disagreements) == 1
    assert "0.0380, 0.0425" in comparability.rate_disagreements[0]


def test_a_benchmark_covering_less_history_than_the_window_is_reported(tmp_path):
    facts = {
        "variant": "initial", "currency": "USD", "objective": "MSR",
        "selection": "user_provided", "candidates": CANDIDATES, "benchmark": "SPY",
        "risk_free_rate": 0.0380, "annual_return": 0.25, "annual_volatility": 0.10,
        "sharpe": 2.0, "annual_dividend": 4000.0, "dividend_yield": 0.04, **WINDOW_60,
    }
    body = _portfolio_body(WEIGHTS["MSR"]).replace("(60 of 60 month(s))", "(41 of 60 month(s))")
    save_report(body, _archive(tmp_path, "portfolio"), facts)

    records, notes = load_month(tmp_path / "output", MONTH)
    coverage = build_month_digest(records, MONTH, notes).comparability.benchmark_coverage

    assert len(coverage) == 1
    assert "covers 41 of 60 months" in coverage[0]


def test_two_currencies_produce_two_partitions_and_are_never_mixed(tmp_path):
    _save_portfolio(tmp_path, "MSR", 2.0207, "2026-09-11T12:43:56Z")
    _save_portfolio(tmp_path, "GMV", 0.2394, "2026-09-11T12:44:17Z", currency="JPY")

    records, notes = load_month(tmp_path / "output", MONTH)
    digest = build_month_digest(records, MONTH, notes)

    assert len(digest.partitions) == 2
    assert {p.currency for p in digest.partitions} == {"USD", "JPY"}
    assert all(len(p.rows) == 1 for p in digest.partitions)
    assert digest.scope.currencies == ("JPY", "USD")


def test_the_real_month_reconstructs_three_sessions(tmp_path):
    records, notes = _real_month(tmp_path)

    threads = build_month_digest(records, MONTH, notes).threads

    assert [len(thread.steps) for thread in threads] == [4, 3, 2]
    portfolio_thread = threads[0]
    assert portfolio_thread.kind == "portfolio"
    assert portfolio_thread.steps[0].variant == "initial"
    assert portfolio_thread.steps[0].sharpe_change is None
    assert portfolio_thread.steps[1].sharpe_change == pytest.approx(0.3860)
    assert portfolio_thread.steps[1].left == ("BIL",)
    assert portfolio_thread.steps[2].entered == ("BIL", "PFF", "PFFA", "TLT")


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

def test_render_digest_prints_the_real_months_headline_figures(tmp_path):
    records, notes = _real_month(tmp_path)

    text = render_digest(build_month_digest(records, MONTH, notes))

    assert "# Portfolio archive summary - 2026-09" in text
    assert "9 reports (4 portfolio, 5 whatif)." in text
    assert "Window 2021-10-01 to 2026-09-01 (60 months), USD - 7 portfolio(s)" in text
    assert "Window 2022-10-03 to 2026-09-01 (48 months), USD - 2 portfolio(s)" in text
    assert "Held by every run: AMLP, BOXX, ENFR, GOOGL, MLPX, QQQI, T" in text
    assert "Never held by any run: CSPX.L, SPY, TSM, VUAA.L, VZ" in text
    assert "2.0207" in text and "-0.1253" in text


def test_render_digest_never_emits_a_line_that_looks_like_a_front_matter_fence(tmp_path):
    records, notes = _real_month(tmp_path)

    text = render_digest(build_month_digest(records, MONTH, notes))

    assert not any(line.strip() == "---" for line in text.splitlines())


def test_a_rendered_summary_survives_a_round_trip_through_the_archive(tmp_path):
    records, notes = _real_month(tmp_path)
    digest = build_month_digest(records, MONTH, notes)
    body = render_digest(digest)

    saved = save_report(
        body,
        _archive(tmp_path, "summary"),
        {"month": MONTH, "report_count": 9, "sources_digest": digest.sources_digest},
    )
    facts, read_back = load_report(saved.path)

    assert facts["kind"] == "summary"
    assert facts["sources_digest"] == digest.sources_digest
    assert "Held by every run" in read_back


def test_render_digest_on_a_whatif_only_month_explains_each_empty_section(tmp_path):
    _save_whatif(tmp_path, [("PFF", 6000), ("PFFA", 6000)], -0.1253, "2026-09-11T12:53:22Z")

    records, notes = load_month(tmp_path / "output", MONTH)
    text = render_digest(build_month_digest(records, MONTH, notes))

    assert "n/a - no portfolio report in this month records a candidate pool" in text
    assert "n/a - this month holds no portfolio report" in text
    assert "n/a - this month holds no 'whatif' variant" in text


def test_render_digest_on_an_empty_month_says_there_is_nothing_to_summarize():
    text = render_digest(build_month_digest([], MONTH, []))

    assert "No reports were read, so there is nothing to summarize." in text


def test_digest_for_llm_is_compact_and_carries_the_computed_figures(tmp_path):
    records, notes = _real_month(tmp_path)

    facts = digest_for_llm(build_month_digest(records, MONTH, notes))

    assert "MONTH: 2026-09" in facts
    assert "sharpe=2.0207" in facts
    assert "GAP same window: yes" in facts
    assert "WINDOW SENSITIVITY positions PFF:6000, PFFA:6000, VZ:2000 (USD)" in facts
    assert len(facts) < 8000  # small enough for the cheapest model's context


def test_digest_for_llm_says_when_it_truncated_a_partition(tmp_path, monkeypatch):
    monkeypatch.setattr("src.flow.report_summary.LLM_MAX_ROWS_PER_PARTITION", 2)
    records, notes = _real_month(tmp_path)

    facts = digest_for_llm(build_month_digest(records, MONTH, notes))

    assert "truncated: 5 further portfolios in this window are not listed" in facts


def test_a_report_stripped_of_almost_every_fact_still_renders_without_leaking_none(tmp_path):
    """A hand-mangled file must degrade to `n/a` cells, not to the word
    `None` or a traceback. Every accessor returns `None` for an absent fact,
    and every formatter turns that into `n/a`; this test is what keeps the two
    halves of that promise in step."""
    from pathlib import Path

    from src.flow.report_summary import ReportRecord

    bare = ReportRecord(path=Path("mangled.md"), facts={"kind": "portfolio"}, body="")

    digest = build_month_digest([bare], MONTH, [])
    text = render_digest(digest)

    assert "None" not in text
    assert "n/a" in text
    assert digest.partitions[0].window == "window unknown"
    assert "REPORTS: 1 total" in digest_for_llm(digest)


def test_an_empty_month_renders_and_compacts_without_raising():
    digest = build_month_digest([], "2026-10", [])

    assert "nothing to summarize" in render_digest(digest)
    assert "REPORTS: 0 total" in digest_for_llm(digest)
    # Prose offered for a month with no reports places nothing and does not raise.
    assert render_digest(digest, {"headline": "anything", "next_runs": "- x"})


# --------------------------------------------------------------------------
# The source-reports appendix
# --------------------------------------------------------------------------

def test_the_source_list_is_ordered_by_when_each_report_was_saved(tmp_path):
    """Saved order, because that is the order the work happened in.

    The digests are asserted against `saved_at` rather than as literals: these
    are the fixture's own bodies, so its digests are not the real archive's.
    """
    records, notes = _real_month(tmp_path)

    sources = build_month_digest(records, MONTH, notes).sources

    assert len(sources) == 9
    stamps = [source.saved_at for source in sources]
    assert stamps == sorted(stamps)
    assert [s.label for s in sources][:2] == [
        "portfolio MV-benchmark @0.1225", "portfolio MSR",
    ]


def test_each_source_digest_is_the_first_eight_characters_of_its_filename(tmp_path):
    """The claim the whole section rests on: the identifier printed beside a
    portfolio is the token that names its file, so the reader can open it."""
    records, notes = _real_month(tmp_path)

    for source in build_month_digest(records, MONTH, notes).sources:
        assert len(source.digest8) == 8
        assert source.filename.endswith(f"-{source.digest8}.md")
        assert source.digest.startswith(source.digest8)


def test_a_label_shared_by_two_reports_is_qualified_by_its_window(tmp_path):
    """Four of the nine real reports share a label - the held book and one
    variant of it are each measured over two windows - so without the
    qualifier this section could not answer the question it exists for."""
    records, notes = _real_month(tmp_path)

    sources = build_month_digest(records, MONTH, notes).sources
    labels = [source.label for source in sources]

    assert "whatif PFFA+VZ 60mo" in labels
    assert "whatif PFFA+VZ 48mo" in labels
    assert "whatif PFF+PFFA+VZ (held) 60mo" in labels
    assert "whatif PFF+PFFA+VZ (held) 48mo" in labels
    assert len(set(labels)) == len(labels)


def test_an_unshared_label_is_left_exactly_as_the_leaderboard_spells_it(tmp_path):
    records, notes = _real_month(tmp_path)
    digest = build_month_digest(records, MONTH, notes)

    labels = {source.label for source in digest.sources}
    leaderboard = {row.label for partition in digest.partitions for row in partition.rows}

    assert "portfolio MSR" in labels
    assert "portfolio MSR" in leaderboard
    # Every unqualified source label must be findable in the leaderboard, or a
    # reader cannot get from a row to this list at all.
    assert {name for name in labels if not name.endswith("mo")} <= leaderboard


def test_a_report_with_no_saved_at_sorts_last_and_prints_n_a(tmp_path):
    saved = _save_portfolio(tmp_path, "MSR", 2.0207, "2026-09-11T12:43:56Z")
    _save_portfolio(tmp_path, "GMV", 0.2394, "2026-09-11T12:44:17Z")
    text = saved.path.read_text(encoding="utf-8")
    saved.path.write_text(
        "\n".join(l for l in text.splitlines() if not l.startswith("saved_at:")) + "\n",
        encoding="utf-8",
    )

    records, notes = load_month(tmp_path / "output", MONTH)
    digest = build_month_digest(records, MONTH, notes)

    assert digest.sources[-1].saved_at is None
    assert digest.sources[-1].label == "portfolio MSR"
    assert "n/a" in render_digest(digest)


def test_the_appendix_names_the_folder_once_and_then_basenames(tmp_path):
    records, notes = _real_month(tmp_path)
    digest = build_month_digest(records, MONTH, notes)

    text = render_digest(digest)
    body = text[text.index("## Source reports"):]

    assert f"All under {tmp_path / 'output' / MONTH}/ :" in body
    rows = [l for l in body.splitlines() if "-portfolio-" in l or "-whatif-" in l]
    assert len(rows) == 9
    assert all("/" not in row for row in rows)


def test_the_appendix_lists_every_report_and_not_an_earlier_summary(tmp_path):
    records, notes = _real_month(tmp_path)
    save_report(
        "# Portfolio archive summary - 2026-09\n\nan earlier briefing",
        _archive(tmp_path, "summary"),
        {"month": MONTH, "report_count": 9},
    )

    records, notes = load_month(tmp_path / "output", MONTH)
    digest = build_month_digest(records, MONTH, notes)

    assert len(digest.sources) == 9
    assert all("summary" not in source.filename for source in digest.sources)
    assert all(source.kind in ("portfolio", "whatif") for source in digest.sources)


def test_the_trailing_digest_says_it_identifies_the_set_and_not_a_file(tmp_path):
    records, notes = _real_month(tmp_path)
    digest = build_month_digest(records, MONTH, notes)

    text = render_digest(digest)

    assert "Digest of this set of 9 report(s)" in text
    assert "not the digest of any one file above" in text
    assert digest.sources_digest in text


def test_the_llm_facts_sheet_carries_no_report_digests(tmp_path):
    """The model never sees a digest, and that is a safety property rather than
    an omission.

    `verify_narrative` in `src/agents/report_summary.py` rejects prose stating a
    figure absent from this sheet, and it finds figures with a digit-run regex.
    A hex digest is full of digit runs - `02a118b3` alone would make `02`, `118`
    and `3` count as supported - so putting nine of them in the sheet would
    quietly widen the set of numbers the verifier accepts and weaken the one
    mechanism that stops a small model inventing figures.
    """
    records, notes = _real_month(tmp_path)
    digest = build_month_digest(records, MONTH, notes)

    facts = digest_for_llm(digest)

    for source in digest.sources:
        assert source.digest8 not in facts
        assert source.filename not in facts


def test_an_empty_month_renders_no_source_reports_section():
    digest = build_month_digest([], "2026-10", [])

    assert digest.sources == ()
    assert digest.scope.folder is None
    assert "## Source reports" not in render_digest(digest)


def test_two_reports_that_cannot_be_told_apart_by_window_keep_their_digests(tmp_path):
    """The documented end of the qualifying rule.

    When two reports share a label and neither records a returns window, there
    is nothing left to qualify with, so no qualifier is invented - the digest
    and saved-time columns are what tell them apart. Asserted so the fallback
    stays a decision rather than becoming a surprise.
    """
    from pathlib import Path

    from src.flow.report_summary import ReportRecord, build_sources

    shared = {"kind": "whatif", "variant": "what-if", "positions": "PFFA:1"}
    first = ReportRecord(
        path=Path("a.md"), facts={**shared, "saved_at": "2026-09-11T12:00:00Z"}, body="x"
    )
    second = ReportRecord(
        path=Path("b.md"), facts={**shared, "saved_at": "2026-09-11T12:01:00Z"}, body="y"
    )

    sources = build_sources([first, second])

    assert [s.label for s in sources] == ["whatif PFFA", "whatif PFFA"]
    assert sources[0].digest8 != sources[1].digest8
    assert sources[0].saved_at < sources[1].saved_at
