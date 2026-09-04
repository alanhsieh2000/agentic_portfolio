"""Full 2020-2024 backtest runner for `plans/06_interactive_flow.md`:
chains the same LLM-S/LLM-F/scanner/optimizer sequence `run_pipeline` uses
across every one of the stored window's 52 monthly rebalance dates,
scores each month's chosen weights against the *next* rebalance date's
realized `monthly_return` (net of a PyPortfolioOpt-estimated turnover
cost), and reports the resulting month-by-month net returns - this is
what produces a number to compare against the paper's own reported
0.6324 S&P 500 baseline Sharpe ratio (arXiv:2603.23300) over the same
period, per README's Backtest Mode Stage 2.
"""

from __future__ import annotations

import logging
from datetime import date

import duckdb
import numpy as np
import pandas as pd
from pypfopt import objective_functions

from src.agents.llm_s import generate_rule
from src.agents.llm_s_schema import ScreeningRule
from src.config.settings import settings
from src.dataset.membership import compute_rebalance_dates
from src.flow.interactive import run_scan
from src.optimizer.portfolio import compute_weights, load_returns_matrix

logger = logging.getLogger(__name__)


def compute_sharpe_ratio(net_returns: pd.Series, risk_free_rate: float = settings.risk_free_rate) -> float:
    """Annualized Sharpe ratio of a Series of net monthly returns, per
    README's Backtest Mode Stage 2: `(mean - monthly risk-free rate) /
    std`, annualized by `sqrt(12)` - the annual `risk_free_rate` is
    converted to a monthly rate (divided by 12) to match `net_returns`'
    monthly cadence before subtracting it from the mean.
    """
    monthly_risk_free_rate = risk_free_rate / 12
    return float((net_returns.mean() - monthly_risk_free_rate) / net_returns.std() * (12**0.5))


def _turnover_cost(w_t: dict[str, float], w_prev: dict[str, float]) -> float:
    """`pypfopt.objective_functions.transaction_cost(w_t, w_prev, k=...)`,
    called directly as a plain numeric helper, with both weight vectors
    first aligned onto the union of their tickers (missing entries as
    zero weight) since two consecutive months' candidate sets can differ.
    """
    tickers = sorted(set(w_t) | set(w_prev))
    w_t_arr = np.array([w_t.get(t, 0.0) for t in tickers])
    w_prev_arr = np.array([w_prev.get(t, 0.0) for t in tickers])
    return float(objective_functions.transaction_cost(w_t_arr, w_prev_arr, k=settings.transaction_cost_bps / 10000))


def _gross_return(weights: dict[str, float], next_month_returns: pd.Series, rebalance_date: date) -> float | None:
    """Weighted average of `weights` against `next_month_returns`
    (indexed by ticker: the *following* rebalance date's realized
    `monthly_return`), renormalized over only the tickers with a known
    value there.

    A candidate with no value there (delisted mid-month, or otherwise
    missing) is dropped from both the numerator AND the weight-sum
    denominator, logged by name - dropping only the numerator term would
    be numerically identical to treating it as a zero return (the two
    read the same in a plain weighted sum), which the Plan of Work
    explicitly says not to do; renormalizing over the remaining known
    weights is what actually differs. Returns `None` if every candidate
    that month lacked a forward return - the whole month is then
    unscoreable, not a `0.0` result worth reporting.
    """
    known = {t: w for t, w in weights.items() if t in next_month_returns.index and pd.notna(next_month_returns[t])}
    dropped = sorted(set(weights) - set(known))
    if dropped:
        logger.info(
            "run_full_backtest(%s): dropping %s from the gross-return calculation "
            "(no monthly_return at the following rebalance date)",
            rebalance_date,
            dropped,
        )
    total_weight = sum(known.values())
    if total_weight == 0:
        logger.warning(
            "run_full_backtest(%s): every candidate lacked a forward monthly_return; "
            "this month is excluded from the results",
            rebalance_date,
        )
        return None
    return sum(w * next_month_returns[t] for t, w in known.items()) / total_weight


def _load_next_month_returns(rebalance_dates: list[date], db_path: str) -> dict[date, pd.Series]:
    """`{date: Series}` mapping each of `rebalance_dates` to that date's
    `monthly_return` per ticker, read from the shared `returns` table -
    the *forward-looking* read `plans/01_dataset.md`'s Interfaces and
    Dependencies section documents as this plan's own use of that table,
    distinct from plan 5's own backward-looking read for expected-return/
    covariance estimation.
    """
    if not rebalance_dates:
        return {}
    placeholders = ", ".join(["?"] * len(rebalance_dates))
    con = duckdb.connect(db_path, read_only=True)
    try:
        df = con.execute(
            f"SELECT rebalance_date, ticker, monthly_return FROM returns WHERE rebalance_date IN ({placeholders})",
            [d.isoformat() for d in rebalance_dates],
        ).fetchdf()
    finally:
        con.close()
    df["rebalance_date"] = pd.to_datetime(df["rebalance_date"]).dt.date
    return {d: group.set_index("ticker")["monthly_return"] for d, group in df.groupby("rebalance_date")}


def run_full_backtest(
    objective: str, selection: str = "llm_s_only", db_path: str = "data/portfolio.duckdb"
) -> pd.DataFrame:
    """Chain the scanner/optimizer sequence across every one of the stored
    2020-2024 window's 52 monthly rebalance dates and return a
    single-column DataFrame (`net_return`, indexed by `rebalance_date`)
    of each scoreable month's net, transaction-cost-adjusted realized
    return.

    The last of the 52 rebalance dates has no *following* rebalance date
    within the stored window to score its chosen weights against (the
    `returns` table's own last row is that same final date), so it - and
    any other month where every candidate happened to lack a forward
    return - is excluded from the result, leaving up to 51 scored months.

    LLM-S's `generate_rule` is called at most once per distinct calendar
    year across the whole run (cached here, not inside `run_scan`'s own
    optional `rule` passthrough or `generate_rule` itself, which stays
    intentionally non-deterministic per call, per `plans/02_llm_s_agent.md`)
    - see this plan's Decision Log for why calling it fresh for every one
    of a year's ~12 rebalance months would silently violate README's
    Backtest Mode Stage 1 "one rule per year" design (`S_2020`, `S_2021`,
    ...), not merely waste LLM calls.

    `run_scan`'s optimizer counterpart, `allocate_shares`, is deliberately
    never called here: discrete share counts for some arbitrary dollar
    amount are not needed to score a backtest, and tying a 52-month
    unattended run's success to `DiscreteAllocation` succeeding for every
    candidate, every month, would be needless fragility no other
    backtest-scoring step here has.
    """
    rebalance_dates = [ts.date() for ts in compute_rebalance_dates(settings.rebalance_start, settings.rebalance_end)]

    rule_cache: dict[int, ScreeningRule] = {}
    monthly_weights: dict[date, dict[str, float]] = {}
    for rebalance_date in rebalance_dates:
        rule = None
        if selection in ("llm_s_only", "llm_s_and_f"):
            if rebalance_date.year not in rule_cache:
                rule_cache[rebalance_date.year] = generate_rule(rebalance_date.year, db_path=db_path)
            rule = rule_cache[rebalance_date.year]

        scan = run_scan(rebalance_date, selection, db_path, rule=rule)
        candidates = scan["scan_detail"]["candidates"]

        returns_matrix = load_returns_matrix(candidates, as_of=rebalance_date, db_path=db_path)
        weights = compute_weights(returns_matrix, objective)
        monthly_weights[rebalance_date] = weights

        logger.info(
            "run_full_backtest(%s): %d candidates (%s), %d nonzero weights",
            rebalance_date,
            len(candidates),
            scan["scan_detail"]["branch"],
            sum(1 for w in weights.values() if w > 0),
        )

    next_month_returns = _load_next_month_returns(rebalance_dates[1:], db_path)

    rows = []
    prev_weights: dict[str, float] = {}
    for i, rebalance_date in enumerate(rebalance_dates):
        weights = monthly_weights[rebalance_date]
        turnover_cost = _turnover_cost(weights, prev_weights)
        prev_weights = weights

        if i + 1 >= len(rebalance_dates):
            continue  # the last rebalance date has no following date to score against
        next_date = rebalance_dates[i + 1]
        gross = _gross_return(weights, next_month_returns.get(next_date, pd.Series(dtype=float)), rebalance_date)
        if gross is None:
            continue
        rows.append({"rebalance_date": rebalance_date, "net_return": gross - turnover_cost})

    if not rows:
        return pd.DataFrame(columns=["net_return"]).astype({"net_return": float})
    return pd.DataFrame(rows).set_index("rebalance_date")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    result = run_full_backtest("MSR")
    sharpe = compute_sharpe_ratio(result["net_return"])
    print(f"Realized annualized Sharpe (MSR, net of transaction cost and risk-free rate, this project): {sharpe:.4f}")
    print("Paper-reported S&P 500 baseline Sharpe, 2020-2024: 0.6324")
