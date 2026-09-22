# Add an adjustable returns window to the portfolio edit loop

This ExecPlan is a living document. The sections `Progress`, `Surprises & Discoveries`, `Decision Log`, and `Outcomes & Retrospective` must be kept up to date as work proceeds.

This document must be maintained in accordance with `PLANS.md` at the repository root.

## Purpose / Big Picture

After this change, a person running `uv run portfolio` can type `[w]indow` in the post-run edit loop and ask the optimizer to reconsider the same candidate pool over any whole-number window from 24 through 60 monthly returns. The optimizer, benchmark, and any later ticker summaries will use the chosen window without rerunning the screening agents or rebuilding the live market-data snapshot. The report will state the actual dates and number of monthly returns used and will identify a nondefault requested window.

The initial run remains a 60-month calculation. The chosen window lasts only for the current interactive session and is not written to candidate memory or configuration.

## Progress

- [x] (2026-09-22 07:20Z) Inspected the optimizer, interactive edit loop, holdings what-if window control, reporting, archive, and tests.
- [x] (2026-09-22 07:24Z) Added window state, prompting, validation, and rollback to the portfolio edit loop.
- [x] (2026-09-22 07:25Z) Threaded the requested lookback through optimization and ticker summaries.
- [x] (2026-09-22 07:27Z) Made nondefault requested windows visible in reports and archived report bodies.
- [x] (2026-09-22 07:29Z) Added deterministic tests for valid, invalid, retained, benchmark-aligned, and rejected window edits.
- [x] (2026-09-22 07:36Z) Updated user documentation and passed the focused and full test suites.

## Surprises & Discoveries

- Observation: `portfolio-holdings whatif` already defines and validates the desired 24-to-60-month range in `src/agentic_portfolio/optimizer/holdings.py` and provides the desired keep-the-current-value behavior for bad prompt input.
  Evidence: `validate_lookback_months` enforces the two existing data limits, and `_whatif_window` catches its errors without ending the session.

- Observation: Saved-report summaries already treat the actual returns window as part of the comparison key, so successful portfolio variants need no new summary algorithm.
  Evidence: `src/agentic_portfolio/flow/report_summary.py` groups reports by `window_start`, `window_end`, and `window_months`.

- Observation: A ticker summary has its own default 60-month measurement and therefore must receive the active edit-loop lookback or it will cease to describe the estimates the ticker is about to join.
  Evidence: `prepare_ticker_summary` calls `ticker_stats` without its existing `lookback_months` argument.

- Observation: The complete suite is intentionally slow in the holdings modules but requires no network for these tests.
  Evidence: `uv run pytest tests/test_*.py` completed with 1,279 passing tests and three pre-existing PyPortfolioOpt warnings in 404.16 seconds.

## Decision Log

- Decision: Keep the initial `portfolio` calculation at 60 months and expose window selection only in the iterative loop.
  Rationale: This satisfies the requested workflow without changing scripted invocations, backtests, or existing initial output.
  Date/Author: 2026-09-22 / Codex

- Decision: Preserve the current objective and MV target when only the window changes, even when the initial target was derived from a benchmark.
  Rationale: A sensitivity comparison is interpretable when the window is the only changed input. `[o]bjective` and `[t]arget-return` remain available for deliberate changes.
  Date/Author: 2026-09-22 / Codex

- Decision: Reuse `DEFAULT_LOOKBACK_MONTHS` and `validate_lookback_months` from `optimizer.holdings` rather than introduce a second set of bounds.
  Rationale: Those symbols already encode the shared 24-month minimum and 60-month ingestion ceiling used by both optimized and held portfolios.
  Date/Author: 2026-09-22 / Codex

- Decision: Show a requested-window suffix only for a nondefault edit-loop window.
  Rationale: The unchanged initial and default edit output remains byte-compatible, while a sensitivity result explicitly distinguishes the request from the actual usable row count.
  Date/Author: 2026-09-22 / Codex

## Outcomes & Retrospective

Implemented the session-only `[w]indow` edit for optimized portfolios. A successful 24-to-60-month choice now drives the return matrix, weights, allocation, benchmark comparison, later edits, and later ticker summaries while preserving the screened pool and live snapshot. Reports identify nondefault requested windows, rejected solves restore the previous state, and initial/noninteractive behavior remains at 60 months.

Validation completed with `uv run pytest tests/test_cli.py tests/test_interactive_flow.py` (285 passing tests before the final benchmark-specific case was added) and the final `uv run pytest tests/test_*.py` run (1,279 passing tests, three expected warnings). No migration, persistent setting, or new dependency was needed.

## Context and Orientation

`src/agentic_portfolio/flow/cli.py` owns the `portfolio` command and its `_run_edit_loop`. The loop retains the screened candidate list and recomputes only optimization and allocation after edits. `src/agentic_portfolio/flow/interactive.py` supplies `compute_weights_and_allocation`, which loads a returns matrix and then solves the requested portfolio objective. `src/agentic_portfolio/optimizer/portfolio.py` already accepts `lookback_months` in `load_returns_matrix`, but the interactive wrapper currently relies on its fixed 60-month default.

The term “returns window” means the trailing monthly-return rows used to estimate expected annual return and covariance. A 36-month request asks for the most recent 36 return dates on or before the run's rebalance date. Individual candidates must still have at least 24 usable returns; candidates below that threshold are removed by the existing minimum-history rule.

`src/agentic_portfolio/optimizer/holdings.py` contains `DEFAULT_LOOKBACK_MONTHS = 60` and `validate_lookback_months`, which enforces the 24-to-60 inclusive range. `src/agentic_portfolio/flow/holdings_cli.py` demonstrates the desired prompt behavior. `src/agentic_portfolio/optimizer/benchmark.py` can slice an already-prepared benchmark source to the exact start and end dates in the optimized portfolio statistics, so changing the optimization matrix and then using the existing benchmark call keeps the comparison aligned.

`tests/test_cli.py` isolates edit-loop sequencing with mocked optimization and input. `tests/test_interactive_flow.py` tests the wrapper that loads returns and builds ticker summaries. Tests must remain deterministic and must not call Yahoo Finance.

## Plan of Work

First, extend `compute_weights_and_allocation` in `src/agentic_portfolio/flow/interactive.py` with a defaulted `lookback_months` keyword and pass it to `load_returns_matrix`. Extend `prepare_ticker_summary` with the same keyword and pass it to both `ticker_stats` paths. Existing callers omit the keyword and retain 60-month behavior.

Second, update `src/agentic_portfolio/flow/cli.py`. Import the existing lookback default and validator, add a prompt helper modeled on holdings what-if, and add a `lookback_months` parameter and local state to `_run_edit_loop`. Offer `[w]indow` in the prompt. A valid changed value enters the same recomputation path as other edits, while blank, invalid, and unchanged values continue without recomputation. Save the prior window beside the other editable state and restore all of it if optimization raises `ValueError`. Pass the active value to every optimizer call and to ticker summaries requested after the loop begins.

Third, extend `print_weights_and_allocation` with an optional requested-lookback argument. Preserve the exact default returns-window line when the request is 60 or unspecified. For a nondefault request, append text such as `, 36 requested` inside the existing parenthetical. Pass this value from `_run_edit_loop`; because the report archive captures this printed block, different requested variants remain distinct without changing archive storage formats.

Fourth, add focused tests. Test the prompt, valid boundary and intermediate values, validation failures and unchanged values, retention across a later unrelated edit, complete rollback after a rejected solve, requested-window rendering, optimizer matrix loading, and ticker-summary forwarding. Existing tests prove initial/default compatibility by continuing to assert the old output and calls.

Finally, document the new command in `README.md`, update relevant docstrings, run focused test modules, and then run the repository's complete prescribed suite.

## Concrete Steps

Work from `/app/agentic_portfolio`.

Apply the implementation and tests, then run:

    uv run pytest tests/test_cli.py tests/test_interactive_flow.py

Expect both modules to pass without network calls. Then run:

    uv run pytest tests/test_*.py

Expect the complete suite to pass. Exercise the behavior manually only against a safe fixture or an already-prepared local database; do not invoke the write-oriented builder commands listed in `AGENTS.md` merely to inspect entry points.

## Validation and Acceptance

Acceptance requires that an interactive session can start with its normal 60-month report, accept `w` followed by `36`, print `Measuring over 36 month(s) of returns.`, and produce a new optimized report whose returns-window line identifies `36 requested`. Typing an invalid value such as `12` must explain the 24-to-60 bounds, retain the current window, and perform no solve. If a new window makes optimization impossible, the loop must print the optimizer reason, restore the old window and all other editable values, and remain usable.

A candidate, objective, dividend, or benchmark edit after a successful window change must continue using the selected lookback. A ticker summary requested after that change must compute its project-owned figures over the same requested lookback. Benchmark figures must still be sliced to the actual start and end dates reported by the optimized portfolio.

The initial pipeline, backtest path, holdings commands, and direct callers that omit the new keyword must remain on 60 months. The complete test suite must pass.

## Idempotence and Recovery

All code and documentation edits are ordinary tracked-file changes and can be applied repeatedly through version control. Tests use temporary databases and monkeypatching and do not alter market-data files. The new control writes no persistent window preference. A failed interactive solve restores in-memory state, so the user can retry another window without restarting or refetching.

## Artifacts and Notes

The intended interaction is:

    Edit candidates? ... / [w]indow / ...: w
    Months of returns to optimize over (24-60, currently 60): 36
    Measuring over 36 month(s) of returns.
    Returns window: 2023-09-30 to 2026-08-31 (36 month(s) of monthly returns, 36 requested)

Exact dates depend on the rebalance date and database contents.

## Interfaces and Dependencies

In `src/agentic_portfolio/flow/interactive.py`, the resulting interfaces are:

    def compute_weights_and_allocation(..., lookback_months: int = DEFAULT_LOOKBACK_MONTHS, returns_matrix: pd.DataFrame | None = None) -> tuple[PortfolioStats, tuple[dict[str, int], float]]

    def prepare_ticker_summary(..., lookback_months: int = DEFAULT_LOOKBACK_MONTHS, ...) -> TickerSummary

In `src/agentic_portfolio/flow/cli.py`, `_run_edit_loop` gains a defaulted `lookback_months` argument, and `print_weights_and_allocation` gains an optional requested-lookback argument used only for display. No new package or external service is required.

Revision note (2026-09-22): Marked the plan complete after implementing the feature, documenting the shared validator wording, and recording focused and full-suite verification results.
