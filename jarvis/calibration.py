"""Calibration script: fit ADAPTER_RELIABILITY from labeled telemetry outcomes.

Algorithm
---------
For each (adapter_source, app_class) bucket with N labeled records:
  - success_rate = count(answer_correct=True) / N
  - smoothed     = (successes + PRIOR_ALPHA) / (N + PRIOR_ALPHA + PRIOR_BETA)
    where PRIOR_ALPHA / (PRIOR_ALPHA + PRIOR_BETA) = 0.7 (neutral prior)
  - new_reliability = clip(smoothed, MIN_RELIABILITY, MAX_RELIABILITY)

Only updates buckets with >= MIN_SAMPLES labeled records.  Buckets with fewer
observations keep their current config value unchanged.

Output: prints a ready-to-paste ADAPTER_RELIABILITY dict and optionally writes
it to config.py in-place if --apply is given.

Usage
-----
  python -m jarvis.calibration                     # dry-run, print table
  python -m jarvis.calibration --apply             # write to config.py
  python -m jarvis.calibration --telemetry PATH    # custom telemetry file
  python -m jarvis.calibration --min-samples 10    # require more data
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

# Bayesian smoothing prior: equivalent to 10 pseudo-observations at 0.7 success rate.
_PRIOR_ALPHA = 7.0     # pseudo-successes
_PRIOR_BETA  = 3.0     # pseudo-failures
_MIN_RELIABILITY = 0.05
_MAX_RELIABILITY = 1.0
_DEFAULT_MIN_SAMPLES = 5


# ---------------------------------------------------------------------------
# Load telemetry
# ---------------------------------------------------------------------------

def load_telemetry(path: Path) -> list[dict]:
    records: list[dict] = []
    if not path.exists():
        return records
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return records


# ---------------------------------------------------------------------------
# Aggregate by (source, app_class)
# ---------------------------------------------------------------------------

def _extract_source(record: dict) -> str | None:
    """Extract the adapter source from a telemetry record.

    The ``answer_source`` field may be the raw source tag ("uia", "ocr",
    "fusion", "moondream", "gemini", "world_state", …) or a composite like
    "fusion/uia".  We normalise to the canonical adapter name.
    """
    src = (record.get("answer_source") or "").lower().strip()
    if not src or src in ("", "none", "fallback"):
        return None
    # Composite "fusion/X" → take the primary tag X.
    if "/" in src:
        src = src.split("/")[-1]
    # Map known tags to canonical adapter names.
    _MAP = {
        "uia":         "uia",
        "ocr":         "ocr",
        "cv":          "cv",
        "vision":      "vision",
        "moondream":   "moondream",
        "gemini":      "vision",   # Gemini vision answer ≈ vision adapter
        "fusion":      "uia",      # fusion with UIA as primary
        "world_state": None,       # cross-window cache — not a direct adapter
    }
    return _MAP.get(src)


def aggregate(records: list[dict]) -> dict[tuple[str, str | None], dict]:
    """Return counts keyed by (source, app_class).

    Each value is {"successes": int, "total": int}.
    """
    from collections import defaultdict

    counts: dict[tuple[str, str | None], dict[str, int]] = defaultdict(
        lambda: {"successes": 0, "total": 0}
    )
    for rec in records:
        if rec.get("answer_correct") is None:
            continue   # unlabeled — skip
        source = _extract_source(rec)
        if source is None:
            continue
        app_class: str | None = rec.get("app_class") or None
        key = (source, app_class)
        counts[key]["total"] += 1
        if rec["answer_correct"]:
            counts[key]["successes"] += 1
    return dict(counts)


# ---------------------------------------------------------------------------
# Fit reliability values
# ---------------------------------------------------------------------------

def fit_reliability(
    counts: dict[tuple[str, str | None], dict[str, int]],
    current_table: dict[tuple[str, str | None], float],
    min_samples: int = _DEFAULT_MIN_SAMPLES,
) -> dict[tuple[str, str | None], float]:
    """Return an updated reliability table.

    Buckets with < min_samples keep their current value.
    """
    updated = dict(current_table)
    for key, c in counts.items():
        if c["total"] < min_samples:
            continue
        smoothed = (c["successes"] + _PRIOR_ALPHA) / (c["total"] + _PRIOR_ALPHA + _PRIOR_BETA)
        clipped = max(_MIN_RELIABILITY, min(_MAX_RELIABILITY, smoothed))
        updated[key] = round(clipped, 4)
    return updated


# ---------------------------------------------------------------------------
# Format for printing / config patch
# ---------------------------------------------------------------------------

def format_table(table: dict[tuple[str, str | None], float]) -> str:
    lines = ["ADAPTER_RELIABILITY: dict[tuple[str, str | None], float] = {"]
    # Group by source for readability.
    from collections import defaultdict as _dd
    by_source: dict[str, list[tuple[str | None, float]]] = _dd(list)
    for (src, app), val in sorted(table.items()):
        by_source[src].append((app, val))
    for src, entries in sorted(by_source.items()):
        lines.append(f"    # {src}")
        for app, val in sorted(entries, key=lambda x: (x[0] or "")):
            key_repr = f'("{src}", {repr(app)})'
            lines.append(f"    {key_repr}: {val},")
    lines.append("}")
    return "\n".join(lines)


def patch_config(new_table: dict[tuple[str, str | None], float], config_path: Path) -> None:
    """Replace the ADAPTER_RELIABILITY block in config.py in-place."""
    text = config_path.read_text(encoding="utf-8")

    new_block = format_table(new_table)
    # Match from 'ADAPTER_RELIABILITY' through the closing '}'
    pattern = re.compile(
        r"ADAPTER_RELIABILITY\s*:.*?=\s*\{.*?\}",
        re.DOTALL,
    )
    if not pattern.search(text):
        print("[calibration] Could not locate ADAPTER_RELIABILITY block in config.py",
              file=sys.stderr)
        return

    patched = pattern.sub(new_block, text, count=1)
    config_path.write_text(patched, encoding="utf-8")
    print(f"[calibration] config.py updated at {config_path}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fit ADAPTER_RELIABILITY from telemetry")
    parser.add_argument("--telemetry", metavar="FILE",
                        help="Path to telemetry JSONL (default: ~/.jarvis/telemetry.jsonl)")
    parser.add_argument("--min-samples", type=int, default=_DEFAULT_MIN_SAMPLES,
                        metavar="N", help="Min labeled records per bucket before updating")
    parser.add_argument("--apply", action="store_true",
                        help="Write updated table to config.py (dry-run by default)")
    args = parser.parse_args(argv)

    # Load telemetry.
    if args.telemetry:
        tel_path = Path(args.telemetry)
    else:
        import config as _cfg
        tel_path = _cfg.TELEMETRY_PATH

    records = load_telemetry(tel_path)
    labeled = [r for r in records if r.get("answer_correct") is not None]
    print(f"[calibration] {len(records)} total records, {len(labeled)} labeled")

    if not labeled:
        print("[calibration] No labeled records found. Label records using telemetry.label_outcome().")
        return 0

    counts = aggregate(records)
    if not counts:
        print("[calibration] No actionable (source, app_class) buckets found.")
        return 0

    # Load current reliability table from config.
    import config as _cfg
    current_table = dict(_cfg.ADAPTER_RELIABILITY)
    updated_table = fit_reliability(counts, current_table, min_samples=args.min_samples)

    # Report changes.
    changed: list[tuple[tuple[str, str | None], float, float]] = []
    for key, new_val in updated_table.items():
        old_val = current_table.get(key)
        if old_val is not None and abs(new_val - old_val) > 0.001:
            changed.append((key, old_val, new_val))

    if changed:
        print("\nProposed reliability changes:")
        for key, old, new in sorted(changed):
            print(f"  {key}:  {old:.4f}  →  {new:.4f}")
    else:
        print("No significant changes — table already well-calibrated.")

    if args.apply:
        config_path = Path(_cfg.__file__).resolve()
        patch_config(updated_table, config_path)
    else:
        print("\n[calibration] Dry-run. Pass --apply to update config.py.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
