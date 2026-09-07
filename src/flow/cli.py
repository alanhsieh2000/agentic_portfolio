"""CLI entry point for `plans/06_interactive_flow.md`'s backtest and live
modes: `uv run python -m src.flow.cli --date YYYY-MM-DD|today --objective
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
change the objective, or change MV's target return; each edit re-runs only
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
`src/flow/rate_memory.py`) and shared with `uv run portfolio-holdings`.
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
from datetime import date

from src.config.settings import settings
from src.dataset.ticker_currency import DEFAULT_CURRENCY, group_by_currency
from src.dataset.holdings_cache import DEFAULT_HOLDINGS_CACHE_PATH
from src.dataset.ticker_ingestion import validate_and_ingest_tickers
from src.flow.candidate_memory import (
    DEFAULT_CANDIDATES_PATH,
    load_all_pools,
    load_candidate_benchmark,
    load_pool_benchmarks,
    save_candidate_pool,
)
from src.flow.interactive import (
    VALID_SELECTIONS,
    compute_weights_and_allocation,
    edit_candidates,
    open_pipeline_session,
    prepare_benchmark,
    prepare_holdings,
    run_pipeline_against,
    validate_and_edit_candidates,
)
from src.flow.rate_memory import (
    DEFAULT_RATES_PATH,
    ResolvedRiskFreeRate,
    load_risk_free_rate,
    resolve_risk_free_rate,
    save_risk_free_rate,
    validate_risk_free_rate,
)
from src.flow.user_portfolio import DEFAULT_PORTFOLIO_PATH, load_portfolio
from src.optimizer.benchmark import (
    BenchmarkSource,
    BenchmarkStats,
    benchmark_stats_for_window,
    resolve_benchmark_ticker,
)
from src.optimizer.dividends import (
    DIVIDEND_BINDING_TOLERANCE,
    MAX_DIVIDEND_YIELD,
    NO_DIVIDEND_FIGURES,
    DividendFloor,
    validate_dividend_yield,
    validate_min_annual_dividend,
)
from src.optimizer.holdings import HoldingsStats
from src.optimizer.portfolio import DEFAULT_TARGET_ANNUAL_RETURN, PortfolioStats, VALID_OBJECTIVES


CURRENCY_SYMBOLS = {"USD": "$", "JPY": "¥", "GBP": "£", "EUR": "€"}
"""Symbols for the currencies most likely to come up, used alongside - never
instead of - the ISO code. A bare '$' is shared by the US, Canadian,
Australian, Hong Kong and Singapore dollars, and removing exactly that
ambiguity is the point of tracking currency at all, so the code is always
printed too. A currency absent from this table simply prints its code.
"""


def parse_date(value: str) -> date:
    """A `--date` argument as a `date`: an ISO `YYYY-MM-DD`, or `"today"`.

    Public because `src/flow/holdings_cli.py` parses the same `--date`
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

    `origin` (from `src/flow/rate_memory.py`'s `ResolvedRiskFreeRate`) is
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
            print(f"  {ticker}: yield n/a - no trailing dividend data")
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
    """
    print(f"\nPortfolio currency: {currency} - --value is interpreted as {currency}")
    print(f"Returns window: {stats.returns_window_start} to {stats.returns_window_end} "
          f"({stats.returns_window_months} month(s) of monthly returns)")

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
            print(
                f"  covers {stats.dividend_weight_covered:.4f} of the weight; "
                f"{', '.join(stats.dividend_yields_missing)} "
                f"{'has' if len(stats.dividend_yields_missing) == 1 else 'have'} no trailing "
                "dividend data and are left out"
                if len(stats.dividend_yields_missing) != 1
                else f"  covers {stats.dividend_weight_covered:.4f} of the weight; "
                     f"{stats.dividend_yields_missing[0]} has no trailing dividend data and is "
                     "left out"
            )
    print(format_dividend_floor(
        stats.dividend_yield_floor, stats.dividend_floor_origin, stats.portfolio_dividend_yield
    ))
    if objective == "MV":
        print(f"Target annual return: {stats.target_annual_return:.4f}")
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

    Two things separate them, and the second is larger than it looks.
    Whole shares plus leftover cash cannot reproduce continuous weights
    exactly, which is a rounding effect worth a fraction of a percent. But
    `allocate_shares` prices shares from `load_latest_prices`, which reads
    `adj_close`, while a dividend yield is cash over the raw market `close`
    (see `src/dataset/dividends.py`'s `load_latest_close` for why it must
    be). On a database whose price window ended well before it was fetched,
    `adj_close` sits meaningfully below `close` - by 6.7% to 17.8% across
    the dividend payers in the shipped `data/portfolio.duckdb` - so the
    share counts are struck against the lower number and this total comes
    out correspondingly higher. It is an accurate statement about the share
    counts printed above it; the gap is a property of which price column
    the pre-existing allocation step uses, not of this arithmetic. On a
    cache fetched up to today the two columns agree at the newest date and
    the gap collapses to rounding alone.

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
    if total_value and covered is not None and abs(covered - total_value) > 0.005:
        gap = sorted(figures.unavailable)
        base = (
            f"on {format_money(covered, currency)} of the "
            f"{format_money(total_value, currency)} total; "
            f"{', '.join(gap)} {'has' if len(gap) == 1 else 'have'} no trailing dividend data"
        )
    else:
        base = f"on the full {format_money(covered or 0.0, currency)} total"
    return (
        f"Trailing annual dividends: {format_money(figures.total_annual_dividends, currency)}  "
        f"Dividend yield: {figures.dividend_yield:.4f} ({base})"
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


def print_user_portfolio(
    holdings: HoldingsStats,
    path: str,
    risk_free_rate_origin: str | None = None,
    *,
    heading: str | None = None,
    window_origin: str | None = None,
) -> None:
    """Human-readable rendering of the user's OWN saved portfolio (see
    `src/flow/user_portfolio.py` and `src/optimizer/holdings.py`) - what is
    held, what it is worth, and its annualized return, volatility and
    Sharpe ratio.

    The file it came from is named in the header, because these figures
    describe stored state rather than anything the current command line
    said, and a reader who disagrees with them needs to know which file to
    edit.

    `heading` replaces that header outright, and exists for the one caller
    whose figures describe NO stored state: a what-if variant
    (`src/flow/holdings_cli.py`'s `whatif`). Naming a file there would be a
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
    `src/dataset/holdings_cache.py`), so the total can legitimately be weeks
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


def print_pipeline_result(
    result: dict,
    risk_free_rate_origin: str | None = None,
    portfolio_value: float | None = None,
) -> None:
    """Human-readable rendering of one `run_pipeline_against` result dict.

    `risk_free_rate_origin` is a parameter rather than a key inside `result`
    on purpose: it is a display string, and `run_pipeline_against` lives in
    the orchestration layer, which should not acquire resolution's vocabulary
    just so a parenthetical can ride along. `portfolio_value` is a parameter
    for a plainer reason: it is what `--value` allocated, the orchestration
    layer never puts it in the result dict, and it is what turns a dividend
    yield into money.
    """
    print(f"Mode: {result['mode']}  Rebalance date: {result['rebalance_date']}  "
          f"Objective: {result['objective']}  Selection: {result['selection']}")

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

    print_weights_and_allocation(
        result["stats"], result["allocation"], result["objective"], result["currency"],
        benchmark=result.get("benchmark"), risk_free_rate_origin=risk_free_rate_origin,
        portfolio_value=portfolio_value,
    )


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

    while True:
        choice = input(
            "\nEdit candidate pool? [a]dd tickers / [r]emove tickers / [d]one: "
        ).strip().lower()

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
    risk_free_rate: float = settings.risk_free_rate,
    currency: str = DEFAULT_CURRENCY,
    benchmark: BenchmarkSource | None = None,
    allow_benchmark_fetch: bool = True,
    risk_free_rate_origin: str | None = None,
) -> None:
    """Prompt in a loop for add/remove/objective/target-return/benchmark/
    finish; each
    edit recomputes weights and allocation against the current `candidates`
    and reprints them. Returns when the user chooses to finish.

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

    `benchmark` is carried for the same reason and re-narrowed to the
    portfolio's window on every recompute, so the comparison line follows
    each edit rather than describing the run as it was before it. `[b]`
    changes it, prompting until an answer is usable; a refused or blank
    answer keeps the benchmark already in force, the same keep-what-you-had
    treatment `[o]bjective` gives a rejected edit. A benchmark chosen here is
    always an explicit choice, so for `user_provided` it is persisted.
    """
    while True:
        choice = input(
            "\nEdit candidates? [a]dd tickers / [r]emove tickers / [o]bjective / "
            "[t]arget-return / [d]ividend / [b]enchmark / [f]inish: "
        ).strip().lower()

        if choice in ("", "f", "finish"):
            return

        previous_candidates, previous_objective, previous_target, previous_dividend_floor = (
            candidates,
            objective,
            target_annual_return,
            dividend_floor,
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
                dividend_floor=dividend_floor,
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
                "Keeping the previous candidates, objective, target return, and dividend floor."
            )
            candidates, objective, target_annual_return, dividend_floor = (
                previous_candidates,
                previous_objective,
                previous_target,
                previous_dividend_floor,
            )
            continue

        if selection == "user_provided" and choice in ("a", "add", "r", "remove"):
            save_candidate_pool(candidates, path=memory_path, currency=currency)

        print_weights_and_allocation(
            stats, allocation, objective, currency, portfolio_value=portfolio_value,
            benchmark=benchmark_stats_for_window(
                benchmark, stats.returns_window_start, stats.returns_window_end, risk_free_rate
            ),
            risk_free_rate_origin=risk_free_rate_origin,
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Run plans/06_interactive_flow.md's interactive pipeline.")
    parser.add_argument("--date", required=True, help="Rebalance date, YYYY-MM-DD, or 'today' for live mode.")
    parser.add_argument("--objective", required=True, choices=VALID_OBJECTIVES)
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
        default=DEFAULT_TARGET_ANNUAL_RETURN,
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
    parser.add_argument("--db-path", default="data/portfolio.duckdb")
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
        "--no-benchmark-fetch",
        action="store_true",
        help="Take the benchmark only from returns this session's database already holds, "
             "never by fetching. Keeps a backtest-window run entirely offline, at the cost of "
             "reporting the benchmark as unavailable when the cache does not contain it.",
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

    benchmark_enabled = args.benchmark != BENCHMARK_DISABLED
    benchmark_override = args.benchmark if benchmark_enabled else None

    with open_pipeline_session(rebalance_date, args.selection, args.db_path) as (session_db_path, mode):
        candidates = None
        currency = DEFAULT_CURRENCY
        if args.selection == "user_provided":
            candidates, currency = _run_user_provided_confirm_loop(
                load_all_pools(args.memory_path),
                rebalance_date,
                session_db_path,
                memory_path=args.memory_path,
                currency=args.currency,
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
        )
        print_pipeline_result(
            result, risk_free_rate_origin=resolved_rate.origin, portfolio_value=args.value
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
        _remember_risk_free_rate(resolved_rate, currency, args.rates_path)

        # Printed once, here, rather than from inside
        # `print_weights_and_allocation`: that function reprints the pool's
        # currency, window and benchmark after every interactive edit
        # because those are what an edit changes, and an edit to the
        # candidate pool changes nothing about what the user owns. Repeating
        # three unchanged numbers after every keystroke would be noise, and
        # would imply a relationship between the edit and the holdings that
        # does not exist.
        if not args.no_holdings:
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

        _run_edit_loop(
            result["scan_detail"]["candidates"], args.objective, args.value, rebalance_date, session_db_path,
            selection=args.selection, memory_path=args.memory_path,
            target_annual_return=args.target_return, risk_free_rate=args.risk_free_rate,
            dividend_floor=dividend_floor,
            currency=currency, benchmark=benchmark,
            allow_benchmark_fetch=not args.no_benchmark_fetch,
            risk_free_rate_origin=resolved_rate.origin,
        )


if __name__ == "__main__":
    main()
