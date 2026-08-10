# Build the LLM-S fundamentals screening agent


This ExecPlan is a living document. The sections `Progress`, `Surprises & Discoveries`, `Decision Log`, and `Outcomes & Retrospective` must be kept up to date as work proceeds. This plan must be maintained in accordance with `PLANS.md` at the repository root. This plan builds on `plans/01_dataset.md`, which is checked into this repository at that path — this plan does not repeat that plan's factor-computation details, but does restate, in the Interfaces and Dependencies section below, exactly which tables and columns this plan reads from it, so a reader does not need to have plan 1 open to follow along.


## Purpose / Big Picture


After this plan is done, a person can run one command for any calendar year between 2020 and 2024 and see a small set of plain-English buy and sell rules — things like "buy companies with a book-to-market ratio above 0.4 and momentum above -0.1" — that a large language model produced after looking at that year's actual distribution of company sizes, valuations, and momentum. Given those rules, the same command applies them mechanically to every stock and prints, for a chosen date, exactly which stocks would be flagged buy, sell, or neither. That is LLM-S: the fundamentals-screening half of the two-agent screening layer described in the paper this project reproduces ("Designing Agentic AI-Based Screening for Portfolio Investment", Caner, Capponi, Sun, and Tan, arXiv:2603.23300).


The paper reruns its fundamentals agent once per year, on the theory that broad economic regimes (what counts as "large," "cheap," or "in an uptrend") shift slowly enough that a yearly refresh is sufficient, while still letting the agent adapt its thresholds to each year's actual data rather than using one fixed rule forever. This plan keeps that same yearly cadence.


## Progress


- [x] (2026-08-05) Define the Pydantic schema for LLM-S's output (a rule, plus its rationale), using the paper's own variable names (`mve`, `bm`, `mom12m`) rather than a `_z`-suffixed naming scheme. Implemented in `src/agents/llm_s_schema.py`: `ScreeningRule(year: int, buy_condition: str, sell_condition: str, rationale: str)`. Verified end-to-end: constructing a `ScreeningRule` with a sample buy/sell condition round-trips through `.model_dump()` correctly.
- [x] (2026-08-05) Write the shared, restricted condition evaluator (`src/agents/condition_eval.py`) that both rule-application and the `test_complex_condition` tool use, so what the agent tests during exploration has identical semantics to what runs in production. Implemented `evaluate_condition(condition: str, values: dict[str, float]) -> bool` via an `ast`-based restricted walker (allow-listing `BoolOp`/`UnaryOp`/`Compare`/`Name`/`Constant` and the operators `and`/`or`/`not`/`>`/`<`/`>=`/`<=`), plus `ALLOWED_NAMES = {"mve", "bm", "mom12m"}`. Verified end-to-end: correctly evaluates nested/chained boolean conditions (including the paper's own worked examples, e.g. `"(bm < -1.02 and mom12m > 0.53) or mve > 1.59"`) and negative-literal comparisons (`"mve <-0.68"`); raises `ValueError` (not a silent wrong answer, not a crash) for a disallowed name (`pe_ratio`), a disallowed operator (`==`), malformed syntax, and an arbitrary-code-execution attempt (`__import__('os').system(...)`).
- [x] (2026-08-05) Write the `agents.yaml`/`tasks.yaml` config and `LLMSCrew` class (CrewAI's `@CrewBase` pattern), using the paper's verbatim role/goal/backstory/description/expected_output text (Appendix C.5 of arXiv:2603.23300v1), parameterized only by `{as_of_date}`. Implemented in `src/agents/llm_s_crew/config/{agents,tasks}.yaml` and `src/agents/llm_s_crew/crew.py` (renamed from the originally-planned `src/agents/llm_s/` — see Surprises & Discoveries: that name collides with the flat `src/agents/llm_s.py` entry point). Verified end-to-end: `LLMSCrew(snapshot, as_of_date, model).crew()` builds without any LLM call, with the YAML's `{as_of_date}` placeholder still present pre-kickoff (confirming interpolation is deferred to `.kickoff(inputs=...)`, not baked in early), the agent's role/LLM set correctly, and the task carrying all 4 tools plus `output_pydantic=ScreeningRule`.
- [x] (2026-08-05) Implement the 4 tools the paper's Task attaches (`get_database_schema`, `query_firm_database`, `get_extreme_firms`, `test_complex_condition`), scoped to a single causally-masked snapshot date per crew invocation. Implemented in `src/agents/llm_s_crew/tools.py` (`load_snapshot` plus `GetDatabaseSchemaTool`/`QueryFirmDatabaseTool`/`GetExtremeFirmsTool`/`TestComplexConditionTool`, each a `crewai.tools.BaseTool` subclass with a Pydantic `args_schema`). Verified end-to-end against a 5-row fixture snapshot: schema tool reports the right characteristic count/date; query tool respects `sort_by`/`limit`/`tickers`; extreme-firms tool returns the correct highest/lowest rows; `test_complex_condition` returns the correct match count/percentage for a valid condition and a plain error string (not a raised exception) for one referencing a disallowed name.
- [x] (2026-08-05) Write `generate_rule(year, model=None, db_path=...)` that resolves the causal-masking snapshot date for that year, builds the crew, kicks it off, and returns a validated `ScreeningRule`. Implemented in `src/agents/llm_s.py`, plus a `resolve_as_of_date(year, db_path)` helper. Verified against the real `data/portfolio.duckdb`: `resolve_as_of_date(2024)` correctly resolves to `2023-12-01` (the real December-2023 rebalance date); `resolve_as_of_date(2020)` correctly falls back to the earliest available date `2020-01-01` with the expected warning logged (the documented year=2020 edge case); `load_snapshot` + `LLMSCrew(...).crew()` build successfully end-to-end against the real 480-firm 2023-12-01 snapshot (not just the earlier fixture), with all 4 tools working against real data. The actual `.kickoff()` LLM call itself was not exercised in this pass (no `ANTHROPIC_API_KEY` in this environment) — that is covered by this plan's separate, later "manually run the agent for at least one real year" item.
- [x] (2026-08-05) Write the deterministic rule-application function that takes a produced rule and a set of per-stock factor values and returns a buy/sell/hold signal per stock. Implemented `apply_rule(rule: ScreeningRule, factor_row: dict) -> str` in `src/agents/llm_s_apply.py`, checking `buy_condition` first (via the shared `condition_eval.evaluate_condition`) then `sell_condition`, defaulting to `"hold"`. Verified end-to-end: returns `"buy"`/`"sell"`/`"hold"` correctly for rows matching only the buy condition, only the sell condition, or neither, and resolves buy-takes-precedence when a row (pathologically) matches both.
- [x] (2026-08-05) Write `screen(rule, rebalance_date, db_path=...)` in `src/agents/llm_s_signals.py`, mapping `factors`'s `_z` columns to the bare `mve`/`bm`/`mom12m` keys and applying `apply_rule` per ticker. Verified end-to-end: against a hand-built fixture DuckDB table with one null-`mve_z` row, correctly excludes that row and returns the right buy/sell/hold signal for each of the remaining three; against the real `data/portfolio.duckdb` for `2024-03-01`, returns a plausible three-way split (2 buy / 131 sell / 350 hold) with real, recognizable ticker symbols.
- [x] (2026-08-05) Write `tests/test_llm_s.py` covering the evaluator, rule application, `screen`, and the 4 tools against a small fixture table (not rule generation itself, which needs a real LLM call). 10 tests, all passing (`uv run pytest tests/test_llm_s.py`), covering every Validation and Acceptance required case; full repo suite (`uv run pytest tests/`) still passes at 62/62. Minor fixup along the way: added `__test__ = False` to `TestComplexConditionTool` since pytest's collector otherwise misidentifies it as a test class from its `Test`-prefixed name.
- [x] (2026-08-06) Manually run the agent for at least one real year and sanity-check the rule it produces against the paper's own worked 2024 example. Ran `generate_rule(2024)` with `LLM_S_MODEL=anthropic/claude-opus-4-8` against the real 2023-12-01 snapshot (480 firms). Verified end-to-end: the verbose CrewAI trace shows the agent calling `get_database_schema` once, `get_extreme_firms` six times, `query_firm_database` twice, and `test_complex_condition` roughly a dozen times to iteratively test candidate thresholds before finalizing — it explored rather than shortcut to an answer. Produced `buy_condition='(mom12m > 0.55 and mve > -0.9) or (bm > 0.2 and mom12m > -0.15)'`, `sell_condition='mom12m < -0.85 or (bm > 0.4 and mom12m < -0.5)'`, with precise (non-round) thresholds in range, referencing only `mve`/`bm`/`mom12m`. Structurally matches the paper's worked 2024 example (undervalued-plus-momentum BUY logic, momentum/value-trap SELL logic) without needing an exact numeric match, per this plan's own acceptance bar. Applied the rule via `screen(rule, date(2024, 3, 1))` against real data: 106 buy / 71 sell / 306 hold, with recognizable real tickers (NVDA, MSFT, NFLX, MU) in the buy set — a plausible three-way split. `uv run pytest tests/test_llm_s.py` still passes (10/10) after the run.


## Surprises & Discoveries


(Record here anything about how the chosen LLM actually behaves — for example, if it produces rules referencing factors other than the three it was given, or thresholds far outside the standardized [-3, 3]-ish range one would expect from mean-0/variance-1 data, both of which would need a decision about whether to reject and retry the generation. Also record here whether the agent actually calls its 4 tools before answering, or tries to shortcut straight to a final rule without exploring the data.)

- Discovery (2026-08-06): with `LLM_S_MODEL=anthropic/claude-opus-4-8`, the agent called all 4 tools, used `test_complex_condition` heavily (roughly a dozen calls) to compare candidate BUY/SELL thresholds by match count/percentage before settling on a final rule, and explicitly checked (and reported in its rationale) that the BUY and SELL sets did not overlap. It also flagged two rows (`BNY`, `ECHO`) as likely data artifacts (implausible `bm` z-scores of 20.47 and 6.45) and adjusted its size threshold (`mve > -0.9`) specifically to exclude the corresponding extreme-negative-`mve` tail — this is exploratory judgment the plan's tools enable but don't explicitly prompt for. All thresholds landed in the expected roughly `[-1, 1]` range, well within `[-3, 3]`, and only `mve`/`bm`/`mom12m` were referenced. No code or prompt changes were needed to get this behavior — `LLM_S_MODEL` alone was sufficient to point the existing implementation at a different Anthropic model.
- Discovery (2026-08-06): CrewAI's native Anthropic integration (not LiteLLM, which isn't installed in this repo) resolves any `anthropic/claude-...` model string through the real `anthropic` Python SDK, which itself falls back to reading `ANTHROPIC_BASE_URL` from the environment when `crew.py` doesn't set an explicit `base_url`. This means a custom Anthropic-compatible endpoint (e.g. this environment's proxy) works with zero code changes — just exporting `ANTHROPIC_API_KEY` and `ANTHROPIC_BASE_URL` in the shell before the run. Also: neither `llm_s.py` nor `crew.py` call `load_dotenv()`, so `.env` must be sourced into the shell explicitly (`set -a && source .env && set +a`) — it is not auto-loaded.

- Discovery (2026-08-05): the earlier draft of this plan specified both a subpackage `src/agents/llm_s/` (holding the CrewAI machinery) and a flat module `src/agents/llm_s.py` (the `generate_rule` entry point) as siblings under `src/agents/`. Python cannot have a package and a module of the same name in the same parent package — confirmed empirically (`from src.agents.llm_s import generate_rule` raised `ImportError`, because the package's `__init__.py` silently shadows the module). Fixed by renaming the subpackage to `src/agents/llm_s_crew/` throughout this plan, keeping `src/agents/llm_s.py` as the flat entry point plan 4/6 already depend on. The equivalent future rename applies to plan 3's `LLMFCrew` (`src/agents/llm_f_crew/`, not `src/agents/llm_f/`) for the identical reason, since plan 3 will have the same `llm_f.py`/`llm_f_crew/` naming collision once it gets its own C.5-style rewrite.
- Discovery (2026-08-05): `src/agents/llm_s_signals.py`'s `screen` function was fully specified in Plan of Work and promised to plan 4 in Interfaces and Dependencies, but the earlier rewrite of this plan dropped it from both the Progress checklist and the Validation and Acceptance required-test-cases list (the intro paragraph there already mentioned `screen`, but no bullet actually required a test for it). Fixed by adding both back before implementing `screen`.
- Discovery (2026-08-05): `crewai` 1.15.10's `BaseTool` is a Pydantic model whose `__init__` only accepts keyword arguments (`self, /, **data`) — tool instances must be constructed as `SomeTool(field=value)`, never positionally. `CrewBase`'s `base_directory` resolves as `Path(inspect.getfile(cls)).parent` (confirmed by reading `crewai/project/crew_base.py`), so `agents_config = "config/agents.yaml"` on `LLMSCrew` in `src/agents/llm_s_crew/crew.py` correctly resolves to `src/agents/llm_s_crew/config/agents.yaml` regardless of the process's working directory.


## Decision Log


- Decision: default LLM-S to Anthropic Claude via CrewAI's native LLM string syntax (e.g. `Agent(llm="anthropic/claude-sonnet-4-5")`), but read the actual model string from an environment variable `LLM_S_MODEL` (falling back to a hardcoded Anthropic default if the variable is unset), so a user can point LLM-S at an OpenAI or Gemini model instead by setting that one environment variable, without touching code.
  Rationale: the user explicitly asked for Anthropic as the default while wanting OpenAI and Gemini available "at will." CrewAI's `LLM` class (and the plain string shorthand `Agent(llm="provider/model")`) is built on LiteLLM-style provider prefixes, so `LLM_S_MODEL=openai/gpt-4o` or `LLM_S_MODEL=gemini/gemini-2.5-pro` work with zero additional code once the environment variable is read — this is not a new abstraction, just reading a string from the environment where a literal string currently sits.
  Date/Author: 2026-08-05, decided by repository owner during planning interview.
- Decision: rule generation happens once per calendar year (a single LLM call per year), and rule application is 100% deterministic Python with no LLM calls.
  Rationale: matches the paper's stated annual rerun cadence for its fundamentals agent, and keeps the expensive/nondeterministic part (one LLM call) cleanly separated from the cheap/deterministic part (evaluating a threshold rule against a table of numbers), which also makes rule application trivially unit-testable without mocking an LLM.
  Date/Author: 2026-08-05, plan author.
- Decision: port the paper's Appendix C.5 `Agent`/`Task` Python snippet into CrewAI's `agents.yaml`/`tasks.yaml` config pattern (a `strategy_agent` entry and a `strategy_task` entry) plus a minimal `LLMSCrew(@CrewBase)` class, instead of constructing `Agent`/`Task` inline in Python, per `CREWAI.md`'s documented best practice ("YAML-first configuration: Define agents and tasks in YAML, keep crew classes minimal") and the user's explicit request.
  Rationale: matches this repository's own reference doc's recommended pattern, and keeps the large verbatim prompt blocks out of Python source, where line-length and quoting would make them harder to keep byte-for-byte faithful to the paper.
  Date/Author: 2026-08-05, decided during planning interview.
- Decision: use the paper's own verbatim `role`, `goal`, `backstory`, `description`, and `expected_output` text (quoted in full in Plan of Work below) rather than this plan's earlier paraphrase, parameterized only by CrewAI's `{as_of_date}` YAML interpolation in place of the paper's hardcoded "December 2023" / "2024".
  Rationale: user explicitly asked to keep the prompt the same as the paper's; the earlier paraphrase drifted from the paper's exact wording (e.g. calling the persona "a portfolio manager" rather than the paper's "Quantitative Strategy Developer").
  Date/Author: 2026-08-05, decided during planning interview, confirmed against a direct extraction of arXiv:2603.23300v1 page 56-59 (Appendix C.5, "LLM-S Prompts and Outputs").
- Decision: `ScreeningRule.buy_condition`/`sell_condition` use the bare names `mve`, `bm`, `mom12m` (matching the paper's prompt, which describes these as already-standardized z-scores under those names), not `mve_z`/`bm_z`/`mom12m_z`. The DuckDB `factors` table (from `plans/01_dataset.md`) still stores both raw and `_z`-suffixed columns; this plan's code is responsible for presenting only the `_z` columns to the agent and tools, under the bare names, and mapping back when applying a rule to `factors` rows.
  Rationale: keeping the prompt verbatim (previous decision) requires the variable names inside that prompt to match what the paper actually wrote; inventing a `_z` suffix the paper never used would silently diverge from "keep the prompt the same."
  Date/Author: 2026-08-05, decided during planning interview.
- Decision: `generate_rule` drops the `factor_summary` parameter entirely. The agent explores the `factors` table itself via the 4 tools described below, rather than being hand-fed a pre-computed summary dict.
  Rationale: the paper's own task description ("1. Get database schema and understand available data... 2. Explore extreme values... 4. Use test_complex_condition...") has the agent doing this exploration itself; handing it a summary up front both contradicts the paper's design and makes 3 of the 4 tools redundant.
  Date/Author: 2026-08-05, decided during planning interview. This is a breaking change to `generate_rule`'s signature from this plan's earlier draft; `plans/04_candidate_scanner.md`'s Concrete Steps example call is updated in the same pass since it referenced the old signature.
- Decision: the 4 tools are scoped to a single "as-of" snapshot date per `generate_rule` call — the most recent `factors` rebalance date in December of `year - 1` (e.g. `2023-12-01` when generating the rule for 2024), falling back to the earliest available rebalance date in the `factors` table if no such December date exists (this only affects `year=2020`, the first year in the project's 2020-2024 window, which has no prior-December data at all). The date is fixed in Python when the tools are constructed, not exposed as an argument the LLM can choose in its tool calls.
  Rationale: the paper explicitly requires causal masking ("you must use causal masking from December 2023 to prevent any look-ahead bias") for exactly this reason. Exposing the date as an LLM-chosen tool argument would rely on the LLM voluntarily respecting that instruction; fixing it in the tool's Python-side implementation enforces it mechanically regardless of what the LLM asks for.
  Date/Author: 2026-08-05, decided during planning interview.
- Decision: keep `resolve_as_of_date`'s fallback-to-earliest-date behavior unchanged, and instead close the `year=2020` edge case at the data layer — add a real 2019-12-31 row to the `factors` table via a new `src/dataset/backfill_snapshot.py` module (see `plans/01_dataset.md`'s Decision Log), rather than changing `resolve_as_of_date` itself or updating README.md's stated 2019-12-31 date to match the 2020-01-01 fallback.
  Rationale: `plans/08_consistency_review.md` finding 4 flagged that README.md's Backtest Mode Stage 1 states `S_2020` comes from a 2019-12-31 snapshot, which this project's data did not actually have. The repository owner, when asked whether to reword README or backfill real data, chose to backfill. `resolve_as_of_date`'s SQL query (`max(rebalance_date) < date(year, 1, 1)`) needs no code change at all — it will pick up the new 2019-12-31 row automatically once `backfill_snapshot.py` has been run against a real database, and its fallback path remains valuable as a safety net for any environment where that backfill has not yet been run (e.g. a smaller test fixture, or before the backfill script's first run). Its docstring in `src/agents/llm_s.py` was updated to point future readers at `backfill_snapshot.py` and this Decision Log entry.
  Date/Author: 2026-08-10, decided by repository owner during the `plans/08_consistency_review.md` implementation session.


## Outcomes & Retrospective


This plan is complete. LLM-S generates a rule via one real LLM call per year, exploring a causally-masked snapshot through its 4 tools rather than being handed a pre-computed summary, and the resulting rule applies deterministically to real data with no LLM involvement at screening time. The one manual-run item (real LLM call, not unit-testable) confirmed the design works end-to-end against Anthropic Claude, including against a non-default model pointed at via `LLM_S_MODEL` and a custom `ANTHROPIC_BASE_URL`, with no code changes required for either.


## Context and Orientation


This plan adds a new package `src/agents/` (a sibling to the `src/dataset/` package built in `plans/01_dataset.md`) to this Python 3.12, `uv`-managed repository. All commands below run from the repository root and assume `plans/01_dataset.md` has already been implemented, so `data/portfolio.duckdb` exists with a populated `factors` table.


This plan uses CrewAI, an agent-orchestration framework already a dependency of this repository (see `pyproject.toml`'s `crewai[anthropic,azure-ai-inference,google-genai,tools]` entry and the reference document `CREWAI.md` at the repository root). Two CrewAI terms of art are used below. An "Agent" is CrewAI's object representing one LLM persona with a role, a goal, and a backstory — under the hood it is a wrapper around a chat-completions call to whichever LLM you configure it with. A "Task" is CrewAI's object pairing an Agent with a specific description of work and an expected output shape.


This plan follows `CREWAI.md`'s documented project layout: a `config/agents.yaml` and `config/tasks.yaml` pair defining the agent's and task's text fields (role, goal, backstory; description, expected_output), loaded by a `@CrewBase`-decorated Python class (`LLMSCrew`, in `src/agents/llm_s_crew/crew.py`) that attaches the 4 tools to the task and the LLM string to the agent (neither of which YAML can express) and exposes a `@crew` method returning a single-agent, single-task `Crew(process=Process.sequential)`. `generate_rule` builds this crew, calls `.kickoff(inputs={"as_of_date": ...})` (CrewAI interpolates `{as_of_date}` into the YAML text at kickoff time), and reads `result.pydantic` (via `output_pydantic=ScreeningRule` set on the task).


The prompt text itself — every word of `role`, `goal`, `backstory`, `description`, and `expected_output` below — is quoted directly from Appendix C.5 of arXiv:2603.23300v1 ("LLM-S Prompts and Outputs"), with only `{as_of_date}` substituted for the paper's hardcoded "December 2023." Do not paraphrase or "clean up" this text when implementing — it is intentionally verbatim, including its em-dashes, capitalization, and informal tone, because the user explicitly asked to keep the prompt the same as the paper's.


Before writing any CrewAI code, per `CREWAI.md`'s mandatory freshness check, run:

    uv run python -c "import crewai; print(crewai.__version__)"

and compare against the latest version on PyPI (`https://pypi.org/pypi/crewai/json`) and the changelog (`https://docs.crewai.com/en/changelog`), since CrewAI's API for YAML config, structured output, and custom tools has changed across versions and this plan's code examples must match whatever is actually installed, not this plan's assumptions. Additionally, since this plan uses YAML config, custom `BaseTool` subclasses, and task-level `tools=` overrides, also fetch `https://docs.crewai.com/en/concepts/tools` and confirm the current `BaseTool`/`args_schema` API matches what's assumed below, alongside `https://docs.crewai.com/en/concepts/agents` and `https://docs.crewai.com/en/concepts/tasks` for the `@CrewBase` class shape and `output_pydantic` result access.


"Standardized factor" here means the `mve_z`, `bm_z`, and `mom12m_z` columns produced by `plans/01_dataset.md`'s `factors` table — each one rescaled, separately for each rebalance date, to cross-sectional mean 0 and variance 1, as the paper's own methodology specifies before showing factor data to its fundamentals agent. This plan presents those `_z` columns to the agent and its tools under the bare names `mve`, `bm`, `mom12m` (see Decision Log above), matching the paper's own naming.


## Plan of Work


Create **`src/agents/condition_eval.py`** with a module-level constant `ALLOWED_NAMES = {"mve", "bm", "mom12m"}` and a function `evaluate_condition(condition: str, values: dict[str, float]) -> bool` that parses `condition` with `ast.parse(condition, mode="eval")` and walks the resulting tree, rejecting any node type outside `Compare`, `BoolOp`, `UnaryOp`, `Name`, `Constant`, and the comparison/boolean operators `>`, `<`, `>=`, `<=`, `and`, `or`, `not`. If the expression references any name outside `ALLOWED_NAMES`, or uses disallowed syntax, raise `ValueError` naming the offending token. This avoids arbitrary code execution from a string an LLM generated. Both `llm_s_apply.py` (production rule application) and the `test_complex_condition` tool (agent-time rule testing) share this one implementation — the agent should never be able to "test" a rule with semantics that differ from what will actually run later.


Create **`src/agents/llm_s_schema.py`** defining a Pydantic model `ScreeningRule` with fields: `year: int`, `buy_condition: str` (a human-readable boolean expression over `mve`, `bm`, `mom12m`, for example `"bm > 0.4 and mom12m > -0.1"`), `sell_condition: str` (same shape, for example `"bm < -0.3 or mom12m < -0.6 or mve < -0.8"`), and `rationale: str` (the agent's plain-English explanation for why it chose these thresholds). This mirrors the shape of the paper's own worked example, which the paper phrases as "targets undervalued (high bm), reasonably sized (mve>0.3) companies with positive momentum (mom12m>-0.5)" for a buy rule and a parallel form for sell.


Create the subpackage **`src/agents/llm_s_crew/`** holding the CrewAI machinery:


- `src/agents/llm_s_crew/config/agents.yaml`:

  ```yaml
  strategy_agent:
    role: >
      Quantitative Strategy Developer
    goal: >
      Develop systematic BUY/HOLD/SELL rules based on firm characteristics that can be applied
      to all S&P 500 firms
    backstory: >
      You are an expert quantitative strategist who creates systematic, rule-based trading
      strategies.

      CRITICAL DATA UNDERSTANDING:
      - 'mve' = log(market value of equity), represents log firm size
      - 'bm' = book-to-market ratio (value factor). Understand that a high book-to-market
      value means undervalued, and a low book-to-market value means overvalued.
      - 'mom12m' = 12-month momentum
      - ALL features are standardized: mean = 0, standard deviation = 1
      - Values are z-scores showing standard deviations from mean

      Your task is to develop EXPLICIT, SYSTEMATIC RULES for generating trading signals.
      Understand that when doing the following, you must use causal masking from {as_of_date}
      to prevent any look-ahead bias.

      1. EXPLORE THE DATA ({as_of_date}):
      - Identify what constitutes "extreme" values for mve, bm, and mom12m
      - Look for natural clustering or breakpoints in the data
      - Consider correlations between characteristics

      2. DEVELOP CLEAR RULES based on economic intuition:
      - Keep in mind the market conditions at this date - this might influence the rules you
      choose.
      - The following are example questions you can consider, BUT THEY ARE NOT EXHAUSTIVE:
        - Value stocks: Should low bm (cheap) be BUY or SELL?
        - Momentum: Should high mom12m (strong performance) be BUY or SELL?
        - Size: Should mve matter for the strategy?
        - Combinations: What about value + momentum + size together?

      3. DEFINE SPECIFIC THRESHOLDS:
      Your output must include exact rules. You can use test_complex_condition to test
      different combinations.
      Your output must include exact rules like:
      - "BUY if: bm < -0.71 AND mom12m > 0.85 AND mve > 0.23" or "BUY if: (bm > 0.57 AND
      mom12m < 0.82) OR mve > -0.98" or "BUY if: bm > 0.63 OR mve < 0.98"
      - "SELL if: bm > 1.25 OR mom12m < -0.98" or "SELL if: (bm < -0.84 OR mve < 0) AND
      mom12m < -0.97" or "SELL if: (bm > 0.94 AND mom12m < -1.06) OR mve <-0.68"
      - "HOLD: all other cases"
      - However, the above is only AN EXAMPLE - so do not simply copy the format above. You
      are free to include/exclude as many conditions in the if statements. You are also free
      to make the conditions as complicated or as simple as you like.
      - Be PRECISE in your thresholds, do not choose numbers that are nice or round - you are
      a quantitative strategy developer.

      4. PROVIDE RATIONALE:
      - Why these thresholds?
      - What's the economic intuition?
      - What patterns did you observe in the data?

      CRITICAL REQUIREMENTS:
      - Rules must be DETERMINISTIC (same inputs -> same output)
      - Use ONLY z-score comparisons (>, <, AND, OR), but these may be impacted by market
      conditions.
      - Define thresholds for BUY, SELL, and HOLD
      - Rules should be implementable as: if (condition) then signal
      - Keep in mind that we will use the buy signals to construct a portfolio, so it is
      better to give too many signals, rather than too few signals.

      OUTPUT FORMAT:
      ===========================================
      SYSTEMATIC TRADING RULES
      ===========================================
      Data Exploration Summary:
      - [Key statistics and patterns observed]

      BUY RULE:
      if [ANY complex z-score condition using AND/OR/NOT]:
          signal = BUY

      Examples of valid BUY rules:
      - "bm < -1.15 AND mom12m > 0.73 AND mve < 1.11" (simple AND)
      - "bm < -1.56 OR mom12m > 1.28 OR mve > 1.52" (simple OR)
      - "(bm < -1.02 AND mom12m > 0.53) OR mve > 1.59" (combination)
      - "bm < -0.83 AND (mom12m > 0.77 OR mve > 1.08)" (nested conditions)

      SELL RULE:
      if [ANY complex z-score condition]:
          signal = SELL

      HOLD RULE:
      else:
          signal = HOLD

      Rationale:
      - [Economic reasoning for BUY rule]
      - [Economic reasoning for SELL rule]
      - [Expected signal distribution]
      ===========================================

      Be precise, systematic, and data-driven. Your rules will be applied to ~500 firms.
  ```

- `src/agents/llm_s_crew/config/tasks.yaml`:

  ```yaml
  strategy_task:
    description: >
      Develop systematic BUY/HOLD/SELL rules for S&P 500 firms at {as_of_date}.

      Available characteristics (all are z-scores):
      - mve: log firm size
      - bm: log book-to-market (value)
      - mom12m: 12-month momentum

      Your process:
      1. Get database schema and understand available data for {as_of_date}
      2. Explore extreme values for each characteristic
      3. Look for patterns and correlations
      4. Use test_complex_condition to test different rule combinations
      5. Develop systematic rules with specific z-score thresholds

      You have COMPLETE FLEXIBILITY in creating rules. Test different combinations using AND,
      OR, NOT.

      CRITICAL: Your output must be EXPLICIT RULES with exact thresholds that can be
      implemented in Python/pandas. You must give PRECISE THRESHOLDS - do not give thresholds
      that are only nice or round numbers. Further, use causal masking from {as_of_date} to
      prevent any look-ahead bias.

      Focus on:
      - Economic intuition (value, momentum, size effects)
      - Clear, implementable thresholds
      - Balance between signal strength and diversification
      - Rules that make sense for ~500 firms

      Output systematic rules that I can directly implement in code.
    expected_output: >
      Complete strategy document with:
      1. Data exploration summary
      2. Explicit BUY rule with z-score thresholds
      3. Explicit SELL rule with z-score thresholds
      4. HOLD rule (default case)
      5. Economic rationale for each rule

      Rules must be deterministic and implementable.
    agent: strategy_agent
  ```

  CrewAI's YAML cannot express tool lists or `output_pydantic`; those are attached in `crew.py`, matching `CREWAI.md`'s own note that "Tools go on agents (not tasks) unless task-specific override is needed" — the paper's own snippet attaches tools to the Task specifically, so this plan does the same via a task-level `tools=` override.


- `src/agents/llm_s_crew/tools.py` — a snapshot loader plus the 4 tools, all reading from one pre-loaded, causally-masked DataFrame rather than hitting DuckDB on every tool call:

  `load_snapshot(db_path: str, as_of_date: date) -> pd.DataFrame` reads the `factors` table's `mve_z`, `bm_z`, `mom12m_z` columns (renamed to `mve`, `bm`, `mom12m`) for rows where `rebalance_date == as_of_date`, drops rows with any null among the three, and returns a DataFrame with columns `ticker`, `mve`, `bm`, `mom12m`. This is called once per `generate_rule` invocation and passed into each tool's constructor, so exploring the data doesn't mean repeated DuckDB round-trips.

  Four `BaseTool` subclasses, each constructed with the shared snapshot DataFrame and `as_of_date`:

  - **`GetDatabaseSchemaTool`** (name `get_database_schema`)
    - Args: none (empty `args_schema`).
    - Returns: a string describing the 3 available characteristics (`mve`, `bm`, `mom12m`, each with the one-line meaning from the backstory), the single `as_of_date` this call is scoped to, and the number of firms with complete (non-null) data on that date (`len(snapshot)`).
    - Description: *"Returns the schema of the firm-characteristics data available to you: which characteristics exist, what each one means, the single date's snapshot you are scoped to (the causal-masking cutoff), and how many firms have complete data as of that date. Call this first, before querying or testing anything, to know what's available."*

  - **`QueryFirmDatabaseTool`** (name `query_firm_database`)
    - Args: `sort_by: Literal["mve", "bm", "mom12m"] | None = None`, `ascending: bool = True`, `limit: int = 25`, `tickers: list[str] | None = None`.
    - Returns: if `tickers` is given, the rows for those tickers (any not found in the snapshot are silently omitted); otherwise, up to `limit` rows sorted by `sort_by` (or in the snapshot's default order if `sort_by` is omitted), each row as `{"ticker": str, "mve": float, "bm": float, "mom12m": float}`.
    - Description: *"Browse rows of the firm-characteristics snapshot. Optionally sort by one characteristic (mve, bm, or mom12m), ascending or descending, and cap how many rows come back (default 25). Or pass specific ticker symbols to look them up directly. Use this to inspect the real numbers behind any pattern you suspect."*

  - **`GetExtremeFirmsTool`** (name `get_extreme_firms`)
    - Args: `characteristic: Literal["mve", "bm", "mom12m"]`, `n: int = 10`, `direction: Literal["highest", "lowest", "both"] = "both"`.
    - Returns: `{"highest": [...], "lowest": [...]}` (only the requested key(s) populated), each list holding up to `n` rows `{"ticker": str, "mve": float, "bm": float, "mom12m": float}` — all three characteristics are always included per row (not just the requested one), so the agent can see whether firms at one extreme also cluster on the other two.
    - Description: *"Return the n firms with the highest and/or lowest values of one characteristic (mve, bm, or mom12m) as of the snapshot date, showing all three characteristics for each firm so you can spot correlations — e.g. whether the smallest firms also tend to have the strongest momentum. Use this to find natural breakpoints before setting thresholds."*

  - **`TestComplexConditionTool`** (name `test_complex_condition`)
    - Args: `condition: str` — a boolean expression over `mve`, `bm`, `mom12m` using `>`, `<`, `>=`, `<=`, `and`, `or`, `not`, and numeric literals only (the exact grammar `condition_eval.evaluate_condition` accepts).
    - Returns: on a valid condition, `{"matching_count": int, "total_firms": int, "matching_pct": float, "sample_tickers": list[str]}` (`sample_tickers` is up to 10 matching tickers). On an invalid condition (bad syntax or a disallowed name), returns a plain string describing the problem instead of raising — unlike `apply_rule` in production (which must raise on a malformed *final* rule), a malformed condition during exploration is a normal, expected event the agent should be able to see and correct, not a crash.
    - Description: *"Test any candidate BUY, SELL, or HOLD condition (for example \"bm > 0.5 and mom12m > -0.2\") against the snapshot date's real firms before committing to it. Returns how many of the firms would match, what percentage that is, and a small sample of matching tickers. There is no limit on how many times you can call this — use it repeatedly while iterating on thresholds."*

- `src/agents/llm_s_crew/crew.py` — `LLMSCrew`, a `@CrewBase`-decorated class with `agents_config = "config/agents.yaml"`, `tasks_config = "config/tasks.yaml"`. Its constructor takes `snapshot: pd.DataFrame`, `as_of_date: date`, and `model: str`. Its `@agent strategy_agent` method returns `Agent(config=self.agents_config["strategy_agent"], llm=self.model, verbose=True)`. Its `@task strategy_task` method returns `Task(config=self.tasks_config["strategy_task"], tools=[GetDatabaseSchemaTool(snapshot=self.snapshot, as_of_date=self.as_of_date), QueryFirmDatabaseTool(snapshot=self.snapshot), GetExtremeFirmsTool(snapshot=self.snapshot), TestComplexConditionTool(snapshot=self.snapshot)], output_pydantic=ScreeningRule)` (all tool constructor args must be keyword, not positional — `BaseTool` is a Pydantic model whose `__init__` only accepts `**data`). Its `@crew crew` method returns `Crew(agents=self.agents, tasks=self.tasks, process=Process.sequential, verbose=True)`.


Create **`src/agents/llm_s.py`** with a function `generate_rule(year: int, model: str | None = None, db_path: str = "data/portfolio.duckdb") -> ScreeningRule`. Resolves `as_of_date` as the `factors` table's rebalance date in December of `year - 1` (querying DuckDB for `max(rebalance_date) where rebalance_date < date(year, 1, 1)`), falling back to the table's overall earliest rebalance date if none exists (the `year=2020` edge case — log a warning when this fallback triggers, since it means that year's rule is not truly causally masked). Calls `load_snapshot(db_path, as_of_date)`, resolves `model` as `os.environ.get("LLM_S_MODEL", "anthropic/claude-sonnet-4-5")` (or whatever the current recommended Claude model string is at implementation time — check `https://docs.claude.com/en/docs/about-claude/models` if unsure, since model names are versioned and this plan's specific string may be stale by the time it is implemented), builds `LLMSCrew(snapshot, as_of_date, model)`, calls `.crew().kickoff(inputs={"as_of_date": as_of_date.isoformat(), "year": year})`, and returns `result.pydantic` (or `result.tasks_output[0].pydantic` — confirm the exact accessor against the freshness-checked docs, since this plan goes through `Crew.kickoff()` rather than a direct `Agent.kickoff()`).


Create **`src/agents/llm_s_apply.py`** with a function `apply_rule(rule: ScreeningRule, factor_row: dict) -> str` that evaluates `rule.buy_condition` and `rule.sell_condition` against a single stock's `factor_row` (a dict with keys `mve`, `bm`, `mom12m`) using `condition_eval.evaluate_condition`, and returns one of the literal strings `"buy"`, `"sell"`, or `"hold"`. Do not use Python's `eval()` on the LLM-produced condition strings — `condition_eval.evaluate_condition`'s restricted AST walk is the only path from an LLM-generated string to a boolean result anywhere in this plan.


Create **`src/agents/llm_s_signals.py`** with a function `screen(rule: ScreeningRule, rebalance_date: date, db_path: str = "data/portfolio.duckdb") -> pd.DataFrame` that reads that rebalance date's rows from the `factors` table (built by `plans/01_dataset.md`), maps each row's `mve_z`/`bm_z`/`mom12m_z` columns to the bare keys `mve`/`bm`/`mom12m` (matching the Decision Log's naming decision), applies `apply_rule` to each row, and returns a DataFrame with columns `ticker`, `signal` (one of `"buy"`/`"sell"`/`"hold"`), for every ticker that had a non-null `mve_z`, `bm_z`, and `mom12m_z` on that date. Rows with any null standardized factor are excluded from the output entirely (they cannot be evaluated against a numeric rule) but the function should log how many rows were excluded this way, since a large exclusion count is a data-quality signal worth noticing.


## Concrete Steps


Run every command from the repository root, with `plans/01_dataset.md` already implemented and `data/portfolio.duckdb` populated.


Step 1 — confirm the installed CrewAI version and its current YAML/`BaseTool`/task-level-`tools=` API, since this plan's code must match it exactly:

    uv run python -c "import crewai; print(crewai.__version__)"

Then fetch `https://docs.crewai.com/en/concepts/agents`, `https://docs.crewai.com/en/concepts/tasks`, and `https://docs.crewai.com/en/concepts/tools` to confirm the exact YAML interpolation syntax, the `@CrewBase` class shape, the current `BaseTool`/`args_schema` API, and how `output_pydantic` results are read off `Crew.kickoff()`'s return value, before writing any of the files above.


Step 2 — implement the files described in Plan of Work, then generate one real rule for 2024 (which will explore the December 2023 snapshot) and print it:

    export ANTHROPIC_API_KEY=...  # or set LLM_S_MODEL and the matching provider key
    uv run python -c "
    from src.agents.llm_s import generate_rule
    rule = generate_rule(2024)
    print(rule)
    "

Expected: a printed `ScreeningRule` whose `buy_condition` and `sell_condition` reference only `mve`, `bm`, `mom12m` with precise (not round) thresholds roughly within -3 to 3, and a `rationale` mentioning patterns from the December 2023 snapshot specifically. Compare this qualitatively against the paper's own worked 2024 example rule quoted in Plan of Work — an exact match is not expected (a different LLM run on different underlying data will produce different specific thresholds), but the shape and reasoning should be recognizably the same kind of rule. Also check, from CrewAI's verbose tool-call log, that the agent actually called `get_database_schema` and at least one other tool before producing its final answer — if it produced a rule without calling any tools, that is worth investigating (it means the LLM ignored the task's stated process).


Step 3 — apply the generated rule to one real rebalance date and inspect the signal counts:

    uv run python -c "
    from datetime import date
    from src.agents.llm_s_signals import screen
    # rule from Step 2, or re-generate it
    signals = screen(rule, date(2024, 3, 1))
    print(signals['signal'].value_counts())
    print(signals[signals['signal'] == 'buy'].head(10))
    "

Expected: three nonzero counts across `buy`, `sell`, `hold` (a rule that flags zero buys or sells for an entire month across ~500 stocks is suspicious and worth investigating before moving on), and a `buy` list containing recognizable, real ticker symbols.


## Validation and Acceptance


Run `uv run pytest tests/test_llm_s.py` and expect all tests to pass. Per `AGENTS.md`'s testing guidance to keep external calls out of unit tests, these tests must not call any LLM — they exercise `condition_eval.evaluate_condition`, `apply_rule`, `screen`, and the 4 tools (which are pure DataFrame logic with no LLM involvement) against small fixtures. Required cases:

- `evaluate_condition` returns the correct boolean for a simple known condition (for example `"bm > 0.5"` against `{"mve": 0, "bm": 1.0, "mom12m": 0}` → `True`, and against `{"mve": 0, "bm": 0.1, "mom12m": 0}` → `False`).
- `evaluate_condition` raises `ValueError` for a condition referencing a disallowed name (for example `"pe_ratio > 10"`) and for one attempting unsafe syntax (for example `"__import__('os').system('echo hi')"`), proving the evaluator rejects arbitrary code rather than merely happening not to break on the happy path.
- `apply_rule` returns `"buy"` for a fixture row matching `buy_condition` but not `sell_condition`, `"sell"` for one matching `sell_condition`, and `"hold"` for one matching neither.
- `screen`, given a fixture DuckDB `factors` table (a handful of rows for one `rebalance_date`, including at least one row with a null `_z` column), returns a DataFrame with exactly the non-null rows, correctly mapped to `mve`/`bm`/`mom12m` and screened via `apply_rule`, excluding the null row.
- `GetExtremeFirmsTool` and `QueryFirmDatabaseTool`, given a small hand-built fixture DataFrame (5-10 rows), return the expected top/bottom rows and respect `limit`/`sort_by`.
- `TestComplexConditionTool`, given the same fixture, returns the correct `matching_count`/`matching_pct` for a known condition, and returns an error string (not a raised exception) for a malformed one.


Acceptance for this plan is: `uv run pytest tests/test_llm_s.py` passes; the Concrete Steps Step 2 and Step 3 transcripts (captured for real, not fabricated) show a coherent generated rule (with visible evidence the agent used its tools) and a plausible three-way signal split on real 2024 data.


## Idempotence and Recovery


`generate_rule` makes exactly one LLM call and returns; running it twice for the same year makes two independent calls and may return two different (both individually valid) rules, since LLM output is not guaranteed deterministic — this is expected, not a bug, and later plans (specifically plan 6's interactive flow) may choose to cache a generated rule per year rather than regenerating it on every run, to keep a single backtest's results stable across reruns. `apply_rule` and `screen` are pure functions of their inputs and safe to call any number of times.


## Artifacts and Notes


Real `ScreeningRule` generated 2026-08-06 via `generate_rule(2024)` with `LLM_S_MODEL=anthropic/claude-opus-4-8`, against the real 2023-12-01 snapshot (480 firms):

```
year=2024
buy_condition='(mom12m > 0.55 and mve > -0.9) or (bm > 0.2 and mom12m > -0.15)'
sell_condition='mom12m < -0.85 or (bm > 0.4 and mom12m < -0.5)'
rationale="Data exploration on the 2023-12-01 causally-masked snapshot (480 S&P 500 firms, all
characteristics as cross-sectional z-scores) revealed the following. SIZE (mve) is strongly
right-skewed: mega-caps AAPL (3.96), MSFT (3.89), GOOG/GOOGL (~3.44), AMZN (3.37), NVDA (3.12) and
META (2.83) dominate the right tail, while the extreme negative mve names (ECHO -4.25, BNY -3.08)
are distressed/data-artifact firms that also carry corrupt bm values. VALUE (bm) is extremely
right-skewed with two clear outliers (BNY 20.47, ECHO 6.45) that are artifacts; the real value
cluster is banks/financials and old-economy cyclicals. MOMENTUM (mom12m) is the strongest and
best-populated signal for late 2023, with the Magnificent-Seven/AI complex in the right tail and
clean-energy/broken-growth names in the left tail. Momentum is chosen as the dominant BUY engine,
with a size floor to exclude distressed artifact names, plus a secondary value-without-collapse
leg for diversification. SELL targets deep-negative-momentum falling knives and high-bm value
traps whose price is also declining. BUY matches 117 firms (~24%), SELL 83 firms (~17%),
non-overlapping, verified via test_complex_condition."
```

Applying this rule via `screen(rule, date(2024, 3, 1))` against real `data/portfolio.duckdb` data:

```
signal
hold    306
buy     106
sell     71
```

Sample buy tickers: NFLX, NTAP, NVDA, NOW, MU, MSFT, NVR, NWSA, PHM, PH.

Full verbose CrewAI trace (tool calls, intermediate reasoning) captured at generation time; not checked into the repo, but reproducible by re-running Concrete Steps Step 2 with the same `LLM_S_MODEL`.


## Interfaces and Dependencies


This plan depends on `plans/01_dataset.md`'s output: the `factors` table in `data/portfolio.duckdb`, specifically its `rebalance_date`, `ticker`, `mve_z`, `bm_z`, and `mom12m_z` columns. This plan depends on `crewai` (already in `pyproject.toml`) for the Agent/Task machinery, and on whichever LLM provider environment variable is set (`ANTHROPIC_API_KEY` by default, or the matching key for OpenAI/Gemini if `LLM_S_MODEL` is overridden).


At the end of this plan, the following must exist and be usable by later plans exactly as described:


In `src/agents/llm_s_schema.py`, the Pydantic model `ScreeningRule` with fields `year: int`, `buy_condition: str`, `sell_condition: str`, `rationale: str`.


In `src/agents/llm_s.py`, `def generate_rule(year: int, model: str | None = None, db_path: str = "data/portfolio.duckdb") -> ScreeningRule`.


In `src/agents/llm_s_signals.py`, `def screen(rule: ScreeningRule, rebalance_date: date, db_path: str = "data/portfolio.duckdb") -> pd.DataFrame`, returning a DataFrame with columns `ticker: str`, `signal: str` (one of `"buy"`, `"sell"`, `"hold"`). Plan 4 (`plans/04_candidate_scanner.md`) calls `screen` to get LLM-S's buy set for a given rebalance date, and combines it with LLM-F's buy set from plan 3.
