# Summarize a month of saved portfolio reports into one decision briefing

This ExecPlan is a living document. The sections `Progress`, `Surprises & Discoveries`,
`Decision Log`, and `Outcomes & Retrospective` must be kept up to date as work proceeds.

This document must be maintained in accordance with `PLANS.md`, at the repository root of
`/app/agentic_portfolio`. It lives at `plans/19_monthly_report_summary.md`, per `AGENTS.md`'s rule
that ExecPlans live under `plans/` as `NN_*.md`.

It builds directly on `plans/18_saved_report_archive.md`, which is checked into this repository
and incorporated here by reference. Everything this plan relies on from it is nevertheless
restated below, so a reader who opens only this file can finish the work.


## Context

`plans/18_saved_report_archive.md` made both report-printing commands persist their output under
`output/<YYYY-MM>/`: `uv run portfolio` (which screens or accepts a candidate pool, optimizes it,
and prints weights plus a share allocation) and `uv run portfolio-holdings whatif` (which prices
the portfolio you actually hold and lets you try hypothetical changes to it). Each saved file opens
with a `---`-delimited block of `key: value` facts above the report text. That plan deliberately
stopped there and said so: "That later command - the side-by-side summary - is deliberately NOT
part of this plan. This plan delivers the saving layer it will read."

This plan is that command. Right now `output/2026-09/` holds nine files and nothing can read them
back. `src/flow/report_archive.py:473`'s `load_report` was written as the seam for exactly this and
has no caller outside its own tests.

The nine real files show why a summary is worth building, and shaped the structure below. Four are
`kind: portfolio` reports saved within two minutes of each other: one 17-ticker `user_provided`
pool taken through MV pinned to the benchmark's 0.1225 return, then MSR, then GMV, then MV re-pinned
to 0.1000. Five are `kind: whatif` reports: a held book of PFF/PFFA/VZ worth $408,200 measured over
60 months and again over 48, then two hypothetical variants. No single one of those nine files can
say that the best Sharpe ratio of the month was 2.0207 (MSR) while the book actually held scored
-0.1253; that five of the seventeen candidates never earned weight under any objective; or that the
held book's annual return moves from 0.0229 to 0.0713 on the window length alone. Those are the
facts a month's archive contains and no report in it states.

After this change, `uv run portfolio-summary` reads a month folder and writes one briefing that
answers those questions, with every figure computed in Python and the prose written by a CrewAI
agent on a cheap model configured by the new `LLM_QUICK` environment variable.


## Purpose / Big Picture

Someone who has spent an afternoon running `uv run portfolio` and `uv run portfolio-holdings
whatif` can now run one command and get back a single document that says what they explored, which
portfolio won on which axis, what every run agreed on, how the book they actually hold compares to
the frontier they just mapped, and what to run next - with a warning wherever two reports are not
actually comparable.

Here is the whole feature in one transcript. Run from the repository root, `/app/agentic_portfolio`:

    $ uv run portfolio-summary
    Reading output/2026-09/ ... 9 reports (4 portfolio, 5 whatif), 1 currency.

    # Portfolio archive summary - 2026-09

    9 reports saved 2026-09-11, as-of 2026-09-11, currency USD.
    Benchmark SPY: return 0.1225, volatility 0.1481, Sharpe 0.5704.
    Risk-free rate 0.0380 in every report. Two returns windows appear; see Comparability.

    ## What you explored
    ... two or three paragraphs of prose, written by the agent ...

    ## Risk/return leaderboard
    Window 2021-10-01 to 2026-09-01 (60 months), USD - 7 portfolios

      rank  what                        return   volatility   Sharpe   div yield
         1  portfolio MSR               0.2513       0.1056   2.0207      0.0475
         2  portfolio MV @0.1225        0.1225       0.0517   1.6347      0.0405
         3  portfolio MV @0.1000        0.1000       0.0467   1.3289      0.0416
         4  whatif CSPX.L+PFFA+VZ       0.0839       0.1250   0.3670      0.0460
         5  portfolio GMV               0.0480       0.0416   0.2394      0.0385
         6  whatif PFFA+VZ              0.0498       0.1447   0.0813      0.0795
         7  whatif PFF+PFFA+VZ (held)   0.0229       0.1207  -0.1253      0.0682
            benchmark SPY               0.1225       0.1481   0.5704         n/a

    Window 2022-10-03 to 2026-09-01 (48 months), USD - 2 portfolios
      ... the two 48-month whatif reports, ranked among themselves only ...

    ## What every run agreed on
    ... deterministic lists, then the agent's reading of them ...

    ## Comparability and methodology cautions
    ... deterministic, from the real data ...

    ## What to run next
    ... the agent's suggestions, each a runnable command ...

    Saved summary: output/2026-09/2026-09-11-summary-4f2a9c11.md

    $ uv run portfolio-summary
    Reading output/2026-09/ ... 9 reports (4 portfolio, 5 whatif), 1 currency.
    Summary already saved for these 9 reports: output/2026-09/2026-09-11-summary-4f2a9c11.md
    Re-run with --force to write a new one, or --stdout to print without saving.

Five things in that transcript are the whole point.

First, every number in it was computed in Python from the saved front matter. The agent is handed
those numbers already computed and writes only prose. A report summary that quietly miscalculated
a Sharpe ratio would be worse than no summary, and the model this runs on - `openai/gpt-5-nano` by
default - is the cheapest one available precisely because it is small.

Second, the leaderboard is split by returns window, not merged. Ranking a 48-month measurement
against a 60-month one would be comparing two different questions. The real archive contains both,
so the command must handle it rather than average over it.

Third, the second run wrote nothing. Dedup is by a digest over the *source reports'* digests, not
over the summary text - because the agent's prose differs run to run, so a text digest would write
a new file every time and the archive would fill with near-duplicates.

Fourth, the summary lands in the month folder it describes, carrying `kind: summary` front matter,
and the reader deliberately skips `kind: summary` - otherwise the second run would summarize the
first summary.

Fifth, the command works with no LLM at all. `--no-llm` prints the same document with the prose
sections omitted and a note saying so. That is what makes every test in this plan hermetic, and it
is the fallback when `OPENAI_API_KEY` is unset.

Two later milestones widened who the briefing is FOR, and they share this transcript. A document
that is English and Markdown serves the person who ran the commands and nobody else. So:

    $ uv run portfolio-summary 2026-09 --language zh-TW --pdf
    Reading output/2026-09/ ... 9 reports (4 portfolio, 5 whatif), 1 currency.

    # Portfolio archive summary - 2026-09
    ... the briefing, in English, with prose ...

    Saved report: output/2026-09/2026-09-11-summary-473f6f15.md
    Saved PDF: output/2026-09/2026-09-11-summary-473f6f15.pdf

    # 投資組合檔案摘要 - 2026-09
    ... the same document, same tables, same figures, Chinese sentences ...

      rank  what                       return  volatility  Sharpe   div yield  div income
      1     portfolio MSR              0.2513  0.1056      2.0207   0.0475     4,753.20

    Translation: zh-TW by openai/gpt-5-nano, from 2026-09-11-summary-473f6f15.md.
    Every figure and every table above is the English original's.
    Saved report: output/2026-09/2026-09-11-summary-zh-TW-91c40e7b.md
    Saved PDF: output/2026-09/2026-09-11-summary-zh-TW-91c40e7b.pdf

Four files, and three things in that transcript matter as much as the first five.

Sixth, the leaderboard in the Chinese document is the SAME BYTES as the leaderboard in the English
one. The translator is never shown a table. That is not a safety belt bolted on afterwards; it is
the plan's own figures-in-Python split applied one level up, and it is what keeps the columns
aligned - `_table` pads with `str.ljust` while a CJK glyph is double-width, so a translated header
would break every row beneath it and the Chinese document would be HARDER to read than the English.

Seventh, the English document is always written first and the translation is derived from it, so
the two can be diffed and neither can quietly disagree with the other. A month that already has an
English summary needs no prose call at all to gain a Chinese one.

Eighth, the PDF is a rendering of the saved Markdown rather than a second document, and its tables
are fixed-width text because that is the only thing that keeps seven columns in line on a page.


## Progress

- [x] (2026-09-11 13:05Z) Wrote this ExecPlan and put it at `plans/19_monthly_report_summary.md`.
- [x] (2026-09-11 13:20Z) Verified against the real archive before writing code: the nine files in
      `output/2026-09/` are four `kind: portfolio` and five `kind: whatif`, all USD, on two returns
      windows; and confirmed the predicted cross-report findings by hand so the acceptance criteria
      below are real rather than hoped for.
- [x] (2026-09-11 13:45Z) Milestone 1: `src/flow/report_summary.py` (the deterministic pass, no LLM
      and no network) plus `tests/test_report_summary.py`. `uv run pytest
      tests/test_report_summary.py -q` reports 55 passed.
- [x] (2026-09-11 14:00Z) Milestone 2: `llm_quick` and `openai_api_key` in
      `src/config/settings.py`; `src/agents/summary_schema.py`, `src/agents/summary_crew/`
      (crew plus `config/agents.yaml` and `config/tasks.yaml`), `src/agents/report_summary.py`
      including `verify_narrative`, and `tests/test_report_summary_agent.py` (19 passed).
- [x] (2026-09-11 14:15Z) Milestone 3: `src/flow/summary_cli.py`, the `portfolio-summary` console
      script in `pyproject.toml`, the five new `FACT_ORDER` keys in `src/flow/report_archive.py`,
      the amended `tests/conftest.py` docstring, and `tests/test_summary_cli.py` (30 passed).
- [x] (2026-09-11 14:20Z) Milestone 4: `README.md` describes the command, `LLM_QUICK` and
      `OPENAI_API_KEY`.
- [x] (2026-09-11 14:30Z) Milestone 5: a live `openai/gpt-5-nano` call produced grounded prose and
      the verifier dropped one suggestion that stated an uncomputed figure. End-to-end transcript
      in `Artifacts and Notes`.
- [x] (2026-09-11 14:40Z) Full suite green: `uv run pytest tests/test_*.py` reports
      `1031 passed, 3 warnings in 442.98s` - the 927 baseline plus the 104 tests that existed when
      that run started. Two further tests were added afterwards, so
      `uv run pytest tests/test_*.py --collect-only -q` now reports `1033 tests collected`. The
      runtime FELL, from the 455.73s recorded in `plans/18_saved_report_archive.md`, which is the
      reassuring direction: nothing in the new code reaches the network.
- [x] (2026-09-11 15:10Z) Milestone 6: the `Source reports` appendix, so a row of the briefing can
      be traced to the file behind it. `build_sources` and `SourceReport` in
      `src/flow/report_summary.py`, the rendered section, and the reworded trailing set digest.
      Added after the feature was in use: see the amendment note at the foot of this file.
- [x] (2026-09-11 15:25Z) Milestone 6, follow-on found while verifying it: `existing_summary` in
      `src/flow/summary_cli.py` returned the FIRST matching summary by filename, which after a
      `--force` run means the older briefing. Now returns the newest by `saved_at`. Evidence in
      `Surprises & Discoveries`.
- [x] (2026-09-11 15:30Z) Milestone 6 tests: 16 added in total -
      `tests/test_report_summary.py` 57 -> 68 and `tests/test_summary_cli.py` 30 -> 35.
      `uv run pytest tests/test_report_summary.py tests/test_report_summary_agent.py
      tests/test_summary_cli.py -q` reports 122 passed.
- [x] (2026-09-11 15:50Z) Full suite green on the finished code: `uv run pytest tests/test_*.py`
      reports `1049 passed, 3 warnings in 422.13s`, and `--collect-only -q` confirms 1049 - the
      1033 baseline plus the 16 tests of Milestone 6. The runtime fell again, from 443s, so nothing
      in the appendix reaches the network. Note the earlier 436s/1046 run predates the
      `existing_summary` fix and is superseded by this one.
- [x] (2026-09-13 07:20Z) Milestone 7: the translated briefing.
      `src/agentic_portfolio/flow/briefing_blocks.py` (splitter plus token protection, 39
      tests), `protected_literals` in `src/agentic_portfolio/flow/report_summary.py`,
      `src/agentic_portfolio/agents/translation_schema.py`,
      `src/agentic_portfolio/agents/translate_crew/`,
      `src/agentic_portfolio/agents/report_translation.py` (40 tests), the `--language` flag and
      the reuse-the-saved-English path in `src/agentic_portfolio/flow/summary_cli.py` (22 tests),
      four new `FACT_ORDER` keys and a `language` filename token, `purpose`/`remedy` parameters
      on `api_key_problem`, and `translate_crew` added to `CREW_PACKAGES`.
- [x] (2026-09-13 07:45Z) Milestone 8: the PDF. `src/agentic_portfolio/flow/report_pdf.py` and
      the packaged `report_pdf.css` (48 tests, WeasyPrint invoked for real), the `--pdf` flag
      and `_render_pdf` in the CLI (12 tests), two packaging guards for the stylesheet, the
      Pango/HarfBuzz/font packages in both container images, and the corrected
      `pyproject.toml` dependency comment.
- [x] (2026-09-13 07:50Z) Both milestones verified end to end against a copy of the real
      22-report `output/2026-09/`. Transcript and measurements in `Artifacts and Notes`.
- [x] (2026-09-13 08:35Z) Full suite green on the finished code: `uv run pytest tests/test_*.py`
      reports `1261 passed, 3 warnings in 634.33s`. The runtime rise over the 422s recorded for
      Milestone 6 was investigated rather than accepted and is not this amendment's: the 15
      slowest tests are all in `tests/test_holdings.py` and `tests/test_holdings_cli.py`, which
      import nothing changed here, while the nine affected files total 5.2s. See
      `Artifacts and Notes`.
- [x] (2026-09-14 14:00Z) Milestone 9: adapted the visual language of the root `report.css` into
      the packaged `src/agentic_portfolio/flow/report_pdf.css`, while retaining the fixed-width
      table, CJK, provenance and no-overwrite contracts. Added three renderer tests and one
      packaging assertion; `uv run pytest tests/test_report_pdf.py tests/test_packaging.py -q`
      reports 72 passed in 2.27s.
- [x] (2026-09-14 14:08Z) Milestone 9 full-suite verification: `uv run pytest
      tests/test_*.py -q` reports `1264 passed, 3 warnings in 411.24s` - the previous 1261 tests
      plus the three new renderer tests. The warnings are the same expected-return warnings from
      three holdings tests recorded by the earlier milestone, not PDF regressions.
- [ ] Not done: no live-model run of `--language`. Every translation path was exercised with a
      stub, so the verifier, the gates and the CLI wiring are proven while the QUALITY of a real
      `gpt-5-nano` Traditional Chinese translation is not. That is the one thing left to try, and
      it costs one call.

## Surprises & Discoveries

- Observation: `pathlib.Path.glob("*.md")` matches dotfiles, and `save_report` writes through a
  `.report-*.md` temporary. A process killed mid-write therefore leaves a file the loader would
  pick up and fail on.
  Evidence: a directory holding `.report-abc.md` and `real.md` gives
  `glob *.md -> ['.report-abc.md', 'real.md']`. `load_month` now filters
  `not p.name.startswith(".")`, and skips such a file silently rather than reporting it - a
  half-written report is not something the reader should be asked to look at.

- Observation: `litellm` is NOT installed in this container, and `openai/gpt-5-nano` never reaches
  it. CrewAI 1.15.18 lists that model in `crewai.llms.constants.OPENAI_MODELS`, so the bare string
  resolves to `crewai.llms.providers.openai.completion.OpenAICompletion`, which calls the OpenAI
  SDK directly. Crucially, `temperature` defaults to `None` and the provider only sends it when it
  is set, so the string shorthand cannot trip the gpt-5 family's "temperature must be 1"
  restriction at all.
  Evidence: `litellm installed: False`; `LLM(model='openai/gpt-5-nano')` gives
  `class: OpenAICompletion, temperature attr: None`; and
  `crewai/llms/providers/openai/completion.py:863` reads `if self.temperature is not None:`.
  Consequence: the plan's expected `crewai.LLM(model=..., temperature=1)` fallback was NOT needed,
  and the repo's existing `llm="provider/model"` shorthand is kept. The real landmine is the
  opposite of the one anticipated - passing `max_tokens` would 400 on a gpt-5 model, which wants
  `max_completion_tokens` - so this crew passes neither, and a future output cap must use the
  latter.

- Observation: `pydantic_settings` loads `.env` WITHOUT exporting it to `os.environ`. A pre-flight
  API-key check written against the environment alone - which is what this plan originally
  specified - would have refused to run on this very repository, whose keys live only in `.env`.
  Evidence: with `.env` present, `settings.anthropic_api_key set from .env: True` while
  `os.environ has ANTHROPIC_API_KEY: False`. `api_key_problem` therefore consults
  `settings.<provider>_api_key` first and `os.environ` second, and `openai_api_key` is declared in
  `Settings` for that reason rather than for documentation.

- Observation: the archive dedupes by a digest of the report BODY, which makes a test fixture that
  omits a body line the reports really carry silently lose reports. The first draft of
  `tests/test_report_summary.py` built whatif bodies without the `Returns window:` line, so the
  60-month and 48-month baselines of the same book produced identical bodies and the archive -
  entirely correctly - stored one file instead of two. The nine-report fixture quietly became
  eight and nine tests failed.
  Evidence: `assert 9 == len(records)` failing with 8 until `_whatif_body` gained the window line
  and `_portfolio_body` gained the headline-figures line. Lesson recorded because it generalizes:
  a fixture for this archive must differ wherever the real reports differ, or it is testing a
  smaller month than it claims.

- Observation: a `whatif` variant's printed delta cannot be reconstructed from the saved
  four-decimal facts. `output/2026-09/2026-09-11-whatif-USD-6d095826.md` prints
  `return +0.0359`, while `0.1073 - 0.0713 = 0.0360`: the report subtracted at full precision and
  stored the operands rounded.
  Evidence: `parse_whatif_deltas` returns `0.0359` and the test
  `test_every_whatif_variant_is_paired_with_the_baseline_of_its_own_window` asserts it. So the
  ledger parses the printed line rather than recomputing it, and a summary that recomputed would
  contradict the file it cites.

- Observation: the same trap applies to the benchmark. The front matter carries `benchmark_return`
  but neither the benchmark's volatility nor its Sharpe, and deriving the Sharpe gives
  `(0.1225 - 0.0380) / 0.1481 = 0.5706` where the report prints `0.5704`. `parse_benchmark` reads
  the body line instead.

- Observation: (Milestone 6) `existing_summary` named the WRONG summary once a month had two of
  them. It returned the first match in filename order, and a summary's filename carries a digest of
  its own text, which has no chronological meaning at all. So after a `--force` run - the very thing
  a reader does to pick up a changed briefing format - a later run pointed them back at the
  superseded file.
  Evidence: with `473f6f15` saved at `13:58:53Z` and `81849599` at `14:48:43Z`, both covering the
  same nine reports, `portfolio-summary 2026-09` reported `473f6f15`. It now reports `81849599`.
  Found only by regenerating the real archive rather than by any test, which is the argument for
  running a feature against real data after changing it: the bug needed two summaries of one month
  to exist, and no test had created that situation.

- Observation: (Milestone 6) `save_report` stamps `saved_at` to the nearest second, so two summaries
  written by one test tie and no ordering can separate them. The tests that are about which summary
  is newer therefore set the timestamps themselves, and the production tiebreak falls back to the
  filename - arbitrary but stable.
  Evidence: the first draft of `test_after_force_a_repeat_run_names_the_newer_summary` failed
  because both files carried the same `saved_at`, and the test's own `max` and the code's
  `(saved_at, name)` key broke the tie in opposite directions.

- Observation: on a live call, `openai/gpt-5-nano` wrote grounded prose but produced one suggested
  command containing a figure nobody computed, and `verify_narrative` dropped it. The mechanism
  earned its place on its first real use rather than in theory.
  Evidence: the run's closing line, `Prose: openai/gpt-5-nano, with 1 section(s) replaced by
  machine-written text for stating a figure that was never computed (next_runs (1 of 5 dropped)).`
  A separate imperfection the verifier does NOT catch is noted under
  `Outcomes & Retrospective`: it checks figures, not flag spelling, and the model invented a
  positional argument for `portfolio-holdings whatif`.

- Observation: (Milestone 8) the briefing's tables are invisible to a Markdown parser. `_table`
  in `src/agentic_portfolio/flow/report_summary.py` indents by TWO spaces and pads with
  `str.ljust`, but CommonMark needs FOUR spaces to make a code block, so a 2-space indented line
  is lazy paragraph continuation - and HTML then collapses the very runs of spaces that are the
  column alignment. So the obvious implementation of "convert the Markdown to HTML and print it"
  destroys every table in the document, which is the one thing the document exists for.
  Evidence: the real saved briefing holds 95 indented lines across 13 blocks. This is why there is
  no Markdown library in Milestone 8 and why every indented run becomes a `<pre>` element: a
  generic converter would have needed a pre-processor that already knew this rule, at which point
  the pre-processor is the converter.

- Observation: (Milestone 8) a line too wide for the page is not reported by WeasyPrint. With
  `white-space: pre` a 302-character line is laid out on ONE line 620pt wide inside 688pt of
  content width and simply runs off the paper - no warning, no error, no visible sign in the
  build. The document's widest indented line is 183 characters, in the every-window list.
  Evidence: rendering `"  " + "W"*300` at 9pt lays out all 302 characters on a single line;
  the same input under `white-space: pre-wrap` lays out 302 characters across two lines.
  Consequence: `pre-wrap` plus a hanging indent is load-bearing rather than cosmetic, and each
  block is additionally sized from its own widest line so that wrapping is the exception.

- Observation: (Milestone 8) no font in this container has a single CJK glyph, and nothing says so.
  `fc-list :lang=zh-tw` is empty and the only families present are DejaVu Sans, DejaVu Serif and
  DejaVu Sans Mono. A Traditional Chinese PDF therefore renders as a page of empty boxes, and
  WeasyPrint emits no warning at all while doing it.
  Evidence: probing every `.ttf` under `/usr/share/fonts` shows all six map `A` and none maps
  U+6708 (月). Consequence: `--pdf` REFUSES a CJK document when no CJK font is installed, naming
  the font package, rather than writing a plausible-looking file full of boxes. A silent artifact
  that gets emailed is worse than a refusal, and the Markdown is already on disk either way.

- Observation: (Milestone 8) the released image could not have imported WeasyPrint at all.
  `docker/Dockerfile.dev` installs `libpango-1.0-0`, `libpangoft2-1.0-0`, `libharfbuzz0b` and
  `libharfbuzz-subset0`, but the runtime stage of the root `Dockerfile` installs only `libgomp1`
  and `libatomic1`, and neither image installs any font package. WeasyPrint `dlopen`s Pango
  through cffi at IMPORT time, so the failure would have been an `OSError` at the first `--pdf`
  and in no build step. Note no cairo is needed: WeasyPrint has written PDF itself since v53.
  Evidence: the two `apt-get install` lines, and a clean render in this container, which has no
  `libcairo2`.

- Observation: (Milestone 8) fontconfig can be asked which families exist WITHOUT a new
  dependency and without shelling out to `fc-list`. WeasyPrint already holds a cffi handle on the
  fontconfig library, so `from weasyprint.text.ffi import ffi, fontconfig` and an
  `FcFontMatch` for a family name answers the question directly: a family that is installed
  matches itself, and one that is absent matches the substituted default.
  Evidence: `"DejaVu Serif" -> "DejaVu Serif"` while `"Noto Sans CJK TC" -> "DejaVu Sans"`.
  Consequence: the glyph check needs no `fontTools` declaration, which the first draft of this
  milestone had added, and no `fc-list` binary - fontconfig's LIBRARY is guaranteed wherever
  WeasyPrint imports at all, but its command-line tool is a separate package and is not.

- Observation: (Milestone 7) a naive "protect the tokens that must survive translation" regex
  mangles ordinary English, and it does so because of this project's own data. `T` is a real
  ticker in the September candidate pool, so matching protected literals as plain substrings
  masked the `T` inside `THIN`, `The` and `Two`, turning `WITHIN` into `WI[[0]]HIN`. Separately, a
  `provider/model` pattern greedily swallowed the sentence period in `Prose: openai/gpt-5-nano.`.
  Evidence: masking the real saved briefing produced `Ranked by Sharpe ratio WI[[0]]HIN each
  returns window` before the fix and the correct `WITHIN` after it. Consequence: protected
  literals are anchored with `(?<![0-9A-Za-z])` and `(?![0-9A-Za-z])`, and a trailing dot is
  excluded from the provider pattern. Recorded because it generalizes: the protected set is drawn
  from real market data, so single-letter tickers WILL collide with English words, and any future
  matching rule must be anchored.

- Observation: (Milestone 7) the protected set should come from the month's own data, not from a
  regex over capital letters. A `[A-Z]{2,}` rule looks right and is wrong: it protects `CAUTION`,
  `WITHIN`, `SAME` and `NOT APPLICABLE`, all of which are English words this feature exists to
  translate. The `MonthDigest` already knows every ticker, currency, objective, benchmark, label,
  filename, report digest and window date in the month, so the exact list is available for free.
  Evidence: masking all 34 prose lines of the real briefing with the digest-derived list plus five
  narrow patterns leaves every English word intact and every ticker, command, filename, date and
  64-character digest opaque - for example `Skipped: [[0]]: skipped, kind is summary` and
  `- [[0]] - settle [[1]] under standard benchmark and risk-free settings.`

- Observation: (Milestone 7) the marker check is BLIND to a lost heading, and the markers were
  the whole verification story until this was noticed. `#` is not a figure and a heading often
  carries no markers at all, so a model that translated `## Risk/return leaderboard` into a bare
  sentence passed every check - and the rebuilt document then wrote that block without its `##`,
  silently demoting a section to a paragraph. Found by reading the verifier rather than from a
  failure, because the test helper standing in for a translator was reproducing the prefix by
  accident.
  Evidence: `verify_block("## Risk/return leaderboard", "風險報酬排行榜")` returned `None` before
  the fix. `structure_prefix` now extracts the leading `#{1,6} ` or `- ` and the verifier requires
  the translation to keep it - and to not GROW one, since a paragraph that came back as a heading
  is the same defect mirrored. Twelve tests failed the moment it was added, all of them because
  the helper had been translating the prefix away; the helper was wrong in exactly the way a live
  model would be. The general lesson: a mechanical check covers what it measures, and structure is
  not a figure.

- Observation: (Milestone 7) a marker is lost to adjacent punctuation long before it is lost to
  a bad translation. The first test helper written to stand in for a translator kept any word
  that STARTED with a marker, which quietly dropped the marker in `([[1]]` - and a rendered
  briefing puts punctuation against a marker constantly, in `([[1]] portfolio, [[2]] whatif)`
  and every window caption.
  Evidence: the helper produced a block the verifier rejected, and the run failed the coverage
  gate at 71%. The helper was what was wrong, but the lesson is about the real thing: this is
  the single likeliest way a live model loses a marker, which is an argument for the rule being
  exact equality on an ordered list rather than anything fuzzier.

- Observation: (Milestone 8) `--language --pdf` in THIS container produces three files, not
  four, and that is the design working rather than a defect. The English pair is written, the
  Chinese Markdown is written, and the Chinese PDF is refused by name because no CJK font is
  installed. The refusal is per FILE, so forgetting to install a font costs the translation's
  PDF and nothing else.
  Evidence: `Warning: no PDF - ...-summary-zh-TW-....md contains characters that need a CJK
  font, and none is installed, so every one of them would print as an empty box. Install one
  (on Debian: apt-get install fonts-noto-cjk) ...`. A test written to assert four files failed
  here, correctly, and was split into one test of the CLI wiring (with the font probe patched)
  and one of the refusal (with it patched the other way) - because whether a machine has a
  Chinese font must not decide whether the suite passes.

- Observation: (Milestone 8) `<p[^>]*>` also matches `<pre ...>`, so the obvious regex for "find
  every paragraph" reports every table as a paragraph. Worth one line because the test it broke
  was the test asserting that no table is EVER rendered as a paragraph - the assertion that
  matters most in that file was the one the regex silently inverted.
  Evidence: the failure quoted `'  rank  what ... return'` as a paragraph's contents. The
  pattern is now `<p(?:\s[^>]*)?>`.

- Observation: (Milestone 7) importing `crewai` puts `.env` into `os.environ`, which makes
  `monkeypatch.delenv` alone an unreliable way to simulate a missing key - and makes an ad-hoc
  check of the missing-key path spend money instead of refusing.
  Evidence: `os.environ.pop("OPENAI_API_KEY")` then `import crewai` leaves the variable SET
  again, because CrewAI calls `load_dotenv()` at import time. A by-hand check of
  `translate_briefing`'s refusal path, written as "clear the key, then import and call", therefore
  sailed past `api_key_problem` and made one real model call. Consequence: within the test suite
  this is harmless, because `crewai` is imported once before any test body runs and a `delenv`
  after that sticks - which is why the existing tests work. But it is why those tests clear
  `settings.openai_api_key` AS WELL as the environment variable, and why a refusal path must be
  exercised from a test rather than from a scratch script.

- Observation: (Milestone 7) CrewAI's brace interpolation is safe for a substituted value today,
  but only by one implementation detail. `crewai.utilities.string_utils.interpolate_only` raises
  `KeyError: "Template variable 'stray' not found in inputs dictionary"` for any `{name}` in the
  TEMPLATE it cannot resolve, and interpolation runs once - so a brace arriving inside the
  document being translated passes through untouched.
  Evidence: `interpolate_only("Doc: {document}", {"document": "a {brace} here"})` returns
  `"Doc: a {brace} here"`, while a stray `{stray}` in the template raises. Consequence: the
  request text maps braces to brackets anyway. It is one line, it changes nothing observable - the
  real summaries contain no braces - and it removes a dependency on the order in which a third
  party interpolates.

- Observation: (Milestone 9) the root `report.css` cannot safely replace the packaged stylesheet
  verbatim. It expects semantic HTML tables, a subtitle and running-element divs that plan 19's
  deliberately closed converter never emits; its 16mm side margins also disagree with the
  renderer's 510pt table-width constant, and it drops the existing source footer.
  Evidence: applying it unchanged to the real English briefing grew the document from six to nine
  pages and produced a widest text box of 672.8px in a 672.8px content area. The adapted stylesheet
  renders the real English and Traditional Chinese briefings in eight pages, with a 680.3px widest
  text box in the retained 680.3px content area.

## Decision Log

- Decision: Python computes every figure; the CrewAI agent writes only prose, into fixed
  `output_pydantic` fields, and is instructed never to compute or restate an unstated number.
  Rationale: this is exactly the split `src/agents/llm_f.py` already makes - the LLM estimates a
  positive/negative probability per headline and `compute_decayed_score` does all the arithmetic,
  with the docstring at `src/agents/llm_f.py:1-17` recording that a holistic LLM judgment was
  deliberately removed in `plans/08_consistency_review.md`. The same reasoning applies harder here:
  the default model is the cheapest available. It also makes `--no-llm` a real mode and every test
  hermetic. Date/Author: 2026-09-11, agreed with the user before implementation.
- Decision: The leaderboard is partitioned by `(currency, window_start, window_end,
  window_months)` and reports are ranked only within a partition.
  Rationale: the real `output/2026-09/` contains the identical PFF/PFFA/VZ positions measured over
  60 months (annual return 0.0229, Sharpe -0.1253) and over 48 (0.0713, 0.2808). One table
  containing both would invite a comparison that the numbers do not support. Currency joins the
  key because the month folder is shared across currencies by design.
  Date/Author: 2026-09-11.
- Decision: Dedup by a SHA-256 over the sorted `digest` values of the source reports, stored as a
  `sources_digest` front-matter fact on the summary, not by a digest of the summary text.
  Rationale: the agent's prose varies between runs, so a text digest never repeats and every run
  would add a file. The question the dedup must answer is "have I already summarized *these
  reports*", and that is what the source digests identify. Date/Author: 2026-09-11.
- Decision: `load_month` accepts only `kind: portfolio` and `kind: whatif` and skips everything
  else, including `kind: summary`.
  Rationale: the summary is written into the folder it describes, so without the filter the second
  run would feed the first summary back in. An allow-list rather than a deny-list means a future
  third report kind is skipped until this module is taught about it, instead of being silently
  half-parsed. Date/Author: 2026-09-11.
- Decision: Do NOT add a `weights` fact to `FACT_ORDER` in `src/flow/report_archive.py`.
  Rationale: it would only populate for files saved after the change, so the body parser is needed
  regardless to read the nine files that already exist. Two sources for one fact is worse than one
  slightly fragile source, and this plan does not touch the archive writer at all. Revisit only if
  the body format changes. Date/Author: 2026-09-11.
- Decision: The new setting is `llm_quick`, read from `LLM_QUICK`, defaulting to
  `openai/gpt-5-nano`, and it goes in `src/config/settings.py` beside `llm_s_model`.
  Rationale: the user specified the name, the default and the purpose. Putting it in `Settings`
  rather than a bare `os.environ.get` follows `llm_s_model` and not `llm_f.py:110`'s
  `os.environ.get("LLM_F_MODEL", ...)`, which bypasses the settings singleton and is the known
  asymmetry in this repo. Date/Author: 2026-09-11.
- Decision: Enforce the no-arithmetic rule mechanically as well as by instruction, with
  `verify_narrative` in `src/agents/report_summary.py`: every figure a prose field states must also
  appear in the facts the model was given, or the field is replaced by a deterministic sentence and
  the substitution is reported in the document's last line.
  Rationale: this goes slightly beyond what the plan as approved described, which relied on the
  schema descriptions and the agent backstory. Those are instructions, and an instruction to a
  small model is a hope. The test is deliberately narrow - quoting a computed figure is useful and
  allowed; producing one that was never computed is the single failure the whole design exists to
  prevent - and it is the only one of the three mechanisms that can be tested without spending
  money. Its first live use justified it: one of five suggestions was dropped for an invented
  threshold. A silent substitution would be worse than the fabrication, so the count and the field
  names are printed. Date/Author: 2026-09-11.
- Decision: `render_digest(digest, prose=None)` takes a `Mapping[str, str]` of section key to
  paragraph, rather than the plan's separate `render_summary` in the CLI assembling section
  fragments.
  Rationale: the plan's intent was that the deterministic module stay unaware of the agent, and a
  mapping of text satisfies that - the module renders text it is handed and cannot tell a model's
  sentence from a fixed string. The alternative, splitting the rendered document back apart in the
  CLI, would have duplicated the section order in two files and let them drift. The narrative is
  mapped onto those keys by `narrative_prose` in `src/flow/summary_cli.py`, which is the one place
  the two halves meet. Date/Author: 2026-09-11.
- Decision: Keep the repo's bare `llm="provider/model"` shorthand; do not use `crewai.LLM`.
  Rationale: verified rather than assumed - see `Surprises & Discoveries`. `openai/gpt-5-nano`
  routes to CrewAI's native OpenAI provider, which omits `temperature` unless it is set, so the
  gpt-5 temperature restriction never applies. `CREWAI.md` recommends `crewai.LLM` unconditionally,
  but consistency with `LLMSCrew` and `LLMFCrew` wins where the provider does not force otherwise.
  The two conditions that would flip this: wanting an output cap (which must be
  `max_completion_tokens`, never `max_tokens`) or a non-default `base_url`.
  Date/Author: 2026-09-11.
- Decision: `verbose=False` on `ReportSummaryCrew`, where both other crews use `verbose=True`.
  Rationale: this crew runs underneath a command whose entire terminal output is a briefing and a
  one-line save notice, and CrewAI's progress banner would bury it. LLM-S and LLM-F run inside long
  pipelines where that banner is the only sign of progress. The difference is in the setting rather
  than a change of mind, and it is recorded so it does not read as an oversight.
  Date/Author: 2026-09-11.
- Decision: `generate_narrative` raises `NarrativeUnavailable` rather than returning `None`, and
  returns `(narrative, replaced_fields)` rather than a bare narrative.
  Rationale: a caller cannot use a half-built narrative by accident, and the CLI has to report how
  much of the prose was replaced - which means the count has to travel with it. The CLI catches the
  exception and prints the computed briefing regardless: an unreachable model must not cost the
  reader the figures. Date/Author: 2026-09-11.
- Decision: a repeat is recognized by scanning each existing summary's `sources_digest` FACT, not
  by looking for a filename.
  Rationale: `save_report` builds the filename from a digest of the body, and the body contains the
  prose, which differs between runs. So the filename cannot answer "have these reports already been
  summarized"; only the fact can. Date/Author: 2026-09-11.
- Decision: (Milestone 6) The briefing gains a `Source reports` appendix and NOTHING else - no
  `report` column on the leaderboard, no inline `[digest8]` beside any label.
  Rationale: the user's explicit choice among three presented options, taken to keep the briefing's
  body reading exactly as it already did. The cost is recorded here so it is not later rediscovered
  as a bug: tracing a row to a file is a manual, by-label step, and the two sections most likely to
  send a reader to the source - the what-if ledger and the window-sensitivity pairs - still name no
  file. Adding the inline digests later is small, and `SourceReport` is already the data it would
  need. Date/Author: 2026-09-11.
- Decision: (Milestone 6) The appendix qualifies a label with its returns window, but only when
  another report in the month shares the unqualified form.
  Rationale: load bearing, not cosmetic. In the real `output/2026-09/`, `label()` names a whatif
  report by its positions, so four of the nine reports collide: the held book appears twice
  (`3b1de978` at 60 months, `5b860aeb` at 48) and so does one variant of it (`b3e81204`,
  `6d095826`). A bare appendix would print `whatif PFFA+VZ` twice and answer nothing. Qualifying
  only where needed keeps the other seven labels matching the leaderboard verbatim, which is how a
  reader gets from a row into this list at all. Where two reports still collide after qualification
  - the same positions over the same window, which happens when a book is re-priced - the digest
  and saved-time columns already tell them apart and no further qualifier is invented.
  Date/Author: 2026-09-11.
- Decision: (Milestone 6) The appendix is built from the records, not from `LeaderRow`s, and
  `LeaderRow.digest` is left alone as documented dead data.
  Rationale: `_collapse` merges rows sharing a label and figures, keeping only the first digest, so
  a `LeaderRow` cannot identify its sources. The appendix is a per-file listing and the records are
  the per-file data. Widening `LeaderRow.digest` to a tuple would be unused code, so `_collapse`'s
  docstring now warns the next reader not to trust that field instead. Date/Author: 2026-09-11.
- Decision: (Milestone 6) `Scope.folder` is derived from `records[0].path.parent` rather than
  threading an `output_dir` argument into `build_month_digest`.
  Rationale: the records know where they came from, and a new parameter would have touched every
  existing caller and test for no gain. Date/Author: 2026-09-11.
- Decision: (Milestone 6) `existing_summary` returns the NEWEST summary matching a source set, not
  the first by filename.
  Rationale: a filename here carries a content digest, which is not an order. After `--force` a
  month legitimately holds two summaries of the same reports, and the one to point a reader at is
  the later one, because it is the one the current version of the command wrote. Ties at
  one-second resolution fall back to the filename, which is arbitrary but stable and is the only
  thing left. Date/Author: 2026-09-11.
- Decision: (Milestone 6) `digest_for_llm` does NOT carry the source list, and a test enforces it.
  Rationale: the model has no use for a filename, and - the reason that matters - `verify_narrative`
  finds figures with the digit-run regex `[0-9][0-9,]*(?:\.[0-9]+)?`, so a hex digest is full of
  them. `02a118b3` alone would make `02`, `118` and `3` count as supported figures. Nine digests in
  the facts sheet would quietly widen the set of numbers the verifier accepts and weaken the one
  mechanism that stops a small model inventing them. Date/Author: 2026-09-11.
- Decision: A month with fewer than two reports gets no LLM call at all.
  Rationale: the direct analogue of `generate_signal`'s empty-headlines short-circuit at
  `src/agents/llm_f.py:104` - there is nothing to compare, so calling anyway would either waste a
  call or invite the model to invent a comparison. Date/Author: 2026-09-11.
- Decision: (Milestone 7) The translator is never shown a table. Only the heading, paragraph and
  bullet blocks are sent; every indented run is re-inserted byte-identical because it was never
  sent anywhere.
  Rationale: this is the same split the whole plan is built on, applied one level up. Three things
  follow from it that no after-the-fact verification of a translated document can promise. Every
  figure, ticker and filename inside a table survives exactly, because it did not travel. Column
  alignment survives, which matters more than it sounds: `_table` pads with `str.ljust` while a
  CJK glyph is double-width in a monospace face, so a translated table header would break every
  row beneath it and the document would be less readable in Chinese than in English. And a failure
  is localizable to one block instead of costing the whole briefing, which is what makes the
  fallback usable rather than theoretical. Date/Author: 2026-09-13.
- Decision: (Milestone 7) Inside a block that IS sent, the tokens that must survive are replaced
  by opaque placeholders built from the month's own data - every ticker, currency, objective,
  benchmark, label, filename, report digest, window date and command string the `MonthDigest`
  knows - plus five narrow patterns for figures, ISO dates, long hex runs, `*.md` names and a
  `uv run ...` command head.
  Rationale: the alternative, a regex over capital letters, protects `CAUTION`, `WITHIN` and `SAME`
  - English words this feature exists to translate - while still missing a lowercase model name.
  Drawing the list from the digest is exact rather than heuristic, and it costs nothing because
  the digest already holds all of it. See `Surprises & Discoveries` for the two bugs the prototype
  found, both of which are about matching rather than about the idea. Date/Author: 2026-09-13.
- Decision: (Milestone 7) The verifier requires the ordered list of placeholders in a translated
  block to EQUAL the list in its source, rather than requiring its figures to be a subset of the
  source's.
  Rationale: strictly stronger than `verify_narrative`'s subset test, and available here only
  because of the decision above - once every figure is an opaque token, equality is a cheap exact
  check rather than an approximation. It catches a dropped ticker, a localized date and a mangled
  digest in one rule. The fallback is deliberately per block, and the count is printed, which is
  the principle `verify_narrative` already established: a silent substitution is worse than the
  fabrication it replaces. Date/Author: 2026-09-13.
- Decision: (Milestone 7) The prose is written in English and then translated, rather than written
  natively in the target language.
  Rationale: `verify_narrative` compares figures, not language, so a Chinese narrative would pass
  it - this is a choice, not a constraint. Three reasons decide it. Only a handful of the
  briefing's lines are model prose; the great majority are the renderer's own English headings,
  captions and `n/a` reasons, so a native-language prose pass would leave the document mostly
  English and fail exactly the reader this milestone is for - and once a document pass exists,
  native prose buys nothing. The English file is always saved, so a native pass would mean either
  an English file with no prose or two independent narratives of one month sharing one
  `sources_digest`; a translation is a derivative that can be diffed against its source, two
  narratives are two documents. And the mechanical check needs a source text: every translated
  line has an English line to be checked against, which is what makes the guarantee auditable by
  a reviewer who reads only English. Date/Author: 2026-09-13.
- Decision: (Milestone 7) A translation is its own archive entry carrying a `language` fact, and a
  repeat is recognized by `(sources_digest, language)`.
  Rationale: the user's explicit choice among three options. The English document stays the
  canonical artifact and the translation is traceable to it by `translated_from`, so the pair can
  be diffed. Matching on the pair is what stops an English summary from blocking a Chinese one.
  The `language` fact is ABSENT rather than `en` on an English file, because the two summaries
  already in `output/2026-09/` have no such fact and a reader should not have to learn that absent
  and `en` mean the same thing; the lookup normalizes instead. Date/Author: 2026-09-13.
- Decision: (Milestone 7) `--language` accepts free-form text - a BCP-47 tag or a plain language
  name - with no allow-list.
  Rationale: the user's explicit choice. Adding Japanese is then not a code change. The cost is
  stated rather than hidden: a typo becomes a strange translation instead of an argument error,
  and a language name in its own script slugs to nothing in the filename, where `_slug` yields
  `unknown` - the file is still correct and still unique, because the name ends in a digest of the
  body, and the `language` fact carries the truth. Date/Author: 2026-09-13.
- Decision: (Milestone 7) `_FIGURE` and `stated_figures` stay in
  `src/agentic_portfolio/agents/report_summary.py`. An earlier draft of this milestone moved them
  into `src/agentic_portfolio/flow/report_summary.py` and re-exported them, citing the
  `api_key_problem` precedent; that was specified and then dropped.
  Rationale: the precedent does not apply, because there is no consumer. It was written while the
  translation verifier was still imagined as comparing FIGURES, which a `crewai`-free flow module
  would have needed. Once the verifier became placeholder equality - see the entry above - the only
  figure pattern left in the flow layer is one alternative among eight inside
  `briefing_blocks._NARROW`, which answers a different question: not "what figures does this prose
  state" but "which spans must not be translated". Those two happen to share a sub-pattern and are
  not one definition in two places. Against that, the move has a real cost that was measured rather
  than assumed: no module under `src/agentic_portfolio/agents/` imports from
  `src/agentic_portfolio/flow/` today, while three flow modules import from agents, so the
  dependency runs exactly one way. Re-exporting would have made it the first bidirectional
  package dependency in the project, in exchange for nothing. Recorded rather than quietly deleted
  because the reasoning, not the conclusion, is the reusable part: a shared helper needs a second
  caller before it needs a new home. Date/Author: 2026-09-13.
- Decision: (Milestone 8) No Markdown library. The converter for this document's four constructs
  is written here instead.
  Rationale: not a preference but a consequence of `Surprises & Discoveries`. A generic parser
  reads the briefing's 2-space-indented tables as prose and collapses the padding that IS the
  table, so it would need a pre-processor that already applied the "an indented run is
  preformatted" rule - at which point the pre-processor is the converter and the library renders
  the four remaining constructs. `render_digest` is the only producer of this text and its
  construct set is closed - two heading levels, plain paragraphs, `- ` bullets, indented runs and
  one inline backtick - so the mapping can be pinned by test. It also keeps faith with the
  eleven packages `plans/20_packaging_and_release.md` removed for being unused in a container
  image: bringing one back to do a tenth of a job we must code anyway would read as reversing that
  decision without actually reversing it. Date/Author: 2026-09-13.
- Decision: (Milestone 8) Each indented block is sized from its own widest line, clamped between
  6.5pt and 9pt, rather than the document taking one uniform size.
  Rationale: measured, not guessed. Usable width on A4 with 15mm margins is 510pt and DejaVu Sans
  Mono advances 0.602em, so a size follows from a character count. The briefing's widest indented
  line is 183 characters, in the every-window list; sizing the WHOLE document to fit that puts the
  leaderboard at about 5.5pt and makes the table the document is built around unreadable in order
  to protect a prose list that happened to be indented. Per-block sizing keeps ten of thirteen
  blocks at full size. Width is measured with `unicodedata.east_asian_width` counting wide and
  fullwidth characters as two, so a block is sized for the space its glyphs actually occupy.
  Date/Author: 2026-09-13.
- Decision: (Milestone 8) `--pdf` renders from the saved file on disk, via `load_report`, not from
  the body this run rendered in memory.
  Rationale: three things follow and each one is wanted. The PDF provably matches the saved bytes
  rather than a parallel rendering of them. It works on the already-saved path, where no body was
  rendered this run - which is what lets someone who forgot `--pdf` get one without `--force` and
  without a second Markdown file. And it works for a translated file whose text this code never
  saw, which is how one flag serves both milestones. Date/Author: 2026-09-13.
- Decision: (Milestone 8) A CJK document with no CJK font installed is REFUSED, not rendered with
  a warning.
  Rationale: the two outcomes are not symmetric. A refusal leaves the reader the Markdown, which
  is already on disk and readable. A warning leaves them a four-page PDF of empty boxes that looks
  finished, and that is the artifact that gets emailed. The check runs before rendering, names the
  font package to install, and names the Markdown file that is complete regardless. Note this is
  the opposite disposition from a failed prose or translation pass, which degrades and continues -
  there the document is still correct without the prose, here it is not. Date/Author: 2026-09-13.
- Decision: (Milestone 8) The glyph check asks fontconfig through WeasyPrint's own cffi handle, so
  no new distribution is declared.
  Rationale: the first draft of this milestone declared `fontTools` to read font `cmap` tables
  directly. It works, but `weasyprint.text.ffi` already exposes the fontconfig library, and an
  `FcFontMatch` answers exactly the question being asked. Shelling out to `fc-list` was the third
  option and is the worst: fontconfig's library is guaranteed wherever WeasyPrint imports at all,
  while its command-line tool is a separate package that the release image does not install.
  Date/Author: 2026-09-13.
- Decision: (Milestone 8) The PDF is not written through `save_report`, and no `pdf_status` fact is
  recorded.
  Rationale: `save_report` is a text-body API - it normalizes, digests, composes a filename from
  archive facts, writes front matter, and refuses a path that exists. A PDF has no body to digest
  independently of the Markdown it renders, no front matter, and an identity that is derived
  rather than archived. Routing it through the archive would need either a second digest scheme or
  a second archive kind, and either would let a PDF's name drift from the Markdown's. So
  `report_filename` keeps sole ownership of `.md` naming and the PDF is only ever a suffix swap. A
  `pdf_status` fact is refused for a sharper reason: front matter is written before the PDF exists,
  so recording it would make the Markdown's own digest and filename depend on whether a PDF
  succeeded. Date/Author: 2026-09-13.

- Decision: (Milestone 9) Adapt the root `report.css` as a visual reference rather than making it
  the runtime stylesheet. Use its navy/blue palette, centered title, larger section hierarchy,
  CJK-aware prose typography and pale code blocks, but keep 15mm side margins, DejaVu Sans Mono
  first for tables, `pre-wrap`, the hanging indent and the source footer.
  Rationale: those retained rules are coupled to table-width arithmetic and archive traceability;
  the reference file targets a different HTML contract. The packaged stylesheet remains the only
  runtime asset and the untracked root file remains untouched. Date/Author: 2026-09-14, agreed
  with the user before implementation.
- Decision: (Milestone 9) Add the report title and month as a top-right running string, keep the
  source at bottom left and page count at bottom right, and add no investment disclaimer.
  Rationale: this gives a detached page context without inventing wording that is absent from the
  saved Markdown. Existing PDFs remain immutable; the new appearance applies only to PDFs rendered
  after this change. Date/Author: 2026-09-14, agreed with the user before implementation.

## Outcomes & Retrospective

The command exists and does what the Purpose section promised. `uv run portfolio-summary` reads a
month folder, prints a briefing whose leaderboard is partitioned by currency and returns window
and ranked by Sharpe within each partition, names the seven tickers every optimizer run held and
the five candidates none did, sets the held book against the best run of the month, lists what each
hypothetical change cost and bought, warns where two reports are not comparable, ends with runnable
next commands, and names the file it saved. A repeat run recognizes the source set and writes
nothing. `--no-llm` produces the same document without prose and makes no network call.

What the split bought. Every figure is computed in `src/flow/report_summary.py` and is therefore
testable without a model: 55 of the 104 new tests cover the arithmetic, and they run in under a
quarter of a second. On the live call the model's prose was grounded and readable. On an earlier
live call one of five suggested runs stated a threshold nobody computed and `verify_narrative`
dropped it. Both outcomes are the design working - the second more usefully than the first,
because it is the one that shows the mechanism is not decorative.

What remains, honestly stated. The verifier checks figures, not flag spelling: on one live run the
model suggested `uv run portfolio-holdings whatif CSPX.L+PFFA+VZ`, and `whatif` takes no positional
arguments - it prompts for changes at its own loop. So a suggested command can be unrunnable while
every figure in it is real. Checking a suggestion against the actual argument parsers would catch
that, and is the obvious next increment; it was not attempted here because the failure is visible
to anyone who types the command, whereas a wrong Sharpe ratio is not, and the two do not deserve
the same weight of machinery.

A gap this plan originally shipped and Milestone 6 closed: the briefing named portfolios and the
archive names files, and nothing connected the two, so a reader who wanted the full report behind a
leaderboard row had to guess among nine filenames. The `Source reports` appendix now answers that,
and it needed no new data - every report's front matter already carried the digest whose first
eight characters `report_filename` builds the filename from. The lesson is that a summary is only
half a tool if it cannot point back at what it summarized, and that is worth designing in rather
than discovering from use.

Also left undone on purpose: no cross-month mode, per the scope decided with the user; no `weights`
front-matter fact, per the `Decision Log`; and no attempt to reconcile `src/agents/llm_f.py`'s
`os.environ.get("LLM_F_MODEL", ...)` with the settings singleton, which is a pre-existing asymmetry
this work sits beside rather than a thing it introduced.

The lesson worth carrying forward is the fixture one in `Surprises & Discoveries`. Building test
inputs with the real writer was right, but it does not protect against a fixture whose bodies are
less varied than the real reports': the archive dedupes by body digest, so a body missing a line
the real reports carry silently merges two reports into one and shrinks the month under test.
Nine tests failed at once for that reason, and the failure looked like nine bugs rather than one
fixture.

As of 2026-09-14 the plan has delivered three further milestones, 7 through 9. They do not change
any figure or any section of the briefing; they change who can read it and how comfortably. The
translated copy keeps every table byte-identical to the English original, the PDF remains a render
of the saved Markdown, and the visual refinement gives that PDF a deliberate navy/blue hierarchy
without weakening the table or provenance guarantees. The amendment notes at the foot of this file
record what those milestones turn on and what they deliberately leave undone.

One claim in this retrospective is worth re-reading in their light. The paragraph above says the
briefing "is only half a tool if it cannot point back at what it summarized". The same sentence
shape applies again and was the argument for both milestones: a briefing is only half a tool if the
person who needs to be shown it cannot read it. Milestone 6 was discovered from use, and so were
these - which suggests the next gap will be too, and will be about reaching somebody this document
still does not.

## Context and Orientation

Read this section as if you know nothing about this repository.

**The archive.** `src/flow/report_archive.py` is the only module that writes under `output/`. It
stores one report per file at `<output_dir>/<YYYY-MM>/<as_of>-<kind>[-<objective>][-<selection>]
[-<currency>]-<digest8>.md`, where the month comes from the run's as-of date rather than the wall
clock. `output_dir` defaults to `settings.output_dir` (`"output"`, overridable with `OUTPUT_DIR` or
`--output-dir`). `output/` is gitignored.

The functions that matter here, all in `src/flow/report_archive.py`:

- `load_report(path) -> tuple[dict[str, str], str]` at line 473. Returns the front-matter facts as
  strings and the body text. Splits each line on its FIRST colon only, so a `command:` value
  containing a colon survives. Raises `ValueError` if the file does not open with a `---` fence.
- `ReportArchive(output_dir, enabled, kind, as_of, command)` at line 151, a `NamedTuple`.
- `save_report(text, archive, facts) -> SavedReport | None` at line 392. Writes front matter, a
  blank line, then the normalized body, atomically. Returns `created=False` and writes nothing if
  the target path already exists.
- `month_dir(archive) -> Path` at line 355, `report_filename(archive, facts, digest)` at line 332,
  `command_line(argv=None)` at line 135, `FACT_ORDER` at line 94.

**A term of art used throughout.** *Front matter* here means the `---`-delimited block of plain
`key: value` lines at the top of each saved report. It is not YAML and no YAML library is used in
either direction; values are flat strings, floats printed to four decimal places for ratios and
two for money, and a key whose value was `None` is absent from the file entirely. So a parser must
treat every key as optional.

**What the front matter actually carries**, verified against `output/2026-09/`. Common to both
kinds: `digest`, `saved_at` (UTC ISO-8601 with a `Z`), `as_of` (`YYYY-MM-DD`), `kind`, `variant`,
`command`, `currency`, `risk_free_rate`, `window_start`, `window_end`, `window_months`,
`annual_return`, `annual_volatility`, `sharpe`, `annual_dividend`, `dividend_yield`.
Only on `kind: portfolio`: `objective` (one of `MV`, `GMV`, `MSR`), `objective_origin` (present
only when the objective was derived rather than typed, e.g. `matching benchmark SPY's 0.1225
return`), `target_return` (only for MV), `clamped_from`, `selection`, `value`, `candidates` (a
`", "`-joined list), `benchmark`, `benchmark_return`, `dividend_floor_yield`,
`dividend_floor_origin`. Only on `kind: whatif`: `positions` (`", "`-joined `TICKER:shares`),
`total_value`, `priced_as_of`. `variant` is `initial` or `edit` for portfolio reports and
`baseline` or `what-if` for whatif reports.

**What the body carries that the front matter does not.** For a portfolio report, the per-ticker
`Weights:` block, the per-ticker expected return/volatility block, the per-ticker dividend block
and the `Share allocation:` block. For a whatif variant, two delta lines reading
`Change from your saved portfolio: return +0.0359  volatility +0.0274  Sharpe +0.1932` and
`Dividend change: yield +0.0113  annual income -$9,858.00 USD`. This plan parses only the
`Weights:` block and those two delta lines; everything else stays unread.

**The CrewAI patterns this repo already uses.** Two crews exist, both single-agent and single-task,
both `@CrewBase` classes with `agents_config = "config/agents.yaml"` and
`tasks_config = "config/tasks.yaml"` beside them: `src/agents/llm_s_crew/crew.py` and
`src/agents/llm_f_crew/crew.py`. The model is a provider-prefixed string passed as
`Agent(config=..., llm=self.model, verbose=True)`; `crewai.LLM` is never instantiated. Structured
output is a Pydantic model on the task (`output_pydantic=...`). The driver module builds the crew,
calls `.crew().kickoff(inputs={...})` and reads `result.pydantic` - see
`src/agents/llm_f.py:93-118`. `src/agents/llm_f_crew/config/tasks.yaml` shows how a long text input
is interpolated through a `{headlines}` placeholder rather than by subclassing.

**Testing conventions.** `AGENTS.md` requires `pytest`, files named `test_*.py`, one file per
module where practical, and no network or LLM calls in unit tests. There is no `unittest.mock` and
no `pytest-mock` anywhere; fakes are hand-written classes installed with `monkeypatch.setattr` on
the name as imported into the module under test. The CrewAI fake to copy is
`tests/test_llm_f.py:205-231`: a three-level duck type (`_FakeTask` with a `.pydantic` attribute,
`_FakeCrew` with `kickoff(inputs)`, `_FakeLLMFCrew` with `crew()`), installed as
`monkeypatch.setattr(llm_f, "LLMFCrew", lambda model: _FakeLLMFCrew(batch, model))`.
`tests/conftest.py` holds the suite's only fixture: an autouse `monkeypatch.setattr(settings,
"output_dir", str(tmp_path / "archived-reports"))`, so no test can read or write the real archive.


## Plan of Work

### Milestone 1 - the deterministic pass

At the end of this milestone a new module can read a month folder and produce the whole briefing
except its prose, with no LLM and no network, and `uv run python -c` can print it for the real
`output/2026-09/`.

Create `src/flow/report_summary.py`. It imports from `src/flow/report_archive.py` and from the
standard library only - no pandas, no numpy, no CrewAI. Its module docstring must state that it
makes no LLM call and reads no network, and why: it is the half of the summary whose output has to
be trustworthy.

Define `SOURCE_KINDS = ("portfolio", "whatif")` and `SUMMARY_KIND = "summary"`.

Define `ReportRecord` as a frozen dataclass holding `path: Path`, `facts: dict[str, str]`,
`body: str`, and typed read-only properties that coerce the string facts: `digest`, `kind`,
`variant`, `currency`, `as_of` (a `date`), `saved_at` (a `datetime`), `objective`,
`objective_origin`, `target_return`, `selection`, `candidates` (a `tuple[str, ...]` split on
`", "`), `benchmark`, `benchmark_return`, `risk_free_rate`, `window_start`, `window_end`,
`window_months` (an `int`), `annual_return`, `annual_volatility`, `sharpe`, `annual_dividend`,
`dividend_yield`, `value`, `total_value`, `positions` (a `tuple[tuple[str, int], ...]`). Every one
returns `None` when the fact is absent or does not parse, because a hand-edited file must degrade
to a blank cell rather than crash the command. Add `weights` and `deltas` properties that call the
body parsers below and cache the result.

Add `label(record) -> str`, the short name a leaderboard row is called by: for a portfolio report,
`"portfolio MSR"`, or `"portfolio MV @0.1225"` when a `target_return` is present; for a whatif
report, the position tickers joined with `+` plus `" (held)"` when `variant` is `baseline` - so
`"whatif PFF+PFFA+VZ (held)"`. Truncate to the first four tickers plus `"+N more"` beyond that.

Add `parse_weights(body) -> tuple[tuple[str, float], ...]`. Scan for a line equal to `Weights:`,
then take following lines matching `^\s{2,}([A-Za-z0-9._-]+):\s*([0-9.]+)\s*$` until the first line
that does not match, and return them in file order (the report prints them descending by weight).
Return an empty tuple when the marker is absent or no line matches - a whatif body has no weights
block and that is not an error.

Add `parse_benchmark(body) -> tuple[str, float, float, float, int, int] | None`. The front matter
carries only `benchmark` and `benchmark_return`, but a portfolio report's body prints the whole
comparison on one line: `Benchmark SPY: return=0.1225  volatility=0.1481  Sharpe=0.5704  (60 of 60
month(s))`. Read the ticker, the three figures and the two month counts from it; return `None` when
the line is absent, which is the case for every whatif report and for a portfolio run whose
benchmark did not resolve. The two month counts matter: a benchmark covering fewer months than the
portfolio's window is a comparability note, not a footnote.

Add `parse_whatif_deltas(body) -> dict[str, float]`. Find a line starting
`Change from your saved portfolio:` and read the `return`, `volatility` and `Sharpe` signed floats
from it; find a line starting `Dividend change:` and read the `yield` signed float and the
`annual income` signed money amount (stripping `$`, `,` and the currency code). Return only the
keys found, and `{}` for a baseline report which has neither line.

Add `load_month(output_dir, month) -> tuple[list[ReportRecord], list[str]]`. `month` is a
`YYYY-MM` string. Glob `<output_dir>/<month>/*.md`, sorted by name for determinism. For each file
call `load_report`; on `ValueError` record a skip note `"<path>: not a report this archive wrote"`
and continue - a stray hand-written note in the folder must not break the command. Keep only
records whose `kind` is in `SOURCE_KINDS`, recording a note for each other kind skipped. Return the
kept records sorted by `(saved_at or as_of, path.name)` alongside the notes. A missing or empty
folder returns `([], [])`; it is the caller's job to say so.

Add `sources_digest(records) -> str`: `hashlib.sha256` over the newline-joined sorted `digest`
values of the records, hex. Two runs over the same set of files get the same value; adding one
report changes it.

Now the analysis. Define these as frozen dataclasses, each a plain data holder with no rendering:

- `Scope`: `month`, `report_count`, `portfolio_count`, `whatif_count`, `currencies`,
  `as_of_first`, `as_of_last`, `saved_first`, `saved_last`, `risk_free_rates` (a mapping of
  currency to the set of rates seen), `benchmarks` (currency to `(ticker, return, volatility,
  sharpe)` where a benchmark report exists), `commands` (the distinct `command` values), and
  `skipped` (Milestone 1's notes).
- `Partition`: `currency`, `window_start`, `window_end`, `window_months`, and `rows`, a list of
  `LeaderRow(label, kind, variant, annual_return, annual_volatility, sharpe, dividend_yield,
  annual_dividend, digest)` sorted by `sharpe` descending with `None` last.
- `Thread`: one exploration session. `kind`, `key` (the grouping key), `steps`, a list of
  `ThreadStep(label, variant, saved_at, annual_return, annual_volatility, sharpe, dividend_yield,
  changed)` in `saved_at` order, where `changed` is the per-figure difference against the previous
  step and the set of tickers that entered and left the weights between them.
- `Consensus`: only computed when at least two portfolio reports share one candidate pool.
  `candidates`, `always_held` (tickers with a non-zero weight in every run), `sometimes_held`
  (ticker to the list of objectives that held it, with the weight), `never_held` (candidates absent
  from every run), and `objective_specific` (the tickers held under exactly one objective, with
  that weight). A ticker counts as held when the body printed a weight for it at all, because the
  report only prints the tickers it allocated; but a weight below `0.0050` is additionally marked
  negligible in the render, because the real MV-at-0.1000 run printed `TLT: 0.0005` and allocated
  it one single share, and calling that "held under two objectives" would overstate what happened.
  Against the four real portfolio reports this yields `always_held` of `AMLP, BOXX, ENFR, GOOGL,
  MLPX, QQQI, T`, `never_held` of `CSPX.L, SPY, TSM, VUAA.L, VZ`, `BIL` in three runs but not MSR,
  `NVDA` in three but not GMV, and `PFF` under GMV alone at `0.0935` despite its expected return
  over this window being negative - which is the kind of thing the consensus section exists to
  surface.
- `BookComparison`: only computed when a `whatif` baseline and at least one portfolio report share
  a currency. The baseline's figures, the best-Sharpe portfolio row in the same currency, the
  benchmark row, and the gaps between them - plus, explicitly, whether they share a returns window,
  because the gap is only meaningful if they do.
- `ScenarioLedger`: for each whatif variant, its label, its figures, its parsed deltas, and the
  positions added and removed relative to the baseline it is compared against (derived from the
  `positions` facts, which both carry). Pair each variant with the baseline **in its own
  partition** - same currency and same returns window - and not merely with "the baseline". The
  real archive proves why: the 48-month variant's printed
  `Change from your saved portfolio: return +0.0359` is measured against the 48-month baseline
  (0.0713), while the 60-month variant's `+0.0269` is against the 60-month baseline (0.0229).
  Pairing across windows would make both delta lines wrong. If a partition has no baseline, the
  ledger entry keeps the report's own printed deltas and says which baseline it could not find.
- `Comparability`: the distinct windows seen and which labels fall in each; any currency whose
  reports disagree on `risk_free_rate`; any partition holding a single row (so its ranking says
  nothing); and the window-sensitivity pairs - two records with identical `positions` or identical
  `candidates`-plus-`objective` but different windows, with both sets of figures. This section is
  the one the summary exists to make visible, so build it from the data and never from a constant.
- `MonthDigest`: all of the above plus `sources_digest`.

Add `build_month_digest(records, month, skipped) -> MonthDigest` that fills them in, and
`render_digest(digest) -> str` that renders the whole thing as the Markdown of the transcript above:
a scope paragraph, the partitioned leaderboard tables, the thread walk-throughs, the consensus
lists, the book comparison, the scenario ledger, and the comparability notes. Tables are
space-aligned plain text inside the Markdown, matching how every other report in this project
prints - not pipe tables. A section with nothing to say prints one `n/a` line naming the reason
("no portfolio report in this month shares a candidate pool with another, so there is nothing to
compare"), never a silent omission, exactly as the ticker summary in
`plans/17_ticker_performance_summary.md` degrades.

Add `digest_for_llm(digest) -> str`, a second, compact rendering of the same object for the agent's
task input - the figures and lists with no table alignment and no prose, capped so a month with two
hundred reports does not exceed a small model's context: at most the top twelve rows per partition,
at most twenty tickers per consensus list, at most twelve threads and twelve ledger entries, each
cap noted in the text when it truncates.

Write `tests/test_report_summary.py`, hermetic, using `tmp_path`, a module docstring stating why it
touches no network and no LLM, and module-level helper functions rather than fixtures, matching
`tests/test_report_archive.py`. Build inputs by writing report files with
`report_archive.save_report` so the tests exercise the real front-matter format rather than a
hand-rolled imitation. Cover: `load_month` on a missing folder and on an empty one; a `kind:
summary` file and an unknown kind both skipped with a note; a file without a fence skipped with a
note rather than raising; records sorted by `saved_at`; `parse_weights` on a real portfolio body,
on a whatif body (empty tuple), on a body whose block is missing, and on a block followed
immediately by another section; `parse_benchmark` on a portfolio body, on a whatif body (`None`)
and on a body whose benchmark line is absent; `parse_whatif_deltas` on a variant body, on a
baseline body (`{}`), and on the negative-income line; `sources_digest` stable under reordering and changed by an added
report; partitioning putting the 60-month and 48-month reports in separate tables; ranking by
Sharpe with a `None` sharpe last; `always_held`/`never_held` against the four real portfolio
weight sets; `Consensus` returning its `n/a` reason when only one portfolio report exists;
`BookComparison` absent when the month has no whatif baseline; `Comparability` naming the
window-sensitivity pair when two reports share `positions` but not `window_months`; a month holding
two currencies producing two partitions and never mixing them; and `render_digest` on a
whatif-only month not raising and printing the consensus section's `n/a` reason.

Verify by hand from the repository root:

    uv run pytest tests/test_report_summary.py -q
    uv run python -c "from src.flow.report_summary import *; r, s = load_month('output', '2026-09'); print(render_digest(build_month_digest(r, '2026-09', s)))"

The second command must print a briefing whose leaderboard ranks MSR at 2.0207 first and the held
book at -0.1253 last within the 60-month partition, lists `AMLP, BOXX, ENFR, GOOGL, MLPX, QQQI, T`
as always held, `CSPX.L, SPY, TSM, VUAA.L, VZ` as never held, splits the leaderboard into a
60-month partition of seven rows and a 48-month partition of two, and names **both**
window-sensitivity pairs under comparability: `PFF:6000, PFFA:6000, VZ:2000` at
`0.0229 / -0.1253` over 60 months against `0.0713 / 0.2808` over 48, and `PFFA:6000, VZ:2000` at
`0.0498 / 0.0813` against `0.1073 / 0.4740`.

### Milestone 2 - the agent

At the end of this milestone a crew can turn Milestone 1's compact digest into prose, and a test
proves the driver's plumbing without any LLM call.

In `src/config/settings.py`, add beside `llm_s_model` (line 78):

    llm_quick: str = "openai/gpt-5-nano"
    """Model used by the cheap, high-volume agents - currently only the
    monthly report summarizer in `src/agents/report_summary.py`. Override
    with LLM_QUICK, or per run with `--model` on `portfolio-summary`."""

and add `openai_api_key: str | None = None` beside `anthropic_api_key`, with the same
documentation-only comment: nothing in `src/` reads it, LiteLLM under CrewAI picks it up from the
process environment. Extend the module docstring's list of what it centralizes.

Create `src/agents/summary_schema.py` with one Pydantic model, every field a `str` or a
`list[str]`, and every field carrying a `Field(description=...)` that tells the model what the
field is for and what NOT to put in it - matching `src/agents/llm_f_schema.py`, where every field
of every model is described and the module docstring explains the LLM/arithmetic split. Those
descriptions reach the model as the output schema, so they are the second half of the
no-arithmetic instruction and not documentation:

    class MonthNarrative(BaseModel):
        headline: str                 # one sentence, the month's single most useful finding
        exploration_story: str        # 2-4 sentences: what was explored and in what order
        risk_return_read: str         # 2-4 sentences interpreting the leaderboard
        income_read: str              # 2-3 sentences on the dividend-yield spread
        holdings_gap_read: str        # 2-4 sentences on the held book vs the frontier; "" if none
        consensus_read: str           # 2-4 sentences on always/never-held tickers; "" if none
        methodology_caution: str      # 2-3 sentences on the comparability notes
        next_runs: list[str]          # 2 to 5 entries, each one runnable command plus a reason

Create `src/agents/summary_crew/__init__.py`, `src/agents/summary_crew/crew.py`,
`src/agents/summary_crew/config/agents.yaml` and
`src/agents/summary_crew/config/tasks.yaml`, copying the shape of `src/agents/llm_f_crew/` exactly:
a `@CrewBase class ReportSummaryCrew` whose `__init__(self, model: str)` stores only the model, an
`@agent summary_agent()` returning `Agent(config=self.agents_config["summary_agent"],
llm=self.model, verbose=True)`, an `@task summary_task()` returning
`Task(config=self.tasks_config["summary_task"], output_pydantic=MonthNarrative)`, and an
`@crew crew()` returning `Crew(agents=self.agents, tasks=self.tasks, process=Process.sequential,
verbose=True)`.

`agents.yaml` gives the agent the role of a portfolio research archivist whose goal is to explain
what a month of saved optimizer and holdings reports means to the person who ran them. Its
backstory must carry the single constraint that makes this design work, stated as a rule about its
own output: every figure it writes must be copied character-for-character from the facts it was
given; it must never add, subtract, average, rank or round a number, because the ranking and the
arithmetic were already done before it was called and its job is only to say what they mean. It
must write `""` for a section whose facts were not supplied rather than inventing one.

`tasks.yaml` interpolates `{month}`, `{report_count}` and `{facts}` - the last being Milestone 1's
`digest_for_llm` output. The description restates the no-arithmetic rule, says the facts block is
the only source of truth and that anything absent from it does not exist, and asks for each
`next_runs` entry to name a real flag of `uv run portfolio` or `uv run portfolio-holdings whatif`.
The `expected_output` names the `MonthNarrative` fields and their length limits.

Create `src/agents/report_summary.py` with
`generate_narrative(month, report_count, facts, model=None) -> MonthNarrative`, following
`src/agents/llm_f.py:93` line for line: resolve `model or settings.llm_quick`, build
`ReportSummaryCrew(model=resolved_model)`, `kickoff(inputs={...})`, return `result.pydantic`. Read
the model from `settings.llm_quick` and not from `os.environ`, matching `src/agents/llm_s.py:74`
rather than `src/agents/llm_f.py:110`.

Two failure paths must be explicit here rather than left to a traceback. If the resolved model
starts with `openai/` and `OPENAI_API_KEY` is absent from the environment, raise a
`ValueError` naming the variable, the model, and `--no-llm` as the alternative - checked before the
crew is built, so nothing is charged and nothing half-runs. And wrap the `kickoff` so any exception
from the provider is re-raised as a `ValueError` that names the model and points at `--no-llm`; the
CLI turns that into a graceful degradation in Milestone 3.

One live-model unknown to settle while implementing: the `gpt-5` family, reached through LiteLLM,
rejects a `temperature` other than `1` and uses `max_completion_tokens` rather than `max_tokens`.
CrewAI's bare `llm="openai/gpt-5-nano"` shorthand may or may not send a default `temperature`. Try
the shorthand first, because both existing crews in this repo use it; if the provider returns an
unsupported-parameter error, switch that one line to `crewai.LLM(model=self.model, temperature=1)`
and record the exact error text in `Surprises & Discoveries`. Note that `CREWAI.md`'s LLM
Configuration section recommends `crewai.LLM` unconditionally while this repo's two crews use the
shorthand; consistency with the repo wins unless the provider forces otherwise.

Write `tests/test_report_summary_agent.py` with the three-level fake crew copied from
`tests/test_llm_f.py:205-231` and installed as
`monkeypatch.setattr(report_summary, "ReportSummaryCrew", lambda model: _FakeCrewHolder(...))`.
Cover: the resolved model defaulting to `settings.llm_quick`; an explicit `model` argument winning;
the `inputs` dict carrying `month`, `report_count` and `facts` verbatim; `result.pydantic` returned
unchanged; the missing-`OPENAI_API_KEY` `ValueError` naming the variable and firing before the
crew is constructed (assert with a fake whose constructor raises); and a provider exception
surfacing as a `ValueError` naming the model. Use `monkeypatch.delenv("OPENAI_API_KEY",
raising=False)` and `monkeypatch.setattr(settings, "llm_quick", ...)` so no test depends on the
developer's `.env`.

Verify: `uv run pytest tests/test_report_summary_agent.py -q`.

### Milestone 3 - the command

At the end of this milestone `uv run portfolio-summary` works end to end, saves its output, and
recognizes a repeat.

Create `src/flow/summary_cli.py` with a `main()` built with plain `argparse` and no subparsers,
matching `src/flow/holdings_cli.py:854`:

- a positional `month`, `nargs="?"`, default `None` meaning the current calendar month, accepted as
  `YYYY-MM` and rejected through `parser.error` otherwise (exit 2);
- `--output-dir`, default `settings.output_dir`, worded as on the other two commands;
- `--model`, default `None`, documented as overriding `LLM_QUICK` for this run;
- `--no-llm`, `store_true`, print the computed sections and omit the prose;
- `--stdout`, `store_true`, print and save nothing;
- `--force`, `store_true`, write a new summary even when one already covers these reports.

`main()` resolves the month, calls `load_month`, and exits 1 with a clear message naming the folder
when the month holds no readable source report - `"No reports to summarize in output/2026-10/
(looked for kind: portfolio or kind: whatif)."` - because a summary of nothing is not a summary.
Otherwise it prints the `Reading ...` line including any skip notes, builds the digest, and unless
`--force` or `--stdout` scans the month folder for an existing `kind: summary` file whose
`sources_digest` matches, printing the already-saved notice and exiting 0 if it finds one. Then,
unless `--no-llm`, it calls `generate_narrative`, catching `ValueError` to print a one-line warning
naming the reason and continuing without prose - an unreachable model must not cost the reader the
figures.

Add `render_summary(digest, narrative) -> str` in the same module, interleaving Milestone 1's
rendered sections with the narrative fields in the order shown in the Purpose transcript, and
emitting `"(no narrative: <reason>)"` in place of each prose section when `narrative` is `None`.
Keeping this in the CLI module and not in `report_summary.py` keeps the deterministic module free
of any awareness of the agent.

Save with the existing archive writer rather than a new one: build
`ReportArchive(output_dir=args.output_dir, enabled=True, kind="summary", as_of=max(r.as_of for r in
records), command=command_line())` and call `save_report(body, archive, facts)` where `facts`
carries `month`, `report_count`, `sources_digest`, `llm_model` (or `None` under `--no-llm`) and
`narrative_status` (`"written"`, `"skipped"` or `"failed: <reason>"`). Using `max(as_of)` puts the
file in the month folder it describes even when the command is run in a later month. Print
`format_save_notice`'s line. Two constraints on the rendered body follow from how the archive
stores it: it must never contain a line that is exactly `---`, because a Markdown horizontal rule
inside the body would read as a front-matter fence to anything less careful than `load_report`
(use a blank line or a heading to separate sections instead); and because `save_report` derives the
filename from a digest of the body, a `--force` run whose prose differs gets a new filename while
one whose prose came back byte-identical collapses onto the existing file and reports
`created=False`, which is correct behaviour rather than an error.

Add those five keys to `FACT_ORDER` in
`src/flow/report_archive.py` after `kind` so the order stays stable; they are additive and cannot
change any existing file, since digests cover the body only.

Register the script in `pyproject.toml` under `[project.scripts]`:

    portfolio-summary = "src.flow.summary_cli:main"

`tests/conftest.py`'s autouse fixture already covers this command without change, because
`main()` reads `settings.output_dir` at parse time as its `--output-dir` default exactly as the
other two do - so no test can read or write the repository's real `output/`. Amend that file's
docstring, which currently names two entry points, to name three.

Write `tests/test_summary_cli.py` in the style of `tests/test_cli.py`: argv through
`monkeypatch.setattr("sys.argv", ...)`, output through `capsys`, every path under `tmp_path`,
`generate_narrative` monkeypatched at `src.flow.summary_cli.generate_narrative` to return a
hand-built `MonthNarrative` so no test calls an LLM. Cover: a month of two saved reports summarized,
one file written, the notice naming it; the same run repeated writing nothing and printing the
already-saved notice; `--force` writing a second file; `--stdout` writing nothing and creating no
directory; `--no-llm` never calling the patched `generate_narrative` and printing the
`(no narrative: ...)` markers; `generate_narrative` raising `ValueError` producing a warning plus a
complete figures-only document and exit 0; an empty or missing month exiting 1 with the folder
named; a malformed month argument exiting 2; the default month being today's; the saved file's
front matter carrying `kind: summary`, `month`, `report_count` and `sources_digest`; and a second
`portfolio-summary` run over a folder that already holds a summary counting the same source reports
as before, proving the `kind: summary` skip.

Verify: `uv run pytest tests/test_summary_cli.py -q`.

### Milestone 4 - documentation

Add a bullet to `README.md` beside the report-archive bullet at line 68, describing what
`uv run portfolio-summary` produces, that every figure in it is computed rather than written by the
model, that the leaderboard is split by returns window and why, that a repeat is recognized by the
set of reports it covers, and that `--no-llm` is the offline mode. Add `LLM_QUICK` to the
Configuration list at lines 14-27 with its default and its purpose, and `OPENAI_API_KEY` beside
`ANTHROPIC_API_KEY`.

### Milestone 5 - end-to-end and the full suite

Run the real thing against the real archive and paste the transcript into `Artifacts and Notes`:

    uv run portfolio-summary 2026-09 --no-llm --stdout
    uv run portfolio-summary 2026-09
    uv run portfolio-summary 2026-09
    uv run portfolio-summary 2026-10
    git status --short

Expect: the first prints the figures-only briefing and writes nothing; the second adds one file and
names it; the third writes nothing and says so; the fourth exits 1 naming `output/2026-10/`; and
`git status` stays clean, because `output/` is gitignored. Confirm the saved file's numbers against
the nine source files by eye - in particular that the leaderboard's MSR row reads
`0.2513 / 0.1056 / 2.0207` and the held-book row `0.0229 / 0.1207 / -0.1253`, and that no figure in
the agent's prose contradicts the tables.

Then the full suite, in the background because it takes about thirteen minutes:

    uv run pytest tests/test_*.py

Expect the 927-test baseline plus the tests added here, all passing. A runtime materially above
about 460 seconds means something in the new code is reaching the network and must be fixed, not
accepted.


### Milestone 7 - the translated briefing


At the end of this milestone `uv run portfolio-summary --language zh-TW` saves a second file beside
the English one: the same briefing with its sentences in Traditional Chinese and every table, every
figure and every ticker byte-identical to the English original.

A note on paths before anything else. This plan was written before
`plans/20_packaging_and_release.md` moved the package, so every `src/flow/...` and `src/agents/...`
path above now reads `src/agentic_portfolio/flow/...` and `src/agentic_portfolio/agents/...`. This
milestone and the next use the current paths throughout.

**The shared splitter.** Create `src/agentic_portfolio/flow/briefing_blocks.py`, standard library
only, no `crewai` and no `pydantic`, with a module docstring saying it is shared by the translator
and the PDF renderer and that it makes no LLM call. Define a `Block` named tuple of `kind` and
`text`, where `kind` is one of `heading`, `paragraph`, `bullet`, `preformatted` and `blank`, and
`split_briefing(body) -> tuple[Block, ...]` plus `join_briefing(blocks) -> str`.

The classification rule is short because `render_digest` is the only thing that produces this text.
A line beginning `#` is a heading. A line beginning `- ` at column zero is a bullet. **Any line
indented two or more spaces is preformatted**, and contiguous preformatted lines group into one
block - including a blank line between two indented runs, so a window-sensitivity subject and the
4-space table beneath it stay together. Everything else is a paragraph. Against the real saved
briefing that rule classifies every line: 95 preformatted, 31 paragraph, 9 heading, 4 bullet, 49
blank. `join_briefing(split_briefing(text)) == text` is a round-trip law, it deserves its own test,
and it is what makes reassembly safe rather than hopeful.

**Token protection.** In the same module, add
`protect_tokens(text, literals) -> tuple[str, tuple[str, ...]]` and
`restore_tokens(text, tokens) -> str`. `protect_tokens` replaces each protected span with a
placeholder the model is told to copy, and returns the masked text alongside the spans in order.
Protected spans are the `literals` it is given, matched with `(?<![0-9A-Za-z])` and
`(?![0-9A-Za-z])` on both sides - single-letter tickers exist, `T` is one, and an unanchored match
turns `WITHIN` into a masked fragment - plus five narrow patterns: an ISO date, a figure (the same
`[0-9][0-9,]*(?:\.[0-9]+)?` run the narrative verifier uses), a hex run of sixteen or more, a
`*.md` filename, and a `uv run ...` command head. Adjacent spans separated by at most one space
merge into one placeholder, so a whole command line is one token rather than a dozen. A trailing
dot is excluded from the provider-name pattern, or `Prose: openai/gpt-5-nano.` loses its sentence
period.

The literals come from the month's own data, not from a regex over capital letters. Add
`protected_literals(digest, records) -> tuple[str, ...]` to
`src/agentic_portfolio/flow/report_summary.py`, which already holds all of it: every ticker from
`candidates`, `positions` and the parsed weights, every currency, objective, benchmark and
`label`, every source filename, every report digest, the `sources_digest`, every `command` string
and every window date. A `[A-Z]{2,}` rule was the obvious alternative and is wrong - it protects
`CAUTION`, `WITHIN` and `SAME`, which are English words this milestone exists to translate.

Nothing moves out of `src/agentic_portfolio/agents/report_summary.py`. An earlier draft of this
milestone moved `_FIGURE` and `stated_figures` into the flow layer so that a module without
`crewai` could reuse them; that is not needed and would cost something. See the `Decision Log`
entry for why it was specified and then dropped.

**The schema.** Create `src/agentic_portfolio/agents/translation_schema.py` with
`TranslatedBlock(index: int, text: str)` and `TranslatedBriefing(blocks: list[TranslatedBlock])`,
plus a short `disclaimer` field holding one sentence IN THE TARGET LANGUAGE saying the document is
a machine translation whose tables were not translated. Every field carries a
`Field(description=...)`, because a description reaches the model as its output schema and is
therefore half the instruction rather than documentation of it - the same reasoning
`src/agentic_portfolio/agents/summary_schema.py` records. The index is explicit rather than
positional on purpose: a model that drops one block must not shift every block after it.

**The crew.** Create `src/agentic_portfolio/agents/translate_crew/` with `__init__.py`, `crew.py`
and `config/agents.yaml` plus `config/tasks.yaml`, copying the shape of `summary_crew/` exactly: a
`@CrewBase class ReportTranslationCrew` whose `__init__(self, model: str)` stores only the model,
an `@agent translator_agent()` and `@task translation_task()` built through `require(...)` from
`src/agentic_portfolio/agents/crew_config.py`, `output_pydantic=TranslatedBriefing`, `verbose=False`
for the same reason this command's other crew uses it, and `llm=self.model` as a bare
`provider/model` string rather than a `crewai.LLM` - see this plan's existing decision on that.
`tasks.yaml` interpolates `{language}`, `{month}`, `{block_count}` and `{blocks}`.

`agents.yaml` gives the agent the role of a financial report translator, and its backstory carries
the one constraint that makes the design work, stated as a rule about its own output: you are
translating a document whose numbers are already correct and already printed in tables your
sentences will sit beside; every placeholder you are given is copied character for character and
never translated, reordered or explained; you translate only the words around them; you return one
entry per block index you were given and nothing else.

**The driver.** Create `src/agentic_portfolio/agents/report_translation.py` with
`TranslationUnavailable`, `verify_translation(sources, translated)` and
`translate_briefing(body, language, digest, records, model=None) -> tuple[str, tuple[str, ...]]`,
following `generate_narrative` in this order, so that every refusal happens before anything is
charged: refuse a blank `language`; split and mask; refuse a document with more translatable blocks
than the cap, naming chunking as the thing that is not implemented; resolve
`model or settings.llm_quick`; call `api_key_problem` BEFORE the crew is built; kickoff, wrapping
any provider exception as `TranslationUnavailable` naming the model; refuse a result with no
`pydantic` payload.

`verify_translation` is where the guarantee lives, and it is a stricter test than
`verify_narrative`'s: **the ordered list of placeholders in a translated block must equal the list
in its source.** Once every figure, ticker, date, digest and command is an opaque token, equality
is exact and cheap, and one rule catches a dropped ticker, a localized date and a mangled digest
alike. A block that fails, or that the model omits entirely, keeps its English text and is counted.
Then two whole-document gates, each raising with a sentence naming the reason: if fewer than four
fifths of the blocks were accepted, abandon the translation, because a document that is one fifth
English is worse than one that is honestly all English; and if fewer than half the accepted blocks
actually differ from their English source, abandon it as well, since a small model echoing its
input back would otherwise pass every placeholder check and produce an English file labelled
`language: zh-TW`. Script detection cannot substitute for that second gate, because `--language`
is free-form and the target script is unknown.

Reassembly is `restore_tokens` then `join_briefing`. The body then gains the model's disclaimer in
the target language and one deterministic English audit line naming the model, the language, the
file translated from, and how many blocks stayed English - two languages deliberately, because the
target-language reader needs to know it is machine-translated and a reviewer who reads only English
needs the counts.

**The command.** In `src/agentic_portfolio/flow/summary_cli.py` add `--language`, documented in this
command's established voice: free-form, a BCP-47 tag or a plain language name, the English summary
always written first, every figure and table copied from it unchanged, and any sentence whose
placeholders do not survive left in English rather than guessed at. `--language` with `--no-llm` is
a contradiction and is refused through `parser.error` naming both flags, exit 2, since `--no-llm`
promises no network call at all. Give `existing_summary` a `language` parameter defaulting to
`None`, and match `(facts.get("language") or None) == (language or None)` so that an absent fact
means English and the two summaries already in `output/2026-09/` keep being recognized.

The orchestration has three cases and the middle one is the valuable one. If neither file exists,
run the prose pass, render, save the English, translate, save the translation. If the English
exists but the translation does not, make **no prose call**: read the saved English body back with
`load_report` and translate that, recording `translated_from` as its digest - so `--language` on an
already-summarized month is cheap, idempotent, and provably a translation of the bytes on disk. If
both exist, print both already-saved notices and write nothing. `--force` rewrites both.

A translation failure degrades exactly as `NarrativeUnavailable` does: warn on stderr, print and
save the English, exit 0. A failed translation must not cost the reader the briefing.

**The archive.** In `src/agentic_portfolio/flow/report_archive.py` add `language`,
`translation_model`, `translated_from` and `translation_status` to `FACT_ORDER` after
`narrative_status`, so the summary group reads in the order a person asks the questions. Add
`language` to `report_filename`'s token loop, giving
`2026-09-11-summary-zh-TW-91c40e7b.md`. Say in the docstring that this token is for human
legibility and NOT a uniqueness mechanism - the name already ends in a digest of the body, and two
languages cannot produce one body - so that nobody later mistakes it for one. All of this is
additive: a key absent from a report's facts is omitted from the file, and digests cover the body
alone, so no existing file or call site changes.

Add `translate_crew` to `CREW_PACKAGES` in `tests/test_packaging.py`. That is the guard which
catches a prompt YAML missing from the wheel, and a new crew that skips it ships broken.

Write `tests/test_briefing_blocks.py` and `tests/test_report_translation.py`, and extend
`tests/test_summary_cli.py`, `tests/test_report_archive.py` and `tests/test_packaging.py`. Build
fixtures by writing real reports with `save_report` and rendering them with `render_digest`, per
this plan's own lesson that a hand-rolled imitation tests a smaller month than it claims. Cover at
least: the round-trip law; every indented line landing in a `preformatted` block and none in a
paragraph; a blank line inside an indented run not splitting it; `protect_tokens` leaving `WITHIN`
intact while masking the ticker `T` where it stands alone; a command line masked as ONE placeholder;
`Prose: openai/gpt-5-nano.` keeping its period; a block whose placeholders were reordered rejected;
a block the model omitted keeping its English; the coverage and echo gates each abandoning a
translation with a named reason; no line of a translated briefing being exactly `---`; an English
summary not blocking a translation of the same reports and vice versa; `--language` on an
already-summarized month translating without a prose call, asserted with a fake
`generate_narrative` that fails if called; and a translated file not being read back as a source
report.

Verify:

    uv run pytest tests/test_briefing_blocks.py tests/test_report_translation.py -q
    uv run pytest tests/test_summary_cli.py tests/test_report_archive.py tests/test_packaging.py -q


### Milestone 8 - the PDF


At the end of this milestone `uv run portfolio-summary --pdf` writes a PDF beside every Markdown
file the run saved, with the tables still in columns, and refuses - by name - to write a Chinese PDF
on a machine with no Chinese font.

**Why there is no Markdown library here.** `render_digest` indents its tables by two spaces and
pads them with `str.ljust`. CommonMark needs four spaces to make a code block, so a generic parser
reads every table as prose and HTML collapses the padding that is the table. A library would
therefore need a pre-processor that already applied the "an indented run is preformatted" rule, at
which point the pre-processor is the converter. Milestone 7's `split_briefing` already is that rule,
so the conversion is the four remaining constructs and no new dependency.

Create `src/agentic_portfolio/flow/report_pdf.py` with `PdfUnavailable`, a `RenderedPdf` named tuple
of `path` and `created` mirroring `SavedReport`, `format_pdf_notice(rendered)` mirroring
`format_save_notice`, and:

    def contains_cjk(text: str) -> bool
    def display_width(line: str) -> int
    def pre_font_size(width: int) -> float
    def cjk_font_family(families: Sequence[str] = CJK_FAMILIES) -> str | None
    def briefing_html(facts: Mapping[str, str], body: str, *, source_name: str) -> str
    def stylesheet() -> str
    def pdf_path(md_path: Path | str) -> Path
    def write_pdf(md_path: Path | str) -> RenderedPdf | None

`briefing_html` walks `split_briefing`'s blocks and emits two heading levels, paragraphs, one
unordered list and a `<pre>` per indented run, escaping with `html.escape` and touching nothing
inside a preformatted block. It sets the document language so WeasyPrint picks the right font and
line-breaking rules, and emits one small provenance paragraph naming the file, the save time, the
source-report count and the prose model - which `string-set` also pins into every page footer, so a
printed page always says which file it came from. It deliberately does not reproduce all fifteen
front-matter facts: that is a page of machine metadata in a document made for reading, and it is one
`cat` away in the Markdown.

Sizing is arithmetic, not taste. Usable width on A4 portrait with 15mm margins is 510pt and DejaVu
Sans Mono advances 0.602em, so `pre_font_size` picks the largest size at which a block's widest line
fits, clamped between 6.5pt and 9pt, and `display_width` counts wide and fullwidth characters as two
via `unicodedata.east_asian_width`. Each block is sized from its OWN widest line: the briefing's
widest indented line is 183 characters, in the every-window list, and sizing the whole document to
fit that puts the leaderboard at about 5.5pt - unreadable, in order to protect a prose list that
happened to be indented.

`write_pdf` takes a PATH and re-reads it with `load_report`, rather than taking the body this run
rendered. That is what makes the PDF provably match the saved bytes, what makes `--pdf` work on the
already-saved path where no body was rendered, and what lets one flag serve a translated file whose
text this module never saw. If the PDF already exists it returns `created=False` and renders
nothing. Otherwise it writes to a `.report-*.pdf` temporary in the destination directory and
`os.replace`s it, copying `_write_atomically`'s shape including its cleanup on any `BaseException`.

Create `src/agentic_portfolio/flow/report_pdf.css` as package data, read with
`importlib.resources.files` - the pattern `tests/test_packaging.py` already defends and the reason
the six crew YAML files ship at all. It now uses A4 portrait with 18mm vertical and 15mm side
margins: retaining 15mm sides preserves the 510pt table-width calculation. Its navy/blue palette,
centered title, larger section hierarchy, CJK-aware proportional prose and pale code blocks are
adapted from the repository-root `report.css`, which is a design reference for a different HTML
producer rather than a runtime asset. The `h1` supplies a named string repeated at top right; the
footer retains the source at bottom left and `page / pages` at bottom right. DejaVu Sans Mono stays
first for tables because their arithmetic is calibrated to its advance width. `h2` retains
`break-after: avoid`, and a `lead` class keeps a table caption with its table without inventing
emphasis the Markdown does not state. `pre` uses `white-space: pre-wrap` with a hanging indent, and
that is load-bearing rather than cosmetic: under plain `white-space: pre` an over-wide line is laid
out in a single box wider than the paper and simply runs off it, with no warning from WeasyPrint at
all. Wrapping misaligns the tail of one line; the alternative loses it.

Failure handling, all of it raising `PdfUnavailable`, caught once in the CLI and printed as a
warning on stderr with the exit status unchanged - the Markdown is the product and the PDF is a
rendering of it. WeasyPrint absent or its system libraries missing is an `ImportError` or an
`OSError` from `dlopen`, and the message names `libpango-1.0-0` and friends. An unwritable path is
an `OSError` naming it. A file that is not a report this archive wrote is named too.

The one exception to degrading is the font check, and it goes the other way. If the document
contains CJK text and `cjk_font_family()` finds no CJK family, `--pdf` REFUSES before rendering,
naming the font package to install and the Markdown file that is complete regardless. A refusal
leaves the reader a readable document; a warning leaves them a four-page PDF of empty boxes that
looks finished, and that is the artifact that gets emailed. The check asks fontconfig through
WeasyPrint's own cffi handle, so it needs no new distribution and no `fc-list` binary.

**The command.** Add `--pdf` to `src/agentic_portfolio/flow/summary_cli.py`, have `_write` return
its `SavedReport`, and add a `_render_pdf(path)` sibling that never raises. Call it after each save
and once on the already-saved path, gated on `args.pdf`. `--pdf` with `--stdout` is a contradiction
and is refused through `parser.error` naming both flags, exit 2, because `--stdout` promises to save
nothing and there is then nothing to render beside. `--pdf` is otherwise orthogonal to `--no-llm`
and composes with `--language` by rendering one PDF per file saved.

**The images.** Extend the runtime stage of the root `Dockerfile` with `libpango-1.0-0`,
`libpangoft2-1.0-0`, `libharfbuzz0b`, `libharfbuzz-subset0`, `fonts-dejavu-core` and
`fonts-noto-cjk`, and `docker/Dockerfile.dev` with the two font packages - its Pango libraries are
already there. Comment both in those files' established why-not-what voice, including that no cairo
is needed because WeasyPrint has written PDF itself since v53, that a missing font is silent, and
that `fonts-noto-cjk` is the largest single addition to the release image and is what makes a
Chinese briefing readable rather than a page of boxes. In `pyproject.toml`, correct the now-stale
"exactly the nine distributions" comment, record that `weasyprint` is imported by this module, and
say that `markdown` was deliberately NOT brought back when `weasyprint` arrived, with the reason
above - a lock file cannot express that this dependency's SYSTEM libraries are part of the contract,
so the comment must.

Write `tests/test_report_pdf.py` and extend `tests/test_summary_cli.py` and
`tests/test_packaging.py`. These tests invoke WeasyPrint for real: it is a declared dependency, it
is installed, it touches no network, and the only interesting question - does WeasyPrint accept this
HTML and this CSS - is precisely what a fake cannot answer. One render of the real briefing was
measured at 0.11 to 0.14 seconds, so this adds about a second to a seven-minute suite, and a
runtime rise here would be a real signal rather than noise. The two degradation paths monkeypatch
this module's two named seams, the same way `tests/test_summary_cli.py` patches
`generate_narrative` by its imported name.

Cover at least: the exact substring `"  rank  what                       return"` appearing inside a
`<pre>` in the HTML, which is the assertion this whole milestone exists for; no line starting with a
space landing in a paragraph, checked over a real `render_digest` output; a blank row inside an
indented block not splitting it; HTML metacharacters escaped; a narrow table keeping the largest
size and a 117-character one shrinking to fit 510pt; a 183-character line clamped AND the stylesheet
containing `pre-wrap`; `display_width("台灣") == 4`; the bytes starting `%PDF-` and the real briefing
paginating to more than one page, so nothing was dropped into one overflowing page; an existing PDF
left alone with `created=False`; two Markdown files in one folder getting two PDFs, which is the
translation case; a missing renderer degrading with a message naming `libpango-1.0-0` while the
Markdown is untouched; a CJK document with no CJK font refusing with a message naming
`fonts-noto-cjk` and writing no file; `--pdf --stdout` exiting 2 and saying which flag to drop; a
repeat run with `--pdf` rendering beside the summary already saved; a failed PDF keeping the
briefing and warning; and a `.pdf` in the month folder not being read as a report and not even
producing a skip note.

Verify:

    uv run pytest tests/test_report_pdf.py -q
    uv run pytest tests/test_summary_cli.py tests/test_packaging.py -q


### Milestone 9 - adapt the preferred print style

This milestone refines the appearance of the already-delivered PDF without changing its Markdown,
CLI or Python interfaces. Edit only the packaged `src/agentic_portfolio/flow/report_pdf.css`; leave
the untracked root `report.css` untouched as the design reference. Carry over its navy/blue palette,
centered title, readable spacing, CJK-aware proportional typography and pale code treatment. Use
10pt body type at 1.6 line height, a centered 21pt title, 13.5pt navy section headings with a 1.2pt
blue rule, and A4 margins of `18mm 15mm 18mm`. Set the `h1` as `report-title` and repeat it in a
muted 7.7pt top-right page header. Keep the centered provenance below the title, repeat it at bottom
left, and keep `page / pages` at bottom right.

Do not copy selectors for semantic tables, subtitles, callouts, images or deeper headings: the
closed converter does not emit them. Do not change `USABLE_WIDTH_PT`, the DejaVu-first monospace
stack, per-block inline sizes, `pre-wrap` or the hanging indent. Shade each `<pre>` without adding
horizontal padding beyond that existing indent, so the width calculation remains true. Add tests
that pin the visual contract and inspect WeasyPrint's actual margin boxes on every page. Acceptance
is that the focused PDF and packaging suite passes and both real-language briefings stay within the
680.3px content area.


## Validation and Acceptance

Acceptance is the Purpose transcript reproduced: from `/app/agentic_portfolio`,
`uv run portfolio-summary` reads `output/<current month>/`, prints a briefing whose leaderboard is
partitioned by returns window and ranked by Sharpe within each partition, names the tickers every
optimizer run held and the candidates none did, states the held book's figures against the best
portfolio of the month, warns where two reports are not comparable, ends with two to five runnable
next commands, and names the file it saved. Running it again prints the already-saved notice and
writes nothing. `--no-llm` produces the same document minus the prose and makes no network call of
any kind.

Per-milestone: `uv run pytest tests/test_report_summary.py tests/test_report_summary_agent.py
tests/test_summary_cli.py -q` passes; each of those three files fails before its milestone and
passes after. `uv run pytest tests/test_*.py` stays green.

Acceptance for Milestone 7 is one comparison, and it is the whole point of the design:
`uv run portfolio-summary 2026-09 --language zh-TW` saves a second file whose headings and
paragraphs are Traditional Chinese and whose tables are byte-identical to the English one, so

    diff <(grep '^  ' <english>.md) <(grep '^  ' <chinese>.md)

produces no output. The document's last lines name the model, the language, the English file it was
translated from, and how many blocks stayed English. Running it again writes nothing and names both
files. `--language` with `--no-llm` exits 2 naming both flags.

Acceptance for Milestone 8 is that `uv run portfolio-summary 2026-09 --pdf` writes a file beginning
`%PDF-` beside each saved briefing, that the real briefing paginates to more than one page rather
than one overflowing page, and that its tables are still in columns in a monospace face rather than
reflowed into prose. `--pdf --stdout` exits 2 naming both flags. In a container with no CJK font
installed - which is the state of this one - a Chinese document is REFUSED by name, and that
refusal firing is itself an acceptance criterion rather than a defect: it is the difference between
a known limitation and a silent page of empty boxes. Installing `fonts-noto-cjk` is how to see the
finished article.


## Idempotence and Recovery

Every step is safe to repeat. Re-running `portfolio-summary` over an already-summarized month
writes nothing. `--stdout` writes nothing at all and is the safe way to inspect output. Nothing in
this plan deletes or rewrites a file: the only writer is `save_report`, which refuses a path that
exists. Nothing modifies `memory/`, `data/` or any existing report. The one edit to an existing
module is five additive names in `src/flow/report_archive.py`'s `FACT_ORDER`, which cannot change
any saved file's digest or filename because digests cover the body only; reverting it leaves the
new facts written in sorted order after the known ones, still readable. A half-finished milestone
is recovered by deleting the new files it created and re-reading this plan.

Milestones 7 and 8 keep that property and widen the first sentence rather than weakening it. The
edits to existing modules are still additive: four more `FACT_ORDER` names, one more token in
`report_filename` that only a fact-carrying summary can trigger, and the move of `stated_figures`
into `src/agentic_portfolio/flow/report_summary.py` with a re-export at its old path, so every
existing import still resolves. Re-running with `--language` over an already-translated month
writes nothing. `--pdf` renders only when the PDF is absent, so it is safe to repeat and is also
the way to add a PDF to a month summarized before Milestone 8 existed. Neither flag deletes or
rewrites anything: the only writers are `save_report`, which refuses a path that exists, and
`write_pdf`, which returns `created=False` for one. A PDF is derived, so recovery from a bad render
is deleting the `.pdf` and re-running - the Markdown it came from is untouched, which is the
property the whole failure design is built on.

Milestone 9 does not weaken that rule. A stylesheet change deliberately does not replace PDFs
already in the archive. New PDFs use the new appearance; to restyle an old derived file, move that
PDF aside and re-run with `--pdf`. The saved Markdown remains authoritative and unchanged.


## Artifacts and Notes

The end-to-end acceptance transcript, run from `/app/agentic_portfolio` against the real
`output/2026-09/`:

    $ uv run portfolio-summary 2026-09 --no-llm --stdout
    Reading output/2026-09/ ... 9 reports (4 portfolio, 5 whatif), 1 currency.
    ... the whole briefing, no prose ...
    Prose: none, by --no-llm. Every figure above was computed.
    # and the folder still holds nine files, not ten

    $ uv run portfolio-summary 2026-09
    Reading output/2026-09/ ... 9 reports (4 portfolio, 5 whatif), 1 currency.
    ... the briefing, with prose between the sections ...
    Prose: openai/gpt-5-nano. Every figure above was computed, not written by the model.
    Saved report: output/2026-09/2026-09-11-summary-473f6f15.md

    $ uv run portfolio-summary 2026-09
    Reading output/2026-09/ ... 9 reports (4 portfolio, 5 whatif), 1 currency.
    Summary already saved for these 9 reports: output/2026-09/2026-09-11-summary-473f6f15.md
    Re-run with --force to write a new one, or --stdout to print without saving.

    $ uv run portfolio-summary 2026-10
    No reports to summarize in output/2026-10/ (looked for kind: portfolio or kind: whatif).
    $ echo $?
    1

The saved summary's front matter, and the proof it is not read back as a source:

    ---
    digest: 473f6f1557f3e3c072839cba0e4b05161be8f299a70cbb9b436cee75f8613c4f
    saved_at: 2026-09-11T13:58:53Z
    as_of: 2026-09-11
    kind: summary
    command: portfolio-summary 2026-09
    month: 2026-09
    report_count: 9
    sources_digest: a5276d115b6d4ab77675f21dfab3e623003bfdb0f00ddf4cb2a1ed991cba389a
    llm_model: openai/gpt-5-nano
    narrative_status: written
    ---

    >>> load_month('output', '2026-09')
    sources: 9 | skipped: ['2026-09-11-summary-473f6f15.md: skipped, kind is summary']

The computed leaderboard, which is the section the whole command is built around - note that it is
two tables and not one, and that the held book appears in both:

    Window 2021-10-01 to 2026-09-01 (60 months), USD - 7 portfolio(s)

      rank  what                       return  volatility  Sharpe   div yield  div income
      1     portfolio MSR              0.2513  0.1056      2.0207   0.0475     4,753.20
      2     portfolio MV @0.1225       0.1225  0.0517      1.6347   0.0405     4,050.81
      3     portfolio MV @0.1000       0.1000  0.0467      1.3289   0.0416     4,156.13
      4     whatif CSPX.L+PFFA+VZ      0.0839  0.1250      0.3670   0.0460     17,976.00
      5     portfolio GMV              0.0480  0.0416      0.2394   0.0385     3,853.17
      6     whatif PFFA+VZ             0.0498  0.1447      0.0813   0.0795     17,976.00
      7     whatif PFF+PFFA+VZ (held)  0.0229  0.1207      -0.1253  0.0682     27,834.00
            benchmark SPY              0.1225  0.1481      0.5704   n/a        n/a

      Window 2022-10-03 to 2026-09-01 (48 months), USD - 2 portfolio(s)

      rank  what                       return  volatility  Sharpe  div yield  div income
      1     whatif PFFA+VZ             0.1073  0.1461      0.4740  0.0795     17,976.00
      2     whatif PFF+PFFA+VZ (held)  0.0713  0.1187      0.2808  0.0682     27,834.00

And the section the command exists for, which no single saved report can print:

    The SAME thing measured over more than one returns window. These are not different
    portfolios; they are one portfolio and two measurements of it.

      positions PFF:6000, PFFA:6000, VZ:2000 (USD)
        window                                return  volatility  Sharpe
        2021-10-01 to 2026-09-01 (60 months)  0.0229  0.1207      -0.1253
        2022-10-03 to 2026-09-01 (48 months)  0.0713  0.1187      0.2808

      positions PFFA:6000, VZ:2000 (USD)
        window                                return  volatility  Sharpe
        2021-10-01 to 2026-09-01 (60 months)  0.0498  0.1447      0.0813
        2022-10-03 to 2026-09-01 (48 months)  0.1073  0.1461      0.4740

Milestones 7 and 8, verified 2026-09-13 against a copy of the real `output/2026-09/` - 22 source
reports across USD and JPY, which is what that folder actually holds now rather than the nine it
held when this plan was written. A COPY, so the verification wrote nothing into the archive:

    $ uv run portfolio-summary 2026-09 --output-dir <copy> --no-llm --pdf
    ... the figures-only briefing ...
    Prose: none, by --no-llm. Every figure above was computed.
    Saved report: <copy>/2026-09/2026-09-13-summary-799d0668.md
    Saved PDF: <copy>/2026-09/2026-09-13-summary-799d0668.pdf

    $ uv run portfolio-summary 2026-09 --output-dir <copy> --no-llm --pdf
    Summary already saved for these 22 reports: .../2026-09-13-summary-799d0668.md
    Re-run with --force to write a new one, or --stdout to print without saving.
    PDF already saved beside the report: .../2026-09-13-summary-799d0668.pdf

The PDF: 29,399 bytes, `%PDF-1.7`, six pages, and the widest text box laid out at 663.9px inside
680.3px of content width - so nothing runs off the paper. `load_month` over the same folder
afterwards reports `sources: 22` and one skip note naming the summary; the `.pdf` produces no note
at all, because the reader globs `*.md`.

Both refusals, with their real exit statuses:

    $ uv run portfolio-summary 2026-09 --output-dir <copy> --pdf --stdout ; echo $?
    portfolio-summary: error: --pdf renders a PDF beside a saved Markdown file, and --stdout
    saves nothing. Drop --stdout to save both, or drop --pdf to just look at the month
    2
    $ uv run portfolio-summary 2026-09 --output-dir <copy> --language zh-TW --no-llm ; echo $?
    portfolio-summary: error: --language cannot be combined with --no-llm: a translation is
    written by a model, and --no-llm makes no network call at all. Drop one of them
    2
    $ uv run portfolio-summary 2026-10 --output-dir <copy> --no-llm ; echo $?
    No reports to summarize in <copy>/2026-10/ (looked for kind: portfolio or kind: whatif).
    1

`--language zh-TW --pdf` with a STUBBED translator - a stub that masks, replaces every word, and
restores the markers, exactly as a faithful translator would - wrote the four files, and the
acceptance proof is the diff:

    $ diff <(grep '^  ' <english>.md) <(grep '^  ' <chinese>.md)
    # no output: all 206 table lines byte-identical

Comparing the FIGURES of the two bodies is the other half of it. The English body states 1,081 and
the Chinese 1,087; nothing is missing, and every one of the six additions is accounted for by the
provenance lines this feature appends on purpose - `2026`, `09`, `13`, `5981` and `661` from the
English filename it names, and `5` from the model name. The body grows by exactly three lines: a
blank, the model's disclaimer in the target language, and the English audit line.

The translation's front matter, which is where the pair becomes traceable:

    kind: summary
    month: 2026-09
    report_count: 22
    sources_digest: f3700d2de48c19f082d751bd15b77e3b1d1785506d7318ba29a2fcf079cd6a0c
    llm_model: openai/gpt-5-nano
    narrative_status: written
    language: zh-TW
    translation_model: openai/gpt-5-nano
    translated_from: 5981d66154b1ab92dbefbad3294902f73d11df891a3f92c93b510aadc81f3262
    translation_status: written

Test counts for the two milestones: `tests/test_briefing_blocks.py` 39,
`tests/test_report_translation.py` 49, `tests/test_report_pdf.py` 48, and
`tests/test_summary_cli.py` 35 -> 73. Running the nine affected files together reports 363 passed
in 5.2s, which is the reassuring direction: nothing added here reaches the network, and the PDF
tests invoke WeasyPrint for real at about a tenth of a second per render.

The full suite: `uv run pytest tests/test_*.py` reports `1261 passed, 3 warnings in 634.33s`, and
`--collect-only -q` confirms 1261 - the 1049 recorded earlier in this plan plus the 127 added here
and 85 added by other work in between.

**The runtime needs explaining, because this plan made it an acceptance criterion**, and 634s is
well above the "materially above about 460 seconds means something is reaching the network" line
written for Milestone 5. It was measured and it is not the new code. The 15 slowest tests are ALL
in `tests/test_holdings.py` (13 of them) and `tests/test_holdings_cli.py` (2), the slowest at
23.36s, together accounting for about 236s; not one of the new test files appears among them, and
`tests/test_holdings.py` imports no module this amendment touched. The nine affected files run in
5.2s in total, so Milestones 7 and 8 contribute under one percent of the suite's time.

Two corrections worth recording rather than hiding. The first full run of this amendment reported
`1223.81s`, nearly double the real figure, because two earlier suite runs had been left going and
were competing for the same cores - a measurement error, not a regression, and the reason the
number was re-measured alone before being written down. And the 460-second threshold itself is now
misleading: it was a sound heuristic for a 1049-test suite whose slowest tests were not the
holdings ones, and it should be read as "the LLM and network-touching paths must stay mocked",
which the per-file timings above demonstrate directly, rather than as a single number against a
suite whose composition has changed.

Per-milestone test counts, each file failing before its milestone and passing after:

    $ uv run pytest tests/test_report_summary.py tests/test_report_summary_agent.py \
        tests/test_summary_cli.py -q
    104 passed in 2.00s

## Interfaces and Dependencies

No new third-party dependency. `crewai` and `pydantic` are already declared in `pyproject.toml`;
everything else used is standard library (`hashlib`, `dataclasses`, `datetime`, `pathlib`, `re`,
`argparse`).

In `src/config/settings.py`, add to `Settings`:

    llm_quick: str = "openai/gpt-5-nano"
    openai_api_key: str | None = None

In `src/flow/report_summary.py`, define:

    SOURCE_KINDS: tuple[str, ...]
    SUMMARY_KIND: str

    @dataclass(frozen=True)
    class ReportRecord:
        path: Path
        facts: dict[str, str]
        body: str

    def load_month(output_dir: str | Path, month: str) -> tuple[list[ReportRecord], list[str]]
    def parse_weights(body: str) -> tuple[tuple[str, float], ...]
    def parse_benchmark(body: str) -> tuple[str, float, float, float, int, int] | None
    def parse_whatif_deltas(body: str) -> dict[str, float]
    def sources_digest(records: Sequence[ReportRecord]) -> str
    def label(record: ReportRecord) -> str
    def build_month_digest(
        records: Sequence[ReportRecord], month: str, skipped: Sequence[str]
    ) -> MonthDigest
    def render_digest(digest: MonthDigest) -> str
    def digest_for_llm(digest: MonthDigest) -> str

plus the frozen dataclasses `Scope`, `LeaderRow`, `Partition`, `ThreadStep`, `Thread`, `Consensus`,
`BookComparison`, `ScenarioLedger`, `Comparability` and `MonthDigest`.

In `src/agents/summary_schema.py`, define `class MonthNarrative(BaseModel)` with the eight fields
listed in Milestone 2.

In `src/agents/summary_crew/crew.py`, define:

    @CrewBase
    class ReportSummaryCrew:
        agents_config = "config/agents.yaml"
        tasks_config = "config/tasks.yaml"
        def __init__(self, model: str) -> None

In `src/agents/report_summary.py`, define:

    def generate_narrative(
        month: str, report_count: int, facts: str, model: str | None = None
    ) -> MonthNarrative

In `src/flow/summary_cli.py`, define:

    def render_summary(digest: MonthDigest, narrative: MonthNarrative | None, reason: str | None) -> str
    def main() -> None

In `pyproject.toml`, add `portfolio-summary = "src.flow.summary_cli:main"` to
`[project.scripts]`.

Milestones 7 and 8 add no new third-party distribution. `weasyprint` was already declared in
`pyproject.toml` and was, until Milestone 8, the one declared dependency nothing imported. An
earlier draft of Milestone 8 also declared `fontTools`; it is not needed, because
`weasyprint.text.ffi` already exposes the fontconfig library the glyph check asks. What those
milestones DO add is a system-library requirement that a lock file cannot express: WeasyPrint
`dlopen`s Pango at import time, so both container images must install `libpango-1.0-0`,
`libpangoft2-1.0-0`, `libharfbuzz0b` and `libharfbuzz-subset0`, plus `fonts-dejavu-core` for the
tables and `fonts-noto-cjk` for any non-Latin translation.

In `src/agentic_portfolio/flow/briefing_blocks.py`, define:

    class Block(NamedTuple):
        kind: str      # heading | paragraph | bullet | preformatted | blank
        text: str

    def split_briefing(body: str) -> tuple[Block, ...]
    def join_briefing(blocks: Sequence[Block]) -> str
    def protect_tokens(text: str, literals: Sequence[str]) -> tuple[str, tuple[str, ...]]
    def restore_tokens(text: str, tokens: Sequence[str]) -> str

In `src/agentic_portfolio/flow/report_summary.py`, add:

    def protected_literals(
        digest: MonthDigest, records: Sequence[ReportRecord]
    ) -> tuple[str, ...]


In `src/agentic_portfolio/agents/translation_schema.py`, define `TranslatedBlock` and
`TranslatedBriefing`. In `src/agentic_portfolio/agents/translate_crew/crew.py`, define
`ReportTranslationCrew` with the same three decorated methods as `ReportSummaryCrew`. In
`src/agentic_portfolio/agents/report_translation.py`, define:

    class TranslationUnavailable(RuntimeError)

    def verify_translation(
        sources: Sequence[str], translated: Mapping[int, str]
    ) -> tuple[dict[int, str], tuple[str, ...]]

    def translate_briefing(
        body: str,
        language: str,
        digest: MonthDigest,
        records: Sequence[ReportRecord],
        model: str | None = None,
    ) -> tuple[str, tuple[str, ...]]

In `src/agentic_portfolio/flow/report_pdf.py`, define:

    class PdfUnavailable(RuntimeError)

    class RenderedPdf(NamedTuple):
        path: Path
        created: bool

    def contains_cjk(text: str) -> bool
    def display_width(line: str) -> int
    def pre_font_size(width: int) -> float
    def cjk_font_family(families: Sequence[str] = CJK_FAMILIES) -> str | None
    def briefing_html(facts: Mapping[str, str], body: str, *, source_name: str) -> str
    def stylesheet() -> str
    def pdf_path(md_path: Path | str) -> Path
    def write_pdf(md_path: Path | str) -> RenderedPdf | None
    def format_pdf_notice(rendered: RenderedPdf | None) -> str | None

In `src/agentic_portfolio/flow/summary_cli.py`, `existing_summary` gains a `language` parameter,
`_write` returns its `SavedReport`, and a `_render_pdf(path) -> None` sibling is added.


## Amendment note - 2026-09-11, Milestone 6

**What changed.** A `Source reports` appendix was added to the end of the rendered briefing, listing
every report it was built from with the eight characters that name that report's file, the time it
was saved, and what it is. Supporting it: a `SourceReport` dataclass, `build_sources`, a
`sources` field on `MonthDigest` and a `folder` field on `Scope`, all in
`src/flow/report_summary.py`; 13 new tests; and a `README.md` sentence. The document's trailing
`Source reports digest:` line was reworded to say that it identifies the SET of reports rather than
any one file, because printing it directly beneath a heading called `Source reports` would otherwise
read as the digest of that section.

**Why.** From use. The briefing succeeded at comparing portfolios and failed at the next thing a
reader does with it: having picked `whatif CSPX.L+PFFA+VZ` out of the leaderboard, they had no way
to tell which of the month's files to open. The identifier needed was already in the data and simply
never printed.

**Why as an amendment rather than `plans/20_*.md`.** `PLANS.md` makes ExecPlans living documents,
and this is a refinement of a delivered feature rather than a step of its own. Splitting it off
would have left this plan's `Outcomes & Retrospective` describing a command that no longer matched
it - and in particular still listing the traceability gap as an open one.

**A bug this amendment found.** Verifying the appendix against the real archive meant regenerating
it with `--force`, which left `output/2026-09/` holding two summaries of the same nine reports - and
that revealed `existing_summary` returning the first match by filename, i.e. the older briefing. A
summary's filename carries a digest of its own text and therefore no order, so the fix is to compare
`saved_at`. Worth recording how it was found: no test had put two summaries of one month in a folder,
so only running the thing against real data surfaced it.

**One behaviour worth knowing about, which this amendment exposed rather than introduced.** Repeat
detection compares the digest of the SET of source reports, not of the briefing text (see the
`Decision Log`). So a change to the briefing's FORMAT does not invalidate an already-saved summary:
a month summarized before this amendment still reports `Summary already saved for these 9 reports:`,
and `--force` is how the new layout reaches a saved file. That is the right trade - the alternative
writes a new file on every run - but it is not obvious, so `README.md` now says it.


## Amendment note - 2026-09-13, Milestones 7 and 8


**What changed.** The briefing can now be read by someone who is not the person who ran the
commands. Two flags were added to `uv run portfolio-summary`: `--language`, which saves a second
file holding the same briefing with its sentences translated and every table byte-identical to the
English original, and `--pdf`, which renders a PDF beside every Markdown file the run saved.
Supporting them: `src/agentic_portfolio/flow/briefing_blocks.py` (shared by both),
`src/agentic_portfolio/agents/translation_schema.py`,
`src/agentic_portfolio/agents/translate_crew/`,
`src/agentic_portfolio/agents/report_translation.py`, `src/agentic_portfolio/flow/report_pdf.py`
and its packaged stylesheet, `protected_literals` in
`src/agentic_portfolio/flow/report_summary.py`, four new `FACT_ORDER` keys with a `language`
filename token, `purpose` and `remedy` parameters on `api_key_problem` so that a missing key stops
recommending `--no-llm` to a caller for whom `--no-llm` is refused, `translate_crew` added to
`tests/test_packaging.py`'s `CREW_PACKAGES`, and the system libraries and fonts both container
images were missing.

**Why.** From use, and from the same observation twice. The briefing succeeded at saying what a
month of runs meant and failed at reaching anyone who does not read English comfortably or does not
have a Markdown viewer - which is most people who would want to be shown one. Neither gap needed
new analysis: the figures were already computed, the document was already rendered, and
`weasyprint` was already declared in `pyproject.toml` and imported nowhere.

**Why as an amendment rather than `plans/21_*.md`.** `PLANS.md` makes ExecPlans living documents,
and this is the same judgement Milestone 6 recorded: these are new flags on a delivered command,
reading the document that command already renders, rather than a step of their own. Splitting them
off would have left this plan's `Outcomes & Retrospective` describing a command that no longer
matched it - and in particular still describing a briefing that is only ever English Markdown.

**What the design turns on, in one sentence each.** The translator is never shown a table, which is
this plan's own figures-in-Python split applied one level up and is what keeps the columns aligned
in a language whose glyphs are double-width. The tokens that must survive inside a translated
sentence are drawn from the `MonthDigest`'s own knowledge of the month rather than from a regex
over capital letters, which would have protected `CAUTION` and `WITHIN`. And there is no Markdown
library, because a generic parser reads a 2-space-indented `ljust`-padded table as prose.

**Two things this amendment changed about itself while being implemented**, both recorded in the
`Decision Log` and `Surprises & Discoveries` rather than quietly edited out. The first draft
specified moving `_FIGURE` and `stated_figures` into the flow layer; that was written while the
verifier was still imagined as comparing figures, and once it became placeholder equality the move
had no consumer and would have introduced the project's first `agents` -> `flow` import for
nothing. And the verifier as first written checked only the markers, which is blind to a heading:
`#` is not a figure, so a model that translated `## Risk/return leaderboard` into a bare sentence
passed every check and silently demoted a section. `structure_prefix` closed that, and twelve tests
failed the moment it was added - every one of them because the stand-in translator had been
translating the prefix away, which is exactly what a live model would do.

**Two things the exploration found that were not suspected.** The released image could not have
imported WeasyPrint at all - the runtime stage installs neither Pango nor HarfBuzz, and the failure
would have surfaced at a user's first `--pdf` rather than in any build step. And no font in this
container has a single CJK glyph, which WeasyPrint renders as empty boxes without a word of warning.
That second one is why `--pdf` refuses a CJK document rather than warning about it: a refusal leaves
the reader the Markdown, a warning leaves them a finished-looking file full of boxes.

**What these milestones deliberately do not do.** Table headers and cell contents stay English -
`rank`, `what`, `return`, every ticker, every label - because `str.ljust` padding and double-width
CJK glyphs cannot both be right, and localizing them means teaching `_table` to pad by display
width, which is an i18n project rather than a flag. The deterministic English sentences are
translated by a model on each run, so one heading may be worded differently in two runs; a message
catalogue would fix that and is much larger. Nothing checks translation QUALITY - the verifier
proves the figures, tables, tickers and structure are the English original's, and a fluent
mistranslation of a caution passes every check, which is the honest boundary of a mechanical
verifier and is why the document says it was machine-translated. There is no `--language` on
`uv run portfolio` or `portfolio-holdings whatif`. And dedup is still by source set rather than by
text, now doubled: after a change to the translation logic or the PDF renderer, an
already-summarized month still reports `Summary already saved`, and `--force` is how the new output
reaches disk.


## Amendment note - 2026-09-14, Milestone 9


**What changed.** The packaged PDF stylesheet now adapts the visual language of the root
`report.css`: navy and blue hierarchy, a centered 21pt title, more readable 10pt prose, stronger
section rules, pale fixed-width blocks, and more deliberate spacing. The title and month repeat at
top right while the source filename and page count remain in the footer. No Markdown, CLI or Python
interface changed.

**Why adaptation rather than replacement.** The reference stylesheet belongs to a different
Markdown-to-HTML producer. Its semantic tables, subtitle, running divs, callouts and deeper headings
do not exist in this briefing's deliberately closed HTML vocabulary. More importantly, its 16mm
side margins disagree with the 510pt width used to size each fixed-width table. Copying it verbatim
would make unsupported selectors look implemented while weakening the two guarantees that made the
PDF feature useful: aligned tables and traceable pages.

**What remained load bearing.** The side margins stay at 15mm, DejaVu Sans Mono remains the first
table font, and `pre-wrap`, per-block font sizing and the hanging indent are unchanged. Prose can use
Noto first because it is not measured by the table arithmetic. Automated rendering confirms the
real English and Traditional Chinese briefings each occupy eight pages and no text box exceeds the
680.3px content width; focused PDF and packaging tests report 72 passed in 2.27s.

**Archive behavior.** The untracked root `report.css` remains untouched and is not packaged. An
existing PDF remains immutable and therefore keeps its old appearance; moving that derived file
aside and re-running with `--pdf` renders the saved Markdown with the new style. This preserves the
no-overwrite decision from Milestone 8 rather than quietly assigning a new meaning to `--force`.
