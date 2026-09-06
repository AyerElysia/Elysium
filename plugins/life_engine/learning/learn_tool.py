"""Capability door for a subject-selected Learning workflow.

Learning is not a fixed heartbeat pipeline. The opportunity runtime admits
one named operation from the exact workflow the subject selected; existing
Learning tool classes remain the atomic implementations.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from typing import Annotated, Any, ClassVar

from src.app.plugin_system.base import BaseTool

from .opportunity_capability import LearningOpportunityCapability
from .tools import (
    LifeChallengeInsightTool,
    LifeCompleteValidationExperimentTool,
    LifeDecideSkillCandidateTool,
    LifeDecideSubjectCandidateTool,
    LifeDistillSkillCandidateTool,
    LifeKnowledgeCandidatesTool,
    LifeListInsightsTool,
    LifeListSkillCandidatesTool,
    LifeListSubjectCandidatesTool,
    LifeListValidationExperimentsTool,
    LifeObserveStaleInsightsTool,
    LifeProposeKnowledgeCandidateTool,
    LifeReadSkillCandidateTool,
    LifeReadSubjectCandidateTool,
    LifeReconsiderInsightTool,
    LifeReflectNowTool,
    LifeReviewSubjectDocumentTool,
    LifeRunIndependentAuditTool,
    LifeRunNextReflectionTool,
    LifeViewKnowledgeTool,
)

LEARNING_SKILL_RELATIVE = Path("skills") / "learning" / "SKILL.md"
LEARNING_SKILL_MAX_BYTES = 8192
LEARN_TOOL_NAME = "nucleus_learn"

_HELP_ACTIONS = frozenset({"help", "read_skill", "skill"})
_OBSERVE_OPERATIONS = frozenset(
    {
        "list_insights",
        "view_knowledge",
        "list_subject_candidates",
        "read_subject_candidate",
        "list_skill_candidates",
        "read_skill_candidate",
        "observe_stale_insights",
        "list_validation_experiments",
    }
)
_OPERATION_TOOLS: dict[str, type[BaseTool]] = {
    "reflect_now": LifeReflectNowTool,
    "run_next_reflection": LifeRunNextReflectionTool,
    "audit_once": LifeRunIndependentAuditTool,
    "run_independent_audit": LifeRunIndependentAuditTool,
    "propose_knowledge_candidate": LifeProposeKnowledgeCandidateTool,
    "distill_skill_candidate": LifeDistillSkillCandidateTool,
    "list_insights": LifeListInsightsTool,
    "challenge_insight": LifeChallengeInsightTool,
    "reconsider_insight": LifeReconsiderInsightTool,
    "view_knowledge": LifeViewKnowledgeTool,
    "knowledge_candidates": LifeKnowledgeCandidatesTool,
    "review_subject_document": LifeReviewSubjectDocumentTool,
    "list_subject_candidates": LifeListSubjectCandidatesTool,
    "read_subject_candidate": LifeReadSubjectCandidateTool,
    "decide_subject_candidate": LifeDecideSubjectCandidateTool,
    "list_skill_candidates": LifeListSkillCandidatesTool,
    "read_skill_candidate": LifeReadSkillCandidateTool,
    "decide_skill_candidate": LifeDecideSkillCandidateTool,
    "observe_stale_insights": LifeObserveStaleInsightsTool,
    "list_validation_experiments": LifeListValidationExperimentsTool,
    "complete_validation_experiment": LifeCompleteValidationExperimentTool,
}


def normalize_learn_action(action: str) -> str:
    """Map help aliases and legacy tool names onto workflow operation ids."""

    text = str(action or "").strip().lower()
    if text in _HELP_ACTIONS:
        return "help"
    return text.removeprefix("nucleus_")


def _merge_learn_arguments(args: dict[str, Any] | None) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if not isinstance(args, dict):
        return payload
    raw = args.get("arguments")
    if isinstance(raw, dict):
        payload.update(raw)
    elif isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            payload.update(parsed)
    for key, value in args.items():
        if key in {"action", "arguments"} or value is None:
            continue
        payload[key] = value
    return payload


def learn_call_counts_as_activity(action: str, args: dict[str, Any] | None) -> bool:
    """Observation (help/list/read/status) does not reset heartbeat idle."""

    operation = normalize_learn_action(action)
    if operation == "help" or operation in _HELP_ACTIONS:
        return False
    if operation in _OBSERVE_OPERATIONS:
        return False
    nested = _merge_learn_arguments(args)
    inner = str(nested.get("action") or "").strip().lower()
    if operation == "knowledge_candidates":
        return inner in {"decide"}
    if operation == "review_subject_document":
        return inner not in {"", "status"}
    return bool(operation)


def learning_skill_workspace_path(plugin: Any) -> Path:
    """Return the legacy subject-adopted workflow path."""

    from .tools import _get_workspace

    return _get_workspace(plugin) / LEARNING_SKILL_RELATIVE


def _clip_utf8(data: bytes, max_bytes: int) -> tuple[str, bool]:
    budget = max(256, int(max_bytes))
    truncated = len(data) > budget
    clipped = data[:budget]
    while clipped:
        try:
            return clipped.decode("utf-8"), truncated
        except UnicodeDecodeError:
            clipped = clipped[:-1]
            truncated = True
    return "", truncated


def read_learning_skill(plugin: Any) -> dict[str, Any]:
    """Read an already-adopted legacy workflow without recreating it."""

    dest = learning_skill_workspace_path(plugin)
    if not dest.is_file():
        raise RuntimeError("LearningWorkflowNotAdopted")
    raw = dest.read_bytes()
    content, truncated = _clip_utf8(raw, LEARNING_SKILL_MAX_BYTES)
    return {
        "action": "help",
        "skill": "learning",
        "path": LEARNING_SKILL_RELATIVE.as_posix(),
        "source": "legacy_subject_workflow",
        "subject_adopted": True,
        "seeded": False,
        "truncated": truncated,
        "original_bytes": len(raw),
        "content": content,
    }


def _copy_runtime(source: BaseTool, dest: BaseTool) -> None:
    dest._bind_runtime_context(
        stream_id=source.get_current_stream_id(),
        message=source.trigger_message,
        tool_call_id=str(getattr(source, "_tool_call_id", "") or ""),
    )
    for attr in (
        "_runtime_task_name",
        "_life_source_occurrence_id",
        "_life_source_occurred_at",
        "_life_source_instance_id",
    ):
        if hasattr(source, attr):
            setattr(dest, attr, getattr(source, attr))


def _forward_kwargs(execute: Any, payload: dict[str, Any]) -> dict[str, Any]:
    signature = inspect.signature(execute)
    if any(
        param.kind == inspect.Parameter.VAR_KEYWORD
        for param in signature.parameters.values()
    ):
        return dict(payload)
    accepted: dict[str, Any] = {}
    for name, param in signature.parameters.items():
        if name == "self":
            continue
        if param.kind is inspect.Parameter.VAR_POSITIONAL:
            continue
        if name not in payload or payload[name] is None:
            continue
        accepted[name] = payload[name]
    return accepted


class NucleusLearnTool(BaseTool):
    """Run one explicitly selected operation from the bound workflow."""

    tool_name: str = LEARN_TOOL_NAME
    tool_description: str = (
        "learning capability 的执行门。action=help 读取当前主体已经选择并绑定的"
        "工作流；工程默认模板不会被自动安装或冒充她的选择。其它 action 是工作流中的操作名"
        "（也接受旧工具全名）。参数放在 arguments 对象中。"
        "忽略机会、不调用、安静结束都完整，不等于拒绝。"
        "MEMORY.md 只能通过受控的主体连续性复盘与权威提交链修改。"
    )
    chatter_allow: ClassVar[list[str]] = ["life_engine_internal"]

    @classmethod
    def to_schema(cls) -> dict[str, Any]:
        """Keep one small object schema; the selected workflow lists operations."""

        return {
            "type": "function",
            "function": {
                "name": f"tool-{cls.tool_name}",
                "description": cls.tool_description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "description": (
                                "help 读取已绑定工作流；其它值为其中的操作名，"
                                "或旧工具全名如 nucleus_reflect_now"
                            ),
                        },
                        "arguments": {
                            "type": "object",
                            "description": "该原子操作的参数",
                        },
                    },
                    "required": ["action"],
                },
            },
        }

    async def execute(
        self,
        action: Annotated[str, "help 或已绑定工作流中的操作名"],
        arguments: Annotated[
            dict[str, Any] | None,
            "该原子操作的参数对象",
        ] = None,
        **extra: object,
    ) -> tuple[bool, str | dict[str, Any]]:
        envelope: dict[str, Any] = {"action": action}
        if arguments is not None:
            envelope["arguments"] = arguments
        for key, value in extra.items():
            if key == "action" or value is None:
                continue
            envelope[key] = value
        try:
            capability = await LearningOpportunityCapability.bind(self)
        except Exception as exc:  # noqa: BLE001 - explicit capability refusal
            return False, f"learning capability 不可用: {type(exc).__name__}"
        operation = normalize_learn_action(action)
        if operation == "help":
            try:
                if capability.opportunity_runtime is not None:
                    return True, await capability.read_selected_workflow(
                        max_bytes=LEARNING_SKILL_MAX_BYTES,
                    )
                return True, read_learning_skill(self.plugin)
            except Exception as exc:  # noqa: BLE001
                return False, f"learning workflow 不可读: {type(exc).__name__}"

        tool_cls = _OPERATION_TOOLS.get(operation)
        if tool_cls is None:
            return False, (f"未知学习操作: {action}。先 action=help 读取已绑定工作流。")
        payload = _merge_learn_arguments(envelope)
        inner = tool_cls(plugin=self.plugin)
        _copy_runtime(self, inner)
        try:
            return await inner.execute(**_forward_kwargs(inner.execute, payload))
        except TypeError as exc:
            return False, f"学习操作参数不合法: {exc}"


LEARN_TOOLS = [NucleusLearnTool]

__all__ = [
    "LEARNING_SKILL_MAX_BYTES",
    "LEARNING_SKILL_RELATIVE",
    "LEARN_TOOLS",
    "LEARN_TOOL_NAME",
    "NucleusLearnTool",
    "learn_call_counts_as_activity",
    "learning_skill_workspace_path",
    "normalize_learn_action",
    "read_learning_skill",
]
