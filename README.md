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

After we use the Backtest Mode to find effective and efficient values for hyper-paraments and see a promising result, we will be ready to use the Live Mode.

- LLM-S generate scan rules using currently available fundamental data of S&P 500 members and stores them in memory/rules.json, if the file is missing or rules in the file are out of date. The system decides whether LLM-S is needed or not automatically and shows the progress. When the timestamp of the latest rules is in the previous year or even earlier, they are out of date.
- These LLM-S rules are applied to the current S&P 500 members to generate the candidate set $S$. $S$ should be stored in memory/candidates.json. If the file is missing or $S$ in the file is out of date, the system refreshes them automatically and shows the progress. When the timestamp of the latest $S$ is in the previous season or even earlier, the are out of date. Seasons are defined as 1/1 - 3/31, 4/1 - 6/30, 7/1 - 9/30, 10/1 - 12/31. The system should make a summary for each candidate in $S$ and store this information in memory/S-summary.md. The old memory/S-summary.md will be removed.
- LLM-F generate sentiment scores from exponentially-decreasing weighted sum of positive probability minus negative probability of the most recent 30 days for each candidate in $S$ and stores the candidate set $S \cap F$ in memory/candidates.json. If the file is missing or $S \cap F$ in the file is out of date, the system refreshes them automatically and shows the progress. When the timestamp of the latest $S \cap F$ is earlier than 30 days ago, the are out of date. The system should make a summary for each candidate in $S \cap F$ and store this information in memory/F-summary.md. The old memory/F-summary.md will be removed.
- Users can ask the system to analyze candidates, including ETF. The system should use the current LLM-S rules, look up the most recent 30-day news, use LLM-F to generate sentiment scores, and make a summary for candidates. This information should be stored in memory/analysis.md. New analyzed summaries will be appended to the end, and summaries with timestamp older than 30 days will be removed from the file.
- Users can ask the system to add candidates to the candidate set $U$. $U$ should be stored in memory/candidates.json. For added candidates, the system will move summaries associate with them from memory/analysis.md to memory/U-summary.md.
- Users can ask the system to remove candidates from any candidate set, including $S$, $S \cap F$, and $U$. The system should also update memory/S-, F-, U-summary.md accordingly.
- Users can ask the system to calculate weights for GMV, MSR, MV, using $S$, $S \cap F$, $U$, $S \cup U$, $(S \cap F) \cup U$. The system will report summaries and store them as output/GMV-, MSR-, MV-{set}-portfolio-{date}.md, where {set} is 'S', 'SF', 'U', 'SU', or 'SFU'.
- Users can ask the system to remember risk free rate and/or target return rate. The system will store them in memory/long-term.md. These information will be used to replace default values in Live Mode.

# Acknowledgements and Citation

- "Designing Agentic AI-Based Screening for Portfolio Investment", https://arxiv.org/abs/2603.23300v1.
- Martin, R. A., (2021). PyPortfolioOpt: portfolio optimization in Python. Journal of Open Source Software, 6(61), 3066, https://doi.org/10.21105/joss.03066
- https://docs.crewai.com/