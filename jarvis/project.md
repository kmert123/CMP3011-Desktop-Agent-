# Jarvis — Project Context

## What it is

Voice-activated, screen-aware desktop AI assistant for Windows. Say the wake word → it captures the foreground window (and interaction state: cursor, focus, selection) before Jarvis steals focus → understands what is on-screen via a multi-adapter perception ladder → answers or acts. Built in Python 3.12, runs entirely on the local machine except for optional Gemini API calls. The VLM and answer model are switchable between local Ollama and Gemini 2.5 Flash.

---

## Entry point and turn flow

`main.py` — `JarvisApp`. Two entry paths converge at `_answer_worker(question, target)`:

- **Voice**: wake word fires → foreground target captured (including cursor/focus/selection state, before UI shown) → audio recorded → Whisper transcription → `_answer_worker()`
- **Typed follow-up**: `_handle_follow_up()` re-captures the foreground target if the window changed or elapsed > `FOLLOWUP_RECAPTURE_MS`; otherwise reuses the stored target (cache will serve or miss correctly)

Inside `_answer_worker()`:
1. `router.route(query, session)` — classify, check cache, run perception, return `RouteResult`
2. `classify_intent(query)` — check `needs_focus`; if set, run `focus_resolver.resolve_focus()` and attach `FocusResult` to `RouteResult`
3. For ACT intents: `gemini.parse_action()` → `actions.execute_plan()` (plan + precondition/postcondition loop)
4. For ANSWER intents: `gemini.ask_stream()` (streaming) or `local_llm.complete_text()` (fallback/NO_CONTEXT)
5. Pre-answer escalation: if `RouteResult.perception` max confidence < `ESCALATE_CONF`, call `router.escalate_route()` one rung deeper
6. `session.add_turn()` → bubble rendered in UI

---

## Full pipeline

```
wake word fires
  → _capture_interaction_state() — cursor_pos, focused_element, selection_text grabbed NOW
  → PerceptionTarget captured (hwnd, pid, process, title, bounds, app_class,
                                cursor_pos, focused_element, selection_text, wake_ts)
  → router.route(query, session)
      ├─ classify_intent()         regex fast-path → local LLM → regex fallback
      ├─ entry_rung_for()          deterministic: (perception_mode, app_class) → Rung
      ├─ resolve_cross_window()    WorldState registry lookup for "paste into Slack" patterns
      ├─ session.screen_read_fresh()  dhash + density delta + title/URL + TTL cache check
      └─ run_ladder(entry_rung, policy=policy_for(app_class))
             fusion path: uia_adapter + ocr_adapter + cv_adapter → fuse() → ScreenModel
             plain path:  first rung that returns ok=True
  → focus resolution (when classify.needs_focus):
      focus_resolver.resolve_focus()
        rung 1: selection_adapter.get_selected_text() via UIA TextPattern
        rung 2: focus.get_element_at_cursor() / get_focused_element()
        rung 3: screen_model.resolve_reference() — linguistic + spatial
        rung 4: focus.resolve_reference_vlm() — SoM grounding (localize-then-extract)
      → FocusResult attached to RouteResult (injected as PRIMARY context in prompt)
  → confidence escalation (one rung deeper if max calibrated_conf < ESCALATE_CONF)
  → gemini.ask_stream() / local_llm.complete_text()
       model-driven escalation: model calls need_deeper_rung / need_image / element_not_found
       (capped at MAX_MODEL_ESCALATIONS=2)
       model-driven focus: model calls get_selected_text / get_element_at_cursor /
                           find_element / read_region
       (capped at MAX_FOCUS_TOOL_CALLS=2, independent cap)
  → session.add_turn()
  → telemetry.log_query(rung_reached, app_class, answer_source, …)

ACT path:
  gemini.parse_action() → ActionPlan (steps with precondition / expected_postcondition)
  → actions.execute_plan()
       per step: check precondition → dispatch_one → event-wait (SETTLE_MIN/MAX_MS)
                 → re-read screen → check postcondition
       retry ≤1 on postcondition failure; undo completed steps on any failure
```

---

## Module reference

### Core orchestration

**`main.py`**
App wiring: wake-word thread, voice thread, UI, event bus, session actor. Owns the single kill-hotkey binding (`Cancel` event → `actions._cancel.set()`). `_answer_worker` runs the full pipeline: route → classify → focus resolution (if `needs_focus`) → stream answer → optional pre-answer escalation. `_handle_follow_up` re-captures the foreground target on hwnd change or elapsed > `FOLLOWUP_RECAPTURE_MS`.

**`config.py`**
All constants and `.env` loading. Validates `VISION_BACKEND` at import time. Notable tunables: `ESCALATE_CONF=0.6`, `GROUND_CONF=0.6`, `GROUND_MARGIN=0.15`, `SCREEN_READ_TTL=8`, `BROWSER_SCREEN_READ_TTL=2`, `CACHE_HAMMING_MAX=10`, `CACHE_DENSITY_DELTA_MAX=0.04`, `MAX_MODEL_ESCALATIONS=2`, `MAX_FOCUS_TOOL_CALLS=2`, `CURSOR_RADIUS_PX=80`, `FOLLOWUP_RECAPTURE_MS=1500`, `SETTLE_MIN_MS=80`, `SETTLE_MAX_MS=800`, `ADAPTER_RELIABILITY` table (source × app_class multipliers).

**`core/events.py`**
Typed frozen dataclasses for all inter-thread events: `WakeEvent`, `TargetCaptured`, `TranscriptReady`, `PerceptionUpdated`, `AnswerChunk`, `AnswerDone`, `ActionProposed`, `ActionVerified`, `Cancel`. `EventBus` wraps a `queue.Queue` with an optional subscription list for UI callbacks.

**`core/session_actor.py`**
Single-writer state owner. Processes the event queue serially so session state is never mutated from multiple threads simultaneously. Handles `Cancel` events by clearing in-flight work before any action is dispatched.

---

### Classification and routing

**`classify.py`**
Two-axis classifier: `act ∈ {ANSWER, ACT}` and `perception ∈ {NONE, STRUCTURE, PIXELS}`. 22 compiled regex rules evaluated in order; first match wins. `ClassifyResult` has three notable fields:
- `high_conf` — True → skip LLM (all ACT rules + explicit PIXELS phrases like `looking_at`, `on_screen`, plus certain deictic rules)
- `needs_focus` — True → the query references a specific on-screen element by deixis ("this", "that"), selection state ("highlighted"/"selected"), cursor position ("what I'm pointing at"), or spatial phrase ("the button at the top right"). Set by a second independent pass over `_DEICTIC_RULES` (11 patterns) that annotates the main-axis result without replacing it. `_DEICTIC_HIGH_CONF` rules also set `high_conf=True`.
- Backward-compat `Intent` enum derived from axes via `Intent.from_axes()`.

**`llm_router.py`**
Calls local Ollama (`llama3.2:3b`) with a structured JSON-only prompt; 1.5 s timeout. Returns `{act, perception, confidence}` or `None` on timeout/parse failure. Optional fast-path — routing never depends on it for `entry_rung`.

**`router.py`**
Main routing entry point. Derives `entry_rung` deterministically from `(perception_mode, app_class)` — Electron/game falls back to OCR. Derives `PerceptionPolicy` via `policy_for(app_class)`. Checks `resolve_cross_window()` before running the ladder. `RouteResult` carries `act`, `perception_mode`, `perception` (PerceptionResult), `used_cache`, and `focus_result` (populated by `_answer_worker` when `needs_focus`).

---

### Perception

**`perception_target.py`**
Wake-time snapshot of the foreground window. Fields: `hwnd`, `pid`, `process`, `title`, `bounds`, `is_self`, `app_class`, `wake_ts`. **Interaction state** (captured before Jarvis steals focus): `cursor_pos` (virtual-desktop pixels from `GetCursorPos`), `focused_element` (raw `IUIAutomationElement` COM pointer — queryable against background hwnd), `selection_text` (eager TextPattern selection grab). `_capture_interaction_state()` is called by `capture_foreground_target()` before `ui.show_window()`.

**`app_classifier.py`**
`classify_app(process_name, window_class, hwnd) → AppClass`. Decision: process name lookup; `Chrome_WidgetWin_*` class check; game classes (`LWJGL`, `SDL_app`, `UnrealWindow`); flat-UIA probe (`_probe_flat_uia`) for unlisted Electron apps. Returns `AppClass ∈ {NATIVE_WIN32, CHROMIUM_ELECTRON, UWP, JAVA_SWING, GAME_FULLSCREEN, UNKNOWN}`.

**`perception_policy.py`**
`PerceptionPolicy` frozen dataclass: `run_uia`, `run_ocr`, `run_cv`, `run_vision`, `ladder_entry`. Policy table keyed by `AppClass.value`: Electron skips UIA and starts at OCR; games run VISION only; native/UWP/unknown run full UIA→OCR→CV.

**`perception.py`**
Four-rung ladder: `WINDOW` → `UIA` → `OCR` → `VISION`. `run_ladder(entry, frame, target, policy)` runs either the fusion path (all three adapters in parallel, fused into ScreenModel) or the plain path. Policy gates control which rungs are attempted.

**`adapters/uia_adapter.py`**
`read_uia(target) → list[ScreenElement]`. DFS walk via pywinauto; depth ≤ `UIA_MAX_DEPTH`, nodes ≤ `UIA_MAX_NODES`. Skips invisible rects and decorative types. Each visible named node becomes a `ScreenElement` with `role=control_type`, `confidence=0.9`, pywinauto wrapper as `handle`.

**`adapters/ocr_adapter.py`**
`read_ocr(crop, origin) → list[ScreenElement]`. Preprocessing pipeline: upscale by `OCR_SCALE` (default 2.5, `INTER_CUBIC`) → dark-mode detection (mean luminance < `OCR_DARK_THRESHOLD` → invert) → grayscale + contrast normalization → pytesseract with explicit `OCR_PSM` (default 6). Word boxes divided back down by scale, then origin-added. Also exposes `read_region(bbox, frame=None) → {text, elements}`: crops the frame to bbox, runs OCR at `READ_REGION_SCALE` (default 3.5) for high-res sub-region reads.

**`adapters/cv_adapter.py`**
`read_cv(crop, origin) → list[ScreenElement]`. Canny edge detection + contour finding. Provides spatial anchors for OCR orphan placement in fusion; never sole source for actions.

**`adapters/vision_adapter.py`**
`ask_vlm(crop, query, ask_elements) → VLMResult`. Routes to moondream (Ollama) or Gemini by `VISION_MODEL` (`"moondream"`, `"gemini"`, `"auto"`). In auto mode, moondream first; Gemini fallback when element detection is needed. Tags `backend` for per-model calibration.

**`adapters/selection_adapter.py`**
`get_selected_text(target, use_fallback=False) → str | None`. Primary: UIA TextPattern walk of `target.hwnd`, using `target.focused_element` as a hint (skips full tree scan if it supports TextPattern). Returns selected string, `""` if TextPattern found but nothing selected, `None` if no TextPattern support. Fallback (when `use_fallback=True`): synthetic `Ctrl+C` — focuses target, sends key event, reads clipboard, restores prior clipboard. Focus-stealing; last resort only.

**`fusion.py`**
`fuse(target, uia, ocr, cv, frame) → ScreenModel`. Three-step algorithm:
1. **UIA containment tree**: parent = smallest UIA ancestor containing the element's centre; assigns `parent_id`, `children_ids`, `tree_key`.
2. **Attach non-UIA**: each OCR/CV element placed under its smallest containing UIA node; orphans inherit role from covering CV region.
3. **Cross-source dedup**: merged when `overlap_fraction ≥ 0.4` AND `text_similarity ≥ 60` AND compatible roles. Winner: highest `calibrated_confidence` (source priority UIA > OCR > CV > vision as tie-breaker). `calibrated_confidence = raw × ADAPTER_RELIABILITY[(source, app_class)]` computed before dedup.

**`screen_model.py`**
`ScreenElement` dataclass: `id`, `role`, `text`, `bbox (x,y,w,h)`, `source`, `confidence`, `calibrated_confidence`, `invokable`, `handle`, `source_ts`, `parent_id`, `children_ids`, `tree_key`. `ScreenModel`: `target`, `elements`, `full_text`, `captured_at`, `screen_hash`, `stale`, `root_ids`. Key methods:
- `find(role, text_contains, invokable, within)` — ancestor-scoped search by `calibrated_confidence`
- `to_prompt_block(max_tokens)` — indented tree render (DFS, siblings top-to-bottom then left-to-right)
- `resolve_reference(phrase) → list[ReferenceMatch]` — ranked candidates combining role filter, fuzzy text (rapidfuzz), and spatial predicates parsed from the phrase (`top/bottom/left/right/center`, `below <X>`, `next to <X>`). Returns `ReferenceMatch(element, score)` sorted descending.
- `age_ms()`, hash utilities: `dhash()` (8×8), `roi_dhash()` (16×16 with volatile-region masking for cache key), `density_delta()`, `hamming()`.

**`world_state.py`**
Thread-safe multi-window `ScreenModel` registry. `update_active(model)` + `register_window(model)`. Registry eviction: TTL=120s + cap 8 entries. `find_window(name_hint)` — 3-tier case-insensitive match. `invalidate_active()` called by UIAWatcher.

**`capture.py`**
mss screen grab → BGR ndarray. `capture_target(target)` RE-RESOLVES bounds via win32gui at perception time, returns `(bgr, origin, dpi_scale, stale)`. Per-monitor DPI v2 aware.

---

### Focus resolution (Phase 3)

The focus resolution subsystem resolves what a query _refers to_ ("the highlighted text", "this field", "the error message") and extracts it precisely — injecting the resolved text as the **primary** model context rather than reasoning over a whole-screen dump.

**`classify.py` — `needs_focus` flag**
Set by the deictic pass in `classify_intent()`. 11 `_DEICTIC_RULES` detect: pure deixis (`this`/`that`), selection state (`highlighted`/`selected`), cursor deictic (`what I'm pointing at`/`under the cursor`), focused element (`focused field`/`active input`), spatial phrases (`the button at the top right`, `the top X`, `below the Y`). `_DEICTIC_HIGH_CONF` rules additionally set `high_conf=True`.

**`focus_resolver.py` — `resolve_focus()`**
`FocusResult` dataclass: `text`, `elements`, `bbox`, `source` (FocusSource tag), `confidence`, `ambiguous`, `runners_up`. `resolve_focus(query, classify_result, screen_model, target) → FocusResult` runs a four-rung ladder:

| Rung | Trigger | Mechanism | Confidence |
|------|---------|-----------|------------|
| 1 — Selection | phrase matches `_SELECTION_RE` | `selection_adapter.get_selected_text()` via UIA TextPattern | 0.95 |
| 2 — Cursor/Focus | phrase matches `_CURSOR_RE` | `focus.get_element_at_cursor()` → `focus.get_focused_element()` | 0.85/0.80 |
| 3 — Linguistic | all queries | `screen_model.resolve_reference()`, score ≥ `LINGUISTIC_MIN_SCORE=30` | score/100 |
| 4 — VLM SoM | rung 3 failed or ambiguous | `focus.resolve_reference_vlm()` over ≤`VLM_MAX_CANDIDATES=12` | 0.65 |

Ambiguity: when rung-3 runners-up are within `AMBIGUITY_MARGIN=12.0` points of the best score, `ambiguous=True` is set and rung 4 is called with only the close candidates for disambiguation. If VLM also fails, the ambiguous rung-3 result is returned for the caller to surface a clarification prompt.

**`focus.py`**
Three resolver functions:
- `get_element_at_cursor(screen_model, cursor_pos)` — smallest containing bbox; nearest centroid within `CURSOR_RADIUS_PX` as fallback.
- `get_focused_element(screen_model, focused_ref)` — three tiers: handle identity → exact COM bbox match → rapidfuzz WRatio fuzzy name+role match (min score 60).
- `resolve_reference_vlm(phrase, candidates, screen_model, target)` — localize-then-extract: fresh capture → `render_som` over candidates → `ask_som_marker` (VLM picks marker integer only) → `_refresh_element_text` re-reads actual text via `read_region()` OCR (or keeps UIA text for `source="uia"` elements). VLM is trusted **only for which region**, never for text content.

**`router.py` — `RouteResult.focus_result`**
`FocusResult` field (default `None`). Populated by `_answer_worker` when `classify_intent().needs_focus` is True, after `router.route()` completes.

**`gemini.py` — prompt injection**
`_build_initial_contents` and `_build_local_prompt` call `_format_focus_block(focus)` when `focus.is_useful()` is True. The block is injected **before** the full screen text section, which is relabeled "Additional screen context". Format: `The user is referring to: / Text: / Location: / Role: / Resolved via: / Other candidates:`.

---

### Actions

**`actions.py`**
`ActionStep` + `ActionPlan` dataclasses with `precondition` and `expected_postcondition` (`PropertyAssertion`: `element_present`, `element_absent`, `element_state`, `clipboard_equals`). `execute_plan()` runs steps sequentially: check precondition → `dispatch_one()` → event-wait (`UIAWatcher` StructureChanged/PropertyChanged, bounded `[SETTLE_MIN_MS=80, SETTLE_MAX_MS=800]`) → re-read ScreenModel → check postcondition; retry once on postcondition failure, then abort with undo of completed steps.

`click_element()` grounding priority:
1. UIA handle path: `_ground_element()` — filters `calibrated_confidence ≥ GROUND_CONF`, rapidfuzz WRatio scoring, best-to-runner-up margin ≥ `GROUND_MARGIN×100`, ancestor-scoped if `ancestor_hint` provided; then `_uia_recheck()` + `invoke()` / `click_input()`.
2. SoM fallback: `_som_click()` — staleness guard: re-captures region around chosen marker, compares dhash to render-time dhash; aborts if changed beyond `CLICK_STALE_HAMMING` (prevents clicking a scrolled-away row). Then `_coord_click()` with confirm modal.
3. Live UIA walk: fallback when no ScreenModel.

Safety gate: `ACTIONS_ENABLED` → `ALLOWED_ACTIONS` whitelist → kill-hotkey cancel → `confirm_action` modal (30s auto-deny) → `DRY_RUN`.

**`set_of_marks.py`**
`render_som(crop, elements, origin) → (annotated_bgr, markers_dict)` — filled red numbered circles (radius 14px) at element centres; returns annotated image + `{marker_num: ScreenElement}` index. `ask_som_marker(annotated_bgr, label, n_markers) → int | None` — PNG base64 to VLM, structured prompt asking for 1-based marker number. `marker_screen_center(marker_num, markers) → (cx, cy)` converts to virtual-desktop coords.

**`gemini.py`**
`ask_stream(question, route_result, session, meta) → Generator[str]` — builds a Gemini 2.5 Flash streaming call. Exposes **seven** tool declarations in two groups:

*Perception tools* (cap: `MAX_MODEL_ESCALATIONS=2`):
- `need_deeper_rung(reason)` — run one rung deeper
- `need_image(reason)` — capture and attach screenshot
- `element_not_found(query)` — re-run UIA/OCR rescan

*Focus tools* (cap: `MAX_FOCUS_TOOL_CALLS=2`, independent counter):
- `get_selected_text()` — UIA TextPattern selection
- `get_element_at_cursor()` — element under wake-time cursor
- `find_element(description)` — linguistic match → VLM SoM fallback
- `read_region(x, y, w, h)` — high-res OCR of a sub-region

Tool calls are partitioned by `_FOCUS_TOOL_NAMES` frozenset; each group is clamped to its remaining budget. Capped-out calls receive synthetic `"Tool call limit reached"` function-responses so the conversation stays structurally valid for the Gemini API. Falls back to `local_llm.complete_text()` on connection error. `parse_action(query) → ActionPlan` — structured JSON prompt.

---

### Session and state

**`session_context.py`**
`SessionContext`: last 10 turns, last 5 recent windows, screen-read cache. Cache key: `(process, screen_hash)`. `screen_read_fresh(process, hash, crop)` — four-condition gate: same process AND age < TTL (8s native, 2s browser/Electron) AND hamming ≤ `CACHE_HAMMING_MAX` AND `density_delta ≤ CACHE_DENSITY_DELTA_MAX`. Browser/Electron TTL uses `BROWSER_SCREEN_READ_TTL`; window title change forces a miss regardless of pixel hashes. `invalidate_screen_cache()` clears cache + propagates to `world_state.invalidate_active()`.

**`uia_watcher.py`**
Subscribes to UIA `StructureChanged` + relevant `PropertyChanged` on the cached target window. On event: calls `session.invalidate_screen_cache()`. Runs on a daemon thread; handlers non-blocking. Used by `execute_plan()` for event-driven postcondition settle.

---

### Telemetry and eval

**`telemetry.py`**
Append-only JSONL at `~/.jarvis/telemetry.jsonl`. Schema fields: `ts`, `query`, `intent`, `perception_rung`, `used_cache`, `escalated`, `escalated_rung`, `latency_ms`, `error`, `action_kind`, `action_verified`, `answer_source`, `router_source`, `router_confidence`, `answer_correct`, `rung_reached`, `app_class`.

**`eval/harness.py`**
Offline eval runner. Loads `eval/cases/queries.jsonl` + `eval/cases/sessions/*.json`. Runs `classify_intent` + `entry_rung_for` + **frame-fixture replay** (real OCR/CV adapters against PNG fixtures + sidecar JSON with target metadata and golden expectations — required text substrings, max-fragmentation assertions). Computes 6 metrics: `routing_accuracy`, `success_at_rung`, `grounding_prec`, `false_success_rate`, `escalation_rate`, `perception_quality`. Release gate: exits with code 1 if any of the first three drop >5 points from baseline, false-success increases >5 points, or `perception_quality` drops >5 points.

**`calibration.py`**
Reads labeled telemetry. Bayesian smoothing: `smoothed = (successes + 7) / (total + 10)`. With `--apply`, patches `ADAPTER_RELIABILITY` in `config.py` in-place.

---

### Vision and VLM

**`local_vision.py`**
Ollama HTTP client for moondream. BGR → RGB → PNG base64 → `/api/chat` POST.

**`adapters/vision_adapter.py`**
Unified VLM interface routing by `VISION_MODEL`.

---

### Infrastructure

**`wake_word.py`**
openWakeWord listener. On detection: `capture_foreground_target()` first (interaction state included), then posts `WakeEvent` + `TargetCaptured`.

**`voice.py`**
PyAudio recording. Silence detection via RMS < `SILENCE_THRESHOLD_RMS` for `SILENCE_DURATION_SEC`. Hard cap `MAX_RECORDING_SEC`.

**`transcription.py`**
Whisper (`base`, English). `build_transcription_prompt()` from `WorldState` window/app-name tokens + `TRANSCRIPTION_STATIC_APPS` — biases Whisper toward on-screen proper nouns so cross-window references like "paste into Slack" transcribe correctly.

**`ui.py`**
CustomTkinter chat window. Thread-safe event queue; bubbles streamed chunk-by-chunk. Offline badge (amber) when `answer_source == "local_fallback"`. `confirm_action(description)` — modal with 30s auto-deny.

**`privacy.py`**
First-run modal. Text adapts to `VISION_BACKEND`.

**`cv_pipeline.py`**
Standalone Canny + contour segmentation used by `cv_adapter`. Returns labelled regions (toolbar, content_area, statusbar).

**`debug_overlay.py`**
Draws every `ScreenElement`'s bbox, source tag, and confidence onto the frame. Saves to `~/.jarvis/debug/overlay_*.png`, gated by `config.DEBUG_OVERLAY`.

---

## Data flow: perception target lifecycle

```
wake_word fires
  → _capture_interaction_state() — BEFORE ui.show_window()
      cursor_pos  = GetCursorPos (virtual-desktop px)
      focused_element = UIA GetFocusedElement → raw COM pointer
      selection_text  = TextPattern.GetSelection() on the focused element
  → capture_foreground_target() stores all of the above on PerceptionTarget
      + hwnd, pid, process, title, bounds, app_class, wake_ts
  → stored as session.active_target for all turns until next wake event

perception time (run_ladder call):
  → capture.capture_target(target) RE-RESOLVES bounds from live win32 state
      returns stale=True if window gone/minimised
  → all adapters use the live crop from this moment
  → ScreenModel.captured_at = time.monotonic() at pixel grab

focus resolution (when needs_focus):
  → selection_adapter reads against stored target.hwnd + focused_element hint
  → focus.get_element_at_cursor() uses target.cursor_pos
  → focus.get_focused_element() uses target.focused_element
  → resolve_reference_vlm() re-captures a fresh crop for SoM rendering
```

---

## Coordinate system

All `ScreenElement.bbox` values are `(x, y, w, h)` in **virtual-desktop pixels** (origin = top-left of primary monitor). UIA physical rects returned by Win32 are already in screen coords. OCR crop-relative coords are converted by adding the window origin. `cursor_pos` from `GetCursorPos` is also in this space.

---

## Privacy

| Operation | Leaves device | Backend |
|---|---|---|
| UIA tree walk | No | — |
| OCR (Tesseract) | No | local binary |
| CV segmentation | No | local |
| Vision rung (moondream) | No | Ollama |
| Vision rung (gemini) | **Yes** | Google API |
| Gemini text answer | Yes (text only) | Google API |

Image is attached to Gemini only when `intent == VISUAL` or `max(calibrated_confidence) < VISION_IMAGE_CONF (0.5)`.

---

## Config quick-reference

`.env`: `GEMINI_API_KEY`

| Key | Default | Purpose |
|---|---|---|
| `GEMINI_MODEL` | `gemini-2.5-flash` | Answer model |
| `VISION_MODEL` | `auto` | `moondream` / `gemini` / `auto` |
| `VISION_BACKEND` | `local` | `local` / `gemini` — where vision rung sends pixels |
| `LOCAL_LLM_MODEL` | `llama3.2:3b` | Router classifier model |
| `LOCAL_LLM_TIMEOUT_MS` | `1500` | Router LLM call timeout |
| `LOCAL_ANSWER_TIMEOUT_MS` | `10000` | Local LLM answer timeout (fallback) |
| `SCREEN_READ_TTL` | `8` (s) | Max cache age (native windows) |
| `BROWSER_SCREEN_READ_TTL` | `2` (s) | Max cache age (Chromium/Electron) |
| `CACHE_HAMMING_MAX` | `10` | Max hash bit-flips for cache hit (256-bit) |
| `CACHE_DENSITY_DELTA_MAX` | `0.04` | Max edge-density delta for cache hit |
| `FOLLOWUP_RECAPTURE_MS` | `1500` | Re-capture threshold for typed follow-ups |
| `ESCALATE_CONF` | `0.6` | Max calibrated confidence below which to escalate |
| `GROUND_CONF` | `0.6` | Min calibrated confidence for action grounding |
| `GROUND_MARGIN` | `0.15` | Best-vs-runner-up margin required (0–1 scale) |
| `MAX_MODEL_ESCALATIONS` | `2` | Perception tool-call cap per query |
| `MAX_FOCUS_TOOL_CALLS` | `2` | Focus tool-call cap per query (independent) |
| `CURSOR_RADIUS_PX` | `80` | Nearest-centroid fallback radius for cursor resolution |
| `SETTLE_MIN_MS` | `80` | Min settle wait after UIA event in action postcondition check |
| `SETTLE_MAX_MS` | `800` | Max settle wait ceiling before timed re-read |
| `UIA_MAX_DEPTH` | `6` | UIA tree walk depth cap |
| `UIA_MAX_NODES` | `150` | UIA tree walk node cap |
| `OCR_SCALE` | `2.5` | Upscale factor for whole-screen OCR |
| `READ_REGION_SCALE` | `3.5` | Upscale factor for `read_region()` sub-region OCR |
| `OCR_PSM` | `6` | Tesseract page-segmentation mode |
| `OCR_DARK_THRESHOLD` | `128` | Mean luminance below which to invert before OCR |
| `ACTIONS_ENABLED` | `True` | Master actions switch |
| `DRY_RUN` | `False` | Log actions without executing |
| `DEBUG_OVERLAY` | `False` | Save bbox overlay PNGs |
| `MODEL_HISTORY_TURNS` | `6` | Past turns sent to model (stored session keeps 10) |

---

## How to run

```
# Ollama must be running:
#   ollama pull llama3.2:3b
#   ollama pull moondream

pip install -r requirements.txt
python main.py
```

Tesseract must be at `C:\Program Files\Tesseract-OCR\tesseract.exe`.

## Key dependencies

`openwakeword`, `pyaudio`, `openai-whisper`, `mss`, `opencv-python`, `google-genai`, `customtkinter`, `pywin32`, `pywinauto`, `pytesseract`, `keyboard`, `rapidfuzz`, `psutil`, `python-dotenv`, `Pillow`, `comtypes`
