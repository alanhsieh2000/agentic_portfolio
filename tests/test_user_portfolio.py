"""Tests for src/flow/user_portfolio.py's memory/portfolio.json read/write
round trip: the per-currency portfolios it holds, the share-count
validation a hand-editable file needs, and the per-currency independence
that keeps saving one portfolio from disturbing another.

Pure filesystem tests against `tmp_path` - no DuckDB, no network, no LLM,
per AGENTS.md.
"""

import json
from pathlib import Path

import pytest

from src.flow.user_portfolio import (
    load_all_portfolios,
    load_portfolio,
    save_portfolio,
)


def _write(path, payload):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(payload))


def test_load_portfolio_returns_empty_dict_when_file_missing(tmp_path):
    missing = str(tmp_path / "memory" / "portfolio.json")
    assert load_portfolio(missing) == {}


def test_load_all_portfolios_returns_empty_dict_when_file_missing(tmp_path):
    assert load_all_portfolios(str(tmp_path / "nope.json")) == {}


def test_save_then_load_round_trips_the_share_counts(tmp_path):
    path = str(tmp_path / "memory" / "portfolio.json")
    save_portfolio({"SPY": 1000, "T": 500.5}, path=path, currency="USD")
    assert load_portfolio(path, "USD") == {"SPY": 1000.0, "T": 500.5}


def test_save_portfolio_creates_the_parent_directory(tmp_path):
    path = str(tmp_path / "brand" / "new" / "portfolio.json")
    save_portfolio({"SPY": 1}, path=path)
    assert Path(path).exists()


def test_save_portfolio_records_an_updated_at_timestamp(tmp_path):
    path = str(tmp_path / "portfolio.json")
    save_portfolio({"SPY": 1}, path=path, currency="USD")
    stored = json.loads(Path(path).read_text())
    assert stored["portfolios"]["USD"]["updated_at"].endswith("+00:00")


def test_saving_one_currency_leaves_another_currencys_positions_untouched(tmp_path):
    path = str(tmp_path / "portfolio.json")
    save_portfolio({"1321.T": 50}, path=path, currency="JPY")
    save_portfolio({"SPY": 1000}, path=path, currency="USD")

    assert load_all_portfolios(path) == {"JPY": {"1321.T": 50.0}, "USD": {"SPY": 1000.0}}


def test_saving_one_currency_leaves_another_currencys_updated_at_untouched(tmp_path):
    path = str(tmp_path / "portfolio.json")
    save_portfolio({"1321.T": 50}, path=path, currency="JPY")
    before = json.loads(Path(path).read_text())["portfolios"]["JPY"]["updated_at"]

    save_portfolio({"SPY": 1000}, path=path, currency="USD")
    after = json.loads(Path(path).read_text())["portfolios"]["JPY"]["updated_at"]

    assert after == before


def test_tickers_are_upper_cased_on_the_way_in(tmp_path):
    path = str(tmp_path / "portfolio.json")
    save_portfolio({"spy": 10, " t ": 5}, path=path)
    assert load_portfolio(path) == {"SPY": 10.0, "T": 5.0}


def test_a_zero_share_count_retires_the_holding_rather_than_being_stored(tmp_path):
    path = str(tmp_path / "portfolio.json")
    save_portfolio({"SPY": 1000, "T": 0}, path=path, currency="USD")

    assert load_portfolio(path, "USD") == {"SPY": 1000.0}
    assert "T" not in json.loads(Path(path).read_text())["portfolios"]["USD"]["positions"]


def test_an_empty_portfolio_is_recorded_rather_than_removing_the_currency(tmp_path):
    path = str(tmp_path / "portfolio.json")
    save_portfolio({"SPY": 1000}, path=path, currency="USD")
    save_portfolio({}, path=path, currency="USD")

    stored = json.loads(Path(path).read_text())
    assert stored["portfolios"]["USD"]["positions"] == {}
    assert load_portfolio(path, "USD") == {}


def test_a_negative_share_count_is_refused_naming_the_file_and_currency(tmp_path):
    path = str(tmp_path / "portfolio.json")
    with pytest.raises(ValueError) as excinfo:
        save_portfolio({"SPY": -10}, path=path, currency="USD")

    message = str(excinfo.value)
    assert "portfolio.json" in message and "'USD'" in message and "SPY" in message


def test_a_non_numeric_share_count_is_refused_naming_the_file_and_currency(tmp_path):
    path = str(tmp_path / "portfolio.json")
    _write(path, {"portfolios": {"USD": {"positions": {"SPY": "a lot"}}}})

    with pytest.raises(ValueError) as excinfo:
        load_portfolio(path, "USD")

    message = str(excinfo.value)
    assert "portfolio.json" in message and "'USD'" in message and "SPY" in message


def test_a_boolean_share_count_is_refused_even_though_bool_is_an_int(tmp_path):
    path = str(tmp_path / "portfolio.json")
    _write(path, {"portfolios": {"USD": {"positions": {"SPY": True}}}})

    with pytest.raises(ValueError, match="must be a number"):
        load_portfolio(path, "USD")


def test_a_non_finite_share_count_is_refused(tmp_path):
    path = str(tmp_path / "portfolio.json")
    with pytest.raises(ValueError, match="must be finite"):
        save_portfolio({"SPY": float("inf")}, path=path, currency="USD")


def test_a_positions_field_that_is_not_an_object_is_refused(tmp_path):
    path = str(tmp_path / "portfolio.json")
    _write(path, {"portfolios": {"USD": {"positions": ["SPY", 1000]}}})

    with pytest.raises(ValueError, match="object mapping tickers to share counts"):
        load_all_portfolios(path)


def test_a_missing_positions_field_is_refused(tmp_path):
    path = str(tmp_path / "portfolio.json")
    _write(path, {"portfolios": {"USD": {"updated_at": "2026-01-01T00:00:00+00:00"}}})

    with pytest.raises(ValueError, match="'positions' field"):
        load_all_portfolios(path)


def test_malformed_json_propagates_rather_than_reading_as_no_holdings(tmp_path):
    path = tmp_path / "portfolio.json"
    path.write_text("{not json")

    with pytest.raises(json.JSONDecodeError):
        load_all_portfolios(str(path))


def test_load_portfolio_defaults_to_usd(tmp_path):
    path = str(tmp_path / "portfolio.json")
    save_portfolio({"SPY": 1000}, path=path, currency="USD")
    assert load_portfolio(path) == {"SPY": 1000.0}
