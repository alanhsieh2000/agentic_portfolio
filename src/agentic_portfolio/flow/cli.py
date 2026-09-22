"""CLI entry point for `plans/06_interactive_flow.md`'s backtest and live
modes: `uv run python -m agentic_portfolio.flow.cli --date YYYY-MM-DD|today --objective
GMV|MV|MSR --value 100000 [--target-return 0.12] [--risk-free-rate 0.02]
[--selection llm_s_only|llm_f_only|llm_s_and_f|user_provided]`.

Prints the initial pipeline result - which mode ran, LLM-S's rule (if
run), the scanner's branch and candidate list, the weights, the expected
return/volatility behind them, the portfolio's own expected return,
volatility and Sharpe ratio, the same three figures for the benchmark the
pool is measured against, and the share allocation - so a person can see
why each ticker is or is not a candidate, what the optimizer expected of
the ones it kept, and whether all of it beat simply holding the market,
not just the final number of shares. Then
enters an interactive loop letting the user add/remove candidate tickers,
change the objective, change MV's target return, or vary the trailing returns
window from 24 through 60 months; each edit re-runs only
`compute_weights_and_allocation` (never LLM-S or LLM-F again - the user is
overriding the agents' already-given recommendation, not asking them to
reconsider it) and reprints the updated candidates, weights, and
allocation. `open_pipeline_session` keeps live mode's throwaway snapshot
alive for this entire loop, not just the initial run.

The benchmark defaults to whatever the pool recorded, else the currency's
own default (`SPY` for USD), else - for a currency this project makes no
assumption about - whatever the person is asked for once and thereafter
remembered. `--benchmark` overrides it for a run, `[b]` changes it
mid-session, and `--benchmark none` leaves the comparison out.

The risk-free rate every Sharpe ratio here is measured against is
per-currency, remembered in `memory/rates.json` (see
`src/agentic_portfolio/flow/rate_memory.py`) and shared with `uv run portfolio-holdings`.
`--risk-free-rate` overrides it for the run and is remembered for the run's
currency; left out, the rate is whatever that currency remembered, else the
configured `RISK_FREE_RATE`, else 2%. One resolved rate reaches the pool's
figures, the benchmark line and the holdings block alike, and every one of
them prints which source it came from.

The report ends with the portfolio the user ACTUALLY holds, read from
`memory/portfolio.json` (maintained by `uv run portfolio-holdings`) for
this run's own currency: what is held, what it is worth, and its
annualized return, volatility and Sharpe ratio measured with the same
estimators and the same `--risk-free-rate` as the pool's and the
benchmark's - so the optimizer's suggestion, the market, and what the
person owns can all be read on one scale. `--no-holdings` leaves that
block out; `--no-holdings-fetch` restricts it to cached data.

`--selection user_provided` runs neither agent. It instead shows the
candidate pool persisted at `--memory-path` (`memory/candidates.json` by
default) and prompts add/remove until the user confirms it, validating each
added ticker against Yahoo Finance so a typo is named rather than silently
carried into the optimizer - then feeds the confirmed pool through the same
optimizer and post-run edit loop every other selection uses. That file
holds one pool per currency, since a portfolio can only hold one; when
several are saved, the user is asked which to resume - or names one with
`--currency`, which answers that question (and the mixed-pool repair
question behind it) from the command line so the run can be scripted.
"""

from __future__ import annotations

import argparse

import pandas as pd
from datetime import date

from agentic_portfolio.config.preflight import require_api_keys
from agentic_portfolio.config.settings import settings
from agentic_portfolio.dataset.ticker_currency import DEFAULT_CURRENCY, group_by_currency
from agentic_portfolio.dataset.holdings_cache import DEFAULT_HOLDINGS_CACHE_PATH
from agentic_portfolio.dataset.ticker_ingestion import validate_and_ingest_tickers
from agentic_portfolio.flow.candidate_memory import (
    DEFAULT_CANDIDATES_PATH,
    load_all_pools,
    load_candidate_benchmark,
    load_pool_benchmarks,
    save_candidate_pool,
)
from agentic_portfolio.flow.interactive import (
    VALID_SELECTIONS,
    compute_weights_and_allocation,
    edit_candidates,
    open_pipeline_session,
    prepare_benchmark,
    prepare_holdings,
    prepare_ticker_summary,
    run_pipeline_against,
    validate_and_edit_candidates,
)
from agentic_portfolio.flow.report_archive import ReportArchive, command_line, record_report
from agentic_portfolio.flow.rate_memory import (
    DEFAULT_RATES_PATH,
    ResolvedRiskFreeRate,
    load_risk_free_rate,
    resolve_risk_free_rate,
    save_risk_free_rate,
    validate_risk_free_rate,
)
from agentic_portfolio.flow.user_portfolio import DEFAULT_PORTFOLIO_PATH, load_portfolio
from agentic_portfolio.optimizer.benchmark import (
    BenchmarkSource,
    BenchmarkStats,
    benchmark_stats_for_window,
    resolve_benchmark_ticker,
)
from agentic_portfolio.optimizer.dividends import (
    DIVIDEND_BINDING_TOLERANCE,
    MAX_DIVIDEND_YIELD,
    NO_DIVIDEND_FIGURES,
    DividendFloor,
    validate_dividend_yield,
    validate_min_annual_dividend,
)
from agentic_portfolio.optimizer.holdings import (
    DEFAULT_LOOKBACK_MONTHS,
    HoldingsStats,
    validate_lookback_months,
)
from agentic_portfolio.optimizer.portfolio import DEFAULT_TARGET_ANNUAL_RETURN, PortfolioStats, VALID_OBJECTIVES
from agentic_portfolio.dataset.ticker_profile import TickerProfile
from agentic_portfolio.optimizer.ticker_stats import TickerStats


CURRENCY_SYMBOLS = {"USD": "$", "JPY": "¥", "GBP": "£", "EUR": "€"}
"""Symbols for the currencies most likely to come up, used alongside - never
instead of - the ISO code. A bare '$' is shared by the US, Canadian,
Australian, Hong Kong and Singapore dollars, and removing exactly that
ambiguity is the point of tracking currency at all, so the code is always
printed too. A currency absent from this table simply prints its code.
"""


def parse_date(value: str) -> date:
    """A `--date` argument as a `date`: an ISO `YYYY-MM-DD`, or `"today"`.

    Public because `src/agentic_portfolio/flow/holdings_cli.py` parses the same `--date`
    argument and must accept exactly the same values - two commands that
    disagreed about what `"today"` means would be worse than the shared
    name.
    """
    return date.today() if value == "today" else date.fromisoformat(value)


def format_money(amount: float, currency: str) -> str:
    """A money amount with both its symbol (when known) and its ISO code, so
    the unit is never ambiguous: `'$12.34 USD'`, `'¥12.34 JPY'`, `'12.34 SGD'`.
    """
    return f"{CURRENCY_SYMBOLS.get(currency, '')}{amount:,.2f} {currency}"


BENCHMARK_DISABLED = "none"
"""The `--benchmark` value that switches the comparison off entirely, as
opposed to naming a ticker or leaving the flag out (which takes the pool's
recorded benchmark, or this currency's default, or asks).

Distinguishing "off" from "none available" matters in the report: switched
off prints no benchmark line at all, while none available prints a line
saying so, because in the second case the person may well want to fix it.
"""


def format_benchmark(benchmark: BenchmarkStats, portfolio_window_months: int) -> str:
    """The one report line that puts the benchmark's figures beside the
    portfolio's, or explains why there are none.

    The month count is always printed as "N of M" against the portfolio's own
    window rather than being left implied, because the two can legitimately
    differ - a benchmark that listed partway through the window is still worth
    comparing against, but only if the reader can see that it covers fewer
    months than the portfolio does.
    """
    label = f"Benchmark {benchmark.ticker}" if benchmark.ticker else "Benchmark"
    if benchmark.unavailable_reason is not None:
        return f"{label}: n/a - {benchmark.unavailable_reason}"
    return (
        f"{label}: return={benchmark.annual_return:.4f}  "
        f"volatility={benchmark.annual_volatility:.4f}  "
        f"Sharpe={benchmark.sharpe:.4f}  "
        f"({benchmark.window_months} of {portfolio_window_months} month(s))"
    )


def format_risk_free_rate(rate: float, origin: str | None = None) -> str:
    """The one report line stating which risk-free rate the Sharpe ratios
    above it were measured against, and where that rate came from.

    `origin` (from `src/agentic_portfolio/flow/rate_memory.py`'s `ResolvedRiskFreeRate`) is
    optional only so that the many existing direct callers of the two
    printing functions - and their tests - need no change. Every real
    reporting path supplies it, and it is populated in every case including
    the plain default, because a rate printed without its source is exactly
    what the per-currency bug looked like: a bare, entirely plausible
    `0.0200` that a reader could not tell from a deliberate choice.
    """
    suffix = f" ({origin})" if origin else ""
    return f"Risk-free rate used: {rate:.4f}{suffix}"


def format_signed_money(amount: float, currency: str = DEFAULT_CURRENCY) -> str:
    """A signed money amount: `'+$1,240.00 USD'`, `'-$310.00 USD'`.

    `format_money` cannot render a delta directly - it would produce
    `'$-310.00 USD'`, with the sign wedged between the symbol and the
    digits. The sign belongs in front of the whole quantity, where a reader
    scanning a column of deltas expects to find it.
    """
    return f"{'+' if amount >= 0 else '-'}{format_money(abs(amount), currency)}"


def format_dividend_coverage(
    covered: float | None,
    missing: tuple[str, ...],
    reasons: dict[str, str] | None = None,
) -> str:
    """The indented caveat under a portfolio dividend yield that does not
    describe the whole portfolio.

    Printed only when some ticker's yield is unknown, and it names both the
    share of weight the figure covers and the tickers left out. The
    alternative - quietly diluting the yield toward zero by treating an
    unknown as a zero - would report a number that is not the answer to any
    question. This is the rule `src/agentic_portfolio/optimizer/holdings.py` already applies
    to a holding it cannot measure: shrink the denominator and say so,
    rather than withhold a correct answer about the rest.
    """
    names = ", ".join(missing)
    verb = "has" if len(missing) == 1 else "have"
    tail = "it is left out" if len(missing) == 1 else "they are left out"
    share = f"{covered:.4f}" if covered is not None else "an unknown share"
    line = (
        f"  covers {share} of the weight; {names} {verb} no trailing dividend data, so {tail}"
    )
    # The reason goes on its own indented line per ticker rather than into
    # the sentence above: these sentences name dates and ranges, and one of
    # them inlined would push this line past anything readable in a
    # terminal - while two of them would make it unparseable.
    detail = [f"    {t}: {reasons[t]}" for t in missing if (reasons or {}).get(t)]
    return "\n".join([line, *detail]) if detail else line


def format_split_restatement(
    splits_in_window: dict | None,
    dividends_per_share: dict[str, float] | None = None,
    currency: str = DEFAULT_CURRENCY,
) -> str | None:
    """The indented sentence explaining that a per-share dividend figure has
    been restated by a stock split, or `None` when nothing in the window
    split and there is therefore nothing to explain.

    This line exists because of a real confusion it costs nothing to
    prevent. Yahoo Finance reports every dividend on a ticker's CURRENT
    share basis, so a payment made before a split is divided down by it:
    9984.T declared 22 yen per share for its 2025-09-29 ex-date, split 4:1
    on 2025-12-29, and that payment is therefore stored - correctly - as
    5.5. A reader comparing the report against the company's own
    announcement sees 5.5 where they expected 22 and concludes the report is
    broken. It is not; it was simply silent about the one fact that
    reconciles the two.

    Both figures in the sentence are DERIVED rather than stored: the
    per-share amount is the one the trailing sum actually used, and the
    announced amount is that same row multiplied back by the cumulative
    split ratio (`src/agentic_portfolio/dataset/dividends.py`'s `announced_amount`). The
    payment named is a real row rather than a total divided by a count,
    which matters because a payer that changed its dividend mid-window - the
    payer most likely to have split - would make any such average wrong.

    Printed only when a split really falls inside the window, so the
    overwhelming majority of reports are byte-identical to before.
    Takes `dict[str, SplitContext]`; `dividends_per_share` is accepted but
    unused, kept so the two dividend formatters take the same shape of
    argument at their call sites.
    """
    if not splits_in_window:
        return None

    from agentic_portfolio.dataset.dividends import announced_amount

    parts: list[str] = []
    for ticker in sorted(splits_in_window):
        context = splits_in_window[ticker]
        events = sorted(context.splits)
        if not events:
            continue
        ratios = " and ".join(f"{ratio:g}:1 on {ex_date}" for ex_date, ratio in events)
        sentence = f"{ticker} split {ratios}"

        # Name a payment the reader may well be holding an announcement for:
        # the earliest in the window, which every split since has restated.
        payments = sorted(context.payments)
        series = pd.Series(
            [ratio for _ex, ratio in events],
            index=pd.to_datetime([ex for ex, _r in events]),
        )
        for ex_date, stored in payments:
            announced = announced_amount(stored, ex_date, series)
            if abs(announced - stored) > 1e-9:
                sentence += (
                    f", so its {ex_date} payment of "
                    f"{format_money(announced, currency)} as announced counts as "
                    f"{format_money(stored, currency)} per current share"
                )
                break
        parts.append(sentence)

    if not parts:
        return None
    return (
        "  Per-share amounts are on each ticker's CURRENT share basis. "
        + "; ".join(parts)
        + "."
    )


def format_dividend_floor(
    floor: float | None, origin: str | None, realized: float | None
) -> str:
    """The `Minimum dividend yield:` line, printed on EVERY run.

    Follows two conventions this module already established. The floor's
    provenance rides in parentheses after the figure, exactly as
    `format_risk_free_rate` names a rate's source, because a floor printed
    without saying which flag produced it is the same ambiguity that
    convention exists to prevent - and here the provenance also restates a
    cash request in the unit it was made in, so a reader who typed 3000 need
    not multiply anything to check that 0.0300 is the same instruction. And
    the line is printed whether or not a floor was asked for, saying so
    explicitly when none was, so its presence and position never depend on
    how this particular run was invoked - the same rule the target-return
    line follows.

    Whether the floor BINDS is worth stating and cheap to determine. A
    linear inequality that is strictly slack at the optimum is inactive, so
    removing it would give the same weights: "clears it unaided" is a true
    claim about the portfolio, not a hedge, and it tells a reader that their
    floor changed nothing.
    """
    if floor is None:
        return (
            "Minimum dividend yield: n/a (no floor was asked for; use "
            "--min-annual-dividend or --min-dividend-yield)"
        )
    source = f" ({origin})" if origin else ""
    if realized is None:
        return f"Minimum dividend yield: {floor:.4f}{source}"
    if realized <= floor + DIVIDEND_BINDING_TOLERANCE:
        return (
            f"Minimum dividend yield: {floor:.4f}{source} - binding, the portfolio sits "
            "on the floor"
        )
    return (
        f"Minimum dividend yield: {floor:.4f}{source} - not binding, the portfolio clears "
        f"it by {realized - floor:.4f} unaided"
    )


def print_dividend_section(
    stats: PortfolioStats,
    held: list[str],
    currency: str,
    portfolio_value: float | None = None,
) -> None:
    """The per-ticker `Dividend yield / annual income` block for an
    optimized pool.

    A section of its own rather than two more figures on the existing
    `Expected return / volatility (annualized)` lines, because that block's
    title would then be false and because these are different KINDS of
    number: an expected return is an estimate over a returns window, a
    trailing dividend is a record of cash already paid. Placed immediately
    after that block so the per-ticker sections stay adjacent, in the same
    order, and can be read against each other line for line.

    A confirmed non-payer prints `0.0000` and is labelled as such; a ticker
    whose dividend data is missing prints `n/a` and says so. Those are
    different facts and the report never merges them - merging them is the
    one silently wrong answer this feature could give.

    `portfolio_value` is what `--value` allocated, and it is what turns a
    yield into money. Left `None`, the yields print without a cash column
    rather than against a fabricated basis - the interactive edit loop
    always has the value to hand, so this is a courtesy to a programmatic
    caller rather than a path the CLI takes.
    """
    if stats.dividend_yields is None:
        print(
            "\nDividend yield / annual income (trailing 12 months): n/a - dividend data "
            "was not consulted for this run"
        )
        return

    print("\nDividend yield / annual income (trailing 12 months):")
    for ticker in held:
        if ticker not in stats.dividend_yields:
            # The dividend layer's own sentence when there is one. "No
            # trailing dividend data" is true of every case and actionable
            # in none of them; "yfinance no longer serves this ticker's
            # history for 2015-01-01..2024-04-30" tells the reader whether
            # to rebuild, re-run, or stop trying.
            reason = (stats.dividend_unavailable or {}).get(ticker, "no trailing dividend data")
            print(f"  {ticker}: yield n/a - {reason}")
            continue
        ticker_yield = stats.dividend_yields[ticker]
        note = "  (pays no dividend)" if ticker_yield == 0.0 else ""
        if portfolio_value is None:
            print(f"  {ticker}: yield={ticker_yield:.4f}{note}")
            continue
        # This ticker's share of --value at the CONTINUOUS weight the
        # optimizer solved for, so these lines sum to the portfolio figure
        # printed below them rather than to the whole-share allocation,
        # which is reported separately and deliberately differs.
        cash = stats.weights.get(ticker, 0.0) * float(portfolio_value) * ticker_yield
        print(f"  {ticker}: yield={ticker_yield:.4f}  {format_money(cash, currency)}{note}")


def print_weights_and_allocation(
    stats: PortfolioStats,
    allocation: tuple[dict[str, int], float],
    objective: str,
    currency: str = DEFAULT_CURRENCY,
    benchmark: BenchmarkStats | None = None,
    risk_free_rate_origin: str | None = None,
    portfolio_value: float | None = None,
    resolved_objective=None,
    requested_lookback_months: int | None = None,
) -> None:
    """Human-readable rendering of one `compute_weights_and_allocation`
    result, including the figures the optimizer decided from.

    The per-ticker expected-return/volatility lines cover only the tickers
    that actually received weight, in the same order as the weights above
    them, so the two sections read side by side - `stats` itself carries
    every considered ticker's figures, including the rejected ones. The
    target-return line is printed for every objective, saying so explicitly
    when it does not apply, so its presence and position never depend on
    which objective ran.

    `currency` names the unit every money figure here is in, including the
    `--value` that was allocated. It is printed first, because it qualifies
    everything below it, and it is printed here rather than in
    `print_pipeline_result`'s header because the interactive edit loop calls
    this function directly and would otherwise never show it. The returns
    window line follows immediately for the same reason: it says which
    months of history actually produced the figures below, and must reappear
    after every interactive edit's recompute, not only on the initial run.

    `benchmark`, when given, is printed immediately beneath the portfolio's
    own return/volatility/Sharpe line so the two triplets read side by side -
    that adjacency is the whole point, since a portfolio's figures only mean
    something next to what holding the market over the same months returned.
    `None` prints nothing at all, which is both the default (so every caller
    that never asked for a benchmark is unaffected) and what `--benchmark
    none` produces.

    `portfolio_value` is what `--value` allocated, and it is what lets the
    dividend lines report money as well as a yield. Left `None`, they report
    the yield alone rather than money against a basis nobody supplied.

    The dividend figures sit in two places, for the same reason the existing
    figures do: the per-ticker block goes beside the other per-ticker block,
    and the portfolio-level lines go with the other portfolio-level lines,
    beneath the benchmark and the risk-free rate. Note that the two money
    figures a run prints - the income on the continuous weights here, and
    the income on the whole-share allocation further down - deliberately
    differ, because whole shares plus leftover cash cannot buy the
    continuous portfolio exactly. Both are printed because they answer
    different questions.

    `risk_free_rate_origin` names where the rate on the line below came from
    - this run's flag, the currency's remembered rate, or the configured
    default - see `format_risk_free_rate`. It is carried through the edit
    loop for the same reason the rate itself is: provenance that appeared on
    the initial report and vanished on the first edit would be worse than
    none.

    `requested_lookback_months` is shown only when it differs from the
    60-month default. The report always prints the actual dates and row
    count; naming the request as well makes any shortage of available data
    visible rather than looking like a silently shortened choice.
    """
    print(f"\nPortfolio currency: {currency} - --value is interpreted as {currency}")
    requested = (
        f", {requested_lookback_months} requested"
        if requested_lookback_months is not None
        and requested_lookback_months != DEFAULT_LOOKBACK_MONTHS
        else ""
    )
    print(f"Returns window: {stats.returns_window_start} to {stats.returns_window_end} "
          f"({stats.returns_window_months} month(s) of monthly returns{requested})")

    held = [ticker for ticker, weight in sorted(stats.weights.items(), key=lambda kv: -kv[1]) if weight > 0]

    print("\nWeights:")
    for ticker in held:
        print(f"  {ticker}: {stats.weights[ticker]:.4f}")

    print("\nExpected return / volatility (annualized):")
    for ticker in held:
        print(f"  {ticker}: return={stats.expected_returns[ticker]:.4f}  volatility={stats.volatility[ticker]:.4f}")

    print_dividend_section(stats, held, currency, portfolio_value)

    print(f"\nPortfolio expected return: {stats.portfolio_expected_return:.4f}  "
          f"Portfolio volatility: {stats.portfolio_volatility:.4f}  "
          f"Portfolio Sharpe: {stats.portfolio_sharpe:.4f}")
    if benchmark is not None:
        print(format_benchmark(benchmark, stats.returns_window_months))
    print(format_risk_free_rate(stats.risk_free_rate, risk_free_rate_origin))
    if stats.portfolio_dividend_yield is not None:
        income = (
            f"  Annual dividend income: "
            f"{format_money(stats.portfolio_dividend_yield * portfolio_value, currency)} "
            f"(on --value {format_money(portfolio_value, currency)})"
            if portfolio_value is not None
            else ""
        )
        print(f"Portfolio dividend yield: {stats.portfolio_dividend_yield:.4f}{income}")
        if stats.dividend_yields_missing:
            print(format_dividend_coverage(
                stats.dividend_weight_covered,
                stats.dividend_yields_missing,
                stats.dividend_unavailable,
            ))
        restated = format_split_restatement(
            stats.dividend_splits, stats.dividends_per_share, currency
        )
        if restated is not None:
            print(restated)
    print(format_dividend_floor(
        stats.dividend_yield_floor, stats.dividend_floor_origin, stats.portfolio_dividend_yield
    ))
    if objective == "MV":
        clamped = getattr(resolved_objective, "clamped_from", None)
        # Both numbers, never one silently substituted for the other: a
        # target that was lowered to fit the pool is a different claim from
        # one that was asked for.
        detail = (
            f" (clamped down from {float(clamped):.4f}, which no portfolio of these "
            "candidates can reach)"
            if clamped is not None
            else ""
        )
        print(f"Target annual return: {stats.target_annual_return:.4f}{detail}")
    else:
        print(f"Target annual return: n/a (objective is {objective}, not MV)")

    shares, leftover_cash = allocation
    print("\nShare allocation:")
    for ticker, count in sorted(shares.items()):
        print(f"  {ticker}: {count}")
    print(f"Leftover cash: {format_money(leftover_cash, currency)}")
    allocated = format_allocated_dividends(stats, shares, currency, portfolio_value)
    if allocated is not None:
        print(allocated)


def format_allocated_dividends(
    stats: PortfolioStats,
    shares: dict[str, int],
    currency: str,
    portfolio_value: float | None,
) -> str | None:
    """What the WHOLE-SHARE allocation actually pays over a year, or `None`
    when dividends were not consulted.

    Deliberately a different figure from the `Portfolio dividend yield`
    line above, and printed anyway. That one describes the continuous
    portfolio the optimizer solved for; this one describes the shares a
    person would really buy. They answer two different questions - "what
    does the portfolio I asked for yield" and "what will these share counts
    pay me" - and each belongs beside the block it describes.

    What separates them is whole-share rounding alone: whole shares plus
    leftover cash cannot reproduce continuous weights exactly, so the two
    differ by a fraction of a percent. Measured on a five-name high-dividend
    pool, $6,900.48 against $6,898.91 - 0.02% apart.

    That was not always true. Until the allocation was corrected to price
    shares at the market `close` rather than the back-adjusted `adj_close`,
    the two figures sat about 18% apart on a historical-window database,
    because the share counts were struck against a price nobody could trade
    at. Their agreement is now the cleanest end-to-end signal that the
    allocation is priced correctly, which is worth knowing if it ever
    widens again.

    Computed as `sum(shares * dividends_per_share)`, from the per-share
    cash the data layer stored rather than from a yield, because a share
    count times a per-share dividend IS the money. A ticker in the
    allocation with no per-share figure makes the whole line `n/a` and is
    named: a total that silently omitted one holding would understate the
    income while looking complete.
    """
    if stats.dividends_per_share is None:
        return None

    missing = sorted(t for t in shares if t not in stats.dividends_per_share)
    if missing:
        return (
            "Annual dividends at these share counts: n/a - no trailing "
            f"dividends-per-share for {', '.join(missing)}"
        )

    total = sum(float(count) * stats.dividends_per_share[t] for t, count in shares.items())
    basis = (
        f" (a {total / portfolio_value:.4f} yield on --value "
        f"{format_money(portfolio_value, currency)})"
        if portfolio_value
        else ""
    )
    return f"Annual dividends at these share counts: {format_money(total, currency)}{basis}"


def format_share_count(shares: float) -> str:
    """A share count with a thousands separator and no decimal noise:
    `'1,000'` for a whole holding, `'0.5432'` for a fractional one.

    Brokers sell fractional shares, so the count cannot simply be rendered
    as an integer - but neither should a plain 1000-share holding print as
    `1,000.0000`, which reads like a precision claim nobody made.
    """
    if float(shares).is_integer():
        return f"{int(shares):,}"
    return f"{shares:,.4f}".rstrip("0").rstrip(".")


def format_holding_dividend(holdings: HoldingsStats, ticker: str, currency: str) -> str:
    """The dividend tail on one holding's line: `'  yield 0.0397  $2,530.88
    USD/yr'`, or `''` when dividends were not consulted at all.

    The cash figure is `shares * dividends_per_share`, the money this holder
    will actually receive, and the yield printed beside it is that cash over
    the same `market_values` entry the line's own value came from - so the
    two numbers on one line reconcile against one price rather than against
    a yield struck at some other date.

    A holding with no dividend data prints `yield n/a`, never `0.0000`. That
    distinction is the whole discipline of this feature: a confirmed
    non-payer and an unmeasured holding are different facts.
    """
    figures = holdings.dividends
    if figures is NO_DIVIDEND_FIGURES or not figures.dividends_per_share:
        return ""
    if ticker not in figures.annual_dividends:
        return "  yield n/a"

    cash = figures.annual_dividends[ticker]
    # The dividend layer's own yield, NOT `cash / market_value`. The two
    # differ because market values are struck from `adj_close` while a yield
    # is cash over the raw market close, and printing the quotient would make
    # this line disagree with the identical ticker's line in an optimized
    # pool's report - see `DividendFigures.yields`.
    ticker_yield = figures.yields.get(ticker)
    if ticker_yield is None:
        return f"  yield n/a  {format_money(cash, currency)}/yr"
    return f"  yield {ticker_yield:.4f}  {format_money(cash, currency)}/yr"


def format_holdings_dividend_total(holdings: HoldingsStats, currency: str) -> str | None:
    """The portfolio-level `Trailing annual dividends:` line, or `None` when
    dividends were not consulted.

    The denominator is stated EVERY time, including when it is the whole
    total. That is the same discipline the returns-window and risk-free-rate
    lines follow, and for the same reason: a yield whose base is not printed
    beside it invites being compared against one computed on a different
    base. When a holding has no dividend data the base shrinks and both
    numbers are shown, so the gap between them is visible rather than
    absorbed.
    """
    figures = holdings.dividends
    if figures is NO_DIVIDEND_FIGURES or not figures.dividends_per_share:
        return None

    if figures.total_annual_dividends is None:
        return "Trailing annual dividends: n/a - no trailing dividend data for any holding"

    total_value = holdings.total_value
    covered = figures.value_covered
    detail = ""
    if total_value and covered is not None and abs(covered - total_value) > 0.005:
        gap = sorted(figures.unavailable)
        base = (
            f"on {format_money(covered, currency)} of the "
            f"{format_money(total_value, currency)} total; "
            f"{', '.join(gap)} {'has' if len(gap) == 1 else 'have'} no trailing dividend data"
        )
        # Each holding's reason on its own line BELOW the figure, never
        # inside its parenthetical: for a holding this is the difference
        # between "run --refresh-holdings" and "this income figure will
        # never be complete", and these sentences name dates and ranges
        # that would push the figure line past anything readable in a
        # terminal. The same shape `format_dividend_coverage` uses.
        detail = "".join(f"\n  {t}: {figures.unavailable[t]}" for t in gap)
    else:
        base = f"on the full {format_money(covered or 0.0, currency)} total"
    return (
        f"Trailing annual dividends: {format_money(figures.total_annual_dividends, currency)}  "
        f"Dividend yield: {figures.dividend_yield:.4f} ({base}){detail}"
    )


def format_dividend_delta(baseline: HoldingsStats, hypothetical: HoldingsStats) -> str:
    """How the trailing dividend yield and annual income moved between the
    saved holdings and a hypothetical variant.

    A separate line from `format_holdings_delta` rather than two more
    figures on it, for two reasons. That line is already three figures
    wide. And this one has genuinely DIFFERENT comparability rules:
    `format_holdings_delta` withholds a delta when the two sides were
    measured over different returns windows, because the difference between
    two Sharpe ratios spanning different months is partly a difference of
    months. A trailing dividend is not estimated over a window at all - it
    is a record of twelve months of cash - so `whatif`'s `[w]indow` leaves
    these two figures untouched, and this function reports a real
    `+0.0000` across a window change rather than an apology. That is worth
    printing: it tells the reader the window they just changed cannot
    flatter the income.

    Still withheld when either side has no dividend figures, for
    `format_holdings_delta`'s own reason - subtracting from `None` is not a
    small bug but a misleading number.
    """
    before, after = baseline.dividends, hypothetical.dividends
    if before.dividend_yield is None or after.dividend_yield is None:
        return "Dividend change: n/a - one of the two has no dividend figures to compare."

    currency = hypothetical.currency
    return (
        f"Dividend change: yield {after.dividend_yield - before.dividend_yield:+.4f}  "
        f"annual income "
        f"{format_signed_money(after.total_annual_dividends - before.total_annual_dividends, currency)}"
    )


def _as_argument(shares: float) -> str:
    """A share count as a command-line ARGUMENT: `'4000'`, `'0.5432'` - no
    thousands separators, which `format_share_count` adds for reading and
    which would make a suggested command fail to parse.
    """
    if float(shares).is_integer():
        return str(int(shares))
    return f"{shares:.4f}".rstrip("0").rstrip(".")


def format_stale_share_counts(
    path: str,
    currency: str,
    positions: dict[str, float],
    db_path: str,
) -> str | None:
    """A warning naming every holding whose stored share count predates a
    split, or `None` when there is nothing to warn about.

    Printed ABOVE the report, because it qualifies every figure below it -
    the same position the `Portfolio currency` line occupies, and for the
    same reason. A share count that a split has multiplied leaves the whole
    report internally consistent and uniformly wrong, so a caveat printed
    underneath would be read after the numbers it invalidates.

    Never writes anything. The suggested count is `stored * ratio`, which is
    right only if nothing else changed, and a split is exactly the kind of
    event around which people also buy and sell - so the command to apply it
    is printed for the reader to run deliberately rather than executed on
    their behalf. `memory/portfolio.json` is theirs.

    Reads the timestamp from `path` and the splits from `db_path`, so a
    database with no `splits` table (or a portfolio with no `updated_at`)
    yields `None` and changes no existing output.
    """
    from agentic_portfolio.dataset.dividends import load_splits_long, split_series_by_ticker
    from agentic_portfolio.flow.user_portfolio import portfolio_updated_at, stale_share_counts

    if not positions:
        return None

    updated_at = portfolio_updated_at(path, currency)
    if updated_at is None:
        return None

    splits = split_series_by_ticker(load_splits_long(sorted(positions), db_path))
    stale = stale_share_counts(positions, updated_at, splits)
    if not stale:
        return None

    lines = [""]
    for ticker in sorted(stale):
        entry = stale[ticker]
        lines.append(
            f"WARNING: {ticker} split {entry.ratio:g}:1 on {entry.ex_date}, after this "
            f"portfolio was last updated ({updated_at.date()}). The stored "
            f"{format_share_count(entry.stored_shares)} shares is likely a pre-split count "
            f"and the position is probably {format_share_count(entry.likely_shares)} now, "
            f"which would understate every figure below by {entry.ratio:g}x. To confirm it:"
        )
        # A bare number, NOT `format_share_count`: that adds thousands
        # separators for readability, and `set 9984.T 4,000` is not a command
        # anybody can run. A suggestion that has to be edited before it works
        # is worse than no suggestion.
        lines.append(
            f"  uv run portfolio-holdings set {ticker} {_as_argument(entry.likely_shares)}"
        )
    return "\n".join(lines)


def print_user_portfolio(
    holdings: HoldingsStats,
    path: str,
    risk_free_rate_origin: str | None = None,
    *,
    heading: str | None = None,
    window_origin: str | None = None,
) -> None:
    """Human-readable rendering of the user's OWN saved portfolio (see
    `src/agentic_portfolio/flow/user_portfolio.py` and `src/agentic_portfolio/optimizer/holdings.py`) - what is
    held, what it is worth, and its annualized return, volatility and
    Sharpe ratio.

    The file it came from is named in the header, because these figures
    describe stored state rather than anything the current command line
    said, and a reader who disagrees with them needs to know which file to
    edit.

    `heading` replaces that header outright, and exists for the one caller
    whose figures describe NO stored state: a what-if variant
    (`src/agentic_portfolio/flow/holdings_cli.py`'s `whatif`). Naming a file there would be a
    plain falsehood - the hypothetical holdings are not in it and are never
    going to be - so that caller passes its own header saying so. Keyword-
    only and defaulting to the wording above, so every existing caller and
    test is untouched.

    Every position is listed, including one excluded from the figures, since
    this block doubles as the record of what the user owns; an excluded
    holding shows `weight n/a` and is explained by name underneath. The
    returns window and the risk-free rate are printed whenever figures are,
    never only on request: the same holdings measured over a different
    window or against a different rate are different numbers, and a figure
    whose derivation is not stated beside it invites being compared with one
    derived differently.

    `window_origin` names the window that was ASKED for, when somebody asked
    - `uv run portfolio-holdings whatif`'s `[w]indow`. The month count beside
    it stays derived from the data actually used, so the two together
    distinguish "36 months because I chose 36" from "36 months because that
    is all there was", and make a request that could not be honoured visible
    rather than silent: 48 months of data against a 60-month request prints
    both numbers. Shortening a window until the figures improve is
    cherry-picking, and the defence is that the window is stated on the same
    line as the figures it produced, every time.

    `Total value` carries the date of the prices behind it. Those prices can
    come from a cache that is only refreshed monthly (see
    `src/agentic_portfolio/dataset/holdings_cache.py`), so the total can legitimately be weeks
    old - and the one thing worse than a stale money figure is a stale money
    figure that looks current. The date is derived from the price rows
    actually used, not from when the cache was written, so it cannot claim
    a freshness the data does not have.

    The per-holding `Expected return / volatility (annualized)` section is
    the same section, in the same wording and the same position relative to
    the portfolio-level line, that `print_weights_and_allocation` prints for
    an optimized pool - deliberately, so the two blocks of one report can be
    read against each other line for line. It covers only the holdings
    behind the figures, in the same order as the positions above it, for the
    same reason that function covers only the weighted tickers: a holding
    with no estimate has nothing to print.

    A portfolio with no figures at all prints its reason in the `n/a -
    reason` shape `format_benchmark` already uses, and prints nothing else
    when there are also no positions to list.
    """
    currency = holdings.currency
    if not holdings.positions:
        label = heading or f"Your portfolio ({currency})"
        print(f"\n{label}: n/a - {holdings.unavailable_reason}")
        return

    print(f"\n{heading or f'Your portfolio ({currency}), from {path}'}:")
    ordered = sorted(
        holdings.positions,
        key=lambda t: (-holdings.weights.get(t, -1.0), t),
    )
    for ticker in ordered:
        value = holdings.market_values.get(ticker)
        value_text = format_money(value, currency) if value is not None else "value n/a"
        weight = holdings.weights.get(ticker)
        weight_text = f"weight {weight:.4f}" if weight is not None else "weight n/a"
        print(f"  {ticker}: {format_share_count(holdings.positions[ticker])} shares  "
              f"{value_text}  {weight_text}"
              f"{format_holding_dividend(holdings, ticker, currency)}")

    if holdings.total_value is not None:
        priced = f" (priced {holdings.priced_as_of})" if holdings.priced_as_of else ""
        print(f"Total value: {format_money(holdings.total_value, currency)}{priced}")

    # Printed here, above the figures/n-a branch, because a trailing
    # dividend is window-independent and therefore belongs on BOTH sides of
    # that branch: a portfolio whose returns cannot be measured still pays
    # what it pays, and that is precisely the number its holder wants.
    dividend_total = format_holdings_dividend_total(holdings, currency)
    if dividend_total is not None:
        print(dividend_total)
        restated = format_split_restatement(
            holdings.dividends.splits_in_window, holdings.dividends.dividends_per_share, currency
        )
        if restated is not None:
            print(restated)

    if holdings.unavailable_reason is not None:
        print(f"Figures: n/a - {holdings.unavailable_reason}")
        # The rate is printed here too, not only alongside figures. Someone
        # reading an `n/a` block is the reader most likely to be working out
        # why the numbers moved or vanished, and withholding the rate and its
        # provenance from exactly them would be backwards.
        print(format_risk_free_rate(holdings.risk_free_rate, risk_free_rate_origin))
    else:
        requested = f", {window_origin}" if window_origin else ""
        print(f"Returns window: {holdings.window_start} to {holdings.window_end} "
              f"({holdings.window_months} month(s) of monthly returns{requested})")
        # Same section, same wording and same position relative to the
        # portfolio-level line as `print_weights_and_allocation` gives an
        # optimized pool - so the two blocks of one report can be read
        # against each other line for line, which is the whole reason for
        # printing them together.
        print("Expected return / volatility (annualized):")
        for ticker in ordered:
            if ticker in holdings.expected_returns:
                print(f"  {ticker}: return={holdings.expected_returns[ticker]:.4f}  "
                      f"volatility={holdings.volatility[ticker]:.4f}")
        print(f"Annual return: {holdings.annual_return:.4f}  "
              f"Annual volatility: {holdings.annual_volatility:.4f}  "
              f"Sharpe: {holdings.sharpe:.4f}")
        print(format_risk_free_rate(holdings.risk_free_rate, risk_free_rate_origin))

    if holdings.excluded:
        print("Excluded from the figures:")
        for ticker in sorted(holdings.excluded):
            share = ""
            value = holdings.market_values.get(ticker)
            if value is not None and holdings.total_value:
                share = f" ({value / holdings.total_value:.1%} of total value)"
            print(f"  {ticker}: {holdings.excluded[ticker]}{share}")


def format_holdings_delta(baseline: HoldingsStats, hypothetical: HoldingsStats) -> str:
    """The one line a what-if exists to print: how the three portfolio-level
    figures moved between the saved holdings and a hypothetical variant.

    Signed to four decimals, matching every other figure in the report, so
    `+0.1056` reads as "a tenth better" against the `0.1225` two lines above
    it rather than needing conversion.

    Returns a REASON instead of a delta when the two are not comparable,
    which is the whole reason this is a function rather than three
    subtractions at the call site. Two ways that happens. Either side may
    have no figures at all - a variant whose every holding was excluded, for
    instance - and subtracting from `None` is not a small bug but a
    misleading number. And the two may have been measured over different
    returns windows, in which case the difference between their Sharpe
    ratios is partly just the difference between two spans of months.
    `open_holdings_session` exists to make that second case impossible for
    a change of HOLDINGS, since every variant is measured against one
    database. It is reachable, legitimately, when somebody changes the
    window itself - which is why `whatif`'s `[w]indow` re-measures the
    baseline at the new length rather than leaving the two sides on
    different spans. Either way, saying so beats printing a plausible number
    that is not the answer to any question.

    Only the three portfolio-level figures are diffed. A holding's OWN
    volatility legitimately moves between the two blocks even when nothing
    about that holding changed, because `CovarianceShrinkage.ledoit_wolf`
    shrinks across whatever cross-section it sits in - so a per-holding
    delta would report as change something that is an artefact of the
    portfolio around it. See `plans/13_user_portfolio.md`'s Revision Notes.
    """
    if baseline.unavailable_reason is not None or hypothetical.unavailable_reason is not None:
        return "Change from your saved portfolio: n/a - one of the two has no figures to compare."

    if (baseline.window_start, baseline.window_end) != (
        hypothetical.window_start,
        hypothetical.window_end,
    ):
        return (
            "Change from your saved portfolio: n/a - measured over different windows "
            f"({baseline.window_start} to {baseline.window_end} against "
            f"{hypothetical.window_start} to {hypothetical.window_end}), so the "
            "difference would partly be the windows rather than the holdings."
        )

    return (
        "Change from your saved portfolio: "
        f"return {hypothetical.annual_return - baseline.annual_return:+.4f}  "
        f"volatility {hypothetical.annual_volatility - baseline.annual_volatility:+.4f}  "
        f"Sharpe {hypothetical.sharpe - baseline.sharpe:+.4f}"
    )


def report_facts(
    variant: str,
    currency: str,
    stats: PortfolioStats,
    objective: str,
    resolved_objective=None,
    selection: str | None = None,
    candidates: list[str] | None = None,
    portfolio_value: float | None = None,
    benchmark: BenchmarkStats | None = None,
) -> dict[str, object]:
    """One optimized portfolio's report reduced to the flat facts stored above
    the report text (see `src/agentic_portfolio/flow/report_archive.py`).

    It lives here, beside the printers, for the reason
    `print_pipeline_result`'s own docstring gives for taking
    `risk_free_rate_origin` as a parameter: this is the display layer, and it
    is the only layer that already knows every one of `PortfolioStats`,
    `BenchmarkStats` and `ResolvedObjective`. The archive module deliberately
    knows none of them - it stores scalars, so it never has to be revised when
    a record gains a field.

    Every figure the eventual side-by-side comparison would otherwise have to
    read out of English prose is included as a value: the three headline
    figures, the dividend yield, the returns window, and the provenance
    phrases for the objective and any dividend floor. `None` facts are dropped
    by the archive rather than written, so an absent benchmark or an
    unconsulted dividend layer simply has no line - which is why nothing here
    substitutes a zero for a figure that was never measured.

    `annual_dividend` is derived rather than carried, because
    `PortfolioStats` holds the yield and only the command line knows the value
    it applies to. It is omitted when either half is missing, since a cash
    figure resting on an assumed value would be worse than no cash figure.

    `candidates` is SORTED here rather than kept in the order the report
    printed it. A pool is a set - the same tickers screened by a different
    branch, or typed in a different order, are the same pool - and the stored
    fact exists to be compared across runs, which an order-dependent string
    could not be. The printed list is untouched; it keeps the scanner's own
    order because that is what the reader was shown.
    """
    dividend_yield = stats.portfolio_dividend_yield
    annual_dividend = (
        dividend_yield * portfolio_value
        if dividend_yield is not None and portfolio_value is not None
        else None
    )
    # The target is meaningful only for MV, as everywhere else in this
    # project, so it is recorded only there rather than stored as a number a
    # reader would have to know to ignore.
    target_return = stats.target_annual_return if objective == "MV" else None

    return {
        "variant": variant,
        "currency": currency,
        "objective": objective,
        "objective_origin": resolved_objective.origin if resolved_objective is not None else None,
        "target_return": target_return,
        "clamped_from": (
            resolved_objective.clamped_from if resolved_objective is not None else None
        ),
        "selection": selection,
        "value": portfolio_value,
        "candidates": sorted(candidates) if candidates else None,
        "benchmark": benchmark.ticker if benchmark is not None else None,
        "benchmark_return": benchmark.annual_return if benchmark is not None else None,
        "risk_free_rate": stats.risk_free_rate,
        "dividend_floor_yield": stats.dividend_yield_floor,
        "dividend_floor_origin": stats.dividend_floor_origin,
        "window_start": stats.returns_window_start,
        "window_end": stats.returns_window_end,
        "window_months": stats.returns_window_months,
        "annual_return": stats.portfolio_expected_return,
        "annual_volatility": stats.portfolio_volatility,
        "sharpe": stats.portfolio_sharpe,
        "annual_dividend": annual_dividend,
        "dividend_yield": dividend_yield,
    }


def holdings_report_facts(variant: str, holdings: HoldingsStats) -> dict[str, object]:
    """One held-portfolio report reduced to the flat facts stored above the
    report text - the `whatif` counterpart of `report_facts`.

    `positions` is rendered as sorted `TICKER:shares` pairs so two variants of
    the same portfolio produce the same string regardless of the order the
    shares were typed in, which matters because that string is what a reader
    scans to tell one saved variant from another. Share counts go through
    `_as_argument` rather than `format_share_count`: the latter adds thousands
    separators for reading, and a comma inside a field whose own separator is a
    comma would make `SPY:1,000` unparseable by the command meant to read it.

    `HoldingsStats`' three figures are all-or-nothing - either all six of the
    return, volatility, Sharpe and window fields are populated or all six are
    `None` - so nothing here checks them individually: an unmeasurable
    portfolio simply contributes no such lines, and the archive omits them.
    That is the honest outcome. A portfolio whose value can be priced but whose
    history is too short to measure still records what it is worth.
    """
    dividends = holdings.dividends
    return {
        "variant": variant,
        "currency": holdings.currency,
        "positions": ", ".join(
            f"{ticker}:{_as_argument(shares)}"
            for ticker, shares in sorted(holdings.positions.items())
        )
        or None,
        "total_value": holdings.total_value,
        "priced_as_of": holdings.priced_as_of,
        "risk_free_rate": holdings.risk_free_rate,
        "window_start": holdings.window_start,
        "window_end": holdings.window_end,
        "window_months": holdings.window_months,
        "annual_return": holdings.annual_return,
        "annual_volatility": holdings.annual_volatility,
        "sharpe": holdings.sharpe,
        "annual_dividend": dividends.total_annual_dividends,
        "dividend_yield": dividends.dividend_yield,
    }


def print_pipeline_result(
    result: dict,
    risk_free_rate_origin: str | None = None,
    portfolio_value: float | None = None,
    *,
    archive: ReportArchive | None = None,
) -> None:
    """Human-readable rendering of one `run_pipeline_against` result dict.

    `risk_free_rate_origin` is a parameter rather than a key inside `result`
    on purpose: it is a display string, and `run_pipeline_against` lives in
    the orchestration layer, which should not acquire resolution's vocabulary
    just so a parenthetical can ride along. `portfolio_value` is a parameter
    for a plainer reason: it is what `--value` allocated, the orchestration
    layer never puts it in the result dict, and it is what turns a dividend
    yield into money.

    A result carrying `"unsatisfiable"` prints its reason where the weights
    and allocation would have gone, and nothing else changes: the mode,
    date, objective, LLM rule, scanner branch and candidate list are all
    real and all still shown. That is strictly more useful than a bare
    failure, since the reason is a statement about numbers on the command
    line and the candidate list is what makes it actionable.

    `archive`, when given, saves the weights-and-allocation block - not this
    function's whole output - to a file under its output directory, and names
    the file afterwards. See `src/agentic_portfolio/flow/report_archive.py` for why the archived
    body stops where it does: that block is the only section this function and
    the interactive edit loop both print, so anchoring the digest on it is what
    lets an edit that returns to this portfolio be recognized as the same
    report rather than saved a second time. The header lines above it are not
    lost - they are passed as front-matter facts, where the eventual
    side-by-side comparison wants them as values rather than as a sentence.

    A `None` archive - the default, and so every existing caller and test -
    prints byte-identically to what this function printed before archiving
    existed. The unsatisfiable path returns before the archived block is
    reached, so such a run stores nothing without needing a check for it:
    there is no portfolio, and an archive of non-answers would only make the
    month folder harder to read.
    """
    resolved = result.get("resolved_objective")
    origin = f" ({resolved.origin})" if resolved is not None else ""
    print(f"Mode: {result['mode']}  Rebalance date: {result['rebalance_date']}  "
          f"Objective: {result['objective']}{origin}  Selection: {result['selection']}")

    rule = result["rule"]
    if rule is not None:
        print("\nLLM-S rule:")
        print(f"  buy_condition:  {rule.buy_condition}")
        print(f"  sell_condition: {rule.sell_condition}")
        print(f"  rationale: {rule.rationale}")

    scan_detail = result["scan_detail"]
    print(f"\nScanner branch: {scan_detail['branch']}  "
          f"(buy_s={scan_detail['buy_s_size']} buy_f={scan_detail['buy_f_size']} "
          f"intersection={scan_detail['intersection_size']} union={scan_detail['union_size']})")
    print(f"Candidates ({len(scan_detail['candidates'])}): {', '.join(scan_detail['candidates'])}")

    unsatisfiable = result.get("unsatisfiable")
    if unsatisfiable is not None:
        # In place of the weights and allocation block, which do not exist.
        # Everything above still printed, because the screening really did
        # run and its output is what the person paid for - and because
        # seeing the candidate list beside the reason is what makes the
        # remedy obvious.
        print(f"\nCannot optimize this run: {unsatisfiable}")
        print("The fetched data is still open, so you can fix this without starting over.")
        return

    with record_report(
        archive,
        **report_facts(
            variant="initial",
            currency=result["currency"],
            stats=result["stats"],
            objective=result["objective"],
            resolved_objective=resolved,
            selection=result.get("selection"),
            candidates=result["scan_detail"]["candidates"],
            portfolio_value=portfolio_value,
            benchmark=result.get("benchmark"),
        ),
    ):
        print_weights_and_allocation(
            result["stats"], result["allocation"], result["objective"], result["currency"],
            benchmark=result.get("benchmark"), risk_free_rate_origin=risk_free_rate_origin,
            portfolio_value=portfolio_value, resolved_objective=resolved,
        )
        note = result.get("concentration_note")
        if note is not None:
            print(note)


def _print_add_outcome(
    valid_added: list[str],
    invalid: dict[str, str],
    refused: dict[str, str] | None = None,
    pool_currency: str | None = None,
) -> None:
    """Report which of the tickers just typed resolved, which did not, and
    which were valid but could not join this pool - the whole point of
    validating an add is that the good tickers in a batch still land while
    the bad ones are named.

    A `refused` ticker is a different thing from an `invalid` one and gets a
    different sentence: it exists and is priceable, it simply trades in
    another currency, and a portfolio whose prices carry two units cannot be
    allocated correctly.
    """
    if valid_added:
        print(f"Added: {', '.join(sorted(valid_added))}.")
    if invalid:
        print(f"Ignored (not found): {', '.join(sorted(invalid))}.")
    for ticker in sorted(refused or {}):
        print(
            f"Refused: {ticker} is priced in {refused[ticker]} but this pool is {pool_currency}. "
            "A portfolio cannot mix currencies; run them separately."
        )


NO_FUND_FIGURES_LABEL = "Yahoo fund figures"
"""Label on the line that stands in for the fund block when there is none.

It gets a label and a reason rather than being omitted because an absent
line is indistinguishable from a line that was never going to be there: a
person who typed a company share should learn that Yahoo publishes no
category comparison for one, not silently see a shorter block and wonder
whether the fetch failed.
"""


def format_ratio_pair(label: str, value: float | None, category: float | None) -> str | None:
    """One `label: 0.0101 (category average 0.0157)` line, or `None` when
    there is no figure to print at all.

    The category average is what makes the fund's own number mean anything -
    a 1% expense ratio is cheap in one category and dear in another - so it
    is printed on the same line rather than left to be looked up. When Yahoo
    supplies the fund's figure but no category average, the parenthesis is
    dropped rather than filled with a placeholder.
    """
    if value is None:
        return None
    if category is None:
        return f"{label}: {value:.4f}"
    return f"{label}: {value:.4f} (category average {category:.4f})"


def format_trailing_return_rows(
    returns: dict[str, float], category: dict[str, float] | None, per_row: int = 4
) -> list[str]:
    """Yahoo's trailing total returns as `fund / category` pairs, wrapped
    into rows of at most `per_row` periods.

    Wrapped rather than printed on one line because seven periods with two
    figures each does not fit a terminal, and a report line that wraps
    wherever the window happens to end is unreadable. Each period prints as
    `1Y 0.2355 / 0.3076`; a period the category has no figure for prints the
    fund's alone rather than inventing a comparison.
    """
    category = category or {}
    cells = []
    for label, value in returns.items():
        peer = category.get(label)
        cells.append(
            f"{label} {value:.4f} / {peer:.4f}" if peer is not None else f"{label} {value:.4f}"
        )
    return [
        "    " + "   ".join(cells[i : i + per_row]) for i in range(0, len(cells), per_row)
    ]


def format_risk_statistics(statistics: dict[str, float]) -> str | None:
    """Yahoo's three-year risk statistics as one line, or `None` when there
    are none.

    Every value here is already a fraction: `src/agentic_portfolio/dataset/ticker_profile.py`
    divides Yahoo's percentage-valued `alpha` and `stdDev` by 100 on the way
    in, because these numbers are printed a few lines above this project's
    own `Annual volatility`, and a `13.70` beside a `0.1893` invites a
    hundredfold misreading.
    """
    if not statistics:
        return None
    names = {"alpha": "alpha", "beta": "beta", "stdDev": "stdDev", "sharpeRatio": "Sharpe"}
    parts = [f"{names[k]}={v:.4f}" for k, v in statistics.items() if k in names]
    if not parts:
        return None
    return f"  Yahoo risk statistics (3y): {'  '.join(parts)}"


def format_ticker_dividend_yield(stats: TickerStats) -> str:
    """The summary's dividend line: a trailing yield with the window it was
    summed over, or a named `n/a`.

    Never a bare zero. A yield of exactly `0.0` here means a CONFIRMED
    non-payer - this project proved it had the dividend history and found no
    payments - while a ticker whose history is missing prints its reason
    instead. `AVB`, `EA`, `EQR` and `LEG` are the second case in this
    project's own data, and collapsing the two would be the one silent wrong
    answer this feature could produce.
    """
    if stats.dividend_yield is None:
        reason = stats.dividend_unavailable_reason or "no trailing dividend data"
        return f"    Trailing dividend yield: n/a - {reason}"
    return (
        f"    Trailing {stats.dividend_lookback_months}-month dividend yield: "
        f"{stats.dividend_yield:.4f}"
    )


def _print_ticker_identity(profile: TickerProfile) -> None:
    """The header line and whichever identity block fits the security: a
    fund's category and fees, or a company share's sector and multiples.

    A fund gets the fund block whenever Yahoo served the fund modules; every
    security gets the equity block for the fields `info` carried, because
    `sector`, `marketCap`, `trailingPE` and `beta` are the plain multiples
    the unit rule permits and an ETF that happens to report them is not a
    reason to hide them.
    """
    name = profile.long_name or "(name unavailable)"
    kind = ", ".join(p for p in (profile.quote_type, profile.currency) if p)
    header = f"\nTicker summary: {profile.ticker} - {name}"
    print(f"{header}  ({kind})" if kind else header)

    if profile.category_name or profile.family or profile.legal_type:
        parts = [
            f"Category: {profile.category_name}" if profile.category_name else None,
            f"Family: {profile.family}" if profile.family else None,
            f"Legal type: {profile.legal_type}" if profile.legal_type else None,
        ]
        print("  " + "   ".join(p for p in parts if p))

    fees = [
        format_ratio_pair("Expense ratio", profile.expense_ratio, profile.expense_ratio_category),
        f"Holdings turnover: {profile.holdings_turnover:.4f}"
        if profile.holdings_turnover is not None
        else None,
    ]
    if any(fees):
        print("  " + "   ".join(p for p in fees if p))

    if profile.sector or profile.industry:
        parts = [
            f"Sector: {profile.sector}" if profile.sector else None,
            f"Industry: {profile.industry}" if profile.industry else None,
        ]
        print("  " + "   ".join(p for p in parts if p))

    currency = profile.currency or DEFAULT_CURRENCY
    size = [
        f"Net assets: {format_money(profile.net_assets, currency)}"
        if profile.net_assets is not None
        else None,
        f"Market cap: {format_money(profile.market_cap, currency)}"
        if profile.market_cap is not None
        else None,
        f"52-week range: {profile.fifty_two_week_low:.2f} - {profile.fifty_two_week_high:.2f}"
        if profile.fifty_two_week_low is not None and profile.fifty_two_week_high is not None
        else None,
    ]
    if any(size):
        print("  " + "   ".join(p for p in size if p))

    multiples = [
        f"Trailing P/E: {profile.trailing_pe:.4f}" if profile.trailing_pe is not None else None,
        f"Forward P/E: {profile.forward_pe:.4f}" if profile.forward_pe is not None else None,
        f"Beta: {profile.beta:.4f}" if profile.beta is not None else None,
    ]
    if any(multiples):
        print("  " + "   ".join(p for p in multiples if p))


def print_ticker_summary(
    profile: TickerProfile, stats: TickerStats, risk_free_rate_origin: str | None = None
) -> None:
    """The whole per-ticker summary block: what Yahoo Finance publishes about
    the security, then the figures this project computed itself.

    Printed automatically for each ticker added to a `user_provided`
    candidate pool, and on demand through that loop's `[s]ummary` choice.
    The two halves come from independent sources - `fetch_ticker_profile`
    over the network, `ticker_stats` out of the session database - and they
    FAIL INDEPENDENTLY here: either can collapse to a single `n/a` line
    carrying its reason while the other prints in full. That is deliberate
    and load-bearing. A ticker Yahoo has nothing to say about still has a
    return and a volatility worth seeing before it joins a portfolio, and a
    ticker too newly listed for this project to measure still has a category
    and an expense ratio worth knowing about.

    Every ratio prints as a four-decimal fraction, every money amount
    through `format_money`, and the risk-free rate through
    `format_risk_free_rate` with its provenance - the same conventions
    `print_weights_and_allocation` follows, so the two blocks can be read
    against each other without translating between units.

    The heading over this project's own figures names the real first and last
    month behind them rather than the 60-month window that was requested,
    because real history is frequently shorter than the target and the same
    command on two different days uses different months.
    """
    if profile.unavailable_reason is not None:
        print(f"\nTicker summary: {profile.ticker} - n/a - {profile.unavailable_reason}")
    else:
        _print_ticker_identity(profile)

        if profile.trailing_returns:
            as_of = profile.trailing_returns_as_of
            stamp = f" as of {as_of}" if as_of else ""
            print(f"\n  Yahoo trailing total returns{stamp}, fund / category:")
            for row in format_trailing_return_rows(
                profile.trailing_returns, profile.trailing_returns_category
            ):
                print(row)

        risk_line = format_risk_statistics(profile.risk_statistics or {})
        if risk_line is not None:
            print(risk_line)

        if profile.fund_data_reason is not None:
            print(f"  {NO_FUND_FIGURES_LABEL}: n/a - {profile.fund_data_reason}")

    if stats.unavailable_reason is not None:
        print(f"\n  This project's own figures: n/a - {stats.unavailable_reason}")
        return

    print(
        f"\n  This project's own figures, {stats.window_start} to {stats.window_end} "
        f"({stats.window_months} month(s) of monthly returns):"
    )
    print(
        f"    Annual return: {stats.annual_return:.4f}   "
        f"Annual volatility: {stats.annual_volatility:.4f}   "
        f"Sharpe: {stats.sharpe:.4f}"
    )
    print(f"    {format_risk_free_rate(stats.risk_free_rate, risk_free_rate_origin)}")
    print(format_ticker_dividend_yield(stats))


def _resolve_mixed_persisted_pool(
    pool: list[str], currencies: dict[str, str], override: str | None = None
) -> tuple[list[str], str]:
    """Report a saved pool that spans several currencies and ask which one to
    keep, returning `(kept_tickers, currency)`.

    A pool saved before currencies were recorded can legitimately be mixed,
    and so can one whose ticker changed listing. The machine genuinely
    cannot decide which currency was intended - keeping the largest group
    would just be guessing quietly - so this asks unless `override` has
    already said, which is safe because this loop is already interactive and
    the pool is the person's own. Nothing reaches disk until they confirm at
    the `[d]one` prompt.

    `override` (`main`'s `--currency`) answers the question without asking
    it. This is the SECOND currency prompt on the resume path, and a flag
    that silenced only `_choose_pool_to_resume`'s would still hang a
    scripted run here - so `--currency` has to reach this one too, or it
    would not really mean "no prompts".

    An override naming a currency the pool has no tickers in keeps nothing
    rather than falling back to the prompt, for the same reason: a script
    cannot answer a prompt, and "I asked for a JPY pool" is a coherent
    instruction even when the mixed file turns out to hold no JPY tickers.
    Either way the warning and the keeping/dropping line still print - they
    are the only record that tickers were discarded, and an override is a
    reason to skip the question, not a reason to hide the answer.
    """
    groups = group_by_currency(pool, currencies)
    print("\nWarning: the saved candidate pool mixes currencies, which one portfolio cannot do:")
    for currency, tickers in groups.items():
        print(f"  {currency}: {', '.join(tickers)}")

    if override:
        if override in groups:
            dropped = sorted(t for c, ts in groups.items() if c != override for t in ts)
            print(f"Keeping {override}; dropping {', '.join(dropped)} (--currency).")
            return groups[override], override
        print(
            f"No {override} tickers in the saved pool; dropping all of it and starting "
            f"an empty {override} pool (--currency)."
        )
        return [], override

    while True:
        chosen = input(f"Keep which currency? ({'/'.join(groups)}): ").strip().upper()
        if chosen in groups:
            dropped = sorted(t for c, ts in groups.items() if c != chosen for t in ts)
            print(f"Keeping {chosen}; dropping {', '.join(dropped)}.")
            return groups[chosen], chosen
        print(f"Unrecognized currency {chosen!r}.")


def _choose_pool_to_resume(
    pools: dict[str, list[str]],
    benchmarks: dict[str, str | None] | None = None,
    override: str | None = None,
) -> tuple[list[str], str | None]:
    """Pick which saved pool this session works on, returning
    `(tickers, currency)`.

    `memory/candidates.json` holds one pool per currency, since a portfolio
    can only hold one, so with anything saved there is nothing but the
    person's intent to go on - which `override` supplies when given, and
    which this asks for otherwise. A `None` currency means "start a
    new pool": the same not-yet-established state as a first-ever run, where
    the first ticker typed decides its currency. Choosing a currency that
    already has a pool and emptying it is simply editing that pool, which
    overwrites it on save - `save_candidate_pool` has always replaced rather
    than merged.

    The `[n]ew` option is offered even when only one pool is saved, which
    costs a keystroke in the common case but is what makes a second currency
    reachable at all: resuming the sole saved pool unconditionally would
    leave a person with a USD pool no way to start a JPY one, since every
    Tokyo ticker they typed would be refused against the pool they were
    forced into.

    `benchmarks` names each pool's recorded benchmark so the listing shows it
    beside the tickers - which pool a person means to resume is often decided
    by what it is measured against, and a pool with none recorded is about to
    be asked about, so seeing that in advance is useful rather than noise.

    `override` (`main`'s `--currency`) answers the question without asking
    it, which is what makes this command scriptable: the prompt is the right
    default for a person deciding at the keyboard, but it cannot be answered
    from a shell history line or a cron entry. A saved pool in that currency
    is resumed exactly as typing its code would resume it; no saved pool in
    that currency starts a new one, the non-interactive equivalent of
    `[n]ew`.

    Note what that second case does differently from typing `[n]ew`: it
    returns the currency rather than `None`, settling it up front instead of
    leaving the first typed ticker to establish it. That is deliberate. A
    scripted `--currency JPY` that then adds a dollar ticker by mistake
    should be refused by name on the first add, not silently become a dollar
    pool - which is exactly what `None` would let happen.

    The listing still prints either way, so a mistyped `--currency JYP` shows
    the saved `JPY` pool directly above the line saying no `JYP` pool was
    found. Surprising, but never silent.
    """
    if override:
        override = override.strip().upper()

    if not pools:
        return [], override

    benchmarks = benchmarks or {}
    print("\nSaved candidate pools:")
    for currency, tickers in sorted(pools.items()):
        benchmark = benchmarks.get(currency)
        count = f"{len(tickers)}, benchmark {benchmark}" if benchmark else str(len(tickers))
        print(f"  {currency} ({count}): {', '.join(tickers)}")

    if override:
        if override in pools:
            print(f"Resuming the {override} pool (--currency).")
            return pools[override], override
        print(f"No saved {override} pool; starting a new one (--currency).")
        return [], override

    if len(pools) == 1:
        only_currency, only_tickers = next(iter(pools.items()))
        prompt = f"Resume the {only_currency} pool? [Enter] resume / [n]ew pool in another currency: "
    else:
        only_currency, only_tickers = None, None
        prompt = f"Resume which pool? ({'/'.join(sorted(pools))}) or [n]ew: "

    while True:
        chosen = input(prompt).strip().upper()
        if chosen in ("N", "NEW"):
            return [], None
        if not chosen and only_currency is not None:
            return only_tickers, only_currency
        if chosen in pools:
            return pools[chosen], chosen
        print(f"Unrecognized choice {chosen!r}.")


def _run_user_provided_confirm_loop(
    initial_pools: dict[str, list[str]],
    rebalance_date: date,
    db_path: str,
    memory_path: str = DEFAULT_CANDIDATES_PATH,
    currency: str | None = None,
    rates_path: str = DEFAULT_RATES_PATH,
    risk_free_rate: float | None = None,
    show_summaries: bool = False,
    profile_cache: dict[str, TickerProfile] | None = None,
) -> tuple[list[str], str]:
    """Show the persisted candidate pool, then prompt in a loop for
    add/remove until the user confirms they are done, and return the
    confirmed pool together with the currency it is priced in - the
    `user_provided` selection's replacement for the agents that choose
    candidates in every other selection.

    `initial_pools` is every pool saved at `memory_path`, keyed by currency;
    `_choose_pool_to_resume` settles which one this session works on, from
    the prompt or from `currency`. The
    chosen pool is persisted exactly once, when the user confirms, and only
    into its own currency's slot - an in-progress edit is never written, and
    the other currencies' pools are left exactly as they were. It is
    re-validated (and re-ingested into this session's `db_path`, which is a
    fresh scratch database every run) before anything is shown, so a
    previously-saved ticker that no longer resolves is dropped with a
    warning rather than breaking the optimizer later.

    Every ticker in the pool must trade in one currency, since prices in two
    different units cannot be allocated against a single budget. The first
    ticker added to an empty pool establishes that currency and later adds
    are measured against it - unless `currency` settled it in advance.

    `currency` is `main`'s `--currency`, and it reaches BOTH currency
    questions on this path (`_choose_pool_to_resume`'s "which pool" and
    `_resolve_mixed_persisted_pool`'s "keep which"), because a flag that
    silenced only the first would still leave a scripted run waiting on the
    second. It also seeds `pool_currency` below, so a pool that starts empty
    starts with its currency already known rather than waiting for the first
    typed ticker to decide.

    One thing `currency` does NOT override: the currency a resumed pool's
    tickers actually price in, per the database. A pool saved under `USD`
    whose ticker has since moved to Tokyo really is JPY now, and the
    database is a better authority on that than a stale key in a JSON file.
    The disagreement is reported rather than silently applied.

    `[s]ummary` prints one ticker's performance summary - what Yahoo Finance
    publishes about it beside the figures this project computes itself - and
    changes nothing: not the pool, not its currency, not anything on disk.
    That is what makes it usable for studying a candidate BEFORE deciding to
    add it, and it is also why a ticker in another currency can be
    summarized freely when an add would refuse it. Looking is always
    allowed; only joining a pool is gated on currency.

    Each ticker a successful add actually admits is summarized automatically
    too, in the order it was typed, because the moment a ticker joins the
    pool is exactly the moment the missing information matters and a person
    who does not know to go looking will not go looking. `show_summaries`
    (`main`'s `--no-ticker-summary`) switches off that automatic half only;
    `[s]ummary` still works, because asking for something is not the same as
    having it volunteered.

    `show_summaries` defaults to `False` even though the behavior it enables
    is ON for every real run, which `main` expresses by passing `True`. That
    inversion is deliberate: the automatic summary reaches Yahoo Finance, and
    a default of `True` would silently give network access to every existing
    caller and test of this loop that only ever meant to exercise the
    add/remove sequencing. This is the reasoning `format_risk_free_rate`'s
    optional `origin` already follows - a defaulted parameter whose default
    exists for the benefit of callers that predate it.

    `rates_path` and `risk_free_rate` are needed only by the summary, and
    only because this loop runs BEFORE `_settle_risk_free_rate`: the rate is
    per-currency and the currency is not settled until this loop returns, so
    a Sharpe ratio printed here has to resolve its own rate and name where it
    came from. `profile_cache` is shared with `_run_edit_loop` so a ticker
    summarized in one loop is not re-fetched in the other.
    """
    initial_pool, resumed_currency = _choose_pool_to_resume(
        initial_pools, load_pool_benchmarks(memory_path), override=currency
    )
    # `_choose_pool_to_resume` normalizes; take its answer so the rest of
    # this function compares against one spelling.
    currency = resumed_currency if currency else None

    pool, invalid, currencies = validate_and_ingest_tickers(initial_pool, rebalance_date, db_path)
    if invalid:
        print(f"Warning: dropping previously-saved ticker(s) that no longer resolve: {', '.join(sorted(invalid))}.")

    pool_currency: str | None = resumed_currency
    if pool:
        groups = group_by_currency(pool, currencies)
        if len(groups) > 1:
            pool, pool_currency = _resolve_mixed_persisted_pool(pool, currencies, override=currency)
        else:
            pool_currency = next(iter(groups))
            if currency and pool_currency != currency:
                print(
                    f"Note: the saved {currency} pool's tickers now price in {pool_currency}; "
                    f"continuing as {pool_currency}."
                )

    print(f"\nCurrent candidate pool ({len(pool)}): {', '.join(pool) if pool else '(empty)'}")

    def show_summary(symbol: str, symbol_currency: str | None) -> None:
        """Print one ticker's summary, resolving its own risk-free rate.

        Closed over the loop's `db_path`, `pool` and caches so both the
        `[s]ummary` choice and the automatic post-add printing go through one
        code path - a summary that differed depending on which of the two
        asked for it would be a bug waiting to happen.
        """
        summary = prepare_ticker_summary(
            symbol,
            rebalance_date,
            pool,
            db_path,
            currency=symbol_currency,
            rates_path=rates_path,
            risk_free_rate_override=risk_free_rate,
            profile_cache=profile_cache,
        )
        print_ticker_summary(summary.profile, summary.stats, summary.risk_free_rate_origin)

    while True:
        choice = input(
            "\nEdit candidate pool? [a]dd tickers / [r]emove tickers / [s]ummary / [d]one: "
        ).strip().lower()

        if choice in ("s", "summary"):
            symbol = input("Ticker to summarize: ").strip().upper()
            if not symbol:
                print("No ticker given; nothing to summarize.")
                continue
            # Deliberately not `continue`d through the edit machinery below:
            # this choice changes nothing, so it must not print a pool line,
            # must not touch `pool_currency`, and must not be able to trigger
            # the empty-pool revert.
            show_summary(symbol, pool_currency if symbol in pool else None)
            continue

        if choice in ("", "d", "done"):
            if not pool:
                print("Candidate pool is empty; add at least one ticker before finishing.")
                continue
            confirmed_currency = pool_currency or DEFAULT_CURRENCY
            save_candidate_pool(pool, path=memory_path, currency=confirmed_currency)
            print(f"Saved {len(pool)} {confirmed_currency} ticker(s) to {memory_path}.")
            return pool, confirmed_currency

        previous_pool, previous_pool_currency = pool, pool_currency
        if choice in ("a", "add"):
            raw = input("Ticker(s) to add (space-separated): ").strip().upper()
            edit = validate_and_edit_candidates(
                pool, add=raw.split(), remove=[], as_of=rebalance_date, db_path=db_path,
                pool_currency=pool_currency,
            )
            # `validate_and_edit_candidates` reports `pool_currency=None`
            # whenever the resulting pool is empty, which loses the currency
            # this add was actually measured against - so fall back to the
            # one that was in force. Without that, an add whose every ticker
            # was refused would report "this pool is None" and, worse, would
            # forget the currency, letting the next add establish a different
            # one. Reachable only once a currency can be settled before the
            # pool is non-empty, i.e. via `--currency`.
            pool = edit.pool
            pool_currency = edit.pool_currency or previous_pool_currency
            _print_add_outcome(edit.added, edit.invalid, edit.refused, pool_currency)
            if show_summaries:
                # In typed order, which `validate_and_edit_candidates` already
                # preserves in `added`, so a person reading down the screen
                # sees the blocks in the order they asked for them.
                for symbol in edit.added:
                    show_summary(symbol, pool_currency)
        elif choice in ("r", "remove"):
            raw = input("Ticker(s) to remove (space-separated): ").strip().upper()
            requested = raw.split()
            pool = edit_candidates({"candidates": pool}, add=[], remove=requested)
            absent = sorted(set(requested) - set(previous_pool))
            if absent:
                print(f"Not in pool (ignored): {', '.join(absent)}.")
        else:
            print(f"Unrecognized choice {choice!r}.")
            continue

        if not pool:
            print("Resulting pool would be empty; ignoring this edit and keeping the previous pool.")
            # The currency is restored with the pool. Reverting one without
            # the other used to leave a restored non-empty pool alongside a
            # `None` currency, so the next add would re-establish it from
            # whatever was typed rather than from the pool that is actually
            # still there.
            pool, pool_currency = previous_pool, previous_pool_currency
            continue

        print(f"Candidate pool ({len(pool)}): {', '.join(pool)}")


def _prompt_for_benchmark(
    prompt: str,
    currency: str,
    rebalance_date: date,
    db_path: str,
    allow_fetch: bool,
) -> BenchmarkSource | None:
    """Ask for a benchmark ticker until one is usable or the person declines
    with a blank answer, returning the PREPARED source rather than the bare
    ticker.

    Returning the source matters: validating an answer means resolving the
    ticker and reading its return history anyway, so handing that back means
    the fetch which proved the answer good is the same one the report then
    uses, instead of a second identical round trip.

    A blank answer returns `None`, which every caller reads as "leave things
    as they were" - no benchmark at all on the first ask, or the previous one
    when changing it mid-session.
    """
    while True:
        raw = input(prompt).strip().upper()
        if not raw:
            return None

        source = prepare_benchmark(raw, currency, rebalance_date, db_path, allow_fetch=allow_fetch)
        if source.unavailable_reason is None:
            print(f"Benchmark set to {source.ticker}.")
            return source
        print(f"Refused: {source.unavailable_reason}")


def _settle_benchmark(
    pool: list[str],
    currency: str,
    rebalance_date: date,
    db_path: str,
    override: str | None = None,
    enabled: bool = True,
    allow_fetch: bool = True,
    pool_memory_path: str | None = None,
) -> BenchmarkSource | None:
    """Decide what this session's report compares the portfolio against, and
    resolve it into the return history the report will slice.

    Precedence is `resolve_benchmark_ticker`'s: this run's `--benchmark`, then
    what the pool recorded, then the currency's default. Only if all three
    come up empty - a pool in a currency `DEFAULT_BENCHMARKS` makes no
    assumption about, which today is every currency but USD - is the person
    asked, because guessing an index for them would quietly measure their
    portfolio against something they never chose.

    `pool_memory_path` is both where a recorded benchmark is read from and
    the signal that this is a `user_provided` session: a selection whose
    candidates come from an agent has no pool file to record a benchmark in
    and nobody mid-conversation to ask, and is USD by construction anyway, so
    it passes `None` and simply takes the default.

    A benchmark is written to that file only when it was chosen EXPLICITLY -
    named with `--benchmark` or typed at the prompt - never when it merely
    came from `DEFAULT_BENCHMARKS`. That way the file records decisions
    rather than defaults, and improving a default later still reaches every
    pool that never made one.

    `enabled=False` (from `--benchmark none`) returns `None`, which prints no
    benchmark line at all rather than an explanation.
    """
    if not enabled:
        return None

    saved = load_candidate_benchmark(pool_memory_path, currency) if pool_memory_path else None
    ticker = resolve_benchmark_ticker(currency, override=override, saved=saved)

    if ticker is None:
        if pool_memory_path is None:
            return prepare_benchmark(None, currency, rebalance_date, db_path, allow_fetch=allow_fetch)

        print(f"\nNo benchmark recorded for this {currency} pool.")
        source = _prompt_for_benchmark(
            "Benchmark ticker (blank to skip): ", currency, rebalance_date, db_path, allow_fetch
        )
        if source is None:
            return prepare_benchmark(None, currency, rebalance_date, db_path, allow_fetch=allow_fetch)
        save_candidate_pool(pool, path=pool_memory_path, currency=currency, benchmark=source.ticker)
        return source

    source = prepare_benchmark(ticker, currency, rebalance_date, db_path, allow_fetch=allow_fetch)
    if pool_memory_path and override and source.unavailable_reason is None:
        save_candidate_pool(pool, path=pool_memory_path, currency=currency, benchmark=source.ticker)
    return source


def _settle_risk_free_rate(
    override: float | None, currency: str, rates_path: str
) -> ResolvedRiskFreeRate:
    """Decide which risk-free rate this session's Sharpe ratios are measured
    against, and what to call its source.

    Precedence is `resolve_risk_free_rate`'s: this run's `--risk-free-rate`,
    then whatever `currency` remembered in `rates_path`, then the configured
    `settings.risk_free_rate` (which already reflects any `RISK_FREE_RATE`
    environment variable or `.env` entry). A remembered rate outranking the
    environment variable is deliberate: a per-currency entry is the more
    specific statement, and one global variable cannot express "0.5% for yen,
    4.25% for dollars" at all.

    Called once the pool's currency has settled, never before - the rate is
    a property of the currency, and before the confirm loop returns there is
    no currency to look one up for. The single resolved float then reaches
    the pool's figures, the benchmark line AND the holdings block, which is
    what keeps the three sets of numbers a report prints one under another on
    one scale.

    Deliberately does NOT write. Remembering is `_remember_risk_free_rate`'s
    job, called only once the run has actually produced a report - the same
    ordering the candidate-pool save observes (see `_run_edit_loop`, where a
    rejected edit must not survive on disk) and the same gate
    `_settle_benchmark` applies by writing only a benchmark that resolved.
    """
    return resolve_risk_free_rate(
        override, currency, load_risk_free_rate(rates_path, currency), settings.risk_free_rate
    )


def _remember_risk_free_rate(resolved: ResolvedRiskFreeRate, currency: str, rates_path: str) -> None:
    """Record an explicitly-given rate as `currency`'s remembered rate,
    announcing the write.

    Only a rate that came from the command line is written; a default or an
    already-remembered value is not. That is `DEFAULT_BENCHMARKS`' rule for
    `memory/candidates.json` applied here - the file records decisions, not
    defaults, so improving the default later still reaches every currency
    that never made one.

    Announced because this project has no silent writes, and because
    remembering is a side effect of a flag rather than an explicit command.
    The message names the file, since editing it is the only way to
    un-remember a rate.
    """
    if not resolved.from_override:
        return
    if save_risk_free_rate(resolved.rate, path=rates_path, currency=currency):
        print(
            f"\nRemembered {resolved.rate:.4f} as the {currency} risk-free rate in {rates_path}."
        )


def _settle_dividend_floor(
    min_annual_dividend: float | None,
    min_dividend_yield: float | None,
    portfolio_value: float,
    currency: str,
) -> DividendFloor | None:
    """Turn this run's dividend flags into the single yield floor the
    optimizer consumes, plus the phrase the report prints beside it.

    Called after the pool's currency has settled and never before, for the
    same reason `_settle_risk_free_rate` is: the floor's numeric value does
    not depend on the currency, but the sentence describing it does, and a
    cash amount printed without its unit is exactly the ambiguity
    `format_money` and `CURRENCY_SYMBOLS` exist to remove.

    Both flags were already refused back at `parser.error` time for being
    non-numeric, non-finite, negative or a mistyped percentage. This
    function repeats only the mutual exclusion, which argparse enforces for
    a command line but a programmatic caller could still violate - and it
    refuses rather than reconciling, because any reconciliation (`max`,
    `min`, last-one-wins) would silently discard a number somebody typed.

    `None` means no floor was asked for, and it is a genuinely different
    value from a floor of `0.0` - which is a deliberate no-op somebody
    typed, and which the report therefore prints as a floor.
    """
    if min_dividend_yield is not None and min_annual_dividend is not None:
        raise ValueError(
            "--min-annual-dividend and --min-dividend-yield are two spellings of one "
            "constraint; give at most one. Reconciling them would mean discarding a number "
            "you typed."
        )

    if min_dividend_yield is not None:
        return DividendFloor(
            yield_floor=validate_dividend_yield(min_dividend_yield, "--min-dividend-yield"),
            origin="--min-dividend-yield",
            cash_floor=None,
            portfolio_value=float(portfolio_value),
            currency=currency,
        )

    if min_annual_dividend is not None:
        return DividendFloor(
            yield_floor=validate_min_annual_dividend(
                min_annual_dividend, "--min-annual-dividend", portfolio_value
            ),
            origin=(
                f"--min-annual-dividend {format_money(min_annual_dividend, currency)} "
                f"/ --value {format_money(portfolio_value, currency)}"
            ),
            cash_floor=float(min_annual_dividend),
            portfolio_value=float(portfolio_value),
            currency=currency,
        )

    return None


def _prompt_dividend_floor(
    current: DividendFloor | None, portfolio_value: float, currency: str
) -> DividendFloor | None:
    """Ask for a new minimum dividend, returning `current` unchanged when the
    answer is blank or unusable - the same keep-what-you-had treatment
    `_prompt_target_return` gives a rejected edit, and what lets the caller
    skip a pointless recompute.

    Accepts three shapes, and ECHOES which one it read, because the
    cash-versus-yield distinction is an inference this function makes and
    the person must be able to check it:

        3000    a cash amount, divided by --value
        3%      a yield
        0.03    a yield
        none    clears the floor

    The rule separating cash from yield is magnitude: below 1 is a yield, 1
    or above is cash. A minimum annual dividend of thirty cents is not
    something anybody asks for, whereas `0.03` unmistakably reads as a
    decimal - and this is the same magnitude reasoning
    `validate_risk_free_rate` already uses to catch a mistyped percentage. A
    trailing `%` overrides the rule outright, which is how `3%` and `0.03`
    can both mean the same thing.

    `0` clears the floor rather than setting a zero one. Both readings are
    non-binding, and "I typed zero to turn it off" is overwhelmingly the
    likelier intent here - unlike `--min-dividend-yield 0` on a command
    line, which is a deliberate scripted no-op and is honoured as one.
    """
    current_text = (
        f"{current.yield_floor:.4f} from {current.origin}" if current is not None else "none"
    )
    raw = input(
        "Minimum dividend - cash like 3000, a yield like 3% or 0.03, or [n]one to clear "
        f"(current {current_text}): "
    ).strip()

    if not raw:
        return current
    if raw.lower() in ("n", "no", "none", "clear", "0"):
        print("Dividend floor cleared.")
        return None

    as_yield = raw.endswith("%")
    try:
        number = float(raw[:-1]) / 100 if as_yield else float(raw)
    except ValueError:
        print(f"Unrecognized minimum dividend {raw!r}; keeping {current_text}.")
        return current

    try:
        if as_yield or number < 1:
            floor = DividendFloor(
                yield_floor=validate_dividend_yield(number, "the dividend floor"),
                origin="[d]ividend, this session only",
                cash_floor=None,
                portfolio_value=float(portfolio_value),
                currency=currency,
            )
            print(f"Read as a {floor.yield_floor:.4f} minimum portfolio dividend yield.")
            return floor

        floor = DividendFloor(
            yield_floor=validate_min_annual_dividend(
                number, "the dividend floor", portfolio_value
            ),
            origin=(
                f"[d]ividend {format_money(number, currency)} / --value "
                f"{format_money(portfolio_value, currency)}, this session only"
            ),
            cash_floor=float(number),
            portfolio_value=float(portfolio_value),
            currency=currency,
        )
    except ValueError as e:
        print(f"Keeping {current_text}: {e}")
        return current

    print(
        f"Read as a {floor.yield_floor:.4f} minimum portfolio dividend yield "
        f"({format_money(number, currency)} on --value "
        f"{format_money(portfolio_value, currency)})."
    )
    return floor


def _prompt_target_return(prompt: str, current: float) -> float:
    """Ask for a new MV target annual return, returning `current` unchanged
    when the user presses enter or types something unparseable - the same
    keep-what-you-had treatment the objective prompt gives a rejected edit.
    An unchanged value is what lets the caller skip a pointless recompute.
    """
    raw = input(prompt).strip()
    if not raw:
        return current
    try:
        return float(raw)
    except ValueError:
        print(f"Unrecognized target return {raw!r}; keeping {current!r}.")
        return current


def _prompt_lookback_months(current: int) -> int:
    """Read a portfolio returns-window length, keeping `current` on bad input.

    This deliberately matches `portfolio-holdings whatif`: both commands
    offer the same 24-to-60-month experiment, and a rejected answer costs
    one prompt rather than destroying the valid window already in force.
    """
    raw = input(
        f"Months of returns to optimize over (24-60, currently {current}): "
    ).strip()
    if not raw:
        return current

    try:
        months: object = int(raw)
    except ValueError:
        months = raw

    try:
        return validate_lookback_months(months, "the window")
    except ValueError as e:
        print(f"Keeping {current} months: {e}")
        return current


def _run_edit_loop(
    candidates: list[str],
    objective: str,
    portfolio_value: float,
    rebalance_date: date,
    db_path: str,
    selection: str = "llm_s_only",
    memory_path: str = DEFAULT_CANDIDATES_PATH,
    target_annual_return: float = DEFAULT_TARGET_ANNUAL_RETURN,
    dividend_floor: DividendFloor | None = None,
    consult_dividends: bool = True,
    risk_free_rate: float = settings.risk_free_rate,
    currency: str = DEFAULT_CURRENCY,
    benchmark: BenchmarkSource | None = None,
    allow_benchmark_fetch: bool = True,
    risk_free_rate_origin: str | None = None,
    show_summaries: bool = False,
    profile_cache: dict[str, TickerProfile] | None = None,
    archive: ReportArchive | None = None,
    lookback_months: int = DEFAULT_LOOKBACK_MONTHS,
) -> bool:
    """Prompt in a loop for add/remove/objective/target-return/window/
    benchmark/finish; each
    edit recomputes weights and allocation against the current `candidates`
    and reprints them. Returns when the user chooses to finish, reporting
    whether it ever printed a report.

    That return value exists because this loop is now also entered when the
    INITIAL optimization produced nothing - an impossible `--target-return`,
    dividend floor or risk-free rate - which is why it prompts before it
    computes. Live mode's snapshot cost minutes of fetching and is still
    open, so `[t]`, `[d]` or `[o]` can correct the number without paying for
    any of it again; exiting instead would make a corrected retry cost a
    full refetch. `main` combines the flag with the initial outcome to pick
    an exit code, so a session that explained why it could not optimize and
    was then finished without a correction exits non-zero rather than
    looking like a success.

    For `selection="user_provided"` an added ticker is validated and
    ingested first (the pool is the user's own, so a typo here deserves the
    same message it gets in the confirm loop), and every accepted edit is
    persisted to `memory_path` - unlike the other selections, whose
    candidate lists are one agent run's ephemeral output and are
    deliberately never saved. "Accepted" means the recompute succeeded, so
    the save happens after it: an edit the optimizer rejects is reverted in
    memory and must not survive on disk.

    `target_annual_return` only means anything while `objective` is `"MV"`,
    so the `[t]` choice explains itself and changes nothing under the other
    two objectives, and switching to `"MV"` prompts for a target in the same
    step - that switch is the moment the value starts to matter, and a user
    who has never considered it would otherwise silently inherit whatever
    default the command line supplied.

    `risk_free_rate` is not editable here, but it must still be carried:
    every recompute has to use the rate the session settled on, or a
    `--risk-free-rate` - or a rate this currency remembered - would apply to
    the initial run and then silently revert to the configured default on the
    first edit. The rate stays deliberately non-editable because, unlike
    MV's target, it is a property of the market environment rather than of
    the portfolio being designed, so changing it mid-session invites treating
    it as a knob to make a Sharpe ratio look better.

    `risk_free_rate_origin` is carried for exactly the same reason as the
    rate: provenance that appeared on the initial report and vanished on the
    first edit would be worse than none at all.

    `lookback_months` starts at the optimizer's unchanged 60-month default
    and is editable with `[w]indow`. A successful change applies to every
    later recompute and ticker summary in this loop; an infeasible solve
    restores it with the other editable values. It is deliberately session
    state only, never candidate memory or configuration.

    `benchmark` is carried for the same reason and re-narrowed to the
    portfolio's window on every recompute, so the comparison line follows
    each edit rather than describing the run as it was before it. `[b]`
    changes it, prompting until an answer is usable; a refused or blank
    answer keeps the benchmark already in force, the same keep-what-you-had
    treatment `[o]bjective` gives a rejected edit. A benchmark chosen here is
    always an explicit choice, so for `user_provided` it is persisted.

    `[s]ummary` is offered for `user_provided` only - the same narrow gate
    that already makes adds validate and edits persist in this loop - and
    prints one ticker's performance summary without changing anything, so it
    cannot end the session or force a recompute. Unlike the candidate-pool
    confirm loop, this one already carries the settled `risk_free_rate` and
    `risk_free_rate_origin`, which are passed straight through: a summary
    printed here must agree with the report printed above it rather than
    resolving a second rate of its own.

    `show_summaries` defaults to `False` for the reason given in
    `_run_user_provided_confirm_loop`: the automatic post-add summary reaches
    Yahoo Finance, and a `True` default would silently give network access to
    every existing caller and test of this loop. `main` passes `True`.
    `profile_cache` is shared with that loop so a ticker summarized before
    the report is not re-fetched after it.

    `archive` is carried for exactly the same reason `risk_free_rate` and
    `risk_free_rate_origin` are: a facility that applied to the initial report
    and silently stopped applying on the first edit would be worse than none at
    all. Every recompute here is a report of its own and is offered to the
    archive as one; identical ones collapse by digest, which is what keeps an
    edit that returns to an earlier portfolio from adding a second copy of it.
    Defaults to `None`, so every existing caller and test of this loop writes
    nothing and prints byte-identically to before.
    """
    def show_summary(symbol: str) -> None:
        """Print one ticker's summary against the rate this session settled
        on, so it agrees with the report printed above it."""
        summary = prepare_ticker_summary(
            symbol,
            rebalance_date,
            candidates,
            db_path,
            currency=currency if symbol in candidates else None,
            risk_free_rate=risk_free_rate,
            risk_free_rate_origin=risk_free_rate_origin,
            profile_cache=profile_cache,
            lookback_months=lookback_months,
        )
        print_ticker_summary(summary.profile, summary.stats, summary.risk_free_rate_origin)

    printed_a_report = False
    summary_offer = " / [s]ummary" if selection == "user_provided" else ""
    while True:
        choice = input(
            "\nEdit candidates? [a]dd tickers / [r]emove tickers / [o]bjective / "
            f"[t]arget-return / [d]ividend / [w]indow / [b]enchmark{summary_offer} / "
            "[f]inish: "
        ).strip().lower()

        if choice in ("", "f", "finish"):
            return printed_a_report

        if choice in ("s", "summary") and selection == "user_provided":
            symbol = input("Ticker to summarize: ").strip().upper()
            if not symbol:
                print("No ticker given; nothing to summarize.")
                continue
            # Returns before the edit machinery below: this choice changes
            # nothing, so it must not recompute, reprint the report, or be
            # able to revert anything.
            show_summary(symbol)
            continue

        (
            previous_candidates,
            previous_objective,
            previous_target,
            previous_dividend_floor,
            previous_window,
        ) = (
            candidates,
            objective,
            target_annual_return,
            dividend_floor,
            lookback_months,
        )
        if choice in ("a", "add"):
            raw = input("Ticker(s) to add (space-separated): ").strip().upper()
            if selection == "user_provided":
                edit = validate_and_edit_candidates(
                    candidates, add=raw.split(), remove=[], as_of=rebalance_date, db_path=db_path,
                    pool_currency=currency,
                )
                candidates = edit.pool
                _print_add_outcome(edit.added, edit.invalid, edit.refused, currency)
                # Before the recompute below, so the summary that justifies
                # keeping a ticker is read beside the add that admitted it
                # rather than under the whole reprinted report.
                if show_summaries:
                    for symbol in edit.added:
                        show_summary(symbol)
            else:
                candidates = edit_candidates({"candidates": candidates}, add=raw.split(), remove=[])
        elif choice in ("r", "remove"):
            raw = input("Ticker(s) to remove (space-separated): ").strip().upper()
            candidates = edit_candidates({"candidates": candidates}, add=[], remove=raw.split())
        elif choice in ("o", "objective"):
            raw = input(f"New objective ({'/'.join(VALID_OBJECTIVES)}): ").strip().upper()
            if raw not in VALID_OBJECTIVES:
                print(f"Unrecognized objective {raw!r}; keeping {objective!r}.")
                continue
            objective = raw
            if objective == "MV":
                target_annual_return = _prompt_target_return(
                    f"Target annual return for MV (default {target_annual_return}): ", target_annual_return
                )
        elif choice in ("d", "dividend"):
            if not consult_dividends:
                # The same contradiction `--no-dividend-fetch` is refused
                # for at the command line, reached the other way round.
                print(
                    "A dividend floor needs dividend data, and this run was started with "
                    "--no-dividend-fetch. Restart without that flag to set one."
                )
                continue
            new_floor = _prompt_dividend_floor(dividend_floor, portfolio_value, currency)
            # NamedTuple equality is by value, so re-typing the same floor
            # is correctly a no-op and skips the recompute - matching
            # [t]arget-return's own `if new_target == target_annual_return`.
            if new_floor == dividend_floor:
                continue
            dividend_floor = new_floor
        elif choice in ("t", "target-return"):
            if objective != "MV":
                print(f"Target annual return applies only to objective MV; current objective is {objective!r}.")
                continue
            new_target = _prompt_target_return(
                f"New target annual return (current {target_annual_return}): ", target_annual_return
            )
            if new_target == target_annual_return:
                continue
            target_annual_return = new_target
        elif choice in ("w", "window"):
            chosen = _prompt_lookback_months(lookback_months)
            if chosen == lookback_months:
                continue
            lookback_months = chosen
        elif choice in ("b", "benchmark"):
            current = benchmark.ticker if benchmark is not None else None
            new_benchmark = _prompt_for_benchmark(
                f"New benchmark ticker (current {current or 'none'}): ",
                currency, rebalance_date, db_path, allow_benchmark_fetch,
            )
            if new_benchmark is None:
                continue
            benchmark = new_benchmark
            if selection == "user_provided":
                save_candidate_pool(
                    candidates, path=memory_path, currency=currency, benchmark=benchmark.ticker
                )
        else:
            print(f"Unrecognized choice {choice!r}.")
            continue

        if not candidates:
            print("Candidate list is empty; ignoring this edit and keeping the previous list.")
            candidates = previous_candidates
            continue

        print(f"\nCandidates ({len(candidates)}): {', '.join(candidates)}")
        try:
            stats, allocation = compute_weights_and_allocation(
                candidates, objective, portfolio_value, rebalance_date, db_path,
                target_annual_return=target_annual_return, risk_free_rate=risk_free_rate,
                dividend_floor=dividend_floor, consult_dividends=consult_dividends,
                lookback_months=lookback_months,
            )
        except ValueError as e:
            # An edit can be individually valid and still leave the optimizer
            # with nothing to solve - most easily by asking MV for a target
            # return no combination of these candidates can reach, or by
            # assembling a pool that mixes currencies (MixedCurrencyPoolError
            # is a ValueError for exactly this reason). That is a rejected
            # edit, not a failed session: this loop holds live mode's only
            # snapshot open, so letting it escape would throw away the fetched
            # data and the user's confirmed pool along with it.
            print(f"Cannot optimize that edit: {e}")
            print(
                "Keeping the previous candidates, objective, target return, dividend floor, "
                "and returns window."
            )
            candidates, objective, target_annual_return, dividend_floor, lookback_months = (
                previous_candidates,
                previous_objective,
                previous_target,
                previous_dividend_floor,
                previous_window,
            )
            continue

        if selection == "user_provided" and choice in ("a", "add", "r", "remove"):
            save_candidate_pool(candidates, path=memory_path, currency=currency)

        printed_a_report = True
        if choice in ("w", "window"):
            print(f"Measuring over {lookback_months} month(s) of returns.")
        # Hoisted out of the call below because the archived facts need the
        # same `BenchmarkStats` the report prints, and re-narrowing the
        # benchmark twice could only ever disagree with itself.
        benchmark_stats = benchmark_stats_for_window(
            benchmark, stats.returns_window_start, stats.returns_window_end, risk_free_rate
        )
        with record_report(
            archive,
            **report_facts(
                variant="edit",
                currency=currency,
                stats=stats,
                objective=objective,
                selection=selection,
                candidates=candidates,
                portfolio_value=portfolio_value,
                benchmark=benchmark_stats,
            ),
        ):
            print_weights_and_allocation(
                stats, allocation, objective, currency, portfolio_value=portfolio_value,
                benchmark=benchmark_stats,
                risk_free_rate_origin=risk_free_rate_origin,
                requested_lookback_months=lookback_months,
            )


def main() -> None:
    parser = argparse.ArgumentParser(description="Run plans/06_interactive_flow.md's interactive pipeline.")
    parser.add_argument("--date", required=True, help="Rebalance date, YYYY-MM-DD, or 'today' for live mode.")
    parser.add_argument(
        "--objective",
        default=None,
        choices=VALID_OBJECTIVES,
        help="Which portfolio to solve for. Left out, it is derived from the pool: MV "
             "targeting the benchmark's own expected return when that benchmark can be "
             "measured - matching what it returned, at the least risk that does so - and "
             "GMV when there is no benchmark return to aim at. The report always says which "
             "it chose and why. Giving --target-return without --objective implies MV.",
    )
    parser.add_argument(
        "--value",
        required=True,
        type=float,
        help="Total portfolio value to allocate, in the portfolio's own currency (shown as the "
             "'Portfolio currency' line in the output). For --selection user_provided that is the "
             "currency the pool's tickers are priced in; for every other selection it is USD.",
    )
    parser.add_argument("--selection", default="llm_s_only", choices=VALID_SELECTIONS)
    parser.add_argument(
        "--target-return",
        type=float,
        default=None,
        help="Annual return --objective MV optimizes toward; ignored by GMV and MSR.",
    )
    parser.add_argument(
        "--risk-free-rate",
        type=float,
        default=None,
        help="Rate --objective MSR maximizes its Sharpe ratio against, and that every "
             "objective's reported Sharpe ratio - the pool's, the benchmark's and your own "
             "holdings' - is measured against. A decimal (4.25%% is 0.0425; negative rates are "
             "allowed). A value given here is REMEMBERED as this run's currency's rate and used "
             "by later runs of both this command and 'uv run portfolio-holdings'. Left out, the "
             "rate is whatever that currency remembered, else the configured RISK_FREE_RATE, "
             "else 2%%. The report always says which of those it used.",
    )
    dividend_floor_group = parser.add_mutually_exclusive_group()
    dividend_floor_group.add_argument(
        "--min-annual-dividend",
        type=float,
        default=None,
        metavar="CASH",
        help="Refuse any portfolio expected to pay less than this in dividends over the coming "
             "year, in the portfolio's own currency - the same unit as --value, shown as the "
             "'Portfolio currency' line. It becomes one linear constraint on the weights by "
             "being divided by --value, so '--min-annual-dividend 3000 --value 100000' is "
             "exactly '--min-dividend-yield 0.03'. Applies to GMV, MV and MSR alike. The "
             "estimate is each ticker's TRAILING twelve-month yield - a record of what was "
             "paid, not a promise of what will be. Mutually exclusive with "
             "--min-dividend-yield.",
    )
    dividend_floor_group.add_argument(
        "--min-dividend-yield",
        type=float,
        default=None,
        metavar="YIELD",
        help="The same single constraint --min-annual-dividend produces, stated as a decimal "
             "yield rather than a cash amount and so independent of --value (3%% is 0.03; the "
             f"ceiling is {MAX_DIVIDEND_YIELD}). Mutually exclusive with "
             "--min-annual-dividend.",
    )
    parser.add_argument("--db-path", default=settings.db_path)
    parser.add_argument(
        "--benchmark",
        default=None,
        help="Ticker the report compares the portfolio against, over the same months and with "
             f"the same estimators - or '{BENCHMARK_DISABLED}' to leave the comparison out. "
             "Defaults to the pool's recorded benchmark, else the currency's default (SPY for "
             "USD); a pool in a currency with no default is asked for one. Must trade in the "
             "portfolio's own currency. For --selection user_provided, a benchmark named here "
             "is remembered with the pool.",
    )
    parser.add_argument(
        "--no-dividend-fetch",
        action="store_true",
        help="Skip fetching dividend history and leave the dividend figures out of the "
             "report. In live mode the session snapshot otherwise builds dividends for the "
             "whole membership universe, which is one more pass over ~500 tickers on top of "
             "the prices, factors, momentum and returns it already fetches. Refused "
             "alongside --min-annual-dividend or --min-dividend-yield, which need that data "
             "to enforce a floor. Historical-date runs read whatever "
             "'uv run portfolio-build-dividends' put in the shared cache and never fetch, "
             "so there this flag only suppresses the report's dividend lines.",
    )
    parser.add_argument(
        "--no-benchmark-fetch",
        action="store_true",
        help="Take the benchmark only from returns this session's database already holds, "
             "never by fetching. Keeps a backtest-window run entirely offline, at the cost of "
             "reporting the benchmark as unavailable when the cache does not contain it.",
    )
    parser.add_argument(
        "--no-ticker-summary",
        action="store_true",
        help="Do not print a ticker's performance summary automatically when it is added to "
             "a --selection user_provided candidate pool. Each summary costs two Yahoo "
             "Finance requests, so a scripted run that already knows its pool need not pay "
             "for them. The [s]ummary choice in both interactive loops still works, since "
             "asking for a summary is not the same as having it volunteered.",
    )
    parser.add_argument(
        "--memory-path",
        default=DEFAULT_CANDIDATES_PATH,
        help="Which file the user_provided selection's candidate pools are persisted in - an "
             "alternate or scratch store, for trying something without touching a curated pool. "
             "One file holds one pool per currency, so this is not how currencies are kept apart; "
             "when several pools are saved you are asked which to resume, or name one with "
             "--currency.",
    )
    parser.add_argument(
        "--currency",
        default=None,
        help="Which currency's saved candidate pool to resume, answering the 'Resume which pool?' "
             "prompt from the command line so the run can be scripted. A currency with no saved "
             "pool starts a new one, with that currency settled up front - so a ticker in any "
             "other currency is then refused by name rather than silently establishing a "
             "different pool. Applies only to --selection user_provided; every other selection "
             "screens the S&P 500 and is USD by construction.",
    )
    parser.add_argument(
        "--rates-path",
        default=DEFAULT_RATES_PATH,
        help="Which file the per-currency risk-free rates are remembered in. One file holds "
             "one rate per currency, shared with 'uv run portfolio-holdings'.",
    )
    parser.add_argument(
        "--holdings-path",
        default=DEFAULT_PORTFOLIO_PATH,
        help="Which file the portfolio you actually hold is read from, for the holdings block at "
             "the end of the report. Maintained by 'uv run portfolio-holdings'; one file holds "
             "one portfolio per currency, and the one matching this run's currency is reported.",
    )
    parser.add_argument(
        "--holdings-cache-path",
        default=DEFAULT_HOLDINGS_CACHE_PATH,
        help="Which file the holdings' prices and monthly returns are cached in, so the holdings "
             "block costs no network on a repeated run. Shared with 'uv run portfolio-holdings'. "
             "Never the same file as --db-path: holdings rows must not land in the database the "
             "candidate pool is measured against.",
    )
    parser.add_argument(
        "--output-dir",
        default=settings.output_dir,
        help="Directory every report this run prints is also saved under, one subdirectory per "
             "month of --date (so 'output/2026-09/'). A report identical to one already saved "
             "that month is recognized by a digest of its own text and not written twice. Point "
             "it somewhere else to try something without adding to the real archive; override "
             "the default for good with OUTPUT_DIR.",
    )
    parser.add_argument(
        "--no-save-reports",
        action="store_true",
        help="Print the reports and keep no copy of them - the counterpart of --no-holdings. "
             "Nothing is written under --output-dir, and no directory is created.",
    )
    parser.add_argument(
        "--refresh-holdings",
        action="store_true",
        help="Refetch the holdings' prices now rather than reusing the cache, which is otherwise "
             "good for the rest of the calendar month. The report states the date it priced the "
             "holdings at, and this is how you move it.",
    )
    parser.add_argument(
        "--no-holdings",
        action="store_true",
        help="Leave the holdings block out of the report entirely - the counterpart of "
             "'--benchmark none'. Use it when you keep no record of your holdings here and do "
             "not want to be told so on every run.",
    )
    parser.add_argument(
        "--no-holdings-fetch",
        action="store_true",
        help="Measure the holdings only from data already on disk - this session's database or "
             "the holdings cache - never by fetching. Keeps a backtest-window run entirely "
             "offline, at the cost of reporting the holdings as unmeasurable when neither "
             "contains them.",
    )
    args = parser.parse_args()

    # Before anything opens a database or fetches anything. A live-mode screened
    # run otherwise pays for the whole Yahoo Finance snapshot and only then
    # discovers it has no API key, reporting it as a provider exception from
    # inside CrewAI rather than as the missing variable it is.
    require_api_keys(args.selection)

    if args.currency:
        args.currency = args.currency.strip().upper()
        if args.selection != "user_provided":
            # Refused rather than ignored. For every other selection the
            # currency is the hardcoded DEFAULT_CURRENCY, because those
            # selections screen the S&P 500 - so accepting the flag would
            # print a USD report to someone who just asked for JPY, which is
            # exactly the silent contradiction this codebase refuses
            # everywhere else. `parser.error` exits 2 with argparse's own
            # formatting, the same path a malformed --risk-free-rate takes.
            parser.error(
                "--currency applies only to --selection user_provided; every other selection "
                "screens the S&P 500 and is USD by construction"
            )

    if args.risk_free_rate is not None:
        # Checked here, before `open_pipeline_session` builds a live snapshot
        # (a real Wikipedia/yfinance/SEC fetch) and before the user_provided
        # confirm loop re-ingests every saved ticker. Refusing a mistyped rate
        # only after all that would be gratuitous - and note argparse's
        # `type=float` accepts `nan` and `inf` quite happily, so this is the
        # only place they can be caught.
        #
        # Reported through `parser.error`, which exits 2 with argparse's own
        # formatting, because a malformed argument value is precisely what
        # argparse already reports that way (`--value abc`); a traceback for
        # a typo would be a worse answer to the same class of mistake. This
        # is not the case `plans/10_performance_reporting_and_target_return.md`
        # decided to let propagate - that was an unreachable `--target-return`,
        # which only the optimizer can discover.
        try:
            args.risk_free_rate = validate_risk_free_rate(args.risk_free_rate, "--risk-free-rate")
        except ValueError as e:
            parser.error(str(e))

    # Both dividend flags are refused here, before `open_pipeline_session`
    # builds a live snapshot out of real Wikipedia/yfinance/SEC calls, for
    # the same reason the rate above is - and because argparse's
    # `type=float` accepts `nan` and `inf` quite happily, so this is the
    # only place they can be caught at all. A `nan` floor would otherwise
    # reach cvxpy as a constraint that is neither met nor refused, reported
    # with a message naming neither the flag nor the number.
    if args.min_dividend_yield is not None:
        try:
            args.min_dividend_yield = validate_dividend_yield(
                args.min_dividend_yield, "--min-dividend-yield"
            )
        except ValueError as e:
            parser.error(str(e))

    if args.no_dividend_fetch and (
        args.min_dividend_yield is not None or args.min_annual_dividend is not None
    ):
        # Refused here rather than left to the optimizer, which would raise
        # `DividendYieldUnavailableError` naming every ticker in the pool -
        # a poor way to discover you typed two incompatible flags, and only
        # after a live snapshot had been fetched.
        parser.error(
            "--no-dividend-fetch cannot be combined with --min-annual-dividend or "
            "--min-dividend-yield: a dividend floor is enforced against the dividend data "
            "this flag declines to fetch. Drop the floor, or drop --no-dividend-fetch."
        )

    if args.min_annual_dividend is not None:
        # Checked against --value here, but NOT converted: the conversion's
        # `origin` string needs the pool's currency, which is not settled
        # until the confirm loop returns. The numeric refusal happens before
        # any network work; only the wording is built later.
        try:
            validate_min_annual_dividend(
                args.min_annual_dividend, "--min-annual-dividend", args.value
            )
        except ValueError as e:
            parser.error(str(e))

    rebalance_date = parse_date(args.date)

    # One archive for the whole run, not one per report: the output directory,
    # the kind, the as-of date and the command line are properties of the
    # invocation rather than of any single report. Built here, before
    # `open_pipeline_session` fetches anything, so a malformed --output-dir is
    # discovered before a live snapshot costs minutes of Yahoo Finance calls.
    # The month folder follows `rebalance_date`, not the clock, because a
    # report is about a month of market data: a --date 2024-03-29 run belongs
    # with the month it measured.
    archive = ReportArchive(
        output_dir=args.output_dir,
        enabled=not args.no_save_reports,
        kind="portfolio",
        as_of=rebalance_date,
        command=command_line(),
    )

    benchmark_enabled = args.benchmark != BENCHMARK_DISABLED
    benchmark_override = args.benchmark if benchmark_enabled else None

    # One cache for the whole run, shared by both interactive loops, so a
    # ticker summarized while confirming the pool is not fetched again when
    # it is summarized after the report. Yahoo's description of a security is
    # a point-in-time fact rather than a history to accumulate, which is why
    # this is a dictionary that dies with the process and not a table.
    profile_cache: dict[str, TickerProfile] = {}

    with open_pipeline_session(
        rebalance_date,
        args.selection,
        args.db_path,
        allow_dividend_fetch=not args.no_dividend_fetch,
    ) as (session_db_path, mode):
        candidates = None
        currency = DEFAULT_CURRENCY
        if args.selection == "user_provided":
            candidates, currency = _run_user_provided_confirm_loop(
                load_all_pools(args.memory_path),
                rebalance_date,
                session_db_path,
                memory_path=args.memory_path,
                currency=args.currency,
                rates_path=args.rates_path,
                risk_free_rate=args.risk_free_rate,
                show_summaries=not args.no_ticker_summary,
                profile_cache=profile_cache,
            )

        # Settled after the confirm loop, never before: an empty pool has no
        # currency until the first ticker is added, and the benchmark has to
        # be measured against the currency the pool actually ended up in.
        # Settled here for the same reason the benchmark is, and immediately
        # before it: the rate is a property of the pool's currency, which an
        # empty pool does not have until the confirm loop returns. The single
        # resolved float below then reaches the pool's figures, the benchmark
        # line and the holdings block, which is what keeps the three sets of
        # numbers this report prints one under another on one scale.
        resolved_rate = _settle_risk_free_rate(args.risk_free_rate, currency, args.rates_path)
        # Rebound so that no consumption site can silently keep using the raw
        # flag (which is now `None` whenever it was not given).
        args.risk_free_rate = resolved_rate.rate

        # Settled here, beside the rate, for the same reason: the floor's
        # numeric value is currency-independent but the sentence describing
        # it is not, and `currency` is only known now.
        dividend_floor = _settle_dividend_floor(
            args.min_annual_dividend, args.min_dividend_yield, args.value, currency
        )

        benchmark = _settle_benchmark(
            candidates or [],
            currency,
            rebalance_date,
            session_db_path,
            override=benchmark_override,
            enabled=benchmark_enabled,
            allow_fetch=not args.no_benchmark_fetch,
            pool_memory_path=args.memory_path if args.selection == "user_provided" else None,
        )

        result = run_pipeline_against(
            rebalance_date, args.objective, args.value, args.selection, session_db_path, mode,
            candidates=candidates, target_annual_return=args.target_return,
            risk_free_rate=args.risk_free_rate, currency=currency, benchmark=benchmark,
            dividend_floor=dividend_floor,
            consult_dividends=not args.no_dividend_fetch,
        )
        print_pipeline_result(
            result, risk_free_rate_origin=resolved_rate.origin, portfolio_value=args.value,
            archive=archive,
        )

        # Remembered only now, after the run has actually produced a report.
        # `plans/10_performance_reporting_and_target_return.md` established
        # this ordering for the candidate pool ("the save now happens after a
        # successful recompute"), and `_settle_benchmark` applies the same gate
        # by writing only a benchmark that resolved. Without it, an
        # `--objective MSR --risk-free-rate 0.05` run against a pool where no
        # ticker clears 5% would write 0.05 to disk and then raise out of
        # PyPortfolioOpt, leaving standing state from a run that printed
        # nothing.
        # Skipped when the run produced no portfolio, for exactly the
        # reason the comment above gives: this is the "printed nothing"
        # case, reached now by report rather than by traceback. A later
        # interactive edit may succeed, but the remember call sits here by
        # design, so a run whose first attempt failed does not record its
        # rate. That is the conservative reading of "only after a report".
        produced_report = result.get("unsatisfiable") is None
        if produced_report:
            _remember_risk_free_rate(resolved_rate, currency, args.rates_path)

        # Printed once, here, rather than from inside
        # `print_weights_and_allocation`: that function reprints the pool's
        # currency, window and benchmark after every interactive edit
        # because those are what an edit changes, and an edit to the
        # candidate pool changes nothing about what the user owns. Repeating
        # three unchanged numbers after every keystroke would be noise, and
        # would imply a relationship between the edit and the holdings that
        # does not exist.
        # Also skipped on the unsatisfiable path: the holdings block reads
        # "beneath the optimizer's own figures", and there are none to sit
        # beneath. It costs a fetch, and the reason and the edit prompt are
        # what the reader needs next.
        if not args.no_holdings and produced_report:
            stale = format_stale_share_counts(
                args.holdings_path,
                currency,
                load_portfolio(args.holdings_path, currency),
                args.holdings_cache_path,
            )
            if stale is not None:
                print(stale)
            print_user_portfolio(
                prepare_holdings(
                    load_portfolio(args.holdings_path, currency),
                    currency,
                    rebalance_date,
                    session_db_path,
                    risk_free_rate=args.risk_free_rate,
                    allow_fetch=not args.no_holdings_fetch,
                    cache_path=args.holdings_cache_path,
                    force_refresh=args.refresh_holdings,
                ),
                args.holdings_path,
                risk_free_rate_origin=resolved_rate.origin,
            )

        # The RESOLVED objective and target, not the raw flags: those may be
        # `None` because the pool derived them, and the loop has to continue
        # from what the report actually showed. Resolution happens once per
        # session - the returns window is database-derived and stable - so an
        # edit changes the pool, never silently the objective.
        # Read defensively: `run_pipeline_against` populates this, but a
        # programmatic caller assembling its own result dict should not have
        # to, and falling back to the reported objective is always correct -
        # it is the resolved one.
        resolved = result.get("resolved_objective")
        loop_objective = (
            resolved.objective
            if resolved is not None
            else (result.get("objective") or args.objective)
        )
        # Fall back to the FLAG before the default: a `--target-return` the
        # user typed must reach the edit loop even when the result carries no
        # resolved objective, or a rate that applied to the initial run would
        # silently revert on the first edit - the bug plan 10 fixed for the
        # risk-free rate, in a new place.
        if resolved is not None and resolved.target_annual_return is not None:
            loop_target = resolved.target_annual_return
        elif args.target_return is not None:
            loop_target = args.target_return
        else:
            loop_target = DEFAULT_TARGET_ANNUAL_RETURN
        edited_report = _run_edit_loop(
            result["scan_detail"]["candidates"], loop_objective, args.value, rebalance_date,
            session_db_path,
            selection=args.selection, memory_path=args.memory_path,
            target_annual_return=loop_target,
            risk_free_rate=args.risk_free_rate,
            dividend_floor=dividend_floor,
            consult_dividends=not args.no_dividend_fetch,
            currency=currency, benchmark=benchmark,
            allow_benchmark_fetch=not args.no_benchmark_fetch,
            risk_free_rate_origin=resolved_rate.origin,
            show_summaries=not args.no_ticker_summary,
            profile_cache=profile_cache,
            archive=archive,
        )

        # A run that never printed a portfolio must not look like a success
        # to a script, however gracefully it explained itself.
        if not (produced_report or edited_report):
            raise SystemExit(1)


if __name__ == "__main__":
    main()
