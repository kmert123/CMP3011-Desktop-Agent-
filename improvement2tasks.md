# Jarvis — Reasoning-Agent Upgrade: Implementation Plan

Goal of this plan: move Jarvis from a deterministic, active-window-scoped automation tool to a
reasoning-based desktop agent with full-screen-aware perception, hybrid routing, a unified screen
model, semantic action grounding, and a local-model tier — without destabilizing the working
prototype.

The work is spec-first. **Task 1 writes `docs/AGENT_UPGRADE_SPEC.md`**; every later prompt references
its sections so shared definitions (the `ScreenElement`/`ScreenModel` schema, the routing JSON
contract, the confidence model) live in one place and aren't re-described per task.

## Phases & critical path

```
Phase 0  Spec + safety net          Task 1, 2
Phase 1  Fix self-perception        Task 3 → 4 → 5
Phase 2  Unified screen model       Task 6 → {7,8,9} → 10 → 11 → 12
Phase 3  Hybrid routing             Task 13 → 14 → 15 → 16
Phase 4  Closed-loop actions        Task 17 → 18
Phase 5  Cache, local vision, fallback   Task 19 ; 20 ; 21
```

**Critical path:** 1 → 6 → 10 → 11 → (17, 12). The unified screen model (Task 10/11) is the substrate
that routing, grounding, and verification all depend on — build it before the clever bits.

**Parallelizable:** Task 2 (evals) runs alongside everything and is _re-run_ after Task 15.
Adapters 7/8/9 split across agents after Task 6. Task 13 (local LLM client) can start any time after
Task 1.

**Re-run evals (Task 2) after Tasks 15, 16, 18** — those are the changes most likely to regress.

---

### Task 1: Architecture spec

**Goal:** Single source of truth that all later task prompts cite.
**Files:** `docs/AGENT_UPGRADE_SPEC.md` (new).
**Depends on:** none.

```prompt
Create `docs/AGENT_UPGRADE_SPEC.md`. This is a terse reference, not prose — bullet lists and code
blocks. Write these sections:

§0 Glossary + conventions. All bboxes are (x, y, w, h) in SCREEN coordinates. "Target" = the window
   the user cared about when they invoked Jarvis, which is NOT necessarily the foreground window.

§1 Target window. Why foreground-at-perception-time is wrong (Jarvis takes focus on interaction).
   Rule: capture the target at WAKE time and reuse it for follow-ups. Jarvis must never perceive
   itself.

§2 Schemas (dataclasses):
   PerceptionTarget: hwnd:int, pid:int, process:str, title:str, bounds:tuple[int,int,int,int],
       is_self:bool
   ScreenElement: id:str, role:str, text:str, bbox:tuple[int,int,int,int], source:str
       ("uia"|"ocr"|"cv"|"vision"), confidence:float, invokable:bool, handle:object|None
   ScreenModel: target:PerceptionTarget, elements:list[ScreenElement], full_text:str,
       captured_at:float, screen_hash:str

§3 Perception adapters: 3.1 UIA, 3.2 OCR, 3.3 CV, 3.4 Vision. One short paragraph each: input,
   output (list[ScreenElement]), default confidence, when used.

§4 Fusion. Merge adapter outputs by spatial IoU. Priority: UIA structure > OCR text > CV layout.
   Confidence-weighted dedup. Output one ScreenModel.

§5 Routing. 5.1 local-LLM-first. 5.2 hybrid (regex fast-path for high-confidence + safety-critical;
   LLM otherwise; regex fallback if LLM down). 5.3 confidence-based escalation (replaces looks_blind).
   Routing JSON contract:
     {"intent": "ACTION|VISUAL|TEXT|NO_CONTEXT", "entry_rung": "WINDOW|UIA|OCR|VISION|null",
      "action_params": {...}|null, "confidence": 0.0-1.0}
   Keep the existing "default bias TEXT" safety rule.

§6 Action grounding + verification. Resolve target against ScreenModel; require confidence + invokable;
   re-perceive after acting to confirm a state delta. Open-loop → closed-loop.

§7 Caching. screen_hash (dHash) + process as freshness key, TTL as ceiling.

§8 Local vision backend + privacy. Honor VISION_BACKEND; state when pixels leave the device.

§9 Eval harness. Fixtures + offline scoring of intent/rung/action.
```

**Verify:** File exists; sections §0–§9 present; schemas compile if pasted into a `.py` scratch file.

---

### Task 2: Eval harness + telemetry bootstrap

**Goal:** Make routing/perception changes measurable before changing behavior.
**Files:** `evals/run_evals.py`, `evals/from_telemetry.py`, `evals/fixtures/` (new).
**Depends on:** Task 1.

```prompt
Read @docs/AGENT_UPGRADE_SPEC.md §9 and §5.

Build an offline eval harness. Fixture = a JSON file in evals/fixtures/:
  {"query": str, "screenshot_path": str|null, "expected_intent": str,
   "expected_rung": str|null, "expected_action": {...}|null}

evals/run_evals.py:
- Load all fixtures. For each, call the router/classifier in a DRY mode (no Gemini, no real actions).
- Score: intent accuracy, entry_rung accuracy, action-param match rate. Print a per-fixture table
  and overall percentages.
- `--baseline` flag writes current scores to evals/baseline.json.
- Exit non-zero if overall intent accuracy drops below the baseline by > 2 points (regression guard).

evals/from_telemetry.py:
- Read ~/.jarvis/telemetry.jsonl, emit candidate fixtures (query + logged intent/rung as a starting
  guess) to evals/fixtures/candidates/ for a human to correct. Skip records with no query.

Hand-write 5 starter fixtures covering the known failures, including
  {"query": "type this into the chat", "expected_intent": "ACTION", ...}.
```

**Verify:** `python evals/run_evals.py` prints a scored table on the 5 fixtures. `--baseline` writes baseline.json.

---

### Task 3: PerceptionTarget + wake-time capture

**Goal:** Snapshot the user's real foreground window at wake, before Jarvis grabs focus.
**Files:** `perception_target.py` (new), `wake_word.py`, `main.py`.
**Depends on:** Task 1.

```prompt
Read @docs/AGENT_UPGRADE_SPEC.md §1 and §2.

1. Create jarvis/perception_target.py:
   - PerceptionTarget dataclass (per §2).
   - A thread-safe module-level holder: set_pending_target(t), take_pending_target() -> PerceptionTarget|None
     (guarded by a threading.Lock; take() clears it).
   - capture_foreground_target() -> PerceptionTarget:
       hwnd = win32gui.GetForegroundWindow()
       _, pid = win32process.GetWindowThreadProcessId(hwnd)
       process = psutil.Process(pid).name()
       title  = win32gui.GetWindowText(hwnd)
       l,t,r,b = win32gui.GetWindowRect(hwnd)
       bounds = (l, t, r-l, b-t); is_self computed in Task 4 (default False here).

2. wake_word.py: the INSTANT the wake word fires, BEFORE posting anything to the UI, call
   capture_foreground_target() and set_pending_target(t). This must happen before the chat window
   can take focus.

3. main.py:
   - _voice_invocation: target = take_pending_target(); pass it down the answer path.
   - _handle_follow_up: do NOT recapture foreground (user is typing into Jarvis). Reuse the last
     target stored on the session.
   - Store the active target on session_context so follow-ups can reuse it.
```

**Verify:** Focus VSCode, say the wake word. Logged `target.process` is `Code.exe` (or similar), not `python`/jarvis.

---

### Task 4: Self-exclusion

**Goal:** Jarvis must never perceive its own window; fall back gracefully if it's the only target.
**Files:** `perception_target.py`, `perception.py`, `capture.py`.
**Depends on:** Task 3.

```prompt
Read @docs/AGENT_UPGRADE_SPEC.md §1.

1. At app startup (main.py), record Jarvis's own pid (os.getpid()) and main-window hwnd; store them
   in perception_target.py (module globals set via register_self(hwnd, pid)).

2. perception_target.py: is_self_window(hwnd=None, pid=None) -> bool. capture_foreground_target()
   sets PerceptionTarget.is_self accordingly.

3. perception.py WINDOW + UIA rungs and capture.py: if target.is_self, do NOT read it. Fall back, in
   order: most recent non-self entry in session.recent_windows → full-screen capture.
```

**Verify:** Focus the Jarvis window itself, invoke. Perception falls back to the last real app or full screen; it does not read Jarvis's own UI text.

---

### Task 5: Capture by target + full-screen option

**Goal:** Capture the target region, with a real full-screen path.
**Files:** `capture.py`, `cv_pipeline.py`.
**Depends on:** Tasks 3, 4.

```prompt
Read @docs/AGENT_UPGRADE_SPEC.md §0, §1.

capture.py:
- capture_target(target: PerceptionTarget) -> tuple[np.ndarray, tuple[int,int]]
  Returns (BGR crop, (origin_x, origin_y)). Uses mss to grab the primary monitor, then crops to
  target.bounds, clamped to monitor size. If bounds invalid or target.is_self → return full screen
  with origin (0,0).
- capture_full_screen() -> tuple[np.ndarray, tuple[int,int]].
The origin offset is needed later to convert crop-local OCR/CV boxes back to SCREEN coords.
Replace crop_to_active_window callers with capture_target.
```

**Verify:** `capture_target(vscode_target)` returns a VSCode-only image; a self-target returns the full screen with origin (0,0).

---

### Task 6: ScreenElement / ScreenModel module

**Goal:** Implement the shared schema + helpers.
**Files:** `screen_model.py` (new).
**Depends on:** Task 1.

```prompt
Read @docs/AGENT_UPGRADE_SPEC.md §2, §7.

Implement jarvis/screen_model.py:
- PerceptionTarget is imported from perception_target.py.
- ScreenElement and ScreenModel dataclasses per §2.
- ScreenModel.find(role=None, text_contains=None, invokable=None) -> list[ScreenElement]
  filtered + sorted by confidence desc.
- ScreenModel.to_prompt_block(max_tokens=400) -> str: compact lines like
  "[role] text @ (x,y,w,h)", invokable marked, truncated to budget (approx 4 chars/token).
- dhash(frame: np.ndarray, size=8) -> str: grayscale, resize to (size+1, size), compare adjacent
  cols → 64-bit hex. No new deps beyond numpy/opencv.
- hamming(a_hex, b_hex) -> int.
```

**Verify:** Construct a ScreenModel in a REPL; `find(role="button")` filters; `to_prompt_block()` stays under the cap; `hamming(dhash(a), dhash(a)) == 0`.

---

### Task 7: UIA adapter → ScreenElements

**Goal:** Emit structured elements from the UIA tree, scoped to the target.
**Files:** `adapters/uia_adapter.py` (new), refactor UIA rung in `perception.py`.
**Depends on:** Task 6.

```prompt
Read @docs/AGENT_UPGRADE_SPEC.md §3.1.

adapters/uia_adapter.py: read_uia(target: PerceptionTarget) -> list[ScreenElement].
Wrap the existing pywinauto UIA walk (respect config.UIA_MAX_DEPTH, UIA_MAX_NODES). For each node:
  role = control_type; text = name or value-pattern text;
  bbox from .rectangle() → screen coords (x,y,w,h);
  invokable = supports InvokePattern OR TogglePattern OR ValuePattern (is_editable);
  handle = the pywinauto wrapper (needed by Task 17); source="uia"; confidence=0.9.
Connect to the target hwnd, not the live foreground. Skip self (Task 4).
```

**Verify:** On Notepad/Explorer, returns named elements with real bboxes; menu/button items have `invokable=True` and a non-None `handle`.

---

### Task 8: OCR adapter with bounding boxes

**Goal:** Emit text elements with positions, in screen coords.
**Files:** `adapters/ocr_adapter.py` (new).
**Depends on:** Task 6.

```prompt
Read @docs/AGENT_UPGRADE_SPEC.md §3.2.

adapters/ocr_adapter.py: read_ocr(crop: np.ndarray, origin: tuple[int,int]) -> list[ScreenElement].
Use pytesseract.image_to_data(crop, output_type=Output.DICT). Group tokens by (block,par,line) into
text lines. Per line: text joined; bbox = union of token boxes + origin offset → SCREEN coords;
role="text"; source="ocr"; confidence = mean(token conf)/100; invokable=False.
Drop lines with confidence < config.OCR_MIN_CONF (add to config, default 0.4).
```

**Verify:** On a terminal or Electron app, returns text lines whose bboxes line up with on-screen text after the origin offset.

---

### Task 9: CV adapter (activate dormant segmentation)

**Goal:** Emit layout regions to anchor spatial structure during fusion.
**Files:** `cv_pipeline.py`, `adapters/cv_adapter.py` (new).
**Depends on:** Task 6.

```prompt
Read @docs/AGENT_UPGRADE_SPEC.md §3.3.

Activate the segmentation that's currently inactive in the hot path. adapters/cv_adapter.py:
read_cv(crop: np.ndarray, origin: tuple[int,int]) -> list[ScreenElement].
Reuse CVPipeline.segment_regions (Canny → contours → classify toolbar/content/dialog/statusbar).
Per region: role = the classification; bbox + origin → screen coords; text=""; source="cv";
confidence=0.5; invokable=False. These give layout even when UIA/OCR are sparse.
```

**Verify:** On a window with a modal dialog, a `dialog` element appears with a sensible bbox.

---

### Task 10: Fusion

**Goal:** Merge the three adapters into one deduped, confidence-weighted ScreenModel.
**Files:** `fusion.py` (new).
**Depends on:** Tasks 7, 8, 9.

```prompt
Read @docs/AGENT_UPGRADE_SPEC.md §4.

fusion.py: fuse(target, uia, ocr, cv, frame) -> ScreenModel.
- Merge by IoU > config.FUSE_IOU (default 0.6):
  * UIA ∩ OCR overlap → keep UIA element; if its text is empty, adopt OCR text; confidence=max.
  * OCR line with no UIA match but inside a CV region → keep it, role inherits the region's role.
  * CV regions with no UIA/OCR inside → keep as low-confidence layout anchors.
- full_text = reading-order concatenation of UIA/OCR text (top→bottom, left→right).
- screen_hash = dhash(frame). captured_at = time.time().
Return a single ScreenModel. Prefer structure (UIA) over raw text (OCR) over layout (CV).
```

**Verify:** Fused model on a real window has fewer, richer elements than any single adapter; invokable UIA elements keep their handles and bboxes; `full_text` reads top-to-bottom.

---

### Task 11: Integrate fusion into the perception ladder

**Goal:** Keep the cheap cascade for trivial queries; use fusion for hard ones — without breaking callers.
**Files:** `perception.py`, `router.py`.
**Depends on:** Task 10.

```prompt
Read @docs/AGENT_UPGRADE_SPEC.md §3, §4.

- Extend PerceptionResult with an optional `screen_model: ScreenModel|None`; KEEP the existing
  `.text` and `.image` fields populated (text = screen_model.full_text) so current consumers don't break.
- run_ladder: for trivial intents keep the WINDOW fast path. For VISUAL, ACTION-with-target, or
  low-confidence/ambiguous queries, run fusion mode: capture_target → uia+ocr+cv → fuse → ScreenModel.
- VISION stays terminal: attach the cropped frame + screen_model.to_prompt_block().
- Self-exclusion (Task 4) and target reuse (Task 3) apply throughout.
```

**Verify:** A "click the Save button" query triggers fusion and the result carries a ScreenModel containing the Save button element. A simple "what window am I in" still uses the WINDOW fast path.

---

### Task 12: ScreenModel → Gemini prompt + conditional image

**Goal:** Feed structured context to Gemini; attach the image only when it adds value.
**Files:** `gemini.py`.
**Depends on:** Task 11.

```prompt
Read @docs/AGENT_UPGRADE_SPEC.md §3.4.

In ask_stream: when route_result carries a screen_model, prepend screen_model.to_prompt_block() to
the text context. Attach the cropped frame ONLY when intent == VISUAL OR fusion confidence is low
(max element confidence < config.VISION_IMAGE_CONF). Otherwise send text-only (cheaper, faster, more
private). Leave the daemon-thread + queue.get(timeout=remaining) streaming machinery unchanged.
```

**Verify:** A visual query streams an answer that references on-screen elements by name; telemetry shows `image_attached=true` only for VISUAL/low-confidence cases.

---

### Task 13: Local LLM client

**Goal:** A small, fast, local model tier for routing and fallback.
**Files:** `local_llm.py` (new), `config.py`.
**Depends on:** Task 1.

````prompt
Read @docs/AGENT_UPGRADE_SPEC.md §5.

local_llm.py: thin client over Ollama HTTP (POST http://localhost:11434/api/generate, stream=false).
- complete_json(prompt: str, timeout_ms: int) -> dict|None: strips ```json fences, json.loads,
  returns None on timeout/parse error/connection error.
- complete_text(prompt, timeout_ms) -> str|None.
- Use the same daemon-thread + queue.Queue + queue.get(timeout=...) pattern as gemini.py so a hung
  local model can't block the hot path.
config.py: LOCAL_LLM_BACKEND="ollama", LOCAL_LLM_MODEL="llama3.2:3b", LOCAL_LLM_TIMEOUT_MS=1500.
````

**Verify:** With Ollama running, `complete_json` returns a dict; with Ollama stopped, it returns None within `LOCAL_LLM_TIMEOUT_MS`.

---

### Task 14: LLM router

**Goal:** Replace regex-primary classification with a local-LLM classifier that also extracts action params.
**Files:** `llm_router.py` (new).
**Depends on:** Tasks 6, 13.

```prompt
Read @docs/AGENT_UPGRADE_SPEC.md §5.

llm_router.py: route_llm(query, session, target) -> dict matching the §5 JSON contract.
Prompt the local model with: the query, session.to_prompt_block(), and target.process/title.
Do NOT send a screenshot here — this tier is text-only and fast. Instruct it to return ONLY the JSON
contract. Validate: intent in the enum, entry_rung in {WINDOW,UIA,OCR,VISION,null}, confidence float
0–1; coerce/clamp invalid values. If confidence < config.ROUTER_MIN_CONF or parse fails → bias to
intent=TEXT (the existing safety default). action_params for ACTION must include {kind, target?, text?}.
```

**Verify:** "type hello into the chat box" → `ACTION` with `action_params` containing the text and a target; "capital of France" → `NO_CONTEXT`; gibberish → `TEXT` (safe default).

---

### Task 15: Hybrid integration

**Goal:** Wire regex fast-path + LLM router + regex fallback into the live router.
**Files:** `router.py`, `classify.py`.
**Depends on:** Task 14.

```prompt
Read @docs/AGENT_UPGRADE_SPEC.md §5.2.

In router.route():
1. Run the existing regex classify FIRST as a fast-path. If it matches a HIGH-confidence rule
   (especially safety-critical action verbs you want deterministic), use it as-is.
2. Otherwise call llm_router.route_llm(). Use its result.
3. If the local LLM is unavailable (None), fall back to regex classify (current behavior).
Add telemetry fields: router_source ∈ {"regex","llm","fallback"}, router_confidence.
Keep exactly one telemetry record per query. Preserve the cache-check step.
```

**Verify:** Re-run `evals/run_evals.py`. Intent accuracy on the misrouted fixtures improves vs `baseline.json`; the regression guard passes.

---

### Task 16: Confidence-based escalation

**Goal:** Replace the brittle `looks_blind(answer)` text heuristic with perception-confidence escalation.
**Files:** `router.py`.
**Depends on:** Tasks 11, 15.

```prompt
Read @docs/AGENT_UPGRADE_SPEC.md §5.3.

Remove the looks_blind(answer) introspection. After a non-VISUAL answer, escalate ONE rung deeper iff:
  - the ScreenModel's max element confidence for the queried region < config.ESCALATE_CONF, OR
  - the query referenced a target element that find() couldn't locate in the ScreenModel.
Keep the hard cap of one escalation per query. Keep logging the `escalated` telemetry flag.
```

**Verify:** A low-confidence perception triggers exactly one escalation; a confident one does not. `escalated` still appears in telemetry. Re-run evals.

---

### Task 17: Semantic action grounding

**Goal:** Resolve action targets against the screen model before acting; refuse low-confidence targets.
**Files:** `actions.py`, `gemini.py`.
**Depends on:** Task 11.

```prompt
Read @docs/AGENT_UPGRADE_SPEC.md §6.

Before dispatch, ground the action: match action_params.target against screen_model elements by
role + fuzzy text match (rapidfuzz or difflib). Require best-match confidence ≥ config.GROUND_CONF
AND invokable == True. If no confident match → DO NOT act; return ActionResult(ok=False,
reason="target not found on screen") which the UI surfaces.
click_element uses the matched element's UIA handle: try invoke(), fall back to click_input().
Keep all existing safety gates (ACTIONS_ENABLED → whitelist → kill hotkey → confirm modal → DRY_RUN).
```

**Verify:** "click Submit" with no Submit on screen → reports not-found, does nothing. With a real Submit button → it clicks it via the grounded handle.

---

### Task 18: Closed-loop verification

**Goal:** Confirm actions actually did something; stop reporting silent no-ops as success.
**Files:** `actions.py`.
**Depends on:** Task 17.

```prompt
Read @docs/AGENT_UPGRADE_SPEC.md §6.

After dispatch, re-perceive once (a targeted UIA re-read of the affected region is enough) and confirm
the expected delta per kind:
  click_element → focus/state change, or the expected element/element-state now present;
  set_clipboard → clipboard contents equal the intended value;
  open_app      → a new window/process for the target now exists;
  notify        → no verification needed (ok=True).
Return ActionResult(ok, verified: bool, detail). If not verified → ok=False with a reason. Cap
re-perception to one attempt so a failed action can't loop.
```

**Verify:** Force a no-op (invoke on a dead element) → result is `verified=False, ok=False` with a reason, instead of a false success.

---

### Task 19: Perceptual-hash cache key

**Goal:** Make the screen-read cache reflect actual screen content, not just window title.
**Files:** `session_context.py`, `perception.py`.
**Depends on:** Task 6.

```prompt
Read @docs/AGENT_UPGRADE_SPEC.md §7.

Replace window_sig as the freshness key with (target.process, screen_hash). screen_read_fresh now
returns True iff: same process AND hamming(prev_hash, cur_hash) ≤ config.HASH_HAMMING_MAX (default 5)
AND age < SCREEN_READ_TTL. Store screen_hash alongside last_screen_read. Update all callers of
screen_read_fresh. TTL remains the absolute ceiling.
```

**Verify:** Scrolling or content change in the same window invalidates the cache; an idle, unchanged screen hits the cache within TTL.

---

### Task 20: Local vision backend + honest privacy

**Goal:** Honor `VISION_BACKEND`, fix the local/cloud inconsistency, and make the privacy modal accurate.
**Files:** `gemini.py`, `local_vision.py` (new), `config.py`, `privacy.py`.
**Depends on:** Tasks 12, 13.

```prompt
Read @docs/AGENT_UPGRADE_SPEC.md §8.

Today config says VISION_BACKEND="local" but the vision path calls cloud Gemini — resolve this.
- local_vision.py: describe_image(frame, prompt) over a local VLM via Ollama (e.g. llava / qwen2-vl).
- gemini.py / perception: if config.VISION_BACKEND == "local", route VISION through local_vision and
  do NOT send the screenshot to the cloud. If "cloud", current behavior.
- Validate VISION_BACKEND at startup; fail loudly on an unknown value.
- privacy.py: update the modal copy to state accurately when screen pixels leave the device
  (only on cloud vision / cloud text) vs stay local.
```

**Verify:** With `VISION_BACKEND=local`, a visual query is answered and telemetry shows NO cloud image call. With `cloud`, behavior is unchanged. An invalid value aborts startup with a clear message.

---

### Task 21: Local fallback + NO_CONTEXT fast path

**Goal:** Stay useful when the network or Gemini is unavailable; skip the network when context isn't needed.
**Files:** `gemini.py`, `router.py`, `local_llm.py`.
**Depends on:** Tasks 13, 15.

```prompt
Read @docs/AGENT_UPGRADE_SPEC.md §5.

- ask_stream: on Gemini deadline timeout or connection error, fall back to local_llm.complete_text for
  a degraded answer. Emit a UI flag (e.g. an "offline answer" badge via the stream_begin payload).
- For intent == NO_CONTEXT, if config.PREFER_LOCAL_NO_CONTEXT is set, answer directly from the local
  model and skip the network entirely.
- Telemetry: add answer_source ∈ {"gemini","local_fallback","local_no_context"}.
```

**Verify:** Disconnect the network → a query still returns a local answer with a visible degraded/offline flag; telemetry records `answer_source`.

---

## Notes on scope discipline

- **The eval harness (Task 2) is the thing that keeps this from becoming slower and _less_ reliable.**
  Re-run it after Tasks 15, 16, and 18. If intent accuracy or action-verification rate drops, stop and
  fix before moving on.
- **Latency budget.** Tasks 14 and 13 add a local-model call to the hot path. Measure it. If the 3B
  model is too slow on the target machine, keep the regex fast-path doing more of the work and reserve
  the LLM for the genuinely ambiguous tail.
- **Don't over-fuse.** Fusion (Task 10) is only worth its cost on hard queries. The cascade fast-path
  in Task 11 is not optional — it's what keeps trivial queries instant.
- **Per-app adapters (IDE/browser/terminal/Discord) are deliberately out of scope here.** They belong
  after the unified screen model is solid; they're specializations of Task 10's fusion, not replacements.
