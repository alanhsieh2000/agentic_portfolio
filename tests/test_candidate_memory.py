"""Tests for src/flow/candidate_memory.py's memory/candidates.json
read/write round trip.

Pure filesystem tests against `tmp_path` - no DuckDB, no network, no LLM,
per AGENTS.md.
"""

import json
from pathlib import Path

import pytest

from src.flow.candidate_memory import load_candidate_pool, save_candidate_pool


def test_load_candidate_pool_returns_empty_list_when_file_missing(tmp_path):
    missing = str(tmp_path / "memory" / "candidates.json")
    assert load_candidate_pool(missing) == []


def test_save_then_load_round_trips_sorted_deduplicated_tickers(tmp_path):
    path = str(tmp_path / "candidates.json")
    save_candidate_pool(["MSFT", "AAPL", "MSFT"], path)
    assert load_candidate_pool(path) == ["AAPL", "MSFT"]


def test_save_candidate_pool_writes_an_updated_at_timestamp(tmp_path):
    path = tmp_path / "candidates.json"
    save_candidate_pool(["AAPL"], str(path))
    payload = json.loads(path.read_text())
    assert payload["tickers"] == ["AAPL"]
    assert payload["updated_at"]


def test_save_candidate_pool_creates_parent_directory(tmp_path):
    path = tmp_path / "memory" / "nested" / "candidates.json"
    save_candidate_pool(["AAPL"], str(path))
    assert path.exists()


def test_load_candidate_pool_raises_value_error_for_missing_tickers_key(tmp_path):
    path = tmp_path / "candidates.json"
    path.write_text(json.dumps({"updated_at": "2026-09-05T00:00:00+00:00"}))
    with pytest.raises(ValueError, match="tickers"):
        load_candidate_pool(str(path))


def test_load_candidate_pool_raises_value_error_when_tickers_is_not_a_string_list(tmp_path):
    path = tmp_path / "candidates.json"
    path.write_text(json.dumps({"tickers": ["AAPL", 7]}))
    with pytest.raises(ValueError, match="tickers"):
        load_candidate_pool(str(path))


def test_load_candidate_pool_propagates_json_decode_error_for_malformed_file(tmp_path):
    path = tmp_path / "candidates.json"
    path.write_text("{not json")
    with pytest.raises(json.JSONDecodeError):
        load_candidate_pool(str(path))


def test_save_candidate_pool_overwrites_a_previous_pool(tmp_path):
    path = str(tmp_path / "candidates.json")
    save_candidate_pool(["AAPL", "MSFT"], path)
    save_candidate_pool(["NVDA"], path)
    assert load_candidate_pool(path) == ["NVDA"]
    assert Path(path).exists()
