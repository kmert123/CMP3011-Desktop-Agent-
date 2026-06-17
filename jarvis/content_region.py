"""Content-region resolver: identify the primary content rectangle for a given target.

Geometric heuristic (shipped first, no new deps):
  - CHROMIUM_ELECTRON / UWP: subtract the browser-chrome top band (tab strip +
    omnibox + bookmarks) and any narrow left/right side panels.
  - All other app classes: content region == full window (no-op).

The returned bbox is in virtual-desktop pixels, same coordinate space as
ScreenElement.bbox.

Seam for future upgrades
------------------------
TODO (CV-assisted): find the largest central contour block that is not inside the
  chrome band, using cv_pipeline's region anchors.
TODO (CDP-native): when USE_CDP is True, the DOM's <main> / largest text-bearing
  scroll container gives the exact content viewport — prefer it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import config

if TYPE_CHECKING:
    import numpy as np
    from perception_target import PerceptionTarget
    from screen_model import ScreenElement


def resolve_content_region(
    target: "PerceptionTarget",
    frame: "np.ndarray | None",
    elements: "list[ScreenElement]",
    origin: "tuple[int, int]",
) -> "tuple[int, int, int, int]":
    """Return the content region bbox (x, y, w, h) in virtual-desktop pixels.

    Falls back to the full window rect if the target's app class is not a
    browser/webview, or if the window has no usable bounds.
    """
    bounds = getattr(target, "bounds", None)
    if not bounds or len(bounds) < 4:
        return _full_window(target, origin, frame)

    win_x, win_y, win_w, win_h = bounds[0], bounds[1], bounds[2], bounds[3]
    if win_w <= 0 or win_h <= 0:
        return _full_window(target, origin, frame)

    app_class = getattr(target, "app_class", None)
    if app_class is None:
        return (win_x, win_y, win_w, win_h)

    from app_classifier import AppClass
    if app_class not in (AppClass.CHROMIUM_ELECTRON, AppClass.UWP):
        # Native apps: content == full window; salience tagging is a no-op.
        return (win_x, win_y, win_w, win_h)

    # --- Geometric chrome subtraction ---
    dpi_scale = getattr(target, "dpi_scale", 1.0) or 1.0

    # Top chrome band: tab strip + omnibox + bookmarks bar.
    top_band_px = int(config.CHROME_TOP_BAND_PX * dpi_scale)

    # Narrow side panels: columns whose width < CHROME_SIDE_PANEL_MAX_FRAC of window width
    # and that run nearly the full window height. Detect from the element list.
    left_margin, right_margin = _detect_side_panels(
        elements, win_x, win_y, win_w, win_h,
        config.CHROME_SIDE_PANEL_MAX_FRAC,
    )

    content_x = win_x + left_margin
    content_y = win_y + top_band_px
    content_w = win_w - left_margin - right_margin
    content_h = win_h - top_band_px

    # Guard: never produce a degenerate region.
    if content_w <= 0 or content_h <= 0:
        return (win_x, win_y, win_w, win_h)

    return (content_x, content_y, content_w, content_h)


def _full_window(
    target: "PerceptionTarget",
    origin: "tuple[int, int]",
    frame: "np.ndarray | None",
) -> "tuple[int, int, int, int]":
    """Return the full window bbox, falling back to origin+frame shape if bounds absent."""
    bounds = getattr(target, "bounds", None)
    if bounds and len(bounds) >= 4:
        return (bounds[0], bounds[1], bounds[2], bounds[3])
    if frame is not None:
        h, w = frame.shape[:2]
        return (origin[0], origin[1], w, h)
    return (origin[0], origin[1], 0, 0)


def _detect_side_panels(
    elements: "list[ScreenElement]",
    win_x: int,
    win_y: int,
    win_w: int,
    win_h: int,
    panel_max_frac: float,
) -> "tuple[int, int]":
    """Estimate left and right side-panel widths from the element layout.

    A side panel is a cluster of elements that:
      - occupy a column of width < panel_max_frac * win_w
      - span nearly the full window height (>= 60%)

    Returns (left_margin, right_margin) in pixels.
    """
    if not elements or win_w <= 0 or win_h <= 0:
        return 0, 0

    max_panel_w = int(panel_max_frac * win_w)
    min_panel_span_h = int(0.60 * win_h)

    # Left panel: elements whose right edge <= win_x + max_panel_w
    left_x_max = win_x + max_panel_w
    left_elems = [
        e for e in elements
        if e.bbox[2] > 0 and e.bbox[3] > 0
        and (e.bbox[0] + e.bbox[2]) <= left_x_max
    ]
    left_margin = 0
    if left_elems:
        min_y = min(e.bbox[1] for e in left_elems)
        max_y = max(e.bbox[1] + e.bbox[3] for e in left_elems)
        if (max_y - min_y) >= min_panel_span_h:
            left_edge = max(e.bbox[0] + e.bbox[2] for e in left_elems)
            left_margin = max(0, left_edge - win_x)

    # Right panel: elements whose left edge >= win_x + win_w - max_panel_w
    right_x_min = win_x + win_w - max_panel_w
    right_elems = [
        e for e in elements
        if e.bbox[2] > 0 and e.bbox[3] > 0
        and e.bbox[0] >= right_x_min
    ]
    right_margin = 0
    if right_elems:
        min_y = min(e.bbox[1] for e in right_elems)
        max_y = max(e.bbox[1] + e.bbox[3] for e in right_elems)
        if (max_y - min_y) >= min_panel_span_h:
            right_edge_from_win = win_x + win_w - min(e.bbox[0] for e in right_elems)
            right_margin = max(0, right_edge_from_win)

    return left_margin, right_margin


def tag_elements_with_region(
    elements: "list[ScreenElement]",
    content_bbox: "tuple[int, int, int, int]",
) -> "list[ScreenElement]":
    """Return elements with in_content_region set by centre-point containment.

    Elements whose centre falls inside content_bbox are marked True; others False.
    If content_bbox is the full window (i.e. no chrome was stripped), all elements
    are left True.
    """
    from dataclasses import replace

    cx, cy, cw, ch = content_bbox
    result = []
    for elem in elements:
        ex, ey, ew, eh = elem.bbox
        if ew <= 0 or eh <= 0:
            result.append(elem)
            continue
        centre_x = ex + ew // 2
        centre_y = ey + eh // 2
        in_region = (cx <= centre_x < cx + cw) and (cy <= centre_y < cy + ch)
        result.append(replace(elem, in_content_region=in_region))
    return result
