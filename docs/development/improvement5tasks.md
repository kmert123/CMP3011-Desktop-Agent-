# Jarvis — Chrome Perception & Gemini-Load Treatment Plan

Treatment for the seven root causes in `jarvis/docs/DIAGNOSIS.md`. Each task is scoped to be
handed to a coding agent as-is. The `prompt` block is what you paste; it carries enough technical
direction to act without re-deriving the design.

## Conventions

- `@jarvis/docs/DIAGNOSIS.md §N` references the diagnosis. Section map:
  - §2.A No structural Chrome backend · §2.B loose cache identity · §2.C stale follow-up target
  - §2.D history outweighs perception · §2.E Gemini-only answer path
  - §3 doc/code divergences (D1–D7) · §4 ranked root causes · §5 proposed remediation (P1–P7)
- Tasks are ordered so dependencies always precede dependents. Don't skip ahead.
- Every task ends with a concrete **Verify** check. Treat a task as incomplete until it passes.
- The whole codebase lives under `jarvis/`. All file paths below are relative to `jarvis/`.

**Treatment summary (maps diagnosis → tasks):**

| Root cause (rank) | Symptom(s) | Task(s) |
|---|---|---|
| R1 — No structural Chrome backend | 1,2,3,4 | 4 (CDP backend), 5 (policy wiring), 6 (URL identity from CDP) |
| R2 — Gemini-only answer path | 3 | 7 (local-first STRUCTURE) |
| R3 — hwnd-only follow-up recapture | 1,4 | 1 (title-aware recapture), 2 (stale COM/selection guards) |
| R4 — URL cache guard no-op | 2,4 | 3 (omnibox-class URL fallback), 6 (CDP URL) |
| R5 — Unconditional history injection | 1 | 8 (window-continuity gate) |
| R6 — Double-answer on escalation | 1 | 9 (single-stream escalation) |
| R7 — No per-turn visibility / telemetry bug | 4 | 10 (debug + telemetry fix), 11 (Chrome eval fixtures) |

**Phases:** P1 Quick wins (1–3, 7–11) → P2 Structural Chrome backend (4–6).
P1 tasks are independent, low-risk, and individually shippable — land them first for immediate
relief. P2 is the high-leverage structural fix and is gated behind a feature flag so it can be
merged dark and enabled per-environment.

---

## Phase 1 — Quick wins (independent, low-risk)

### Task 1: Title-aware follow-up recapture for Chrome tab switches

**Goal:** Detect Chrome/Electron tab switches (which preserve hwnd) on typed follow-ups so the
session stops reusing the previous tab's target. Fixes R3 / Symptoms 1 & 4.
**Files:** `main.py`, `perception_target.py` (read only — confirm `PerceptionTarget` carries `title` and `app_class`).
**Depends on:** none.

```prompt
Read @jarvis/docs/DIAGNOSIS.md §2.C and §5/P3.

In main.py `_handle_follow_up()` the recapture decision is currently `need_recapture =
(current_hwnd != stored.hwnd)` inside the `elapsed_ms <= FOLLOWUP_RECAPTURE_MS` window. A Chrome
tab switch keeps the same hwnd, so this never fires and the stale Tab-A target is reused.

Add a secondary signal: when `stored.app_class` is CHROMIUM_ELECTRON (or UWP — both reuse one
top-level hwnd across tabs/views), also compare the live foreground window title
(`win32gui.GetWindowText(current_hwnd)`) against `stored.title`. If the title differs, force
`need_recapture = True`. Keep the existing hwnd check for all app classes. Guard the win32 calls
in the existing try/except; on any exception, fall back to recapture (`need_recapture = True`),
not reuse — failing safe means re-reading, not serving stale context.

Confirm PerceptionTarget already stores `title` and `app_class`; if `title` is absent, capture it
in `capture_foreground_target()` via GetWindowText at capture time. Do not change the
FOLLOWUP_RECAPTURE_MS constant.
```

**Verify:** On Chrome, ask a question on Tab A, switch to Tab B (same window), type a follow-up within 1.5s → a fresh `TargetCaptured` is posted and the answer is about Tab B, not Tab A.

---

### Task 2: Invalidate stale interaction state on recapture

**Goal:** Stop a wake-time COM `focused_element` pointer and `selection_text` from one page leaking
into a later turn on a different page. Fixes R3 / Symptom 1.
**Files:** `perception_target.py`, `focus_resolver.py`, `focus.py`.
**Depends on:** 1.

```prompt
Read @jarvis/docs/DIAGNOSIS.md §2.C.

PerceptionTarget.focused_element is a raw comtypes IUIAutomationElement COM pointer captured at
wake time; selection_text is captured eagerly at the same moment. Both are held across follow-up
turns. After a tab switch/navigation the pointer can reference a destroyed DOM subtree and fuzzy
name+role matching can bind it to an unrelated element on the new page.

Two changes:
1. When Task 1 forces a recapture, the new PerceptionTarget already re-runs
   `_capture_interaction_state()`, so the new target is clean. Verify the OLD target is fully
   replaced in session state (not merged) — no field from the prior target survives.
2. Add a validity guard before any consumer dereferences `focused_element`. In focus.py's
   `get_focused_element()` (and any other call site that touches the COM pointer), wrap the first
   property access (e.g. CurrentName / CurrentBoundingRectangle) in try/except for
   comtypes.COMError / ElementNotAvailable. On failure, treat the focused element as absent
   (return None) instead of falling through to fuzzy name+role matching against the current screen
   model. Add a short comment that a dead COM pointer must never fuzzy-match a live element.

Do not weaken the live-pointer happy path; only short-circuit when the pointer is provably stale.
```

**Verify:** Select text on Tab A, ask a deictic question; switch to Tab B with nothing selected, ask "what's selected" → does NOT report Tab A's selection; no stale element is matched.

---

### Task 3: Omnibox-class URL fallback (no accessibility mode required)

**Goal:** Make the browser URL cache guard actually fire when Chrome accessibility mode is off (the
common case), so same-process tab navigations within the 2s TTL stop being served from cache.
Fixes R4 / Symptoms 2 & 4.
**Files:** `session_context.py`, `config.py`.
**Depends on:** none.

```prompt
Read @jarvis/docs/DIAGNOSIS.md §2.B and §4/Rank 4.

`_get_browser_url(hwnd)` in session_context.py walks the UIA tree depth-3 for an Edit control whose
name hints at the address bar. With Chrome accessibility mode off it returns "" — and
`screen_read_fresh()` skips the URL check entirely when `cached_url == ""`. The tightest
browser-specific cache guard is therefore a no-op in the common case.

Add a non-accessibility fallback inside `_get_browser_url()`: if the UIA walk yields no URL, try to
read Chrome's omnibox via its child window class. Enumerate child windows of `hwnd`
(win32gui.EnumChildWindows) looking for the omnibox edit control (class typically
`Chrome_OmniboxView` or a `Chrome_WidgetWin_*` descendant containing an Edit). If found, read its
text via WM_GETTEXT / GetWindowText. This still won't expose a full URL on every Chrome build, so
return whatever identifier you get (it only needs to be a stable per-tab discriminator, not a
canonical URL). If nothing is readable, return "" as today.

Separately, fix the no-op asymmetry: when the FRESH read produces a non-empty URL but the cached
entry has `cached_url == ""`, treat that as a content change (return False / cache miss) rather
than silently passing. Rationale: gaining URL visibility between reads usually means the tab/page
changed. Keep the existing positive check (both non-empty and differing → miss).

Add a config flag CHROME_OMNIBOX_URL_FALLBACK = True so this can be disabled if it misbehaves on a
given Chrome build.
```

**Verify:** With Chrome accessibility mode OFF, read page A, switch to page B in the same window within 2s, ask a question → cache MISS (page B is re-read), not a stale hit on page A.

---

### Task 7: Local-first answers for high-confidence STRUCTURE queries

**Goal:** Stop routing every ANSWER query to Gemini. Answer high-confidence on-screen-text queries
locally; reserve Gemini for low-confidence, visual, and action work. Fixes R2 / Symptom 3.
**Files:** `gemini.py`, `config.py`, `router.py` (read), `local_llm.py`.
**Depends on:** none.

```prompt
Read @jarvis/docs/DIAGNOSIS.md §2.E and §5/P2.

Today the only non-Gemini answer path is (Intent.NO_CONTEXT AND PREFER_LOCAL_NO_CONTEXT=True), plus
the Gemini-unreachable fallback. There is no primary local path for STRUCTURE answers, so ~100% of
ANSWER queries hit Gemini and ~80% degrade to local_fallback under rate limiting.

Add PREFER_LOCAL_STRUCTURE = True to config.py. In gemini.ask_stream(), BEFORE the Gemini path
(near the existing PREFER_LOCAL_NO_CONTEXT branch around gemini.py:560), add a branch:

  if route_result.intent == Intent.TEXT  # (ANSWER, STRUCTURE)
     and config.PREFER_LOCAL_STRUCTURE
     and route_result.perception is not None
     and route_result.perception.elements
     and max(e.calibrated_confidence for e in route_result.perception.elements) >= config.ESCALATE_CONF
     and not route_result.classify.needs_focus:   # deictic/focus queries still want the strong model
        -> build the same prompt (history + focus + screen block) and call
           local_llm.complete_text(...) with LOCAL_ANSWER_TIMEOUT_MS.
        -> stream/yield the local answer; set answer_source so telemetry records "local_answer"
           (add this as a distinct value, separate from "local_fallback").
        -> If the local answer is empty, shorter than a small floor (e.g. < 15 chars), or the local
           call times out/errors, DO NOT fail — fall through to the normal Gemini path.

Leave PIXELS/VISUAL, ACT, low-confidence, and needs_focus queries on the Gemini path unchanged.
Confirm telemetry's answer_source enum/handling accepts the new "local_answer" value.
```

**Verify:** A high-confidence STRUCTURE query (clear native-window text, conf ≥ 0.6) is answered with `answer_source="local_answer"` and makes zero Gemini calls; a low-confidence or visual query still goes to Gemini.

---

### Task 8: Window-continuity gate on history injection

**Goal:** Stop prior-turn answers about a different window/page from outweighing the current (often
sparse) screen perception. Fixes R5 / Symptom 1.
**Files:** `session_context.py`, `gemini.py` (caller).
**Depends on:** none.

```prompt
Read @jarvis/docs/DIAGNOSIS.md §2.D and §5/P5.

session_context.to_prompt_block() injects the last MODEL_HISTORY_TURNS (6) turns unconditionally,
with no check that those turns happened on the same window/page as the current query. When current
perception is weak, the history block (up to ~1600 chars) outweighs it and the model anchors on
what it said about a previous page.

1. Persist a lightweight window signature per stored turn. When add_turn() records a turn, also
   store the current target's signature: (process, app_class.value, title) — or, once Task 6 lands,
   the CDP/omnibox URL when available. Add the field without breaking existing stored turns (treat
   missing signature as "unknown", which never matches).
2. Add a parameter `current_window_sig` to to_prompt_block(). For each history turn whose stored
   signature differs from current_window_sig, EITHER omit it from the History block OR keep it but
   prefix the line with "[different window] " so the model can discount it. Default behavior:
   prefix-and-keep for the immediately preceding turn (conversational continuity), omit for older
   cross-window turns. Make the strategy a small config flag HISTORY_CROSS_WINDOW = "annotate" |
   "drop" (default "annotate").
3. Update the gemini.py caller to pass the current window signature derived from the active target.

Keep the existing guarantee that raw screen text is never stored in history entries.
```

**Verify:** Ask about code in an editor, switch to a YouTube tab, ask a question → the editor turns are dropped or marked `[different window]` and the model answers about YouTube without mentioning the code.

---

### Task 9: Single-stream pre-answer escalation

**Goal:** Stop streaming two sequential answers when escalation fires; decide escalation before the
first (and only) stream. Fixes R6 / Symptom 1.
**Files:** `main.py`, `router.py` (read), `telemetry.py`.
**Depends on:** none.

```prompt
Read @jarvis/docs/DIAGNOSIS.md §3/D3 and §4/Rank 6.

In main.py `_answer_worker()` the current order is: _stream_answer(route_result) ->
should_escalate()? -> escalate_route() -> _stream_answer(escalated). The user sees two answer
streams.

Restructure to escalate-then-stream-once:
  route_result = router.route(...)
  if not cancel and router.should_escalate(question, route_result):
      escalated = router.escalate_route(...)
      if escalated and escalated.perception:
          route_result = escalated            # adopt the better result
  self._stream_answer(session_id, question, route_result, cancel_event)   # exactly one stream

Preserve the escalation cap and the cancel_event checks at each step. Ensure exactly one
_stream_answer call executes per turn for the ANSWER path.

Telemetry fix (D7): the single primary query record must carry escalated=True when escalation
fired (and the rung it escalated to), instead of logging a separate second record that the harness
never reads. Update build_record/log_query so escalated/escalated_rung are set on the one record
emitted per query. Remove or fold in the orphan second log_query call.
```

**Verify:** A low-confidence query that triggers escalation produces exactly one answer stream in the UI, and its telemetry record has `escalated=true` with a non-null `escalated_rung`.

---

### Task 10: Per-turn perception visibility + telemetry escalation field

**Goal:** Make the opaque pipeline observable per turn (rung, app-class, confidence, latency, cache
state) in the UI and in telemetry. Fixes R7 / Symptom 4.
**Files:** `ui.py`, `config.py`, `telemetry.py`, `main.py`, `debug_overlay.py`.
**Depends on:** 9 (telemetry field fix).

```prompt
Read @jarvis/docs/DIAGNOSIS.md §3/D7 and §4/Rank 7 and §5/P7.

The pipeline gives the user no signal about which rung ran, whether cache hit, or how confident
perception was. DEBUG_OVERLAY is off and only writes PNGs.

1. UI footer badge: in ui.py, render a compact status line under each answer bubble:
   "[<rung> · <app_class> · <max_conf>.2f conf · <latency>.1fs · cache <hit|miss>]". Source these
   from the RouteResult / ScreenModel / telemetry record already computed for the turn; thread the
   values through to the bubble render call. Keep it visually subordinate (small, muted) to the
   answer text.
2. Debug overlay: add a DEBUG env flag (read in config.py). When DEBUG=True, default
   DEBUG_OVERLAY=True so element-box overlays are saved during development without editing config.
   Do not change the production default (False).
3. Confirm the Task 9 telemetry fix actually persists escalated/escalated_rung; add answer_source,
   rung_reached, app_class, used_cache to the badge data path if not already present.

Keep all of this read-only with respect to perception logic — it only surfaces existing values.
```

**Verify:** Each answer bubble shows a footer like `[OCR · chromium_electron · 0.54 conf · 2.1s · cache miss]`; with `DEBUG=1` set, overlay PNGs appear in `~/.jarvis/debug/`.

---

### Task 11: Chrome eval fixtures + per-app-class perception quality

**Goal:** Give the eval harness real Chrome cases so `perception_quality` stops vacuously reporting
1.0 and Chrome regressions become measurable. Supports R1/R7 validation.
**Files:** `eval/harness.py`, `eval/cases/` (new fixtures).
**Depends on:** none (but most useful after Task 4/5 to measure the CDP win).

```prompt
Read @jarvis/docs/DIAGNOSIS.md §3/D6 and the Telemetry Analysis section.

eval/cases/ is empty, so perception_quality is computed over zero fixtures and always reports 1.0.
There is no per-app-class breakdown and the fixture path always passes uia=[] to fuse().

1. Capture 3–5 real Chrome frame fixtures using the harness's --dump-frame path (YouTube home, an
   article page, a page with a form). For each, write the .json sidecar with:
   "app_class": "chromium_electron" and "required_substrings": [<visible text that MUST be read>].
2. Add a couple native_win32 fixtures as a control.
3. In run_frame_fixture()/the metrics aggregation, add a per_class_quality dict to EvalMetrics
   keyed by app_class, and report perception_quality both globally and per app_class.
4. If/when the CDP backend (Task 4) exists, allow a fixture to declare which backend produced its
   elements so the harness can replay CDP fixtures too (don't hardcode uia=[] for non-UIA classes;
   route fixture elements through the backend the fixture names).

Document in a short README in eval/cases/ how to capture a new fixture and fill the sidecar.
```

**Verify:** `python -m eval.harness` reports a non-trivial `perception_quality` for `chromium_electron` (not 1.0) computed over the new fixtures, with a per-class breakdown.

---

## Phase 2 — Structural Chrome backend (high leverage, flag-gated)

### Task 4: CDP perception backend for Chrome

**Goal:** Replace flat OCR for Chrome with real DOM structure — semantic roles, text without OCR
noise, the active-tab URL, and selection. Fixes R1 / Symptoms 1,2,3,4 (the highest-leverage change).
**Files:** new `adapters/cdp_adapter.py`, `screen_model.py` (read), `config.py`, `requirements.txt`.
**Depends on:** none (but lands behind a flag; wire-in is Task 5).

```prompt
Read @jarvis/docs/DIAGNOSIS.md §2.A and §4/Rank 1 and §5/P1.

Chrome is classified CHROMIUM_ELECTRON → perception_policy sets run_uia=False → the only text
source is Tesseract OCR of a screenshot. The model gets a flat list of text blobs: no hierarchy, no
roles, no URL. Build a Chrome DevTools Protocol (CDP) backend as a new adapter that produces real
ScreenElements.

Scope of this task = the adapter only (no policy wiring — that's Task 5):

1. Add config:
   USE_CDP = False                 # master flag; backend is dark until Task 5 enables it per env
   CDP_DEBUG_PORT = 9222           # Chrome --remote-debugging-port
   CDP_CONNECT_TIMEOUT_MS = 500    # fail fast → caller falls back to OCR
2. Implement adapters/cdp_adapter.py with read_cdp(target) -> list[ScreenElement]:
   - Connect to the local CDP endpoint (http://localhost:<CDP_DEBUG_PORT>/json to list targets;
     pick the active page target matching the foreground tab by title/URL when resolvable).
     Use a lightweight client (pychrome or a thin websocket-client wrapper — prefer the smallest
     dependency; add it to requirements.txt). Hard-timeout the connect at CDP_CONNECT_TIMEOUT_MS.
   - Pull the accessibility tree (Accessibility.getFullAXTree) AND/OR DOM via DOMSnapshot for
     geometry. Map each meaningful node to a ScreenElement: role from AX role, text from name/value,
     bounds from layout rects converted to virtual-desktop pixels (account for device pixel ratio
     and the tab's content offset within the window — reuse the existing coordinate-normalization
     conventions so CDP bounds share the same space as OCR/UIA). Preserve parent/child containment
     if ScreenElement supports it.
   - Set source="cdp" on every element. Set a raw confidence of 1.0 (the DOM is authoritative);
     calibration multiplier is added in config in Task 5.
   - Surface the active tab URL and selected text (DOM.getDocument / Runtime.evaluate
     "window.getSelection().toString()") on the returned model or via helper functions
     get_cdp_url(target) / get_cdp_selection(target) for Tasks 6/2 to consume.
3. Robustness: if Chrome was not launched with --remote-debugging-port, or no matching target is
   found, or anything times out/raises → return [] (and log once at debug). The caller must be able
   to fall back to OCR cleanly. NEVER block the perception hot path beyond the timeout.
4. Note in a module docstring that enabling CDP requires launching Chrome with
   `--remote-debugging-port=9222`; document this and consider detecting its absence to emit a
   one-time actionable log line.

Add ("cdp", "chromium_electron"): 0.9 (and a generic ("cdp", None): 0.9) to ADAPTER_RELIABILITY in
config.py so calibrated_confidence is high when CDP succeeds.
```

**Verify:** With Chrome launched `--remote-debugging-port=9222` and `USE_CDP=True`, calling `read_cdp(target)` on a YouTube page returns structured elements (link/heading/button roles) with correct bounds and a non-empty URL; with the flag off or Chrome launched normally, it returns `[]` within the timeout.

---

### Task 5: Wire CDP into the perception policy with OCR fallback

**Goal:** Make CHROMIUM_ELECTRON perception try CDP first and fuse/fall back to OCR+CV, behind the
feature flag. Completes R1.
**Files:** `perception_policy.py`, `perception.py`, `config.py`.
**Depends on:** 4.

```prompt
Read @jarvis/docs/DIAGNOSIS.md §5/P1 (target architecture) and §4/Rank 1.

With the CDP adapter (Task 4) built, wire it into the perception ladder for Chrome, gated by
USE_CDP, with OCR as the guaranteed fallback.

1. perception_policy.py: add a run_cdp boolean to PerceptionPolicy and an AdapterOrder.CDP_FIRST
   value. For the chromium_electron policy, when config.USE_CDP is True, set run_cdp=True,
   order=CDP_FIRST, ladder_entry unchanged-or-"CDP"; keep run_ocr=True, run_cv=True, run_uia=False.
   When USE_CDP is False, the policy is exactly as today (no behavior change → safe to merge).
2. perception.py: in the fusion path, when policy.run_cdp, call cdp_adapter.read_cdp(target) and
   include its elements in the fuse() inputs (alongside OCR and CV). Fusion already merges by
   spatial overlap + text similarity; CDP elements should win role/text where they overlap OCR
   (higher calibrated confidence handles this). If read_cdp returns [] (Chrome not in debug mode,
   timeout, no target), the fusion proceeds with OCR+CV exactly as today — no hard dependency on
   CDP succeeding.
3. Do not run CDP for non-CHROMIUM_ELECTRON classes.
4. Keep the OCR/CV path fully intact as fallback; the only goal is "CDP first, OCR/CV always
   available."

Add a short note that CDP-vs-OCR for the same page should be visible via the Task 10 footer badge
(rung shows "CDP" when CDP contributed).
```

**Verify:** With `USE_CDP=True` and Chrome in debug mode, a Chrome query's perception is dominated by CDP elements (structured roles in `to_prompt_block()`, footer badge shows CDP) and OCR confidence no longer drives escalation; with Chrome in normal mode, the same query degrades gracefully to OCR exactly as before.

---

### Task 6: CDP-sourced URL identity for the screen-read cache

**Goal:** Use the authoritative CDP active-tab URL as the cache identity key for Chrome, eliminating
same-process tab-collision cache hits at the source. Strengthens R4 beyond the Task 3 fallback.
**Files:** `session_context.py`, `router.py` (read), `config.py`.
**Depends on:** 4, (3 for the non-CDP fallback path).

```prompt
Read @jarvis/docs/DIAGNOSIS.md §2.B, §4/Rank 4, and §5/P4.

The browser cache guard keys page identity on a UIA-walked URL that is usually "" (Task 3 adds an
omnibox fallback). When CDP (Task 4) is available it provides the canonical active-tab URL — use it
as the primary content key.

In session_context.py screen_read_fresh() and set_screen_read():
1. When the target is CHROMIUM_ELECTRON and config.USE_CDP, source the content URL from
   cdp_adapter.get_cdp_url(target) instead of the UIA walk. Store it as content_url on the cache
   entry at write time, and compare it at validation time.
2. Resolve the existing no-op asymmetry consistently with Task 3: empty-cached + non-empty-fresh
   URL → cache miss. With CDP the URL is reliably non-empty, so a tab switch now always changes
   content_url → guaranteed miss on the new tab.
3. Order of precedence for the URL: CDP (when USE_CDP and available) → omnibox fallback (Task 3) →
   UIA walk → "". The cache guard should treat any non-empty change as a content change.

This is the structural fix that makes the 2s browser TTL safe even on fast tab switches. Keep
behavior identical when USE_CDP is False (Task 3's fallback governs).
```

**Verify:** With CDP enabled, read tab A, switch to tab B (same window, distinct URL), immediately query within the 2s TTL → cache miss keyed on the differing CDP URL; re-querying the unchanged tab A within TTL still hits cache.

---

## Dependency summary

```
Phase 1 (independent — land first):
  1 ─ 2          (follow-up recapture → stale-state guard)
  3              (omnibox URL fallback)
  7              (local-first STRUCTURE answers)
  8              (history window-continuity gate)
  9 ─ 10         (single-stream escalation → telemetry/UI visibility)
  11             (Chrome eval fixtures)

Phase 2 (structural, flag-gated):
  4 ─┬─ 5        (CDP adapter → policy wiring)
     └─ 6        (CDP URL → cache identity; also benefits from 3)
```

Critical path to fixing the primary use case (72% of queries are Chrome-class):
**4 → 5** (real Chrome structure) is the single highest-leverage change. Ship Phase 1's **7**
(local-first answers) and **1/3** (follow-up + URL identity) first for immediate relief on
rate-limits and context bleed while the CDP backend is built behind its flag. Tasks 9/10/11 make
the remaining work measurable and observable.
