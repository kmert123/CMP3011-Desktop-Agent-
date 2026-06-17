"""CV perception adapter: layout region segmentation via Canny contours."""

from __future__ import annotations

import numpy as np

from screen_model import ScreenElement, make_element_id


def read_cv(crop: np.ndarray, origin: tuple[int, int]) -> list[ScreenElement]:
    """Segment crop into layout regions; return ScreenElements in screen coords.

    Returns [] on any failure.
    """
    try:
        from cv_pipeline import segment_regions

        ox, oy = origin
        regions = segment_regions(crop)
        out: list[ScreenElement] = []
        for r in regions:
            rx, ry, rw, rh = r["bbox"]
            bbox = (rx + ox, ry + oy, rw, rh)
            role = r["region"]
            out.append(ScreenElement(
                id=make_element_id(role, "", bbox),
                role=role,
                text="",
                bbox=bbox,
                source="cv",
                confidence=0.5,
                invokable=False,
                handle=None,
            ))
        return out
    except Exception:
        return []
