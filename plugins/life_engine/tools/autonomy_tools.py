"""Tools for life_engine autonomy intents."""

from __future__ import annotations

from typing import Annotated

from src.app.plugin_system.base import BaseTool


class LifeEngineScheduleAutonomyIntentTool(BaseTool):
    """Register a delayed autonomy intent."""

    tool_name = "nucleus_schedule_autonomy_intent"
    tool_description = (
        "登记一个由 life_engine 自己形成的延迟自主意向。"
        "这不是命令表达层立刻行动，也不是规则触发；只是让某个意向在 delay_minutes 后重新浮现。"
        "\n\n"
        "kind 支持：speak / reflect / silence。"
        "\n- speak：到点后把意向交给 life_chatter 重新判断是否开口。"
        "\n- reflect：到点后回到 life_engine 事件流，供后续心跳继续思考。"
        "\n- silence：到点后只记录选择沉默，不打扰任何聊天。"
        "\n\n"
        "只能填写 delay_minutes，不要填写绝对时间。"
        "speak 只能写 motivation、target_hint 和 constraints，不能写最终回复话术。"
        "如果知道精确聊天流可填 target_stream_id；否则留空，只让意向进入事件流，不猜测目标。"
    )
    chatter_allow: list[str] = ["life_engine_internal"]

    async def execute(
        self,
        kind: Annotated[str, "意向类型：speak / reflect / silence"],
        motivation: Annotated[str, "形成这个意向的内在动机；不要写最终话术"],
        delay_minutes: Annotated[int, "隔多少分钟后让这个意向重新浮现"],
        target_hint: Annotated[str, "目标提示，例如某个群/私聊/关系对象；不用于直接路由"] = "",
        target_stream_id: Annotated[str, "可选：精确聊天流 ID。speak 到点后会唤醒该流"] = "",
        target_key: Annotated[str, "可选：近期发送目标 key；通常留空"] = "",
        constraints: Annotated[list[str] | None, "表达层承接时应知道的约束，不是台词"] = None,
    ) -> tuple[bool, str | dict]:
        service = getattr(self.plugin, "service", None)
        if service is None:
            return False, "life_engine 服务不可用"
        try:
            result = await service.schedule_autonomy_intent(
                kind=kind,
                motivation=motivation,
                delay_minutes=delay_minutes,
                target_hint=target_hint,
                target_stream_id=target_stream_id,
                target_key=target_key,
                constraints=constraints or [],
            )
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)
        return True, result


AUTONOMY_TOOLS = [
    LifeEngineScheduleAutonomyIntentTool,
]

__all__ = [
    "AUTONOMY_TOOLS",
    "LifeEngineScheduleAutonomyIntentTool",
]
