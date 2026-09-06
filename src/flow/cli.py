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
several are saved, the user is asked which to resume.
"""

from __future__ import annotations

import argparse
from datetime import date

from src.config.settings import settings
from src.dataset.ticker_currency import DEFAULT_CURRENCY, group_by_currency
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
from src.flow.user_portfolio import DEFAULT_PORTFOLIO_PATH, load_portfolio
from src.optimizer.benchmark import (
    BenchmarkSource,
    BenchmarkStats,
    benchmark_stats_for_window,
    resolve_benchmark_ticker,
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


def print_weights_and_allocation(
    stats: PortfolioStats,
    allocation: tuple[dict[str, int], float],
    objective: str,
    currency: str = DEFAULT_CURRENCY,
    benchmark: BenchmarkStats | None = None,
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

    print(f"\nPortfolio expected return: {stats.portfolio_expected_return:.4f}  "
          f"Portfolio volatility: {stats.portfolio_volatility:.4f}  "
          f"Portfolio Sharpe: {stats.portfolio_sharpe:.4f}")
    if benchmark is not None:
        print(format_benchmark(benchmark, stats.returns_window_months))
    print(f"Risk-free rate used: {stats.risk_free_rate:.4f}")
    if objective == "MV":
        print(f"Target annual return: {stats.target_annual_return:.4f}")
    else:
        print(f"Target annual return: n/a (objective is {objective}, not MV)")

    shares, leftover_cash = allocation
    print("\nShare allocation:")
    for ticker, count in sorted(shares.items()):
        print(f"  {ticker}: {count}")
    print(f"Leftover cash: {format_money(leftover_cash, currency)}")


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


def print_user_portfolio(holdings: HoldingsStats, path: str) -> None:
    """Human-readable rendering of the user's OWN saved portfolio (see
    `src/flow/user_portfolio.py` and `src/optimizer/holdings.py`) - what is
    held, what it is worth, and its annualized return, volatility and
    Sharpe ratio.

    The file it came from is named in the header, because these figures
    describe stored state rather than anything the current command line
    said, and a reader who disagrees with them needs to know which file to
    edit.

    Every position is listed, including one excluded from the figures, since
    this block doubles as the record of what the user owns; an excluded
    holding shows `weight n/a` and is explained by name underneath. The
    returns window and the risk-free rate are printed whenever figures are,
    never only on request: the same holdings measured over a different
    window or against a different rate are different numbers, and a figure
    whose derivation is not stated beside it invites being compared with one
    derived differently.

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
        print(f"\nYour portfolio ({currency}): n/a - {holdings.unavailable_reason}")
        return

    print(f"\nYour portfolio ({currency}), from {path}:")
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
              f"{value_text}  {weight_text}")

    if holdings.total_value is not None:
        print(f"Total value: {format_money(holdings.total_value, currency)}")

    if holdings.unavailable_reason is not None:
        print(f"Figures: n/a - {holdings.unavailable_reason}")
    else:
        print(f"Returns window: {holdings.window_start} to {holdings.window_end} "
              f"({holdings.window_months} month(s) of monthly returns)")
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
        print(f"Risk-free rate used: {holdings.risk_free_rate:.4f}")

    if holdings.excluded:
        print("Excluded from the figures:")
        for ticker in sorted(holdings.excluded):
            share = ""
            value = holdings.market_values.get(ticker)
            if value is not None and holdings.total_value:
                share = f" ({value / holdings.total_value:.1%} of total value)"
            print(f"  {ticker}: {holdings.excluded[ticker]}{share}")


def print_pipeline_result(result: dict) -> None:
    """Human-readable rendering of one `run_pipeline_against` result dict."""
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
        benchmark=result.get("benchmark"),
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


def _resolve_mixed_persisted_pool(pool: list[str], currencies: dict[str, str]) -> tuple[list[str], str]:
    """Report a saved pool that spans several currencies and ask which one to
    keep, returning `(kept_tickers, currency)`.

    A pool saved before currencies were recorded can legitimately be mixed,
    and so can one whose ticker changed listing. The machine genuinely
    cannot decide which currency was intended - keeping the largest group
    would just be guessing quietly - so this asks, which is safe because
    this loop is already interactive and the pool is the person's own.
    Nothing reaches disk until they confirm at the `[d]one` prompt.
    """
    groups = group_by_currency(pool, currencies)
    print("\nWarning: the saved candidate pool mixes currencies, which one portfolio cannot do:")
    for currency, tickers in groups.items():
        print(f"  {currency}: {', '.join(tickers)}")

    while True:
        chosen = input(f"Keep which currency? ({'/'.join(groups)}): ").strip().upper()
        if chosen in groups:
            dropped = sorted(t for c, ts in groups.items() if c != chosen for t in ts)
            print(f"Keeping {chosen}; dropping {', '.join(dropped)}.")
            return groups[chosen], chosen
        print(f"Unrecognized currency {chosen!r}.")


def _choose_pool_to_resume(
    pools: dict[str, list[str]], benchmarks: dict[str, str | None] | None = None
) -> tuple[list[str], str | None]:
    """Pick which saved pool this session works on, returning
    `(tickers, currency)`.

    `memory/candidates.json` holds one pool per currency, since a portfolio
    can only hold one, so with anything saved there is nothing but the
    person's intent to go on and this asks. A `None` currency means "start a
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
    """
    if not pools:
        return [], None

    benchmarks = benchmarks or {}
    print("\nSaved candidate pools:")
    for currency, tickers in sorted(pools.items()):
        benchmark = benchmarks.get(currency)
        count = f"{len(tickers)}, benchmark {benchmark}" if benchmark else str(len(tickers))
        print(f"  {currency} ({count}): {', '.join(tickers)}")

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
) -> tuple[list[str], str]:
    """Show the persisted candidate pool, then prompt in a loop for
    add/remove until the user confirms they are done, and return the
    confirmed pool together with the currency it is priced in - the
    `user_provided` selection's replacement for the agents that choose
    candidates in every other selection.

    `initial_pools` is every pool saved at `memory_path`, keyed by currency;
    `_choose_pool_to_resume` settles which one this session works on. The
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
    are measured against it.
    """
    initial_pool, resumed_currency = _choose_pool_to_resume(
        initial_pools, load_pool_benchmarks(memory_path)
    )

    pool, invalid, currencies = validate_and_ingest_tickers(initial_pool, rebalance_date, db_path)
    if invalid:
        print(f"Warning: dropping previously-saved ticker(s) that no longer resolve: {', '.join(sorted(invalid))}.")

    pool_currency: str | None = resumed_currency
    if pool:
        groups = group_by_currency(pool, currencies)
        if len(groups) > 1:
            pool, pool_currency = _resolve_mixed_persisted_pool(pool, currencies)
        else:
            pool_currency = next(iter(groups))

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

        previous_pool = pool
        if choice in ("a", "add"):
            raw = input("Ticker(s) to add (space-separated): ").strip().upper()
            edit = validate_and_edit_candidates(
                pool, add=raw.split(), remove=[], as_of=rebalance_date, db_path=db_path,
                pool_currency=pool_currency,
            )
            pool, pool_currency = edit.pool, edit.pool_currency
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
            pool = previous_pool
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
    risk_free_rate: float = settings.risk_free_rate,
    currency: str = DEFAULT_CURRENCY,
    benchmark: BenchmarkSource | None = None,
    allow_benchmark_fetch: bool = True,
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
    every recompute has to use the rate the command line supplied, or a
    `--risk-free-rate` would apply to the initial run and then silently
    revert to the configured default on the first edit.

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
            "[t]arget-return / [b]enchmark / [f]inish: "
        ).strip().lower()

        if choice in ("", "f", "finish"):
            return

        previous_candidates, previous_objective, previous_target = candidates, objective, target_annual_return
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
            print("Keeping the previous candidates, objective, and target return.")
            candidates, objective, target_annual_return = previous_candidates, previous_objective, previous_target
            continue

        if selection == "user_provided" and choice in ("a", "add", "r", "remove"):
            save_candidate_pool(candidates, path=memory_path, currency=currency)

        print_weights_and_allocation(
            stats, allocation, objective, currency,
            benchmark=benchmark_stats_for_window(
                benchmark, stats.returns_window_start, stats.returns_window_end, risk_free_rate
            ),
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
        default=settings.risk_free_rate,
        help="Rate --objective MSR maximizes its Sharpe ratio against, and that every "
             "objective's reported Sharpe ratio is measured against. Defaults to the "
             "configured RISK_FREE_RATE.",
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
             "when several pools are saved you are asked which to resume.",
    )
    parser.add_argument(
        "--holdings-path",
        default=DEFAULT_PORTFOLIO_PATH,
        help="Which file the portfolio you actually hold is read from, for the holdings block at "
             "the end of the report. Maintained by 'uv run portfolio-holdings'; one file holds "
             "one portfolio per currency, and the one matching this run's currency is reported.",
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
        help="Measure the holdings only from returns this session's database already holds, "
             "never by fetching. Keeps a backtest-window run entirely offline, at the cost of "
             "reporting the holdings as unmeasurable when the cache does not contain them.",
    )
    args = parser.parse_args()

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
            )

        # Settled after the confirm loop, never before: an empty pool has no
        # currency until the first ticker is added, and the benchmark has to
        # be measured against the currency the pool actually ended up in.
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
        )
        print_pipeline_result(result)

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
                ),
                args.holdings_path,
            )

        _run_edit_loop(
            result["scan_detail"]["candidates"], args.objective, args.value, rebalance_date, session_db_path,
            selection=args.selection, memory_path=args.memory_path,
            target_annual_return=args.target_return, risk_free_rate=args.risk_free_rate,
            currency=currency, benchmark=benchmark,
            allow_benchmark_fetch=not args.no_benchmark_fetch,
        )


if __name__ == "__main__":
    main()
