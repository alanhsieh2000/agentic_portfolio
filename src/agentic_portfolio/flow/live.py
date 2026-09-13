"""Live-mode snapshot builder for `plans/06_interactive_flow.md`: assembles
a throwaway DuckDB database holding fresh S&P 500 membership, price
history, factors, and momentum for a given `as_of` date - by calling
plans 1 through 3's own `build_*` functions completely unchanged, just
pointed at this temporary file instead of `data/portfolio.duckdb` - so
plans 2 through 5's functions (which all read their inputs via SQL against
a `db_path` parameter) can run against live data with no changes of their
own, per this plan's Plan of Work ("reusing the same functions from plans
1 through 3, just pointed at live data instead of the DuckDB cache").

Two distinct membership/factors dates are built when LLM-S is part of
`selection`, not one: `as_of` itself (what `screen` applies the rule to,
and what the scanner/optimizer stage anchors on) and a causally-masked
December-of-`as_of.year - 1` date (what `generate_rule` needs to derive a
rule without look-ahead bias, mirroring `src/agentic_portfolio/agents/llm_s.py`'s
`resolve_as_of_date` exactly, which always looks for a `factors` row
strictly before `date(year, 1, 1)`). `llm_f_only` mode needs neither -
`screen_month` only reads `sp500_membership` and news headlines, never the
`factors` table - so factors/momentum are skipped entirely for it, per the
same "skip the call, not just its result" discipline this plan's Progress
item 18 already applies to LLM-S/LLM-F themselves.

`user_provided` mode needs none of it: its candidates come from the user,
not from index membership or either agent, so it skips the Wikipedia fetch,
the full-universe price/returns build, factors/momentum, and the news
archive entirely - the snapshot it gets is an empty-tabled scratch database
that `src/agentic_portfolio/dataset/ticker_ingestion.py` fills in one user-supplied ticker
batch at a time. That keeps `data/portfolio.duckdb` itself untouched while
still letting the optimizer read prices/returns through the same `db_path`
parameter every other mode uses.
"""

from __future__ import annotations

import logging
import os
import tempfile
from contextlib import contextmanager
from datetime import date, timedelta

import duckdb
import pandas as pd

from agentic_portfolio.config.preflight import require_sec_user_agent
from agentic_portfolio.dataset.fundamentals import build_factors
from agentic_portfolio.dataset.membership import apply_changes_asof, compute_rebalance_dates, fetch_and_normalize_membership, write_membership_table
from agentic_portfolio.dataset.dividends import build_dividends
from agentic_portfolio.dataset.momentum import build_momentum_factors
from agentic_portfolio.dataset.prices import build_price_history
from agentic_portfolio.dataset.returns import build_returns

logger = logging.getLogger(__name__)

LOOKBACK_MONTHS = 65
"""Months of price history fetched before `as_of`: `load_returns_matrix`'s
default 60-month lookback, plus a buffer so every one of those 60 monthly
returns has a real preceding-month price to compute from (rather than the
first row nulling out, per `plans/01_dataset.md`'s documented edge case)
- and, incidentally, comfortably enough to also cover the causal-masking
December date's own 12-month momentum lookback, which never reaches more
than ~33 months before `as_of`."""


def _causal_masking_date(as_of: date) -> date:
    """The same "December of the prior year" snapshot date
    `src/agentic_portfolio/agents/llm_s.py`'s `resolve_as_of_date` is written to look for:
    the first business day of December, `as_of.year - 1`, per
    `src/agentic_portfolio/dataset/membership.py`'s own `compute_rebalance_dates` definition
    of a rebalance date - reused here rather than hand-rolled, so live
    mode's causal-masking date is computed the identical way backtest
    mode's stored one already is.
    """
    year = as_of.year - 1
    return compute_rebalance_dates(f"{year}-12-01", f"{year}-12-31")[0].date()


def _init_empty_price_and_returns_tables(db_path: str) -> None:
    """Create the empty `prices`/`unresolved_tickers`/`returns`/
    `ticker_currency`/`dividends`/`splits`/`dividend_coverage` tables the
    `user_provided` selection incrementally fills in via
    `src/agentic_portfolio/dataset/ticker_ingestion.py`'s upserts, with the same column shapes
    plans 1's, 11's and 15's own writers use. The three corporate-action
    tables are created here for completeness rather than out of necessity -
    the upserts use CREATE TABLE IF NOT EXISTS - but omitting them from this
    list would suggest dividends are not part of this flow, which they are. No `sp500_membership`
    table: nothing in that selection's downstream path reads one, since its
    candidates come from the user rather than from index membership.
    """
    con = duckdb.connect(db_path)
    try:
        con.execute("CREATE TABLE prices (date DATE, ticker VARCHAR, close DOUBLE, adj_close DOUBLE)")
        con.execute("CREATE TABLE unresolved_tickers (ticker VARCHAR, reason VARCHAR)")
        con.execute("CREATE TABLE returns (rebalance_date DATE, ticker VARCHAR, monthly_return DOUBLE)")
        con.execute(
            "CREATE TABLE ticker_currency "
            "(ticker VARCHAR, currency VARCHAR, quoted_currency VARCHAR, price_multiplier DOUBLE)"
        )
        con.execute("CREATE TABLE dividends (ex_date DATE, ticker VARCHAR, amount DOUBLE)")
        con.execute("CREATE TABLE splits (ex_date DATE, ticker VARCHAR, ratio DOUBLE)")
        con.execute(
            "CREATE TABLE dividend_coverage (ticker VARCHAR, fetch_start DATE, fetch_end DATE)"
        )
    finally:
        con.close()


def _copy_news_archive(source_db_path: str, dest_db_path: str) -> None:
    """Copy `news_articles_hf` (a static historical archive, unrelated to
    `as_of`) from `source_db_path` into `dest_db_path`, so `screen_month`'s
    `fetch_headlines` can run against the live snapshot unmodified - it
    checks this table's min/max `publish_date` to decide whether to read
    the archive or fall back to live `yfinance.news`; a live `as_of` date
    is always outside the archive's historical coverage, so this correctly
    always falls through to the live-headline path. A missing source table
    (a repository with plan 1's dataset build skipped) is logged, not
    raised - only `llm_f_only`/`llm_s_and_f` selections need it.
    """
    con = duckdb.connect(dest_db_path)
    try:
        # DuckDB's ATTACH does not accept a bound parameter for its target path
        # (verified live: "Parser Error: syntax error at or near '?'") - the
        # path is escaped and embedded as a string literal instead.
        escaped_path = source_db_path.replace("'", "''")
        con.execute(f"ATTACH '{escaped_path}' AS src (READ_ONLY)")
        try:
            con.execute("CREATE TABLE news_articles_hf AS SELECT * FROM src.news_articles_hf")
        except duckdb.CatalogException:
            logger.warning(
                "no news_articles_hf table in %s; live mode's LLM-F path will fail if selected",
                source_db_path,
            )
        finally:
            con.execute("DETACH src")
    finally:
        con.close()


@contextmanager
def build_scratch_snapshot(prefix: str = "benchmark_snapshot_"):
    """A throwaway DuckDB file holding only the empty `prices`/
    `unresolved_tickers`/`returns`/`ticker_currency` tables, yielded as a
    path and deleted on exit regardless of success.

    This is the one place the "temporary database nobody owns" discipline
    lives: `mkstemp` to get a name nothing else will take, remove the empty
    file it created (`duckdb.connect` must create the file itself, or it is
    confused by a zero-byte one), create the tables, and always delete on the
    way out. `build_live_snapshot`'s `user_provided` branch is one caller;
    `src/agentic_portfolio/flow/interactive.py`'s `prepare_benchmark` is the other, which needs
    somewhere to fetch a benchmark's price and return history that is
    emphatically NOT the session's own database - see that function for why.
    """
    fd, temp_db_path = tempfile.mkstemp(suffix=".duckdb", prefix=prefix)
    os.close(fd)
    os.remove(temp_db_path)  # duckdb.connect must create this file itself; an empty file confuses it
    try:
        _init_empty_price_and_returns_tables(temp_db_path)
        yield temp_db_path
    finally:
        if os.path.exists(temp_db_path):
            os.remove(temp_db_path)


@contextmanager
def build_live_snapshot(
    as_of: date, selection: str, source_db_path: str, allow_dividend_fetch: bool = True
):
    """Build a throwaway DuckDB file covering `as_of` (always) and,
    when `selection` needs LLM-S, the causal-masking December date too -
    membership for each via a fresh Wikipedia fetch, `LOOKBACK_MONTHS`
    months of price history through `as_of`, and (when LLM-S is needed)
    the resulting factors/momentum tables - all via plan 1's own `build_*`
    functions, unmodified, pointed at this temp file. Yields the temp
    file's path; deletes it on exit regardless of success, per this plan's
    Idempotence and Recovery section ("live mode ... writes nothing to
    disk" - a temp file that exists only for this call's duration and is
    always removed before returning satisfies that intent even though it
    technically touches disk briefly).

    Dividend history is built too, for the same screened selections, so the
    report's income figures and any `--min-annual-dividend` floor work in
    live mode. Without it a live screened run had no `dividends` table at
    all and named every candidate's dividend data as unavailable - the
    default selection's most visible gap, since the snapshot is discarded
    at the end of the run and no build command could reach it.
    `allow_dividend_fetch=False` (from `--no-dividend-fetch`) skips that one
    pass, which is a pass over the whole membership universe.

    `selection="user_provided"` skips every fetch and build above, delegating
    to `build_scratch_snapshot` for a file holding only the empty
    `prices`/`unresolved_tickers`/`returns`/`ticker_currency` tables that
    `src/agentic_portfolio/dataset/ticker_ingestion.py` fills in as the user names tickers -
    see this module's docstring.
    """
    if selection == "user_provided":
        with build_scratch_snapshot("live_snapshot_") as scratch_db_path:
            yield scratch_db_path
        return

    # Only the selections that call `build_factors` below reach the SEC, and
    # they reach it after the membership and price fetches - so establish the
    # User-Agent now rather than several minutes in. `user_provided` returned
    # above and needs nothing.
    if selection in ("llm_s_only", "llm_s_and_f"):
        require_sec_user_agent()

    fd, temp_db_path = tempfile.mkstemp(suffix=".duckdb", prefix="live_snapshot_")
    os.close(fd)
    os.remove(temp_db_path)  # duckdb.connect must create this file itself; an empty file confuses it
    try:
        current, changes = fetch_and_normalize_membership()

        snapshot_dates = [as_of]
        if selection in ("llm_s_only", "llm_s_and_f"):
            snapshot_dates = [_causal_masking_date(as_of), as_of]

        frames = []
        for d in snapshot_dates:
            members = apply_changes_asof(current, changes, d).copy()
            members.insert(0, "rebalance_date", pd.Timestamp(d))
            frames.append(members)
            logger.info("live snapshot: rebalance_date=%s membership_count=%d", d, len(members))
        write_membership_table(pd.concat(frames, ignore_index=True), db_path=temp_db_path)

        price_start = (pd.Timestamp(as_of) - pd.DateOffset(months=LOOKBACK_MONTHS)).date().isoformat()
        price_end = (as_of + timedelta(days=5)).isoformat()
        build_price_history(db_path=temp_db_path, start=price_start, end=price_end)

        if selection in ("llm_s_only", "llm_s_and_f"):
            build_factors(db_path=temp_db_path)
            build_momentum_factors(db_path=temp_db_path)

        build_returns(db_path=temp_db_path, start=price_start, end=as_of.isoformat())

        if allow_dividend_fetch:
            # After `build_price_history`, never before: `build_dividends`
            # takes its ticker universe from the `prices` table it is
            # pointed at, and raises if that table is empty.
            #
            # A failure here is logged and swallowed. This snapshot has by
            # now cost minutes of fetching - membership, prices, factors,
            # momentum, returns - and discarding all of it because a
            # dividend batch was rate-limited would be a poor trade. The
            # report already names a ticker whose dividend data is missing
            # rather than assuming it pays nothing, so the degradation is
            # visible and honest. Same discipline
            # `src/agentic_portfolio/dataset/ticker_ingestion.py` applies to its own dividend
            # call.
            try:
                build_dividends(db_path=temp_db_path, start=price_start, end=price_end)
            except Exception as e:  # noqa: BLE001 - yfinance and duckdb raise assorted types
                logger.warning(
                    "live snapshot: could not build dividend history (%s); the report will "
                    "name each candidate's dividend data as unavailable", e
                )

        if selection in ("llm_f_only", "llm_s_and_f"):
            _copy_news_archive(source_db_path, temp_db_path)

        yield temp_db_path
    finally:
        if os.path.exists(temp_db_path):
            os.remove(temp_db_path)
