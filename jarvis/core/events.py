"""In-process event bus and typed event dataclasses.

All worker threads (voice, perception, gemini, ui) communicate with the
SessionActor exclusively by posting events here.  Nobody mutates shared state
directly; they call EventBus.post().

Event hierarchy
---------------
WakeEvent          — wake word fired; target not yet captured
TargetCaptured     — foreground target snapshot ready (posted immediately after wake)
TranscriptReady    — speech-to-text completed
PerceptionUpdated  — a perception ladder run finished
AnswerChunk        — one streamed text chunk from Gemini/LLM
AnswerDone         — streaming finished (ok or empty)
ActionProposed     — actor wants to execute an action; awaits confirmation result
ActionVerified     — post-action verification result
Cancel             — abort everything in flight for a session (kill hotkey or explicit cancel)
"""

from __future__ import annotations

import queue
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from perception_target import PerceptionTarget
    from router import RouteResult


# ---------------------------------------------------------------------------
# Event dataclasses
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class WakeEvent:
    session_id: str


@dataclass(frozen=True)
class TargetCaptured:
    session_id: str
    target: "PerceptionTarget"


@dataclass(frozen=True)
class TranscriptReady:
    session_id: str
    text: str


@dataclass(frozen=True)
class PerceptionUpdated:
    session_id: str
    route_result: "RouteResult"


@dataclass(frozen=True)
class AnswerChunk:
    session_id: str
    chunk: str
    bubble_id: str


@dataclass(frozen=True)
class AnswerDone:
    session_id: str
    bubble_id: str
    full_text: str
    answer_source: str = "gemini"
    ok: bool = True
    window_sig: str = ""


@dataclass(frozen=True)
class ActionProposed:
    session_id: str
    kind: str
    args: dict[str, Any]
    description: str


@dataclass(frozen=True)
class ActionVerified:
    session_id: str
    ok: bool
    message: str
    detail: str = ""


@dataclass(frozen=True)
class Cancel:
    session_id: str


# Union of all event types (for type checkers)
AnyEvent = (
    WakeEvent
    | TargetCaptured
    | TranscriptReady
    | PerceptionUpdated
    | AnswerChunk
    | AnswerDone
    | ActionProposed
    | ActionVerified
    | Cancel
)


# ---------------------------------------------------------------------------
# EventBus
# ---------------------------------------------------------------------------

class EventBus:
    """Thread-safe FIFO event bus backed by a single queue.Queue.

    Multiple producers (worker threads) call post(); one consumer (the
    SessionActor) calls get() in its loop.  The bus also supports lightweight
    typed subscriptions so the UI layer can receive callbacks without polling.
    """

    def __init__(self, maxsize: int = 0) -> None:
        self._q: queue.Queue[AnyEvent] = queue.Queue(maxsize=maxsize)
        self._lock = threading.Lock()
        self._subscribers: list[Callable[[AnyEvent], None]] = []

    # ------------------------------------------------------------------
    # Producer API
    # ------------------------------------------------------------------

    def post(self, event: AnyEvent) -> None:
        """Enqueue an event.  Never blocks (queue is unbounded by default)."""
        self._q.put_nowait(event)
        with self._lock:
            subscribers = list(self._subscribers)
        for cb in subscribers:
            try:
                cb(event)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Consumer API
    # ------------------------------------------------------------------

    def get(self, timeout: Optional[float] = None) -> AnyEvent:
        """Block until an event is available; raises queue.Empty on timeout."""
        return self._q.get(timeout=timeout)

    def get_nowait(self) -> AnyEvent:
        """Return immediately; raises queue.Empty if queue is empty."""
        return self._q.get_nowait()

    def drain(self) -> list[AnyEvent]:
        """Return all currently queued events without blocking."""
        events: list[AnyEvent] = []
        while True:
            try:
                events.append(self._q.get_nowait())
            except queue.Empty:
                break
        return events

    # ------------------------------------------------------------------
    # Subscription API (for UI callbacks)
    # ------------------------------------------------------------------

    def subscribe(self, callback: Callable[[AnyEvent], None]) -> None:
        """Register *callback* to be invoked synchronously on every post().

        Callbacks run on the posting thread, so keep them fast.
        """
        with self._lock:
            self._subscribers.append(callback)

    def unsubscribe(self, callback: Callable[[AnyEvent], None]) -> None:
        with self._lock:
            try:
                self._subscribers.remove(callback)
            except ValueError:
                pass
