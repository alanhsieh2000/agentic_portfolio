# Build the GMV/MV/MSR optimizer and share-allocation tool


This ExecPlan is a living document. The sections `Progress`, `Surprises & Discoveries`, `Decision Log`, and `Outcomes & Retrospective` must be kept up to date as work proceeds. This plan must be maintained in accordance with `PLANS.md` at the repository root. This plan builds on `plans/01_dataset.md` (checked into this repository, for monthly returns and price history) and `plans/04_candidate_scanner.md` (checked into this repository, for the candidate ticker list), but is designed to also accept an arbitrary user-supplied ticker list, per README.md's explicit requirement.


## Purpose / Big Picture


After this plan is done, a person can hand this tool a list of tickers (whether that list came from the candidate scanner in `plans/04_candidate_scanner.md`, or is a list they typed themselves) and a dollar amount, and get back two things: a set of portfolio weights — under three different objectives named GMV, MV, and MSR, explained below, estimated from each ticker's history of realized monthly returns — and, given the weights and a portfolio dollar value, an actual number of whole shares of each stock to buy so the money is spent as close to those weights as possible without buying fractional shares. This is `README.md`'s "optimization tool" and "consolidation tool" bullets combined into one plan, since in practice they are two calls into the same already-installed library, one right after the other.


"GMV" stands for Global Minimum Variance: the portfolio weights, among all portfolios of the given candidate stocks that add up to 100% invested, that produce the lowest possible variance (a proxy for risk) in the portfolio's returns, without any regard for how high or low its expected return is. "MV" stands for Mean-Variance: the lowest-variance portfolio that still achieves at least some target return the user specifies (this plan defaults that target to 1% per month, matching the target the paper this project reproduces uses in its own empirical analysis). "MSR" stands for Maximum Sharpe Ratio: the portfolio, among all possible weight combinations, with the highest ratio of expected return to risk (its Sharpe ratio) — informally, the "best risk-adjusted bet" rather than the "safest bet" (GMV) or "cheapest way to hit a return target" (MV). All three names and definitions are used identically in the paper this project reproduces ("Designing Agentic AI-Based Screening for Portfolio Investment", Caner, Capponi, Sun, and Tan, arXiv:2603.23300).


## Progress


- [x] Implement the trailing monthly-returns matrix loader (with a minimum-history drop rule) that feeds expected-return and covariance-matrix estimation.
- [ ] Implement the three weight-optimization functions (GMV, MV, MSR) using PyPortfolioOpt, fed by monthly returns rather than daily prices.
- [ ] Implement a separate latest-price lookup (from the `prices` table) for the allocation step, independent of the returns matrix.
- [ ] Implement the discrete share-allocation step using PyPortfolioOpt's `DiscreteAllocation`.
- [ ] Write `tests/test_optimizer.py` covering weight computation and allocation against small fixture price series with hand-checkable expected results.
- [ ] Manually run all three objectives against a real candidate list from `plans/04_candidate_scanner.md` and sanity-check the resulting weights and share counts.


## Surprises & Discoveries

`load_returns_matrix` is implemented in `src/optimizer/portfolio.py`, split into a DB-reading half (`_load_window_dates`, `_load_returns_long`) and two pure, independently-testable halves (`pivot_returns_matrix`, `apply_min_history_rule`), per this plan's own Validation and Acceptance hint to isolate the drop-logic from the actual database read. Verified against the real `data/portfolio.duckdb`: for 8 long-established large caps (AAPL, MSFT, JPM, XOM, JNJ, PG, KO, DIS) as of 2024-03-01, all 60 months of the lookback window are present and non-null, and a nonexistent ticker is correctly dropped and logged with 0 months of history. Covered by `tests/test_optimizer.py` (min-months drop, recent-IPO leading nulls kept, internal-gap drop, pre-`as_of`-delisting trailing nulls kept).

(Remaining to observe once `compute_weights` is implemented: whether `max_sharpe` or `efficient_return` fails to converge or raises on certain small or highly-correlated candidate sets, a real, documented PyPortfolioOpt behavior worth having a fallback for once observed rather than guessed at in advance.)


## Decision Log


- Decision: use PyPortfolioOpt's built-in risk models (`risk_models.sample_cov` and `risk_models.CovarianceShrinkage(...).ledoit_wolf()`) as the covariance-matrix input to `EfficientFrontier`, rather than implementing any of the paper's five custom precision-matrix estimators (nodewise regression, residual nodewise regression, POET, a deep-learning-based estimator, or nonlinear shrinkage).
  Rationale: PyPortfolioOpt is already a dependency of this repository and is explicitly named in `README.md`'s acknowledgements (citing Martin, R. A., 2021, Journal of Open Source Software) as the intended optimization library; the user explicitly chose this simpler built-in option over replicating the paper's more elaborate estimator comparison when asked. This is recorded here as a known fidelity gap versus the paper's specific empirical results, which found its more sophisticated estimators (especially the deep-learning one) produced meaningfully higher Sharpe ratios than simpler covariance estimates.
  Date/Author: 2026-08-05, decided by repository owner during planning interview.
- Decision: default the MV objective's target return to 1% per month, matching the paper's own stated target, but make it a parameter so a user can change it.
  Rationale: matches the paper's empirical setup exactly by default, while not hardcoding an assumption a user might reasonably want to change.
  Date/Author: 2026-08-05, plan author.
- Decision: derive expected returns and the covariance matrix from `plans/01_dataset.md`'s persisted `returns` table (monthly returns), not from PyPortfolioOpt's default daily-price-derived-and-annualized calculation.
  Rationale: this project rebalances monthly and the paper's own MV target is stated per month; estimating from 252 days of daily prices and then annualizing, as an earlier draft of this plan did, is a frequency mismatch with that. The user chose monthly returns over keeping the daily-price approach when asked directly, accepting the statistical cost described in the next decision as a known tradeoff.
  Date/Author: 2026-08-05, decided by repository owner during a plan-review follow-up question.
- Decision: use a 60-month (5-year) rolling lookback for the returns matrix, with a 24-month minimum below which a ticker is dropped from that month's optimization (logged) — shorter than the paper's own window, as a deliberate, documented deviation.
  Rationale: the paper this project reproduces states explicitly that it uses "a rolling window of 180 months (15 years) of historical returns data, stepping forward one month at a time" to estimate its precision matrix, reporting screened portfolios averaging ~22 stocks against that 180-observation window. Matching 180 months exactly would require `plans/01_dataset.md` to fetch and validate roughly 19 years of price history (back to ~2005) for this project's full ticker universe — a real cost this project chooses not to pay, on top of the fidelity gaps already recorded elsewhere in this plan set. When shown the paper's actual 180-month figure and asked whether to match it or use something shorter, the user chose 60 months lookback with a 24-month minimum as a practical middle ground: long enough to be a meaningfully less noisy estimate than an even shorter window, short enough to only require price history back to 2015 rather than 2005. `plans/01_dataset.md`'s `prices` and `returns` tables are built with this 60-month figure in mind (price history from 2015-01-01, and `returns` computed over a wider 112-month sequence so the earliest 2020-2021 backtest rebalances have real trailing observations to draw on).
  Date/Author: 2026-08-05, decided by repository owner after being shown the paper's actual 180-month figure during a plan-review follow-up question.
- Decision: within the 60-month/24-month-minimum window above, use whatever number of months a ticker actually has (between 24 and 60) rather than requiring the full 60; only drop a ticker outright if it has fewer than 24.
  Rationale: the paper does not specify a rule for tickers with less than its own full 180-month window (for example, recent IPOs), so this project makes its own explicit choice rather than leaving the behavior undefined: a floor-with-available-history approach, consistent with how `plans/01_dataset.md`'s other missing-data handling (dropped tickers logged, not silently imputed) already works throughout this plan set.
  Date/Author: 2026-08-05, plan author, following the user's 60-month/24-month decision above.
- Decision: use the shared `returns` table from `plans/01_dataset.md` for the optimizer's inputs, and rely on that same table (rather than an independent calculation) for plan 6's backtest scoring of realized next-month returns.
  Rationale: the user explicitly chose "one shared returns table" over letting this plan and plan 6 each derive "monthly return" independently, which would risk the two disagreeing.
  Date/Author: 2026-08-05, decided by repository owner during a plan-review follow-up question.


## Outcomes & Retrospective


(To be filled in once this plan is implemented and validated.)


## Context and Orientation


This plan adds `src/optimizer/portfolio.py` to this Python 3.12, `uv`-managed repository, a new package sibling to `src/dataset/`, `src/agents/`, and `src/scanner/` from prior plans. All commands below run from the repository root and assume `plans/01_dataset.md` has already been implemented, so `data/portfolio.duckdb`'s `returns` table is populated with monthly returns spanning 2015-01-01 through 2024-04-30 (112 months, wider than just the 2020-2024 backtest window, specifically so this plan's 60-month trailing lookback has real observations even for the earliest backtest rebalances), and its `prices` table is populated with daily prices for the same 2015-2024 span.


This plan uses PyPortfolioOpt, a Python portfolio-optimization library already a dependency of this repository (see `pyproject.toml`'s `pyportfolioopt` entry, and its citation in `README.md`'s acknowledgements: Martin, R. A., 2021, "PyPortfolioOpt: portfolio optimization in Python," Journal of Open Source Software, 6(61), 3066). Three of its terms of art are used directly below. `risk_models` is PyPortfolioOpt's module of functions that turn a DataFrame of historical returns (or prices) into a covariance matrix (a table describing how every pair of stocks' returns move together, which the optimizer needs to know to judge diversification) — passing `returns_data=True` tells it the DataFrame already holds period returns rather than raw prices, so it should not internally difference or convert anything, and `frequency=12` tells it those periods are months, so any internal annualization multiplies by 12 rather than by the default 252 (trading days). `EfficientFrontier` is PyPortfolioOpt's main optimizer class: constructed with expected returns and a covariance matrix, it exposes methods like `.min_volatility()` (this plan's GMV), `.efficient_return(target_return=...)` (this plan's MV), and `.max_sharpe()` (this plan's MSR), each of which, once called, lets you read back the resulting weights with `.clean_weights()`. `DiscreteAllocation` is a separate PyPortfolioOpt class that takes a weights dict, a Series of latest prices, and a total dollar amount, and computes an actual whole-number-of-shares allocation plus any leftover unallocated cash — note that `DiscreteAllocation` itself always wants real dollar prices, not returns, which is why this plan keeps a separate, small use of the `prices` table just for this step.


"Candidate list" here means the `list[str]` of ticker symbols produced by `plans/04_candidate_scanner.md`'s `scan` function, or any other list of ticker symbols a user supplies directly — this plan's functions accept a plain list either way and have no awareness of where it came from, satisfying `README.md`'s requirement that the optimization tool work "for given and user input candidates."


## Plan of Work


Create `src/optimizer/portfolio.py` with a function `load_returns_matrix(tickers: list[str], as_of: date, lookback_months: int = 60, min_months: int = 24, db_path: str = "data/portfolio.duckdb") -> pd.DataFrame` that reads `plans/01_dataset.md`'s `returns` table for exactly the given tickers, for the `lookback_months` months (default 60) ending on or before `as_of` (drawing on the wider 2015-2024 date coverage that table has, per its Interfaces and Dependencies documentation in `plans/01_dataset.md`, not only the 52 narrower backtest rebalance dates), and pivots it into a wide DataFrame (months as rows, tickers as columns, `monthly_return` values). For each ticker, count how many non-null months it actually has within that window; if a ticker has fewer than `min_months` (default 24), drop that ticker's column from the matrix entirely and log the ticker and how many months it did have — PyPortfolioOpt's covariance estimators require a complete, rectangular returns matrix with no gaps, and silently forward-filling or zero-filling missing months would fabricate data. A ticker with between `min_months` and `lookback_months` non-null months (for any ticker with an IPO more recent than the start of the lookback window) is kept, using only the months it actually has — PyPortfolioOpt's estimators tolerate a shorter history per column, they just cannot tolerate gaps within a column, so any internal null for a kept ticker within its own available span must not occur (if the underlying `returns` table has an internal null for a ticker that otherwise has enough surrounding months, drop that ticker too, and log why, rather than silently interpolating). This 60-month/24-month pairing is a deliberate, smaller-than-the-paper's window, recorded in the Decision Log above — the paper itself uses 180 months.


Add a function `compute_weights(returns_matrix: pd.DataFrame, objective: str, target_monthly_return: float = 0.01) -> dict[str, float]` where `objective` is one of the literal strings `"GMV"`, `"MV"`, or `"MSR"`. Internally: compute expected returns with PyPortfolioOpt's `expected_returns.mean_historical_return(returns_matrix, returns_data=True, frequency=12)`, compute the covariance matrix with `risk_models.CovarianceShrinkage(returns_matrix, returns_data=True, frequency=12).ledoit_wolf()` (Ledoit-Wolf shrinkage is used for all three objectives, per the Decision Log above, rather than plain sample covariance, because shrinkage is well-documented to produce a better-conditioned, less noisy covariance estimate for the number of assets this project's candidate lists are likely to contain, which matters even more with a monthly-returns matrix's already-thin sample size than it would with daily data), construct `ef = EfficientFrontier(expected_returns, cov_matrix)`, and then call `ef.min_volatility()` for `"GMV"`, `ef.efficient_return(target_return=target_monthly_return)` for `"MV"` (no annualization is needed here: both `target_monthly_return` and the `expected_returns`/`cov_matrix` PyPortfolioOpt just computed are already at monthly frequency, because `frequency=12` was passed above — passing the raw 1%-per-month figure straight through is correct, unlike an earlier draft of this plan that annualized it to reconcile against PyPortfolioOpt's daily-derived defaults, which no longer applies now that the inputs are monthly throughout), or `ef.max_sharpe()` for `"MSR"`, then return `ef.clean_weights()` (a dict mapping ticker to weight, with near-zero weights rounded to exactly zero for readability). If `objective` is none of the three literal strings, raise `ValueError` naming the invalid value. If PyPortfolioOpt itself raises (for example, an infeasible target return for `efficient_return`, which is a documented real failure mode when the target exceeds what any combination of the candidate assets can achieve — a real risk with a monthly 1% target and a thin monthly sample), let that exception propagate with the original PyPortfolioOpt message intact rather than catching and hiding it — the caller (plan 6's interactive flow) is in a better position to decide whether to relax the target or drop a candidate than this function is.


Add a function `load_latest_prices(tickers: list[str], as_of: date, db_path: str = "data/portfolio.duckdb") -> pd.Series` that reads `plans/01_dataset.md`'s `prices` table (not the `returns` table) and, for each ticker, returns its most recent `adj_close` on or before `as_of`, as a `pd.Series` indexed by ticker. This is the one place in this plan that still touches raw daily prices rather than monthly returns, because `DiscreteAllocation` (used by `allocate_shares` below) needs a real per-share dollar price to convert weights into share counts, and a monthly return has no dollar unit to allocate against.


Add a function `allocate_shares(weights: dict[str, float], latest_prices: pd.Series, total_value: float) -> tuple[dict[str, int], float]` that constructs PyPortfolioOpt's `DiscreteAllocation(weights, latest_prices, total_portfolio_value=total_value)` and calls its `.greedy_portfolio()` method (PyPortfolioOpt's default, share-by-share greedy allocation algorithm, generally preferred over its alternative linear-programming-based allocator for typical portfolio sizes), returning the resulting `(allocation_dict, leftover_cash)` tuple exactly as PyPortfolioOpt produces it. `latest_prices` should come from `load_latest_prices` called with the same `as_of` date used for `load_returns_matrix`, so weights and allocation prices are anchored to the same date rather than mixing stale and fresh prices.


## Concrete Steps


Run every command from the repository root, with `plans/01_dataset.md` already implemented and `data/portfolio.duckdb` populated.


Step 1 — confirm the installed PyPortfolioOpt version's exact API surface matches this plan's assumptions (its API has been stable for years, but this check is cheap and removes any doubt):

    uv run python -c "
    import pypfopt
    print(pypfopt.__version__ if hasattr(pypfopt, '__version__') else 'version attr not found, check pyproject.toml pin instead')
    from pypfopt import EfficientFrontier, risk_models, expected_returns, DiscreteAllocation
    print('imports OK')
    "


Step 2 — implement the functions in Plan of Work, then run all three objectives against a real, small candidate list (5-10 large, liquid tickers is enough for a first sanity check; the full candidate-scanner output from `plans/04_candidate_scanner.md` can be used once this smaller check passes):

    uv run python -c "
    from datetime import date
    from src.optimizer.portfolio import load_returns_matrix, load_latest_prices, compute_weights, allocate_shares
    tickers = ['AAPL', 'MSFT', 'JPM', 'XOM', 'JNJ', 'PG', 'KO', 'DIS']
    returns = load_returns_matrix(tickers, date(2024, 3, 1))
    print('months available per ticker:', returns.count())
    for objective in ['GMV', 'MV', 'MSR']:
        weights = compute_weights(returns, objective)
        print(objective, {k: round(v, 3) for k, v in weights.items() if v > 0})
    latest = load_latest_prices(tickers, date(2024, 3, 1))
    alloc, leftover = allocate_shares(compute_weights(returns, 'MSR'), latest, 100000)
    print('allocation:', alloc, 'leftover cash:', round(leftover, 2))
    "

Expected: a per-ticker month count close to 60 for all eight tickers (all are long-established large caps with no history gaps expected in this window, and `plans/01_dataset.md`'s price history now reaches back to 2015-01-01, comfortably covering a 60-month lookback from a 2024-03-01 `as_of` date), and three distinct weight dicts (GMV's weights should look visibly more evenly spread and defensively tilted toward historically lower-volatility names like JNJ/PG/KO than MSR's, which should concentrate more; this is the qualitative signature of the three objectives actually differing, not a coincidence of this specific data), all weights summing to approximately 1.0 (allow for PyPortfolioOpt's own small rounding in `clean_weights()`), and a share allocation whose implied total dollar value (sum of `shares * latest price` for each ticker) is close to $100,000 minus the reported leftover cash.


## Validation and Acceptance


Run `uv run pytest tests/test_optimizer.py` and expect all tests to pass. Per `AGENTS.md`'s testing guidance, these tests use small, hand-built fixture returns series (for example, 2-3 synthetic tickers with a known, simple covariance structure — one pair of perfectly correlated series and one uncorrelated series is enough to make GMV's preference for the uncorrelated one checkable by hand) rather than live `yfinance`/database calls. Required cases: a test that `compute_weights` with `objective="GMV"` on a fixture where one ticker has near-zero historical variance assigns that ticker most of the weight; a test that an invalid `objective` string raises `ValueError`; a test that `load_returns_matrix`, given a fixture DuckDB (or a fixture DataFrame standing in for one, if the test isolates the drop-logic from the actual database read) where one ticker has only 10 months of returns against the `min_months=24` default threshold, drops that ticker and keeps the others (and a second case where a ticker has, say, 30 months — between the 24 minimum and the 60 target lookback — asserting it is kept using only those 30 months, not padded or dropped); a test that `allocate_shares` on a simple fixture (for example, one $100 stock, one $50 stock, weights 50/50, $1000 total) produces a share count whose implied dollar value is within one share's price of the target weight allocation, and that `leftover_cash` plus the allocated dollar value sums to (approximately) the original `total_value`.


Acceptance for this plan is: `uv run pytest tests/test_optimizer.py` passes; the Concrete Steps Step 2 transcript (captured for real) shows three visibly different, plausible weight sets and a share allocation whose implied value is close to the requested total.


## Idempotence and Recovery


`load_returns_matrix`, `load_latest_prices`, `compute_weights`, and `allocate_shares` are all pure functions of their inputs (aside from `compute_weights`'s and `allocate_shares`'s calls into PyPortfolioOpt, which are themselves deterministic given the same inputs, unlike the LLM calls in plans 2 and 3) — safe to call any number of times with no side effects and no state to clean up.


## Artifacts and Notes


(To be filled in with the real three-objective weight comparison and allocation transcript from Concrete Steps once this plan is executed.)


## Interfaces and Dependencies


This plan depends on `plans/01_dataset.md`'s `returns` table (for expected-return/covariance estimation) and `prices` table (for allocation pricing only) in `data/portfolio.duckdb`. This plan depends on `pyportfolioopt` (already in `pyproject.toml` as `pyportfolioopt`, imported in Python as `pypfopt`) for all optimization and allocation math.


At the end of this plan, the following must exist and be usable by later plans exactly as described:


In `src/optimizer/portfolio.py`: `def load_returns_matrix(tickers: list[str], as_of: date, lookback_months: int = 60, min_months: int = 24, db_path: str = "data/portfolio.duckdb") -> pd.DataFrame`; `def load_latest_prices(tickers: list[str], as_of: date, db_path: str = "data/portfolio.duckdb") -> pd.Series`; `def compute_weights(returns_matrix: pd.DataFrame, objective: str, target_monthly_return: float = 0.01) -> dict[str, float]`; `def allocate_shares(weights: dict[str, float], latest_prices: pd.Series, total_value: float) -> tuple[dict[str, int], float]`. Plan 6 (`plans/06_interactive_flow.md`) calls all four in sequence — `scan` (plan 4) to get candidates, `load_returns_matrix` and `compute_weights` (this plan) to get weights under whichever objective the user picks, `load_latest_prices` (this plan) to get allocation-ready prices, and `allocate_shares` (this plan) to turn weights into a concrete share order, re-running `compute_weights`/`allocate_shares` fresh whenever the user edits the candidate list. Plan 6 also reads `plans/01_dataset.md`'s `returns` table directly (not through this plan's functions) for its own backtest-scoring purpose, per that table's Interfaces documentation in `plans/01_dataset.md`.
