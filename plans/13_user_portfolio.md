# Remember the user's own portfolio, per currency, and report its return, volatility and Sharpe ratio


This ExecPlan is a living document. The sections `Progress`, `Surprises & Discoveries`, `Decision Log`, and `Outcomes & Retrospective` must be kept up to date as work proceeds. This plan must be maintained in accordance with `PLANS.md` at the repository root. This plan builds on `plans/05_optimizer_and_allocation.md` (for `load_returns_matrix`, `apply_min_history_rule`, `load_latest_prices` and `allocate_shares`), `plans/06_interactive_flow.md` (for the CLI, its printing functions and live mode's throwaway snapshot), `plans/09_user_provided_selection.md` (for the `user_provided` selection and its candidate memory file), `plans/10_performance_reporting_and_target_return.md` (for `PortfolioStats` and `print_weights_and_allocation`), `plans/11_non_us_tickers_and_single_currency.md` (for per-currency pools, the `ticker_currency` table and the single-currency rule), and `plans/12_benchmark_per_candidate_pool.md` (for `BenchmarkStats`, `prepare_benchmark` and the scratch-database isolation rule) — all six checked into this repository.


## Purpose / Big Picture


Before this change, this project knew only about **candidate pools**: lists of tickers a person is *considering*, saved per currency in `memory/candidates.json` and handed to the GMV/MV/MSR optimizer, which answers "what weights *should* I hold?". Nothing anywhere recorded what the person **actually owns**. So the obvious complementary question was unanswerable: what have my real holdings done, and how do they compare with what the optimizer suggests, or with simply buying the market?

After this change there is a second, separate memory: the user's own portfolio, stored per currency in `memory/portfolio.json` as a list of tickers and the number of shares held of each. A new command maintains it, and both that command and every ordinary `uv run portfolio ...` run print that portfolio's annualized return, annualized volatility and Sharpe ratio, computed from the number of shares actually held.

Maintaining it looks like this:

    $ uv run portfolio-holdings set SPY 1000 T 500
    Added to the USD portfolio: SPY (1000 shares), T (500 shares).

    Your portfolio (USD), from memory/portfolio.json:
      SPY: 1000 shares  $687,420.00 USD  weight 0.9691
      T:    500 shares   $21,905.00 USD  weight 0.0309
    Total value: $709,325.00 USD
    Returns window: 2021-10-01 to 2026-09-01 (60 month(s) of monthly returns)
    Annual return: 0.1198  Annual volatility: 0.1451  Sharpe: 0.6879
    Risk-free rate used: 0.0200

And the same block appears at the end of an ordinary pipeline run, directly under the figures for the pool the optimizer just solved and the benchmark it was measured against, so all three can be read together:

    $ uv run portfolio --date today --objective GMV --value 100000 --selection user_provided
    ...
    Portfolio expected return: 0.1507  Portfolio volatility: 0.1958  Portfolio Sharpe: 0.6675
    Benchmark SPY: return=0.1225  volatility=0.1481  Sharpe=0.6919  (60 of 60 month(s))
    ...
    Your portfolio (USD), from memory/portfolio.json:
      ...
    Annual return: 0.1198  Annual volatility: 0.1451  Sharpe: 0.6879

Read together, those three lines answer a question none of them answers alone: the optimizer's suggested pool expects more return than what the person holds, at more risk; the market beat both on Sharpe; and the portfolio actually held sits between them. That comparison is what this change buys, and it is only meaningful because all three sets of figures are produced by the *same estimators over their own honestly-reported windows* — a requirement this plan treats as the central correctness constraint, not a nicety.

Define the terms used throughout, in plain language:

- A **position** (or **holding**) is one ticker plus the number of shares of it the user owns, for example `SPY, 1000`. Share counts may be fractional, because brokers sell fractional shares.
- A **weight** is a holding's share of the portfolio's total market value: `shares × price` divided by the sum of `shares × price` over all holdings. Weights are what the return/volatility/Sharpe arithmetic consumes; share counts are what the user maintains. Converting the one into the other is why this feature needs a current market price at all.
- **Annualized return** here means the geometric (compounding) mean of monthly returns raised to an annual figure, which is what PyPortfolioOpt's `mean_historical_return(..., frequency=12)` computes.
- **Annualized volatility** means the standard deviation of monthly returns scaled to a year, taken as the square root of the diagonal of an annualized covariance matrix.
- The **Sharpe ratio** is `(annual return - risk-free rate) / annual volatility`: return earned per unit of risk taken, above what a riskless asset would have paid.
- A **returns window** is the specific run of consecutive months of monthly returns the three figures were estimated from — for example 2021-10-01 to 2026-09-01, sixty months. It is derived from the data actually available, never assumed, and it is always printed, because the same portfolio measured over a different window is a different number.


## Progress


- [x] (2026-09-06 04:10Z) Explored the existing code and settled the five open design questions with the user (entry-point name, storage location, both-commands reporting, exclude-and-name on thin history, benchmark-style data resolution). Recorded in `Decision Log`.
- [x] (2026-09-06 04:15Z) Wrote this ExecPlan.
- [x] (2026-09-06 04:22Z) Milestone 1 — the memory store: `src/flow/user_portfolio.py` and `tests/test_user_portfolio.py` (18 tests).
- [x] (2026-09-06 04:29Z) Milestone 2 — the figures: `_estimate_mu_and_cov` and `stats_for_weights` in `src/optimizer/portfolio.py`, `load_returns_matrix_unfiltered` split out of `load_returns_matrix`, `src/optimizer/holdings.py`, `src/flow/interactive.py`'s `prepare_holdings`, and `tests/test_holdings.py` (15 tests). `tests/test_optimizer.py` passes unmodified.
- [x] (2026-09-06 04:33Z) Milestone 3 — the entry point: `src/flow/holdings_cli.py`, `src/flow/cli.py`'s `print_user_portfolio`, the `pyproject.toml` script registration, and `tests/test_holdings_cli.py` (26 tests).
- [x] (2026-09-06 04:40Z) Milestone 4 — the pipeline section and docs: the three new `cli.main` flags, the holdings block after `print_pipeline_result`, five new `tests/test_cli.py` tests, the two `README.md` bullets, and this plan's living sections. Full suite: 424 passed.
- [x] (2026-09-06 07:10Z) Milestone 5a — one session, one window: `open_holdings_session` and `measure_holdings` in `src/flow/interactive.py`, a context-manager sibling of `open_pipeline_session` yielding one throwaway database with the holdings' prices and returns already resolved into it, so every variant an interactive loop measures shares one returns window and one fetch.
- [x] (2026-09-06 07:14Z) Milestone 5b — the before/after report: a keyword-only `heading` on `src/flow/cli.py`'s `print_user_portfolio` (the current wording stays the default, so no existing caller or test changed), and `format_holdings_delta` printing the signed change in return, volatility and Sharpe - withholding the delta with a stated reason when either side has no figures or the two windows differ.
- [x] (2026-09-06 07:22Z) Milestone 5c — the loop: `whatif` in `VALID_COMMANDS` and `_run_whatif`/`_whatif_set`/`_whatif_apply`/`_whatif_set_command` in `src/flow/holdings_cli.py`, prompting `[s]et shares / [r]emove / [u]ndo all / [f]inish`, reusing `_validate_set_targets` (which gained `db_path` and `pool_currency`) so a cross-currency ticker is refused by name exactly as `set` refuses it, calling neither `save_portfolio` nor `_remember_rate`, and closing with the `set` command that would apply the experiment. 21 new tests in `tests/test_holdings_cli.py`.
- [x] (2026-09-06 07:26Z) Milestone 5d — closed Milestone 2's test gap: the four `prepare_holdings` tests this plan specified had never been written (`grep -rn prepare_holdings tests/` found only monkeypatches and docstrings). They land now, beside six new `open_holdings_session`/`measure_holdings` tests, in `tests/test_holdings.py`.
- [x] (2026-09-06 07:34Z) Milestone 5e — documentation: the `holdings_cli` module docstring's non-interactive claim narrowed to the subcommands that change the record, a `README.md` bullet, and this plan's living sections. Full suite: 544 passed.
- [x] (2026-09-06 08:02Z) Milestone 6a — the cache: `src/dataset/holdings_cache.py` and `tests/test_holdings_cache.py` (16 tests). A ticker is stale when the cache lacks the monthly return for the most recent rebalance date on or before the report's date - derived from the data, so there is no `fetched_at` to drift out of agreement with the rows it describes. `--refresh-holdings` forces it.
- [x] (2026-09-06 08:09Z) Milestone 6b — the honest price date: `HoldingsStats.priced_as_of`, `latest_price_date` in `src/optimizer/portfolio.py`, and `Total value: ... (priced YYYY-MM-DD)` in `print_user_portfolio`. The monthly rule keeps the returns current but lets the prices behind `Total value` age, so the report states how current they are rather than leaving a reader to assume.
- [x] (2026-09-06 08:18Z) Milestone 6c — wiring: `_resolve_holdings` and `open_holdings_session` resolve through the cache instead of a throwaway database, `_validate_set_targets` gained `refresh_cache` so a re-tried what-if ticker costs nothing, and both CLIs gained `--holdings-cache-path` and `--refresh-holdings`. `--no-holdings-fetch` now reaches the cache, since reading a local file is not a network call.
- [x] (2026-09-06 08:52Z) Item 5f — made Milestone 5's new-ticker capability discoverable: `whatif` has always accepted a ticker the user does not own, but nothing the program printed said so, and the repository owner reasonably read the loop as editing existing holdings only. Six user-facing strings reworded in `src/flow/holdings_cli.py`; no logic, no behaviour change, two tests pinning the prompts. Full suite: 569 passed.
- [x] (2026-09-06 08:26Z) Milestone 6d — documentation: this plan's living sections, a `README.md` bullet, and the superseded `Decision Log` entry marked as such. Full suite: 567 passed.
- [x] (2026-09-06 09:40Z) Milestone 7a — the window as a validated value: `DEFAULT_LOOKBACK_MONTHS` and `validate_lookback_months` in `src/optimizer/holdings.py`, refusing anything outside 24-60 by name, and `holdings_stats`' bare `60` default replaced by the constant.
- [x] (2026-09-06 09:46Z) Milestone 7b — threading: `lookback_months` on `interactive.measure_holdings` and `_holdings_stats_excluding` and their `holdings_stats` call sites. `prepare_holdings` deliberately untouched, which keeps `show`, `set`, `remove` and every `uv run portfolio` block on the fixed 60 - and keeps the tested pool/benchmark/holdings one-scale guarantee true.
- [x] (2026-09-06 09:55Z) Milestone 7c — the loop: `[w]indow` in `WHATIF_PROMPT` and `_whatif_window` in `src/flow/holdings_cli.py`, with the window as loop state and a re-measure of the baseline on change. Also stopped labelling an unchanged portfolio "What if - not saved", which a window-only edit made newly visible.
- [x] (2026-09-06 10:02Z) Milestone 7d — saying the window was chosen: `window_origin` on `print_user_portfolio`, so `Returns window: ... (36 month(s) of monthly returns, 36 requested)` separates a chosen window from all the data there was. 21 new tests; full suite 590 passed.


## Surprises & Discoveries


- Observation: `apply_min_history_rule` cannot safely be called with `min_months=0`, which the first draft of Milestone 2 intended in order to obtain the pre-rule matrix. A column with no data at all reaches `positions[0]` on an empty `np.flatnonzero` result and raises `IndexError`; the function is only safe because every existing caller passes `min_months >= 1`, which catches an all-null column on the count branch first. Rather than harden a working function for one new caller's convenience, `load_returns_matrix` was split: `load_returns_matrix_unfiltered` does everything except the rule, and `load_returns_matrix` is now that plus `apply_min_history_rule`. This turned out better than the original plan anyway, because the unfiltered matrix is exactly what the exclusion messages need - `apply_min_history_rule` drops a column without recording how many months it had, so "NEWCO was excluded" could otherwise never become the actionable "NEWCO has 8 months, and 24 are needed".
  Evidence: `src/optimizer/portfolio.py`'s `apply_min_history_rule`, lines beginning `positions = np.flatnonzero(non_null.to_numpy())`.

- Observation: the comparability contract holds in a real run, not just in a fixture. A portfolio holding only `SPY` reports the identical three figures the `Benchmark SPY` line reports over the same window, to all four printed decimals - which is what makes it legitimate to print the two blocks in one report and compare them.
  Evidence: a live run on 2026-09-06 printed `Benchmark SPY: return=0.1225  volatility=0.1481  Sharpe=0.6919  (60 of 60 month(s))` and, in the holdings block below it, `Annual return: 0.1225  Annual volatility: 0.1481  Sharpe: 0.6919`. Pinned in `tests/test_holdings.py::test_a_one_holding_portfolio_equals_its_own_benchmark`.

- Observation: `pypfopt`'s `mean_historical_return` emits `UserWarning: Some returns are NaN` for a holdings matrix containing a recently-listed ticker, and this is correct rather than a bug to silence. `_load_window_dates` derives the window from `SELECT DISTINCT rebalance_date FROM returns` over the WHOLE table, so a portfolio holding both a long-lived and a new ticker gets a window that reaches into months the long-lived one predates the end of - leaving it with trailing nulls, which `apply_min_history_rule` correctly does not treat as a gap.
  Evidence: two tests in `tests/test_holdings.py` emit this warning; `mean_historical_return` uses each column's own non-null count and `_covariance_input` drops to the complete-overlap rows, so both estimates remain correct.

- Observation: `prepare_holdings` had to be stubbed inside `tests/test_cli.py`'s existing `_stub_main_pipeline` helper, not merely tolerated. Left real, it reads the developer's own `memory/portfolio.json` - so those pre-existing tests would have passed on a machine with no saved portfolio and made a Yahoo Finance call on one with holdings. A test whose behavior depends on untracked local state is not hermetic even when it passes.
  Evidence: `tests/test_cli.py::_stub_main_pipeline`, and its docstring.

- Observation: refetching the holdings on every run is noticeable but tolerable. A two-holding portfolio's block took roughly ten seconds of the wall clock in a live `user_provided` run (one batched `yf.download` for the prices plus one `fast_info` request per ticker with `yfinance_fundamentals_pause_seconds` between them). It scales with the number of holdings, so a thirty-holding portfolio would be worth caching; see the open `Progress` item.
  Evidence: the live acceptance runs of 2026-09-06.


- Observation: routing `whatif`'s typed tickers through `_validate_set_targets` was necessary but not sufficient - the first attempt accepted a yen ticker into a dollar experiment instead of refusing it. `partition_by_currency` takes a `pool_currency` and, when given `None`, lets the FIRST ticker establish it. That is exactly right for `set`, where `set 1321.T 50` should create a JPY portfolio; it is exactly wrong for a what-if, where the portfolio already has a currency. Fixed by giving `_validate_set_targets` a `pool_currency` parameter that `whatif` passes and `set` leaves alone.
  Evidence: `test_whatif_refuses_a_cross_currency_ticker_by_name` failed with the hypothetical containing `7203.T` and the closing line reading `To keep it: uv run portfolio-holdings set 7203.T 100` - the experiment had silently become a yen one.

- Observation: the first live run answered the question the feature exists for, and answered it counter-intuitively. Adding 100 NVDA to a 1,000-share SPY portfolio raised the expected return by 1.4 points but the volatility by 2.9, so the Sharpe ratio FELL. A person guessing which change would help would very likely have guessed wrong, which is the whole argument for computing the delta rather than eyeballing two blocks.
  Evidence: a live run on 2026-09-06 printed `Change from your saved portfolio: return +0.0138  volatility +0.0293  Sharpe -0.0113`, with both blocks over the identical `2021-10-01 to 2026-09-01 (60 month(s))` window.

- Observation: the report was initially claiming a write it had not made. `resolve_risk_free_rate` phrases an overridden rate as `--risk-free-rate, remembered for USD`, which is true of every other subcommand, so a `whatif --risk-free-rate 0.05` printed exactly that while writing nothing. The provenance line added by `plans/14` exists to stop a rate being printed without an honest source; this was its mirror image, an honest-looking source that was false.
  Evidence: the live run printed `Risk-free rate used: 0.0500 (--risk-free-rate, remembered for USD)` while `md5sum -c` confirmed `rates.json` unchanged. Now pinned by `test_whatif_does_not_claim_the_rate_was_remembered`.

- Observation: plan 13's own Milestone 2 specified four `prepare_holdings` tests that were never written - `grep -rn prepare_holdings tests/` found only monkeypatches and docstring mentions, so the function was exclusively stubbed and never directly exercised. Milestone 5a builds its sibling in the same module, which made this the moment to close the gap rather than widen it.
  Evidence: `grep -rn "prepare_holdings" tests/` before Milestone 5d returned seven hits, every one a `monkeypatch.setattr` or a docstring.

- Observation: the cache's saving is the whole network cost, and interpreter startup is what remains. A three-holding portfolio's report went from 9 seconds cold to 4 warm, and a forced refresh back to 8 - so roughly 5 seconds of Yahoo Finance disappears and the residual 4 is `uv run` starting Python and importing pandas/PyPortfolioOpt. The saving therefore scales with the number of holdings (as the original note predicted) while the floor does not.
  Evidence: live runs on 2026-09-06 against a three-holding USD portfolio - `wall: 9s` cold, `wall: 4s` warm, `wall: 8s` with `--refresh-holdings`.

- Observation: the what-if loop's add path bypassed the cache entirely at first, so re-trying a candidate refetched it every time - 7 seconds each, for data already on disk. `_validate_set_targets` called `validate_and_ingest_tickers` unconditionally because it was written when its database was always a throwaway. Fixed with a `refresh_cache` flag that routes it through the cache's staleness rule instead. Caught by timing a repeat, not by a test.
  Evidence: re-trying `NVDA 100` took `wall: 7s` before and `wall: 4s` after; a genuinely new `TSM 300` still takes 7s, which is the fetch actually being needed.

- Observation: THREE separate tests reached the real network after the cache landed, all for one reason: the fetch a holdings report makes moved from `src/flow/interactive.py`'s and `src/flow/holdings_cli.py`'s references to `src/dataset/holdings_cache.py`'s, and every existing stub was pinned to the old ones. The worst of them was not a failure at all - `test_whatif_refuses_a_cross_currency_ticker_by_name` kept passing, because the live lookup of `7203.T` agreed with the stub it had bypassed, and it left a `session.duckdb` in the repository root as the only visible trace. The rule "patch the importing module's own symbol" was followed correctly in all three; what changed was which module does the importing. Introducing a new layer between a caller and a seam invalidates every stub aimed at that seam, and a passing suite is not evidence otherwise - `git status` and a check for stray files are.
  Evidence: `data/holdings.duckdb` contained a single ticker, `NEWCO`, fetched by a test fixture; `session.duckdb` appeared untracked in the repository root with the four tables `_init_empty_price_and_returns_tables` creates. Both are now impossible: `_stub_ingest` and `_no_fetch` each patch both modules.

- Observation: adding the cache silently moved a monkeypatch target and let a real network call escape into the test suite. `tests/test_holdings.py` patched `src.flow.interactive.validate_and_ingest_tickers`, but the fetch a holdings report makes now goes through `src.dataset.holdings_cache`'s own reference - so the stub stopped intercepting and the real function ran, reporting yfinance's genuine "no non-null close/adj_close values" message for a fixture ticker. This is precisely the failure mode the project's patch-the-importing-module rule exists to prevent, arriving from the other direction: the rule was followed, and the importing module changed underneath it.
  Evidence: `test_prepare_holdings_turns_an_ingest_failure_into_a_reported_reason` failed asserting `"yfinance exploded" in stats.unavailable_reason` and got yfinance's own wording instead. `_no_fetch` now patches both modules, and the new tests patch `src.dataset.holdings_cache`'s symbol.

- Observation: the monthly staleness rule needs the month-boundary case handled or the cache is permanently stale for a day or two every month. `latest_expected_rebalance_date(date(2026, 11, 1))` must be October's first business day, not November's, because 2026-11-01 is a Sunday and November's return cannot exist yet. Getting it wrong would make every run that weekend see a "stale" cache and refetch.
  Evidence: pinned by `test_a_report_before_its_months_first_business_day_expects_the_previous_month`, and by `test_the_expected_month_uses_the_same_grid_the_returns_table_was_written_on`, which asserts the cache's idea of the monthly grid is `compute_rebalance_dates`' own - two different definitions would make a cache that can never look fresh.

- Observation: a capability can be fully implemented, tested, live-verified AND specifically optimized for, and still be invisible - at which point the person who cannot see it correctly concludes it is missing. `whatif` accepted an unheld ticker from the day Milestone 5 landed; `test_whatif_reports_the_baseline_then_the_hypothetical` pinned it; the live run that produced this plan's headline finding (adding NVDA lowers the Sharpe ratio) WAS a new-ticker experiment; and Milestone 6's `refresh_cache=True` exists for no other purpose than making a re-tried unheld candidate free. The repository owner nonetheless reported the scope as limited to existing holdings, and was right about every word the program prints.
  Evidence: the tell was that the ONLY in-program sentence describing the capability lived inside a refusal - `"Drop that flag to try a holding you do not already own"`, reachable only by somebody who had already tried it and passed `--no-holdings-fetch`. The loop said `[s]et shares`, the `args` help said `Ignored by ... 'whatif'`, and both `_run_whatif`'s docstring and this plan's own Milestone 5 Scope sentence said "changes to the saved holdings", which reads as editing what is there. The affirmative statement existed only in `README.md` and this plan - neither of which a person sees at a prompt.
  Lesson: this project audits the provenance of NUMBERS rigorously - `plans/14`'s rate-origin line, Milestone 6's `(priced ...)` note, the refusal to print a figure without saying where it came from. It had no equivalent habit for AFFORDANCES. A feature's discoverability deserves the same suspicion as a figure's provenance, and "it is in the README" is the affordance equivalent of a bare `0.0200`.

- Observation: the feature was two signatures away the whole time. `lookback_months` already existed as a parameter on `load_returns_matrix_unfiltered`, `load_returns_matrix` AND `holdings_stats`, threaded correctly all the way down - and `grep -rn lookback_months src tests` showed nothing in `src/flow` or `tests` had ever passed it. Every occurrence was a default or an internal pass-through, so the window had been configurable at the estimator for as long as the estimator existed, with no way to reach it.
  Evidence: the break was `interactive._holdings_stats_excluding`, which simply stopped at `risk_free_rate` when calling `holdings_stats`. Two parameters and two call sites later, `[w]indow` worked.

- Observation: the PFF case the repository owner described is real, and the effect is larger than "different" - it changes the sign. Over 60 months PFF returns -1.05% at 11.08% volatility for a Sharpe of -0.2752; over 36 months, which excludes the 2022 rate shock, it returns +3.67% at 8.02% for a Sharpe of +0.2079. Both are true statements about different spans of months, which is exactly why the window has to be printed on the same line as the figures rather than assumed.
  Evidence: a live run on 2026-09-06 against a 1,000-share PFF portfolio printed `Returns window: 2021-10-01 to 2026-09-01 (60 month(s) of monthly returns)` then `Returns window: 2023-10-02 to 2026-09-01 (36 month(s) of monthly returns, 36 requested)`.

- Observation: adding the window verb exposed a false label that had been there since Milestone 5. A window-only change leaves the holdings untouched, so the loop printed the person's REAL portfolio under the heading `What if (USD) - not saved` with a row of zero deltas beneath it. The same thing had always happened after `[u]ndo all`, unnoticed, because nobody had a reason to do it. Fixed by reprinting the baseline AS the baseline whenever the hypothetical holdings equal the saved ones.
  Evidence: `test_choosing_a_window_measures_over_it` first failed with `[60, 36, 36] == [60, 36]` - that third measurement being the pointless re-measure of an unchanged portfolio which the false heading was about to sit on top of.

## Decision Log


- Decision: the new entry point is `portfolio-holdings`, registered as `src.flow.holdings_cli:main`, rather than adding subcommands to the existing `portfolio` command.
  Rationale: the user asked for a new entry point, and every other console script in this repository is already named `portfolio-<noun>`. "Holdings" is the standard word for shares actually owned and keeps the new concept audibly distinct from the *candidate pool*, which is the thing `portfolio` itself edits. Two commands that both said "portfolio" would invite exactly the confusion this feature has to avoid.
  Date/Author: 2026-09-06, agreed with the user.

- Decision: holdings live in their own file, `memory/portfolio.json`, not as an extra `positions` key inside `memory/candidates.json`'s per-currency pools.
  Rationale: a candidate pool and a portfolio are different things with different lifetimes. A pool is a list of things being considered and is rewritten wholesale by a confirm loop; a portfolio is a record of fact that changes only when the user trades. Folding them together would mean an ordinary candidate edit rewrites the record of what the user owns, and would force every reader of one concept to parse the other. Agreed with the user.
  Date/Author: 2026-09-06, agreed with the user.

- Decision: the figures print from BOTH commands — after every `portfolio-holdings` operation, and once per `uv run portfolio ...` run.
  Rationale: the user asked for exactly this. The maintenance command shows the effect of the edit just made; the pipeline command puts the held portfolio beside the optimizer's suggestion and the benchmark, which is where the comparison is worth something.
  Date/Author: 2026-09-06, agreed with the user.

- Decision: a holding with too little usable price history is EXCLUDED from the figures and named, with its share of total value, rather than blocking the report.
  Rationale: refusing to print anything because one recently-listed holding lacks two years of history would withhold a correct answer about 98% of a portfolio over 2% of it. Naming the exclusion and its value share tells the reader precisely how much of the portfolio the figures do not cover, which is strictly more information than a refusal. This mirrors how `plans/12`'s benchmark reports a one-line `n/a` with a reason rather than interrupting a run. Agreed with the user.
  Date/Author: 2026-09-06, agreed with the user.

- Decision (SUPERSEDED 2026-09-06 by Milestone 6, which built the rejected alternative): holdings prices and returns are resolved the way `plans/12`'s benchmark already resolves its history — read the run's own database first, and only fetch from Yahoo Finance into a **throwaway scratch database** when that database holds too little. `--no-holdings-fetch` restricts it to what is already cached.
  Rationale: two alternatives were considered and rejected. Fetching unconditionally on every invocation would add several seconds of network to every single `uv run portfolio` run for no gain when the data is already cached. Keeping a dedicated persistent cache (`data/holdings.duckdb`) would be faster on repeat runs but requires inventing a staleness policy — "how old may the newest monthly return be before we refetch?" — which is a whole design question of its own and not one this feature needs to answer. Mirroring the benchmark reuses a policy already written, tested and documented, and it keeps the number of caches in this project at one. Agreed with the user.
  Superseding note: Milestone 6 built `data/holdings.duckdb` after all, at the user's request, and answered the staleness question this entry deferred - a ticker is stale when the cache lacks the monthly return for the report month's rebalance date. What this entry got right is that the question was real and had to be answered before the cache could exist; what it did not anticipate is that the answer would be derived from the DATA rather than from a clock, which is why it needs no timestamp table and cannot drift. The two rules that motivated the throwaway database are untouched: the cache is neither `data/portfolio.duckdb` nor any session database, so holdings rows still cannot move the window a candidate pool is measured over.
  Date/Author: 2026-09-06, agreed with the user; superseded 2026-09-06 by Milestone 6.

- Decision: the holdings block prints ONCE per pipeline run, immediately after `print_pipeline_result` and before the interactive candidate-edit loop; it is not reprinted after each edit.
  Rationale: `print_weights_and_allocation` reprints the pool's currency, window and benchmark after every edit because those are what an edit changes. An edit to the candidate pool changes nothing about what the user owns. Reprinting three unchanged numbers after every keystroke would be noise, and it would also imply a relationship between the edit and the holdings that does not exist.
  Date/Author: 2026-09-06.

- Decision: the holdings' returns window is its own, derived from the holdings' own data, and is printed on its own line rather than being forced to match the candidate pool's window.
  Rationale: the two are genuinely different windows — a pool of large-cap US stocks may have sixty months of history while a portfolio containing a fund launched last year has fewer. Forcing them to agree would mean either truncating the pool's estimation window to the holdings' (degrading the optimizer's inputs for a reporting convenience) or claiming coverage the holdings do not have. Printing both, each labelled, is the honest option, and it is the same discipline `format_benchmark` already applies when it prints "N of M month(s)".
  Date/Author: 2026-09-06.

- Decision: weights come from `load_latest_prices` (`adj_close` on or before the run's date), the same function `allocate_shares` divides a budget by.
  Rationale: using a different price source for "what my holdings are worth" than for "how many shares to buy" would let the two disagree within one report. The adjusted close on the most recent date equals the unadjusted close there — back-adjustment for splits and dividends only rewrites *earlier* prices — so the most recent `adj_close` is a real per-share market price and not a synthetic one.
  Date/Author: 2026-09-06.

- Decision: `market_values` and `total_value` cover every holding that could be priced, INCLUDING ones excluded from the risk figures, while `weights` are renormalized over the survivors and sum to 1.
  Rationale: these answer two different questions and a single denominator cannot serve both. "What is my portfolio worth?" must include a holding whose history is too short — the user owns it either way. "What produced these three figures?" must not. Reporting each with its own honest denominator, and naming every exclusion together with its percentage of total value, keeps both answers correct and makes the gap between them visible.
  Date/Author: 2026-09-06.


- Decision: a ticker's own trading currency selects which portfolio a `set` edits, rather than a non-matching ticker being refused against whichever portfolio already exists.
  Rationale: this reverses what an early draft of this plan's acceptance section said, and the draft was wrong. If `set 7203.T 100` were refused because a USD portfolio exists, there would be no way to start a yen portfolio without discovering `--currency` first, which defeats "maintain his portfolio per currency". Refusal is now reserved for the two cases where it is genuinely needed: naming two currencies in ONE `set` (that would be editing two portfolios at once), and naming a ticker that contradicts an explicit `--currency`. Reuses `partition_by_currency` and `_in_typed_order` unchanged.
  Date/Author: 2026-09-06, corrected during Milestone 3.

- Decision: `print_user_portfolio(holdings, path)` takes the currency from `holdings.currency` rather than as a separate argument.
  Rationale: an early draft passed `currency` alongside, which admits a caller passing one that disagrees with the figures - and a money figure labelled with the wrong currency is precisely the failure `CURRENCY_SYMBOLS`' always-print-the-ISO-code rule exists to prevent. Making it unpassable is better than documenting that it must match.
  Date/Author: 2026-09-06, corrected during Milestone 3.

- Decision: `src/flow/holdings_cli.py` uses one flat `argparse` parser with a positional `command` and a positional `args` list, not `add_subparsers`.
  Rationale: subparsers would force every shared flag to be either repeated per subcommand or accepted only BEFORE the subcommand, so `portfolio-holdings set SPY 1000 --path X` would fail while `portfolio-holdings --path X set SPY 1000` worked - a distinction no user should have to learn. A flat parser accepts flags anywhere. It also matches this repository's existing CLI style: `src/flow/cli.py` and `src/flow/candidate_memory.py` are both flat parsers and nothing here uses subparsers.
  Date/Author: 2026-09-06, during Milestone 3.

- Decision: a cross-currency holding found in a hand-edited `memory/portfolio.json` is EXCLUDED and named, not raised as `MixedCurrencyPoolError` the way a mixed candidate pool is.
  Rationale: the optimizer raises because it is about to allocate real money against a budget and a wrong answer there is spent money. This is a report; the useful response to one misplaced holding is to name it and measure the rest, which is the same exclude-and-name discipline the thin-history rule follows. The holding IS withheld from `total_value` as well as from the weights, since summing yen into a dollar total would produce a number that is not an amount of anything.
  Date/Author: 2026-09-06, added during Milestone 2 as `interactive._holdings_currency_gate`.

- Decision: the exclusions print as an indented `Excluded from the figures:` section rather than one long line.
  Rationale: the plan first specified a single line. With three exclusions that line wraps unreadably in a terminal, and the surrounding report already establishes the indented-section idiom (`Weights:`, `Expected return / volatility (annualized):`, `Share allocation:`).
  Date/Author: 2026-09-06, during Milestone 3.


- Decision: `whatif` is INTERACTIVE, the only subcommand of `portfolio-holdings` that is, and the module docstring's "entirely non-interactive" claim is narrowed rather than abandoned.
  Rationale: chosen by the user from three offered shapes. The docstring's justification for the rule was scriptability, and that survives intact: every subcommand that changes the record is still a single non-interactive line. What a flag-per-run shape could not give is the actual workflow - trying five variations in a row and watching the numbers - because each variation would re-pay the Yahoo Finance round trip that one open session pays once. The two facts belong together, and the docstring now says so: exploring is inherently a conversation, and it is safe to make this one because nothing it does can outlive the session.
  Date/Author: 2026-09-06, agreed with the user.

- Decision: the baseline and every variant are measured against ONE throwaway database, kept open for the whole loop, rather than each resolving its own.
  Rationale: correctness first, speed second. `src/optimizer/portfolio.py`'s `_load_window_dates` derives the returns window from `SELECT DISTINCT rebalance_date FROM returns` over the WHOLE table, so two variants resolved separately can come back measured over two different windows - and the difference between two Sharpe ratios computed over different months is partly just the difference between the months. A what-if's entire output is that subtraction, so one database makes the shared window structural instead of a coincidence to be checked. `format_holdings_delta` still checks it and withholds the delta if it ever fails to hold, because a plausible wrong number is worse than a stated refusal. The speed is the bonus: the first measurement fetches, every later one is instant. The shape is `open_pipeline_session`'s, which already does exactly this for `_run_edit_loop`.
  Date/Author: 2026-09-06.

- Decision: a what-if never saves, and closes by printing the `set` command that would apply it.
  Rationale: the user chose never-save over an offer-to-keep prompt, which keeps the guarantee absolute - the figures a what-if reports can always be trusted not to have moved the record. The cost would have been retyping a good experiment by hand, so `_whatif_set_command` computes the exact one-line `set` that turns the saved holdings into the hypothetical ones (a retirement spelled `0`, as `set` spells it). That removes the retyping without weakening the promise, and it is why `_whatif_apply` deliberately mirrors `_run_set`'s merge: a what-if is only worth trusting if it predicts what `set` would actually do.
  Date/Author: 2026-09-06, agreed with the user.

- Decision: `whatif` suppresses BOTH writes on the `set` path - `save_portfolio` and `_remember_rate` - and relabels the rate's provenance.
  Rationale: `_remember_rate` is the one easy to miss. `--risk-free-rate` is *remembered* per currency by every other subcommand, so accepting the flag without care would leave a permanent rate change behind from a run whose whole promise was changing nothing. The flag is still accepted, because "what if the riskless return were 5%?" is a real question, but nothing is written - and the report says `--risk-free-rate, not remembered - this is a what-if` rather than `resolve_risk_free_rate`'s usual `remembered for USD`, since printing the latter would have the report claim a write this command exists specifically not to make. `_guard_rate_is_rememberable` exempts `whatif` for the same reason: with nothing to remember, the ambiguity that guard refuses cannot arise.
  Date/Author: 2026-09-06.

- Decision: a ticker typed at the `[s]et` prompt is validated through `_validate_set_targets`, the same function `set` uses, rather than being handed straight to the measurement.
  Rationale: the by-name cross-currency refusal lives only at the CLI layer. `interactive._holdings_currency_gate` merely EXCLUDES a foreign holding with a one-line reason - deliberately, because it guards a hand-edited file and naming the holding while measuring the rest is the right behaviour there. But a ticker just typed is different: a mistyped `7203.T` in a dollar experiment deserves `set`'s "so it belongs to the JPY portfolio" sentence, not a quiet disappearance from the figures. This required giving `_validate_set_targets` a `pool_currency` parameter - see `Surprises & Discoveries` for the bug that omitting it caused.
  Date/Author: 2026-09-06, during Milestone 5c.

- Decision: under `--no-holdings-fetch`, adding a ticker the experiment has not already resolved is refused rather than ingested.
  Rationale: with fetching disabled the session yields `--db-path` itself rather than a scratch file, and that path is documented as never written to. Ingesting a new ticker there would write rows into the shared `data/portfolio.duckdb` as a side effect of an experiment - the exact isolation rule `prepare_holdings` calls a correctness requirement. Refusing by name, and saying which flag to drop, is the only honest option.
  Date/Author: 2026-09-06.

- Decision: a held ticker's cached data is stale when the cache lacks the monthly return for the most recent rebalance date on or before the report's date - in plainer words, the cache is good for the rest of the calendar month.
  Rationale: chosen by the user from three offered policies, and it is the question the original deferral hinged on. Two things make it work better than the clock-based alternatives it beat. It is derived from the DATA: "does the cache hold the latest month's return?" is answered by looking for the row, so there is no `fetched_at` timestamp that can disagree with the rows it claims to describe, and no metadata table to keep in step. And it reuses `compute_rebalance_dates` - the same function that decided where those rows go - so the cache's idea of the monthly grid cannot drift from the writer's, which would produce a cache that never looks fresh and refetches forever.
  Date/Author: 2026-09-06, agreed with the user.

- Decision: the report states the date of the prices behind `Total value`, and `--refresh-holdings` forces a refetch.
  Rationale: this is the mitigation for the one real cost of the monthly rule, which I flagged when offering it and the user chose anyway. Monthly returns change monthly, but prices change daily, and the latest price is what turns share counts into `Total value` and into weights - so a cache refreshed on the 3rd and read on the 25th reports a total three weeks out of date. The 60-month annualized figures barely notice; a money figure does. Rather than override the user's choice or quietly hope, the cost is made visible: `Total value: $850,722.00 USD (priced 2026-09-04)`, with the date read off the price rows actually used rather than off a refresh timestamp, so it cannot claim a freshness the data lacks. `--refresh-holdings` is how a person who wants today's total says so. This is the same discipline `plans/14` established for the risk-free rate - never print a figure without saying where it came from.
  Date/Author: 2026-09-06.

- Decision: only the tickers a report actually asks about are ever refreshed.
  Rationale: the obvious alternative - refresh the whole cache when any of it is stale - turns the file into a liability rather than an asset. What-if tickers are cached too (the user's choice, and correct: a price cache holds facts, and re-trying a candidate should be free), so the file slowly accumulates tickers nobody owns. Refreshing wholesale would mean every monthly refresh refetching everything ever tried. Per-ticker refresh keeps the cost proportional to the report, and the leftover rows are harmless: every query filters by ticker, and the one that does not (`_load_window_dates`) reads only distinct rebalance dates, which are the same monthly grid for every ticker.
  Date/Author: 2026-09-06.

- Decision: `--no-holdings-fetch` now reads the holdings cache, where before it read only the session database.
  Rationale: a deliberate widening of a documented flag, on the grounds that its help text described the letter and its name describes the intent. "Never by fetching" is about the network; a local DuckDB file is not the network. Before this, an offline run with a fully populated cache reported the holdings as unmeasurable, which is not what anybody asking for offline behaviour wants. The flag's help now says "from data already on disk - this session's database or the holdings cache - never by fetching", and the unavailable message names both places it looked.
  Date/Author: 2026-09-06.

- Decision: the reported "whatif cannot add new tickers" gap was fixed by REWORDING, not by adding a distinct `[a]dd` verb.
  Rationale: the capability was already there, so a new verb would have added a second way to spell an existing action rather than a new action. `[s]et` has to keep accepting an unheld ticker regardless - what makes the loop trustworthy is that it predicts exactly what `set` would do, and `set` accepts one - so an `[a]dd` verb would be a synonym needing its own explanation of how it differs, which it does not. The actual defect was that six user-facing strings described the target as "the saved holdings" and the action as "changes", both of which read as editing existing rows. Six strings changed; no logic touched.
  Date/Author: 2026-09-06, agreed with the repository owner after establishing the capability already existed.

- Decision: connecting `whatif` to `memory/candidates.json` was considered and deliberately NOT taken here.
  Rationale: it was the one reading of "expand the scope to add new tickers" that is genuinely unbuilt - a `[c]andidates` option listing the curated candidate pool for the run's currency, so a person could try one without typing a ticker, connecting the two halves of this project. The repository owner chose the wording fix instead when the three options were put side by side. Recorded because it is a real and reachable follow-up, and because whoever picks it up should know it was seen rather than missed: `load_all_pools` and `load_candidate_pool` in `src/flow/candidate_memory.py` already provide everything the listing would need.
  Date/Author: 2026-09-06.

- Decision: the selectable returns window lives INSIDE the `whatif` loop only, and nowhere else.
  Rationale: chosen by the repository owner, and it is the option that avoids a documented, tested guarantee rather than merely being the smallest. `uv run portfolio` prints the pool's figures, the benchmark's and the holdings' and promises all three describe the same months; `benchmark_stats_for_window` is deliberately anchored to the POOL's window, `README.md` asserts that a one-holding portfolio reports exactly its own benchmark's numbers, and `tests/test_interactive_flow.py` pins `benchmark.window_months == stats.returns_window_months`. A holdings-only window there would silently falsify all three. The whatif loop has no such coupling - it measures through `measure_holdings`, not `prepare_holdings` - which is why the change cost two parameters instead of a re-anchoring of the whole pipeline.
  Date/Author: 2026-09-06, agreed with the repository owner.

- Decision: 24 to 60 months, refusing anything outside by name.
  Rationale: chosen by the repository owner from three offered ranges, and both bounds are existing facts rather than new numbers. The floor is `HOLDINGS_MIN_MONTHS`, the minimum history a holding needs before it is measured at all: below it `apply_min_history_rule` drops EVERY holding, since a column cannot hold more non-null months than the window has rows, and the report would come back with no figures and a complaint about a `min_months` nobody typed. The ceiling is what the data can support - an ingest fetches 65 months of prices, which is 60 monthly returns plus a buffer month - so a larger request cannot be honoured and is refused rather than silently truncated. Refusing also keeps a zero or negative value away from `_load_window_dates`, whose `LIMIT ?` raises a raw DuckDB `BinderException` that the report layer's blanket handler would surface as an unhelpful "could not measure this variant".
  Date/Author: 2026-09-06, agreed with the repository owner.

- Decision: `min_months` stays at `HOLDINGS_MIN_MONTHS` regardless of the window, with the consequence documented rather than engineered around.
  Rationale: at a 24-month window the bar equals the window, so a holding needs EVERY month in it - a single missing one leaves it at 23 and it is excluded, and not even a gap is required: one leading null from a recent listing, which `apply_min_history_rule` is designed to tolerate, trips the count branch instead. That narrows `plans/05_optimizer_and_allocation.md`'s "use whatever months a ticker actually has, between 24 and 60" tolerance to nothing at the floor. Scaling the bar down with the window was considered and rejected: 24 is shared by the optimizer, the benchmark and the holdings report, and `plans/13`'s own Surprises section records that `apply_min_history_rule` is unsafe at `min_months=0`, so moving it is not a local change. The existing per-holding exclusion message already explains itself - "23 month(s) of monthly returns in the window, under 24" - so this needs a test and this entry, not new machinery.
  Date/Author: 2026-09-06.

- Decision: a window change re-measures the BASELINE as well as the variant.
  Rationale: `format_holdings_delta` withholds a delta when the two sides' windows differ, deliberately, because the difference would then be partly the months rather than the holdings. Leaving the baseline where it was would therefore turn every subsequent comparison into an apology. Re-measuring both keeps the delta meaningful: one window on both sides, different holdings. It also made that guard's docstring wrong - it said reaching the branch "means something has gone wrong upstream", which stops being true once a person can legitimately change the window - so the sentence was corrected while the guard itself stayed exactly as it was.
  Date/Author: 2026-09-06.

- Decision: the report names the window that was REQUESTED, beside the count it actually used.
  Rationale: the `Returns window:` line has always been derived from the data, which is right and unchanged. But derived alone cannot distinguish "36 months because I chose 36" from "36 months because that is all there was", and once the length is a choice that distinction is the whole point. Naming the request also makes an unhonoured one visible: 48 months of data against a 60-month request prints both numbers instead of quietly reporting 48. And it is the honest answer to this feature's obvious hazard - shortening a window until the figures improve is cherry-picking, and the defence is that the window is stated on the same line as the figures it produced, every time. Same shape as `risk_free_rate_origin`, for the same reason.
  Date/Author: 2026-09-06.

## Outcomes & Retrospective


**Delivered, and verifiable.** `uv run portfolio-holdings` maintains the portfolio a person actually holds, per currency, in `memory/portfolio.json`; it and every `uv run portfolio ...` run print that portfolio's annual return, annual volatility and Sharpe ratio. Both halves of the original request are done, and the numbers are the right numbers rather than merely numbers: a single-holding portfolio reproduces its own benchmark's three figures exactly, in a fixture test and in a live run.

The measured shape of the change: three new modules (`src/flow/user_portfolio.py`, `src/optimizer/holdings.py`, `src/flow/holdings_cli.py`), one new console script, three touched (`src/optimizer/portfolio.py`, `src/flow/interactive.py`, `src/flow/cli.py`), three new test files plus five tests added to `tests/test_cli.py`, and 424 tests passing where 360 passed before. `tests/test_optimizer.py` was never edited, which is the evidence the estimator extraction moved nothing.

**What went right, and why.** Almost none of the arithmetic is new. The three figures come from `_estimate_mu_and_cov` and PyPortfolioOpt's `portfolio_performance` - the same estimators the optimizer and the benchmark already used - and the thin-history exclusion is `apply_min_history_rule`, which already did exactly the right thing for exactly the right reasons. The genuinely new code is the persistence, the shares-to-weights conversion, and the reporting. That is why the comparability test passed the first time it ran: there was no second implementation to disagree with the first.

Mirroring `prepare_benchmark` for data resolution paid off twice. It supplied a fetch policy already reasoned about, and it supplied the isolation rule - fetch into a throwaway database, never the session's - which was verified by hand: `data/portfolio.duckdb`'s size and mtime were byte-identical before and after a full `portfolio-holdings` session.

**What remains.** One open item, deliberately: holdings are refetched whenever the session database lacks them, which for a live run is always. Roughly ten seconds for two holdings, scaling with the count. The fix is a persistent holdings cache with a staleness rule, and the reason it was not done is that "how stale may a monthly return be?" is its own design question rather than a detail of this one. **Closed 2026-09-06 by Milestone 6**, which answered that question (stale when the cache lacks the report month's return) and measured the result: 9 seconds cold, 4 warm, with the residual being interpreter startup rather than network.

Two smaller gaps worth naming. Nothing yet compares the held portfolio against the optimizer's suggested weights *quantitatively* - the report puts the two blocks side by side and leaves the comparison to the reader, which is the right first step but stops short of "here is what rebalancing would buy you". **Milestone 5 closes that from the other direction**: rather than compute an optimum, `uv run portfolio-holdings whatif` lets a person try a change and see the three figures move, with the signed delta computed for them. The optimizer-comparison half remains open. And `--date` is accepted by `portfolio-holdings` but a past date measures today's share counts against that date's prices, which is only meaningful if the holdings have not changed since; that is documented in the flag's help but not enforced, because enforcing it would require a position history this feature does not keep.

**Lesson.** The plan's acceptance section asserted that `set 7203.T 100` against an existing USD portfolio would be refused, and writing the code made it obvious that this was backwards: it would make a second currency unreachable without a flag the user has not met yet. The plan was specific enough to be *falsifiable*, which is what let the error surface during implementation instead of after it - an argument for stating acceptance as concrete transcripts rather than as properties.


## Context and Orientation


This repository is a Python 3.12 project managed with `uv`. Application code lives under `src/`, imported as `src.<package>.<module>` (the import root is `src` itself — see `pyproject.toml`'s `[tool.hatch.build.targets.wheel] packages = ["src"]`). Tests live flat under `tests/`, are run with `uv run pytest tests/test_*.py`, and must never make a network call. Console commands are declared in `pyproject.toml`'s `[project.scripts]` and run as `uv run <name>`.

The pieces this plan touches, by full path:

`src/config/settings.py` holds a `pydantic-settings` singleton named `settings`, the single source of every environment-configurable value. The one this plan needs is `settings.risk_free_rate`, default `0.02`.

`src/dataset/prices.py` fetches daily price history from Yahoo Finance via the `yfinance` library and stores it in a DuckDB database file (default `data/portfolio.duckdb`) in a table `prices (date, ticker, close, adj_close)`. There is no HTTP cache; the DuckDB file *is* the cache.

`src/dataset/returns.py` derives, from those prices, a table `returns (rebalance_date DATE, ticker VARCHAR, monthly_return DOUBLE)`: one trailing-one-month realized return per ticker per month. Every downstream risk figure in this project is computed from this table, never from raw prices.

`src/dataset/ticker_currency.py` records which currency each ticker trades in, in a table `ticker_currency (ticker, currency, quoted_currency, price_multiplier)`, and enforces this project's rule that **one portfolio holds exactly one currency**. It exposes `DEFAULT_CURRENCY = "USD"` (assumed for any ticker with no recorded currency), `MixedCurrencyPoolError` (a subclass of `ValueError`), `group_by_currency(tickers, currencies)`, `load_ticker_currencies(tickers, db_path)` (read-only, returns `{}` when the table or file is absent — the normal case), and `partition_by_currency(candidates, pool_currency, currencies)`, which splits a batch of tickers into those that may join a pool and those refused by name for trading in another currency. The rule exists because prices in two units cannot be allocated against one budget; conversion between currencies is deliberately not implemented.

`src/dataset/ticker_ingestion.py` has one function, `validate_and_ingest_tickers(tickers, as_of, db_path) -> (valid, invalid, currencies)`. It is the only path by which an arbitrary user-typed ticker enters this system: it fetches 65 months of prices ending at `as_of`, looks up each ticker's currency, normalizes minor units (London pence into pounds), and writes prices, `ticker_currency` and `returns` rows into `db_path`. A ticker that does not resolve, or whose currency cannot be determined, comes back in `invalid` mapped to a human-readable reason rather than raising.

`src/optimizer/portfolio.py` is the estimation and optimization layer. The functions this plan uses:

- `load_returns_matrix(tickers, as_of, lookback_months=60, min_months=24, db_path=settings.db_path) -> pd.DataFrame` returns a wide matrix, rows being months and columns being tickers, of monthly returns. Internally it asks the `returns` table for the most recent `lookback_months` distinct `rebalance_date`s at or before `as_of` (`_load_window_dates`), pivots the rows into that shape, and then applies `apply_min_history_rule`, which **drops any column** with fewer than `min_months` non-null values or with an internal gap (leading nulls from a recent listing and trailing nulls from a delisting are not gaps). It drops columns, never rows, so the window itself is unaffected by the rule.
- `load_latest_prices(tickers, as_of, db_path) -> pd.Series` gives the most recent `adj_close` at or before `as_of` per ticker, or NaN when there is none.
- `_fit_efficient_frontier(returns_matrix, objective, target_annual_return, risk_free_rate)` estimates the two inputs every risk figure in this project comes from, and this plan must reuse them exactly rather than re-deriving them:

        mu = expected_returns.mean_historical_return(returns_matrix, returns_data=True, frequency=12)
        cov_matrix = risk_models.CovarianceShrinkage(
            _covariance_input(returns_matrix), returns_data=True, frequency=12
        ).ledoit_wolf()

  `frequency=12` is PyPortfolioOpt's annualization multiplier for monthly data, so both come out annual. `mean_historical_return` compounds by default, so `mu` is a geometric annual growth rate. `CovarianceShrinkage(...).ledoit_wolf()` is a *shrunk* covariance estimate: it pulls a noisy sample covariance toward a structured target by a data-chosen amount, which stabilizes the estimate for a small cross-section. `_covariance_input` first restricts the matrix to months where every column is present, because `ledoit_wolf()` internally replaces NaN with zero and would otherwise read a missing month as a 0% return.
- `compute_weights_and_stats(...) -> PortfolioStats` solves an objective and reports the result, taking the portfolio's three figures from PyPortfolioOpt's `ef.portfolio_performance(risk_free_rate=...)`, which is `(w'mu, sqrt(w'Σw), (w'mu - rf)/sqrt(w'Σw))`.

`src/optimizer/benchmark.py` measures a single reference ticker with the *same* estimators, in `annualized_return_and_volatility(monthly_returns)`, and defines `BENCHMARK_MIN_MONTHS = 24` — below which it declines to print figures at all — and the `BenchmarkStats` record, whose discipline this plan copies: every field is populated, or every figure is `None` and `unavailable_reason` says why. Never a half state. Its module docstring documents one deliberate asymmetry at length: shrinkage is defined across a cross-section, so a single-column benchmark receives none (`delta` is provably 0), while a multi-ticker pool's covariance genuinely shrinks. That asymmetry must not be "fixed" by folding a benchmark into a pool's matrix, because then the benchmark's printed volatility would move whenever a candidate was added, and a reference point that moves is not a reference point. A holdings portfolio is a genuine multi-ticker cross-section, so it shrinks exactly as a candidate pool does and the existing documented asymmetry covers it unchanged.

`src/flow/candidate_memory.py` is the persistence layer for `memory/candidates.json` and is the direct model for this plan's new store. Its file shape is `{"pools": {"USD": {"tickers": [...], "benchmark": "SPY", "updated_at": "..."}}}`, one entry per currency. Its semantics, which this plan copies deliberately: a missing file reads as empty rather than raising, because that is the expected state on a first run; malformed JSON lets `json.JSONDecodeError` propagate; a wrongly-typed field raises `ValueError` naming both the file and the currency it was found under, so the failure is reported where it can be fixed rather than much later inside a Yahoo Finance lookup; and a save re-reads every currency and replaces only its own slot, carrying every other currency's contents *and its own `updated_at`* over untouched, so saving one currency can never disturb another or make it look freshly edited.

`src/flow/live.py` builds throwaway DuckDB databases. `build_scratch_snapshot()` is a context manager yielding the path of a temporary database containing only empty `prices`, `unresolved_tickers`, `returns` and `ticker_currency` tables, deleted when the block exits.

`src/flow/interactive.py` orchestrates a run. `open_pipeline_session(rebalance_date, selection, db_path)` yields `(effective_db_path, mode)`: the shared `data/portfolio.duckdb` for a date inside the stored 2020-01-01..2024-04-30 backtest window, or a freshly built throwaway snapshot otherwise — and always a snapshot for `selection="user_provided"`, whose user-typed tickers must never be written into the shared cache. `prepare_benchmark(...)` is the function this plan's `prepare_holdings` mirrors, and its docstring states the rule both must obey, which is worth repeating in full because it is the single most important constraint in this plan: benchmark history is fetched into a database of its **own**, never the session's, because `portfolio._load_window_dates` derives a portfolio's returns window from `SELECT DISTINCT rebalance_date FROM returns` over the **whole table** rather than over the candidate tickers — so writing extra months into the session's database could move the very window the portfolio is being measured over. Keeping the databases apart makes "this never influences that" structural rather than a convention someone has to remember. `prepare_benchmark` also never raises: any failure becomes an `unavailable_reason` plus a logged warning, because losing a live session's fetched snapshot over one report line would cost far more than the line is worth.

`src/flow/cli.py` is the `portfolio` command: an `argparse` parser, plain `print` output (no `rich`, no `tabulate`), and raw `input()` for its interactive loops. `format_money(amount, currency)` renders money with both a symbol and its ISO code (`'$12.34 USD'`), because a bare `$` is shared by five currencies and disambiguating that is the whole point of tracking currency. `print_weights_and_allocation(...)` renders one optimization, and its docstring records why the currency and returns-window lines live there rather than in the run header: the interactive edit loop calls that function directly, so anything printed only in the header would vanish after the first edit.

`memory/` is listed in `.gitignore`, so nothing this plan writes there is ever committed — which is what `AGENTS.md`'s security guidance requires of local watchlist data.


## Plan of Work


The work divides into four independently verifiable milestones: a persistence layer with no dependencies, then the arithmetic, then a command that exposes both, then the same block inside the existing command plus documentation. Each is described in its own section below under `Milestones`.

The shape of the whole change, so a reader can navigate it before reading the details:

    memory/portfolio.json                <- new: what the user owns, one entry per currency
      |
      v
    src/flow/user_portfolio.py           <- new: read/write that file. No network. No src/optimizer import.
      |
      v
    src/flow/holdings_cli.py             <- new: `uv run portfolio-holdings set|remove|show`
      |    src/flow/cli.py               <- changed: print_user_portfolio(), three new flags, one new block
      |      |
      v      v
    src/flow/interactive.py              <- changed: prepare_holdings(), mirroring prepare_benchmark()
      |
      v
    src/optimizer/holdings.py            <- new: positions x prices -> weights -> the three figures
      |
      v
    src/optimizer/portfolio.py           <- changed: _estimate_mu_and_cov() extracted; stats_for_weights() added

Milestone 5 adds the what-if loop on top, touching three of the same files and creating none:

    memory/portfolio.json                <- READ ONLY: a what-if never writes it, nor memory/rates.json
      |
      v
    src/flow/holdings_cli.py             <- changed: `whatif` + _run_whatif/_whatif_set, calling
      |                                     neither save_portfolio nor _remember_rate
      v
    src/flow/interactive.py              <- changed: open_holdings_session() + measure_holdings() -
      |                                     ONE throwaway database per session, so every variant
      |                                     is measured over one returns window
      v
    src/flow/cli.py                      <- changed: print_user_portfolio(heading=...) and
                                            format_holdings_delta()

Milestone 6 replaces the throwaway database under all of it with a persistent one:

    data/holdings.duckdb                 <- new: the holdings' prices and monthly returns,
      ^                                     refreshed per ticker when its latest month is missing.
      |                                     NEVER data/portfolio.duckdb and never a session db.
      |
    src/dataset/holdings_cache.py        <- new: the staleness rule and the per-ticker refresh
      ^
      |
    src/flow/interactive.py              <- changed: _resolve_holdings and open_holdings_session
      |                                     resolve through the cache, not build_scratch_snapshot
      v
    src/optimizer/holdings.py            <- changed: HoldingsStats.priced_as_of, so a total that
      |                                     came from a month-old cache says so
      v
    src/flow/cli.py                      <- changed: latest_price_date(), the "(priced ...)" note,
    src/flow/holdings_cli.py                and --holdings-cache-path / --refresh-holdings on both

Milestone 7 makes the window itself a choice, in the what-if loop alone:

    src/optimizer/holdings.py            <- changed: DEFAULT_LOOKBACK_MONTHS and
      |                                     validate_lookback_months (24-60, refused outside)
      v
    src/flow/interactive.py              <- changed: lookback_months on measure_holdings and
      |                                     _holdings_stats_excluding. NOT on prepare_holdings,
      |                                     which is what keeps show/set/remove and every
      |                                     `uv run portfolio` block on the fixed 60
      v
    src/flow/holdings_cli.py             <- changed: [w]indow in WHATIF_PROMPT + _whatif_window,
      |                                     the window as loop state, baseline re-measured on change
      v
    src/flow/cli.py                      <- changed: print_user_portfolio(window_origin=...), so
                                            "36 requested" sits beside the derived month count


## Milestones


### Milestone 1 — the memory store


Scope: one new module that reads and writes `memory/portfolio.json`, and its tests. Nothing else in the repository changes, and nothing here touches the network or a database. At the end of this milestone the file format exists and is proven safe against the failure modes a hand-editable JSON file invites; no user-visible behavior exists yet.

Create `src/flow/user_portfolio.py`, structured as a close mirror of `src/flow/candidate_memory.py`. It must not import from `src/optimizer` or from any other `src/flow` module, so that persistence stays independent of arithmetic. The public surface:

    DEFAULT_PORTFOLIO_PATH = "memory/portfolio.json"

    def load_all_portfolios(path: str = DEFAULT_PORTFOLIO_PATH) -> dict[str, dict[str, float]]
    def load_portfolio(path: str = DEFAULT_PORTFOLIO_PATH, currency: str = DEFAULT_CURRENCY) -> dict[str, float]
    def save_portfolio(positions: dict[str, float], path: str = DEFAULT_PORTFOLIO_PATH,
                       currency: str = DEFAULT_CURRENCY) -> None

The file holds one entry per currency, keyed under a top-level `"portfolios"` object:

    {
      "portfolios": {
        "USD": {
          "positions": {"SPY": 1000.0, "T": 500.0},
          "updated_at": "2026-09-06T04:20:11.123456+00:00"
        },
        "JPY": {
          "positions": {"1321.T": 50.0},
          "updated_at": "2026-09-06T04:21:02.987654+00:00"
        }
      }
    }

`"portfolios"` rather than `"pools"` because this is a different file describing a different thing, and a reader who opens either file should be able to tell which one they have. There is no earlier shape to migrate: this file did not exist before this plan, so unlike `candidate_memory.py` there is no legacy branch and no migration command.

Required semantics, each of which exists for a reason worth stating in the code:

- A missing file reads as `{}` (from `load_all_portfolios`) or `{}` (from `load_portfolio`), never an error. That is the expected state before the user has ever saved a portfolio, and treating it as an error would make the very first run fail.
- Malformed JSON lets `json.JSONDecodeError` propagate unchanged. The file is hand-editable; a syntax error in it is a real problem the user must see, not something to paper over with an empty result that would silently look like "no holdings".
- A private `_validate_positions(path, currency, positions)` returns a clean `dict[str, float]` and raises `ValueError` naming **both** the file and the currency when `positions` is not an object mapping ticker strings to numbers, or when a share count is not finite. Naming both is what `candidate_memory._validate_tickers` does, and for the same reason: a malformed value discovered here is fixable, whereas the same value surfacing later inside a price lookup is a confusing failure a long way from its cause. Booleans must be rejected explicitly, since `isinstance(True, int)` is true in Python and `{"SPY": true}` is not a share count.
- Tickers are normalized to upper case on the way in and out, matching how every other ticker in this project is handled.
- A non-positive share count is dropped rather than written, so `set SPY 0` is how a holding is retired and a stored file never contains a zero row claiming to be a position. Negative counts are refused by `_validate_positions` with their own message, because this project has no model of a short position and silently storing one would produce a negative weight that the risk arithmetic would accept and quietly misreport.
- `save_portfolio` re-reads every currency in the file, replaces only `currency`'s slot with the given positions and a fresh UTC timestamp, and writes them all back — carrying every other currency's positions and its own `updated_at` over untouched. Saving the USD portfolio must not make the JPY one look freshly edited.
- `save_portfolio` creates the parent directory if needed (`Path(path).parent.mkdir(parents=True, exist_ok=True)`), so a fresh clone with no `memory/` directory works.
- Saving an empty `positions` dict writes an entry with an empty `positions` object rather than deleting the currency, because "I sold everything in this currency" is a fact worth recording, and it keeps `save` total: every call leaves the file describing what the caller said.

Create `tests/test_user_portfolio.py` following this repository's conventions: a module docstring citing `AGENTS.md`'s no-network requirement, plain helper functions rather than fixtures, `tmp_path` for the file, and long full-sentence test names. Cover: a missing file reading as empty; a round trip; per-currency independence including the untouched `updated_at`; ticker upper-casing; a zero count retiring a holding; a negative count refused; a non-numeric count refused with the file and currency named; a boolean count refused; malformed JSON propagating; the parent directory being created; and an empty portfolio saving as an empty `positions` object rather than a removed currency.

Commands and acceptance:

    cd /app/agentic_portfolio
    uv run pytest tests/test_user_portfolio.py -q

Expect every test in the new file to pass. Then confirm the whole suite is untouched, since this milestone adds a module and changes none:

    uv run pytest tests/test_*.py -q


### Milestone 2 — the figures


Scope: turn a set of positions into the three numbers, using the estimators already in this repository and no new ones. Three edits: a behavior-preserving extraction inside `src/optimizer/portfolio.py`, a new `src/optimizer/holdings.py`, and a new `prepare_holdings` in `src/flow/interactive.py`. At the end of this milestone a Python caller can obtain a complete, printable record of a portfolio's risk figures, and the test that proves the numbers are the *right* numbers passes. Still no user-visible command.

**Step 2a — extract the estimator.** In `src/optimizer/portfolio.py`, move the two estimation statements out of `_fit_efficient_frontier` into

    def _estimate_mu_and_cov(returns_matrix: pd.DataFrame) -> tuple[pd.Series, pd.DataFrame]

and have `_fit_efficient_frontier` call it. This is a pure refactor with no numerical change: the point is that there is exactly one place in this repository where annualized expected returns and an annualized shrunk covariance are estimated, so a second consumer cannot drift from the first. `tests/test_optimizer.py` must pass **unmodified** after this step, which is the proof that nothing moved.

Then add, in the same module:

    def stats_for_weights(returns_matrix: pd.DataFrame, weights: dict[str, float],
                          risk_free_rate: float = settings.risk_free_rate) -> tuple[float, float, float]

returning `(annual_return, annual_volatility, sharpe)` for a **given** weight vector — as opposed to `compute_weights_and_stats`, which chooses the weights itself. Implement it with PyPortfolioOpt's own `pypfopt.base_optimizer.portfolio_performance(weights, mu, cov_matrix, risk_free_rate=..., verbose=False)`, so the definition of all three figures is identical to the one `EfficientFrontier.portfolio_performance` applies to an optimized portfolio. Build the weight argument keyed by **every** column of `returns_matrix`, filling `0.0` for columns with no position, so a mismatch between the two orderings can never silently misalign a weight with the wrong ticker's expected return.

**Step 2b — positions to figures.** Create `src/optimizer/holdings.py`:

    HOLDINGS_MIN_MONTHS = 24

    class HoldingsStats(NamedTuple):
        currency: str | None
        positions: dict[str, float]
        weights: dict[str, float]
        market_values: dict[str, float]
        total_value: float | None
        annual_return: float | None
        annual_volatility: float | None
        sharpe: float | None
        risk_free_rate: float
        window_start: date | None
        window_end: date | None
        window_months: int | None
        excluded: dict[str, str]
        unavailable_reason: str | None

    def weights_from_positions(positions: dict[str, float], latest_prices: pd.Series
                               ) -> tuple[dict[str, float], dict[str, float], float]

    def holdings_stats(positions: dict[str, float], as_of: date, db_path: str,
                       currency: str, risk_free_rate: float = settings.risk_free_rate,
                       lookback_months: int = 60,
                       min_months: int = HOLDINGS_MIN_MONTHS) -> HoldingsStats

`HOLDINGS_MIN_MONTHS = 24` is the same bar `load_returns_matrix`'s `min_months` default and `BENCHMARK_MIN_MONTHS` already use, stated here as its own named constant so the holdings report's threshold is findable from the module that applies it.

`HoldingsStats` follows `BenchmarkStats`'s all-or-nothing discipline: either the three figures and the three window fields are all populated, or all six are `None` and `unavailable_reason` explains why in a sentence a person can act on. `excluded` maps a ticker to why it is not behind the figures and is independent of that: a report can have both good figures and exclusions.

`weights_from_positions` computes `market_values = {ticker: shares * price}` for every position with a usable (non-NaN, positive) price and returns `(weights, market_values, total_value)`, where `weights` are `market_value / total_value` and `total_value` is the sum over `market_values`. A position whose price is missing contributes to neither.

`holdings_stats` proceeds in this order, and the order matters:

1. Empty `positions`: return immediately with every figure `None` and `unavailable_reason` naming the command that fixes it — `"no holdings are saved for USD; add some with: uv run portfolio-holdings set SPY 1000"`. An empty portfolio is a legitimate state (the user asked for "0+ entries"), not an error.
2. Call `load_returns_matrix(list(positions), as_of, lookback_months, min_months, db_path)`. Its `apply_min_history_rule` performs precisely the exclusion the user asked for, so this is reuse rather than reimplementation.
3. For each requested ticker absent from the resulting columns, record an `excluded` entry stating how many months of returns it actually had inside the window and the threshold it missed — for example `"8 month(s) of monthly returns in the window, under 24"`. Obtaining the actual count requires the pre-rule matrix, so call `load_returns_matrix` with `min_months=0` once and apply `apply_min_history_rule` to its result in this module, rather than calling the loader twice.
4. Price every position with `load_latest_prices(list(positions), as_of, db_path)`. A NaN price is another `excluded` entry (`"no price on or before <as_of>"`), and such a ticker cannot be weighted even if its returns history was fine.
5. Build weights over the survivors — tickers that both cleared the history rule and have a price — via `weights_from_positions`, so they sum to 1.
6. If no ticker survives, return with `unavailable_reason` naming what was excluded and why, rather than calling the estimator on an empty matrix.
7. Otherwise call `stats_for_weights(matrix[survivors], weights, risk_free_rate)` and read `window_start`, `window_end` and `window_months` off `matrix.index` — off the data actually used, exactly as `compute_weights_and_stats` reads its window off the matrix rather than from the configured lookback, so a portfolio with less history than requested reports the window it truly had.
8. `market_values` and `total_value` cover **every** priceable position, including excluded ones — see the `Decision Log` entry for why these two denominators must differ from the weights'.

**Step 2c — resolve the data.** In `src/flow/interactive.py`, beside `prepare_benchmark` and documented against it (together with the private `_holdings_currency_gate` and `_holdings_stats_excluding` helpers it uses - see the `Decision Log` on a hand-edited cross-currency holding):

    def prepare_holdings(positions: dict[str, float], currency: str, rebalance_date: date,
                         db_path: str, risk_free_rate: float = settings.risk_free_rate,
                         allow_fetch: bool = True) -> HoldingsStats

Behavior:

1. Empty positions: delegate straight to `holdings_stats`, which produces the "nothing saved" sentence without touching a database.
2. Count months per held ticker in `db_path`, read-only. If **every** held ticker has at least `HOLDINGS_MIN_MONTHS`, compute against `db_path` and stop. The condition is deliberately "every", not "some": in a live `user_provided` run the session database contains only the candidate pool's tickers, and a partial hit there would silently drop every holding that happens not to also be a candidate.
3. Otherwise, if `allow_fetch` is false, return an `unavailable_reason` naming the flag responsible, in the same shape `prepare_benchmark` uses for `--no-benchmark-fetch`.
4. Otherwise open `build_scratch_snapshot()`, call `validate_and_ingest_tickers(list(positions), rebalance_date, scratch_db_path)`, and compute against the scratch path **inside** the `with` block. Tickers that came back `invalid` become `excluded` entries carrying yfinance's own reason. Writing to a scratch database rather than `db_path` is the correctness rule quoted in `Context and Orientation`: it is what makes it impossible for a holdings fetch to move the candidate pool's returns window, or to add rows to the shared `data/portfolio.duckdb` as a side effect of printing a report.
5. Never raise. Wrap the body and turn any exception into an `unavailable_reason` plus `logger.warning`, exactly as `prepare_benchmark` does, so a report line can never cost a live session its fetched snapshot.

Create `tests/test_holdings.py`. The invariants to pin, and why each one is worth a test:

- **A one-holding portfolio's three figures equal that ticker's own benchmark figures** over the same window, to `abs=1e-9`. This is the direct analogue of `tests/test_benchmark.py::test_a_one_asset_portfolio_equals_its_own_benchmark` and is the contract that makes it legitimate to print the holdings line beneath the benchmark line: if these two ever disagreed for a single asset, the comparison the whole feature exists for would be meaningless.
- Weights are share-times-price normalized, against fixture prices chosen so the expected weights are exact.
- A holding with eight months of returns is excluded, named with its actual month count, and the survivors' weights still sum to 1.
- A holding with no price is excluded with the price reason even when its returns history is complete.
- `total_value` includes an excluded holding's value while `weights` do not.
- An empty portfolio reports the "nothing saved" sentence and no figures, and never touches the database.
- `prepare_holdings` never writes to the session database: assert both the row count in `returns` and the file's `st_mtime_ns` are unchanged, mirroring `tests/test_interactive_flow.py::test_prepare_benchmark_never_writes_to_the_session_database`.
- `prepare_holdings` reads `db_path` without fetching when every ticker already has enough history: monkeypatch `src.flow.interactive.validate_and_ingest_tickers` to a function that fails the test if called.
- `allow_fetch=False` with an insufficient database reports the `--no-holdings-fetch` reason and does not fetch.
- `prepare_holdings` turns an exception from the ingest path into an `unavailable_reason` rather than propagating it.

Commands and acceptance:

    cd /app/agentic_portfolio
    uv run pytest tests/test_optimizer.py -q      # unmodified, must still pass: proves 2a moved nothing
    uv run pytest tests/test_holdings.py -q
    uv run pytest tests/test_*.py -q


### Milestone 3 — the entry point


Scope: the command the user types. At the end of this milestone `uv run portfolio-holdings` exists and maintains `memory/portfolio.json`, printing the figures after every operation. This is the first milestone with behavior a person can see.

Add to `src/flow/cli.py` a single formatter, so the two commands can never drift into two different-looking reports:

    def print_user_portfolio(holdings: HoldingsStats, currency: str, path: str) -> None

Output shape, with money rendered through the existing `format_money` so the unit is never ambiguous:

    Your portfolio (USD), from memory/portfolio.json:
      SPY: 1000 shares  $687,420.00 USD  weight 0.9691
      T:    500 shares   $21,905.00 USD  weight 0.0309
    Total value: $709,325.00 USD
    Returns window: 2021-10-01 to 2026-09-01 (60 month(s) of monthly returns)
    Expected return / volatility (annualized):
      SPY: return=0.1225  volatility=0.1736
      T: return=0.1382  volatility=0.2554
    Annual return: 0.1198  Annual volatility: 0.1451  Sharpe: 0.6879
    Risk-free rate used: 0.0200

The `Expected return / volatility (annualized)` section is the same section, in the same
wording and the same position relative to the portfolio-level line, that
`print_weights_and_allocation` prints for an optimized pool - so the two blocks of one report
can be read against each other line for line. It covers only the holdings behind the figures,
in the same order as the positions above it, for the same reason that function covers only the
weighted tickers: a holding with no estimate has nothing to print.

An indented `Excluded from the figures:` section is appended, one entry per exclusion, whenever `excluded` is non-empty:

    Excluded from the figures:
      NEWCO: 8 month(s) of monthly returns in the window, under 24 (2.0% of total value)

and, when there are no figures at all, a single line carrying the reason, in the same `n/a - reason` shape `format_benchmark` already uses for an unavailable benchmark:

    Your portfolio (USD): n/a - no holdings are saved for USD; add some with: uv run portfolio-holdings set SPY 1000

Print the returns window and the risk-free rate every time figures are printed, never only on request: the same holdings measured over a different window or against a different rate are different numbers, and a figure whose derivation is not stated beside it invites being compared with one derived differently.

Create `src/flow/holdings_cli.py` with `main()`, and register it in `pyproject.toml`:

    portfolio-holdings = "src.flow.holdings_cli:main"

An `argparse` parser with three subcommands, non-interactive throughout (no `input()`; this command is for scripting and for one-line edits):

- `show [--currency USD]` — the default when no subcommand is given, so bare `uv run portfolio-holdings` shows everything. With `--currency`, that one portfolio; without it, every saved currency in turn, each with its own figures.
- `set TICKER SHARES [TICKER SHARES ...]` — validate and ingest each ticker through `validate_and_ingest_tickers` into a scratch snapshot before saving it, so a typo is named rather than silently stored and then quietly dropped by the optimizer weeks later. Which currency's portfolio is edited follows the rule `interactive._in_typed_order` already exists to protect: the **first ticker typed** establishes it (reuse that helper — sorting instead would make `set AAPL 10 7203.T 5` a yen portfolio, because digits sort before letters), and `partition_by_currency` refuses the rest by name, naming the portfolio each one does belong to and the command that would record it there. `--currency` names the target portfolio explicitly instead, in which case a ticker in any other currency is refused. `SHARES` of `0` retires a holding. Accept fractional share counts, and strip a trailing comma from a ticker token so the `SPY, 1000` notation a user naturally writes also works.
- `remove TICKER [TICKER ...]` — no validation needed, since removing a ticker that is not held is already a harmless no-op. Without `--currency`, infer the portfolio from the one that holds the ticker; refuse by name, listing the candidates, when more than one does.

Shared flags: `--path` (default `DEFAULT_PORTFOLIO_PATH`), `--db-path` (default `data/portfolio.duckdb`, read only), `--date` (default `today`, parsed by `cli._parse_date`), `--risk-free-rate` (default `settings.risk_free_rate`), and `--no-fetch`. Every subcommand ends by calling `cli.print_user_portfolio` for the affected currency.

Extend `tests/test_cli.py` (or add `tests/test_holdings_cli.py` if that file's size makes it unwieldy) covering: `set` saving and reporting; `set` of a cross-currency ticker refused by name with nothing saved; `set` of an unresolvable ticker reported as not found with nothing saved; `set X 0` retiring a holding; `remove` inferring the currency and refusing an ambiguous one; `show` with nothing saved printing the `n/a` sentence; and the exclusion line's wording. Monkeypatch `validate_and_ingest_tickers` and `prepare_holdings` at the importing module's own symbol, as every other test in this repository does.

Commands and acceptance — these hit Yahoo Finance and write `memory/portfolio.json`:

    cd /app/agentic_portfolio
    uv run portfolio-holdings set SPY 1000 T 500     # expect: both saved, then the USD block
    uv run portfolio-holdings set 1321.T 50          # expect: a new JPY portfolio and its block
    uv run portfolio-holdings set SPY 1000 7203.T 100  # expect: 7203.T refused, SPY still saved
    uv run portfolio-holdings show                   # expect: both portfolios, each with its own figures
    uv run portfolio-holdings set NOTATICKER 10      # expect: "Ignored (not found)", nothing saved
    uv run portfolio-holdings remove T               # expect: T gone, USD block reprinted


### Milestone 4 — the pipeline section, and the documentation


Scope: the same block inside `uv run portfolio`, plus `README.md` and this plan's closing sections. At the end of this milestone both halves of the user's request are delivered.

In `src/flow/cli.py`'s `main()`, add three arguments:

- `--holdings-path`, default `DEFAULT_PORTFOLIO_PATH`, for pointing at an alternate or scratch store the way `--memory-path` already does for candidate pools.
- `--no-holdings`, leaving the block out entirely — the counterpart of `--benchmark none`, and the flag a user reaches for when they have not recorded a portfolio and do not want to be told so on every run.
- `--no-holdings-fetch`, mirroring `--no-benchmark-fetch`: take the holdings only from what the run's database already holds, which is what keeps a backtest-window run entirely offline.

Then, immediately after `print_pipeline_result(result)` and before `_run_edit_loop`, load the portfolio saved for the run's own `currency` and print the block:

    if not args.no_holdings:
        print_user_portfolio(
            prepare_holdings(
                load_portfolio(args.holdings_path, currency), currency, rebalance_date,
                session_db_path, risk_free_rate=args.risk_free_rate,
                allow_fetch=not args.no_holdings_fetch,
            ),
            currency, args.holdings_path,
        )

Passing `args.risk_free_rate` rather than the configured default is what makes the holdings Sharpe comparable with the pool's and the benchmark's on the lines above it: a `--risk-free-rate 0.04` run must measure all three against 4%, or the three lines would not be reading the same scale. Placing the block after the pipeline report and before the edit loop, printed once, is the `Decision Log`'s eighth entry.

Add a `README.md` bullet in the Live Mode section, in the voice of the existing per-currency and benchmark bullets: what `memory/portfolio.json` holds; that it is one portfolio per currency for the same reason a candidate pool is (prices in two units cannot be weighed against one total, and conversion is not implemented); how `uv run portfolio-holdings` maintains it; that share counts become weights through each holding's latest market price; that the figures use the same estimators and the same risk-free rate as the pool's and the benchmark's, so all three lines read on one scale; that a holding with under 24 months of usable monthly returns is excluded from the figures and named with its share of total value rather than blocking the report; that the holdings' returns window is its own and is always printed; and what `--no-holdings` and `--no-holdings-fetch` do.

Finally, fill in this plan's `Surprises & Discoveries` and `Outcomes & Retrospective` sections with what was actually learned, and mark every `Progress` item.

Commands and acceptance:

    cd /app/agentic_portfolio
    uv run pytest tests/test_*.py -q
    uv run portfolio --date today --objective GMV --value 100000 --selection user_provided

Expect the report to end with the `Your portfolio (USD), ...` block beneath the pool's own figures and its benchmark. Then confirm the two escape hatches:

    uv run portfolio --date today --objective GMV --value 100000 --selection user_provided --no-holdings
    uv run portfolio --date 2024-04-30 --objective GMV --value 100000 --no-holdings-fetch

Expect no holdings block at all in the first, and in the second a single `n/a` line naming `--no-holdings-fetch` whenever the cached database lacks the held tickers.


### Milestone 5 — the what-if loop


Scope: a way to ask what a CHANGE would do, without making it. At the end of this milestone `uv run portfolio-holdings whatif` opens a loop in which hypothetical changes to the saved holdings - INCLUDING adding a ticker the person does not own - are applied, measured and reported with the signed change in all three figures, and nothing whatsoever is written. (That clause was added by item 5f: the original sentence said only "changes to the saved holdings", and both this plan and the CLI's own prompts thereby described a narrower feature than the one that shipped. See `Surprises & Discoveries`.) This milestone was added after the first four shipped, in response to a live run: seeing the current figures immediately raises the question of which change would improve them, and answering that by hand is the hard part. It closes, from the other direction, the gap this plan's own `Outcomes & Retrospective` pre-registered.

**Step 5a — one session, one window.** In `src/flow/interactive.py`, beside `prepare_holdings`:

    class HoldingsSession(NamedTuple):
        db_path: str
        can_ingest: bool

    @contextmanager
    def open_holdings_session(tickers: list[str], rebalance_date: date, db_path: str,
                              allow_fetch: bool = True)

    def measure_holdings(positions: dict[str, float], currency: str, rebalance_date: date,
                         session: HoldingsSession,
                         risk_free_rate: float = settings.risk_free_rate,
                         currencies: dict[str, str] | None = None) -> HoldingsStats

`open_holdings_session` is `open_pipeline_session`'s sibling: a context manager that resolves every holding's prices and returns into ONE throwaway database and keeps it alive for the whole loop. That is a correctness requirement, not an optimization. `src/optimizer/portfolio.py`'s `_load_window_dates` derives the returns window from `SELECT DISTINCT rebalance_date FROM returns` over the whole table, so two variants resolved into two separate throwaway databases can legitimately come back measured over two different windows - and the difference between two Sharpe ratios computed over different months is partly just the difference between the months. A what-if's whole output is that subtraction. One database makes the shared window structural.

`measure_holdings` is the what-if counterpart to `prepare_holdings`: it assumes the data is already resolved, fetches nothing, writes nothing, and ends in the same `_holdings_stats_excluding` so a holding excluded for thin history or the wrong currency is reported identically either way. Like `prepare_holdings` it never raises - losing a session mid-loop over one unmeasurable variant would throw away every fetch already paid for.

With `allow_fetch=False` the session yields `db_path` itself and reports `can_ingest=False`. That is a different promise rather than a degraded one: `--no-holdings-fetch` means touch no network, `db_path` is documented as never written to, so a ticker the cache does not hold cannot join the experiment at all and the caller must refuse it by name.

**Step 5b — the before/after report.** In `src/flow/cli.py`, `print_user_portfolio` gains a keyword-only `heading: str | None = None`. Its header hardcodes `from {path}`, which for a hypothetical block would be a plain falsehood - those holdings are not in that file and never will be. Keyword-only and defaulting to the current wording, so all twenty-odd existing direct callers and their tests are untouched. Then:

    def format_holdings_delta(baseline: HoldingsStats, hypothetical: HoldingsStats) -> str

Signed to four decimals, matching every other figure in the report. It returns a REASON rather than a delta in two cases, which is why it is a function and not three subtractions at the call site: either side may have no figures (subtracting from `None` is not a small bug but a misleading number), and the two may have been measured over different windows. `open_holdings_session` exists to make the second impossible, so reaching it means something upstream is wrong and saying so beats printing a plausible number that answers no question. Only the three portfolio-level figures are diffed: a holding's OWN volatility legitimately moves between blocks even when nothing about that holding changed, because `CovarianceShrinkage.ledoit_wolf` shrinks across whatever cross-section it sits in.

**Step 5c — the loop.** In `src/flow/holdings_cli.py`, `"whatif"` joins `VALID_COMMANDS` and `_run_whatif` joins the dispatch dict. It proceeds in this order:

1. Resolve which currency's portfolio to experiment on, via the existing `_rate_currency_for_report`; refuse by name, pointing at `--currency`, when the command names none.
2. Open one session over the saved holdings' tickers.
3. Measure and print the baseline through `print_user_portfolio` with its ordinary header.
4. Loop on `[s]et shares / [r]emove / [u]ndo all / [f]inish`, snapshotting the positions before each edit so a rejected one reverts, and `continue`ing on unparseable input keeping what you had - `_run_edit_loop`'s discipline throughout, minus the persist step, which makes it strictly simpler than the loop it copies.
5. After any edit that actually changed something, measure the variant and print it under `What if ({currency}) - not saved`, then the delta line.
6. On `[f]inish`, print that nothing was saved and, when anything changed, the one-line `set` command that would apply it.

A ticker typed at `[s]et` that the experiment has not already resolved goes through `_validate_set_targets`, the same function `set` uses, which gains two parameters for this: `db_path` (ingest into the session's database, so the ticker is measurable in the same breath) and `pool_currency` (the experiment's currency, so a yen ticker in a dollar experiment is refused rather than establishing JPY - see `Surprises & Discoveries`). Nothing is written: not `save_portfolio`, and not `_remember_rate`, whose omission is the subtler half.

**Step 5d — the tests Milestone 2 specified and never wrote.** `grep -rn prepare_holdings tests/` found only monkeypatches and docstrings, so that function had never been directly exercised. Milestone 5a builds its sibling in the same module, so the four land now: never writes to the session database, reads the cache without fetching when it suffices, `allow_fetch=False` names the flag, and an ingest exception becomes an `unavailable_reason`.

The invariants pinned for the loop itself, and why each is worth a test:

- **Nothing is written at all** - asserted on `save_portfolio` and `save_risk_free_rate` spies rather than on the files, because a file comparison would also pass if the write happened and wrote identical bytes.
- **Every variant shares one window**, and the delta is the exact arithmetic difference of the two blocks.
- **A differing window withholds the delta** and says so, rather than subtracting across windows.
- A cross-currency ticker is **refused by name with `set`'s wording** and does not enter the hypothetical - the test that caught the `pool_currency` bug.
- A new ticker under `--no-holdings-fetch` is refused and `validate_and_ingest_tickers` is **never called**, the guard against writing to the shared cache.
- `--risk-free-rate` reaches the figures, is **not** remembered, and is **not labelled** as remembered.
- `[u]ndo all` returns to the saved holdings and then offers no `set` command, since nothing changed.

Commands and acceptance - the loop is interactive, so these are driven by piping its answers:

    cd /app/agentic_portfolio
    uv run pytest tests/test_holdings.py tests/test_holdings_cli.py -q
    uv run pytest tests/test_*.py -q

    # against a scratch --path/--rates-path so the real memory/ is untouched
    printf 's\nNVDA 100\nf\n' | uv run portfolio-holdings whatif
    # expect: the baseline block, then "What if (USD) - not saved:", both over the SAME
    #         "Returns window:" line, then "Change from your saved portfolio: ...", then
    #         "Nothing was saved: ..." and "To keep it: uv run portfolio-holdings set NVDA 100"

    printf 's\n7203.T 100\nf\n' | uv run portfolio-holdings whatif       # refused by name
    printf 's\nNVDA 100\nf\n'   | uv run portfolio-holdings whatif --no-fetch  # refused, needs a fetch
    printf 'f\n' | uv run portfolio-holdings --risk-free-rate 0.05 whatif  # "not remembered"

Then confirm the promise by inspection, which is the acceptance that matters most: `md5sum` of `memory/portfolio.json` and `memory/rates.json`, and the size and mtime of `data/portfolio.duckdb`, must be identical either side of the whole session.

### Milestone 6 — the persistent holdings cache


Scope: stop refetching. At the end of this milestone the holdings' prices and monthly returns live in `data/holdings.duckdb` and a repeated report costs no network at all. This was the one open `Progress` item this plan shipped with, deliberately deferred because it needs a staleness policy - "how old may the cached data be before we refetch?" - which is a design question rather than a detail. Milestone 6 answers it.

**Step 6a — the cache and its staleness rule.** A new `src/dataset/holdings_cache.py`:

    DEFAULT_HOLDINGS_CACHE_PATH = "data/holdings.duckdb"

    def latest_expected_rebalance_date(as_of: date) -> date
    def cached_month_ends(tickers: list[str], cache_path: str) -> dict[str, date | None]
    def stale_tickers(tickers: list[str], as_of: date, cache_path: str,
                      force: bool = False) -> list[str]
    def refresh_holdings_cache(tickers: list[str], as_of: date, cache_path: str,
                               force: bool = False
                               ) -> tuple[list[str], dict[str, str], dict[str, str]]

A ticker is stale when the cache does not hold the monthly return for the most recent rebalance date on or before the report's date - so the cache is good for the rest of the calendar month. Two properties make that rule work rather than merely sound plausible. It is derived from the DATA, so "is this fresh?" is answered by looking for the row, and there is no `fetched_at` timestamp that can disagree with the rows it claims to describe. And it reuses `src/dataset/membership.py`'s `compute_rebalance_dates` - the same function that decided where those rows go - so the cache's monthly grid cannot drift from the writer's, which would give a cache that never looks fresh and refetches forever.

`refresh_holdings_cache` returns `validate_and_ingest_tickers`' exact triple, reporting a ticker that needed nothing as valid with the currency the cache recorded for it. That is what lets `_resolve_holdings` treat a cache hit and a cold fetch identically rather than growing two branches. Only the tickers a report asks about are ever refreshed: what-if tickers are cached too, so the file slowly accumulates things nobody owns, and a wholesale refresh would mean refetching everything ever tried. Leftover rows are harmless - every query filters by ticker, and the one that does not (`_load_window_dates`) reads only distinct rebalance dates, the same monthly grid for every ticker.

This is the project's second cache and deliberately not its first. `data/portfolio.duckdb` holds the S&P 500 universe candidate pools are measured against; it must not gain rows as a side effect of reporting what somebody owns, and `_load_window_dates` reads its whole `returns` table, so holdings rows landing there could move the very window a pool's figures are computed over. A separate file makes both impossible - the same reasoning the throwaway database was built on, with the throwing-away removed.

**Step 6b — the honest price date.** The cost of the monthly rule, stated rather than hidden. `HoldingsStats` gains `priced_as_of`, `src/optimizer/portfolio.py` gains `latest_price_date`, and `print_user_portfolio` prints `Total value: $850,722.00 USD (priced 2026-09-04)`. Monthly returns change monthly, but prices change daily and are what turn share counts into a total and into weights - so a cache refreshed on the 3rd and read on the 25th reports a total three weeks old. The date is read off the price rows actually used, never off a refresh timestamp, so it cannot claim a freshness the data lacks. `--refresh-holdings` moves it.

**Step 6c — wiring.** `_resolve_holdings` and `open_holdings_session` resolve through the cache instead of `build_scratch_snapshot`. The order is unchanged at the front: the session database is still consulted first, because that read is free and a backtest-window run against the S&P cache must not start depending on a holdings cache it never needed. `_validate_set_targets` gains `refresh_cache`, so a what-if re-trying a candidate goes through the staleness rule rather than refetching - see `Surprises & Discoveries` for the 7-second-per-retry bug that omitting it caused. Both CLIs gain `--holdings-cache-path` and `--refresh-holdings`.

`--no-holdings-fetch` is deliberately widened to read the cache. Its help text described the letter ("this session's database") while its name describes the intent ("touch no network"), and a local DuckDB file is not the network - so an offline run with a populated cache now works instead of reporting the holdings unmeasurable. The help text and the unavailable message both name both places.

The invariants pinned, and why each is worth a test:

- **A ticker cached this month is not refetched** - the whole point of the file, and the thing a subtly wrong rule breaks silently.
- **A new month makes it stale**, and **a report before its month's first business day expects the previous month** - 2026-11-01 is a Sunday, so November's return cannot exist yet, and getting this wrong refetches on every run for a day or two each month.
- **The cache's monthly grid is `compute_rebalance_dates`' own** - two definitions would give a permanently-stale cache.
- **Only stale tickers are fetched**, and **a stale ticker nobody asks about is never touched**.
- **A null monthly return does not count as coverage** - `src/dataset/returns.py` writes a full month-by-ticker cross product, so a row can exist holding nothing usable.
- **Holdings rows never reach the session database** - the isolation rule, re-pinned now that the destination is persistent.
- **An offline run reads the cache**, and names both places it looked when neither has enough.
- **The report states its price date.**

Commands and acceptance - the first of these fetches, the rest should not:

    cd /app/agentic_portfolio
    uv run pytest tests/test_holdings_cache.py tests/test_holdings.py -q
    uv run pytest tests/test_*.py -q

    # against a scratch --path/--rates-path/--holdings-cache-path
    uv run portfolio-holdings show                    # cold: fetches
    uv run portfolio-holdings show                    # warm: no network
    uv run portfolio-holdings --refresh-holdings show # fetches again on demand
    uv run portfolio-holdings --no-fetch show         # works off the cache
    printf 's\nNVDA 100\nf\n' | uv run portfolio-holdings whatif   # fetches NVDA once
    printf 's\nNVDA 100\nf\n' | uv run portfolio-holdings whatif   # then never again

Expect `Total value: ... (priced YYYY-MM-DD)` on every one of them, and `data/portfolio.duckdb`'s size and mtime unchanged throughout - the isolation rule is the acceptance that matters most, since the cache becoming persistent is exactly the change that could have broken it.

### Milestone 7 — the selectable window


Scope: let a person vary the LENGTH of the returns window, not only the holdings. At the end of this milestone `uv run portfolio-holdings whatif` has a `[w]indow` verb that re-measures over any length from 24 to 60 months, and the report names the length that was asked for. Added because for some holdings the window dominates the answer: `PFF` fell hard in roughly the first 12 months of the current 60-month window (the 2022 rate shock hitting preferred stock) and has been broadly stable since, so it reads as a losing position over 60 months and a modestly winning one over 36. Both are true statements about different spans of months, and 2 to 5 years are all defensible choices, so the person needs to see more than one.

**Step 7a — the window as a validated value.** In `src/optimizer/holdings.py`, beside `HOLDINGS_MIN_MONTHS`:

    DEFAULT_LOOKBACK_MONTHS = 60

    def validate_lookback_months(months: object, source: str) -> int

Modelled on `src/flow/rate_memory.py`'s `validate_risk_free_rate`: takes `object` so a value from anywhere gets the same scrutiny, names `source` in every message, raises `ValueError`, returns the coerced value. Refuses anything outside `HOLDINGS_MIN_MONTHS .. DEFAULT_LOOKBACK_MONTHS`, and rejects a `bool` ahead of the numeric check since `isinstance(True, int)` is true in Python and would otherwise mean a one-month window. `holdings_stats`' bare `lookback_months: int = 60` becomes the constant while we are there - its neighbour `min_months` already used a named one, and the asymmetry is what made the 60 hard to find.

**Step 7b — threading.** `lookback_months` on `interactive.measure_holdings` and `interactive._holdings_stats_excluding`, passed to their `holdings_stats` calls. That is the entire chain: below `src/flow`, `load_returns_matrix_unfiltered`, `load_returns_matrix` and `holdings_stats` all had the parameter already and had simply never been given a value. `prepare_holdings` is deliberately NOT given one - see the `Decision Log` on why a holdings-only window in `uv run portfolio` would falsify a tested guarantee.

**Step 7c — the loop.** `[w]indow` joins `WHATIF_PROMPT`, and `_whatif_window(current)` reads a length, keeping what you had on an unusable answer. It proceeds in this order:

1. Read the answer; a blank one keeps the current window.
2. Parse to `int`; on failure hand the RAW TEXT to `validate_lookback_months` rather than letting `int`'s own "invalid literal for int() with base 10" reach the person - one message then covers a non-number and an out-of-range number, and it is the message that explains the bounds.
3. If the length did not change, `continue` without re-measuring.
4. Otherwise re-measure the BASELINE at the new length, because `format_holdings_delta` withholds a delta across differing windows and leaving the baseline behind would turn every later comparison into an apology.
5. Report. If the hypothetical holdings equal the saved ones - which a window-only change guarantees - reprint the baseline AS the baseline rather than heading the person's real portfolio `What if - not saved` above a row of zero deltas.

**Step 7d — saying the window was chosen.** `print_user_portfolio` gains a keyword-only `window_origin`, mirroring `risk_free_rate_origin`, appended to the existing line:

    Returns window: 2023-10-02 to 2026-09-01 (36 month(s) of monthly returns, 36 requested)

The count stays derived from the data and the request is named beside it, so a chosen window is never mistaken for all the data there was, and a request that could not be honoured shows both numbers instead of quietly reporting the smaller one. Nothing is annotated at the default, so an ordinary run reads exactly as it did before.

The invariants pinned, and why each is worth a test:

- **A shorter window actually changes the figures.** A parameter that reached the estimator and moved nothing would look like success while being inert, which is precisely what a threading bug produces.
- **The window is derived, the request is named** - including when fewer months exist than were asked for.
- **At a 24-month window a holding missing one month is excluded and named**, the documented consequence of keeping the bar at 24.
- **23, 61, 0, -1, `36.5`, `"abc"` and `True` are all refused by name**, and a zero or negative one never reaches DuckDB's `LIMIT`.
- **A window change re-measures the baseline**, so the next delta prints figures rather than the "measured over different windows" apology - the symptom a naive implementation shows.
- **An unusable answer keeps the previous window** and says so, in the validator's words rather than `int`'s.
- **An unchanged portfolio is not labelled a what-if**, after a window change or an `[u]ndo all`.
- **The prompt advertises `[w]indow`**, per item 5f's lesson about invisible capability.

Commands and acceptance - the loop is interactive, so drive it with `printf`:

    cd /app/agentic_portfolio
    uv run pytest tests/test_holdings.py tests/test_holdings_cli.py -q
    uv run pytest tests/test_*.py -q

    # against a scratch --path/--rates-path/--holdings-cache-path holding PFF
    printf 'w\n36\nf\n' | uv run portfolio-holdings whatif
    # expect the baseline over 60 months, then the same holdings over 36 with
    # "36 month(s) of monthly returns, 36 requested" - and the figures to MOVE:
    #   60 months: Annual return: -0.0105  Annual volatility: 0.1108  Sharpe: -0.2752
    #   36 months: Annual return:  0.0367  Annual volatility: 0.0802  Sharpe:  0.2079

    printf 'w\n24\nf\n'  | uv run portfolio-holdings whatif   # the tight end of the range
    printf 'w\n12\nf\n'  | uv run portfolio-holdings whatif   # refused, naming 24-60
    printf 'w\n61\nf\n'  | uv run portfolio-holdings whatif   # refused, naming the 65-month ingest
    printf 'w\nabc\nf\n' | uv run portfolio-holdings whatif   # keeps 60, in the validator's words

    # the actual use case: a shorter window AND a change, delta still printing
    printf 'w\n36\ns\nSPY 200\nf\n' | uv run portfolio-holdings whatif

If the PFF figures do NOT move between 60 and 36 months, the window is not reaching the estimator and the feature is inert. And `memory/portfolio.json`, `memory/rates.json` and `data/portfolio.duckdb` must all be unchanged, since a what-if still writes nothing.

## Validation and Acceptance


Run the whole suite from the repository root; every pre-existing test must still pass, and the three new test files must pass:

    cd /app/agentic_portfolio
    uv run pytest tests/test_*.py -q

`tests/test_optimizer.py` must pass **without being edited**, which is what proves Milestone 2a's extraction was behavior-preserving.

The end-to-end behavior a human can verify, phrased as observations rather than internals:

1. `uv run portfolio-holdings set SPY 1000` prints `Added to the USD portfolio: SPY (1000 shares).` followed by a block naming `SPY`, its market value, a weight of `1.0000`, a total value, a returns window, and three figures. Because a single holding is its own benchmark, `uv run portfolio --date today --objective GMV --value 10000 --selection user_provided --benchmark SPY` against a pool of just `SPY` prints a benchmark line whose three numbers match the holdings block's to four decimal places — the visible form of the equality Milestone 2's first test pins.
2. `uv run portfolio-holdings set SPY 1000 7203.T 100` prints `Refused: 7203.T is priced in JPY, so it belongs to the JPY portfolio, not the USD one.` and names the command that would record it, while still saving `SPY` - one refused ticker never blocks a good one typed beside it. A `set 7203.T 100` on its own is NOT refused: a ticker's own currency selects its portfolio, so that creates or edits the JPY one.
3. `uv run portfolio-holdings --currency JPY set 1321.T 50` then `uv run portfolio-holdings show` prints two blocks, USD and JPY, each with money in its own currency and its own returns window.
4. `uv run portfolio-holdings set NOTATICKER 10` prints `Ignored (not found): NOTATICKER.` and saves nothing.
5. An ordinary pipeline run ends with the holdings block; `--no-holdings` removes it; `--no-holdings-fetch` on a cold cache replaces the figures with one `n/a` line naming that flag.
6. The isolation rule holds by inspection: record `data/portfolio.duckdb`'s size and `mtime`, run `uv run portfolio-holdings show`, and confirm both are unchanged, proving the holdings fetch went to a throwaway database.


## Idempotence and Recovery


Every step here is additive and repeatable. Milestones 1 and 2 add modules and one behavior-preserving extraction; re-running their tests is free. `save_portfolio` is idempotent for identical input except for `updated_at`, and it never touches another currency's entry, so a repeated `set` is harmless. `set` and `remove` read the file before writing it, so an interrupted command leaves the previous file intact.

Recovery paths, should something go wrong:

- `memory/portfolio.json` is a small hand-editable JSON file outside version control (`memory/` is gitignored). A file corrupted by hand-editing raises `json.JSONDecodeError` naming the file; deleting it resets to "no portfolios saved", losing only the share counts, which the user can retype.
- No step migrates or rewrites `memory/candidates.json`, and no step writes to `data/portfolio.duckdb` — verifiable by the size-and-mtime check in `Validation and Acceptance`. So there is nothing to back up and nothing to roll back beyond `git checkout` of the source files.
- The scratch databases `build_scratch_snapshot` creates are deleted when their `with` block exits, including on an exception, so a failed fetch leaves no temporary files behind.


## Artifacts and Notes


The shape of the store after the Milestone 3 acceptance commands, taken from a real run:

    {
      "portfolios": {
        "USD": {
          "positions": {
            "SPY": 1000.0
          },
          "updated_at": "2026-09-06T05:02:44.117293+00:00"
        },
        "JPY": {
          "positions": {
            "1321.T": 50.0
          },
          "updated_at": "2026-09-06T05:03:12.884401+00:00"
        }
      }
    }

The live acceptance run of 2026-09-06, showing the whole point of the feature - the optimizer's
suggested pool, the market, and what the person actually holds, all measured over the same
sixty months with the same estimators and the same 2% risk-free rate, on one screen:

    $ printf '\nd\nf\n' | uv run portfolio --date today --objective GMV --value 100000 \
        --selection user_provided

    Portfolio currency: USD - --value is interpreted as USD
    Returns window: 2021-10-01 to 2026-09-01 (60 month(s) of monthly returns)

    Weights:
      AAPL: 0.5496
      MSFT: 0.4504

    Expected return / volatility (annualized):
      AAPL: return=0.1635  volatility=0.2343
      MSFT: return=0.1351  volatility=0.2510

    Portfolio expected return: 0.1507  Portfolio volatility: 0.1958  Portfolio Sharpe: 0.6675
    Benchmark SPY: return=0.1225  volatility=0.1481  Sharpe=0.6919  (60 of 60 month(s))
    Risk-free rate used: 0.0200
    Target annual return: n/a (objective is GMV, not MV)

    Share allocation:
      AAPL: 171
      MSFT: 90
    Leftover cash: $312.13 USD

    Your portfolio (USD), from memory/portfolio.json:                          <-- new
      SPY: 1,000 shares  $770,190.00 USD  weight 1.0000                        <-- new
    Total value: $770,190.00 USD                                               <-- new
    Returns window: 2021-10-01 to 2026-09-01 (60 month(s) of monthly returns)  <-- new
    Annual return: 0.1225  Annual volatility: 0.1481  Sharpe: 0.6919           <-- new
    Risk-free rate used: 0.0200                                                <-- new

Note that the holdings block's three figures are identical to the `Benchmark SPY` line's, to
every printed decimal. That is not a coincidence and it is not a bug: this portfolio holds
`SPY` and nothing else, so it IS the benchmark, and the two agreeing is the visible form of the
comparability contract `tests/test_holdings.py::test_a_one_holding_portfolio_equals_its_own_benchmark`
pins. Had they disagreed, the two blocks could not honestly be read together at all.

A per-currency `set`, showing that a ticker's own currency routes the edit and that a refused
ticker never blocks a good one typed beside it:

    $ uv run portfolio-holdings set 1321.T 50
    Recorded in the JPY portfolio: 1321.T (50 shares).

    Your portfolio (JPY), from memory/portfolio.json:
      1321.T: 50 shares  ¥3,366,500.00 JPY  weight 1.0000
    Total value: ¥3,366,500.00 JPY
    Returns window: 2021-10-01 to 2026-09-01 (60 month(s) of monthly returns)
    Annual return: 0.2159  Annual volatility: 0.1770  Sharpe: 1.1065
    Risk-free rate used: 0.0200

    $ uv run portfolio-holdings set SPY 1000 7203.T 100
    Refused: 7203.T is priced in JPY, so it belongs to the JPY portfolio, not the USD one.
    Record it with: uv run portfolio-holdings --currency JPY set 7203.T N
    Recorded in the USD portfolio: SPY (1000 shares).

And the offline escape hatch, on a session database that does not hold the holding - note that
what is owned is still reported even though the figures cannot be:

    $ uv run portfolio --date today ... --no-holdings-fetch

    Your portfolio (USD), from memory/portfolio.json:
      SPY: 1,000 shares  value n/a  weight n/a
    Figures: n/a - SPY has under 24 month(s) of returns in this session's database and
    fetching more was disabled with --no-holdings-fetch

The isolation rule, checked by hand around a full `portfolio-holdings` session that fetched
three tickers' price history:

    $ stat -c '%s %Y' data/portfolio.duckdb    # before
    46936064 1788571364
    $ stat -c '%s %Y' data/portfolio.duckdb    # after
    46936064 1788571364

Byte-identical, mtime unchanged: every fetch went to a throwaway database, so recording what
somebody owns cannot add rows to the shared S&P 500 cache or move the window a candidate pool
is measured over.


## Interfaces and Dependencies


No new third-party dependency. Everything needed is already declared in `pyproject.toml`: `pandas` and `numpy` for the frames, `pyportfolioopt` for the estimators and `portfolio_performance`, `duckdb` for the cache, `yfinance` (reached only through `src/dataset/ticker_ingestion.py`) for prices, and `pytest` for the tests.

The interfaces that must exist at the end of the plan, by full path:

In `src/flow/user_portfolio.py`:

    DEFAULT_PORTFOLIO_PATH: str

    def load_all_portfolios(path: str = DEFAULT_PORTFOLIO_PATH) -> dict[str, dict[str, float]]: ...
    def load_portfolio(path: str = DEFAULT_PORTFOLIO_PATH,
                       currency: str = DEFAULT_CURRENCY) -> dict[str, float]: ...
    def save_portfolio(positions: dict[str, float], path: str = DEFAULT_PORTFOLIO_PATH,
                       currency: str = DEFAULT_CURRENCY) -> None: ...

In `src/optimizer/portfolio.py`:

    def _estimate_mu_and_cov(returns_matrix: pd.DataFrame) -> tuple[pd.Series, pd.DataFrame]: ...
    def load_returns_matrix_unfiltered(tickers: list[str], as_of: date, lookback_months: int = 60,
                                       db_path: str = settings.db_path) -> pd.DataFrame: ...
    def _per_ticker_figures(mu: pd.Series, cov_matrix: pd.DataFrame
                            ) -> tuple[dict[str, float], dict[str, float]]: ...
    class WeightedStats(NamedTuple): ...     # expected_returns, volatility, and the three
                                             # portfolio-level figures
    def stats_for_weights(returns_matrix: pd.DataFrame, weights: dict[str, float],
                          risk_free_rate: float = settings.risk_free_rate
                          ) -> WeightedStats: ...

In `src/optimizer/holdings.py`:

    HOLDINGS_MIN_MONTHS: int

    class HoldingsStats(NamedTuple): ...      # fields as listed in Milestone 2
    def unavailable_holdings(currency: str, positions: dict[str, float], risk_free_rate: float,
                             reason: str, market_values: dict[str, float] | None = None,
                             total_value: float | None = None,
                             excluded: dict[str, str] | None = None) -> HoldingsStats: ...
    def stored_month_counts(tickers: list[str], as_of: date, db_path: str) -> dict[str, int]: ...
    def weights_from_positions(positions: dict[str, float], latest_prices: pd.Series
                               ) -> tuple[dict[str, float], dict[str, float], float]: ...
    def holdings_stats(positions: dict[str, float], as_of: date, db_path: str, currency: str,
                       risk_free_rate: float = settings.risk_free_rate,
                       lookback_months: int = 60,
                       min_months: int = HOLDINGS_MIN_MONTHS) -> HoldingsStats: ...

In `src/flow/interactive.py`:

    def prepare_holdings(positions: dict[str, float], currency: str, rebalance_date: date,
                         db_path: str, risk_free_rate: float = settings.risk_free_rate,
                         allow_fetch: bool = True) -> HoldingsStats: ...

In `src/flow/cli.py`:

    def format_share_count(shares: float) -> str: ...
    def print_user_portfolio(holdings: HoldingsStats, path: str) -> None: ...

The currency is taken from `holdings.currency` rather than passed alongside, and
`load_returns_matrix_unfiltered` is everything `load_returns_matrix` does except
`apply_min_history_rule` - see the `Decision Log` and `Surprises & Discoveries` for why each
of those shapes is what it is. `unavailable_holdings` is the shared "there are no figures"
constructor, public because `prepare_holdings` needs it for the cases it discovers before
`holdings_stats` is reached; `stored_month_counts` is the read-only count that decides
whether a fetch is needed at all, opening `db_path` read-only and reporting a missing file or
table as zero, so asking the question neither creates a database nor locks one.

In `src/flow/holdings_cli.py`:

    def main() -> None: ...

In `pyproject.toml`, under `[project.scripts]`:

    portfolio-holdings = "src.flow.holdings_cli:main"


## Revision Notes


- 2026-09-06, after implementation: `Progress`, `Surprises & Discoveries`, `Decision Log`,
  `Outcomes & Retrospective` and `Artifacts and Notes` filled in from the work as actually done,
  and three claims corrected where implementing them proved them wrong. First, the acceptance
  section said `set 7203.T 100` against an existing USD portfolio would be refused; a ticker's
  own currency now selects the portfolio it lands in, because refusing it would leave no way to
  start a second currency's portfolio without first discovering `--currency`. Second,
  `print_user_portfolio` lost its separate `currency` argument in favour of `holdings.currency`,
  so a caller cannot label money figures with a currency that disagrees with them. Third,
  `load_returns_matrix` was split rather than called with `min_months=0` as the plan assumed,
  because that value raises `IndexError` on an all-null column and because the unfiltered matrix
  is what the exclusion messages need in any case. Each correction is recorded with its reason in
  the `Decision Log` or `Surprises & Discoveries` above.
- 2026-09-06, cleanup: `interactive._in_typed_order` and `cli._parse_date` became public
  (`in_typed_order`, `parse_date`), since `src/flow/holdings_cli.py` legitimately needs both and
  no other module in `src/` imports a private name across a module boundary. Both docstrings now
  say why they are shared: the typed-order rule protects against the same first-ticker-picks-the-
  currency hazard for portfolios that it already protected candidate pools from, and two commands
  that disagreed about what `--date today` means would be worse than a shared name.
- 2026-09-06, after a live test: the holdings block now prints each holding's own annualized
  expected return and volatility, not only the portfolio-level three. The omission was a real
  gap rather than a style choice - the report for an optimized pool has always answered "which
  holding contributed what", and a reader looking at two blocks in one report will compare a
  holding's line against the same ticker's line in the pool above it. Delivered by extracting
  `_per_ticker_figures` from `compute_weights_and_stats` (so exactly one place turns `(mu,
  cov_matrix)` into per-ticker figures) and widening `stats_for_weights`' return from a bare
  triplet to a `WeightedStats` record carrying both maps, which `HoldingsStats` now echoes
  under the same field names `PortfolioStats` uses. Pinned by
  `tests/test_holdings.py::test_each_holdings_own_figures_match_what_the_optimizer_reports_for_it`,
  which asserts the per-holding numbers equal what `compute_weights_and_stats` reports for the
  same tickers over the same months.

  Worth knowing when reading the output: a holding's printed volatility depends on the
  cross-section it sits in, because the covariance is Ledoit-Wolf shrunk. `SPY` alone reports
  0.1481; in a three-holding portfolio beside `GOOGL` and `T` the same `SPY` reports 0.1736.
  That is the shrinkage behaviour `src/optimizer/benchmark.py`'s module docstring already
  documents at length, it is identical to what an optimized pool's report does, and it is the
  reason the benchmark is deliberately never folded into a pool's matrix.
- 2026-09-06, superseded in part by `plans/14_per_currency_risk_free_rate.md`: the risk-free rate is
  no longer one global value. Three things this plan specifies are now out of date. The
  `--risk-free-rate` flag on `uv run portfolio-holdings` no longer defaults to
  `settings.risk_free_rate` but to `None`, so that "not given" can be told from "given 0.02" and a
  remembered per-currency rate can fill the gap; a value given there is now REMEMBERED for that
  currency. Its help text no longer says "Use the same value here as for 'uv run portfolio' if you
  intend to compare the two reports" — the two commands now share the value through
  `memory/rates.json` automatically, which was the point. And `print_user_portfolio` gained a
  trailing optional `risk_free_rate_origin`, so the `Risk-free rate used:` line names which of the
  sources the rate came from; that line now also prints in the `Figures: n/a` branch, where it was
  previously withheld from exactly the reader trying to work out why the numbers had gone.

  Worth recording because this plan's own claim made it matter: the sentence here that "the three
  figures use the same estimators and the same `--risk-free-rate` as the pool's and the benchmark's,
  so all three lines read on one scale" was true for a USD portfolio and quietly false for a JPY one,
  since the 2% default is a dollar rate. The claim now holds for both.
- 2026-09-06, extended with Milestone 5 (the what-if loop): a completed plan was reopened rather
  than given a `plans/15_*.md`, deliberately. `AGENTS.md` reserves a new numbered plan for "a major
  step of the project" and `PLANS.md` requires each milestone to "incrementally implement the
  overall goal of the execution plan" - and this work serves *this* plan's stated goal (report what
  the held portfolio has done) rather than introducing a new one. It also closes, from the other
  direction, a gap this plan's own `Outcomes & Retrospective` had pre-registered: "stops short of
  'here is what rebalancing would buy you'". The user asked for it here for the same reason.

  Three things about it are worth carrying forward. The one-throwaway-database design is a
  correctness device, not a speed one: `_load_window_dates` reads the whole `returns` table, so
  variants resolved separately can be measured over different windows and their Sharpe difference
  would then be partly a difference of months. Two of the four bugs found during implementation
  were the report claiming something untrue rather than computing something wrong - a yen ticker
  silently accepted into a dollar experiment, and a rate labelled "remembered" by a command that
  remembers nothing - which suggests the provenance discipline `plans/14` introduced deserves the
  same suspicion applied to every label, not only to numbers. And this milestone finally wrote the
  four `prepare_holdings` tests Milestone 2 specified in this very file and never delivered; a plan
  listing tests is not evidence they exist, and `grep` is cheap.
- 2026-09-06, extended with Milestone 6 (the persistent holdings cache): the last open `Progress`
  item on this plan, built at the user's request. It required superseding this plan's own Decision
  Log entry that had rejected `data/holdings.duckdb` - marked as such above rather than rewritten,
  since that entry was right that the staleness question had to be answered first and only wrong
  that it could not be answered here.

  The answer the user chose - stale when the cache lacks the report month's return - is the one I
  had flagged as risking a stale `Total value`, since prices move daily while monthly returns do
  not. They chose it anyway, so rather than override the choice or hope nobody noticed, the cost is
  now visible: `Total value` carries the date of the prices behind it, read off the rows actually
  used. That is the same discipline `plans/14` established for the risk-free rate, and it turns a
  silent staleness into a stated one. `--refresh-holdings` is the escape hatch.

  Every bug found was caused by the cache moving something out from under existing code rather than
  by the cache itself. The what-if loop's add path kept refetching because it was written when its
  database was always a throwaway. And three tests began reaching the real network because the
  fetch moved behind a new module and their stubs were pinned to the old callers - one of which
  kept PASSING, because the live lookup happened to agree with the stub it had bypassed, leaving a
  stray DuckDB file in the repository root as the only sign. Both are recorded in
  `Surprises & Discoveries`. The lesson worth carrying: making a throwaway thing persistent changes
  every assumption anybody made about it being throwaway, those assumptions are not all in the file
  being changed, and a green suite does not prove they were found - checking for files nobody meant
  to create does.
- 2026-09-06, item 5f: the reported gap - "`whatif` focuses on existing holding tickers, expand it
  to add new ones" - turned out to be documentation rather than code. Adding an unheld ticker had
  worked since Milestone 5, was pinned by
  `test_whatif_reports_the_baseline_then_the_hypothetical`, and was the subject of this plan's own
  headline live finding; Milestone 6's `refresh_cache=True` exists solely to make re-trying one
  free. What was missing was any user-facing sentence saying so. Six strings in
  `src/flow/holdings_cli.py` reworded - the loop prompt now reads
  `[s]et shares (any ticker)`, the sub-prompt `Ticker and shares, held or not` - plus two tests
  pinning the prompts so a future tidy-up cannot quietly restore the confusion. No logic changed
  and no behaviour changed; the words now match a feature that was already there.

  Recorded at length in `Surprises & Discoveries` because the general lesson outlives this fix:
  this project is rigorous about never printing a number without its provenance, and had no
  equivalent habit for whether a feature announces itself. A capability documented only in the
  README is, from the prompt, indistinguishable from one that does not exist.
- 2026-09-06, extended with Milestone 7 (a selectable returns window inside `whatif`): the window
  length can now be varied from 24 to 60 months in the what-if loop, because for a holding like
  `PFF` the choice dominates the answer - 60 months gives a Sharpe of -0.2752 and 36 months, which
  excludes the 2022 rate shock, gives +0.2079.

  This does NOT supersede `plans/05_optimizer_and_allocation.md`'s 60-month/24-month decision. The
  default is unchanged, the optimizer and the candidate pool are untouched, `prepare_holdings` was
  deliberately left alone so `show`, `set`, `remove` and every `uv run portfolio` block still
  measure over the fixed 60, and only an explicitly-requested exploration inside `whatif` can
  differ. What it does narrow is that plan's second decision - "use whatever months a ticker
  actually has, between 24 and 60" - at the very bottom of the range, where a 24-month window
  leaves the 24-month bar no room to tolerate anything; recorded in the `Decision Log` above.

  Two things worth carrying forward. The parameter had existed on `load_returns_matrix` and
  `holdings_stats` since those functions were written and had never once been passed by anything in
  `src/flow` or `tests`, so a feature that looked like new capability was two signatures of
  threading. And it exposed a false label that predated it: a window-only change leaves the
  holdings untouched, so the loop printed the person's real portfolio under `What if - not saved`
  with zero deltas beneath - which had also always happened after `[u]ndo all`, unnoticed because
  nobody had a reason to do it.
