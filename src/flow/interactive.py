"""Backtest-mode orchestration for `plans/06_interactive_flow.md`: run
LLM-S, LLM-F, the scanner, and the optimizer in sequence for one historical
rebalance date and bundle every intermediate result for display, reusing
plans 1 through 5's functions unchanged.

Live mode (a `rebalance_date` outside the stored 2020-01-01..2024-04-30
window, requiring a freshly-fetched membership/price/factor/news snapshot
rather than a read from `data/portfolio.duckdb`'s cached historical
tables) is `plans/06_interactive_flow.md`'s Progress item 19, not yet
implemented - `run_pipeline` raises `NotImplementedError` for any such
date rather than silently mishandling it. The interactive candidate-editing
loop (`edit_candidates`) is that plan's Progress item 20, also not yet
implemented.
"""

from __future__ import annotations

from datetime import date

from src.agents.llm_f_signals import screen_month
from src.agents.llm_s import generate_rule
from src.agents.llm_s_signals import screen
from src.config.settings import settings
from src.optimizer.portfolio import allocate_shares, compute_weights, load_latest_prices, load_returns_matrix
from src.scanner.candidate_scanner import scan_with_detail

VALID_SELECTIONS = ("llm_s_only", "llm_f_only", "llm_s_and_f")


def _is_backtest_date(rebalance_date: date) -> bool:
    """Whether `rebalance_date` falls within the stored 2020-2024 window
    (`settings.rebalance_start`..`settings.rebalance_end`) that
    `data/portfolio.duckdb`'s historical tables actually cover.
    """
    start = date.fromisoformat(settings.rebalance_start)
    end = date.fromisoformat(settings.rebalance_end)
    return start <= rebalance_date <= end


def run_pipeline(
    rebalance_date: date,
    objective: str,
    portfolio_value: float,
    selection: str = "llm_s_only",
    db_path: str = "data/portfolio.duckdb",
) -> dict:
    """Backtest mode's full pipeline for one historical `rebalance_date`:
    LLM-S (`generate_rule` + `screen`) and/or LLM-F (`screen_month`) per
    `selection`, the scanner (`scan_with_detail`), and the optimizer
    (`compute_weights` + `allocate_shares`).

    `selection` (one of `"llm_s_only"` (the default), `"llm_f_only"`,
    `"llm_s_and_f"`) controls which agent(s) actually run, per README's
    Backtest Mode Stage 1 default-selection sentence and
    `plans/08_consistency_review.md` finding 9: the skipped agent's call is
    never made (not merely its result discarded), since README frames
    LLM-F evaluation as "expensive". Raises `ValueError` for any other
    `selection` value.

    Live mode (`rebalance_date` outside the stored 2020-2024 window) raises
    `NotImplementedError` - see this module's docstring.

    Returns a dict bundling every intermediate result: `rule` (LLM-S's
    `ScreeningRule`, `None` if skipped), `llm_s_signals`/`llm_f_signals`
    (each a `ticker`/`signal` DataFrame, `None` if skipped), `scan_detail`
    (`scan_with_detail`'s full output, including the branch taken),
    `weights`, and `allocation` (the `(shares_per_ticker, leftover_cash)`
    tuple from `allocate_shares`) - so a CLI layer can display why each
    ticker is or is not a candidate, not just the final share counts.
    """
    if selection not in VALID_SELECTIONS:
        raise ValueError(f"selection must be one of {VALID_SELECTIONS}, got {selection!r}")

    if not _is_backtest_date(rebalance_date):
        raise NotImplementedError(
            f"live mode is not yet implemented (plans/06_interactive_flow.md Progress item 19); "
            f"rebalance_date={rebalance_date} falls outside the stored "
            f"{settings.rebalance_start}..{settings.rebalance_end} backtest window"
        )

    rule = None
    llm_s_signals = None
    if selection in ("llm_s_only", "llm_s_and_f"):
        rule = generate_rule(rebalance_date.year, db_path=db_path)
        llm_s_signals = screen(rule, rebalance_date, db_path=db_path)

    llm_f_signals = None
    if selection in ("llm_f_only", "llm_s_and_f"):
        llm_f_signals = screen_month(rebalance_date.year, rebalance_date.month, db_path=db_path)

    scan_detail = scan_with_detail(llm_s_signals, llm_f_signals)
    candidates = scan_detail["candidates"]

    returns_matrix = load_returns_matrix(candidates, as_of=rebalance_date, db_path=db_path)
    weights = compute_weights(returns_matrix, objective)

    latest_prices = load_latest_prices(list(weights.keys()), as_of=rebalance_date, db_path=db_path)
    allocation = allocate_shares(weights, latest_prices, portfolio_value)

    return {
        "mode": "backtest",
        "rebalance_date": rebalance_date,
        "objective": objective,
        "selection": selection,
        "rule": rule,
        "llm_s_signals": llm_s_signals,
        "llm_f_signals": llm_f_signals,
        "scan_detail": scan_detail,
        "weights": weights,
        "allocation": allocation,
    }
