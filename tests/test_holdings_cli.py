"""Tests for src/flow/holdings_cli.py (`uv run portfolio-holdings`) and
src/flow/cli.py's `print_user_portfolio`: recording and retiring holdings,
the per-currency routing that decides which portfolio an edit lands in, and
the exact wording of the report block both commands share.

Per AGENTS.md's testing guidance these are hermetic. Yahoo Finance is
reached only through `validate_and_ingest_tickers` and the risk figures
only through `prepare_holdings`, both monkeypatched on
`src.flow.holdings_cli`'s own symbols, so nothing here touches the network
or the real `memory/portfolio.json`.
"""

from datetime import date
from pathlib import Path

import pytest

from src.config.settings import settings
from src.flow.holdings_cli import _normalize_ticker, _parse_pairs, main
from src.flow.rate_memory import load_all_risk_free_rates, load_risk_free_rate
from src.flow.user_portfolio import load_all_portfolios, load_portfolio
from src.optimizer.holdings import HoldingsStats, unavailable_holdings
from src.flow.cli import format_share_count, print_user_portfolio


def _stub_ingest(monkeypatch, currencies: dict[str, str], invalid: dict[str, str] | None = None):
    """Stand in for `validate_and_ingest_tickers`, resolving exactly the
    tickers in `currencies` and reporting the rest as not found.
    """
    invalid = invalid or {}

    def fake(tickers, as_of, db_path):
        valid = sorted(t for t in tickers if t in currencies)
        missing = {t: "not found on Yahoo Finance" for t in tickers if t not in currencies}
        return valid, {**missing, **invalid}, {t: currencies[t] for t in valid}

    monkeypatch.setattr("src.flow.holdings_cli.validate_and_ingest_tickers", fake)


def _stub_report(monkeypatch) -> list[tuple[str, dict]]:
    """Replace `prepare_holdings` with a spy, so a CLI test asserts on what
    was saved and reported rather than on estimator arithmetic (covered by
    tests/test_holdings.py). Returns the list it records into.
    """
    seen: list[tuple[str, dict]] = []

    def fake(positions, currency, rebalance_date, db_path, risk_free_rate=0.02, allow_fetch=True):
        seen.append((currency, dict(positions), risk_free_rate))
        return unavailable_holdings(currency, dict(positions), risk_free_rate, "stubbed")

    monkeypatch.setattr("src.flow.holdings_cli.prepare_holdings", fake)
    return seen


def _run(monkeypatch, path, argv: list[str], rates_path=None) -> None:
    """Drive `main()` with both memory files pointed inside `tmp_path`.

    `--rates-path` is not optional politeness: an argparse default is bound
    at parse time, so monkeypatching `DEFAULT_RATES_PATH` would not take
    effect. Without the flag these tests would READ and, for any run passing
    `--risk-free-rate`, WRITE the developer's real `memory/rates.json` -
    green on a clean checkout, failing on a machine that has ever remembered
    a rate, and invisible in `git status` because `memory/` is gitignored.
    """
    rates_path = rates_path or Path(path).with_name("rates.json")
    monkeypatch.setattr(
        "sys.argv",
        ["portfolio-holdings", *argv, "--path", str(path), "--rates-path", str(rates_path)],
    )
    main()


# --- token parsing ----------------------------------------------------------


def test_a_trailing_comma_is_stripped_from_a_typed_ticker():
    assert _normalize_ticker("SPY,") == "SPY"
    assert _normalize_ticker(" spy ") == "SPY"


def test_pairs_are_parsed_in_the_order_typed():
    assert _parse_pairs(["AAPL", "10", "7203.T", "5"]) == [("AAPL", 10.0), ("7203.T", 5.0)]


def test_an_odd_number_of_tokens_is_refused_by_name():
    with pytest.raises(ValueError, match="odd number of values"):
        _parse_pairs(["SPY", "1000", "T"])


def test_a_non_numeric_share_count_is_refused_naming_the_ticker():
    with pytest.raises(ValueError, match="share count for SPY"):
        _parse_pairs(["SPY", "lots"])


def test_a_negative_share_count_is_refused_by_the_parser():
    with pytest.raises(ValueError, match="no model of a short position"):
        _parse_pairs(["SPY", "-10"])


# --- set --------------------------------------------------------------------


def test_set_records_the_share_counts_and_reports_the_portfolio(monkeypatch, tmp_path, capsys):
    path = tmp_path / "portfolio.json"
    _stub_ingest(monkeypatch, {"SPY": "USD", "T": "USD"})
    seen = _stub_report(monkeypatch)

    _run(monkeypatch, path, ["set", "SPY", "1000", "T", "500"])

    assert load_portfolio(str(path), "USD") == {"SPY": 1000.0, "T": 500.0}
    assert [(c, p) for c, p, _ in seen] == [("USD", {"SPY": 1000.0, "T": 500.0})]
    assert "Recorded in the USD portfolio" in capsys.readouterr().out


def test_a_tickers_own_currency_selects_the_portfolio_it_lands_in(monkeypatch, tmp_path):
    path = tmp_path / "portfolio.json"
    _stub_ingest(monkeypatch, {"SPY": "USD", "1321.T": "JPY"})
    _stub_report(monkeypatch)

    _run(monkeypatch, path, ["set", "SPY", "1000"])
    _run(monkeypatch, path, ["set", "1321.T", "50"])

    assert load_all_portfolios(str(path)) == {"USD": {"SPY": 1000.0}, "JPY": {"1321.T": 50.0}}


def test_naming_two_currencies_in_one_set_refuses_the_second_by_name(monkeypatch, tmp_path, capsys):
    path = tmp_path / "portfolio.json"
    _stub_ingest(monkeypatch, {"SPY": "USD", "1321.T": "JPY"})
    _stub_report(monkeypatch)

    _run(monkeypatch, path, ["set", "SPY", "1000", "1321.T", "50"])

    out = capsys.readouterr().out
    assert "Refused: 1321.T is priced in JPY" in out
    assert load_all_portfolios(str(path)) == {"USD": {"SPY": 1000.0}}


def test_an_explicit_currency_refuses_a_ticker_from_another_one(monkeypatch, tmp_path, capsys):
    path = tmp_path / "portfolio.json"
    _stub_ingest(monkeypatch, {"1321.T": "JPY"})
    _stub_report(monkeypatch)

    _run(monkeypatch, path, ["--currency", "USD", "set", "1321.T", "50"])

    assert "Refused: 1321.T is priced in JPY" in capsys.readouterr().out
    assert load_all_portfolios(str(path)) == {}


def test_an_unresolvable_ticker_is_reported_and_nothing_is_saved(monkeypatch, tmp_path, capsys):
    path = tmp_path / "portfolio.json"
    _stub_ingest(monkeypatch, {})
    _stub_report(monkeypatch)

    _run(monkeypatch, path, ["set", "NOTATICKER", "10"])

    out = capsys.readouterr().out
    assert "Ignored (not found): NOTATICKER." in out
    assert "Nothing was saved." in out
    assert not path.exists()


def test_a_good_ticker_typed_beside_a_bad_one_still_lands(monkeypatch, tmp_path, capsys):
    path = tmp_path / "portfolio.json"
    _stub_ingest(monkeypatch, {"SPY": "USD"})
    _stub_report(monkeypatch)

    _run(monkeypatch, path, ["set", "SPY", "1000", "NOTATICKER", "10"])

    assert "Ignored (not found): NOTATICKER." in capsys.readouterr().out
    assert load_portfolio(str(path), "USD") == {"SPY": 1000.0}


def test_setting_zero_shares_retires_the_holding(monkeypatch, tmp_path, capsys):
    path = tmp_path / "portfolio.json"
    _stub_ingest(monkeypatch, {"SPY": "USD", "T": "USD"})
    _stub_report(monkeypatch)

    _run(monkeypatch, path, ["set", "SPY", "1000", "T", "500"])
    _run(monkeypatch, path, ["set", "T", "0"])

    assert load_portfolio(str(path), "USD") == {"SPY": 1000.0}
    assert "Retired from the USD portfolio: T." in capsys.readouterr().out


def test_set_updates_an_existing_holding_rather_than_duplicating_it(monkeypatch, tmp_path):
    path = tmp_path / "portfolio.json"
    _stub_ingest(monkeypatch, {"SPY": "USD"})
    _stub_report(monkeypatch)

    _run(monkeypatch, path, ["set", "SPY", "1000"])
    _run(monkeypatch, path, ["set", "SPY", "1200.5"])

    assert load_portfolio(str(path), "USD") == {"SPY": 1200.5}


def test_a_comma_after_a_ticker_is_accepted(monkeypatch, tmp_path):
    path = tmp_path / "portfolio.json"
    _stub_ingest(monkeypatch, {"SPY": "USD"})
    _stub_report(monkeypatch)

    _run(monkeypatch, path, ["set", "SPY,", "1000"])

    assert load_portfolio(str(path), "USD") == {"SPY": 1000.0}


def test_a_mistyped_pair_exits_non_zero_without_saving(monkeypatch, tmp_path, capsys):
    path = tmp_path / "portfolio.json"
    _stub_ingest(monkeypatch, {"SPY": "USD"})
    _stub_report(monkeypatch)

    with pytest.raises(SystemExit) as excinfo:
        _run(monkeypatch, path, ["set", "SPY", "1000", "T"])

    assert excinfo.value.code == 2
    assert "odd number of values" in capsys.readouterr().err
    assert not path.exists()


# --- remove -----------------------------------------------------------------


def test_remove_infers_the_portfolio_that_holds_the_ticker(monkeypatch, tmp_path, capsys):
    path = tmp_path / "portfolio.json"
    _stub_ingest(monkeypatch, {"SPY": "USD", "1321.T": "JPY"})
    _stub_report(monkeypatch)
    _run(monkeypatch, path, ["set", "SPY", "1000"])
    _run(monkeypatch, path, ["set", "1321.T", "50"])

    _run(monkeypatch, path, ["remove", "1321.T"])

    assert load_all_portfolios(str(path)) == {"USD": {"SPY": 1000.0}, "JPY": {}}
    assert "Removed from the JPY portfolio: 1321.T." in capsys.readouterr().out


def test_removing_a_ticker_held_in_two_portfolios_is_refused_as_ambiguous(monkeypatch, tmp_path, capsys):
    path = tmp_path / "portfolio.json"
    _stub_ingest(monkeypatch, {"VUAA.L": "USD"})
    _stub_report(monkeypatch)
    _run(monkeypatch, path, ["set", "VUAA.L", "10"])
    _stub_ingest(monkeypatch, {"VUAA.L": "GBP"})
    _run(monkeypatch, path, ["--currency", "GBP", "set", "VUAA.L", "20"])

    with pytest.raises(SystemExit):
        _run(monkeypatch, path, ["remove", "VUAA.L"])

    assert "more than one portfolio" in capsys.readouterr().err
    assert load_all_portfolios(str(path)) == {"USD": {"VUAA.L": 10.0}, "GBP": {"VUAA.L": 20.0}}


def test_removing_a_ticker_that_is_not_held_says_so(monkeypatch, tmp_path, capsys):
    path = tmp_path / "portfolio.json"
    _stub_report(monkeypatch)

    _run(monkeypatch, path, ["remove", "SPY"])

    assert "Not held in any saved portfolio: SPY." in capsys.readouterr().out


# --- show -------------------------------------------------------------------


def test_show_reports_every_saved_currency(monkeypatch, tmp_path):
    path = tmp_path / "portfolio.json"
    _stub_ingest(monkeypatch, {"SPY": "USD", "1321.T": "JPY"})
    _stub_report(monkeypatch)
    _run(monkeypatch, path, ["set", "SPY", "1000"])
    _run(monkeypatch, path, ["set", "1321.T", "50"])

    seen = _stub_report(monkeypatch)
    _run(monkeypatch, path, [])

    assert [currency for currency, _, _ in seen] == ["JPY", "USD"]


def test_show_with_nothing_saved_still_reports_usd_so_the_hint_is_printed(monkeypatch, tmp_path):
    path = tmp_path / "portfolio.json"
    seen = _stub_report(monkeypatch)

    _run(monkeypatch, path, ["show"])

    assert [(c, p) for c, p, _ in seen] == [("USD", {})]


# --- print_user_portfolio ---------------------------------------------------


def _stats(**overrides) -> HoldingsStats:
    base = dict(
        currency="USD",
        positions={"SPY": 1000.0, "T": 500.0},
        weights={"SPY": 0.8, "T": 0.2},
        market_values={"SPY": 80_000.0, "T": 20_000.0},
        expected_returns={"SPY": 0.1225, "T": 0.0810},
        volatility={"SPY": 0.1481, "T": 0.2033},
        total_value=100_000.0,
        annual_return=0.1234,
        annual_volatility=0.1500,
        sharpe=0.6893,
        risk_free_rate=0.02,
        window_start=date(2021, 10, 1),
        window_end=date(2026, 9, 1),
        window_months=60,
        excluded={},
        unavailable_reason=None,
    )
    return HoldingsStats(**{**base, **overrides})


def test_the_report_states_the_window_and_the_rate_the_figures_came_from(capsys):
    print_user_portfolio(_stats(), "memory/portfolio.json")
    out = capsys.readouterr().out

    assert "Your portfolio (USD), from memory/portfolio.json:" in out
    assert "Returns window: 2021-10-01 to 2026-09-01 (60 month(s) of monthly returns)" in out
    assert "Annual return: 0.1234  Annual volatility: 0.1500  Sharpe: 0.6893" in out
    assert "Risk-free rate used: 0.0200" in out
    assert "Total value: $100,000.00 USD" in out


def test_holdings_are_listed_heaviest_first_with_value_and_weight(capsys):
    print_user_portfolio(_stats(), "memory/portfolio.json")
    lines = [line for line in capsys.readouterr().out.splitlines() if "shares" in line]

    assert lines[0] == "  SPY: 1,000 shares  $80,000.00 USD  weight 0.8000"
    assert lines[1] == "  T: 500 shares  $20,000.00 USD  weight 0.2000"


def test_each_holdings_own_annualized_return_and_volatility_are_reported(capsys):
    """The same section, wording and position `print_weights_and_allocation`
    gives an optimized pool, so the two blocks of one report read against
    each other line for line.
    """
    print_user_portfolio(_stats(), "memory/portfolio.json")
    out = capsys.readouterr().out

    assert "Expected return / volatility (annualized):" in out
    assert "  SPY: return=0.1225  volatility=0.1481" in out
    assert "  T: return=0.0810  volatility=0.2033" in out


def test_the_per_holding_figures_follow_the_same_order_as_the_positions(capsys):
    print_user_portfolio(_stats(), "memory/portfolio.json")
    figures = [line for line in capsys.readouterr().out.splitlines() if "return=" in line]

    assert figures[0].startswith("  SPY:")
    assert figures[1].startswith("  T:")


def test_an_excluded_holding_gets_no_per_holding_figure_line(capsys):
    """It has no estimate to print - the same reason
    `print_weights_and_allocation` covers only the weighted tickers.
    """
    print_user_portfolio(
        _stats(
            positions={"SPY": 1000.0, "NEWCO": 40.0},
            weights={"SPY": 1.0},
            market_values={"SPY": 98_000.0, "NEWCO": 2_000.0},
            expected_returns={"SPY": 0.1225},
            volatility={"SPY": 0.1481},
            excluded={"NEWCO": "8 month(s) of monthly returns in the window, under 24"},
        ),
        "memory/portfolio.json",
    )
    figures = [line for line in capsys.readouterr().out.splitlines() if "return=" in line]

    assert figures == ["  SPY: return=0.1225  volatility=0.1481"]


def test_an_excluded_holding_is_named_with_its_reason_and_value_share(capsys):
    print_user_portfolio(
        _stats(
            positions={"SPY": 1000.0, "NEWCO": 40.0},
            weights={"SPY": 1.0},
            market_values={"SPY": 98_000.0, "NEWCO": 2_000.0},
            expected_returns={"SPY": 0.1225},
            volatility={"SPY": 0.1481},
            total_value=100_000.0,
            excluded={"NEWCO": "8 month(s) of monthly returns in the window, under 24"},
        ),
        "memory/portfolio.json",
    )
    out = capsys.readouterr().out

    assert "  NEWCO: 40 shares  $2,000.00 USD  weight n/a" in out
    assert "Excluded from the figures:" in out
    assert "  NEWCO: 8 month(s) of monthly returns in the window, under 24 (2.0% of total value)" in out


def test_an_empty_portfolio_prints_one_line_carrying_the_reason(capsys):
    print_user_portfolio(
        unavailable_holdings("USD", {}, 0.02, "no holdings are saved for USD; add some"),
        "memory/portfolio.json",
    )
    out = capsys.readouterr().out

    assert out.strip() == "Your portfolio (USD): n/a - no holdings are saved for USD; add some"


def test_a_portfolio_with_no_figures_still_lists_what_is_held(capsys):
    print_user_portfolio(
        unavailable_holdings(
            "USD", {"NEWCO": 40.0}, 0.02, "fetching was disabled with --no-holdings-fetch"
        ),
        "memory/portfolio.json",
    )
    out = capsys.readouterr().out

    assert "  NEWCO: 40 shares  value n/a  weight n/a" in out
    assert "Figures: n/a - fetching was disabled with --no-holdings-fetch" in out


def test_a_fractional_share_count_is_not_padded_with_false_precision():
    assert format_share_count(1000.0) == "1,000"
    assert format_share_count(1234567.0) == "1,234,567"
    assert format_share_count(0.5) == "0.5"
    assert format_share_count(10.25) == "10.25"


# --- the per-currency risk-free rate ---------------------------------------


def test_a_rate_given_on_the_command_line_is_remembered_and_announced(monkeypatch, tmp_path, capsys):
    path = tmp_path / "portfolio.json"
    _stub_ingest(monkeypatch, {"1321.T": "JPY"})
    _stub_report(monkeypatch)
    _run(monkeypatch, path, ["set", "1321.T", "50"])

    _run(monkeypatch, path, ["--currency", "JPY", "--risk-free-rate", "0.005", "show"])

    assert load_risk_free_rate(str(tmp_path / "rates.json"), "JPY") == 0.005
    assert "Remembered 0.0050 as the JPY risk-free rate" in capsys.readouterr().out


def test_a_remembered_rate_applies_with_no_flag_on_a_later_run(monkeypatch, tmp_path):
    path = tmp_path / "portfolio.json"
    _stub_ingest(monkeypatch, {"1321.T": "JPY"})
    _stub_report(monkeypatch)
    _run(monkeypatch, path, ["set", "1321.T", "50"])
    _run(monkeypatch, path, ["--currency", "JPY", "--risk-free-rate", "0.005", "show"])

    seen = _stub_report(monkeypatch)
    _run(monkeypatch, path, ["--currency", "JPY", "show"])

    assert [rate for _, _, rate in seen] == [0.005]


def test_each_currency_is_measured_against_its_own_remembered_rate_in_one_show(
    monkeypatch, tmp_path
):
    """The reported bug: before this, one `show` applied the 2% dollar
    default to a yen portfolio and a dollar portfolio alike.
    """
    path = tmp_path / "portfolio.json"
    _stub_ingest(monkeypatch, {"SPY": "USD", "1321.T": "JPY"})
    _stub_report(monkeypatch)
    _run(monkeypatch, path, ["set", "SPY", "1000"])
    _run(monkeypatch, path, ["set", "1321.T", "50"])
    _run(monkeypatch, path, ["--currency", "JPY", "--risk-free-rate", "0.005", "show"])
    _run(monkeypatch, path, ["--currency", "USD", "--risk-free-rate", "0.0425", "show"])

    seen = _stub_report(monkeypatch)
    _run(monkeypatch, path, ["show"])

    assert [(currency, rate) for currency, _, rate in seen] == [("JPY", 0.005), ("USD", 0.0425)]


def test_a_rate_never_leaks_from_one_currency_into_another(monkeypatch, tmp_path):
    path = tmp_path / "portfolio.json"
    _stub_ingest(monkeypatch, {"SPY": "USD", "1321.T": "JPY"})
    _stub_report(monkeypatch)
    _run(monkeypatch, path, ["set", "SPY", "1000"])
    _run(monkeypatch, path, ["set", "1321.T", "50"])
    _run(monkeypatch, path, ["--currency", "JPY", "--risk-free-rate", "0.005", "show"])

    seen = _stub_report(monkeypatch)
    _run(monkeypatch, path, ["--currency", "USD", "show"])

    assert [rate for _, _, rate in seen] == [pytest.approx(settings.risk_free_rate)]


def test_a_negative_rate_is_remembered_for_a_yen_portfolio(monkeypatch, tmp_path):
    path = tmp_path / "portfolio.json"
    _stub_ingest(monkeypatch, {"1321.T": "JPY"})
    _stub_report(monkeypatch)
    _run(monkeypatch, path, ["set", "1321.T", "50"])

    _run(monkeypatch, path, ["--currency", "JPY", "--risk-free-rate", "-0.001", "show"])

    assert load_risk_free_rate(str(tmp_path / "rates.json"), "JPY") == -0.001


def test_a_set_remembers_the_rate_for_the_currency_the_tickers_landed_in(monkeypatch, tmp_path):
    """`set` needs no --currency: the tickers determine it."""
    path = tmp_path / "portfolio.json"
    _stub_ingest(monkeypatch, {"1321.T": "JPY"})
    _stub_report(monkeypatch)

    _run(monkeypatch, path, ["set", "1321.T", "50", "--risk-free-rate", "0.005"])

    assert load_risk_free_rate(str(tmp_path / "rates.json"), "JPY") == 0.005


def test_a_set_that_saved_nothing_remembers_no_rate(monkeypatch, tmp_path):
    path = tmp_path / "portfolio.json"
    _stub_ingest(monkeypatch, {})
    _stub_report(monkeypatch)

    _run(monkeypatch, path, ["set", "NOTATICKER", "10", "--risk-free-rate", "0.04"])

    assert not (tmp_path / "rates.json").exists()


def test_a_rate_on_a_show_spanning_two_currencies_is_refused_as_ambiguous(
    monkeypatch, tmp_path, capsys
):
    path = tmp_path / "portfolio.json"
    _stub_ingest(monkeypatch, {"SPY": "USD", "1321.T": "JPY"})
    _stub_report(monkeypatch)
    _run(monkeypatch, path, ["set", "SPY", "1000"])
    _run(monkeypatch, path, ["set", "1321.T", "50"])

    with pytest.raises(SystemExit) as excinfo:
        _run(monkeypatch, path, ["--risk-free-rate", "0.03", "show"])

    assert excinfo.value.code == 2
    assert "--currency" in capsys.readouterr().err
    assert not (tmp_path / "rates.json").exists()


def test_a_rate_on_a_show_with_exactly_one_saved_currency_needs_no_flag(monkeypatch, tmp_path):
    path = tmp_path / "portfolio.json"
    _stub_ingest(monkeypatch, {"SPY": "USD"})
    _stub_report(monkeypatch)
    _run(monkeypatch, path, ["set", "SPY", "1000"])

    _run(monkeypatch, path, ["--risk-free-rate", "0.0425", "show"])

    assert load_all_risk_free_rates(str(tmp_path / "rates.json")) == {"USD": 0.0425}


def test_a_rate_with_nothing_saved_at_all_is_refused_rather_than_inventing_usd(
    monkeypatch, tmp_path
):
    """`_run_show` reports USD there only so the "add some holdings" hint
    prints; that fallback is not a determination worth writing down.
    """
    path = tmp_path / "portfolio.json"
    _stub_report(monkeypatch)

    with pytest.raises(SystemExit):
        _run(monkeypatch, path, ["--risk-free-rate", "0.0425", "show"])

    assert not (tmp_path / "rates.json").exists()


def test_a_mistyped_percentage_rate_is_refused_before_any_ticker_lookup(
    monkeypatch, tmp_path, capsys
):
    """4.5 meaning 4.5% must not be remembered - it would make every later
    MSR run fail inside PyPortfolioOpt naming neither the rate nor the file.
    """
    path = tmp_path / "portfolio.json"

    def _fail_if_called(tickers, as_of, db_path):
        raise AssertionError("validated the tickers before refusing the rate")

    monkeypatch.setattr("src.flow.holdings_cli.validate_and_ingest_tickers", _fail_if_called)
    _stub_report(monkeypatch)

    with pytest.raises(SystemExit) as excinfo:
        _run(monkeypatch, path, ["set", "SPY", "1000", "--risk-free-rate", "4.5"])

    assert excinfo.value.code == 2
    assert "4.5% is 0.045" in capsys.readouterr().err
    assert not (tmp_path / "rates.json").exists()


def test_a_nan_rate_is_refused(monkeypatch, tmp_path, capsys):
    path = tmp_path / "portfolio.json"
    _stub_report(monkeypatch)

    with pytest.raises(SystemExit):
        _run(monkeypatch, path, ["--currency", "USD", "--risk-free-rate", "nan", "show"])

    assert "finite" in capsys.readouterr().err


def test_a_malformed_rates_file_exits_two_rather_than_printing_a_traceback(
    monkeypatch, tmp_path, capsys
):
    path = tmp_path / "portfolio.json"
    rates_path = tmp_path / "rates.json"
    rates_path.write_text("{not json")
    _stub_report(monkeypatch)

    with pytest.raises(SystemExit) as excinfo:
        _run(monkeypatch, path, ["--currency", "USD", "show"])

    assert excinfo.value.code == 2
    assert "JSON" in capsys.readouterr().err


def test_re_remembering_the_same_rate_announces_nothing(monkeypatch, tmp_path, capsys):
    path = tmp_path / "portfolio.json"
    _stub_ingest(monkeypatch, {"SPY": "USD"})
    _stub_report(monkeypatch)
    _run(monkeypatch, path, ["set", "SPY", "1000"])
    _run(monkeypatch, path, ["--currency", "USD", "--risk-free-rate", "0.0425", "show"])

    capsys.readouterr()
    _run(monkeypatch, path, ["--currency", "USD", "--risk-free-rate", "0.0425", "show"])

    assert "Remembered" not in capsys.readouterr().out


# --- the provenance line ---------------------------------------------------


def test_the_report_names_where_the_rate_came_from(capsys):
    print_user_portfolio(_stats(), "memory/portfolio.json", "remembered for USD")
    assert "Risk-free rate used: 0.0200 (remembered for USD)" in capsys.readouterr().out


def test_the_report_states_the_configured_default_as_a_source_too(capsys):
    """A bare number is what the bug looked like; every rate names a source."""
    print_user_portfolio(_stats(), "memory/portfolio.json", "the configured default")
    assert "Risk-free rate used: 0.0200 (the configured default)" in capsys.readouterr().out


def test_the_rate_and_its_origin_print_even_when_there_are_no_figures(capsys):
    """The reader of an n/a block is the one most likely to be working out
    why the numbers moved.
    """
    print_user_portfolio(
        unavailable_holdings("JPY", {"1321.T": 50.0}, 0.005, "fetching was disabled"),
        "memory/portfolio.json",
        "remembered for JPY",
    )
    out = capsys.readouterr().out

    assert "Figures: n/a - fetching was disabled" in out
    assert "Risk-free rate used: 0.0050 (remembered for JPY)" in out
