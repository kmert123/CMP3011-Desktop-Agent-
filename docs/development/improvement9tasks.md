# Jarvis — Diagnosis 5 Treatment Plan (Wave 9)

Treatment for the four core problems root-caused in `jarvis/docs/diagnosis.md`.
Four problems, ten tasks, ordered so each is independently shippable and dependencies precede dependents.
Each task is scoped to be handed to a coding agent as-is. The `prompt` block is what you paste.

## Conventions

- `@jarvis/docs/diagnosis.md §N` references the diagnosis. Section map:
  - §1 Problem 1 — perception / "ultimate seeing" (root causes A–C, fixes S1–S3)
  - §2 Problem 2 — knowledge refusal (root causes A–C, fixes K1–K3)
  - §3 Problem 3 — context bleed (root causes A–C, fixes C1–C3)
  - §4 Problem 4 — observability (gaps A–D, fixes L1–L4)
  - §5 Cross-cutting note · §6 Execution order · §7 Non-goals
- Tasks are ordered so dependencies always precede dependents. Do not skip ahead.
- Every task ends with a concrete **Verify** check. Treat a task as incomplete until it passes.
- The whole codebase lives under `jarvis/`. Unless a path starts with `tools/`, all file paths
  below are relative to `jarvis/`. The trace viewer lives at repo-root `tools/trace_view.py`.
- The prior wave (Wave 8) already added: `trace.py` (`TurnTrace`), `tools/trace_view.py`,
  the `stale` field on `PerceptionResult`, thin-read auto-escalation (G1C), and the
  knowledge-granting system prompt rewrite. This wave builds on that — do **not** re-do it.

## ⚠️ Local-first decision (read before starting)

**This deployment is LOCAL-FIRST.** Gemini is unavailable ~70% of the time (rate limits), so the
local models must be as strong as the hardware allows, and Gemini is used **opportunistically** —
when a key is set and the call succeeds — never as a hard dependency. Every answer and every visual
read must produce a good result with Gemini completely absent.

**Target hardware:** RTX 3060 Laptop (**6 GB VRAM**), 32 GB RAM, i7-11800H. The 6 GB VRAM is the
binding constraint: a 7B model at 4-bit (~5 GB) fits and runs fast; the VLM and the text LLM cannot
both sit in VRAM simultaneously, so Ollama load/unload cost on backend switch is expected and
acceptable.

**Chosen local model lineup (set in Task 0):**
- **Vision VLM:** `qwen2.5vl:7b` — strong open VLM for screen/UI/document understanding + grounding.
  Replaces `moondream` (2B), which is the single biggest current weakness.
  Fallback if it fails to load on this Ollama build: `llava:7b`.
- **Text LLM:** `qwen2.5:7b-instruct` — strong 7B reasoner that pairs with the VLM and fits 6 GB.
  Replaces `mistral-nemo:12b` (7.1 GB — spills to RAM and is slow to pair with the VLM).
- `moondream` is kept only as a last-ditch fast degraded VLM fallback.

**Pull before Task 0:**
```
ollama pull qwen2.5vl:7b
ollama pull qwen2.5:7b-instruct
```

**Routing principle for this wave:** the strong path is "best local model + (a) best structured
perception, (b) the screenshot when appearance matters, (c) screen as context not answer." If a
Gemini key is present AND the call succeeds, prefer it for the hardest visual/reasoning turns; on
ANY Gemini failure or absence, fall through to the strong local model — never to a refusal or a
stub. Tasks 2 and 4 below are written around this principle (local strong by default, Gemini
opportunistic), NOT "Gemini-only".

**Treatment summary (maps diagnosis → tasks):**

| Diagnosis fix | Problem | Task |
|---|---|---|
| Model swap — qwen2.5vl:7b + qwen2.5:7b-instruct, local-first config | Seeing+Knowledge: local stack too weak | 0 |
| L1 + L2 — trace the real prompt + real answer text | Observability (needed to verify everything else) | 1 |
| K1 — strong-model-default answerer (local-first, Gemini opportunistic) | Knowledge: weak/narrow path answers reasoning queries | 2 |
| K2 + K3 — one honest screen label + scoped honesty guard | Knowledge: prompt frames screen as the answer / refuses | 3 |
| S1 — VLM (local-first) gets the actual screenshot on visual intent | Seeing: pixels go to a weak/blind path | 4 |
| C1 + C2 — correct window_sig on assistant turn + deterministic turn ordering | Context bleed: history gating fed mislabelled data | 5 |
| C3 — thin-read prompt signal | Context bleed / blurting on sparse reads | 6 |
| S2 — color + shape on ScreenElement, rendered in prompt | Seeing: appearance never extracted | 7 |
| S3 — guaranteed VISION fallback on thin reads (finish G1E) | Seeing: hard apps still bottom out at OCR | 8 |
| L3 — per-adapter evidence sampling in the trace | Observability: "saw nothing" not diagnosable | 9 |
| L4 — `--full` replay view in trace_view | Observability: no single request→path→answer view | 10 |

**Phases:**
- **Phase A (CRITICAL, do first): Tasks 0–5.** Task 0 swaps in the strong local models; Task 1
  (trace) must land right after so every later task is verifiable. Together Phase A addresses all
  four symptoms.
- **Phase B (HIGH refinement): Tasks 6–8.** Durable structural hardening of seeing + bleed.
- **Phase C (observability completion): Tasks 9–10.** Complete the analysis loop.

---

## Task 0: Swap in the strong local model lineup (Model swap)

**Goal:** The local stack is the actual ceiling on quality: vision = moondream (2B, near-useless for
dense UI / color / shape), text = mistral-nemo:12b (7.1 GB, spills to RAM on a 6 GB card and is slow
to swap with a VLM). Replace both with 7B-class models that fit 6 GB at 4-bit and are dramatically
stronger at screen understanding and reasoning. This is the highest-leverage single change for a
local-first deployment; it lands first so every later "seeing"/"knowledge" task is built on the good
models.
**Files:** `config.py`. (Plus Ollama pulls — done by the user out-of-band.)
**Depends on:** none. **Pre-req:** `ollama pull qwen2.5vl:7b` and `ollama pull qwen2.5:7b-instruct`
have been run.

```prompt
This deployment is LOCAL-FIRST on an RTX 3060 Laptop (6 GB VRAM). The local models must be the
strongest that fit. Swap the model lineup in jarvis/config.py. Do NOT touch any other file in this
task — later tasks wire the routing; this task only changes which models are named.

The user has already run:
  ollama pull qwen2.5vl:7b
  ollama pull qwen2.5:7b-instruct

CHANGE 1 — Text LLM. In jarvis/config.py set:
  LOCAL_LLM_MODEL = "qwen2.5:7b-instruct"
Leave a comment: replaces mistral-nemo:12b; 7B 4-bit fits 6 GB VRAM and pairs with the VLM without
both spilling to RAM. Keep the commented alternative line(s) but update them to note the 14B option
(qwen2.5:14b-instruct) spills ~3 GB to RAM and should not be the default on a 6 GB card.

CHANGE 2 — Vision model name. config.py currently has MOONDREAM_MODEL = "moondream" and
VISION_MODEL = "auto". The codebase's local VLM path (local_vision.describe_image and
adapters/vision_adapter._vlm_moondream) calls Ollama with config.MOONDREAM_MODEL. To route the local
VLM to Qwen2.5-VL without renaming code symbols, add a new constant and repoint the existing one:
  LOCAL_VLM_MODEL = "qwen2.5vl:7b"     # strong local VLM for screen/UI/color/shape understanding
  LOCAL_VLM_FALLBACK_MODEL = "moondream"   # fast degraded fallback if the strong VLM errors/unloads
  MOONDREAM_MODEL = LOCAL_VLM_MODEL    # back-compat: existing local_vision / vision_adapter calls
                                       # read MOONDREAM_MODEL; repointing it routes them to Qwen-VL
Add a comment explaining MOONDREAM_MODEL is kept only as the symbol the existing Ollama calls read,
and now points at the strong VLM. (A later task may rename the symbol; for now repointing is the
minimal, safe change.)

CHANGE 3 — Vision backend / model selection for the answer path. Set:
  VISION_BACKEND = "local"     # local-first: keep local as the default vision backend
  VISION_MODEL = "auto"        # auto = local VLM first; Gemini only as opportunistic fallback
Add a comment that, per the local-first decision, Gemini is opportunistic (used when a key is set
AND the call succeeds) and the local VLM (now Qwen2.5-VL) is the default — Gemini is never required.
Do NOT set VISION_BACKEND="gemini" (that was the old cloud-first plan; this deployment is local).

CHANGE 4 — Local answer timeout headroom. Qwen-VL and a 7B text model on a 6 GB card with CPU spill
can take several seconds, and the VLM load on first call after a swap is slow. In config.py raise:
  LOCAL_ANSWER_TIMEOUT_MS = 40_000     # raised from 25_000: 6 GB card + VLM/LLM swap load cost
Keep LOCAL_LLM_TIMEOUT_MS (the fast router classification timeout) unchanged — that path must stay
snappy and falls back to regex on timeout.

After editing, sanity-check that config imports cleanly:
  python -c "import sys; sys.path.insert(0,'jarvis'); import config; print(config.LOCAL_LLM_MODEL, config.MOONDREAM_MODEL, config.VISION_MODEL, config.VISION_BACKEND, config.LOCAL_ANSWER_TIMEOUT_MS)"
Expected: qwen2.5:7b-instruct qwen2.5vl:7b auto local 40000
```

**Verify:** `ollama list` shows `qwen2.5vl:7b` and `qwen2.5:7b-instruct`. The config import check
above prints the expected line. Run `ollama run qwen2.5vl:7b "hi"` once to confirm the VLM loads on
this Ollama build; if it errors, set `LOCAL_VLM_MODEL = "llava:7b"` (after `ollama pull llava:7b`)
and re-run the check. No application code is changed in this task.

---

## Task 1: Trace the real prompt and the real answer (Fixes L1, L2)

**Goal:** The per-turn trace (`TurnTrace`) already records stage *counts* (`screen_block_chars`,
`history_turns`, answer `chars`) but never the *content*. To debug Problems 1–3 you must be able
to read the literal prompt that was sent and the literal answer that came back. This task makes the
trace a faithful replay. It lands first because no other fix in this wave can be verified without it.
**Files:** `gemini.py`, `main.py`, `trace.py` (no signature change needed, just new payload fields).
**Depends on:** none.

```prompt
Read @jarvis/docs/diagnosis.md §4 (Problem 4 — observability), gaps A and B, and fix L1 + L2.

Context: jarvis/trace.py already defines TurnTrace with .record(stage, **kwargs) and .finish().
gemini.py:ask_stream already records a "PROMPT" stage and main.py:_stream_answer already records
an "ANSWER" stage, but both store only lengths, not text. Make the trace capture the actual content.

CHANGE 1 — Capture the real prompt text (L1). In jarvis/gemini.py, inside ask_stream(), find the
block that does `if trace is not None:` and records trace.record("PROMPT", ...). The initial Gemini
turn is built from `initial_contents` (a list[types.Part]) via _build_initial_contents(). Add the
literal assembled prompt text to that PROMPT record:
  - Concatenate the text of every Part that has a non-empty `.text` attribute into one string
    (the same parts already summed for screen_block_chars). Call it prompt_text.
  - Add a kwarg `prompt_text=prompt_text[:4000]` to the existing trace.record("PROMPT", ...) call.
    Keep all existing kwargs (answer_source_expected, screen_block_chars, history_turns,
    image_attached). 4000 chars is a generous cap; truncate, do not drop.
  - Also add `system_prompt=_SYSTEM_PROMPT[:1500]` so the trace shows which system instruction was
    in force (it changes across waves; capturing it makes traces self-describing).

CHANGE 2 — Capture the real answer text (L2). In jarvis/main.py, inside _stream_answer(), find the
`finally:` block where `full_text = "".join(chunks)` is computed and trace.record("ANSWER", ...) is
called. Add a kwarg `answer_text=full_text[:4000]` to that ANSWER record. Keep the existing kwargs
(escalated, escalated_rung, answer_source, chars).

CHANGE 3 — Also handle the early-return path. In _stream_answer there is an early `except
StopIteration:` branch that calls trace.finish(...) directly (when the generator yields nothing).
In that branch, before finish(), add `trace.record("ANSWER", answer_text="", answer_source=source,
chars=0)` so every turn has exactly one ANSWER stage regardless of path.

Do not change trace.py — TurnTrace.record already accepts arbitrary kwargs and serialises them.
Do not log prompt/answer text to the standard logger or telemetry (that is a separate file with
different retention); keep it inside the trace JSONL only.
```

**Verify:** Run one query end-to-end (voice or typed). Open `~/.jarvis/traces.jsonl`, take the last
line, and confirm the `PROMPT` stage has a non-empty `prompt_text` and `system_prompt`, and the
`ANSWER` stage has a non-empty `answer_text`. Then run `python tools/trace_view.py --last 1` and
confirm it still prints without error (it ignores the new fields for now).

---

## Task 2: Strong-model-default answering (local-first, Gemini opportunistic) (Fix K1)

**Goal (local-first reframing of K1):** `ask_stream` has a "high-confidence STRUCTURE local
fast-path" whose problem is NOT that it uses the local model — local-first, it *should* — but that
it routes reasoning/judgement queries ("is this safe?", "what's wrong here?", "should I click
this?") into a **narrow read-back prompt** and returns immediately, so the model only parrots the
screen. The fix is to (a) keep local as the default brain (now the strong qwen2.5:7b-instruct from
Task 0), (b) make the fast-path fire ONLY for literal read-back and defer everything reasoning-shaped
to the full answering path (which carries the better system prompt + knowledge framing from Task 3),
and (c) when a Gemini key IS present, prefer Gemini for the hardest turns but ALWAYS fall through to
a strong local answer on any Gemini failure/absence — never to a refusal.
**Files:** `config.py`, `gemini.py`.
**Depends on:** Tasks 0, 1 (strong local models in place; trace shows which model answered).

```prompt
Read @jarvis/docs/diagnosis.md §2 (Problem 2), root cause A, and fix K1. NOTE: this deployment is
LOCAL-FIRST (Gemini unavailable ~70% of the time). Do NOT make Gemini a hard default. The goal is a
strong answer whether or not Gemini is reachable.

Context: jarvis/gemini.py:ask_stream has a STRUCTURE local fast-path gated on
config.PREFER_LOCAL_STRUCTURE (currently True). It uses _build_local_prompt (a narrower prompt) and
returns before the full answering path runs. The bug is that reasoning/judgement queries fall into
this narrow path and get read-back answers. The local model itself is fine (Task 0 upgraded it to
qwen2.5:7b-instruct) — the prompt framing and the gate are the problem.

Make three changes. Keep ALL local code paths — they are now first-class, not just fallbacks.

CHANGE 1 — Narrow the fast-path to literal read-back only. In jarvis/gemini.py the STRUCTURE
fast-path condition includes `and not _is_knowledge_query(query)`. Add a sibling helper
_is_reasoning_or_judgement_query(query) that returns True when the query (case-insensitive,
word-ish match) contains any of:
  "is this", "is it", "should i", "what's wrong", "whats wrong", "what is wrong", "safe", "risk",
  "recommend", "better", "best", "why", "how do i", "explain", "what happens", "what would",
  "could i", "can i", "is there a problem"
AND require the fast-path condition to additionally include `and not
_is_reasoning_or_judgement_query(query)`. Keep the existing _KNOWLEDGE_CUES / _is_knowledge_query.
Net effect: the fast-path (narrow read-back prompt) now ONLY handles pure read-back like "summarise
this" / "what does this say"; everything reasoning-shaped goes to the full answering path.

CHANGE 2 — Keep PREFER_LOCAL_STRUCTURE = True (local-first), but make it mean "use the local
fast-path for read-back", not "skip the strong path for reasoning". Update its comment in config.py
to say exactly that. (We are NOT flipping it to False — that was the old cloud-first plan. Local IS
the default here.) The reasoning queries excluded by CHANGE 1 now flow to the main path below.

CHANGE 3 — Make the main answering path local-first with opportunistic Gemini. Today the main path
goes straight to Gemini and only falls back to local on connection error. Restructure ask_stream's
main path (after the two fast-path blocks) so it is:
  - If config.GEMINI_API_KEY is set: try Gemini first (existing streaming code). On ANY exception or
    empty response, fall through to the local answer instead of yielding an error string. Set
    meta["answer_source"] accordingly ("gemini" on success, "local_fallback" when Gemini failed).
  - If config.GEMINI_API_KEY is NOT set: skip Gemini entirely and answer from the local model using
    the FULL knowledge-framed prompt. IMPORTANT: this must use the same strong framing as the Gemini
    path, not the narrow _build_local_prompt read-back framing. If _build_local_prompt is the only
    local prompt builder, extend it (or add _build_local_answer_prompt) so the local reasoning answer
    gets the same "use your own knowledge + screen is context, not the answer" framing that Task 3
    installs in the system prompt. Stream it via _local_stream and set meta["answer_source"] =
    "local_answer".
  - Never yield a bare "GEMINI_API_KEY is not set" error when no key is present — that path must
    produce a real local answer. (Remove/replace the current early `yield "GEMINI_API_KEY is not
    set…"` return so it routes to the local answer instead.)

The result: with no Gemini key, reasoning questions still get a strong, knowledge-using answer from
qwen2.5:7b-instruct; with a key, Gemini is tried first and local catches every failure.
```

**Verify:** With NO Gemini key set (the common case), ask a screen-grounded reasoning question
(point at an error, ask "is this serious?"). Confirm in the last trace that `OUTCOME.answer_source`
is `local_answer` (NOT `local_no_context` and NOT an error), and that `PROMPT.prompt_text` shows the
knowledge-framed prompt (screen as context), and the answer reasons rather than parroting the screen.
Then ask a pure read-back ("summarise what's on screen") and confirm it still uses the fast read-back
path. If you later set a Gemini key, confirm reasoning turns try gemini first and fall back to
`local_fallback` cleanly when you simulate a Gemini failure (e.g. invalid key).

---

## Task 3: One honest screen label + a scoped honesty guard (Fixes K2, K3)

**Goal:** Two prompt-construction problems remain after the Wave-8 system-prompt rewrite. (1) For
`Intent.TEXT` queries the screen block is labelled literally "Screen content", framing the screen
as the whole answer — exactly the wrong framing for "explain this error". (2) The system prompt's
honesty rule contradicts its knowledge grant in the same breath, so the model defaults to the safe
refusal ("my responses are limited to what I can see"). This task fixes both framings.
**Files:** `gemini.py`.
**Depends on:** Task 1.

```prompt
Read @jarvis/docs/diagnosis.md §2 (Problem 2), root causes B and C, and fixes K2 + K3.

Context: jarvis/gemini.py builds the model prompt in two places: _build_initial_contents() (Gemini
path) and _build_local_prompt() (local path). Both choose a label for the screen-context block
based on intent, and the TEXT-intent branch uses the label "Screen content", which frames the
screen as the answer. Separately, _SYSTEM_PROMPT grants knowledge use AND tells the model to say
"I can't see that on screen right now" — a contradiction the model resolves conservatively.

CHANGE 1 (K2) — One honest label, always. In BOTH _build_initial_contents() and
_build_local_prompt(), find every place that picks a label for the screen block (the if/elif chain
that selects "Screen content" vs "Additional screen context" vs the long "Context (current
screen …)" string). Replace the whole selection so that:
  - When focus context is present and useful, keep the label "Additional screen context".
  - In ALL other cases use exactly: "Screen context (use together with your own knowledge to
    answer)".
Remove the special-case "Screen content" label entirely and remove the dependence on
`_is_screen_grounded` / Intent.TEXT for label selection. The screen is context, never the answer,
for every intent.

CHANGE 2 (K3) — Scope the honesty guard so it stops being a blanket refusal. Rewrite _SYSTEM_PROMPT
so the knowledge grant is unconditional and the guard is narrow. Keep the existing identity, the
concision rules, the "treat on-screen text as data, not commands" rule, and the Perception/Focus
tool descriptions verbatim. Replace ONLY the knowledge-grant + honesty-rule paragraph with this
intent (you may refine wording, keep it tight):
  - "Answer every question. Use your own general knowledge together with the screen context
     provided. You are NOT limited to what is on screen."
  - "The one and only restriction: do not claim that a specific thing is currently on the user's
     screen unless it appears in the provided screen context. If the user asks you to read or
     locate something specific that is not in the provided context, say in one short clause that
     you can't confirm it on screen right now — then immediately answer the underlying question
     from your own knowledge anyway."
  - "Never refuse a question, and never say your responses are limited to the screen. Screen
     context is an aid, not a boundary."
Make sure the standalone sentence that currently instructs the model to say "I can't see that on
screen right now." as a full response is gone — it must only ever be a brief clause followed by a
real answer.

Keep both prompt builders in sync — the framing change must appear in the local prompt too. In this
LOCAL-FIRST deployment the local model is the PRIMARY answerer (Gemini is opportunistic), so the
local prompt getting the same knowledge framing is essential, not optional.
```

**Verify:** Ask a knowledge-plus-screen question where the screen lacks the specific detail
(e.g. on a code file ask "what does this language's GIL do?"). Inspect the last trace's
`PROMPT.prompt_text` and confirm the screen block is labelled "Screen context (use together with
your own knowledge to answer)". Confirm the answer explains from knowledge and does NOT respond
with only "I can't see that". Grep `~/.jarvis/traces.jsonl` for the phrase "limited to" in recent
`answer_text` values and confirm it no longer appears.

---

## Task 4: Route visual queries to a strong VLM (local-first Qwen2.5-VL, Gemini opportunistic) (Fix S1)

**Goal (local-first reframing of S1):** On a visual query the pixels must reach a *strong* VLM. In
the local-first deployment that strong VLM is **qwen2.5vl:7b** (set in Task 0) — a real upgrade over
moondream and capable of color/shape/layout/icon understanding. The path must: (a) when Gemini is
available, attach the actual screenshot to the Gemini request (Gemini is natively multimodal); (b)
when Gemini is absent (the common case), produce a high-quality **Qwen2.5-VL** description of the
screenshot and feed THAT into the answer, instead of a weak moondream paragraph. Either way, the
final answer is grounded in a strong look at the pixels, never blind.
**Files:** `config.py` (verify Task-0 values), `gemini.py`, `adapters/vision_adapter.py` (ensure the
local VLM path is the strong one), `local_vision.py` (uses `MOONDREAM_MODEL`, now repointed in Task 0).
**Depends on:** Tasks 0, 1, 2 (strong local VLM pulled + named; local-first answering in place).

```prompt
Read @jarvis/docs/diagnosis.md §1 (Problem 1), root cause A, and fix S1. NOTE: LOCAL-FIRST — the
strong VLM is the local qwen2.5vl:7b (config.MOONDREAM_MODEL was repointed to it in Task 0). Gemini
is opportunistic, used only when a key is set AND the call succeeds. The answer must be grounded in
a strong VLM look at the pixels whether or not Gemini is available.

Context: jarvis/gemini.py:_build_initial_contents() decides what to do with perc.image. Today it
only attaches the image to the request when (is_visual or low_conf) AND VISION_BACKEND != "local";
under VISION_BACKEND="local" it instead calls local_vision.describe_image (which, before Task 0,
hit moondream — now it hits qwen2.5vl:7b because MOONDREAM_MODEL was repointed). A text-only Ollama
LLM (qwen2.5:7b-instruct) cannot receive an image part, so for the LOCAL path the correct design is:
run the strong local VLM (qwen2.5vl) on the screenshot to produce a rich description, and inject
that description as text for the local text LLM to answer over. For the GEMINI path, attach the raw
image directly (Gemini is multimodal).

Make these changes.

CHANGE 1 — Confirm config (from Task 0). Ensure jarvis/config.py has VISION_BACKEND = "local",
VISION_MODEL = "auto", MOONDREAM_MODEL = LOCAL_VLM_MODEL = "qwen2.5vl:7b". If Task 0 was applied,
these are already set — do not change them here. (Do NOT set VISION_BACKEND="gemini".)

CHANGE 2 — Make the local VLM description strong and explicit. In _build_initial_contents(), in the
branch where config.VISION_BACKEND == "local" and (is_visual or low_conf) and perc.image is not
None: instead of a generic describe call, pass a screen-understanding prompt that asks the VLM to
report layout, key text, colors, shapes, and notable UI elements with their approximate positions.
Use local_vision.describe_image(perc.image, <that prompt>) — which now routes to qwen2.5vl:7b — and
append the result as a text block labelled "Detailed screen view (local vision model):" so the
answering text LLM (or Gemini) treats it as high-quality grounding. Keep the question itself
appended afterward as today.
  - Prefer routing this through adapters/vision_adapter.ask_vlm(perc.image, query) rather than
    local_vision.describe_image directly, so the same backend-selection logic (VISION_MODEL="auto":
    local VLM first, Gemini fallback only if a key is set and local fails) is reused. If ask_vlm's
    plain-answer mode (ask_elements=False) returns VlmResult.text, use that as the description.

CHANGE 3 — Gemini path attaches the real image (opportunistic). Keep a branch: if a Gemini key is
set AND VISION_BACKEND would use Gemini for this turn (e.g. VISION_MODEL in ("gemini",) or the
"auto" path elected Gemini), set attach_image = Image.fromarray(cv2.cvtColor(perc.image,
COLOR_BGR2RGB)) so the screenshot is attached via the existing _pil_to_part(attach_image) Part. In
the local-first default (no key / auto→local) this branch simply won't fire and the strong local
description from CHANGE 2 carries the grounding. Do not error if there is no key.

CHANGE 4 — Make the local VLM call robust to load latency. The first qwen2.5vl call after an LLM
swap can take several seconds (model load). Ensure local_vision.describe_image's timeout is generous
(it currently uses timeout=30 — keep or raise to 60). On timeout/error it returns an error string;
when the description string looks like an error (starts with "Local vision" or "Cannot reach"),
do NOT inject it as grounding — fall back to attaching nothing and letting the structured
ScreenModel (plus Task 7 color/shape) carry the answer, so a slow/cold VLM never blocks the turn
with a broken description.

Leave read_vision() in perception.py to Task 8 (the perception-rung VISION fallback). This task is
about the answer-path grounding only.
```

**Verify:** With NO Gemini key, open something with distinct colors/shapes (a chart, a colored
button bar) and ask "what color is the primary button?" / "what shape is the icon top-left?".
Confirm in the last trace that `PROMPT.prompt_text` contains a "Detailed screen view (local vision
model):" block with a substantive description (proving qwen2.5vl ran, not moondream), and that the
answer correctly names the color/shape. Confirm `OUTCOME.answer_source` is `local_answer`. Time the
first such query (cold VLM load) and confirm it completes within the raised timeout. If you set a
Gemini key and force the Gemini path, confirm `PROMPT.image_attached == true` instead.

---

## Task 5: Correct the assistant turn's window_sig + deterministic turn ordering (Fixes C1, C2)

**Goal:** The history machinery demotes turns recorded on a different window, but it never works for
*answers*: the assistant turn is stored with an empty `window_sig`, which the gate treats as
"same window", so prior answers always leak into a new, unrelated question. Also, the user turn is
recorded *after* streaming while the assistant turn is recorded by the actor on `AnswerDone`,
allowing mis-paired Q/A under thread timing. This task makes both halves of every exchange carry
the same window_sig and be recorded in a deterministic order.
**Files:** `core/events.py`, `core/session_actor.py`, `main.py`.
**Depends on:** Task 1.

```prompt
Read @jarvis/docs/diagnosis.md §3 (Problem 3), root causes A and B, and fixes C1 + C2.

Context: session_context.to_prompt_block() decides whether a stored turn is "[different window]"
by comparing each turn's window_sig to the current one, and treats an empty sig as "not
cross-window". The assistant turn is added in core/session_actor.py:_handle_answer_done() via
self._session.add_turn("assistant", event.full_text) with NO window_sig — so it is always empty,
so every answer escapes cross-window demotion. Separately, main.py:_answer_worker records the user
turn AFTER _stream_answer returns, while the assistant turn is added by the actor on AnswerDone,
so the two halves of one exchange can be recorded out of order.

Make three changes. The window_sig string format used elsewhere is
"process|app_class_value|title" (see _answer_worker and gemini._make_window_sig). Reuse that format.

CHANGE 1 — Carry window_sig on the AnswerDone event. In jarvis/core/events.py, add a field to the
AnswerDone dataclass:
  window_sig: str = ""
It is a frozen dataclass; add it with a default so existing constructors that omit it still work.

CHANGE 2 — Plumb the window_sig from the answer worker to AnswerDone. In jarvis/main.py:
  - Compute the window_sig string ONCE in _answer_worker (it is already computed near the end for
    the user turn). Pass it into _stream_answer as a new keyword arg `window_sig`.
  - In _stream_answer, include window_sig=window_sig on EVERY AnswerDone the bus posts (there are
    two: the StopIteration early-return path and the normal finally path).

CHANGE 3 — Tag the assistant turn and fix ordering. In jarvis/core/session_actor.py
_handle_answer_done(), change the add_turn call to:
  self._session.add_turn("assistant", event.full_text, window_sig=event.window_sig)

CHANGE 4 — Record the user turn deterministically. In jarvis/main.py:_answer_worker, MOVE the
`session_ctx.add_turn("user", question, window_sig=window_sig)` call to BEFORE _stream_answer is
invoked (right after the escalation block, before self._stream_answer(...)). This guarantees the
user turn is in history before its answer is appended by the actor, so the pair is always ordered
user-then-assistant. (If the turn is cancelled mid-stream, an unanswered user turn in history is
harmless and correctly reflects what happened.)

Do not change to_prompt_block()'s gating logic — it is correct; it was just being fed empty sigs.
```

**Verify:** Ask a question in App A, then switch to App B and ask an unrelated question. Inspect the
second turn's `PROMPT.prompt_text`: the App-A exchange (both the user line AND the assistant line)
must be prefixed with "[different window]" in the History block. Before this fix the assistant line
was not prefixed. Confirm the new answer does not blend App-A content.

---

## Task 6: Signal thin perception in the prompt instead of going silent (Fix C3)

**Goal:** When perception is sparse, the content-floor demotion drops history (good) but the model
still gets a near-empty screen block and the raw question, and tends to pad/"blurt". This task adds
one explicit instruction on thin turns so the model answers from knowledge rather than narrating an
empty screen.
**Files:** `gemini.py`, `session_context.py` (read-only reference), `perception.py` (reuse the thin signal).
**Depends on:** Tasks 1, 3.

```prompt
Read @jarvis/docs/diagnosis.md §3 (Problem 3), root cause C, and fix C3. Also re-read the thin-read
logic added in Wave 8 in perception.py (config.THIN_TEXT_CHAR_FLOOR / THIN_TEXT_ELEM_FLOOR).

Context: When a screen read is thin, session_context.to_prompt_block() already demotes history to
zero turns, but nothing tells the model that the screen read itself was weak. With an almost-empty
screen block the model pads. We want a single, explicit prompt line on thin turns.

CHANGE — In jarvis/gemini.py, in BOTH _build_initial_contents() and _build_local_prompt(), after
the screen-context block is appended, detect a thin read and append one short notice line.

Detection: a read is thin when the perception screen_model is present AND
  len((sm.full_text or "").strip()) < config.THIN_TEXT_CHAR_FLOOR
  AND len([e for e in sm.elements if e.text]) < config.THIN_TEXT_ELEM_FLOOR
(use the same two config floors already used by perception.py so the definition stays consistent).
If perception has no screen_model but perc.text is very short (< THIN_TEXT_CHAR_FLOOR), also treat
as thin.

When thin, append this as its own text part / line (not inside the screen block):
  "[Note: the screen read was sparse this turn. Answer from your own knowledge; do not describe an
   empty or near-empty screen, and do not invent screen contents.]"

Place it AFTER the screen-context block and BEFORE the final "User: {query}" line so the model reads
it as a directive about the context it was just given. Keep it to that one line. Do not change the
demotion logic in session_context.py.
```

**Verify:** Trigger a thin read (a mostly-blank window, or an app where UIA+OCR return little) and
ask a general question. Confirm in `PROMPT.prompt_text` that the "[Note: the screen read was sparse
…]" line is present, and that the answer addresses the question from knowledge rather than saying
the screen is empty.

---

## Task 7: Extract color + shape into the structured model and render them (Fix S2)

**Goal:** `ScreenElement` carries no appearance, so any "the red button" / "the green check"
question has no structured grounding — it requires a full VLM round-trip. This task samples a
dominant color and a coarse shape hint from each element's pixels during fusion and renders them in
the prompt, giving cheap deterministic appearance grounding.
**Files:** `screen_model.py`, `adapters/cv_adapter.py`, `fusion.py`.
**Depends on:** Tasks 1, 4.

```prompt
Read @jarvis/docs/diagnosis.md §1 (Problem 1), root cause B, and fix S2.

Context: jarvis/screen_model.py:ScreenElement has fields role/text/bbox/source/confidence/invokable
and tree fields, but nothing about appearance. fusion.fuse() receives the full BGR `frame` and the
elements with bboxes, so it has everything needed to sample appearance. cv_adapter already has the
crop.

Make three changes. Keep everything optional and defaulted so existing callers and fixtures are
unaffected.

CHANGE 1 — Add appearance fields to ScreenElement (jarvis/screen_model.py). Add, with defaults:
  dominant_color: tuple[int, int, int] | None = field(default=None, repr=False)   # (R,G,B) 0-255
  color_name: str = field(default="", repr=False)        # nearest human color name, e.g. "red"
  shape_hint: str = field(default="", repr=False)         # "" | "rect" | "round" | "icon" | "line"
Add a module-level helper `nearest_color_name(rgb)` that maps an (R,G,B) tuple to the closest name
from a small fixed palette (black, white, gray, red, orange, yellow, green, teal, blue, purple,
pink, brown) by Euclidean distance in RGB. Keep it dependency-free.

CHANGE 2 — Sample appearance during fusion (jarvis/fusion.py). After the final element list is
built and tagged (just before assembling full_text / building the ScreenModel), add a pass that,
for each element with a positive-area bbox, crops the corresponding region from `frame` and fills:
  - dominant_color: the median BGR of the element's interior (convert to RGB for storage). Use the
    median, not mean, so a single text color doesn't dominate a button fill. Subsample for speed:
    if the crop is large, take a strided sample (e.g. every 4th pixel) — this runs per element so
    keep it cheap.
  - color_name: nearest_color_name(dominant_color).
  - shape_hint: a coarse heuristic from the bbox aspect ratio and size:
       aspect = w / max(1, h)
       "line"  if h <= 4 or w <= 4
       "icon"  if 0.7 <= aspect <= 1.4 and max(w, h) <= 48
       "round" if the bbox is roughly square AND a quick contour test on the crop finds a circle-ish
               blob (optional; if you skip the contour test, fold this into "icon")
       "rect"  otherwise
  Guard the whole pass in try/except per element so a bad crop never breaks fusion. `frame` is in
  window-local coordinates and element bboxes are in virtual-desktop coords — subtract the window
  origin to index into `frame`. fuse() is called with the crop as `frame` and the elements already
  carry absolute bboxes; reuse the same origin-subtraction pattern used by the content-region
  re-OCR block in perception.py (origin = capture origin). If fuse() does not currently receive the
  origin, thread it through from perception.run_ladder's fusion call (it already has `origin`).

CHANGE 3 — Render appearance in the prompt (jarvis/screen_model.py:to_prompt_block). In the line
format for each element, when color_name and/or shape_hint are non-empty and the element is
text-bearing-or-invokable, append a compact appearance tag, e.g.:
  [Button] Submit (green, rect) @ (x,y,w,h) [invokable]
Only add the parenthetical when at least one of color_name/shape_hint is set, and keep it short so
the token budget is respected. Do not append appearance to pure layout/region rows.

Performance note: the per-element sampling must stay cheap (subsampled median + arithmetic). If
profiling shows it is slow on element-dense screens, cap it to the top-N elements by area or to
in_content_region elements only.
```

**Verify:** On a screen with clearly colored buttons, run a turn and inspect the last trace's
`PROMPT.prompt_text`: element lines should show appearance tags like "(green, rect)". Ask "which
button is red?" and confirm the answer uses the structured color (it should work even if the
screenshot is NOT attached, proving the structured grounding). Confirm fusion still succeeds on a
plain-text window (no appearance tags is fine there).

---

## Task 8: Guaranteed VISION fallback on thin reads — finish G1E (Fix S3)

**Goal:** Wave-8 thin-read auto-escalation only steps one rung (UIA→OCR) and only reaches VISION
when entry was already OCR. For dark/custom-drawn apps where UIA *and* OCR are both thin, the ladder
stops at OCR and the strong VLM never sees the pixels — the exact "couldn't see anything" residue.
This task adds a final, capped VISION fallback that fires when text remains thin after escalation.
**Files:** `perception.py`, `config.py`.
**Depends on:** Tasks 1, 4 (Gemini vision must be wired first).

```prompt
Read @jarvis/docs/diagnosis.md §1 (Problem 1), root cause C, and fix S3 (finishing G1E). Re-read
the existing thin-read auto-escalation block in jarvis/perception.py:run_ladder (the fusion path,
the block commented "G1C: auto-escalate on thin reads before returning").

Context: After fusion and the one-step G1C escalation, a hard app can still have full_text below
THIN_TEXT_CHAR_FLOOR and fewer than THIN_TEXT_ELEM_FLOOR text elements. Today the function returns
that thin result. We want: when still thin AND the app class is "hard", capture once and send the
screenshot to the VLM, merging any returned element refs and attaching the image to the result so
the answer path (Task 4) shows Gemini the pixels.

CHANGE 1 — config. In jarvis/config.py add:
  VISION_THIN_FALLBACK = True              # master flag for the post-OCR VISION fallback
  VISION_THIN_FALLBACK_APP_CLASSES = ("chromium_electron", "game_fullscreen", "unknown", "uwp")
Add a one-line comment that this is the G1E fallback: it fires at most once per read when UIA+OCR
both came back thin on a hard-to-read app.

CHANGE 2 — perception.py. In run_ladder's fusion path, AFTER the existing G1C escalation block and
BEFORE building the final PerceptionResult, add a second guard:
  - Recompute is_still_thin using sm.full_text and text-bearing element count against the two
    THIN floors (same definition as G1C).
  - Compute app_class_value from target.app_class (None-safe).
  - If config.VISION_THIN_FALLBACK AND is_still_thin AND app_class_value in
    config.VISION_THIN_FALLBACK_APP_CLASSES AND (policy is None or policy.run_vision) AND entry <
    Rung.VISION:
      * Call adapters.vision_adapter.ask_vlm(crop, "", ask_elements=True). LOCAL-FIRST: with
        VISION_MODEL="auto" (Task 0) this runs the local strong VLM qwen2.5vl:7b for element
        detection, and only falls back to Gemini if a key is set and the local call fails. Either
        way it returns structured ElementRefs.
      * If the result is ok and has refs, convert each ElementRef to a ScreenElement (image-local
        bbox + origin → virtual-desktop bbox; source="vision"; role from ref.role; text=ref.label;
        confidence=ref.confidence; invokable=False) and fuse them in via _fuse(target,
        list(sm.elements), [], vision_elems_as_cv_position?, crop, stale=stale) — pass the vision
        elements through the OCR slot of _fuse so they are treated as text evidence (they carry
        labels). Keep the richer model only if it increased full_text length, mirroring the G1C
        keep-if-better check.
      * REGARDLESS of whether refs were returned, set a local flag attach_vision_image=True so the
        returned PerceptionResult carries image=crop. This is the critical part: even when the VLM
        returns no structured refs, the answer path must still receive the raw screenshot so the
        answer-path VLM (local qwen2.5vl description, or Gemini if a key is set — see Task 4) can
        look at the pixels. 
  - When building the final PerceptionResult, set image=crop when attach_vision_image is True (in
    addition to the existing `entry == Rung.VISION` case). Leave the rung label as `entry` (the
    caller/telemetry can still see it escalated via the trace), or optionally set rung=Rung.VISION
    when the fallback fired — pick whichever keeps should_escalate/telemetry consistent and note
    your choice in a comment.

Cap: this fallback runs at most once per run_ladder call (no loop). Wrap the ask_vlm call in
try/except so a VLM failure degrades to returning the thin-but-real OCR result, never an exception.

LOCAL-FIRST PERFORMANCE NOTE: on a 6 GB card the local qwen2.5vl call here costs ~2–8 s (more on
the first call after a text-LLM swap, due to model load). That is acceptable because this fallback
only fires on hard apps where UIA+OCR already failed — it is the exception, not every turn. Keep the
gate tight (the app-class allow-list + thin-after-escalation condition) so it never fires on normal
text apps. Do NOT call the VLM speculatively on every read.

Record it in the trace if a `trace` is available: trace.record("PERCEPTION_DETAIL",
vision_fallback=True, vision_refs=<count>) so Task 9/10 can show that the fallback fired.
```

**Verify:** On a dark-mode Electron app (or any app where UIA+OCR are sparse), run a "what's on
screen" query. In the last trace confirm a `PERCEPTION_DETAIL` stage with `vision_fallback: true`,
and that the final `PROMPT.image_attached == true`. Confirm the answer describes actual screen
content. On a normal text app, confirm the fallback does NOT fire (no `vision_fallback` key).

---

## Task 9: Sample per-adapter evidence in the trace (Fix L3)

**Goal:** PERCEPTION_DETAIL records adapter element *counts* but not *which* elements, so
"Jarvis sees nothing" is not diagnosable from the trace — you can't tell whether UIA returned two
chrome labels and OCR returned zero, or vice versa. This task adds small text samples per adapter.
**Files:** `perception.py`.
**Depends on:** Task 1.

```prompt
Read @jarvis/docs/diagnosis.md §4 (Problem 4), gap C, and fix L3.

Context: jarvis/perception.py:run_ladder (fusion path) already records a PERCEPTION_DETAIL trace
stage with uia_count/ocr_count/cv_count/fused_count/full_text_chars right after sm = _fuse(...).
Add small samples so "saw nothing" is diagnosable.

CHANGE — Extend the existing trace.record("PERCEPTION_DETAIL", ...) call (do not add a second one)
with these kwargs:
  uia_sample = [e.text for e in uia_elems if e.text][:10]
  ocr_sample = [e.text for e in ocr_elems if e.text][:10]
  fused_text_sample = (sm.full_text or "")[:400]
Each list element is short text; cap each list at 10 entries and each string is naturally short. Do
not include cv elements' text (they are textless layout anchors). Keep the existing count kwargs.

If run_ladder later re-fuses (content re-OCR pass, G1C escalation, or the Task-8 VISION fallback)
and a trace is present, it is fine to leave the original PERCEPTION_DETAIL as the canonical
per-adapter snapshot — those later stages already record their own deltas (vision_fallback etc.).
```

**Verify:** Run a turn on any app and confirm the last trace's `PERCEPTION_DETAIL` stage now
includes `uia_sample`, `ocr_sample`, and `fused_text_sample` with readable content. Then run on an
app that sees little and confirm the samples make the failure obvious (e.g. `uia_sample` shows only
menu labels, `ocr_sample` is empty).

---

## Task 10: `--full` replay view in trace_view (Fix L4)

**Goal:** There is no single view that shows the literal "request → path → prompt → answer" the
user asked for. `tools/trace_view.py` prints stage summaries but truncates and omits the prompt and
answer bodies. This task adds a `--full <turn_id>` (and `--full-last`) mode that prints the complete
replay of one turn.
**Files:** `tools/trace_view.py`.
**Depends on:** Tasks 1, 9 (the fields it prints must exist in traces first).

```prompt
Read @jarvis/docs/diagnosis.md §4 (Problem 4), gap D, and fix L4. Read the existing
tools/trace_view.py to reuse its _read_traces / _find_turn / _stage / _all_stages helpers and its
output style.

Context: tools/trace_view.py reads ~/.jarvis/traces.jsonl and pretty-prints stages per turn, but
truncates and never prints PROMPT.prompt_text, PROMPT.system_prompt, ANSWER.answer_text, or the
new PERCEPTION_DETAIL samples. Add a verbose replay mode.

CHANGE 1 — Add CLI flags. Add `--full ID` (print the full replay of a specific turn_id) and
`--full-last` (full replay of the most recent turn). When either is set, use the new _print_full()
renderer instead of the compact _print_turn().

CHANGE 2 — Write _print_full(trace). Print, in this order, with clear section headers and NO
truncation of the bodies (these are meant to be read in full):
  1. turn_id and wall-clock-ish ordering (wake_ts).
  2. REQUEST: INPUT.query, and the target (process | app_class | title).
  3. PATH: each stage in chronological order with its key fields:
       - CLASSIFY: act / perception_mode / intent / used_cache
       - PERCEPTION: rung / source / chars / element_count / ok / stale / used_cache
       - PERCEPTION_DETAIL: counts AND uia_sample / ocr_sample / fused_text_sample (full),
         plus vision_fallback / vision_refs when present
       - FOCUS: source / confidence / resolved_text / ambiguous
       - TOOLS (each): name(args) → result_summary, budget_remaining
  4. SYSTEM PROMPT: PROMPT.system_prompt in full.
  5. PROMPT SENT: PROMPT.prompt_text in full (this is the literal text the model received), plus
     image_attached / history_turns / screen_block_chars.
  6. ANSWER: ANSWER.answer_text in full, plus answer_source / escalated / escalated_rung.
  7. OUTCOME: answer_source / latency_ms.
Use simple delimiter lines so the prompt and answer bodies are easy to copy out. Guard every field
access with .get(...) so a missing field prints "(absent)" rather than crashing — older traces from
before this wave will lack the new fields.

CHANGE 3 — Keep the existing --last / --turn compact behaviour unchanged.
```

**Verify:** Run `python tools/trace_view.py --full-last`. Confirm it prints the request, the full
ordered path (including PERCEPTION_DETAIL samples), the full system prompt, the full literal prompt
sent to the model, and the full answer text — i.e. the complete "request → path → answer" for one
turn — without truncation and without crashing on an older trace line.

---

## Done criteria for Wave 9 (local-first)

The bar is: **every criterion holds with Gemini completely absent** (no API key). A Gemini key, when
present, only makes the hardest turns better — it is never required.

- **Models (Task 0):** `ollama list` shows `qwen2.5vl:7b` and `qwen2.5:7b-instruct`; config names them.
- **Problem 4 (observability):** `python tools/trace_view.py --full-last` shows request → full path
  → exact prompt → exact answer for any turn. (Tasks 1, 9, 10.)
- **Problem 2 (knowledge):** with no Gemini key, screen-grounded reasoning questions are answered by
  the strong local model (`answer_source: local_answer`) using the knowledge-framed prompt; the
  screen block is always framed as context; "limited to the screen" refusals no longer appear.
  (Tasks 2, 3.)
- **Problem 1 (seeing):** visual queries are grounded in a strong VLM look at the pixels — locally
  via a qwen2.5vl description block, or via an attached screenshot when a Gemini key is present;
  elements carry color/shape; hard apps trigger a one-shot VISION fallback. (Tasks 4, 7, 8.)
- **Problem 3 (bleed):** both halves of every exchange carry the correct window_sig and are ordered
  deterministically, so cross-window history is demoted and prior context stops leaking. (Tasks 5, 6.)

Land Phase A (Tasks 0–5) first and re-test all four symptoms **with no Gemini key** before starting
Phase B.
