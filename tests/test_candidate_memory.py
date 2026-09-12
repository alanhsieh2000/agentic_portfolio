"""Tests for src/agentic_portfolio/flow/candidate_memory.py's memory/candidates.json
read/write round trip, including the per-currency pools it holds, each
pool's optional benchmark ticker, and the migration of the old single-pool
file shape.

Pure filesystem tests against `tmp_path` - no DuckDB, no network, no LLM,
per AGENTS.md.
"""

import json
from pathlib import Path

import pytest

from agentic_portfolio.flow.candidate_memory import (
    load_all_pools,
    load_candidate_benchmark,
    load_candidate_pool,
    load_pool_benchmarks,
    migrate_candidate_pools,
    save_candidate_pool,
)


def test_load_candidate_pool_returns_empty_list_when_file_missing(tmp_path):
    missing = str(tmp_path / "memory" / "candidates.json")
    assert load_candidate_pool(missing) == []


def test_load_all_pools_returns_empty_dict_when_file_missing(tmp_path):
    assert load_all_pools(str(tmp_path / "memory" / "candidates.json")) == {}


def test_save_then_load_round_trips_sorted_deduplicated_tickers(tmp_path):
    path = str(tmp_path / "candidates.json")
    save_candidate_pool(["MSFT", "AAPL", "MSFT"], path)
    assert load_candidate_pool(path) == ["AAPL", "MSFT"]


def test_save_candidate_pool_writes_an_updated_at_timestamp(tmp_path):
    path = tmp_path / "candidates.json"
    save_candidate_pool(["AAPL"], str(path))
    payload = json.loads(path.read_text())
    assert payload["pools"]["USD"]["tickers"] == ["AAPL"]
    assert payload["pools"]["USD"]["updated_at"]


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


def test_load_all_pools_names_the_offending_currency_in_its_error(tmp_path):
    path = tmp_path / "candidates.json"
    path.write_text(json.dumps({"pools": {"JPY": {"tickers": "7203.T"}}}))
    with pytest.raises(ValueError, match="JPY"):
        load_all_pools(str(path))


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


# ---------------------------------------------------------------------------
# several currencies in one file
# ---------------------------------------------------------------------------


def test_two_currencies_round_trip_independently_in_one_file(tmp_path):
    """The whole point of the keyed shape: one file, one pool per currency."""
    path = str(tmp_path / "candidates.json")

    save_candidate_pool(["AAPL", "SPY"], path, currency="USD")
    save_candidate_pool(["7203.T", "6758.T"], path, currency="JPY")

    assert load_all_pools(path) == {"USD": ["AAPL", "SPY"], "JPY": ["6758.T", "7203.T"]}
    assert load_candidate_pool(path, currency="USD") == ["AAPL", "SPY"]
    assert load_candidate_pool(path, currency="JPY") == ["6758.T", "7203.T"]


def test_saving_one_currency_leaves_another_untouched(tmp_path):
    path = str(tmp_path / "candidates.json")
    save_candidate_pool(["AAPL", "SPY"], path, currency="USD")
    save_candidate_pool(["7203.T"], path, currency="JPY")

    save_candidate_pool(["6758.T"], path, currency="JPY")

    assert load_candidate_pool(path, currency="USD") == ["AAPL", "SPY"]
    assert load_candidate_pool(path, currency="JPY") == ["6758.T"]


def test_saving_one_currency_preserves_anothers_timestamp(tmp_path):
    """An untouched pool must not look freshly written, or `updated_at`
    stops meaning anything for staleness purposes.
    """
    path = tmp_path / "candidates.json"
    save_candidate_pool(["AAPL"], str(path), currency="USD")
    usd_written_at = json.loads(path.read_text())["pools"]["USD"]["updated_at"]

    save_candidate_pool(["7203.T"], str(path), currency="JPY")

    payload = json.loads(path.read_text())
    assert payload["pools"]["USD"]["updated_at"] == usd_written_at
    assert payload["pools"]["JPY"]["updated_at"] != usd_written_at


def test_load_candidate_pool_returns_empty_for_a_currency_with_no_saved_pool(tmp_path):
    path = str(tmp_path / "candidates.json")
    save_candidate_pool(["AAPL"], path, currency="USD")

    assert load_candidate_pool(path, currency="JPY") == []


# ---------------------------------------------------------------------------
# migration from the old single-pool shape
# ---------------------------------------------------------------------------


def test_the_old_flat_file_shape_loads_as_the_usd_pool(tmp_path):
    """Nothing could be ingested before per-currency pools existed unless it
    was USD, so reading a legacy file as the USD pool loses no information.
    """
    path = tmp_path / "candidates.json"
    path.write_text(json.dumps({"tickers": ["SPY", "AAPL"], "updated_at": "2026-09-05T01:49:26+00:00"}))

    assert load_all_pools(str(path)) == {"USD": ["AAPL", "SPY"]}
    assert load_candidate_pool(str(path)) == ["AAPL", "SPY"]


def test_saving_a_new_currency_migrates_a_legacy_file_without_losing_it(tmp_path):
    path = tmp_path / "candidates.json"
    path.write_text(json.dumps({"tickers": ["AAPL"], "updated_at": "2026-09-05T01:49:26+00:00"}))

    save_candidate_pool(["7203.T"], str(path), currency="JPY")

    assert load_all_pools(str(path)) == {"USD": ["AAPL"], "JPY": ["7203.T"]}
    assert "pools" in json.loads(path.read_text())


# ---------------------------------------------------------------------------
# migrate_candidate_pools
# ---------------------------------------------------------------------------


def test_migrate_converts_a_legacy_file_and_preserves_its_timestamp(tmp_path):
    """The reason this exists rather than letting the next save convert the
    file: a save would stamp the current time over the only record of when
    the pool was curated.
    """
    path = tmp_path / "candidates.json"
    path.write_text(json.dumps({"tickers": ["SPY", "AAPL"], "updated_at": "2026-09-05T01:49:26.829379+00:00"}))

    assert migrate_candidate_pools(str(path)) == {"USD": ["AAPL", "SPY"]}

    payload = json.loads(path.read_text())
    assert payload == {
        "pools": {"USD": {"tickers": ["AAPL", "SPY"], "updated_at": "2026-09-05T01:49:26.829379+00:00"}}
    }


def test_migrate_is_idempotent(tmp_path):
    path = tmp_path / "candidates.json"
    path.write_text(json.dumps({"tickers": ["AAPL"], "updated_at": "2026-09-05T01:49:26+00:00"}))

    migrate_candidate_pools(str(path))
    once = path.read_text()
    migrate_candidate_pools(str(path))

    assert path.read_text() == once


def test_migrate_leaves_an_already_converted_multi_pool_file_intact(tmp_path):
    path = str(tmp_path / "candidates.json")
    save_candidate_pool(["AAPL", "SPY"], path, currency="USD")
    save_candidate_pool(["7203.T"], path, currency="JPY")
    before = json.loads(Path(path).read_text())

    assert migrate_candidate_pools(path) == {"USD": ["AAPL", "SPY"], "JPY": ["7203.T"]}

    assert json.loads(Path(path).read_text()) == before


def test_migrate_reports_a_missing_file_without_creating_one(tmp_path):
    """A read-shaped operation must not materialize the thing it was asked
    about - the same rule `load_ticker_currencies` follows.
    """
    missing = tmp_path / "memory" / "candidates.json"

    assert migrate_candidate_pools(str(missing)) == {}
    assert not missing.exists()


def test_migrate_leaves_a_legacy_file_without_a_timestamp_null(tmp_path):
    """Back-filling `updated_at` with the migration time would invent a
    curation date that is not known.
    """
    path = tmp_path / "candidates.json"
    path.write_text(json.dumps({"tickers": ["AAPL"]}))

    migrate_candidate_pools(str(path))

    assert json.loads(path.read_text())["pools"]["USD"]["updated_at"] is None


# ---------------------------------------------------------------------------
# per-pool benchmarks
# ---------------------------------------------------------------------------


def test_save_candidate_pool_records_a_benchmark(tmp_path):
    path = str(tmp_path / "candidates.json")
    save_candidate_pool(["AAPL", "MSFT"], path, currency="USD", benchmark="spy")

    assert load_candidate_benchmark(path, "USD") == "SPY"
    assert load_candidate_pool(path, "USD") == ["AAPL", "MSFT"]


def test_saving_without_a_benchmark_preserves_a_previously_saved_one(tmp_path):
    """The asymmetry that matters: every ordinary add/remove edit saves the
    pool with no benchmark argument, so `None` must mean "leave it alone"
    rather than "clear it", or the first ticker edit after choosing a
    benchmark would silently throw that choice away.
    """
    path = str(tmp_path / "candidates.json")
    save_candidate_pool(["AAPL"], path, currency="USD", benchmark="QQQ")

    save_candidate_pool(["AAPL", "NVDA"], path, currency="USD")

    assert load_candidate_benchmark(path, "USD") == "QQQ"
    assert load_candidate_pool(path, "USD") == ["AAPL", "NVDA"]


def test_naming_a_different_benchmark_replaces_the_recorded_one(tmp_path):
    path = str(tmp_path / "candidates.json")
    save_candidate_pool(["AAPL"], path, currency="USD", benchmark="SPY")

    save_candidate_pool(["AAPL"], path, currency="USD", benchmark="VOO")

    assert load_candidate_benchmark(path, "USD") == "VOO"


def test_saving_a_benchmark_leaves_another_currencys_benchmark_untouched(tmp_path):
    path = str(tmp_path / "candidates.json")
    save_candidate_pool(["7203.T"], path, currency="JPY", benchmark="1306.T")

    save_candidate_pool(["AAPL"], path, currency="USD", benchmark="SPY")

    assert load_pool_benchmarks(path) == {"JPY": "1306.T", "USD": "SPY"}


def test_load_candidate_benchmark_is_none_when_none_was_recorded(tmp_path):
    path = str(tmp_path / "candidates.json")
    save_candidate_pool(["7203.T"], path, currency="JPY")

    assert load_candidate_benchmark(path, "JPY") is None
    assert load_candidate_benchmark(path, "USD") is None


def test_load_pool_benchmarks_is_empty_when_the_file_is_missing(tmp_path):
    assert load_pool_benchmarks(str(tmp_path / "memory" / "candidates.json")) == {}


def test_write_omits_the_benchmark_key_when_there_is_none(tmp_path):
    """A pool that never named a benchmark stays byte-identical to how it was
    written before benchmarks existed, which is what keeps the migration a
    genuine no-op on such a file.
    """
    path = tmp_path / "candidates.json"
    save_candidate_pool(["AAPL"], str(path), currency="USD")

    entry = json.loads(path.read_text())["pools"]["USD"]
    assert "benchmark" not in entry
    assert sorted(entry) == ["tickers", "updated_at"]


def test_an_empty_benchmark_string_records_nothing(tmp_path):
    path = tmp_path / "candidates.json"
    save_candidate_pool(["AAPL"], str(path), currency="USD", benchmark="   ")

    assert "benchmark" not in json.loads(path.read_text())["pools"]["USD"]


def test_a_non_string_benchmark_is_a_value_error_naming_the_currency(tmp_path):
    path = tmp_path / "candidates.json"
    path.write_text(json.dumps({"pools": {"USD": {"tickers": ["AAPL"], "benchmark": 42}}}))

    with pytest.raises(ValueError) as excinfo:
        load_pool_benchmarks(str(path))

    assert "USD" in str(excinfo.value)
    assert "benchmark" in str(excinfo.value)


def test_migrate_preserves_a_recorded_benchmark(tmp_path):
    path = tmp_path / "candidates.json"
    path.write_text(
        json.dumps(
            {
                "pools": {
                    "USD": {"tickers": ["AAPL"], "benchmark": "SPY", "updated_at": "2026-01-01T00:00:00+00:00"},
                    "JPY": {"tickers": ["7203.T"], "updated_at": "2026-02-01T00:00:00+00:00"},
                }
            },
            indent=2,
        )
    )
    before = path.read_text()

    migrate_candidate_pools(str(path))

    assert path.read_text() == before
    assert load_pool_benchmarks(str(path)) == {"USD": "SPY", "JPY": None}


def test_the_old_flat_file_shape_has_no_benchmark(tmp_path):
    path = tmp_path / "candidates.json"
    path.write_text(json.dumps({"tickers": ["AAPL"], "updated_at": "2026-01-01T00:00:00+00:00"}))

    assert load_pool_benchmarks(str(path)) == {"USD": None}
    assert load_candidate_benchmark(str(path)) is None
