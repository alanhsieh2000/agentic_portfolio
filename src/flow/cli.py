"""CLI entry point for `plans/06_interactive_flow.md`'s backtest and live
modes: `uv run python -m src.flow.cli --date YYYY-MM-DD|today --objective
GMV|MV|MSR --value 100000 [--selection llm_s_only|llm_f_only|llm_s_and_f]`.

Prints the initial pipeline result - which mode ran, LLM-S's rule (if
run), the scanner's branch and candidate list, the weights, and the share
allocation - so a person can see why each ticker is or is not a candidate,
not just the final number of shares. Then enters an interactive loop
letting the user add/remove candidate tickers or change the objective;
each edit re-runs only `compute_weights_and_allocation` (never LLM-S or
LLM-F again - the user is overriding the agents' already-given
recommendation, not asking them to reconsider it) and reprints the
updated candidates, weights, and allocation. `open_pipeline_session` keeps
live mode's throwaway snapshot alive for this entire loop, not just the
initial run.
"""

from __future__ import annotations

import argparse
from datetime import date

from src.flow.interactive import (
    VALID_SELECTIONS,
    compute_weights_and_allocation,
    edit_candidates,
    open_pipeline_session,
    run_pipeline_against,
)
from src.optimizer.portfolio import VALID_OBJECTIVES


def _parse_date(value: str) -> date:
    return date.today() if value == "today" else date.fromisoformat(value)


def print_weights_and_allocation(weights: dict[str, float], allocation: tuple[dict[str, int], float]) -> None:
    """Human-readable rendering of one `compute_weights_and_allocation` result."""
    print("\nWeights:")
    for ticker, weight in sorted(weights.items(), key=lambda kv: -kv[1]):
        if weight > 0:
            print(f"  {ticker}: {weight:.4f}")

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

    print_weights_and_allocation(result["weights"], result["allocation"])


def _run_edit_loop(
    candidates: list[str],
    objective: str,
    portfolio_value: float,
    rebalance_date: date,
    db_path: str,
) -> None:
    """Prompt in a loop for add/remove/objective/finish; each edit
    recomputes weights and allocation against the current `candidates`
    and reprints them. Returns when the user chooses to finish.
    """
    while True:
        choice = input(
            "\nEdit candidates? [a]dd tickers / [r]emove tickers / [o]bjective / [f]inish: "
        ).strip().lower()

        if choice in ("", "f", "finish"):
            return

        if choice in ("a", "add"):
            raw = input("Ticker(s) to add (space-separated): ").strip().upper()
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
        else:
            print(f"Unrecognized choice {choice!r}.")
            continue

        if not candidates:
            print("Candidate list is empty; ignoring this edit and keeping the previous list.")
            continue

        print(f"\nCandidates ({len(candidates)}): {', '.join(candidates)}")
        weights, allocation = compute_weights_and_allocation(candidates, objective, portfolio_value, rebalance_date, db_path)
        print_weights_and_allocation(weights, allocation)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run plans/06_interactive_flow.md's interactive pipeline.")
    parser.add_argument("--date", required=True, help="Rebalance date, YYYY-MM-DD, or 'today' for live mode.")
    parser.add_argument("--objective", required=True, choices=VALID_OBJECTIVES)
    parser.add_argument("--value", required=True, type=float, help="Total portfolio value to allocate.")
    parser.add_argument("--selection", default="llm_s_only", choices=VALID_SELECTIONS)
    parser.add_argument("--db-path", default="data/portfolio.duckdb")
    args = parser.parse_args()

    rebalance_date = _parse_date(args.date)

    with open_pipeline_session(rebalance_date, args.selection, args.db_path) as (session_db_path, mode):
        result = run_pipeline_against(
            rebalance_date, args.objective, args.value, args.selection, session_db_path, mode
        )
        print_pipeline_result(result)

        _run_edit_loop(
            result["scan_detail"]["candidates"], args.objective, args.value, rebalance_date, session_db_path
        )


if __name__ == "__main__":
    main()
