"""CLI entry point for maintaining the user's OWN portfolio, per
`plans/13_user_portfolio.md`:
`uv run portfolio-holdings [show|set|remove|whatif]`.

    uv run portfolio-holdings set SPY 1000 T 500
    uv run portfolio-holdings remove T
    uv run portfolio-holdings show
    uv run portfolio-holdings whatif

This is the command that answers "what do I actually hold, and what has it
done?" - as opposed to `uv run portfolio` (`src/agentic_portfolio/flow/cli.py`), which
answers "given these candidates, what should I hold?". The two are
deliberately separate commands over separate files, because a candidate
pool is a list of tickers under consideration while a portfolio is a record
of fact; see `src/agentic_portfolio/flow/user_portfolio.py`'s module docstring.

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
`src/agentic_portfolio/flow/cli.py`'s `print_user_portfolio` that `uv run portfolio` uses -
one formatter, so the two commands' reports cannot drift into two
different-looking blocks.

The risk-free rate those Sharpe ratios are measured against is likewise per
currency, remembered in `memory/rates.json` (see
`src/agentic_portfolio/flow/rate_memory.py`) and shared with `uv run portfolio`. It is
resolved once per REPORTED currency, so a `show` spanning a dollar and a
yen portfolio subtracts each one's own riskless return instead of applying a
dollar rate to both. `--risk-free-rate` overrides it for the run and is
remembered for that currency; it is refused when the command names no single
currency to remember it against.

`whatif` is the exception to all of the above, and the exception proves the
rule: it applies hypothetical changes to a portfolio and reports what the
figures would become, and it saves NOTHING - not the positions, and not a
`--risk-free-rate`, which every other subcommand remembers.

A hypothetical change is NOT limited to what you already hold. Naming a
ticker you do not own is the question the loop mainly earns its keep on -
"what would adding this do?" is precisely what cannot be answered by eye,
since a holding that raises expected return often raises volatility more
and lowers the Sharpe ratio. Such a ticker is validated and priced exactly
as `set` would validate and price it, so the experiment predicts what
actually recording it would produce. It is also the
one interactive subcommand, prompting in a loop so several variations can be
tried in a row against one fetch. Those two facts belong together: the
reason the rest of this command is non-interactive is that each of its
operations is a single edit better expressed as one scriptable line, whereas
exploring is inherently a conversation - and it is safe to make it one
precisely because nothing it does can outlive the session.

The prices and monthly returns behind all of it are cached in
`data/holdings.duckdb` (see `src/agentic_portfolio/dataset/holdings_cache.py`), refreshed for
a ticker when the cache lacks its latest month - so a repeated report costs
no network, and `--refresh-holdings` forces one sooner. Because that rule
keeps the returns current while letting the prices age, `Total value` is
printed with the date it was priced at.

So every subcommand that CHANGES the record is entirely non-interactive: it
never calls `input()`, and is usable from a script or a one-line edit. That
was previously true of the whole command; see
`plans/13_user_portfolio.md`'s Milestone 5.
"""

from __future__ import annotations

import argparse
import json
import sys

from agentic_portfolio.config.settings import settings
from agentic_portfolio.dataset.ticker_currency import DEFAULT_CURRENCY, partition_by_currency
from agentic_portfolio.flow.cli import (
    format_dividend_delta,
    format_stale_share_counts,
    format_holdings_delta,
    holdings_report_facts,
    parse_date,
    print_user_portfolio,
)
from agentic_portfolio.flow.report_archive import ReportArchive, command_line, record_report
from agentic_portfolio.flow.rate_memory import (
    DEFAULT_RATES_PATH,
    load_risk_free_rate,
    resolve_risk_free_rate,
    save_risk_free_rate,
    validate_risk_free_rate,
)
from agentic_portfolio.dataset.holdings_cache import DEFAULT_HOLDINGS_CACHE_PATH, refresh_holdings_cache
from agentic_portfolio.optimizer.holdings import (
    DEFAULT_LOOKBACK_MONTHS,
    HoldingsStats,
    MIN_LOOKBACK_MONTHS,
    validate_lookback_months,
)
from agentic_portfolio.flow.interactive import (
    in_typed_order,
    measure_holdings,
    open_holdings_session,
    prepare_holdings,
)
from agentic_portfolio.flow.live import build_scratch_snapshot
from agentic_portfolio.dataset.ticker_ingestion import validate_and_ingest_tickers
from agentic_portfolio.flow.user_portfolio import (
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
    `src/agentic_portfolio/flow/interactive.py`'s `in_typed_order` for why sorting would pick
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
    # Printed before the report because a share count a split has multiplied
    # makes every figure below uniformly wrong while leaving them
    # self-consistent, so a caveat underneath would come too late.
    stale = format_stale_share_counts(
        args.path, currency, load_portfolio(args.path, currency), args.holdings_cache_path
    )
    if stale is not None:
        print(stale)
    print_user_portfolio(
        prepare_holdings(
            load_portfolio(args.path, currency),
            currency,
            parse_date(args.date),
            args.db_path,
            risk_free_rate=resolved.rate,
            allow_fetch=not args.no_holdings_fetch,
            cache_path=args.holdings_cache_path,
            force_refresh=args.refresh_holdings,
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
    the run, the same ordering `src/agentic_portfolio/flow/cli.py`'s candidate-pool save and
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
    refresh_cache: bool = False,
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

    `refresh_cache` says that `db_path` is the persistent holdings cache
    rather than a throwaway file, so a ticker it already holds this month
    needs no request at all. `whatif` passes it, which is why re-trying a
    candidate is free after the first time.

    `pool_currency` is the currency the tickers must match, defaulting to
    `--currency`. `set` leaves it alone, because for `set` the first typed
    ticker is exactly what SHOULD establish the currency - that is how
    `set 1321.T 50` creates a JPY portfolio. `whatif` must pass it: the
    portfolio being experimented on already has a currency, so leaving it
    `None` would let a typed yen ticker establish JPY and be accepted into a
    dollar experiment rather than refused by name.
    """
    typed = [ticker for ticker, _ in pairs]
    if db_path is not None and refresh_cache:
        # `db_path` is the holdings cache, so go through its staleness rule
        # rather than fetching unconditionally: a ticker tried in an earlier
        # what-if is already there and costs nothing to try again, which is
        # most of what an explore loop does.
        valid, invalid, currencies = refresh_holdings_cache(
            typed, parse_date(args.date), db_path
        )
    elif db_path is not None:
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


WHATIF_PROMPT = (
    "\nWhat if? [s]et shares (any ticker) / [r]emove / [w]indow / [u]ndo all / [f]inish: "
)

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


def _whatif_window(current: int) -> int:
    """Read a new returns-window length, or keep `current` when the answer is
    unusable.

    The `continue`-on-unparseable-input discipline `_run_edit_loop`
    established: a bad answer costs the person one line, not the window they
    already had. `validate_lookback_months` supplies the message, so the
    bounds are explained once, where they are enforced.
    """
    raw = input(
        f"Months of returns to measure over ({MIN_LOOKBACK_MONTHS}-60, currently {current}): "
    ).strip()
    if not raw:
        return current

    try:
        months: object = int(raw)
    except ValueError:
        # Hand the raw text to the validator rather than letting `int`'s own
        # "invalid literal for int() with base 10" reach the person. One
        # message covers a non-number and an out-of-range number, and it is
        # the message that explains the bounds.
        months = raw

    try:
        return validate_lookback_months(months, "the window")
    except ValueError as e:
        print(f"Keeping {current} months: {e}")
        return current


def _run_whatif(args) -> None:
    """`whatif`: try hypothetical changes to the saved holdings - including
    adding a ticker you do not own, and including the length of the returns
    window itself - and see the three figures move, without saving anything.

    The window is a change worth trying because it can dominate the answer.
    A holding that fell hard early in the 60-month window and has been
    stable since reads as poor over 60 months and quite differently over 36;
    both are true statements about different spans of months, and 1 to 5
    years are all defensible choices. `[w]indow` is how you see more than
    one, and the report names the length that was asked for so a chosen
    window is never mistaken for all the data there was.

    The one interactive subcommand, and the one that changes nothing - those
    two facts are related. Every other subcommand is a single edit that is
    better expressed as one scriptable line, whereas the point here is to try
    five variations in a row and watch the numbers, which a flag-per-run
    shape would make unbearable: each variation would re-pay the Yahoo
    Finance round trip that `open_holdings_session` pays once.

    Writes no state. Not to `memory/portfolio.json`, and - the easier one to
    miss - not to `memory/rates.json` either: `--risk-free-rate` works here
    as a run-only override, useful for asking what a different riskless
    return would do to the Sharpe ratio, but `_remember_rate` is
    deliberately not called, because a command whose whole promise is
    changing nothing must not leave a rate behind.

    The one thing it does write is a copy of each report it prints, under
    `--output-dir` (see `src/agentic_portfolio/flow/report_archive.py`), and that is not an
    exception to the promise above: state is what changes a later run, and a
    report changes none - it is an observation. The distinction matters
    because trying five variations in a row is exactly the case whose results
    are worth keeping, and re-deriving one costs the Yahoo Finance round trip
    `open_holdings_session` pays. Each variant names the file it went to as it
    is printed, so nothing is written silently, and `--no-save-reports`
    restores writing nothing whatsoever. `NOT_SAVED` below is left as it is:
    it speaks about the portfolio, and about the portfolio it remains exactly
    true.

    Imitates `src/agentic_portfolio/flow/cli.py`'s `_run_edit_loop` throughout: snapshot the
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

    # One archive for the whole session, built before the fetch below for the
    # same reason `uv run portfolio` builds its own before opening a pipeline
    # session: a malformed --output-dir should be discovered before a Yahoo
    # Finance round trip, not after it. `kind="whatif"` is what keeps these
    # files distinguishable from a pool report's in a shared month folder.
    archive = ReportArchive(
        output_dir=args.output_dir,
        enabled=not args.no_save_reports,
        kind="whatif",
        as_of=parse_date(args.date),
        command=command_line(),
    )

    with open_holdings_session(
        list(baseline_positions),
        parse_date(args.date),
        args.db_path,
        allow_fetch=not args.no_holdings_fetch,
        cache_path=args.holdings_cache_path,
        force_refresh=args.refresh_holdings,
    ) as session:
        known: dict[str, str] = {}
        window = DEFAULT_LOOKBACK_MONTHS

        def measure(these: dict[str, float]) -> HoldingsStats:
            return measure_holdings(
                these, currency, parse_date(args.date), session, resolved.rate, known, window
            )

        def window_note() -> str | None:
            # Named only when somebody chose it, so the default run's report
            # is byte-identical to what it printed before this existed.
            return None if window == DEFAULT_LOOKBACK_MONTHS else f"{window} requested"

        baseline = measure(baseline_positions)
        # On the baseline only: the hypothetical variants below are the
        # user's own inventions, so repeating a warning about the SAVED
        # counts after every edit would be noise.
        stale = format_stale_share_counts(
            args.path, currency, baseline_positions, args.holdings_cache_path
        )
        if stale is not None:
            print(stale)
        # The stale warning is deliberately OUTSIDE the archived block: it is
        # a fact about `memory/portfolio.json` and the price cache rather than
        # about the portfolio's figures, so including it would give the same
        # holdings two different digests depending on cache state, and the
        # same baseline would be stored twice across two days.
        with record_report(archive, **holdings_report_facts("baseline", baseline)):
            print_user_portfolio(
                baseline, args.path, risk_free_rate_origin=origin, window_origin=window_note()
            )

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
            elif choice in ("w", "window"):
                chosen = _whatif_window(window)
                if chosen == window:
                    continue
                window = chosen
                # The baseline is re-measured at the new length, not left
                # where it was. `format_holdings_delta` withholds a delta
                # across differing windows - correctly, since the difference
                # would then be partly the months rather than the holdings -
                # so a window change has to move both sides or every
                # subsequent comparison would come back as an apology.
                baseline = measure(baseline_positions)
                print(f"Measuring over {window} month(s) of returns.")
            elif choice in ("u", "undo"):
                positions = dict(baseline_positions)
                print("Back to your saved holdings.")
            else:
                print(f"Unrecognized choice {choice!r}.")
                continue

            if positions == previous and choice not in ("w", "window"):
                continue

            if positions == baseline_positions:
                # These ARE the saved holdings - either nothing was changed
                # yet (a window-only edit) or `[u]ndo all` put them back. So
                # reprint the baseline as the baseline. Labelling it "What if
                # - not saved" would be a false label on the person's real
                # portfolio, and the delta beneath it would be a row of
                # zeroes dressed up as a finding.
                with record_report(archive, **holdings_report_facts("baseline", baseline)):
                    print_user_portfolio(
                        baseline,
                        args.path,
                        risk_free_rate_origin=origin,
                        window_origin=window_note(),
                    )
                continue

            hypothetical = measure(positions)
            # The two delta lines are inside the archived block because they
            # are part of what the reader compares: a stored variant that had
            # dropped them would be missing the very figures the experiment
            # was run to see.
            with record_report(archive, **holdings_report_facts("what-if", hypothetical)):
                print_user_portfolio(
                    hypothetical,
                    args.path,
                    risk_free_rate_origin=origin,
                    heading=f"What if ({currency}) - not saved",
                    window_origin=window_note(),
                )
                print(format_holdings_delta(baseline, hypothetical))
                # A separate line, and one that stays informative across a
                # [w]indow change: a trailing dividend is a record of cash
                # paid, not an estimate over a returns window, so the two
                # sides remain comparable where the Sharpe delta above does
                # not. See `format_dividend_delta`.
                print(format_dividend_delta(baseline, hypothetical))


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
    raw = input("Ticker and shares, held or not (e.g. NVDA 100, 0 to drop): ").strip().upper()
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
                refresh_cache=True,
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
               "portfolio-holdings whatif (try changes, including tickers you do not own, "
               "without saving them)",
    )
    parser.add_argument(
        "command",
        nargs="?",
        default="show",
        choices=VALID_COMMANDS,
        help="What to do. Defaults to 'show', so a bare invocation reports every saved portfolio. "
             "'whatif' opens a loop for trying hypothetical changes and seeing the figures move - "
             "including for a ticker you do not own yet - and saves nothing at all.",
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
        default=settings.db_path,
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
        "--holdings-cache-path",
        default=DEFAULT_HOLDINGS_CACHE_PATH,
        help="Which file the holdings' prices and monthly returns are cached in, so a repeated "
             "report costs no network. Refreshed for a ticker when it does not hold that "
             "ticker's latest month, which means once a month; --refresh-holdings forces it. "
             "Never the same file as --db-path.",
    )
    parser.add_argument(
        "--refresh-holdings",
        action="store_true",
        help="Refetch every holding's prices now rather than reusing the cache. The cache is "
             "otherwise good for the rest of the calendar month, so the monthly returns are "
             "always current but the prices behind 'Total value' can be weeks old - the report "
             "states the date it priced them at, and this flag is how you move it.",
    )
    parser.add_argument(
        "--rates-path",
        default=DEFAULT_RATES_PATH,
        help="Which file the per-currency risk-free rates are remembered in. One file holds "
             "one rate per currency, shared with 'uv run portfolio'.",
    )
    parser.add_argument(
        "--output-dir",
        default=settings.output_dir,
        help="Directory every report 'whatif' prints is also saved under, one subdirectory per "
             "month of --date (so 'output/2026-09/'). A variant identical to one already saved "
             "that month is recognized by a digest of its own text and not written twice. This is "
             "the only thing 'whatif' writes; your portfolio and the remembered rates are still "
             "untouched. Shared with 'uv run portfolio', so a month's folder holds both commands' "
             "reports side by side.",
    )
    parser.add_argument(
        "--no-save-reports",
        action="store_true",
        help="Print the reports and keep no copy of them, restoring 'whatif' to writing nothing "
             "whatsoever. Nothing is written under --output-dir, and no directory is created.",
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
        help="Report the holdings only from data already on disk - what --db-path holds, or the "
             "holdings cache - never by fetching. Keeps the command entirely offline, at the "
             "cost of reporting a holding as unmeasurable when neither contains it.",
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
