"""Tests for src/agentic_portfolio/dataset/ticker_ingestion.py's validate-and-ingest step for
arbitrary user-supplied tickers.

Per AGENTS.md no test here calls yfinance: the two functions in the chain
that perform network I/O - `fetch_and_reshape_for_tickers` (prices) and
`fetch_ticker_currencies` (the per-ticker currency lookup) - are
monkeypatched at their point of use, as are the write helpers, so these
tests cover the normalization, the valid/invalid split, the currency
classification, the pence normalization, and the
never-crash-on-a-bad-ticker contract without touching the network or a
database.
"""

from datetime import date
from unittest.mock import MagicMock

import pandas as pd
import pytest

from agentic_portfolio.dataset.ticker_currency import CURRENCY_LOOKUP_FAILED_REASON
from agentic_portfolio.dataset.ticker_ingestion import validate_and_ingest_tickers

AS_OF = date(2026, 9, 5)


def _prices(*tickers: str, close: float = 100.0) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": [pd.Timestamp("2026-09-01")] * len(tickers),
            "ticker": list(tickers),
            "close": [close] * len(tickers),
            "adj_close": [close] * len(tickers),
        }
    )


def _unresolved(*tickers: str) -> pd.DataFrame:
    return pd.DataFrame({"ticker": list(tickers), "reason": ["no data"] * len(tickers)})


def _patch_chain(monkeypatch, fetch_result, currencies: dict[str, str] | None = None):
    """Monkeypatch the collaborators ticker_ingestion calls, returning the
    price-fetch spy, the two write spies, and the currency-lookup spy.

    `currencies` maps ticker -> the RAW code yfinance would report (so pass
    `'GBp'` to exercise the pence path). Defaulting it to `'USD'` for every
    fetched ticker keeps the pre-currency tests reading unchanged.

    The dividend build is patched too - it performs network I/O of its own -
    and its spy is reachable as `_patch_chain.dividends` rather than through
    the return tuple, so every existing caller's unpacking keeps working.
    """
    fetch_spy = MagicMock(return_value=fetch_result) if fetch_result is not None else MagicMock()
    upsert_prices_spy = MagicMock()
    upsert_currency_spy = MagicMock()
    build_returns_spy = MagicMock()
    build_dividends_spy = MagicMock()

    def fake_currencies(tickers, *args, **kwargs):
        if currencies is None:
            return {t: "USD" for t in tickers}
        return {t: currencies[t] for t in tickers if t in currencies}

    currency_spy = MagicMock(side_effect=fake_currencies)
    monkeypatch.setattr("agentic_portfolio.dataset.ticker_ingestion.fetch_and_reshape_for_tickers", fetch_spy)
    monkeypatch.setattr("agentic_portfolio.dataset.ticker_ingestion.upsert_prices_tables", upsert_prices_spy)
    monkeypatch.setattr("agentic_portfolio.dataset.ticker_ingestion.upsert_ticker_currency_table", upsert_currency_spy)
    monkeypatch.setattr("agentic_portfolio.dataset.ticker_ingestion.build_returns_for_tickers", build_returns_spy)
    monkeypatch.setattr("agentic_portfolio.dataset.ticker_ingestion.fetch_ticker_currencies", currency_spy)
    monkeypatch.setattr(
        "agentic_portfolio.dataset.ticker_ingestion.build_dividends_for_tickers", build_dividends_spy
    )
    _patch_chain.dividends = build_dividends_spy
    return fetch_spy, upsert_prices_spy, build_returns_spy, currency_spy, upsert_currency_spy


def test_validate_and_ingest_tickers_empty_input_makes_no_network_call(monkeypatch):
    fetch_spy, upsert_prices_spy, build_returns_spy, currency_spy, _ = _patch_chain(monkeypatch, None)

    assert validate_and_ingest_tickers([], AS_OF, "unused.duckdb") == ([], {}, {})
    assert fetch_spy.called is False
    assert upsert_prices_spy.called is False
    assert build_returns_spy.called is False
    assert currency_spy.called is False


def test_validate_and_ingest_tickers_blank_only_input_makes_no_network_call(monkeypatch):
    fetch_spy, _, _, currency_spy, _ = _patch_chain(monkeypatch, None)

    assert validate_and_ingest_tickers(["  ", ""], AS_OF, "unused.duckdb") == ([], {}, {})
    assert fetch_spy.called is False
    assert currency_spy.called is False


def test_validate_and_ingest_tickers_splits_valid_and_invalid(monkeypatch):
    _patch_chain(monkeypatch, (_prices("AAPL"), _unresolved("ZZZZ")))

    valid, invalid, _ = validate_and_ingest_tickers(["AAPL", "ZZZZ"], AS_OF, "fixture.duckdb")

    assert valid == ["AAPL"]
    assert list(invalid) == ["ZZZZ"]
    assert invalid["ZZZZ"] == "no data"


def test_validate_and_ingest_tickers_all_valid_returns_no_invalid(monkeypatch):
    _patch_chain(monkeypatch, (_prices("AAPL", "MSFT"), _unresolved()))

    valid, invalid, _ = validate_and_ingest_tickers(["AAPL", "MSFT"], AS_OF, "fixture.duckdb")

    assert valid == ["AAPL", "MSFT"]
    assert invalid == {}


def test_validate_and_ingest_tickers_dedupes_and_upcases_before_fetching(monkeypatch):
    fetch_spy, upsert_prices_spy, build_returns_spy, _, _ = _patch_chain(
        monkeypatch, (_prices("AAPL", "MSFT"), _unresolved())
    )

    valid, _, _ = validate_and_ingest_tickers(["aapl", "AAPL", " msft "], AS_OF, "fixture.duckdb")

    assert fetch_spy.call_args.args[0] == ["AAPL", "MSFT"]
    assert upsert_prices_spy.call_args.args[2] == ["AAPL", "MSFT"]
    assert build_returns_spy.call_args.args[0] == ["AAPL", "MSFT"]
    assert valid == ["AAPL", "MSFT"]


def test_validate_and_ingest_tickers_ingests_prices_then_currency_then_returns(monkeypatch):
    """`build_returns_for_tickers` reads the `prices` table, so the price
    upsert must already have happened when it runs. The dividend build is
    ordered too - and patched, because it performs network I/O of its own and
    `upsert_dividends_tables` would otherwise create the relative
    `fixture.duckdb` this test names right in the repository root.
    """
    call_order: list[str] = []
    monkeypatch.setattr(
        "agentic_portfolio.dataset.ticker_ingestion.fetch_and_reshape_for_tickers",
        lambda *a, **k: (_prices("AAPL"), _unresolved()),
    )
    monkeypatch.setattr(
        "agentic_portfolio.dataset.ticker_ingestion.fetch_ticker_currencies", lambda tickers, *a, **k: {"AAPL": "USD"}
    )
    monkeypatch.setattr(
        "agentic_portfolio.dataset.ticker_ingestion.upsert_prices_tables",
        lambda *a, **k: call_order.append("prices"),
    )
    monkeypatch.setattr(
        "agentic_portfolio.dataset.ticker_ingestion.upsert_ticker_currency_table",
        lambda *a, **k: call_order.append("currency"),
    )
    monkeypatch.setattr(
        "agentic_portfolio.dataset.ticker_ingestion.build_returns_for_tickers",
        lambda *a, **k: call_order.append("returns"),
    )
    monkeypatch.setattr(
        "agentic_portfolio.dataset.ticker_ingestion.build_dividends_for_tickers",
        lambda *a, **k: call_order.append("dividends"),
    )

    validate_and_ingest_tickers(["AAPL"], AS_OF, "fixture.duckdb")

    # Dividends land last: they need the multipliers the currency lookup
    # produced, and committing them after the prices they will be divided by
    # keeps the pair consistent.
    assert call_order == ["prices", "currency", "returns", "dividends"]


def test_validate_and_ingest_tickers_handles_symbol_collision_gracefully(monkeypatch):
    _, upsert_prices_spy, build_returns_spy, currency_spy, _ = _patch_chain(monkeypatch, None)
    monkeypatch.setattr(
        "agentic_portfolio.dataset.ticker_ingestion.fetch_and_reshape_for_tickers",
        MagicMock(side_effect=ValueError("both map to yfinance symbol 'BRK-B'")),
    )

    valid, invalid, currencies = validate_and_ingest_tickers(["BRK.B", "BRK-B"], AS_OF, "fixture.duckdb")

    assert valid == []
    assert sorted(invalid) == ["BRK-B", "BRK.B"]
    assert all("BRK-B" in reason for reason in invalid.values())
    assert currencies == {}
    assert upsert_prices_spy.called is False
    assert build_returns_spy.called is False
    assert currency_spy.called is False


def test_validate_and_ingest_tickers_fetch_window_covers_the_returns_lookback(monkeypatch):
    fetch_spy, _, _, _, _ = _patch_chain(monkeypatch, (_prices("AAPL"), _unresolved()))

    validate_and_ingest_tickers(["AAPL"], AS_OF, "fixture.duckdb")

    start, end = fetch_spy.call_args.args[1], fetch_spy.call_args.args[2]
    assert start == "2021-04-05"  # 65 months before 2026-09-05
    assert end == "2026-09-10"  # as_of + 5 days, so as_of itself is inside the window


# ---------------------------------------------------------------------------
# currency classification
# ---------------------------------------------------------------------------


def test_validate_and_ingest_tickers_returns_the_normalized_currency(monkeypatch):
    """The caller must see 'GBP', never the 'GBp' yfinance reports, since the
    stored prices have already been scaled into that major unit.
    """
    _patch_chain(monkeypatch, (_prices("BARC.L"), _unresolved()), currencies={"BARC.L": "GBp"})

    _, _, currencies = validate_and_ingest_tickers(["BARC.L"], AS_OF, "fixture.duckdb")

    assert currencies == {"BARC.L": "GBP"}


def test_validate_and_ingest_tickers_looks_up_currency_only_for_resolved_tickers(monkeypatch):
    """A typo already failed the price fetch, so spending a request to ask
    what currency it trades in would be pure waste.
    """
    _, _, _, currency_spy, _ = _patch_chain(
        monkeypatch, (_prices("AAPL"), _unresolved("ZZZZ")), currencies={"AAPL": "USD"}
    )

    validate_and_ingest_tickers(["AAPL", "ZZZZ"], AS_OF, "fixture.duckdb")

    assert currency_spy.call_args.args[0] == ["AAPL"]


def test_validate_and_ingest_tickers_scales_a_pence_quote_into_pounds(monkeypatch):
    """Both price columns are scaled, and a dollar ticker fetched in the same
    batch is left alone.
    """
    fetched = pd.concat([_prices("BARC.L", close=184.12), _prices("AAPL", close=168.84)])
    _, upsert_prices_spy, _, _, _ = _patch_chain(
        monkeypatch, (fetched, _unresolved()), currencies={"BARC.L": "GBp", "AAPL": "USD"}
    )

    validate_and_ingest_tickers(["BARC.L", "AAPL"], AS_OF, "fixture.duckdb")

    written = upsert_prices_spy.call_args.args[0].set_index("ticker")
    assert written.loc["BARC.L", "close"] == pytest.approx(1.8412)
    assert written.loc["BARC.L", "adj_close"] == pytest.approx(1.8412)
    assert written.loc["AAPL", "close"] == pytest.approx(168.84)
    assert written.loc["AAPL", "adj_close"] == pytest.approx(168.84)


def test_validate_and_ingest_tickers_records_the_quoted_currency_and_multiplier(monkeypatch):
    _, _, _, _, upsert_currency_spy = _patch_chain(
        monkeypatch, (_prices("BARC.L"), _unresolved()), currencies={"BARC.L": "GBp"}
    )

    validate_and_ingest_tickers(["BARC.L"], AS_OF, "fixture.duckdb")

    written = upsert_currency_spy.call_args.args[0].set_index("ticker")
    assert written.loc["BARC.L", "currency"] == "GBP"
    assert written.loc["BARC.L", "quoted_currency"] == "GBp"
    assert written.loc["BARC.L", "price_multiplier"] == pytest.approx(0.01)
    # Deleted-and-replaced by the same requested list as the price upsert.
    assert upsert_currency_spy.call_args.args[1] == ["BARC.L"]


def test_validate_and_ingest_tickers_rejects_a_ticker_whose_currency_is_unknown(monkeypatch):
    """Assuming dollars for a ticker we tried and failed to classify is
    exactly the silent-mixing trap the currency work exists to close, so it
    is reported invalid instead.
    """
    _patch_chain(monkeypatch, (_prices("AAPL", "MYSTERY"), _unresolved()), currencies={"AAPL": "USD"})

    valid, invalid, currencies = validate_and_ingest_tickers(["AAPL", "MYSTERY"], AS_OF, "fixture.duckdb")

    assert valid == ["AAPL"]
    assert invalid["MYSTERY"] == CURRENCY_LOOKUP_FAILED_REASON
    assert currencies == {"AAPL": "USD"}


def test_validate_and_ingest_tickers_ingests_dividends_on_the_same_pass(monkeypatch):
    """Without this, a `user_provided` pool would have prices in its session
    snapshot and no yields, so `--min-annual-dividend` would be refused for
    every ticker the person typed - correctly, since a missing yield is not a
    zero yield, but uselessly.
    """
    _patch_chain(monkeypatch, (_prices("AAPL"), _unresolved()))
    validate_and_ingest_tickers(["AAPL"], AS_OF, "session.duckdb")

    spy = _patch_chain.dividends
    assert spy.call_count == 1
    assert spy.call_args.args[0] == ["AAPL"]
    assert spy.call_args.args[1] == "session.duckdb"


def test_validate_and_ingest_tickers_scales_a_pence_dividend_like_its_prices(monkeypatch):
    """The dividend and the price must share a unit, or a 3% yielder reports
    at 310% and satisfies any floor a person could type.
    """
    _patch_chain(
        monkeypatch,
        (_prices("BARC.L"), _unresolved()),
        currencies={"BARC.L": "GBp"},
    )
    validate_and_ingest_tickers(["BARC.L"], AS_OF, "session.duckdb")

    multipliers = _patch_chain.dividends.call_args.kwargs["multipliers"]
    assert multipliers["BARC.L"] == pytest.approx(0.01)


def test_validate_and_ingest_tickers_records_no_dividend_coverage_for_an_invalid_ticker(
    monkeypatch,
):
    _patch_chain(
        monkeypatch,
        (_prices("AAPL"), _unresolved("BOGUS")),
        currencies={"AAPL": "USD"},
    )
    validate_and_ingest_tickers(["AAPL", "BOGUS"], AS_OF, "session.duckdb")

    assert "BOGUS" in _patch_chain.dividends.call_args.kwargs["unresolved"]


def test_validate_and_ingest_tickers_survives_a_failing_dividend_fetch(monkeypatch):
    """A dividend gap is reported by name downstream; raising here would
    throw away the prices, returns and currencies already stored and reject
    a perfectly good ticker.
    """
    _patch_chain(monkeypatch, (_prices("AAPL"), _unresolved()))
    monkeypatch.setattr(
        "agentic_portfolio.dataset.ticker_ingestion.build_dividends_for_tickers",
        MagicMock(side_effect=RuntimeError("yfinance exploded")),
    )
    valid, invalid, currencies = validate_and_ingest_tickers(
        ["AAPL"], AS_OF, "session.duckdb"
    )
    assert valid == ["AAPL"]
    assert invalid == {}


def test_validate_and_ingest_tickers_skips_dividends_when_the_fetch_is_declined(monkeypatch):
    """`--no-dividend-fetch` should not pay for data the report is about to
    ignore, and a `user_provided` run reaches its dividends through here
    rather than through the snapshot build.
    """
    _patch_chain(monkeypatch, (_prices("AAPL"), _unresolved()))

    valid, invalid, currencies = validate_and_ingest_tickers(
        ["AAPL"], AS_OF, "session.duckdb", fetch_dividends=False
    )

    _patch_chain.dividends.assert_not_called()
    # Everything else still happened: the ticker is usable, just without
    # dividend figures.
    assert valid == ["AAPL"]
    assert invalid == {}
    assert currencies == {"AAPL": "USD"}
