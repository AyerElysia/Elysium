"""Ayla independent-application channel adapter.

Inbound messages enter through the authenticated API injection path. Outbound
messages are acknowledged here, while Ayla's SSE projection is the sole
application delivery channel.
"""

from __future__ import annotations

from typing import Any, cast

from mofox_wire import CoreSink, MessageEnvelope

from src.app.plugin_system.api.log_api import get_logger
from src.core.components.base import BaseAdapter, BasePlugin
from src.core.components.loader import register_plugin

from .config import AylaAdapterConfig
from .sender import AylaSender

logger = get_logger("ayla_adapter")


class AylaAdapter(BaseAdapter):
    """Transport identity for the trusted Ayla application surface."""

    adapter_name = "ayla_adapter"
    adapter_version = "1.0.0"
    adapter_author = "Elysium Team"
    adapter_description = "Ayla 独立应用聊天通道（platform=ayla）"
    platform = "ayla"
    run_in_subprocess = False

    def __init__(
        self,
        core_sink: CoreSink,
        plugin: "AylaAdapterPlugin | None" = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(core_sink, plugin=plugin, **kwargs)
        self._sender: AylaSender | None = None
        self._config_ok = False

    def _get_config(self) -> AylaAdapterConfig | None:
        if self.plugin is None or self.plugin.config is None:
            return None
        return cast(AylaAdapterConfig, self.plugin.config)

    async def on_adapter_loaded(self) -> None:
        config = self._get_config()
        if config is None:
            raise RuntimeError("Ayla 适配器启动失败：缺少插件配置")
        if not config.plugin.enabled:
            raise RuntimeError("Ayla 适配器启动失败：插件已停用")
        self._sender = AylaSender()
        self._config_ok = True
        logger.info("Ayla 适配器已加载；实际交付由应用 SSE 投影负责")

    async def on_adapter_unloaded(self) -> None:
        self._config_ok = False
        self._sender = None

    async def from_platform_message(
        self,
        raw: dict[str, Any],
    ) -> MessageEnvelope | None:
        del raw
        return None

    async def _send_platform_message(self, envelope: MessageEnvelope) -> None:
        if self._sender is None:
            raise RuntimeError("Ayla 适配器尚未加载")
        await self._sender.send(envelope)

    async def get_bot_info(self) -> dict[str, Any]:
        config = self._get_config()
        return {
            "bot_id": "elysia",
            "bot_name": (config.backend.bot_name if config else "") or "爱莉",
            "platform": self.platform,
        }

    async def health_check(self) -> bool:
        return self._config_ok and self._sender is not None

    @property
    def sender(self) -> AylaSender | None:
        return self._sender


@register_plugin
class AylaAdapterPlugin(BasePlugin):
    """Plugin container for the Ayla application channel."""

    plugin_name = "ayla_adapter"
    plugin_version = "1.0.0"
    plugin_author = "Elysium Team"
    plugin_description = "Ayla 独立应用聊天通道"
    configs = [AylaAdapterConfig]

    def get_components(self) -> list[type]:
        config = cast(AylaAdapterConfig | None, self.config)
        if config is not None and not config.plugin.enabled:
            return []
        return [AylaAdapter]
