"""Tests for src/dataset/membership.py.

Per AGENTS.md, no test here calls yfinance or fetches the live Wikipedia
page; all inputs are hand-written in-memory fixtures.
"""

import pandas as pd
import pytest

from src.dataset.membership import (
    MembershipTableNotFoundError,
    _locate_table,
    apply_changes_asof,
    compute_rebalance_dates,
)


def _current_fixture() -> pd.DataFrame:
    """Today's membership, i.e. already reflecting every row in
    _changes_fixture(): AAA was removed and DDD/EEE were added historically,
    so today's list is BBB, CCC, DDD, EEE — AAA is absent today precisely
    because _changes_fixture() removed it on 2022-06-01.
    """
    return pd.DataFrame(
        {
            "ticker": ["BBB", "CCC", "DDD", "EEE"],
            "security": ["Beta Inc", "Gamma LLC", "Delta Co", "Epsilon Ltd"],
        }
    )


def _changes_fixture() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": pd.to_datetime(["2022-06-01", "2022-06-01", "2021-03-01"]),
            "added_ticker": ["DDD", None, "EEE"],
            "added_security": ["Delta Co", None, "Epsilon Ltd"],
            "removed_ticker": [None, "AAA", None],
            "removed_security": [None, "Alpha Corp", None],
        }
    )


def test_apply_changes_asof_reconstructs_expected_membership():
    """Before any change row's date, membership should equal the untouched
    current set: DDD/EEE haven't been added yet and AAA hasn't been removed.
    """
    result = apply_changes_asof(_current_fixture(), _changes_fixture(), as_of="2021-01-01")

    assert set(result["ticker"]) == {"AAA", "BBB", "CCC"}
    assert dict(zip(result["ticker"], result["security"]))["AAA"] == "Alpha Corp"


def test_apply_changes_asof_after_all_changes_matches_current():
    """After every change row's date, membership should equal today's
    (unmodified) current set, since there is nothing left to undo.
    """
    result = apply_changes_asof(_current_fixture(), _changes_fixture(), as_of="2023-01-01")

    assert set(result["ticker"]) == {"BBB", "CCC", "DDD", "EEE"}


def test_apply_changes_asof_tsla_boundary_matches_known_history():
    """Regression test for the historical fact named in plans/01_dataset.md
    Concrete Steps Step 2: Tesla was added to the S&P 500 on 2020-12-21.

    This fixture mimics the real Wikipedia change row for that event
    (confirmed against the live page during planning: Effective Date
    "December 21, 2020", Added Ticker TSLA, Removed Ticker AIV) without
    making a network call, so it stays deterministic while still covering
    the exact fact the ExecPlan calls out.
    """
    current = pd.DataFrame(
        {
            "ticker": ["TSLA", "AAPL"],
            "security": ["Tesla", "Apple Inc."],
        }
    )
    changes = pd.DataFrame(
        {
            "date": pd.to_datetime(["2020-12-21"]),
            "added_ticker": ["TSLA"],
            "added_security": ["Tesla"],
            "removed_ticker": ["AIV"],
            "removed_security": ["Apartment Investment & Management"],
        }
    )

    before = apply_changes_asof(current, changes, as_of="2020-12-01")
    after = apply_changes_asof(current, changes, as_of="2021-01-01")

    assert "TSLA" not in set(before["ticker"])
    assert "TSLA" in set(after["ticker"])
    assert "AIV" in set(before["ticker"])
    assert "AIV" not in set(after["ticker"])


def test_locate_table_raises_when_no_table_matches():
    """If Wikipedia's page structure changes so no table has the expected
    columns, table location must fail loudly, not return an empty/wrong table.
    """
    unrelated = pd.DataFrame({"foo": [1], "bar": [2]})
    with pytest.raises(MembershipTableNotFoundError):
        _locate_table([unrelated], required=["symbol", "date added"], table_name="current constituents")


def test_compute_rebalance_dates_returns_52_month_start_dates():
    dates = compute_rebalance_dates("2020-01-01", "2024-04-30")

    assert len(dates) == 52
    assert dates[0] == pd.Timestamp("2020-01-01")
    assert dates[-1] == pd.Timestamp("2024-04-01")
