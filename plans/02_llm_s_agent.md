# Build the LLM-S fundamentals screening agent


This ExecPlan is a living document. The sections `Progress`, `Surprises & Discoveries`, `Decision Log`, and `Outcomes & Retrospective` must be kept up to date as work proceeds. This plan must be maintained in accordance with `PLANS.md` at the repository root. This plan builds on `plans/01_dataset.md`, which is checked into this repository at that path — this plan does not repeat that plan's factor-computation details, but does restate, in the Interfaces and Dependencies section below, exactly which tables and columns this plan reads from it, so a reader does not need to have plan 1 open to follow along.


## Purpose / Big Picture


After this plan is done, a person can run one command for any calendar year between 2020 and 2024 and see a small set of plain-English buy and sell rules — things like "buy companies with a book-to-market ratio above 0.4 and momentum above -0.1" — that a large language model produced after looking at that year's actual distribution of company sizes, valuations, and momentum. Given those rules, the same command applies them mechanically to every stock and prints, for a chosen date, exactly which stocks would be flagged buy, sell, or neither. That is LLM-S: the fundamentals-screening half of the two-agent screening layer described in the paper this project reproduces ("Designing Agentic AI-Based Screening for Portfolio Investment", Caner, Capponi, Sun, and Tan, arXiv:2603.23300).


The paper reruns its fundamentals agent once per year, on the theory that broad economic regimes (what counts as "large," "cheap," or "in an uptrend") shift slowly enough that a yearly refresh is sufficient, while still letting the agent adapt its thresholds to each year's actual data rather than using one fixed rule forever. This plan keeps that same yearly cadence.


## Progress


- [ ] Define the Pydantic schema for LLM-S's output (a rule, plus its rationale).
- [ ] Write the CrewAI agent and task that, given one year's standardized factor distribution, produces that schema.
- [ ] Write the deterministic rule-application function that takes a produced rule and a set of per-stock factor values and returns a buy/sell/hold signal per stock.
- [ ] Wire in the configurable-model environment variable (`LLM_S_MODEL`).
- [ ] Write `tests/test_llm_s.py` covering rule application (not rule generation, which needs a real LLM call) against fixtures.
- [ ] Manually run the agent for at least one real year and sanity-check the rule it produces against the paper's own worked 2024 example.


## Surprises & Discoveries


(Empty until this plan is implemented. Record here anything about how the chosen LLM actually behaves — for example, if it produces rules referencing factors other than the three it was given, or thresholds far outside the standardized [-3, 3]-ish range one would expect from mean-0/variance-1 data, both of which would need a decision about whether to reject and retry the generation.)


## Decision Log


- Decision: default LLM-S to Anthropic Claude via CrewAI's native LLM string syntax (e.g. `Agent(llm="anthropic/claude-sonnet-4-5")`), but read the actual model string from an environment variable `LLM_S_MODEL` (falling back to a hardcoded Anthropic default if the variable is unset), so a user can point LLM-S at an OpenAI or Gemini model instead by setting that one environment variable, without touching code.
  Rationale: the user explicitly asked for Anthropic as the default while wanting OpenAI and Gemini available "at will." CrewAI's `LLM` class (and the plain string shorthand `Agent(llm="provider/model")`) is built on LiteLLM-style provider prefixes, so `LLM_S_MODEL=openai/gpt-4o` or `LLM_S_MODEL=gemini/gemini-2.5-pro` work with zero additional code once the environment variable is read — this is not a new abstraction, just reading a string from the environment where a literal string currently sits.
  Date/Author: 2026-08-05, decided by repository owner during planning interview.
- Decision: rule generation happens once per calendar year (a single LLM call per year), and rule application is 100% deterministic Python with no LLM calls.
  Rationale: matches the paper's stated annual rerun cadence for its fundamentals agent, and keeps the expensive/nondeterministic part (one LLM call) cleanly separated from the cheap/deterministic part (evaluating a threshold rule against a table of numbers), which also makes rule application trivially unit-testable without mocking an LLM.
  Date/Author: 2026-08-05, plan author.


## Outcomes & Retrospective


(To be filled in once this plan is implemented and validated.)


## Context and Orientation


This plan adds a new package `src/agents/llm_s.py` (a sibling to the `src/dataset/` package built in `plans/01_dataset.md`) to this Python 3.12, `uv`-managed repository. All commands below run from the repository root and assume `plans/01_dataset.md` has already been implemented, so `data/portfolio.duckdb` exists with a populated `factors` table.


This plan uses CrewAI, an agent-orchestration framework already a dependency of this repository (see `pyproject.toml`'s `crewai[anthropic,azure-ai-inference,google-genai,tools]` entry and the reference document `CREWAI.md` at the repository root). Two CrewAI terms of art are used below. An "Agent" is CrewAI's object representing one LLM persona with a role, a goal, and a backstory — under the hood it is a wrapper around a chat-completions call to whichever LLM you configure it with. A "Task" is CrewAI's object pairing an Agent with a specific description of work and an expected output shape; calling `.kickoff()` on an Agent (directly, without needing a multi-agent "Crew") runs one Task and returns a `LiteAgentOutput` object whose `.pydantic` attribute holds a validated instance of whatever Pydantic model you told the Agent to produce, if you set `response_format`.


Before writing any CrewAI code, per `CREWAI.md`'s mandatory freshness check, run:

    uv run python -c "import crewai; print(crewai.__version__)"

and compare against the latest version on PyPI (`https://pypi.org/pypi/crewai/json`) and the changelog (`https://docs.crewai.com/en/changelog`), since CrewAI's API for direct agent kickoff and structured output has changed across versions and this plan's code examples must match whatever is actually installed, not this plan's assumptions.


"Standardized factor" here means the `mve_z`, `bm_z`, and `mom12m_z` columns produced by `plans/01_dataset.md`'s `factors` table — each one rescaled, separately for each rebalance date, to cross-sectional mean 0 and variance 1, as the paper's own methodology specifies before showing factor data to its fundamentals agent.


## Plan of Work


Create `src/agents/llm_s_schema.py` defining a Pydantic model `ScreeningRule` with fields: `year: int`, `buy_condition: str` (a human-readable boolean expression over `mve_z`, `bm_z`, `mom12m_z`, for example `"bm_z > 0.4 and mom12m_z > -0.1"`), `sell_condition: str` (same shape, for example `"bm_z < -0.3 or mom12m_z < -0.6 or mve_z < -0.8"`), and `rationale: str` (the agent's plain-English explanation for why it chose these thresholds). This mirrors the shape of the paper's own worked example, which the paper phrases as "targets undervalued (high bm), reasonably sized (mve>0.3) companies with positive momentum (mom12m>-0.5)" for a buy rule and a parallel form for sell.


Create `src/agents/llm_s.py` with a function `generate_rule(year: int, factor_summary: dict, model: str | None = None) -> ScreeningRule`. `factor_summary` is a small dictionary of cross-sectional summary statistics for that year (min, max, mean, standard deviation, and a handful of percentiles for each of `mve_z`, `bm_z`, `mom12m_z`, computed by pooling all rebalance dates within that calendar year) — this is deliberately a compact numerical summary, not the full per-stock table, both because the paper's LLM-S is described as reasoning about "the distribution of the data," not memorizing every row, and because a compact summary keeps the prompt small and the agent's behavior easy to reason about. Build the CrewAI `Agent` with a role along the lines of "portfolio manager screening for fundamentally attractive stocks," a backstory establishing that it should behave like a value-and-momentum-aware analyst (matching the paper's own framing of the agent adopting "a portfolio manager persona"), and instruct it, in the Task description, to follow the paper's own four-step process verbatim: explore the summary statistics for extreme values, clustering, or breakpoints and correlations between factors; develop a rule based on stated economic intuition; define specific numeric thresholds for buy and sell; and provide a rationale. Set `response_format=ScreeningRule` on the Task (or Agent, depending on what the installed CrewAI version's API actually calls for — check this against the live docs per the freshness-check step above) so `.kickoff()` returns a validated `ScreeningRule` directly. Resolve the LLM string as `os.environ.get("LLM_S_MODEL", "anthropic/claude-sonnet-4-5")` (or whatever the current recommended Claude model string is at implementation time — check `https://docs.claude.com/en/docs/about-claude/models` if unsure, since model names are versioned and this plan's specific string may be stale by the time it is implemented).


Create `src/agents/llm_s_apply.py` with a function `apply_rule(rule: ScreeningRule, factor_row: dict) -> str` that evaluates `rule.buy_condition` and `rule.sell_condition` against a single stock's `factor_row` (a dict with keys `mve_z`, `bm_z`, `mom12m_z`) and returns one of the literal strings `"buy"`, `"sell"`, or `"hold"`. Do not use Python's `eval()` on the LLM-produced condition strings directly against arbitrary input — instead, parse the condition string with a small, explicit expression evaluator restricted to the three known variable names and the operators `>`, `<`, `>=`, `<=`, `and`, `or`, `not`, comparing against numeric literals (Python's `ast` module, using `ast.parse(expr, mode="eval")` and then walking the resulting tree while rejecting any node type outside a small allow-list of `Compare`, `BoolOp`, `UnaryOp`, `Name`, `Constant`, and the specific comparison/boolean operators, is enough for this purpose and avoids arbitrary code execution from a string an LLM generated). If a condition references any name outside `{"mve_z", "bm_z", "mom12m_z"}` or uses any disallowed syntax, `apply_rule` must raise a clear `ValueError` naming the offending token, not silently ignore it — a malformed rule should be visibly rejected, not silently misapplied.


Create `src/agents/llm_s_signals.py` with a function `screen(rule: ScreeningRule, rebalance_date: date, db_path: str = "data/portfolio.duckdb") -> pd.DataFrame` that reads that rebalance date's rows from the `factors` table (built by `plans/01_dataset.md`), applies `apply_rule` to each row, and returns a DataFrame with columns `ticker`, `signal` (one of `"buy"`/`"sell"`/`"hold"`), for every ticker that had a non-null `mve_z`, `bm_z`, and `mom12m_z` on that date. Rows with any null standardized factor are excluded from the output entirely (they cannot be evaluated against a numeric rule) but the function should log how many rows were excluded this way, since a large exclusion count is a data-quality signal worth noticing.


## Concrete Steps


Run every command from the repository root, with `plans/01_dataset.md` already implemented and `data/portfolio.duckdb` populated.


Step 1 — confirm the installed CrewAI version and its current structured-output API, since this plan's code must match it exactly:

    uv run python -c "import crewai; print(crewai.__version__)"

Then fetch `https://docs.crewai.com/en/concepts/agents` and `https://docs.crewai.com/en/concepts/tasks` (or whatever the current docs URL structure is) to confirm the exact syntax for direct-agent structured-output kickoff before writing `src/agents/llm_s.py`.


Step 2 — implement the files described in Plan of Work, then generate one real rule and print it:

    export ANTHROPIC_API_KEY=...  # or set LLM_S_MODEL and the matching provider key
    uv run python -c "
    from src.agents.llm_s import generate_rule
    import duckdb
    con = duckdb.connect('data/portfolio.duckdb')
    df = con.execute(\"select mve_z, bm_z, mom12m_z from factors where rebalance_date >= '2024-01-01' and rebalance_date < '2025-01-01'\").fetchdf()
    summary = df.describe(percentiles=[0.1, 0.25, 0.5, 0.75, 0.9]).to_dict()
    rule = generate_rule(2024, summary)
    print(rule)
    "

Expected: a printed `ScreeningRule` whose `buy_condition` and `sell_condition` reference only `mve_z`, `bm_z`, `mom12m_z` with plausible standardized thresholds (roughly within -3 to 3), and a `rationale` that reads as coherent financial reasoning. Compare this qualitatively against the paper's own worked 2024 example rule quoted in Plan of Work — an exact match is not expected (a different LLM run on different underlying data will produce different specific thresholds), but the shape and reasoning should be recognizably the same kind of rule.


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


Run `uv run pytest tests/test_llm_s.py` and expect all tests to pass. Per `AGENTS.md`'s testing guidance to keep external calls out of unit tests, these tests must not call any LLM — they exercise only `apply_rule` and `screen`. Required cases: a test that constructs a `ScreeningRule` by hand with a simple known condition (for example `buy_condition="bm_z > 0.5"`) and asserts `apply_rule` returns `"buy"` for a fixture row with `bm_z=1.0` and `"hold"` for a fixture row with `bm_z=0.1` (assuming no matching sell condition); a test that a condition referencing a disallowed name (for example `"pe_ratio > 10"`) raises `ValueError`; and a test that a condition attempting something unsafe (for example `"__import__('os').system('echo hi')"`) raises `ValueError` rather than executing anything, proving the `ast`-based evaluator actually rejects arbitrary code rather than merely happening not to break on the happy path.


Acceptance for this plan is: `uv run pytest tests/test_llm_s.py` passes; the Concrete Steps Step 2 and Step 3 transcripts (captured for real, not fabricated) show a coherent generated rule and a plausible three-way signal split on real 2024 data.


## Idempotence and Recovery


`generate_rule` makes exactly one LLM call and returns; running it twice for the same year makes two independent calls and may return two different (both individually valid) rules, since LLM output is not guaranteed deterministic — this is expected, not a bug, and later plans (specifically plan 6's interactive flow) may choose to cache a generated rule per year rather than regenerating it on every run, to keep a single backtest's results stable across reruns. `apply_rule` and `screen` are pure functions of their inputs and safe to call any number of times.


## Artifacts and Notes


(To be filled in with the real generated `ScreeningRule` transcript and signal counts from Concrete Steps once this plan is executed.)


## Interfaces and Dependencies


This plan depends on `plans/01_dataset.md`'s output: the `factors` table in `data/portfolio.duckdb`, specifically its `rebalance_date`, `ticker`, `mve_z`, `bm_z`, and `mom12m_z` columns. This plan depends on `crewai` (already in `pyproject.toml`) for the Agent/Task machinery, and on whichever LLM provider environment variable is set (`ANTHROPIC_API_KEY` by default, or the matching key for OpenAI/Gemini if `LLM_S_MODEL` is overridden).


At the end of this plan, the following must exist and be usable by later plans exactly as described:


In `src/agents/llm_s_schema.py`, the Pydantic model `ScreeningRule` with fields `year: int`, `buy_condition: str`, `sell_condition: str`, `rationale: str`.


In `src/agents/llm_s.py`, `def generate_rule(year: int, factor_summary: dict, model: str | None = None) -> ScreeningRule`.


In `src/agents/llm_s_signals.py`, `def screen(rule: ScreeningRule, rebalance_date: date, db_path: str = "data/portfolio.duckdb") -> pd.DataFrame`, returning a DataFrame with columns `ticker: str`, `signal: str` (one of `"buy"`, `"sell"`, `"hold"`). Plan 4 (`plans/04_candidate_scanner.md`) calls `screen` to get LLM-S's buy set for a given rebalance date, and combines it with LLM-F's buy set from plan 3.
