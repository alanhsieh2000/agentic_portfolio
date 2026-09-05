"""Validate and ingest arbitrary user-supplied tickers - any listed symbol,
including ETFs and non-S&P-500 stocks - into a session database, so the
`user_provided` selection mode's optimizer step can price and weight them.

One function does both jobs deliberately: "does this ticker exist" can only
be answered by asking Yahoo Finance for its prices, and those same prices
are exactly what `load_returns_matrix`/`load_latest_prices` need afterward,
so splitting validation from ingestion would mean fetching twice for no
benefit. A ticker yfinance returns no usable rows for is reported back as
invalid (never raised), letting a caller add the good tickers from the same
batch and name the bad ones.

Note that "valid" here means fetchable, not necessarily usable by the
optimizer: a genuinely-listed but very recently IPO'd ticker passes
validation and is still dropped later by
`src/optimizer/portfolio.py`'s `apply_min_history_rule` for having fewer
than its `min_months` of history. That is the optimizer's own pre-existing
rule, not a validation failure.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

import pandas as pd

from src.dataset.prices import fetch_and_reshape_for_tickers, upsert_prices_tables
from src.dataset.returns import build_returns_for_tickers

logger = logging.getLogger(__name__)

LOOKBACK_MONTHS = 65
"""Months of price history fetched before `as_of`, matching
`src/flow/live.py`'s own `LOOKBACK_MONTHS` (60-month returns lookback plus a
buffer month so the earliest monthly return has a preceding price to compute
from). Duplicated rather than imported: this dataset-layer module must not
depend on `src/flow`.
"""


def validate_and_ingest_tickers(
    tickers: list[str],
    as_of: date,
    db_path: str,
) -> tuple[list[str], dict[str, str]]:
    """Fetch `LOOKBACK_MONTHS` months of prices through `as_of` for
    `tickers`, merge the results (and the resulting monthly returns) into
    `db_path`, and report which tickers resolved.

    Returns `(valid, invalid)`: `valid` is the tickers yfinance produced
    usable price rows for (normalized to upper case, deduplicated, sorted),
    `invalid` maps each remaining ticker to the reason it was rejected.
    Never raises for a bad ticker - an empty `tickers` list short-circuits
    without any network call at all.
    """
    cleaned = sorted({t.strip().upper() for t in tickers if t.strip()})
    if not cleaned:
        return [], {}

    start = (pd.Timestamp(as_of) - pd.DateOffset(months=LOOKBACK_MONTHS)).date().isoformat()
    end = (as_of + timedelta(days=5)).isoformat()

    try:
        long_prices, unresolved = fetch_and_reshape_for_tickers(cleaned, start, end)
    except ValueError as e:
        # _build_symbol_map rejects a batch whose tickers collide on the same
        # yfinance symbol (e.g. 'BRK.B' alongside 'BRK-B'); the whole batch is
        # ambiguous, so report it rather than crashing the caller's loop.
        logger.warning("ticker batch %s rejected: %s", cleaned, e)
        return [], {ticker: str(e) for ticker in cleaned}

    upsert_prices_tables(long_prices, unresolved, cleaned, db_path)
    build_returns_for_tickers(cleaned, db_path, start=start, end=as_of.isoformat())

    invalid = dict(zip(unresolved["ticker"], unresolved["reason"])) if not unresolved.empty else {}
    valid = [ticker for ticker in cleaned if ticker not in invalid]
    return valid, invalid
