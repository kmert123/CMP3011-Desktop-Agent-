"""One-off script: generate synthetic starter frame fixtures for the eval harness.

Run from the jarvis/ directory:
    python eval/gen_frames.py
"""
import json
import cv2
import numpy as np
from pathlib import Path

FRAMES_DIR = Path(__file__).parent / "cases" / "frames"
FRAMES_DIR.mkdir(parents=True, exist_ok=True)


def save(name: str, img: np.ndarray, meta: dict) -> None:
    cv2.imwrite(str(FRAMES_DIR / f"{name}.png"), img)
    (FRAMES_DIR / f"{name}.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    print(f"  {name}.png  {img.shape}")


H, W = 768, 1280

# ------------------------------------------------------------------
# Frame 1: dark-mode browser page
# ------------------------------------------------------------------
f1 = np.zeros((H, W, 3), dtype=np.uint8)
f1[:] = (30, 30, 30)

cv2.rectangle(f1, (0, 0), (W, 60), (45, 45, 45), -1)
cv2.rectangle(f1, (160, 10), (W - 200, 50), (58, 58, 58), -1)
cv2.putText(f1, "https://example.com/docs/getting-started", (170, 38),
            cv2.FONT_HERSHEY_SIMPLEX, 0.55, (200, 200, 200), 1, cv2.LINE_AA)
cv2.putText(f1, "Getting Started", (80, 140),
            cv2.FONT_HERSHEY_SIMPLEX, 1.4, (240, 240, 240), 2, cv2.LINE_AA)
cv2.putText(f1, "Installation", (80, 220),
            cv2.FONT_HERSHEY_SIMPLEX, 0.9, (200, 200, 200), 1, cv2.LINE_AA)
for i, line in enumerate([
    "Install the package using pip:",
    "pip install jarvis-agent",
    "Then run the setup wizard:",
    "python -m jarvis setup",
]):
    cv2.putText(f1, line, (80, 270 + i * 38),
                cv2.FONT_HERSHEY_SIMPLEX, 0.6, (180, 180, 180), 1, cv2.LINE_AA)

save("frame_dark_browser", f1, {
    "process": "chrome", "app_class": "BROWSER",
    "bounds": [0, 0, W, H], "origin": [0, 0], "dpi_scale": 1.0,
    "golden": {
        "required_substrings": ["Getting Started", "Installation", "pip install"],
        "max_fragment_phrases": [
            {"phrase": "Getting Started", "max_fragments": 1},
            {"phrase": "pip install", "max_fragments": 1},
        ],
    },
})

# ------------------------------------------------------------------
# Frame 2: sidebar + content layout (VS Code-style)
# ------------------------------------------------------------------
f2 = np.ones((H, W, 3), dtype=np.uint8) * 240
cv2.rectangle(f2, (0, 0), (200, H), (50, 50, 50), -1)
for i, item in enumerate(["EXPLORER", "SEARCH", "SOURCE CONTROL", "EXTENSIONS"]):
    cv2.putText(f2, item, (10, 80 + i * 50),
                cv2.FONT_HERSHEY_SIMPLEX, 0.42, (200, 200, 200), 1, cv2.LINE_AA)
for i, fn in enumerate(["jarvis/", "  config.py", "  gemini.py", "  router.py", "  fusion.py"]):
    cv2.putText(f2, fn, (10, 280 + i * 28),
                cv2.FONT_HERSHEY_SIMPLEX, 0.38, (210, 210, 210), 1, cv2.LINE_AA)
cv2.rectangle(f2, (200, 0), (W, H), (30, 30, 30), -1)
cv2.rectangle(f2, (200, 0), (W, 30), (45, 45, 45), -1)
cv2.putText(f2, "router.py", (215, 22),
            cv2.FONT_HERSHEY_SIMPLEX, 0.5, (200, 200, 200), 1, cv2.LINE_AA)
for i, cl in enumerate([
    "from classify import classify_intent",
    "from app_classifier import AppClass",
    "",
    "def route(query: str, target) -> RouteResult:",
    "    result = classify_intent(query)",
    "    act = result.act",
    "    perception = result.perception",
    "    rung = entry_rung_for(perception, target.app_class)",
    "    return RouteResult(act=act, rung=rung)",
]):
    cv2.putText(f2, cl, (220, 70 + i * 30),
                cv2.FONT_HERSHEY_SIMPLEX, 0.44, (180, 220, 160), 1, cv2.LINE_AA)

save("frame_sidebar_editor", f2, {
    "process": "code", "app_class": "ELECTRON",
    "bounds": [0, 0, W, H], "origin": [0, 0], "dpi_scale": 1.0,
    "golden": {
        "required_substrings": ["EXPLORER", "router.py", "classify_intent", "entry_rung_for"],
        "max_fragment_phrases": [
            {"phrase": "classify_intent", "max_fragments": 1},
            {"phrase": "entry_rung_for", "max_fragments": 1},
        ],
    },
})

# ------------------------------------------------------------------
# Frame 3: dense native window (task manager-style)
# ------------------------------------------------------------------
f3 = np.ones((H, W, 3), dtype=np.uint8) * 245
cv2.rectangle(f3, (0, 0), (W, 40), (0, 120, 212), -1)
cv2.putText(f3, "Task Manager", (10, 27),
            cv2.FONT_HERSHEY_SIMPLEX, 0.65, (255, 255, 255), 1, cv2.LINE_AA)
cv2.rectangle(f3, (0, 40), (W, 70), (230, 230, 230), -1)
cols = ["Name", "CPU", "Memory", "Disk", "Network", "GPU"]
col_xs = [10, 280, 370, 460, 550, 640]
for col, cx in zip(cols, col_xs):
    cv2.putText(f3, col, (cx, 62), cv2.FONT_HERSHEY_SIMPLEX, 0.48, (0, 0, 0), 1, cv2.LINE_AA)
processes = [
    ("chrome.exe",       "12.4%", "1,240 MB", "0.1 MB/s", "5.2 Mbps", "2%"),
    ("code.exe",         " 3.1%",   "540 MB", "0.0 MB/s", "0.0 Mbps", "1%"),
    ("python.exe",       " 8.7%",   "320 MB", "1.2 MB/s", "0.1 Mbps", "0%"),
    ("System",           " 0.5%",    "60 MB", "0.0 MB/s", "0.0 Mbps", "0%"),
    ("Windows Security", " 0.2%",   "180 MB", "0.0 MB/s", "0.0 Mbps", "0%"),
    ("Discord",          " 1.4%",   "420 MB", "0.0 MB/s", "2.1 Mbps", "0%"),
    ("explorer.exe",     " 0.8%",   "200 MB", "0.0 MB/s", "0.0 Mbps", "0%"),
]
for row_i, row in enumerate(processes):
    y = 90 + row_i * 32
    bg = (255, 255, 255) if row_i % 2 == 0 else (245, 245, 245)
    cv2.rectangle(f3, (0, y - 18), (W, y + 14), bg, -1)
    for val, cx in zip(row, col_xs):
        cv2.putText(f3, val, (cx, y), cv2.FONT_HERSHEY_SIMPLEX, 0.44, (40, 40, 40), 1, cv2.LINE_AA)
cv2.rectangle(f3, (720, 80), (W - 20, 300), (220, 220, 220), -1)
cv2.putText(f3, "CPU usage  12%", (730, 105),
            cv2.FONT_HERSHEY_SIMPLEX, 0.48, (0, 0, 0), 1, cv2.LINE_AA)

save("frame_dense_native", f3, {
    "process": "taskmgr", "app_class": "NATIVE",
    "bounds": [0, 0, W, H], "origin": [0, 0], "dpi_scale": 1.0,
    "golden": {
        "required_substrings": ["Task Manager", "Name", "CPU", "Memory", "chrome.exe", "python.exe"],
        "max_fragment_phrases": [
            {"phrase": "Task Manager", "max_fragments": 1},
            {"phrase": "CPU usage", "max_fragments": 1},
        ],
    },
})

print("Done.")
