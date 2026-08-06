"""Historical book equity from SEC EDGAR's XBRL structured-filing data, as
a deeper, more precise alternative to yfinance's balance-sheet rolling
window (see src/dataset/fundamentals.py's module docstring and
plans/01_dataset.md's Surprises & Discoveries for why yfinance alone leaves
`bm` null for essentially all of 2020-2021).

SEC's `companyconcept` API returns a company's COMPLETE historical series
for one XBRL tag (e.g. `StockholdersEquity`), each value tagged with the
real `filed` date — the actual date that figure became publicly knowable —
rather than yfinance's short rolling window and this project's own 3-month
lag approximation. This module fetches that directly and is designed to be
used as fundamentals.py's PREFERRED source for `bm`'s book-equity input,
with yfinance's existing balance-sheet path kept as a fallback (see
has_sufficient_coverage) for tickers where SEC coverage is missing or
doesn't reach this project's rebalance window.

Same 3-layer split as every other module in this package: network I/O
(the only functions touching the network) -> pure transform (unit-testable
with hand-built fixtures, zero network calls in tests) -> orchestration
(paced, per-ticker degrade-not-crash, matching fetch_all_ticker_fundamentals
in fundamentals.py).

CRITICAL, verified live: the CURRENT ticker->CIK mapping can point to a
DIFFERENT legal entity than the one that filed this project's target-window
(2020-2024) filings, after a corporate restructuring. Concretely, `XOM`
currently maps to CIK 2115436 ("ExxonMobil Holdings Corp", a newly-formed
2025/2026-era holding entity with facts only from 2025-2026) -- but the
classic "EXXON MOBIL CORP" entity that filed all the real 2020-2024
10-Ks/10-Qs is CIK 34088, no longer associated with the ticker in the
current mapping. has_sufficient_coverage() detects this cheaply (from data
already fetched, no extra network call) so fundamentals.py can correctly
fall back to yfinance for tickers like XOM instead of reporting a
confidently-wrong empty result.
"""

from __future__ import annotations

import logging
import time

import pandas as pd
import requests

from src.config.settings import settings
from src.dataset.prices import to_yfinance_symbol

logger = logging.getLogger(__name__)

SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_COMPANY_CONCEPT_URL_TMPL = "https://data.sec.gov/api/xbrl/companyconcept/CIK{cik:010d}/us-gaap/{tag}.json"
SEC_COMPANY_FACTS_URL_TMPL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"

BOOK_EQUITY_XBRL_TAGS = [
    "StockholdersEquity",  # excludes minority interest; verified present for AAPL and JPM
    "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",  # last resort; verified larger than the above for XOM
]


class SecUserAgentMissingError(RuntimeError):
    """SEC_UA is unset/blank (via src.config.settings.settings, which loads
    .env at its own import time). The blank check itself still only fires
    at first use (_sec_headers's first real call), not at import time.
    Never silently falls back to a generic User-Agent -- verified live that
    SEC returns HTTP 403 for a missing/generic User-Agent, which would
    otherwise masquerade as "no data" for every single ticker.
    """


class SecAccessForbiddenError(RuntimeError):
    """A SEC request returned HTTP 403 -- verified live this is SEC's actual
    response to a missing/bad User-Agent, a config problem, not per-ticker
    data absence. Deliberately left UNCAUGHT everywhere in this module,
    including the orchestration layer, so a broken SEC_UA fails loud on the
    first call instead of silently producing hundreds of fallback tickers.
    """


class SecTickerMapShapeError(RuntimeError):
    """company_tickers.json parsed but its shape no longer matches the
    expected {index: {"cik_str":..., "ticker":..., "title":...}} -- a
    structural break in SEC's own file format, not per-ticker absence.
    """


def _sec_headers() -> dict[str, str]:
    """Reads SEC_UA from src.config.settings.settings (which loads .env at
    import time). Raises SecUserAgentMissingError if unset/blank.
    """
    ua = settings.sec_ua.strip()
    if not ua:
        raise SecUserAgentMissingError(
            "SEC_UA is unset or blank. SEC requires a descriptive User-Agent "
            "header (e.g. 'Your Name your.email@example.com') and returns "
            "HTTP 403 without one; set SEC_UA in .env."
        )
    return {"User-Agent": ua}


def fetch_cik_map(session: requests.Session | None = None, timeout: float = settings.http_timeout_seconds) -> dict[str, int]:
    """One GET of SEC's full ticker->CIK file, covering every CURRENTLY
    SEC-registered ticker (verified live: ~10,398 tickers, all uppercase,
    dashes not dots for share classes -- identical convention to
    prices.py's to_yfinance_symbol). Fetch once per build run, not once per
    ticker.

    Raises SecAccessForbiddenError on HTTP 403, SecTickerMapShapeError if
    the JSON isn't shaped as expected.
    """
    sess = session or requests.Session()
    response = sess.get(SEC_TICKERS_URL, headers=_sec_headers(), timeout=timeout)
    if response.status_code == 403:
        raise SecAccessForbiddenError(f"SEC returned HTTP 403 fetching {SEC_TICKERS_URL}; check SEC_UA.")
    response.raise_for_status()
    data = response.json()
    try:
        entries = data.values() if isinstance(data, dict) else data
        cik_map = {str(e["ticker"]).strip().upper(): int(e["cik_str"]) for e in entries}
    except (KeyError, TypeError, ValueError) as e:
        raise SecTickerMapShapeError(
            f"company_tickers.json did not have the expected 'ticker'/'cik_str' "
            f"shape; top-level type was {type(data)!r}."
        ) from e
    if not cik_map:
        raise SecTickerMapShapeError("company_tickers.json parsed to zero ticker entries.")
    return cik_map


def fetch_book_equity_facts(
    cik: int, tag: str, session: requests.Session, headers: dict[str, str], timeout: float = settings.http_timeout_seconds
) -> pd.DataFrame:
    """One GET of companyconcept for one (cik, tag). Returns an empty
    DataFrame (ordinary absence, verified live to be SEC's real response)
    on HTTP 404. Raises SecAccessForbiddenError on HTTP 403.

    Returns columns ['end', 'val', 'filed', 'form', 'accn'], 'end'/'filed'
    parsed to pd.Timestamp immediately. Filters to USD facts only (a
    foreign-currency filer with no 'USD' unit key yields an empty frame,
    logged, not a crash -- see sec_edgar.py's module docstring on foreign
    private issuers).
    """
    url = SEC_COMPANY_CONCEPT_URL_TMPL.format(cik=cik, tag=tag)
    response = session.get(url, headers=headers, timeout=timeout)
    if response.status_code == 404:
        return pd.DataFrame(columns=["end", "val", "filed", "form", "accn"])
    if response.status_code == 403:
        raise SecAccessForbiddenError(f"SEC returned HTTP 403 fetching {url}; check SEC_UA.")
    response.raise_for_status()
    data = response.json()
    usd_facts = data.get("units", {}).get("USD")
    if not usd_facts:
        logger.info("no USD-denominated facts for cik=%s tag=%s (possibly a foreign filer)", cik, tag)
        return pd.DataFrame(columns=["end", "val", "filed", "form", "accn"])
    df = pd.DataFrame(usd_facts)
    df["end"] = pd.to_datetime(df["end"])
    df["filed"] = pd.to_datetime(df["filed"])
    return df[["end", "val", "filed", "form", "accn"]]


def fetch_company_facts(
    cik: int, session: requests.Session, headers: dict[str, str], timeout: float = settings.http_timeout_seconds
) -> dict:
    """One GET of the bulk companyfacts endpoint for one CIK -- used only
    as a recovery path when companyconcept (the normally-preferred,
    smaller, targeted endpoint) returns zero facts for every alias tag.

    Verified live: SEC's companyconcept endpoint can consistently
    (reproduced 3x, not transient) return zero USD facts for a tag that
    companyfacts shows has substantial real historical data for the exact
    same CIK+tag -- e.g. Abbott Laboratories' (CIK 1800) StockholdersEquity:
    0 facts via companyconcept, 140 via companyfacts, back to 2007. This is
    a genuine SEC API inconsistency (confirmed for 8+ tickers), not a bug
    in this code -- companyfacts is the more reliable source when
    companyconcept comes back empty, at the cost of a larger payload, so
    it's used only as a fallback, not the default.

    Returns {} on HTTP 404 (ordinary CIK absence). Raises
    SecAccessForbiddenError on HTTP 403.
    """
    url = SEC_COMPANY_FACTS_URL_TMPL.format(cik=cik)
    response = session.get(url, headers=headers, timeout=timeout)
    if response.status_code == 404:
        return {}
    if response.status_code == 403:
        raise SecAccessForbiddenError(f"SEC returned HTTP 403 fetching {url}; check SEC_UA.")
    response.raise_for_status()
    return response.json()


def extract_book_equity_facts_from_company_facts(company_facts: dict, tag: str) -> pd.DataFrame:
    """Pure. Extracts one us-gaap tag's USD facts from an already-fetched
    companyfacts blob, in the same shape fetch_book_equity_facts returns
    (['end', 'val', 'filed', 'form', 'accn']). Returns an empty DataFrame
    if the tag or its USD unit is absent -- ordinary absence, not an error.
    """
    gaap = company_facts.get("facts", {}).get("us-gaap", {})
    tag_data = gaap.get(tag)
    if not tag_data:
        return pd.DataFrame(columns=["end", "val", "filed", "form", "accn"])
    usd_facts = tag_data.get("units", {}).get("USD")
    if not usd_facts:
        return pd.DataFrame(columns=["end", "val", "filed", "form", "accn"])
    df = pd.DataFrame(usd_facts)
    df["end"] = pd.to_datetime(df["end"])
    df["filed"] = pd.to_datetime(df["filed"])
    return df[["end", "val", "filed", "form", "accn"]]


def lookup_cik(ticker: str, cik_map: dict[str, int]) -> int | None:
    """to_yfinance_symbol(ticker), uppercased, looked up in cik_map. Returns
    None (not raise) if absent -- ordinary per-ticker absence, verified live
    for delisted tickers already in this project's own unresolved_tickers
    table (e.g. TWTR, FRC).
    """
    symbol = to_yfinance_symbol(ticker).strip().upper()
    return cik_map.get(symbol)


def dedupe_to_earliest_filed(facts: pd.DataFrame) -> pd.DataFrame:
    """Group by (end, val), keep the row with the minimum 'filed' -- the
    earliest date that exact fact became public, discarding later filings'
    comparative restatements of the identical figure. Verified live this is
    necessary: AAPL's 2020-03-28 StockholdersEquity fact appears under 4
    different 'filed' dates with an identical val.

    Genuine restatements (different val, same end) survive as separate
    rows, since they group separately -- see select_book_equity_asof's tie
    break for how those are resolved at selection time.

    Returns columns ['end', 'val', 'filed'], sorted by 'end' ascending.
    """
    if facts.empty:
        return pd.DataFrame(columns=["end", "val", "filed"])
    deduped = facts.loc[facts.groupby(["end", "val"])["filed"].idxmin()]
    return deduped[["end", "val", "filed"]].sort_values("end").reset_index(drop=True)


def select_book_equity_asof(facts: pd.DataFrame, as_of) -> float | None:
    """Among deduped `facts` rows with filed <= as_of, pick the value at the
    largest 'end'; if multiple rows share that maximal 'end' (a real
    restatement), break the tie by the largest 'filed' still <= as_of --
    the most up-to-date figure for that period actually knowable by as_of.

    Returns None if no row has filed <= as_of -- ordinary "not yet publicly
    known", not an error. Uses filed <= as_of directly, with no added lag:
    SEC's filed date already IS the literal public-availability date this
    project's 3-month-lag approximation (used for the yfinance path) was
    trying to estimate -- using it directly is strictly more precise, not
    less conservative in any way that matters.
    """
    if facts is None or facts.empty:
        return None
    as_of_ts = pd.Timestamp(as_of)
    eligible = facts[facts["filed"] <= as_of_ts]
    if eligible.empty:
        return None
    max_end = eligible["end"].max()
    at_max_end = eligible[eligible["end"] == max_end]
    winner = at_max_end.loc[at_max_end["filed"].idxmax()]
    return float(winner["val"])


def select_book_equity_multi_tag(facts_by_tag: dict[str, pd.DataFrame], as_of) -> float | None:
    """Tries BOOK_EQUITY_XBRL_TAGS in order (as ordered in facts_by_tag's
    construction), returns the first tag's select_book_equity_asof result
    once any tag has non-empty facts -- never blends "excludes NCI" and
    "includes NCI" values within one ticker's timeline, mirroring
    fundamentals.py's find_book_equity_row alias-preference philosophy.
    """
    for tag in BOOK_EQUITY_XBRL_TAGS:
        facts = facts_by_tag.get(tag)
        if facts is not None and not facts.empty:
            value = select_book_equity_asof(facts, as_of)
            if value is not None:
                return value
    return None


def has_sufficient_coverage(facts: pd.DataFrame, latest_rebalance_date) -> bool:
    """True iff `facts` has at least one row with end <= latest_rebalance_date.

    This is the entity-succession detector: verified live it correctly
    flags XOM's new CIK (earliest fact end=2025-12-31, postdates this
    project's 2024-04-01 latest rebalance date) while not false-positiving
    on AAPL, and costs zero extra network calls since it reads data already
    fetched. Deliberately generous -- requires only ONE old-enough row, not
    full-window coverage -- so a genuine recent IPO with thin-but-real
    history isn't misclassified as an entity-succession case.

    (A tempting alternative -- checking submissions.json's 'tickers' field
    to pre-validate a CIK -- is verified actively wrong: the classic,
    correct XOM CIK 34088 itself has an empty 'tickers' field, so that
    check would reject the right entity too. Not used here.)
    """
    if facts is None or facts.empty:
        return False
    cutoff = pd.Timestamp(latest_rebalance_date)
    return bool((facts["end"] <= cutoff).any())


def fetch_all_sec_book_equity(
    tickers: list[str],
    cik_map: dict[str, int],
    latest_rebalance_date,
    session: requests.Session | None = None,
    pause_seconds: float = settings.sec_pause_seconds,
) -> tuple[dict[str, pd.DataFrame], dict[str, str]]:
    """Fetch-once-per-ticker: resolve CIK, try BOOK_EQUITY_XBRL_TAGS in
    order (first tag with any facts wins), check has_sufficient_coverage.

    Returns (book_equity_by_ticker, fallback_reason_by_ticker):
    book_equity_by_ticker holds the deduped facts DataFrame for every
    ticker with sufficient SEC coverage; fallback_reason_by_ticker names a
    reason for every OTHER requested ticker (no CIK found / zero facts
    under any alias tag / insufficient window coverage / fetch error) --
    exactly the set fundamentals.py's build_factors must resolve bm for via
    the existing yfinance path.

    SecAccessForbiddenError propagates uncaught (fail loud immediately on
    a broken SEC_UA, not after burning through hundreds of tickers). One
    shared requests.Session(); paced at pause_seconds after every
    companyconcept call, safely under SEC's ~10 req/sec guidance.
    """
    sess = session or requests.Session()
    headers = _sec_headers()
    book_equity_by_ticker: dict[str, pd.DataFrame] = {}
    fallback_reason_by_ticker: dict[str, str] = {}

    for i, ticker in enumerate(tickers, start=1):
        cik = lookup_cik(ticker, cik_map)
        if cik is None:
            fallback_reason_by_ticker[ticker] = "no CIK found in SEC's current ticker map"
            continue

        facts_by_tag: dict[str, pd.DataFrame] = {}
        try:
            for tag in BOOK_EQUITY_XBRL_TAGS:
                facts = fetch_book_equity_facts(cik, tag, sess, headers)
                facts_by_tag[tag] = dedupe_to_earliest_filed(facts) if not facts.empty else facts
                time.sleep(pause_seconds)
                if not facts.empty:
                    break  # first tag with any facts wins; don't fetch the fallback tag needlessly
        except SecAccessForbiddenError:
            raise
        except Exception:
            logger.warning("SEC fetch failed for %s (cik=%s); falling back to yfinance", ticker, cik, exc_info=True)
            fallback_reason_by_ticker[ticker] = f"SEC fetch error for cik={cik}"
            continue

        winning_facts = next((f for f in facts_by_tag.values() if not f.empty), pd.DataFrame())
        if winning_facts.empty:
            # Recovery: companyconcept can return empty for a tag that companyfacts
            # shows has real data (verified live, e.g. Abbott Laboratories) -- try
            # once more via the bulk endpoint before giving up on this ticker.
            try:
                company_facts = fetch_company_facts(cik, sess, headers)
                time.sleep(pause_seconds)
            except SecAccessForbiddenError:
                raise
            except Exception:
                logger.warning("companyfacts recovery fetch failed for %s (cik=%s)", ticker, cik, exc_info=True)
                company_facts = {}
            for tag in BOOK_EQUITY_XBRL_TAGS:
                recovered = extract_book_equity_facts_from_company_facts(company_facts, tag)
                if not recovered.empty:
                    winning_facts = dedupe_to_earliest_filed(recovered)
                    break

        if winning_facts.empty:
            fallback_reason_by_ticker[ticker] = (
                f"cik={cik} has zero facts under any of {BOOK_EQUITY_XBRL_TAGS} "
                "(checked both companyconcept and companyfacts)"
            )
        elif not has_sufficient_coverage(winning_facts, latest_rebalance_date):
            fallback_reason_by_ticker[ticker] = (
                f"cik={cik} has facts but none with end<={pd.Timestamp(latest_rebalance_date).date()} "
                "(likely entity succession -- ticker's current CIK postdates this project's window)"
            )
        else:
            book_equity_by_ticker[ticker] = winning_facts

        if i % 50 == 0:
            logger.info("fetched SEC book equity for %d/%d tickers", i, len(tickers))

    return book_equity_by_ticker, fallback_reason_by_ticker
