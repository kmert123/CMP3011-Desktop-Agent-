"""UIA event watcher — invalidates the screen-model cache on structural / property changes.

Subscribes to IUIAutomation StructureChanged and relevant PropertyChanged events on a
target HWND.  Runs the COM pump on a dedicated background thread so it never blocks
the hot path.  Falls back silently if comtypes / pywinauto is unavailable.

Usage
-----
    watcher = UIAWatcher(hwnd, on_invalidate=session.invalidate_screen_cache)
    watcher.start()
    ...
    watcher.stop()   # call when the target changes or the app exits
"""

from __future__ import annotations

import logging
import threading
from typing import Callable

_log = logging.getLogger(__name__)

# PropertyChanged event IDs that signal visible content has changed.
# UIA_* property IDs from UIAutomationClient.
_WATCHED_PROPERTIES: tuple[int, ...] = (
    30012,  # UIA_NamePropertyId
    30031,  # UIA_ValueValuePropertyId
    30033,  # UIA_IsEnabledPropertyId
    30021,  # UIA_IsOffscreenPropertyId
    30045,  # UIA_ExpandCollapseExpandCollapseStatePropertyId
    30070,  # UIA_ToggleToggleStatePropertyId
    30076,  # UIA_SelectionItemIsSelectedPropertyId
)


class UIAWatcher:
    """Subscribe to UIA events on *hwnd* and call *on_invalidate* when they fire.

    The COM pump runs on a dedicated daemon thread.  All event handlers are
    non-blocking — they only call the lightweight callback.
    If COM initialisation fails, the watcher silently does nothing;
    the existing TTL + density-delta cache fallback remains the safety net.
    """

    def __init__(
        self,
        hwnd: int,
        on_invalidate: Callable[[], None],
        on_change: Callable[[], None] | None = None,
    ) -> None:
        self._hwnd = hwnd
        self._on_invalidate = on_invalidate
        # Optional second callback fired on every UIA event (before on_invalidate).
        # Used by execute_plan to signal its settle Event without touching the cache logic.
        self._on_change = on_change
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> None:
        if self._hwnd == 0:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._pump,
            name=f"uia-watcher-{self._hwnd}",
            daemon=True,
        )
        self._thread.start()
        _log.debug("UIAWatcher started for hwnd=%d", self._hwnd)

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        _log.debug("UIAWatcher stopped for hwnd=%d", self._hwnd)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _pump(self) -> None:
        """Run in the watcher thread: initialise COM, subscribe, pump messages."""
        try:
            self._run_com_loop()
        except Exception as exc:
            _log.debug("UIAWatcher pump exited: %s", exc)

    def _run_com_loop(self) -> None:
        # comtypes COM must be initialised per-thread (STA).
        try:
            import comtypes
            import comtypes.client
            comtypes.CoInitialize()
        except Exception as exc:
            _log.debug("UIAWatcher: comtypes init failed — falling back to TTL: %s", exc)
            return

        uia = None
        try:
            # Import the pre-generated UIAutomation wrapper.
            # comtypes generates either UIAutomationClient or a GUID-named module depending
            # on how it was first loaded; try both and fall back to GetModule on first run.
            UIA = None
            for _mod_name in ("comtypes.gen.UIAutomationClient",
                              "comtypes.gen._944DE083_8FB8_45CF_BCB7_C477ACB2F897_0_1_0"):
                try:
                    import importlib
                    UIA = importlib.import_module(_mod_name)
                    break
                except ImportError:
                    pass
            if UIA is None:
                comtypes.client.GetModule("UIAutomationCore.dll")
                import comtypes.gen.UIAutomationClient as UIA  # type: ignore[import]

            uia = comtypes.client.CreateObject(
                UIA.CUIAutomation._reg_clsid_,
                interface=UIA.IUIAutomation,
            )

            element = uia.ElementFromHandle(self._hwnd)
            if element is None:
                _log.debug("UIAWatcher: ElementFromHandle(%d) returned None", self._hwnd)
                return

            cb = self._on_invalidate
            cb_change = self._on_change

            def _fire() -> None:
                try:
                    if cb_change is not None:
                        cb_change()
                except Exception:
                    pass
                try:
                    cb()
                except Exception:
                    pass

            # Build COM-compatible handler classes by subclassing the comtypes interfaces.
            # comtypes implements COM vtable dispatch automatically for Python subclasses.

            class StructureHandler(comtypes.COMObject):
                _com_interfaces_ = [UIA.IUIAutomationStructureChangedEventHandler]

                def HandleStructureChangedEvent(self, sender, change_type, runtime_id):
                    _fire()

            class PropertyHandler(comtypes.COMObject):
                _com_interfaces_ = [UIA.IUIAutomationPropertyChangedEventHandler]

                def HandlePropertyChangedEvent(self, sender, property_id, new_value):
                    _fire()

            struct_handler = StructureHandler()
            uia.AddStructureChangedEventHandler(
                element,
                UIA.TreeScope_Subtree,
                None,  # CacheRequest — None means no caching
                struct_handler,
            )

            prop_handler = PropertyHandler()
            prop_ids = (comtypes.c_int * len(_WATCHED_PROPERTIES))(*_WATCHED_PROPERTIES)
            uia.AddPropertyChangedEventHandlerNativeArray(
                element,
                UIA.TreeScope_Subtree,
                None,
                prop_handler,
                prop_ids,
                len(_WATCHED_PROPERTIES),
            )

            _log.debug("UIAWatcher: subscribed to hwnd=%d", self._hwnd)

            # Pump COM messages until stop() is called.
            # MsgWaitForMultipleObjectsEx with QS_ALLINPUT pumps the STA message queue
            # so COM callbacks are dispatched on this thread.
            import ctypes
            MWMO_ALERTABLE = 0x0002
            QS_ALLINPUT = 0x04FF
            while not self._stop_event.wait(timeout=0):
                ctypes.windll.user32.MsgWaitForMultipleObjectsEx(
                    0, None, 50, QS_ALLINPUT, MWMO_ALERTABLE,
                )
                if self._stop_event.is_set():
                    break

        except Exception as exc:
            _log.debug("UIAWatcher: subscription failed — falling back to TTL: %s", exc)

        finally:
            try:
                if uia is not None:
                    uia.RemoveAllEventHandlers()
            except Exception:
                pass
            try:
                comtypes.CoUninitialize()
            except Exception:
                pass
