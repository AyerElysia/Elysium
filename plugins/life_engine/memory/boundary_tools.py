"""Life Engine tools for subject-owned long-memory boundaries and indexes."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from typing import Annotated, Any, ClassVar
from urllib.parse import quote

from src.app.plugin_system.api import log_api
from src.app.plugin_system.base import BaseTool

from ..tools.bounded_projection import project_bounded_items
from .boundary import (
    MemoryBoundaryManifest,
    MemoryBoundaryRepository,
    MemoryBoundarySegment,
    memory_boundary_uri,
)
from .boundary_resolver import MemoryBoundaryResolver
from .continuity_index import (
    build_continuity_memory_index_health,
    diagnose_continuity_memory_index,
)
from .continuity_stewardship import ContinuityMemoryStewardship

logger = log_api.get_logger("life_engine.memory_boundary_tools")


@dataclass(frozen=True, slots=True)
class _BoundaryToolRuntime:
    service: Any
    memory_service: Any
    scheduler: Any
    repository: MemoryBoundaryRepository
    actor_consciousness_instance_id: str
    stream_scope: str


def _canonical_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def _stable_occurrence(tool: BaseTool, prefix: str, material: Any) -> str:
    tool_call_id = str(getattr(tool, "_tool_call_id", "") or "").strip()
    message_id = str(
        getattr(getattr(tool, "trigger_message", None), "message_id", "") or ""
    ).strip()
    trigger_extra = getattr(getattr(tool, "trigger_message", None), "extra", {}) or {}
    turn_scope = trigger_extra.get("life_turn_scope")
    turn_key = (
        str(turn_scope.get("turn_key") or "").strip()
        if isinstance(turn_scope, dict)
        else ""
    )
    if not tool_call_id:
        raise RuntimeError("MemoryBoundaryToolCallIdentityRequired")
    digest = hashlib.sha256(
        (
            tool_call_id
            + "\0"
            + tool.get_current_stream_id()
            + "\0"
            + message_id
            + "\0"
            + turn_key
            + "\0"
            + _canonical_hash(material)
        ).encode("utf-8")
    ).hexdigest()
    return f"{prefix}:{digest}"


def _stable_recall_chain(tool: BaseTool, prefix: str, material: Any) -> str:
    """Bind all continuation pages in one subject turn to one recall episode."""

    message = getattr(tool, "trigger_message", None)
    message_id = str(getattr(message, "message_id", "") or "").strip()
    trigger_extra = getattr(message, "extra", {}) or {}
    turn_scope = trigger_extra.get("life_turn_scope")
    turn_key = (
        str(turn_scope.get("turn_key") or "").strip()
        if isinstance(turn_scope, dict)
        else ""
    )
    if not message_id and not turn_key:
        raise RuntimeError("MemoryBoundaryRecallTurnIdentityRequired")
    digest = hashlib.sha256(
        (
            tool.get_current_stream_id()
            + "\0"
            + message_id
            + "\0"
            + turn_key
            + "\0"
            + _canonical_hash(material)
        ).encode("utf-8")
    ).hexdigest()
    return f"{prefix}:{digest}"


def _stable_recall_time(tool: BaseTool) -> str:
    """Use the source turn time so paginated recall remains replay-identical."""

    message = getattr(tool, "trigger_message", None)
    value = getattr(message, "time", None)
    if isinstance(value, datetime):
        timestamp = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        return timestamp.astimezone(UTC).isoformat()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.fromtimestamp(float(value), tz=UTC).isoformat()
    text = str(value or "").strip()
    if text:
        try:
            return (
                datetime.fromisoformat(text)
                .astimezone(UTC)
                .isoformat()
            )
        except ValueError:
            pass
    # The tool-call identity still prevents replay ambiguity.  This fallback is
    # only for legacy synthetic turns that did not carry a source timestamp.
    return datetime.now(UTC).isoformat()


async def _resolve_runtime(tool: BaseTool) -> _BoundaryToolRuntime:
    from ..service.registry import get_life_engine_service

    service = get_life_engine_service()
    if service is None:
        raise RuntimeError("LifeEngineServiceUnavailable")
    memory_service = getattr(service, "_memory_service", None)
    if memory_service is None:
        raise RuntimeError("LifeMemoryServiceUnavailable")
    scheduler = getattr(service, "_learning_scheduler", None)
    if scheduler is None:
        raise RuntimeError("LearningSchedulerUnavailable")
    stream_scope = str(tool.get_current_stream_id() or "")
    if not stream_scope:
        raise PermissionError("MemoryBoundaryStreamOwnerRequired")
    instance = service.consciousness_registry.get_for_stream(stream_scope)
    if instance is None or not instance.is_active:
        raise PermissionError("MemoryBoundaryActorIsNotActive")
    actor = str(instance.instance_id or "").strip()
    if not actor:
        raise PermissionError("MemoryBoundaryActorIdentityRequired")
    living = memory_service._require_memory_storage().living
    return _BoundaryToolRuntime(
        service=service,
        memory_service=memory_service,
        scheduler=scheduler,
        repository=MemoryBoundaryRepository(living),
        actor_consciousness_instance_id=actor,
        stream_scope=stream_scope,
    )


async def _current_memory_snapshot(
    scheduler: Any,
) -> tuple[bytes, str, str]:
    """Read exact MEMORY bytes plus their immutable source identity."""

    read_with_identity = scheduler.read_subject_document_with_identity
    content, version_id, revision = await read_with_identity("MEMORY.md")
    return bytes(content), str(version_id), str(revision)


def _segment_from_payload(
    payload: dict[str, Any],
    *,
    default_scope: str,
    default_visibility: str,
    default_source_occurrence_id: str,
) -> MemoryBoundarySegment:
    source_refs = payload.get("source_refs")
    if not isinstance(source_refs, list):
        raise TypeError("each segment requires a source_refs list")
    source_occurrences = payload.get("source_occurrence_ids")
    if source_occurrences is None:
        source_occurrences = [default_source_occurrence_id]
    if not isinstance(source_occurrences, list):
        raise TypeError("segment source_occurrence_ids must be a list")
    return MemoryBoundarySegment.create(
        segment_id=str(payload.get("segment_id") or ""),
        title=str(payload.get("title") or ""),
        content=str(payload.get("content") or ""),
        source_refs=tuple(str(item) for item in source_refs),
        source_occurrence_ids=tuple(str(item) for item in source_occurrences),
        scope=str(payload.get("scope") or default_scope),
        visibility=str(payload.get("visibility") or default_visibility),
    )


def _segment_from_subject_range(
    payload: dict[str, Any],
    *,
    memory_bytes: bytes,
    memory_version_id: str,
    memory_sha256: str,
    source_occurrence_id: str,
    default_scope: str,
    default_visibility: str,
    previous_end: int,
) -> tuple[MemoryBoundarySegment, int, dict[str, Any]]:
    """Build one exact segment without accepting model-supplied source text."""

    allowed = {
        "segment_id",
        "title",
        "byte_start",
        "byte_end",
        "scope",
        "visibility",
    }
    unexpected = sorted(set(payload) - allowed)
    if unexpected:
        raise ValueError(
            "MemoryBoundaryReviewedRangeFieldsInvalid:" + ",".join(unexpected)
        )
    byte_start = payload.get("byte_start")
    byte_end = payload.get("byte_end")
    if (
        isinstance(byte_start, bool)
        or not isinstance(byte_start, int)
        or isinstance(byte_end, bool)
        or not isinstance(byte_end, int)
        or byte_start < 0
        or byte_end <= byte_start
        or byte_end > len(memory_bytes)
    ):
        raise ValueError("MemoryBoundaryReviewedRangeInvalid")
    if byte_start < previous_end:
        raise ValueError("MemoryBoundaryReviewedRangesOverlapOrUnordered")
    try:
        content = memory_bytes[byte_start:byte_end].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("MemoryBoundaryReviewedRangeNotUtf8Boundary") from exc
    range_sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
    source_ref = (
        "subject://life_engine_workspace/MEMORY.md@"
        + quote(memory_version_id, safe="._:-")
        + f"#sha256={memory_sha256}"
        + f"&bytes={byte_start}-{byte_end}"
        + f"&range_sha256={range_sha256}"
    )
    segment = MemoryBoundarySegment.create(
        segment_id=str(payload.get("segment_id") or ""),
        title=str(payload.get("title") or ""),
        content=content,
        source_refs=(source_ref,),
        source_occurrence_ids=(source_occurrence_id,),
        scope=str(payload.get("scope") or default_scope),
        visibility=str(payload.get("visibility") or default_visibility),
    )
    return (
        segment,
        byte_end,
        {
            "segment_id": segment.segment_id,
            "byte_start": byte_start,
            "byte_end": byte_end,
            "byte_length": segment.byte_length,
            "content_sha256": segment.content_sha256,
            "source_ref_sha256": hashlib.sha256(source_ref.encode("utf-8")).hexdigest(),
        },
    )


class LifeCreateMemoryBoundaryTool(BaseTool):
    """Create or revise one complete immutable long-memory boundary."""

    tool_name = "nucleus_create_memory_boundary"
    tool_description = (
        "把一段很长、但边界明确的完整记忆保存为不可变 Boundary。"
        "它不会修改 MEMORY.md，也不会判断重要性；返回的精确 URI 可由你写入完整"
        " MEMORY.md 候选。更新前先用读取工具 history 查看 head_revision，并提供 CAS。"
    )
    chatter_allow: ClassVar[list[str]] = ["life_engine_internal", "life_chatter"]

    async def execute(
        self,
        boundary_id: Annotated[str, "稳定的技术边界 ID；ASCII 字母数字开头"],
        title: Annotated[str, "由你书写的边界标题"],
        scope: Annotated[str, "这段记忆覆盖什么、不覆盖什么"],
        current_meaning: Annotated[str, "你目前如何理解它；不是客观事实判定"],
        non_generalization: Annotated[str, "这段记忆不应被自动泛化成什么"],
        segments: Annotated[
            list[dict[str, Any]],
            "有序完整分段：segment_id/title/content/source_refs，可选 source_occurrence_ids/scope/visibility",
        ],
        expected_head_revision: Annotated[
            int, "新建为 0；更新使用 history 返回的 head revision"
        ] = 0,
        source_occurrence_id: Annotated[
            str,
            "来源 occurrence；留空时使用当前消息 occurrence",
        ] = "",
        visibility: Annotated[str, "开放文本可见范围"] = "private",
    ) -> tuple[bool, dict[str, Any]]:
        try:
            runtime = await _resolve_runtime(self)
            subject_revision = (
                str(await runtime.scheduler.current_subject_revision()).strip().lower()
            )
            occurrence = _stable_occurrence(
                self,
                "memory_boundary_operation",
                {
                    "boundary_id": boundary_id,
                    "title": title,
                    "scope": scope,
                    "current_meaning": current_meaning,
                    "non_generalization": non_generalization,
                    "segments": segments,
                    "expected_head_revision": expected_head_revision,
                    "visibility": visibility,
                },
            )
            source_occurrence = str(source_occurrence_id or "").strip() or str(
                getattr(getattr(self, "trigger_message", None), "message_id", "")
                or occurrence
            )
            if not isinstance(segments, list) or not segments:
                return False, {"error": "MemoryBoundarySegmentsRequired"}
            parsed_segments = tuple(
                _segment_from_payload(
                    item,
                    default_scope=scope,
                    default_visibility=visibility,
                    default_source_occurrence_id=source_occurrence,
                )
                for item in segments
                if isinstance(item, dict)
            )
            if len(parsed_segments) != len(segments):
                return False, {"error": "MemoryBoundarySegmentObjectRequired"}
            manifest = MemoryBoundaryManifest(
                boundary_id=str(boundary_id),
                manifest_revision=int(expected_head_revision) + 1,
                operation_occurrence_id=occurrence,
                title=str(title),
                scope=str(scope),
                current_meaning=str(current_meaning),
                non_generalization=str(non_generalization),
                actor_id=runtime.actor_consciousness_instance_id,
                consciousness_instance_id=(runtime.actor_consciousness_instance_id),
                stream_scope=runtime.stream_scope,
                decision_occurrence_id=f"{occurrence}:subject_action",
                source_occurrence_id=source_occurrence,
                subject_revision=subject_revision,
                segments=parsed_segments,
                visibility=str(visibility),
            )
            stored = await runtime.repository.append(
                manifest,
                expected_head_revision=int(expected_head_revision),
            )
            return True, {
                "action": "memory_boundary_recorded",
                "boundary_id": stored.manifest.boundary_id,
                "manifest_revision": stored.manifest.manifest_revision,
                "head_revision": stored.head_revision,
                "artifact_id": stored.artifact.artifact_id,
                "root_sha256": stored.manifest.root_sha256,
                "exact_uri": stored.exact_uri,
                "canonical_bytes": len(stored.manifest.canonical_bytes),
                "segment_count": len(stored.manifest.segments),
                "operation_occurrence_id": (stored.manifest.operation_occurrence_id),
                "subject_revision": stored.manifest.subject_revision,
                "authority": "immutable_memory_artifact_not_MEMORY_md",
                "next_step": (
                    "若你愿意让它常驻连续性，请把 exact_uri 写入完整 MEMORY.md "
                    "候选；这一步不会自动替你提出或接受候选。"
                ),
            }
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"长期记忆边界记录失败: error_type={type(exc).__name__}")
            return False, {"error": type(exc).__name__, "detail": str(exc)}


class LifeCreateMemoryBoundaryFromSubjectRangeTool(BaseTool):
    """Seal exact reviewed MEMORY bytes into an immutable boundary."""

    tool_name = "nucleus_create_memory_boundary_from_subject_range"
    tool_description = (
        "从你刚用 nucleus_review_subject_document 读过的权威 MEMORY.md 精确字节范围"
        "创建不可变 Boundary。正文由服务端按 version/hash/UTF-8 范围直接截取，禁止"
        "重新转写；标题、边界与当前理解仍由你表达。它只保存 Boundary，不修改 "
        "MEMORY.md，不提出或接受候选。"
    )
    chatter_allow: ClassVar[list[str]] = ["life_engine_internal", "life_chatter"]

    async def execute(
        self,
        boundary_id: Annotated[str, "稳定的技术边界 ID；ASCII 字母数字开头"],
        title: Annotated[str, "由你书写的边界标题"],
        scope: Annotated[str, "这段记忆覆盖什么、不覆盖什么"],
        current_meaning: Annotated[str, "你目前如何理解它；不是客观事实判定"],
        non_generalization: Annotated[str, "这段记忆不应被自动泛化成什么"],
        segments: Annotated[
            list[dict[str, Any]],
            "有序精确范围：segment_id/title/byte_start/byte_end，可选 scope/visibility；不得传 content/source_refs",
        ],
        expected_subject_revision: Annotated[
            str, "status 返回的统一 SOUL+USER+MEMORY revision"
        ],
        reviewed_memory_version_id: Annotated[
            str, "status 返回的 MEMORY.md 精确 version_id"
        ],
        reviewed_content_sha256: Annotated[
            str, "status 返回的 MEMORY.md 完整内容 SHA-256"
        ],
        expected_head_revision: Annotated[
            int, "新建为 0；更新使用 history 返回的 head revision"
        ] = 0,
        visibility: Annotated[str, "开放文本可见范围"] = "private",
    ) -> tuple[bool, dict[str, Any]]:
        try:
            runtime = await _resolve_runtime(self)
            validated_revision = (
                await runtime.scheduler.validate_subject_review_context(
                    actor_consciousness_instance_id=(
                        runtime.actor_consciousness_instance_id
                    ),
                    expected_subject_revision=str(expected_subject_revision),
                )
            )
            snapshot = await runtime.scheduler.read_subject_document_snapshot(
                "MEMORY.md"
            )
            if snapshot.unified_subject_revision != validated_revision:
                raise RuntimeError("LearningSubjectRevisionConflict")
            reviewed_version = str(reviewed_memory_version_id or "").strip()
            if reviewed_version != snapshot.version_id:
                raise RuntimeError("MemoryBoundarySourceVersionConflict")
            reviewed_hash = str(reviewed_content_sha256 or "").strip().lower()
            if reviewed_hash != snapshot.content_sha256:
                raise RuntimeError("MemoryBoundarySourceContentHashConflict")
            try:
                snapshot.content_bytes.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ValueError("MemoryBoundarySourceDocumentNotUtf8") from exc
            if not isinstance(segments, list) or not segments:
                return False, {"error": "MemoryBoundaryReviewedRangesRequired"}

            parsed_segments: list[MemoryBoundarySegment] = []
            range_receipts: list[dict[str, Any]] = []
            previous_end = 0
            for payload in segments:
                if not isinstance(payload, dict):
                    return False, {"error": "MemoryBoundaryReviewedRangeObjectRequired"}
                segment, previous_end, receipt = _segment_from_subject_range(
                    payload,
                    memory_bytes=snapshot.content_bytes,
                    memory_version_id=snapshot.version_id,
                    memory_sha256=snapshot.content_sha256,
                    source_occurrence_id=snapshot.source_occurrence_id,
                    default_scope=str(scope),
                    default_visibility=str(visibility),
                    previous_end=previous_end,
                )
                parsed_segments.append(segment)
                range_receipts.append(receipt)

            occurrence = _stable_occurrence(
                self,
                "memory_boundary_subject_range_operation",
                {
                    "boundary_id": boundary_id,
                    "title": title,
                    "scope": scope,
                    "current_meaning": current_meaning,
                    "non_generalization": non_generalization,
                    "ranges": range_receipts,
                    "expected_head_revision": expected_head_revision,
                    "source_memory_version_id": snapshot.version_id,
                    "source_memory_sha256": snapshot.content_sha256,
                    "subject_revision": validated_revision,
                    "visibility": visibility,
                },
            )
            manifest = MemoryBoundaryManifest(
                boundary_id=str(boundary_id),
                manifest_revision=int(expected_head_revision) + 1,
                operation_occurrence_id=occurrence,
                title=str(title),
                scope=str(scope),
                current_meaning=str(current_meaning),
                non_generalization=str(non_generalization),
                actor_id=runtime.actor_consciousness_instance_id,
                consciousness_instance_id=(runtime.actor_consciousness_instance_id),
                stream_scope=runtime.stream_scope,
                decision_occurrence_id=f"{occurrence}:subject_action",
                source_occurrence_id=snapshot.source_occurrence_id,
                subject_revision=validated_revision,
                segments=tuple(parsed_segments),
                visibility=str(visibility),
            )
            stored = await runtime.repository.append(
                manifest,
                expected_head_revision=int(expected_head_revision),
            )
            return True, {
                "action": "memory_boundary_recorded_from_subject_range",
                "boundary_id": stored.manifest.boundary_id,
                "manifest_revision": stored.manifest.manifest_revision,
                "head_revision": stored.head_revision,
                "artifact_id": stored.artifact.artifact_id,
                "root_sha256": stored.manifest.root_sha256,
                "exact_uri": stored.exact_uri,
                "canonical_bytes": len(stored.manifest.canonical_bytes),
                "segment_count": len(stored.manifest.segments),
                "source_memory_version_id": snapshot.version_id,
                "source_memory_sha256": snapshot.content_sha256,
                "source_occurrence_id": snapshot.source_occurrence_id,
                "source_provenance_status": snapshot.provenance_status,
                "ranges": range_receipts,
                "operation_occurrence_id": (stored.manifest.operation_occurrence_id),
                "subject_revision": stored.manifest.subject_revision,
                "authority": "immutable_memory_artifact_not_MEMORY_md",
                "next_step": (
                    "若你愿意让它常驻连续性，请把 exact_uri 写入完整 MEMORY.md "
                    "候选；本工具不会自动提出或接受候选。"
                ),
            }
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                f"权威 MEMORY 精确范围封存失败: error_type={type(exc).__name__}"
            )
            return False, {"error": type(exc).__name__, "detail": str(exc)}


class LifeReadMemoryBoundaryTool(BaseTool):
    """Read exact boundary context, provenance, segments, or history."""

    tool_name = "nucleus_read_memory_boundary"
    tool_description = (
        "沿 MEMORY.md 中的精确 memory://boundary URI 读取完整长记忆。"
        "mode=overview 查看边界与分段目录，mode=context 分页读完整边界语义，"
        "mode=provenance 分页读完整来源标签（明确标记为外部未验证），"
        "mode=segment 按 segment_id 分页读正文，"
        "mode=history 查看某个 boundary_id 的不可变版本链。永远不会自动漂移到最新版。"
    )
    chatter_allow: ClassVar[list[str]] = ["life_engine_internal", "life_chatter"]

    async def execute(
        self,
        mode: Annotated[
            str,
            "overview / context / provenance / segment / history",
        ] = "overview",
        exact_uri: Annotated[str, "MEMORY.md 中完整的精确 URI"] = "",
        boundary_id: Annotated[str, "history 模式使用的 boundary ID"] = "",
        segment_id: Annotated[str, "segment 模式使用的分段 ID"] = "",
        continuation: Annotated[str, "上一页返回的稳定 continuation"] = "",
        max_bytes: Annotated[int | None, "可选，只能下调当前任务硬顶"] = None,
        reason: Annotated[
            str, "你这次为什么沿着索引回忆；开放文本"
        ] = "沿长期记忆索引回忆",
    ) -> tuple[bool, dict[str, Any]]:
        try:
            runtime = await _resolve_runtime(self)
            normalized = str(mode or "overview").strip().lower()
            task_name = str(getattr(self, "_runtime_task_name", "") or "")
            if normalized == "history":
                descriptors = await runtime.repository.history_descriptors(
                    str(boundary_id)
                )
                items = [asdict(item) for item in descriptors]
                payload = project_bounded_items(
                    projection_name="memory-boundary-history-v1",
                    task_name=task_name,
                    requested_max_bytes=max_bytes,
                    binding={
                        "mode": "history",
                        "boundary_id": boundary_id,
                        "stream_scope": runtime.stream_scope,
                    },
                    frontier=items,
                    base_payload={
                        "action": "read_memory_boundary_history",
                        "boundary_id": str(boundary_id),
                    },
                    items_key="revisions",
                    items=items,
                    item_refs=[
                        f"memory-boundary-revision:{item.artifact_id}"
                        for item in descriptors
                    ],
                    continuation=continuation,
                )
                return True, payload

            resolver = MemoryBoundaryResolver(
                runtime.repository,
                recall=runtime.memory_service,
            )
            recall_chain_id = _stable_recall_chain(
                self,
                "memory_boundary_recall",
                {
                    "mode": normalized,
                    "exact_uri": str(exact_uri),
                    "segment_id": str(segment_id),
                    "reason_sha256": hashlib.sha256(
                        str(reason).encode("utf-8")
                    ).hexdigest(),
                },
            )
            delivery_occurrence_id = _stable_occurrence(
                self,
                "memory_boundary_delivery",
                {
                    "mode": normalized,
                    "exact_uri": str(exact_uri),
                    "segment_id": str(segment_id),
                    "continuation": str(continuation),
                    "max_bytes": max_bytes,
                    "reason_sha256": hashlib.sha256(
                        str(reason).encode("utf-8")
                    ).hexdigest(),
                },
            )
            recorded_at = _stable_recall_time(self)
            if normalized == "overview":
                return True, await resolver.overview(
                    str(exact_uri),
                    task_name=task_name,
                    consciousness_instance_id=(runtime.actor_consciousness_instance_id),
                    stream_scope=runtime.stream_scope,
                    continuation=str(continuation),
                    max_bytes=max_bytes,
                    retrieval_reason=str(reason),
                    recall_chain_id=recall_chain_id,
                    delivery_occurrence_id=delivery_occurrence_id,
                    recorded_at=recorded_at,
                )
            if normalized == "context":
                return True, await resolver.read_context(
                    str(exact_uri),
                    task_name=task_name,
                    consciousness_instance_id=(runtime.actor_consciousness_instance_id),
                    stream_scope=runtime.stream_scope,
                    continuation=str(continuation),
                    max_bytes=max_bytes,
                    retrieval_reason=str(reason),
                    recall_chain_id=recall_chain_id,
                    delivery_occurrence_id=delivery_occurrence_id,
                    recorded_at=recorded_at,
                )
            if normalized == "provenance":
                return True, await resolver.read_provenance(
                    str(exact_uri),
                    task_name=task_name,
                    consciousness_instance_id=(runtime.actor_consciousness_instance_id),
                    stream_scope=runtime.stream_scope,
                    continuation=str(continuation),
                    max_bytes=max_bytes,
                    retrieval_reason=str(reason),
                    recall_chain_id=recall_chain_id,
                    delivery_occurrence_id=delivery_occurrence_id,
                    recorded_at=recorded_at,
                )
            if normalized == "segment":
                return True, await resolver.read_segment(
                    str(exact_uri),
                    str(segment_id),
                    task_name=task_name,
                    consciousness_instance_id=(runtime.actor_consciousness_instance_id),
                    stream_scope=runtime.stream_scope,
                    continuation=str(continuation),
                    max_bytes=max_bytes,
                    retrieval_reason=str(reason),
                    recall_chain_id=recall_chain_id,
                    delivery_occurrence_id=delivery_occurrence_id,
                    recorded_at=recorded_at,
                )
            return False, {
                "error": (
                    "mode must be overview, context, provenance, segment, or history"
                )
            }
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"长期记忆边界读取失败: error_type={type(exc).__name__}")
            return False, {"error": type(exc).__name__, "detail": str(exc)}


class LifeInspectMemoryContinuityTool(BaseTool):
    """Inspect the current exact MEMORY index without assigning importance."""

    tool_name = "nucleus_inspect_memory_continuity"
    tool_description = (
        "检查当前权威 MEMORY.md 的显式长记忆索引、精确 artifact 可达性和工程字节压力。"
        "16KiB/24KiB 只是复盘提醒，不会推荐删除，也不会改文件。"
    )
    chatter_allow: ClassVar[list[str]] = ["life_engine_internal", "life_chatter"]

    async def execute(
        self,
        continuation: Annotated[str, "上一页返回的稳定 continuation"] = "",
        max_bytes: Annotated[int | None, "可选，只能下调当前任务硬顶"] = None,
    ) -> tuple[bool, dict[str, Any]]:
        try:
            runtime = await _resolve_runtime(self)
            content, version_id, subject_revision = await _current_memory_snapshot(
                runtime.scheduler
            )
            diagnostics = diagnose_continuity_memory_index(
                content,
                subject_document_version_id=version_id,
                unified_subject_revision=subject_revision,
            )
            index = diagnostics.index
            items: list[dict[str, Any]] = []
            broken = 0
            for entry in index.entries:
                uri = memory_boundary_uri(
                    entry.boundary_id,
                    entry.artifact_id,
                    entry.root_sha256,
                )
                status = "exact"
                error_type = ""
                try:
                    await runtime.repository.read_exact(uri)
                except Exception as exc:  # noqa: BLE001
                    broken += 1
                    status = "unresolved"
                    error_type = type(exc).__name__
                items.append(
                    {
                        "entry_id": entry.entry_id,
                        "anchor_text": entry.anchor_text,
                        "exact_uri": uri,
                        "entry_sha256": entry.entry_sha256,
                        "byte_start": entry.byte_start,
                        "byte_end": entry.byte_end,
                        "resolution_status": status,
                        "error_type": error_type,
                    }
                )
            health = replace(
                build_continuity_memory_index_health(index),
                broken_reference_count=broken,
            )
            payload = project_bounded_items(
                projection_name="memory-continuity-index-inspection-v1",
                task_name=str(getattr(self, "_runtime_task_name", "") or ""),
                requested_max_bytes=max_bytes,
                binding={
                    "memory_version_id": version_id,
                    "subject_revision": subject_revision,
                    "stream_scope": runtime.stream_scope,
                },
                frontier={
                    "memory_sha256": index.source_document_sha256,
                    "entries": [item.entry_sha256 for item in index.entries],
                },
                base_payload={
                    "action": "inspect_memory_continuity",
                    "memory_version_id": version_id,
                    "subject_revision": subject_revision,
                    "memory_sha256": index.source_document_sha256,
                    "health": health.as_dict(),
                    "index_issue_count": len(diagnostics.issues),
                    "index_issues_sha256": diagnostics.issues_sha256,
                    "repair_requires_complete_candidate": bool(diagnostics.issues),
                    "automatic_deletion": False,
                },
                items_key="entries",
                items=items,
                item_refs=[
                    f"memory-continuity-entry:{item.entry_id}" for item in index.entries
                ],
                continuation=continuation,
            )
            return broken == 0 and not diagnostics.issues, payload
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"长期记忆索引检查失败: error_type={type(exc).__name__}")
            return False, {"error": type(exc).__name__, "detail": str(exc)}


class LifeProposeMemoryContinuityRevisionTool(BaseTool):
    """Validate and append a complete MEMORY candidate, never accept it."""

    tool_name = "nucleus_propose_memory_continuity_revision"
    tool_description = (
        "基于你刚复盘的精确 MEMORY.md，提交一个完整新版本候选。所有显式 Boundary URI "
        "必须能按 artifact+root 精确打开。候选保持 open；之后必须重新读取，并另行调用"
        " nucleus_decide_subject_candidate 才能接受、拒绝或保持开放。"
    )
    chatter_allow: ClassVar[list[str]] = ["life_engine_internal", "life_chatter"]

    async def execute(
        self,
        proposed_content: Annotated[str, "完整的 MEMORY.md 新候选，不能只传 diff"],
        reviewed_content_sha256: Annotated[str, "本次已阅读的当前 MEMORY.md 精确哈希"],
        expected_subject_revision: Annotated[
            str, "本次已阅读的统一 SOUL+USER+MEMORY revision"
        ],
        reason: Annotated[str, "你为什么提出这一完整版本；开放文本"],
    ) -> tuple[bool, dict[str, Any]]:
        try:
            runtime = await _resolve_runtime(self)
            current_revision = await runtime.scheduler.validate_subject_review_context(
                actor_consciousness_instance_id=(
                    runtime.actor_consciousness_instance_id
                ),
                expected_subject_revision=str(expected_subject_revision),
            )
            current, version_id, snapshot_revision = await _current_memory_snapshot(
                runtime.scheduler
            )
            if snapshot_revision != current_revision:
                raise RuntimeError("LearningSubjectRevisionConflict")
            ledger = getattr(runtime.scheduler, "decision_ledger", None)
            if ledger is None:
                raise RuntimeError("SubjectAuthorityMigrationRequired")
            proposal_occurrence = _stable_occurrence(
                self,
                "memory_continuity_proposal",
                {
                    "reviewed_content_sha256": reviewed_content_sha256,
                    "expected_subject_revision": expected_subject_revision,
                    "proposed_content_sha256": hashlib.sha256(
                        str(proposed_content).encode("utf-8")
                    ).hexdigest(),
                    "reason_sha256": hashlib.sha256(
                        str(reason).encode("utf-8")
                    ).hexdigest(),
                },
            )
            source_occurrence = str(
                getattr(getattr(self, "trigger_message", None), "message_id", "")
                or proposal_occurrence
            )
            proposal = await ContinuityMemoryStewardship(
                runtime.repository,
                ledger,
            ).propose(
                current_memory_bytes=current,
                current_memory_version_id=version_id,
                reviewed_current_memory_sha256=str(reviewed_content_sha256),
                proposed_memory_bytes=str(proposed_content).encode("utf-8"),
                unified_subject_revision=current_revision,
                actor_consciousness_instance_id=(
                    runtime.actor_consciousness_instance_id
                ),
                source_occurrence_id=source_occurrence,
                proposal_occurrence_id=proposal_occurrence,
                reason=str(reason),
                stream_scope=runtime.stream_scope,
            )
            review_health_warning = ""
            try:
                await runtime.scheduler.record_subject_review_outcome(
                    target_path="MEMORY.md",
                    outcome="candidate_proposed",
                    actor_consciousness_instance_id=(
                        runtime.actor_consciousness_instance_id
                    ),
                    subject_revision=current_revision,
                    occurrence_id=proposal.candidate.candidate_occurrence_id,
                    reason=str(reason),
                    candidate_id=proposal.candidate.candidate_id,
                    candidate_sha256=proposal.candidate.candidate_sha256,
                )
            except Exception as exc:  # noqa: BLE001 - candidate already persists
                review_health_warning = type(exc).__name__
            return True, {
                "action": "memory_continuity_candidate_proposed",
                "candidate_id": proposal.candidate.candidate_id,
                "candidate_revision": proposal.candidate.candidate_revision,
                "candidate_sha256": proposal.candidate.candidate_sha256,
                "candidate_occurrence_id": (proposal.candidate.candidate_occurrence_id),
                "subject_revision": current_revision,
                "status": proposal.receipt.status,
                "verified_boundary_count": proposal.verified_boundary_count,
                "verified_boundary_refs_sha256": (
                    proposal.verified_boundary_refs_sha256
                ),
                "current_index_issue_count": proposal.current_index_issue_count,
                "current_index_issues_sha256": (proposal.current_index_issues_sha256),
                "activated_entry_ids": list(proposal.lifecycle.activated),
                "deactivated_entry_ids": list(proposal.lifecycle.deactivated),
                "rewritten_entry_ids": list(proposal.lifecycle.rewritten),
                "retargeted_entry_ids": [
                    item.entry_id for item in proposal.lifecycle.retargeted
                ],
                "authority": "candidate_only",
                **(
                    {"review_health_warning": review_health_warning}
                    if review_health_warning
                    else {}
                ),
                "next_step": (
                    "请重新读取这个候选；只有另行调用 "
                    "nucleus_decide_subject_candidate 才会形成主体决定。"
                ),
            }
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"长期记忆索引候选提交失败: error_type={type(exc).__name__}")
            return False, {"error": type(exc).__name__, "detail": str(exc)}


MEMORY_BOUNDARY_TOOLS = [
    LifeCreateMemoryBoundaryTool,
    LifeCreateMemoryBoundaryFromSubjectRangeTool,
    LifeReadMemoryBoundaryTool,
    LifeInspectMemoryContinuityTool,
    LifeProposeMemoryContinuityRevisionTool,
]


__all__ = [
    "MEMORY_BOUNDARY_TOOLS",
    "LifeCreateMemoryBoundaryFromSubjectRangeTool",
    "LifeCreateMemoryBoundaryTool",
    "LifeInspectMemoryContinuityTool",
    "LifeProposeMemoryContinuityRevisionTool",
    "LifeReadMemoryBoundaryTool",
]
