"""Tests for the merge-write (upsert) layer added for the `user_provided`
selection: src/agentic_portfolio/dataset/prices.py's `upsert_prices_tables` /
`fetch_and_reshape_for_tickers` and src/agentic_portfolio/dataset/returns.py's
`upsert_returns_table` / `build_returns_for_tickers`.

Unlike `tests/test_dataset.py` (pure in-memory fixtures), these functions
exist precisely to merge into a real database without disturbing rows they
weren't asked about, so they are tested against small hand-built DuckDB
files under `tmp_path`. Per AGENTS.md no test here calls yfinance:
`fetch_price_history` is monkeypatched wherever the fetch path is involved.
"""

from unittest.mock import MagicMock

import duckdb
import pandas as pd

from agentic_portfolio.dataset.prices import (
    fetch_and_reshape_for_tickers,
    upsert_prices_tables,
    write_prices_tables,
)
from agentic_portfolio.dataset.returns import build_returns_for_tickers, upsert_returns_table
from agentic_portfolio.dataset.ticker_currency import load_ticker_currencies, upsert_ticker_currency_table

# ---------------------------------------------------------------------------
# upsert_prices_tables
# ---------------------------------------------------------------------------


def _prices(*rows: tuple[str, str, float]) -> pd.DataFrame:
    """(date, ticker, close) triples -> a long-format prices frame."""
    return pd.DataFrame(
        {
            "date": pd.to_datetime([r[0] for r in rows]),
            "ticker": [r[1] for r in rows],
            "close": [r[2] for r in rows],
            "adj_close": [r[2] for r in rows],
        }
    )


def _unresolved(*tickers: str) -> pd.DataFrame:
    return pd.DataFrame({"ticker": list(tickers), "reason": ["no data"] * len(tickers)})


def _read(db_path: str, sql: str) -> list[tuple]:
    con = duckdb.connect(db_path)
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


def test_upsert_prices_tables_creates_tables_when_absent(tmp_path):
    db_path = str(tmp_path / "fresh.duckdb")

    upsert_prices_tables(_prices(("2026-09-01", "AAPL", 100.0)), _unresolved(), ["AAPL"], db_path)

    assert _read(db_path, "SELECT ticker, close FROM prices") == [("AAPL", 100.0)]
    assert _read(db_path, "SELECT count(*) FROM unresolved_tickers") == [(0,)]


def test_upsert_prices_tables_replaces_only_named_tickers_leaves_others_untouched(tmp_path):
    db_path = str(tmp_path / "seeded.duckdb")
    write_prices_tables(
        _prices(("2026-09-01", "AAPL", 100.0), ("2026-09-01", "MSFT", 200.0)),
        _unresolved(),
        db_path,
    )

    upsert_prices_tables(_prices(("2026-09-02", "AAPL", 111.0)), _unresolved(), ["AAPL"], db_path)

    assert sorted(_read(db_path, "SELECT ticker, close FROM prices")) == [
        ("AAPL", 111.0),
        ("MSFT", 200.0),
    ]


def test_upsert_prices_tables_clears_stale_unresolved_row_when_ticker_now_resolves(tmp_path):
    db_path = str(tmp_path / "seeded.duckdb")
    write_prices_tables(pd.DataFrame(columns=["date", "ticker", "close", "adj_close"]), _unresolved("AAPL"), db_path)

    upsert_prices_tables(_prices(("2026-09-01", "AAPL", 100.0)), _unresolved(), ["AAPL"], db_path)

    assert _read(db_path, "SELECT count(*) FROM unresolved_tickers") == [(0,)]
    assert _read(db_path, "SELECT ticker FROM prices") == [("AAPL",)]


def test_upsert_prices_tables_clears_stale_price_rows_when_ticker_stops_resolving(tmp_path):
    db_path = str(tmp_path / "seeded.duckdb")
    write_prices_tables(_prices(("2026-09-01", "AAPL", 100.0)), _unresolved(), db_path)

    upsert_prices_tables(
        pd.DataFrame(columns=["date", "ticker", "close", "adj_close"]),
        _unresolved("AAPL"),
        ["AAPL"],
        db_path,
    )

    assert _read(db_path, "SELECT count(*) FROM prices") == [(0,)]
    assert _read(db_path, "SELECT ticker FROM unresolved_tickers") == [("AAPL",)]


def test_upsert_prices_tables_is_idempotent_for_an_unchanged_ticker(tmp_path):
    db_path = str(tmp_path / "fresh.duckdb")
    prices = _prices(("2026-09-01", "AAPL", 100.0))

    upsert_prices_tables(prices, _unresolved(), ["AAPL"], db_path)
    upsert_prices_tables(prices, _unresolved(), ["AAPL"], db_path)

    assert _read(db_path, "SELECT count(*) FROM prices") == [(1,)]


# ---------------------------------------------------------------------------
# upsert_ticker_currency_table / load_ticker_currencies
# ---------------------------------------------------------------------------


def _currencies(*rows: tuple[str, str, str, float]) -> pd.DataFrame:
    """(ticker, currency, quoted_currency, price_multiplier) tuples."""
    return pd.DataFrame(
        {
            "ticker": [r[0] for r in rows],
            "currency": [r[1] for r in rows],
            "quoted_currency": [r[2] for r in rows],
            "price_multiplier": [r[3] for r in rows],
        }
    )


def test_upsert_ticker_currency_table_creates_the_table_when_absent(tmp_path):
    db_path = str(tmp_path / "fresh.duckdb")

    upsert_ticker_currency_table(_currencies(("BARC.L", "GBP", "GBp", 0.01)), ["BARC.L"], db_path)

    assert _read(db_path, "SELECT ticker, currency, quoted_currency, price_multiplier FROM ticker_currency") == [
        ("BARC.L", "GBP", "GBp", 0.01)
    ]


def test_upsert_ticker_currency_table_replaces_only_named_tickers(tmp_path):
    db_path = str(tmp_path / "fresh.duckdb")
    upsert_ticker_currency_table(
        _currencies(("AAPL", "USD", "USD", 1.0), ("7203.T", "JPY", "JPY", 1.0)),
        ["AAPL", "7203.T"],
        db_path,
    )

    upsert_ticker_currency_table(_currencies(("AAPL", "USD", "USD", 1.0)), ["AAPL"], db_path)

    assert sorted(_read(db_path, "SELECT ticker, currency FROM ticker_currency")) == [
        ("7203.T", "JPY"),
        ("AAPL", "USD"),
    ]


def test_upsert_ticker_currency_table_is_idempotent_for_an_unchanged_ticker(tmp_path):
    db_path = str(tmp_path / "fresh.duckdb")
    row = _currencies(("7203.T", "JPY", "JPY", 1.0))

    upsert_ticker_currency_table(row, ["7203.T"], db_path)
    upsert_ticker_currency_table(row, ["7203.T"], db_path)

    assert _read(db_path, "SELECT count(*) FROM ticker_currency") == [(1,)]


def test_load_ticker_currencies_returns_the_normalized_currency(tmp_path):
    db_path = str(tmp_path / "fresh.duckdb")
    upsert_ticker_currency_table(
        _currencies(("BARC.L", "GBP", "GBp", 0.01), ("7203.T", "JPY", "JPY", 1.0)),
        ["BARC.L", "7203.T"],
        db_path,
    )

    assert load_ticker_currencies(["BARC.L", "7203.T"], db_path) == {"BARC.L": "GBP", "7203.T": "JPY"}


def test_load_ticker_currencies_without_the_table_returns_empty_not_an_error(tmp_path):
    """The normal case for the shared historical cache and every
    non-user_provided path: no table, so callers fall back to USD. This is
    what keeps the backtest path working with no changes at all.
    """
    db_path = str(tmp_path / "no-currency-table.duckdb")
    write_prices_tables(_prices(("2026-09-01", "AAPL", 100.0)), _unresolved(), db_path)

    assert load_ticker_currencies(["AAPL"], db_path) == {}


def test_load_ticker_currencies_with_no_tickers_makes_no_query(tmp_path):
    assert load_ticker_currencies([], str(tmp_path / "does-not-exist.duckdb")) == {}


def test_load_ticker_currencies_does_not_create_a_database(tmp_path):
    """A read must not have the side effect of creating what it reads from -
    otherwise merely asking about an absent database litters a stray file.
    """
    missing = tmp_path / "does-not-exist.duckdb"

    assert load_ticker_currencies(["AAPL"], str(missing)) == {}
    assert not missing.exists()


# ---------------------------------------------------------------------------
# fetch_and_reshape_for_tickers
# ---------------------------------------------------------------------------


def test_fetch_and_reshape_for_tickers_delegates_to_existing_primitives(monkeypatch):
    raw = pd.DataFrame(
        {
            ("Close", "AAPL"): [100.0],
            ("Adj Close", "AAPL"): [99.0],
        },
        index=pd.to_datetime(["2026-09-01"]),
    )
    raw.columns = pd.MultiIndex.from_tuples(raw.columns)
    fetch_spy = MagicMock(return_value=raw)
    monkeypatch.setattr("agentic_portfolio.dataset.prices.fetch_price_history", fetch_spy)

    long_prices, unresolved = fetch_and_reshape_for_tickers(["AAPL", "GHOST"], "2021-04-05", "2026-09-10")

    assert sorted(fetch_spy.call_args.args[0]) == ["AAPL", "GHOST"]
    assert list(long_prices["ticker"]) == ["AAPL"]
    assert list(unresolved["ticker"]) == ["GHOST"]
    assert "2021-04-05" in unresolved.loc[0, "reason"]


def test_fetch_and_reshape_for_tickers_translates_dotted_share_class_symbols(monkeypatch):
    """'BRK.B' must be fetched as 'BRK-B' but reported back under its
    original ticker string - the translation to_yfinance_symbol exists for.
    """
    raw = pd.DataFrame(
        {
            ("Close", "BRK-B"): [100.0],
            ("Adj Close", "BRK-B"): [99.0],
        },
        index=pd.to_datetime(["2026-09-01"]),
    )
    raw.columns = pd.MultiIndex.from_tuples(raw.columns)
    fetch_spy = MagicMock(return_value=raw)
    monkeypatch.setattr("agentic_portfolio.dataset.prices.fetch_price_history", fetch_spy)

    long_prices, unresolved = fetch_and_reshape_for_tickers(["BRK.B"], "2021-04-05", "2026-09-10")

    assert fetch_spy.call_args.args[0] == ["BRK-B"]
    assert list(long_prices["ticker"]) == ["BRK.B"]
    assert unresolved.empty


# ---------------------------------------------------------------------------
# upsert_returns_table / build_returns_for_tickers
# ---------------------------------------------------------------------------


def _returns(*rows: tuple[str, str, float]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "rebalance_date": pd.to_datetime([r[0] for r in rows]),
            "ticker": [r[1] for r in rows],
            "monthly_return": [r[2] for r in rows],
        }
    )


def test_upsert_returns_table_creates_table_when_absent(tmp_path):
    db_path = str(tmp_path / "fresh.duckdb")

    upsert_returns_table(_returns(("2026-09-01", "AAPL", 0.01)), ["AAPL"], db_path)

    assert _read(db_path, "SELECT ticker, monthly_return FROM returns") == [("AAPL", 0.01)]


def test_upsert_returns_table_replaces_only_named_tickers(tmp_path):
    db_path = str(tmp_path / "seeded.duckdb")
    upsert_returns_table(
        _returns(("2026-09-01", "AAPL", 0.01), ("2026-09-01", "MSFT", 0.02)),
        ["AAPL", "MSFT"],
        db_path,
    )

    upsert_returns_table(_returns(("2026-09-01", "AAPL", 0.05)), ["AAPL"], db_path)

    assert sorted(_read(db_path, "SELECT ticker, monthly_return FROM returns")) == [
        ("AAPL", 0.05),
        ("MSFT", 0.02),
    ]


def test_build_returns_for_tickers_computes_from_prices_and_leaves_other_tickers_alone(tmp_path):
    """The whole point of the ticker-scoped variant: it reads only the
    `prices` rows already in the database (no fetch, no membership table)
    and merges its result without disturbing another ticker's returns.
    """
    db_path = str(tmp_path / "session.duckdb")
    # Two months of prices for AAA, doubling month over month.
    upsert_prices_tables(
        _prices(("2026-07-01", "AAA", 100.0), ("2026-08-03", "AAA", 200.0)),
        _unresolved(),
        ["AAA"],
        db_path,
    )
    upsert_returns_table(_returns(("2026-08-03", "OTHER", 0.42)), ["OTHER"], db_path)

    returns = build_returns_for_tickers(["AAA"], db_path, start="2026-07-01", end="2026-08-31")

    assert set(returns["ticker"]) == {"AAA"}
    august = returns.loc[returns["rebalance_date"] == pd.Timestamp("2026-08-03"), "monthly_return"]
    assert august.iloc[0] == 1.0  # 200/100 - 1
    assert ("OTHER", 0.42) in _read(db_path, "SELECT ticker, monthly_return FROM returns")


def test_build_returns_for_tickers_survives_a_prices_table_with_no_rows(tmp_path):
    """The shape that happens when every ticker in a batch fails to resolve -
    a typo typed on its own, or a benchmark symbol that does not exist. The
    ticker gets null returns, which is what `attach_nearest_price` documents
    for a ticker with no price row.

    This is a regression test for a real crash: DuckDB's `fetchdf()` types an
    empty `ticker` column as `object` but a non-empty one as pandas' `str`,
    and `pd.merge_asof` refused to join across that difference with
    "MergeError: incompatible merge keys ... must be the same type". Every
    other test of this function stubs it out or supplies prices, so nothing
    caught it until a single unresolvable ticker was ingested on its own.
    """
    db_path = str(tmp_path / "session.duckdb")
    upsert_prices_tables(_prices(), _unresolved(("ZZZZ", "no data")), ["ZZZZ"], db_path)

    returns = build_returns_for_tickers(["ZZZZ"], db_path, start="2026-07-01", end="2026-08-31")

    assert set(returns["ticker"]) == {"ZZZZ"}
    assert returns["monthly_return"].isna().all()
