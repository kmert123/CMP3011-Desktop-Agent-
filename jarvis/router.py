"""Route a query: classify -> window check -> cache or ladder -> telemetry."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Optional, TYPE_CHECKING

import config
import llm_router
from classify import Act, Intent, Perception, _HIGH_CONF_RULES, classify_intent
from perception import PerceptionResult, Rung, read_window, run_ladder
from perception_policy import policy_for
from session_context import SessionContext
from telemetry import build_record, log_query

if TYPE_CHECKING:
    import numpy as np
    from app_classifier import AppClass
    from focus_resolver import FocusResult
    from perception_target import PerceptionTarget


# ---------------------------------------------------------------------------
# entry_rung: deterministic function of (perception, app_class)
# No LLM guessing — the router just looks up the cheapest rung that can satisfy
# the perception need for this renderer family.
# ---------------------------------------------------------------------------

def entry_rung_for(perception: Perception, app_class: "AppClass | None") -> Optional[Rung]:
    """Return the cheapest Rung that satisfies *perception* for *app_class*.

    Electron/game UIA trees are nearly empty, so STRUCTURE falls back to OCR.
    PIXELS always uses VISION.
    NONE means no perception needed.
    """
    if perception == Perception.NONE:
        return None
    if perception == Perception.PIXELS:
        return Rung.VISION

    # STRUCTURE: UIA is sufficient for most apps; fall back to OCR for Electron/games.
    from app_classifier import AppClass as AC
    if app_class in (AC.CHROMIUM_ELECTRON, AC.GAME_FULLSCREEN):
        return Rung.OCR
    return Rung.UIA


# ---------------------------------------------------------------------------
# RouteResult — two-axis schema; .intent is a backward-compat property
# ---------------------------------------------------------------------------

@dataclass
class RouteResult:
    act: Act
    perception_mode: Perception            # routing axis: NONE / STRUCTURE / PIXELS
    perception: Optional[PerceptionResult] # actual perception output (PerceptionResult | None)
    used_cache: bool
    focus_result: "Optional[FocusResult]" = field(default=None)  # populated by _answer_worker when needs_focus

    @property
    def intent(self) -> Intent:
        """Backward-compat: derive Intent from (act, perception_mode)."""
        return Intent.from_axes(self.act, self.perception_mode)


# ---------------------------------------------------------------------------
# Escalation helpers
# ---------------------------------------------------------------------------

_ELEMENT_REF_RE = re.compile(
    r'the\s+(\w+)\s+(?:button|menu|tab|field|checkbox|link|option|item)',
    re.IGNORECASE,
)
_QUOTED_RE = re.compile(r'["\x27](\w[\w\s]*?)["\x27]')

# Cross-window reference patterns: "paste into Slack", "open in Chrome", etc.
# Captures ONE capitalised word after the preposition (app names are usually one token).
_CROSS_WINDOW_RE = re.compile(
    r'\b(?:in|into|to|on|using|via|with|open(?:\s+in)?)\s+([A-Z][A-Za-z0-9]+)',
)
# Stop-words that are not app names.
_CROSS_WINDOW_STOPWORDS = frozenset({
    "the", "a", "an", "it", "this", "that", "my", "your", "our",
    "here", "there", "word", "text", "note", "file", "folder",
    "window", "screen", "desktop", "clipboard",
})


def should_escalate(query: str, route_result: RouteResult) -> bool:
    """True if perception quality is too low and we should run one rung deeper.

    Conditions (either triggers escalation):
    1. ScreenModel exists and max element confidence < ESCALATE_CONF.
    2. Query explicitly names a UI element ("the X button", quoted strings) that
       find() cannot locate in the ScreenModel.

    Hard guards: never escalate PIXELS perception; skip if no perception.
    """
    if route_result.perception_mode == Perception.PIXELS:
        return False
    if route_result.perception is None:
        return False

    sm = route_result.perception.screen_model

    if sm is not None and sm.elements:
        # P8: compute max confidence over content-region elements only so that
        # high-confidence chrome text (tabs, omnibox) cannot mask poorly-perceived content.
        content_elems = [e for e in sm.elements if e.in_content_region]
        conf_pool = content_elems if content_elems else sm.elements  # fallback: all
        if max(e.calibrated_confidence for e in conf_pool) < config.ESCALATE_CONF:
            return True
        refs = _ELEMENT_REF_RE.findall(query) + _QUOTED_RE.findall(query)
        if refs and not any(sm.find(text_contains=r) for r in refs):
            return True

    return False


# ---------------------------------------------------------------------------
# Cross-window reference helpers
# ---------------------------------------------------------------------------

def extract_cross_window_hints(query: str) -> list[str]:
    """Return candidate app-name hints mentioned as cross-window targets in query.

    E.g. "paste into Slack" → ["slack"]
         "open this in Chrome" → ["chrome"]
    Returns lowercase strings; caller checks against WorldState.
    """
    hints: list[str] = []
    for m in _CROSS_WINDOW_RE.finditer(query):
        candidate = m.group(1).strip().lower()
        if candidate not in _CROSS_WINDOW_STOPWORDS and len(candidate) >= 2:
            hints.append(candidate)
    return hints


def resolve_cross_window(
    query: str, session: SessionContext
) -> "PerceptionResult | None":
    """Check if the query references a named window other than the active target.

    If found in WorldState.registry, return a synthetic PerceptionResult backed by
    that window's ScreenModel so the action path can ground there.  Returns None if
    no cross-window hint matches anything in the registry.
    """
    hints = extract_cross_window_hints(query)
    if not hints:
        return None

    ws = session.world_state
    active = ws.active
    active_process = (
        active.target.process.lower() if active is not None else ""
    )

    for hint in hints:
        # Skip if hint matches the already-active window.
        if hint == active_process or hint in active_process:
            continue
        model = ws.find_window(hint)
        if model is not None:
            return PerceptionResult(
                rung=Rung.UIA,
                text=model.full_text,
                window_sig=f"{model.target.process}:{model.target.title}",
                source="world_state",
                ok=not model.stale,
                screen_model=model,
            )

    return None


# ---------------------------------------------------------------------------
# Cache hash helper
# ---------------------------------------------------------------------------

def _compute_cache_hash(
    target: "PerceptionTarget | None",
) -> "tuple[str, np.ndarray | None]":
    try:
        import numpy as np
        import capture
        from screen_model import roi_dhash
        if target is not None and not target.is_self:
            crop, _origin, _dpi, _stale = capture.capture_target(target)
        else:
            crop = capture.capture_primary_monitor()
        return roi_dhash(crop, size=config.CACHE_HASH_SIZE), crop
    except Exception:
        return "", None


# ---------------------------------------------------------------------------
# Window signal helper
# ---------------------------------------------------------------------------

def _window_sig_and_fallback(session: SessionContext) -> tuple[str, str]:
    target = session.active_target
    if target is not None and target.is_self:
        fallback_sig = ""
        if session.recent_windows:
            w = session.recent_windows[-1]
            fallback_sig = f"{w['process']}:{w['title']}"
        return fallback_sig, fallback_sig

    win = read_window()
    window_sig = win.window_sig
    parts = window_sig.split(":", 1)
    session.note_window(
        title=parts[1] if len(parts) > 1 else "",
        process=parts[0],
    )
    return window_sig, ""


# ---------------------------------------------------------------------------
# LLM result parsing — now only supplies act/perception, NOT entry_rung
# ---------------------------------------------------------------------------

_ACT_MAP  = {"ACT": Act.ACT, "ANSWER": Act.ANSWER}
_PERC_MAP = {"NONE": Perception.NONE, "STRUCTURE": Perception.STRUCTURE, "PIXELS": Perception.PIXELS}


def _llm_to_axes(llm: dict) -> tuple[Act, Perception]:
    act  = _ACT_MAP.get(str(llm.get("act", "")).upper(), Act.ANSWER)
    perc = _PERC_MAP.get(str(llm.get("perception", "")).upper(), Perception.STRUCTURE)
    return act, perc


# ---------------------------------------------------------------------------
# Main routing entry point
# ---------------------------------------------------------------------------

def route(query: str, session: SessionContext, frame=None, trace=None) -> RouteResult:
    """Classify the query, check the cache, run the perception ladder if needed.

    Routing priority (§5.2):
      1. Regex fast-path — if matched rule is in _HIGH_CONF_RULES (ACT / explicit PIXELS).
      2. Local LLM — optional fast-path for ambiguous queries; demoted, never for entry_rung.
      3. Regex fallback — when LLM is down or timed out.

    entry_rung is always derived deterministically from (perception_mode, app_class).
    """
    t0 = time.monotonic()
    target = session.active_target
    app_class = getattr(target, "app_class", None) if target else None

    # --- Classification ---
    regex_result = classify_intent(query)

    if regex_result.high_conf:
        act             = regex_result.act
        perception_mode = regex_result.perception
        router_source   = "regex"
        router_confidence = 1.0
    else:
        llm_result = llm_router.route_llm(query, session, target)
        if llm_result is not None:
            act, perception_mode = _llm_to_axes(llm_result)
            router_source     = "llm"
            router_confidence = float(llm_result.get("confidence", 0.5))
        else:
            act             = regex_result.act
            perception_mode = regex_result.perception
            router_source   = "fallback"
            router_confidence = 0.5

    # --- entry_rung: deterministic from axes + app_class (no LLM input) ---
    entry_rung = entry_rung_for(perception_mode, app_class)

    # --- Window signal + cache check ---
    window_sig, fallback_sig = _window_sig_and_fallback(session)
    process = window_sig.split(":", 1)[0] if window_sig else ""

    perception: Optional[PerceptionResult] = None
    used_cache = False

    # Derive the perception policy from the target's app class.
    policy = policy_for(app_class)

    if act == Act.ANSWER and entry_rung is not None:
        current_hash, current_crop = _compute_cache_hash(target)
        if entry_rung <= Rung.UIA and session.screen_read_fresh(process, current_hash, current_crop):
            sr = session.last_screen_read
            assert sr is not None
            perception = PerceptionResult(
                rung=entry_rung,
                text=sr["text"],
                window_sig=window_sig,
                source=sr["source"],
                ok=True,
                # Restore the ScreenModel so a cache hit delivers the same rich
                # context as a fresh read (full text block, element tree,
                # escalation eligibility) instead of a truncated text-only prompt.
                screen_model=sr.get("screen_model"),
            )
            used_cache = True
        else:
            # Check for a cross-window reference before running the ladder.
            cross = resolve_cross_window(query, session)
            if cross is not None and cross.ok:
                perception = cross
            else:
                use_fusion = (
                    target is not None
                    and not getattr(target, "is_self", False)
                    and entry_rung is not None
                )
                perception = run_ladder(
                    entry_rung, frame,
                    target=target,
                    fallback_sig=fallback_sig,
                    use_fusion=use_fusion,
                    policy=policy,
                    trace=trace,
                )
            if trace is not None:
                sm = perception.screen_model
                trace.record(
                    "PERCEPTION",
                    rung=perception.rung.name,
                    source=perception.source,
                    chars=len(perception.text or ""),
                    element_count=len(sm.elements) if sm else 0,
                    ok=perception.ok,
                    stale=getattr(perception, "stale", False),
                    used_cache=used_cache,
                )
            if perception.text.strip():
                session.set_screen_read(
                    perception.text, perception.source, process,
                    current_hash, roi_crop=current_crop,
                    hwnd=target.hwnd if target else 0,
                    app_class=app_class.value if app_class is not None else None,
                    screen_model=perception.screen_model,
                )
            # Update WorldState with the freshly-perceived ScreenModel.
            if perception.screen_model is not None:
                session.world_state.update_active(perception.screen_model)

    latency_ms = int((time.monotonic() - t0) * 1000)
    app_class_val = app_class.value if app_class is not None else None
    log_query(build_record(
        query=query,
        intent=Intent.from_axes(act, perception_mode).value,
        perception_rung=perception.rung.name if perception else None,
        used_cache=used_cache,
        latency_ms=latency_ms,
        router_source=router_source,
        router_confidence=router_confidence,
        rung_reached=perception.rung.name if perception else None,
        app_class=app_class_val,
    ))

    return RouteResult(
        act=act,
        perception_mode=perception_mode,
        perception=perception,
        used_cache=used_cache,
    )


def escalate_route(
    query: str,
    session: SessionContext,
    current_rung: Optional[Rung],
    frame=None,
) -> RouteResult:
    """Run perception one rung deeper than current_rung and return a new RouteResult."""
    _escalation_map: dict[Optional[Rung], Optional[Rung]] = {
        None:        Rung.UIA,
        Rung.UIA:    Rung.OCR,
        Rung.OCR:    Rung.VISION,
        Rung.VISION: None,
    }
    next_rung = _escalation_map.get(current_rung)
    if next_rung is None:
        return RouteResult(
            act=Act.ANSWER,
            perception_mode=Perception.PIXELS,
            perception=None,
            used_cache=False,
        )

    target = session.active_target
    app_class = getattr(target, "app_class", None) if target else None
    policy = policy_for(app_class)
    fallback_sig = ""
    if target is not None and target.is_self and session.recent_windows:
        w = session.recent_windows[-1]
        fallback_sig = f"{w['process']}:{w['title']}"
    win_sig = fallback_sig if (target and target.is_self) else read_window().window_sig
    process = win_sig.split(":", 1)[0] if win_sig else ""
    current_hash, current_crop = _compute_cache_hash(target)

    perception = run_ladder(next_rung, frame, target=target, fallback_sig=fallback_sig, policy=policy)
    if perception.text.strip():
        session.set_screen_read(
            perception.text, perception.source, process,
            current_hash, roi_crop=current_crop,
            hwnd=target.hwnd if target else 0,
            app_class=app_class.value if app_class is not None else None,
            screen_model=perception.screen_model,
        )
    if perception.screen_model is not None:
        session.world_state.update_active(perception.screen_model)

    # Map the rung back to a perception_mode for the result.
    perc_mode = Perception.PIXELS if next_rung == Rung.VISION else Perception.STRUCTURE
    return RouteResult(
        act=Act.ANSWER,
        perception_mode=perc_mode,
        perception=perception,
        used_cache=False,
    )
