"""思考动作 - 在发送回复前记录一段内心动作。"""

from typing import Annotated

from src.core.components.base.action import BaseAction
from src.kernel.logger import get_logger

logger = get_logger("thinking_plugin")


class ThinkAction(BaseAction):
    """思考动作。

    该 action 只用于发送回复前的同轮前置动作。
    它不承担信息检索或 follow-up 推理，而是让模型在真正发送文本前，
    先显式记录一段内心活动。
    """

    action_name = "think"
    action_description = (
        "在发送文本回复前，先记录一段内心思考动作。"
        "此 action 必须与 action-send_text 同时使用，且必须排在 action-send_text 之前；"
        "不要单独调用，也不要把它和查询型 tool 混在同一轮。"
        "注意：thought 只写内心活动，不要把真正要发给用户的正文只写在 thought 里；"
        "最终回复必须单独写进 action-send_text 的 content。"
        "调用时请在 thought 参数中写下你此刻的心理活动。"
    )

    chatter_allow: list[str] = ["default_chatter", "kokoro_flow_chatter"]
    primary_action: bool = False

    async def go_activate(self) -> bool:
        """检查动作是否应该激活。"""
        config = getattr(self.plugin, "config", None)
        if config is None:
            return True
        return getattr(config.settings, "enabled", True)

    async def execute(
        self,
        mood: Annotated[
            str,
            "此刻的心情/情绪状态（必填）。例如：开心、疑惑、担心、期待等。"
        ],
        decision: Annotated[
            str,
            "你决定的下一步行动（必填）。此处通常应填写你准备发送什么样的回复。"
        ],
        expected_response: Annotated[
            str,
            "你预期用户看到回复后的反应（必填）。例如：'应该会满意吧'、'可能会继续追问'等。"
        ],
        thought: Annotated[
            str | None,
            "你的心理活动，写下你此刻的想法和分析过程。请真诚地反映你的思考，不要敷衍。"
        ] = None,
        **extra_kwargs: object,
    ) -> tuple[bool, str]:
        """执行思考动作。"""
        legacy_content = extra_kwargs.pop("content", None)
        normalized_thought = (thought or "").strip()
        if not normalized_thought and isinstance(legacy_content, str):
            normalized_thought = legacy_content.strip()
            if normalized_thought:
                logger.warning(
                    "action-think 收到兼容字段 content，已映射到 thought"
                )

        if not normalized_thought:
            # 保底容错：避免参数漂移导致整轮 action 链路报错。
            logger.warning(
                "action-think 缺少 thought/content，已按 mood/decision/expected_response 降级记录"
            )

        if extra_kwargs:
            # 模型偶发会把 send_text 的字段（如 content）误塞到 action-think。
            # 这里选择容错忽略，避免整轮 tool call 因未知参数失败。
            logger.warning(
                "action-think 收到未知参数，已忽略: %s",
                sorted(extra_kwargs.keys()),
            )
        self._remove_trigger_reminder()
        return (
            True,
            "思考动作已记录。请在同一轮内继续调用 action-send_text 发送最终回复。",
        )

    def _remove_trigger_reminder(self) -> None:
        """移除思考触发器提醒。"""
        try:
            trigger = None
            for comp in self.plugin.components:
                if (
                    hasattr(comp, "handler_name")
                    and comp.handler_name == "thinking_thinker_trigger"
                ):
                    trigger = comp
                    break

            if trigger and hasattr(trigger, "remove_reminder"):
                trigger.remove_reminder()
            else:
                from src.app.plugin_system.api.prompt_api import (
                    _get_system_reminder_store,
                )

                store = _get_system_reminder_store()
                store.delete(bucket="actor", name="think_trigger_temp")
        except Exception as exc:
            logger.debug(f"移除思考触发器失败：{exc}")
