"""Per-ticker trading currency: look it up, normalize minor units, store it,
and decide whether a set of tickers can share one portfolio.

This module exists because prices are stored exactly as Yahoo Finance quotes
them - yen for a Tokyo listing, pence for a London one - while nothing
downstream carries a unit. `src/optimizer/portfolio.py`'s weights are immune
to that (a monthly return is a ratio of one ticker's own prices, so the
currency cancels), but `allocate_shares` is not: it divides a budget by a
price, which is only meaningful when every price and the budget share one
unit. Scaling all of them by the same factor leaves whole-share counts
identical, which is exactly why a single-currency portfolio is correct and a
mixed one is silently wrong rather than an error.

So the job here is to make the currency known, uniform, and refusable -
never to convert between currencies. Conversion would change what the
`returns` table means (a dollar-based investor's return on a Tokyo stock
includes the yen/dollar move) and is deliberately left to a future plan; see
`plans/11_non_us_tickers_and_single_currency.md`'s Decision Log.

`fetch_ticker_currencies` is the only function here that performs network
I/O, mirroring `src/dataset/prices.py`'s `_fetch_batch` discipline, so it is
the single seam tests monkeypatch.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping

import duckdb
import pandas as pd
import yfinance as yf

from src.config.settings import settings
from src.errors import UnsatisfiableRequestError
from src.dataset.prices import to_yfinance_symbol

logger = logging.getLogger(__name__)

DEFAULT_CURRENCY = "USD"
"""The currency assumed for a ticker with no row in `ticker_currency`.

This default is what makes this feature free for the rest of the project.
Every other path - `build_price_history`'s S&P 500 membership universe, the
three LLM selections, `src/dataset/backfill_snapshot.py`, the backtest
runner, and the shared `data/portfolio.duckdb` cache - never records a
currency and never needs to, because that universe is US-listed by
construction. A missing row therefore means "US dollars", not "unknown", and
no schema change or extra network request is imposed on any of them.

A ticker whose lookup was *attempted and failed* is a different situation and
must NOT land here: it is reported invalid instead (see
`CURRENCY_LOOKUP_FAILED_REASON`), because guessing dollars for a symbol we
could not classify is precisely the silent-mixing failure this module exists
to prevent.
"""

MINOR_UNIT_CURRENCIES: dict[str, tuple[str, float]] = {
    "GBp": ("GBP", 0.01),  # UK pence -> pounds sterling
    "ZAc": ("ZAR", 0.01),  # South African cents -> rand
    "ILA": ("ILS", 0.01),  # Israeli agorot -> new shekels
}
"""Yahoo Finance codes for a *minor* currency unit, mapped to the major unit
and the factor converting one into the other.

Some exchanges quote in a fraction of the currency: the London Stock Exchange
reports Barclays near 190, meaning 190 pence, for a share costing about one
pound ninety. Stored unchanged that price buys a hundredth of the intended
shares - and unlike a yen price, 190 looks like a perfectly plausible dollar
figure, so nothing appears wrong. yfinance performs this same conversion in
`PriceHistory._standardise_currency`, but only under `repair=True`, which
also enables broader price-repair heuristics that would alter every already
cached ticker; hence this explicit table.

Lookups against this table are CASE-SENSITIVE, and must stay that way:
'GBp' is pence but 'GBP' is pounds, so upper-casing a key before the lookup
would divide genuine sterling quotes by 100.
"""

CURRENCY_LOOKUP_FAILED_REASON = (
    "could not determine this ticker's trading currency from Yahoo Finance; "
    "refusing it rather than assuming US dollars, since a wrong guess would "
    "silently mix currencies inside one portfolio"
)


class MixedCurrencyPoolError(UnsatisfiableRequestError):
    """Raised when one portfolio's tickers do not share a single currency.

    Deliberately a `ValueError` subclass: `src/flow/cli.py`'s `_run_edit_loop`
    already catches `ValueError` around its recompute (added for unreachable
    MV targets by `plans/10_performance_reporting_and_target_return.md`), so
    this reverts the offending edit and keeps live mode's only snapshot open
    with no new error handling anywhere.
    """


def normalize_currency(raw_currency: str) -> tuple[str, float]:
    """Translate a Yahoo Finance currency code into `(major_unit, multiplier)`.

    `normalize_currency('GBp')` is `('GBP', 0.01)` - pence, so prices need
    scaling - while `normalize_currency('GBP')` is `('GBP', 1.0)`, genuine
    pounds that must be left alone. That distinction is one character of
    case, which is why the `MINOR_UNIT_CURRENCIES` lookup happens before any
    case folding. Anything not in that table is already a major unit and is
    upper-cased for consistent storage.
    """
    stripped = raw_currency.strip()
    if stripped in MINOR_UNIT_CURRENCIES:
        return MINOR_UNIT_CURRENCIES[stripped]
    return stripped.upper(), 1.0


def fetch_ticker_currencies(
    tickers: list[str],
    pause_seconds: float = settings.yfinance_fundamentals_pause_seconds,
) -> dict[str, str]:
    """Ask Yahoo Finance which currency each of `tickers` trades in, returning
    `{ticker: raw_currency_code}` with the codes exactly as reported (so
    `'GBp'`, not yet `'GBP'`).

    The only function in this module that performs network I/O. Each ticker
    costs one request: the batched `yf.download` call
    `src/dataset/prices.py` already makes does not carry a currency, and
    there is no batched endpoint that does. That is affordable here only
    because this runs for user-supplied pools of a handful of tickers, never
    for the ~500-ticker membership universe - see `DEFAULT_CURRENCY`.

    Uses `fast_info` rather than `Ticker.info`: one request instead of two,
    and `currency` sits on yfinance's own retired-keys list for `info`.
    A ticker whose lookup fails or reports nothing is OMITTED from the result
    and logged, never raised - the caller decides what to do about it, and
    `validate_and_ingest_tickers` reports it as invalid rather than guessing.

    The yfinance symbol is derived with `to_yfinance_symbol`, the same
    translation the price fetch uses, so the currency returned describes the
    very series that was priced.
    """
    currencies: dict[str, str] = {}
    for i, ticker in enumerate(tickers):
        symbol = to_yfinance_symbol(ticker)
        try:
            raw = yf.Ticker(symbol).fast_info.currency
        except Exception as e:  # noqa: BLE001 - yfinance raises assorted types here
            logger.warning("currency lookup failed for %s (symbol %s): %s", ticker, symbol, e)
        else:
            if raw:
                currencies[ticker] = str(raw)
            else:
                logger.warning("currency lookup returned nothing for %s (symbol %s)", ticker, symbol)
        if i < len(tickers) - 1:
            time.sleep(pause_seconds)
    return currencies


def upsert_ticker_currency_table(currency_df: pd.DataFrame, tickers: list[str], db_path: str) -> None:
    """Merge `currency_df` into the `ticker_currency` table at `db_path`,
    replacing only the rows for `tickers` and leaving every other ticker's
    row untouched - the same delete-then-insert discipline
    `src/dataset/prices.py`'s `upsert_prices_tables` uses, and driven by the
    requested `tickers` rather than by `currency_df`'s own contents so a
    ticker that stops resolving has its stale row cleared too.

    `currency_df` columns: `ticker`, `currency`, `quoted_currency`,
    `price_multiplier`.
    """
    con = duckdb.connect(db_path)
    try:
        con.execute(
            "CREATE TABLE IF NOT EXISTS ticker_currency "
            "(ticker VARCHAR, currency VARCHAR, quoted_currency VARCHAR, price_multiplier DOUBLE)"
        )
        if tickers:
            placeholders = ", ".join(["?"] * len(tickers))
            con.execute(f"DELETE FROM ticker_currency WHERE ticker IN ({placeholders})", tickers)

        con.register("currency_df", currency_df)
        con.execute(
            "INSERT INTO ticker_currency SELECT ticker::VARCHAR, currency::VARCHAR, "
            "quoted_currency::VARCHAR, price_multiplier::DOUBLE FROM currency_df"
        )
        con.unregister("currency_df")
    finally:
        con.close()


def load_ticker_currencies(tickers: list[str], db_path: str) -> dict[str, str]:
    """`{ticker: normalized_currency}` for whichever of `tickers` have a row.

    Returns an empty mapping when `db_path` has no `ticker_currency` table -
    or no database file at all - which is the NORMAL case rather than an
    error: the shared historical cache and every non-`user_provided` path
    never create one. Callers then fall back to `DEFAULT_CURRENCY`, which is
    what keeps those paths working unchanged.

    Opened read-only, following `src/dataset/fundamentals.py`'s
    `get_factor_reference_stats`, so that a lookup never creates a database
    as a side effect of asking a question about one.
    """
    if not tickers:
        return {}

    placeholders = ", ".join(["?"] * len(tickers))
    try:
        con = duckdb.connect(db_path, read_only=True)
    except duckdb.IOException:
        return {}
    try:
        rows = con.execute(
            f"SELECT ticker, currency FROM ticker_currency WHERE ticker IN ({placeholders})",
            tickers,
        ).fetchall()
    except duckdb.CatalogException:
        return {}
    finally:
        con.close()
    return {ticker: currency for ticker, currency in rows}


def apply_price_multipliers(long_prices: pd.DataFrame, multipliers: dict[str, float]) -> pd.DataFrame:
    """Scale each ticker's `close` AND `adj_close` by its multiplier, so a
    minor-unit quote (London's pence) is stored in the major unit.

    Both columns are scaled because both are quoted in the minor unit -
    verified directly against Yahoo Finance, where `BARC.L` on 2024-04-02
    reports `close` 184.119995 and `adj_close` 184.006180, and Barclays
    genuinely traded near GBP 1.84. `adj_close` is the one that matters most,
    since `src/optimizer/portfolio.py`'s `load_latest_prices` reads it.

    Pure: returns a new frame, and a ticker absent from `multipliers` is left
    exactly as it was. Note this changes no monthly return, and therefore no
    weight, volatility or Sharpe ratio: `src/dataset/returns.py` computes a
    ratio of two prices of the same ticker, so a common factor cancels. It
    affects only share allocation and display.
    """
    if long_prices.empty:
        return long_prices.copy()

    scaled = long_prices.copy()
    factors = scaled["ticker"].map(multipliers).fillna(1.0).astype(float)
    scaled["close"] = scaled["close"] * factors
    scaled["adj_close"] = scaled["adj_close"] * factors
    return scaled


def group_by_currency(tickers: list[str], currencies: Mapping[str, str]) -> dict[str, list[str]]:
    """Bucket `tickers` by currency, treating one with no entry in
    `currencies` as `DEFAULT_CURRENCY`. Keys and values are sorted so the
    result renders deterministically. A single key means the set can form one
    portfolio; more than one means it cannot.
    """
    grouped: dict[str, list[str]] = {}
    for ticker in tickers:
        grouped.setdefault(currencies.get(ticker, DEFAULT_CURRENCY), []).append(ticker)
    return {currency: sorted(grouped[currency]) for currency in sorted(grouped)}


def partition_by_currency(
    candidates: list[str],
    pool_currency: str | None,
    currencies: Mapping[str, str],
) -> tuple[list[str], dict[str, str], str | None]:
    """Split `candidates` into those that may join a pool of `pool_currency`
    and those that may not, returning `(accepted, refused, pool_currency)`
    where `refused` maps each rejected ticker to its own currency.

    `pool_currency=None` means the pool is empty, in which case the FIRST
    entry of `candidates` establishes it and the rest are measured against
    that - so `candidates` must arrive in the order the person typed them,
    not sorted, or an alphabetical accident would pick the currency (see
    `src/flow/interactive.py`'s `validate_and_edit_candidates`).

    Rejecting per ticker rather than rejecting the whole batch is deliberate:
    it matches the established behavior for an unresolvable ticker, where the
    good tickers typed alongside it are still added.
    """
    accepted: list[str] = []
    refused: dict[str, str] = {}
    for ticker in candidates:
        currency = currencies.get(ticker, DEFAULT_CURRENCY)
        if pool_currency is None:
            pool_currency = currency
        if currency == pool_currency:
            accepted.append(ticker)
        else:
            refused[ticker] = currency
    return accepted, refused, pool_currency
