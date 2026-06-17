"""Structured per-turn trace written to ~/.jarvis/traces.jsonl."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any


class TurnTrace:
    def __init__(self, turn_id: str, wake_ts: float) -> None:
        self.turn_id = turn_id
        self.wake_ts = wake_ts
        self._stages: list[dict[str, Any]] = []

    def record(self, stage: str, **kwargs: Any) -> None:
        self._stages.append({"stage": stage, "ts": time.monotonic(), **kwargs})

    def record_tool_call(
        self,
        name: str,
        args: dict,
        result_summary: str,
        budget_remaining: int,
    ) -> None:
        self.record(
            "TOOLS",
            name=name,
            args=args,
            result_summary=result_summary,
            budget_remaining=budget_remaining,
        )

    @property
    def full_dict(self) -> dict[str, Any]:
        return {
            "turn_id": self.turn_id,
            "wake_ts": self.wake_ts,
            "stages": self._stages,
        }

    def finish(self, **kwargs: Any) -> None:
        self.record("OUTCOME", **kwargs)
        try:
            path = Path.home() / ".jarvis" / "traces.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(self.full_dict) + "\n")
        except Exception as exc:
            print(f"[trace] write failed: {exc}", file=sys.stderr)
