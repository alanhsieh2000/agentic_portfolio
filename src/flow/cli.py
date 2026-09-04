"""CLI entry point for `plans/06_interactive_flow.md`'s backtest mode:
`uv run python -m src.flow.cli --date YYYY-MM-DD --objective GMV|MV|MSR
--value 100000 [--selection llm_s_only|llm_f_only|llm_s_and_f]`.

Prints `run_pipeline`'s bundled result - LLM-S's rule (if run), the
scanner's branch and candidate list, the weights, and the share allocation
- so a person can see why each ticker is or is not a candidate, not just
the final number of shares.

Live mode (`--date today`) and the interactive add/remove/finish editing
loop are `plans/06_interactive_flow.md`'s Progress items 19 and 20,
not yet implemented; this entry point only drives backtest mode's
one-shot, non-interactive display for now.
"""

from __future__ import annotations

import argparse
from datetime import date

from src.flow.interactive import VALID_SELECTIONS, run_pipeline
from src.optimizer.portfolio import VALID_OBJECTIVES


def print_pipeline_result(result: dict) -> None:
    """Human-readable rendering of one `run_pipeline` result dict."""
    print(f"Rebalance date: {result['rebalance_date']}  Objective: {result['objective']}  "
          f"Selection: {result['selection']}")

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

    weights = result["weights"]
    print("\nWeights:")
    for ticker, weight in sorted(weights.items(), key=lambda kv: -kv[1]):
        if weight > 0:
            print(f"  {ticker}: {weight:.4f}")

    shares, leftover_cash = result["allocation"]
    print("\nShare allocation:")
    for ticker, count in sorted(shares.items()):
        print(f"  {ticker}: {count}")
    print(f"Leftover cash: ${leftover_cash:.2f}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run plans/06_interactive_flow.md's backtest-mode pipeline.")
    parser.add_argument("--date", required=True, help="Rebalance date, YYYY-MM-DD (backtest mode only for now).")
    parser.add_argument("--objective", required=True, choices=VALID_OBJECTIVES)
    parser.add_argument("--value", required=True, type=float, help="Total portfolio value to allocate.")
    parser.add_argument("--selection", default="llm_s_only", choices=VALID_SELECTIONS)
    parser.add_argument("--db-path", default="data/portfolio.duckdb")
    args = parser.parse_args()

    rebalance_date = date.fromisoformat(args.date)
    result = run_pipeline(
        rebalance_date=rebalance_date,
        objective=args.objective,
        portfolio_value=args.value,
        selection=args.selection,
        db_path=args.db_path,
    )
    print_pipeline_result(result)


if __name__ == "__main__":
    main()
