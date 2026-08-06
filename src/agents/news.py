"""Headline fetching for LLM-F, per the hybrid-source decision recorded in
`plans/03_llm_f_agent.md`'s Decision Log.

`yfinance.Ticker(ticker).news` has zero historical reach — verified live
(see Surprises & Discoveries): every item it returns is dated the day of
the call, never older, so it cannot answer any 2020-2024 backtest month by
itself. `fetch_headlines` therefore checks whether the requested
`(year, month)` falls inside the Hugging Face archive's measured coverage
range (`src/dataset/news_archive.py`'s `news_articles_hf` table in
`data/portfolio.duckdb`) and, if so, reads from that archive — including
returning an empty list when the archive covers that month but has nothing
for this specific ticker, which is the common case (only ~6.5% of
(ticker, month) pairs have any archive article) and must not trigger a
fallback to yfinance, since yfinance could not possibly help for a
historical month either. Only requests for months outside the archive's
covered range fall back to the live `yfinance.news` call.
"""

from __future__ import annotations

from datetime import date

import duckdb
import yfinance as yf

from src.config.settings import settings


def _archive_covers_month(year: int, month: int, db_path: str) -> bool:
    """Whether `year`-`month` falls within `news_articles_hf`'s actual
    min/max `publish_date`, read from the table itself rather than
    hardcoding the range measured in Surprises & Discoveries — the archive
    could be re-ingested later with different coverage.
    """
    con = duckdb.connect(db_path, read_only=True)
    try:
        earliest, latest = con.execute(
            "SELECT strftime(min(publish_date), '%Y-%m'), strftime(max(publish_date), '%Y-%m') "
            "FROM news_articles_hf"
        ).fetchone()
    finally:
        con.close()
    target = f"{year:04d}-{month:02d}"
    return earliest <= target <= latest


def _fetch_from_archive(ticker: str, year: int, month: int, limit: int, db_path: str) -> list[dict]:
    """Read this ticker's archived headlines for the given month, oldest
    first, capped at `limit`. An empty result is valid and expected — most
    (ticker, month) pairs have no archive coverage.
    """
    con = duckdb.connect(db_path, read_only=True)
    try:
        rows = con.execute(
            "SELECT title, publish_date FROM news_articles_hf "
            "WHERE symbol = ? AND strftime(publish_date, '%Y-%m') = ? "
            "ORDER BY publish_date LIMIT ?",
            [ticker, f"{year:04d}-{month:02d}", limit],
        ).fetchall()
    finally:
        con.close()
    return [{"title": title, "publish_date": publish_date.isoformat()} for title, publish_date in rows]


def _fetch_from_yfinance(ticker: str, year: int, month: int, limit: int) -> list[dict]:
    """Read this ticker's live headlines for the given month from
    `yfinance.Ticker(ticker).news`. The publish date lives at
    `item["content"]["pubDate"]` (verified live, ISO 8601 with a `Z`
    suffix) — if either key is absent, that means yfinance's response
    shape has changed again and this must fail loudly naming what it did
    find, rather than silently returning an empty list that looks
    identical to "no news that month".
    """
    target = f"{year:04d}-{month:02d}"
    matched: list[dict] = []
    for item in yf.Ticker(ticker).news:
        content = item.get("content")
        if content is None:
            raise ValueError(f"yfinance news item has no 'content' key; keys found: {sorted(item.keys())}")
        publish_date = content.get("pubDate")
        if publish_date is None:
            raise ValueError(
                f"yfinance news item's 'content' has no 'pubDate' key; keys found: {sorted(content.keys())}"
            )
        if publish_date[:7] == target:
            matched.append({"title": content.get("title", ""), "publish_date": publish_date})
        if len(matched) >= limit:
            break
    return matched


def fetch_headlines(ticker: str, year: int, month: int, limit: int = 20, db_path: str = settings.db_path) -> list[dict]:
    """Headlines for `ticker` published in the given `year`/`month`, at
    most `limit` items, each a dict with at least `title` and
    `publish_date`. Reads the historical archive for months it covers
    (including returning `[]` when the archive covers the month but not
    this ticker); falls back to live `yfinance.news` only for months
    outside the archive's coverage.
    """
    if _archive_covers_month(year, month, db_path):
        return _fetch_from_archive(ticker, year, month, limit, db_path)
    return _fetch_from_yfinance(ticker, year, month, limit)
