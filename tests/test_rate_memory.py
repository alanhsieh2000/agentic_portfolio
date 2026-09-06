"""Tests for src/flow/rate_memory.py's memory/rates.json read/write round
trip, the per-currency independence that keeps one currency's rate from
disturbing another's, the validation a hand-editable rate file needs, and
the four-deep precedence both CLI layers apply.

Pure filesystem and pure-function tests against `tmp_path` - no DuckDB, no
network, no LLM, per AGENTS.md.

The load-bearing tests here are
`test_a_negative_rate_is_accepted_because_yen_policy_rates_have_been_negative`
and `test_a_rate_of_one_or_more_is_refused_as_a_mistyped_percentage`: the
first guards the currency that motivated this whole feature, and the second
guards against a remembered nonsense rate breaking every later MSR run.
"""

import json
import math
from pathlib import Path

import pytest

from src.flow.rate_memory import (
    MAX_ABS_RISK_FREE_RATE,
    load_all_risk_free_rates,
    load_risk_free_rate,
    resolve_risk_free_rate,
    save_risk_free_rate,
    validate_risk_free_rate,
)


def _write(path, payload):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(payload))


# --- validate_risk_free_rate ------------------------------------------------


def test_an_ordinary_decimal_rate_is_accepted():
    assert validate_risk_free_rate(0.0425, "--risk-free-rate") == 0.0425


def test_an_integer_zero_is_accepted():
    assert validate_risk_free_rate(0, "--risk-free-rate") == 0.0


def test_a_negative_rate_is_accepted_because_yen_policy_rates_have_been_negative():
    """The currency that motivated this feature. Refusing negatives would
    reintroduce the bug for exactly the portfolio it was meant to fix.
    """
    assert validate_risk_free_rate(-0.001, "--risk-free-rate") == -0.001


def test_a_non_numeric_rate_is_refused_naming_its_source():
    with pytest.raises(ValueError, match="--risk-free-rate must be a number"):
        validate_risk_free_rate("cheap", "--risk-free-rate")


def test_a_boolean_rate_is_refused_even_though_bool_is_an_int():
    """`isinstance(True, int)` is true in Python and PyPortfolioOpt's own
    guard has the same hole, so `true` in the file would silently become a
    100% rate if this check were left to the numeric test.
    """
    with pytest.raises(ValueError, match="must be a number"):
        validate_risk_free_rate(True, "--risk-free-rate")


def test_a_nan_rate_is_refused_rather_than_producing_a_nan_sharpe_ratio():
    with pytest.raises(ValueError, match="must be a finite number"):
        validate_risk_free_rate(float("nan"), "--risk-free-rate")


def test_an_infinite_rate_is_refused():
    with pytest.raises(ValueError, match="must be a finite number"):
        validate_risk_free_rate(float("inf"), "--risk-free-rate")


def test_a_rate_of_one_or_more_is_refused_as_a_mistyped_percentage():
    with pytest.raises(ValueError) as excinfo:
        validate_risk_free_rate(4.5, "--risk-free-rate")

    assert "4.5% is 0.045" in str(excinfo.value)


def test_exactly_one_is_refused_not_merely_above_one():
    with pytest.raises(ValueError, match="decimal rate"):
        validate_risk_free_rate(MAX_ABS_RISK_FREE_RATE, "--risk-free-rate")


def test_a_large_negative_rate_is_refused_too():
    with pytest.raises(ValueError, match="decimal rate"):
        validate_risk_free_rate(-4.5, "--risk-free-rate")


# --- load / save round trip -------------------------------------------------


def test_load_risk_free_rate_returns_none_when_the_file_is_missing(tmp_path):
    assert load_risk_free_rate(str(tmp_path / "memory" / "rates.json"), "USD") is None


def test_load_all_risk_free_rates_returns_empty_dict_when_the_file_is_missing(tmp_path):
    assert load_all_risk_free_rates(str(tmp_path / "nope.json")) == {}


def test_save_then_load_round_trips_the_rate(tmp_path):
    path = str(tmp_path / "memory" / "rates.json")
    assert save_risk_free_rate(0.0425, path=path, currency="USD") is True
    assert load_risk_free_rate(path, "USD") == 0.0425


def test_save_risk_free_rate_creates_the_parent_directory(tmp_path):
    path = str(tmp_path / "brand" / "new" / "rates.json")
    save_risk_free_rate(0.02, path=path)
    assert Path(path).exists()


def test_save_risk_free_rate_records_an_updated_at_timestamp(tmp_path):
    path = str(tmp_path / "rates.json")
    save_risk_free_rate(0.005, path=path, currency="JPY")
    stored = json.loads(Path(path).read_text())
    assert stored["rates"]["JPY"]["updated_at"].endswith("+00:00")


def test_a_negative_rate_round_trips_through_the_file(tmp_path):
    path = str(tmp_path / "rates.json")
    save_risk_free_rate(-0.001, path=path, currency="JPY")
    assert load_risk_free_rate(path, "JPY") == -0.001


def test_remembering_one_currencys_rate_leaves_anothers_untouched(tmp_path):
    path = str(tmp_path / "rates.json")
    save_risk_free_rate(0.005, path=path, currency="JPY")
    save_risk_free_rate(0.0425, path=path, currency="USD")

    assert load_all_risk_free_rates(path) == {"JPY": 0.005, "USD": 0.0425}


def test_remembering_one_currencys_rate_leaves_anothers_updated_at_untouched(tmp_path):
    path = str(tmp_path / "rates.json")
    save_risk_free_rate(0.005, path=path, currency="JPY")
    before = json.loads(Path(path).read_text())["rates"]["JPY"]["updated_at"]

    save_risk_free_rate(0.0425, path=path, currency="USD")
    after = json.loads(Path(path).read_text())["rates"]["JPY"]["updated_at"]

    assert after == before


def test_re_saving_an_identical_rate_writes_nothing_and_says_so(tmp_path):
    """`updated_at` must record when the rate CHANGED, not when it was last
    restated - every run passing --risk-free-rate arrives here.
    """
    path = str(tmp_path / "rates.json")
    save_risk_free_rate(0.0425, path=path, currency="USD")
    before = json.loads(Path(path).read_text())["rates"]["USD"]["updated_at"]

    assert save_risk_free_rate(0.0425, path=path, currency="USD") is False
    assert json.loads(Path(path).read_text())["rates"]["USD"]["updated_at"] == before


def test_saving_a_different_rate_does_write_and_says_so(tmp_path):
    path = str(tmp_path / "rates.json")
    save_risk_free_rate(0.0425, path=path, currency="USD")

    assert save_risk_free_rate(0.045, path=path, currency="USD") is True
    assert load_risk_free_rate(path, "USD") == 0.045


def test_a_lower_case_currency_is_upper_cased_on_write(tmp_path):
    path = str(tmp_path / "rates.json")
    save_risk_free_rate(0.005, path=path, currency="jpy")
    assert load_all_risk_free_rates(path) == {"JPY": 0.005}


def test_a_hand_edited_lower_case_currency_key_is_upper_cased_on_read(tmp_path):
    """Otherwise a `"jpy"` entry would live alongside `"JPY"` as a second rate
    for one currency, with no way to tell which a report used.
    """
    path = str(tmp_path / "rates.json")
    _write(path, {"rates": {"jpy": {"risk_free_rate": 0.005}}})

    assert load_risk_free_rate(path, "JPY") == 0.005
    assert load_all_risk_free_rates(path) == {"JPY": 0.005}


def test_load_risk_free_rate_defaults_to_usd(tmp_path):
    path = str(tmp_path / "rates.json")
    save_risk_free_rate(0.0425, path=path, currency="USD")
    assert load_risk_free_rate(path) == 0.0425


def test_a_currency_with_no_remembered_rate_reads_as_none(tmp_path):
    path = str(tmp_path / "rates.json")
    save_risk_free_rate(0.0425, path=path, currency="USD")
    assert load_risk_free_rate(path, "JPY") is None


# --- validation of what is already on disk ----------------------------------


def test_a_bad_stored_rate_is_refused_naming_the_file_and_the_currency(tmp_path):
    path = str(tmp_path / "rates.json")
    _write(path, {"rates": {"JPY": {"risk_free_rate": "cheap"}}})

    with pytest.raises(ValueError) as excinfo:
        load_risk_free_rate(path, "JPY")

    message = str(excinfo.value)
    assert "rates.json" in message and "'JPY'" in message


def test_a_stored_rate_of_one_or_more_is_refused_rather_than_warned_about_once(tmp_path):
    """A bad value hand-edited into the file must not warn on the run that
    wrote it and then be silently trusted forever after.
    """
    path = str(tmp_path / "rates.json")
    _write(path, {"rates": {"USD": {"risk_free_rate": 4.5}}})

    with pytest.raises(ValueError, match="decimal rate"):
        load_risk_free_rate(path, "USD")


def test_a_missing_risk_free_rate_field_is_refused(tmp_path):
    path = str(tmp_path / "rates.json")
    _write(path, {"rates": {"USD": {"updated_at": "2026-01-01T00:00:00+00:00"}}})

    with pytest.raises(ValueError, match="must be a number"):
        load_all_risk_free_rates(path)


def test_malformed_json_propagates_rather_than_reading_as_no_rate_remembered(tmp_path):
    path = tmp_path / "rates.json"
    path.write_text("{not json")

    with pytest.raises(json.JSONDecodeError):
        load_all_risk_free_rates(str(path))


def test_saving_a_bad_rate_is_refused_before_anything_is_written(tmp_path):
    path = tmp_path / "rates.json"

    with pytest.raises(ValueError):
        save_risk_free_rate(float("nan"), path=str(path), currency="USD")

    assert not path.exists()


# --- resolve_risk_free_rate -------------------------------------------------


def test_the_command_line_override_wins_over_a_remembered_rate():
    resolved = resolve_risk_free_rate(override=0.045, currency="USD", saved=0.0425, default=0.02)

    assert resolved.rate == 0.045
    assert resolved.origin == "--risk-free-rate, remembered for USD"
    assert resolved.from_override is True


def test_a_remembered_rate_wins_over_the_configured_default():
    resolved = resolve_risk_free_rate(override=None, currency="JPY", saved=0.005, default=0.02)

    assert resolved.rate == 0.005
    assert resolved.origin == "remembered for JPY"
    assert resolved.from_override is False


def test_the_configured_default_applies_when_nothing_is_remembered():
    resolved = resolve_risk_free_rate(override=None, currency="USD", saved=None, default=0.02)

    assert resolved.rate == 0.02
    assert resolved.origin == "the configured default"
    assert resolved.from_override is False


def test_an_origin_is_always_populated_so_a_rate_can_never_print_unprovenanced():
    for override, saved in ((0.03, 0.02), (None, 0.02), (None, None)):
        resolved = resolve_risk_free_rate(override, "USD", saved, 0.02)
        assert resolved.origin


def test_a_remembered_rate_of_zero_is_not_mistaken_for_nothing_remembered():
    """`0.0` is falsy, so a naive truthiness check would silently fall back
    to the 2% default for somebody who deliberately chose 0%.
    """
    resolved = resolve_risk_free_rate(override=None, currency="USD", saved=0.0, default=0.02)

    assert resolved.rate == 0.0
    assert resolved.origin == "remembered for USD"


def test_an_override_of_zero_is_not_mistaken_for_no_override():
    resolved = resolve_risk_free_rate(override=0.0, currency="USD", saved=0.0425, default=0.02)

    assert resolved.rate == 0.0
    assert resolved.from_override is True


def test_resolve_upper_cases_the_currency_in_the_origin():
    assert resolve_risk_free_rate(None, "jpy", 0.005, 0.02).origin == "remembered for JPY"


def test_resolve_consults_no_table_of_per_currency_defaults():
    """There is deliberately no DEFAULT_RISK_FREE_RATES; an unknown currency
    with nothing remembered falls back to the caller's default like any other.
    """
    resolved = resolve_risk_free_rate(None, "ZWL", None, 0.02)

    assert resolved.rate == 0.02
    assert resolved.origin == "the configured default"
