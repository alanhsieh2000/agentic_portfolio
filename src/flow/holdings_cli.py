"""CLI entry point for maintaining the user's OWN portfolio, per
`plans/13_user_portfolio.md`: `uv run portfolio-holdings [show|set|remove]`.

    uv run portfolio-holdings set SPY 1000 T 500
    uv run portfolio-holdings remove T
    uv run portfolio-holdings show

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

Unlike `src/flow/cli.py` this command is entirely non-interactive: it never
calls `input()`, so it is usable from a script or a one-line edit.
"""

from __future__ import annotations

import argparse
import sys

from src.config.settings import settings
from src.dataset.ticker_currency import DEFAULT_CURRENCY, partition_by_currency
from src.flow.cli import parse_date, print_user_portfolio
from src.flow.interactive import in_typed_order, prepare_holdings
from src.flow.live import build_scratch_snapshot
from src.dataset.ticker_ingestion import validate_and_ingest_tickers
from src.flow.user_portfolio import (
    DEFAULT_PORTFOLIO_PATH,
    load_all_portfolios,
    load_portfolio,
    save_portfolio,
)

VALID_COMMANDS = ("show", "set", "remove")


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


def _report(currency: str, args) -> None:
    """Print one currency's portfolio and its figures - the last thing every
    subcommand does, so an edit's effect is always visible immediately
    rather than requiring a second command to see.
    """
    print_user_portfolio(
        prepare_holdings(
            load_portfolio(args.path, currency),
            currency,
            parse_date(args.date),
            args.db_path,
            risk_free_rate=args.risk_free_rate,
            allow_fetch=not args.no_holdings_fetch,
        ),
        args.path,
    )


def _run_show(args) -> None:
    """`show`: one currency's portfolio, or every saved one in turn.

    With nothing saved at all, `DEFAULT_CURRENCY`'s empty portfolio is
    reported rather than nothing printed, because that report carries the
    sentence naming the command that fixes it - a bare "no output" would
    leave a first-time user with nowhere to go.
    """
    if args.currency:
        _report(args.currency, args)
        return

    saved = sorted(load_all_portfolios(args.path))
    for currency in saved or [DEFAULT_CURRENCY]:
        _report(currency, args)


def _validate_set_targets(
    pairs: list[tuple[str, float]], args
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
    """
    typed = [ticker for ticker, _ in pairs]
    with build_scratch_snapshot(prefix="holdings_validate_") as scratch_db_path:
        valid, invalid, currencies = validate_and_ingest_tickers(
            typed, parse_date(args.date), scratch_db_path
        )

    accepted, refused, currency = partition_by_currency(
        in_typed_order(valid, typed), args.currency, currencies
    )
    return currency, accepted, invalid, refused


def _run_set(args) -> None:
    """`set TICKER SHARES ...`: record or update holdings, then report.

    A share count of `0` retires a holding rather than storing a zero row -
    it is this command's way of saying "I sold all of it", and it needs no
    separate spelling from an ordinary edit.
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


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Maintain the portfolio you actually hold, per currency, and report its "
                    "annualized return, volatility and Sharpe ratio.",
        epilog="Examples: portfolio-holdings set SPY 1000 T 500 | portfolio-holdings remove T | "
               "portfolio-holdings --currency JPY set 1321.T 50 | portfolio-holdings show",
    )
    parser.add_argument(
        "command",
        nargs="?",
        default="show",
        choices=VALID_COMMANDS,
        help="What to do. Defaults to 'show', so a bare invocation reports every saved portfolio.",
    )
    parser.add_argument(
        "args",
        nargs="*",
        help="For 'set', alternating TICKER SHARES pairs (SHARES of 0 retires a holding). "
             "For 'remove', tickers. Ignored by 'show'.",
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
        default=settings.risk_free_rate,
        help="Rate the reported Sharpe ratio is measured against. Defaults to the configured "
             "RISK_FREE_RATE. Use the same value here as for 'uv run portfolio' if you intend "
             "to compare the two reports.",
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

    runners = {"show": _run_show, "set": _run_set, "remove": _run_remove}
    try:
        runners[args.command](args)
    except ValueError as e:
        # A mistyped pair, a negative share count, or an ambiguous removal is
        # the user's input to fix, not a stack trace to read: reported on
        # stderr with a non-zero exit so a script can tell it failed.
        print(f"error: {e}", file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
