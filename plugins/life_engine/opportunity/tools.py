"""Subject-facing governance tools for the canonical opportunity runtime.

The tools are intentionally thin. They bind the active consciousness actor and
the immutable source/tool-call occurrence, then delegate to
``LifeEngineService``. A workflow Skill is subject-owned prose; it never becomes
executable Python and cannot add operations beyond a capability manifest.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Annotated, Any, ClassVar, Literal, get_args

from src.app.plugin_system.base import BaseTool

OpportunityQueryResource = Literal[
    "protocol",
    "operation_schema",
    "catalog",
    "providers",
    "capability",
    "manual",
    "default_skill",
    "opportunities",
    "opportunity",
    "workflow",
    "deliveries",
    "uninstall_impact",
    "history",
    "history_reason",
    "health",
]

OpportunityCommandAction = Literal[
    "capability.install",
    "capability.pause",
    "capability.resume",
    "capability.uninstall",
    "capability.reinstall",
    "capability.bind_workflow",
    "workflow.replace",
    "opportunity.open",
    "opportunity.pause",
    "opportunity.resume",
    "opportunity.close",
    "opportunity.schedule",
    "opportunity.snooze",
]


def _service_actor(tool: BaseTool) -> tuple[Any, str]:
    from ..service.registry import get_life_engine_service

    service = get_life_engine_service()
    if service is None:
        raise RuntimeError("LifeEngineServiceUnavailable")
    stream_id = str(tool.get_current_stream_id() or "").strip()
    registry = service.consciousness_registry
    instance = registry.get_for_stream(stream_id) if stream_id else None
    if (
        instance is None
        and stream_id == "chat_global"
        and str(getattr(tool, "_runtime_task_name", "") or "").strip() == "core"
    ):
        instance = registry.get("chat_global")
    if instance is None or not instance.is_active:
        raise PermissionError("OpportunityActorIsNotActive")
    return service, str(instance.instance_id)


def _source_occurrence(tool: BaseTool) -> str:
    bound = str(getattr(tool, "_life_source_occurrence_id", "") or "").strip()
    if bound:
        return bound
    message_id = str(getattr(tool.trigger_message, "message_id", "") or "").strip()
    if message_id:
        return f"message:{message_id}"
    tool_call_id = str(getattr(tool, "_tool_call_id", "") or "").strip()
    if tool_call_id:
        return f"tool-call:{tool_call_id}"
    raise RuntimeError("OpportunitySourceOccurrenceRequired")


def _turn_scope_source_instance_id(extra: object) -> str:
    """Read instance attribution already written onto this turn's trigger extra."""

    if not isinstance(extra, dict):
        return ""
    turn_scope = extra.get("life_turn_scope")
    if not isinstance(turn_scope, dict):
        turn_scope = {}
    return str(
        extra.get("source_instance_id")
        or extra.get("consciousness_instance_id")
        or turn_scope.get("source_instance_id")
        or turn_scope.get("consciousness_instance_id")
        or ""
    ).strip()


def _source_instance(tool: BaseTool, actor: str) -> str:
    bound = str(getattr(tool, "_life_source_instance_id", "") or "").strip()
    if bound:
        return bound
    trigger = tool.trigger_message
    extra = getattr(trigger, "extra", {}) or {}
    explicit = str(
        getattr(trigger, "source_instance_id", "")
        or _turn_scope_source_instance_id(extra)
        or ""
    ).strip()
    if explicit:
        return explicit
    task_name = str(getattr(tool, "_runtime_task_name", "") or "").strip()
    stream_id = str(tool.get_current_stream_id() or "").strip()
    if task_name == "core" and stream_id == "chat_global" and actor == "chat_global":
        return actor
    raise RuntimeError("OpportunitySourceInstanceRequired")


def _decision_occurrence(tool: BaseTool) -> str:
    tool_call_id = str(getattr(tool, "_tool_call_id", "") or "").strip()
    if not tool_call_id:
        raise RuntimeError("OpportunityToolCallIdentityRequired")
    source = _source_occurrence(tool)
    digest = hashlib.sha256(
        f"{tool_call_id}\0{tool.get_current_stream_id()}\0{source}".encode()
    ).hexdigest()
    return f"opportunity:decision:{digest}"


def _occurred_at(tool: BaseTool) -> str:
    bound = str(getattr(tool, "_life_source_occurred_at", "") or "").strip()
    if bound:
        parsed = datetime.fromisoformat(bound)
        if parsed.tzinfo is None:
            raise RuntimeError("OpportunitySourceTimeMustIncludeTimezone")
        return parsed.astimezone(UTC).isoformat()
    value = getattr(tool.trigger_message, "time", None)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, UTC).isoformat()
    if isinstance(value, datetime):
        parsed = value if value.tzinfo else value.replace(tzinfo=UTC)
        return parsed.astimezone(UTC).isoformat()
    raise RuntimeError("OpportunitySourceTimeRequired")


def _error_payload(exc: BaseException) -> dict[str, object]:
    return {
        "authority_committed": False,
        "error": type(exc).__name__,
    }


def _service_method(service: Any, name: str) -> Any:
    method = getattr(service, name, None)
    if not callable(method):
        raise RuntimeError("OpportunityRuntimeUnavailable")
    return method


def _arguments(payload: dict[str, Any] | None) -> dict[str, Any]:
    if payload is not None and not isinstance(payload, dict):
        raise TypeError("OpportunityArgumentsMustBeObject")
    copied = dict(payload or {})
    reserved = {
        "actor_consciousness_instance_id",
        "source_instance_id",
        "source_occurrence_id",
        "decision_occurrence_id",
        "occurred_at",
        "caller_tool",
        "runtime",
        "writer_claim",
    }
    if copied.keys() & reserved:
        raise ValueError("OpportunityArgumentsCannotReplaceRuntimeIdentity")
    return copied


class LifeEngineOpportunityQueryTool(BaseTool):
    """Read a bounded opportunity/capability projection without mutation."""

    tool_name = "nucleus_opportunity_query"
    tool_description = (
        "只读查看你可管理的机会与认知能力。protocol 查看操作参数（record_id=动作名）；"
        "operation_schema 查看能力的真实调用参数（record_id=能力ID，operation=操作名）；"
        "catalog/capability 查看工程能力；"
        "opportunities/opportunity 查看当前机会；workflow 查看你选择的运作方式；"
        "deliveries/history/health 查看有界技术事实；uninstall_impact 在卸载前读取"
        "精确影响摘要。读取、忽略或沉默都不会改变状态。"
    )
    chatter_allow: ClassVar[list[str]] = ["life_engine_internal", "life_chatter"]

    async def execute(
        self,
        resource: Annotated[OpportunityQueryResource, "要读取的资源"],
        record_id: Annotated[str, "可选 capability/opportunity/workflow 完整 ID"] = "",
        continuation: Annotated[str, "上一页返回的 continuation token"] = "",
        offset_bytes: Annotated[int, "长文档 UTF-8 起始字节"] = 0,
        max_bytes: Annotated[int, "本页最大 UTF-8 字节数"] = 8192,
        limit: Annotated[int, "本页最多记录数"] = 20,
        revision: Annotated[int, "读取 workflow 的精确 revision"] = 0,
        operation: Annotated[str, "operation_schema 中的真实 operation 名称"] = "",
        action: Annotated[str, "可选 Learning 内层 action 的参数 schema"] = "",
        family: Annotated[
            str, "历史账本 provider/workflow/registration"
        ] = "registration",
    ) -> tuple[bool, str | dict[str, object]]:
        try:
            service, actor = _service_actor(self)
            if resource not in get_args(OpportunityQueryResource):
                raise ValueError("OpportunityQueryResourceUnknown")
            result = await _service_method(service, "query_opportunity_runtime")(
                resource=str(resource),
                record_id=str(record_id or "").strip(),
                continuation=str(continuation or "").strip(),
                offset_bytes=max(0, int(offset_bytes)),
                max_bytes=max(512, min(32 * 1024, int(max_bytes))),
                limit=max(1, min(100, int(limit))),
                revision=int(revision),
                operation=str(operation),
                action=str(action),
                family=family,
                actor_consciousness_instance_id=actor,
            )
            return True, result
        except (
            KeyError,
            OSError,
            PermissionError,
            RuntimeError,
            TypeError,
            ValueError,
        ) as exc:
            return False, _error_payload(exc)


class LifeEngineOpportunityCommandTool(BaseTool):
    """Submit one explicit subject governance decision."""

    tool_name = "nucleus_opportunity_command"
    tool_description = (
        "管理你的机会与可选认知能力：安装、暂停、恢复、修改 workflow、安排或"
        "关闭机会，以及卸载整个能力。actor 由当前活跃意识绑定；所有写入都需要"
        "expected_revision 和不可变来源。卸载不会抹掉历史，也不会扩大机器权限。"
    )
    chatter_allow: ClassVar[list[str]] = ["life_engine_internal", "life_chatter"]

    async def execute(
        self,
        action: Annotated[OpportunityCommandAction, "要提交的治理动作"],
        target_id: Annotated[str, "capability_id 或 opportunity_id"] = "",
        expected_revision: Annotated[int, "据以决定的当前 revision；新建为 0"] = 0,
        arguments: Annotated[dict[str, Any] | None, "动作的结构化参数"] = None,
        reason: Annotated[str, "你做出本次治理决定的原因"] = "",
    ) -> tuple[bool, str | dict[str, object]]:
        try:
            service, actor = _service_actor(self)
            if action not in get_args(OpportunityCommandAction):
                raise ValueError("OpportunityCommandActionUnknown")
            if isinstance(expected_revision, bool) or int(expected_revision) < 0:
                raise ValueError("OpportunityRevisionMustBeNonnegative")
            result = await _service_method(service, "manage_opportunity_runtime")(
                action=str(action),
                target_id=str(target_id or "").strip(),
                expected_revision=int(expected_revision),
                arguments=_arguments(arguments),
                reason=str(reason or ""),
                actor_consciousness_instance_id=actor,
                source_instance_id=_source_instance(self, actor),
                source_occurrence_id=_source_occurrence(self),
                decision_occurrence_id=_decision_occurrence(self),
                occurred_at=_occurred_at(self),
            )
            return True, result
        except (
            KeyError,
            OSError,
            PermissionError,
            RuntimeError,
            TypeError,
            ValueError,
        ) as exc:
            return False, _error_payload(exc)


class LifeEngineCapabilityCallTool(BaseTool):
    """Call one manifest-declared operation through the installed capability."""

    tool_name = "nucleus_capability_call"
    tool_description = (
        "调用一个已安装且启用的认知能力原子操作。operation 必须由该能力的"
        "CAPABILITY.md/manifest 声明；你写下的 Skill 只决定怎么使用，不能新增"
        "权限或副作用。已暂停或卸载的能力会明确拒绝。"
    )
    chatter_allow: ClassVar[list[str]] = ["life_engine_internal", "life_chatter"]

    async def execute(
        self,
        capability_id: Annotated[str, "完整 capability_id"],
        operation: Annotated[str, "manifest 声明的原子 operation ID"],
        arguments: Annotated[dict[str, Any] | None, "原子操作参数"] = None,
    ) -> tuple[bool, str | dict[str, object]]:
        try:
            service, actor = _service_actor(self)
            result = await _service_method(service, "call_opportunity_capability")(
                capability_id=str(capability_id or "").strip(),
                operation=str(operation or "").strip(),
                arguments=_arguments(arguments),
                caller_tool=self,
                actor_consciousness_instance_id=actor,
                source_instance_id=_source_instance(self, actor),
                source_occurrence_id=_source_occurrence(self),
                decision_occurrence_id=_decision_occurrence(self),
                occurred_at=_occurred_at(self),
            )
            if not isinstance(result, dict) or not isinstance(
                result.get("success"), bool
            ):
                raise RuntimeError("CapabilityResultContractInvalid")
            return result["success"], result.get("value")
        except (
            KeyError,
            OSError,
            PermissionError,
            RuntimeError,
            TypeError,
            ValueError,
        ) as exc:
            return False, _error_payload(exc)


OPPORTUNITY_TOOLS = [
    LifeEngineOpportunityQueryTool,
    LifeEngineOpportunityCommandTool,
    LifeEngineCapabilityCallTool,
]

__all__ = [
    "LifeEngineCapabilityCallTool",
    "LifeEngineOpportunityCommandTool",
    "LifeEngineOpportunityQueryTool",
    "OPPORTUNITY_TOOLS",
]
