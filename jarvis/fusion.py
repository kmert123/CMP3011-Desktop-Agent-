"""Fusion: merge UIA, OCR, CV adapter outputs into one authoritative ScreenModel.

Three-step algorithm:
  1. Build containment tree from UIA elements (parent = smallest UIA ancestor).
  2. Place OCR/CV evidence into its smallest containing UIA node; orphans become
     root-level elements with CV-inherited role where available.
  3. Cross-source dedup: merge only when spatial overlap AND text-similarity
     (rapidfuzz) AND role-compatibility all hold.
"""

from __future__ import annotations

import logging
import time
from dataclasses import replace
from typing import TYPE_CHECKING

import numpy as np

import config
from content_region import resolve_content_region, tag_elements_with_region
from screen_model import (
    ScreenElement,
    ScreenModel,
    _role_compatible,
    dhash,
    make_tree_key,
    nearest_color_name,
)

if TYPE_CHECKING:
    from perception_target import PerceptionTarget

_log = logging.getLogger(__name__)

# Minimum rapidfuzz token_sort_ratio (0–100) for a cross-source text match.
_TEXT_SIM_MIN = 60
# Minimum fractional overlap (intersection / smaller-area) for "spatial overlap".
_OVERLAP_MIN = 0.4


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------

def _iou(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> float:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    ix = max(ax, bx);  iy = max(ay, by)
    ix2 = min(ax + aw, bx + bw);  iy2 = min(ay + ah, by + bh)
    if ix2 <= ix or iy2 <= iy:
        return 0.0
    inter = (ix2 - ix) * (iy2 - iy)
    union = aw * ah + bw * bh - inter
    return inter / union if union > 0 else 0.0


def _overlap_fraction(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> float:
    """Intersection / min(area_a, area_b) — more lenient than IoU for containment cases."""
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    ix = max(ax, bx);  iy = max(ay, by)
    ix2 = min(ax + aw, bx + bw);  iy2 = min(ay + ah, by + bh)
    if ix2 <= ix or iy2 <= iy:
        return 0.0
    inter = (ix2 - ix) * (iy2 - iy)
    min_area = min(aw * ah, bw * bh)
    return inter / min_area if min_area > 0 else 0.0


def _contains_center(region: tuple[int, int, int, int], bbox: tuple[int, int, int, int]) -> bool:
    rx, ry, rw, rh = region
    cx = bbox[0] + bbox[2] // 2
    cy = bbox[1] + bbox[3] // 2
    return rx <= cx < rx + rw and ry <= cy < ry + rh


def _area(bbox: tuple[int, int, int, int]) -> int:
    return bbox[2] * bbox[3]


# ---------------------------------------------------------------------------
# Step 1: Build containment tree from UIA elements
# ---------------------------------------------------------------------------

def _build_uia_tree(uia: list[ScreenElement]) -> list[ScreenElement]:
    """Assign parent_id / children_ids / tree_key to each UIA element.

    Parent = the UIA element with the smallest area whose bbox contains the
    centre of this element (excluding self).  Produces a proper forest.
    """
    # Work with mutable dicts so we can patch fields before converting back.
    nodes: list[dict] = [
        {
            "elem": e,
            "parent_id": None,
            "children_ids": [],
        }
        for e in uia
    ]
    id_to_node: dict[str, dict] = {n["elem"].id: n for n in nodes}

    # Sort by area ascending so that when iterating we encounter smaller elements first;
    # this ensures parent assignment picks the tightest enclosing ancestor.
    by_area = sorted(nodes, key=lambda n: _area(n["elem"].bbox))

    for node in by_area:
        e = node["elem"]
        best_parent: dict | None = None
        best_area = 10 ** 9
        for candidate in by_area:
            if candidate is node:
                continue
            c = candidate["elem"]
            ca = _area(c.bbox)
            if ca <= _area(e.bbox):
                continue  # must be strictly larger
            if ca >= best_area:
                continue
            if _contains_center(c.bbox, e.bbox):
                best_parent = candidate
                best_area = ca
        if best_parent is not None:
            node["parent_id"] = best_parent["elem"].id
            best_parent["children_ids"].append(e.id)

    result: list[ScreenElement] = []
    for node in nodes:
        e = node["elem"]
        parent_bbox: tuple[int, int, int, int] | None = None
        if node["parent_id"] is not None:
            parent_node = id_to_node.get(node["parent_id"])
            if parent_node:
                parent_bbox = parent_node["elem"].bbox
        tk = make_tree_key(e.role, e.text, e.bbox, parent_bbox)
        result.append(replace(
            e,
            parent_id=node["parent_id"],
            children_ids=list(node["children_ids"]),
            tree_key=tk,
        ))
    return result


# ---------------------------------------------------------------------------
# Step 2: Place OCR / CV evidence into the UIA tree
# ---------------------------------------------------------------------------

def _attach_non_uia(
    non_uia: list[ScreenElement],
    uia_tree: list[ScreenElement],
    cv: list[ScreenElement],
) -> list[ScreenElement]:
    """Place each OCR/CV element as a child of its smallest containing UIA node.

    If no UIA node contains it, it becomes a root-level orphan with a CV-inherited role.
    Returns the non-uia elements with parent_id / tree_key updated.
    """
    uia_by_area = sorted(uia_tree, key=lambda e: _area(e.bbox))
    result: list[ScreenElement] = []

    for elem in non_uia:
        best: ScreenElement | None = None
        best_area = 10 ** 9
        for u in uia_by_area:
            if not _contains_center(u.bbox, elem.bbox):
                continue
            a = _area(u.bbox)
            if a < best_area:
                best = u
                best_area = a

        inherited_role = elem.role
        if best is None:
            # Orphan: try to inherit role from CV region whose centre contains this element.
            for cv_elem in cv:
                if _contains_center(cv_elem.bbox, elem.bbox):
                    inherited_role = cv_elem.role
                    break

        parent_id = best.id if best is not None else None
        parent_bbox = best.bbox if best is not None else None
        tk = make_tree_key(elem.role, elem.text, elem.bbox, parent_bbox)
        result.append(replace(
            elem,
            role=inherited_role if inherited_role != elem.role else elem.role,
            parent_id=parent_id,
            tree_key=tk,
        ))
    return result


# ---------------------------------------------------------------------------
# Step 3: Cross-source dedup with text-similarity + role-compatibility
# ---------------------------------------------------------------------------

def _text_sim(a: str, b: str) -> float:
    """rapidfuzz token_sort_ratio, 0–100."""
    try:
        from rapidfuzz.fuzz import token_sort_ratio
        return token_sort_ratio(a, b)
    except Exception:
        # Fallback to difflib if rapidfuzz is unavailable at runtime.
        import difflib
        return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio() * 100


def _merge_pair(primary: ScreenElement, secondary: ScreenElement) -> ScreenElement:
    """Merge secondary into primary (primary wins on role/handle/confidence).

    Text selection keeps the *longer* of the two non-empty texts so that a fuller
    OCR line is not discarded when a partial/abbreviated UIA accessibility name
    happens to win the cluster.  Grounding is unaffected — it keys on
    bbox/role/confidence, not the rendered text.
    """
    if primary.text and secondary.text:
        text = primary.text if len(primary.text) >= len(secondary.text) else secondary.text
    else:
        text = primary.text or secondary.text
    conf = max(primary.confidence, secondary.confidence)
    cal = max(primary.calibrated_confidence, secondary.calibrated_confidence)
    return replace(primary, text=text, confidence=conf, calibrated_confidence=cal)


_SOURCE_PRIORITY = {"uia": 0, "ocr": 1, "cv": 2, "vision": 3}


def _dedup_cross_source(elements: list[ScreenElement]) -> list[ScreenElement]:
    """Remove duplicates across sources using overlap + text-sim + role-compat.

    Winner is the element with the highest calibrated_confidence; source priority
    is used only as a tie-breaker.  calibrated_confidence must be filled before
    this function is called.
    """
    surviving: list[ScreenElement] = []
    absorbed: set[str] = set()

    # Pre-sort by (descending calibrated_confidence, ascending source_priority) so
    # the outer loop processes the best-calibrated element first for each cluster.
    ordered = sorted(
        elements,
        key=lambda e: (
            -e.calibrated_confidence,
            _SOURCE_PRIORITY.get(e.source, 99),
        ),
    )

    for elem in ordered:
        if elem.id in absorbed:
            continue
        merged = elem
        for other in ordered:
            if other.id == elem.id or other.id in absorbed:
                continue
            if other.source == elem.source:
                continue
            if _overlap_fraction(elem.bbox, other.bbox) < _OVERLAP_MIN:
                continue
            if not _role_compatible(elem.role, other.role):
                continue
            # At least one of them must have non-empty text for text-sim to matter.
            if elem.text or other.text:
                sim = _text_sim(elem.text, other.text)
                if sim < _TEXT_SIM_MIN:
                    continue
            absorbed.add(other.id)
            # Winner = higher calibrated_confidence; source priority breaks ties.
            elem_pri = (
                -elem.calibrated_confidence,
                _SOURCE_PRIORITY.get(elem.source, 99),
            )
            other_pri = (
                -other.calibrated_confidence,
                _SOURCE_PRIORITY.get(other.source, 99),
            )
            hi, lo = (elem, other) if elem_pri <= other_pri else (other, elem)
            merged = _merge_pair(hi, lo)
        surviving.append(merged)

    return surviving


# ---------------------------------------------------------------------------
# Calibration (unchanged from previous implementation)
# ---------------------------------------------------------------------------

def _calibrate(elem: ScreenElement, app_class_value: str | None) -> ScreenElement:
    """Return elem with calibrated_confidence filled from config.ADAPTER_RELIABILITY."""
    key = (elem.source, app_class_value)
    reliability = config.ADAPTER_RELIABILITY.get(key)
    if reliability is None:
        reliability = config.ADAPTER_RELIABILITY.get((elem.source, None), 1.0)
    cal = round(min(1.0, elem.confidence * reliability), 4)
    return replace(elem, calibrated_confidence=cal)


# ---------------------------------------------------------------------------
# Appearance sampling
# ---------------------------------------------------------------------------

def _sample_appearance(
    elements: list[ScreenElement],
    frame: np.ndarray,
    target: "PerceptionTarget",
) -> list[ScreenElement]:
    """Fill dominant_color, color_name, shape_hint on each element by sampling frame.

    frame is window-local (crop); element bboxes are virtual-desktop coords.
    Only elements with positive-area bboxes are sampled.  Each crop is cheap:
    strided median on BGR then converted to RGB.  Errors per element are silently
    swallowed so a bad crop never breaks fusion.
    """
    ox = target.bounds[0]
    oy = target.bounds[1]
    frame_h, frame_w = frame.shape[:2]
    result: list[ScreenElement] = []

    for elem in elements:
        bx, by, bw, bh = elem.bbox
        if bw <= 0 or bh <= 0:
            result.append(elem)
            continue
        try:
            # Convert virtual-desktop bbox to frame-local coords.
            lx = bx - ox
            ly = by - oy
            lx2 = lx + bw
            ly2 = ly + bh
            # Skip if entirely outside the frame.
            if lx2 <= 0 or ly2 <= 0 or lx >= frame_w or ly >= frame_h:
                result.append(elem)
                continue
            # Clamp to frame boundaries.
            lx = max(0, lx)
            ly = max(0, ly)
            lx2 = min(frame_w, lx2)
            ly2 = min(frame_h, ly2)
            if lx2 <= lx or ly2 <= ly:
                result.append(elem)
                continue

            crop = frame[ly:ly2, lx:lx2]  # BGR, window-local slice

            # Subsample for speed: take every 4th pixel along each axis.
            sampled = crop[::4, ::4]
            if sampled.size == 0:
                sampled = crop

            # Median per channel (B, G, R).
            b_med = int(np.median(sampled[:, :, 0]))
            g_med = int(np.median(sampled[:, :, 1]))
            r_med = int(np.median(sampled[:, :, 2]))
            dom_rgb = (r_med, g_med, b_med)

            # Shape hint from bbox geometry.
            aspect = bw / max(1, bh)
            if bh <= 4 or bw <= 4:
                hint = "line"
            elif 0.7 <= aspect <= 1.4 and max(bw, bh) <= 48:
                hint = "icon"
            else:
                hint = "rect"

            result.append(replace(
                elem,
                dominant_color=dom_rgb,
                color_name=nearest_color_name(dom_rgb),
                shape_hint=hint,
            ))
        except Exception:
            result.append(elem)

    return result


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def fuse(
    target: "PerceptionTarget",
    uia: list[ScreenElement],
    ocr: list[ScreenElement],
    cv: list[ScreenElement],
    frame: np.ndarray,
    stale: bool = False,
) -> ScreenModel:
    """Merge adapter outputs into one authoritative ScreenModel.

    Priority order: UIA > OCR > CV.
    stale=True means the target window was absent/minimized at capture time.
    """
    # --- Step 1: UIA containment tree ---
    uia_tree = _build_uia_tree(list(uia))

    # --- Step 2: Attach OCR and CV into the tree; pure CV anchors become roots ---
    non_uia_text = list(ocr)  # OCR elements
    # CV elements that have no UIA or OCR overlapping them become layout anchors.
    text_elements_bboxes = [e.bbox for e in uia_tree] + [e.bbox for e in ocr]
    anchor_cv = [
        cv_e for cv_e in cv
        if not any(
            _overlap_fraction(cv_e.bbox, tb) >= _OVERLAP_MIN
            for tb in text_elements_bboxes
        )
    ]

    attached_non_uia = _attach_non_uia(non_uia_text + anchor_cv, uia_tree, cv)

    # --- Calibrate confidence BEFORE dedup so the winner selection uses real scores ---
    app_class = getattr(target, "app_class", None)
    app_class_value: str | None = app_class.value if app_class is not None else None
    ts = time.monotonic()

    all_elements: list[ScreenElement] = [
        _calibrate(replace(e, source_ts=e.source_ts or ts), app_class_value)
        for e in uia_tree + attached_non_uia
    ]

    # --- Step 3: Cross-source dedup (uses calibrated_confidence for winner selection) ---
    all_elements = _dedup_cross_source(all_elements)

    # --- Rebuild children_ids after dedup (some elements may have been absorbed) ---
    surviving_ids: set[str] = {e.id for e in all_elements}
    all_elements = [
        replace(e, children_ids=[c for c in e.children_ids if c in surviving_ids])
        for e in all_elements
    ]

    # --- Tag in_content_region (P8 salience) ---
    # resolve_content_region derives the content bbox from target.bounds; the origin
    # arg is only used in the degenerate frame-only fallback path.
    content_bbox = resolve_content_region(target, frame, all_elements, (0, 0))
    all_elements = tag_elements_with_region(all_elements, content_bbox)

    # --- Sample appearance (dominant color + shape hint) from element crops ---
    # frame is in window-local coords; element bboxes are virtual-desktop coords.
    # Subtract the window origin so we can index into frame.
    all_elements = _sample_appearance(all_elements, frame, target)

    # --- Assemble full_text (reading order, text-bearing elements only) ---
    text_bearing = sorted(
        (e for e in all_elements if e.text and e.source in ("uia", "ocr")),
        key=lambda e: (e.bbox[1], e.bbox[0]),
    )
    full_text = "\n".join(e.text for e in text_bearing)

    # --- Root IDs (reading order) ---
    root_ids = [
        e.id for e in sorted(
            (e for e in all_elements if e.parent_id is None),
            key=lambda e: (e.bbox[1], e.bbox[0]),
        )
    ]

    _log.debug(
        "fuse: target=%s app_class=%s elements=%d roots=%d stale=%s",
        target.process, app_class_value, len(all_elements), len(root_ids), stale,
    )

    captured_at = ts
    return ScreenModel(
        target=target,
        elements=all_elements,
        full_text=full_text,
        captured_at=captured_at,
        screen_hash=dhash(frame),
        stale=stale,
        capture_ts=captured_at,
        root_ids=root_ids,
    )
