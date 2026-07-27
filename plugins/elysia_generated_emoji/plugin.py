"""Elysia 生成表情包插件。"""

from __future__ import annotations

from src.core.components.base import BasePlugin
from src.core.components.loader import register_plugin
from src.kernel.logger import get_logger

from .actions import GenerateEmojiMemeAction
from .config import ElysiaGeneratedEmojiConfig
from .service import ElysiaGeneratedEmojiService

logger = get_logger("elysia_generated_emoji")


@register_plugin
class ElysiaGeneratedEmojiPlugin(BasePlugin):
    """现场生成表情包，不检索旧表情包库。"""

    plugin_name = "elysia_generated_emoji"
    plugin_description = "爱莉现场生成表情包：不检索旧表情包库，不回退旧图"
    plugin_version = "0.1.0"
    plugin_author = "Neo-MoFox Team"

    configs = [ElysiaGeneratedEmojiConfig]

    def __init__(self, config: ElysiaGeneratedEmojiConfig | None = None) -> None:
        super().__init__(config)
        self.emoji_service: ElysiaGeneratedEmojiService | None = None

    async def on_plugin_loaded(self) -> None:
        cfg = self.config
        if not isinstance(cfg, ElysiaGeneratedEmojiConfig) or not cfg.plugin.enabled:
            logger.info("ElysiaGeneratedEmojiPlugin 已禁用")
            return
        self.emoji_service = ElysiaGeneratedEmojiService(self)
        await self.emoji_service.initialize()

    async def on_plugin_unloaded(self) -> None:
        self.emoji_service = None

    def get_components(self) -> list[type]:
        cfg = self.config
        if not isinstance(cfg, ElysiaGeneratedEmojiConfig) or not cfg.plugin.enabled:
            return []
        return [ElysiaGeneratedEmojiService, GenerateEmojiMemeAction]
