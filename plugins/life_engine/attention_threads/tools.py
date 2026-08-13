"""Explicit subject-facing tool for durable attention decisions."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Annotated, ClassVar, Literal
from uuid import uuid4

from src.app.plugin_system.base import BaseTool

from .contracts import (
    AttentionThreadCommand,
    AttentionThreadConflict,
    AttentionThreadPageQuery,
)

AttentionToolAction = Literal[
    "open",
    "note",
    "pause",
    "resume",
    "close",
    "list",
]


def _service_and_actor(tool: BaseTool):
    from ..service.registry import get_life_engine_service

    service = get_life_engine_service()
    if service is None:
        raise RuntimeError("LifeEngineServiceUnavailable")
    actor = service.resolve_consciousness_instance(tool.get_current_stream_id())
    instance = service.consciousness_registry.get(actor)
    if instance is None or not instance.is_active:
        raise PermissionError("AttentionThreadActorIsNotActive")
    return service, actor


def _source_occurrence(tool: BaseTool) -> str:
    message_id = str(
        getattr(tool.trigger_message, "message_id", "") or ""
    ).strip()
    if message_id:
        return f"message:{message_id}"
    return f"attention:tool-source:{uuid4().hex}"


def _decision_occurrence(tool: BaseTool, material: str) -> str:
    message_id = str(
        getattr(tool.trigger_message, "message_id", "") or ""
    ).strip()
    if not message_id:
        return f"attention:decision:{uuid4().hex}"
    digest = hashlib.sha256(
        f"{message_id}\0{tool.get_current_stream_id()}\0{material}".encode()
    ).hexdigest()
    return f"attention:decision:{digest}"


def _occurred_at(tool: BaseTool) -> str:
    value = getattr(tool.trigger_message, "time", None)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, UTC).isoformat()
    if isinstance(value, datetime):
        parsed = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        return parsed.astimezone(UTC).isoformat()
    return datetime.now(UTC).isoformat()


class LifeEngineManageAttentionThreadTool(BaseTool):
    """Open, annotate, pause, resume, close, or list attention threads."""

    tool_name: str = "nucleus_manage_attention_thread"
    tool_description: str = (
        "管理你明确选择长期保留的‘持续关注线索’。它不是待办、评分或隐藏思维记录。"
        "open/note/close 的 statement 只能写你愿意公开给未来意识实例读取的简洁表述，"
        "不要填写内部推理过程。pause/resume/close 只会在你显式选择时发生；"
        "系统不会按时间、热度或容量替你改变状态。list 返回有界、可追溯的线索引用与 revision。"
    )
    chatter_allow: ClassVar[list[str]] = ["life_engine_internal"]

    async def execute(
        self,
        action: Annotated[
            AttentionToolAction,
            "操作：open / note / pause / resume / close / list",
        ],
        thread_id: Annotated[
            str,
            "线索 ID；除 open/list 外必填。必须使用 list 返回的完整 thread_ref 原值"
            "（含前缀），不要手工去掉或改写前缀。open 留空时由系统生成稳定 ID",
        ] = "",
        expected_revision: Annotated[
            int,
            "你读取并据此作出决定的当前 revision；open 必须为 0",
        ] = 0,
        statement: Annotated[
            str,
            "愿意持久公开给未来意识实例的简洁表述；open/note/close 必填",
        ] = "",
        include_closed: Annotated[
            bool,
            "list 时是否同时读取已关闭线索",
        ] = False,
    ) -> tuple[bool, str | dict[str, object]]:
        try:
            service, actor = _service_and_actor(self)
            if action == "list":
                statuses = ("open", "paused", "closed") if include_closed else (
                    "open",
                    "paused",
                )
                page = await service.page_attention_threads(
                    AttentionThreadPageQuery(
                        statuses=statuses,
                        limit=32,
                        max_bytes=32 * 1024,
                        projection_kind="subject_tool",
                        focus_instance_id=actor,
                    )
                )
                return True, {
                    "action": "list",
                    "content": page.content,
                    "source_frontier": page.source_frontier,
                    "projection_sha256": page.projection_sha256,
                    "delivered_bytes": page.delivered_bytes,
                    "omitted_count": page.omitted_count,
                    "continuation": page.continuation,
                }

            source_occurrence = _source_occurrence(self)
            material = "\0".join(
                (
                    action,
                    str(thread_id or "").strip(),
                    str(int(expected_revision)),
                    str(statement or ""),
                )
            )
            occurrence_id = _decision_occurrence(self, material)
            identity = str(thread_id or "").strip()
            if action == "open" and not identity:
                identity = "attention:thread:" + occurrence_id.rsplit(":", 1)[-1]
            command = AttentionThreadCommand(
                occurrence_id=occurrence_id,
                thread_id=identity,
                action=action,
                actor_consciousness_instance_id=actor,
                source_instance_id=actor,
                source_occurrence_ids=(source_occurrence,),
                causation_occurrence_id=source_occurrence,
                expected_revision=int(expected_revision),
                public_statement=(
                    "" if action in {"pause", "resume"} else str(statement or "")
                ),
                occurred_at=_occurred_at(self),
            )
            commit = await service.decide_attention_thread(command)
            return True, {
                "action": action,
                "thread_id": commit.thread_id,
                "revision": commit.revision,
                "status": commit.status,
                "event_id": commit.event_id,
                "idempotent_replay": commit.idempotent_replay,
            }
        except AttentionThreadConflict as exc:
            current = exc.current_revision
            exists = exc.thread_exists
            if exists:
                detail = (
                    f"线索当前 revision 已是 {current}，"
                    "请用该值作为 expected_revision 重新提交"
                )
            elif exists is False:
                detail = (
                    "线索不存在，thread_id 必须使用 list 返回的完整 thread_ref 原值"
                )
            else:
                detail = "线索状态未知，请重新调用 list 确认 thread_ref 与 revision"
            return False, {
                "error": "AttentionThreadConflict",
                "detail": detail,
                "thread_id": exc.thread_id,
                "current_revision": current,
                "thread_exists": exists,
                "recoverable": True,
                "hint": (
                    "重新调用 list 获取最新 thread_ref 与 revision；"
                    "thread_id 使用返回的完整 thread_ref 原值，"
                    "expected_revision 使用 current_revision"
                ),
            }
        except (KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
            return False, f"持续关注线索操作失败: {type(exc).__name__}"


ATTENTION_THREAD_TOOLS = [LifeEngineManageAttentionThreadTool]

__all__ = [
    "ATTENTION_THREAD_TOOLS",
    "AttentionToolAction",
    "LifeEngineManageAttentionThreadTool",
]
