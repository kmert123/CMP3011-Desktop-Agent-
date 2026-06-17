# Recorded sessions for replay harness

Each file is a JSON object with the following schema:

```json
{
  "session_id": "string",
  "recorded_at": "ISO-8601",
  "app_class": "native_win32 | chromium_electron | ...",
  "turns": [
    {
      "query": "string",
      "frame_path": "relative/path/to/frame.png or null",
      "screen_model_path": "relative/path/to/screen_model.json or null",
      "expected_act": "ANSWER | ACT",
      "expected_perception": "NONE | STRUCTURE | PIXELS",
      "expected_rung": "UIA | OCR | VISION | null",
      "expected_action_kind": "click_element | open_app | set_clipboard | null",
      "answer_correct": true,
      "grounding_target": "element text or null",
      "notes": "optional free text"
    }
  ]
}
```

Place recorded session JSON files here. The harness (`eval/harness.py`) auto-discovers all
`*.json` files in this directory and replays them using offline fixture ScreenModels.
