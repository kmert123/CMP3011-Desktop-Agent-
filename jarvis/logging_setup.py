"""Configure the root logger for Jarvis.

Call setup_logging() once at startup — before any other module does work —
so the RotatingFileHandler is attached before the first _log.debug() fires.
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path


def setup_logging(level: int | str | None = None) -> None:
    """Attach file + stderr handlers to the root logger.

    Idempotent: does nothing if handlers are already attached.
    """
    root = logging.getLogger()
    if root.handlers:
        return

    resolved = _resolve_level(level)

    log_dir = Path.home() / ".jarvis"
    log_dir.mkdir(parents=True, exist_ok=True)

    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)-22s %(message)s")

    fh = RotatingFileHandler(
        log_dir / "jarvis.log",
        maxBytes=5_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    fh.setFormatter(fmt)
    fh.setLevel(resolved)

    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    ch.setLevel(logging.WARNING)

    root.setLevel(resolved)
    root.addHandler(fh)
    root.addHandler(ch)


def _resolve_level(level: int | str | None) -> int:
    if level is not None:
        return logging.getLevelName(level) if isinstance(level, str) else int(level)
    try:
        import config as _cfg
        raw = getattr(_cfg, "LOG_LEVEL", "DEBUG")
        result = logging.getLevelName(raw)
        return result if isinstance(result, int) else logging.DEBUG
    except Exception:
        return logging.DEBUG
