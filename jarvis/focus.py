"""Cursor, focused-element, and VLM referring-expression resolution.

Resolves deictic ("this field") and descriptive ("the red Submit button")
references to a concrete ScreenElement.

Public API
----------
get_element_at_cursor(screen_model, cursor_pos) -> ScreenElement | None
get_focused_element(screen_model, focused_ref)  -> ScreenElement | None
resolve_reference_vlm(phrase, candidates, screen_model, target)
    -> ScreenElement | None
    VLM-backed last-resort resolver: renders SoM over candidates, asks the
    VLM which marker matches the phrase, returns the element — never trusting
    the VLM's own text transcription (localize-then-extract pattern).
"""

from __future__ import annotations

import logging
import math
from typing import TYPE_CHECKING, Any, Optional

import config

if TYPE_CHECKING:
    from perception_target import PerceptionTarget
    from screen_model import ScreenElement, ScreenModel

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Cursor-position resolution
# ---------------------------------------------------------------------------

def _bbox_area(elem: "ScreenElement") -> int:
    _, _, w, h = elem.bbox
    return w * h


def _bbox_contains(elem: "ScreenElement", x: int, y: int) -> bool:
    ex, ey, ew, eh = elem.bbox
    return ex <= x < ex + ew and ey <= y < ey + eh


def _centroid_dist(elem: "ScreenElement", x: int, y: int) -> float:
    ex, ey, ew, eh = elem.bbox
    cx = ex + ew / 2.0
    cy = ey + eh / 2.0
    return math.hypot(cx - x, cy - y)


def get_element_at_cursor(
    screen_model: "ScreenModel",
    cursor_pos: Optional[tuple[int, int]],
) -> Optional["ScreenElement"]:
    """Return the most specific ScreenElement at cursor_pos.

    Algorithm
    ---------
    1. Collect all elements whose bbox strictly contains cursor_pos.
    2. Among those, return the one with the smallest bbox area (tightest
       bounding box = leaf / most specific control).
    3. If no element contains the point, return the element whose centroid
       is nearest to cursor_pos and within CURSOR_RADIUS_PX.  None if even
       that is empty.

    Parameters
    ----------
    screen_model:
        The active ScreenModel for the target window.
    cursor_pos:
        Virtual-desktop pixel coordinates from PerceptionTarget.cursor_pos.
        Returns None immediately when cursor_pos is None.
    """
    if cursor_pos is None:
        return None

    px, py = cursor_pos
    candidates = [
        e for e in screen_model.elements
        if e.bbox[2] > 0 and e.bbox[3] > 0  # non-zero bbox only
    ]

    # Step 1 + 2: containment, then smallest.
    containing = [e for e in candidates if _bbox_contains(e, px, py)]
    if containing:
        return min(containing, key=_bbox_area)

    # Step 3: nearest centroid within radius.
    radius = config.CURSOR_RADIUS_PX
    near = [
        (e, _centroid_dist(e, px, py))
        for e in candidates
    ]
    near = [(e, d) for e, d in near if d <= radius]
    if near:
        return min(near, key=lambda t: t[1])[0]

    return None


# ---------------------------------------------------------------------------
# Focused-element resolution
# ---------------------------------------------------------------------------

def _try_com_bbox(focused_ref: Any) -> Optional[tuple[int, int, int, int]]:
    """Extract (x, y, w, h) from a raw IUIAutomationElement COM pointer.

    Returns None if the pointer doesn't expose CurrentBoundingRectangle or
    the call fails.
    """
    try:
        rect = focused_ref.CurrentBoundingRectangle
        w = rect.right - rect.left
        h = rect.bottom - rect.top
        if w > 0 and h > 0:
            return rect.left, rect.top, w, h
    except Exception:
        pass
    return None


def _try_com_name(focused_ref: Any) -> str:
    """Extract CurrentName from a raw IUIAutomationElement COM pointer."""
    try:
        return (focused_ref.CurrentName or "").strip()
    except Exception:
        return ""


def _try_com_role(focused_ref: Any) -> str:
    """Extract CurrentLocalizedControlType from a raw IUIAutomationElement COM pointer."""
    try:
        return (focused_ref.CurrentLocalizedControlType or "").strip().lower()
    except Exception:
        return ""


def _com_pointer_is_alive(focused_ref: Any) -> bool:
    """Probe the COM pointer with the cheapest property read available.

    A destroyed UIA element raises COMError (HRESULT 0x80131500 /
    UIA_E_ELEMENTNOTAVAILABLE) or OSError on the first property access.
    We probe CurrentName — it is the lightest call and always implemented.
    Returns False when the pointer is provably dead; True when it is live
    or the probe is inconclusive (non-COM exceptions mean the ref is not
    a COM object at all, so treat it as dead too).
    """
    try:
        _ = focused_ref.CurrentName
        return True
    except OSError:
        # COM HRESULT errors surface as OSError in comtypes on Windows.
        return False
    except Exception:
        # Anything else (AttributeError, etc.) means it is not a valid pointer.
        return False


def get_focused_element(
    screen_model: "ScreenModel",
    focused_ref: Any,
) -> Optional["ScreenElement"]:
    """Map the wake-time focused UIA element to a ScreenElement.

    Matching tiers (first match wins):
    1. Handle identity — elem.handle is focused_ref (same COM pointer).
    2. Exact bbox match — if focused_ref.CurrentBoundingRectangle matches
       a ScreenElement's bbox exactly.
    3. Text + role fuzzy fallback — if COM name and control type are
       readable, pick the ScreenElement with the highest rapidfuzz WRatio
       against the COM name, restricted to elements whose role is compatible.

    Parameters
    ----------
    screen_model:
        The active ScreenModel for the target window.
    focused_ref:
        PerceptionTarget.focused_element — a raw comtypes IUIAutomationElement
        captured at wake time.  Returns None when focused_ref is None.
    """
    if focused_ref is None:
        return None

    # A dead COM pointer must never fuzzy-match a live element on a new page.
    # Probe cheaply before any tier runs; abort the whole function on failure.
    if not _com_pointer_is_alive(focused_ref):
        _log.debug("get_focused_element: COM pointer is stale, returning None")
        return None

    candidates = [e for e in screen_model.elements if e.bbox[2] > 0 and e.bbox[3] > 0]

    # Tier 1: handle identity.
    for elem in candidates:
        try:
            if elem.handle is not None and elem.handle is focused_ref:
                return elem
        except Exception:
            pass

    # Tier 2: exact bbox match from COM.
    com_bbox = _try_com_bbox(focused_ref)
    if com_bbox is not None:
        for elem in candidates:
            if elem.bbox == com_bbox:
                return elem

    # Tier 3: text + role fuzzy match.
    com_name = _try_com_name(focused_ref)
    com_role_lc = _try_com_role(focused_ref)

    if not com_name:
        return None

    try:
        from rapidfuzz.fuzz import WRatio
    except ImportError:
        import difflib
        def WRatio(a: str, b: str) -> float:  # type: ignore[misc]
            return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio() * 100

    best_elem: Optional["ScreenElement"] = None
    best_score = 0.0

    for elem in candidates:
        # Loose role filter: skip only if both roles are known and clearly different.
        if com_role_lc:
            elem_role_lc = elem.role.lower()
            if (
                elem_role_lc not in (com_role_lc, "unknown", "")
                and com_role_lc not in (elem_role_lc, "unknown", "")
            ):
                continue

        score = float(WRatio(com_name, elem.text))
        if score > best_score:
            best_score = score
            best_elem = elem

    # Require a minimum match quality to avoid false positives.
    _FUZZY_MIN = 60.0
    if best_score >= _FUZZY_MIN:
        return best_elem

    return None


# ---------------------------------------------------------------------------
# VLM referring-expression grounding (last resort)
# ---------------------------------------------------------------------------

def resolve_reference_vlm(
    phrase: str,
    candidates: "list[ScreenElement]",
    screen_model: "ScreenModel",
    target: "PerceptionTarget",
) -> "Optional[ScreenElement]":
    """Resolve *phrase* to a ScreenElement using VLM-based SoM grounding.

    This is the resolver of last resort — called only after
    get_element_at_cursor, get_focused_element, and
    screen_model.resolve_reference() all fail to yield a confident result.

    Algorithm (localize-then-extract)
    ----------------------------------
    1. Capture a fresh crop of the target window.
    2. Render numbered SoM markers over *candidates* (subset of screen_model
       elements, pre-filtered by the caller to the most plausible candidates).
    3. Ask the VLM "which marker matches <phrase>?" — trusting it only for
       WHICH region to focus on, never for text content.
    4. Look up the winning ScreenElement from the marker map.
    5. Re-read the element's text via read_region() (high-res OCR crop) or
       the existing UIA node — return the element with the OCR text patched in
       so callers receive precise, not hallucinated, transcription.

    Parameters
    ----------
    phrase     : The referring expression to resolve (e.g. "the error message").
    candidates : ScreenElements to mark up. Typically the top results from
                 screen_model.resolve_reference(), or all elements if the
                 linguistic resolver produced nothing.  Must be non-empty.
    screen_model : Active ScreenModel; used for origin when rendering markers.
    target     : PerceptionTarget for fresh capture.

    Returns
    -------
    ScreenElement whose .text has been refreshed via OCR, or None if the VLM
    returned no usable answer or the capture failed.
    """
    if not candidates:
        return None

    # Step 1: fresh capture.
    try:
        from capture import capture_target
        crop, origin, _dpi, stale = capture_target(target)
        if stale:
            _log.debug("resolve_reference_vlm: capture stale, aborting")
            return None
    except Exception as exc:
        _log.debug("resolve_reference_vlm: capture failed: %s", exc)
        return None

    # Step 2: render SoM markers over candidates.
    try:
        from set_of_marks import render_som, ask_som_marker
        annotated, markers = render_som(crop, candidates, origin)
    except Exception as exc:
        _log.debug("resolve_reference_vlm: render_som failed: %s", exc)
        return None

    if not markers:
        _log.debug("resolve_reference_vlm: no markers drawn (all candidates out of crop)")
        return None

    # Step 3: ask VLM which marker number matches the phrase.
    try:
        marker_num = ask_som_marker(annotated, phrase, n_markers=len(markers))
    except Exception as exc:
        _log.debug("resolve_reference_vlm: ask_som_marker failed: %s", exc)
        return None

    if marker_num is None:
        _log.debug("resolve_reference_vlm: VLM returned no marker for %r", phrase)
        return None

    elem = markers.get(marker_num)
    if elem is None:
        _log.debug("resolve_reference_vlm: marker %d not in map", marker_num)
        return None

    _log.debug("resolve_reference_vlm: VLM chose marker %d → elem %r", marker_num, elem.id)

    # Step 4+5: localize-then-extract — refresh element text via high-res OCR,
    # never using the VLM's own transcription.
    elem = _refresh_element_text(elem, crop, origin)
    return elem


def _refresh_element_text(
    elem: "ScreenElement",
    crop: "Any",
    origin: tuple[int, int],
) -> "ScreenElement":
    """Return elem with .text refreshed via read_region() OCR on its bbox.

    Falls back to elem unchanged on any failure (UIA source elements already
    have authoritative text; OCR is only strictly needed for cv/vision sources).
    """
    # UIA-sourced elements already have accurate text from accessibility tree.
    if elem.source == "uia" and elem.text:
        return elem

    try:
        from adapters.ocr_adapter import read_region

        bx, by, bw, bh = elem.bbox
        ox, oy = origin

        # Crop to the element's region from the already-captured frame.
        # read_region() accepts a full-frame + bbox; pass the full crop with
        # the bbox translated back to full-frame coordinates so read_region
        # clips correctly.  The crop IS the full frame here (capture_target
        # already crops to the window); we need a frame that covers the bbox.
        # Reconstruct a frame-relative bbox using the crop origin.
        bx_local = bx - ox
        by_local = by - oy
        if bx_local < 0 or by_local < 0 or bw <= 0 or bh <= 0:
            return elem

        region = crop[
            max(0, by_local): by_local + bh,
            max(0, bx_local): bx_local + bw,
        ]
        if region.size == 0:
            return elem

        # read_region wants virtual-desktop bbox + a frame large enough to
        # contain it.  Build a minimal 1:1 frame from the region so bboxes
        # line up: the "frame" IS the region, and bbox is (0, 0, bw, bh).
        result = read_region((0, 0, bw, bh), frame=region)
        ocr_text = (result.get("text") or "").strip()
        if ocr_text:
            import dataclasses
            return dataclasses.replace(elem, text=ocr_text)
    except Exception as exc:
        _log.debug("_refresh_element_text OCR failed for elem %r: %s", elem.id, exc)

    return elem
