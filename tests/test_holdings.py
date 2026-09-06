"""Tests for src/optimizer/holdings.py and src/flow/interactive.py's
`prepare_holdings`: turning saved share counts into value weights, the
three figures behind them, and the exclusion rules for a holding that
cannot be measured.

Per AGENTS.md's testing guidance these are hermetic. The arithmetic runs
against tiny fixture DuckDB files built in `tmp_path` rather than the real
`data/portfolio.duckdb`, and every network seam
(`validate_and_ingest_tickers`, `build_scratch_snapshot`) is monkeypatched
on the importing module's own symbol, so nothing here depends on the
dataset build or touches Yahoo Finance.

The load-bearing test is
`test_a_one_holding_portfolio_equals_its_own_benchmark`: the whole point of
this feature is printing a held portfolio's figures beneath the benchmark's
and comparing them, which is only legitimate if a single-asset portfolio
measured either way gives the same three numbers.
"""

from contextlib import contextmanager
from datetime import date

import duckdb
import pandas as pd
import pytest

from src.optimizer.benchmark import benchmark_stats_for_window, BenchmarkSource
from src.optimizer.holdings import (
    HOLDINGS_MIN_MONTHS,
    holdings_stats,
    stored_month_counts,
    weights_from_positions,
)
from src.flow.interactive import (
    measure_holdings,
    open_holdings_session,
    prepare_holdings,
)
from src.optimizer.portfolio import compute_weights_and_stats


def _monthly_returns(n: int, ticker: str, start: str = "2020-01-01", shift: int = 0) -> pd.Series:
    """`n` deterministic, non-degenerate monthly returns on the month-start
    grid. `shift` decorrelates one ticker's series from another's without
    making either degenerate, the same trick `tests/test_benchmark.py`'s own
    fixture uses.
    """
    idx = pd.date_range(start, periods=n, freq="MS", name="rebalance_date")
    values = [
        0.01 + (0.03 if (i + shift) % 2 == 0 else -0.02) + 0.001 * ((i + shift) % 5)
        for i in range(n)
    ]
    return pd.Series(values, index=idx, name=ticker)


def _make_db(db_path: str, series: list[pd.Series], prices: dict[str, float]) -> None:
    """A fixture database holding `returns` rows for each series and one
    `prices` row per ticker in `prices`, on a date every test's `as_of`
    is after.
    """
    con = duckdb.connect(db_path)
    try:
        con.execute("CREATE TABLE returns (rebalance_date DATE, ticker VARCHAR, monthly_return DOUBLE)")
        con.execute("CREATE TABLE prices (date DATE, ticker VARCHAR, close DOUBLE, adj_close DOUBLE)")
        for one in series:
            con.executemany(
                "INSERT INTO returns VALUES (?, ?, ?)",
                [(ts.date(), str(one.name), float(v)) for ts, v in one.items()],
            )
        if prices:
            con.executemany(
                "INSERT INTO prices VALUES (?, ?, ?, ?)",
                [(date(2019, 12, 2), ticker, price, price) for ticker, price in prices.items()],
            )
    finally:
        con.close()


AS_OF = date(2026, 1, 1)


@contextmanager
def _fake_scratch(path: str):
    """Stand in for `build_scratch_snapshot`, yielding a fixture database
    that already exists instead of creating and deleting a real one.
    """
    yield path


# --- weights_from_positions -------------------------------------------------


def test_weights_are_share_count_times_price_normalized():
    prices = pd.Series({"CHEAP": 2.0, "PRICEY": 400.0})
    weights, market_values, total_value = weights_from_positions(
        {"CHEAP": 100.0, "PRICEY": 2.0}, prices
    )

    assert market_values == {"CHEAP": 200.0, "PRICEY": 800.0}
    assert total_value == 1000.0
    assert weights == {"CHEAP": 0.2, "PRICEY": 0.8}


def test_a_holding_with_no_price_is_left_out_of_every_figure():
    prices = pd.Series({"SPY": 100.0, "GHOST": float("nan")})
    weights, market_values, total_value = weights_from_positions(
        {"SPY": 10.0, "GHOST": 10.0}, prices
    )

    assert "GHOST" not in market_values and "GHOST" not in weights
    assert total_value == 1000.0
    assert weights == {"SPY": 1.0}


def test_fractional_share_counts_are_weighted_like_any_other():
    prices = pd.Series({"A": 100.0, "B": 100.0})
    weights, _, total_value = weights_from_positions({"A": 0.5, "B": 1.5}, prices)

    assert total_value == 200.0
    assert weights == {"A": 0.25, "B": 0.75}


# --- holdings_stats ---------------------------------------------------------


def test_a_one_holding_portfolio_equals_its_own_benchmark(tmp_path):
    """The comparability guarantee. A portfolio holding one ticker is that
    ticker, so the three figures the holdings block prints must equal the
    three the benchmark line prints for the same series over the same
    months. If this ever fails, the two lines the whole feature exists to
    put side by side are measuring different things.
    """
    series = _monthly_returns(60, "SPY")
    db_path = str(tmp_path / "fixture.duckdb")
    _make_db(db_path, [series], {"SPY": 500.0})

    held = holdings_stats({"SPY": 1000.0}, AS_OF, db_path, "USD", risk_free_rate=0.02)
    benchmark = benchmark_stats_for_window(
        BenchmarkSource("SPY", "USD", series, None),
        held.window_start,
        held.window_end,
        risk_free_rate=0.02,
    )

    assert held.weights == {"SPY": 1.0}
    assert held.annual_return == pytest.approx(benchmark.annual_return, abs=1e-9)
    assert held.annual_volatility == pytest.approx(benchmark.annual_volatility, abs=1e-9)
    assert held.sharpe == pytest.approx(benchmark.sharpe, abs=1e-9)


def test_each_holdings_own_figures_match_what_the_optimizer_reports_for_it(tmp_path):
    """The per-holding estimates must be the same numbers
    `compute_weights_and_stats` reports for the same tickers over the same
    months, because both blocks appear in one report and a reader will
    compare a holding's line against the same ticker's line in the pool
    above it. Same estimators, same shrinkage, same cross-section - so same
    numbers.
    """
    db_path = str(tmp_path / "fixture.duckdb")
    spy, t = _monthly_returns(60, "SPY"), _monthly_returns(60, "T", shift=1)
    _make_db(db_path, [spy, t], {"SPY": 600.0, "T": 20.0})

    held = holdings_stats({"SPY": 100.0, "T": 500.0}, AS_OF, db_path, "USD")
    pool = compute_weights_and_stats(
        pd.concat([spy, t], axis=1), "GMV", risk_free_rate=held.risk_free_rate
    )

    for ticker in ("SPY", "T"):
        assert held.expected_returns[ticker] == pytest.approx(
            pool.expected_returns[ticker], abs=1e-9
        )
        assert held.volatility[ticker] == pytest.approx(pool.volatility[ticker], abs=1e-9)


def test_an_excluded_holding_has_no_per_holding_figures(tmp_path):
    db_path = str(tmp_path / "fixture.duckdb")
    _make_db(
        db_path,
        [_monthly_returns(60, "SPY"), _monthly_returns(8, "NEWCO", start="2025-05-01")],
        {"SPY": 600.0, "NEWCO": 25.0},
    )

    held = holdings_stats({"SPY": 100.0, "NEWCO": 40.0}, AS_OF, db_path, "USD")

    assert set(held.expected_returns) == {"SPY"}
    assert set(held.volatility) == {"SPY"}


def test_a_one_holding_portfolios_own_figure_is_the_portfolio_figure(tmp_path):
    """With a single holding at full weight the per-holding line and the
    portfolio line describe the same thing, and printing two different
    numbers there would be visibly wrong.
    """
    db_path = str(tmp_path / "fixture.duckdb")
    _make_db(db_path, [_monthly_returns(60, "SPY")], {"SPY": 600.0})

    held = holdings_stats({"SPY": 1000.0}, AS_OF, db_path, "USD")

    assert held.expected_returns["SPY"] == pytest.approx(held.annual_return, abs=1e-9)
    assert held.volatility["SPY"] == pytest.approx(held.annual_volatility, abs=1e-9)


def test_the_reported_window_comes_from_the_data_actually_used(tmp_path):
    series = _monthly_returns(36, "SPY")
    db_path = str(tmp_path / "fixture.duckdb")
    _make_db(db_path, [series], {"SPY": 500.0})

    held = holdings_stats({"SPY": 1000.0}, AS_OF, db_path, "USD")

    assert held.window_months == 36
    assert held.window_start == date(2020, 1, 1)
    assert held.window_end == date(2022, 12, 1)


def test_total_value_and_weights_reflect_the_share_counts(tmp_path):
    db_path = str(tmp_path / "fixture.duckdb")
    _make_db(
        db_path,
        [_monthly_returns(60, "SPY"), _monthly_returns(60, "T", shift=1)],
        {"SPY": 600.0, "T": 20.0},
    )

    held = holdings_stats({"SPY": 100.0, "T": 500.0}, AS_OF, db_path, "USD")

    assert held.market_values == {"SPY": 60_000.0, "T": 10_000.0}
    assert held.total_value == 70_000.0
    assert held.weights["SPY"] == pytest.approx(60_000 / 70_000)
    assert sum(held.weights.values()) == pytest.approx(1.0)
    assert held.unavailable_reason is None


def test_a_thin_history_holding_is_excluded_and_named_with_its_month_count(tmp_path):
    db_path = str(tmp_path / "fixture.duckdb")
    _make_db(
        db_path,
        [_monthly_returns(60, "SPY"), _monthly_returns(8, "NEWCO", start="2025-05-01")],
        {"SPY": 600.0, "NEWCO": 25.0},
    )

    held = holdings_stats({"SPY": 100.0, "NEWCO": 40.0}, AS_OF, db_path, "USD")

    assert "8 month(s)" in held.excluded["NEWCO"]
    assert f"under {HOLDINGS_MIN_MONTHS}" in held.excluded["NEWCO"]
    assert held.weights == {"SPY": 1.0}
    assert held.annual_return is not None


def test_an_excluded_holding_still_counts_toward_total_value(tmp_path):
    """`total_value` answers "what is my portfolio worth?" and `weights`
    answers "what produced these figures?" - the two denominators differ on
    purpose, and this pins that they do.
    """
    db_path = str(tmp_path / "fixture.duckdb")
    _make_db(
        db_path,
        [_monthly_returns(60, "SPY"), _monthly_returns(8, "NEWCO", start="2025-05-01")],
        {"SPY": 600.0, "NEWCO": 25.0},
    )

    held = holdings_stats({"SPY": 100.0, "NEWCO": 40.0}, AS_OF, db_path, "USD")

    assert held.market_values == {"SPY": 60_000.0, "NEWCO": 1_000.0}
    assert held.total_value == 61_000.0
    assert sum(held.weights.values()) == pytest.approx(1.0)


def test_a_holding_with_history_but_no_price_is_excluded_for_the_price(tmp_path):
    db_path = str(tmp_path / "fixture.duckdb")
    _make_db(db_path, [_monthly_returns(60, "SPY"), _monthly_returns(60, "T", shift=1)], {"SPY": 600.0})

    held = holdings_stats({"SPY": 100.0, "T": 500.0}, AS_OF, db_path, "USD")

    assert "no price on or before" in held.excluded["T"]
    assert held.weights == {"SPY": 1.0}


def test_an_empty_portfolio_reports_the_command_that_fixes_it(tmp_path):
    held = holdings_stats({}, AS_OF, str(tmp_path / "never-created.duckdb"), "USD")

    assert held.annual_return is None and held.window_months is None
    assert "no holdings are saved for USD" in held.unavailable_reason
    assert "portfolio-holdings set" in held.unavailable_reason
    assert not (tmp_path / "never-created.duckdb").exists()


def test_a_portfolio_whose_every_holding_is_thin_reports_no_figures(tmp_path):
    db_path = str(tmp_path / "fixture.duckdb")
    _make_db(db_path, [_monthly_returns(8, "NEWCO", start="2025-05-01")], {"NEWCO": 25.0})

    held = holdings_stats({"NEWCO": 40.0}, AS_OF, db_path, "USD")

    assert held.annual_return is None and held.annual_volatility is None and held.sharpe is None
    assert held.window_start is None and held.window_end is None and held.window_months is None
    assert "NEWCO" in held.excluded
    assert str(HOLDINGS_MIN_MONTHS) in held.unavailable_reason


def test_the_risk_free_rate_is_echoed_back_so_the_report_can_state_it(tmp_path):
    db_path = str(tmp_path / "fixture.duckdb")
    _make_db(db_path, [_monthly_returns(60, "SPY")], {"SPY": 600.0})

    held = holdings_stats({"SPY": 100.0}, AS_OF, db_path, "USD", risk_free_rate=0.045)

    assert held.risk_free_rate == 0.045
    assert held.sharpe == pytest.approx((held.annual_return - 0.045) / held.annual_volatility)


# --- stored_month_counts ----------------------------------------------------


def test_stored_month_counts_reports_zero_for_a_missing_database(tmp_path):
    assert stored_month_counts(["SPY"], AS_OF, str(tmp_path / "nope.duckdb")) == {"SPY": 0}


def test_stored_month_counts_does_not_create_the_database_it_is_asked_about(tmp_path):
    missing = tmp_path / "nope.duckdb"
    stored_month_counts(["SPY"], AS_OF, str(missing))
    assert not missing.exists()


def test_stored_month_counts_counts_each_tickers_own_months(tmp_path):
    db_path = str(tmp_path / "fixture.duckdb")
    _make_db(db_path, [_monthly_returns(60, "SPY"), _monthly_returns(8, "NEWCO")], {})

    assert stored_month_counts(["SPY", "NEWCO", "ABSENT"], AS_OF, db_path) == {
        "SPY": 60,
        "NEWCO": 8,
        "ABSENT": 0,
    }


# --- open_holdings_session / measure_holdings -------------------------------


def _no_fetch(monkeypatch):
    """Fail the test if anything tries to reach Yahoo Finance."""

    def fail(tickers, as_of, db_path):
        raise AssertionError(f"fetched {tickers} when it should not have")

    monkeypatch.setattr("src.flow.interactive.validate_and_ingest_tickers", fail)


def test_a_session_measures_every_variant_over_one_window(tmp_path, monkeypatch):
    """The correctness reason `open_holdings_session` exists. Two variants
    measured against two separate throwaway databases can come back over two
    different windows, because the window is derived from the whole `returns`
    table - and then the change between their Sharpe ratios is partly the
    change of window. One session makes that impossible.
    """
    db_path = str(tmp_path / "fixture.duckdb")
    _make_db(
        db_path,
        [_monthly_returns(60, "SPY"), _monthly_returns(60, "T", shift=1)],
        {"SPY": 600.0, "T": 20.0},
    )
    _no_fetch(monkeypatch)

    with open_holdings_session([], AS_OF, db_path, allow_fetch=False) as session:
        one = measure_holdings({"SPY": 100.0}, "USD", AS_OF, session, 0.02)
        two = measure_holdings({"SPY": 100.0, "T": 500.0}, "USD", AS_OF, session, 0.02)

    assert (one.window_start, one.window_end) == (two.window_start, two.window_end)
    assert one.window_months == two.window_months


def test_a_no_fetch_session_uses_the_given_database_and_forbids_ingesting(tmp_path, monkeypatch):
    """`--no-holdings-fetch` means touch no network, and `--db-path` is
    documented as never written to - so a session over it must announce that
    a new ticker cannot be added rather than quietly ingesting into the
    shared cache.
    """
    db_path = str(tmp_path / "fixture.duckdb")
    _make_db(db_path, [_monthly_returns(60, "SPY")], {"SPY": 600.0})
    _no_fetch(monkeypatch)

    with open_holdings_session(["SPY"], AS_OF, db_path, allow_fetch=False) as session:
        assert session.db_path == db_path
        assert session.can_ingest is False


def test_measure_holdings_never_fetches(tmp_path, monkeypatch):
    db_path = str(tmp_path / "fixture.duckdb")
    _make_db(db_path, [_monthly_returns(60, "SPY")], {"SPY": 600.0})
    _no_fetch(monkeypatch)

    with open_holdings_session([], AS_OF, db_path, allow_fetch=False) as session:
        stats = measure_holdings({"SPY": 100.0}, "USD", AS_OF, session, 0.02)

    assert stats.sharpe is not None


def test_measure_holdings_agrees_with_prepare_holdings_on_the_same_data(tmp_path, monkeypatch):
    """A what-if variant and an ordinary report must measure a portfolio
    identically, or the baseline block and the hypothetical block would not
    be comparable even before anything changed.
    """
    db_path = str(tmp_path / "fixture.duckdb")
    _make_db(
        db_path,
        [_monthly_returns(60, "SPY"), _monthly_returns(60, "T", shift=1)],
        {"SPY": 600.0, "T": 20.0},
    )
    _no_fetch(monkeypatch)
    positions = {"SPY": 100.0, "T": 500.0}

    with open_holdings_session([], AS_OF, db_path, allow_fetch=False) as session:
        measured = measure_holdings(positions, "USD", AS_OF, session, 0.02)
    direct = holdings_stats(positions, AS_OF, db_path, "USD", 0.02)

    assert measured.annual_return == pytest.approx(direct.annual_return)
    assert measured.annual_volatility == pytest.approx(direct.annual_volatility)
    assert measured.sharpe == pytest.approx(direct.sharpe)


def test_measure_holdings_excludes_a_cross_currency_holding(tmp_path, monkeypatch):
    db_path = str(tmp_path / "fixture.duckdb")
    _make_db(
        db_path,
        [_monthly_returns(60, "SPY"), _monthly_returns(60, "7203.T", shift=1)],
        {"SPY": 600.0, "7203.T": 3000.0},
    )
    _no_fetch(monkeypatch)

    with open_holdings_session([], AS_OF, db_path, allow_fetch=False) as session:
        stats = measure_holdings(
            {"SPY": 100.0, "7203.T": 50.0}, "USD", AS_OF, session, 0.02, {"7203.T": "JPY"}
        )

    assert "priced in JPY" in stats.excluded["7203.T"]
    assert "7203.T" not in stats.weights


def test_measure_holdings_reports_an_empty_variant_rather_than_raising(tmp_path, monkeypatch):
    db_path = str(tmp_path / "fixture.duckdb")
    _make_db(db_path, [_monthly_returns(60, "SPY")], {"SPY": 600.0})
    _no_fetch(monkeypatch)

    with open_holdings_session([], AS_OF, db_path, allow_fetch=False) as session:
        stats = measure_holdings({}, "USD", AS_OF, session, 0.02)

    assert stats.annual_return is None
    assert stats.unavailable_reason is not None


# --- prepare_holdings (the tests Milestone 2 specified and never wrote) -----


def test_prepare_holdings_never_writes_to_the_session_database(tmp_path, monkeypatch):
    """The isolation rule `prepare_holdings`' docstring calls a correctness
    requirement: the shared cache must not gain rows as a side effect of
    printing a report, and the portfolio's returns window must not be
    movable by a holdings fetch.
    """
    db_path = tmp_path / "session.duckdb"
    _make_db(str(db_path), [_monthly_returns(60, "SPY")], {"SPY": 600.0})

    before_rows = duckdb.connect(str(db_path)).execute("SELECT count(*) FROM returns").fetchone()[0]
    before_mtime = db_path.stat().st_mtime_ns

    scratch = tmp_path / "scratch.duckdb"
    _make_db(str(scratch), [_monthly_returns(60, "NEWCO")], {"NEWCO": 25.0})
    monkeypatch.setattr(
        "src.flow.interactive.build_scratch_snapshot",
        lambda prefix="x": _fake_scratch(str(scratch)),
    )
    monkeypatch.setattr(
        "src.flow.interactive.validate_and_ingest_tickers",
        lambda tickers, as_of, path: (list(tickers), {}, {t: "USD" for t in tickers}),
    )

    prepare_holdings({"NEWCO": 40.0}, "USD", AS_OF, str(db_path), 0.02)

    after_rows = duckdb.connect(str(db_path)).execute("SELECT count(*) FROM returns").fetchone()[0]
    assert after_rows == before_rows
    assert db_path.stat().st_mtime_ns == before_mtime


def test_prepare_holdings_reads_the_cache_without_fetching_when_it_suffices(tmp_path, monkeypatch):
    db_path = str(tmp_path / "session.duckdb")
    _make_db(db_path, [_monthly_returns(60, "SPY")], {"SPY": 600.0})
    _no_fetch(monkeypatch)

    stats = prepare_holdings({"SPY": 100.0}, "USD", AS_OF, db_path, 0.02)

    assert stats.sharpe is not None


def test_prepare_holdings_with_fetching_disabled_names_the_flag(tmp_path, monkeypatch):
    db_path = str(tmp_path / "session.duckdb")
    _make_db(db_path, [_monthly_returns(8, "NEWCO")], {"NEWCO": 25.0})
    _no_fetch(monkeypatch)

    stats = prepare_holdings({"NEWCO": 40.0}, "USD", AS_OF, db_path, 0.02, allow_fetch=False)

    assert stats.annual_return is None
    assert "--no-holdings-fetch" in stats.unavailable_reason


def test_prepare_holdings_turns_an_ingest_failure_into_a_reported_reason(tmp_path, monkeypatch):
    """Never raises: losing a live session's fetched snapshot over one report
    block would cost far more than the block is worth.
    """
    db_path = str(tmp_path / "session.duckdb")
    _make_db(db_path, [], {})
    monkeypatch.setattr(
        "src.flow.interactive.validate_and_ingest_tickers",
        lambda tickers, as_of, path: (_ for _ in ()).throw(RuntimeError("yfinance exploded")),
    )

    stats = prepare_holdings({"NEWCO": 40.0}, "USD", AS_OF, db_path, 0.02)

    assert stats.annual_return is None
    assert "yfinance exploded" in stats.unavailable_reason
