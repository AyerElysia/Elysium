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
)

ContinuityReviewAction = Literal[
    "open",
    "read_source",
    "prepare_candidate",
    "read_candidate",
    "status",
    "decide",
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
        "把你明确选择的长记忆范围机械保存为不可变 Boundary，生成只含精确 URI 锚点的"
        "完整 MEMORY 候选，再分页核对候选并生成独立决定请求。基础设施不会替你写文字、"
        "不会自动接受候选，也不会直接修改 SOUL/USER/MEMORY。"
    )
    chatter_allow: ClassVar[list[str]] = ["life_engine_internal", "life_chatter"]

    async def execute(
        self,
        action: Annotated[
            ContinuityReviewAction,
            "阶段：open/read_source/prepare_candidate/read_candidate/status/decide",
        ],
        session_id: Annotated[str, "open 返回的固定会话 ID"] = "",
        expected_subject_revision: Annotated[
            str, "open 返回的统一 SOUL+USER+MEMORY 精确 revision"
        ] = "",
        memory_version_id: Annotated[str, "open 返回的 MEMORY 版本 ID"] = "",
        memory_sha256: Annotated[str, "open 返回的 MEMORY 精确字节哈希"] = "",
        offset: Annotated[int, "read_source/read_candidate 的 UTF-8 字节偏移"] = 0,
        max_bytes: Annotated[int, "本页最大 UTF-8 字节数，硬上限 32768"] = 32768,
        boundaries: Annotated[
            list[dict[str, Any]] | None,
            "prepare_candidate 的主体 Boundary 计划；不得包含 content/source_refs",
        ] = None,
        edits: Annotated[
            list[dict[str, Any]] | None,
            "prepare_candidate 的机械替换范围与主体书写的锚点文字",
        ] = None,
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
            if action == "prepare_candidate":
                raw_boundaries = boundaries if boundaries is not None else []
                raw_edits = edits if edits is not None else []
                parsed_boundaries = tuple(
                    ContinuityBoundaryPlan.from_payload(item) for item in raw_boundaries
                )
                parsed_edits = tuple(
                    BoundaryAnchorEdit.from_payload(item) for item in raw_edits
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
