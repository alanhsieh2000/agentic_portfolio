"""Twelve-month momentum (mom12m) per ticker per rebalance date.

Computed directly from the `prices` table (src/dataset/prices.py) — unlike
`mve`/`bm`, this needs no new network fetch (no yfinance, no SEC EDGAR).
For rebalance date d and ticker t: find the adj_close nearest to (and not
after) d minus 1 month, and the adj_close nearest to (and not after) d
minus 12 months; mom12m is the compounded return between those two points.
The most recent month is deliberately skipped (the window is [d-12mo,
d-1mo], not [d-12mo, d]) — the standard academic "Fama-French style"
momentum definition, avoiding conflating momentum with short-term reversal
(see plans/01_dataset.md's Context and Orientation section for the full
definition this project uses).

This module's entry point reads the `factors` table that
src/dataset/fundamentals.py already wrote (with `mve`/`bm` populated and
`mom12m`/`mom12m_z` as NULL placeholders) and fills in `mom12m`/`mom12m_z`
on top of it, joined explicitly on (rebalance_date, ticker) rather than
assumed row order — deliberately not recomputing mve/bm from scratch,
since that would mean a wasted multi-minute yfinance/SEC EDGAR re-fetch
just to add a column that doesn't depend on either.
"""

from __future__ import annotations

import logging

import duckdb
import pandas as pd

from src.config.settings import settings
from src.dataset.fundamentals import (
    add_cross_sectional_z,
    attach_nearest_price,
    load_membership,
    most_recent_value_on_or_before,
    write_factors_table,
)

logger = logging.getLogger(__name__)


class FactorsTableMissingError(RuntimeError):
    """Raised when the `factors` table doesn't exist yet. This module
    extends the table src/dataset/fundamentals.py's build_factors already
    wrote (preserving its mve/bm/mve_z/bm_z columns) rather than
    recomputing those from scratch; run fundamentals.py first.
    """


def compute_mom12m(price_1mo_before: float | None, price_12mo_before: float | None) -> float | None:
    """(price_1mo_before / price_12mo_before) - 1. Returns None (not NaN,
    not an exception) if either price is missing/NaN or price_12mo_before
    is non-positive — the expected, common case near the start of this
    project's price history, matching the null-preserving philosophy every
    other factor in this codebase follows.
    """
    if (
        price_1mo_before is None
        or pd.isna(price_1mo_before)
        or price_12mo_before is None
        or pd.isna(price_12mo_before)
        or price_12mo_before <= 0
    ):
        return None
    return (price_1mo_before / price_12mo_before) - 1.0


def load_prices_for_join(db_path: str = settings.db_path) -> pd.DataFrame:
    """columns ['date', 'ticker', 'adj_close'] — identical shape to
    fundamentals.py's load_prices_for_join (duplicated here rather than
    imported, since fundamentals.py's version is a private-ish helper
    tightly scoped to that module's own build_factors; both simply read
    the same three columns from the same `prices` table).
    """
    con = duckdb.connect(db_path)
    try:
        df = con.execute("SELECT date, ticker, adj_close FROM prices").fetchdf()
    finally:
        con.close()
    df["date"] = pd.to_datetime(df["date"])
    return df


def load_factors(db_path: str = settings.db_path) -> pd.DataFrame:
    """Read the existing `factors` table written by
    fundamentals.py's build_factors. Raises FactorsTableMissingError if it
    doesn't exist yet — a structural precondition for this module, not
    ordinary per-row data absence.
    """
    con = duckdb.connect(db_path)
    try:
        df = con.execute(
            "SELECT rebalance_date, ticker, mve, bm, mom12m, mve_z, bm_z, mom12m_z FROM factors"
        ).fetchdf()
    except duckdb.CatalogException as e:
        raise FactorsTableMissingError(
            f"No 'factors' table found in {db_path!r}. Run "
            "`uv run python -m src.dataset.fundamentals` first."
        ) from e
    finally:
        con.close()
    df["rebalance_date"] = pd.to_datetime(df["rebalance_date"])
    return df


def compute_mom12m_column(membership: pd.DataFrame, prices: pd.DataFrame) -> pd.Series:
    """Row-wise mom12m for every (rebalance_date, ticker) in `membership`.

    Reuses attach_nearest_price twice, against two independently-shifted
    copies of `membership` (rebalance_date - 1 month, rebalance_date - 12
    months) — safe to combine via ordinary index-aligned arithmetic because
    attach_nearest_price now preserves the original membership index (see
    its docstring in fundamentals.py) regardless of either call's internal
    sort order.
    """
    one_month_before = membership.assign(rebalance_date=membership["rebalance_date"] - pd.DateOffset(months=1))
    twelve_months_before = membership.assign(rebalance_date=membership["rebalance_date"] - pd.DateOffset(months=12))

    price_1mo = attach_nearest_price(one_month_before, prices)["adj_close"]
    price_12mo = attach_nearest_price(twelve_months_before, prices)["adj_close"]

    return pd.Series(
        [compute_mom12m(p1, p12) for p1, p12 in zip(price_1mo.reindex(membership.index), price_12mo.reindex(membership.index))],
        index=membership.index,
    )


def build_momentum_factors(db_path: str = settings.db_path) -> pd.DataFrame:
    """Compute mom12m/mom12m_z and merge them into the existing `factors`
    table (preserving mve/bm/mve_z/bm_z), then write the combined table
    back. Returns the resulting DataFrame.
    """
    membership = load_membership(db_path)
    prices = load_prices_for_join(db_path)
    factors = load_factors(db_path)

    membership["mom12m"] = compute_mom12m_column(membership, prices)
    membership = add_cross_sectional_z(membership, "mom12m", "mom12m_z")

    updated = factors.drop(columns=["mom12m", "mom12m_z"]).merge(
        membership[["rebalance_date", "ticker", "mom12m", "mom12m_z"]],
        on=["rebalance_date", "ticker"],
        how="left",
    )
    updated = updated[["rebalance_date", "ticker", "mve", "bm", "mom12m", "mve_z", "bm_z", "mom12m_z"]]

    write_factors_table(updated, db_path)
    logger.info(
        "wrote %d rows to %s::factors (mom12m populated for %d rows; mve/bm columns preserved from fundamentals.py)",
        len(updated),
        db_path,
        updated["mom12m"].notna().sum(),
    )
    return updated


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    build_momentum_factors()
