"""mss screenshot capture — primary monitor and target-window crop.

Coordinate invariant: all returned origins are in virtual-desktop pixel space
(physical screen pixels), consistent with UIA rectangles and OCR crop-relative coords
once the process is declared DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

import cv2
import mss
import numpy as np

if TYPE_CHECKING:
    from perception_target import PerceptionTarget


def _get_monitor_dpi(hwnd: int) -> float:
    """Return DPI scale for the monitor containing hwnd (1.0 = 96 DPI = 100%).

    Falls back to 1.0 on any error (non-Windows, old OS, missing hwnd).
    """
    try:
        import ctypes
        MONITOR_DEFAULTTONEAREST = 2
        MDT_EFFECTIVE_DPI = 0
        hmon = ctypes.windll.user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
        if not hmon:
            return 1.0
        dpi_x = ctypes.c_uint(96)
        ctypes.windll.shcore.GetDpiForMonitor(
            hmon, MDT_EFFECTIVE_DPI, ctypes.byref(dpi_x), ctypes.byref(ctypes.c_uint()),
        )
        return dpi_x.value / 96.0
    except Exception:
        return 1.0


def resolve_target_bounds(target: "PerceptionTarget") -> tuple[tuple[int, int, int, int], bool]:
    """Re-resolve the target window's current bounds at perception time.

    Returns (bounds, stale) where:
    - bounds is the current (x, y, w, h) in virtual-desktop pixels
    - stale=True means the window is gone, minimized, or zero-sized

    The mss grab always happens fresh at perception time; this gives us the
    up-to-date rect even if the window moved or resized since wake.
    """
    if not target.hwnd:
        return target.bounds, True
    try:
        import ctypes
        SW_SHOWMINIMIZED = 2
        # WINDOWPLACEMENT to detect minimized state
        class WINDOWPLACEMENT(ctypes.Structure):
            _fields_ = [
                ("length", ctypes.c_uint),
                ("flags", ctypes.c_uint),
                ("showCmd", ctypes.c_uint),
                ("ptMinPosition", ctypes.c_long * 2),
                ("ptMaxPosition", ctypes.c_long * 2),
                ("rcNormalPosition", ctypes.c_long * 4),
            ]
        wp = WINDOWPLACEMENT()
        wp.length = ctypes.sizeof(WINDOWPLACEMENT)
        ctypes.windll.user32.GetWindowPlacement(target.hwnd, ctypes.byref(wp))
        if wp.showCmd == SW_SHOWMINIMIZED:
            return target.bounds, True

        # Check window still exists and is visible
        if not ctypes.windll.user32.IsWindow(target.hwnd):
            return target.bounds, True
        if not ctypes.windll.user32.IsWindowVisible(target.hwnd):
            return target.bounds, True

        # Re-read current rect
        class RECT(ctypes.Structure):
            _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                        ("right", ctypes.c_long), ("bottom", ctypes.c_long)]
        r = RECT()
        ctypes.windll.user32.GetWindowRect(target.hwnd, ctypes.byref(r))
        w = r.right - r.left
        h = r.bottom - r.top
        if w <= 0 or h <= 0:
            return target.bounds, True
        return (r.left, r.top, w, h), False
    except Exception:
        return target.bounds, False


def capture_primary_monitor() -> np.ndarray:
    """Grab the primary monitor and return a BGR numpy array."""
    with mss.mss() as sct:
        raw = sct.grab(sct.monitors[1])
    return cv2.cvtColor(np.array(raw), cv2.COLOR_BGRA2BGR)


def capture_full_screen() -> tuple[np.ndarray, tuple[int, int], float, bool]:
    """Grab the primary monitor; return (BGR frame, origin=(0, 0), dpi_scale=1.0, stale=False)."""
    return capture_primary_monitor(), (0, 0), 1.0, False


def capture_target(target: "PerceptionTarget") -> tuple[np.ndarray, tuple[int, int], float, bool]:
    """Grab the primary monitor and crop to the target's CURRENT bounds (re-resolved at call time).

    Returns (BGR crop, (origin_x, origin_y), dpi_scale, stale).
    - origin is in virtual-desktop pixel space.
    - stale=True means the window was gone/minimized; callers should propagate to ScreenModel.stale.
    Falls back to full-screen with origin (0, 0) when bounds are invalid or target.is_self.
    """
    frame = capture_primary_monitor()
    dpi_scale = _get_monitor_dpi(target.hwnd)

    if target.is_self:
        return frame, (0, 0), dpi_scale, False

    # Re-resolve bounds at perception time — window may have moved/resized since wake.
    bounds, stale = resolve_target_bounds(target)
    if stale:
        return frame, (0, 0), dpi_scale, True

    x, y, w, h = bounds
    if w <= 0 or h <= 0:
        return frame, (0, 0), dpi_scale, True

    fh, fw = frame.shape[:2]
    cx = max(0, min(x, fw - 1))
    cy = max(0, min(y, fh - 1))
    x2 = max(0, min(x + w, fw))
    y2 = max(0, min(y + h, fh))

    if x2 <= cx or y2 <= cy:
        return frame, (0, 0), dpi_scale, True

    return frame[cy:y2, cx:x2], (cx, cy), dpi_scale, False


if __name__ == "__main__":
    screenshot = capture_primary_monitor()
    out_path = Path(tempfile.gettempdir()) / "jarvis_capture_test.png"
    cv2.imwrite(str(out_path), screenshot)
    print(out_path)
    print(screenshot.shape)
