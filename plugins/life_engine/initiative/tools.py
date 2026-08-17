"""Subject-facing initiative tools shared by consciousness instances."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Annotated, ClassVar, Literal

from src.app.plugin_system.base import BaseTool

from ..tools.bounded_projection import project_bounded_items, sha256_json
from .contracts import (
    InitiativeConflict,
    InitiativeOutreachCommand,
    InitiativeSeedCommand,
)
from .projection import (
    initiative_seed_summary,
    project_initiative_seed_content,
)
from .reachability import load_reachable_surfaces

InitiativeToolAction = Literal[
    "hold",
    "rewrite",
    "reencounter",
    "release",
    "list",
    "read",
]


def _service_actor(tool: BaseTool):
    from ..service.registry import get_life_engine_service

    service = get_life_engine_service()
    if service is None:
        raise RuntimeError("LifeEngineServiceUnavailable")
    stream_id = str(tool.get_current_stream_id() or "").strip()
    registry = service.consciousness_registry
    instance = registry.get_for_stream(stream_id) if stream_id else None
    # The heartbeat is an internal deliberative phase of the canonical global
    # consciousness, not a separate surface. Only that explicit task contract
    # may bind chat_global without a stream owner; stale/unknown chat, voice,
    # livestream, or embodied surfaces must fail closed instead of borrowing
    # chat_global's authority.
    if instance is None and str(
        getattr(tool, "_runtime_task_name", "") or ""
    ).strip() == "core":
        instance = registry.get("chat_global")
    if instance is None or not instance.is_active:
        raise PermissionError("InitiativeActorIsNotActive")
    return service, str(instance.instance_id)


def _source_occurrence(tool: BaseTool) -> str:
    bound = str(
        getattr(tool, "_life_source_occurrence_id", "") or ""
    ).strip()
    if bound:
        return bound
    message_id = str(getattr(tool.trigger_message, "message_id", "") or "").strip()
    if message_id:
        return f"message:{message_id}"
    tool_call_id = str(getattr(tool, "_tool_call_id", "") or "").strip()
    if tool_call_id:
        return f"tool-call:{tool_call_id}"
    raise RuntimeError("InitiativeSourceOccurrenceRequired")


def _source_instance(tool: BaseTool, actor: str) -> str:
    """Keep observed-source attribution distinct from the deciding actor."""

    trigger = tool.trigger_message
    extra = getattr(trigger, "extra", {}) or {}
    if not isinstance(extra, dict):
        extra = {}
    return str(
        getattr(trigger, "source_instance_id", "")
        or extra.get("source_instance_id")
        or extra.get("consciousness_instance_id")
        or actor
    ).strip()


def _decision_occurrence(tool: BaseTool, material: str) -> str:
    message_id = str(getattr(tool.trigger_message, "message_id", "") or "").strip()
    tool_call_id = str(getattr(tool, "_tool_call_id", "") or "").strip()
    if not tool_call_id:
        raise RuntimeError("InitiativeToolCallIdentityRequired")
    source_occurrence = _source_occurrence(tool)
    digest = hashlib.sha256(
        (
            f"{tool_call_id}\0{tool.get_current_stream_id()}\0{message_id}\0"
            f"{source_occurrence}\0{material}"
        ).encode()
    ).hexdigest()
    return f"initiative:decision:{digest}"


def _occurred_at(tool: BaseTool) -> str:
    bound = str(getattr(tool, "_life_source_occurred_at", "") or "").strip()
    if bound:
        parsed = datetime.fromisoformat(bound)
        if parsed.tzinfo is None:
            raise RuntimeError("InitiativeSourceTimeMustIncludeTimezone")
        return parsed.astimezone(UTC).isoformat()
    value = getattr(tool.trigger_message, "time", None)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, UTC).isoformat()
    if isinstance(value, datetime):
        parsed = value if value.tzinfo else value.replace(tzinfo=UTC)
        return parsed.astimezone(UTC).isoformat()
    raise RuntimeError("InitiativeSourceTimeRequired")


class LifeEngineManageInitiativeSeedTool(BaseTool):
    """Let one live consciousness explicitly author future continuity."""

    tool_name = "nucleus_manage_initiative_seed"
    tool_description = (
        "管理你明确选择保留的主体主动线索。它不是任务队列、优先级、隐藏推理或"
        "定时回复。hold/rewrite/release 的 statement 是愿意让未来意识实例看到的"
        "第一人称公开表述；reencounter 的 statement 和 related_entity_refs 必须留空，"
        "它只安排一次再次遇见，不会周期重复或偷渡新的主体意义。"
        "related_entity_refs 只记录你明确给出的稳定对象引用，不能由系统猜测。"
        "list 只给内容中立的索引；read 按稳定 continuation 无损读取完整公开内容。"
    )
    chatter_allow: ClassVar[list[str]] = ["life_engine_internal", "life_chatter"]

    async def execute(
        self,
        action: Annotated[
            InitiativeToolAction,
            "hold/rewrite/reencounter/release/list/read",
        ],
        seed_id: Annotated[str, "线索 ID；hold/list 可留空，read 必填"] = "",
        expected_revision: Annotated[int, "现有 revision；hold 必须是 0"] = 0,
        statement: Annotated[
            str,
            "主体愿意长期看见的公开意向表述；reencounter 必须留空",
        ] = "",
        related_entity_refs: Annotated[
            list[str] | None,
            "明确相关的稳定对象引用；不是发送目标或平台流；reencounter 必须留空",
        ] = None,
        reencounter_after_minutes: Annotated[
            int,
            "reencounter 时隔多少分钟再次遇见一次；不会自动循环",
        ] = 0,
        include_released: Annotated[bool, "list 是否包含已释放线索"] = False,
        continuation: Annotated[str, "list 上一页 continuation"] = "",
        max_bytes: Annotated[int | None, "list 投影字节预算"] = None,
    ) -> tuple[bool, str | dict[str, object]]:
        try:
            service, actor = _service_actor(self)
            if action == "list":
                views = await service.list_initiative_seeds(
                    include_released=include_released
                )
                items = [initiative_seed_summary(view) for view in views]
                projected = project_bounded_items(
                    projection_name="initiative-seed-list",
                    task_name=getattr(self, "_runtime_task_name", ""),
                    requested_max_bytes=max_bytes,
                    binding={"include_released": include_released},
                    frontier={"count": len(items), "sha256": sha256_json(items)},
                    base_payload={"authority": "subject_initiative"},
                    items_key="seeds",
                    items=items,
                    item_refs=[
                        f"{item['seed_id']}:revision:{item['revision']}" for item in items
                    ],
                    continuation=continuation,
                )
                return True, projected
            if action == "read":
                identity = str(seed_id or "").strip()
                if not identity:
                    raise ValueError("seed_id is required for read")
                view = await service.get_initiative_seed(identity)
                if view is None:
                    raise ValueError("initiative seed does not exist")
                return True, project_initiative_seed_content(
                    view,
                    continuation=continuation,
                    max_bytes=max_bytes,
                )
            source = _source_occurrence(self)
            material = "\0".join(
                (
                    action,
                    str(seed_id or ""),
                    str(expected_revision),
                    str(statement or ""),
                    "\0".join(related_entity_refs or ()),
                    str(reencounter_after_minutes),
                )
            )
            occurrence = _decision_occurrence(self, material)
            identity = str(seed_id or "").strip()
            if action == "hold" and not identity:
                identity = "initiative:seed:" + occurrence.rsplit(":", 1)[-1]
            command = InitiativeSeedCommand(
                occurrence_id=occurrence,
                seed_id=identity,
                action=action,
                actor_consciousness_instance_id=actor,
                source_instance_id=_source_instance(self, actor),
                source_occurrence_ids=(source,),
                causation_occurrence_id=source,
                expected_revision=int(expected_revision),
                public_statement=statement,
                related_entity_refs=tuple(related_entity_refs or ()),
                occurred_at=_occurred_at(self),
                reencounter_after_minutes=int(reencounter_after_minutes),
            )
            commit = await service.decide_initiative_seed(command)
            return True, {
                "seed_id": commit.seed_id,
                "revision": commit.revision,
                "status": commit.status,
                "event_id": commit.event_id,
                "idempotent_replay": commit.idempotent_replay,
            }
        except InitiativeConflict as exc:
            return False, {
                "error": "InitiativeConflict",
                "seed_id": exc.seed_id,
                "current_revision": exc.current_revision,
                "recoverable": True,
            }
        except (KeyError, OSError, PermissionError, RuntimeError, TypeError, ValueError) as exc:
            return False, f"主体主动线索操作失败: {type(exc).__name__}"


class LifeEngineReachabilityTool(BaseTool):
    """Inspect physical delivery surfaces without choosing for the subject."""

    tool_name = "nucleus_reachability"
    tool_description = (
        "只读查看当前已登记的对象引用和可达表面。列表按稳定技术标识排列，不按"
        "最近活跃、当前聊天、分数或重要性排序；同名账号不会被合并。只有明确的"
        "canonical_person_key 才代表跨平台同一个人。查询结果本身不构成行动决定。"
    )
    chatter_allow: ClassVar[list[str]] = ["life_engine_internal", "life_chatter"]

    async def execute(
        self,
        audience_ref: Annotated[
            str,
            "可选：只查看这个完整 audience_ref 的表面；不得填昵称猜测",
        ] = "",
        continuation: Annotated[str, "上一页 continuation"] = "",
        max_bytes: Annotated[int | None, "投影字节预算"] = None,
    ) -> tuple[bool, str | dict[str, object]]:
        try:
            surfaces = await load_reachable_surfaces()
            audience = str(audience_ref or "").strip()
            if audience:
                surfaces = tuple(
                    item for item in surfaces if item.audience_ref == audience
                )
            items = [item.public_projection() for item in surfaces]
            projected = project_bounded_items(
                projection_name="initiative-reachability",
                task_name=getattr(self, "_runtime_task_name", ""),
                requested_max_bytes=max_bytes,
                binding={"audience_ref": audience},
                frontier={"count": len(items), "sha256": sha256_json(items)},
                base_payload={
                    "ordering": "stable_identity_not_salience",
                    "decision_made": False,
                },
                items_key="surfaces",
                items=items,
                item_refs=[item["surface_ref"] for item in items],
                continuation=continuation,
            )
            return True, projected
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            return False, f"可达表面读取失败: {type(exc).__name__}"


class LifeEngineBeginOutreachTool(BaseTool):
    """Make one explicit outreach decision and wake the chosen surface."""

    tool_name = "nucleus_begin_outreach"
    tool_description = (
        "当你现在明确想对某个对象发起一次表达时使用。audience_ref 与 surface_ref"
        "必须来自 nucleus_reachability；来源场景不会默认、限制或替你选择发送平台。"
        "public_intention 只说明这次想做什么，不写最终消息；目标表达实例会在真实"
        "上下文中重新决定发送或保持沉默。"
    )
    chatter_allow: ClassVar[list[str]] = ["life_engine_internal", "life_chatter"]

    async def execute(
        self,
        audience_ref: Annotated[str, "完整 audience_ref"],
        surface_ref: Annotated[str, "同一对象下明确选择的完整 surface_ref"],
        public_intention: Annotated[str, "本次主体明确选择的公开行动意向；不是最终话术"],
        seed_id: Annotated[str, "可选：承接的 InitiativeSeed ID"] = "",
        seed_revision: Annotated[int, "填写 seed_id 时必须同时给出准确 revision"] = 0,
    ) -> tuple[bool, str | dict[str, object]]:
        try:
            service, actor = _service_actor(self)
            source = _source_occurrence(self)
            material = "\0".join(
                (
                    audience_ref,
                    surface_ref,
                    public_intention,
                    seed_id,
                    str(seed_revision),
                )
            )
            command = InitiativeOutreachCommand(
                occurrence_id=_decision_occurrence(self, material),
                actor_consciousness_instance_id=actor,
                source_instance_id=_source_instance(self, actor),
                source_occurrence_ids=(source,),
                causation_occurrence_id=source,
                audience_ref=audience_ref,
                surface_ref=surface_ref,
                public_intention=public_intention,
                occurred_at=_occurred_at(self),
                seed_id=seed_id,
                seed_revision=int(seed_revision),
            )
            return True, await service.begin_initiative_outreach(command)
        except (KeyError, OSError, PermissionError, RuntimeError, TypeError, ValueError) as exc:
            return False, f"主动外联开始失败: {type(exc).__name__}"


INITIATIVE_TOOLS = [
    LifeEngineManageInitiativeSeedTool,
    LifeEngineReachabilityTool,
    LifeEngineBeginOutreachTool,
]

__all__ = [
    "INITIATIVE_TOOLS",
    "InitiativeToolAction",
    "LifeEngineBeginOutreachTool",
    "LifeEngineManageInitiativeSeedTool",
    "LifeEngineReachabilityTool",
]
