"""Tests for src/dataset/ticker_currency.py's pure helpers.

Per AGENTS.md no test here calls yfinance: `fetch_ticker_currencies` is the
one function in that module performing network I/O and is exercised only
through monkeypatching in `tests/test_ticker_ingestion.py`, so everything
covered here is pure computation. The storage pair
(`upsert_ticker_currency_table`/`load_ticker_currencies`) is covered against
real fixture DuckDB files in `tests/test_dataset_upsert.py`, beside the
price-upsert tests it mirrors.
"""

import pandas as pd
import pytest

from src.dataset.ticker_currency import (
    DEFAULT_CURRENCY,
    apply_price_multipliers,
    group_by_currency,
    normalize_currency,
    partition_by_currency,
)


def _prices(*rows: tuple[str, float, float]) -> pd.DataFrame:
    """rows: (ticker, close, adj_close)."""
    return pd.DataFrame(
        {
            "date": [pd.Timestamp("2024-04-02")] * len(rows),
            "ticker": [r[0] for r in rows],
            "close": [r[1] for r in rows],
            "adj_close": [r[2] for r in rows],
        }
    )


# ---------------------------------------------------------------------------
# normalize_currency
# ---------------------------------------------------------------------------


def test_normalize_currency_leaves_a_major_unit_alone():
    assert normalize_currency("USD") == ("USD", 1.0)
    assert normalize_currency("JPY") == ("JPY", 1.0)


def test_normalize_currency_converts_pence_to_pounds():
    assert normalize_currency("GBp") == ("GBP", 0.01)


def test_normalize_currency_is_case_sensitive_so_pounds_are_not_divided():
    """'GBp' is pence and 'GBP' is pounds - one character of case apart. Upper-
    casing before the lookup would scale genuine sterling quotes by 1/100,
    which is why the minor-unit lookup deliberately precedes any case folding.
    """
    assert normalize_currency("GBP") == ("GBP", 1.0)
    assert normalize_currency("GBp") == ("GBP", 0.01)


def test_normalize_currency_handles_the_other_minor_units():
    assert normalize_currency("ZAc") == ("ZAR", 0.01)
    assert normalize_currency("ILA") == ("ILS", 0.01)


def test_normalize_currency_upcases_and_strips_an_unknown_major_unit():
    assert normalize_currency(" sgd ") == ("SGD", 1.0)


# ---------------------------------------------------------------------------
# apply_price_multipliers
# ---------------------------------------------------------------------------


def test_apply_price_multipliers_scales_both_price_columns():
    """Both columns are quoted in the minor unit - verified against Yahoo
    Finance, where BARC.L reports close 184.119995 and adj_close 184.006180
    for a share that genuinely traded near GBP 1.84 - and `adj_close` is the
    one `load_latest_prices` reads.
    """
    df = _prices(("BARC.L", 184.119995, 184.006180))

    scaled = apply_price_multipliers(df, {"BARC.L": 0.01})

    assert scaled["close"].iloc[0] == pytest.approx(1.84119995)
    assert scaled["adj_close"].iloc[0] == pytest.approx(1.84006180)


def test_apply_price_multipliers_leaves_other_tickers_untouched():
    df = _prices(("BARC.L", 184.12, 184.01), ("AAPL", 168.84, 167.02))

    scaled = apply_price_multipliers(df, {"BARC.L": 0.01})

    aapl = scaled[scaled["ticker"] == "AAPL"].iloc[0]
    assert aapl["close"] == pytest.approx(168.84)
    assert aapl["adj_close"] == pytest.approx(167.02)


def test_apply_price_multipliers_with_no_multipliers_is_a_no_op():
    df = _prices(("AAPL", 168.84, 167.02))

    pd.testing.assert_frame_equal(apply_price_multipliers(df, {}), df)


def test_apply_price_multipliers_does_not_mutate_its_input():
    df = _prices(("BARC.L", 184.12, 184.01))

    apply_price_multipliers(df, {"BARC.L": 0.01})

    assert df["close"].iloc[0] == pytest.approx(184.12)


def test_apply_price_multipliers_on_an_empty_frame_keeps_its_columns():
    empty = pd.DataFrame(columns=["date", "ticker", "close", "adj_close"])

    result = apply_price_multipliers(empty, {"BARC.L": 0.01})

    assert list(result.columns) == ["date", "ticker", "close", "adj_close"]
    assert result.empty


# ---------------------------------------------------------------------------
# group_by_currency
# ---------------------------------------------------------------------------


def test_group_by_currency_buckets_and_sorts():
    grouped = group_by_currency(
        ["7203.T", "AAPL", "6758.T"], {"7203.T": "JPY", "6758.T": "JPY", "AAPL": "USD"}
    )

    assert grouped == {"JPY": ["6758.T", "7203.T"], "USD": ["AAPL"]}
    assert list(grouped) == ["JPY", "USD"]


def test_group_by_currency_defaults_an_unrecorded_ticker_to_usd():
    """A ticker with no row is every ticker in the rest of this project - the
    S&P 500 universe and the shared cache - which is US-listed by
    construction, so a missing record means dollars, not unknown.
    """
    grouped = group_by_currency(["AAPL", "SPY"], {})

    assert grouped == {DEFAULT_CURRENCY: ["AAPL", "SPY"]}


def test_group_by_currency_single_key_means_one_portfolio_is_possible():
    assert len(group_by_currency(["7203.T", "6758.T"], {"7203.T": "JPY", "6758.T": "JPY"})) == 1


# ---------------------------------------------------------------------------
# partition_by_currency
# ---------------------------------------------------------------------------


def test_partition_by_currency_refuses_a_ticker_that_does_not_match_the_pool():
    accepted, refused, currency = partition_by_currency(
        ["AAPL", "6758.T"], "JPY", {"AAPL": "USD", "6758.T": "JPY"}
    )

    assert accepted == ["6758.T"]
    assert refused == {"AAPL": "USD"}
    assert currency == "JPY"


def test_partition_by_currency_first_candidate_establishes_an_empty_pools_currency():
    accepted, refused, currency = partition_by_currency(
        ["7203.T", "AAPL"], None, {"7203.T": "JPY", "AAPL": "USD"}
    )

    assert accepted == ["7203.T"]
    assert refused == {"AAPL": "USD"}
    assert currency == "JPY"


def test_partition_by_currency_first_candidate_rule_respects_the_other_ordering():
    """The mirror image of the test above: the order the tickers arrive in is
    what decides, which is why the caller must pass them in the order the
    person typed rather than sorted.
    """
    accepted, refused, currency = partition_by_currency(
        ["AAPL", "7203.T"], None, {"7203.T": "JPY", "AAPL": "USD"}
    )

    assert accepted == ["AAPL"]
    assert refused == {"7203.T": "JPY"}
    assert currency == "USD"


def test_partition_by_currency_accepts_everything_when_all_agree():
    accepted, refused, currency = partition_by_currency(
        ["7203.T", "6758.T"], None, {"7203.T": "JPY", "6758.T": "JPY"}
    )

    assert accepted == ["7203.T", "6758.T"]
    assert refused == {}
    assert currency == "JPY"


def test_partition_by_currency_empty_candidates_leaves_the_pool_currency_alone():
    assert partition_by_currency([], "JPY", {}) == ([], {}, "JPY")
    assert partition_by_currency([], None, {}) == ([], {}, None)


def test_partition_by_currency_treats_unrecorded_tickers_as_usd():
    accepted, refused, currency = partition_by_currency(["AAPL", "SPY"], None, {})

    assert accepted == ["AAPL", "SPY"]
    assert refused == {}
    assert currency == DEFAULT_CURRENCY
