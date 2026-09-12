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
                            "updated_at": "..."},
                    "JPY": {"positions": {"1321.T": 50.0},
                            "updated_at": "..."}}}

The top-level key is `"portfolios"`, not `"pools"`, so that a person who
opens either file can tell at a glance which one they have. There is no
earlier shape to migrate: this file did not exist before
`plans/13_user_portfolio.md`, so unlike `candidate_memory.py` there is no
legacy branch and no migration command.

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

DEFAULT_PORTFOLIO_PATH = "memory/portfolio.json"


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
    return {
        currency: {
            "positions": _validate_positions(path, currency, entry.get("positions")),
            "updated_at": entry.get("updated_at"),
        }
        for currency, entry in raw.items()
    }


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


def stale_share_counts(
    positions: dict[str, float],
    updated_at: datetime | None,
    splits: dict[str, "pd.Series"],
) -> dict[str, StaleShareCount]:
    """Which of `positions` were recorded before a split, as
    `{ticker: StaleShareCount}` - empty in the ordinary case where nothing
    split since the portfolio was written.

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
    timestamps would close the gap and are a `memory/portfolio.json` format
    change, not attempted here. And an absent `updated_at` yields nothing
    at all, for the same reason - an undated count cannot be judged.

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


def save_portfolio(
    positions: dict[str, float],
    path: str = DEFAULT_PORTFOLIO_PATH,
    currency: str = DEFAULT_CURRENCY,
) -> None:
    """Write `positions` as `currency`'s portfolio at `path`, together with
    the current UTC timestamp, creating `path`'s parent directory if needed.

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
    portfolios[currency] = {
        "positions": _validate_positions(path, currency, positions),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }

    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(json.dumps({"portfolios": portfolios}, indent=2))
