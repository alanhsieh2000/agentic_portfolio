"""Combine LLM-S and LLM-F's independent buy/sell/hold signals into one
final candidate list, per the paper's consensus rule (arXiv:2603.23300,
Section 5.2): two independent, differently-informed agents each cast a
vote, and by the time this module's code runs, both have already produced
their final signals independently - combining those two already-final
answers is deterministic set arithmetic, not further LLM reasoning.
"""

from __future__ import annotations

import pandas as pd


def _buy_set(signals: pd.DataFrame) -> set[str]:
    return set(signals.loc[signals["signal"] == "buy", "ticker"])


def scan_with_detail(
    llm_s_signals: pd.DataFrame | None,
    llm_f_signals: pd.DataFrame | None,
) -> dict:
    """Combine `llm_s_signals`/`llm_f_signals` (each shaped like
    `plans/02_llm_s_agent.md`'s `screen` output / `plans/03_llm_f_agent.md`'s
    `screen_month` output: columns `ticker`, `signal`) into one candidate
    list, reporting which branch was taken and the raw set sizes involved.
    At least one of the two must be provided - both `None` is a
    programming error, raised as `ValueError` rather than silently
    returning an empty list.

    Single-agent mode ("and/or" per README.md): if only one is given,
    returns that agent's buy set directly (`branch` = `"llm_s_only"` or
    `"llm_f_only"`).

    Two-agent mode: the paper's exact consensus rule - the intersection of
    both buy sets, unless that intersection has cardinality 1 or fewer (in
    which case a consensus of 0-1 stocks would give a near-empty or empty
    portfolio), falling back to the union instead (`branch` =
    `"intersection"` or `"union"`).
    """
    if llm_s_signals is None and llm_f_signals is None:
        raise ValueError("at least one of llm_s_signals or llm_f_signals must be provided")

    if llm_f_signals is None:
        buy_s = _buy_set(llm_s_signals)
        return {
            "candidates": sorted(buy_s),
            "branch": "llm_s_only",
            "buy_s_size": len(buy_s),
            "buy_f_size": None,
            "intersection_size": None,
            "union_size": None,
        }

    if llm_s_signals is None:
        buy_f = _buy_set(llm_f_signals)
        return {
            "candidates": sorted(buy_f),
            "branch": "llm_f_only",
            "buy_s_size": None,
            "buy_f_size": len(buy_f),
            "intersection_size": None,
            "union_size": None,
        }

    buy_s = _buy_set(llm_s_signals)
    buy_f = _buy_set(llm_f_signals)
    intersection = buy_s & buy_f
    union = buy_s | buy_f

    if len(intersection) > 1:
        branch = "intersection"
        candidates = intersection
    else:
        branch = "union"
        candidates = union

    return {
        "candidates": sorted(candidates),
        "branch": branch,
        "buy_s_size": len(buy_s),
        "buy_f_size": len(buy_f),
        "intersection_size": len(intersection),
        "union_size": len(union),
    }


def scan(
    llm_s_signals: pd.DataFrame | None,
    llm_f_signals: pd.DataFrame | None,
) -> list[str]:
    """The candidate ticker list only - see `scan_with_detail` for the
    branch taken and the raw set sizes behind it.
    """
    return scan_with_detail(llm_s_signals, llm_f_signals)["candidates"]
