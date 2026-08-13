"""Single multi-stage tool for subject-owned continuity review.

Registration and runtime dependency construction intentionally live outside
this module.  If the plugin exposes no public runtime provider, execution
fails closed instead of reaching into private service fields.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Annotated, Any, ClassVar, Literal

from src.core.components.base.tool import BaseTool

from .continuity_session import (
    BoundaryAnchorEdit,
    ContinuityBoundaryPlan,
    ContinuityReviewActorContext,
    ContinuityReviewError,
    ContinuityReviewInputError,
    ContinuityReviewRuntimeUnavailable,
    ContinuityReviewSession,
    SubjectTextEdit,
)

ContinuityReviewAction = Literal[
    "open",
    "read_source",
    "open_auxiliary_source",
    "read_auxiliary_source",
    "prepare_candidate",
    "read_candidate",
    "status",
    "decide",
    "unchanged",
    "snooze",
]


@dataclass(frozen=True, slots=True)
class ContinuityReviewToolRuntime:
    """Public dependency bundle produced by the eventual plugin integration."""

    session: ContinuityReviewSession
    actor: ContinuityReviewActorContext


async def resolve_continuity_review_tool_runtime(
    tool: LifeMemoryContinuityReviewSessionTool,
) -> ContinuityReviewToolRuntime:
    """Resolve only a declared public provider; private fallback is forbidden."""

    owners = (tool.plugin, getattr(tool.plugin, "service", None))
    for owner in owners:
        provider = getattr(owner, "get_memory_continuity_review_runtime", None)
        if not callable(provider):
            continue
        value = provider(tool)
        if inspect.isawaitable(value):
            value = await value
        if not isinstance(value, ContinuityReviewToolRuntime):
            raise ContinuityReviewRuntimeUnavailable(
                "ContinuityReviewPublicRuntimeProviderReturnedInvalidBundle"
            )
        return value
    raise ContinuityReviewRuntimeUnavailable(
        "ContinuityReviewPublicRuntimeProviderUnavailable:"
        "get_memory_continuity_review_runtime"
    )


class LifeMemoryContinuityReviewSessionTool(BaseTool):
    """Run one recoverable, subject-gated continuity review state machine."""

    tool_name = "nucleus_memory_continuity_review"
    tool_description = (
        "在一个工具中分阶段复盘 MEMORY.md：打开并精确分页读取当前版本，"
        "也可打开并精确分页读取 Subject Authority 中受控 diaries 根下的辅助不可变文档（优先 "
        "Witness 投影）。把你明确选择的长记忆范围机械保存为不可变 Boundary；Boundary 正文"
        "只能来自固定 MEMORY 或辅助文档版本的精确 UTF-8 字节切片，source_refs 与来源 occurrence "
        "只由服务端构造，禁止提交正文或引用。外部长记忆可用零长度 MEMORY anchor 新增链接，也可"
        "替换旧索引。你也可以在同一次 prepare_candidate 中用有界 SubjectTextEdit "
        "显式新增、更新或删除短文本；这些文字只来自当前活跃意识的 edit，基础设施仅按精确"
        "UTF-8 字节范围机械应用。工具生成完整 MEMORY 候选后仍须分页核对并另行决定；它不会"
        "自动接受候选，也不会直接修改 SOUL/USER/MEMORY。若本次明确不改或稍后再看，请在同一"
        "固定会话使用 unchanged 或 1–720 小时的 snooze 留下可重放的主体决定证据。"
    )
    chatter_allow: ClassVar[list[str]] = ["life_engine_internal", "life_chatter"]

    async def execute(
        self,
        action: Annotated[
            ContinuityReviewAction,
            "阶段：open/read_source/open_auxiliary_source/read_auxiliary_source/"
            "prepare_candidate/read_candidate/status/decide/unchanged/snooze",
        ],
        session_id: Annotated[str, "open 返回的固定会话 ID"] = "",
        expected_subject_revision: Annotated[
            str, "open 返回的统一 SOUL+USER+MEMORY 精确 revision"
        ] = "",
        memory_version_id: Annotated[str, "open 返回的 MEMORY 版本 ID"] = "",
        memory_sha256: Annotated[str, "open 返回的 MEMORY 精确字节哈希"] = "",
        auxiliary_logical_path: Annotated[
            str,
            "辅助来源的受控 Subject logical_path，仅允许 diaries 根",
        ] = "",
        auxiliary_version_id: Annotated[
            str,
            "open_auxiliary_source 返回的不可变版本 ID",
        ] = "",
        auxiliary_content_hash: Annotated[
            str,
            "open_auxiliary_source 返回的精确内容 SHA-256",
        ] = "",
        offset: Annotated[
            int,
            "read_source/read_auxiliary_source/read_candidate 的 UTF-8 字节偏移",
        ] = 0,
        max_bytes: Annotated[int, "本页最大 UTF-8 字节数，硬上限 32768"] = 32768,
        boundaries: Annotated[
            list[dict[str, Any]] | None,
            "prepare_candidate 的主体 Boundary 计划；辅助 segment 只提供 "
            "logical_path/version_id/content_hash/精确范围，任何 segment 均不得包含 "
            "content/source_refs/source_occurrence_ids",
        ] = None,
        edits: Annotated[
            list[dict[str, Any]] | None,
            "prepare_candidate 的机械替换范围与主体书写的锚点文字",
        ] = None,
        text_edits: Annotated[
            list[dict[str, Any]] | None,
            "prepare_candidate 的主体短文本精确 UTF-8 字节编辑；默认空列表",
        ] = None,
        snooze_hours: Annotated[int, "snooze 的严格延后小时数，范围 1–720"] = 0,
        reason: Annotated[str, "主体书写的候选或决定理由"] = "",
        candidate_id: Annotated[str, "候选 ID"] = "",
        candidate_revision: Annotated[int, "候选 revision"] = 0,
        candidate_sha256: Annotated[str, "候选精确字节哈希"] = "",
        decision_kind: Annotated[
            Literal["accept_requested", "rejected", "kept_open"] | None,
            "decide 的明确主体决定",
        ] = None,
        delivery_receipt: Annotated[
            dict[str, Any] | None,
            "accept_requested 所需、由可信投递层核验的完整候选回执",
        ] = None,
    ) -> tuple[bool, dict[str, Any]]:
        try:
            runtime = await resolve_continuity_review_tool_runtime(self)
            session = runtime.session
            actor = runtime.actor
            if action == "open":
                opened = await session.open(actor, offset=offset, max_bytes=max_bytes)
                return True, opened.as_dict()
            if action == "read_source":
                page = await session.read_source(
                    actor,
                    session_id=session_id,
                    expected_subject_revision=expected_subject_revision,
                    memory_version_id=memory_version_id,
                    memory_sha256=memory_sha256,
                    offset=offset,
                    max_bytes=max_bytes,
                )
                return True, {
                    "action": "source_read",
                    "session_id": session_id,
                    "page": page.as_dict(),
                    "authority_written": False,
                }
            if action == "open_auxiliary_source":
                opened_source = await session.open_auxiliary_source(
                    actor,
                    session_id=session_id,
                    expected_subject_revision=expected_subject_revision,
                    memory_version_id=memory_version_id,
                    memory_sha256=memory_sha256,
                    logical_path=auxiliary_logical_path,
                    offset=offset,
                    max_bytes=max_bytes,
                )
                return True, opened_source.as_dict()
            if action == "read_auxiliary_source":
                source_page = await session.read_auxiliary_source(
                    actor,
                    session_id=session_id,
                    expected_subject_revision=expected_subject_revision,
                    memory_version_id=memory_version_id,
                    memory_sha256=memory_sha256,
                    logical_path=auxiliary_logical_path,
                    version_id=auxiliary_version_id,
                    content_hash=auxiliary_content_hash,
                    offset=offset,
                    max_bytes=max_bytes,
                )
                return True, source_page.as_dict()
            if action == "unchanged":
                recording = await session.unchanged(
                    actor,
                    session_id=session_id,
                    expected_subject_revision=expected_subject_revision,
                    memory_version_id=memory_version_id,
                    memory_sha256=memory_sha256,
                    reason=reason,
                )
                return recording.status == "recorded", {
                    "action": "unchanged",
                    "outcome_recording": recording.as_dict(),
                    "candidate_created": False,
                    "authority_written": False,
                }
            if action == "snooze":
                recording = await session.snooze(
                    actor,
                    session_id=session_id,
                    expected_subject_revision=expected_subject_revision,
                    memory_version_id=memory_version_id,
                    memory_sha256=memory_sha256,
                    reason=reason,
                    snooze_hours=snooze_hours,
                )
                return recording.status == "recorded", {
                    "action": "snooze",
                    "snooze_hours": snooze_hours,
                    "outcome_recording": recording.as_dict(),
                    "candidate_created": False,
                    "authority_written": False,
                }
            if action == "prepare_candidate":
                raw_boundaries = boundaries if boundaries is not None else []
                raw_edits = edits if edits is not None else []
                raw_text_edits = text_edits if text_edits is not None else []
                parsed_boundaries = tuple(
                    ContinuityBoundaryPlan.from_payload(item) for item in raw_boundaries
                )
                parsed_edits = tuple(
                    BoundaryAnchorEdit.from_payload(item) for item in raw_edits
                )
                parsed_text_edits = tuple(
                    SubjectTextEdit.from_payload(item) for item in raw_text_edits
                )
                prepared = await session.prepare_candidate(
                    actor,
                    session_id=session_id,
                    expected_subject_revision=expected_subject_revision,
                    memory_version_id=memory_version_id,
                    memory_sha256=memory_sha256,
                    boundaries=parsed_boundaries,
                    edits=parsed_edits,
                    reason=reason,
                    text_edits=parsed_text_edits,
                )
                return True, prepared.as_dict()
            if action == "read_candidate":
                candidate = await session.read_candidate(
                    actor,
                    session_id=session_id,
                    candidate_id=candidate_id,
                    candidate_revision=candidate_revision,
                    candidate_sha256=candidate_sha256,
                    expected_subject_revision=expected_subject_revision,
                    offset=offset,
                    max_bytes=max_bytes,
                )
                return True, candidate.as_dict()
            if action == "status":
                status = await session.status(
                    actor,
                    session_id=session_id,
                    candidate_id=candidate_id,
                )
                return True, status.as_dict()
            if action == "decide":
                if decision_kind is None:
                    raise ContinuityReviewInputError("decide requires decision_kind")
                persisted = await session.decide(
                    actor,
                    session_id=session_id,
                    candidate_id=candidate_id,
                    candidate_revision=candidate_revision,
                    candidate_sha256=candidate_sha256,
                    expected_subject_revision=expected_subject_revision,
                    decision_kind=decision_kind,
                    reason=reason,
                    delivery_receipt=delivery_receipt,
                )
                return True, persisted.as_dict()
            raise ContinuityReviewInputError(f"unsupported action: {action}")
        except ContinuityReviewError as exc:
            return False, {
                "error": type(exc).__name__,
                "reason": str(exc),
                "authority_written": False,
            }
        except (TypeError, ValueError) as exc:
            return False, {
                "error": type(exc).__name__,
                "reason": str(exc),
                "authority_written": False,
            }
        except Exception as exc:  # noqa: BLE001 - public tool boundary fails closed
            return False, {
                "error": type(exc).__name__,
                "reason": "ContinuityReviewUnexpectedFailure",
                "authority_written": False,
            }


CONTINUITY_REVIEW_TOOLS = [LifeMemoryContinuityReviewSessionTool]


__all__ = [
    "CONTINUITY_REVIEW_TOOLS",
    "ContinuityReviewAction",
    "ContinuityReviewToolRuntime",
    "LifeMemoryContinuityReviewSessionTool",
    "resolve_continuity_review_tool_runtime",
]
