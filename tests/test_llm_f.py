"""Tests for src/agents/news.py's `fetch_headlines`,
src/agents/llm_f.py's `generate_signal`, and
src/agents/llm_f_signals.py's `screen_month`.

Per AGENTS.md, no test here calls yfinance's live API or any LLM. The
archive path is exercised against a small hand-built `news_articles_hf`
fixture table in a temp DuckDB file; the yfinance fallback path is
exercised against a hand-built fixture shaped like yfinance's real
response (`item["content"]["pubDate"]`/`["title"]`, per
plans/03_llm_f_agent.md's Surprises & Discoveries), with `yf.Ticker`
patched so no network call happens. `screen_month`'s tests monkeypatch
`generate_signal` itself (mirroring how `tests/test_llm_s.py` tests
`screen` against a fixture `factors` table) so its DB-plumbing/DataFrame
shape is verified without any LLM call.
"""

import duckdb
import pytest

from src.agents.llm_f import generate_signal
from src.agents.llm_f_schema import SentimentSignal
from src.agents.llm_f_signals import screen_month
from src.agents.news import fetch_headlines


def _build_archive_db(db_path, rows: list[tuple[str, str, str]]) -> None:
    """`rows` is a list of (symbol, title, publish_date) tuples."""
    con = duckdb.connect(str(db_path))
    con.execute(
        "CREATE TABLE news_articles_hf "
        "(id_ BIGINT, links VARCHAR, symbol VARCHAR, company VARCHAR, "
        "title VARCHAR, body VARCHAR, publish_date TIMESTAMP)"
    )
    for i, (symbol, title, publish_date) in enumerate(rows):
        con.execute(
            "INSERT INTO news_articles_hf VALUES (?, '', ?, '', ?, '', ?)",
            [i, symbol, title, publish_date],
        )
    con.close()


def test_fetch_headlines_reads_archive_for_covered_month_and_respects_limit(tmp_path):
    db_path = tmp_path / "test.duckdb"
    _build_archive_db(
        db_path,
        [
            ("AAPL", "in month 1", "2024-03-05"),
            ("AAPL", "in month 2", "2024-03-20"),
            ("AAPL", "in month 3", "2024-03-25"),
            ("AAPL", "outside month", "2024-04-01"),
            ("MSFT", "different ticker", "2024-03-10"),
        ],
    )

    headlines = fetch_headlines("AAPL", 2024, 3, limit=2, db_path=str(db_path))

    assert len(headlines) == 2
    assert all(h["publish_date"].startswith("2024-03") for h in headlines)
    assert all(h["title"] not in ("outside month", "different ticker") for h in headlines)


def test_fetch_headlines_archive_covers_month_but_ticker_has_none_returns_empty(tmp_path):
    """The common case: the archive's date range covers this month, but
    this specific ticker has no article in it - a real, expected "no
    news" result, not a reason to fall back to yfinance.
    """
    db_path = tmp_path / "test.duckdb"
    _build_archive_db(db_path, [("MSFT", "msft news", "2024-03-10")])

    headlines = fetch_headlines("AAPL", 2024, 3, db_path=str(db_path))

    assert headlines == []


def test_fetch_headlines_falls_back_to_yfinance_outside_archive_range(tmp_path, monkeypatch):
    db_path = tmp_path / "test.duckdb"
    _build_archive_db(db_path, [("AAPL", "archived news", "2024-03-10")])  # archive only covers 2024-03

    class FakeTicker:
        def __init__(self, ticker):
            self.ticker = ticker

        @property
        def news(self):
            return [
                {"content": {"title": "in month one", "pubDate": "2025-06-15T00:00:00Z"}},
                {"content": {"title": "in month two", "pubDate": "2025-06-20T00:00:00Z"}},
                {"content": {"title": "outside month", "pubDate": "2025-07-01T00:00:00Z"}},
            ]

    monkeypatch.setattr("src.agents.news.yf.Ticker", FakeTicker)

    headlines = fetch_headlines("AAPL", 2025, 6, db_path=str(db_path))

    assert {h["title"] for h in headlines} == {"in month one", "in month two"}


def test_fetch_headlines_yfinance_fallback_respects_limit(tmp_path, monkeypatch):
    db_path = tmp_path / "test.duckdb"
    _build_archive_db(db_path, [("AAPL", "archived news", "2024-03-10")])

    class FakeTicker:
        def __init__(self, ticker):
            pass

        @property
        def news(self):
            return [
                {"content": {"title": f"item {i}", "pubDate": "2025-06-01T00:00:00Z"}} for i in range(5)
            ]

    monkeypatch.setattr("src.agents.news.yf.Ticker", FakeTicker)

    headlines = fetch_headlines("AAPL", 2025, 6, limit=2, db_path=str(db_path))

    assert len(headlines) == 2


def test_fetch_headlines_yfinance_fallback_raises_clear_error_when_content_key_missing(tmp_path, monkeypatch):
    db_path = tmp_path / "test.duckdb"
    _build_archive_db(db_path, [("AAPL", "archived news", "2024-03-10")])

    class FakeTicker:
        def __init__(self, ticker):
            pass

        @property
        def news(self):
            return [{"unexpected_key": "no content here"}]

    monkeypatch.setattr("src.agents.news.yf.Ticker", FakeTicker)

    with pytest.raises(ValueError, match="content"):
        fetch_headlines("AAPL", 2025, 6, db_path=str(db_path))


def test_generate_signal_returns_hold_without_llm_call_when_headlines_empty():
    signal = generate_signal("AAPL", 2024, 3, [])

    assert isinstance(signal, SentimentSignal)
    assert signal.ticker == "AAPL"
    assert signal.month == "2024-03"
    assert signal.signal == "hold"
    assert signal.confidence == 0.0


def _build_membership_and_archive_db(db_path, membership_rows, archive_rows) -> None:
    con = duckdb.connect(str(db_path))
    con.execute("CREATE TABLE sp500_membership (rebalance_date DATE, ticker VARCHAR, security VARCHAR)")
    for rebalance_date, ticker in membership_rows:
        con.execute(
            "INSERT INTO sp500_membership VALUES (?, ?, '')", [rebalance_date, ticker]
        )
    con.execute(
        "CREATE TABLE news_articles_hf "
        "(id_ BIGINT, links VARCHAR, symbol VARCHAR, company VARCHAR, "
        "title VARCHAR, body VARCHAR, publish_date TIMESTAMP)"
    )
    for i, (symbol, title, publish_date) in enumerate(archive_rows):
        con.execute(
            "INSERT INTO news_articles_hf VALUES (?, '', ?, '', ?, '', ?)",
            [i, symbol, title, publish_date],
        )
    con.close()


def test_screen_month_resolves_rebalance_date_and_returns_ticker_signal_frame(tmp_path, monkeypatch):
    db_path = tmp_path / "test.duckdb"
    _build_membership_and_archive_db(
        db_path,
        membership_rows=[("2024-03-01", "AAPL"), ("2024-03-01", "MSFT")],
        archive_rows=[("AAPL", "some AAPL news", "2024-03-05")],
    )

    def fake_generate_signal(ticker, year, month, headlines, model=None):
        month_str = f"{year:04d}-{month:02d}"
        signal = "buy" if headlines else "hold"
        return SentimentSignal(ticker=ticker, month=month_str, signal=signal, confidence=0.5, rationale="x")

    monkeypatch.setattr("src.agents.llm_f_signals.generate_signal", fake_generate_signal)

    result = screen_month(2024, 3, db_path=str(db_path))

    assert list(result.columns) == ["ticker", "signal"]
    assert result.set_index("ticker")["signal"].to_dict() == {"AAPL": "buy", "MSFT": "hold"}


def test_screen_month_raises_clear_error_when_no_rebalance_date_matches(tmp_path):
    db_path = tmp_path / "test.duckdb"
    _build_membership_and_archive_db(
        db_path, membership_rows=[("2024-03-01", "AAPL")], archive_rows=[]
    )

    with pytest.raises(ValueError, match="2020-01"):
        screen_month(2020, 1, db_path=str(db_path))
