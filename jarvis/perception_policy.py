"""Perception policy: maps AppClass to the ordered list of adapters to run.

Policy table
------------
NATIVE_WIN32      UIA → OCR → CV       (UIA is authoritative; OCR fills gaps)
CHROMIUM_ELECTRON OCR → CV → VISION    (skip UIA; its tree is nearly empty)
UWP               UIA → OCR → CV       (UWP exposes a real UIA tree)
JAVA_SWING        UIA → OCR            (Java has decent UIA; CV adds little)
GAME_FULLSCREEN   VISION only          (no text layer; pure pixel grounding)
UNKNOWN           UIA → OCR → CV       (conservative default; same as NATIVE)

The policy drives two decisions in ``perception.run_ladder``:
1. Which adapters are called when ``use_fusion=True``.
2. Which rung is skipped in the plain ladder (non-fusion) path.
"""

from __future__ import annotations

import enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app_classifier import AppClass


class AdapterOrder(enum.Enum):
    """Named adapter pipeline configurations."""
    FULL          = "full"           # UIA → OCR → CV
    OCR_FIRST     = "ocr_first"      # OCR → CV → VISION  (skip UIA)
    UIA_OCR       = "uia_ocr"        # UIA → OCR  (no CV)
    VISION_ONLY   = "vision_only"    # VISION only


from dataclasses import dataclass


@dataclass(frozen=True)
class PerceptionPolicy:
    """Describes which adapters to run and in what order for a given app class.

    Attributes
    ----------
    order         : Named pipeline variant.
    run_uia       : Whether to call the UIA adapter.
    run_ocr       : Whether to call the OCR adapter.
    run_cv        : Whether to call the CV adapter.
    run_vision    : Whether to call the VISION adapter.
    uia_entry_rung: When not using fusion, the cheapest ladder rung to start from.
                    Electron → OCR; game → VISION; others → UIA.
    """
    order: AdapterOrder
    run_uia: bool
    run_ocr: bool
    run_cv: bool
    run_vision: bool
    # The cheapest rung for the plain (non-fusion) ladder path.
    ladder_entry: str   # Rung name: "UIA" | "OCR" | "VISION"


# ---------------------------------------------------------------------------
# Policy table
# ---------------------------------------------------------------------------

_POLICIES: dict[str, PerceptionPolicy] = {
    "native_win32": PerceptionPolicy(
        order=AdapterOrder.FULL,
        run_uia=True, run_ocr=True, run_cv=True, run_vision=False,
        ladder_entry="UIA",
    ),
    "chromium_electron": PerceptionPolicy(
        order=AdapterOrder.OCR_FIRST,
        run_uia=False, run_ocr=True, run_cv=True, run_vision=True,
        ladder_entry="OCR",
    ),
    "uwp": PerceptionPolicy(
        order=AdapterOrder.FULL,
        run_uia=True, run_ocr=True, run_cv=True, run_vision=False,
        ladder_entry="UIA",
    ),
    "java_swing": PerceptionPolicy(
        order=AdapterOrder.UIA_OCR,
        run_uia=True, run_ocr=True, run_cv=False, run_vision=False,
        ladder_entry="UIA",
    ),
    "game_fullscreen": PerceptionPolicy(
        order=AdapterOrder.VISION_ONLY,
        run_uia=False, run_ocr=False, run_cv=False, run_vision=True,
        ladder_entry="VISION",
    ),
    "unknown": PerceptionPolicy(
        order=AdapterOrder.FULL,
        run_uia=True, run_ocr=True, run_cv=True, run_vision=False,
        ladder_entry="UIA",
    ),
}

_DEFAULT_POLICY = _POLICIES["unknown"]


def policy_for(app_class: "AppClass | None") -> PerceptionPolicy:
    """Return the PerceptionPolicy for the given AppClass (or the default)."""
    if app_class is None:
        return _DEFAULT_POLICY
    return _POLICIES.get(app_class.value, _DEFAULT_POLICY)
