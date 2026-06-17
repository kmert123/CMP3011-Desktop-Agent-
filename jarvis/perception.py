"""Perception ladder: WINDOW -> UIA -> OCR -> VISION."""

from __future__ import annotations

import enum
import logging
import queue
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Optional

import numpy as np

import config

_log = logging.getLogger(__name__)

from screen_model import ScreenElement, make_element_id

if TYPE_CHECKING:
    from perception_policy import PerceptionPolicy
    from perception_target import PerceptionTarget
    from screen_model import ScreenModel


class Rung(enum.IntEnum):
    WINDOW = 0
    UIA = 1
    OCR = 2
    VISION = 3


@dataclass
class PerceptionResult:
    rung: Rung
    text: str = ""
    image: Optional[np.ndarray] = field(default=None, repr=False)
    window_sig: str = ""
    source: str = ""
    ok: bool = False
    screen_model: Optional["ScreenModel"] = field(default=None, repr=False)
    stale: bool = False


# ---------------------------------------------------------------------------
# WINDOW rung
# ---------------------------------------------------------------------------

def _get_process_name(pid: int) -> str:
    try:
        import ctypes
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not h:
            return str(pid)
        buf = ctypes.create_unicode_buffer(260)
        size = ctypes.c_ulong(260)
        ctypes.windll.kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size))
        ctypes.windll.kernel32.CloseHandle(h)
        return Path(buf.value).stem if buf.value else str(pid)
    except Exception:
        return str(pid)


def read_window() -> PerceptionResult:
    """Read foreground window title + process name. Always succeeds instantly."""
    if sys.platform == "win32":
        try:
            import win32gui
            import win32process
            hwnd = win32gui.GetForegroundWindow()
            title = win32gui.GetWindowText(hwnd)
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            process = _get_process_name(pid)
            window_sig = f"{process}:{title}"
            return PerceptionResult(
                rung=Rung.WINDOW, text="", window_sig=window_sig,
                source="window", ok=bool(title),
            )
        except Exception:
            pass
    return PerceptionResult(rung=Rung.WINDOW, text="", window_sig="unknown:", source="window", ok=False)


# ---------------------------------------------------------------------------
# UIA rung
# ---------------------------------------------------------------------------

def _walk_uia(elem, depth: int, lines: list[str], count: list[int]) -> None:
    if depth > config.UIA_MAX_DEPTH or count[0] >= config.UIA_MAX_NODES:
        return
    count[0] += 1

    ctrl_type = name = value = ""
    visible = True
    try:
        ctrl_type = (getattr(elem.element_info, "control_type", "") or "").strip()
        name = (elem.element_info.name or "").strip()
        r = elem.element_info.rectangle
        visible = (r.right - r.left) > 0 and (r.bottom - r.top) > 0
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

    _SKIP = {"Image", "Separator", "Custom"}
    if (name or value) and ctrl_type not in _SKIP:
        parts: list[str] = []
        if ctrl_type:
            parts.append(ctrl_type)
        if name:
            parts.append(f"'{name}'")
        if value:
            parts.append(f"={repr(value[:100])}")
        lines.append(" ".join(parts))

    try:
        for child in elem.children():
            _walk_uia(child, depth + 1, lines, count)
    except Exception:
        pass


def read_uia(window_sig: str = "") -> PerceptionResult:
    """Walk the UIA accessibility tree of the foreground window."""
    try:
        import win32gui
        from pywinauto import Desktop

        hwnd = win32gui.GetForegroundWindow()
        if not hwnd:
            return PerceptionResult(rung=Rung.UIA, window_sig=window_sig, source="uia", ok=False)

        desktop = Desktop(backend="uia")
        wrapper = desktop.window(handle=hwnd).wrapper_object()

        lines: list[str] = []
        _walk_uia(wrapper, 0, lines, [0])
        text = "\n".join(lines)

        return PerceptionResult(rung=Rung.UIA, text=text, window_sig=window_sig, source="uia", ok=bool(text.strip()))
    except Exception:
        return PerceptionResult(rung=Rung.UIA, window_sig=window_sig, source="uia", ok=False)


# ---------------------------------------------------------------------------
# OCR rung
# ---------------------------------------------------------------------------

def read_ocr(window_sig: str = "", target: "PerceptionTarget | None" = None) -> PerceptionResult:
    """OCR via the full preprocessing adapter (upscale, dark-mode invert, CLAHE, PSM)."""
    try:
        from adapters.ocr_adapter import read_ocr as _adapter_ocr

        if target is not None:
            from capture import capture_target
            crop, origin, _dpi, stale = capture_target(target)
        else:
            from capture import capture_primary_monitor
            crop = capture_primary_monitor()
            origin = (0, 0)
            stale = False

        elements = _adapter_ocr(crop, origin)
        text = "\n".join(e.text for e in elements if e.text)
        return PerceptionResult(
            rung=Rung.OCR,
            text=text,
            window_sig=window_sig,
            source="ocr",
            ok=bool(text.strip()),
            stale=stale,
        )
    except Exception:
        return PerceptionResult(rung=Rung.OCR, window_sig=window_sig, source="ocr", ok=False)


# ---------------------------------------------------------------------------
# VISION rung
# ---------------------------------------------------------------------------

def read_vision(
    window_sig: str = "",
    target: "PerceptionTarget | None" = None,
    *,
    describe: bool = False,
    ask_elements: bool = False,
) -> PerceptionResult:
    """Capture the target window crop; optionally describe it via the configured VLM.

    Parameters
    ----------
    describe      : If True, call ask_vlm for a natural-language description.
    ask_elements  : If True, call ask_vlm with structured element detection.
                    Populates result.text with JSON-parsed label list summary.

    The result.source is tagged with the VLM backend that ran ("moondream" or
    "gemini") so the calibration table can apply per-model reliability weights.
    """
    try:
        from capture import capture_full_screen, capture_target

        cropped, _origin, _dpi, _stale = (
            capture_target(target) if target is not None else capture_full_screen()
        )

        source = "vision"
        text = ""

        if (describe or ask_elements) and not _stale:
            try:
                from adapters.vision_adapter import ask_vlm
                vlm_result = ask_vlm(cropped, "", ask_elements=ask_elements)
                if vlm_result.ok:
                    source = vlm_result.backend  # "moondream" | "gemini"
                    if ask_elements and vlm_result.refs:
                        text = "; ".join(
                            f"{r.label} ({r.role})" for r in vlm_result.refs
                        )
                    else:
                        text = vlm_result.text
            except Exception:
                pass

        return PerceptionResult(
            rung=Rung.VISION, text=text, image=cropped,
            window_sig=window_sig, source=source, ok=not _stale,
        )
    except Exception:
        return PerceptionResult(rung=Rung.VISION, window_sig=window_sig, source="vision", ok=False)


# ---------------------------------------------------------------------------
# Ladder
# ---------------------------------------------------------------------------

def run_ladder(
    entry: Rung,
    frame: np.ndarray | None = None,  # unused; kept for API compatibility
    target: "PerceptionTarget | None" = None,
    fallback_sig: str = "",
    use_fusion: bool = False,
    policy: "PerceptionPolicy | None" = None,
    trace=None,
) -> PerceptionResult:
    """Run the perception ladder from `entry` down, returning the first useful result.

    When target.is_self is True (Jarvis captured its own window), WINDOW and UIA rungs
    are skipped. OCR/VISION fall back to full-screen via capture_target's is_self guard.
    fallback_sig is used as window_sig in that case (from session.recent_windows).

    policy — if supplied (derived from target.app_class via perception_policy.policy_for),
    controls which adapters run and whether UIA is skipped for Electron/game targets.
    When policy is None the behaviour is unchanged from the old implementation.
    """
    is_self = target is not None and getattr(target, "is_self", False)

    if is_self:
        window_sig = fallback_sig
        if entry == Rung.WINDOW:
            return PerceptionResult(rung=Rung.WINDOW, window_sig=fallback_sig, source="window", ok=False)
    else:
        win = read_window()
        window_sig = win.window_sig
        if entry == Rung.WINDOW:
            return win

    if use_fusion and target is not None and not is_self:
        from adapters.uia_adapter import read_uia as _uia_adapt
        from adapters.ocr_adapter import read_ocr as _ocr_adapt
        from adapters.cv_adapter import read_cv as _cv_adapt
        from capture import capture_target as _cap
        from fusion import fuse as _fuse

        crop, origin, _dpi, stale = _cap(target)

        # Respect policy: skip adapters that the app class doesn't benefit from.
        uia_elems = _uia_adapt(target) if (policy is None or policy.run_uia) else []
        ocr_elems = _ocr_adapt(crop, origin) if (policy is None or policy.run_ocr) else []
        cv_elems  = _cv_adapt(crop, origin)  if (policy is None or policy.run_cv)  else []

        sm = _fuse(target, uia_elems, ocr_elems, cv_elems, crop, stale=stale)

        if trace is not None:
            trace.record(
                "PERCEPTION_DETAIL",
                uia_count=len(uia_elems),
                ocr_count=len(ocr_elems),
                cv_count=len(cv_elems),
                fused_count=len(sm.elements),
                full_text_chars=len(sm.full_text or ""),
                uia_sample=[e.text for e in uia_elems if e.text][:10],
                ocr_sample=[e.text for e in ocr_elems if e.text][:10],
                fused_text_sample=(sm.full_text or "")[:400],
            )

        # --- P9: content-region re-OCR at higher scale + sparse PSM ---
        # After fuse() has resolved in_content_region, re-OCR just the content
        # rectangle at CONTENT_REOCR_SCALE / OCR_PSM_CONTENT and merge the new
        # elements back in. Only runs when:
        #   - CONTENT_REOCR master flag is on
        #   - not stale (no useful pixels to re-read)
        #   - policy permits OCR (same gate as the first pass)
        #   - at least one element is NOT in the content region (i.e. chrome was
        #     actually stripped — otherwise the content region == full window and
        #     a second pass over the same area is wasteful)
        if (
            config.CONTENT_REOCR
            and not stale
            and (policy is None or policy.run_ocr)
            and any(not e.in_content_region for e in sm.elements)
        ):
            from content_region import resolve_content_region
            content_bbox = resolve_content_region(target, crop, sm.elements, origin)
            cx, cy, cw, ch = content_bbox
            # Clamp to the captured crop boundaries (crop is in window-local coords).
            crop_h, crop_w = crop.shape[:2]
            # content_bbox is in virtual-desktop coords; subtract the window origin
            # (origin) to get crop-local coords.
            ox_win, oy_win = origin
            local_x = max(0, cx - ox_win)
            local_y = max(0, cy - oy_win)
            local_x2 = min(crop_w, cx - ox_win + cw)
            local_y2 = min(crop_h, cy - oy_win + ch)
            if local_x2 > local_x and local_y2 > local_y:
                content_crop = crop[local_y:local_y2, local_x:local_x2]
                content_origin = (cx, cy)  # virtual-desktop origin for bbox conversion
                reocr_elems = _ocr_adapt(
                    content_crop,
                    content_origin,
                    scale=config.CONTENT_REOCR_SCALE,
                    psm=config.OCR_PSM_CONTENT,
                    min_conf=config.OCR_MIN_CONF_CONTENT,
                    token_conf_min=config.OCR_TOKEN_CONF_MIN_CONTENT,
                )
                if reocr_elems:
                    # Merge re-OCR elements into the existing model via a second fuse.
                    # Pass empty UIA/CV so only the new OCR evidence is added on top.
                    sm = _fuse(
                        target,
                        list(sm.elements),   # existing elements become the UIA backbone
                        reocr_elems,
                        [],
                        crop,
                        stale=stale,
                    )

        import debug_overlay as _dbg
        _dbg.save_overlay(crop, sm, origin)

        # G1C: auto-escalate on thin reads before returning (one step, never loops).
        text_elems = [e for e in sm.elements if e.text]
        is_thin = (
            len(sm.full_text.strip()) < config.THIN_TEXT_CHAR_FLOOR
            and len(text_elems) < config.THIN_TEXT_ELEM_FLOOR
        )
        if is_thin and entry < Rung.VISION and (policy is None or policy.run_ocr):
            _log.info(
                "thin read (%d chars, %d elems) — auto-escalating from %s",
                len(sm.full_text.strip()), len(text_elems), entry.name,
            )
            next_rung = Rung(entry + 1)
            _next_ocr = _ocr_adapt(crop, origin) if next_rung == Rung.OCR else []
            _next_vis_elems: list = []
            if next_rung == Rung.VISION and (policy is None or policy.run_vision):
                try:
                    from adapters.vision_adapter import ask_vlm as _ask_vlm
                    vlm_r = _ask_vlm(crop, "", ask_elements=True)
                    if vlm_r.ok and vlm_r.refs:
                        _next_vis_elems = list(vlm_r.refs)
                except Exception:
                    pass
            if _next_ocr or _next_vis_elems:
                sm_esc = _fuse(target, list(sm.elements), _next_ocr, cv_elems, crop, stale=stale)
                if len(sm_esc.full_text.strip()) > len(sm.full_text.strip()):
                    sm = sm_esc
                    _dbg.save_overlay(crop, sm, origin)

        # G1E: VISION thin-read fallback — runs after the one-step G1C escalation when the
        # result is still thin AND the app class is in the hard-app allow-list.
        # Only fires once per run_ladder call (no loop).  Even when the VLM returns no
        # structured refs, attach_vision_image is set so the answer path receives the raw
        # screenshot (Task 4 / _build_initial_contents uses it for the local-VLM description
        # or Gemini multimodal call).
        # Rung label stays as `entry` (the escalation is visible in the trace via
        # vision_fallback=True) so existing telemetry thresholds are unaffected.
        attach_vision_image = (entry == Rung.VISION)
        if config.VISION_THIN_FALLBACK and not attach_vision_image and (policy is None or policy.run_vision):
            _text_elems_post = [e for e in sm.elements if e.text]
            _still_thin = (
                len(sm.full_text.strip()) < config.THIN_TEXT_CHAR_FLOOR
                and len(_text_elems_post) < config.THIN_TEXT_ELEM_FLOOR
            )
            _app_cls = getattr(getattr(target, "app_class", None), "value", None) or "unknown"
            if _still_thin and _app_cls in config.VISION_THIN_FALLBACK_APP_CLASSES:
                _vision_refs_count = 0
                try:
                    from adapters.vision_adapter import ask_vlm as _ask_vlm_g1e
                    _vlm_r = _ask_vlm_g1e(crop, "", ask_elements=True)
                    if _vlm_r.ok and _vlm_r.refs:
                        _vision_refs_count = len(_vlm_r.refs)
                        ox, oy = origin
                        _vis_elems = []
                        for _ref in _vlm_r.refs:
                            _rx, _ry, _rw, _rh = _ref.bbox
                            _vis_elems.append(ScreenElement(
                                id=make_element_id(_ref.role, _ref.label, (_rx + ox, _ry + oy, _rw, _rh)),
                                role=_ref.role,
                                text=_ref.label,
                                bbox=(_rx + ox, _ry + oy, _rw, _rh),
                                source="vision",
                                confidence=_ref.confidence,
                                invokable=False,
                            ))
                        if _vis_elems:
                            sm_v = _fuse(target, list(sm.elements), _vis_elems, [], crop, stale=stale)
                            if len(sm_v.full_text.strip()) > len(sm.full_text.strip()):
                                sm = sm_v
                                _dbg.save_overlay(crop, sm, origin)
                except Exception:
                    pass
                # Always attach the screenshot when the fallback fired, even with no refs.
                attach_vision_image = True
                _log.info(
                    "G1E VISION thin-read fallback fired: app_class=%s refs=%d",
                    _app_cls, _vision_refs_count,
                )
                if trace is not None:
                    trace.record("PERCEPTION_DETAIL", vision_fallback=True, vision_refs=_vision_refs_count)

        tree_text = sm.to_prompt_block()
        return PerceptionResult(
            rung=entry,
            text=tree_text,
            image=crop if attach_vision_image else None,
            window_sig=window_sig,
            source="fusion",
            ok=bool(sm.full_text.strip()) or attach_vision_image,
            screen_model=sm,
            stale=stale,
        )

    # Plain (non-fusion) ladder path — honour policy.ladder_entry to skip UIA
    # for Electron/game targets and go directly to the cheapest useful rung.
    effective_entry = entry
    if policy is not None:
        _rung_map = {"UIA": Rung.UIA, "OCR": Rung.OCR, "VISION": Rung.VISION}
        policy_rung = _rung_map.get(policy.ladder_entry, Rung.UIA)
        if policy_rung > effective_entry:
            effective_entry = policy_rung

    _providers = [
        (Rung.UIA,    lambda: read_uia(window_sig)),
        (Rung.OCR,    lambda: read_ocr(window_sig, target)),
        (Rung.VISION, lambda: read_vision(window_sig, target, ask_elements=True)),
    ]

    for rung, provider in _providers:
        if rung < effective_entry:
            continue
        if is_self and rung == Rung.UIA:
            continue  # UIA would walk the Jarvis window itself
        # Policy: skip disabled adapters even in ladder mode.
        if policy is not None:
            if rung == Rung.UIA    and not policy.run_uia:
                continue
            if rung == Rung.OCR    and not policy.run_ocr:
                continue
            if rung == Rung.VISION and not policy.run_vision:
                continue
        try:
            result = provider()
        except Exception:
            result = PerceptionResult(rung=rung, window_sig=window_sig, source=rung.name.lower(), ok=False)

        if rung == Rung.VISION or (result.ok and result.text.strip()):
            return result

    return PerceptionResult(rung=effective_entry, window_sig=window_sig, source="fallback", ok=False)


if __name__ == "__main__":
    print("WINDOW:", run_ladder(Rung.WINDOW))
    print()
    r = run_ladder(Rung.UIA)
    print(f"UIA ok={r.ok} source={r.source} text[:200]={r.text[:200]!r}")
    print()
    r = run_ladder(Rung.OCR)
    print(f"OCR ok={r.ok} text[:200]={r.text[:200]!r}")
    print()
    r = run_ladder(Rung.VISION)
    print(f"VISION ok={r.ok} image={'yes' if r.image is not None else 'no'}")
