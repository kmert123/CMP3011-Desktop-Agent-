"""ScreenElement, ScreenModel dataclasses; dhash and hamming utilities."""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Optional

import cv2
import numpy as np

if TYPE_CHECKING:
    from perception_target import PerceptionTarget


_COLOR_PALETTE: list[tuple[str, tuple[int, int, int]]] = [
    ("black",  (0,   0,   0)),
    ("white",  (255, 255, 255)),
    ("gray",   (128, 128, 128)),
    ("red",    (220, 30,  30)),
    ("orange", (230, 120, 0)),
    ("yellow", (220, 210, 0)),
    ("green",  (30,  160, 30)),
    ("teal",   (0,   160, 140)),
    ("blue",   (30,  80,  210)),
    ("purple", (130, 30,  200)),
    ("pink",   (220, 80,  160)),
    ("brown",  (130, 70,  30)),
]


def nearest_color_name(rgb: tuple[int, int, int]) -> str:
    """Map an (R, G, B) tuple to the nearest human color name by Euclidean distance."""
    r, g, b = rgb
    best_name = "gray"
    best_dist = 10 ** 9
    for name, (pr, pg, pb) in _COLOR_PALETTE:
        d = (r - pr) ** 2 + (g - pg) ** 2 + (b - pb) ** 2
        if d < best_dist:
            best_dist = d
            best_name = name
    return best_name


@dataclass
class ScreenElement:
    id: str
    role: str                           # UIA control_type or "text_block" / "region"
    text: str
    bbox: tuple[int, int, int, int]     # (x, y, w, h) virtual-desktop pixels
    source: str                         # "uia" | "ocr" | "cv" | "vision"
    confidence: float                   # raw adapter confidence, 0.0–1.0
    invokable: bool
    handle: Any | None = field(default=None, repr=False)
    calibrated_confidence: float = field(default=0.0)  # reliability-weighted confidence
    source_ts: float = field(default=0.0)              # time.monotonic() when adapter produced this element
    # Containment tree fields — populated by fusion.build_tree()
    parent_id: str | None = field(default=None, repr=False)
    children_ids: list[str] = field(default_factory=list, repr=False)
    tree_key: str = field(default="", repr=False)  # role + normalised-text + relative-pos hash
    # Salience: True when element centre falls inside the resolved content region (P8).
    # Default True so native-app and fixture elements are unaffected.
    in_content_region: bool = field(default=True, repr=False)
    # Appearance — sampled by fusion from the element crop; all optional.
    dominant_color: tuple[int, int, int] | None = field(default=None, repr=False)  # (R,G,B) 0-255
    color_name: str = field(default="", repr=False)    # nearest palette name, e.g. "red"
    shape_hint: str = field(default="", repr=False)    # "" | "rect" | "round" | "icon" | "line"


@dataclass
class ReferenceMatch:
    """A candidate element and its composite match score from resolve_reference()."""
    element: "ScreenElement"
    score: float   # 0.0–100.0; higher is better


@dataclass
class ScreenModel:
    target: "PerceptionTarget"
    elements: list[ScreenElement]
    full_text: str                      # all element texts, newline-separated, top-to-bottom
    captured_at: float                  # time.monotonic() when the pixel grab occurred
    screen_hash: str                    # dhash hex of active-window crop
    stale: bool = field(default=False)  # True if the target window was gone/minimized at perception time
    capture_ts: float = field(default=0.0)  # time.monotonic() alias exposed for callers that need it explicitly
    # Root-level element IDs (those with no parent), in reading order
    root_ids: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._by_id: dict[str, ScreenElement] = {e.id: e for e in self.elements}
        if not self.root_ids:
            self.root_ids = [e.id for e in self.elements if e.parent_id is None]

    def age_ms(self) -> float:
        """Milliseconds elapsed since the pixel grab."""
        return (time.monotonic() - self.captured_at) * 1000.0

    def _descendants(self, elem_id: str) -> list[ScreenElement]:
        """Return all elements in the subtree rooted at elem_id (BFS, elem included)."""
        result: list[ScreenElement] = []
        queue = [elem_id]
        while queue:
            eid = queue.pop(0)
            node = self._by_id.get(eid)
            if node is None:
                continue
            result.append(node)
            queue.extend(node.children_ids)
        return result

    def find(
        self,
        role: Optional[str] = None,
        text_contains: Optional[str] = None,
        invokable: Optional[bool] = None,
        within: Optional["ScreenElement"] = None,
    ) -> list[ScreenElement]:
        """Filter elements with optional ancestor-scoped search.

        within — if given, restrict the search to the subtree rooted at that element.
        """
        if within is not None:
            candidates = self._descendants(within.id)
        else:
            candidates = self.elements

        if role is not None:
            candidates = [e for e in candidates if e.role == role]
        if text_contains is not None:
            needle = text_contains.lower()
            candidates = [e for e in candidates if needle in e.text.lower()]
        if invokable is not None:
            candidates = [e for e in candidates if e.invokable == invokable]
        return sorted(candidates, key=lambda e: e.calibrated_confidence, reverse=True)

    def resolve_reference(self, phrase: str) -> list["ReferenceMatch"]:
        """Resolve a natural-language phrase to ranked ScreenElement candidates.

        Pipeline
        --------
        1. Strip role keywords to isolate the core text query.
        2. Parse a spatial predicate (zone or relational) from the phrase.
        3. Score every non-zero-bbox element by fuzzy text similarity (WRatio).
        4. Multiply by a spatial compatibility score (0.0–1.0).
        5. Return candidates with score > 0, sorted descending.

        The caller (orchestrator) decides whether the top result is confident
        enough or whether to surface runners-up for disambiguation.
        """
        return _resolve_reference(self, phrase)

    def to_prompt_block(self, max_tokens: int = 400) -> str:
        """Render the containment tree as indented text (DFS), truncated to ~max_tokens.

        Content-region elements are rendered first (P8 salience).  Browser-chrome
        elements (in_content_region=False) follow in a labelled, budget-capped section
        capped at ~15% of the total budget.  Content is never truncated to make room
        for chrome.

        Siblings are ordered top-to-bottom then left-to-right so that spatially
        separate columns (sidebar vs body) stay grouped under their respective
        parent containers rather than interleaved by global y-coordinate.
        """
        total_budget = max_tokens * 4
        # Chrome section is capped at 15% of the total budget.
        chrome_budget = int(total_budget * 0.15)
        content_budget = total_budget - chrome_budget

        def _sorted_children(node: "ScreenElement") -> list[str]:
            children = [
                self._by_id[cid]
                for cid in node.children_ids
                if cid in self._by_id
            ]
            children.sort(key=lambda e: (e.bbox[1], e.bbox[0]))
            return [c.id for c in children]

        # Check whether any element has in_content_region=False; if not, skip the
        # two-section split entirely (native apps, full-window = content).
        has_chrome_elems = any(not e.in_content_region for e in self.elements)

        lines: list[str] = []
        used = 0

        def _render(elem_id: str, depth: int, budget: int) -> bool:
            nonlocal used
            node = self._by_id.get(elem_id)
            if node is None:
                return True
            indent = "  " * depth
            x, y, w, h = node.bbox
            inv = " [invokable]" if node.invokable else ""
            # Append appearance tag only for text-bearing or invokable leaf elements.
            appearance = ""
            if (node.text or node.invokable) and (node.color_name or node.shape_hint):
                parts = [p for p in (node.color_name, node.shape_hint) if p]
                appearance = f" ({', '.join(parts)})"
            line = f"{indent}[{node.role}] {node.text}{appearance} @ ({x},{y},{w},{h}){inv}"
            cost = len(line) + 1
            if used + cost > budget:
                return False  # budget exhausted
            lines.append(line)
            used += cost
            for child_id in _sorted_children(node):
                if not _render(child_id, depth + 1, budget):
                    return False
            return True

        if not has_chrome_elems:
            # Single section: all roots in reading order (unchanged behaviour).
            for root_id in self.root_ids:
                if not _render(root_id, 0, total_budget):
                    break
        else:
            # Two-section render: content first, then chrome.
            content_roots = sorted(
                [
                    rid for rid in self.root_ids
                    if self._by_id.get(rid) and self._by_id[rid].in_content_region
                ],
                key=lambda rid: getattr(self._by_id[rid], "calibrated_confidence", 0.0),
                reverse=True,
            )
            chrome_roots = [
                rid for rid in self.root_ids
                if self._by_id.get(rid) and not self._by_id[rid].in_content_region
            ]

            for root_id in content_roots:
                if not _render(root_id, 0, content_budget):
                    break

            if chrome_roots:
                chrome_header = "Browser chrome (low priority):"
                header_cost = len(chrome_header) + 1
                if used + header_cost <= total_budget:
                    lines.append(chrome_header)
                    used += header_cost
                    chrome_limit = used + chrome_budget
                    for root_id in chrome_roots:
                        if not _render(root_id, 0, chrome_limit):
                            break

        return "\n".join(lines)

    def to_full_text_block(self, max_chars: int = 4000) -> str:
        """Return ALL visible screen text as clean readable lines — no geometry.

        Unlike to_prompt_block(), this carries no roles, bboxes, or [invokable]
        decorations and is not capped at the tight tree budget.  It is the
        verbatim text of the screen in reading order, intended to be injected
        into the reasoning prompt so the model can answer "what does the text on
        my screen say?" without parsing the structured tree.

        Ordering: content-region elements first (P8 salience), then the rest, so
        browser/app chrome never buries the body text.  Within each section,
        elements are sorted top-to-bottom then left-to-right.  Only text-bearing
        UIA/OCR elements contribute (same source filter as full_text).  Exact
        consecutive duplicate lines are collapsed.  Truncated to max_chars.
        """
        text_elems = [
            e for e in self.elements
            if e.text and e.text.strip() and e.source in ("uia", "ocr")
        ]
        if not text_elems:
            # Fall back to the precomputed full_text when no elements qualify.
            full = (self.full_text or "").strip()
            return full[:max_chars]

        def _reading_order(elems: list["ScreenElement"]) -> list["ScreenElement"]:
            return sorted(elems, key=lambda e: (e.bbox[1], e.bbox[0]))

        content = _reading_order([e for e in text_elems if e.in_content_region])
        chrome = _reading_order([e for e in text_elems if not e.in_content_region])

        lines: list[str] = []
        prev: str | None = None
        for elem in content + chrome:
            line = elem.text.strip()
            if line and line != prev:  # collapse exact consecutive duplicates
                lines.append(line)
                prev = line

        block = "\n".join(lines)
        if len(block) > max_chars:
            block = block[:max_chars].rstrip() + "…"
        return block


def make_element_id(role: str, text: str, bbox: tuple[int, int, int, int]) -> str:
    """Stable 16-char hex ID derived from (role, text, bbox)."""
    raw = f"{role}|{text}|{bbox[0]},{bbox[1]},{bbox[2]},{bbox[3]}"
    return hashlib.md5(raw.encode()).hexdigest()[:16]


# Roles that are structurally transparent (containers only, not leaf nodes of interest).
_CONTAINER_ROLES = {
    "Pane", "Group", "Window", "Document", "ScrollViewer",
    "Custom", "Panel", "Frame", "Form",
}

# Cross-source merge: roles considered compatible for deduplication.
# Mapped to a canonical category; two elements are role-compatible if they share a category.
_ROLE_CATEGORY: dict[str, str] = {
    "Button": "button", "MenuItem": "button", "ListItem": "button",
    "CheckBox": "toggle", "RadioButton": "toggle", "ToggleButton": "toggle",
    "Edit": "input", "ComboBox": "input",
    "Text": "text", "Hyperlink": "text", "Label": "text",
    "text_block": "text",
    "region": "layout",
    "ToolBar": "layout", "StatusBar": "layout",
}


def _role_compatible(role_a: str, role_b: str) -> bool:
    """True when two roles belong to the same category, or either is uncategorised."""
    cat_a = _ROLE_CATEGORY.get(role_a)
    cat_b = _ROLE_CATEGORY.get(role_b)
    if cat_a is None or cat_b is None:
        return True  # unknown roles don't block a merge
    return cat_a == cat_b


def _normalise_text(t: str) -> str:
    """Collapse whitespace, lowercase, strip punctuation for tree_key."""
    return re.sub(r"[^a-z0-9 ]", "", t.lower().strip())


def make_tree_key(
    role: str,
    text: str,
    bbox: tuple[int, int, int, int],
    parent_bbox: tuple[int, int, int, int] | None,
) -> str:
    """Stable key: role + normalised text + relative-position bucket (8px grid).

    Using relative position (offset from parent or absolute if no parent) makes the
    key stable across small window moves while still distinguishing sibling nodes at
    different positions.
    """
    x, y, w, h = bbox
    if parent_bbox is not None:
        px, py = parent_bbox[0], parent_bbox[1]
        rel_x, rel_y = (x - px) // 8, (y - py) // 8
    else:
        rel_x, rel_y = x // 8, y // 8
    norm = _normalise_text(text)
    raw = f"{role}|{norm}|{rel_x},{rel_y}"
    return hashlib.md5(raw.encode()).hexdigest()[:12]


def dhash(bgr_crop: np.ndarray, size: int = 8) -> str:
    """Difference hash of a BGR image; returns a hex string."""
    gray = cv2.cvtColor(bgr_crop, cv2.COLOR_BGR2GRAY)
    resized = cv2.resize(gray, (size + 1, size))
    diff = resized[:, 1:] > resized[:, :-1]
    return format(int(np.packbits(diff.flatten()).tobytes().hex(), 16), "x")


# Volatile sub-regions that are excluded from the cache hash to avoid false misses.
# Each entry is (y_frac_start, y_frac_end, x_frac_start, x_frac_end) in [0,1] relative coords.
# Currently: bottom status-bar strip (clock/tray area) and top ~3px (window drag border flicker).
_VOLATILE_MASKS: list[tuple[float, float, float, float]] = [
    (0.0,  0.03,  0.0, 1.0),   # top border flicker
    (0.92, 1.0,   0.7, 1.0),   # bottom-right corner (clock / system tray)
]


def _mask_volatile(gray: np.ndarray) -> np.ndarray:
    """Zero out known volatile sub-regions in a grayscale crop copy."""
    out = gray.copy()
    h, w = out.shape[:2]
    for y0f, y1f, x0f, x1f in _VOLATILE_MASKS:
        y0, y1 = int(y0f * h), int(y1f * h)
        x0, x1 = int(x0f * w), int(x1f * w)
        out[y0:y1, x0:x1] = 0
    return out


def roi_dhash(bgr_crop: np.ndarray, size: int = 16) -> str:
    """16×16 dHash of the target-region crop, with volatile sub-regions masked.

    Used as the primary cache key component. Larger size (256 bits) gives finer
    sensitivity to single-char edits and toggle state changes.
    """
    gray = cv2.cvtColor(bgr_crop, cv2.COLOR_BGR2GRAY)
    masked = _mask_volatile(gray)
    resized = cv2.resize(masked, (size + 1, size))
    diff = resized[:, 1:] > resized[:, :-1]
    return format(int(np.packbits(diff.flatten()).tobytes().hex(), 16), "x")


def density_delta(a: np.ndarray, b: np.ndarray) -> float:
    """Cheap text/edge-density difference between two BGR crops.

    Computes Canny edge pixel fraction for each crop and returns |fa - fb|.
    Values < CACHE_DENSITY_DELTA_MAX indicate the ROI is visually stable.
    Falls back to 0.0 on error (treat as stable; let Hamming decide).
    """
    try:
        def _density(bgr: np.ndarray) -> float:
            gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
            edges = cv2.Canny(gray, threshold1=40, threshold2=120)
            return float(np.count_nonzero(edges)) / max(1, edges.size)

        return abs(_density(a) - _density(b))
    except Exception:
        return 0.0


def hamming(a_hex: str, b_hex: str) -> int:
    """Bit-level Hamming distance between two dhash hex strings."""
    return bin(int(a_hex, 16) ^ int(b_hex, 16)).count("1")


# ---------------------------------------------------------------------------
# resolve_reference implementation
# ---------------------------------------------------------------------------

# Role keywords that appear in natural-language references but should be
# stripped before fuzzy-matching element text.
_ROLE_WORDS: dict[str, str] = {
    "button":    "Button",
    "btn":       "Button",
    "checkbox":  "CheckBox",
    "check box": "CheckBox",
    "check":     "CheckBox",
    "radio":     "RadioButton",
    "link":      "Hyperlink",
    "hyperlink": "Hyperlink",
    "text":      "Text",
    "label":     "Text",
    "input":     "Edit",
    "field":     "Edit",
    "textbox":   "Edit",
    "text box":  "Edit",
    "edit":      "Edit",
    "combo":     "ComboBox",
    "dropdown":  "ComboBox",
    "drop-down": "ComboBox",
    "menu":      "MenuItem",
    "item":      "ListItem",
    "tab":       "TabItem",
    "image":     "Image",
    "icon":      "Image",
}

# Spatial zone patterns: map phrase fragments to (y_zone, x_zone) where each
# zone is -1 (low/left), 0 (center), or 1 (high/right).
# Checked in order; first match wins.
_ZONE_PATTERNS: list[tuple[re.Pattern[str], int, int]] = [
    (re.compile(r"\btop[- ]left\b"),     -1, -1),
    (re.compile(r"\btop[- ]right\b"),    -1,  1),
    (re.compile(r"\bbottom[- ]left\b"),   1, -1),
    (re.compile(r"\bbottom[- ]right\b"),  1,  1),
    (re.compile(r"\btop\b"),             -1,  0),
    (re.compile(r"\bbottom\b"),           1,  0),
    (re.compile(r"\bleft\b"),             0, -1),
    (re.compile(r"\bright\b"),            0,  1),
    (re.compile(r"\bcenter\b"),           0,  0),
    (re.compile(r"\bcentre\b"),           0,  0),
    (re.compile(r"\bmiddle\b"),           0,  0),
]

# Relational predicates: (pattern, direction_tag).
# Capture group 1 = anchor text.
_REL_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bbelow\s+(.+)$"),       "below"),
    (re.compile(r"\bunder\s+(.+)$"),       "below"),
    (re.compile(r"\babove\s+(.+)$"),       "above"),
    (re.compile(r"\bover\s+(.+)$"),        "above"),
    (re.compile(r"\bnext\s+to\s+(.+)$"),   "beside"),
    (re.compile(r"\bbeside\s+(.+)$"),      "beside"),
    (re.compile(r"\bleft\s+of\s+(.+)$"),   "left_of"),
    (re.compile(r"\bright\s+of\s+(.+)$"),  "right_of"),
]


def _fuzz(a: str, b: str) -> float:
    try:
        from rapidfuzz.fuzz import WRatio
        return float(WRatio(a, b))
    except ImportError:
        import difflib
        return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio() * 100.0


def _centroid(elem: "ScreenElement") -> tuple[float, float]:
    x, y, w, h = elem.bbox
    return x + w / 2.0, y + h / 2.0


def _zone_score(elem: "ScreenElement", y_zone: int, x_zone: int,
                all_elems: "list[ScreenElement]") -> float:
    """Score 1.0 when the element's centroid is in the expected zone, tapering to 0.5."""
    if not all_elems:
        return 1.0
    xs = [e.bbox[0] + e.bbox[2] / 2.0 for e in all_elems if e.bbox[2] > 0]
    ys = [e.bbox[1] + e.bbox[3] / 2.0 for e in all_elems if e.bbox[3] > 0]
    if not xs or not ys:
        return 1.0
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    cx, cy = _centroid(elem)

    def _frac(v: float, lo: float, hi: float) -> float:
        if hi == lo:
            return 0.5
        return (v - lo) / (hi - lo)  # 0.0 = leftmost/topmost, 1.0 = rightmost/bottommost

    x_frac = _frac(cx, min_x, max_x)
    y_frac = _frac(cy, min_y, max_y)

    # x_zone: -1=left (frac→0), 0=center (frac→0.5), 1=right (frac→1)
    x_target = {-1: 0.0, 0: 0.5, 1: 1.0}[x_zone]
    y_target = {-1: 0.0, 0: 0.5, 1: 1.0}[y_zone]

    x_match = 1.0 - abs(x_frac - x_target)  # 0.0–1.0
    y_match = 1.0 - abs(y_frac - y_target)

    # Both axes must agree; weight y slightly higher (reading order).
    combined = 0.4 * x_match + 0.6 * y_match
    # Map to [0.5, 1.0] so even a poor zone match still contributes.
    return 0.5 + 0.5 * combined


def _rel_score(elem: "ScreenElement", direction: str,
               anchor: "ScreenElement") -> float:
    """Score how well elem satisfies the directional relation to anchor.

    Returns 1.0 for perfect placement, tapering toward 0.0.
    """
    import math
    ax, ay = _centroid(anchor)
    ex, ey = _centroid(elem)
    dx, dy = ex - ax, ey - ay
    dist = math.hypot(dx, dy)
    if dist == 0:
        return 0.0  # same element

    if direction == "below":
        # dy > 0 is below; score peaks when dx≈0
        if dy <= 0:
            return 0.0
        alignment = 1.0 - min(1.0, abs(dx) / max(1.0, abs(dy)))
        proximity = 1.0 / (1.0 + dist / 200.0)
        return alignment * proximity

    if direction == "above":
        if dy >= 0:
            return 0.0
        alignment = 1.0 - min(1.0, abs(dx) / max(1.0, abs(dy)))
        proximity = 1.0 / (1.0 + dist / 200.0)
        return alignment * proximity

    if direction == "left_of":
        if dx >= 0:
            return 0.0
        alignment = 1.0 - min(1.0, abs(dy) / max(1.0, abs(dx)))
        proximity = 1.0 / (1.0 + dist / 200.0)
        return alignment * proximity

    if direction == "right_of":
        if dx <= 0:
            return 0.0
        alignment = 1.0 - min(1.0, abs(dy) / max(1.0, abs(dx)))
        proximity = 1.0 / (1.0 + dist / 200.0)
        return alignment * proximity

    if direction == "beside":
        # Either side; weight by horizontal proximity vs vertical deviation.
        if abs(dx) < 1:
            return 0.0
        alignment = 1.0 - min(1.0, abs(dy) / max(1.0, abs(dx)))
        proximity = 1.0 / (1.0 + dist / 200.0)
        return alignment * proximity

    return 1.0


def _resolve_reference(model: "ScreenModel", phrase: str) -> "list[ReferenceMatch]":
    phrase_lc = phrase.lower().strip()

    # --- Step 1: extract role hint ---
    role_hint: str | None = None
    stripped = phrase_lc
    for kw, role in sorted(_ROLE_WORDS.items(), key=lambda t: -len(t[0])):
        if kw in stripped:
            role_hint = role
            stripped = stripped.replace(kw, " ").strip()
            break

    # --- Step 2: parse spatial predicate ---
    # Check relational before zone (relational patterns include directional words
    # that would also match zone patterns).
    rel_direction: str | None = None
    rel_anchor_text: str | None = None
    zone_y: int | None = None
    zone_x: int | None = None

    for pattern, direction in _REL_PATTERNS:
        m = pattern.search(stripped)
        if m:
            rel_direction = direction
            rel_anchor_text = m.group(1).strip()
            stripped = stripped[:m.start()].strip()
            break

    if rel_direction is None:
        for pattern, yz, xz in _ZONE_PATTERNS:
            if pattern.search(stripped):
                zone_y, zone_x = yz, xz
                stripped = pattern.sub("", stripped).strip()
                break

    # Core text query after stripping role and spatial words.
    # Remove filler words (articles, prepositions) that carry no semantic content
    # for element matching.  Using word-boundary patterns avoids clobbering
    # partial words (e.g. "at" inside "that").
    core_text = re.sub(r"\s+", " ", stripped).strip()
    _FILLER = re.compile(r"\b(the|a|an|at|in|on|of|for|with)\b", re.IGNORECASE)
    core_text = _FILLER.sub("", core_text).strip()
    core_text = re.sub(r"\s+", " ", core_text).strip()

    # --- Step 3: candidate pool ---
    pool = [e for e in model.elements if e.bbox[2] > 0 and e.bbox[3] > 0]
    if role_hint is not None:
        # Prefer role-matching elements but don't exclude entirely — some elements
        # have generic roles like "Unknown".
        role_pool = [e for e in pool if e.role == role_hint]
        if role_pool:
            pool = role_pool

    # --- Step 4: resolve relational anchor ---
    anchor_elem: "ScreenElement | None" = None
    if rel_direction is not None and rel_anchor_text:
        anchor_candidates = sorted(
            pool,
            key=lambda e: _fuzz(rel_anchor_text, e.text),
            reverse=True,
        )
        if anchor_candidates and _fuzz(rel_anchor_text, anchor_candidates[0].text) >= 40.0:
            anchor_elem = anchor_candidates[0]

    # --- Step 5: score each candidate ---
    _FUZZY_MIN = 10.0  # low floor; spatial score can carry weak text matches
    results: list[ReferenceMatch] = []

    for elem in pool:
        # Text similarity (0–100).
        if core_text:
            text_score = _fuzz(core_text, elem.text)
        else:
            # No text component (e.g. "the button at the top right") — use role
            # match as a weak baseline so spatial can still pick a winner.
            text_score = 60.0 if (role_hint and elem.role == role_hint) else 30.0

        if text_score < _FUZZY_MIN:
            continue

        # Spatial multiplier.
        spatial_mult = 1.0
        if anchor_elem is not None:
            if anchor_elem is elem:
                continue  # the anchor itself cannot satisfy "below/above/next to anchor"
            r = _rel_score(elem, rel_direction, anchor_elem)  # type: ignore[arg-type]
            spatial_mult = 0.3 + 0.7 * r  # floor at 0.3 so direction=None doesn't zero-out
        elif zone_y is not None and zone_x is not None:
            spatial_mult = _zone_score(elem, zone_y, zone_x, pool)

        combined = text_score * spatial_mult
        if combined > 0:
            results.append(ReferenceMatch(element=elem, score=combined))

    results.sort(key=lambda r: r.score, reverse=True)
    return results
