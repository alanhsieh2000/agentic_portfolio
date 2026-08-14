"""Point-in-time S&P 500 membership reconstruction from Wikipedia.

Reconstructs which tickers were actually S&P 500 members on each of the
monthly rebalance dates this project uses, rather than using today's
constituent list for the entire backtest window (which would silently drop
every company that left the index and cause survivorship bias).
"""

from __future__ import annotations

import io
import logging
from pathlib import Path

import duckdb
import pandas as pd
import requests

from src.config.settings import settings

logger = logging.getLogger(__name__)

WIKIPEDIA_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
CHANGES_URL = "https://en.wikipedia.org/wiki/Historical_components_of_the_S%26P_500"
USER_AGENT = "agentic-portfolio-dataset-builder/0.1 (educational research project)"


class MembershipTableNotFoundError(RuntimeError):
    """Raised when the Wikipedia page no longer has a table matching the
    expected columns for either the current-constituents table or the
    historical-changes table. Wikipedia editors can restructure this page at
    any time; this exists so that happening fails loudly with a diagnosable
    message instead of silently returning an empty or wrong table.
    """


def _fetch_html(url: str = WIKIPEDIA_URL, timeout: float = settings.http_timeout_seconds) -> str:
    """Download the raw page HTML. The only network I/O in this module."""
    response = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout)
    response.raise_for_status()
    return response.text


def _flatten_column(col: object) -> str:
    """Collapse a possibly-MultiIndex column label into one readable string.

    The real changes table on Wikipedia is parsed by pandas.read_html as a
    2-level MultiIndex, e.g. ('Added', 'Ticker') or ('Effective Date',
    'Effective Date') where the top level duplicates the second — this
    drops the duplicate level so matching stays simple.
    """
    if isinstance(col, tuple):
        parts = [str(p).strip() for p in col if not str(p).lower().startswith("unnamed")]
        deduped: list[str] = []
        for part in parts:
            if not deduped or deduped[-1].lower() != part.lower():
                deduped.append(part)
        return " ".join(deduped)
    return str(col).strip()


def _locate_table(
    tables: list[pd.DataFrame], required: list[str], table_name: str, source_url: str = WIKIPEDIA_URL
) -> pd.DataFrame:
    """Return the first table whose flattened columns cover every substring
    in `required`. Matching is substring-based and case-insensitive because
    the real column is named "Effective Date", not the "Date" the plan's
    prose paraphrase suggested, and because Wikipedia can reorder the page's
    tables (a navbox table must be skipped by content, not by index).
    """
    for table in tables:
        flat_cols = [_flatten_column(c).lower() for c in table.columns]
        if all(any(req in col for col in flat_cols) for req in required):
            return table
    raise MembershipTableNotFoundError(
        f"Could not find the {table_name!r} table among {len(tables)} tables parsed "
        f"from {source_url}. Looked for columns containing every one of {required}. "
        f"Columns found in each candidate table: "
        f"{[[_flatten_column(c) for c in t.columns] for t in tables]}"
    )


def fetch_membership_tables(
    url: str = WIKIPEDIA_URL, changes_url: str = CHANGES_URL, timeout: float = settings.http_timeout_seconds
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Fetch (raw_current_table, raw_changes_table), located by column-name
    inspection rather than table index.

    As of 2026-08-11 (Wikipedia revision 1368903137, "move to [[Historical
    components of the S&P 500]]"), the changes table no longer lives on the
    same page as the current-constituents table — it was split out to its
    own article. Both tables are looked for on `url` first (so this keeps
    working unmodified if Wikipedia ever merges them back onto one page);
    only if the changes table isn't found there does this fall back to a
    second fetch of `changes_url`.
    """
    html = _fetch_html(url, timeout=timeout)
    tables = pd.read_html(io.StringIO(html))
    current = _locate_table(tables, required=["symbol", "date added"], table_name="current constituents", source_url=url)
    try:
        changes = _locate_table(tables, required=["date", "added", "removed"], table_name="selected changes", source_url=url)
    except MembershipTableNotFoundError:
        changes_html = _fetch_html(changes_url, timeout=timeout)
        changes_tables = pd.read_html(io.StringIO(changes_html))
        changes = _locate_table(
            changes_tables, required=["date", "added", "removed"], table_name="selected changes", source_url=changes_url
        )
    return current, changes


def _normalize_current_table(raw: pd.DataFrame) -> pd.DataFrame:
    """Raw current-constituents table -> columns ['ticker', 'security'].

    Only these two columns are needed: the backward-walk starts from
    today's full membership set and never consults 'Date added' — only the
    changes table's dates matter for the walk.
    """
    cols = {_flatten_column(c): c for c in raw.columns}
    ticker_col = next(c for name, c in cols.items() if "symbol" in name.lower())
    security_col = next(c for name, c in cols.items() if name.lower() == "security")
    out = raw[[ticker_col, security_col]].copy()
    out.columns = ["ticker", "security"]
    out["ticker"] = out["ticker"].astype(str).str.strip()
    out["security"] = out["security"].astype(str).str.strip()
    return out


def _normalize_changes_table(raw: pd.DataFrame) -> pd.DataFrame:
    """Raw changes table (MultiIndex columns) -> columns ['date',
    'added_ticker', 'added_security', 'removed_ticker', 'removed_security'].

    Missing sides of a change row stay as real NaN (verified empty cells
    parse as float NaN, not empty string), so downstream code must check
    with pd.notna rather than truthiness.
    """
    flat = raw.copy()
    flat.columns = [_flatten_column(c) for c in flat.columns]
    lower_to_actual = {c.lower(): c for c in flat.columns}

    def find(*substrings: str) -> str:
        for name, actual in lower_to_actual.items():
            if all(s in name for s in substrings):
                return actual
        raise MembershipTableNotFoundError(
            f"Changes table is missing an expected column containing {substrings}; "
            f"actual columns: {list(flat.columns)}"
        )

    date_col = find("date")
    added_ticker_col = find("added", "ticker")
    added_security_col = find("added", "security")
    removed_ticker_col = find("removed", "ticker")
    removed_security_col = find("removed", "security")

    out = pd.DataFrame(
        {
            "date": pd.to_datetime(flat[date_col], errors="raise"),
            "added_ticker": flat[added_ticker_col].astype("object"),
            "added_security": flat[added_security_col].astype("object"),
            "removed_ticker": flat[removed_ticker_col].astype("object"),
            "removed_security": flat[removed_security_col].astype("object"),
        }
    )
    for col in ("added_ticker", "added_security", "removed_ticker", "removed_security"):
        out[col] = out[col].apply(lambda v: v.strip() if isinstance(v, str) else v)
    return out


def apply_changes_asof(current_members: pd.DataFrame, changes: pd.DataFrame, as_of) -> pd.DataFrame:
    """Reconstruct membership as of `as_of` by undoing every change strictly
    newer than `as_of`, walking from most recent to oldest.

    Pure function, no I/O: `current_members` has columns ['ticker',
    'security'] (today's full membership), `changes` has columns ['date',
    'added_ticker', 'added_security', 'removed_ticker', 'removed_security'].
    An addition after `as_of` means the ticker was not yet a member as of
    `as_of` (remove it from the working set); a removal after `as_of` means
    the ticker had not yet been removed as of `as_of` (add it back). Returns
    columns ['ticker', 'security'], sorted by ticker.
    """
    as_of_ts = pd.Timestamp(as_of)
    members: dict[str, str] = dict(zip(current_members["ticker"], current_members["security"]))

    later = changes[changes["date"] > as_of_ts].sort_values("date", ascending=False)
    for row in later.itertuples(index=False):
        if pd.notna(row.added_ticker) and str(row.added_ticker).strip():
            members.pop(str(row.added_ticker).strip(), None)
        if pd.notna(row.removed_ticker) and str(row.removed_ticker).strip():
            ticker = str(row.removed_ticker).strip()
            security = row.removed_security if pd.notna(row.removed_security) else ""
            members[ticker] = str(security).strip()

    return pd.DataFrame(sorted(members.items()), columns=["ticker", "security"])


def compute_rebalance_dates(
    start: str = settings.rebalance_start, end: str = settings.rebalance_end
) -> list[pd.Timestamp]:
    """Return the first trading day of every month from `start` through
    `end` inclusive, as pandas Timestamps.

    Uses freq="BMS" (first weekday of each calendar month), which does not
    account for U.S. market holidays. Exact NYSE-holiday precision is
    deliberately skipped here: it would need a new dependency
    (pandas_market_calendars is not in pyproject.toml) for no change in any
    answer this module produces, since every downstream module (prices.py,
    fundamentals.py, momentum.py, returns.py — future work) looks up the
    nearest available price on or before a date rather than requiring an
    exact-date match. Later modules should import and reuse this exact
    function rather than reimplementing month-start logic, so every table
    in data/portfolio.duckdb agrees on what "rebalance_date" means.
    """
    return list(pd.date_range(start=start, end=end, freq="BMS"))


def reconstruct_membership(
    current: pd.DataFrame, changes: pd.DataFrame, rebalance_dates: list[pd.Timestamp]
) -> pd.DataFrame:
    """Apply `apply_changes_asof` across every rebalance date, recomputing
    from the full current-membership set each time rather than
    incrementally — this keeps the logic simple to verify and is cheap
    enough (tens of milliseconds) that recomputation cost doesn't matter.

    Returns one DataFrame with columns ['rebalance_date', 'ticker', 'security'].
    """
    frames = []
    for d in rebalance_dates:
        as_of_members = apply_changes_asof(current, changes, d).copy()
        as_of_members.insert(0, "rebalance_date", d)
        frames.append(as_of_members)
        logger.info("rebalance_date=%s membership_count=%d", d.date(), len(as_of_members))
    return pd.concat(frames, ignore_index=True)


def write_membership_table(df: pd.DataFrame, db_path: str = settings.db_path) -> None:
    """Write `df` to the sp500_membership table in the DuckDB file at
    `db_path`, creating the parent directory if needed. Drops any
    pre-existing table first, so re-running this is always safe.
    """
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(db_path)
    try:
        con.register("membership_df", df)
        con.execute("DROP TABLE IF EXISTS sp500_membership")
        con.execute(
            "CREATE TABLE sp500_membership AS "
            "SELECT rebalance_date::DATE AS rebalance_date, "
            "ticker::VARCHAR AS ticker, "
            "security::VARCHAR AS security "
            "FROM membership_df"
        )
        con.unregister("membership_df")
    finally:
        con.close()


def fetch_and_normalize_membership(url: str = WIKIPEDIA_URL) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Fetch and normalize Wikipedia's current-constituents and changes
    tables in one call, returning (current, changes) ready for
    `apply_changes_asof` or `reconstruct_membership`. Extracted out of
    `build_membership_table` so a caller needing membership as of a single
    out-of-band date (e.g. `src/dataset/backfill_snapshot.py`, which
    backfills a pre-2020-01-01 snapshot per `plans/08_consistency_review.md`
    finding 4) does not need to duplicate the fetch-then-normalize steps.
    """
    raw_current, raw_changes = fetch_membership_tables(url)
    return _normalize_current_table(raw_current), _normalize_changes_table(raw_changes)


def build_membership_table(db_path: str = settings.db_path, url: str = WIKIPEDIA_URL) -> pd.DataFrame:
    """Fetch, normalize, reconstruct across all rebalance dates, write to
    DuckDB, and return the resulting DataFrame.
    """
    current, changes = fetch_and_normalize_membership(url)
    rebalance_dates = compute_rebalance_dates()
    membership = reconstruct_membership(current, changes, rebalance_dates)
    write_membership_table(membership, db_path)
    logger.info("wrote %d rows to %s::sp500_membership", len(membership), db_path)
    return membership


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    build_membership_table()
