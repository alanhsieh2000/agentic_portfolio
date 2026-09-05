# Support non-US tickers, and make a portfolio's currency explicit and single


This ExecPlan is a living document. The sections `Progress`, `Surprises & Discoveries`, `Decision Log`, and `Outcomes & Retrospective` must be kept up to date as work proceeds. This plan must be maintained in accordance with `PLANS.md` at the repository root. This plan builds on `plans/01_dataset.md` (for the `prices`/`returns` tables and the fetch/upsert functions it extends), `plans/05_optimizer_and_allocation.md` (for `load_returns_matrix`/`compute_weights`/`load_latest_prices`/`allocate_shares`), `plans/09_user_provided_selection.md` (for the `user_provided` selection, its candidate memory, and its two interactive loops), and `plans/10_performance_reporting_and_target_return.md` (for `PortfolioStats`, `print_weights_and_allocation`'s current shape, and the `except ValueError` recovery this plan reuses) — all four checked into this repository.


## Purpose / Big Picture


Before this change, a person could not build a portfolio from anything but US-listed stocks. Typing `7203.T` (Toyota, on the Tokyo Stock Exchange) or `BARC.L` (Barclays, on the London Stock Exchange) into the candidate pool got it rejected as if it were a typo. The cause was a one-line translation, `src/dataset/prices.py`'s `to_yfinance_symbol`, which rewrote every dot in a ticker to a dash. That is correct for the way Wikipedia writes US share classes (`BRK.B` really is `BRK-B` on Yahoo Finance) but it destroys Yahoo Finance's own convention, which uses a literal dot to name the exchange a symbol trades on. `7203.T` became `7203-T`, which does not exist.

Fixing that one line alone would have been actively harmful, and understanding why is the whole design of this plan. Prices are stored exactly as Yahoo Finance reports them, in whatever currency the exchange quotes — yen for Tokyo, pence for London. Nothing in this project recorded, checked, or converted a currency. So a pool containing both `AAPL` (dollars) and `7203.T` (yen) would have been handed to the share-allocation step as a set of bare numbers with no units, and that step would have compared a ¥2,500 price against a dollar budget as though the two were the same thing. The result is not an error message; it is a portfolio that looks fine and is wrong. Asking for a 50/50 split of $100,000 between those two would have bought 20 shares of Toyota — about $333 worth — alongside $50,000 of Apple, leaving nearly half the money unspent and reporting `Leftover cash: $0.00`.

The good news, and the reason this plan is smaller than it might have been, is that a portfolio built entirely from *one* currency already computes correctly, and did so before this change. Two facts make that true. Monthly returns are ratios of one ticker's own prices, so the currency cancels and the optimizer's weights never depend on it. And the share-allocation step divides a budget by a price, so scaling every price and the budget by the same factor leaves the whole-share counts identical. A yen portfolio with a yen budget was therefore always right — it simply had no way to say so, and no protection against a dollar ticker wandering in.

So this plan does not convert currencies. It makes the currency *known*, *visible*, and *uniform*: each ticker's trading currency is looked up and recorded when the ticker is added, London's pence quotes are normalized to pounds, a ticker whose currency differs from the pool's is refused by name at the moment it is typed, and the portfolio's currency is printed alongside the money it describes. Then, and only then, the symbol translation is fixed so foreign tickers can be added at all.

Concretely, this is the observable outcome. Building a yen portfolio:

    $ uv run portfolio --date today --objective GMV --value 15000000 \
        --selection user_provided --memory-path /tmp/jpy.json --risk-free-rate 0.005

    Current candidate pool (0): (empty)

    Edit candidate pool? [a]dd tickers / [r]emove tickers / [d]one: a
    Ticker(s) to add (space-separated): 7203.T 6758.T
    Added: 6758.T, 7203.T.
    Candidate pool (2): 6758.T, 7203.T

    Edit candidate pool? [a]dd tickers / [r]emove tickers / [d]one: a
    Ticker(s) to add (space-separated): AAPL
    Refused: AAPL is priced in USD but this pool is JPY. A portfolio cannot mix currencies; run them separately.
    Candidate pool (2): 6758.T, 7203.T

    Edit candidate pool? [a]dd tickers / [r]emove tickers / [d]one: d
    Saved 2 ticker(s) to /tmp/jpy.json.

    Portfolio currency: JPY - --value is interpreted as JPY

    Weights:
      6758.T: 0.5009
      7203.T: 0.4991
    ...
    Share allocation:
      6758.T: 1934
      7203.T: 2429
    Leftover cash: ¥2,661.00 JPY

`AAPL` is refused by name, in the same breath as it is typed, with the reason stated — and the two yen tickers that were already accepted are untouched. That refusal is the point of the plan; the yen portfolio itself was already computable.

The share counts are what show the yen arithmetic is genuinely right rather than merely labelled: 2,429 shares of Toyota at roughly ¥3,081 is about ¥7.5 million, half of the ¥15 million allocated, which is what a near-even split should buy.

The second observable outcome is the pence normalization, which is invisible unless you know to look for it:

    Ticker(s) to add (space-separated): BARC.L VOD.L
    Added: BARC.L, VOD.L.
    ...
    Portfolio currency: GBP - --value is interpreted as GBP

    Share allocation:
      BARC.L: 9286
      VOD.L: 44302
    Leftover cash: £1.53 GBP

Barclays quotes on the London Stock Exchange in pence, so Yahoo Finance reports a price near 493 for a share that costs about four pounds ninety. Stored unchanged, that price would have bought a hundredth of the intended number of shares — and, unlike a yen price, 493 looks like a perfectly plausible dollar figure, so nothing would have looked wrong anywhere. This plan divides such quotes by 100 at ingestion and records the currency as `GBP`, so the stored price is 4.9340 against a raw 493.40 and the 9,286-share position is right. The direct proof of that ratio is in Artifacts and Notes.


## Progress


- [x] (2026-09-05) M1: added `src/dataset/ticker_currency.py` with `DEFAULT_CURRENCY`, `MINOR_UNIT_CURRENCIES`, `CURRENCY_LOOKUP_FAILED_REASON`, `MixedCurrencyPoolError`, the pure helpers (`normalize_currency`, `apply_price_multipliers`, `group_by_currency`, `partition_by_currency`), the storage pair (`upsert_ticker_currency_table`, `load_ticker_currencies`), and the single network seam `fetch_ticker_currencies`. Added the `ticker_currency` table to `src/flow/live.py`'s `_init_empty_price_and_returns_tables`. Covered by 22 tests in the new `tests/test_ticker_currency.py` plus 6 storage tests in `tests/test_dataset_upsert.py`.
- [x] (2026-09-05) M2: wired the currency lookup, the pence normalization, and the currency upsert into `validate_and_ingest_tickers`, widening its return to `(valid, invalid, currencies)`. Covered by 5 new tests in `tests/test_ticker_ingestion.py` plus the 8 existing ones widened.
- [x] (2026-09-05) M3: added `CandidateEditResult` and `_in_typed_order` to `src/flow/interactive.py`, threaded the currency through both `src/flow/cli.py` loops, added `_resolve_mixed_persisted_pool`'s report-and-prompt, and added the `_require_single_currency` guard to `compute_weights_and_allocation`.
- [x] (2026-09-05) M4: added `KNOWN_EXCHANGE_SUFFIXES` and rewrote `to_yfinance_symbol` around it — the change that finally lets a foreign ticker resolve, landed last by design. Added `CURRENCY_SYMBOLS`, `format_money`, the `Portfolio currency:` line, and the clarified `--value` help text.
- [x] (2026-09-05) M6: made `memory/candidates.json` hold one pool PER CURRENCY instead of one flat list, so a standing JPY pool and a standing USD pool live in the same default file rather than requiring different `--memory-path` values. Rewrote `src/flow/candidate_memory.py` around `load_all_pools`/`_load_raw_pools`, gave `load_candidate_pool`/`save_candidate_pool` a `currency` parameter, made saving one currency preserve every other's tickers *and* `updated_at`, and auto-migrated the old flat shape as the USD pool. Added `_choose_pool_to_resume` to `src/flow/cli.py`. Suite 268 to 283 passed. See Revision Note 1.
- [x] (2026-09-05) M5: verified the full suite (268 passed, up from 217) and verified end to end with four real runs — a JPY pool refusing `AAPL`, a GBP pool proving the pence normalization, an unchanged USD pool, and a backtest-date run leaving `data/portfolio.duckdb` byte-identical. Added the `README.md` Live Mode note. Transcripts in Artifacts and Notes.


## Surprises & Discoveries


- Discovery (2026-09-05): the test suite could not see that this change had broken production, because the tests mock the very seam that broke. After M2 widened `validate_and_ingest_tickers` to return three values, `uv run pytest tests/test_*.py` reported **247 passed, 0 failed** — while both real callers still unpacked two values and would raise on the first keystroke. `tests/test_cli.py`'s `_fake_ingestion` and `tests/test_interactive_flow.py` both monkeypatch `validate_and_ingest_tickers`, so no test ever exercised the real function against its real callers. Confirmed by calling the caller directly with a correctly-shaped stub:

        ValueError: too many values to unpack (expected 2)

  This is worth recording because the suite's green result was actively misleading, and the same blind spot will exist for any future change to a mocked function's signature. What caught it was reading the call sites after changing the callee, not running the tests. The lesson for this repository: a seam mocked in every test is a seam whose contract no test enforces.

- Surprise (2026-09-05): London's pence quote is dangerous precisely because it looks reasonable. Verified against Yahoo Finance through this repository's own `_fetch_batch`, `BARC.L` reported `184.119995` for 2024-04-02 while Barclays genuinely traded near GBP 1.84 — and **both** price columns are affected, `adj_close` reading `184.006180`, which matters because `load_latest_prices` reads `adj_close` rather than `close`. A yen price of 3,081 is obviously not a dollar price and would invite suspicion; 184 is an entirely plausible dollar figure, so an unnormalized London holding would have produced a hundredth of the intended position with nothing looking wrong anywhere. This is why the multiplier is applied to both columns and why the plan treats it as a correctness issue rather than cosmetics.

- Discovery (2026-09-05): `fast_info.currency` distinguishes pence from pounds by one character of case, so the normalization table must be case-sensitive. Verified live: `AAPL` reports `'USD'`, `7203.T` reports `'JPY'`, and both `BARC.L` and `VOD.L` report `'GBp'` — lowercase `p`. Since some London instruments do quote in genuine `GBP`, an `.upper()` anywhere before the `MINOR_UNIT_CURRENCIES` lookup would silently divide those by 100. Pinned by `test_normalize_currency_is_case_sensitive_so_pounds_are_not_divided`. A related trap avoided: `Ticker.info` reports `financialCurrency='GBP'` for `BARC.L` even while `currency='GBp'`, so keying the conversion off the wrong field would miss the factor of 100 entirely.

- Observation (2026-09-05): the end-to-end evidence for the pence fix is unusually clean, because the ×100 is unmistakable. Ingesting `BARC.L` for real and reading back what was stored:

        raw yfinance adj_close (pence):   493.4000
        stored in prices  (pounds):       4.9340
        ratio stored/raw:                 0.010000   (expect 0.01)
        ticker_currency row:              [('GBP', 'GBp', 0.01)]
        returned currencies:              {'BARC.L': 'GBP'}

  The same effect is visible in the share counts without any database access: a GBP 45,820 allocation buys 9,286 shares of a GBP 4.93 stock, where the unnormalized price would have bought about 93.

- Discovery (2026-09-05): a test asserting "nothing was persisted" turned out to be asserting the wrong thing, and the distinction is worth keeping. When every ticker in an add is refused, `_run_edit_loop` still reaches its `save_candidate_pool` call — but the pool is unchanged at that point, so the write is content-identical and harmless. The requirement is that a *refused ticker never reaches disk*, not that no write occurs, so the test now asserts the contents of every save call rather than the absence of one. Recorded because the first formulation would have forced a needless change-detection branch into the loop to satisfy a test that was measuring an implementation detail.

- Surprise (2026-09-05): the sorted return of `validate_and_ingest_tickers` would have picked the pool's currency by alphabetical accident. That function returns its valid tickers sorted, and `'7203.T'` sorts before `'AAPL'` because digits precede letters in ASCII — so typing `AAPL 7203.T` into an empty pool would have established a **yen** pool and refused the dollar ticker the person named first. `_in_typed_order` re-derives the typed order before the currency is established. Pinned from both directions by `test_confirm_loop_currency_is_established_by_what_was_typed_first` and its mirror image, which is the kind of bug that is invisible until someone types the two orders and gets different answers.

- Discovery (2026-09-05): keeping the single-pool case frictionless would have made a second currency impossible to create, and only a real run showed it. The M6 design said to auto-resume without prompting when exactly one pool is saved. Running it against this repository's actual `memory/candidates.json` — one legacy USD pool of twelve tickers — the intended JPY session did this instead:

        Current candidate pool (12): AMLP, BIL, BOXX, GOOGL, NVDA, PFF, ...
        Edit candidate pool? ... : Saved 12 USD ticker(s) to /tmp/candidates.json.
        Portfolio currency: USD

  The two Tokyo tickers typed at the add prompt were refused — correctly, since the pool was USD — but the person had been placed in that pool with no way out, so a JPY pool could never be created at all. The whole feature was unreachable for exactly the case that motivated it. Fixed by offering `[n]ew` whenever anything is saved, with a bare Enter accepting the sole pool. Every unit test passed both before and after the fix, because each one supplies its own pool fixture and none could observe that no path existed to a second currency; what caught it was running the real command against the real file.

- Observation (2026-09-05): the timestamp is the part of a multi-pool save that is easy to get wrong invisibly. A first draft of `save_candidate_pool` rebuilt the file from `load_all_pools` (which returns only tickers) and stamped `updated_at` with the current time for every currency, so saving a JPY pool made an untouched USD pool look freshly written — quietly destroying the one field a future staleness rule would depend on, while every ticker remained correct. Fixed by reading the raw entries (`_load_raw_pools`) and preserving each untouched pool's own timestamp, pinned by `test_saving_one_currency_preserves_anothers_timestamp`, and confirmed in the real file afterwards: the migrated USD pool kept `2026-09-05T01:49:26.829379+00:00` from before this feature existed while the new JPY pool carried its own.

- Discovery (2026-09-05): a read helper was quietly creating the database it was asked to read, and the evidence was a stray file in the repository root. `git status` after a green test run showed an untracked `unused.duckdb` — the literal path several tests pass as a throwaway `db_path`. The cause was `load_ticker_currencies` calling `duckdb.connect(db_path)`, which creates a database when the file is absent, so the new mixed-currency guard was materializing a file merely by asking a question about one. Fixed by opening read-only, following `src/dataset/fundamentals.py`'s `get_factor_reference_stats`, and treating `duckdb.IOException` (raised for a missing file in read-only mode) exactly like the missing-table case. Pinned by `test_load_ticker_currencies_does_not_create_a_database`. Worth recording because the test suite was entirely green while doing this: nothing asserted on the filesystem, and the symptom was visible only in `git status`.

- Observation (2026-09-05): the guard added as a last line of defense costs the rest of the project nothing measurable, which was the point of the missing-row-means-dollars policy. `_require_single_currency` runs on every `compute_weights_and_allocation` call including the entire backtest path, and in every one of those cases `load_ticker_currencies` hits `duckdb.CatalogException` on a database with no `ticker_currency` table, returns `{}`, and yields one implicit US-dollar group. The whole pre-existing suite stayed green through M3 with no changes to any backtest or LLM-selection test, and `test_compute_weights_and_allocation_is_unaffected_without_a_currency_table` pins it deliberately.


## Decision Log


- Decision: enforce a single currency per portfolio and do not implement any currency conversion.
  Rationale: repository owner's explicit choice when asked, after the investigation showed a single-currency portfolio already computes correctly (see Context and Orientation). Conversion would be a substantially larger change with its own design questions — which layer converts, whether both raw and converted prices are stored, what happens to the shared 2020-2024 cache — and it would change what the `returns` table *means*, since a dollar-based investor's return on a Tokyo stock includes the yen/dollar move and therefore belongs in the covariance matrix. Enforcing one currency delivers the requested capability (a yen portfolio, a pound portfolio, run separately) without pre-committing any of that. Conversion is left to a future dedicated plan, which this plan's recorded per-ticker currency is a prerequisite for.
  Date/Author: 2026-09-05, decided during the planning interview.
- Decision: refuse a cross-currency ticker at the moment it is added, rather than warning later or refusing at the allocation step.
  Rationale: repository owner's explicit choice when asked. The pool then never exists in a mixed state, so no downstream code has to cope with one, and the message can name the offending ticker and both currencies while the person is still looking at what they typed. Refusing later would mean either printing weights that silently ignore currency risk or discarding work already done. Good tickers typed on the same line as a refused one are still added, matching the established behavior for an unresolvable ticker (`Ignored (not found):`) from `plans/09_user_provided_selection.md`.
  Date/Author: 2026-09-05, decided during the planning interview.
- Decision: normalize pence-quoted prices in this project's own code, rather than passing `repair=True` to yfinance.
  Rationale: repository owner's explicit choice when asked. yfinance's `PriceHistory._standardise_currency` does perform exactly the conversion needed (`GBp`→`GBP`, `ZAc`→`ZAR`, `ILA`→`ILS`, all at ×0.01) but only inside its `repair=True` path, which also enables a much broader set of price-repair heuristics that would change the numbers for every existing ticker — including everything already cached in `data/portfolio.duckdb`. An explicit multiplier applied to the tickers this plan ingests is testable, auditable through the stored `price_multiplier` column, and cannot affect any other path.
  Date/Author: 2026-09-05, decided during the planning interview.
- Decision: store currency in a new `ticker_currency` table rather than as a column on `prices`.
  Rationale: currency is a property of a ticker, not of a `(date, ticker)` price observation. As a `prices` column it would repeat across roughly 1,300 rows per ticker and admit rows that contradict each other — a state with no meaning that would then need its own validation. It would also touch both price writers (`write_prices_tables`, `upsert_prices_tables`), their SQL, `src/flow/live.py`'s empty-table initializer, and the fixtures and assertions in `tests/test_dataset_upsert.py`. The separate table touches none of those, which is a checkable signal that it is the additive choice, and it composes with the existing delete-then-insert-by-requested-ticker upsert discipline verbatim.
  Date/Author: 2026-09-05, decided during design.
- Decision: treat a ticker with no recorded currency as `USD`, but treat a ticker whose currency lookup was attempted and *failed* as invalid.
  Rationale: these two situations look identical in the database and must not be handled identically. The first is every ticker in the rest of this project: the S&P 500 membership universe, the shared historical cache, the backtest path. Every one of those is US-listed by construction, so defaulting to `USD` when no row exists is what lets this entire feature cost those paths nothing — no schema change, no code change, and no extra network requests for roughly 500 tickers. The second is a ticker this plan tried to classify and could not; defaulting *that* to `USD` would be precisely the silent-mixing failure the plan exists to prevent, so it is reported as invalid and never joins a pool.
  Date/Author: 2026-09-05, decided during design.
- Decision: order the milestones so that `to_yfinance_symbol` — the one-line fix that makes foreign tickers resolve — is the *last* code change, not the first.
  Rationale: this is the plan's central safety property rather than a preference. Between fixing the symbol translation and adding the currency guards there exists a state in which foreign tickers can be added and nothing stops them mixing with dollar tickers, which is the silent misallocation described in Purpose / Big Picture. Landing the fix last means that state never exists in the repository's history, so no intermediate commit is dangerous to run, and every guard is already covered by tests before the first foreign ticker can be ingested.
  Date/Author: 2026-09-05, decided during design.
- Decision: exclude `A`, `B`, `C`, `K`, `V`, and `F` from `KNOWN_EXCHANGE_SUFFIXES`, while including the single letters `T` and `L`.
  Rationale: the allowlist decides whether a dot means "exchange suffix" (keep it) or "US share class" (convert it to a dash), and single-letter codes are the only ones that can be ambiguous — every other Yahoo Finance exchange code is two or more letters and cannot collide with a share class. `A`, `B`, `C`, and `K` are the letters US listings actually use as share classes (`BRK.B` and `BF.B` are the only dotted tickers this repository has ever produced; `HEI.A` and `MOG.A` are other real examples), so they must keep converting. `T` and `L` are included because Tokyo and London are the exchanges this plan exists to support and no US listing uses those letters as a share class. `V` (TSX Venture) and `F` (Frankfurt) are deliberately left out: they carry the same single-letter collision risk with no offsetting need, and their exchanges remain reachable by other suffixes (`SAP.DE` works; `SAP.F` does not).
  Date/Author: 2026-09-05, decided during design.
- Decision: place the last-line-of-defense mixed-currency guard in `src/flow/interactive.py`'s `compute_weights_and_allocation`, not in `src/optimizer/portfolio.py`'s `allocate_shares`, and have it raise a subclass of `ValueError`.
  Rationale: `allocate_shares` receives only weights, prices, and a total value — it has no `db_path` and therefore cannot resolve a currency without both a signature change and a new dependency from the optimizer package onto the dataset package, which `src/optimizer/portfolio.py`'s own module docstring is careful to avoid. `compute_weights_and_allocation` already holds both the candidate list and `db_path` and is `allocate_shares`'s only caller. Making the exception a `ValueError` subclass means `src/flow/cli.py`'s existing `except ValueError` in `_run_edit_loop` (added by `plans/10_performance_reporting_and_target_return.md` for unreachable `MV` targets) already reverts the edit, reports it, and keeps live mode's snapshot open, so no new error handling is needed anywhere.
  Date/Author: 2026-09-05, decided during design.
- Decision: add this guard even though the add-time refusal should make it unreachable.
  Rationale: this repository's conventions discourage error handling for situations that cannot occur, so this needs justifying rather than assuming. The situation demonstrably *can* occur by two routes that bypass the interactive refusal entirely: a hand-edited `memory/candidates.json`, and `run_pipeline(selection="user_provided", candidates=[...])`, a public documented entry point with no confirmation loop at all. And the consequence of missing it is not a crash but silently misallocated money, which is the worst possible failure mode for this project. The precedent is `_validate_efficient_return_result` in `src/optimizer/portfolio.py`, whose docstring makes the same argument: positively verify rather than assuming silence means success.
  Date/Author: 2026-09-05, decided during design.
- Decision: store one pool per currency inside the single `memory/candidates.json`, keyed by currency code, rather than one pool per file.
  Rationale: repository owner's explicit request. Each pool is single-currency by this plan's own enforcement, so keeping several pools apart by *file* forced a person to remember and type a different `--memory-path` for each currency, which is what this plan's own verification had to do. Keying them inside one file makes the default path hold everything, and `--memory-path` reverts to meaning only "which file" — still useful for a scratch or throwaway file. The write is read-modify-write so saving one currency's pool preserves every other's tickers and its own `updated_at`.
  Date/Author: 2026-09-05, requested by the repository owner after M5.
- Decision: when several pools are saved, list them and prompt for which to resume, rather than adding a `--currency` selector flag.
  Rationale: repository owner's explicit choice when asked. A flag would have to be remembered and retyped on every run to reach a non-default pool, and would silently give the wrong pool when mistyped; the prompt shows what actually exists, with ticker counts, at the moment the choice matters. It also reuses the print-then-prompt shape this plan already established for repairing a mixed pool. The `[n]ew` option is offered even for a single saved pool, for the reason recorded in Surprises & Discoveries — without it a second currency is unreachable.
  Date/Author: 2026-09-05, decided during the M6 planning interview.
- Decision: auto-migrate a legacy flat-shape `candidates.json` as the USD pool instead of requiring a manual migration.
  Rationale: repository owner's explicit choice when asked. This is not a guess about old data: the currency enforcement in M1-M4 did not exist when any such file was written, and before it no non-USD ticker could resolve at all (`to_yfinance_symbol` mangled every exchange suffix), so every pool ever saved under the old shape is necessarily USD. Reading it as the USD pool therefore loses no information and costs the person nothing, and the next save rewrites the file in the new shape.
  Date/Author: 2026-09-05, decided during the M6 planning interview.
- Decision: render money as a symbol followed by the ISO currency code (`$12.34 USD`, `Y12.34 JPY`), rather than a symbol alone or a code alone.
  Rationale: a bare `$` is shared by the US, Canadian, Australian, Hong Kong and Singapore dollars, and removing exactly that kind of ambiguity is the entire purpose of this plan, so a symbol alone would reintroduce the problem the plan is solving. A code alone is unambiguous but harder to scan. Printing both costs nothing and degrades gracefully for a currency with no symbol in the table, which prints the code alone. It also happens to keep the existing test assertion `"Leftover cash: $12.34"` passing, since that string remains a substring of `"Leftover cash: $12.34 USD"`.
  Date/Author: 2026-09-05, decided during design.


## Outcomes & Retrospective


The stated purpose is met. A Tokyo or London ticker can now be added to a candidate pool; each portfolio carries exactly one currency, established by the first ticker added and enforced by refusing any ticker that disagrees, by name and with the reason; London's pence quotes are normalized to pounds; every money figure is printed with both its symbol and its ISO code; and (per M6) every currency's pool lives in the one `memory/candidates.json`, listed and chosen at session start, with saving one pool never disturbing another. Verified with four real runs against live market data, not only in tests: a yen pool of `7203.T` and `6758.T` that refused `AAPL`, a pound pool of `BARC.L` and `VOD.L` whose stored prices were confirmed to be exactly one hundredth of what Yahoo Finance reported, an unchanged dollar pool, and a backtest-date run that left `data/portfolio.duckdb` byte-identical. The suite grew from 217 to 268 tests, all passing.

The milestone ordering earned its keep, and is the part of this plan most worth reusing. Fixing `to_yfinance_symbol` is a three-line change and the obvious place to start; landing it last instead meant that at no point did the repository contain a state where foreign tickers could be ingested but nothing stopped them mixing with dollar tickers. Every guard was written and tested against a tree in which the dangerous input was still unreachable. The cost was some discipline about ordering; the benefit is that no intermediate commit here is unsafe to check out and run.

Two things went better than expected. The decision that a missing `ticker_currency` row means US dollars turned out to carry almost the whole design: it is why the S&P 500 membership path, all three LLM selections, the backfill script, the backtest runner and the shared 2020-2024 cache needed no changes at all and pay no extra network requests, and why the new mixed-currency guard is a single cheap query everywhere outside this feature. And making `MixedCurrencyPoolError` a `ValueError` subclass meant the guard needed no error handling written for it anywhere: the `except ValueError` that `plans/10_performance_reporting_and_target_return.md` added for unreachable MV targets already reverts the edit and keeps live mode's snapshot open.

M6 repeated that lesson in a new shape, and it is the one to remember from this plan. Its approved design said to skip the resume prompt when only one pool is saved, to keep the common case frictionless — a reasonable-sounding choice that made the feature's entire purpose unreachable, since being auto-placed in the sole USD pool left no way to ever create a JPY one. Every unit test passed before and after the fix, because each supplies its own pool fixture and none could observe the absence of a path to a second currency. Running the real command against the real file found it in one attempt. Twice now in this plan, the thing tests could not see was found by using the program.

The most valuable finding was not about currency at all. After the ingestion function's return type widened, the full suite reported 247 passing while both of its real callers were broken badly enough to fail on the first keystroke — because every test monkeypatches that exact function. That blind spot is structural rather than a one-off: any function mocked in all its tests has a contract no test enforces. It was caught by re-reading the call sites after changing the callee, which is now the habit this plan would recommend to anyone widening a mocked signature here.

What remains, all deliberately out of scope. Currency conversion is unbuilt, and with it the only thing it would enable: a single portfolio holding several currencies at once. The per-ticker currency this plan records is the prerequisite a future FX plan needs, and the natural shape of that plan is now clear — fetch an FX series (verified feasible: `JPYUSD=X` and friends work through this repository's existing `fetch_and_reshape_for_tickers` unchanged, with daily coverage that already spans every equity trading day tested), convert prices into a chosen base currency before they reach the optimizer, and accept that doing so changes what the `returns` table means, since a dollar-based investor's return on a Tokyo stock includes the yen move and that belongs in the covariance matrix.

Two landmines are documented rather than fixed, both unreachable today and both named in Interfaces and Dependencies with line references. `src/dataset/fundamentals.py`'s `mve` is the logarithm of a local-currency market capitalization, so a yen figure enters roughly five units high and would read as about four standard deviations above the S&P 500 cross-section — a mid-cap Japanese company looking like the largest company in the world to any rule keyed on size — and its `bm` divides US-dollar SEC book equity by that same local-currency market capitalization. Neither can be reached from this feature, because `user_provided` skips factor construction entirely. And `src/agents/external_screen.py`'s `screen_external_candidate`, which `plans/09_user_provided_selection.md` names as the natural next feature, would hit both problems and additionally fail outright against a `user_provided` session database, which has no `factors` table. Anyone wiring it up should read that section first.


## Revision Note 1 (2026-09-05)


Changed, at the repository owner's request after M5: `memory/candidates.json` now holds one candidate pool per currency rather than one flat ticker list, so every pool lives in that one default file.

Why: M1 through M5 made each pool single-currency, but left persistence one-pool-per-file. That meant anyone wanting a standing JPY pool *and* a standing USD pool had to remember two different `--memory-path` values — which is exactly how this plan's own verification ran (`/tmp/jpy.json`, `/tmp/gbp.json`) and which defeats the purpose of `memory/candidates.json` being the discoverable default. The owner asked for one file holding all of them.

The file shape became `{"pools": {"USD": {"tickers": [...], "updated_at": ...}, "JPY": {...}}}`. `src/flow/candidate_memory.py` gained `load_all_pools` as its primitive, a `currency` parameter on `load_candidate_pool` and `save_candidate_pool`, and a read-modify-write save that carries every other currency's pool over untouched. `src/flow/cli.py` gained `_choose_pool_to_resume`, and `_run_user_provided_confirm_loop` now takes `initial_pools: dict[str, list[str]]` rather than a single pre-loaded list. Two decisions were confirmed with the owner beforehand and are recorded in the Decision Log: the CLI lists the saved pools and prompts rather than adding a `--currency` flag, and a legacy flat-shape file is auto-migrated as the USD pool.

One design flaw in the approved plan was found by real testing and fixed before finishing; it is the most useful thing in this revision and is written up under Surprises & Discoveries. The plan said to auto-resume without prompting when exactly one pool is saved, to keep the common case frictionless. That silently made a second currency unreachable: resuming the sole USD pool unconditionally meant every Tokyo ticker typed afterwards was refused against a pool the person had been placed in with no way out. The `[n]ew` option is now offered whenever anything is saved, with a bare Enter accepting the single-pool case.


## Context and Orientation


This repository builds monthly stock portfolios. The relevant pipeline runs in four stages. First, a candidate pool is chosen — either by one or both LLM agents screening S&P 500 members, or, with `--selection user_provided`, by the person typing tickers directly. Second, `src/optimizer/portfolio.py` reads a `returns` table to build a matrix of each candidate's monthly returns (`load_returns_matrix`) and computes portfolio weights from it (`compute_weights_and_stats`). Third, those weights are turned into whole share counts against the latest prices (`load_latest_prices`, `allocate_shares`). Fourth, everything is printed by `src/flow/cli.py`.

A *rebalance date* is the month a portfolio is being built for; `--date today` means live mode, which fetches fresh data into a throwaway database and deletes it on exit. Data lives in DuckDB, a file-based SQL database; every function that reads it takes a `db_path` parameter, which is why pointing the pipeline at a different database needs no code changes. The `prices` table holds daily closing prices per ticker (columns `date`, `ticker`, `close`, `adj_close`); the `returns` table holds one trailing-one-month return per ticker per month. An *upsert* means a write that replaces only the rows for the tickers it was asked about, leaving every other ticker's rows in place.

Three facts about the current state are what this plan is built on, and each was verified directly rather than assumed.

The first is that **the optimizer's weights cannot depend on currency**. `src/dataset/returns.py`'s `compute_monthly_return` is `(price_at_d / price_one_month_before) - 1.0`, a ratio of two prices of the *same* ticker, so any currency unit cancels exactly; a yen series and a dollar series with the same shape produce identical numbers. And `load_returns_matrix` reads only the `returns` table, never raw prices. So `mu`, the covariance matrix, and every weight are dimensionless.

The second is that **share allocation is scale-invariant**. `allocate_shares` wraps PyPortfolioOpt's `DiscreteAllocation(...).greedy_portfolio()`, whose core step is `int(weight * total_portfolio_value / price)` — a ratio of two money quantities. Multiply every price *and* the total value by any constant and the integer share counts come out identical, with leftover cash scaled by the same constant. This is why an all-yen portfolio with a yen budget is already correct, and it is also why a *mixed* portfolio is wrong: the invariance holds only when one constant applies to everything.

The third is that **nothing anywhere records a currency**. There is no currency column, no conversion, no unit on `--value`, and exactly one currency symbol in any output — a hardcoded `$` in `print_weights_and_allocation`. `DiscreteAllocation` validates only that weights are a dict, that no weight or price is NaN, and that the total value is positive, so a mixed-currency portfolio raises nothing at all.

Two more pieces of context matter for scope. Yahoo Finance reports a ticker's trading currency, but not through the batched `download()` call this project already makes; it requires a separate request per ticker, available as `yf.Ticker(symbol).fast_info.currency`. Verified live: this returns `'USD'` for `AAPL`, `'JPY'` for `7203.T`, and `'GBp'` for both `BARC.L` and `VOD.L`. That `GBp` — lowercase `p` — is Yahoo Finance's code for pence, one hundredth of a pound, and it must be distinguished from `GBP` by case.

And the `user_provided` selection is unusually well isolated, which keeps this plan small. `src/flow/live.py`'s `build_live_snapshot` takes a single-statement branch for it, creating empty `prices`, `unresolved_tickers` and `returns` tables and nothing else — no Wikipedia membership scrape, no factor computation, no news archive. `src/flow/interactive.py`'s `run_scan` short-circuits before either LLM agent runs. So the parts of this project that genuinely *are* currency-broken for a foreign ticker — `src/dataset/fundamentals.py`'s `mve`, which is the logarithm of a market capitalization and therefore shifts additively with the currency, and its `bm`, which divides US-dollar book equity from SEC filings by a local-currency market capitalization — cannot be reached from this feature at all. They are named in Interfaces and Dependencies as an explicit non-goal so a future reader does not have to re-derive it.


## Plan of Work


The work divides into five milestones. Their order is deliberate and is itself a safety property, explained in the Decision Log: the change that lets a foreign ticker resolve lands last, so the repository never passes through a state where foreign tickers work but the guards do not.

**M1** adds one new module, `src/dataset/ticker_currency.py`, and one line elsewhere. The module holds `MINOR_UNIT_CURRENCIES`, mapping each of Yahoo Finance's minor-unit codes to its major unit and multiplier (`GBp`→`GBP` at 0.01, `ZAc`→`ZAR`, `ILA`→`ILS`); `DEFAULT_CURRENCY = "USD"`; a `CURRENCY_LOOKUP_FAILED_REASON` string; and `MixedCurrencyPoolError`, a `ValueError` subclass. Its pure functions are `normalize_currency(raw)`, returning a `(currency, multiplier)` pair and deliberately case-sensitive so that `GBP` is not mistaken for `GBp`; `apply_price_multipliers(long_prices, multipliers)`, which scales *both* the `close` and `adj_close` columns, since both are quoted in the minor unit and `load_latest_prices` reads `adj_close`; `group_by_currency(tickers, currencies)`, which buckets tickers and treats an unrecorded one as `DEFAULT_CURRENCY`; and `partition_by_currency(candidates, pool_currency, currencies)`, which splits a batch into accepted and refused and, when the pool is empty, lets the first candidate establish the currency. Storage mirrors the existing upsert discipline exactly: `upsert_ticker_currency_table` creates the table if absent, deletes the rows for the requested tickers, then inserts; `load_ticker_currencies` returns a mapping and, crucially, an empty one when the table does not exist, which is the normal case for the shared cache. The single network function is `fetch_ticker_currencies`, the only place in the module that touches yfinance, which makes it the one seam tests need to monkeypatch. Finally, add the `ticker_currency` table to `src/flow/live.py`'s `_init_empty_price_and_returns_tables`, matching that function's stated purpose of pre-creating the shapes the upserts fill.

**M2** wires all of that into `src/dataset/ticker_ingestion.py`'s `validate_and_ingest_tickers`, the single point through which every user-supplied ticker passes. Its return widens from `(valid, invalid)` to `(valid, invalid, currencies)`, with currencies already normalized. The body order matters: fetch prices as today; look up currencies for *only* the tickers that resolved, so a typo costs no extra request; move any ticker whose lookup failed into `invalid`; apply the price multipliers *before* writing; then upsert prices, upsert currencies, and build returns. Two properties are worth stating in the code: the pence multiplication changes no return, weight, volatility or Sharpe ratio, because returns are ratios — it affects only share counts and display; and it cannot compound on repeated ingestion, because the price upsert deletes and re-inserts from a fresh fetch rather than modifying stored rows.

**M3** is the user-facing refusal. In `src/flow/interactive.py`, `validate_and_edit_candidates` returns a `CandidateEditResult` named tuple whose first three fields are the existing ones, in order, followed by `refused` and `pool_currency`. It must reorder the valid additions by the order the person typed them before partitioning, because `validate_and_ingest_tickers` returns its list sorted and `'7203.T'` sorts before `'AAPL'` — so typing `AAPL 7203.T` into an empty pool would otherwise establish yen rather than dollars. In `src/flow/cli.py`, `_run_user_provided_confirm_loop` returns the pool's currency alongside the pool, tracks it across edits, resets it when a removal empties the pool, and reports a refusal by name. It also handles a persisted pool that already mixes currencies — possible for a pool saved before this plan — by printing the groups and prompting for which to keep, rather than silently choosing. `_run_edit_loop` gains a `currency` parameter it threads into both the edit and the printing. And `compute_weights_and_allocation` gains the last-line-of-defense guard that raises `MixedCurrencyPoolError`.

**M4** finally changes `to_yfinance_symbol`, adding `KNOWN_EXCHANGE_SUFFIXES` and the rule: a ticker with no dot passes through; a ticker whose text after the last dot matches the allowlist case-insensitively passes through unchanged; anything else converts dots to dashes exactly as before. The same milestone adds the display half — `CURRENCY_SYMBOLS`, a `format_money` helper, a `Portfolio currency:` line as the first thing `print_weights_and_allocation` prints, the currency routed into that function from both the initial run and the edit loop, and a clarified `--value` help string.

**M5** verifies: the full suite, then real runs against live market data for a yen pool and a pence pool, a check that a dollar run is unchanged, a check that the shared cache is byte-identical after a backtest-date run, and a `README.md` note.


## Concrete Steps


Run everything from the repository root, `/app/agentic_portfolio`.

To run the tests:

    uv run pytest tests/test_*.py

To build a yen portfolio and see a dollar ticker refused:

    uv run portfolio --date today --objective GMV --value 15000000 \
        --selection user_provided --memory-path /tmp/jpy.json --risk-free-rate 0.005

Type `a`, then `7203.T 6758.T`, then `a` again and `AAPL`. Expect the two Tokyo tickers accepted, then `Refused: AAPL is priced in USD but this pool is JPY. A portfolio cannot mix currencies; run them separately.` with the pool still holding exactly the two Tokyo tickers. Type `d` to confirm and expect `Portfolio currency: JPY` above the weights and a `JPY`-labelled leftover cash line.

To prove the pence normalization:

    uv run portfolio --date today --objective GMV --value 100000 \
        --selection user_provided --memory-path /tmp/gbp.json

Type `a`, then `BARC.L`, then `d`. Expect `Portfolio currency: GBP` — the major unit, not `GBp`. The share count should reflect a price near one pound ninety rather than one hundred ninety; the direct check is that `adj_close` for `BARC.L` in the session database is near 1.9.

To confirm nothing changed for dollars, run the same command with `AAPL SPY` and expect `Portfolio currency: USD` and a `$`-prefixed leftover cash line.


## Validation and Acceptance


Acceptance is behavioral, in seven parts, each verified for real and recorded in Artifacts and Notes.

A Tokyo ticker (`7203.T`) and a London ticker (`BARC.L`) are accepted rather than reported as not found — the capability that did not exist before. `BRK.B` still resolves, which is the regression that would matter most, since it is the reason the dot-to-dash translation exists. A pool of Tokyo tickers reports `Portfolio currency: JPY` and interprets `--value` as yen. Adding `AAPL` to that pool prints the refusal naming both `AAPL` and both currencies, and leaves the pool exactly as it was, while a valid same-currency ticker typed on the same line as a refused one is still added. A London ticker's stored `adj_close` is one hundredth of what Yahoo Finance reports, so its share count reflects a price of about £1.9. A dollar portfolio behaves exactly as it did before this plan, including its `$`-prefixed output. And a backtest-window date still reports `Mode: backtest` and leaves `data/portfolio.duckdb` byte-identical, checked with `md5sum` before and after.

For the automated suite, every one of the 217 tests passing before this plan must still pass, and the new tests must cover: the suffix allowlist in both directions; the case-sensitivity of `normalize_currency`, including that `GBP` is *not* divided by 100; the multiplier applied to both price columns while a dollar ticker in the same batch is untouched; the currency lookup receiving only the tickers that resolved; a lookup failure landing in `invalid` rather than defaulting to dollars; the first-typed-ticker rule establishing the pool currency; a refusal not being persisted; and — the most important regression — `compute_weights_and_allocation` being entirely unaffected when no `ticker_currency` table exists, which is what proves the backtest path and the shared cache are untouched. Per `AGENTS.md`, no test calls Yahoo Finance or an LLM; `fetch_ticker_currencies` is the seam, monkeypatched at its point of use exactly as `fetch_and_reshape_for_tickers` already is.


## Idempotence and Recovery


Every step is safe to repeat. `upsert_ticker_currency_table` deletes the rows for the tickers it is given before inserting, so ingesting the same ticker twice leaves exactly one row. The pence multiplier cannot compound, because it is applied to a freshly fetched frame before the price upsert replaces the stored rows wholesale — it never multiplies a value already in the database. A `user_provided` session always gets a throwaway database that `src/flow/live.py`'s existing `finally` clause deletes on exit, so nothing this plan writes can damage the shared historical cache.

Two recoverable situations are worth naming. A persisted `memory/candidates.json` that mixes currencies is reported at session start with its tickers grouped by currency, and the person chooses which group to keep; deleting the file is always a complete reset, since a missing file reads back as an empty pool. And a ticker whose currency cannot be determined is reported as invalid rather than guessed at, so the recovery is simply to omit it or try again later.


## Artifacts and Notes


The yen portfolio, live, abridged of `yfinance`'s stderr noise. This is the acceptance transcript: both Tokyo tickers resolve (which they could not before), `AAPL` is refused by name, and the money is labelled in yen:

    Current candidate pool (0): (empty)
    Edit candidate pool? ... : a
    Ticker(s) to add (space-separated): 7203.T 6758.T
    Added: 6758.T, 7203.T.
    Candidate pool (2): 6758.T, 7203.T

    Edit candidate pool? ... : a
    Ticker(s) to add (space-separated): AAPL
    Refused: AAPL is priced in USD but this pool is JPY. A portfolio cannot mix currencies; run them separately.
    Candidate pool (2): 6758.T, 7203.T

    Edit candidate pool? ... : d
    Saved 2 ticker(s) to /tmp/jpy.json.
    Mode: live  Rebalance date: 2026-09-05  Objective: GMV  Selection: user_provided

    Candidates (2): 6758.T, 7203.T

    Portfolio currency: JPY - --value is interpreted as JPY

    Weights:
      6758.T: 0.5009
      7203.T: 0.4991

    Expected return / volatility (annualized):
      6758.T: return=0.1289  volatility=0.2678
      7203.T: return=0.1458  volatility=0.2683

    Portfolio expected return: 0.1373  Portfolio volatility: 0.1913  Portfolio Sharpe: 0.6916
    Risk-free rate used: 0.0050
    Target annual return: n/a (objective is GMV, not MV)

    Share allocation:
      6758.T: 1934
      7203.T: 2429
    Leftover cash: ¥2,661.00 JPY

The share counts are the check that the yen arithmetic is right rather than merely labelled: 2,429 shares of Toyota at roughly ¥3,081 is about ¥7.5 million, half of the ¥15 million allocated, which is what a near-50/50 weighting should buy. Note `--risk-free-rate 0.005` was passed, since the 2% default is a dollar rate; the reported `Risk-free rate used: 0.0050` confirms it reached the Sharpe calculation.

The pound portfolio, live. `Portfolio currency` reads `GBP`, the major unit, not the `GBp` Yahoo Finance reports:

    Ticker(s) to add (space-separated): BARC.L VOD.L
    Added: BARC.L, VOD.L.
    ...
    Portfolio currency: GBP - --value is interpreted as GBP

    Weights:
      VOD.L: 0.5418
      BARC.L: 0.4582

    Share allocation:
      BARC.L: 9286
      VOD.L: 44302
    Leftover cash: £1.53 GBP

And the direct proof of the normalization, ingesting `BARC.L` for real and reading back the stored row:

    raw yfinance adj_close (pence):   493.4000
    stored in prices  (pounds):       4.9340
    ratio stored/raw:                 0.010000   (expect 0.01)
    ticker_currency row:              [('GBP', 'GBp', 0.01)]
    returned currencies:              {'BARC.L': 'GBP'}

The two regression checks. A dollar pool is unchanged from before this plan, including its weights, and the shared cache survives a backtest-date run byte-identical:

    $ md5sum data/portfolio.duckdb
    d75c6f24dd3f9a74263e0d15bd6abd6c  data/portfolio.duckdb

    Portfolio currency: USD - --value is interpreted as USD
      SPY: 0.9802
      AAPL: 0.0198
    Leftover cash: $266.05 USD

    Mode: backtest  Rebalance date: 2024-03-01  Objective: GMV  Selection: user_provided
    Portfolio currency: USD - --value is interpreted as USD
    Leftover cash: $276.79 USD

    $ md5sum data/portfolio.duckdb
    d75c6f24dd3f9a74263e0d15bd6abd6c  data/portfolio.duckdb

Those `SPY: 0.9802 / AAPL: 0.0198` weights match the run recorded in `plans/10_performance_reporting_and_target_return.md` exactly, which is the evidence that nothing about the dollar path moved.

M6, both currencies in one file. Starting from this repository's real legacy `memory/candidates.json` (a flat twelve-ticker USD pool), choosing `[n]ew` and building a JPY pool:

    Saved candidate pools:
      USD (12): AMLP, BIL, BOXX, GOOGL, NVDA, PFF, PFFA, QQQI, T, TLT, TSM, VZ
    Resume the USD pool? [Enter] resume / [n]ew pool in another currency: n

    Current candidate pool (0): (empty)
    Ticker(s) to add (space-separated): 7203.T 6758.T
    Added: 6758.T, 7203.T.
    Saved 2 JPY ticker(s) to /tmp/candidates.json.
    Portfolio currency: JPY - --value is interpreted as JPY
    Leftover cash: ¥2,661.00 JPY

The file afterwards, showing the legacy pool migrated into the keyed shape with its original timestamp intact beside the newly written one:

    {
      "pools": {
        "USD": {
          "tickers": ["AMLP", "BIL", "BOXX", "GOOGL", "NVDA", "PFF",
                      "PFFA", "QQQI", "T", "TLT", "TSM", "VZ"],
          "updated_at": "2026-09-05T01:49:26.829379+00:00"
        },
        "JPY": {
          "tickers": ["6758.T", "7203.T"],
          "updated_at": "2026-09-05T05:31:33.115047+00:00"
        }
      }
    }

And the next run, with both pools offered and the USD one resumed unchanged:

    Saved candidate pools:
      JPY (2): 6758.T, 7203.T
      USD (12): AMLP, BIL, BOXX, GOOGL, NVDA, PFF, PFFA, QQQI, T, TLT, TSM, VZ
    Resume which pool? (JPY/USD) or [n]ew: USD

    Current candidate pool (12): AMLP, BIL, BOXX, GOOGL, NVDA, PFF, PFFA, QQQI, T, TLT, TSM, VZ
    Saved 12 USD ticker(s) to /tmp/candidates.json.
    Portfolio currency: USD - --value is interpreted as USD

Reading the file back after that USD save confirms the JPY pool survived it untouched, timestamp included:

    USD: 12 tickers, updated_at=2026-09-05T05:31:53.885356+00:00
    JPY: 2 tickers, updated_at=2026-09-05T05:31:33.115047+00:00

The suite:

    $ uv run pytest tests/test_*.py -q
    283 passed in 50.63s

And the intermediate state worth recording, from immediately after M2, before the callers were updated:

    $ uv run pytest tests/test_*.py -q
    247 passed in 39.68s

Zero failures, with production broken. See the first entry under Surprises & Discoveries.


## Interfaces and Dependencies


No new third-party dependencies. Everything uses `duckdb`, `pandas`, and `yfinance`, all already declared in `pyproject.toml`.

In the new `src/dataset/ticker_currency.py`, define:

    DEFAULT_CURRENCY = "USD"
    MINOR_UNIT_CURRENCIES: dict[str, tuple[str, float]]
    CURRENCY_LOOKUP_FAILED_REASON: str

    class MixedCurrencyPoolError(ValueError)

    def normalize_currency(raw_currency: str) -> tuple[str, float]
    def fetch_ticker_currencies(tickers: list[str], pause_seconds: float = ...) -> dict[str, str]
    def upsert_ticker_currency_table(currency_df: pd.DataFrame, tickers: list[str], db_path: str) -> None
    def load_ticker_currencies(tickers: list[str], db_path: str) -> dict[str, str]
    def apply_price_multipliers(long_prices: pd.DataFrame, multipliers: dict[str, float]) -> pd.DataFrame
    def group_by_currency(tickers: list[str], currencies: Mapping[str, str]) -> dict[str, list[str]]
    def partition_by_currency(
        candidates: list[str], pool_currency: str | None, currencies: Mapping[str, str]
    ) -> tuple[list[str], dict[str, str], str | None]

The `ticker_currency` table's schema, created by `upsert_ticker_currency_table` and by `src/flow/live.py`'s `_init_empty_price_and_returns_tables`:

    ticker VARCHAR            -- the original ticker string, never the yfinance symbol
    currency VARCHAR          -- normalized major unit: 'USD', 'JPY', 'GBP'
    quoted_currency VARCHAR   -- exactly what yfinance reported, e.g. 'GBp'
    price_multiplier DOUBLE   -- the factor already applied at ingestion: 1.0 or 0.01

In `src/dataset/ticker_ingestion.py`, `validate_and_ingest_tickers` becomes:

    def validate_and_ingest_tickers(
        tickers: list[str], as_of: date, db_path: str
    ) -> tuple[list[str], dict[str, str], dict[str, str]]

In `src/flow/interactive.py`, define and return:

    class CandidateEditResult(NamedTuple):
        pool: list[str]
        added: list[str]
        invalid: dict[str, str]
        refused: dict[str, str]
        pool_currency: str | None

    def validate_and_edit_candidates(
        pool: list[str], add: list[str], remove: list[str], as_of: date, db_path: str,
        pool_currency: str | None = None,
    ) -> CandidateEditResult

`compute_weights_and_allocation`, `run_pipeline_against`, and `run_pipeline` each gain a `currency: str = DEFAULT_CURRENCY` parameter, and `run_pipeline_against`'s result dictionary gains a `"currency"` key.

In `src/dataset/prices.py`, add `KNOWN_EXCHANGE_SUFFIXES: frozenset[str]` and rewrite `to_yfinance_symbol` around it, leaving every other function unchanged.

In `src/flow/candidate_memory.py`, the stored shape becomes one pool per currency, and the API becomes:

    def load_all_pools(path: str = DEFAULT_CANDIDATES_PATH) -> dict[str, list[str]]
    def load_candidate_pool(path: str = DEFAULT_CANDIDATES_PATH, currency: str = DEFAULT_CURRENCY) -> list[str]
    def save_candidate_pool(
        tickers: list[str], path: str = DEFAULT_CANDIDATES_PATH, currency: str = DEFAULT_CURRENCY
    ) -> None

with the file holding:

    {"pools": {"USD": {"tickers": [...], "updated_at": "..."},
               "JPY": {"tickers": [...], "updated_at": "..."}}}

A file in the old `{"tickers": [...], "updated_at": ...}` shape is read as the USD pool and rewritten in the new shape on the next save.

In `src/flow/cli.py`, add:

    CURRENCY_SYMBOLS: dict[str, str]
    def format_money(amount: float, currency: str) -> str
    def _choose_pool_to_resume(pools: dict[str, list[str]]) -> tuple[list[str], str | None]

and `_run_user_provided_confirm_loop`'s first parameter becomes `initial_pools: dict[str, list[str]]` (every saved pool, keyed by currency) rather than a single pre-loaded `list[str]`.

`print_weights_and_allocation` gains `currency: str = DEFAULT_CURRENCY`; `_run_edit_loop` gains the same; `_run_user_provided_confirm_loop` returns `tuple[list[str], str]`.

Explicitly out of scope, and to be left untouched: currency conversion of any kind; mixed-currency portfolios; and the currency defects in `src/dataset/fundamentals.py` (`mve` as the logarithm of a local-currency market capitalization, `bm` dividing US-dollar SEC book equity by a local-currency market capitalization) together with `src/dataset/sec_edgar.py`'s US-only ticker-to-CIK map and `units.USD` filter. Those are unreachable from this feature: `src/flow/live.py` calls `build_factors` and `build_momentum_factors` only in the `else` branch of its `user_provided` check, `src/flow/interactive.py`'s `run_scan` short-circuits before either agent, and the shared cache's `factors` table covers only S&P 500 members. Also note `src/agents/external_screen.py`'s `screen_external_candidate`, which currently has no production caller: it would standardize a yen market capitalization against the S&P 500 cross-section and return a confident verdict from a meaningless figure, and it would fail outright against a `user_provided` session database, which has no `factors` table. `plans/09_user_provided_selection.md` names wiring it up as the natural next feature, so anyone doing that must address both problems first.
