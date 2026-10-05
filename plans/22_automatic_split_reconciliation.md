# Automatically reconcile saved holdings after stock splits

This ExecPlan is a living document. The sections `Progress`, `Surprises & Discoveries`, `Decision Log`, and `Outcomes & Retrospective` must be kept up to date as work proceeds. This document must be maintained in accordance with `PLANS.md` at the repository root.

## Purpose / Big Picture

`memory/portfolio.json` records literal share counts. A stock split changes that count even when the owner makes no trade, but the current application only warns that the saved number may be stale. After this change, every report of saved holdings first incorporates confirmed split events, saves the corrected count, and measures the corrected portfolio. The motivating observable case is Tokyo Electron (`8035.T`): 100 shares recorded before its 5:1 split on 2026-09-29 must be reported and saved as 500 shares on 2026-10-05.

## Progress

- [x] (2026-10-05) Confirmed the saved JPY portfolio contains `8035.T: 100`, is dated 2026-09-06, and the holdings cache contains a 5:1 split dated 2026-09-29.
- [x] (2026-10-05) Recorded the persistence and all-report scope decisions with the user.
- [x] (2026-10-05) Added backward-compatible per-ticker split provenance and idempotent reconciliation to portfolio persistence.
- [x] (2026-10-05) Wired reconciliation after cache refresh into direct reports, pipeline holdings blocks, and what-if baselines.
- [x] (2026-10-05) Replaced warning-only output with an announced saved adjustment and documented the policy change.
- [x] (2026-10-05) Added deterministic regression coverage, passed 355 focused tests, passed all 1,292 tests, and verified the real 8035.T data against a temporary portfolio copy.

## Surprises & Discoveries

- Observation: the database already has every fact required to fix the reported count.
  Evidence: `memory/portfolio.json` records the JPY portfolio at 2026-09-06 with 100 shares, while `data/holdings.duckdb` contains `(2026-09-29, 8035.T, 5.0)` in `splits`.
- Observation: the current per-currency `updated_at` can hide one ticker's stale count when an unrelated ticker is edited.
  Evidence: `stale_share_counts` compares every position with the same portfolio timestamp, and its docstring explicitly records this false-negative limitation.
- Observation: reconciliation has to run after the existing preparation step, even though that means a cheap second measurement when a split is applied.
  Evidence: the regression test creates the split table during the first `prepare_holdings` call and observes calls with 100 then 500 shares; reconciling before preparation would defer a newly fetched event until the next command.
- Observation: retaining the manual basis and separately recording applied event dates handles late data better than advancing the basis after an automatic write.
  Evidence: a test first discovers the newer 3:1 event, later discovers an older 2:1 event, reaches 600 shares, and remains at 600 on the third pass.

## Decision Log

- Decision: confirmed splits automatically update and save the real portfolio, with an announcement before the report.
  Rationale: the user explicitly selected persistence, and leaving the raw count stale makes every derived figure wrong on every later run.
  Date/Author: 2026-10-05, user and Codex.
- Decision: reconciliation applies to all saved-holdings reports, including `portfolio` and the real baseline of `portfolio-holdings whatif`.
  Rationale: the same saved holding must not have different share counts depending on which report displays it; hypothetical what-if edits remain unsaved.
  Date/Author: 2026-10-05, user and Codex.
- Decision: retain numeric `positions` and add parallel `position_updated_at` and `applied_splits` maps.
  Rationale: existing Python loaders and hand-edited files keep their simple share-count shape, while each ticker gains enough provenance for unrelated edits and delayed split discovery.
  Date/Author: 2026-10-05, Codex.

## Outcomes & Retrospective

The reported defect is fixed. Against a temporary copy of the real portfolio and the real cached database, the first offline command printed `Adjusted 8035.T from 100 to 500 shares for its 5:1 split on 2026-09-29`, displayed and measured 500 shares, and persisted the event. An identical second command displayed 500 without another adjustment. The actual `memory/portfolio.json` was not changed during verification.

Per-ticker bases remove the old unrelated-edit false negative, and applied dates make both repeated reports and out-of-order event discovery safe. The remaining deliberate limit is an undated hand-edited position: the program names it but cannot safely infer whether an old split is already reflected. The complete suite passed with 1,292 tests and three pre-existing PyPortfolioOpt warnings.

## Context and Orientation

`src/agentic_portfolio/flow/user_portfolio.py` validates and writes `memory/portfolio.json`. Its `load_portfolio` API returns a plain mapping from ticker to shares and must remain unchanged. `src/agentic_portfolio/dataset/dividends.py` reads cached corporate-action rows from the DuckDB `splits` table. `src/agentic_portfolio/flow/holdings_cli.py` reports holdings directly, while `src/agentic_portfolio/flow/cli.py` prints the same saved holdings below an optimized candidate portfolio. `src/agentic_portfolio/flow/interactive.py` refreshes `data/holdings.duckdb` before measuring holdings.

A split basis is the instant at which a manually entered share count was known to be current. An applied split is a cached split event already multiplied into that count. These are distinct: retaining the manual basis while recording individual applied events lets a split discovered late still be applied without applying earlier events twice.

## Plan of Work

First, extend `user_portfolio.py` so a portfolio entry may carry `position_updated_at` and `applied_splits`. Legacy entries use their existing portfolio-level `updated_at` as each ticker's initial basis. `save_portfolio` will accept the tickers manually changed by a partial edit; it will reset provenance only for those tickers and preserve it for every untouched ticker. Removal drops parallel metadata.

Add a pure reconciliation result naming before/after shares and events, plus a persistence operation that considers split events after each ticker's manual basis and on or before the report date. It multiplies only event dates not already recorded, writes all corrections once, and returns corrected positions. Missing provenance never causes a guessed multiplication.

Next, replace `format_stale_share_counts` with formatting for completed adjustments and unknown-basis diagnostics. In each report path, let the existing holdings preparation or session refresh populate the split cache first, reconcile second, and measure again only when positions changed. In a what-if session, reconcile the real baseline before hypothetical edits; change its closing sentence and docs to promise that hypothetical changes, rather than corporate-action maintenance, are never saved.

Finally, update README's warning-only description and the historical note in plan 15, add deterministic tests for migration, partial edits, idempotence, multiple and reverse splits, date boundaries, no-fetch cached operation, first-refresh ordering, and each CLI path, then run all tests.

## Concrete Steps

Work from `/app/agentic_portfolio`.

Run focused tests during implementation:

    uv run pytest tests/test_user_portfolio.py tests/test_holdings_cache.py tests/test_holdings_cli.py tests/test_cli.py

Run the complete suite before completion:

    uv run pytest tests/test_*.py

For a non-destructive real-data check, copy `memory/portfolio.json` to a temporary path, invoke `portfolio-holdings` with `--path` pointing to the copy, and confirm 8035.T changes from 100 to 500 once and remains 500 on a second run.

## Validation and Acceptance

A fixture matching the real JPY record must print and persist 500 shares for 8035.T on 2026-10-05, name the 5:1 split and 2026-09-29 date, and leave 9984.T unchanged. A second invocation must make no further adjustment. Editing 9984.T after the split must not hide the pending 8035.T event. Both main commands and what-if's baseline must use the corrected count. All existing tests plus the new regression tests must pass without network access.

## Idempotence and Recovery

Applied split dates make reconciliation idempotent. Writes continue to use the existing hand-readable JSON file and preserve other currencies. A legacy file needs no separate migration command: metadata is derived when read and materialized on its next write. If split data or a trustworthy timestamp is absent, the application reports that it cannot reconcile and leaves the count unchanged. Tests and real-data verification use temporary paths and never alter the user's actual portfolio.

## Artifacts and Notes

The motivating state observed before implementation is:

    portfolio JPY updated_at: 2026-09-06T05:26:22.246637+00:00
    stored position: 8035.T = 100
    cached split: 8035.T = 5:1 on 2026-09-29

The pre-change focused baseline was:

    349 passed in 70.50s

The completed focused and full validations were:

    355 passed in 88.79s
    1292 passed, 3 warnings in 405.72s

The real-data dry run printed, once:

    Adjusted 8035.T from 100 to 500 shares for its 5:1 split on 2026-09-29

## Interfaces and Dependencies

`load_portfolio(path, currency) -> dict[str, float]` and `load_all_portfolios(path) -> dict[str, dict[str, float]]` remain unchanged. `save_portfolio` gains an optional set of manually updated tickers so partial CLI edits preserve metadata. A new reconciliation operation accepts the portfolio path, currency, report date, and split series, and returns corrected positions, applied adjustments, and tickers that could not be dated. It uses only the existing standard library, pandas, and the repository's existing split data; no new dependency or network call is introduced.

Revision note (2026-10-05): created this plan from the accepted design and recorded the verified local state before implementation.

Revision note (2026-10-05): completed implementation, recorded the cache-ordering and delayed-event findings, and added focused, full-suite, and real-data evidence.
