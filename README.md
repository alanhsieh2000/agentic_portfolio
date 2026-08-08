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

# Backtest Mode

The backtest period is from 2020-01-01 to 2024-04-30. There are 2 stages:

- Stage 1
  - LLM-S generate scan rules on 2019-12-31, and these rules are then applied to S&P 500 members at that time to get the candidates set $S_{2020}$. Repeating the same process for the following years, that will give us $S_{year}$, where $year = 2020..2024$.
  - LLM-F generate sentiment scores from exponentially-decreasing weighted sum of positive probability minus negative probability of 2019 December news articles for each S&P 500 members on 2019-12-31, and these sentiment scores divide S&P 500 members into buy group for score > 0.1, sell group for score < -0.1, and hold group for others. Buy and sell groups together consist the candidates set $F_0$. Repeating the same process for the following months, that will give us $F_{t}$, where $t = 1..52$.
  - For LLM-S only selection, the portfolio set $P_{t} = S_{2020 + \lfloor (t-1) / 12 \rfloor}$, where $t = 1..52$.
  - For LLM-F only selection, the portfolio set $P_{t} = F_{t}$, where $t = 1..52$.
  - For LLM-S + LLM-F selection, the portfolio set $P_{t} = S_{2020 + \lfloor (t-1) / 12 \rfloor} \cap F_{t}$, where $t = 1..52$. If any intersection is empty, the respective portfolio set $P_{t} = S_{2020 + \lfloor (t-1) / 12 \rfloor} \cup F_{t}$.
- Stage 2
  - EfficientFrontier of PyPortfolioOpt is used to optimize weights of the portfolio set $P_{t}$. That will give us weights $\hat{w}_{t,j}$, where $j = 1..\lvert P_t \rvert$, and $t = 1..52$. The required parameters expected_returns and cov_matrix are calculated from 60 most recent monthly returns of each company in the portfolio set and then are annualized before given to EfficientFrontier. In case that 60 months data is unavailable, at least 24 months data should be used. Companies with less than 24 monthly returns should be removed from the portfolio set.
  - The annualized expected_returns and weights $\hat{w}_{t,j}$ can give us the gross return of the portfolio at time t. By using pypfopt.objective_functions.transaction_cost(), we may take the transaction cost into consideration just like what the paper did. We can assume $\hat{w}_{0,j} = 0$ for all $j$. That will give us the net returns $r_t$, where $t = 1..52$.
  - The sharpe ratio, mean, variance of $r_t$ will give us Sharpe Ratio, Returns and Variance as the Table 1 of the paper.
  - By running the optimization for GMV, MV (12% target return), MSR, we will have all results to compare with.
  - Both transaction cost and risk-free rate which we need for the Stage 2 calculation are configurable constants and have default values as 10 basis point and 2%.

# Live Mode

# Acknowledgements and Citation

- "Designing Agentic AI-Based Screening for Portfolio Investment", https://arxiv.org/abs/2603.23300v1.
- Martin, R. A., (2021). PyPortfolioOpt: portfolio optimization in Python. Journal of Open Source Software, 6(61), 3066, https://doi.org/10.21105/joss.03066
- https://docs.crewai.com/