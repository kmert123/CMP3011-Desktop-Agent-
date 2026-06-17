# Jarvis — Upgrade Implementation Plan

A phased, dependency-ordered task list synthesizing two rounds of improvements: (A) stability
and perception-quality fixes, and (B) a new **focus-resolution** capability so Jarvis can resolve
references like "the highlighted text" / "this" and reason about the _right_ part of the screen.

Each task is scoped to be handed to a coding agent as-is. The `prompt` block is what you paste — it
carries enough technical direction to act without re-deriving the design.

## Conventions

- **Prompts are self-contained.** There is no external spec file; everything the agent needs is
  inline. File lists are inferred from the project-context doc — if a path differs, the prompt has
  enough detail for the agent to find the right module.
- Tasks are ordered so dependencies always precede dependents. `Depends on:` is listed per task.
- Every task ends with a concrete **Verify** check. Treat a task as incomplete until it passes.
- Two cross-cutting principles run through Phase 3:
  - **Ask the OS before the AI.** Selection, cursor, and focus state are exposed by Windows
    precisely; only fall back to the VLM when no deterministic signal exists.
  - **VLM localizes, deterministic adapters extract.** Trust the VLM for _where_, never for _what_
    (it hallucinates transcribed text).

**Phases:** P1 Stabilize (1–6) → P2 Structural (7–11) → P3 Intelligence: focus resolution (12–19).
P1 fixes the three reported problems (stuck screen, fragmented text, context bleed) and is safe to
ship incrementally. P2 hardens fusion/actions. P3 builds the referring-expression capability on top.

---

## Phase 1 — Stabilize

### Task 1: Perception replay fixture harness

**Goal:** Be able to regression-test perception quality offline, so every later P1/P3 change is gated.
**Files:** `eval/harness.py`, new `eval/cases/frames/`, `telemetry.py`.
**Depends on:** none.

```prompt
The current eval harness runs classify + entry_rung only, with NO live perception — so OCR, fusion,
and full_text quality have no regression gate. Add one.

Add a "frame fixture" format: a captured frame PNG under eval/cases/frames/ plus a sidecar JSON with
target metadata (process, app_class, bounds/origin, dpi_scale) and a golden expectation (required
text substrings that must appear in the assembled model text, and a max-fragmentation assertion,
e.g. a known multi-word phrase must not be split across >1 line element).

Add a replay mode that loads a frame + metadata, runs the REAL ocr/cv adapters and fuse() against it
(no live capture), and asserts the goldens. Add a `perception_quality` metric to the release gate
alongside the existing five; fail the gate if it drops >5 points from baseline.

Add a small `--dump-frame` dev path (reuse capture.py + debug_overlay) to record new fixtures from a
live session. Commit 3–5 starter frames: one dark-mode browser page, one sidebar+content layout, one
dense native window.
```

**Verify:** Harness runs offline on committed frames. Temporarily disabling OCR upscaling (Task 2) makes the `perception_quality` metric drop and the gate fail.

---

### Task 2: OCR pipeline overhaul (upscale + dark-mode + PSM + contrast)

**Goal:** Fix fragmented, unreadable text at standard zoom — the highest-impact single change.
**Files:** `adapters/ocr_adapter.py`, `config.py`.
**Depends on:** 1 (for a fixture to validate against).

```prompt
read_ocr currently runs pytesseract on the raw BGR crop at native resolution, which fragments words
and lines at normal screen DPI. Rework the preprocessing, keeping ScreenElement bbox coords in
virtual-desktop pixels (origin add stays correct):

1. UPSCALE the crop by OCR_SCALE (config, default 2.5) with INTER_CUBIC BEFORE pytesseract. After
   reading, divide every word/line bbox (x,y,w,h) back down by OCR_SCALE, THEN add the window origin.
2. DARK MODE: compute mean luminance of the grayscale crop; if below a threshold (dark UI, e.g.
   YouTube dark), invert the image before OCR (Tesseract strongly prefers dark text on light).
3. CONTRAST: grayscale + light contrast normalization (convertScaleAbs or CLAHE) before OCR.
4. PSM: set an explicit page-segmentation mode via config OCR_PSM (default 6; expose so we can try 11
   for sparse UI text). The default PSM 3 runs its own layout analysis that fights our fusion line
   grouping.

Keep the conf>=30 word filter and line grouping. Make OCR_SCALE / OCR_PSM / dark-mode threshold
config tunables.
```

**Verify:** On the dark-mode and small-text fixtures from Task 1, lines read as coherent sentences; the debug overlay still shows boxes landing on the correct words.

---

### Task 3: Feed the model tree-structured text, not a flat pixel sort

**Goal:** Stop interleaving columns (sidebar + body) into noise in the model's input.
**Files:** `gemini.py`, `screen_model.py`, `fusion.py`.
**Depends on:** none.

```prompt
fusion.full_text is assembled by a global top-to-bottom, left-to-right sort, which interleaves
separate columns (e.g. a sidebar line at y=100 lands between two body lines). The fusion layer
already builds a containment tree (parent_id/children_ids) and ScreenModel.to_prompt_block() already
renders an indented tree.

Change the perception text that gemini.ask_stream() passes to the model to use the tree
linearization (DFS over the containment tree, siblings ordered top-to-bottom then left-to-right,
indented by depth) instead of flat full_text. Keep full_text available for cache hashing/telemetry,
but it must no longer be the primary model input. Cap by to_prompt_block(max_tokens).
```

**Verify:** On the sidebar+content fixture, the model-input text keeps each column contiguous instead of interleaving them.

---

### Task 4: Decouple perception context from conversation history

**Goal:** Stop old screens (e.g. a previous YouTube page) from anchoring answers about the current screen.
**Files:** `gemini.py`, `session_context.py`.
**Depends on:** none.

```prompt
ask_stream feeds the last ~10 turns to Gemini, and those turns currently carry their screen-perception
payload verbatim — so stale screen text keeps voting on new answers.

Separate the two streams: past turns retain ONLY the user question and the assistant answer; strip any
screen text / perception block from historical turns. Inject the CURRENT ScreenModel text fresh as the
sole perception context for this turn. Perception is always about NOW and must never be persisted into
history. Also add MODEL_HISTORY_TURNS (config) and default it lower (3–5) than the stored session
history; the stored SessionContext can keep 10, but only a few go into the prompt.
```

**Verify:** Ask about screen A → navigate to a clearly different screen B → ask a related question. The answer reflects B with no A bleed-through.

---

### Task 5: Content-based cache invalidation for browser app classes

**Goal:** Fix the cache staying warm across a full page navigation in the same Chrome window.
**Files:** `session_context.py`, `capture.py`, `config.py`.
**Depends on:** none.

```prompt
screen_read_fresh() gates on: same process AND age<SCREEN_READ_TTL AND hamming<=CACHE_HAMMING_MAX AND
density_delta<=CACHE_DENSITY_DELTA_MAX. For browsers this fails: an in-page (SPA) navigation often
emits no UIA StructureChanged event, and at 16x16 roi_dhash the masthead/sidebar are constant across
pages so hamming stays under threshold. The cache serves stale content.

For CHROMIUM_ELECTRON (and UWP webview) app_class, add a CONTENT key to the cache check: the live
window title (win32gui.GetWindowText on the stored hwnd — capture.py already re-resolves at perception
time, so this is nearly free), and if cheap, the browser address-bar/document value via a single UIA
query. If title/URL changed vs the cached entry, force a cache MISS regardless of pixel hashes. Even
SPA navigations update document.title -> window title. Also add a per-app-class TTL: keep
BROWSER_SCREEN_READ_TTL (default 2–3s) as a backstop, leave native windows at 8s.
```

**Verify:** YouTube comment view → homepage in the same Chrome window forces a fresh capture instead of returning cached text.

---

### Task 6: Typed follow-up re-capture (stale-target fix)

**Goal:** Make typed follow-ups answer about the screen as it is _now_, not at the last wake word.
**Files:** `main.py`.
**Depends on:** 5.

```prompt
_handle_follow_up() reuses the stored target from the last voice turn without re-capturing, so a typed
follow-up reasons about the foreground window as it was at the last wake word.

On follow-up: re-resolve the current foreground window. If its hwnd differs from the stored target, OR
elapsed time since the last capture exceeds FOLLOWUP_RECAPTURE_MS (config, default ~1500ms), re-capture
the target and re-run perception instead of reusing the stale ScreenModel. Otherwise the cache (Task 5)
will correctly serve or miss.
```

**Verify:** Voice query about app A → switch focus to app B → typed follow-up → answer reflects B.

---

## Phase 2 — Structural

### Task 7: Make the fusion dedup winner follow calibrated confidence

**Goal:** Close the calibration loop — actually use learned reliability where merges are decided.
**Files:** `fusion.py`, `calibration.py`.
**Depends on:** none.

```prompt
In fuse() cross-source dedup, when two overlapping elements merge (overlap>=0.4, text_sim>=60,
compatible roles), the winner that keeps role/handle/text is chosen by HARDCODED source priority
(UIA>OCR>CV>vision). But calibration.py learns per-(source, app_class) reliability into
calibrated_confidence and that learning is then ignored at the one decision that matters most.

Change the dedup winner to be the element with the highest calibrated_confidence, using the fixed
source priority only as a tie-breaker. Ensure calibrated_confidence (raw x ADAPTER_RELIABILITY[
(source, app_class)]) is computed BEFORE the dedup merge, not after — move that step earlier if needed.
```

**Verify:** With an ADAPTER_RELIABILITY table where OCR outranks UIA for some app_class, a merged element in that class takes OCR's text/handle.

---

### Task 8: `read_region(bbox)` high-resolution sub-region reader

**Goal:** Read a specific small region precisely and cheaply; foundation for focus resolution.
**Files:** new helper in `perception.py` (or `adapters/ocr_adapter.py`), `config.py`.
**Depends on:** 2.

```prompt
Add read_region(bbox, frame=None) -> {text, elements}. Crop the live (or supplied) frame to bbox,
run the Task-2 OCR pipeline at a HIGHER scale (READ_REGION_SCALE, default 3.5 — affordable because the
region is small), and return clean text plus refined element boxes in virtual-desktop pixels. This is
the on-demand high-res reader used by Phase 3 (focus resolution / agentic tools) so we never have to
upscale the whole screen to read one label.
```

**Verify:** Point read_region at a small region that whole-screen OCR fragmented; it returns clean, contiguous text.

---

### Task 9: Event-driven postcondition verification in actions

**Goal:** Replace the fixed 300ms settle with something fast when the UI is fast, patient when it's slow.
**Files:** `actions.py`, `uia_watcher.py`.
**Depends on:** none.

```prompt
execute_plan() does a fixed 300ms wait + re-read ScreenModel after each step before checking the
postcondition. Replace the fixed sleep with an event wait: after dispatch_one, wait on uia_watcher's
StructureChanged / relevant PropertyChanged for the target window, bounded by [SETTLE_MIN_MS=80,
SETTLE_MAX_MS=800] (config). If an event fires, settle briefly then re-read; if none fires before the
ceiling, fall back to the timed re-read. Then check the postcondition as today (retry<=1, undo on
failure unchanged).
```

**Verify:** A fast UI transition verifies noticeably quicker than 300ms; a slow one still waits up to the ceiling and passes.

---

### Task 10: Bias Whisper toward on-screen window/app names

**Goal:** Stop ASR from mangling the proper nouns that drive cross-window routing.
**Files:** `transcription.py`, `world_state.py`.
**Depends on:** none.

```prompt
resolve_cross_window relies on transcribing app names correctly ("paste into Slack"), but Whisper base
mangles proper nouns. world_state already holds live window names (process + title). Build a short
initial_prompt (Whisper's prompt is length-limited — keep it tight) from the current world_state
window/app-name tokens plus a small static set of common app names, and pass it to the Whisper call to
bias recognition toward exactly those tokens.
```

**Verify:** With Slack open, "paste into Slack" transcribes "Slack" correctly far more reliably than before.

---

### Task 11: Coordinate-click staleness guard for SoM clicks

**Goal:** Don't fire a coordinate click at a spot that scrolled/changed since the markers were rendered.
**Files:** `actions.py`, `set_of_marks.py`, `screen_model.py`.
**Depends on:** none.

```prompt
The UIA-handle click path re-checks liveness, but the SoM/coordinate path (_som_click -> _coord_click)
clicks bbox coordinates from the ScreenModel that may be stale if the window scrolled or navigated
between render_som and the click.

Before _coord_click: re-capture the small region around the chosen marker and compare its dhash to the
dhash captured at render_som time. If it changed beyond CLICK_STALE_HAMMING (config), ABORT the click
and re-resolve (re-render SoM or escalate) instead of clicking blind. Keep the confirm modal.
```

**Verify:** Scroll a list between SoM render and click → the click aborts/re-resolves instead of hitting the wrong row.

---

## Phase 3 — Intelligence: focus resolution

The capability: resolve what a query _refers to_ ("the highlighted text", "this", "the error
message"), extract exactly that, and reason about it — with the rest of the screen as secondary
context. Resolution runs a ladder from cheap+precise (OS selection) to expensive+fuzzy (VLM).

### Task 12: Wake-time interaction snapshot

**Goal:** Capture selection/cursor/focus the instant the wake word fires, before Jarvis steals focus.
**Files:** `perception_target.py`, `wake_word.py`, `capture.py`.
**Depends on:** none.

```prompt
At wake (BEFORE ui.show_window steals focus), extend the PerceptionTarget snapshot with interaction
state captured against the original foreground hwnd:
- cursor_pos via GetCursorPos (virtual-desktop pixels),
- focused_element: a UIA reference to the currently focused element,
- selection_text: an eager UIA TextPattern selection grab (see Task 13) — capture it now because once
  Jarvis has focus, GetFocusedElement returns Jarvis, not the app.
Store these on the target and thread them through to perception/focus stages. UIA queries against the
stored background hwnd are fine without focus.
```

**Verify:** The stored target snapshot contains cursor coords, a focused-element ref, and (when text was selected) the selection string for the pre-wake foreground window.

---

### Task 13: Selection adapter — `get_selected_text()` via UIA TextPattern

**Goal:** Answer "the highlighted/selected text" deterministically, no VLM, no guessing.
**Files:** new `adapters/selection_adapter.py`, `actions.py` (copy fallback), `config.py`.
**Depends on:** 12.

```prompt
Add get_selected_text(target) -> str|None. PRIMARY path: via UIA, find the element supporting the Text
control pattern (document/edit/text), call TextPattern GetSelection(), join the selected text ranges.
This works on a BACKGROUND hwnd without focus — prefer it.

FALLBACK (only if TextPattern is unavailable AND the query clearly needs selection): synthetic copy —
focus the target hwnd, send Ctrl+C, read clipboard, then RESTORE the prior clipboard (reuse the
clipboard save/restore already in actions.py). Note the caveat in code: this path changes focus and is
last-resort. Return None cleanly when nothing is selected.
```

**Verify:** Highlight text in Chrome / Word / a PDF viewer → returns the exact selection via TextPattern with no copy side effect. Copy fallback works in an app lacking TextPattern.

---

### Task 14: Cursor / focused-element resolution

**Goal:** Resolve deictic "this" / "this field" to the element under the cursor or in focus.
**Files:** new `focus.py` (resolver fns), `screen_model.py`.
**Depends on:** 12.

```prompt
Using the wake-time snapshot from Task 12, add:
- get_element_at_cursor(screen_model, cursor_pos): return the SMALLEST ScreenModel element whose bbox
  contains cursor_pos; if none contains it, the nearest element within CURSOR_RADIUS_PX (config).
- get_focused_element(screen_model, focused_ref): map the wake-time focused UIA element to a
  ScreenModel element (by handle identity or bbox/text match).
These resolve "what does this mean" / "this field" without any VLM call.
```

**Verify:** Hover a term and say "what does this mean" → the element under the cursor is selected as the focus.

---

### Task 15: Linguistic + spatial reference matching

**Goal:** Resolve descriptive references ("the error message", "the button at the top right").
**Files:** `screen_model.py`.
**Depends on:** none.

```prompt
Extend ScreenModel with resolve_reference(phrase) -> ranked candidates, building on find(). Support:
- descriptive matching: role filter + fuzzy text (rapidfuzz) + optional color/state hint,
- spatial predicates parsed from the phrase: top/bottom/left/right/center, "the top X", "below <X>",
  "next to <X>" (resolve the anchor element first, then apply the relation over bboxes).
Return candidates sorted by combined match score; expose the top candidate plus runners-up so the
orchestrator (Task 18) can decide whether to disambiguate.
```

**Verify:** On a test ScreenModel, "the error message" and "the button at the top right" each resolve to the correct element.

---

### Task 16: VLM referring-expression grounding (localize → extract)

**Goal:** Fallback resolver for genuinely visual/ambiguous references, without trusting VLM transcription.
**Files:** `set_of_marks.py`, `adapters/vision_adapter.py`, `focus.py`.
**Depends on:** 8.

```prompt
Reuse render_som + ask_som_marker to resolve a reference for ANSWERING (not clicking): render numbered
markers over candidate elements, ask the VLM which marker matches the user's phrase, and return that
element. Apply localize-then-extract: the VLM is trusted ONLY for WHICH region; get the precise text by
calling read_region(element.bbox) (Task 8) or reading the UIA node — never use the VLM's own
transcription. This is the resolver of last resort, after selection/cursor/linguistic paths fail.
```

**Verify:** "Summarize the highlighted paragraph" with no UIA selection available → the VLM picks the region and the text is extracted by OCR/UIA, not transcribed by the VLM.

---

### Task 17: Deictic / reference classification

**Goal:** Detect when a query needs focus resolution and route it accordingly.
**Files:** `classify.py`.
**Depends on:** none.

```prompt
Add detection of referring/deictic expressions: "this/that", "the highlighted/selected", "what I'm
pointing at", "the <thing> at the top/bottom/left/right", "more info on this", etc. Add a boolean flag
(e.g. needs_focus) to the classification result without disturbing the existing act/perception axes.
Add the obvious selection/deictic phrases to the high-confidence regex fast-path so they skip the LLM.
```

**Verify:** "Give me more info on the highlighted text" is flagged needs_focus=True; a generic "what's the weather" is not.

---

### Task 18: Focus-resolution orchestrator (the stage)

**Goal:** Tie the ladder together and make the resolved focus the PRIMARY context for answering.
**Files:** new `focus.py` (orchestrator), `router.py`, `gemini.py`, `main.py`.
**Depends on:** 13, 14, 15, 16, 17.

```prompt
Implement resolve_focus(query, screen_model, target) -> FocusResult{text, elements, bbox, source,
confidence}. Run the ladder in order, stopping at the first confident hit:
1. selection (Task 13) when the reference is selection-type ("highlighted"/"selected"),
2. cursor / focused element (Task 14) for "this"/"this field",
3. linguistic + spatial match (Task 15) for descriptive references,
4. VLM SoM grounding (Task 16) as fallback,
5. whole-screen fallback only if nothing resolves.
If the result is empty or ambiguous (multiple close candidates), do a quick SoM disambiguation or ask
the user — do NOT silently guess.

Wire into the answer path: when classify (Task 17) sets needs_focus, call resolve_focus and pass the
FocusResult as the PRIMARY context to ask_stream (framed as "the user is referring to: <text>"), with
the rest of the screen text as secondary. For this app the deictic path is the common case, so make
focus resolution the default when needs_focus is set and whole-screen the exception.
```

**Verify:** End-to-end: "give me more info on the highlighted text" extracts the selection and answers about it; "what does this mean" with the mouse over a term uses the element under the cursor.

---

### Task 19: Expose focus resolution as agentic tools to the model

**Goal:** Let the model direct its own perception — request the focused content instead of being handed everything.
**Files:** `gemini.py`.
**Depends on:** 8, 13, 14, 15.

```prompt
Add tool declarations to ask_stream that the model can call mid-answer, reusing the existing
model-escalation plumbing (run the resolver, append a function-response turn, re-invoke; respect a cap,
e.g. MAX_FOCUS_TOOL_CALLS=2):
- get_selected_text() -> the UIA TextPattern selection,
- get_element_at_cursor() -> element under the wake-time cursor,
- find_element(description) -> SoM/VLM or linguistic match, returns matched element text + bbox,
- read_region(bbox) -> high-res OCR of a sub-region (Task 8).
This makes understanding active: the model reads the query, decides it means the selected text, calls
get_selected_text(), then answers — rather than reasoning over a whole-screen dump.
```

**Verify:** Trace a query where the model first calls find_element (or get_selected_text), receives the focused text, and answers from it — visible in telemetry as a focus tool call.

---

## Suggested execution order & risk notes

- **Ship P1 incrementally.** Tasks 2, 4, and 5 individually move the needle on the three reported
  problems; none requires the others. Do Task 1 first so the rest is gated.
- **Confirm two inferences before P1.** (a) What `ask_stream` currently passes as screen text
  (full_text vs to_prompt_block) — drives Task 3. (b) Whether `_handle_follow_up` re-captures — drives
  Task 6. Both are stated from the architecture doc, not the source.
- **P3 hinges on Task 12.** The wake-time snapshot must run before Jarvis takes focus, or selection and
  focused-element resolution silently return Jarvis's own window. Get that right first.
- **Keep the "OS before AI" ordering in Task 18.** The VLM rung is the expensive, least-reliable path;
  if it starts firing on queries that selection/cursor should have caught, that's a routing bug, not a
  model-quality problem.
