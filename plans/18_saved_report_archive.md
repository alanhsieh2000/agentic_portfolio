# Save every printed portfolio report under output/<month>/, deduplicated by digest

This ExecPlan is a living document. The sections `Progress`, `Surprises & Discoveries`,
`Decision Log`, and `Outcomes & Retrospective` must be kept up to date as work proceeds.

This document must be maintained in accordance with `PLANS.md`, at the repository root of
`/app/agentic_portfolio`.

It builds on work described by `plans/09_user_provided_selection.md`,
`plans/10_performance_reporting_and_target_return.md`,
`plans/11_non_us_tickers_and_single_currency.md`, `plans/12_benchmark_per_candidate_pool.md`,
`plans/13_user_portfolio.md`, `plans/14_per_currency_risk_free_rate.md`,
`plans/15_minimum_expected_dividend.md`, `plans/16_dividend_coverage_reasons.md`, and
`plans/17_ticker_performance_summary.md`. All of those files are checked into this repository and
are incorporated here by reference; everything this plan actually relies on is nevertheless
restated below, so a reader who opens only this file can finish the work.


## Purpose / Big Picture

Two commands in this project print a portfolio report. `uv run portfolio ...` screens or accepts a
candidate pool, optimizes it, and prints the weights, the figures behind them, and the share
allocation. `uv run portfolio-holdings whatif` takes the portfolio you actually hold and lets you
try hypothetical changes to it, reprinting the same kind of figures after each one.

Deciding which portfolio to hold means running these many times: three objectives, several
candidate pools, a dividend floor tried at two levels, a returns window at 36 months and at 60.
Each run prints thirty to sixty lines. After four or five runs the earlier reports have scrolled
out of the terminal, and the only way to compare the fourth against the first is to run the first
again - which, in live mode, means paying for the whole Yahoo Finance snapshot a second time.

Today nothing is persisted. There is no `output/` directory and no code anywhere in `src/` writes
one. After this change, every report either command prints is also written to a file under
`output/<year>-<month>/`, named after the run and carrying a digest of its own text, so that:

- an analysis can be spread over several sittings, because last week's reports are still there;
- running the identical analysis twice does not leave two identical files, because the second one
  is recognized by its digest and skipped;
- each file is self-describing - it carries the objective, the currency, the candidate pool, the
  returns window and the headline figures as structured fields above the report text - so a later
  command can render several of them side by side without parsing English prose.

That later command - the side-by-side summary - is deliberately NOT part of this plan. This plan
delivers the saving layer it will read. That split is the user's own instruction: "the first step
is to save reports for the later summarization."

Here is the whole feature in one transcript. Run from the repository root, `/app/agentic_portfolio`:

    $ printf 'd\nf\n' | uv run portfolio --date today --objective GMV --value 100000 \
        --selection user_provided --output-dir /tmp/report-check \
        --memory-path /tmp/cand.json --rates-path /tmp/rates.json
    ... the report prints exactly as it always has ...
    Share allocation:
      AVB    112 shares  ($24,998.08)
      ...
    Leftover cash: $412.09 USD

    Saved report: /tmp/report-check/2026-09/2026-09-10-portfolio-GMV-user_provided-USD-a1b2c3d4.md

    $ printf 'd\nf\n' | uv run portfolio --date today --objective GMV --value 100000 \
        --selection user_provided --output-dir /tmp/report-check \
        --memory-path /tmp/cand.json --rates-path /tmp/rates.json
    ... the same report ...
    Report already saved this month: /tmp/report-check/2026-09/2026-09-10-portfolio-GMV-user_provided-USD-a1b2c3d4.md

    $ ls /tmp/report-check/2026-09/
    2026-09-10-portfolio-GMV-user_provided-USD-a1b2c3d4.md

Four things in that transcript are the whole point.

First, the report itself is byte-for-byte what it was before this change. Nothing was reformatted,
reordered or moved to make saving possible. The archive is a second reader of the same bytes.

Second, one new line appears at the end, and it names the file. A command that writes a file must
say which file, or the write is a side effect the user has to discover.

Third, the second run wrote nothing and said so. The digest in the filename is the whole mechanism:
asking "have I already saved this exact report this month?" is asking whether that path exists.

Fourth, the month folder came from the run's as-of date, not from the clock. A run at
`--date 2024-03-29` files under `output/2024-03/` even if it is executed today, because that is the
month the report is *about*.


## Progress

- [x] (2026-09-10 06:20Z) Researched the two report paths and confirmed the starting facts: no
      `output/` writer and no hashing anywhere in `src/`; report text produced only by `print()`;
      test baseline 880 collected.
- [x] (2026-09-10 06:25Z) Prototyped the tee'd `sys.stdout` capture and confirmed the captured text
      is byte-identical to what reaches the terminal, including under a replaced `sys.stdout` (what
      pytest's `capsys` installs). Evidence in `Surprises & Discoveries`.
- [x] (2026-09-10 06:40Z) Wrote this ExecPlan.
- [x] (2026-09-10 13:35Z) Milestone 1: `src/flow/report_archive.py` plus
      `tests/test_report_archive.py` (29 tests) and `settings.output_dir`. `uv run pytest
      tests/test_report_archive.py -q` reports 29 passed.
- [x] (2026-09-10 13:45Z) Milestone 2: the two flags, one `ReportArchive` per run threaded into
      `print_pipeline_result` and `_run_edit_loop`, the `report_facts`/`holdings_report_facts`
      builders, and `tests/conftest.py`. `tests/test_cli.py` went from 189 to 201 passed.
- [x] (2026-09-10 13:52Z) Milestone 3: the same flags on `portfolio-holdings`, the baseline and
      each variant recorded in `_run_whatif`, docstring amended. `tests/test_holdings_cli.py` went
      from 79 to 85 passed.
- [x] (2026-09-10 13:56Z) Milestone 4: `README.md` describes the archive, and its line 55's
      unimplemented `output/{objective}-{set}-portfolio-{date}.md` convention now points at the
      one actually built.
- [x] (2026-09-10 14:00Z) End-to-end against live Yahoo Finance data: a pool report saved and
      recognized on repeat, an edit-loop variant saved and the return to GMV recognized, a whatif
      baseline and variant saved with `[u]ndo all` recognized, `memory/` byte-identical either
      side, `--no-save-reports` writing nothing, and `git status` clean. Transcripts in
      `Artifacts and Notes`.
- [x] (2026-09-10 14:05Z) Full suite green: `uv run pytest tests/test_*.py` reports
      `927 passed, 3 warnings in 455.73s` - the 880 baseline plus the 47 tests added (29 in
      `tests/test_report_archive.py`, 12 in `tests/test_cli.py`, 6 in
      `tests/test_holdings_cli.py`). The runtime fell rather than rose, so nothing in the new code
      reaches the network.


## Surprises & Discoveries

- Observation: The `output/*.md` convention this plan implements was already SPECIFIED in
  `README.md` and never built. Its line 55 reads "The system will report summaries and store them
  as output/GMV-, MSR-, MV-{set}-portfolio-{date}.md, where {set} is 'S', 'SF', 'U', 'SU', or
  'SFU'." That naming predates per-currency pools (`plans/11`), per-pool benchmarks (`plans/12`)
  and the `user_provided` selection (`plans/09`), and it has no month folder and no deduplication.
  `plans/08_consistency_review.md` records it as an open gap at its lines 107-137. It must therefore
  be *reconciled* - the README updated to the convention actually built - rather than left to
  contradict the code.
  Evidence:

      $ grep -rn "output/" src/ --include=*.py | wc -l
      0
      $ grep -rn "hashlib\|sha256\|md5" src/ | wc -l
      0
      $ grep -n "output/" README.md | head -1
      55:... store them as `output/GMV-, MSR-, MV-{set}-portfolio-{date}.md` ...

- Observation: A tee'd `sys.stdout` captures report text byte-identically and composes correctly
  with pytest's `capsys`, which replaces `sys.stdout` with an object of its own. This is what makes
  the whole feature a small change instead of a rewrite of roughly twenty printing helpers.
  Evidence, run with `uv run python -c ...`:

      through-to-fake: 'Mode: live  Objective: GMV\nWeights:\n'
      captured:        'Mode: live  Objective: GMV\nWeights:\n'
      equal: True
      isatty delegated: True

- Observation: `print_weights_and_allocation` is the ONLY section both report paths of
  `uv run portfolio` share. `print_pipeline_result` reaches it (at `src/flow/cli.py:1028`) and the
  interactive edit loop calls it directly (at `src/flow/cli.py:2175`), while the header lines above
  it - mode, rebalance date, objective, selection, scanner branch, candidate list - are printed
  only by `print_pipeline_result`. Anchoring the archived body on that shared section is what makes
  deduplication work across an initial run and an edit that lands back on the same portfolio;
  anchoring it on the whole of `print_pipeline_result` would not.

- Observation: Roughly fifty tests in `tests/test_cli.py` invoke `main()` by monkeypatching
  `sys.argv`. Once `main()` builds a real archive, those tests would write into the repository's
  own `output/`. The suite has no `conftest.py` today and uses no fixtures at all, so this hazard
  has no existing guard to extend.

- Observation: In live mode, two runs of the SAME command minutes apart legitimately produce
  different reports, because prices move while the market is open. This is not a deduplication
  failure - deduplication is over the report text by design - but it is worth knowing before
  reading a month folder: a live-mode repeat is a new file whenever a price ticked.
  Evidence, two `--objective GMV` runs about ninety seconds apart, diffed body against body:

      14,15c14,15
      <   SPY: yield=0.0099  $725.21 USD
      <   T: yield=0.0432  $1,167.35 USD
      ---
      >   SPY: yield=0.0099  $725.22 USD
      >   T: yield=0.0432  $1,165.76 USD
      25,27c25,27
      <   T: 1049
      < Leftover cash: $303.04 USD
      ---
      >   T: 1048
      > Leftover cash: $293.04 USD

  T's price rose enough for one fewer share to fit inside the same 100,000, which moved the
  leftover cash and the dividend total with it. Within ONE session the identical-report case is
  exactly reproduced, which is the case that matters: driving the edit loop GMV to MSR and back
  reported the first file as already saved rather than writing a third.

- Observation: `format_share_count` renders share counts with thousands separators for reading,
  so a `positions` front-matter field built from it read `SPY:1,000` - a comma inside a field whose
  own separator is a comma, unparseable by the command meant to read it back. `src/flow/cli.py`
  already had the right helper for this, `_as_argument`, which exists precisely because a
  suggested command line must not contain separators either.
  Evidence:

      -   SPY:1000
      +   SPY:1,000

- Observation: Normalizing a report body with `text.strip()` before hashing also strips leading
  INDENTATION from the first line, which is a change to the report's shape rather than to its
  incidental spacing. Report bodies happen to begin unindented today, so nothing was actually
  damaged, but the fix - drop leading and trailing blank LINES, and `rstrip` each line - preserves
  what was printed instead of relying on that happening to be true.
  Evidence: `normalize_report("\n\n  a  \n b \n\n")` now returns `"  a\n b"`, keeping the two
  leading spaces it previously discarded.

- Observation: Dots had to be excluded from the filename-safe character set, not merely path
  separators. With dots allowed, a currency of `../../etc` slugged to `..-..-etc`, which keeps the
  traversal fragments intact. Nothing legitimate that reaches a filename contains a dot - the
  `.md` suffix is appended after the tokens are joined, and tickers, which do contain dots
  (`1321.T`), never appear in a name - so excluding them costs nothing.
  Evidence: the guard test asserts neither `/` nor `..` survives, and failed on the `..` half
  before dots were dropped from the set.


## Decision Log

- Decision: Capture the report by teeing `sys.stdout` around the existing `print()` calls, rather
  than refactoring the `print_*` helpers to return strings.
  Rationale: `src/flow/cli.py` emits report text through about 109 `print()` calls and
  `tests/test_cli.py` is 132 KB of assertions against that text. This codebase repeatedly promises
  byte-identical output - `src/flow/holdings_cli.py:568` says a default run's report is
  "byte-identical to what it printed before this existed". A tee makes that promise trivially true
  because the terminal receives the very bytes it received before; a rewrite would make it a thing
  to verify at twenty sites.
  Date/Author: 2026-09-10, Claude (Opus 5), at the user's request.

- Decision: The archived body is `print_weights_and_allocation`'s output plus the concentration
  note - not the whole of `print_pipeline_result`, and not including the holdings block.
  Rationale: It is the one section both the initial report and every edit-loop recompute print, so
  a digest over it deduplicates across them. The header facts are not lost: they are written as
  structured front-matter fields, which is a better home for them anyway, because the follow-up
  summary command needs them as values rather than as a sentence. The holdings block is excluded
  because it is identical across every variant in a session and would bloat every file with the
  same text; the stale-share-count warning is excluded because it is a fact about the store rather
  than about the portfolio, and including it would give two identical portfolios two digests.
  Date/Author: 2026-09-10, Claude (Opus 5).

- Decision: Deduplicate on the report TEXT, not on the run's inputs.
  Rationale: The user's choice. It catches genuinely identical output however it was reached - an
  objective named on the command line and the same objective arrived at through the edit loop
  produce one file, not two. The cost, accepted knowingly: prices move, so the same question asked
  on two different days is two different answers and two files. That is correct for an archive of
  reports, which is what this is.
  Date/Author: 2026-09-10, Claude (Opus 5), at the user's request.

- Decision: Put the digest in the filename and keep no index file.
  Rationale: Deduplication becomes `path.exists()`, which is inherently scoped to the month folder -
  exactly the scope the user asked for - and cannot fall out of sync with the files it describes,
  because it IS the files. An index would be a second thing to write atomically, to repair when
  corrupt, and to keep consistent with the directory.
  Date/Author: 2026-09-10, Claude (Opus 5).

- Decision: Front matter inside each `.md` file, not a sidecar metadata file, and it carries the
  headline figures as well as the run's inputs.
  Rationale: One file per report means one atomic write and no orphan metadata. Carrying
  `annual_return`, `annual_volatility`, `sharpe`, `dividend_yield` and the window as fields is what
  turns the follow-up side-by-side summary into a table build rather than a prose parser, and it
  costs nine lines per file.
  Date/Author: 2026-09-10, Claude (Opus 5).

- Decision: The month folder comes from the run's as-of date (`--date`), not the wall clock.
  Rationale: The user's choice, and the right one: a report is about a month of market data, and a
  historical-date run belongs with the month it measured, not the month somebody happened to
  execute it in.
  Date/Author: 2026-09-10, Claude (Opus 5), at the user's request.

- Decision: Archive every reported variant - the initial report and each edit-loop recompute, the
  whatif baseline and each hypothetical - rather than only the final one.
  Rationale: The user's choice. The variants ARE the analysis; keeping only the last one would
  discard the comparison the archive exists to enable. Digest deduplication is what keeps the
  resulting file count honest: a variant that returns to an earlier portfolio adds no file.
  Date/Author: 2026-09-10, Claude (Opus 5), at the user's request.

- Decision: `whatif` archives reports by default, and its "nothing was saved" sentence is left
  exactly as it is.
  Rationale: That command's promise, stated in its docstring at `src/flow/holdings_cli.py:517` and
  printed at its end, is about state that changes a later run - `memory/portfolio.json` and
  `memory/rates.json`. That promise is kept absolutely: `_remember_rate` is still not called and no
  position is still ever written. A report file changes no later run's behaviour; it is an
  observation, not state. The sentence "Nothing was saved: that was a what-if, and your portfolio
  is unchanged." remains literally true of the portfolio, so it is not reworded - but each variant
  prints its own line naming the file it wrote, so nothing is hidden, and the docstring is amended
  to say precisely what is and is not written. `--no-save-reports` restores the literal no-write
  behaviour.
  Date/Author: 2026-09-10, Claude (Opus 5).

- Decision: An unsatisfiable run archives nothing.
  Rationale: `print_pipeline_result` returns early at `src/flow/cli.py:1026` on an impossible
  target return, dividend floor or risk-free rate, printing the reason where the weights would have
  been. There is no portfolio, so there is nothing to compare against another portfolio, and an
  archive of non-answers would make the month folder harder to read for no gain. The existing
  `produced_report` flag at `src/flow/cli.py:2517` already expresses this state.
  Date/Author: 2026-09-10, Claude (Opus 5).

- Decision: Add `tests/conftest.py` with a single autouse fixture redirecting `settings.output_dir`
  at `tmp_path`, even though this repository has no `conftest.py` and uses no fixtures anywhere.
  Rationale: About fifty tests in `tests/test_cli.py` call `main()` through a monkeypatched
  `sys.argv`, and every other path in those tests is redirected by a per-test flag. Adding
  `--output-dir` to fifty argv builders is churn whose only effect is that the fifty-first, written
  later, gets forgotten and silently writes into the repository. A guard that cannot be forgotten is
  worth the style deviation here, because the failure mode is invisible: `output/` is gitignored, so
  a polluting test leaves `git status` clean.
  Date/Author: 2026-09-10, Claude (Opus 5).


## Outcomes & Retrospective

All four milestones are complete and the behaviour is verified against live Yahoo Finance data,
not only against stubs. What exists now that did not before: every report either entry point
prints is also a file under `output/<YYYY-MM>/`, self-describing through a front-matter block of
`key: value` facts, and free of redundant copies because the digest of the report text is in the
filename. An analysis can be put down and picked up next week, and the fourth variant can be read
beside the first without re-running either.

The design decision that earned its keep was anchoring the archived body on
`print_weights_and_allocation`'s block rather than on `print_pipeline_result`'s whole output.
Driving the edit loop from GMV to MSR and back reported the GMV file as already saved instead of
writing a third one - which is the behaviour the user asked for, and it would not have worked if
the initial report had carried the header lines into its digest while the edit-loop variant could
not.

The tee'd `sys.stdout` was the other decision worth having prototyped before building on it.
`tests/test_cli.py` went from 189 to 201 passed with no existing assertion touched, and
`tests/test_holdings_cli.py` from 79 to 85 the same way, which is the concrete form of the
byte-identical promise this repository keeps making.

This work also closes part of the gap `plans/08_consistency_review.md` records at its lines
107-137: `README.md`'s `output/*.md` reports were specified there and unbuilt, and are now built.
What remains open from that finding is unchanged by this plan - the target-return override,
`memory/rules.json`, the multi-set staleness rules, and the `memory/{S,F,U}-summary.md` files.

What remains, deliberately out of scope: the side-by-side summary command that reads these files.
`load_report` in `src/flow/report_archive.py` is the seam it should use, and the headline figures
are already in the front matter so that command is a table build rather than a prose parser. A
`plans/19_*` should cover it. Also deliberately not done: archiving `uv run portfolio-holdings
show`, `set` and `remove`, which print the same held-portfolio block. The writer is shared, so
adding them later is three `record_report` wrappers; they were left out because the user named
`uv run portfolio` and `whatif`, and because a `set` is an edit rather than an analysis.

Lessons. First, the two normalization bugs (the stripped first-line indentation and the surviving
`..`) were both found by tests written from the plan's own edge-case list rather than by running
the feature - the list was worth writing down before the code. Second, the `format_share_count`
comma was found only by an end-to-end assertion on a real front-matter value; a test that had
merely checked "positions is present" would have shipped an unparseable field. Third, the
`whatif` "never writes" promise needed to be reconciled in prose before the code was touched,
because the honest answer (state versus observation) is what determined that `NOT_SAVED` should be
left alone and a per-variant notice added instead.


## Context and Orientation

Everything below is relative to the repository root, `/app/agentic_portfolio`. Python 3.12, managed
with `uv`. The test command is `uv run pytest tests/test_*.py`.

There are two command-line entry points, declared in `pyproject.toml` under `[project.scripts]`:

    portfolio          = "src.flow.cli:main"
    portfolio-holdings = "src.flow.holdings_cli:main"

**`uv run portfolio`** (`src/flow/cli.py`, 2606 lines) builds a candidate pool, optimizes it, and
prints a report. Its `main()` starts at line 2184, its `argparse` parser is built inline from line
2185, and `args = parser.parse_args()` is at line 2350. `--date` accepts `YYYY-MM-DD` or the word
`today`; `parse_date` at line 125 turns either into a `datetime.date`. `--value` is the money to
allocate, in the pool's own currency. `--selection` picks how the pool is chosen, and
`user_provided` means the user types the tickers.

The report is printed by `print_pipeline_result(result, risk_free_rate_origin, portfolio_value)` at
line 977. It prints a header (mode, rebalance date, objective, selection), the LLM-S rule if there
was one, the scanner branch and candidate list, and then delegates to
`print_weights_and_allocation` at line 408 for the portfolio itself: currency, returns window,
weights, per-ticker expected return and volatility, the dividend section, the portfolio's annual
return, volatility and Sharpe ratio, the benchmark's same three figures, the risk-free rate and
where it came from, dividend yield and coverage and any floor, the target return, the share
allocation and the leftover cash. After that call, `print_pipeline_result` prints
`result["concentration_note"]` when there is one - a sentence warning that a clamped target return
has concentrated the money in a single holding.

A run that cannot be optimized at all does not reach that block: `print_pipeline_result` returns
early at line 1026 after printing `Cannot optimize this run: <reason>`.

After the report, `main()` prints the user's own saved holdings (`print_user_portfolio` at line
785), then enters `_run_edit_loop` at line 1933. That loop prompts
`[a]dd / [r]emove / [o]bjective / [t]arget-return / [d]ividend / [b]enchmark / [s]ummary /
[f]inish`, and after each accepted edit it recomputes and calls `print_weights_and_allocation`
directly, at line 2175. It never reprints the header and never reprints the holdings block - the
comments there explain why: an edit to the candidate pool changes neither.

**`uv run portfolio-holdings`** (`src/flow/holdings_cli.py`, 950 lines) maintains the portfolio the
user actually holds, one per currency, in `memory/portfolio.json`. Its `main()` is at line 814 and
its subcommands are `show`, `set`, `remove` and `whatif`, dispatched through a `runners` dict at
line 915. `--date` defaults to `today`.

`whatif` is `_run_whatif` at line 497. It measures the saved portfolio, prints it with
`print_user_portfolio`, and loops on
`What if? [s]et shares (any ticker) / [r]emove / [w]indow / [u]ndo all / [f]inish`. After each
accepted change it prints the hypothetical portfolio with a `heading` of
`What if (<CURRENCY>) - not saved`, then `format_holdings_delta` (`src/flow/cli.py:924`) and
`format_dividend_delta` (`src/flow/cli.py:682`) - the signed change in the figures. `[w]indow`
varies the length of the returns window between 24 and 60 months and re-measures the baseline too.
On `[f]inish` it prints the module constant `NOT_SAVED` at line 422, "Nothing was saved: that was a
what-if, and your portfolio is unchanged.", and then the exact `set` command that would make the
experiment real.

The records the report is built from are all `typing.NamedTuple`s. The three that matter here:

- `PortfolioStats` (`src/optimizer/portfolio.py:644`) carries `weights`, per-ticker
  `expected_returns` and `volatility`, `portfolio_expected_return`, `portfolio_volatility`,
  `portfolio_sharpe`, `risk_free_rate`, `target_annual_return`, `returns_window_start`,
  `returns_window_end`, `returns_window_months`, and optional dividend fields including
  `portfolio_dividend_yield`.
- `HoldingsStats` (`src/optimizer/holdings.py:145`) carries `currency`, `positions`, `weights`,
  `market_values`, `total_value`, `annual_return`, `annual_volatility`, `sharpe`,
  `risk_free_rate`, `window_start`, `window_end`, `window_months`, `priced_as_of`, `excluded`,
  `unavailable_reason` and a `dividends` sub-record. Its numeric figures are all-or-nothing: either
  the six of them are populated, or all six are `None` and `unavailable_reason` is a sentence.
- `BenchmarkStats` (`src/optimizer/benchmark.py:149`) carries `ticker`, `currency`, and the same
  three figures over the portfolio's exact window, with the same all-or-nothing discipline.

Two more are needed for the front matter: `ResolvedObjective`
(`src/optimizer/benchmark.py:372`) with `objective`, `target_annual_return`, `origin` and
`clamped_from`; and `DividendFloor` (`src/optimizer/dividends.py:102`) with `yield_floor`,
`origin`, `cash_floor`, `portfolio_value` and `currency`.

Configuration lives in `src/config/settings.py`, a 78-line `pydantic_settings.BaseSettings`
subclass instantiated once as the module-level singleton `settings` at its line 78. Every field
name doubles as a case-insensitive environment-variable name, so a field called `output_dir` is
overridable by setting `OUTPUT_DIR`, and `.env` at the repository root is loaded automatically.

The only atomic file writer in the project is `_write_rates` in `src/flow/rate_memory.py`, lines
217-240: it creates the parent directory, writes to a `tempfile.mkstemp` file in the *same*
directory, `os.replace`s it into position, and unlinks the temporary file on any `BaseException`.
That is the pattern this plan copies. Every other writer is a plain `write_text` or a DuckDB
statement.

`output/` is already listed in `.gitignore` (line 8), so nothing written by this feature can be
committed by accident. Note the consequence, which matters for the test guard: a test that
pollutes `output/` leaves `git status` clean and is therefore invisible.

Terms used below, defined here because they are not ordinary English:

- **Report body**: the exact text `print_weights_and_allocation` prints, plus the concentration
  note when there is one - or, for `whatif`, the text `print_user_portfolio` prints plus the two
  delta lines. This is what gets hashed and what gets stored beneath the front matter.
- **Digest**: the SHA-256 hash of the report body, hex-encoded, after normalization (trailing
  whitespace stripped from each line, leading and trailing blank lines removed). The first eight
  characters appear in the filename.
- **Front matter**: a block of `key: value` lines at the top of the saved file, delimited above and
  below by a line containing exactly three hyphens. A convention borrowed from static site
  generators; here it is read and written with plain string operations and needs no YAML library.
- **Variant**: which report within a session this is - `initial`, `edit`, `baseline` or `what-if`.
  It is front-matter metadata, not part of the digest, so two variants whose text happens to be
  identical collapse into one file.
- **Tee**: an object that stands in for `sys.stdout`, forwarding everything written to it to the
  real stream while also keeping a copy. Installed with `contextlib.redirect_stdout`.


## Plan of Work

### Milestone 1 - the archive module, standing alone

At the end of this milestone a new module can hash, name, write and read back a report file, and it
is tested without touching either CLI, the network, or DuckDB.

Create `src/flow/report_archive.py`. It owns everything about `output/`: nothing else in the project
may write there. It needs `hashlib`, `io`, `os`, `sys`, `tempfile`, `contextlib`, `datetime`,
`pathlib` and `typing` - no third-party imports, and in particular no YAML library, because the
front matter is scalars only.

Define `DEFAULT_OUTPUT_DIR = "output"` and add the matching `output_dir: str = "output"` field to
`Settings` in `src/config/settings.py`, beside `db_path` at its line 46, with a docstring saying
what it is for. The module constant is the fallback; `settings.output_dir` is the env-overridable
source the CLIs read.

Define the private `_Tee` class and the `capture_report()` context manager exactly as prototyped:
`_Tee.write` writes to its buffer and then to the wrapped stream, returning the wrapped stream's
return value, and `_Tee.__getattr__` delegates everything else (`flush`, `isatty`, `encoding`,
`writelines`) so the object is a faithful stand-in. `capture_report()` wraps `sys.stdout` as it is
at entry - never `sys.__stdout__` - because that is what makes it compose with `capsys`, and yields
a zero-argument callable returning the text captured so far.

Define `report_digest(text)`: normalize by `"\n".join(line.rstrip() for line in
text.strip().splitlines())`, then SHA-256 the UTF-8 bytes and return the hex digest. Normalizing
first is what stops a stray trailing space from defeating deduplication.

Define the two records, `ReportArchive` and `SavedReport`, and the functions `save_report`,
`format_save_notice`, `load_report` and the `record_report` context manager. Their exact signatures
are in `Interfaces and Dependencies` below, and their behaviour is:

`save_report` refuses a body that is empty after normalization, returning `None` - a report that
printed nothing is not a report. Otherwise it computes the digest, builds the month directory
`<output_dir>/<as_of as YYYY-MM>`, builds the filename from the tokens described below, and returns
a `SavedReport` with `created=False` and no write at all if that path already exists. When it does
write, it writes the front matter and then a blank line and then the normalized body, atomically:
`mkdir(parents=True, exist_ok=True)`, `tempfile.mkstemp(dir=month_dir, prefix=".report-",
suffix=".md")`, write, close, `os.replace`, and `Path(tmp).unlink(missing_ok=True)` inside an
`except BaseException: raise` guard, copying `src/flow/rate_memory.py:217-240`.

The filename tokens are the as-of date in `YYYY-MM-DD`, the archive kind, then whichever of the
objective and the selection are known, then the currency, then the first eight characters of the
digest, joined with single hyphens and suffixed `.md`. So a pool report is
`2026-09-10-portfolio-GMV-user_provided-USD-a1b2c3d4.md` and a whatif report is
`2026-09-10-whatif-USD-7b1e9f02.md`. Any token that could contain a character unsafe in a filename
is passed through a small `_slug` helper that keeps letters, digits, dots, underscores and hyphens
and replaces every other run of characters with a single hyphen; in practice objectives, selections
and currency codes are already safe, and the helper exists so a future kind or a hand-set currency
cannot produce a path with a slash in it.

Front matter is written as `---`, then one `key: value` line per fact in a stable, hand-chosen
order (never a dict's iteration order, because a stable order is what makes two files diffable),
then `---`. Values are formatted by type: a `date` as `YYYY-MM-DD`, a float that represents a ratio
to four decimal places, a float that represents money to two, an int bare, a string as-is, a
sequence of strings joined with `", "`. A fact whose value is `None` is omitted entirely rather than
written as the word `None`, because an absent benchmark and a benchmark literally named "None" must
not look the same. `digest` carries the full 64-character hex digest even though the filename
carries only eight, so a file can be verified against its own contents.

`load_report` is the inverse and exists because it is the seam the follow-up summary command will
read, and because a round-trip test is the cheapest proof the writer is correct. It reads a path,
requires the first line to be `---`, collects `key: value` lines splitting on the FIRST colon only
(so a `command` value containing a colon survives), stops at the closing `---`, and returns the
facts as a `dict[str, str]` alongside the body text. Values come back as strings; interpreting them
is the caller's business.

`format_save_notice` turns a `SavedReport` into the one line the CLI prints -
`Saved report: <path>` when it created the file, `Report already saved this month: <path>` when it
did not - and returns `None` for a `None` input, so the CLI site has no branching of its own.

`record_report(archive, **facts)` is the only thing either CLI touches. Given a `None` or disabled
archive it yields immediately and does nothing else, so a call site with archiving off behaves
exactly as it did before this change. Otherwise it opens `capture_report()`, yields, and afterwards
calls `save_report` and prints `format_save_notice`'s line. Because the save is after the `yield`
and NOT in a `finally`, a block that raises saves nothing - which is correct, since a report that
did not finish printing is not a report - while the text already printed has still reached the
terminal, because the tee writes through immediately rather than at the end.

Write `tests/test_report_archive.py`, hermetic, using `tmp_path` and no fixtures, matching the
house style of a module docstring stating why it touches no network. Cover: digest stability and
normalization (trailing spaces and surrounding blank lines do not change the digest, but a changed
figure does); the month folder coming from the as-of date and not from today; the filename token
order and the eight-character digest suffix; `_slug` refusing to emit a path separator; front-matter
round-trip through `load_report`, including a value containing a colon and an omitted `None` fact;
the empty-body refusal returning `None`; saving twice producing one file with `created` `True` then
`False`; `record_report` with a `None` archive printing nothing and creating no directory; and
`record_report` letting an exception propagate while writing no file.

### Milestone 2 - wire it into `uv run portfolio`

At the end of this milestone a pool run writes its initial report and one file per edit-loop
recompute, an edit that returns to an earlier portfolio writes nothing new, and the whole test suite
still passes without writing into the repository.

Add two flags to `main()` in `src/flow/cli.py`, beside `--holdings-cache-path` around line 2320 so
the path flags stay together: `--output-dir`, defaulting to `settings.output_dir`, documented as
where reports are saved and that pointing it elsewhere is how a scratch run avoids the real
archive; and `--no-save-reports`, a `store_true`, documented as the counterpart of `--no-holdings` -
print the report and keep no copy.

Build exactly one `ReportArchive` per run, immediately after `rebalance_date = parse_date(args.date)`
at line 2427, before any network work. One per run and not one per report, because `kind`, `as_of`,
`command` and `output_dir` are properties of the run, and because building it early means a
malformed `--output-dir` is discovered before a live snapshot is fetched. `command` is
`" ".join([Path(sys.argv[0]).name, *sys.argv[1:]])`, so the recorded command is reproducible
without embedding the container's absolute paths.

Add an `archive: ReportArchive | None = None` keyword parameter to `print_pipeline_result` (line
977) and wrap its `print_weights_and_allocation` call and the concentration note (lines 1028-1035)
in `record_report`. The default of `None` is what keeps every existing caller and every existing
test byte-identical. The facts passed are: `variant="initial"`, the currency, the resolved objective
and its `origin`, the selection, the candidate list, `--value`, the benchmark ticker and its annual
return when it resolved, the risk-free rate, the dividend floor's yield and origin when there is
one, the window start, end and month count from `stats`, and the portfolio's own annual return,
volatility, Sharpe ratio and dividend yield. The unsatisfiable path returns before this block, so it
archives nothing without needing a check.

Thread the archive through `_run_edit_loop` (line 1933) as one more carried keyword, alongside the
risk-free rate and its origin, which are carried for exactly the same reason - a facility that
worked on the initial report and vanished on the first edit would be worse than none - and wrap the
loop's `print_weights_and_allocation` call at line 2175 the same way, with `variant="edit"` and the
current objective, target, candidates and benchmark rather than the initial ones. Each recompute is
a report of its own, so each is offered to the archive; identical ones collapse by digest.

Add `tests/conftest.py`: a module docstring explaining the hazard, and one autouse fixture that
uses `monkeypatch` to point `settings.output_dir` at a subdirectory of `tmp_path` for every test in
the suite. Because `pydantic_settings` models are ordinary Pydantic models, set the attribute on the
singleton with `monkeypatch.setattr(settings, "output_dir", str(tmp_path / "output"))`; verify while
implementing that the model permits attribute assignment, and if it does not, patch
`src.flow.cli.settings` and `src.flow.holdings_cli.settings` module attributes instead and say so
in `Surprises & Discoveries`.

Add tests to `tests/test_cli.py`, in its existing style - argv via
`monkeypatch.setattr("sys.argv", ...)`, `builtins.input` scripted through the existing `_script`
helper at its line 66, output read from `capsys`, and every path pointed at `tmp_path`. Cover: a run
saves one file and prints the notice; the same run repeated prints the already-saved notice and
leaves one file; `--no-save-reports` writes nothing and prints no notice; an edit to a different
objective adds a second file and editing back adds no third; an unsatisfiable run writes nothing;
and the saved file's front matter carries the objective, selection, currency, candidates, window and
the three headline figures, with its body equal to the weights block that was printed.

### Milestone 3 - wire it into `whatif`

At the end of this milestone a whatif session leaves one file per distinct variant, and still writes
nothing at all to `memory/`.

Add the same two flags to `main()` in `src/flow/holdings_cli.py`, around line 900 beside
`--rates-path`, worded for this command. Build one `ReportArchive` with `kind="whatif"` inside
`_run_whatif` (line 497) once the currency and date are settled.

Wrap the baseline's `print_user_portfolio` (line 580) in `record_report` with `variant="baseline"`,
and each hypothetical's `print_user_portfolio` plus the two delta prints (lines 644-657) with
`variant="what-if"`. Wrap the "these are the saved holdings again" reprint at line 635 as
`variant="baseline"` too, since that is what it is. Do NOT wrap the stale-share-count warning at
line 579: it is a fact about `memory/portfolio.json` rather than about the portfolio's figures, and
including it would give the same holdings two different digests depending on cache state.

The facts for a whatif report are the currency, the positions rendered as `TICKER:shares` pairs in
sorted order, the total value, the priced-as-of date, the risk-free rate, the window start, end and
month count, and the annual return, volatility and Sharpe ratio - each omitted when `None`, which
is the whole point of omitting `None` facts, because `HoldingsStats` is all-or-nothing and an
unmeasurable portfolio legitimately has none of the three.

Amend `_run_whatif`'s docstring at line 517. Its paragraph currently begins "Never writes." and
must instead state precisely what is written and what is not: no position, no rate, nothing that
changes a later run - and a report file under `--output-dir` unless `--no-save-reports` is given.
Leave the `NOT_SAVED` constant at line 422 alone; it is about the portfolio and remains true, and
the per-variant notice lines make the file writes explicit.

Add tests to `tests/test_holdings_cli.py` in its existing style: a whatif session saves the baseline
and each distinct variant; `[u]ndo all` adds no new file because the text matches the baseline
already saved; `--no-save-reports` writes nothing; and the session leaves `memory/portfolio.json`
and the rates file byte-identical, asserted by reading them before and after.

### Milestone 4 - documentation reconciliation

At the end of this milestone `README.md` describes what the code does, and no longer describes a
convention nobody built.

Add a paragraph to `README.md`'s Live Mode section, in that file's established voice - dense prose
that says what the behaviour is and why it is that way, naming the flags. Cover the month folder and
that it comes from the as-of date, the digest deduplication and why a second identical run leaves
one file, the front matter and that it exists so reports can later be compared as data, the two
flags, and the exact sense in which `whatif` still saves nothing.

Then replace the stale sentence at line 55 - the `output/GMV-, MSR-, MV-{set}-portfolio-{date}.md`
convention - with the naming actually implemented, since that line predates per-currency pools, the
`user_provided` selection and month folders. Note in this plan's `Outcomes & Retrospective` that
doing so closes part of the gap recorded at `plans/08_consistency_review.md:107-137`.


## Concrete Steps

All commands run from the repository root, `/app/agentic_portfolio`.

Establish the baseline before touching anything:

    $ uv run pytest tests/test_*.py --collect-only -q | tail -1
    880 tests collected in 11.33s

Milestone 1, after writing `src/flow/report_archive.py` and `tests/test_report_archive.py`:

    $ uv run pytest tests/test_report_archive.py -q

Expect every test to pass, and note that before the module exists the file cannot even be imported,
so the whole file fails before the change and passes after.

Milestone 2, after the flags, the archive construction, the two wrapped sites, `tests/conftest.py`
and the new tests:

    $ uv run pytest tests/test_report_archive.py tests/test_cli.py -q

Milestone 3:

    $ uv run pytest tests/test_holdings_cli.py -q

After each milestone, and certainly before declaring the work done, run the whole suite. It takes
about thirteen minutes on this container, so run it in the background rather than waiting on it, and
treat a large jump in that runtime as a sign a test has started reaching the network:

    $ uv run pytest tests/test_*.py

Expect `880 passed` plus the number of tests added.

Then exercise it for real. Use a scratch output directory and scratch memory files so the
repository's own `output/` and `memory/` are untouched:

    $ printf 'd\nf\n' | uv run portfolio --date today --objective GMV --value 100000 \
        --selection user_provided --output-dir /tmp/report-check \
        --memory-path /tmp/cand.json --rates-path /tmp/rates.json
    $ ls /tmp/report-check/2026-09/
    $ head -30 /tmp/report-check/2026-09/*.md

The `head` should show the front matter followed by the same weights-and-allocation text the
terminal printed:

    ---
    digest: a1b2c3d4...
    saved_at: 2026-09-10T06:12:44Z
    as_of: 2026-09-10
    kind: portfolio
    variant: initial
    command: portfolio --date today --objective GMV --value 100000 --selection user_provided ...
    currency: USD
    objective: GMV
    objective_origin: named on the command line
    selection: user_provided
    value: 100000.00
    candidates: AVB, EQR, SPY, T
    benchmark: SPY
    risk_free_rate: 0.0200
    window_start: 2021-10-31
    window_end: 2026-08-31
    window_months: 59
    annual_return: 0.0871
    annual_volatility: 0.0451
    sharpe: 1.4878
    ---

    Portfolio currency: USD
    Returns window: ...

Repeat the identical command and confirm the already-saved notice and that `ls` still shows one
file. Then drive the edit loop to a second objective and back:

    $ printf 'd\no\nMSR\no\nGMV\nf\n' | uv run portfolio --date today --value 100000 \
        --selection user_provided --output-dir /tmp/report-check \
        --memory-path /tmp/cand.json --rates-path /tmp/rates.json

Then the whatif loop, checking the memory files either side:

    $ md5sum memory/portfolio.json memory/rates.json > /tmp/before.md5
    $ printf 's\nSPY 10\nf\n' | uv run portfolio-holdings whatif --currency USD \
        --output-dir /tmp/report-check
    $ md5sum -c /tmp/before.md5

Finally the opt-out, and a check that nothing leaked into the repository:

    $ uv run portfolio-holdings whatif --currency USD --no-save-reports </dev/null
    $ git status --porcelain
    $ ls output 2>&1


## Validation and Acceptance

Run `uv run pytest tests/test_*.py` and expect `927 passed` - the 880 that passed before this
plan, plus its 47. `tests/test_report_archive.py` fails before the change - it cannot import
`src.flow.report_archive` - and passes after.

Run the `uv run portfolio` command above. Accepted when the report prints exactly as it did before
this change, one additional line `Saved report: /tmp/report-check/2026-09/<name>.md` appears at the
end, and that file exists with front matter naming the objective, currency, candidate list, returns
window and the three headline figures, followed by the identical weights-and-allocation text.

Run it a second time with identical arguments. Accepted when the line reads
`Report already saved this month: ...` naming the same path, and `ls` on the month folder still
shows exactly one file. This is the deduplication requirement and also the idempotence proof.

Run the edit-loop command that switches to MSR and back to GMV. Accepted when the MSR recompute
produces a second file, and returning to GMV reports the first file as already saved rather than
writing a third - which demonstrates that deduplication works across the initial report and an
edit-loop variant, the reason the archived body is anchored on the shared section.

Run a deliberately impossible request, for example `--objective MV --target-return 5.0`. Accepted
when the run prints `Cannot optimize this run: ...` and the month folder gains no file.

Run the whatif command above. Accepted when the baseline and the hypothetical each leave a file,
the session still ends with `Nothing was saved: that was a what-if, and your portfolio is
unchanged.`, and `md5sum -c /tmp/before.md5` reports both memory files unchanged - the never-save
guarantee, verified rather than asserted. Choosing `[u]ndo all` after an edit is accepted when it
adds no file, because its text matches the baseline already stored.

Run either command with `--no-save-reports`. Accepted when no notice line is printed and no file is
created anywhere under the output directory.

Finally run `git status --porcelain` after all of the above. Accepted when it is clean, and `ls
output` shows either no such directory or nothing from these runs - proof the feature wrote only
where it was told to.


## Idempotence and Recovery

Every step here is safe to repeat. The month directory is created with `exist_ok=True`. A report
whose digest already exists is skipped rather than rewritten, so re-running an analysis converges
instead of accumulating. The write itself is `mkstemp` plus `os.replace`, so a run interrupted
mid-write leaves either the previous file or the complete new one and never a truncated one, and the
temporary file is unlinked on any exception; a stray `.report-*` file in a month folder is therefore
evidence of a hard kill and is safe to delete by hand.

Nothing is ever deleted from `output/`. Pruning old months is out of scope and belongs to the
person who owns the archive. To undo everything this feature produced, `rm -rf output/`; because
`output/` is gitignored, no commit is ever at risk.

If the work has to be abandoned part-way, the safe stopping points are the milestone boundaries.
After milestone 1 the new module exists and is tested but nothing calls it, which changes no
behaviour at all. After milestone 2 the pool report is archived and `whatif` is not, which is
coherent on its own. Recovery from a bad wiring change is `git checkout -- src/flow/cli.py` (or
`src/flow/holdings_cli.py`), since the module itself carries no state.


## Artifacts and Notes

The tee prototype, which is the one piece of this design worth having proved before building on it:

    $ uv run python -c "..."
    through-to-fake: 'Mode: live  Objective: GMV\nWeights:\n'
    captured:        'Mode: live  Objective: GMV\nWeights:\n'
    equal: True
    isatty delegated: True

The starting state, for a reader who wants to confirm nothing was there before:

    $ grep -rn "output/" src/ --include=*.py | wc -l
    0
    $ grep -rn "hashlib\|sha256\|md5" src/ | wc -l
    0
    $ ls output
    ls: cannot access 'output': No such file or directory

One report as actually saved, from a live run against a two-ticker `user_provided` pool. The
`command` line is truncated here for width; in the file it is complete:

    ---
    digest: e207ef3c58f5a00423a6746b487f5de3aeed8874c68ccf2ad841b6e4d7d2a4cb
    saved_at: 2026-09-10T13:53:21Z
    as_of: 2026-09-10
    kind: portfolio
    variant: initial
    command: portfolio --date today --objective GMV --value 100000 --selection user_provided ...
    currency: USD
    objective: GMV
    objective_origin: --objective
    selection: user_provided
    value: 100000.00
    candidates: SPY, T
    risk_free_rate: 0.0200
    window_start: 2021-10-01
    window_end: 2026-09-01
    window_months: 60
    annual_return: 0.1267
    annual_volatility: 0.1426
    sharpe: 0.7482
    annual_dividend: 1892.57
    dividend_yield: 0.0189
    ---

    Portfolio currency: USD - --value is interpreted as USD
    Returns window: 2021-10-01 to 2026-09-01 (60 month(s) of monthly returns)

    Weights:
      SPY: 0.7300
      T: 0.2700

    Expected return / volatility (annualized):
      SPY: return=0.1225  volatility=0.1617
      T: return=0.1382  volatility=0.2506

    Dividend yield / annual income (trailing 12 months):
      SPY: yield=0.0099  $725.21 USD
      T: yield=0.0432  $1,167.35 USD

    Portfolio expected return: 0.1267  Portfolio volatility: 0.1426  Portfolio Sharpe: 0.7482
    Risk-free rate used: 0.0200 (the configured default)
    Portfolio dividend yield: 0.0189  Annual dividend income: $1,892.57 USD (on --value $100,000.00 USD)
    Minimum dividend yield: n/a (no floor was asked for; use --min-annual-dividend or --min-dividend-yield)
    Target annual return: n/a (objective is GMV, not MV)

    Share allocation:
      SPY: 96
      T: 1049
    Leftover cash: $303.04 USD
    Annual dividends at these share counts: $1,888.89 USD (a 0.0189 yield on --value $100,000.00 USD)

Deduplication across the initial report and an edit-loop variant, which is the case that decided
where the archived body starts and stops. The script drives GMV, then MSR, then back to GMV:

    $ printf 'd\no\nMSR\no\nGMV\nf\n' | uv run portfolio --date today --objective GMV \
        --value 100000 --selection user_provided --currency USD --output-dir $SP/report-check ...
    Portfolio expected return: 0.1267  Portfolio volatility: 0.1426  Portfolio Sharpe: 0.7482
    Saved report: .../2026-09/2026-09-10-portfolio-GMV-user_provided-USD-60432f13.md
    Portfolio expected return: 0.1273  Portfolio volatility: 0.1430  Portfolio Sharpe: 0.7503
    Saved report: .../2026-09/2026-09-10-portfolio-MSR-user_provided-USD-874bc999.md
    Portfolio expected return: 0.1267  Portfolio volatility: 0.1426  Portfolio Sharpe: 0.7482
    Report already saved this month: .../2026-09/2026-09-10-portfolio-GMV-user_provided-USD-60432f13.md

Three recomputes, two files. The `whatif` session, with `[u]ndo all` recognized the same way and
the never-save guarantee checked rather than asserted:

    $ md5sum $SP/pf.json $SP/rt.json > $SP/before.md5
    $ printf 's\nT 100\nu\nf\n' | uv run portfolio-holdings whatif --currency USD \
        --path $SP/pf.json --rates-path $SP/rt.json --output-dir $SP/report-check
    Annual return: 0.0230  Annual volatility: 0.1209  Sharpe: -0.1242
    Saved report: .../2026-09/2026-09-10-whatif-USD-eafde087.md
    What if (USD) - not saved:
    Annual return: 0.0237  Annual volatility: 0.1228  Sharpe: -0.1163
    Change from your saved portfolio: return +0.0007  volatility +0.0020  Sharpe +0.0079
    Saved report: .../2026-09/2026-09-10-whatif-USD-a269bdaf.md
    Back to your saved holdings.
    Annual return: 0.0230  Annual volatility: 0.1209  Sharpe: -0.1242
    Report already saved this month: .../2026-09/2026-09-10-whatif-USD-eafde087.md

    Nothing was saved: that was a what-if, and your portfolio is unchanged.

    $ md5sum -c $SP/before.md5
    .../pf.json: OK
    .../rt.json: OK

Both commands' reports sitting in one month folder, told apart by the `kind` token in the name:

    $ ls -1 $SP/report-check/2026-09/
    2026-09-10-portfolio-GMV-user_provided-USD-60432f13.md
    2026-09-10-portfolio-GMV-user_provided-USD-e207ef3c.md
    2026-09-10-portfolio-MSR-user_provided-USD-874bc999.md
    2026-09-10-whatif-USD-a269bdaf.md
    2026-09-10-whatif-USD-eafde087.md

The opt-out, and the proof nothing reached the repository:

    $ printf 'f\n' | uv run portfolio-holdings whatif --currency USD --path $SP/pf.json \
        --output-dir $SP/nowhere --no-save-reports | grep -Ec "Saved report|already saved"
    0
    $ ls $SP/nowhere
    ls: cannot access '.../nowhere': No such file or directory
    $ ls output
    ls: cannot access 'output': No such file or directory
    $ git status --porcelain
     M README.md
     M src/config/settings.py
     M src/flow/cli.py
     M src/flow/holdings_cli.py
     M tests/test_cli.py
     M tests/test_holdings_cli.py
    ?? plans/18_saved_report_archive.md
    ?? src/flow/report_archive.py
    ?? tests/conftest.py
    ?? tests/test_report_archive.py


## Interfaces and Dependencies

No new third-party dependency. Everything used is in the Python 3.12 standard library:
`hashlib.sha256`, `io.StringIO`, `contextlib.redirect_stdout` and `contextmanager`,
`tempfile.mkstemp`, `os.replace`, `pathlib.Path`, `datetime.date` and `datetime.datetime`.

In `src/config/settings.py`, add to `Settings`:

    output_dir: str = "output"

In `src/flow/report_archive.py`, define:

    DEFAULT_OUTPUT_DIR = "output"

    class ReportArchive(NamedTuple):
        output_dir: str
        enabled: bool
        kind: str            # "portfolio" | "whatif"
        as_of: date
        command: str

    class SavedReport(NamedTuple):
        path: Path
        digest: str
        created: bool        # False means an identical report was already there

    def report_digest(text: str) -> str

    def save_report(
        text: str,
        archive: ReportArchive,
        facts: dict[str, object],
    ) -> SavedReport | None

    def format_save_notice(saved: SavedReport | None) -> str | None

    def load_report(path: Path | str) -> tuple[dict[str, str], str]

    @contextmanager
    def capture_report() -> Iterator[Callable[[], str]]

    @contextmanager
    def record_report(archive: ReportArchive | None, **facts) -> Iterator[None]

In `src/flow/cli.py`, `print_pipeline_result` gains one keyword-only parameter and `_run_edit_loop`
gains one keyword parameter, both defaulting to `None` so no existing caller changes:

    def print_pipeline_result(
        result: dict,
        risk_free_rate_origin: str | None = None,
        portfolio_value: float | None = None,
        *,
        archive: ReportArchive | None = None,
    ) -> None

    def _run_edit_loop(..., archive: ReportArchive | None = None) -> bool

Both CLIs gain the same two flags:

    --output-dir DIR        default settings.output_dir
    --no-save-reports       store_true

The call shape at every wrapped site is:

    with record_report(archive, variant="initial", currency=currency, objective=objective, ...):
        print_weights_and_allocation(...)
        if note is not None:
            print(note)


## Revision Note (2026-09-10)

This plan was written before implementation and revised on completion of all four milestones. The
changes, and why each was made:

`Progress` now records each milestone as done with its timestamp and the test count it moved, plus
the end-to-end run against live data, because a plan whose checkboxes lag the working tree cannot
be restarted from - which is the whole point of the section.

`Surprises & Discoveries` gained four entries that only implementation could produce. Two were
outright bugs in this plan's own first attempt at the archive module - `text.strip()` silently
discarding a report's first-line indentation, and a filename slug that allowed dots and so let
`../..` through as `..-..` - and both are recorded rather than quietly fixed, because the next
contributor who touches `normalize_report` or `_slug` needs to know what those two lines are
defending against. The third, `format_share_count`'s thousands separator making a `positions`
field unparseable, is recorded because it is the kind of mistake that passes every "is the field
present?" test. The fourth, live prices moving between two runs minutes apart, is recorded because
it will otherwise be misread as a deduplication failure by whoever first looks at a month folder
during market hours.

`Outcomes & Retrospective` was written, as the section requires, comparing the result against the
original purpose and stating what was deliberately left undone: the side-by-side summary command
(the user's explicit second step, and `load_report` is the seam it should use) and archiving the
`show`, `set` and `remove` subcommands.

`Artifacts and Notes` gained the real transcripts - one saved file in full, the GMV/MSR/GMV
deduplication run, the `whatif` session with its `md5sum -c` proof, and the `--no-save-reports`
opt-out - replacing the illustrative examples the plan was written with. Evidence from a run that
actually happened is worth more than an example of what one should look like, and the acceptance
criteria in `Validation and Acceptance` are phrased against exactly these outputs.

Two implementation details differ from what the plan first specified, and the plan now reflects
the code rather than the intention. `report_facts` and `holdings_report_facts` were added to
`src/flow/cli.py` rather than left implicit: the archive module deliberately knows none of
`PortfolioStats`, `BenchmarkStats`, `ResolvedObjective` or `HoldingsStats`, so something in the
display layer has to flatten them, and the display layer is the only layer that already knows all
four. And `command_line` was added to `src/flow/report_archive.py` rather than inlined at each
`main()`, so both entry points record a command the same way and the `argv[0]`-basename rule is
tested once.
