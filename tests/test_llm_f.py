"""Tests for src/agents/news.py's `fetch_headlines`,
src/agents/llm_f.py's `generate_signal`/`compute_decayed_score`, and
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
shape is verified without any LLM call. `generate_signal`'s LLM-call path
is exercised with `LLMFCrew` itself monkeypatched to a fake crew returning
a hand-built `HeadlineSentimentBatch`, per plans/08_consistency_review.md
finding 5's redesign away from a holistic LLM judgment call.
"""

from datetime import date

import duckdb
import pytest

from src.agents import llm_f
from src.agents.llm_f import compute_decayed_score, generate_signal
from src.agents.llm_f_schema import HeadlineSentiment, HeadlineSentimentBatch, SentimentSignal
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
    assert signal.score == 0.0


def test_compute_decayed_score_weights_recent_headline_more_than_stale_one():
    """Two headlines in the same month, one strongly positive dated at
    month-end (full weight), one strongly negative dated 7 days earlier
    (half-life away, so half weight): the recent positive headline should
    dominate, pulling the score positive overall.
    """
    headlines = [
        {"title": "old bad news", "publish_date": "2024-03-24"},
        {"title": "fresh good news", "publish_date": "2024-03-31"},
    ]
    sentiments = [
        HeadlineSentiment(index=0, positive_probability=0.0, negative_probability=0.9),
        HeadlineSentiment(index=1, positive_probability=0.9, negative_probability=0.0),
    ]

    score = compute_decayed_score(headlines, sentiments, month_end=date(2024, 3, 31))

    # weight(old) = 0.5 ** (7/7) = 0.5, weight(fresh) = 0.5 ** (0/7) = 1.0
    expected = (0.5 * -0.9 + 1.0 * 0.9) / (0.5 + 1.0)
    assert score == pytest.approx(expected)
    assert score > 0


def test_compute_decayed_score_matches_input_order_via_index_not_position():
    headlines = [
        {"title": "first", "publish_date": "2024-03-31"},
        {"title": "second", "publish_date": "2024-03-31"},
    ]
    # Deliberately returned out of order and reversed relative to `headlines`.
    sentiments = [
        HeadlineSentiment(index=1, positive_probability=1.0, negative_probability=0.0),
        HeadlineSentiment(index=0, positive_probability=0.0, negative_probability=1.0),
    ]

    score = compute_decayed_score(headlines, sentiments, month_end=date(2024, 3, 31))

    # Equal weights (both dated exactly on month_end): (-1.0 + 1.0) / 2 = 0.0.
    assert score == pytest.approx(0.0)


def test_compute_decayed_score_empty_headlines_returns_zero():
    assert compute_decayed_score([], [], month_end=date(2024, 3, 31)) == 0.0


def test_compute_decayed_score_raises_on_index_mismatch():
    headlines = [{"title": "only one", "publish_date": "2024-03-31"}]
    sentiments = [HeadlineSentiment(index=1, positive_probability=0.5, negative_probability=0.5)]

    with pytest.raises(ValueError, match="indices"):
        compute_decayed_score(headlines, sentiments, month_end=date(2024, 3, 31))


class _FakeTask:
    def __init__(self, batch):
        self.pydantic = batch


class _FakeCrew:
    def __init__(self, batch):
        self._batch = batch

    def kickoff(self, inputs):
        return _FakeTask(self._batch)


class _FakeLLMFCrew:
    def __init__(self, batch, model=None):
        self._batch = batch
        self.model = model

    def crew(self):
        return _FakeCrew(self._batch)


def test_generate_signal_derives_buy_signal_from_score_above_threshold(monkeypatch):
    batch = HeadlineSentimentBatch(
        headlines=[HeadlineSentiment(index=0, positive_probability=0.9, negative_probability=0.0)]
    )
    monkeypatch.setattr(llm_f, "LLMFCrew", lambda model: _FakeLLMFCrew(batch, model))

    signal = generate_signal("AAPL", 2024, 3, [{"title": "great news", "publish_date": "2024-03-31"}])

    assert signal.score == pytest.approx(0.9)
    assert signal.signal == "buy"


def test_generate_signal_derives_sell_signal_from_score_below_threshold(monkeypatch):
    batch = HeadlineSentimentBatch(
        headlines=[HeadlineSentiment(index=0, positive_probability=0.0, negative_probability=0.9)]
    )
    monkeypatch.setattr(llm_f, "LLMFCrew", lambda model: _FakeLLMFCrew(batch, model))

    signal = generate_signal("AAPL", 2024, 3, [{"title": "bad news", "publish_date": "2024-03-31"}])

    assert signal.score == pytest.approx(-0.9)
    assert signal.signal == "sell"


def test_generate_signal_derives_hold_signal_from_score_within_threshold(monkeypatch):
    batch = HeadlineSentimentBatch(
        headlines=[HeadlineSentiment(index=0, positive_probability=0.5, negative_probability=0.45)]
    )
    monkeypatch.setattr(llm_f, "LLMFCrew", lambda model: _FakeLLMFCrew(batch, model))

    signal = generate_signal("AAPL", 2024, 3, [{"title": "mixed news", "publish_date": "2024-03-31"}])

    assert signal.score == pytest.approx(0.05)
    assert signal.signal == "hold"


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
        return SentimentSignal(ticker=ticker, month=month_str, signal=signal, score=0.2 if headlines else 0.0)

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
