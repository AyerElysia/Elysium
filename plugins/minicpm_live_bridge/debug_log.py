"""Terminal-facing debug logging helpers for MiniCPM Live Bridge."""

from __future__ import annotations

import sys
from datetime import datetime
from typing import Any

from src.kernel.logger import Logger


def _bool_config(config: Any, name: str, default: bool) -> bool:
    debug_config = getattr(config, "debug", None)
    return bool(getattr(debug_config, name, default))


def _preview(value: Any, *, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "..."


def _preview_limit(config: Any) -> int:
    debug_config = getattr(config, "debug", None)
    try:
        return max(80, int(getattr(debug_config, "preview_chars", 360) or 360))
    except (TypeError, ValueError):
        return 360


def live_terminal_log(
    logger: Logger,
    config: Any,
    message: str,
    *,
    level: str = "info",
) -> None:
    """Write a live debug line through Neo logger and optional stderr mirror."""

    if not _bool_config(config, "terminal_log_enabled", True):
        return

    normalized_level = str(level or "info").lower()
    log_func = getattr(logger, normalized_level, logger.info)
    log_func(message)

    if not _bool_config(config, "stderr_mirror_enabled", True):
        return

    timestamp = datetime.now().strftime("%H:%M:%S")
    line = (
        f"[{timestamp}] MiniCPM Live | {normalized_level.upper()} | "
        f"{_preview(message, limit=_preview_limit(config))}\n"
    )
    try:
        sys.stderr.write(line)
        sys.stderr.flush()
    except Exception:
        pass
