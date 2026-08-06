"""Apply an already-generated `ScreeningRule` to every ticker on one
rebalance date, producing LLM-S's buy/sell/hold signal set for that date.
"""

from __future__ import annotations

import logging
from datetime import date

import duckdb
import pandas as pd

from src.agents.llm_s_apply import apply_rule
from src.agents.llm_s_schema import ScreeningRule
from src.config.settings import settings

logger = logging.getLogger(__name__)


def screen(rule: ScreeningRule, rebalance_date: date, db_path: str = settings.db_path) -> pd.DataFrame:
    """`rule` applied to every ticker in the `factors` table on
    `rebalance_date`. Rows with a null `mve_z`, `bm_z`, or `mom12m_z`
    cannot be evaluated against a numeric rule and are excluded entirely
    (not imputed, not defaulted to `"hold"`) - the exclusion count is
    logged, since a large one is a data-quality signal worth noticing.
    Returns a DataFrame with columns `ticker`, `signal`
    (`"buy"`/`"sell"`/`"hold"`).
    """
    con = duckdb.connect(db_path, read_only=True)
    try:
        df = con.execute(
            "SELECT ticker, mve_z AS mve, bm_z AS bm, mom12m_z AS mom12m "
            "FROM factors WHERE rebalance_date = ?",
            [rebalance_date],
        ).fetchdf()
    finally:
        con.close()

    total = len(df)
    df = df.dropna(subset=["mve", "bm", "mom12m"]).reset_index(drop=True)
    excluded = total - len(df)
    if excluded:
        logger.info(
            "screen(rebalance_date=%s): excluded %d/%d tickers with a null mve/bm/mom12m",
            rebalance_date,
            excluded,
            total,
        )

    df["signal"] = [
        apply_rule(rule, {"mve": row.mve, "bm": row.bm, "mom12m": row.mom12m}) for row in df.itertuples()
    ]
    return df[["ticker", "signal"]]
