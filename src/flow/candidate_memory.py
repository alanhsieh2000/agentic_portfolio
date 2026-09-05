"""Persistence for the `user_provided` selection mode's candidate pool: a
flat list of tickers plus a timestamp, stored at `memory/candidates.json`.

This is a narrow, self-contained slice of the much larger persistent-memory
Live Mode architecture README.md describes (multi-set $S$/$S \\cap F$/$U$
semantics, staleness rules, `*-summary.md` files) - that whole area is an
unimplemented scope gap tracked separately (see
`plans/08_consistency_review.md` Finding 11) and is out of scope here.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_CANDIDATES_PATH = "memory/candidates.json"


def load_candidate_pool(path: str = DEFAULT_CANDIDATES_PATH) -> list[str]:
    """Sorted, deduplicated tickers from `path`, or `[]` if the file does
    not exist yet (the expected state on a repository's first-ever
    `user_provided` run). Raises `ValueError` if the file exists but its
    `tickers` field is missing or not a list of strings; a malformed JSON
    file lets `json.JSONDecodeError` propagate unchanged.
    """
    file_path = Path(path)
    if not file_path.exists():
        return []

    data = json.loads(file_path.read_text())
    tickers = data.get("tickers")
    if not isinstance(tickers, list) or not all(isinstance(t, str) for t in tickers):
        raise ValueError(f"{path!r} must contain a 'tickers' field holding a list of strings, got {tickers!r}")
    return sorted(set(tickers))


def save_candidate_pool(tickers: list[str], path: str = DEFAULT_CANDIDATES_PATH) -> None:
    """Write `tickers` (sorted, deduplicated) and the current UTC timestamp
    to `path`, creating its parent directory if needed.
    """
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "tickers": sorted(set(tickers)),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    file_path.write_text(json.dumps(payload, indent=2))
