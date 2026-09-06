# Remember a risk-free rate per currency, shared by both entry points


This ExecPlan is a living document. The sections `Progress`, `Surprises & Discoveries`, `Decision Log`, and `Outcomes & Retrospective` must be kept up to date as work proceeds. This plan must be maintained in accordance with `PLANS.md` at the repository root. This plan builds on `plans/10_performance_reporting_and_target_return.md` (which added `--risk-free-rate` and the `Risk-free rate used:` report line, and whose Decision Log this plan deliberately reverses), `plans/11_non_us_tickers_and_single_currency.md` (per-currency pools and the single-currency rule, and the source of this plan's motivating evidence), `plans/12_benchmark_per_candidate_pool.md` (`resolve_benchmark_ticker`'s precedence and `_settle_benchmark`'s persist-only-a-validated-choice rule, both of which this plan mirrors), and `plans/13_user_portfolio.md` (`memory/portfolio.json`, `uv run portfolio-holdings`, and the holdings report block) — all four checked into this repository.


## Purpose / Big Picture


Every Sharpe ratio this project prints is measured against one global number, `settings.risk_free_rate`, whose default is 2%. That is a **dollar** rate. A Sharpe ratio is `(annual return - risk-free rate) / annual volatility` — return earned per unit of risk *above what a riskless asset would have paid* — so measuring a yen portfolio against a dollar rate subtracts the wrong thing and reports a number that means nothing in particular.

Before this change, `uv run portfolio-holdings` reported a JPY portfolio and a USD portfolio against 2% alike, in the same breath:

    $ uv run portfolio-holdings show

    Your portfolio (JPY), from memory/portfolio.json:
      ...
    Annual return: 0.2159  Annual volatility: 0.1770  Sharpe: 1.1065
    Risk-free rate used: 0.0200          <-- a dollar rate applied to a yen portfolio

    Your portfolio (USD), from memory/portfolio.json:
      ...
    Risk-free rate used: 0.0200

The workaround existed but was manual and easy to forget. `plans/11_non_us_tickers_and_single_currency.md` records it twice, passing `--risk-free-rate 0.005` on every yen run with the note "the 2% default is a dollar rate". Worse, the two commands did not agree with each other: `uv run portfolio` and `uv run portfolio-holdings` each had their own `--risk-free-rate`, so unless you typed the same value into both, the pool's figures, the benchmark's and your own holdings' — three blocks printed one under another in a single report, specifically so they could be compared — were measured on different scales.

After this change each currency has its own remembered rate, stored in a new `memory/rates.json` and read by both commands. You state it once:

    $ uv run portfolio-holdings --currency JPY --risk-free-rate 0.005 show
    Remembered 0.0050 as the JPY risk-free rate in memory/rates.json.

    Your portfolio (JPY), from memory/portfolio.json:
      ...
    Risk-free rate used: 0.0050 (--risk-free-rate, remembered for JPY)

and thereafter every run in that currency, from either command, uses it:

    $ uv run portfolio-holdings show

    Your portfolio (JPY), from memory/portfolio.json:
      ...
    Risk-free rate used: 0.0050 (remembered for JPY)

    Your portfolio (USD), from memory/portfolio.json:
      ...
    Risk-free rate used: 0.0425 (remembered for USD)

Two currencies, two rates, one command, no flags. That is the whole feature.

Note the parenthetical. Every printed rate now says which of four sources it came from, because a bare `0.0200` is exactly what the old bug looked like and a reader had no way to tell a deliberate choice from an inherited default.

Terms used below, in plain language:

- The **risk-free rate** is the annual return a riskless asset pays — in practice a short-dated government bill in that currency. It is a property of a *currency*, not of a portfolio, which is why one number cannot serve a dollar portfolio and a yen portfolio at once.
- **Annualized**, throughout this project, means expressed as a per-year figure. Rates here are written as decimals, so 4.25% is `0.0425`.
- A **currency's pool / portfolio** is this project's existing rule that any one portfolio holds exactly one currency (see `src/dataset/ticker_currency.py`); conversion between currencies is deliberately not implemented. That rule is what makes "a rate per currency" the natural granularity: every report already has exactly one currency in scope.
- **Precedence** here means which of several possible sources of a value wins. This plan's is four deep, described below.


## Progress


- [x] (2026-09-06 05:40Z) Mapped every read of `settings.risk_free_rate` in `src/` (eleven, all of them default arguments or argparse defaults, none inside a function body) and every place a rate reaches a calculation.
- [x] (2026-09-06 05:50Z) Settled the three open questions with the repository owner: `--risk-free-rate` remembers the value; risk-free rate only, not the target return; backtests untouched.
- [x] (2026-09-06 06:05Z) Had the design adversarially reviewed, which changed three things before any code was written — see `Decision Log` entries on validation timing, persistence ordering, and out-of-range refusal.
- [x] (2026-09-06 06:10Z) Wrote this ExecPlan.
- [x] (2026-09-06 06:22Z) Milestone 1 — the store: `src/flow/rate_memory.py` and `tests/test_rate_memory.py` (37 tests).
- [x] (2026-09-06 06:31Z) Milestone 2 — `uv run portfolio-holdings`: `_resolve_rate` per reported currency, `_remember_rate` and `_guard_rate_is_rememberable` in `main`, `--rates-path`, and `format_risk_free_rate` shared by both report blocks. `tests/test_holdings_cli.py` 29 → 46 tests.
- [x] (2026-09-06 06:38Z) Milestone 3 — `uv run portfolio`: validation via `parser.error` right after `parse_args`, `_settle_risk_free_rate` after the currency settles, `_remember_risk_free_rate` after the report, and the origin threaded through `print_pipeline_result`, `print_user_portfolio` and `_run_edit_loop`. `tests/test_cli.py` 78 → 91 tests.
- [x] (2026-09-06 06:45Z) Milestone 4 — documentation: two `README.md` edits and revision notes on plans 08, 10, 11, 12 and 13. Full suite: 497 passed, `tests/test_optimizer.py` unmodified.


## Surprises & Discoveries


- Observation: the design review predicted that "a missed wiring site fails silently, and nothing catches it" — and then it happened anyway, to me, in this very plan. The origin string was threaded into `print_pipeline_result` and `_run_edit_loop` but NOT into `cli.main`'s `print_user_portfolio` call, so the holdings block printed a bare `Risk-free rate used: 0.0050` while the pool block two lines above it correctly read `0.0050 (remembered for JPY)`. Nothing failed, because `risk_free_rate_origin` defaults to `None` and `None` prints nothing. It was caught by reading a live run's output, not by the 490-odd tests. Now pinned by `test_main_names_the_rates_source_in_the_holdings_block_too`.
  Evidence: the live JPY pipeline run of 2026-09-06 printed `Risk-free rate used: 0.0050 (remembered for JPY)` for the pool and `Risk-free rate used: 0.0050` for the holdings.
  Lesson: a defaulted optional parameter is exactly the shape that makes an omission invisible. The review's own suggested remedy — make the report print an origin unconditionally so an unprovenanced number is impossible to produce — is the stronger design, and the reason it was not adopted is that the default keeps twenty-odd existing direct calls to the two printers working unchanged. The compromise accepted here is that every *real* reporting path is now covered by a test asserting the origin arrived.

- Observation: `json.JSONDecodeError` is a subclass of `ValueError`, so adding it as a SECOND `except` clause after `except ValueError` in `holdings_cli.main` did nothing at all — the broader clause swallowed it and reported a bare "Expecting property name enclosed in double quotes: line 1 column 2", with no hint that a memory file was the culprit. The handler order had to be inverted.
  Evidence: `test_a_malformed_rates_file_exits_two_rather_than_printing_a_traceback` failed on exactly that assertion before the clauses were swapped, which is the only reason it was noticed.

- Observation: every read of `settings.risk_free_rate` in `src/` turned out to be a default argument or an argparse default — eleven of them, not one inside a function body. That is what made resolving at the CLI edge cheap rather than invasive: all eleven already behave as fallbacks, so none had to change, and no optimizer signature gained a `currency` parameter it had no business having. It also means the two argparse defaults were the ONLY places that had to move, since a default bound at parse time cannot express "not given".
  Evidence: `tests/test_optimizer.py` passes unmodified; the full suite went 430 → 497 tests with no edits to anything under `src/optimizer/`.

- Observation: a remembered rate visibly moves the numbers, which is the proof the bug was real. The same JPY portfolio reported `Sharpe: 1.1065` against the 2% dollar default and `Sharpe: 1.1913` against a 0.5% yen rate; the USD portfolio went from `0.6919` to `0.5400` once a realistic 4.25% dollar rate was remembered. The second of those is a portfolio that looked meaningfully better than it was.
  Evidence: the live `portfolio-holdings show` runs of 2026-09-06, before and after remembering rates.

- Observation: refusing a malformed `--risk-free-rate` through `parser.error` rather than letting `ValueError` propagate was a late change, and it is consistent with `plans/10_performance_reporting_and_target_return.md` rather than a departure from it. That plan decided a bad command-line argument may propagate raw — but its example was an UNREACHABLE `--target-return`, which only the optimizer can discover. A malformed value is what argparse itself already reports as an exit-2 error (`--value abc`), so matching that is the consistent choice, and a traceback for a typo would be a worse answer to the same class of mistake.
  Evidence: `uv run portfolio ... --risk-free-rate nan` now prints `portfolio: error: --risk-free-rate must be a finite number, got nan`.


## Decision Log


- Decision: `--risk-free-rate X` REMEMBERS X for the run's currency; there is no separate `rate` subcommand.
  Rationale: chosen by the repository owner from three offered options. It mirrors `--benchmark`, which `plans/12_benchmark_per_candidate_pool.md` already made sticky per pool, so there is one idiom to learn rather than two. The cost is that a comparison run leaves its rate standing — see `Limitations`.
  Date/Author: 2026-09-06, agreed with the repository owner.

- Decision: the risk-free rate only. The target return stays a per-run flag.
  Rationale: chosen by the repository owner. `README.md`'s Live Mode section promises to remember "risk free rate and/or target return rate" together, so this fulfils half of that promise; the file's shape leaves room for the other half without a migration.
  Date/Author: 2026-09-06, agreed with the repository owner.

- Decision: `uv run portfolio-backtest` and `src/flow/backtest.py`'s `compute_sharpe_ratio` are untouched, keeping `settings.risk_free_rate`.
  Rationale: chosen by the repository owner. That path produces the figure this project compares against the paper's published 0.6324 S&P 500 baseline; it is USD- and S&P-500-only by construction; and `src/optimizer/portfolio.py`'s `compute_weights` docstring already warns that changing the rate it fits at "would silently move every published backtest figure". A per-currency lookup has nothing to offer a single-currency historical benchmark and everything to lose.
  Date/Author: 2026-09-06, agreed with the repository owner.

- Decision: resolution happens ONLY at the CLI edge, in `cli.main` and `holdings_cli.main`; everything below keeps receiving a plain `float`.
  Rationale: all eleven reads of `settings.risk_free_rate` are default arguments or argparse defaults, so they already function as fallbacks and need no change. Pushing the lookup downward would be far worse: `compute_weights_and_stats`, `stats_for_weights` and `compute_sharpe_ratio` have no `currency` parameter at all, and `src/optimizer/portfolio.py`'s module docstring is deliberate about not acquiring dependencies of that kind. Resolving once at the edge also preserves the guarantee that matters most here — the pool's figures, the benchmark's and the holdings' all receive the SAME float, so the three report blocks stay on one scale.
  Date/Author: 2026-09-06.

- Decision: precedence is `--risk-free-rate` → `memory/rates.json` for this currency → `RISK_FREE_RATE` (environment or `.env`, via `settings`) → the built-in `0.02`.
  Rationale: the first two mirror `resolve_benchmark_ticker`'s override-then-saved shape exactly. The third and fourth are what `settings.risk_free_rate` already means. A stored rate outranking the environment variable is the deliberate part: a per-currency entry is the *more specific* statement, and a single global environment variable cannot express "0.5% for yen, 4.25% for dollars" at all. `README.md`'s `RISK_FREE_RATE` entry must say so, or someone who sets it and sees a different number has no way to find out why.
  Date/Author: 2026-09-06.

- Decision: validate the flag's value immediately after `parse_args()`, but resolve (flag versus stored) only after the run's currency has settled.
  Rationale: these are two different moments and the first draft of this design collapsed them, which the review caught. Resolution genuinely needs the currency, which in `cli.main` is not known until the `user_provided` confirm loop returns. Refusal does not — and refusing late would mean a bad number is reported only after `open_pipeline_session` has built a live snapshot (a real Wikipedia, yfinance and SEC EDGAR fetch) and, for `user_provided`, after every saved ticker has been re-validated and re-ingested and the whole interactive confirm loop has been typed through. Note `argparse`'s `type=float` accepts `nan` and `inf` today, so before this change `--risk-free-rate nan` printed `Sharpe: nan` without complaint.
  Date/Author: 2026-09-06, corrected during design review.

- Decision: persist only AFTER the run has actually produced a report.
  Rationale: the review caught this too, and the precedent is explicit in two places. `plans/10_performance_reporting_and_target_return.md` records fixing exactly this ordering for the candidate pool — the save "now happens after a successful recompute" — pinned by `test_run_edit_loop_an_unoptimizable_edit_is_never_persisted`. And `_settle_benchmark` writes a benchmark only when the choice validated (`... and source.unavailable_reason is None`). Without the same gate, `uv run portfolio --objective MSR --risk-free-rate 0.05` against a pool where no ticker's expected return clears 5% would write `0.05` to disk and *then* raise out of PyPortfolioOpt's `max_sharpe`, leaving standing state behind from a run that printed nothing.
  Date/Author: 2026-09-06, corrected during design review.

- Decision: a rate whose magnitude is 1 or more is REFUSED by name, not merely cautioned about.
  Rationale: the first draft printed a caution and carried on, and the review showed how that composes into a trap. `--risk-free-rate 4.5`, meaning 4.5%, would warn once — on a line the user may not read — and 4.5 would then be the permanent USD rate. Every later `--objective MSR` run would raise `ValueError: at least one of the assets must have an expected return exceeding the risk-free rate` from inside PyPortfolioOpt, in a message naming neither the rate nor the file it came from, on runs where the user typed nothing at all; GMV and MV would keep working and report a Sharpe ratio around -29. Refusing at the door removes the trap rather than documenting it, and the refusal message can say the thing the user needs to hear: rates are decimals, so 4.5% is `0.045`.
  Date/Author: 2026-09-06, corrected during design review.

- Decision: NEGATIVE rates are accepted.
  Rationale: Japan ran a negative policy rate for years, and this feature exists precisely to stop measuring a yen portfolio against a dollar assumption. Refusing negatives would reintroduce the bug for the one currency that motivated the change.
  Date/Author: 2026-09-06.

- Decision: every write is announced on stdout, naming the value, the currency and the file.
  Rationale: this project has no silent writes — the confirm loop prints `Saved {n} {currency} ticker(s) to {path}.` and the benchmark prompt prints `Benchmark set to {ticker}.` A rate reaching disk silently would be the only one, and since remembering is a side effect of a flag rather than an explicit command, it is exactly the kind of write a user needs told about. Naming the file also gives them the only way to undo it.
  Date/Author: 2026-09-06, added during design review.

- Decision: the report never prints a rate without its origin — including when the origin is the plain configured default.
  Rationale: making the parenthetical conditional would mean a call site somebody forgot to wire prints a bare, entirely plausible `Risk-free rate used: 0.0200` and no test fails. That is the motivating bug in miniature. Printing an origin unconditionally makes an unprovenanced number impossible to produce. As a second defence, `cli.main` rebinds `args.risk_free_rate` to the resolved value immediately after resolving, so no downstream consumption site can be missed — `holdings_cli.main` already mutates `args` this way for `--currency`.
  Date/Author: 2026-09-06.

- Decision: only an explicit choice is ever written to the file; the configured default never is.
  Rationale: the rule `DEFAULT_BENCHMARKS`' docstring already states for `memory/candidates.json`, and it holds for the same reason — improving the default later still reaches every currency that never named one, instead of being shadowed by a value that got baked into the file once.
  Date/Author: 2026-09-06.

- Decision: there is NO per-currency default table, unlike `src/optimizer/benchmark.py`'s `DEFAULT_BENCHMARKS`.
  Rationale: `SPY` is defensible as a source constant because the S&P 500 has stood for "the US market" for decades. A policy rate changes several times a year, so a table of per-currency rates compiled into the source would be wrong by the time it shipped and wrong differently every quarter after. That is exactly why this value has to be remembered from the user rather than assumed — and why the report must say which source a printed rate came from. Consequently `resolve_risk_free_rate`'s `currency` parameter drives only the origin string; it consults no table, and its docstring says so rather than implying one.
  Date/Author: 2026-09-06.

- Decision: `resolve_risk_free_rate` lives in `src/flow/rate_memory.py` but takes the fallback as an explicit `default` parameter rather than reading `settings`.
  Rationale: `resolve_benchmark_ticker` lives in `src/optimizer/benchmark.py` beside `DEFAULT_BENCHMARKS`, deliberately not in `candidate_memory.py` — persistence and resolution are separated there. There is no equivalent module for rates to sit beside (see the previous entry: no table), so it goes in `rate_memory.py`. Taking `default` as a parameter is what keeps that placement honest: the function stays pure and testable, and the module keeps the property both its siblings advertise — no `src/config/settings` import.
  Date/Author: 2026-09-06, corrected during design review.

- Decision: in `holdings_cli`, resolution lives in `_report` but persistence does NOT; the persist target is decided once in `main` before dispatch.
  Rationale: `_run_show` calls `_report` in a loop over every saved currency, so putting the write there would make a bare `show --risk-free-rate 0.03` write the same rate into every currency — the very error class this feature exists to fix. Deciding in `main` also catches `remove GHOST --risk-free-rate ...`, which falls through to `_run_show` after failing to find the ticker.
  Date/Author: 2026-09-06, corrected during design review.

- Decision: an explicit `--risk-free-rate` persists only to a currency the command actually DETERMINED, and anything ambiguous is refused by name.
  Rationale: "determined" means an explicit `--currency`, a `set`'s or `remove`'s resolved target currency, or the single saved portfolio when exactly one exists. It excludes two cases that would otherwise write a rate on the strength of a fallback rather than an observation: a `show` with nothing saved at all, which reports `DEFAULT_CURRENCY` only so the "add some holdings" hint gets printed; and a `set` whose tickers all failed to resolve, which leaves `partition_by_currency` returning `None` and prints "Nothing was saved." A `show` spanning several saved currencies with no `--currency` is refused, naming `--currency`, in the same shape `_resolve_remove_currency`'s existing ambiguity refusal uses.
  Date/Author: 2026-09-06.

- Decision: persisting happens from ANY `--selection`, unlike `--benchmark`, which `_settle_benchmark` writes only for `user_provided`.
  Rationale: a benchmark belongs to a *pool*, and an agent-chosen candidate list has no pool file to record one in. A rate belongs to a *currency*, which every run has. Worth recording the reachability consequence: for every selection but `user_provided`, `currency` is the hardcoded `DEFAULT_CURRENCY`, so `uv run portfolio` can only ever record a USD rate unless a `user_provided` pool is non-USD. That is correct — the agent selections are USD by construction, as `_settle_benchmark`'s own docstring argues — but it means the practical route to a JPY rate is `portfolio-holdings --currency JPY` or a JPY `user_provided` pool.
  Date/Author: 2026-09-06.

- Decision: `memory/rates.json` is written atomically (temp file, then `os.replace`), and a write whose value is already identical is skipped entirely.
  Rationale: two differences from the sibling memory files, both justified by this file's larger blast radius. Unlike `candidates.json` (read only by `user_provided`) and `portfolio.json` (read only by the holdings block), `rates.json` sits on the startup path of BOTH commands for EVERY currency, so one truncated write breaks everything — and `Path.write_text` truncates before writing, so an interrupted save leaves exactly that. Skipping a no-op write means `updated_at` records when the rate actually changed rather than when it was last echoed, which matters because a flagged run rewrites it every time; this is `migrate_candidate_pools`' reasoning ("that would stamp the current time over the only record of when the pool was last curated") applied to a value that is re-stated often. Retrofitting atomic writes to the two sibling modules is a sensible follow-up and is out of scope here.
  Date/Author: 2026-09-06.

- Decision: currency keys are normalized to upper case on read as well as on write.
  Rationale: neither sibling module does this, so a hand-edited `{"portfolios": {"jpy": ...}}` yields `currency == "jpy"` flowing onward. In this file that would create a `"jpy"` entry distinct from `"JPY"` — two rates for one currency, with no way to tell which a report used.
  Date/Author: 2026-09-06.

- Decision: the risk-free rate remains NOT editable inside `_run_edit_loop`.
  Rationale: unchanged from `plans/10_performance_reporting_and_target_return.md`'s Decision Log, and worth restating because this plan makes the rate stickier: unlike MV's target return, the rate is a property of the market environment rather than of the portfolio being designed, so changing it mid-session invites treating it as a tuning knob to make a Sharpe ratio look better. The origin string is nevertheless carried through the loop for the same reason the rate itself already is — otherwise provenance would appear on the initial report and silently vanish on the first edit.
  Date/Author: 2026-09-06.


## Limitations


Recorded deliberately rather than fixed, so a future contributor knows they were considered.

**There is no way to un-remember a rate from the command line.** The escape hatch is editing or deleting `memory/rates.json`, which is a small, hand-editable, gitignored file — and which is why the announcement line names it. A `--forget-risk-free-rate` flag or a `rate` subcommand is the obvious follow-up if this proves annoying. Refusing out-of-range values at the door (see the `Decision Log`) removes the case where the absence would really hurt.

**A comparison run leaves its rate standing.** `plans/10_performance_reporting_and_target_return.md` added `--risk-free-rate` specifically because "comparing rates is a real workflow", and three runs at 0.02, 0.03 and 0.045 now leave 0.045 remembered for that currency. The announcement line makes this visible rather than silent, which is the mitigation; it is not a fix.

**A session's rate is fixed at resolution time.** A `portfolio-holdings ... --risk-free-rate` run in another terminal does not change a live `uv run portfolio` session's subsequent reprints. Read-modify-write races on the per-currency merge are the same weakness `save_candidate_pool` and `save_portfolio` already have, and are equally acceptable for a single-user local tool.


## Context and Orientation


This repository is a Python 3.12 project managed with `uv`. Application code lives under `src/`, imported as `src.<package>.<module>`. Tests live flat under `tests/`, run with `uv run pytest tests/test_*.py`, and must never make a network call. Console commands are declared in `pyproject.toml`'s `[project.scripts]`.

The pieces this plan touches, by full path.

`src/config/settings.py` holds a `pydantic-settings` singleton named `settings`, the single source of every environment-configurable value. `risk_free_rate: float = 0.02` is the field this plan is about; because pydantic-settings matches field names to environment variables case-insensitively, `RISK_FREE_RATE` in the environment or in a `.env` file at the repository root already overrides it. The singleton is constructed at import time, which is why every default argument written as `risk_free_rate: float = settings.risk_free_rate` is bound once, at import, and cannot be changed by mutating `settings` later.

`src/dataset/ticker_currency.py` enforces this project's rule that one portfolio holds exactly one currency. It exposes `DEFAULT_CURRENCY = "USD"` (assumed for any ticker with no recorded currency) and `partition_by_currency(candidates, pool_currency, currencies)`, which splits a batch of tickers into those that may join a pool of a given currency and those refused by name for trading in another. Currency conversion is deliberately not implemented.

`src/flow/candidate_memory.py` and `src/flow/user_portfolio.py` are the two existing per-currency memory modules, and the direct models for this plan's third. Their shapes are `{"pools": {"USD": {"tickers": [...], "benchmark": "SPY", "updated_at": "..."}}}` and `{"portfolios": {"USD": {"positions": {"SPY": 1000.0}, "updated_at": "..."}}}` respectively. The semantics this plan copies, each for the reason those modules' docstrings give: a missing file reads as empty rather than raising, because that is the expected state on a first run; malformed JSON lets `json.JSONDecodeError` propagate, because the file is hand-editable and a syntax error must be seen rather than papered over with an empty result that looks like "nothing saved"; a wrongly-typed field raises `ValueError` naming both the file and the currency it was found under, so the failure is reported where it can be fixed; and a save re-reads every currency and replaces only its own slot, carrying every other currency's contents *and its own `updated_at`* over untouched, so saving one currency can never disturb another or make it look freshly edited. Neither module imports `src/config/settings`, and `user_portfolio.py`'s docstring says so explicitly as a property worth keeping.

`src/optimizer/benchmark.py` is the closest architectural precedent, and this plan mirrors two things from it. `resolve_benchmark_ticker(currency, override, saved) -> str | None` is a pure function implementing override-then-saved-then-default precedence — pure, taking `saved` as a parameter rather than reading a file. And `DEFAULT_BENCHMARKS: dict[str, str] = {"USD": "SPY"}` carries a docstring arguing that only USD gets a default because `SPY` is uncontested while no other currency has an obvious single answer, and noting that "because a default is never written into `memory/candidates.json` — only an explicit choice is — adding a currency here later still reaches every pool that never named one". Both arguments carry over; the table itself does not (see the `Decision Log`).

`src/optimizer/portfolio.py` is the estimation layer. `compute_weights_and_stats(returns_matrix, objective, target_annual_return, risk_free_rate)` both *fits* MSR at the given rate (via `EfficientFrontier.max_sharpe`) and *measures* the reported Sharpe ratio against it. Two consequences matter here. A stored rate therefore changes MSR's WEIGHTS, not merely a printed number — which is correct, since a Sharpe-maximizing portfolio is defined relative to a rate, but is worth knowing. And that function's docstring already records that "a pool whose every ticker has an expected annual return at or below `risk_free_rate` makes MSR genuinely undefined, and PyPortfolioOpt raises `ValueError` saying so" — the failure mode a nonsense stored rate would cause on every later run, and the reason this plan refuses out-of-range values rather than warning about them. `stats_for_weights(returns_matrix, weights, risk_free_rate)` measures a given weight vector, and is how the user's own holdings are scored. Neither function takes a `currency`.

`src/flow/interactive.py` orchestrates a run and threads `risk_free_rate` from the CLI down to the optimizer, the benchmark and the holdings, through `run_pipeline_against`, `compute_weights_and_allocation` and `prepare_holdings`. `src/flow/cli.py` is the `portfolio` command: a flat `argparse` parser, plain `print` output, raw `input()` for its interactive loops, and three printing functions — `print_pipeline_result(result)`, `print_weights_and_allocation(...)` (which owns the `Risk-free rate used:` line for the pool) and `print_user_portfolio(holdings, path)` (which owns it for the user's holdings). `_settle_benchmark` is the function whose shape this plan's `_settle_risk_free_rate` mirrors. `src/flow/holdings_cli.py` is the `portfolio-holdings` command, whose `_report(currency, args)` is the single funnel every subcommand ends with, and whose `main` already catches `ValueError` into an exit-2 message.

`src/flow/backtest.py` computes the realized Sharpe ratio of a 52-month backtest with its own convention and is deliberately untouched by this plan.

`memory/` is gitignored, so nothing written there is ever committed.


## Plan of Work


Four milestones, each independently verifiable. The shape of the whole change:

    memory/rates.json                  <- new: one risk-free rate per currency
      |
      v
    src/flow/rate_memory.py            <- new: read/write it, plus PURE validate/resolve helpers.
      |                                   No network. No src/config/settings import.
      +------------------------+
      |                        |
      v                        v
    src/flow/holdings_cli.py   src/flow/cli.py
      - resolve in _report       - validate right after parse_args()
      - persist decided once     - resolve once the currency settles
        in main                  - persist after the report succeeds
      - --rates-path             - --rates-path, origin threaded through the edit loop
      |                        |
      v                        v
    src/flow/cli.py's three printers gain a trailing optional
    `risk_free_rate_origin`, so every printed rate names its source.

Nothing under `src/optimizer/` changes. That is the point of resolving at the edge.


## Milestones


### Milestone 1 — the store


Scope: one new module and its tests. Nothing else changes; no network, no database, no user-visible behaviour yet. At the end of this milestone the file format exists and is proven against the failure modes a hand-editable JSON file invites.

Create `src/flow/rate_memory.py`. It must not import `src/config/settings`, `src/optimizer/*`, or any other `src/flow` module — only `DEFAULT_CURRENCY` from `src/dataset/ticker_currency.py`, matching both sibling modules.

    DEFAULT_RATES_PATH = "memory/rates.json"

    def validate_risk_free_rate(rate: object, source: str) -> float
    def load_all_risk_free_rates(path: str = DEFAULT_RATES_PATH) -> dict[str, float]
    def load_risk_free_rate(path: str = DEFAULT_RATES_PATH,
                            currency: str = DEFAULT_CURRENCY) -> float | None
    def save_risk_free_rate(rate: float, path: str = DEFAULT_RATES_PATH,
                            currency: str = DEFAULT_CURRENCY) -> bool

    class ResolvedRiskFreeRate(NamedTuple):
        rate: float
        origin: str
        from_override: bool

    def resolve_risk_free_rate(override: float | None, currency: str,
                               saved: float | None, default: float) -> ResolvedRiskFreeRate

On-disk shape, one entry per currency under a top-level `"rates"` key:

    {
      "rates": {
        "USD": {"risk_free_rate": 0.0425, "updated_at": "2026-09-06T06:20:11.123456+00:00"},
        "JPY": {"risk_free_rate": 0.005,  "updated_at": "2026-09-06T06:21:02.987654+00:00"}
      }
    }

`"rates"` rather than `"pools"` or `"portfolios"` so a person who opens any of the three memory files can tell at a glance which one they have. No legacy shape and no migration command: this file did not exist before.

`validate_risk_free_rate` is the single gate every value passes through, whether it arrived from a command line or from the file, and `source` is the human-readable thing it names in a failure (`"--risk-free-rate"`, or `"'memory/rates.json''s 'JPY' entry"`). It refuses, with a message a person can act on:

- a value that is not a number, and a `bool` explicitly — `isinstance(True, int)` is `True` in Python, so `{"risk_free_rate": true}` would otherwise become a 100% rate. PyPortfolioOpt's own guard has this same hole, so nothing downstream would catch it.
- a non-finite value. `argparse`'s `type=float` accepts `nan`, `inf` and `-inf`, so before this change `--risk-free-rate nan` printed `Sharpe: nan` without complaint.
- a magnitude of 1 or more, with a message saying rates are decimals so 4.5% is `0.045`. See the `Decision Log` for why this is a refusal rather than a warning.

Negative values are accepted, and a test must pin that.

`save_risk_free_rate` returns `True` when it wrote and `False` when the stored value was already identical, so a caller knows whether to print the announcement line. It writes atomically: `json.dumps(..., indent=2)` to a temp file in the same directory, then `os.replace`.

`resolve_risk_free_rate` is pure. It returns the override when one was given (with `from_override=True` and origin `"--risk-free-rate, remembered for JPY"`), else the saved value (origin `"remembered for JPY"`), else `default` (origin `"the configured default"`). Its `currency` argument drives only the origin string; the docstring must say so, because a reader will otherwise assume a lookup table exists.

Create `tests/test_rate_memory.py`: module docstring citing `AGENTS.md`'s no-network requirement, `tmp_path`, long full-sentence names. Cover the precedence table with its three origins; a negative rate round-tripping; refusal of a non-number, a bool, `nan`, `inf`, `1.0` and `4.5`, each naming its source; the file *and* currency named when a bad value comes from the file; malformed JSON propagating; per-currency independence including the untouched `updated_at`; a re-save of an identical value returning `False` and leaving `updated_at` alone; upper-casing of a lower-case currency key on read and on write; the parent directory being created; and a missing file reading as `{}` / `None`.

Commands and acceptance, from the repository root:

    uv run pytest tests/test_rate_memory.py -q     # expect every test in the new file to pass
    uv run pytest tests/test_*.py -q               # expect no change: this milestone adds a module


### Milestone 2 — `uv run portfolio-holdings`


Scope: the command where the bug was reported. At the end of this milestone one `uv run portfolio-holdings show` measures a JPY portfolio and a USD portfolio each against its own remembered rate.

In `src/flow/cli.py`, add the display half first, since both commands share it:

- `print_user_portfolio(holdings, path, risk_free_rate_origin=None)` — a *trailing optional* parameter, which is what keeps this cheap: the two test files call this printer directly about twenty times and none of those calls change. When given, it is appended in parentheses to the existing `Risk-free rate used:` line.
- That line must also appear in the `Figures: n/a - reason` branch, which today omits it. A reader hitting `--no-holdings-fetch` or an unpriceable holding is exactly the reader trying to work out why the numbers moved, and hiding the rate from them is backwards.

In `src/flow/holdings_cli.py`:

- `--risk-free-rate` default becomes `None`, so "not given" is distinguishable from "given 0.02". Its help text states the precedence, and loses the now-obsolete sentence "Use the same value here as for `uv run portfolio` if you intend to compare the two reports" — sharing the value automatically is the whole point.
- New `--rates-path`, default `DEFAULT_RATES_PATH`, mirroring `--path`. This is not optional polish: an argparse default is bound at parse time, so tests achieve hermeticity by *passing* these flags, which is why `tests/test_holdings_cli.py`'s `_run` helper already appends `--path` to every invocation.
- Validate the flag's value in `main`, right after `parse_args()`, before any Yahoo Finance round trip.
- `_report(currency, args)` resolves per reported currency — `load_risk_free_rate(args.rates_path, currency)`, then `resolve_risk_free_rate(...)` — and passes both `resolved.rate` and `resolved.origin` onward. This is what fixes the reported bug: `_run_show` loops over every saved currency, and each iteration now resolves that currency's own rate.
- Persistence is decided ONCE in `main`, before dispatch, and never inside `_report` — see the `Decision Log`. The persist target is a currency the command determined: an explicit `--currency`; `set`'s or `remove`'s resolved target; or the single saved portfolio when exactly one exists. A `show` spanning several saved currencies with no `--currency` raises `ValueError` naming `--currency`, which the existing handler turns into exit 2. Nothing is written when the currency came from the nothing-is-saved `DEFAULT_CURRENCY` fallback, nor when `set` resolved no currency at all.
- `main`'s existing `except ValueError` also catches `json.JSONDecodeError`, into an exit-2 message naming the file. A malformed `rates.json` now sits on the startup path of a bare `portfolio-holdings show`, and a raw traceback there would be a poor answer to a one-byte typo.

Tests, in `tests/test_holdings_cli.py`: add `--rates-path` to the `_run` helper; a stored rate applies with no flag; the flag overrides and rewrites a stored rate and announces it; `show` across two currencies measures each against its own stored rate; the ambiguous `show --risk-free-rate` exits 2 and writes nothing; with exactly one saved currency it writes exactly one entry; with nothing saved it refuses rather than inventing USD; `--risk-free-rate 4.5` and `nan` are refused before any ingest; and the report line's parenthetical is right in each of the three origin cases, including in the `Figures: n/a` branch.

Commands and acceptance:

    uv run pytest tests/test_holdings_cli.py tests/test_rate_memory.py -q

    uv run portfolio-holdings --currency JPY --risk-free-rate 0.005 show
    # expect: "Remembered 0.0050 as the JPY risk-free rate in memory/rates.json."
    #         then "Risk-free rate used: 0.0050 (--risk-free-rate, remembered for JPY)"

    uv run portfolio-holdings show
    # expect, in ONE invocation: the JPY block at 0.0050 (remembered for JPY)
    #         and the USD block at 0.0200 (the configured default)


### Milestone 3 — `uv run portfolio`


Scope: the same rate reaching all three blocks of the pipeline report. At the end of this milestone a JPY `user_provided` run shows the pool's figures, the benchmark line and the holdings block all measured against the remembered JPY rate.

In `src/flow/cli.py`:

- `--risk-free-rate` default becomes `None`; new `--rates-path`; the help text states the precedence and that a value given here is remembered for the run's currency.
- Validate right after `parse_args()`, so a bad value exits before `open_pipeline_session` builds a live snapshot.
- `_settle_risk_free_rate(override, currency, rates_path) -> ResolvedRiskFreeRate`, placed beside `_settle_benchmark` and documented against it: load, resolve, and deliberately do NOT write. Call it once the currency has settled — after the `user_provided` confirm loop, before `_settle_benchmark` — then rebind `args.risk_free_rate = resolved.rate` so no downstream consumption site can be missed.
- Persist after `run_pipeline_against` has returned, printing the announcement line. Not before: see the `Decision Log`.
- `print_weights_and_allocation` and `print_pipeline_result` each gain the same trailing optional `risk_free_rate_origin`. `print_pipeline_result` takes only the result dict, so it gets a parameter rather than the origin riding inside that dict — the origin is a display string, and `run_pipeline_against` should not grow a parameter that puts resolution's vocabulary into the orchestration layer.
- `_run_edit_loop` carries the origin, for exactly the reason it already carries the rate: otherwise provenance appears on the initial report and vanishes on the first `[a]dd`.

Tests, in `tests/test_cli.py`: add `--rates-path` to the `main()` tests; one resolution reaches all three of `run_pipeline_against`, `prepare_holdings` and `_run_edit_loop`; a stored rate applies with no flag and the flag overrides it; `main` with the flag writes to `--rates-path` and not to `DEFAULT_RATES_PATH`; `--risk-free-rate nan` is refused before `open_pipeline_session` is entered (assert the session stub was never called); a run that raises out of the optimizer leaves the rates file untouched; and `_run_edit_loop` reprints the origin on every recompute.

Also add one test asserting that with a stored USD rate of 0.0425 in place, `backtest.compute_sharpe_ratio` still resolves 0.02 — the guarantee this project's published figures rest on.

Commands and acceptance:

    uv run pytest tests/test_*.py -q      # expect every pre-existing test to pass, test_optimizer.py unmodified

    uv run portfolio --date today --objective GMV --value 100000 --selection user_provided
    # against the JPY pool: expect the pool's figures, the benchmark line and the
    # holdings block ALL reporting "Risk-free rate used: 0.0050 (remembered for JPY)"


### Milestone 4 — documentation


Scope: reconciling the documents this change contradicts. This repository treats its plans as living documents, and this change supersedes a decision recorded in one of them, so this milestone is not optional tidying.

`README.md`, two edits. The Live Mode bullet promising "Users can ask the system to remember risk free rate and/or target return rate. The system will store them in `memory/long-term.md`" becomes: the risk-free rate is remembered per currency in `memory/rates.json`, because a rate is a property of a currency and this project's portfolios are single-currency by construction, so one global value cannot be right for a dollar and a yen portfolio at once; the target return remains unremembered. And the `RISK_FREE_RATE` environment-variable entry must state that a stored per-currency rate outranks it, or someone who sets it and sees a different number has no way to find out why.

Then revision notes on five plans:

`plans/10_performance_reporting_and_target_return.md` is the important one. Its Decision Log contains "Decision: do not implement `README.md`'s `memory/long-term.md` persistence of the risk-free rate and target return", and both its Surprises and its Outcomes sections claim a future memory plan could feed these values in "without any signature change". Reverse the decision in writing, and correct that claim: it is false for a per-currency design, because resolution needs the currency and `compute_weights_and_stats`, `stats_for_weights` and `compute_sharpe_ratio` have none. Name what actually changed — the two argparse defaults, not the optimizer — and restate that the rate stays non-editable in the edit loop for the reason that plan gives.

`plans/11_non_us_tickers_and_single_currency.md` carries the motivating evidence: two reproduce-me commands passing `--risk-free-rate 0.005`, with the note "the 2% default is a dollar rate". Add that the flag is now remembered per currency, so those commands need it only once.

`plans/08_consistency_review.md`'s Finding 11 tracks the whole unbuilt persistent-memory architecture and names `memory/long-term.md (user-remembered risk-free/target-return overrides)`. Record that the risk-free half is now built as per-currency `memory/rates.json`, that the target-return half remains open, and that a future `long-term.md` must not duplicate it.

`plans/12_benchmark_per_candidate_pool.md` shows `Risk-free rate used: 0.0200` in its transcripts. Note that the line now carries provenance, and cross-reference that this plan's precedence mirrors `resolve_benchmark_ticker`'s while its persistence gate mirrors `_settle_benchmark`'s validated-choice-only rule.

`plans/13_user_portfolio.md`'s `Interfaces` block, its "Shared flags: ... `--risk-free-rate` (default `settings.risk_free_rate`)" line, and the help text it specifies ("Use the same value here as for `uv run portfolio`") are all now wrong.

Finally, fill in this plan's `Surprises & Discoveries` and `Outcomes & Retrospective`, and mark every `Progress` item.


## Validation and Acceptance


Run the whole suite from the repository root. Every pre-existing test must pass and `tests/test_optimizer.py` must pass **unmodified**, which is the evidence that resolving at the CLI edge left the optimizer alone:

    cd /app/agentic_portfolio
    uv run pytest tests/test_*.py -q

Then the behaviour a human can verify, phrased as observations.

1. `uv run portfolio-holdings --currency JPY --risk-free-rate 0.005 show` prints `Remembered 0.0050 as the JPY risk-free rate in memory/rates.json.` and then a JPY block whose line reads `Risk-free rate used: 0.0050 (--risk-free-rate, remembered for JPY)`.
2. `uv run portfolio-holdings show`, with no flags at all, prints the JPY block at `0.0050 (remembered for JPY)` and the USD block at `0.0200 (the configured default)` — two rates, one invocation. This is the reported bug, fixed.
3. `uv run portfolio-holdings --currency USD --risk-free-rate 0.0425 show`, then `uv run portfolio-holdings show`: both blocks now on their own remembered rates. `cat memory/rates.json` shows two entries with independent `updated_at` values.
4. `uv run portfolio --date today --objective GMV --value 100000 --selection user_provided` against the JPY pool: the pool's figures, the benchmark line and the holdings block all report `Risk-free rate used: 0.0050 (remembered for JPY)`.
5. `uv run portfolio-holdings --currency USD --risk-free-rate 4.5 show` is refused with a message saying rates are decimals, and `memory/rates.json` is unchanged.
6. `uv run portfolio-holdings --risk-free-rate 0.03 show`, with two saved portfolios and no `--currency`, exits non-zero naming `--currency`, and writes nothing.
7. `uv run portfolio --date today --objective GMV --value 1 --risk-free-rate nan` is refused before any fetch happens — visible as the absence of any yfinance or Wikipedia output.
8. `data/portfolio.duckdb`'s size and mtime are unchanged across all of the above.


## Idempotence and Recovery


Every step is additive and repeatable. Milestone 1 adds a module. Milestones 2 and 3 change two argparse defaults and add a flag apiece; re-running their tests is free. `save_risk_free_rate` is idempotent by construction — an identical value is not written at all, so `updated_at` does not churn — and it never touches another currency's entry, so a repeated flagged run is harmless. The write is atomic, so an interrupted save leaves the previous file intact rather than a truncated one.

Recovery paths. `memory/rates.json` is a small hand-editable JSON file outside version control; deleting it resets every currency to the configured default, losing only the remembered rates, which are one flag each to restore. A file corrupted by hand-editing raises `json.JSONDecodeError` naming the file, and `portfolio-holdings` reports that as an exit-2 message rather than a traceback. No step migrates or rewrites `memory/candidates.json` or `memory/portfolio.json`, and no step writes to `data/portfolio.duckdb` — verifiable by the size-and-mtime check above — so there is nothing to back up beyond `git checkout` of the source files.


## Outcomes & Retrospective


**Delivered, and the reported bug is visibly fixed.** One `uv run portfolio-holdings show`, with no flags at all, now measures a yen portfolio against a yen rate and a dollar portfolio against a dollar rate, and says which rate each used. `uv run portfolio` reads the same file, so the pool's figures, the benchmark's and the user's own holdings' all arrive on one scale — which is what those three blocks being printed one under another was always supposed to mean.

The shape of the change: one new module (`src/flow/rate_memory.py`, 300 lines), two CLI layers touched, one new test file, and 497 tests passing where 430 did before. Nothing under `src/optimizer/` changed at all, and `tests/test_optimizer.py` passes unmodified — the evidence that resolving at the CLI edge was the right call rather than merely the convenient one.

**What the design review bought.** Three things in the first draft would have shipped bugs, and all three were caught before any code existed. Persistence was going to happen before the optimizer had proven the rate usable, which would have left a remembered rate behind from a run that printed nothing — reversing an ordering rule `plans/10` had already established and pinned with a test. Validation was going to happen after the currency settled, which would have refused a mistyped rate only after a live snapshot fetch and a full interactive confirm loop had been paid for. And an out-of-range rate was going to draw a caution rather than a refusal, which composes with "remembered forever" and "no way to clear it" into a trap: `--risk-free-rate 4.5` would have warned once and then broken every later `MSR` run with a message naming neither the rate nor the file. The review also caught that the resolver's placement gave the persistence module a `settings` import both its siblings deliberately lack, fixed by taking the fallback as a parameter — which is what `resolve_benchmark_ticker` does anyway.

**What went wrong regardless.** The one bug that reached a running program was mine and was of exactly the class the review had warned about in the abstract: a display parameter defaulted to `None`, one of four call sites was not wired, and the result was a plausible-looking number with no provenance and no failing test. It was found by reading output, not by testing. See `Surprises & Discoveries`; the general lesson is that an optional parameter added for backwards compatibility buys that compatibility by making omissions invisible.

**What remains.** The three items under `Limitations` are open by choice: no command-line way to un-remember a rate, a comparison run leaving its last rate standing, and a session's rate fixed at resolution time. The target-return half of `README.md`'s `memory/long-term.md` promise is still unbuilt, and `plans/08_consistency_review.md`'s Finding 11 has been updated to say so and to warn that a future `long-term.md` must not duplicate the rate. Retrofitting `rate_memory`'s atomic write to `candidate_memory` and `user_portfolio` is a small, obvious follow-up.


## Artifacts and Notes


The reported bug, before and after. Before, one `show` applied the 2% dollar default to both portfolios:

    Your portfolio (JPY), ...
    Annual return: 0.2159  Annual volatility: 0.1770  Sharpe: 1.1065
    Risk-free rate used: 0.0200

    Your portfolio (USD), ...
    Annual return: 0.1225  Annual volatility: 0.1481  Sharpe: 0.6919
    Risk-free rate used: 0.0200

After — the same command, no flags, from the live run of 2026-09-06:

    $ uv run portfolio-holdings show

    Your portfolio (JPY), from memory/portfolio.json:
      1321.T: 50 shares  ¥3,366,500.00 JPY  weight 1.0000
    Total value: ¥3,366,500.00 JPY
    Returns window: 2021-10-01 to 2026-09-01 (60 month(s) of monthly returns)
    Expected return / volatility (annualized):
      1321.T: return=0.2159  volatility=0.1770
    Annual return: 0.2159  Annual volatility: 0.1770  Sharpe: 1.1913
    Risk-free rate used: 0.0050 (remembered for JPY)

    Your portfolio (USD), from memory/portfolio.json:
      SPY: 1,000 shares  $770,190.00 USD  weight 1.0000
    Total value: $770,190.00 USD
    Returns window: 2021-10-01 to 2026-09-01 (60 month(s) of monthly returns)
    Expected return / volatility (annualized):
      SPY: return=0.1225  volatility=0.1481
    Annual return: 0.1225  Annual volatility: 0.1481  Sharpe: 0.5400
    Risk-free rate used: 0.0425 (remembered for USD)

Both Sharpe ratios moved, which is the point: the yen portfolio was being penalised 2% it never had to clear (1.1065 to 1.1913), and the dollar portfolio was being credited for clearing only 2% when the real dollar rate was 4.25% (0.6919 to 0.5400). That second one had been reporting a portfolio as meaningfully better than it was.

Remembering a rate, announced:

    $ uv run portfolio-holdings --currency JPY --risk-free-rate 0.005 show
    ...
    Risk-free rate used: 0.0050 (--risk-free-rate, remembered for JPY)
    Remembered 0.0050 as the JPY risk-free rate in memory/rates.json.

One rate reaching all three blocks of a pipeline run, from a live JPY `user_provided` run:

    $ uv run portfolio --date today --objective GMV --value 15000000 --selection user_provided

    Portfolio expected return: 0.1373  Portfolio volatility: 0.1913  Portfolio Sharpe: 0.6916
    Benchmark 1321.T: return=0.2159  volatility=0.1770  Sharpe=1.1913  (60 of 60 month(s))
    Risk-free rate used: 0.0050 (remembered for JPY)
    ...
    Your portfolio (JPY), from memory/portfolio.json:
      1321.T: 50 shares  ¥3,366,500.00 JPY  weight 1.0000
    ...
    Annual return: 0.2159  Annual volatility: 0.1770  Sharpe: 1.1913
    Risk-free rate used: 0.0050 (remembered for JPY)

Note the benchmark's Sharpe (1.1913) and the holdings block's Sharpe (1.1913) agree exactly — this portfolio holds only `1321.T`, which is also the pool's benchmark, so the two must, and they still do at the new rate. That is `plans/13_user_portfolio.md`'s comparability guarantee surviving the change.

The three refusals:

    $ uv run portfolio-holdings --currency USD --risk-free-rate 4.5 show
    error: --risk-free-rate must be a decimal rate smaller than 1.0 in magnitude, got 4.5;
    rates are decimals here, so 4.5% is 0.045

    $ uv run portfolio-holdings --risk-free-rate 0.03 show
    error: --risk-free-rate has to be remembered for one currency, and this command names
    none; add --currency (for example --currency JPY)

    $ uv run portfolio --date today --objective GMV --value 1 --risk-free-rate nan
    portfolio: error: --risk-free-rate must be a finite number, got nan

`memory/rates.json` after all of it, with independent timestamps:

    {
      "rates": {
        "JPY": {
          "risk_free_rate": 0.005,
          "updated_at": "2026-09-06T06:35:25.264869+00:00"
        },
        "USD": {
          "risk_free_rate": 0.0425,
          "updated_at": "2026-09-06T06:35:33.405083+00:00"
        }
      }
    }

And the isolation check: `data/portfolio.duckdb`'s size and mtime were byte-identical (`46936064 1788571364`) before and after every one of these runs.

The intended shape of `memory/rates.json` after the acceptance commands:

    {
      "rates": {
        "JPY": {
          "risk_free_rate": 0.005,
          "updated_at": "2026-09-06T06:31:12.884401+00:00"
        },
        "USD": {
          "risk_free_rate": 0.0425,
          "updated_at": "2026-09-06T06:32:44.117293+00:00"
        }
      }
    }


## Interfaces and Dependencies


No new third-party dependency; everything needed is already declared in `pyproject.toml`. No new console script: both existing entry points gain flags.

In `src/flow/rate_memory.py`:

    DEFAULT_RATES_PATH: str

    def validate_risk_free_rate(rate: object, source: str) -> float: ...
    def load_all_risk_free_rates(path: str = DEFAULT_RATES_PATH) -> dict[str, float]: ...
    def load_risk_free_rate(path: str = DEFAULT_RATES_PATH,
                            currency: str = DEFAULT_CURRENCY) -> float | None: ...
    def save_risk_free_rate(rate: float, path: str = DEFAULT_RATES_PATH,
                            currency: str = DEFAULT_CURRENCY) -> bool: ...

    class ResolvedRiskFreeRate(NamedTuple):
        rate: float
        origin: str
        from_override: bool

    def resolve_risk_free_rate(override: float | None, currency: str, saved: float | None,
                               default: float) -> ResolvedRiskFreeRate: ...

In `src/flow/cli.py`:

    def _settle_risk_free_rate(override: float | None, currency: str,
                               rates_path: str) -> ResolvedRiskFreeRate: ...
    def print_weights_and_allocation(..., risk_free_rate_origin: str | None = None) -> None: ...
    def print_pipeline_result(result: dict, risk_free_rate_origin: str | None = None) -> None: ...
    def print_user_portfolio(holdings: HoldingsStats, path: str,
                             risk_free_rate_origin: str | None = None) -> None: ...

Unchanged, and deliberately so: every signature under `src/optimizer/`, and `src/flow/backtest.py`'s `compute_sharpe_ratio`.


## Revision Notes


- 2026-09-06, after implementation: `Progress`, `Surprises & Discoveries` and
  `Outcomes & Retrospective` filled in from the work as actually done, and `Artifacts and Notes`
  replaced with real transcripts. Two things changed from the plan as written, both recorded in
  `Surprises & Discoveries` with their evidence. A malformed `--risk-free-rate` is now reported
  through `parser.error` (argparse's own exit-2 formatting) rather than left to propagate as a
  `ValueError`, because a malformed argument value is exactly what argparse already reports that way
  and a traceback for a typo is a worse answer. And `holdings_cli.main`'s `json.JSONDecodeError`
  clause had to be placed BEFORE its `ValueError` clause, because the former subclasses the latter —
  as written, the new branch was dead code.
- 2026-09-06, correction found in a live run: the origin string was not threaded into `cli.main`'s
  `print_user_portfolio` call, so the holdings block printed a rate with no provenance while the pool
  block above it printed one with. Fixed, and pinned by
  `test_main_names_the_rates_source_in_the_holdings_block_too`. Recorded at length in
  `Surprises & Discoveries` because the design review had predicted this exact failure mode in the
  abstract and the mitigation chosen at the time (a defaulted optional parameter) is what let it
  happen.
