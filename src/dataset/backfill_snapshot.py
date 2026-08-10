"""One-off, additive backfill of a single out-of-band `factors` snapshot.

`plans/08_consistency_review.md` finding 4: `README.md`'s Backtest Mode
Stage 1 states LLM-S's year=2020 rule is generated from a 2019-12-31
snapshot, but `src/dataset/membership.py`'s `compute_rebalance_dates` only
ever produces rows from `settings.rebalance_start` (2020-01-01) onward, so
`src/agents/llm_s.py`'s `resolve_as_of_date(2020)` fell back to 2020-01-01
and logged a "not truly causally masked" warning. This module computes and
inserts exactly one real 2019-12-31 row per S&P 500 member into the
existing `factors` table, reusing every pure/orchestration function
`membership.py`, `fundamentals.py`, `momentum.py`, and `sec_edgar.py`
already provide for the project's regular 52-date window — INSERTing into
the existing table rather than the DROP-and-fully-recreate pattern every
`write_*_table` function in this package otherwise uses, since a full
rebuild is not needed and would needlessly re-fetch every other date's
network data.

The membership fetch (Wikipedia) and the book-equity/shares/splits fetches
(SEC EDGAR, yfinance) are live network calls, same as the regular build.
The price data mve and mom12m need is read from the existing `prices`
table, not re-fetched — `settings.fetch_start` (2015-01-01) already covers
2019-12-31 and the 12 months before it.
"""

from __future__ import annotations

import logging
from datetime import date

import duckdb
import pandas as pd

from src.config.settings import settings
from src.dataset import sec_edgar
from src.dataset.fundamentals import (
    add_cross_sectional_z,
    attach_nearest_price,
    compute_bm_column,
    compute_market_cap_column,
    compute_mve_column,
    fetch_all_ticker_fundamentals,
    load_prices_for_join,
    load_unresolved_tickers,
)
from src.dataset.membership import apply_changes_asof, fetch_and_normalize_membership
from src.dataset.momentum import compute_mom12m_column

logger = logging.getLogger(__name__)


def build_snapshot_for_date(as_of: date, db_path: str = settings.db_path) -> pd.DataFrame:
    """Compute one date's full factors cross-section (`mve`, `bm`,
    `mom12m`, and their cross-sectional z-scores) for whichever tickers
    were S&P 500 members on `as_of`, mirroring exactly what
    `fundamentals.build_factors` and `momentum.build_momentum_factors`
    compute for the project's regular 52-date window, for this one
    additional date. Returns columns `['rebalance_date', 'ticker', 'mve',
    'bm', 'mom12m', 'mve_z', 'bm_z', 'mom12m_z']`, not yet written to disk.
    """
    current, changes = fetch_and_normalize_membership()
    members = apply_changes_asof(current, changes, as_of)
    membership = pd.DataFrame(
        {
            "rebalance_date": pd.to_datetime([as_of] * len(members)).astype("datetime64[us]"),
            "ticker": members["ticker"].tolist(),
        }
    )
    logger.info("as_of=%s membership_count=%d", as_of, len(membership))

    prices = load_prices_for_join(db_path)
    unresolved = load_unresolved_tickers(db_path)
    fetchable = sorted(set(membership["ticker"]) - unresolved)

    cik_map = sec_edgar.fetch_cik_map()
    sec_book_equity_by_ticker, sec_fallback_reasons = sec_edgar.fetch_all_sec_book_equity(
        fetchable, cik_map, as_of
    )
    shares_by_ticker, splits_by_ticker, quarterly_bs_by_ticker, annual_bs_by_ticker = (
        fetch_all_ticker_fundamentals(fetchable)
    )

    merged = attach_nearest_price(membership, prices)
    market_cap = compute_market_cap_column(merged, shares_by_ticker, splits_by_ticker)
    merged["mve"] = compute_mve_column(market_cap)
    merged["bm"] = compute_bm_column(
        merged, market_cap, quarterly_bs_by_ticker, annual_bs_by_ticker, sec_book_equity_by_ticker
    )
    merged["mom12m"] = compute_mom12m_column(membership, prices)

    merged = add_cross_sectional_z(merged, "mve", "mve_z")
    merged = add_cross_sectional_z(merged, "bm", "bm_z")
    merged = add_cross_sectional_z(merged, "mom12m", "mom12m_z")

    factors = merged[["rebalance_date", "ticker", "mve", "bm", "mom12m", "mve_z", "bm_z", "mom12m_z"]]
    logger.info(
        "computed %d snapshot row(s) for as_of=%s (mve populated for %d; bm populated for %d, "
        "%d via SEC, %d fell back to yfinance; mom12m populated for %d)",
        len(factors),
        as_of,
        factors["mve"].notna().sum(),
        factors["bm"].notna().sum(),
        len(sec_book_equity_by_ticker),
        len(sec_fallback_reasons),
        factors["mom12m"].notna().sum(),
    )
    return factors


def insert_snapshot_rows(df: pd.DataFrame, db_path: str = settings.db_path) -> None:
    """INSERT `df`'s rows into the existing `factors` table, first
    DELETEing any pre-existing row for the same `rebalance_date`(s) — safe
    to rerun, and (unlike `fundamentals.write_factors_table` /
    `momentum.write_factors_table`, which both DROP and fully recreate the
    whole table) never touches any other date's rows.
    """
    con = duckdb.connect(db_path)
    try:
        con.register("snapshot_df", df)
        for d in df["rebalance_date"].dropna().unique().tolist():
            con.execute("DELETE FROM factors WHERE rebalance_date = ?", [pd.Timestamp(d).date()])
        con.execute(
            "INSERT INTO factors "
            "SELECT rebalance_date::DATE AS rebalance_date, "
            "ticker::VARCHAR AS ticker, "
            "mve::DOUBLE AS mve, "
            "bm::DOUBLE AS bm, "
            "mom12m::DOUBLE AS mom12m, "
            "mve_z::DOUBLE AS mve_z, "
            "bm_z::DOUBLE AS bm_z, "
            "mom12m_z::DOUBLE AS mom12m_z "
            "FROM snapshot_df"
        )
        con.unregister("snapshot_df")
    finally:
        con.close()
    logger.info("inserted %d row(s) into %s::factors", len(df), db_path)


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO)
    target = date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else date(2019, 12, 31)
    snapshot = build_snapshot_for_date(target)
    insert_snapshot_rows(snapshot)
