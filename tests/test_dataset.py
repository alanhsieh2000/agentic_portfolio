"""Tests for src/dataset/membership.py, src/dataset/prices.py,
src/dataset/fundamentals.py, and src/dataset/sec_edgar.py.

Per AGENTS.md, no test here calls yfinance, fetches the live Wikipedia
page, or calls SEC EDGAR; all inputs are hand-written in-memory fixtures.
"""

import math

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
from src.dataset.fundamentals import (
    BookEquityLineItemNotFoundError,
    add_cross_sectional_z,
    compute_bm,
    compute_market_cap,
    compute_mve,
    cumulative_split_ratio_after,
    find_book_equity_row,
    most_recent_book_equity_before_lag,
    most_recent_shares_on_or_before,
    select_book_equity,
)
from src.dataset.sec_edgar import (
    dedupe_to_earliest_filed,
    extract_book_equity_facts_from_company_facts,
    has_sufficient_coverage,
    lookup_cik,
    select_book_equity_asof,
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


def test_cumulative_split_ratio_after_multiplies_only_splits_strictly_after_as_of():
    splits = pd.Series(
        [2.0, 4.0, 10.0],
        index=pd.to_datetime(["2010-06-01", "2020-08-31", "2024-06-10"]).tz_localize("America/New_York"),
    )

    assert cumulative_split_ratio_after(splits, "2009-01-01") == pytest.approx(2.0 * 4.0 * 10.0)
    assert cumulative_split_ratio_after(splits, "2015-01-01") == pytest.approx(4.0 * 10.0)
    assert cumulative_split_ratio_after(splits, "2021-01-01") == pytest.approx(10.0)
    assert cumulative_split_ratio_after(splits, "2025-01-01") == pytest.approx(1.0)


def test_cumulative_split_ratio_after_empty_series_returns_one():
    assert cumulative_split_ratio_after(pd.Series(dtype="float64"), "2020-01-01") == 1.0


def test_most_recent_shares_on_or_before_dedupes_and_picks_nearest_prior():
    shares = pd.Series(
        [4.0e9, 4.1e9, 4.3e9],
        index=pd.to_datetime(["2019-01-01", "2019-06-01", "2019-06-01"]).tz_localize("America/New_York"),
    )

    # Duplicate 2019-06-01 rows: keep-last means 4.3e9 wins.
    assert most_recent_shares_on_or_before(shares, "2019-12-01") == pytest.approx(4.3e9)
    assert most_recent_shares_on_or_before(shares, "2018-01-01") is None


def test_compute_mve_reproduces_aapl_trillion_dollar_sanity_check():
    """Fixture mirrors the real AAPL scenario: a raw shares count from
    before the 2020-08-31 4-for-1 split, a split-adjusted price from the
    prices table for the same pre-split date, and one split after `as_of`.
    """
    price = 75.087502  # real 2020-01-02 adj_close-basis value
    shares_raw = 4.275e9  # real pre-split raw shares outstanding
    splits = pd.Series([4.0], index=pd.to_datetime(["2020-08-31"]).tz_localize("America/New_York"))
    as_of = "2020-01-02"

    mve = compute_mve(price, shares_raw, splits, as_of)

    expected = math.log(price * shares_raw * 4.0)
    assert mve == pytest.approx(expected)
    market_cap = math.exp(mve)
    assert 1.2e12 < market_cap < 1.4e12  # ~$1.284T, matching AAPL's real Jan 2020 market cap


def test_compute_mve_returns_none_when_price_or_shares_missing():
    assert compute_mve(None, 4.0e9, pd.Series(dtype="float64"), "2020-01-01") is None
    assert compute_mve(75.0, None, pd.Series(dtype="float64"), "2020-01-01") is None
    assert compute_mve(float("nan"), 4.0e9, pd.Series(dtype="float64"), "2020-01-01") is None


def test_add_cross_sectional_z_has_mean_zero_and_variance_one():
    df = pd.DataFrame(
        {
            "rebalance_date": pd.to_datetime(["2020-01-01"] * 4),
            "ticker": ["AAA", "BBB", "CCC", "DDD"],
            "mve": [20.0, 22.0, 24.0, 26.0],
        }
    )

    result = add_cross_sectional_z(df, "mve", "mve_z")

    assert result["mve_z"].mean() == pytest.approx(0.0, abs=1e-9)
    assert result["mve_z"].var(ddof=1) == pytest.approx(1.0, abs=1e-9)


def test_add_cross_sectional_z_masks_zero_std_group_instead_of_producing_inf():
    df = pd.DataFrame(
        {
            "rebalance_date": pd.to_datetime(["2020-01-01", "2020-01-01"]),
            "ticker": ["AAA", "BBB"],
            "mve": [20.0, 20.0],  # identical -> std == 0
        }
    )

    result = add_cross_sectional_z(df, "mve", "mve_z")

    assert result["mve_z"].isna().all()


def test_find_book_equity_row_prefers_common_stock_equity_when_present():
    bs = pd.DataFrame(
        {pd.Timestamp("2022-09-30"): [50.0, 50.0, 55.0, -2.0]},
        index=["Common Stock Equity", "Stockholders Equity", "Total Equity Gross Minority Interest", "Other Equity Adjustments"],
    )

    row = find_book_equity_row(bs, "AAPL")

    assert row.loc[pd.Timestamp("2022-09-30")] == 50.0


def test_find_book_equity_row_falls_back_to_stockholders_equity():
    bs = pd.DataFrame(
        {pd.Timestamp("2022-09-30"): [50.0, 55.0]},
        index=["Stockholders Equity", "Total Equity Gross Minority Interest"],
    )

    row = find_book_equity_row(bs, "XOM")

    assert row.loc[pd.Timestamp("2022-09-30")] == 50.0


def test_find_book_equity_row_falls_back_to_total_equity_gross_minority_interest():
    bs = pd.DataFrame({pd.Timestamp("2022-09-30"): [266626.0]}, index=["Total Equity Gross Minority Interest"])

    row = find_book_equity_row(bs, "XOM")

    assert row.loc[pd.Timestamp("2022-09-30")] == 266626.0


def test_find_book_equity_row_raises_loudly_when_no_alias_matches():
    bs = pd.DataFrame({pd.Timestamp("2022-09-30"): [-7172.0]}, index=["Other Equity Adjustments"])

    with pytest.raises(BookEquityLineItemNotFoundError, match="AAPL"):
        find_book_equity_row(bs, "AAPL")


def test_find_book_equity_row_returns_none_for_entirely_empty_balance_sheet():
    assert find_book_equity_row(pd.DataFrame(), "ZZZZZINVALID") is None


def test_most_recent_book_equity_before_lag_picks_latest_eligible_column():
    row = pd.Series([50672.0, 62146.0], index=pd.to_datetime(["2022-09-30", "2023-09-30"]))

    # as_of=2024-01-01, lag=3mo -> cutoff 2023-10-01; both columns eligible, latest is 2023-09-30.
    assert most_recent_book_equity_before_lag(row, "2024-01-01", lag_months=3) == pytest.approx(62146.0)


def test_most_recent_book_equity_before_lag_skips_nan_eligible_column_for_earlier_populated_one():
    """Regression for the verified-live AAPL case: the most-recent eligible
    column (2021-09-30) is itself NaN; an older eligible column
    (2020-09-30) has a value and should be used instead of returning None.
    """
    row = pd.Series([40000.0, float("nan")], index=pd.to_datetime(["2020-09-30", "2021-09-30"]))

    # as_of=2022-01-01, lag=3mo -> cutoff 2021-10-01; both eligible, 2021-09-30 is more recent but NaN.
    assert most_recent_book_equity_before_lag(row, "2022-01-01", lag_months=3) == pytest.approx(40000.0)


def test_most_recent_book_equity_before_lag_returns_none_when_no_column_satisfies_lag():
    row = pd.Series([50672.0], index=pd.to_datetime(["2023-09-30"]))

    # as_of=2023-10-01, lag=3mo -> cutoff 2023-07-01; 2023-09-30 is NOT <= cutoff.
    assert most_recent_book_equity_before_lag(row, "2023-10-01", lag_months=3) is None


def test_most_recent_book_equity_before_lag_returns_none_for_empty_row():
    assert most_recent_book_equity_before_lag(None, "2024-01-01") is None
    assert most_recent_book_equity_before_lag(pd.Series(dtype="float64"), "2024-01-01") is None


def test_select_book_equity_prefers_quarterly_when_it_satisfies_lag():
    quarterly_bs = pd.DataFrame({pd.Timestamp("2023-09-30"): [61000.0]}, index=["Common Stock Equity"])
    annual_bs = pd.DataFrame({pd.Timestamp("2023-09-30"): [62146.0]}, index=["Common Stock Equity"])

    value = select_book_equity(quarterly_bs, annual_bs, "2024-01-01", "AAPL", lag_months=3)

    assert value == pytest.approx(61000.0)  # quarterly value used, not annual's


def test_select_book_equity_falls_back_to_annual_when_quarterly_has_no_eligible_column():
    quarterly_bs = pd.DataFrame({pd.Timestamp("2024-12-31"): [70000.0]}, index=["Common Stock Equity"])
    annual_bs = pd.DataFrame({pd.Timestamp("2022-09-30"): [50672.0]}, index=["Common Stock Equity"])

    # as_of=2024-01-01: quarterly's only column (2024-12-31) fails the lag rule entirely.
    value = select_book_equity(quarterly_bs, annual_bs, "2024-01-01", "AAPL", lag_months=3)

    assert value == pytest.approx(50672.0)


def test_compute_bm_aapl_style_fixture_is_small_positive_fraction():
    """AAPL is a documented 'growth' stock priced far above book value; bm
    should land as a small positive number under 1. Reuses the same
    real-scale price/shares/splits fixture as
    test_compute_mve_reproduces_aapl_trillion_dollar_sanity_check.
    """
    price = 75.087502
    shares_raw = 4.275e9
    splits = pd.Series([4.0], index=pd.to_datetime(["2020-08-31"]).tz_localize("America/New_York"))
    as_of = "2020-01-02"
    book_equity = 90_488_000_000.0  # plausible AAPL FY2019-scale total equity, in dollars

    bm = compute_bm(book_equity, price, shares_raw, splits, as_of)

    assert bm is not None
    assert 0.0 < bm < 1.0
    assert bm == pytest.approx(book_equity / (price * shares_raw * 4.0))


def test_compute_bm_returns_none_when_book_equity_missing():
    assert compute_bm(None, 75.0, 4.275e9, pd.Series(dtype="float64"), "2020-01-02") is None
    assert compute_bm(float("nan"), 75.0, 4.275e9, pd.Series(dtype="float64"), "2020-01-02") is None


def test_compute_market_cap_and_compute_mve_agree():
    """Regression for the compute_mve/compute_market_cap refactor: mve must
    still equal log(market_cap) exactly.
    """
    price = 75.087502
    shares_raw = 4.275e9
    splits = pd.Series([4.0], index=pd.to_datetime(["2020-08-31"]).tz_localize("America/New_York"))
    as_of = "2020-01-02"

    market_cap = compute_market_cap(price, shares_raw, splits, as_of)
    mve = compute_mve(price, shares_raw, splits, as_of)

    assert market_cap is not None
    assert mve == pytest.approx(math.log(market_cap))


def test_lookup_cik_finds_known_ticker_and_returns_none_for_unknown():
    cik_map = {"AAPL": 320193, "BRK-B": 1067983}

    assert lookup_cik("AAPL", cik_map) == 320193
    assert lookup_cik("BRK.B", cik_map) == 1067983  # dot translated to dash before lookup
    assert lookup_cik("TWTR", cik_map) is None


def _facts_fixture(rows: list[tuple[str, float, str]]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "end": pd.to_datetime([r[0] for r in rows]),
            "val": [r[1] for r in rows],
            "filed": pd.to_datetime([r[2] for r in rows]),
        }
    )


def test_dedupe_to_earliest_filed_keeps_earliest_filed_per_end_val():
    """Mirrors AAPL's real verified shape: the same (end, val) fact appears
    under multiple later filings' comparative restatements.
    """
    facts = _facts_fixture(
        [
            ("2020-03-28", 78_425_000_000.0, "2020-07-31"),
            ("2020-03-28", 78_425_000_000.0, "2020-05-01"),
            ("2020-03-28", 78_425_000_000.0, "2021-04-29"),
        ]
    )

    deduped = dedupe_to_earliest_filed(facts)

    assert len(deduped) == 1
    assert deduped.iloc[0]["filed"] == pd.Timestamp("2020-05-01")


def test_dedupe_to_earliest_filed_keeps_distinct_rows_for_true_restatements():
    facts = _facts_fixture(
        [
            ("2007-09-29", 100.0, "2007-11-15"),
            ("2007-09-29", 95.0, "2008-11-15"),  # genuine restatement: different val, same end
        ]
    )

    deduped = dedupe_to_earliest_filed(facts)

    assert len(deduped) == 2
    assert set(deduped["val"]) == {100.0, 95.0}


def test_dedupe_to_earliest_filed_returns_empty_for_empty_input():
    empty = pd.DataFrame(columns=["end", "val", "filed"])
    deduped = dedupe_to_earliest_filed(empty)
    assert deduped.empty


def test_select_book_equity_asof_picks_latest_end_with_filed_on_or_before_as_of():
    facts = _facts_fixture(
        [
            ("2019-09-28", 90_000_000_000.0, "2019-10-31"),
            ("2020-03-28", 78_425_000_000.0, "2020-05-01"),
        ]
    )

    value = select_book_equity_asof(facts, "2020-06-01")

    assert value == pytest.approx(78_425_000_000.0)


def test_select_book_equity_asof_returns_none_when_no_fact_filed_on_or_before_as_of():
    facts = _facts_fixture([("2020-03-28", 78_425_000_000.0, "2020-05-01")])

    value = select_book_equity_asof(facts, "2020-04-01")

    assert value is None


def test_select_book_equity_asof_tie_breaks_restated_same_end_by_latest_filed_still_eligible():
    facts = _facts_fixture(
        [
            ("2007-09-29", 100.0, "2007-11-15"),
            ("2007-09-29", 95.0, "2008-11-15"),  # restatement, filed later
        ]
    )

    assert select_book_equity_asof(facts, "2008-01-01") == pytest.approx(100.0)  # only the original is eligible yet
    assert select_book_equity_asof(facts, "2009-01-01") == pytest.approx(95.0)  # restatement now eligible, wins tie


def test_has_sufficient_coverage_true_when_earliest_end_predates_latest_rebalance():
    facts = _facts_fixture([("2019-09-28", 90_000_000_000.0, "2019-10-31")])
    assert has_sufficient_coverage(facts, "2024-04-01") is True


def test_has_sufficient_coverage_false_for_entity_succession_fixture():
    """Mirrors the verified live XOM case: the resolved CIK's only facts
    postdate this project's latest rebalance date entirely.
    """
    facts = _facts_fixture(
        [
            ("2025-12-31", 259_386_000_000.0, "2026-02-01"),
            ("2026-06-30", 263_000_000_000.0, "2026-08-01"),
        ]
    )

    assert has_sufficient_coverage(facts, "2024-04-01") is False


def test_has_sufficient_coverage_false_for_empty_facts():
    assert has_sufficient_coverage(pd.DataFrame(columns=["end", "val", "filed"]), "2024-04-01") is False


def test_compute_bm_via_sec_facts_reproduces_aapl_2020_03_28_figure():
    """End-to-end-style test closing the coverage gap: AAPL's real,
    verified SEC-reported stockholders' equity for fiscal Q2 2020
    ($78.425B, filed 2020-05-01) combined with the same price/shares/splits
    fixture used by test_compute_mve_reproduces_aapl_trillion_dollar_sanity_check
    should now produce a non-null bm for a 2020 rebalance date -- something
    the yfinance-only path never could (verified live: yfinance's balance
    sheet data starts no earlier than ~2021-2022).
    """
    price = 75.087502
    shares_raw = 4.275e9
    splits = pd.Series([4.0], index=pd.to_datetime(["2020-08-31"]).tz_localize("America/New_York"))
    as_of = "2020-06-01"
    facts = _facts_fixture([("2020-03-28", 78_425_000_000.0, "2020-05-01")])

    book_equity = select_book_equity_asof(facts, as_of)
    bm = compute_bm(book_equity, price, shares_raw, splits, as_of)

    assert book_equity is not None
    assert bm is not None
    assert 0.0 < bm < 1.0


def test_extract_book_equity_facts_from_company_facts_finds_populated_tag():
    """Mirrors the verified live Abbott Laboratories case: companyconcept
    returned zero facts for StockholdersEquity, but the bulk companyfacts
    blob has real data for the exact same tag.
    """
    company_facts = {
        "facts": {
            "us-gaap": {
                "StockholdersEquity": {
                    "units": {
                        "USD": [
                            {"end": "2019-12-31", "val": 17_778_540_000, "filed": "2020-02-19", "form": "10-K", "accn": "x"},
                        ]
                    }
                }
            }
        }
    }

    facts = extract_book_equity_facts_from_company_facts(company_facts, "StockholdersEquity")

    assert len(facts) == 1
    assert facts.iloc[0]["val"] == 17_778_540_000


def test_extract_book_equity_facts_from_company_facts_returns_empty_for_missing_tag():
    assert extract_book_equity_facts_from_company_facts({"facts": {"us-gaap": {}}}, "StockholdersEquity").empty
    assert extract_book_equity_facts_from_company_facts({}, "StockholdersEquity").empty
