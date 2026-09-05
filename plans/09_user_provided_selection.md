# Let the user choose the candidate pool directly with a `user_provided` selection


This ExecPlan is a living document. The sections `Progress`, `Surprises & Discoveries`, `Decision Log`, and `Outcomes & Retrospective` must be kept up to date as work proceeds. This plan must be maintained in accordance with `PLANS.md` at the repository root. This plan builds on `plans/01_dataset.md` (for the `prices`/`returns` tables and the fetch/compute functions it reuses), `plans/05_optimizer_and_allocation.md` (for `load_returns_matrix`/`compute_weights`/`allocate_shares`), and `plans/06_interactive_flow.md` (for the CLI, the live snapshot, and the candidate-editing loop it extends) — all three checked into this repository.


## Purpose / Big Picture


Before this change, every way of running this project's pipeline decided the candidate pool for you. The three existing `--selection` values (`llm_s_only`, `llm_f_only`, `llm_s_and_f`) all work the same way: an LLM agent reads data about the current S&P 500 members and votes "buy", "sell", or "hold" on each one, and the tickers it voted "buy" on become the candidate pool that the optimizer then assigns weights to. A person who already knows which tickers they want in their portfolio had no way to say so. They could only run an agent first and then edit its answer afterwards, and even then their edits were thrown away when the command exited.

After this change, a person can run the pipeline with `--selection user_provided` and be asked directly. The command prints the candidate pool it remembers from last time, then repeatedly offers to add or remove tickers until the person says they are done. Each ticker they add is checked against Yahoo Finance first: a symbol that does not resolve is named back to them and left out, while the good symbols typed on the same line are still added. When they confirm they are done, the pool is saved to a file (`memory/candidates.json` by default) so the next run starts from it, and the pool goes through the exact same optimizer and share-allocation steps every other selection uses.

Concretely, this is the observable outcome, from a real run (the `$` lines are what a person types):

    $ uv run python -m src.flow.cli --date today --objective GMV --value 100000 --selection user_provided

    Current candidate pool (0): (empty)

    Edit candidate pool? [a]dd tickers / [r]emove tickers / [d]one: a
    Ticker(s) to add (space-separated): AAPL SPY ZZZZQQQ
    Added: AAPL, SPY.
    Ignored (not found): ZZZZQQQ.
    Candidate pool (2): AAPL, SPY

    Edit candidate pool? [a]dd tickers / [r]emove tickers / [d]one: d
    Saved 2 ticker(s) to memory/candidates.json.
    Mode: live  Rebalance date: 2026-09-05  Objective: GMV  Selection: user_provided

    Scanner branch: user_provided  (buy_s=None buy_f=None intersection=None union=None)
    Candidates (2): AAPL, SPY

    Weights:
      SPY: 0.9802
      AAPL: 0.0198

    Share allocation:
      AAPL: 6
      SPY: 126
    Leftover cash: $611.32

Two things in that transcript are worth naming explicitly, because they are the whole point of the feature. `ZZZZQQQ` is not a real ticker, and it was reported as such without preventing `AAPL` and `SPY` from being added. And `SPY` is an exchange-traded fund, not an S&P 500 member company — this selection deliberately accepts any symbol Yahoo Finance can price, not just index members, so a person can build a pool from ETFs, mid-caps, or anything else they care about.


## Progress


- [x] (2026-09-05) Added `src/flow/candidate_memory.py` with `load_candidate_pool`/`save_candidate_pool`, persisting the pool as `{"tickers": [...], "updated_at": "<UTC ISO timestamp>"}`. A missing file reads back as an empty list (the expected first-run state); a file whose `tickers` field is missing or is not a list of strings raises `ValueError` rather than silently yielding an empty pool. Covered by 8 tests in `tests/test_candidate_memory.py`.
- [x] (2026-09-05) Added a merge-write ("upsert") layer to the dataset modules, because a user-supplied ticker must be added to a database that already holds other tickers' data without disturbing it: `upsert_prices_tables` and `fetch_and_reshape_for_tickers` in `src/dataset/prices.py`, and `upsert_returns_table` and `build_returns_for_tickers` in `src/dataset/returns.py`. Also gave `detect_unresolved_tickers` optional `start`/`end` parameters so its explanatory message names the window actually fetched instead of the module-wide default one. `build_price_history` was refactored to call the new `fetch_and_reshape_for_tickers` rather than duplicating that sequence inline — no behavior change. Covered by 10 tests in `tests/test_dataset_upsert.py` plus 1 added to `tests/test_dataset.py`.
- [x] (2026-09-05) Added `src/dataset/ticker_ingestion.py` with `validate_and_ingest_tickers`, the single function that both answers "does this ticker exist" and stores what the optimizer will need for it. Covered by 8 tests in `tests/test_ticker_ingestion.py`, all with the network call monkeypatched.
- [x] (2026-09-05) Taught `src/flow/live.py`'s `build_live_snapshot` a `user_provided` branch that creates empty `prices`/`unresolved_tickers`/`returns` tables (new helper `_init_empty_price_and_returns_tables`) instead of fetching Wikipedia membership and ~500 tickers of price history — none of which this selection reads.
- [x] (2026-09-05) Wired the selection into `src/flow/interactive.py`: added `"user_provided"` to `VALID_SELECTIONS`; made `open_pipeline_session` always hand this selection a scratch snapshot (even for a date the stored cache covers, so the shared cache is never written to); added a short-circuit at the top of `run_scan` that returns the user's list in the same six-key shape the scanner produces, without calling either agent or the scanner; threaded an additive `candidates` parameter through `run_scan`/`run_pipeline_against`/`run_pipeline`; and added `validate_and_edit_candidates`, the validating counterpart to the existing `edit_candidates`. Covered by 7 tests added to `tests/test_interactive_flow.py`.
- [x] (2026-09-05) Wired the user-facing loop into `src/flow/cli.py`: added `_run_user_provided_confirm_loop` (the show-pool, add/remove, confirm-when-done loop), a `--memory-path` argument, and made the existing post-run `_run_edit_loop` validate added tickers and re-save the pool when — and only when — the selection is `user_provided`. Covered by 13 tests in the new `tests/test_cli.py`, the first automated coverage this CLI module has had.
- [x] (2026-09-05) Added `memory/` to `.gitignore`, since a saved candidate pool is exactly the "local watchlist data" `AGENTS.md` says must not land in tracked files.
- [x] (2026-09-05) Verified the full suite: `uv run pytest tests/test_*.py` reports 188 passed, up from 141 before this work (47 new tests). Verified end to end with three real network runs, transcripts in Artifacts and Notes below.


## Surprises & Discoveries


- Discovery (2026-09-05): a `user_provided` session cannot reuse the ordinary live snapshot, and it must not use the stored cache either. `src/dataset/prices.py`'s `build_price_history` derives the tickers it fetches from the `sp500_membership` table via `load_ticker_universe`, and raises `TickerUniverseEmptyError` if that table is missing — so it cannot be pointed at an arbitrary user-supplied ticker instead. Meanwhile the existing `write_prices_tables`/`write_returns_table` both begin with `DROP TABLE IF EXISTS`, so calling them for a newly added ticker would erase every previously added ticker's rows. Both facts together are why this plan added a ticker-scoped fetch path (`fetch_and_reshape_for_tickers`, `build_returns_for_tickers`) and a merge-write path (`upsert_prices_tables`, `upsert_returns_table`) rather than reusing the existing writers.
- Discovery (2026-09-05): the merge-write path has to delete by the *requested* ticker list rather than by what is present in the data it is inserting. A ticker can move between the two tables between runs — resolving one day (rows in `prices`, absent from `unresolved_tickers`) and failing the next (the reverse) — so deleting only what is about to be inserted would leave a stale row behind in the other table. Two tests pin this down: `test_upsert_prices_tables_clears_stale_unresolved_row_when_ticker_now_resolves` and `test_upsert_prices_tables_clears_stale_price_rows_when_ticker_stops_resolving`.
- Discovery (2026-09-05): the incremental merge genuinely preserves earlier batches, verified directly rather than assumed, since it was the main technical risk in this design. Ingesting `MSFT`, then separately ingesting `NVDA` and a bogus symbol into the same session database, leaves both real tickers fully intact and usable by the optimizer:

        batch1 valid: ['MSFT'] invalid: []
        batch2 valid: ['NVDA'] invalid: ['BADTICKER1']
        returns matrix shape: (60, 2) columns kept: ['MSFT', 'NVDA']
        non-null counts: {'MSFT': 60, 'NVDA': 60}
        latest prices: MSFT 407.567352, NVDA 82.133011

- Surprise (2026-09-05): a validated ticker can still legitimately receive zero weight, which looks like a failure but is not. In the run recorded in Artifacts below, `NVDA` was added successfully and yet the printed weights showed only `MSFT: 1.0000`. The cause is the optimizer, not the ingestion: `GMV` means global minimum variance, so it concentrates in the lowest-volatility asset available, and `src/flow/cli.py`'s `print_weights_and_allocation` only prints tickers whose weight is above zero. The check in the previous bullet is what distinguishes the two explanations — `NVDA` had all 60 months of returns and was kept in the matrix, so it was priced and considered, then simply not chosen. The same effect is visible in the live run in Purpose above, where `SPY` (a diversified fund, low volatility) takes 98% of a `GMV` portfolio and `AAPL` (a single volatile stock) takes 2%.
- Discovery (2026-09-05): "valid" and "usable by the optimizer" are not the same thing, and this boundary is worth stating for whoever hits it. `validate_and_ingest_tickers` confirms only that Yahoo Finance returns usable prices. A genuinely listed but very recently floated company passes that check and can still be dropped later by `src/optimizer/portfolio.py`'s `apply_min_history_rule`, which requires 24 months of returns by default and logs each drop. That is the optimizer's own pre-existing rule, unchanged by this plan, not a validation failure — but a person who adds a brand-new IPO and sees it vanish from the weights deserves this sentence to explain why.
- Discovery (2026-09-05): `src/flow/cli.py`'s existing `_run_edit_loop` had a latent bug that this feature would have turned into a real one. The loop assigned the edited list to `candidates` *before* testing whether the edit had emptied it, and on the empty branch printed "keeping the previous list" and continued without actually restoring anything — so the in-memory list was left empty despite the message. Harmless before this plan, because nothing read that value until a later edit overwrote it; not harmless once an accepted edit gets persisted to disk. Fixed by capturing the pre-edit list and restoring it on that branch, and by ordering the save to happen after the guard. Pinned by `test_run_edit_loop_keeps_previous_list_when_an_edit_would_empty_it`.
- Surprise (2026-09-05): monkeypatching `builtins.input` with `iter(responses).__next__` — the obvious first instinct for scripting an interactive loop in a test — fails with `TypeError: expected 0 arguments, got 1`, because the real `input()` is always called here with a prompt string. The working form, used by `_script` in `tests/test_cli.py`, is a lambda that accepts and discards the prompt: `lambda _prompt="": next(remaining)`. Recorded because `tests/test_cli.py` is the first test file in this repository to script an interactive loop, so there was no local precedent to copy.


## Decision Log


- Decision: accept any ticker Yahoo Finance can price, including ETFs and companies outside the S&P 500, rather than restricting the pool to current index members.
  Rationale: user's explicit choice when asked, and it matches what `README.md`'s Live Mode section already anticipates ("Users can ask the system to analyze candidates, including ETF"). Restricting to index membership would have been substantially less work — the ordinary live snapshot already holds every member's prices and returns — but it would have made the selection nearly pointless, since the agents already cover exactly that universe. The cost of the broader choice is this plan's ticker-scoped fetch and merge-write layer, which exists solely so an out-of-universe symbol has prices and returns available to the optimizer.
  Date/Author: 2026-09-05, decided during the planning interview.
- Decision: validate and ingest in a single function (`validate_and_ingest_tickers`) and call it synchronously on every add, including once over the remembered pool at the start of each session.
  Rationale: "does this ticker exist" can only be answered by asking Yahoo Finance for its prices, and those same prices are exactly what the optimizer needs afterwards — separating the two would mean fetching twice for no benefit. Doing it at add time rather than deferring to the end is what lets the person see `Ignored (not found): ZZZZQQQ.` in the same breath as their typing; deferring would surface a typo much later as a confusing optimizer result or crash. Re-validating the remembered pool at session start is necessary rather than merely cautious, because each session gets a brand-new empty scratch database, so a remembered ticker's data has to be re-fetched anyway — and doing so is what catches a symbol that has stopped resolving since it was saved.
  Date/Author: 2026-09-05, decided during design.
- Decision: `open_pipeline_session` routes `user_provided` through `build_live_snapshot` unconditionally, even for a rebalance date inside the stored 2020-2024 window that the cache covers.
  Rationale: this selection writes its user-supplied tickers' prices and returns into whichever database it is handed, and `data/portfolio.duckdb` is the shared historical cache that every backtest reads — mutating it as a side effect of one person's interactive session would be a silent, hard-to-diagnose corruption of everyone else's results. Handing this selection a throwaway snapshot instead makes that structurally impossible. The snapshot is nearly free here because of the next decision. Verified by checksumming the cache before and after a backtest-date run: unchanged (see Artifacts).
  Date/Author: 2026-09-05, decided during design.
- Decision: `build_live_snapshot` short-circuits for `user_provided`, creating empty `prices`/`unresolved_tickers`/`returns` tables instead of fetching anything.
  Rationale: this selection reads none of what the ordinary snapshot builds. It has no use for `sp500_membership` (its candidates come from the person, not the index), no use for `factors`/`momentum` (no rule is applied), and no use for the news archive (LLM-F never runs). Building them anyway would mean a fresh Wikipedia scrape and ~500 tickers of price history before the person is even shown their pool. This extends the "skip the call, not just its result" discipline `plans/06_interactive_flow.md` already applies to the two agents.
  Date/Author: 2026-09-05, decided during design.
- Decision: persist the pool exactly once in the confirmation loop, when the person confirms they are done — but on every accepted edit in the post-run `_run_edit_loop`.
  Rationale: user's explicit choice for the first half; the asymmetry follows from the loops' different shapes rather than being an inconsistency. The confirmation loop has a distinct "done" step, so there is an unambiguous moment at which the person has committed to a pool, and in-progress edits should not reach disk before it. The post-run loop has no such step — its `[f]inish` ends the whole command — so "save when they are finished" and "save on each edit" collapse to the same thing there, and saving per edit additionally means an interrupted session does not lose the work. In both loops the save is ordered after the empty-pool guard, so an edit that would leave the pool empty is never written.
  Date/Author: 2026-09-05, decided during the planning interview (persistence timing) and design (the asymmetry).
- Decision: the post-run `_run_edit_loop` continues to run for `user_provided`, as it does for every other selection, but becomes validation- and persistence-aware only for it, via a defaulted `selection` parameter.
  Rationale: user's explicit choice. The parameter defaults to `"llm_s_only"` so every existing call and test is unaffected, and the branch is narrow: one `if` inside the existing add case, plus one save call after the existing empty guard. A candidate list produced by an agent is one run's ephemeral output and is deliberately still never saved — pinned by `test_run_edit_loop_non_user_provided_never_touches_candidate_memory`.
  Date/Author: 2026-09-05, decided during the planning interview.
- Decision: the confirmation loop's "I am finished" keyword is `[d]one`, while the post-run loop keeps its existing `[f]inish`.
  Rationale: they mean different things and should not look identical. Confirming the pool moves the session forward into the optimizer; finishing the post-run loop ends the command. Using the same word for both would suggest the first one exits.
  Date/Author: 2026-09-05, decided during design.
- Decision: store the pool as a flat list of tickers plus a timestamp, and do not implement the rest of the persistent-memory architecture `README.md` describes.
  Rationale: `README.md`'s Live Mode section describes a much larger design — `memory/rules.json`, three separately-tracked candidate sets ($S$, $S \cap F$, $U$) with per-set staleness rules, and several `*-summary.md` files — none of which exists, as `plans/08_consistency_review.md` Finding 11 already records, recommending a dedicated future plan for it. Building a flat pool for this one selection delivers the requested behavior without pre-committing that larger design, and does not conflict with it: a future plan can migrate this file's shape, and `load_candidate_pool` fails loudly rather than silently on a shape it does not recognize, which is what makes such a migration safe to detect.
  Date/Author: 2026-09-05, decided during the planning interview.
- Decision: fix the pre-existing empty-list revert bug in `_run_edit_loop` as part of this change rather than leaving it or filing it separately.
  Rationale: it is a one-line fix inside the exact `if not candidates:` branch this plan already modifies, and this plan is what makes it consequential — adding persistence to that path is precisely what would turn a stale in-memory value into a wrongly-saved file. Leaving it would have meant knowingly building a save on top of a known-broken revert.
  Date/Author: 2026-09-05, decided during implementation.


## Outcomes & Retrospective


The stated purpose is met. `--selection user_provided` shows the remembered pool, loops on add/remove until the person confirms, names bad tickers without discarding good ones typed alongside them, saves the confirmed pool, and feeds it through the unchanged optimizer and allocation steps. All three real end-to-end runs behaved as specified, including with an ETF (`SPY`) and on a backtest-window date, and the shared cache was byte-identical afterwards. The suite grew from 141 to 188 tests, all passing.

Two things went better than expected. The existing code turned out to be well shaped for this: `edit_candidates` already accepted a bare `{"candidates": [...]}` dictionary, so the validating variant could reuse its set arithmetic verbatim, and every downstream function already took a `db_path` parameter, so pointing the optimizer at a scratch database required no new plumbing at all. And `open_pipeline_session` already kept its snapshot open across the whole command, so the interactive loop could ingest tickers mid-session without any change to that lifecycle.

One thing is worth flagging as a real limitation rather than a gap to be tidied away. Because each session gets a fresh scratch database, every remembered ticker is re-fetched from Yahoo Finance at the start of every run. For a pool of a handful of tickers this is a second or two and entirely reasonable; for a pool of many dozens it would become the slowest part of starting up. The obvious remedy — a persistent per-ticker price cache keyed by ticker and window — was deliberately not built here, because it is a caching design with its own staleness questions and belongs with the broader memory plan `plans/08_consistency_review.md` Finding 11 already calls for, not bolted onto this selection.

What remains, all explicitly out of scope for this plan and none of it blocking: the wider `memory/*` architecture from `README.md` (rules, the three tracked sets, the summary files); the per-ticker price cache just described; and the integration `plans/07_external_candidate_screening.md` anticipated, where a user-added off-index candidate could also be screened against the current LLM-S rule to show *why* it might belong in the pool, rather than only being priced. That last one composes cleanly with this work — `screen_external_candidate` already exists and takes exactly the kind of ticker this selection now accepts — and is the most natural next step for anyone extending this.


## Context and Orientation


This repository builds monthly stock portfolios. The relevant pipeline, all under `src/`, runs in four stages, and this plan changes only the first one.

The first stage decides *which* tickers are even eligible — the "candidate pool". Until this plan, that was always the job of one or both LLM agents: `src/agents/llm_s.py` and `src/agents/llm_s_signals.py` (called LLM-S, which writes a screening rule from company fundamentals and applies it to every S&P 500 member) and `src/agents/llm_f_signals.py` (called LLM-F, which reads recent news headlines per member). `src/scanner/candidate_scanner.py`'s `scan_with_detail` combines their votes into a final list. The second stage, `src/optimizer/portfolio.py`, reads a `returns` table to build a matrix of each candidate's monthly returns (`load_returns_matrix`) and computes portfolio weights from it (`compute_weights`, with `objective` one of `GMV`, `MV`, `MSR`). The third stage turns weights into whole share counts against the latest prices (`load_latest_prices`, `allocate_shares`). The fourth is just printing, in `src/flow/cli.py`.

Two facts about the data layer matter for this plan. Everything downstream reads its inputs by SQL against a `db_path` parameter — a path to a DuckDB database file — which is why pointing the whole pipeline at a different database requires no code changes downstream. And there are two kinds of such database. `data/portfolio.duckdb` is the shared historical cache, built once by `plans/01_dataset.md`'s modules, covering the stored 2020-01-01..2024-04-30 backtest window. For any date outside that window, `src/flow/live.py`'s `build_live_snapshot` instead builds a throwaway temporary database with freshly fetched data and deletes it when the command exits. `src/flow/interactive.py`'s `open_pipeline_session` is what chooses between them, and it deliberately holds whichever one it chose open for the entire command, so the interactive editing loop can recompute weights repeatedly without re-fetching anything.

Three terms recur below. A *rebalance date* is the month the portfolio is being built for; `--date today` means live mode. The `prices` table holds daily closing prices per ticker; the `returns` table holds one trailing-one-month return per ticker per month, computed from `prices`. An *upsert* (a word this plan introduces) means a write that replaces only the rows for the tickers it was asked about, leaving every other ticker's rows in place — as opposed to the existing writers in `src/dataset/prices.py` and `src/dataset/returns.py`, which drop and rebuild their whole table.


## Plan of Work


The work divides into a persistence file, a data-layer path for arbitrary tickers, and the wiring that exposes it as a selection.

Persistence is a new module, `src/flow/candidate_memory.py`, holding only `load_candidate_pool(path) -> list[str]` and `save_candidate_pool(tickers, path) -> None` plus the `DEFAULT_CANDIDATES_PATH = "memory/candidates.json"` constant. It is placed under `src/flow/` rather than `src/dataset/` because it has nothing to do with Yahoo Finance, which `AGENTS.md` asks to keep isolated. It is the first module in this repository to read or write JSON, so it establishes that pattern: `json.dumps(..., indent=2)`, and `Path(path).parent.mkdir(parents=True, exist_ok=True)` before writing, matching the directory-creation idiom already used in `src/dataset/membership.py`.

The data-layer path exists because a user-supplied ticker needs prices and returns in the session database, and neither existing writer can do that without destroying other tickers' rows. In `src/dataset/prices.py`, add `fetch_and_reshape_for_tickers(tickers, start, end, batch_size)` — the fetch/reshape/detect sequence `build_price_history` already performs, factored out so it can run against an explicit ticker list instead of `load_ticker_universe`'s membership-derived one — and `upsert_prices_tables(prices_df, unresolved_df, tickers, db_path)`, which creates the two tables if absent, deletes the rows for `tickers` in both, then inserts. Refactor `build_price_history` to call the new fetch helper so the sequence exists once. Give `detect_unresolved_tickers` optional `start`/`end` parameters, defaulted to the module-wide window so existing callers are unaffected, so its message names the window actually used. In `src/dataset/returns.py`, mirror this with `upsert_returns_table(df, tickers, db_path)` and `build_returns_for_tickers(tickers, db_path, start, end)`, the latter being `build_returns` with an explicit ticker list and an upsert instead of an overwrite. Leave every existing function's signature and behavior alone.

Tying those together is a new module, `src/dataset/ticker_ingestion.py`, with the single function `validate_and_ingest_tickers(tickers, as_of, db_path) -> tuple[list[str], dict[str, str]]`. It normalizes and de-duplicates its input, returns immediately without any network call if that leaves nothing, computes the same 65-month lookback window `src/flow/live.py` uses, fetches, upserts prices, builds returns, and returns the tickers that resolved alongside a mapping from each that did not to the reason. It catches the `ValueError` that `_build_symbol_map` raises for a batch whose symbols collide and reports the whole batch as invalid rather than letting it escape into an interactive loop. `LOOKBACK_MONTHS = 65` is duplicated here rather than imported, because a `src/dataset/` module must not depend on `src/flow/`.

The wiring is small and additive. In `src/flow/live.py`, add `_init_empty_price_and_returns_tables(db_path)` and branch at the top of `build_live_snapshot`'s `try` so `user_provided` calls it instead of the existing fetch-and-build sequence. In `src/flow/interactive.py`: append `"user_provided"` to `VALID_SELECTIONS` (both the argument parser's accepted values and `run_pipeline`'s validation read that one tuple, so nothing else needs editing for the name to be accepted); rewrite `open_pipeline_session` to compute `mode` first and then route this selection to a snapshot regardless of date; add a short-circuit as the first statement of `run_scan` returning the six-key dictionary shape `scan_with_detail` produces, with `branch="user_provided"` and the four count fields `None`, so `src/flow/cli.py`'s existing printing code needs no changes; thread a defaulted `candidates` parameter through `run_scan`, `run_pipeline_against`, and `run_pipeline`; and add `validate_and_edit_candidates(pool, add, remove, as_of, db_path)`, which validates the additions and then delegates to the existing `edit_candidates` for the set arithmetic.

In `src/flow/cli.py`, add `_run_user_provided_confirm_loop`, the `--memory-path` argument, and the small `selection`-dependent branch in `_run_edit_loop`, plus the shared `_print_add_outcome` helper both loops use to report which added tickers resolved. Finally, add `memory/` to `.gitignore`.


## Concrete Steps


Run everything from the repository root, `/app/agentic_portfolio`.

To see the feature work, with a scratch memory file so nothing is left behind:

    uv run python -m src.flow.cli --date today --objective GMV --value 100000 \
        --selection user_provided --memory-path /tmp/candidates.json

Type `a`, then `AAPL SPY ZZZZQQQ`, then `d`. Expect the transcript shown in Purpose above: `Added: AAPL, SPY.` followed by `Ignored (not found): ZZZZQQQ.`, then a saved-pool line, then the weights and share allocation. Yahoo Finance also prints its own `HTTP Error 404` and `1 Failed download: ['ZZZZQQQ']` noise to stderr for the bogus symbol; that is `yfinance` reporting the same fact the loop reports, not an error in this code.

Run the same command again to see the pool remembered. Expect it to open with `Current candidate pool (2): AAPL, SPY` instead of `(empty)`.

To run the tests:

    uv run pytest tests/test_*.py

Expect `188 passed`. To run only what this plan added:

    uv run pytest tests/test_candidate_memory.py tests/test_ticker_ingestion.py \
        tests/test_dataset_upsert.py tests/test_cli.py

Expect `39 passed`. Every test in these four files fails or errors before this plan's changes, since each imports something the change introduces.


## Validation and Acceptance


Acceptance is behavioral, in five parts, each verified for real and recorded in Artifacts below.

A person running `--selection user_provided` with no saved pool is shown `Current candidate pool (0): (empty)` and prompted, rather than the command failing or silently optimizing nothing. Adding `AAPL SPY ZZZZQQQ` on one line adds `AAPL` and `SPY`, prints `Ignored (not found): ZZZZQQQ.`, and leaves the pool at two tickers — this is the specific "handle the good tickers and say which one is wrong" behavior the feature exists for, and `SPY` being an ETF confirms the pool is not restricted to index members. Confirming with `d` writes the pool to the `--memory-path` file and proceeds to weights and a share allocation. Re-running the same command opens with the two remembered tickers. And running with a rebalance date inside the stored window (for instance `--date 2024-03-01`) reports `Mode: backtest`, works identically, and leaves `data/portfolio.duckdb` byte-identical — checked with `md5sum` before and after.

For the automated suite, `uv run pytest tests/test_*.py` reports 188 passed, up from 141. The four new files (`tests/test_candidate_memory.py`, `tests/test_ticker_ingestion.py`, `tests/test_dataset_upsert.py`, `tests/test_cli.py`) contribute 39 tests, and the remaining 8 are added to `tests/test_interactive_flow.py` (7) and `tests/test_dataset.py` (1). Per `AGENTS.md`, no test calls Yahoo Finance or an LLM: `tests/test_ticker_ingestion.py` and `tests/test_cli.py` monkeypatch the fetch at its point of use, and `tests/test_dataset_upsert.py` monkeypatches `fetch_price_history` while exercising the real DuckDB writes against small hand-built databases under `tmp_path`.

Three of those tests are worth calling out as the ones that pin this plan's actual claims. `test_run_scan_user_provided_never_calls_either_agent_or_the_scanner` asserts, with spies, that `generate_rule`, `screen`, `screen_month`, and `scan_with_detail` are each genuinely never invoked — not merely that their results are absent. `test_open_pipeline_session_user_provided_uses_a_snapshot_even_for_a_backtest_date` asserts the shared cache path is never yielded for this selection. And `test_run_edit_loop_non_user_provided_never_touches_candidate_memory` asserts the other three selections' behavior is unchanged, which is the regression that would matter most.


## Idempotence and Recovery


Every step here is safe to repeat. The upsert functions delete the rows for the tickers they are given before inserting, so ingesting the same ticker twice leaves exactly one copy — pinned by `test_upsert_prices_tables_is_idempotent_for_an_unchanged_ticker`. `save_candidate_pool` overwrites its file wholesale, and `load_candidate_pool` treats a missing file as an empty pool, so deleting `memory/candidates.json` is a complete, safe reset: the next run simply starts from an empty pool.

Nothing this feature does can damage the shared historical cache, structurally rather than by convention: a `user_provided` session is always handed a temporary database, and `src/flow/live.py`'s existing `finally` clause deletes that file on exit whether the command succeeded, failed, or was interrupted. If a run is interrupted mid-loop, the only lasting effect is that `memory/candidates.json` holds whatever was last saved — the confirmed pool from a previous run, or, in the post-run loop, the most recent accepted edit.

The one recoverable failure worth naming is a `memory/candidates.json` that has been hand-edited into a shape `load_candidate_pool` does not accept. It raises `ValueError` naming the file and the offending field rather than silently starting from an empty pool; the fix is to correct the `tickers` field or delete the file.


## Artifacts and Notes


Adding a mix of valid and invalid tickers, live mode — the acceptance transcript, abridged of `yfinance`'s stderr noise:

    Current candidate pool (0): (empty)

    Edit candidate pool? [a]dd tickers / [r]emove tickers / [d]one: a
    Ticker(s) to add (space-separated): AAPL SPY ZZZZQQQ
    Added: AAPL, SPY.
    Ignored (not found): ZZZZQQQ.
    Candidate pool (2): AAPL, SPY

    Edit candidate pool? [a]dd tickers / [r]emove tickers / [d]one: d
    Saved 2 ticker(s) to /tmp/candidates.json.
    Mode: live  Rebalance date: 2026-09-05  Objective: GMV  Selection: user_provided

    Scanner branch: user_provided  (buy_s=None buy_f=None intersection=None union=None)
    Candidates (2): AAPL, SPY

    Weights:
      SPY: 0.9802
      AAPL: 0.0198

    Share allocation:
      AAPL: 6
      SPY: 126
    Leftover cash: $611.32

The file that produced, showing the persisted shape:

    {
      "tickers": [
        "AAPL",
        "SPY"
      ],
      "updated_at": "2026-09-05T00:41:15.852126+00:00"
    }

The second run over that file, removing a ticker and switching objective — the pool is remembered, and the reduced pool is saved back:

    Current candidate pool (2): AAPL, SPY
    Edit candidate pool? [a]dd tickers / [r]emove tickers / [d]one: r
    Ticker(s) to remove (space-separated): AAPL
    Candidate pool (1): SPY
    Edit candidate pool? [a]dd tickers / [r]emove tickers / [d]one: d
    Saved 1 ticker(s) to /tmp/candidates.json.
    Mode: live  Rebalance date: 2026-09-05  Objective: MSR  Selection: user_provided
    Candidates (1): SPY
    Weights:
      SPY: 1.0000
    Share allocation:
      SPY: 64
    Leftover cash: $517.12

A backtest-window date, also exercising the post-run edit loop's validated add, with the cache checksummed before and after. Note `Mode: backtest` alongside an unchanged cache, which is the point of the always-snapshot routing decision:

    $ md5sum data/portfolio.duckdb
    350a13c05ddcfba0b64e1228013de5ef  data/portfolio.duckdb

    Current candidate pool (0): (empty)
    Edit candidate pool? ... : a
    Ticker(s) to add (space-separated): MSFT
    Added: MSFT.
    Candidate pool (1): MSFT
    Edit candidate pool? ... : d
    Saved 1 ticker(s) to /tmp/bt.json.
    Mode: backtest  Rebalance date: 2024-03-01  Objective: GMV  Selection: user_provided
    Candidates (1): MSFT
    Weights:
      MSFT: 1.0000
    Share allocation:
      MSFT: 49
    Leftover cash: $29.20

    Edit candidates? [a]dd tickers / [r]emove tickers / [o]bjective / [f]inish: a
    Ticker(s) to add (space-separated): NVDA BADTICKER1
    Added: NVDA.
    Ignored (not found): BADTICKER1.
    Candidates (2): MSFT, NVDA
    Weights:
      MSFT: 1.0000

    $ md5sum data/portfolio.duckdb
    350a13c05ddcfba0b64e1228013de5ef  data/portfolio.duckdb

`NVDA` receiving no printed weight there is the `GMV`-concentration effect explained in Surprises & Discoveries, not a data problem — the direct check recorded there confirms `NVDA` had all 60 months of returns and was kept in the optimizer's matrix.

Test suite after the change:

    $ uv run pytest tests/test_*.py
    188 passed in 34.28s


## Interfaces and Dependencies


No new third-party dependencies. Everything here uses libraries already declared in `pyproject.toml`: `duckdb`, `pandas`, `yfinance` (only indirectly, through the existing functions in `src/dataset/prices.py`), and the standard library's `json`, `pathlib`, and `datetime`.

In `src/flow/candidate_memory.py`, define:

    DEFAULT_CANDIDATES_PATH = "memory/candidates.json"

    def load_candidate_pool(path: str = DEFAULT_CANDIDATES_PATH) -> list[str]
    def save_candidate_pool(tickers: list[str], path: str = DEFAULT_CANDIDATES_PATH) -> None

In `src/dataset/ticker_ingestion.py`, define:

    LOOKBACK_MONTHS = 65

    def validate_and_ingest_tickers(
        tickers: list[str],
        as_of: date,
        db_path: str,
    ) -> tuple[list[str], dict[str, str]]

In `src/dataset/prices.py`, add:

    def fetch_and_reshape_for_tickers(
        tickers: list[str],
        start: str,
        end: str,
        batch_size: int = settings.price_batch_size,
    ) -> tuple[pd.DataFrame, pd.DataFrame]

    def upsert_prices_tables(
        prices_df: pd.DataFrame,
        unresolved_df: pd.DataFrame,
        tickers: list[str],
        db_path: str = settings.db_path,
    ) -> None

and extend `detect_unresolved_tickers` with `start: str = settings.fetch_start, end: str = settings.fetch_end`.

In `src/dataset/returns.py`, add:

    def upsert_returns_table(df: pd.DataFrame, tickers: list[str], db_path: str = settings.db_path) -> None
    def build_returns_for_tickers(tickers: list[str], db_path: str, start: str, end: str) -> pd.DataFrame

In `src/flow/live.py`, add:

    def _init_empty_price_and_returns_tables(db_path: str) -> None

In `src/flow/interactive.py`, `VALID_SELECTIONS` becomes:

    VALID_SELECTIONS = ("llm_s_only", "llm_f_only", "llm_s_and_f", "user_provided")

and add:

    def validate_and_edit_candidates(
        pool: list[str],
        add: list[str],
        remove: list[str],
        as_of: date,
        db_path: str,
    ) -> tuple[list[str], list[str], dict[str, str]]

`run_scan`, `run_pipeline_against`, and `run_pipeline` each gain a `candidates: list[str] | None = None` parameter, defaulted so every existing caller — including `src/flow/backtest.py`'s `run_full_backtest` — is unaffected.

In `src/flow/cli.py`, add:

    def _print_add_outcome(valid_added: list[str], invalid: dict[str, str]) -> None

    def _run_user_provided_confirm_loop(
        initial_pool: list[str],
        rebalance_date: date,
        db_path: str,
        memory_path: str = DEFAULT_CANDIDATES_PATH,
    ) -> list[str]

and `_run_edit_loop` gains `selection: str = "llm_s_only"` and `memory_path: str = DEFAULT_CANDIDATES_PATH`.
