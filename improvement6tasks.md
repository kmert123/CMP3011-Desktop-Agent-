# Jarvis — Perception Salience & Resolution Treatment Plan (Wave 6)

Treatment for the root causes in `jarvis/docs/DIAGNOSIS2.md`. The second-pass diagnosis concludes the
architecture is **structurally correct — keep it** — and is missing two things: a **content-salience
model (M2)** and a **higher-resolution second look at content (P9)**. These tasks implement the
diagnosis's new remediation items **P8–P12** (extending DIAGNOSIS.md's P1–P7). The existing CDP plan
(M1 / DIAGNOSIS.md Tasks 4–6) stays the top *structural* item but is deliberately **not a prerequisite**
here — every task below works on any app, CDP or not.

Each task is scoped to be handed to a coding agent as-is. The `prompt` block is what you paste; it
carries enough technical direction to act without re-deriving the design.

## Conventions

- `@jarvis/docs/DIAGNOSIS2.md §N` references the diagnosis. Section map:
  - §1 architecture verdict (keep it; add M1+M2) · §2 report #1 "stuck on prior context" (F1–F3)
  - §3 report #2 "misses small text" (F4–F6) · §4 report #3 "attends to chrome not content" (F7–F8)
  - §5 OCR resolution/threshold regime is mistuned · §6 remediation P8–P12 · §7 what to do first
- Fix → task map: **F7,F8 → Task 1** · **F4,F6 → Task 2** · **F5 → Task 3** · **F1 → Task 4** ·
  **F3,F2 → Task 5**. (These correspond to diagnosis P8, P9, P10, P11, P12 respectively.)
- Tasks are ordered so dependencies always precede dependents. Don't skip ahead.
- Every task ends with a concrete **Verify** check. Treat a task as incomplete until it passes.
- The whole codebase lives under `jarvis/`. All file paths below are relative to `jarvis/`.
- Two principles from the diagnosis run through this wave:
  - **Content before chrome.** The model must see page content before it ever sees tabs/omnibox/sidebar.
  - **A higher-resolution second look beats one coarse pass.** Re-read the content region at higher
    scale instead of trusting a single full-window OCR.

**Treatment summary (maps diagnosis → tasks):**

| Diagnosis fix (rank) | Report addressed | Task |
|---|---|---|
| F7,F8 — content-region detection + salience (P8) | #3 (and helps #2) | 1 |
| F4,F6 — content-region re-OCR pass + PSM (P9) | #2 | 2 |
| F5 — soften OCR thresholds inside content region (P10) | #2 | 3 |
| F1 — perishable wake-time interaction state (P11) | #1 (residual) | 4 |
| F3,F2 — weak-perception history demotion + browser cache (P12) | #1 | 5 |

**Phases:** Tasks 1–3 are the perception-quality core (do them together; they map one-to-one onto
reports #3 and #2 and share the content-region concept). Tasks 4–5 close the residual context-bleed in
report #1. Per §7, ship **1 + 2** first, then **3**, then **4**, then **5**.

---

## Task 1: Content-region detection + salience weighting (Fixes F7, F8 / P8)

**Goal:** Teach the pipeline the difference between browser chrome (tabs, omnibox, bookmarks, side
panel) and page content, then render content first and escalate on content confidence — so the model
stops anchoring on tab/sidebar text. This is the **highest new leverage** change and is the prerequisite
for Task 2. Fixes report #3, substantially helps #2.
**Files:** new `content_region.py`; `screen_model.py` (new field + prompt order); `fusion.py` (tag
elements); `router.py` (content-only escalation confidence); `perception.py` (invoke the resolver in the
fusion path); `config.py` (constants).
**Depends on:** none.

```prompt
Read @jarvis/docs/DIAGNOSIS2.md §4 (report #3) and §6/P8.

Today nothing distinguishes browser chrome from page content. capture.py crops the ENTIRE window
(title bar + tab strip + omnibox + bookmarks + side panel + content). OCR over that produces short,
high-contrast, high-confidence chrome strings ("New Tab", "youtube.com", bookmark names) that
(a) get the HIGHEST calibrated_confidence, (b) render FIRST in to_prompt_block() reading order and
eat the 400-token budget before content is reached, and (c) keep max(calibrated_confidence) high
enough to SUPPRESS escalation even when the actual content was perceived terribly. Add a salience model.

1. New module content_region.py with resolve_content_region(target, frame, elements, origin) ->
   bbox (x,y,w,h) in virtual-desktop pixels (same coord space as ScreenElement.bbox). Implement the
   GEOMETRIC heuristic first (ship-first, no new deps):
   - For app_class CHROMIUM_ELECTRON (and UWP webview): subtract a top chrome band (tab strip +
     omnibox + bookmarks ≈ 110–140px at 100% DPI) scaled by target dpi_scale; subtract narrow
     left/right side panels (columns whose width < ~15% of window width and that run nearly full
     height). The remaining central rectangle is the content region.
   - For non-browser app_classes: default the content region to the full window (no chrome to strip)
     so behavior is unchanged for native apps.
   - Make the band sizes / side-panel fraction config constants (CHROME_TOP_BAND_PX=124,
     CHROME_SIDE_PANEL_MAX_FRAC=0.15). Leave a clear seam/TODO for a CV-assisted version (largest
     central contour block, reusing cv_pipeline) and a CDP-native version (the <main>/largest text
     scroll container) — do NOT build those now.

2. screen_model.py: add `in_content_region: bool = True` to ScreenElement (default True so native apps
   and existing fixtures are unaffected). In to_prompt_block(): render in-content-region elements FIRST
   (existing tree order within them), then a separate, clearly-labelled, budget-capped section for
   chrome, e.g. "Browser chrome (low priority): ..." capped at ~15% of max_tokens. Content must never
   be truncated to make room for chrome.

3. fusion.py: after elements are assembled, call content_region.resolve_content_region(...) and set
   in_content_region on each element by bbox containment (centre inside the region). Tag this in the
   fusion path only; if no region resolver applies, leave all True.

4. router.py: in should_escalate() (and any max-confidence computation that drives pre-answer
   escalation), compute the max calibrated_confidence over in_content_region elements ONLY, so a
   high-confidence "youtube.com" omnibox string can no longer mask poorly-perceived content. If there
   are zero content-region elements, fall back to the old all-elements max (don't divide by zero).

5. perception.py: ensure the fusion path produces a ScreenModel whose elements carry the
   in_content_region tag (i.e. the fusion step in (3) runs before the model is returned).

Keep everything behavior-neutral for native/non-browser app classes. This is independent of CDP.
```

**Verify:** On a Chrome page (e.g. YouTube), `to_prompt_block()` lists page content before any tab/omnibox text, the chrome section is clearly labelled and budget-capped, and a query whose content was perceived weakly now escalates even when the omnibox string is high-confidence (escalation keys on content-region confidence only).

---

## Task 2: Content-region re-OCR pass at higher scale + PSM (Fixes F4, F6 / P9)

**Goal:** Automate what the user did manually (zoom): after the coarse full-window OCR, re-read just the
content region at a higher scale and a sparse-layout PSM, then merge. Fixes report #2 (small Twitter
caption read only after zoom).
**Files:** `perception.py` (second pass in the fusion path); `adapters/ocr_adapter.py` (parameterise
scale + PSM in `read_ocr`/`read_region`); `config.py` (`CONTENT_REOCR`, PSM-by-class).
**Depends on:** 1 (needs the content region to scope the second pass).

```prompt
Read @jarvis/docs/DIAGNOSIS2.md §3.1, §3.3 and §6/P9.

The default perception pass runs ONE full-window OCR at OCR_SCALE=2.5 with PSM=6 (uniform block). On a
maximised browser, a ~14px tweet caption upscaled 2.5× is ~35px — near Tesseract's floor — so it reads
poorly or not at all. read_region() already exists and runs at READ_REGION_SCALE=3.5, but it is only
reachable as a Gemini tool / focus refresh, never in the default pass. Add a scoped second pass.

1. config.py: add
   CONTENT_REOCR = True             # master flag for the second pass
   CONTENT_REOCR_SCALE = 3.5        # reuse READ_REGION_SCALE-level resolution for the content region
   OCR_PSM_CONTENT = 11             # sparse-text PSM for social/feed content regions
   (Keep OCR_PSM=6 for the full-window pass; §5 notes PSM 6 is right for native dialogs.)

2. adapters/ocr_adapter.py: parameterise read_ocr so the scale and PSM are arguments
   (read_ocr(crop, origin, scale=OCR_SCALE, psm=OCR_PSM)). Keep current defaults so existing callers and
   fixtures are unchanged. read_region() already upscales — ensure its PSM is also parameterisable.

3. perception.py: in the fusion path, AFTER the full-window OCR + fuse() produces the content region
   (Task 1), when CONTENT_REOCR is on AND the query is a content query (Intent.TEXT/ANSWER — i.e. not a
   pure action) AND a content region was resolved that is smaller than the full window, run a SECOND OCR
   pass over just the content-region crop at CONTENT_REOCR_SCALE with OCR_PSM_CONTENT, and MERGE those
   elements into the model (reuse fuse()/the existing dedup so duplicates collapse; higher-res elements
   should win on text where they overlap the coarse pass). Scope strictly to the content region so cost
   stays bounded — do NOT re-OCR the whole window.

4. Gate cost: only one second pass per turn; skip it for ACT intents and for windows where the content
   region equals the full window (nothing to gain).

Coordinate space: the re-OCR crop is the content-region sub-rectangle; convert its element bboxes back
to virtual-desktop pixels exactly as read_region does (sub-crop origin + window origin), so merged
elements share the same coordinate space as the first pass.
```

**Verify:** On a fixture/page with small body text that the single 2.5× pass fragments or drops, the second content-region pass at 3.5× / PSM 11 recovers the caption as coherent text in the merged `ScreenModel`; an ACT-intent query does NOT trigger the second pass.

---

## Task 3: Soften OCR thresholds inside the content region (Fix F5 / P10)

**Goal:** Stop silently hard-dropping low-confidence OCR lines *inside the content region* before fusion
ever sees them; keep-and-tag them and let salience + fusion arbitrate. Leave chrome/native thresholds
as-is. Completes the report-#2 fix.
**Files:** `adapters/ocr_adapter.py`; `config.py`.
**Depends on:** 1, 2.

```prompt
Read @jarvis/docs/DIAGNOSIS2.md §3.2 and §6/P10.

Two filters drop exactly the small/anti-aliased text the user cares about, BEFORE fusion sees it:
- ocr_adapter.py _TOKEN_CONF_MIN = 30 (drops individual tokens < 30%)
- mean_conf < OCR_MIN_CONF (0.4) drops whole lines.
A caption Tesseract reads at ~35% mean confidence is gone before escalation/salience can recover it —
the element simply doesn't exist in the ScreenModel.

Soften thresholds ONLY for the content-region re-OCR pass (Task 2), not the full-window pass:
1. config.py: add OCR_MIN_CONF_CONTENT = 0.25 (and, if you keep a token floor, a lower
   _TOKEN_CONF_MIN_CONTENT, e.g. 15) used only by the content-region pass.
2. ocr_adapter.py: thread the threshold(s) as parameters of read_ocr/read_region (defaulting to the
   current strict values) so the Task-2 content pass can call with the softened values while the
   full-window pass keeps OCR_MIN_CONF=0.4 / _TOKEN_CONF_MIN=30 unchanged.
3. Preferred per the diagnosis: instead of permanently lowering the bar everywhere, KEEP low-confidence
   lines that fall inside the content region (tagged low-confidence via their existing confidence value,
   which calibration + fusion already use) rather than discarding them. A low-confidence line inside the
   content area is far more valuable than a high-confidence "Subscribe" button — the Task-1 salience tag
   lets downstream code weight it correctly.

Do NOT change the native-app dialog case: the strict full-window thresholds still serve clean,
high-contrast text well. The softening is content-region-scoped only.
```

**Verify:** A content-region line that Tesseract reads at ~30–35% mean confidence now survives into the `ScreenModel` (tagged low-confidence) instead of being dropped; the full-window/native pass still applies the strict 0.4 / 30 thresholds and chrome noise is not amplified.

---

## Task 4: Perishable wake-time interaction state (Fix F1 / P11)

**Goal:** Treat wake-time `cursor_pos` / `focused_element` / `selection_text` as *perishable* so a stale
selection or focused-element string from a prior turn can't outweigh fresh (often weak) perception —
the mechanism behind the Twitter "said it only after I told him" symptom. Fixes the residual part of
report #1.
**Files:** `perception_target.py` (TTL stamp); `focus_resolver.py` (liveness + "text present in model"
check); `main.py` (live re-read on typed follow-up); `config.py`.
**Depends on:** none (independent of Tasks 1–3; lands the report-#1 residual).

```prompt
Read @jarvis/docs/DIAGNOSIS2.md §2.1 and §6/P11.

PerceptionTarget captures cursor_pos, focused_element (raw COM pointer), and selection_text ONCE at wake
time. On follow-ups these describe a past moment. DIAGNOSIS.md Task 2 added a DEAD-pointer guard, but a
pointer can be ALIVE AND WRONG after a same-page scroll or SPA update, and selection_text is a plain
string re-injected verbatim with no liveness check. When current OCR is weak, this stale focus block
(injected as PRIMARY context by gemini.py _format_focus_block) outweighs fresh perception and looks like
"remembering" the previous turn.

Make wake-time interaction state perishable:
1. config.py: add FOCUS_STATE_TTL_MS = 1500 (same window as FOLLOWUP_RECAPTURE_MS).
2. perception_target.py: the target already has wake_ts. Add a helper, e.g.
   interaction_state_fresh() -> bool returning (monotonic() - wake_ts) <= FOCUS_STATE_TTL_MS. Any
   consumer of cursor_pos / focused_element / selection_text must check this first and treat the field
   as ABSENT when stale (do not delete the fields; just refuse to use them when expired).
3. main.py: on a TYPED follow-up, re-read selection and focus LIVE against the current foreground hwnd
   (a fresh GetFocusedElement / TextPattern GetSelection is cheap) instead of reusing the wake-time
   snapshot. Reuse the existing capture path so a fresh PerceptionTarget fully replaces the old one (no
   field merged from the prior target). (This complements DIAGNOSIS.md Task 1's title-aware recapture.)
4. focus_resolver.py: when a focused element resolves but its text is NOT present anywhere in the
   current ScreenModel (substring/fuzzy check against model.full_text or element texts), DISCARD it — a
   focused element whose text no longer appears on screen is stale by definition. Same idea for a
   selection_text string that doesn't appear in the current model when perception is non-empty.

Do not weaken the live/fresh happy path; only short-circuit provably-stale state.
```

**Verify:** Select text on page A, ask a deictic question; scroll or navigate to page B (no selection), wait past the TTL or type a follow-up → the answer does NOT report page A's selection, and a focused-element whose text is absent from the current screen is discarded rather than injected as primary context.

---

## Task 5: Weak-perception history demotion + tighter browser cache (Fixes F3, F2 / P12)

**Goal:** When fresh perception is below a content floor, stop the history block from outweighing it
(the within-window scroll case DIAGNOSIS.md Task 8 doesn't cover), and tighten the browser cache so two
visually-similar SPA feed states within the TTL stop colliding — until CDP provides a canonical URL key.
Closes report #1.
**Files:** `session_context.py` (history floor gate + browser cache constants); `config.py`.
**Depends on:** 1 (the "perception is weak" signal — content-region element count/chars — is cleanest
once content elements are tagged), 4.

```prompt
Read @jarvis/docs/DIAGNOSIS2.md §2.3 (F3), §2.2 (F2), and §6/P12.

Two within-window context-bleed gaps remain after DIAGNOSIS.md Tasks 3 & 8:

A) HISTORY WINS WHEN PERCEPTION IS EMPTY (F3). DIAGNOSIS.md Task 8's window-continuity gate only handles
   CROSS-window turns. Same Chrome window, user scrolls the feed and asks about a new post: all history
   turns share the window signature, none are annotated, and when fresh OCR is sparse the model anchors
   on its own prior answer.
   Fix: in session_context.to_prompt_block(), compute a "perception is weak" signal from the CURRENT
   model — fewer than HISTORY_CONTENT_FLOOR_ELEMENTS text-bearing content-region elements (use the
   in_content_region tag from Task 1) OR < HISTORY_CONTENT_FLOOR_CHARS of content full_text. When weak,
   DEMOTE history: keep only the last 1 turn AND prefix the history block with an explicit instruction
   that the screen has changed and prior answers may not apply. Reuse the same weak signal that already
   drives escalation; add the two floor constants to config.py (e.g.
   HISTORY_CONTENT_FLOOR_ELEMENTS=3, HISTORY_CONTENT_FLOOR_CHARS=40). Keep the existing guarantee that
   raw screen text is never stored in history.

B) BROWSER CACHE CAN SERVE A STALE FEED STATE (F2). For SPA sites that never change window title/omnibox
   between tweets/videos, the only differentiator left is the 16×16 roi_dhash (Hamming tol 10/256) and
   the 2s browser TTL — two similar feed states within 2s can collide and serve stale content.
   Interim fix (until CDP lands — the canonical fix is CDP's navigation-history/target URL):
   - Tighten BROWSER_SCREEN_READ_TTL to ~1.0s (from 2s).
   - Add a browser-only BROWSER_CACHE_HAMMING_MAX (lower than the global CACHE_HAMMING_MAX=10, e.g. 6)
     and use it for CHROMIUM_ELECTRON/UWP in screen_read_fresh(); keep native windows on CACHE_HAMMING_MAX.
   This trades more cache misses (more OCR) for never serving a stale feed state. Leave a clear
   comment that CDP (DIAGNOSIS.md Task 6) supersedes this with an exact URL key.

Keep native-app cache behavior (8s TTL, hamming 10) unchanged.
```

**Verify:** With sparse current perception, history is reduced to the last turn and prefixed with the "screen changed" notice, and the model answers about the current screen without parroting a prior page; on a browser, two similar feed states within ~1s no longer return a cache hit (the tighter TTL + browser Hamming force a re-read).

---

## Dependency summary

```
Perception-quality core (ship 1+2 first, then 3):
  1 ──┬── 2 ── 3        (content region + salience → content re-OCR → soften thresholds)
      └── 5 (A: history floor reuses the in_content_region signal)

Context-bleed residual (report #1):
  4                      (perishable wake-time interaction state — independent)
  4 ── 5                 (history demotion + browser cache; 5 also benefits from 1)
```

Per `DIAGNOSIS2.md §7`: ship **Task 1 + Task 2** together — they map one-to-one onto reports #3 and #2,
need no new dependencies, and work on every app, not just Chrome. Then **Task 3** to recover the
low-confidence content lines. Then **Task 4** to close the residual stickiness in report #1, and
**Task 5** for the within-window history/cache gaps. **M1/CDP** (DIAGNOSIS.md Tasks 4–6) remains the
strategic investment for the 72%-Chrome case but is *not* a prerequisite for any task here — when it
lands, Task 1 gains a native content-region source and Task 5/F2 gains the exact URL key.
