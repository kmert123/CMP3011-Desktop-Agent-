# Jarvis — Implementation Tasks

This file is the build plan. Execute tasks **in order**. For each task:

1. Read the "Goal" and "Files" sections.
2. Copy the prompt inside the ` ```prompt ` block.
3. Paste it into Claude Code (in VSCode).
4. After it runs, run the "Verify" step yourself.
5. Don't move on until verify passes.

`@JARVIS_SPEC.md` references in prompts mean Claude Code should read that file. Make sure `JARVIS_SPEC.md` is in the same repo so Claude Code can find it.

---

## Phase 1 — Foundations

### Task 1: Project setup

**Goal:** Skeleton directory, configs, no app logic.
**Files:** `jarvis/` (folder), `requirements.txt`, `.env.example`, `.gitignore`, `config.py`, `README.md` stub.
**Depends on:** nothing.

```prompt
Read @JARVIS_SPEC.md sections 8, 9, and 10.

Create the project skeleton at the repo root:

1. Create a `jarvis/` directory and `cd` into it for all subsequent files.
2. Create these empty Python files, each with a one-line module docstring:
   main.py, config.py, wake_word.py, voice.py, transcription.py,
   capture.py, cv_pipeline.py, gemini.py, ui.py
3. Create `requirements.txt` with the EXACT contents from spec section 9.
4. Create `.env.example` with the two keys from section 10, values empty.
5. Create `.gitignore` covering: .env, __pycache__/, *.pyc, .venv/, venv/, .jarvis_seen
6. Create `config.py` with all constants from spec section 10. At the top:
     from pathlib import Path
     from dotenv import load_dotenv
     import os
     BASE_DIR = Path(__file__).parent.resolve()
     load_dotenv(BASE_DIR / ".env")
     GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
   Then all the constants from section 10.
7. Stub `README.md`: project name + one-line description. We fill it in Task 17.

Do NOT pip install anything — I'll do that manually.
Print the resulting `tree jarvis/` at the end.
```

**Verify:** `cd jarvis && python -c "import config; print(config.GEMINI_MODEL)"` prints `gemini-2.5-flash`.

---

### Task 2: Gemini API client

**Goal:** `gemini.py` works standalone — give it an image + question, get an answer.
**Files:** `gemini.py`.
**Depends on:** Task 1. You must have `GEMINI_API_KEY` set in `.env` and `pip install -r requirements.txt` done.

```prompt
Read @JARVIS_SPEC.md sections 4.1, 4.3, 4.4, 4.5, and 0 (coding conventions).

Implement `jarvis/gemini.py`:

1. Public function:
     def ask(image: "np.ndarray", question: str, context: dict | None = None) -> str
   - image: BGR numpy array (as returned by mss or cv2)
   - question: transcribed user question (str)
   - context: dict with keys "active_window", "regions", "changed_regions"
       (all optional — if None or missing keys, omit those lines from prompt)
   - returns: Gemini's text response

2. Inside `ask`:
   - Convert BGR → RGB with cv2.cvtColor
   - Wrap in PIL.Image.fromarray
   - Use google.generativeai (import as genai)
   - genai.configure(api_key=config.GEMINI_API_KEY)
   - If GEMINI_API_KEY is None or empty, raise RuntimeError with a clear message
   - Build the system prompt and user message text exactly as in spec 4.3
   - Use config.GEMINI_MODEL for the model name
   - Send as a single multimodal request: [system_prompt, user_message, pil_image]
   - Apply config.GEMINI_TIMEOUT_SEC (use request_options={"timeout": ...} or wrap in concurrent.futures)

3. Error handling — wrap the API call in try/except and return user-facing strings per spec 4.4:
   - google.api_core.exceptions.ResourceExhausted → "Rate limit reached — wait a moment."
   - TimeoutError or concurrent.futures.TimeoutError → "Request timed out — try again."
   - google.api_core.exceptions.PermissionDenied / Unauthenticated → "Gemini API key invalid — check .env"
   - Any ConnectionError / network exception → "Cannot reach Gemini — check connection."
   - response.text empty/None → "No response received — please try again."
   - Generic Exception (last resort) → "Unexpected error: {type name}"

4. Add `if __name__ == "__main__":` block that:
   - Takes a screenshot of the primary monitor with mss
   - Calls ask(screenshot, "What am I looking at right now?", context=None)
   - Prints the response

Type hints on the public function. No comments unless logic is non-obvious.
```

**Verify:** `python jarvis/gemini.py` prints a sensible 3-4 sentence description of your screen.

---

### Task 3: Screenshot capture

**Goal:** `capture.py` — clean wrapper around mss returning a BGR numpy array.
**Files:** `capture.py`.
**Depends on:** Task 1.

```prompt
Read @JARVIS_SPEC.md section 0.

Implement `jarvis/capture.py`:

1. Public function:
     def capture_primary_monitor() -> "np.ndarray"
   - Uses mss to grab the primary monitor (index 1 in mss.monitors)
   - Converts mss output to a BGR numpy array (mss returns BGRA — drop alpha)
   - Returns the array

2. `if __name__ == "__main__":` saves the screenshot to `/tmp/jarvis_capture_test.png` (or
   `%TEMP%/jarvis_capture_test.png` on Windows — use tempfile.gettempdir()) using cv2.imwrite
   so I can eyeball it. Print the path and array shape.

Keep it under 30 lines. No comments.
```

**Verify:** `python jarvis/capture.py` saves a screenshot and prints shape like `(1080, 1920, 3)`.

---

### Task 4: Whisper transcription

**Goal:** `transcription.py` — transcribe a WAV file or raw PCM bytes.
**Files:** `transcription.py`.
**Depends on:** Task 1.

```prompt
Read @JARVIS_SPEC.md sections 5.3 and 0.

Implement `jarvis/transcription.py`:

1. Module-level lazy-loaded Whisper model:
     _model = None
     def _get_model():
         global _model
         if _model is None:
             import whisper
             _model = whisper.load_model(config.WHISPER_MODEL)
         return _model

2. Public function:
     def transcribe_pcm(pcm_bytes: bytes, sample_rate: int = config.SAMPLE_RATE) -> str
   - Converts 16-bit PCM bytes to a float32 numpy array in [-1, 1]
   - If sample_rate != 16000, resample (use scipy or librosa — but prefer just enforcing 16000 from PyAudio side)
   - Calls _get_model().transcribe(audio_array, language=config.WHISPER_LANGUAGE, fp16=False)
   - Returns the "text" field, stripped

3. `if __name__ == "__main__":`
   - Record 5 seconds from default mic with PyAudio at config.SAMPLE_RATE
   - Pass bytes to transcribe_pcm
   - Print the transcript
   - This block can import pyaudio locally inside the if

Type hints. No comments.
```

**Verify:** `python jarvis/transcription.py`, speak a sentence, see it printed back. (First run downloads ~150MB Whisper model — expected.)

---

## Phase 2 — Voice loop

### Task 5: Audio recording with silence detection

**Goal:** `voice.py` — record from mic, stop when user stops talking.
**Files:** `voice.py`.
**Depends on:** Task 1, Task 4 (uses same audio format).

```prompt
Read @JARVIS_SPEC.md sections 5.2 and 0.

Implement `jarvis/voice.py`:

1. Public function:
     def record_until_silence() -> bytes
   - Opens PyAudio stream: format=paInt16, channels=CHANNELS, rate=SAMPLE_RATE,
     input=True, frames_per_buffer=CHUNK_SIZE (all from config)
   - Reads chunks in a loop
   - Computes RMS per chunk: rms = sqrt(mean(samples**2)) on int16 samples
   - Tracks silent time: once a chunk has rms < SILENCE_THRESHOLD_RMS, start counting
     silence duration. Reset on a loud chunk.
   - Stops when silence duration >= SILENCE_DURATION_SEC OR total time >= MAX_RECORDING_SEC
   - Closes stream, returns concatenated PCM bytes

2. Helper:
     def _rms(chunk_bytes: bytes) -> float
   - Converts to np.int16 array, computes float RMS

3. `if __name__ == "__main__":`
   - Calls record_until_silence()
   - Saves to /tmp/jarvis_voice_test.wav using wave module
   - Prints duration in seconds

Type hints. Use numpy for RMS. No comments.
```

**Verify:** `python jarvis/voice.py`, speak a sentence, wait. It should stop ~1.5s after you stop. Open the WAV — your voice is in it.

---

### Task 6: Wake word

**Goal:** `wake_word.py` — openWakeWord listener that calls a callback when "Hey Jarvis" is heard.
**Files:** `wake_word.py`.
**Depends on:** Task 1. No external setup — the pre-trained model auto-downloads on first run (~25MB), so the first launch needs internet.

```prompt
Read @JARVIS_SPEC.md sections 5.1 and 0.

Implement `jarvis/wake_word.py`:

1. Class:
     class WakeWordListener:
         def __init__(self, on_wake: Callable[[], None]): ...
         def start(self) -> None    # starts a background thread
         def stop(self) -> None     # sets a stop flag and joins thread

2. Internally:
   - In __init__, lazy-load the openWakeWord model:
       from openwakeword.model import Model
       self._oww = Model(
           wakeword_models=[config.WAKEWORD_MODEL],
           inference_framework="onnx",
       )
     Wrap this in try/except — if it fails (e.g. no internet on first run while
     downloading the model), raise RuntimeError with a message saying the first
     launch needs internet to fetch the openWakeWord model.

   - On start(), spawn a threading.Thread(daemon=True) running _listen_loop.

   - _listen_loop:
       * Open PyAudio input stream:
           format=pyaudio.paInt16, channels=1, rate=config.SAMPLE_RATE,
           input=True, frames_per_buffer=config.WAKEWORD_CHUNK_SIZE
       * Loop while not self._stop_flag:
           - data = stream.read(config.WAKEWORD_CHUNK_SIZE, exception_on_overflow=False)
           - audio_np = np.frombuffer(data, dtype=np.int16)
           - scores = self._oww.predict(audio_np)
           - score = scores.get(config.WAKEWORD_MODEL, 0.0)
           - If score > config.WAKEWORD_THRESHOLD:
               * Compute RMS of the same frame
               * If rms >= config.SILENCE_THRESHOLD_RMS:
                   - Call self._on_wake()
               * Either way, reset the model to avoid re-firing on the same event:
                   self._oww.reset()
               * Sleep ~0.3s to debounce
       * On exit: stream.stop_stream(), stream.close(), paudio.terminate()

3. stop(): sets self._stop_flag = True, then thread.join(timeout=2).

4. Type hints on public methods. No comments unless logic is non-obvious.

5. `if __name__ == "__main__":`
   - Create a listener with callback `lambda: print("Wake word detected!")`
   - listener.start()
   - print("Say 'Hey Jarvis'. Ctrl-C to quit.")
   - Sleep in a loop, on KeyboardInterrupt call listener.stop()
```

**Verify:** Run `python jarvis/wake_word.py`. First run downloads the model (~25MB, a few seconds). Then say "Hey Jarvis" — should print the message. Ctrl-C to exit.

---

### Task 7: Phase 1+2 integration smoke test (temporary)

**Goal:** Prove the full voice → screenshot → Gemini loop works end-to-end. No UI yet.
**Files:** `proto_main.py` (temporary, will be deleted in Task 16).
**Depends on:** Tasks 2, 3, 4, 5, 6.

```prompt
Read @JARVIS_SPEC.md section 2.3.

Create `jarvis/proto_main.py` — a TEMPORARY integration test:

1. Import: wake_word, voice, transcription, capture, gemini.

2. Define handle_wake() that:
   - Prints "[heard wake word]"
   - Calls voice.record_until_silence() → pcm
   - Prints "[recorded, transcribing]"
   - Calls transcription.transcribe_pcm(pcm) → question
   - Prints f"[question] {question}"
   - Calls capture.capture_primary_monitor() → image
   - Calls gemini.ask(image, question, context=None) → answer
   - Prints f"[jarvis] {answer}"

3. Main:
   - Construct WakeWordListener(on_wake=handle_wake)
   - listener.start()
   - print("Say 'Hey Jarvis' to begin. Ctrl-C to quit.")
   - Sleep in a loop until KeyboardInterrupt → listener.stop()

No threading concerns — wake_word callback runs in the wake thread, which is fine here
because there's no UI yet. We add proper thread handoff in Task 13.

Add a note at the top: "# TEMPORARY — replaced by main.py in Task 16."
```

**Verify:** `python jarvis/proto_main.py`. Say "Hey Jarvis, what am I looking at?" and wait. Terminal prints the answer. **This is your first working version of Jarvis.**

---

## Phase 3 — CV pipeline

### Task 8: Active window ROI

**Goal:** Crop a full screenshot to the active window's bounds.
**Files:** `cv_pipeline.py` (start of file).
**Depends on:** Task 3.

```prompt
Read @JARVIS_SPEC.md sections 3.1 (Stage 1) and 0.

Begin `jarvis/cv_pipeline.py`. Add only the active-window logic for now — we add segmentation and change detection in the next two tasks.

1. Cross-platform active window detection:
     def _get_active_window_bounds() -> tuple[tuple[int,int,int,int], str]
   Returns ((x, y, w, h), title).

   On Windows (sys.platform == "win32"):
   - import win32gui
   - hwnd = win32gui.GetForegroundWindow()
   - title = win32gui.GetWindowText(hwnd)
   - rect = win32gui.GetWindowRect(hwnd)   # (left, top, right, bottom)
   - return ((left, top, right-left, bottom-top), title)

   On Mac (sys.platform == "darwin"):
   - Use AppKit / Quartz: from AppKit import NSWorkspace and Quartz.CGWindowListCopyWindowInfo
   - Best-effort; if it fails, fall back to full screen
   - Return (bounds, app_name)

   On error or unsupported: return ((0, 0, image_width, image_height), "Unknown")
   — but since we don't have the image here, return a sentinel and let the caller fall back.
   Cleanest: catch all exceptions, return ((-1, -1, -1, -1), "Unknown") and caller checks.

2. Public:
     def crop_to_active_window(full: "np.ndarray") -> tuple["np.ndarray", str]
   - Calls _get_active_window_bounds()
   - If bounds is (-1,-1,-1,-1) or w/h <= 0, return (full, title)
   - Clamp x, y, w, h to image dimensions
   - Returns (full[y:y+h, x:x+w], title)

3. `if __name__ == "__main__":`
   - Capture primary monitor
   - Crop to active window
   - Save to tempdir and print path + title + shape
```

**Verify:** Run it with VSCode focused. Saved image should be just the VSCode window, title should mention VSCode.

---

### Task 9: UI region segmentation

**Goal:** Detect and classify UI regions in the cropped active window.
**Files:** `cv_pipeline.py` (add to).
**Depends on:** Task 8.

```prompt
Read @JARVIS_SPEC.md sections 3.1 (Stage 2) and 0.

Add to `jarvis/cv_pipeline.py`:

1. Function:
     def segment_regions(image: "np.ndarray") -> list[dict]
   - image is BGR
   - Convert to gray, apply cv2.Canny(gray, 50, 150)
   - Find contours: cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
   - frame_area = image.shape[0] * image.shape[1]
   - For each contour:
       * cv2.boundingRect → (x, y, w, h)
       * area = w * h
       * if area < config.MIN_CONTOUR_AREA_RATIO * frame_area: skip
       * classify:
           y_center_ratio = (y + h/2) / image.shape[0]
           x_center_ratio = (x + w/2) / image.shape[1]
           aspect = w / h if h else 0
           if y_center_ratio < config.TOOLBAR_Y_RATIO: region = "toolbar"
           elif y_center_ratio > config.STATUSBAR_Y_RATIO: region = "statusbar"
           elif (0.3 < x_center_ratio < 0.7
                 and 0.3 < y_center_ratio < 0.7
                 and 0.5 < aspect < 0.85
                 and area > 0.15 * frame_area):
               region = "dialog"
           else: region = "content"
       * append {"region": region, "bbox": (x, y, w, h)}
   - Return the list

2. Helper for the next task:
     def unique_regions(regions: list[dict]) -> list[str]
   - Returns deduplicated list of region names, ordered: toolbar, content, dialog, statusbar
     (only those present)

3. Extend the `__main__` block: print unique_regions(segment_regions(cropped)).
```

**Verify:** Run it on different windows (browser, terminal, file explorer). Output should include sensible regions like `["toolbar", "content", "statusbar"]`.

---

### Task 10: Change detection

**Goal:** Diff against previous frame, report which regions changed.
**Files:** `cv_pipeline.py` (wrap everything in a class).
**Depends on:** Task 9.

```prompt
Read @JARVIS_SPEC.md sections 3.1 (Stage 3), 3.4, and 0.

Refactor `jarvis/cv_pipeline.py` so all logic lives inside a class:

class CVPipeline:
    def __init__(self):
        self._prev_gray: "np.ndarray | None" = None

    def run(self, full_screenshot: "np.ndarray") -> dict:
        # 1. Crop to active window (existing crop_to_active_window logic)
        # 2. Segment regions (existing segment_regions logic)
        # 3. Change detection:
        cropped_gray = cv2.cvtColor(cropped, cv2.COLOR_BGR2GRAY)
        if self._prev_gray is None or self._prev_gray.shape != cropped_gray.shape:
            changed_region_names = ["initial_capture"]
        else:
            diff = cv2.absdiff(cropped_gray, self._prev_gray)
            _, thresh = cv2.threshold(diff, config.CHANGE_DIFF_THRESHOLD, 255, cv2.THRESH_BINARY)
            change_contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            # For each change contour, find centroid, then find which segmented region contains it
            changed = set()
            for c in change_contours:
                if cv2.contourArea(c) < 50:  # ignore tiny diffs
                    continue
                M = cv2.moments(c)
                if M["m00"] == 0: continue
                cx, cy = int(M["m10"]/M["m00"]), int(M["m01"]/M["m00"])
                for r in regions:
                    x, y, w, h = r["bbox"]
                    if x <= cx < x+w and y <= cy < y+h:
                        changed.add(r["region"])
                        break
            changed_region_names = sorted(changed) if changed else ["none"]
        self._prev_gray = cropped_gray
        # 4. Convert cropped BGR → RGB for downstream model (spec 3.1)
        cropped_rgb = cv2.cvtColor(cropped, cv2.COLOR_BGR2RGB)
        return {
            "active_window": title,
            "regions": unique_regions(regions),
            "changed_regions": changed_region_names,
            "image": cropped_rgb,
        }

Keep crop_to_active_window, segment_regions, unique_regions as module-level helpers OR private methods — your call, but keep the class as the public API.

Update `__main__`:
- pipeline = CVPipeline()
- Capture twice with a 2-second sleep between
- Run pipeline on each, print the returned dict (omit "image", just print its shape)
- Expect first call: changed_regions == ["initial_capture"]; second call: some subset of regions
```

**Verify:** Run, switch windows between the two captures, see the diff show up.

---

### Task 11: Wire CV pipeline into Gemini

**Goal:** Update `gemini.py` to consume the CV context; update `proto_main.py` to use the pipeline.
**Files:** `gemini.py`, `proto_main.py`.
**Depends on:** Tasks 2, 10.

```prompt
Read @JARVIS_SPEC.md sections 3.4 and 4.3.

Two changes:

A) `jarvis/gemini.py` — the public `ask` function should now accept the context dict shape:
     {"active_window": str, "regions": list[str], "changed_regions": list[str], "image": np.ndarray}
   Update signature to:
     def ask(question: str, context: dict) -> str
   The image is taken from context["image"] (already RGB per Task 10). Build the user
   message exactly as in spec 4.3, joining list fields with ", ". If image is BGR (defensive
   check via shape and dtype is fine — but skip if you can trust the contract), convert.
   Keep all error handling from Task 2.

   Update the __main__ in gemini.py: build a fake context dict from a screenshot for the
   self-test (no CV — just pass full screen as image with empty regions list).

B) `jarvis/proto_main.py`:
   - Import cv_pipeline.CVPipeline
   - Create one instance at module level
   - In handle_wake, replace the bare screenshot + ask call with:
       full = capture.capture_primary_monitor()
       context = pipeline.run(full)
       answer = gemini.ask(question, context)
   - Print context["active_window"], context["regions"], context["changed_regions"]
     before printing the answer, so you can see what was sent.
```

**Verify:** `python jarvis/proto_main.py`, say "Hey Jarvis, what changed since last time?" — the response should reference the changed region.

---

## Phase 4 — UI

### Task 12: CustomTkinter window scaffolding

**Goal:** Standalone chat window with status bar, scrollable history, input + send button. No threading yet.
**Files:** `ui.py`.
**Depends on:** Task 1.

```prompt
Read @JARVIS_SPEC.md sections 6.1, 6.2, 6.3, and 0.

Implement `jarvis/ui.py`:

import customtkinter as ctk
import queue

class JarvisWindow:
    def __init__(self, on_submit_text):
        """
        on_submit_text: Callable[[str], None]
          Called when user types a follow-up and hits send/enter.
          Runs on the Tk main thread — the callback should kick off work
          on a worker thread itself if it does I/O.
        """
        self._on_submit_text = on_submit_text
        self.root = ctk.CTk()
        self.root.title("Jarvis")
        self.root.geometry(f"{config.WINDOW_WIDTH}x{config.WINDOW_HEIGHT}")
        self.root.attributes("-topmost", True)
        self._position_bottom_right()
        # Hide on close (X) instead of quitting:
        self.root.protocol("WM_DELETE_WINDOW", self.hide)

        # Status bar
        self.status_label = ctk.CTkLabel(self.root, text="", anchor="w")
        self.status_label.pack(fill="x", padx=8, pady=(8,4))

        # Chat scrollable frame
        self.chat_frame = ctk.CTkScrollableFrame(self.root)
        self.chat_frame.pack(fill="both", expand=True, padx=8, pady=4)

        # Input row
        input_row = ctk.CTkFrame(self.root)
        input_row.pack(fill="x", padx=8, pady=(4,8))
        self.input_entry = ctk.CTkEntry(input_row, placeholder_text="Type follow-up…")
        self.input_entry.pack(side="left", fill="x", expand=True, padx=(0,4))
        self.input_entry.bind("<Return>", lambda e: self._submit())
        self.send_btn = ctk.CTkButton(input_row, text="Send", width=60, command=self._submit)
        self.send_btn.pack(side="right")

        self.root.withdraw()  # start hidden

    def _position_bottom_right(self): ...
        # Compute screen size and place window at bottom-right with ~20px margin

    def _submit(self):
        text = self.input_entry.get().strip()
        if not text: return
        self.input_entry.delete(0, "end")
        self.add_user_message(text)
        self._on_submit_text(text)

    def add_user_message(self, text: str):
        # Right-aligned bubble: CTkLabel inside a frame anchored "e"
        ...

    def add_jarvis_message(self, text: str):
        # Left-aligned bubble: CTkLabel inside a frame anchored "w"
        ...

    def set_status(self, text: str):
        self.status_label.configure(text=text)

    def show(self):
        self.root.deiconify()
        self.root.lift()

    def hide(self):
        self.root.withdraw()

    def mainloop(self):
        self.root.mainloop()

if __name__ == "__main__":
    win = JarvisWindow(on_submit_text=lambda t: win.add_jarvis_message(f"You said: {t}"))
    win.show()
    win.set_status("Test mode")
    win.add_jarvis_message("Hello from Jarvis.")
    win.mainloop()

Implementation details to figure out:
- Bubble layout: I suggest a CTkFrame per message inside chat_frame, anchored e or w via
  .pack(anchor="e") or .pack(anchor="w"), containing a CTkLabel with wraplength ~360.
- Use different fg_color for user vs jarvis bubbles (e.g. blue vs gray).
- After adding a message, scroll to bottom: self.chat_frame._parent_canvas.yview_moveto(1.0)
```

**Verify:** `python jarvis/ui.py` shows a window bottom-right, you can type and see your message + a fake Jarvis echo.

---

### Task 13: Threading + queue communication

**Goal:** Define how background threads talk to the UI. Add a queue and a poll loop.
**Files:** `ui.py` (extend).
**Depends on:** Task 12.

```prompt
Read @JARVIS_SPEC.md section 2.2 and 0.

Extend `jarvis/ui.py`:

1. Inside JarvisWindow.__init__, create:
     self._event_queue: "queue.Queue[tuple[str, object]]" = queue.Queue()
   Event tuple format: (event_type, payload)
   Event types we'll use:
     ("status", str)            — update status bar text
     ("show", None)             — show the window
     ("hide", None)             — hide
     ("user_msg", str)          — render a user bubble
     ("jarvis_msg", str)        — render a jarvis bubble
     ("error", str)             — render an error as a jarvis bubble + clear status

2. Add a poll method:
     def _poll_queue(self):
         try:
             while True:
                 event_type, payload = self._event_queue.get_nowait()
                 self._handle_event(event_type, payload)
         except queue.Empty:
             pass
         self.root.after(50, self._poll_queue)

   def _handle_event(self, event_type, payload):
       match event_type:
           case "status": self.set_status(payload)
           case "show": self.show()
           case "hide": self.hide()
           case "user_msg": self.add_user_message(payload)
           case "jarvis_msg": self.add_jarvis_message(payload); self.set_status("")
           case "error": self.add_jarvis_message(payload); self.set_status("")

3. Add a thread-safe public API for other threads to enqueue events:
     def post(self, event_type: str, payload=None) -> None:
         self._event_queue.put((event_type, payload))

4. Schedule the first poll at the end of __init__:
     self.root.after(50, self._poll_queue)

5. Update __main__ test to demo: spawn a threading.Thread that sleeps then calls
   win.post("status", "Hello from thread"). Confirm it appears on the UI.
```

**Verify:** Run `python jarvis/ui.py`. After ~1s, the status bar should update from the background thread.

---

### Task 14: Status states + show/hide behavior

**Goal:** No new logic per se — make sure the JarvisWindow exposes the right API for `main.py` to use in Task 16.
**Files:** `ui.py`.
**Depends on:** Task 13.

```prompt
Read @JARVIS_SPEC.md section 6.4 and 0.

Light cleanup pass on `jarvis/ui.py`:

1. Add convenience methods (all just enqueue events — safe from any thread):
     def status_heard(self):       self.post("status", "Heard you, listening...")
     def status_recording(self):   self.post("status", "Listening...")
     def status_transcribing(self):self.post("status", "Processing speech...")
     def status_cv(self):          self.post("status", "Analyzing screen...")
     def status_thinking(self):    self.post("status", "Thinking...")
     def status_clear(self):       self.post("status", "")
     def show_window(self):        self.post("show")
     def hide_window(self):        self.post("hide")
     def user_said(self, text):    self.post("user_msg", text)
     def jarvis_says(self, text):  self.post("jarvis_msg", text)
     def show_error(self, text):   self.post("error", text)

2. These are the methods main.py will call. Everything else in JarvisWindow can be private.

3. Update __main__ to cycle through all status states with 800ms delays to demo them
   (spawn a thread that calls them in order).
```

**Verify:** Run `python jarvis/ui.py`, watch the status bar cycle through all states.

---

## Phase 5 — Polish

### Task 15: Privacy warning + first-run marker

**Goal:** First launch shows the modal; subsequent launches skip it.
**Files:** new `privacy.py` (small) OR add to `ui.py`. Suggest standalone `privacy.py`.
**Depends on:** Task 12.

```prompt
Read @JARVIS_SPEC.md section 7.2 and 0.

Create `jarvis/privacy.py`:

import customtkinter as ctk
from pathlib import Path
import config

def _marker_path() -> Path:
    return Path.home() / config.PRIVACY_MARKER_FILENAME

def needs_warning() -> bool:
    return not _marker_path().exists()

def show_warning_blocking() -> None:
    """Shows a modal warning and blocks until user clicks OK. Touches marker on dismissal."""
    win = ctk.CTk()
    win.title("Jarvis — Privacy Notice")
    win.geometry("420x220")
    win.attributes("-topmost", True)
    msg = ctk.CTkLabel(
        win,
        text=(
            "Jarvis will capture your screen when you say the wake word.\n\n"
            "Do not use near passwords, financial info, or private documents."
        ),
        wraplength=380,
        justify="left",
    )
    msg.pack(padx=20, pady=20, fill="both", expand=True)
    def _ok():
        _marker_path().touch()
        win.destroy()
    btn = ctk.CTkButton(win, text="I understand", command=_ok)
    btn.pack(pady=(0, 20))
    win.protocol("WM_DELETE_WINDOW", _ok)
    win.mainloop()

if __name__ == "__main__":
    # For testing: delete marker and re-show
    p = _marker_path()
    if p.exists(): p.unlink()
    show_warning_blocking()
    print("Marker now exists:", p.exists())
```

**Verify:** `python jarvis/privacy.py` shows the modal, clicking OK creates `~/.jarvis_seen`.

---

### Task 16: main.py wire-up

**Goal:** Replace `proto_main.py` with the real `main.py` that wires everything through the UI queue.
**Files:** `main.py`. Delete `proto_main.py` at the end.
**Depends on:** Tasks 6, 11, 14, 15.

```prompt
Read @JARVIS_SPEC.md sections 2.2, 2.3, and 0.

Implement `jarvis/main.py` and delete `proto_main.py`:

import threading
import privacy
from ui import JarvisWindow
from wake_word import WakeWordListener
from cv_pipeline import CVPipeline
import voice, transcription, capture, gemini

class JarvisApp:
    def __init__(self):
        self.ui = JarvisWindow(on_submit_text=self._handle_follow_up)
        self.pipeline = CVPipeline()
        self.listener = WakeWordListener(on_wake=self._handle_wake)
        self._busy_lock = threading.Lock()  # prevents overlapping invocations

    def _handle_wake(self):
        # runs in wake thread; spawn a worker so we don't block the listener
        threading.Thread(target=self._voice_invocation, daemon=True).start()

    def _voice_invocation(self):
        if not self._busy_lock.acquire(blocking=False):
            return  # already processing; drop this trigger
        try:
            self.ui.show_window()
            self.ui.status_heard()
            self.ui.status_recording()
            pcm = voice.record_until_silence()
            self.ui.status_transcribing()
            question = transcription.transcribe_pcm(pcm).strip()
            if not question:
                self.ui.show_error("Didn't catch that. Try again.")
                return
            self.ui.user_said(question)
            self._answer(question)
        finally:
            self._busy_lock.release()

    def _handle_follow_up(self, text: str):
        # called on UI thread; do the work on a worker
        threading.Thread(target=lambda: self._answer(text), daemon=True).start()

    def _answer(self, question: str):
        self.ui.status_cv()
        full = capture.capture_primary_monitor()
        context = self.pipeline.run(full)
        self.ui.status_thinking()
        try:
            answer = gemini.ask(question, context)
        except Exception as e:
            self.ui.show_error(f"Error: {e}")
            return
        self.ui.jarvis_says(answer)

    def run(self):
        if privacy.needs_warning():
            privacy.show_warning_blocking()
        self.listener.start()
        try:
            self.ui.mainloop()  # blocks main thread
        finally:
            self.listener.stop()

if __name__ == "__main__":
    JarvisApp().run()

After writing main.py, delete proto_main.py (`rm jarvis/proto_main.py`).
```

**Verify:** `python jarvis/main.py`. First time: privacy modal. After: window stays hidden. Say "Jarvis, what am I looking at?" — window pops up, status cycles, answer appears. Type a follow-up — works. Close window with X — listener keeps running. Say "Jarvis" again — window reappears.

---

### Task 17: README and setup instructions

**Goal:** A README a stranger can follow to run the project.
**Files:** `README.md`.
**Depends on:** all prior tasks complete.

```prompt
Read @JARVIS_SPEC.md sections 1, 8, 9, 10.

Write `README.md` (overwrite the stub from Task 1). Include:

1. One-paragraph description (what Jarvis does, voice + screen + Gemini).
2. Demo GIF placeholder line: `![demo](demo.gif)` with a TODO comment that I'll record it.
3. Requirements: Python 3.10+, Windows primary (Mac compatible), a Google AI Studio account (for Gemini API key).
4. Setup steps:
   a. Clone repo
   b. `python -m venv .venv && .venv\Scripts\activate` (Windows) or `source .venv/bin/activate` (Mac)
   c. Mac only: `brew install portaudio`
   d. `pip install -r requirements.txt`
   e. Sign up at aistudio.google.com → create API key
   f. `cp .env.example .env` → fill in `GEMINI_API_KEY`
   g. `python jarvis/main.py` (first run downloads ~25MB openWakeWord model + ~150MB Whisper model — needs internet)
5. Usage: say "Jarvis" + question.
6. Architecture: short summary of the 5 components + a link/reference to `JARVIS_SPEC.md`.
7. Project structure: tree from spec section 8.
8. Privacy: link to spec section 7.
9. Troubleshooting:
   - "No wake word triggers" → check mic permissions, lower `WAKEWORD_THRESHOLD` in config.py, ensure first-run model download completed
   - "Gemini errors from Turkey" → see Task 18 / local fallback
   - "PyAudio install fails on Mac" → brew install portaudio first
   - "Whisper slow" → first run downloads the model; subsequent runs are faster
10. License: MIT (or your choice — I'll edit).

Keep it under 200 lines. Use code fences for commands. Use real headers (#, ##).
```

**Verify:** Hand the README to a friend (or just re-read it). They should be able to set up from scratch.

---

### Task 18 (OPTIONAL): Local vision model fallback

**Goal:** Drop-in replacement for `gemini.py` using Ollama + a local vision model. Only build if Gemini API gives you trouble.
**Files:** `local_vision.py`, small change in `gemini.py` or `main.py` to switch backend.
**Depends on:** Task 11.

```prompt
Read @JARVIS_SPEC.md section 4.2.

Only build this if Gemini API is unreliable. Otherwise skip.

1. Install Ollama (https://ollama.com) and pull a vision model:
     ollama pull moondream
   (or `ollama pull qwen2-vl:2b` for better quality — uses ~4GB VRAM vs ~3GB)

2. Create `jarvis/local_vision.py` with the SAME public signature as gemini.py:
     def ask(question: str, context: dict) -> str

3. Implementation:
   - Import requests (or use ollama-python: `pip install ollama`)
   - Convert context["image"] (RGB ndarray) → PIL → base64 PNG string
   - POST to http://localhost:11434/api/chat with:
       {
         "model": "moondream",
         "messages": [
           {"role": "system", "content": <system prompt from spec 4.3>},
           {"role": "user", "content": <user message text from spec 4.3>,
            "images": [<base64 png>]}
         ],
         "stream": False
       }
   - Parse response.json()["message"]["content"]
   - Same error handling philosophy: return user-facing strings, never raise to the UI

4. Add `MOONDREAM_MODEL = "moondream"` (or "qwen2-vl:2b") to config.py.

5. In main.py, add at the top:
     from config import VISION_BACKEND
     if VISION_BACKEND == "local":
         import local_vision as vision
     else:
         import gemini as vision
   Then replace `gemini.ask(...)` with `vision.ask(...)`.

6. Flip the switch in config.py: VISION_BACKEND = "local".

7. Document this in README's troubleshooting section.
```

**Verify:** Set `VISION_BACKEND = "local"` in config.py, ensure `ollama serve` is running, run `python jarvis/main.py`, ask a question. Response comes from local model — slower than Gemini but works offline.

---

## Done

After Task 17 (or 18), the project is feature-complete per the MVP scope in `JARVIS_SPEC.md`.

Suggested timeline:

- Phase 1 (Tasks 1-4): half a day each, ~2 days
- Phase 2 (Tasks 5-7): ~2 days
- Phase 3 (Tasks 8-11): ~3 days (most CV-heavy)
- Phase 4 (Tasks 12-14): ~2 days
- Phase 5 (Tasks 15-17): ~1 day
- Total: ~10 working days for a class project pace
