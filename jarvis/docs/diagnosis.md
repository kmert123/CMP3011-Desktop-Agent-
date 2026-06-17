# Jarvis Diagnosis 5 — The Four Core Problems, Root-Caused, with an Architecture to Fix Them

Audit date: 2026-05-31
Author: investigation pass over the full repo (no code changed)
Scope: The user's four problems, stated verbatim below, traced to specific
lines, with a concrete plan. This is the "big revelation" pass — it does not
re-tune knobs (DIAGNOSIS3/4 already did that). It identifies the *structural*
reasons the symptoms persist and proposes the architecture that removes them.

> This file replaces an earlier general pipeline-map draft (2026-05-30). The
> canonical numbered series (`DIAGNOSIS.md` … `diagnosis4.md`) remains the
> history; this document is the current, problem-focused diagnosis the user
> asked for.

> The four problems, in the user's words:
> 1. **Ultimate seeing.** Jarvis must perfectly understand everything on screen —
>    UI elements, colors, shapes, text. Any architecture that delivers that is fine.
> 2. **Use its own knowledge.** Jarvis only parrots what it literally sees. It
>    should bring knowledge *about* what it sees from its own model, not just read
>    the screen back.
> 3. **Context bleed.** Jarvis mixes context from previous requests into the
>    current one and "blurts out a mess of text."
> 4. **Observability.** To analyze behaviour we need: the request Jarvis got →
>    the path Jarvis followed → the answer Jarvis gave, all logged together.

---

## 0. Executive summary — the single root insight

Every one of the four problems has the **same shape**: the codebase already
contains the *good* machinery, but a gate, an ordering bug, or a missing wire
routes the common case around it to a *crippled* path. We are not missing
capabilities. We are losing them to plumbing.

| # | Problem | The good thing that exists | What routes around it |
|---|---------|----------------------------|------------------------|
| 1 | Seeing | The fusion pipeline (UIA+OCR+CV → real `ScreenModel`) and a `VISION` rung | Default answers go to local moondream/text-only; pixels, color, and shape are never extracted; the strong VLM fires only if the model *asks* |
| 2 | Knowledge | A capable LLM (Gemini 2.5 Flash) and a knowledge-granting system prompt | Three layers each independently bias the answer back to "screen only": local-LLM fast-path, screen-block labels, and a self-contradicting honesty rule |
| 3 | Context bleed | `to_prompt_block()` with cross-window gating and a content-floor demotion | The assistant turn is stored with **no window_sig** and the user turn is stored **after streaming**, so the gating it relies on never sees correct data |
| 4 | Observability | `trace.py` (`TurnTrace`) and `tools/trace_view.py` already exist | The trace captures *stage counts* but not the **actual prompt sent** or the **actual answer text**, so you still can't see *why* an answer came out wrong |

The rest of this document is the per-problem root cause and the fix.

---

## 1. Problem 1 — "Ultimate seeing and understanding"

### 1.1 What the user is actually asking for

Three distinct capabilities, only one of which Jarvis attempts today:

- **(a) Text** — what words are on screen. *Partially works* (UIA + OCR fusion).
- **(b) Structure / UI elements** — buttons, fields, layout, which element is
  which. *Partially works* (UIA roles + CV anchors), but **degrades to text-only**
  in the prompt.
- **(c) Pixels — color, shape, icons, charts, images.** **Does not work at all.**
  Nothing in the perception stack extracts color or shape. `cv_adapter` finds
  contour *regions* (layout anchors) but discards their appearance. The only path
  to "what color / what does this icon look like" is the VLM, and the VLM is (i)
  a 2B local model by default and (ii) only invoked reactively.

So "perfectly understand colors shapes texts" is, today, **texts only, sometimes**.

### 1.2 Root cause A — the default answer path is *text-only*, even for visual questions

Trace the common query "what am I looking at" / "describe this":

1. `classify.py` maps visual phrases to `Perception.PIXELS` → `entry_rung = VISION`.
   Good so far.
2. But the **vision rung itself** mostly produces text. `read_vision()`
   ([perception.py:189](perception.py#L189)) calls `ask_vlm(...)` and, with
   `VISION_MODEL="auto"`, that's **moondream first** — a 2B model whose element
   JSON is frequently empty and whose descriptions are weak. The screenshot is
   stored on `PerceptionResult.image`, but…
3. In `_build_initial_contents()` ([gemini.py:654](gemini.py#L654)) the image is
   only forwarded to Gemini when `is_visual or low_conf`, and **only when
   `VISION_BACKEND != "local"`**. The repo ships `VISION_BACKEND = "local"`
   ([config.py:88](config.py#L88)). So in the shipped config, for a visual query,
   the pixels are routed to **moondream's text description** and **Gemini never
   receives the image at all**. Gemini — the strong model — answers "what color is
   this" from a moondream paragraph, blind.

**This is the core seeing bug for color/shape/icons:** the capable model is
deliberately kept text-only; the pixels go to the weak model.

### 1.3 Root cause B — color and shape are never extracted by anyone

Even when the VLM does run, the *structured* `ScreenModel` (the thing the prompt
is built from for non-visual queries) has **no color, no shape, no visual
attributes** on `ScreenElement`. Look at the dataclass
([screen_model.py:18](screen_model.py#L18)): `role, text, bbox, source,
confidence, invokable`. There is nowhere to put "this is a red button" or "this is
a green checkmark icon." `cv_adapter` had the pixels in hand and threw the
appearance away. So any question about appearance has *no grounding data* unless a
full screenshot reaches a capable VLM — which §1.2 shows it usually doesn't.

### 1.4 Root cause C — VISION is reactive, not a first-class rung for hard apps

DIAGNOSIS4 G1E ("vision fallback that actually fires") was *listed* but, per the
current code, the auto-escalation added in G1C only escalates **one rung** and
only runs OCR for the next step in the common UIA→OCR case
([perception.py:355](perception.py#L355)). For a dark-mode Electron app where
UIA is thin *and* OCR is thin, the ladder stops at OCR — it does **not** push on
to VISION with the screenshot, because the thin-read escalation's VISION branch
only triggers when `entry` was already OCR. So the hardest apps (the ones the user
explicitly complained "couldn't see anything") still bottom out without ever
sending pixels to a strong model.

### 1.5 The architecture that delivers "ultimate seeing"

The fix is not another OCR knob. It is to make **pixels a first-class, always-
available evidence source feeding a strong VLM**, and to enrich the structured
model with appearance. Three moves, in priority order:

**S1 (highest leverage) — Make Gemini the vision backend and always give it the
screenshot when the question is visual.**
- Set `VISION_BACKEND = "gemini"` and `VISION_MODEL = "gemini"` for the answer
  path (keep moondream only as an offline fallback). Gemini 2.5 Flash is natively
  multimodal and *good* at color/shape/layout — this single change is most of
  "ultimate seeing."
- In `_build_initial_contents`, attach the screenshot to the Gemini call whenever
  `perception.image is not None` **and** the intent is VISUAL (or perception is
  thin), regardless of `VISION_BACKEND`. Stop substituting a moondream paragraph
  for the pixels when the strong model could just look.
- Files: [config.py](config.py), [gemini.py:654](gemini.py#L654).

**S2 — Add appearance to the structured model.**
- Extend `ScreenElement` with optional `dominant_color: tuple|None`,
  `shape_hint: str` ("rect"/"circle"/"icon"), sampled by `cv_adapter` from the
  element crop (mean color in the bbox; aspect-ratio + contour for shape). Cheap,
  deterministic, and gives the prompt grounding for "the red button" without a VLM
  round-trip.
- Render these in `to_prompt_block()` so the model sees `[Button 'Submit' red @ …]`.
- Files: [screen_model.py](screen_model.py), [adapters/cv_adapter.py](adapters/cv_adapter.py),
  [fusion.py](fusion.py).

**S3 — Promote VISION to a guaranteed fallback on thin reads.**
- When fused `full_text` and element count are both below the thin floor **after**
  the one-step escalation, and the app_class is in the "hard" set
  (chromium_electron, game_fullscreen, unknown), capture once and send the
  screenshot to the VLM unconditionally — the G1E that was specced but never wired
  end-to-end. Cap to one call; tag `source="vision"` so calibration applies.
- Files: [perception.py:355](perception.py#L355), [router.py](router.py).

> Do **S1 first**. It is a config change plus a ~10-line gate edit and converts
> "weak text guess about pixels" into "the strong multimodal model looks at the
> actual screen." S2 and S3 are the durable structural upgrades.

---

## 2. Problem 2 — "Jarvis won't use its own knowledge, only reads the screen back"

This is not a prompt-wording problem (DIAGNOSIS4 already rewrote the prompt). It
is a **routing + labelling** problem: three independent mechanisms each pull the
answer back toward "screen only," and they stack.

### 2.1 Root cause A — the local-LLM fast-path answers structure queries with a weaker model and a narrower prompt

`ask_stream` has a **high-confidence STRUCTURE local fast-path**
([gemini.py:716](gemini.py#L716)): when the screen text is confident and the
query is *not* detected as a "knowledge query," it answers from the **local
mistral-nemo** via `_build_local_prompt` and **returns before Gemini is ever
called**. `PREFER_LOCAL_STRUCTURE = True` ships on
([config.py:163](config.py#L163)).

Two failure modes:
- The "is this a knowledge query" gate is a tiny keyword set
  (`_KNOWLEDGE_CUES`, [gemini.py:42](gemini.py#L42)). "What does this setting do?"
  contains "what does" → treated as knowledge (good). But "is this safe?",
  "should I click this?", "what's wrong here?" do **not** all match, so they go to
  the local model, which — given only screen text — does exactly what the user
  complains about: reads the screen back.
- Even when it *is* a knowledge query, the local 12B model is far weaker than
  Gemini at synthesizing knowledge on top of context.

### 2.2 Root cause B — the screen-block label tells the model the screen *is* the answer

In `_build_initial_contents`, when `intent == TEXT` the screen block is labelled
literally **`"Screen content"`** ([gemini.py:639](gemini.py#L639)), with no
instruction to combine it with knowledge. Only the non-grounded branch gets the
helpful label `"Context (current screen — combine with your own knowledge to
answer)"`. So for the *exact* class of query where the user wants knowledge added
("explain this error", "what is this code doing") — which classify routes to
`Perception.STRUCTURE` → `Intent.TEXT` — the prompt frames the screen as the whole
answer. The label is fighting the system prompt.

### 2.3 Root cause C — the honesty rule is self-contradicting and the model resolves it conservatively

The system prompt ([gemini.py:55](gemini.py#L55)) says, in the same breath,
"use your own general knowledge … you are not limited to what is visible" **and**
"if the user asks you to locate/read/describe something specific that was NOT
captured … say 'I can't see that on screen right now.'" A capable model facing a
contradiction defaults to the *safe* branch — the refusal — which is precisely the
"my responses are limited to what I can see" string the user reported. The grant
and the guard are not scoped tightly enough for the model to tell them apart.

### 2.4 The fix — make Gemini the default answerer and scope the guard to grounding-only

**K1 — Demote the local fast-path for anything answer-shaped.**
- Set `PREFER_LOCAL_STRUCTURE = False` (or restrict it to a tiny set of pure
  read-back intents like "summarize this"). Route reasoning/explanation/judgement
  queries to Gemini every time. The local model becomes a *fallback when Gemini is
  unreachable*, not the primary brain. Files: [config.py:163](config.py#L163),
  [gemini.py:716](gemini.py#L716).

**K2 — One honest label, always.**
- Drop the `"Screen content"` framing. Always label the block:
  `"Screen context (use together with your own knowledge to answer):"`. The screen
  is *context*, never *the answer*, regardless of intent. Files:
  [gemini.py:629-652](gemini.py#L629), and the mirror in `_build_local_prompt`.

**K3 — Split the prompt's two jobs cleanly.**
- Rewrite the system prompt so the grant is unconditional and the guard is scoped
  *only* to factual claims about current on-screen state: "Answer every question
  using your own knowledge. The screen text is context. The *only* thing you must
  not do is assert that something specific is currently on screen when it is not in
  the provided text — in that one case say you can't confirm it, then answer the
  question from your knowledge anyway." Remove the standalone "I can't see that"
  sentence that the model over-applies. Files: [gemini.py:55](gemini.py#L55).

These three are small and directly attack the three independent pull-backs.

---

## 3. Problem 3 — "Blends previous context and blurts a mess"

The history machinery is *designed* correctly (cross-window annotation, content-
floor demotion). It fails because the **data it gates on is recorded wrong**.

### 3.1 Root cause A — the assistant turn is stored with no window_sig

`to_prompt_block()` decides whether a past turn is "[different window]" by
comparing each turn's stored `window_sig` to the current one
([session_context.py:316](session_context.py#L316)). But the **assistant** turn
is recorded by the actor as `add_turn("assistant", event.full_text)` with **no
window_sig** ([core/session_actor.py:292](core/session_actor.py#L292)) — it
defaults to `""`. And the cross-window check explicitly treats empty sig as "not
cross-window" (`sig and sig != current`). So **every assistant answer is always
treated as same-window**, even when it was produced on a different app. Half of
every Q&A pair escapes the gating. That is the bleed.

### 3.2 Root cause B — the user turn is recorded *after* the answer streams

In `_answer_worker`, `session_ctx.add_turn("user", question, …)` runs at the very
end, **after** `_stream_answer` returns ([main.py:337](main.py#L337)). Meanwhile
the assistant turn for the *same* exchange is appended by the actor on `AnswerDone`
([core/session_actor.py:292](core/session_actor.py#L292)). Depending on thread
timing the order can interleave, and on the *next* turn the "last N" slice
(`MODEL_HISTORY_TURNS=6`) can include a user question whose paired answer sits in a
different relative position than expected — the model sees mis-paired Q/A
fragments. Combined with §3.1 (answers never demoted across windows), prior context
leaks into an unrelated new question.

### 3.3 Root cause C — content-floor demotion only drops history, never signals the stale screen

The P12/F3 demotion ([session_context.py:298](session_context.py#L298)) sets
`n=0` (no history) when perception is thin — good — but the *current* perception
block is still injected by the caller, thin or not. When perception is thin, the
model gets *no* history but a near-empty screen block and the raw question, and
with the §2 pull-backs it tends to pad — "blurt." The demotion solves the wrong
half: it should also signal "answer from knowledge; screen read was weak this turn."

### 3.4 The fix

**C1 — Tag the assistant turn with the same window_sig as its user turn.**
- Thread the turn's `window_sig` into `AnswerDone` (or look up the active target in
  the actor) so `add_turn("assistant", …, window_sig=sig)` matches its pair. Now
  cross-window gating sees *both* halves. Files:
  [core/session_actor.py:292](core/session_actor.py#L292), [core/events.py](core/events.py).

**C2 — Record the user turn before streaming, atomically pair it with the answer.**
- Move `add_turn("user", …)` to *before* `_stream_answer`, or better: record the
  pair together on `AnswerDone` so ordering is deterministic and a cancelled turn
  records neither. Files: [main.py:337](main.py#L337), [core/session_actor.py](core/session_actor.py).

**C3 — When perception is thin, say so in the prompt instead of going silent on
history.** Reuse the thin signal (now available from S1/G1C) to add one line:
"(Screen read was sparse this turn — answer from your own knowledge; don't
describe an empty screen.)" Files: [session_context.py:298](session_context.py#L298),
[gemini.py](gemini.py).

> C1 is the highest-leverage single fix for the bleed: it makes the gating the
> codebase already wrote actually function.

---

## 4. Problem 4 — "Log request → path → answer so we can analyze"

`TurnTrace` ([trace.py](trace.py)) and `tools/trace_view.py` already exist and
already record stage timings (INPUT, CLASSIFY, PERCEPTION, PERCEPTION_DETAIL,
FOCUS, PROMPT, TOOLS, ANSWER, OUTCOME). That is the *skeleton* of what the user
wants. It is missing the two fields that make a trace actually *explain* a bad
answer.

### 4.1 Gap A — the trace records prompt *metadata*, not the prompt

The PROMPT stage records `screen_block_chars`, `history_turns`, `image_attached`
([gemini.py:766](gemini.py#L766)) — counts, not content. When Jarvis gives a bad
answer, you cannot see *what text it was actually given*. To debug Problems 1–3 you
need the literal screen block, the literal history block, and the literal final
user line that went to the model. Counts can't tell you "the screen block said the
wrong thing" or "stale history leaked in."

### 4.2 Gap B — the trace records answer *length*, not the answer

ANSWER records `chars=len(full_text)` ([main.py:410](main.py#L410)) — not the
text. The whole point of "the answer Jarvis gave" (problem 4, verbatim) is to read
it back alongside the path. Telemetry stores neither the prompt nor the answer
text either.

### 4.3 Gap C — perception evidence per adapter is counted but not sampled

PERCEPTION_DETAIL records `uia_count/ocr_count/cv_count/fused_count`
([perception.py:291](perception.py#L291)) — but not *which* elements or a text
sample. When "Jarvis sees nothing," you want the trace to show "UIA returned 2
elements: ['File', 'Edit']; OCR returned 0" so you can see *where* seeing broke.

### 4.4 Gap D — the trace isn't joined to the answer for analysis

The trace writes to `traces.jsonl`; telemetry writes to `telemetry.jsonl`; they
share `turn_id` (good) but there's no view that shows **request → full path → full
prompt → full answer** on one screen. `trace_view.py` prints stage summaries but
truncates and omits prompt/answer bodies.

### 4.5 The fix — make the trace a faithful replay, not a summary

**L1 — Capture the real prompt.** In the PROMPT stage, store the actual assembled
text parts (screen block, history block, focus block, final user line), not just
their lengths. Truncate generously (e.g. 4 KB) but keep the *content*. Files:
[gemini.py:766](gemini.py#L766).

**L2 — Capture the real answer.** In the ANSWER stage, store `answer_text`
(the streamed `full_text`), capped to a few KB. Files: [main.py:410](main.py#L410).

**L3 — Sample perception evidence.** In PERCEPTION_DETAIL, add the first ~10
element texts per adapter (`uia_sample`, `ocr_sample`) so "saw nothing" is
diagnosable from the trace alone. Files: [perception.py:291](perception.py#L291).

**L4 — One analysis view.** Extend `tools/trace_view.py` with a `--full <turn_id>`
mode that prints, in order: the request, every stage with its evidence, the *exact
prompt sent*, and the *exact answer returned* — the literal "request → path →
answer" the user asked for. Files: [tools/trace_view.py](../../tools/trace_view.py).

> With L1+L2 alone, the next time Jarvis "reads the screen back" or "blurts a
> mess," you open the trace and *see the prompt that caused it* — which is the only
> way to confirm whether the §2 and §3 fixes worked.

---

## 5. Cross-cutting architecture note — why these four are really one problem

All four reduce to: **the strong, capable path exists but the common case is
routed to a weaker one, and we couldn't see it happening.**

- Seeing: pixels → weak local VLM instead of strong multimodal Gemini.
- Knowledge: answer → weak local LLM / screen-only framing instead of Gemini-with-
  knowledge.
- Bleed: history gating → fed mislabelled turn data instead of correct sigs.
- Observability: trace → records counts instead of content.

The unifying fix is a **single deliberate routing principle**: *the strong
multimodal model is the default answerer; it always receives (a) the best
structured perception, (b) the screenshot when appearance matters, and (c) screen
content framed as context, never as the answer; and every turn writes a faithful,
replayable trace.* The local models become fallbacks, not defaults.

---

## 6. Recommended execution order

Smallest, highest-leverage first. Each is independently shippable.

| Step | Problem | Change | Effort | Why this order |
|------|---------|--------|--------|----------------|
| **1** | 4 | L1+L2: trace the real prompt + real answer | S | You cannot verify any other fix without this. Do it first. |
| **2** | 2 | K1+K2+K3: Gemini-default, one honest label, scoped guard | S | Directly kills "reads screen back / refuses knowledge." |
| **3** | 1 | S1: `VISION_BACKEND/MODEL=gemini`; always attach screenshot on visual intent | S | Converts weak pixel guesses into strong multimodal seeing. |
| **4** | 3 | C1+C2: correct window_sig on assistant turn; order user turn before stream | S | Makes the existing history gating actually function. |
| **5** | 1 | S2: color/shape on `ScreenElement` + render in prompt | M | Durable structured-appearance grounding. |
| **6** | 1 | S3: guaranteed VISION fallback on thin reads (finish G1E) | M | Closes the "hard app sees nothing" residue. |
| **7** | 4 | L3+L4: per-adapter evidence sampling + `--full` replay view | M | Completes the analysis loop. |

Steps 1–4 are all size-S and together address all four reported symptoms. Steps
5–7 are the structural hardening that makes the wins durable.

---

## 7. What this document deliberately does NOT recommend

- **No new OCR/UIA tuning.** DIAGNOSIS3/4 exhausted that; the residue is structural.
- **No new perception rung or adapter rewrite** before S1 — proving that "Gemini
  with the screenshot" answers the seeing questions is the cheap experiment that
  tells us whether S2/S3 are even needed at their full scope.
- **No model-asks-for-tools reliance** for the basics — perception should arrive
  *good by default*, with `need_image`/`find_element` as refinements, not as the
  only way to get pixels or color.
