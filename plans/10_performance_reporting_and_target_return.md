# Report the numbers behind the weights, and let the user set MV's target return


This ExecPlan is a living document. The sections `Progress`, `Surprises & Discoveries`, `Decision Log`, and `Outcomes & Retrospective` must be kept up to date as work proceeds. This plan must be maintained in accordance with `PLANS.md` at the repository root. This plan builds on `plans/05_optimizer_and_allocation.md` (for `load_returns_matrix`/`compute_weights`/`load_latest_prices`/`allocate_shares`, the four functions it changes or wraps) and `plans/06_interactive_flow.md` (for the CLI, its printing functions, and its post-run editing loop) — both checked into this repository.


## Purpose / Big Picture


Before this change, running this project's pipeline told you *what* to buy but never *why* those holdings were supposed to be good. The command printed each ticker's portfolio weight, a whole-share allocation, and the leftover cash — and nothing else. A person could not see what annual return the optimizer expected from any holding, how volatile it believed that holding to be, what return and risk it expected from the portfolio as a whole, what Sharpe ratio that implied, or against which risk-free rate that Sharpe ratio was measured. Those are the numbers the optimizer actually made its decision from, and all of them were computed internally and then thrown away.

The second, related gap was worse than merely invisible. One of the three optimization objectives, `MV`, is defined by a target annual return — you tell it the return you want and it finds the least-risky way to reach it. That target was hardcoded at 12% a year in `src/optimizer/portfolio.py` and was never shown to the user nor settable by them. So a person choosing `--objective MV` was silently optimizing for somebody else's goal with no way to see or change it, which defeats the point of picking that objective at all.

After this change, every run reports the numbers behind its own answer; `MV`'s target return is both visible and adjustable, from the command line before the run and interactively during it; and the risk-free rate that every reported Sharpe ratio is measured against is both named in the output and settable per run with `--risk-free-rate`.

Concretely, this is the observable outcome, from a real run on 2026-09-05 (the `$` line is what a person types; the candidate pool `AAPL MSFT SPY` was typed at the prompts that precede this output):

    $ uv run portfolio --date today --objective MV --target-return 0.10 --value 100000 --selection user_provided

    Mode: live  Rebalance date: 2026-09-05  Objective: MV  Selection: user_provided

    Scanner branch: user_provided  (buy_s=None buy_f=None intersection=None union=None)
    Candidates (3): AAPL, MSFT, SPY

    Weights:
      SPY: 0.8664
      MSFT: 0.0731
      AAPL: 0.0604

    Expected return / volatility (annualized):
      SPY: return=0.1225  volatility=0.1553
      MSFT: return=0.1351  volatility=0.2505
      AAPL: return=0.1635  volatility=0.2301

    Portfolio expected return: 0.1259  Portfolio volatility: 0.1540  Portfolio Sharpe: 0.6873
    Risk-free rate used: 0.0200
    Target annual return: 0.1000

    Share allocation:
      AAPL: 19
      MSFT: 15
      SPY: 112
    Leftover cash: $163.79

Everything below `Weights:` is new. The per-ticker `return`/`volatility` lines are the optimizer's own annualized estimates for the holdings it chose, and they explain the weights: `SPY` takes 87% of the portfolio because at 15.5% volatility it is much steadier than the two individual stocks, not because it has the best expected return — it has the worst of the three. `Risk-free rate used: 0.0200` names the rate the Sharpe ratio was measured against, which was previously not just unshown but genuinely inconsistent (see Surprises & Discoveries).

One line in that transcript needs explaining, because it looks wrong and is not. `Target annual return: 0.1000` was asked for, yet `Portfolio expected return: 0.1259` came back higher. That is correct `MV` behavior: the constraint is "return at least the target", so when the least-risky portfolio available already returns 12.59% on its own, a 10% target asks for nothing extra and the optimizer simply returns that portfolio. A target only changes the answer once it exceeds what minimum-risk investing already achieves — which is exactly what the next step demonstrates.

Continuing the same session into the interactive loop, without restarting or refetching anything:

    Edit candidates? [a]dd tickers / [r]emove tickers / [o]bjective / [t]arget-return / [f]inish: t
    New target annual return (current 0.1): 0.15

    Candidates (3): AAPL, MSFT, SPY

    Weights:
      AAPL: 0.6230
      SPY: 0.2210
      MSFT: 0.1561

    Portfolio expected return: 0.1500  Portfolio volatility: 0.1870  Portfolio Sharpe: 0.6953
    Risk-free rate used: 0.0200
    Target annual return: 0.1500

Raising the target to 15% forced the optimizer to buy return it would rather have avoided: `AAPL`, the highest-return and second-most-volatile holding, went from 6% of the portfolio to 62%, the reported portfolio return landed exactly on the new target, and portfolio volatility rose from 15.4% to 18.7% — the price of the extra return, now visible. That cause and effect was impossible to see, or to ask for, before this change.

Asking for more than the candidates can deliver is reported rather than fatal, which matters because this loop is holding live mode's only copy of the freshly fetched data:

    Edit candidates? ... / [t]arget-return / [f]inish: t
    New target annual return (current 0.1): 0.18

    Candidates (3): AAPL, MSFT, SPY
    Cannot optimize that edit: target_return must be lower than the maximum possible return
    Keeping the previous candidates, objective, and target return.

No combination of assets returning at most 16.35% can reach 18%, so there is no portfolio to report; the session says so and carries on with the target it had.


## Progress


- [x] (2026-09-05) Added `DEFAULT_TARGET_ANNUAL_RETURN`, `PortfolioStats`, `_fit_efficient_frontier`, and `compute_weights_and_stats` to `src/optimizer/portfolio.py`, leaving `compute_weights`'s signature and behavior untouched (it now calls the shared helper with an explicit `risk_free_rate=0.0`, which is what PyPortfolioOpt was already defaulting to).
- [x] (2026-09-05) Threaded `target_annual_return`/`risk_free_rate` through `src/flow/interactive.py`'s `compute_weights_and_allocation`, `run_pipeline_against`, and `run_pipeline`, and added the additive `"stats"` key to the pipeline result dict while keeping `"weights"` a plain `dict[str, float]`.
- [x] (2026-09-05) Added the `--target-return` argument, the expanded `print_weights_and_allocation` output, the `_prompt_target_return` helper, and the `[t]arget-return` edit command plus the switch-to-MV prompt to `src/flow/cli.py`.
- [x] (2026-09-05) Fixed a session-ending crash the target-return feature exposed in `_run_edit_loop`: an unreachable target made PyPortfolioOpt raise `ValueError` straight out of the loop, destroying the live snapshot. The recompute is now guarded, the edit reverted, and — because the same failure is reachable by an add/remove edit — the `user_provided` pool is persisted only after the recompute succeeds. See Surprises & Discoveries and the Decision Log.
- [x] (2026-09-05) Added a `--risk-free-rate` command-line argument, defaulting to `settings.risk_free_rate`, threaded into both `run_pipeline_against` and `_run_edit_loop` — the latter being necessary rather than tidy, since a rate applied only to the initial run would silently revert to the configured default on the first interactive edit. This reverses an earlier decision in this plan; see the Decision Log and Revision Note 2.
- [x] (2026-09-05) Added 29 tests: 10 in `tests/test_optimizer.py` (18 to 28), 3 in `tests/test_interactive_flow.py` (23 to 26), and 16 in `tests/test_cli.py` (13 to 29). Only one pre-existing test construct changed, as predicted: `tests/test_cli.py`'s `stub_optimizer` fixture.
- [x] (2026-09-05) Verified the full suite: `uv run pytest tests/test_*.py` reports 217 passed, up from 188. Verified end to end with real runs: live `MV` with an interactive target change and an unreachable target, live `MSR` at three different risk-free rates, and a backtest-date `GMV` to `MV` switch, with `data/portfolio.duckdb` confirmed byte-identical afterwards. Transcripts in Artifacts and Notes.


## Surprises & Discoveries


- Discovery (2026-09-05): this project's configured risk-free rate was never reaching the optimizer, so `MSR` was maximizing a different Sharpe ratio than the one the project reports elsewhere. `src/config/settings.py` has defined `risk_free_rate: float = 0.02` since the settings module was written, and `src/flow/backtest.py`'s `compute_sharpe_ratio` uses it to score a whole backtest run — but `src/optimizer/portfolio.py`'s `MSR` branch called `ef.max_sharpe()` with no arguments, and PyPortfolioOpt's signature is `max_sharpe(self, risk_free_rate=0.0)`. So the weights were fitted against a 0% rate while the results were judged against a 2% one. The same was true of the one existing `ef.portfolio_performance()` call. This plan fixes it on the CLI path only, deliberately (see the Decision Log entry on keeping `compute_weights` untouched), and `test_compute_weights_msr_still_fits_at_zero_risk_free_rate` pins the backtest path's old behavior so the difference cannot drift unnoticed.

- Surprise (2026-09-05): passing a nonzero risk-free rate introduces a failure mode that could not occur at 0%. PyPortfolioOpt's `max_sharpe` begins with a guard:

        if max(self.expected_returns) <= risk_free_rate:
            raise ValueError(
                "at least one of the assets must have an expected return exceeding the risk-free rate"
            )

  At the old implicit 0% this only triggered for a pool where every asset had a non-positive expected return. At 2% it triggers for any pool whose best asset is expected to return 2% or less — unusual but entirely reachable, for instance a pool of bond funds in a flat year. The exception is left to propagate from `compute_weights_and_stats` rather than masked, since maximizing a Sharpe ratio really is undefined there and quietly returning some other portfolio would misreport what was optimized; the interactive loop now catches it at the session boundary instead (see the next entry).

- Discovery (2026-09-05): making `MV`'s target user-settable turned a latent crash into an easily-reachable one, and the fix belonged in the interactive loop rather than the optimizer. In the first real end-to-end run, typing `0.18` at the new target prompt ended the whole session with a traceback:

        Edit candidates? ... / [t]arget-return / [f]inish: t
        New target annual return (current 0.1): 0.18
        ...
          File "/app/agentic_portfolio/src/optimizer/portfolio.py", line 263, in _fit_efficient_frontier
            ef.efficient_return(target_return=float(target_annual_return))
        ValueError: target_return must be lower than the maximum possible return

  The pool's best asset was expected to return 16.35%, so 18% was genuinely impossible and PyPortfolioOpt was right to refuse. But the cost of that refusal was wildly out of proportion: `src/flow/interactive.py`'s `open_pipeline_session` holds live mode's throwaway snapshot open for exactly the lifetime of this loop, so an exception escaping it discards freshly fetched market data and the user's confirmed pool over what is essentially a typo. The loop already had the right idiom for this — "keeping the previous list", "keeping `'GMV'`" — and now applies it to the recompute itself. Verified in the rerun: the same `0.18` prints `Cannot optimize that edit: ...`, reverts, and the session continues to accept `0.15` successfully.

- Discovery (2026-09-05): that same fix exposed an ordering bug that would have been much harder to notice. `_run_edit_loop` persisted the `user_provided` candidate pool to disk *before* recomputing, so once the recompute could fail and be reverted, an add or removal that the optimizer then rejected would have been reverted in memory while remaining saved on disk — the in-memory list and `memory/candidates.json` silently disagreeing for the rest of the session. This is reachable without any target-return edit at all: under `MV`, removing the pool's highest-return ticker can make a previously-reachable target unreachable. The save now happens after a successful recompute, which is what `plans/09_user_provided_selection.md`'s Decision Log already argued for in the analogous empty-pool case ("the save is ordered after the empty-pool guard"). Pinned by `test_run_edit_loop_an_unoptimizable_edit_is_never_persisted`.

- Surprise (2026-09-05): the honest demonstration of `MV` is not the obvious one. Asking for a 10% target and getting a portfolio expected to return 12.59% looks like the target was ignored, but `efficient_return`'s constraint is an inequality (`return >= target`), so a target below what minimum-risk investing already achieves changes nothing — a fact `_validate_efficient_return_result`'s docstring already recorded for the validation logic, but which becomes user-visible for the first time now that both numbers are printed side by side. This shaped the Purpose section: it takes a *raised* target (10% to 15%, moving `AAPL` from 6% to 62% of the portfolio) to show the parameter actually doing something, and the plan now explains the non-binding case explicitly rather than showing an idealized transcript where target and outcome happen to match.

- Observation (2026-09-05): the three objectives' reported Sharpe ratios came out correctly ordered on live data for the same three-ticker pool, which is a free end-to-end sanity check on the new reporting arithmetic. `MV` at a 10% target (effectively the minimum-variance portfolio) reported 0.6873, `MV` at 15% reported 0.6953, and `MSR` reported 0.7241 — the objective that explicitly maximizes Sharpe ratio produced the highest one, as it must, and the two `MV` runs bracket it from below.

- Discovery (2026-09-05): `README.md`'s Live Mode section already specifies a home for these two values — "Users can ask the system to remember risk free rate and/or target return rate. The system will store them in memory/long-term.md" — which is more than this plan builds. The Decision Log records why that persistence is deferred rather than absorbed here; it is noted as a discovery because it means a future memory plan has a defined place to plug into, and the interfaces this plan adds (a defaulted `target_annual_return` parameter threaded from the command line down to the optimizer) are already shaped to be fed from a file instead of a flag without any signature change. **Correction, 2026-09-06:** that last clause proved false for the risk-free rate once the file became per-currency. Resolving a per-currency value needs the currency, and `compute_weights_and_stats`, `stats_for_weights` and `compute_sharpe_ratio` have no currency parameter at all — nor should they acquire one, as `src/optimizer/portfolio.py`'s module docstring is deliberate about. `plans/14_per_currency_risk_free_rate.md` therefore resolves the rate at the CLI edge and keeps threading a plain float, which did leave every one of those signatures untouched, but it changed the two `--risk-free-rate` argparse defaults from `settings.risk_free_rate` to `None` — because "not given" has to be distinguishable from "given 0.02" before a file can supply the difference.


## Decision Log


- Decision: report per-ticker expected return and volatility only for tickers whose weight is above zero, not for every candidate that entered the optimizer.
  Rationale: user's explicit choice when asked. The alternative — reporting every ticker in the returns matrix — would explain *why* a candidate was rejected, which is genuinely useful, but it makes the common case noisier: a 40-candidate agent-driven run would print 40 return/volatility lines to explain a 6-holding portfolio. The chosen set matches the `Weights:` section exactly, so the two sections can be read side by side. Note that the underlying `PortfolioStats` still carries every candidate's figures, so a future change can widen the display without touching the optimizer.
  Date/Author: 2026-09-05, decided during the planning interview.
- Decision: pass this project's `settings.risk_free_rate` into both `EfficientFrontier.max_sharpe()` and `EfficientFrontier.portfolio_performance()` on the CLI's reporting path, accepting that this changes `MSR`'s optimized weights.
  Rationale: user's explicit choice when asked, after the inconsistency was found (see Surprises & Discoveries). `MSR` means maximum Sharpe ratio, and a Sharpe ratio is only defined relative to a risk-free rate; PyPortfolioOpt's own default for that parameter is 0.0, so the objective was being maximized against a different rate than this project documents and uses elsewhere. Reporting a Sharpe ratio computed at 2% next to weights optimized at 0% would have been a new, subtler inconsistency than the one being fixed.
  Date/Author: 2026-09-05, decided during the planning interview.
- Decision: expose `MV`'s target return both as a `--target-return` command-line argument and as a `[t]arget-return` command in the interactive loop, and prompt for it immediately when the user switches the objective to `MV` mid-session.
  Rationale: user's explicit choice when asked. The flag alone would mean a person who realizes mid-session that they want a different target has to quit and re-run, losing the live snapshot and their pool. The interactive command alone would mean the target can never be set non-interactively, which breaks scripted use. The prompt-on-switch exists because switching to `MV` is precisely the moment the target starts to matter, and a user who has never thought about it would otherwise silently inherit the 12% default.
  Date/Author: 2026-09-05, decided during the planning interview.
- Decision: leave `compute_weights`'s signature, return type, and behavior exactly as they are, and add a separate `compute_weights_and_stats` alongside it, with the shared estimation and objective dispatch extracted into one private `_fit_efficient_frontier` helper.
  Rationale: `compute_weights` is called by `src/flow/backtest.py`'s 52-month backtest runner, which needs only the weights and computes its own realized Sharpe ratio from actual monthly returns rather than the optimizer's forward-looking estimates. Changing its return type would have forced edits to that runner and to every existing test in `tests/test_optimizer.py` for no behavioral gain there, and — more importantly — would have silently changed the backtest's `MSR` results, since the new function deliberately fits `max_sharpe` at a nonzero risk-free rate. Keeping two entry points over one shared helper means the estimation logic exists once while the two callers keep their different, deliberate risk-free-rate treatments. The cost is that the interactive path fits the frontier a second time rather than reusing the backtest path's fit; this is negligible here (a matrix of at most 60 months by a few dozen tickers) and is the direct consequence of the two paths legitimately needing different inputs.
  Date/Author: 2026-09-05, decided during design.
- Decision: keep the pipeline result dictionary's `"weights"` key as a plain `dict[str, float]` and add the richer `PortfolioStats` under a new, additive `"stats"` key.
  Rationale: `tests/test_interactive_flow.py` asserts on `result["weights"]` directly in four separate tests, and `"weights"` being a plain mapping of ticker to weight is the obvious meaning of that key for any future reader. Replacing it with a structured object would have broken those tests for a purely presentational reason and made the common case (just read the weights) harder.
  Date/Author: 2026-09-05, decided during design.
- Decision: introduce a single `DEFAULT_TARGET_ANNUAL_RETURN = 0.12` constant in `src/optimizer/portfolio.py` and use it as the default in every function and argument that needs one, rather than repeating the literal `0.12`.
  Rationale: this plan adds a target-return parameter to five functions plus one command-line argument. Repeating the literal in six places is exactly the kind of duplication that drifts — a later change to the project's default would silently apply to some entry points and not others, producing a run whose reported target and actual target disagree. Replacing `compute_weights`'s existing `0.12` literal with the constant is behavior-preserving.
  Date/Author: 2026-09-05, decided during implementation.
- Decision: catch `ValueError` around `_run_edit_loop`'s recompute, revert the edit, and continue the loop — and move the `user_provided` pool's save to after a successful recompute.
  Rationale: forced by a real crash found in end-to-end testing, described in Surprises & Discoveries. The narrow alternative — validating the target against the candidates' maximum expected return before calling the optimizer — was rejected because it duplicates knowledge PyPortfolioOpt already owns (and would still miss `max_sharpe`'s own analogous guard), and because the loop needs to survive an unsolvable state however it arises, not just this one way of arriving at it. Catching `ValueError` specifically, rather than every exception, keeps genuine defects (a missing table, a bad database path) loud. The save reordering is not a separate change but the same fix: once an edit can be rejected after being applied in memory, persisting it before knowing whether it survives is a bug.
  Date/Author: 2026-09-05, decided during implementation after the first real run.
- Decision: let an unreachable `--target-return` on the *initial* command-line run fail with PyPortfolioOpt's own exception rather than adding a handler in `main`.
  Rationale: the interactive loop must survive a bad target because it is holding fetched data and a confirmed pool that would be destroyed with it; a bad command-line argument has no such stake, and PyPortfolioOpt's message ("target_return must be lower than the maximum possible return") already names the problem. There is one rough edge, accepted knowingly: under `--selection user_provided` the pool is confirmed and typed before the optimizer runs, so a bad `--target-return` wastes that typing. It does not lose it — the pool is saved to `--memory-path` at confirmation time, so re-running with a corrected flag starts from the saved pool.
  Date/Author: 2026-09-05, decided during implementation.
- Decision (SUPERSEDED 2026-09-06 by `plans/14_per_currency_risk_free_rate.md`, for the risk-free rate only): do not implement `README.md`'s `memory/long-term.md` persistence of the risk-free rate and target return.
  Superseding note: the risk-free rate IS now persistent, remembered per currency in `memory/rates.json` rather than globally in `memory/long-term.md`. The reason this decision did not survive is that it treated the rate as one global value awaiting a larger memory architecture, and `plans/11_non_us_tickers_and_single_currency.md` then made every portfolio single-currency — at which point a global rate became not merely unpersisted but WRONG, since a risk-free rate is a property of a currency and this plan's own 2% default is a dollar rate. That plan's transcripts already show the workaround, `--risk-free-rate 0.005` typed by hand on every yen run. The target return remains unpersisted and this decision still stands for it.
  Rationale: the request this plan answers was to *report* the risk-free rate and to make `MV`'s target *settable*, not to make either persistent. `README.md`'s Live Mode section does describe remembering both values in `memory/long-term.md`, but that file is one part of a larger memory architecture (`memory/rules.json`, three separately-tracked candidate sets, several `*-summary.md` files) that does not exist and that `plans/08_consistency_review.md` Finding 11 already recommends handling in a dedicated future plan. Building the flags and the interactive prompt now does not conflict with that: a future memory plan can seed this plan's defaults from `memory/long-term.md` without changing any interface added here.
  Date/Author: 2026-09-05, decided during implementation.
- Decision: reversed mid-plan — add a `--risk-free-rate` command-line argument after all, defaulting to `settings.risk_free_rate`, and carry it through `_run_edit_loop` without making it interactively editable.
  Rationale: this plan originally argued the flag was scope creep, on the grounds that the rate was already environment-configurable as `RISK_FREE_RATE` through `src/config/settings.py`'s pydantic-settings singleton and that only reporting had been asked for. The repository owner, while checking this plan's claim about `max_sharpe`, asked specifically about running at `0.015` — which made clear that comparing rates is a real workflow, and that requiring an environment variable for a per-run analytical choice was the wrong ergonomics when `--target-return` had just been added one line away for exactly the same kind of value. The plumbing was already complete (`risk_free_rate` was a parameter on `compute_weights_and_stats`, `compute_weights_and_allocation`, `run_pipeline_against`, and `run_pipeline`), so the change is one argparse entry plus threading. It is deliberately not editable inside the edit loop: unlike `MV`'s target, the risk-free rate is a property of the market environment rather than of the portfolio being designed, so changing it mid-session invites treating it as a tuning knob to make a Sharpe ratio look better. Carrying it through the loop is nevertheless mandatory, because otherwise the flag would govern the initial run and then silently revert to the configured default on the first edit — pinned by `test_run_edit_loop_carries_the_risk_free_rate_into_every_recompute`.
  Date/Author: 2026-09-05, decided at the repository owner's request after the verification described in Revision Note 2.


## Outcomes & Retrospective


The stated purpose is met. Every run now reports each holding's annualized expected return and volatility, the portfolio's own expected return, volatility and Sharpe ratio, and the risk-free rate that Sharpe ratio was measured against; `MV`'s target return is visible on every run, settable with `--target-return`, and changeable mid-session with `[t]`, including a prompt at the moment the objective is switched to `MV`; and the risk-free rate is settable per run with `--risk-free-rate`. All of it was verified against live market data, not only in tests: real runs covering `MV` (binding and non-binding targets, plus an unreachable one), `MSR` at three different risk-free rates, and a backtest-date `GMV`-to-`MV` switch. The suite grew from 188 to 217 tests, all passing, and `data/portfolio.duckdb` was byte-identical after the backtest-date run, so `plans/09_user_provided_selection.md`'s guarantee that this selection never mutates the shared cache still holds.

Two things went better than expected. The `_fit_efficient_frontier` extraction turned out to be genuinely behavior-preserving on the first try, and the evidence is stronger than an assertion: the entire pre-existing suite passed unmodified after the optimizer was rewritten, with the only failures anywhere being the four `tests/test_cli.py` tests whose stub signature the plan had already predicted would need updating. And `test_compute_weights_and_stats_gmv_weights_match_compute_weights` pins the equivalence permanently for the one objective where the two entry points must agree exactly. Separately, the decision to have `PortfolioStats` carry every considered ticker's figures while the CLI narrows to held ones cost nothing and already paid off — the "only positive weights" display choice is now a two-line change to reverse, with no optimizer involvement, if that preference ever changes.

The most valuable outcome was not in the plan at all. Making the target user-settable exposed a crash that ended the entire session, discarding live mode's freshly fetched snapshot, whenever someone asked for a return the candidates could not deliver — a mistake so easy to make that the very first real run hit it. Fixing it uncovered a second, quieter bug in the same code path: the `user_provided` pool was being written to disk before the recompute that might reject the edit, so a rejected add or removal would have left the in-memory list and `memory/candidates.json` disagreeing. Neither was reachable before this plan, and neither would have been found by the test suite as designed, because both required an optimizer failure mid-session — which is exactly what a user-supplied target return makes ordinary. The lesson worth carrying forward is that adding a user-controlled numeric parameter to a long-lived interactive session is not just a plumbing change: it creates new ways for the session's core computation to fail at a moment when the session is holding state that is expensive to rebuild.

One late addition is worth noting as a process point rather than a technical one. This plan originally argued that a `--risk-free-rate` flag was scope creep, since the rate was already environment-configurable and only reporting had been requested. That reasoning did not survive contact with the repository owner, who — while checking this plan's claim about `max_sharpe` — asked about running at `0.015`, revealing that comparing rates per run is a real workflow and that an environment variable is poor ergonomics for a per-run analytical choice sitting one line away from `--target-return`. The flag was added, and the reversal is recorded in the Decision Log rather than quietly rewritten. It also nearly shipped with a subtle bug: threading the rate into the initial pipeline call alone would have let it silently revert to the default on the first interactive edit, which is now pinned by two tests. The general lesson matches the one above — a per-run parameter added to a session that recomputes must be carried by the recompute path, not just the entry point.

What remains, all deliberately out of scope and none of it blocking. Neither the risk-free rate nor the target return is persistent, and `README.md`'s `memory/long-term.md` home for both remains unbuilt, awaiting the broader memory plan `plans/08_consistency_review.md` Finding 11 calls for; the interfaces added here are already shaped to be fed from that file instead of from a flag. **Update, 2026-09-06:** the risk-free rate is now persistent, per currency, in `memory/rates.json` — see `plans/14_per_currency_risk_free_rate.md`, and the correction above on why "without any signature change" was not quite right. The target return is still per-run. Two decisions this plan made were reaffirmed rather than reversed by that work: the rate is still deliberately not editable inside the edit loop, for the reason recorded here (it is a property of the market environment, not of the portfolio being designed), and the ordering rule this plan established for the candidate-pool save — write only after the run has actually produced a report — was applied to the rate as well, so a run that raises out of the optimizer remembers nothing. `src/flow/backtest.py` still fits `MSR` at a 0% risk-free rate, which is correct for reproducing this project's published backtest figures but means the two paths' `MSR` results are not directly comparable — worth stating in any future write-up that puts them side by side. And an unreachable `--target-return` supplied on the command line still exits with PyPortfolioOpt's raw exception rather than a friendly message, which the Decision Log accepts and explains.


## Context and Orientation


This repository builds monthly stock portfolios. A *rebalance date* is the month a portfolio is being built for; `--date today` means live mode, which fetches fresh data, while a date inside the stored 2020-01-01..2024-04-30 window reads a cached DuckDB database at `data/portfolio.duckdb`. The pipeline runs in four stages and this plan changes only the last two.

The first stage decides which tickers are eligible, the *candidate pool*. Either an LLM agent votes on every S&P 500 member (`src/agents/llm_s.py`, `src/agents/llm_f_signals.py`, combined by `src/scanner/candidate_scanner.py`), or, with `--selection user_provided`, the person types their own list. The second stage, in `src/optimizer/portfolio.py`, reads a `returns` table to build a matrix of each candidate's monthly returns (`load_returns_matrix`) and computes portfolio weights from it (`compute_weights`). The third stage turns weights into whole share counts against the latest prices (`load_latest_prices`, `allocate_shares`). The fourth is printing, in `src/flow/cli.py`.

Four terms recur below and each is worth stating plainly, because this plan is entirely about making them visible.

An *expected return* here is a purely historical estimate: the average of a ticker's past monthly returns, scaled up to a yearly figure. *Volatility* is the standard deviation of those same monthly returns, also scaled to a yearly figure — a measure of how much the return bounces around, used as the definition of risk throughout this project. The *risk-free rate* is the return you could get without taking risk at all; this project defaults it to 2% a year (`risk_free_rate` in `src/config/settings.py`) and it exists only to be subtracted from a portfolio's return before that return is compared against its risk. That comparison is the *Sharpe ratio*: portfolio return minus the risk-free rate, divided by portfolio volatility — return per unit of risk, and the single number this project's source paper uses to rank strategies.

The three objectives named by `--objective` are the three ways this project asks for weights. `GMV` (global minimum variance) ignores returns entirely and finds the least volatile combination available. `MSR` (maximum Sharpe ratio) finds the combination with the best return-per-risk. `MV` (mean-variance) is the one that takes a parameter: you name a target annual return and it finds the least volatile combination that achieves at least that return.

All of this is computed by PyPortfolioOpt, a third-party library imported as `pypfopt`, already a declared dependency in `pyproject.toml`. Three of its pieces matter here. `expected_returns.mean_historical_return(matrix, returns_data=True, frequency=12)` produces the per-ticker annualized expected returns described above, where `frequency=12` is what turns monthly figures into annual ones. `risk_models.CovarianceShrinkage(...).ledoit_wolf()` produces the annualized *covariance matrix* — a square table whose diagonal entries are each ticker's own variance (so the square root of the diagonal is each ticker's volatility) and whose off-diagonal entries say how much each pair of tickers moves together. And `EfficientFrontier` is the optimizer object: it is constructed from those two inputs, then told which objective to solve via `min_volatility()`, `max_sharpe(risk_free_rate=...)`, or `efficient_return(target_return=...)`, after which `clean_weights()` returns the solved weights and `portfolio_performance(risk_free_rate=...)` returns the resulting portfolio's `(expected annual return, annual volatility, Sharpe ratio)` as a three-element tuple.

The state before this plan is that `src/optimizer/portfolio.py`'s `compute_weights` computes every one of those quantities and returns only the weights. Its `mu` (per-ticker expected returns) and `cov_matrix` are local variables that go out of scope. Its `MSR` branch calls `max_sharpe()` with no arguments. And `portfolio_performance()` is called in exactly one place — inside the private `_validate_efficient_return_result`, purely to check that an `MV` result actually met its target — where two of its three values are discarded into `_`. Meanwhile `src/flow/cli.py`'s `print_weights_and_allocation` receives only a weights dictionary and an allocation tuple, so it has nothing else it could print.


## Plan of Work


The work is three files of production code plus three test files, in that order, and nothing outside `src/optimizer/portfolio.py`, `src/flow/interactive.py`, and `src/flow/cli.py` needs to change.

In `src/optimizer/portfolio.py`, add the constant `DEFAULT_TARGET_ANNUAL_RETURN = 0.12` beside the existing `VALID_OBJECTIVES` and `MV_RETURN_TOLERANCE`, and use it as the default for `compute_weights`'s existing `target_annual_return` parameter in place of the current `0.12` literal. Extract the body of `compute_weights` — the objective validation, the `mu` and `cov_matrix` estimation, the construction of `EfficientFrontier`, and the three-way `GMV`/`MSR`/`MV` dispatch — into a new private helper `_fit_efficient_frontier(returns_matrix, objective, target_annual_return, risk_free_rate)` that returns the fitted `EfficientFrontier` together with `mu` and `cov_matrix`. The helper takes `risk_free_rate` and passes it to `max_sharpe`; only the `MSR` branch uses it, because `min_volatility` and `efficient_return` have no such parameter. Rewrite `compute_weights` to call the helper with `risk_free_rate=0.0`, which is PyPortfolioOpt's own default for that parameter and therefore leaves this function's behavior bit-for-bit unchanged for its existing callers.

Then add, in the same module, a `PortfolioStats` type (a `typing.NamedTuple`, chosen over a plain dictionary so every field is named and type-checked at the point of construction, and over a dataclass because nothing here needs mutability or methods) carrying the weights, the per-ticker expected returns, the per-ticker volatilities, the portfolio's own expected return, volatility, and Sharpe ratio, and — echoed back for display — the risk-free rate and target annual return that produced them. Add `compute_weights_and_stats(returns_matrix, objective, target_annual_return=DEFAULT_TARGET_ANNUAL_RETURN, risk_free_rate=settings.risk_free_rate)` which calls the same helper with the real risk-free rate, cleans the weights, runs the same `MV` validation `compute_weights` runs, derives per-ticker volatilities as the square root of the covariance matrix's diagonal, calls `portfolio_performance(risk_free_rate=risk_free_rate)`, and returns everything as a `PortfolioStats`. The per-ticker dictionaries deliberately cover every ticker in the returns matrix rather than only the held ones; narrowing to held tickers is a display decision and belongs in the CLI.

In `src/flow/interactive.py`, give `compute_weights_and_allocation` two new trailing parameters, `target_annual_return` and `risk_free_rate`, both defaulted, have it call `compute_weights_and_stats` instead of `compute_weights`, and change the first element of its returned tuple from the weights dictionary to the whole `PortfolioStats`. Its `load_latest_prices` call must then read `stats.weights` rather than the old local. Give `run_pipeline_against` and `run_pipeline` the same two trailing parameters and thread them through. In `run_pipeline_against`'s returned dictionary, keep `"weights"` exactly as it is — a plain ticker-to-weight mapping, now sourced from `stats.weights` — and add one new key, `"stats"`, holding the `PortfolioStats`. Update the module's import line to bring in `compute_weights_and_stats` in place of `compute_weights`, which this module no longer calls.

In `src/flow/cli.py`, add a `--target-return` argument to the parser, defaulted to `DEFAULT_TARGET_ANNUAL_RETURN` and documented as applying only to `--objective MV`, and a `--risk-free-rate` argument defaulted to `settings.risk_free_rate` (which requires importing `settings` into this module for the first time). Pass both to the initial `run_pipeline_against` call and to the `_run_edit_loop` call. Passing the rate to the loop as well as to the initial call is required, not merely tidy: the loop recomputes on its own, so a rate given only to the initial call would govern the first printed result and then silently revert to the configured default on the first edit. Change `print_weights_and_allocation` to take `(stats, allocation, objective)`: it keeps its existing `Weights:` section unchanged, then prints an `Expected return / volatility (annualized):` section listing the same held tickers in the same order, then a single line carrying the portfolio's expected return, volatility, and Sharpe ratio, then the risk-free rate used, then the target annual return — the last of these printed always, showing `n/a (objective is GMV, not MV)` for the two objectives that do not use it, so that the line's presence and position never depend on the objective. The existing `Share allocation:` and `Leftover cash:` output follows unchanged. Update `print_pipeline_result`'s single call site to pass `result["stats"]`, `result["allocation"]`, and `result["objective"]`.

Finally, extend `_run_edit_loop` in the same file. It gains a defaulted `target_annual_return` parameter held as a loop-local variable reassigned in place, exactly as `objective` and `candidates` already are. Its prompt string gains `[t]arget-return`. A new branch handles that choice: when the current objective is not `MV` it prints one explanatory line and loops without recomputing, since there is nothing the value would change; otherwise it prompts for a new value, where blank input keeps the current one and unparseable input keeps the current one with a message, both matching how the existing `[o]bjective` branch handles a rejected edit. Both prompts go through one shared `_prompt_target_return(prompt, current)` helper that returns `current` unchanged for blank or unparseable input, which is also what lets the `[t]` branch skip a recompute that would change nothing. The existing `[o]bjective` branch gains a follow-up prompt: immediately after accepting a switch to `MV`, it asks for the target return, again treating blank input as "keep the current value". The loop's recompute call at the bottom passes the loop-local target through and unpacks a `PortfolioStats`.

That recompute must also be guarded, which is a change this plan discovered rather than anticipated (see Surprises & Discoveries). An edit can be individually valid and still leave the optimizer with nothing to solve — most easily by asking `MV` for a target no combination of the candidates can reach, but also by removing the one high-return ticker that made an existing target reachable. PyPortfolioOpt signals that with `ValueError`, and letting it escape ends the whole command, taking live mode's throwaway snapshot and the user's confirmed pool with it. So capture the pre-edit `objective` and `target_annual_return` alongside the `previous_candidates` the loop already captures, wrap the recompute in `try`/`except ValueError`, and on failure print the optimizer's own message, restore all three values, and continue the loop. Only `ValueError` is caught, so genuine defects such as a missing table still surface. This also forces one reordering: the `user_provided` pool's `save_candidate_pool` call must move to after the successful recompute, because persisting an edit that is about to be reverted would leave the file disagreeing with the in-memory list for the rest of the session.

The tests follow the same three-file shape. `tests/test_optimizer.py` gains coverage that the new function passes the project's risk-free rate to both `max_sharpe` and `portfolio_performance`, that an explicit override is honored, that the per-ticker figures equal independently computed PyPortfolioOpt output for the existing three-ticker fixture, that the portfolio-level triple equals a directly constructed `EfficientFrontier`'s own `portfolio_performance`, and — the regression that matters most — that `compute_weights` still fits `MSR` at a zero risk-free rate, which is what keeps `src/flow/backtest.py`'s results unchanged. `tests/test_interactive_flow.py` gains coverage that the pipeline result now carries a `PortfolioStats` whose weights agree with the long-standing `"weights"` key, and that the target return defaults and overrides flow end to end. `tests/test_cli.py`'s `stub_optimizer` fixture must be updated for the new call signature and return shape, after which its existing tests pass unchanged, and it gains coverage for the `[t]arget-return` branch's three cases and for the switch-to-`MV` prompt.


## Concrete Steps


Run everything from the repository root, `/app/agentic_portfolio`.

To run the tests:

    uv run pytest tests/test_*.py

Expect `217 passed`, up from 188 before this plan. To run only the three files this plan touches:

    uv run pytest tests/test_optimizer.py tests/test_interactive_flow.py tests/test_cli.py

Expect `83 passed`.

To see the feature work against real market data, with a scratch memory file so nothing is left behind:

    uv run portfolio --date today --objective MV --target-return 0.10 --value 100000 \
        --selection user_provided --memory-path /tmp/candidates.json

Type `a`, then `AAPL MSFT SPY`, then `d`. Expect the transcript shown in Purpose / Big Picture above: the new per-ticker `return=`/`volatility=` section, the portfolio return/volatility/Sharpe line, `Risk-free rate used: 0.0200`, and `Target annual return: 0.1000`. The exact numbers will differ from that transcript, since they depend on the day's prices, but the shape and the reported target will not.

Then, in the edit loop, type `t` and `0.15` and expect the reported `Portfolio expected return` to land on `0.1500` with weight shifting toward the highest-return holding. Type `t` and `0.99` and expect `Cannot optimize that edit: target_return must be lower than the maximum possible return` followed by `Keeping the previous candidates, objective, and target return.`, with the session still running. Type `o` and `MSR` and expect `Target annual return: n/a (objective is MSR, not MV)` and a Sharpe ratio at least as high as any the `MV` runs reported. Type `f` to finish.

Note that these commands are piped-input friendly, which is how the transcripts below were captured — for example:

    printf 'a\nAAPL MSFT SPY\nd\nt\n0.15\nf\n' | uv run portfolio --date today \
        --objective MV --target-return 0.10 --value 100000 \
        --selection user_provided --memory-path /tmp/candidates.json

When input is piped rather than typed, the prompts are not echoed, so a prompt and the following output appear on the same line; this is a display artifact of piping, not of the code.

To see the objective-switch prompt, run with `--objective GMV` and, in the edit loop, type `o`, then `MV`, and observe that it immediately asks `Target annual return for MV (default 0.12):` before recomputing.

To see the risk-free rate change the answer rather than only the report, run the same `MSR` pool at several rates and compare:

    for RFR in "" "--risk-free-rate 0.015" "--risk-free-rate 0.06"; do
        printf 'a\nAAPL MSFT SPY\nd\nf\n' | uv run portfolio --date today --objective MSR \
            --value 100000 --selection user_provided --memory-path /tmp/rfr.json $RFR
    done

Expect `Risk-free rate used:` to echo each rate, and expect the weights to tilt toward the highest-expected-return holding as the rate rises — the transcript is in Artifacts and Notes.


## Validation and Acceptance


Acceptance is behavioral, in six parts, each verified for real and recorded in Artifacts and Notes below.

Running any objective prints, between the existing `Weights:` and `Share allocation:` sections, one `return=`/`volatility=` line for each held ticker and no line for a candidate that received zero weight; a portfolio line carrying expected return, volatility, and Sharpe ratio; and a `Risk-free rate used: 0.0200` line matching `src/config/settings.py`'s `risk_free_rate`. Running `--objective MV --target-return 0.10` reports `Target annual return: 0.1000`, and raising that target interactively to a value above the minimum-variance portfolio's own return makes `Portfolio expected return` land exactly on it — the non-binding case, where a low target correctly leaves the answer unchanged, is explained in Purpose / Big Picture and is not a defect. Running `--objective GMV` or `--objective MSR` reports `Target annual return: n/a (objective is GMV, not MV)` rather than omitting the line. In the interactive loop, `t` on an `MV` session changes the target and visibly moves both the weights and the reported portfolio return, while `t` on a `GMV` session prints an explanation and changes nothing. Switching the objective to `MV` with `o` prompts for the target return in the same step. And asking for a target the candidates cannot reach prints `Cannot optimize that edit: ...`, reverts, and leaves the session running rather than ending the command.

Two further guarantees are verified because they are the ones a regression would be most costly in. A backtest-window date (for instance `--date 2024-03-01`) still reports `Mode: backtest` and leaves `data/portfolio.duckdb` byte-identical, checked with `md5sum` before and after — the promise `plans/09_user_provided_selection.md` made about this selection never mutating the shared cache. And `src/flow/backtest.py` is untouched, so its `MSR` results are unchanged.

The `--risk-free-rate` flag is accepted separately: running `--objective MSR --risk-free-rate 0.015` reports `Risk-free rate used: 0.0150`, produces different weights from the same run at the default 0.02, and — the part a regression would most likely break — keeps that rate after an interactive edit rather than reverting to the configured default.

For the automated suite, `uv run pytest tests/test_*.py` reports 217 passed, up from 188, of which 29 are new: 10 in `tests/test_optimizer.py`, 3 in `tests/test_interactive_flow.py`, and 16 in `tests/test_cli.py`. Every test that existed before this plan still passes, and only one existing test construct changes: `tests/test_cli.py`'s `stub_optimizer` fixture, whose stub must match the new call signature and return a `PortfolioStats`. Per `AGENTS.md`, no test added here calls Yahoo Finance or an LLM; the optimizer tests run PyPortfolioOpt for real against the hand-built in-memory fixture that `tests/test_optimizer.py` already defines, and the CLI tests monkeypatch both the optimizer call and the printing.

Five of the added tests pin this plan's actual claims rather than merely exercising its code. `test_compute_weights_and_stats_msr_fits_at_the_configured_risk_free_rate` asserts with a spy that the project's 2% rate genuinely reaches PyPortfolioOpt's `max_sharpe`, which is the inconsistency this plan set out to fix, and `test_compute_weights_msr_still_fits_at_zero_risk_free_rate` asserts the opposite for the untouched `compute_weights`, which is what proves `src/flow/backtest.py`'s existing results are unaffected. `test_compute_weights_and_stats_per_ticker_figures_match_pypfopt_estimates` recomputes `mean_historical_return` and the covariance diagonal independently and compares, so the reported figures are verified against their definition rather than against themselves, and `test_compute_weights_and_stats_sharpe_is_measured_against_the_risk_free_rate` does the same for the Sharpe ratio, confirming the four numbers printed together actually describe one another. Finally `test_run_edit_loop_an_unoptimizable_edit_is_reverted_instead_of_ending_the_session` scripts a refused target followed by an unrelated edit and asserts the second edit's recompute received the reverted value, which pins the revert itself rather than merely the error message.


## Idempotence and Recovery


Every step here is safe to repeat, and this plan adds no persistent state of any kind: no new file is written, no database table is created or altered, and no schema changes. The `--target-return` value and any interactive edit of it live only for the duration of one command. Re-running any command in Concrete Steps simply recomputes from the same inputs, and a live-mode run's throwaway snapshot is deleted on exit by the existing `finally` clause in `src/flow/live.py` exactly as before.

The one behavior change that is not confined to display is `MSR`'s optimized weights on the CLI path, which now maximize the Sharpe ratio against a 2% risk-free rate instead of 0%. This is intentional and is recorded in the Decision Log. It is also reversible without code changes: setting `RISK_FREE_RATE=0` in the environment (or in `.env`) restores the previous numerical behavior exactly, since `src/config/settings.py` reads that variable into `settings.risk_free_rate`, which is what the new code passes through. `src/flow/backtest.py`'s results are unaffected either way, because it calls the unchanged `compute_weights`.


## Artifacts and Notes


The live `MSR` run, showing the `n/a` target line and the highest Sharpe ratio of the three objectives — reached by typing `o` then `MSR` in the same session as the Purpose transcript, so the candidates and their per-ticker figures are identical to it:

    Candidates (3): AAPL, MSFT, SPY

    Weights:
      SPY: 0.5556
      AAPL: 0.3313
      MSFT: 0.1131

    Expected return / volatility (annualized):
      SPY: return=0.1225  volatility=0.1553
      AAPL: return=0.1635  volatility=0.2301
      MSFT: return=0.1351  volatility=0.2505

    Portfolio expected return: 0.1375  Portfolio volatility: 0.1623  Portfolio Sharpe: 0.7241
    Risk-free rate used: 0.0200
    Target annual return: n/a (objective is MSR, not MV)

    Share allocation:
      AAPL: 103
      MSFT: 23
      SPY: 72
    Leftover cash: $96.31

The backtest-date run, exercising the `GMV` `n/a` line, the switch-to-`MV` prompt, a blank target-return edit, and the shared-cache guarantee. Note `NVDA` is a candidate but receives no weight, so it appears in `Candidates` and in neither the `Weights` nor the per-ticker figures — the "held tickers only" display decision working:

    $ md5sum data/portfolio.duckdb
    d75c6f24dd3f9a74263e0d15bd6abd6c  data/portfolio.duckdb

    Current candidate pool (0): (empty)
    Edit candidate pool? ... : a
    Ticker(s) to add (space-separated): MSFT NVDA
    Added: MSFT, NVDA.
    Candidate pool (2): MSFT, NVDA
    Edit candidate pool? ... : d
    Saved 2 ticker(s) to /tmp/bt10.json.
    Mode: backtest  Rebalance date: 2024-03-01  Objective: GMV  Selection: user_provided

    Candidates (2): MSFT, NVDA

    Weights:
      MSFT: 1.0000

    Expected return / volatility (annualized):
      MSFT: return=0.3503  volatility=0.2267

    Portfolio expected return: 0.3503  Portfolio volatility: 0.2267  Portfolio Sharpe: 1.4571
    Risk-free rate used: 0.0200
    Target annual return: n/a (objective is GMV, not MV)

    Share allocation:
      MSFT: 122
    Leftover cash: $276.78

    Edit candidates? ... / [t]arget-return / [f]inish: o
    New objective (GMV/MV/MSR): MV
    Target annual return for MV (default 0.12): 0.14

    Candidates (2): MSFT, NVDA
    ...
    Target annual return: 0.1400

    Edit candidates? ... / [t]arget-return / [f]inish: t
    New target annual return (current 0.14):
    Edit candidates? ... / [t]arget-return / [f]inish: f

    $ md5sum data/portfolio.duckdb
    d75c6f24dd3f9a74263e0d15bd6abd6c  data/portfolio.duckdb

Two things in that transcript are worth naming. The blank response to the target-return prompt produced no recompute at all — the next line is the edit prompt again — which is the "nothing changed, so don't recompute" behavior `_prompt_target_return` exists to enable. And switching to `MV` with a 0.14 target left the weights identical to `GMV`'s, because with `MSFT` expected to return 35% a year the 14% target is non-binding; this is the same inequality-constraint behavior explained in Purpose / Big Picture, here on historical data.

The test suite before and after, showing that the optimizer refactor broke nothing beyond the one fixture whose shape the plan predicted would change:

    $ uv run pytest tests/test_*.py -q      # after the src/ changes, before the test updates
    4 failed, 184 passed in 33.13s
    FAILED tests/test_cli.py::test_run_edit_loop_user_provided_persists_each_accepted_edit
    FAILED tests/test_cli.py::test_run_edit_loop_user_provided_validates_added_tickers
    FAILED tests/test_cli.py::test_run_edit_loop_non_user_provided_never_touches_candidate_memory
    FAILED tests/test_cli.py::test_run_edit_loop_keeps_previous_list_when_an_edit_would_empty_it

    $ uv run pytest tests/test_*.py -q      # final
    217 passed in 37.82s

All four failures were the single `stub_optimizer` fixture's stub signature (`TypeError: ... got an unexpected keyword argument 'target_annual_return'`), not four independent problems. Notably every test in `tests/test_optimizer.py` and `tests/test_interactive_flow.py` passed at that intermediate point, which is the evidence that extracting `_fit_efficient_frontier` and rewriting `compute_weights` around it changed no arithmetic.

The `--risk-free-rate` flag on live data, same `MSR` pool at three rates, abridged to the lines that differ:

    ########## (default 0.02) ##########
      SPY: 0.5556
      AAPL: 0.3313
      MSFT: 0.1131
    Portfolio expected return: 0.1375  Portfolio volatility: 0.1623  Portfolio Sharpe: 0.7241
    Risk-free rate used: 0.0200
    ########## --risk-free-rate 0.015 ##########
      SPY: 0.5696
      AAPL: 0.3191
      MSFT: 0.1113
    Portfolio expected return: 0.1370  Portfolio volatility: 0.1616  Portfolio Sharpe: 0.7549
    Risk-free rate used: 0.0150
    ########## --risk-free-rate 0.06 ##########
      AAPL: 0.4958
      SPY: 0.3669
      MSFT: 0.1373
    Portfolio expected return: 0.1445  Portfolio volatility: 0.1745  Portfolio Sharpe: 0.4845
    Risk-free rate used: 0.0600

This is the clearest available demonstration that the risk-free rate was a real input to `MSR` and not merely a reporting detail. As the rate rises, `AAPL` — the highest-expected-return holding — goes from 32% to 33% to 50% of the portfolio, because a higher risk-free hurdle makes return above that hurdle worth more risk; and the reported Sharpe ratio falls monotonically (0.7549, 0.7241, 0.4845) because a larger rate is being subtracted from a barely-changed portfolio return. Before this plan every one of these runs would have optimized identically, against an implicit 0%, while reporting nothing about it.


## Interfaces and Dependencies


No new third-party dependencies. Everything here uses `pypfopt`, `pandas`, and `numpy`, all already declared in `pyproject.toml` and already imported by the modules being changed, plus the standard library's `typing.NamedTuple`.

In `src/optimizer/portfolio.py`, define:

    DEFAULT_TARGET_ANNUAL_RETURN = 0.12

    class PortfolioStats(NamedTuple):
        weights: dict[str, float]
        expected_returns: dict[str, float]
        volatility: dict[str, float]
        portfolio_expected_return: float
        portfolio_volatility: float
        portfolio_sharpe: float
        risk_free_rate: float
        target_annual_return: float

    def _fit_efficient_frontier(
        returns_matrix: pd.DataFrame,
        objective: str,
        target_annual_return: float,
        risk_free_rate: float,
    ) -> tuple[EfficientFrontier, pd.Series, pd.DataFrame]

    def compute_weights_and_stats(
        returns_matrix: pd.DataFrame,
        objective: str,
        target_annual_return: float = DEFAULT_TARGET_ANNUAL_RETURN,
        risk_free_rate: float = settings.risk_free_rate,
    ) -> PortfolioStats

`compute_weights`'s signature is unchanged except that its `target_annual_return` default is now spelled `DEFAULT_TARGET_ANNUAL_RETURN` rather than `0.12`.

In `src/flow/interactive.py`, `compute_weights_and_allocation` becomes:

    def compute_weights_and_allocation(
        candidates: list[str],
        objective: str,
        portfolio_value: float,
        rebalance_date: date,
        db_path: str,
        target_annual_return: float = DEFAULT_TARGET_ANNUAL_RETURN,
        risk_free_rate: float = settings.risk_free_rate,
    ) -> tuple[PortfolioStats, tuple[dict[str, int], float]]

and `run_pipeline_against` and `run_pipeline` each gain the same two trailing keyword parameters with the same defaults. `run_pipeline_against`'s result dictionary gains one key, `"stats"`, holding a `PortfolioStats`; its `"weights"` key keeps its existing `dict[str, float]` type and meaning.

In `src/flow/cli.py`, `print_weights_and_allocation` becomes:

    def print_weights_and_allocation(
        stats: PortfolioStats,
        allocation: tuple[dict[str, int], float],
        objective: str,
    ) -> None

and `_run_edit_loop` gains `target_annual_return: float = DEFAULT_TARGET_ANNUAL_RETURN` and `risk_free_rate: float = settings.risk_free_rate` as trailing keyword parameters. The same module also gains:

    def _prompt_target_return(prompt: str, current: float) -> float

which returns `current` unchanged for blank or unparseable input and is shared by the `[t]arget-return` branch and the switch-to-`MV` follow-up prompt. The parser gains `--target-return`, a `float` defaulting to `DEFAULT_TARGET_ANNUAL_RETURN` and reachable as `args.target_return`, and `--risk-free-rate`, a `float` defaulting to `settings.risk_free_rate` and reachable as `args.risk_free_rate`. `src/flow/cli.py` therefore also imports `settings` from `src.config.settings`, which it did not previously need.

The command-line surface after this plan is:

    uv run portfolio --date YYYY-MM-DD|today --objective GMV|MV|MSR --value FLOAT
        [--target-return FLOAT] [--risk-free-rate FLOAT]
        [--selection llm_s_only|llm_f_only|llm_s_and_f|user_provided]
        [--db-path PATH] [--memory-path PATH]

`--risk-free-rate` defaults to `settings.risk_free_rate`, which remains configurable per environment as `RISK_FREE_RATE`, either exported in the shell or set in `.env`; the flag overrides it for one run. Both spellings reach the same place, so `RISK_FREE_RATE=0.015 uv run portfolio ...` and `uv run portfolio --risk-free-rate 0.015 ...` are equivalent.


## Revision Note 1 (2026-09-05)


Changed, during implementation and after the first real end-to-end run: `_run_edit_loop`'s recompute is now wrapped in `try`/`except ValueError`, which reverts the edit and continues the loop, and the `user_provided` selection's `save_candidate_pool` call moved to after that recompute succeeds. The `Plan of Work`, `Progress`, `Surprises & Discoveries`, `Decision Log`, `Outcomes & Retrospective`, `Validation and Acceptance`, and `Concrete Steps` sections were all updated to reflect this, and two tests were added to pin it.

Why: the feature this plan set out to build made an existing latent crash trivially reachable. Asking `MV` for a target return higher than any combination of the candidates can achieve makes PyPortfolioOpt raise `ValueError`, and before this fix that exception escaped the interactive loop and ended the whole command — discarding live mode's freshly fetched snapshot and the user's just-confirmed candidate pool over what amounts to a typo. The very first real run hit it, which is recorded verbatim in `Surprises & Discoveries`. Fixing it exposed a second bug in the same code path: the pool was persisted before the recompute that might reject the edit, so a rejected add or removal would have left `memory/candidates.json` disagreeing with the in-memory list.

Also changed: the transcripts in `Purpose / Big Picture` were replaced with the real captured output. The originals were written before implementation and showed `Portfolio expected return` equal to the requested `Target annual return`, which is only true when the target binds. The real run's 10% target did not bind — the minimum-variance portfolio already returned 12.59% — so the plan now shows that case honestly, explains why it is correct rather than a defect, and uses a *raised* target (to 15%) to demonstrate the parameter actually changing the answer. The stated test count also moved from an estimated 205 to the actual 214.


## Revision Note 2 (2026-09-05)


Changed, after this plan was otherwise complete: a `--risk-free-rate` command-line argument was added, defaulting to `settings.risk_free_rate` and threaded into both the initial pipeline call and `_run_edit_loop`. This reverses this plan's original decision not to add such a flag; the `Decision Log` entry has been rewritten to record both the original reasoning and the reversal, and `Progress`, `Concrete Steps`, `Validation and Acceptance`, `Artifacts and Notes`, and `Interfaces and Dependencies` were updated. Three tests were added (16 total in `tests/test_cli.py`, 29 across the plan), and the suite moved from 214 to 217 passing.

Why: the repository owner questioned this plan's claim about `max_sharpe` and the risk-free rate, asking specifically whether a value like `0.015` could be passed. Verifying it confirmed the plan's account was correct — PyPortfolioOpt's signature is `max_sharpe(self, risk_free_rate=0.0)`, the old code called it with no argument and so took the `0.0` default, and the parameter is entirely passable — but the question itself revealed that comparing rates per run is a real workflow. Requiring an environment variable for that, when `--target-return` had just been added one line away for exactly the same kind of per-run analytical value, was the wrong ergonomics. The flag is deliberately not editable inside the interactive loop, for the reason given in the Decision Log: unlike `MV`'s target, the risk-free rate describes the market environment rather than the portfolio being designed.

One genuine bug was avoided in the process, and is worth recording because it would have been easy to ship. Adding the argparse entry alone is not sufficient: `_run_edit_loop` performs its own `compute_weights_and_allocation` calls, so a rate threaded only into the initial `run_pipeline_against` call would have governed the first printed result and then silently reverted to the configured default on the very first interactive edit — the kind of inconsistency that is invisible unless the two outputs are compared closely. `test_run_edit_loop_carries_the_risk_free_rate_into_every_recompute` and the edit-loop assertion in `test_main_threads_the_risk_free_rate_argument_into_the_pipeline_and_the_edit_loop` exist to pin exactly that.


## Revision Note: an unreachable target return no longer propagates (2026-09-08)

This plan decided that an unreachable `--target-return` should propagate out of `main`,
on the grounds that only the optimizer can discover it and that a `parser.error` would
therefore be the wrong shape. The first half was right and the second did not follow: "not
knowable at parse time" does not imply "deliver it as a traceback".

Two things were wrong in practice. PyPortfolioOpt's message - "target_return must be lower
than the maximum possible return" - names neither the flag responsible nor a value that would
work, so the person is told they asked for something impossible but not what to ask instead.
And the same condition was already handled gracefully inside the interactive edit loop, so
the behavior depended on whether the target was typed on the command line or at a prompt.

`--target-return` above the pool's ceiling now raises
`UnreachableTargetReturnError`, one of the `UnsatisfiableRequestError` family in
`src/errors.py`, with a message naming the flag, the attainable ceiling and the ticker that
sets it. `src/flow/cli.py` prints it and enters the edit loop rather than raising, so the
number can be corrected without rebuilding a live snapshot. See
`plans/15_minimum_expected_dividend.md`'s revision note of the same date, which copied this
plan's decision and prompted the correction of both.
