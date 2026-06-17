"""CustomTkinter chat window."""

from __future__ import annotations

import threading
from typing import Callable

import customtkinter as ctk
import queue

import config


class JarvisWindow:
	def __init__(self, on_submit_text: Callable[[str], None]) -> None:
		"""
		on_submit_text: Callable[[str], None]
		  Called when user types a follow-up and hits send/enter.
		  Runs on the Tk main thread.
		"""
		self._on_submit_text = on_submit_text
		self.root = ctk.CTk()
		self.root.title("Jarvis")
		self.root.geometry(f"{config.WINDOW_WIDTH}x{config.WINDOW_HEIGHT}")
		self.root.attributes("-topmost", True)
		self._position_bottom_right()
		self.root.protocol("WM_DELETE_WINDOW", self.hide)

		self.status_label = ctk.CTkLabel(self.root, text="", anchor="w")
		self.status_label.pack(fill="x", padx=8, pady=(8, 4))

		self.chat_frame = ctk.CTkScrollableFrame(self.root)
		self.chat_frame.pack(fill="both", expand=True, padx=8, pady=4)

		input_row = ctk.CTkFrame(self.root)
		input_row.pack(fill="x", padx=8, pady=(4, 8))
		self.input_entry = ctk.CTkEntry(input_row, placeholder_text="Type follow-up...")
		self.input_entry.pack(side="left", fill="x", expand=True, padx=(0, 4))
		self.input_entry.bind("<Return>", lambda _e: self._submit())
		self.send_btn = ctk.CTkButton(input_row, text="Send", width=60, command=self._submit)
		self.send_btn.pack(side="right")

		self._event_queue: "queue.Queue[tuple[str, object]]" = queue.Queue()
		self._stream_bubbles: dict[str, tuple[ctk.CTkFrame, ctk.CTkLabel, str]] = {}
		self.root.withdraw()
		self.root.after(50, self._poll_queue)

	def _position_bottom_right(self) -> None:
		self.root.update_idletasks()
		screen_w = self.root.winfo_screenwidth()
		screen_h = self.root.winfo_screenheight()
		x = max(0, screen_w - config.WINDOW_WIDTH - 20)
		y = max(0, screen_h - config.WINDOW_HEIGHT - 40)
		self.root.geometry(f"{config.WINDOW_WIDTH}x{config.WINDOW_HEIGHT}+{x}+{y}")

	def _submit(self) -> None:
		text = self.input_entry.get().strip()
		if not text:
			return
		self.input_entry.delete(0, "end")
		self.add_user_message(text)
		self._on_submit_text(text)

	def add_user_message(self, text: str) -> None:
		wrapper = ctk.CTkFrame(self.chat_frame, fg_color="transparent")
		wrapper.pack(fill="x", padx=4, pady=2, anchor="e")
		bubble = ctk.CTkLabel(
			wrapper,
			text=text,
			fg_color="#2B6CB0",
			text_color="white",
			wraplength=360,
			justify="left",
			corner_radius=10,
		)
		bubble.pack(anchor="e", padx=6, pady=4)
		self.chat_frame._parent_canvas.yview_moveto(1.0)

	def add_jarvis_message(self, text: str) -> None:
		wrapper = ctk.CTkFrame(self.chat_frame, fg_color="transparent")
		wrapper.pack(fill="x", padx=4, pady=2, anchor="w")
		bubble = ctk.CTkLabel(
			wrapper,
			text=text,
			fg_color="#444444",
			text_color="white",
			wraplength=360,
			justify="left",
			corner_radius=10,
		)
		bubble.pack(anchor="w", padx=6, pady=4)
		self.chat_frame._parent_canvas.yview_moveto(1.0)

	def set_status(self, text: str) -> None:
		self.status_label.configure(text=text)

	def show(self) -> None:
		self.root.deiconify()
		self.root.lift()

	def hide(self) -> None:
		self.root.withdraw()

	def _poll_queue(self) -> None:
		try:
			while True:
				event_type, payload = self._event_queue.get_nowait()
				self._handle_event(event_type, payload)
		except queue.Empty:
			pass
		self.root.after(50, self._poll_queue)

	def _handle_event(self, event_type: str, payload: object) -> None:
		match event_type:
			case "status":
				self.set_status(str(payload))
			case "show":
				self.show()
			case "hide":
				self.hide()
			case "user_msg":
				self.add_user_message(str(payload))
			case "jarvis_msg":
				self.add_jarvis_message(str(payload))
				self.set_status("")
			case "error":
				self.add_jarvis_message(str(payload))
				self.set_status("")
			case "stream_begin":
				bubble_id, offline = payload if isinstance(payload, tuple) else (str(payload), False)
				wrapper = ctk.CTkFrame(self.chat_frame, fg_color="transparent")
				wrapper.pack(fill="x", padx=4, pady=2, anchor="w")
				fg = "#5a3824" if offline else "#444444"
				label = ctk.CTkLabel(
					wrapper, text="…", fg_color=fg, text_color="white",
					wraplength=360, justify="left", corner_radius=10,
				)
				label.pack(anchor="w", padx=6, pady=4)
				self._stream_bubbles[bubble_id] = (wrapper, label, "")
				self.set_status("")
				self.chat_frame._parent_canvas.yview_moveto(1.0)
			case "stream_chunk":
				bubble_id, chunk = payload  # type: ignore[misc]
				if bubble_id in self._stream_bubbles:
					wrapper, label, existing = self._stream_bubbles[bubble_id]
					new_text = existing + chunk
					label.configure(text=new_text)
					self._stream_bubbles[bubble_id] = (wrapper, label, new_text)
					self.chat_frame._parent_canvas.yview_moveto(1.0)
			case "stream_end":
				self._stream_bubbles.pop(str(payload), None)
			case "confirm":
				description, callback = payload  # type: ignore[misc]
				self._show_confirm_dialog(str(description), callback)

	def post(self, event_type: str, payload: object = None) -> None:
		self._event_queue.put((event_type, payload))

	def status_heard(self) -> None:
		self.post("status", "Heard you, listening...")

	def status_recording(self) -> None:
		self.post("status", "Listening...")

	def status_transcribing(self) -> None:
		self.post("status", "Processing speech...")

	def status_cv(self) -> None:
		self.post("status", "Analyzing screen...")

	def status_thinking(self) -> None:
		self.post("status", "Thinking...")

	def status_clear(self) -> None:
		self.post("status", "")

	def show_window(self) -> None:
		self.post("show")

	def hide_window(self) -> None:
		self.post("hide")

	def user_said(self, text: str) -> None:
		self.post("user_msg", text)

	def jarvis_says(self, text: str) -> None:
		self.post("jarvis_msg", text)

	def show_error(self, text: str) -> None:
		self.post("error", text)

	# --- Streaming bubble API (thread-safe) ---

	def begin_stream_bubble(self, bubble_id: str, offline: bool = False) -> None:
		self.post("stream_begin", (bubble_id, offline))

	def append_stream_chunk(self, bubble_id: str, chunk: str) -> None:
		self.post("stream_chunk", (bubble_id, chunk))

	def finish_stream_bubble(self, bubble_id: str) -> None:
		self.post("stream_end", bubble_id)

	# --- Blocking confirmation modal (call from any thread) ---

	def confirm_action(self, description: str) -> bool:
		"""Block the calling thread until the user confirms or cancels. Auto-denies after 30s."""
		evt = threading.Event()
		result: list[bool] = [False]

		def _callback(confirmed: bool) -> None:
			result[0] = confirmed
			evt.set()

		self.post("confirm", (description, _callback))
		evt.wait(timeout=30)
		return result[0]

	def _show_confirm_dialog(self, description: str, callback: Callable[[bool], None]) -> None:
		dialog = ctk.CTkToplevel(self.root)
		dialog.title("Confirm Action")
		dialog.geometry("380x160")
		dialog.attributes("-topmost", True)
		dialog.grab_set()

		ctk.CTkLabel(dialog, text=description, wraplength=340, justify="left").pack(
			padx=20, pady=(20, 8), fill="both", expand=True
		)
		btn_row = ctk.CTkFrame(dialog, fg_color="transparent")
		btn_row.pack(pady=(0, 16))

		def _yes() -> None:
			dialog.destroy()
			callback(True)

		def _no() -> None:
			dialog.destroy()
			callback(False)

		ctk.CTkButton(btn_row, text="Yes", width=80, fg_color="#2B6CB0", command=_yes).pack(side="left", padx=8)
		ctk.CTkButton(btn_row, text="No",  width=80, fg_color="#666666",  command=_no).pack(side="left", padx=8)
		dialog.protocol("WM_DELETE_WINDOW", _no)

	def get_hwnd(self) -> int:
		"""Return the Win32 HWND of the Jarvis root window (0 on non-Windows)."""
		try:
			return self.root.winfo_id()
		except Exception:
			return 0

	def mainloop(self) -> None:
		self.root.mainloop()


if __name__ == "__main__":
	win = JarvisWindow(on_submit_text=lambda t: win.add_jarvis_message(f"You said: {t}"))

	def _boot() -> None:
		win.show()
		win.add_jarvis_message("Hello from Jarvis.")
		def _threaded() -> None:
			import time
			import threading

			def _worker() -> None:
				statuses = [
					win.status_heard,
					win.status_recording,
					win.status_transcribing,
					win.status_cv,
					win.status_thinking,
					win.status_clear,
				]
				for step in statuses:
					step()
					time.sleep(0.8)

			threading.Thread(target=_worker, daemon=True).start()

		_threaded()

	win.root.after(0, _boot)
	win.mainloop()
