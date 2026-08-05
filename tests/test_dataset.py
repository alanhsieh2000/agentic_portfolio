"""Tests for src/dataset/membership.py and src/dataset/prices.py.

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
from src.dataset.prices import (
    detect_unresolved_tickers,
    reshape_prices_long,
    to_yfinance_symbol,
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


def test_to_yfinance_symbol_translates_dot_to_dash_for_share_classes():
    assert to_yfinance_symbol("BRK.B") == "BRK-B"
    assert to_yfinance_symbol("BF.B") == "BF-B"
    assert to_yfinance_symbol("AAPL") == "AAPL"


def _raw_prices_fixture() -> pd.DataFrame:
    """Shaped like yfinance's real multi-ticker download() output with
    auto_adjust=False: 2-level MultiIndex columns (field, symbol), field in
    {'Close', 'Adj Close'}. GHOST is an unresolved ticker: yfinance returns
    it as an all-NaN column (matching its empty_df() reindexed across the
    batch's date range), not omitted from the result entirely.
    """
    dates = pd.to_datetime(["2020-01-02", "2020-01-03", "2020-01-06"])
    columns = pd.MultiIndex.from_tuples(
        [
            ("Close", "AAPL"), ("Close", "BRK-B"), ("Close", "GHOST"),
            ("Adj Close", "AAPL"), ("Adj Close", "BRK-B"), ("Adj Close", "GHOST"),
        ],
        names=["Price", "Ticker"],
    )
    data = [
        [100.0, 200.0, float("nan"), 99.0, 199.0, float("nan")],
        [101.0, 201.0, float("nan"), 100.0, 200.0, float("nan")],
        [102.0, 202.0, float("nan"), 101.0, 201.0, float("nan")],
    ]
    df = pd.DataFrame(data, index=dates, columns=columns)
    df.index.name = "Date"
    return df


def test_reshape_prices_long_produces_expected_long_format_and_drops_all_null_ticker():
    raw = _raw_prices_fixture()
    symbol_to_ticker = {"AAPL": "AAPL", "BRK-B": "BRK.B", "GHOST": "GHOST"}

    long = reshape_prices_long(raw, symbol_to_ticker)

    assert list(long.columns) == ["date", "ticker", "close", "adj_close"]
    assert set(long["ticker"]) == {"AAPL", "BRK.B"}  # GHOST dropped: all-null

    aapl = long[long["ticker"] == "AAPL"].sort_values("date")
    assert list(aapl["close"]) == [100.0, 101.0, 102.0]
    assert list(aapl["adj_close"]) == [99.0, 100.0, 101.0]
    # BRK-B's dash symbol must be translated back to the dotted membership ticker.
    assert "BRK-B" not in set(long["ticker"])
    assert "BRK.B" in set(long["ticker"])


def test_detect_unresolved_tickers_flags_ticker_with_all_null_prices():
    all_tickers = ["AAPL", "BRK.B", "GHOST"]
    long_prices = pd.DataFrame(
        {
            "date": pd.to_datetime(["2020-01-02", "2020-01-02"]),
            "ticker": ["AAPL", "BRK.B"],
            "close": [100.0, 200.0],
            "adj_close": [99.0, 199.0],
        }
    )

    unresolved = detect_unresolved_tickers(all_tickers, long_prices)

    assert list(unresolved["ticker"]) == ["GHOST"]
    assert unresolved.loc[0, "reason"]


def test_detect_unresolved_tickers_returns_empty_typed_frame_when_all_resolved():
    all_tickers = ["AAPL"]
    long_prices = pd.DataFrame(
        {
            "date": pd.to_datetime(["2020-01-02"]),
            "ticker": ["AAPL"],
            "close": [1.0],
            "adj_close": [1.0],
        }
    )

    unresolved = detect_unresolved_tickers(all_tickers, long_prices)

    assert unresolved.empty
    assert list(unresolved.columns) == ["ticker", "reason"]
