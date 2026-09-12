"""One candidate ticker's annual return, annual volatility, Sharpe ratio and
trailing dividend yield, measured the way this project measures everything
else.

This is the second half of the summary `src/agentic_portfolio/flow/cli.py`'s
`print_ticker_summary` prints when somebody adds a ticker to a
`user_provided` candidate pool. The first half, in
`src/agentic_portfolio/dataset/ticker_profile.py`, is what Yahoo Finance publishes about the
security. This half answers a different question - not "what is this thing"
but "what will it contribute to the portfolio I am about to build" - and
that is why it cannot be taken from Yahoo's published figures even though
Yahoo publishes something with the same names.

The reason is comparability, the same argument `src/agentic_portfolio/optimizer/benchmark.py`
makes at length for a benchmark. Yahoo computes its trailing returns over
Yahoo's own windows with Yahoo's own conventions. The weights this command
is about to print come from a 60-month window of this project's `returns`
table, with expected returns from `mean_historical_return` and volatilities
from `CovarianceShrinkage(...).ledoit_wolf()`. A figure produced one way
printed beside weights produced the other way invites being compared with
one derived differently - so every figure here is produced by the very
estimators the optimizer uses, via `annualized_return_and_volatility`,
which is imported rather than reimplemented.

Nothing here fetches anything. This module reads a database read-only and
imports nothing from `src/agentic_portfolio/flow` and nothing from `yfinance`, following
`src/agentic_portfolio/optimizer/holdings.py`, which is what makes it testable against a
fixture database with no mocking at all. Deciding whether a ticker needs
fetching first, and into which database, is `src/agentic_portfolio/flow/interactive.py`'s
`prepare_ticker_summary`.

One asymmetry with the portfolio's own per-ticker figures is worth knowing
before reading a close call. The volatility reported here is the ticker's
standalone volatility, because Ledoit-Wolf shrinkage on a single column is
inert. Inside a pool's covariance matrix the same ticker's volatility is
genuinely shrunk - measured on a four-asset pool in
`src/agentic_portfolio/optimizer/benchmark.py`'s docstring, one holding moved from 0.1614
standalone to 0.1852 inside the matrix, +14.8%. So the number here is what
the ticker did on its own, which is the right question at the add prompt,
and it will not always equal the per-ticker line the report prints after
optimizing. That is the same deliberate asymmetry the benchmark has, and
for the same reason: a figure that moves when you add an unrelated
candidate is not a description of the candidate.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import NamedTuple

import numpy as np
import pandas as pd

from agentic_portfolio.config.settings import settings
from agentic_portfolio.dataset.dividends import load_dividend_figures
from agentic_portfolio.optimizer.benchmark import (
    MIN_USABLE_ANNUAL_VOLATILITY,
    annualized_return_and_volatility,
    load_ticker_monthly_returns,
)

logger = logging.getLogger(__name__)

DEFAULT_LOOKBACK_MONTHS = 60
"""Months of monthly returns the figures are measured over.

The same trailing window `src/agentic_portfolio/optimizer/portfolio.py`'s
`load_returns_matrix` defaults to, so the return and volatility printed for
a ticker at the add prompt describe the same months the optimizer is about
to weight it over.
"""

TICKER_MIN_MONTHS = 24
"""Fewest monthly returns a ticker must have in the window before figures
are reported rather than declined.

Deliberately the same bar `src/agentic_portfolio/optimizer/portfolio.py`'s
`apply_min_history_rule` sets for a pool ticker and
`src/agentic_portfolio/optimizer/benchmark.py`'s `BENCHMARK_MIN_MONTHS` sets for a benchmark.
Holding all three to one standard is the point: a candidate whose summary
promised an annual return and was then silently dropped from the optimizer
for insufficient history would be worse than one that said up front how
many months it has.

Note what this threshold does NOT do. A ticker passing it can still be
dropped later by `apply_min_history_rule` for an internal gap in its
monthly returns, which this module does not check because a gap changes
whether a covariance term can be computed rather than whether a single
series can be described.
"""


class TickerStats(NamedTuple):
    """One ticker's figures over one window, or the reason there are none.

    Follows the contract `BenchmarkStats` and `HoldingsStats` set: either
    every numeric field is populated and `unavailable_reason` is `None`, or
    every numeric field is `None` and `unavailable_reason` says why in one
    sentence fit to print. There is no half-populated state to guard against
    at the print site.

    The dividend fields are a SEPARATE group with their own reason, because
    they fail independently: a ticker can have five years of clean monthly
    returns and no usable dividend history at all - `AVB`, `EA`, `EQR` and
    `LEG` are exactly that case in this project's own `dividend_unresolved`
    table. A `dividend_yield` of `None` with a reason beside it is never the
    same thing as a yield of `0.0`, which means a confirmed non-payer.

    `window_start`, `window_end` and `window_months` are read off the data
    that was actually found, never off the requested `lookback_months`: real
    history is frequently shorter than the target, and this project's rule
    is that a figure prints the window that produced it.
    """

    ticker: str
    currency: str | None
    annual_return: float | None
    annual_volatility: float | None
    sharpe: float | None
    risk_free_rate: float
    window_start: date | None
    window_end: date | None
    window_months: int
    dividend_yield: float | None
    dividend_lookback_months: int | None
    dividend_unavailable_reason: str | None
    unavailable_reason: str | None


def _unavailable(
    ticker: str, currency: str | None, risk_free_rate: float, reason: str
) -> TickerStats:
    """A `TickerStats` carrying only a reason, for every case where there are
    no honest figures. `risk_free_rate` is still echoed because it describes
    the run rather than the ticker.
    """
    return TickerStats(
        ticker=ticker,
        currency=currency,
        annual_return=None,
        annual_volatility=None,
        sharpe=None,
        risk_free_rate=float(risk_free_rate),
        window_start=None,
        window_end=None,
        window_months=0,
        dividend_yield=None,
        dividend_lookback_months=None,
        dividend_unavailable_reason=None,
        unavailable_reason=reason,
    )


def _dividend_figures(
    ticker: str, as_of: date, db_path: str, lookback_months: int
) -> tuple[float | None, int | None, str | None]:
    """`(yield, lookback_months, reason)` for one ticker's trailing dividend
    yield, from the dividend rows an add already ingested.

    Reuses `src/agentic_portfolio/dataset/dividends.py`'s `load_dividend_figures`, which is
    the same loader the optimizer's minimum-dividend constraint uses - so
    the yield shown at the add prompt is the yield that constraint will
    later apply, rather than a second opinion computed differently. Its
    contract is carried through unchanged: a key present in `yields` is
    reliable (and `0.0` means a confirmed non-payer), while a ticker absent
    from it is named in `unavailable` with a sentence saying why.

    Any failure degrades to a reason rather than raising. A summary is worth
    printing without a yield; it is not worth losing an in-progress
    candidate pool over.
    """
    try:
        yields, _per_share, unavailable, _splits = load_dividend_figures(
            [ticker], as_of, db_path, lookback_months
        )
    except Exception as e:  # noqa: BLE001 - a report must not fail over a missing table
        logger.info("dividend figures unavailable for %s: %s", ticker, e)
        return None, None, f"could not read dividend history for {ticker} ({type(e).__name__})"

    if ticker in yields:
        return float(yields[ticker]), int(lookback_months), None
    return None, None, unavailable.get(ticker, f"no trailing dividend data for {ticker}")


def ticker_stats(
    ticker: str,
    as_of: date,
    db_path: str,
    currency: str | None = None,
    risk_free_rate: float = settings.risk_free_rate,
    lookback_months: int = DEFAULT_LOOKBACK_MONTHS,
    min_months: int = TICKER_MIN_MONTHS,
    dividend_lookback_months: int = settings.dividend_lookback_months,
) -> TickerStats:
    """`ticker`'s annual return, annual volatility, Sharpe ratio and trailing
    dividend yield over the trailing `lookback_months` months ending at or
    before `as_of`, read from `db_path`.

    The return and volatility come from `annualized_return_and_volatility`,
    imported from `src/agentic_portfolio/optimizer/benchmark.py` rather than reimplemented.
    That matters more than it looks: its volatility comes from
    `CovarianceShrinkage(...).ledoit_wolf()`, whose `ddof=0` sample
    covariance differs from the obvious `series.std() * sqrt(12)` by about
    0.8% on real data - enough to make a candidate look better or worse than
    the portfolio it is joining for no reason but a convention mismatch. The
    Sharpe ratio is `(annual_return - risk_free_rate) / annual_volatility`,
    the identical definition `benchmark_stats_for_window` uses.

    Returns a `TickerStats` carrying only `unavailable_reason` when the
    ticker has no rows at all, has fewer than `min_months` of them in the
    window, or did not move across them - see `MIN_USABLE_ANNUAL_VOLATILITY`
    in `src/agentic_portfolio/optimizer/benchmark.py`: a flat series is a data problem rather
    than a riskless asset, and dividing by the floating-point residue it has
    for a volatility yields a Sharpe ratio around 1.8e16, a number that is
    not merely useless but would print as the best investment ever measured.

    Reads read-only throughout, so pointing this at a shared cache cannot
    create, lock or modify it.
    """
    history = load_ticker_monthly_returns(ticker, as_of, db_path)
    if history.empty:
        return _unavailable(
            ticker,
            currency,
            risk_free_rate,
            f"no monthly returns stored for {ticker} on or before {as_of}",
        )

    # Trailing `lookback_months` months of whatever was found, taken from the
    # end rather than by date arithmetic so a ticker whose history has fewer
    # months than the window keeps all of them.
    window = history.iloc[-int(lookback_months) :] if lookback_months > 0 else history.iloc[:0]
    if len(window) < min_months:
        return _unavailable(
            ticker,
            currency,
            risk_free_rate,
            f"{len(window)} month(s) of history for {ticker} in this window, "
            f"below the {min_months} required",
        )

    annual_return, annual_volatility = annualized_return_and_volatility(window)
    if not annual_volatility > MIN_USABLE_ANNUAL_VOLATILITY or not np.isfinite(annual_return):
        return _unavailable(
            ticker,
            currency,
            risk_free_rate,
            f"{ticker}'s monthly returns in this window give no usable "
            f"return/volatility estimate (annual volatility {annual_volatility!r})",
        )

    dividend_yield, dividend_months, dividend_reason = _dividend_figures(
        ticker, as_of, db_path, dividend_lookback_months
    )

    return TickerStats(
        ticker=ticker,
        currency=currency,
        annual_return=annual_return,
        annual_volatility=annual_volatility,
        sharpe=(annual_return - float(risk_free_rate)) / annual_volatility,
        risk_free_rate=float(risk_free_rate),
        window_start=pd.Timestamp(window.index.min()).date(),
        window_end=pd.Timestamp(window.index.max()).date(),
        window_months=len(window),
        dividend_yield=dividend_yield,
        dividend_lookback_months=dividend_months,
        dividend_unavailable_reason=dividend_reason,
        unavailable_reason=None,
    )
