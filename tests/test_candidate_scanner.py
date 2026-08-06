"""Tests for src/scanner/candidate_scanner.py.

Pure set arithmetic over hand-built fixture DataFrames - no database, no
yfinance, no LLM calls, per AGENTS.md's testing guidance.
"""

import pandas as pd
import pytest

from src.scanner.candidate_scanner import scan, scan_with_detail


def _signals(*ticker_signal_pairs: tuple[str, str]) -> pd.DataFrame:
    return pd.DataFrame(ticker_signal_pairs, columns=["ticker", "signal"])


def test_intersection_of_3_or_more_returns_exactly_that_intersection():
    llm_s = _signals(("AAPL", "buy"), ("MSFT", "buy"), ("NVDA", "buy"), ("XOM", "sell"), ("F", "hold"))
    llm_f = _signals(("AAPL", "buy"), ("MSFT", "buy"), ("NVDA", "buy"), ("TSLA", "buy"), ("F", "buy"))

    result = scan_with_detail(llm_s, llm_f)

    assert result["branch"] == "intersection"
    assert result["candidates"] == ["AAPL", "MSFT", "NVDA"]
    assert scan(llm_s, llm_f) == ["AAPL", "MSFT", "NVDA"]


def test_intersection_of_exactly_1_falls_back_to_full_union():
    llm_s = _signals(("AAPL", "buy"), ("MSFT", "buy"))
    llm_f = _signals(("AAPL", "buy"), ("TSLA", "buy"), ("F", "buy"))

    result = scan_with_detail(llm_s, llm_f)

    assert result["branch"] == "union"
    assert result["candidates"] == ["AAPL", "F", "MSFT", "TSLA"]


def test_zero_overlap_falls_back_to_full_union():
    llm_s = _signals(("AAPL", "buy"), ("MSFT", "buy"))
    llm_f = _signals(("TSLA", "buy"), ("F", "buy"))

    result = scan_with_detail(llm_s, llm_f)

    assert result["branch"] == "union"
    assert result["candidates"] == ["AAPL", "F", "MSFT", "TSLA"]


def test_llm_s_only_returns_its_buy_set_directly():
    llm_s = _signals(("AAPL", "buy"), ("MSFT", "sell"), ("NVDA", "buy"))

    result = scan_with_detail(llm_s, None)

    assert result["branch"] == "llm_s_only"
    assert result["candidates"] == ["AAPL", "NVDA"]
    assert scan(llm_s, None) == ["AAPL", "NVDA"]


def test_llm_f_only_returns_its_buy_set_directly():
    llm_f = _signals(("AAPL", "hold"), ("TSLA", "buy"))

    result = scan_with_detail(None, llm_f)

    assert result["branch"] == "llm_f_only"
    assert result["candidates"] == ["TSLA"]


def test_both_none_raises_value_error():
    with pytest.raises(ValueError):
        scan_with_detail(None, None)
    with pytest.raises(ValueError):
        scan(None, None)
