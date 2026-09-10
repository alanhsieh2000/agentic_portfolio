"""Tests for `src/dataset/ticker_profile.py`.

Per `AGENTS.md` no test here calls Yahoo Finance. The fixtures below are
literal excerpts of real responses captured live from yfinance 1.5.2 on
2026-09-10 (AMLP for the fund path, AVB for the company-share path), trimmed
to the keys this module reads plus the ones it must be proven NOT to read.

The unit traps these tests exist to pin are documented in
`src/dataset/ticker_profile.py`'s docstring: `info` reports the same expense
ratio as `1.01` that `fundProfile` reports as `0.0101`, the same YTD return
as `24.96513` that `fundPerformance` reports as `0.2496513`, and a
`dividendYield` whose unit differs between tickers. Every one of those keys
is present in `_AMLP_INFO` on purpose, so that a future change reading them
by mistake fails a test instead of printing a figure a hundred times too
large.
"""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock

import pytest

from src.dataset.ticker_profile import (
    INFO_FETCH_FAILED_REASON,
    RawTickerProfile,
    build_ticker_profile,
    fetch_ticker_profile,
)

_AMLP_INFO = {
    "symbol": "AMLP",
    "longName": "Alerian MLP ETF",
    "shortName": "Alerian MLP ETF",
    "quoteType": "ETF",
    "currency": "USD",
    "fiftyTwoWeekLow": 44.64,
    "fiftyTwoWeekHigh": 56.29,
    "totalAssets": 13442306048,
    "netAssets": 13442306000.0,
    # Every key below is a unit trap this module must never read.
    "netExpenseRatio": 1.01,
    "ytdReturn": 24.96513,
    "threeYearAverageReturn": 0.1993137,
    "fiveYearAverageReturn": 0.2053602,
    "yield": 0.0733,
    "dividendYield": 7.33,
}

_AMLP_FUND_PROFILE = {
    "family": "ALPS",
    "categoryName": "Energy Limited Partnership",
    "legalType": "Exchange Traded Fund",
    "managementInfo": {"managerName": None, "managerBio": None},
    "feesExpensesInvestment": {
        "annualReportExpenseRatio": 0.0101,
        "annualHoldingsTurnover": 0.14,
        "totalNetAssets": 20566.84,
        "projectionValues": {},
    },
    "feesExpensesInvestmentCat": {
        "annualReportExpenseRatio": 0.0156968,
        "annualHoldingsTurnover": 0.3614,
        "totalNetAssets": 20566.84,
        "projectionValuesCat": {},
    },
    "brokerages": [],
}

_AMLP_FUND_PERFORMANCE = {
    "fundCategoryName": "Energy Limited Partnership",
    "trailingReturns": {
        "asOfDate": 1788825600,
        "ytd": 0.2496513,
        "oneMonth": 0.0241871,
        "threeMonth": 0.09676699,
        "oneYear": 0.23550029,
        "threeYear": 0.19673571,
        "fiveYear": 0.2069134,
        "tenYear": 0.073727995,
        "lastBullMkt": 0.0,
        "lastBearMkt": 0.0,
    },
    "trailingReturnsCat": {
        "ytd": 0.23837629,
        "oneMonth": 0.0277838,
        "threeMonth": 0.15287751,
        "oneYear": 0.3075745,
        "threeYear": 0.2577587,
        "fiveYear": 0.2278815,
        "tenYear": 0.1023464,
    },
    "riskOverviewStatistics": {
        "riskStatistics": [
            {
                "year": "5y",
                "alpha": 12.84,
                "beta": 0.5,
                "meanAnnualReturn": 1.71,
                "rSquared": 17.46,
                "stdDev": 17.86,
                "sharpeRatio": 0.93,
                "treynorRatio": 33.51,
            },
            {
                "year": "3y",
                "alpha": 10.44,
                "beta": 0.27,
                "meanAnnualReturn": 1.58,
                "rSquared": 5.82,
                "stdDev": 13.7,
                "sharpeRatio": 1.05,
                "treynorRatio": 56.28,
            },
            {
                "year": "10y",
                "alpha": -3.38,
                "beta": 1.21,
                "meanAnnualReturn": 0.98,
                "rSquared": 36.2,
                "stdDev": 29.5,
                "sharpeRatio": 0.32,
                "treynorRatio": 4.02,
            },
        ]
    },
}

_AVB_INFO = {
    "symbol": "AVB",
    "longName": "AvalonBay Communities Inc",
    "shortName": "AvalonBay Communities, Inc.",
    "quoteType": "EQUITY",
    "currency": "USD",
    "sector": "Real Estate",
    "industry": "REIT - Residential",
    "marketCap": 26283393024,
    "trailingPE": 25.248285,
    "forwardPE": 36.584446,
    "beta": 0.773,
    "fiftyTwoWeekLow": 57.320084,
    "fiftyTwoWeekHigh": 185.62,
    "dividendYield": 0.0387,
}

# The reason string `_fetch_fund_modules` records for AVB, whose fund request
# really does answer HTTP 404 - measured live, and the ordinary answer for a
# security that is not a fund.
_AVB_FUND_REASON = "Yahoo Finance served no fund data for this ticker (HTTPError)"


def _amlp_raw() -> RawTickerProfile:
    return RawTickerProfile(
        info=dict(_AMLP_INFO),
        fund_profile=dict(_AMLP_FUND_PROFILE),
        fund_performance=dict(_AMLP_FUND_PERFORMANCE),
    )


def _avb_raw() -> RawTickerProfile:
    return RawTickerProfile(
        info=dict(_AVB_INFO),
        fund_profile=None,
        fund_performance=None,
        fund_data_reason=_AVB_FUND_REASON,
    )


def test_build_ticker_profile_reads_the_identity_fields_from_info():
    profile = build_ticker_profile("AMLP", _amlp_raw())

    assert profile.ticker == "AMLP"
    assert profile.long_name == "Alerian MLP ETF"
    assert profile.quote_type == "ETF"
    assert profile.currency == "USD"
    assert profile.fifty_two_week_low == 44.64
    assert profile.fifty_two_week_high == 56.29
    assert profile.unavailable_reason is None
    assert profile.fund_data_reason is None


def test_build_ticker_profile_reads_the_fund_fields_from_fund_profile():
    profile = build_ticker_profile("AMLP", _amlp_raw())

    assert profile.category_name == "Energy Limited Partnership"
    assert profile.family == "ALPS"
    assert profile.legal_type == "Exchange Traded Fund"
    assert profile.holdings_turnover == pytest.approx(0.14)


def test_the_expense_ratio_is_the_fractional_one_and_not_infos_percentage():
    """`fundProfile` says 0.0101 where `info["netExpenseRatio"]` says 1.01.

    Reading the wrong one would print an expense ratio a hundred times too
    large beside a correctly-fractional category average, so this is pinned
    against the exact live values rather than an approximation.
    """
    profile = build_ticker_profile("AMLP", _amlp_raw())

    assert profile.expense_ratio == pytest.approx(0.0101)
    assert profile.expense_ratio_category == pytest.approx(0.0156968)
    assert profile.expense_ratio != _AMLP_INFO["netExpenseRatio"]


def test_net_assets_come_from_info_and_not_from_the_category_wide_fund_profile_figure():
    """`fundProfile.feesExpensesInvestment.totalNetAssets` is the CATEGORY's
    net assets, not the fund's - proven live by its being bit-identical to
    the `...Cat` copy of the same field while the fund's real net assets are
    a different number entirely. This fixture keeps that discrepancy, so
    reading the wrong field fails here.
    """
    profile = build_ticker_profile("AMLP", _amlp_raw())

    assert profile.net_assets == 13442306048
    assert profile.net_assets != _AMLP_FUND_PROFILE["feesExpensesInvestment"]["totalNetAssets"]


def test_trailing_returns_are_fractions_in_printing_order_beside_the_categorys():
    profile = build_ticker_profile("AMLP", _amlp_raw())

    assert list(profile.trailing_returns) == ["YTD", "1M", "3M", "1Y", "3Y", "5Y", "10Y"]
    assert profile.trailing_returns["YTD"] == pytest.approx(0.2496513)
    assert profile.trailing_returns["10Y"] == pytest.approx(0.073727995)
    assert profile.trailing_returns_category["YTD"] == pytest.approx(0.23837629)
    assert profile.trailing_returns_as_of == date(2026, 9, 8)
    # `info["ytdReturn"]` is the same figure as a percentage; taking it would
    # put 24.96 in a row of fractions.
    assert profile.trailing_returns["YTD"] != _AMLP_INFO["ytdReturn"]


def test_a_period_yahoo_omits_is_absent_rather_than_none():
    """A fund too young for a ten-year figure must print one fewer column,
    not `10Y n/a` inside a row of numbers."""
    performance = dict(_AMLP_FUND_PERFORMANCE)
    trailing = dict(performance["trailingReturns"])
    trailing.pop("tenYear")
    trailing["fiveYear"] = None
    performance["trailingReturns"] = trailing
    raw = _amlp_raw()._replace(fund_performance=performance)

    profile = build_ticker_profile("AMLP", raw)

    assert "10Y" not in profile.trailing_returns
    assert "5Y" not in profile.trailing_returns
    assert "3Y" in profile.trailing_returns


def test_the_bull_and_bear_market_periods_are_never_reported():
    """Yahoo reports both as 0.0 for every ticker measured, and a period
    whose boundaries are undocumented cannot be compared with anything."""
    profile = build_ticker_profile("AMLP", _amlp_raw())

    assert set(profile.trailing_returns) == {"YTD", "1M", "3M", "1Y", "3Y", "5Y", "10Y"}


def test_risk_statistics_take_the_three_year_row_with_percentages_divided_by_100():
    """`alpha` and `stdDev` arrive as percentages while `beta` and
    `sharpeRatio` are plain. A `13.70` standard deviation printed three lines
    above this project's own `0.1893` volatility would invite a hundredfold
    misreading, so both are converted here.
    """
    profile = build_ticker_profile("AMLP", _amlp_raw())

    assert profile.risk_statistics == {
        "alpha": pytest.approx(0.1044),
        "beta": pytest.approx(0.27),
        "stdDev": pytest.approx(0.137),
        "sharpeRatio": pytest.approx(1.05),
    }


def test_risk_statistics_drop_the_monthly_mean_and_the_compactness_casualties():
    """`meanAnnualReturn` is 1.58 for a three-year period whose total return
    is 0.1967 - a MONTHLY mean despite its name - so it is dropped rather
    than printed under a wrong label. `rSquared` and `treynorRatio` are
    dropped to keep the block compact.
    """
    profile = build_ticker_profile("AMLP", _amlp_raw())

    assert "meanAnnualReturn" not in profile.risk_statistics
    assert "rSquared" not in profile.risk_statistics
    assert "treynorRatio" not in profile.risk_statistics


def test_a_company_share_gets_the_equity_fields_and_a_named_fund_data_reason():
    profile = build_ticker_profile("AVB", _avb_raw())

    assert profile.long_name == "AvalonBay Communities Inc"
    assert profile.quote_type == "EQUITY"
    assert profile.sector == "Real Estate"
    assert profile.industry == "REIT - Residential"
    assert profile.market_cap == 26283393024
    assert profile.trailing_pe == pytest.approx(25.248285)
    assert profile.forward_pe == pytest.approx(36.584446)
    assert profile.beta == pytest.approx(0.773)
    assert profile.unavailable_reason is None

    assert profile.category_name is None
    assert profile.expense_ratio is None
    assert profile.net_assets is None
    assert profile.trailing_returns is None
    assert profile.risk_statistics is None

    assert profile.fund_data_reason == (
        "AVB is not a fund (quoteType EQUITY), so Yahoo publishes no category "
        "comparison or fund risk statistics for it"
    )


def test_no_yield_is_ever_read_from_yahoo():
    """`info["dividendYield"]` is a percentage for AMLP, SPY and MSFT but a
    fraction for AVB. Nothing in a `TickerProfile` may come from it, or from
    `info["yield"]`; the summary's yield is computed by
    `src/optimizer/ticker_stats.py` from dividend history this project
    downloaded itself.
    """
    for ticker, raw in (("AMLP", _amlp_raw()), ("AVB", _avb_raw())):
        profile = build_ticker_profile(ticker, raw)
        values = [v for v in profile if isinstance(v, float)]
        for forbidden in (7.33, 0.0733, 0.0387):
            assert forbidden not in values, f"{ticker} profile carries a Yahoo yield"


def test_a_fund_whose_modules_failed_keeps_everything_else_and_reports_the_reason():
    """The two requests are independent on purpose: a fund whose
    `quoteSummary` call was rate-limited must still be described by name,
    quote type and 52-week range.
    """
    raw = _amlp_raw()._replace(
        fund_profile=None,
        fund_performance=None,
        fund_data_reason="Yahoo Finance served no fund data for this ticker (YFRateLimitError)",
    )

    profile = build_ticker_profile("AMLP", raw)

    assert profile.long_name == "Alerian MLP ETF"
    assert profile.quote_type == "ETF"
    assert profile.fifty_two_week_high == 56.29
    assert profile.expense_ratio is None
    assert profile.fund_data_reason == (
        "Yahoo Finance served no fund data for this ticker (YFRateLimitError)"
    )


def test_an_unusable_info_response_yields_a_profile_carrying_only_a_reason():
    raw = RawTickerProfile(info={}, fund_profile=None, fund_performance=None)

    profile = build_ticker_profile("ZZZZQQQ", raw)

    assert profile.ticker == "ZZZZQQQ"
    assert profile.unavailable_reason == INFO_FETCH_FAILED_REASON
    assert profile.long_name is None
    assert profile.quote_type is None
    assert profile.expense_ratio is None


def test_a_symbol_yahoo_does_not_know_is_unavailable_not_partially_populated():
    """Asked about `ZZZZQQQ`, `Ticker.info` really returns
    `{'trailingPegRatio': None}` - measured live. Treating that non-empty
    dict as a populated profile printed a header reading
    `ZZZZQQQ - (name unavailable)` above a lone fund-data reason, which reads
    like a partial success rather than "Yahoo has never heard of this
    symbol".
    """
    raw = RawTickerProfile(
        info={"trailingPegRatio": None},
        fund_profile=None,
        fund_performance=None,
        fund_data_reason="Yahoo Finance served no fund data for this ticker (HTTPError)",
    )

    profile = build_ticker_profile("ZZZZQQQ", raw)

    assert profile.unavailable_reason == INFO_FETCH_FAILED_REASON
    assert profile.long_name is None
    assert profile.quote_type is None


def test_a_quote_type_alone_is_enough_to_count_as_recognized():
    """The test is "did Yahoo recognize the symbol", not "is the profile
    complete" - an obscure but real symbol may carry little else."""
    raw = RawTickerProfile(
        info={"quoteType": "EQUITY"}, fund_profile=None, fund_performance=None
    )

    profile = build_ticker_profile("OBSCURE", raw)

    assert profile.unavailable_reason is None
    assert profile.quote_type == "EQUITY"


def test_a_stripped_down_payload_yields_nones_rather_than_raising():
    """`AGENTS.md`: treat Yahoo responses as unstable input. A payload where
    every value has turned null, or become an empty object, or become a
    string, must produce missing figures rather than a traceback at an
    interactive prompt.
    """
    raw = RawTickerProfile(
        info={"quoteType": "ETF", "longName": None, "fiftyTwoWeekLow": "n/a", "totalAssets": {}},
        fund_profile={"categoryName": "", "feesExpensesInvestment": None},
        fund_performance={"trailingReturns": None, "riskOverviewStatistics": []},
    )

    profile = build_ticker_profile("WEIRD", raw)

    assert profile.quote_type == "ETF"
    assert profile.long_name is None
    assert profile.fifty_two_week_low is None
    assert profile.net_assets is None
    assert profile.category_name is None
    assert profile.expense_ratio is None
    assert profile.trailing_returns is None
    assert profile.risk_statistics is None
    assert profile.unavailable_reason is None


def test_fetch_ticker_profile_translates_dotted_share_class_symbols(monkeypatch):
    """`BRK.B` must be asked about as `BRK-B` - the same translation the
    price fetch applies - so the description belongs to the very series that
    was priced."""
    ticker_spy = MagicMock(return_value=MagicMock(info={"quoteType": "EQUITY"}))
    monkeypatch.setattr("src.dataset.ticker_profile.yf.Ticker", ticker_spy)
    monkeypatch.setattr(
        "src.dataset.ticker_profile._fetch_fund_modules", lambda symbol: (None, None, "no")
    )

    raw = fetch_ticker_profile("BRK.B", pause_seconds=0.0)

    assert ticker_spy.call_args.args[0] == "BRK-B"
    assert raw.info == {"quoteType": "EQUITY"}
    assert raw.info_reason is None


def test_fetch_ticker_profile_never_raises_when_the_info_request_fails(monkeypatch):
    def boom(symbol):
        raise RuntimeError("rate limited")

    monkeypatch.setattr("src.dataset.ticker_profile.yf.Ticker", boom)
    monkeypatch.setattr(
        "src.dataset.ticker_profile._fetch_fund_modules", lambda symbol: (None, None, None)
    )

    raw = fetch_ticker_profile("AMLP", pause_seconds=0.0)

    assert raw.info == {}
    assert raw.info_reason == INFO_FETCH_FAILED_REASON


def test_fetch_ticker_profile_still_reads_info_when_the_fund_request_fails(monkeypatch):
    """Every company share fails the fund request, so one failing must never
    cost the other."""
    monkeypatch.setattr(
        "src.dataset.ticker_profile.yf.Ticker",
        lambda symbol: MagicMock(info=dict(_AVB_INFO)),
    )

    raw = fetch_ticker_profile("AVB", pause_seconds=0.0)

    assert raw.info["longName"] == "AvalonBay Communities Inc"
    assert raw.fund_profile is None
    assert raw.fund_performance is None
    assert raw.fund_data_reason is not None


def test_the_fund_module_fetch_reports_a_reason_rather_than_raising(monkeypatch):
    """`_fetch_fund_modules` imports the non-public `yfinance.data.YfData`
    inside itself, so even that module disappearing is just another reason
    string - it must never break the profile's identity fields."""
    import src.dataset.ticker_profile as module

    def boom(*args, **kwargs):
        raise ImportError("no module named yfinance.data")

    monkeypatch.setattr("builtins.__import__", boom)
    fund_profile, fund_performance, reason = module._fetch_fund_modules("AMLP")

    assert fund_profile is None
    assert fund_performance is None
    assert "ImportError" in reason
