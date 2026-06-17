# Jarvis Agent Upgrade Spec

Terse reference for the next architectural iteration. Bullet lists and code blocks throughout.

---

## §0 Glossary + Conventions

- **bbox** — `(x, y, w, h)` in **screen coordinates** (pixels from top-left of primary monitor).
- **Target** — the window the user cared about when they invoked Jarvis. NOT necessarily the foreground window at perception time.
- **Rung** — one tier of the perception ladder: WINDOW < UIA < OCR < VISION (IntEnum, ascending cost).
- **ScreenModel** — the unified, fused snapshot of the target at a point in time.
- **entry_rung** — the cheapest rung the router says is sufficient to answer the query.
- **screen_hash** — dHash of the active-window crop; used as a fine-grained freshness key.
- **invokable** — an element can be acted upon (Invoke/Click pattern available).
- **confidence** — float 0.0–1.0. Below 0.5 = uncertain; below 0.3 = treat as absent.
- All timestamps are `time.monotonic()` floats unless noted.
- Process names are lowercase stems (`Path(exe).stem.lower()`).

---

## §1 Target Window

### Why foreground-at-perception-time is wrong

- Jarvis opens its own UI window on wake → takes focus → `GetForegroundWindow()` now returns Jarvis, not the user's app.
- Any perception or action resolved against the foreground will target Jarvis itself.
- Race: if the user Alt-Tabs between wake and perception, the foreground has already changed.

### Rule

- **Capture the target at WAKE time** (before `ui.show_window()`), store as `PerceptionTarget`.
- Reuse that same `PerceptionTarget` for all follow-up turns in the same session turn.
- **Jarvis must never perceive itself**: check `is_self` before any perception call; abort and warn if True.
- A new wake event starts a new target capture.

```python
# Correct order in _voice_invocation
target = capture_target()   # grab hwnd BEFORE showing UI
self.ui.show_window()
...
route_result = router.route(question, self.session, target=target)
```

---

## §2 Schemas

```python
from dataclasses import dataclass, field
from typing import Any

@dataclass
class PerceptionTarget:
    hwnd: int
    pid: int
    process: str                        # lowercase stem
    title: str
    bounds: tuple[int, int, int, int]   # (x, y, w, h) screen coords
    is_self: bool                       # True if hwnd belongs to JarvisWindow

@dataclass
class ScreenElement:
    id: str                             # stable deterministic hash of (role, text, bbox)
    role: str                           # UIA control_type or "text_block" / "region"
    text: str
    bbox: tuple[int, int, int, int]     # (x, y, w, h) screen coords
    source: str                         # "uia" | "ocr" | "cv" | "vision"
    confidence: float                   # 0.0–1.0
    invokable: bool
    handle: Any | None                  # pywinauto wrapper or None

@dataclass
class ScreenModel:
    target: PerceptionTarget
    elements: list[ScreenElement]
    full_text: str                      # concatenated text of all elements, newline-separated
    captured_at: float                  # time.monotonic()
    screen_hash: str                    # dHash hex of active-window crop
```

---

## §3 Perception Adapters

Each adapter takes a `PerceptionTarget` and returns `list[ScreenElement]`. Adapters never raise; return `[]` on failure.

### 3.1 UIA

- **Input:** `PerceptionTarget` (hwnd).
- **Process:** pywinauto `Desktop(backend="uia").window(handle=hwnd)`, DFS walk capped at `UIA_MAX_DEPTH` / `UIA_MAX_NODES`. Skip invisible rects and decorative types (Image, Separator, Custom).
- **Output:** one `ScreenElement` per visible named node; bbox converted to screen coords via `element_info.rectangle`.
- **Default confidence:** 0.95 (accessibility API is authoritative).
- **When used:** entry_rung ≤ UIA; always preferred for native Win32/WPF/browser apps.

### 3.2 OCR

- **Input:** BGR crop of target window bounds (from `PerceptionTarget.bounds`).
- **Process:** pytesseract `image_to_data(output_type=Output.DICT)`; filter by `conf >= 30`; group into line-level `ScreenElement`s using word bboxes.
- **Output:** one element per text line; bbox in screen coords (add window origin).
- **Default confidence:** `tesseract_conf / 100` per element (word-level; aggregate to line mean).
- **When used:** UIA returned empty or `ok=False`; Electron, games, PDF viewers.

### 3.3 CV

- **Input:** BGR crop of target window bounds.
- **Process:** Reuse `cv_pipeline.segment_regions()` contour logic. Each region becomes one element with role `"region"`.
- **Output:** one element per detected UI region; text = region label (e.g. "toolbar", "content_area"); no invokable.
- **Default confidence:** 0.6.
- **When used:** Supplementary layout context alongside OCR or Vision; never sole source for action grounding.

### 3.4 Vision

- **Input:** BGR crop of target window.
- **Process:** Encode to PNG base64; send to `VISION_BACKEND` (Gemini multimodal or Ollama/Moondream). Ask model to enumerate visible elements as JSON `[{role, text, bbox_approx}]`.
- **Output:** one element per model-returned item; confidence = 0.7 (model is approximate).
- **When used:** Terminal rung; all text rungs failed or query is explicitly visual.

---

## §4 Fusion

Goal: one authoritative `ScreenModel` from multiple adapter outputs.

### Algorithm

1. Collect all `list[ScreenElement]` from adapters that ran.
2. For each pair of elements from different sources, compute **IoU of bboxes**.
   - IoU ≥ 0.5 → same physical element → merge into one.
3. **Priority on merge:** UIA > OCR > CV > Vision.
   - Keep higher-priority source's `role`, `invokable`, `handle`.
   - Keep higher-confidence source's `text` unless it is empty; then fall back.
4. **Confidence-weighted dedup:** if two UIA elements have IoU ≥ 0.5, keep higher confidence.
5. Assemble `full_text` by sorting surviving elements top-to-bottom, left-to-right.
6. Compute `screen_hash` (dHash of crop, see §7).

```
adapters_out = [uia_elements, ocr_elements, cv_elements, vision_elements]
merged = fuse(adapters_out, iou_threshold=0.5)  # returns list[ScreenElement]
model = ScreenModel(target, merged, full_text, captured_at, screen_hash)
```

---

## §5 Routing

### 5.1 Local-LLM-first

- Default routing backend: local LLM (Ollama) for intent classification when available.
- Fallback order: local LLM → regex rules → default-bias TEXT.
- Local LLM prompt: structured JSON-only request (see contract below); timeout 2s.

### 5.2 Hybrid fast-path

```
query
  ├─ regex fast-path  →  if high-confidence match (ACTION safety-critical, explicit VISUAL phrases)
  │                       emit result immediately, skip LLM
  ├─ local LLM        →  all other queries; parse JSON response
  └─ regex fallback   →  if LLM unreachable or response unparseable
```

Safety-critical actions (`click_element`, `open_app`) ALWAYS go through regex fast-path confirmation regardless of LLM output — never rely solely on LLM to gate destructive actions.

### 5.3 Confidence-based escalation (replaces `looks_blind`)

- Replaces the hedge-string check with a numeric gate.
- After getting an answer, compute answer confidence via:
  - Model self-reported confidence (if available in response metadata).
  - Heuristic: length, hedge phrases, question marks in answer, etc.
- Escalation rule:
  ```
  if answer_confidence < ESCALATION_THRESHOLD and current_rung < Rung.VISION:
      run one rung deeper, re-ask once
  ```
- `ESCALATION_THRESHOLD = 0.6` (config).
- Hard cap: one escalation per query. VISUAL answers never escalate.
- Log `escalated=True` + both `entry_rung` and `escalated_rung` in telemetry.

### Routing JSON contract

Request (to local LLM or returned by regex fast-path):
```json
{
  "query": "<user query>",
  "context": "<brief window/app summary>"
}
```

Response (parsed from LLM or constructed by regex):
```json
{
  "intent": "ACTION | VISUAL | TEXT | NO_CONTEXT",
  "entry_rung": "WINDOW | UIA | OCR | VISION | null",
  "action_params": {"kind": "open_app", "args": {...}} | null,
  "confidence": 0.85
}
```

- `entry_rung: null` → no perception (NO_CONTEXT or ACTION handled by action layer).
- **Keep the existing default-bias TEXT rule**: if confidence < 0.5 or parse fails, emit `intent=TEXT, entry_rung=UIA, confidence=0.5`.

---

## §6 Action Grounding + Verification

### Grounding (open-loop → closed-loop)

Current: action parameters come from LLM JSON; element is located at execution time.  
Target: resolve every action target against the current `ScreenModel` before executing.

```
action_params = {kind: "click_element", args: {label: "Submit"}}

1. Resolve: find ScreenElement where text ≈ label AND invokable=True AND confidence ≥ 0.5
2. Gate: if no match → abort with reason, do not execute
3. Confirm: ui.confirm_action(description including element.text + element.bbox)
4. Execute: element.handle.invoke() or click_input()
5. Verify: re-perceive target after 300ms; compute new screen_hash
   - If screen_hash changed → action confirmed, log success
   - If screen_hash unchanged → log warning "no state delta detected"
```

### Rules

- Never execute on `element.confidence < 0.5`.
- Never execute on `element.invokable == False`.
- CV-sourced elements (`source="cv"`) are never invokable.
- Vision-sourced elements require explicit user confirmation even after grounding.
- `open_app` and `set_clipboard` do not require post-action re-perception (no screen delta expected immediately).

---

## §7 Caching

### Freshness key

```python
cache_key = f"{target.process}:{screen_hash}"
```

- `target.process` — catches window switches (different app, same title).
- `screen_hash` — dHash of the active-window crop; catches in-app content changes.

### dHash computation

```python
import cv2, numpy as np

def dhash(bgr_crop: np.ndarray, size: int = 8) -> str:
    gray = cv2.cvtColor(bgr_crop, cv2.COLOR_BGR2GRAY)
    resized = cv2.resize(gray, (size + 1, size))
    diff = resized[:, 1:] > resized[:, :-1]
    return format(int(np.packbits(diff.flatten()).tobytes().hex(), 16), 'x')
```

### TTL

- `SCREEN_READ_TTL` (config, default 20s) is a **ceiling**, not a trigger.
- A cache entry is stale if: `screen_hash` changed **OR** `age > SCREEN_READ_TTL`.
- On cache hit: skip the perception ladder entirely.
- On cache miss: run ladder from `entry_rung`, update cache with new `ScreenModel`.

---

## §8 Local Vision Backend + Privacy

### Backend selection

`VISION_BACKEND` in `config.py`:
- `"gemini"` — pixels sent to Google's API. Requires `GEMINI_API_KEY`.
- `"local"` — pixels sent to Ollama at `localhost:11434`. Requires Ollama running with `MOONDREAM_MODEL` pulled.

### When pixels leave the device

| Operation | Leaves device? | Backend |
|---|---|---|
| UIA tree walk | No | — |
| OCR (tesseract) | No | local binary |
| CV segmentation | No | local |
| Vision rung | **Yes, if `VISION_BACKEND="gemini"`** | Google API |
| Vision rung | No, if `VISION_BACKEND="local"` | Ollama |
| Gemini text answer | Yes (text only, no screenshot) | Google API |
| Gemini action parse | Yes (text only) | Google API |

### Privacy rules

- Never send pixels when `entry_rung < VISION` and the ladder already got a useful result.
- Log `vision_backend` in telemetry whenever the VISION rung is used.
- `local_vision.py` is the integration point for `VISION_BACKEND="local"`; it is currently unused in the hot path and must be wired up to `perception.read_vision()` (guarded by the config flag).

---

## §9 Eval Harness

### Structure

```
jarvis/tests/
  fixtures/
    queries.jsonl          # {query, expected_intent, expected_rung, expected_action_kind}
    screen_snapshots/      # captured ScreenModel JSON + screenshot pairs
  test_classify.py         # offline intent + rung assertion
  test_router.py           # offline routing (no live perception)
  test_action_grounding.py # resolve against fixture ScreenModels, assert element match
  test_fusion.py           # unit tests for IoU merge logic
```

### `queries.jsonl` schema

```jsonl
{"query": "open notepad", "intent": "ACTION", "rung": null, "action_kind": "open_app"}
{"query": "what am I looking at", "intent": "VISUAL", "rung": "VISION", "action_kind": null}
{"query": "summarize what I'm reading", "intent": "TEXT", "rung": "UIA", "action_kind": null}
{"query": "something random", "intent": "TEXT", "rung": "UIA", "action_kind": null}
```

### Offline scoring

```python
# test_classify.py sketch
import json
from classify import classify_intent

with open("fixtures/queries.jsonl") as f:
    cases = [json.loads(l) for l in f]

passed = total = 0
for c in cases:
    r = classify_intent(c["query"])
    total += 1
    if r.intent.value == c["intent"]:
        passed += 1
    else:
        print(f"FAIL: {c['query']!r} → {r.intent.value} (expected {c['intent']})")

print(f"{passed}/{total} passed")
```

- Target: 100% on fixtures before merging routing changes.
- Add new fixture rows whenever a misclassification is found in telemetry.
- `screen_snapshots/` fixture captures allow testing fusion and grounding without live Windows APIs.
