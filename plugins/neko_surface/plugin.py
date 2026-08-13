"""N.E.K.O Surface Gateway plugin."""

from __future__ import annotations

from src.core.components.base.plugin import BasePlugin
from src.core.components.loader import register_plugin
from src.kernel.logger import get_logger

from .adapter import NekoSurfaceAdapter
from .config import NekoSurfaceConfig
from .event_handler import NekoSurfaceDeliveredMirror
from .router import NekoSurfaceRouter
from .service import NekoSurfaceGateway, NekoSurfaceService

logger = get_logger("neko_surface")


@register_plugin
class NekoSurfacePlugin(BasePlugin):
    plugin_name = "neko_surface"
    plugin_description = "Versioned N.E.K.O presentation surface gateway"
    plugin_version = "1.0.0"
    configs = [NekoSurfaceConfig]

    def __init__(self, config: NekoSurfaceConfig | None = None) -> None:
        super().__init__(config)
        self.gateway: NekoSurfaceGateway | None = None

    def _enabled(self) -> bool:
        return isinstance(self.config, NekoSurfaceConfig) and self.config.plugin.enabled

    def _initialize_gateway(self) -> NekoSurfaceGateway:
        if self.gateway is None:
            self.gateway = NekoSurfaceGateway()
        return self.gateway

    def get_components(self) -> list[type]:
        if not self._enabled():
            return []
        self._initialize_gateway()
        return [
            NekoSurfaceService,
            NekoSurfaceRouter,
            NekoSurfaceAdapter,
            NekoSurfaceDeliveredMirror,
        ]

    async def on_plugin_loaded(self) -> None:
        if not self._enabled():
            return
        gateway = self._initialize_gateway()
        if not gateway.token_configured:
            logger.warning("NEKO_SURFACE_TOKEN is not configured; Surface WS will reject clients")

    async def on_plugin_unloaded(self) -> None:
        if self.gateway is not None:
            await self.gateway.shutdown()
            self.gateway = None
