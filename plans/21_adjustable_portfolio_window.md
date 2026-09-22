# Add an adjustable returns window to the portfolio edit loop

This ExecPlan is a living document. The sections `Progress`, `Surprises & Discoveries`, `Decision Log`, and `Outcomes & Retrospective` must be kept up to date as work proceeds.

This document must be maintained in accordance with `PLANS.md` at the repository root.

## Purpose / Big Picture

After this change, a person running `uv run portfolio` can type `[w]indow` in the post-run edit loop and ask the optimizer to reconsider the same candidate pool over any whole-number window from 12 through 60 monthly returns. The optimizer, benchmark, and any later ticker summaries will use the chosen window without rerunning the screening agents or rebuilding the live market-data snapshot. The report will state the actual dates and number of monthly returns used and will identify a nondefault requested window.

The initial run remains a 60-month calculation. The chosen window lasts only for the current interactive session and is not written to candidate memory or configuration.

## Progress

- [x] (2026-09-22 07:20Z) Inspected the optimizer, interactive edit loop, holdings what-if window control, reporting, archive, and tests.
- [x] (2026-09-22 07:24Z) Added window state, prompting, validation, and rollback to the portfolio edit loop.
- [x] (2026-09-22 07:25Z) Threaded the requested lookback through optimization and ticker summaries.
- [x] (2026-09-22 07:27Z) Made nondefault requested windows visible in reports and archived report bodies.
- [x] (2026-09-22 07:29Z) Added deterministic tests for valid, invalid, retained, benchmark-aligned, and rejected window edits.
- [x] (2026-09-22 07:36Z) Updated user documentation and passed the focused and full test suites.
- [x] (2026-09-22 08:20Z) Expanded shared validation and both prompts from 24-60 to 12-60 months.
- [x] (2026-09-22 08:24Z) Applied `min(24, requested window)` consistently to candidates, benchmarks, holdings, and ticker summaries while retaining the low-level defaults.
- [x] (2026-09-22 08:37Z) Added short-window regression tests, updated user documentation, and passed the focused and full suites.

## Surprises & Discoveries

- Observation: The first implementation reused `portfolio-holdings whatif`'s 24-to-60-month validation from `src/agentic_portfolio/optimizer/holdings.py`; expanding the prompt alone cannot work because the same 24 was also the minimum-history threshold.
  Evidence: a 12-to-23-month matrix cannot contain the 24 non-null returns required by `load_returns_matrix`, `holdings_stats`, `benchmark_stats_for_window`, or `ticker_stats`, so every security would otherwise be excluded.

- Observation: Saved-report summaries already treat the actual returns window as part of the comparison key, so successful portfolio variants need no new summary algorithm.
  Evidence: `src/agentic_portfolio/flow/report_summary.py` groups reports by `window_start`, `window_end`, and `window_months`.

- Observation: A ticker summary has its own default 60-month measurement and therefore must receive the active edit-loop lookback or it will cease to describe the estimates the ticker is about to join.
  Evidence: `prepare_ticker_summary` calls `ticker_stats` without its existing `lookback_months` argument.

- Observation: The complete suite is intentionally slow in the holdings modules but requires no network for these tests.
  Evidence: `uv run pytest tests/test_*.py` completed with 1,279 passing tests and three pre-existing PyPortfolioOpt warnings in 404.16 seconds.

- Observation: The expanded implementation adds no data migration or archive-schema change.
  Evidence: the stored return history already covers 12-month requests, while archived reports already record actual window dates, row count, and the requested nondefault window in their body.

## Decision Log

- Decision: Keep the initial `portfolio` calculation at 60 months and expose window selection only in the iterative loop.
  Rationale: This satisfies the requested workflow without changing scripted invocations, backtests, or existing initial output.
  Date/Author: 2026-09-22 / Codex

- Decision: Preserve the current objective and MV target when only the window changes, even when the initial target was derived from a benchmark.
  Rationale: A sensitivity comparison is interpretable when the window is the only changed input. `[o]bjective` and `[t]arget-return` remain available for deliberate changes.
  Date/Author: 2026-09-22 / Codex

- Decision: Reuse `DEFAULT_LOOKBACK_MONTHS` and `validate_lookback_months` from `optimizer.holdings` rather than introduce a second validator. This decision is retained, but its original rationale tying the selectable floor to the 24-month history threshold is superseded by the next decision.
  Rationale: Both commands must still accept and reject the same values, and the 60-month ingestion ceiling remains shared.
  Date/Author: 2026-09-22 / Codex

- Decision: The selectable range is 12 through 60 whole months, and the effective minimum usable history is `min(24, requested_lookback_months)`.
  Rationale: Keeping 24 as the unconditional threshold would make every 12-to-23-month request empty. Lowering the global default to 12 would instead admit a 12-month ticker into an ordinary 60-month estimate and silently change established behavior. The dynamic rule makes a short request require its complete requested history while preserving the existing 24-month eligibility rule for every 24-to-60-month calculation.
  Date/Author: 2026-09-22 / Codex and repository owner

- Decision: Show a requested-window suffix only for a nondefault edit-loop window.
  Rationale: The unchanged initial and default edit output remains byte-compatible, while a sensitivity result explicitly distinguishes the request from the actual usable row count.
  Date/Author: 2026-09-22 / Codex

## Outcomes & Retrospective

The session-only `[w]indow` edit now accepts every whole-number window from 12 to 60 months in both interactive commands. The shared `effective_min_months` rule requires complete history for a 12-to-23-month request and retains the 24-month bar from 24 through 60. Candidate optimization, holdings, benchmark comparisons, and ticker summaries all receive that same threshold; initial, noninteractive, and backtest behavior remains on the established 60/24 defaults.

Expansion validation completed with 22 focused boundary and propagation cases, followed by `uv run pytest tests/test_*.py` (1,286 passing tests and three expected PyPortfolioOpt warnings in 404.67 seconds). No migration, persistent setting, or new dependency was needed.

## Context and Orientation

`src/agentic_portfolio/flow/cli.py` owns the `portfolio` command and its `_run_edit_loop`. The loop retains the screened candidate list and recomputes only optimization and allocation after edits. `src/agentic_portfolio/flow/interactive.py` supplies `compute_weights_and_allocation`, which loads a returns matrix and then solves the requested portfolio objective. `src/agentic_portfolio/optimizer/portfolio.py` already accepts `lookback_months` in `load_returns_matrix`, but the interactive wrapper currently relies on its fixed 60-month default.

The term “returns window” means the trailing monthly-return rows used to estimate expected annual return and covariance. A 36-month request asks for the most recent 36 return dates on or before the run's rebalance date. For a requested window of 24 through 60 months, an individual candidate still needs at least 24 usable returns. For a requested window of 12 through 23 months, it needs every requested month. In formula form, the effective threshold is `min(24, requested_lookback_months)`.

`src/agentic_portfolio/optimizer/holdings.py` contains `DEFAULT_LOOKBACK_MONTHS = 60` and `validate_lookback_months`; the expansion changes its accepted range to 12 through 60. `src/agentic_portfolio/flow/holdings_cli.py` supplies the matching holdings prompt. `src/agentic_portfolio/optimizer/benchmark.py` can slice an already-prepared benchmark source to the exact start and end dates in the optimized portfolio statistics, so changing the optimization matrix and then using the same effective minimum keeps the comparison aligned.

`tests/test_cli.py` isolates edit-loop sequencing with mocked optimization and input. `tests/test_interactive_flow.py` tests the wrapper that loads returns and builds ticker summaries. Tests must remain deterministic and must not call Yahoo Finance.

## Plan of Work

First, retain the existing `lookback_months` threading and add one shared calculation for `effective_min_months = min(24, lookback_months)`. Pass both values to `load_returns_matrix`. Pass the same effective minimum through `prepare_ticker_summary` to both `ticker_stats` paths. Existing callers that omit a window retain the 60-month lookback and 24-month minimum.

Second, change the shared validator and both prompts to accept 12 through 60. A valid changed value enters the existing recomputation path, while blank, invalid, and unchanged values continue without recomputation. Save and restore the prior window exactly as the first milestone does when optimization raises `ValueError`.

Third, extend `print_weights_and_allocation` with an optional requested-lookback argument. Preserve the exact default returns-window line when the request is 60 or unspecified. For a nondefault request, append text such as `, 36 requested` inside the existing parenthetical. Pass this value from `_run_edit_loop`; because the report archive captures this printed block, different requested variants remain distinct without changing archive storage formats.

Fourth, add focused tests. Test accepted windows 12, 18, 24, and 60; rejected windows 11 and 61; retention across later edits; rollback after a rejected solve; and requested-window rendering. Assert that a 12-month request passes `min_months=12` to the candidate loader, benchmark measurement, holdings measurement, and ticker summary, while a 36- or 60-month request passes 24. Existing tests continue to prove initial/default compatibility.

Finally, document the new command in `README.md`, update relevant docstrings, run focused test modules, and then run the repository's complete prescribed suite.

## Concrete Steps

Work from `/app/agentic_portfolio`.

Apply the implementation and tests, then run:

    uv run pytest tests/test_cli.py tests/test_interactive_flow.py

Expect both modules to pass without network calls. Then run:

    uv run pytest tests/test_*.py

Expect the complete suite to pass. Exercise the behavior manually only against a safe fixture or an already-prepared local database; do not invoke the write-oriented builder commands listed in `AGENTS.md` merely to inspect entry points.

## Validation and Acceptance

Acceptance requires that an interactive session can start with its normal 60-month report, accept `w` followed by `12`, print `Measuring over 12 month(s) of returns.`, and produce a new optimized report whose returns-window line identifies `12 requested`. Twelve complete monthly returns are sufficient for that request; eleven are not. Typing 11 or 61 must explain the 12-to-60 bounds, retain the current window, and perform no solve. If a new window makes optimization impossible, the loop must print the optimizer reason, restore the old window and all other editable values, and remain usable.

A candidate, objective, dividend, or benchmark edit after a successful window change must continue using the selected lookback. A ticker summary requested after that change must compute its project-owned figures over the same requested lookback. Benchmark figures must still be sliced to the actual start and end dates reported by the optimized portfolio.

The initial pipeline, backtest path, holdings commands, and direct callers that omit the new keyword must remain on 60 months. The complete test suite must pass.

## Idempotence and Recovery

All code and documentation edits are ordinary tracked-file changes and can be applied repeatedly through version control. Tests use temporary databases and monkeypatching and do not alter market-data files. The new control writes no persistent window preference. A failed interactive solve restores in-memory state, so the user can retry another window without restarting or refetching.

## Artifacts and Notes

The intended interaction is:

    Edit candidates? ... / [w]indow / ...: w
    Months of returns to optimize over (12-60, currently 60): 12
    Measuring over 12 month(s) of returns.
    Returns window: 2025-09-30 to 2026-08-31 (12 month(s) of monthly returns, 12 requested)

Exact dates depend on the rebalance date and database contents.

## Interfaces and Dependencies

In `src/agentic_portfolio/flow/interactive.py`, the resulting interfaces are:

    def compute_weights_and_allocation(..., lookback_months: int = DEFAULT_LOOKBACK_MONTHS, returns_matrix: pd.DataFrame | None = None) -> tuple[PortfolioStats, tuple[dict[str, int], float]]

    def prepare_ticker_summary(..., lookback_months: int = DEFAULT_LOOKBACK_MONTHS, ...) -> TickerSummary

In `src/agentic_portfolio/flow/cli.py`, `_run_edit_loop` retains its defaulted `lookback_months` argument, and `print_weights_and_allocation` retains its optional requested-lookback argument used only for display. The optimization, benchmark, holdings, and ticker-stat interfaces must accept or derive the effective minimum so all four measurements apply `min(24, requested_lookback_months)`. No new package or external service is required.

Revision note (2026-09-22): Marked the plan complete after implementing the feature, documenting the shared validator wording, and recording focused and full-suite verification results.

Revision note (2026-09-22): Reopened the plan to expand both interactive commands from 24-60 to 12-60 months. Added the dynamic minimum-history rule so the expansion is usable without weakening ordinary 24-to-60-month estimates.
