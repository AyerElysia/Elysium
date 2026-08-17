"""Public resolver for the canonical local message TTS service."""

from __future__ import annotations

from typing import Protocol, cast

from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.api.service_api import get_service
from src.core.managers import get_plugin_manager

logger = get_logger("tts_voice_plugin.api")
SERVICE_SIGNATURE = "tts_voice_plugin:service:tts"


class LocalTTSService(Protocol):
    """Minimal cross-plugin synthesis contract."""

    async def generate_voice(
        self,
        text: str,
        style_hint: str = "default",
        language_hint: str | None = None,
    ) -> str | None: ...


def get_local_tts_service() -> LocalTTSService | None:
    """Return the plugin-owned service, with a framework fallback for partial runtimes."""
    try:
        plugin = get_plugin_manager().get_plugin("tts_voice_plugin")
        service = getattr(plugin, "tts_service", None) if plugin is not None else None
        if service is not None and callable(getattr(service, "generate_voice", None)):
            return cast(LocalTTSService, service)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"canonical local TTS lookup failed: {type(exc).__name__}")

    try:
        service = get_service(SERVICE_SIGNATURE)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"local TTS service fallback failed: {type(exc).__name__}")
        return None
    if service is None or not callable(getattr(service, "generate_voice", None)):
        return None
    return cast(LocalTTSService, service)


__all__ = ["LocalTTSService", "SERVICE_SIGNATURE", "get_local_tts_service"]
