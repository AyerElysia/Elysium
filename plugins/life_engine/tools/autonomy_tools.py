"""Tools for life_engine autonomy intents."""

from __future__ import annotations

from typing import Annotated

from src.app.plugin_system.base import BaseTool

from .bounded_projection import project_bounded_items, sha256_json


class LifeEngineScheduleAutonomyIntentTool(BaseTool):
    """Register a delayed autonomy intent."""

    tool_name = "nucleus_schedule_autonomy_intent"
    tool_description = (
        "登记一个由 life_engine 自己形成的延迟自主意向。"
        "这不是命令表达层立刻行动，也不是规则触发；只是让某个意向在 delay_minutes 后重新浮现。"
        "如果 repeat=true，则到点后会在上一 occurrence 完整结束后按 interval_minutes 再次浮现；"
        "interval_minutes 留空时使用 delay_minutes，并且必须提供 max_occurrences 或 lease_minutes。"
        "\n\n"
        "kind 支持：speak / reflect / silence。"
        "\n- speak：到点后把意向交给 life_chatter 重新判断是否开口。"
        "\n- reflect：到点后回到 life_engine 事件流，供后续心跳继续思考。"
        "\n- silence：到点后只记录选择沉默，不打扰任何聊天。"
        "\n\n"
        "只能填写 delay_minutes，不要填写绝对时间。"
        "周期意向也只是反复浮现，不代表必须行动或必须开口。"
        "speak 只能写 motivation、target_hint 和 constraints，不能写最终回复话术。"
        "speak 的目标可填心跳里看到的 target_key，或精确 target_stream_id；"
        "都留空时意向到点只会以事件浮现给心跳、不会唤醒表达层，不要猜测列表之外的目标。"
    )
    chatter_allow: list[str] = ["life_engine_internal"]

    async def execute(
        self,
        kind: Annotated[str, "意向类型：speak / reflect / silence"],
        motivation: Annotated[str, "形成这个意向的内在动机；不要写最终话术"],
        delay_minutes: Annotated[int, "隔多少分钟后让这个意向重新浮现"],
        target_hint: Annotated[str, "目标提示，例如某个群/私聊/关系对象；不用于直接路由"] = "",
        target_stream_id: Annotated[str, "可选：精确聊天流 ID。speak 到点后会唤醒该流"] = "",
        target_key: Annotated[str, "可选：心跳里「你可以触达的人和地方」列出的目标 key"] = "",
        constraints: Annotated[list[str] | None, "表达层承接时应知道的约束，不是台词"] = None,
        repeat: Annotated[bool, "是否周期性重复浮现；默认 false"] = False,
        interval_minutes: Annotated[int | None, "repeat=true 时每隔多少分钟再次浮现；留空则使用 delay_minutes"] = None,
        max_occurrences: Annotated[int | None, "repeat=true 时最多浮现多少次；与 lease_minutes 至少填写一个"] = None,
        lease_minutes: Annotated[int | None, "repeat=true 时执行租约持续多少分钟；与 max_occurrences 至少填写一个"] = None,
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
                repeat=repeat,
                interval_minutes=interval_minutes,
                max_occurrences=max_occurrences,
                lease_minutes=lease_minutes,
            )
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)
        return True, result


class LifeEngineManageAutonomyIntentTool(BaseTool):
    """Let the subject explicitly inspect or change intent execution lifecycle."""

    tool_name = "nucleus_manage_autonomy_intent"
    tool_description = (
        "管理由你自己形成的自主意向。list 只读列出意向；pause 暂停后续浮现；"
        "cancel 明确取消；renew 为周期意向增加次数租约或时间租约。"
        "这些操作只改变执行生命周期，不改写原始动机、约束或历史。"
    )
    chatter_allow: list[str] = ["life_engine_internal"]

    async def execute(
        self,
        action: Annotated[str, "list / pause / cancel / renew"],
        intent_id: Annotated[str, "目标 intent_id；list 时可留空"] = "",
        additional_occurrences: Annotated[int, "renew 时追加允许浮现的次数"] = 0,
        lease_minutes: Annotated[int, "renew 时追加的时间租约（分钟）"] = 0,
        continuation: Annotated[
            str,
            "Optional list continuation returned by the previous page",
        ] = "",
        max_bytes: Annotated[
            int | None,
            "Optional list byte budget; the task hard cap still applies",
        ] = None,
    ) -> tuple[bool, str | dict]:
        service = getattr(self.plugin, "service", None)
        if service is None:
            return False, "life_engine 服务不可用"
        try:
            result = await service.manage_autonomy_intent(
                action=action,
                intent_id=intent_id,
                additional_occurrences=additional_occurrences,
                lease_minutes=lease_minutes,
            )
            if str(action or "").strip().lower() == "list":
                source_items = list(result.get("intents") or [])
                frontier = {
                    "count": len(source_items),
                    "intents_sha256": sha256_json(source_items),
                }
                item_refs = []
                for item in source_items:
                    item_hash = sha256_json(item)
                    intent_ref = str(item.get("intent_id") or "").strip()
                    item_refs.append(
                        f"autonomy-intent:{intent_ref or 'unknown'}:sha256:{item_hash}"
                    )
                result = project_bounded_items(
                    projection_name="autonomy-intent-list",
                    task_name=getattr(self, "_runtime_task_name", ""),
                    requested_max_bytes=max_bytes,
                    binding={"action": "list"},
                    frontier=frontier,
                    base_payload={
                        key: value
                        for key, value in result.items()
                        if key != "intents"
                    },
                    items_key="intents",
                    items=source_items,
                    item_refs=item_refs,
                    continuation=continuation,
                )
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)
        return True, result


AUTONOMY_TOOLS = [
    LifeEngineScheduleAutonomyIntentTool,
    LifeEngineManageAutonomyIntentTool,
]

__all__ = [
    "AUTONOMY_TOOLS",
    "LifeEngineManageAutonomyIntentTool",
    "LifeEngineScheduleAutonomyIntentTool",
]
