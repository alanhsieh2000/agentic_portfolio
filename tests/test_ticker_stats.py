"""Tests for `src/optimizer/ticker_stats.py`: one candidate ticker's annual
return, annual volatility, Sharpe ratio and trailing dividend yield.

Per `AGENTS.md` these are hermetic. The arithmetic runs against tiny fixture
DuckDB files built in `tmp_path` rather than the real
`data/portfolio.duckdb`, and nothing here touches Yahoo Finance - this
module performs no network I/O at all, which is what makes that possible
without any mocking.

The load-bearing test is `test_a_tickers_figures_equal_its_own_benchmarks`.
The whole point of the second half of a ticker summary is that its figures
can be read beside the benchmark's and the portfolio's, which is only
legitimate if the same series measured either way gives the same numbers.
"""

from datetime import date

import duckdb
import pandas as pd
import pytest

from src.optimizer.benchmark import BenchmarkSource, benchmark_stats_for_window
from src.optimizer.ticker_stats import (
    DEFAULT_LOOKBACK_MONTHS,
    TICKER_MIN_MONTHS,
    ticker_stats,
)

AS_OF = date(2026, 1, 1)


def _monthly_returns(n: int, ticker: str, start: str = "2019-01-01", shift: int = 0) -> pd.Series:
    """`n` deterministic, non-degenerate monthly returns on the month-start
    grid - the same fixture shape `tests/test_holdings.py` and
    `tests/test_benchmark.py` use."""
    idx = pd.date_range(start, periods=n, freq="MS", name="rebalance_date")
    values = [
        0.01 + (0.03 if (i + shift) % 2 == 0 else -0.02) + 0.001 * ((i + shift) % 5)
        for i in range(n)
    ]
    return pd.Series(values, index=idx, name=ticker)


def _make_db(
    db_path: str,
    series: list[pd.Series],
    prices: dict[str, float] | None = None,
    dividends: dict[str, list[tuple[date, float]]] | None = None,
    coverage: dict[str, tuple[date, date]] | None = None,
    unresolved: dict[str, str] | None = None,
) -> None:
    """A fixture database in the shape `validate_and_ingest_tickers` leaves
    behind: `returns`, `prices`, `splits`, `dividends`, `dividend_coverage`
    and `dividend_unresolved`.

    `dividend_coverage` is what separates a confirmed non-payer from a
    ticker whose data is missing, so a fixture that omits it deliberately
    exercises the missing case.
    """
    con = duckdb.connect(db_path)
    try:
        con.execute(
            "CREATE TABLE returns (rebalance_date DATE, ticker VARCHAR, monthly_return DOUBLE)"
        )
        con.execute(
            "CREATE TABLE prices (date DATE, ticker VARCHAR, close DOUBLE, adj_close DOUBLE)"
        )
        con.execute("CREATE TABLE splits (ex_date DATE, ticker VARCHAR, ratio DOUBLE)")
        con.execute("CREATE TABLE dividends (ex_date DATE, ticker VARCHAR, amount DOUBLE)")
        con.execute(
            "CREATE TABLE dividend_coverage (ticker VARCHAR, fetch_start DATE, fetch_end DATE)"
        )
        con.execute("CREATE TABLE dividend_unresolved (ticker VARCHAR, reason VARCHAR)")
        for one in series:
            # One multi-row INSERT rather than `executemany`, which is about
            # five times slower for these row counts and buys nothing.
            values = ", ".join(
                f"('{ts.date()}', '{one.name}', {float(v)!r})" for ts, v in one.items()
            )
            if values:
                con.execute(f"INSERT INTO returns VALUES {values}")
        for ticker, price in (prices or {}).items():
            con.execute("INSERT INTO prices VALUES (?, ?, ?, ?)", [date(2025, 12, 1), ticker, price, price])
        for ticker, payments in (dividends or {}).items():
            values = ", ".join(f"('{d}', '{ticker}', {a!r})" for d, a in payments)
            if values:
                con.execute(f"INSERT INTO dividends VALUES {values}")
        for ticker, (start, end) in (coverage or {}).items():
            con.execute("INSERT INTO dividend_coverage VALUES (?, ?, ?)", [ticker, start, end])
        for ticker, reason in (unresolved or {}).items():
            con.execute("INSERT INTO dividend_unresolved VALUES (?, ?)", [ticker, reason])
    finally:
        con.close()


def test_a_ticker_with_full_history_reports_all_three_figures(tmp_path):
    db = str(tmp_path / "p.duckdb")
    _make_db(db, [_monthly_returns(72, "AMLP")], prices={"AMLP": 50.0})

    stats = ticker_stats("AMLP", AS_OF, db, currency="USD", risk_free_rate=0.02)

    assert stats.unavailable_reason is None
    assert stats.ticker == "AMLP"
    assert stats.currency == "USD"
    assert stats.annual_return is not None
    assert stats.annual_volatility > 0
    assert stats.sharpe == pytest.approx(
        (stats.annual_return - 0.02) / stats.annual_volatility
    )
    assert stats.risk_free_rate == 0.02


def test_the_window_is_the_trailing_lookback_and_reports_its_real_dates(tmp_path):
    """72 months are stored; the default window is 60, so the figures must
    describe the most recent 60 and say which months those were."""
    db = str(tmp_path / "p.duckdb")
    _make_db(db, [_monthly_returns(72, "AMLP", start="2019-01-01")])

    stats = ticker_stats("AMLP", AS_OF, db)

    assert stats.window_months == DEFAULT_LOOKBACK_MONTHS
    assert stats.window_start == date(2020, 1, 1)
    assert stats.window_end == date(2024, 12, 1)


def test_a_window_shorter_than_the_lookback_reports_the_real_start_and_end(tmp_path):
    """Real history is frequently shorter than the target, and this
    project's rule is that a figure prints the window that produced it -
    never the constant that was asked for."""
    db = str(tmp_path / "p.duckdb")
    _make_db(db, [_monthly_returns(30, "NEWCO", start="2022-03-01")])

    stats = ticker_stats("NEWCO", AS_OF, db)

    assert stats.window_months == 30
    assert stats.window_start == date(2022, 3, 1)
    assert stats.window_end == date(2024, 8, 1)
    assert stats.annual_return is not None


def test_a_ticker_below_the_minimum_history_declines_and_names_both_counts(tmp_path):
    db = str(tmp_path / "p.duckdb")
    _make_db(db, [_monthly_returns(12, "NEWCO")])

    stats = ticker_stats("NEWCO", AS_OF, db)

    assert stats.annual_return is None
    assert stats.annual_volatility is None
    assert stats.sharpe is None
    assert stats.window_months == 0
    assert "12 month(s) of history for NEWCO" in stats.unavailable_reason
    assert f"below the {TICKER_MIN_MONTHS} required" in stats.unavailable_reason


def test_a_ticker_absent_from_the_returns_table_declines_with_a_reason(tmp_path):
    db = str(tmp_path / "p.duckdb")
    _make_db(db, [_monthly_returns(72, "AMLP")])

    stats = ticker_stats("MISSING", AS_OF, db)

    assert stats.annual_return is None
    assert stats.unavailable_reason == (
        f"no monthly returns stored for MISSING on or before {AS_OF}"
    )


def test_a_database_that_does_not_exist_declines_rather_than_raising(tmp_path):
    """Asking about a database must never create one, and a summary is not
    worth losing an in-progress candidate pool over."""
    stats = ticker_stats("AMLP", AS_OF, str(tmp_path / "absent.duckdb"))

    assert stats.unavailable_reason is not None
    assert not (tmp_path / "absent.duckdb").exists()


def test_a_flat_return_series_declines_rather_than_reporting_a_vast_sharpe(tmp_path):
    """`ledoit_wolf()` returns about 6e-18 rather than 0.0 for a genuinely
    constant series, so a plain `volatility > 0` guard would let through a
    Sharpe ratio around 1.8e16 - a number that would print as the best
    investment ever measured."""
    db = str(tmp_path / "p.duckdb")
    idx = pd.date_range("2020-01-01", periods=60, freq="MS", name="rebalance_date")
    _make_db(db, [pd.Series([0.01] * 60, index=idx, name="FLAT")])

    stats = ticker_stats("FLAT", AS_OF, db)

    assert stats.sharpe is None
    assert "no usable return/volatility estimate" in stats.unavailable_reason


def test_a_ticker_with_dividend_coverage_reports_a_yield(tmp_path):
    db = str(tmp_path / "p.duckdb")
    as_of = date(2025, 12, 15)
    _make_db(
        db,
        [_monthly_returns(60, "AMLP", start="2020-01-01")],
        prices={"AMLP": 50.0},
        dividends={
            "AMLP": [
                (date(2025, 3, 10), 1.0),
                (date(2025, 6, 10), 1.0),
                (date(2025, 9, 10), 1.0),
                (date(2025, 12, 10), 1.0),
            ]
        },
        coverage={"AMLP": (date(2015, 1, 1), as_of)},
    )

    stats = ticker_stats("AMLP", as_of, db, dividend_lookback_months=12)

    assert stats.dividend_yield == pytest.approx(4.0 / 50.0)
    assert stats.dividend_lookback_months == 12
    assert stats.dividend_unavailable_reason is None


def test_a_ticker_without_dividend_coverage_reports_a_reason_and_not_a_zero(tmp_path):
    """A missing yield is not a zero yield. `AVB`, `EA`, `EQR` and `LEG` sit
    in this project's real `dividend_unresolved` table for exactly this
    reason, and collapsing the two would be the one silent wrong answer
    this feature could produce."""
    db = str(tmp_path / "p.duckdb")
    _make_db(
        db,
        [_monthly_returns(60, "AVB", start="2020-01-01")],
        prices={"AVB": 180.0},
        unresolved={"AVB": "Yahoo Finance no longer serves this window for AVB"},
    )

    stats = ticker_stats("AVB", AS_OF, db)

    assert stats.dividend_yield is None
    assert stats.dividend_lookback_months is None
    assert stats.dividend_unavailable_reason == (
        "Yahoo Finance no longer serves this window for AVB"
    )
    # The returns figures are unaffected: the two groups fail independently.
    assert stats.annual_return is not None
    assert stats.unavailable_reason is None


def test_a_confirmed_non_payer_reports_a_zero_yield_rather_than_a_reason(tmp_path):
    db = str(tmp_path / "p.duckdb")
    _make_db(
        db,
        [_monthly_returns(60, "NOPAY", start="2020-01-01")],
        prices={"NOPAY": 100.0},
        coverage={"NOPAY": (date(2015, 1, 1), AS_OF)},
    )

    stats = ticker_stats("NOPAY", AS_OF, db)

    assert stats.dividend_yield == 0.0
    assert stats.dividend_unavailable_reason is None


def test_a_database_with_no_dividend_tables_reports_a_reason_not_a_crash(tmp_path):
    """An older cache built before dividends existed must degrade into a
    named gap rather than a traceback."""
    db = str(tmp_path / "p.duckdb")
    con = duckdb.connect(db)
    try:
        con.execute(
            "CREATE TABLE returns (rebalance_date DATE, ticker VARCHAR, monthly_return DOUBLE)"
        )
        one = _monthly_returns(60, "AMLP", start="2020-01-01")
        values = ", ".join(f"('{ts.date()}', 'AMLP', {float(v)!r})" for ts, v in one.items())
        con.execute(f"INSERT INTO returns VALUES {values}")
    finally:
        con.close()

    stats = ticker_stats("AMLP", AS_OF, db)

    assert stats.annual_return is not None
    assert stats.dividend_yield is None
    assert stats.dividend_unavailable_reason is not None


def test_a_tickers_figures_equal_its_own_benchmarks(tmp_path):
    """The load-bearing test. A ticker summary's figures are printed to be
    read beside the benchmark's and the portfolio's, which is only
    legitimate if the same series measured either way gives the same
    numbers - so this pins that `ticker_stats` and
    `benchmark_stats_for_window` share their estimators rather than merely
    resembling each other.
    """
    db = str(tmp_path / "p.duckdb")
    series = _monthly_returns(60, "SPY", start="2020-01-01")
    _make_db(db, [series])

    stats = ticker_stats("SPY", AS_OF, db, risk_free_rate=0.02)
    as_benchmark = benchmark_stats_for_window(
        BenchmarkSource("SPY", "USD", series),
        stats.window_start,
        stats.window_end,
        risk_free_rate=0.02,
    )

    assert stats.annual_return == pytest.approx(as_benchmark.annual_return)
    assert stats.annual_volatility == pytest.approx(as_benchmark.annual_volatility)
    assert stats.sharpe == pytest.approx(as_benchmark.sharpe)
    assert stats.window_months == as_benchmark.window_months


def test_the_minimum_history_bar_matches_the_optimizers_own(tmp_path):
    """`TICKER_MIN_MONTHS` is deliberately the same 24 months
    `apply_min_history_rule` and `BENCHMARK_MIN_MONTHS` require, so a
    candidate whose summary promised figures cannot then be silently dropped
    from the optimizer for having too little history."""
    from src.optimizer.benchmark import BENCHMARK_MIN_MONTHS

    assert TICKER_MIN_MONTHS == BENCHMARK_MIN_MONTHS == 24
