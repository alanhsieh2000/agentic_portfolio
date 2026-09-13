# Make this project installable and release it as a Docker image

This ExecPlan is a living document. The sections `Progress`, `Surprises & Discoveries`,
`Decision Log`, and `Outcomes & Retrospective` must be kept up to date as work proceeds.

This document must be maintained in accordance with `PLANS.md`, at the repository root of
`/app/agentic_portfolio`.

It builds on work described by every plan from `plans/01_dataset.md` through
`plans/19_monthly_report_summary.md`. All of those files are checked into this repository and are
incorporated here by reference. Everything this plan actually relies on is nevertheless restated
below, so a reader who opens only this file can finish the work.

One warning about those earlier plans, stated here because it will otherwise confuse you. They
were written before this plan renamed the Python package, so they refer to source files by paths
like `src/flow/cli.py`. After this plan is done, that file lives at
`src/agentic_portfolio/flow/cli.py`. Plans 01 through 19 are deliberately **not** rewritten - they
are a dated record of decisions taken at a time when those paths were correct, and rewriting 809
path references across nineteen files would falsify that record while touching quoted code blocks
that were accurate when quoted. When you read an older plan, mentally insert `agentic_portfolio/`
after `src/`. This plan uses the new paths throughout, except where it is quoting the state of the
tree before the move.


## Purpose / Big Picture

Today this project can only be run by its author, in a development container, from the repository
directory. Nobody else can deploy it. After this plan, anyone with Docker can run the whole thing
on their own machine with one command, and the paper reproduction works immediately because the
market-data database ships inside the image.

This is what someone gains. Before:

    $ portfolio-backtest
    bash: portfolio-backtest: command not found

    # ... and to get there themselves: install Python 3.12, install uv, clone the repo,
    # install two dozen dependencies including crewai's whole provider stack, obtain an
    # SEC User-Agent, then run eight dataset build commands, one of which is a
    # rate-limited pass over roughly five hundred companies' SEC filings.

After:

    $ docker run --rm ghcr.io/<owner>/agentic-portfolio:0.1.0 portfolio-backtest
    Realized annualized Sharpe (MSR, net of transaction cost and risk-free rate, this project): 0.7118
    Paper-reported S&P 500 baseline Sharpe, 2020-2024: 0.6324

That run needs no dataset build, no `SEC_UA`, and no Python on the host. It does need an Anthropic
API key, because the backtest asks the LLM-S agent to generate screening rules - and if the key is
missing, the run now says so in about a second instead of failing with a provider traceback several
minutes into fetching data.

The interactive work also becomes available to other people:

    $ docker run -it --rm --user "$(id -u):$(id -g)" \
        -v "$PWD:/work" -w /work \
        ghcr.io/<owner>/agentic-portfolio:0.1.0 \
        portfolio --date today --value 100000 --selection user_provided

    Edit candidate pool? [a]dd tickers / [r]emove tickers / [s]ummary / [d]one: a
    Tickers to add: SPY
    ...
    Saved report: output/2026-09/2026-09-12-portfolio-GMV-user_provided-USD-a1b2c3d4.md

    $ ls -l output/2026-09/
    -rw-r--r-- 1 alan alan 4821 Sep 12 14:02 2026-09-12-portfolio-GMV-user_provided-USD-a1b2c3d4.md

Note the ownership in that last line: the report belongs to the host user, not to root and not to
some container-internal account. That is what `--user "$(id -u):$(id -g)"` buys, and it is the
difference between a usable tool and one that litters your directory with files you cannot delete.

Three things make this possible, and each is a separate piece of work:

- The project becomes a **real Python package**. Right now a built wheel would install a top-level
  directory literally named `src` into the user's Python environment - a name so generic it
  collides with any other project that does the same. After this plan the installed package is
  `agentic_portfolio`.
- Every **file path the program uses** is resolved from one place, so the image can point the
  market-data database at a read-only location inside the image while everything the program
  *writes* still lands in the directory the user mounted.
- The **image** is built, carries the dataset, runs as a non-root user, and is published to GHCR
  for both Intel and ARM machines - with a check that proves no secret and nothing personal to the
  author ever enters a published layer.


## Progress

- [x] (2026-09-12 06:10Z) Plan authored. Distribution decisions settled with the user: Docker image
      only, published to GHCR, multi-arch, bare-command entrypoint; do the package rename now
      regardless; keep paths cwd-relative behind one home setting; bake the dataset; add API-key
      preflight checks rather than a new init command; trim dependencies; MIT license.
- [x] (2026-09-12 07:17Z) Step 0 - baseline recorded: `1049 passed, 3 warnings in 421.48s (0:07:01)`
      on a clean tree at commit `1cdd58f`, branch `docker_deployment`. Bytecode caches purged.
      Reference counts for Step 2 verified by measurement, correcting two figures taken from an
      earlier survey (see `Surprises & Discoveries`).
- [x] (2026-09-12 07:30Z) Step 1 - moved `src/*` into `src/agentic_portfolio/` via the temporary
      directory. Git recorded **64** renames (58 `.py` plus the 6 CrewAI YAML), all at 100%
      similarity, no content changes.
- [x] (2026-09-12 07:35Z) Step 2 - rewrote all 527 `src.` and 314 `src/` references across 75
      files. Needed **six** sed rules, not four: a fifth category of reference
      (`python -m src.<module>`) was missed on the first pass and caught by the gate. Both gates
      pass: the only surviving `src.` is `flow/live.py:125`'s DuckDB alias, and its `ATTACH ... AS
      src` on line 123 is intact.
- [x] (2026-09-12 08:25Z) Step 3 - done. `pyproject.toml`: 13 script targets rewritten to
      `agentic_portfolio.*`, `packages = ["src/agentic_portfolio"]` with the old justifying comment
      replaced, `pythonpath = ["."]` removed with a comment explaining why. `uv sync` re-installed
      and `agentic_portfolio.flow.cli` resolves to `src/agentic_portfolio/flow/cli.py`. Gates
      passed: **1049 tests collected** in 4.16s, **94 passed** in the `test_optimizer.py` +
      `test_summary_cli.py` smoke subset, and all 13 entry points import with a callable `main`.
      Docs: the two pre-move paths in `README.md` and the illustrative one in `AGENTS.md` updated,
      and `AGENTS.md` now states the importable package is `src/agentic_portfolio/` and notes the
      layout matches the canonical CrewAI layout in `CREWAI.md`. The `pyproject.toml` part of this
      step was committed in `a4ab6e7`; the doc edits came after.
- [x] (2026-09-12 09:05Z) Step 4 - all seven path literals now exist only in
      `config/settings.py`, each wrapped in `_under_home`, with five new fields
      (`holdings_db_path`, `news_archive_path`, `candidates_path`, `rates_path`,
      `portfolio_path`) beside the existing `db_path` and `output_dir`. Defaults verified
      **byte-identical** to before. `AGENTIC_PORTFOLIO_HOME=/tmp/ws` moves all seven together;
      `DB_PATH` wins alone and leaves `output_dir` in place, which is the split the image needs.
      The two inert argparse defaults are fixed - proof: `DB_PATH=/ro/baked.duckdb portfolio
      --date 2024-03-29 --value 1000` now fails naming `/ro/baked.duckdb`, where before it named
      `data/portfolio.duckdb` and ignored the variable. Gate: **556 passed** across the ten
      path-sensitive test modules. `tests/conftest.py` needed **no** change - see
      `Surprises & Discoveries`.
- [x] (2026-09-12 09:55Z) Step 5 - `config/preflight.py` added, holding `api_key_problem` (moved
      from `agents/report_summary.py` and re-exported there so existing callers and monkeypatch
      targets still resolve), plus `require_api_keys`, `require_sec_user_agent` and
      `export_api_keys`. Wired into `flow/cli.py:main` (right after `parse_args`),
      `flow/backtest.py:main`, `dataset/fundamentals.py:main`,
      `dataset/backfill_snapshot.py:main`, and `flow/live.py`'s snapshot builder for the two
      selections that reach the SEC. `llm_f_model` added to `Settings` and `agents/llm_f.py`
      switched to it, removing the last direct `os.environ` model read; `DEFAULT_MODEL` and the now
      unused `import os` deleted from that module, and `import os` from `report_summary.py`.
      A new `agents/crew_config.py` turns CrewAI's silent missing-YAML behavior into a named error -
      it previously surfaced as a bare `KeyError: 'strategy_agent'`, which matters much more once
      the prompts ship inside a wheel. Both refusals exit **2** with the sentence on stderr, which
      needed fixing after measurement (see `Surprises & Discoveries`). 17 new tests in
      `tests/test_preflight.py`; the four LLM/summary modules' own 85 tests still pass.
- [x] (2026-09-12 10:20Z) Step 6 - runtime dependencies cut from 21 to the **nine** actually
      imported, verified by parsing every module's import statements with `ast` rather than by
      reading the list. Removed: `aisuite`, `exa-py`, `markdown`, `matplotlib`, `parsel`,
      `python-dotenv`, `questionary`, `redis`, `regex`, `seaborn`, `stockstats`, and `pytest`
      (which was a runtime dependency). `pytest` and `cvxpy` moved to a PEP 735
      `[dependency-groups] dev`. crewai extras cut to `[anthropic]` alone - `[tools]` went because
      `from crewai.tools import BaseTool` resolves to crewai **core**
      (`site-packages/crewai/tools/__init__.py`) and nothing imports `crewai_tools`. Result:
      **204 locked distributions -> 166, 38 removed.** Gate: 107 tests across the optimizer, LLM
      and preflight modules, plus all three crews and the `BaseTool` subclass importing cleanly -
      which is the real check on the pruned extras.
- [x] (2026-09-12 10:35Z) Step 7 - `description`, `readme`, `license = "MIT"`, `license-files`,
      `authors`, `keywords`, `classifiers` (including `Private :: Do Not Upload` as a hard guard
      against an accidental PyPI publish), `[project.urls]` and
      `[tool.hatch.build.targets.sdist]` added. New `LICENSE` (MIT) and `NOTICE` recording the
      three data sources and their differing positions. `uv build` produces both artifacts, and
      the wheel is verified correct: **6** CrewAI prompt YAML files present, top-level names are
      `agentic_portfolio` and `agentic_portfolio-0.1.0.dist-info` only, **no** top-level `src/`,
      and all **13** console scripts declared against `agentic_portfolio.*`.
- [x] (2026-09-12 07:38Z) Step 8 - `.dockerignore` written **out of order**, deliberately: brought
      forward so that no build could ever pick up `.env` or the author's personal data. Verified
      without Docker by simulating its rules over the whole tree: **76 files would be sent, 24,407
      excluded**; top-level entries are `LICENSE NOTICE README.md data docker pyproject.toml src
      uv.lock`; no `.env`, `memory/`, `output/`, `.venv/`, `.git/` or `holdings.duckdb`; and
      `data/` contributes **only** `portfolio.duckdb`. That is a simulation of Docker's semantics,
      not Docker's own evaluation - confirm with the real builder when one is available.
- [~] (2026-09-12 11:10Z) Step 9 - **written but UNVERIFIED: there is no Docker in this dev
      container** (`docker`, `buildx`, `podman` and `nerdctl` are all absent and there is no
      `/var/run/docker.sock`). Authored: the four-stage runtime `Dockerfile`; `docker/README.md`
      with build, verify and publish commands; `docker/dataset_manifest.py`; and
      `docker/dataset.sha256` holding
      `916e4f5f5670b68490b99905a75131e6902e2333184571376f74c0d3108adc05` - the **recovered**
      dataset. The development container moved to `docker/Dockerfile.dev` (not `.devcontainer/`,
      which is gitignored and would have untracked it). The manifest generator **is** verified: run
      against the real database it produces a 3.9 KB `DATASET.json` reporting 11 tables, 1,203,663
      price rows over 2015-01-02..2024-04-29, 525 distinct tickers, and the AVB/EA/EQR/LEG reasons.
      **Still to do on a host with Docker:** build it, run the verification block in
      `docker/README.md`, and above all run the secret-absence gate before any push.
- [~] (2026-09-12 11:30Z) Step 10 - **documentation done, publishing blocked.** `README.md` now
      carries a `# Installation` section (pull command, the `apx` alias, what each `docker run`
      flag buys, a table of which credential each command needs and which need none, and the two
      traps: builders need `-e DB_PATH=/work/...`, and `user_provided`/`holdings`/`whatif` need
      network but no database); a `## Where files are written` table mapping `/work` to the host
      directory; a `# Development` section explaining the two-Dockerfile split, the missing pytest
      `pythonpath` and why, and the `--help` hazard; and
      `## Bundled market data - provenance and licensing` under Acknowledgements, naming all three
      data sources and their differing terms. The `# How to Use` preamble no longer says `uv run`
      unconditionally. **Blocked:** the actual multi-arch build and GHCR push, on the missing
      Docker. Commands are written up in `docker/README.md` ready to run, including the
      `--annotation index:` and make-the-package-public steps that are easy to miss.

Steps 1, 2, partial 3 and 8 are committed as `a4ab6e7` on branch `docker_deployment`.

- [x] (2026-09-12 08:15Z) **Unplanned: recovered `data/portfolio.duckdb` after damaging it.** A
      `--help` sweep over all thirteen console scripts ran the dataset builders (see
      `Surprises & Discoveries`), costing 7,038 price rows. Recovered offline by grafting the four
      dividend tables from the damaged file onto a pre-dividend backup whose price history was
      provably the original. All nine verification properties match, and the recovered file is
      **46,936,064 bytes - byte-for-byte the original size**, which no step of the merge targeted
      and so is independent corroboration. Verified through the application's own dividend code,
      not just SQL: `JNJ 0.0321, KO 0.0325, T 0.0715, XOM 0.0345` as of 2024-03-29, with
      `AVB/EA/EQR/LEG` correctly reported unavailable rather than as zero payers. Both inputs kept
      as `data/portfolio.candidate.duckdb` and `data/portfolio.damaged-20260912.duckdb`.
- [ ] Follow-up (not in this plan): make the `crewai` import lazy so `--selection user_provided`
      starts without loading the LLM stack. Deferred because roughly eight tests in
      `tests/test_interactive_flow.py` monkeypatch `generate_rule` and `screen_month` by attribute
      name, which only works while those names exist at module scope; rewriting those mocks during
      a release refactor buys startup time that is invisible inside an image that contains crewai
      anyway.


## Surprises & Discoveries

- Observation: the packaging defect cannot be reproduced in the development container, which is why
  it has gone unnoticed. The project *is* installed in `.venv`, but as an **editable** install.
  Evidence: `agentic_portfolio-0.1.0.dist-info` exists in
  `.venv/lib/python3.12/site-packages/` while `site-packages/src` does not, and all thirteen
  console scripts exist in `.venv/bin/`. `head -4 .venv/bin/portfolio` shows `from src.flow.cli`,
  which resolves only because an editable install adds a path hook pointing back at the repository.
  A normal install has no such hook, so it would have to materialize a real `site-packages/src/`
  directory. Only inspecting a built wheel can catch this.

- Observation: a naive search-and-replace of `src.` would silently corrupt SQL, not Python.
  Evidence: `src/flow/live.py:123-125` (pre-move path) executes
  `ATTACH '{escaped_path}' AS src (READ_ONLY)` and then
  `CREATE TABLE news_articles_hf AS SELECT * FROM src.news_articles_hf`. Here `src` is a DuckDB
  database alias, not a Python module. Rewriting it produces valid Python and broken SQL, and the
  test that exercises that code path (`tests/test_interactive_flow.py:833-864`) mocks the surrounding
  builders, so the corruption would not necessarily fail loudly. It is reachable only via
  `--selection llm_f_only` or `llm_s_and_f` in live mode - the two most expensive selections, which
  nobody runs casually.

- Observation: the rewrite is about twice the size a first look suggests. Evidence: 260 import
  lines, but **248 string-target monkeypatches** of the form
  `monkeypatch.setattr("src.flow.cli.X", ...)` across twelve test files, plus four backticked
  `` `src.module` `` docstring references and 314 prose `src/...` path references inside
  docstrings. An import-anchored rule alone would miss all 248. Measured with
  `grep -ro '"src\.' --include='*.py' src tests | wc -l`, which finds all 248 in `tests/` and none
  in `src/`.

- Observation: **how an API key in `.env` actually reaches litellm, and why it will stop working
  once this is packaged.** This took three attempts to get right; the first two conclusions written
  here were both wrong, so the mechanism is spelled out rather than the symptom.

  Nothing in `agents/` ever passes a key to the LLM client (`grep -rn "api_key\|base_url"
  src/agentic_portfolio/agents/` finds only the preflight's own check), and pydantic-settings loads
  `.env` without exporting it to `os.environ`, which the SDKs read. So a key in `.env` alone looks
  like it could never work. It does work, because **CrewAI calls `load_dotenv()` itself** in
  `crewai/llm.py`. The part that matters is *which* `.env` that finds: python-dotenv searches
  **upward from the calling module's own file**, not from the working directory. In a development
  checkout CrewAI's file is `<repo>/.venv/lib/python3.12/site-packages/crewai/llm.py`, so the search
  walks up and finds `<repo>/.env` - whatever directory you ran from. Measured:

      $ cd /tmp/nokeys        # no .env here or in any parent
      $ env -u ANTHROPIC_API_KEY python /tmp/probe.py
      cwd         : /tmp/nokeys
      env KEY     : None
      after settings import -> settings.anthropic_api_key: None
      after cli import      -> env KEY : '1k3HpAFpbLwkclRtoLk...'     <-- injected by crewai
      crewai/llm.py lives in : /app/agentic_portfolio/.venv/lib/python3.12/site-packages/crewai
      first .env walking UP  : /app/agentic_portfolio/.env

  **This accident does not survive packaging, and that is the whole point.** In the image the
  virtual environment is at `/opt/agentic-portfolio/venv`, so searching upward from CrewAI reaches
  `/opt/agentic-portfolio/` and `/` - never the user's mounted `/work`. A `.env` in the mounted
  workspace would satisfy this project's own `Settings` (whose `env_file` follows
  `AGENTIC_PORTFOLIO_HOME`, default: the working directory) and therefore *pass the preflight*, then
  fail inside the provider call minutes later. A check that passes and a run that dies afterwards is
  worse than no check.

  So Step 5 adds `export_api_keys()`, which copies a provider key that `Settings` has and the
  environment lacks into `os.environ`, never overriding one already set. Three lines, and it makes
  the two readers agree in every layout - checkout, wheel and image alike. It also means the
  preflight's verdict is now the same question the SDK will ask.

  One consequence for testing, recorded in `tests/test_preflight.py`'s own docstring: because
  importing anything that reaches CrewAI injects the repository's real key into `os.environ`, the
  refusal path **cannot** be tested in this repository by unsetting the variable. Both `Settings`
  and `os.environ` have to be monkeypatched, or the test asserts nothing.

- Observation: **the packaging defect is provably fixed, and the proof required a venv outside the
  repository.** Installing the built wheel into `/tmp/fresh` and running from `/tmp` - so nothing
  can resolve from the source tree - gives:

      $ /tmp/fresh/bin/python -c "import agentic_portfolio as p; print(p.__file__)"
      /tmp/fresh/lib/python3.12/site-packages/agentic_portfolio/__init__.py
      $ ls /tmp/fresh/lib/python3.12/site-packages/ | grep -xE 'src|agentic_portfolio'
      agentic_portfolio                      <-- and no `src`
      13 entry points import and expose a callable main
      6 prompt files present and non-empty in the installed package

  That clean venv is also the only place in this session where the preflight's refusal path could
  be tested honestly, because `load_dotenv` can reach no `.env` by walking up from
  `/tmp/fresh/lib/python3.12/site-packages/crewai` - which is precisely the image's situation:

      $ env -u ANTHROPIC_API_KEY /tmp/fresh/bin/portfolio --date 2024-03-29 --value 1000
      ANTHROPIC_API_KEY is not set, so the LLM-S screening rule for --selection llm_s_only ...
      exit code: 2

      $ env -u SEC_UA /tmp/fresh/bin/portfolio-build-fundamentals
      SEC_UA is not set, so the book-equity figures cannot be fetched - the SEC requires a ...
      exit code: 2

  Before Step 5 the first of those reached `resolve_as_of_date` and died with a DuckDB
  `IOException`. `--selection user_provided` is correctly not blocked by either check.

  One number worth keeping for the deferred follow-up: the refusal takes **3.4 seconds**, nearly all
  of it importing `crewai`. That is the eager-import cost, and it is what making the import lazy
  would buy - visible here, invisible in the image.

- Observation: `uv build` writes a `dist/.gitignore` containing `*`, so the build directory
  self-ignores and no `.gitignore` change is needed for it.

- Observation: **there is no Docker inside the development container**, so Steps 9 and 10 cannot be
  executed or verified here at all. `command -v docker buildx podman nerdctl` finds nothing and
  `/var/run/docker.sock` does not exist. Everything in those steps is therefore *written* and
  reviewed but **unexercised**: the image has never been built, the multi-arch build has never
  run, and the secret-absence gate has never run. That gate in particular must be run before the
  first push, because a key in a published layer survives deleting the tag.

  What could be verified without Docker, and was: the `.dockerignore` by simulating its rules over
  the tree (76 files in, 24,407 out, nothing sensitive, `data/` contributing only
  `portfolio.duckdb`), and `docker/dataset_manifest.py` by running it against the real database.

- Observation: the development container's Dockerfile should not move to `.devcontainer/`, which
  the plan originally suggested. `.devcontainer` is listed in `.gitignore`, so a file placed there
  stops being version controlled. It went to `docker/Dockerfile.dev` instead, which keeps the two
  images separate and both tracked.

- Observation: a `[project.urls]` table placed above the `dependencies` list silently swallows it.
  TOML sub-tables run until the next table header, so `dependencies = [...]` became a *URL entry*
  and the build failed with `TypeError: URL 'dependencies' of field 'project.urls' must be a
  string` - a message that does not mention ordering at all. `[project.urls]` now sits after every
  plain key in `[project]`, with a comment saying why. Worth recording because the error text sends
  you looking at the URLs rather than at their position.

- Observation: `raise SystemExit("a message")` does **not** exit 2. It sets `.code` to the string
  and exits 1, printing the message. The preflight documented itself as using argparse's
  usage-error status, so the message and the status are now set separately - `print(..., file=
  sys.stderr)` then `raise SystemExit(2)`. Caught by measuring rather than by reading:

      $ ... preflight.require_api_keys('llm_s_only'); echo $?
      ANTHROPIC_API_KEY is not set, so the LLM-S screening rule ...
      1        <-- documented as 2

  `tests/test_preflight.py::test_the_refusal_exits_two_not_one` now asserts both the code and that
  the sentence still reaches stderr, so a later simplification back to `SystemExit(message)` fails.

- Observation: the dataset builders open the market-data database for writing, which conflicts with
  baking it into a read-only image layer. Evidence: `src/dataset/membership.py:241` and
  `src/dataset/prices.py:351,374,416` (pre-move paths) call `duckdb.connect(db_path)` with no
  `read_only=True`, while all roughly forty read sites do pass it. Left unhandled, a builder run
  against the baked path would copy forty-five megabytes into the container's ephemeral layer,
  appear to succeed, and discard the result when the container exits.

- Observation: the documentation churn is almost entirely in one place that should not be touched.
  Evidence: `grep -o 'src/' README.md | wc -l` is 2; `AGENTS.md` is 2; `PLANS.md` and `CLAUDE.md`
  are 0; `CREWAI.md` is 4 and they are generic CrewAI template paths, not this repository's files;
  `plans/` is 809.

- Observation: `crewai[tools]` is declared but never used, while `chromadb` cannot be avoided.
  Evidence: `src/agents/llm_s_crew/tools.py:20` imports `crewai.tools.BaseTool`, and
  `crewai/tools/base_tool.py` ships in crewai core - the `crewai-tools` distribution that the
  `[tools]` extra installs is never imported. But crewai core lists `chromadb~=1.1.0` as a direct
  requirement, so dropping the extra does not remove the embedding stack from the image.


- Observation: **`--help` is destructive on nine of the thirteen console scripts, and this cost a
  dataset.** Only `portfolio`, `portfolio-holdings`, `portfolio-summary` and
  `portfolio-migrate-candidates` build an `argparse` parser. The eight `portfolio-build-*` /
  `portfolio-backfill-snapshot` commands and `portfolio-backtest` ignore argv entirely and act
  immediately, and the builders open the market-data database **writable**
  (`dataset/membership.py:241`, `dataset/prices.py:351,374,416`). Verifying the rename with a loop
  of `uv run <script> --help` therefore re-ran `portfolio-build-membership` and
  `portfolio-build-prices` against the real `data/portfolio.duckdb`: membership re-fetched
  Wikipedia's *current* S&P 500 list, prices rebuilt against it, and the result lost 7,038 price
  rows. Evidence:

      prices rows          1,203,663 -> 1,196,625
      unresolved_tickers          55 -> 57
      sec_fallback_tickers         3 -> 2
      file size           46,936,064 -> 68,431,872 bytes

  Two aggravating factors worth learning from, beyond the flag itself. The loop was run as a
  **backgrounded** command, so it kept executing for roughly two minutes after the first sign of
  trouble - a destructive check that cannot be watched is worse than one that can. And there was no
  record of the dataset's prior state: only `prices` had been measured beforehand, so the other ten
  tables could not be checked for damage at all. That absence is the strongest argument for the
  `DATASET.json` provenance file in Step 9, which exists precisely so this question is answerable.

- Observation: the damage was fully recoverable offline, and the recovery corroborated itself. A
  pre-dividend backup held `prices` at exactly 1,203,663 - the pre-damage count - while the damaged
  file still held the four dividend tables, built before the accident against that same price
  history. Grafting the latter onto the former with the `ATTACH ... (READ_ONLY)` pattern from
  `flow/live.py:113-134` produced a file of **46,936,064 bytes, identical to the original size**.
  Nothing in the merge aimed at that number, so it is independent evidence that the reconstruction
  is exact. No network fetch was needed, which also means the dividend figures `README.md`
  documents are unchanged.

- Observation: two of the planned Step 4 mitigations turned out to be unnecessary, and one of them
  could not have worked. The plan called for extending `tests/conftest.py` to pin the new path
  fields and to `monkeypatch.delenv("AGENTIC_PORTFOLIO_HOME")`. Neither was needed: every test that
  touches these paths passes them **explicitly** as arguments or flags rather than relying on a
  default (`grep -rn '"memory/rates\.json"\|"data/portfolio\.duckdb"' tests/` shows only explicit
  call arguments), and `DEFAULT_RATES_PATH` still evaluates to the same string, so
  `tests/test_cli.py:1509` holds unchanged. 556 tests across the ten path-sensitive modules passed
  with no test edits at all. And the `delenv` would have been inert regardless: `_HOME` is read from
  `os.environ` at **settings-import time**, which happens when `conftest.py` itself imports the
  singleton - long before any fixture body runs. Anything wanting to neutralize that variable for a
  test run has to do it before that import, not in a fixture.

- Observation: two modules carried docstrings stating they deliberately avoided importing the
  settings singleton - `flow/rate_memory.py` ("what keeps this module free of a
  `agentic_portfolio.config.settings` import") and `flow/report_archive.py` ("Kept here as well so
  this module is usable without importing the settings singleton"). Step 4 contradicts both, and
  did so on purpose: a path constant that opted out of `AGENTIC_PORTFOLIO_HOME` would put reports
  somewhere different from the database and the memory files whenever that setting is used, so
  partial adoption would be the defect. The import is cheap - `config/settings.py` depends on
  nothing inside this project, so there is no cycle. Both docstrings were rewritten to state what is
  now true rather than left standing as false claims, and `resolve_risk_free_rate` itself is
  untouched: it still takes `saved` and `default` as parameters, which is the purity that note was
  actually protecting.

- Observation: the four tickers with no dividend coverage are unfixable by re-running, exactly as
  `README.md` claims, and the recovered dataset still says so in the application's own words:

      AVB: yfinance no longer serves this ticker's history for the requested 2015-01-01..2024-04-30
      EA:  (same)   EQR: (same)   LEG: (same)


## Decision Log

- Decision: rename the Python package from `src` to `agentic_portfolio`, using a src-layout
  (`src/agentic_portfolio/...`) rather than a flat layout (`agentic_portfolio/...` at the
  repository root).
  Rationale: `AGENTS.md` instructs that application code lives under `src/`, and a src-layout
  satisfies that literally. It also has a technical benefit the flat layout lacks: because the
  package is not at the repository root, `import agentic_portfolio` from the repository root
  resolves to the *installed* copy rather than the source tree, so a packaging bug - a prompt YAML
  file missing from the wheel, say - fails in our own test run instead of at a user's. Finally,
  `CREWAI.md:937` already documents `src/<project_name>/main.py` as CrewAI's own canonical layout,
  so this moves toward the repository's reference doc rather than away from it.
  Date/Author: 2026-09-12, agreed with the user before implementation.

- Decision: do the rename now, even though distribution is Docker-only and an image would tolerate
  the generic `src` package name.
  Rationale: inside an image the package is the only thing in `site-packages`, so nothing collides.
  But the rename is a one-way door whose cost grows with every plan file and docstring written
  against the old paths, and leaving it undone forecloses a future pip or PyPI release. Doing it
  while the tree is small and the test suite is green is the cheapest this will ever be.
  Date/Author: 2026-09-12, user's explicit choice when offered the option to skip it.

- Decision: keep every runtime path relative to the current working directory by default, and
  consolidate them behind one `AGENTIC_PORTFOLIO_HOME` setting that defaults to `"."`. Do not
  adop`platformdirs` or any per-user standard location.
  Rationale: the current behavior is already the right shape for a container, where `-w` chooses the
  working directory and `-v` chooses where it lands on the host; a per-user location would write
  inside the container and vanish on exit. Keeping the default byte-identical also means no test
  and no line of `README.md` has to change. The single home setting exists so a deployer has one
  knob, and so the image can override exactly one path - the market-data database - without
  disturbing the rest.
  Date/Author: 2026-09-12, agreed with the user.

- Decision: bake `data/portfolio.duckdb` into an image layer, accepting that this redistributes
  market data.
  Rationale: the backtest window is fixed history - `fetch_start` 2015-01-01, `fetch_end`
  2024-04-30, rebalance 2020-01-01 to 2024-04-30 - so a bundled dataset is not a stale snapshot of
  something live; it is exactly correct, permanently. Without it the project's headline capability,
  reproducing the paper's table, requires a multi-hour rate-limited build and an SEC User-Agent
  before a new user sees anything work. The file is about forty-five megabytes against a
  multi-gigabyte dependency layer, so size is not the consideration.
  The licensing position, recorded deliberately rather than passed over: SEC EDGAR filing data is
  public domain; the S&P 500 membership list derives from Wikipedia and is CC-BY-SA, which this plan
  attributes in `NOTICE`; the prices are Yahoo Finance-derived and Yahoo's terms of service restrict
  redistribution. The user has chosen to bundle them anyway, for non-commercial reproduction of a
  published result, and `NOTICE` says so plainly and offers the rebuild-your-own path as the
  clean-hands alternative for anyone who would rather not rely on our copy.
  Date/Author: 2026-09-12, user's proposal, licensing caveat raised by the assistant and accepted.

- Decision: add preflight checks for the API keys and the SEC User-Agent, but do **not** add a
  `portfolio-init` command.
  Rationale: with the dataset baked in and `DB_PATH` pointing at it, the missing-database failures
  disappear, and `memory/` and `output/` are already created on first write. What remains is the
  late, opaque API-key failure, and a check is a much smaller change than a new entry point. This
  repository already contains the pattern to copy, in `api_key_problem()`.
  Date/Author: 2026-09-12, agreed with the user.

- Decision: MIT for the source code, with data terms handled in a separate `NOTICE` file.
  Rationale: the cited paper's methodology is not copyrightable, so the arXiv citation is an
  attribution obligation - already met in `README.md` - not a licensing one, and a research-only or
  copyleft license would impose restrictions the upstream work never did. The real exposure is the
  bundled *data*, which no source license can grant rights to, so it is addressed in prose where a
  reader will actually look. Apache-2.0 would be the reasonable alternative if a formal patent grant
  were wanted; MIT is chosen for brevity in a single-author academic reproduction.
  Date/Author: 2026-09-12.

- Decision: publish by a documented manual `docker buildx` push; do not add a GitHub Actions
  workflow.
  Rationale: `data/portfolio.duckdb` is not tracked by git, so a CI runner could not build the image
  at all - automating this would first require solving dataset hosting, which is out of scope. The
  command is recorded in `docker/README.md` so it does not live only in someone's shell history.
  Date/Author: 2026-09-12.

- Decision: recover the damaged dataset by grafting the dividend tables onto the backup, rather
  than rebuilding it with the eight builders.
  Rationale: the backup's `prices` count matches the pre-damage count exactly, so its price,
  membership, factor and returns data is provably the original; the damaged file's dividend tables
  predate the accident and were built against that same price history, so the two are mutually
  consistent. A rebuild would have been slower, would have needed the rate-limited SEC pass, and -
  decisively - would have refetched *current* dividend data, changing figures that `README.md`
  documents and that the saved reports under `output/2026-09/` were computed against. The merge is
  offline and changes no figure. Both input files are kept rather than deleted, since `data/` is
  gitignored and costs nothing to retain.
  Date/Author: 2026-09-12, after verifying all three files read-only.

- Decision: verify console-script wiring by importing each entry point and asserting `main` is
  callable, never by invoking `--help`.
  Rationale: `--help` is not a safe probe here - nine of the thirteen commands ignore argv and act,
  and the builders write to the market-data database. The import check tests the thing a rename can
  actually break (a script pointing at a module path that no longer exists) while executing
  nothing. Both places in this plan that reached for `--help` on all thirteen have been replaced,
  and a warning now sits at each.
  Date/Author: 2026-09-12, after the incident recorded in `Surprises & Discoveries`.

- Decision: keep the development container and the release image as separate Dockerfiles rather than
  sharing stages.
  Rationale: they optimize for opposite things. The development image deliberately does *not*
  install the project (so edits take effect without reinstalling) and carries node plus a coding
  agent; the release image must install the project, run non-root, and carry nothing extra. Sharing
  a base would drag node into the release image or non-root semantics into the development loop.
  `.devcontainer` is also gitignored, so it cannot be the published build path in any case.
  Date/Author: 2026-09-12.


## Outcomes & Retrospective

### At the end of Step 7 (2026-09-12): the package is real

The half of the Purpose section that does not need Docker is delivered and proven. A wheel built
from this tree, installed into a fresh virtual environment outside the repository, puts
`agentic_portfolio` into `site-packages` with no top-level `src`, exposes all thirteen console
scripts, and carries all six CrewAI prompt files. The test suite went from 1049 to 1081 passing with
no regressions, the runtime dependency list went from 21 declarations to 9, and the locked
distribution count from 204 to 166.

Two things are better than the plan asked for. `DB_PATH` now actually works - it was inert before,
because two argparse defaults hardcoded the path string, so the environment variable the README
documented did nothing. And a run that lacks a credential refuses in about a second naming the
variable, where before it either died with a DuckDB `IOException` after reaching the database or
with a provider traceback minutes into a fetch.

One thing is worse than the plan assumed: **Steps 9 and 10 cannot be finished in this environment
at all**, because the development container has no Docker. The Dockerfile, the manifest generator,
the checksum and the publish instructions are all written, and the two pieces that could be checked
without a daemon were checked - but the image has never been built and the secret-absence gate has
never run. Anyone picking this up should treat that gate as the first task, not the last, because a
key in a published layer survives deleting the tag.

### Lessons worth carrying forward

**Measure, then write it down; do not reason and write it down.** Three claims in this document were
wrong when first written and each was corrected only by running something: that a `.env` file could
not supply the LLM keys, then that CrewAI resolved `.env` from the working directory, and that
`SystemExit("message")` exits 2. The `.env` question took three attempts because each measurement
answered a narrower question than the one that mattered. The version that finally held up explains
the *mechanism* - python-dotenv searching upward from the calling module's own file - and that
version is the one that predicted the image would behave differently, which is the whole reason it
matters.

**A verification step can be more dangerous than the change it verifies.** The single most costly
event in this work was not a code change; it was checking the rename by running `--help` on all
thirteen console scripts, nine of which ignore argv and act immediately. That destroyed 7,038 price
rows. Two aggravating factors are worth naming: the check ran in the background, so it kept going
for about two minutes after the first sign of trouble, and there was no record of the dataset's
prior state, so the damage could not be fully assessed afterwards. The first is a habit to change;
the second is why `DATASET.json` exists.

**A backup nobody has verified is still worth having.** The recovery worked because a pre-dividend
backup existed and its price count matched the pre-damage figure exactly. Neither file was correct
alone - the backup lacked four tables, the damaged copy lacked 7,038 rows - but together they were
complete, and the merged result came out byte-for-byte the size of the original, which nothing in
the merge was aiming at.

**Documented decisions deserve to be overridden explicitly or not at all.** Two modules carried
docstrings stating they avoided importing the settings singleton. The right move was neither to
quietly contradict them nor to preserve a partial feature, but to override them and rewrite the
docstrings to say what is now true and why.

### Still to do

Step 9's build and verification, and Step 10's publish, on a machine with Docker. Also the deferred
follow-up in `Progress`: making the `crewai` import lazy, now quantified at **3.4 seconds** of
startup for a run that needs no LLM at all.


## Context and Orientation

This section describes the repository as it stands before any of this plan's work, assuming you know
nothing about it.

**What the project does.** It reproduces a paper on using large language models to screen stocks for
a portfolio. Two agents do the screening: LLM-S reads company fundamentals and writes screening
rules, and LLM-F reads news headlines and scores sentiment. Their output is a set of candidate
tickers, which an optimizer turns into portfolio weights, which a final step turns into a number of
whole shares to buy. There is a Backtest Mode that runs the whole thing over 2020 to 2024 against
historical data, and a Live Mode that fetches current data.

**How it is invoked.** Entirely from the command line. `pyproject.toml` has a `[project.scripts]`
table declaring thirteen console scripts - `portfolio`, `portfolio-backtest`, `portfolio-holdings`,
`portfolio-summary`, `portfolio-migrate-candidates`, and eight dataset builders named
`portfolio-build-*` and `portfolio-backfill-snapshot`. A console script is an executable that a
Python install creates on your `PATH`, which imports a named function and calls it; the declaration
`portfolio = "src.flow.cli:main"` means the `portfolio` command calls `main()` in
`src/flow/cli.py`. `README.md` documents each one and its flags under its `# How to Use` heading.

**Where data lives.** DuckDB is an embedded analytical database - one file on disk, no server. The
project uses three files plus two directories, all relative to the current working directory:

- `data/portfolio.duckdb` - the shared market-data cache: prices, monthly returns, computed
  factors, S&P 500 membership history, dividends, splits, and a table of news articles. Built by the
  `portfolio-build-*` commands. About forty-five megabytes. Not tracked by git.
- `data/holdings.duckdb` - a separate cache for the prices and returns of the tickers the user
  personally owns. Deliberately a different file so that reporting on your own holdings can never
  add rows to the shared cache, because the window a candidate pool is measured over is derived from
  every row in that shared table.
- `data/news_archive_source.parquet` - a downloaded file that the news-archive builder reads to
  populate the news table inside `data/portfolio.duckdb`.
- `memory/` - three small JSON files holding the user's candidate pools, their own holdings, and a
  remembered risk-free rate per currency.
- `output/<year>-<month>/` - every report the program prints is also saved here as Markdown.

`data/`, `memory/` and `output/` are all listed in `.gitignore`, and nothing under them is tracked.

**How configuration works.** `src/config/settings.py` defines a `Settings` class using
`pydantic-settings`, a library that populates an object's fields from environment variables and from
a `.env` file. Field names match environment variable names case-insensitively, so a field named
`db_path` is set by `DB_PATH`. The class is instantiated once at the bottom of the module as
`settings`, and roughly forty functions elsewhere use `settings.<field>` as a **default argument
value** - which means those defaults are fixed at import time and cannot be changed afterwards by
assigning to the singleton. That constraint shapes Step 4.

**The three environment variables that matter.** `SEC_UA` is a descriptive User-Agent string that
the U.S. Securities and Exchange Commission requires; without it their server returns HTTP 403.
`ANTHROPIC_API_KEY` authenticates the LLM-S and LLM-F calls. `OPENAI_API_KEY` is needed only by
`portfolio-summary`'s optional prose and can be skipped with `--no-llm`.

**The tests.** Twenty-seven files under `tests/`, plus one `tests/conftest.py` holding a single
automatically-applied fixture that redirects the report output directory into a temporary path. 1049
tests, all hermetic - they use temporary directories and mock every network call. The whole suite
takes about seven minutes. `pyproject.toml` sets `pythonpath = ["."]`, which is what lets a test
write `from src.flow.cli import ...` and have it resolve to the source tree.

**The container situation.** `Dockerfile` at the repository root is a *development* container. It
installs dependencies with `uv sync --frozen --no-install-project` - the flag means "install what
this project depends on, but not this project itself" - so **none of the thirteen console scripts
exist inside it**. It also installs node and a coding agent. `.devcontainer/devcontainer.json` does
not even build it; it references a pre-built local image and bind-mounts the author's home
directory by absolute path. There is **no `.dockerignore` file at all**, which means a Docker build
today would send the entire directory as build context, including a 1.4 GB virtual environment and
the author's `.env` containing live API keys.

**Terms used below.** *Build context* is the set of files Docker uploads to the builder before
running a build; `.dockerignore` controls it. A *layer* is one filesystem change in an image,
cached independently, and crucially **a file deleted in a later layer still exists in the earlier
one** - which is why proving an image contains no secret requires inspecting layers, not just
listing the final filesystem. *Multi-arch* means one image tag that resolves to different builds for
Intel (`linux/amd64`) and ARM (`linux/arm64`) machines. *GHCR* is the GitHub Container Registry, at
`ghcr.io`. An *editable install* is a Python install that points at your source tree instead of
copying it, so edits take effect immediately.


## Plan of Work

The order matters: the Python package must be correct before an image can install it, and the
`.dockerignore` must exist before the first build so that no build ever sees the author's secrets.

Steps 1 through 3 are one mechanical unit - the package move - and the tree is broken in the middle
of them, so run them together and gate on collection at the end. Step 4 changes behavior in a way
designed to be a no-op for existing users. Step 5 adds new behavior. Steps 6 and 7 are metadata.
Steps 8 through 10 build and publish.

Each step ends at a state you can verify and commit. `AGENTS.md` says not to commit unless the user
asks, so prepare the commits and ask.


## Concrete Steps

All commands run from the repository root, `/app/agentic_portfolio`, unless stated otherwise.


### Step 0 - Baseline

Confirm the working tree is clean and record the test count you must not regress:

    $ git status --short
    $ uv run pytest -q 2>&1 | tail -3
    1049 passed, 3 warnings in 421.48s (0:07:01)

The three warnings are pre-existing and unrelated - PyPortfolioOpt warning about NaN returns in
three `tests/test_holdings.py` cases that deliberately construct a thin price history. Expect them
to remain.

Then remove compiled bytecode, so that no later step is accidentally validated against a stale
cache of the old module names:

    $ find src tests -name __pycache__ -prune -exec rm -rf {} +
    $ rm -rf .pytest_cache

Do not skip the baseline. After Step 2 you will have changed 68 files mechanically, and "1049
passed" is the only thing that distinguishes a correct rewrite from a subtly broken one.


### Step 1 - Move the package

`src` is currently both the containing directory and the Python package name, so a single `git mv`
cannot nest it inside itself. Use a temporary directory:

    $ mkdir -p .rename_tmp
    $ git mv src .rename_tmp/agentic_portfolio
    $ git mv .rename_tmp src

Confirm git recorded renames rather than deletions plus additions:

    $ git diff --cached -M --stat --summary | tail -3

You should see 58 files renamed and no content changes. Keep
`src/agentic_portfolio/__init__.py` - it is the marker that makes the directory a regular package,
and removing it would invite ambiguity with implicit namespace packages during the wheel build.

Do **not** add a `__version__` string to that file. `version = "0.1.0"` in `pyproject.toml` is
already the single source of truth, nothing in the code reads a version today, and a hand-written
literal in a second place drifts silently. If a `--version` flag is wanted later, read
`importlib.metadata.version("agentic-portfolio")`, which works precisely because Step 3 makes the
project properly installed.


### Step 2 - Rewrite the references

There are 527 occurrences of `src.` and 314 of `src/` across 75 files. They are four different
kinds of thing and need four different rules. **Never use a bare `\bsrc\b` pattern.**

The kinds:

1. *Import statements*, 260 of them - `from src.flow.cli import main`,
   `import src.dataset.prices`. Note there is no `from src import <name>` anywhere and no bare
   `import src`: `src/errors.py` is a real module but is always reached as
   `from src.errors import ...`, which the first rule below already covers. The
   `from src import` rule is kept only as a guard in case one is added later.
2. *String monkeypatch targets in tests*, 248 of them - `monkeypatch.setattr("src.flow.cli.X", ...)`.
   `monkeypatch.setattr` accepts a dotted string naming what to replace; these are module paths
   written as text, so no import-anchored rule will find them. They are concentrated in
   `tests/test_cli.py`, `tests/test_interactive_flow.py` and `tests/test_holdings_cli.py`.
3. *Backticked module references in docstrings*, 4 of them - `` `src.flow.rate_memory` ``.
4. *Prose file-path references in docstrings*, 314 of them - "see `src/flow/rate_memory.py`".
5. *Module-invocation references*, 13 of them - `python -m src.dataset.membership`. These are real
   module paths, so they break if not rewritten, and two of them are inside **runtime error
   messages a user sees** (`dataset/prices.py:357`, `dataset/momentum.py:98` both tell the user to
   run a builder first). One more, in `dataset/sec_edgar.py:60`, names
   `src.config.settings.settings` in prose after the word "via". This category was missed on the
   first pass and found by the gate below - see `Surprises & Discoveries`.

The rules, applied to tracked Python files only. Note the pathspec must reach nested directories,
so filter the file list rather than relying on `src/*.py` matching recursively:

    $ git ls-files -z 'src' 'tests' | tr '\0' '\n' | grep '\.py$' | tr '\n' '\0' \
      | xargs -0 sed -i -E \
        -e 's|\bfrom src\.|from agentic_portfolio.|g' \
        -e 's|\bfrom src import\b|from agentic_portfolio import|g' \
        -e 's|\bimport src\.|import agentic_portfolio.|g' \
        -e 's|"src\.|"agentic_portfolio.|g' \
        -e 's|`src\.|`agentic_portfolio.|g' \
        -e 's|src/|src/agentic_portfolio/|g' \
        -e 's|-m src\.|-m agentic_portfolio.|g' \
        -e 's|via src\.config|via agentic_portfolio.config|g'

The six CrewAI YAML files need no rewriting - confirmed with
`git ls-files 'src' | grep '\.ya\?ml$' | xargs grep -l 'src[./]'`, which matches nothing.

**The quote anchor in the fourth rule is load-bearing, and here is why.**
`src/agentic_portfolio/flow/live.py` around lines 123 to 125 contains this:

    con.execute(f"ATTACH '{escaped_path}' AS src (READ_ONLY)")
    ...
    con.execute("CREATE TABLE news_articles_hf AS SELECT * FROM src.news_articles_hf")

In that SQL, `src` is a DuckDB database alias created by the `ATTACH` on the line above - it has
nothing to do with Python modules. The `"src.` pattern only matches a quote immediately followed by
`src.`, so it matches `"src.flow.cli.X"` in a monkeypatch call but not `FROM src.news_articles_hf`
in the middle of a string. An unanchored rule would rewrite that SQL into
`FROM agentic_portfolio.news_articles_hf`, which is still valid Python and completely broken
DuckDB - and the test that exercises it, `tests/test_interactive_flow.py:833-864`, mocks the
builders around it, so it may not fail loudly. Read that file by hand after the rewrite:

    $ grep -n "AS src\|FROM src\." src/agentic_portfolio/flow/live.py

Both lines must still say `src`. Then gate the whole rewrite:

    $ grep -rn 'src\.' src tests --include=*.py
    # expect ONLY the two live.py SQL lines above

    $ grep -rn 'src/' src tests --include=*.py | grep -v 'src/agentic_portfolio/'
    # expect no output

The sixth rule is idempotent in the wrong direction if run twice - it would produce
`src/agentic_portfolio/agentic_portfolio/`. If you need to re-run, start from
`git checkout -- src tests` and redo Step 2 from a clean state.


### Step 3 - Wire up `pyproject.toml`, and update the two docs that need it

Rewrite all thirteen script targets:

    $ sed -i 's|= "src\.|= "agentic_portfolio.|' pyproject.toml
    $ grep -c 'agentic_portfolio\..*:main' pyproject.toml     # expect 13

In `[tool.hatch.build.targets.wheel]`, replace `packages = ["src"]` with
`packages = ["src/agentic_portfolio"]`, and **replace the two-line comment above it**, which
justifies the old design and becomes actively misleading. The new comment should record three
things: that this is a standard src-layout whose installed top-level package is
`agentic_portfolio`; that the six `agents/*_crew/config/*.yaml` files ship because hatchling
includes non-Python files found inside a `packages` directory; and that CrewAI resolves those YAML
files relative to each crew module's own file location rather than the working directory, which is
why they must stay beside their crew module and why the wheel must contain them.

Delete the `pythonpath = ["."]` line from `[tool.pytest.ini_options]`. After the rename it would
have to point at `src`, which would let the test suite pass against a source tree that was never
packaged - exactly the class of bug this plan exists to close. `uv run pytest` installs the project
into `.venv` first, so tests will import the installed package instead. Verify:

    $ uv sync
    $ uv run python -c "import agentic_portfolio.flow.cli as m; print(m.__file__)"
    $ uv run portfolio --help | head -3

`portfolio` is one of the four commands that parse arguments, so `--help` is safe there. **Do not
extend that check into a loop over every console script.** Nine of the thirteen ignore argv and act
immediately - the eight builders write to the market-data database and `portfolio-backtest` runs a
full backtest. The safe way to verify all thirteen is the import check in
`Validation and Acceptance`, which executes nothing.

**Documentation.** Update only `README.md` (2 references, at lines 16 and 24) and `AGENTS.md`
(2 references; line 4 states the real convention, line 10 is an illustrative example path). Leave
`plans/01` through `plans/19` and `CREWAI.md` untouched, for the reasons given at the top of this
document. While editing `AGENTS.md`, add one sentence noting that the src-layout matches the
canonical CrewAI project layout already documented in `CREWAI.md`.

**Gate.** Collection is the fast, high-signal check that Steps 1 to 3 are complete - it imports
every module without running any test:

    $ uv run pytest --collect-only -q 2>&1 | tail -2
    1049 tests collected in 4.21s

Then a smoke subset, about eighty-five tests and well under a minute:

    $ uv run pytest tests/test_optimizer.py tests/test_summary_cli.py -q


### Step 4 - Consolidate the seven path literals behind `AGENTIC_PORTFOLIO_HOME`

The goal is that every path the program uses is derived from one setting, so the image can override
exactly one of them. The default must remain byte-identical to today's, because tests and printed
output both depend on the exact strings.

In `src/agentic_portfolio/config/settings.py`, above the `Settings` class:

    _HOME = os.environ.get("AGENTIC_PORTFOLIO_HOME", ".")


    def _under_home(relative: str) -> str:
        return relative if _HOME in ("", ".") else str(Path(_HOME) / relative)

The identity branch is not cosmetic. `str(Path(".") / "data/portfolio.duckdb")` yields
`data/portfolio.duckdb` on this Python version, but relying on that is fragile, and an explicit
branch also documents the intent: when home is unset, nothing changes at all. There is a test that
asserts the exact string - `tests/test_interactive_flow.py:397` checks
`db_path == "data/portfolio.duckdb"` - and printed reports quote these paths to the user.

Read home from `os.environ` rather than declaring it as a field first, because `model_config` is
evaluated as part of the class body and cannot see field values - and `model_config` needs it:

    model_config = SettingsConfigDict(
        env_file=_under_home(".env"), env_file_encoding="utf-8", extra="ignore"
    )

Without that, `AGENTIC_PORTFOLIO_HOME` would relocate the data but still look for `.env` in the
working directory only.

Then declare the home setting as an ordinary field so it is introspectable and documented:

    agentic_portfolio_home: str = _HOME

**Do not name that field `home`.** `case_sensitive` is not set, so pydantic-settings matches field
names to environment variables case-insensitively - a field named `home` would silently absorb the
value of `$HOME`, which is set on every Unix system, and every path in the program would move to the
user's home directory.

Give all seven paths fields whose defaults call `_under_home`: the existing `db_path` and
`output_dir`, plus five new ones - `holdings_db_path`, `news_archive_path`, `candidates_path`,
`rates_path`, `portfolio_path`. Precedence then works correctly with no extra code: an environment
variable or `.env` entry replaces the field default outright, and a command-line flag overrides that
when the parser is built. This is why the derivation happens inside the default *expression* rather
than in a validator or a `default_factory` - the roughly forty functions that use `settings.<field>`
as an argument default capture the value at import time, so it has to be right by then.

Now point every existing definition at those fields. Five module-level constants become assignments,
so that all existing imports and tests that refer to them by name keep working:

- `src/agentic_portfolio/dataset/holdings_cache.py:62` -
  `DEFAULT_HOLDINGS_CACHE_PATH = settings.holdings_db_path`
- `src/agentic_portfolio/flow/candidate_memory.py:44` -
  `DEFAULT_CANDIDATES_PATH = settings.candidates_path`
- `src/agentic_portfolio/flow/rate_memory.py:63` - `DEFAULT_RATES_PATH = settings.rates_path`
- `src/agentic_portfolio/flow/user_portfolio.py:56` -
  `DEFAULT_PORTFOLIO_PATH = settings.portfolio_path`
- `src/agentic_portfolio/flow/report_archive.py:80` - `DEFAULT_OUTPUT_DIR = settings.output_dir`

And six inline literals become references:

- `src/agentic_portfolio/flow/interactive.py:142` and `:1541` - `settings.db_path`
- `src/agentic_portfolio/flow/backtest.py:115` - `settings.db_path`
- `src/agentic_portfolio/dataset/news_archive.py:41` and `:64` - `settings.news_archive_path`
- **`src/agentic_portfolio/flow/cli.py:2433`** and
  **`src/agentic_portfolio/flow/holdings_cli.py:895`** - `settings.db_path`. These two are the
  actual bug: they hardcode `default="data/portfolio.duckdb"` in `add_argument`, which is why the
  `DB_PATH` environment variable does nothing today even though `settings.db_path` reads it. Fixing
  these is what makes the baked dataset reachable in Step 9.

**Handle the new test exposure.** Once `--db-path`'s default comes from `settings`, a developer's
`.env` containing `DB_PATH` would change the behavior of roughly fifty argparse-driven tests in
`tests/test_cli.py`. The suite is green today only because the current `.env` happens to set just
the four API and User-Agent variables - that is luck, not design, and it will bite whoever adds
`DB_PATH` to their `.env` next. Extend the automatically-applied fixture in `tests/conftest.py`,
which already pins `settings.output_dir` for exactly this reason, to pin all the new path fields to
children of `tmp_path` and to call `monkeypatch.delenv("AGENTIC_PORTFOLIO_HOME", raising=False)`.
Update its docstring to say so. Three test docstrings claim the defaults are literals -
`tests/test_cli.py:838`, `tests/test_cli.py:1509`, `tests/test_holdings_cli.py:84-89` - and remain
true but should be reworded to say the default now comes from `Settings`.

**Gate.** The argparse-heavy quarter of the suite, about 403 tests:

    $ uv run pytest tests/test_cli.py tests/test_holdings_cli.py \
        tests/test_interactive_flow.py tests/test_holdings.py -q

Plus two live checks proving both override paths work:

    $ AGENTIC_PORTFOLIO_HOME=/tmp/x uv run portfolio --help | grep -A1 'db-path'
    # expect /tmp/x/data/portfolio.duckdb

    $ DB_PATH=/ro/portfolio.duckdb uv run portfolio --help | grep -A1 'db-path'
    # expect /ro/portfolio.duckdb


### Step 5 - Preflight checks

Create `src/agentic_portfolio/config/preflight.py`. It goes in `config/` rather than `agents/`
because it must be importable without loading crewai, and `agents/report_summary.py` is not.

Move `api_key_problem(model)` there verbatim from `agents/report_summary.py:69-92`, keeping its
docstring and, critically, its lookup order: it checks the `Settings` field **before**
`os.environ`, because pydantic-settings loads `.env` without exporting it, so a check against the
environment alone would refuse to run on a machine whose key lives only in `.env`. Re-import the
name into `report_summary.py` so that `tests/test_report_summary_agent.py:138,146-148` and any
monkeypatch target keep resolving. Then add two functions:

    def require_api_keys(selection: str) -> None: ...
    def require_sec_user_agent() -> None: ...

`require_api_keys` checks the LLM-S model for selections `llm_s_only` and `llm_s_and_f`, the LLM-F
model for `llm_f_only` and `llm_s_and_f`, and nothing at all for `user_provided`. Both functions
raise `SystemExit(2)` with the message, matching argparse's exit code for a usage error so that a
configuration problem stays distinguishable from a data failure.

Call sites: `flow/cli.py:main()` and `flow/backtest.py:main()`, immediately after `parse_args()` and
before anything opens a database or fetches anything. `require_sec_user_agent()` goes at the top of
`dataset/fundamentals.py:main()` and `dataset/backfill_snapshot.py:main()`, and in
`flow/interactive.py`'s live-snapshot entry point, which reaches the SEC indirectly through
`build_factors`. Leave `summary_cli.py` alone - it already degrades to printing its tables without
prose, which is correct behavior that must not become a hard failure.

**One prerequisite.** `agents/llm_f.py:108` reads `os.environ.get("LLM_F_MODEL", DEFAULT_MODEL)`
directly, so there is no single source of truth for which model LLM-F uses. Add an `llm_f_model`
field to `Settings`, mirroring the existing `llm_s_model`, and have `llm_f.py` read it. Otherwise
the preflight has to duplicate the resolution logic and the two will drift.

**Wording.** Copy the style of `api_key_problem` exactly - one sentence, no trailing period, naming
the variable, saying what cannot happen, and offering concrete ways out:

    ANTHROPIC_API_KEY is not set, so the LLM-S screening rule for --selection llm_s_only cannot
    be generated with anthropic/claude-sonnet-4-5. Set it in .env or the environment, choose
    another model with LLM_S_MODEL, or re-run with --selection user_provided

**Also close the key-forwarding gap, with `export_api_keys()`.** Nothing in `agents/` passes a key
to the LLM client; the SDKs read `os.environ`, and pydantic-settings does not export `.env`. That
works today only because CrewAI's own `load_dotenv()` finds `<repo>/.env` by searching upward from
its installed file inside `<repo>/.venv/` - an accident of checkout layout that **does not hold in
the image**, where the virtual environment is at `/opt/agentic-portfolio/venv` and the user's `.env`
is in the mounted `/work`. See `Surprises & Discoveries` for the measurement. So
`export_api_keys()` copies any provider key that `Settings` holds and the environment lacks into
`os.environ`, never overriding one already set, and `require_api_keys` calls it first. That makes
the preflight's verdict the same question the SDK will ask, in every layout.

**Close the CrewAI missing-config trap too.** CrewAI's `_load_config` logs a warning and returns an
empty dictionary when a YAML file is missing, so `agents/llm_s_crew/crew.py:41` then fails with a
bare `KeyError: 'strategy_agent'` that says nothing about a missing file. Since Step 9 puts those
YAML files inside a wheel inside an image, a clear error matters. Add a small
`_require(config, key, path)` helper used by all three crews that raises a message naming the file
that should have been there.

**Tests.** New `tests/test_preflight.py`: the four selections crossed with key present and absent,
asserting the exit code and that the message names both the variable and the model; monkeypatching
both `settings.anthropic_api_key` and `os.environ` to pin the two-source order; and two tests at
`main()` level that patch `sys.argv`, expect `SystemExit`, and assert **no DuckDB file was opened**
before the refusal - that last assertion is the whole point of the feature.

    $ uv run pytest tests/test_preflight.py tests/test_report_summary_agent.py \
        tests/test_summary_cli.py -q


### Step 6 - Trim dependencies

Twelve of the twenty-one declared runtime dependencies are imported nowhere in `src/` or `tests/`.
The final runtime list is nine entries, one per package actually imported:

    dependencies = [
        "crewai[anthropic]>=1.15.18",
        "duckdb>=1.5.2",
        "numpy>=2.4.6",
        "pandas>=3.0.3",
        "pydantic>=2.12.5",
        "pydantic-settings>=2.14.2",
        "pyportfolioopt>=1.6.0",
        "requests>=2.34.2",
        "yfinance>=1.3.0",
    ]

Removed: `aisuite`, `exa-py`, `markdown`, `matplotlib`, `parsel`, `redis`, `regex`, `seaborn`,
`stockstats`, `questionary` (the interactive loops use plain `input()`), `python-dotenv` (superseded
by pydantic-settings' own `.env` support), and `pytest` (a test dependency, not a runtime one).
Three narrower reductions, each verified:

- The `[tools]` extra goes. `agents/llm_s_crew/tools.py` imports `crewai.tools.BaseTool`, which is
  part of crewai core; the separate `crewai-tools` distribution is never imported. Do not expect
  this to shrink the image much - crewai core requires `chromadb` directly.
- The `[azure-ai-inference]` and `[google-genai]` extras go; `[anthropic]` stays, because crewai
  1.15 lists neither litellm nor the Anthropic SDK in its core requirements, so an
  `anthropic/claude-*` model needs that extra present.
- `pydantic`'s `[email,timezone]` extras go; no `EmailStr` or timezone-aware types appear in `src/`.

Test-only dependencies go in a PEP 735 dependency group, not in `[project.optional-dependencies]`:

    [dependency-groups]
    dev = ["pytest>=9.0.3", "cvxpy>=1.7"]

`uv sync` and `uv run` install the `dev` group by default, so `uv run pytest` keeps working with no
change to anyone's workflow, while the image builds with `--no-dev` and gets a genuinely smaller
tree. An extra would have to be named explicitly in both places and would leak into published
metadata for a package we are deliberately not publishing. `cvxpy` belongs there because
`tests/test_optimizer.py` imports it directly while nothing declares it - it is satisfied only
transitively through PyPortfolioOpt today, which breaks the moment PyPortfolioOpt changes solvers.

    $ uv lock && uv sync
    $ uv run pytest tests/test_optimizer.py tests/test_llm_s.py tests/test_llm_f.py -q
    $ uv run python -c "import agentic_portfolio.agents.llm_s_crew.crew"

That last import is the real gate on the extras: it proves the pruned crewai still satisfies the
crews.


### Step 7 - Metadata, `LICENSE`, `NOTICE`

Fill in the `[project]` table: `description`, `readme = "README.md"`, `authors`, `keywords`,
`classifiers`, and `[project.urls]`. Add `[tool.hatch.build.targets.sdist]` naming `src`, `tests`,
`README.md`, `AGENTS.md`, `PLANS.md`, `plans`, `LICENSE` and `NOTICE`.

Include `"Private :: Do Not Upload"` among the classifiers. PyPI rejects any distribution carrying
that classifier, which is a hard guard against an accidental publish of a package whose distribution
is deliberately Docker-only.

Add `LICENSE` containing the MIT license text, and `NOTICE` recording the data provenance: SEC EDGAR
filing data is public domain; the S&P 500 membership list derives from Wikipedia and is CC-BY-SA,
attributed here; the price history is Yahoo Finance-derived and Yahoo's terms restrict
redistribution, and it is bundled for non-commercial reproduction of a published result, with the
`portfolio-build-*` commands available to anyone who prefers to build their own. State explicitly
that the MIT license covers this repository's source code only and grants no rights in the data.

On PEP 639: `license = "MIT"` together with `license-files = ["LICENSE"]` requires a hatchling new
enough to emit metadata 2.4. If `uv build` rejects the SPDX string, fall back to
`license = {file = "LICENSE"}` and drop `license-files`. Do not add a
`License :: OSI Approved :: MIT License` classifier alongside the PEP 639 fields - that combination
is an error.


### Step 8 - `.dockerignore`

Write this before the Dockerfile, so that no build ever sees the author's secrets. Create
`.dockerignore` at the repository root:

    # Deny everything, then allow only what the build needs. The leading `*` is the point:
    # a new file at the repository root is excluded by default and can only enter the build
    # context by someone editing this file. An allowlist of denials cannot give that property.
    #
    # Second, independent barrier: the Dockerfile never uses `COPY . .` - every COPY names an
    # explicit path. So a mistake here alone cannot leak anything; it would take two mistakes.
    *

    !pyproject.toml
    !uv.lock
    !README.md
    !LICENSE
    !NOTICE
    !src/
    !docker/

    # data/ must be un-ignored for Docker to traverse into it, then its contents re-denied,
    # so that exactly one file can be re-admitted by name. holdings.duckdb is a price cache
    # for only the tickers the author personally owns; news_archive_source.parquet is 9.2 MB
    # already materialized into the news table inside portfolio.duckdb.
    !data/
    data/*
    !data/portfolio.duckdb

    **/__pycache__/
    **/*.pyc

Verify what the context would now contain before building anything:

    $ docker build --no-cache -f /dev/stdin -t ctx-check . <<'EOF'
    FROM busybox
    COPY . /ctx
    RUN find /ctx -type f | sort
    EOF

Read that list. It must contain `pyproject.toml`, `uv.lock`, `README.md`, `LICENSE`, `NOTICE`,
`data/portfolio.duckdb`, the files under `src/` and `docker/`, and nothing else. Specifically it must
not contain `.env`, anything under `memory/` or `output/`, `data/holdings.duckdb`,
`data/news_archive_source.parquet`, `.venv`, or `.git`.


### Step 9 - The runtime `Dockerfile`

Replace the root `Dockerfile` with a release image, and move the existing development container to
`.devcontainer/Dockerfile` unchanged. Four stages on `python:3.12-slim-bookworm`, pinned to a
specific patch release. Alpine is not an option: its musl C library means the pre-built wheels for
numpy, scipy, cvxpy, duckdb and curl_cffi do not apply, so those would compile from source - under
emulation, for the ARM build. A distroless base is not an option either, because the agreed
interface includes `docker run IMAGE bash`.

- **`deps`** - `uv sync --frozen --no-dev --no-install-project` into
  `/opt/agentic-portfolio/venv`. This stage copies only `pyproject.toml` and `uv.lock`, so the
  multi-gigabyte dependency layer is cached independently of every source change. `build-essential`
  and `python3-dev` are installed here and nowhere else. Use a pinned `uv` image, not `:latest`.
- **`wheel`** - copies `src`, `pyproject.toml`, `uv.lock`, `README.md`, then runs
  `uv build --wheel`. Tiny, and rebuilt on every source change.
- **`provenance`** - runs a new `docker/dataset_manifest.py` against the dataset to produce
  `DATASET.json`.
- **`runtime`** - installs only `libgomp1` and `libatomic1`; copies the virtual environment, then
  the dataset with `--chmod=0444`, then installs the wheel with `uv pip install --no-deps`. Sets
  `ENV DB_PATH=/opt/agentic-portfolio/data/portfolio.duckdb`, puts the virtual environment first on
  `PATH`, sets `WORKDIR /work` and `USER 10001:10001`, and declares `CMD ["portfolio", "--help"]`
  with **no `ENTRYPOINT`**, so that any arguments the user supplies replace the command entirely and
  both `IMAGE portfolio-backtest` and `IMAGE bash` work.

Do **not** set `AGENTIC_PORTFOLIO_HOME` in the image. Leaving it at `"."` is what makes `memory/`,
`output/` and `data/holdings.duckdb` resolve under `/work` - the user's mount - with no extra flag.
`DB_PATH` is baked precisely because the market-data database is the one path that should not follow
the working directory.

**The `0444` file mode is load-bearing, not decorative.** The dataset builders open that database
for writing (see Surprises above). Without the read-only mode, `portfolio-build-prices` pointed at
the baked path would copy forty-five megabytes into the container's writable layer, report success,
and lose the result on exit. With `0444` and a non-root user it fails immediately with a permission
error. Document `-e DB_PATH=/work/data/portfolio.duckdb` as mandatory for any builder run.

**On the non-root user and bind mounts.** Baking `USER 10001` gives a safe default and is what makes
`0444` meaningful. But uid 10001 is not the host user, so writes into a bind-mounted host directory
would land with the wrong owner. The answer is the documented `--user "$(id -u):$(id -g)"` override,
which makes reports and the JSON memory files belong to whoever ran the command. This is safe here:
nothing in the code calls `Path.home()`, `os.path.expanduser` or `getpass.getuser()`, so a uid with
no entry in `/etc/passwd` causes no problem. Set `HOME=/home/app` world-writable for any dependency
that wants a cache directory. Do **not** copy the development container's `--build-arg USER_UID`
approach, which would make the published artifact specific to one host's user.

**Dataset integrity.** Record the expected checksum in `docker/dataset.sha256`, take
`--build-arg DATASET_SHA256`, and verify with `sha256sum -c` in the runtime stage, so that building
with the wrong dataset fails the build rather than shipping quietly.

**Provenance.** `docker/dataset_manifest.py` opens the database read-only and writes
`DATASET.json` next to it, containing per-table row counts for all eleven tables, the first and last
date in `prices`, the fetch and rebalance windows the file was built under, the contents of
`dividend_unresolved` so that the four permanently-uncovered tickers (`AVB`, `EA`, `EQR`, `LEG`) and
the reasons are discoverable, the file's sha256, and the build date. Generate it rather than writing
it by hand: a hand-maintained manifest drifts from the file it describes, which is worse than none.

Also confirm once, during verification, that DuckDB does not attempt to write a temporary spill file
beside the read-only database while running the backtest. It should not at this data size, but the
failure mode would be confusing, so check it deliberately.

**Reproducibility, stated honestly.** `data/portfolio.duckdb` is not tracked by git, so this image
cannot be built from a clean clone. Say so in `README.md` as a documented maintainer prerequisite -
build the dataset with the `portfolio-build-*` commands, or obtain the file and verify it against
`docker/dataset.sha256`. Do not paper over it.


### Step 10 - Build, publish, document

Build both architectures and push:

    $ docker buildx create --name apx --driver docker-container --use --bootstrap
    $ echo "$GHCR_TOKEN" | docker login ghcr.io -u <owner> --password-stdin
    $ SHA=$(sha256sum data/portfolio.duckdb | cut -d' ' -f1)
    $ docker buildx build --platform linux/amd64,linux/arm64 \
        --build-arg DATASET_SHA256="$SHA" \
        --label org.opencontainers.image.source=https://github.com/<owner>/agentic_portfolio \
        --label org.opencontainers.image.version=0.1.0 \
        --label org.opencontainers.image.revision="$(git rev-parse HEAD)" \
        --label org.opencontainers.image.licenses=MIT \
        --annotation "index:org.opencontainers.image.description=Agentic AI portfolio screening with bundled market data" \
        -t ghcr.io/<owner>/agentic-portfolio:0.1.0 \
        -t ghcr.io/<owner>/agentic-portfolio:0.1 \
        -t ghcr.io/<owner>/agentic-portfolio:latest \
        -t ghcr.io/<owner>/agentic-portfolio:sha-$(git rev-parse --short HEAD) \
        --provenance=true --sbom=true --push .

The `--annotation index:` is separate from the labels deliberately: GHCR's package page reads the
image *index* annotation, and labels alone leave the listing description blank. After the first
push, make the package public once in the GHCR web interface - packages default to private, and
otherwise every `docker pull` fails with a 401 and the whole distribution story breaks silently.

Version numbers come from `pyproject.toml` and must be bumped in the same change as anything that
alters image content - **including a dataset refresh**. Re-pushing a different dataset under an
existing tag is the one thing that would make `:0.1.0` a lie.

Record the whole command in `docker/README.md` rather than leaving it in a shell history. Do not add
a GitHub Actions workflow: the dataset is untracked, so a runner cannot build this image at all.

If the emulated ARM build stalls compiling a source distribution somewhere in the crewai dependency
tail, build each architecture natively and join them:

    $ docker buildx imagetools create -t ghcr.io/<owner>/agentic-portfolio:0.1.0 \
        ghcr.io/<owner>/agentic-portfolio:0.1.0-amd64 \
        ghcr.io/<owner>/agentic-portfolio:0.1.0-arm64

**README changes**, surgical rather than a rewrite. The current top-level headings are
`# Agentic Portfolio`, `# Configuration`, `# How to Use`, `# Backtest Mode`, `# Live Mode`,
`# Acknowledgements and Citation`. Add:

- A `# Installation` section between `# Configuration` and `# How to Use`, giving the `docker pull`
  command and one shell alias that the later command documentation can assume, then explaining what
  each flag buys. State that `-it` is **mandatory** for `portfolio --selection user_provided` and
  `portfolio-holdings whatif`, which sit at `input()` prompts. On API keys, give both working
  options: `-e ANTHROPIC_API_KEY` (or `--env-file`) passes them from the host environment, and a
  `.env` in the mounted working directory also works - the latter **only because**
  `export_api_keys()` bridges it, since CrewAI's own `load_dotenv()` cannot reach `/work` from
  `/opt/agentic-portfolio/venv`; recommend `-e` as the primary route regardless, since it keeps
  credentials out of a directory that also holds saved reports. State that `SEC_UA` is not needed at
  all by a user of the bundled dataset. State that builder commands need
  `-e DB_PATH=/work/data/portfolio.duckdb`.
- A `## Where files are written` subsection under `# How to Use`, mapping `/work` to the host
  directory.
- An amendment to the `# How to Use` preamble, which currently says every command is run as
  `uv run <name>`: commands are now shown by bare name, prefixed with either the Docker alias or
  `uv run` depending on how you installed.
- A `# Development` section before `# Acknowledgements and Citation`, noting that the development
  container is unchanged, lives at `.devcontainer/Dockerfile`, and builds nothing from the release
  `Dockerfile`.
- A `## Bundled market data - provenance and licensing` subsection under
  `# Acknowledgements and Citation`, summarizing `NOTICE` and pointing at `DATASET.json`.

One existing sentence becomes true for the first time: `README.md` already refers to "the shipped
`data/portfolio.duckdb`" when discussing the four tickers with no dividend coverage. Nothing was
shipped before. It can stay exactly as written.


## Validation and Acceptance

Phrase acceptance as observable behavior. Every number below is a real measurement from this
repository, so a mismatch means something is wrong.

**The package.** Run `uv run pytest -q` and expect `1049 passed`, the same count as the Step 0
baseline. During the refactor use the per-step gates instead - the full suite is about seven
minutes, and `uv run pytest --collect-only -q` catches an incomplete rename in about four seconds.

**The wheel, not the source tree.** This distinction is the whole point: an editable install
resolves the CrewAI prompt files from the repository, so only the built artifact can prove they
ship.

    $ uv build
    $ python -m zipfile -l dist/agentic_portfolio-0.1.0-*.whl | grep -c 'config/.*\.yaml'
    6
    $ python -m zipfile -l dist/agentic_portfolio-0.1.0-*.whl | grep -c '^agentic_portfolio/'
    # non-zero
    $ python -m zipfile -l dist/agentic_portfolio-0.1.0-*.whl | grep -c '^src/'
    0

Add a permanent `tests/test_packaging.py` asserting that
`importlib.resources.files("agentic_portfolio.agents.llm_s_crew").joinpath("config/agents.yaml")`
and its five siblings all exist, plus one test that the new `_require` helper raises a message
naming the file when handed an empty config. Those tests are what stop this regressing later.

**A real install.** Into a throwaway environment, not the development one:

    $ python -m venv /tmp/fresh && /tmp/fresh/bin/pip install -q dist/*.whl
    $ ls /tmp/fresh/lib/python3.12/site-packages/ | grep -x 'src'      # expect no match
    $ ls /tmp/fresh/lib/python3.12/site-packages/ | grep -x 'agentic_portfolio'
    $ ls /tmp/fresh/bin/ | grep -c '^portfolio'                        # expect 13

**NEVER verify these commands by running `--help` on them.** Nine of the thirteen take no
arguments at all and act immediately: the eight `portfolio-build-*` / `portfolio-backfill-snapshot`
builders start fetching and **writing to the market-data database**, and `portfolio-backtest` runs
a complete backtest. `--help` is not a safe probe - it is ignored, and the command runs. Only
`portfolio`, `portfolio-holdings`, `portfolio-summary` and `portfolio-migrate-candidates` use
`argparse` and understand `--help`. See `Surprises & Discoveries` for what this cost when it was
learned the hard way.

To check that every entry point is wired correctly, import it and assert `main` is callable, which
executes nothing:

    $ /tmp/fresh/bin/python - <<'PY'
    from importlib.metadata import entry_points
    import importlib
    names = []
    for ep in entry_points(group="console_scripts"):
        if ep.name.startswith("portfolio"):
            module, _, attribute = ep.value.partition(":")
            assert callable(getattr(importlib.import_module(module), attribute)), ep.name
            names.append(ep.name)
    print(f"{len(names)} entry points import and expose a callable main")
    PY
    13 entry points import and expose a callable main

That check catches exactly what matters after a rename - a script pointing at a module path that no
longer exists - without side effects.

**The image.**

    $ IMG=ghcr.io/<owner>/agentic-portfolio:0.1.0
    $ docker run --rm $IMG bash -lc 'ls /opt/agentic-portfolio/venv/bin | grep -c "^portfolio"'
    13
    $ docker run --rm $IMG python -c 'import agentic_portfolio; print("ok")'
    $ docker run --rm $IMG bash -lc 'ls -l $DB_PATH'
    # mode must be -r--r--r--
    $ docker run --rm $IMG python -c "import duckdb, os; c = duckdb.connect(os.environ['DB_PATH'], read_only=True); print(c.sql('select count(*), min(date), max(date) from prices').fetchall())"
    [(1203663, datetime.date(2015, 1, 2), datetime.date(2024, 4, 29))]
    $ docker run --rm $IMG bash -lc ': > $DB_PATH'; echo "expect non-zero: $?"
    $ docker run --rm $IMG cat /opt/agentic-portfolio/data/DATASET.json
    $ docker buildx imagetools inspect $IMG | grep -E 'linux/(amd64|arm64)'

**The paper reproduction**, with no dataset mounted:

    $ docker run --rm -e ANTHROPIC_API_KEY $IMG portfolio-backtest
    Realized annualized Sharpe (MSR, ...): <a number>
    Paper-reported S&P 500 baseline Sharpe, 2020-2024: 0.6324

**The interactive path, and file ownership:**

    $ mkdir -p work
    $ docker run -it --rm --user "$(id -u):$(id -g)" -v "$PWD/work:/work" -w /work \
        $IMG portfolio --date today --value 10000 --selection user_provided
    $ ls -ln work/output/*/ work/memory/
    # uid and gid must equal your own - not 0, and not 10001

**The preflight**, which must refuse before touching the network or a database:

    $ time docker run --rm -e ANTHROPIC_API_KEY= $IMG portfolio-backtest
    ANTHROPIC_API_KEY is not set, so ...
    # about one second, exit code 2, no traceback

**The secret-absence gate. Run this before the first `--push`, not after.** A running-container
`ls` is not sufficient, because a file added in one layer and deleted in a later one still exists in
the earlier layer and is still downloaded by everyone who pulls the image. Check all three ways:

    # 1. the flattened final filesystem
    $ docker create --name chk $IMG >/dev/null
    $ docker export chk | tar -tf - | grep -E '(^|/)(\.env|memory/|output/|holdings\.duckdb|news_archive_source\.parquet|\.git/|\.venv/)' \
        && echo LEAK || echo CLEAN

    # 2. every layer tarball, which catches added-then-deleted files
    $ docker save $IMG -o /tmp/img.tar && mkdir -p /tmp/imgx && tar -xf /tmp/img.tar -C /tmp/imgx
    $ find /tmp/imgx -name '*.tar*' -exec sh -c 'tar -tf "$1" 2>/dev/null' _ {} \; \
        | grep -E '\.env$|memory/|output/|holdings\.duckdb|news_archive_source\.parquet' \
        && echo LEAK || echo CLEAN

    # 3. a content scan for the key shapes themselves, independent of filenames
    $ docker export chk | grep -aoE 'sk-ant-[A-Za-z0-9_-]{20,}|sk-proj-[A-Za-z0-9_-]{20,}' | head
    $ docker rm chk >/dev/null

The third check is the gate: filenames can be renamed, key material cannot be disguised. If any of
the three finds something, do not push - and if a push already happened, deleting the tag does not
reliably delete the underlying blob, so the exposed key must be rotated.


## Idempotence and Recovery

Steps 0, 3 through 8, and 10 are safely repeatable. Steps 1 and 2 are not, and the recovery paths
differ:

- **Step 1** (the move) is repeatable only in the sense that re-running it after a completed run
  would fail harmlessly, because `src/agentic_portfolio` would already exist and `git mv src` would
  refuse. To undo it before committing: `git mv src/agentic_portfolio .rename_tmp && rmdir src &&
  git mv .rename_tmp src`, or simply `git reset --hard` if you have nothing else uncommitted -
  check `git status` first.
- **Step 2** (the rewrite) is **not** idempotent. The `s|src/|src/agentic_portfolio/|g` rule applied
  twice produces `src/agentic_portfolio/agentic_portfolio/`. If a rule needs changing, restore with
  `git checkout -- src tests` and apply the whole set again from the clean state. Do not patch a
  half-applied rewrite by hand.
- **Step 4** is a pure edit and repeatable, but note that its correctness depends on the default
  staying byte-identical. If `tests/test_interactive_flow.py:397` starts failing with
  `./data/portfolio.duckdb`, the identity branch in `_under_home` is wrong or missing.
- **Step 6** regenerates the tracked `uv.lock`. If a pruned dependency turns out to be needed after
  all, restore with `git checkout -- pyproject.toml uv.lock && uv sync`.
- **Step 9 and 10**: image builds are immutable and create nothing outside Docker's own storage.
  Reclaim space with `docker buildx prune`. The one irreversible action in this entire plan is
  `docker buildx build --push` - a pushed layer is public and cached by others. That is why the
  secret-absence gate runs against a locally-built image first.

Leave the working tree clean: remove `dist/`, `/tmp/fresh`, `/tmp/img.tar`, `/tmp/imgx` and the
`ctx-check` image when done. `dist/` should be added to `.gitignore` if `uv build` is run in the
repository.


## Artifacts and Notes

The evidence that the packaging defect is real, gathered before any change:

    $ ls -d .venv/lib/python3.12/site-packages/agentic* .venv/lib/python3.12/site-packages/src
    ls: cannot access '.venv/lib/python3.12/site-packages/src': No such file or directory
    .venv/lib/python3.12/site-packages/agentic_portfolio-0.1.0.dist-info

    $ grep -o "from src[^ ]*" .venv/bin/portfolio | head -1
    from src.flow.cli

An installed distribution whose console script imports `src`, with no `src` in `site-packages`: the
signature of an editable install, and the reason a normal install would have to create a top-level
`src/` directory.

The dataset as measured, which the provenance manifest must agree with:

    $ python -c "import duckdb; c = duckdb.connect('data/portfolio.duckdb', read_only=True); \
        print(sorted(r[0] for r in c.execute('show tables').fetchall())); \
        print(c.execute('select min(date), max(date), count(*) from prices').fetchone())"
    ['dividend_coverage', 'dividend_unresolved', 'dividends', 'factors', 'news_articles_hf',
     'prices', 'returns', 'sec_fallback_tickers', 'sp500_membership', 'splits',
     'unresolved_tickers']
    (datetime.date(2015, 1, 2), datetime.date(2024, 4, 29), 1203663)

The SQL that must survive the rewrite unchanged, in
`src/agentic_portfolio/flow/live.py`:

    con.execute(f"ATTACH '{escaped_path}' AS src (READ_ONLY)")
    con.execute("CREATE TABLE news_articles_hf AS SELECT * FROM src.news_articles_hf")

The reference counts that justify leaving the older plans alone:

    $ for f in README.md AGENTS.md CREWAI.md PLANS.md; do echo "$f: $(grep -o 'src/' $f | wc -l)"; done
    README.md: 2
    AGENTS.md: 2
    CREWAI.md: 4
    PLANS.md: 0
    $ grep -ro 'src/' plans/ | wc -l
    809


## Interfaces and Dependencies

At the end of this plan the following must exist.

In `src/agentic_portfolio/config/settings.py`, module scope:

    _HOME: str
    def _under_home(relative: str) -> str

and on `Settings`, these fields, every path-valued one defaulting through `_under_home`:

    agentic_portfolio_home: str
    db_path: str
    output_dir: str
    holdings_db_path: str
    news_archive_path: str
    candidates_path: str
    rates_path: str
    portfolio_path: str
    llm_f_model: str

In a new `src/agentic_portfolio/config/preflight.py`:

    def api_key_problem(model: str) -> str | None      # moved here from agents/report_summary.py
    def require_api_keys(selection: str) -> None       # raises SystemExit(2)
    def require_sec_user_agent() -> None               # raises SystemExit(2)

In `pyproject.toml`: thirteen `[project.scripts]` entries of the form
`portfolio = "agentic_portfolio.flow.cli:main"`; `[tool.hatch.build.targets.wheel]` with
`packages = ["src/agentic_portfolio"]`; a `[dependency-groups]` table with a `dev` group; no
`pythonpath` under `[tool.pytest.ini_options]`.

New files: `LICENSE`, `NOTICE`, `.dockerignore`, `docker/dataset_manifest.py`,
`docker/dataset.sha256`, `docker/README.md`, `tests/test_preflight.py`,
`tests/test_packaging.py`, `.devcontainer/Dockerfile` (the current development container, moved).

Rewritten: `Dockerfile` (release image, four stages).

Libraries relied on, all already present: `hatchling` builds the wheel and includes non-Python files
found inside a `packages` directory, which is how the six CrewAI YAML files ship. `uv` resolves,
locks, syncs and builds; it installs PEP 735 dependency groups by default and skips them with
`--no-dev`. `pydantic-settings` populates `Settings` from the environment and `.env`, matching field
names case-insensitively - which is why no field may be named `home`. `duckdb` opens the bundled
database read-only. `docker buildx` produces the multi-architecture image.


## Revision Notes

- 2026-09-12: Initial version. Written after an exploration pass over the repository and a
  decision session with the user covering distribution channel, package namespace, runtime file
  locations, dataset bundling and first-run behavior; all of those are recorded in the
  `Decision Log`. Two pre-existing defects were found during design rather than implementation -
  the LLM key that a `.env` file cannot actually supply, and the dataset builders' writable
  connection - and both are folded into Steps 5 and 9 respectively because each would otherwise
  turn into a confusing failure inside a container. The reference counts in
  `Artifacts and Notes` were measured rather than estimated, and they are what changed the
  documentation policy from "rewrite everything" to "leave plans 01 to 19 alone".

- 2026-09-12, second revision, after implementing Steps 0 through 8 and authoring 9 and 10. Six
  changes, each because the plan as written was wrong or incomplete rather than because the design
  moved:

  **A destructive instruction was removed.** The `Validation and Acceptance` section told the
  reader to verify the console scripts with `for s in /tmp/fresh/bin/portfolio*; do "$s" --help`.
  Following that instruction destroyed 7,038 rows of `data/portfolio.duckdb`, because nine of the
  thirteen commands ignore argv and act immediately. It is replaced by an import-only check, and a
  warning now sits at both places a reader would reach for `--help`. This was the single most
  important correction in this revision: the plan actively harmed the person following it.

  **Step 2's rewrite rules were incomplete.** Four rules were specified; six were needed. The
  missing category was `python -m src.<module>`, which appears in docstrings and in two runtime
  error messages users actually see. The gate caught it, which is the argument for having the gate.

  **The `.env` mechanism was described wrongly, twice.** Both `Surprises & Discoveries` and Step 5
  now carry the actual mechanism - python-dotenv searching upward from CrewAI's own installed file
  - and Step 5 gained `export_api_keys()` as a result, having previously said not to touch key
  forwarding at all. The corrected understanding is what revealed that a `.env` in the mounted
  workspace would pass the preflight and then fail inside the image.

  **The dev container's destination changed** from `.devcontainer/Dockerfile` to
  `docker/Dockerfile.dev`, because `.devcontainer` is gitignored and the original destination would
  have silently untracked the file.

  **Two `conftest.py` mitigations were dropped as unnecessary**, with the reason recorded: every
  affected test passes its paths explicitly, and the proposed `monkeypatch.delenv` could not have
  worked anyway because `_HOME` is read at settings-import time.

  **Steps 9 and 10 are marked `[~]`, not `[x]`.** The development container has no Docker, so the
  image has never been built and the secret-absence gate has never run. Saying otherwise would be
  the most expensive kind of inaccuracy this document could contain.

  The unplanned dataset recovery is recorded in `Progress`, `Surprises & Discoveries` and the
  `Decision Log`, and `Outcomes & Retrospective` now has its first two entries.
