"""Tests for src/dataset/ticker_ingestion.py's validate-and-ingest step for
arbitrary user-supplied tickers.

Per AGENTS.md no test here calls yfinance: `fetch_and_reshape_for_tickers`
(the one function in the chain that performs network I/O) is monkeypatched
at its point of use, as are the two write helpers, so these tests cover the
normalization, valid/invalid split, and never-crash-on-a-bad-ticker
contract without touching the network or a database.
"""

from datetime import date
from unittest.mock import MagicMock

import pandas as pd

from src.dataset.ticker_ingestion import validate_and_ingest_tickers

AS_OF = date(2026, 9, 5)


def _prices(*tickers: str) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": [pd.Timestamp("2026-09-01")] * len(tickers),
            "ticker": list(tickers),
            "close": [100.0] * len(tickers),
            "adj_close": [100.0] * len(tickers),
        }
    )


def _unresolved(*tickers: str) -> pd.DataFrame:
    return pd.DataFrame({"ticker": list(tickers), "reason": ["no data"] * len(tickers)})


def _patch_chain(monkeypatch, fetch_result):
    """Monkeypatch the three collaborators ticker_ingestion calls, returning
    the spies for the two writers plus the fetch spy.
    """
    fetch_spy = MagicMock(return_value=fetch_result) if fetch_result is not None else MagicMock()
    upsert_prices_spy = MagicMock()
    build_returns_spy = MagicMock()
    monkeypatch.setattr("src.dataset.ticker_ingestion.fetch_and_reshape_for_tickers", fetch_spy)
    monkeypatch.setattr("src.dataset.ticker_ingestion.upsert_prices_tables", upsert_prices_spy)
    monkeypatch.setattr("src.dataset.ticker_ingestion.build_returns_for_tickers", build_returns_spy)
    return fetch_spy, upsert_prices_spy, build_returns_spy


def test_validate_and_ingest_tickers_empty_input_makes_no_network_call(monkeypatch):
    fetch_spy, upsert_prices_spy, build_returns_spy = _patch_chain(monkeypatch, None)

    assert validate_and_ingest_tickers([], AS_OF, "unused.duckdb") == ([], {})
    assert fetch_spy.called is False
    assert upsert_prices_spy.called is False
    assert build_returns_spy.called is False


def test_validate_and_ingest_tickers_blank_only_input_makes_no_network_call(monkeypatch):
    fetch_spy, _, _ = _patch_chain(monkeypatch, None)

    assert validate_and_ingest_tickers(["  ", ""], AS_OF, "unused.duckdb") == ([], {})
    assert fetch_spy.called is False


def test_validate_and_ingest_tickers_splits_valid_and_invalid(monkeypatch):
    _patch_chain(monkeypatch, (_prices("AAPL"), _unresolved("ZZZZ")))

    valid, invalid = validate_and_ingest_tickers(["AAPL", "ZZZZ"], AS_OF, "fixture.duckdb")

    assert valid == ["AAPL"]
    assert list(invalid) == ["ZZZZ"]
    assert invalid["ZZZZ"] == "no data"


def test_validate_and_ingest_tickers_all_valid_returns_no_invalid(monkeypatch):
    _patch_chain(monkeypatch, (_prices("AAPL", "MSFT"), _unresolved()))

    valid, invalid = validate_and_ingest_tickers(["AAPL", "MSFT"], AS_OF, "fixture.duckdb")

    assert valid == ["AAPL", "MSFT"]
    assert invalid == {}


def test_validate_and_ingest_tickers_dedupes_and_upcases_before_fetching(monkeypatch):
    fetch_spy, upsert_prices_spy, build_returns_spy = _patch_chain(
        monkeypatch, (_prices("AAPL", "MSFT"), _unresolved())
    )

    valid, _ = validate_and_ingest_tickers(["aapl", "AAPL", " msft "], AS_OF, "fixture.duckdb")

    assert fetch_spy.call_args.args[0] == ["AAPL", "MSFT"]
    assert upsert_prices_spy.call_args.args[2] == ["AAPL", "MSFT"]
    assert build_returns_spy.call_args.args[0] == ["AAPL", "MSFT"]
    assert valid == ["AAPL", "MSFT"]


def test_validate_and_ingest_tickers_ingests_prices_before_returns(monkeypatch):
    """build_returns_for_tickers reads the `prices` table, so the price
    upsert must already have happened when it runs.
    """
    call_order: list[str] = []
    monkeypatch.setattr(
        "src.dataset.ticker_ingestion.fetch_and_reshape_for_tickers",
        lambda *a, **k: (_prices("AAPL"), _unresolved()),
    )
    monkeypatch.setattr(
        "src.dataset.ticker_ingestion.upsert_prices_tables",
        lambda *a, **k: call_order.append("prices"),
    )
    monkeypatch.setattr(
        "src.dataset.ticker_ingestion.build_returns_for_tickers",
        lambda *a, **k: call_order.append("returns"),
    )

    validate_and_ingest_tickers(["AAPL"], AS_OF, "fixture.duckdb")

    assert call_order == ["prices", "returns"]


def test_validate_and_ingest_tickers_handles_symbol_collision_gracefully(monkeypatch):
    _, upsert_prices_spy, build_returns_spy = _patch_chain(monkeypatch, None)
    monkeypatch.setattr(
        "src.dataset.ticker_ingestion.fetch_and_reshape_for_tickers",
        MagicMock(side_effect=ValueError("both map to yfinance symbol 'BRK-B'")),
    )

    valid, invalid = validate_and_ingest_tickers(["BRK.B", "BRK-B"], AS_OF, "fixture.duckdb")

    assert valid == []
    assert sorted(invalid) == ["BRK-B", "BRK.B"]
    assert all("BRK-B" in reason for reason in invalid.values())
    assert upsert_prices_spy.called is False
    assert build_returns_spy.called is False


def test_validate_and_ingest_tickers_fetch_window_covers_the_returns_lookback(monkeypatch):
    fetch_spy, _, _ = _patch_chain(monkeypatch, (_prices("AAPL"), _unresolved()))

    validate_and_ingest_tickers(["AAPL"], AS_OF, "fixture.duckdb")

    start, end = fetch_spy.call_args.args[1], fetch_spy.call_args.args[2]
    assert start == "2021-04-05"  # 65 months before 2026-09-05
    assert end == "2026-09-10"  # as_of + 5 days, so as_of itself is inside the window
