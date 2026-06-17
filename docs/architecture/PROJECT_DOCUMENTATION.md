# Jarvis — Desktop AI Agent Project Documentation

## Project Overview

**Jarvis** is a voice-activated, screen-aware desktop AI agent that listens for the wake word "Jarvis", captures the active window, processes it through a computer vision pipeline, and uses Google's Gemini 2.5 Flash to answer user questions about their screen. Responses are displayed in a chat window with support for follow-up questions.

**Primary OS:** Windows  
**Secondary OS:** macOS (same codebase with OS shims)  
**Language:** Python 3.10+  
**Budget:** Free tiers only (Gemini API, openWakeWord, local Whisper)  
**Status:** Multi-phase development (MVP complete, improvements underway)

---

## Project Philosophy

Jarvis is built around an **intelligent router** pattern, not a monolithic pipeline. The system intelligently determines what perception level is needed for each query:

- **Cheapest perception first**: Can a simple OCR or UIA query answer this? Send that before involving Gemini.
- **Escalate only when needed**: Only escalate to vision+reasoning when the question requires true understanding of the screen.
- **Measure everything**: Every query is logged with telemetry (latency, perception path, cache hits, escalation).

This approach optimizes for performance and API cost while maintaining answer quality.

---

## Repository Structure

```
comp_vision_project/
├── jarvis/                          # Main application package
│   ├── core/                        # Core abstractions
│   │   ├── __init__.py
│   │   ├── events.py                # Event definitions (QueryEvent, ResponseEvent, etc.)
│   │   └── session_actor.py         # Session lifecycle management
│   │
│   ├── adapters/                    # Perception layer — pluggable data sources
│   │   ├── __init__.py
│   │   ├── cv_adapter.py            # OpenCV region detection
│   │   ├── ocr_adapter.py           # Pytesseract/EasyOCR text extraction
│   │   ├── selection_adapter.py     # Recently selected text from clipboard
│   │   ├── uia_adapter.py           # Windows UIA accessibility tree
│   │   └── vision_adapter.py        # Gemini vision reasoning
│   │
│   ├── main.py                      # Application entry point
│   ├── config.py                    # Configuration + constants (all from .env)
│   ├── capture.py                   # mss screenshot capture
│   ├── wake_word.py                 # openWakeWord listener
│   ├── voice.py                     # PyAudio recording + silence detection
│   ├── transcription.py             # Whisper transcription
│   ├── privacy.py                   # First-run privacy warning modal
│   ├── ui.py                        # CustomTkinter chat window + event queue
│   │
│   ├── perception.py                # Orchestrates adapters, routes to cheapest path
│   ├── perception_policy.py         # Policy engine for adapter selection
│   ├── perception_target.py         # Target definition (query intent classifier)
│   ├── router.py                    # Routes queries to correct perception path
│   ├── llm_router.py                # Determines which LLM backend to use
│   │
│   ├── gemini.py                    # Gemini API client + prompt construction
│   ├── local_vision.py              # Local vision model fallback (Ollama/Moondream)
│   ├── local_llm.py                 # Local LLM fallback
│   │
│   ├── cv_pipeline.py               # Classical CV: ROI, segmentation, change detection
│   ├── content_region.py            # Region-of-interest logic
│   ├── screen_model.py              # Screen state abstraction
│   ├── world_state.py               # Global state tracker (current app, mouse pos, etc.)
│   ├── focus.py                     # Window focus tracking
│   ├── focus_resolver.py            # Cross-platform focus detection
│   ├── app_classifier.py            # Identifies running application context
│   ├── classify.py                  # Intent/content classification utilities
│   │
│   ├── actions.py                   # Safe action execution (click, type, etc.)
│   ├── set_of_marks.py              # Selectable UI element tracking ("Jarvis, click mark 5")
│   ├── debug_overlay.py             # Debugging visualization on screen
│   │
│   ├── calibration.py               # Multi-monitor calibration
│   ├── telemetry.py                 # JSONL query logging (cost/latency tracking)
│   ├── session_context.py          # Session state (recently visited, app history)
│   ├── logging_setup.py             # Logging configuration
│   ├── trace.py                     # Query tracing/debugging
│   ├── uia_watcher.py               # UIA event monitoring
│   ├── fusion.py                    # Sensor fusion (combine UIA + OCR + vision)
│   │
│   ├── gen_frames.py                # Streaming frame generation
│   ├── harness.py                   # Test harness for offline development
│   │
│   ├── requirements.txt              # Python dependencies
│   ├── .env.example                 # Environment template
│   ├── .env                         # Local secrets (GITIGNORED)
│   ├── .gitignore                   # Git exclusions
│   └── README.md                    # Setup and usage guide
│
├── docs/                            # Documentation (markdown)
├── evals/                           # Evaluation scripts + datasets
├── tools/                           # Utilities and scripts
├── JARVIS_SPEC.md                   # Original MVP specification (frozen reference)
├── TASKS.md                         # MVP implementation tasks (1-18)
├── improvement1tasks.md             # Phase 2: Router + telemetry (Tasks 1-15)
├── improvement2tasks.md             # Phase 2 continued: Advanced features
│   ... (improvement3-9tasks.md)     # Future improvement phases
└── .claude/                         # Claude Code configuration

```

---

## Core Components Explained

### 1. **Voice Pipeline** (`wake_word.py`, `voice.py`, `transcription.py`)

| Component | Technology | Purpose |
|-----------|-----------|---------|
| **Wake Word** | openWakeWord (ONNX) | Detects "Hey Jarvis" in background audio (16kHz, 1% CPU) |
| **Audio Recording** | PyAudio | Records user voice at 16-bit PCM, 16kHz, mono |
| **Transcription** | Whisper "base" | Converts PCM to text locally (offline, ~150MB model) |

**Flow:** Background listener → detects wake word → records until silence → transcribes → triggers main pipeline

---

### 2. **Screenshot & CV Pipeline** (`capture.py`, `cv_pipeline.py`)

Classical computer vision that Gemini cannot do natively:

| Stage | Input | Process | Output |
|-------|-------|---------|--------|
| **1. ROI** | Full monitor screenshot | Detect + crop to active window | Cropped image, window title |
| **2. Segmentation** | Cropped BGR image | Canny edges → contours → classify by position | List of regions (toolbar, content, dialog, statusbar) |
| **3. Change Detection** | Current + previous frame | Frame diff → track changed regions | List of regions changed since last capture |

**Why Classical CV?**
- Gemini can't output structured region coordinates.
- Change detection needs frame-to-frame history (Gemini is stateless).
- ROI cropping reduces API payload and model attention.

**What we DON'T do:**
- OCR (Gemini reads text well natively)
- YOLO/object detection (overkill, Gemini describes objects)
- Image resizing (Gemini Flash handles active window fine)

---

### 3. **Perception Layer** (`perception.py`, `adapters/`, `router.py`)

The **intelligent router** that chooses the cheapest perception path:

```
Query arrives
  ↓
Classify intent (is this UIA-answerable? OCR? Vision?)
  ↓
Try adapters in order of cost/speed:
  1. UIA adapter    (100ms, free)    — "What's in this text field?"
  2. OCR adapter    (300ms, free)    — "Read the error on screen"
  3. Vision adapter (3-5s, API cost) — "What does this image mean?"
  ↓
Return answer or escalate to next rung
  ↓
Log telemetry (latency, perception path, cost)
```

**Adapters:**
- `uia_adapter.py` — Windows Accessibility (UIA) tree navigation
- `ocr_adapter.py` — Pytesseract or EasyOCR text extraction
- `selection_adapter.py` — Recently copied text from clipboard
- `cv_adapter.py` — Region boundaries and layout
- `vision_adapter.py` — Gemini multimodal reasoning

---

### 4. **LLM Integration** (`gemini.py`, `local_vision.py`, `local_llm.py`)

| Backend | When | Cost | Speed | Notes |
|---------|------|------|-------|-------|
| **Gemini 2.5 Flash** | Default | ~$0.075/1M input tokens | 1-3s | Cloud, reliable, supports vision |
| **Ollama Moondream2** | Offline/unreliable API | Free | 2-5s | Local, needs RTX 3060+ 6GB VRAM |
| **Ollama Qwen2-VL 2B** | Better quality locally | Free | 3-7s | Local, ~4GB VRAM, superior reasoning |

**Switchable via `config.VISION_BACKEND`** and **`config.LLM_BACKEND`**

**Prompt Structure:**
- System: "You are Jarvis, a desktop screen assistant..."
- User: Context dict (active window, regions, changed regions) + question
- Image: Cropped active window (RGB) as multimodal attachment

---

### 5. **UI Layer** (`ui.py`)

CustomTkinter chat window with thread-safe event queue:

```
┌─────────────────────────────────────┐
│ Status bar (Hearing / Processing)   │
├─────────────────────────────────────┤
│                                     │
│  Scrollable chat history            │
│  User messages (right-aligned)      │
│  Jarvis responses (left-aligned)    │
│                                     │
├─────────────────────────────────────┤
│ Text input for follow-ups [ Send ]  │
└─────────────────────────────────────┘
```

**Features:**
- Always-on-top, bottom-right of monitor
- Close (X) hides window; listener keeps running
- Event queue for thread-safe updates
- Status states: Hearing → Recording → Processing → Thinking → Response

---

### 6. **Telemetry** (`telemetry.py`, `session_context.py`)

**Per-query JSONL logging:**
```json
{
  "ts": "2026-06-17T10:30:45Z",
  "query": "What's the error?",
  "intent": "error_diagnosis",
  "perception_rung": "vision",
  "used_cache": false,
  "escalated": false,
  "latency_ms": 2340,
  "error": null,
  "action_kind": null
}
```

Used to measure:
- Route effectiveness (% of queries needing vision vs OCR vs UIA)
- Cost optimization (average tokens/query)
- Performance (p50, p95 latency)
- Cache hit rates

---

### 7. **State Management** (`world_state.py`, `screen_model.py`, `session_context.py`)

Passive state that feeds every prompt:
- **Current app** (from focus detection)
- **Mouse position**
- **Recent app switches** (history)
- **Clipboard** (recently selected text)
- **Screen geometry** (multi-monitor layout)
- **Query history** (last 5 questions)

No state persists to disk; all lost on app exit.

---

## Configuration

**File:** `jarvis/config.py` (generated from `.env` and defaults)

### Key Settings

```python
# Models
GEMINI_MODEL = "gemini-2.5-flash"
WHISPER_MODEL = "base"
VISION_BACKEND = "gemini"  # or "local" for offline
LLM_BACKEND = "gemini"      # or "local_llm"

# Audio (16kHz required for Whisper + openWakeWord)
SAMPLE_RATE = 16000
CHANNELS = 1
CHUNK_SIZE = 1024
SILENCE_THRESHOLD_RMS = 500
SILENCE_DURATION_SEC = 1.5
MAX_RECORDING_SEC = 30

# Wake word
WAKEWORD_MODEL = "hey_jarvis"
WAKEWORD_THRESHOLD = 0.5

# CV Pipeline
MIN_CONTOUR_AREA_RATIO = 0.005      # 0.5% of frame
TOOLBAR_Y_RATIO = 0.10
STATUSBAR_Y_RATIO = 0.85
CHANGE_DIFF_THRESHOLD = 30

# UI
WINDOW_WIDTH = 480
WINDOW_HEIGHT = 600

# Telemetry
TELEMETRY_PATH = "~/.jarvis/telemetry.jsonl"
```

**Secrets (`.env`, GITIGNORED):**
```env
GEMINI_API_KEY=<your-api-key>
# Optional:
# OLLAMA_BASE_URL=http://localhost:11434
# LOCAL_LLM_MODEL=mistral:latest
```

---

## Data & Privacy

| Data | Storage | Retention |
|------|---------|-----------|
| Screenshots | RAM (np.ndarray) | Discarded after pipeline |
| Audio | RAM (bytes buffer) | Discarded after transcription |
| Chat history | RAM (Python list) | Lost on app close |
| API keys | `.env`, gitignored | Local only |
| Telemetry | `~/.jarvis/telemetry.jsonl` | Persistent, user can delete |
| First-run marker | `~/.jarvis_seen` | Persistent (privacy ack) |

**First Launch:** Modal warning shown on first run. After dismissal, touches `~/.jarvis_seen` so subsequent launches skip it.

---

## Execution Flow (High Level)

```
1. main.py starts JarvisApp
   ├─ Check first-run → show privacy modal if needed
   ├─ Start WakeWordListener (background thread)
   ├─ Show/hide UI based on state
   └─ Main thread runs UI.mainloop()

2. User says "Hey Jarvis, what is this error?"
   └─ WakeWordListener detects wake word
   
3. Background thread spawned:
   ├─ Set UI status: "Heard you, listening..."
   ├─ voice.record_until_silence() → PCM bytes
   ├─ Set UI status: "Processing speech..."
   ├─ transcription.transcribe_pcm(pcm) → "what is this error?"
   ├─ Render user message to UI
   │
   └─ _answer(question):
       ├─ Set UI status: "Analyzing screen..."
       ├─ capture.capture_primary_monitor() → screenshot
       ├─ pipeline.run(screenshot) → context dict
       │   ├─ Crop to active window
       │   ├─ Segment regions
       │   ├─ Detect changes
       │   └─ Return {active_window, regions, changed_regions, image}
       │
       ├─ router.route(question, context) → "use_vision"
       ├─ Set UI status: "Thinking..."
       ├─ gemini.ask(question, context) → "The error is..."
       ├─ telemetry.log_query({query, perception_path, latency_ms})
       └─ Render Jarvis message to UI via thread-safe queue

4. User types follow-up: "What's the fix?"
   └─ Same flow as step 3 (re-captures screen for changed context)
```

---

## Development Workflow

### Running the App

```bash
# First time setup
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r jarvis/requirements.txt
cp jarvis/.env.example jarvis/.env
# Edit jarvis/.env and add your GEMINI_API_KEY from aistudio.google.com

# Run
python jarvis/main.py
```

### Testing Individual Modules

```bash
# Test Gemini API client
python jarvis/gemini.py

# Test screenshot capture
python jarvis/capture.py

# Test voice recording
python jarvis/voice.py

# Test wake word listener
python jarvis/wake_word.py

# Test CV pipeline
python jarvis/cv_pipeline.py

# Test UI
python jarvis/ui.py
```

### Debugging

1. **Logs:** Check `~/.jarvis_*.log` and `~/.jarvis/telemetry.jsonl`
2. **Debug overlay:** Enable in config → marks regions on screen
3. **Trace mode:** `config.DEBUG_TRACE = True` → prints every state change
4. **Offline harness:** `jarvis/harness.py` — test without audio/UI

---

## Dependency Tree

```
Phase 1 (MVP — Spec):
  Task 1  → Project skeleton (config, requirements)
  Task 2  → Gemini API client
  Task 3  → Screenshot capture
  Task 4  → Whisper transcription
  Task 5  → Audio recording + silence detection
  Task 6  → Wake word listener
  Task 7  → Integration smoke test (proto_main.py)
  Task 8  → Active window ROI
  Task 9  → UI region segmentation
  Task 10 → Change detection + CVPipeline class
  Task 11 → CV pipeline wired into Gemini
  Task 12 → CustomTkinter chat window
  Task 13 → Threading + queue communication
  Task 14 → Status states + show/hide
  Task 15 → Privacy warning + first-run marker
  Task 16 → main.py wire-up (replaces proto_main.py)
  Task 17 → README + setup docs
  Task 18 → (Optional) Local vision fallback

Phase 2 (Improvements — improvement1tasks.md):
  Task 1  → Telemetry logger
  Task 2  → SessionContext (passive state)
  Task 3  → Perception scaffolding
  Task 4  → UIA adapter rung
  Task 5  → OCR adapter rung
  Task 6  → Vision adapter rung
  Task 7  → Intent classifier
  Task 8  → Router orchestration
  Task 9  → Gemini context + streaming
  Task 10 → Escalation logic
  Task 11 → main.py rewire
  Task 12 → UI streaming
  Task 13-15 → Actions (click, type, etc.)
```

---

## Troubleshooting

| Issue | Cause | Fix |
|-------|-------|-----|
| **"No wake word triggers"** | Mic permissions, threshold too high | Check OS mic perms, lower `WAKEWORD_THRESHOLD` in config.py |
| **"Gemini API errors from Turkey"** | Geo-blocking or rate limits | Use Task 18 local fallback or check firewall |
| **"PyAudio install fails on Mac"** | Missing portaudio system lib | `brew install portaudio` before `pip install pyaudio` |
| **"Whisper is slow"** | First run downloads 150MB model | Expected; subsequent runs use cached model, ~1s |
| **"Region detection is poor"** | Canny edge threshold too aggressive | Tune `cv2.Canny(50, 150)` in `cv_pipeline.py` |
| **"Transcription returns empty string"** | Silent audio or low volume | Increase mic input level, lower `SILENCE_THRESHOLD_RMS` |

---

## Performance Targets (MVP)

- **End-to-end latency:** < 5 seconds wake → answer (target)
- **Wake word CPU:** ~ 1-2% idle
- **Memory footprint:** ~400MB (Whisper model in RAM, CV buffers)
- **API cost:** ~$0.10 per query average (Gemini Flash)

---

## File Purpose Reference

### Core Files (Must Understand)

| File | Purpose | Lines | Key Functions |
|------|---------|-------|---|
| `config.py` | All constants, env loader | ~100 | `load_dotenv()`, constants |
| `main.py` | App entry, thread orchestration | ~150 | `JarvisApp.run()`, lifecycle |
| `ui.py` | Chat window, event queue | ~300 | `JarvisWindow`, thread-safe queue |
| `wake_word.py` | Background listener | ~100 | `WakeWordListener`, daemon thread |
| `voice.py` | Audio recording | ~80 | `record_until_silence()`, RMS detection |
| `transcription.py` | Whisper wrapper | ~50 | `transcribe_pcm()`, lazy load |
| `capture.py` | Screenshot via mss | ~30 | `capture_primary_monitor()` |
| `gemini.py` | Gemini API client | ~120 | `ask()`, error handling, prompt construction |
| `cv_pipeline.py` | CV pipeline | ~250 | `CVPipeline.run()`, ROI, segmentation, change detection |

### Perception Layer (Understanding Routing)

| File | Purpose | Key Abstraction |
|------|---------|---|
| `perception.py` | Orchestrates adapters | `Perception.run(question, context)` → answer |
| `router.py` | Routes to adapter | `route(question, context)` → adapter_name |
| `perception_policy.py` | Policy engine | Rules for adapter selection |
| `adapters/*.py` | Pluggable data sources | Each has `query(target) → answer` signature |

### State & Telemetry (Understanding Context)

| File | Purpose | Key |
|------|---------|-----|
| `session_context.py` | Passive session state | `SessionContext` dataclass, accumulates per-query |
| `world_state.py` | Global state (app, mouse) | `WorldState`, updated between queries |
| `telemetry.py` | JSONL query logging | `log_query(record)`, `read_recent(n)` |

### Actions (Implementing Desktop Control)

| File | Purpose | Status |
|------|---------|--------|
| `actions.py` | Safe action execution | Work in progress (Task 13-15 in improvement1) |
| `set_of_marks.py` | "Click mark 5" interface | Tracks selectable regions |

---

## Key Concepts

### **Perception Rungs**

A "rung" is one level of perception complexity:
1. **UIA rung** — Fast, free (Windows Accessibility API)
2. **OCR rung** — Medium (text extraction)
3. **Vision rung** — Expensive (Gemini, ~1-3s, ~$0.0001 per query)

The router tries to answer with the cheapest rung first.

### **Intent Classification**

Before routing, the system classifies the query intent:
- `"read_text"` → UIA or OCR
- `"error_diagnosis"` → likely vision
- `"click_button"` → CV (mark extraction)
- `"what_on_screen"` → vision

### **Change Detection**

The CV pipeline tracks frame-to-frame changes to tell Gemini what's new:
- First capture: `changed_regions = ["initial_capture"]`
- Second capture: `changed_regions = ["content"]` (if user scrolled)
- This lets Gemini reason about *what changed*, not just what exists

### **Thread Safety**

- **Main thread:** CustomTkinter UI (required by Tk)
- **Wake thread:** openWakeWord listener (daemon, background)
- **Worker thread:** spawned per query, runs: record → transcribe → CV → API → queue event

**Critical rule:** Background threads MUST NOT touch Tk widgets. All updates go through `ui.post(event_type, payload)`.

---

## Future Roadmap

Based on `improvement*.md` files:

| Phase | Focus | Tasks | Timeline |
|-------|-------|-------|----------|
| **Phase 1 (MVP)** | Voice + Screen + Gemini | 1-18 | Complete |
| **Phase 2** | Router + Telemetry | improvement1 (1-15) | In progress |
| **Phase 3** | Local action capability | improvement2+ (actions) | Planned |
| **Phase 4** | Multi-monitor + system tray | improvement3-9 | Future |
| **Phase 5** | Persistent memory + accounts | Out of scope | Not planned |

---

## Getting Help

1. **Check JARVIS_SPEC.md** — Original MVP specification, frozen reference
2. **Check TASKS.md** — MVP implementation tasks with prompts
3. **Check improvement*.md** — Feature phases with detailed breakdowns
4. **Run tests:** Each module is independently testable via `python jarvis/<module>.py`
5. **Debug:** Enable `config.DEBUG_TRACE = True` for detailed logs

---

## License

MIT (or as specified in LICENSE file)

---

**Last Updated:** 2026-06-17  
**MVP Status:** Feature-complete  
**Current Phase:** Improvements (Router + Telemetry)
