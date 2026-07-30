"""Voice Live 事件处理器。

监听系统事件（如 adapter_command），
支持通过 QQ 命令触发/停止语音通话，
并发布通话状态变更到事件总线。
"""

from __future__ import annotations

from typing import Any

from src.core.components.base.event_handler import BaseEventHandler
from src.core.components.types import EventType
from src.kernel.event import EventDecision
from src.kernel.logger import get_logger

logger = get_logger("voice_live.event_handler", display="Voice Live Events")

# 自定义事件名
VOICE_LIVE_COMMAND_EVENT = "voice_live_command"


class VoiceLiveEventHandler(BaseEventHandler):
    """Voice Live 事件处理器。

    订阅事件：
    - ADAPTER_COMMAND: 接收来自 QQ 的适配器命令（触发/停止通话）
    - PLUGIN_LOADED: 插件加载完成后的初始化

    功能：
    - 响应 voice_live_start / voice_live_stop 命令
    - 发布通话状态变更事件
    """

    handler_name = "voice_live_handler"
    handler_description = "语音通话事件处理"
    weight = 50
    intercept_message = False
    init_subscribe = [EventType.ON_MESSAGE_RECEIVED, VOICE_LIVE_COMMAND_EVENT]

    def __init__(self, plugin: Any) -> None:
        super().__init__(plugin)
        self._active_sessions: set[str] = set()

    async def execute(
        self, event_name: str, params: dict[str, Any]
    ) -> tuple[EventDecision, dict[str, Any]]:
        """处理事件。"""
        if event_name == VOICE_LIVE_COMMAND_EVENT:
            return await self._handle_voice_command(params)

        return EventDecision.PASS, params

    async def _handle_voice_command(
        self, params: dict[str, Any]
    ) -> tuple[EventDecision, dict[str, Any]]:
        """处理语音通话命令。"""
        command = params.get("command", "") or params.get("action", "")

        match command:
            case "voice_live_start":
                await self._start_voice_live(params)
                return EventDecision.SUCCESS, params

            case "voice_live_stop":
                await self._stop_voice_live(params)
                return EventDecision.SUCCESS, params

            case "voice_live_status":
                await self._report_status(params)
                return EventDecision.SUCCESS, params

        return EventDecision.PASS, params

    async def _start_voice_live(self, params: dict[str, Any]) -> None:
        """启动语音通话（通知）。"""
        logger.info("收到语音通话启动命令")
        # 语音通话主要通过 Web 页面发起
        # QQ 命令仅作为通知/触发机制
        # 实际通话需要用户在浏览器中操作

        # 发布事件通知
        try:
            from src.kernel.event import get_event_bus
            bus = get_event_bus()
            await bus.publish("voice_live_started", {
                "source": "qq_command",
                "user_id": params.get("user_id", ""),
            })
        except Exception:  # noqa: BLE001
            pass

    async def _stop_voice_live(self, params: dict[str, Any]) -> None:
        """停止语音通话。"""
        logger.info("收到语音通话停止命令")

        # 通知所有活跃会话停止
        router = self._get_router()
        if router:
            for session_id, session in list(router._sessions.items()):
                await session.stop()
                router._sessions.pop(session_id, None)

        # 发布事件
        try:
            from src.kernel.event import get_event_bus
            bus = get_event_bus()
            await bus.publish("voice_live_stopped", {
                "source": "qq_command",
            })
        except Exception:  # noqa: BLE001
            pass

    async def _report_status(self, params: dict[str, Any]) -> None:
        """报告通话状态。"""
        router = self._get_router()
        active_count = len(router._sessions) if router else 0
        logger.info(f"语音通话状态: {active_count} 个活跃会话")

    def _get_router(self) -> Any:
        """获取 VoiceLiveRouter 实例。"""
        try:
            from src.core.managers import get_plugin_manager
            pm = get_plugin_manager()
            # 尝试通过组件签名获取
            router = pm.get_component("Voice-Live:router:voice_live")
            return router
        except Exception:  # noqa: BLE001
            pass
        return None
