"""The only model-facing surface for durable proactive state."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Annotated, ClassVar, Literal

from src.app.plugin_system.base import BaseTool

from ..attention_threads import (
    AttentionThreadCommand,
    AttentionThreadConflict,
    AttentionThreadPageQuery,
)
from ..attention_threads.contracts import (
    ATTENTION_THREAD_MAX_PAGE_BYTES,
    ATTENTION_THREAD_MIN_PAGE_BYTES,
)
from ..initiative.contracts import (
    InitiativeConflict,
    InitiativeOutreachCommand,
    InitiativeSeedCommand,
)
from ..initiative.projection import (
    initiative_seed_summary,
    project_initiative_seed_content,
)
from ..initiative.reachability import load_reachable_surfaces
from ..inner_dialogue.protocol import (
    InnerDialogueConflict,
    InnerDialogueReturnBlocked,
    InnerDialogueReturnRequiresHeartbeat,
    inner_dialogue_summary,
)
from ..tools.bounded_projection import (
    project_bounded_items,
    project_bounded_text,
    sha256_json,
)

ProactiveQueryResource = Literal[
    "attention",
    "initiative",
    "reachability",
    "inner_dialogue",
]
ProactiveCommandAction = Literal[
    "attention.open",
    "attention.note",
    "attention.pause",
    "attention.resume",
    "attention.close",
    "initiative.hold",
    "initiative.rewrite",
    "initiative.reencounter",
    "initiative.release",
    "outreach.begin",
    "inner.return",
]


def _service_actor(tool: BaseTool):
    from ..service.registry import get_life_engine_service

    service = get_life_engine_service()
    if service is None:
        raise RuntimeError("LifeEngineServiceUnavailable")
    stream_id = str(tool.get_current_stream_id() or "").strip()
    registry = service.consciousness_registry
    instance = registry.get_for_stream(stream_id) if stream_id else None
    if instance is None and str(
        getattr(tool, "_runtime_task_name", "") or ""
    ).strip() == "core":
        instance = registry.get("chat_global")
    if instance is None or not instance.is_active:
        raise PermissionError("ProactiveActorIsNotActive")
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
    raise RuntimeError("ProactiveSourceOccurrenceRequired")


def _source_instance(tool: BaseTool, actor: str) -> str:
    bound = str(
        getattr(tool, "_life_source_instance_id", "") or ""
    ).strip()
    if bound:
        return bound
    trigger = tool.trigger_message
    extra = getattr(trigger, "extra", {}) or {}
    if not isinstance(extra, dict):
        extra = {}
    explicit = str(
        getattr(trigger, "source_instance_id", "")
        or extra.get("source_instance_id")
        or extra.get("consciousness_instance_id")
        or ""
    ).strip()
    if explicit:
        return explicit
    task_name = str(getattr(tool, "_runtime_task_name", "") or "").strip()
    stream_id = str(tool.get_current_stream_id() or "").strip()
    if task_name == "core" and stream_id == "chat_global" and actor == "chat_global":
        return actor
    raise RuntimeError("ProactiveSourceInstanceRequired")


def _decision_occurrence(tool: BaseTool) -> str:
    tool_call_id = str(getattr(tool, "_tool_call_id", "") or "").strip()
    if not tool_call_id:
        raise RuntimeError("ProactiveToolCallIdentityRequired")
    source = _source_occurrence(tool)
    digest = hashlib.sha256(
        f"{tool_call_id}\0{tool.get_current_stream_id()}\0{source}".encode()
    ).hexdigest()
    return f"proactive:decision:{digest}"


def _occurred_at(tool: BaseTool) -> str:
    bound = str(getattr(tool, "_life_source_occurred_at", "") or "").strip()
    if bound:
        parsed = datetime.fromisoformat(bound)
        if parsed.tzinfo is None:
            raise RuntimeError("ProactiveSourceTimeMustIncludeTimezone")
        return parsed.astimezone(UTC).isoformat()
    value = getattr(tool.trigger_message, "time", None)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, UTC).isoformat()
    if isinstance(value, datetime):
        parsed = value if value.tzinfo else value.replace(tzinfo=UTC)
        return parsed.astimezone(UTC).isoformat()
    raise RuntimeError("ProactiveSourceTimeRequired")


class LifeEngineProactiveQueryTool(BaseTool):
    """Read one bounded projection without making a subject decision."""

    tool_name = "nucleus_proactive_query"
    tool_description = (
        "只读查看统一主动系统。resource=attention 查看主体明确保留的持续关注；"
        "initiative 查看未来可能行动的公开意向；reachability 查看已登记对象和"
        "物理表面；inner_dialogue 查看表达层沉下来、尚未交还的内心对话。"
        "读取、未读取、忽略都不会改变任何状态，也不代表重要性。"
        "record_id 留空时列出有界索引；填写后读取该记录。"
    )
    chatter_allow: ClassVar[list[str]] = ["life_engine_internal", "life_chatter"]

    async def execute(
        self,
        resource: Annotated[
            ProactiveQueryResource,
            "attention / initiative / reachability / inner_dialogue",
        ],
        record_id: Annotated[
            str,
            "可选完整 thread_id/seed_id/inner_dialogue receipt_id；留空返回列表",
        ] = "",
        include_inactive: Annotated[
            bool,
            "是否包含 closed/released 历史记录",
        ] = False,
        audience_ref: Annotated[
            str,
            "reachability 可选对象引用；不得填写昵称猜测",
        ] = "",
        continuation: Annotated[str, "上一页 continuation"] = "",
        offset_bytes: Annotated[
            int,
            "attention 精确正文读取的 UTF-8 字节偏移",
        ] = 0,
        max_bytes: Annotated[int | None, "本次投影字节预算"] = None,
    ) -> tuple[bool, str | dict[str, object]]:
        try:
            service, actor = _service_actor(self)
            if resource not in {
                "attention",
                "initiative",
                "reachability",
                "inner_dialogue",
            }:
                raise ValueError("unsupported proactive query resource")
            identity = str(record_id or "").strip()
            budget = int(max_bytes or 16 * 1024)
            if resource == "attention":
                if not identity:
                    budget = max(
                        ATTENTION_THREAD_MIN_PAGE_BYTES,
                        min(budget, ATTENTION_THREAD_MAX_PAGE_BYTES),
                    )
                    statuses = (
                        ("open", "paused", "closed")
                        if include_inactive
                        else ("open", "paused")
                    )
                    page = await service.page_attention_threads(
                        AttentionThreadPageQuery(
                            statuses=statuses,
                            continuation=continuation,
                            limit=32,
                            max_bytes=budget,
                            projection_kind="proactive_query",
                            focus_instance_id=actor,
                        )
                    )
                    return True, {
                        "resource": "attention",
                        "content": page.content,
                        "source_frontier": page.source_frontier,
                        "projection_sha256": page.projection_sha256,
                        "delivered_bytes": page.delivered_bytes,
                        "omitted_count": page.omitted_count,
                        "continuation": page.continuation,
                    }
                view = await service.proactive_authority.get_attention(identity)
                if view is None:
                    raise ValueError("attention thread does not exist")
                chunk = await service.proactive_authority.read_attention_statement(
                    view.statement_event_id,
                    offset_bytes=max(0, int(offset_bytes)),
                    max_bytes=max(256, min(budget, 256 * 1024)),
                )
                return True, {
                    "resource": "attention",
                    "thread_id": view.thread_id,
                    "status": view.status,
                    "revision": view.revision,
                    "statement_event_id": view.statement_event_id,
                    "statement_sha256": chunk.statement_sha256,
                    "offset_bytes": chunk.offset_bytes,
                    "next_offset_bytes": chunk.next_offset_bytes,
                    "total_bytes": chunk.total_bytes,
                    "content": chunk.content,
                    "complete": chunk.complete,
                }

            if resource == "initiative":
                if identity:
                    view = await service.get_initiative_seed(identity)
                    if view is None:
                        raise ValueError("initiative seed does not exist")
                    return True, project_initiative_seed_content(
                        view,
                        continuation=continuation,
                        max_bytes=max_bytes,
                    )
                views = await service.list_initiative_seeds(
                    include_released=include_inactive
                )
                items = [initiative_seed_summary(view) for view in views]
                return True, project_bounded_items(
                    projection_name="proactive-initiative-list",
                    task_name=getattr(self, "_runtime_task_name", ""),
                    requested_max_bytes=max_bytes,
                    binding={"include_released": include_inactive},
                    frontier={"count": len(items), "sha256": sha256_json(items)},
                    base_payload={"resource": "initiative"},
                    items_key="seeds",
                    items=items,
                    item_refs=[
                        f"{item['seed_id']}:revision:{item['revision']}"
                        for item in items
                    ],
                    continuation=continuation,
                )

            if resource == "inner_dialogue":
                if identity:
                    record = await service.get_inner_dialogue_record(identity)
                    if record is None:
                        raise ValueError("inner dialogue receipt does not exist")
                    return True, project_bounded_text(
                        projection_name="proactive-inner-dialogue-content",
                        task_name=getattr(self, "_runtime_task_name", ""),
                        requested_max_bytes=max_bytes,
                        binding={"receipt_id": identity},
                        frontier={
                            "receipt_id": record.receipt_id,
                            "thought_sha256": record.thought_sha256,
                            "status": record.status,
                        },
                        base_payload={
                            "resource": "inner_dialogue",
                            **inner_dialogue_summary(record),
                        },
                        content=record.thought,
                        content_ref=f"inner-dialogue:{record.receipt_id}",
                        continuation=continuation,
                    )
                records = await service.list_inner_dialogue_records()
                items = [inner_dialogue_summary(record) for record in records]
                return True, project_bounded_items(
                    projection_name="proactive-inner-dialogue-list",
                    task_name=getattr(self, "_runtime_task_name", ""),
                    requested_max_bytes=max_bytes,
                    binding={"status": "open"},
                    frontier={"count": len(items), "sha256": sha256_json(items)},
                    base_payload={"resource": "inner_dialogue"},
                    items_key="receipts",
                    items=items,
                    item_refs=[item["receipt_id"] for item in items],
                    continuation=continuation,
                )

            surfaces = await load_reachable_surfaces()
            audience = str(audience_ref or "").strip()
            if audience:
                surfaces = tuple(
                    item for item in surfaces if item.audience_ref == audience
                )
            items = [item.public_projection() for item in surfaces]
            return True, project_bounded_items(
                projection_name="proactive-reachability",
                task_name=getattr(self, "_runtime_task_name", ""),
                requested_max_bytes=max_bytes,
                binding={"audience_ref": audience},
                frontier={"count": len(items), "sha256": sha256_json(items)},
                base_payload={
                    "resource": "reachability",
                    "ordering": "stable_identity_not_salience",
                    "decision_made": False,
                },
                items_key="surfaces",
                items=items,
                item_refs=[item["surface_ref"] for item in items],
                continuation=continuation,
            )
        except (
            KeyError,
            OSError,
            PermissionError,
            RuntimeError,
            TypeError,
            ValueError,
        ) as exc:
            return False, {
                "error": type(exc).__name__,
                "error_message": str(exc),
                "operation": "proactive_query",
                "mutated": False,
            }


class LifeEngineProactiveCommandTool(BaseTool):
    """Commit one explicit subject decision and return its durable receipt."""

    tool_name = "nucleus_proactive_command"
    tool_description = (
        "统一主动系统的唯一写入口。attention.* 管理持续关注；initiative.* 管理"
        "未来可能行动的公开意向；outreach.begin 明确选择对象和物理表面；"
        "inner.return 把一条内心对话回声交还给沉下去的那个表达窗口。"
        "所有写入都要求活跃意识、稳定来源、expected_revision 和不可变事件回执。"
        "基础设施不会按分数、时间、容量或最近聊天替你创建、推进或发送。"
    )
    chatter_allow: ClassVar[list[str]] = ["life_engine_internal", "life_chatter"]

    async def execute(
        self,
        action: Annotated[ProactiveCommandAction, "要提交的显式主动决定"],
        record_id: Annotated[
            str,
            "AttentionThread、InitiativeSeed 或 inner_dialogue receipt 完整 ID；新建可留空",
        ] = "",
        expected_revision: Annotated[
            int,
            "据以决定的当前 revision；新建必须为 0",
        ] = 0,
        statement: Annotated[
            str,
            "愿意让未来意识实例看到的公开表述；inner.return 写给表达层的第一人称回声；"
            "pause/resume/reencounter 留空",
        ] = "",
        related_entity_refs: Annotated[
            list[str] | None,
            "initiative 明确相关的稳定对象引用；不得猜测",
        ] = None,
        reencounter_after_minutes: Annotated[
            int,
            "initiative.reencounter 的一次性再次遇见延迟",
        ] = 0,
        audience_ref: Annotated[
            str,
            "outreach.begin 使用 query 返回的完整对象引用",
        ] = "",
        surface_ref: Annotated[
            str,
            "outreach.begin 使用 query 返回的完整物理表面引用",
        ] = "",
        seed_id: Annotated[str, "outreach.begin 可选承接的 InitiativeSeed ID"] = "",
        seed_revision: Annotated[int, "承接 seed 时的准确 revision"] = 0,
    ) -> tuple[bool, str | dict[str, object]]:
        try:
            service, actor = _service_actor(self)
            if action not in {
                "attention.open",
                "attention.note",
                "attention.pause",
                "attention.resume",
                "attention.close",
                "initiative.hold",
                "initiative.rewrite",
                "initiative.reencounter",
                "initiative.release",
                "outreach.begin",
                "inner.return",
            }:
                raise ValueError("unsupported proactive command action")
            source = _source_occurrence(self)
            occurrence = _decision_occurrence(self)
            occurred_at = _occurred_at(self)
            source_instance = _source_instance(self, actor)

            if action.startswith("attention."):
                transition = action.split(".", 1)[1]
                identity = str(record_id or "").strip()
                if transition == "open" and not identity:
                    identity = "attention:thread:" + occurrence.rsplit(":", 1)[-1]
                command = AttentionThreadCommand(
                    occurrence_id=occurrence,
                    thread_id=identity,
                    action=transition,
                    actor_consciousness_instance_id=actor,
                    source_instance_id=source_instance,
                    source_occurrence_ids=(source,),
                    causation_occurrence_id=source,
                    expected_revision=int(expected_revision),
                    public_statement=(
                        "" if transition in {"pause", "resume"} else statement
                    ),
                    occurred_at=occurred_at,
                )
                commit = await service.decide_attention_thread(command)
                return True, {
                    "authority_committed": True,
                    "record_family": "attention",
                    "record_id": commit.thread_id,
                    "revision": commit.revision,
                    "status": commit.status,
                    "event_id": commit.event_id,
                    "idempotent_replay": commit.idempotent_replay,
                }

            if action.startswith("initiative."):
                transition = action.split(".", 1)[1]
                identity = str(record_id or "").strip()
                if transition == "hold" and not identity:
                    identity = "initiative:seed:" + occurrence.rsplit(":", 1)[-1]
                command = InitiativeSeedCommand(
                    occurrence_id=occurrence,
                    seed_id=identity,
                    action=transition,
                    actor_consciousness_instance_id=actor,
                    source_instance_id=source_instance,
                    source_occurrence_ids=(source,),
                    causation_occurrence_id=source,
                    expected_revision=int(expected_revision),
                    public_statement=statement,
                    related_entity_refs=tuple(related_entity_refs or ()),
                    occurred_at=occurred_at,
                    reencounter_after_minutes=int(reencounter_after_minutes),
                )
                commit = await service.decide_initiative_seed(command)
                return True, {
                    "authority_committed": True,
                    "record_family": "initiative",
                    "record_id": commit.seed_id,
                    "revision": commit.revision,
                    "status": commit.status,
                    "event_id": commit.event_id,
                    "idempotent_replay": commit.idempotent_replay,
                }

            if action == "inner.return":
                if str(getattr(self, "_runtime_task_name", "") or "").strip() != "core":
                    raise InnerDialogueReturnRequiresHeartbeat()
                return True, await service.return_inner_dialogue(
                    receipt_id=str(record_id or "").strip(),
                    statement=statement,
                    occurrence_id=occurrence,
                    actor_consciousness_instance_id=actor,
                    source_instance_id=source_instance,
                    causation_id=source,
                )

            command = InitiativeOutreachCommand(
                occurrence_id=occurrence,
                actor_consciousness_instance_id=actor,
                source_instance_id=source_instance,
                source_occurrence_ids=(source,),
                causation_occurrence_id=source,
                audience_ref=audience_ref,
                surface_ref=surface_ref,
                public_intention=statement,
                occurred_at=occurred_at,
                seed_id=seed_id,
                seed_revision=int(seed_revision),
            )
            return True, await service.begin_initiative_outreach(command)
        except InnerDialogueConflict as exc:
            return False, {
                "error": "InnerDialogueConflict",
                "record_id": exc.receipt_id,
                "occurrence_id": exc.occurrence_id,
                "authority_committed": False,
                "recoverable": True,
            }
        except InnerDialogueReturnBlocked as exc:
            return False, {
                "error": "InnerDialogueReturnBlocked",
                "error_message": exc.reason,
                "record_id": exc.receipt_id,
                "authority_committed": False,
            }
        except InnerDialogueReturnRequiresHeartbeat as exc:
            return False, {
                "error": type(exc).__name__,
                "error_message": str(exc),
                "operation": action,
                "authority_committed": False,
            }
        except AttentionThreadConflict as exc:
            return False, {
                "error": "AttentionThreadConflict",
                "record_id": exc.thread_id,
                "current_revision": exc.current_revision,
                "thread_exists": exc.thread_exists,
                "authority_committed": False,
                "recoverable": True,
            }
        except InitiativeConflict as exc:
            return False, {
                "error": "InitiativeConflict",
                "record_id": exc.seed_id,
                "current_revision": exc.current_revision,
                "authority_committed": False,
                "recoverable": True,
            }
        except (
            KeyError,
            OSError,
            PermissionError,
            RuntimeError,
            TypeError,
            ValueError,
        ) as exc:
            return False, {
                "error": type(exc).__name__,
                "error_message": str(exc),
                "operation": action,
                "authority_committed": False,
            }


PROACTIVE_TOOLS = [
    LifeEngineProactiveQueryTool,
    LifeEngineProactiveCommandTool,
]

__all__ = [
    "PROACTIVE_TOOLS",
    "LifeEngineProactiveCommandTool",
    "LifeEngineProactiveQueryTool",
    "ProactiveCommandAction",
    "ProactiveQueryResource",
]
