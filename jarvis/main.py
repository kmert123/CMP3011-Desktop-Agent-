"""Entry point — wires session, router, streaming Gemini, and UI via the event bus."""

from __future__ import annotations

import os
import sys
import threading
import time
import uuid

# Set per-monitor DPI v2 awareness before any window or Win32 API call so that
# GetWindowRect, UIA rectangles, and mss captures all use physical screen pixels
# (virtual-desktop pixel space).  Must happen before CustomTkinter creates a HWND.
if sys.platform == "win32":
    try:
        import ctypes as _ctypes
        # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
        _ctypes.windll.user32.SetProcessDpiAwarenessContext(-4)
    except Exception:
        pass

import actions
import gemini
import perception_target as pt
import privacy
import router
import telemetry
import transcription
import voice
from classify import Intent, classify_intent
from core.events import (
    ActionProposed,
    ActionVerified,
    AnswerChunk,
    AnswerDone,
    Cancel,
    EventBus,
    PerceptionUpdated,
    TargetCaptured,
    TranscriptReady,
    WakeEvent,
)
from core.session_actor import SessionActor
from router import RouteResult
from session_context import SessionContext
from ui import JarvisWindow
from uia_watcher import UIAWatcher
from wake_word import WakeWordListener


class JarvisApp:
    def __init__(self) -> None:
        self.ui = JarvisWindow(on_submit_text=self._handle_follow_up)

        # Event bus: all workers post here; actor consumes.
        self._bus = EventBus()

        self.session = SessionContext()
        self._actor = SessionActor(
            self._bus,
            self.session,
            on_wake=self._on_wake,
            on_transcript=self._on_transcript,
            on_answer_chunk=self._on_answer_chunk,
            on_answer_done=self._on_answer_done,
            on_action_proposed=self._on_action_proposed,
            on_action_verified=self._on_action_verified,
            on_cancel=self._on_cancel,
        )

        self.listener = WakeWordListener(
            on_wake=self._handle_wake_word,
            bus=self._bus,
        )
        self._uia_watcher: UIAWatcher | None = None
        pt.register_self(self.ui.get_hwnd(), os.getpid())

        # Wire kill hotkey → Cancel event for the current session.
        self._setup_kill_hotkey()

    # ------------------------------------------------------------------
    # Kill hotkey
    # ------------------------------------------------------------------

    def _setup_kill_hotkey(self) -> None:
        import config
        try:
            import keyboard
            def _post_cancel():
                sid = self._actor.current_session_id()
                if sid:
                    self._bus.post(Cancel(session_id=sid))
                # Also set the legacy actions._cancel flag so in-flight
                # actions that haven't migrated yet still stop.
                actions._cancel.set()
            keyboard.add_hotkey(config.KILL_HOTKEY, _post_cancel)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Wake-word → WakeEvent + TargetCaptured
    # ------------------------------------------------------------------

    def _handle_wake_word(self, session_id: str) -> None:
        """Called by WakeWordListener after it posts WakeEvent+TargetCaptured."""
        # WakeWordListener now owns the event posting; this callback just
        # triggers the UI show.  We spawn a thread to do the recording so we
        # don't block the wake-word audio loop.
        threading.Thread(
            target=self._voice_invocation,
            args=(session_id,),
            daemon=True,
        ).start()

    # ------------------------------------------------------------------
    # Actor callbacks (all called on the actor thread — hand off fast)
    # ------------------------------------------------------------------

    def _on_wake(self, session_id: str) -> None:
        self.ui.status_heard()

    def _on_transcript(
        self,
        session_id: str,
        text: str,
        target,
        session_ctx: SessionContext,
        cancel_event: threading.Event,
    ) -> None:
        threading.Thread(
            target=self._answer_worker,
            args=(session_id, text, target, session_ctx, cancel_event),
            daemon=True,
        ).start()

    def _on_answer_chunk(self, session_id: str, bubble_id: str, chunk: str) -> None:
        self.ui.append_stream_chunk(bubble_id, chunk)

    def _on_answer_done(
        self,
        session_id: str,
        bubble_id: str,
        full_text: str,
        source: str,
        ok: bool,
    ) -> None:
        self.ui.finish_stream_bubble(bubble_id)

    def _on_action_proposed(
        self,
        session_id: str,
        kind: str,
        args: dict,
        description: str,
        cancel_event: threading.Event,
    ) -> None:
        threading.Thread(
            target=self._action_worker,
            args=(session_id, kind, args, description, cancel_event),
            daemon=True,
        ).start()

    def _on_action_verified(
        self, session_id: str, ok: bool, message: str, detail: str
    ) -> None:
        if ok:
            self.ui.jarvis_says(message)
        else:
            error = f"{message} — {detail}" if detail else message
            self.ui.show_error(error)

    def _on_cancel(self, session_id: str) -> None:
        self.ui.status_heard()  # reset status indicator

    # ------------------------------------------------------------------
    # Voice invocation worker
    # ------------------------------------------------------------------

    def _voice_invocation(self, session_id: str) -> None:
        self.ui.show_window()
        self.ui.status_recording()
        pcm = voice.record_until_silence()
        self.ui.status_transcribing()
        prompt = self.session.world_state.build_transcription_prompt()
        question = transcription.transcribe_pcm(pcm, initial_prompt=prompt).strip()
        if not question:
            self.ui.show_error("Didn't catch that. Try again.")
            return
        self.ui.user_said(question)
        self._bus.post(TranscriptReady(session_id=session_id, text=question))

    # ------------------------------------------------------------------
    # Follow-up text (typed in the UI)
    # ------------------------------------------------------------------

    def _handle_follow_up(self, text: str) -> None:
        import config
        import time as _time

        sid = str(uuid.uuid4())
        self._bus.post(WakeEvent(session_id=sid))

        # Re-capture if the foreground window changed or the stored target is stale.
        stored = self.session.active_target
        elapsed_ms = (
            (_time.monotonic() - stored.wake_ts) * 1000
            if stored is not None and stored.wake_ts
            else float("inf")
        )
        need_recapture = True
        if stored is not None and elapsed_ms <= config.FOLLOWUP_RECAPTURE_MS:
            # Check whether the foreground hwnd still matches the stored target.
            try:
                import win32gui
                current_hwnd = win32gui.GetForegroundWindow()
                need_recapture = (current_hwnd != stored.hwnd)

                # hwnd is insufficient for Chromium/Electron and UWP: a tab switch
                # keeps the same top-level hwnd while changing the page.  Compare
                # the live window title as a secondary signal — a tab switch always
                # changes the title.  On any win32 failure, fail safe (recapture).
                if not need_recapture and stored.app_class is not None:
                    _tab_reuse_classes = {"chromium_electron", "uwp"}
                    if stored.app_class.value in _tab_reuse_classes:
                        current_title = win32gui.GetWindowText(current_hwnd)
                        if current_title != stored.title:
                            need_recapture = True
            except Exception:
                need_recapture = True  # can't check; fail safe → recapture

        # P11: also recapture when the stored target's interaction state has
        # expired, even if the hwnd matches — selection_text and focused_element
        # are stale after FOCUS_STATE_TTL_MS and must be re-read live.
        if not need_recapture and stored is not None and not stored.interaction_state_fresh():
            need_recapture = True

        if need_recapture:
            fresh = pt.capture_foreground_target()
            self._bus.post(TargetCaptured(session_id=sid, target=fresh))

        self._bus.post(TranscriptReady(session_id=sid, text=text))

    # ------------------------------------------------------------------
    # Answer worker (runs off the actor thread)
    # ------------------------------------------------------------------

    def _answer_worker(
        self,
        session_id: str,
        question: str,
        target,
        session_ctx: SessionContext,
        cancel_event: threading.Event,
    ) -> None:
        import trace as _trace_mod
        turn_id = str(uuid.uuid4())
        t0 = time.monotonic()
        trace = _trace_mod.TurnTrace(turn_id, t0)
        trace.record(
            "INPUT",
            query=question,
            process=getattr(target, "process", "") or "",
            title=getattr(target, "title", "") or "",
            app_class=str(getattr(target, "app_class", "") or ""),
        )

        self._attach_watcher(target)
        self.ui.status_cv()

        route_result = router.route(question, session_ctx, trace=trace)

        if cancel_event.is_set():
            trace.finish(cancelled=True)
            return

        trace.record(
            "CLASSIFY",
            act=route_result.act.value,
            perception_mode=route_result.perception_mode.value,
            intent=route_result.intent.value,
            used_cache=route_result.used_cache,
        )

        # Focus resolution (Task 18): run the deictic/reference ladder when
        # the classifier flagged needs_focus.  Attaches FocusResult to
        # route_result so the prompt builders in gemini.py can inject it as
        # the PRIMARY context for the model.
        classify_result = classify_intent(question)
        if classify_result.needs_focus:
            from focus_resolver import resolve_focus
            sm = route_result.perception.screen_model if route_result.perception else None
            route_result.focus_result = resolve_focus(
                question, classify_result, sm, target, trace=trace
            )

        self._bus.post(PerceptionUpdated(session_id=session_id, route_result=route_result))

        if route_result.intent == Intent.ACTION:
            sm = route_result.perception.screen_model if route_result.perception else None
            self._dispatch_action_from_route(session_id, question, sm, cancel_event)
            trace.finish(answer_source="action")
            return

        self.ui.status_thinking()

        # Escalate BEFORE streaming so the user sees exactly one answer.
        did_escalate = False
        pre_rung = route_result.perception.rung if route_result.perception else None
        escalated_rung_name = None
        if not cancel_event.is_set() and router.should_escalate(question, route_result):
            escalated = router.escalate_route(question, session_ctx, pre_rung)
            if not cancel_event.is_set() and escalated.perception:
                route_result = escalated
                did_escalate = True
                escalated_rung_name = escalated.perception.rung.name

        # Build window signature for history continuity.
        # Computed before streaming so both the user turn (recorded below) and
        # the assistant turn (carried on AnswerDone) share the same sig, which
        # lets cross-window gating in to_prompt_block() demote both halves.
        window_sig: str = ""
        if target is not None:
            ac = getattr(target, "app_class", None)
            window_sig = "|".join([
                getattr(target, "process", "") or "",
                ac.value if ac is not None else "",
                getattr(target, "title", "") or "",
            ])

        # Record the user turn BEFORE streaming so it is in history when the
        # actor appends the assistant turn on AnswerDone (ordering is deterministic).
        session_ctx.add_turn("user", question, window_sig=window_sig)

        self._stream_answer(
            session_id, question, route_result, cancel_event,
            t0=t0,
            turn_id=turn_id,
            trace=trace,
            escalated=did_escalate,
            escalated_rung=escalated_rung_name,
            pre_rung=pre_rung.name if pre_rung else None,
            window_sig=window_sig,
        )

    def _stream_answer(
        self,
        session_id: str,
        question: str,
        route_result: RouteResult,
        cancel_event: threading.Event,
        *,
        t0: float | None = None,
        turn_id: "str | None" = None,
        trace=None,
        escalated: bool = False,
        escalated_rung: str | None = None,
        pre_rung: "str | None" = None,
        window_sig: str = "",
    ) -> str:
        bubble_id = str(uuid.uuid4())
        meta: dict = {}
        gen = gemini.ask_stream(question, route_result, self.session, meta=meta, trace=trace)

        try:
            first_chunk = next(gen)
        except StopIteration:
            source = meta.get("answer_source", "gemini")
            self.ui.begin_stream_bubble(bubble_id, offline=(source != "gemini"))
            self.ui.finish_stream_bubble(bubble_id)
            self._bus.post(AnswerDone(
                session_id=session_id, bubble_id=bubble_id,
                full_text="", answer_source=source, ok=True,
                window_sig=window_sig,
            ))
            if trace is not None:
                trace.record("ANSWER", answer_text="", answer_source=source, chars=0)
                trace.finish(answer_source=source, latency_ms=int((time.monotonic() - t0) * 1000) if t0 else None)
            return ""

        source = meta.get("answer_source", "gemini")
        self.ui.begin_stream_bubble(bubble_id, offline=(source != "gemini"))
        chunks = [first_chunk]
        self._bus.post(AnswerChunk(
            session_id=session_id, bubble_id=bubble_id, chunk=first_chunk,
        ))

        try:
            for chunk in gen:
                if cancel_event.is_set():
                    break
                chunks.append(chunk)
                self._bus.post(AnswerChunk(
                    session_id=session_id, bubble_id=bubble_id, chunk=chunk,
                ))
        finally:
            full_text = "".join(chunks)
            self._bus.post(AnswerDone(
                session_id=session_id, bubble_id=bubble_id,
                full_text=full_text, answer_source=source, ok=True,
                window_sig=window_sig,
            ))
            rung = route_result.perception.rung if route_result.perception else None
            latency_ms = int((time.monotonic() - t0) * 1000) if t0 is not None else None
            perc = route_result.perception
            elem_count = len(perc.screen_model.elements) if (perc and perc.screen_model) else None
            char_count = len(perc.text or "") if perc else None
            telemetry.log_query(telemetry.build_record(
                query=question,
                intent=route_result.intent.value,
                answer_source=source,
                perception_rung=pre_rung or (rung.name if rung else None),
                escalated=escalated if escalated else None,
                escalated_rung=escalated_rung,
                latency_ms=latency_ms,
                turn_id=turn_id,
                element_count=elem_count,
                char_count=char_count,
            ))
            if trace is not None:
                trace.record("ANSWER", escalated=escalated, escalated_rung=escalated_rung,
                             answer_source=source, chars=len(full_text),
                             answer_text=full_text[:4000])
                trace.finish(answer_source=source, latency_ms=latency_ms)

        return "".join(chunks)

    # ------------------------------------------------------------------
    # Action dispatch
    # ------------------------------------------------------------------

    def _dispatch_action_from_route(
        self,
        session_id: str,
        question: str,
        screen_model,
        cancel_event: threading.Event,
    ) -> None:
        self.ui.status_thinking()
        plan = gemini.parse_action(question)
        if not plan:
            self.ui.show_error("Could not parse action command — try rephrasing.")
            return
        # Summarise plan for the confirm modal description.
        description = "; ".join(
            s.description or s.kind for s in plan.steps
        ) if plan.steps else question
        self._bus.post(ActionProposed(
            session_id=session_id,
            kind="__plan__",
            args={"_plan": plan, "_screen_model": screen_model},
            description=description,
        ))

    def _action_worker(
        self,
        session_id: str,
        kind: str,
        args: dict,
        description: str,
        cancel_event: threading.Event,
    ) -> None:
        if cancel_event.is_set():
            return
        # Extract smuggled plan + screen_model from args.
        screen_model = args.pop("_screen_model", None)
        plan = args.pop("_plan", None)

        if plan is not None:
            result = actions.execute_plan(plan, self.ui, screen_model=screen_model)
        else:
            result = actions.dispatch(kind, args, self.ui, screen_model=screen_model)

        self._bus.post(ActionVerified(
            session_id=session_id,
            ok=result.ok,
            message=result.message,
            detail=result.detail,
        ))
        self.session.add_turn("user", description)
        self.session.add_turn("assistant", result.message)

    # ------------------------------------------------------------------
    # UIA watcher helpers
    # ------------------------------------------------------------------

    def _attach_watcher(self, target: "pt.PerceptionTarget | None") -> None:
        if self._uia_watcher is not None:
            self._uia_watcher.stop()
            self._uia_watcher = None
        if target is not None and not target.is_self and target.hwnd:
            self._uia_watcher = UIAWatcher(
                target.hwnd,
                on_invalidate=self.session.invalidate_screen_cache,
            )
            self._uia_watcher.start()

    # ------------------------------------------------------------------
    # Run
    # ------------------------------------------------------------------

    def run(self) -> None:
        if privacy.needs_warning():
            privacy.show_warning_blocking()
        self._actor.start()
        self.listener.start()
        try:
            self.ui.mainloop()
        finally:
            self.listener.stop()
            self._actor.stop()
            if self._uia_watcher is not None:
                self._uia_watcher.stop()


if __name__ == "__main__":
    import logging_setup
    logging_setup.setup_logging()
    JarvisApp().run()
