"""Selection adapter: retrieve the currently selected text from a target window.

Primary path  — UIA TextPattern (background-safe, no focus change):
    Walk the UIA tree of target.hwnd looking for elements that support
    IUIAutomationTextPattern.  Call GetSelection(), join all non-empty ranges.
    This works while Jarvis has focus; the target hwnd is queried out-of-process.

    Shortcut: if target.focused_element was captured at wake time (Task 12),
    try that element first before walking the full tree.

Fallback path — synthetic Ctrl+C (focus-stealing, last resort):
    Focus the target hwnd, send Ctrl+C, read the clipboard, then restore the
    prior clipboard value.  Only attempted when TextPattern is unavailable AND
    the caller explicitly opts in via use_fallback=True.
"""

from __future__ import annotations

import sys
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from perception_target import PerceptionTarget

# UIA pattern id for IUIAutomationTextPattern (constant; does not require COM init).
_UIA_TEXT_PATTERN_ID = 10014


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _text_from_uia_element(elem_com: Any) -> str | None:
    """Try to get the selected text from a raw IUIAutomationElement COM object.

    Returns the joined selection string, "" if nothing is selected, or None if
    the element does not support TextPattern.
    """
    try:
        pattern_obj = elem_com.GetCurrentPattern(_UIA_TEXT_PATTERN_ID)
        if not pattern_obj:
            return None
        try:
            import comtypes.gen.UIAutomationClient as _uia  # type: ignore[import]
            text_pattern = pattern_obj.QueryInterface(_uia.IUIAutomationTextPattern)
        except Exception:
            return None

        sel_array = text_pattern.GetSelection()
        if sel_array is None:
            return ""
        parts: list[str] = []
        for i in range(sel_array.Length):
            r = sel_array.GetElement(i)
            if r is not None:
                try:
                    chunk = r.GetText(-1) or ""
                    if chunk:
                        parts.append(chunk)
                except Exception:
                    pass
        return "".join(parts)
    except Exception:
        return None


def _iter_uia_elements(hwnd: int):
    """Yield raw IUIAutomationElement COM objects for all descendants of hwnd.

    Uses pywinauto to get the root wrapper, then accesses the underlying
    element_info._element (the raw comtypes pointer) for each node.
    Yields the root first, then children depth-first.
    """
    try:
        from pywinauto import Desktop
        import config

        desktop = Desktop(backend="uia")
        root = desktop.window(handle=hwnd).wrapper_object()

        def _walk(node: Any, depth: int):
            if depth > config.UIA_MAX_DEPTH:
                return
            try:
                raw = node.element_info._element  # raw IUIAutomationElement
                if raw is not None:
                    yield raw
            except Exception:
                pass
            try:
                for child in node.children():
                    yield from _walk(child, depth + 1)
            except Exception:
                pass

        yield from _walk(root, 0)
    except Exception:
        return


def _uia_get_selected(hwnd: int, hint_elem: Any = None) -> str | None:
    """Primary path: scan UIA tree of hwnd for TextPattern selection.

    hint_elem, if provided, is tried first (it was the focused element at wake
    time — most likely candidate).

    Returns the selected string (may be ""), or None if no element in the tree
    supports TextPattern at all.
    """
    if sys.platform != "win32" or not hwnd:
        return None

    try:
        import comtypes
        try:
            comtypes.CoInitialize()
        except Exception:
            pass
    except ImportError:
        return None

    any_pattern_found = False

    # Shortcut: try the hint element (wake-time focused element) first.
    if hint_elem is not None:
        result = _text_from_uia_element(hint_elem)
        if result is not None:
            any_pattern_found = True
            if result:
                return result
            # Pattern supported but nothing selected; still fall through to
            # check other elements (e.g. a document pane behind a toolbar).

    # Full tree scan.
    for raw_elem in _iter_uia_elements(hwnd):
        if raw_elem is hint_elem:
            continue  # already tried
        result = _text_from_uia_element(raw_elem)
        if result is not None:
            any_pattern_found = True
            if result:
                return result

    # Tree had at least one TextPattern element but nothing was selected.
    if any_pattern_found:
        return ""

    # No element in the tree supports TextPattern.
    return None


# ---------------------------------------------------------------------------
# Fallback: synthetic Ctrl+C
# CAVEAT: this path steals focus from the target window temporarily and writes
# to the shared clipboard.  It is intentionally last-resort and must only be
# attempted when the caller has confirmed that TextPattern is unavailable.
# ---------------------------------------------------------------------------

def _copy_fallback(hwnd: int) -> str | None:
    """Focus hwnd, send Ctrl+C, read clipboard, restore prior clipboard.

    Returns the clipboard text (possibly "") or None on failure.
    """
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        import win32gui
        import win32con

        # Save prior clipboard.
        from actions import _read_clipboard, _undo_set_clipboard
        prior = _read_clipboard()

        # Bring target window to foreground.
        try:
            win32gui.SetForegroundWindow(hwnd)
        except Exception:
            # Fallback: AllowSetForegroundWindow + AttachThreadInput trick.
            try:
                fg_tid = ctypes.windll.user32.GetWindowThreadProcessId(
                    win32gui.GetForegroundWindow(), None
                )
                tgt_tid = ctypes.windll.user32.GetWindowThreadProcessId(hwnd, None)
                ctypes.windll.user32.AttachThreadInput(fg_tid, tgt_tid, True)
                ctypes.windll.user32.SetForegroundWindow(hwnd)
                ctypes.windll.user32.AttachThreadInput(fg_tid, tgt_tid, False)
            except Exception:
                return None

        time.sleep(0.05)  # let the window accept focus

        # Send Ctrl+C via keybd_event.
        VK_CONTROL = 0x11
        VK_C = 0x43
        KEYEVENTF_KEYUP = 0x0002
        ctypes.windll.user32.keybd_event(VK_CONTROL, 0, 0, 0)
        ctypes.windll.user32.keybd_event(VK_C, 0, 0, 0)
        ctypes.windll.user32.keybd_event(VK_C, 0, KEYEVENTF_KEYUP, 0)
        ctypes.windll.user32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)

        time.sleep(0.08)  # clipboard write is async

        from actions import _read_clipboard
        result = _read_clipboard()

        # Restore.
        _undo_set_clipboard(prior)

        # If clipboard didn't change from what we saved, nothing was copied.
        if result == prior:
            return None
        return result
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_selected_text(
    target: "PerceptionTarget",
    *,
    use_fallback: bool = False,
) -> str | None:
    """Return the selected text in target's window, or None if nothing is selected.

    Parameters
    ----------
    target:
        The PerceptionTarget for the window to query.  target.focused_element
        (captured at wake time via Task 12) is used as a hint to skip the full
        tree scan when possible.
    use_fallback:
        When True and the primary UIA TextPattern path finds no supporting
        elements, attempt the synthetic Ctrl+C fallback.  Default False because
        the fallback steals focus and writes to the clipboard.

    Returns
    -------
    str   — the selected text (non-empty).
    ""    — TextPattern was found but nothing is currently selected.
    None  — no TextPattern support in the tree AND fallback was not requested
            (or also yielded nothing).
    """
    if sys.platform != "win32" or not target.hwnd or target.is_self:
        return None

    hint = getattr(target, "focused_element", None)
    result = _uia_get_selected(target.hwnd, hint_elem=hint)

    if result is not None:
        # TextPattern path succeeded (even if empty string — nothing selected).
        return result if result else None

    # result is None → no element in the tree supports TextPattern.
    if use_fallback:
        return _copy_fallback(target.hwnd) or None

    return None
