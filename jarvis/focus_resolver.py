"""Focus resolution ladder — Task 18.

resolve_focus(query, classify_result, screen_model, target) -> FocusResult

Runs a four-rung ladder stopping at the first confident hit, then packages
the result as FocusResult so callers (gemini.py prompt builders) can inject
it as the PRIMARY context for the model.

Ladder order
------------
1. Selection  — UIA TextPattern / clipboard (Task 13).
   Triggered when the classified phrase is selection-type.
2. Cursor / focused element  — wake-time snapshot (Task 14).
   Triggered for deictic "this" / "this field" phrases.
3. Linguistic + spatial match  — screen_model.resolve_reference() (Task 15).
   For descriptive references ("the Submit button", "the button at the top right").
4. VLM SoM grounding  — render_som + ask_som_marker (Task 16).
   Last resort; only fires when rungs 1–3 all fail.

Ambiguity handling
------------------
If the best linguistic candidates have scores within AMBIGUITY_MARGIN of each
other (and neither is clearly dominant), the result is marked ambiguous.
Callers should surface a disambiguation prompt rather than silently guessing.

Public API
----------
FocusResult   — dataclass returned by resolve_focus()
resolve_focus(query, classify_result, screen_model, target) -> FocusResult
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from classify import ClassifyResult
    from perception_target import PerceptionTarget
    from screen_model import ScreenElement, ScreenModel

_log = logging.getLogger(__name__)

# Scores within this gap of the top score are treated as ambiguous runners-up.
AMBIGUITY_MARGIN = 12.0

# Minimum resolve_reference score required for rung-3 to be considered a hit
# (avoids accepting a 10-point "best guess" as confident focus).
LINGUISTIC_MIN_SCORE = 30.0

# Maximum number of candidates forwarded to the VLM SoM rung.
VLM_MAX_CANDIDATES = 12


# ---------------------------------------------------------------------------
# Source tags (what resolved the focus)
# ---------------------------------------------------------------------------

class FocusSource:
    SELECTION  = "selection"    # UIA TextPattern selection
    CURSOR     = "cursor"       # cursor-position element
    FOCUSED    = "focused"      # UIA focused-element at wake time
    LINGUISTIC = "linguistic"   # screen_model.resolve_reference()
    VLM        = "vlm"          # VLM SoM grounding
    WHOLE      = "whole_screen" # no focus resolved; use full screen text
    UNKNOWN    = "unknown"


# ---------------------------------------------------------------------------
# FocusResult
# ---------------------------------------------------------------------------

@dataclass
class FocusResult:
    """Output of resolve_focus().

    Fields
    ------
    text       : The resolved text content of the focused element / selection.
                 Empty string when source=WHOLE_SCREEN or resolution failed.
    elements   : The ScreenElement(s) that were resolved, in confidence order.
                 Empty when text was obtained via raw selection (no element match).
    bbox       : (x, y, w, h) of the primary element in virtual-desktop pixels,
                 or None.
    source     : FocusSource tag indicating which rung resolved the focus.
    confidence : 0.0–1.0 rough estimate of how reliable this resolution is.
    ambiguous  : True when multiple candidates have nearly equal scores and
                 the caller should prompt the user for clarification.
    runners_up : Additional close candidates when ambiguous=True.
    """
    text: str                                = ""
    elements: list["ScreenElement"]          = field(default_factory=list)
    bbox: Optional[tuple[int, int, int, int]] = None
    source: str                              = FocusSource.UNKNOWN
    confidence: float                        = 0.0
    ambiguous: bool                          = False
    runners_up: list["ScreenElement"]        = field(default_factory=list)

    def is_useful(self) -> bool:
        """True if this result carries meaningful focused context."""
        return bool(self.text) and self.source != FocusSource.WHOLE

    def primary_element(self) -> "Optional[ScreenElement]":
        return self.elements[0] if self.elements else None


# ---------------------------------------------------------------------------
# Deictic sub-classifiers
# ---------------------------------------------------------------------------

# Matches selection-state phrases ("highlighted", "selected", "what's selected").
_SELECTION_RE = re.compile(
    r"\b(highlighted|selected|chosen|what\s+(?:i\s+have\s+|have\s+i\s+|i\'ve\s+)?(?:selected|highlighted)|what\'s\s+(?:selected|highlighted))\b",
    re.IGNORECASE,
)

# Matches cursor/focused-element phrases ("this", "that", "it", "what I'm pointing at").
_CURSOR_RE = re.compile(
    r"\b(this|that)\b"
    r"|what\s+i\'?m\s+(?:pointing|hovering|mousing)\s+(?:at|over)"
    r"|\b(under|at)\s+(the\s+)?cursor\b"
    r"|\b(focused|active|current)\s+(field|element|control|input|box)\b",
    re.IGNORECASE,
)


def _is_selection_query(query: str) -> bool:
    return bool(_SELECTION_RE.search(query))


def _is_cursor_query(query: str) -> bool:
    return bool(_CURSOR_RE.search(query))


# ---------------------------------------------------------------------------
# Rung implementations
# ---------------------------------------------------------------------------

def _text_in_model(text: str, screen_model: "ScreenModel") -> bool:
    """Return True if *text* appears (substring) anywhere in the current ScreenModel.

    Used as a liveness check: if the focused element's text or selection_text is
    absent from the model when perception is non-empty, the state is stale.
    """
    if not text or not screen_model.full_text:
        return True  # can't disprove; don't discard
    needle = text.strip().lower()
    return needle in screen_model.full_text.lower()


def _rung1_selection(
    target: "PerceptionTarget",
    screen_model: "ScreenModel",
) -> Optional[FocusResult]:
    """Rung 1: UIA TextPattern selection (Task 13)."""
    # P11: check TTL before using wake-time selection_text.
    if not target.interaction_state_fresh():
        _log.debug("resolve_focus rung1: interaction state expired (TTL)")
        return None
    # P11: if selection_text from wake time no longer appears in the live model,
    # it describes content that has since scrolled away or changed.
    sel = getattr(target, "selection_text", "")
    if sel and screen_model.full_text and not _text_in_model(sel, screen_model):
        _log.debug("resolve_focus rung1: selection_text absent from current model, discarding")
        return None
    try:
        from adapters.selection_adapter import get_selected_text
        text = get_selected_text(target, use_fallback=False)
        if text:
            _log.debug("resolve_focus rung1 selection: %r", text[:60])
            return FocusResult(
                text=text,
                source=FocusSource.SELECTION,
                confidence=0.95,
            )
        # Explicit empty-string means TextPattern found but nothing selected.
        if text is not None:
            _log.debug("resolve_focus rung1: TextPattern present but nothing selected")
    except Exception as exc:
        _log.debug("resolve_focus rung1 failed: %s", exc)
    return None


def _rung2_cursor(
    screen_model: "ScreenModel",
    target: "PerceptionTarget",
) -> Optional[FocusResult]:
    """Rung 2: cursor-position + UIA-focused-element resolution (Task 14)."""
    # P11: interaction state expires after FOCUS_STATE_TTL_MS.
    state_fresh = target.interaction_state_fresh()

    try:
        from focus import get_element_at_cursor, get_focused_element

        # Try cursor position first (most direct deictic reference).
        # cursor_pos is a screen coordinate, not text — still useful after TTL
        # because cursor *position* is relatively stable (the user hasn't moved
        # the mouse).  Only skip if the target itself predates the TTL.
        cursor_pos = getattr(target, "cursor_pos", None) if state_fresh else None
        elem = get_element_at_cursor(screen_model, cursor_pos)
        if elem is not None:
            _log.debug("resolve_focus rung2 cursor→elem %r", elem.id)
            return FocusResult(
                text=elem.text,
                elements=[elem],
                bbox=elem.bbox,
                source=FocusSource.CURSOR,
                confidence=0.85,
            )

        # Try the UIA focused element (captured before Jarvis took focus).
        # P11: skip entirely when state is stale.
        if not state_fresh:
            _log.debug("resolve_focus rung2: interaction state expired, skipping focused_element")
            return None

        focused_ref = getattr(target, "focused_element", None)
        elem = get_focused_element(screen_model, focused_ref)
        if elem is not None:
            # P11: discard if the resolved element's text is absent from the
            # current ScreenModel — it points at content no longer on screen.
            if elem.text and not _text_in_model(elem.text, screen_model):
                _log.debug(
                    "resolve_focus rung2: focused element text %r absent from model, discarding",
                    elem.text[:40],
                )
                return None
            _log.debug("resolve_focus rung2 focused→elem %r", elem.id)
            return FocusResult(
                text=elem.text,
                elements=[elem],
                bbox=elem.bbox,
                source=FocusSource.FOCUSED,
                confidence=0.80,
            )
    except Exception as exc:
        _log.debug("resolve_focus rung2 failed: %s", exc)
    return None


def _rung3_linguistic(
    query: str,
    screen_model: "ScreenModel",
) -> Optional["tuple[FocusResult, list]"]:
    """Rung 3: screen_model.resolve_reference() (Task 15).

    Returns (FocusResult, all_candidates) or None.
    all_candidates is passed to rung 4 if this rung is insufficient.
    """
    try:
        matches = screen_model.resolve_reference(query)
        if not matches:
            return None

        best = matches[0]
        if best.score < LINGUISTIC_MIN_SCORE:
            _log.debug(
                "resolve_focus rung3: best score %.1f below threshold %.1f",
                best.score, LINGUISTIC_MIN_SCORE,
            )
            return None

        # Check for ambiguity: are runner-up scores within AMBIGUITY_MARGIN?
        runners_up_elems = [
            m.element for m in matches[1:]
            if best.score - m.score <= AMBIGUITY_MARGIN
        ]
        ambiguous = len(runners_up_elems) > 0

        _log.debug(
            "resolve_focus rung3: best=%.1f elem=%r ambiguous=%s",
            best.score, best.element.id, ambiguous,
        )

        result = FocusResult(
            text=best.element.text,
            elements=[best.element],
            bbox=best.element.bbox,
            source=FocusSource.LINGUISTIC,
            confidence=min(1.0, best.score / 100.0),
            ambiguous=ambiguous,
            runners_up=runners_up_elems,
        )
        return result, [m.element for m in matches]
    except Exception as exc:
        _log.debug("resolve_focus rung3 failed: %s", exc)
    return None


def _rung4_vlm(
    query: str,
    candidates: "list[ScreenElement]",
    screen_model: "ScreenModel",
    target: "PerceptionTarget",
) -> Optional[FocusResult]:
    """Rung 4: VLM SoM grounding (Task 16)."""
    try:
        from focus import resolve_reference_vlm
        cands = candidates[:VLM_MAX_CANDIDATES] if candidates else screen_model.elements[:VLM_MAX_CANDIDATES]
        elem = resolve_reference_vlm(query, cands, screen_model, target)
        if elem is not None:
            _log.debug("resolve_focus rung4 VLM→elem %r text=%r", elem.id, elem.text[:40])
            return FocusResult(
                text=elem.text,
                elements=[elem],
                bbox=elem.bbox,
                source=FocusSource.VLM,
                confidence=0.65,  # VLM localization is good but not perfect
            )
    except Exception as exc:
        _log.debug("resolve_focus rung4 failed: %s", exc)
    return None


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def resolve_focus(
    query: str,
    classify_result: "ClassifyResult",
    screen_model: "Optional[ScreenModel]",
    target: "Optional[PerceptionTarget]",
    trace=None,
) -> FocusResult:
    """Run the focus-resolution ladder and return the best FocusResult.

    The ladder stops at the first confident hit.  If nothing resolves,
    returns FocusResult(source=WHOLE_SCREEN) so callers can fall back to
    full screen text without special-casing.

    Parameters
    ----------
    query           : The raw user query string.
    classify_result : ClassifyResult from classify_intent(); needs_focus must
                      be True for this function to be called, but it is safe
                      to call with needs_focus=False (returns WHOLE_SCREEN).
    screen_model    : Active ScreenModel (may be None if perception hasn't run).
    target          : PerceptionTarget captured at wake time (may be None).
    """
    if screen_model is None or target is None:
        result = FocusResult(source=FocusSource.WHOLE, confidence=0.0)
        if trace is not None:
            trace.record("FOCUS", source=str(result.source), resolved_text="",
                         confidence=result.confidence, ambiguous=False)
        return result

    def _emit(result: FocusResult) -> FocusResult:
        if trace is not None:
            trace.record(
                "FOCUS",
                source=str(result.source),
                resolved_text=(result.text or "")[:80],
                confidence=result.confidence,
                ambiguous=result.ambiguous,
            )
        return result

    # Rung 1 — selection (only when the phrase is selection-type).
    if _is_selection_query(query):
        r = _rung1_selection(target, screen_model)
        if r is not None:
            return _emit(r)

    # Rung 2 — cursor / focused element (deictic "this"/"that").
    if _is_cursor_query(query) or _is_selection_query(query):
        r = _rung2_cursor(screen_model, target)
        if r is not None:
            return _emit(r)

    # Rung 3 — linguistic + spatial.
    r3 = _rung3_linguistic(query, screen_model)
    if r3 is not None:
        result, all_candidates = r3
        # Confident non-ambiguous hit: done.
        if not result.ambiguous:
            return _emit(result)
        # Ambiguous: let rung 4 arbitrate, passing the close candidates.
        _log.debug(
            "resolve_focus rung3 ambiguous (%d runners-up), escalating to VLM",
            len(result.runners_up),
        )
        vlm_candidates = [result.elements[0]] + result.runners_up
        r4 = _rung4_vlm(query, vlm_candidates, screen_model, target)
        if r4 is not None:
            return _emit(r4)
        # VLM didn't help; return the ambiguous rung-3 result so the caller
        # can decide whether to surface a disambiguation prompt.
        return _emit(result)

    # Rung 4 — VLM SoM over all elements (last resort).
    r4 = _rung4_vlm(query, [], screen_model, target)
    if r4 is not None:
        return _emit(r4)

    # Nothing resolved — whole-screen fallback.
    _log.debug("resolve_focus: all rungs failed, returning WHOLE_SCREEN")
    return _emit(FocusResult(source=FocusSource.WHOLE, confidence=0.0))
