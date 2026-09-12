"""Tests for src/agentic_portfolio/optimizer/holdings.py and src/agentic_portfolio/flow/interactive.py's
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

from agentic_portfolio.optimizer.benchmark import benchmark_stats_for_window, BenchmarkSource
from agentic_portfolio.optimizer.dividends import NO_DIVIDEND_FIGURES, dividend_figures
from agentic_portfolio.optimizer.holdings import (
    DEFAULT_LOOKBACK_MONTHS,
    HOLDINGS_MIN_MONTHS,
    holdings_stats,
    unavailable_holdings,
    stored_month_counts,
    validate_lookback_months,
    weights_from_positions,
)
from agentic_portfolio.flow.interactive import (
    measure_holdings,
    open_holdings_session,
    prepare_holdings,
)
from agentic_portfolio.optimizer.portfolio import compute_weights_and_stats


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

    Also creates the empty `splits` table that any database written by the
    current code has. Without it, `stale_tickers`' one-time migration check
    would treat a freshly-built fixture cache as pre-migration and refetch,
    which is a property of the fixture's vintage rather than of anything
    these tests mean to exercise.
    """
    con = duckdb.connect(db_path)
    try:
        con.execute("CREATE TABLE returns (rebalance_date DATE, ticker VARCHAR, monthly_return DOUBLE)")
        con.execute("CREATE TABLE prices (date DATE, ticker VARCHAR, close DOUBLE, adj_close DOUBLE)")
        con.execute("CREATE TABLE splits (ex_date DATE, ticker VARCHAR, ratio DOUBLE)")
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
    """Fail the test if anything tries to reach Yahoo Finance.

    Patched on BOTH importing modules. `src/agentic_portfolio/flow/interactive.py` still holds
    its own reference, but since the holdings cache landed the fetch a
    holdings report makes goes through `src/agentic_portfolio/dataset/holdings_cache.py`'s
    reference instead - and patching only the old one let a real network
    call escape into the suite, which is exactly why this project's rule is
    to patch the importing module's own symbol rather than the definition.
    """

    def fail(tickers, as_of, db_path):
        raise AssertionError(f"fetched {tickers} when it should not have")

    monkeypatch.setattr("agentic_portfolio.flow.interactive.validate_and_ingest_tickers", fail)
    monkeypatch.setattr("agentic_portfolio.dataset.holdings_cache.validate_and_ingest_tickers", fail)


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

    with open_holdings_session([], AS_OF, db_path, allow_fetch=False, cache_path=db_path) as session:
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

    with open_holdings_session(["SPY"], AS_OF, db_path, allow_fetch=False, cache_path=db_path) as session:
        assert session.db_path == db_path
        assert session.can_ingest is False


def test_measure_holdings_never_fetches(tmp_path, monkeypatch):
    db_path = str(tmp_path / "fixture.duckdb")
    _make_db(db_path, [_monthly_returns(60, "SPY")], {"SPY": 600.0})
    _no_fetch(monkeypatch)

    with open_holdings_session([], AS_OF, db_path, allow_fetch=False, cache_path=db_path) as session:
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

    with open_holdings_session([], AS_OF, db_path, allow_fetch=False, cache_path=db_path) as session:
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

    with open_holdings_session([], AS_OF, db_path, allow_fetch=False, cache_path=db_path) as session:
        stats = measure_holdings(
            {"SPY": 100.0, "7203.T": 50.0}, "USD", AS_OF, session, 0.02, {"7203.T": "JPY"}
        )

    assert "priced in JPY" in stats.excluded["7203.T"]
    assert "7203.T" not in stats.weights


def test_measure_holdings_reports_an_empty_variant_rather_than_raising(tmp_path, monkeypatch):
    db_path = str(tmp_path / "fixture.duckdb")
    _make_db(db_path, [_monthly_returns(60, "SPY")], {"SPY": 600.0})
    _no_fetch(monkeypatch)

    with open_holdings_session([], AS_OF, db_path, allow_fetch=False, cache_path=db_path) as session:
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
        "agentic_portfolio.flow.interactive.build_scratch_snapshot",
        lambda prefix="x": _fake_scratch(str(scratch)),
    )
    monkeypatch.setattr(
        "agentic_portfolio.dataset.holdings_cache.validate_and_ingest_tickers",
        lambda tickers, as_of, path: (list(tickers), {}, {t: "USD" for t in tickers}),
    )

    prepare_holdings({"NEWCO": 40.0}, "USD", AS_OF, str(db_path), 0.02, cache_path=str(scratch))

    after_rows = duckdb.connect(str(db_path)).execute("SELECT count(*) FROM returns").fetchone()[0]
    assert after_rows == before_rows
    assert db_path.stat().st_mtime_ns == before_mtime


def test_prepare_holdings_reads_the_cache_without_fetching_when_it_suffices(tmp_path, monkeypatch):
    db_path = str(tmp_path / "session.duckdb")
    _make_db(db_path, [_monthly_returns(60, "SPY")], {"SPY": 600.0})
    _no_fetch(monkeypatch)

    stats = prepare_holdings({"SPY": 100.0}, "USD", AS_OF, db_path, 0.02, cache_path=db_path)

    assert stats.sharpe is not None


def test_prepare_holdings_with_fetching_disabled_names_the_flag(tmp_path, monkeypatch):
    db_path = str(tmp_path / "session.duckdb")
    _make_db(db_path, [_monthly_returns(8, "NEWCO")], {"NEWCO": 25.0})
    _no_fetch(monkeypatch)

    stats = prepare_holdings(
        {"NEWCO": 40.0}, "USD", AS_OF, db_path, 0.02, allow_fetch=False, cache_path=db_path
    )

    assert stats.annual_return is None
    assert "--no-holdings-fetch" in stats.unavailable_reason


def test_prepare_holdings_turns_an_ingest_failure_into_a_reported_reason(tmp_path, monkeypatch):
    """Never raises: losing a live session's fetched snapshot over one report
    block would cost far more than the block is worth.
    """
    db_path = str(tmp_path / "session.duckdb")
    _make_db(db_path, [], {})
    monkeypatch.setattr(
        "agentic_portfolio.dataset.holdings_cache.validate_and_ingest_tickers",
        lambda tickers, as_of, path: (_ for _ in ()).throw(RuntimeError("yfinance exploded")),
    )

    stats = prepare_holdings({"NEWCO": 40.0}, "USD", AS_OF, db_path, 0.02, cache_path=db_path)

    assert stats.annual_return is None
    assert "yfinance exploded" in stats.unavailable_reason


# --- the holdings cache, through prepare_holdings ---------------------------


def test_prepare_holdings_reuses_a_fresh_cache_without_fetching(tmp_path, monkeypatch):
    """The point of the cache, seen from the report: a second run in the same
    month costs no network at all.
    """
    session = str(tmp_path / "session.duckdb")
    _make_db(session, [], {})
    cache = str(tmp_path / "holdings.duckdb")
    _make_db(cache, [_monthly_returns(60, "SPY", start="2021-10-01")], {"SPY": 600.0})
    _no_fetch(monkeypatch)

    stats = prepare_holdings(
        {"SPY": 100.0}, "USD", date(2026, 9, 25), session, 0.02, cache_path=cache
    )

    assert stats.sharpe is not None


def test_prepare_holdings_prefers_the_session_database_over_the_cache(tmp_path, monkeypatch):
    """The session database is consulted first because that read is free and
    already correct - a backtest-window run against the S&P cache must not
    start depending on a holdings cache it never needed.
    """
    session = str(tmp_path / "session.duckdb")
    _make_db(session, [_monthly_returns(60, "SPY")], {"SPY": 600.0})
    missing_cache = tmp_path / "holdings.duckdb"
    _no_fetch(monkeypatch)

    stats = prepare_holdings(
        {"SPY": 100.0}, "USD", AS_OF, session, 0.02, cache_path=str(missing_cache)
    )

    assert stats.sharpe is not None
    assert not missing_cache.exists()


def test_prepare_holdings_never_writes_to_the_shared_price_cache(tmp_path, monkeypatch):
    """The isolation rule survives the cache becoming persistent: holdings
    rows land in their OWN file, so they can never move the returns window a
    candidate pool is measured over.
    """
    session = tmp_path / "session.duckdb"
    _make_db(str(session), [_monthly_returns(60, "AAPL")], {"AAPL": 200.0})
    before_mtime = session.stat().st_mtime_ns
    cache = str(tmp_path / "holdings.duckdb")

    monkeypatch.setattr(
        "agentic_portfolio.dataset.holdings_cache.validate_and_ingest_tickers",
        lambda tickers, as_of, path: (list(tickers), {}, {t: "USD" for t in tickers}),
    )

    prepare_holdings({"NEWCO": 40.0}, "USD", AS_OF, str(session), 0.02, cache_path=cache)

    assert session.stat().st_mtime_ns == before_mtime


def test_prepare_holdings_offline_still_reads_the_cache(tmp_path, monkeypatch):
    """`--no-holdings-fetch` means touch no network, and reading a local file
    is not a network call - so an offline run gets whatever the cache holds
    rather than nothing at all.
    """
    session = str(tmp_path / "session.duckdb")
    _make_db(session, [], {})
    cache = str(tmp_path / "holdings.duckdb")
    _make_db(cache, [_monthly_returns(60, "SPY", start="2021-10-01")], {"SPY": 600.0})
    _no_fetch(monkeypatch)

    stats = prepare_holdings(
        {"SPY": 100.0}, "USD", date(2026, 9, 25), session, 0.02,
        allow_fetch=False, cache_path=cache,
    )

    assert stats.sharpe is not None


def test_prepare_holdings_offline_with_an_empty_cache_names_both_places(tmp_path, monkeypatch):
    session = str(tmp_path / "session.duckdb")
    _make_db(session, [], {})
    cache = str(tmp_path / "holdings.duckdb")
    _make_db(cache, [], {})
    _no_fetch(monkeypatch)

    stats = prepare_holdings(
        {"SPY": 100.0}, "USD", AS_OF, session, 0.02, allow_fetch=False, cache_path=cache
    )

    assert stats.annual_return is None
    assert "holdings cache" in stats.unavailable_reason
    assert "--no-holdings-fetch" in stats.unavailable_reason


def test_the_report_states_the_date_it_priced_the_holdings_at(tmp_path, monkeypatch):
    """The mitigation for the monthly rule. Prices change daily but the cache
    is only refreshed monthly, so a total can be weeks old - and a money
    figure whose age is not stated is one a reader assumes is current.
    """
    session = str(tmp_path / "session.duckdb")
    _make_db(session, [_monthly_returns(60, "SPY")], {"SPY": 600.0})
    _no_fetch(monkeypatch)

    stats = prepare_holdings({"SPY": 100.0}, "USD", AS_OF, session, 0.02, cache_path=session)

    # `_make_db` writes its one price row at 2019-12-02.
    assert stats.priced_as_of == date(2019, 12, 2)


def test_a_what_if_session_opens_on_the_cache_and_refreshes_only_what_is_stale(
    tmp_path, monkeypatch
):
    """Since the cache landed, the first measurement of a what-if session
    usually costs nothing - which is what makes the loop worth opening.
    """
    cache = str(tmp_path / "holdings.duckdb")
    _make_db(cache, [_monthly_returns(60, "SPY", start="2021-10-01")], {"SPY": 600.0})
    _no_fetch(monkeypatch)

    with open_holdings_session(
        ["SPY"], date(2026, 9, 25), "unused.duckdb", cache_path=cache
    ) as session:
        assert session.db_path == cache
        assert session.can_ingest is True
        stats = measure_holdings({"SPY": 100.0}, "USD", date(2026, 9, 25), session, 0.02)

    assert stats.sharpe is not None


# --- the selectable returns window -----------------------------------------


def test_a_shorter_window_measures_over_only_those_months(tmp_path, monkeypatch):
    db_path = str(tmp_path / "fixture.duckdb")
    # Starts 2021-02 so all 60 months land on or before AS_OF (2026-01-01);
    # a later start would be truncated and the 60-month case would not be
    # exercising a full window at all.
    _make_db(db_path, [_monthly_returns(60, "SPY", start="2021-02-01")], {"SPY": 600.0})
    _no_fetch(monkeypatch)

    with open_holdings_session([], AS_OF, db_path, allow_fetch=False, cache_path=db_path) as s:
        short = measure_holdings({"SPY": 100.0}, "USD", AS_OF, s, 0.02, None, 36)

    assert short.window_months == 36
    assert (short.window_start, short.window_end) == (date(2023, 2, 1), date(2026, 1, 1))


def test_a_shorter_window_actually_changes_the_figures(tmp_path, monkeypatch):
    """The whole point. A window that reached the estimator but changed
    nothing would mean the parameter is inert - which is exactly the failure
    a threading bug produces, and it would look like success.

    The fixture's returns oscillate on a 2-month and a 5-month cycle, so a
    36-month slice genuinely differs from the 60-month one.
    """
    db_path = str(tmp_path / "fixture.duckdb")
    _make_db(
        db_path,
        [
            _monthly_returns(60, "SPY", start="2021-02-01"),
            _monthly_returns(60, "T", start="2021-02-01", shift=1),
        ],
        {"SPY": 600.0, "T": 20.0},
    )
    _no_fetch(monkeypatch)
    positions = {"SPY": 100.0, "T": 500.0}

    with open_holdings_session([], AS_OF, db_path, allow_fetch=False, cache_path=db_path) as s:
        full = measure_holdings(positions, "USD", AS_OF, s, 0.02, None, 60)
        short = measure_holdings(positions, "USD", AS_OF, s, 0.02, None, 36)

    assert full.window_months == 60 and short.window_months == 36
    assert short.annual_volatility != pytest.approx(full.annual_volatility)


def test_a_window_shorter_than_the_data_still_reports_what_it_used(tmp_path, monkeypatch):
    """A request larger than the table degrades to what exists, and the
    reported count is the real one - which is why the report names the
    REQUEST separately.
    """
    db_path = str(tmp_path / "fixture.duckdb")
    _make_db(db_path, [_monthly_returns(40, "SPY", start="2022-09-01")], {"SPY": 600.0})
    _no_fetch(monkeypatch)

    with open_holdings_session([], AS_OF, db_path, allow_fetch=False, cache_path=db_path) as s:
        stats = measure_holdings({"SPY": 100.0}, "USD", AS_OF, s, 0.02, None, 60)

    assert stats.window_months == 40


def test_at_the_shortest_window_a_holding_missing_one_month_is_excluded(tmp_path, monkeypatch):
    """The documented consequence of keeping `min_months` at 24: when the
    window is 24 too, a holding needs EVERY month, so a single missing one
    drops it. `plans/05_optimizer_and_allocation.md`'s "use whatever months
    a ticker actually has" tolerance has no room to operate at the floor.
    The exclusion message explains itself, which is why this is documented
    rather than fixed.
    """
    db_path = str(tmp_path / "fixture.duckdb")
    full = _monthly_returns(60, "SPY", start="2021-10-01")
    # 23 of the last 24 months: starts one month late.
    partial = _monthly_returns(23, "LATE", start="2024-11-01")
    _make_db(db_path, [full, partial], {"SPY": 600.0, "LATE": 50.0})
    _no_fetch(monkeypatch)

    with open_holdings_session([], AS_OF, db_path, allow_fetch=False, cache_path=db_path) as s:
        stats = measure_holdings({"SPY": 100.0, "LATE": 10.0}, "USD", AS_OF, s, 0.02, None, 24)

    assert stats.window_months == 24
    assert "under 24" in stats.excluded["LATE"]
    assert "LATE" not in stats.weights


# --- validate_lookback_months ----------------------------------------------


def test_the_default_and_the_ceiling_are_the_same_sixty():
    assert validate_lookback_months(DEFAULT_LOOKBACK_MONTHS, "x") == 60
    assert validate_lookback_months(HOLDINGS_MIN_MONTHS, "x") == 24


def test_a_window_below_the_minimum_history_bar_is_refused():
    """Below 24 every holding falls under `min_months` and the report comes
    back with no figures at all, complaining about a number the person never
    typed. Refused at the door instead.
    """
    with pytest.raises(ValueError) as excinfo:
        validate_lookback_months(23, "[w]indow")

    assert "between 24 and 60" in str(excinfo.value)


def test_a_window_beyond_what_an_ingest_fetches_is_refused():
    with pytest.raises(ValueError) as excinfo:
        validate_lookback_months(61, "[w]indow")

    assert "65 months of prices" in str(excinfo.value)


def test_a_zero_or_negative_window_is_refused_before_duckdb_sees_it():
    """`_load_window_dates` passes the value straight to SQL `LIMIT`, and
    DuckDB raises a BinderException on a negative - which the report layer's
    blanket handler would surface as an unhelpful "could not measure".
    """
    for bad in (0, -1):
        with pytest.raises(ValueError, match="between 24 and 60"):
            validate_lookback_months(bad, "[w]indow")


def test_a_non_integer_window_is_refused_naming_its_source():
    for bad in (36.5, "abc", None):
        with pytest.raises(ValueError, match="whole number of months"):
            validate_lookback_months(bad, "[w]indow")


def test_a_boolean_window_is_refused_even_though_bool_is_an_int():
    with pytest.raises(ValueError, match="whole number of months"):
        validate_lookback_months(True, "[w]indow")


# --- dividend figures on a held portfolio -----------------------------------


def test_holdings_stats_does_not_consult_dividends_by_default(tmp_path):
    """Every caller and test predating this feature must be unaffected, so
    the dividend figures are opt-in and their absence is a distinguishable
    state rather than a portfolio that pays nothing.
    """
    db = str(tmp_path / "h.duckdb")
    _make_db(db, [_monthly_returns(40, "T")], {"T": 20.0})
    stats = holdings_stats({"T": 100.0}, AS_OF, db, "USD")
    assert stats.dividends is NO_DIVIDEND_FIGURES


def test_holdings_stats_reports_annual_dividends_from_shares_and_per_share_cash(tmp_path):
    db = str(tmp_path / "h.duckdb")
    _make_db(db, [_monthly_returns(40, "T")], {"T": 20.0})
    stats = holdings_stats(
        {"T": 100.0}, AS_OF, db, "USD", dividends_per_share={"T": 1.11}
    )
    assert stats.dividends.annual_dividends["T"] == pytest.approx(111.0)
    assert stats.dividends.total_annual_dividends == pytest.approx(111.0)
    assert stats.dividends.value_covered == pytest.approx(2000.0)
    assert stats.dividends.dividend_yield == pytest.approx(111.0 / 2000.0)


def test_holdings_stats_dividends_cover_a_holding_excluded_from_the_return_figures(tmp_path):
    """The denominator-discipline test. A dividend yield needs no return
    history at all, so a holding dropped from the risk figures for being too
    recently listed still pays what it pays - and leaving it out would
    understate the income the portfolio really produces.
    """
    db = str(tmp_path / "h.duckdb")
    _make_db(
        db,
        [_monthly_returns(40, "T"), _monthly_returns(8, "NEWCO", shift=1)],
        {"T": 20.0, "NEWCO": 50.0},
    )
    stats = holdings_stats(
        {"T": 100.0, "NEWCO": 10.0},
        AS_OF,
        db,
        "USD",
        dividends_per_share={"T": 1.11, "NEWCO": 2.00},
    )
    assert "NEWCO" in stats.excluded
    assert "NEWCO" not in stats.weights
    assert stats.dividends.annual_dividends["NEWCO"] == pytest.approx(20.0)
    assert stats.dividends.total_annual_dividends == pytest.approx(131.0)


def test_holdings_stats_dividends_do_not_move_with_the_lookback_window(tmp_path):
    """A trailing dividend is a record of cash paid, not an estimate over a
    returns window - which is what lets `whatif`'s `[w]indow` report a real
    zero change in income rather than withholding the comparison.
    """
    db = str(tmp_path / "h.duckdb")
    _make_db(db, [_monthly_returns(60, "T")], {"T": 20.0})
    short = holdings_stats(
        {"T": 100.0}, AS_OF, db, "USD", lookback_months=24, dividends_per_share={"T": 1.11}
    )
    long = holdings_stats(
        {"T": 100.0}, AS_OF, db, "USD", lookback_months=60, dividends_per_share={"T": 1.11}
    )
    assert short.window_months != long.window_months
    assert short.dividends == long.dividends


def test_holdings_stats_shrinks_the_dividend_denominator_for_a_holding_with_no_data(tmp_path):
    db = str(tmp_path / "h.duckdb")
    _make_db(
        db,
        [_monthly_returns(40, "T"), _monthly_returns(40, "GROWTH", shift=1)],
        {"T": 20.0, "GROWTH": 100.0},
    )
    stats = holdings_stats(
        {"T": 100.0, "GROWTH": 10.0},
        AS_OF,
        db,
        "USD",
        dividends_per_share={"T": 1.11},
        dividend_unavailable={"GROWTH": "no trailing dividend data"},
    )
    assert stats.dividends.value_covered == pytest.approx(2000.0)
    assert stats.total_value == pytest.approx(3000.0)
    assert "GROWTH" in stats.dividends.unavailable


def test_unavailable_holdings_can_still_carry_dividend_figures():
    """A portfolio whose returns cannot be measured still pays what it pays,
    and that is the one number such a holder most wants.
    """
    figures = dividend_figures({"T": 100.0}, {"T": 2000.0}, {"T": 1.11})
    stats = unavailable_holdings(
        "USD", {"T": 100.0}, 0.02, "too little history", dividends=figures
    )
    assert stats.annual_return is None
    assert stats.dividends.total_annual_dividends == pytest.approx(111.0)


def test_holdings_are_valued_at_the_market_close_not_the_adjusted_close(tmp_path):
    """`Total value` and every weight derived from it must use a price
    somebody could sell at.

    `adj_close` is back-adjusted, so at any date before the price window's
    end it sits below the market price - measured on the real holdings
    cache, `VZ` at 2024-06-03 was undervalued by 15.9%. Because
    `weights_from_positions` shares one price loader with
    `allocate_shares`, this and the allocation are provably the same number,
    which is the invariant `plans/13_user_portfolio.md` established.
    """
    db = str(tmp_path / "h.duckdb")
    con = duckdb.connect(db)
    try:
        con.execute("CREATE TABLE returns (rebalance_date DATE, ticker VARCHAR, monthly_return DOUBLE)")
        con.execute("CREATE TABLE prices (date DATE, ticker VARCHAR, close DOUBLE, adj_close DOUBLE)")
        con.execute("CREATE TABLE splits (ex_date DATE, ticker VARCHAR, ratio DOUBLE)")
        con.executemany(
            "INSERT INTO returns VALUES (?, 'VZ', ?)",
            [(ts.date(), float(v)) for ts, v in _monthly_returns(40, "VZ").items()],
        )
        con.execute("INSERT INTO prices VALUES ('2024-06-03', 'VZ', 40.98, 35.37)")
    finally:
        con.close()

    stats = holdings_stats({"VZ": 100.0}, date(2024, 6, 4), db, "USD")

    assert stats.market_values["VZ"] == pytest.approx(4098.0)
    assert stats.total_value == pytest.approx(4098.0)
