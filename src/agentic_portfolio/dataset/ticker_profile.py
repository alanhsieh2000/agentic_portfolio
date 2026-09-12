"""What Yahoo Finance publishes *about* one ticker: its name, what kind of
security it is, and - for a fund - its category, its fees, its net assets,
its trailing total returns beside its category's, and its risk statistics.

This module exists because `--selection user_provided` used to answer a
successful add with one word, `Added: AMLP.`, which proves only that Yahoo
could return prices for the symbol. It says nothing about what the thing is
or what it costs to own. The figures gathered here are the "what is this"
half of the summary printed by `src/agentic_portfolio/flow/cli.py`'s `print_ticker_summary`;
the "what will it do to my portfolio" half is computed from this project's
own returns by `src/agentic_portfolio/optimizer/ticker_stats.py`.

`fetch_ticker_profile` is the only function here that performs network I/O,
mirroring `src/agentic_portfolio/dataset/ticker_currency.py`'s `fetch_ticker_currencies`
discipline, so it is the single seam tests monkeypatch. Everything else is
pure.

THE UNIT RULE, which is the most important thing in this module
-----------------------------------------------------------------
`Ticker.info`'s ratio fields are in inconsistent units, sometimes within one
dictionary and sometimes differing between tickers. Measured live against
yfinance 1.5.2 on 2026-09-10, in a single session:

    AMLP info["netExpenseRatio"]        = 1.01        <- percent
    AMLP fundProfile expense ratio      = 0.0101      <- fraction, same number
    AMLP info["ytdReturn"]              = 24.96513    <- percent
    AMLP fundPerformance trailing ytd   = 0.2496513   <- fraction, same number
    AMLP info["threeYearAverageReturn"] = 0.1993137   <- fraction, same dict

and worse for yields, where the unit differs between tickers:

    AMLP dividendYield = 7.33    SPY dividendYield = 0.98
    MSFT dividendYield = 0.74    AVB dividendYield = 0.0387   <- a fraction

So the rule this module follows, and which must not be relaxed into "check
each field's unit" (a rule no future contributor can verify by reading the
code): from `info` read ONLY names, quote type, dates, absolute currency
amounts, and plain multiples (`beta`, `trailingPE`, `forwardPE`). Take every
RATIO from `fundProfile`/`fundPerformance`, which are consistently
fractional. And never read a yield from Yahoo at all - this project already
computes a trailing dividend yield from dividend history it downloads
itself (`src/agentic_portfolio/optimizer/dividends.py`), which is additionally the same yield
the optimizer's dividend floor uses, so the summary and the constraint
agree.

Two further traps, both measured:

- `fundProfile.feesExpensesInvestment.totalNetAssets` is NOT the fund's net
  assets. It reads 20566.84 for AMLP and the `...Cat` copy of the field
  reads the identical 20566.84, i.e. it carries the category-level figure,
  while AMLP's real net assets are `info["totalAssets"] = 13442306048`. Net
  assets therefore come from `totalAssets` with `netAssets` as the fallback
  - the pairing `src/agentic_portfolio/agents/external_screen.py`'s `_auto_fetch_etf_aum`
  documents as verified live across nine ETFs.

- Yahoo's risk-statistics row mixes units and misnames a field: `alpha`,
  `stdDev` and `rSquared` are percentages while `beta` and `sharpeRatio`
  are plain, and `meanAnnualReturn` is 1.58 for a 3-year period whose total
  return is 0.1967 - a MONTHLY mean despite its name. So `alpha` and
  `stdDev` are divided by 100 here to match every other ratio this project
  prints, and `meanAnnualReturn`, `rSquared` and `treynorRatio` are dropped
  rather than printed under a wrong label or padded into an already long
  block.

WHY THE FUND DATA COMES FROM A NON-PUBLIC yfinance MODULE
---------------------------------------------------------
yfinance's public `Ticker` exposes no fund-performance accessor: `funds_data`
carries holdings, sectors, asset classes and fund operations but no returns
or risk statistics, and in 1.5.2 `Ticker` has no `_fetch` method either. The
only working route is `yfinance.data.YfData`, a non-public helper, against
Yahoo's `quoteSummary` endpoint - which does return both modules this needs
in a single request. That is accepted, but only as strictly OPTIONAL
enrichment: the import happens inside the function and any failure at all,
`ImportError` from a future yfinance included, degrades to a printable
reason while the rest of the profile still populates. An ordinary company
share answers that same URL with HTTP 404, which is the normal expected
answer for a non-fund rather than a fault.
"""

from __future__ import annotations

import logging
import time
from datetime import date, datetime, timezone
from typing import NamedTuple

import yfinance as yf

from agentic_portfolio.config.settings import settings
from agentic_portfolio.dataset.prices import to_yfinance_symbol

logger = logging.getLogger(__name__)

QUOTE_SUMMARY_URL = "https://query2.finance.yahoo.com/v10/finance/quoteSummary/{symbol}"
"""Yahoo's `quoteSummary` endpoint, which serves the fund modules.

Deliberately NOT in `src/agentic_portfolio/config/settings.py`: like
`src/agentic_portfolio/dataset/membership.py`'s `WIKIPEDIA_URL`, this is tied to an external
source's actual shape rather than being a value an operator tunes per
environment, and changing it requires code-level awareness of that shape.
"""

FUND_MODULES = "fundProfile,fundPerformance"
"""The two `quoteSummary` modules this module reads, requested together
because Yahoo serves both in one response and a second request would double
the cost of every summary for nothing.

`fundProfile` carries the category name, the fund family, the legal type and
the fee figures with their category averages. `fundPerformance` carries the
trailing total returns with their category counterparts and the risk
statistics.
"""

FUND_QUOTE_TYPES = frozenset({"ETF", "MUTUALFUND"})
"""Yahoo quote types for which the fund modules exist.

Used only to phrase the reason a non-fund has no fund figures. The code never
SKIPS the fund request based on this set, because Yahoo's classification is
the thing being described rather than something to be second-guessed - a
symbol it types unexpectedly still gets asked about, and a 404 answers just
as clearly as a prediction would have.
"""

TRAILING_RETURN_PERIODS: tuple[tuple[str, str], ...] = (
    ("YTD", "ytd"),
    ("1M", "oneMonth"),
    ("3M", "threeMonth"),
    ("1Y", "oneYear"),
    ("3Y", "threeYear"),
    ("5Y", "fiveYear"),
    ("10Y", "tenYear"),
)
"""Label to print, paired with Yahoo's key, in the order they are printed.

`lastBullMkt` and `lastBearMkt` are deliberately excluded: Yahoo reports
both as 0.0 for every ticker measured, and a period whose boundaries are
undocumented cannot be compared with anything.
"""

RISK_STATISTICS_PERIOD = "3y"
"""Which of Yahoo's risk-statistic rows to print.

Yahoo supplies `3y`, `5y` and `10y`. One row is printed rather than three to
keep the block compact, and the three-year row is chosen because it is the
one whose window most nearly overlaps this project's own 60-month default -
so the reader is comparing Yahoo's figures against ours over roughly
comparable spans rather than wholly different ones.
"""

PERCENT_RISK_STATISTICS = frozenset({"alpha", "stdDev"})
"""Risk-statistic keys Yahoo reports as percentages, divided by 100 here.

`beta` and `sharpeRatio` are plain numbers and are left alone. See this
module's docstring for why consistency of units across one screen matters
more than fidelity to Yahoo's own presentation.
"""

RISK_STATISTIC_KEYS: tuple[str, ...] = ("alpha", "beta", "stdDev", "sharpeRatio")
"""Which risk statistics are kept, in printing order.

`meanAnnualReturn` is excluded because it is monthly despite its name (see
this module's docstring); `rSquared` and `treynorRatio` are excluded to keep
the block compact, and neither is load-bearing for the decision a person is
making at the add prompt.
"""

INFO_FETCH_FAILED_REASON = (
    "could not read this ticker's profile from Yahoo Finance, so there is "
    "nothing to describe it with"
)

IDENTIFYING_INFO_KEYS: tuple[str, ...] = ("quoteType", "longName", "shortName")
"""Keys at least one of which a usable `info` response must carry.

A symbol Yahoo does not know does NOT come back as an empty dictionary. Asked
about `ZZZZQQQ`, `Ticker.info` returns `{'trailingPegRatio': None}` - a
non-empty dict carrying nothing that identifies anything. Treating that as a
populated profile printed a header reading `ZZZZQQQ - (name unavailable)`
above a lone fund-data reason, which looks like a partial success rather than
the "Yahoo has never heard of this symbol" it actually is. Requiring one
identifying key turns it back into a single `n/a` line with a reason.

`quoteType` alone is enough, and is what a real but obscure symbol would
have: this test is for "did Yahoo recognize the symbol at all", not "is the
profile complete".
"""


class RawTickerProfile(NamedTuple):
    """Yahoo's answers exactly as they arrived, before any normalization.

    Split from `TickerProfile` so that the network half and the
    unit-correcting half can be tested separately: `fetch_ticker_profile`
    is monkeypatched in every test above this layer, and
    `build_ticker_profile` is exercised against literal payloads captured
    from real responses.

    `info_reason` and `fund_data_reason` each hold one printable sentence
    when the corresponding request failed. They are independent on purpose:
    an ordinary company share always fails the fund request (HTTP 404) and
    must still get a full profile from `info`.
    """

    info: dict
    fund_profile: dict | None
    fund_performance: dict | None
    info_reason: str | None = None
    fund_data_reason: str | None = None


class TickerProfile(NamedTuple):
    """One ticker's published description, with every unit corrected.

    Follows the contract `BenchmarkStats` and `HoldingsStats` set: a GROUP
    of fields is either wholly populated or wholly `None` with a reason
    beside it, so the printing code never guards a half-filled state. There
    are three such groups here. The identity fields (`long_name` through
    `fifty_two_week_high`) are absent only when `unavailable_reason` is set,
    which means Yahoo said nothing usable about the symbol at all. The fund
    fields (`category_name` through `risk_statistics`) are absent when
    `fund_data_reason` is set, which is the ORDINARY case for a company
    share rather than a failure. The equity fields (`sector` through `beta`)
    are populated whenever `info` carried them, whatever the quote type,
    since they are the plain multiples the unit rule permits.

    `trailing_returns` and `trailing_returns_category` are ordered mappings
    of the printed period label to a fractional return, so a period Yahoo
    omitted is simply absent from both rather than present as a `None` the
    renderer must skip.
    """

    ticker: str
    long_name: str | None = None
    quote_type: str | None = None
    currency: str | None = None
    fifty_two_week_low: float | None = None
    fifty_two_week_high: float | None = None
    category_name: str | None = None
    family: str | None = None
    legal_type: str | None = None
    expense_ratio: float | None = None
    expense_ratio_category: float | None = None
    holdings_turnover: float | None = None
    net_assets: float | None = None
    trailing_returns: dict[str, float] | None = None
    trailing_returns_category: dict[str, float] | None = None
    trailing_returns_as_of: date | None = None
    risk_statistics: dict[str, float] | None = None
    sector: str | None = None
    industry: str | None = None
    market_cap: float | None = None
    trailing_pe: float | None = None
    forward_pe: float | None = None
    beta: float | None = None
    fund_data_reason: str | None = None
    unavailable_reason: str | None = None


def _fetch_fund_modules(symbol: str) -> tuple[dict | None, dict | None, str | None]:
    """`(fund_profile, fund_performance, reason)` from one `quoteSummary`
    request, or `(None, None, reason)` for anything that went wrong.

    `YfData` is imported HERE rather than at module scope deliberately. It is
    not part of yfinance's public API (see this module's docstring for why
    there is no public alternative), so a future yfinance that moves or
    removes it would otherwise break the import of this whole module - and
    with it the profile's identity and equity fields, which do not depend on
    it at all. Inside the function, that same removal is just another reason
    string.

    Never raises. A company share answers this URL with HTTP 404, which is
    the expected answer for a non-fund, so failure here is an ordinary
    outcome rather than an error.
    """
    try:
        from yfinance.data import YfData

        payload = YfData().get_raw_json(
            QUOTE_SUMMARY_URL.format(symbol=symbol),
            params={
                "modules": FUND_MODULES,
                "corsDomain": "finance.yahoo.com",
                "formatted": "false",
            },
        )
        results = (payload or {}).get("quoteSummary", {}).get("result") or []
        if not results:
            return None, None, "Yahoo Finance returned no fund modules for this ticker"
        result = results[0]
        return result.get("fundProfile"), result.get("fundPerformance"), None
    except Exception as e:  # noqa: BLE001 - yfinance and urllib raise assorted types here
        logger.info("fund module lookup failed for symbol %s: %s", symbol, e)
        return None, None, f"Yahoo Finance served no fund data for this ticker ({type(e).__name__})"


def fetch_ticker_profile(
    ticker: str, pause_seconds: float = settings.yfinance_fundamentals_pause_seconds
) -> RawTickerProfile:
    """Ask Yahoo Finance to describe `ticker`, returning its answers raw.

    The only function in this module that performs network I/O. At most two
    requests: `Ticker.info` for the identity, the absolute amounts and the
    plain multiples, then one `quoteSummary` request for both fund modules.
    Each is wrapped separately so one failing cannot cost the other - which
    matters constantly, since every company share fails the second one.

    Never raises, following `fetch_ticker_currencies`: a ticker Yahoo will
    not describe comes back carrying a reason, and the caller turns that into
    a printed `n/a` line.

    The yfinance symbol is derived with `to_yfinance_symbol`, the same
    translation the price fetch uses, so the description belongs to the very
    series that was priced (`BRK.B` is asked about as `BRK-B`, while `7203.T`
    passes through unchanged).

    `pause_seconds` separates the two requests, matching
    `fetch_ticker_currencies`' pacing. This is affordable because it runs
    for one ticker at a time at an interactive prompt, never across the
    ~500-ticker membership universe.
    """
    symbol = to_yfinance_symbol(ticker)

    info: dict = {}
    info_reason: str | None = None
    try:
        info = dict(yf.Ticker(symbol).info or {})
    except Exception as e:  # noqa: BLE001 - yfinance raises assorted types here
        logger.warning("profile lookup failed for %s (symbol %s): %s", ticker, symbol, e)
        info_reason = INFO_FETCH_FAILED_REASON
    if not info and info_reason is None:
        info_reason = INFO_FETCH_FAILED_REASON

    time.sleep(pause_seconds)
    fund_profile, fund_performance, fund_reason = _fetch_fund_modules(symbol)

    return RawTickerProfile(
        info=info,
        fund_profile=fund_profile,
        fund_performance=fund_performance,
        info_reason=info_reason,
        fund_data_reason=fund_reason,
    )


def _as_float(value: object) -> float | None:
    """`value` as a float, or `None` for anything that is not a finite
    number - which includes `None`, an empty dict (Yahoo's placeholder for
    an absent nested value) and a string.

    Every field read from a Yahoo payload goes through this rather than
    `float(...)`, because `AGENTS.md` requires treating those responses as
    unstable input: a key that has always been a number may arrive as null
    or as an empty object, and that must yield a missing figure rather than
    an exception at an interactive prompt.
    """
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if result != result or result in (float("inf"), float("-inf")):
        return None
    return result


def _as_text(value: object) -> str | None:
    """`value` as a non-empty stripped string, or `None`. Yahoo reports an
    absent string as `null` or as `''` interchangeably, and both mean the
    same thing to a reader."""
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _trailing_returns(block: object) -> dict[str, float]:
    """Yahoo's trailing-return block as `{printed label: fraction}`, in
    printing order, omitting any period Yahoo did not supply.

    Omission rather than a `None` entry is deliberate: the renderer prints
    whatever periods it is given, so a fund too young to have a ten-year
    figure simply prints one fewer column instead of printing `10Y n/a`
    inside a row of numbers.
    """
    if not isinstance(block, dict):
        return {}
    returns: dict[str, float] = {}
    for label, key in TRAILING_RETURN_PERIODS:
        value = _as_float(block.get(key))
        if value is not None:
            returns[label] = value
    return returns


def _risk_statistics(block: object) -> dict[str, float]:
    """The `RISK_STATISTICS_PERIOD` row of Yahoo's risk statistics, keeping
    only `RISK_STATISTIC_KEYS` and converting the percentage-valued ones to
    fractions.

    See this module's docstring: `alpha` and `stdDev` arrive as percentages
    while `beta` and `sharpeRatio` are plain, and printing a `13.70`
    standard deviation three lines above this project's own `0.1893`
    volatility would invite exactly the misreading the four-decimal-fraction
    convention exists to prevent.
    """
    if not isinstance(block, dict):
        return {}
    rows = block.get("riskStatistics")
    if not isinstance(rows, list):
        return {}
    for row in rows:
        if isinstance(row, dict) and row.get("year") == RISK_STATISTICS_PERIOD:
            stats: dict[str, float] = {}
            for key in RISK_STATISTIC_KEYS:
                value = _as_float(row.get(key))
                if value is None:
                    continue
                stats[key] = value / 100.0 if key in PERCENT_RISK_STATISTICS else value
            return stats
    return {}


def _as_of_date(block: object) -> date | None:
    """The `asOfDate` Unix timestamp inside a trailing-return block, as a
    UTC date.

    Yahoo stamps its own published figures with the date they were computed
    for, which is not the date this command is being run - and this project
    prints the window behind every figure, so that stamp must survive to the
    report rather than being replaced with today.
    """
    if not isinstance(block, dict):
        return None
    stamp = _as_float(block.get("asOfDate"))
    if stamp is None:
        return None
    try:
        return datetime.fromtimestamp(stamp, tz=timezone.utc).date()
    except (OverflowError, OSError, ValueError):
        return None


def _net_assets(info: dict) -> float | None:
    """The fund's own net assets, from `totalAssets` with `netAssets` as the
    fallback - NEVER from `fundProfile.feesExpensesInvestment.totalNetAssets`,
    which carries the category-level figure (see this module's docstring for
    the measurement proving it)."""
    for key in ("totalAssets", "netAssets"):
        value = _as_float(info.get(key))
        if value is not None:
            return value
    return None


def _no_fund_data_reason(ticker: str, quote_type: str | None, fetch_reason: str | None) -> str:
    """One printable sentence saying why there are no fund figures.

    A non-fund quote type gets a sentence naming it, because that is
    information rather than a fault: a person who typed a company share
    should learn that Yahoo publishes no category comparison for one, not
    read a network-error message. Anything else falls back to the reason the
    fetch itself recorded.
    """
    if quote_type and quote_type.upper() not in FUND_QUOTE_TYPES:
        return (
            f"{ticker} is not a fund (quoteType {quote_type}), so Yahoo publishes no "
            "category comparison or fund risk statistics for it"
        )
    return fetch_reason or "Yahoo Finance served no fund data for this ticker"


def build_ticker_profile(ticker: str, raw: RawTickerProfile) -> TickerProfile:
    """Normalize `raw` into a `TickerProfile`, applying this module's unit
    rule. Pure: no I/O, no logging, no shared state.

    Every field is absent-by-default. A Yahoo payload missing any key - or
    carrying a null, an empty object or a string where a number belonged -
    yields `None` for that field rather than raising, because this runs at
    an interactive prompt where a traceback would cost the person their
    in-progress candidate pool.
    """
    info = raw.info or {}
    if not any(info.get(key) for key in IDENTIFYING_INFO_KEYS):
        return TickerProfile(
            ticker=ticker,
            unavailable_reason=raw.info_reason or INFO_FETCH_FAILED_REASON,
            fund_data_reason=raw.fund_data_reason,
        )

    quote_type = _as_text(info.get("quoteType"))

    fees = raw.fund_profile.get("feesExpensesInvestment") if raw.fund_profile else None
    fees_category = raw.fund_profile.get("feesExpensesInvestmentCat") if raw.fund_profile else None
    fees = fees if isinstance(fees, dict) else {}
    fees_category = fees_category if isinstance(fees_category, dict) else {}

    trailing = _trailing_returns((raw.fund_performance or {}).get("trailingReturns"))
    trailing_category = _trailing_returns((raw.fund_performance or {}).get("trailingReturnsCat"))
    risk = _risk_statistics((raw.fund_performance or {}).get("riskOverviewStatistics"))

    has_fund_data = bool(raw.fund_profile) or bool(raw.fund_performance)
    fund_data_reason = (
        None if has_fund_data else _no_fund_data_reason(ticker, quote_type, raw.fund_data_reason)
    )

    return TickerProfile(
        ticker=ticker,
        long_name=_as_text(info.get("longName")) or _as_text(info.get("shortName")),
        quote_type=quote_type,
        currency=_as_text(info.get("currency")),
        fifty_two_week_low=_as_float(info.get("fiftyTwoWeekLow")),
        fifty_two_week_high=_as_float(info.get("fiftyTwoWeekHigh")),
        category_name=_as_text((raw.fund_profile or {}).get("categoryName")),
        family=_as_text((raw.fund_profile or {}).get("family")),
        legal_type=_as_text((raw.fund_profile or {}).get("legalType")),
        expense_ratio=_as_float(fees.get("annualReportExpenseRatio")),
        expense_ratio_category=_as_float(fees_category.get("annualReportExpenseRatio")),
        holdings_turnover=_as_float(fees.get("annualHoldingsTurnover")),
        net_assets=_net_assets(info) if has_fund_data else None,
        trailing_returns=trailing or None,
        trailing_returns_category=trailing_category or None,
        trailing_returns_as_of=_as_of_date((raw.fund_performance or {}).get("trailingReturns")),
        risk_statistics=risk or None,
        sector=_as_text(info.get("sector")),
        industry=_as_text(info.get("industry")),
        market_cap=_as_float(info.get("marketCap")),
        trailing_pe=_as_float(info.get("trailingPE")),
        forward_pe=_as_float(info.get("forwardPE")),
        beta=_as_float(info.get("beta")),
        fund_data_reason=fund_data_reason,
        unavailable_reason=None,
    )
