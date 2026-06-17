# Jarvis Improvements — Implementation Plan

## Philosophy (read before any task)

Build an intelligent router, not a bigger pipeline. The goal: by the time a query reaches Gemini, the system already knows what app you're in, what's on screen as text, and what you were just doing — so the model does pure reasoning, not screenshot-parsing. Optimize for performance/cost ratio: cheapest perception that answers the question, escalate only when needed, measure everything.

## Conventions

- Package root is `jarvis/`. Run commands from the repo root.
- Every new module fails _soft_: perception/telemetry/actions never raise into the main loop.
- New deps introduced along the way: `pywinauto` (or `comtypes`), one OCR engine (`pytesseract` **or** `easyocr` — pick the lighter to install), `keyboard`.
- Each task is independently verifiable. Do them in order; dependencies are noted.

## Task order at a glance

1 Telemetry → 2 SessionContext → 3 Perception scaffolding → 4 UIA rung → 5 OCR rung → 6 Vision rung → 7 Intent classifier → 8 Router → 9 Gemini context+streaming → 10 Escalation → 11 main rewire → 12 UI streaming → 13 Actions stage 1 + safety → 14 Actions stage 2 → 15 Wire actions.

---

### Task 1: Telemetry logger

**Goal:** Per-query JSONL logging so every later claim ("60% fewer calls") is measured, not guessed.
**Files:** `jarvis/telemetry.py`, edit `jarvis/config.py`.
**Depends on:** none.

```prompt
Read jarvis/config.py to match the existing config style.

Create jarvis/telemetry.py — a tiny logging utility. Requirements:
- log_query(record: dict) appends one JSON line to a logfile. Path from config (TELEMETRY_PATH, default ~/.jarvis/telemetry.jsonl). Create the directory if missing.
- Provide a helper to build a record with these fields: ts (iso), query, intent, perception_rung, used_cache, escalated, latency_ms, error, action_kind. Missing fields default to null.
- MUST NOT raise into the caller: wrap all IO in try/except and fail silently (optionally print to stderr).
- read_recent(n) returns the last n records as dicts (for a future summary screen).
Add TELEMETRY_PATH to config.py.
```

**Verify:** Run a snippet that logs 3 fake records, then `cat` the jsonl — exactly 3 valid JSON lines, directory auto-created, no exception when the dir already exists.

---

### Task 2: SessionContext

**Goal:** Passive state that accumulates between queries and feeds every prompt cheaply.
**Files:** `jarvis/session_context.py`.
**Depends on:** none.

```prompt
Create jarvis/session_context.py with a SessionContext dataclass + methods.

Fields:
- turns: list of {role, text}, capped at ~10 (drop oldest).
- recent_windows: list of {title, process}, capped at ~5, skip if same as last (no consecutive dupes).
- last_screen_read: {text, source, window_sig, ts} or None.

Methods:
- add_turn(role, text)
- note_window(title, process)
- set_screen_read(text, source, window_sig)
- screen_read_fresh(current_window_sig) -> bool : True only if window_sig matches AND age < SCREEN_READ_TTL seconds (config, default 20). This is the staleness guard — a cached read is invalid the moment the active window changes.
- to_prompt_block() -> str : a compact context string (recent turns + current app + last screen text), hard-capped to ~400 tokens (truncate text, don't dump everything).

Keep it pure Python, no IO.
```

**Verify:** Script: add 12 turns (assert only 10 kept), note window A twice (assert one entry), set_screen_read with sig "A", assert `screen_read_fresh("A")` True and `screen_read_fresh("B")` False; print `to_prompt_block()` and confirm it's short.

---

### Task 3: Perception scaffolding + window rung

**Goal:** Define the ladder's contract and its cheapest, always-available rung.
**Files:** `jarvis/perception.py`.
**Depends on:** none.

```prompt
Create jarvis/perception.py.

Define:
- Rung enum: WINDOW, UIA, OCR, VISION.
- PerceptionResult dataclass: rung, text (str, ""), image (optional, None), window_sig (str), source (str), ok (bool).

Implement read_window() using pywin32: foreground window title + process name. Always succeeds, instant. window_sig = f"{process}:{title}". ok = bool(title).

Implement run_ladder(entry: Rung, frame=None) -> PerceptionResult:
- Always read_window() first to obtain window_sig.
- Then call providers from `entry` downward (UIA -> OCR -> VISION), stopping at the first that returns useful non-empty text; VISION always returns ok=True.
- For now, stub read_uia/read_ocr/read_vision to return ok=False so the ladder is testable end to end.
Fail soft: any provider exception -> ok=False, continue down the ladder.
```

**Verify:** Focus a known window, call `read_window()` → correct title+process printed. `run_ladder(Rung.WINDOW)` returns that result with a populated `window_sig`.

---

### Task 4: UIAutomation rung

**Goal:** Structured ground-truth text for native apps/browsers/IDE/Office — replaces guessy CV as the default.
**Files:** `jarvis/perception.py` (extend), `jarvis/config.py`.
**Depends on:** Task 3.

```prompt
Read jarvis/perception.py.

Implement read_uia() using pywinauto's UIA backend (or comtypes UIAutomation).
- Connect to the foreground window.
- Walk a SCOPED subtree only: focused element plus visible descendants, capped by UIA_MAX_DEPTH and UIA_MAX_NODES (config) to stay under ~300ms. Do NOT walk the whole tree.
- Extract control_type + name + value into compact one-line-per-element text. Skip empty/decorative nodes.
- Return PerceptionResult(rung=UIA, text=..., source="uia", ok=bool(text.strip())).
- If the app exposes no/blocked a11y tree, return ok=False (so the ladder falls through to OCR). Never crash.
Add UIA_MAX_DEPTH, UIA_MAX_NODES to config.
```

**Verify:** Open Notepad with some text and a browser with links → `read_uia()` returns readable structured lines including that text/those labels, in well under a second. Open an app that blocks accessibility → `ok=False`, no exception.

---

### Task 5: OCR rung

**Goal:** Text fallback for Electron apps, games, PDF viewers that block accessibility.
**Files:** `jarvis/perception.py` (extend), `jarvis/config.py`, requirements.
**Depends on:** Task 3.

```prompt
Read jarvis/perception.py.

Implement read_ocr(frame) using ONE engine (pytesseract or easyocr — choose the lighter to install; tesseract binary path configurable via config).
- OCR the active-window crop of `frame` (capture full screen if frame is None).
- Lazy-import the engine inside the function so app startup isn't slowed.
- Return PerceptionResult(rung=OCR, text=..., source="ocr", ok=bool(text.strip())).
Fail soft on blank/garbage input. Add OCR_ENGINE / TESSERACT_PATH to config.
```

**Verify:** Pass a screenshot of a window with visible text → recognizable words returned. Blank image → `ok=False`, no crash.

---

### Task 6: Vision rung + retire CV as primary

**Goal:** Bottom rung supplies an image to Gemini; OpenCV segmentation stops being the default path.
**Files:** `jarvis/perception.py` (extend); note `jarvis/cv_pipeline.py`, `jarvis/capture.py`.
**Depends on:** Task 3.

```prompt
Read jarvis/capture.py and jarvis/cv_pipeline.py.

Implement read_vision(frame):
- Capture the primary monitor (reuse capture.py) if frame is None.
- Crop to the active window (reuse ONLY the cropping logic from cv_pipeline.py).
- DROP region segmentation and change detection from this path — they are no longer used in the hot path. Leave cv_pipeline.py in place but stop calling segmentation.
- Return PerceptionResult(rung=VISION, image=crop, text="", source="vision", window_sig=..., ok=True).

Finalize run_ladder ordering: WINDOW -> UIA -> OCR -> VISION, where VISION always returns ok=True (terminal rung).
```

**Verify:** `run_ladder(Rung.UIA)` on a normal app returns UIA text and **no** image; `run_ladder(Rung.VISION)` returns a correctly cropped image of the active window.

---

### Task 7: Intent classifier

**Goal:** A pure, testable function that picks a bucket and a perception entry point — biased to escalate.
**Files:** `jarvis/classify.py`.
**Depends on:** none.

```prompt
Create jarvis/classify.py.

Define Intent enum: NO_CONTEXT, TEXT, VISUAL, ACTION.
classify_intent(query: str) -> ClassifyResult{intent, entry_rung, matched_rule}.

Rule-based (~20 keyword/regex rules), readable as a table:
- ACTION: leading verbs open/launch/close/click/type/press/set clipboard.
- VISUAL: "this chart", "on screen", "what am I looking at", "describe this", "this UI", "this image/diagram".
- NO_CONTEXT: "what is", "define", "write a/an", "timer", "convert", "translate", general knowledge.
- TEXT: "summarize what I'm reading", "fix this error", "explain this code", anything referencing on-screen text.

Map to entry rung: NO_CONTEXT -> None (skip perception); TEXT -> Rung.UIA; VISUAL -> Rung.VISION; ACTION -> None (handled by action layer).

CRITICAL bias rule: if no rule matches confidently, default to TEXT (entry UIA), NOT NO_CONTEXT. Erring toward more context costs a little latency; erring toward less produces silent wrong answers.
```

**Verify:** An assert table of ~12 example queries → expected intents, all pass. An out-of-vocabulary query (e.g. "thoughts on this?") defaults to TEXT.

---

### Task 8: Unified router

**Goal:** One brain: classify → reuse cache or run ladder → return enriched context, logging telemetry.
**Files:** `jarvis/router.py`.
**Depends on:** 1, 2, 3, 7 (4/5/6 for full behavior).

```prompt
Read classify.py, perception.py, session_context.py, telemetry.py.

Create jarvis/router.py:
route(query, session) -> RouteResult{intent, perception: PerceptionResult|None, used_cache: bool}.
Steps:
1. classify_intent(query).
2. Always read_window() to get the current window_sig and note it on the session.
3. If intent NO_CONTEXT or ACTION -> perception None.
4. Else if entry_rung <= UIA AND session.screen_read_fresh(window_sig) -> reuse cached read (used_cache=True, skip the ladder).
5. Else run_ladder(entry_rung, frame). If the result has text, session.set_screen_read(text, source, window_sig).
6. Never run VISION when a text rung already returned good text.
7. Log telemetry: intent, perception_rung, used_cache, latency_ms.
```

**Verify:** `route("what is a monad")` → perception None, telemetry rung=null. `route("summarize what I'm reading")` on a text app → UIA/OCR text + telemetry line. Immediate identical call on the same window → `used_cache=True` and no second ladder walk.

---

### Task 9: Gemini structured context + streaming

**Goal:** Prompt built from structured text + session; stream tokens for perceived speed.
**Files:** `jarvis/gemini.py`; adjust the UI callback signature it expects.
**Depends on:** 2.

```prompt
Read jarvis/gemini.py.

Refactor into:
- ask_stream(query, route_result, session) -> generator yielding text chunks, using Gemini stream=True.
  Prompt = session.to_prompt_block() + (route_result.perception.text if present) + the query.
  Attach an image ONLY when route_result.perception.image is present (VISUAL path).
  Instruct the model: the provided window title + on-screen text IS the screen state; reason over it directly, don't ask for a screenshot.
- ask(query, route_result, session) -> str : wrapper that joins ask_stream chunks, for non-streaming callers.
Keep API key handling unchanged.
```

**Verify:** `ask_stream` on a text-only route yields chunks that concatenate into a sensible answer with **no** image sent. On a VISUAL route, an image is included in the request.

---

### Task 10: Escalation on failure

**Goal:** If a cheap answer comes back blind, retry exactly one rung deeper — makes aggressive routing safe.
**Files:** `jarvis/router.py` (escalate helper) and/or small `jarvis/escalate.py`; `jarvis/gemini.py`.
**Depends on:** 8, 9.

```prompt
Read router.py and gemini.py.

Add looks_blind(answer: str) -> bool detecting hedges: "can't see your screen", "no screenshot", "couldn't find", "as a text model", "please share", etc.

In the answer flow: after a NO_CONTEXT or TEXT answer, if looks_blind(answer):
- escalate ONE rung deeper (None->UIA, UIA->OCR, OCR->VISION),
- re-run the ladder at the deeper rung, re-ask once,
- mark escalated=True in telemetry.
Hard cap: at most one escalation per query. VISUAL answers never escalate.
```

**Verify:** Force a NO_CONTEXT route on a query that needs the screen → it escalates once to a perception read, the second answer is grounded, telemetry shows `escalated=true`. A normal answer does **not** escalate.

---

### Task 11: main.py rewire

**Goal:** Replace the direct CV-pipeline call with router + session + telemetry.
**Files:** `jarvis/main.py`; remove `cv_pipeline` from the hot path.
**Depends on:** 8, 9, 10.

```prompt
Read jarvis/main.py.

In the answer path, replace pipeline.run(full) with:
- maintain a single SessionContext on the app instance;
- result = router.route(query, session);
- stream the answer via gemini.ask_stream(query, result, session), pushing chunks to the UI incrementally (through the existing UI queue);
- after completion, session.add_turn for both user and assistant;
- apply the escalation check from Task 10.
Wrap the whole query in a telemetry latency timer. Keep the wake-word threading and busy-lock exactly as they are. Stop importing/calling cv_pipeline in the hot path.
```

**Verify:** Run the app. "What is X" answers with no screenshot (telemetry rung=null). "What am I looking at" pulls the screen. A follow-up correctly references the prior turn. `telemetry.jsonl` grows by one line per query.

---

### Task 12: UI streaming render

**Goal:** Answers appear progressively instead of all at once.
**Files:** `jarvis/ui.py`.
**Depends on:** 9, 11.

```prompt
Read jarvis/ui.py.

Add a streaming render path: a method that creates ONE assistant bubble and appends text chunks as they arrive, marshalled through the existing thread-safe UI queue. Show the "thinking" state until the first chunk lands, then switch to appending. Keep the existing non-streaming jarvis_says for error paths.
```

**Verify:** Ask a long question — text appears progressively within ~1s, not in one block at the end. UI stays responsive.

---

### Task 13: Action layer — stage 1 + safety

**Goal:** Safe system actions behind a whitelist, confirmation, kill hotkey, and dry-run.
**Files:** `jarvis/actions.py`, `jarvis/ui.py` (confirm dialog), `jarvis/config.py`.
**Depends on:** 7.

```prompt
Read jarvis/ui.py and jarvis/config.py.

Create jarvis/actions.py — STAGE 1 ONLY (no mouse control):
- open_app(name) via subprocess, from a whitelisted name->command map in config.
- set_clipboard(text); notify(message).
Safety, non-negotiable, all in this task:
- Config flags ACTIONS_ENABLED, ALLOWED_ACTIONS (whitelist of kinds), DRY_RUN, KILL_HOTKEY.
- Before ANY action executes: ui.confirm_action(description) -> bool modal; if declined, abort.
- Global kill hotkey (keyboard lib, default ctrl+alt+esc) sets a cancel flag that aborts any pending action.
- DRY_RUN logs the intended action via telemetry and does NOT execute.
Add ui.confirm_action modal to ui.py.
```

**Verify:** Trigger `open_app("notepad")` → confirm dialog appears → confirm opens Notepad; cancel does nothing. Set DRY_RUN → only a telemetry log, no launch. Press the kill hotkey while the confirm is pending → action cancelled.

---

### Task 14: Action layer — stage 2 (semantic clicks)

**Goal:** Click named elements via accessibility labels, never coordinates.
**Files:** `jarvis/actions.py` (extend).
**Depends on:** 4, 13.

```prompt
Read actions.py and perception.py.

Add click_element(label, window=active) that locates the element in the UIA tree (by name + control_type) and invokes it via the Invoke/Click pattern — NOT pixel coordinates. Reuse the UIA walk from perception. Still gated by confirmation + whitelist + kill hotkey + dry-run. If no element matches, return a clear reason and do not crash. STOP HERE: no pyautogui/coordinate clicking.
```

**Verify:** In a simple app, "click the Submit button" → confirm → the named button is invoked. A nonexistent label → graceful failure message, no crash, no stray click.

---

### Task 15: Wire actions into router + main

**Goal:** ACTION intent flows to the action layer instead of the Gemini answer path.
**Files:** `jarvis/router.py`, `jarvis/main.py`.
**Depends on:** 8, 11, 13, 14.

```prompt
Read router.py, main.py, actions.py, gemini.py.

When classify returns ACTION:
- route to an action handler, NOT the answer path.
- Use Gemini only as a parser: one small JSON-only call that turns the natural-language command into {kind, args}. (Strip markdown fences, parse safely.)
- Execute via actions.py with all safety gates (confirm, whitelist, kill hotkey, dry-run).
- Surface the result back in the UI and as a session turn.
- Log action telemetry (action_kind, success).
Leave the answer path unchanged for non-action intents.
```

**Verify:** "Open notepad" → parsed → confirm → opens, with no vision call (telemetry shows action, not an answer). "Click Submit" → stage-2 invoke. "What is X" still answers normally via the answer path. Telemetry cleanly distinguishes action vs answer rows.

---

## Definition of done

- A no-context query never captures the screen (telemetry proves it).
- A text query is answered from UIA/OCR text, not a JPEG, for normal apps.
- Follow-ups stay coherent across turns.
- Wrong cheap-routing self-corrects via one escalation.
- Actions only fire behind confirmation + kill hotkey, stage 1 and 2 only.
- Every query writes one telemetry line you can summarize later.
