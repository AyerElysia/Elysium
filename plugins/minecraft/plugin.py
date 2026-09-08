"""Minecraft is an independently configured Elysium scene plugin."""
from __future__ import annotations

from src.app.plugin_system.base import BasePlugin, register_plugin
from .config import MinecraftConfig
from .service import MinecraftService
from .tools import MINECRAFT_TOOLS


@register_plugin
class MinecraftPlugin(BasePlugin):
    plugin_name = "minecraft"
    plugin_description = "Minecraft 具身体验与场景意识接入"
    plugin_version = "0.1.0"
    configs = [MinecraftConfig]
    dependent_components = ["life_engine:service:life_engine"]

    def __init__(self, config: MinecraftConfig | None = None) -> None:
        super().__init__(config if config is not None else MinecraftConfig())
        self._service: MinecraftService | None = None

    @property
    def service(self) -> MinecraftService:
        if self._service is None:
            self._service = MinecraftService(self)
        return self._service

    def get_components(self) -> list[type]:
        if not self.config.settings.enabled:
            return []
        return [MinecraftService, *MINECRAFT_TOOLS]

    async def on_plugin_loaded(self) -> None:
        await self.service.start()

    async def on_plugin_unloaded(self) -> None:
        if self._service is not None:
            await self._service.stop()
