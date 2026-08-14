"""Tests for src/agents/external_screen.py and
src/dataset/fundamentals.py's get_factor_reference_stats.

Per AGENTS.md, no test here makes a network call: compute_raw_factors_for_ticker
and compute_raw_factors_for_etf (which call yfinance) are exercised manually,
per plans/07_external_candidate_screening.md's Concrete Steps, not here.
Everything under test here is pure Python/DataFrame/SQL logic against a
small hand-built fixture DuckDB file.
"""

from datetime import date

import duckdb
import pytest

from src.agents.external_screen import (
    aggregate_price_to_book,
    screen_external_candidate,
    standardize_raw_factors,
)
from src.agents.llm_s_schema import ScreeningRule
from src.dataset.fundamentals import get_factor_reference_stats


def _rule(buy_condition: str, sell_condition: str) -> ScreeningRule:
    return ScreeningRule(year=2024, buy_condition=buy_condition, sell_condition=sell_condition, rationale="test")


def _fixture_factors_db(tmp_path) -> str:
    """One rebalance date, 3 tickers, hand-picked raw values whose mean/std
    are easy to verify by hand: mve = [1, 2, 3] -> mean 2.0, std 1.0;
    bm = [0.1, 0.3, 0.5] -> mean 0.3, std 0.2; mom12m = [-0.2, 0.0, 0.2] ->
    mean 0.0, std 0.2 (all sample std, ddof=1, matching pandas' default and
    DuckDB's STDDEV_SAMP).
    """
    db_path = str(tmp_path / "fixture.duckdb")
    con = duckdb.connect(db_path)
    con.execute(
        "CREATE TABLE factors (rebalance_date DATE, ticker VARCHAR, "
        "mve DOUBLE, bm DOUBLE, mom12m DOUBLE, mve_z DOUBLE, bm_z DOUBLE, mom12m_z DOUBLE)"
    )
    con.execute(
        "INSERT INTO factors VALUES "
        "('2024-03-01', 'AAA', 1.0, 0.1, -0.2, NULL, NULL, NULL), "
        "('2024-03-01', 'BBB', 2.0, 0.3, 0.0, NULL, NULL, NULL), "
        "('2024-03-01', 'CCC', 3.0, 0.5, 0.2, NULL, NULL, NULL)"
    )
    con.close()
    return db_path


def test_get_factor_reference_stats_matches_hand_computed_mean_std(tmp_path):
    db_path = _fixture_factors_db(tmp_path)
    stats = get_factor_reference_stats(date(2024, 3, 1), db_path=db_path)

    assert stats["mve"] == pytest.approx((2.0, 1.0))
    assert stats["bm"] == pytest.approx((0.3, 0.2))
    assert stats["mom12m"] == pytest.approx((0.0, 0.2))


def test_get_factor_reference_stats_raises_for_unknown_date(tmp_path):
    db_path = _fixture_factors_db(tmp_path)
    with pytest.raises(ValueError, match="2024-04-01"):
        get_factor_reference_stats(date(2024, 4, 1), db_path=db_path)


def test_standardize_raw_factors_matches_manual_zscore_arithmetic():
    stats = {"mve": (2.0, 1.0), "bm": (0.3, 0.2), "mom12m": (0.0, 0.2)}
    raw = {"mve": 3.0, "bm": 0.5, "mom12m": 0.4}

    result = standardize_raw_factors(raw, stats)

    assert result == pytest.approx({"mve": 1.0, "bm": 1.0, "mom12m": 2.0})


def test_standardize_raw_factors_omits_missing_value():
    stats = {"mve": (2.0, 1.0), "bm": (0.3, 0.2), "mom12m": (0.0, 0.2)}
    raw = {"mve": 3.0, "bm": None, "mom12m": 0.4}

    result = standardize_raw_factors(raw, stats)

    assert "bm" not in result
    assert result == pytest.approx({"mve": 1.0, "mom12m": 2.0})


def test_standardize_raw_factors_omits_zero_std_factor():
    stats = {"mve": (2.0, 0.0), "bm": (0.3, 0.2), "mom12m": (0.0, 0.2)}
    raw = {"mve": 3.0, "bm": 0.5, "mom12m": 0.4}

    result = standardize_raw_factors(raw, stats)

    assert "mve" not in result
    assert result == pytest.approx({"bm": 1.0, "mom12m": 2.0})


def test_screen_external_candidate_returns_correct_signal(tmp_path):
    db_path = _fixture_factors_db(tmp_path)
    rule = _rule(buy_condition="mve > 0.5", sell_condition="mom12m < -0.5")

    # Raw mve=3.0 standardizes to (3.0 - 2.0) / 1.0 = 1.0 -> matches buy_condition.
    signal = screen_external_candidate(
        rule, {"mve": 3.0, "bm": 0.3, "mom12m": 0.0}, date(2024, 3, 1), db_path=db_path
    )

    assert signal == "buy"


def test_aggregate_price_to_book_matches_hand_computed_harmonic_mean():
    # weight_sum = 1.0; inv_sum = 0.5/2.0 + 0.5/4.0 = 0.375; result = 1.0 / 0.375 = 8/3.
    result = aggregate_price_to_book([(0.5, 2.0), (0.5, 4.0)])

    assert result == pytest.approx(8 / 3)


def test_aggregate_price_to_book_skips_zero_pb_pair():
    # The (0.5, 0) pair is skipped entirely (undefined ratio), leaving only
    # (0.5, 3.0): weight_sum = 0.5, inv_sum = 0.5 / 3.0, result = 3.0.
    result = aggregate_price_to_book([(0.5, 0.0), (0.5, 3.0)])

    assert result == pytest.approx(3.0)


def test_aggregate_price_to_book_returns_none_when_negative_book_dominates():
    # A dominant negative-pb holding (e.g. a real AbbVie-like case) can push
    # the implied aggregate book value (inv_sum) non-positive: here
    # 0.9 / -10 + 0.1 / 2.0 = -0.04. There is no economically meaningful
    # ratio to report in that case, so this must return None, not a
    # sign-flipped or otherwise misleading number.
    result = aggregate_price_to_book([(0.9, -10.0), (0.1, 2.0)])

    assert result is None


def test_aggregate_price_to_book_returns_none_for_empty_input():
    assert aggregate_price_to_book([]) is None


def test_screen_external_candidate_degrades_gracefully_for_missing_factor(tmp_path):
    """A candidate missing a factor a rule's condition needs (e.g. an ETF
    with no bm proxy, like PFFA) no longer gets blanket-rejected as
    "insufficient_data" — the missing-factor clause becomes indeterminate,
    and with nothing else here to decide buy_condition (its only clause
    needs bm) or sell_condition (mom12m=0.0 doesn't satisfy < -0.5), this
    resolves to a real "hold", per plans/07_external_candidate_screening.md's
    Decision Log.
    """
    db_path = _fixture_factors_db(tmp_path)
    rule = _rule(buy_condition="bm > 0.5", sell_condition="mom12m < -0.5")

    signal = screen_external_candidate(
        rule, {"mve": 3.0, "bm": None, "mom12m": 0.0}, date(2024, 3, 1), db_path=db_path
    )

    assert signal == "hold"


def test_screen_external_candidate_missing_factor_does_not_block_other_clause(tmp_path):
    """A PFFA-shaped candidate (bm missing) whose buy_condition has a
    bm-free clause that genuinely fires now gets a real "buy" instead of
    being screened out just because the rule also mentions bm elsewhere.
    """
    db_path = _fixture_factors_db(tmp_path)
    rule = _rule(buy_condition="mve > 0.5 or bm > 0.9", sell_condition="mom12m < -0.5")

    # Raw mve=3.0 standardizes to (3.0 - 2.0) / 1.0 = 1.0 -> satisfies "mve > 0.5"
    # on its own, without ever needing the missing bm.
    signal = screen_external_candidate(
        rule, {"mve": 3.0, "bm": None, "mom12m": 0.0}, date(2024, 3, 1), db_path=db_path
    )

    assert signal == "buy"
