"""Tests for src/dataset/holdings_cache.py: the monthly staleness rule that
decides when a held ticker's prices are refetched, and the refresh that
touches only the tickers a report actually asked about.

Per AGENTS.md's testing guidance these are hermetic. The one network seam
(`validate_and_ingest_tickers`) is monkeypatched on this module's own
symbol, and every cache is a tiny fixture DuckDB file in `tmp_path` rather
than the real `data/holdings.duckdb` - which, unpatched and unpointed,
these tests would otherwise fetch into.

The load-bearing test is
`test_a_ticker_cached_this_month_is_not_refetched`: the whole point of the
file is that a repeated report costs no network, and the whole risk is that
a subtly wrong staleness rule makes it refetch anyway (or, worse, never).
"""

from datetime import date

import duckdb
import pandas as pd
import pytest

from src.dataset.holdings_cache import (
    cached_month_ends,
    latest_expected_rebalance_date,
    refresh_holdings_cache,
    stale_tickers,
)


def _make_cache(cache_path: str, newest: dict[str, str], currency: str = "USD") -> None:
    """A cache holding one monthly return per ticker, dated `newest[ticker]`,
    plus a `ticker_currency` row so a fresh ticker's currency is readable
    without a fetch.
    """
    con = duckdb.connect(cache_path)
    try:
        con.execute(
            "CREATE TABLE returns (rebalance_date DATE, ticker VARCHAR, monthly_return DOUBLE)"
        )
        con.execute(
            "CREATE TABLE ticker_currency "
            "(ticker VARCHAR, currency VARCHAR, quoted_currency VARCHAR, price_multiplier DOUBLE)"
        )
        con.executemany(
            "INSERT INTO returns VALUES (?, ?, ?)",
            [(date.fromisoformat(d), t, 0.01) for t, d in newest.items()],
        )
        con.executemany(
            "INSERT INTO ticker_currency VALUES (?, ?, ?, ?)",
            [(t, currency, currency, 1.0) for t in newest],
        )
    finally:
        con.close()


def _stub_fetch(monkeypatch, currencies: dict[str, str] | None = None) -> list[list[str]]:
    """Record which tickers each fetch asked for, and write the rows a real
    ingest would. Returns the list of fetched batches.
    """
    fetched: list[list[str]] = []

    def fake(tickers, as_of, db_path):
        fetched.append(sorted(tickers))
        expected = latest_expected_rebalance_date(as_of)
        con = duckdb.connect(db_path)
        try:
            con.execute(
                "CREATE TABLE IF NOT EXISTS returns "
                "(rebalance_date DATE, ticker VARCHAR, monthly_return DOUBLE)"
            )
            con.executemany(
                "INSERT INTO returns VALUES (?, ?, ?)",
                [(expected, t, 0.01) for t in tickers],
            )
        finally:
            con.close()
        return sorted(tickers), {}, {t: (currencies or {}).get(t, "USD") for t in tickers}

    monkeypatch.setattr("src.dataset.holdings_cache.validate_and_ingest_tickers", fake)
    return fetched


# --- latest_expected_rebalance_date ----------------------------------------


def test_the_expected_month_is_the_first_business_day_of_the_reports_own_month():
    assert latest_expected_rebalance_date(date(2026, 9, 6)) == date(2026, 9, 1)


def test_a_report_on_the_first_business_day_expects_that_day():
    assert latest_expected_rebalance_date(date(2026, 9, 1)) == date(2026, 9, 1)


def test_a_report_before_its_months_first_business_day_expects_the_previous_month():
    """2026-11-01 is a Sunday, so November's first business day has not
    happened yet and October's return is the newest one that can exist.
    Getting this wrong would make the cache permanently stale for a day or
    two each month and refetch on every single run.
    """
    assert latest_expected_rebalance_date(date(2026, 11, 1)) == date(2026, 10, 1)
    assert latest_expected_rebalance_date(date(2026, 11, 2)) == date(2026, 11, 2)


def test_the_expected_month_uses_the_same_grid_the_returns_table_was_written_on():
    """Two different definitions of "the monthly grid" would make a cache
    that can never look fresh, so this is pinned against the function that
    wrote the rows.
    """
    from src.dataset.membership import compute_rebalance_dates

    as_of = date(2026, 2, 20)
    expected = compute_rebalance_dates("2026-02-01", as_of.isoformat())[-1].date()

    assert latest_expected_rebalance_date(as_of) == expected


# --- staleness -------------------------------------------------------------


def test_a_ticker_cached_this_month_is_not_refetched(tmp_path, monkeypatch):
    """The whole point of the file."""
    cache = str(tmp_path / "holdings.duckdb")
    _make_cache(cache, {"SPY": "2026-09-01"})
    fetched = _stub_fetch(monkeypatch)

    assert stale_tickers(["SPY"], date(2026, 9, 25), cache) == []
    refresh_holdings_cache(["SPY"], date(2026, 9, 25), cache)

    assert fetched == []


def test_a_new_month_makes_a_cached_ticker_stale(tmp_path, monkeypatch):
    cache = str(tmp_path / "holdings.duckdb")
    _make_cache(cache, {"SPY": "2026-09-01"})
    fetched = _stub_fetch(monkeypatch)

    assert stale_tickers(["SPY"], date(2026, 10, 5), cache) == ["SPY"]
    refresh_holdings_cache(["SPY"], date(2026, 10, 5), cache)

    assert fetched == [["SPY"]]


def test_a_ticker_the_cache_has_never_seen_is_stale(tmp_path):
    cache = str(tmp_path / "holdings.duckdb")
    _make_cache(cache, {"SPY": "2026-09-01"})

    assert stale_tickers(["SPY", "NVDA"], date(2026, 9, 25), cache) == ["NVDA"]


def test_a_missing_cache_file_makes_everything_stale_without_creating_it(tmp_path):
    missing = tmp_path / "holdings.duckdb"

    assert stale_tickers(["SPY"], date(2026, 9, 25), str(missing)) == ["SPY"]
    assert not missing.exists()


def test_only_the_stale_tickers_are_fetched(tmp_path, monkeypatch):
    """A five-holding portfolio with one new ticker must cost one ticker's
    worth of network, not five - and the cache must not slowly become a list
    of everything ever typed that all has to be refetched together.
    """
    cache = str(tmp_path / "holdings.duckdb")
    _make_cache(cache, {"SPY": "2026-09-01", "T": "2026-09-01", "OLD": "2024-01-02"})
    fetched = _stub_fetch(monkeypatch)

    refresh_holdings_cache(["SPY", "T", "NVDA"], date(2026, 9, 25), cache)

    assert fetched == [["NVDA"]]


def test_a_stale_ticker_nobody_asks_about_is_never_touched(tmp_path, monkeypatch):
    cache = str(tmp_path / "holdings.duckdb")
    _make_cache(cache, {"SPY": "2026-09-01", "OLD": "2024-01-02"})
    fetched = _stub_fetch(monkeypatch)

    refresh_holdings_cache(["SPY"], date(2026, 9, 25), cache)

    assert fetched == []


def test_force_makes_every_requested_ticker_stale(tmp_path, monkeypatch):
    """`--refresh-holdings`. The monthly rule keeps the returns current but
    lets the prices behind Total value age, so there has to be a way to say
    "no, now".
    """
    cache = str(tmp_path / "holdings.duckdb")
    _make_cache(cache, {"SPY": "2026-09-01", "T": "2026-09-01"})
    fetched = _stub_fetch(monkeypatch)

    assert stale_tickers(["SPY", "T"], date(2026, 9, 25), cache, force=True) == ["SPY", "T"]
    refresh_holdings_cache(["SPY", "T"], date(2026, 9, 25), cache, force=True)

    assert fetched == [["SPY", "T"]]


def test_a_null_monthly_return_does_not_count_as_cached(tmp_path):
    """`src/dataset/returns.py` writes a full month-by-ticker cross product,
    so a row can exist with a null return. Counting that as coverage would
    make the cache look fresh while holding nothing usable.
    """
    cache = str(tmp_path / "holdings.duckdb")
    con = duckdb.connect(cache)
    try:
        con.execute(
            "CREATE TABLE returns (rebalance_date DATE, ticker VARCHAR, monthly_return DOUBLE)"
        )
        con.execute("INSERT INTO returns VALUES (?, ?, NULL)", [date(2026, 9, 1), "SPY"])
    finally:
        con.close()

    assert stale_tickers(["SPY"], date(2026, 9, 25), cache) == ["SPY"]


# --- what the refresh reports back -----------------------------------------


def test_a_fresh_ticker_is_reported_valid_with_its_cached_currency(tmp_path, monkeypatch):
    """A caller cannot tell a cache hit from a cold fetch, so it can treat
    both the same way - which is what keeps `_resolve_holdings` from growing
    two branches.
    """
    cache = str(tmp_path / "holdings.duckdb")
    _make_cache(cache, {"1321.T": "2026-09-01"}, currency="JPY")
    _stub_fetch(monkeypatch)

    valid, invalid, currencies = refresh_holdings_cache(["1321.T"], date(2026, 9, 25), cache)

    assert valid == ["1321.T"]
    assert invalid == {}
    assert currencies == {"1321.T": "JPY"}


def test_a_mix_of_fresh_and_fetched_reports_both(tmp_path, monkeypatch):
    cache = str(tmp_path / "holdings.duckdb")
    _make_cache(cache, {"SPY": "2026-09-01"})
    _stub_fetch(monkeypatch, {"NVDA": "USD"})

    valid, _invalid, currencies = refresh_holdings_cache(
        ["SPY", "NVDA"], date(2026, 9, 25), cache
    )

    assert valid == ["NVDA", "SPY"]
    assert currencies == {"SPY": "USD", "NVDA": "USD"}


def test_cached_month_ends_reports_none_for_an_unknown_ticker(tmp_path):
    cache = str(tmp_path / "holdings.duckdb")
    _make_cache(cache, {"SPY": "2026-09-01"})

    assert cached_month_ends(["SPY", "NVDA"], cache) == {
        "SPY": date(2026, 9, 1),
        "NVDA": None,
    }


def test_asking_about_no_tickers_costs_nothing(tmp_path, monkeypatch):
    fetched = _stub_fetch(monkeypatch)
    cache = str(tmp_path / "holdings.duckdb")

    assert refresh_holdings_cache([], date(2026, 9, 25), cache) == ([], {}, {})
    assert fetched == []
    assert not (tmp_path / "holdings.duckdb").exists()
