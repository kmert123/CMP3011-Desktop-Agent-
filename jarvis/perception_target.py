"""Target window snapshot — must be captured BEFORE Jarvis UI takes focus.

The wake-word thread calls capture_foreground_target() + set_pending_target() the instant
a wake word fires. The main thread calls take_pending_target() at the start of
_voice_invocation, after which the pending slot is cleared.
"""
from __future__ import annotations

import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from app_classifier import AppClass


@dataclass
class PerceptionTarget:
    hwnd: int
    pid: int
    process: str                        # lowercase exe stem
    title: str                          # title at wake time (informational only)
    bounds: tuple[int, int, int, int]   # (x, y, w, h) screen coords at wake time; RE-RESOLVED at perception time
    is_self: bool                       # True if hwnd/pid belongs to the Jarvis process itself
    app_class: "AppClass | None" = field(default=None)  # renderer family; None until classified
    wake_ts: float = field(default=0.0) # time.monotonic() when the target was captured

    # Interaction state captured BEFORE Jarvis UI takes focus.
    # cursor_pos: virtual-desktop pixel position at wake time, or None.
    cursor_pos: Optional[tuple[int, int]] = field(default=None)
    # focused_element: raw IUIAutomationElement COM object for the focused control,
    # or None.  Queryable against the background hwnd without holding focus.
    focused_element: Any = field(default=None)
    # selection_text: text currently selected in the focused element (via TextPattern),
    # or empty string.  Grabbed eagerly here because once Jarvis has focus
    # GetFocusedElement returns Jarvis's own window.
    selection_text: str = field(default="")

    def interaction_state_fresh(self) -> bool:
        """Return True if the wake-time interaction state is still within TTL.

        cursor_pos, focused_element, and selection_text are captured once at wake
        time.  After FOCUS_STATE_TTL_MS they describe a past moment and must be
        treated as absent by all consumers.
        """
        import time as _time
        import config as _cfg
        if not self.wake_ts:
            return False
        elapsed_ms = (_time.monotonic() - self.wake_ts) * 1000
        return elapsed_ms <= _cfg.FOCUS_STATE_TTL_MS


# ---------------------------------------------------------------------------
# Self-registration — Jarvis records its own hwnd/pid at startup
# ---------------------------------------------------------------------------

_self_hwnd: int = 0
_self_pid: int = 0


def register_self(hwnd: int, pid: int) -> None:
    """Called once at startup with JarvisWindow's hwnd and os.getpid()."""
    global _self_hwnd, _self_pid
    _self_hwnd = hwnd
    _self_pid = pid


def is_self_window(hwnd: int = 0, pid: int = 0) -> bool:
    """Return True if hwnd or pid belongs to the Jarvis process itself."""
    if _self_pid and pid and _self_pid == pid:
        return True
    if _self_hwnd and hwnd and _self_hwnd == hwnd:
        return True
    return False


# ---------------------------------------------------------------------------
# Thread-safe one-shot holder
# ---------------------------------------------------------------------------

_lock: threading.Lock = threading.Lock()
_pending: Optional[PerceptionTarget] = None


def set_pending_target(t: PerceptionTarget) -> None:
    """Store a target. Overwrites any un-consumed previous capture."""
    global _pending
    with _lock:
        _pending = t


def take_pending_target() -> Optional[PerceptionTarget]:
    """Consume and return the pending target, or None if not set."""
    global _pending
    with _lock:
        t, _pending = _pending, None
        return t


# ---------------------------------------------------------------------------
# Interaction-state capture (before focus steal)
# ---------------------------------------------------------------------------

def _capture_interaction_state() -> tuple[Optional[tuple[int, int]], Any, str]:
    """Return (cursor_pos, focused_element, selection_text) right now.

    Must be called while the target app still owns focus.  All failures are
    swallowed — callers must tolerate None / empty-string returns.

    focused_element is a raw comtypes IUIAutomationElement pointer; it remains
    valid and queryable against the background hwnd after Jarvis takes focus.
    """
    cursor_pos: Optional[tuple[int, int]] = None
    focused_element: Any = None
    selection_text: str = ""

    if sys.platform != "win32":
        return cursor_pos, focused_element, selection_text

    # 1. Cursor position via GetCursorPos (virtual-desktop pixels).
    try:
        import ctypes
        import ctypes.wintypes
        pt = ctypes.wintypes.POINT()
        if ctypes.windll.user32.GetCursorPos(ctypes.byref(pt)):
            cursor_pos = (pt.x, pt.y)
    except Exception:
        pass

    # 2. Focused element + selection text via UIA COM.
    try:
        import comtypes
        import comtypes.client

        # CoInitialize on this thread if not already done.
        try:
            comtypes.CoInitialize()
        except Exception:
            pass

        uia = comtypes.client.CreateObject(
            "{ff48dba4-60ef-4201-aa87-54103eef594e}",   # CLSID_CUIAutomation
            interface=comtypes.gen.UIAutomationClient.IUIAutomation,  # type: ignore[attr-defined]
        )
        elem = uia.GetFocusedElement()
        if elem is not None:
            focused_element = elem

            # 3. TextPattern selection — grab while we still own the focused element.
            try:
                # IUIAutomationTextPattern pattern id = 10014
                _TextPatternId = 10014
                pattern_obj = elem.GetCurrentPattern(_TextPatternId)
                if pattern_obj:
                    import comtypes.gen.UIAutomationClient as _uia_mod  # type: ignore[import]
                    text_pattern = pattern_obj.QueryInterface(
                        _uia_mod.IUIAutomationTextPattern
                    )
                    sel_array = text_pattern.GetSelection()
                    if sel_array is not None:
                        parts: list[str] = []
                        for i in range(sel_array.Length):
                            r = sel_array.GetElement(i)
                            if r is not None:
                                try:
                                    parts.append(r.GetText(-1) or "")
                                except Exception:
                                    pass
                        selection_text = "".join(parts)
            except Exception:
                pass
    except Exception:
        pass

    return cursor_pos, focused_element, selection_text


# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------

def capture_foreground_target() -> PerceptionTarget:
    """Snapshot the current foreground window. Must be called before ui.show_window()."""
    if sys.platform != "win32":
        return PerceptionTarget(
            hwnd=0, pid=0, process="unknown", title="",
            bounds=(0, 0, 0, 0), is_self=False,
        )
    try:
        import win32gui
        import win32process
        import psutil

        hwnd = win32gui.GetForegroundWindow()
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        try:
            process = Path(psutil.Process(pid).exe()).stem.lower()
        except Exception:
            process = str(pid)
        title = win32gui.GetWindowText(hwnd)
        l, t, r, b = win32gui.GetWindowRect(hwnd)
        bounds = (l, t, r - l, b - t)
        from app_classifier import classify_app
        import time as _time
        app_class = classify_app(process, "", hwnd)

        # Capture interaction state while target app still owns focus.
        cursor_pos, focused_element, selection_text = _capture_interaction_state()

        return PerceptionTarget(
            hwnd=hwnd, pid=pid, process=process, title=title,
            bounds=bounds, is_self=is_self_window(hwnd, pid),
            app_class=app_class,
            wake_ts=_time.monotonic(),
            cursor_pos=cursor_pos,
            focused_element=focused_element,
            selection_text=selection_text,
        )
    except Exception:
        return PerceptionTarget(
            hwnd=0, pid=0, process="unknown", title="",
            bounds=(0, 0, 0, 0), is_self=False,
        )
