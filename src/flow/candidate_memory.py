"""Persistence for the `user_provided` selection mode's candidate pools,
stored at `memory/candidates.json`.

Since `plans/11_non_us_tickers_and_single_currency.md` made every pool
single-currency, this file holds one pool PER CURRENCY rather than one flat
list, so a standing JPY pool and a standing USD pool can both live in the
same default file instead of requiring separate `--memory-path` values:

    {"pools": {"USD": {"tickers": [...], "updated_at": "..."},
               "JPY": {"tickers": [...], "updated_at": "..."}}}

A file written before that plan holds the old flat shape,
`{"tickers": [...], "updated_at": "..."}`, with no `"pools"` key.
`load_all_pools` reads that as `{"USD": [...]}` rather than raising - safe
because nothing before that plan could ever ingest a non-USD ticker, so
every pool saved under the old shape is guaranteed to be USD. The next
`save_candidate_pool` call rewrites the file in the new shape.

This is a narrow, self-contained slice of the much larger persistent-memory
Live Mode architecture README.md describes (multi-set $S$/$S \\cap F$/$U$
semantics, staleness rules, `*-summary.md` files) - that whole area is an
unimplemented scope gap tracked separately (see
`plans/08_consistency_review.md` Finding 11) and is out of scope here.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from src.dataset.ticker_currency import DEFAULT_CURRENCY

DEFAULT_CANDIDATES_PATH = "memory/candidates.json"


def _validate_tickers(path: str, currency: str, tickers: object) -> list[str]:
    if not isinstance(tickers, list) or not all(isinstance(t, str) for t in tickers):
        raise ValueError(
            f"{path!r}'s {currency!r} pool must contain a 'tickers' field holding a "
            f"list of strings, got {tickers!r}"
        )
    return sorted(set(tickers))


def _load_raw_pools(path: str) -> dict[str, dict]:
    """Every saved pool at `path` as `{currency: {"tickers": [...],
    "updated_at": ...}}`, normalizing the old single-pool shape and
    validating each entry. `{}` when the file does not exist.
    """
    file_path = Path(path)
    if not file_path.exists():
        return {}

    data = json.loads(file_path.read_text())
    if "pools" not in data:
        # The old, single-pool shape - safe to read as the USD pool; see the
        # module docstring for why no other currency could appear here.
        raw = {DEFAULT_CURRENCY: data}
    else:
        raw = data["pools"]

    return {
        currency: {
            "tickers": _validate_tickers(path, currency, entry.get("tickers")),
            "updated_at": entry.get("updated_at"),
        }
        for currency, entry in raw.items()
    }


def load_all_pools(path: str = DEFAULT_CANDIDATES_PATH) -> dict[str, list[str]]:
    """`{currency: sorted_deduplicated_tickers}` for every pool saved at
    `path`, or `{}` if the file does not exist yet (the expected state on a
    repository's first-ever `user_provided` run).

    Raises `ValueError`, naming the file and the offending currency, if a
    pool's `tickers` field is missing or not a list of strings. A malformed
    JSON file lets `json.JSONDecodeError` propagate unchanged.
    """
    return {currency: entry["tickers"] for currency, entry in _load_raw_pools(path).items()}


def load_candidate_pool(path: str = DEFAULT_CANDIDATES_PATH, currency: str = DEFAULT_CURRENCY) -> list[str]:
    """Sorted, deduplicated tickers for `currency`'s pool at `path`, or `[]`
    if that pool has never been saved.
    """
    return load_all_pools(path).get(currency, [])


def save_candidate_pool(
    tickers: list[str], path: str = DEFAULT_CANDIDATES_PATH, currency: str = DEFAULT_CURRENCY
) -> None:
    """Write `tickers` (sorted, deduplicated) as `currency`'s pool at `path`,
    together with the current UTC timestamp, creating `path`'s parent
    directory if needed.

    Every other currency already saved at `path` - read first, in either
    the old or new shape - is carried over untouched, keeping its own
    `updated_at`, so saving one currency's pool can never disturb another's
    tickers or make it look freshly written.
    """
    pools = _load_raw_pools(path)
    pools[currency] = {
        "tickers": sorted(set(tickers)),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    _write_pools(path, pools)


def _write_pools(path: str, pools: dict[str, dict]) -> None:
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(json.dumps({"pools": pools}, indent=2))


def migrate_candidate_pools(path: str = DEFAULT_CANDIDATES_PATH) -> dict[str, list[str]]:
    """Rewrite `path` in the per-currency shape without touching its contents,
    returning `{currency: tickers}` for whatever it holds afterwards.

    Only the file's *shape* changes. Every pool keeps its tickers and its own
    `updated_at` verbatim, which is the reason this exists rather than just
    letting the next `save_candidate_pool` do the conversion: that would
    stamp the current time over the only record of when the pool was last
    curated, and it happens only after a live re-validation of every ticker,
    which can drop one that momentarily fails to resolve.

    Performs no network calls. Safe to repeat: a file already in the new
    shape is rewritten identically. A missing file is reported as `{}` rather
    than created, so asking about a file that is not there does not
    materialize one.

    A legacy file with no `updated_at` migrates with that field left null.
    Back-filling it with the migration time would invent a curation date we
    do not know.
    """
    if not Path(path).exists():
        return {}

    pools = _load_raw_pools(path)
    _write_pools(path, pools)
    return {currency: entry["tickers"] for currency, entry in pools.items()}


def main() -> None:
    """Console-script entry point (`portfolio-migrate-candidates`), for
    converting a `candidates.json` written before pools were kept per
    currency. Running it on an already-converted file is a harmless no-op.
    """
    parser = argparse.ArgumentParser(
        description="Rewrite a candidates.json in the per-currency shape, preserving tickers and timestamps."
    )
    parser.add_argument("--path", default=DEFAULT_CANDIDATES_PATH, help="The candidates file to convert.")
    args = parser.parse_args()

    pools = migrate_candidate_pools(args.path)
    if not pools:
        print(f"Nothing to migrate: {args.path} does not exist.")
        return

    print(f"Migrated {args.path} to the per-currency shape:")
    for currency, tickers in sorted(pools.items()):
        print(f"  {currency} ({len(tickers)}): {', '.join(tickers)}")


if __name__ == "__main__":
    main()
