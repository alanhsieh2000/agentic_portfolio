"""Persistence for the user's OWN portfolio - the tickers they actually
hold and how many shares of each - stored at `memory/portfolio.json`.

This is a different thing from `src/flow/candidate_memory.py`'s
`memory/candidates.json`, and the two are deliberately separate files. A
candidate pool is a list of tickers under *consideration*, rewritten
wholesale every time the `user_provided` confirm loop runs. A portfolio is
a record of *fact* that changes only when the user trades. Folding the
positions into a pool entry would mean an ordinary candidate edit rewrites
the record of what the user owns, and would force every reader of one
concept to parse the other.

Like candidate pools - and for the same reason, `src/dataset/
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
`src/optimizer/holdings.py`'s arithmetic would accept and quietly
misreport.

`memory/` is gitignored, so nothing written here is ever committed - what
`AGENTS.md`'s security guidance requires of local watchlist data. This
module performs no network calls and imports nothing from `src/optimizer`
or from any other `src/flow` module, keeping persistence independent of the
arithmetic that consumes it.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path

from src.dataset.ticker_currency import DEFAULT_CURRENCY

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
