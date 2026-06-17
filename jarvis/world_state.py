"""WorldState: holds ScreenModels for multiple windows simultaneously.

The active slot tracks the window the current session is grounding against.
The registry stores recently-perceived windows keyed by "process:title" so
cross-window references ("paste into Slack") can be resolved without a new
full perception pass.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from screen_model import ScreenModel

# A registry entry that is older than this is evicted on access.
_REGISTRY_TTL_SEC = 120.0

# Maximum number of non-active ScreenModels kept in the registry.
_REGISTRY_MAX = 8


@dataclass
class _RegistryEntry:
    model: "ScreenModel"
    registered_at: float = field(default_factory=time.monotonic)


class WorldState:
    """Thread-safe multi-window ScreenModel registry.

    Callers
    -------
    - ``update_active(model)``  — called after every successful perception pass
      for the session's target window.
    - ``register_window(model)`` — called whenever *any* window is perceived
      (background watcher, escalation pass).
    - ``find_window(name_hint)`` — locate a named window for cross-window
      grounding ("paste into Slack").
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active: Optional["ScreenModel"] = None
        self._registry: dict[str, _RegistryEntry] = {}

    # ------------------------------------------------------------------
    # Writers (must be called while _lock is NOT held by caller)
    # ------------------------------------------------------------------

    def update_active(self, model: "ScreenModel") -> None:
        """Set the active ScreenModel and mirror it into the registry."""
        with self._lock:
            self._active = model
            self._register_locked(model)

    def register_window(self, model: "ScreenModel") -> None:
        """Register a ScreenModel for a non-active window."""
        with self._lock:
            self._register_locked(model)

    def _register_locked(self, model: "ScreenModel") -> None:
        key = self._key(model)
        self._registry[key] = _RegistryEntry(model=model)
        self._evict_locked()

    def _evict_locked(self) -> None:
        now = time.monotonic()
        # Remove expired entries first.
        expired = [k for k, e in self._registry.items()
                   if now - e.registered_at > _REGISTRY_TTL_SEC]
        for k in expired:
            del self._registry[k]
        # If still over cap, drop the oldest.
        while len(self._registry) > _REGISTRY_MAX:
            oldest = min(self._registry, key=lambda k: self._registry[k].registered_at)
            del self._registry[oldest]

    # ------------------------------------------------------------------
    # Readers
    # ------------------------------------------------------------------

    @property
    def active(self) -> Optional["ScreenModel"]:
        with self._lock:
            return self._active

    def find_window(self, name_hint: str) -> Optional["ScreenModel"]:
        """Return the best-matching ScreenModel from the registry.

        Matching strategy (case-insensitive, in priority order):
        1. Exact process name match.
        2. Process name substring match.
        3. Window title substring match.

        Returns None if no live (non-stale, non-expired) match is found.
        """
        if not name_hint:
            return None
        hint = name_hint.lower().strip()
        now = time.monotonic()

        with self._lock:
            candidates: list[tuple[int, "ScreenModel"]] = []
            for key, entry in self._registry.items():
                if now - entry.registered_at > _REGISTRY_TTL_SEC:
                    continue
                model = entry.model
                if model.stale:
                    continue
                process = (model.target.process or "").lower()
                title = (model.target.title or "").lower()
                if process == hint:
                    candidates.append((0, model))
                elif hint in process:
                    candidates.append((1, model))
                elif hint in title:
                    candidates.append((2, model))

            if not candidates:
                return None
            # Best priority (lowest score), then most recently registered.
            candidates.sort(key=lambda t: (t[0], -self._registry[self._key(t[1])].registered_at))
            return candidates[0][1]

    def all_windows(self) -> list["ScreenModel"]:
        """Return all non-stale, non-expired ScreenModels (active first)."""
        now = time.monotonic()
        with self._lock:
            active = self._active
            result: list["ScreenModel"] = []
            if active is not None and not active.stale:
                result.append(active)
            for entry in sorted(self._registry.values(), key=lambda e: -e.registered_at):
                if now - entry.registered_at > _REGISTRY_TTL_SEC:
                    continue
                if entry.model.stale:
                    continue
                if active is not None and entry.model is active:
                    continue
                result.append(entry.model)
            return result

    def invalidate_active(self) -> None:
        """Mark the active ScreenModel as stale (called by UIAWatcher)."""
        with self._lock:
            self._active = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _key(model: "ScreenModel") -> str:
        process = (model.target.process or "unknown").lower()
        title = (model.target.title or "")[:60]
        return f"{process}:{title}"

    def build_transcription_prompt(self) -> str:
        """Return a short Whisper initial_prompt biasing recognition toward live app names.

        Combines TRANSCRIPTION_STATIC_APPS (config) with process/title tokens from the
        current registry, deduplicates case-insensitively, and keeps the result under
        ~200 characters so it fits Whisper's prompt window without crowding it out.
        """
        import re
        import config

        # Start with the static list (already properly cased).
        seen_lower: set[str] = set()
        tokens: list[str] = []
        for name in config.TRANSCRIPTION_STATIC_APPS:
            lc = name.lower()
            if lc not in seen_lower:
                seen_lower.add(lc)
                tokens.append(name)

        # Add live process and title tokens from the registry (non-stale, non-expired).
        now = time.monotonic()
        with self._lock:
            entries = list(self._registry.values())

        _SKIP_PROCESS = frozenset({"python", "python3", "pythonw", "explorer", "unknown"})
        _TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9+#._-]{1,}")

        for entry in entries:
            if now - entry.registered_at > _REGISTRY_TTL_SEC:
                continue
            if entry.model.stale:
                continue
            raw_process = (entry.model.target.process or "").strip()
            raw_title = (entry.model.target.title or "").strip()
            for raw in (raw_process, raw_title):
                for tok in _TOKEN_RE.findall(raw):
                    lc = tok.lower()
                    if lc in _SKIP_PROCESS or lc in seen_lower or len(tok) < 2:
                        continue
                    seen_lower.add(lc)
                    tokens.append(tok)

        # Build the prompt string; trim to fit within ~200 chars.
        prompt = ", ".join(tokens)
        if len(prompt) > 200:
            # Drop tokens from the end (live ones) until it fits; static set always fits.
            while len(prompt) > 200 and tokens:
                tokens.pop()
                prompt = ", ".join(tokens)
        return prompt
