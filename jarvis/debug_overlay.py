"""Debug utility: draw ScreenElement bboxes onto a capture frame and save to disk.

Gated by config.DEBUG_OVERLAY — save_overlay() is a no-op when the flag is False.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING

import cv2
import numpy as np

if TYPE_CHECKING:
    from screen_model import ScreenModel

# BGR colors per adapter source
_SOURCE_COLORS: dict[str, tuple[int, int, int]] = {
    "uia":    (0,   210,  0),    # green
    "ocr":    (210, 100,  0),    # blue
    "cv":     (0,   210, 210),   # yellow
    "vision": (210,   0, 210),   # magenta
}
_DEFAULT_COLOR: tuple[int, int, int] = (160, 160, 160)

_FONT = cv2.FONT_HERSHEY_SIMPLEX
_FONT_SCALE = 0.42
_THICKNESS = 1


def save_overlay(
    frame: np.ndarray,
    screen_model: "ScreenModel",
    origin: tuple[int, int] = (0, 0),
) -> Path | None:
    """Draw element boxes + source + confidence onto frame (crop-local coords).

    screen_model.elements carry virtual-desktop pixel bboxes; subtracting origin
    converts them to crop-local coords for drawing.

    Saves to ~/.jarvis/debug/overlay_<timestamp_ms>.png.
    Returns the saved path, or None when DEBUG_OVERLAY is False or on any error.
    """
    import config
    if not config.DEBUG_OVERLAY:
        return None
    try:
        out = frame.copy()
        ox, oy = origin
        for elem in screen_model.elements:
            ex, ey, ew, eh = elem.bbox
            x1 = ex - ox
            y1 = ey - oy
            x2 = x1 + ew
            y2 = y1 + eh
            color = _SOURCE_COLORS.get(elem.source, _DEFAULT_COLOR)
            cv2.rectangle(out, (x1, y1), (x2, y2), color, 2)
            snippet = elem.text[:22].replace("\n", " ") if elem.text else ""
            label = f"{elem.source} r={elem.confidence:.2f} c={elem.calibrated_confidence:.2f}"
            if snippet:
                label += f" {snippet}"
            label_y = max(0, y1 - 4)
            cv2.putText(out, label, (x1, label_y), _FONT, _FONT_SCALE, color, _THICKNESS, cv2.LINE_AA)

        dest_dir = Path.home() / ".jarvis" / "debug"
        dest_dir.mkdir(parents=True, exist_ok=True)
        ts = int(time.time() * 1000)
        path = dest_dir / f"overlay_{ts}.png"
        cv2.imwrite(str(path), out)
        return path
    except Exception:
        return None
