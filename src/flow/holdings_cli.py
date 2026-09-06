"""CLI entry point for maintaining the user's OWN portfolio, per
`plans/13_user_portfolio.md`:
`uv run portfolio-holdings [show|set|remove|whatif]`.

    uv run portfolio-holdings set SPY 1000 T 500
    uv run portfolio-holdings remove T
    uv run portfolio-holdings show
    uv run portfolio-holdings whatif

This is the command that answers "what do I actually hold, and what has it
done?" - as opposed to `uv run portfolio` (`src/flow/cli.py`), which
answers "given these candidates, what should I hold?". The two are
deliberately separate commands over separate files, because a candidate
pool is a list of tickers under consideration while a portfolio is a record
of fact; see `src/flow/user_portfolio.py`'s module docstring.

Holdings are stored per currency in `memory/portfolio.json`, one portfolio
per currency, for the same reason candidate pools are: prices in two
different units cannot be weighed against one total, and this project does
not convert between currencies. Which portfolio a `set` edits therefore
follows from the ticker itself - `set SPY 1000` edits the USD portfolio and
`set 1321.T 50` edits the JPY one, creating it if it does not exist. Naming
tickers of two different currencies in ONE `set` is refused by name, since
that would be editing two portfolios at once. `--currency` names the target
portfolio explicitly instead, and then a ticker in any other currency is
refused.

Every subcommand ends by printing the affected portfolio's holdings and its
annualized return, volatility and Sharpe ratio, through the very same
`src/flow/cli.py`'s `print_user_portfolio` that `uv run portfolio` uses -
one formatter, so the two commands' reports cannot drift into two
different-looking blocks.

The risk-free rate those Sharpe ratios are measured against is likewise per
currency, remembered in `memory/rates.json` (see
`src/flow/rate_memory.py`) and shared with `uv run portfolio`. It is
resolved once per REPORTED currency, so a `show` spanning a dollar and a
yen portfolio subtracts each one's own riskless return instead of applying a
dollar rate to both. `--risk-free-rate` overrides it for the run and is
remembered for that currency; it is refused when the command names no single
currency to remember it against.

`whatif` is the exception to all of the above, and the exception proves the
rule: it applies hypothetical changes to a portfolio and reports what the
figures would become, and it saves NOTHING - not the positions, and not a
`--risk-free-rate`, which every other subcommand remembers. It is also the
one interactive subcommand, prompting in a loop so several variations can be
tried in a row against one fetch. Those two facts belong together: the
reason the rest of this command is non-interactive is that each of its
operations is a single edit better expressed as one scriptable line, whereas
exploring is inherently a conversation - and it is safe to make it one
precisely because nothing it does can outlive the session.

So every subcommand that CHANGES the record is entirely non-interactive: it
never calls `input()`, and is usable from a script or a one-line edit. That
was previously true of the whole command; see
`plans/13_user_portfolio.md`'s Milestone 5.
"""

from __future__ import annotations

import argparse
import json
import sys

from src.config.settings import settings
from src.dataset.ticker_currency import DEFAULT_CURRENCY, partition_by_currency
from src.flow.cli import format_holdings_delta, parse_date, print_user_portfolio
from src.flow.rate_memory import (
    DEFAULT_RATES_PATH,
    load_risk_free_rate,
    resolve_risk_free_rate,
    save_risk_free_rate,
    validate_risk_free_rate,
)
from src.flow.interactive import (
    in_typed_order,
    measure_holdings,
    open_holdings_session,
    prepare_holdings,
)
from src.flow.live import build_scratch_snapshot
from src.dataset.ticker_ingestion import validate_and_ingest_tickers
from src.flow.user_portfolio import (
    DEFAULT_PORTFOLIO_PATH,
    load_all_portfolios,
    load_portfolio,
    save_portfolio,
)

VALID_COMMANDS = ("show", "set", "remove", "whatif")


def _normalize_ticker(token: str) -> str:
    """One typed ticker token, upper-cased with a trailing comma stripped.

    The comma exists because `SPY, 1000` is how a person naturally writes a
    holding, and refusing it over punctuation the shell has already split
    for us would be gratuitous.
    """
    return token.strip().rstrip(",").strip().upper()


def _parse_pairs(tokens: list[str]) -> list[tuple[str, float]]:
    """`['SPY', '1000', 'T', '500.5']` as `[('SPY', 1000.0), ('T', 500.5)]`,
    in the order typed.

    Typed order is preserved rather than sorted because the first ticker
    establishes which currency's portfolio is being edited - see
    `src/flow/interactive.py`'s `in_typed_order` for why sorting would pick
    the wrong one (digits sort before letters, so `AAPL 7203.T` would become
    a yen edit).

    Raises `ValueError` naming the problem for an odd number of tokens or a
    share count that is not a number, so a typo is reported rather than
    half-applied.
    """
    if len(tokens) % 2 != 0:
        raise ValueError(
            "expected alternating TICKER SHARES pairs, e.g. 'set SPY 1000 T 500', "
            f"but got an odd number of values: {' '.join(tokens)}"
        )

    pairs: list[tuple[str, float]] = []
    for ticker_token, shares_token in zip(tokens[::2], tokens[1::2]):
        ticker = _normalize_ticker(ticker_token)
        try:
            shares = float(shares_token)
        except ValueError:
            raise ValueError(
                f"share count for {ticker} must be a number, got {shares_token!r}"
            ) from None
        if shares < 0:
            raise ValueError(
                f"share count for {ticker} must not be negative, got {shares_token!r}; "
                "this project has no model of a short position"
            )
        pairs.append((ticker, shares))
    return pairs


def _resolve_rate(currency: str, args):
    """The risk-free rate to measure `currency`'s portfolio against, and
    where it came from.

    Resolved PER REPORTED CURRENCY rather than once for the whole
    invocation, which is the point of the whole feature: `_run_show` loops
    over every saved currency, and each iteration must subtract that
    currency's own riskless return rather than a dollar rate applied to
    everything alike.
    """
    return resolve_risk_free_rate(
        args.risk_free_rate,
        currency,
        load_risk_free_rate(args.rates_path, currency),
        settings.risk_free_rate,
    )


def _report(currency: str, args) -> None:
    """Print one currency's portfolio and its figures - the last thing every
    subcommand does, so an edit's effect is always visible immediately
    rather than requiring a second command to see.

    Resolves the rate but deliberately never PERSISTS it. `_run_show` calls
    this in a loop, so a write here would make a bare
    `show --risk-free-rate 0.03` record the same rate under every currency -
    the exact error class this feature exists to fix. Remembering is
    `_remember_rate`'s job, called at most once per invocation.
    """
    resolved = _resolve_rate(currency, args)
    print_user_portfolio(
        prepare_holdings(
            load_portfolio(args.path, currency),
            currency,
            parse_date(args.date),
            args.db_path,
            risk_free_rate=resolved.rate,
            allow_fetch=not args.no_holdings_fetch,
        ),
        args.path,
        risk_free_rate_origin=resolved.origin,
    )


AMBIGUOUS_RATE_CURRENCY = (
    "--risk-free-rate has to be remembered for one currency, and this command names none; "
    "add --currency (for example --currency JPY)"
)


def _remember_rate(currency: str | None, args) -> None:
    """Record an explicit `--risk-free-rate` as `currency`'s remembered rate,
    announcing the write.

    Called at most once per invocation, and only after the report it applied
    to actually printed - a value that governed no output should not outlive
    the run, the same ordering `src/flow/cli.py`'s candidate-pool save and
    `_settle_benchmark` both observe.

    Announced because this project has no silent writes, and because
    remembering here is a side effect of a flag rather than an explicit
    command - which makes it exactly the kind of write a user needs told
    about. The message names the file too, since editing it is the only way
    to un-remember a rate.

    `currency=None` means the command determined no single currency to
    remember against; that is refused rather than guessed, because writing a
    rate under a currency nobody named is how the bug this fixes got started.
    """
    if args.risk_free_rate is None:
        return
    if currency is None:
        raise ValueError(AMBIGUOUS_RATE_CURRENCY)

    if save_risk_free_rate(args.risk_free_rate, path=args.rates_path, currency=currency):
        print(
            f"Remembered {args.risk_free_rate:.4f} as the {currency} risk-free rate "
            f"in {args.rates_path}."
        )


def _rate_currency_for_report(args) -> str | None:
    """Which currency a `show`-shaped invocation would remember a rate for:
    an explicit `--currency`, else the single saved portfolio's currency,
    else `None`.

    `None` covers two cases that must not become a write. Several saved
    portfolios with no `--currency` is ambiguous. And nothing saved at all is
    not a determination either: `_run_show` reports `DEFAULT_CURRENCY` there
    only so the "add some holdings" hint gets printed, and recording a dollar
    rate on the strength of that fallback would be recording a guess.
    """
    if args.currency:
        return args.currency
    saved = sorted(load_all_portfolios(args.path))
    return saved[0] if len(saved) == 1 else None


def _run_show(args) -> None:
    """`show`: one currency's portfolio, or every saved one in turn.

    With nothing saved at all, `DEFAULT_CURRENCY`'s empty portfolio is
    reported rather than nothing printed, because that report carries the
    sentence naming the command that fixes it - a bare "no output" would
    leave a first-time user with nowhere to go.
    """
    if args.currency:
        _report(args.currency, args)
        _remember_rate(args.currency, args)
        return

    saved = sorted(load_all_portfolios(args.path))
    for currency in saved or [DEFAULT_CURRENCY]:
        _report(currency, args)
    _remember_rate(_rate_currency_for_report(args), args)


def _validate_set_targets(
    pairs: list[tuple[str, float]],
    args,
    db_path: str | None = None,
    pool_currency: str | None = None,
) -> tuple[str | None, list[str], dict[str, str], dict[str, str]]:
    """Resolve which currency's portfolio a `set` edits, and which of its
    tickers may join it: `(currency, accepted, invalid, refused)`.

    Every ticker is first validated against Yahoo Finance (and its prices,
    returns and trading currency ingested) so a typo is named here rather
    than silently saved and then quietly dropped by the risk figures weeks
    later. The ingest goes into a THROWAWAY scratch database, never into
    `--db-path`: the shared `data/portfolio.duckdb` cache holds the S&P 500
    universe and must not gain rows as a side effect of recording what
    somebody owns. The share counts are what is persisted; the fetched
    prices are only how the tickers were checked, and `prepare_holdings`
    fetches its own copy when it needs them.

    `db_path` names an ALREADY-OPEN throwaway database to ingest into
    instead of opening one that is discarded on return. `whatif` passes its
    session's, so a ticker validated here is immediately measurable in the
    same breath - which is the difference between one fetch per experiment
    and one fetch per typed ticker. Still never `--db-path`: the caller owns
    the file it passes, and `open_holdings_session` only hands over a scratch
    one.

    `pool_currency` is the currency the tickers must match, defaulting to
    `--currency`. `set` leaves it alone, because for `set` the first typed
    ticker is exactly what SHOULD establish the currency - that is how
    `set 1321.T 50` creates a JPY portfolio. `whatif` must pass it: the
    portfolio being experimented on already has a currency, so leaving it
    `None` would let a typed yen ticker establish JPY and be accepted into a
    dollar experiment rather than refused by name.
    """
    typed = [ticker for ticker, _ in pairs]
    if db_path is not None:
        valid, invalid, currencies = validate_and_ingest_tickers(
            typed, parse_date(args.date), db_path
        )
    else:
        with build_scratch_snapshot(prefix="holdings_validate_") as scratch_db_path:
            valid, invalid, currencies = validate_and_ingest_tickers(
                typed, parse_date(args.date), scratch_db_path
            )

    accepted, refused, currency = partition_by_currency(
        in_typed_order(valid, typed), pool_currency or args.currency, currencies
    )
    return currency, accepted, invalid, refused


def _run_set(args) -> None:
    """`set TICKER SHARES ...`: record or update holdings, then report.

    A share count of `0` retires a holding rather than storing a zero row -
    it is this command's way of saying "I sold all of it", and it needs no
    separate spelling from an ordinary edit.

    An explicit `--risk-free-rate` is remembered for the currency this edit
    landed in - determined by the tickers themselves, so unlike `show` it
    needs no `--currency` to be unambiguous. When nothing was saved at all
    (every ticker unresolvable), nothing is remembered either: there is no
    currency to attach it to.
    """
    pairs = _parse_pairs(args.args)
    if not pairs:
        raise ValueError("set needs at least one TICKER SHARES pair, e.g. 'set SPY 1000'")

    currency, accepted, invalid, refused = _validate_set_targets(pairs, args)
    if invalid:
        print(f"Ignored (not found): {', '.join(sorted(invalid))}.")
    for ticker in sorted(refused):
        print(
            f"Refused: {ticker} is priced in {refused[ticker]}, so it belongs to the "
            f"{refused[ticker]} portfolio, not the {currency} one. "
            f"Record it with: uv run portfolio-holdings --currency {refused[ticker]} set {ticker} N"
        )

    if not accepted:
        print("Nothing was saved.")
        if currency:
            _report(currency, args)
            _remember_rate(currency, args)
        return

    positions = load_portfolio(args.path, currency)
    retired = [ticker for ticker, shares in pairs if ticker in accepted and shares == 0]
    for ticker, shares in pairs:
        if ticker in accepted:
            positions[ticker] = shares

    save_portfolio(positions, path=args.path, currency=currency)

    recorded = [(t, s) for t, s in pairs if t in accepted and s > 0]
    if recorded:
        print(
            f"Recorded in the {currency} portfolio: "
            + ", ".join(f"{t} ({s:g} shares)" for t, s in recorded)
            + "."
        )
    if retired:
        print(f"Retired from the {currency} portfolio: {', '.join(sorted(retired))}.")

    _report(currency, args)
    _remember_rate(currency, args)


WHATIF_PROMPT = "\nWhat if? [s]et shares / [r]emove / [u]ndo all / [f]inish: "

NOT_SAVED = (
    "Nothing was saved: that was a what-if, and your portfolio is unchanged."
)


def _whatif_apply(
    positions: dict[str, float], pairs: list[tuple[str, float]], currency: str
) -> dict[str, float]:
    """`positions` with `pairs` applied, using `set`'s own semantics: a share
    count replaces whatever was there, and `0` retires the holding.

    Kept identical to `_run_set`'s merge on purpose. The value of a what-if
    is that it predicts what `set` would do, and it can only promise that if
    it applies changes the same way - which is also why `whatif` ends by
    printing the `set` command that would make the experiment real.
    """
    applied = dict(positions)
    for ticker, shares in pairs:
        if shares > 0:
            applied[ticker] = shares
        else:
            applied.pop(ticker, None)
    return applied


def _whatif_set_command(baseline: dict[str, float], hypothetical: dict[str, float]) -> str | None:
    """The single `set` invocation that would turn `baseline` into
    `hypothetical`, or `None` when they are the same.

    Printed when the loop finishes because a what-if deliberately writes
    nothing, and the person who just found an improvement should not have to
    retype it from the report. A retired holding appears as `0`, which is how
    `set` spells a removal.
    """
    changed = {
        ticker: hypothetical.get(ticker, 0.0)
        for ticker in sorted(set(baseline) | set(hypothetical))
        if baseline.get(ticker, 0.0) != hypothetical.get(ticker, 0.0)
    }
    if not changed:
        return None

    pairs = " ".join(f"{ticker} {shares:g}" for ticker, shares in changed.items())
    return f"uv run portfolio-holdings set {pairs}"


def _run_whatif(args) -> None:
    """`whatif`: try hypothetical changes to the saved holdings and see the
    three figures move, without saving anything.

    The one interactive subcommand, and the one that changes nothing - those
    two facts are related. Every other subcommand is a single edit that is
    better expressed as one scriptable line, whereas the point here is to try
    five variations in a row and watch the numbers, which a flag-per-run
    shape would make unbearable: each variation would re-pay the Yahoo
    Finance round trip that `open_holdings_session` pays once.

    Never writes. Not to `memory/portfolio.json`, and - the easier one to
    miss - not to `memory/rates.json` either: `--risk-free-rate` works here
    as a run-only override, useful for asking what a different riskless
    return would do to the Sharpe ratio, but `_remember_rate` is
    deliberately not called, because a command whose whole promise is
    changing nothing must not leave a rate behind.

    Imitates `src/flow/cli.py`'s `_run_edit_loop` throughout: snapshot the
    positions before an edit so a rejected one reverts, `continue` on
    unparseable input keeping what you had, and recompute-then-report. It is
    strictly simpler than that loop in one respect - there is no persist step
    to order correctly, because there is no persist step at all.
    """
    currency = _rate_currency_for_report(args)
    if currency is None:
        raise ValueError(
            "whatif needs one currency's portfolio to experiment on, and this command names "
            "none; add --currency (for example --currency JPY)"
        )

    baseline_positions = load_portfolio(args.path, currency)
    resolved = _resolve_rate(currency, args)
    # `resolve_risk_free_rate` phrases an overridden rate as "remembered for
    # USD", which is true of every other subcommand and false of this one.
    # Printing it here would have the report claim a write this command
    # exists specifically not to make - the mirror image of the
    # unprovenanced-rate bug `format_risk_free_rate` was added to prevent.
    origin = (
        "--risk-free-rate, not remembered - this is a what-if"
        if resolved.from_override
        else resolved.origin
    )

    with open_holdings_session(
        list(baseline_positions),
        parse_date(args.date),
        args.db_path,
        allow_fetch=not args.no_holdings_fetch,
    ) as session:
        known: dict[str, str] = {}
        baseline = measure_holdings(
            baseline_positions, currency, parse_date(args.date), session, resolved.rate, known
        )
        print_user_portfolio(baseline, args.path, risk_free_rate_origin=origin)

        positions = dict(baseline_positions)
        while True:
            choice = input(WHATIF_PROMPT).strip().lower()

            if choice in ("", "f", "finish"):
                print(f"\n{NOT_SAVED}")
                command = _whatif_set_command(baseline_positions, positions)
                if command is not None:
                    print(f"To keep it: {command}")
                return

            previous = dict(positions)
            if choice in ("s", "set"):
                positions = _whatif_set(positions, currency, session, known, args)
            elif choice in ("r", "remove"):
                raw = input("Ticker(s) to remove (space-separated): ").strip().upper()
                tickers = [_normalize_ticker(t) for t in raw.split() if _normalize_ticker(t)]
                absent = sorted(t for t in tickers if t not in positions)
                if absent:
                    print(f"Not held (ignored): {', '.join(absent)}.")
                positions = {t: n for t, n in positions.items() if t not in tickers}
            elif choice in ("u", "undo"):
                positions = dict(baseline_positions)
                print("Back to your saved holdings.")
            else:
                print(f"Unrecognized choice {choice!r}.")
                continue

            if positions == previous:
                continue

            hypothetical = measure_holdings(
                positions, currency, parse_date(args.date), session, resolved.rate, known
            )
            print_user_portfolio(
                hypothetical,
                args.path,
                risk_free_rate_origin=origin,
                heading=f"What if ({currency}) - not saved",
            )
            print(format_holdings_delta(baseline, hypothetical))


def _whatif_set(
    positions: dict[str, float],
    currency: str,
    session,
    known: dict[str, str],
    args,
) -> dict[str, float]:
    """One `[s]et` step: read `TICKER SHARES` pairs, validate anything new,
    and return the resulting hypothetical positions.

    A ticker already in the experiment needs no validation - the session
    resolved it - so only genuinely new ones cost a lookup, which is what
    keeps a second edit to the same holding instant.

    New tickers go through `_validate_set_targets`, the same function `set`
    uses, so a cross-currency ticker is refused with the same sentence and
    the same suggested command. Routing straight to the measurement instead
    would have `interactive._holdings_currency_gate` merely EXCLUDE it with
    a one-line reason, which is right for a hand-edited file but wrong for
    something just typed: a typo deserves to be named, not quietly dropped
    from the figures.
    """
    raw = input("Ticker and shares (e.g. NVDA 100, 0 to drop): ").strip().upper()
    try:
        pairs = _parse_pairs(raw.split())
    except ValueError as e:
        print(f"Ignoring that: {e}")
        return positions
    if not pairs:
        return positions

    new = [(t, n) for t, n in pairs if t not in positions and n > 0]
    if new:
        if not session.can_ingest:
            print(
                f"Refused: {', '.join(sorted(t for t, _ in new))} would need a price fetch, and "
                "fetching is disabled with --no-holdings-fetch. Drop that flag to try a holding "
                "you do not already own."
            )
            pairs = [(t, n) for t, n in pairs if (t, n) not in new]
        else:
            _resolved, accepted, invalid, refused = _validate_set_targets(
                [(t, n) for t, n in new],
                args,
                db_path=session.db_path,
                pool_currency=currency,
            )
            if invalid:
                print(f"Ignored (not found): {', '.join(sorted(invalid))}.")
            for ticker in sorted(refused):
                print(
                    f"Refused: {ticker} is priced in {refused[ticker]}, so it belongs to the "
                    f"{refused[ticker]} portfolio, not the {currency} one."
                )
            known.update({t: currency for t in accepted})
            keep = set(accepted) | {t for t, _ in pairs if t in positions} | {
                t for t, n in pairs if n == 0
            }
            pairs = [(t, n) for t, n in pairs if t in keep]

    if not pairs:
        return positions
    return _whatif_apply(positions, pairs, currency)


def _resolve_remove_currency(tickers: list[str], args) -> str | None:
    """Which portfolio a `remove` acts on: `--currency` when given,
    otherwise the single saved portfolio that holds any of `tickers`.

    Raises `ValueError` naming the candidates when more than one portfolio
    holds them, rather than guessing. Removing a holding is destructive of
    a record the user maintains by hand, and picking one of two plausible
    portfolios for them is the kind of guess that is silently wrong.
    """
    if args.currency:
        return args.currency

    holding = sorted(
        currency
        for currency, positions in load_all_portfolios(args.path).items()
        if set(positions) & set(tickers)
    )
    if len(holding) > 1:
        raise ValueError(
            f"{', '.join(tickers)} appear in more than one portfolio ({', '.join(holding)}); "
            "name the one you mean with --currency"
        )
    return holding[0] if holding else None


def _run_remove(args) -> None:
    """`remove TICKER ...`: drop holdings, then report.

    No Yahoo Finance validation: removing a ticker that is not held is
    already a harmless no-op, so there is nothing a lookup could protect
    against here.
    """
    tickers = [_normalize_ticker(token) for token in args.args if _normalize_ticker(token)]
    if not tickers:
        raise ValueError("remove needs at least one ticker, e.g. 'remove T'")

    currency = _resolve_remove_currency(tickers, args)
    if currency is None:
        print(f"Not held in any saved portfolio: {', '.join(sorted(tickers))}.")
        _run_show(args)
        return

    positions = load_portfolio(args.path, currency)
    removed = sorted(set(tickers) & set(positions))
    missing = sorted(set(tickers) - set(positions))

    if removed:
        save_portfolio(
            {t: s for t, s in positions.items() if t not in removed},
            path=args.path,
            currency=currency,
        )
        print(f"Removed from the {currency} portfolio: {', '.join(removed)}.")
    if missing:
        print(f"Not held in the {currency} portfolio: {', '.join(missing)}.")

    _report(currency, args)
    _remember_rate(currency, args)


def _guard_rate_is_rememberable(args) -> None:
    """Refuse an explicit `--risk-free-rate` that names no single currency,
    BEFORE the command does any work.

    `_remember_rate` would refuse it anyway, but only at the end - after
    `set` has already paid for a Yahoo Finance round trip, or after `remove`
    has printed "Not held in any saved portfolio". Checking here means the
    complaint arrives before the cost.

    `set` is exempt: its typed tickers determine the currency, so there is
    nothing to disambiguate and nothing yet to check. `whatif` is exempt for
    a different reason - it never remembers a rate at all, so the ambiguity
    this guard exists to refuse cannot arise. It does its own refusal when
    it cannot tell which portfolio to experiment on.
    """
    if args.currency or args.command in ("set", "whatif"):
        return

    if args.command == "remove":
        tickers = [_normalize_ticker(token) for token in args.args if _normalize_ticker(token)]
        # Raises on its own if the tickers span several portfolios, which is
        # the same refusal in a more specific wording.
        if _resolve_remove_currency(tickers, args) is not None:
            return

    if _rate_currency_for_report(args) is None:
        raise ValueError(AMBIGUOUS_RATE_CURRENCY)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Maintain the portfolio you actually hold, per currency, and report its "
                    "annualized return, volatility and Sharpe ratio.",
        epilog="Examples: portfolio-holdings set SPY 1000 T 500 | portfolio-holdings remove T | "
               "portfolio-holdings --currency JPY set 1321.T 50 | portfolio-holdings show | "
               "portfolio-holdings whatif (try changes without saving them)",
    )
    parser.add_argument(
        "command",
        nargs="?",
        default="show",
        choices=VALID_COMMANDS,
        help="What to do. Defaults to 'show', so a bare invocation reports every saved portfolio. "
             "'whatif' opens a loop for trying hypothetical changes and seeing the figures move, "
             "and saves nothing at all.",
    )
    parser.add_argument(
        "args",
        nargs="*",
        help="For 'set', alternating TICKER SHARES pairs (SHARES of 0 retires a holding). "
             "For 'remove', tickers. Ignored by 'show' and 'whatif', which takes its changes "
             "at its own prompt.",
    )
    parser.add_argument(
        "--path",
        default=DEFAULT_PORTFOLIO_PATH,
        help="Which file the portfolios are stored in - an alternate or scratch store, for "
             "trying something without touching the real record. One file holds one portfolio "
             "per currency, so this is not how currencies are kept apart.",
    )
    parser.add_argument(
        "--currency",
        default=None,
        help="Which currency's portfolio to act on. Usually unnecessary: a ticker's own trading "
             "currency selects the portfolio it belongs to. Give it to target a portfolio "
             "explicitly, in which case a ticker in any other currency is refused.",
    )
    parser.add_argument(
        "--db-path",
        default="data/portfolio.duckdb",
        help="Database consulted (read only) for cached prices and monthly returns before "
             "anything is fetched. Never written to.",
    )
    parser.add_argument(
        "--date",
        default="today",
        help="Date the holdings are priced and measured as of, YYYY-MM-DD, or 'today'.",
    )
    parser.add_argument(
        "--risk-free-rate",
        type=float,
        default=None,
        help="Rate the reported Sharpe ratio is measured against, as a decimal (4.25%% is "
             "0.0425; negative rates are allowed). A value given here is REMEMBERED as this "
             "currency's rate and used by later runs of both this command and 'uv run "
             "portfolio'. Left out, the rate is whatever this currency remembered, else the "
             "configured RISK_FREE_RATE, else 2%%. Every report says which of those it used.",
    )
    parser.add_argument(
        "--rates-path",
        default=DEFAULT_RATES_PATH,
        help="Which file the per-currency risk-free rates are remembered in. One file holds "
             "one rate per currency, shared with 'uv run portfolio'.",
    )
    parser.add_argument(
        # Spelled the same as `uv run portfolio`'s flag, with the shorter
        # `--no-fetch` accepted too: the sentence the report prints when it
        # applies is produced by shared code that cannot know which of the two
        # commands is running, so it would otherwise name a flag the reader
        # did not type.
        "--no-holdings-fetch",
        "--no-fetch",
        action="store_true",
        help="Report the holdings only from returns --db-path already holds, never by fetching. "
             "Keeps the command entirely offline, at the cost of reporting a holding as "
             "unmeasurable when the cache does not contain it.",
    )
    args = parser.parse_args()

    if args.currency:
        args.currency = args.currency.strip().upper()

    runners = {
        "show": _run_show,
        "set": _run_set,
        "remove": _run_remove,
        "whatif": _run_whatif,
    }
    try:
        if args.risk_free_rate is not None:
            # Checked here, before any Yahoo Finance round trip: `set` would
            # otherwise refuse a mistyped rate only after validating and
            # ingesting every typed ticker.
            args.risk_free_rate = validate_risk_free_rate(
                args.risk_free_rate, "--risk-free-rate"
            )
            _guard_rate_is_rememberable(args)
        runners[args.command](args)
    except json.JSONDecodeError as e:
        # MUST precede the `ValueError` branch: `json.JSONDecodeError`
        # subclasses `ValueError`, so the broader clause would otherwise
        # swallow it and report a bare "Expecting property name..." with no
        # hint that a memory file is the culprit. Worth its own branch at all
        # because `--rates-path` now sits on the startup path of even a bare
        # `show`, and a raw parser message is a poor answer to a stray comma.
        print(f"error: could not read a memory file as JSON: {e}", file=sys.stderr)
        raise SystemExit(2)
    except ValueError as e:
        # A mistyped pair, a negative share count, an ambiguous removal, or a
        # rate that names no single currency is the user's input to fix, not a
        # stack trace to read: reported on stderr with a non-zero exit so a
        # script can tell it failed.
        print(f"error: {e}", file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
