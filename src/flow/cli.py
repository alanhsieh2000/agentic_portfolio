"""CLI entry point for `plans/06_interactive_flow.md`'s backtest and live
modes: `uv run python -m src.flow.cli --date YYYY-MM-DD|today --objective
GMV|MV|MSR --value 100000 [--target-return 0.12] [--risk-free-rate 0.02]
[--selection llm_s_only|llm_f_only|llm_s_and_f|user_provided]`.

Prints the initial pipeline result - which mode ran, LLM-S's rule (if
run), the scanner's branch and candidate list, the weights, the expected
return/volatility behind them, the portfolio's own expected return,
volatility and Sharpe ratio, and the share allocation - so a person can
see why each ticker is or is not a candidate and what the optimizer
expected of the ones it kept, not just the final number of shares. Then
enters an interactive loop letting the user add/remove candidate tickers,
change the objective, or change MV's target return; each edit re-runs only
`compute_weights_and_allocation` (never LLM-S or LLM-F again - the user is
overriding the agents' already-given recommendation, not asking them to
reconsider it) and reprints the updated candidates, weights, and
allocation. `open_pipeline_session` keeps live mode's throwaway snapshot
alive for this entire loop, not just the initial run.

`--selection user_provided` runs neither agent. It instead shows the
candidate pool persisted at `--memory-path` (`memory/candidates.json` by
default) and prompts add/remove until the user confirms it, validating each
added ticker against Yahoo Finance so a typo is named rather than silently
carried into the optimizer - then feeds the confirmed pool through the same
optimizer and post-run edit loop every other selection uses.
"""

from __future__ import annotations

import argparse
from datetime import date

from src.config.settings import settings
from src.dataset.ticker_ingestion import validate_and_ingest_tickers
from src.flow.candidate_memory import DEFAULT_CANDIDATES_PATH, load_candidate_pool, save_candidate_pool
from src.flow.interactive import (
    VALID_SELECTIONS,
    compute_weights_and_allocation,
    edit_candidates,
    open_pipeline_session,
    run_pipeline_against,
    validate_and_edit_candidates,
)
from src.optimizer.portfolio import DEFAULT_TARGET_ANNUAL_RETURN, PortfolioStats, VALID_OBJECTIVES


def _parse_date(value: str) -> date:
    return date.today() if value == "today" else date.fromisoformat(value)


def print_weights_and_allocation(
    stats: PortfolioStats,
    allocation: tuple[dict[str, int], float],
    objective: str,
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
    """
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
    print(f"Risk-free rate used: {stats.risk_free_rate:.4f}")
    if objective == "MV":
        print(f"Target annual return: {stats.target_annual_return:.4f}")
    else:
        print(f"Target annual return: n/a (objective is {objective}, not MV)")

    shares, leftover_cash = allocation
    print("\nShare allocation:")
    for ticker, count in sorted(shares.items()):
        print(f"  {ticker}: {count}")
    print(f"Leftover cash: ${leftover_cash:.2f}")


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

    print_weights_and_allocation(result["stats"], result["allocation"], result["objective"])


def _print_add_outcome(valid_added: list[str], invalid: dict[str, str]) -> None:
    """Report which of the tickers just typed resolved and which did not -
    the whole point of validating an add is that the good tickers in a
    batch still land while the bad ones are named.
    """
    if valid_added:
        print(f"Added: {', '.join(valid_added)}.")
    if invalid:
        print(f"Ignored (not found): {', '.join(sorted(invalid))}.")


def _run_user_provided_confirm_loop(
    initial_pool: list[str],
    rebalance_date: date,
    db_path: str,
    memory_path: str = DEFAULT_CANDIDATES_PATH,
) -> list[str]:
    """Show the persisted candidate pool, then prompt in a loop for
    add/remove until the user confirms they are done, and return the
    confirmed pool - the `user_provided` selection's replacement for the
    agents that choose candidates in every other selection.

    The pool is persisted exactly once, when the user confirms: an
    in-progress edit is never written to `memory_path`. `initial_pool` is
    re-validated (and re-ingested into this session's `db_path`, which is a
    fresh scratch database every run) before anything is shown, so a
    previously-saved ticker that no longer resolves is dropped with a
    warning rather than breaking the optimizer later.
    """
    pool, invalid = validate_and_ingest_tickers(initial_pool, rebalance_date, db_path)
    if invalid:
        print(f"Warning: dropping previously-saved ticker(s) that no longer resolve: {', '.join(sorted(invalid))}.")

    print(f"\nCurrent candidate pool ({len(pool)}): {', '.join(pool) if pool else '(empty)'}")

    while True:
        choice = input(
            "\nEdit candidate pool? [a]dd tickers / [r]emove tickers / [d]one: "
        ).strip().lower()

        if choice in ("", "d", "done"):
            if not pool:
                print("Candidate pool is empty; add at least one ticker before finishing.")
                continue
            save_candidate_pool(pool, path=memory_path)
            print(f"Saved {len(pool)} ticker(s) to {memory_path}.")
            return pool

        previous_pool = pool
        if choice in ("a", "add"):
            raw = input("Ticker(s) to add (space-separated): ").strip().upper()
            pool, valid_added, invalid = validate_and_edit_candidates(
                pool, add=raw.split(), remove=[], as_of=rebalance_date, db_path=db_path
            )
            _print_add_outcome(valid_added, invalid)
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
) -> None:
    """Prompt in a loop for add/remove/objective/target-return/finish; each
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
    """
    while True:
        choice = input(
            "\nEdit candidates? [a]dd tickers / [r]emove tickers / [o]bjective / "
            "[t]arget-return / [f]inish: "
        ).strip().lower()

        if choice in ("", "f", "finish"):
            return

        previous_candidates, previous_objective, previous_target = candidates, objective, target_annual_return
        if choice in ("a", "add"):
            raw = input("Ticker(s) to add (space-separated): ").strip().upper()
            if selection == "user_provided":
                candidates, valid_added, invalid = validate_and_edit_candidates(
                    candidates, add=raw.split(), remove=[], as_of=rebalance_date, db_path=db_path
                )
                _print_add_outcome(valid_added, invalid)
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
            # return no combination of these candidates can reach. That is a
            # rejected edit, not a failed session: this loop holds live mode's
            # only snapshot open, so letting it escape would throw away the
            # fetched data and the user's confirmed pool along with it.
            print(f"Cannot optimize that edit: {e}")
            print("Keeping the previous candidates, objective, and target return.")
            candidates, objective, target_annual_return = previous_candidates, previous_objective, previous_target
            continue

        if selection == "user_provided" and choice in ("a", "add", "r", "remove"):
            save_candidate_pool(candidates, path=memory_path)

        print_weights_and_allocation(stats, allocation, objective)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run plans/06_interactive_flow.md's interactive pipeline.")
    parser.add_argument("--date", required=True, help="Rebalance date, YYYY-MM-DD, or 'today' for live mode.")
    parser.add_argument("--objective", required=True, choices=VALID_OBJECTIVES)
    parser.add_argument("--value", required=True, type=float, help="Total portfolio value to allocate.")
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
        "--memory-path",
        default=DEFAULT_CANDIDATES_PATH,
        help="Where the user_provided selection's candidate pool is persisted.",
    )
    args = parser.parse_args()

    rebalance_date = _parse_date(args.date)

    with open_pipeline_session(rebalance_date, args.selection, args.db_path) as (session_db_path, mode):
        candidates = None
        if args.selection == "user_provided":
            candidates = _run_user_provided_confirm_loop(
                load_candidate_pool(args.memory_path),
                rebalance_date,
                session_db_path,
                memory_path=args.memory_path,
            )

        result = run_pipeline_against(
            rebalance_date, args.objective, args.value, args.selection, session_db_path, mode,
            candidates=candidates, target_annual_return=args.target_return,
            risk_free_rate=args.risk_free_rate,
        )
        print_pipeline_result(result)

        _run_edit_loop(
            result["scan_detail"]["candidates"], args.objective, args.value, rebalance_date, session_db_path,
            selection=args.selection, memory_path=args.memory_path,
            target_annual_return=args.target_return, risk_free_rate=args.risk_free_rate,
        )


if __name__ == "__main__":
    main()
