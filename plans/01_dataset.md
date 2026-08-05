# Build the point-in-time S&P 500 factor dataset


This ExecPlan is a living document. The sections `Progress`, `Surprises & Discoveries`, `Decision Log`, and `Outcomes & Retrospective` must be kept up to date as work proceeds. This plan must be maintained in accordance with `PLANS.md` at the repository root, which defines the required structure and writing style for every ExecPlan in this repository.


## Purpose / Big Picture


Every later piece of this project — the two screening agents, the candidate scanner, the optimizer — needs a table of numbers to look at: for every stock that was actually in the S&P 500 on a given date between 2020-01-01 and 2024-04-30, what was its size, its valuation, and its recent price momentum. This plan builds exactly that table and saves it to a local file so nothing downstream has to talk to Yahoo Finance or Wikipedia again. After this plan is done, a person can run one command and see, for example, that on 2021-06-01 Apple's log market value (`mve`) was some specific number, its book-to-market ratio (`bm`) was some other specific number, and its trailing twelve-month momentum (`mom12m`) was a third number — and that this number is reproducible by re-running the same command. That table, plus the exact list of tickers that counted as "the S&P 500" on that date, is the foundation everything else in this repository stands on.


We are reproducing the dataset construction described in the paper "Designing Agentic AI-Based Screening for Portfolio Investment" (Caner, Capponi, Sun, and Tan; arXiv:2603.23300, submitted March 2026; https://arxiv.org/abs/2603.23300). That paper builds its dataset from CRSP, Compustat, and WRDS — commercial, licensed academic data feeds that this repository does not have access to and will never have access to. This plan substitutes free, programmatically-reachable sources (Yahoo Finance via the `yfinance` Python package, and the English Wikipedia article on the S&P 500) for those licensed feeds, and documents every place where that substitution changes what the numbers mean or how trustworthy they are.


## Progress


- [x] Prototype: confirm what history `yfinance` actually returns for shares-outstanding and balance-sheet fields, for a small sample of tickers, back to 2020.
- [x] (2026-08-05) Build the point-in-time S&P 500 membership reconstruction from Wikipedia. Implemented in `src/dataset/membership.py`, table `sp500_membership` written to `data/portfolio.duckdb` (26,268 rows across 52 rebalance dates). Verified end-to-end: TSLA absent from the 2020-12-01 rebalance date, present from 2021-01-01, matching its real 2020-12-21 addition to the index.
- [x] (2026-08-05) Build the daily price history cache for every ticker that was ever a member in the window. Implemented in `src/dataset/prices.py`, table `prices` written to `data/portfolio.duckdb` (1,203,663 rows, 2015-01-02 through 2024-04-29, 525 of 580 ever-a-member tickers resolved; the other 55 are recorded in `unresolved_tickers` with a diagnostic reason). Verified end-to-end: `BRK.B` (a dotted ticker) is present under its original dotted symbol despite yfinance requiring the dashed `BRK-B` at the fetch boundary; AAPL's 2020-01-02 `close`/`adj_close` values are consistent with its real historical price once the split-adjustment discovery below is accounted for.
- [x] (2026-08-05) Compute `mve` (log market value of equity) per ticker per rebalance date. Implemented in `src/dataset/fundamentals.py`, writing a `factors` table to `data/portfolio.duckdb` with `mve`/`mve_z` populated and `bm`/`mom12m`/`bm_z`/`mom12m_z` as NULL placeholders pending their own checklist items (26,268 rows total, matching `sp500_membership`'s row count; 24,274 have a non-null `mve`, the rest are tickers whose price resolved but shares/splits data didn't reach far enough back, or that are in `unresolved_tickers`). This corrects a real defect in this plan's original `mve` formula — see Surprises & Discoveries below. Verified end-to-end: AAPL's mve on the 2020-06-01 (pre-split) rebalance date converts to a $1.35 trillion market cap, and on 2024-01-01 (post-split) to $2.96 trillion, both matching AAPL's real historical market cap; every row joined against `unresolved_tickers` has a null `mve`, not a crash; `mve_z` has cross-sectional mean ~0 and variance 1 on a spot-checked date.
- [ ] Compute `bm` (book-to-market ratio) per ticker per rebalance date.
- [ ] Compute `mom12m` (twelve-month momentum) per ticker per rebalance date.
- [ ] Standardize all three factors cross-sectionally (mean 0, variance 1) per rebalance date.
- [ ] Compute each ticker's trailing-one-month realized return for every month in the wider 2015-2024 sequence, for the shared `returns` table.
- [ ] Persist membership, prices, factors, and returns to DuckDB under `data/portfolio.duckdb`.
- [ ] Write `tests/test_dataset.py` covering the membership reconstruction, factor math, and monthly-return calculation against small, hand-checkable fixtures.


## Surprises & Discoveries


Prototype run (Concrete Steps Step 1, expanded to 4 large-cap tickers — AAPL, MSFT, XOM, GE — since a single ticker was not enough to tell a systematic API limitation from a ticker-specific gap), run 2026-08-05:

`get_shares_full(start='2015-01-01', end='2024-04-30')` behaves as expected: it honors the `end` argument and returns a real historical series reaching back to 2015-2016 for all four tickers checked (AAPL: 289 rows, 2015-10-28 to 2024-04-27; MSFT: 425 rows, 2015-10-23 to 2024-04-30; XOM: 359 rows, 2015-11-04 to 2024-04-30; GE: 336 rows, 2015-11-20 to 2024-04-30). `mve` should be computable across the full 2020-2024 rebalance window for large-cap tickers on the shares-outstanding side.

`Ticker.balance_sheet` and `Ticker.quarterly_balance_sheet` do **not** behave like `get_shares_full` — they take no `start`/`end` arguments at all, and both return only a short rolling window trailing from **today's real-world date**, not from any date this plan cares about. In this environment (system date 2026-08-05), `quarterly_balance_sheet` returned only 7 quarterly columns for every ticker tested, the oldest being `2024-12-31` (AAPL, MSFT, GE) or `2024-12-31` (XOM); `balance_sheet` (annual) returned only 4-5 columns, the oldest being `2021-09-30` (AAPL), `2022-06-30` (MSFT), `2022-12-31` (XOM), or `2021-12-31` (GE). Concretely: **`yfinance` currently exposes no balance-sheet data at all — quarterly or annual — for any rebalance date before roughly late 2021**, and only annual (not quarterly) data for 2022-2023 dates. This directly contradicts this plan's Step 1 expectation ("`bs.columns` should print roughly 4 to 5 quarterly report dates" spanning the years this plan needs); the columns exist, but they are the *wrong* years for a plan whose rebalance window starts 2020-01-01.

Consequence for `bm`: applying the "period ended at least three months before `d`" rule from `fundamentals.py`'s design, `bm` will be null for essentially every ticker on every rebalance date from 2020-01-01 through late 2021, and null for quarterly-lag precision (falling back to annual, coarser figures) through 2023. This is not a bug to fix in code — the plan already keeps nulls rather than imputing them — but it is a real, structural gap in how much of the 2020-2024 window this substitute data source can honestly cover for `bm`, and it should be called out explicitly wherever this project's README or later plans describe how faithfully the free-data pipeline reproduces the paper's CRSP/Compustat-backed factors. The line item for `bm` coverage should say "sparse-to-absent for 2020-2021" rather than implying full coverage.

The good news: the balance-sheet equity line item itself is easy to find across all four tickers — `Common Stock Equity` and `Stockholders Equity` (among others: `Total Equity Gross Minority Interest`, `Other Equity Adjustments`) both appear consistently, so `fundamentals.py`'s alias list does not need to be large, just correctly ordered by preference.

Membership reconstruction, run 2026-08-05: `pandas.read_html` on the live Wikipedia page returns 3 tables today (current constituents, the changes table, and an irrelevant `vte` navbox table), not 2 as this plan's prose originally implied — column-name matching (not table index) correctly skips the navbox. The changes table's columns parse as a 2-level MultiIndex, and its date column is literally named "Effective Date", not "Date" as this plan's Plan of Work section paraphrases it — `_locate_table`/`_normalize_changes_table` in `src/dataset/membership.py` match on substrings ("date") rather than exact names for exactly this reason. Missing sides of a change row (e.g. a row that only adds a ticker, with no corresponding removal) parse as float `NaN`, not empty string, so the backward-walk code checks with `pd.notna` rather than truthiness. `src/`, `tests/` had no `__init__.py` files and `pyproject.toml` had no `[tool.pytest.ini_options]` section — since this is the first code in the repository, `uv run pytest tests/test_dataset.py` could not have resolved `from src.dataset import membership` without adding `pythonpath = ["."]` under `[tool.pytest.ini_options]`; this is now in place for every future test file in the repo, not just this one.

Price history cache, run 2026-08-05: two Wikipedia-sourced tickers in the 580-ticker universe contain a literal dot (`BRK.B`, `BF.B`); Yahoo Finance's API requires a dash instead (`BRK-B`, `BF-B`). `src/dataset/prices.py` translates at the fetch boundary only (`to_yfinance_symbol`) and translates back before writing, so the `prices` table is keyed by the original dotted ticker — confirmed by querying `BRK.B` directly out of the written table. More importantly: **`yfinance.download`'s `Close` column is always split-adjusted, regardless of the `auto_adjust` flag** — this plan's Plan of Work section assumed `auto_adjust=False` would yield a raw, split-*un*adjusted close alongside a separately split-and-dividend-adjusted `Adj Close`, and that assumption is wrong. Evidence: AAPL split 4-for-1 on 2020-08-31; its real historical (unadjusted) close on 2020-01-02 was approximately $300.35; the `close` value this pipeline wrote for that row is `75.087502` — almost exactly $300.35 / 4. `adj_close` for the same row (`72.33387`) is slightly lower still, consistent with an additional dividend adjustment on top of the same split adjustment. So `close` and `adj_close` in the `prices` table differ from each other only by dividend adjustment, not by split adjustment — both are already split-adjusted, and there is no way to get a truly raw, split-unadjusted price out of `yfinance.download`. This matters for `fundamentals.py` (future work, not yet implemented): computing `mve` as shares-outstanding × price is only correct if the shares-outstanding series from `get_shares_full` is on the same split basis as the price series it's multiplied against; `fundamentals.py` must explicitly check (and document) whether `get_shares_full`'s historical share counts are already split-adjusted to match, or whether pre-split-date rows would otherwise silently compute a market value off by the split ratio. 55 of the 580 ever-a-member tickers ended up in `unresolved_tickers` (log evidence: `yfinance` reported "possibly delisted; no timezone found" or "no price data found" for each, batched across 6 fetches of ~100 symbols); spot-checking several by name (`TWTR`, `XLNX`, `VAR`, `TIF`, `FRC`, `SIVB`, `SBNY`, `PXD`, `HES`, `MRO`, `NBL`, `JNPR`, `WBA`, `JWN`) shows every one matches a real, well-documented acquisition, bankruptcy, or take-private event between 2020 and this environment's present date of 2026-08-05 — consistent with this plan's own anticipated `unresolved_tickers` category ("delisted long enough ago that Yahoo Finance dropped them"), not a fetch bug.

Full `unresolved_tickers` list from that same 2026-08-05 run, reviewed ticker-by-ticker afterward: `ABMD`, `ADS`, `AGN`, `ALXN`, `ANSS`, `ARNC`, `ATVI`, `CDAY`, `CERN`, `CMA`, `CTLT`, `CTRA`, `CTXS`, `CXO`, `DAY`, `DFS`, `DISCA`, `DISCK`, `DISH`, `DRE`, `ETFC`, `FBHS`, `FLIR`, `FLT`, `FRC`, `GPS`, `HBI`, `HES`, `HFC`, `HOLX`, `INFO`, `IPG`, `JNPR`, `JWN`, `K`, `KSU`, `MRO`, `MXIM`, `NBL`, `NLSN`, `PBCT`, `PXD`, `RE`, `RTN`, `SBNY`, `SEE`, `SIVB`, `TIF`, `TWTR`, `VAR`, `WBA`, `WCG`, `WLTW`, `XEC`, `XLNX` (55 total; every row carries the same generic reason string, since `yfinance.download` cannot distinguish "never resolved" from "resolved but empty in range" at the batch level — see `detect_unresolved_tickers` in `src/dataset/prices.py`). Most of the 55 check out as genuine, well-documented delistings (mergers, bankruptcies, take-privates) between 2020 and 2026. But at least eight — `GPS`, `HBI`, `IPG`, `ANSS`, `CMA`, `DFS`, `DISH`, `K` — appear to still trade under those exact tickers as of this writing, which does not fit the "delisted long enough ago that Yahoo dropped it" explanation this plan anticipated. The most likely alternative explanation is a transient fetch failure (rate-limiting or a dropped connection during one of the six ~100-symbol batches) rather than a real data gap, since `yfinance`'s own log output for the failing batches used the generic, error-code-free message "possibly delisted; no timezone found" for these alongside the genuinely-delisted names, and that message is documented (in `yfinance`'s own source) to fire on ordinary request failures, not only on confirmed delistings. This has not yet been re-verified with a retry — a follow-up worth doing before `fundamentals.py`/`momentum.py` treat this list as ground truth for "no usable data exists," since a handful of large, liquid, still-current tickers being wrongly marked unresolved would understate this project's factor coverage more than the genuinely-delisted names do.

`mve` computation, run 2026-08-05: this plan's original Plan of Work text for `mve` ("multiply [`get_shares_full`'s most recent shares outstanding] by the `adj_close` price... and take the natural log") is **wrong as written** and would have silently understated `mve` for any ticker that ever underwent a stock split, by exactly `log(cumulative_split_ratio)` for every rebalance date before that split. Cause: the prior discovery above (both `prices.close` and `prices.adj_close` are always split-adjusted relative to today's real-world date) combines with a second fact confirmed live here — `Ticker.get_shares_full` returns the RAW, point-in-time-real historical share count, which does NOT retroactively split-adjust. Evidence: AAPL's `get_shares_full` series jumps from ~4.28 billion to ~17.1 billion shares exactly at 2020-08-31 (AAPL's real 4-for-1 split date) — a genuine jump in the real outstanding-share count, not a smoothed series. Multiplying a pre-split raw share count by a post-split-basis price (as the original prose specified) understates market cap by the split ratio. Worse, this isn't limited to splits inside this project's 2015-2024 fetch window: NVDA split 10-for-1 on 2024-06-10 — after this project's price-fetch window ends (2024-04-30) but before this environment's real "today" (2026-08-05) — and NVDA's price rows for dates well before 2024-04-30 are already adjusted for that later split too, confirmed live, proving the adjustment basis is "whenever the code happens to run," not the fetch window's end date. The fix: `Ticker.splits` returns each ticker's complete, unbounded split history (confirmed live for AAPL: 5 splits 1987-2020; for NVDA: 6 splits 2000-2024, including the 2024-06-10 one) with no start/end restriction needed. `src/dataset/fundamentals.py`'s `cumulative_split_ratio_after(splits, as_of)` multiplies the raw shares-outstanding value by the product of every split ratio whose ex-date is strictly after the rebalance date, bringing it onto the same split-adjustment basis as the price series before the `mve = log(price * shares * ratio)` calculation. Sanity-checked by hand and by test (`test_compute_mve_reproduces_aapl_trillion_dollar_sanity_check`): AAPL pre-split raw shares (~4.275 billion) × cumulative ratio 4.0 × split-adjusted price ($75.09) ≈ $1.284 trillion, matching AAPL's real, well-documented January 2020 market cap — and confirmed again end-to-end against the live-built `factors` table for the 2020-06-01 and 2024-01-01 rebalance dates (see Progress above).


## Decision Log


- Decision: reconstruct point-in-time S&P 500 membership per rebalance date, rather than using today's constituent list for the entire 2020–2024 window.
  Rationale: using today's list for the whole backtest window would silently drop every company that left the index between 2020 and 2024 (survivorship bias), which would make Sharpe-ratio comparisons against the paper misleading. The user explicitly chose point-in-time reconstruction over the simpler current-list approach when asked.
  Date/Author: 2026-08-05, decided by repository owner during planning interview.
- Decision: source membership history from the English Wikipedia article "List of S&P 500 companies" rather than a paid index-membership feed.
  Rationale: it is the only free, structured, publicly reachable source of both a current constituent list and a dated history of additions and removals. It is a Wikipedia article and can be edited or restructured at any time; this plan's scraping code must fail loudly (not silently produce a wrong or empty table) if the expected table structure disappears.
  Date/Author: 2026-08-05, plan author.
- Decision: use PyPortfolioOpt's built-in risk models (sample covariance, Ledoit-Wolf shrinkage) later in the pipeline rather than the paper's five custom precision-matrix estimators. This does not affect this plan directly, but it does mean this plan only needs to produce clean per-stock return series, not any special covariance-ready format.
  Date/Author: 2026-08-05, decided by repository owner during planning interview; recorded here for context since it affects what "done" means for the price cache this plan produces.
  Rationale: matches the tool already cited in `README.md`'s acknowledgements and avoids building five separate statistical estimators from scratch.
- Decision: persist a `returns` table — one row per ticker per rebalance date, holding that ticker's realized return over the trailing month ending at that date — as part of this plan's output, rather than leaving "what did ticker X return in month Y" to be recomputed wherever it is needed.
  Rationale: plan 5's optimizer (`plans/05_optimizer_and_allocation.md`) needs monthly returns, not daily prices, to compute expected returns and a covariance matrix at the same monthly frequency this project rebalances at, and plan 6's backtest runner (`plans/06_interactive_flow.md`) needs the realized next-month return per candidate to score each month's chosen weights. The user explicitly asked whether this data would be stored once, shared by both consumers, rather than derived twice independently, and chose the shared-table approach when asked.
  Date/Author: 2026-08-05, decided by repository owner during a plan-review follow-up question; plan author.
- Decision: add `[tool.pytest.ini_options]` with `pythonpath = ["."]` to `pyproject.toml`, and empty `src/__init__.py` / `src/dataset/__init__.py` files, alongside implementing `src/dataset/membership.py`.
  Rationale: this is the first code written anywhere in the repository; without the repo root on `sys.path`, `uv run pytest tests/test_dataset.py` cannot resolve `from src.dataset import membership`. This is a one-time fix that benefits every later plan's test file, not something specific to membership reconstruction.
  Date/Author: 2026-08-05, plan author, discovered while implementing this plan's first Progress item.
- Decision: correct this plan's original `mve` formula to multiply raw shares outstanding by a cumulative split-adjustment ratio (derived from `Ticker.splits`) before combining with the split-adjusted price, rather than implementing the Plan of Work section's original text literally.
  Rationale: implementing the original text as written would have silently produced a systematically wrong (understated) `mve` for every ticker that ever split, for every rebalance date before that split — a material, not hypothetical, defect given how many large-cap constituents split during 2015-2026 (AAPL, TSLA, NVDA, and others). This was only discoverable by combining two facts, neither obvious from the ExecPlan text alone: `prices.py`'s price series is always split-adjusted (documented in that module's own Surprises & Discoveries entry), while `get_shares_full`'s share counts are not. Fixing the formula in code and documenting why here (rather than treating the original prose as authoritative) matches this plan's own instruction to resolve ambiguities and correct course explicitly, with the reasoning recorded for the next contributor.
  Date/Author: 2026-08-05, plan author, discovered while implementing this plan's `mve` Progress item.
- Decision: write the `factors` table now, with `mve`/`mve_z` populated and `bm`/`mom12m`/`bm_z`/`mom12m_z` as NULL placeholders, rather than deferring any DuckDB write until `bm`/`mom12m`/`build.py` all exist.
  Rationale: matches the standalone-runnable, write-its-own-table-immediately convention `membership.py` and `prices.py` already established (both are runnable via `python -m src.dataset.<module>` and produce an inspectable result on their own); `build.py` (future work) is already documented to `DROP TABLE IF EXISTS factors` and recreate it wholesale once `bm`/`mom12m` exist, so this table is guaranteed to be safely and completely overwritten later without `build.py` needing to know this module ran standalone first.
  Date/Author: 2026-08-05, plan author, discovered while implementing this plan's `mve` Progress item.


## Outcomes & Retrospective


(To be filled in once this plan is implemented and validated.)


## Context and Orientation


This repository is a Python 3.12 project managed with `uv` (see the root `pyproject.toml` and `uv.lock`). All commands in this plan assume the working directory is the repository root, `/app/agentic_portfolio` (or wherever this repository is checked out — the pattern is the same), and that dependencies are already installed, matching this repository's stated setup (`AGENTS.md`: "Set up dependencies are already done and provided as the container"). If a command below fails with `ModuleNotFoundError`, run `uv sync` first.


There is no code in this repository yet. `src/` and `tests/` are empty directories. This plan creates the first real code: a `src/dataset/` package.


Three terms of art recur throughout this plan and the paper it reproduces:


"Log market value of equity" (`mve`) means the natural logarithm of a company's market capitalization — its share price multiplied by the number of shares outstanding — on a given date. Logging it is a standard finance convention because market cap spans many orders of magnitude (a small company might be worth $2 billion, a large one $2 trillion) and the logarithm compresses that range into something closer to a normal distribution, which the screening agents in later plans will reason about more sensibly than raw dollar figures.


"Book-to-market ratio" (`bm`) means a company's book value of equity (roughly, total assets minus total liabilities, as reported on its balance sheet — an accounting snapshot of what the company's own books say it is worth) divided by its market value of equity (what public traders are currently paying for it). A high `bm` means the company is "cheap" relative to its own accounting books (traders are not paying much for each dollar of book equity); a low `bm` means it is "expensive" by that same yardstick. This plan must not divide a book value by a market value taken from before the book value was actually known to the public — the "Book value timing" paragraph below explains exactly how this plan avoids that mistake.


"Twelve-month momentum" (`mom12m`) means the cumulative stock return from 12 months before the rebalance date to 1 month before the rebalance date — that is, the most recent month is deliberately skipped. This is the standard academic ("Fama-French style") definition of momentum; skipping the most recent month avoids conflating momentum with short-term reversal effects that are a separate, unrelated phenomenon. Concretely, for a rebalance date of 2022-07-01, `mom12m` is the compounded return from the close on or near 2021-07-01 to the close on or near 2022-06-01.


"Rebalance date" means one of the fixed calendar dates on which this project recomputes membership, factors, and (in later plans) agent signals and portfolio weights. This plan fixes rebalance dates at the first trading day of every month from January 2020 through April 2024 inclusive — 52 dates in total. Monthly is chosen because the paper's own sentiment agent (FinBERT, replaced by LLM-F in this project's plan 3) is rerun monthly, and the optimizer plan (plan 5) needs a cadence to rebalance at; there is no reason to compute factors more finely than the coarsest thing that consumes them.


This plan also uses a second, wider date sequence that is easy to conflate with the 52 rebalance dates above but serves a different purpose: the first trading day of every month from January 2015 through April 2024 inclusive — 112 months in total — used only by the `prices` and `returns` tables described below, not by `sp500_membership` or `factors`. The reason this wider range exists at all is plan 5 (`plans/05_optimizer_and_allocation.md`): its portfolio optimizer estimates expected returns and a covariance matrix from each candidate ticker's trailing 60 months of realized monthly returns. For the earliest backtest rebalance, 2020-01-01, a 60-month trailing window reaches back to 2015-02-01 — dates that fall well before the 2020-2024 backtest window itself. If the `returns` table only had rows for the 52 rebalance dates, there would be no return observations at all for most of that 60-month lookback at the start of the backtest. The paper this project reproduces uses an even longer 180-month (15-year) rolling window for the same estimation step; this project deliberately uses a shorter 60-month window instead (with a 24-month minimum below which a ticker is dropped from a given month's optimization) as a documented, practical deviation — matching the paper's 180-month window would require fetching and validating roughly 19 years of price history for this project's full ticker universe, a cost judged not worth it here. This shorter-window choice is recorded in `plans/05_optimizer_and_allocation.md`'s Decision Log; this plan's job is only to make sure enough raw price and return history actually exists to support it.


"Standardized" means transformed so that, across all stocks present on a given rebalance date, the factor's cross-sectional mean is 0 and its cross-sectional variance is 1 (subtract the mean, divide by the standard deviation, computed freshly for each rebalance date across that date's membership). The paper states its three factors are "standardized to have mean 0 and variance 1" before being shown to the LLM-S screening agent; this plan produces both the raw and the standardized values so that plan 2 (LLM-S) can show the agent standardized numbers while this table remains independently checkable in real units.


## Plan of Work


Create a new package at `src/dataset/`, with these modules:


`src/dataset/membership.py` reconstructs point-in-time S&P 500 membership. It fetches the Wikipedia article "List of S&P 500 companies" (`https://en.wikipedia.org/wiki/List_of_S%26P_500_companies`), which contains two tables: a current-constituents table (columns include `Symbol`, `Security`, and `Date added`) and a table titled "Selected changes to the list of S&P 500 components" (columns: `Date`, `Added Ticker`, `Added Security`, `Removed Ticker`, `Removed Security`, `Reason`). Both tables can be parsed with `pandas.read_html`, which returns a list of DataFrames for every `<table>` on the page — the code must locate the right two tables by inspecting their column names (matching on the presence of `Symbol`/`Date added` for the first, and `Date`/`Added`/`Removed` for the second) rather than by a hardcoded table index, because Wikipedia can and does reorder page sections.


The reconstruction algorithm walks backward in time from today's constituent list. For a target rebalance date `d`, start with the full current-membership set. Then, for every row in the changes table whose `Date` is strictly after `d`, sorted from most recent to oldest, undo that change: if the row added a ticker after `d`, remove that ticker from the working set (it was not yet a member as of `d`); if the row removed a ticker after `d`, add that ticker back into the working set (it had not yet been removed as of `d`). After applying every change row newer than `d`, the working set is the S&P 500's membership as of `d`. Repeat this independently for each of the 52 rebalance dates (recomputing from the full current list each time, rather than incrementally, keeps the logic simple to verify and cheap enough to matter only in the tens-of-milliseconds range).


Write this membership table to a DuckDB table named `sp500_membership` with columns `rebalance_date DATE`, `ticker VARCHAR`, `security VARCHAR`. A row's presence means that ticker was a member as of that rebalance date.


`src/dataset/prices.py` downloads and caches daily adjusted-close prices for the union of every ticker that appears in `sp500_membership` across all 52 dates (a company that was only briefly a member still needs a price history, both for momentum near its membership window and because a ticker delisted mid-window still needs whatever prices exist up to delisting). Use `yfinance.download` (batched, since `yfinance` supports fetching many tickers in one call) for the date range 2015-01-01 (60 months before the earliest 2020-01-01 rebalance date, which both covers plan 5's 60-month returns lookback and comfortably subsumes the one-year buffer the `mom12m` factor needs) through 2024-04-30. Store the result in a DuckDB table `prices` with columns `date DATE`, `ticker VARCHAR`, `close DOUBLE`, `adj_close DOUBLE`. Tickers that `yfinance` cannot resolve at all (delisted long enough ago that Yahoo Finance dropped them, or that changed ticker symbols in a way this plan's simple lookup does not follow) must be recorded, not silently dropped — write their symbols to a DuckDB table `unresolved_tickers` with columns `ticker VARCHAR`, `reason VARCHAR`, so the gap is visible and auditable rather than hidden inside a smaller-than-expected membership count.


`src/dataset/fundamentals.py` computes `mve` and `bm`. For `mve` on rebalance date `d` for ticker `t`: call `yfinance.Ticker(t).get_shares_full(start=..., end=d)` to get a time series of historical shares outstanding, take the most recent value on or before `d`, multiply by the `adj_close` price on or nearest before `d` from the `prices` table, and take the natural log. For `bm` on rebalance date `d` for ticker `t`: call `yfinance.Ticker(t).quarterly_balance_sheet` (or `.balance_sheet` for annual figures if quarterly is unavailable for that ticker) to get reported common stockholders' equity ("Common Stock Equity" or the closest available line item — `yfinance`'s exact label has changed across versions, so the code must check for a small list of known aliases and fail loudly, naming the ticker and the columns it did find, if none match) for the most recent statement whose reporting period ended at least three months before `d`. That three-month lag is the "Book value timing" safeguard: a company's 10-K or 10-Q for a fiscal period is not filed with the SEC, and so not knowable to any trader or agent, until roughly one to three months after that period ends; using the balance sheet from the period that ended immediately before `d` without a lag would let the factor "see the future" relative to what a real screening process could have known on `d`. Divide that book value by the market value of equity (unlogged, i.e. price times shares outstanding) as of `d` to get `bm`.


`src/dataset/momentum.py` computes `mom12m` directly from the `prices` table: for rebalance date `d` and ticker `t`, find the `adj_close` nearest to (and not after) the date 12 months before `d`, and the `adj_close` nearest to (and not after) the date 1 month before `d`, and compute `(price_at_1_month_before / price_at_12_months_before) - 1`.


`src/dataset/returns.py` computes each ticker's realized return over the trailing month ending at a given month, using the same nearest-available-price lookup style as `momentum.py`: for month-end anchor date `d` and ticker `t`, find the `adj_close` nearest to (and not after) `d`, and the `adj_close` nearest to (and not after) the date one calendar month before `d`, and compute `(price_at_d / price_at_one_month_before) - 1`. This is deliberately the same kind of calculation as one leg of `mom12m`, just anchored differently (ending at `d` rather than ending one month before `d`) — the two live in separate modules because they serve different purposes (`mom12m` is a screening factor shown to LLM-S; this trailing-one-month return is a realized outcome consumed by the optimizer and the backtest scorer) and because keeping them separate means a change to one calculation can never accidentally alter the other. Unlike `momentum.py` and `fundamentals.py`, which only ever run against the 52 rebalance dates, `returns.py` is called once for every one of the 112 months in the wider 2015-01-01–2024-04-30 sequence defined in Context and Orientation above, for every ticker in the "ever a member 2020-2024" universe `prices.py` fetched — not only for the months a ticker was actually an S&P 500 member, since a candidate's pre-membership return history still matters to plan 5's trailing covariance window. One specific null is expected and harmless: the very first month of this wide sequence, 2015-01-01, will always get a null `monthly_return` for every ticker, by construction, since computing it would need a price dated 2014-12-01 — one month before this plan's price fetch even starts. This is not a data-quality problem to chase down; it simply reflects that the wide sequence's first month has no month before it within the fetched range. It is also never actually needed: plan 5's smallest lookback window (the 24-month minimum) for the earliest possible backtest rebalance, 2020-01-01, only ever reaches back to the 2018-02-01 monthly return (24 months before 2020-01, which itself needs a price dated 2018-01-01, well within range), and its full 60-month window for that same rebalance reaches back only to the 2015-02-01 monthly return (needing a price dated 2015-01-01, exactly the first date this plan fetches) — so the 2015-01-01 boundary null sits one month earlier than anything either window ever reads.


`src/dataset/build.py` is the entry point that ties the above together: for each of the 52 rebalance dates, for each ticker in that date's membership, compute `mve`, `bm`, and `mom12m`; separately, for each of the 112 months in the wider date sequence, for every ticker in the full universe, compute the trailing-one-month return. Within each of the 52 rebalance dates, compute the cross-sectional mean and standard deviation of each of the three factors across all tickers present that date, and add three more columns (`mve_z`, `bm_z`, `mom12m_z`) holding the standardized values. Write the factor result to a DuckDB table `factors` with columns `rebalance_date DATE`, `ticker VARCHAR`, `mve DOUBLE`, `bm DOUBLE`, `mom12m DOUBLE`, `mve_z DOUBLE`, `bm_z DOUBLE`, `mom12m_z DOUBLE`, and write the return result to a separate DuckDB table `returns` with columns `rebalance_date DATE`, `ticker VARCHAR`, `monthly_return DOUBLE` — despite the column name `rebalance_date`, in the `returns` table this column holds one of the 112 wider-sequence months, not necessarily one of the 52 narrower rebalance dates; the column is named the same way in both tables only because it holds the same kind of value (a first-trading-day-of-month date), not because the two tables share the same set of dates. A row with a null in any of `mve`, `bm`, `mom12m`, or `monthly_return` (because, for example, `yfinance` had no shares-outstanding history for that ticker on that date, or no price existed one month prior) is kept in its table with the null preserved, not dropped and not silently imputed — later plans decide how to handle missing values, and this plan's job is only to be honest about what is and is not known. Because the `returns` table spans 112 months for the full ticker universe rather than 52 months for each date's membership only, expect it to end up with meaningfully more rows than `factors` — this is expected, not a sign of a bug.


All of this writes to a single DuckDB file at `data/portfolio.duckdb` (the `data/` directory is already listed in `.gitignore`, so this file is never committed; anyone re-running this plan's build step regenerates it locally). `build.py` must be idempotent: running it twice produces the same tables (achieved simply by having it `DROP TABLE IF EXISTS` each of `sp500_membership`, `prices`, `unresolved_tickers`, `factors`, and `returns` before recreating them, rather than appending).


## Concrete Steps


Run every command from the repository root.


Step 1 — prototype what `yfinance` actually returns, before writing any pipeline code, since the entire rest of this plan depends on the answer:

    uv run python -c "
    import yfinance as yf
    t = yf.Ticker('AAPL')
    shares = t.get_shares_full(start='2015-01-01', end='2024-04-30')
    print('shares outstanding rows:', len(shares))
    print(shares.head())
    print(shares.tail())
    bs = t.quarterly_balance_sheet
    print('quarterly balance sheet columns (dates):', list(bs.columns))
    print([idx for idx in bs.index if 'equity' in idx.lower() or 'Equity' in idx])
    "

Expected: `shares` should print a pandas Series (or DataFrame) with a datetime index spanning at least back to 2019, and `bs.columns` should print roughly 4 to 5 quarterly report dates. If `shares` is empty or `bs.columns` shows fewer than 4 quarters, record that in Surprises & Discoveries immediately — it changes how far back this plan can honestly claim to compute `mve`/`bm`, and that limitation must be written down before proceeding, not discovered later.


Step 2 — after the prototype confirms feasibility, implement `src/dataset/membership.py` and manually check its output for one known historical fact: Tesla was added to the S&P 500 on 2020-12-21. Running the membership reconstruction for rebalance date 2020-12-01 must NOT include `TSLA`, and for rebalance date 2021-01-01 must include it. This is a fast, human-checkable correctness test independent of any code this plan writes to automate the same check.


Step 3 — implement `src/dataset/prices.py`, `src/dataset/fundamentals.py`, `src/dataset/momentum.py`, `src/dataset/returns.py`, and `src/dataset/build.py`, then run:

    uv run python -m src.dataset.build

Expected output: log lines reporting, for each of the 52 rebalance dates, how many tickers were in membership and how many of those got a complete (non-null) factor row; separately, log lines reporting, for each of the 112 wider-sequence months, how many tickers got a complete (non-null) return row; followed by a final line reporting the total row count written to the `factors` table and the `returns` table and the path to `data/portfolio.duckdb`.


Step 4 — inspect the result directly:

    uv run python -c "
    import duckdb
    con = duckdb.connect('data/portfolio.duckdb')
    print(con.execute('select count(*) from sp500_membership').fetchone())
    print(con.execute('select count(*) from factors').fetchone())
    print(con.execute('select count(*) from returns').fetchone())
    print(con.execute('select min(rebalance_date), max(rebalance_date) from returns').fetchone())
    print(con.execute(\"select * from factors where ticker = 'AAPL' order by rebalance_date limit 5\").fetchdf())
    print(con.execute(\"select * from returns where ticker = 'AAPL' order by rebalance_date limit 5\").fetchdf())
    "

Expected: nonzero counts, with the `returns` table's row count meaningfully larger than `factors`'s (112 months of full-universe coverage versus 52 months of membership-only coverage) and its min/max dates spanning from on or near 2015-01-01 to 2024-04-30, not just the 2020-2024 backtest window; and the `AAPL` rows showing plausible numbers — `mve` somewhere around 27 to 29 (Apple's market cap was in the trillions of dollars across this window; ln(trillions) lands in that range), `bm` a small positive number well under 1 (Apple is a "growth" stock, historically priced far above its book value), `mom12m` varying month to month but generally in the range of roughly -0.3 to +0.6, and `monthly_return` varying month to month but generally in the range of roughly -0.2 to +0.2 (a single month's move is smaller in magnitude than a trailing-twelve-month cumulative momentum figure).


Step 5 — spot-check one specific, hand-verifiable monthly return: Apple's stock split 4-for-1 on 2020-08-31, and its price (split-adjusted) rose sharply over calendar 2020. Query `returns` for `ticker = 'AAPL'` and `rebalance_date = '2020-08-01'` and confirm the sign and rough magnitude of `monthly_return` against Apple's publicly known price move from early July 2020 to early August 2020 (a well-documented sharp rally ahead of the split announcement) — this does not need to match to the decimal, but a wildly different sign or an order-of-magnitude mismatch indicates a bug in the date-lookup logic in `src/dataset/returns.py`, most likely an off-by-one-month error or a mismatched use of split-adjusted versus unadjusted price.


## Validation and Acceptance


Run `uv run pytest tests/test_dataset.py` and expect all tests to pass. The test file must include at least:


A test that feeds `src/dataset/membership.py`'s change-application function a small, hand-written fixture of 3-4 change rows (not the real Wikipedia table) and asserts the resulting membership set for a specific target date matches a hand-computed expected set — this isolates the backward-walk algorithm from any dependency on Wikipedia's current page contents, so the test stays deterministic and does not make network calls.


A test that feeds `src/dataset/momentum.py`'s momentum function a small fixture price series with known values and asserts the computed `mom12m` matches a value computed by hand.


A test that feeds `src/dataset/build.py`'s standardization step a small fixture of raw factor values for 3-4 tickers on one date and asserts the resulting `_z` columns have cross-sectional mean approximately 0 and variance approximately 1.


A test that feeds `src/dataset/returns.py`'s monthly-return function a small fixture price series with known values and asserts the computed `monthly_return` matches a value computed by hand — structurally the same test shape as the existing `mom12m` fixture test, just anchored at a different pair of dates.


Per `AGENTS.md`'s testing guidelines, none of these tests may call `yfinance` or fetch the live Wikipedia page; all use in-memory fixtures. A separate, explicitly-marked integration check (for example `tests/test_dataset_integration.py`, run manually and not part of the default `pytest tests/test_*.py` sweep, or marked with `@pytest.mark.integration` if the project later adopts pytest markers) may hit the real network to validate the end-to-end build, matching AGENTS.md's "Keep external API calls out of unit tests unless a test is explicitly marked as an integration check."


Acceptance for this plan is: `uv run python -m src.dataset.build` completes without error and reports nonzero factor and returns row counts for all 52 rebalance dates; `data/portfolio.duckdb` exists and the manual inspection queries in Concrete Steps Step 4 return plausible Apple numbers as described; `uv run pytest tests/test_dataset.py` passes.


## Idempotence and Recovery


`src/dataset/build.py` drops and recreates all five of its DuckDB tables (`sp500_membership`, `prices`, `unresolved_tickers`, `factors`, `returns`) on every run, so re-running it after a partial or failed run is always safe — there is no accumulation of duplicate or stale rows to clean up. If a run fails partway through (for example, a network timeout while fetching prices for one ticker), the DuckDB file may be left in a state with some tables built and others missing; simply re-run `uv run python -m src.dataset.build` from scratch. Because `data/` is gitignored, deleting `data/portfolio.duckdb` entirely and re-running the build is always a safe way to start clean.


## Artifacts and Notes


(To be filled in with real transcript excerpts — for example, the actual `yfinance` prototype output from Concrete Steps Step 1 — once this plan is executed.)


## Interfaces and Dependencies


This plan depends on: `yfinance` (already in `pyproject.toml`) for prices, shares outstanding, and balance sheet data; `pandas` (already present) for `read_html` table parsing and general tabular manipulation; `duckdb` (already present) as the on-disk storage format for everything this plan produces.


At the end of this plan, the following must exist and be usable by later plans exactly as described:


In `src/dataset/build.py`, a function:

    def build_all(db_path: str = "data/portfolio.duckdb") -> None

which performs the full pipeline described in Plan of Work and leaves the DuckDB file in the state described there.


The DuckDB file at `data/portfolio.duckdb` (path configurable, but this is the default every later plan assumes unless told otherwise) must contain:


Table `sp500_membership(rebalance_date DATE, ticker VARCHAR, security VARCHAR)` — one row per ticker per rebalance date that ticker was a member.


Table `factors(rebalance_date DATE, ticker VARCHAR, mve DOUBLE, bm DOUBLE, mom12m DOUBLE, mve_z DOUBLE, bm_z DOUBLE, mom12m_z DOUBLE)` — one row per ticker per rebalance date, with nulls where a factor could not be computed. Plan 2 (`plans/02_llm_s_agent.md`) reads the `_z` columns to show LLM-S the standardized cross-sectional distribution; plan 5 (`plans/05_optimizer_and_allocation.md`) reads raw `mve` only incidentally, if at all, since the optimizer works from prices, not factors.


Table `prices(date DATE, ticker VARCHAR, close DOUBLE, adj_close DOUBLE)` — daily prices for every ticker that was ever a member in the window, from 2015-01-01 through 2024-04-30 (five years before the 2020-2024 backtest window starts, to support plan 5's 60-month returns lookback). Plan 5's optimizer reads this table directly only to price share allocations (the single latest price per ticker); plan 3 (`plans/03_llm_f_agent.md`) reads it incidentally if it needs a reference price alongside news headlines.


Table `returns(rebalance_date DATE, ticker VARCHAR, monthly_return DOUBLE)` — one row per ticker per month in the wider 112-month sequence (2015-01-01 through 2024-04-30, not only the 52 narrower backtest rebalance dates), holding that ticker's realized return over the trailing month ending at that date, with nulls where no price existed one month prior to compute a return from. This wider date coverage exists specifically so plan 5's 60-month trailing lookback has real observations to draw on even for the earliest 2020-2021 backtest rebalances, whose lookback windows reach back before the backtest window itself starts. This is the shared source of truth both plan 5 (`plans/05_optimizer_and_allocation.md`, for expected-return and covariance-matrix estimation at monthly frequency, using its own shorter 60-month/24-month-minimum window — a documented, deliberate deviation from the paper's own 180-month window, recorded in that plan's Decision Log) and plan 6 (`plans/06_interactive_flow.md`, for scoring each backtest month's realized portfolio return, which only ever needs single months within the narrower 2020-2024 window) read from, so that "what did ticker X return in month Y" is computed exactly once in this repository.


Table `unresolved_tickers(ticker VARCHAR, reason VARCHAR)` — tickers this plan could not fetch prices for at all. Any later plan that iterates over `sp500_membership` should treat a ticker also appearing in `unresolved_tickers` as having no usable data, not as an error to crash on.
