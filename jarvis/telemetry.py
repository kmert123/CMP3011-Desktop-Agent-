"""Append-only JSONL telemetry logger for query records."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from typing import Any

import config

_FIELDS = (
    "ts", "query", "intent", "perception_rung",
    "used_cache", "escalated", "escalated_rung", "latency_ms", "error", "action_kind",
    "action_verified", "answer_source",
    "router_source", "router_confidence",
    # Outcome labels (set post-hoc via label_outcome() or by the eval harness)
    "answer_correct",   # bool | None — human/harness label
    "rung_reached",     # str  | None — actual rung that produced the accepted answer
    "app_class",        # str  | None — AppClass.value of the perception target
    # Richer fields added in Task 7
    "turn_id",          # str  | None — correlates with traces.jsonl
    "element_count",    # int  | None — fused element count from ScreenModel
    "char_count",       # int  | None — perception text length
    "tool_calls",       # list | None — list of tool names called by the model
    "screen_block_chars", # int | None — chars in screen context block sent to model
)


def build_record(**kwargs: Any) -> dict:
    """Return a telemetry record with all standard fields, missing ones set to None."""
    record: dict[str, Any] = {f: None for f in _FIELDS}
    record["ts"] = datetime.now(timezone.utc).isoformat()
    record.update({k: v for k, v in kwargs.items() if k in _FIELDS})
    return record


def log_query(record: dict) -> None:
    """Append one JSON line to the telemetry file. Never raises."""
    try:
        path = config.TELEMETRY_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except Exception as exc:
        print(f"[telemetry] write failed: {exc}", file=sys.stderr)


def read_recent(n: int) -> list[dict]:
    """Return the last n records from the telemetry file. Returns [] on any error."""
    try:
        path = config.TELEMETRY_PATH
        if not path.exists():
            return []
        lines = path.read_text(encoding="utf-8").splitlines()
        return [json.loads(line) for line in lines[-n:] if line.strip()]
    except Exception as exc:
        print(f"[telemetry] read failed: {exc}", file=sys.stderr)
        return []


def label_outcome(
    record: dict,
    *,
    answer_correct: "bool | None" = None,
    rung_reached: "str | None" = None,
    app_class: "str | None" = None,
) -> dict:
    """Return a copy of *record* with outcome label fields filled in.

    Call this after a query completes (or from the eval harness) to attach
    ground-truth labels used by the calibration script.
    """
    updated = dict(record)
    if answer_correct is not None:
        updated["answer_correct"] = answer_correct
    if rung_reached is not None:
        updated["rung_reached"] = rung_reached
    if app_class is not None:
        updated["app_class"] = app_class
    return updated


if __name__ == "__main__":
    rec = build_record(query="what is on screen?", intent="describe", latency_ms=420)
    log_query(rec)
    print("Logged:", rec)
    print("Recent:", read_recent(5))
