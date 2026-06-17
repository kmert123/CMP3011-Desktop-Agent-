"""Porcupine wake-word listener running in a background thread."""

from __future__ import annotations

import math
import threading
import time
import uuid
from typing import Callable, Optional, TYPE_CHECKING

import numpy as np
import pyaudio

import config
import perception_target as pt

if TYPE_CHECKING:
    from core.events import EventBus


class WakeWordListener:
    def __init__(
        self,
        on_wake: Callable[[str], None],
        bus: "Optional[EventBus]" = None,
    ) -> None:
        """
        Parameters
        ----------
        on_wake:
            Called with the new session_id string after WakeEvent and
            TargetCaptured have been posted.  The caller should start the
            voice-recording worker here (off the audio loop thread).
        bus:
            EventBus to post WakeEvent + TargetCaptured into.  If None the
            listener falls back to the legacy set_pending_target path so that
            the class can still be used without the actor.
        """
        self._on_wake = on_wake
        self._bus = bus
        self._stop_flag = False
        self._thread: threading.Thread | None = None

        try:
            import openwakeword
            openwakeword.utils.download_models()
            from openwakeword.model import Model
            self._oww = Model(
                wakeword_models=[config.WAKEWORD_MODEL],
                inference_framework="onnx",
            )
        except Exception as exc:
            raise RuntimeError(
                "Failed to load the openWakeWord model. The first launch requires "
                "an internet connection to download it (~25 MB)."
            ) from exc

    def start(self) -> None:
        """Start the wake-word listener in a background daemon thread."""
        self._stop_flag = False
        self._thread = threading.Thread(target=self._listen_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Signal the listener to stop and wait for the thread to exit."""
        self._stop_flag = True
        if self._thread is not None:
            self._thread.join(timeout=2)

    def _listen_loop(self) -> None:
        pa = pyaudio.PyAudio()
        stream = pa.open(
            format=pyaudio.paInt16,
            channels=1,
            rate=config.SAMPLE_RATE,
            input=True,
            frames_per_buffer=config.WAKEWORD_CHUNK_SIZE,
        )
        try:
            while not self._stop_flag:
                data = stream.read(config.WAKEWORD_CHUNK_SIZE, exception_on_overflow=False)
                audio_np = np.frombuffer(data, dtype=np.int16)
                scores = self._oww.predict(audio_np)
                score = scores.get(config.WAKEWORD_MODEL, 0.0)
                if score > config.WAKEWORD_THRESHOLD:
                    self._fire_wake()
                    self._oww.reset()
                    time.sleep(0.3)
        finally:
            stream.stop_stream()
            stream.close()
            pa.terminate()

    def _fire_wake(self) -> None:
        # Capture the foreground target BEFORE any UI is shown.
        target = pt.capture_foreground_target()
        session_id = str(uuid.uuid4())

        if self._bus is not None:
            from core.events import WakeEvent, TargetCaptured
            self._bus.post(WakeEvent(session_id=session_id))
            self._bus.post(TargetCaptured(session_id=session_id, target=target))
        else:
            # Legacy path: single-slot pending target (no actor).
            pt.set_pending_target(target)

        self._on_wake(session_id)


if __name__ == "__main__":
    import sys
    debug = "--debug" in sys.argv

    def _on_wake(session_id: str):
        print(f"*** Wake word detected! session={session_id} ***")

    listener = WakeWordListener(on_wake=_on_wake)

    if debug:
        _orig_loop = listener._listen_loop

        def _debug_loop():
            pa = pyaudio.PyAudio()
            stream = pa.open(
                format=pyaudio.paInt16,
                channels=1,
                rate=config.SAMPLE_RATE,
                input=True,
                frames_per_buffer=config.WAKEWORD_CHUNK_SIZE,
            )
            try:
                while not listener._stop_flag:
                    data = stream.read(config.WAKEWORD_CHUNK_SIZE, exception_on_overflow=False)
                    audio_np = np.frombuffer(data, dtype=np.int16)
                    scores = listener._oww.predict(audio_np)
                    rms = math.sqrt(float(np.mean(audio_np.astype(np.float32) ** 2)))
                    score = max(scores.values(), default=0.0)
                    if score > 0.05:
                        print(f"score={score:.3f}  rms={rms:.0f}  keys={list(scores.keys())}")
                    if score > config.WAKEWORD_THRESHOLD:
                        listener._fire_wake()
                        listener._oww.reset()
                        time.sleep(0.3)
            finally:
                stream.stop_stream()
                stream.close()
                pa.terminate()

        listener._listen_loop = _debug_loop

    listener.start()
    print(f"Say 'Hey Jarvis'. Ctrl-C to quit.  (model key: {config.WAKEWORD_MODEL})")
    try:
        while True:
            time.sleep(0.1)
    except KeyboardInterrupt:
        listener.stop()
