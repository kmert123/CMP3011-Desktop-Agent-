# Jarvis — Perception Blindness, Context Pollution & Model Quality Treatment Plan (Wave 7)

Treatment for the root causes in `jarvis/docs/DIAGNOSIS3.md`. Three field-report problems, six tasks.
Each task is scoped to be handed to a coding agent as-is. The `prompt` block is what you paste.

## Conventions

- `@jarvis/docs/DIAGNOSIS3.md §N` references the diagnosis. Section map:
  - §1 Problem 1 — perception blindness (F1A–F1F)
  - §2 Problem 2 — context pollution / history bleed (F2A–F2E)
  - §3 Problem 3 — model quality: local LLM + vision (F3A–F3D)
  - §4 Prioritised remediation plan (P13–P18)
- Tasks are ordered so dependencies always precede dependents. Don't skip ahead.
- Every task ends with a concrete **Verify** check. Treat a task as incomplete until it passes.
- The whole codebase lives under `jarvis/`. All file paths below are relative to `jarvis/`.
- All fixes here are **independent of CDP (DIAGNOSIS.md Tasks 4–6)**; they work on every app.

**Treatment summary (maps diagnosis → tasks):**

| Diagnosis fix | Problem | Task |
|---|---|---|
| F1A, F1B, F1C, F1D — OCR + UIA config tuning (P13) | Perception blindness | 1 |
| F1E, F2C, F2D — Honesty guard + history zero-drop (P14) | Perception blindness + context pollution | 2 |
| F2A, F2E — Demotion floor + TTL tightening (P14, partial) | Context pollution | 3 |
| F2B — Cache-hit signal surfaced to model (P17) | Context pollution | 4 |
| F1F — Content elements sorted by confidence before render (P15) | Perception blindness (truncation) | 5 |
| F3A, F3C, F3D — Local LLM upgrade + streaming + fallback notice (P16, P18) | Model quality | 6 |

**Phases:** Tasks 1–3 are CRITICAL and config-or-tiny-code changes — do them first for immediate
impact. Tasks 4–5 are HIGH and each touch one file. Task 6 is HIGH/MEDIUM; the model pull is
external (instructions included in the task).

---

## Task 1: OCR and UIA config tuning (Fixes F1A, F1B, F1C, F1D / P13)

**Goal:** Recover content that is currently being silently dropped by miscalibrated OCR thresholds
and a UIA walker that stops too early. Pure config changes — no logic altered, immediate uplift
across every app Jarvis looks at.
**Files:** `config.py`.
**Depends on:** none.

```prompt
Read @jarvis/docs/DIAGNOSIS3.md §1.3 (OCR blind) and §1.2 (UIA caps) and §4/P13.

The full-window OCR pass uses settings that were conservative defaults, not tuned values. They are
now measurably causing content to be missed:

- OCR_PSM = 6 (uniform block) is the wrong page-segmentation mode for general desktop screens.
  Most UIs are multi-column, sparse, or mixed. PSM=11 (sparse text, find-all-text-it-can) is the
  correct general choice. The content-region re-OCR pass (P9/Task 2 of wave 6) already uses PSM=11
  via OCR_PSM_CONTENT — the full-window pass must match it.

- OCR_SCALE = 2.5 is borderline for small text at 100% DPI. The re-OCR pass already uses 3.5
  (CONTENT_REOCR_SCALE). There is no reason for the full-window pass to use a lower scale — the
  cost is acceptable for a single full-screen crop.

- OCR_MIN_CONF = 0.4 silently drops whole text lines where Tesseract is less than 40% confident.
  In dark-mode apps, custom-font apps, and any app with small text, this discards real content.
  The content-region re-OCR pass uses 0.25 (OCR_MIN_CONF_CONTENT). The full-window pass should
  match that floor, not be stricter.

- UIA_MAX_NODES = 150 and UIA_MAX_DEPTH = 6 cause the UIA walker to stop before it reaches content
  nodes in any complex app. A modern Electron, UWP, or rich Win32 app easily has 300–1000+ nodes;
  the cap fires immediately and the walker returns only window-level chrome. Raising these by 2–3×
  costs a few hundred milliseconds at most and recovers structurally-exposed text that is currently
  invisible.

Make the following changes to config.py:
  OCR_PSM = 11          (was 6)
  OCR_SCALE = 3.5       (was 2.5)
  OCR_MIN_CONF = 0.25   (was 0.4)
  UIA_MAX_NODES = 400   (was 150)
  UIA_MAX_DEPTH = 8     (was 6)

No other file needs changing. Do not touch OCR_PSM_CONTENT, CONTENT_REOCR_SCALE, or
OCR_MIN_CONF_CONTENT — those are the per-pass overrides and are already correct. Do not change any
logic in ocr_adapter.py, perception.py, or fusion.py. Update the inline comments next to each
constant to reflect the rationale (one short line each).
```

**Verify:** After the config change, run a perception pass on a browser tab or VS Code window and
confirm `ScreenModel.full_text` is longer / contains more content lines than before. Confirm the
UIA walker no longer hits the node cap on a complex native app (add a temporary debug print of
`len(nodes_visited)` if needed, then remove it). No imports change and no tests break.

---

## Task 2: Honesty guard + history zero-drop on weak perception (Fixes F1E, F2C, F2D / P14)

**Goal:** Stop Jarvis from confidently answering from a partial or empty screen view. When
perception is weak, (a) the model is told it may not be able to see the target so it should say so,
and (b) history is cleared entirely — not demoted to 1 turn but to 0 turns — so no prior answer can
be recycled. The system prompt's "never say you cannot see" instruction is replaced with the
opposite calibration rule.
**Files:** `gemini.py`, `session_context.py`.
**Depends on:** none (but pairs naturally with Task 3 which raises the weak-perception threshold).

```prompt
Read @jarvis/docs/DIAGNOSIS3.md §1.6 (system prompt problem), §2.4 (history bleed on
weak perception), §2.5 (confidence vs calibration), and §4/P14.

Two changes are needed.

CHANGE 1 — Rewrite the system prompt honesty rule in gemini.py.

Find _SYSTEM_PROMPT (approximately lines 42–75 of gemini.py). It currently contains:
  "The provided window title and on-screen text IS the current screen state —
   reason over it directly. Never say you cannot see the screen."

Replace that sentence pair with:
  "The provided window title and on-screen text represents what was captured from the screen.
   Reason over what is provided. If the screen content is insufficient to answer the question
   — for example, if the relevant element, text, or area is not in the capture — say clearly:
   'I can't see that on screen right now.' Do not guess and do not reuse a prior answer."

Keep all other lines in _SYSTEM_PROMPT unchanged. Do not add new paragraphs.

CHANGE 2 — Zero-drop history when perception is weak in session_context.py.

In to_prompt_block() (around line 307), find the demotion logic:
  n = 1 if perception_weak else config.MODEL_HISTORY_TURNS

Change it to:
  n = 0 if perception_weak else config.MODEL_HISTORY_TURNS

When n=0, the history section must be completely omitted from the output (no "History:" header,
no turn lines, no warning prefix). The staleness warning is no longer needed when there is zero
history — the model has nothing to recycle. Adjust the surrounding if-block so that when n=0,
the lines/history assembly is skipped entirely and parts contains only the app/window line.

Do not change config.py in this task (the threshold constants HISTORY_CONTENT_FLOOR_* are raised
in Task 3). Do not change the weak-perception detection logic — only change what happens when it
fires.
```

**Verify:** With a sparse ScreenModel (fewer than 3 content-region elements and fewer than 40 chars
of content text — the current thresholds), `to_prompt_block()` returns a string with no "History:"
section at all. Ask Jarvis about something not on screen — it says it cannot see it, rather than
recycling a prior answer.

---

## Task 3: Raise demotion floor + tighten native TTL (Fixes F2A, F2E / P14 partial)

**Goal:** Make the weak-perception demotion actually trigger by raising the content floor thresholds
to a level where sidebar labels and menu items do not pass, and reduce the native screen-read cache
TTL from 8s to 3s so stale screen content evicts faster.
**Files:** `config.py`.
**Depends on:** 2 (Task 2 must land first so the new thresholds have something to trigger).

```prompt
Read @jarvis/docs/DIAGNOSIS3.md §2.2 (demotion floor analysis) and §2.3 (cache TTL) and §4/P14.

The current weak-perception trigger in session_context.to_prompt_block() fires when:
  len(content_elems) < HISTORY_CONTENT_FLOOR_ELEMENTS (currently 3)
  OR len(content_text) < HISTORY_CONTENT_FLOOR_CHARS (currently 40)

This almost never fires in practice. A mostly-empty window still exposes sidebar labels, menu
items, status bar text, and window-level UIA nodes — easily producing 3+ elements and 40+
characters. The threshold was set at "essentially blank screen" rather than "insufficient content
to answer a question".

Change config.py:
  HISTORY_CONTENT_FLOOR_ELEMENTS = 5    (was 3)
  HISTORY_CONTENT_FLOOR_CHARS = 200     (was 40)

With these values, a window that exposes only chrome/labels (sidebar, toolbar, tab strip) still
falls below the floor, and history is dropped. A window with a meaningful content block (a
document, a code file, a web article) will produce well over 200 characters of content text and
pass the floor.

Also change:
  SCREEN_READ_TTL = 3    (was 8)

The native-app cache TTL of 8 seconds is too generous. A user can switch files, scroll to a
different section, or open a dialog in under 8 seconds; the cache serves the old content silently.
3 seconds is long enough to avoid redundant reads within a single interaction but short enough that
a changed context triggers a fresh read.

Do not touch BROWSER_SCREEN_READ_TTL (already 1.0s from P12) or BROWSER_CACHE_HAMMING_MAX.
No other files change.
```

**Verify:** After changing the constants, a window showing only sidebar navigation (≈ 3 UIA nodes,
≈ 80 chars of label text) triggers `perception_weak = True` and the history block is cleared
(Task 2's n=0 path). A window showing a code file or article does NOT trigger it.

---

## Task 4: Surface cache-hit signal to the model prompt (Fix F2B / P17)

**Goal:** When the screen read comes from cache (not a fresh capture), tell the model so it does not
treat stale content as live. Currently the model has no way to know whether it is reading pixels
from right now or from 3 seconds ago.
**Files:** `router.py`, `gemini.py`.
**Depends on:** none.

```prompt
Read @jarvis/docs/DIAGNOSIS3.md §2.3 and §4/P17.

When screen_read_fresh() returns True (cache hit), the model receives last-read's text and has no
signal that it is not live. It then answers about content that may have scrolled away or changed.

Thread a cache_hit boolean from the freshness check through to the prompt builder:

1. router.py: RouteResult (or PerceptionResult — whichever struct carries the screen text to the
   prompt builders) does not currently carry a cache_hit field. Add one:
     cache_hit: bool = False
   In the route() function, where screen_read_fresh() is called, set
   perception.cache_hit = True when the freshness check passes (cache is being used), False when a
   fresh read ran.

2. gemini.py: in both _build_initial_contents() (Gemini path) and _build_local_prompt() (local LLM
   path), after the perception/screen block is assembled, check route_result.perception.cache_hit.
   When True, prepend a one-line note to the perception block:
     "[Screen content from cache — up to N seconds old. If the screen has changed, I may not
      have the current view.]"
   Where N is the configured TTL for the app class (read from config.SCREEN_READ_TTL or
   config.BROWSER_SCREEN_READ_TTL depending on the target's app_class).

Do not change the cache hit/miss logic itself. Do not change session_context.screen_read_fresh().
This is purely a pass-through of an existing boolean with a new prompt annotation.
```

**Verify:** Trigger a cache hit (read once, immediately query again within the TTL). The model
prompt contains the cache-age notice. A fresh read (first query, or after TTL) does NOT include the
notice. Check that the note does not appear on local LLM prompts when the cache was not used.

---

## Task 5: Sort content elements by confidence before rendering (Fix F1F / P15)

**Goal:** When `to_prompt_block()` renders content-region elements into the model prompt, the
highest-confidence (most reliably extracted) content should come first, so it is least likely to be
truncated by the 1600-char budget cap. Currently elements are rendered in containment-tree order,
which has no correlation with reliability.
**Files:** `screen_model.py`.
**Depends on:** none.

```prompt
Read @jarvis/docs/DIAGNOSIS3.md §1.5 (truncation problem) and §4/P15.

ScreenModel.to_prompt_block() renders content-region elements (in_content_region=True) before
chrome elements. Within the content-region group, elements are rendered in containment-tree order
(the order they appear in the fused ScreenModel.elements list). This order has no relation to
how reliably the text was extracted. An element at the bottom of the tree order might be the most
important piece of screen content, and it gets truncated first when the 1600-char budget is hit.

In to_prompt_block() in screen_model.py, when building the content-region element list for
rendering, sort them by calibrated_confidence descending BEFORE building the text output:

  content_elems = sorted(
      [e for e in self.elements if getattr(e, "in_content_region", True) and e.text],
      key=lambda e: getattr(e, "calibrated_confidence", 0.0),
      reverse=True,
  )

Then render content_elems in that order (highest confidence first). The chrome section (elements
where in_content_region=False) remains unchanged and continues to be capped at ~15% of the budget.

Do not change the chrome section ordering. Do not change the budget cap logic. Do not change
anything in fusion.py or perception.py — this is a rendering-order change only inside
screen_model.py's to_prompt_block().

If screen_model.py has multiple to_prompt_block()-like methods (e.g. one for Gemini, one for local
— check carefully), apply the sort to all of them.
```

**Verify:** On a screen with mixed high-confidence UIA text and lower-confidence OCR content text,
the rendered prompt block shows the high-confidence lines first. A low-confidence but in-content
element that would have been cut now appears above a high-confidence chrome element. Confirm the
existing tests (if any) for to_prompt_block() still pass with the new order.

---

## Task 6: Upgrade local LLM + add streaming + fallback-mode notice (Fixes F3A, F3C, F3D / P16, P18)

**Goal:** Replace the 3B parameter local model with a capable general-purpose 12B model, add a
fallback-mode notice to the local prompt so the model knows it is operating without Gemini, and
implement streaming for the local LLM path so the UI does not freeze for 10+ seconds.
**Files:** `config.py`, `gemini.py`.
**Depends on:** none (model pull is an external prerequisite — see below).

**Model pull (do this outside VS Code before running the task):**
```
ollama pull mistral-nemo:12b
```
Alternative if VRAM is limited (requires ~5GB vs ~8GB):
```
ollama pull gemma3:9b
```
Verify the pull with `ollama list` — confirm the model appears. Then proceed with the code changes.

```prompt
Read @jarvis/docs/DIAGNOSIS3.md §3.2 (model too small), §3.3 (no streaming), §3.4 (fallback
notice), and §4/P16 and §4/P18.

Three changes in this task.

CHANGE 1 — Update the local model config in config.py.

  LOCAL_LLM_MODEL = "mistral-nemo:12b"    (was "llama3.2:3b")
  LOCAL_ANSWER_TIMEOUT_MS = 25_000         (was 10_000)

Update the comment next to LOCAL_LLM_MODEL to reflect the new model. If you add gemma3:9b as the
alternative, add it as a commented-out alternative line.

CHANGE 2 — Add a fallback-mode notice in _build_local_prompt() in gemini.py.

Find the function that builds the prompt string for the local LLM (typically _build_local_prompt
or equivalent). At the top of the prompt, BEFORE the history or screen block, prepend:

  "You are running as a local fallback model (Gemini is unavailable). "
  "Answer from what is provided in the screen context. "
  "If you are not sure, say so clearly — do not invent details.\n\n"

This combats confabulation from a model with less world knowledge operating on incomplete context.

CHANGE 3 — Add streaming for the local LLM path in gemini.py.

Currently the local LLM call is a blocking request (waits for the full response, then yields it as
a single chunk). Find this call — it uses the Ollama HTTP API (http://localhost:11434/api/generate
or similar). The Ollama API supports streaming via "stream": true in the POST body and returns
newline-delimited JSON where each line has a "response" field and a "done" field.

Replace the blocking call with a streaming generator:
1. Send the POST with "stream": true and stream=True in the requests.post call (response.iter_lines()).
2. Parse each line as JSON. Yield response["response"] for each line where done is False.
3. When done is True, stop iteration.
4. Wrap the generator so it fits the existing ask_stream() yield interface — the caller expects
   string chunks, not JSON. The meta dict should still be updated with answer_source="local_fallback"
   (or "local_answer" if PREFER_LOCAL_STRUCTURE is True — preserve existing logic).
5. Keep the LOCAL_ANSWER_TIMEOUT_MS timeout: apply it as a requests timeout (connect + read per
   chunk). If the request times out or the connection is refused, yield an empty iterator and let
   the caller handle the empty-stream case as before.

Do not change the Gemini streaming path. Do not change the answer_source logic. The UI already
handles streamed chunks from the Gemini path — the local path using the same yield interface will
work with no UI changes.
```

**Verify:** With `ollama list` confirming `mistral-nemo:12b` is installed: ask a general-knowledge
question with Gemini disabled (set GEMINI_API_KEY="" in .env temporarily). The answer streams
token by token instead of appearing all at once after a 10s pause. The prompt visible in debug logs
contains the fallback-mode notice. The answer is substantively better than llama3.2:3b on a
non-trivial question.

---

## Dependency summary

```
All tasks are independent. Recommended order:

  1          OCR + UIA tuning (config only, immediate uplift — land first)
  2          Honesty guard + zero-drop history (3 file edits — land second)
  3          Demotion floor + TTL (depends on 2 being live)
  4          Cache-hit signal to model (independent, any time)
  5          Confidence sort in prompt render (independent, any time)
  6          Local LLM upgrade + streaming (external pull required first)
```

Critical path for the "Jarvis can't see what I'm talking about" and "says bullshit from prior
context" symptoms:
**Task 1** (OCR/UIA recall) → **Task 2** (honesty + history clear) → **Task 3** (floor threshold)
covers both field reports with 3 tasks, mostly config changes and ~30 lines of code.

**Task 6 model pull is a prerequisite the agent cannot do.** Run `ollama pull mistral-nemo:12b`
before handing Task 6 to a coding agent.
