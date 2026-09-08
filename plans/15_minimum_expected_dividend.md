# Add a minimum expected-dividend constraint to portfolio optimization

This ExecPlan is a living document. The sections `Progress`, `Surprises & Discoveries`,
`Decision Log`, and `Outcomes & Retrospective` must be kept up to date as work proceeds.
This plan must be maintained in accordance with `PLANS.md` at the repository root.

This plan builds on four earlier ExecPlans, all checked into this repository and
incorporated here by reference: `plans/05_optimizer_and_allocation.md` (the GMV/MV/MSR
optimizer and share allocation), `plans/10_performance_reporting_and_target_return.md`
(reporting the figures behind the weights, `--target-return`, `--risk-free-rate`),
`plans/11_non_us_tickers_and_single_currency.md` (Yahoo suffix notation, per-ticker
currency, pence normalization), and `plans/13_user_portfolio.md` (the user's own holdings,
`portfolio-holdings`, `data/holdings.duckdb`, `whatif`). Everything needed to execute this
plan is restated below, so it can be followed without reading them.


## Purpose / Big Picture

Before this change, `uv run portfolio` optimizes with no constraints at all beyond
PyPortfolioOpt's defaults. `src/optimizer/portfolio.py` never calls `add_constraint` and
never overrides `weight_bounds`, so the only rules in force are "no short positions" and
"spend all the money". A person who needs their portfolio to *pay* them a certain amount
of cash each year has no way to say so: GMV minimizes variance, MSR maximizes the Sharpe
ratio, and either may hand back a pool of near-zero-yield growth names.

After this change they can say it, in whichever unit they think in:

    uv run portfolio --date today --objective GMV --value 100000 \
      --selection user_provided --min-annual-dividend 3000

    uv run portfolio --date today --objective GMV --value 100000 \
      --selection user_provided --min-dividend-yield 0.03

Both express one thing - a floor on the portfolio's dividend yield - and the report prints
what the result actually pays beside what it is expected to return. `uv run
portfolio-holdings` reports the same figures for the portfolio the person already owns, so
"am I already meeting my income need?" is answerable before deciding to change anything.

Two terms of art, defined once and used throughout. A **dividend** is a cash payment a
company makes to each share it has issued; the date that fixes who receives it is the
**ex-date**. A ticker's **trailing twelve-month dividend yield** is the total per-share
dividends with an ex-date in the last year, divided by the current price of one share -
so a 27.94 stock that paid 1.11 over the year yields 0.0397, or 3.97%. A portfolio's
dividend yield is the average of its holdings' yields weighted by how much money sits in
each. Requiring that average to be at least some number is a single linear inequality on
the weights, which is why PyPortfolioOpt can enforce it exactly rather than approximately,
and why it composes with all three existing objectives instead of replacing any of them.

One thing must be said plainly, because it surprises people and the report has to say it
too. This project's expected returns are estimated from the `returns` table, which is
built from `adj_close` - a **dividend-adjusted** price - so those returns are **total**
returns and already contain dividends. A dividend floor therefore adds no new source of
return. It constrains the *composition* of a return the optimizer already saw, shifting it
from price appreciation toward cash income. A portfolio's expected total return will
usually **fall** when the floor binds, because a floor can only shrink the set of
portfolios the optimizer may choose from. That is the trade being made, not a bug.


## Progress

- [x] (2026-09-07 11:40Z) Read the installed PyPortfolioOpt 1.6.0 source and settled how
      `max_sharpe`, `efficient_return` and `_solve_cvxpy_opt_problem` treat an added
      constraint. Recorded in `Surprises & Discoveries` 1, 2 and 3.
- [x] (2026-09-07 11:55Z) Numeric spike proving the constraint works under all three
      objectives, that the wrong spelling breaks MSR silently, and that an unreachable
      floor raises `OptimizationError` rather than `ValueError`.
- [x] (2026-09-07 12:05Z) Milestone 1: settled the yfinance dividend contract empirically -
      split adjustment, column shape, non-payer representation, pence handling. Recorded in
      `Surprises & Discoveries` 5 and 6.
- [x] (2026-09-07 12:20Z) Milestone 2: `src/dataset/dividends.py` with the `dividends` and
      `dividend_coverage` tables, `portfolio-build-dividends`, and
      `dividend_lookback_months` in `src/config/settings.py`.
- [x] (2026-09-07 12:30Z) Milestone 3: `load_dividend_figures` and the trailing-yield
      chain, verified against real market data for seven tickers.
- [x] (2026-09-07 12:45Z) Milestone 4: `src/optimizer/dividends.py` and the constraint in
      `_fit_efficient_frontier`, with the ceiling pre-check, both exception classes, and
      the new `PortfolioStats` fields.
- [x] (2026-09-07 13:05Z) Milestone 5: the two CLI flags with mutual exclusion and
      pre-network validation, the report lines for both the pool and the holdings,
      `[d]ividend` in the edit loop, and the `whatif` dividend delta.
- [x] (2026-09-07 13:20Z) Milestone 6 (partial): `tests/test_dividends.py` (58 tests) and
      the `tests/test_optimizer.py` block (13 tests) pass; `README.md` updated; this plan
      written.
- [x] (2026-09-07 13:45Z) Milestone 6: CLI-level tests for the report lines, the
      `[d]ividend` prompt's three input shapes, `_settle_dividend_floor`, and the revert
      path (22 tests); holdings-level tests including the wider-denominator and
      window-independence rules (6 tests).
- [x] (2026-09-07 14:00Z) Backfilled `data/portfolio.duckdb`: 14,589 dividend rows for 429
      paying tickers, 521 of 525 tickers covered, ex-dates spanning 2015-01-02 to
      2024-04-29. Reconciliation across 412 payers recorded in `Artifacts and Notes`.
- [x] (2026-09-07 14:30Z) Forced the holdings report and the pool report onto one yield
      definition after a live run showed the same ticker reporting two different yields, and
      fixed a test whose hermeticity the new ingestion call had broken. Both recorded in
      `Surprises & Discoveries`.
- [x] (2026-09-07 13:35Z) Full suite green in one invocation: `709 passed` across all 21
      test files in 368.54s, up from 590 before this work. Per-group figures are recorded in
      `Validation and Acceptance` too, for an environment that cannot hold a six-minute
      process.
- [x] Backfill `data/holdings.duckdb` via `uv run portfolio-holdings --refresh-holdings`
      (needs the holdings file, which is user state, so it is left for the user to run).
- [x] (2026-09-07 14:15Z) Wired `build_dividends_for_tickers` into
      `validate_and_ingest_tickers`, which Milestone 2 specified and the first end-to-end
      run caught as missing: a `user_provided` pool runs against a session snapshot
      database, not `data/portfolio.duckdb`, so without this every typed ticker was
      correctly but uselessly refused as having no yield.
- [x] (2026-09-08) Optional follow-up, CONFIRMED WRONG and closed by
      `plans/16_dividend_coverage_reasons.md`: this item claimed the four uncovered universe
      tickers "are ones yfinance cannot resolve at all and already appear in
      `unresolved_tickers`". Both halves are false. The four are AVB, EA, EQR and LEG; none
      of them is in `unresolved_tickers`, and each has a complete 2,346-row price history.
      yfinance resolves all four - it just no longer serves their 2015-2024 window. See
      `Surprises & Discoveries` below.
- [x] (2026-09-08) FIXED, as its own piece of work: `allocate_shares` priced shares from
      `adj_close` rather than raw `close`, over-allocating by up to 2.87x on a
      historical-window database (18.1% on a real five-name run). `load_latest_prices` now
      reads the market close through one shared loader in `src/dataset/prices.py`. See
      `plans/05_optimizer_and_allocation.md`'s revision note.


## Surprises & Discoveries

- Observation: `max_sharpe` rewrites every constraint, and one natural way to spell ours
  breaks silently under that rewrite - affecting MSR only, with no exception raised.
  `max_sharpe` solves a transformed problem in which `w = y/k` and rebuilds each existing
  constraint by homogenizing it with `k`: for an `Inequality` whose `args[0]` is a cvxpy
  `Constant`, it emits `args[1] >= args[0] * k`. With the floor **alone** on one side,
  cvxpy builds `Inequality(floor, vector @ w)`, giving `vector @ y >= floor * k`, and
  dividing by `k > 0` recovers the intended constraint. Folded into the expression, cvxpy
  builds `Inequality(0, vector @ w - floor)`, which homogenizes into
  `vector @ w >= floor / k` - a different constraint.
  Evidence: cvxpy inspection, then the solved result on a three-asset pool with yields
  `{LOWYLD: 0.002, MIDYLD: 0.025, HIYLD: 0.055}`:

        === wrong form (constant folded into the expression) under MSR ===
        MSR floor=0.030  weights=[0.5267, 0.4254, 0.048]  realized_yield=0.0143  VIOLATED!
        MSR floor=0.050  weights=[0.5267, 0.4254, 0.048]  realized_yield=0.0143  VIOLATED!

  Those are the *unconstrained* MSR weights: the floor became entirely non-binding. The
  correct form honors it exactly, under every objective:

        GMV floor=0.050  weights=[0.0215, 0.1287, 0.8498]  realized_yield=0.0500 OK
        MSR floor=0.050  weights=[0.0943, 0.0,    0.9057]  realized_yield=0.0500 OK
        MSR floor=0.030  weights=[0.3211, 0.2661, 0.4128]  realized_yield=0.0300 OK

- Observation: a dividend floor lowers the maximum attainable return, which collides with
  MV far more often than expected. `efficient_return` computes its ceiling from
  `self.deepcopy()._max_return()`, and the deepcopy includes our constraint - so the
  ceiling correctly accounts for the floor, and a `--target-return` that was fine
  yesterday becomes unreachable today purely because income was demanded. PyPortfolioOpt's
  message for that names no dividend at all.
  Evidence: on the same three-asset pool,

        max attainable annual return vs dividend floor:
          floor=none  -> max_return=0.1400
          floor=0.010 -> max_return=0.1264
          floor=0.030 -> max_return=0.0925
          floor=0.050 -> max_return=0.0585
          floor=0.055 -> max_return=0.0500

  This project's default `--target-return` is `0.12`, so on that pool any floor at or above
  roughly 1.5% makes the default MV target unreachable. Hence `_dividend_blocked_target`.

- Observation: an unreachable floor raises the wrong exception type for the interactive
  edit loop. `_solve_cvxpy_opt_problem` reports infeasibility as
  `pypfopt.exceptions.OptimizationError`, which subclasses plain `Exception`, **not**
  `ValueError` - while `src/flow/cli.py`'s edit loop catches only `ValueError` and that
  loop holds live mode's only fetched snapshot open.
  Evidence: with a floor above the ceiling, all three objectives and an all-zero-yield pool
  produced `OptimizationError: ('Please check your objectives/constraints or use a
  different solver.', 'Solver status: infeasible')`. Hence both the ceiling pre-check and
  the wrapper, and hence `DividendFloorError(ValueError)`.

- Observation: `clean_weights()` moves the realized yield slightly, so any verification
  must use a tolerance and must be one-sided.
  Evidence: raw solved yield `0.050000000000` against `clean_weights()` yield
  `0.050000190000` - a `+1.9e-7` drift that could as easily have been negative. The
  reported figure is therefore computed from the raw solved vector, exactly as
  `portfolio_performance` already does for the return/volatility/Sharpe triplet.

- Observation: Yahoo Finance's dividend amounts **are already split-adjusted** to the
  current share basis, so no split correction is needed - and this could not be determined
  from the yfinance source, which passes the amounts through untouched.
  Evidence: NVDA split 10-for-1 on 2024-06-10, and one download reports

        2024-03-05 (pre-split)  Dividends = 0.004   <- actually paid $0.04; 0.04/10 = 0.004
        2024-06-11 (post-split) Dividends = 0.010   <- actually paid $0.01, unadjusted

  Had this gone the other way, a dividend paid before a 10-for-1 split divided by a
  post-split price would have overstated a yield tenfold.

- Observation: dividends are quoted in the **same unit** as prices, minor units included,
  so the existing pence multiplier must be applied to them and applying it is correct
  rather than a double-scaling.
  Evidence: `BARC.L` reports a close of `264.75` with dividends of `5.3` and `2.9` - all
  pence, giving Barclays' real ~3.1% yield. yfinance carries its own pence heuristic for
  dividends at `scrapers/history.py:1100-1113`, but it sits in a currency-*repair* path
  this project never enables.

- Observation: `actions=True` cannot safely be added to the existing price fetch, even
  though it costs no extra network call. yfinance requests dividend events on every call
  regardless and `actions` only decides whether the parsed columns are *dropped*, so the
  flag changes no price value - but the all-nan-or-zero row cleanup that runs immediately
  afterwards builds its column list from whichever columns are present, so with
  `actions=True` a row carrying a dividend and no usable price is **kept** where it would
  otherwise be dropped. That can change the row set of the download that populates the
  1.2-million-row `prices` table.
  Evidence: `yfinance/scrapers/history.py:523-530`. A same-window comparison found no
  actual difference (168 rows either way, identical index, zero price diff), but the code
  path exists, so the dividend fetch is kept separate and `prices.py:_fetch_batch` is
  untouched. One extra pass over the ticker list is a small price for that table being
  provably unable to shift.

- Observation: the fetched dividends can be reconciled against data already on disk, with
  no network call. `plans/01_dataset.md` records that yfinance's `Close` is always
  split-adjusted regardless of `auto_adjust`, so in this project's `prices` table `close`
  and `adj_close` differ **only** by dividend adjustment - making `adj_close / close` the
  cumulative dividend-reinvestment factor.
  Evidence: over the last twelve months of the shipped `data/portfolio.duckdb`,

        T      total=+0.0320 price=-0.0368  implied_div_return=0.0715
        XOM    total=+0.0472 price=+0.0110  implied_div_return=0.0358
        KO     total=-0.0021 price=-0.0329  implied_div_return=0.0319
        AAPL   total=+0.0280 price=+0.0225  implied_div_return=0.0053
        NVDA   total=+2.1635 price=+2.1625  implied_div_return=0.0003
        GOOGL  total=+0.5479 price=+0.5479  implied_div_return=-0.0000

  Those match each company's real yield for the period, and GOOGL - which paid no dividend
  before mid-2024 - comes out at exactly zero, so the technique cleanly identifies a
  genuine non-payer. It is a real check, not a rough one.

- Observation: the two quantities above are close but deliberately not equal, and the gap
  can reach 20% relative. The reconciliation measures each payment against the price
  prevailing when it was paid and compounds reinvestment; a trailing yield sums nominal
  cash and divides by one final price. They diverge for a ticker whose price moved a lot.
  Evidence: on a 2024 build, T's trailing yield came to 0.0488 against an implied 0.0619 -
  T's price rose sharply that year, so its early payments were made against much lower
  prices. Hence `reconcile_trailing_yield`'s docstring, and hence the unit test builds its
  fixture *forwards* from a known dividend and asserts an exact match, rather than
  comparing the two definitions against live data.

- Observation: adding a network call to an existing chain silently broke a test's
  hermeticity, and the try/except that keeps the ingestion robust is exactly what hid it.
  `tests/test_ticker_ingestion.py::test_validate_and_ingest_tickers_ingests_prices_then_currency_then_returns`
  patches its collaborators one by one rather than through the file's `_patch_chain`
  helper, so the newly-wired `build_dividends_for_tickers` ran for real - reaching Yahoo
  Finance, and creating the relative `fixture.duckdb` that test names right in the
  repository root. The test still passed, because the swallowed failure is by design.
  Evidence: the file appeared in `git status` containing only the two new tables, and
  bisecting the module named that single test. After patching that seam too, the module's
  runtime fell from 3.44s to 0.68s - the gap was the network round trip - and the
  repository root stayed clean.
  Two lessons. When a function gains a collaborator, every test that patches collaborators
  individually rather than through a shared helper has to be revisited; a helper is not
  enough on its own. And a deliberately-swallowed exception needs a test that the swallowing
  happened, not just a test that the caller survived - which is why
  `test_validate_and_ingest_tickers_survives_a_failing_dividend_fetch` patches the seam to
  raise rather than letting a real failure stand in for one.

- Observation: the same pre-existing `adj_close` valuation basis made the holdings report
  print a yield about 12% above the pool report's for the SAME ticker, until the two were
  forced onto one definition.
  Evidence: the first working `portfolio-holdings show` printed `KO ... yield 0.0328`,
  `VZ ... yield 0.0734`, `T ... yield 0.0719`, against `0.0307`, `0.0623` and `0.0635` for
  the identical tickers in the pool report - because the per-holding yield was computed as
  `annual_dividends / market_value` and market values come from `load_latest_prices`
  (`adj_close`), while the dividend layer divides by the raw close. `README.md` promises
  that "a portfolio holding only `SPY` reports exactly the numbers the `Benchmark SPY` line
  above it does", so one ticker showing two yields was a real defect rather than a cosmetic
  one. Fixed by carrying the dividend layer's own per-holding yields on `DividendFigures`
  and printing those, and by defining the portfolio-level figure as the market-value-weighted
  average of them - the same `sum(w * y)` an optimized pool uses. After the fix the report
  reconciles line to line: `0.3807*0.0307 + 0.3604*0.0623 + 0.2589*0.0635 = 0.0506`, which
  is exactly what it now prints. The accepted residual is that
  `yields[t] * market_values[t]` no longer equals `annual_dividends[t]` exactly, since the
  two rest on different price columns; that is the lesser evil and it is documented where it
  lives.

- Observation: the whole-share allocation's dividend total sits about 12% above the
  continuous portfolio's on the shipped database, and almost none of that is whole-share
  rounding - it is the pre-existing allocation step pricing shares from `adj_close` while a
  yield is cash over raw `close`.
  Evidence: a real run over `T, VZ, KO, XOM, PG` at 2024-04-01 printed
  `Annual dividend income: $3,707.91 USD` against
  `Annual dividends at these share counts: $4,161.58 USD`, with only $11.94 of leftover
  cash - far too little rounding to explain a $453 gap. The two price columns on that date:

        ticker      close  adj_close   ratio
        PG         160.58     150.27  1.0686
        VZ          42.28      35.91  1.1775
        KO          60.68      56.89  1.0666
        XOM        116.99     108.67  1.0765
        T           17.50      15.47  1.1310

  The weighted average of those ratios is about 1.12, which is the whole gap.
  `allocate_shares` takes its prices from `load_latest_prices`, which reads `adj_close`, so
  on a database whose window ended two years before it was fetched it buys about 12% more
  shares than the money would really purchase at market prices - and this line, being an
  honest `sum(shares * dividends_per_share)`, inherits that. This is PRE-EXISTING behavior
  that `plans/13_user_portfolio.md` justified on the grounds that the newest `adj_close`
  equals the newest raw close, which holds for a cache fetched up to today and not for a
  historical window. This plan deliberately did not change it - allocation and holdings
  valuation must price against the same column as each other, and changing it would move
  existing numbers - but the new line makes it visible, so both the code comment and
  `README.md` name the real cause rather than calling it rounding. Worth raising as its own
  piece of work.

- Observation: concurrent yfinance use contends on a shared SQLite cache, so a
  whole-universe dividend build can partially fail - and the `dividend_coverage` table is
  what makes that safe rather than silently wrong. Running
  `uv run portfolio-build-dividends` while the test suite was also exercising yfinance
  produced `OperationalError('database is locked')` for about 200 of the 525 tickers.
  Evidence: after that partial build, the affected tickers reported honestly rather than as
  non-payers, which is the whole point of the coverage table:

        T       yield=0.0653  dps=1.1120
        KO      yield=0.0301  dps=1.8650
        XOM     yield=0.0311  dps=3.7200
        WMT     n/a  (no dividend data has been fetched for it)
        JPM     n/a  (no dividend data has been fetched for it)
        BRK.B   yield=0.0000  dps=0.0000

  `WMT` and `JPM` are real payers whose fetch failed; `BRK.B` is a genuine non-payer whose
  fetch succeeded. Had coverage been inferred from the presence of price rows, all three
  would have reported an identical, confident `0.0000`. Re-running the build filled the
  gaps (248 payers, then 395, then 429) with no duplicated rows, because the upsert is
  keyed on the requested tickers. Practical note for an operator: do not run a
  whole-universe build concurrently with anything else that uses yfinance.

- Observation: `adj_close` is the wrong denominator for a yield, and measurably so on this
  project's own shipped data - which contradicts an assumption `plans/13_user_portfolio.md`
  relies on elsewhere.
  Evidence: `data/portfolio.duckdb` holds prices through 2024-04-29 but was fetched later,
  so subsequent dividends have back-adjusted even its newest row: AAPL closes at `173.50`
  there with an `adj_close` of `171.78`. `plans/13`'s "the newest `adj_close` equals the
  newest unadjusted close" holds only when the fetch window ends at roughly today, which is
  true for `data/holdings.duckdb` and false here. Using `adj_close` would inflate every
  historical-mode yield by about 1%. `load_latest_close` therefore reads raw `close`, while
  `load_latest_prices` is deliberately left alone - allocation and holdings valuation must
  price against the same column as each other, and for their dates the two agree.


- Observation (2026-09-08, while confirming the `Progress` follow-up above): a ticker can
  have no dividend coverage while resolving perfectly well, and this project had no way to
  say so. The four uncovered universe tickers are **AVB, EA, EQR and LEG** - obtained as the
  set difference between `prices`' 525 distinct tickers and `dividend_coverage`'s 521 rows,
  not from any recorded list.

  What is actually wrong with them: `_fetch_batch(['AVB','EA','EQR','LEG'], '2015-01-01',
  '2024-04-30')` returns a 0-row frame carrying `Close` columns and no `Dividends` column,
  with Yahoo answering each symbol `Data doesn't exist for startDate = 1420088400, endDate =
  1714449600`. Probed over the widest window the same symbols return only a short, already
  stale recent history - AVB 27 rows, EQR 15, EA 6, LEG 5, every one of them starting
  2026-07-17 - while MSFT returns 4,194 rows to 2026-09-04. Batch position rules out a
  batch-level failure: the four sit mid-batch in three different batches (indices 45, 152,
  168 and 281 of 525, at `price_batch_size` 100). So the symbols resolve, the requested
  window does not exist at the source, and `fetched_dividend_tickers` recorded no coverage
  correctly.

  Why it mattered more than a missing row: every message the project printed for them
  recommended the one action that cannot work. `trailing_dividend_yields` said "no dividend
  data has been fetched for it" and `dividend_yield_vector`'s refusal said "build its
  dividend history" - a full 525-ticker fetch that ends exactly where it started. And
  `dividend_yield_vector` refuses an *entire* floored run when any of the four is in the
  pool, which for LEG is the worst possible loss: its real trailing yield over the window is
  0.1004, higher than MO's 0.0885, making it the best payer in the universe.

  Their real figures, recovered from the stored `close`/`adj_close` pair for evidence only -
  see `plans/16_dividend_coverage_reasons.md` on why that recovery was deliberately NOT
  turned into stored data: LEG 0.1004, EQR 0.0408, AVB 0.0348, EA 0.0059.

## Decision Log

- Decision: offer two mutually exclusive flags, `--min-annual-dividend` (cash) and
  `--min-dividend-yield` (a decimal yield), rather than only one.
  Rationale: cash is how someone with an income need states it and `--value` is already
  required, so the conversion is exact; a yield is what the solver consumes and is
  independent of portfolio size. Both reduce to one linear constraint, so supporting both
  costs one division. Naming both at once is refused rather than reconciled, because any
  reconciliation - `max`, `min`, last-one-wins - silently discards a number the user typed.
  argparse's `add_mutually_exclusive_group` enforces this before any of this project's code
  runs, and `_settle_dividend_floor` repeats the refusal for a programmatic caller.
  Date/Author: 2026-09-07, decided with the user.

- Decision: estimate each ticker's yield as its trailing twelve-month figure - actual
  per-share dividends with an ex-date in the window, over the latest price - with the
  window configurable via `dividend_lookback_months`.
  Rationale: it is derived entirely from real data this project can store and re-derive,
  it can be reconciled against the price adjustment already on disk, and it is the standard
  meaning of "dividend yield". The alternatives were a multi-year average, which smooths
  irregular payers but lags a dividend cut, and Yahoo's own forward `dividendYield` field,
  which is an opaque vendor number that cannot be recomputed or audited and needs one
  request per ticker. The report states the estimator is a record of what was paid, not a
  promise of what will be.
  Date/Author: 2026-09-07, decided with the user.

- Decision: apply the constraint to all three objectives and to the interactive edit loop,
  report the figures for the user's own holdings and in `whatif`, and leave
  `portfolio-backtest` entirely alone.
  Rationale: a floor is orthogonal to an objective, so excluding one would be arbitrary.
  The holdings report is where the motivating question actually gets answered. The backtest
  is excluded because `README.md` documents its published figures as fixed, and because it
  would need dividend history back to 2015 for all 525 universe tickers for no gain.
  Date/Author: 2026-09-07, decided with the user.

- Decision: refuse an unreachable floor, naming the ceiling and the ticker that sets it,
  rather than falling back to an unconstrained solution with a warning.
  Rationale: this project fails loudly elsewhere rather than quietly returning something
  else, and a portfolio silently ignoring a stated income requirement is exactly the kind
  of answer a person would act on wrongly. Because weights are long-only and sum to one,
  the maximum attainable portfolio yield is *exactly* the highest-yielding ticker's, so the
  ceiling is cheap to compute and precise enough to name.
  Date/Author: 2026-09-07, decided with the user.

- Decision: leave `compute_weights`' signature completely unchanged.
  Rationale: it exists for one caller, `src/flow/backtest.py`, whose 52-month published
  figures must not move. Adding two optional parameters would create a wire nobody should
  ever connect; leaving the signature alone makes "the backtest cannot acquire a dividend
  floor" a property of the type system rather than a promise in a docstring, and gives a
  one-line structural test stronger than any numerical regression check.
  Date/Author: 2026-09-07.

- Decision: distinguish a confirmed non-payer (present in the yields dict with exactly
  `0.0`) from a ticker whose dividend data is missing (absent from the dict, named with a
  reason), and never collapse the second into the first.
  Rationale: this is the one silently wrong answer the feature could produce. Substituting
  zero for an unknown is not the conservative choice it appears to be: the reported ceiling
  would then be a function of a fabricated number, so a refusal could name the wrong ticker
  and quote a ceiling that does not exist, and a genuinely feasible pool could be refused
  because the one ticker with no data happened to be its best payer. Under a floor the
  unknown is refused by name; with no floor the figure is reported against its own stated
  denominator, following `plans/13`'s rule that withholding a correct answer about 98% of a
  portfolio over a marginal holding is less useful, not more careful.
  Date/Author: 2026-09-07.

- Decision: add a `dividend_coverage` table rather than inferring coverage from the
  presence of price rows.
  Rationale: the `dividends` table cannot answer "was this ticker ever fetched?" by itself,
  since a real non-payer and an unfetched ticker both have zero rows. Because this plan
  fetches dividends separately from prices, a database built before this feature - which is
  every existing one - has prices for the whole universe and dividends for none; inferring
  coverage from prices there would report all 525 tickers as paying exactly nothing. The
  coverage table makes that state an honest, fixable gap instead. It is the same job
  `unresolved_tickers` does for prices.
  Date/Author: 2026-09-07, after the data-layer design review.

- Decision: keep the dividend fetch in its own function rather than turning on
  `actions=True` in `src/dataset/prices.py`'s `_fetch_batch`.
  Rationale: see `Surprises & Discoveries` - the flag cannot change a price value but can
  change the row set, and the whole premise of this plan is that existing numbers do not
  move. The cost is one extra pass over the ticker list, paid only when dividends are
  built.
  Date/Author: 2026-09-07.

- Decision: measure the holdings' dividend figures over every *priced* holding, a wider set
  than the return/volatility/Sharpe figures cover.
  Rationale: a dividend yield needs no return history at all - it is trailing cash per
  share over the latest price - so a holding excluded from the risk figures for having
  under 24 months of monthly returns still pays what it pays, and leaving it out would
  understate the portfolio's real income. `DividendFigures` carries its own `value_covered`
  denominator so the report states the base each figure sits on. This is the same "two
  honest denominators, with the gap printed" split `plans/13` already made between
  `total_value` and `weights`.
  Date/Author: 2026-09-07.

- Decision: in the `[d]ividend` prompt, read a bare number below 1 as a yield and 1 or
  above as cash, treat a trailing `%` as a yield outright, and echo the reading back.
  Rationale: one prompt has to accept both units the flags accept. Nobody asks for a 100%
  dividend yield or for thirty cents of annual income, so magnitude separates the two
  unambiguously in practice - the same reasoning `validate_risk_free_rate` already uses to
  catch a mistyped percentage. Echoing the interpretation back is required because it is an
  inference the code makes and the person must be able to check it.
  Date/Author: 2026-09-07.


## Outcomes & Retrospective

At the end of Milestone 5 the feature works end to end on real market data. Measured on a
six-ticker income pool (`T`, `VZ`, `KO`, `XOM`, `AAPL`, `MSFT`) priced to 2025-09-01 under
`--objective GMV`:

    no floor      yield=0.0269  ret=0.1420  vol=0.1188
    $3,000/yr     yield=0.0300  ret=0.1327  vol=0.1197
    yield 5%      yield=0.0500  ret=0.0711  vol=0.1601
                  weights: VZ 0.6295, KO 0.2124, XOM 0.1210, T 0.0268, MSFT 0.0104
    yield 40%     REFUSED: the highest-yielding candidate is VZ at 0.0613

That is the intended behavior in every line: a 3% floor binds exactly and costs about a
point of expected return; a 5% floor binds exactly and pushes 63% of the money into the
pool's genuine best payer at a real cost in both return and volatility; an impossible floor
is refused by name. The trade the second bullet of `Purpose / Big Picture` describes is
visible as a number rather than asserted.

The interactive path and the holdings path were exercised live against the backfilled
database, and both behave as specified. `[d]ividend` accepting `6%` produced a portfolio
yield of exactly `0.0600` reported as `binding, the portfolio sits on the floor`; a
subsequent `0.20` was refused with `the highest-yielding candidate is T at 0.0635` and the
session continued with the previous floor intact. `portfolio-holdings whatif` raising a VZ
position from 300 to 900 shares reported `Dividend change: yield +0.0049  annual income
+$1,581.60 USD`, and then `[w]indow 36` changed every risk delta (`return -0.0167` became
`-0.0204`) while leaving that dividend delta byte-identical - the window-independence
property demonstrated rather than asserted.

What remains: the backfill of `data/holdings.duckdb`, which depends on user state and is
one `--refresh-holdings` away. Lessons worth carrying forward - the most dangerous defect in this work was not
a crash but a *silent* one, the `max_sharpe` constraint rewrite, and it was found only by
reading the library's source rather than its documentation and then measuring the solved
result. Every subsequent design choice that looked like extra ceremony (the ceiling
pre-check, the exception wrapper, the coverage table, the raw-`close` denominator) turned
out to defend against a failure that was already reachable, and three of the four were
found by reading either the dependency's source or this project's own shipped data.

The second lesson is that running the thing end to end found two defects no unit test did,
both of the same shape: a figure that was locally correct and globally inconsistent. The
`user_provided` path had no dividends at all because `validate_and_ingest_tickers` writes
to a session snapshot rather than to `data/portfolio.duckdb`, and the holdings report
printed a different yield for the same ticker than the pool report did, because the two
divided by different price columns. Both were caught by looking at real output, and both
were only *visible* because the design insists a missing yield is refused rather than
silently zeroed. A quieter design would have printed plausible numbers in both cases.


## Context and Orientation

The reader needs five files. Every path is from the repository root.

`src/optimizer/portfolio.py` holds the optimizer. `_fit_efficient_frontier` is the single
place the GMV/MV/MSR choice is dispatched: it builds `EfficientFrontier(mu, cov_matrix)`
from annualized expected returns and a Ledoit-Wolf shrunk covariance matrix (both produced
by `_estimate_mu_and_cov`, documented as the only place in the project those two estimates
are made), then calls `min_volatility()` for GMV, `max_sharpe(risk_free_rate=...)` for MSR,
or `efficient_return(target_return=...)` for MV. Two public wrappers sit over it.
`compute_weights` is used only by `src/flow/backtest.py` and fits MSR at a hardcoded
`risk_free_rate=0.0` so published backtest figures cannot move. `compute_weights_and_stats`
is used by the interactive CLI, fits at the session's resolved rate, and returns a
`PortfolioStats` named tuple carrying the weights plus every figure needed to explain them.
`apply_min_history_rule` drops any ticker with fewer than 24 months of monthly returns, or
with an internal gap, so the matrix the optimizer sees is a *subset* of the requested
candidates - which matters a great deal below.

`src/dataset/prices.py` fetches prices. `_fetch_batch` is documented as the only function
in that module performing network I/O and calls `yf.download(..., auto_adjust=False)`, so
the `prices` table stores both `close` and `adj_close`. Every module in `src/dataset/`
follows the same three-layer shape, mandated by `AGENTS.md`: one network function, pure
transform functions with no I/O, and DuckDB persist functions. `to_yfinance_symbol` maps
`BRK.B` to `BRK-B` while leaving exchange suffixes like `7203.T` and `BARC.L` alone; this is
a fetch-boundary concern only, and every stored table is keyed by the original ticker.

`src/dataset/ticker_currency.py` records what currency each ticker trades in.
`normalize_currency` turns Yahoo's `GBp` into `('GBP', 0.01)`, and
`apply_price_multipliers` scales `close` and `adj_close` by that factor before storage so
every consumer sees one unit per ticker. `MixedCurrencyPoolError` is a `ValueError`
subclass, deliberately, so the interactive edit loop's handler reverts a cross-currency
edit rather than crashing.

`src/flow/cli.py` is the `uv run portfolio` entry point: an argparse block, report
functions that `print()` to stdout, and `_run_edit_loop`, which prompts for
add/remove/objective/target-return/benchmark/finish and recomputes after each accepted
edit. That loop catches `ValueError` and reverts, because in live mode it holds the only
fetched snapshot open - letting an exception escape would throw away the fetched data and
the user's confirmed pool. `format_risk_free_rate` sets a convention this plan follows
twice over: a figure is printed with its provenance in parentheses, because "a rate printed
without its source is exactly what the per-currency bug looked like".

`src/optimizer/holdings.py` and `src/flow/holdings_cli.py` handle the portfolio the user
actually owns. `HoldingsStats` follows an all-or-nothing discipline - either all six of the
window and figure fields are populated, or all six are `None` with an actionable
`unavailable_reason`. `holdings_stats` measures only holdings clearing two bars, a usable
price and 24 months of returns, and normalizes `weights` over just those while
`total_value` counts every priced holding.

Nothing in the project stored any dividend data before this plan: a search for `dividend`
or `actions` across `src/` returned nothing.


## Plan of Work

### Milestone 1 - Settle the yfinance dividend contract

Nothing can be designed until two questions are answered, because both decide real
arithmetic and neither is readable from the yfinance source: whether the `Dividends` amounts
are split-adjusted, and what the returned block actually looks like. yfinance itself applies
no split arithmetic to dividends - it merges Yahoo's events payload in untouched - so the
answer depends on what Yahoo sends.

Settle it with one scripted call against a ticker that split recently and pays a real
dividend, and check the pence case at the same time. What exists at the end of this
milestone is a written answer with a pasted transcript, and a test that encodes it. The
findings are in `Surprises & Discoveries` 5 and 6; the transcript is in `Artifacts and
Notes`.

### Milestone 2 - Dividend history in the dataset layer

Create `src/dataset/dividends.py` following the three-layer discipline: `_fetch_batch` is
its own `yf.download(..., actions=True)` call and the only network function;
`reshape_dividends_long`, `apply_dividend_multipliers`, `coverage_frame`,
`fetched_dividend_tickers`, `trailing_dividends_per_share`, `trailing_dividend_yields` and
`reconcile_trailing_yield` are pure; `write_dividends_tables` and `upsert_dividends_tables`
are the only DuckDB writers. Add `portfolio-build-dividends` to `pyproject.toml` alongside
the existing `portfolio-build-*` scripts, and `dividend_lookback_months: int = 12` to
`src/config/settings.py`.

Store raw per-ex-date rows, never a precomputed yield: `dividends(ex_date DATE, ticker
VARCHAR, amount DOUBLE)`. A yield depends on the as-of date and the lookback window, both
run-time choices - and `data/portfolio.duckdb` is read at 52 different rebalance dates by
backtest mode, so one stored yield would be wrong for 51 of them. Alongside it,
`dividend_coverage(ticker VARCHAR, fetch_start DATE, fetch_end DATE)`, whose only job is to
record what the fetch attempted; see the Decision Log for why this is not optional.

Reuse rather than reinvent: `_build_symbol_map` for the fetch boundary, the
`upsert_prices_tables` delete-then-insert-by-ticker idiom for persistence (keyed on the
*requested tickers*, not the frame's content, so a former payer's stale rows are cleared
when it suspends its dividend), and the `price_multiplier` already recorded in
`ticker_currency` so a pence dividend becomes a pound dividend exactly as a pence price
does. Wire `build_dividends_for_tickers` into `validate_and_ingest_tickers` so a
user-provided ticker gets dividend rows on the pass that already fetches its prices,
currency and returns - without that, every user-provided pool has prices and no yields.

Acceptance: `uv run portfolio-build-dividends` populates both tables, is safe to re-run,
and the amounts match reality for a handful of known payers.

### Milestone 3 - Per-ticker trailing yield

`load_dividend_figures(tickers, as_of, db_path)` returns `(yields, dividends_per_share,
unavailable)` and is the one function a consumer calls. The window is half-open on the left
- `as_of - lookback_months < ex_date <= as_of` - so a payer on an annual schedule cannot
have two payments counted in one twelve-month window on the single run whose date lands on
an anniversary.

Two details are easy to get wrong and both are load-bearing. The denominator is raw
`close`, via a new `load_latest_close`, not the `adj_close` that `load_latest_prices` reads
- see `Surprises & Discoveries` for the measured 1% error on this project's own data. And a
key is present in `yields` only when the yield can be relied on, including exactly `0.0`
for a confirmed non-payer, while a ticker with missing data is absent and named in
`unavailable`.

### Milestone 4 - The constraint

Create `src/optimizer/dividends.py`: pure, importing nothing from `src/flow` or
`src/dataset`, holding `DividendFloor`, `DividendFloorError(ValueError)`,
`DividendYieldUnavailableError(ValueError)`, `validate_dividend_yield`,
`validate_min_annual_dividend`, `dividend_yield_vector`, `dividend_yield_ceiling`,
`check_dividend_floor_feasible`, `portfolio_dividend_yield`, `covered_dividend_yield`,
`DividendFigures` and `dividend_figures`.

Then thread an optional floor through `_fit_efficient_frontier` and
`compute_weights_and_stats`, leaving `compute_weights` alone. With no floor, not one line
of solver input changes. With a floor: build the yield vector aligned to `mu.index`,
pre-check the ceiling, add the constraint **before** the objective call in the
constant-alone form, wrap `OptimizationError`, and re-raise `efficient_return`'s
target-unreachable `ValueError` with a message that names the dividend floor. Extend
`PortfolioStats` with seven defaulted fields and verify the solved result one-sidedly.

### Milestone 5 - Flags, reporting, and the two loops

Add the two flags in an argparse mutually exclusive group, validated before any network
work in the manner `validate_risk_free_rate` establishes - argparse's `type=float` accepts
`nan` and `inf`, so this is the only place they can be caught. `_settle_dividend_floor`
builds the `DividendFloor` once the pool's currency is known, because the floor's value is
currency-independent but the sentence describing it is not.

Print the figures following both house conventions: provenance in parentheses, and the
floor line on every run saying so explicitly when none was asked for. Thread the floor
through `compute_weights_and_allocation` - reloading the yields on every recompute, never
carrying them, since `[a]dd` can introduce a ticker nobody has a yield for - and add
`[d]ividend` to the edit loop with a four-member revert snapshot. Load the holdings'
dividends at `_holdings_stats_excluding`, the one choke point every holdings path passes
through, and add the signed delta to `whatif`.

### Milestone 6 - Tests, README, and this plan

Tests in the established style: pytest with `monkeypatch`/`MagicMock`, hand-built pandas
fixtures, `tmp_path` DuckDB files, no network. The list that matters most is in
`Validation and Acceptance`.


## Concrete Steps

Run everything from the repository root, `/app/agentic_portfolio`.

Build the dividend history for the shipped universe database, then again to prove the
build is idempotent:

    uv run portfolio-build-dividends
    uv run portfolio-build-dividends

Expect a line of the shape:

    Wrote 31847 dividend row(s) for 431 paying ticker(s) to data/portfolio.duckdb.

Refresh the holdings cache, which now fetches dividends on the same pass:

    uv run portfolio-holdings --refresh-holdings

Run the suite:

    uv run pytest tests/test_*.py

Then exercise the behavior. A floor low enough to be non-binding must leave the weights
identical to a run without it; a floor high enough to bind must visibly move weight toward
the higher-yielding names and print an income at or above what was asked:

    uv run portfolio --date today --objective GMV --value 100000 \
      --selection user_provided --min-annual-dividend 3000

Expect, among the existing figures:

    Dividend yield / annual income (trailing 12 months):
      VZ: yield=0.0613  $3,858.24 USD
      KO: yield=0.0287  $609.68 USD
      AAPL: yield=0.0040  $39.42 USD

    Portfolio dividend yield: 0.0300  Annual dividend income: $3,000.00 USD (on --value $100,000.00 USD)
    Minimum dividend yield: 0.0300 (--min-annual-dividend $3,000.00 USD / --value $100,000.00 USD) - binding, the portfolio sits on the floor

An impossible floor must be refused by name:

    uv run portfolio --date today --objective GMV --value 100000 \
      --selection user_provided --min-dividend-yield 0.40

    --min-dividend-yield asks for a portfolio dividend yield of at least 0.4000, but the
    highest-yielding candidate is VZ at 0.0613 - because weights are long-only and sum to
    1, no combination of these candidates can yield more than its single best-yielding
    member. Lower the floor to at most 0.0613, or add a higher-yielding candidate.

A mistyped percentage must be refused before any network work happens:

    uv run portfolio --date today --objective GMV --value 100000 --min-dividend-yield 3

    portfolio: error: --min-dividend-yield must be a decimal yield below 0.25, got 3.0;
    yields are decimals here, so 3% is 0.03 - and no long-only portfolio of ordinary
    equities sustains a 25% trailing yield, so this is almost certainly a percentage typed
    as a number

And the two flags together must be refused by argparse itself:

    portfolio: error: argument --min-dividend-yield: not allowed with argument --min-annual-dividend


## Validation and Acceptance

Run `uv run pytest tests/test_*.py` and expect every test to pass. Before this work the
suite was 590 tests; it is 709 after it, and all 709 pass. The additions are
`tests/test_dividends.py` (60 written, expanding to 62 collected through parametrization)
plus new blocks in `tests/test_optimizer.py`, `tests/test_cli.py`, `tests/test_holdings.py`
and `tests/test_ticker_ingestion.py`.

Verified in a single invocation:

    uv run pytest tests/test_*.py
    709 passed, 3 warnings in 368.54s (0:06:08)

The three warnings are pre-existing `Some returns are NaN` notices from PyPortfolioOpt on
fixtures that deliberately contain gaps; they were there before this work.

A practical note on running it: `tests/test_holdings.py` accounts for about six of those
minutes on a cold cache because its tests each run a real optimization, so it is worth
running in file groups if the environment kills long-lived processes. Verified group by
group as well:

    tests/test_backfill_snapshot.py tests/test_benchmark.py tests/test_candidate_memory.py
    tests/test_candidate_scanner.py tests/test_dataset.py tests/test_dataset_upsert.py
    tests/test_dividends.py tests/test_external_screen.py     -> 209 passed in 62.83s

    tests/test_cli.py tests/test_optimizer.py tests/test_ticker_ingestion.py
    tests/test_ticker_currency.py tests/test_rate_memory.py
    tests/test_user_portfolio.py                              -> 279 passed in 9.17s

    tests/test_holdings_cache.py                              ->  16 passed in 19.35s
    tests/test_holdings.py                                    ->  51 passed in 368.94s
    tests/test_holdings_cli.py                                ->  78 passed in 43.36s

    tests/test_interactive_flow.py tests/test_llm_f.py tests/test_llm_s.py
    tests/test_news_archive.py                                ->  76 passed in 142.15s

The tests that carry the design, each of which fails before the corresponding change and
passes after:

`test_the_dividend_floor_survives_max_sharpes_variable_substitution` asserts that a
binding floor is met under MSR specifically. Written with the constant folded into the
expression instead, the solver returns the unconstrained MSR weights and this test fails
while every other objective still passes - which is the whole reason it exists.
`test_the_dividend_constraint_puts_the_floor_alone_on_one_side` pins the same thing
structurally, by asserting the built `Inequality`'s left-hand constant is the scalar floor
rather than zero; both spellings produce a `Constant` there, so only its value distinguishes
them.

`test_compute_weights_and_stats_without_a_floor_reproduces_the_unconstrained_weights`,
parametrized over all three objectives, asserts that supplying yields changes what is
reported and nothing about what is solved.
`test_supplying_yields_without_a_floor_adds_no_constraint` counts `add_constraint` calls to
prove it. `test_compute_weights_signature_cannot_express_a_dividend_floor` asserts
`compute_weights`' parameter list exactly, so the backtest path cannot acquire a floor even
by accident.

`test_a_floor_above_the_ceiling_is_refused_before_the_solver_runs` asserts both the message
naming the ticker and that the solver was never called.
`test_a_solver_infeasibility_under_a_floor_is_wrapped_as_a_value_error` asserts the wrapper
and that the result is a `ValueError`, which is what keeps the edit loop's revert working;
`test_a_solver_infeasibility_with_no_floor_keeps_its_own_type` asserts the wrapper does not
overreach. `test_mv_target_made_unreachable_by_a_floor_says_so_and_names_both_numbers`
asserts the same call succeeds without the floor, so the floor is provably what closed the
target.

`test_the_dividend_yield_vector_is_aligned_after_min_history_drops_a_ticker` builds a
matrix with a high-yielding short-history column, filters it, and asserts the refusal names
the surviving best payer and never the dropped one.
`test_dividend_yield_vector_is_ordered_by_the_tickers_not_the_dict` pins the alignment
directly.

On the data side, `test_a_covered_non_payer_yields_exactly_zero_not_nan` and
`test_an_uncovered_ticker_is_unavailable_never_zero` pin the distinction the Decision Log
calls the one silently wrong answer available.
`test_load_dividend_figures_reports_a_pre_existing_database_as_unavailable` proves a
database built before this feature reports a named gap rather than a universe of
non-payers. `test_load_dividend_figures_divides_by_the_raw_close_not_the_adjusted_close`
pins the denominator. `test_a_dividend_exactly_lookback_months_before_as_of_is_excluded`
and its one-day-later counterpart pin the half-open window.
`test_a_pence_quote_and_its_pound_equivalent_give_the_same_yield` pins the invariant that
actually matters about minor units, rather than one scale factor.
`test_upsert_dividends_tables_clears_rows_when_a_payer_stops_paying` pins the
tickers-driven delete. `test_reconcile_trailing_yield_recovers_a_known_dividend_return`
builds its fixture forwards from a known dividend and asserts recovery to `rel=1e-9`.

Beyond the suite, the behavior in `Concrete Steps` is the acceptance: a non-binding floor
leaves weights untouched, a binding floor moves them and prints an income at or above the
request, an impossible floor is refused naming the real ceiling, `[d]ividend` retunes the
floor and an unreachable value reverts the edit rather than ending the session, and
`uv run portfolio-holdings` and `uv run portfolio-holdings whatif` report the held
portfolio's yield, its annual income, and the signed change after each edit.


## Idempotence and Recovery

Every step is safe to repeat. `upsert_dividends_tables` deletes the named tickers' rows
before inserting, so re-running `portfolio-build-dividends` replaces rather than
duplicates - verified by rebuilding and re-counting (35 rows and 7 coverage rows before and
after). `write_dividends_tables` drops and recreates both tables outright. Reading is
read-only throughout and never creates a database: `tickers_with_dividend_data`,
`load_dividends_long` and `load_latest_close` all open `read_only=True` and report a
missing file or missing table as "nothing known" rather than raising, following
`load_ticker_currencies`' discipline.

The blast radius is small by construction. The new tables are additive - no existing table
is read differently or written at all by this work, and `src/dataset/prices.py`'s
`_fetch_batch` is untouched specifically so the 1.2-million-row `prices` table cannot
shift. A partially completed build leaves correct rows for the tickers it reached and no
coverage rows for the rest, which reports as an honest gap and is fixed by re-running.

To roll back the data entirely, drop the two tables; every consumer then reports "no
dividend data has been fetched" and the rest of every report is unaffected:

    uv run python -c "
    import duckdb
    con = duckdb.connect('data/portfolio.duckdb')
    con.execute('DROP TABLE IF EXISTS dividends')
    con.execute('DROP TABLE IF EXISTS dividend_coverage')
    con.close()"


## Artifacts and Notes

The Milestone 1 transcript, which settles the two questions that decide the arithmetic:

    TOP-LEVEL FIELDS: ['Adj Close', 'Close', 'Dividends', 'High', 'Low', 'Open',
                       'Stock Splits', 'Volume']
    nlevels: 2 | names: ['Price', 'Ticker']

    Dividends non-zero rows:
    Ticker      GOOGL     KO   NVDA
    Date
    2024-03-05    0.0  0.000  0.004
    2024-03-14    0.0  0.485  0.000
    2024-06-10    0.2  0.000  0.000
    2024-06-11    0.0  0.000  0.010
    2024-06-14    0.0  0.485  0.000

    Stock Splits non-zero rows:
    Ticker      GOOGL   KO  NVDA
    Date
    2024-06-10    0.0  0.0  10.0

    GOOGL dividends sum: 0.2
    GOOGL dividend dtype/nan count: float64 0 of 168

NVDA's 0.004 before its 10-for-1 split and 0.010 after it prove the amounts are already on
the current share basis. GOOGL's zero NaN count shows a non-payer is zero-filled rather
than null. And the pence check:

    close level (pence): 264.75
    dividends: 2024-02-29  5.3
               2024-08-15  2.9
    implied yield if same unit: 0.03097
    fast_info currency: GBp

3.1% is Barclays' real yield, so dividends share the price's unit and take the same
multiplier.

The trailing yields the data layer produces, against real Yahoo data to 2024-12-31 - every
figure matching the company's actual yield, and BRK.B correctly a covered zero rather than
an unavailable one:

    ticker     TTM dps    yield
    T           1.1120   0.0488
    KO          1.9400   0.0312
    XOM         3.8400   0.0357
    AAPL        0.9900   0.0040
    GOOGL       0.6000   0.0032
    BRK.B       0.0000   0.0000
    NVDA        0.0340   0.0003

The reconciliation run over the shipped `data/portfolio.duckdb` after the backfill,
comparing every payer's trailing yield against the dividend-reinvestment factor already
implied by its own `close`/`adj_close` pair - 412 payers, no network call:

    reconciled 412 payers against the close/adj_close factor already on disk
      median relative difference : 0.1058
      90th percentile            : 0.2320
      within 25% relative        : 92.0%
      within 50% relative        : 99.8%

    a few household names:
    ticker  trailing  implied  rel_diff
      AAPL    0.0055   0.0053    0.0366
       CVX    0.0370   0.0414    0.1060
       IBM    0.0397   0.0464    0.1450
       JNJ    0.0324   0.0308    0.0542
        KO    0.0301   0.0319    0.0572
      MSFT    0.0071   0.0082    0.1337
        PG    0.0237   0.0256    0.0755
         T    0.0653   0.0715    0.0857
        VZ    0.0661   0.0743    0.1108
       XOM    0.0311   0.0358    0.1311

    largest divergences (tickers whose price moved most):
    ticker  trailing  implied  rel_diff
       APH    0.0035   0.0093    0.6213
        GL    0.0117   0.0082    0.4351
      NVDA    0.0002   0.0003    0.4315
       VFC    0.0605   0.0426    0.4197
       NRG    0.0157   0.0270    0.4170

How to read this. The two quantities are not the same definition, so exact agreement is not
expected - the divergence tracks how far each ticker's price moved over the window, and the
five largest are all big movers (APH and NVDA rose steeply, VFC fell hard). What the check
proves is the absence of a SYSTEMATIC error, and it proves it strongly: a split-basis
mistake would show as a clean factor of 2, 4 or 10 (100% to 900% relative) and a minor-unit
mistake as a factor of 100, and no ticker in 412 is anywhere near either - 99.8% sit inside
50%. Every yield is also the right order of magnitude for the company it belongs to.

A rendered report with a floor in force:

    Dividend yield / annual income (trailing 12 months):
      INCOME: yield=0.0600  $3,330.54 USD
      MIDCAP: yield=0.0250  $847.45 USD
      GROWTH: yield=0.0010  $10.59 USD

    Portfolio expected return: 0.0969  Portfolio volatility: 0.0694  Portfolio Sharpe: 1.1083
    Risk-free rate used: 0.0200
    Portfolio dividend yield: 0.0419  Annual dividend income: $4,188.57 USD (on --value $100,000.00 USD)
    Minimum dividend yield: 0.0300 (--min-annual-dividend $3,000.00 USD / --value $100,000.00 USD) - not binding, the portfolio clears it by 0.0119 unaided
    Target annual return: n/a (objective is GMV, not MV)

And the MV collision, with the attainable return computed under the floor:

    a target annual return of 0.1000 is unreachable once --min-dividend-yield's dividend
    floor of 0.0450 is applied: the highest annual return any portfolio meeting that floor
    can reach is 0.0981, because the floor forces weight into the higher-yielding
    candidates, which are not the highest-returning ones. Lower --target-return to at most
    0.0981, lower the dividend floor, or switch to GMV or MSR.


## Interfaces and Dependencies

No new third-party dependency. PyPortfolioOpt 1.6.0 (already pinned at `>=1.6.0`) supplies
`EfficientFrontier.add_constraint`; yfinance (already pinned at `>=1.3.0`) supplies dividend
history through its existing `download` function's `actions=True` argument; cvxpy arrives
with PyPortfolioOpt.

In `src/dataset/dividends.py`, define:

    DIVIDENDS_LONG_COLUMNS = ["ex_date", "ticker", "amount"]
    COVERAGE_COLUMNS = ["ticker", "fetch_start", "fetch_end"]

    class DividendFieldMissingError(RuntimeError): ...

    def _fetch_batch(symbols: list[str], start: str, end: str) -> pd.DataFrame
    def fetch_dividend_history(symbols, start, end, batch_size, pause_seconds) -> pd.DataFrame
    def reshape_dividends_long(raw, symbol_to_ticker: dict[str, str]) -> pd.DataFrame
    def fetched_dividend_tickers(raw, symbol_to_ticker: dict[str, str]) -> set[str]
    def apply_dividend_multipliers(long_dividends, multipliers: dict[str, float]) -> pd.DataFrame
    def coverage_frame(tickers, unresolved: set[str], start, end) -> pd.DataFrame
    def write_dividends_tables(dividends_df, coverage_df, db_path) -> None
    def upsert_dividends_tables(dividends_df, coverage_df, tickers, db_path) -> None
    def load_dividends_long(tickers, window_start, window_end, db_path, read_only=True) -> pd.DataFrame
    def tickers_with_dividend_data(tickers, db_path) -> set[str]
    def trailing_dividends_per_share(long_df, tickers, as_of, lookback_months=12) -> dict[str, float]
    def trailing_dividend_yields(dividends_per_share, prices, known_tickers) -> tuple[dict, dict]
    def reconcile_trailing_yield(prices_df, as_of, lookback_months=12) -> float | None
    def load_latest_close(tickers, as_of, db_path) -> pd.Series
    def load_dividend_figures(tickers, as_of, db_path, lookback_months) -> tuple[dict, dict, dict]
    def build_dividends_for_tickers(tickers, db_path, start, end, multipliers, unresolved) -> pd.DataFrame
    def build_dividends(db_path, start, end) -> pd.DataFrame
    def main() -> None

In `src/optimizer/dividends.py`, define:

    MAX_DIVIDEND_YIELD = 0.25
    DIVIDEND_FEASIBILITY_TOLERANCE = 1e-9
    DIVIDEND_BINDING_TOLERANCE = 1e-6

    class DividendFloorError(ValueError): ...
    class DividendYieldUnavailableError(ValueError): ...

    class DividendFloor(NamedTuple):
        yield_floor: float
        origin: str
        cash_floor: float | None = None
        portfolio_value: float | None = None
        currency: str = "USD"
        def cash_on(self, total_value: float) -> float

    class DividendFigures(NamedTuple):
        dividends_per_share: dict[str, float]
        annual_dividends: dict[str, float]
        total_annual_dividends: float | None
        dividend_yield: float | None
        value_covered: float | None
        unavailable: dict[str, str]

    NO_DIVIDEND_FIGURES = DividendFigures({}, {}, None, None, None, {})

    def validate_dividend_yield(value: object, source: str) -> float
    def validate_min_annual_dividend(value: object, source: str, portfolio_value: object) -> float
    def dividend_yield_vector(yields: dict[str, float], tickers: Sequence[str]) -> np.ndarray
    def dividend_yield_ceiling(vector: np.ndarray, tickers: Sequence[str]) -> tuple[str, float]
    def check_dividend_floor_feasible(floor, vector, tickers) -> None
    def portfolio_dividend_yield(weights, vector: np.ndarray) -> float
    def covered_dividend_yield(weights, yields) -> tuple[float, float, tuple[str, ...]]
    def dividend_figures(positions, market_values, dividends_per_share, unavailable) -> DividendFigures

In `src/optimizer/portfolio.py`, these signatures change:

    def _fit_efficient_frontier(
        returns_matrix, objective, target_annual_return, risk_free_rate,
        dividend_floor: DividendFloor | None = None,
        dividend_yields: dict[str, float] | None = None,
    ) -> tuple[EfficientFrontier, pd.Series, pd.DataFrame]

    def compute_weights_and_stats(
        returns_matrix, objective, target_annual_return, risk_free_rate, *,
        dividend_floor: DividendFloor | None = None,
        dividend_yields: dict[str, float] | None = None,
        dividends_per_share: dict[str, float] | None = None,
    ) -> PortfolioStats

`PortfolioStats` gains seven defaulted fields: `dividend_yields`, `dividends_per_share`,
`portfolio_dividend_yield`, `dividend_yield_floor`, `dividend_floor_origin`,
`dividend_yields_missing`, `dividend_weight_covered`. `compute_weights` is unchanged, by
design.

In `src/optimizer/holdings.py`, `HoldingsStats` gains
`dividends: DividendFigures = NO_DIVIDEND_FIGURES`, and `holdings_stats` and
`unavailable_holdings` gain the parameters that populate it.

In `src/flow/`, `compute_weights_and_allocation`, `run_pipeline_against` and
`_run_edit_loop` gain `dividend_floor: DividendFloor | None = None`;
`print_weights_and_allocation` and `print_pipeline_result` gain
`portfolio_value: float | None = None`; and `src/flow/cli.py` gains
`_settle_dividend_floor`, `_prompt_dividend_floor`, `format_signed_money`,
`format_dividend_floor`, `format_dividend_coverage`, `print_dividend_section`,
`format_allocated_dividends`, `format_holding_dividend`,
`format_holdings_dividend_total` and `format_dividend_delta`.

The two DuckDB tables, in both `data/portfolio.duckdb` and `data/holdings.duckdb`:

    CREATE TABLE dividends (ex_date DATE, ticker VARCHAR, amount DOUBLE);
    CREATE TABLE dividend_coverage (ticker VARCHAR, fetch_start DATE, fetch_end DATE);

And one new script in `pyproject.toml`:

    portfolio-build-dividends = "src.dataset.dividends:main"


## Revision Notes

Revised during implementation, on 2026-09-07, in four passes. Recorded here because the plan
changed materially after work began and the reasons matter more than the edits.

The first pass replaced Milestone 1's two open questions with their answers, once the
yfinance dividend contract had actually been probed - dividends are already split-adjusted,
non-payers are zero-filled rather than null, and dividends share the price's minor unit. The
plan had reserved judgment on all three because none is readable from the library's source,
and the answer to the first would have added a whole split-correction step had it gone the
other way.

The second pass added the finding that `actions=True` cannot safely be turned on in the
existing price fetch, and changed the Plan of Work accordingly. The original reasoning had
been that one download guarantees one split basis for prices and dividends alike, which is a
real benefit; reading yfinance's row-retention code showed the flag can also change the row
set of the download that populates the 1.2-million-row `prices` table, and this plan's
premise is that existing numbers do not move. The `dividend_coverage` table was added in the
same pass, because a separate fetch means every database built before this feature has
prices for the whole universe and dividends for none - and without a coverage record that
state reports as 525 confident non-payers rather than as a fixable gap.

The third pass added three discoveries that only a live end-to-end run could produce, and
one `Progress` entry for a step the plan had specified and the implementation had missed:
`validate_and_ingest_tickers` was never wired to fetch dividends, so a `user_provided` pool
- which runs against a session snapshot rather than `data/portfolio.duckdb` - was correctly
but uselessly refusing every ticker somebody typed. The two discoveries beside it are both
consequences of the pre-existing `adj_close` valuation basis: the holdings report and the
pool report printed different yields for the same ticker until they were forced onto one
definition, and the whole-share allocation's dividend total sits about 12% above the
continuous portfolio's for the same reason. The second of those is left as separate work,
named in `Progress`, because fixing it means changing which price column
`allocate_shares` uses and that would move existing numbers.

The fourth pass recorded that adding a network call to the ingestion chain broke one test's
hermeticity, that the try/except keeping the ingestion robust is exactly what hid it, and
that the test's runtime falling from 3.44s to 0.68s after the fix was the evidence.

What did not change across any of those passes: the four decisions settled with the user
before implementation - two flags, a trailing twelve-month estimator, the scope, and refusal
over fallback - and the constraint's own shape, which the numeric spike had already proven
before a line of it was written.


## Revision Note: splits made visible, and stale share counts caught (2026-09-08)

A user reported 9984.T's expected dividend as ¥11,000/yr on 1,000 shares when the announced
dividends were ¥22 (2025-09-29) and ¥5.5 (2026-03-30), apparently ¥27,500, and asked whether
it was a bug.

It was not. 9984.T split 4:1 on 2025-12-29, and Yahoo Finance restates every dividend onto
the current share basis, so the ¥22 is stored as ¥5.5 and every payment back to 2021 reads
¥5.5 for the same reason. The trailing twelve months is ¥11 per current share, and ¥11 ×
1,000 = ¥11,000 - which is exactly the cash a holder of 1,000 current shares received: 250
shares × ¥22 before the split plus 1,000 × ¥5.5 after. The ¥27,500 expectation adds a
pre-split dividend to a post-split share count. This vindicates `Surprises & Discoveries`
entry 5, which established from NVDA's 10:1 split that Yahoo already split-adjusts dividends
and that this plan should therefore apply no correction of its own; 9984.T is the second
independent confirmation, and the first from a non-US listing.

But the report could not be reconciled against reality, and that WAS a defect. It printed
¥11,000 and never mentioned the split or the restatement, so anyone comparing it with an
announcement would reasonably conclude it was broken - exactly what happened. A dividend
figure had been given without its methodology, the same failure the returns window and the
risk-free rate's provenance already exist to prevent.

Worse, the investigation exposed a latent correctness bug this plan had not considered:
`memory/portfolio.json` holds raw share counts with no split awareness, so a count recorded
before a split silently understates the position, and therefore the total value, every
weight and every dividend figure, by the split factor - while leaving the report internally
consistent, which is what makes it dangerous. The reporting user's portfolio was written
after the split and so was correct, but only by luck; had they recorded it in November, the
figure would have been wrong by exactly the fourfold factor they suspected.

Three changes followed, all additive. A `splits` table now stores the `Stock Splits` column
that `actions=True` was already downloading and `reshape_dividends_long` was discarding, so
it costs no extra network call. `load_dividend_figures` returns a `SplitContext` for any
ticker that split inside the trailing window - the splits and the payments they restated -
and both reports print a sentence naming the split and reconciling the announced amount
against the stored one, derived via the pre-existing
`src/dataset/fundamentals.py:cumulative_split_ratio_after` rather than hardcoded. And
`stale_share_counts` compares each portfolio's own `updated_at` against the splits since,
warning above the figures it affects and printing the exact `set` command that would fix it,
without ever writing to the file.

Three things worth recording for whoever comes next. The check on cache staleness had to be
extended (`src/dataset/holdings_cache.py`) or the feature would never have activated on an
existing cache, and it asks whether the `splits` TABLE exists rather than whether a ticker
has split rows - the identical trap `dividend_coverage` was built to avoid, since a ticker
that never split holds no rows either. That guard then broke seven existing tests whose
fixtures build caches with no `splits` table; the fix was to give the fixtures the table, so
they keep exercising the monthly rule while one new test builds a pre-migration cache
deliberately. And `dividend_figures` was calling `list()` on the new `SplitContext`, which
flattened the NamedTuple into `[splits, payments]` and destroyed the field names - caught
only by running the real command, which is the third time in this feature's history that an
end-to-end run found what the unit tests could not.

Verified: `uv run pytest tests/test_*.py` -> `743 passed` in 404.57s, up from 709. Both
reports were exercised against live data - `portfolio-holdings show` and
`uv run portfolio --selection user_provided` each print the reconciling sentence for
9984.T - and the guard was checked both ways: silent on the real portfolio (written
2026-09-06, after the split) and warning on a copy backdated to 2025-11-02, whose suggested
`uv run portfolio-holdings set 9984.T 4000` was then run to confirm it parses verbatim.

One item from this plan's `Progress` list is now resolved by other means: `data/holdings.duckdb`
has been backfilled, since the splits migration forces a refresh on next use.

The `allocate_shares` `adj_close` issue, carried as follow-up through two sessions, is now
FIXED - see `plans/05_optimizer_and_allocation.md`'s revision note of the same date. Two
consequences for this plan. The `Annual dividends at these share counts` line and the
`Annual dividend income` line above it now agree to within whole-share rounding ($6,900.48
against $6,898.91, 0.02% apart) where they previously sat about 18% apart, so
`format_allocated_dividends`' docstring and the `README.md` bullet no longer explain a gap
that exists. And the dividend yield's denominator is no longer a separate near-copy of the
price lookup: `load_latest_close` moved out of `src/dataset/dividends.py` into
`src/dataset/prices.py`, where `src/optimizer/portfolio.py`'s `load_latest_prices` now also
reads it, so the price a share is bought at, valued at and has its dividend divided by is one
number by construction rather than by coincidence. The duplication this plan introduced was
itself part of how the column choice drifted, which is worth remembering the next time a
convenient local copy looks harmless.


## Revision Note: dividends reach live mode, and `--no-dividend-fetch` (2026-09-08)

A user asked when `uv run portfolio-build-dividends` is meant to be run, noted that backtest
mode does not need dividend data, and asked why `uv run portfolio` does not use the
`--refresh-holdings` flag that `uv run portfolio-holdings` does. Tracing it found a gap this
plan had left, and the gap was in the mode people use most.

Where candidate-pool dividends came from:

    date         selection                reads                        dividends?
    historical   screened (llm_s_only..)  data/portfolio.duckdb (RO)   only if built
    historical   user_provided            throwaway snapshot           automatic
    today        screened (the DEFAULT)   live snapshot                NONE, EVER
    today        user_provided            throwaway snapshot           automatic

This plan wired `validate_and_ingest_tickers` and never `build_live_snapshot`, so a live run
with a screened selection - `uv run portfolio --date today`, the default `--selection
llm_s_only` - produced a snapshot with prices, factors, momentum and returns but no
`dividends` table at all. Every candidate reported its dividend data as unavailable and
`--min-annual-dividend` was refused for the whole pool, and no flag or build command could
reach it, because the snapshot is created and discarded inside the run. That was the third
end-to-end run in this feature's history to find what the unit tests could not, and the
lesson is the same each time: this project has four mode/selection paths through the session
database, and wiring one of them is not wiring the feature.

`build_live_snapshot` now calls `build_dividends` after `build_returns` for the screened
selections, which needed no new plumbing because that function already derives its ticker
universe from the `prices` table it is pointed at. Its failure is logged and swallowed: by
that point the snapshot has cost minutes of fetching membership, prices, factors, momentum
and returns, and discarding all of it over a rate-limited dividend batch would be a poor
trade, while the report already names a ticker whose dividend data is missing rather than
assuming it pays nothing.

`--no-dividend-fetch` was added alongside `--no-benchmark-fetch`, because that build is one
more pass over ~500 tickers on every live run. It does two things, and doing both is what
makes it honest: it skips the snapshot's dividend build (and
`validate_and_ingest_tickers`' per-ticker fetch), and it skips CONSULTING dividends, so the
report reaches the `None` "not consulted" state rather than the `{}` "consulted, nothing
found" state that names each ticker's reason. Those two states were already modelled and
worded differently by this plan; the flag simply had to land on the right one. Combining it
with either floor flag is a contradiction and is refused at `parser.error` time before any
snapshot is paid for, and `[d]ividend` in the edit loop refuses for the same reason.

On the `--refresh-holdings` question, which was reasonable and whose answer is worth writing
down: `data/holdings.duckdb` is a CACHE of what the user owns, with a data-derived monthly
staleness rule that `--refresh-holdings` forces. `data/portfolio.duckdb` is a BUILD ARTIFACT
maintained by the `portfolio-build-*` family, and `uv run portfolio` is forbidden from
mutating it, so it cannot have a refresh flag at all. Different storage, different lifecycle,
different control - but the confusion was justified, because a third case was getting neither.

`portfolio-build-dividends` is kept and now documents its own single job, in its docstring,
in the command's output and in `README.md`: populate `data/portfolio.duckdb` for
historical-date screened runs, run after `portfolio-build-prices`. Every other path needs
nothing.


## Revision Note: an unsatisfiable request is reported, not raised (2026-09-08)

A user ran `--objective MV --min-dividend-yield 0.03` and got a Python traceback whose last
line was a perfectly good message: 12% is unreachable under a 3% floor, the best complying
portfolio reaches 10.97%, here are three ways to fix it. Delivering that as a traceback
wasted the message and looked like a crash.

This plan chose that behavior deliberately, reasoning that an unreachable `--target-return`
already propagated the same way and that consistency argued for matching it. The reasoning
was sound and the conclusion was wrong: the precedent was itself bad, and copying it spread a
defect rather than containing one. Consistency with a bad pattern is not a virtue - worth
remembering the next time this plan's Decision Log cites an existing behavior as
justification.

It was also a family. Three conditions - a dividend floor blocking an MV target, a target no
pool can reach, a risk-free rate above every expected return - are all `ValueError`, all
tracebacked on the initial run, and all were already handled gracefully INSIDE the
interactive edit loop. That asymmetry was the actual defect, and the second was worse than
the reported one: PyPortfolioOpt's raw text named neither the flag responsible nor a value
that would work.

All five members now share `UnsatisfiableRequestError` in the new `src/errors.py` (the
dividend pair, `MixedCurrencyPoolError`, and two new ones translating PyPortfolioOpt's raw
messages), so `src/flow/cli.py` catches one narrow type rather than `ValueError`, which would
also swallow genuine bugs. A test pins that: a `ValueError` which is not an
`UnsatisfiableRequestError` still escapes `main`.

The design constraint worth recording is why the failure is RETURNED rather than raised from
`run_pipeline_against`. That function runs `run_scan` - the LLM agents - and then the
optimizer, so an exception discards the screening, and re-running it means re-invoking agents
this project treats as a correctness problem to repeat, not merely a cost. So the result dict
now carries `unsatisfiable` with every figure `None` beside it, which is the shape
`BenchmarkStats` and `HoldingsStats` already use. The payoff is better than a clean exit: the
LLM rule, the scanner branch and the candidate list all still print, and the session stays
open so `[t]` or `[d]` fixes the number against the snapshot already paid for. Verified end to
end - the reported command now prints its reason and then produces a full report after
typing `t` and `0.10`, with no refetch, exiting 0; finishing without a correction exits 1.
