"""Thin Ollama HTTP client — non-blocking via daemon thread + queue."""

from __future__ import annotations

import json
import queue
import threading
from typing import Any

import config

_OLLAMA_URL = "http://localhost:11434/api/generate"


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        text = text.rsplit("```", 1)[0].strip()
    return text


def _call(prompt: str, timeout_ms: int) -> str | None:
    """POST to Ollama and return the response string, or None on any failure."""
    import urllib.request
    import urllib.error

    payload = json.dumps({
        "model": config.LOCAL_LLM_MODEL,
        "prompt": prompt,
        "stream": False,
    }).encode()

    result_q: queue.Queue[str | None] = queue.Queue()

    def _worker() -> None:
        try:
            req = urllib.request.Request(
                _OLLAMA_URL,
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            timeout_s = timeout_ms / 1000.0
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                body = json.loads(resp.read())
            result_q.put(body.get("response", ""))
        except Exception:
            result_q.put(None)

    threading.Thread(target=_worker, daemon=True).start()

    try:
        return result_q.get(timeout=timeout_ms / 1000.0 + 0.2)
    except queue.Empty:
        return None


def complete_text(prompt: str, timeout_ms: int | None = None) -> str | None:
    """Return raw text from the local LLM, or None on timeout/error."""
    return _call(prompt, timeout_ms or config.LOCAL_LLM_TIMEOUT_MS)


def complete_json(prompt: str, timeout_ms: int | None = None) -> dict[str, Any] | None:
    """Return parsed JSON dict from the local LLM, or None on timeout/parse error."""
    raw = _call(prompt, timeout_ms or config.LOCAL_LLM_TIMEOUT_MS)
    if raw is None:
        return None
    try:
        return json.loads(_strip_fences(raw))
    except (json.JSONDecodeError, ValueError):
        return None
