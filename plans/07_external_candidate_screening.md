# Screen candidate stocks and ETFs outside the S&P 500 universe with LLM-S's rule


This ExecPlan is a living document. The sections `Progress`, `Surprises & Discoveries`, `Decision Log`, and `Outcomes & Retrospective` must be kept up to date as work proceeds. This plan must be maintained in accordance with `PLANS.md` at the repository root. This plan builds on `plans/01_dataset.md` (for the `factors` table and the raw-factor-fetching functions it reuses) and `plans/02_llm_s_agent.md` (for the `ScreeningRule` schema and the `apply_rule` function it reuses unchanged) — both checked into this repository.


## Purpose / Big Picture


After this plan is done, a person who has already generated an LLM-S rule from the S&P 500's data for a given month (`plans/02_llm_s_agent.md`'s `generate_rule`) can ask a new question the earlier plans never answered: "should this stock or ETF, which is not and never was an S&P 500 member, be pulled into my portfolio's candidate set?" They compute that candidate's own `mve` (log market value of equity), `bm` (book-to-market ratio), and `mom12m` (12-month momentum), pass them into one new function together with the already-generated rule and the rebalance date the rule was written against, and get back `"buy"`, `"sell"`, `"hold"`, or `"insufficient_data"` — the same three-way signal LLM-S already produces for in-universe stocks, now usable for a stock or ETF entirely outside that universe.


LLM-S's rule (see `plans/02_llm_s_agent.md`) is written entirely in terms of standardized z-scores — each factor rescaled, across that rebalance date's ~500 S&P 500 members, to cross-sectional mean 0 and variance 1. "Cross-sectional" here means: computed across every company at one point in time, not across one company's history over time. A z-score of 1.0 for `mve` means "one standard deviation above the average S&P 500 company's size that month," which is meaningless without knowing what that average and standard deviation actually were. A candidate outside the S&P 500 was never part of that average-and-standard-deviation calculation, so applying the rule to it requires standardizing its raw factor values against the *same* S&P 500 reference numbers the rule's author (the LLM) actually saw — not against a new calculation that includes the candidate itself, which would change meaning every time a different candidate is tested and cannot even be computed for a single ticker in isolation (a standard deviation needs a population of more than one).


This plan's central discovery, recorded in detail in the Decision Log below, is that the S&P 500's reference mean and standard deviation for any historical rebalance date do not need to be stored anywhere new: `plans/01_dataset.md`'s `factors` table already persists every member's *raw* `mve`, `bm`, `mom12m` alongside their z-scores, and the mean/standard deviation can be recomputed from those raw columns on demand, exactly reproducing the numbers originally used.


## Progress


- [x] (2026-08-06) Add `get_factor_reference_stats(rebalance_date, db_path) -> dict[str, tuple[float, float]]` to `src/dataset/fundamentals.py`, recomputing the S&P 500 universe's mean/std for `mve`, `bm`, `mom12m` on demand from the `factors` table's persisted raw columns via one SQL aggregate query. Verified against a hand-built 3-row fixture `factors` table (raw values chosen so mean/std are easy to check by hand: `mve = [1, 2, 3]` giving `(mean=2.0, std=1.0)`) that the returned numbers match exactly; verified `ValueError` is raised (naming the offending date) when no rows exist for the requested `rebalance_date`.
- [x] (2026-08-06) Add `src/agents/external_screen.py` with `compute_raw_factors_for_ticker` (off-index individual stocks, reusing the exact per-ticker fetch/compute functions `plans/01_dataset.md`'s `build_factors`/`build_momentum_factors` already call per row — no new fetch logic), `compute_raw_factors_for_etf` (ETFs, using the AUM/price-to-book proxies described in the Decision Log), `standardize_raw_factors` (pure z-score arithmetic against `get_factor_reference_stats`'s output), and `screen_external_candidate` (ties the above together and calls the existing, unmodified `apply_rule` from `plans/02_llm_s_agent.md`). Verified end-to-end with real network calls against the real `2023-12-01` snapshot (the same as-of date `plans/02_llm_s_agent.md`'s recorded 2024 rule was generated against): GameStop (`GME`, an off-index individual stock) computed real raw factors (`mve=22.26`, `bm=0.28`, `mom12m=-0.51`) that standardized to `mve_z=-1.80`, `bm_z=-0.10`, `mom12m_z=-1.97` against the real S&P 500 reference stats for that date, correctly triggering the recorded rule's `sell_condition` (`mom12m < -0.85`); the ETF `PFFA` (AUM $2.48B, P/B 2.58, both real figures) computed `mve=21.63` (`log(2.48e9)`), `bm=0.388` (`1/2.58`), standardizing to `mve_z=-2.36`, `bm_z=-0.04`, `mom12m_z=0.37`, correctly resolving to `"hold"` (neither the recorded buy nor sell condition fires).
- [x] (2026-08-06) Write `tests/test_external_screen.py` covering `get_factor_reference_stats` (matches hand-computed mean/std; raises on an unknown date) and `standardize_raw_factors`/`screen_external_candidate` (matches manual z-score arithmetic; omits a missing or zero-std factor rather than inventing a placeholder; returns `"insufficient_data"`, not a crash or a guessed signal, when a rule's condition needs a factor the candidate doesn't have) against small hand-built fixtures — no network calls, matching `AGENTS.md`'s testing guidance. 7 tests, all passing (`uv run pytest tests/test_external_screen.py`); full repo suite (`uv run pytest tests/`) still passes.
- [ ] Measure whether `yfinance`'s `Ticker.info` actually exposes usable AUM/total-net-assets and price-to-book fields for ETFs live, so a future pass could auto-fetch `compute_raw_factors_for_etf`'s two proxy inputs instead of requiring the caller to look them up manually (e.g. from Morningstar, as in this plan's own PFFA example) and pass them in by hand. Not required for this plan's acceptance — `compute_raw_factors_for_etf` already works correctly with manually-supplied numbers — but worth measuring before deciding whether to build an auto-fetch path.


## Surprises & Discoveries


- Discovery (2026-08-06): the mean/standard deviation used to build LLM-S's z-scored factors were never a design gap requiring new storage — `plans/01_dataset.md`'s `factors` table already stores each S&P 500 member's *raw* `mve`, `bm`, `mom12m` per rebalance date (confirmed against the live schema: columns `rebalance_date, ticker, mve, bm, mom12m, mve_z, bm_z, mom12m_z`), not just the standardized `_z` columns. `src/dataset/fundamentals.py`'s `add_cross_sectional_z` computes mean/std via `pandas.groupby("rebalance_date").transform(...)` and discards them once the `_z` column is written, but since the raw inputs to that exact computation are themselves persisted, the same mean/std can be recomputed on demand at any later time via a single SQL aggregate query. Verified live: DuckDB's `STDDEV_SAMP` (sample standard deviation, `ddof=1`) matches pandas' default `.std()` convention exactly, and a hand-computed 3-row fixture (`mve = [1, 2, 3]`) reproduces `(mean=2.0, std=1.0)` bit-for-bit through `get_factor_reference_stats`.
- Discovery (2026-08-06): the real S&P 500 reference stats for `2023-12-01` (the as-of date `plans/02_llm_s_agent.md`'s recorded 2024 rule was generated against) are `mve: (mean=24.28, std=1.12)`, `bm: (mean=0.47, std=1.78)`, `mom12m: (mean=-0.045, std=0.237)` — GameStop's real raw `mve=22.26` (roughly a $4.7 billion market cap that month) standardizes to `mve_z=-1.80`, meaning GameStop was nearly two standard deviations *smaller* than the average S&P 500 company that month, which matches the real-world fact that GameStop was never an S&P 500 constituent and is a mid-cap relative to that index.
- Discovery (2026-08-06): the approved design originally planned `compute_raw_factors_for_ticker` for `src/dataset/fundamentals.py` (alongside `get_factor_reference_stats`), but it was implemented in `src/agents/external_screen.py` instead. Reason: computing `mom12m` for an arbitrary ticker requires `src/dataset/momentum.py`'s `compute_mom12m`, and `momentum.py` already imports from `fundamentals.py` — adding a `fundamentals.py -> momentum.py` import for this one function would create a circular import between the two dataset modules. Placing the per-ticker orchestration wrapper in `external_screen.py` (which imports from both `fundamentals.py` and `momentum.py`, sitting above both in the dependency graph) avoids the cycle without restructuring either existing module.


## Decision Log


- Decision: recompute the S&P 500 universe's per-rebalance-date factor mean/std on demand from the `factors` table's already-persisted raw columns (`get_factor_reference_stats`), rather than adding a new table or file that stores mean/std as a separate artifact.
  Rationale: the raw values `add_cross_sectional_z` computes mean/std from are themselves already stored in the `factors` table (`mve`, `bm`, `mom12m`, distinct from the `_z`-suffixed standardized columns) — a second, separately-persisted copy of statistics derivable from data already on disk would only add a synchronization risk (the stored stats silently going stale if `factors` were ever rebuilt with different inputs) for no benefit, since DuckDB's `STDDEV_SAMP` reproduces pandas' `.std()` exactly and the underlying raw data never changes once written for a historical date.
  Date/Author: 2026-08-06, decided during planning interview, after confirming via two research passes (one over all six existing plan documents, one over the actual `src/dataset/fundamentals.py` implementation) that no prior plan mentioned or built this, and that the raw columns needed to make on-demand recomputation possible were already present.
- Decision: for ETFs, use `mve = log(aum)` (log of the fund's total net assets, supplied by the caller) and `bm = 1 / price_to_book` (the inverse of the fund's aggregate portfolio price-to-book ratio, also caller-supplied) as explicit proxies for the single-company concepts `mve`/`bm` don't literally apply to for a fund.
  Rationale: user's explicit direction, worked through with a real example (the ETF `PFFA`: Morningstar reports its total assets as $2.48 billion and its portfolio price-to-book ratio as 2.58) — `log(assets)` is the fund-level analogue of log market value of equity (both describe the scale of capital involved), and `1/(price-to-book)` matches this project's book-to-market convention (`bm`, not the more common inverse "price-to-book" framing), reusing the exact same sign convention LLM-S's rule already reasons in.
  Date/Author: 2026-08-06, decided during planning interview.
- Decision: `compute_raw_factors_for_etf` takes `aum` and `price_to_book` as explicit caller-supplied arguments rather than fetching them automatically from `yfinance` or scraping a third-party site like Morningstar.
  Rationale: live `yfinance` coverage of these two fields for ETFs was unverified at design time (now tracked as an open Progress item to measure, not assumed); scraping Morningstar (or any site without an API contract) is both unreliable to keep working and outside this project's existing data-source conventions (`yfinance` and SEC EDGAR, both used elsewhere in this project via a stable API, not HTML scraping of arbitrary finance sites). Requiring explicit numbers keeps the function's behavior honest about what it does and doesn't know, matching this project's general null-preserving philosophy of never silently guessing a value it cannot actually observe.
  Date/Author: 2026-08-06, decided during planning interview.
- Decision: `standardize_raw_factors` omits a factor from its output dict entirely (rather than setting it to `None`, `0`, or some other placeholder) whenever the raw value is missing or the reference standard deviation is zero/missing, and `screen_external_candidate` catches the resulting `ValueError` from `apply_rule`/`evaluate_condition` and returns the literal string `"insufficient_data"`.
  Rationale: `plans/02_llm_s_agent.md`'s `condition_eval.evaluate_condition` already raises `ValueError` naming the missing factor when a condition references a name absent from the values dict passed to it — omitting the key (rather than inventing a value) means this existing, already-tested error path fires naturally with no new special-casing inside `evaluate_condition` itself. Returning `"insufficient_data"` instead of letting the exception propagate, or instead of silently treating the missing factor as failing every comparison (which would misrepresent what the rule actually says — a rule that never even mentions a missing factor should not be penalized by its absence, and one that does mention it should not be silently satisfied or denied), gives the caller an honest, explicit third outcome distinct from a real buy/sell/hold signal.
  Date/Author: 2026-08-06, decided during planning interview.
- Decision: this becomes its own new ExecPlan (`plans/07_external_candidate_screening.md`) rather than an addition to `plans/04_candidate_scanner.md`.
  Rationale: `AGENTS.md` calls for one plan file per major project step; `plans/04_candidate_scanner.md`'s own stated scope is "deterministic set arithmetic" combining two already-in-universe signal DataFrames LLM-S and LLM-F already produced — it has no dependency on `data/portfolio.duckdb` or any external API at all. This plan's actual work (fetching a candidate's own raw factors, recomputing reference statistics, standardizing, then reusing `apply_rule`) is a materially different, self-contained capability that would have diluted plan 04's narrow, already-fully-specified purpose.
  Date/Author: 2026-08-06, decided during planning interview, user's explicit choice among presented alternatives.


## Outcomes & Retrospective


This plan is complete for individual off-index stocks and for ETFs given manually-supplied AUM/price-to-book figures. The core insight — that no new persisted statistics table was needed, because the `factors` table's raw columns already contain everything required to recompute the exact same mean/std on demand — meant this plan added two small, pure functions and one small orchestration module, with zero changes to any existing table schema or any existing function's behavior (`apply_rule` itself is reused completely unmodified). The one remaining open question (whether `yfinance` can supply ETF AUM/price-to-book automatically) is deliberately left as a measurement task rather than blocking this plan's completion, since manual entry already produces correct, verified results.


## Context and Orientation


This plan adds two functions to the existing module `src/dataset/fundamentals.py` (created by `plans/01_dataset.md`, a Python 3.12, `uv`-managed repository) and a new module `src/agents/external_screen.py` (a sibling to `src/agents/llm_s.py`, `src/agents/llm_s_apply.py`, and `src/agents/llm_s_signals.py`, all created by `plans/02_llm_s_agent.md`).


"The `factors` table" refers to the DuckDB table `plans/01_dataset.md` builds at `data/portfolio.duckdb`, with columns `rebalance_date DATE, ticker VARCHAR, mve DOUBLE, bm DOUBLE, mom12m DOUBLE, mve_z DOUBLE, bm_z DOUBLE, mom12m_z DOUBLE` — one row per S&P 500 member per monthly rebalance date, holding both each factor's raw value and its cross-sectionally standardized (`_z`-suffixed) counterpart. "Cross-sectional standardization" (or "z-score") means: for one rebalance date, taking every member's raw value of one factor, computing the average and the standard deviation across all of them, then replacing each member's value with `(value - average) / standard_deviation` — the result has average 0 and standard deviation 1 across that date's members, by construction. This is what `src/dataset/fundamentals.py`'s `add_cross_sectional_z` function (lines 553-578 as of this plan's writing) computes for `mve`/`bm`, and what `src/dataset/momentum.py`'s `build_momentum_factors` reuses (via the same `add_cross_sectional_z` function) for `mom12m`.


"`ScreeningRule`" refers to the Pydantic model `plans/02_llm_s_agent.md` defines in `src/agents/llm_s_schema.py`: `year: int`, `buy_condition: str`, `sell_condition: str`, `rationale: str`, where the two condition strings are boolean expressions over the three bare names `mve`, `bm`, `mom12m` (for example `"bm > 0.4 and mom12m > -0.1"`) — always referring to the standardized z-score of that factor, never the raw value, since that is what the LLM that produced the rule was shown. "`apply_rule`" refers to the function `plans/02_llm_s_agent.md` defines in `src/agents/llm_s_apply.py`: given a `ScreeningRule` and a dict of `{"mve": float, "bm": float, "mom12m": float}` (already standardized), it returns the literal string `"buy"`, `"sell"`, or `"hold"` by evaluating the rule's two conditions via `src/agents/condition_eval.py`'s `evaluate_condition` (a restricted, `ast`-based boolean-expression evaluator that never uses Python's `eval()`, so it is safe to run against a string an LLM produced). This plan calls `apply_rule` completely unmodified — none of its code changes.


"Off-index" means a ticker that is not, and for the historical period this project studies (2020-2024) never was, a member of the S&P 500 index tracked in `plans/01_dataset.md`'s `sp500_membership` table — for example, GameStop (`GME`), used as this plan's own worked example. An ETF (exchange-traded fund) is a basket of many underlying securities traded as a single ticker; unlike an individual company, it has no single "market value of equity" or "book equity" in the sense `mve`/`bm` are defined for a company, which is why this plan defines separate proxy inputs for ETFs (see Decision Log).


## Plan of Work


Add `get_factor_reference_stats(rebalance_date, db_path=DEFAULT_DB_PATH) -> dict[str, tuple[float, float]]` to `src/dataset/fundamentals.py`, placed immediately before `write_factors_table` (which already exists in that file). It runs one query, `SELECT AVG(mve), STDDEV_SAMP(mve), AVG(bm), STDDEV_SAMP(bm), AVG(mom12m), STDDEV_SAMP(mom12m), COUNT(*) FROM factors WHERE rebalance_date = ?`, against the `factors` table, and returns `{"mve": (mean, std), "bm": (mean, std), "mom12m": (mean, std)}`. If the query's `COUNT(*)` is zero (no rows at all for that date), it raises `ValueError` naming the requested date — there is deliberately no "nearest available date" fallback here (unlike `src/agents/llm_s.py`'s `resolve_as_of_date`), because the reference statistics must match the exact date the target rule was generated against or the resulting z-scores would be standardized against the wrong population.


Create `src/agents/external_screen.py` with five functions. `_load_price_series(ticker, as_of_ts)` is a small private helper that fetches one ticker's adjusted-close price history from 13 months before `as_of_ts` (a buffer wide enough for the 12-month-momentum lookback) through 5 days after (so a price dated exactly `as_of_ts`, if one exists, is not excluded by `yfinance`'s exclusive `end` parameter), reusing `src/dataset/prices.py`'s existing `fetch_price_history` and `reshape_prices_long` functions — the same functions `plans/01_dataset.md`'s S&P-500-wide price pipeline already calls, just invoked for a list containing one ticker instead of the whole membership universe.


`compute_raw_factors_for_ticker(ticker, as_of_date) -> dict[str, float | None]` computes an off-index individual stock's raw `mve`, `bm`, `mom12m` for one date, by calling, in order: `_load_price_series` for the price history; `src/dataset/fundamentals.py`'s existing `most_recent_value_on_or_before` three times (at `as_of_date`, one month before, and twelve months before) to pick specific prices out of that history; `fetch_shares_and_splits` and `compute_mve` (both already existing in `fundamentals.py`) for `mve`; `fetch_balance_sheets`, `select_book_equity`, and `compute_bm` (all already existing in `fundamentals.py`) for `bm`; and `src/dataset/momentum.py`'s existing `compute_mom12m` for `mom12m`. Every one of these calls is a function `plans/01_dataset.md`'s `build_factors`/`build_momentum_factors` already invokes per row for the S&P 500 membership loop — this function is simply the same logic invoked for one arbitrary ticker instead. Any factor whose inputs are unavailable comes back `None`, matching the rest of this codebase's null-preserving convention rather than raising.


`compute_raw_factors_for_etf(ticker, as_of_date, aum, price_to_book) -> dict[str, float | None]` computes `mve` as `log(aum)` and `bm` as `1 / price_to_book` (both `None` if the corresponding input is `None`, non-positive, or zero), and computes `mom12m` identically to the stock path above (via `_load_price_series` and `compute_mom12m`), since price momentum is well-defined for any priced instrument, ETF or not.


`standardize_raw_factors(raw, stats) -> dict[str, float]` applies `(value - mean) / std` to each factor present and non-null in `raw`, using the corresponding `(mean, std)` pair from `stats` (the output of `get_factor_reference_stats`), and omits a factor from the returned dict entirely if its raw value is missing or its reference `std` is zero or missing — never inventing a placeholder number.


`screen_external_candidate(rule, raw_factors, rebalance_date, db_path=DEFAULT_DB_PATH) -> str` calls `get_factor_reference_stats(rebalance_date, db_path)`, then `standardize_raw_factors(raw_factors, stats)`, then `plans/02_llm_s_agent.md`'s existing `apply_rule(rule, standardized)` — catching the `ValueError` `apply_rule`/`evaluate_condition` raises when the rule's condition needs a factor missing from the standardized dict, and returning the literal string `"insufficient_data"` in that case instead of letting the exception propagate or guessing a signal.


## Concrete Steps


Run every command from the repository root, with `plans/01_dataset.md` and `plans/02_llm_s_agent.md` already implemented (so `data/portfolio.duckdb`'s `factors` table is populated).


Step 1 — run the new unit tests (no network calls):

    uv run pytest tests/test_external_screen.py -v

Expected: 7 passed, covering `get_factor_reference_stats` against a hand-built fixture, and `standardize_raw_factors`/`screen_external_candidate` against manually-computed z-score arithmetic.


Step 2 — exercise the real, network-calling paths against one off-index stock and one ETF, reusing the real 2024 rule already recorded in `plans/02_llm_s_agent.md`'s Artifacts and Notes:

    uv run python -c "
    from datetime import date
    from src.agents.llm_s_schema import ScreeningRule
    from src.agents.external_screen import (
        compute_raw_factors_for_ticker,
        compute_raw_factors_for_etf,
        screen_external_candidate,
    )

    rule = ScreeningRule(
        year=2024,
        buy_condition='(mom12m > 0.55 and mve > -0.9) or (bm > 0.2 and mom12m > -0.15)',
        sell_condition='mom12m < -0.85 or (bm > 0.4 and mom12m < -0.5)',
        rationale='real 2024 rule from plan 02',
    )
    as_of = date(2023, 12, 1)

    raw_gme = compute_raw_factors_for_ticker('GME', as_of)
    print('GME raw:', raw_gme, '-> signal:', screen_external_candidate(rule, raw_gme, as_of))

    raw_pffa = compute_raw_factors_for_etf('PFFA', as_of, aum=2.48e9, price_to_book=2.58)
    print('PFFA raw:', raw_pffa, '-> signal:', screen_external_candidate(rule, raw_pffa, as_of))
    "

Expected and actually observed (captured 2026-08-06):

    GME raw: {'mve': 22.264465001146863, 'bm': 0.283136399726766, 'mom12m': -0.5109022540486521} -> signal: sell
    PFFA raw: {'mve': 21.6315243971233, 'bm': 0.38759689922480617, 'mom12m': 0.04198710810202888} -> signal: hold


## Validation and Acceptance


Run `uv run pytest tests/test_external_screen.py` and expect all 7 tests to pass. Per `AGENTS.md`'s testing guidance to keep external API calls out of unit tests, these tests exercise only `get_factor_reference_stats` and `standardize_raw_factors`/`screen_external_candidate` — pure SQL-aggregate and dict arithmetic against small fixtures, no `yfinance` calls. `compute_raw_factors_for_ticker`/`compute_raw_factors_for_etf` (which do call `yfinance`) are validated manually instead, per Concrete Steps Step 2, matching the precedent `plans/02_llm_s_agent.md` set for `generate_rule`'s own un-unit-tested, real-LLM-calling path.


Acceptance for this plan is: `uv run pytest tests/test_external_screen.py` passes with all 7 cases; `uv run pytest tests/` (the full repository suite) still passes, confirming no regression to any existing plan's code; and the Concrete Steps Step 2 transcript (captured for real, not fabricated) shows both a real off-index stock and a real ETF producing a coherent, explainable signal.


## Idempotence and Recovery


`get_factor_reference_stats`, `standardize_raw_factors`, and `screen_external_candidate` are pure functions of their inputs (plus one read-only database query) and safe to call any number of times. `compute_raw_factors_for_ticker`/`compute_raw_factors_for_etf` perform network I/O (`yfinance`) but write nothing to disk and have no side effects beyond that read — safe to retry on a transient network failure. This plan adds no new tables and modifies no existing ones; there is nothing to recover from writing.


## Artifacts and Notes


Real output from Concrete Steps Step 2, captured 2026-08-06, against the real `data/portfolio.duckdb` and the real 2024 `ScreeningRule` recorded in `plans/02_llm_s_agent.md`:

    reference stats (S&P 500, 2023-12-01):
      mve:    (mean=24.278003486481886, std=1.1198377758624491)
      bm:     (mean=0.4661227749337229, std=1.7764539570613092)
      mom12m: (mean=-0.04525113851519343, std=0.2368947256155325)

    GME raw:            {'mve': 22.264465001146863, 'bm': 0.283136399726766, 'mom12m': -0.5109022540486521}
    GME standardized:   {'mve': -1.7980626558023414, 'bm': -0.10300653978652013, 'mom12m': -1.9656457708103878}
    GME signal:         sell   (triggered by sell_condition's 'mom12m < -0.85': -1.97 < -0.85)

    PFFA raw:            {'mve': 21.6315243971233, 'bm': 0.38759689922480617, 'mom12m': 0.04198710810202888}
    PFFA standardized:   {'mve': -2.363270061434019, 'bm': -0.044203721349928934, 'mom12m': 0.3682574459627495}
    PFFA signal:         hold   (neither buy_condition nor sell_condition's clauses are satisfied)

GameStop's real December-2023 market value (`mve=22.26`, roughly a $4.7 billion market cap) standardizes to nearly two standard deviations below the average S&P 500 company's size that month (`mve_z=-1.80`) — a sanity-checkable, real-world-consistent result, since GameStop is a mid-cap stock and was never an S&P 500 constituent.


## Interfaces and Dependencies


This plan depends on `plans/01_dataset.md`'s `factors` table (specifically its raw `mve`/`bm`/`mom12m` columns, not just the `_z` columns) and its existing per-ticker fetch/compute functions in `src/dataset/fundamentals.py` (`most_recent_value_on_or_before`, `fetch_shares_and_splits`, `compute_mve`, `fetch_balance_sheets`, `select_book_equity`, `compute_bm`) and `src/dataset/prices.py` (`fetch_price_history`, `reshape_prices_long`, `to_yfinance_symbol`), plus `src/dataset/momentum.py`'s `compute_mom12m`. This plan depends on `plans/02_llm_s_agent.md`'s `ScreeningRule` (`src/agents/llm_s_schema.py`) and `apply_rule` (`src/agents/llm_s_apply.py`), both used completely unmodified.


At the end of this plan, the following must exist and be usable by later plans exactly as described:


In `src/dataset/fundamentals.py`, `def get_factor_reference_stats(rebalance_date, db_path: str = DEFAULT_DB_PATH) -> dict[str, tuple[float, float]]`.


In `src/agents/external_screen.py`: `def compute_raw_factors_for_ticker(ticker: str, as_of_date) -> dict[str, float | None]`; `def compute_raw_factors_for_etf(ticker: str, as_of_date, aum: float | None, price_to_book: float | None) -> dict[str, float | None]`; `def standardize_raw_factors(raw: dict[str, float | None], stats: dict[str, tuple[float, float]]) -> dict[str, float]`; `def screen_external_candidate(rule: ScreeningRule, raw_factors: dict[str, float | None], rebalance_date, db_path: str = DEFAULT_DB_PATH) -> str`, returning one of `"buy"`, `"sell"`, `"hold"`, `"insufficient_data"`. Any future plan wanting to test whether an off-index candidate belongs in the portfolio's candidate set (extending `plans/04_candidate_scanner.md`'s scope, or a future interactive-flow feature in the spirit of `plans/06_interactive_flow.md`) can call `screen_external_candidate` directly, the same way `plans/04_candidate_scanner.md` calls `plans/02_llm_s_agent.md`'s in-universe `screen`.
