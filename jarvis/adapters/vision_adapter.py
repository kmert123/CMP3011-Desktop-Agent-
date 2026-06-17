"""Pluggable VLM adapter — standardised contract for the VISION perception rung.

VlmResult contract
------------------
Every VLM call returns a VlmResult.  Callers must handle both cases:

    result.text       — natural-language answer (may be empty when refs present)
    result.refs       — list of ElementRef (marker ids + bboxes) for grounding
    result.ok         — False if the backend failed completely

ElementRef maps to a ScreenElement and is consumed by:
    - set_of_marks.ask_som_marker (SoM click path)
    - gemini.ask_stream (model-escalation need_image path)

Backend selection (config.VISION_MODEL)
-----------------------------------------
"moondream"   — Ollama/moondream2 via local_vision (default, low-resource).
                Reliability = 0.5 (calibration table downgraded from 0.7).
"gemini"      — Gemini multimodal API.  Used for grounding-critical queries.
"auto"        — moondream first; falls back to Gemini if moondream returns no
                refs and the query is grounding-critical (VISION_BACKEND="gemini"
                or caller passes force_gemini=True).

Structured element detection
------------------------------
When ask_elements=True the adapter asks the VLM to enumerate visible UI
elements and return JSON.  The JSON is parsed into a list[ElementRef].
If parsing fails the raw text answer is kept and refs=[].
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Optional, TYPE_CHECKING

import numpy as np

import config

if TYPE_CHECKING:
    pass

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Standardised output types
# ---------------------------------------------------------------------------

@dataclass
class ElementRef:
    """A VLM-detected UI element with approximate bounding box."""
    label: str
    role: str = "unknown"
    bbox: tuple[int, int, int, int] = (0, 0, 0, 0)  # (x, y, w, h) in image-local coords
    confidence: float = 0.7


@dataclass
class VlmResult:
    """Unified return type from any VLM backend."""
    ok: bool
    text: str = ""
    refs: list[ElementRef] = field(default_factory=list)
    backend: str = "unknown"
    error: str = ""


# ---------------------------------------------------------------------------
# JSON schema for structured element detection
# ---------------------------------------------------------------------------

_ELEMENT_DETECT_PROMPT = (
    "Enumerate all visible interactive UI elements in this screenshot.\n"
    "Return ONLY a JSON array of objects, no explanation, no markdown fences.\n"
    'Each object has keys: "label" (string), "role" (string), '
    '"bbox" ([x, y, w, h] in pixels from top-left of image).\n'
    "Examples of roles: button, link, checkbox, input, menu, tab, icon, label.\n"
    "If you cannot detect elements, return []."
)

_ANSWER_PROMPT_TEMPLATE = (
    "Screenshot of the user's active window is attached.\n\n"
    "User question: {question}\n\n"
    "Answer concisely (2-4 sentences). Reference specific UI elements when relevant."
)

# Prompt used when the caller wants a rich description of the screen for grounding
# a text-only LLM (no multimodal capability).  Requests all the visual attributes
# the text model cannot infer on its own: layout, colors, shapes, icons.
_SCREEN_DESCRIPTION_PROMPT = (
    "You are analyzing a screenshot of a user's active window. "
    "Describe the screen thoroughly so that a text-only AI can answer questions about it. "
    "Include:\n"
    "- The app/window type and overall layout\n"
    "- All visible text content (headings, labels, body text, error messages)\n"
    "- UI elements: buttons, inputs, dropdowns, checkboxes, tabs — their labels, states "
    "(enabled/disabled/checked), and approximate positions (top-left, center, bottom-right, etc.)\n"
    "- Colors: background color, accent colors, any colored icons/buttons/status indicators\n"
    "- Shapes and icons: describe any non-text icons or graphical elements\n"
    "- Anything visually prominent or unusual\n"
    "Be specific and factual. Do not speculate beyond what is visible."
)


def _parse_element_json(raw: str) -> list[ElementRef]:
    """Extract a JSON array from raw VLM output and parse into ElementRef list."""
    # Strip markdown fences if present.
    raw = re.sub(r"```(?:json)?", "", raw).strip().rstrip("`").strip()
    try:
        items = json.loads(raw)
        if not isinstance(items, list):
            return []
        refs: list[ElementRef] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            label = str(item.get("label", "")).strip()
            if not label:
                continue
            role = str(item.get("role", "unknown"))
            raw_bbox = item.get("bbox", [0, 0, 0, 0])
            try:
                bbox = tuple(int(v) for v in raw_bbox[:4])
                if len(bbox) < 4:
                    bbox = (0, 0, 0, 0)
            except (TypeError, ValueError):
                bbox = (0, 0, 0, 0)
            refs.append(ElementRef(label=label, role=role, bbox=bbox))
        return refs
    except (json.JSONDecodeError, TypeError):
        return []


# ---------------------------------------------------------------------------
# Backend: moondream via local_vision
# ---------------------------------------------------------------------------

def _vlm_moondream(
    bgr: np.ndarray,
    prompt: str,
    *,
    ask_elements: bool = False,
) -> VlmResult:
    try:
        import local_vision

        actual_prompt = _ELEMENT_DETECT_PROMPT if ask_elements else prompt
        # local_vision.describe_image accepts PIL images or numpy; convert to PIL.
        import cv2
        from PIL import Image as PilImage
        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        pil_img = PilImage.fromarray(rgb)
        raw = local_vision.describe_image(pil_img, actual_prompt)

        if ask_elements:
            refs = _parse_element_json(raw)
            return VlmResult(ok=True, text=raw if not refs else "", refs=refs, backend="moondream")

        return VlmResult(ok=True, text=raw, backend="moondream")
    except Exception as exc:
        _log.debug("Moondream VLM failed: %s", exc)
        return VlmResult(ok=False, error=str(exc), backend="moondream")


# ---------------------------------------------------------------------------
# Backend: Gemini multimodal
# ---------------------------------------------------------------------------

def _vlm_gemini(
    bgr: np.ndarray,
    prompt: str,
    *,
    ask_elements: bool = False,
) -> VlmResult:
    if not config.GEMINI_API_KEY:
        return VlmResult(ok=False, error="GEMINI_API_KEY not set", backend="gemini")
    try:
        import io
        import cv2
        from PIL import Image as PilImage
        from google import genai
        from google.genai import types

        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        pil_img = PilImage.fromarray(rgb)
        buf = io.BytesIO()
        pil_img.save(buf, format="PNG")
        img_part = types.Part.from_bytes(data=buf.getvalue(), mime_type="image/png")

        actual_prompt = _ELEMENT_DETECT_PROMPT if ask_elements else prompt
        client = genai.Client(api_key=config.GEMINI_API_KEY)
        resp = client.models.generate_content(
            model=config.GEMINI_MODEL,
            contents=[
                types.Content(role="user", parts=[
                    types.Part.from_text(actual_prompt),
                    img_part,
                ])
            ],
        )
        raw = (resp.text or "").strip()

        if ask_elements:
            refs = _parse_element_json(raw)
            return VlmResult(ok=True, text=raw if not refs else "", refs=refs, backend="gemini")

        return VlmResult(ok=True, text=raw, backend="gemini")
    except Exception as exc:
        _log.debug("Gemini VLM failed: %s", exc)
        return VlmResult(ok=False, error=str(exc), backend="gemini")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def ask_vlm(
    bgr: np.ndarray,
    question: str,
    *,
    ask_elements: bool = False,
    force_gemini: bool = False,
    describe_screen: bool = False,
) -> VlmResult:
    """Ask the configured VLM about *bgr* (BGR numpy array).

    Parameters
    ----------
    bgr            : BGR screenshot crop.
    question       : Natural-language question or task description.
    ask_elements   : If True, request structured element detection JSON instead
                     of a plain text answer.  Returns VlmResult.refs on success.
    force_gemini   : Bypass VISION_MODEL and always use Gemini.  Used by the
                     SoM click path when grounding accuracy matters.
    describe_screen: If True, use a rich screen-description prompt suitable for
                     grounding a text-only LLM (layout, colors, shapes, text).
                     Overrides ask_elements when True.

    Returns
    -------
    VlmResult with .text (answer) and/or .refs (element list).
    """
    model = config.VISION_MODEL

    if describe_screen:
        prompt = _SCREEN_DESCRIPTION_PROMPT
        ask_elements = False
    else:
        prompt = (
            _ELEMENT_DETECT_PROMPT if ask_elements
            else _ANSWER_PROMPT_TEMPLATE.format(question=question)
        )

    if force_gemini or model == "gemini":
        result = _vlm_gemini(bgr, prompt, ask_elements=ask_elements)
        if not result.ok and model != "gemini":
            result = _vlm_moondream(bgr, prompt, ask_elements=ask_elements)
        return result

    if model == "moondream":
        result = _vlm_moondream(bgr, prompt, ask_elements=ask_elements)
        if not result.ok:
            # Hard fallback to Gemini only if key is available.
            if config.GEMINI_API_KEY:
                result = _vlm_gemini(bgr, prompt, ask_elements=ask_elements)
        return result

    # model == "auto": moondream first; Gemini fallback when grounding matters.
    result = _vlm_moondream(bgr, prompt, ask_elements=ask_elements)
    if not result.ok or (ask_elements and not result.refs and config.GEMINI_API_KEY):
        gemini_result = _vlm_gemini(bgr, prompt, ask_elements=ask_elements)
        if gemini_result.ok:
            return gemini_result
    return result
