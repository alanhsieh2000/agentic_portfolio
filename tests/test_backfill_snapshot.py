"""Tests for src/dataset/backfill_snapshot.py.

Per AGENTS.md's testing guidance, every network-touching call (Wikipedia,
SEC EDGAR, yfinance) is monkeypatched out; `build_snapshot_for_date` is
exercised against small, hand-built fixtures so its wiring of
membership/fundamentals/momentum's already-tested pure functions can be
verified without any live network call, per plans/08_consistency_review.md
finding 4.
"""

import math
from datetime import date

import duckdb
import pandas as pd
import pytest

from src.dataset import backfill_snapshot


def _make_prices_db(db_path: str, rows: list[tuple[str, str, float, float]]) -> None:
    """rows: list of (date, ticker, close, adj_close) tuples."""
    con = duckdb.connect(db_path)
    try:
        con.execute("CREATE TABLE prices (date DATE, ticker VARCHAR, close DOUBLE, adj_close DOUBLE)")
        if rows:
            con.executemany("INSERT INTO prices VALUES (?, ?, ?, ?)", rows)
    finally:
        con.close()


def _make_factors_db(db_path: str, rows: list[tuple[str, str, float, float, float, float, float, float]]) -> None:
    con = duckdb.connect(db_path)
    try:
        con.execute(
            "CREATE TABLE factors (rebalance_date DATE, ticker VARCHAR, mve DOUBLE, bm DOUBLE, "
            "mom12m DOUBLE, mve_z DOUBLE, bm_z DOUBLE, mom12m_z DOUBLE)"
        )
        if rows:
            con.executemany("INSERT INTO factors VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
    finally:
        con.close()


def test_build_snapshot_for_date_computes_expected_mve_bm_mom12m(tmp_path, monkeypatch):
    db_path = str(tmp_path / "portfolio.duckdb")
    _make_prices_db(
        db_path,
        [
            ("2018-12-01", "AAA", 50.0, 50.0),
            ("2019-11-01", "AAA", 55.0, 55.0),
            ("2019-12-01", "AAA", 60.0, 60.0),
            ("2018-12-01", "BBB", 100.0, 100.0),
            ("2019-11-01", "BBB", 80.0, 80.0),
            ("2019-12-01", "BBB", 90.0, 90.0),
        ],
    )

    current = pd.DataFrame({"ticker": ["AAA", "BBB"], "security": ["Company AAA", "Company BBB"]})
    changes = pd.DataFrame(
        columns=["date", "added_ticker", "added_security", "removed_ticker", "removed_security"]
    )
    monkeypatch.setattr(backfill_snapshot, "fetch_and_normalize_membership", lambda: (current, changes))
    monkeypatch.setattr(backfill_snapshot.sec_edgar, "fetch_cik_map", lambda: {})
    monkeypatch.setattr(
        backfill_snapshot.sec_edgar,
        "fetch_all_sec_book_equity",
        lambda tickers, cik_map, as_of: ({}, {t: "no CIK found" for t in tickers}),
    )

    shares_by_ticker = {
        "AAA": pd.Series([1_000_000.0], index=pd.DatetimeIndex(["2015-01-01"])),
        "BBB": pd.Series([2_000_000.0], index=pd.DatetimeIndex(["2015-01-01"])),
    }
    splits_by_ticker = {"AAA": pd.Series(dtype="float64"), "BBB": pd.Series(dtype="float64")}
    quarterly_bs_by_ticker = {"AAA": pd.DataFrame(), "BBB": pd.DataFrame()}
    annual_bs_by_ticker = {
        "AAA": pd.DataFrame([[30_000_000.0]], index=["Stockholders Equity"], columns=[pd.Timestamp("2019-06-30")]),
        "BBB": pd.DataFrame([[54_000_000.0]], index=["Stockholders Equity"], columns=[pd.Timestamp("2019-06-30")]),
    }
    monkeypatch.setattr(
        backfill_snapshot,
        "fetch_all_ticker_fundamentals",
        lambda tickers: (shares_by_ticker, splits_by_ticker, quarterly_bs_by_ticker, annual_bs_by_ticker),
    )

    result = backfill_snapshot.build_snapshot_for_date(date(2019, 12, 31), db_path=db_path)

    assert list(result.columns) == ["rebalance_date", "ticker", "mve", "bm", "mom12m", "mve_z", "bm_z", "mom12m_z"]
    assert set(result["ticker"]) == {"AAA", "BBB"}
    assert (result["rebalance_date"] == pd.Timestamp("2019-12-31")).all()

    aaa = result[result["ticker"] == "AAA"].iloc[0]
    bbb = result[result["ticker"] == "BBB"].iloc[0]

    assert aaa["mve"] == pytest.approx(math.log(60.0 * 1_000_000.0))
    assert bbb["mve"] == pytest.approx(math.log(90.0 * 2_000_000.0))
    assert aaa["bm"] == pytest.approx(30_000_000.0 / (60.0 * 1_000_000.0))
    assert bbb["bm"] == pytest.approx(54_000_000.0 / (90.0 * 2_000_000.0))
    assert aaa["mom12m"] == pytest.approx((55.0 / 50.0) - 1.0)
    assert bbb["mom12m"] == pytest.approx((80.0 / 100.0) - 1.0)

    # Cross-sectional z-scores computed within this single date's 2-row
    # group: mean 0 across the group for each standardized column.
    assert (result["mve_z"] + result["mve_z"].iloc[::-1].to_numpy()).sum() == pytest.approx(0.0, abs=1e-9)
    assert result["mve_z"].notna().all()
    assert result["bm_z"].notna().all()
    assert result["mom12m_z"].notna().all()


def test_insert_snapshot_rows_adds_new_date_without_touching_existing_rows(tmp_path):
    db_path = str(tmp_path / "portfolio.duckdb")
    _make_factors_db(
        db_path,
        [
            ("2020-01-01", "AAA", 1.0, 0.5, 0.1, 0.0, 0.0, 0.0),
            ("2020-02-03", "AAA", 1.1, 0.5, 0.1, 0.0, 0.0, 0.0),
        ],
    )
    snapshot = pd.DataFrame(
        {
            "rebalance_date": [pd.Timestamp("2019-12-31"), pd.Timestamp("2019-12-31")],
            "ticker": ["AAA", "BBB"],
            "mve": [2.0, 3.0],
            "bm": [0.4, 0.6],
            "mom12m": [0.05, -0.05],
            "mve_z": [-0.7, 0.7],
            "bm_z": [-0.7, 0.7],
            "mom12m_z": [0.7, -0.7],
        }
    )

    backfill_snapshot.insert_snapshot_rows(snapshot, db_path=db_path)

    con = duckdb.connect(db_path, read_only=True)
    try:
        rows = con.execute("SELECT rebalance_date, ticker FROM factors ORDER BY rebalance_date, ticker").fetchall()
    finally:
        con.close()
    assert rows == [
        (date(2019, 12, 31), "AAA"),
        (date(2019, 12, 31), "BBB"),
        (date(2020, 1, 1), "AAA"),
        (date(2020, 2, 3), "AAA"),
    ]


def test_insert_snapshot_rows_is_idempotent_on_rerun(tmp_path):
    db_path = str(tmp_path / "portfolio.duckdb")
    _make_factors_db(db_path, [("2020-01-01", "AAA", 1.0, 0.5, 0.1, 0.0, 0.0, 0.0)])
    snapshot = pd.DataFrame(
        {
            "rebalance_date": [pd.Timestamp("2019-12-31")],
            "ticker": ["AAA"],
            "mve": [2.0],
            "bm": [0.4],
            "mom12m": [0.05],
            "mve_z": [0.0],
            "bm_z": [0.0],
            "mom12m_z": [0.0],
        }
    )

    backfill_snapshot.insert_snapshot_rows(snapshot, db_path=db_path)
    backfill_snapshot.insert_snapshot_rows(snapshot, db_path=db_path)

    con = duckdb.connect(db_path, read_only=True)
    try:
        count = con.execute(
            "SELECT COUNT(*) FROM factors WHERE rebalance_date = ?", [date(2019, 12, 31)]
        ).fetchone()[0]
    finally:
        con.close()
    assert count == 1
