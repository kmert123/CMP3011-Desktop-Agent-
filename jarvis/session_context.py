"""In-memory session state: conversation turns, window history, and screen read cache."""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

import config

if TYPE_CHECKING:
    import numpy as np
    from perception_target import PerceptionTarget

_MAX_TURNS = 10
_MAX_WINDOWS = 5
_MAX_CHARS = 1600   # ~400 tokens at 4 chars/token

# AppClass values that use the tighter browser TTL and get content-key checks.
_BROWSER_APP_CLASSES = frozenset({"chromium_electron", "uwp"})


def _get_window_title(hwnd: int) -> str:
    """Return the current window title for hwnd; empty string on any error."""
    if not hwnd or sys.platform != "win32":
        return ""
    try:
        import win32gui
        return win32gui.GetWindowText(hwnd) or ""
    except Exception:
        return ""


def _get_browser_url_omnibox(hwnd: int) -> str:
    """Fallback: read Chrome's address bar via child-window enumeration.

    Enumerates child windows of hwnd looking for the omnibox edit control
    (class Chrome_OmniboxView or an Edit child of Chrome_WidgetWin_*).
    Does not require Chrome accessibility mode.  Returns "" when nothing
    readable is found.  Gated by config.CHROME_OMNIBOX_URL_FALLBACK.
    """
    if not hwnd or sys.platform != "win32":
        return ""
    try:
        import win32gui
        import win32con

        found: list[str] = []

        def _enum_child(child_hwnd: int, _param: int) -> bool:
            try:
                cls = win32gui.GetClassName(child_hwnd)
                if cls in ("Chrome_OmniboxView", "OmniboxViewViews"):
                    text = win32gui.GetWindowText(child_hwnd)
                    if text:
                        found.append(text)
                        return False  # stop after first hit
            except Exception:
                pass
            return True  # continue enumeration

        win32gui.EnumChildWindows(hwnd, _enum_child, 0)
        return found[0] if found else ""
    except Exception:
        return ""


def _get_browser_url(hwnd: int) -> str:
    """Best-effort: read the address-bar value via a single UIA query.

    Falls back to child-window enumeration (Chrome_OmniboxView class) when
    the UIA walk yields nothing and CHROME_OMNIBOX_URL_FALLBACK is True.
    Returns empty string if not found or on any error — this is a bonus
    check, never a hard requirement.
    """
    if not hwnd or sys.platform != "win32":
        return ""
    try:
        from pywinauto import Desktop
        desktop = Desktop(backend="uia")
        wrapper = desktop.window(handle=hwnd).wrapper_object()

        # Walk at most depth-3 looking for an Edit control whose name hints at URL/address
        _URL_HINTS = frozenset({"address", "url", "location", "search", "omnibox"})

        def _find_address(elem, depth: int) -> str:
            if depth > 3:
                return ""
            try:
                ctrl = (getattr(elem.element_info, "control_type", "") or "").strip()
                name = (elem.element_info.name or "").lower()
                if ctrl == "Edit" and any(h in name for h in _URL_HINTS):
                    val = (elem.window_text() or "").strip()
                    if val:
                        return val
                for child in elem.children():
                    found = _find_address(child, depth + 1)
                    if found:
                        return found
            except Exception:
                pass
            return ""

        url = _find_address(wrapper, 0)
    except Exception:
        url = ""

    if not url and config.CHROME_OMNIBOX_URL_FALLBACK:
        url = _get_browser_url_omnibox(hwnd)

    return url


@dataclass
class SessionContext:
    turns: list[dict[str, str]] = field(default_factory=list)
    recent_windows: list[dict[str, str]] = field(default_factory=list)
    last_screen_read: dict[str, Any] | None = None
    active_target: "PerceptionTarget | None" = field(default=None)
    # WorldState is created lazily on first access so SessionContext can be
    # instantiated without importing world_state at module load time.
    _world_state: Any = field(default=None, repr=False, compare=False)

    @property
    def world_state(self):
        """Lazily-initialised WorldState for this session."""
        if self._world_state is None:
            from world_state import WorldState
            self._world_state = WorldState()
        return self._world_state

    def set_active_target(self, t: "PerceptionTarget") -> None:
        self.active_target = t

    def add_turn(self, role: str, text: str, window_sig: "str | None" = None) -> None:
        """Record a conversation turn. window_sig is (process, app_class, title) joined
        by "|" — passed by the answer worker so history can be gated by window continuity."""
        self.turns.append({"role": role, "text": text, "window_sig": window_sig or ""})
        if len(self.turns) > _MAX_TURNS:
            self.turns = self.turns[-_MAX_TURNS:]

    def note_window(self, title: str, process: str) -> None:
        entry = {"title": title, "process": process}
        if self.recent_windows and self.recent_windows[-1] == entry:
            return
        self.recent_windows.append(entry)
        if len(self.recent_windows) > _MAX_WINDOWS:
            self.recent_windows = self.recent_windows[-_MAX_WINDOWS:]

    def set_screen_read(
        self,
        text: str,
        source: str,
        process: str,
        screen_hash: str,
        roi_crop: "np.ndarray | None" = None,
        hwnd: int = 0,
        app_class: str | None = None,
        screen_model: "Any | None" = None,
    ) -> None:
        """Store a screen read result.

        screen_hash is the roi_dhash (16×16, 256-bit) of the target-region crop.
        roi_crop, if provided, is saved for density comparison on the next freshness check.
        hwnd / app_class are stored so screen_read_fresh can apply per-class TTL
        and content-key checks for browser/Electron targets.
        screen_model is the ScreenModel that produced *text*; storing it here keeps the
        cache self-contained so a cache hit can restore the full structured context
        (full text, element tree, escalation eligibility) instead of degrading to a
        truncated text-only prompt.
        """
        content_title = _get_window_title(hwnd)
        content_url = (
            _get_browser_url(hwnd)
            if (app_class in _BROWSER_APP_CLASSES)
            else ""
        )
        self.last_screen_read = {
            "text": text,
            "source": source,
            "process": process,
            "screen_hash": screen_hash,
            "roi_crop": roi_crop if (roi_crop is not None and roi_crop.size > 0) else None,
            "ts": datetime.now(timezone.utc),
            "hwnd": hwnd,
            "app_class": app_class,
            "content_title": content_title,
            "content_url": content_url,
            "screen_model": screen_model,
        }

    def invalidate_screen_cache(self) -> None:
        """Discard the cached screen read. Called by UIAWatcher on structural changes."""
        self.last_screen_read = None
        if self._world_state is not None:
            self._world_state.invalidate_active()

    def screen_read_fresh(
        self,
        process: str,
        current_hash: str,
        current_crop: "np.ndarray | None" = None,
    ) -> bool:
        """True iff the cached read is still valid for the current ROI state.

        Conditions (ALL must hold):
        1. Same process.
        2. Age < TTL  (BROWSER_SCREEN_READ_TTL for CHROMIUM_ELECTRON/UWP, else SCREEN_READ_TTL).
        3. For browser/Electron: window title and URL-bar value unchanged (content key).
        4. Hamming(prev_roi_hash, current_roi_hash) ≤ CACHE_HAMMING_MAX.
        5. density_delta(prev_crop, current_crop) ≤ CACHE_DENSITY_DELTA_MAX  (if crops available).
        """
        if self.last_screen_read is None:
            return False
        if self.last_screen_read.get("process", "") != process:
            return False

        app_class = self.last_screen_read.get("app_class")
        is_browser = app_class in _BROWSER_APP_CLASSES
        ttl = config.BROWSER_SCREEN_READ_TTL if is_browser else config.SCREEN_READ_TTL

        age = (datetime.now(timezone.utc) - self.last_screen_read["ts"]).total_seconds()
        if age >= ttl:
            return False

        # Content-key check for browser/Electron: any navigation → immediate miss.
        if is_browser:
            hwnd = self.last_screen_read.get("hwnd", 0)
            if hwnd:
                current_title = _get_window_title(hwnd)
                cached_title = self.last_screen_read.get("content_title", "")
                if cached_title and current_title and current_title != cached_title:
                    return False
                # URL check: miss when either side has a URL and they differ.
                # A cached "" + fresh non-empty URL means we just gained visibility
                # into the URL, which typically means the tab/page changed.
                cached_url = self.last_screen_read.get("content_url", "")
                current_url = _get_browser_url(hwnd)
                if current_url and cached_url and current_url != cached_url:
                    return False
                if current_url and not cached_url:
                    # Gained URL visibility between reads → treat as content change.
                    return False

        prev_hash = self.last_screen_read.get("screen_hash", "")
        if not prev_hash or not current_hash:
            return True  # hash unavailable; TTL alone decides

        try:
            from screen_model import hamming, density_delta
            # P12/F2: tighter Hamming for browser/Electron to avoid feed-state collisions.
            # CDP (Task 6) supersedes this with an exact URL key.
            hamming_max = (
                config.BROWSER_CACHE_HAMMING_MAX if is_browser else config.CACHE_HAMMING_MAX
            )
            if hamming(prev_hash, current_hash) > hamming_max:
                return False
            prev_crop = self.last_screen_read.get("roi_crop")
            if prev_crop is not None and current_crop is not None and current_crop.size > 0:
                if density_delta(prev_crop, current_crop) > config.CACHE_DENSITY_DELTA_MAX:
                    return False
            return True
        except (ValueError, ImportError):
            return True  # malformed hex or missing module; trust TTL

    def to_prompt_block(
        self,
        current_window_sig: "str | None" = None,
        screen_model: "Any | None" = None,
    ) -> str:
        """Return app/window context + recent Q&A history.

        Screen/perception text is intentionally excluded — callers inject the
        CURRENT perception block directly so stale screen text never leaks into
        the prompt via history.

        When current_window_sig is provided, turns recorded on a different
        window are handled per HISTORY_CROSS_WINDOW:
          "annotate" — kept but prefixed with "[different window] " (default)
          "drop"     — oldest cross-window turns are omitted entirely;
                       the immediately preceding turn is annotated for continuity.

        screen_model (P12/F3): when the fresh perception is below the content
        floor (HISTORY_CONTENT_FLOOR_ELEMENTS or HISTORY_CONTENT_FLOOR_CHARS),
        history is demoted to the last 1 turn and prefixed with a staleness
        warning so the model does not anchor on prior answers.
        """
        parts: list[str] = []

        if self.recent_windows:
            win = self.recent_windows[-1]
            parts.append(f"App: {win['title']} ({win['process']})")

        if self.turns:
            # P12/F3: detect weak perception and demote history when below floor.
            perception_weak = False
            if screen_model is not None:
                try:
                    content_elems = [
                        e for e in screen_model.elements
                        if getattr(e, "in_content_region", True) and e.text
                    ]
                    content_text = getattr(screen_model, "full_text", "") or ""
                    if (
                        len(content_elems) < config.HISTORY_CONTENT_FLOOR_ELEMENTS
                        or len(content_text) < config.HISTORY_CONTENT_FLOOR_CHARS
                    ):
                        perception_weak = True
                except Exception:
                    pass

            n = 0 if perception_weak else config.MODEL_HISTORY_TURNS
            if n > 0:
                recent = self.turns[-n:]
                lines: list[str] = []

                strategy = getattr(config, "HISTORY_CROSS_WINDOW", "annotate")
                for i, t in enumerate(recent):
                    prefix = "User" if t["role"] == "user" else "Jarvis"
                    sig = t.get("window_sig", "")
                    is_cross = bool(
                        current_window_sig
                        and sig
                        and sig != current_window_sig
                    )
                    if is_cross and strategy == "drop":
                        # Keep only the immediately preceding turn pair for continuity.
                        if i < len(recent) - 2:
                            continue
                        lines.append(f"[different window] {prefix}: {t['text']}")
                    elif is_cross:
                        lines.append(f"[different window] {prefix}: {t['text']}")
                    else:
                        lines.append(f"{prefix}: {t['text']}")
                if lines:
                    parts.append("History:\n" + "\n".join(lines))

        block = "\n\n".join(parts)
        if len(block) > _MAX_CHARS:
            block = block[:_MAX_CHARS] + "…"
        return block
