"""直播事件处理器。

订阅系统事件，支持外部命令触发直播开始/停止。
"""

from __future__ import annotations

import logging
from typing import Any

from src.core.components.base.event_handler import BaseEventHandler, EventDecision
from src.core.components.types import EventType

logger = logging.getLogger(__name__)

# 自定义事件名
LIVESTREAM_COMMAND_EVENT = "livestream_command"


class LivestreamEventHandler(BaseEventHandler):
    """直播事件处理器。

    支持命令：
    - livestream_start: 开始直播
    - livestream_stop: 停止直播
    - livestream_status: 查询状态
    """

    handler_name = "livestream_handler"
    handler_description = "AI 直播事件处理"

    @property
    def init_subscribe(self) -> list:
        return [EventType.ON_START, EventType.ON_STOP, LIVESTREAM_COMMAND_EVENT]

    async def execute(self, event_name: str, params: dict[str, Any]) -> tuple[EventDecision, dict[str, Any]]:
        """处理事件。"""
        # 系统启动/停止
        if event_name == EventType.ON_START:
            return await self._on_system_start()
        if event_name == EventType.ON_STOP:
            return await self._on_system_stop()

        # 自定义命令
        if event_name == LIVESTREAM_COMMAND_EVENT:
            command = params.get("command", "")
            return await self._handle_command(command, params)

        return EventDecision.CONTINUE, params

    async def _on_system_start(self) -> tuple[EventDecision, dict]:
        """系统启动时检查是否需要自动开播。"""
        # auto_start 逻辑由 router 处理
        logger.info("直播事件处理器已就绪")
        return EventDecision.CONTINUE, {}

    async def _on_system_stop(self) -> tuple[EventDecision, dict]:
        """系统停止时清理。"""
        logger.info("直播事件处理器已停止")
        return EventDecision.CONTINUE, {}

    async def _handle_command(
        self, command: str, params: dict
    ) -> tuple[EventDecision, dict]:
        """处理直播命令。"""
        match command:
            case "livestream_start":
                logger.info("收到直播开始命令")
                return EventDecision.CONTINUE, {"action": "start"}
            case "livestream_stop":
                logger.info("收到直播停止命令")
                return EventDecision.CONTINUE, {"action": "stop"}
            case "livestream_status":
                logger.info("收到直播状态查询")
                return EventDecision.CONTINUE, {"action": "status"}
            case _:
                return EventDecision.CONTINUE, {}
