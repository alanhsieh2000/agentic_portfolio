# Show a per-ticker performance summary when the user builds a candidate pool by hand


This ExecPlan is a living document. The sections `Progress`, `Surprises & Discoveries`, `Decision Log`, and `Outcomes & Retrospective` must be kept up to date as work proceeds. This plan must be maintained in accordance with `PLANS.md` at the repository root. This plan builds on `plans/09_user_provided_selection.md` (for the `user_provided` selection, its candidate-pool confirm loop, and `validate_and_ingest_tickers`), `plans/05_optimizer_and_allocation.md` (for the returns matrix and the 60-month/24-month window rules), `plans/10_performance_reporting_and_target_return.md` (for the report that states the window its figures came from), `plans/12_benchmark_per_candidate_pool.md` (for `src/optimizer/benchmark.py`'s single-series return/volatility/Sharpe estimators, reused verbatim here), `plans/14_per_currency_risk_free_rate.md` (for the remembered per-currency risk-free rate and the convention of printing where a rate came from), and `plans/15_minimum_expected_dividend.md` (for the trailing dividend figures reused here) - all six checked into this repository and incorporated here by reference.


## Purpose / Big Picture


Before this change, a person building a candidate pool by hand got almost no information back about what they had typed. Running `uv run portfolio --date today --value 100000 --selection user_provided` prints a pool, asks whether to add or remove tickers, and answers a successful add with one word: `Added: AMLP.` The only thing that sentence proves is that Yahoo Finance was able to return prices for the symbol. It says nothing about what `AMLP` is, what it holds, what it costs to own, or how it has performed. The command then hands the pool to the optimizer, which prints weights, and the person is expected to have known all along that `AMLP` is an exchange-traded fund tracking energy pipeline partnerships with a 1.01% annual expense ratio. That assumption - that anybody typing a ticker has already studied it - is not a practical one.

After this change, every ticker successfully added to a `user_provided` pool prints a compact performance summary immediately, and a new `[s]ummary` choice in the same loop prints that summary for any ticker on demand, including a ticker that is not in the pool and that the person is still deciding about. The summary has two halves, and the split matters. The first half is what Yahoo Finance publishes about the security on its own performance page: the fund's name and category, its annual expense ratio beside the average for its category, its net assets, and its trailing total returns for one month through ten years printed beside the same figures for its category, plus Yahoo's own three-year alpha, beta, standard deviation and Sharpe ratio. The second half is what this project computes itself, from the very monthly returns the optimizer is about to use, over a window whose real start and end dates are printed on the same line as the figures: annual return, annual volatility, Sharpe ratio against the risk-free rate this run will actually use, and the trailing twelve-month dividend yield derived from the dividend history that adding the ticker already downloaded.

Here is the observable outcome, transcribed from a real run on 2026-09-10. The `$` line is the command; lines the person types are marked.

    $ uv run portfolio --date today --value 100000 --selection user_provided

    Current candidate pool (0): (empty)

    Edit candidate pool? [a]dd tickers / [r]emove tickers / [s]ummary / [d]one: a     <- typed
    Ticker(s) to add (space-separated): AMLP     <- typed
    Added: AMLP.

    Ticker summary: AMLP - Alerian MLP ETF  (ETF, USD)
      Category: Energy Limited Partnership   Family: ALPS   Legal type: Exchange Traded Fund
      Expense ratio: 0.0101 (category average 0.0157)   Holdings turnover: 0.1400
      Net assets: $13,442,306,048.00 USD   52-week range: 44.64 - 56.29
      Trailing P/E: 15.5220

      Yahoo trailing total returns as of 2026-09-08, fund / category:
        YTD 0.2497 / 0.2384   1M 0.0242 / 0.0278   3M 0.0968 / 0.1529   1Y 0.2355 / 0.3076
        3Y 0.1967 / 0.2578   5Y 0.2069 / 0.2279   10Y 0.0737 / 0.1023
      Yahoo risk statistics (3y): alpha=0.1044  beta=0.2700  stdDev=0.1370  Sharpe=1.0500

      This project's own figures, 2021-10-01 to 2026-09-01 (60 month(s) of monthly returns):
        Annual return: 0.1893   Annual volatility: 0.1874   Sharpe: 0.9037
        Risk-free rate used: 0.0200 (the configured default)
        Trailing 12-month dividend yield: 0.0724

    Candidate pool (1): AMLP

    Edit candidate pool? [a]dd tickers / [r]emove tickers / [s]ummary / [d]one: s     <- typed
    Ticker to summarize: T     <- typed

    Ticker summary: T - AT&T Inc.  (EQUITY, USD)
      Sector: Communication Services   Industry: Telecom Services
      Market cap: $172,337,496,064.00 USD   52-week range: 19.89 - 29.79
      Trailing P/E: 8.4396   Forward P/E: 9.8100   Beta: 0.4310
      Yahoo fund figures: n/a - T is not a fund (quoteType EQUITY), so Yahoo publishes no
        category comparison or fund risk statistics for it

      This project's own figures, 2021-10-01 to 2026-09-01 (60 month(s) of monthly returns):
        Annual return: 0.1382   Annual volatility: 0.2589   Sharpe: 0.4566
        Risk-free rate used: 0.0200 (the configured default)
        Trailing 12-month dividend yield: 0.0442

    Candidate pool (1): AMLP

Five things in that transcript are the whole point of the feature and are worth naming explicitly.

First, `AMLP`'s summary printed without being asked for, because the moment a ticker joins the pool is exactly the moment the missing information matters, and somebody who does not know to go looking will not go looking.

Second, `T` was summarized without joining the pool - the pool line still reads `(1): AMLP` afterwards - so a ticker can be studied before being committed to.

Third, `T` is an ordinary company share rather than a fund, so Yahoo publishes no category comparison for it. That absence is printed as a named `n/a` with its reason rather than as a blank space, and the second half of the summary still prints in full - the two halves come from independent sources and fail independently.

Fourth, every single ratio in that output is a plain decimal fraction printed to four places. Yahoo publishes several of these same numbers as percentages (its own copies of that expense ratio and that YTD return read `1.01` and `24.97`), and the block deliberately does not, because it sits a few lines above this project's own figures and a reader is comparing the two.

Fifth, and most usefully, the two halves disagree and say why. Yahoo reports `AMLP`'s three-year Sharpe ratio as 1.05 while this project measures 0.9037 - different windows, different risk-free rates, different volatility conventions - and each figure prints the window and the rate provenance that produced it, on the same line, so the gap is legible instead of mysterious. Neither number is wrong, and neither could substitute for the other: Yahoo's answers "how does this compare to its peers", ours answers "what will this contribute to the portfolio about to be built from it".


## Progress


- [x] (2026-09-10) Milestone 1: added `src/dataset/ticker_profile.py` with `fetch_ticker_profile` (the single network seam, at most two requests per ticker) and the pure `build_ticker_profile`, applying the unit rules recorded below. Covered by 18 tests in `tests/test_ticker_profile.py`, whose fixtures are literal excerpts of real AMLP and AVB responses and which keep every unit-trap key present in the fixture on purpose, so reading one by mistake fails a test.
- [x] (2026-09-10) Milestone 2: extracted `load_ticker_monthly_returns` out of `load_benchmark_returns` in `src/optimizer/benchmark.py` (pure extract-and-delegate; `tests/test_benchmark.py` passes untouched at 26 tests), and added `src/optimizer/ticker_stats.py` with `TICKER_MIN_MONTHS`, `DEFAULT_LOOKBACK_MONTHS`, `TickerStats` and `ticker_stats`. Covered by 13 tests in `tests/test_ticker_stats.py`, including the load-bearing one proving a ticker's figures equal the same series measured as a benchmark.
- [x] (2026-09-10) Milestone 3: added `print_ticker_summary` plus `format_ratio_pair`, `format_trailing_return_rows`, `format_risk_statistics`, `format_ticker_dividend_yield` and the private `_print_ticker_identity` to `src/flow/cli.py`. Covered by 13 tests added to `tests/test_cli.py`, one of which asserts no line of the block contains a `%`.
- [x] (2026-09-10) Milestone 4: added `TickerSummary` and `prepare_ticker_summary` to `src/flow/interactive.py`; added the `[s]ummary` choice and the automatic-on-add printing to both `_run_user_provided_confirm_loop` and `_run_edit_loop`; added `--no-ticker-summary` and the shared per-session profile cache to `main`. Covered by 14 tests added to `tests/test_cli.py` and 10 added to `tests/test_interactive_flow.py`, including the structural test that a summary-only ticker's rows never reach the session database.
- [x] (2026-09-10) Milestone 5: verified end to end with four real network runs (transcripts in `Artifacts and Notes`) - the fund path, `--no-ticker-summary`, a company share plus `[s]ummary` on a non-pool ticker, and `[s]ummary` in the post-run edit loop. Documented the feature and the flag in `README.md` and completed this plan's living sections.
- [x] (2026-09-10) Sped up `_insert_returns` in `tests/test_interactive_flow.py` and the insert loops in `tests/test_ticker_stats.py` from `executemany` to one multi-row `INSERT`, after the new tests made that file's fixture cost visible. Not part of the feature, but caused by it: `tests/test_interactive_flow.py` went from 120s for 60 tests to 140s for 70, so the ten new tests cost about 20s while the fixture change paid back roughly 50s.
- [x] (2026-09-10) Full suite verified: `uv run pytest tests/test_*.py` reports 880 passed, up from 812 before this work (68 new tests: 20 in `tests/test_ticker_profile.py`, 13 in `tests/test_ticker_stats.py`, 25 added to `tests/test_cli.py`, 10 added to `tests/test_interactive_flow.py`).


## Surprises & Discoveries


Everything in this section was measured live against `yfinance` 1.5.2 on 2026-09-10 while this plan was being written, before any code was changed. It is recorded here because each item changed a design decision, and because a future contributor who does not know these facts will write a plausible-looking bug.

- Observation: Yahoo's ETF performance data - the trailing returns beside the category's, and the fund risk statistics - is reachable, but `yfinance`'s public `Ticker` object does not expose it. `Ticker` has no `fundPerformance` accessor and, in 1.5.2, no `_fetch` method either (`hasattr(yf.Ticker('AMLP'), '_fetch')` is `False`). `Ticker.funds_data` returns a `FundsData` object whose attributes are `asset_classes`, `bond_holdings`, `bond_ratings`, `description`, `equity_holdings`, `fund_operations`, `fund_overview`, `quote_type`, `sector_weightings` and `top_holdings` - no performance anywhere. The only working route is the non-public `yfinance.data.YfData` HTTP helper against Yahoo's `quoteSummary` endpoint, and both modules this plan needs come back in a single request.
  Evidence:

        >>> from yfinance.data import YfData
        >>> r = YfData().get_raw_json(
        ...     "https://query2.finance.yahoo.com/v10/finance/quoteSummary/AMLP",
        ...     params={"modules": "fundProfile,fundPerformance",
        ...             "corsDomain": "finance.yahoo.com", "formatted": "false"})
        >>> list(r["quoteSummary"]["result"][0].keys())
        ['fundProfile', 'fundPerformance']
        >>> r["quoteSummary"]["result"][0]["fundPerformance"]["trailingReturns"]
        {'asOfDate': 1788825600, 'ytd': 0.2496513, 'oneMonth': 0.0241871,
         'threeMonth': 0.09676699, 'oneYear': 0.23550029, 'threeYear': 0.19673571,
         'fiveYear': 0.2069134, 'tenYear': 0.073727995, 'lastBullMkt': 0.0, 'lastBearMkt': 0.0}

- Observation: the same URL returns HTTP 404 for an ordinary company share, and `Ticker.funds_data` raises for one. This is the normal, expected answer for a non-fund rather than a fault, and it is why the summary has an equity branch at all.
  Evidence:

        AMLP -> ['fundProfile', 'fundPerformance']
        AVB  -> ERR HTTPError HTTP Error 404:
        >>> yf.Ticker('AVB').funds_data.fund_overview
        yfinance.exceptions.YFDataException: AVB: No Fund data found.

- Observation: `Ticker.info`'s ratio fields are in inconsistent units, sometimes within the same dictionary and sometimes differing between tickers. A percentage and a fraction sitting side by side under similar names is a 100x error waiting to be printed. This is the single most important finding in this section: it is the reason the code takes every ratio from `fundProfile`/`fundPerformance` (which are consistently fractional) and takes only names, types, dates, absolute amounts and plain multiples from `info`.
  Evidence, all from one session:

        AMLP info["netExpenseRatio"]     = 1.01          <- percent
        AMLP fundProfile ...ExpenseRatio = 0.0101        <- fraction, same number
        AMLP info["ytdReturn"]           = 24.96513      <- percent
        AMLP fundPerformance ...["ytd"]  = 0.2496513     <- fraction, same number
        AMLP info["threeYearAverageReturn"] = 0.1993137  <- fraction, in the same dict as ytdReturn

- Observation: `info["dividendYield"]` is unreliable enough that no yield should ever be read from Yahoo. It is a percentage for most tickers but a fraction for at least one, and the ticker where it breaks is one this repository already records as having broken Yahoo data. This project already computes a trailing dividend yield from dividend history it downloads itself, so the summary uses that instead and the ambiguity disappears entirely.
  Evidence:

        AMLP quoteType=ETF     yield=0.0733  dividendYield=7.33    <- percent
        SPY  quoteType=ETF     yield=0.0098  dividendYield=0.98    <- percent
        MSFT quoteType=EQUITY  yield=None    dividendYield=0.74    <- percent
        AVB  quoteType=EQUITY  yield=None    dividendYield=0.0387  <- fraction

  `AVB` is one of the four tickers (`AVB`, `EA`, `EQR`, `LEG`) already sitting in this project's `dividend_unresolved` table because Yahoo will not serve their historical dividend window - see `src/dataset/dividends.py`'s `probe_served_window`. That the same four tickers also carry a malformed `dividendYield` is consistent with them being generally unreliable on Yahoo's side, and is a good argument for never depending on that field.

- Observation: `fundProfile.feesExpensesInvestment.totalNetAssets` is not the fund's net assets. It carries the category-level figure, and the proof is that the fund's copy and the category's copy of the field are bit-identical while the fund's real net assets are a different number entirely. Net assets must therefore come from `info["totalAssets"]`, falling back to `info["netAssets"]` - the same pairing `src/agents/external_screen.py`'s `_auto_fetch_etf_aum` already documents as verified live across nine ETFs.
  Evidence:

        fundProfile["feesExpensesInvestment"]["totalNetAssets"]    = 20566.84
        fundProfile["feesExpensesInvestmentCat"]["totalNetAssets"] = 20566.84   <- identical
        info["totalAssets"]                                       = 13442306048
        info["netAssets"]                                         = 13442306000.0

- Observation: giving the automatic post-add summary a `True` default silently handed network access to every pre-existing test of the candidate-pool confirm loop. Those tests monkeypatch `validate_and_ingest_tickers` but of course knew nothing about `fetch_ticker_profile`, so each scripted add started reaching Yahoo Finance for real - a direct violation of `AGENTS.md`'s "keep external API calls out of unit tests". They still PASSED, which is what makes this worth recording: the only visible symptom was the runtime.
  Evidence: `uv run pytest tests/test_cli.py -q` went from 4.15s to 17.92s on adding the automatic summary with a `True` default, and back to 4.15s on changing that default to `False` and having `main` pass `True` explicitly. Nothing about the assertions changed.

- Observation: `src/dataset/dividends.py`'s `load_dividend_figures` does not degrade cleanly on a database that has a `returns` table but no dividend tables at all - it raises a pandas `MergeError` rather than reporting every ticker as unavailable, which its own docstring says it does for "a database with no `dividends` table". `ticker_stats` therefore wraps the call and turns any exception into a printable reason of its own. This is a fixture-shaped situation rather than a production one, since a real add always writes the dividend tables, but a summary must not be able to raise at an interactive prompt.
  Evidence: against a `tmp_path` database holding only `returns`, `ticker_stats("AAA", ...)` returns `dividend_unavailable_reason='could not read dividend history for AAA (MergeError)'` and the return/volatility figures intact. Pinned by `tests/test_ticker_stats.py::test_a_database_with_no_dividend_tables_reports_a_reason_not_a_crash`.

- Observation: `prepare_ticker_summary` itself is fast; what makes its tests slow is DuckDB fixture writes. Instrumenting every internal call showed the production path taking 0.38s end to end, essentially all of it the one unavoidable `ledoit_wolf()` call, while the surrounding test spent six to nine seconds building fixture databases. Worth knowing before anybody optimizes the wrong thing.
  Evidence: with the fixture's row insertion stubbed out, `prepare_ticker_summary("SPY", ...)` timed at `total 0.38 {'profile': 0.0, 'ingest': 0.0, 'rate': 0.0, 'ticker_stats': 0.38}`; with it restored, three consecutive calls timed 7.61s, 6.32s and 8.91s.

- Observation: Yahoo's `asOfDate` for AMLP's trailing returns, `1788825600`, is 2026-09-08 in UTC and not 2026-09-07. Trivial, but it cost a test failure on the first run, and it is the reason the conversion is done with an explicit `timezone.utc` rather than local time - a date printed in the reader's timezone would drift by a day for anybody west of Greenwich.
  Evidence: `tests/test_ticker_profile.py::test_trailing_returns_are_fractions_in_printing_order_beside_the_categorys` failed with `assert datetime.date(2026, 9, 8) == datetime.date(2026, 9, 7)` before the expectation was corrected.

- Observation: Yahoo's risk-statistics row mixes units too, and one of its fields is misnamed. `alpha`, `stdDev` and `rSquared` are percentages while `beta` and `sharpeRatio` are plain numbers, and `meanAnnualReturn` is a monthly figure despite the word "Annual" in its name. Printing `stdDev=13.7000` three lines above this project's own `Annual volatility: 0.1893` would invite exactly the misreading this project's reporting conventions exist to prevent.
  Evidence: for the `3y` row, `alpha=10.44 beta=0.27 meanAnnualReturn=1.58 rSquared=5.82 stdDev=13.7 sharpeRatio=1.05 treynorRatio=56.28`, while the same payload's `trailingReturns["threeYear"]` is `0.19673571`. A 1.58% monthly mean compounds to roughly 20.7% a year, which matches that three-year figure; a 1.58% *annual* mean does not match anything. So `meanAnnualReturn` is monthly.


## Decision Log


- Decision: the summary combines Yahoo's published facts with figures this project computes itself, rather than showing only one or the other.
  Rationale: the two halves answer different questions and neither is sufficient. Yahoo's half answers "what is this thing and how does it compare to its peers", which this project cannot compute at all because it has no notion of a fund category. This project's half answers "what will this ticker contribute to the portfolio I am about to build", which Yahoo cannot answer because its figures are computed over Yahoo's own windows with Yahoo's own conventions, not over the 60-month window and the Ledoit-Wolf volatility convention that `src/optimizer/portfolio.py` is about to apply. Printing only Yahoo's numbers would put figures beside the weights table that were derived differently from the weights, which `src/flow/cli.py`'s `print_user_portfolio` docstring already identifies as the thing to avoid: "a figure whose derivation is not stated beside it invites being compared with one derived differently".
  Date/Author: 2026-09-10, agreed with the user before implementation.

- Decision: the summary prints automatically for each ticker successfully added, AND is separately available through a new `[s]ummary` choice.
  Rationale: the automatic half addresses the actual gap, which is that a person who does not know they should look something up will not go looking. The on-demand half addresses the case the automatic half cannot reach - studying a ticker before deciding to add it, and re-reading one already in the pool - and it is also what makes the feature usable at all when `--no-ticker-summary` has switched the automatic half off.
  Date/Author: 2026-09-10, agreed with the user before implementation.

- Decision: the block is compact, around twelve to eighteen lines, and deliberately omits the fund's top-ten holdings, its sector weightings, its asset-class breakdown and its year-by-year return history, all of which are available.
  Rationale: this block prints inside an interactive loop, potentially several times in a row when several tickers are added on one line. At forty-plus lines each it would push the loop's own prompt off the top of the screen, which makes the loop harder to use and so makes the feature counterproductive. The omitted data is genuinely interesting and is a reasonable subject for a later plan that renders it somewhere it can be read at leisure.
  Date/Author: 2026-09-10, agreed with the user before implementation.

- Decision: every ratio comes from `fundProfile`/`fundPerformance`; from `info` the code reads only names, quote type, dates, absolute currency amounts and plain multiples (`beta`, `trailingPE`, `forwardPE`); and no yield is ever read from Yahoo.
  Rationale: see the `Surprises & Discoveries` entries on `info`'s inconsistent units and on `dividendYield`. A rule stated as "check the unit of each field" would not survive contact with a future contributor; a rule stated as "never take a ratio from `info`" is checkable by reading the code. The dividend yield this project computes from its own ingested dividend history is additionally the yield the optimizer's dividend floor uses (`plans/15_minimum_expected_dividend.md`), so using it here makes the summary and the constraint agree.
  Date/Author: 2026-09-10.

- Decision: Yahoo's `alpha` and `stdDev` are divided by 100 before printing, `meanAnnualReturn`, `rSquared` and `treynorRatio` are not printed at all.
  Rationale: consistency of units across one screen of output matters more than fidelity to Yahoo's own presentation, because the reader is comparing the two halves of the block against each other. `meanAnnualReturn` is excluded because it is monthly despite its name and this plan will not print a misnamed figure; `rSquared` and `treynorRatio` are excluded to keep the block compact, and neither is load-bearing for the decision the reader is making.
  Date/Author: 2026-09-10.

- Decision: reaching Yahoo's fund data through the non-public `yfinance.data.YfData` is accepted, but only as strictly optional enrichment - any failure, including `ImportError` if a future `yfinance` removes the module, degrades to a named `n/a` line while the rest of the summary still prints.
  Rationale: there is no public route (see `Surprises & Discoveries`), and the data is the most useful part of the Yahoo half because the category comparison is what makes an expense ratio or a trailing return interpretable. `AGENTS.md` already instructs treating Yahoo responses as unstable input and handling missing data defensively, and every other Yahoo-derived figure in this project already degrades to a reason rather than an exception. A test pins the degradation so the fallback cannot rot unnoticed.
  Date/Author: 2026-09-10.

- Decision: a ticker summarized through `[s]ummary` that is not in the pool is ingested into a throwaway scratch database, never into the open session database.
  Rationale: `src/optimizer/portfolio.py`'s `_load_window_dates` derives the returns window from `SELECT DISTINCT rebalance_date FROM returns` across the whole table. A ticker with longer history than the pool's own tickers would therefore add earlier month-ends to that set and could widen the window the pool's report is measured over - so merely looking at a ticker would silently change the numbers printed for the portfolio. This is the identical hazard `src/flow/interactive.py`'s `prepare_benchmark` already documents and solves the same way, with `build_scratch_snapshot` from `src/flow/live.py`.
  Date/Author: 2026-09-10.

- Decision: the confirm loop resolves the risk-free rate for the summary itself rather than receiving a settled one.
  Rationale: it has to. `main` calls `_run_user_provided_confirm_loop` before `_settle_risk_free_rate`, because the rate is per-currency and the currency is not known until the pool is confirmed. Printing a Sharpe ratio without saying which rate produced it is precisely the failure `plans/14_per_currency_risk_free_rate.md` was written to fix, and omitting the Sharpe ratio entirely would make the summary's second half less useful than the report it precedes. So the loop reads the remembered rate itself through `src/flow/rate_memory.py`'s existing `load_risk_free_rate` and `resolve_risk_free_rate`, resolving against the summarized ticker's own currency - which ingestion has established even when the pool's has not been - and prints the `origin` string that comes back. In `_run_edit_loop` the rate and origin are already settled and are passed straight through.
  Date/Author: 2026-09-10.

- Decision: no new database table; Yahoo's published facts are cached only for the lifetime of the session, in a plain dictionary.
  Rationale: these are point-in-time descriptions of a security - its current category, its current expense ratio - not a history to accumulate, so there is nothing to be gained from making them durable and there would be a staleness question to answer if they were. A per-session dictionary is enough to make re-summarizing the same ticker free, which mirrors how `BenchmarkSource` resolves a benchmark's whole history once per session.
  Date/Author: 2026-09-10.

- Decision: `show_summaries` defaults to `False` on both loop functions even though the behavior is on for every real run, which `main` expresses by passing `True`.
  Rationale: the automatic summary reaches Yahoo Finance, and a `True` default gave network access to every pre-existing test of those loops (see `Surprises & Discoveries`) - tests that only ever meant to exercise add/remove sequencing and that passed regardless, so the mistake was invisible except in the runtime. Inverting the default makes "this call site wants network access" something a reader can see at the call site. `format_risk_free_rate`'s optional `origin` already follows this reasoning: a defaulted parameter whose default exists for the benefit of callers that predate it.
  Date/Author: 2026-09-10.

- Decision: the field is `dividend_lookback_months`, not the `dividend_coverage_months` this plan originally specified.
  Rationale: `load_dividend_figures` reports which tickers have reliable dividend data but not how many months of it each one has, so a field named for coverage would have been filled with the window that was summed over - a different thing wearing a more reassuring name. The window is what there is, so the field says so, and the reason a yield is missing comes through separately in `dividend_unavailable_reason`.
  Date/Author: 2026-09-10.

- Decision: in the post-run `_run_edit_loop`, an added ticker's automatic summary prints BEFORE the recompute that may reject the edit.
  Rationale: the summary is what justifies keeping a ticker, so it belongs beside the add that admitted it rather than buried under a whole reprinted report. The cost is that an edit the optimizer then rejects has already had its summary printed - but the revert message follows immediately and names what was undone, and seeing what you tried to add is not misleading. The alternative, printing after a successful recompute, would put fifteen lines of summary below thirty lines of report and lose the connection to the add entirely.
  Date/Author: 2026-09-10.

- Decision: this is plain deterministic Python, not a CrewAI agent or tool.
  Rationale: CrewAI is used in exactly two places in this repository, `src/agents/llm_s_crew/` and `src/agents/llm_f_crew/`, and in both an LLM is asked to produce a judgment (a screening rule, a sentiment label). Every statistic and every report in this project - `PortfolioStats`, `BenchmarkStats`, `HoldingsStats`, and all of plans 10, 12, 13 and 14 - is ordinary code in `src/optimizer/` paired with a `format_*`/`print_*` function in `src/flow/cli.py`. Numbers derived from prices are never routed through a language model here, and a summary of published facts and arithmetic gives one nothing to decide.
  Date/Author: 2026-09-10.

- Decision: max drawdown, beta against the pool's benchmark, Sortino ratio and tracking error are out of scope.
  Rationale: none of them exists anywhere in this repository today, so each is new arithmetic that needs its own justification and its own tests rather than arriving as a side effect of a reporting change. Beta against the benchmark is additionally impossible at the point the confirm loop runs, because `_settle_benchmark` has not been called yet and the pool may not even have a currency.
  Date/Author: 2026-09-10.


## Outcomes & Retrospective


Delivered as specified. `--selection user_provided` now summarizes every ticker it admits and offers `[s]ummary` on demand in both interactive loops, the block matches the transcript in `Purpose / Big Picture` in shape and in substance, and 60 new tests cover the four layers separately. Nothing about what the optimizer computes changed: the same pool produces the same weights and the same share allocation as before, which the live runs confirm.

The feature justified itself on its first real run, in a way this plan did not predict. Adding `AVB` printed `This project's own figures: n/a - 1 month(s) of history for AVB in this window, below the 24 required` - and the report that followed gave `AMLP: 1.0000` with no line for `AVB` at all. `AVB` had been silently dropped by `apply_min_history_rule`, exactly as it always would have been, and before this change the only clue was `Added: AVB.` followed by a weights table that quietly did not mention it. That is precisely the "the user has studied this ticker" assumption the feature was built to stop relying on, and it turned out to bite on a ticker already in the shipped candidate pool.

The second thing worth recording is how far apart the two halves of the block can legitimately be. For `SPY`, Yahoo publishes a three-year Sharpe ratio of 1.19 while this project measures 0.6919 over its own 60 months; for `AMLP`, 1.05 against 0.9037. Neither is wrong. They are different windows, different risk-free rates and different volatility conventions, and a reader who did not have both windows printed beside both figures would have no way to know that. Every figure in the block carries the window and the provenance that produced it, and this is the case that shows why the house rule exists.

What remains, deliberately: the top-ten holdings, sector weightings and year-by-year returns Yahoo also serves, all of which are reachable and none of which fit a block that prints inside an interactive loop; and max drawdown and beta against the pool's benchmark, which are new arithmetic rather than new reporting. A later plan that renders the fuller picture somewhere it can be read at leisure would be a reasonable successor.

The lesson to carry forward is the one in `Surprises & Discoveries` about default parameter values. Giving the automatic summary a `True` default made every pre-existing test of the two interactive loops start calling Yahoo Finance for real, and every one of them still passed - the only symptom was `tests/test_cli.py` going from 4.15s to 17.92s. A test that reaches the network and passes is worse than one that fails, because nothing draws attention to it. When adding a network-touching side effect to a function that already has tests, the safe default is off, and the call site that wants it says so.


## Context and Orientation


This section assumes no knowledge of this repository.

This project builds stock and fund portfolios. Its main command is `uv run portfolio`, which is the console script `portfolio` declared in `pyproject.toml`'s `[project.scripts]` and pointing at `main` in `src/flow/cli.py`. The command needs a *candidate pool* - the list of tickers it is allowed to hold - and the `--selection` flag decides where that list comes from. Three of the four possible values ask a language model to pick the pool. The fourth, `--selection user_provided`, asks the person running the command, and that is the only one this plan touches.

The `user_provided` path works like this, and all of it is in `src/flow/cli.py`. `main` opens a *session snapshot* - a temporary DuckDB database file, created by `build_live_snapshot` in `src/flow/live.py`, that this run may write to freely and that is deleted when the run ends. It then calls `_run_user_provided_confirm_loop`, which loads whatever pools were saved from previous runs out of `memory/candidates.json` (one pool per currency; the file's reader and writer are in `src/flow/candidate_memory.py`), asks which to resume, and then loops on a prompt offering to add tickers, remove tickers, or finish. An add goes through `validate_and_edit_candidates` in `src/flow/interactive.py`, which calls `validate_and_ingest_tickers` in `src/dataset/ticker_ingestion.py`. That function is the one that does the real work of an add: it downloads 65 months of prices for the typed symbols, looks up each one's trading currency, writes prices, currencies, monthly returns and dividend history into the session database using merge-writes that disturb no other ticker's rows, and reports back which symbols resolved and which did not. When the person finishes, the pool is saved and `main` settles the remaining questions in a fixed order - `_settle_risk_free_rate` first, then `_settle_benchmark` - runs the optimizer, prints the report, and finally enters a second interactive loop, `_run_edit_loop`, in which the pool can be edited again with the report reprinted after each change.

Two things in that description are the reason this plan's design looks the way it does. First, by the time a ticker has been added, its monthly returns and its dividend history are already sitting in the session database - so the second half of the summary needs no network access at all and can be computed from data the add already paid for. Second, the risk-free rate is settled *after* the confirm loop, not before, so the confirm loop has to resolve one for itself.

A few terms used below. A *monthly return* is one month's proportional price change for one ticker; they are stored one row per ticker per month in a table called `returns`, keyed by a `rebalance_date` that is always a month end. The *returns window* is the trailing run of months a figure was computed over; this project's default is the most recent 60 months, and it will decline to report figures for a ticker with fewer than 24 months of them, a rule implemented by `apply_min_history_rule` in `src/optimizer/portfolio.py`. *Annualized* means converted from the monthly cadence to a yearly one. The *Sharpe ratio* is `(annual return - risk-free rate) / annual volatility`, a return-per-unit-of-risk figure. The *risk-free rate* is what a supposedly riskless investment returns; this project remembers one per currency in `memory/rates.json` and always prints where the one it used came from. A *benchmark* is a single ticker standing in for "the market", used for comparison. A *quote type* is Yahoo Finance's own classification of a symbol, the string `ETF`, `MUTUALFUND`, `EQUITY` and so on; this plan branches on it because Yahoo publishes fund data only for funds.

The files this plan adds or changes:

- `src/dataset/ticker_profile.py` - new. Fetches what Yahoo publishes about one ticker and normalizes it. The only module in this plan that touches the network.
- `src/optimizer/ticker_stats.py` - new. Computes this project's own figures for one ticker from a database. Touches no network and imports nothing from `src/flow`.
- `src/optimizer/benchmark.py` - changed, by one pure refactor: the body of `load_benchmark_returns` moves into a neutrally named `load_ticker_monthly_returns` and `load_benchmark_returns` calls it. No behavior change.
- `src/flow/interactive.py` - changed, gaining `prepare_ticker_summary`, which decides where a summarized ticker's data should come from and does any fetching required.
- `src/flow/cli.py` - changed, gaining `print_ticker_summary` and its helpers, the `[s]ummary` choice in both loops, the automatic printing after an add, and the `--no-ticker-summary` flag.
- `tests/test_ticker_profile.py`, `tests/test_ticker_stats.py` - new. `tests/test_cli.py`, `tests/test_interactive_flow.py` - added to.
- `README.md` - changed, documenting the new flag and choice.

House conventions this plan must obey, all of them already visible in `src/flow/cli.py`. Output is plain `print()` with f-strings; there is no `rich` and no `tabulate` in this project and none may be added. Every ratio, return, volatility, yield and rate prints as a decimal fraction to exactly four places with `:.4f`, never as a percentage. Money never prints raw: it goes through `format_money(amount, currency)`, which produces `$12.34 USD`. A section opens with `print("\nLabel:")` and its items are indented two spaces. The dash inside a printed string is the ASCII hyphen `-`, not an em dash. A figure that cannot be produced prints as one line reading `n/a - <reason>`; it is never omitted, and it never raises. And a figure always prints the window and the provenance that produced it, because a number whose derivation is not stated beside it invites being compared against one derived differently.


## Plan of Work


### Milestone 1 - what Yahoo publishes about one ticker


At the end of this milestone a new module can be handed a ticker string and return a fully normalized description of it, with every unit correct, without the rest of the project knowing anything about Yahoo's payload shapes. Nothing user-visible changes yet; the proof is a new test file that passes.

Create `src/dataset/ticker_profile.py`. Shape it exactly like the existing `src/dataset/ticker_currency.py`: one function performs network I/O and is the single seam tests replace, everything else beside it is pure. Give the module a docstring that states that rule, states the unit rules from this plan's `Decision Log`, and explains that the fund data comes from a non-public `yfinance` module and is therefore optional.

Define two `NamedTuple`s. `RawTickerProfile` carries `info: dict`, `fund_profile: dict | None`, `fund_performance: dict | None`, `info_reason: str | None` and `fund_data_reason: str | None` - the two reason fields hold a printable sentence when the corresponding fetch failed. `TickerProfile` carries the normalized result: `ticker`, `long_name`, `quote_type`, `currency`, `fifty_two_week_low`, `fifty_two_week_high`; the fund fields `category_name`, `family`, `legal_type`, `expense_ratio`, `expense_ratio_category`, `holdings_turnover`, `net_assets`, `trailing_returns`, `trailing_returns_category`, `risk_statistics`; the equity fields `sector`, `industry`, `market_cap`, `trailing_pe`, `forward_pe`, `beta`; and `fund_data_reason`, `unavailable_reason`. Follow the contract every comparable type in this project follows - `BenchmarkStats`, `HoldingsStats` - that a group of fields is either wholly populated or wholly `None` with a reason set, so the printing code never has to guard a half-filled state.

Write `fetch_ticker_profile(ticker, pause_seconds=settings.yfinance_fundamentals_pause_seconds) -> RawTickerProfile`, the only network function. Translate the ticker to a Yahoo symbol with `to_yfinance_symbol` from `src/dataset/prices.py` first - every fetcher in this project does, and it is what makes `BRK.B` fetch as `BRK-B` while leaving `7203.T` alone. Then make at most two requests: `yf.Ticker(symbol).info`, and the combined `quoteSummary` request for `fundProfile,fundPerformance` shown in `Surprises & Discoveries`. Wrap each in its own `try/except Exception` that logs a warning and records a reason string; neither may raise, and a failure of one must not prevent the other. Import `YfData` inside the function rather than at module scope so that a future `yfinance` without it degrades to a reason rather than breaking the import of this whole module. Sleep `pause_seconds` between the two requests, matching `fetch_ticker_currencies`, the only other per-ticker fetcher in this project.

Write `build_ticker_profile(ticker, raw) -> TickerProfile`, pure. If `raw.info` is empty and `raw.info_reason` is set, return a `TickerProfile` carrying only `ticker` and `unavailable_reason`. Otherwise read `long_name` from `longName` then `shortName`, `quote_type` from `quoteType`, `currency` from `currency`, and the 52-week bounds from `fiftyTwoWeekLow`/`fiftyTwoWeekHigh`. If `raw.fund_profile` and `raw.fund_performance` are present, populate the fund fields: category, family and legal type from `fundProfile`; expense ratio and holdings turnover from `fundProfile.feesExpensesInvestment` with the category averages from `feesExpensesInvestmentCat`; net assets from `info["totalAssets"]` falling back to `info["netAssets"]` and never from `feesExpensesInvestment.totalNetAssets`; trailing returns and their category counterparts from `fundPerformance.trailingReturns`/`trailingReturnsCat` as an ordered mapping of period label to value, carrying `asOfDate` converted from its Unix timestamp to a `date`; and the risk statistics from the `3y` entry of `fundPerformance.riskOverviewStatistics.riskStatistics`, dividing `alpha` and `stdDev` by 100 and dropping `meanAnnualReturn`, `rSquared` and `treynorRatio`. If either module is missing, leave every fund field `None` and set `fund_data_reason` - to the reason the fetch recorded when there was one, or to a sentence naming the quote type and explaining that Yahoo publishes no fund figures for a non-fund when the quote type is not `ETF` or `MUTUALFUND`. Populate the equity fields from `info` whenever they are present, regardless of quote type, since they are the plain multiples the unit rule permits. Treat every field as absent-by-default: a Yahoo payload missing any key must yield `None` for that field, not a `KeyError`.

Write `tests/test_ticker_profile.py`, with a docstring stating that per `AGENTS.md` no test here calls Yahoo Finance. Build the fixtures as literal dictionaries copied from the payloads captured live while this plan was written. Cover: the ETF path end to end against the AMLP payload, asserting each normalized value including the fractional expense ratio and the divided-by-100 alpha and standard deviation; the equity path against the AVB payload, asserting the fund fields are all `None` and `fund_data_reason` names the quote type; the fund-fetch-failed path, asserting the fund fields are `None` with the fetch's own reason and the rest still populated; the info-failed path, asserting a profile carrying only a reason; net assets coming from `totalAssets` and specifically not from `feesExpensesInvestment.totalNetAssets`, using a fixture where the two differ; that no field is ever populated from `info["dividendYield"]`, `info["yield"]`, `info["netExpenseRatio"]` or `info["ytdReturn"]`; and a payload stripped down to almost nothing yielding `None`s rather than raising.


### Milestone 2 - this project's own figures for one ticker


At the end of this milestone a second new module can compute, for one ticker, the same annual return, annual volatility and Sharpe ratio this project computes for a benchmark and for a portfolio, over the same window rules, plus the trailing twelve-month dividend yield - all from a database, with no network access. Again nothing user-visible changes; the proof is a second new test file passing while every existing test still passes.

First do the refactor, on its own, so it can be verified in isolation. In `src/optimizer/benchmark.py`, `load_benchmark_returns(ticker, as_of, db_path)` already loads any single ticker's whole monthly-return history read-only and returns an empty series rather than raising when the file, the table or the ticker is missing. Nothing about it is benchmark-specific except its name. Move its body into a new `load_ticker_monthly_returns(ticker, as_of, db_path)` in the same module and make `load_benchmark_returns` a one-line call to it, keeping its docstring. This is a pure extract-and-delegate refactor; `tests/test_benchmark.py` must pass untouched afterwards, and that is the acceptance for this step.

Then create `src/optimizer/ticker_stats.py`. It must read a database and nothing else - no `src/flow` import, no `yfinance` - which is the contract `src/optimizer/holdings.py` follows and what makes it testable against a fixture database with no mocking at all. Define `TickerStats` as a `NamedTuple` with `ticker`, `currency`, `annual_return`, `annual_volatility`, `sharpe`, `risk_free_rate`, `window_start`, `window_end`, `window_months`, `dividend_yield`, `dividend_lookback_months`, `dividend_unavailable_reason` and `unavailable_reason`, following the same all-or-nothing contract as `BenchmarkStats`. (This field was called `dividend_coverage_months` when the plan was first written; see the `Decision Log` for why it is named for the window instead.)

Write `ticker_stats(ticker, as_of, db_path, currency, risk_free_rate, lookback_months=60, min_months=TICKER_MIN_MONTHS)`. Load the whole history with `load_ticker_monthly_returns`, slice the trailing `lookback_months` months ending at or before `as_of`, and reuse - do not reimplement - `annualized_return_and_volatility` from `src/optimizer/benchmark.py` for the two figures. That function's own docstring explains at length why the volatility must come from `CovarianceShrinkage(...).ledoit_wolf()` and never from `series.std() * sqrt(12)`; the two differ by about 0.8% on real data because of a `ddof` convention, which is enough to make a ticker look better or worse than the portfolio it is about to join for no reason but a mismatch. Compute the Sharpe ratio as `(annual_return - risk_free_rate) / annual_volatility`, the identical definition `benchmark_stats_for_window` uses. Set `TICKER_MIN_MONTHS = 24` with a docstring explaining that it is deliberately the same bar `apply_min_history_rule` sets for a pool ticker and that `BENCHMARK_MIN_MONTHS` sets for a benchmark, so that all three sides of any comparison are held to one standard, and reuse `MIN_USABLE_ANNUAL_VOLATILITY` from `src/optimizer/benchmark.py` to reject a flat series - `ledoit_wolf()` returns about `6e-18` rather than zero for a genuinely constant series, and dividing by that yields a Sharpe ratio around `1.8e16`, which would print as the best investment ever measured.

Report `window_start`, `window_end` and `window_months` read off the sliced data's own index, never off the requested `lookback_months`. Real history is frequently shorter than the target, and this project's rule is to print what actually happened.

Fold in the dividend yield by reusing `load_dividend_figures(tickers, as_of, db_path, lookback_months)` from `src/optimizer/dividends.py`, which reads the very dividend rows `validate_and_ingest_tickers` ingested when the ticker was added, and record both the yield and the coverage behind it. A ticker with no coverage gets `dividend_unavailable_reason` set and `dividend_yield` left `None`; it must never be silently counted as a zero yield, which is the distinction the `dividend_coverage` and `dividend_unresolved` tables exist to preserve.

Every unusable case returns a `TickerStats` carrying only `unavailable_reason`, phrased as a single sentence fit to print: no rows for this ticker at all, fewer than `min_months` in the window, or a volatility below the usable threshold.

Write `tests/test_ticker_stats.py` against real DuckDB files under `tmp_path`, built with `CREATE TABLE` and `INSERT` - the pattern `tests/test_holdings.py` and `tests/test_dataset_upsert.py` already use. Cover: a ticker with full history producing all three figures and the real window dates; a ticker with 12 months of history declining with a reason naming both counts; a ticker absent from the table declining with a reason; a flat return series declining rather than reporting an enormous Sharpe ratio; a window shorter than `lookback_months` reporting its real start and end; a ticker with dividend coverage reporting a yield; and a ticker without coverage reporting a reason rather than zero. Add one test asserting that `ticker_stats` and `benchmark_stats_for_window` given the identical series and rate produce the identical return, volatility and Sharpe ratio, which is what pins the "held to one standard" claim.


### Milestone 3 - rendering the block


At the end of this milestone the block in this plan's `Purpose / Big Picture` can be printed from a `TickerProfile` and a `TickerStats`, and its exact text is pinned by tests. Still nothing is wired into the loops.

In `src/flow/cli.py`, add `print_ticker_summary(profile, stats, risk_free_rate_origin=None)` and the small `format_*` helpers it composes, placed near `_print_add_outcome` and `print_weights_and_allocation` and following their style exactly: `format_*` functions are pure, return one line, and return `None` to mean "print nothing" so the caller writes `if line is not None: print(line)`. Reuse `format_money` for the net-assets and market-capitalization lines and reuse `format_risk_free_rate(rate, origin)` verbatim for the provenance line rather than writing a second one. Give the header line the ticker, the long name, the quote type and the currency; give the fund branch its category, fees and net-assets lines and the two Yahoo blocks; give the equity branch its sector, market-capitalization and multiples lines; and in both branches print the `Yahoo fund figures: n/a - <reason>` line whenever `fund_data_reason` is set. Print this project's own block last, headed with the real window dates and month count, and print the dividend line either as a yield with its coverage or as a named `n/a`. A `TickerProfile` carrying only `unavailable_reason`, or a `TickerStats` carrying only `unavailable_reason`, collapses its own half of the block to a single `n/a` line while the other half prints normally - the two halves fail independently, because they come from independent sources.

Add tests to `tests/test_cli.py` asserting on captured standard output, which is how every rendering test in that file already works: the ETF block containing its expense-ratio-versus-category line and its trailing-returns line; the equity block containing the `n/a` fund line and still containing this project's figures; a profile-unavailable case; a stats-unavailable case; the risk-free-rate line carrying its origin; and one test asserting no line of the output contains a `%` character, which is what pins the never-print-percentages convention for this block.


### Milestone 4 - wiring it into the loops


At the end of this milestone the transcript in `Purpose / Big Picture` is reproducible. This is the milestone that changes user-visible behavior.

In `src/flow/interactive.py`, add `prepare_ticker_summary(ticker, as_of, pool, session_db_path, risk_free_rate, rates_path=None, risk_free_rate_override=None, profile_cache=None, allow_fetch=True)`, returning the `TickerProfile`, the `TickerStats` and the risk-free-rate origin string. This is the fetching counterpart to the existing `prepare_benchmark` and `prepare_holdings` and belongs here for the same reason they do: it keeps network decisions out of both the computation layer and the rendering layer.

It must decide where the ticker's returns come from. A ticker already in `pool` is read straight out of the open session database, because the add already ingested it there. A ticker not in `pool` must be ingested into a fresh throwaway database obtained from `build_scratch_snapshot(prefix="summary_snapshot_")` in `src/flow/live.py` and read from there, and the session database must not be written to at all. The reason is the hazard `prepare_benchmark` already documents in its own docstring: `_load_window_dates` derives the returns window from `SELECT DISTINCT rebalance_date FROM returns` across the whole table, so a summarized ticker with longer history than the pool's tickers would add earlier month-ends and could widen the window the portfolio's own report is measured over. Merely looking at a ticker must not change the numbers printed for the portfolio. Ingestion reuses `validate_and_ingest_tickers` unchanged, so a symbol that does not resolve is reported by name exactly as it is at the add prompt, and the ticker's own currency comes back from the same call.

Resolve the risk-free rate against the summarized ticker's own currency using `load_risk_free_rate` and `resolve_risk_free_rate` from `src/flow/rate_memory.py`, returning the `origin` string for printing, unless the caller passed a settled rate - which `_run_edit_loop` does and the confirm loop cannot. Cache profiles in the caller-supplied `profile_cache` dictionary so re-summarizing a ticker costs nothing.

In `src/flow/cli.py`, change `_run_user_provided_confirm_loop`. Its prompt becomes `"\nEdit candidate pool? [a]dd tickers / [r]emove tickers / [s]ummary / [d]one: "`. Accept `s` or `summary`, read a symbol from a follow-up `"Ticker to summarize: "` prompt, and print the block; this choice must never change the pool, its currency, or anything on disk, which also means a ticker in a different currency can be inspected freely rather than being refused as an add would be. After the existing `_print_add_outcome` call, print one block per ticker in `edit.added`, in the order they were typed. Thread two new defaulted parameters, `rates_path` and `risk_free_rate` (the `--risk-free-rate` override), plus a `show_summaries` flag, from `main`; defaults must keep every existing test passing unchanged.

Change `_run_edit_loop` the same way but only for `selection == "user_provided"`, which is the same narrow gate that already makes adds validate and edits persist in that loop. Here the settled rate and its origin are already carried as parameters and are passed straight through.

Add `--no-ticker-summary` to `main`'s argument parser, following the existing `--no-dividend-fetch`, `--no-benchmark-fetch` and `--no-holdings-fetch` family. It suppresses only the automatic-on-add printing, so a scripted run pays no extra requests; the explicit `[s]ummary` choice still works, because asking for something is not the same as having it volunteered. Create one profile cache in `main` and pass it to both loops so a ticker summarized in the confirm loop is not re-fetched in the edit loop.

Add tests to `tests/test_cli.py` and `tests/test_interactive_flow.py`, monkeypatching by string path on the *importing* module - the convention every test in this project follows, and the one `tests/test_ticker_ingestion.py`'s `_patch_chain` helper establishes for the ingestion chain. Cover: `[s]` on a ticker already in the pool printing a block; `[s]` on a ticker not in the pool printing a block, leaving the pool unchanged, and leaving the session database's `returns` table untouched; `[s]` on an unresolvable symbol reporting it by name without a traceback; `[s]` on a ticker in another currency printing a block rather than refusing it; a two-ticker add printing two blocks in typed order; `--no-ticker-summary` suppressing the add-time blocks while `[s]` still prints one; a Yahoo failure still printing this project's own figures; and the profile cache preventing a second fetch of the same ticker.


### Milestone 5 - verification, documentation, and closing the plan


Run the whole suite. Then run the real command three times against the live network: once adding `AMLP` to see the full fund block, once adding `AVB` to see the equity block with its named `n/a` fund line - a good stress case, since `AVB` is one of the four tickers this project already records as having broken Yahoo dividend data - and once with `--no-ticker-summary` to confirm the automatic half is silent while `[s]` still works. Paste the transcripts into `Artifacts and Notes`. Confirm that a run finishing with `[d]one` produces exactly the weights and allocation it produced before this feature existed; nothing here may change what the optimizer does.

Document the `[s]ummary` choice and the `--no-ticker-summary` flag in `README.md`'s Live Mode section. Complete this plan's `Progress`, `Surprises & Discoveries`, `Decision Log` and `Outcomes & Retrospective`.


## Concrete Steps


All commands run from the repository root, `/app/agentic_portfolio`.

Establish the baseline before changing anything:

    $ uv run pytest tests/test_*.py --collect-only -q | tail -1
    812 tests collected in 9.57s

    $ uv run pytest tests/test_*.py
    ... 812 passed ...

After each milestone, run that milestone's own tests, then the whole suite:

    $ uv run pytest tests/test_ticker_profile.py -q                          # milestone 1
    ... 20 passed ...
    $ uv run pytest tests/test_ticker_stats.py tests/test_benchmark.py -q    # milestone 2
    ... 39 passed ...
    $ uv run pytest tests/test_cli.py -q                                     # milestones 3 and 4
    ... 190 passed ...
    $ uv run pytest tests/test_interactive_flow.py -q                        # milestone 4
    ... 70 passed ...

The whole suite after the work, which takes about thirteen minutes on the
container - long enough that it is worth running in the background rather
than waiting on it:

    $ uv run pytest tests/test_*.py --collect-only -q | tail -1
    880 tests collected in 3.35s

    $ uv run pytest tests/test_*.py
    ... 880 passed ...

The end-to-end check needs network access. Piping the answers in makes it
repeatable, and pointing `--memory-path` and `--rates-path` at throwaway
files keeps the checked-out `memory/` untouched:

    $ printf 'a\nAMLP\ns\nT\ns\nZZZZQQQ\nd\nf\n' | uv run portfolio --date today \
        --value 100000 --selection user_provided --memory-path /tmp/cand.json \
        --rates-path /tmp/rates.json --no-holdings

Compare what prints against the transcript in `Purpose / Big Picture`. `AMLP`
must summarize automatically under `Added: AMLP.`; `T` must summarize on
demand with the equity block and leave the pool line reading `(1): AMLP`;
`ZZZZQQQ` must print a summary block whose BOTH halves are named `n/a` lines
(not `Ignored (not found)`, which is the add prompt's message - `[s]ummary`
reports through the summary block instead) and must return to the prompt with
no traceback. Expect some raw yfinance noise around the bad symbol (`HTTP
Error 404: {...}`, `possibly delisted`); that is yfinance printing for itself
and an ordinary add produces the same lines.

To confirm the automatic half can be switched off, add `--no-ticker-summary`
to the same command with `printf 'a\nAMLP\ns\nAMLP\nd\nf\n'`. The add must
print only `Added: AMLP.` and the pool line, while `s` must still print a
full block.


## Validation and Acceptance


Acceptance is behavior a person can see, not code that exists.

Run `uv run pytest tests/test_*.py`. Before any of this work it reports 812 passed; after it, 880 passed, all of them. Every new test must fail before its own milestone's change and pass after it. The refactor in Milestone 2 is accepted specifically by `tests/test_benchmark.py` continuing to pass with no edits at all.

Run `uv run portfolio --date today --value 100000 --selection user_provided`, start a new pool, and add `AMLP`. Accepted when the block from `Purpose / Big Picture` prints under `Added: AMLP.`: a header naming the fund and its quote type, an expense ratio of about `0.0101` printed beside a category average of about `0.0157`, trailing returns printed beside the category's for every period Yahoo supplies, a three-year risk-statistics line whose standard deviation is about `0.1370` rather than `13.70`, and then this project's own annual return, volatility and Sharpe ratio under a heading naming the real first and last month of the returns they came from and a line naming where the risk-free rate came from. Every ratio on screen is a four-decimal fraction and no line contains a `%`.

Choose `[s]` and type a ticker that is not in the pool. Accepted when the same shape of block prints and the following `Candidate pool (N): ...` line is unchanged from before.

Choose `[s]` and type an ordinary company share such as `T`. Accepted when the fund lines collapse to one line beginning `Yahoo fund figures: n/a - ` and naming the quote type, the sector, market-capitalization and multiples lines print instead, and this project's own figures still print in full.

Choose `[s]` and type `ZZZZQQQ`. Accepted when both halves of the block print as named `n/a` lines - `Ticker summary: ZZZZQQQ - n/a - could not read this ticker's profile ...` and `This project's own figures: n/a - ...` - and the loop returns to its prompt with no traceback and no change to the pool. Note this is NOT the add prompt's `Ignored (not found): ZZZZQQQ.`: an add reports through `_print_add_outcome` because it is deciding pool membership, while `[s]ummary` reports through the summary block because it is describing a ticker.

Worth checking explicitly, because it is the case that most justifies the feature: add a ticker whose history this project cannot measure. `AVB` is one today - it is in the shipped candidate pool and Yahoo currently serves it a single month. Accepted when its summary prints `This project's own figures: n/a - 1 month(s) of history for AVB in this window, below the 24 required` and the report below then shows no weight for `AVB` at all. The summary is the only warning anywhere in the run that a ticker was admitted and then silently dropped by the optimizer's minimum-history rule.

Re-run with `--no-ticker-summary` and add a ticker. Accepted when the only output is `Added: AMLP.` and the pool line, and when `[s]` still prints a full block.

Finish a run with `[d]one` and compare the weights and share allocation against a run of the same command and pool from before this change. Accepted when they are identical: this feature reports, and must change nothing the optimizer computes.


## Idempotence and Recovery


Every step here is additive and every command may be run repeatedly.

`validate_and_ingest_tickers` merges rather than replaces - `CREATE TABLE IF NOT EXISTS`, then `DELETE` for the requested tickers only, then `INSERT` - so summarizing the same ticker any number of times leaves the database in the same state and disturbs no other ticker's rows. Summary-only ingestion for a ticker outside the pool happens inside a temporary database from `build_scratch_snapshot`, which deletes it in a `finally`; an interrupted run leaves nothing behind but a temporary file the operating system will reclaim, and the shared `data/portfolio.duckdb` cache is never opened for writing at all, because `open_pipeline_session` already hands the `user_provided` selection a throwaway snapshot.

Nothing in this plan writes to `memory/`. The candidate pool is still saved only where it was saved before, by the existing code paths, and `[s]ummary` writes nothing anywhere.

The one change to existing behavior that could in principle break something is Milestone 2's refactor of `load_benchmark_returns`. It is an extract-and-delegate with no change to the function's signature, docstring or return value, and reverting it is a two-line change. Run `uv run pytest tests/test_benchmark.py` immediately after it; if that passes, the refactor is safe.

If a milestone must be abandoned partway, the feature is inert until Milestone 4 wires it in: Milestones 1 through 3 add new modules and new functions that nothing calls, so the command behaves exactly as it did before with them merged.


## Artifacts and Notes


Three real network runs, 2026-09-10, each against its own throwaway `--memory-path` and `--rates-path` so the checked-out `memory/` was untouched. Input was piped, which is why each typed answer appears on the same line as the prompt that asked for it.

RUN ONE - the fund path, and the discovery described in `Outcomes & Retrospective`:

    $ printf 'a\nAMLP AVB\ns\nZZZZQQQ\nd\nf\n' | uv run portfolio --date today \
        --value 100000 --selection user_provided --memory-path /tmp/cand.json \
        --rates-path /tmp/rates.json --no-holdings

    Added: AMLP, AVB.

    Ticker summary: AMLP - Alerian MLP ETF  (ETF, USD)
      Category: Energy Limited Partnership   Family: ALPS   Legal type: Exchange Traded Fund
      Expense ratio: 0.0101 (category average 0.0157)   Holdings turnover: 0.1400
      Net assets: $13,442,306,048.00 USD   52-week range: 44.64 - 56.29
      Trailing P/E: 15.5220

      Yahoo trailing total returns as of 2026-09-08, fund / category:
        YTD 0.2497 / 0.2384   1M 0.0242 / 0.0278   3M 0.0968 / 0.1529   1Y 0.2355 / 0.3076
        3Y 0.1967 / 0.2578   5Y 0.2069 / 0.2279   10Y 0.0737 / 0.1023
      Yahoo risk statistics (3y): alpha=0.1044  beta=0.2700  stdDev=0.1370  Sharpe=1.0500

      This project's own figures, 2021-10-01 to 2026-09-01 (60 month(s) of monthly returns):
        Annual return: 0.1893   Annual volatility: 0.1874   Sharpe: 0.9037
        Risk-free rate used: 0.0200 (the configured default)
        Trailing 12-month dividend yield: 0.0724

    Ticker summary: AVB - AvalonBay Communities Inc  (EQUITY, USD)
      Sector: Real Estate   Industry: REIT - Residential
      Market cap: $26,283,393,024.00 USD   52-week range: 57.32 - 185.62
      Trailing P/E: 25.2483   Forward P/E: 36.5844   Beta: 0.7730
      Yahoo fund figures: n/a - AVB is not a fund (quoteType EQUITY), so Yahoo publishes no
        category comparison or fund risk statistics for it

      This project's own figures: n/a - 1 month(s) of history for AVB in this window,
        below the 24 required
    Candidate pool (2): AMLP, AVB

    [s]ummary of a symbol Yahoo does not know:

    Ticker summary: ZZZZQQQ - n/a - could not read this ticker's profile from Yahoo Finance,
      so there is nothing to describe it with
      This project's own figures: n/a - yfinance returned no non-null close/adj_close values
        for this ticker (or its yfinance-mapped symbol) across the full
        2021-04-10..2026-09-15 fetch window; ...

    Candidates (2): AMLP, AVB
    Weights:
      AMLP: 1.0000

Note what those last two lines prove, because it is the strongest justification this feature has. `AVB` was admitted to the pool and then dropped by the optimizer's own minimum-history rule, receiving no weight and appearing on no line of the report - and the ONLY warning of that anywhere in the run is the `n/a` in its summary. Before this change the run said `Added: AVB.` and then printed a weights table that quietly did not mention it. `AVB` is one of the four tickers this project already records in `dividend_unresolved` as having broken Yahoo history, and it is in the shipped candidate pool.

RUN TWO - the `--no-ticker-summary` check, same command with the flag added and `printf 'a\nAMLP\ns\nAMLP\nd\nf\n'`:

    Edit candidate pool? [a]dd tickers / [r]emove tickers / [s]ummary / [d]one: a
    Ticker(s) to add (space-separated): AMLP
    Added: AMLP.
    Candidate pool (1): AMLP

    Edit candidate pool? [a]dd tickers / [r]emove tickers / [s]ummary / [d]one: s
    Ticker to summarize: AMLP

    Ticker summary: AMLP - Alerian MLP ETF  (ETF, USD)
      ... the full block, exactly as in run one ...

The add prints nothing but `Added: AMLP.` and the pool line, while `[s]` still prints the whole block: the automatic half off, the on-demand half working, which is the distinction the flag is for.

RUN THREE - a company share with healthy history, plus `[s]ummary` on a ticker that is NOT in the pool. `printf 'a\nT\ns\nSPY\nd\nf\n'`:

    Added: T.

    Ticker summary: T - AT&T Inc.  (EQUITY, USD)
      Sector: Communication Services   Industry: Telecom Services
      Market cap: $172,337,496,064.00 USD   52-week range: 19.89 - 29.79
      Trailing P/E: 8.4396   Forward P/E: 9.8100   Beta: 0.4310
      Yahoo fund figures: n/a - T is not a fund (quoteType EQUITY), so Yahoo publishes no
        category comparison or fund risk statistics for it

      This project's own figures, 2021-10-01 to 2026-09-01 (60 month(s) of monthly returns):
        Annual return: 0.1382   Annual volatility: 0.2589   Sharpe: 0.4566
        Risk-free rate used: 0.0200 (the configured default)
        Trailing 12-month dividend yield: 0.0442
    Candidate pool (1): T

    Edit candidate pool? ... : s
    Ticker to summarize: SPY

    Ticker summary: SPY - State Street SPDR S&P 500 ETF Trust  (ETF, USD)
      Category: Large Blend   Family: State Street Investment Management
        Legal type: Exchange Traded Fund
      Expense ratio: 0.0009 (category average 0.0072)   Holdings turnover: 0.0300
      Net assets: $811,937,038,336.00 USD   52-week range: 629.28 - 779.37
      Trailing P/E: 24.6329

      Yahoo trailing total returns as of 2026-09-08, fund / category:
        YTD 0.1307 / 0.0514   1M 0.0271 / 0.0943   3M 0.0166 / 0.0342   1Y 0.2026 / 0.2772
        3Y 0.2091 / 0.1934   5Y 0.1269 / 0.1120   10Y 0.1527 / 0.1377
      Yahoo risk statistics (3y): alpha=-0.0009  beta=1.0000  stdDev=0.1292  Sharpe=1.1900

      This project's own figures, 2021-10-01 to 2026-09-01 (60 month(s) of monthly returns):
        Annual return: 0.1225   Annual volatility: 0.1481   Sharpe: 0.6919
        Risk-free rate used: 0.0200 (the configured default)
        Trailing 12-month dividend yield: 0.0099

    Candidates (1): T

`SPY` was summarized without joining the pool - the report below it still holds only `T`. Two other things in that block are worth pointing at. `SPY`'s expense ratio of 0.0009 beside a category average of 0.0072 is the whole argument for printing the category: nine basis points means nothing until you know its peers charge seventy-two. And Yahoo's three-year Sharpe ratio of 1.19 sits beside this project's 0.6919 over sixty months - both correct, measured over different windows with different conventions, which is exactly why each figure prints the window that produced it.

RUN FOUR - `[s]ummary` in the POST-RUN edit loop, which the first three runs never reached because each finished with `f` straight away. `printf 'a\nAMLP\nd\ns\nSPY\nf\n'` with `--no-ticker-summary`:

    Edit candidates? [a]dd tickers / [r]emove tickers / [o]bjective / [t]arget-return /
      [d]ividend / [b]enchmark / [s]ummary / [f]inish: s
    Ticker to summarize: SPY

    Ticker summary: SPY - State Street SPDR S&P 500 ETF Trust  (ETF, USD)
      Category: Large Blend   Family: State Street Investment Management
        Legal type: Exchange Traded Fund
      Expense ratio: 0.0009 (category average 0.0072)   Holdings turnover: 0.0300
      ... the full block ...
        Risk-free rate used: 0.0200 (the configured default)
        Trailing 12-month dividend yield: 0.0099

    Edit candidates? [a]dd tickers / [r]emove tickers / [o]bjective / [t]arget-return /
      [d]ividend / [b]enchmark / [s]ummary / [f]inish:

Three things to check in that output. `[s]ummary` appears in the prompt, which it must not for any other selection. The block prints and the loop returns to the SAME prompt - no recompute, no reprinted report, nothing reverted, because a summary changes nothing. And the risk-free rate line reads the rate this session settled on rather than one resolved afresh, so it agrees with the report printed above it.

The payloads captured while writing this plan, which the Milestone 1 fixtures are copied from, are recorded in `Surprises & Discoveries` above.


## Interfaces and Dependencies


No new third-party dependency. This plan uses `yfinance` (already required by `pyproject.toml`), `pandas`, `numpy`, `duckdb` and `pypfopt`, all already in use, and nothing else. The one unusual import is `yfinance.data.YfData`, which is not part of yfinance's public API and is therefore imported inside the function that needs it - see `src/dataset/ticker_profile.py`'s docstring and this plan's `Decision Log`.

These are the signatures as shipped. In `src/dataset/ticker_profile.py`:

    QUOTE_SUMMARY_URL = "https://query2.finance.yahoo.com/v10/finance/quoteSummary/{symbol}"
    FUND_MODULES = "fundProfile,fundPerformance"
    FUND_QUOTE_TYPES = frozenset({"ETF", "MUTUALFUND"})
    TRAILING_RETURN_PERIODS: tuple[tuple[str, str], ...]   # printed label -> Yahoo key
    RISK_STATISTICS_PERIOD = "3y"
    PERCENT_RISK_STATISTICS = frozenset({"alpha", "stdDev"})
    RISK_STATISTIC_KEYS = ("alpha", "beta", "stdDev", "sharpeRatio")
    IDENTIFYING_INFO_KEYS = ("quoteType", "longName", "shortName")
    INFO_FETCH_FAILED_REASON: str

    class RawTickerProfile(NamedTuple):
        info: dict
        fund_profile: dict | None
        fund_performance: dict | None
        info_reason: str | None = None
        fund_data_reason: str | None = None

    class TickerProfile(NamedTuple):
        ticker: str
        # identity, absent only when `unavailable_reason` is set
        long_name / quote_type / currency: str | None
        fifty_two_week_low / fifty_two_week_high: float | None
        # fund fields, absent when `fund_data_reason` is set
        category_name / family / legal_type: str | None
        expense_ratio / expense_ratio_category / holdings_turnover / net_assets: float | None
        trailing_returns / trailing_returns_category: dict[str, float] | None
        trailing_returns_as_of: date | None
        risk_statistics: dict[str, float] | None
        # equity fields, populated whenever `info` carried them, any quote type
        sector / industry: str | None
        market_cap / trailing_pe / forward_pe / beta: float | None
        fund_data_reason: str | None
        unavailable_reason: str | None

    def fetch_ticker_profile(
        ticker: str,
        pause_seconds: float = settings.yfinance_fundamentals_pause_seconds,
    ) -> RawTickerProfile: ...      # the ONLY network I/O in this module

    def build_ticker_profile(ticker: str, raw: RawTickerProfile) -> TickerProfile: ...   # pure

In `src/optimizer/benchmark.py`, added, with `load_benchmark_returns` reduced to a one-line call to it and its own signature, docstring and behavior unchanged:

    def load_ticker_monthly_returns(ticker: str, as_of: date, db_path: str) -> pd.Series: ...

In `src/optimizer/ticker_stats.py` - reads a database, imports nothing from `src/flow` and nothing from `yfinance`:

    DEFAULT_LOOKBACK_MONTHS = 60
    TICKER_MIN_MONTHS = 24          # == benchmark.BENCHMARK_MIN_MONTHS, deliberately

    class TickerStats(NamedTuple):
        ticker: str
        currency: str | None
        annual_return / annual_volatility / sharpe: float | None
        risk_free_rate: float
        window_start / window_end: date | None
        window_months: int
        dividend_yield: float | None
        dividend_lookback_months: int | None
        dividend_unavailable_reason: str | None
        unavailable_reason: str | None

    def ticker_stats(
        ticker: str,
        as_of: date,
        db_path: str,
        currency: str | None = None,
        risk_free_rate: float = settings.risk_free_rate,
        lookback_months: int = DEFAULT_LOOKBACK_MONTHS,
        min_months: int = TICKER_MIN_MONTHS,
        dividend_lookback_months: int = settings.dividend_lookback_months,
    ) -> TickerStats: ...

In `src/flow/interactive.py` - the only layer that decides whether to fetch and into which database:

    class TickerSummary(NamedTuple):
        profile: TickerProfile
        stats: TickerStats
        risk_free_rate_origin: str | None = None

    def prepare_ticker_summary(
        ticker: str,
        as_of: date,
        pool: list[str],
        session_db_path: str,
        currency: str | None = None,
        risk_free_rate: float | None = None,          # a SETTLED rate, from _run_edit_loop
        risk_free_rate_origin: str | None = None,
        rates_path: str | None = DEFAULT_RATES_PATH,  # to resolve one, from the confirm loop
        risk_free_rate_override: float | None = None, # this run's --risk-free-rate
        profile_cache: dict[str, TickerProfile] | None = None,
        allow_fetch: bool = True,
    ) -> TickerSummary: ...          # never raises

In `src/flow/cli.py`:

    NO_FUND_FIGURES_LABEL = "Yahoo fund figures"

    def format_ratio_pair(label: str, value: float | None, category: float | None) -> str | None
    def format_trailing_return_rows(
        returns: dict[str, float], category: dict[str, float] | None, per_row: int = 4
    ) -> list[str]
    def format_risk_statistics(statistics: dict[str, float]) -> str | None
    def format_ticker_dividend_yield(stats: TickerStats) -> str
    def print_ticker_summary(
        profile: TickerProfile, stats: TickerStats, risk_free_rate_origin: str | None = None
    ) -> None

and both interactive loops gained the same four additive, defaulted parameters, so every existing caller and every existing test keeps working unchanged:

    _run_user_provided_confirm_loop(..., rates_path=DEFAULT_RATES_PATH,
        risk_free_rate=None, show_summaries=False, profile_cache=None)
    _run_edit_loop(..., show_summaries=False, profile_cache=None)

`show_summaries` defaults to `False` on both even though `main` always passes `True` - see the `Decision Log` for why a `True` default was a mistake worth reverting rather than a convenience.

`main` gained `--no-ticker-summary` and one `profile_cache: dict[str, TickerProfile]` shared by both loops.


## Revision Note: transcripts and acceptance replaced with what actually ran (2026-09-10)


This plan was written before the code and then revised after it, in three places, because a plan whose transcripts do not match the program is worse than one with no transcripts at all.

The transcript in `Purpose / Big Picture` originally carried illustrative figures worked out from the captured Yahoo payloads (`Annual return: 0.2071`, a `(12 month(s) of dividend coverage)` suffix on the dividend line, and `AVB` as the on-demand example). Those are now the real lines from the run recorded in `Artifacts and Notes`: this project measures `AMLP` at 0.1893/0.1874/0.9037 over 2021-10-01 to 2026-09-01, the dividend line carries no coverage suffix (see the `dividend_lookback_months` entry in the `Decision Log` for why the field it would have come from does not exist), and the on-demand example is `T`, since `AVB` turned out to have too little history for the second half to print at all. A fifth numbered observation was added under the transcript, about the two halves legitimately disagreeing, because that is the thing the live runs made obvious and the original four points did not cover.

`Concrete Steps` and `Validation and Acceptance` originally said a `[s]ummary` of an unresolvable symbol would print `Ignored (not found): ZZZZQQQ.`. It does not, and should not: that message belongs to `_print_add_outcome`, which is deciding pool membership, whereas `[s]ummary` is describing a ticker and so reports through the summary block's own `n/a` lines. Both sections now say so, and `Validation` gained the `AVB` check - a ticker admitted to the pool and then silently dropped by the optimizer, which the summary is the only warning of.

The counts throughout were placeholders (`roughly 40 to 50 more tests`) and are now the measured 812 to 880.
