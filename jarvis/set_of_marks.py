"""Set-of-Marks (SoM) overlay and VLM matching.

When UIA grounding fails to find an invokable handle (Electron, game, PDF viewer),
SoM renders numbered circles on each detected element's centre and asks the VLM
which marker best matches the user's target label.  The matched marker's centre
point is then used as the click coordinate.

Public API
----------
render_som(bgr_crop, elements, origin)
    -> (annotated_bgr, markers: dict[int, ScreenElement])

ask_som_marker(annotated_bgr, label, *, session=None)
    -> int | None   (1-based marker number, or None if the VLM gave no answer)
"""

from __future__ import annotations

import io
import json
import logging
import re
from typing import TYPE_CHECKING, Optional

import cv2
import numpy as np

import config

if TYPE_CHECKING:
    from screen_model import ScreenElement
    from session_context import SessionContext

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Visual parameters for the marker overlay
# ---------------------------------------------------------------------------

_MARKER_RADIUS   = 14      # circle radius in px
_MARKER_THICKNESS = -1     # filled circle
_BG_COLOR  = (30,  30, 220)   # BGR: red-ish
_FG_COLOR  = (255, 255, 255)  # white text
_FONT      = cv2.FONT_HERSHEY_SIMPLEX
_FONT_SCALE = 0.45
_FONT_THICK = 1


# ---------------------------------------------------------------------------
# render_som
# ---------------------------------------------------------------------------

def render_som(
    bgr_crop: np.ndarray,
    elements: "list[ScreenElement]",
    origin: tuple[int, int],
) -> tuple[np.ndarray, dict[int, "ScreenElement"]]:
    """Draw numbered circles on each element's centre; return annotated image + index map.

    Parameters
    ----------
    bgr_crop : BGR numpy array of the target-window crop (screen-coords).
    elements : ScreenElements to mark.  Only elements with a non-zero bbox are drawn.
    origin   : (ox, oy) top-left of crop in virtual-desktop pixel space.
               Used to convert screen-coord bboxes to crop-local coords.

    Returns
    -------
    annotated_bgr : copy of bgr_crop with markers drawn.
    markers       : {1-based marker number → ScreenElement} for all drawn elements.
    """
    ox, oy = origin
    annotated = bgr_crop.copy()
    markers: dict[int, "ScreenElement"] = {}
    idx = 1

    for elem in elements:
        bx, by, bw, bh = elem.bbox
        if bw <= 0 or bh <= 0:
            continue

        # Convert from screen coords to crop-local coords.
        cx = bx - ox + bw // 2
        cy = by - oy + bh // 2

        # Skip if outside the crop bounds.
        h, w = annotated.shape[:2]
        if not (0 <= cx < w and 0 <= cy < h):
            continue

        # Draw circle background + number label.
        cv2.circle(annotated, (cx, cy), _MARKER_RADIUS, _BG_COLOR, _MARKER_THICKNESS)
        label = str(idx)
        (tw, th), _ = cv2.getTextSize(label, _FONT, _FONT_SCALE, _FONT_THICK)
        tx = cx - tw // 2
        ty = cy + th // 2
        cv2.putText(annotated, label, (tx, ty), _FONT, _FONT_SCALE, _FG_COLOR, _FONT_THICK, cv2.LINE_AA)

        markers[idx] = elem
        idx += 1

    return annotated, markers


# ---------------------------------------------------------------------------
# ask_som_marker — VLM disambiguation
# ---------------------------------------------------------------------------

def _encode_png_part(bgr: np.ndarray) -> bytes:
    """Encode BGR array to PNG bytes."""
    ok, buf = cv2.imencode(".png", bgr)
    if not ok:
        raise RuntimeError("cv2.imencode failed")
    return buf.tobytes()


def _gemini_ask_marker(bgr: np.ndarray, label: str, n_markers: int) -> int | None:
    """Send the annotated screenshot to Gemini and ask which marker number matches label."""
    if not config.GEMINI_API_KEY:
        return None
    try:
        from PIL import Image as PilImage
        from google import genai
        from google.genai import types

        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        pil_img = PilImage.fromarray(rgb)
        buf = io.BytesIO()
        pil_img.save(buf, format="PNG")
        img_part = types.Part.from_bytes(data=buf.getvalue(), mime_type="image/png")

        prompt = (
            f"The image shows a screenshot with {n_markers} numbered red circle markers "
            f"overlaid on UI elements.\n"
            f"Which marker number (1–{n_markers}) best corresponds to the element "
            f"labelled: '{label}'?\n"
            f"Respond with ONLY a single integer (e.g. '3'). "
            f"If none match, respond with '0'."
        )

        client = genai.Client(api_key=config.GEMINI_API_KEY)
        resp = client.models.generate_content(
            model=config.GEMINI_MODEL,
            contents=[
                types.Content(role="user", parts=[
                    types.Part.from_text(prompt),
                    img_part,
                ])
            ],
        )
        raw = (resp.text or "").strip()
        m = re.search(r"\d+", raw)
        if m:
            n = int(m.group())
            return n if 1 <= n <= n_markers else None
        return None
    except Exception as exc:
        _log.debug("Gemini SoM ask failed: %s", exc)
        return None


def _local_ask_marker(bgr: np.ndarray, label: str, n_markers: int) -> int | None:
    """Send the annotated screenshot to the local VLM (Moondream/Ollama)."""
    try:
        import local_vision  # type: ignore[import]
        from PIL import Image as PilImage

        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        pil_img = PilImage.fromarray(rgb)

        prompt = (
            f"This image has {n_markers} numbered red circle markers on UI elements. "
            f"Which marker number best matches: '{label}'? "
            f"Reply with only the number (or 0 if none match)."
        )
        raw = local_vision.describe_image(pil_img, prompt)
        m = re.search(r"\d+", raw or "")
        if m:
            n = int(m.group())
            return n if 1 <= n <= n_markers else None
        return None
    except Exception as exc:
        _log.debug("Local VLM SoM ask failed: %s", exc)
        return None


def ask_som_marker(
    annotated_bgr: np.ndarray,
    label: str,
    *,
    n_markers: int,
    session: "Optional[SessionContext]" = None,
) -> int | None:
    """Ask the configured VLM which numbered marker best matches *label*.

    Returns a 1-based marker number, or None if the model gave no usable answer.
    Routes to Gemini when VISION_BACKEND="gemini" or as primary attempt;
    falls back to the local VLM when Gemini is unavailable.
    """
    if config.VISION_BACKEND == "local":
        result = _local_ask_marker(annotated_bgr, label, n_markers)
        if result is None:
            result = _gemini_ask_marker(annotated_bgr, label, n_markers)
    else:
        result = _gemini_ask_marker(annotated_bgr, label, n_markers)
        if result is None:
            result = _local_ask_marker(annotated_bgr, label, n_markers)

    _log.debug("SoM ask '%s' → marker %s (of %d)", label, result, n_markers)
    return result


# ---------------------------------------------------------------------------
# Coordinate helper
# ---------------------------------------------------------------------------

def marker_screen_center(
    marker_num: int,
    markers: dict[int, "ScreenElement"],
) -> tuple[int, int] | None:
    """Return the screen-coordinate centre of the element at *marker_num*, or None."""
    elem = markers.get(marker_num)
    if elem is None:
        return None
    bx, by, bw, bh = elem.bbox
    return bx + bw // 2, by + bh // 2
