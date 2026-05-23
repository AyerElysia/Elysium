"""QQ group Werewolf game plugin."""

from __future__ import annotations

from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.base import BasePlugin, register_plugin

from .actions import WerewolfPlayerAction
from .event_handler import WerewolfCommandEventHandler
from .service import WerewolfGameService

logger = get_logger("werewolf_game")


@register_plugin
class WerewolfGamePlugin(BasePlugin):
    """QQ 群狼人杀插件。"""

    plugin_name = "werewolf_game"
    plugin_description = "QQ 群狼人杀裁判，支持爱莉以玩家视角参与"
    plugin_version = "0.1.0"

    configs: list[type] = []
    dependent_components: list[str] = []

    def __init__(self, config=None) -> None:
        super().__init__(config)
        self._werewolf_engine = None
        self._werewolf_games = {}

    def get_components(self) -> list[type]:
        logger.info("werewolf_game 已加载：群命令 /狼人杀，爱莉玩家动作 action-werewolf_player")
        return [
            WerewolfGameService,
            WerewolfCommandEventHandler,
            WerewolfPlayerAction,
        ]

