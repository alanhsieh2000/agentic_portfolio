"""A candidate pool's benchmark: which ticker stands in for "the market",
and what its annual return, annual volatility and Sharpe ratio were over
exactly the window of monthly returns a portfolio's own figures came from.

The point of this module is comparability, not new arithmetic. A report that
says a portfolio expects 12.6% at 15.4% volatility answers "what" but not
"was this worth building" - that needs the same three numbers for simply
holding the market over the same months, computed the same way. So every
figure here is produced by the very estimators `src/optimizer/portfolio.py`
uses for the portfolio itself:

- the annual return is `expected_returns.mean_historical_return(frame,
  returns_data=True, frequency=12)`, whose `compounding=True` default makes
  it a compound (geometric) annual growth rate, not an arithmetic mean;
- the annual volatility is the square root of
  `risk_models.CovarianceShrinkage(frame, returns_data=True,
  frequency=12).ledoit_wolf()`'s single diagonal entry.

That second one matters more than it looks. `ledoit_wolf()` hands scikit-learn
a `ddof=0` sample covariance, and for a single column Ledoit-Wolf shrinkage is
a no-op (the constant-variance target *is* that column's variance), so the
only difference from the obvious `series.std() * sqrt(12)` is the denominator
- and on real data that difference is about 0.8%, enough to make a benchmark
look better or worse than the portfolio for no reason but a convention
mismatch. `tests/test_benchmark.py` pins both facts.

One asymmetry between the two sides is deliberate and must NOT be "fixed".
They share the return estimator, the annualization, the ddof convention, the
Sharpe definition and the risk-free rate - but they do not share Ledoit-Wolf
shrinkage, and cannot. Shrinkage is defined relative to a cross-section of
assets: it pulls each variance toward the average of them. A benchmark is one
column, so scikit-learn shrinks it by exactly zero (it reports `delta=0.0`),
while a pool's covariance genuinely is shrunk - measured here, a four-asset
pool drew `delta=0.176`, which moved one holding's annual volatility from
0.1614 standalone to 0.1852 inside the matrix, +14.8%.

Folding the benchmark into the pool's returns matrix so both sides got the
same shrinkage would be worse, not better: the benchmark's reported
volatility would then depend on which tickers happen to be in the pool, so
the same benchmark over the same months would print differently from one run
to the next and could not be compared across pools at all. A reference point
that moves when you add a candidate is not a reference point. Hence
`benchmark_stats_for_window` deliberately takes only a `BenchmarkSource` and
a window, never the pool - and
`tests/test_benchmark.py::test_the_benchmark_is_not_shrunk_against_a_pool`
fails if that ever changes.

The residual consequence is worth knowing when reading a close call.
Shrinkage moves the PORTFOLIO's reported volatility and Sharpe ratio by
several percent - on that same four-asset pool, its GMV volatility went
0.1474 unshrunk to 0.1527 shrunk and its Sharpe ratio 0.9378 to 1.0505 - in a
direction that depends on the pool's own correlation structure, while leaving
the benchmark's figures untouched. So a Sharpe gap of a couple of hundredths
between portfolio and benchmark sits inside the noise of that estimator
choice and should not be read as decisive; a gap like 0.61 against 1.55
plainly does not. The report deliberately does not print this caveat on every
run, which would make it noise rather than information.

Nothing here fetches anything. `load_benchmark_returns` reads a `returns`
table READ-ONLY and reports a missing file, table or ticker as an empty
series, so a benchmark can be read straight out of the shared
`data/portfolio.duckdb` cache without any possibility of mutating it. The
network side - deciding whether the benchmark needs fetching and where those
rows may be written - lives in `src/flow/interactive.py`'s
`prepare_benchmark`, keeping this layer free of both `src/flow` and yfinance.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import NamedTuple

import numpy as np
import pandas as pd
from pypfopt import expected_returns, risk_models

from src.optimizer.portfolio import load_returns_long

logger = logging.getLogger(__name__)

DEFAULT_BENCHMARKS: dict[str, str] = {"USD": "SPY"}
"""The benchmark assumed for a pool whose currency appears here and that has
recorded no benchmark of its own.

Only USD has a default, deliberately. `SPY` is the uncontested stand-in for
"the US market", so defaulting it costs a person nothing and asks them
nothing. There is no comparably obvious single answer for every other
currency - a yen pool might reasonably be measured against TOPIX or the
Nikkei 225, and picking one here would quietly compare somebody's portfolio
against an index they did not choose. So a currency absent from this table
is ASKED for instead (see `src/flow/cli.py`'s candidate-pool confirm loop),
and a pool that never answers simply reports that no benchmark is set.

Because a default is never written into `memory/candidates.json` - only an
explicit choice is - adding a currency here later still reaches every pool
that never named one.
"""

MIN_USABLE_ANNUAL_VOLATILITY = 1e-12
"""Below this annualized volatility a benchmark is treated as having no
usable estimate rather than as a riskless asset.

A benchmark whose monthly return never changes should have exactly zero
variance, but it does not: `ledoit_wolf()` on a genuinely constant 36-month
series returns 6e-18 rather than 0.0, so a plain `volatility > 0` guard lets
through a Sharpe ratio of 1.8e16 - a number that is not merely useless but
actively misleading, since it would print as the best benchmark ever
measured. Anything under this threshold is floating-point residue, not risk:
an annual volatility of 1e-12 is a ten-billionth of a percent.
"""

BENCHMARK_MIN_MONTHS = 24
"""Fewest monthly returns a benchmark must have inside the portfolio's window
before its figures are reported rather than declined.

The same bar `src/optimizer/portfolio.py`'s `apply_min_history_rule` sets for
a pool ticker (its `min_months` default), and for the same reason: an annual
return and volatility estimated from a handful of months is noise wearing a
number's clothes. Applying the portfolio's own threshold keeps the two sides
of the comparison held to one standard.
"""


class BenchmarkSource(NamedTuple):
    """One benchmark's whole usable return history, resolved once per session.

    `monthly_returns` is indexed by `rebalance_date` with nulls already
    dropped, and covers everything available up to the session's rebalance
    date rather than one particular window - which is what makes an
    interactive edit loop free: every recompute re-slices this series in
    memory instead of touching a database or the network again.

    `unavailable_reason` being set means there are no figures to report and
    says why in one sentence fit to print. It is a normal outcome, not an
    error: a typo'd ticker, a symbol Yahoo Finance does not know, or a
    benchmark trading in the wrong currency all land here, and the report
    prints the reason and carries on. A `None` `ticker` alongside it is the
    narrower case of no benchmark having been chosen at all, where the reason
    names the way to choose one instead of naming a symbol.
    """

    ticker: str | None
    currency: str | None
    monthly_returns: pd.Series
    unavailable_reason: str | None = None


class BenchmarkStats(NamedTuple):
    """The three figures a report puts beside the portfolio's own, measured
    over the portfolio's exact window.

    Either every numeric field is populated and `unavailable_reason` is
    `None`, or every numeric field is `None` and `unavailable_reason` says
    why - there is no half-populated state to guard against at the print
    site. `ticker` is `None` only in the "no benchmark configured at all"
    case, where the reason names the `--benchmark` flag instead of a symbol.

    `window_months` is how many monthly returns actually went into the
    figures, which can be fewer than the portfolio's own count when the
    benchmark listed later than the pool's tickers - the report says both
    numbers rather than implying a match.
    """

    ticker: str | None
    currency: str | None
    annual_return: float | None
    annual_volatility: float | None
    sharpe: float | None
    risk_free_rate: float
    window_start: date | None
    window_end: date | None
    window_months: int
    unavailable_reason: str | None


def resolve_benchmark_ticker(
    currency: str, override: str | None = None, saved: str | None = None
) -> str | None:
    """Which ticker benchmarks a `currency` pool this run: `override` (this
    run's `--benchmark`) if given, else `saved` (what the pool recorded), else
    `DEFAULT_BENCHMARKS`'s entry for `currency`, else `None`.

    `None` is a legitimate answer, not a failure - it is what a pool in a
    currency with no default and no recorded choice gets, and the caller
    turns it into a question (interactively) or into an explanatory line in
    the report (everywhere else). Whitespace is stripped and the result
    upper-cased, matching how every other ticker in this project is stored.
    """
    for candidate in (override, saved, DEFAULT_BENCHMARKS.get(currency)):
        if candidate and candidate.strip():
            return candidate.strip().upper()
    return None


def empty_benchmark_returns(ticker: str | None = None) -> pd.Series:
    """The correctly-shaped, empty monthly-return series a `BenchmarkSource`
    carries when there are no figures - a float series on an empty
    `rebalance_date` index, so slicing and `len()` behave at the report site
    exactly as they do for a populated one.
    """
    return pd.Series(dtype=float, name=ticker, index=pd.DatetimeIndex([], name="rebalance_date"))


def load_benchmark_returns(ticker: str, as_of: date, db_path: str) -> pd.Series:
    """Every monthly return `db_path`'s `returns` table holds for `ticker` on
    or before `as_of`, indexed by `rebalance_date`, ascending, nulls dropped.

    Deliberately unbounded below: the whole history is read once and sliced
    per window afterwards, so an interactive session that recomputes a dozen
    times reads the database once. An empty series - never an exception - is
    returned when the file, the `returns` table, or the ticker is absent,
    which is the ordinary case for a benchmark that has to be fetched first.

    Reads read-only via `load_returns_long`, so pointing this at the shared
    `data/portfolio.duckdb` cache cannot create, lock or modify it.
    """
    long_df = load_returns_long([ticker], None, as_of, db_path, read_only=True)
    if long_df.empty:
        return empty_benchmark_returns(ticker)

    series = (
        long_df.set_index("rebalance_date")["monthly_return"]
        .sort_index()
        .dropna()
        .astype(float)
    )
    series.name = ticker
    return series


def annualized_return_and_volatility(monthly_returns: pd.Series) -> tuple[float, float]:
    """`(annual_return, annual_volatility)` for one series of monthly returns,
    using the identical estimators `src/optimizer/portfolio.py` applies to a
    portfolio's tickers - see this module's docstring for why the volatility
    must come from `CovarianceShrinkage(...).ledoit_wolf()` and never from
    `series.std() * sqrt(12)`.

    Pure: no I/O, no logging, no shared state. `frequency=12` annualizes from
    the monthly cadence `src/dataset/returns.py` produces.

    Takes ONE series and never a pool, deliberately: Ledoit-Wolf shrinkage on
    a single column is inert, so the volatility reported here is the
    benchmark's own and does not vary with whatever else a portfolio happens
    to hold. See this module's docstring for why that independence is the
    point rather than an oversight.
    """
    frame = monthly_returns.to_frame()
    mu = expected_returns.mean_historical_return(frame, returns_data=True, frequency=12)
    cov = risk_models.CovarianceShrinkage(frame, returns_data=True, frequency=12).ledoit_wolf()
    return float(mu.iloc[0]), float(np.sqrt(cov.to_numpy()[0, 0]))


def _unavailable(
    ticker: str | None, currency: str | None, risk_free_rate: float, reason: str
) -> BenchmarkStats:
    """A `BenchmarkStats` carrying only a reason, for every case where there
    are no honest figures to print. `risk_free_rate` is still echoed because
    it describes the run, not the benchmark.
    """
    return BenchmarkStats(
        ticker=ticker,
        currency=currency,
        annual_return=None,
        annual_volatility=None,
        sharpe=None,
        risk_free_rate=float(risk_free_rate),
        window_start=None,
        window_end=None,
        window_months=0,
        unavailable_reason=reason,
    )


def benchmark_stats_for_window(
    source: BenchmarkSource | None,
    window_start: date,
    window_end: date,
    risk_free_rate: float,
    min_months: int = BENCHMARK_MIN_MONTHS,
) -> BenchmarkStats | None:
    """The benchmark's annual return, volatility and Sharpe ratio over
    [`window_start`, `window_end`] - the window a `PortfolioStats` reports as
    its own, so the two sets of figures describe the same months.

    `source=None` means benchmarking is switched off for this run (nobody
    named one and no default applied, or `--benchmark none`), and returns
    `None` so the report prints nothing at all rather than an apology.

    Every other unusable case returns a `BenchmarkStats` carrying only
    `unavailable_reason`: the source itself was unavailable, fewer than
    `min_months` of its months fall inside the window, or the benchmark did
    not move at all across them (see `MIN_USABLE_ANNUAL_VOLATILITY`: a flat
    series is a data problem, not a riskless asset, and dividing by the
    floating-point residue it has for a volatility yields a spectacular
    Sharpe ratio rather than an error).

    The Sharpe ratio is `(annual_return - risk_free_rate) / annual_volatility`
    measured against the same `risk_free_rate` the portfolio's own Sharpe
    ratio used - which is exactly what PyPortfolioOpt's
    `portfolio_performance` computes for a single holding at full weight, so
    the two numbers are comparable by construction rather than by coincidence.
    """
    if source is None:
        return None

    if source.unavailable_reason is not None:
        return _unavailable(source.ticker, source.currency, risk_free_rate, source.unavailable_reason)

    window = source.monthly_returns.loc[pd.Timestamp(window_start) : pd.Timestamp(window_end)]
    if len(window) < min_months:
        return _unavailable(
            source.ticker,
            source.currency,
            risk_free_rate,
            f"{len(window)} month(s) of history for {source.ticker} in this window, "
            f"below the {min_months} required",
        )

    annual_return, annual_volatility = annualized_return_and_volatility(window)
    if not annual_volatility > MIN_USABLE_ANNUAL_VOLATILITY or not np.isfinite(annual_return):
        return _unavailable(
            source.ticker,
            source.currency,
            risk_free_rate,
            f"{source.ticker}'s monthly returns in this window give no usable "
            f"return/volatility estimate (annual volatility {annual_volatility!r})",
        )

    return BenchmarkStats(
        ticker=source.ticker,
        currency=source.currency,
        annual_return=annual_return,
        annual_volatility=annual_volatility,
        sharpe=(annual_return - float(risk_free_rate)) / annual_volatility,
        risk_free_rate=float(risk_free_rate),
        window_start=window.index.min().date(),
        window_end=window.index.max().date(),
        window_months=len(window),
        unavailable_reason=None,
    )
