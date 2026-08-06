"""Apply an already-generated LLM-S `ScreeningRule` to a candidate stock or
ETF OUTSIDE the S&P 500 universe the rule was written against.

`plans/02_llm_s_agent.md`'s rule is written entirely in terms of
standardized (cross-sectional mean 0, variance 1) z-scores of `mve`, `bm`,
`mom12m`, computed over that rebalance date's ~500 S&P 500 members
(`src/dataset/fundamentals.py`'s `add_cross_sectional_z`). Applying that
same rule to a ticker that was never part of that cross-section means
standardizing the candidate's own raw factor values against the S&P 500
universe's mean/std for that date — not recomputing a fresh mean/std that
includes the candidate itself, which would change meaning on every new
candidate tested and cannot be done at all for a single ticker in
isolation (variance needs a population). `get_factor_reference_stats`
(`src/dataset/fundamentals.py`) recovers that S&P 500 mean/std on demand
from the `factors` table's persisted raw columns; see
`plans/07_external_candidate_screening.md` for why no separate stats table
is stored anywhere in this project.
"""

from __future__ import annotations

import logging
import math

import pandas as pd

from src.agents.llm_s_apply import apply_rule
from src.agents.llm_s_schema import ScreeningRule
from src.dataset.fundamentals import (
    DEFAULT_DB_PATH,
    compute_bm,
    compute_mve,
    fetch_balance_sheets,
    fetch_shares_and_splits,
    get_factor_reference_stats,
    most_recent_value_on_or_before,
    select_book_equity,
)
from src.dataset.momentum import compute_mom12m
from src.dataset.prices import fetch_price_history, reshape_prices_long, to_yfinance_symbol

logger = logging.getLogger(__name__)


def _load_price_series(ticker: str, as_of_ts: pd.Timestamp) -> pd.Series:
    """Adjusted-close price history for one arbitrary ticker (stock or
    ETF), covering 13 months before `as_of_ts` (a buffer for the
    12-month-momentum lookback) through a few days after (so a price dated
    exactly `as_of_ts`, if one exists, is included — yfinance's `end` is
    exclusive). Reuses the exact same fetch/reshape functions
    `src/dataset/prices.py`'s S&P-500-membership pipeline calls, just for
    one ticker instead of the whole universe.
    """
    start = (as_of_ts - pd.DateOffset(months=13)).strftime("%Y-%m-%d")
    end = (as_of_ts + pd.Timedelta(days=5)).strftime("%Y-%m-%d")
    yf_symbol = to_yfinance_symbol(ticker)
    raw = fetch_price_history([ticker], start=start, end=end)
    long_prices = reshape_prices_long(raw, {yf_symbol: ticker})
    if long_prices.empty:
        return pd.Series(dtype="float64")
    return long_prices.set_index("date")["adj_close"]


def compute_raw_factors_for_ticker(ticker: str, as_of_date) -> dict[str, float | None]:
    """Raw `mve`, `bm`, `mom12m` for one off-index individual stock,
    reusing the exact per-ticker functions
    `src/dataset/fundamentals.py`'s `build_factors` and
    `src/dataset/momentum.py`'s `build_momentum_factors` already call per
    row for S&P 500 members — no new fetch logic, just invoked for one
    arbitrary ticker instead of looping over the membership table.

    Any factor whose inputs are unavailable (e.g. no balance-sheet
    coverage for `bm`) comes back `None`, matching this codebase's
    null-preserving philosophy elsewhere — never raises for ordinary
    per-ticker data absence.
    """
    as_of_ts = pd.Timestamp(as_of_date)
    price_series = _load_price_series(ticker, as_of_ts)
    price_at_asof = most_recent_value_on_or_before(price_series, as_of_ts)
    price_1mo_before = most_recent_value_on_or_before(price_series, as_of_ts - pd.DateOffset(months=1))
    price_12mo_before = most_recent_value_on_or_before(price_series, as_of_ts - pd.DateOffset(months=12))

    try:
        shares_series, splits = fetch_shares_and_splits(ticker)
    except Exception:
        logger.warning("shares/splits fetch failed for %s; mve/bm will be null", ticker, exc_info=True)
        shares_series, splits = pd.Series(dtype="float64"), pd.Series(dtype="float64")
    shares_raw = most_recent_value_on_or_before(shares_series, as_of_ts)

    mve = compute_mve(price_at_asof, shares_raw, splits, as_of_ts)

    try:
        quarterly_bs, annual_bs = fetch_balance_sheets(ticker)
    except Exception:
        logger.warning("balance sheet fetch failed for %s; bm will be null", ticker, exc_info=True)
        quarterly_bs, annual_bs = pd.DataFrame(), pd.DataFrame()
    book_equity = select_book_equity(quarterly_bs, annual_bs, as_of_ts, ticker)
    bm = compute_bm(book_equity, price_at_asof, shares_raw, splits, as_of_ts)

    mom12m = compute_mom12m(price_1mo_before, price_12mo_before)

    return {"mve": mve, "bm": bm, "mom12m": mom12m}


def compute_raw_factors_for_etf(
    ticker: str, as_of_date, aum: float | None, price_to_book: float | None
) -> dict[str, float | None]:
    """Raw `mve`, `bm`, `mom12m` for one ETF, where `mve`/`bm` in the
    single-company sense this project otherwise uses do not apply (no
    shares outstanding or book equity for a fund the way there is for a
    company). Per the repository owner's explicit direction, `mve` uses
    `log(aum)` (log of the fund's total net assets, the fund-level
    analogue of log market value of equity) and `bm` uses `1 /
    price_to_book` (the fund's aggregate portfolio price-to-book ratio,
    inverted to match this project's book-to-market convention).

    `aum` and `price_to_book` are supplied by the caller rather than
    fetched automatically: live `yfinance` coverage of these two fields
    for ETFs is unverified (see plans/07_external_candidate_screening.md's
    Progress checklist), so this function does not silently guess or scrape
    a third-party site for them. `mom12m` is computed identically to a
    stock's — ordinary price momentum applies to any priced instrument.
    """
    mve = math.log(aum) if aum is not None and aum > 0 else None
    bm = (1.0 / price_to_book) if price_to_book else None

    as_of_ts = pd.Timestamp(as_of_date)
    price_series = _load_price_series(ticker, as_of_ts)
    price_1mo_before = most_recent_value_on_or_before(price_series, as_of_ts - pd.DateOffset(months=1))
    price_12mo_before = most_recent_value_on_or_before(price_series, as_of_ts - pd.DateOffset(months=12))
    mom12m = compute_mom12m(price_1mo_before, price_12mo_before)

    return {"mve": mve, "bm": bm, "mom12m": mom12m}


def standardize_raw_factors(
    raw: dict[str, float | None], stats: dict[str, tuple[float, float]]
) -> dict[str, float]:
    """Apply `(x - mean) / std` per factor, using the S&P 500 universe's
    reference `stats` (from `get_factor_reference_stats`). A factor is
    OMITTED from the result entirely — not set to `None` or 0 — whenever
    its raw value is missing, or its reference std is 0 or missing
    (mirroring `add_cross_sectional_z`'s existing zero-std guard):
    omitting the key, rather than inventing a placeholder number, means
    `condition_eval.evaluate_condition` naturally raises "missing from the
    supplied values" if a rule's condition needs that factor, which
    `screen_external_candidate` below turns into an explicit
    `"insufficient_data"` result instead of a wrong signal.
    """
    standardized: dict[str, float] = {}
    for factor, value in raw.items():
        if value is None or pd.isna(value):
            continue
        mean, std = stats[factor]
        if std is None or pd.isna(std) or std == 0:
            continue
        standardized[factor] = (value - mean) / std
    return standardized


def screen_external_candidate(
    rule: ScreeningRule,
    raw_factors: dict[str, float | None],
    rebalance_date,
    db_path: str = DEFAULT_DB_PATH,
) -> str:
    """`"buy"`/`"sell"`/`"hold"` for one candidate ticker OUTSIDE the S&P
    500 universe, standardizing `raw_factors` against that universe's
    reference mean/std for `rebalance_date` and then applying `rule` via
    the exact same, unmodified `apply_rule`
    (`src/agents/llm_s_apply.py`) the in-universe `screen()` function uses.

    Returns `"insufficient_data"` (never raises, never guesses a signal)
    if `rule`'s buy/sell conditions reference a factor this candidate is
    missing after standardization — e.g. an ETF with no `bm` proxy
    supplied, screened against a rule whose buy_condition needs `bm`.
    """
    stats = get_factor_reference_stats(rebalance_date, db_path)
    standardized = standardize_raw_factors(raw_factors, stats)
    try:
        return apply_rule(rule, standardized)
    except ValueError:
        logger.info(
            "insufficient standardized data to evaluate rule against %s for rebalance_date=%s",
            standardized,
            rebalance_date,
        )
        return "insufficient_data"
