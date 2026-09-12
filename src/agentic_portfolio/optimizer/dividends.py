"""The minimum expected-dividend constraint: the floor a user asks for, the
per-ticker yield vector it is enforced against, and the refusals it can
produce.

Everything here is pure - no DuckDB, no yfinance, nothing from `src/agentic_portfolio/flow` -
so `src/agentic_portfolio/optimizer/portfolio.py` can type its new parameters against these
names without acquiring a dependency on the dataset layer, and every rule
below is testable against a hand-built dict.

What the constraint IS, in plain terms. Each ticker has a trailing
twelve-month dividend yield: the cash it paid per share over the last year
divided by the price of one share. A portfolio's yield is the weighted
average of its holdings' yields, weighted by how much money sits in each.
Requiring that average to be at least some floor is a single linear
inequality on the weights, which is why PyPortfolioOpt can enforce it
exactly rather than approximately, and why it composes with all three of
this project's objectives instead of replacing any of them.

One thing worth saying plainly, because it surprises people. This project's
expected returns come from the `returns` table, which is built from
`adj_close` - a dividend-ADJUSTED price - so those returns are TOTAL
returns and already contain dividends. A dividend floor therefore adds no
new source of return. It constrains the COMPOSITION of a return the
optimizer already sees, shifting it from price appreciation toward cash
income. A portfolio's expected total return will usually FALL when the
floor binds, because a floor can only shrink the set of portfolios the
optimizer may choose from. That is the trade being made, not a bug, and the
report says so.
"""

from __future__ import annotations

import math
from datetime import date
from collections.abc import Sequence
from typing import NamedTuple

import numpy as np

from agentic_portfolio.errors import UnsatisfiableRequestError

MAX_DIVIDEND_YIELD = 0.25
"""Ceiling on an acceptable dividend-yield FLOOR, refusing a percentage
typed as a decimal.

`src/agentic_portfolio/flow/rate_memory.py`'s `MAX_ABS_RISK_FREE_RATE = 1.0` reasoning applied
to a yield. Yields here are decimals, so `--min-dividend-yield 3` means 300%
and is certainly a user who meant 3%. The cutoff is 0.25 rather than 1.0
because the distributions are different: no long-only portfolio of ordinary
listed equities sustains a 25% trailing yield, whereas 4% is routine, so a
tighter bound catches more typos without refusing anything real. The highest
mainstream yielders - mortgage REITs, covered-call and leveraged preferred
funds - sit in the 8-15% band, comfortably inside it.
"""

DIVIDEND_FEASIBILITY_TOLERANCE = 1e-9
"""Slack in the pre-solve ceiling check, so a floor that lands a float's
width above the ceiling is left for the solver to judge rather than refused
by a rounding error. `--min-annual-dividend`'s own division by `--value` is
the likeliest way to land there."""

DIVIDEND_BINDING_TOLERANCE = 1e-6
"""How close a realized yield must sit to the floor to be called binding,
and the slack in the post-solve verification.

Sized against a measured effect rather than guessed: `clean_weights()`
rounds to five decimals and clips below 1e-4, which moved a solved yield by
1.9e-7 in testing. That is well inside this tolerance and nowhere near the
1e-4 `MV_RETURN_TOLERANCE` uses for the same job on returns.
"""


class DividendFloorError(UnsatisfiableRequestError):
    """A dividend floor that no portfolio of these candidates can meet.

    An `UnsatisfiableRequestError` (and so still a `ValueError`) for
    precisely the reason
    `src/agentic_portfolio/dataset/ticker_currency.py`'s `MixedCurrencyPoolError` is one: the
    interactive edit loop in `src/agentic_portfolio/flow/cli.py` catches `ValueError` to
    revert a rejected edit, and that loop holds live mode's ONLY fetched
    snapshot open. PyPortfolioOpt's own `OptimizationError` subclasses plain
    `Exception`, so an unwrapped solver infeasibility would sail past that
    handler and destroy the snapshot along with the user's confirmed pool.
    Every dividend-shaped refusal in this project therefore arrives as this
    type.
    """


class DividendYieldUnavailableError(UnsatisfiableRequestError):
    """A dividend floor was asked for over a pool containing a ticker whose
    trailing yield is not known.

    Deliberately a different class from `DividendFloorError`: that one means
    "the answer is no", this one means "the question cannot be asked". A
    missing yield is not a zero yield, and the gap between those two is the
    gap between a portfolio that pays nothing and a portfolio nobody
    measured. Also an `UnsatisfiableRequestError`, and so still a
    `ValueError`, for the same edit-loop reason.
    """


class DividendFloor(NamedTuple):
    """One minimum-dividend request: the single number the optimizer needs,
    plus the provenance a report has to print beside it.

    `yield_floor` is what reaches the solver, and it is always a yield -
    both CLI flags reduce to one linear constraint, and a cash amount is
    just a yield that has not been divided by `--value` yet. `cash_floor`
    and `portfolio_value` are carried so a report can restate the request in
    the unit it was MADE in, which is the same reason `PortfolioStats` echoes
    `risk_free_rate` and `target_annual_return` back verbatim: a reader who
    typed 3000 should not have to multiply anything to check that 0.0300 is
    the same instruction.

    `origin` follows the convention `format_risk_free_rate` established - a
    phrase fit to print in parentheses after the figure - and is populated
    in EVERY case, never left blank, because a floor printed without its
    source is the exact ambiguity that convention exists to prevent.
    """

    yield_floor: float
    origin: str
    cash_floor: float | None = None
    portfolio_value: float | None = None
    currency: str = "USD"

    def cash_on(self, total_value: float) -> float:
        """The floor restated as cash against `total_value`."""
        return float(self.yield_floor) * float(total_value)


def validate_dividend_yield(value: object, source: str) -> float:
    """`value` as a usable portfolio dividend-yield floor, naming `source`
    in any refusal.

    Modelled on `src/agentic_portfolio/flow/rate_memory.py`'s `validate_risk_free_rate`, and
    for the same three reasons. It accepts `object` so a value from a flag,
    an interactive prompt or a hand-edited file gets identical scrutiny.
    Booleans are refused ahead of the numeric check because
    `isinstance(True, int)` is true in Python and `True` would otherwise
    become a 100% floor. And non-finite values are refused because
    `argparse`'s `type=float` accepts `nan` and `inf` quite happily, while a
    `nan` floor reaches cvxpy as a constraint that is neither satisfied nor
    refused - it simply makes the problem infeasible with a message naming
    neither the flag nor the number.

    Unlike a risk-free rate, a NEGATIVE floor is refused. A negative policy
    rate is a real thing a central bank has done, which is why that
    validator accepts one; a negative dividend is not, and a floor below zero
    is met by every portfolio, so it can only be a typo.

    Exactly `0.0` IS accepted. It is a legitimate, deliberately non-binding
    floor, and refusing it would mean a scripted `--min-dividend-yield 0`
    failed where `0.001` worked.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{source} must be a number, got {value!r}")

    floor = float(value)
    if not math.isfinite(floor):
        raise ValueError(f"{source} must be a finite number, got {value!r}")
    if floor < 0:
        raise ValueError(
            f"{source} must not be negative, got {value!r}; every portfolio already meets a "
            "negative dividend floor, so this cannot be what you meant"
        )
    if floor >= MAX_DIVIDEND_YIELD:
        raise ValueError(
            f"{source} must be a decimal yield below {MAX_DIVIDEND_YIELD}, got {value!r}; "
            "yields are decimals here, so 3% is 0.03 - and no long-only portfolio of ordinary "
            f"equities sustains a {MAX_DIVIDEND_YIELD:.0%} trailing yield, so this is almost "
            "certainly a percentage typed as a number"
        )
    return floor


def validate_min_annual_dividend(value: object, source: str, portfolio_value: object) -> float:
    """`value`, a cash amount, converted to the one yield the optimizer
    consumes by dividing by `portfolio_value` - and refused, naming
    `source`, if either number is unusable.

    The `portfolio_value` check is load-bearing rather than defensive.
    `--value` is declared `required=True, type=float`, which accepts `0` and
    `-5` without complaint; a zero would arrive at this division as a
    `ZeroDivisionError` and a negative would silently invert the
    constraint's meaning, turning a floor into a ceiling.

    The implied yield goes through `validate_dividend_yield` so both flags
    share one ceiling, but a failure is re-worded to name BOTH numbers: a
    person who typed `--min-annual-dividend 50000 --value 100000` needs to be
    told about the 0.5000 it implies, not merely that 0.5000 is too high.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{source} must be a number, got {value!r}")
    cash = float(value)
    if not math.isfinite(cash):
        raise ValueError(f"{source} must be a finite number, got {value!r}")
    if cash < 0:
        raise ValueError(f"{source} must not be negative, got {value!r}")

    if isinstance(portfolio_value, bool) or not isinstance(portfolio_value, (int, float)):
        raise ValueError(f"{source} needs a numeric --value to divide by, got {portfolio_value!r}")
    total = float(portfolio_value)
    if not math.isfinite(total) or total <= 0:
        raise ValueError(
            f"{source} is a cash amount and only means something as a share of the portfolio, "
            f"so it needs a positive --value to divide by; got --value {portfolio_value!r}"
        )

    implied = cash / total
    try:
        return validate_dividend_yield(implied, source)
    except ValueError as e:
        raise ValueError(
            f"{source} {cash:,.2f} on --value {total:,.2f} implies a portfolio dividend yield "
            f"of {implied:.4f}, which is not usable: {e}"
        ) from e


def unavailable_yield_refusal(
    missing: Sequence[str], reasons: dict[str, str] | None = None
) -> DividendYieldUnavailableError:
    """The refusal to raise when some pool ticker has no trailing dividend
    yield, worded from whatever is known about WHY.

Split out of `dividend_yield_vector` rather than inlined, because the
    message now has two forms and the choice between them is a decision
    worth reading on its own - and because a caller that learns the reasons
    only after the refusal (the shape `src/agentic_portfolio/flow/interactive.py`'s
    `_explain_dropped_dividend_payers` takes for the ceiling refusal) can
    rebuild the same sentence instead of appending to it.

    "Build its dividend history" is included only when it might work. With
    a recorded reason for EVERY missing ticker the advice is replaced by
    those reasons, because the case that produced this function - a window
    Yahoo no longer serves - makes rebuilding futile, and a full-universe
    fetch is an expensive way to learn nothing. With a reason for only
    some, the advice stays: it is still actionable for the rest.
    """
    names = ", ".join(sorted(missing))
    preamble = (
        f"no trailing dividend yield is available for {names}, so a dividend floor cannot be "
        "applied to a pool containing it - a missing yield is not a zero yield, and treating "
        "it as one would understate this portfolio's income while quietly forcing weight "
        "elsewhere. "
    )
    known = {t: (reasons or {}).get(t) for t in sorted(missing)}
    if all(known.values()):
        detail = " ".join(f"{t}: {r}" for t, r in known.items())
        return DividendYieldUnavailableError(
            f"{preamble}{detail} Remove it from the pool, or drop the floor and read the "
            "reported figures instead."
        )
    return DividendYieldUnavailableError(
        f"{preamble}Remove it from the pool, build its dividend history, or drop the floor and "
        "read the reported figures instead."
    )


def dividend_yield_vector(
    yields: dict[str, float],
    tickers: Sequence[str],
    reasons: dict[str, str] | None = None,
) -> np.ndarray:
    """`yields` as a dense float array in `tickers`' exact order.

    THE alignment guarantee of this feature, and the reason it is a named
    function rather than a comprehension at the call site.
    `EfficientFrontier` is built from a `pd.Series` `mu` and takes its own
    ticker order from `list(expected_returns.index)`, which is the order of
    the `cp.Variable` this vector gets multiplied against. Every caller
    therefore passes `mu.index` and nothing else. Building the vector from
    the dict's own key order, or from the returns matrix's columns BEFORE
    `apply_min_history_rule` dropped a short-history ticker, would multiply
    the right numbers against the wrong tickers - a constraint satisfied on
    paper and meaningless in fact, with nothing anywhere to raise about it.

    A ticker absent from `yields` raises `DividendYieldUnavailableError`
    rather than defaulting to `0.0`. The data layer's contract is that a key
    is present with `0.0` for a CONFIRMED non-payer and absent when the
    trailing yield could not be determined, and collapsing those two is the
    one silently wrong answer this feature is able to produce. Substituting
    zero is not the conservative choice it looks like: it would make the
    reported ceiling a function of a fabricated number, so a refusal could
    name the wrong ticker and quote a ceiling that does not exist, and a
    genuinely feasible pool could be refused because the one ticker with no
    data happened to be its best payer.

    A non-finite yield is refused for the same reason a non-finite floor is:
    a `nan` in this vector produces a constraint cvxpy can neither satisfy
    nor report.

    `reasons` - from `src/agentic_portfolio/dataset/dividends.py`'s
    `dividend_unresolved_reasons` - is what the refusal says instead of
    "build its dividend history" when the reason a yield is missing is
    already recorded. That advice is not always available: for a ticker
    whose historical window Yahoo no longer serves, building its dividend
    history is impossible, and telling the user to try costs them a
    full-universe fetch to learn nothing. A reason is only substituted when
    EVERY missing ticker has one, since a message that drops the actionable
    advice while one ticker could still act on it would be a worse message.
    """
    missing = [t for t in tickers if t not in yields]
    if missing:
        raise unavailable_yield_refusal(missing, reasons)

    values = [float(yields[t]) for t in tickers]
    bad = [t for t, v in zip(tickers, values) if not math.isfinite(v)]
    if bad:
        raise DividendYieldUnavailableError(
            f"the trailing dividend yield for {', '.join(sorted(bad))} is not a finite number, "
            "so no dividend floor can be enforced against it"
        )
    return np.asarray(values, dtype=float)


def dividend_yield_ceiling(vector: np.ndarray, tickers: Sequence[str]) -> tuple[str, float]:
    """The highest portfolio dividend yield reachable at all, and the ticker
    that reaches it, as `(ticker, yield)`.

    The formula is simply `max(vector)`, and it is EXACT rather than a
    bound. Nothing in this project overrides PyPortfolioOpt's default
    `weight_bounds=(0, 1)`, and `min_volatility`, `efficient_return` and
    `max_sharpe` each append a fully-invested constraint at call time, so
    weights are long-only and sum to one. A weighted average of the
    per-ticker yields cannot exceed the largest of them, and putting the
    entire portfolio into that one ticker attains it. That is what makes the
    ceiling cheap enough to compute before solving and precise enough to
    name in a refusal - which is the whole reason this feature pre-checks
    instead of letting the solver come back with "infeasible".
    """
    if len(vector) == 0:
        raise ValueError("cannot take a dividend-yield ceiling over an empty pool")
    best = int(np.argmax(vector))
    return str(tickers[best]), float(vector[best])


CEILING_EXPLANATION = (
    "because weights are long-only and sum to 1, no combination of these candidates can yield "
    "more than its single best-yielding member"
)


def check_dividend_floor_feasible(
    floor: DividendFloor, vector: np.ndarray, tickers: Sequence[str]
) -> None:
    """Refuse an unreachable `floor` BEFORE the solver is asked, naming the
    ceiling and the ticker that sets it.

    Raises `DividendFloorError`. Pre-checking rather than catching the
    solver's own complaint is what turns "Solver status: infeasible" into a
    sentence a person can act on, and the arithmetic in
    `dividend_yield_ceiling` is exact, so this refuses nothing that would
    have worked.
    """
    ticker, ceiling = dividend_yield_ceiling(vector, tickers)
    if floor.yield_floor <= ceiling + DIVIDEND_FEASIBILITY_TOLERANCE:
        return
    raise DividendFloorError(
        f"{floor.origin} asks for a portfolio dividend yield of at least "
        f"{floor.yield_floor:.4f}, but the highest-yielding candidate is {ticker} at "
        f"{ceiling:.4f} - {CEILING_EXPLANATION}. Lower the floor to at most {ceiling:.4f}, "
        "or add a higher-yielding candidate."
    )


def portfolio_dividend_yield(
    weights: np.ndarray | Sequence[float], vector: np.ndarray
) -> float:
    """`sum(w_i * y_i)` - the one definition of a portfolio's dividend yield
    used everywhere in this project, so the figure a report prints is the
    same quantity the constraint enforced rather than a lookalike computed
    another way."""
    return float(np.dot(np.asarray(weights, dtype=float), vector))


def covered_dividend_yield(
    weights: dict[str, float], yields: dict[str, float]
) -> tuple[float, float, tuple[str, ...]]:
    """`(yield_over_covered_weight, covered_weight, tickers_with_no_yield)`
    for a weight vector that may name a ticker with no known yield.

    The no-floor counterpart to `portfolio_dividend_yield`, used when a
    report must say something useful about a pool the constraint machinery
    would have refused outright. When a yield is unknown the honest answer is
    not a smaller total but a smaller DENOMINATOR, stated - which is the rule
    `src/agentic_portfolio/optimizer/holdings.py` already applies to a holding it cannot
    measure, rather than withholding a correct answer about the rest of the
    portfolio.

    `yield_over_covered_weight` is `sum(w*y) / sum(w)` over the covered
    tickers, so it reads on the same scale as a fully-covered figure instead
    of being silently diluted toward zero by the missing ones.
    """
    covered = {t: w for t, w in weights.items() if t in yields}
    missing = tuple(sorted(t for t in weights if t not in yields))
    covered_weight = float(sum(covered.values()))
    if covered_weight <= 0:
        return 0.0, 0.0, missing
    weighted = float(sum(w * yields[t] for t, w in covered.items()))
    return weighted / covered_weight, covered_weight, missing


class DividendFigures(NamedTuple):
    """What a portfolio of actual SHARES pays over a trailing year.

    All-or-nothing, following `HoldingsStats`' own discipline: either
    `total_annual_dividends`, `dividend_yield` and `value_covered` are all
    populated, or all three are `None` and `unavailable` says why for each
    holding. `NO_DIVIDEND_FIGURES` is a third, distinguishable state -
    dividends were never consulted at all - recognizable because
    `dividends_per_share` is empty as well as the totals being `None`.

    `value_covered` is the denominator `dividend_yield` was computed over: the
    market value of the holdings with a known trailing dividends-per-share,
    which is NOT necessarily `total_value`. It is a stored field rather than
    something the reader derives, because the report must print it - a 2.4%
    yield over 99.9% of a portfolio and a 2.4% yield over 60% of it are
    different claims, and only one of them is worth acting on.

    `splits_in_window` names, per ticker, the `(ex_date, ratio)` splits that
    fall inside the same trailing window the dividends were summed over. It
    is almost always empty, and it is carried so a report can explain a
    per-share figure that Yahoo Finance restated: a payment made before a
    split is reported divided down by it, so the stored amount is not the
    amount anybody announced. Without this the report prints a number a
    reader cannot reconcile against a company announcement and reasonably
    concludes is wrong.

    `yields` carries each holding's own trailing yield as the dividend layer
    computed it - cash per share over the RAW market close - and it exists so
    the same ticker never reports two different yields in two places. The
    obvious alternative, dividing this record's own `annual_dividends[t]` by
    `market_values[t]`, would be internally consistent and cross-report
    WRONG: market values come from `load_latest_prices`, which reads
    `adj_close`, so on a database whose price window ended well before it was
    fetched that quotient runs above the pool report's figure for the very
    same ticker - measured at roughly 12% on the shipped data. This project
    promises that a portfolio holding only `SPY` reports the same numbers the
    `Benchmark SPY` line does, and one ticker showing two yields would break
    that promise in the most confusing possible way. The consequence to be
    aware of, and it is the lesser evil: `yields[t] * market_values[t]` does
    not exactly equal `annual_dividends[t]`, because the two rest on
    different price columns - see this module's note on the pre-existing
    valuation basis.

    Deliberately window-INDEPENDENT. A trailing dividend is a record of the
    last twelve months of cash, not an estimate over a returns window, so
    these figures cover every PRICED holding - including one
    `apply_min_history_rule` dropped from the return/volatility/Sharpe
    figures for having too little history. That holding pays what it pays
    whether or not anybody can estimate its covariance, and leaving it out
    would understate the income the portfolio actually produces. This is the
    same deliberate split `holdings.py` already makes between `total_value`
    (every priced holding) and `weights` (only the measured ones).
    """

    dividends_per_share: dict[str, float]
    annual_dividends: dict[str, float]
    total_annual_dividends: float | None
    dividend_yield: float | None
    value_covered: float | None
    unavailable: dict[str, str]
    yields: dict[str, float] = {}
    splits_in_window: dict[str, list[tuple[date, float]]] = {}


NO_DIVIDEND_FIGURES = DividendFigures({}, {}, None, None, None, {}, {}, {})
"""The "dividends were not consulted" shape, shared and never mutated."""


def dividend_figures(
    positions: dict[str, float],
    market_values: dict[str, float],
    dividends_per_share: dict[str, float] | None,
    unavailable: dict[str, str] | None = None,
    yields: dict[str, float] | None = None,
    splits_in_window: dict[str, list[tuple[date, float]]] | None = None,
) -> DividendFigures:
    """Build a `DividendFigures` for `positions` priced at `market_values`.

    `dividends_per_share=None` means "not consulted" and yields
    `NO_DIVIDEND_FIGURES`. An empty dict means "consulted, nothing found"
    and yields the all-`None` totals with every holding named in
    `unavailable` - a different statement, and one a report should make out
    loud.

    `annual_dividends[t]` is `shares * dividends_per_share[t]`: the cash the
    holder will actually receive, computed from the shares they own rather
    than from a weight. The per-holding yield a report prints is that cash
    over `market_values[t]`, so the two figures on one line reconcile
    against the same price the value used instead of against a yield struck
    at some other date.
    """
    if dividends_per_share is None:
        return NO_DIVIDEND_FIGURES

    reasons = dict(unavailable or {})
    annual: dict[str, float] = {}
    covered_value = 0.0

    for ticker, shares in positions.items():
        if ticker not in market_values:
            reasons.setdefault(ticker, "no price to value its dividends against")
            continue
        if ticker not in dividends_per_share:
            reasons.setdefault(ticker, "no trailing dividend data")
            continue
        annual[ticker] = float(shares) * float(dividends_per_share[ticker])
        covered_value += float(market_values[ticker])

    held_yields = {t: float(v) for t, v in (yields or {}).items() if t in annual}
    # Restricted to holdings that actually contribute a dividend figure, so
    # the report never explains a restatement for a ticker whose numbers it
    # is not showing.
    # Kept as the record it is, NOT list()-ed: `SplitContext` is a
    # NamedTuple, so list() would flatten it into [splits, payments] and
    # silently destroy the field names every reader uses.
    held_splits = {t: v for t, v in (splits_in_window or {}).items() if t in annual}

    if not annual or covered_value <= 0:
        return DividendFigures(
            dividends_per_share=dict(dividends_per_share),
            annual_dividends={},
            total_annual_dividends=None,
            dividend_yield=None,
            value_covered=None,
            unavailable=reasons,
            yields=held_yields,
            splits_in_window=held_splits,
        )

    total = float(sum(annual.values()))

    # The portfolio yield is the market-value-weighted average of the
    # per-holding yields whenever those are known - the SAME definition
    # `portfolio_dividend_yield` applies to an optimized pool, so one report
    # cannot state a yield the other would compute differently, and so this
    # line equals the weighted average of the per-holding lines printed above
    # it. Falling back to `total / covered_value` covers the case where no
    # per-holding yield was supplied; the two differ only because market
    # values are struck from `adj_close` while a yield divides by the raw
    # close, and `yields` documents why that gap is accepted here rather
    # than papered over.
    if held_yields and covered_value > 0:
        weighted = sum(
            float(market_values[t]) * held_yields[t] for t in held_yields if t in market_values
        )
        covered_with_yield = sum(
            float(market_values[t]) for t in held_yields if t in market_values
        )
        portfolio_yield = weighted / covered_with_yield if covered_with_yield > 0 else None
    else:
        portfolio_yield = total / covered_value

    return DividendFigures(
        dividends_per_share=dict(dividends_per_share),
        annual_dividends=annual,
        total_annual_dividends=total,
        dividend_yield=portfolio_yield,
        value_covered=covered_value,
        unavailable=reasons,
        yields=held_yields,
        splits_in_window=held_splits,
    )
