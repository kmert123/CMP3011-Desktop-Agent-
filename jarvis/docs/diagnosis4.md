# Jarvis Diagnosis 4 — Why P13–P18 Didn't Fix It, and What Will

Audit date: 2026-05-31
Scope: Three new field reports after DIAGNOSIS3 (P13–P18) was *applied* to the
code. The config in `config.py` confirms the P13–P18 fixes are live:
`OCR_PSM=11`, `OCR_SCALE=3.5`, `OCR_MIN_CONF=0.25`, `UIA_MAX_DEPTH=8`,
`UIA_MAX_NODES=400`, `SCREEN_READ_TTL=3`, `LOCAL_LLM_MODEL=mistral-nemo:12b`,
the honesty guard in `_SYSTEM_PROMPT`, the cache notice, and local streaming.

**The fixes landed and the user still reports the same two symptoms.** That tells
us the previous diagnosis aimed at the wrong layer. This document finds the
*code-level* reasons the symptoms survive, not just the tuning knobs.

---

## 0. The three field reports

1. **Jarvis can't see certain apps at all** — on some apps it "couldn't see
   anything." Screen-reading must work on *any* screen. (Most important.)
2. **Jarvis refuses to use its own knowledge** — it literally says *"my
   responses are limited to the information I can see on the screen"* and
   *"I don't have external information about that."* We have an LLM precisely so
   it can add knowledge on top of the screen. (Second most important.)
3. **No logging / observability** — we cannot see the user's request, the steps
   Jarvis took, what was invoked, or why an answer came out the way it did. We
   need a trace to debug 1 and 2 in the first place.

---

## 1. Feedback 1 — "Can't see anything on certain apps"

P13 tuned OCR/UIA *config*. But the config only reaches **one of the two OCR
code paths**, and the path that actually runs for most non-fusion reads is a
crippled stub. There is also a structural reason whole classes of apps return
nothing. Both are below.

### 1.1 Root cause A — there are TWO `read_ocr` functions and the live one ignores all OCR config (CRITICAL, this is the bug)

There are two completely separate OCR implementations:

- **The good one:** [`adapters/ocr_adapter.py:read_ocr()`](../adapters/ocr_adapter.py#L54)
  — does upscaling, dark-mode inversion, CLAHE, explicit `--psm`, per-token and
  per-line confidence floors, returns structured `ScreenElement`s. *This is the
  one P13's config was written for.*
- **The crippled one:** [`perception.py:read_ocr()`](../perception.py#L153)
  — this is what the **plain (non-fusion) ladder** calls. Look at its body:

  ```python
  cropped, _origin, _dpi, _stale = capture_target(target) ...
  pil_img = Image.fromarray(cv2.cvtColor(cropped, cv2.COLOR_BGR2RGB))
  text = pytesseract.image_to_string(pil_img).strip()
  ```

  No upscaling. No dark-mode inversion. No CLAHE. **No `--psm` (so it ignores
  `OCR_PSM=11` and silently uses Tesseract's default PSM=3).** No confidence
  handling. On a dark-mode app (VS Code, Discord, a terminal) `image_to_string`
  on the raw BGR→RGB crop returns near-garbage or an empty string — and the
  ladder treats empty string as `ok=False` and moves on, or returns a blob the
  model can't use.

**Why this is the "can't see anything" bug.** `run_ladder` only takes the good
fusion path when `use_fusion=True` **and** `target is not None` **and**
`not is_self` ([`perception.py:262`](../perception.py#L262)). `use_fusion` is set
by the router only when `perception_mode == PIXELS` or `entry_rung >= OCR`
([`router.py:305`](../router.py#L305)). For a normal STRUCTURE query on a native
or unknown app, `entry_rung` is `UIA` — **below OCR** — so `use_fusion=False` and
the ladder runs the **plain path**. If UIA returns sparse/empty text (Electron,
custom-drawn, or any app whose UIA tree is thin), the ladder falls to
`perception.py:read_ocr` — the stub — and that stub, with no preprocessing on a
dark UI, returns nothing. Jarvis "sees nothing."

So the apps where Jarvis "couldn't see anything" are exactly: **dark-mode and/or
thin-UIA apps reached through the non-fusion ladder.** P13's OCR tuning never
touched that path.

### 1.2 Root cause B — `entry_rung` for STRUCTURE never engages fusion, so the multi-adapter pipeline is bypassed for the common case

`entry_rung_for(STRUCTURE, native/unknown)` returns `Rung.UIA`
([`router.py:47`](../router.py#L47)). The fusion pipeline — the only path that
runs OCR+CV+UIA together, does content-region re-OCR, and builds a real
`ScreenModel` — only runs when entry is `OCR` or deeper. Result: the richest
perception machinery in the codebase is **dormant** for the most common query
shape ("what does this say", "summarise this"), and the model instead gets either
a UIA skeleton or the stub-OCR blob.

This is the single highest-leverage structural finding: **the good perception
path exists but is gated off for ordinary screen-reading.**

### 1.3 Root cause C — apps that genuinely don't expose accessibility or rasterised text

For Chromium/Electron without the accessibility flag, GPU-rendered surfaces
(games, some video/Canvas apps), and protected/DRM windows, *neither* UIA nor a
screenshot yields stable text. This is the real, hard residue noted in
DIAGNOSIS3 §5 and DIAGNOSIS2 M1 (CDP). It is *not* the cause of the current
report for most apps — A and B are — but it is the ceiling once A and B are fixed.

### 1.4 Root cause D — `ok` and staleness can suppress a usable read

`perception.py:read_ocr` sets `ok = bool(text) and not _stale`. The fusion path
sets `ok = (bool(full_text) or VISION) and not stale`. A transient `stale=True`
from `capture_target` (window re-resolve race, brief minimise, DPI flip) makes a
perfectly good crop report `ok=False`, and the ladder discards it. There is no
"degraded but usable" state — it's binary, and the failure is silent.

### 1.5 Fixes for Feedback 1

| ID | Description | File(s) | Effort |
|----|-------------|---------|--------|
| **G1A** | **Delete the stub `read_ocr` in `perception.py` and make it delegate to `adapters.ocr_adapter.read_ocr`** (capture the crop+origin, call the real adapter, join element text). This single change makes the plain ladder honour every OCR config P13 set. This is the most important fix in this document. | `perception.py` | S |
| **G1B** | **Make STRUCTURE queries use the fusion pipeline.** Either (a) set `use_fusion=True` whenever `entry_rung is not None and target is not None and not is_self` in `router.route` (cheapest), or (b) change `entry_rung_for(STRUCTURE, …)` to return a rung that triggers fusion. Option (a) is lower-risk: it keeps UIA-first semantics but runs OCR+CV alongside and produces a real ScreenModel for every read. | `router.py` | S |
| **G1C** | **Add a perception-quality signal instead of binary `ok`.** Return `char_count` / element_count on `PerceptionResult` and, when a read is "thin" (below a floor), automatically escalate one rung in the ladder *before* returning, rather than handing the model an empty blob. Today escalation only happens later via `should_escalate` on confidence — a thin OCR string has no confidence to trip it. | `perception.py`, `router.py` | M |
| **G1D** | **Don't discard a good crop on transient `stale`.** Treat `stale` as a warning attached to the result, not an `ok=False` veto, when text was actually extracted. | `perception.py`, `capture.py` | XS |
| **G1E** | **Vision fallback that actually fires.** When UIA+OCR both come back thin on an app, route to the VLM with the screenshot automatically (not only when the model later calls `need_image`). For dark/custom-drawn apps the pixels are the only ground truth. Gate on app_class + thin-text, cap to one call. | `perception.py`, `router.py` | M |

> **Do G1A and G1B first.** They are small, localized, and together they convert
> "sees nothing" into "runs the real pipeline every time." Everything else is
> refinement on top.

---

## 2. Feedback 2 — "Only talks about the screen, won't add knowledge"

This is the most important *behavioural* regression and it is, ironically, a
**side effect of the DIAGNOSIS3 honesty fix (F2C)**. We told the model to stop
hallucinating about the screen; it over-generalised into "I can only talk about
the screen."

### 2.1 Root cause A — the honesty guard reads as a topic restriction (CRITICAL)

Current [`_SYSTEM_PROMPT`](../gemini.py#L42):

> "The provided window title and on-screen text represents what was captured from
> the screen. Reason over what is provided. **If the screen content is
> insufficient to answer the question … say clearly: 'I can't see that on screen
> right now.' Do not guess and do not reuse a prior answer.**"

There is **no sentence anywhere telling the model it may use its own world
knowledge.** The entire framing is "you are given screen text; answer from it;
if it's not there, say you can't see it." A reasonable model concludes that
its job is *screen transcription/Q&A only* — exactly the behaviour the user
hit: *"my responses are limited to the information I can see on the screen."*

The honesty guard was meant to fire on *"what does the red banner say"* when the
banner wasn't captured. Instead it fires on *"what is this library / explain this
error / what's a good fix"* — questions where the screen is **context**, not the
**answer**, and the model should answer from knowledge.

### 2.2 Root cause B — there is no notion of "screen as context vs. screen as the question"

Every query is treated identically: dump screen text, ask the question. But two
very different query shapes exist:

- **Screen-grounded** ("what does this say", "summarise this", "what's selected")
  → screen text *is* the answer source. Honesty guard appropriate.
- **Knowledge-with-context** ("what does this error mean", "is this code right",
  "who is this person", "explain this") → screen is the *subject*, the answer
  comes from the model's knowledge applied to it.

The classifier already has the axes to tell these apart (`Act`, `Perception`,
and especially `Intent.NO_CONTEXT`), but the prompt collapses them. The model
is never told "use the screen as context and bring your own knowledge."

### 2.3 Root cause C — `PREFER_LOCAL_STRUCTURE` silently routes good queries to the weak model

[`config.PREFER_LOCAL_STRUCTURE=True`](../config.py#L160) plus the fast-path in
[`gemini.ask_stream`](../gemini.py#L669) means: any TEXT query where screen
confidence ≥ `ESCALATE_CONF` is answered by **mistral-nemo:12b locally, Gemini
never called**, as long as the local answer is ≥15 chars. mistral-nemo is fine at
"read this back to me" but markedly weaker at "explain / reason about this with
outside knowledge." So precisely the knowledge-seeking queries from Feedback 2,
when they happen to have decent screen text, get silently downgraded to the model
least able to add knowledge — and the local prompt's fallback preamble ("Answer
from what is provided … do not invent details") *doubles down* on the
screen-only framing. The user sees a thin, screen-bound answer and concludes
Jarvis "won't give more context."

### 2.4 Root cause D — the local prompt preamble is even more restrictive than the system prompt

[`_build_local_prompt`](../gemini.py#L435) preamble:

> "Answer from what is provided in the screen context. If you are not sure, say
> so clearly — do not invent details."

Combined with a 2–3 sentence limit and "speak directly, no filler," this is a
recipe for "I can only talk about what's on screen." There is no permission to
use general knowledge at all.

### 2.5 Fixes for Feedback 2

| ID | Description | File(s) | Effort |
|----|-------------|---------|--------|
| **K2A** | **Rewrite the system prompt to grant knowledge + scope the honesty guard.** Add explicitly: *"You are a knowledgeable assistant, not just a screen reader. Use the on-screen content as context and combine it with your own general knowledge to give a complete, useful answer. The honesty rule applies ONLY to claims about what is literally on the screen: if asked to read or locate something specific that wasn't captured, say you can't see it — but always still answer the underlying question from your own knowledge."* This is the single most important Feedback-2 fix. | `gemini.py` | XS |
| **K2B** | **Mirror the same grant in the local prompt preamble.** Replace "answer from what is provided … do not invent details" with the knowledge-plus-context framing, keeping only "don't fabricate *what is on the screen*." | `gemini.py` | XS |
| **K2C** | **Make `PREFER_LOCAL_STRUCTURE` knowledge-aware.** Only take the local fast-path for clearly screen-grounded intents (read/summarise/locate), never for knowledge-seeking ones. Cheapest implementation: skip the fast-path when the query contains reasoning/knowledge cues (why/how/explain/what does … mean/is this correct/should I), or — better — gate on a classifier signal. Falls through to Gemini, which has the knowledge. | `gemini.py`, `classify.py` | S |
| **K2D** | **Distinguish "screen as context" vs "screen as question" in the prompt label.** When the intent is knowledge-seeking, label the screen block "Context (the user's current screen) — use this together with your own knowledge" instead of "Screen content." A label change measurably shifts model behaviour. | `gemini.py` | XS |
| **K2E** | **Raise the answer length ceiling for knowledge queries.** The blanket "3–4 sentences" cap starves explanatory answers. Allow longer answers when the query is explanatory. | `gemini.py` | XS |

> **K2A alone will fix most of Feedback 2.** It is a one-paragraph prompt change.
> K2C stops the silent downgrade that makes the failure intermittent and
> confusing.

---

## 3. Feedback 3 — Logging / observability

Right now there is effectively **no usable logging.** Modules across the
codebase (`gemini.py`, `focus_resolver.py`, `fusion.py`, `focus.py`,
`actions.py`, `uia_watcher.py`, `vision_adapter.py`, `session_actor.py`,
`set_of_marks.py`) all do `_log = logging.getLogger(__name__)` and call
`_log.debug(...)` — but **nothing ever calls `logging.basicConfig()` or attaches
a handler.** With no root handler and default level WARNING, every one of those
`debug`/`info` lines is silently dropped. The telemetry JSONL
([`telemetry.py`](../telemetry.py)) records one summary line per query but not the
*steps*: it has no record of which rung ran, what UIA/OCR returned, which tools
the model called, what the focus resolver decided, or what prompt was sent.

So today, when the user asks "why did Jarvis answer that way," **we have no trace
to look at.** This is why Feedback 1 and 2 took a code audit to diagnose instead
of a log read.

### 3.1 What a useful trace needs to capture

For each turn, one structured, correlated record chain keyed by a `turn_id`:

```
turn_id, wake_ts
  ├─ INPUT       transcript / typed text, target {process, title, app_class, hwnd}
  ├─ CLASSIFY    act, perception_mode, needs_focus, high_conf, router_source, confidence
  ├─ ROUTE       entry_rung, used_cache, cross_window_hit, use_fusion
  ├─ PERCEPTION  rung_reached, source, element_count, char_count,
  │              content_region_bbox, ok, stale, per-adapter counts (uia/ocr/cv)
  ├─ FOCUS       source rung, resolved_text (truncated), confidence, ambiguous
  ├─ PROMPT      answer_source (gemini/local), system-prompt id, screen_block_chars,
  │              history_turns_included, image_attached
  ├─ TOOLS       each model tool call: name, args, result_summary, budget_remaining
  ├─ ANSWER      final text, latency_ms, escalated, escalated_rung
  └─ OUTCOME     error?, answer_source
```

This is the chain that lets you answer "Jarvis said X — why?" in one read:
you see whether perception was thin, whether the cache served stale text,
whether it went local instead of Gemini, and which tools fired.

### 3.2 How to achieve it (recommended design)

There are three layers; do them in order of leverage.

**Layer 1 — turn the existing logging on (XS, do immediately).**
Add a `logging_setup.py` called once at `main.py` startup:

```python
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

def setup_logging(level=logging.INFO):
    log_dir = Path.home() / ".jarvis"
    log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)-22s %(message)s"
    )
    fh = RotatingFileHandler(
        log_dir / "jarvis.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8"
    )
    fh.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(level)
    root.addHandler(fh)
    # optional: console handler at WARNING so the terminal isn't noisy
    ch = logging.StreamHandler()
    ch.setLevel(logging.WARNING)
    ch.setFormatter(fmt)
    root.addHandler(ch)
```

Call `setup_logging()` as the first line of `JarvisApp.__init__` (or `main`).
Make the level configurable via `config.LOG_LEVEL` (env `JARVIS_LOG_LEVEL`).
This alone surfaces every `_log.debug` already written across the codebase —
instant, free observability for the tool calls, focus decisions, and escalations
that are *already logged but invisible*.

**Layer 2 — structured per-turn trace (S–M, the real ask).**
Add a `trace.py` with a `TurnTrace` object created in `_answer_worker` and
threaded through `route` / `run_ladder` / `resolve_focus` / `ask_stream`
(pass it as an optional kwarg; default `None` so nothing breaks). Each stage
calls `trace.add("PERCEPTION", rung=..., chars=..., ...)`. At turn end, write
the whole chain as **one JSON object** to `~/.jarvis/traces/<turn_id>.json` (or
one line in `traces.jsonl`). Generate `turn_id = uuid4()` at wake time and put it
on every event and every telemetry record so the existing telemetry and the new
trace correlate. This is the artefact the user asked for: *request → steps →
what was invoked*, in one place, per turn.

Minimal surface change: a single optional `trace` parameter added to
`router.route`, `perception.run_ladder`, `focus_resolver.resolve_focus`, and
`gemini.ask_stream`. None of them need to *require* it.

**Layer 3 — make it inspectable (S, quality-of-life).**
- Extend `telemetry.py` `_FIELDS` with `turn_id`, `element_count`, `char_count`,
  `tool_calls` (list), `screen_block_chars`, `answer_text` so the existing JSONL
  is richer even without reading the per-turn trace files.
- A tiny `tools/trace_view.py` that pretty-prints the last N traces (or tails
  `jarvis.log`) so debugging is `python tools/trace_view.py` rather than grepping.
- Optional: when `config.DEBUG_OVERLAY` is on, the trace already pairs naturally
  with the saved `overlay_*.png` — record the overlay path in the trace so you
  can see the bbox map for the exact turn you're debugging.

### 3.3 Why this ordering

Layer 1 is 30 minutes and immediately makes the *existing* debug lines visible —
you could likely confirm Feedback 1 and 2 from logs the same day. Layer 2 is the
durable answer to "see the request and the steps Jarvis takes." Layer 3 makes it
pleasant. Don't build Layer 2's framework before Layer 1 proves what's already
being logged.

---

## 4. Prioritised remediation plan

### Priority 0 — Observability first (so 1 and 2 are verifiable)
- **L1**: `logging_setup.py` + call at startup + `config.LOG_LEVEL`. (XS)
  Everything below becomes measurable once this is in.

### Priority 1 — Perception: run the real pipeline every time (CRITICAL)
- **G1A**: delete the stub `perception.py:read_ocr`, delegate to the adapter. (S)
- **G1B**: enable fusion for STRUCTURE reads. (S)
- **G1C/G1D**: thin-read auto-escalation + stop discarding good crops on transient stale. (M/XS)

### Priority 2 — Behaviour: let the model use its knowledge (CRITICAL)
- **K2A**: rewrite system prompt — knowledge grant + scoped honesty guard. (XS)
- **K2B**: same grant in the local prompt. (XS)
- **K2C**: make `PREFER_LOCAL_STRUCTURE` skip knowledge-seeking queries. (S)

### Priority 3 — Perception ceiling (HIGH)
- **G1E**: automatic VLM fallback on thin UIA+OCR. (M)

### Priority 4 — Structured trace (HIGH, the explicit ask in Feedback 3)
- **L2**: `trace.py` + `turn_id` threaded through route/ladder/focus/answer. (S–M)
- **L3**: richer telemetry fields + `tools/trace_view.py`. (S)

---

## 5. What this will and won't fix

**Will fix**
- "Sees nothing on certain (dark-mode / thin-UIA) apps" — G1A is the direct cause;
  G1B makes the rich path run for ordinary reads.
- "Only talks about the screen, won't add knowledge" — K2A/K2B remove the
  screen-only framing; K2C stops the silent downgrade to the weak local model.
- "Can't debug what happened" — L1 surfaces existing logs immediately; L2 gives
  the per-turn request→steps→invocations trace the user asked for.

**Won't fix**
- Apps that expose neither accessibility text nor rasterised text (Electron
  without the a11y flag, GPU/Canvas/DRM surfaces). G1E mitigates via pixels but
  the CDP path (DIAGNOSIS2 M1) is still the real fix for browsers/Electron.
- Local-model quality ceiling — mistral-nemo:12b is better than 3B but is not
  Gemini; a live Gemini key remains the highest-leverage quality lever.

---

## 6. Quick reference

| Symptom | Deepest root cause | Fix |
|---------|--------------------|-----|
| "Can't see anything" on some apps | `perception.py:read_ocr` is a no-preprocessing stub; fusion path gated off for STRUCTURE | **G1A + G1B** |
| Thin read → empty answer | binary `ok`/stale veto, no auto-escalate on thin text | G1C/G1D |
| "I can only talk about the screen" | system + local prompt frame the job as screen-only; no knowledge grant | **K2A + K2B** |
| Knowledge query gets thin answer intermittently | `PREFER_LOCAL_STRUCTURE` silently routes to weak local model | K2C |
| Can't debug any of the above | no `logging.basicConfig`; debug lines dropped; no per-turn trace | **L1 then L2** |
