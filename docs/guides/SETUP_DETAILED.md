# Jarvis

Jarvis is a voice-activated, screen-aware desktop AI agent that listens for a wake word, captures the active window, runs a lightweight CV pipeline, and asks Gemini for a concise answer based on what you see.

![demo](demo.gif) <!-- TODO: record demo -->

## Requirements

- Python 3.10+
- Windows primary (macOS compatible)
- Google AI Studio account (Gemini API key)

## Setup

### Prerequisites

- **Python 3.10 or higher**
- **Windows** (primary): Windows 10 / 11
- **macOS** (supported): macOS 10.14+
- **Internet connection** (first run downloads ~175MB of models)

### Step 1: Clone and Navigate

```bash
git clone <repo-url>
cd comp_vision_project
```

### Step 2: Create and Activate Virtual Environment

**Windows:**
```bash
python -m venv .venv
.venv\Scripts\activate
```

**macOS / Linux:**
```bash
python3 -m venv .venv
source .venv/bin/activate
```

### Step 3: Install System Dependencies

**macOS only** (required before Python packages):
```bash
brew install portaudio
```

**Windows** (optional, for OCR fallback):
- Download Tesseract: https://github.com/UB-Mannheim/tesseract/wiki
- Install to: `C:\Program Files\Tesseract-OCR\`
- Verify path in `config.py` line 57

### Step 4: Install Python Dependencies

```bash
pip install -r jarvis/requirements.txt
```

⏱️ **Expected time:** 2-5 minutes (depending on internet speed)

### Step 5: Get Gemini API Key

1. Visit: https://aistudio.google.com
2. Sign in with Google account (or create one)
3. Click "Create API key"
4. Copy the key

### Step 6: Configure Environment

```bash
cp jarvis/.env.example jarvis/.env
```

Then edit `jarvis/.env` and add your API key:
```env
GEMINI_API_KEY=your_api_key_here
```

### Step 7: Run Jarvis

```bash
python jarvis/main.py
```

**First run:** 
- Downloads openWakeWord model (~25MB)
- Downloads Whisper model (~150MB)
- Expected wait: 2-5 minutes (one-time only)
- Subsequent runs: ~1 second startup

**On first launch:** A privacy warning modal will appear. Read and dismiss it.

## Usage

Say "Jarvis" and ask a question about what is on screen.

## Architecture

Jarvis is built from five components: wake word listener, audio recording + transcription, screen capture, CV pipeline, and Gemini inference. See JARVIS_SPEC.md for the full architecture and rationale.

## Project Structure

### Entry Point & Configuration
```
jarvis/
├── main.py              # Application entry point, event bus, session lifecycle
├── config.py            # All constants and .env loader (single source of truth)
├── requirements.txt     # Python dependencies (17 packages)
├── .env.example         # Template for secrets (GEMINI_API_KEY)
├── .env                 # Production secrets (gitignored)
└── .gitignore           # Git exclusions
```

### Voice Pipeline
```
├── wake_word.py         # openWakeWord listener (background thread)
├── voice.py             # PyAudio recording + silence detection
└── transcription.py     # Whisper speech-to-text (local, offline)
```

### Screen Capture & CV
```
├── capture.py           # mss screenshot capture (cross-platform)
└── cv_pipeline.py       # ROI detection, segmentation, change detection
```

### Core Logic
```
├── perception.py        # Orchestrates perception adapters
├── router.py            # Routes queries to appropriate adapter
├── classify.py          # Intent classification utilities
└── perception_policy.py # Policy for adapter selection
```

### Perception Adapters (Pluggable)
```
├── adapters/
│   ├── uia_adapter.py       # Windows Accessibility tree navigation
│   ├── ocr_adapter.py       # Text extraction (Tesseract/EasyOCR)
│   ├── selection_adapter.py # Recently copied text (clipboard)
│   ├── cv_adapter.py        # Region boundaries from CV pipeline
│   └── vision_adapter.py    # Gemini or local vision models
└── core/
    ├── events.py            # Event definitions (EventBus)
    └── session_actor.py     # Session lifecycle state machine
```

### LLM Backends
```
├── gemini.py            # Google Gemini API client (primary)
├── local_vision.py      # Local vision model (Ollama fallback)
└── local_llm.py         # Local LLM backend (offline reasoning)
```

### State & Telemetry
```
├── session_context.py   # Per-session state (query history, app context)
├── world_state.py       # Global state (current app, mouse position)
├── screen_model.py      # Screen abstraction and state
├── telemetry.py         # JSONL query logging (~/.jarvis/telemetry.jsonl)
└── trace.py             # Query tracing and debugging
```

### UI & Actions
```
├── ui.py                # CustomTkinter chat window (thread-safe event queue)
├── privacy.py           # First-run privacy warning modal
├── actions.py           # Safe desktop automation (click, type, etc.)
└── set_of_marks.py      # UI mark extraction ("click mark 5")
```

### Utilities
```
├── focus.py             # Window focus tracking
├── focus_resolver.py    # Cross-platform focus detection
├── app_classifier.py    # Identify current application
├── calibration.py       # Multi-monitor calibration
├── debug_overlay.py     # Debug visualization on screen
├── uia_watcher.py       # UIA event monitoring
├── fusion.py            # Sensor fusion (combine data sources)
├── logging_setup.py     # Logging configuration
└── gen_frames.py        # Streaming frame generation
```

## Privacy

Jarvis captures your screen on wake word. See JARVIS_SPEC.md section 7 for the full privacy note and retention details.

## Troubleshooting

### "No wake word triggers" or window doesn't appear when I say "Jarvis"

**Cause:** Microphone permissions, threshold too high, or mic is muted.

**Fix:**
1. Check OS microphone permissions:
   - **Windows:** Settings → Privacy & Security → Microphone → Allow apps to access microphone
   - **macOS:** System Preferences → Security & Privacy → Microphone
2. Test microphone:
   ```bash
   python jarvis/wake_word.py
   # Say "Hey Jarvis" multiple times, watch for "Wake word detected!"
   ```
3. If still not working, lower threshold in `jarvis/config.py` line 36:
   ```python
   WAKEWORD_THRESHOLD = 0.3  # More sensitive (default is 0.5)
   ```
4. Ensure first-run model download completed (check internet connection)

### "PyAudio install fails on macOS" or "error: 'portaudio.h' file not found"

**Fix:** Install portaudio BEFORE pip install:
```bash
brew install portaudio
pip install --force-reinstall pyaudio
```

Then try installing requirements again:
```bash
pip install -r jarvis/requirements.txt
```

### "Gemini API errors" or "Cannot reach Gemini — check connection"

**Cause:** API key invalid, network issues, or API downtime (especially from Turkey).

**Fix:**
1. Verify API key in `jarvis/.env`:
   ```bash
   cat jarvis/.env  # Windows: type jarvis\.env
   ```
2. Check key is not empty or malformed
3. Regenerate key at https://aistudio.google.com
4. For reliable offline operation, use local vision fallback (requires Ollama):
   ```bash
   # Install Ollama from https://ollama.com
   ollama pull qwen2.5vl:7b
   
   # In config.py, change:
   VISION_BACKEND = "local"
   ```

### "Whisper is slow" or "ModuleNotFoundError: No module named 'whisper'"

**Cause:** First run downloads ~150MB Whisper model, or package not installed.

**Fix:**
- First run is expected to take 2-5 minutes (model downloads to `~/.cache/whisper/`)
- Subsequent runs use the cached model (~1 second)
- If "module not found" error:
  ```bash
  pip install --upgrade openai-whisper
  ```

### "No such file or directory: config.py"

**Cause:** Running from wrong directory.

**Fix:** Always run from repo root:
```bash
cd comp_vision_project
python jarvis/main.py
```

### "GEMINI_API_KEY is not set in .env"

**Cause:** `.env` file not created or key not filled in.

**Fix:**
```bash
# Verify file exists
ls jarvis/.env  # Windows: dir jarvis\.env

# Verify key is set
grep GEMINI_API_KEY jarvis/.env

# If empty, edit and fill in:
nano jarvis/.env  # or use your editor
```

### "Window appears but no response when I speak"

**Cause:** Transcription failed, question is empty, or API rate limit.

**Fix:**
1. Speak clearly and wait for silence
2. Check console for error messages
3. Verify internet connection
4. If rate-limited (many queries in short time), wait a minute and try again

## License

MIT
