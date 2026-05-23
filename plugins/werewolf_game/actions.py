"""Life-chatter facing player action for Werewolf."""

from __future__ import annotations

from typing import Annotated

from src.app.plugin_system.base import BaseAction

from .service import WerewolfGameService


class WerewolfPlayerAction(BaseAction):
    """Let the active chatter play Werewolf through a player-only view."""

    action_name = "werewolf_player"
    action_description = (
        "作为狼人杀玩家执行动作或查看自己的玩家视角。只返回当前角色能知道的信息，"
        "不会暴露裁判全量身份表。action 可用：view/join/kill/check/heal/poison/pass/vote。"
    )
    primary_action = False
    chatter_allow = ["default_chatter", "life_chatter"]

    async def execute(
        self,
        action: Annotated[str, "动作：view、join、kill、check、heal、poison、pass、vote"],
        target: Annotated[str, "目标玩家编号或昵称；view/join/heal/pass 可留空"] = "",
    ) -> tuple[bool, str]:
        service = WerewolfGameService(plugin=self.plugin)
        result = await service.handle_bot_action(
            self.chat_stream,
            action=action,
            target=target,
        )
        return True, result

