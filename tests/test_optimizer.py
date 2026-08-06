"""Tests for src/optimizer/portfolio.py.

Per AGENTS.md's testing guidance, the returns-matrix drop-logic is tested
against hand-built fixture DataFrames standing in for the `returns` table,
isolated from the actual DuckDB read (per
plans/05_optimizer_and_allocation.md's Validation and Acceptance section).
"""

import pandas as pd

from src.optimizer.portfolio import apply_min_history_rule, pivot_returns_matrix


def _dates(n: int) -> list[pd.Timestamp]:
    return list(pd.date_range("2019-04-01", periods=n, freq="MS"))


def test_pivot_returns_matrix_reindexes_missing_ticker_to_all_null_column():
    window = _dates(3)
    long_df = pd.DataFrame(
        {
            "rebalance_date": [window[0], window[1], window[2]],
            "ticker": ["AAPL", "AAPL", "AAPL"],
            "monthly_return": [0.01, 0.02, 0.03],
        }
    )

    wide = pivot_returns_matrix(long_df, ["AAPL", "MISSING"], window)

    assert list(wide.columns) == ["AAPL", "MISSING"]
    assert wide["MISSING"].isna().all()
    assert wide["AAPL"].tolist() == [0.01, 0.02, 0.03]


def test_apply_min_history_rule_drops_ticker_below_min_months():
    window = _dates(60)
    wide = pd.DataFrame(index=pd.DatetimeIndex(window, name="rebalance_date"))
    wide["ENOUGH"] = [0.01] * 60
    wide["TOO_SHORT"] = [None] * 50 + [0.01] * 10  # 10 non-null, below min_months=24

    result = apply_min_history_rule(wide, min_months=24)

    assert list(result.columns) == ["ENOUGH"]


def test_apply_min_history_rule_keeps_ticker_with_recent_ipo_leading_nulls():
    window = _dates(60)
    wide = pd.DataFrame(index=pd.DatetimeIndex(window, name="rebalance_date"))
    wide["RECENT_IPO"] = [None] * 30 + [0.01] * 30  # 30 non-null, between 24 and 60

    result = apply_min_history_rule(wide, min_months=24)

    assert list(result.columns) == ["RECENT_IPO"]
    assert result["RECENT_IPO"].notna().sum() == 30


def test_apply_min_history_rule_drops_ticker_with_internal_gap():
    window = _dates(30)
    wide = pd.DataFrame(index=pd.DatetimeIndex(window, name="rebalance_date"))
    values = [0.01] * 30
    values[15] = None  # a single null sandwiched between non-null months
    wide["GAPPY"] = values

    result = apply_min_history_rule(wide, min_months=24)

    assert "GAPPY" not in result.columns


def test_apply_min_history_rule_keeps_ticker_delisted_before_as_of():
    window = _dates(30)
    wide = pd.DataFrame(index=pd.DatetimeIndex(window, name="rebalance_date"))
    wide["DELISTED"] = [0.01] * 25 + [None] * 5  # trailing nulls, not a gap

    result = apply_min_history_rule(wide, min_months=24)

    assert list(result.columns) == ["DELISTED"]
    assert result["DELISTED"].notna().sum() == 25
