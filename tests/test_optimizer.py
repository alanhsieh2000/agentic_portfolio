"""Tests for src/optimizer/portfolio.py.

Per AGENTS.md's testing guidance, the returns-matrix drop-logic is tested
against hand-built fixture DataFrames standing in for the `returns` table,
isolated from the actual DuckDB read (per
plans/05_optimizer_and_allocation.md's Validation and Acceptance section).
`load_latest_prices`'s DB-reading half is exercised against a tiny,
hand-built fixture DuckDB file (via `tmp_path`), not the real
`data/portfolio.duckdb`, so this test module stays hermetic and
deterministic regardless of whether the real dataset has been built.
"""

from datetime import date

import duckdb
import pandas as pd
import pytest

from src.optimizer.portfolio import (
    _covariance_input,
    allocate_shares,
    apply_min_history_rule,
    compute_weights,
    load_latest_prices,
    pivot_returns_matrix,
)


def _make_prices_db(db_path: str, rows: list[tuple[str, str, float, float]]) -> None:
    """rows: list of (date, ticker, close, adj_close) tuples."""
    con = duckdb.connect(db_path)
    try:
        con.execute("CREATE TABLE prices (date DATE, ticker VARCHAR, close DOUBLE, adj_close DOUBLE)")
        if rows:
            con.executemany("INSERT INTO prices VALUES (?, ?, ?, ?)", rows)
    finally:
        con.close()


def _dates(n: int) -> list[pd.Timestamp]:
    return list(pd.date_range("2019-04-01", periods=n, freq="MS"))


def _three_ticker_fixture() -> pd.DataFrame:
    """24 months, hand-built so the covariance structure is checkable by
    hand: STABLE has near-zero variance and near-zero covariance with the
    other two; RISKY_A and RISKY_B have substantial variance and, because
    they oscillate on different periods (2 months vs. 3 months over a
    24-month span - a common multiple, so the pattern is exact and
    deterministic), a small but genuinely non-zero covariance with each
    other rather than the (near-)perfect correlation an earlier draft of
    this fixture accidentally produced (verified empirically: a perfectly
    negatively-correlated RISKY_A/RISKY_B pair lets the optimizer cancel
    almost all variance with any 50/50 split of the two, making GMV's
    result degenerate/flat across many weight combinations rather than
    favoring STABLE - not what this fixture is meant to test).
    """
    idx = pd.date_range("2020-01-01", periods=24, freq="MS")
    stable = [0.005 + (0.0002 if i % 2 == 0 else -0.0002) for i in range(24)]
    risky_a = [0.01 + (0.05 if i % 2 == 0 else -0.05) for i in range(24)]
    risky_b = [0.01 + (0.04 if i % 3 == 0 else -0.02) for i in range(24)]
    return pd.DataFrame({"STABLE": stable, "RISKY_A": risky_a, "RISKY_B": risky_b}, index=idx)


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


def test_covariance_input_unchanged_when_every_column_is_complete():
    df = _three_ticker_fixture()

    result = _covariance_input(df)

    pd.testing.assert_frame_equal(result, df)


def test_covariance_input_drops_incomplete_rows_for_partial_history_ticker():
    df = _three_ticker_fixture()
    partial = df.copy()
    partial.loc[partial.index[:10], "RISKY_B"] = None  # simulated recent IPO

    result = _covariance_input(partial)

    assert len(result) == len(df) - 10
    assert result.isna().sum().sum() == 0


def test_compute_weights_gmv_favors_near_zero_variance_ticker():
    df = _three_ticker_fixture()

    weights = compute_weights(df, "GMV")

    assert weights["STABLE"] > 0.8
    assert sum(weights.values()) == pytest.approx(1.0, abs=1e-3)


def test_compute_weights_invalid_objective_raises_value_error():
    df = _three_ticker_fixture()

    with pytest.raises(ValueError):
        compute_weights(df, "BOGUS")


def test_compute_weights_mv_below_gmv_return_matches_gmv_since_constraint_is_non_binding():
    """`efficient_return`'s constraint is `return >= target`, an inequality:
    when GMV's own unconstrained return already clears a low target, the
    constraint doesn't bind and MV legitimately returns the same weights
    as GMV - this is real, verified PyPortfolioOpt behavior (see
    plans/05_optimizer_and_allocation.md's Decision Log), not a bug.
    """
    df = _three_ticker_fixture()

    gmv_weights = compute_weights(df, "GMV")
    mv_weights = compute_weights(df, "MV", target_annual_return=0.03)

    assert mv_weights == pytest.approx(gmv_weights, abs=1e-6)


def test_compute_weights_mv_unreachable_target_raises_value_error():
    df = _three_ticker_fixture()

    with pytest.raises(ValueError):
        compute_weights(df, "MV", target_annual_return=1.0)


def test_load_latest_prices_returns_nearest_on_or_before(tmp_path):
    db_path = str(tmp_path / "prices.duckdb")
    _make_prices_db(
        db_path,
        [
            ("2024-01-01", "AAPL", 100.0, 100.0),
            ("2024-01-15", "AAPL", 110.0, 110.0),
            ("2024-02-01", "AAPL", 120.0, 120.0),
        ],
    )

    result = load_latest_prices(["AAPL"], date(2024, 1, 20), db_path=db_path)

    assert result["AAPL"] == pytest.approx(110.0)


def test_load_latest_prices_is_nan_for_ticker_with_no_price_on_or_before_as_of(tmp_path):
    db_path = str(tmp_path / "prices.duckdb")
    _make_prices_db(db_path, [("2024-02-01", "AAPL", 120.0, 120.0)])

    result = load_latest_prices(["AAPL"], date(2024, 1, 1), db_path=db_path)

    assert pd.isna(result["AAPL"])


def test_load_latest_prices_uses_adj_close_not_close(tmp_path):
    db_path = str(tmp_path / "prices.duckdb")
    _make_prices_db(db_path, [("2024-01-01", "AAPL", 100.0, 95.0)])  # split-adjusted, close != adj_close

    result = load_latest_prices(["AAPL"], date(2024, 1, 1), db_path=db_path)

    assert result["AAPL"] == pytest.approx(95.0)


def test_load_latest_prices_empty_tickers_returns_empty_series(tmp_path):
    db_path = str(tmp_path / "prices.duckdb")
    _make_prices_db(db_path, [])

    result = load_latest_prices([], date(2024, 1, 1), db_path=db_path)

    assert result.empty


def test_allocate_shares_matches_target_weights_within_one_share():
    """Per plans/05_optimizer_and_allocation.md's Validation and Acceptance
    section: one $100 stock, one $50 stock, 50/50 weights, $1000 total.
    """
    weights = {"A": 0.5, "B": 0.5}
    latest_prices = pd.Series({"A": 100.0, "B": 50.0})

    allocation, leftover_cash = allocate_shares(weights, latest_prices, 1000.0)

    implied_value = sum(shares * latest_prices[ticker] for ticker, shares in allocation.items())
    for ticker, weight in weights.items():
        target_value = weight * 1000.0
        actual_value = allocation.get(ticker, 0) * latest_prices[ticker]
        assert abs(actual_value - target_value) <= latest_prices[ticker]
    assert implied_value + leftover_cash == pytest.approx(1000.0)


def test_allocate_shares_exact_fit_leaves_zero_leftover_cash():
    allocation, leftover_cash = allocate_shares({"A": 0.5, "B": 0.5}, pd.Series({"A": 100.0, "B": 50.0}), 1000.0)

    assert allocation == {"A": 5, "B": 10}
    assert leftover_cash == pytest.approx(0.0)


def test_allocate_shares_raises_on_nan_price():
    with pytest.raises((TypeError, ValueError)):
        allocate_shares({"A": 1.0}, pd.Series({"A": float("nan")}), 1000.0)
