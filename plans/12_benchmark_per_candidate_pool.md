# Give each candidate pool a benchmark, and report its return, volatility and Sharpe ratio


This ExecPlan is a living document. The sections `Progress`, `Surprises & Discoveries`, `Decision Log`, and `Outcomes & Retrospective` must be kept up to date as work proceeds. This plan must be maintained in accordance with `PLANS.md` at the repository root. This plan builds on `plans/05_optimizer_and_allocation.md` (for `load_returns_matrix`/`compute_weights`/`load_latest_prices`/`allocate_shares`), `plans/06_interactive_flow.md` (for the CLI, its printing functions, its post-run edit loop, and live mode's throwaway snapshot), `plans/09_user_provided_selection.md` (for the `user_provided` selection and its candidate memory), `plans/10_performance_reporting_and_target_return.md` (for `PortfolioStats` and `print_weights_and_allocation`'s current shape), and `plans/11_non_us_tickers_and_single_currency.md` (for per-currency pools, the `ticker_currency` table, and the single-currency rule) — all five checked into this repository.


## Purpose / Big Picture


Before this change the report told you what the optimizer decided and why it decided it, but not whether any of it was worth doing. A run printed each holding's annualized expected return and volatility, the portfolio's own expected return, volatility and Sharpe ratio, the risk-free rate those were measured against, and the window of monthly returns they were estimated from. Every number described the portfolio in isolation. A person reading "Portfolio expected return: 0.1507  Portfolio volatility: 0.1958  Portfolio Sharpe: 0.6675" had no way to answer the obvious next question: is that better than just buying the market?

After this change, each candidate pool has a **benchmark** — a single ticker standing in for "the market" for that pool — and the report prints that benchmark's annual return, annual volatility and Sharpe ratio over exactly the same months, computed with exactly the same estimators, on the line directly below the portfolio's own. That adjacency is the whole feature. Here is a real run from 2026-09-06 against a two-ticker USD pool, with the new line marked:

    $ uv run portfolio --date today --objective GMV --value 100000 --selection user_provided

    Portfolio currency: USD - --value is interpreted as USD
    Returns window: 2021-10-01 to 2026-09-01 (60 month(s) of monthly returns)

    Weights:
      AAPL: 0.5496
      MSFT: 0.4504

    Expected return / volatility (annualized):
      AAPL: return=0.1635  volatility=0.2343
      MSFT: return=0.1351  volatility=0.2510

    Portfolio expected return: 0.1507  Portfolio volatility: 0.1958  Portfolio Sharpe: 0.6675
    Benchmark SPY: return=0.1225  volatility=0.1481  Sharpe=0.6919  (60 of 60 month(s))     <-- new
    Risk-free rate used: 0.0200
    Target annual return: n/a (objective is GMV, not MV)

    Share allocation:
      AAPL: 171
      MSFT: 90
    Leftover cash: $312.13 USD

Read those two lines together and they say something neither says alone. The two-stock portfolio expects a higher return than the S&P 500 (15.07% against 12.25%) but at meaningfully more risk (19.58% against 14.81%), and once the risk-free rate is accounted for it is very slightly the *worse* bet per unit of risk: Sharpe 0.6675 against 0.6919. The stock picking bought return, not efficiency. That conclusion was unavailable before this change.

The comparison can be far more damning, which is the point of having it. The same run against a two-ticker Tokyo pool, benchmarked against TOPIX:

    Portfolio expected return: 0.1373  Portfolio volatility: 0.1913  Portfolio Sharpe: 0.6132
    Benchmark 1306.T: return=0.2009  volatility=0.1168  Sharpe=1.5486  (60 of 60 month(s))

The broad Japanese market returned half again as much as the two hand-picked stocks, at *less* than two thirds their volatility. A person seeing that has learned something actionable about their pool in one line.

Three terms in the above need defining in plain language before anything else, because the rest of this plan leans on them.

A **candidate pool** is the list of tickers a person has assembled to build a portfolio from, saved in the file `memory/candidates.json`. There is one pool per currency, because a single portfolio cannot mix currencies — see the Context section. A **benchmark** is one extra ticker, *not* part of the pool and never bought, whose own historical returns the report compares the portfolio's against; a broad-market exchange-traded fund such as `SPY` (which tracks the S&P 500) is the usual choice. The **returns window** is the run of consecutive months of historical returns the optimizer estimated everything from — up to the most recent 60 months available, reported on the `Returns window:` line above.


## Progress


- [x] (2026-09-06 01:35Z) Promoted `src/optimizer/portfolio.py`'s private `_load_returns_long` to a public `load_returns_long` taking optional window endpoints and an optional read-only open, so the benchmark can read a `returns` table without a lower date bound and without any possibility of writing to it.
- [x] (2026-09-06 01:45Z) Added `src/optimizer/benchmark.py`: `DEFAULT_BENCHMARKS`, `BENCHMARK_MIN_MONTHS`, `MIN_USABLE_ANNUAL_VOLATILITY`, `BenchmarkSource`, `BenchmarkStats`, `resolve_benchmark_ticker`, `empty_benchmark_returns`, `load_benchmark_returns`, `annualized_return_and_volatility`, `benchmark_stats_for_window`.
- [x] (2026-09-06 01:50Z) Added `tests/test_benchmark.py` (19 tests), including the one-asset-portfolio equality proof and the ddof-convention lock.
- [x] (2026-09-06 01:58Z) Added `build_scratch_snapshot` to `src/flow/live.py` and made `build_live_snapshot`'s `user_provided` branch delegate to it, so the temp-database discipline exists in exactly one place.
- [x] (2026-09-06 02:02Z) Added `prepare_benchmark` (plus `_resolve_benchmark_source` and `_benchmark_currency_gate`) to `src/flow/interactive.py`, and threaded an optional `benchmark` through `run_pipeline_against`/`run_pipeline` into a new `"benchmark"` result key.
- [x] (2026-09-06 02:04Z) Added 11 tests to `tests/test_interactive_flow.py`, including one that asserts the session database is never written to.
- [x] (2026-09-06 02:06Z) Added the optional per-pool `benchmark` key to `memory/candidates.json` via `src/flow/candidate_memory.py`: `_validate_benchmark`, `load_pool_benchmarks`, `load_candidate_benchmark`, `save_candidate_pool`'s preserving `benchmark` argument, and `_write_pools`' omission of an absent key.
- [x] (2026-09-06 02:07Z) Added 11 tests to `tests/test_candidate_memory.py`.
- [x] (2026-09-06 02:10Z) Wired the CLI: `--benchmark`, `--no-benchmark-fetch`, `format_benchmark`, the new line in `print_weights_and_allocation`, the benchmark in `_choose_pool_to_resume`'s listing, `_prompt_for_benchmark`, `_settle_benchmark`, the `[b]enchmark` choice in the edit loop, and `main`'s threading.
- [x] (2026-09-06 02:11Z) Added 27 tests to `tests/test_cli.py`, and stubbed `_settle_benchmark` in `_stub_main_pipeline` — left real it put a live yfinance call inside four existing tests.
- [x] (2026-09-06 02:20Z) Fixed a pre-existing crash in `attach_nearest_price` (`src/dataset/fundamentals.py`) that an unresolvable single ticker reaches, with a regression test in `tests/test_dataset_upsert.py`. See Surprises & Discoveries.
- [x] (2026-09-06 02:30Z) Verified end to end: five real CLI runs covering the USD default, the JPY prompt with a refusal, `[b]`, `--benchmark none`, `--no-benchmark-fetch`, and an unresolvable `--benchmark`. `data/portfolio.duckdb` byte-identical throughout (md5 checked before and after).
- [x] (2026-09-06 02:35Z) Documented in `README.md`'s Live Mode section.
- [x] (2026-09-06 02:40Z) Full suite: 360 passed, up from 290 before this plan.
- [x] (2026-09-06 03:05Z) Recorded the shrinkage asymmetry — the one thing the ddof fix does NOT make symmetric — in `src/optimizer/benchmark.py`'s module docstring, in `annualized_return_and_volatility`'s docstring, in this plan's Surprises & Discoveries and Decision Log, and as `tests/test_benchmark.py::test_the_benchmark_is_not_shrunk_against_a_pool`. The report deliberately stays silent about it.
- [ ] Not done, deliberately out of scope: a `DEFAULT_BENCHMARKS` entry for any currency other than USD (the person is asked instead — see the Decision Log); a benchmark for `src/flow/backtest.py`'s 52-month backtest scoring, which reports a realized Sharpe ratio from a different computation entirely; and any currency conversion, which remains unimplemented across the whole project.


## Surprises & Discoveries


- Observation: The obvious way to annualize a benchmark's volatility disagrees with the way the portfolio's own volatility is computed, by about 0.8%. `PortfolioStats.volatility` and `portfolio_volatility` come from `risk_models.CovarianceShrinkage(...).ledoit_wolf()`, which hands scikit-learn a `ddof=0` sample covariance. For a single column, Ledoit-Wolf shrinkage is provably a no-op — its constant-variance shrinkage target *is* that column's variance — so the only difference from `series.std() * sqrt(12)` is the `ddof` denominator, and that difference is large enough to make a benchmark look better or worse than the portfolio for no reason but a convention mismatch.
  Evidence: `AAPL`, 60 months ending 2024-04-01, from the real `data/portfolio.duckdb`:

      one-asset compute_weights_and_stats : return 0.3446037943053359  vol 0.3129845088136446  Sharpe 1.0371241552360968
      benchmark module (this plan)        : return 0.3446037943482607  vol 0.3129845088526308  Sharpe 1.0371241552440567
      series.std() * sqrt(12)             :                            vol 0.31562577516342744

  The first two agree to ~1e-10 (solver noise); the third is 0.8% high. `tests/test_benchmark.py::test_volatility_is_not_the_sample_standard_deviation` locks this in so a future "simplification" cannot quietly break comparability.

- Observation: Fixing the ddof convention makes the two sides agree on the return estimator, the annualization, the ddof convention, the Sharpe definition and the risk-free rate — but NOT on Ledoit-Wolf shrinkage, and there is no way to make them agree on that. Shrinkage is defined relative to a cross-section of assets: it pulls each variance toward the average of them. A benchmark is one column, so scikit-learn shrinks it by exactly zero, while a pool's covariance genuinely is shrunk, and by considerably more than the 0.84% the ddof convention was worth.
  Evidence: a four-asset pool (`AAPL`, `MSFT`, `JNJ`, `XOM`, 60 months to 2024-04-01) drew `delta=0.1763`, moving each holding's annual volatility away from its standalone value in BOTH directions:

      ticker  standalone  inside the pool matrix
      AAPL        0.3130  0.3058  (-2.3%)
      MSFT        0.2061  0.2187  (+6.1%)
      JNJ         0.1614  0.1852  (+14.8%)
      XOM         0.3535  0.3402  (-3.7%)

  and moving that pool's GMV portfolio from `vol=0.1474  Sharpe=0.9378` unshrunk to `vol=0.1527  Sharpe=1.0505` shrunk. So the portfolio side of the comparison is moved several percent by an estimator choice that leaves the benchmark side untouched. See the Decision Log for why the answer is nonetheless to leave the benchmark unshrunk, and for the one caveat that follows.

- Observation: pypfopt is itself internally inconsistent about `ddof`, which is a good reason never to infer a convention and always to read the code path in use. `CovarianceShrinkage.__init__` computes `self.S = self.X.cov().values`, which is pandas' `ddof=1`. The default `ledoit_wolf()` path ignores `self.S` entirely and calls scikit-learn on the raw returns (`ddof=0`); `_ledoit_wolf_single_factor` explicitly converts `np.cov` to `ddof=0` with a `* (t - 1) / t` factor; but `_ledoit_wolf_constant_correlation` uses `self.S` as-is, at `ddof=1`.
  Evidence: read `CovarianceShrinkage` in `.venv/lib/python3.12/site-packages/pypfopt/risk_models.py`. Three shrinkage targets, two ddof conventions between them.

- Observation: Writing the benchmark's rows into the session's own database is not merely untidy, it can move the very window the benchmark is supposed to be measured over. `src/optimizer/portfolio.py`'s `_load_window_dates` derives the portfolio's returns window from `SELECT DISTINCT rebalance_date FROM returns WHERE rebalance_date <= ? ORDER BY rebalance_date DESC LIMIT ?` — from the whole table, with no restriction to the candidate tickers. Benchmark months present in that table are therefore candidates for the portfolio's own window.
  Evidence: read the SQL in `_load_window_dates`; there is no `ticker` predicate. It happens to be harmless today because every writer grids over `compute_rebalance_dates`, so the same date span always produces the same month anchors — but that is a coincidence of the current writers, not a guarantee. Hence the decision that the benchmark always gets its own database.

- Observation: A benchmark whose monthly return never changes does not have zero volatility, it has 6e-18 of it, and a plain `volatility > 0` guard therefore lets through a Sharpe ratio of 1.8e16 — which prints as the most spectacular benchmark ever measured rather than as an error.
  Evidence: a constant 36-month series through `annualized_return_and_volatility` gives `annual_volatility=6.009258394948637e-18`, and the first draft of `benchmark_stats_for_window` reported `sharpe=1.7776741007137714e+16`. Fixed by the named threshold `MIN_USABLE_ANNUAL_VOLATILITY = 1e-12`; pinned by `test_zero_volatility_is_unavailable_rather_than_dividing_by_zero`.

- Observation: A PRE-EXISTING crash, unrelated to benchmarks but reachable through them, in the nearest-price join every returns build uses. DuckDB's `fetchdf()` types an empty `ticker` column as numpy `object` but a non-empty one as pandas' `str`, and `pd.merge_asof` refuses to join across that difference. So `build_returns_for_tickers` raised `MergeError` whenever the `prices` table it read had no rows at all — which is exactly what happens when every ticker in an ingestion batch fails to resolve. Typing a single typo'd ticker at the candidate prompt has always been able to reach this; a typo'd `--benchmark` reaches it every time, because a benchmark is always a batch of one.
  Evidence: before the fix, `uv run portfolio ... --benchmark ZZZZ` reported

      Benchmark ZZZZ: n/a - could not prepare ZZZZ as a benchmark: incompatible merge keys [0] <StringDtype(na_value=nan)> and dtype('O'), must be the same type

  and directly, on a scratch database:

      empty prices ticker dtype: object
      non-empty prices ticker dtype: str
      grid ticker dtype: str
      empty-prices build FAILED: MergeError incompatible merge keys [0] <StringDtype(na_value=nan)> and dtype('O'), must be the same type

  Fixed by casting both join keys with `.astype(str)` inside `attach_nearest_price` (`src/dataset/fundamentals.py`), which is the single shared join all three callers use. Every other test of `build_returns_for_tickers` either stubs it out or supplies prices, which is why nothing caught it; `tests/test_dataset_upsert.py::test_build_returns_for_tickers_survives_a_prices_table_with_no_rows` now covers it. After the fix the same command reports the project's own explanation instead:

      Benchmark ZZZZ: n/a - yfinance returned no non-null close/adj_close values for this ticker (or its yfinance-mapped symbol) across the full 2021-04-06..2026-09-11 fetch window; ...

- Observation: Four existing tests in `tests/test_cli.py` silently acquired a live network call the moment `main` learned to settle a benchmark. `_stub_main_pipeline` stubs `open_pipeline_session`, `run_pipeline_against`, `print_pipeline_result` and `_run_edit_loop`, but a new call in `main` is not covered by any of those, and USD's default benchmark resolves to `SPY`, whose history is not in any stub database — so `prepare_benchmark` went to yfinance for it.
  Evidence: a single trivial argument-parsing test took 6.38s, and the whole file 12.21s. After adding `_settle_benchmark` to the stub, the file runs in 3.03s. AGENTS.md forbids network access in unit tests, so this had to be stubbed rather than tolerated.


## Decision Log


- Decision: The benchmark's price and return history is ALWAYS fetched into a throwaway database of its own (`build_scratch_snapshot`), never into the session's database, in every mode.
  Rationale: Two independent reasons, either sufficient. The shared `data/portfolio.duckdb` cache holds the S&P 500 universe and must not gain rows as a side effect of printing a report — it does not contain `SPY` at all (`SELECT count(*) FROM returns WHERE ticker='SPY'` is 0), so a benchmark would otherwise have to be written somewhere. And `_load_window_dates` reads the whole `returns` table, so benchmark rows in the session database could move the portfolio's own window (see Surprises & Discoveries). Keeping the two databases apart makes "the benchmark never influences the portfolio" structural rather than a convention someone has to remember, and it makes every mode take one uniform path instead of a writable-session case and a read-only-cache case.
  Date/Author: 2026-09-06, this plan's implementation.

- Decision: The three figures are computed with PyPortfolioOpt's `mean_historical_return(..., returns_data=True, frequency=12)` and `sqrt(CovarianceShrinkage(..., frequency=12).ledoit_wolf())`, not with any hand-written mean and standard deviation.
  Rationale: Comparability is the entire feature. A benchmark is a one-asset portfolio, and using the estimators the portfolio itself uses makes the two lines comparable by construction rather than by coincidence — including `mean_historical_return`'s `compounding=True` default, which makes the return a compound annual growth rate rather than an arithmetic mean. A hand-written volatility disagrees by 0.8% (see Surprises & Discoveries). `test_a_one_asset_portfolio_equals_its_own_benchmark` asserts the equality against `compute_weights_and_stats` directly, so the two can never drift apart unnoticed.
  Date/Author: 2026-09-06.

- Decision: Delegate to those two estimator calls rather than calling `compute_weights_and_stats(one_column_frame, "GMV")` and reading its portfolio-level fields.
  Rationale: That delegation was tried and does work exactly (weight comes back `{ticker: 1.0}` and the three figures are the benchmark's own, agreeing to ~1e-10). It was rejected because it invokes a quadratic-programming solver to discover that the only available asset gets all the weight — solver noise, solver failure modes and solver runtime for no benefit. The equality is instead captured as a test, which gives the same guarantee without the machinery.
  Date/Author: 2026-09-06.

- Decision: The benchmark's volatility is its OWN standalone annualized volatility, deliberately not shrunk against the pool's covariance matrix, even though that leaves the two sides of the comparison on different shrinkage regimes. Do not "fix" this by adding the benchmark as an extra column of the pool's returns matrix.
  Rationale: Shrinkage is a function of the cross-section it is applied to, so folding the benchmark into the pool's matrix would make its reported volatility depend on which tickers happen to be in the pool — measured at up to +14.8% for one holding of a four-asset pool (see Surprises & Discoveries). The same benchmark over the same months would then print differently from one run to the next, and could not be compared across pools at all. A reference point that moves when a candidate is added is not a reference point. The signature of `benchmark_stats_for_window` enforces this structurally by taking only a `BenchmarkSource` and a window, never the pool, and `tests/test_benchmark.py::test_the_benchmark_is_not_shrunk_against_a_pool` fails if the implementation ever starts shrinking against one.
  The caveat this leaves, worth knowing when reading a close call: shrinkage moves the PORTFOLIO's reported volatility and Sharpe ratio by several percent in a direction that depends on its own correlation structure, while leaving the benchmark's untouched, so a Sharpe gap of a couple of hundredths — such as the live USD run's 0.6675 against 0.6921 — sits inside the noise of that choice and is not decisive. A gap like the JPY run's 0.6132 against 1.5486 plainly is. This is recorded in `src/optimizer/benchmark.py`'s module docstring rather than printed on every run, since a caveat that always appears becomes noise rather than information.
  Date/Author: 2026-09-06, raised by the user on reviewing the ddof finding.

- Decision: `DEFAULT_BENCHMARKS` contains `USD -> SPY` and nothing else. Any other currency is ASKED for, once, and the answer is remembered.
  Rationale: `SPY` is the uncontested stand-in for the US market, so defaulting it costs nobody anything. There is no comparably obvious single answer elsewhere — a yen pool might reasonably be measured against TOPIX (`1306.T`) or the Nikkei 225 (`1321.T`), and those genuinely differ: over the same 60 months this project measured them at 20.09% return / 11.68% volatility and 21.59% / 17.70% respectively, a Sharpe ratio of 1.55 against 1.11. Picking one in code would quietly measure somebody's portfolio against an index they never chose, which is precisely the kind of invisible wrong answer `plans/11` was written to eliminate for currencies. Asking is cheap because the pool-editing loop is already interactive, and the answer is remembered so it is asked at most once per pool.
  Date/Author: 2026-09-06, confirmed with the user.

- Decision: A benchmark is written into `memory/candidates.json` only when it was chosen EXPLICITLY — named with `--benchmark` or typed at a prompt — never when it merely came from `DEFAULT_BENCHMARKS`.
  Rationale: The file should record decisions, not defaults. If `SPY` were written into every USD pool the first time one was run, then improving the default later (or correcting a bad one) would reach no existing pool, because each would be shadowed by a copy of the old default that nobody chose. This is the same reasoning `migrate_candidate_pools` already applies to `updated_at`, which it refuses to back-fill.
  Date/Author: 2026-09-06.

- Decision: `save_candidate_pool`'s `benchmark=None` PRESERVES whatever the pool already recorded, unlike `tickers`, which replaces.
  Rationale: Both interactive loops call `save_candidate_pool` after every accepted add/remove with no benchmark argument. Under a "None replaces" reading, the first ticker edit after choosing a benchmark would silently discard that choice. The asymmetry is documented in the function's own docstring and pinned by `test_saving_without_a_benchmark_preserves_a_previously_saved_one`. Nothing needs to clear a benchmark, because choosing one is always choosing a different one.
  Date/Author: 2026-09-06.

- Decision: A benchmark must trade in the pool's own currency, and one that does not is refused by name with the reason rather than compared anyway.
  Rationale: A cross-currency benchmark is not arithmetically broken — a monthly return is a ratio of one ticker's own prices, so the currency cancels and the number is valid. It is the COMPARISON that breaks: `SPY`'s dollar return set beside a yen portfolio's yen return differs by the period's yen/dollar move, and the report would present that exchange-rate drift as out- or under-performance. This mirrors exactly how `plans/11` treats a cross-currency candidate ticker.
  Date/Author: 2026-09-06.

- Decision: The library layer defaults to NO benchmark. `run_pipeline_against` and `run_pipeline` take `benchmark=None` by default, and only `src/flow/cli.py` applies `DEFAULT_BENCHMARKS`.
  Rationale: Resolving a benchmark can reach the network. A programmatic caller — a test, a future backtest runner — must not acquire a network fetch it never asked for merely because it called an existing function. Keeping the default off means every existing hermetic test of those functions is unaffected, and the one place that decides to spend a network request is the one place a person typed a command.
  Date/Author: 2026-09-06.

- Decision: `_run_user_provided_confirm_loop` was left untouched; the benchmark is settled by a separate `_settle_benchmark` called immediately after it.
  Rationale: The benchmark cannot be settled *before* that loop, because an empty pool has no currency until its first ticker is added and the benchmark must be measured against the currency the pool actually ended up in. Settling it inside the loop would have meant changing its return from `(pool, currency)` to a triple, churning seventeen call sites for no gain. A separate function called after it reads in the right order for the person (pool first, then what it is measured against) and leaves the existing loop and its tests alone.
  Date/Author: 2026-09-06.

- Decision: Two escape hatches, `--benchmark none` and `--no-benchmark-fetch`, which do different things.
  Rationale: `--benchmark none` prints no benchmark line at all; `--no-benchmark-fetch` prints the line but reports the benchmark unavailable rather than fetching it. The second exists because a backtest-window run against the cached historical tables previously touched the network not at all, and a report feature should not silently change that; the first exists because "I do not want this line" is a different wish from "I do not want a network call". Distinguishing "switched off" from "none available" also matters in the report: the second prints a reason, because a person seeing it may well want to fix it.
  Date/Author: 2026-09-06.

- Decision: Fixed the pre-existing `attach_nearest_price` dtype crash as part of this plan rather than leaving it.
  Rationale: This plan requires that an unusable benchmark degrade to a one-line explanation. Without the fix a typo'd `--benchmark` leaked a raw pandas `MergeError` into the report, which fails that requirement. The fix is two lines at the single shared join, is independently correct, and repairs the same crash on the candidate-ticker path where it has always been reachable.
  Date/Author: 2026-09-06.


## Outcomes & Retrospective


Delivered in full. Every candidate pool now has a benchmark, the report prints its annual return, annual volatility and Sharpe ratio over the portfolio's exact window on the line below the portfolio's own figures, and the numbers are comparable by construction rather than by hope. The test suite went from 290 to 360 passing. Five real end-to-end runs exercised the USD default, the JPY prompt including a refusal and a re-ask, `[b]` mid-session, `--benchmark none`, `--no-benchmark-fetch`, and an unresolvable ticker; `data/portfolio.duckdb` was byte-identical (md5) before and after all of them, and still contains zero `SPY` rows.

Two things went better than expected. First, treating a benchmark as a one-asset portfolio turned out to be exactly true rather than merely close, which converted "are these numbers comparable?" from an argument into an assertion — `test_a_one_asset_portfolio_equals_its_own_benchmark` is the most valuable test in this plan. Second, refusing to touch `_run_user_provided_confirm_loop` cost nothing and saved churn across seventeen call sites; the benchmark genuinely is a separate concern from editing a ticker list, and the code reads better for saying so.

One thing was worse than expected: the feature surfaced a latent crash in code this plan never intended to touch (the empty-`prices` merge), and a latent hermeticity hole in tests it did touch (four `main` tests that started calling yfinance). Both were found only by running the real thing and by noticing a test file's runtime quadrupling. The lesson is the one PLANS.md already states — a plan is not done when the code compiles and the tests are green, it is done when someone has watched it work and looked at what it cost.

What remains: no currency but USD has an assumed benchmark, by choice; `src/flow/backtest.py`'s 52-month backtest scoring has no benchmark comparison, which would be a separate and larger piece of work because its realized Sharpe ratio is a different computation from the optimizer's estimated one; and a benchmark still cannot be compared across currencies, which would require the currency conversion the whole project deliberately does not do.


## Context and Orientation


Read this section if you have never seen this repository. It describes only what this plan touches.

This project builds share portfolios. Its command-line entry point is `src/flow/cli.py`, run as `uv run portfolio` (declared in `pyproject.toml` under `[project.scripts]`). A run names a date, an optimization objective, an amount of money, and how the candidate tickers are chosen:

    uv run portfolio --date today --objective GMV --value 100000 --selection user_provided

The `--selection user_provided` mode is the one this plan matters most for: the tickers come from the person rather than from one of the project's two LLM agents. Those tickers live in `memory/candidates.json`, managed by `src/flow/candidate_memory.py`. That file holds one **pool per currency**:

    {"pools": {"USD": {"tickers": ["AAPL", "MSFT"], "updated_at": "2026-09-01T00:00:00+00:00"},
               "JPY": {"tickers": ["6758.T", "7203.T"], "updated_at": "2026-09-01T00:00:00+00:00"}}}

It is one pool per currency because a portfolio cannot mix currencies. Prices are stored exactly as Yahoo Finance quotes them, in yen for a Tokyo listing and pounds for a London one, and the share-allocation step divides a budget by a price — which is only meaningful when the budget and every price share one unit. Portfolio *weights* are immune (a monthly return is a ratio of one ticker's own prices, so the currency cancels), but allocation is not. `src/dataset/ticker_currency.py` records each ticker's trading currency in a `ticker_currency` table and refuses a ticker whose currency differs from the pool's.

Market data lives in DuckDB (a single-file SQL database; think SQLite for analytics). Two tables matter here. `prices` holds daily `close`/`adj_close` per ticker. `returns` holds one **monthly return** per ticker per month, keyed by `rebalance_date` — the first weekday of each calendar month, produced by `src/dataset/membership.py`'s `compute_rebalance_dates` with pandas' `freq="BMS"`. Note carefully that these are the first *weekday*, so `2023-01-02` and `2023-04-03`, not the 1st; nothing may reconstruct that grid with `freq="MS"`.

There are two databases in play at once, and telling them apart is essential to this plan:

- `data/portfolio.duckdb` is the shared historical cache, built once by the `portfolio-build-*` scripts. It covers the S&P 500 membership universe from 2015 to 2024. It does NOT contain `SPY`, or any other ETF — verified: `SELECT count(*) FROM returns WHERE ticker='SPY'` returns 0. Nothing in a reporting path may write to it.
- A **session database** is what a run actually reads. For a date inside the stored 2020-01-01..2024-04-30 window with an agent-driven selection, it is `data/portfolio.duckdb` itself. Otherwise — and always for `user_provided` — `src/flow/live.py`'s `build_live_snapshot` creates a throwaway temporary DuckDB file, fills it with freshly fetched data (or, for `user_provided`, with empty tables that `src/dataset/ticker_ingestion.py`'s `validate_and_ingest_tickers` fills one user-typed ticker batch at a time), and deletes it when the run ends.

The optimizer is `src/optimizer/portfolio.py`. `load_returns_matrix(tickers, as_of, lookback_months=60, min_months=24, db_path)` reads the `returns` table into a table of months (rows) by tickers (columns) and drops any ticker with fewer than 24 months of history or an internal gap. `compute_weights_and_stats(returns_matrix, objective, target_annual_return, risk_free_rate)` hands that to PyPortfolioOpt and returns a `PortfolioStats` named tuple carrying the weights, each ticker's annualized expected return and volatility, the portfolio's own expected return, volatility and Sharpe ratio, the risk-free rate, the MV target return, and `returns_window_start`/`returns_window_end`/`returns_window_months` describing the months actually used.

The statistics come from two PyPortfolioOpt calls, and this plan's whole correctness argument rests on reusing them verbatim:

    mu = expected_returns.mean_historical_return(returns_matrix, returns_data=True, frequency=12)
    cov_matrix = risk_models.CovarianceShrinkage(..., returns_data=True, frequency=12).ledoit_wolf()

`frequency=12` annualizes from the monthly cadence. `mean_historical_return`'s undeclared `compounding=True` default makes the return a compound annual growth rate. Per-ticker volatility is `sqrt` of `cov_matrix`'s diagonal.

`src/flow/interactive.py` glues it together: `open_pipeline_session` yields the session database path and the mode, `run_pipeline_against` runs the candidate selection and the optimizer and bundles everything into a result dictionary, and `compute_weights_and_allocation` is the part an interactive edit re-runs. `src/flow/cli.py` prints all of it via `print_pipeline_result` and `print_weights_and_allocation`, then enters an edit loop where a person can add or remove tickers, change the objective, or change MV's target return, each edit recomputing and reprinting.

One detail of that printing matters for this plan. The `Portfolio currency:` and `Returns window:` lines are printed inside `print_weights_and_allocation`, not in `print_pipeline_result`'s header, specifically because the edit loop calls `print_weights_and_allocation` directly — anything in the header would appear once and then never again after an edit. The benchmark line has exactly the same requirement.

Tests live in `tests/`, run with `uv run pytest tests/test_*.py`, and per `AGENTS.md` none of them may call yfinance or an LLM. The established seams to monkeypatch are `src/dataset/ticker_currency.py`'s `fetch_ticker_currencies` and `src/dataset/ticker_ingestion.py`'s `validate_and_ingest_tickers`.


## Plan of Work


The work divides into four milestones, each independently verifiable. Read them in order; each assumes the previous one exists.


### Milestone 1: the benchmark's arithmetic, with nothing else attached


At the end of this milestone a new module can turn a ticker and a date window into three annualized figures, reading a `returns` table read-only, with no network access, no CLI, and no persistence. It is verifiable entirely by `uv run pytest tests/test_benchmark.py`.

First, in `src/optimizer/portfolio.py`, promote the private `_load_returns_long(tickers, window_dates, db_path)` to a public

    def load_returns_long(tickers, window_start, window_end, db_path, read_only=False) -> pd.DataFrame

returning the same `['rebalance_date', 'ticker', 'monthly_return']` long-format rows. Three changes to its behavior. `window_end=None` returns the correctly-shaped empty frame, preserving what the old empty-`window_dates` early return did. `window_start=None` means no lower bound, which is what lets a benchmark's whole history be read in one query. And `read_only=True` opens the database read-only and converts `duckdb.IOException` (no such file) and `duckdb.CatalogException` (no such table) into an empty frame instead of raising — the same discipline `src/dataset/ticker_currency.py`'s `load_ticker_currencies` already follows, and what makes reading the shared cache provably harmless. Add a module constant `RETURNS_LONG_COLUMNS` for the column shape, since three code paths now construct the empty frame. Update the single caller, `load_returns_matrix`, to pass `window_dates[0] if window_dates else None` and `window_dates[-1] if window_dates else None`. No test references the old private name, so this is invisible churn.

Then create `src/optimizer/benchmark.py`. It must not import anything from `src/flow` and must not import yfinance: this is the arithmetic layer, and the network belongs to Milestone 2. Its contents are specified in Interfaces and Dependencies below. The two things to get right are the estimators — `mean_historical_return` and `sqrt(ledoit_wolf())` exactly as the optimizer uses them, never `series.std() * sqrt(12)` — and the fact that `benchmark_stats_for_window` returns figures or a reason, never both and never neither.

Then write `tests/test_benchmark.py`. Two of its tests are the reason the module exists in this shape and must not be dropped: one asserting that `benchmark_stats_for_window` on a series agrees with `compute_weights_and_stats(series.to_frame(), "GMV")`'s portfolio-level figures to 1e-9, and one asserting that the volatility differs from `series.std() * 12**0.5` and equals `series.std(ddof=0) * 12**0.5`.


### Milestone 2: resolving a benchmark, including fetching it, without touching the session database


At the end of this milestone a benchmark ticker can be turned into its whole return history — fetching it if necessary — and `run_pipeline_against`'s result carries a `BenchmarkStats`. Still no CLI.

In `src/flow/live.py`, extract the temporary-database discipline into

    @contextmanager
    def build_scratch_snapshot(prefix: str = "benchmark_snapshot_") -> Iterator[str]

which does exactly what `build_live_snapshot`'s `user_provided` branch already did: `tempfile.mkstemp`, `os.close`, `os.remove` the empty file it created (DuckDB must create the file itself; a zero-byte file confuses it), create the four empty tables via the existing `_init_empty_price_and_returns_tables`, yield the path, and delete it in a `finally`. Then make `build_live_snapshot`'s `user_provided` branch delegate to it and return early, so the discipline exists once.

In `src/flow/interactive.py`, add `prepare_benchmark(ticker, currency, rebalance_date, db_path, allow_fetch=True) -> BenchmarkSource`. Its order of operations is: read `db_path` first, because that read is free, offline and read-only, and it hits whenever the benchmark is already a pool member; if that yields fewer than `BENCHMARK_MIN_MONTHS` of history and `allow_fetch` is set, fetch into a `build_scratch_snapshot` database via the existing `validate_and_ingest_tickers` and read the result back out of *that*; then check the resolved currency against `currency` and refuse a mismatch by name. `ticker=None` returns an unavailable source naming `--benchmark`. The whole body is wrapped so that any exception becomes an unavailable source with a logged warning — losing a live session's freshly fetched snapshot over a benchmark would cost far more than the benchmark is worth.

Then give `run_pipeline_against` and `run_pipeline` a `benchmark: BenchmarkSource | None = None` parameter and one new result key, `"benchmark"`, holding `benchmark_stats_for_window(benchmark, stats.returns_window_start, stats.returns_window_end, risk_free_rate)`. The `None` default is load-bearing: it keeps every existing programmatic caller and hermetic test free of any network access.


### Milestone 3: remembering a pool's benchmark


At the end of this milestone `memory/candidates.json` can carry a benchmark per pool, and an old file without one still reads.

In `src/flow/candidate_memory.py` add an optional `benchmark` key to each pool entry. `_load_raw_pools` validates it through a new `_validate_benchmark(path, currency, value)` that accepts a string or `None` and otherwise raises `ValueError` naming the file and the currency, mirroring `_validate_tickers`; a blank string reads as `None`. `_write_pools` OMITS the key when it is `None`, which keeps a pool that never named a benchmark byte-identical to how it was written before benchmarks existed — that is what makes `migrate_candidate_pools` a genuine no-op on such a file, and the existing migration tests compare exact JSON payloads. `save_candidate_pool` gains `benchmark=None`, which PRESERVES what is recorded rather than clearing it; document that asymmetry against `tickers` in its docstring. Add `load_pool_benchmarks(path)` and `load_candidate_benchmark(path, currency)`. Leave `load_all_pools`, `load_candidate_pool` and `migrate_candidate_pools` returning exactly what they return today.

No migration step is needed and none should be added: a missing key reads as `None` and resolves through `DEFAULT_BENCHMARKS` at use time.


### Milestone 4: the CLI — the flag, the line, the question, and changing your mind


At the end of this milestone the feature is visible to a person.

In `src/flow/cli.py`, add `--benchmark` (default `None`; the literal string `none`, held in a new `BENCHMARK_DISABLED` constant, switches the comparison off) and `--no-benchmark-fetch`. Add `format_benchmark(benchmark, portfolio_window_months)` returning the single report line, and give `print_weights_and_allocation` a trailing `benchmark=None` keyword that prints that line between the portfolio's own figures and `Risk-free rate used:` — the trailing default keeps every existing positional call working. Have `print_pipeline_result` pass `result.get("benchmark")`, using `.get` so an existing test stub's minimal dictionary stays valid.

Add `_prompt_for_benchmark(prompt, currency, rebalance_date, db_path, allow_fetch)`, which asks until an answer is usable or blank and returns the PREPARED source — returning the source rather than the ticker means the fetch that validated the answer is the same one the report then uses, instead of a second identical round trip.

Add `_settle_benchmark(pool, currency, rebalance_date, db_path, override, enabled, allow_fetch, pool_memory_path)`, which applies the precedence (`--benchmark`, then the pool's record, then the currency default), asks when all three come up empty, and persists only an explicit choice. `pool_memory_path=None` means "an agent-chosen candidate list": nowhere to record a benchmark, nobody mid-conversation to ask, and USD by construction, so it simply takes the default.

Give `_choose_pool_to_resume` an optional `benchmarks` argument so the saved-pool listing shows each pool's benchmark. Give `_run_edit_loop` a `benchmark` and an `allow_benchmark_fetch` parameter and a `[b]enchmark` choice which prompts, keeps the previous benchmark on a refused or blank answer, and persists a successful change for `user_provided`; then let it fall through to the shared recompute so the line is reprinted. Add `[b]enchmark` to the menu string.

In `main`, settle the benchmark AFTER the confirm loop — an empty pool has no currency until its first ticker is added — and thread the result into both `run_pipeline_against` and `_run_edit_loop`. Pass `pool_memory_path=args.memory_path` only for `--selection user_provided`.

Finally, and this is not optional: add `_settle_benchmark` to `tests/test_cli.py`'s `_stub_main_pipeline`. Left real, it resolves USD's default `SPY` and goes to yfinance inside four existing tests.


## Concrete Steps


All commands are run from the repository root, `/app/agentic_portfolio`.

Establish the baseline first, so you can tell what you changed:

    $ uv run pytest tests/test_*.py -q
    290 passed in 77.97s (0:01:17)

After Milestone 1:

    $ uv run pytest tests/test_benchmark.py tests/test_optimizer.py -q
    50 passed in 5.32s

Then confirm the equality claim against real data, which needs `data/portfolio.duckdb` to have been built (if it has not, skip this and rely on `tests/test_benchmark.py`, which needs no such thing):

    $ uv run python -c "
    from datetime import date
    from src.optimizer.portfolio import load_returns_matrix, compute_weights_and_stats
    from src.optimizer.benchmark import load_benchmark_returns, benchmark_stats_for_window, BenchmarkSource
    m = load_returns_matrix(['AAPL'], as_of=date(2024,4,1), db_path='data/portfolio.duckdb')
    s = compute_weights_and_stats(m, 'GMV', risk_free_rate=0.02)
    print('one-asset portfolio :', s.portfolio_expected_return, s.portfolio_volatility, s.portfolio_sharpe)
    ser = load_benchmark_returns('AAPL', date(2024,4,1), 'data/portfolio.duckdb')
    b = benchmark_stats_for_window(BenchmarkSource('AAPL','USD',ser,None), s.returns_window_start, s.returns_window_end, 0.02)
    print('benchmark module    :', b.annual_return, b.annual_volatility, b.sharpe)
    print('sample std*sqrt12   :', float(m['AAPL'].std()*12**0.5))"

    one-asset portfolio : 0.3446037943053359 0.3129845088136446 1.0371241552360968
    benchmark module    : 0.3446037943482607 0.3129845088526308 1.0371241552440567
    sample std*sqrt12   : 0.31562577516342744

The first two lines must agree to about 1e-10. The third must differ; if it matches, the wrong volatility convention is in use.

After Milestone 2:

    $ uv run pytest tests/test_interactive_flow.py -q
    42 passed in 70.03s (0:01:10)

After Milestone 3:

    $ uv run pytest tests/test_candidate_memory.py -q
    32 passed in 0.67s

After Milestone 4:

    $ uv run pytest tests/test_cli.py -q
    73 passed in 3.03s

That runtime matters as much as the count. Before `_settle_benchmark` was stubbed the same file took 12.21s, the difference being live yfinance calls. If `tests/test_cli.py` takes more than a few seconds, something in it is reaching the network.

The `attach_nearest_price` fix from Surprises & Discoveries brings one more:

    $ uv run pytest tests/test_dataset_upsert.py -q
    18 passed in 10.03s

Then the whole suite:

    $ uv run pytest tests/test_*.py -q
    360 passed in 98.90s (0:01:38)


## Validation and Acceptance


Acceptance is behavior a person can watch, so the tests are necessary but not sufficient. Run all of the following.

**The windows genuinely coincide, and the shared cache is untouched.** This needs no network and no LLM. `AAPL` stands in as an in-cache benchmark because `SPY` is not in the cache:

    $ md5sum data/portfolio.duckdb > /tmp/before.md5
    $ uv run python -c "
    from datetime import date
    from src.optimizer.portfolio import load_returns_matrix, compute_weights_and_stats
    from src.flow.interactive import prepare_benchmark
    from src.optimizer.benchmark import benchmark_stats_for_window
    from src.flow.cli import format_benchmark
    m = load_returns_matrix(['MSFT'], as_of=date(2024,4,1), db_path='data/portfolio.duckdb')
    s = compute_weights_and_stats(m, 'GMV', risk_free_rate=0.02)
    src = prepare_benchmark('AAPL', 'USD', date(2024,4,1), 'data/portfolio.duckdb', allow_fetch=False)
    b = benchmark_stats_for_window(src, s.returns_window_start, s.returns_window_end, s.risk_free_rate)
    print('portfolio window:', s.returns_window_start, s.returns_window_end, s.returns_window_months)
    print('benchmark window:', b.window_start, b.window_end, b.window_months)
    print(f'Portfolio expected return: {s.portfolio_expected_return:.4f}  Portfolio volatility: {s.portfolio_volatility:.4f}  Portfolio Sharpe: {s.portfolio_sharpe:.4f}')
    print(format_benchmark(b, s.returns_window_months))"

    portfolio window: 2019-05-01 2024-04-01 60
    benchmark window: 2019-05-01 2024-04-01 60
    Portfolio expected return: 0.3410  Portfolio volatility: 0.2061  Portfolio Sharpe: 1.5573
    Benchmark AAPL: return=0.3446  volatility=0.3130  Sharpe=1.0371  (60 of 60 month(s))

    $ md5sum -c /tmp/before.md5
    data/portfolio.duckdb: OK

    $ uv run python -c "import duckdb; print(duckdb.connect('data/portfolio.duckdb', read_only=True).execute(\"select count(*) from returns where ticker='SPY'\").fetchone()[0])"
    0

The two window lines must be identical. The md5 must still match after every run below, and the `SPY` count must stay 0 forever.

**The USD default needs no configuration.** These runs fetch from Yahoo Finance and write to the pool file, so use a scratch copy rather than your real `memory/candidates.json` — an ordinary save replaces `updated_at`, which is the only record of when a pool was last curated:

    $ cp memory/candidates.json /tmp/candidates.json
    $ printf 'USD\nd\nf\n' | uv run portfolio --date today --objective GMV --value 100000 \
        --selection user_provided --memory-path /tmp/candidates.json

Expect a `Benchmark SPY: return=... volatility=... Sharpe=... (60 of 60 month(s))` line directly beneath the portfolio's own three figures, and no benchmark prompt. Expect `/tmp/candidates.json` to gain NO `benchmark` key for USD — a default is not a decision.

**A non-USD pool is asked, once, and refuses the wrong currency.** With a JPY pool saved and no benchmark recorded:

    $ printf 'JPY\nd\nSPY\n1306.T\nf\n' | uv run portfolio --date today --objective GMV --value 15000000 \
        --selection user_provided --memory-path /tmp/candidates.json

    No benchmark recorded for this JPY pool.
    Benchmark ticker (blank to skip): Refused: SPY trades in USD but this portfolio is JPY; comparing them would fold an exchange-rate move into the comparison
    Benchmark ticker (blank to skip): Benchmark set to 1306.T.
    ...
    Portfolio expected return: 0.1373  Portfolio volatility: 0.1913  Portfolio Sharpe: 0.6132
    Benchmark 1306.T: return=0.2009  volatility=0.1168  Sharpe=1.5486  (60 of 60 month(s))

Then confirm `/tmp/candidates.json` records `"benchmark": "1306.T"` under JPY and nothing new under USD.

**It is remembered, listed, and changeable.** Running the same JPY pool again must show the benchmark in the pool listing and ask nothing; `[b]` must change it, recompute, reprint, and persist:

    $ printf 'JPY\nd\nb\n1321.T\nf\n' | uv run portfolio --date today --objective GMV --value 15000000 \
        --selection user_provided --memory-path /tmp/candidates.json

    Saved candidate pools:
      JPY (2, benchmark 1306.T): 6758.T, 7203.T
      USD (2): AAPL, MSFT
    ...
    Benchmark 1306.T: return=0.2009  volatility=0.1168  Sharpe=1.5486  (60 of 60 month(s))
    Edit candidates? [a]dd tickers / [r]emove tickers / [o]bjective / [t]arget-return / [b]enchmark / [f]inish: New benchmark ticker (current 1306.T): Benchmark set to 1321.T.
    ...
    Benchmark 1321.T: return=0.2159  volatility=0.1770  Sharpe=1.1065  (60 of 60 month(s))

**Every degradation is a line, never a crash.** Three runs, each of which must complete normally:

    $ printf 'USD\nd\nf\n' | uv run portfolio ... --benchmark none
    Portfolio expected return: 0.1507  Portfolio volatility: 0.1958  Portfolio Sharpe: 0.6675
    Risk-free rate used: 0.0200

    $ printf 'USD\nd\nf\n' | uv run portfolio ... --no-benchmark-fetch
    Benchmark SPY: n/a - SPY has 0 month(s) of returns in this session's database and fetching more was disabled with --no-benchmark-fetch

    $ printf 'USD\nd\nf\n' | uv run portfolio ... --benchmark ZZZZ
    Benchmark ZZZZ: n/a - yfinance returned no non-null close/adj_close values for this ticker ...

The first must print no benchmark line at all. The second and third must print a line whose reason is a sentence about tickers and data, NOT a pandas or DuckDB exception message — if you see `MergeError` or `incompatible merge keys`, the `attach_nearest_price` fix described in Surprises & Discoveries is missing.


## Idempotence and Recovery


Everything here is safe to repeat. The benchmark's scratch database is created and deleted inside one `with` block, in a `finally`, so an interrupted run leaves nothing behind but a file under the system temp directory that the next reboot clears. `data/portfolio.duckdb` is only ever opened read-only on the benchmark path, so no amount of repetition can change it — the md5 check above is the recovery test, and if it ever fails, something is writing where it must not.

`memory/candidates.json` is written only when a person confirms a pool or explicitly names a benchmark, and only into the currency being worked on: `save_candidate_pool` reads the whole file first and carries every other currency over untouched, keeping its own `updated_at`. A file written before this plan needs no migration and must not be given one; `uv run portfolio-migrate-candidates --path memory/candidates.json` remains a byte-identical no-op on a file that records no benchmark, which is worth re-running to confirm. If you want to experiment without touching a curated pool, pass `--memory-path` a scratch copy, as the validation steps above do.

A benchmark that stops resolving degrades to an `n/a` line on the next run rather than breaking it, so a recorded benchmark can never wedge a session. To change one, use `[b]` or `--benchmark`; there is deliberately no way to clear one, because choosing a benchmark is always choosing a different one.


## Artifacts and Notes


The volatility-convention evidence, measured on `AAPL` over the 60 months ending 2024-04-01 from the real cache:

    one-asset compute_weights_and_stats : return 0.3446037943053359  vol 0.3129845088136446  Sharpe 1.0371241552360968
    mean_historical_return + ledoit_wolf: return 0.3446037943482607  vol 0.3129845088526308  Sharpe 1.0371241552440567
    sample std * sqrt(12)               :                            vol 0.31562577516342744

The zero-volatility trap, from the first draft of `benchmark_stats_for_window` on a constant 36-month series:

    BenchmarkStats(ticker='FLAT', ..., annual_return=0.12682503013197,
                   annual_volatility=6.009258394948637e-18, sharpe=1.7776741007137714e+16, ...)

The DuckDB dtype asymmetry behind the `attach_nearest_price` fix:

    empty prices ticker dtype: object
    non-empty prices ticker dtype: str
    grid ticker dtype: str
    empty-prices build FAILED: MergeError incompatible merge keys [0] <StringDtype(na_value=nan)> and dtype('O'), must be the same type

The JPY pool file after the prompt was answered, showing that only JPY gained a benchmark:

    {
      "pools": {
        "USD": {"tickers": ["AAPL", "MSFT"], "updated_at": "2026-09-06T02:12:12.490741+00:00"},
        "JPY": {"tickers": ["6758.T", "7203.T"], "benchmark": "1306.T",
                "updated_at": "2026-09-06T02:12:39.884200+00:00"}
      }
    }

The two candidate Japanese benchmarks, measured over the same 60 months, which is the evidence behind refusing to guess a non-USD default:

    Benchmark 1306.T (TOPIX):      return=0.2009  volatility=0.1168  Sharpe=1.5486
    Benchmark 1321.T (Nikkei 225): return=0.2159  volatility=0.1770  Sharpe=1.1065


## Interfaces and Dependencies


No new third-party dependency. Everything here uses libraries already in `pyproject.toml`: `pandas`, `numpy`, `duckdb`, and `pyportfolioopt` (imported as `pypfopt`).

In `src/optimizer/portfolio.py`, the promoted loader:

    RETURNS_LONG_COLUMNS = ["rebalance_date", "ticker", "monthly_return"]

    def load_returns_long(
        tickers: list[str],
        window_start: pd.Timestamp | date | None,
        window_end: pd.Timestamp | date | None,
        db_path: str,
        read_only: bool = False,
    ) -> pd.DataFrame

In `src/optimizer/benchmark.py`:

    DEFAULT_BENCHMARKS: dict[str, str] = {"USD": "SPY"}
    MIN_USABLE_ANNUAL_VOLATILITY = 1e-12
    BENCHMARK_MIN_MONTHS = 24

    class BenchmarkSource(NamedTuple):
        ticker: str | None
        currency: str | None
        monthly_returns: pd.Series
        unavailable_reason: str | None = None

    class BenchmarkStats(NamedTuple):
        ticker: str | None
        currency: str | None
        annual_return: float | None
        annual_volatility: float | None
        sharpe: float | None
        risk_free_rate: float
        window_start: date | None
        window_end: date | None
        window_months: int
        unavailable_reason: str | None

    def resolve_benchmark_ticker(currency: str, override: str | None = None, saved: str | None = None) -> str | None
    def empty_benchmark_returns(ticker: str | None = None) -> pd.Series
    def load_benchmark_returns(ticker: str, as_of: date, db_path: str) -> pd.Series
    def annualized_return_and_volatility(monthly_returns: pd.Series) -> tuple[float, float]
    def benchmark_stats_for_window(
        source: BenchmarkSource | None,
        window_start: date,
        window_end: date,
        risk_free_rate: float,
        min_months: int = BENCHMARK_MIN_MONTHS,
    ) -> BenchmarkStats | None

In `src/flow/live.py`:

    @contextmanager
    def build_scratch_snapshot(prefix: str = "benchmark_snapshot_")

In `src/flow/interactive.py`:

    def prepare_benchmark(
        ticker: str | None, currency: str, rebalance_date: date, db_path: str, allow_fetch: bool = True
    ) -> BenchmarkSource

plus a `benchmark: BenchmarkSource | None = None` parameter on `run_pipeline_against` and `run_pipeline`, and a `"benchmark"` key in the result dictionary holding a `BenchmarkStats | None`.

In `src/flow/candidate_memory.py`:

    def load_pool_benchmarks(path: str = DEFAULT_CANDIDATES_PATH) -> dict[str, str | None]
    def load_candidate_benchmark(path: str = DEFAULT_CANDIDATES_PATH, currency: str = DEFAULT_CURRENCY) -> str | None
    def save_candidate_pool(
        tickers: list[str],
        path: str = DEFAULT_CANDIDATES_PATH,
        currency: str = DEFAULT_CURRENCY,
        benchmark: str | None = None,
    ) -> None

In `src/flow/cli.py`:

    BENCHMARK_DISABLED = "none"

    def format_benchmark(benchmark: BenchmarkStats, portfolio_window_months: int) -> str
    def _prompt_for_benchmark(
        prompt: str, currency: str, rebalance_date: date, db_path: str, allow_fetch: bool
    ) -> BenchmarkSource | None
    def _settle_benchmark(
        pool: list[str],
        currency: str,
        rebalance_date: date,
        db_path: str,
        override: str | None = None,
        enabled: bool = True,
        allow_fetch: bool = True,
        pool_memory_path: str | None = None,
    ) -> BenchmarkSource | None

plus a trailing `benchmark: BenchmarkStats | None = None` on `print_weights_and_allocation`, an optional `benchmarks` argument on `_choose_pool_to_resume`, and `benchmark`/`allow_benchmark_fetch` parameters on `_run_edit_loop`.


## Revision Note — 2026-09-06, per-currency risk-free rates


`plans/14_per_currency_risk_free_rate.md` changed one line of every transcript in this plan. `Risk-free rate used: 0.0200` now carries the source the rate came from, as in `Risk-free rate used: 0.0050 (remembered for JPY)`, because the rate is resolved per currency from `memory/rates.json` and a bare number could not be told from an inherited dollar default. Nothing about the benchmark's arithmetic changed: `benchmark_stats_for_window` still takes `risk_free_rate` as a plain float and still measures the benchmark's Sharpe ratio against exactly the rate the portfolio's was measured against — which is now that currency's own rate rather than a global 2%, so the two figures this plan exists to put side by side remain comparable and are both finally right for a non-dollar pool.

Two of this plan's designs were reused rather than merely referenced, and the resemblance is deliberate. The rate's precedence — this run's `--risk-free-rate`, then what the currency remembered, then the configured default — is `resolve_benchmark_ticker`'s override-then-saved-then-default shape, and `resolve_risk_free_rate` is likewise a pure function taking `saved` as a parameter rather than reading a file. And the rate is written only after the run has actually produced a report, which is this plan's `_settle_benchmark` gate (`... and source.unavailable_reason is None`, writing only a benchmark that resolved) applied to a different value.

One design was deliberately NOT reused: there is no per-currency table of default rates to match `DEFAULT_BENCHMARKS`. That constant's own docstring argues `SPY` is defensible because it is the uncontested stand-in for "the US market"; a policy rate moves several times a year, so a table of them compiled into source would rot silently, which is exactly why a rate must be remembered from the user instead. The rule that constant states about `memory/candidates.json` — that a default is never written to the file, only an explicit choice, so improving the default later still reaches everything that never chose — does carry over verbatim.
