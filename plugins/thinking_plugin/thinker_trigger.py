"""思考触发器 - 事件处理器。

订阅对话事件，在每次 LLM 生成回复前注入醒目的"先思考"提醒，
并在 think action 被调用后立即移除，实现"用完即删"。
"""

from __future__ import annotations

from typing import Any

from src.core.components.base.event_handler import BaseEventHandler
from src.core.components.types import EventType
from src.kernel.event import EventDecision
from src.kernel.logger import get_logger

from src.app.plugin_system.api.prompt_api import (
    add_system_reminder,
    _get_system_reminder_store,
)


logger = get_logger("thinking_plugin")

# 醒目的思考触发器提醒内容
THINK_TRIGGER_REMINDER = (
    "本轮如果要调用 `action-send_text` 回复用户，必须先调用 `action-think`，"
    "并且两者必须同轮出现，think 在前。不要直接发送正文。"
)

# 临时提醒的唯一标识名称
TRIGGER_REMINDER_NAME = "think_trigger_temp"


class ThinkerTrigger(BaseEventHandler):
    """思考触发器

    在每次 LLM 生成回复前注入醒目的思考提醒，
    当 think action 被调用后立即移除，实现"用完即删"。
    """

    handler_name: str = "thinking_thinker_trigger"
    handler_description: str = "在 LLM 回复前注入思考触发器提醒"
    weight: int = 5  # 较高优先级，在 prompt 组装前执行

    # 订阅 ON_CHATTER_STEP 事件（每次 chatter 生成回复时触发）
    init_subscribe: list[EventType | str] = [
        EventType.ON_CHATTER_STEP,
    ]

    def __init__(self, plugin: Any) -> None:
        """初始化思考触发器"""
        super().__init__(plugin)
        self._reminder_injected = False  # 标记是否已注入提醒

    async def execute(
        self,
        event_name: str,
        params: dict[str, Any],
    ) -> tuple[EventDecision, dict[str, Any]]:
        """执行触发器

        在 LLM 生成回复前注入提醒，在 think action 执行后移除提醒。
        """
        try:
            # 检查配置是否启用
            config = getattr(self.plugin, "config", None)
            if config and hasattr(config, "settings"):
                # 检查思考动作总开关
                if not getattr(config.settings, "enabled", True):
                    return EventDecision.SUCCESS, params
                # 检查触发器提醒开关
                if not getattr(config.settings, "enable_trigger_reminder", True):
                    logger.debug("触发器提醒已禁用，跳过注入")
                    return EventDecision.SUCCESS, params

            # 注入思考触发器提醒
            self._inject_think_reminder()

        except Exception as e:
            logger.error(f"思考触发器执行失败：{e}")
            import traceback
            logger.error(f"堆栈追踪：{traceback.format_exc()}")

        return EventDecision.SUCCESS, params

    def _inject_think_reminder(self) -> None:
        """注入思考触发器提醒"""
        # 如果已经注入过，跳过（避免重复）
        if self._reminder_injected:
            logger.debug("思考触发器提醒已注入，跳过")
            return

        try:
            add_system_reminder(
                bucket="actor",
                name=TRIGGER_REMINDER_NAME,
                content=THINK_TRIGGER_REMINDER,
            )
            self._reminder_injected = True
            logger.debug("已注入思考触发器提醒")
        except Exception as e:
            logger.warning(f"注入思考触发器失败：{e}")

    def remove_reminder(self) -> None:
        """移除思考触发器提醒（由 think action 调用时触发）"""
        try:
            store = _get_system_reminder_store()
            store.delete(bucket="actor", name=TRIGGER_REMINDER_NAME)
            self._reminder_injected = False
            logger.debug("已移除思考触发器提醒")
        except Exception as e:
            logger.warning(f"移除思考触发器失败：{e}")
