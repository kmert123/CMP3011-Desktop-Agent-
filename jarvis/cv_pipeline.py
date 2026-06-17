"""OpenCV pipeline: ROI crop, UI region segmentation, change detection."""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import cv2
import numpy as np

import config


def _get_active_window_bounds() -> tuple[tuple[int, int, int, int], str]:
	if sys.platform == "win32":
		try:
			import win32gui  # type: ignore

			hwnd = win32gui.GetForegroundWindow()
			title = win32gui.GetWindowText(hwnd)
			left, top, right, bottom = win32gui.GetWindowRect(hwnd)
			return ((left, top, right - left, bottom - top), title)
		except Exception:
			return ((-1, -1, -1, -1), "Unknown")

	if sys.platform == "darwin":
		try:
			from AppKit import NSWorkspace  # type: ignore
			from Quartz import (  # type: ignore
				CGWindowListCopyWindowInfo,
				kCGNullWindowID,
				kCGWindowListExcludeDesktopElements,
				kCGWindowListOptionOnScreenOnly,
			)

			app = NSWorkspace.sharedWorkspace().frontmostApplication()
			app_name = app.localizedName() if app else "Unknown"
			options = kCGWindowListOptionOnScreenOnly | kCGWindowListExcludeDesktopElements
			windows = CGWindowListCopyWindowInfo(options, kCGNullWindowID)

			if windows:
				for window in windows:
					if window.get("kCGWindowOwnerName") != app_name:
						continue
					if window.get("kCGWindowLayer") != 0:
						continue
					bounds = window.get("kCGWindowBounds") or {}
					x = int(bounds.get("X", -1))
					y = int(bounds.get("Y", -1))
					w = int(bounds.get("Width", -1))
					h = int(bounds.get("Height", -1))
					return ((x, y, w, h), app_name)

			return ((-1, -1, -1, -1), app_name)
		except Exception:
			return ((-1, -1, -1, -1), "Unknown")

	return ((-1, -1, -1, -1), "Unknown")


def crop_to_active_window(full: "np.ndarray") -> tuple["np.ndarray", str]:
	"""Crop a screenshot to the active window; fallback to full frame."""
	(x, y, w, h), title = _get_active_window_bounds()
	if (x, y, w, h) == (-1, -1, -1, -1) or w <= 0 or h <= 0:
		return full, title

	height, width = full.shape[:2]
	x = max(0, min(x, width - 1))
	y = max(0, min(y, height - 1))
	x2 = max(0, min(x + w, width))
	y2 = max(0, min(y + h, height))

	if x2 <= x or y2 <= y:
		return full, title

	return full[y:y2, x:x2], title


def segment_regions(image: "np.ndarray") -> list[dict]:
	"""Segment UI regions from a BGR image."""
	gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
	edges = cv2.Canny(gray, 50, 150)
	contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

	frame_area = image.shape[0] * image.shape[1]
	regions: list[dict] = []
	for contour in contours:
		x, y, w, h = cv2.boundingRect(contour)
		area = w * h
		if area < config.MIN_CONTOUR_AREA_RATIO * frame_area:
			continue

		y_center_ratio = (y + h / 2) / image.shape[0]
		x_center_ratio = (x + w / 2) / image.shape[1]
		aspect = w / h if h else 0
		if y_center_ratio < config.TOOLBAR_Y_RATIO:
			region = "toolbar"
		elif y_center_ratio > config.STATUSBAR_Y_RATIO:
			region = "statusbar"
		elif (
			0.3 < x_center_ratio < 0.7
			and 0.3 < y_center_ratio < 0.7
			and 0.5 < aspect < 0.85
			and area > 0.15 * frame_area
		):
			region = "dialog"
		else:
			region = "content"

		regions.append({"region": region, "bbox": (x, y, w, h)})

	return regions


def unique_regions(regions: list[dict]) -> list[str]:
	"""Return region names in a fixed, deduplicated order."""
	order = ["toolbar", "content", "dialog", "statusbar"]
	present = {r.get("region") for r in regions}
	return [name for name in order if name in present]


class CVPipeline:
	def __init__(self) -> None:
		self._prev_gray: "np.ndarray | None" = None

	def run(self, target: "Any") -> dict:
		"""Capture and analyse the target window.

		target must be a PerceptionTarget (or any object with .title and .is_self /
		.bounds attributes). Uses capture_target() so is_self and invalid bounds
		automatically fall back to a full-screen crop.
		"""
		from capture import capture_target
		cropped, _origin, _dpi, _stale = capture_target(target)
		title = getattr(target, "title", "")

		regions = segment_regions(cropped)

		cropped_gray = cv2.cvtColor(cropped, cv2.COLOR_BGR2GRAY)
		if self._prev_gray is None or self._prev_gray.shape != cropped_gray.shape:
			changed_region_names = ["initial_capture"]
		else:
			diff = cv2.absdiff(cropped_gray, self._prev_gray)
			_, thresh = cv2.threshold(
				diff, config.CHANGE_DIFF_THRESHOLD, 255, cv2.THRESH_BINARY
			)
			change_contours, _ = cv2.findContours(
				thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
			)
			changed = set()
			for contour in change_contours:
				if cv2.contourArea(contour) < 50:
					continue
				moments = cv2.moments(contour)
				if moments["m00"] == 0:
					continue
				mcx = int(moments["m10"] / moments["m00"])
				mcy = int(moments["m01"] / moments["m00"])
				for region in regions:
					rx, ry, rw, rh = region["bbox"]
					if rx <= mcx < rx + rw and ry <= mcy < ry + rh:
						changed.add(region["region"])
						break
			changed_region_names = sorted(changed) if changed else ["none"]

		self._prev_gray = cropped_gray
		cropped_rgb = cv2.cvtColor(cropped, cv2.COLOR_BGR2RGB)
		return {
			"active_window": title,
			"regions": unique_regions(regions),
			"changed_regions": changed_region_names,
			"image": cropped_rgb,
		}


if __name__ == "__main__":
	from perception_target import capture_foreground_target
	pipeline = CVPipeline()
	first = pipeline.run(capture_foreground_target())
	out_path = Path(tempfile.gettempdir()) / "jarvis_active_window.png"
	cv2.imwrite(str(out_path), cv2.cvtColor(first["image"], cv2.COLOR_RGB2BGR))
	print(out_path)
	print({**first, "image": first["image"].shape})

	time.sleep(2)
	second = pipeline.run(capture_foreground_target())
	print({**second, "image": second["image"].shape})
