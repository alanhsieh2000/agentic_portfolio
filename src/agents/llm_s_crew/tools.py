"""The 4 tools LLM-S's Task uses to explore its causally-masked snapshot
and test candidate rules (paper Appendix C.5, arXiv:2603.23300v1's
`tools=[...]` list on `strategy_task`), plus the snapshot loader that
scopes them to one as-of date.

Each tool is constructed with a pre-loaded snapshot DataFrame (see
`load_snapshot`) rather than a `db_path`/`as_of_date` pair — this is what
mechanically enforces causal masking (see plans/02_llm_s_agent.md's
Decision Log): the LLM chooses what to query, never which date.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Literal

import duckdb
import pandas as pd
from crewai.tools import BaseTool
from pydantic import BaseModel, Field

from src.agents.condition_eval import evaluate_condition

logger = logging.getLogger(__name__)

_CHARACTERISTIC_MEANING = {
    "mve": "log(market value of equity) - log firm size",
    "bm": "book-to-market ratio (value factor); high = undervalued, low = overvalued",
    "mom12m": "12-month momentum",
}


def load_snapshot(db_path: str, as_of_date: date) -> pd.DataFrame:
    """The `factors` table's `mve_z`/`bm_z`/`mom12m_z` columns for a single
    `rebalance_date`, renamed to the paper's bare `mve`/`bm`/`mom12m`.
    Rows with any null among the three are dropped, since a tool can't
    show the agent a characteristic that isn't there. Called once per
    `generate_rule` call and shared across all 4 tool instances, so
    exploring the data doesn't mean repeated DuckDB round-trips.
    """
    con = duckdb.connect(db_path, read_only=True)
    try:
        df = con.execute(
            "SELECT ticker, mve_z AS mve, bm_z AS bm, mom12m_z AS mom12m "
            "FROM factors WHERE rebalance_date = ?",
            [as_of_date],
        ).fetchdf()
    finally:
        con.close()
    before = len(df)
    df = df.dropna(subset=["mve", "bm", "mom12m"]).reset_index(drop=True)
    if before != len(df):
        logger.info(
            "load_snapshot(%s): dropped %d/%d rows with a null mve/bm/mom12m",
            as_of_date,
            before - len(df),
            before,
        )
    return df


class _NoArgs(BaseModel):
    """Empty args schema for tools that take no input."""


class GetDatabaseSchemaTool(BaseTool):
    name: str = "get_database_schema"
    description: str = (
        "Returns the schema of the firm-characteristics data available to you: which "
        "characteristics exist, what each one means, the single date's snapshot you are "
        "scoped to (the causal-masking cutoff), and how many firms have complete data as of "
        "that date. Call this first, before querying or testing anything, to know what's "
        "available."
    )
    args_schema: type[BaseModel] = _NoArgs
    snapshot: pd.DataFrame = Field(exclude=True)
    as_of_date: date

    def _run(self) -> str:
        lines = [
            f"As-of date (causal-masking cutoff): {self.as_of_date.isoformat()}",
            f"Firms with complete data as of this date: {len(self.snapshot)}",
            "Characteristics (all are cross-sectional z-scores: mean 0, standard deviation 1):",
        ]
        lines += [f"  - {name}: {meaning}" for name, meaning in _CHARACTERISTIC_MEANING.items()]
        return "\n".join(lines)


class QueryFirmDatabaseArgs(BaseModel):
    sort_by: Literal["mve", "bm", "mom12m"] | None = Field(
        default=None, description="Characteristic to sort the returned rows by."
    )
    ascending: bool = Field(default=True, description="Sort ascending if true, descending if false.")
    limit: int = Field(default=25, description="Maximum number of rows to return.")
    tickers: list[str] | None = Field(
        default=None,
        description="Specific ticker symbols to look up directly, ignoring sort_by/ascending/limit.",
    )


class QueryFirmDatabaseTool(BaseTool):
    name: str = "query_firm_database"
    description: str = (
        "Browse rows of the firm-characteristics snapshot. Optionally sort by one "
        "characteristic (mve, bm, or mom12m), ascending or descending, and cap how many rows "
        "come back (default 25). Or pass specific ticker symbols to look them up directly. "
        "Use this to inspect the real numbers behind any pattern you suspect."
    )
    args_schema: type[BaseModel] = QueryFirmDatabaseArgs
    snapshot: pd.DataFrame = Field(exclude=True)

    def _run(
        self,
        sort_by: Literal["mve", "bm", "mom12m"] | None = None,
        ascending: bool = True,
        limit: int = 25,
        tickers: list[str] | None = None,
    ) -> list[dict]:
        if tickers:
            return self.snapshot[self.snapshot["ticker"].isin(tickers)].to_dict("records")
        df = self.snapshot
        if sort_by:
            df = df.sort_values(sort_by, ascending=ascending)
        return df.head(limit).to_dict("records")


class GetExtremeFirmsArgs(BaseModel):
    characteristic: Literal["mve", "bm", "mom12m"] = Field(description="Which characteristic to rank firms by.")
    n: int = Field(default=10, description="How many firms to return per direction.")
    direction: Literal["highest", "lowest", "both"] = Field(
        default="both", description="Which extreme(s) to return."
    )


class GetExtremeFirmsTool(BaseTool):
    name: str = "get_extreme_firms"
    description: str = (
        "Return the n firms with the highest and/or lowest values of one characteristic (mve, "
        "bm, or mom12m) as of the snapshot date, showing all three characteristics for each "
        "firm so you can spot correlations - e.g. whether the smallest firms also tend to have "
        "the strongest momentum. Use this to find natural breakpoints before setting "
        "thresholds."
    )
    args_schema: type[BaseModel] = GetExtremeFirmsArgs
    snapshot: pd.DataFrame = Field(exclude=True)

    def _run(
        self,
        characteristic: Literal["mve", "bm", "mom12m"],
        n: int = 10,
        direction: Literal["highest", "lowest", "both"] = "both",
    ) -> dict[str, list[dict]]:
        result: dict[str, list[dict]] = {}
        if direction in ("highest", "both"):
            result["highest"] = self.snapshot.sort_values(characteristic, ascending=False).head(n).to_dict("records")
        if direction in ("lowest", "both"):
            result["lowest"] = self.snapshot.sort_values(characteristic, ascending=True).head(n).to_dict("records")
        return result


class TestComplexConditionArgs(BaseModel):
    condition: str = Field(
        description=(
            "Boolean expression over mve, bm, mom12m using >, <, >=, <=, and, or, not, and "
            "numeric literals, e.g. 'bm > 0.5 and mom12m > -0.2'."
        )
    )


class TestComplexConditionTool(BaseTool):
    __test__ = False  # not a pytest test class - pytest's collector matches on the "Test" prefix

    name: str = "test_complex_condition"
    description: str = (
        'Test any candidate BUY, SELL, or HOLD condition (for example "bm > 0.5 and mom12m > '
        '-0.2") against the snapshot date\'s real firms before committing to it. Returns how '
        "many of the firms would match, what percentage that is, and a small sample of "
        "matching tickers. There is no limit on how many times you can call this - use it "
        "repeatedly while iterating on thresholds."
    )
    args_schema: type[BaseModel] = TestComplexConditionArgs
    snapshot: pd.DataFrame = Field(exclude=True)

    def _run(self, condition: str) -> dict | str:
        try:
            matches = self.snapshot[
                self.snapshot.apply(
                    lambda row: evaluate_condition(
                        condition, {"mve": row["mve"], "bm": row["bm"], "mom12m": row["mom12m"]}
                    ),
                    axis=1,
                )
            ]
        except ValueError as e:
            return f"Invalid condition, not tested: {e}"
        total = len(self.snapshot)
        count = len(matches)
        return {
            "matching_count": count,
            "total_firms": total,
            "matching_pct": round(100 * count / total, 2) if total else 0.0,
            "sample_tickers": matches["ticker"].head(10).tolist(),
        }
