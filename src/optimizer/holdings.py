"""The user's OWN portfolio, measured: turn the share counts saved in
`memory/portfolio.json` (see `src/flow/user_portfolio.py`) into value
weights, and report that portfolio's annualized return, annualized
volatility and Sharpe ratio, per `plans/13_user_portfolio.md`.

The distinction from `src/optimizer/portfolio.py` is which direction the
question runs. That module answers "given these candidates, what weights
SHOULD I hold?" and derives weights from an objective. This one answers
"given the shares I DO hold, what have they returned and at what risk?" and
derives weights from what is owned - `shares * price` as a fraction of the
whole. The measurement itself is deliberately not reimplemented here: it
comes from that module's `stats_for_weights`, which shares its estimators
with the optimizer and with `src/optimizer/benchmark.py`, so a held
portfolio's figures can be printed directly beneath an optimized pool's and
the market's and honestly compared with them. That covers the per-HOLDING
annualized return and volatility as well as the portfolio-level three:
knowing that a portfolio returned 12% at 15% volatility raises the
immediate question of which holding contributed what, and the report for an
optimized pool has always answered it.

Two reporting rules shape this module, both chosen deliberately (see
`plans/13_user_portfolio.md`'s Decision Log):

Holdings with too little usable price history are EXCLUDED from the figures
and named, rather than blocking the report. Refusing to say anything about
98% of a portfolio because one recently-listed fund lacks two years of
monthly returns would withhold a correct answer over a marginal holding.
Every exclusion is reported with the month count it actually had and its
share of total value, so the reader can see exactly how much of their
portfolio the figures do not cover.

`market_values`/`total_value` and `weights` therefore use DIFFERENT
denominators, on purpose. "What is my portfolio worth?" must count a
holding whose history is too short - the user owns it either way. "What
produced these three figures?" must not. Each is reported against its own
honest denominator instead of one being quietly bent to match the other.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import NamedTuple

import pandas as pd

from src.config.settings import settings
from src.optimizer.portfolio import (
    apply_min_history_rule,
    load_latest_prices,
    load_returns_long,
    load_returns_matrix_unfiltered,
    stats_for_weights,
)

logger = logging.getLogger(__name__)

HOLDINGS_MIN_MONTHS = 24
"""Months of monthly returns a holding needs before it counts toward the
reported figures.

The same bar `load_returns_matrix`'s `min_months` default and
`src/optimizer/benchmark.py`'s `BENCHMARK_MIN_MONTHS` already apply, named
here as its own constant so the threshold is findable from the module that
reports against it - and so a reader who wonders why a holding was left out
does not have to trace it through a default argument.
"""

NO_HOLDINGS_REASON = (
    "no holdings are saved for {currency}; add some with: "
    "uv run portfolio-holdings set TICKER SHARES"
)


class HoldingsStats(NamedTuple):
    """One portfolio-as-held report: what is owned, what it is worth, and
    the three figures behind it.

    Follows `src/optimizer/benchmark.py`'s `BenchmarkStats` discipline:
    either all six of `annual_return`, `annual_volatility`, `sharpe`,
    `window_start`, `window_end` and `window_months` are populated, or all
    six are `None` and `unavailable_reason` is a sentence a person can act
    on. Never a half state, so a caller cannot print half a report by
    forgetting to check one field.

    `excluded` is independent of that: it maps a ticker to why it is not
    behind the figures, and a report can perfectly well have both good
    figures and exclusions. `positions`, `market_values` and `total_value`
    likewise survive an unavailable report whenever they are knowable - a
    portfolio whose value can be priced but whose history is too short to
    measure should still tell the user what it is worth.

    `expected_returns` and `volatility` are the annualized per-HOLDING
    estimates the three portfolio-level figures were computed from, named to
    match `src/optimizer/portfolio.py`'s `PortfolioStats` fields so the
    report can render a held portfolio's "Expected return / volatility
    (annualized)" section exactly as it renders an optimized pool's. They
    cover only the measured holdings, since an excluded one has no estimate
    to report - which is the one place these two records legitimately
    differ, `PortfolioStats` carrying every ticker the optimizer considered
    including the ones it gave no weight.
    """

    currency: str
    positions: dict[str, float]
    weights: dict[str, float]
    market_values: dict[str, float]
    expected_returns: dict[str, float]
    volatility: dict[str, float]
    total_value: float | None
    annual_return: float | None
    annual_volatility: float | None
    sharpe: float | None
    risk_free_rate: float
    window_start: date | None
    window_end: date | None
    window_months: int | None
    excluded: dict[str, str]
    unavailable_reason: str | None


def unavailable_holdings(
    currency: str,
    positions: dict[str, float],
    risk_free_rate: float,
    reason: str,
    market_values: dict[str, float] | None = None,
    total_value: float | None = None,
    excluded: dict[str, str] | None = None,
) -> HoldingsStats:
    """A `HoldingsStats` carrying only `unavailable_reason` and whatever is
    still knowable, so every "there are no figures" path produces the same
    all-or-nothing shape rather than each assembling its own.

    Public because `src/flow/interactive.py`'s `prepare_holdings` needs it
    for the cases it discovers before this module is ever reached - a
    disabled fetch, or an exception it must turn into a report line rather
    than let escape.
    """
    return HoldingsStats(
        currency=currency,
        positions=positions,
        weights={},
        market_values=market_values or {},
        expected_returns={},
        volatility={},
        total_value=total_value,
        annual_return=None,
        annual_volatility=None,
        sharpe=None,
        risk_free_rate=float(risk_free_rate),
        window_start=None,
        window_end=None,
        window_months=None,
        excluded=excluded or {},
        unavailable_reason=reason,
    )


def stored_month_counts(tickers: list[str], as_of: date, db_path: str) -> dict[str, int]:
    """How many months of monthly returns `db_path` already holds for each
    of `tickers` at or before `as_of`, as `{ticker: count}` with a zero for
    every ticker it holds none for.

    Opened read-only, and a missing file or missing `returns` table counts
    as zero rather than raising - the normal state of a freshly built
    scratch database. This is what lets `prepare_holdings` decide whether a
    Yahoo Finance fetch is needed at all without creating a database, taking
    a write lock, or paying for a fetch whose answer is already cached.

    Counts the whole stored history up to `as_of` rather than only the
    reporting window, which is the right question here: `_load_window_dates`
    derives that window from the dates the table actually holds, so history
    the table has is history the window will reach.
    """
    long_df = load_returns_long(tickers, None, as_of, db_path, read_only=True)
    if long_df.empty:
        return {ticker: 0 for ticker in tickers}
    counts = long_df.dropna(subset=["monthly_return"]).groupby("ticker").size()
    return {ticker: int(counts.get(ticker, 0)) for ticker in tickers}


def weights_from_positions(
    positions: dict[str, float], latest_prices: pd.Series
) -> tuple[dict[str, float], dict[str, float], float]:
    """`(weights, market_values, total_value)` for `positions` priced at
    `latest_prices`.

    `market_values[ticker]` is `shares * price` and `weights[ticker]` is
    that value's share of `total_value`, which is what the return and
    volatility arithmetic consumes - a portfolio's risk depends on how its
    money is distributed, not on how many pieces of paper it is divided
    into, so 1 share of a $600 fund and 600 shares of a $1 stock carry the
    same weight.

    A holding whose price is missing (NaN), non-positive, or absent from
    `latest_prices` contributes to none of the three: it cannot be valued,
    so any weight given to it would be invented. Callers report such a
    holding by name - see `holdings_stats`.
    """
    market_values: dict[str, float] = {}
    for ticker, shares in positions.items():
        price = latest_prices.get(ticker) if latest_prices is not None else None
        if price is None or pd.isna(price) or float(price) <= 0:
            continue
        market_values[ticker] = float(shares) * float(price)

    total_value = float(sum(market_values.values()))
    if total_value <= 0:
        return {}, market_values, total_value

    weights = {ticker: value / total_value for ticker, value in market_values.items()}
    return weights, market_values, total_value


def _exclusion_reasons(
    positions: dict[str, float],
    raw: pd.DataFrame,
    kept: pd.DataFrame,
    min_months: int,
) -> dict[str, str]:
    """Why each held ticker that `apply_min_history_rule` dropped was
    dropped, in words, one entry per excluded ticker.

    Distinguishes the rule's two grounds, because they call for different
    responses from the user: too few months means "wait, or accept that
    this holding is not measured", while an internal gap in an otherwise
    long history means the cached price data is incomplete and worth
    rebuilding. The month count comes from `raw` - the pre-rule matrix -
    since the rule itself leaves no record of it.
    """
    counts = raw.notna().sum() if not raw.empty else pd.Series(dtype=int)
    reasons: dict[str, str] = {}
    for ticker in positions:
        if ticker in kept.columns:
            continue
        months = int(counts.get(ticker, 0))
        if months < min_months:
            reasons[ticker] = (
                f"{months} month(s) of monthly returns in the window, under {min_months}"
            )
        else:
            reasons[ticker] = (
                f"a gap in its {months} month(s) of monthly returns; "
                "the cached price history is incomplete"
            )
    return reasons


def holdings_stats(
    positions: dict[str, float],
    as_of: date,
    db_path: str,
    currency: str,
    risk_free_rate: float = settings.risk_free_rate,
    lookback_months: int = 60,
    min_months: int = HOLDINGS_MIN_MONTHS,
) -> HoldingsStats:
    """Measure `positions` as of `as_of` against the prices and monthly
    returns already stored in `db_path`.

    Reads `db_path` and nothing else - resolving where that data comes from,
    and fetching it when it is not there, is
    `src/flow/interactive.py`'s `prepare_holdings`, kept separate for the
    same reason `prepare_benchmark` is separate from
    `benchmark_stats_for_window`: this function must be callable against a
    fixture database in a test with no network seam to stub.

    The window and both figures are read off the data actually used, never
    from `lookback_months`: a portfolio whose holdings have forty months of
    history reports forty months, because the same portfolio measured over
    a different window is a different number and a figure whose derivation
    is not stated beside it invites being compared with one derived
    differently.

    An empty `positions` is a legitimate state, not an error - the user may
    genuinely hold nothing in this currency yet - and comes back as an
    unavailable report naming the command that fixes it, without touching
    `db_path` at all.
    """
    tickers = sorted(positions)
    if not tickers:
        return unavailable_holdings(
            currency, {}, risk_free_rate, NO_HOLDINGS_REASON.format(currency=currency)
        )

    raw = load_returns_matrix_unfiltered(tickers, as_of, lookback_months, db_path)
    kept = apply_min_history_rule(raw, min_months)
    excluded = _exclusion_reasons(positions, raw, kept, min_months)

    latest_prices = load_latest_prices(tickers, as_of, db_path)
    weights, market_values, total_value = weights_from_positions(positions, latest_prices)
    for ticker in tickers:
        if ticker not in market_values:
            excluded[ticker] = f"no price on or before {as_of}"

    # A holding must clear BOTH bars to be measured: enough history to
    # estimate from, and a price to turn its shares into a weight.
    measured = [t for t in kept.columns if t in market_values]
    if not measured:
        return unavailable_holdings(
            currency,
            positions,
            risk_free_rate,
            "no holding has both a usable price and at least "
            f"{min_months} month(s) of monthly returns",
            market_values=market_values,
            total_value=total_value if market_values else None,
            excluded=excluded,
        )

    matrix = kept[measured]
    measured_value = float(sum(market_values[t] for t in measured))
    measured_weights = {t: market_values[t] / measured_value for t in measured}

    measured_stats = stats_for_weights(matrix, measured_weights, risk_free_rate)

    return HoldingsStats(
        currency=currency,
        positions=positions,
        weights=measured_weights,
        market_values=market_values,
        expected_returns={t: measured_stats.expected_returns[t] for t in measured},
        volatility={t: measured_stats.volatility[t] for t in measured},
        total_value=total_value,
        annual_return=measured_stats.portfolio_expected_return,
        annual_volatility=measured_stats.portfolio_volatility,
        sharpe=measured_stats.portfolio_sharpe,
        risk_free_rate=float(risk_free_rate),
        window_start=matrix.index.min().date(),
        window_end=matrix.index.max().date(),
        window_months=len(matrix.index),
        excluded=excluded,
        unavailable_reason=None,
    )
