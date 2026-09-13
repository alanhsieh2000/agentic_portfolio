"""Describe a built market-data database, so the copy baked into an image is not
an opaque blob.

Run at image build time, writing `DATASET.json` next to the database it
describes. Read it back with, for example:

    docker run --rm <image> cat /opt/agentic-portfolio/data/DATASET.json

Why generated rather than hand-written. A manifest maintained by hand drifts
from the file it describes, and a manifest that disagrees with its data is worse
than none - you cannot tell which of the two is wrong. Everything here is read
out of the database itself, so it cannot disagree.

Why this script imports only `duckdb` and the standard library. It runs in an
image stage that has the project's dependencies installed but NOT the project
itself, and deliberately so: the dependency layer is cached on
`pyproject.toml` + `uv.lock` alone, and importing `agentic_portfolio` here would
mean rebuilding this manifest on every source change. Everything it needs is in
the data - including the fetch window, which `dividend_coverage` records
directly rather than being something to pass in and get wrong.

The four tickers with no dividend coverage (AVB, EA, EQR, LEG at the time of
writing) are listed WITH their recorded reasons. That is the point of including
them: the reasons distinguish a transient fetch failure, which re-running fixes,
from a ticker whose remaining upstream history postdates the window entirely,
which re-running never will.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import date, datetime, timezone
from pathlib import Path

import duckdb


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_safe(value: object) -> object:
    """Dates as ISO strings; everything else unchanged.

    DuckDB hands back `datetime.date` objects, which `json.dump` refuses.
    """
    return value.isoformat() if isinstance(value, (date, datetime)) else value


def build_manifest(db_path: str) -> dict:
    """Everything worth knowing about this database, read out of it."""
    path = Path(db_path)
    con = duckdb.connect(str(path), read_only=True)
    try:
        tables = sorted(row[0] for row in con.execute("SHOW TABLES").fetchall())
        counts = {
            table: con.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in tables
        }

        spans: dict[str, dict[str, object]] = {}
        for table, column in (("prices", "date"), ("dividends", "ex_date")):
            if table in tables and counts[table]:
                low, high = con.execute(f"SELECT min({column}), max({column}) FROM {table}").fetchone()
                spans[table] = {"first": _json_safe(low), "last": _json_safe(high)}

        # The window the data was actually fetched over, recorded by the build
        # rather than inferred from what arrived - so a ticker that returned
        # nothing is still covered by a stated window.
        fetch_window: dict[str, object] = {}
        if "dividend_coverage" in tables and counts["dividend_coverage"]:
            low, high = con.execute(
                "SELECT min(fetch_start), max(fetch_end) FROM dividend_coverage"
            ).fetchone()
            fetch_window = {"start": _json_safe(low), "end": _json_safe(high)}

        tickers = (
            con.execute("SELECT count(DISTINCT ticker) FROM prices").fetchone()[0]
            if "prices" in tables
            else 0
        )

        unresolved_dividends = []
        if "dividend_unresolved" in tables:
            unresolved_dividends = [
                {"ticker": ticker, "reason": reason}
                for ticker, reason in con.execute(
                    "SELECT ticker, reason FROM dividend_unresolved ORDER BY ticker"
                ).fetchall()
            ]

        # Grouped by reason rather than listed per ticker. These are delisted or
        # renamed symbols and they almost all share one generic sentence, so a
        # per-ticker list would repeat the same 300 characters fifty-odd times
        # and bury the tickers themselves. Grouping keeps the manifest readable
        # while losing nothing.
        unresolved_tickers: list[dict[str, object]] = []
        if "unresolved_tickers" in tables:
            grouped: dict[str, list[str]] = {}
            for ticker, reason in con.execute(
                "SELECT ticker, reason FROM unresolved_tickers ORDER BY ticker"
            ).fetchall():
                grouped.setdefault(reason, []).append(ticker)
            unresolved_tickers = [
                {"tickers": tickers, "count": len(tickers), "reason": reason}
                for reason, tickers in sorted(grouped.items(), key=lambda kv: -len(kv[1]))
            ]
    finally:
        con.close()

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "file": {
            "name": path.name,
            "bytes": path.stat().st_size,
            "sha256": _sha256(path),
        },
        "distinct_tickers_in_prices": tickers,
        "spans": spans,
        "dividend_fetch_window": fetch_window,
        "table_row_counts": counts,
        "dividends_unresolved": unresolved_dividends,
        "prices_unresolved": unresolved_tickers,
        "notes": (
            "Derived from SEC EDGAR (public domain), Wikipedia's S&P 500 membership list "
            "(CC BY-SA 4.0) and Yahoo Finance price/dividend history. See NOTICE for the terms, "
            "which differ per source. A ticker listed under dividends_unresolved whose reason "
            "says the upstream history postdates the window will never be fixed by re-running; "
            "one that failed transiently may be."
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Write a JSON manifest describing a market-data DuckDB database."
    )
    parser.add_argument("db_path", help="The database to describe. Opened read-only.")
    parser.add_argument("out_path", help="Where to write the manifest. Parents are created.")
    args = parser.parse_args()

    manifest = build_manifest(args.db_path)
    out = Path(args.out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(manifest, indent=2, sort_keys=False) + "\n")
    print(
        f"Wrote {out} describing {manifest['file']['name']} "
        f"({manifest['file']['bytes']:,} bytes, sha256 {manifest['file']['sha256'][:12]}...)"
    )


if __name__ == "__main__":
    main()
