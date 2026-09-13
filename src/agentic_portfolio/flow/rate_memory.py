"""Persistence for the risk-free rate, remembered PER CURRENCY at
`memory/rates.json`.

A Sharpe ratio is `(annual return - risk-free rate) / annual volatility` -
return earned per unit of risk above what a riskless asset would have paid.
The riskless asset in question is a short-dated government bill in the
portfolio's own currency, so the rate is a property of a CURRENCY, not of a
portfolio. Before this file existed, every Sharpe ratio in this project was
measured against one global `settings.risk_free_rate` whose 2% default is a
dollar rate, which meant a yen portfolio was measured by subtracting the
wrong number. This is the third of this project's per-currency memory files
and it exists to fix exactly that:

    {"rates": {"USD": {"risk_free_rate": 0.0425, "updated_at": "..."},
               "JPY": {"risk_free_rate": 0.005,  "updated_at": "..."}}}

The top-level key is `"rates"`, not `"pools"` (`candidate_memory.py`) or
`"portfolios"` (`user_portfolio.py`), so a person who opens any of the three
can tell at a glance which one they have. There is no earlier shape to
migrate and no migration command: this file did not exist before
`plans/14_per_currency_risk_free_rate.md`.

Precedence, resolved by `resolve_risk_free_rate` and applied by the two CLI
layers: `--risk-free-rate` beats a stored rate, which beats `RISK_FREE_RATE`
from the environment or `.env`, which beats the built-in 2%. A stored rate
outranking the environment variable is deliberate - a per-currency entry is
the more specific statement, and one global variable cannot express "0.5%
for yen, 4.25% for dollars" at all.

There is deliberately NO table of per-currency default rates here, unlike
`src/agentic_portfolio/optimizer/benchmark.py`'s `DEFAULT_BENCHMARKS`. `SPY` is defensible as
a source constant because the S&P 500 has stood for "the US market" for
decades; a policy rate changes several times a year, so a table of them
compiled into this file would be wrong by the time it shipped and wrong
differently every quarter after. That is precisely why this value has to be
remembered from the user rather than assumed - and why the report always
prints which source a rate came from.

Only an EXPLICIT choice is ever written here, never the configured default -
the rule `DEFAULT_BENCHMARKS`' docstring states for `memory/candidates.json`,
and for the same reason: improving the default later still reaches every
currency that never named one.

`memory/` is gitignored, so nothing written here is ever committed. This
module performs no network calls and, like both its sibling memory modules,
imports nothing from `src/agentic_portfolio/config`, `src/agentic_portfolio/optimizer`, or any other `src/agentic_portfolio/flow`
module - which is why `resolve_risk_free_rate` takes its fallback as a
parameter rather than reading the `settings` singleton.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple

from agentic_portfolio.dataset.ticker_currency import DEFAULT_CURRENCY
from agentic_portfolio.config.settings import settings

DEFAULT_RATES_PATH = settings.rates_path

MAX_ABS_RISK_FREE_RATE = 1.0
"""Magnitude at or above which a rate is refused outright.

Rates in this project are decimals, so 4.25% is `0.0425` and a value of 1 or
more is a mistyped percentage rather than a rate anybody means. It is refused
rather than warned about because a remembered rate is used again on every
later run: `--risk-free-rate 4.5` would warn once, on a line the user may not
read, and then make every subsequent `--objective MSR` run fail inside
PyPortfolioOpt ("at least one of the assets must have an expected return
exceeding the risk-free rate") in a message naming neither the rate nor this
file - while GMV and MV would keep working and quietly report a Sharpe ratio
around -29. Refusing at the door removes the trap instead of documenting it.
"""


def validate_risk_free_rate(rate: object, source: str) -> float:
    """`rate` as a float, refusing anything that is not a usable rate and
    naming `source` (a command-line flag, or a file and currency) in the
    message.

    The single gate every rate passes through, whichever direction it came
    from. A value hand-edited into `memory/rates.json` gets exactly the same
    scrutiny as one typed on the command line, because a bad stored value is
    worse than a bad typed one: it is used again on every later run, and its
    complaint would otherwise be printed once and never again.

    Booleans are rejected ahead of the numeric check because
    `isinstance(True, int)` is true in Python, so `{"risk_free_rate": true}`
    would otherwise become a 100% rate - and PyPortfolioOpt's own guard has
    the same hole, so nothing downstream would catch it either.

    Non-finite values are rejected because `argparse`'s `type=float` accepts
    `nan` and `inf` quite happily, and a `nan` rate propagates silently into
    a `nan` Sharpe ratio rather than failing.

    NEGATIVE rates are accepted. Japan ran a negative policy rate for years,
    and this module exists precisely to stop measuring a yen portfolio
    against a dollar assumption; refusing negatives would reintroduce that
    bug for the one currency that motivated the change.
    """
    if isinstance(rate, bool) or not isinstance(rate, (int, float)):
        raise ValueError(f"{source} must be a number, got {rate!r}")

    value = float(rate)
    if not math.isfinite(value):
        raise ValueError(f"{source} must be a finite number, got {rate!r}")
    if abs(value) >= MAX_ABS_RISK_FREE_RATE:
        raise ValueError(
            f"{source} must be a decimal rate smaller than {MAX_ABS_RISK_FREE_RATE} in magnitude, "
            f"got {rate!r}; rates are decimals here, so 4.5% is 0.045"
        )
    return value


def _load_raw_rates(path: str) -> dict[str, dict]:
    """Every saved rate at `path` as `{CURRENCY: {"risk_free_rate": float,
    "updated_at": ...}}`, validating each entry. `{}` when the file does not
    exist - the expected state before anyone has remembered a rate, which is
    why it is not an error.

    Currency keys are upper-cased here, not merely on write. Neither sibling
    memory module normalizes them, so a hand-edited `{"rates": {"jpy": ...}}`
    would otherwise produce a `"jpy"` entry distinct from `"JPY"` - two rates
    for one currency, with no way to tell which a report used.

    A malformed JSON file lets `json.JSONDecodeError` propagate unchanged.
    The file is hand-editable, so a syntax error in it is a real problem the
    user must see; swallowing it into an empty result would look exactly like
    "no rate remembered" and quietly measure against the wrong number, which
    is the bug this module exists to fix.
    """
    file_path = Path(path)
    if not file_path.exists():
        return {}

    data = json.loads(file_path.read_text())
    raw = data.get("rates", {})
    return {
        currency.strip().upper(): {
            "risk_free_rate": validate_risk_free_rate(
                entry.get("risk_free_rate"), f"{path!r}'s {currency!r} entry"
            ),
            "updated_at": entry.get("updated_at"),
        }
        for currency, entry in raw.items()
    }


def load_all_risk_free_rates(path: str = DEFAULT_RATES_PATH) -> dict[str, float]:
    """`{CURRENCY: rate}` for every rate saved at `path`, or `{}` if the file
    does not exist yet.

    Raises `ValueError`, naming the file and the offending currency, if an
    entry's `risk_free_rate` is missing or is not a usable rate.
    """
    return {currency: entry["risk_free_rate"] for currency, entry in _load_raw_rates(path).items()}


def load_risk_free_rate(
    path: str = DEFAULT_RATES_PATH, currency: str = DEFAULT_CURRENCY
) -> float | None:
    """The rate remembered for `currency` at `path`, or `None` if none has
    ever been remembered for it.

    `None` rather than the configured default, because the caller has to be
    able to tell "nobody chose one" from "somebody chose 2%" in order to
    report the difference - see `resolve_risk_free_rate`.
    """
    return load_all_risk_free_rates(path).get(currency.strip().upper())


def save_risk_free_rate(
    rate: float, path: str = DEFAULT_RATES_PATH, currency: str = DEFAULT_CURRENCY
) -> bool:
    """Remember `rate` as `currency`'s risk-free rate at `path`, returning
    whether anything was actually written.

    `False` means the stored value was already identical and the file was left
    alone, so `updated_at` records when the rate last CHANGED rather than when
    it was last restated - which matters because every run that passes
    `--risk-free-rate` arrives here. This is `migrate_candidate_pools`'
    reasoning ("that would stamp the current time over the only record of when
    the pool was last curated") applied to a value that is re-stated often.
    Callers use the return value to decide whether to announce the write.

    Every other currency already saved at `path` is read first and carried
    over untouched, keeping its own rate AND its own `updated_at`, so
    remembering a dollar rate can never disturb the yen one or make it look
    freshly chosen.

    The write is atomic - a temp file in the same directory, then
    `os.replace`. `Path.write_text` truncates before writing, and unlike
    `memory/candidates.json` (read only by the `user_provided` selection) or
    `memory/portfolio.json` (read only by the holdings block), this file sits
    on the startup path of BOTH commands for EVERY currency, so an
    interrupted save would break everything rather than one feature.
    """
    currency = currency.strip().upper()
    validated = validate_risk_free_rate(rate, "--risk-free-rate")

    rates = _load_raw_rates(path)
    if currency in rates and rates[currency]["risk_free_rate"] == validated:
        return False

    rates[currency] = {
        "risk_free_rate": validated,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    _write_rates(path, rates)
    return True


def _write_rates(path: str, rates: dict[str, dict]) -> None:
    """Write `rates` to `path` atomically, creating the parent directory if
    needed.

    The temp file is created in the destination's own directory so that
    `os.replace` is a rename within one filesystem, which is the part that is
    atomic; a temp file in `/tmp` could land on a different device and fall
    back to a copy.
    """
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"rates": rates}, indent=2)

    handle, temp_path = tempfile.mkstemp(dir=str(file_path.parent), prefix=".rates-", suffix=".json")
    try:
        with os.fdopen(handle, "w") as temp_file:
            temp_file.write(payload)
        os.replace(temp_path, file_path)
    except BaseException:
        Path(temp_path).unlink(missing_ok=True)
        raise


class ResolvedRiskFreeRate(NamedTuple):
    """One resolved rate together with where it came from.

    `origin` is a human-readable phrase for the report to print in
    parentheses after the rate, and it is populated in EVERY case, including
    the plain default. A report that named its source only sometimes would
    let a call site somebody forgot to wire print a bare, entirely plausible
    `Risk-free rate used: 0.0200` with no test failing - which is the bug
    this whole module exists to fix, in miniature.

    `from_override` says whether this value came from the command line, and
    therefore whether it is the caller's to remember. A default or an
    already-stored rate is not written back: see this module's docstring on
    why only explicit choices are recorded.
    """

    rate: float
    origin: str
    from_override: bool


def resolve_risk_free_rate(
    override: float | None, currency: str, saved: float | None, default: float
) -> ResolvedRiskFreeRate:
    """Which rate to use for `currency`, and what to call its source.

    Precedence is `override` (this run's `--risk-free-rate`), then `saved`
    (what `currency` remembered), then `default` (the caller's
    `settings.risk_free_rate`, which itself already reflects any
    `RISK_FREE_RATE` environment variable or `.env` entry).

    Pure, and takes `saved` and `default` as parameters rather than reading a
    file or the settings singleton - the same shape
    `src/agentic_portfolio/optimizer/benchmark.py`'s `resolve_benchmark_ticker` uses. This
    function's purity is the point and is unchanged; note that the module does
    now import `agentic_portfolio.config.settings`, but only for
    `DEFAULT_RATES_PATH` above, so that the remembered-rates file follows
    `AGENTIC_PORTFOLIO_HOME` like every other path. Nothing in the resolution
    below reads it.

    `currency` drives ONLY the wording of `origin`. It is not looked up in
    any table of per-currency defaults, because deliberately no such table
    exists - see this module's docstring for why a compiled-in rate would
    rot.
    """
    currency = currency.strip().upper()
    if override is not None:
        return ResolvedRiskFreeRate(
            rate=float(override),
            origin=f"--risk-free-rate, remembered for {currency}",
            from_override=True,
        )
    if saved is not None:
        return ResolvedRiskFreeRate(
            rate=float(saved), origin=f"remembered for {currency}", from_override=False
        )
    return ResolvedRiskFreeRate(
        rate=float(default), origin="the configured default", from_override=False
    )
