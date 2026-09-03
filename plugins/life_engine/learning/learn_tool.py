"""Heartbeat door for the learning skill: opportunity + operation document.

Learning is not a separate heartbeat subsystem. Due work appears on the
opportunity page; this tool reads the engineering skill and performs a named
operation. Existing learning Tool classes remain the implementation.
"""

from __future__ import annotations

import inspect
import json
import shutil
from pathlib import Path
from typing import Annotated, Any, ClassVar

from src.app.plugin_system.api import log_api
from src.app.plugin_system.base import BaseTool

from .tools import (
    LifeChallengeInsightTool,
    LifeCompleteValidationExperimentTool,
    LifeDecideSkillCandidateTool,
    LifeDecideSubjectCandidateTool,
    LifeKnowledgeCandidatesTool,
    LifeListInsightsTool,
    LifeListSkillCandidatesTool,
    LifeListSubjectCandidatesTool,
    LifeListValidationExperimentsTool,
    LifeObserveStaleInsightsTool,
    LifeReadSkillCandidateTool,
    LifeReadSubjectCandidateTool,
    LifeReconsiderInsightTool,
    LifeReflectNowTool,
    LifeReviewSubjectDocumentTool,
    LifeViewKnowledgeTool,
)

logger = log_api.get_logger("life_engine.learn_tool")

LEARNING_SKILL_RELATIVE = Path("skills") / "learning" / "SKILL.md"
PACKAGED_LEARNING_SKILL = Path(__file__).resolve().parent / "SKILL.md"
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
    """Map help aliases and legacy tool names onto skill operation ids."""

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
    from .tools import _get_workspace

    return _get_workspace(plugin) / LEARNING_SKILL_RELATIVE


def seed_learning_skill_if_absent(plugin: Any) -> Path:
    """Copy the packaged template only when the workspace file is missing."""

    dest = learning_skill_workspace_path(plugin)
    if dest.is_file():
        return dest
    if not PACKAGED_LEARNING_SKILL.is_file():
        raise RuntimeError("LearningSkillTemplateMissing")
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(PACKAGED_LEARNING_SKILL, dest)
    logger.debug("seeded learning skill template into workspace")
    return dest


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
    """Read workspace skill if present; otherwise seed from the packaged template."""

    dest = learning_skill_workspace_path(plugin)
    source = "workspace"
    seeded = False
    if dest.is_file():
        path = dest
    else:
        path = seed_learning_skill_if_absent(plugin)
        source = "seeded_template"
        seeded = True
    raw = path.read_bytes()
    content, truncated = _clip_utf8(raw, LEARNING_SKILL_MAX_BYTES)
    return {
        "action": "help",
        "skill": "learning",
        "path": LEARNING_SKILL_RELATIVE.as_posix(),
        "source": source,
        "seeded": seeded,
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
    """Follow the learning skill after an opportunity appears."""

    tool_name: str = LEARN_TOOL_NAME
    tool_description: str = (
        "跟随 learning skill 的执行门。学习到期事实出现在机会页邀请栏，"
        "操作说明在 skills/learning/SKILL.md，不在本工具的参数表里。"
        "action=help 读取该 skill。其它 action 是 skill 中的操作名"
        "（也接受旧工具全名）。参数放在 arguments 对象中。"
        "忽略机会、不调用、安静结束都完整，不等于拒绝。"
        "MEMORY.md 也可以用普通文件工具改；结构化整理仍可用 nucleus_memory_continuity_review。"
    )
    chatter_allow: ClassVar[list[str]] = ["life_engine_internal"]

    @classmethod
    def to_schema(cls) -> dict[str, Any]:
        """Keep one small object schema; the skill document lists operations."""

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
                                "help 读取 learning skill；其它值为 skill 中的操作名，"
                                "或旧工具全名如 nucleus_reflect_now"
                            ),
                        },
                        "arguments": {
                            "type": "object",
                            "description": "该操作的参数，字段以 learning skill 为准",
                        },
                    },
                    "required": ["action"],
                },
            },
        }

    async def execute(
        self,
        action: Annotated[str, "help 或 learning skill 中的操作名"],
        arguments: Annotated[
            dict[str, Any] | None,
            "该操作的参数对象，字段以 learning skill 为准",
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
        operation = normalize_learn_action(action)
        if operation == "help":
            try:
                return True, read_learning_skill(self.plugin)
            except Exception as exc:  # noqa: BLE001
                return False, f"learning skill 不可读: {type(exc).__name__}"

        tool_cls = _OPERATION_TOOLS.get(operation)
        if tool_cls is None:
            return False, (
                f"未知学习操作: {action}。先 action=help 读 learning skill。"
            )
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
    "seed_learning_skill_if_absent",
]
