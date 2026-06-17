"""App class detection: classify a target window into a renderer family.

Used to steer perception strategy (e.g. skip UIA for Electron, prefer OCR).
No behavior changes in this revision — classification is stored and logged only.
"""

from __future__ import annotations

import enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

# Known Chromium/Electron window class prefixes
_CHROMIUM_WINDOW_CLASSES = ("Chrome_WidgetWin_",)

# Process names that are definitively Chromium/Electron regardless of window class
_ELECTRON_PROCESSES = frozenset({
    "electron",
    "code",            # VS Code
    "slack",
    "discord",
    "notion",
    "obsidian",
    "figma",
    "spotify",
    "teams",
    "msteams",
    "1password",
    "bitwarden",
    "signal",
    "whatsapp",
    "postman",
})

# Process names that are definitively Chromium browsers
_CHROMIUM_BROWSER_PROCESSES = frozenset({
    "chrome",
    "msedge",
    "brave",
    "opera",
    "vivaldi",
    "chromium",
})

# UWP host processes
_UWP_PROCESSES = frozenset({
    "applicationframehost",
    "systemsettings",
    "windowsstore",
    "wwahost",
})

# Java Swing / AWT
_JAVA_PROCESSES = frozenset({
    "java",
    "javaw",
    "javaws",
})

# Flat-UIA heuristic thresholds: an app with few, generic-role nodes is likely Electron/web
_FLAT_UIA_MAX_NODES = 12
_FLAT_UIA_GENERIC_ROLES = frozenset({"Custom", "Pane", "Document", "Group", ""})


class AppClass(enum.Enum):
    NATIVE_WIN32        = "native_win32"
    CHROMIUM_ELECTRON   = "chromium_electron"
    UWP                 = "uwp"
    JAVA_SWING          = "java_swing"
    GAME_FULLSCREEN     = "game_fullscreen"
    UNKNOWN             = "unknown"


def _get_window_class(hwnd: int) -> str:
    """Return the Win32 window class name for hwnd, or '' on error."""
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(256)
        ctypes.windll.user32.GetClassNameW(hwnd, buf, 256)
        return buf.value
    except Exception:
        return ""


def _probe_flat_uia(hwnd: int) -> bool:
    """Return True if the UIA tree for hwnd looks flat/low-value (Electron heuristic).

    A flat tree has few total nodes and most roles are generic (Pane, Custom, Document).
    """
    try:
        from pywinauto import Desktop
        desktop = Desktop(backend="uia")
        wrapper = desktop.window(handle=hwnd).wrapper_object()

        nodes: list[str] = []

        def _collect(elem, depth: int) -> None:
            if depth > 3 or len(nodes) > _FLAT_UIA_MAX_NODES + 5:
                return
            try:
                role = (getattr(elem.element_info, "control_type", "") or "").strip()
                nodes.append(role)
                for child in elem.children():
                    _collect(child, depth + 1)
            except Exception:
                pass

        _collect(wrapper, 0)

        if len(nodes) > _FLAT_UIA_MAX_NODES:
            return False
        generic_count = sum(1 for r in nodes if r in _FLAT_UIA_GENERIC_ROLES)
        return generic_count >= max(1, len(nodes) * 0.6)
    except Exception:
        return False


def classify_app(process_name: str, window_class: str, hwnd: int) -> AppClass:
    """Classify the renderer family of a window.

    Args:
        process_name: lowercase exe stem (e.g. "code", "chrome", "notepad")
        window_class:  Win32 class name from GetClassName (pass "" to auto-fetch via hwnd)
        hwnd:          window handle; used for window-class fetch and UIA probe

    Returns:
        AppClass member
    """
    pname = process_name.lower().strip()

    # Auto-fetch window class if caller passed an empty string
    wclass = window_class or (_get_window_class(hwnd) if hwnd else "")

    # --- Chromium / Electron ---
    if pname in _CHROMIUM_BROWSER_PROCESSES or pname in _ELECTRON_PROCESSES:
        return AppClass.CHROMIUM_ELECTRON
    if any(wclass.startswith(prefix) for prefix in _CHROMIUM_WINDOW_CLASSES):
        return AppClass.CHROMIUM_ELECTRON

    # --- UWP ---
    if pname in _UWP_PROCESSES:
        return AppClass.UWP

    # --- Java Swing ---
    if pname in _JAVA_PROCESSES:
        return AppClass.JAVA_SWING

    # --- Game fullscreen heuristic ---
    # D3D/OpenGL fullscreen windows typically use class names like "LWJGL" or vendor strings,
    # and have zero UIA children.  Detect via window style WS_POPUP + no UIA text.
    if wclass in ("LWJGL", "GLFW30", "UnrealWindow", "SDL_app"):
        return AppClass.GAME_FULLSCREEN

    # --- Flat UIA probe (catches unlisted Electron apps) ---
    if hwnd and _probe_flat_uia(hwnd):
        return AppClass.CHROMIUM_ELECTRON

    # Default: assume native Win32
    return AppClass.NATIVE_WIN32
