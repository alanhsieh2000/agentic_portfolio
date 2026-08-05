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

# Acknowledgements and Citation

- "Designing Agentic AI-Based Screening for Portfolio Investment", https://arxiv.org/abs/2603.23300v1.
- Martin, R. A., (2021). PyPortfolioOpt: portfolio optimization in Python. Journal of Open Source Software, 6(61), 3066, https://doi.org/10.21105/joss.03066
- https://docs.crewai.com/