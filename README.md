# Agentic Portfolio

This project aims to follow the idea proposed by "Designing Agentic AI-Based Screening for Portfolio Investment" (https://arxiv.org/abs/2603.23300v1). It provides an approach to evaluate portfolio selection automation. To use this approach, this project will need to:

- Build the dataset that includes 3 factors: log firm size (mve), book-to-market ratio (bm), and 12-month momentum
(mom12m) for all S&P 500 member companies during 2020-01-01 and 2024-04-30.
- Implement LLM-S agent.
- Implement LLM-F agent which replace the role of FinBERT.
- Implement the candidate scanner that uses the rules proposed by LLM-S and/or LLM-F agents.
- Build the optimization tool that provides GMV, MV, MSR weights for given and user input candidates.
- Implement the consolidation tool that converts weights into allocation in shares according to the market prices.
- Implement an interactive agent flow that coordinates the all agents of this system and modifies candidate set per user's request.

# Configuration

Runtime settings (DuckDB path, fetch/rebalance date windows, batch sizes, rate-limit pause seconds, HTTP timeouts, the book-equity reporting lag, and the LLM model name) are centralized in `src/config/settings.py`, a `pydantic-settings` `Settings` class with sensible defaults — see that file for the authoritative list of every configurable value and its default. Any field can be overridden via an environment variable of the same name (case-insensitive) or via a `.env` file at the repository root (gitignored, never commit one with real secrets).

Environment variables this project depends on:

- `SEC_UA` (required for the dataset build's SEC EDGAR fetches) — a descriptive User-Agent string (e.g. `"Your Name your.email@example.com"`); SEC returns HTTP 403 without one.
- `ANTHROPIC_API_KEY` / `ANTHROPIC_BASE_URL` (required for LLM-S/LLM-F's CrewAI Anthropic calls) — read directly by the `anthropic` SDK underneath `crewai`, not by this project's own code.
- `LLM_S_MODEL` (optional) — overrides the default Claude model LLM-S uses (`anthropic/claude-sonnet-4-5`).
- `DB_PATH` (optional) — overrides the default DuckDB file location (`data/portfolio.duckdb`).

# Acknowledgements and Citation

- "Designing Agentic AI-Based Screening for Portfolio Investment", https://arxiv.org/abs/2603.23300v1.
- Martin, R. A., (2021). PyPortfolioOpt: portfolio optimization in Python. Journal of Open Source Software, 6(61), 3066, https://doi.org/10.21105/joss.03066
- https://docs.crewai.com/