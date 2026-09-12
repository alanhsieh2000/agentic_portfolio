"""Tests for src/agentic_portfolio/flow/holdings_cli.py (`uv run portfolio-holdings`) and
src/agentic_portfolio/flow/cli.py's `print_user_portfolio`: recording and retiring holdings,
the per-currency routing that decides which portfolio an edit lands in, and
the exact wording of the report block both commands share.

Per AGENTS.md's testing guidance these are hermetic. Yahoo Finance is
reached only through `validate_and_ingest_tickers` and the risk figures
only through `prepare_holdings`, both monkeypatched on
`agentic_portfolio.flow.holdings_cli`'s own symbols, so nothing here touches the network
or the real `memory/portfolio.json`.
"""

from contextlib import contextmanager
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agentic_portfolio.config.settings import settings
from agentic_portfolio.flow.holdings_cli import (
    NOT_SAVED,
    WHATIF_PROMPT,
    _normalize_ticker,
    _parse_pairs,
    main,
)
from agentic_portfolio.flow.interactive import HoldingsSession
from agentic_portfolio.flow.report_archive import load_report
from agentic_portfolio.flow.rate_memory import load_all_risk_free_rates, load_risk_free_rate
from agentic_portfolio.flow.user_portfolio import load_all_portfolios, load_portfolio
from agentic_portfolio.optimizer.holdings import HoldingsStats, unavailable_holdings
from agentic_portfolio.flow.cli import (
    format_holdings_delta,
    format_holdings_dividend_total,
    format_share_count,
    print_user_portfolio,
)
from agentic_portfolio.optimizer.dividends import dividend_figures


def _stub_ingest(monkeypatch, currencies: dict[str, str], invalid: dict[str, str] | None = None):
    """Stand in for `validate_and_ingest_tickers`, resolving exactly the
    tickers in `currencies` and reporting the rest as not found.
    """
    invalid = invalid or {}

    def fake(tickers, as_of, db_path):
        valid = sorted(t for t in tickers if t in currencies)
        missing = {t: "not found on Yahoo Finance" for t in tickers if t not in currencies}
        return valid, {**missing, **invalid}, {t: currencies[t] for t in valid}

    monkeypatch.setattr("agentic_portfolio.flow.holdings_cli.validate_and_ingest_tickers", fake)
    # Also on the cache module's own reference. Since the holdings cache
    # landed, `whatif`'s add path goes through `refresh_holdings_cache`,
    # which holds its own import - so patching only `holdings_cli`'s left
    # the real function running, reaching Yahoo Finance from the suite and
    # creating a DuckDB file in the repository root. Worse than failing: a
    # test asserting that `7203.T` is refused as JPY passed anyway, because
    # the live lookup agreed with the stub it had bypassed.
    monkeypatch.setattr("agentic_portfolio.dataset.holdings_cache.validate_and_ingest_tickers", fake)


def _stub_report(monkeypatch) -> list[tuple[str, dict]]:
    """Replace `prepare_holdings` with a spy, so a CLI test asserts on what
    was saved and reported rather than on estimator arithmetic (covered by
    tests/test_holdings.py). Returns the list it records into.
    """
    seen: list[tuple[str, dict]] = []

    def fake(positions, currency, rebalance_date, db_path, risk_free_rate=0.02, **kwargs):
        seen.append((currency, dict(positions), risk_free_rate))
        return unavailable_holdings(currency, dict(positions), risk_free_rate, "stubbed")

    monkeypatch.setattr("agentic_portfolio.flow.holdings_cli.prepare_holdings", fake)
    return seen


def _run(monkeypatch, path, argv: list[str], rates_path=None) -> None:
    """Drive `main()` with both memory files pointed inside `tmp_path`.

    `--rates-path` and `--holdings-cache-path` are not optional politeness:
    an argparse default is bound at parse time, so monkeypatching
    `DEFAULT_RATES_PATH` or `DEFAULT_HOLDINGS_CACHE_PATH` would not take
    effect. Without the flags these tests would READ and, for any run
    passing `--risk-free-rate`, WRITE the developer's real
    `memory/rates.json` - green on a clean checkout, failing on a machine
    that has ever remembered a rate, and invisible in `git status` because
    `memory/` is gitignored. `data/holdings.duckdb` is the same hazard with
    a worse failure mode: a test that fetched into it would reach Yahoo
    Finance from the suite.
    """
    rates_path = rates_path or Path(path).with_name("rates.json")
    cache_path = Path(path).with_name("holdings.duckdb")
    monkeypatch.setattr(
        "sys.argv",
        [
            "portfolio-holdings", *argv,
            "--path", str(path),
            "--rates-path", str(rates_path),
            "--holdings-cache-path", str(cache_path),
        ],
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
        priced_as_of=date(2026, 9, 4),
        excluded={},
        unavailable_reason=None,
    )
    return HoldingsStats(**{**base, **overrides})


def test_the_dividend_total_names_why_a_holding_is_missing_from_it():
    """The income line already shrinks its denominator and names the gap.
    The reason is what tells the holder whether the gap is fixable: a
    transient miss clears on `--refresh-holdings`, while a window the
    source no longer serves means this figure will never be complete.
    """
    reason = "yfinance returned no dividend column for 2022-12-28..2026-09-04; transient"
    figures = dividend_figures(
        {"SPY": 1000.0, "BOXX": 500.0},
        {"SPY": 80_000.0, "BOXX": 20_000.0},
        {"SPY": 1.60},
        {"BOXX": reason},
        {"SPY": 0.02},
    )
    line = format_holdings_dividend_total(_stats(dividends=figures), "USD")
    assert "on $80,000.00 USD of the $100,000.00 USD total" in line
    assert "BOXX has no trailing dividend data" in line
    # Below the figure, never inside its parenthetical - the reasons name
    # dates and ranges and would push the figure line off the terminal.
    assert line.endswith(f"\n  BOXX: {reason}")


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

    monkeypatch.setattr("agentic_portfolio.flow.holdings_cli.validate_and_ingest_tickers", _fail_if_called)
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


# --- whatif: the never-saved explore loop ----------------------------------


def _script(monkeypatch, *responses: str) -> None:
    """Feed `responses` to successive `input()` calls. An unexpected prompt
    raises `StopIteration`, which is how "did not prompt" gets asserted.
    """
    remaining = iter(responses)
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(remaining))


def _stub_session(
    monkeypatch,
    measured: list[dict] | None = None,
    can_ingest: bool = True,
    windows: list[int] | None = None,
):
    """Replace the what-if session and its measurement with spies, so a CLI
    test asserts on the loop's behaviour rather than on estimator arithmetic
    (covered by tests/test_holdings.py). Returns the list of measured
    position dicts, in order; pass `windows` to also capture the
    `lookback_months` each measurement used.
    """
    seen = measured if measured is not None else []
    seen_windows = windows if windows is not None else []

    @contextmanager
    def fake_session(tickers, rebalance_date, db_path, allow_fetch=True, **kwargs):
        yield HoldingsSession(db_path="session.duckdb", can_ingest=can_ingest)

    def fake_measure(
        positions,
        currency,
        rebalance_date,
        session,
        risk_free_rate=0.02,
        currencies=None,
        lookback_months=60,
    ):
        seen.append(dict(positions))
        seen_windows.append(lookback_months)
        # The window shows through to the report, so a test can assert on the
        # printed line as well as on what was measured.
        return _stats(
            currency=currency,
            positions=dict(positions),
            risk_free_rate=risk_free_rate,
            window_months=lookback_months,
        )

    monkeypatch.setattr("agentic_portfolio.flow.holdings_cli.open_holdings_session", fake_session)
    monkeypatch.setattr("agentic_portfolio.flow.holdings_cli.measure_holdings", fake_measure)
    return seen


def _no_writes(monkeypatch) -> tuple[MagicMock, MagicMock]:
    """Spies on both writes a whatif must never make. Returned rather than
    asserted here so each test can name which one it cares about.
    """
    save_positions = MagicMock()
    save_rate = MagicMock(return_value=True)
    monkeypatch.setattr("agentic_portfolio.flow.holdings_cli.save_portfolio", save_positions)
    monkeypatch.setattr("agentic_portfolio.flow.holdings_cli.save_risk_free_rate", save_rate)
    return save_positions, save_rate


def test_whatif_saves_nothing_at_all(monkeypatch, tmp_path, capsys):
    """The feature's whole promise. Asserted on the write functions rather
    than on the file, because a file comparison would also pass if the write
    happened but wrote identical bytes.
    """
    path = tmp_path / "portfolio.json"
    _stub_ingest(monkeypatch, {"SPY": "USD", "NVDA": "USD"})
    _stub_report(monkeypatch)
    _run(monkeypatch, path, ["set", "SPY", "1000"])
    before = path.read_bytes()

    save_positions, save_rate = _no_writes(monkeypatch)
    _stub_session(monkeypatch)
    _script(monkeypatch, "s", "NVDA 100", "f")
    _run(monkeypatch, path, ["whatif"])

    assert save_positions.called is False
    assert save_rate.called is False
    assert path.read_bytes() == before
    assert "Nothing was saved" in capsys.readouterr().out


def test_whatif_reports_the_baseline_then_the_hypothetical(monkeypatch, tmp_path):
    path = tmp_path / "portfolio.json"
    _stub_ingest(monkeypatch, {"SPY": "USD", "NVDA": "USD"})
    _stub_report(monkeypatch)
    _run(monkeypatch, path, ["set", "SPY", "1000"])

    _no_writes(monkeypatch)
    measured = _stub_session(monkeypatch)
    _script(monkeypatch, "s", "NVDA 100", "f")
    _run(monkeypatch, path, ["whatif"])

    assert measured == [{"SPY": 1000.0}, {"SPY": 1000.0, "NVDA": 100.0}]


def test_whatif_labels_the_hypothetical_block_as_not_saved(monkeypatch, tmp_path, capsys):
    """Naming the file in that header would be a plain falsehood - the
    hypothetical holdings are not in it and never will be.
    """
    path = tmp_path / "portfolio.json"
    _stub_ingest(monkeypatch, {"SPY": "USD", "NVDA": "USD"})
    _stub_report(monkeypatch)
    _run(monkeypatch, path, ["set", "SPY", "1000"])

    _no_writes(monkeypatch)
    _stub_session(monkeypatch)
    _script(monkeypatch, "s", "NVDA 100", "f")
    _run(monkeypatch, path, ["whatif"])

    out = capsys.readouterr().out
    assert "What if (USD) - not saved:" in out
    assert "Your portfolio (USD), from" in out


def test_whatif_hands_over_the_set_command_that_would_apply_it(monkeypatch, tmp_path, capsys):
    path = tmp_path / "portfolio.json"
    _stub_ingest(monkeypatch, {"SPY": "USD", "NVDA": "USD"})
    _stub_report(monkeypatch)
    _run(monkeypatch, path, ["set", "SPY", "1000"])

    _no_writes(monkeypatch)
    _stub_session(monkeypatch)
    _script(monkeypatch, "s", "NVDA 100", "f")
    _run(monkeypatch, path, ["whatif"])

    assert "To keep it: uv run portfolio-holdings set NVDA 100" in capsys.readouterr().out


def test_whatif_offers_no_command_when_nothing_was_changed(monkeypatch, tmp_path, capsys):
    path = tmp_path / "portfolio.json"
    _stub_ingest(monkeypatch, {"SPY": "USD"})
    _stub_report(monkeypatch)
    _run(monkeypatch, path, ["set", "SPY", "1000"])

    _no_writes(monkeypatch)
    _stub_session(monkeypatch)
    _script(monkeypatch, "f")
    _run(monkeypatch, path, ["whatif"])

    out = capsys.readouterr().out
    assert "Nothing was saved" in out
    assert "To keep it:" not in out


def test_whatif_spells_a_retirement_as_zero_in_the_set_command(monkeypatch, tmp_path, capsys):
    path = tmp_path / "portfolio.json"
    _stub_ingest(monkeypatch, {"SPY": "USD", "T": "USD"})
    _stub_report(monkeypatch)
    _run(monkeypatch, path, ["set", "SPY", "1000", "T", "500"])

    _no_writes(monkeypatch)
    _stub_session(monkeypatch)
    _script(monkeypatch, "r", "T", "f")
    _run(monkeypatch, path, ["whatif"])

    assert "To keep it: uv run portfolio-holdings set T 0" in capsys.readouterr().out


def test_whatif_undo_all_returns_to_the_saved_holdings(monkeypatch, tmp_path, capsys):
    path = tmp_path / "portfolio.json"
    _stub_ingest(monkeypatch, {"SPY": "USD", "NVDA": "USD"})
    _stub_report(monkeypatch)
    _run(monkeypatch, path, ["set", "SPY", "1000"])

    _no_writes(monkeypatch)
    _stub_session(monkeypatch)
    _script(monkeypatch, "s", "NVDA 100", "u", "f")
    _run(monkeypatch, path, ["whatif"])

    out = capsys.readouterr().out
    assert "Back to your saved holdings." in out
    assert "To keep it:" not in out
    # Reprinted as the baseline, not as a "what if" with zero deltas: after
    # an undo these are the real holdings again, so the label must say so.
    assert out.rstrip().endswith(NOT_SAVED)
    assert out.count("What if (USD) - not saved:") == 1  # only the NVDA variant


def test_whatif_refuses_a_cross_currency_ticker_by_name(monkeypatch, tmp_path, capsys):
    """`set`'s wording, because the point of a what-if is predicting what
    `set` would do - and because a typo deserves naming, not quiet exclusion
    from the figures.
    """
    path = tmp_path / "portfolio.json"
    _stub_ingest(monkeypatch, {"SPY": "USD"})
    _stub_report(monkeypatch)
    _run(monkeypatch, path, ["set", "SPY", "1000"])

    _no_writes(monkeypatch)
    _stub_ingest(monkeypatch, {"7203.T": "JPY"})
    measured = _stub_session(monkeypatch)
    _script(monkeypatch, "s", "7203.T 100", "f")
    _run(monkeypatch, path, ["whatif"])

    assert "Refused: 7203.T is priced in JPY" in capsys.readouterr().out
    assert measured == [{"SPY": 1000.0}]


def test_whatif_reports_an_unresolvable_ticker_and_carries_on(monkeypatch, tmp_path, capsys):
    path = tmp_path / "portfolio.json"
    _stub_ingest(monkeypatch, {"SPY": "USD"})
    _stub_report(monkeypatch)
    _run(monkeypatch, path, ["set", "SPY", "1000"])

    _no_writes(monkeypatch)
    _stub_ingest(monkeypatch, {})
    _stub_session(monkeypatch)
    _script(monkeypatch, "s", "NOTATICKER 5", "f")
    _run(monkeypatch, path, ["whatif"])

    assert "Ignored (not found): NOTATICKER." in capsys.readouterr().out


def test_whatif_refuses_a_new_ticker_when_fetching_is_disabled(monkeypatch, tmp_path, capsys):
    """`--db-path` is never written to, so a session over it cannot ingest -
    refusing by name beats silently writing to the shared cache.
    """
    path = tmp_path / "portfolio.json"
    _stub_ingest(monkeypatch, {"SPY": "USD"})
    _stub_report(monkeypatch)
    _run(monkeypatch, path, ["set", "SPY", "1000"])

    _no_writes(monkeypatch)

    def _fail_if_called(tickers, as_of, db_path):
        raise AssertionError("ingested into the shared cache under --no-fetch")

    monkeypatch.setattr("agentic_portfolio.flow.holdings_cli.validate_and_ingest_tickers", _fail_if_called)
    _stub_session(monkeypatch, can_ingest=False)
    _script(monkeypatch, "s", "NVDA 100", "f")
    _run(monkeypatch, path, ["whatif", "--no-fetch"])

    assert "would need a price fetch" in capsys.readouterr().out


def test_whatif_keeps_what_you_had_on_an_unparseable_pair(monkeypatch, tmp_path, capsys):
    path = tmp_path / "portfolio.json"
    _stub_ingest(monkeypatch, {"SPY": "USD"})
    _stub_report(monkeypatch)
    _run(monkeypatch, path, ["set", "SPY", "1000"])

    _no_writes(monkeypatch)
    measured = _stub_session(monkeypatch)
    _script(monkeypatch, "s", "NVDA", "f")
    _run(monkeypatch, path, ["whatif"])

    assert "Ignoring that:" in capsys.readouterr().out
    assert measured == [{"SPY": 1000.0}]


def test_whatif_reprompts_on_an_unrecognized_choice(monkeypatch, tmp_path, capsys):
    path = tmp_path / "portfolio.json"
    _stub_ingest(monkeypatch, {"SPY": "USD"})
    _stub_report(monkeypatch)
    _run(monkeypatch, path, ["set", "SPY", "1000"])

    _no_writes(monkeypatch)
    _stub_session(monkeypatch)
    _script(monkeypatch, "q", "f")
    _run(monkeypatch, path, ["whatif"])

    assert "Unrecognized choice 'q'." in capsys.readouterr().out


def test_whatif_applies_the_risk_free_rate_without_remembering_it(monkeypatch, tmp_path):
    """The write most likely to slip through: --risk-free-rate is remembered
    by every other subcommand, and must not be here.
    """
    path = tmp_path / "portfolio.json"
    _stub_ingest(monkeypatch, {"SPY": "USD"})
    _stub_report(monkeypatch)
    _run(monkeypatch, path, ["set", "SPY", "1000"])

    _save_positions, save_rate = _no_writes(monkeypatch)
    _stub_session(monkeypatch)
    _script(monkeypatch, "f")
    _run(monkeypatch, path, ["whatif", "--risk-free-rate", "0.05"])

    assert save_rate.called is False
    assert not load_all_risk_free_rates(str(tmp_path / "rates.json"))


def test_whatif_refuses_when_it_cannot_tell_which_portfolio_to_try(monkeypatch, tmp_path, capsys):
    path = tmp_path / "portfolio.json"
    _stub_ingest(monkeypatch, {"SPY": "USD", "1321.T": "JPY"})
    _stub_report(monkeypatch)
    _run(monkeypatch, path, ["set", "SPY", "1000"])
    _run(monkeypatch, path, ["set", "1321.T", "50"])

    _no_writes(monkeypatch)
    _stub_session(monkeypatch)
    _script(monkeypatch)

    with pytest.raises(SystemExit) as excinfo:
        _run(monkeypatch, path, ["whatif"])

    assert excinfo.value.code == 2
    assert "--currency" in capsys.readouterr().err


# --- format_holdings_delta -------------------------------------------------


def test_the_delta_is_the_signed_change_in_the_three_figures():
    baseline = _stats(annual_return=0.1225, annual_volatility=0.1481, sharpe=0.5400)
    hypothetical = _stats(annual_return=0.2281, annual_volatility=0.1904, sharpe=0.9744)

    line = format_holdings_delta(baseline, hypothetical)

    assert line == (
        "Change from your saved portfolio: "
        "return +0.1056  volatility +0.0423  Sharpe +0.4344"
    )


def test_a_worsening_change_is_signed_negative():
    baseline = _stats(sharpe=0.9744)
    hypothetical = _stats(sharpe=0.5400)

    assert "Sharpe -0.4344" in format_holdings_delta(baseline, hypothetical)


def test_the_delta_is_withheld_when_the_windows_differ():
    """Subtracting two Sharpe ratios measured over different months would
    report the change of window as a change of portfolio.
    """
    baseline = _stats()
    hypothetical = _stats(window_start=date(2022, 10, 1), window_months=48)

    line = format_holdings_delta(baseline, hypothetical)

    assert "n/a - measured over different windows" in line
    assert "2022-10-01" in line


def test_the_delta_is_withheld_when_either_side_has_no_figures():
    baseline = _stats()
    hypothetical = unavailable_holdings("USD", {"NEWCO": 1.0}, 0.02, "too thin")

    assert "no figures to compare" in format_holdings_delta(baseline, hypothetical)


def test_whatif_does_not_claim_the_rate_was_remembered(monkeypatch, tmp_path, capsys):
    """Every other subcommand phrases an overridden rate as "remembered for
    USD". Printing that here would claim a write this command exists not to
    make - the provenance line's whole job is to be true.
    """
    path = tmp_path / "portfolio.json"
    _stub_ingest(monkeypatch, {"SPY": "USD"})
    _stub_report(monkeypatch)
    _run(monkeypatch, path, ["set", "SPY", "1000"])

    _no_writes(monkeypatch)
    _stub_session(monkeypatch)
    _script(monkeypatch, "f")
    _run(monkeypatch, path, ["whatif", "--risk-free-rate", "0.05"])

    out = capsys.readouterr().out
    assert "not remembered - this is a what-if" in out
    assert "remembered for USD" not in out


def test_the_whatif_loop_prompt_says_a_ticker_need_not_be_one_you_own():
    """The capability existed from Milestone 5 and was invisible: the loop
    said "[s]et shares", which reads as adjusting a quantity on something
    you already have, and the only in-program sentence admitting otherwise
    was inside the --no-holdings-fetch refusal - visible only to somebody
    who had already tried it AND passed the flag that blocks it. The
    repository owner reasonably concluded the feature was missing.

    Pinned on the prompt strings rather than on prose, because the next
    person to tidy them for length would re-introduce exactly that
    confusion.
    """
    assert "any ticker" in WHATIF_PROMPT


def test_the_set_sub_prompt_says_the_ticker_need_not_be_held(monkeypatch, tmp_path, capsys):
    path = tmp_path / "portfolio.json"
    _stub_ingest(monkeypatch, {"SPY": "USD"})
    _stub_report(monkeypatch)
    _run(monkeypatch, path, ["set", "SPY", "1000"])

    _no_writes(monkeypatch)
    _stub_session(monkeypatch)
    prompts: list[str] = []
    remaining = iter(["s", "SPY 900", "f"])

    def record(prompt=""):
        prompts.append(prompt)
        return next(remaining)

    monkeypatch.setattr("builtins.input", record)
    _run(monkeypatch, path, ["whatif"])

    assert any("held or not" in p for p in prompts)
    assert any("any ticker" in p for p in prompts)


# --- whatif's [w]indow verb ------------------------------------------------


def _whatif_setup(monkeypatch, tmp_path, windows=None, measured=None):
    """A saved one-holding USD portfolio plus the whatif stubs, since every
    window test needs the same three lines first.
    """
    path = tmp_path / "portfolio.json"
    _stub_ingest(monkeypatch, {"SPY": "USD", "NVDA": "USD"})
    _stub_report(monkeypatch)
    _run(monkeypatch, path, ["set", "SPY", "1000"])
    _no_writes(monkeypatch)
    _stub_session(monkeypatch, measured=measured, windows=windows)
    return path


def test_the_whatif_prompt_offers_the_window(monkeypatch, tmp_path):
    assert "[w]indow" in WHATIF_PROMPT


def test_choosing_a_window_measures_over_it(monkeypatch, tmp_path):
    windows: list[int] = []
    path = _whatif_setup(monkeypatch, tmp_path, windows=windows)
    _script(monkeypatch, "w", "36", "f")

    _run(monkeypatch, path, ["whatif"])

    # Baseline at the default, then the baseline re-measured at 36.
    assert windows == [60, 36]


def test_a_window_change_re_measures_the_baseline_so_the_delta_still_prints(
    monkeypatch, tmp_path, capsys
):
    """`format_holdings_delta` withholds a delta across differing windows -
    correctly, since the difference would then be partly the months rather
    than the holdings. So a window change has to move BOTH sides, or every
    later comparison comes back as an apology. This asserts on the symptom a
    naive implementation would show.
    """
    path = _whatif_setup(monkeypatch, tmp_path)
    _script(monkeypatch, "w", "36", "s", "NVDA 100", "f")

    _run(monkeypatch, path, ["whatif"])
    out = capsys.readouterr().out

    assert "measured over different windows" not in out
    assert "Change from your saved portfolio: return " in out


def test_the_report_names_the_window_that_was_asked_for(monkeypatch, tmp_path, capsys):
    path = _whatif_setup(monkeypatch, tmp_path)
    _script(monkeypatch, "w", "36", "f")

    _run(monkeypatch, path, ["whatif"])

    assert "36 month(s) of monthly returns, 36 requested" in capsys.readouterr().out


def test_the_default_window_is_not_annotated(monkeypatch, tmp_path, capsys):
    """A run that chose nothing should read exactly as it did before the
    window became selectable - the annotation is for a choice, not decoration.
    """
    path = _whatif_setup(monkeypatch, tmp_path)
    _script(monkeypatch, "f")

    _run(monkeypatch, path, ["whatif"])
    out = capsys.readouterr().out

    assert "60 month(s) of monthly returns)" in out
    assert "requested" not in out


def test_a_window_outside_the_range_is_refused_and_the_previous_one_kept(
    monkeypatch, tmp_path, capsys
):
    windows: list[int] = []
    path = _whatif_setup(monkeypatch, tmp_path, windows=windows)
    _script(monkeypatch, "w", "12", "f")

    _run(monkeypatch, path, ["whatif"])
    out = capsys.readouterr().out

    assert "Keeping 60 months" in out
    assert "between 24 and 60" in out
    assert windows == [60]  # never re-measured


def test_a_non_numeric_window_keeps_the_previous_one(monkeypatch, tmp_path, capsys):
    windows: list[int] = []
    path = _whatif_setup(monkeypatch, tmp_path, windows=windows)
    _script(monkeypatch, "w", "abc", "f")

    _run(monkeypatch, path, ["whatif"])
    out = capsys.readouterr().out

    assert "Keeping 60 months" in out
    # The validator's wording, not `int`'s "invalid literal for int() with
    # base 10", which says nothing about what a window may be.
    assert "whole number of months" in out
    assert "invalid literal" not in out
    assert windows == [60]


def test_a_blank_window_answer_keeps_the_previous_one(monkeypatch, tmp_path):
    windows: list[int] = []
    path = _whatif_setup(monkeypatch, tmp_path, windows=windows)
    _script(monkeypatch, "w", "", "f")

    _run(monkeypatch, path, ["whatif"])

    assert windows == [60]


def test_re_choosing_the_same_window_does_not_re_measure(monkeypatch, tmp_path):
    windows: list[int] = []
    path = _whatif_setup(monkeypatch, tmp_path, windows=windows)
    _script(monkeypatch, "w", "60", "f")

    _run(monkeypatch, path, ["whatif"])

    assert windows == [60]


def test_a_window_change_and_a_holding_change_compose(monkeypatch, tmp_path):
    """The actual use case: measure a different portfolio over a different
    window, in one session.
    """
    windows: list[int] = []
    measured: list[dict] = []
    path = _whatif_setup(monkeypatch, tmp_path, windows=windows, measured=measured)
    _script(monkeypatch, "w", "36", "s", "NVDA 100", "f")

    _run(monkeypatch, path, ["whatif"])

    assert windows == [60, 36, 36]
    assert measured[-1] == {"SPY": 1000.0, "NVDA": 100.0}


def test_the_window_survives_an_undo_all(monkeypatch, tmp_path):
    """`[u]ndo all` is about holdings, not the window - it says "back to your
    saved holdings", and silently resetting the window too would be a
    different promise.
    """
    windows: list[int] = []
    path = _whatif_setup(monkeypatch, tmp_path, windows=windows)
    _script(monkeypatch, "w", "36", "s", "NVDA 100", "u", "f")

    _run(monkeypatch, path, ["whatif"])

    assert windows[-1] == 36


# ==========================================================================
# Archiving each whatif report under output/<month>/
#
# The archive itself is tested in tests/test_report_archive.py. What is
# tested here is only what `_run_whatif` is responsible for: that the
# baseline and each variant are stored, that the never-save promise about the
# PORTFOLIO still holds while they are, and that --no-save-reports restores
# writing nothing whatsoever.
# ==========================================================================


def _whatif_archive_dir(tmp_path) -> Path:
    """The month folder a whatif run archives into. `--date` defaults to
    `today`, so the month is this month."""
    return tmp_path / "reports" / date.today().strftime("%Y-%m")


def _whatif_argv(tmp_path, *extra: str) -> list[str]:
    return ["whatif", "--output-dir", str(tmp_path / "reports"), *extra]


def _archived_whatif(tmp_path) -> list[Path]:
    month = _whatif_archive_dir(tmp_path)
    return sorted(month.iterdir()) if month.exists() else []


def test_whatif_archives_the_baseline_and_each_variant(monkeypatch, tmp_path, capsys):
    path = tmp_path / "portfolio.json"
    _stub_ingest(monkeypatch, {"SPY": "USD", "NVDA": "USD"})
    _stub_report(monkeypatch)
    _run(monkeypatch, path, ["set", "SPY", "1000"])

    _no_writes(monkeypatch)
    _stub_session(monkeypatch)
    _script(monkeypatch, "s", "NVDA 100", "f")
    _run(monkeypatch, path, _whatif_argv(tmp_path))

    out = capsys.readouterr().out
    saved = _archived_whatif(tmp_path)

    assert len(saved) == 2
    variants = [load_report(p)[0]["variant"] for p in saved]
    assert sorted(variants) == ["baseline", "what-if"]
    assert out.count("Saved report: ") == 2


def test_whatif_still_saves_nothing_about_the_portfolio_while_archiving(
    monkeypatch, tmp_path, capsys
):
    """The promise the command is built on, re-asserted now that it writes a
    file: state is what changes a later run, and a report changes none.
    """
    path = tmp_path / "portfolio.json"
    rates = tmp_path / "rates.json"
    _stub_ingest(monkeypatch, {"SPY": "USD", "NVDA": "USD"})
    _stub_report(monkeypatch)
    _run(monkeypatch, path, ["set", "SPY", "1000"])
    before_portfolio = path.read_bytes()
    before_rates = rates.read_bytes() if rates.exists() else None

    save_positions, save_rate = _no_writes(monkeypatch)
    _stub_session(monkeypatch)
    _script(monkeypatch, "s", "NVDA 100", "f")
    _run(monkeypatch, path, _whatif_argv(tmp_path))

    assert save_positions.called is False
    assert save_rate.called is False
    assert path.read_bytes() == before_portfolio
    assert (rates.read_bytes() if rates.exists() else None) == before_rates
    # And the sentence is left exactly as it was: it speaks about the
    # portfolio, and about the portfolio it is still true.
    assert "Nothing was saved" in capsys.readouterr().out
    assert _archived_whatif(tmp_path) != []


def test_whatif_undo_all_adds_no_new_report(monkeypatch, tmp_path):
    """Back at the saved holdings is the baseline again, and the baseline is
    already stored - which is what the digest recognizes."""
    path = tmp_path / "portfolio.json"
    _stub_ingest(monkeypatch, {"SPY": "USD", "NVDA": "USD"})
    _stub_report(monkeypatch)
    _run(monkeypatch, path, ["set", "SPY", "1000"])

    _no_writes(monkeypatch)
    _stub_session(monkeypatch)
    _script(monkeypatch, "s", "NVDA 100", "u", "f")
    _run(monkeypatch, path, _whatif_argv(tmp_path))

    # Baseline, the hypothetical, and the baseline again - two files.
    assert len(_archived_whatif(tmp_path)) == 2


def test_whatif_no_save_reports_writes_nothing(monkeypatch, tmp_path, capsys):
    path = tmp_path / "portfolio.json"
    _stub_ingest(monkeypatch, {"SPY": "USD", "NVDA": "USD"})
    _stub_report(monkeypatch)
    _run(monkeypatch, path, ["set", "SPY", "1000"])

    _no_writes(monkeypatch)
    _stub_session(monkeypatch)
    _script(monkeypatch, "s", "NVDA 100", "f")
    _run(monkeypatch, path, _whatif_argv(tmp_path, "--no-save-reports"))

    assert "Saved report:" not in capsys.readouterr().out
    assert not (tmp_path / "reports").exists()


def test_an_archived_whatif_report_records_the_positions_and_the_figures(
    monkeypatch, tmp_path
):
    """What a later side-by-side comparison reads instead of parsing prose."""
    path = tmp_path / "portfolio.json"
    _stub_ingest(monkeypatch, {"SPY": "USD", "NVDA": "USD"})
    _stub_report(monkeypatch)
    _run(monkeypatch, path, ["set", "SPY", "1000"])

    _no_writes(monkeypatch)
    _stub_session(monkeypatch)
    _script(monkeypatch, "s", "NVDA 100", "f")
    _run(monkeypatch, path, _whatif_argv(tmp_path))

    variant = next(
        p for p in _archived_whatif(tmp_path) if load_report(p)[0]["variant"] == "what-if"
    )
    facts, body = load_report(variant)

    assert facts["kind"] == "whatif"
    assert facts["currency"] == "USD"
    assert facts["positions"] == "NVDA:100, SPY:1000"
    assert facts["annual_return"] == "0.1234"
    assert facts["annual_volatility"] == "0.1500"
    assert facts["sharpe"] == "0.6893"
    assert facts["total_value"] == "100000.00"
    assert facts["priced_as_of"] == "2026-09-04"
    assert facts["command"].startswith("portfolio-holdings whatif")
    # The delta lines are part of what the reader compares, so they are part
    # of what is stored.
    assert "What if (USD) - not saved" in body


def test_a_whatif_report_without_figures_records_no_figures(monkeypatch, tmp_path):
    """`HoldingsStats` is all-or-nothing, so an unmeasurable portfolio
    contributes no figure lines rather than zeroes."""
    path = tmp_path / "portfolio.json"
    _stub_ingest(monkeypatch, {"SPY": "USD", "NVDA": "USD"})
    _stub_report(monkeypatch)
    _run(monkeypatch, path, ["set", "SPY", "1000"])

    _no_writes(monkeypatch)

    @contextmanager
    def fake_session(*_args, **_kwargs):
        yield HoldingsSession(db_path="session.duckdb", can_ingest=True)

    monkeypatch.setattr("agentic_portfolio.flow.holdings_cli.open_holdings_session", fake_session)
    monkeypatch.setattr(
        "agentic_portfolio.flow.holdings_cli.measure_holdings",
        lambda positions, currency, *a, **k: unavailable_holdings(
            currency, dict(positions), 0.02, "not enough history"
        ),
    )
    _script(monkeypatch, "f")
    _run(monkeypatch, path, _whatif_argv(tmp_path))

    (saved,) = _archived_whatif(tmp_path)
    facts, _body = load_report(saved)

    assert "sharpe" not in facts
    assert "annual_return" not in facts
    assert facts["positions"] == "SPY:1000"
