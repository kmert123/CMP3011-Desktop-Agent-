"""System actions: open_app, set_clipboard, notify, click_element.

All actions are gated by: ACTIONS_ENABLED flag, ALLOWED_ACTIONS whitelist,
kill-hotkey cancel flag, user confirmation modal, and DRY_RUN mode.
After each successful dispatch, the action is verified once (no retry loop).

The kill-hotkey is wired by main.py (via the event bus Cancel event) which
also calls _cancel.set() directly.  actions.py no longer registers its own
hotkey so there is only one binding.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Optional

import config
import telemetry

if TYPE_CHECKING:
    from screen_model import ScreenElement, ScreenModel
    from ui import JarvisWindow

_cancel = threading.Event()

# Minimum rapidfuzz WRatio score (0–100) for a candidate to enter the pool at all.
_FUZZY_MIN = 55.0


# ---------------------------------------------------------------------------
# Action plan types
# ---------------------------------------------------------------------------

@dataclass
class PropertyAssertion:
    """A single property assertion on the UI state.

    kind:   "element_present" | "element_absent" | "clipboard_equals"
            | "element_state"  (e.g. checked/unchecked)
    target: label / text to locate (for element_* kinds) or expected value
            (for clipboard_equals)
    value:  optional secondary constraint (e.g. state name "checked")
    """
    kind: str
    target: str
    value: str = ""


@dataclass
class ActionStep:
    """One step in a multi-step action plan.

    kind:                action kind ("open_app", "set_clipboard", etc.)
    args:                args dict for the action
    precondition:        assertion that must hold BEFORE executing; None = always pass
    expected_postcondition: assertion that must hold AFTER executing; None = skip verify
    description:         human-readable step label for the confirm modal
    """
    kind: str
    args: dict
    precondition: Optional[PropertyAssertion] = None
    expected_postcondition: Optional[PropertyAssertion] = None
    description: str = ""


@dataclass
class ActionPlan:
    """Ordered list of ActionStep objects parsed from a user command."""
    steps: list[ActionStep]
    original_command: str = ""


def _fuzz_score(a: str, b: str) -> float:
    """rapidfuzz WRatio (0–100); falls back to difflib*100 if unavailable."""
    try:
        from rapidfuzz.fuzz import WRatio
        return WRatio(a, b)
    except Exception:
        import difflib
        return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio() * 100


# _cancel is set by main.py when the kill hotkey fires (via Cancel event + direct set).
# No hotkey registration here — main.py owns the single binding.


@dataclass
class ActionResult:
    ok: bool
    message: str
    reason: str = ""
    verified: bool = False
    detail: str = ""


def _gate(kind: str, description: str, ui: "JarvisWindow") -> bool:
    """Return True only if all safety checks pass."""
    if not config.ACTIONS_ENABLED:
        return False
    if kind not in config.ALLOWED_ACTIONS:
        return False
    if _cancel.is_set():
        _cancel.clear()
        return False
    if not ui.confirm_action(description):
        return False
    if config.DRY_RUN:
        telemetry.log_query(telemetry.build_record(query=description, action_kind=kind))
        return False
    return True


# ---------------------------------------------------------------------------
# Verification helpers — each called once, no loops
# ---------------------------------------------------------------------------

def _verify_click(screen_model: "ScreenModel | None") -> tuple[bool, str]:
    """Wait 300 ms then compare dHash of target region before vs after."""
    time.sleep(0.3)

    if screen_model is not None:
        try:
            import capture
            from screen_model import dhash as _dhash

            crop, _origin, _dpi, _stale = capture.capture_target(screen_model.target)
            after_hash = _dhash(crop)
            if after_hash != screen_model.screen_hash:
                return True, "Screen content changed after click."
            return False, "No state delta detected after click."
        except Exception as exc:
            return False, f"Hash comparison failed: {exc}"

    # Fallback when no ScreenModel: detect new foreground window.
    try:
        import win32gui
        hwnd_after = win32gui.GetForegroundWindow()
        title = win32gui.GetWindowText(hwnd_after)
        # We can't compare to pre-click state here, so accept any visible window.
        if hwnd_after and title:
            return True, f"Foreground window: '{title}'."
        return False, "No foreground window detected after click."
    except Exception as exc:
        return False, f"Verification error: {exc}"


def _verify_clipboard(intended: str) -> tuple[bool, str]:
    """Read back the clipboard and confirm it matches the intended text."""
    try:
        if sys.platform == "win32":
            import win32clipboard
            win32clipboard.OpenClipboard()
            try:
                actual = win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT)
            finally:
                win32clipboard.CloseClipboard()
        else:
            import pyperclip
            actual = pyperclip.paste()
        if actual == intended:
            return True, "Clipboard matches intended value."
        preview = actual[:60] + ("…" if len(actual) > 60 else "")
        return False, f"Clipboard mismatch: got {preview!r}."
    except Exception as exc:
        return False, f"Clipboard read error: {exc}"


def _verify_open_app(name: str) -> tuple[bool, str]:
    """Wait 500 ms then check whether a matching process or window appeared."""
    time.sleep(0.5)
    stem = name.lower()

    # Prefer psutil (process-level check).
    try:
        import psutil
        for proc in psutil.process_iter(["name"]):
            proc_name = (proc.info["name"] or "").lower()
            if stem in proc_name:
                return True, f"Process '{proc.info['name']}' is running."
        return False, f"No process matching '{name}' found after launch."
    except ImportError:
        pass
    except Exception as exc:
        return False, f"psutil check failed: {exc}"

    # Fallback: enumerate visible windows via win32.
    try:
        import win32gui
        matches: list[str] = []

        def _cb(hwnd: int, _: Any) -> None:
            if win32gui.IsWindowVisible(hwnd):
                t = win32gui.GetWindowText(hwnd)
                if stem in t.lower():
                    matches.append(t)

        win32gui.EnumWindows(_cb, None)
        if matches:
            return True, f"Window '{matches[0]}' appeared."
        return False, f"No window matching '{name}' appeared after launch."
    except Exception as exc:
        return False, f"Window check failed: {exc}"


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------

def open_app(name: str, ui: "JarvisWindow") -> ActionResult:
    desc = f"Open application: {name}"
    if not _gate("open_app", desc, ui):
        return ActionResult(ok=False, message=f"Action skipped: {desc}", reason="gate")
    cmd = config.APP_WHITELIST.get(name.lower())
    if not cmd:
        return ActionResult(
            ok=False,
            message=f"'{name}' is not in the app whitelist (config.APP_WHITELIST).",
            reason="not_whitelisted",
        )
    try:
        subprocess.Popen(cmd, shell=True)
    except Exception as exc:
        return ActionResult(ok=False, message=f"Failed to open {name}: {exc}", reason="exec_error")

    verified, detail = _verify_open_app(name)
    telemetry.log_query(telemetry.build_record(action_kind="open_app", query=f"open {name}", action_verified=verified))
    if not verified:
        return ActionResult(ok=False, message=f"Launched {name} but could not confirm it started.", reason="unverified", detail=detail)
    return ActionResult(ok=True, message=f"Opened {name}.", verified=True, detail=detail)


def set_clipboard(text: str, ui: "JarvisWindow") -> ActionResult:
    preview = text[:60] + ("…" if len(text) > 60 else "")
    desc = f"Copy to clipboard: {preview}"
    if not _gate("set_clipboard", desc, ui):
        return ActionResult(ok=False, message="Action skipped.", reason="gate")
    try:
        if sys.platform == "win32":
            import win32clipboard
            win32clipboard.OpenClipboard()
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardText(text, win32clipboard.CF_UNICODETEXT)
            win32clipboard.CloseClipboard()
        else:
            import pyperclip
            pyperclip.copy(text)
    except Exception as exc:
        return ActionResult(ok=False, message=f"Clipboard failed: {exc}", reason="exec_error")

    verified, detail = _verify_clipboard(text)
    telemetry.log_query(telemetry.build_record(action_kind="set_clipboard", action_verified=verified))
    if not verified:
        return ActionResult(ok=False, message="Clipboard write could not be confirmed.", reason="unverified", detail=detail)
    return ActionResult(ok=True, message="Copied to clipboard.", verified=True, detail=detail)


def notify(message: str, ui: "JarvisWindow") -> ActionResult:
    desc = f"Show notification: {message}"
    if not _gate("notify", desc, ui):
        return ActionResult(ok=False, message="Action skipped.", reason="gate")
    try:
        try:
            from plyer import notification as plyer_notify
            plyer_notify.notify(title="Jarvis", message=message, timeout=5)
        except ImportError:
            if sys.platform == "win32":
                import ctypes
                threading.Thread(
                    target=lambda: ctypes.windll.user32.MessageBoxW(0, message, "Jarvis", 0x40 | 0x1000),
                    daemon=True,
                ).start()
        telemetry.log_query(telemetry.build_record(action_kind="notify", action_verified=True))
        # No re-perception needed for notifications.
        return ActionResult(ok=True, message="Notification sent.", verified=True, detail="no verification required")
    except Exception as exc:
        return ActionResult(ok=False, message=f"Notification failed: {exc}", reason="exec_error")


# ---------------------------------------------------------------------------
# click_element — grounded against ScreenModel, falls back to UIA walk
# ---------------------------------------------------------------------------

@dataclass
class GroundResult:
    """Outcome of the grounding step.

    Exactly one of `element` or `candidates` is populated:
    - element set, candidates empty  → unambiguous match, proceed.
    - element None, candidates set   → ambiguous (margin too small); surface to UI.
    - element None, candidates empty → no match at all.
    """
    element: "ScreenElement | None" = None
    candidates: list["ScreenElement"] = field(default_factory=list)
    reason: str = ""


def _ground_element(
    label: str,
    screen_model: "ScreenModel",
    ancestor_hint: str = "",
) -> GroundResult:
    """Ground label against the element graph.

    Algorithm:
    1. Restrict candidates to invokable elements with calibrated_confidence >= GROUND_CONF.
    2. If ancestor_hint is provided, prefer elements whose ancestors contain matching text
       (restricts pool to that subtree; falls back to full pool if subtree is empty).
    3. Score each candidate with rapidfuzz WRatio against label.
    4. Drop candidates scoring below _FUZZY_MIN.
    5. Require best score > runner-up score + GROUND_MARGIN*100.
       If margin too small → GroundResult(candidates=[best, runner-up, …], reason="ambiguous").
    6. Return GroundResult(element=best) on unambiguous match.
    """
    # Step 1: build candidate pool
    pool = [
        e for e in screen_model.elements
        if e.invokable and e.calibrated_confidence >= config.GROUND_CONF
    ]

    # Step 2: ancestor scoping — narrow to subtree of first ancestor matching hint
    if ancestor_hint and pool:
        hint_lc = ancestor_hint.lower()
        ancestor = next(
            (e for e in screen_model.elements if hint_lc in e.text.lower()),
            None,
        )
        if ancestor is not None:
            scoped = screen_model.find(invokable=True, within=ancestor)
            scoped_conf = [
                e for e in scoped
                if e.calibrated_confidence >= config.GROUND_CONF
            ]
            if scoped_conf:
                pool = scoped_conf

    if not pool:
        return GroundResult(reason="no invokable elements with sufficient confidence")

    # Step 3–4: score and filter
    scored: list[tuple[float, "ScreenElement"]] = []
    for elem in pool:
        score = _fuzz_score(label, elem.text)
        if score >= _FUZZY_MIN:
            scored.append((score, elem))

    if not scored:
        return GroundResult(reason=f"no element text matched '{label}'")

    scored.sort(key=lambda x: x[0], reverse=True)
    best_score, best_elem = scored[0]

    # Step 5: margin check
    if len(scored) >= 2:
        runner_up_score = scored[1][0]
        margin_needed = config.GROUND_MARGIN * 100  # convert to 0–100 scale
        if best_score - runner_up_score < margin_needed:
            candidates = [e for _, e in scored[:4]]  # top-4 for UI disambiguation
            return GroundResult(
                candidates=candidates,
                reason=(
                    f"ambiguous: '{best_elem.text}' ({best_score:.0f}) vs "
                    f"'{scored[1][1].text}' ({runner_up_score:.0f}), "
                    f"margin {best_score - runner_up_score:.1f} < {margin_needed:.0f}"
                ),
            )

    return GroundResult(element=best_elem)


def _invoke_element(elem: "ScreenElement") -> tuple[bool, str]:
    """Try invoke() then click_input() on an element's UIA handle."""
    handle = elem.handle
    if handle is None:
        return False, f"Element '{elem.text}' has no UIA handle."
    try:
        handle.invoke()
        return True, f"Invoked '{elem.text}'."
    except Exception:
        pass
    try:
        handle.click_input()
        return True, f"Clicked '{elem.text}'."
    except Exception as exc:
        return False, f"Found '{elem.text}' but could not invoke it: {exc}"


def _coord_click(cx: int, cy: int) -> tuple[bool, str]:
    """Move the mouse and left-click at virtual-desktop coordinate (cx, cy)."""
    try:
        import pyautogui
        pyautogui.moveTo(cx, cy, duration=0.1)
        pyautogui.click(cx, cy)
        return True, f"Clicked at ({cx}, {cy})."
    except Exception as exc:
        return False, f"Coordinate click at ({cx}, {cy}) failed: {exc}"


def _region_dhash(target: Any, bbox: tuple[int, int, int, int]) -> str | None:
    """Capture the live window crop and return the dhash of the bbox sub-region.

    Returns None if the capture or hash fails so callers can skip the check.
    """
    try:
        import capture
        from screen_model import dhash as _dhash

        crop, origin, _dpi, stale = capture.capture_target(target)
        if stale:
            return None
        ox, oy = origin
        bx, by, bw, bh = bbox
        x1 = max(0, bx - ox)
        y1 = max(0, by - oy)
        x2 = min(crop.shape[1], bx - ox + bw)
        y2 = min(crop.shape[0], by - oy + bh)
        if x2 <= x1 or y2 <= y1:
            return None
        region = crop[y1:y2, x1:x2]
        return _dhash(region)
    except Exception:
        return None


def _hamming(a: str, b: str) -> int:
    """Bit-wise Hamming distance between two hex dhash strings."""
    try:
        diff = int(a, 16) ^ int(b, 16)
        return bin(diff).count("1")
    except Exception:
        return 0


def _som_click(
    label: str,
    screen_model: "ScreenModel",
    ui: "JarvisWindow",
) -> ActionResult:
    """Set-of-Marks fallback: overlay markers on OCR/CV elements, ask VLM, click matched centre.

    Used when grounding found no invokable UIA handle (Electron, game, PDF viewer).
    The confirm modal is ALWAYS shown before the coordinate click.
    """
    from capture import capture_target
    from set_of_marks import render_som, ask_som_marker, marker_screen_center

    # Capture a fresh crop of the target so markers are drawn on the live frame.
    try:
        crop, origin, _dpi, stale = capture_target(screen_model.target)
        if stale:
            return ActionResult(ok=False, message="Target window is not available for SoM click.", reason="stale")
    except Exception as exc:
        return ActionResult(ok=False, message=f"SoM capture failed: {exc}", reason="capture_error")

    # Build the element list: prefer OCR/CV elements (they have spatial bboxes even
    # without handles).  Exclude pure-layout CV regions (empty text, role="region").
    candidates = [
        e for e in screen_model.elements
        if e.bbox[2] > 0 and e.bbox[3] > 0
        and not (e.source == "cv" and not e.text.strip())
    ]
    if not candidates:
        return ActionResult(ok=False, message=f"No spatial elements available for SoM click on '{label}'.", reason="no_elements")

    annotated, markers = render_som(crop, candidates, origin)
    if not markers:
        return ActionResult(ok=False, message="SoM rendering produced no markers.", reason="no_markers")

    # Ask the VLM which marker matches the target label.
    marker_num = ask_som_marker(annotated, label, n_markers=len(markers))
    if marker_num is None:
        return ActionResult(ok=False, message=f"VLM could not identify a marker matching '{label}'.", reason="som_no_match")

    center = marker_screen_center(marker_num, markers)
    if center is None:
        return ActionResult(ok=False, message=f"Marker {marker_num} has no valid centre.", reason="som_invalid_marker")

    cx, cy = center
    elem = markers[marker_num]

    # Hash the marker's region from the render_som crop so we can detect scroll/nav.
    ox, oy = origin
    _bx, _by, _bw, _bh = elem.bbox
    _rx1 = max(0, _bx - ox)
    _ry1 = max(0, _by - oy)
    _rx2 = min(crop.shape[1], _bx - ox + _bw)
    _ry2 = min(crop.shape[0], _by - oy + _bh)
    from screen_model import dhash as _dhash_fn
    render_hash: str | None = (
        _dhash_fn(crop[_ry1:_ry2, _rx1:_rx2])
        if _rx2 > _rx1 and _ry2 > _ry1
        else None
    )

    # Confirm modal — always required for coordinate clicks.
    desc = f"SoM click: marker {marker_num} → '{elem.text or label}' at ({cx}, {cy})"
    if not ui.confirm_action(desc):
        return ActionResult(ok=False, message="SoM click cancelled by user.", reason="gate")
    if config.DRY_RUN:
        telemetry.log_query(telemetry.build_record(query=desc, action_kind="click_element"))
        return ActionResult(ok=False, message=f"DRY_RUN: would click marker {marker_num} at ({cx}, {cy}).", reason="dry_run")

    # Stale-region check: re-capture the marker area and compare dhash to render time.
    # If the content changed beyond CLICK_STALE_HAMMING bits, the window has scrolled
    # or navigated — abort rather than clicking the wrong target.
    if render_hash is not None:
        live_hash = _region_dhash(screen_model.target, elem.bbox)
        if live_hash is not None and _hamming(render_hash, live_hash) > config.CLICK_STALE_HAMMING:
            return ActionResult(
                ok=False,
                message=(
                    f"SoM click aborted: region around '{elem.text or label}' changed "
                    f"since markers were rendered (dhash distance "
                    f"{_hamming(render_hash, live_hash)} > {config.CLICK_STALE_HAMMING}). "
                    "Re-resolving is needed."
                ),
                reason="stale_region",
            )

    ok, msg = _coord_click(cx, cy)
    if not ok:
        telemetry.log_query(telemetry.build_record(action_kind="click_element", query=label, action_verified=False))
        return ActionResult(ok=False, message=msg, reason="invoke_failed")

    verified, detail = _verify_click(screen_model)
    telemetry.log_query(telemetry.build_record(action_kind="click_element", query=label, action_verified=verified))
    if not verified:
        return ActionResult(ok=False, message=msg, reason="unverified", detail=detail)
    return ActionResult(ok=True, message=f"SoM clicked '{elem.text or label}' at ({cx}, {cy}).", verified=True, detail=detail)


# Tolerance in pixels for bbox centre comparison during pre-click re-check.
_BOUNDS_TOLERANCE_PX = 30


def _uia_recheck(elem: "ScreenElement", target: Any) -> tuple[bool, str]:
    """Fresh UIA re-read scoped to elem's region; confirms the element is still
    present, still matches (role + text + bounds), and is the UNIQUE match.

    Returns (ok, reason).  ok=False means the caller must abort.
    """
    # 1. Handle liveness: if the stored handle raises ElementNotAvailable abort immediately.
    if elem.handle is not None:
        try:
            _ = elem.handle.element_info.name
        except Exception as exc:
            return False, f"target changed/ambiguous before click: handle invalid ({exc})"

    # 2. Fresh targeted UIA walk scoped to the element's bbox region.
    try:
        from pywinauto import Desktop
        from pywinauto.base_wrapper import ElementNotAvailable  # type: ignore

        hwnd = getattr(target, "hwnd", 0) if target is not None else 0
        if not hwnd:
            # No live target; skip re-check and trust handle liveness.
            return True, ""

        desktop = Desktop(backend="uia")
        wrapper = desktop.window(handle=hwnd).wrapper_object()

        ex, ey, ew, eh = elem.bbox
        ex_cx = ex + ew // 2
        ex_cy = ey + eh // 2
        role_lc = elem.role.lower()
        text_lc = elem.text.lower()

        matches: list[Any] = []

        def _scan(node: Any, depth: int) -> None:
            if depth > config.UIA_MAX_DEPTH:
                return
            try:
                info = node.element_info
                ctrl = (getattr(info, "control_type", "") or "").strip().lower()
                name = (info.name or "").strip().lower()
                r = info.rectangle
                w = r.right - r.left
                h = r.bottom - r.top
                if w > 0 and h > 0:
                    cx = r.left + w // 2
                    cy = r.top + h // 2
                    within_bounds = (
                        abs(cx - ex_cx) <= _BOUNDS_TOLERANCE_PX
                        and abs(cy - ex_cy) <= _BOUNDS_TOLERANCE_PX
                    )
                    if within_bounds and ctrl == role_lc and name == text_lc:
                        matches.append(node)
            except Exception:
                pass
            try:
                for child in node.children():
                    _scan(child, depth + 1)
            except Exception:
                pass

        _scan(wrapper, 0)

        if len(matches) == 0:
            return False, "target changed/ambiguous before click: element no longer found"
        if len(matches) > 1:
            return False, "target changed/ambiguous before click: multiple matching elements"
        return True, ""

    except Exception as exc:
        # Re-check infrastructure failed; allow the action through (fail open)
        # so that a missing pywinauto import doesn't silently block all clicks.
        import logging
        logging.getLogger(__name__).debug("uia_recheck skipped: %s", exc)
        return True, ""


def _uia_find_element(elem: Any, target: str, depth: int) -> Any:
    if depth > config.UIA_MAX_DEPTH:
        return None
    try:
        name = (elem.element_info.name or "").strip().lower()
        if name == target:
            return elem
    except Exception:
        pass
    try:
        for child in elem.children():
            found = _uia_find_element(child, target, depth + 1)
            if found is not None:
                return found
    except Exception:
        pass
    return None


def _uia_find_and_invoke(label: str) -> tuple[bool, str]:
    """Fallback: walk the live UIA tree (no ScreenModel available)."""
    try:
        import win32gui
        from pywinauto import Desktop

        hwnd = win32gui.GetForegroundWindow()
        if not hwnd:
            return False, "No foreground window."

        desktop = Desktop(backend="uia")
        wrapper = desktop.window(handle=hwnd).wrapper_object()
        target = _uia_find_element(wrapper, label.lower(), depth=0)

        if target is None:
            return False, f"No element named '{label}' found in the UIA tree."

        try:
            target.invoke()
            return True, f"Invoked '{label}'."
        except Exception:
            pass
        try:
            target.click_input()
            return True, f"Clicked '{label}'."
        except Exception as exc:
            return False, f"Found '{label}' but could not invoke it: {exc}"

    except Exception as exc:
        return False, f"UIA click failed: {exc}"


def click_element(
    label: str,
    ui: "JarvisWindow",
    screen_model: "ScreenModel | None" = None,
    ancestor_hint: str = "",
) -> ActionResult:
    """Click a UI element located by label.

    Grounding priority:
    1. UIA handle path: ground against invokable ScreenModel elements → invoke().
    2. SoM fallback: if no invokable handle is found (Electron/game) but OCR/CV
       spatial data exists, render set-of-marks, ask VLM, coordinate-click.
    3. Live UIA walk: if no ScreenModel at all.

    ancestor_hint — optional context string (e.g. dialog title) that scopes
    the search to the subtree of the matching ancestor element.
    """
    desc = f"Click UI element: '{label}'"
    if not _gate("click_element", desc, ui):
        return ActionResult(ok=False, message="Action skipped.", reason="gate")

    if screen_model is not None and screen_model.elements:
        ground = _ground_element(label, screen_model, ancestor_hint=ancestor_hint)

        if ground.candidates:
            # Ambiguous UIA match: surface and abort rather than guess.
            candidate_labels = ", ".join(f"'{e.text}'" for e in ground.candidates)
            return ActionResult(
                ok=False,
                message=f"Ambiguous match for '{label}': {candidate_labels}. Please be more specific.",
                reason=ground.reason,
                detail=candidate_labels,
            )

        if ground.element is not None:
            # --- UIA handle path ---
            elem = ground.element
            recheck_ok, recheck_reason = _uia_recheck(elem, getattr(screen_model, "target", None))
            if not recheck_ok:
                return ActionResult(ok=False, message=recheck_reason, reason=recheck_reason)
            ok, msg = _invoke_element(elem)
            if not ok:
                # Handle exists but invoke failed — try SoM before giving up.
                return _som_click(label, screen_model, ui)
        else:
            # --- No invokable handle found: try SoM ---
            return _som_click(label, screen_model, ui)

    else:
        # --- No ScreenModel: live UIA walk ---
        elem = None
        ok, msg = _uia_find_and_invoke(label)
        if not ok:
            telemetry.log_query(telemetry.build_record(action_kind="click_element", query=label, action_verified=False))
            return ActionResult(ok=False, message=msg, reason="invoke_failed")

    verified, detail = _verify_click(screen_model)
    telemetry.log_query(telemetry.build_record(action_kind="click_element", query=label, action_verified=verified))
    if not verified:
        return ActionResult(ok=False, message=msg, reason="unverified", detail=detail)
    return ActionResult(ok=True, message=msg, verified=True, detail=detail)


# ---------------------------------------------------------------------------
# Undo helpers
# ---------------------------------------------------------------------------

def _read_clipboard() -> str:
    """Read the current clipboard contents; return '' on failure."""
    try:
        if sys.platform == "win32":
            import win32clipboard
            win32clipboard.OpenClipboard()
            try:
                return win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT) or ""
            finally:
                win32clipboard.CloseClipboard()
        else:
            import pyperclip
            return pyperclip.paste() or ""
    except Exception:
        return ""


def _undo_set_clipboard(prior_text: str) -> None:
    """Restore clipboard to *prior_text* (best-effort, no exception)."""
    try:
        if sys.platform == "win32":
            import win32clipboard
            win32clipboard.OpenClipboard()
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardText(prior_text, win32clipboard.CF_UNICODETEXT)
            win32clipboard.CloseClipboard()
        else:
            import pyperclip
            pyperclip.copy(prior_text)
    except Exception:
        pass


def _undo_open_app(process_stem: str) -> None:
    """Kill a recently launched process by stem name (best-effort)."""
    try:
        import psutil
        for proc in psutil.process_iter(["name", "pid"]):
            if process_stem in (proc.info["name"] or "").lower():
                proc.kill()
                break
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Assertion checking
# ---------------------------------------------------------------------------

def _check_assertion(
    assertion: Optional[PropertyAssertion],
    screen_model: "ScreenModel | None",
) -> tuple[bool, str]:
    """Evaluate *assertion* against the current state.

    Returns (passed, reason_if_failed).  None assertion always passes.
    """
    if assertion is None:
        return True, ""

    kind = assertion.kind
    target = assertion.target

    if kind == "clipboard_equals":
        actual = _read_clipboard()
        if actual == target:
            return True, ""
        return False, f"clipboard expected {target!r}, got {actual!r}"

    # Element-based assertions require a ScreenModel.
    if kind in ("element_present", "element_absent", "element_state"):
        if screen_model is None:
            # No model to check against — do a live UIA walk.
            try:
                import win32gui
                from pywinauto import Desktop
                hwnd = win32gui.GetForegroundWindow()
                if hwnd:
                    desktop = Desktop(backend="uia")
                    wrapper = desktop.window(handle=hwnd).wrapper_object()
                    found = _uia_find_element(wrapper, target.lower(), 0)
                    present = found is not None
                else:
                    present = False
            except Exception:
                present = False
        else:
            matches = screen_model.find(text_contains=target)
            present = bool(matches)

        if kind == "element_present":
            if present:
                return True, ""
            return False, f"element '{target}' not found"
        if kind == "element_absent":
            if not present:
                return True, ""
            return False, f"element '{target}' still present (expected absent)"
        if kind == "element_state":
            # Re-read ScreenModel to check the element's state value.
            if not present:
                return False, f"element '{target}' not found (cannot check state)"
            if screen_model is not None:
                matches = screen_model.find(text_contains=target)
                if matches:
                    elem = matches[0]
                    state_val = getattr(elem, "value", "") or ""
                    if assertion.value.lower() in state_val.lower():
                        return True, ""
                    return False, f"element '{target}' state={state_val!r} expected {assertion.value!r}"
            return True, ""  # can't check state without model; pass

    return True, ""  # unknown assertion kind: pass permissively


def _re_read_screen_model(
    screen_model: "ScreenModel | None",
) -> "ScreenModel | None":
    """Re-capture a fresh ScreenModel from the same target as *screen_model*.

    Returns None if re-read fails.  Used for postcondition verification.
    """
    if screen_model is None:
        return None
    try:
        from adapters.uia_adapter import read_uia as _uia_adapt
        from adapters.ocr_adapter import read_ocr as _ocr_adapt
        from adapters.cv_adapter import read_cv as _cv_adapt
        from capture import capture_target as _cap
        from fusion import fuse as _fuse

        target = screen_model.target
        crop, origin, _dpi, stale = _cap(target)
        if stale:
            return None
        return _fuse(target, _uia_adapt(target), _ocr_adapt(crop, origin), _cv_adapt(crop, origin), crop, stale=False)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Event-driven settle helper
# ---------------------------------------------------------------------------

def _settle_and_reread(
    screen_model: "ScreenModel | None",
) -> "ScreenModel | None":
    """Wait for a UIA StructureChanged/PropertyChanged event on the target window,
    then pause briefly and re-read.

    Bounds: [SETTLE_MIN_MS, SETTLE_MAX_MS].
    - If an event fires before the ceiling, sleep SETTLE_MIN_MS then re-read.
    - If no event fires, fall through immediately at the ceiling and re-read.
    Returns None if re-read fails or no target available.
    """
    if screen_model is None:
        return None

    target = screen_model.target
    hwnd = getattr(target, "hwnd", 0)

    min_s = config.SETTLE_MIN_MS / 1000.0
    max_s = config.SETTLE_MAX_MS / 1000.0

    if hwnd:
        try:
            from uia_watcher import UIAWatcher

            changed = threading.Event()

            watcher = UIAWatcher(
                hwnd,
                on_invalidate=lambda: None,  # cache invalidation not needed here
                on_change=lambda: changed.set(),
            )
            watcher.start()
            try:
                changed.wait(timeout=max_s)
                if changed.is_set():
                    time.sleep(min_s)
            finally:
                watcher.stop()
        except Exception:
            # UIAWatcher unavailable (non-Windows, missing COM, etc.) — fall back.
            time.sleep(min_s)
    else:
        time.sleep(min_s)

    return _re_read_screen_model(screen_model)


# ---------------------------------------------------------------------------
# Plan executor
# ---------------------------------------------------------------------------

def execute_plan(
    plan: "ActionPlan",
    ui: "JarvisWindow",
    screen_model: "ScreenModel | None" = None,
) -> ActionResult:
    """Execute each ActionStep in *plan* sequentially.

    For each step:
    1. Check precondition — abort (no retry) if fails.
    2. Execute the action (up to 1 retry on postcondition failure).
    3. Re-read the screen.
    4. Check postcondition — retry once if fails, then abort.

    Undo is applied to already-completed steps before returning a failure:
    - set_clipboard: restore prior clipboard value
    - open_app: kill the launched process

    Returns the last ActionResult.  If any step fails, that result is
    returned immediately with all completed steps' undo already run.
    """
    if not plan.steps:
        return ActionResult(ok=False, message="Empty action plan.", reason="empty_plan")

    # Track completed steps for undo.
    completed: list[tuple[ActionStep, Any]] = []  # (step, undo_state)

    def _run_undo() -> None:
        for done_step, undo_state in reversed(completed):
            if done_step.kind == "set_clipboard":
                _undo_set_clipboard(undo_state)
            elif done_step.kind == "open_app":
                if undo_state:
                    _undo_open_app(undo_state)

    current_sm = screen_model

    for step_idx, step in enumerate(plan.steps):
        if _cancel.is_set():
            _cancel.clear()
            _run_undo()
            return ActionResult(ok=False, message="Action cancelled by kill hotkey.", reason="cancelled")

        step_label = step.description or f"{step.kind}({step.args})"

        # --- 1. Precondition check ---
        pre_ok, pre_reason = _check_assertion(step.precondition, current_sm)
        if not pre_ok:
            _run_undo()
            return ActionResult(
                ok=False,
                message=f"Step {step_idx + 1} precondition failed: {pre_reason}",
                reason="precondition_failed",
                detail=step_label,
            )

        # --- 2. Execute (with one retry on postcondition failure) ---
        for attempt in range(2):  # attempt 0 = first try; attempt 1 = retry
            if _cancel.is_set():
                _cancel.clear()
                _run_undo()
                return ActionResult(ok=False, message="Action cancelled.", reason="cancelled")

            # Save undo state before executing.
            undo_state: Any = None
            if step.kind == "set_clipboard":
                undo_state = _read_clipboard()
            elif step.kind == "open_app":
                undo_state = step.args.get("name", "").lower()

            # Run the individual action.
            result = dispatch_one(step.kind, step.args, ui, screen_model=current_sm)

            if not result.ok:
                # Action itself failed — no point retrying.
                _run_undo()
                return ActionResult(
                    ok=False,
                    message=f"Step {step_idx + 1} failed: {result.message}",
                    reason=result.reason,
                    detail=result.detail,
                )

            # --- 3. Re-read screen (event-driven settle) ---
            fresh_sm = _settle_and_reread(current_sm)

            # --- 4. Postcondition check ---
            post_ok, post_reason = _check_assertion(step.expected_postcondition, fresh_sm)
            if post_ok:
                # Success for this step.
                completed.append((step, undo_state))
                current_sm = fresh_sm or current_sm
                break
            else:
                if attempt == 0:
                    # First failure: retry once.
                    import logging
                    logging.getLogger(__name__).debug(
                        "Step %d postcondition failed (%s), retrying once", step_idx + 1, post_reason
                    )
                    continue
                else:
                    # Second failure: abort with undo.
                    _run_undo()
                    return ActionResult(
                        ok=False,
                        message=f"Step {step_idx + 1} postcondition not satisfied after retry: {post_reason}",
                        reason="postcondition_failed",
                        detail=step_label,
                        verified=False,
                    )

    # All steps passed.
    last = result  # noqa: F821 — loop always runs at least once
    return ActionResult(
        ok=True,
        message=last.message,
        verified=True,
        detail=f"All {len(plan.steps)} step(s) completed and verified.",
    )


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

def dispatch_one(
    kind: str,
    args: dict,
    ui: "JarvisWindow",
    screen_model: "ScreenModel | None" = None,
) -> ActionResult:
    """Execute a single action by kind+args.  No plan logic, no undo."""
    if kind == "open_app":
        return open_app(args.get("name", ""), ui)
    if kind == "set_clipboard":
        return set_clipboard(args.get("text", ""), ui)
    if kind == "notify":
        return notify(args.get("message", ""), ui)
    if kind == "click_element":
        return click_element(
            args.get("label", ""),
            ui,
            screen_model=screen_model,
            ancestor_hint=args.get("ancestor_hint", ""),
        )
    return ActionResult(ok=False, message=f"Unknown action kind: '{kind}'", reason="unknown_kind")


def dispatch(
    kind: str,
    args: dict,
    ui: "JarvisWindow",
    screen_model: "ScreenModel | None" = None,
    *,
    plan: "ActionPlan | None" = None,
) -> ActionResult:
    """Route to execute_plan (preferred) or dispatch_one (single step).

    When *plan* is provided, run the full plan with precondition / postcondition
    checks, retry, and undo.  Otherwise fall back to a single-step dispatch.
    """
    if plan is not None:
        return execute_plan(plan, ui, screen_model=screen_model)
    return dispatch_one(kind, args, ui, screen_model=screen_model)
