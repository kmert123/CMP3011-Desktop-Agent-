"""Privacy warning modal and first-run marker."""

import customtkinter as ctk
from pathlib import Path

import config


def _marker_path() -> Path:
    return Path.home() / config.PRIVACY_MARKER_FILENAME


def needs_warning() -> bool:
    return not _marker_path().exists()


def _privacy_text() -> str:
    """Return modal body text that accurately reflects what leaves the device."""
    if config.VISION_BACKEND == "local":
        pixel_line = (
            "Screenshots are analysed locally via Ollama — "
            "pixel data never leaves your device."
        )
    else:
        pixel_line = (
            "For visual queries, screenshots are sent to Google's Gemini API. "
            "Set VISION_BACKEND=local in .env to keep pixels on-device."
        )
    return (
        "Jarvis captures your screen when you say the wake word.\n\n"
        "Screen text (from accessibility APIs and OCR) is sent to "
        f"Google's Gemini API as text to generate answers.\n\n"
        f"{pixel_line}\n\n"
        "Do not use near passwords, financial info, or private documents."
    )


def show_warning_blocking() -> None:
    """Shows a modal warning and blocks until user clicks OK. Touches marker on dismissal."""
    win = ctk.CTk()
    win.title("Jarvis — Privacy Notice")
    win.geometry("420x300")
    win.attributes("-topmost", True)
    msg = ctk.CTkLabel(
        win,
        text=_privacy_text(),
        wraplength=380,
        justify="left",
    )
    msg.pack(padx=20, pady=20, fill="both", expand=True)

    def _ok() -> None:
        _marker_path().touch()
        win.destroy()

    btn = ctk.CTkButton(win, text="I understand", command=_ok)
    btn.pack(pady=(0, 20))
    win.protocol("WM_DELETE_WINDOW", _ok)
    win.mainloop()


if __name__ == "__main__":
    p = _marker_path()
    if p.exists():
        p.unlink()
    show_warning_blocking()
    print("Marker now exists:", p.exists())
