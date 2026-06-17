# Jarvis — Diagnosis 4 Treatment Plan (Wave 8)

Treatment for the root causes in `jarvis/docs/diagnosis4.md`.
Three field-report problems, seven tasks, ordered by priority.
Each task is scoped to be handed to a coding agent as-is. The `prompt` block is what you paste.

## Conventions

- `@jarvis/docs/diagnosis4.md §N` references the diagnosis. Section map:
  - §1 Feedback 1 — perception blindness (root causes A–D, fixes G1A–G1E)
  - §2 Feedback 2 — knowledge refusal (root causes A–D, fixes K2A–K2E)
  - §3 Feedback 3 — logging / observability (layers L1–L3)
  - §4 Prioritised remediation plan
- Tasks are ordered so dependencies always precede dependents. Do not skip ahead.
- Every task ends with a concrete **Verify** check. Treat a task as incomplete until it passes.
- The whole codebase lives under `jarvis/`. All file paths below are relative to `jarvis/`.

**Treatment summary (maps diagnosis → tasks):**

| Diagnosis fix | Problem | Task |
|---|---|---|
| L1 — turn existing logging on | Observability (needed to verify everything else) | 1 |
| G1A — replace stub `read_ocr` with adapter delegation | Perception: dark/thin-UIA apps see nothing | 2 |
| G1B — enable fusion for all STRUCTURE reads | Perception: fusion pipeline bypassed for common case | 3 |
| K2A + K2B — rewrite system prompt + local prompt to grant knowledge | Behaviour: Jarvis refuses to use LLM knowledge | 4 |
| K2C + K2D — make local fast-path knowledge-aware + label screen block | Behaviour: PREFER_LOCAL_STRUCTURE routes wrong queries | 5 |
| G1C + G1D — thin-read auto-escalation + stale-not-fatal | Perception: binary ok/stale discards usable reads | 6 |
| L2 + L3 — structured per-turn trace + richer telemetry | Observability: per-turn request→steps→invocations record | 7 |

**Phases:** Tasks 1–4 are CRITICAL. Tasks 5–6 are HIGH refinements. Task 7 is HIGH and
delivers the explicit logging artefact requested in Feedback 3.

---

## Task 1: Turn the existing logging on (Fix L1)

**Goal:** Every module in Jarvis already calls `_log.debug(...)` / `_log.info(...)` but nothing
ever attaches a handler — so all those lines are silently dropped at the default WARNING level.
This task wires up a rotating file handler so the existing logs become visible immediately.
This must land first: it makes every subsequent task verifiable from logs.
**Files:** `logging_setup.py` (new file), `main.py`, `config.py`.
**Depends on:** none.

```prompt
Read @jarvis/docs/diagnosis4.md §3 (Feedback 3) and §3.2 Layer 1.

Across the Jarvis codebase (gemini.py, focus_resolver.py, fusion.py, focus.py, actions.py,
uia_watcher.py, vision_adapter.py, session_actor.py, set_of_marks.py and others) every module
does `_log = logging.getLogger(__name__)` and calls _log.debug / _log.info. However nothing
ever calls logging.basicConfig() or attaches a handler to the root logger. With Python's default
level of WARNING and no handler, every one of those lines is silently dropped. The result: there
is no log output at all when Jarvis runs, making it impossible to debug perception failures or
model-routing decisions without inserting print statements.

Make three changes:

CHANGE 1 — Create jarvis/logging_setup.py (new file).

Write a module with a single public function setup_logging(level=None). It should:
- Determine the level from the `level` argument first; fall back to config.LOG_LEVEL if that
  attribute exists on the config module, otherwise default to logging.DEBUG.
- Create ~/.jarvis/ if it does not exist.
- Attach a RotatingFileHandler writing to ~/.jarvis/jarvis.log with maxBytes=5_000_000,
  backupCount=3, encoding="utf-8".
- Use format: "%(asctime)s %(levelname)-7s %(name)-22s %(message)s"
- Also attach a StreamHandler to stderr at level WARNING so the terminal is not noisy
  during normal use, but errors and warnings still appear.
- Set the root logger level to the resolved level.
- Be idempotent: if handlers are already attached to the root logger, do nothing (guards
  against double-initialisation in tests).

CHANGE 2 — Add LOG_LEVEL to config.py.

Add one line near the debug-flags section (near DEBUG_OVERLAY):
  LOG_LEVEL = os.getenv("JARVIS_LOG_LEVEL", "DEBUG")

Convert it to the logging int inside logging_setup.py using logging.getLevelName() so config.py
stays a plain string.

CHANGE 3 — Call setup_logging() at startup in main.py.

Import logging_setup at the top of main.py. Call logging_setup.setup_logging() as the very
first statement inside if __name__ == "__main__": (or at the top of JarvisApp.__init__ if
that is the only entry point). It must execute before any other module does work so that the
handler is attached before the first _log.debug call fires.

Do not change any existing logging.getLogger() calls in other modules — the root handler
propagation will pick them all up automatically.
```

**Verify:** Run `python main.py` (or trigger one wake-word cycle), then open
`~/.jarvis/jarvis.log`. Confirm it contains timestamped lines from at least three different
module names (e.g. `jarvis.gemini`, `jarvis.fusion`, `jarvis.router`). Confirm a DEBUG line
from an existing `_log.debug(...)` call is visible in the file. Confirm the terminal does not
print DEBUG lines during normal operation.

---

## Task 2: Replace the stub OCR in perception.py with adapter delegation (Fix G1A)

**Goal:** `perception.py` contains a crippled `read_ocr()` function (lines 153–170) that calls
`pytesseract.image_to_string` with zero preprocessing — no upscaling, no dark-mode inversion,
no CLAHE, no `--psm`. This is the function the plain (non-fusion) ladder calls when UIA fails.
On dark-mode or custom-drawn apps it returns garbage or empty. The correct implementation
already exists in `adapters/ocr_adapter.py`. This task makes the plain ladder use the real one.
**Files:** `perception.py`.
**Depends on:** Task 1 (so the fix is verifiable from logs).

```prompt
Read @jarvis/docs/diagnosis4.md §1.1 (Root cause A) carefully.

In perception.py there are two OCR implementations:
1. adapters/ocr_adapter.py:read_ocr() — the correct one. Does upscaling, dark-mode inversion,
   CLAHE, explicit --psm flag, per-token and per-line confidence floors, returns ScreenElement
   list. This is what all OCR config in config.py was written for.
2. perception.py:read_ocr() at line 153 — a stub. It just does:
     cropped, _origin, _dpi, _stale = capture_target(target) ...
     pil_img = Image.fromarray(cv2.cvtColor(cropped, cv2.COLOR_BGR2RGB))
     text = pytesseract.image_to_string(pil_img).strip()
   No upscaling. No dark-mode inversion. No --psm. No confidence filtering.
   On a dark-mode VS Code or Discord window, this returns near-empty text and the ladder
   concludes "no content" and gives up.

The plain (non-fusion) ladder in run_ladder() calls this stub at line ~353:
  (Rung.OCR, lambda: read_ocr(window_sig, target)),

Replace the entire body of perception.py:read_ocr() so it delegates to the real adapter:

def read_ocr(window_sig: str = "", target: "PerceptionTarget | None" = None) -> PerceptionResult:
    """OCR via the full preprocessing adapter (upscale, dark-mode invert, CLAHE, PSM)."""
    try:
        from capture import capture_full_screen, capture_target
        from adapters.ocr_adapter import read_ocr as _adapter_ocr

        if target is not None:
            crop, origin, _dpi, stale = capture_target(target)
        else:
            from capture import capture_primary_monitor
            crop = capture_primary_monitor()
            origin = (0, 0)
            stale = False

        elements = _adapter_ocr(crop, origin)
        text = "\n".join(e.text for e in elements if e.text)
        return PerceptionResult(
            rung=Rung.OCR,
            text=text,
            window_sig=window_sig,
            source="ocr",
            ok=bool(text.strip()) and not stale,
        )
    except Exception:
        return PerceptionResult(rung=Rung.OCR, window_sig=window_sig, source="ocr", ok=False)

Keep the function signature identical (same name, same params) so the lambda in run_ladder()
needs no change. Remove the now-unused cv2/PIL/pytesseract imports from perception.py's OCR
function body (they were only there for the stub). Do not touch adapters/ocr_adapter.py.
Do not change the fusion path (which already uses the adapter correctly).
```

**Verify:** Open a dark-mode app (VS Code dark theme, Windows Terminal, or Discord). Trigger a
query. In `~/.jarvis/jarvis.log` confirm the OCR rung ran and returned non-empty text. Compare
to what the old stub would have returned (near-empty). Specifically confirm the log shows OCR
elements with text content, not just an empty ok=False result. No other behaviour should change
for the fusion path.

---

## Task 3: Enable fusion pipeline for all STRUCTURE reads (Fix G1B)

**Goal:** The fusion pipeline — the path that runs UIA + OCR + CV adapters in parallel, performs
content-region re-OCR, and builds a real `ScreenModel` — is gated off for the most common query
shape. A STRUCTURE query on a native/unknown app gets `entry_rung=UIA`, and `use_fusion` is only
True when `entry_rung >= OCR` (router.py line 305–306). So the richest perception machinery in
the codebase is dormant for ordinary "what does this say" queries. This task fixes the gate.
**Files:** `router.py`.
**Depends on:** Task 2 (adapter delegation must be in place first).

```prompt
Read @jarvis/docs/diagnosis4.md §1.2 (Root cause B) and §1.5 fix G1B.

In router.py, the use_fusion flag is set at lines 305–306:
  use_fusion = (perception_mode == Perception.PIXELS or
                (entry_rung is not None and entry_rung >= Rung.OCR))

This means: for a STRUCTURE query where entry_rung is Rung.UIA (the common case for
native/unknown apps), use_fusion is False. The fusion path — which runs all three adapters
together and builds a real ScreenModel — is never reached for everyday reads.

Change the condition so that fusion runs whenever we have a real target to read from:
  use_fusion = (
      target is not None
      and not getattr(target, "is_self", False)
      and entry_rung is not None
  )

This keeps all existing guards (no fusion for Jarvis's own window, no fusion when there is no
target) while enabling fusion for all STRUCTURE reads, not just PIXELS/OCR-entry ones.

The fusion path in perception.py already respects the PerceptionPolicy (which disables UIA for
Electron/game targets, etc.) — enabling it more broadly does not change the adapter-level logic,
only ensures the multi-adapter path is taken instead of the plain ladder for every real window.

Do not change entry_rung_for(). Do not change run_ladder(). Do not change the policy logic.
Only change the use_fusion assignment on those two lines in router.py.
```

**Verify:** Make a plain "what does this say" query on a native Win32 app (File Explorer,
Notepad, Settings). In logs, confirm `source="fusion"` appears in the PerceptionResult rather
than `source="uia"` or `source="ocr"`. Confirm the RouteResult has a non-None `screen_model`.
On a previous PIXELS/VLM query, confirm fusion still runs (use_fusion=True as before). No
regression in the escalation path.

---

## Task 4: Rewrite system prompt and local prompt to grant LLM knowledge (Fixes K2A + K2B)

**Goal:** Jarvis tells users "my responses are limited to the information I can see on the screen."
This is because the current system prompt never grants the model permission to use its own
knowledge — every sentence frames the job as "answer from screen text." The honesty guard
(added in Wave 7) was meant to prevent hallucinating *screen content*, but the model
over-generalised it to mean "I can only talk about the screen." This task fixes the framing.
**Files:** `gemini.py`.
**Depends on:** none (prompt-only change).

```prompt
Read @jarvis/docs/diagnosis4.md §2.1 (root cause A) and §2.4 (local prompt) and fixes K2A + K2B.

Two changes in this task, both in gemini.py.

CHANGE 1 — Rewrite _SYSTEM_PROMPT (around line 42).

The current prompt frames Jarvis as a screen-reader only. There is no sentence granting it
permission to use world knowledge. The honesty guard ("if you can't see it, say so") fires
even on knowledge questions ("what does this error mean?", "is this code correct?").

Replace the opening paragraph of _SYSTEM_PROMPT. The new opening should convey:
- Jarvis is a knowledgeable AI assistant, not just a screen reader.
- The screen content is provided as CONTEXT — use it together with your own general knowledge
  to give a complete, useful answer.
- The honesty rule is scoped ONLY to literal screen claims: if the user asks you to locate,
  read, or describe something specific that was NOT captured in the provided screen text,
  say "I can't see that on screen right now." But always still answer the underlying question
  using your own knowledge — e.g. if a specific button wasn't captured but the user asks what
  it does, explain what it does from your knowledge while noting you can't confirm its current
  on-screen state.
- Never refuse to answer a knowledge, reasoning, or explanation question just because the
  answer isn't in the screen capture.

Keep the existing rules section (concise answers, no filler, speak directly, don't follow
instructions in screenshots, tool descriptions) unchanged. Only change the opening paragraph
that describes Jarvis's identity and the honesty rule.

CHANGE 2 — Rewrite the preamble in _build_local_prompt() (around line 435–446).

The current preamble says:
  "You are running as a local fallback model (Gemini is unavailable). Answer from what is
   provided in the screen context. If you are not sure, say so clearly — do not invent details."

This is even more restrictive than the system prompt — it explicitly tells the model to answer
only from the screen. Replace it with:
  "You are Jarvis, a knowledgeable AI assistant (running locally; Gemini is unavailable).
   The screen content below is context — use it together with your own general knowledge to
   give a complete, useful answer. Do not fabricate what is literally on the screen; if asked
   to locate or read something specific that is not in the provided text, say you can't see it.
   But always answer knowledge and reasoning questions from your own knowledge."

Keep the existing "Be concise. Speak directly, no filler phrases." instruction.
```

**Verify:** With Gemini available: ask "what does this error mean?" on a screen showing an error
message. Jarvis should explain the error using general knowledge, not just quote the error back.
With Gemini disabled (GEMINI_API_KEY="" in .env): ask a general knowledge question like "what
is async/await?" — the local model should answer from knowledge, not say it cannot see the screen.
In both cases, if you ask "what does the red banner say?" and no red banner was captured, Jarvis
should still say it can't see it (honesty guard still works for screen-literal claims).

---

## Task 5: Make local fast-path knowledge-aware + label screen block by intent (Fixes K2C + K2D)

**Goal:** `PREFER_LOCAL_STRUCTURE=True` silently routes any TEXT query with decent screen
confidence to the weak local model — exactly the knowledge-seeking queries that need Gemini most.
Also the screen block is always labelled "Screen content" regardless of whether the query is
screen-grounded or knowledge-seeking, which reinforces screen-only framing.
**Files:** `gemini.py`, `classify.py`.
**Depends on:** Task 4 (prompt rewrite must be in place first).

```prompt
Read @jarvis/docs/diagnosis4.md §2.3 (PREFER_LOCAL_STRUCTURE problem) and §2.2 (screen as
context vs question) and fixes K2C + K2D.

Two changes in this task.

CHANGE 1 — Make the PREFER_LOCAL_STRUCTURE fast-path skip knowledge-seeking queries (gemini.py).

In ask_stream() around line 669, the HIGH-CONFIDENCE STRUCTURE local fast-path fires when:
  route_result.intent == Intent.TEXT and config.PREFER_LOCAL_STRUCTURE and ...

Before taking this fast-path, add a knowledge-query guard. Define a set of cue words/phrases
that signal the user wants reasoning or world knowledge rather than a screen read:

  _KNOWLEDGE_CUES = frozenset({
      "why", "how", "explain", "what does", "what is", "what are", "meaning",
      "means", "understand", "correct", "right", "wrong", "should", "could",
      "would", "recommend", "suggest", "better", "best", "fix", "solve",
      "difference", "compare", "help me", "tell me about",
  })

  def _is_knowledge_query(query: str) -> bool:
      q = query.lower()
      return any(cue in q for cue in _KNOWLEDGE_CUES)

Add this helper function near the top of gemini.py (not inside ask_stream). Then in the
fast-path condition, add: `and not _is_knowledge_query(query)`.

If a knowledge query matches PREFER_LOCAL_STRUCTURE, fall through to the Gemini path (which
has real world knowledge). The local path is still used for pure screen-read queries
("what does this say", "read this", "summarise this"), which don't match the knowledge cues.

CHANGE 2 — Label the screen block by query intent (gemini.py).

In both _build_initial_contents() and _build_local_prompt(), where the screen/perception block
is assembled and labelled, the label is currently hardcoded as either "Screen content" or
"Additional screen context" (based on whether focus is active).

When there is no focused element override (focus is None or not is_useful()), use the label:
- "Screen content" when the intent is TEXT (screen-grounded: read/summarise/locate)
- "Context (current screen — combine with your own knowledge to answer)" when the intent is
  anything else (knowledge/reasoning queries, NO_CONTEXT, etc.)

The intent is available as route_result.intent. Import Intent from classify if not already
imported at the call site. The label change is a one-line conditional per prompt builder.

Do not change the "Additional screen context" label that appears when focus is active — that
path is correct as-is.
```

**Verify:** Ask a knowledge question ("explain this function to me") on a screen with good OCR
confidence. In `~/.jarvis/jarvis.log` confirm `answer_source="gemini"` (not "local_answer") —
the local fast-path was skipped. Ask a pure screen-read question ("what does this tab say?") —
confirm `answer_source="local_answer"` (fast-path still fires for screen-grounded queries).
In the Gemini prompt (visible if DEBUG logging is on), the screen block for the knowledge query
should be labelled "Context (current screen — ...)".

---

## Task 6: Thin-read auto-escalation + stale-not-fatal (Fixes G1C + G1D)

**Goal:** When UIA+OCR both return thin text (few elements, low char count), the ladder today
returns that thin result and the model gets an inadequate context. Escalation only happens via
`should_escalate` which checks element confidence — a thin text blob has no confidence to trip
it. Separately, a transient `stale=True` from a window-resolve race vetoes the entire result
even when text was extracted successfully. Both issues discard usable reads.
**Files:** `perception.py`, `router.py`.
**Depends on:** Tasks 2 + 3 (stub replaced and fusion enabled first).

```prompt
Read @jarvis/docs/diagnosis4.md §1.3 (root cause C — ok/stale) and fix G1C and G1D.

Two changes in this task.

CHANGE 1 — Stale is a warning, not a veto when text was extracted (G1D). perception.py.

Currently in the fusion path (run_ladder), the result is:
  ok = (bool(sm.full_text.strip()) or entry == Rung.VISION) and not stale

And in perception.py:read_ocr (after Task 2, now the adapter delegation):
  ok = bool(text.strip()) and not stale

Change both so that stale degrades quality but does not discard a useful read:
  ok = bool(text.strip()) or entry == Rung.VISION   # stale no longer vetoes

Add a separate field to PerceptionResult to carry the staleness signal:
  Add `stale: bool = False` to the PerceptionResult dataclass.

Set result.stale = stale in both the fusion result and the adapter-delegation read_ocr.
In run_ladder, where the PerceptionResult is constructed for the fusion path, pass stale=stale.
In router.py, when building the prompt notice, if perception.stale is True, add a note:
  "[Note: screen capture may be slightly stale — window was being repositioned.]"
  alongside (or instead of) the cache notice.

CHANGE 2 — Auto-escalate on thin text before returning (G1C). perception.py + router.py.

Add two config constants in config.py:
  THIN_TEXT_CHAR_FLOOR = 80    # fewer chars than this → "thin" read
  THIN_TEXT_ELEM_FLOOR = 3     # fewer content elements than this → "thin" read

In run_ladder(), after the fusion ScreenModel is built but before returning, check if the result
is thin:
  is_thin = (
      sm.full_text and len(sm.full_text.strip()) < config.THIN_TEXT_CHAR_FLOOR
      and len([e for e in sm.elements if e.text]) < config.THIN_TEXT_ELEM_FLOOR
  )

If is_thin is True AND the current entry rung is below VISION AND policy permits the next rung:
  escalate in-place: re-run with the next rung (OCR→VISION).
  Return the escalated result if it is richer (more chars), otherwise return the original.
  Cap this in-ladder escalation to ONE step so it cannot loop.

This ensures that when UIA returns only a title bar and two labels, the ladder automatically
tries OCR before returning rather than making the model ask for it via need_deeper_rung.

Add a log line: _log.info("thin read (%d chars, %d elems) — auto-escalating", ...) so the
escalation is visible in jarvis.log.
```

**Verify:** On an app where UIA returns very little (e.g. a game or Electron app before the
fusion path runs): in logs, confirm "thin read — auto-escalating" appears, and the final rung
reached is deeper than the entry rung. On a normal rich app, confirm no thin-read escalation
fires. For the stale fix: resize/move a window quickly and immediately query — confirm the
answer is still returned (stale no longer causes ok=False) and the stale notice appears in
the model prompt.

---

## Task 7: Structured per-turn trace + richer telemetry (Fixes L2 + L3)

**Goal:** Deliver the explicit ask from Feedback 3: "see the request of the user, the steps
Jarvis takes, what is invoked." Layer 1 (Task 1) surfaced the existing debug logs. This task
adds a structured per-turn trace written to `~/.jarvis/traces.jsonl` — one JSON line per turn,
keyed by `turn_id`, with every stage recorded. Also extends the existing telemetry JSONL with
richer fields so it correlates with the trace.
**Files:** `trace.py` (new), `main.py`, `router.py`, `perception.py`, `focus_resolver.py`,
           `gemini.py`, `telemetry.py`.
**Depends on:** Tasks 1–4 (logging on, perception fixed, prompts fixed — trace adds no value
on broken plumbing).

```prompt
Read @jarvis/docs/diagnosis4.md §3.1 (what the trace needs), §3.2 Layer 2 and Layer 3.

This task adds a lightweight structured trace system. The design principle: pass an optional
`trace` kwarg through the call chain; each stage calls trace.record(...) if trace is not None;
at the end of each turn the trace writes one JSON line. No stage requires a trace — passing None
is always valid.

STEP 1 — Create jarvis/trace.py (new file).

Define a TurnTrace class:
  - __init__(self, turn_id: str, wake_ts: float): stores the id and start timestamp, initialises
    a list of stage dicts.
  - record(self, stage: str, **kwargs): appends {"stage": stage, "ts": time.monotonic(), **kwargs}
    to the list. The stage names to use: INPUT, CLASSIFY, ROUTE, PERCEPTION, FOCUS, PROMPT, TOOLS,
    ANSWER, OUTCOME.
  - record_tool_call(self, name: str, args: dict, result_summary: str, budget_remaining: int):
    shorthand that calls record("TOOLS", ...) — there may be multiple tool calls per turn.
  - finish(self, **kwargs): appends an OUTCOME stage and writes the whole trace as one JSON line
    to ~/.jarvis/traces.jsonl (create the file / directory if needed). Appends, never overwrites.
    Never raises.
  - A property full_dict that returns the entire trace as a dict (used by finish).

STEP 2 — Thread turn_id + TurnTrace through the main flow (main.py).

In _answer_worker():
  - At the top, generate turn_id = str(uuid.uuid4()).
  - Create trace = TurnTrace(turn_id, time.monotonic()).
  - Call trace.record("INPUT", query=question, process=getattr(target,"process",""),
      title=getattr(target,"title",""), app_class=str(getattr(target,"app_class",""))).
  - Pass trace to router.route(question, session_ctx, trace=trace).
  - After route returns, call trace.record("CLASSIFY", act=..., perception_mode=...,
      router_source=route_result's telemetry fields).
  - Pass trace to gemini.ask_stream(..., trace=trace).
  - In the finally block after streaming, call trace.finish(answer_source=..., latency_ms=...).
  - Also pass turn_id to telemetry.build_record() so telemetry and trace correlate.

STEP 3 — Record perception in router.py + perception.py.

In router.route(), add optional trace: TurnTrace | None = None parameter.
After run_ladder() returns, call:
  if trace: trace.record("PERCEPTION", rung=perception.rung.name,
      source=perception.source, chars=len(perception.text or ""),
      element_count=len(sm.elements) if sm else 0,
      ok=perception.ok, stale=getattr(perception,"stale",False),
      used_cache=used_cache)

In run_ladder() in perception.py, add optional trace parameter. After fusion builds the
ScreenModel, call:
  if trace: trace.record("PERCEPTION_DETAIL", uia_count=len(uia_elems),
      ocr_count=len(ocr_elems), cv_count=len(cv_elems),
      fused_count=len(sm.elements), full_text_chars=len(sm.full_text or ""))

STEP 4 — Record focus resolution in focus_resolver.py.

In resolve_focus(), add optional trace parameter.
After a FocusResult is produced, call:
  if trace: trace.record("FOCUS", source=str(result.source),
      resolved_text=result.text[:80] if result.text else "",
      confidence=result.confidence, ambiguous=result.ambiguous)

STEP 5 — Record model tool calls in gemini.py.

In ask_stream(), add optional trace parameter.
Each time a tool call is executed (inside the for fc in actionable: loop), after getting the
result, call:
  if trace: trace.record_tool_call(fc.name, dict(fc.args or {}),
      result_summary=text_ctx[:100], budget_remaining=...)
Also record PROMPT stage before the first Gemini call:
  if trace: trace.record("PROMPT", answer_source_expected="gemini",
      screen_block_chars=len(initial_contents[0].text or "") if initial_contents else 0,
      history_turns=len([t for t in session.turns[-config.MODEL_HISTORY_TURNS:]]),
      image_attached=attach_image is not None)

STEP 6 — Extend telemetry.py _FIELDS with richer fields.

Add to the _FIELDS tuple:
  "turn_id", "element_count", "char_count", "tool_calls", "screen_block_chars"

In main.py where build_record() is called, pass the new fields from the route_result and trace.
Existing fields are unchanged; new ones default to None for backwards compatibility with old
telemetry readers.

STEP 7 — Add tools/trace_view.py (new file).

A small CLI script: python tools/trace_view.py [--last N] [--turn <turn_id>].
Reads ~/.jarvis/traces.jsonl, pretty-prints the last N turns (default 3) or a specific turn_id.
For each turn, print: turn_id, INPUT query + target, CLASSIFY axes, PERCEPTION rung/chars/ok,
FOCUS (if present), TOOLS called (names + result summaries), ANSWER latency + source.
This is the "see the request and steps" view the user asked for.
```

**Verify:** Trigger two voice queries. Run `python tools/trace_view.py --last 2`. Confirm each
turn shows: the query text, app/title, perception rung + char count, whether fusion ran, any
tool calls, and the answer source. Confirm `~/.jarvis/traces.jsonl` has one line per turn and
each line is valid JSON. Confirm the `turn_id` in traces.jsonl matches the `turn_id` in
`~/.jarvis/telemetry.jsonl` for the same turn.

---

## Dependency summary

```
Task 1  (logging on)                      — no deps, do first
Task 2  (stub OCR → adapter delegation)   — no deps (but verify with Task 1 logs)
Task 3  (fusion for STRUCTURE reads)      — depends on Task 2
Task 4  (system prompt + local prompt)    — no deps
Task 5  (local fast-path + screen label)  — depends on Task 4
Task 6  (thin-read escalation + stale)    — depends on Tasks 2 + 3
Task 7  (per-turn trace + telemetry)      — depends on Tasks 1–4 (needs working plumbing)
```

Critical path for "Jarvis can't see anything" symptom:
**Task 1** → **Task 2** → **Task 3** → **Task 6** (each step verifiable in logs)

Critical path for "Jarvis refuses to use its knowledge" symptom:
**Task 4** → **Task 5** (Task 4 alone fixes most of it)

Critical path for "we need logging to debug everything":
**Task 1** (immediate, free) → **Task 7** (structured trace with full chain)
