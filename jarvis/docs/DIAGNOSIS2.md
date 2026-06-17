# Jarvis Diagnosis 2 — Architecture Review & Perception-Quality Root Causes

Audit date: 2026-05-30
Auditor: Claude Opus 4.8
Scope: Read-only code analysis prompted by three field reports after the
DIAGNOSIS.md remediation (Tasks 1–3, 7–9) landed. No code changed in this pass.

This document is a second-pass diagnosis. DIAGNOSIS.md (first pass) identified
the context-bleed and Gemini-load issues and they are largely fixed. The three
new field reports point at a different, deeper class of problem: **perception
quality and content salience**, not plumbing. This document answers the user's
explicit question — *is the current architecture right?* — and then localises
the three reported symptoms to concrete code with concrete fixes.

---

## 0. The three field reports (verbatim intent)

1. **Still gets stuck on prior context.** Cause unknown to the user — "I don't
   know if it looks at another tab or if it's a cache issue."
2. **Misses on-screen content.** On Twitter it could not read the caption of a
   post. After the user zoomed in, it could read it. It also "said it" only
   *after* the user told it what the caption was — i.e. it leaned on the
   conversation instead of the screen.
3. **On Chrome it pays attention to tabs/sidebars instead of the main content
   that is most prominently on screen.**

The unifying thread: **Jarvis perceives the screen as an undifferentiated bag
of text, with no model of where the *content* is, no semantic structure for web
pages, and a resolution/threshold regime that silently drops exactly the small
text the user cares about.**

---

## 1. Architecture verdict — is the current design right?

**Verdict: The architecture is structurally correct and should be kept. It is
missing two things, not built wrong.**

### What is right (keep it)

- **Multi-adapter fusion into one `ScreenModel`** (`fusion.py`). Merging UIA +
  OCR + CV with calibrated per-source reliability is the correct shape for a
  cross-application assistant. There is no single backend that works on every
  app; fusion is the right abstraction.
- **The perception ladder** (WINDOW → UIA → OCR → VISION, `perception.py`) with
  an `AppClass`-driven policy (`perception_policy.py`). Starting cheap and
  escalating on low confidence is correct and cost-aware.
- **Localize-then-extract** for VLM grounding (`focus.py:resolve_reference_vlm`).
  Trusting the VLM only for *which region*, never for transcription, is the
  right discipline and should not be diluted.
- **Calibrated confidence as the escalation signal** (`ADAPTER_RELIABILITY`).
  The mechanism is sound; the *thresholds* are mistuned (see §5).

A rewrite would throw away all of this and reintroduce the same problems. The
fusion-ladder design is not the cause of any of the three reports.

### What is missing (add it)

- **M1 — A semantic backend for web/Electron content.** Chrome is the 72%
  use case (per DIAGNOSIS.md telemetry) and it is perceived as flat OCR. This is
  already scoped as CDP (DIAGNOSIS.md Tasks 4–6, not yet implemented). It is the
  single highest-leverage addition and remains so. **But CDP only helps Chrome,
  and only when Chrome is launched with `--remote-debugging-port`.** It does not
  fix Twitter-in-a-browser-without-CDP, native apps, or the salience problem.
- **M2 — A content-salience model.** Nothing in the pipeline knows the
  difference between "browser chrome" (tabs, omnibox, bookmarks, sidebar) and
  "page content." Every text block competes equally for the model's attention
  and for the 400-token prompt budget. This is a *new* concept the architecture
  lacks entirely, and it is the direct cause of report #3. It must be solved
  independently of CDP because it applies to every app class.

**Conclusion:** Keep the architecture. Add M1 (already planned) and M2 (new).
Re-tune the OCR resolution/threshold regime (§5). Do not rewrite.

---

## 2. Report #1 — "still gets stuck on prior context"

DIAGNOSIS.md §2.B/C/D + Tasks 1, 3, 8 addressed the *plumbing* causes
(hwnd-only recapture, URL cache no-op, unconditional history). Those fixes are
in. The residual stickiness has **three remaining causes**, in order of
likelihood:

### 2.1 — Wake-time interaction state is never refreshed (PRIMARY suspect)

`PerceptionTarget` captures `cursor_pos`, `focused_element` (a raw COM
pointer), and `selection_text` **once, at wake time**
(`perception_target.py` `_capture_interaction_state`). On a follow-up where
recapture is skipped — or even when recapture *runs* but the user has since
moved on — these three fields describe a moment in the past.

Task 2 added `_com_pointer_is_alive()` in `focus.py:140`, which correctly
returns `None` for a *dead* pointer. But a pointer can be **alive and wrong**:
after a same-page scroll or an in-page SPA update, the old focused element may
still be a valid COM object pointing at content that is no longer what the user
means. `selection_text` is worse — it is a plain string captured at wake and
re-injected verbatim with no liveness check at all.

> **Mechanism for the Twitter "said it after I told him" symptom:** if
> `selection_text` or a stale focused-element text from a *prior* turn is still
> in `active_target`, and the current OCR is weak, the focus block (injected as
> PRIMARY context, `gemini.py` `_format_focus_block`) carries old text that
> outweighs the weak fresh perception. The model answers from the stale focus
> string, which can look like "remembering" the previous turn.

**Fix F1.** Treat wake-time interaction state as *perishable*:
- Stamp `cursor_pos` / `focused_element` / `selection_text` with `wake_ts` and
  refuse to use any of them when `monotonic() - wake_ts` exceeds a short TTL
  (e.g. `FOCUS_STATE_TTL_MS = 1500`, same window as `FOLLOWUP_RECAPTURE_MS`).
- On a typed follow-up, **re-read selection and focus live** rather than reusing
  the wake-time snapshot (a fresh `GetFocusedElement` / `GetSelection` is cheap).
- In `focus_resolver.py`, when the focused element resolves but its text is
  *not present anywhere in the current `ScreenModel`*, discard it — a focused
  element whose text no longer appears on screen is stale by definition.

### 2.2 — Cache identity still has no semantic page key

Task 3 added the omnibox fallback and fixed the `cached_url==""` asymmetry. Good.
But the omnibox read depends on the `Chrome_OmniboxView` child class existing
and being populated, and it returns the *typed/displayed* URL which may lag the
actual page during SPA navigation (Twitter, YouTube). The cache key is still
fundamentally `(process, title, maybe-url, roi_dhash, TTL)`.

For SPA sites that never change the window title or the omnibox between
"tweets" or "videos," **the only differentiator left is the 16×16 roi_dhash with
Hamming tolerance 10/256 and the 2-second browser TTL.** Two visually similar
feed states within 2s can collide.

**Fix F2 (depends on M1/CDP).** The canonical fix is CDP's
`Page.getNavigationHistory` / `Target.getTargets` URL, which is exact and
updates on SPA route changes. This is DIAGNOSIS.md Task 6. Until CDP lands,
tighten the browser TTL to ~1.0s and lower `CACHE_HAMMING_MAX` for browser
class only (a dedicated `BROWSER_CACHE_HAMMING_MAX`), accepting more cache
misses (more OCR) in exchange for never serving a stale feed state.

### 2.3 — History still wins when perception is empty

Task 8's window-continuity gate annotates/drops *cross-window* turns. It does
**not** help the within-window case: same Chrome window, user scrolls the feed,
asks about a new post. All history turns share the window signature, so none are
annotated, and when the fresh OCR is sparse (report #2), the model anchors on
its own prior answer.

**Fix F3.** When the fresh perception block is below a content floor (e.g. fewer
than N text-bearing elements or < K chars of `full_text`), **demote history**:
either drop it to the last 1 turn or prefix the whole history block with an
explicit instruction that the screen has changed and prior answers may not
apply. The signal "perception is weak" already exists (it's what drives
escalation); reuse it to gate history weight.

---

## 3. Report #2 — "misses small text (Twitter caption); saw it only after zoom"

This is the most diagnostic report in the set, because the user **proved the
fix manually**: zooming in made the text readable. That tells us unambiguously
the failure is **spatial resolution at OCR time**, not a logic bug. Walking the
OCR path:

### 3.1 — Single full-window OCR at 2.5× is too coarse for body text

`adapters/ocr_adapter.py:_preprocess` upscales the **entire window crop** by
`OCR_SCALE = 2.5` (`config.py:51`) and runs one Tesseract pass at `PSM=6`
(uniform block). On a maximised 1080p+ browser window, a tweet caption at ~14px
font, after 2.5× upscale, is ~35px — near Tesseract's reliability floor,
especially with anti-aliasing and a busy background. When the user zoomed the
*page*, the glyphs grew to where a 2.5× pass clears the floor. **The pipeline
never does what the user did manually: it never re-reads the content region at
higher resolution.**

`read_region()` (same file) *does* exist and runs at `READ_REGION_SCALE = 3.5`
— but it is only ever called as a Gemini tool (`read_region(x,y,w,h)`) or by
`focus.py:_refresh_element_text`. It is **not** part of the default perception
pass. So the high-res capability exists but is gated behind the model
explicitly asking for it, which a streaming answer rarely does in time.

**Fix F4 — content-region re-OCR pass.** After the first full-window OCR, when
the query is a content query (Intent.TEXT/ANSWER) and the main content region is
identified (see M2/§4), run a **second OCR pass on just the content region** at
`READ_REGION_SCALE` (3.5×) or higher and merge those elements in. This is
exactly the user's manual zoom, automated, and scoped to where it matters so it
stays cheap.

### 3.2 — Confidence thresholds discard small text twice

Two filters drop low-confidence tokens, and small/anti-aliased text is exactly
what scores low:

- `ocr_adapter.py:22` `_TOKEN_CONF_MIN = 30` — drops individual tokens below 30%.
- `ocr_adapter.py:97` `mean_conf < OCR_MIN_CONF (0.4)` — drops whole lines whose
  mean token confidence is below 40%.

A small caption that Tesseract reads at ~35% mean confidence is **silently
dropped before fusion ever sees it.** The element does not exist in the
`ScreenModel`, so no escalation, no salience, nothing can recover it. The model
genuinely has no token for the caption — consistent with "it could not see it."

**Fix F5.** Do not hard-drop low-confidence OCR lines; **keep them, tagged
low-confidence**, and let calibration + fusion decide. Specifically:
- Lower `OCR_MIN_CONF` to ~0.25 for the content-region re-OCR pass (F4), or
- Keep dropped lines in a secondary list that the salience pass (M2) can promote
  if they fall inside the main content region. A low-confidence line *inside the
  content area* is far more valuable than a high-confidence "Subscribe" button.

### 3.3 — PSM 6 is wrong for sparse social-media layouts

`OCR_PSM = 6` assumes a uniform block of text. A Twitter feed is sparse,
multi-column, with text interleaved with avatars and media. `PSM 11` (sparse
text) or `PSM 3` (full auto page segmentation) typically recover more on these
layouts. The config comment at `config.py:53` even notes "try 11 for sparse UI"
— but nothing ever switches it.

**Fix F6.** Make PSM policy-driven: `chromium_electron` content → PSM 11/3;
native dialog → PSM 6. Cheapest version: run the content-region pass (F4) at
PSM 11 and keep the full-window pass at PSM 6.

---

## 4. Report #3 — "attention on tabs/sidebars, not the main content"

This is the **architecture gap (M2)**. There is currently **no code anywhere
that distinguishes browser chrome from page content.** Tracing it:

- `capture_target()` (`capture.py:107`) crops to the **entire window rect** —
  title bar, tab strip, omnibox, bookmarks bar, side panel, and content, all in
  one crop.
- OCR runs over the whole crop. The tab strip and omnibox produce **short,
  high-contrast, high-confidence** strings ("New Tab", "youtube.com", bookmark
  names). Body content produces **longer, lower-confidence** strings.
- Calibration multiplies all browser OCR by the same 0.8 (`config.py:153`), so
  the high-raw-confidence chrome text ends up with the *highest*
  `calibrated_confidence` in the model.
- `to_prompt_block()` (`screen_model.py:118`) renders in reading order (top to
  bottom) with a 400-token budget. The tab strip and omnibox are **at the top**,
  so they are rendered **first** and consume budget before the content is
  reached. When the budget runs out (`screen_model.py:148`), **content is what
  gets truncated away.**
- `should_escalate()` keys on `max(calibrated_confidence)`. A high-confidence
  "youtube.com" omnibox string can keep max-confidence high enough to *suppress*
  escalation even when the actual content was perceived terribly.

So the model is, by construction, fed the chrome first and most confidently. The
user's observation is precisely correct and is a direct, predictable consequence
of having no salience model.

**Fix F7 — content-region detection (the core of M2).** Identify the primary
content rectangle and prioritise it. Three escalating implementations:

1. **Geometric heuristic (ship first, no new deps).** For `chromium_electron`,
   subtract a top band for the browser chrome (tab strip + omnibox + bookmarks ≈
   the top 110–140px at 100% DPI, scaled by monitor DPI) and any narrow left/right
   side panels (columns whose width < ~15% of window and which run full height).
   The remainder is the content region. Crop/weight to it.
2. **CV-assisted (uses existing `cv_pipeline`).** The largest central contour
   block that is not in the chrome band is the content viewport. CV already
   produces region anchors; add a "content viewport" classification.
3. **CDP-native (when M1 lands).** CDP gives the DOM; the `<main>` / largest
   text-bearing scroll container *is* the content region, exactly. This is the
   eventual correct answer and another reason M1 is high-leverage.

**Fix F8 — salience weighting in prompt + escalation.** Once a content region
exists:
- In `to_prompt_block()`, render content-region elements **first** and give
  chrome elements a separate, clearly-labelled, budget-capped section (e.g. at
  most ~15% of budget for "Browser chrome: ..."). The model should see content
  before it ever sees tabs.
- In `should_escalate()`, compute max confidence over **content-region elements
  only**, so high-confidence chrome text can no longer mask poorly-perceived
  content.
- Tag each element with an `in_content_region: bool` (or a `salience` float) on
  `ScreenElement` so both the prompt builder and the router can use it.

This is the single change that most directly fixes report #3, and it is
independent of CDP — it works for any app where chrome/content can be separated
geometrically.

---

## 5. Cross-cutting: the OCR resolution/threshold regime is mistuned

Reports #2 and #3 share a root: the OCR regime optimises for clean, large,
high-contrast text (toolbars, dialogs) and against small body content. Summary
of the knobs and their pull:

| Knob | Location | Current | Effect on small content text |
|---|---|---|---|
| `OCR_SCALE` | config.py:51 | 2.5 | Too low for ~14px body text on a full window |
| `READ_REGION_SCALE` | config.py:52 | 3.5 | Good — but never used in the default pass |
| `_TOKEN_CONF_MIN` | ocr_adapter.py:22 | 30 | Drops small/AA tokens pre-fusion |
| `OCR_MIN_CONF` | config.py:50 | 0.4 | Drops whole low-conf content lines |
| `OCR_PSM` | config.py:53 | 6 | Wrong mode for sparse social layouts |
| reliability (ocr) | config.py:153 | 0.8 flat | Same weight for chrome and content text |

The fixes in §3–§4 (content-region re-OCR at higher scale + PSM 11, softer
thresholds *inside* the content region, salience-aware reliability) re-tune this
regime without penalising the native-app dialog case that the current values
serve well.

---

## 6. Remediation plan (priority order)

Numbered to extend DIAGNOSIS.md's P1–P7. New work is P8–P12; M1 is the existing
CDP plan (Tasks 4–6) and stays the top structural item.

### P8 — Content-region detection + salience (Fixes F7, F8) — **highest new leverage**
Directly fixes report #3 and substantially helps #2. App-class-agnostic, no new
deps in its geometric form. Add `in_content_region` / `salience` to
`ScreenElement`; build a `content_region.py` resolver (geometric first, CV
second, CDP-native later); wire salience into `to_prompt_block()` and
`should_escalate()`.
**Files:** new `content_region.py`; `screen_model.py` (field + prompt order);
`fusion.py` (tag elements); `router.py` (content-only escalation conf);
`perception.py` (invoke resolver in fusion path).

### P9 — Content-region re-OCR pass (Fixes F4, F6) — fixes report #2
After the full-window pass, re-OCR the content region at `READ_REGION_SCALE`
with PSM 11 and merge. Scoped to content queries so cost stays bounded.
**Files:** `perception.py` (second pass in fusion path); `adapters/ocr_adapter.py`
(parameterise scale + PSM); `config.py` (`CONTENT_REOCR=True`, PSM-by-class).

### P10 — Soften OCR thresholds inside the content region (Fix F5)
Stop hard-dropping low-confidence content lines; keep-and-tag, let salience +
fusion arbitrate. Leave chrome/native thresholds as-is.
**Files:** `adapters/ocr_adapter.py`; `config.py`.

### P11 — Perishable interaction state (Fix F1) — fixes residual report #1
TTL-gate `cursor_pos` / `focused_element` / `selection_text`; re-read live on
follow-ups; discard focused elements whose text is absent from the current model.
**Files:** `perception_target.py` (TTL stamp); `focus_resolver.py` (liveness +
"text present in model" check); `main.py` (live re-read on follow-up).

### P12 — Weak-perception history demotion (Fix F3) + tighter browser cache (Fix F2)
When fresh perception is below a content floor, demote history weight. Tighten
browser TTL / Hamming until CDP provides the canonical URL key.
**Files:** `session_context.py` (`to_prompt_block` floor gate; browser cache
constants); `config.py`.

### (Existing) M1 / Tasks 4–6 — CDP backend
Still the top structural item for the Chrome use case. Note that **P8–P12 are
deliberately CDP-independent** so perception quality improves even for browsers
launched without `--remote-debugging-port` and for non-browser apps. When CDP
lands, P8 gains a native content-region source and P12/F2 gain the exact URL key.

---

## 7. What to do first (one paragraph)

Ship **P8 (content-region + salience)** and **P9 (content re-OCR)** together —
they are the two changes that map one-to-one onto reports #3 and #2, require no
new dependencies, and work on every app, not just Chrome. Then **P11** to close
the residual stickiness in report #1. Keep **M1/CDP** as the strategic
investment for the 72%-Chrome case, but it is no longer a prerequisite for
making Jarvis read the right text on an arbitrary screen — P8–P11 do that. The
architecture does not need replacing; it needs a salience model and a
higher-resolution second look at the content the user actually cares about.
