"""Persistence for the user's OWN portfolio - the tickers they actually
hold and how many shares of each - stored at `memory/portfolio.json`.

This is a different thing from `src/agentic_portfolio/flow/candidate_memory.py`'s
`memory/candidates.json`, and the two are deliberately separate files. A
candidate pool is a list of tickers under *consideration*, rewritten
wholesale every time the `user_provided` confirm loop runs. A portfolio is
a record of *fact* that changes only when the user trades. Folding the
positions into a pool entry would mean an ordinary candidate edit rewrites
the record of what the user owns, and would force every reader of one
concept to parse the other.

Like candidate pools - and for the same reason, `src/agentic_portfolio/dataset/
ticker_currency.py`'s single-currency rule - this file holds one portfolio
PER CURRENCY rather than one flat list, since prices in two different units
cannot be weighed against one total:

    {"portfolios": {"USD": {"positions": {"SPY": 1000.0, "T": 500.0},
                            "position_updated_at": {"SPY": "...", "T": "..."},
                            "applied_splits": {},
                            "updated_at": "..."},
                    "JPY": {"positions": {"1321.T": 50.0},
                            "updated_at": "..."}}}

The top-level key is `"portfolios"`, not `"pools"`, so that a person who
opens either file can tell at a glance which one they have. Old entries that
predate per-ticker split tracking remain readable: their portfolio-level
`updated_at` is the initial basis for every ticker, and the richer metadata
is materialized on the next write rather than by a separate migration.

Share counts are floats, not ints, because brokers sell fractional shares.
A non-positive count is never stored: zero means "retire this holding" (it
is how `uv run portfolio-holdings set SPY 0` works) and a negative count is
refused outright, since this project has no model of a short position and
silently storing one would produce a negative weight that
`src/agentic_portfolio/optimizer/holdings.py`'s arithmetic would accept and quietly
misreport.

`memory/` is gitignored, so nothing written here is ever committed - what
`AGENTS.md`'s security guidance requires of local watchlist data. This
module performs no network calls and imports nothing from `src/agentic_portfolio/optimizer`
or from any other `src/agentic_portfolio/flow` module, keeping persistence independent of the
arithmetic that consumes it.
"""

from __future__ import annotations

import json
import math
from datetime import date, datetime, timezone
from pathlib import Path
from typing import NamedTuple

import pandas as pd

from agentic_portfolio.dataset.ticker_currency import DEFAULT_CURRENCY
from agentic_portfolio.config.settings import settings

DEFAULT_PORTFOLIO_PATH = settings.portfolio_path


def _validate_share_count(path: str, currency: str, ticker: str, shares: object) -> float:
    """`shares` as a float, refusing anything that is not a finite,
    non-negative number.

    Booleans are rejected explicitly ahead of the numeric check because
    `isinstance(True, int)` is true in Python, and `{"SPY": true}` is a
    typo, not a share count.

    A negative count is refused rather than stored: this project has no
    model of a short position, and a negative weight would flow silently
    into the return/volatility arithmetic. Zero IS accepted here - it is
    the caller's way of retiring a holding - and dropped by
    `_validate_positions` rather than written.
    """
    if isinstance(shares, bool) or not isinstance(shares, (int, float)):
        raise ValueError(
            f"{path!r}'s {currency!r} portfolio has a share count for {ticker!r} that must be a "
            f"number, got {shares!r}"
        )
    value = float(shares)
    if not math.isfinite(value):
        raise ValueError(
            f"{path!r}'s {currency!r} portfolio has a share count for {ticker!r} that must be "
            f"finite, got {shares!r}"
        )
    if value < 0:
        raise ValueError(
            f"{path!r}'s {currency!r} portfolio has a negative share count for {ticker!r} "
            f"({shares!r}); this project has no model of a short position"
        )
    return value


def _validate_positions(path: str, currency: str, positions: object) -> dict[str, float]:
    """`positions` as `{TICKER: shares}`, upper-cased and with every
    non-positive holding dropped.

    Mirrors `candidate_memory._validate_tickers`: a wrong type is reported
    against the file AND the currency it was found under, because a
    malformed value discovered here is fixable, whereas the same value
    surfacing later inside a Yahoo Finance price lookup is a confusing
    failure a long way from its cause. This file is hand-editable, so that
    distinction is a real one.
    """
    if not isinstance(positions, dict) or not all(isinstance(t, str) for t in positions):
        raise ValueError(
            f"{path!r}'s {currency!r} portfolio must contain a 'positions' field holding an "
            f"object mapping tickers to share counts, got {positions!r}"
        )

    validated = {
        ticker.strip().upper(): _validate_share_count(path, currency, ticker, shares)
        for ticker, shares in positions.items()
    }
    return {ticker: shares for ticker, shares in validated.items() if shares > 0}


def _load_raw_portfolios(path: str) -> dict[str, dict]:
    """Every saved portfolio at `path` as `{currency: {"positions": {...},
    "updated_at": ...}}`, validating each entry. `{}` when the file does not
    exist - the expected state before the user has ever saved a portfolio,
    which is why it is not an error.

    A malformed JSON file lets `json.JSONDecodeError` propagate unchanged.
    The file is hand-editable, so a syntax error in it is a real problem the
    user must see; swallowing it into an empty result would look exactly
    like "no holdings" and quietly report the wrong thing.
    """
    file_path = Path(path)
    if not file_path.exists():
        return {}

    data = json.loads(file_path.read_text())
    raw = data.get("portfolios", {})
    portfolios: dict[str, dict] = {}
    for currency, entry in raw.items():
        positions = _validate_positions(path, currency, entry.get("positions"))
        position_updated_at = entry.get("position_updated_at", {})
        if not isinstance(position_updated_at, dict):
            raise ValueError(
                f"{path!r}'s {currency!r} portfolio has 'position_updated_at' that must be "
                f"an object, got {position_updated_at!r}"
            )
        applied_splits = entry.get("applied_splits", {})
        if not isinstance(applied_splits, dict):
            raise ValueError(
                f"{path!r}'s {currency!r} portfolio has 'applied_splits' that must be an "
                f"object, got {applied_splits!r}"
            )

        clean_applied: dict[str, dict[str, float]] = {}
        for ticker, events in applied_splits.items():
            normalized = str(ticker).strip().upper()
            if normalized not in positions:
                continue
            if not isinstance(events, dict):
                raise ValueError(
                    f"{path!r}'s {currency!r} portfolio has applied splits for {ticker!r} "
                    f"that must map dates to ratios, got {events!r}"
                )
            clean_events: dict[str, float] = {}
            for event_date, ratio in events.items():
                try:
                    date.fromisoformat(str(event_date))
                    numeric_ratio = float(ratio)
                except (TypeError, ValueError):
                    raise ValueError(
                        f"{path!r}'s {currency!r} portfolio has an invalid applied split "
                        f"for {ticker!r}: {event_date!r} -> {ratio!r}"
                    ) from None
                if not math.isfinite(numeric_ratio) or numeric_ratio <= 0:
                    raise ValueError(
                        f"{path!r}'s {currency!r} portfolio has a split ratio for {ticker!r} "
                        f"that must be finite and positive, got {ratio!r}"
                    )
                clean_events[str(event_date)] = numeric_ratio
            if clean_events:
                clean_applied[normalized] = clean_events

        portfolios[currency] = {
            "positions": positions,
            "updated_at": entry.get("updated_at"),
            "position_updated_at": {
                str(ticker).strip().upper(): stamp
                for ticker, stamp in position_updated_at.items()
                if str(ticker).strip().upper() in positions
            },
            "applied_splits": clean_applied,
        }
    return portfolios


def load_all_portfolios(path: str = DEFAULT_PORTFOLIO_PATH) -> dict[str, dict[str, float]]:
    """`{currency: {TICKER: shares}}` for every portfolio saved at `path`, or
    `{}` if the file does not exist yet.

    Raises `ValueError`, naming the file and the offending currency, if a
    portfolio's `positions` field is missing, is not an object of tickers to
    numbers, or holds a negative or non-finite share count.
    """
    return {currency: entry["positions"] for currency, entry in _load_raw_portfolios(path).items()}


def load_portfolio(
    path: str = DEFAULT_PORTFOLIO_PATH, currency: str = DEFAULT_CURRENCY
) -> dict[str, float]:
    """`{TICKER: shares}` for `currency`'s portfolio at `path`, or `{}` if
    that portfolio has never been saved (or holds nothing).
    """
    return load_all_portfolios(path).get(currency, {})


def portfolio_updated_at(
    path: str = DEFAULT_PORTFOLIO_PATH, currency: str = DEFAULT_CURRENCY
) -> datetime | None:
    """When `currency`'s portfolio at `path` was last written, or `None` when
    it has never been saved or carries no timestamp.

    Retained for callers of the original warning-only split API. Current
    report reconciliation uses each ticker's `position_updated_at` instead.

    `load_portfolio` deliberately returns only the positions, since that is
    all a measurement needs. This exists for the one question the positions
    cannot answer: whether they are still describing the same shares. A
    stock split multiplies a holding without anybody trading, so a share
    count recorded before a split is simply wrong afterwards - and the only
    thing that can date the count is this timestamp.

    `None` is returned rather than a sentinel date because "we do not know
    when this was written" and "this was written long ago" call for
    different behavior: see `stale_share_counts`, which stays silent on the
    first rather than guessing.
    """
    raw = _load_raw_portfolios(path).get(currency)
    if not raw:
        return None
    stamp = raw.get("updated_at")
    if not stamp:
        return None
    try:
        return datetime.fromisoformat(str(stamp))
    except ValueError:
        # Hand-editable file: an unparseable timestamp is treated as absent
        # rather than raised, because a bad date must not stop somebody
        # seeing what they own.
        return None


class StaleShareCount(NamedTuple):
    """One holding whose stored share count predates a split, and therefore
    probably understates what is actually held.

    `ratio` is the cumulative product of every split since the count was
    written, and `likely_shares` is `stored_shares * ratio` - a suggestion
    to be confirmed, never applied. `ex_date` is the earliest offending
    split, which is the one that dates the problem.
    """

    ticker: str
    stored_shares: float
    ratio: float
    likely_shares: float
    ex_date: date


class SplitAdjustment(NamedTuple):
    """One saved position changed by one or more newly observed splits."""

    ticker: str
    before_shares: float
    after_shares: float
    events: tuple[tuple[date, float], ...]


class SplitReconciliation(NamedTuple):
    """The corrected positions and an audit trail for one reconciliation."""

    positions: dict[str, float]
    adjustments: tuple[SplitAdjustment, ...]
    undated_tickers: tuple[str, ...]


def stale_share_counts(
    positions: dict[str, float],
    updated_at: datetime | None,
    splits: dict[str, "pd.Series"],
) -> dict[str, StaleShareCount]:
    """Which of `positions` were recorded before a split, as
    `{ticker: StaleShareCount}` - empty in the ordinary case where nothing
    split since the portfolio was written.

    This legacy pure detector remains for compatibility and tests of the old
    warning calculation. Reports now call `reconcile_portfolio_splits`,
    which has per-ticker bases and persists confirmed events idempotently.

    Why this is needed at all: `memory/portfolio.json` stores raw share
    counts with no split awareness. Record 1,000 shares, have the stock
    split 4:1, never run `set` again, and the file still says 1,000 while
    the holding is 4,000 - understating the total value, every weight and
    every dividend figure by fourfold, with nothing anywhere to notice. The
    figures stay perfectly self-consistent, which is what makes it
    dangerous.

    `splits` maps a ticker to its split history as a date-indexed Series,
    the shape `src/agentic_portfolio/dataset/fundamentals.py`'s `cumulative_split_ratio_after`
    already takes; `src/agentic_portfolio/dataset/dividends.py`'s `split_series_by_ticker`
    builds it. That function is reused rather than reimplemented, so a
    ticker that never split costs a 1.0 and no special case.

    Two limits, stated rather than papered over. `updated_at` is recorded
    per CURRENCY and reflects the LAST edit to that portfolio, so a
    portfolio touched after a split is taken as current for all of its
    tickers - which means setting A in November, B in January and a
    December split leaves A's staleness invisible. The check can therefore
    MISS a stale count but can never invent one, and that asymmetry is
    deliberate: telling somebody to quadruple a holding that is already
    right would be a far worse failure than staying quiet. Per-ticker
    timestamps close this gap in the current persistence format, but this
    compatibility helper accepts only one timestamp. An absent `updated_at`
    yields nothing at all, for the same reason - an undated count cannot be judged.

    Pure, no I/O.
    """
    from agentic_portfolio.dataset.fundamentals import cumulative_split_ratio_after

    if updated_at is None or not positions or not splits:
        return {}

    stale: dict[str, StaleShareCount] = {}
    for ticker, shares in positions.items():
        series = splits.get(ticker)
        if series is None or series.empty:
            continue
        ratio = cumulative_split_ratio_after(series, updated_at)
        if ratio == 1.0:
            continue
        after = [
            ts for ts in series.index
            if pd.Timestamp(ts).tz_localize(None).normalize()
            > pd.Timestamp(updated_at).tz_localize(None).normalize()
        ]
        stale[ticker] = StaleShareCount(
            ticker=ticker,
            stored_shares=float(shares),
            ratio=float(ratio),
            likely_shares=float(shares) * float(ratio),
            ex_date=min(pd.Timestamp(ts).date() for ts in after),
        )
    return stale


def _parse_timestamp(value: object) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _write_portfolios(path: str, portfolios: dict[str, dict]) -> None:
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(json.dumps({"portfolios": portfolios}, indent=2))


def reconcile_portfolio_splits(
    path: str,
    currency: str,
    as_of: date,
    splits: dict[str, "pd.Series"],
) -> SplitReconciliation:
    """Apply confirmed splits to a saved portfolio exactly once.

    A ticker's manual basis is its own `position_updated_at`, falling back
    to the legacy portfolio-level `updated_at`. Split dates after that basis
    and on or before `as_of` multiply the stored count unless their date is
    already in `applied_splits`. The manual basis deliberately does not move
    when this function writes: if an older event arrives from Yahoo late, it
    still qualifies while already-applied dates remain idempotent.

    Tickers with split history but no trustworthy basis are returned in
    `undated_tickers` and never guessed. The file is written at most once.
    """
    portfolios = _load_raw_portfolios(path)
    entry = portfolios.get(currency)
    if not entry or not entry["positions"]:
        return SplitReconciliation(dict(entry["positions"]) if entry else {}, (), ())

    positions = dict(entry["positions"])
    bases = dict(entry.get("position_updated_at", {}))
    applied = {
        ticker: dict(events) for ticker, events in entry.get("applied_splits", {}).items()
    }
    adjustments: list[SplitAdjustment] = []
    undated: list[str] = []

    for ticker, before in positions.items():
        series = splits.get(ticker)
        if series is None or series.empty:
            continue

        # Yahoo supplies at most one event per date, but multiplying a group
        # makes the persistence contract deterministic if duplicate rows are
        # ever present.
        events_by_date: dict[date, float] = {}
        for timestamp, raw_ratio in series.items():
            event_date = pd.Timestamp(timestamp).date()
            if event_date > as_of:
                continue
            ratio = float(raw_ratio)
            if not math.isfinite(ratio) or ratio <= 0:
                raise ValueError(
                    f"split ratio for {ticker} on {event_date} must be finite and positive, "
                    f"got {raw_ratio!r}"
                )
            events_by_date[event_date] = events_by_date.get(event_date, 1.0) * ratio

        if not events_by_date:
            continue
        basis_value = bases.get(ticker, entry.get("updated_at"))
        basis = _parse_timestamp(basis_value)
        if basis is None:
            undated.append(ticker)
            continue
        # Materialize the legacy fallback without changing its meaning.
        bases[ticker] = str(basis_value)

        already = applied.setdefault(ticker, {})
        new_events = tuple(
            (event_date, ratio)
            for event_date, ratio in sorted(events_by_date.items())
            if event_date > basis.date() and event_date.isoformat() not in already
        )
        if not new_events:
            continue

        combined_ratio = math.prod(ratio for _event_date, ratio in new_events)
        after = float(before) * combined_ratio
        positions[ticker] = after
        for event_date, ratio in new_events:
            already[event_date.isoformat()] = ratio
        adjustments.append(
            SplitAdjustment(ticker, float(before), after, new_events)
        )

    if adjustments:
        now = datetime.now(timezone.utc).isoformat()
        portfolios[currency] = {
            "positions": positions,
            "position_updated_at": {
                ticker: bases[ticker] for ticker in positions if ticker in bases
            },
            "applied_splits": {
                ticker: applied[ticker]
                for ticker in positions
                if applied.get(ticker)
            },
            "updated_at": now,
        }
        _write_portfolios(path, portfolios)

    return SplitReconciliation(
        positions,
        tuple(adjustments),
        tuple(sorted(undated)),
    )


def save_portfolio(
    positions: dict[str, float],
    path: str = DEFAULT_PORTFOLIO_PATH,
    currency: str = DEFAULT_CURRENCY,
    updated_tickers: set[str] | None = None,
) -> None:
    """Write `positions` as `currency`'s portfolio at `path`, together with
    the current UTC timestamp, creating `path`'s parent directory if needed.

    `updated_tickers` identifies a partial manual edit. Those tickers get a
    fresh per-position basis and their applied-split history is cleared;
    every other ticker preserves both. `None` retains the original whole-
    snapshot API by treating every surviving ticker as manually updated.

    Every other currency already saved at `path` is read first and carried
    over untouched, keeping its own positions AND its own `updated_at`, so
    saving the USD portfolio can never disturb the JPY one's holdings or
    make it look freshly edited.

    An empty `positions` writes an entry with an empty `positions` object
    rather than removing the currency: "I sold everything I held in yen" is
    a fact worth recording, and it keeps this function total - every call
    leaves the file describing exactly what the caller said.
    """
    portfolios = _load_raw_portfolios(path)
    previous = portfolios.get(currency, {})
    validated = _validate_positions(path, currency, positions)
    normalized_updated = (
        set(validated)
        if updated_tickers is None
        else {ticker.strip().upper() for ticker in updated_tickers}
    )
    now = datetime.now(timezone.utc).isoformat()

    previous_bases = previous.get("position_updated_at", {})
    legacy_basis = previous.get("updated_at")
    bases = {
        ticker: previous_bases.get(ticker, legacy_basis)
        for ticker in validated
        if previous_bases.get(ticker, legacy_basis)
    }
    applied = {
        ticker: dict(events)
        for ticker, events in previous.get("applied_splits", {}).items()
        if ticker in validated
    }
    for ticker in normalized_updated & set(validated):
        bases[ticker] = now
        applied.pop(ticker, None)

    portfolios[currency] = {
        "positions": validated,
        "position_updated_at": bases,
        "applied_splits": applied,
        "updated_at": now,
    }
    _write_portfolios(path, portfolios)
