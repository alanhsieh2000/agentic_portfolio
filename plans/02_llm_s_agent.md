# Build the LLM-S fundamentals screening agent


This ExecPlan is a living document. The sections `Progress`, `Surprises & Discoveries`, `Decision Log`, and `Outcomes & Retrospective` must be kept up to date as work proceeds. This plan must be maintained in accordance with `PLANS.md` at the repository root. This plan builds on `plans/01_dataset.md`, which is checked into this repository at that path — this plan does not repeat that plan's factor-computation details, but does restate, in the Interfaces and Dependencies section below, exactly which tables and columns this plan reads from it, so a reader does not need to have plan 1 open to follow along.


## Purpose / Big Picture


After this plan is done, a person can run one command for any calendar year between 2020 and 2024 and see a small set of plain-English buy and sell rules — things like "buy companies with a book-to-market ratio above 0.4 and momentum above -0.1" — that a large language model produced after looking at that year's actual distribution of company sizes, valuations, and momentum. Given those rules, the same command applies them mechanically to every stock and prints, for a chosen date, exactly which stocks would be flagged buy, sell, or neither. That is LLM-S: the fundamentals-screening half of the two-agent screening layer described in the paper this project reproduces ("Designing Agentic AI-Based Screening for Portfolio Investment", Caner, Capponi, Sun, and Tan, arXiv:2603.23300).


The paper reruns its fundamentals agent once per year, on the theory that broad economic regimes (what counts as "large," "cheap," or "in an uptrend") shift slowly enough that a yearly refresh is sufficient, while still letting the agent adapt its thresholds to each year's actual data rather than using one fixed rule forever. This plan keeps that same yearly cadence.


## Progress


- [x] (2026-08-05) Define the Pydantic schema for LLM-S's output (a rule, plus its rationale), using the paper's own variable names (`mve`, `bm`, `mom12m`) rather than a `_z`-suffixed naming scheme. Implemented in `src/agents/llm_s_schema.py`: `ScreeningRule(year: int, buy_condition: str, sell_condition: str, rationale: str)`. Verified end-to-end: constructing a `ScreeningRule` with a sample buy/sell condition round-trips through `.model_dump()` correctly.
- [ ] Write the shared, restricted condition evaluator (`src/agents/condition_eval.py`) that both rule-application and the `test_complex_condition` tool use, so what the agent tests during exploration has identical semantics to what runs in production.
- [ ] Write the `agents.yaml`/`tasks.yaml` config and `LLMSCrew` class (CrewAI's `@CrewBase` pattern), using the paper's verbatim role/goal/backstory/description/expected_output text (Appendix C.5 of arXiv:2603.23300v1), parameterized only by `{as_of_date}`.
- [ ] Implement the 4 tools the paper's Task attaches (`get_database_schema`, `query_firm_database`, `get_extreme_firms`, `test_complex_condition`), scoped to a single causally-masked snapshot date per crew invocation.
- [ ] Write `generate_rule(year, model=None, db_path=...)` that resolves the causal-masking snapshot date for that year, builds the crew, kicks it off, and returns a validated `ScreeningRule`.
- [ ] Write the deterministic rule-application function that takes a produced rule and a set of per-stock factor values and returns a buy/sell/hold signal per stock.
- [ ] Write `tests/test_llm_s.py` covering the evaluator, rule application, and the 4 tools against a small fixture table (not rule generation itself, which needs a real LLM call).
- [ ] Manually run the agent for at least one real year and sanity-check the rule it produces against the paper's own worked 2024 example.


## Surprises & Discoveries


(Empty until this plan is implemented. Record here anything about how the chosen LLM actually behaves — for example, if it produces rules referencing factors other than the three it was given, or thresholds far outside the standardized [-3, 3]-ish range one would expect from mean-0/variance-1 data, both of which would need a decision about whether to reject and retry the generation. Also record here whether the agent actually calls its 4 tools before answering, or tries to shortcut straight to a final rule without exploring the data.)


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


## Outcomes & Retrospective


(To be filled in once this plan is implemented and validated.)


## Context and Orientation


This plan adds a new package `src/agents/` (a sibling to the `src/dataset/` package built in `plans/01_dataset.md`) to this Python 3.12, `uv`-managed repository. All commands below run from the repository root and assume `plans/01_dataset.md` has already been implemented, so `data/portfolio.duckdb` exists with a populated `factors` table.


This plan uses CrewAI, an agent-orchestration framework already a dependency of this repository (see `pyproject.toml`'s `crewai[anthropic,azure-ai-inference,google-genai,tools]` entry and the reference document `CREWAI.md` at the repository root). Two CrewAI terms of art are used below. An "Agent" is CrewAI's object representing one LLM persona with a role, a goal, and a backstory — under the hood it is a wrapper around a chat-completions call to whichever LLM you configure it with. A "Task" is CrewAI's object pairing an Agent with a specific description of work and an expected output shape.


This plan follows `CREWAI.md`'s documented project layout: a `config/agents.yaml` and `config/tasks.yaml` pair defining the agent's and task's text fields (role, goal, backstory; description, expected_output), loaded by a `@CrewBase`-decorated Python class (`LLMSCrew`, in `src/agents/llm_s/crew.py`) that attaches the 4 tools to the task and the LLM string to the agent (neither of which YAML can express) and exposes a `@crew` method returning a single-agent, single-task `Crew(process=Process.sequential)`. `generate_rule` builds this crew, calls `.kickoff(inputs={"as_of_date": ...})` (CrewAI interpolates `{as_of_date}` into the YAML text at kickoff time), and reads `result.pydantic` (via `output_pydantic=ScreeningRule` set on the task).


The prompt text itself — every word of `role`, `goal`, `backstory`, `description`, and `expected_output` below — is quoted directly from Appendix C.5 of arXiv:2603.23300v1 ("LLM-S Prompts and Outputs"), with only `{as_of_date}` substituted for the paper's hardcoded "December 2023." Do not paraphrase or "clean up" this text when implementing — it is intentionally verbatim, including its em-dashes, capitalization, and informal tone, because the user explicitly asked to keep the prompt the same as the paper's.


Before writing any CrewAI code, per `CREWAI.md`'s mandatory freshness check, run:

    uv run python -c "import crewai; print(crewai.__version__)"

and compare against the latest version on PyPI (`https://pypi.org/pypi/crewai/json`) and the changelog (`https://docs.crewai.com/en/changelog`), since CrewAI's API for YAML config, structured output, and custom tools has changed across versions and this plan's code examples must match whatever is actually installed, not this plan's assumptions. Additionally, since this plan uses YAML config, custom `BaseTool` subclasses, and task-level `tools=` overrides, also fetch `https://docs.crewai.com/en/concepts/tools` and confirm the current `BaseTool`/`args_schema` API matches what's assumed below, alongside `https://docs.crewai.com/en/concepts/agents` and `https://docs.crewai.com/en/concepts/tasks` for the `@CrewBase` class shape and `output_pydantic` result access.


"Standardized factor" here means the `mve_z`, `bm_z`, and `mom12m_z` columns produced by `plans/01_dataset.md`'s `factors` table — each one rescaled, separately for each rebalance date, to cross-sectional mean 0 and variance 1, as the paper's own methodology specifies before showing factor data to its fundamentals agent. This plan presents those `_z` columns to the agent and its tools under the bare names `mve`, `bm`, `mom12m` (see Decision Log above), matching the paper's own naming.


## Plan of Work


Create **`src/agents/condition_eval.py`** with a module-level constant `ALLOWED_NAMES = {"mve", "bm", "mom12m"}` and a function `evaluate_condition(condition: str, values: dict[str, float]) -> bool` that parses `condition` with `ast.parse(condition, mode="eval")` and walks the resulting tree, rejecting any node type outside `Compare`, `BoolOp`, `UnaryOp`, `Name`, `Constant`, and the comparison/boolean operators `>`, `<`, `>=`, `<=`, `and`, `or`, `not`. If the expression references any name outside `ALLOWED_NAMES`, or uses disallowed syntax, raise `ValueError` naming the offending token. This avoids arbitrary code execution from a string an LLM generated. Both `llm_s_apply.py` (production rule application) and the `test_complex_condition` tool (agent-time rule testing) share this one implementation — the agent should never be able to "test" a rule with semantics that differ from what will actually run later.


Create **`src/agents/llm_s_schema.py`** defining a Pydantic model `ScreeningRule` with fields: `year: int`, `buy_condition: str` (a human-readable boolean expression over `mve`, `bm`, `mom12m`, for example `"bm > 0.4 and mom12m > -0.1"`), `sell_condition: str` (same shape, for example `"bm < -0.3 or mom12m < -0.6 or mve < -0.8"`), and `rationale: str` (the agent's plain-English explanation for why it chose these thresholds). This mirrors the shape of the paper's own worked example, which the paper phrases as "targets undervalued (high bm), reasonably sized (mve>0.3) companies with positive momentum (mom12m>-0.5)" for a buy rule and a parallel form for sell.


Create the subpackage **`src/agents/llm_s/`** holding the CrewAI machinery:


- `src/agents/llm_s/config/agents.yaml`:

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

- `src/agents/llm_s/config/tasks.yaml`:

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


- `src/agents/llm_s/tools.py` — a snapshot loader plus the 4 tools, all reading from one pre-loaded, causally-masked DataFrame rather than hitting DuckDB on every tool call:

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

- `src/agents/llm_s/crew.py` — `LLMSCrew`, a `@CrewBase`-decorated class with `agents_config = "config/agents.yaml"`, `tasks_config = "config/tasks.yaml"`. Its constructor takes `snapshot: pd.DataFrame`, `as_of_date: date`, and `model: str`. Its `@agent strategy_agent` method returns `Agent(config=self.agents_config["strategy_agent"], llm=self.model, verbose=True)`. Its `@task strategy_task` method returns `Task(config=self.tasks_config["strategy_task"], tools=[GetDatabaseSchemaTool(self.snapshot, self.as_of_date), QueryFirmDatabaseTool(self.snapshot), GetExtremeFirmsTool(self.snapshot), TestComplexConditionTool(self.snapshot)], output_pydantic=ScreeningRule)`. Its `@crew crew` method returns `Crew(agents=self.agents, tasks=self.tasks, process=Process.sequential, verbose=True)`.


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
- `GetExtremeFirmsTool` and `QueryFirmDatabaseTool`, given a small hand-built fixture DataFrame (5-10 rows), return the expected top/bottom rows and respect `limit`/`sort_by`.
- `TestComplexConditionTool`, given the same fixture, returns the correct `matching_count`/`matching_pct` for a known condition, and returns an error string (not a raised exception) for a malformed one.


Acceptance for this plan is: `uv run pytest tests/test_llm_s.py` passes; the Concrete Steps Step 2 and Step 3 transcripts (captured for real, not fabricated) show a coherent generated rule (with visible evidence the agent used its tools) and a plausible three-way signal split on real 2024 data.


## Idempotence and Recovery


`generate_rule` makes exactly one LLM call and returns; running it twice for the same year makes two independent calls and may return two different (both individually valid) rules, since LLM output is not guaranteed deterministic — this is expected, not a bug, and later plans (specifically plan 6's interactive flow) may choose to cache a generated rule per year rather than regenerating it on every run, to keep a single backtest's results stable across reruns. `apply_rule` and `screen` are pure functions of their inputs and safe to call any number of times.


## Artifacts and Notes


(To be filled in with the real generated `ScreeningRule` transcript and signal counts from Concrete Steps once this plan is executed.)


## Interfaces and Dependencies


This plan depends on `plans/01_dataset.md`'s output: the `factors` table in `data/portfolio.duckdb`, specifically its `rebalance_date`, `ticker`, `mve_z`, `bm_z`, and `mom12m_z` columns. This plan depends on `crewai` (already in `pyproject.toml`) for the Agent/Task machinery, and on whichever LLM provider environment variable is set (`ANTHROPIC_API_KEY` by default, or the matching key for OpenAI/Gemini if `LLM_S_MODEL` is overridden).


At the end of this plan, the following must exist and be usable by later plans exactly as described:


In `src/agents/llm_s_schema.py`, the Pydantic model `ScreeningRule` with fields `year: int`, `buy_condition: str`, `sell_condition: str`, `rationale: str`.


In `src/agents/llm_s.py`, `def generate_rule(year: int, model: str | None = None, db_path: str = "data/portfolio.duckdb") -> ScreeningRule`.


In `src/agents/llm_s_signals.py`, `def screen(rule: ScreeningRule, rebalance_date: date, db_path: str = "data/portfolio.duckdb") -> pd.DataFrame`, returning a DataFrame with columns `ticker: str`, `signal: str` (one of `"buy"`, `"sell"`, `"hold"`). Plan 4 (`plans/04_candidate_scanner.md`) calls `screen` to get LLM-S's buy set for a given rebalance date, and combines it with LLM-F's buy set from plan 3.
