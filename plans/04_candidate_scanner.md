# Build the consensus candidate scanner


This ExecPlan is a living document. The sections `Progress`, `Surprises & Discoveries`, `Decision Log`, and `Outcomes & Retrospective` must be kept up to date as work proceeds. This plan must be maintained in accordance with `PLANS.md` at the repository root. This plan builds on `plans/02_llm_s_agent.md` and `plans/03_llm_f_agent.md` (both checked into this repository), which each produce a per-ticker buy/sell/hold signal for a given date; this plan combines those two signal sets into one final candidate list.


## Purpose / Big Picture


After this plan is done, a person can run one command for a chosen rebalance date and get back a single, short list of tickers — the stocks both the fundamentals agent (LLM-S) and the sentiment agent (LLM-F) agree are worth buying, or, when they barely overlap, the combined set of everything either agent likes. This is the "candidate scanner" named in `README.md`, and it is the mechanical heart of what the paper this project reproduces ("Designing Agentic AI-Based Screening for Portfolio Investment", Caner, Capponi, Sun, and Tan, arXiv:2603.23300) calls its consensus or deliberation step: two independent, differently-informed agents each cast a vote, and this plan turns those two votes into one candidate set for the optimizer (built in `plans/05_optimizer_and_allocation.md`) to size.


## Progress


- [ ] Implement the two-agent consensus rule (intersection, falling back to union when the intersection is too small).
- [ ] Implement single-agent fallback mode for when only LLM-S or only LLM-F signals are available or requested.
- [ ] Write `tests/test_candidate_scanner.py` covering both the two-agent consensus rule and the single-agent fallback against small hand-built fixtures.
- [ ] Manually run the scanner against one real rebalance date using real LLM-S and LLM-F output and sanity-check the resulting candidate count.


## Surprises & Discoveries


(Empty until this plan is implemented. Record here anything about how often the intersection-versus-union fallback actually triggers on real 2020-2024 data — the paper's own framing suggests the fallback should be rare, since LLM-S and LLM-F are each screening a ~500-stock universe down to a much smaller buy set, so their intersection is usually large enough on its own; if the fallback triggers on most rebalance dates in practice, that is worth recording and thinking about.)


## Decision Log


- Decision: implement the paper's exact two-agent consensus rule — recommend the intersection of the two agents' buy sets, unless that intersection has cardinality 1 or fewer, in which case recommend the union instead — rather than a simpler always-intersection or always-union rule.
  Rationale: this is the rule stated explicitly in the paper this project reproduces, and the paper's own stated justification (it "reduces the hallucination risk inherent in any single agent while preserving the complementarity between fundamental and sentiment signals") is a real methodological point worth preserving rather than simplifying away.
  Date/Author: 2026-08-05, plan author, following the paper's stated methodology.
- Decision: support running the scanner with only one of LLM-S or LLM-F's signals supplied (no consensus logic at all — just that one agent's buy set), in addition to the two-agent consensus mode.
  Rationale: `README.md` explicitly describes the candidate scanner as using "the rules proposed by LLM-S and/or LLM-F agents" (emphasis on "and/or"), which this plan reads as a requirement to support single-agent operation, not just two-agent consensus.
  Date/Author: 2026-08-05, plan author, following README.md's explicit wording.


## Outcomes & Retrospective


(To be filled in once this plan is implemented and validated.)


## Context and Orientation


This plan adds `src/scanner/candidate_scanner.py` to this Python 3.12, `uv`-managed repository, a new package sibling to `src/dataset/` (from `plans/01_dataset.md`) and `src/agents/` (from `plans/02_llm_s_agent.md` and `plans/03_llm_f_agent.md`). This plan has no direct dependency on `data/portfolio.duckdb` or any external API — it is pure Python logic operating on the DataFrames that `plans/02_llm_s_agent.md`'s `screen` function and `plans/03_llm_f_agent.md`'s `screen_month` function already produce, each with columns `ticker: str`, `signal: str` where `signal` is one of `"buy"`, `"sell"`, `"hold"`.


"Buy set" means the set of tickers with `signal == "buy"` in one of those two DataFrames. "Consensus" here specifically means the two-agent combination rule described in Purpose / Big Picture and formalized in Plan of Work below — it does not involve any further LLM reasoning or deliberation; by the time this plan's code runs, both agents have already independently produced their final signals, and combining those two already-final answers is deterministic set arithmetic.


## Plan of Work


Create `src/scanner/candidate_scanner.py` with a function:


    def scan(
        llm_s_signals: pd.DataFrame | None,
        llm_f_signals: pd.DataFrame | None,
    ) -> list[str]


Both parameters are optional DataFrames shaped like `plans/02_llm_s_agent.md`'s `screen` output and `plans/03_llm_f_agent.md`'s `screen_month` output respectively (columns `ticker`, `signal`); at least one must be provided (passing both as `None` is a programming error and should raise `ValueError`, not return an empty list silently). If only one of the two is provided, `scan` returns that agent's buy set directly (the "and/or" single-agent mode from the Decision Log) — the sorted list of `ticker` values where `signal == "buy"`. If both are provided, compute `buy_s` (LLM-S's buy set) and `buy_f` (LLM-F's buy set) as Python `set` objects, compute their intersection, and if `len(intersection) > 1`, return the sorted intersection; otherwise (intersection has 0 or 1 tickers) return the sorted union `buy_s | buy_f`. Always return a plain sorted `list[str]` of ticker symbols — this is deliberately the simplest possible output type, since every consumer downstream (plan 5's optimizer) needs nothing more than "which tickers are candidates."


Add a second function, `scan_with_detail`, with the same inputs but returning a small dict additionally reporting which branch was taken (`"intersection"`, `"union"`, `"llm_s_only"`, or `"llm_f_only"`) and the raw sizes of each set involved — this is what the Concrete Steps and the Surprises & Discoveries measurement (how often the fallback triggers) actually use; `scan` itself can be implemented as a one-line wrapper calling `scan_with_detail(...)["candidates"]` to avoid duplicating the logic.


## Concrete Steps


Run every command from the repository root, with `plans/01_dataset.md`, `plans/02_llm_s_agent.md`, and `plans/03_llm_f_agent.md` already implemented.


Step 1 — implement `src/scanner/candidate_scanner.py` as described, then exercise it against real output from both prior agents for one rebalance date:

    uv run python -c "
    from datetime import date
    from src.agents.llm_s import generate_rule
    from src.agents.llm_s_signals import screen as screen_s
    from src.agents.llm_f_signals import screen_month
    from src.scanner.candidate_scanner import scan_with_detail
    import duckdb
    con = duckdb.connect('data/portfolio.duckdb')
    df = con.execute(\"select mve_z, bm_z, mom12m_z from factors where rebalance_date >= '2024-01-01' and rebalance_date < '2025-01-01'\").fetchdf()
    rule = generate_rule(2024, df.describe().to_dict())
    s_signals = screen_s(rule, date(2024, 3, 1))
    f_signals = screen_month(2024, 3)
    result = scan_with_detail(s_signals, f_signals)
    print(result['branch'], len(result['candidates']))
    print(result['candidates'])
    "

Expected: a printed branch name and a candidate list whose size is small relative to the ~500-stock universe (the paper's own framing is that this step "substantially" narrows the candidate pool), containing recognizable real ticker symbols.


## Validation and Acceptance


Run `uv run pytest tests/test_candidate_scanner.py` and expect all tests to pass. These tests use small, hand-built fixture DataFrames only — no database, no `yfinance`, no LLM calls, matching `AGENTS.md`'s testing guidance and making this the fastest and simplest test suite in the whole project. Required cases: two fixture signal DataFrames whose buy sets overlap by 3+ tickers, asserting the result is exactly that intersection and `branch == "intersection"`; two fixture signal DataFrames whose buy sets overlap by exactly 1 ticker, asserting the result is the full union and `branch == "union"`; two fixture signal DataFrames with zero overlap, asserting the result is still the full union; a single fixture DataFrame passed as `llm_s_signals` with `llm_f_signals=None`, asserting the result is exactly that DataFrame's buy set and `branch == "llm_s_only"`; and a case with both arguments `None`, asserting `ValueError` is raised.


Acceptance for this plan is: `uv run pytest tests/test_candidate_scanner.py` passes with all five cases above; the Concrete Steps Step 1 transcript (captured for real) shows a plausible narrowed candidate list from real 2024 data.


## Idempotence and Recovery


`scan` and `scan_with_detail` are pure functions with no side effects and no state — calling them any number of times with the same inputs returns the same output (modulo whatever nondeterminism already existed upstream in the LLM-generated signals they were passed). There is nothing to recover from in this plan; it writes nothing to disk.


## Artifacts and Notes


(To be filled in with the real candidate-scan transcript from Concrete Steps, and the fallback-frequency measurement from Surprises & Discoveries, once this plan is executed across multiple real rebalance dates.)


## Interfaces and Dependencies


This plan depends only on the output shapes of `plans/02_llm_s_agent.md`'s `screen` function and `plans/03_llm_f_agent.md`'s `screen_month` function (both `pd.DataFrame` with columns `ticker: str`, `signal: str`) — it has no dependency on any external library beyond `pandas`, already present.


At the end of this plan, the following must exist and be usable by later plans exactly as described:


In `src/scanner/candidate_scanner.py`, `def scan(llm_s_signals: pd.DataFrame | None, llm_f_signals: pd.DataFrame | None) -> list[str]`, and `def scan_with_detail(llm_s_signals: pd.DataFrame | None, llm_f_signals: pd.DataFrame | None) -> dict` with at least the keys `candidates: list[str]` and `branch: str`. Plan 5 (`plans/05_optimizer_and_allocation.md`) calls `scan` (or accepts an equivalent user-supplied ticker list, per that plan's own requirement to allow overriding the candidate set) to get the ticker universe it optimizes weights over. Plan 6 (`plans/06_interactive_flow.md`) calls `scan_with_detail` so it can show the user which branch was taken and let them add or remove tickers from the `candidates` list before it is passed on to plan 5.
