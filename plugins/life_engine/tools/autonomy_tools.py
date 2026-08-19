"""Tools for life_engine autonomy intents."""

from __future__ import annotations

from typing import Annotated

from src.app.plugin_system.base import BaseTool

from .bounded_projection import project_bounded_items, sha256_json


class LifeEngineScheduleAutonomyIntentTool(BaseTool):
    """Historical schema retained only for explicit retirement diagnostics."""

    tool_name = "nucleus_schedule_autonomy_intent"
    tool_description = (
        "旧 AutonomyIntent 创建入口已退役且不再注册给模型。历史直接调用只返回"
        "只读错误，不创建任务、不选择聊天流、不产生周期调度。请改用"
        "nucleus_manage_initiative_seed；需要行动时再分别使用"
        "nucleus_reachability 与 nucleus_begin_outreach。"
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
        return False, {
            "error": "LegacyAutonomyReadOnly",
            "mutated": False,
            "replacement": "nucleus_manage_initiative_seed",
        }


class LifeEngineManageAutonomyIntentTool(BaseTool):
    """Read the retired stream-bound intent ledger without mutating it."""

    tool_name = "nucleus_manage_autonomy_intent"
    tool_description = (
        "只读查看旧 AutonomyIntent 归档。旧系统把主体意向绑定到聊天流、最近活跃"
        "目标和周期调度，现已退役；pause/cancel/renew 均明确失败且不会改写旧数据。"
        "新的主体主动性请使用 nucleus_manage_initiative_seed、"
        "nucleus_reachability 和 nucleus_begin_outreach。"
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
        normalized_action = str(action or "").strip().lower()
        if normalized_action != "list":
            return False, {
                "error": "LegacyAutonomyReadOnly",
                "mutated": False,
                "replacement": "nucleus_manage_initiative_seed",
            }
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


# Historical classes remain importable for deterministic replay diagnostics,
# but no AutonomyIntent tool is exposed to a live consciousness instance.
AUTONOMY_TOOLS: list[type[BaseTool]] = []

__all__ = [
    "AUTONOMY_TOOLS",
    "LifeEngineManageAutonomyIntentTool",
    "LifeEngineScheduleAutonomyIntentTool",
]
