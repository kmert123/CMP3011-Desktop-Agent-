# Jarvis Diagnosis 3 — Field Report: Perception Blindness, Context Pollution, Model Quality

Audit date: 2026-05-31
Scope: Three field reports after P8–P12 (DIAGNOSIS2.md) landed.
This document replaces the earlier DIAGNOSIS3 draft which over-indexed on a
specific code/sidebar case. The actual problems are broader and structural.

---

## 0. The three field reports (true intent)

1. **Jarvis cannot see what the user is talking about.** Most of the time,
   asking about something on screen produces an answer that makes it clear
   Jarvis never actually read the relevant content. This is not a sidebar-vs-
   code-editor problem — it is a general perception blindness. Whatever the
   user is looking at, Jarvis does not reliably extract it.

2. **Previous responses or screen state bleeds into current answers.** When
   Jarvis is confused or uncertain, it reaches back into prior turns or prior
   screen captures and emits plausible-sounding but wrong answers. It "just
   says bullshit." The user cannot tell if this is cache, history, or the
   model hallucinating.

3. **Local LLM (llama3.2:3b) and possibly the vision model are too weak.**
   With Gemini exhausted the fallback model lacks the general knowledge and
   reasoning quality to be useful. The user wants a better *general* model,
   not a specialised code model.

---

## 1. Problem 1 — Perception blindness: Jarvis cannot see what is on screen

This is the core failure. Before diagnosing it, the full perception pipeline
must be understood as it actually runs.

### 1.1 How perception reaches the model (actual data flow)

```
capture_target()              → raw BGR crop of the foreground window
  └─ uia_adapter.read_uia()  → ScreenElement list (depth ≤ 6, nodes ≤ 150)
  └─ ocr_adapter.read_ocr()  → ScreenElement list (Tesseract, PSM=6, scale=2.5)
  └─ cv_adapter.read_cv()    → ScreenElement list (layout anchors only)
fusion.fuse()
  └─ _build_uia_tree()       → containment tree (parent = smallest ancestor)
  └─ _attach_non_uia()       → OCR/CV placed into UIA nodes
  └─ _dedup_cross_source()   → duplicates merged by overlap+text-sim+role
  └─ resolve_content_region()→ content bbox tagged (P8)
  └─ full_text assembled     → reading-order join of text-bearing elements
ScreenModel.to_prompt_block() → rendered text (≤ 1600 chars / ~400 tokens)
gemini.py _build_initial_contents() → Part list sent to model
```

This pipeline has several points where content is silently lost or corrupted.

### 1.2 Root cause A — The UIA tree is structurally blind for most modern apps

`_walk_uia()` in perception.py uses pywinauto and walks depth ≤ 6 with a
node cap of 150. For native Win32 apps this is often enough. For the apps
people actually use — browsers, Electron apps (VS Code, Discord, Slack,
Notion), UWP apps — the UIA tree is nearly useless:

- **Electron**: The accessibility tree exposes chrome/shell structure but
  renders page content as a single opaque blob or not at all. `UIA_MAX_DEPTH=6`
  almost never reaches actual content nodes in Electron.
- **Chromium browsers**: Content DOM nodes are exposed only when Chrome's
  accessibility mode is explicitly enabled (via a command-line flag). Without
  it, the UIA tree gives tabs, address bar, toolbar — no page text.
- **Node cap of 150**: A modern app's UIA tree can have thousands of nodes.
  The cap is hit immediately on any complex window; the walker stops before
  reaching content nodes that are deep in the tree.

The result: for most apps people use daily, the UIA rung produces metadata
(window title, toolbar labels, tab names) rather than the content the user
is looking at. The fusion step then builds a model from this sparse skeleton.

### 1.3 Root cause B — OCR is the primary content source but it runs blind

OCR is the fallback that should compensate for UIA blindness. But:

- **PSM=6 (uniform block) is wrong for most screens.** PSM=6 assumes a single
  uniform column of text. Most UIs are multi-column, mixed, or sparse. The
  correct PSM for a general desktop screenshot is PSM=11 (sparse text) or
  PSM=3 (automatic), not PSM=6.
- **Scale=2.5 is borderline for small text.** At 100% DPI a 12px font renders
  as 30px after 2.5× — marginal for Tesseract. At 150% DPI (most modern
  monitors) the native font is already 18px, which is fine — but at 100% DPI
  small text in content areas is frequently missed.
- **CLAHE + inversion preprocessing does not handle all cases.** Gradient
  backgrounds, semi-transparent overlays, and anti-aliased fonts on coloured
  backgrounds all degrade OCR quality. The pipeline has no per-region
  adaptation.
- **`OCR_MIN_CONF=0.4` silently drops lines.** A confidence floor of 0.4 means
  any text line where Tesseract is less than 40% confident is dropped without
  any fallback. In dark-mode apps, small-font UIs, or apps with custom
  rendering, a large fraction of lines fall below this threshold. The content
  disappears silently — Jarvis does not know it missed anything.

### 1.4 Root cause C — Content-region detection (P8) has a silent failure mode

`resolve_content_region()` in content_region.py:
- Has an early-return for non-browser app classes (`native_win32`, etc.) —
  passes all elements through as content.
- For browser/Electron, uses `CHROME_TOP_BAND_PX=124` and
  `CHROME_SIDE_PANEL_MAX_FRAC=0.15`. These are geometry-only constants.
- If content detection gets the region wrong (too small, offset, or wrong for
  a given DPI/zoom level), the content-region re-OCR pass (P9) crops the
  *wrong rectangle* and produces OCR output from the wrong area. This is a
  silent failure — the system proceeds as if everything is fine.

### 1.5 Root cause D — `to_prompt_block()` silently truncates to ~400 tokens

`ScreenModel.to_prompt_block()` renders the containment tree into a string
capped at `_MAX_CHARS = 1600` characters (set in session_context.py). The
`to_prompt_block()` method in screen_model.py itself also applies its own
budget. The budget is applied *after* the content is rendered — meaning the
model receives only the first ~400 tokens of what the screen contains. If
the relevant content is not in the first 400 tokens of the rendered tree, it
is truncated and never reaches the model. The model does not know it is
receiving a truncated view.

The rendering order is: content-region elements (tree order) → chrome elements
(capped at 15% of budget). "Tree order" within the content region is
containment-tree order, not visual reading order or relevance order. The
element the user cares about may be last in tree order and first to be cut.

### 1.6 Root cause E — The model believes it has complete information

The system prompt says:

> "The provided window title and on-screen text IS the current screen state —
> reason over it directly. Never say you cannot see the screen."

This instruction was written to prevent the model from refusing to answer when
it has good perception data. But when perception is poor or truncated, the
instruction prevents the model from signalling uncertainty. The model receives
incomplete screen data and is told to treat it as complete — so it reasons
confidently from a partial view and gets things wrong.

### 1.7 Fix plan

| ID  | Description | File(s) | Effort |
|-----|-------------|---------|--------|
| F1A | Change `OCR_PSM` default from 6 to 11 (sparse text). For the full-window pass PSM=11 is the correct general choice. The content-region re-OCR already uses PSM=11; this aligns the full pass with reality. | `config.py` | XS |
| F1B | Increase `OCR_SCALE` from 2.5 to 3.5 for the full-window pass (matching the re-OCR scale). Higher upscale improves recall on all text sizes; the cost is acceptable for a single-screen crop. | `config.py` | XS |
| F1C | Reduce `OCR_MIN_CONF` from 0.4 to 0.25 (matching `OCR_MIN_CONF_CONTENT`). There is no reason to use a stricter threshold for the full-window pass than the re-OCR pass — they are reading the same screen. | `config.py` | XS |
| F1D | Raise `UIA_MAX_NODES` from 150 to 400 and `UIA_MAX_DEPTH` from 6 to 8. The current limits are too conservative and cause the walker to stop before reaching content nodes in complex apps. This costs a few hundred milliseconds at most. | `config.py` | XS |
| F1E | Add a honesty condition to the system prompt: when `screen_model.full_text` is short (below the content floor), prepend a note to the Part list: "Note: the screen content captured was limited. If you cannot answer from what is provided, say so clearly rather than guessing." This gives the model permission to express uncertainty. | `gemini.py` | XS |
| F1F | In `to_prompt_block()`, sort content-region elements by `calibrated_confidence` descending before rendering, so the most reliably-extracted content reaches the model first and is least likely to be truncated. | `screen_model.py` | S |

---

## 2. Problem 2 — Context pollution: prior responses or screen state bleeds in

### 2.1 Observed behaviour

When perception is thin, Jarvis produces answers that recycle prior responses
or prior screen content. The output is confidently wrong.

### 2.2 Root cause A — P12 history demotion floor almost never fires

`session_context.to_prompt_block()` demotes history to 1 turn only when
`len(content_elems) < 3 OR len(content_text) < 40`. Even a nearly empty
screen passes this test: sidebar labels, window title, status bar, and menu
items together produce well over 3 elements and 40 characters. The demotion
intended to protect against context bleed virtually never activates.

### 2.3 Root cause B — The cache serves stale screen content silently

`screen_read_fresh()` returns True when TTL has not expired AND Hamming
distance is within tolerance. For native apps, TTL is 8 seconds and Hamming
is ≤ 10. When the user switches context (opens a new file, switches to a
different app, scrolls to a different section), the screen content changes but
the hash may still be within tolerance — especially for apps with fixed chrome
and changing only the central content area. The model receives last-session's
content and does not know the cache was used.

The cache text lives in `session_context.last_screen_read["text"]` — a raw
string from the last successful read. This text is NOT the same as the current
ScreenModel — it is the previous read's raw OCR string. When the cache hits,
the router uses this old string to build the perception context. No
invalidation signal reaches the model.

### 2.4 Root cause C — History carries screen-specific answers into different contexts

`session_ctx.add_turn()` stores user question + Jarvis answer. When the user
asks about document A, then switches to document B and asks a different
question, the history block still contains the answer about document A. If
perception of document B is weak, the model anchors on the document-A answer
in history and produces a blend.

The `HISTORY_CROSS_WINDOW` strategy (`annotate` or `drop`) only triggers when
the window *signature* changes (process + app_class + title). A switch between
two files in the same editor does not change the window signature — so history
of the first file's answer persists invisibly while perception of the second
file is being processed.

### 2.5 Root cause D — The system prompt instructs confidence, not calibration

The instruction "reason over it directly, never say you cannot see the screen"
actively suppresses the model's uncertainty expression. When the model is
uncertain, it produces a confident answer based on the most prominent prior
text it has — which is history. The result is the blending the user observes.

### 2.6 Fix plan

| ID  | Description | File(s) | Effort |
|-----|-------------|---------|--------|
| F2A | Raise `HISTORY_CONTENT_FLOOR_CHARS` from 40 to 200 and `HISTORY_CONTENT_FLOOR_ELEMENTS` from 3 to 5 (counting only `in_content_region=True` elements). This makes the demotion trigger whenever content is sparse rather than only when the screen is essentially blank. | `config.py` | XS |
| F2B | When the cache is used (`screen_read_fresh() == True`), pass a `cache_hit=True` flag through `RouteResult`. In the prompt builder, when `cache_hit=True`, prepend: "[Using cached screen state — content may be up to N seconds old.]" so the model knows it is not reading live pixels. | `router.py`, `session_context.py`, `gemini.py` | S |
| F2C | Replace the "never say you cannot see the screen" rule in `_SYSTEM_PROMPT` with: "If the screen content provided is insufficient to answer the question, say clearly: 'I can't see that on screen right now.' Do not guess or reuse prior answers." This is the opposite of the current instruction and correctly calibrates model behaviour when perception is poor. | `gemini.py` | XS |
| F2D | When `perception_weak` is True in `to_prompt_block()`, also clear the history completely (n=0 turns) rather than keeping 1 turn. A single prior turn from a different context is still enough to anchor a confabulation. When perception is weak, no history is safer than partial history. | `session_context.py` | XS |
| F2E | Reduce `SCREEN_READ_TTL` (native apps) from 8s to 3s. Most user interactions take less than 3 seconds; 8 seconds is long enough for the screen to change significantly. | `config.py` | XS |

---

## 3. Problem 3 — Model quality: local LLM and possibly the vision model

### 3.1 Observed behaviour

With Gemini exhausted, the fallback model (llama3.2:3b) lacks the general
reasoning and knowledge to be useful. Responses are thin, incomplete, or
wrong. The user also suspects the vision model (moondream or Gemini multimodal)
may be a weak link.

### 3.2 Root cause A — llama3.2:3b is too small for general assistant work

llama3.2:3b (3B parameters, typically Q4 quantisation) has:
- Poor instruction following on prompts longer than ~500 tokens.
- Weak world knowledge — the model is too small to store broad factual
  associations reliably.
- High confabulation rate when context is ambiguous or sparse.
- Tendency to produce short, generic answers when uncertainty is high.

It is the wrong model for a general desktop assistant.

### 3.3 Root cause B — No streaming from local LLM creates perceived unreliability

The Ollama call in `_build_local_prompt()` is blocking — it returns a full
response or times out at `LOCAL_ANSWER_TIMEOUT_MS=10000ms`. The user sees
silence for up to 10 seconds, then a response appears. This makes the local
path feel broken even when it works. A streaming path would yield tokens
progressively, making the system feel more responsive.

### 3.4 Root cause C — The vision model path rarely fires and is unverified

`read_vision()` with `ask_elements=True` runs only when the perception ladder
reaches `Rung.VISION` — after UIA and OCR have both been tried and found
insufficient, AND the escalation thresholds are met. In practice this path is
rarely triggered. When it does fire, `ask_vlm()` calls either moondream (local,
low accuracy) or Gemini multimodal (currently exhausted). There is no
verification that the vision path produces useful output for the user's queries.

### 3.5 Fix plan

| ID  | Description | File(s) | Effort |
|-----|-------------|---------|--------|
| F3A | Replace `LOCAL_LLM_MODEL` default from `"llama3.2:3b"` to `"mistral-nemo:12b"` or `"gemma3:9b"`. Both are general-purpose models with substantially stronger reasoning and instruction following at consumer hardware cost (8–12GB VRAM or CPU-offload). Document the `ollama pull` command. Update `LOCAL_ANSWER_TIMEOUT_MS` to 25000 to accommodate larger model latency. | `config.py` | XS |
| F3B | Add a local LLM availability check at startup: try a short probe call to the Ollama endpoint. Log whether the model is available and its name. If unavailable, warn clearly. Similarly, probe Gemini at startup and set `GEMINI_AVAILABLE` flag. Emit a startup warning when both are degraded. | `gemini.py` | S |
| F3C | Add streaming support for the local LLM path. Ollama's `/api/generate` supports `"stream": true`; replace the blocking request with a chunked-read generator that yields tokens through the same `ask_stream()` interface as Gemini. This makes the local path feel live instead of frozen. | `gemini.py` | M |
| F3D | Add an explicit "fallback mode" notice in the local prompt preamble when Gemini is unavailable: "You are running as a local fallback model. Answer from what is provided. If you are not sure, say so clearly — do not invent." This combats confabulation from an under-resourced model. | `gemini.py` | XS |

---

## 4. Prioritised remediation plan

### Priority 1 — Fix perception quality (CRITICAL)

**P13 — OCR and UIA tuning**

These are pure config changes. They improve recall immediately across all apps
without changing any logic.

1. `config.py`:
   - `OCR_PSM = 11` (was 6)
   - `OCR_SCALE = 3.5` (was 2.5)
   - `OCR_MIN_CONF = 0.25` (was 0.4)
   - `UIA_MAX_NODES = 400` (was 150)
   - `UIA_MAX_DEPTH = 8` (was 6)
   - `SCREEN_READ_TTL = 3` (was 8)

Rationale: PSM=11 is the correct mode for mixed UI text. Scale=3.5 and conf=0.25
align the full-window pass with the re-OCR pass that was already tuned for content.
Higher UIA limits recover content nodes that the walker was hitting before. Lower
TTL means cached stale content evicts faster.

---

### Priority 2 — Stop context pollution (CRITICAL)

**P14 — Honesty guard + history demotion + cache signal**

1. `gemini.py:_SYSTEM_PROMPT`: Replace the "never say you cannot see the screen"
   instruction with an explicit calibration rule.
2. `config.py`: `HISTORY_CONTENT_FLOOR_CHARS = 200`, `HISTORY_CONTENT_FLOOR_ELEMENTS = 5`,
   `SCREEN_READ_TTL = 3` (same as P13).
3. `session_context.py:to_prompt_block()`: When `perception_weak`, use `n=0`
   (zero history turns) instead of `n=1`.

These three changes together mean: when Jarvis cannot see the screen clearly,
it will say so rather than recycling prior content, and it will not have history
available to pollute the answer.

---

### Priority 3 — Prompt content-salience sort (HIGH)

**P15 — Sort content elements by confidence before rendering**

`screen_model.py:to_prompt_block()`: Sort content-region elements by
`calibrated_confidence` descending before rendering into the prompt block.
This ensures the highest-quality extracted content reaches the model first
and is least likely to be cut by the token budget.

---

### Priority 4 — Better local model (HIGH, easy)

**P16 — Upgrade local LLM + add fallback notice**

1. `config.py`: `LOCAL_LLM_MODEL = "mistral-nemo:12b"`, `LOCAL_ANSWER_TIMEOUT_MS = 25000`.
2. `gemini.py:_build_local_prompt()`: Add fallback-mode notice.

---

### Priority 5 — Cache hit signal to model (MEDIUM)

**P17 — Surface cache state to model**

`router.py` / `gemini.py`: When the screen read was served from cache, note
it in the prompt context so the model knows it may be reading stale content.

---

### Priority 6 — Local LLM streaming (MEDIUM, deferred)

**P18 — Stream local LLM tokens**

Replaces the blocking Ollama call with a streaming generator. Deferred because
the model quality fix (P16) is higher leverage per line of code.

---

## 5. What P13–P14 will and will not fix

**Will fix:**
- Jarvis confidently describing things not on screen (honesty guard).
- History from a prior context bleeding into new answers (demotion floor raise +
  zero-turn demotion on weak perception).
- Small/medium text missed by OCR (scale + PSM + conf threshold tuning).
- UIA walker stopping before content nodes in complex apps (node/depth limit raise).

**Will not fix:**
- Electron/browser apps where UIA fundamentally does not expose content (requires
  CDP — M1 from DIAGNOSIS2.md, still pending).
- Apps where content is entirely rendered by GPU (games, video players, some
  custom frameworks) — OCR on a pixel stream can't recover what's not rasterised
  to a stable bitmap.
- Local LLM quality ceiling — a 12B model is better than 3B but still not Gemini.
  Renewing the Gemini API key is the highest-leverage action for model quality.

---

## 6. Quick reference

| Symptom | Deepest root cause | Priority fix |
|---------|--------------------|-------------|
| Jarvis doesn't see what user is talking about | OCR PSM/scale/conf wrong; UIA caps too low | P13 |
| Reads old screen state | TTL=8s too generous; cache hit not surfaced | P13 + P17 |
| Blends prior answers when confused | Demotion floor too high; honesty rule missing | P14 |
| Local LLM gives thin/wrong answers | 3B model too small | P16 |
| Local LLM feels frozen | No streaming | P18 (deferred) |
| Content at end of screen not in prompt | No confidence-sort before truncation | P15 |
