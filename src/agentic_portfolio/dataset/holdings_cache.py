"""A persistent price-and-returns cache for the tickers a person holds or
experiments with, at `data/holdings.duckdb`.

Before this existed, every report on the user's own portfolio refetched 65
months of daily prices from Yahoo Finance into a throwaway database that
was deleted moments later - about ten seconds of wall clock for a
two-holding portfolio, scaling with the number of holdings, and paid again
on the very next command. See `plans/13_user_portfolio.md`'s Milestone 6.

This is the project's SECOND cache and deliberately not the first.
`data/portfolio.duckdb` holds the S&P 500 universe that the optimizer's
candidate pools are measured against, and two rules keep them apart. It
must not gain rows as a side effect of reporting what somebody owns. And
`src/agentic_portfolio/optimizer/portfolio.py`'s `_load_window_dates` derives a portfolio's
returns window from `SELECT DISTINCT rebalance_date FROM returns` over the
WHOLE table, so holdings rows landing in the database a candidate pool is
being measured against could move the very window that pool's figures are
computed over. A separate file makes both impossible rather than merely
unlikely - the same reasoning the throwaway database was built on, with the
throwing-away removed.

STALENESS, which is the design question that kept this unbuilt: a ticker is
stale when the cache does not hold the monthly return for the most recent
rebalance date on or before the report's date. In plainer words, the cache
is good for the rest of the calendar month and a new month makes it stale.
That is derived from the DATA rather than from a clock: the question "does
this cache contain the latest month's return?" is answered by looking for
the row, so there is no `fetched_at` timestamp to drift out of agreement
with the rows it describes, and no extra table to maintain.

The consequence worth being honest about: monthly returns change monthly,
but PRICES change daily, and the latest price is what turns share counts
into `Total value` and into weights. A cache refreshed on the 3rd and read
on the 25th reports a total three weeks out of date. That is a real cost of
the monthly rule, accepted deliberately, and mitigated rather than hidden -
`src/agentic_portfolio/optimizer/holdings.py` reports the date of the price it actually used
and the report prints it, so a stale total is always a stated one.
`--refresh-holdings` forces a refresh at any time.

Only the tickers a report asks about are ever refreshed, so the cache does
not slowly become a list of everything ever typed that must all be
refetched together. A ticker nobody asks about is simply never touched
again, and its old rows are harmless: every query filters by ticker, and
the one that does not (`_load_window_dates`) reads only distinct rebalance
dates, which are the same monthly grid for every ticker.
"""

from __future__ import annotations

import logging
from datetime import date

import duckdb
import pandas as pd

from agentic_portfolio.dataset.dividends import has_splits_table
from agentic_portfolio.dataset.membership import compute_rebalance_dates
from agentic_portfolio.dataset.ticker_ingestion import validate_and_ingest_tickers

logger = logging.getLogger(__name__)

DEFAULT_HOLDINGS_CACHE_PATH = "data/holdings.duckdb"


def latest_expected_rebalance_date(as_of: date) -> date:
    """The most recent rebalance date on or before `as_of` - the first
    business day of `as_of`'s own month.

    Reuses `src/agentic_portfolio/dataset/membership.py`'s `compute_rebalance_dates` rather
    than hand-rolling a month-start calculation, so the cache's idea of
    "the latest month" is the same one `src/agentic_portfolio/dataset/returns.py` used when it
    wrote the rows being looked for. Two different definitions of the
    monthly grid would make a permanently-stale cache that refetches on
    every single run.
    """
    first_of_month = as_of.replace(day=1)
    dates = compute_rebalance_dates(first_of_month.isoformat(), as_of.isoformat())
    if dates:
        return dates[-1].date()

    # `as_of` falls before its own month's first business day - possible only
    # for a weekend 1st or 2nd. The previous month's is then the latest one.
    previous_month_end = first_of_month - pd.Timedelta(days=1)
    previous = compute_rebalance_dates(
        previous_month_end.replace(day=1).isoformat(), previous_month_end.isoformat()
    )
    return previous[-1].date() if previous else first_of_month


def cached_month_ends(tickers: list[str], cache_path: str) -> dict[str, date | None]:
    """The newest `rebalance_date` the cache holds for each of `tickers`, or
    `None` for one it holds nothing for.

    Opened read-only, and a missing file or missing `returns` table reports
    `None` for everything rather than raising - the expected state before
    the cache has ever been written, and the reason asking about the cache
    never creates one.
    """
    if not tickers:
        return {}

    placeholders = ", ".join(["?"] * len(tickers))
    try:
        con = duckdb.connect(cache_path, read_only=True)
    except duckdb.IOException:
        return {ticker: None for ticker in tickers}
    try:
        rows = con.execute(
            "SELECT ticker, max(rebalance_date) FROM returns "
            f"WHERE ticker IN ({placeholders}) AND monthly_return IS NOT NULL "
            "GROUP BY ticker",
            list(tickers),
        ).fetchall()
    except duckdb.CatalogException:
        return {ticker: None for ticker in tickers}
    finally:
        con.close()

    newest = {ticker: value for ticker, value in rows}
    return {ticker: newest.get(ticker) for ticker in tickers}


def stale_tickers(
    tickers: list[str], as_of: date, cache_path: str, force: bool = False
) -> list[str]:
    """Which of `tickers` the cache cannot answer for as of `as_of`: the ones
    it holds nothing for, and the ones whose newest monthly return predates
    `as_of`'s own month.

    `force=True` (from `--refresh-holdings`) reports every ticker stale. It
    is the escape hatch the monthly rule needs: the monthly returns really
    are current all month, but the PRICES behind `Total value` are not, so a
    person who wants today's total has to be able to say so.

    A cache with no `splits` table reports EVERY ticker stale, whatever its
    monthly returns say. That table arrived after this cache did, and the
    reports that read it explain why a dividend was restated by a split - so
    without it a report is silently missing an explanation it should be
    giving, and a cache written last month would keep it missing until the
    month turned. The check is on the table, never on rows: a ticker that
    never split holds no rows, so counting rows could not tell a
    pre-migration cache from a correct one. It is one-time and self-healing,
    since a single refresh creates the table for good, and it stays derived
    from the data rather than from a version number.

    Sorted, so a refresh fetches in a deterministic order and a test can
    assert on the list.
    """
    if force:
        return sorted(tickers)

    if not has_splits_table(cache_path):
        return sorted(tickers)

    expected = latest_expected_rebalance_date(as_of)
    return sorted(
        ticker
        for ticker, newest in cached_month_ends(tickers, cache_path).items()
        if newest is None or newest < expected
    )


def refresh_holdings_cache(
    tickers: list[str], as_of: date, cache_path: str, force: bool = False
) -> tuple[list[str], dict[str, str], dict[str, str]]:
    """Fetch and merge the stale members of `tickers` into the cache,
    returning `(valid, invalid, currencies)` for ALL of `tickers` - the
    freshly fetched ones and the ones that needed nothing alike.

    Only stale tickers cost a request. A caller asking about a five-holding
    portfolio the day after it was cached pays nothing at all, which is the
    entire point of the file.

    The return shape matches `validate_and_ingest_tickers` so a caller can
    treat a cache refresh and a cold fetch identically. A ticker that was
    already fresh is reported valid with the currency the cache recorded for
    it, since it demonstrably resolved when it was first fetched.
    """
    stale = stale_tickers(tickers, as_of, cache_path, force=force)
    fresh = [ticker for ticker in sorted(tickers) if ticker not in stale]

    valid, invalid, currencies = ([], {}, {})
    if stale:
        logger.info("refreshing %d holdings cache ticker(s): %s", len(stale), ", ".join(stale))
        valid, invalid, currencies = validate_and_ingest_tickers(stale, as_of, cache_path)

    if fresh:
        from agentic_portfolio.dataset.ticker_currency import load_ticker_currencies

        cached_currencies = load_ticker_currencies(fresh, cache_path)
        valid = sorted(set(valid) | set(fresh))
        currencies = {**cached_currencies, **currencies}

    return valid, invalid, currencies
