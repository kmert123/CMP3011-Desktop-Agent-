"""Single state-actor thread that exclusively owns SessionContext and pending targets.

All external threads (voice, perception, gemini, UI) post events to the shared
EventBus.  The SessionActor loop dequeues events one at a time and applies them
in-order, so there are no races on session state.

Per-session-id target queue
---------------------------
The old single-slot ``perception_target._pending`` is replaced by a per-session
deque keyed by session_id.  A WakeEvent creates a new session; TargetCaptured
enqueues the target for that session; TranscriptReady consumes the head target.
If a second WakeEvent arrives before the first session is processed, it gets its
own slot — no clobbering.

Cancel
------
A Cancel event sets a threading.Event that actions.py reads before dispatch.
It also marks the session as cancelled so any in-flight answer/action worker
can detect it and stop early.
"""

from __future__ import annotations

import collections
import logging
import queue
import threading
import uuid
from collections import deque
from typing import TYPE_CHECKING, Any, Callable, Optional

if TYPE_CHECKING:
    from core.events import AnyEvent, EventBus
    from perception_target import PerceptionTarget
    from session_context import SessionContext

from core.events import (
    ActionProposed,
    ActionVerified,
    AnswerChunk,
    AnswerDone,
    Cancel,
    PerceptionUpdated,
    TargetCaptured,
    TranscriptReady,
    WakeEvent,
)

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Per-session state record (private to the actor)
# ---------------------------------------------------------------------------

class _SessionState:
    __slots__ = (
        "session_id",
        "target",
        "cancelled",
        "cancel_event",
        "pending_targets",
    )

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.target: Optional["PerceptionTarget"] = None
        self.cancelled = False
        self.cancel_event = threading.Event()
        # Queue of PerceptionTarget objects for this session_id.
        # A new WakeEvent for the same session_id pushes another target.
        self.pending_targets: deque["PerceptionTarget"] = deque()


# ---------------------------------------------------------------------------
# SessionActor
# ---------------------------------------------------------------------------

class SessionActor:
    """Single-writer owner of SessionContext and all per-session targets.

    Usage::

        bus = EventBus()
        session = SessionContext()
        actor = SessionActor(bus, session, on_wake=..., on_transcript=...)
        actor.start()
        # ... app runs ...
        actor.stop()

    Callbacks
    ---------
    All callbacks are invoked *on the actor thread* and must be fast (hand off
    to a worker thread for anything blocking).  Available callbacks:

    on_wake(session_id)
        Called when a WakeEvent is dequeued (UI: show listening status).

    on_transcript(session_id, text, target, session_context, cancel_event)
        Called when TranscriptReady is dequeued with the paired target.
        Receives the cancel_event so the worker can check it mid-flight.

    on_answer_chunk(session_id, bubble_id, chunk)
        Called for each AnswerChunk.

    on_answer_done(session_id, bubble_id, full_text, source, ok)
        Called when an answer stream completes.

    on_action_proposed(session_id, kind, args, description, cancel_event)
        Called when an ActionProposed event arrives.

    on_action_verified(session_id, ok, message, detail)
        Called with the outcome of action verification.

    on_cancel(session_id)
        Called when Cancel fires; UI should dismiss in-progress indicators.
    """

    def __init__(
        self,
        bus: "EventBus",
        session: "SessionContext",
        *,
        on_wake: Optional[Callable[[str], None]] = None,
        on_transcript: Optional[Callable[..., None]] = None,
        on_answer_chunk: Optional[Callable[[str, str, str], None]] = None,
        on_answer_done: Optional[Callable[..., None]] = None,
        on_action_proposed: Optional[Callable[..., None]] = None,
        on_action_verified: Optional[Callable[..., None]] = None,
        on_cancel: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._bus = bus
        self._session = session
        self._on_wake = on_wake
        self._on_transcript = on_transcript
        self._on_answer_chunk = on_answer_chunk
        self._on_answer_done = on_answer_done
        self._on_action_proposed = on_action_proposed
        self._on_action_verified = on_action_verified
        self._on_cancel = on_cancel

        # Per-session state; keyed by session_id string.
        self._sessions: dict[str, _SessionState] = {}

        # The most recent session_id (for legacy single-session code paths).
        self._current_session_id: Optional[str] = None

        self._stop_flag = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="session-actor", daemon=True
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop_flag.set()
        # Unblock the queue.get() with a sentinel Cancel event.
        self._bus.post(Cancel(session_id="__stop__"))
        self._thread.join(timeout=timeout)

    # ------------------------------------------------------------------
    # Public query helpers (thread-safe; read-only)
    # ------------------------------------------------------------------

    def cancel_event_for(self, session_id: str) -> threading.Event:
        """Return the cancel event for *session_id* (creates a dummy if unknown)."""
        state = self._sessions.get(session_id)
        if state is None:
            return threading.Event()
        return state.cancel_event

    def current_session_id(self) -> Optional[str]:
        return self._current_session_id

    # ------------------------------------------------------------------
    # Actor main loop — runs on dedicated thread
    # ------------------------------------------------------------------

    def _run(self) -> None:
        while not self._stop_flag.is_set():
            try:
                event = self._bus.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self._dispatch(event)
            except Exception:
                _log.exception("SessionActor: unhandled error processing %r", event)

    def _dispatch(self, event: "AnyEvent") -> None:
        if isinstance(event, WakeEvent):
            self._handle_wake(event)
        elif isinstance(event, TargetCaptured):
            self._handle_target_captured(event)
        elif isinstance(event, TranscriptReady):
            self._handle_transcript_ready(event)
        elif isinstance(event, PerceptionUpdated):
            self._handle_perception_updated(event)
        elif isinstance(event, AnswerChunk):
            self._handle_answer_chunk(event)
        elif isinstance(event, AnswerDone):
            self._handle_answer_done(event)
        elif isinstance(event, ActionProposed):
            self._handle_action_proposed(event)
        elif isinstance(event, ActionVerified):
            self._handle_action_verified(event)
        elif isinstance(event, Cancel):
            self._handle_cancel(event)

    # ------------------------------------------------------------------
    # Event handlers (all on the actor thread — no locking needed)
    # ------------------------------------------------------------------

    def _get_or_create(self, session_id: str) -> _SessionState:
        if session_id not in self._sessions:
            self._sessions[session_id] = _SessionState(session_id)
        return self._sessions[session_id]

    def _handle_wake(self, event: WakeEvent) -> None:
        state = self._get_or_create(event.session_id)
        # Reset cancellation for a fresh wake.
        state.cancelled = False
        state.cancel_event.clear()
        self._current_session_id = event.session_id
        _log.debug("Actor: WakeEvent session=%s", event.session_id)
        if self._on_wake:
            self._on_wake(event.session_id)

    def _handle_target_captured(self, event: TargetCaptured) -> None:
        state = self._get_or_create(event.session_id)
        state.pending_targets.append(event.target)
        _log.debug(
            "Actor: TargetCaptured session=%s target=%s (queue depth=%d)",
            event.session_id, event.target.process, len(state.pending_targets),
        )

    def _handle_transcript_ready(self, event: TranscriptReady) -> None:
        state = self._get_or_create(event.session_id)
        if state.cancelled:
            _log.debug("Actor: TranscriptReady dropped (session cancelled)")
            return

        # Consume the oldest pending target for this session.
        target = state.pending_targets.popleft() if state.pending_targets else None
        if target is not None:
            state.target = target
            self._session.set_active_target(target)

        _log.debug(
            "Actor: TranscriptReady session=%s text=%r target=%s",
            event.session_id, event.text[:60],
            target.process if target else "none",
        )
        if self._on_transcript:
            self._on_transcript(
                event.session_id,
                event.text,
                target,
                self._session,
                state.cancel_event,
            )

    def _handle_perception_updated(self, event: PerceptionUpdated) -> None:
        # Perception results are already stored inside the RouteResult;
        # we update the session's screen read cache here for future cache hits.
        state = self._sessions.get(event.session_id)
        if state is None or state.cancelled:
            return
        pr = event.route_result.perception
        if pr is not None and pr.text.strip():
            target = state.target
            process = target.process if target else ""
            # The router already called session.set_screen_read; nothing extra needed.
            _log.debug("Actor: PerceptionUpdated session=%s rung=%s",
                       event.session_id, pr.rung.name if pr else "?")

    def _handle_answer_chunk(self, event: AnswerChunk) -> None:
        state = self._sessions.get(event.session_id)
        if state and state.cancelled:
            return
        if self._on_answer_chunk:
            self._on_answer_chunk(event.session_id, event.bubble_id, event.chunk)

    def _handle_answer_done(self, event: AnswerDone) -> None:
        state = self._sessions.get(event.session_id)
        if state and not state.cancelled:
            self._session.add_turn("assistant", event.full_text, window_sig=event.window_sig)
        if self._on_answer_done:
            self._on_answer_done(
                event.session_id, event.bubble_id,
                event.full_text, event.answer_source, event.ok,
            )
        # Evict old sessions (keep at most 10 session states in memory).
        if len(self._sessions) > 10:
            oldest = next(iter(self._sessions))
            if oldest != event.session_id:
                del self._sessions[oldest]

    def _handle_action_proposed(self, event: ActionProposed) -> None:
        state = self._sessions.get(event.session_id)
        if state is None or state.cancelled:
            return
        if self._on_action_proposed:
            self._on_action_proposed(
                event.session_id,
                event.kind,
                event.args,
                event.description,
                state.cancel_event,
            )

    def _handle_action_verified(self, event: ActionVerified) -> None:
        if self._on_action_verified:
            self._on_action_verified(
                event.session_id, event.ok, event.message, event.detail,
            )

    def _handle_cancel(self, event: Cancel) -> None:
        if event.session_id == "__stop__":
            return
        state = self._sessions.get(event.session_id)
        if state:
            state.cancelled = True
            state.cancel_event.set()
        _log.info("Actor: Cancel session=%s", event.session_id)
        if self._on_cancel:
            self._on_cancel(event.session_id)
