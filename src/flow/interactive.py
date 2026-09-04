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
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date

from src.agents.llm_f_signals import screen_month
from src.agents.llm_s import generate_rule
from src.agents.llm_s_schema import ScreeningRule
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


@contextmanager
def open_pipeline_session(rebalance_date: date, selection: str, db_path: str = "data/portfolio.duckdb"):
    """Yield `(effective_db_path, mode)` for `rebalance_date`: `(db_path,
    "backtest")` unchanged for a date within the stored window, or a
    freshly-built live snapshot's temp path and `"live"` for any other
    date - kept alive for the whole `with` block (not torn down after a
    single pipeline call), so a caller can run the initial pipeline and
    then recompute weights/allocation against edited candidate lists any
    number of times before the snapshot is deleted on exit.
    """
    if _is_backtest_date(rebalance_date):
        yield db_path, "backtest"
    else:
        with build_live_snapshot(rebalance_date, selection, source_db_path=db_path) as live_db_path:
            yield live_db_path, "live"


def compute_weights_and_allocation(
    candidates: list[str],
    objective: str,
    portfolio_value: float,
    rebalance_date: date,
    db_path: str,
) -> tuple[dict[str, float], tuple[dict[str, int], float]]:
    """`compute_weights` + `allocate_shares` for `candidates` as of
    `rebalance_date`, reading `db_path` - the part of the pipeline an
    interactive edit re-runs, deliberately never the two LLM agents, since
    editing the candidate list is the user overriding the agents' already-
    given recommendation, not asking them to reconsider it.
    """
    returns_matrix = load_returns_matrix(candidates, as_of=rebalance_date, db_path=db_path)
    weights = compute_weights(returns_matrix, objective)

    latest_prices = load_latest_prices(list(weights.keys()), as_of=rebalance_date, db_path=db_path)
    allocation = allocate_shares(weights, latest_prices, portfolio_value)
    return weights, allocation


def run_scan(
    rebalance_date: date,
    selection: str,
    db_path: str,
    rule: ScreeningRule | None = None,
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
    """
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
) -> dict:
    """The shared sequence behind both modes: `run_scan` (LLM-S/LLM-F/the
    scanner) followed by the optimizer (`compute_weights_and_allocation`)
    - all reading `db_path`, whichever database that is. Exposed (not
    module-private) so a CLI session opened with `open_pipeline_session`
    can call this once for the initial run and then call
    `compute_weights_and_allocation` directly on its own for every
    subsequent edit, without repeating LLM-S/LLM-F/the scanner. `rule` is
    passed straight through to `run_scan` - see its docstring.
    """
    scan = run_scan(rebalance_date, selection, db_path, rule=rule)
    weights, allocation = compute_weights_and_allocation(
        scan["scan_detail"]["candidates"], objective, portfolio_value, rebalance_date, db_path
    )

    return {
        "mode": mode,
        "rebalance_date": rebalance_date,
        "objective": objective,
        "selection": selection,
        **scan,
        "weights": weights,
        "allocation": allocation,
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


def run_pipeline(
    rebalance_date: date,
    objective: str,
    portfolio_value: float,
    selection: str = "llm_s_only",
    db_path: str = "data/portfolio.duckdb",
) -> dict:
    """Run the full pipeline for one `rebalance_date`, as a single,
    self-contained call - see `open_pipeline_session` for a CLI-style
    session that keeps live mode's snapshot alive across multiple calls
    for interactive editing.

    `selection` (one of `"llm_s_only"` (the default), `"llm_f_only"`,
    `"llm_s_and_f"`) controls which agent(s) actually run, per README's
    Backtest Mode Stage 1 default-selection sentence and
    `plans/08_consistency_review.md` finding 9: the skipped agent's call is
    never made (not merely its result discarded), since README frames
    LLM-F evaluation as "expensive". Raises `ValueError` for any other
    `selection` value.

    Backtest mode (`rebalance_date` within the stored 2020-2024 window)
    reads `db_path`'s cached historical tables directly. Live mode (any
    other date) first builds a fresh, throwaway snapshot (a real
    Wikipedia/yfinance/SEC EDGAR fetch, not a read from `db_path`) and runs
    the identical downstream sequence against that instead - `db_path` is
    used there only as the source of the static `news_articles_hf` archive
    `screen_month` needs.

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

    with open_pipeline_session(rebalance_date, selection, db_path) as (effective_db_path, mode):
        return run_pipeline_against(rebalance_date, objective, portfolio_value, selection, effective_db_path, mode)
