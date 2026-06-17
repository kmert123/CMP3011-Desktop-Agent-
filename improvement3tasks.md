# Jarvis — Upgrade Implementation Plan

A phased, dependency-ordered task list. Each task is scoped to be handed to a coding agent
as-is. The `prompt` block is what you paste; it carries enough technical direction to act
without re-deriving the design.

## Conventions

- `@docs/AGENT_UPGRADE_SPEC.md §N` references the design spec. Section map:
  - §1 Temporal WorldState & freshness · §2 App-class detection · §3 Confidence calibration
  - §4 Element graph & fusion · §5 Grounding · §6 Actions & verification · §7 Routing
  - §8 Model-driven escalation · §9 Cache · §10 Event core / state actor · §11 Vision/VLM
  - §12 Multi-window & policy · §13 Telemetry & eval
  - (If you haven't written the spec, the inline detail in each prompt is sufficient on its own.)
- Tasks are ordered so dependencies always precede dependents. Don't skip ahead.
- Every task ends with a concrete **Verify** check. Treat a task as incomplete until it passes.

**Phases:** P1 Stabilize (1–7) → P2 Structural (8–14) → P3 Intelligence & robustness (15–18).
P1 is safe to ship incrementally and fixes most instability. P2/P3 require P1 foundations.

---

## Phase 1 — Stabilize

### Task 1: DPI-aware coordinate normalization + debug overlay

**Goal:** Put all adapter geometry into one coordinate space; make misalignment visible.
**Files:** `capture.py`, `adapters/uia_adapter.py`, `adapters/ocr_adapter.py`, `adapters/cv_adapter.py`, `fusion.py`, `config.py`.
**Depends on:** none.

```prompt
Read @docs/AGENT_UPGRADE_SPEC.md §1.

Make the process per-monitor DPI v2 aware (SetProcessDpiAwarenessContext) at startup. Define ONE
coordinate space = virtual-desktop pixels. capture.py records the crop origin (left, top) and the
target monitor's DPI scale. Convert UIA physical rects and OCR crop-relative coords into this space;
add crop origin to OCR boxes. Establish the invariant: ScreenElement.bounds is ALWAYS normalized
virtual-desktop pixels.
Add a debug util that draws every element's box + source + confidence onto the captured frame and
saves to ~/.jarvis/debug/overlay_*.png, gated by config.DEBUG_OVERLAY.
```

**Verify:** On a multi-monitor / 150%-scaled display, the overlay shows UIA and OCR boxes landing on the correct widgets.

---

### Task 2: App-class detection

**Goal:** Classify the target window so downstream policy and calibration can adapt.
**Files:** `perception_target.py`, new `perception/app_class.py`, `config.py`, `telemetry.py`.
**Depends on:** none.

```prompt
Read @docs/AGENT_UPGRADE_SPEC.md §2.

Add classify_app(process_name, window_class, hwnd) -> AppClass in
{NATIVE_WIN32, CHROMIUM_ELECTRON, UWP, JAVA_SWING, GAME_FULLSCREEN, UNKNOWN}.
Detect Chromium/Electron via window class (Chrome_WidgetWin_*), known process names, and a
"flat/low-value UIA tree" heuristic (few nodes, generic roles). Attach app_class to the captured
target and thread it through perception + fusion. No behavior change yet beyond storing + logging it.
```

**Verify:** VS Code / Slack / Chrome → CHROMIUM_ELECTRON; Notepad / Explorer → NATIVE_WIN32; value appears in telemetry.

---

### Task 3: Co-located capture + temporal stamping

**Goal:** Eliminate the wake-time→perception-time staleness gap; timestamp all perception.
**Files:** `perception.py`, `capture.py`, `screen_model.py`, `perception_target.py`.
**Depends on:** 1.

```prompt
Read @docs/AGENT_UPGRADE_SPEC.md §1.

Move the mss pixel grab to the moment the perception ladder runs, NOT wake time. From the wake-time
target keep only {hwnd, pid, process, app_class} and RE-RESOLVE bounds at perception time (the window
may have moved/resized). Add capture_ts (monotonic ms) to ScreenModel and source_ts per element;
add ScreenModel.age_ms(). If the target window is gone/minimized at perception time, return a
ScreenModel with stale=True so the answer path can say so instead of perceiving the wrong window.
```

**Verify:** `age_ms()` is small at answer time; moving the target window between wake and end-of-speech still perceives the right region.

---

### Task 4: App-class-conditioned confidence calibration

**Goal:** Make confidences comparable across rungs so escalation fires when UIA is worthless.
**Files:** `screen_model.py`, `fusion.py`, `config.py`, new `perception/calibration.py`.
**Depends on:** 2, 3.

```prompt
Read @docs/AGENT_UPGRADE_SPEC.md §3.

Stop using hardcoded adapter confidences as comparable. Compute
calibrated_confidence = raw_confidence * reliability[(source, app_class)].
Seed the table in config (tunable): UIA×NATIVE=1.0, UIA×CHROMIUM_ELECTRON=0.4, UIA×GAME=0.1;
OCR = measured_token_conf * 0.8; CV×any = 0.2.
Store BOTH raw_confidence and calibrated_confidence on ScreenElement. Escalation (ESCALATE_CONF) and
grounding (GROUND_CONF) now read calibrated_confidence.
```

**Verify:** Pointing at an Electron app with a hollow UIA tree now drops below ESCALATE_CONF and escalates to OCR/VISION instead of answering on junk.

---

### Task 5: Region-scoped cache key + small-change sensitivity (interim)

**Goal:** Stop over-caching tiny critical changes and under-caching noisy idle screens.
**Files:** `session_context.py`, `capture.py`, `config.py`.
**Depends on:** 1.

```prompt
Read @docs/AGENT_UPGRADE_SPEC.md §9.

Change the cache key from full-frame dhash to (window_identity, dhash(target_region_crop)) at a
larger hash size (16x16). A cache hit now requires Hamming <= CACHE_HAMMING AND a low text/edge-density
delta on the ROI (cheap proxy for "small but meaningful change" — single-char edits, toggles).
Lower default TTL to 8s. If a volatile sub-region (clock/spinner/cursor) is trivially detectable,
exclude it from the hash; otherwise rely on the density delta.
```

**Verify:** Scrolling or typing one char invalidates; a video playing in a corner of an otherwise static document still hits cache for document queries.

---

### Task 6: Re-ground and re-read immediately before click

**Goal:** Never invoke a stale UIA handle.
**Files:** `actions.py`, `perception.py`.
**Depends on:** 3.

```prompt
Read @docs/AGENT_UPGRADE_SPEC.md §5,§6.

In click_element, BEFORE invoking, do a fresh targeted UIA re-read of the grounded region and confirm
the element still matches (role + text + bounds within tolerance) and is still the UNIQUE match.
If the handle is invalid (ElementNotAvailable) or the match is no longer unique, abort with
ActionResult(ok=False, reason="target changed/ambiguous before click"). Only then invoke() /
click_input(). Keep all existing safety gates.
```

**Verify:** Destroy the grounded element after grounding but before click (simulate) → aborts cleanly instead of clicking the wrong node.

---

### Task 7: Mid-stream Gemini fallback buffering

**Goal:** Recover from a connection that dies after the first chunk.
**Files:** `gemini.py`, `main.py`, `config.py`.
**Depends on:** none.

```prompt
Read @docs/AGENT_UPGRADE_SPEC.md §1.

Buffer the first GEMINI_FIRST_CHUNK_GRACE_MS (default 1200) of stream output before committing to the
gemini path / rendering bubbles. If the stream errors or stalls within that window — even after >=1
chunk arrived — discard the partial and fall back to local_llm.complete_text with the offline badge.
After the grace window, commit and stream normally. Keep answer_source telemetry accurate
(gemini | local_fallback | local_no_context).
```

**Verify:** Kill the connection right after chunk 1 within the grace window → clean local fallback, no truncated bubble.

---

## Phase 2 — Structural

### Task 8: Containment-aware element graph

**Goal:** Replace the flat fused list with a hierarchy that preserves parent/child context.
**Files:** `screen_model.py`, `fusion.py`, new `perception/spatial.py`.
**Depends on:** 1, 4.

```prompt
Read @docs/AGENT_UPGRADE_SPEC.md §4.

Extend ScreenModel to hold a containment tree: each ScreenElement gains parent/children and a
stable-ish key (role + normalized-text + relative-position hash). Replace IoU-only merge with:
(1) build containment from the UIA tree; (2) place OCR/CV evidence into its smallest containing node;
(3) merge cross-source duplicates ONLY when spatial overlap AND text-similarity (rapidfuzz) AND
role-compatibility all hold. Update to_prompt_block() to render the tree indented so the LLM sees
structure. find() must support ancestor-scoped lookup, e.g. "OK within dialog 'Save changes?'".
```

**Verify:** Two "OK" buttons in different containers are distinct nodes with different ancestors; `to_prompt_block()` shows nesting.

---

### Task 9: Context-qualified unique-match grounding

**Goal:** Refuse ambiguous action targets instead of guessing.
**Files:** `actions.py`, `gemini.py`.
**Depends on:** 8.

```prompt
Read @docs/AGENT_UPGRADE_SPEC.md §5.

Ground the action target against the element graph using role + fuzzy text + optional ancestor/context
hint from the parsed action. Require a SINGLE best match with calibrated_confidence >= GROUND_CONF AND
a margin over the runner-up >= GROUND_MARGIN (default 0.15). If ambiguous (margin too small) → return
ActionResult(ok=False, reason, candidates=[...]) so the UI can ask. If none → ok=False, reason.
This replaces the coin-flip fuzzy match.
```

**Verify:** "click OK" with two OKs → refuses with candidate list; "OK in the save dialog" → unique match, acts.

---

### Task 10: UIA event-driven cache invalidation

**Goal:** Invalidate cache precisely on real UI changes, region-scoped.
**Files:** `session_context.py`, new `perception/uia_events.py`.
**Depends on:** 5.

```prompt
Read @docs/AGENT_UPGRADE_SPEC.md §9.

Subscribe (comtypes/pywinauto UIAutomation) to StructureChanged + relevant PropertyChanged events on
the cached target window. On event, invalidate that window's cached ScreenModel/regions. Keep the
density-delta + TTL path from Task 5 as a fallback for windows that emit no events (games/Electron).
Run the listener off the hot path (dedicated thread or the state actor); handlers must be non-blocking.
```

**Verify:** A value changing in the target window invalidates cache without a full re-hash; an idle window keeps its cache.

---

### Task 11: Two-axis routing

**Goal:** Separate perception-need from action-need; drop the conflated taxonomy.
**Files:** `router.py`, `classify.py`, `llm_router.py`, `config.py`.
**Depends on:** none.

```prompt
Read @docs/AGENT_UPGRADE_SPEC.md §7.

Replace Intent {ACTION|VISUAL|TEXT|NO_CONTEXT} with two fields:
  act        in {ANSWER, ACT}
  perception in {NONE, STRUCTURE, PIXELS}
Map the 22 regex rules onto these axes; keep the regex fast-path for safety-critical / high-confidence
ACT patterns. entry_rung becomes a deterministic function of (perception, app_class), NOT an LLM guess.
NO_CONTEXT collapses to perception=NONE.
```

**Verify:** "what's 2+2" → ANSWER/NONE; "click submit" → ACT/STRUCTURE; "what does this error mean" → ANSWER/PIXELS.

---

### Task 12: Model-driven escalation via tool calls

**Goal:** Let the reasoning model pull more perception instead of relying on a blind threshold.
**Files:** `gemini.py`, `perception.py`, `router.py`.
**Depends on:** 11, 8.

```prompt
Read @docs/AGENT_UPGRADE_SPEC.md §8.

Expose tools to the answering model: need_deeper_rung(reason), need_image(reason),
element_not_found(query). On a call, run the corresponding perception step, append the new context,
and continue the answer. Cap total escalations per query at 2. Keep the calibrated-confidence
escalation (Task 4) as a pre-answer heuristic, but model-requested escalation overrides it.
Demote llama3.2:3b to an optional fast-path only; routing no longer depends on it.
```

**Verify:** A query that needs the image when only text was attached → model calls need_image → image attached → correct answer; escalation logged.

---

### Task 13: Vision-grounded click fallback (set-of-marks)

**Goal:** Make actions work on apps with no usable UIA handles.
**Files:** `actions.py`, `local_vision.py`, `gemini.py`, new `actions/vision_click.py`.
**Depends on:** 9.

```prompt
Read @docs/AGENT_UPGRADE_SPEC.md §6,§11.

When grounding finds no invokable UIA handle (Electron/game) but the action is allowed: render
set-of-marks — overlay numbered markers on detected elements (OCR/CV boxes) — and ask the VLM/Gemini
which marker matches the target. Map marker -> center point, move + click there, then verify by
re-reading. Coordinate clicks ALWAYS require the confirm modal by default. Reuse the full safety chain.
```

**Verify:** In an Electron app, "click the New File button" with no handle → set-of-marks resolves the marker → clicks the correct location → verified.

---

### Task 14: Event bus + single-writer state actor

**Goal:** Make concurrency and cancellation correct; one owner of WorldState/session.
**Files:** `main.py`, `session_context.py`, `perception_target.py`, new `core/events.py`, new `core/session_actor.py`.
**Depends on:** 3.

```prompt
Read @docs/AGENT_UPGRADE_SPEC.md §10.

Introduce an in-process event bus and a single state-actor thread that exclusively owns
session_context and the pending target. All state mutations go through messages: WakeEvent,
TargetCaptured, TranscriptReady, PerceptionUpdated, AnswerChunk, ActionProposed, ActionVerified,
Cancel. Worker threads (voice, perception, gemini, ui) only POST events. Replace the single-slot
pending target with a per-session-id queue. Wire the kill hotkey to a Cancel event that stops an
in-flight action before dispatch.
```

**Verify:** Two wake events / a follow-up mid-invocation → no interleaved turns, no clobbered target; Cancel stops the in-flight action before dispatch.

---

## Phase 3 — Intelligence & Robustness

### Task 15: VLM upgrade + set-of-marks answering

**Goal:** Replace the weakest authority (moondream) with a UI-capable grounding path.
**Files:** `local_vision.py`, `perception.py`, `config.py`.
**Depends on:** 13.

```prompt
Read @docs/AGENT_UPGRADE_SPEC.md §11.

Add a pluggable VISION_MODEL with a UI-detection front end (element/icon detector producing labeled
boxes) feeding set-of-marks to a stronger VLM, OR route grounding-critical vision to Gemini multimodal.
Keep moondream as a low-resource default but mark its outputs lower-reliability in the calibration
table (Task 4). Standardize the VLM contract: return either a text answer OR structured element refs
(marker ids / boxes) the answer + action paths can consume.
```

**Verify:** A dense UI screenshot (e.g. a settings panel) → VLM returns correct element refs usable by the answer/action path.

---

### Task 16: Planner assertions + verified action plans + undo

**Goal:** Replace ad-hoc delta checks with explicit pre/post assertions; recover on failure.
**Files:** `actions.py`, `gemini.py`.
**Depends on:** 9, 6.

```prompt
Read @docs/AGENT_UPGRADE_SPEC.md §6.

gemini.parse_action returns per step: precondition + expected_postcondition as property assertions
(element present / state / value). Execute each step: check precondition -> act -> re-read ->
check postcondition. On failure: bounded retry (<=1) then abort with reason. Where the kind supports
it, undo: set_clipboard restores prior contents; open_app closes the launched window. Multi-step
actions run as a plan and STOP at the first unverified step (no silent success).
```

**Verify:** A step whose postcondition fails → no false success, retried once, then aborted with reason; clipboard undo restores the prior value.

---

### Task 17: Multi-window WorldState + per-app perception policy

**Goal:** Support cross-window tasks and choose rungs by app class.
**Files:** `perception.py`, `perception_target.py`, `screen_model.py`, new `perception/policy.py`.
**Depends on:** 8, 2.

```prompt
Read @docs/AGENT_UPGRADE_SPEC.md §12.

Let WorldState hold >1 window's ScreenModel (active + referenced). Add a perception policy keyed by
app_class: NATIVE -> UIA-first; CHROMIUM_ELECTRON -> OCR/VISION-first (skip the low-value UIA walk);
GAME -> VISION-only. Resolve cross-window references ("paste into Slack") by locating the named window
and grounding there.
```

**Verify:** "copy this and paste into Notepad" works across two windows; an Electron target skips the useless UIA walk.

---

### Task 18: Telemetry-fed calibration + regression eval harness

**Goal:** Turn telemetry into measurable improvement; gate releases.
**Files:** `telemetry.py`, `perception/calibration.py`, new `eval/harness.py`, new `eval/cases/`.
**Depends on:** 4, 12, 16.

```prompt
Read @docs/AGENT_UPGRADE_SPEC.md §13.

Log per-query outcome labels: answer_correct?, action_verified?, escalation_used?, answer_source,
rung_reached, app_class. Build a replayable eval harness from recorded sessions (frame + transcript +
expected outcome) reporting success@rung, grounding precision, and false-success rate. Add a
calibration script that fits reliability[(source, app_class)] from labeled outcomes. Add a
release gate: the eval suite must not regress key metrics.
```

**Verify:** Harness runs offline on recorded cases and reports metrics; calibration updates the reliability table and measurably improves grounding precision.

---

## Dependency summary

```
1 ─┬─ 3 ─┬─ 4 ─┬─ 8 ─┬─ 9 ─┬─ 13 ─ 15
   │     │     │     │     └─ 16 ─┐
   │     │     │     └─ 17        │
   │     ├─ 6 ─────────┘          │
   │     └─ 14                    │
   ├─ 5 ─ 10                      │
2 ─┴─ 4, 17                       │
7  (independent)                  │
11 ─ 12 ─ 18 ◄────────────────────┘  (18 also needs 4, 16)
```

Critical path to a meaningfully more reliable agent: **1 → 3 → 4 → 8 → 9**, plus **6** for action safety
and **11 → 12** for routing. Tasks 7 and 2 can run in parallel early. Phase 3 is where it stops being a
demo, but don't start it until Phase 2's graph + grounding are solid.
