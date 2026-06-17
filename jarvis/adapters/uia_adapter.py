"""UIA perception adapter: walk the accessibility tree of a target window."""

from __future__ import annotations

from typing import TYPE_CHECKING

import config
from screen_model import ScreenElement, make_element_id

if TYPE_CHECKING:
    from perception_target import PerceptionTarget

_SKIP_ROLES = {"Image", "Separator", "Custom"}
_INVOKABLE_PATTERNS = {"InvokePattern", "TogglePattern", "ValuePattern"}


def _is_invokable(elem) -> bool:
    try:
        patterns = set(elem.element_info.patterns or [])
        if patterns & _INVOKABLE_PATTERNS:
            return True
    except Exception:
        pass
    try:
        return bool(elem.is_editable())
    except Exception:
        pass
    return False


def _walk(elem, depth: int, count: list[int], out: list[ScreenElement]) -> None:
    if depth > config.UIA_MAX_DEPTH or count[0] >= config.UIA_MAX_NODES:
        return
    count[0] += 1

    ctrl_type = name = value = ""
    bbox = (0, 0, 0, 0)
    visible = False

    try:
        ctrl_type = (getattr(elem.element_info, "control_type", "") or "").strip()
        name = (elem.element_info.name or "").strip()
        r = elem.element_info.rectangle
        w = r.right - r.left
        h = r.bottom - r.top
        visible = w > 0 and h > 0
        if visible:
            # IUIAutomationElement::get_CurrentBoundingRectangle returns physical screen
            # pixels regardless of caller DPI context — these ARE virtual-desktop pixels.
            bbox = (r.left, r.top, w, h)
    except Exception:
        pass

    if not visible:
        return

    try:
        wt = (elem.window_text() or "").strip()
        if wt != name:
            value = wt
    except Exception:
        pass

    text = name or value
    if text and ctrl_type not in _SKIP_ROLES:
        out.append(ScreenElement(
            id=make_element_id(ctrl_type, text, bbox),
            role=ctrl_type or "Unknown",
            text=text,
            bbox=bbox,
            source="uia",
            confidence=0.9,
            invokable=_is_invokable(elem),
            handle=elem,
        ))

    try:
        for child in elem.children():
            _walk(child, depth + 1, count, out)
    except Exception:
        pass


def read_uia(target: "PerceptionTarget") -> list[ScreenElement]:
    """Walk the UIA tree of target.hwnd; return [] on any failure."""
    if target.is_self or not target.hwnd:
        return []
    try:
        from pywinauto import Desktop

        desktop = Desktop(backend="uia")
        wrapper = desktop.window(handle=target.hwnd).wrapper_object()
        out: list[ScreenElement] = []
        _walk(wrapper, 0, [0], out)
        return out
    except Exception:
        return []
