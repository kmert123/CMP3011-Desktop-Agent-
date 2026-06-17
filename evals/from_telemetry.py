#!/usr/bin/env python3
"""Generate fixture candidates from ~/.jarvis/telemetry.jsonl.

Emits one JSON file per unique query to evals/fixtures/candidates/.
Records with no query field are skipped.
Existing candidate files are not overwritten — re-run is safe.

After running: review each file in candidates/, correct expected_intent/rung,
then move to evals/fixtures/ to include in scoring.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

_TELEMETRY = Path.home() / ".jarvis" / "telemetry.jsonl"
_CANDIDATES = Path(__file__).parent / "fixtures" / "candidates"


def slugify(text: str, max_len: int = 48) -> str:
    slug = "".join(c if c.isalnum() else "_" for c in text[:max_len])
    return slug.strip("_").lower() or "fixture"


def main() -> None:
    if not _TELEMETRY.exists():
        print(f"No telemetry at {_TELEMETRY}")
        sys.exit(0)

    _CANDIDATES.mkdir(parents=True, exist_ok=True)

    emitted = skipped = already_exists = 0

    for line in _TELEMETRY.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            skipped += 1
            continue

        query = record.get("query")
        if not query:
            skipped += 1
            continue

        # Use the logged values as a starting guess — human must verify
        intent = (record.get("intent") or "TEXT").upper()
        rung_raw = record.get("perception_rung")
        rung = rung_raw.upper() if rung_raw else None

        fixture = {
            "query": query,
            "screenshot_path": None,
            "expected_intent": intent,
            "expected_rung": rung,
            "expected_action": None,
            "_note": "CANDIDATE: verify expected_intent/rung before moving to fixtures/",
            "_source_ts": record.get("ts"),
        }

        out = _CANDIDATES / f"{slugify(query)}.json"
        if out.exists():
            already_exists += 1
            continue

        out.write_text(json.dumps(fixture, indent=2), encoding="utf-8")
        emitted += 1

    print(f"Emitted {emitted} new, {already_exists} already existed, {skipped} skipped.")
    print(f"Candidates → {_CANDIDATES}")
    print("Edit expected values, remove _note/_source_ts, move to evals/fixtures/ to score.")


if __name__ == "__main__":
    main()
