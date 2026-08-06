# Build the LLM-F sentiment screening agent (the FinBERT replacement)


This ExecPlan is a living document. The sections `Progress`, `Surprises & Discoveries`, `Decision Log`, and `Outcomes & Retrospective` must be kept up to date as work proceeds. This plan must be maintained in accordance with `PLANS.md` at the repository root. This plan builds on `plans/01_dataset.md` (checked into this repository, for the ticker/date universe and price data) and is a sibling to `plans/02_llm_s_agent.md` (checked into this repository) — both produce a per-ticker, per-rebalance-date buy/sell/hold signal that plan 4 (`plans/04_candidate_scanner.md`) combines.


## Purpose / Big Picture


After this plan is done, a person can run one command for any month between 2020 and 2024 and, for a chosen stock, see a buy, sell, or hold signal derived from an LLM's reading of that month's actual news headlines about that company — plus a short written rationale explaining the call. Run across every stock in that month's S&P 500 membership, this produces the same kind of monthly sentiment-screening layer that the paper this project reproduces ("Designing Agentic AI-Based Screening for Portfolio Investment", Caner, Capponi, Sun, and Tan, arXiv:2603.23300) builds using FinBERT, a specialized sentiment-classification model trained on financial text.


README.md names this project's version of that agent "LLM-F" and explicitly frames it as replacing FinBERT's role with a general-purpose LLM making a judgment call, rather than a specialized sentiment classifier producing a bare probability score. This is a deliberate scope choice already recorded in README.md, not a limitation being introduced here — this repository has no FinBERT model installed (there is no `transformers` or `torch` dependency in `pyproject.toml`), and does not need one, because LLM-F reads headlines and reasons about them directly, the same way LLM-S reads factor statistics and reasons about them directly in `plans/02_llm_s_agent.md`.


## Progress


- [x] Define the Pydantic schema for LLM-F's output (signal, confidence, rationale) per ticker per month.
- [x] Write the news-fetching function using `yfinance`'s per-ticker `.news` accessor.
- [x] Write the CrewAI agent and task that turns a batch of headlines into that schema.
- [x] Wire in the configurable-model environment variable (`LLM_F_MODEL`).
- [x] Measure and record actual `yfinance` news coverage across the 2020-2024 backtest window (this is the single biggest open risk in this plan and must be measured, not assumed).
- [ ] Write `tests/test_llm_f.py` covering the news-to-signal plumbing against fixtures (not real LLM calls).
- [x] Manually run the agent for at least one real ticker and month and sanity-check the output.


## Surprises & Discoveries


**`yfinance.Ticker(ticker).news` has zero historical reach — confirmed, not just suspected.** Verified live on 2026-08-06 for `AAPL`, `MSFT`, `XOM`, `F`: each returned exactly 10 items, and every single item across all 4 tickers was dated `2026-08-05` (i.e. the day before the call, the most recent trading day) — none older. The response shape itself has also changed since this plan was written: the publish date lives at `item['content']['pubDate']` (ISO 8601 with a `Z` suffix), not a top-level `publish_date`/`providerPublishTime` field. Concretely, this means `fetch_headlines` as originally scoped (querying live `yfinance.news` for a historical month like 2021-03) will always return an empty list for every backtest month — this is not a partial-coverage gap, it is total. The fidelity risk flagged in the Decision Log below is confirmed in its strongest form.

**The Hugging Face archive (`KrossKinetic/SP500-Financial-News-Articles-Time-Series`) does have real, dated 2020-2024 coverage, but it is sparse and uneven.** Downloaded and ingested into `data/portfolio.duckdb::news_articles_hf` via the new `src/dataset/news_archive.py` (see Decision Log entry below for why this was added to this plan's scope). Measured facts:

- 4,589 total rows, 469 distinct tickers, real `publish_date` values spanning 2006-12-04 through 2024-04-20 (confirmed via `DESCRIBE`/`min`/`max` on the actual downloaded Parquet file, not the dataset card's advertised schema). No null `publish_date` or `symbol` values.
- 2,621 of the 4,589 rows (57%) fall inside this project's actual rebalance window (`settings.rebalance_start`/`rebalance_end` = 2020-01-01 through 2024-04-30) — the archive is weighted toward exactly the years this project needs, consistent with it being the same dataset the reference paper used (its end date, April 2024, matches `settings.fetch_end`/`rebalance_end` almost exactly).
- Joined against the real per-month S&P 500 universe (`sp500_membership`, 52 rebalance months, 26,268 total (ticker, month) pairs 2020-01 through 2024-04): every one of the 52 months has at least some archive coverage (no calendar gaps), and 405 of the 580 tickers that were ever S&P 500 members in that window (70%) have at least one article somewhere in it.
- But per-(ticker, month) coverage is thin: only 1,708 of 26,268 pairs (6.5%) have ≥1 archive article in that exact calendar month. A given ticker in a given month usually still has zero headlines from this archive — consistent with most tickers capping out at ~10 articles across the entire 18-year source dataset.
- Monthly volume within the window is heavily skewed toward its later end, not even: 16 articles in 2020-01 versus 171 in 2024-04 (roughly 10x growth), so the 2020 portion of the backtest is thinner than the 2023-2024 portion even within the archive's own covered range.

**Read the paper's Section 3.1 and 5.2.1 (arXiv:2603.23300) to confirm FinBERT's exact functional role before designing `generate_signal`'s CrewAI task.** Extracted the PDF's text directly (`pypdf`, via an ephemeral `uv run --with pypdf`, since `pdftoppm`/poppler-utils is not installed in this environment and the Read tool's PDF page-rendering needs it). Findings: FinBERT is rerun monthly (LLM-S is rerun annually) and, for each stock-month, analyzes that month's news articles and computes a sentiment score = (positive FinBERT probability - negative FinBERT probability), exponentially time-decayed toward month-end (half-life 7 days) to downweight stale news; score > 0.1 -> buy, < -0.1 -> sell, else hold. Footnote 5 confirms the paper's news source is the exact same Hugging Face dataset ingested above (`https://huggingface.co/datasets/KrossKinetic/SP500-Financial-News-Articles-Time-Series`), confirming last session's dataset choice was correct. FinBERT's buy/sell output is combined with LLM-S's via an intersection-based consensus rule (Section 5.2's "Sensible Screening": both agents must agree, defaulting to the union when the intersection has <=1 stock) — this happens at the candidate-scanner stage (`plans/04_candidate_scanner.md`), not inside LLM-F itself, so `generate_signal` only needs to independently produce one agent's buy/sell/hold opinion. This confirms the existing Plan of Work design (a holistic LLM judgment call producing buy/sell/hold/confidence/rationale) is the right functional replacement for FinBERT's mechanical score-and-threshold rule — no design change was needed, only added confidence that the replacement is scoped correctly.

**Refactored `generate_signal` to separate prompts from code, matching `LLMSCrew`.** The initial implementation built the CrewAI `Agent`/`Task` inline in Python with hardcoded role/goal/backstory/description strings. Per user request, moved these into `src/agents/llm_f_crew/config/agents.yaml`/`config/tasks.yaml` plus a new `src/agents/llm_f_crew/crew.py`'s `LLMFCrew` (`@CrewBase`), exactly mirroring `LLMSCrew`'s structure. The one structural difference: `LLMFCrew` needs no custom tools (no dataframe to explore — headlines are handed to it as text), so its constructor takes only `model`, and `ticker`/`month`/`headlines` are passed as `kickoff(inputs=...)` values interpolated into the YAML's `{ticker}`/`{month}`/`{headlines}` placeholders, rather than constructor state the way `LLMSCrew` takes `snapshot`/`as_of_date`. Re-ran the same manual AAPL/2024-03 check after the refactor: identical quality of headline-grounded output (still a `sell` at 0.68 confidence, correctly citing the same specific headlines), confirming the refactor is behavior-preserving.

**Operational discovery while running `generate_signal` manually: this environment's default model string doesn't resolve.** `anthropic/claude-sonnet-4-5` (this plan's originally-specified default) returns `anthropic.NotFoundError: DeploymentNotFound` against this environment's `ANTHROPIC_BASE_URL` (an Azure AI Foundry Anthropic-compatible proxy — same setup `plans/02_llm_s_agent.md` already documented). `plans/02_llm_s_agent.md`'s own Surprises & Discoveries already recorded that `anthropic/claude-opus-4-8` works against this specific deployment; confirmed the same is true here via `LLM_F_MODEL=anthropic/claude-opus-4-8`. This is an environment-specific deployment-naming quirk, not a bug in `generate_signal` or its default — the default string itself (`anthropic/claude-sonnet-4-5`) is left unchanged in code, since a different deployment/environment may well have it available; `LLM_F_MODEL` is exactly the escape hatch for this.


## Decision Log


- Decision: use `yfinance`'s per-ticker `.news` accessor as the news source for LLM-F, rather than a paid or exa-py-driven web search.
  Rationale: the user explicitly chose `yfinance` news over `exa-py` when asked, on the basis that it is free and already used elsewhere in this project for prices.
  Date/Author: 2026-08-05, decided by repository owner during planning interview.
- Decision: explicitly flag, rather than silently accept, that `yfinance`'s news accessor is known to skew toward recent articles and may have thin or absent coverage for older months in the 2020-2024 backtest window. This plan's job is to measure the actual severity of that gap (see Progress checklist) and record it in Surprises & Discoveries with evidence, not to assume a specific severity in advance.
  Rationale: the user was told this news-source tradeoff explicitly during the planning interview and accepted it; this repository must not pretend the resulting historical sentiment signal is as reliable as a purpose-built historical news archive would be.
  Date/Author: 2026-08-05, plan author, reflecting the user's explicit choice during interview.
- Decision: default LLM-F to Anthropic Claude via the same `LLM_F_MODEL` environment-variable pattern as LLM-S's `LLM_S_MODEL` in `plans/02_llm_s_agent.md`, independently configurable so a user could, for example, run LLM-S on Claude and LLM-F on a different provider.
  Rationale: matches the user's stated default-plus-configurable preference from the interview, applied consistently to both agents.
  Date/Author: 2026-08-05, plan author.
- Decision: add `src/dataset/news_archive.py` (downloads and ingests the Hugging Face dataset `KrossKinetic/SP500-Financial-News-Articles-Time-Series` into a new `news_articles_hf` table in `data/portfolio.duckdb`, no new dependency needed — `requests` downloads the Parquet file locally, then DuckDB's built-in Parquet reader loads it, avoiding any reliance on DuckDB's `httpfs` extension auto-install) and use it as the **primary** source in `fetch_headlines` for any (ticker, month) inside the archive's covered range (2020-01 through 2024-04), falling back to live `yfinance.news` only for months outside that range (i.e. a genuinely live/current-month run). This supersedes, without deleting, the yfinance-only decision above.
  Rationale: measured directly (see Surprises & Discoveries) rather than assumed, per this plan's own stated requirement. `yfinance.news` has zero historical reach at all — verified live, every returned item across 4 sample tickers was dated the day of the call, none older — so it cannot answer any 2020-2024 backtest month by itself. The HF archive does have real dated coverage across the full backtest window (every one of the 52 rebalance months has some coverage), even though per-ticker-month coverage is sparse (6.5% of pairs). A sparse-but-real historical signal for some ticker-months is strictly better than a live source that can supply zero signal for any of them; the existing "empty headlines -> hold, confidence 0.0" path already handles the (majority) case where neither source has anything, so this hybrid adds coverage without fabricating signal where none exists.
  Date/Author: 2026-08-06, measured and decided this session, per user's request to evaluate the specific dataset named in the reference paper (arXiv:2603.23300, page 19).


## Outcomes & Retrospective


(To be filled in once this plan is implemented and validated, including the actual measured news-coverage numbers from the Progress checklist's coverage-measurement step.)


## Context and Orientation


This plan adds `src/agents/llm_f.py` and related modules, a sibling to `src/agents/llm_s.py` from `plans/02_llm_s_agent.md`, in this Python 3.12, `uv`-managed repository. All commands below run from the repository root and assume `plans/01_dataset.md` has already been implemented, so `data/portfolio.duckdb`'s `sp500_membership` table exists (this plan needs it to know which tickers to screen for a given month; it does not need the `factors` table, since sentiment screening does not use the fundamental factors at all).


This plan uses CrewAI exactly as described in `plans/02_llm_s_agent.md`'s Context and Orientation section — refer there for what an "Agent," a "Task," and CrewAI's structured-output kickoff mean, and for the mandatory step of checking the installed CrewAI version against current docs before writing code, both of which apply identically here.


"Headline window" means the specific date range of news this plan pulls for a given ticker and a given rebalance month: this plan defines it as all headlines `yfinance` returns for that ticker dated within that calendar month (the same month LLM-F is being rerun for, matching the paper's monthly cadence for its sentiment agent), with a hard cap (this plan sets 20) on how many headlines are passed to the LLM per ticker per month, to keep the prompt bounded regardless of how many articles a heavily-covered company like Apple accumulates versus a rarely-covered smaller company.


## Plan of Work


Create `src/agents/llm_f_schema.py` defining a Pydantic model `SentimentSignal` with fields: `ticker: str`, `month: str` (an ISO `"YYYY-MM"` string), `signal: str` (one of `"buy"`, `"sell"`, `"hold"`, enforced with a Pydantic `Literal` type rather than a bare `str`), `confidence: float` (0 to 1), and `rationale: str`.


Create `src/agents/news.py` with a function `fetch_headlines(ticker: str, year: int, month: int, limit: int = 20) -> list[dict]` that, per the Decision Log's hybrid-source decision (measured this plan, see Surprises & Discoveries): first queries `data/portfolio.duckdb::news_articles_hf` (populated by `src/dataset/news_archive.py`) for rows matching `ticker` and that calendar month; if the requested `(year, month)` falls outside the archive's covered range (2020-01 through 2024-04 as measured — check the table's actual `min(publish_date)`/`max(publish_date)` rather than hardcoding these bounds, in case the archive is re-ingested with different coverage later), fall back to calling `yfinance.Ticker(ticker).news` and filtering to that month (verified live: the real field is nested at `item["content"]["pubDate"]`, ISO 8601 with a `Z` suffix, not a top-level `publish_date` — and, per Surprises & Discoveries, this live call only ever returns items dated the day of the call, so in practice this fallback path only ever produces anything for the actual current month, not historical ones). Returns at most `limit` items, each as a dict with at minimum a `title` and a `publish_date`. If neither source returns anything for a ticker/month, that is a valid, expected outcome (confirmed common: only 6.5% of (ticker, month) pairs in the real S&P 500 universe have any archive coverage at all) and must be represented as an empty list, not an exception.


Create `src/agents/llm_f_crew/config/agents.yaml` and `config/tasks.yaml`, and `src/agents/llm_f_crew/crew.py`'s `LLMFCrew` (CrewAI's `@CrewBase` pattern), mirroring `plans/02_llm_s_agent.md`'s `LLMSCrew` — the agent's role/goal/backstory and the task's description/expected_output live in YAML, not Python, matching that plan's separation of prompts from code. `sentiment_agent`'s role is "Financial News Sentiment Analyst"; `sentiment_task`'s description instructs it to read the provided headlines for `{ticker}`/`{month}` and decide whether the aggregate tone suggests buying, selling, or holding the stock, with an explicit confidence level and rationale — this is deliberately a holistic judgment call by the LLM, not a decomposition into positive/negative probability scores the way FinBERT itself would produce them, matching README's framing of LLM-F as replacing FinBERT's role rather than reimplementing FinBERT's specific mechanism. Unlike `LLMSCrew`, `LLMFCrew` needs no custom tools (LLM-F reasons over headline text handed to it directly, not a dataframe it must explore), so its constructor takes only `model: str`, and `ticker`/`month`/`headlines` are interpolated into the YAML's `{ticker}`/`{month}`/`{headlines}` placeholders via `.crew().kickoff(inputs=...)` rather than being constructor state.

Create `src/agents/llm_f.py` with a function `generate_signal(ticker: str, year: int, month: int, headlines: list[dict], model: str | None = None) -> SentimentSignal`. If `headlines` is empty, skip the LLM call entirely and return a `SentimentSignal` with `signal="hold"`, `confidence=0.0`, and a `rationale` stating plainly that no news was found for that ticker and month — calling an LLM with nothing to reason about would either waste a call or invite the model to fabricate sentiment from nothing, neither of which is acceptable. Otherwise, resolve the LLM string as `os.environ.get("LLM_F_MODEL", "anthropic/claude-sonnet-4-5")` (or the current recommended Claude model string at implementation time), independently of `LLM_S_MODEL`, build `LLMFCrew(model=resolved_model)`, and call `.crew().kickoff(inputs={"ticker": ticker, "month": month_str, "headlines": <formatted headline text>})`, returning `result.pydantic` (with `ticker`/`month` overwritten from the request afterward, in case the LLM filled them in slightly differently). `output_pydantic=SentimentSignal` is set on the Task in `crew.py`, per CrewAI's current API — matching `LLMSCrew`'s `ScreeningRule` usage, confirmed already working with the installed CrewAI version (1.15.12).


Create `src/agents/llm_f_signals.py` with a function `screen_month(year: int, month: int, db_path: str = "data/portfolio.duckdb") -> pd.DataFrame` that reads that month's membership from `sp500_membership` (using the rebalance date that falls in that year/month), calls `fetch_headlines` and then `generate_signal` for every member ticker, and returns a DataFrame with columns `ticker`, `signal`. This function necessarily makes one `yfinance` news call and, for tickers with any headlines, one LLM call per ticker per month — for a ~500-ticker universe this is a meaningfully expensive and slow operation (hundreds of LLM calls), so `screen_month` should log progress (for example, one line per 50 tickers processed) rather than running silently for what could be several minutes.


## Concrete Steps


Run every command from the repository root, with `plans/01_dataset.md` already implemented and `data/portfolio.duckdb` populated.


Step 1 — measure real `yfinance` news coverage before building anything else, since this determines how much the rest of this plan can be trusted for historical months:

    uv run python -c "
    import yfinance as yf
    from collections import Counter
    for ticker in ['AAPL', 'MSFT', 'XOM', 'F']:
        news = yf.Ticker(ticker).news
        print(ticker, 'total items:', len(news))
        # print whatever date field is actually present on the first item, to discover the real schema
        print(news[0] if news else 'NO NEWS')
    "

Record the exact output in Surprises & Discoveries, including the real field names found (this plan's Plan of Work deliberately does not hardcode a specific field name for this reason) and, critically, whether any headline dates found are older than a few months in the past — if `yfinance.news` only ever returns very recent articles regardless of when you call it, then a script cannot retroactively fetch "March 2021's headlines" in 2026, and this plan's historical backtest mode for LLM-F may need to be redesigned (for example, downgraded to "hold with zero confidence for all months before some cutoff" with that limitation stated plainly) rather than implemented as originally envisioned. This is exactly the kind of discovery PLANS.md's prototyping-milestone guidance exists for: verify feasibility before building the full pipeline on top of an assumption.


Step 2 — once Step 1's findings are recorded, implement the files in Plan of Work (adjusting the historical-mode design if Step 1 found the limitation described above), then generate one real signal:

    export ANTHROPIC_API_KEY=...
    uv run python -c "
    from src.agents.news import fetch_headlines
    from src.agents.llm_f import generate_signal
    headlines = fetch_headlines('AAPL', 2024, 3)
    print(len(headlines), 'headlines found')
    signal = generate_signal('AAPL', 2024, 3, headlines)
    print(signal)
    "

Expected: a printed `SentimentSignal` whose `rationale` visibly references content from the actual fetched headlines (not generic boilerplate), giving a concrete, checkable sign that the LLM call is really conditioning on the news text passed to it.


Step 3 — run the full month screen for one real month and inspect signal counts, being mindful this may take several minutes and make several hundred LLM calls (consider limiting to a subset of tickers for this first manual check, via a temporary slice of the membership list, rather than the full ~500-name universe, to keep the first sanity check fast and cheap):

    uv run python -c "
    from src.agents.llm_f_signals import screen_month
    signals = screen_month(2024, 3)
    print(signals['signal'].value_counts())
    "


## Validation and Acceptance


Run `uv run pytest tests/test_llm_f.py` and expect all tests to pass. Per `AGENTS.md`'s testing guidance, these tests must not call `yfinance` or any LLM. Required cases: a test that `generate_signal` returns `signal="hold"` and `confidence=0.0` without needing any LLM-related mocking when passed an empty `headlines` list (this is the one code path in this plan that is fully deterministic and testable without touching an LLM); a test that `fetch_headlines`, given a fixture `yfinance`-shaped response object (constructed by hand in the test, not a real API call) with headlines both inside and outside the target month, correctly filters to only the in-month ones and respects the `limit` cap.


Acceptance for this plan is: `uv run pytest tests/test_llm_f.py` passes; the Concrete Steps Step 1 transcript is captured in Surprises & Discoveries with the real coverage findings; Step 2 and Step 3 transcripts show a coherent, headline-grounded signal and a plausible signal-count split.


## Idempotence and Recovery


`fetch_headlines` and `generate_signal` are each safe to call repeatedly; `generate_signal`'s LLM call is not guaranteed deterministic across repeated calls with the same headlines, which is expected. `screen_month` makes no persistent writes to `data/portfolio.duckdb` by itself in this plan's scope (it returns a DataFrame) — plan 6's interactive flow, if it chooses to cache monthly signals for backtest stability, is responsible for adding that persistence and its own idempotence handling at that point.


## Artifacts and Notes


Concrete Steps Step 1's measurement transcript (yfinance side) — ran 2026-08-06:

    uv run python -c "
    import yfinance as yf
    for ticker in ['AAPL', 'MSFT', 'XOM', 'F']:
        news = yf.Ticker(ticker).news
        print(ticker, 'total items:', len(news))
    "
    # AAPL total items: 10, MSFT total items: 10, XOM total items: 10, F total items: 10
    # every item across all 4 tickers: content.pubDate == '2026-08-05T...' (the call date) - no exceptions.

HF archive coverage measurement (this session's addition to Step 1, same underlying risk) — ran 2026-08-06 against the ingested `news_articles_hf` table joined to `sp500_membership`:

    archive rows in [2020-01-01, 2024-04-30]: 2621 (of 4589 total, 57%)
    (ticker, month) pairs in sp500_membership: 26268
    (ticker, month) pairs with >=1 archive article that month: 1708 (6.5%)
    distinct tickers in universe: 580; distinct tickers with >=1 article in window: 405 (70%)
    monthly article counts: 16 in 2020-01 ramping to 171 in 2024-04 (see Surprises & Discoveries for the full table)

Concrete Steps Step 2's transcript — ran 2026-08-06 with `LLM_F_MODEL=anthropic/claude-opus-4-8` (see Surprises & Discoveries for why that override was needed in this environment):

    fetch_headlines('AAPL', 2024, 3) -> 6 headlines (from news_articles_hf, the archive covers 2024-03):
      2024-03-25 EU launches probe into Meta, Apple and Alphabet under sweeping new tech law
      2024-03-26 Apple announces its big annual conference, where it could reveal its AI strategy
      2024-03-26 Apple could double down on China market, Wedbush says, as iPhone sales drop
      2024-03-27 Why the government wants to rearrange your Apple Wallet
      2024-03-27 Op-ed: Why an all-smiles China visit from Apple's Tim Cook isn't good business
      2024-03-28 Apple's bad quarter and what history says will happen next

    generate_signal('AAPL', 2024, 3, headlines) ->
      SentimentSignal(ticker='AAPL', month='2024-03', signal='sell', confidence=0.68,
        rationale="The March 2024 headlines skew clearly negative for AAPL. Regulatory pressure is
        mounting on two fronts: the EU 'launches probe into Meta, Apple and Alphabet under sweeping
        new tech law,' and the U.S. government reportedly 'wants to rearrange your Apple Wallet' ...
        Fundamentals also look soft: 'Apple could double down on China market...as iPhone sales
        drop' ... and 'Apple's bad quarter and what history says will happen next' explicitly frames
        a poor quarter. ... On balance the aggregate tone is bearish, supporting a sell, though
        confidence is moderated by the pending AI catalyst which could shift sentiment.")

The rationale visibly and specifically references the actual fetched headline content (the EU probe, iPhone sales drop, Tim Cook's China visit, the AI-conference counterpoint) rather than generic boilerplate — the acceptance bar this step's Expected Output calls for. Also verified separately: `generate_signal(ticker, year, month, [])` returns `signal='hold', confidence=0.0` with no LLM call at all, for an out-of-archive-range, no-live-news month (`AAPL`, 2020-01).

Step 3's transcript (`screen_month` full-month run) remains pending — that requires implementing `src/agents/llm_f_signals.py` (required by Interfaces and Dependencies below, though not separately itemized in the Progress checklist above) plus Progress item 6's test coverage, both out of scope for this pass.


## Interfaces and Dependencies


This plan depends on `plans/01_dataset.md`'s `sp500_membership` table in `data/portfolio.duckdb` for the ticker universe per month. This plan depends on `yfinance` (already in `pyproject.toml`) for live/current-month news, and `crewai` for the agent machinery, and on whichever LLM provider environment variable is set (`ANTHROPIC_API_KEY` by default, matching `LLM_F_MODEL`'s default). This plan also depends on `src/dataset/news_archive.py`'s `news_articles_hf` table in `data/portfolio.duckdb` (built by running that module; downloads `data/news_archive_source.parquet` from the Hugging Face dataset `KrossKinetic/SP500-Financial-News-Articles-Time-Series` on first run, cached thereafter) as the primary historical-news source for `fetch_headlines`, per the hybrid-source Decision Log entry above.


At the end of this plan, the following must exist and be usable by later plans exactly as described:


In `src/agents/llm_f_schema.py`, the Pydantic model `SentimentSignal` with fields `ticker: str`, `month: str`, `signal: Literal["buy", "sell", "hold"]`, `confidence: float`, `rationale: str`.


In `src/agents/llm_f_signals.py`, `def screen_month(year: int, month: int, db_path: str = "data/portfolio.duckdb") -> pd.DataFrame`, returning a DataFrame with columns `ticker: str`, `signal: str`. Plan 4 (`plans/04_candidate_scanner.md`) calls this to get LLM-F's buy set for a given month, in the same shape as `plans/02_llm_s_agent.md`'s `screen` function's output, so the candidate scanner can treat both agents' outputs uniformly.
