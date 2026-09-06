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

import pytest

from src.flow.holdings_cli import _normalize_ticker, _parse_pairs, main
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
        seen.append((currency, dict(positions)))
        return unavailable_holdings(currency, dict(positions), risk_free_rate, "stubbed")

    monkeypatch.setattr("src.flow.holdings_cli.prepare_holdings", fake)
    return seen


def _run(monkeypatch, path, argv: list[str]) -> None:
    monkeypatch.setattr("sys.argv", ["portfolio-holdings", *argv, "--path", str(path)])
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
    assert seen == [("USD", {"SPY": 1000.0, "T": 500.0})]
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

    assert [currency for currency, _ in seen] == ["JPY", "USD"]


def test_show_with_nothing_saved_still_reports_usd_so_the_hint_is_printed(monkeypatch, tmp_path):
    path = tmp_path / "portfolio.json"
    seen = _stub_report(monkeypatch)

    _run(monkeypatch, path, ["show"])

    assert seen == [("USD", {})]


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
