"""QQ group Werewolf game plugin v2.0."""

from __future__ import annotations

from pathlib import Path

from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.base import BasePlugin, register_plugin

from .config import WerewolfConfig
from .domain import WerewolfDomainService
from .event_handler import WerewolfCommandEventHandler
from .ledger import WerewolfLedger
from .service import WerewolfGameService

logger = get_logger("werewolf_game")


@register_plugin
class WerewolfGamePlugin(BasePlugin):
    """QQ 群狼人杀插件（商业级 v2.0）。"""

    plugin_name = "werewolf_game"
    plugin_description = "商业级 QQ 群狼人杀：警长/守卫/猎人/遗言/PK/AI玩家/戏剧叙事"
    plugin_version = "2.0.0"

    configs: list[type] = [WerewolfConfig]
    dependent_components: list[str] = []

    def __init__(self, config=None) -> None:
        super().__init__(config)
        self._werewolf_engine = None
        self._werewolf_games = {}
        self._werewolf_ai = None
        self._werewolf_narrator = None
        self._werewolf_ledger = WerewolfLedger(
            Path("runtime") / "api" / "tabletop.sqlite3"
        )
        self._werewolf_domain = WerewolfDomainService(self._werewolf_ledger)

    async def on_plugin_unloaded(self) -> None:
        """Release the shared durable ledger owned by this plugin instance."""

        self._werewolf_ledger.close()
        await super().on_plugin_unloaded()

    def get_components(self) -> list[type]:
        logger.info(
            "werewolf_game v2.0 已加载：群命令 /狼人杀，"
            "支持警长/守卫/猎人/白痴/遗言/PK/AI玩家/戏剧叙事"
        )
        return [
            WerewolfGameService,
            WerewolfCommandEventHandler,
        ]

