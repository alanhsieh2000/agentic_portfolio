"""Backtest- and live-mode orchestration for `plans/06_interactive_flow.md`:
run LLM-S, LLM-F, the scanner, and the optimizer in sequence for one
rebalance date and bundle every intermediate result for display, reusing
plans 1 through 5's functions unchanged.

Backtest mode (a `rebalance_date` within the stored 2020-01-01..2024-04-30
window) reads directly from `data/portfolio.duckdb`'s cached historical
tables. Live mode (any other date, e.g. "today") builds a fresh,
throwaway snapshot instead - see `src/flow/live.py`'s `build_live_snapshot`
- and runs the exact same downstream sequence against it. The interactive
candidate-editing loop (`edit_candidates`) is this plan's Progress item
20, not yet implemented.
"""

from __future__ import annotations

from datetime import date

from src.agents.llm_f_signals import screen_month
from src.agents.llm_s import generate_rule
from src.agents.llm_s_signals import screen
from src.config.settings import settings
from src.flow.live import build_live_snapshot
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


def _run_pipeline_against(
    rebalance_date: date,
    objective: str,
    portfolio_value: float,
    selection: str,
    db_path: str,
    mode: str,
) -> dict:
    """The shared sequence behind both modes: LLM-S (`generate_rule` +
    `screen`) and/or LLM-F (`screen_month`) per `selection`, the scanner
    (`scan_with_detail`), and the optimizer (`compute_weights` +
    `allocate_shares`) - all reading `db_path`, whichever database that is.
    """
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
        "mode": mode,
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


def run_pipeline(
    rebalance_date: date,
    objective: str,
    portfolio_value: float,
    selection: str = "llm_s_only",
    db_path: str = "data/portfolio.duckdb",
) -> dict:
    """Run the full pipeline for one `rebalance_date`.

    `selection` (one of `"llm_s_only"` (the default), `"llm_f_only"`,
    `"llm_s_and_f"`) controls which agent(s) actually run, per README's
    Backtest Mode Stage 1 default-selection sentence and
    `plans/08_consistency_review.md` finding 9: the skipped agent's call is
    never made (not merely its result discarded), since README frames
    LLM-F evaluation as "expensive". Raises `ValueError` for any other
    `selection` value.

    Backtest mode (`rebalance_date` within the stored 2020-2024 window)
    reads `db_path`'s cached historical tables directly. Live mode (any
    other date) first builds a fresh, throwaway snapshot via
    `build_live_snapshot` (a real Wikipedia/yfinance/SEC EDGAR fetch, not a
    read from `db_path`) and runs the identical downstream sequence
    against that instead - `db_path` is used there only as the source of
    the static `news_articles_hf` archive `screen_month` needs.

    Returns a dict bundling every intermediate result: `mode`
    (`"backtest"` or `"live"`), `rule` (LLM-S's `ScreeningRule`, `None` if
    skipped), `llm_s_signals`/`llm_f_signals` (each a `ticker`/`signal`
    DataFrame, `None` if skipped), `scan_detail` (`scan_with_detail`'s full
    output, including the branch taken), `weights`, and `allocation` (the
    `(shares_per_ticker, leftover_cash)` tuple from `allocate_shares`) - so
    a CLI layer can display why each ticker is or is not a candidate, not
    just the final share counts.
    """
    if selection not in VALID_SELECTIONS:
        raise ValueError(f"selection must be one of {VALID_SELECTIONS}, got {selection!r}")

    if _is_backtest_date(rebalance_date):
        return _run_pipeline_against(rebalance_date, objective, portfolio_value, selection, db_path, mode="backtest")

    with build_live_snapshot(rebalance_date, selection, source_db_path=db_path) as live_db_path:
        return _run_pipeline_against(rebalance_date, objective, portfolio_value, selection, live_db_path, mode="live")
