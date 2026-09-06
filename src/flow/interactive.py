"""Backtest- and live-mode orchestration for `plans/06_interactive_flow.md`:
run LLM-S, LLM-F, the scanner, and the optimizer in sequence for one
rebalance date and bundle every intermediate result for display, reusing
plans 1 through 5's functions unchanged.

Backtest mode (a `rebalance_date` within the stored 2020-01-01..2024-04-30
window) reads directly from `data/portfolio.duckdb`'s cached historical
tables. Live mode (any other date, e.g. "today") builds a fresh,
throwaway snapshot instead - see `src/flow/live.py`'s `build_live_snapshot`
- and runs the exact same downstream sequence against it.

`open_pipeline_session` is what makes the interactive candidate-editing
loop possible: it keeps live mode's throwaway snapshot alive for an
entire CLI session (one initial `run_pipeline_against` call plus any
number of `compute_weights_and_allocation` recomputes against edited
candidate lists), tearing it down only when the `with` block exits -
`run_pipeline` itself is a single-call convenience wrapper around exactly
one such session, for callers (a future backtest runner, tests) that only
need one shot and never edit anything.

`open_holdings_session` and `measure_holdings` are the what-if pair: one
throwaway database resolved once, then any number of hypothetical position
sets measured against it over a single fixed returns window - which is what
makes the change between two variants a number worth subtracting rather
than an artefact of two different windows. See `open_holdings_session` for
why that is a correctness rule.

`prepare_holdings` is a fourth thing a session reports on, and the one
piece here that is not about the candidate pool at all: the user's own
saved holdings (`memory/portfolio.json`), measured with the same estimators
so the three sets of figures - what the optimizer suggests, what the market
did, and what the user actually owns - can be read on one scale. Like the
benchmark, its data is resolved into a database of its own; see that
function for why that is a correctness rule.

`prepare_benchmark` is the third thing a session sets up, beside the
snapshot and the candidates: the ticker standing in for "the market" in the
report, resolved once into its whole monthly-return history so every
subsequent recompute re-slices that history in memory. Its history is always
fetched into a database of its own rather than the session's - see that
function for why that is a correctness rule and not merely a tidy one.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from datetime import date
from typing import NamedTuple

from src.agents.llm_f_signals import screen_month
from src.agents.llm_s import generate_rule
from src.agents.llm_s_schema import ScreeningRule
from src.agents.llm_s_signals import screen
from src.config.settings import settings
from src.dataset.ticker_currency import (
    DEFAULT_CURRENCY,
    MixedCurrencyPoolError,
    group_by_currency,
    load_ticker_currencies,
    partition_by_currency,
)
from src.dataset.holdings_cache import (
    DEFAULT_HOLDINGS_CACHE_PATH,
    refresh_holdings_cache,
    stale_tickers,
)
from src.dataset.ticker_ingestion import validate_and_ingest_tickers
from src.flow.live import build_live_snapshot, build_scratch_snapshot
from src.optimizer.benchmark import (
    BENCHMARK_MIN_MONTHS,
    BenchmarkSource,
    benchmark_stats_for_window,
    empty_benchmark_returns,
    load_benchmark_returns,
)
from src.optimizer.holdings import (
    HOLDINGS_MIN_MONTHS,
    HoldingsStats,
    holdings_stats,
    stored_month_counts,
    unavailable_holdings,
)
from src.optimizer.portfolio import (
    DEFAULT_TARGET_ANNUAL_RETURN,
    PortfolioStats,
    allocate_shares,
    compute_weights_and_stats,
    load_latest_prices,
    load_returns_matrix,
)
from src.scanner.candidate_scanner import scan_with_detail

logger = logging.getLogger(__name__)

VALID_SELECTIONS = ("llm_s_only", "llm_f_only", "llm_s_and_f", "user_provided")


def _is_backtest_date(rebalance_date: date) -> bool:
    """Whether `rebalance_date` falls within the stored 2020-2024 window
    (`settings.rebalance_start`..`settings.rebalance_end`) that
    `data/portfolio.duckdb`'s historical tables actually cover.
    """
    start = date.fromisoformat(settings.rebalance_start)
    end = date.fromisoformat(settings.rebalance_end)
    return start <= rebalance_date <= end


@contextmanager
def open_pipeline_session(rebalance_date: date, selection: str, db_path: str = "data/portfolio.duckdb"):
    """Yield `(effective_db_path, mode)` for `rebalance_date`: `(db_path,
    "backtest")` unchanged for a date within the stored window, or a
    freshly-built live snapshot's temp path and `"live"` for any other
    date - kept alive for the whole `with` block (not torn down after a
    single pipeline call), so a caller can run the initial pipeline and
    then recompute weights/allocation against edited candidate lists any
    number of times before the snapshot is deleted on exit.

    `selection="user_provided"` always gets a snapshot, even for a
    backtest-window date: that mode writes its user-supplied tickers' prices
    and returns into whichever database it is given, and `db_path` (the
    shared historical cache) must never be mutated. Its snapshot is cheap -
    see `build_live_snapshot`, which builds empty tables for it rather than
    fetching anything.
    """
    mode = "backtest" if _is_backtest_date(rebalance_date) else "live"
    if selection == "user_provided" or mode == "live":
        with build_live_snapshot(rebalance_date, selection, source_db_path=db_path) as session_db_path:
            yield session_db_path, mode
    else:
        yield db_path, mode


def _require_single_currency(candidates: list[str], db_path: str) -> None:
    """Raise `MixedCurrencyPoolError` unless every ticker in `candidates`
    trades in the same currency.

    Checked against `candidates` rather than the optimizer's surviving
    holdings so the refusal happens before any work and describes the pool
    the person actually assembled, even if the minimum-history rule would
    later have dropped one currency's tickers entirely.
    """
    if not candidates:
        return

    groups = group_by_currency(candidates, load_ticker_currencies(candidates, db_path))
    if len(groups) > 1:
        detail = "; ".join(f"{currency}: {', '.join(tickers)}" for currency, tickers in groups.items())
        raise MixedCurrencyPoolError(
            f"refusing to build a portfolio that mixes currencies ({detail}). "
            "A portfolio cannot mix currencies; run them as separate portfolios."
        )


def _benchmark_currency_gate(
    ticker: str, found_currency: str, pool_currency: str, monthly_returns
) -> BenchmarkSource:
    """A `BenchmarkSource` for `ticker`, refused when it does not trade in
    `pool_currency`.

    A benchmark in another currency is not arithmetically broken - a monthly
    return is a ratio of one ticker's own prices, so it is a perfectly valid
    number - it is the COMPARISON that breaks: `SPY`'s dollar return set
    beside a yen portfolio's yen return differs by the year's yen/dollar
    move, and the report would present that exchange-rate drift as though it
    were the benchmark out- or under-performing. So it is declined by name
    with the reason, exactly as a cross-currency candidate ticker is.
    """
    if found_currency != pool_currency:
        return BenchmarkSource(
            ticker=ticker,
            currency=found_currency,
            monthly_returns=empty_benchmark_returns(ticker),
            unavailable_reason=(
                f"{ticker} trades in {found_currency} but this portfolio is {pool_currency}; "
                "comparing them would fold an exchange-rate move into the comparison"
            ),
        )
    return BenchmarkSource(ticker, found_currency, monthly_returns, None)


def _resolve_benchmark_source(
    ticker: str, currency: str, rebalance_date: date, db_path: str, allow_fetch: bool
) -> BenchmarkSource:
    """`prepare_benchmark`'s body, split out so every failure mode it can
    raise is caught in one place by its caller.

    Reads `db_path` first because that read is free, offline and read-only,
    and it hits whenever the benchmark is already a pool member (a JPY pool
    benchmarked against `1321.T` that also holds it) or, in a future rebuilt
    cache, already stored. Only when that yields less than
    `BENCHMARK_MIN_MONTHS` of history - the same bar below which the report
    would decline to print figures anyway - is a fetch worth making.
    """
    stored = load_benchmark_returns(ticker, rebalance_date, db_path)
    if len(stored) >= BENCHMARK_MIN_MONTHS:
        found = load_ticker_currencies([ticker], db_path).get(ticker, DEFAULT_CURRENCY)
        return _benchmark_currency_gate(ticker, found, currency, stored)

    if not allow_fetch:
        return BenchmarkSource(
            ticker=ticker,
            currency=None,
            monthly_returns=stored,
            unavailable_reason=(
                f"{ticker} has {len(stored)} month(s) of returns in this session's database "
                "and fetching more was disabled with --no-benchmark-fetch"
            ),
        )

    with build_scratch_snapshot() as scratch_db_path:
        valid, invalid, currencies = validate_and_ingest_tickers([ticker], rebalance_date, scratch_db_path)
        if ticker not in valid:
            return BenchmarkSource(
                ticker=ticker,
                currency=None,
                monthly_returns=empty_benchmark_returns(ticker),
                unavailable_reason=invalid.get(ticker, f"{ticker} produced no usable price history"),
            )
        fetched = load_benchmark_returns(ticker, rebalance_date, scratch_db_path)
        found = currencies.get(ticker, DEFAULT_CURRENCY)

    return _benchmark_currency_gate(ticker, found, currency, fetched)


def prepare_benchmark(
    ticker: str | None,
    currency: str,
    rebalance_date: date,
    db_path: str,
    allow_fetch: bool = True,
) -> BenchmarkSource:
    """Resolve one benchmark ticker into the whole monthly-return history the
    report will slice, once per session.

    The history is fetched into its OWN throwaway database
    (`build_scratch_snapshot`), never into `db_path`, and this is a
    correctness requirement rather than tidiness. Two reasons. The shared
    `data/portfolio.duckdb` cache holds the S&P 500 universe and must not
    gain rows as a side effect of printing a report. And
    `src/optimizer/portfolio.py`'s `_load_window_dates` derives the
    portfolio's own returns window from `SELECT DISTINCT rebalance_date FROM
    returns` - the whole table, not the candidate tickers - so writing
    benchmark months into the session's database could move the very window
    the benchmark is supposed to be measured over. Keeping the two databases
    apart makes "the benchmark never influences the portfolio" structural
    instead of a convention someone has to remember.

    `ticker=None` is the "nobody chose a benchmark and no default applies to
    this currency" case and comes back as an unavailable source naming how to
    choose one - the report then prints that sentence instead of figures.
    Turning benchmarking off entirely is a different thing, expressed by the
    caller passing `None` where a `BenchmarkSource` is expected, which prints
    no benchmark line at all.

    `allow_fetch=False` restricts this to what `db_path` already holds, so a
    backtest-window run against the cached historical tables stays exactly as
    offline as it was before benchmarks existed.

    Never raises. Anything that goes wrong - a yfinance error, a malformed
    response, an unwritable temp directory - becomes an unavailable source
    with the reason attached and a logged warning, because losing a live
    session's fetched snapshot over a benchmark would cost far more than the
    benchmark is worth.
    """
    if ticker is None:
        return BenchmarkSource(
            ticker=None,
            currency=None,
            monthly_returns=empty_benchmark_returns(),
            unavailable_reason=(
                f"no benchmark is set for this {currency} portfolio and none is assumed for "
                f"{currency}; name one with --benchmark TICKER"
            ),
        )

    try:
        return _resolve_benchmark_source(ticker, currency, rebalance_date, db_path, allow_fetch)
    except Exception as e:  # noqa: BLE001 - yfinance and duckdb raise assorted types here
        logger.warning("could not prepare benchmark %s: %s", ticker, e)
        return BenchmarkSource(
            ticker=ticker,
            currency=None,
            monthly_returns=empty_benchmark_returns(ticker),
            unavailable_reason=f"could not prepare {ticker} as a benchmark: {e}",
        )


class HoldingsSession(NamedTuple):
    """One open what-if session: where to measure variants, and whether new
    tickers can still be resolved into it.

    `db_path` is the database every variant is measured against.
    `can_ingest` says whether a ticker the session has not seen yet may be
    fetched into it - false when the caller disabled fetching, because then
    `db_path` is the shared cache rather than a throwaway file and writing
    to it would be exactly the side effect this whole feature promises not
    to have.
    """

    db_path: str
    can_ingest: bool


@contextmanager
def open_holdings_session(
    tickers: list[str],
    rebalance_date: date,
    db_path: str,
    allow_fetch: bool = True,
    cache_path: str = DEFAULT_HOLDINGS_CACHE_PATH,
    force_refresh: bool = False,
):
    """Yield a `HoldingsSession` whose database already holds `tickers`'
    prices and monthly returns, kept alive for a whole interactive what-if
    loop rather than rebuilt per measurement.

    The sibling of `open_pipeline_session`, and for the same two reasons.

    The first is correctness, and it is the reason this exists at all.
    `src/optimizer/portfolio.py`'s `_load_window_dates` derives a
    portfolio's returns window from `SELECT DISTINCT rebalance_date FROM
    returns` over the WHOLE table, so two variants measured against two
    separate throwaway databases can legitimately come back measured over
    two different windows - and two Sharpe ratios computed over different
    months cannot be subtracted from one another. A what-if's entire output
    is that subtraction, so resolving every ticker into ONE database and
    measuring every variant against it is what makes the reported change
    honest rather than plausible-looking. `holdings_stats` reads the
    database and nothing else, so once the session is open every variant is
    a pure in-memory recomputation over a fixed window.

    The second is that it makes the loop usable: the first measurement pays
    one Yahoo Finance round trip for the whole portfolio and every edit
    after it is instant. Compare `_resolve_holdings`, which fetches per
    call - correct for a one-shot report, ruinous for a loop.

    Since Milestone 6 of `plans/13_user_portfolio.md` the database is the
    PERSISTENT holdings cache rather than a file deleted on exit, so the
    first measurement of a session usually costs nothing either - the cache
    is refreshed only for the tickers whose latest month it is missing. What
    it is NOT, in either version, is `db_path`: see the cache module's
    docstring for why holdings rows must never land in the database a
    candidate pool is being measured against.

    With `allow_fetch=False` the session still yields the cache but reports
    `can_ingest=False`. `--no-holdings-fetch` means "touch no network", so
    reading what the cache already holds is fine while adding a ticker it
    does not hold is not - the caller refuses such an add by name rather
    than reaching for the network behind the flag's back.
    """
    if not allow_fetch:
        yield HoldingsSession(db_path=cache_path, can_ingest=False)
        return

    if tickers:
        refresh_holdings_cache(sorted(tickers), rebalance_date, cache_path, force=force_refresh)
    yield HoldingsSession(db_path=cache_path, can_ingest=True)


def measure_holdings(
    positions: dict[str, float],
    currency: str,
    rebalance_date: date,
    session: HoldingsSession,
    risk_free_rate: float = settings.risk_free_rate,
    currencies: dict[str, str] | None = None,
) -> HoldingsStats:
    """Measure one variant against an open `session`, with no fetching and
    no writes of any kind.

    The what-if counterpart to `prepare_holdings`: that function resolves
    its own data and is right for a one-shot report, while this one assumes
    the data is already resolved and is right for a loop that measures many
    variants over one window. Both end in `_holdings_stats_excluding`, so a
    holding excluded for trading in the wrong currency or for having too
    little history is reported the same way in either.

    `currencies` is what the session learned about each ticker as it was
    ingested; anything absent from it is looked up in the session's own
    database, and anything absent from both is assumed to be `currency` -
    the same assumption `_holdings_currency_gate` already makes.

    Never raises, for the same reason `prepare_holdings` never does: losing
    a session mid-loop over one unmeasurable variant would cost the person
    every fetch the session has already paid for.
    """
    if not positions:
        return holdings_stats({}, rebalance_date, session.db_path, currency, risk_free_rate)

    try:
        known = dict(currencies or {})
        unknown = [ticker for ticker in positions if ticker not in known]
        if unknown:
            known.update(load_ticker_currencies(unknown, session.db_path))
        return _holdings_stats_excluding(
            positions,
            currency,
            rebalance_date,
            session.db_path,
            risk_free_rate,
            _holdings_currency_gate(positions, currency, known),
        )
    except Exception as e:  # noqa: BLE001 - duckdb and pandas raise assorted types here
        logger.warning("could not measure a %s what-if variant: %s", currency, e)
        return unavailable_holdings(
            currency, positions, risk_free_rate, f"could not measure this variant: {e}"
        )


def _holdings_currency_gate(
    positions: dict[str, float], currency: str, currencies: dict[str, str]
) -> dict[str, str]:
    """Which held tickers do not trade in `currency`, mapped to a sentence
    saying so - the holdings equivalent of `_benchmark_currency_gate`.

    `memory/portfolio.json` keys its portfolios by currency, and
    `uv run portfolio-holdings` refuses a cross-currency ticker as it is
    typed, so this should normally find nothing. It is still worth checking,
    for the same reason `_require_single_currency` guards the optimizer: the
    file is hand-editable, and the failure this prevents is not a crash but
    a silently wrong report. Summing a yen holding's value into a dollar
    total would produce a `Total value` that is not an amount of anything,
    and weights derived from it would be wrong for every holding, not just
    the misplaced one.

    Reported as exclusions rather than raised, because this is a report:
    naming the offending holding and measuring the rest is more useful than
    printing nothing.
    """
    return {
        ticker: (
            f"priced in {currencies.get(ticker, DEFAULT_CURRENCY)}, "
            f"but this is the {currency} portfolio"
        )
        for ticker in positions
        if currencies.get(ticker, DEFAULT_CURRENCY) != currency
    }


def _holdings_stats_excluding(
    positions: dict[str, float],
    currency: str,
    rebalance_date: date,
    db_path: str,
    risk_free_rate: float,
    excluded: dict[str, str],
) -> HoldingsStats:
    """`holdings_stats` over the holdings NOT in `excluded`, with those
    exclusions merged back into the result and `positions` restored to the
    full set the user actually owns.

    An already-excluded holding is withheld from `holdings_stats` rather
    than merely annotated afterwards, because that function's
    `total_value` sums every position it is given - and a holding excluded
    for trading in the wrong currency, or for not resolving at all, must not
    contribute to a total denominated in this portfolio's currency.
    """
    measurable = {t: shares for t, shares in positions.items() if t not in excluded}
    if not measurable:
        return unavailable_holdings(
            currency,
            positions,
            risk_free_rate,
            "no holding could be measured: " + "; ".join(
                f"{ticker} ({reason})" for ticker, reason in sorted(excluded.items())
            ),
            excluded=excluded,
        )

    stats = holdings_stats(measurable, rebalance_date, db_path, currency, risk_free_rate)
    return stats._replace(positions=positions, excluded={**excluded, **stats.excluded})


def _resolve_holdings(
    positions: dict[str, float],
    currency: str,
    rebalance_date: date,
    db_path: str,
    risk_free_rate: float,
    allow_fetch: bool,
    cache_path: str = DEFAULT_HOLDINGS_CACHE_PATH,
    force_refresh: bool = False,
) -> HoldingsStats:
    """`prepare_holdings`'s body, split out so every failure mode it can
    raise is caught in one place by its caller.

    Reads `db_path` first because that read is free, offline and read-only,
    and it hits whenever the holdings are already cached - every holding of
    a backtest-window run against `data/portfolio.duckdb`, for instance.
    Only when that yields less than `HOLDINGS_MIN_MONTHS` for a holding -
    the same bar below which the report would decline to measure it anyway -
    is a fetch worth making.

    The condition is deliberately "EVERY holding has enough" rather than
    "some holding does". In a live `user_provided` run the session database
    holds only the candidate pool's tickers, so a partial hit there would
    quietly measure the holdings that happen to also be candidates and
    exclude every other one for thin history - reporting a number for a
    slice of the portfolio chosen by an unrelated coincidence.

    Failing that, the persistent holdings cache
    (`src/dataset/holdings_cache.py`) is consulted, and only the tickers it
    cannot answer for are fetched - into the cache, so the next run pays
    nothing. That file is neither `db_path` nor the shared
    `data/portfolio.duckdb`, which is what preserves the isolation rule the
    throwaway database was originally built for: see the cache module's
    docstring.

    `allow_fetch=False` still reaches the cache. The flag means "touch no
    network", and reading a local file is not a network call - so an offline
    run gets whatever the cache holds, with its price date stated, rather
    than nothing at all.
    """
    tickers = sorted(positions)
    counts = stored_month_counts(tickers, rebalance_date, db_path)
    if all(count >= HOLDINGS_MIN_MONTHS for count in counts.values()):
        currencies = load_ticker_currencies(tickers, db_path)
        return _holdings_stats_excluding(
            positions, currency, rebalance_date, db_path, risk_free_rate,
            _holdings_currency_gate(positions, currency, currencies),
        )

    excluded: dict[str, str] = {}
    if allow_fetch:
        valid, invalid, currencies = refresh_holdings_cache(
            tickers, rebalance_date, cache_path, force=force_refresh
        )
        excluded.update(invalid)
        excluded.update(
            _holdings_currency_gate(
                {t: positions[t] for t in valid if t in positions}, currency, currencies
            )
        )
    else:
        cache_counts = stored_month_counts(tickers, rebalance_date, cache_path)
        short = sorted(t for t in tickers if cache_counts[t] < HOLDINGS_MIN_MONTHS)
        if short:
            return unavailable_holdings(
                currency,
                positions,
                risk_free_rate,
                f"{', '.join(short)} ha{'s' if len(short) == 1 else 've'} under "
                f"{HOLDINGS_MIN_MONTHS} month(s) of returns in this session's database or the "
                "holdings cache, and fetching more was disabled with --no-holdings-fetch",
            )
        excluded.update(
            _holdings_currency_gate(
                positions, currency, load_ticker_currencies(tickers, cache_path)
            )
        )

    return _holdings_stats_excluding(
        positions, currency, rebalance_date, cache_path, risk_free_rate, excluded
    )


def prepare_holdings(
    positions: dict[str, float],
    currency: str,
    rebalance_date: date,
    db_path: str,
    risk_free_rate: float = settings.risk_free_rate,
    allow_fetch: bool = True,
    cache_path: str = DEFAULT_HOLDINGS_CACHE_PATH,
    force_refresh: bool = False,
) -> HoldingsStats:
    """Measure the user's own saved portfolio (`positions`, as read from
    `memory/portfolio.json` by `src/flow/user_portfolio.py`), resolving the
    prices and monthly returns it needs first.

    The prices and returns are fetched into the persistent holdings cache
    (`src/dataset/holdings_cache.py`), never into `db_path`, and this is a
    correctness requirement rather than tidiness - the same rule
    `prepare_benchmark` obeys, for the same two reasons. Until Milestone 6
    of `plans/13_user_portfolio.md` the destination was a throwaway database
    deleted moments later; making it persistent changed where the rows live
    but not the rule that keeps them out of `db_path`. The shared
    `data/portfolio.duckdb` cache holds the S&P 500 universe and must not
    gain rows as a side effect of printing a report. And
    `src/optimizer/portfolio.py`'s `_load_window_dates` derives a
    portfolio's returns window from `SELECT DISTINCT rebalance_date FROM
    returns` - the whole table, not the requested tickers - so writing the
    holdings' months into the session's database could move the very window
    the candidate pool being optimized is measured over. Keeping the
    databases apart makes "the user's holdings never influence the
    optimizer's inputs" structural instead of a convention someone has to
    remember.

    An empty `positions` never touches a database at all: it comes straight
    back as the "nothing saved for this currency" report, which is a
    legitimate state rather than an error.

    `allow_fetch=False` restricts this to what `db_path` already holds, so a
    backtest-window run stays entirely offline - at the cost of reporting
    the holdings as unmeasurable when the cache does not contain them.

    Never raises. Anything that goes wrong - a yfinance error, a malformed
    response, an unwritable temp directory - becomes an unavailable report
    with the reason attached and a logged warning, because losing a live
    session's fetched snapshot over one report block would cost far more
    than the block is worth.
    """
    if not positions:
        return holdings_stats({}, rebalance_date, db_path, currency, risk_free_rate)

    try:
        return _resolve_holdings(
            positions, currency, rebalance_date, db_path, risk_free_rate, allow_fetch,
            cache_path, force_refresh,
        )
    except Exception as e:  # noqa: BLE001 - yfinance and duckdb raise assorted types here
        logger.warning("could not measure the saved %s portfolio: %s", currency, e)
        return unavailable_holdings(
            currency,
            positions,
            risk_free_rate,
            f"could not measure the saved {currency} portfolio: {e}",
        )


def compute_weights_and_allocation(
    candidates: list[str],
    objective: str,
    portfolio_value: float,
    rebalance_date: date,
    db_path: str,
    target_annual_return: float = DEFAULT_TARGET_ANNUAL_RETURN,
    risk_free_rate: float = settings.risk_free_rate,
) -> tuple[PortfolioStats, tuple[dict[str, int], float]]:
    """`compute_weights_and_stats` + `allocate_shares` for `candidates` as of
    `rebalance_date`, reading `db_path` - the part of the pipeline an
    interactive edit re-runs, deliberately never the two LLM agents, since
    editing the candidate list is the user overriding the agents' already-
    given recommendation, not asking them to reconsider it.

    Returns the full `PortfolioStats` rather than only its weights so the CLI
    can report the expected returns, volatilities, and Sharpe ratio behind an
    allocation. `target_annual_return` applies only to `objective="MV"`, the
    one objective defined by a target.

    Raises `MixedCurrencyPoolError` if `candidates` do not all trade in one
    currency. The interactive loops already refuse a cross-currency ticker as
    it is typed, so this is a last line of defense - but a reachable one, via
    a hand-edited `memory/candidates.json` or a direct `run_pipeline` call
    that has no confirmation loop, and the failure it prevents is silently
    misallocated money rather than a crash. In every other selection and in
    backtest mode there is no `ticker_currency` table, so the check is one
    cheap query that finds a single implicit US-dollar group.
    """
    _require_single_currency(candidates, db_path)
    returns_matrix = load_returns_matrix(candidates, as_of=rebalance_date, db_path=db_path)
    stats = compute_weights_and_stats(returns_matrix, objective, target_annual_return, risk_free_rate)

    latest_prices = load_latest_prices(list(stats.weights.keys()), as_of=rebalance_date, db_path=db_path)
    allocation = allocate_shares(stats.weights, latest_prices, portfolio_value)
    return stats, allocation


def run_scan(
    rebalance_date: date,
    selection: str,
    db_path: str,
    rule: ScreeningRule | None = None,
    candidates: list[str] | None = None,
) -> dict:
    """LLM-S (`generate_rule` + `screen`) and/or LLM-F (`screen_month`) per
    `selection`, then the scanner (`scan_with_detail`) - the part of the
    pipeline before the optimizer. Returns a dict with `rule`,
    `llm_s_signals`, `llm_f_signals`, `scan_detail`.

    `rule` lets a caller that manages its own already-generated
    `ScreeningRule` skip the `generate_rule` call entirely - used by
    `src/flow/backtest.py`'s `run_full_backtest`, which must call
    `generate_rule` at most once per calendar year across a 52-month run
    (see that module's docstring for why a fresh LLM-S call per *month*
    would be a correctness bug, not just a wasted one) rather than once
    per call to this function. Left `None` (the default), `generate_rule`
    is still called exactly when `selection` needs LLM-S, matching every
    other caller's existing behavior.

    `candidates` is the user's own already-confirmed ticker list, used only
    by `selection="user_provided"`, which returns it directly in a
    `scan_with_detail`-shaped dict without calling either agent or the
    scanner at all - there are no signals to combine when the user, not an
    agent, chose the candidates.
    """
    if selection == "user_provided":
        return {
            "rule": None,
            "llm_s_signals": None,
            "llm_f_signals": None,
            "scan_detail": {
                "candidates": sorted(set(candidates or [])),
                "branch": "user_provided",
                "buy_s_size": None,
                "buy_f_size": None,
                "intersection_size": None,
                "union_size": None,
            },
        }

    if rule is None and selection in ("llm_s_only", "llm_s_and_f"):
        rule = generate_rule(rebalance_date.year, db_path=db_path)

    llm_s_signals = None
    if selection in ("llm_s_only", "llm_s_and_f"):
        llm_s_signals = screen(rule, rebalance_date, db_path=db_path)

    llm_f_signals = None
    if selection in ("llm_f_only", "llm_s_and_f"):
        llm_f_signals = screen_month(rebalance_date.year, rebalance_date.month, db_path=db_path)

    scan_detail = scan_with_detail(llm_s_signals, llm_f_signals)
    return {
        "rule": rule,
        "llm_s_signals": llm_s_signals,
        "llm_f_signals": llm_f_signals,
        "scan_detail": scan_detail,
    }


def run_pipeline_against(
    rebalance_date: date,
    objective: str,
    portfolio_value: float,
    selection: str,
    db_path: str,
    mode: str,
    rule: ScreeningRule | None = None,
    candidates: list[str] | None = None,
    target_annual_return: float = DEFAULT_TARGET_ANNUAL_RETURN,
    risk_free_rate: float = settings.risk_free_rate,
    currency: str = DEFAULT_CURRENCY,
    benchmark: BenchmarkSource | None = None,
) -> dict:
    """The shared sequence behind both modes: `run_scan` (LLM-S/LLM-F/the
    scanner) followed by the optimizer (`compute_weights_and_allocation`)
    - all reading `db_path`, whichever database that is. Exposed (not
    module-private) so a CLI session opened with `open_pipeline_session`
    can call this once for the initial run and then call
    `compute_weights_and_allocation` directly on its own for every
    subsequent edit, without repeating LLM-S/LLM-F/the scanner. `rule` and
    `candidates` are passed straight through to `run_scan` - see its
    docstring.

    `"weights"` stays a plain ticker-to-weight mapping, which is what every
    caller reading that key means by it; the `PortfolioStats` carrying the
    figures behind those weights is added alongside it under `"stats"`.

    `benchmark` is an already-resolved `BenchmarkSource` (see
    `prepare_benchmark`), narrowed here to the portfolio's own returns window
    and reported under `"benchmark"`. It defaults to `None`, meaning no
    benchmark, so no programmatic caller of this function ever acquires a
    network fetch it did not ask for - applying a per-currency default is the
    CLI's job, not this layer's.
    """
    scan = run_scan(rebalance_date, selection, db_path, rule=rule, candidates=candidates)
    stats, allocation = compute_weights_and_allocation(
        scan["scan_detail"]["candidates"], objective, portfolio_value, rebalance_date, db_path,
        target_annual_return=target_annual_return, risk_free_rate=risk_free_rate,
    )

    return {
        "mode": mode,
        "rebalance_date": rebalance_date,
        "objective": objective,
        "selection": selection,
        **scan,
        "weights": stats.weights,
        "allocation": allocation,
        "stats": stats,
        "currency": currency,
        "benchmark": benchmark_stats_for_window(
            benchmark, stats.returns_window_start, stats.returns_window_end, risk_free_rate
        ),
    }


def edit_candidates(scan_result: dict, add: list[str], remove: list[str]) -> list[str]:
    """`scan_result["candidates"]` (as produced by `scan_with_detail`) with
    every ticker in `add` not already present added, and every ticker in
    `remove` that is present removed - returns the resulting sorted list.

    Deliberately simple set manipulation: by this point in the pipeline
    both agents have already spoken, and the user is directly overriding
    their combined recommendation, the same way
    `plans/04_candidate_scanner.md`'s `scan` function already accepts an
    arbitrary user-supplied ticker list rather than only the scanner's own
    output.
    """
    candidates = set(scan_result["candidates"])
    candidates |= set(add)
    candidates -= set(remove)
    return sorted(candidates)


class CandidateEditResult(NamedTuple):
    """One `validate_and_edit_candidates` outcome.

    `invalid` and `refused` are different rejections and are reported
    differently: `invalid` maps a ticker to why it could not be used at all
    (it did not resolve, or its currency could not be determined), while
    `refused` maps a ticker to its own currency, meaning it is perfectly
    valid but cannot share a portfolio with this pool. `pool_currency` is
    the currency the pool holds after the edit, or `None` when the pool is
    empty and the next add will establish it.
    """

    pool: list[str]
    added: list[str]
    invalid: dict[str, str]
    refused: dict[str, str]
    pool_currency: str | None


def in_typed_order(valid_added: list[str], add: list[str]) -> list[str]:
    """`valid_added` (which `validate_and_ingest_tickers` returns sorted) put
    back into the order the person typed in `add`.

    This matters only for an empty pool, where the first ticker establishes
    the pool's currency: typing `AAPL 7203.T` must give a dollar pool, but
    sorted order puts `'7203.T'` first (digits sort before letters) and
    would silently make it a yen pool instead.

    Public because `src/flow/holdings_cli.py` needs the identical guarantee
    when a typed ticker establishes which currency's PORTFOLIO an edit lands
    in - the same hazard, and it must not be solved twice.
    """
    first_seen: dict[str, int] = {}
    for i, ticker in enumerate(add):
        first_seen.setdefault(ticker.strip().upper(), i)
    return sorted(valid_added, key=lambda t: first_seen.get(t, len(add)))


def validate_and_edit_candidates(
    pool: list[str],
    add: list[str],
    remove: list[str],
    as_of: date,
    db_path: str,
    pool_currency: str | None = None,
) -> CandidateEditResult:
    """`edit_candidates` for the `user_provided` selection: every ticker in
    `add` must first resolve on Yahoo Finance (and have its prices, returns
    and trading currency ingested into `db_path`) before it joins the pool,
    so a typo'd or delisted symbol is reported rather than silently
    surviving until the optimizer drops or chokes on it.

    A ticker that resolves but trades in a different currency from
    `pool_currency` is `refused` rather than added, because a portfolio
    whose prices carry two different units cannot be allocated correctly -
    see `src/dataset/ticker_currency.py`. One refused or unresolvable ticker
    never blocks the good ones typed alongside it, and `remove` needs no
    validation, since removing a ticker that isn't in the pool is already a
    harmless no-op in set arithmetic.
    """
    if add:
        valid_added, invalid, currencies = validate_and_ingest_tickers(add, as_of, db_path)
    else:
        valid_added, invalid, currencies = [], {}, {}

    accepted, refused, pool_currency = partition_by_currency(
        in_typed_order(valid_added, add), pool_currency, currencies
    )
    new_pool = edit_candidates({"candidates": pool}, add=accepted, remove=remove)

    if not new_pool:
        pool_currency = None
    elif remove:
        # A removal can retire the last ticker of the pool's currency, so
        # re-derive it from whatever survived rather than trusting the
        # incoming value.
        surviving = group_by_currency(new_pool, load_ticker_currencies(new_pool, db_path))
        pool_currency = next(iter(surviving)) if len(surviving) == 1 else pool_currency

    return CandidateEditResult(new_pool, accepted, invalid, refused, pool_currency)


def run_pipeline(
    rebalance_date: date,
    objective: str,
    portfolio_value: float,
    selection: str = "llm_s_only",
    db_path: str = "data/portfolio.duckdb",
    candidates: list[str] | None = None,
    target_annual_return: float = DEFAULT_TARGET_ANNUAL_RETURN,
    risk_free_rate: float = settings.risk_free_rate,
    currency: str = DEFAULT_CURRENCY,
    benchmark: BenchmarkSource | None = None,
) -> dict:
    """Run the full pipeline for one `rebalance_date`, as a single,
    self-contained call - see `open_pipeline_session` for a CLI-style
    session that keeps live mode's snapshot alive across multiple calls
    for interactive editing.

    `selection` (one of `"llm_s_only"` (the default), `"llm_f_only"`,
    `"llm_s_and_f"`, `"user_provided"`) controls which agent(s) actually
    run, per README's Backtest Mode Stage 1 default-selection sentence and
    `plans/08_consistency_review.md` finding 9: the skipped agent's call is
    never made (not merely its result discarded), since README frames
    LLM-F evaluation as "expensive". Raises `ValueError` for any other
    `selection` value.

    `"user_provided"` runs neither agent, taking its candidate list from
    `candidates` instead (already validated and ingested by the caller, or
    by `validate_and_edit_candidates`).

    Backtest mode (`rebalance_date` within the stored 2020-2024 window)
    reads `db_path`'s cached historical tables directly. Live mode (any
    other date) first builds a fresh, throwaway snapshot (a real
    Wikipedia/yfinance/SEC EDGAR fetch, not a read from `db_path`) and runs
    the identical downstream sequence against that instead - `db_path` is
    used there only as the source of the static `news_articles_hf` archive
    `screen_month` needs.

    `target_annual_return` is the annual return `objective="MV"` optimizes
    toward and is ignored by the other two objectives; `risk_free_rate` is
    the rate MSR maximizes its Sharpe ratio against and that every objective's
    reported Sharpe ratio is measured against. `currency` names the unit
    `portfolio_value` is expressed in and is echoed back for display; it does
    not convert anything, since every ticker in one portfolio must already
    share a currency (enforced by `compute_weights_and_allocation`).

    Returns a dict bundling every intermediate result: `mode`
    (`"backtest"` or `"live"`), `rule` (LLM-S's `ScreeningRule`, `None` if
    skipped), `llm_s_signals`/`llm_f_signals` (each a `ticker`/`signal`
    DataFrame, `None` if skipped), `scan_detail` (`scan_with_detail`'s full
    output, including the branch taken), `weights`, `allocation` (the
    `(shares_per_ticker, leftover_cash)` tuple from `allocate_shares`), and
    `stats` (the `PortfolioStats` behind those weights), and `benchmark` (the
    `BenchmarkStats` for `benchmark`'s ticker over the same returns window, or
    `None` when no `BenchmarkSource` was supplied) - so a CLI layer can
    display why each ticker is or is not a candidate, what the optimizer
    expected of the ones it kept, and how that compares with simply holding
    the market, not just the final share counts.
    """
    if selection not in VALID_SELECTIONS:
        raise ValueError(f"selection must be one of {VALID_SELECTIONS}, got {selection!r}")

    with open_pipeline_session(rebalance_date, selection, db_path) as (effective_db_path, mode):
        return run_pipeline_against(
            rebalance_date, objective, portfolio_value, selection, effective_db_path, mode,
            candidates=candidates, target_annual_return=target_annual_return, risk_free_rate=risk_free_rate,
            currency=currency, benchmark=benchmark,
        )
