"""OCR perception adapter: group Tesseract tokens into line-level ScreenElements.

Preprocessing pipeline (applied before Tesseract):
  1. Upscale by OCR_SCALE (INTER_CUBIC) — improves word/character recognition at screen DPI.
  2. Convert to grayscale.
  3. Dark-mode inversion — if mean luminance < OCR_DARK_THRESHOLD, invert so text is dark-on-light.
  4. CLAHE contrast normalisation.

All returned ScreenElement bboxes are in virtual-desktop pixel coords:
  bbox = (x_scaled / OCR_SCALE + ox, y_scaled / OCR_SCALE + oy, w_scaled / OCR_SCALE, h_scaled / OCR_SCALE)
"""

from __future__ import annotations

import numpy as np
import cv2

import config
from screen_model import ScreenElement, make_element_id

# Per-token confidence threshold applied before line grouping.
_TOKEN_CONF_MIN = 30


def _preprocess(crop: np.ndarray, scale: float | None = None) -> tuple[np.ndarray, float]:
    """Return (processed_image, scale) ready for Tesseract.

    scale is the factor by which the image was upsampled; divide Tesseract bboxes by it
    to recover original-resolution coordinates.
    """
    if scale is None:
        scale = float(config.OCR_SCALE)

    # Step 1: upscale
    h, w = crop.shape[:2]
    new_w = max(1, int(round(w * scale)))
    new_h = max(1, int(round(h * scale)))
    upscaled = cv2.resize(crop, (new_w, new_h), interpolation=cv2.INTER_CUBIC)

    # Step 2: grayscale
    gray = cv2.cvtColor(upscaled, cv2.COLOR_BGR2GRAY)

    # Step 3: dark-mode inversion
    if gray.mean() < config.OCR_DARK_THRESHOLD:
        gray = cv2.bitwise_not(gray)

    # Step 4: CLAHE contrast normalisation
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    gray = clahe.apply(gray)

    return gray, scale


def read_ocr(
    crop: np.ndarray,
    origin: tuple[int, int],
    scale: float | None = None,
    psm: int | None = None,
    min_conf: float | None = None,
    token_conf_min: int | None = None,
) -> list[ScreenElement]:
    """Run Tesseract on crop; return line-level ScreenElements in screen coords.

    origin is (ox, oy) — the top-left of crop in virtual-desktop pixel coordinates.
    scale overrides OCR_SCALE; psm overrides OCR_PSM. Both default to config values.
    min_conf overrides OCR_MIN_CONF (line-level floor); token_conf_min overrides
    _TOKEN_CONF_MIN (per-token floor). Pass softer values for content-region passes.
    Returns [] on any failure.
    """
    effective_token_min = token_conf_min if token_conf_min is not None else _TOKEN_CONF_MIN
    effective_min_conf  = min_conf       if min_conf       is not None else config.OCR_MIN_CONF

    try:
        import pytesseract
        from pytesseract import Output

        if config.TESSERACT_PATH:
            pytesseract.pytesseract.tesseract_cmd = config.TESSERACT_PATH

        processed, scale = _preprocess(crop, scale)
        effective_psm = int(psm if psm is not None else config.OCR_PSM)
        custom_config = f"--psm {effective_psm}"
        data = pytesseract.image_to_data(processed, output_type=Output.DICT, config=custom_config)
    except Exception:
        return []

    ox, oy = origin
    n = len(data["text"])

    # Group accepted tokens by (block_num, par_num, line_num).
    lines: dict[tuple[int, int, int], list[int]] = {}
    for i in range(n):
        try:
            conf = int(data["conf"][i])
        except (ValueError, TypeError):
            conf = -1
        if conf < effective_token_min or not data["text"][i].strip():
            continue
        key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        lines.setdefault(key, []).append(i)

    out: list[ScreenElement] = []
    for indices in lines.values():
        tokens = [data["text"][i].strip() for i in indices if data["text"][i].strip()]
        if not tokens:
            continue

        confs = [int(data["conf"][i]) for i in indices]
        mean_conf = sum(confs) / len(confs) / 100.0
        if mean_conf < effective_min_conf:
            continue

        # Union of scaled token bboxes, divided back to original resolution, then offset to screen coords.
        xs  = [data["left"][i]                    for i in indices]
        ys  = [data["top"][i]                     for i in indices]
        x2s = [data["left"][i] + data["width"][i] for i in indices]
        y2s = [data["top"][i]  + data["height"][i] for i in indices]

        bx = int(min(xs)  / scale) + ox
        by = int(min(ys)  / scale) + oy
        bw = int((max(x2s) - min(xs))  / scale)
        bh = int((max(y2s) - min(ys))  / scale)
        bbox = (bx, by, bw, bh)

        text = " ".join(tokens)
        out.append(ScreenElement(
            id=make_element_id("text", text, bbox),
            role="text",
            text=text,
            bbox=bbox,
            source="ocr",
            confidence=round(mean_conf, 3),
            invokable=False,
            handle=None,
        ))

    return out


def read_region(
    bbox: tuple[int, int, int, int],
    frame: np.ndarray | None = None,
    psm: int | None = None,
    min_conf: float | None = None,
    token_conf_min: int | None = None,
) -> dict:
    """High-res OCR of a single region.

    Crops *frame* (or a fresh live capture) to *bbox*, runs the OCR pipeline at
    READ_REGION_SCALE (higher than the full-screen OCR_SCALE), and returns::

        {"text": str, "elements": list[ScreenElement]}

    All ScreenElement bboxes are in virtual-desktop pixel coordinates.
    *bbox* is (x, y, w, h) in virtual-desktop pixel coordinates.
    Returns {"text": "", "elements": []} on any failure.
    """
    try:
        bx, by, bw, bh = bbox
        if bw <= 0 or bh <= 0:
            return {"text": "", "elements": []}

        if frame is None:
            from capture import capture_primary_monitor
            frame = capture_primary_monitor()

        fh, fw = frame.shape[:2]
        x1 = max(0, bx)
        y1 = max(0, by)
        x2 = min(fw, bx + bw)
        y2 = min(fh, by + bh)
        if x2 <= x1 or y2 <= y1:
            return {"text": "", "elements": []}

        crop = frame[y1:y2, x1:x2]

        import pytesseract
        from pytesseract import Output

        if config.TESSERACT_PATH:
            pytesseract.pytesseract.tesseract_cmd = config.TESSERACT_PATH

        scale = float(config.READ_REGION_SCALE)
        ch, cw = crop.shape[:2]
        upscaled = cv2.resize(
            crop,
            (max(1, int(round(cw * scale))), max(1, int(round(ch * scale)))),
            interpolation=cv2.INTER_CUBIC,
        )
        gray = cv2.cvtColor(upscaled, cv2.COLOR_BGR2GRAY)
        if gray.mean() < config.OCR_DARK_THRESHOLD:
            gray = cv2.bitwise_not(gray)
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        gray = clahe.apply(gray)

        effective_psm = int(psm if psm is not None else config.OCR_PSM)
        data = pytesseract.image_to_data(
            gray,
            output_type=Output.DICT,
            config=f"--psm {effective_psm}",
        )
    except Exception:
        return {"text": "", "elements": []}

    effective_token_min = token_conf_min if token_conf_min is not None else _TOKEN_CONF_MIN
    effective_min_conf  = min_conf       if min_conf       is not None else config.OCR_MIN_CONF

    origin = (x1, y1)
    ox, oy = origin
    n = len(data["text"])

    lines: dict[tuple[int, int, int], list[int]] = {}
    for i in range(n):
        try:
            conf = int(data["conf"][i])
        except (ValueError, TypeError):
            conf = -1
        if conf < effective_token_min or not data["text"][i].strip():
            continue
        key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        lines.setdefault(key, []).append(i)

    elements: list[ScreenElement] = []
    for indices in lines.values():
        tokens = [data["text"][i].strip() for i in indices if data["text"][i].strip()]
        if not tokens:
            continue
        confs = [int(data["conf"][i]) for i in indices]
        mean_conf = sum(confs) / len(confs) / 100.0
        if mean_conf < effective_min_conf:
            continue

        xs  = [data["left"][i]                    for i in indices]
        ys  = [data["top"][i]                     for i in indices]
        x2s = [data["left"][i] + data["width"][i] for i in indices]
        y2s = [data["top"][i]  + data["height"][i] for i in indices]

        ex = int(min(xs)  / scale) + ox
        ey = int(min(ys)  / scale) + oy
        ew = int((max(x2s) - min(xs))  / scale)
        eh = int((max(y2s) - min(ys))  / scale)
        elem_bbox = (ex, ey, ew, eh)

        text = " ".join(tokens)
        elements.append(ScreenElement(
            id=make_element_id("text", text, elem_bbox),
            role="text",
            text=text,
            bbox=elem_bbox,
            source="ocr",
            confidence=round(mean_conf, 3),
            invokable=False,
            handle=None,
        ))

    elements.sort(key=lambda e: (e.bbox[1], e.bbox[0]))
    full_text = "\n".join(e.text for e in elements)
    return {"text": full_text, "elements": elements}
