"""Tests for the dividend data layer (`src/agentic_portfolio/dataset/dividends.py`) and the
dividend constraint's pure parts (`src/agentic_portfolio/optimizer/dividends.py`).

Per AGENTS.md, every test here is deterministic and hermetic: yfinance
responses are hand-built fixtures shaped like the real `actions=True`
download, DuckDB work happens in a `tmp_path` file, and nothing touches the
network or the real `data/` databases.
"""

from __future__ import annotations

import datetime as dt

import duckdb
import numpy as np
import pandas as pd
import pytest

from agentic_portfolio.dataset.dividends import (
    DIVIDEND_UNRESOLVED_COLUMNS,
    DIVIDENDS_LONG_COLUMNS,
    SPLITS_LONG_COLUMNS,
    dividend_unresolved_reasons,
    unresolved_frame,
    unresolved_frame_from_caller,
    announced_amount,
    has_splits_table,
    reshape_splits_long,
    split_series_by_ticker,
    DividendFieldMissingError,
    apply_dividend_multipliers,
    coverage_frame,
    fetched_dividend_tickers,
    load_dividend_figures,
    reconcile_trailing_yield,
    reshape_dividends_long,
    tickers_with_dividend_data,
    trailing_dividend_yields,
    trailing_dividends_per_share,
    upsert_dividends_tables,
    write_dividends_tables,
)
from agentic_portfolio.optimizer.dividends import (
    MAX_DIVIDEND_YIELD,
    DividendFloor,
    DividendFloorError,
    DividendYieldUnavailableError,
    check_dividend_floor_feasible,
    covered_dividend_yield,
    dividend_figures,
    dividend_yield_ceiling,
    dividend_yield_vector,
    portfolio_dividend_yield,
    validate_dividend_yield,
    validate_min_annual_dividend,
)


def _raw_actions_fixture() -> pd.DataFrame:
    """Shaped like yfinance's real multi-ticker `download()` output with
    `auto_adjust=False, actions=True`: a 2-level (field, symbol) MultiIndex
    whose top level carries `Dividends` and `Stock Splits` alongside the
    price fields.

    PAYER pays twice. NONPAYER is a real non-payer, zero-filled on every
    row exactly as yfinance reports one. NAN_ROW carries a NaN dividend on
    one date, which is what yfinance's union-index reindex produces when
    another ticker introduced that date - it must read as "no dividend", not
    as data.
    """
    dates = pd.to_datetime(["2024-03-05", "2024-06-11", "2024-09-10"])
    columns = pd.MultiIndex.from_tuples(
        [
            ("Close", "PAYER"), ("Close", "NONPAYER"), ("Close", "NAN_ROW"),
            ("Adj Close", "PAYER"), ("Adj Close", "NONPAYER"), ("Adj Close", "NAN_ROW"),
            ("Dividends", "PAYER"), ("Dividends", "NONPAYER"), ("Dividends", "NAN_ROW"),
            ("Stock Splits", "PAYER"), ("Stock Splits", "NONPAYER"), ("Stock Splits", "NAN_ROW"),
        ],
        names=["Price", "Ticker"],
    )
    data = {
        ("Close", "PAYER"): [100.0, 105.0, 110.0],
        ("Close", "NONPAYER"): [50.0, 52.0, 54.0],
        ("Close", "NAN_ROW"): [20.0, 21.0, 22.0],
        ("Adj Close", "PAYER"): [99.0, 104.0, 110.0],
        ("Adj Close", "NONPAYER"): [50.0, 52.0, 54.0],
        ("Adj Close", "NAN_ROW"): [20.0, 21.0, 22.0],
        ("Dividends", "PAYER"): [0.50, 0.0, 0.60],
        ("Dividends", "NONPAYER"): [0.0, 0.0, 0.0],
        ("Dividends", "NAN_ROW"): [float("nan"), 0.0, 0.0],
        ("Stock Splits", "PAYER"): [0.0, 0.0, 0.0],
        ("Stock Splits", "NONPAYER"): [0.0, 0.0, 0.0],
        ("Stock Splits", "NAN_ROW"): [0.0, 0.0, 0.0],
    }
    return pd.DataFrame(data, index=dates, columns=columns)


IDENTITY = {"PAYER": "PAYER", "NONPAYER": "NONPAYER", "NAN_ROW": "NAN_ROW"}


# --------------------------------------------------------------------------
# Pure reshape
# --------------------------------------------------------------------------


def test_reshape_dividends_long_keeps_only_the_rows_that_paid_something():
    long_df = reshape_dividends_long(_raw_actions_fixture(), IDENTITY)
    assert list(long_df.columns) == DIVIDENDS_LONG_COLUMNS
    assert len(long_df) == 2
    assert long_df["ticker"].unique().tolist() == ["PAYER"]
    assert long_df["amount"].tolist() == [0.50, 0.60]


def test_reshape_dividends_long_gives_a_non_payer_no_rows_at_all():
    long_df = reshape_dividends_long(_raw_actions_fixture(), IDENTITY)
    assert "NONPAYER" not in set(long_df["ticker"])


def test_reshape_dividends_long_treats_a_nan_amount_as_absent_not_as_data():
    long_df = reshape_dividends_long(_raw_actions_fixture(), IDENTITY)
    assert "NAN_ROW" not in set(long_df["ticker"])
    assert not long_df["amount"].isna().any()


def test_reshape_dividends_long_translates_a_dash_symbol_back_to_a_dotted_ticker():
    raw = _raw_actions_fixture().rename(columns={"PAYER": "BRK-B"}, level=1)
    long_df = reshape_dividends_long(raw, {"BRK-B": "BRK.B"})
    assert long_df["ticker"].unique().tolist() == ["BRK.B"]


def test_reshape_dividends_long_raises_when_prices_arrived_without_the_actions_columns():
    raw = _raw_actions_fixture()
    raw = raw.drop(columns=["Dividends", "Stock Splits"], level=0)
    with pytest.raises(DividendFieldMissingError, match="actions=True column contract"):
        reshape_dividends_long(raw, IDENTITY)


def test_reshape_dividends_long_returns_an_empty_frame_when_the_whole_batch_failed():
    """A batch where every symbol failed legitimately has no `Dividends`
    column, because yfinance represents such a ticker with an empty frame
    carrying only the price columns. That must not be mistaken for the
    contract having changed - otherwise one rate-limited batch becomes a
    crash.
    """
    raw = _raw_actions_fixture().drop(columns=["Dividends", "Stock Splits"], level=0)
    for ticker in ("PAYER", "NONPAYER", "NAN_ROW"):
        raw[("Close", ticker)] = float("nan")
        raw[("Adj Close", ticker)] = float("nan")
    result = reshape_dividends_long(raw, IDENTITY)
    assert result.empty
    assert list(result.columns) == DIVIDENDS_LONG_COLUMNS


def test_reshape_dividends_long_returns_an_empty_typed_frame_for_empty_input():
    result = reshape_dividends_long(pd.DataFrame(), {})
    assert result.empty
    assert list(result.columns) == DIVIDENDS_LONG_COLUMNS


def test_fetched_dividend_tickers_reports_only_the_symbols_that_came_back():
    raw = _raw_actions_fixture().drop(columns=[("Dividends", "NONPAYER")])
    assert fetched_dividend_tickers(raw, IDENTITY) == {"PAYER", "NAN_ROW"}


# --------------------------------------------------------------------------
# Minor-unit normalization
# --------------------------------------------------------------------------


def _pence_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "ex_date": pd.to_datetime(["2024-02-29", "2024-08-15"]),
            "ticker": ["BARC.L", "BARC.L"],
            "amount": [5.3, 2.9],
        }
    )


def test_apply_dividend_multipliers_scales_a_pence_dividend_into_pounds():
    scaled = apply_dividend_multipliers(_pence_frame(), {"BARC.L": 0.01})
    assert scaled["amount"].tolist() == pytest.approx([0.053, 0.029])


def test_apply_dividend_multipliers_leaves_a_ticker_absent_from_the_map_alone():
    frame = pd.concat([_pence_frame(), pd.DataFrame(
        {"ex_date": pd.to_datetime(["2024-03-05"]), "ticker": ["KO"], "amount": [0.485]}
    )], ignore_index=True)
    scaled = apply_dividend_multipliers(frame, {"BARC.L": 0.01})
    assert scaled.loc[scaled["ticker"] == "KO", "amount"].tolist() == [0.485]


def test_a_pence_quote_and_its_pound_equivalent_give_the_same_yield():
    """The invariant that actually matters, rather than one scale factor.

    A London holding quoted in pence with pence dividends must produce the
    same yield as the identical holding expressed in pounds. Scaling only
    the prices, and not the dividends, would report a 3% yielder at 310% -
    high enough to satisfy any floor a person could type.
    """
    pence = trailing_dividend_yields(
        {"BARC.L": 5.3 + 2.9}, pd.Series({"BARC.L": 264.75}), {"BARC.L"}
    )[0]
    pounds = trailing_dividend_yields(
        {"BARC.L": (5.3 + 2.9) * 0.01}, pd.Series({"BARC.L": 264.75 * 0.01}), {"BARC.L"}
    )[0]
    assert pence["BARC.L"] == pytest.approx(pounds["BARC.L"])


# --------------------------------------------------------------------------
# The trailing window
# --------------------------------------------------------------------------


def _quarterly(dates: list[str], amount: float = 0.25) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "ex_date": pd.to_datetime(dates),
            "ticker": ["Q"] * len(dates),
            "amount": [amount] * len(dates),
        }
    )


def test_a_dividend_exactly_lookback_months_before_as_of_is_excluded():
    """Otherwise a payer on a steady schedule reports five payments in a
    twelve-month window on the one run whose date lands on an anniversary,
    overstating its yield by a quarter.
    """
    frame = _quarterly(["2025-09-04", "2025-12-04", "2026-03-04", "2026-06-04"])
    total = trailing_dividends_per_share(frame, ["Q"], dt.date(2026, 9, 4), 12)
    assert total["Q"] == pytest.approx(0.75)


def test_a_dividend_one_day_inside_the_window_is_included():
    frame = _quarterly(["2025-09-05", "2025-12-04", "2026-03-04", "2026-06-04"])
    total = trailing_dividends_per_share(frame, ["Q"], dt.date(2026, 9, 4), 12)
    assert total["Q"] == pytest.approx(1.00)


def test_a_dividend_dated_after_as_of_is_excluded():
    frame = _quarterly(["2026-06-04", "2026-12-04"])
    total = trailing_dividends_per_share(frame, ["Q"], dt.date(2026, 9, 4), 12)
    assert total["Q"] == pytest.approx(0.25)


def test_the_lookback_window_is_configurable():
    """A 6-month window over the same payments keeps only 2026-06-04. The
    2026-03-04 payment sits exactly six months before `as_of` and so falls
    on the excluded boundary, by the same half-open rule the twelve-month
    test above pins - which is what stops one payment being counted twice
    when two window lengths are compared.
    """
    frame = _quarterly(["2025-09-05", "2025-12-04", "2026-03-04", "2026-06-04"])
    total = trailing_dividends_per_share(frame, ["Q"], dt.date(2026, 9, 4), 6)
    assert total["Q"] == pytest.approx(0.25)

    # One day later on the same schedule, the March payment is inside a
    # six-month window - the boundary is exact, not approximate.
    shifted = _quarterly(["2026-03-05", "2026-06-04"])
    assert trailing_dividends_per_share(shifted, ["Q"], dt.date(2026, 9, 4), 6)["Q"] == pytest.approx(0.50)


def test_a_ticker_with_no_rows_totals_zero_rather_than_going_missing():
    total = trailing_dividends_per_share(_quarterly(["2026-06-04"]), ["Q", "OTHER"], dt.date(2026, 9, 4))
    assert total["OTHER"] == 0.0


# --------------------------------------------------------------------------
# Missing data versus a genuine non-payer
# --------------------------------------------------------------------------


def test_a_covered_non_payer_yields_exactly_zero_not_nan():
    yields, unavailable = trailing_dividend_yields(
        {"NONPAYER": 0.0}, pd.Series({"NONPAYER": 50.0}), {"NONPAYER"}
    )
    assert yields["NONPAYER"] == 0.0
    assert not pd.isna(yields["NONPAYER"])
    assert unavailable == {}


def test_an_uncovered_ticker_is_unavailable_never_zero():
    """The whole reason `dividend_coverage` exists: a ticker nobody fetched
    must not report the same confident zero a real non-payer does.
    """
    yields, unavailable = trailing_dividend_yields(
        {"UNKNOWN": 0.0}, pd.Series({"UNKNOWN": 50.0}), set()
    )
    assert "UNKNOWN" not in yields
    assert "no dividend data has been fetched" in unavailable["UNKNOWN"]


def test_a_ticker_with_no_price_is_unavailable():
    yields, unavailable = trailing_dividend_yields(
        {"T": 1.11}, pd.Series({"T": float("nan")}), {"T"}
    )
    assert "T" not in yields
    assert "no price" in unavailable["T"]


def test_a_ticker_with_a_non_positive_price_is_unavailable():
    yields, unavailable = trailing_dividend_yields({"T": 1.11}, pd.Series({"T": 0.0}), {"T"})
    assert "T" not in yields
    assert "no yield can be computed" in unavailable["T"]


# --------------------------------------------------------------------------
# The offline reconciliation
# --------------------------------------------------------------------------


def test_reconcile_trailing_yield_recovers_a_known_dividend_return():
    """Built FORWARDS from a known dividend so the fixture is independent of
    the code under test: `adj_close` is `close` times a factor that steps by
    exactly `1 / (1 - d/close)` on the ex-date, which is the identity
    Yahoo's own back-adjustment uses.
    """
    dates = pd.date_range("2025-08-01", "2026-09-04", freq="MS")
    close = pd.Series(100.0, index=dates)
    factor = pd.Series(1.0, index=dates)
    step = 1.0 / (1.0 - 2.0 / 100.0)
    factor.loc[dates[dates >= pd.Timestamp("2026-01-01")]] = step
    frame = pd.DataFrame(
        {"date": dates, "close": close.to_numpy(), "adj_close": (close * factor).to_numpy()}
    )
    implied = reconcile_trailing_yield(frame, dt.date(2026, 9, 4), 12)
    assert implied == pytest.approx(step - 1.0, rel=1e-9)


def test_reconcile_trailing_yield_is_zero_for_a_non_payer():
    dates = pd.date_range("2025-08-01", "2026-09-04", freq="MS")
    frame = pd.DataFrame({"date": dates, "close": 100.0, "adj_close": 100.0})
    assert reconcile_trailing_yield(frame, dt.date(2026, 9, 4), 12) == pytest.approx(0.0)


def test_reconcile_trailing_yield_is_none_when_the_window_is_not_covered():
    dates = pd.date_range("2026-06-01", "2026-09-04", freq="MS")
    frame = pd.DataFrame({"date": dates, "close": 100.0, "adj_close": 100.0})
    assert reconcile_trailing_yield(frame, dt.date(2026, 9, 4), 12) is None


# --------------------------------------------------------------------------
# Persistence
# --------------------------------------------------------------------------


def _read(db_path, table):
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        return con.execute(f"SELECT * FROM {table} ORDER BY ticker").fetchdf()
    finally:
        con.close()


def _rows(ticker="AAPL", amount=0.24):
    return pd.DataFrame(
        {
            "ex_date": pd.to_datetime(["2024-02-09"]),
            "ticker": [ticker],
            "amount": [amount],
        }
    )


def test_upsert_dividends_tables_creates_both_tables_when_absent(tmp_path):
    db = tmp_path / "d.duckdb"
    upsert_dividends_tables(
        _rows(), coverage_frame(["AAPL"], set(), "2024-01-01", "2024-12-31"), ["AAPL"], str(db)
    )
    assert _read(db, "dividends")["amount"].tolist() == [0.24]
    assert _read(db, "dividend_coverage")["ticker"].tolist() == ["AAPL"]


def test_upsert_dividends_tables_replaces_only_the_named_tickers(tmp_path):
    db = tmp_path / "d.duckdb"
    seed = pd.concat([_rows("AAPL"), _rows("MSFT", 0.75)], ignore_index=True)
    write_dividends_tables(
        seed, coverage_frame(["AAPL", "MSFT"], set(), "2024-01-01", "2024-12-31"), str(db)
    )
    upsert_dividends_tables(
        _rows("AAPL", 0.25), coverage_frame(["AAPL"], set(), "2024-01-01", "2024-12-31"),
        ["AAPL"], str(db),
    )
    stored = _read(db, "dividends").set_index("ticker")["amount"].to_dict()
    assert stored == {"AAPL": 0.25, "MSFT": 0.75}


def test_upsert_dividends_tables_clears_rows_when_a_payer_stops_paying(tmp_path):
    """The reason the DELETE is driven by `tickers` and not by the frame's
    own content: a non-payer contributes no rows, so a content-driven delete
    would leave a former payer reporting income it no longer pays.
    """
    db = tmp_path / "d.duckdb"
    write_dividends_tables(
        _rows("AAPL"), coverage_frame(["AAPL"], set(), "2024-01-01", "2024-12-31"), str(db)
    )
    upsert_dividends_tables(
        pd.DataFrame(columns=DIVIDENDS_LONG_COLUMNS),
        coverage_frame(["AAPL"], set(), "2024-01-01", "2024-12-31"),
        ["AAPL"], str(db),
    )
    assert _read(db, "dividends").empty
    assert _read(db, "dividend_coverage")["ticker"].tolist() == ["AAPL"]


def test_upsert_dividends_tables_is_idempotent(tmp_path):
    db = tmp_path / "d.duckdb"
    cov = coverage_frame(["AAPL"], set(), "2024-01-01", "2024-12-31")
    for _ in range(3):
        upsert_dividends_tables(_rows(), cov, ["AAPL"], str(db))
    assert len(_read(db, "dividends")) == 1
    assert len(_read(db, "dividend_coverage")) == 1


def test_coverage_frame_omits_a_ticker_the_fetch_could_not_resolve(tmp_path):
    frame = coverage_frame(["AAPL", "BOGUS"], {"BOGUS"}, "2024-01-01", "2024-12-31")
    assert frame["ticker"].tolist() == ["AAPL"]


def test_tickers_with_dividend_data_is_empty_for_a_database_without_the_table(tmp_path):
    db = tmp_path / "none.duckdb"
    con = duckdb.connect(str(db))
    con.execute("CREATE TABLE prices (date DATE, ticker VARCHAR, close DOUBLE, adj_close DOUBLE)")
    con.close()
    assert tickers_with_dividend_data(["AAPL"], str(db)) == set()


def test_tickers_with_dividend_data_does_not_create_a_missing_database(tmp_path):
    db = tmp_path / "absent.duckdb"
    assert tickers_with_dividend_data(["AAPL"], str(db)) == set()
    assert not db.exists()


def test_load_dividend_figures_reports_a_pre_existing_database_as_unavailable(tmp_path):
    """A database built before this feature existed has prices for every
    ticker and no dividend table at all. It must report a named gap, never a
    confident zero for the whole universe.
    """
    db = tmp_path / "old.duckdb"
    con = duckdb.connect(str(db))
    con.execute("CREATE TABLE prices (date DATE, ticker VARCHAR, close DOUBLE, adj_close DOUBLE)")
    con.execute("INSERT INTO prices VALUES ('2024-04-29', 'AAPL', 173.50, 171.78)")
    con.close()
    yields, per_share, unavailable, _splits = load_dividend_figures(
        ["AAPL"], dt.date(2024, 4, 29), str(db)
    )
    assert yields == {}
    assert per_share == {}
    assert "no dividend data has been fetched" in unavailable["AAPL"]


def test_load_dividend_figures_divides_by_the_raw_close_not_the_adjusted_close(tmp_path):
    """`adj_close` is back-adjusted, so at any date before the fetch
    window's end it is below the price anyone could have paid. Dividing real
    cash by it overstates the yield - measurably so on the shipped database,
    where AAPL closes at 173.50 with an `adj_close` of 171.78.
    """
    db = tmp_path / "p.duckdb"
    con = duckdb.connect(str(db))
    con.execute("CREATE TABLE prices (date DATE, ticker VARCHAR, close DOUBLE, adj_close DOUBLE)")
    con.execute("INSERT INTO prices VALUES ('2024-04-29', 'AAPL', 200.00, 100.00)")
    con.close()
    write_dividends_tables(
        _rows("AAPL", 2.00), coverage_frame(["AAPL"], set(), "2023-05-01", "2024-04-29"), str(db)
    )
    yields, _per_share, _unavailable, _splits = load_dividend_figures(
        ["AAPL"], dt.date(2024, 4, 29), str(db)
    )
    assert yields["AAPL"] == pytest.approx(0.01)


# --------------------------------------------------------------------------
# Validators
# --------------------------------------------------------------------------


def test_validate_dividend_yield_accepts_a_plain_decimal():
    assert validate_dividend_yield(0.03, "--min-dividend-yield") == 0.03


def test_validate_dividend_yield_accepts_zero_as_a_deliberate_no_op():
    assert validate_dividend_yield(0, "--min-dividend-yield") == 0.0


def test_validate_dividend_yield_rejects_a_boolean():
    with pytest.raises(ValueError, match="must be a number"):
        validate_dividend_yield(True, "--min-dividend-yield")


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_validate_dividend_yield_rejects_non_finite_values(value):
    with pytest.raises(ValueError, match="finite"):
        validate_dividend_yield(value, "--min-dividend-yield")


def test_validate_dividend_yield_rejects_a_negative_floor():
    with pytest.raises(ValueError, match="must not be negative"):
        validate_dividend_yield(-0.01, "--min-dividend-yield")


def test_validate_dividend_yield_rejects_a_mistyped_percentage():
    with pytest.raises(ValueError, match="3% is 0.03"):
        validate_dividend_yield(3.0, "--min-dividend-yield")
    assert MAX_DIVIDEND_YIELD == 0.25


def test_validate_min_annual_dividend_divides_by_value():
    assert validate_min_annual_dividend(3000, "--min-annual-dividend", 100000.0) == pytest.approx(0.03)


@pytest.mark.parametrize("value", [0, -5.0])
def test_validate_min_annual_dividend_refuses_a_non_positive_value(value):
    with pytest.raises(ValueError, match="--value"):
        validate_min_annual_dividend(3000, "--min-annual-dividend", value)


def test_validate_min_annual_dividend_names_both_numbers_when_the_implied_yield_is_too_high():
    with pytest.raises(ValueError) as excinfo:
        validate_min_annual_dividend(50000, "--min-annual-dividend", 100000.0)
    message = str(excinfo.value)
    assert "50,000.00" in message and "0.5000" in message and "3% is 0.03" in message


# --------------------------------------------------------------------------
# Alignment, ceiling, feasibility
# --------------------------------------------------------------------------


YIELDS = {"STABLE": 0.01, "RISKY_A": 0.06, "RISKY_B": 0.02}


def test_dividend_yield_vector_is_ordered_by_the_tickers_not_the_dict():
    """The alignment guarantee. `EfficientFrontier` takes its variable order
    from `mu.index`, so a vector built in the dict's own insertion order
    would multiply the right numbers against the wrong tickers - a
    constraint satisfied on paper and meaningless in fact, with nothing to
    raise about it.
    """
    shuffled = {"RISKY_B": 0.02, "STABLE": 0.01, "RISKY_A": 0.06}
    vector = dividend_yield_vector(shuffled, ["STABLE", "RISKY_A", "RISKY_B"])
    assert np.array_equal(vector, np.array([0.01, 0.06, 0.02]))


def test_dividend_yield_vector_refuses_a_ticker_with_no_yield():
    with pytest.raises(DividendYieldUnavailableError) as excinfo:
        dividend_yield_vector({"STABLE": 0.01}, ["STABLE", "NEWCO"])
    assert "NEWCO" in str(excinfo.value)
    assert "a missing yield is not a zero yield" in str(excinfo.value)


def test_dividend_yield_vector_accepts_a_confirmed_zero_yield():
    vector = dividend_yield_vector({"BRK.B": 0.0}, ["BRK.B"])
    assert vector.tolist() == [0.0]


def test_dividend_yield_vector_refuses_a_non_finite_yield():
    with pytest.raises(DividendYieldUnavailableError, match="not a finite number"):
        dividend_yield_vector({"T": float("nan")}, ["T"])


def test_dividend_yield_ceiling_is_the_highest_single_yield_and_names_it():
    vector = dividend_yield_vector(YIELDS, list(YIELDS))
    assert dividend_yield_ceiling(vector, list(YIELDS)) == ("RISKY_A", 0.06)


def test_check_dividend_floor_feasible_passes_a_floor_at_exactly_the_ceiling():
    tickers = list(YIELDS)
    vector = dividend_yield_vector(YIELDS, tickers)
    check_dividend_floor_feasible(DividendFloor(0.06, "--min-dividend-yield"), vector, tickers)


def test_check_dividend_floor_feasible_refuses_a_floor_above_the_ceiling_naming_the_ticker():
    tickers = list(YIELDS)
    vector = dividend_yield_vector(YIELDS, tickers)
    with pytest.raises(DividendFloorError) as excinfo:
        check_dividend_floor_feasible(
            DividendFloor(0.07, "--min-dividend-yield"), vector, tickers
        )
    message = str(excinfo.value)
    assert "RISKY_A at 0.0600" in message
    assert "long-only and sum to 1" in message
    assert "Lower the floor to at most 0.0600" in message


def test_both_dividend_errors_are_value_errors():
    """The structural guarantee behind the interactive edit loop's revert:
    it catches `ValueError`, and an escaping exception would destroy live
    mode's only fetched snapshot.
    """
    assert issubclass(DividendFloorError, ValueError)
    assert issubclass(DividendYieldUnavailableError, ValueError)


def test_portfolio_dividend_yield_is_the_weighted_sum():
    vector = dividend_yield_vector(YIELDS, ["STABLE", "RISKY_A", "RISKY_B"])
    assert portfolio_dividend_yield([0.5, 0.25, 0.25], vector) == pytest.approx(0.025)


def test_covered_dividend_yield_reports_a_smaller_denominator_for_an_unknown_yield():
    value, covered, missing = covered_dividend_yield(
        {"STABLE": 0.5, "RISKY_A": 0.3, "NEWCO": 0.2}, {"STABLE": 0.01, "RISKY_A": 0.06}
    )
    assert missing == ("NEWCO",)
    assert covered == pytest.approx(0.8)
    assert value == pytest.approx((0.5 * 0.01 + 0.3 * 0.06) / 0.8)


# --------------------------------------------------------------------------
# DividendFigures
# --------------------------------------------------------------------------


def test_dividend_figures_uses_shares_times_dividends_per_share():
    figures = dividend_figures(
        {"T": 2500.0}, {"T": 63750.0}, {"T": 1.0124}
    )
    assert figures.annual_dividends["T"] == pytest.approx(2531.0)
    assert figures.dividend_yield == pytest.approx(2531.0 / 63750.0)
    assert figures.value_covered == pytest.approx(63750.0)


def test_dividend_figures_not_consulted_reports_the_no_figures_shape():
    from agentic_portfolio.optimizer.dividends import NO_DIVIDEND_FIGURES

    assert dividend_figures({"T": 1.0}, {"T": 10.0}, None) is NO_DIVIDEND_FIGURES


def test_dividend_figures_shrinks_the_denominator_for_a_holding_with_no_data():
    figures = dividend_figures(
        {"T": 100.0, "NEWCO": 10.0}, {"T": 2794.0, "NEWCO": 1000.0}, {"T": 1.11}
    )
    assert figures.value_covered == pytest.approx(2794.0)
    assert "NEWCO" in figures.unavailable
    assert figures.total_annual_dividends == pytest.approx(111.0)


def test_dividend_figures_totals_are_none_when_nothing_can_be_measured():
    figures = dividend_figures({"NEWCO": 10.0}, {"NEWCO": 1000.0}, {})
    assert figures.total_annual_dividends is None
    assert figures.dividend_yield is None
    assert figures.value_covered is None
    assert "NEWCO" in figures.unavailable


def test_dividend_figures_portfolio_yield_is_the_weighted_average_of_the_holding_yields():
    """One definition everywhere. The portfolio line must equal the weighted
    average of the per-holding lines printed above it, and must use the same
    `sum(w * y)` definition an optimized pool's report uses - otherwise the
    same ticker, or the same portfolio, states two different yields in two
    places.
    """
    figures = dividend_figures(
        {"KO": 200.0, "VZ": 300.0, "T": 500.0},
        {"KO": 11378.14, "VZ": 10771.88, "T": 7736.59},
        {"KO": 1.865, "VZ": 2.636, "T": 1.112},
        yields={"KO": 0.0307, "VZ": 0.0623, "T": 0.0635},
    )
    total_value = 11378.14 + 10771.88 + 7736.59
    expected = (
        11378.14 * 0.0307 + 10771.88 * 0.0623 + 7736.59 * 0.0635
    ) / total_value
    assert figures.dividend_yield == pytest.approx(expected)
    assert figures.yields == {"KO": 0.0307, "VZ": 0.0623, "T": 0.0635}


def test_dividend_figures_falls_back_to_cash_over_value_without_per_holding_yields():
    """The pre-yields shape stays available, so a caller that has only
    per-share cash still gets a portfolio figure rather than `None`.
    """
    figures = dividend_figures({"T": 100.0}, {"T": 2000.0}, {"T": 1.11})
    assert figures.yields == {}
    assert figures.dividend_yield == pytest.approx(111.0 / 2000.0)


# --------------------------------------------------------------------------
# Stock splits, and the restatement they explain
# --------------------------------------------------------------------------


def test_reshape_splits_long_keeps_only_the_rows_that_carry_a_split():
    """yfinance zero-fills `Stock Splits` on every trading day, and a NaN
    appears wherever another ticker in the batch introduced a date this one
    has no data for. Neither is a split.
    """
    raw = _raw_actions_fixture()
    raw[("Stock Splits", "PAYER")] = [0.0, 4.0, 0.0]
    raw[("Stock Splits", "NAN_ROW")] = [float("nan"), 0.0, 0.0]
    long_df = reshape_splits_long(raw, IDENTITY)
    assert list(long_df.columns) == SPLITS_LONG_COLUMNS
    assert len(long_df) == 1
    assert long_df.iloc[0]["ticker"] == "PAYER"
    assert long_df.iloc[0]["ratio"] == 4.0


def test_reshape_splits_long_gives_a_never_split_ticker_no_rows():
    long_df = reshape_splits_long(_raw_actions_fixture(), IDENTITY)
    assert long_df.empty
    assert list(long_df.columns) == SPLITS_LONG_COLUMNS


def test_reshape_splits_long_tolerates_a_batch_with_no_splits_column():
    """Unlike a missing `Dividends` column, this is not a broken contract:
    splits are explanatory, so their absence costs a sentence rather than a
    number, and a wholly-failed batch legitimately has no such column.
    """
    raw = _raw_actions_fixture().drop(columns=["Stock Splits"], level=0)
    result = reshape_splits_long(raw, IDENTITY)
    assert result.empty
    assert list(result.columns) == SPLITS_LONG_COLUMNS


def test_announced_amount_recovers_the_declared_figure_across_a_split():
    """The real 9984.T case: SoftBank declared 22 yen for its 2025-09-29
    ex-date, split 4:1 on 2025-12-29, and Yahoo therefore reports that
    payment as 5.5 on the current share basis. Multiplying back by the
    cumulative ratio recovers the 22 a reader would find in the company's
    announcement - which is the whole point of the report line this feeds.
    """
    splits = pd.Series([4.0], index=pd.to_datetime(["2025-12-29"]))
    assert announced_amount(5.5, dt.date(2025, 9, 29), splits) == pytest.approx(22.0)
    # A payment after the split needs no restatement.
    assert announced_amount(5.5, dt.date(2026, 3, 30), splits) == pytest.approx(5.5)


def test_announced_amount_is_a_no_op_for_a_ticker_that_never_split():
    assert announced_amount(1.11, dt.date(2026, 3, 30), None) == pytest.approx(1.11)
    assert announced_amount(1.11, dt.date(2026, 3, 30), pd.Series(dtype=float)) == pytest.approx(1.11)


def test_upsert_dividends_tables_replaces_only_the_named_tickers_splits(tmp_path):
    db = tmp_path / "d.duckdb"
    splits = pd.DataFrame(
        {
            "ex_date": pd.to_datetime(["2025-12-29", "2024-06-10"]),
            "ticker": ["9984.T", "NVDA"],
            "ratio": [4.0, 10.0],
        }
    )
    write_dividends_tables(
        _rows("9984.T"),
        coverage_frame(["9984.T", "NVDA"], set(), "2021-01-01", "2026-09-08"),
        str(db),
        splits_df=splits,
    )
    upsert_dividends_tables(
        _rows("9984.T"),
        coverage_frame(["9984.T"], set(), "2021-01-01", "2026-09-08"),
        ["9984.T"],
        str(db),
        splits_df=pd.DataFrame(columns=SPLITS_LONG_COLUMNS),
    )
    stored = _read(db, "splits")
    assert stored["ticker"].tolist() == ["NVDA"]


def test_write_dividends_tables_creates_an_empty_splits_table_when_given_none(tmp_path):
    """So no reader ever has to tell "this ticker never split" apart from
    "this database predates the splits table".
    """
    db = tmp_path / "d.duckdb"
    write_dividends_tables(
        _rows(), coverage_frame(["AAPL"], set(), "2024-01-01", "2024-12-31"), str(db)
    )
    assert _read(db, "splits").empty
    assert has_splits_table(str(db)) is True


def test_has_splits_table_is_false_before_the_migration_and_for_a_missing_file(tmp_path):
    db = tmp_path / "old.duckdb"
    con = duckdb.connect(str(db))
    con.execute("CREATE TABLE dividends (ex_date DATE, ticker VARCHAR, amount DOUBLE)")
    con.close()
    assert has_splits_table(str(db)) is False

    absent = tmp_path / "absent.duckdb"
    assert has_splits_table(str(absent)) is False
    assert not absent.exists()


def test_load_dividend_figures_reports_a_split_inside_the_window(tmp_path):
    """End to end over the real 9984.T numbers: two stored payments of 5.5
    and a 4:1 split between them, which the report needs paired together to
    explain why 22 became 5.5.
    """
    db = tmp_path / "h.duckdb"
    con = duckdb.connect(str(db))
    con.execute("CREATE TABLE prices (date DATE, ticker VARCHAR, close DOUBLE, adj_close DOUBLE)")
    con.execute("INSERT INTO prices VALUES ('2026-09-08', '9984.T', 6620.0, 6620.0)")
    con.close()
    dividends = pd.DataFrame(
        {
            "ex_date": pd.to_datetime(["2025-09-29", "2026-03-30"]),
            "ticker": ["9984.T", "9984.T"],
            "amount": [5.5, 5.5],
        }
    )
    splits = pd.DataFrame(
        {"ex_date": pd.to_datetime(["2025-12-29"]), "ticker": ["9984.T"], "ratio": [4.0]}
    )
    write_dividends_tables(
        dividends,
        coverage_frame(["9984.T"], set(), "2021-01-01", "2026-09-13"),
        str(db),
        splits_df=splits,
    )

    yields, per_share, _unavailable, in_window = load_dividend_figures(
        ["9984.T"], dt.date(2026, 9, 8), str(db)
    )
    assert per_share["9984.T"] == pytest.approx(11.0)
    assert yields["9984.T"] == pytest.approx(11.0 / 6620.0)

    context = in_window["9984.T"]
    assert context.splits == [(dt.date(2025, 12, 29), 4.0)]
    assert context.payments == [(dt.date(2025, 9, 29), 5.5), (dt.date(2026, 3, 30), 5.5)]


def test_load_dividend_figures_reports_no_splits_for_a_ticker_that_never_split(tmp_path):
    db = tmp_path / "h.duckdb"
    con = duckdb.connect(str(db))
    con.execute("CREATE TABLE prices (date DATE, ticker VARCHAR, close DOUBLE, adj_close DOUBLE)")
    con.execute("INSERT INTO prices VALUES ('2026-09-08', 'KO', 60.68, 60.68)")
    con.close()
    write_dividends_tables(
        _rows("KO", 0.485),
        coverage_frame(["KO"], set(), "2024-01-01", "2026-09-13"),
        str(db),
    )
    _y, _p, _u, in_window = load_dividend_figures(["KO"], dt.date(2026, 9, 8), str(db))
    assert in_window == {}


def test_load_dividend_figures_ignores_a_split_after_the_as_of_date(tmp_path):
    db = tmp_path / "h.duckdb"
    con = duckdb.connect(str(db))
    con.execute("CREATE TABLE prices (date DATE, ticker VARCHAR, close DOUBLE, adj_close DOUBLE)")
    con.execute("INSERT INTO prices VALUES ('2026-03-01', 'X', 100.0, 100.0)")
    con.close()
    write_dividends_tables(
        _rows("X", 1.0),
        coverage_frame(["X"], set(), "2024-01-01", "2026-09-13"),
        str(db),
        splits_df=pd.DataFrame(
            {"ex_date": pd.to_datetime(["2026-08-01"]), "ticker": ["X"], "ratio": [2.0]}
        ),
    )
    _y, _p, _u, in_window = load_dividend_figures(["X"], dt.date(2026, 3, 1), str(db))
    assert in_window == {}


# ---------------------------------------------------------------------------
# `dividend_unresolved`: why a ticker has no coverage
#
# The table exists because "no dividend data has been fetched for it" was
# true of every uncovered ticker and useful for none. AVB, EA, EQR and LEG
# have no coverage in the shipped database because Yahoo no longer serves
# their 2015-2024 window at all - so the build the old sentence recommends
# can never succeed, and following it costs a full 525-ticker fetch. These
# tests pin the distinction between that case and a transient miss, and
# never touch the network: `probe` is injected everywhere.
# ---------------------------------------------------------------------------

_WINDOW = ("2015-01-01", "2024-04-30")


def _probe(mapping):
    """A `probe_served_window` stand-in reading from `mapping`."""
    return lambda symbol: mapping.get(symbol)


def test_unresolved_frame_names_a_window_the_source_no_longer_serves():
    """The AVB/EA/EQR/LEG case, reproduced: the symbol resolves, but the
    only history Yahoo serves for it postdates the requested window
    entirely. Re-running the build cannot fix that, and the reason has to
    say so rather than advise it.
    """
    frame = unresolved_frame(
        ["AVB"],
        {"AVB": "AVB"},
        *_WINDOW,
        probe=_probe({"AVB": (dt.date(2026, 7, 17), dt.date(2026, 8, 24))}),
    )
    reason = frame.set_index("ticker")["reason"]["AVB"]
    assert "no longer serves" in reason
    assert "2026-07-17..2026-08-24" in reason
    assert "2015-01-01..2024-04-30" in reason
    assert "cannot fix this" in reason
    # The advice that does not work must not appear.
    assert "may fix it" not in reason


def test_unresolved_frame_calls_a_miss_transient_when_the_served_range_overlaps():
    """The opposite verdict, and the reason the probe is worth its network
    call: a ticker Yahoo does serve over the requested window failed for
    some other reason, and re-running really may fix it.
    """
    frame = unresolved_frame(
        ["BOXX"],
        {"BOXX": "BOXX"},
        *_WINDOW,
        probe=_probe({"BOXX": (dt.date(2014, 1, 2), dt.date(2026, 9, 4))}),
    )
    reason = frame.set_index("ticker")["reason"]["BOXX"]
    assert "transient" in reason
    assert "may fix it" in reason
    assert "no longer serves" not in reason


def test_unresolved_frame_admits_it_does_not_know_when_the_probe_finds_nothing():
    """A symbol Yahoo serves nothing at all for - a typo, or a ticker that
    never existed. Claiming either verdict would be a guess, so the reason
    says the question is open.
    """
    frame = unresolved_frame(["BOGUS"], {"BOGUS": "BOGUS"}, *_WINDOW, probe=_probe({}))
    reason = frame.set_index("ticker")["reason"]["BOGUS"]
    assert "not known whether" in reason
    assert "cannot fix this" not in reason


def test_unresolved_frame_probes_the_yfinance_symbol_not_the_project_ticker():
    """A non-US ticker is fetched under its yfinance symbol, so that is what
    a probe of "what does the source serve" has to ask about - asking about
    the project's own ticker string would probe a symbol nobody fetched.
    """
    seen = []

    def probe(symbol):
        seen.append(symbol)
        return None

    unresolved_frame(["8035.T"], {"8035.T": "8035.T"}, *_WINDOW, probe=probe)
    assert seen == ["8035.T"]


def test_unresolved_frame_is_empty_and_typed_for_a_fetch_that_missed_nothing():
    frame = unresolved_frame([], {}, *_WINDOW, probe=_probe({}))
    assert frame.empty
    assert list(frame.columns) == DIVIDEND_UNRESOLVED_COLUMNS


def test_a_caller_refused_ticker_still_gets_a_row():
    """`build_dividends_for_tickers`' `unresolved` argument names tickers
    whose prices or currency already failed. They are not probed - the fetch
    was never going to work - but they still need a row, because a ticker
    with neither a coverage row nor an unresolved row is the silent gap.
    """
    frame = unresolved_frame_from_caller(["BOGUS"])
    assert frame["ticker"].tolist() == ["BOGUS"]
    assert "already known to be unusable" in frame["reason"][0]


def test_write_dividends_tables_always_creates_the_unresolved_table(tmp_path):
    """Same discipline as `splits`: the table exists once a build has run,
    so no reader has to distinguish "everything resolved" from "no table".
    """
    db = tmp_path / "d.duckdb"
    write_dividends_tables(
        _rows(), coverage_frame(["AAPL"], set(), *_WINDOW), str(db)
    )
    assert _read(db, "dividend_unresolved").empty


def test_upsert_clears_an_unresolved_row_when_the_ticker_starts_resolving(tmp_path):
    """The keyed delete is what makes a recorded reason self-correcting. A
    transient failure leaves a row saying so; the next fetch that ASKS about
    that ticker must clear it rather than leave a stale explanation sitting
    beside good coverage.
    """
    db = tmp_path / "d.duckdb"
    upsert_dividends_tables(
        pd.DataFrame(columns=DIVIDENDS_LONG_COLUMNS),
        coverage_frame(["AAPL"], {"AAPL"}, *_WINDOW),
        ["AAPL"],
        str(db),
        unresolved_df=unresolved_frame(
            ["AAPL"], {"AAPL": "AAPL"}, *_WINDOW, probe=_probe({})
        ),
    )
    assert _read(db, "dividend_unresolved")["ticker"].tolist() == ["AAPL"]

    upsert_dividends_tables(
        _rows("AAPL"), coverage_frame(["AAPL"], set(), *_WINDOW), ["AAPL"], str(db)
    )
    assert _read(db, "dividend_unresolved").empty
    assert _read(db, "dividend_coverage")["ticker"].tolist() == ["AAPL"]


def test_a_ticker_never_holds_coverage_and_an_unresolved_row_at_once(tmp_path):
    """The invariant that makes either table answerable on its own:
    `coverage_frame` and `unresolved_frame` split one requested list, and
    one upsert deletes from both before inserting.
    """
    db = tmp_path / "d.duckdb"
    upsert_dividends_tables(
        _rows("AAPL"),
        coverage_frame(["AAPL", "AVB"], {"AVB"}, *_WINDOW),
        ["AAPL", "AVB"],
        str(db),
        unresolved_df=unresolved_frame(
            ["AVB"],
            {"AVB": "AVB"},
            *_WINDOW,
            probe=_probe({"AVB": (dt.date(2026, 7, 17), dt.date(2026, 8, 24))}),
        ),
    )
    covered = set(_read(db, "dividend_coverage")["ticker"])
    unresolved = set(_read(db, "dividend_unresolved")["ticker"])
    assert covered == {"AAPL"}
    assert unresolved == {"AVB"}
    assert covered & unresolved == set()


def test_dividend_unresolved_reasons_is_empty_for_a_database_without_the_table(tmp_path):
    """An older cache degrades to the generic sentence instead of raising -
    the same rule `tickers_with_dividend_data` follows.
    """
    db = tmp_path / "old.duckdb"
    con = duckdb.connect(str(db))
    con.execute("CREATE TABLE prices (date DATE, ticker VARCHAR, close DOUBLE, adj_close DOUBLE)")
    con.close()
    assert dividend_unresolved_reasons(["AVB"], str(db)) == {}


def test_dividend_unresolved_reasons_does_not_create_a_missing_database(tmp_path):
    db = tmp_path / "absent.duckdb"
    assert dividend_unresolved_reasons(["AVB"], str(db)) == {}
    assert not db.exists()


def test_trailing_dividend_yields_prefers_a_recorded_reason():
    """The recorded reason replaces the generic sentence - and the ticker is
    still ABSENT from `yields`. A reason explains a missing yield; it never
    supplies one.
    """
    yields, unavailable = trailing_dividend_yields(
        {"AVB": 0.0},
        pd.Series({"AVB": 191.02}),
        set(),
        {"AVB": "yfinance no longer serves this ticker's history"},
    )
    assert "AVB" not in yields
    assert unavailable["AVB"] == "yfinance no longer serves this ticker's history"


def test_trailing_dividend_yields_falls_back_to_the_generic_sentence():
    yields, unavailable = trailing_dividend_yields(
        {"AVB": 0.0}, pd.Series({"AVB": 191.02}), set(), {"OTHER": "some reason"}
    )
    assert "AVB" not in yields
    assert "no dividend data has been fetched" in unavailable["AVB"]


def test_load_dividend_figures_reports_the_recorded_reason(tmp_path):
    """End to end: a ticker with prices, no coverage and a recorded reason
    reports that reason - which is what the AVB/EA/EQR/LEG rows in
    `data/portfolio.duckdb` will do once they are recorded.
    """
    db = tmp_path / "p.duckdb"
    con = duckdb.connect(str(db))
    con.execute("CREATE TABLE prices (date DATE, ticker VARCHAR, close DOUBLE, adj_close DOUBLE)")
    con.execute("INSERT INTO prices VALUES ('2024-04-29', 'AVB', 191.02, 160.00)")
    con.close()
    upsert_dividends_tables(
        pd.DataFrame(columns=DIVIDENDS_LONG_COLUMNS),
        coverage_frame(["AVB"], {"AVB"}, *_WINDOW),
        ["AVB"],
        str(db),
        unresolved_df=unresolved_frame(
            ["AVB"],
            {"AVB": "AVB"},
            *_WINDOW,
            probe=_probe({"AVB": (dt.date(2026, 7, 17), dt.date(2026, 8, 24))}),
        ),
    )
    yields, per_share, unavailable, _splits = load_dividend_figures(
        ["AVB"], dt.date(2024, 4, 29), str(db)
    )
    assert yields == {}
    assert per_share == {}
    assert "no longer serves" in unavailable["AVB"]
    assert "2026-07-17..2026-08-24" in unavailable["AVB"]


def test_dividend_yield_vector_quotes_a_recorded_reason_instead_of_advising_a_rebuild():
    """The refusal a floored pool containing LEG produces. "Build its
    dividend history" is the one thing that cannot work here, so with a
    reason for every missing ticker it is replaced by the reasons.
    """
    with pytest.raises(DividendYieldUnavailableError) as excinfo:
        dividend_yield_vector(
            {"T": 0.0653},
            ["T", "LEG"],
            {"LEG": "yfinance no longer serves this ticker's history for 2015-01-01..2024-04-30"},
        )
    message = str(excinfo.value)
    assert "no longer serves" in message
    assert "build its dividend history" not in message
    assert "Remove it from the pool" in message


def test_dividend_yield_vector_keeps_the_rebuild_advice_when_a_reason_is_missing():
    """Partial explanation keeps the advice, because it is still actionable
    for the ticker that has no recorded reason.
    """
    with pytest.raises(DividendYieldUnavailableError) as excinfo:
        dividend_yield_vector(
            {"T": 0.0653}, ["T", "LEG", "NEW"], {"LEG": "yfinance no longer serves it"}
        )
    assert "build its dividend history" in str(excinfo.value)


def test_dividend_yield_vector_without_reasons_is_unchanged():
    with pytest.raises(DividendYieldUnavailableError) as excinfo:
        dividend_yield_vector({"T": 0.0653}, ["T", "LEG"])
    assert "build its dividend history" in str(excinfo.value)
