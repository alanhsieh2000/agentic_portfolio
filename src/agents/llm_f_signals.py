"""Run LLM-F for every ticker in one month's S&P 500 membership,
producing LLM-F's buy/sell/hold signal set for that month.
"""

from __future__ import annotations

import logging
from datetime import date

import duckdb
import pandas as pd

from src.agents.llm_f import generate_signal
from src.agents.news import fetch_headlines
from src.config.settings import settings

logger = logging.getLogger(__name__)


def _resolve_rebalance_date(year: int, month: int, db_path: str) -> date:
    """The `sp500_membership` rebalance date whose year/month matches
    `year`/`month`. Raises if none exists - calling code asked for a
    month this project has no membership snapshot for.
    """
    con = duckdb.connect(db_path, read_only=True)
    try:
        result = con.execute(
            "SELECT DISTINCT rebalance_date FROM sp500_membership "
            "WHERE extract(year from rebalance_date) = ? AND extract(month from rebalance_date) = ?",
            [year, month],
        ).fetchall()
    finally:
        con.close()
    if not result:
        raise ValueError(f"no sp500_membership rebalance_date found for {year:04d}-{month:02d}")
    return result[0][0]


def screen_month(year: int, month: int, db_path: str = settings.db_path) -> pd.DataFrame:
    """LLM-F's buy/sell/hold signal for every ticker in the S&P 500 as of
    the rebalance date falling in `year`-`month`. Makes one
    `fetch_headlines` call and, for tickers with any headlines, one
    `generate_signal` LLM call, per ticker - a ~500-ticker universe means
    this can make dozens to hundreds of LLM calls, so progress is logged
    periodically rather than running silently for what could be several
    minutes. Returns a DataFrame with columns `ticker`, `signal`.
    """
    rebalance_date = _resolve_rebalance_date(year, month, db_path)

    con = duckdb.connect(db_path, read_only=True)
    try:
        tickers = [
            row[0]
            for row in con.execute(
                "SELECT ticker FROM sp500_membership WHERE rebalance_date = ? ORDER BY ticker",
                [rebalance_date],
            ).fetchall()
        ]
    finally:
        con.close()

    rows = []
    for i, ticker in enumerate(tickers, start=1):
        headlines = fetch_headlines(ticker, year, month, db_path=db_path)
        signal = generate_signal(ticker, year, month, headlines)
        rows.append({"ticker": ticker, "signal": signal.signal})
        if i % 50 == 0:
            logger.info("screen_month(%04d-%02d): processed %d/%d tickers", year, month, i, len(tickers))

    return pd.DataFrame(rows, columns=["ticker", "signal"])
