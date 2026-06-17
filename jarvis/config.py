"""All constants and environment variable loading for Jarvis."""

from pathlib import Path
from dotenv import load_dotenv
import os

BASE_DIR = Path(__file__).parent.resolve()
load_dotenv(BASE_DIR / ".env")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# Models
GEMINI_MODEL = "gemini-2.5-flash"

# Local VLM — LOCAL-FIRST deployment (RTX 3060 Laptop, 6 GB VRAM).
# MOONDREAM_MODEL is the symbol that local_vision.py and vision_adapter._vlm_moondream read;
# it is repointed to the strong VLM so those call-sites route to Qwen2.5-VL without renaming.
# A later task may rename the symbol to LOCAL_VLM_MODEL throughout.
LOCAL_VLM_MODEL          = "qwen2.5vl:7b"   # strong local VLM: screen/UI/color/shape/grounding
LOCAL_VLM_FALLBACK_MODEL = "moondream"       # fast degraded fallback if strong VLM errors/unloads
MOONDREAM_MODEL          = LOCAL_VLM_MODEL   # back-compat alias — repointed from "moondream" to Qwen-VL

WHISPER_MODEL = "base"
WHISPER_LANGUAGE = "en"

# Audio
SAMPLE_RATE = 16000
CHANNELS = 1
CHUNK_SIZE = 1024
SILENCE_THRESHOLD_RMS = 500
SILENCE_DURATION_SEC = 1.5
MAX_RECORDING_SEC = 30

# Wake word
WAKEWORD_MODEL = "hey_jarvis_v0.1"
WAKEWORD_THRESHOLD = 0.5
WAKEWORD_CHUNK_SIZE = 1280   # 80ms at 16kHz — openWakeWord's recommended frame

# API
GEMINI_TIMEOUT_SEC = 30

# CV
MIN_CONTOUR_AREA_RATIO = 0.005     # 0.5% of frame area
TOOLBAR_Y_RATIO = 0.10
STATUSBAR_Y_RATIO = 0.85
CHANGE_DIFF_THRESHOLD = 30

# UIA
UIA_MAX_DEPTH = 8    # raised from 6: content nodes in Electron/UWP/rich Win32 sit deeper in the tree
UIA_MAX_NODES = 400  # raised from 150: modern apps have 300–1000+ nodes; old cap stopped the walker at chrome

# Fusion
FUSE_IOU = 0.6                  # IoU threshold for merging elements from different adapters

# OCR
OCR_ENGINE = "tesseract"
TESSERACT_PATH = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
OCR_MIN_CONF = 0.25             # raised from 0.4: matches OCR_MIN_CONF_CONTENT; stricter floor silently dropped dark-mode/small-font lines
OCR_SCALE = 3.5                 # raised from 2.5: matches CONTENT_REOCR_SCALE; 2.5× was borderline for 12px fonts at 100% DPI
READ_REGION_SCALE = 3.5         # higher upscale for read_region() — affordable because the crop is small
OCR_PSM = 11                    # changed from 6 (uniform block): PSM=11 (sparse text) is correct for mixed/multi-column UI screens
OCR_DARK_THRESHOLD = 128        # mean luminance below this → invert image before OCR (dark-mode UIs)

# Context
SCREEN_READ_TTL = 3              # reduced from 8: 3s is enough to avoid redundant reads within one interaction but short enough that a changed context triggers a fresh read
BROWSER_SCREEN_READ_TTL = 1.0   # tighter TTL for CHROMIUM_ELECTRON / UWP (SPA navigations skip StructureChanged)
                                 # TODO (P12/F2): supersede with CDP Page.getNavigationHistory exact URL key (Task 6)

# UI
WINDOW_WIDTH = 480
WINDOW_HEIGHT = 600

# Privacy
PRIVACY_MARKER_FILENAME = ".jarvis_seen"

# Telemetry
TELEMETRY_PATH = Path.home() / ".jarvis" / "telemetry.jsonl"

# Actions
ACTIONS_ENABLED = True
ALLOWED_ACTIONS = ["open_app", "set_clipboard", "notify", "click_element"]
DRY_RUN = False
KILL_HOTKEY = "ctrl+alt+esc"
APP_WHITELIST: dict[str, str] = {
    "notepad":    "notepad.exe",
    "calculator": "calc.exe",
    "explorer":   "explorer.exe",
    "chrome":     "start chrome",
    "firefox":    "start firefox",
    "vscode":     "code",
    "terminal":   "wt",
    "cmd":        "cmd.exe",
}

# Backend selection — LOCAL-FIRST: Gemini is opportunistic (used when a key is set AND the call
# succeeds). The local VLM (now Qwen2.5-VL via MOONDREAM_MODEL) is the default; Gemini is never
# required for a real answer to be produced.
VISION_BACKEND = "local"   # local-first; "gemini" would make Gemini the primary vision backend
VISION_IMAGE_CONF = 0.5    # attach screenshot when max element confidence falls below this

# Vision model selection for the VISION perception rung and SoM grounding.
# "auto"      — local VLM (MOONDREAM_MODEL = qwen2.5vl:7b) first; Gemini fallback only if a key
#               is set AND the local call fails. This is the correct setting for local-first.
# "moondream" — force local VLM only (same as auto but never tries Gemini).
# "gemini"    — Gemini multimodal only (cloud, requires key; not suitable for local-first).
VISION_MODEL = "auto"

# Local LLM — LOCAL-FIRST. qwen2.5:7b-instruct replaces mistral-nemo:12b.
# 7B 4-bit (~5 GB) fits within 6 GB VRAM and pairs with the VLM without both spilling to RAM.
# mistral-nemo:12b (7.1 GB) spills heavily and is too slow to pair with a VLM on 6 GB.
LOCAL_LLM_BACKEND = "ollama"
LOCAL_LLM_MODEL = "qwen2.5:7b-instruct"  # replaces mistral-nemo:12b; pull: ollama pull qwen2.5:7b-instruct
# LOCAL_LLM_MODEL = "qwen2.5:14b-instruct" # heavier option: spills ~3 GB to RAM on 6 GB card — not recommended as default
# LOCAL_LLM_MODEL = "mistral-nemo:12b"     # previous default; 7.1 GB, slow to pair with VLM on 6 GB
LOCAL_LLM_TIMEOUT_MS = 1500

# Router
ROUTER_MIN_CONF = 0.5       # classify confidence below this → bias to intent=TEXT
ESCALATE_CONF = 0.6         # max element confidence below this → escalate one rung deeper

# Action grounding
GROUND_CONF = 0.6           # min ScreenElement.calibrated_confidence required before acting on an element
GROUND_MARGIN = 0.15        # best score must exceed runner-up by at least this; else ambiguous

# Cache freshness
# roi_dhash is 16×16 = 256 bits; the cache key is (window_identity, roi_dhash).
# A hit requires Hamming ≤ CACHE_HAMMING_MAX AND density_delta ≤ CACHE_DENSITY_DELTA_MAX.
CACHE_HASH_SIZE = 16                # dHash grid size for cache key (bits = size²)
CACHE_HAMMING_MAX = 10              # 256-bit hash; ~4% bit-flip tolerance (native windows)
BROWSER_CACHE_HAMMING_MAX = 6      # tighter Hamming for CHROMIUM_ELECTRON/UWP: fewer feed-state collisions
                                    # TODO (P12/F2): supersede with CDP exact URL key (Task 6)
CACHE_DENSITY_DELTA_MAX = 0.04     # max edge-density change to still treat ROI as stable
HASH_HAMMING_MAX = 5                # legacy: post-click verification uses 8×8 dhash (64 bits)
CLICK_STALE_HAMMING = 4            # max bit-flip distance allowed before a SoM coord-click is aborted as stale
CURSOR_RADIUS_PX = 80              # fallback search radius for get_element_at_cursor when no bbox contains the point

MODEL_HISTORY_TURNS = 6             # how many past turns (individual entries) to include in the model prompt; session stores _MAX_TURNS=10 but only the last N go to the model (6 = 3 Q&A exchanges)
HISTORY_CONTENT_FLOOR_ELEMENTS = 5  # raised from 3: chrome/labels alone exceed 3; need 5 to require actual content elements (P12/F3)
HISTORY_CONTENT_FLOOR_CHARS = 200   # raised from 40: 40 chars fires only on blank screens; 200 requires a meaningful content block (P12/F3)
HISTORY_CROSS_WINDOW = "annotate"   # "annotate" | "drop" — how to handle turns recorded on a different window
FOLLOWUP_RECAPTURE_MS = 1500        # re-capture target on typed follow-up if hwnd changed OR elapsed > this
FOCUS_STATE_TTL_MS = 1500           # wake-time interaction state (cursor_pos / focused_element / selection_text) expires after this many ms
SETTLE_MIN_MS = 80                  # minimum settle pause after a UIA event fires (ms)
SETTLE_MAX_MS = 800                 # ceiling: fall back to timed re-read if no UIA event by this deadline (ms)

# Whisper transcription prompt
# Static app/proper-noun tokens prepended to every transcription prompt so Whisper
# base recognises them without needing to hear them from world_state.
TRANSCRIPTION_STATIC_APPS: tuple[str, ...] = (
    "Slack", "Teams", "Discord", "Zoom", "Notion", "Figma", "GitHub", "GitLab",
    "Jira", "Confluence", "VS Code", "Visual Studio", "PyCharm", "IntelliJ",
    "Chrome", "Firefox", "Edge", "Safari", "Spotify", "Outlook", "Excel", "Word",
    "PowerPoint", "OneNote", "Notepad", "Terminal", "PowerShell", "Obsidian",
)

# Content-region salience (P8)
# Geometric heuristic for CHROMIUM_ELECTRON / UWP browser chrome subtraction.
CHROME_TOP_BAND_PX = 124            # top chrome band height at 100% DPI (tab strip + omnibox + bookmarks)
CHROME_SIDE_PANEL_MAX_FRAC = 0.15   # columns narrower than this fraction of window width are candidate side panels

# Content-region re-OCR (P9)
CONTENT_REOCR = True                # master flag; set False to disable the second pass entirely
CONTENT_REOCR_SCALE = 3.5           # upscale factor for the content-region crop (same as READ_REGION_SCALE)
OCR_PSM_CONTENT = 11                # sparse-text PSM for social/feed layouts; full-window pass keeps OCR_PSM=6

# Softened OCR thresholds for content-region pass only (P10)
# Full-window pass keeps the strict values (OCR_MIN_CONF=0.4, token floor=30).
# A low-confidence line inside the content area is far more valuable than a
# high-confidence chrome string; calibration + salience weighting arbitrate downstream.
OCR_MIN_CONF_CONTENT = 0.25         # mean-token confidence floor for content-region re-OCR
OCR_TOKEN_CONF_MIN_CONTENT = 15     # per-token floor for content-region re-OCR (vs. 30 full-window)

THIN_TEXT_CHAR_FLOOR = 80    # fewer chars than this → "thin" read → auto-escalate
THIN_TEXT_ELEM_FLOOR = 3     # fewer content elements than this → "thin" read

# G1E: post-OCR VISION fallback — fires at most once per run_ladder when UIA+OCR both came back
# thin on a hard-to-read app class. Runs ask_vlm(ask_elements=True) and attaches the screenshot.
VISION_THIN_FALLBACK = True
VISION_THIN_FALLBACK_APP_CLASSES = ("chromium_electron", "game_fullscreen", "unknown", "uwp")

PREFER_LOCAL_NO_CONTEXT = False     # answer NO_CONTEXT queries from local LLM, skip Gemini entirely
PREFER_LOCAL_STRUCTURE = True       # fast-path for pure read-back only (summarise/read-aloud);
                                    # reasoning/judgement queries bypass this via _is_reasoning_or_judgement_query
                                    # and flow to the full answering path (local-first, Gemini opportunistic)
LOCAL_ANSWER_TIMEOUT_MS = 40_000    # raised from 25_000: 6 GB card + VLM/LLM swap load cost on first call
CHROME_OMNIBOX_URL_FALLBACK = True  # fall back to Chrome_OmniboxView child-window text when UIA URL walk fails

# Adapter reliability table  — calibrated_confidence = raw * ADAPTER_RELIABILITY[(source, app_class)]
# Keys: (adapter_source, AppClass.value).  Fallback key (source, None) used when app_class is unknown.
# Values are multipliers in [0.0, 1.0].
ADAPTER_RELIABILITY: dict[tuple[str, str | None], float] = {
    # UIA is authoritative for native Win32 but nearly useless for Electron/games
    ("uia", "native_win32"):        1.0,
    ("uia", "chromium_electron"):   0.4,
    ("uia", "uwp"):                 0.8,
    ("uia", "java_swing"):          0.7,
    ("uia", "game_fullscreen"):     0.1,
    ("uia", "unknown"):             0.7,
    ("uia", None):                  0.7,
    # OCR raw confidence already reflects recognition quality; multiply by 0.8 universal discount
    ("ocr", "native_win32"):        0.8,
    ("ocr", "chromium_electron"):   0.8,
    ("ocr", "uwp"):                 0.8,
    ("ocr", "java_swing"):          0.8,
    ("ocr", "game_fullscreen"):     0.8,
    ("ocr", "unknown"):             0.8,
    ("ocr", None):                  0.8,
    # CV provides layout anchors only; low reliability for text/action purposes
    ("cv",  "native_win32"):        0.2,
    ("cv",  "chromium_electron"):   0.2,
    ("cv",  "uwp"):                 0.2,
    ("cv",  "java_swing"):          0.2,
    ("cv",  "game_fullscreen"):     0.2,
    ("cv",  "unknown"):             0.2,
    ("cv",  None):                  0.2,
    # Vision model output reliability depends on model quality:
    # moondream (VISION_MODEL="moondream") is approximate — downgraded to 0.5.
    # Gemini multimodal is more reliable — would be ~0.8, but we use 0.7 as a
    # conservative shared baseline (the router doesn't know which model ran).
    ("vision", "native_win32"):     0.7,
    ("vision", "chromium_electron"): 0.7,
    ("vision", "uwp"):              0.7,
    ("vision", "java_swing"):       0.7,
    ("vision", "game_fullscreen"):  0.7,
    ("vision", "unknown"):          0.7,
    ("vision", None):               0.7,
    # Per-model overrides (keyed by (source_tag, app_class)):
    # When VISION_MODEL="moondream", perception.py tags source="moondream".
    ("moondream", "native_win32"):     0.5,
    ("moondream", "chromium_electron"): 0.5,
    ("moondream", "uwp"):              0.5,
    ("moondream", "java_swing"):       0.5,
    ("moondream", "game_fullscreen"):  0.5,
    ("moondream", "unknown"):          0.5,
    ("moondream", None):               0.5,
}

# Model-requested escalation
MAX_MODEL_ESCALATIONS = 2           # hard cap on perception-escalation tool calls per query
MAX_FOCUS_TOOL_CALLS  = 2           # hard cap on focus-resolution tool calls per query

# Debug
DEBUG_OVERLAY = False               # save element bbox overlays to ~/.jarvis/debug/overlay_*.png
LOG_LEVEL = os.getenv("JARVIS_LOG_LEVEL", "DEBUG")

# Startup validation
_VALID_VISION_BACKENDS = {"local", "gemini"}
if VISION_BACKEND not in _VALID_VISION_BACKENDS:
    raise ValueError(
        f"VISION_BACKEND={VISION_BACKEND!r} is invalid. "
        f"Must be one of: {', '.join(sorted(_VALID_VISION_BACKENDS))}"
    )
