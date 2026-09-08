# Record why a ticker has no dividend coverage

This ExecPlan is a living document. The sections `Progress`, `Surprises & Discoveries`,
`Decision Log`, and `Outcomes & Retrospective` must be kept up to date as work proceeds.
It must be maintained in accordance with `PLANS.md` at the repository root.

## Purpose / Big Picture

After this change, a ticker with no trailing dividend yield can say **why**, and the report
stops recommending an action that cannot work.

`data/portfolio.duckdb` has prices for 525 tickers and dividend coverage for 521. The four
uncovered ones - AVB, EA, EQR, LEG - are not typos and not unresolvable symbols: yfinance
resolves all four and simply no longer serves their 2015-2024 window. Before this change,
every message about them said either "no dividend data has been fetched for it" or "build
its dividend history", and following the second costs a six-batch, several-minute fetch that
ends exactly where it started. After it, `uv run portfolio-build-dividends AVB EA EQR LEG`
records one row per uncovered ticker naming the window that was requested and the range the
source actually serves, and every report that prints `yield n/a` finishes the sentence with
that reason.

What does NOT change: the yield contract. A missing yield is still absent from `yields`,
never a zero, and the four remain unavailable to the optimizer and to reported income. This
plan makes a gap legible; it does not fill one.

## Progress

- [x] (2026-09-08) Confirmed the pending follow-up in
      `plans/15_minimum_expected_dividend.md` and found its premise false on both counts.
      Recorded in that plan's `Surprises & Discoveries` and below.
- [x] (2026-09-08) Settled with the user: record the gap, do not derive dividends from
      prices. See `Decision Log`.
- [x] (2026-09-08) `dividend_unresolved` table, `probe_served_window`, `unresolved_frame`,
      `unresolved_frame_from_caller` and `dividend_unresolved_reasons` in
      `src/dataset/dividends.py`; both writers extended; reasons threaded through
      `trailing_dividend_yields` and `load_dividend_figures`.
- [x] (2026-09-08) `unavailable_yield_refusal` in `src/optimizer/dividends.py`, the
      `dividend_unavailable` field on `PortfolioStats`, and the three report sites in
      `src/flow/cli.py`.
- [x] (2026-09-08) `uv run portfolio-build-dividends TICKER...` for a targeted rebuild.
- [x] (2026-09-08) Tests: 17 in `tests/test_dividends.py`, 2 in `tests/test_cli.py`, 1 in
      `tests/test_holdings_cli.py`. All hermetic - `probe` is injected everywhere, and no
      test reaches `probe_served_window` itself.
- [x] (2026-09-08) Recorded the four in `data/portfolio.duckdb` with
      `uv run portfolio-build-dividends AVB EA EQR LEG`. After it: 521 coverage rows, 4
      unresolved rows (AVB, EA, EQR, LEG), 14,589 dividend rows across 429 payers unchanged,
      and zero tickers in `prices` that appear in neither table.

## Surprises & Discoveries

- Observation: the premise of plan 15's pending item was false in both halves. The four
  uncovered tickers are AVB, EA, EQR and LEG; none appears in `unresolved_tickers`, and each
  has a complete 2,346-row price history from 2015-01-02 to 2024-04-29.
  Evidence: `prices` has 525 distinct tickers, `dividend_coverage` 521 rows, and the set
  difference is exactly those four. `unresolved_tickers` holds 55 rows, all carrying the
  generic no-close-data reason, and none of the four.

- Observation: the symbols resolve; the *window* is gone.
  Evidence: `_fetch_batch(['AVB','EA','EQR','LEG'], '2015-01-01', '2024-04-30')` returns a
  0-row frame with `Close` columns present, no `Dividends` column, and Yahoo's
  `Data doesn't exist for startDate = 1420088400, endDate = 1714449600` per symbol. Probed
  with `period="max"` the same symbols return a short, already-stale recent history - AVB 27
  rows, EQR 15, EA 6, LEG 5, all beginning 2026-07-17 - while MSFT returns 4,194 rows to
  2026-09-04. Mixed into a batch with a healthy symbol, only the healthy one appears under
  `raw["Dividends"]`.

- Observation: not a batch failure. The four sit mid-batch in three different batches -
  sorted indices 45, 152, 168 and 281 of 525 at `price_batch_size` 100 - so no single failed
  request explains them.

- Observation: LEG is the worst possible ticker to lose silently. Its trailing yield over the
  window is 0.1004, above MO's 0.0885, making it the highest-yielding name in the universe;
  a floored pool containing it was refused outright, and the refusal advised a rebuild that
  cannot succeed.

- Observation (recorded so it is not re-litigated from scratch): the four names' exact
  dividends ARE recoverable from data already on disk, and the recovery is not approximate.
  With `f = adj_close / close`, `D_t = close_{t-1} * (1 - f_{t-1}/f_t)` at each ex-date.
  Evidence: run against the stored `prices` table it recovers 37/37 events for KO, 38/38 for
  T, 37/37 for XOM and 37/37 for AAPL, maximum absolute error 5e-5 and median relative error
  1e-5, AAPL's 2020 4:1 split included - and for the four it reproduces their real quarterly
  payments (AVB 1.65 rising to 1.70, EQR 0.663 rising to 0.675, LEG 0.46, EA 0.19). The
  module docstring previously claimed the adjustment factor encodes dividends "only as a
  back-adjustment factor, never as the '$0.485 per share on 2024-03-14' a cash figure has to
  be built from". That was too strong, and it has been corrected to state the inversion, the
  evidence, and the reason this module still refuses to use it: one provenance, so a stored
  payment is always a reported payment.

- Observation: a second, DIFFERENT gap exists and is deliberately not fixed here.
  `data/holdings.duckdb` has 925 price rows for BOXX and no coverage row, but BOXX refetches
  fine today and reports one dividend event on 2024-08-13 - a transient miss that
  `uv run portfolio-holdings --refresh-holdings` clears. That is exactly the case the new
  reason wording calls "transient", and it is why the reason has two verdicts rather than
  one. (Its fetch also returns a `Capital Gains` field this project ignores. A fund's capital
  gains distribution is cash to a holder, so that omission understates a fund holder's
  income. Out of scope, noted for someone.)

## Decision Log

- Decision: record the gap; do not derive dividends from prices.
  Rationale: the user's call, made with the recovery evidence above in hand. The yield
  contract stays fetch-only - one provenance, no second-class amounts, and no risk that a
  derived figure is read as a reported one.
  Date/Author: 2026-09-08, user.

- Decision: distinguish a transient miss from a window the source no longer serves, using one
  `period="max"` probe per missing ticker.
  Rationale: the two have opposite fixes, and the whole failure being corrected is advice
  that points at the wrong one. `yf.download` cannot tell them apart - the limitation
  `src/dataset/prices.py` documents in its own generic reason - but the comparison is cheap
  when only already-failed tickers are probed: four calls on the run that found this, not
  525.
  Date/Author: 2026-09-08, Claude.

- Decision: a new `dividend_unresolved` table rather than a `reason` column on
  `dividend_coverage`.
  Rationale: `dividend_coverage` means "this ticker was fetched, so a zero is a real zero".
  A row with a reason and no window would corrupt that meaning, and every reader of that
  table would have to learn the exception. Two tables, one requested list split between
  them, and a ticker in neither is a bug either can be checked for.
  Date/Author: 2026-09-08, Claude.

- Decision: thread the reasons through `PortfolioStats.dividend_unavailable` rather than
  re-wording the optimizer's refusal in `src/flow/interactive.py`.
  Rationale: the first design enriched `DividendYieldUnavailableError` at the layer holding
  `db_path`, mirroring `_explain_dropped_dividend_payers`. Once the per-ticker report line
  needed the same reasons, the value had to reach `PortfolioStats` anyway - and with it
  there, one keyword argument replaces an exception-rewriting path. `unavailable_yield_refusal`
  keeps the refusal wording in one place regardless of who raises it.
  Date/Author: 2026-09-08, Claude.

- Decision: "build its dividend history" is dropped from a refusal only when EVERY missing
  ticker has a recorded reason.
  Rationale: with one unexplained ticker in the pool the advice is still actionable, and a
  message that withholds a working fix is worse than one that offers it alongside the
  reasons.
  Date/Author: 2026-09-08, Claude.

## Outcomes & Retrospective

The plan's purpose is met. An uncovered ticker's reason is recorded, carried and printed;
the futile advice is gone from the refusal it appeared in; and `data/portfolio.duckdb` now
answers for all 525 of its priced tickers, 521 with coverage and 4 with a reason. The gap
that started this - a ticker in `prices` that appears in neither table - is now checkable in
one query and currently zero.

The lesson worth keeping is about the item that started this. Plan 15's follow-up recorded a
*hypothesis* ("cannot resolve at all", "already appear in `unresolved_tickers`") in the voice
of a finding, and it sat there for a day reading as settled. Both halves were checkable in
one query and one fetch, and both were wrong. A follow-up item that guesses at a cause should
say it is guessing.

## Context and Orientation

`src/dataset/dividends.py` owns per-ex-date dividends and splits. Its three-layer discipline:
`_fetch_batch` performs the yfinance download, `reshape_dividends_long` and
`trailing_dividend_yields` are pure, and `write_dividends_tables`/`upsert_dividends_tables`
are the only writers. It maintains three tables in a DuckDB file - `dividends`, `splits`,
`dividend_coverage` - and this plan adds a fourth, `dividend_unresolved`.

`dividend_coverage` exists to separate a confirmed non-payer from a ticker nobody fetched: a
non-payer contributes no `dividends` rows, and so does an unfetched ticker, so without the
coverage table every uncovered ticker would report a confident `0.0` yield.
`tickers_with_dividend_data` reads it, and `trailing_dividend_yields` uses the answer to put
a ticker either in `yields` (a number that can be relied on, `0.0` included) or in
`unavailable` (a sentence saying why there is none). `src/optimizer/dividends.py`'s
`dividend_yield_vector` refuses to build a floor constraint over a pool containing an
unavailable ticker, because substituting zero would understate income and misname the
reported ceiling.

The gap this plan closes: `unavailable`'s sentence could only say "no dividend data has been
fetched for it", which is true of every uncovered ticker regardless of cause and actionable
for none.

## Plan of Work

In `src/dataset/dividends.py`:

- `DIVIDEND_UNRESOLVED_COLUMNS = ["ticker", "reason"]`, the same two columns
  `src/dataset/prices.py` writes to `unresolved_tickers`.
- `probe_served_window(symbol)` beside `_fetch_batch`: one `yf.download(period="max")`,
  returning `(first, last)` or `None`, swallowing every failure. The module docstring's
  "`_fetch_batch` is the ONLY function here that performs network I/O" is revised rather
  than left false.
- `unresolved_frame(missing, symbol_map, start, end, probe=probe_served_window)`: one row per
  missing ticker with one of three reasons - the served range cannot cover the request
  ("cannot fix this"), it overlaps ("transient... may fix it"), or the probe found nothing
  ("not known whether"). Every reason quotes the real dates. `probe` is injected so tests
  stay hermetic.
- `unresolved_frame_from_caller(tickers)` for the tickers `build_dividends_for_tickers`'
  caller already knew were unusable: no probe, but still a row, since a ticker in neither
  table is the silent gap.
- `dividend_unresolved_reasons(tickers, db_path)`: reader, `{}` on a missing file or table.
- `write_dividends_tables` and `upsert_dividends_tables` take `unresolved_df`. The upsert
  deletes by the passed `tickers`, never by frame content, so a reason clears itself when the
  ticker starts resolving.
- `trailing_dividend_yields` takes `reasons` and prefers a recorded one;
  `load_dividend_figures` reads them once and passes them in.
- `main()` accepts tickers from `sys.argv` for a targeted rebuild that prints each reason.

In `src/optimizer/dividends.py`: `unavailable_yield_refusal(missing, reasons)` builds the
refusal, `dividend_yield_vector(yields, tickers, reasons)` raises it.

In `src/optimizer/portfolio.py`: `dividend_unavailable` on `PortfolioStats`, threaded through
`compute_weights_and_stats`, `_dividend_stats_fields` (narrowed to the actually-missing
tickers) and `_fit_efficient_frontier` into `dividend_yield_vector`.

In `src/flow/interactive.py`: keep `load_dividend_figures`' `unavailable` and pass it as
`dividend_unavailable`.

In `src/flow/cli.py`: the per-ticker pool line prints the reason;
`format_dividend_coverage` and `format_holdings_dividend_total` append one indented line per
ticker below the figure, never inside a parenthetical.

## Concrete Steps

Run from the repository root.

    uv run pytest tests/test_dividends.py tests/test_optimizer.py tests/test_cli.py tests/test_holdings_cli.py -q
    # expect: 4 files, all passed - 88 in test_dividends.py alone

    uv run pytest tests/test_*.py -q
    # expect: 731 passed (709 before this work)

The data step, a real network fetch, already run against `data/portfolio.duckdb`:

    uv run portfolio-build-dividends AVB EA EQR LEG
    # Wrote 0 dividend row(s) for 0 paying ticker(s) of 4 requested ticker(s) to data/portfolio.duckdb.
    #   AVB: no dividend coverage - yfinance no longer serves this ticker's history for the
    #        requested 2015-01-01..2024-04-30 window: the only range it serves for AVB is
    #        2026-07-17..2026-08-24, which does not overlap the request. Re-running the
    #        dividend build cannot fix this - the data is gone from the source, not missing
    #        from this database.
    #   (EA, EQR, LEG likewise)

## Validation and Acceptance

Behavior, with inputs and outputs:

- A historical screened run whose pool contains LEG, with `--min-dividend-yield`, is refused
  with a message that names the served range and does NOT say "build its dividend history".
  Before the change the same run said exactly that.
- The same pool with no floor prints `LEG: yield n/a - yfinance no longer serves...` and a
  `covers 0.xxxx of the weight` caveat with LEG's reason on its own indented line, while the
  portfolio figures still describe the rest.
- A holdings report containing a ticker with no coverage prints its reason under the
  `Trailing annual dividends:` line.
- A database with no `dividend_unresolved` table prints the old generic sentence and does not
  raise.

Tests: `uv run pytest tests/test_dividends.py` expects 88 passed, of which the 17 named in
`Progress` fail before the change (`ImportError` for `unresolved_frame`) and pass after.

## Idempotence and Recovery

Every step is repeatable. `uv run portfolio-build-dividends AVB EA EQR LEG` upserts: it
deletes those four tickers' rows in all four tables and re-inserts, so running it twice
leaves the same state as running it once, and running it after Yahoo restores the history
replaces the unresolved rows with real coverage without any manual cleanup. A ticker's
reason is never left behind by a later success, which is the property
`test_upsert_clears_an_unresolved_row_when_the_ticker_starts_resolving` pins.

There is no rollback to write: nothing here drops or rewrites an existing table's rows for a
ticker that was not asked about.

## Artifacts and Notes

The reproduction, verbatim:

    >>> raw = _fetch_batch(['AVB','EA','EQR','LEG'], '2015-01-01', '2024-04-30')
    $LEG: possibly delisted; no price data found  (1d 2015-01-01 -> 2024-04-30)
      (Yahoo error = "Data doesn't exist for startDate = 1420088400, endDate = 1714449600")
    ... same for EQR, AVB, EA
    shape (0, 24)
    top-level ['Adj Close', 'Close', 'High', 'Low', 'Open', 'Volume']
    fetched set()

What the source does serve:

    AVB rows 27 range 2026-07-17 2026-08-24 last close 68.14
    EA  rows  6 range 2026-07-17 2026-08-10 last close 209.70
    EQR rows 15 range 2026-07-17 2026-08-21 last close 63.66
    LEG rows  5 range 2026-07-17 2026-08-27 last close 9.20
    MSFT rows 4194 range 2010-01-04 2026-09-04 last close 499.70

The recovery that was NOT adopted, kept as evidence for the decision:

    KO   stored 37 recovered 37 max abs err 2e-05 median rel err 1e-05
    T    stored 38 recovered 38 max abs err 1e-05 median rel err 0.0
    XOM  stored 37 recovered 37 max abs err 3e-05 median rel err 1e-05
    AAPL stored 37 recovered 37 max abs err 5e-05 median rel err 4e-05

    LEG: trailing 12m to 2024-04-29 = 1.8400 over close 18.33 -> yield 0.1004
         [(2023-06-14, 0.46), (2023-09-14, 0.46), (2023-12-14, 0.46), (2024-03-14, 0.46)]

## Interfaces and Dependencies

No new dependency. The signatures that must exist at the end of this milestone:

    src/dataset/dividends.probe_served_window(symbol: str) -> tuple[date, date] | None
    src/dataset/dividends.unresolved_frame(
        missing: list[str], symbol_map: dict[str, str], start: str | date, end: str | date,
        probe: Callable[[str], tuple[date, date] | None] = probe_served_window,
    ) -> pd.DataFrame
    src/dataset/dividends.unresolved_frame_from_caller(tickers: list[str]) -> pd.DataFrame
    src/dataset/dividends.dividend_unresolved_reasons(
        tickers: list[str], db_path: str = settings.db_path
    ) -> dict[str, str]
    src/dataset/dividends.trailing_dividend_yields(
        dividends_per_share: dict[str, float], prices: pd.Series,
        known_tickers: set[str] | None = None, reasons: dict[str, str] | None = None,
    ) -> tuple[dict[str, float], dict[str, str]]
    src/optimizer/dividends.unavailable_yield_refusal(
        missing: Sequence[str], reasons: dict[str, str] | None = None
    ) -> DividendYieldUnavailableError
    src/optimizer/dividends.dividend_yield_vector(
        yields: dict[str, float], tickers: Sequence[str], reasons: dict[str, str] | None = None
    ) -> np.ndarray
    src/optimizer/portfolio.PortfolioStats.dividend_unavailable: dict[str, str] | None

The `dividend_unresolved` table is `(ticker VARCHAR, reason VARCHAR)`, created by both
writers whether or not there is anything to put in it.
