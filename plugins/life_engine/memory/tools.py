"""Life Engine 记忆系统工具集。

为中枢提供仿生记忆能力：
- 语义检索 + 联想
- 追加主体明确表达的 SemanticRelation 历史
- 查看显式关系历史与只读 legacy compatibility projection
"""

from __future__ import annotations

import base64
import hashlib
import inspect
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Annotated, Any, List, Literal, Optional
from uuid import uuid4

from src.app.plugin_system.api import log_api
from src.app.plugin_system.base import BaseTool

from .eligibility import assess_document_path
from .lineage import MemoryBundle
from .recall_delivery import (
    DeliveredMemorySearchRef,
    MEMORY_SEARCH_RECALL_DELIVERY_KIND,
    MEMORY_SEARCH_RECALL_POLICY,
    PendingMemorySearchRecall,
    get_memory_search_recall_delivery_coordinator,
)
from .service import LifeMemoryService

logger = log_api.get_logger("life_engine.memory_tools")

MEMORY_SEARCH_PROJECTION_VERSION = "memory-search-projection-v2"
MEMORY_SEARCH_CORE_MAX_BYTES = 16 * 1024
MEMORY_SEARCH_EXPRESSION_MAX_BYTES = 64 * 1024
MEMORY_SEARCH_MAX_ITEM_EXCERPT_BYTES = 2 * 1024
MEMORY_SEARCH_MAX_LINK_GROUP_BYTES = 2 * 1024
LEGACY_RELATION_MUTATION_RETIRED = "LegacyRelationMutationRetired"
_EXACT_DOCUMENT_FIELDS = (
    "file_path", "node_id", "document_id", "version_id", "document_revision",
    "binding_revision", "content_sha256", "file_ref",
)


def _exact_document_fields(value: Any) -> dict[str, Any]:
    """Carry declared mechanical references, never reconstruct old path lineage."""
    getter = value.get if isinstance(value, dict) else lambda key: getattr(value, key, None)
    fields = {key: getter(key) for key in _EXACT_DOCUMENT_FIELDS}
    return {
        key: item for key, item in fields.items()
        if item not in (None, "") and (
            fields.get("document_id") or key not in {"document_revision", "binding_revision"}
        )
    }


def _evidence_entity_ref(item: Any) -> str:
    """Stable selected document identity, preserving legacy entity refs as-is."""
    if item.kind == "document_evidence":
        document_id = _exact_document_fields(item.metadata).get("document_id")
        if document_id:
            return f"subject-file:{document_id}"
        return f"document:{item.record_id}"
    return f"{item.kind}:{item.record_id}"


def _bundle_primary_fields(bundle: MemoryBundle) -> dict[str, Any]:
    fields = {"primary_path": bundle.primary_path}
    for name in ("primary_node_id", "primary_document_id", "primary_version_id"):
        value = getattr(bundle, name, "")
        if value:
            fields[name] = value
    if fields.get("primary_document_id") and fields.get("primary_version_id"):
        fields["primary_file_ref"] = (
            f"subject-file:{fields['primary_document_id']}@{fields['primary_version_id']}"
        )
    return fields


def _partition_projection_links(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Page large same-content link sets without dropping any exact reference.

    Content hashes still deduplicate bytes. A content ref may recur on later
    pages with explicitly numbered link groups; only its association metadata
    is partitioned, with order and source semantics unchanged.
    """
    result = []
    for record in records:
        groups: list[list[dict[str, Any]]] = []
        current: list[dict[str, Any]] = []
        for link in record["links"]:
            if current and len(_canonical_json_bytes([*current, link])) > MEMORY_SEARCH_MAX_LINK_GROUP_BYTES:
                groups.append(current)
                current = []
            current.append(link)
        groups.append(current)
        for index, group in enumerate(groups):
            split = {**record, "links": group}
            if len(groups) > 1:
                split.update({"link_group_index": index, "link_group_count": len(groups)})
            result.append(split)
    return result


@dataclass(frozen=True, slots=True)
class _RelationRuntime:
    """Trusted runtime identity for one explicit relation operation."""

    memory_service: LifeMemoryService
    life_service: Any
    owner_subject_id: str
    actor_consciousness_instance_id: str
    stream_scope: str
    source_occurrence_id: str
    source_occurrence_kind: str
    tool_call_id: str


@dataclass(frozen=True, slots=True)
class _SearchRecallIdentity:
    """Runtime-bound identity shared by every page of one logical recall."""

    actor_consciousness_instance_id: str
    stream_scope: str
    source_occurrence_id: str
    recall_chain_id: str
    recorded_at: str


def _resolve_search_recall_identity(
    tool: BaseTool,
    *,
    binding: dict[str, Any],
) -> _SearchRecallIdentity | None:
    """Resolve an active actor and stable source turn for recall evidence.

    Search remains usable in isolated projection tests where the Life Engine
    registry is intentionally absent, but then no recall trace is staged.  In
    the running plugin the registry is present and binds every page to the
    actual active consciousness instance instead of the old ``life_engine``
    placeholder actor.
    """

    try:
        from ..service.registry import get_life_engine_service

        service = get_life_engine_service()
        if service is None:
            return None
        stream_scope = str(tool.get_current_stream_id() or "").strip()
        actor = str(service.resolve_consciousness_instance(stream_scope) or "").strip()
        instance = service.consciousness_registry.get(actor)
        if not actor or instance is None or not instance.is_active:
            return None
        source_occurrence_id, _ = _relation_source_occurrence(tool)
        material = json.dumps(
            binding,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        digest = hashlib.sha256(
            (
                actor
                + "\0"
                + stream_scope
                + "\0"
                + source_occurrence_id
                + "\0"
                + material
            ).encode("utf-8")
        ).hexdigest()
        return _SearchRecallIdentity(
            actor_consciousness_instance_id=actor,
            stream_scope=stream_scope,
            source_occurrence_id=source_occurrence_id,
            recall_chain_id=f"memory_search_recall:{digest}",
            recorded_at=_search_source_recorded_at(tool),
        )
    except (AttributeError, PermissionError, RuntimeError, ValueError):
        return None


def _relation_source_occurrence(tool: BaseTool) -> tuple[str, str]:
    """Return the exact source occurrence already bound to this tool call."""

    bound_occurrence = str(
        getattr(tool, "_life_source_occurrence_id", "") or ""
    ).strip()
    if bound_occurrence:
        return bound_occurrence, "life_source"

    message = getattr(tool, "trigger_message", None)
    extra = getattr(message, "extra", {}) or {}
    turn_scope = extra.get("life_turn_scope") if isinstance(extra, dict) else None
    turn_key = (
        str(turn_scope.get("turn_key") or "").strip()
        if isinstance(turn_scope, dict)
        else ""
    )
    if turn_key:
        return turn_key, "life_turn"

    message_id = str(getattr(message, "message_id", "") or "").strip()
    if message_id:
        return message_id, "message"

    tool_call_id = str(getattr(tool, "_tool_call_id", "") or "").strip()
    if tool_call_id:
        return tool_call_id, "tool_call"
    raise PermissionError("SemanticRelationSourceOccurrenceRequired")


def _relation_recorded_at(tool: BaseTool) -> str:
    """Use the source turn timestamp when available for replay stability."""

    value = getattr(tool, "_life_source_occurred_at", None)
    if value in (None, ""):
        value = getattr(getattr(tool, "trigger_message", None), "time", None)
    if isinstance(value, datetime):
        parsed = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        return parsed.astimezone(UTC).isoformat()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.fromtimestamp(float(value), tz=UTC).isoformat()
    text = str(value or "").strip()
    if text:
        try:
            parsed = datetime.fromisoformat(text)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return parsed.astimezone(UTC).isoformat()
        except ValueError:
            pass
    return datetime.now(UTC).isoformat()


def _search_source_recorded_at(tool: BaseTool) -> str:
    """Return only a stable source timestamp; never invent recall history time."""

    value = getattr(tool, "_life_source_occurred_at", None)
    if value in (None, ""):
        value = getattr(getattr(tool, "trigger_message", None), "time", None)
    if isinstance(value, datetime):
        parsed = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        return parsed.astimezone(UTC).isoformat()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.fromtimestamp(float(value), tz=UTC).isoformat()
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return ""
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat()


async def _resolve_relation_runtime(tool: BaseTool) -> _RelationRuntime:
    """Resolve an active consciousness actor from the current runtime stream."""

    from ..service.registry import get_life_engine_service

    service = get_life_engine_service()
    if service is None:
        raise RuntimeError("LifeEngineServiceUnavailable")
    memory_service = getattr(service, "memory_service", None)
    if memory_service is None:
        raise RuntimeError("LifeMemoryServiceUnavailable")

    stream_scope = str(tool.get_current_stream_id() or "").strip()
    if not stream_scope:
        raise PermissionError("SemanticRelationStreamOwnerRequired")
    instance = service.consciousness_registry.get_for_stream(stream_scope)
    if instance is None or not instance.is_active:
        raise PermissionError("SemanticRelationActorIsNotActive")
    actor = str(instance.instance_id or "").strip()
    if not actor:
        raise PermissionError("SemanticRelationActorIdentityRequired")

    tool_call_id = str(getattr(tool, "_tool_call_id", "") or "").strip()
    if not tool_call_id:
        raise PermissionError("SemanticRelationToolCallIdentityRequired")
    source_occurrence_id, source_occurrence_kind = _relation_source_occurrence(tool)
    return _RelationRuntime(
        memory_service=memory_service,
        life_service=service,
        # The trusted registry contains windows of this one continuous subject.
        # Instance and stream identity remain occurrence provenance, not owners.
        owner_subject_id="elysia",
        actor_consciousness_instance_id=actor,
        stream_scope=stream_scope,
        source_occurrence_id=source_occurrence_id,
        source_occurrence_kind=source_occurrence_kind,
        tool_call_id=tool_call_id,
    )


def _stable_relation_id(runtime: _RelationRuntime) -> str:
    """Bind one append identity to the exact consciousness tool occurrence."""

    digest = hashlib.sha256(
        (
            runtime.actor_consciousness_instance_id
            + "\0"
            + runtime.stream_scope
            + "\0"
            + runtime.source_occurrence_id
            + "\0"
            + runtime.tool_call_id
        ).encode("utf-8")
    ).hexdigest()
    return f"relation_{digest}"


def _same_semantic_relation(existing: Any, proposed: Any) -> bool:
    """Compare immutable relation content while allowing stored timestamp reuse."""

    fields = (
        "relation_id",
        "source_ref",
        "target_ref",
        "predicate",
        "reason",
        "actor",
        "consciousness_instance_id",
        "stream_scope",
        "metadata",
        "owner_subject_id",
        "root_relation_id",
        "parent_relation_id",
        "revision",
        "operation",
    )
    return all(getattr(existing, field) == getattr(proposed, field) for field in fields)


def _semantic_relation_payload(relation: Any, *, center_ref: str) -> dict[str, Any]:
    """Project one authoritative semantic history row without inventing meaning."""

    if relation.source_ref == center_ref:
        direction = "outgoing"
        counterpart_ref = relation.target_ref
    elif relation.target_ref == center_ref:
        direction = "incoming"
        counterpart_ref = relation.source_ref
    else:
        direction = "unbound"
        counterpart_ref = ""
    return {
        "relation_id": relation.relation_id,
        "source_ref": relation.source_ref,
        "target_ref": relation.target_ref,
        "predicate": relation.predicate,
        "reason": relation.reason,
        "actor": relation.actor,
        "recorded_at": relation.recorded_at,
        "consciousness_instance_id": relation.consciousness_instance_id,
        "stream_scope": relation.stream_scope,
        "direction": direction,
        "counterpart_ref": counterpart_ref,
        "metadata": dict(relation.metadata),
        "owner_subject_id": relation.owner_subject_id,
        "root_relation_id": relation.root_relation_id,
        "parent_relation_id": relation.parent_relation_id,
        "revision": relation.revision,
        "operation": relation.operation,
        "legacy_owner_unbound": relation.owner_subject_id is None,
        "read_only": relation.owner_subject_id is None,
    }


def _legacy_relation_mutation_retired_payload(action: str) -> dict[str, Any]:
    """Return the stable fail-closed contract for retired graph mutations."""

    return {
        "error": LEGACY_RELATION_MUTATION_RETIRED,
        "error_type": LEGACY_RELATION_MUTATION_RETIRED,
        "action": str(action or "").strip().lower(),
        "mutated": False,
        "message": (
            "Legacy memory_edges remain read-only compatibility data. "
            "Use explicit revise/withdraw with root and parent IDs for owned relations."
        ),
    }


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


def _utf8_prefix(value: str, max_bytes: int) -> str:
    budget = max(0, int(max_bytes))
    encoded = str(value or "").encode("utf-8")
    if len(encoded) <= budget:
        return str(value or "")
    return encoded[:budget].decode("utf-8", errors="ignore")


def _memory_search_budget(task_name: str) -> int:
    normalized = str(task_name or "").strip().lower()
    if normalized in {"expression", "life_chatter"}:
        return MEMORY_SEARCH_EXPRESSION_MAX_BYTES
    return MEMORY_SEARCH_CORE_MAX_BYTES


def _memory_search_tool_result_bytes(value: Any) -> int:
    """Measure the exact UTF-8 bytes emitted by ``ToolResult.to_text``."""

    return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))


def _encode_memory_search_continuation(state: dict[str, Any]) -> str:
    raw = _canonical_json_bytes(state)
    body = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    checksum = hashlib.sha256(raw).hexdigest()[:16]
    return f"{body}.{checksum}"


def _decode_memory_search_continuation(token: str) -> dict[str, Any]:
    try:
        body, checksum = str(token or "").split(".", 1)
        padded = body + "=" * (-len(body) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        if hashlib.sha256(raw).hexdigest()[:16] != checksum:
            raise ValueError("checksum mismatch")
        loaded = json.loads(raw.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise ValueError("invalid memory search continuation") from exc
    if not isinstance(loaded, dict):
        raise ValueError("invalid memory search continuation payload")
    return loaded


def _eligible_path_or_error(file_path: str) -> tuple[str | None, str | None]:
    """Normalize a tool path without allowing runtime/internal memory domains."""
    decision = assess_document_path(file_path)
    if decision.eligible:
        return decision.path, None
    return None, f"不是可操作的记忆文档: {decision.reason}"



async def _resolve_relation_endpoint(
    tool: BaseTool,
    *,
    entity_ref: str,
    file_path: str,
    life_service: Any = None,
) -> tuple[str, str]:
    """Authorize stable or exact file metadata without retargeting its identity."""
    if entity_ref and file_path:
        raise ValueError("SemanticRelationReferencePathConflict")
    if not entity_ref:
        if not file_path:
            raise ValueError("SemanticRelationEndpointRequired")
        path, error = _eligible_path_or_error(file_path)
        if error or path is None:
            raise ValueError("SemanticRelationPathIneligible")
        return f"document:{path}", path

    from ..tools.managed_files import (
        SelectedSubjectStorageNotStarted,
        parse_file_reference,
        selected_file_session,
    )

    # Exact-reference syntax is intentionally shared with the normal file reader.
    # A stable selector is parsed with a syntax-only marker, then authorized using
    # its actual head descriptor; the marker is never looked up or persisted.
    exact = "@" in entity_ref
    document_id, _ = parse_file_reference(
        entity_ref if exact else f"{entity_ref}@ver_syntax_only"
    )
    if life_service is None:
        from ..service.registry import get_life_engine_service

        life_service = get_life_engine_service()
    session = selected_file_session(tool, life_service)
    if session is None:
        raise SelectedSubjectStorageNotStarted()
    authorization_ref = entity_ref
    if not exact:
        head = await session.store.get_document_head(document_id)
        if head is None:
            raise ValueError("ManagedFileDocumentNotFound")
        if head.document_id != document_id:
            raise ValueError("ManagedFileReferenceDocumentConflict")
        authorization_ref = f"{entity_ref}@{head.current_version_id}"
    target, _, _ = await session.resolve_reference(authorization_ref)
    return entity_ref, session.relative(target)


async def _authorize_stored_relation_endpoint(
    tool: BaseTool, entity_ref: str, *, life_service: Any
) -> tuple[str, str]:
    """Recheck the current authority boundary without changing a parent's refs."""
    return await _resolve_relation_endpoint(
        tool,
        entity_ref="" if entity_ref.startswith("document:") else entity_ref,
        file_path=entity_ref[len("document:"):] if entity_ref.startswith("document:") else "",
        life_service=life_service,
    )

# ============================================================
# nucleus_search_memory - 语义检索 + 联想
# ============================================================

class LifeEngineSearchMemoryTool(BaseTool):
    """语义检索 + 联想工具。"""

    tool_name: str = "nucleus_search_memory"
    tool_description: str = (
        "搜索记忆并触发联想。结合关键词和语义检索，找到相关的记忆。"
        "\n\n"
        "**何时使用：**\n"
        "- ✓ 想回忆「我之前对XX有过什么想法」\n"
        "- ✓ 搜索一个主题的所有相关记忆\n"
        "- ✓ 探索记忆之间的潜在联系\n"
        "\n"
        "**何时不用：**\n"
        "- ✗ 知道确切文件路径 → 用 nucleus_read_file\n"
        "- ✗ 搜索文件中的具体关键词 → 用 nucleus_grep_file\n"
        "\n"
        "**💡 联想结果怎么看：**\n"
        "- source='direct'：直接命中的记忆\n"
        "- source='associated'：通过关联路径联想到的，association_path 显示联想路线\n"
        "- memory_bundles：当前理解 + 历史轨迹 + 修正记录；旧记忆不会被删除，会作为演化证据保留\n"
        "- enable_association=false：关闭额外联想及记忆包的历史关系展开，保留直接检索证据\n"
        "\n"
        "**认识论边界：** search_mode 可自由描述本次回忆意图；相关性排名不等于事实置信度。"
        "第一人称见证表达爱莉如何经历，不自动证明其中的外部事实。\n\n"
        "**注意：** 搜索和联想是只读操作，不会自动增强激活强度或创建/强化关联边。"
    )
    chatter_allow: list[str] = ["life_engine_internal", "life_chatter"]

    def __init__(self, plugin):
        super().__init__(plugin)

    async def _get_service(self) -> LifeMemoryService:
        """获取记忆服务实例。"""
        from ..service import LifeEngineService

        service = LifeEngineService.get_instance()
        memory_service = None if service is None else service.memory_service
        if memory_service is None:
            raise RuntimeError("记忆服务未初始化")
        return memory_service

    def _result_budget(self) -> int:
        return _memory_search_budget(getattr(self, "_runtime_task_name", ""))

    @staticmethod
    def _projection_records(
        evidence_results: list[Any],
        bundles: list[MemoryBundle],
    ) -> list[dict[str, Any]]:
        records: dict[str, dict[str, Any]] = {}

        def add_content(content: Any, link: dict[str, Any]) -> str:
            text = str(content or "")
            encoded = text.encode("utf-8")
            digest = hashlib.sha256(encoded).hexdigest()
            ref = f"memory-content:sha256:{digest}"
            record = records.setdefault(
                ref,
                {
                    "ref": ref,
                    "content": text,
                    "content_sha256": digest,
                    "original_bytes": len(encoded),
                    "links": [],
                },
            )
            link_identity = _sha256_json(link)
            if all(
                _sha256_json(existing) != link_identity
                for existing in record["links"]
            ):
                record["links"].append(link)
            return ref

        for item in evidence_results:
            entity_ref = _evidence_entity_ref(item)
            add_content(
                item.content,
                {
                    "link_type": "evidence",
                    "entity_ref": entity_ref,
                    "record_id": item.record_id,
                    **_exact_document_fields(item.metadata),
                    "kind": item.kind,
                    "rank_score": round(float(item.rank_score), 6),
                    "confidence": item.confidence,
                    "source": item.source,
                    "valid_from": item.valid_from,
                    "valid_to": item.valid_to,
                    "recorded_at": item.recorded_at,
                    "stream_scope": item.stream_scope,
                    "visibility": item.visibility,
                    "status": item.status,
                    "provenance_count": len(item.provenance),
                    "provenance_sha256": _sha256_json(list(item.provenance)),
                    "metadata_bytes": len(_canonical_json_bytes(item.metadata)),
                    "metadata_sha256": _sha256_json(item.metadata),
                },
            )

        for bundle_index, bundle in enumerate(bundles):
            primary = _bundle_primary_fields(bundle)
            bundle_id = (
                "memory-bundle:"
                + _sha256_json({"ordinal": bundle_index, **primary})
            )
            if bundle.current_understanding:
                add_content(
                    bundle.current_understanding,
                    {
                        "link_type": "bundle_current",
                        "bundle_id": bundle_id,
                        **primary,
                    },
                )
            for index, item in enumerate(bundle.evidence):
                relation_reason_ref = ""
                if item.relation_reason:
                    relation_reason_ref = add_content(
                        item.relation_reason,
                        {
                            "link_type": "bundle_relation_reason",
                            "bundle_id": bundle_id,
                            **primary,
                            **_exact_document_fields(item),
                            "ordinal": index,
                            "file_path": item.file_path,
                        },
                    )
                add_content(
                    item.snippet,
                    {
                        "link_type": "bundle_evidence",
                        "bundle_id": bundle_id,
                        **primary,
                        **_exact_document_fields(item),
                        "ordinal": index,
                        "file_path": item.file_path,
                        "title": item.title,
                        "relevance": round(float(item.relevance), 3),
                        "source": item.source,
                        "relation": item.relation,
                        "relation_reason_ref": relation_reason_ref,
                        "exists": bool(item.exists),
                    },
                )
            for index, item in enumerate(bundle.history_trace):
                reason_ref = ""
                if item.reason:
                    reason_ref = add_content(
                        item.reason,
                        {
                            "link_type": "bundle_history_reason",
                            "bundle_id": bundle_id,
                            **primary,
                            **_exact_document_fields(item),
                            "ordinal": index,
                            "file_path": item.file_path,
                        },
                    )
                add_content(
                    item.snippet,
                    {
                        "link_type": "bundle_history",
                        "bundle_id": bundle_id,
                        **primary,
                        **_exact_document_fields(item),
                        "ordinal": index,
                        "direction": item.direction,
                        "relation": item.relation,
                        "file_path": item.file_path,
                        "title": item.title,
                        "reason_ref": reason_ref,
                        "exists": bool(item.exists),
                    },
                )
            for index, item in enumerate(bundle.corrections):
                add_content(
                    item.message,
                    {
                        "link_type": "bundle_correction",
                        "bundle_id": bundle_id,
                        **primary,
                        "ordinal": index,
                        "topic": item.topic,
                        "source": item.source,
                        "created_at": item.created_at,
                    },
                )
            if bundle.uncertainty:
                add_content(
                    bundle.uncertainty,
                    {
                        "link_type": "bundle_uncertainty",
                        "bundle_id": bundle_id,
                        **primary,
                    },
                )
        return _partition_projection_links(list(records.values()))

    @staticmethod
    def _project_record(
        record: dict[str, Any],
        *,
        delivery: str,
        excerpt_bytes: int = 0,
    ) -> dict[str, Any]:
        projected = {
            "ref": record["ref"],
            "content_sha256": record["content_sha256"],
            "original_bytes": int(record["original_bytes"]),
            "delivery": delivery,
            "links": list(record["links"]),
        }
        for key in ("link_group_index", "link_group_count"):
            if key in record:
                projected[key] = record[key]
        if delivery == "full":
            projected["content"] = record["content"]
            projected["delivered_content_bytes"] = int(record["original_bytes"])
        elif delivery == "excerpt":
            excerpt = _utf8_prefix(record["content"], excerpt_bytes)
            projected["content"] = excerpt
            projected["delivered_content_bytes"] = len(excerpt.encode("utf-8"))
        else:
            projected["delivered_content_bytes"] = 0
        return projected

    @staticmethod
    def _projection_indexes(
        items: list[dict[str, Any]],
    ) -> tuple[
        list[dict[str, Any]],
        list[dict[str, Any]],
        list[dict[str, Any]],
        list[dict[str, Any]],
    ]:
        evidence: list[dict[str, Any]] = []
        direct: list[dict[str, Any]] = []
        associated: list[dict[str, Any]] = []
        bundles: dict[str, dict[str, Any]] = {}
        for item in items:
            content_ref = str(item["ref"])
            delivery = str(item["delivery"])
            for link in item["links"]:
                kind = str(link.get("link_type") or "")
                projected_link = {
                    key: value
                    for key, value in link.items()
                    if key != "link_type"
                }
                projected_link.update(
                    {
                        "content_ref": content_ref,
                        "content_delivery": delivery,
                    }
                )
                if kind == "evidence":
                    evidence.append(projected_link)
                    continue
                bundle_id = str(link.get("bundle_id") or "")
                if not bundle_id:
                    continue
                bundle = bundles.setdefault(
                    bundle_id,
                    {
                        "bundle_id": bundle_id,
                        "primary_path": str(link.get("primary_path") or ""),
                        "current_refs": [],
                        "evidence_refs": [],
                        "history_refs": [],
                        "correction_refs": [],
                        "uncertainty_refs": [],
                        "relation_reason_refs": [],
                    },
                )
                if link.get("primary_path") and not bundle["primary_path"]:
                    bundle["primary_path"] = str(link["primary_path"])
                for key in (
                    "primary_node_id", "primary_document_id", "primary_version_id", "primary_file_ref"
                ):
                    if link.get(key):
                        bundle[key] = link[key]
                if kind == "bundle_current":
                    bundle["current_refs"].append(content_ref)
                elif kind == "bundle_evidence":
                    relation = dict(projected_link)
                    bundle["evidence_refs"].append(content_ref)
                    if str(link.get("source") or "") == "associated":
                        associated.append(relation)
                    else:
                        direct.append(relation)
                elif kind == "bundle_history":
                    bundle["history_refs"].append(content_ref)
                elif kind == "bundle_correction":
                    bundle["correction_refs"].append(content_ref)
                elif kind == "bundle_uncertainty":
                    bundle["uncertainty_refs"].append(content_ref)
                elif kind in {"bundle_relation_reason", "bundle_history_reason"}:
                    bundle["relation_reason_refs"].append(content_ref)
        return evidence, direct, associated, list(bundles.values())

    @classmethod
    def _projection_payload(
        cls,
        *,
        query: str,
        mode: str,
        stream_scope: str | None,
        valid_at: str,
        recorded_as_of: str,
        episode: Any,
        trace_available: bool,
        recall_delivery_binding: dict[str, Any] | None,
        items: list[dict[str, Any]],
        total_evidence: int,
        budget: int,
        frontier_sha256: str,
        original_items: int,
        original_bytes: int,
        omitted_items: int,
        truncated: bool,
        continuation: str,
    ) -> dict[str, Any]:
        evidence, direct, associated, bundles = cls._projection_indexes(items)
        return {
            "action": "search_memory",
            "query": query,
            "query_sha256": hashlib.sha256(query.encode("utf-8")).hexdigest(),
            "direct_results": direct,
            "associated_results": associated,
            "memory_bundles": bundles,
            "search_mode": mode,
            "stream_scope": stream_scope,
            "valid_at": valid_at,
            "recorded_as_of": recorded_as_of,
            "recall_episode": {
                "episode_id": episode.episode_id,
                "policy_version": episode.policy_version,
                "random_seed": episode.random_seed,
                "context_key": episode.context_key,
                "recall_chain_id": str(
                    getattr(episode, "recall_chain_id", "") or ""
                ),
                "consciousness_instance_id": str(
                    getattr(episode, "consciousness_instance_id", "") or ""
                ),
                "source_occurrence_id": str(
                    getattr(episode, "source_occurrence_id", "") or ""
                ),
                "persisted": False,
                "trace_state": (
                    "pending_exact_tool_result_delivery"
                    if trace_available and recall_delivery_binding
                    else "unavailable"
                ),
            },
            "recall_delivery_binding": recall_delivery_binding,
            "evidence_results": evidence,
            "canonical_items": items,
            "total_found": total_evidence,
            "projection_version": MEMORY_SEARCH_PROJECTION_VERSION,
            "frontier_sha256": frontier_sha256,
            "budget_bytes": budget,
            "original_bytes": original_bytes,
            "original_items": original_items,
            "delivered_bytes": 0,
            "delivered_items": len(items),
            "omitted_bytes": 0,
            "omitted_items": omitted_items,
            "truncated": truncated,
            "continuation": continuation,
        }

    @staticmethod
    def _finalize_projection_bytes(
        payload: dict[str, Any],
        *,
        original_bytes: int,
        force_no_omission: bool = False,
    ) -> dict[str, Any]:
        finalized = dict(payload)
        finalized["delivered_bytes"] = 0
        finalized["omitted_bytes"] = 0
        for _ in range(32):
            actual = _memory_search_tool_result_bytes(finalized)
            omitted = 0 if force_no_omission else max(
                0,
                int(original_bytes) - actual,
            )
            if (
                int(finalized["delivered_bytes"]) == actual
                and int(finalized["omitted_bytes"]) == omitted
            ):
                return finalized
            finalized["delivered_bytes"] = actual
            finalized["omitted_bytes"] = omitted
        raise RuntimeError("memory search projection byte accounting did not converge")

    @staticmethod
    def _delivered_entity_refs(items: list[dict[str, Any]]) -> tuple[str, ...]:
        """Return only evidence refs visibly projected on this exact page."""

        evidence, _direct, _associated, _bundles = (
            LifeEngineSearchMemoryTool._projection_indexes(items)
        )
        return tuple(
            dict.fromkeys(
                str(item.get("entity_ref") or "").strip()
                for item in evidence
                if str(item.get("entity_ref") or "").strip()
            )
        )

    @staticmethod
    def _recall_delivery_binding(
        *,
        delivery_id: str,
        recall_chain_id: str,
        episode_id: str,
        page_offset: int,
        delivered_refs: tuple[str, ...],
    ) -> dict[str, Any] | None:
        """Build the compact binding that is itself inside the byte budget."""

        if not delivery_id or not delivered_refs:
            return None
        refs_bytes = json.dumps(
            list(delivered_refs),
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        return {
            "kind": MEMORY_SEARCH_RECALL_DELIVERY_KIND,
            "delivery_id": delivery_id,
            "recall_chain_id": recall_chain_id,
            "episode_id": episode_id,
            "page_offset": int(page_offset),
            "delivered_ref_count": len(delivered_refs),
            "delivered_refs_sha256": hashlib.sha256(refs_bytes).hexdigest(),
        }

    async def execute(
        self,
        query: Annotated[str, "搜索问题"],
        top_k: Annotated[int, "返回数量"] = 5,
        enable_association: Annotated[
            bool, "是否显式启用额外联想与记忆包历史关系展开；默认 false 保留直接检索证据，增强可达性不代表真值或收益"
        ] = False,
        file_types: Annotated[Optional[List[str]], "限定文件类型"] = None,
        time_range_days: Annotated[int, "时间范围（天），0=不限"] = 0,
        search_mode: Annotated[
            str,
            "自由描述这次回忆想寻找什么；不会被代码归入固定认知类别",
        ] = "",
        stream_scope: Annotated[
            Optional[str],
            "可见的聊天流范围；不提供时不跨流读取私有见证",
        ] = None,
        valid_at: Annotated[
            str,
            "查询现实世界中哪个时点有效的主张；ISO 8601，留空表示不限",
        ] = "",
        recorded_as_of: Annotated[
            str,
            "查询系统在何时已经知道的记录；ISO 8601，留空表示当前",
        ] = "",
        continuation: Annotated[
            str,
            "可选；上一页返回的稳定 continuation。查询或结果前沿变化时会显式失败。",
        ] = "",
    ) -> tuple[bool, dict[str, Any]]:
        """执行记忆搜索。"""
        if not query or not query.strip():
            return False, {"error": "query 不能为空"}

        mode = str(search_mode or "").strip()
        normalized_query = query.strip()
        query_sha256 = hashlib.sha256(normalized_query.encode("utf-8")).hexdigest()
        budget = self._result_budget()
        recall_binding = {
            "query_sha256": query_sha256,
            "search_mode": mode,
            "stream_scope": str(stream_scope or ""),
            "valid_at": str(valid_at or ""),
            "recorded_as_of": str(recorded_as_of or ""),
            "file_types": sorted(str(item) for item in (file_types or [])),
            "time_range_days": int(time_range_days),
            "top_k": int(top_k),
            "enable_association": bool(enable_association),
        }
        recall_identity = _resolve_search_recall_identity(
            self,
            binding=recall_binding,
        )
        continuation_state: dict[str, Any] | None = None
        if continuation:
            try:
                continuation_state = _decode_memory_search_continuation(continuation)
            except ValueError as exc:
                return False, {"error": str(exc)}
            if (
                continuation_state.get("version") != MEMORY_SEARCH_PROJECTION_VERSION
                or continuation_state.get("query_sha256") != query_sha256
                or int(continuation_state.get("budget_bytes") or 0) != budget
            ):
                return False, {"error": "memory search continuation does not match this query/task"}
            continuation_trace_bound = bool(
                continuation_state.get("trace_bound", False)
            )
            if continuation_trace_bound and recall_identity is not None:
                if any(
                    (
                        str(continuation_state.get("recall_chain_id") or "")
                        != recall_identity.recall_chain_id,
                        str(
                            continuation_state.get(
                                "actor_consciousness_instance_id"
                            )
                            or ""
                        )
                        != recall_identity.actor_consciousness_instance_id,
                        str(continuation_state.get("source_occurrence_id") or "")
                        != recall_identity.source_occurrence_id,
                        str(continuation_state.get("trace_stream_scope") or "")
                        != recall_identity.stream_scope,
                    )
                ):
                    return False, {
                        "error": (
                            "memory search continuation does not match the active "
                            "consciousness/source occurrence"
                        )
                    }
            elif not continuation_trace_bound:
                # A page chain that began without a provable actor never starts
                # emitting durable traces halfway through pagination.
                recall_identity = None

        try:
            service = await self._get_service()

            effective_stream = str(
                stream_scope or self.get_current_stream_id() or ""
            )
            trace_stream_scope = (
                recall_identity.stream_scope
                if recall_identity is not None
                else str(
                    (continuation_state or {}).get("trace_stream_scope") or ""
                )
            )
            context_key = "/".join(
                item for item in ("life_engine", trace_stream_scope or effective_stream) if item
            )
            chain_id = (
                str((continuation_state or {}).get("recall_chain_id") or "").strip()
                if continuation_state is not None
                else (
                    recall_identity.recall_chain_id
                    if recall_identity is not None
                    else ""
                )
            )
            if chain_id:
                episode_id = "recall_" + hashlib.sha256(
                    f"episode\0{chain_id}".encode("utf-8")
                ).hexdigest()
                initial_seed = int(
                    hashlib.sha256(chain_id.encode("utf-8")).hexdigest()[:16],
                    16,
                ) & ((1 << 63) - 1)
            else:
                episode_id = str(
                    (continuation_state or {}).get("episode_id") or ""
                ).strip() or f"unpersisted_recall_{uuid4().hex}"
                initial_seed = uuid4().int & ((1 << 63) - 1)
            retrieval_seed = (
                int(continuation_state.get("random_seed") or 0)
                if continuation_state is not None
                else initial_seed
            )
            if chain_id and retrieval_seed != initial_seed:
                return False, {
                    "error": "memory search continuation random seed is invalid"
                }
            if continuation_state is not None and str(
                continuation_state.get("episode_id") or ""
            ) != episode_id:
                return False, {
                    "error": "memory search continuation episode identity is invalid"
                }
            recorded_at = (
                str(continuation_state.get("recorded_at") or "").strip()
                if continuation_state is not None
                else (
                    recall_identity.recorded_at
                    if recall_identity is not None
                    else ""
                )
            )
            trace_available = bool(
                recall_identity is not None
                and chain_id
                and recorded_at
                and bool(
                    (continuation_state or {}).get("trace_bound", True)
                )
            )
            episode = SimpleNamespace(
                episode_id=episode_id,
                policy_version=MEMORY_SEARCH_RECALL_POLICY,
                random_seed=retrieval_seed,
                context_key=context_key,
                recall_chain_id=chain_id,
                consciousness_instance_id=(
                    recall_identity.actor_consciousness_instance_id
                    if trace_available and recall_identity is not None
                    else ""
                ),
                source_occurrence_id=(
                    recall_identity.source_occurrence_id
                    if trace_available and recall_identity is not None
                    else ""
                ),
            )
            expand_associations = getattr(
                service,
                "expand_living_document_associations",
                None,
            )
            document_results = await service.search_memory(
                normalized_query,
                top_k=top_k,
                # The canonical living ledger owns new relation/co-recall
                # expansion.  Legacy weighted edges remain read-only and are
                # used only by older services that do not expose that Port.
                enable_association=(
                    bool(enable_association) and not callable(expand_associations)
                ),
                file_types=file_types,
                time_range_days=time_range_days,
                return_bundles=False,
            )
            if bool(enable_association) and callable(expand_associations):
                document_results = await expand_associations(
                    document_results,
                    context_key=context_key,
                    random_seed=retrieval_seed,
                    limit=max(0, int(top_k)),
                )
            build_bundles = getattr(service, "build_memory_bundles", None)
            bundles = (
                await build_bundles(
                    query=normalized_query,
                    results=document_results,
                    top_k=top_k,
                )
                if bool(enable_association) and callable(build_bundles)
                else []
            )
            evidence_search = service.search_evidence_aware
            evidence_kwargs: dict[str, Any] = {
                "mode": mode,
                "top_k": top_k,
                "stream_scope": stream_scope,
                "enable_association": enable_association,
                "valid_at": valid_at,
                "recorded_as_of": recorded_as_of,
            }
            parameters = inspect.signature(evidence_search).parameters
            if "document_results" in parameters or any(
                item.kind is inspect.Parameter.VAR_KEYWORD
                for item in parameters.values()
            ):
                evidence_kwargs["document_results"] = document_results
            if "association_context_key" in parameters or any(
                item.kind is inspect.Parameter.VAR_KEYWORD
                for item in parameters.values()
            ):
                evidence_kwargs["association_context_key"] = context_key
                evidence_kwargs["association_random_seed"] = retrieval_seed
            evidence_results = await evidence_search(normalized_query, **evidence_kwargs)

            records = self._projection_records(evidence_results, bundles)
            frontier_sha256 = _sha256_json(
                [
                    {
                        "ref": record["ref"],
                        "links_sha256": _sha256_json(record["links"]),
                    }
                    for record in records
                ]
            )
            if continuation_state is not None and (
                continuation_state.get("frontier_sha256") != frontier_sha256
                or int(continuation_state.get("random_seed") or 0) != retrieval_seed
            ):
                return False, {"error": "memory search continuation frontier changed"}
            offset = (
                int(continuation_state.get("offset") or 0)
                if continuation_state is not None
                else 0
            )
            if offset < 0 or offset > len(records):
                return False, {"error": "memory search continuation offset is invalid"}

            trace_capable = all(
                callable(getattr(service, name, None))
                for name in (
                    "begin_memory_recall",
                    "append_memory_recall_events",
                    "append_memory_corecall",
                )
            )
            trace_available = bool(trace_available and trace_capable)
            recall_delivery_id = ""
            if trace_available and recall_identity is not None and evidence_results:
                delivery_material = (
                    chain_id
                    + "\0"
                    + frontier_sha256
                    + "\0"
                    + str(offset)
                )
                delivery_digest = hashlib.sha256(
                    delivery_material.encode("utf-8")
                ).hexdigest()
                recall_delivery_id = f"memory_search_delivery:{delivery_digest}"

            projection_episode = SimpleNamespace(
                episode_id=episode.episode_id,
                policy_version=episode.policy_version,
                random_seed=retrieval_seed,
                context_key=episode.context_key,
                recall_chain_id=chain_id,
                consciousness_instance_id=episode.consciousness_instance_id,
                source_occurrence_id=episode.source_occurrence_id,
            )
            full_items = [
                self._project_record(record, delivery="full")
                for record in records
            ]
            full_delivery_binding = self._recall_delivery_binding(
                delivery_id=recall_delivery_id,
                recall_chain_id=chain_id,
                episode_id=episode_id,
                page_offset=offset,
                delivered_refs=self._delivered_entity_refs(full_items),
            )
            original_bytes = 0
            for _ in range(12):
                original_payload = self._projection_payload(
                    query=normalized_query,
                    mode=mode,
                    stream_scope=stream_scope,
                    valid_at=valid_at,
                    recorded_as_of=recorded_as_of,
                    episode=projection_episode,
                    trace_available=trace_available,
                    recall_delivery_binding=full_delivery_binding,
                    items=full_items,
                    total_evidence=len(evidence_results),
                    budget=budget,
                    frontier_sha256=frontier_sha256,
                    original_items=len(records),
                    original_bytes=original_bytes,
                    omitted_items=0,
                    truncated=False,
                    continuation="",
                )
                original_payload = self._finalize_projection_bytes(
                    original_payload,
                    original_bytes=original_bytes,
                    force_no_omission=True,
                )
                measured = int(original_payload["delivered_bytes"])
                if measured == original_bytes:
                    break
                original_bytes = measured

            selected_items: list[dict[str, Any]] = []
            final_payload: dict[str, Any] | None = None
            for index in range(offset, len(records)):
                record = records[index]
                variants = [self._project_record(record, delivery="full")]
                if int(record["original_bytes"]) > MEMORY_SEARCH_MAX_ITEM_EXCERPT_BYTES:
                    variants.append(
                        self._project_record(
                            record,
                            delivery="excerpt",
                            excerpt_bytes=MEMORY_SEARCH_MAX_ITEM_EXCERPT_BYTES,
                        )
                    )
                variants.append(self._project_record(record, delivery="ref"))
                accepted: tuple[dict[str, Any], dict[str, Any]] | None = None
                for variant in variants:
                    candidate_items = [*selected_items, variant]
                    candidate_offset = index + 1
                    candidate_continuation = ""
                    if candidate_offset < len(records):
                        candidate_continuation = _encode_memory_search_continuation(
                            {
                                "version": MEMORY_SEARCH_PROJECTION_VERSION,
                                "query_sha256": query_sha256,
                                "frontier_sha256": frontier_sha256,
                                "offset": candidate_offset,
                                "random_seed": retrieval_seed,
                                "budget_bytes": budget,
                                "recall_chain_id": chain_id,
                                "episode_id": episode_id,
                                "trace_bound": trace_available,
                                "actor_consciousness_instance_id": (
                                    recall_identity.actor_consciousness_instance_id
                                    if trace_available and recall_identity is not None
                                    else ""
                                ),
                                "source_occurrence_id": (
                                    recall_identity.source_occurrence_id
                                    if trace_available and recall_identity is not None
                                    else ""
                                ),
                                "trace_stream_scope": trace_stream_scope,
                                "recorded_at": recorded_at,
                            }
                        )
                    candidate_delivery_binding = self._recall_delivery_binding(
                        delivery_id=recall_delivery_id,
                        recall_chain_id=chain_id,
                        episode_id=episode_id,
                        page_offset=offset,
                        delivered_refs=self._delivered_entity_refs(candidate_items),
                    )
                    candidate_payload = self._projection_payload(
                        query=normalized_query,
                        mode=mode,
                        stream_scope=stream_scope,
                        valid_at=valid_at,
                        recorded_as_of=recorded_as_of,
                        episode=projection_episode,
                        trace_available=trace_available,
                        recall_delivery_binding=candidate_delivery_binding,
                        items=candidate_items,
                        total_evidence=len(evidence_results),
                        budget=budget,
                        frontier_sha256=frontier_sha256,
                        original_items=len(records),
                        original_bytes=original_bytes,
                        omitted_items=len(records) - candidate_offset,
                        truncated=(
                            candidate_offset < len(records)
                            or any(
                                str(item["delivery"]) != "full"
                                for item in candidate_items
                            )
                        ),
                        continuation=candidate_continuation,
                    )
                    candidate_payload = self._finalize_projection_bytes(
                        candidate_payload,
                        original_bytes=original_bytes,
                    )
                    if int(candidate_payload["delivered_bytes"]) <= budget:
                        accepted = variant, candidate_payload
                        break
                if accepted is None:
                    break
                selected_items.append(accepted[0])
                final_payload = accepted[1]

            if final_payload is None:
                if records and offset < len(records):
                    return False, {"error": "memory search projection budget cannot fit one ref"}
                final_payload = self._projection_payload(
                    query=normalized_query,
                    mode=mode,
                    stream_scope=stream_scope,
                    valid_at=valid_at,
                    recorded_as_of=recorded_as_of,
                    episode=projection_episode,
                    trace_available=False,
                    recall_delivery_binding=None,
                    items=[],
                    total_evidence=len(evidence_results),
                    budget=budget,
                    frontier_sha256=frontier_sha256,
                    original_items=len(records),
                    original_bytes=original_bytes,
                    omitted_items=0,
                    truncated=False,
                    continuation="",
                )
                final_payload = self._finalize_projection_bytes(
                    final_payload,
                    original_bytes=original_bytes,
                )

            delivered_evidence: dict[str, list[dict[str, Any]]] = {}
            for item in final_payload["evidence_results"]:
                delivered_evidence.setdefault(str(item["entity_ref"]), []).append(item)
            if _memory_search_tool_result_bytes(final_payload) > budget:
                return False, {"error": "memory search projection exceeded hard budget"}

            delivery_binding = final_payload.get("recall_delivery_binding")
            if (
                trace_available
                and recall_identity is not None
                and isinstance(delivery_binding, dict)
            ):
                delivered_refs: list[DeliveredMemorySearchRef] = []
                seen_refs: set[str] = set()
                for evidence_item in evidence_results:
                    entity_ref = _evidence_entity_ref(evidence_item)
                    projections = delivered_evidence.get(entity_ref)
                    if not projections or entity_ref in seen_refs:
                        continue
                    projection = projections[0]
                    exact_versions = []
                    for visible in projections:
                        exact = _exact_document_fields(visible)
                        if exact and exact not in exact_versions:
                            exact_versions.append(exact)
                    exact_metadata = exact_versions[0] if len(exact_versions) == 1 else {}
                    if len(exact_versions) > 1:
                        exact_metadata = {"subject_file_versions": exact_versions}
                    seen_refs.add(entity_ref)
                    delivered_refs.append(
                        DeliveredMemorySearchRef(
                            entity_ref=entity_ref,
                            source=str(evidence_item.source or "memory_search"),
                            ordinal=len(delivered_refs),
                            metadata={
                                **exact_metadata,
                                "rank_score": float(evidence_item.rank_score),
                                "rank_is_not_truth": True,
                                "content_delivery": str(
                                    projection.get("content_delivery") or "ref"
                                ),
                                "content_ref": str(
                                    projection.get("content_ref") or ""
                                ),
                            },
                        )
                    )
                if delivered_refs:
                    get_memory_search_recall_delivery_coordinator().register(
                        PendingMemorySearchRecall(
                            delivery_id=str(delivery_binding["delivery_id"]),
                            recall_chain_id=chain_id,
                            episode_id=episode_id,
                            consciousness_instance_id=(
                                recall_identity.actor_consciousness_instance_id
                            ),
                            stream_scope=recall_identity.stream_scope,
                            source_occurrence_id=(
                                recall_identity.source_occurrence_id
                            ),
                            recorded_at=recorded_at,
                            query=normalized_query,
                            retrieval_intent=mode,
                            context_key=context_key,
                            random_seed=retrieval_seed,
                            frontier_sha256=frontier_sha256,
                            page_offset=offset,
                            delivered_refs=tuple(delivered_refs),
                            search_context=dict(recall_binding),
                            recall=service,
                        )
                    )
            return True, final_payload

        except Exception as e:
            logger.error(f"记忆搜索失败: {e}", exc_info=True)
            return False, {"error": f"搜索失败: {e}"}


# ============================================================
# nucleus_memory_stats - 记忆系统统计
# ============================================================


class LifeEngineMemoryStatsTool(BaseTool):
    """读取统一记忆健康快照，不把任一投影冒充全部记忆。"""

    tool_name: str = "nucleus_memory_stats"
    tool_description: str = (
        "读取统一、content-free 的记忆健康快照，包括权威后端、索引、Experience、"
        "Witness、连续性与积压诊断。legacy graph 若存在只会作为明确标注的兼容投影；"
        "健康状态和检索分数都不判断记忆的真值或重要性。"
    )
    chatter_allow: list[str] = ["life_engine_internal", "life_chatter"]

    def __init__(self, plugin):
        super().__init__(plugin)

    async def _get_service(self) -> LifeMemoryService:
        """获取记忆服务实例。"""
        from ..service import LifeEngineService

        service = LifeEngineService.get_instance()
        memory_service = None if service is None else service.memory_service
        if memory_service is None:
            raise RuntimeError("记忆服务未初始化")
        return memory_service

    async def execute(self) -> tuple[bool, dict[str, Any]]:
        """获取统一只读健康快照。"""
        try:
            service = await self._get_service()
            snapshot = await service.health_snapshot()

            return True, {
                "action": "memory_stats",
                "projection_kind": "memory_health_snapshot",
                "authority": False,
                "read_only": True,
                "health": snapshot,
            }

        except Exception as e:
            logger.error(f"获取统计失败: {e}", exc_info=True)
            return False, {"error": f"获取统计失败: {e}"}


# ============================================================
# 工具注册列表
# ============================================================


_RELATION_VIEW_PROJECTION = "semantic-relation-view-v1"
_RELATION_VIEW_MIN_BYTES = 2048
_RELATION_VIEW_MAX_BYTES = 64 * 1024


def _relation_view_cursor(state: dict[str, Any]) -> str:
    body = base64.urlsafe_b64encode(_canonical_json_bytes(state)).decode("ascii").rstrip("=")
    return "rv1." + body + "." + hashlib.sha256(body.encode("ascii")).hexdigest()[:16]


def _relation_view_authority(service: Any) -> str:
    """Hash source-generation identity plus a non-durable service read lifetime.

    The nonce is only a bounded projection-cache lifecycle marker: it is not a
    subject identity, credential, writer lease or durable authority record.
    """
    runtime = getattr(service, "storage_runtime", None)
    source = runtime if runtime is not None else getattr(service, "_memory_storage", None)
    source = service if source is None else source
    cached = getattr(service, "_relation_view_projection_identity", None)
    if cached is None or cached[0] is not source:
        cached = (source, uuid4().hex)
        setattr(service, "_relation_view_projection_identity", cached)
    material: dict[str, Any] = {"service_read_lifetime": cached[1]}
    if runtime is not None:
        generation = getattr(runtime, "generation", None)
        token = getattr(runtime, "authority_token", None)
        material.update({
            "backend": str(getattr(runtime, "backend", "")),
            "backend_identity": str(getattr(runtime, "backend_identity", "")),
            "generation_id": str(getattr(generation, "generation_id", "")),
            "registry_id": str(getattr(token, "registry_id", "")),
            "authority_epoch": getattr(token, "authority_epoch", None),
            "writer_epoch": getattr(runtime, "writer_epoch", None),
        })
    return hashlib.sha256(_canonical_json_bytes(material)).hexdigest()


def _decode_relation_view_cursor(continuation: str) -> dict[str, Any]:
    """Decode only a small, integrity-checked continuation envelope."""
    try:
        if not isinstance(continuation, str) or len(continuation) > 4096:
            raise ValueError("invalid cursor size")
        prefix, encoded, checksum = continuation.split(".")
        if (
            prefix != "rv1"
            or hashlib.sha256(encoded.encode("ascii")).hexdigest()[:16] != checksum
        ):
            raise ValueError("invalid cursor signature")
        state = json.loads(base64.urlsafe_b64decode(
            encoded + "=" * (-len(encoded) % 4)
        ).decode("utf-8"))
        if not isinstance(state, dict) or state["projection"] != _RELATION_VIEW_PROJECTION:
            raise ValueError("invalid cursor version")
        return state
    except (KeyError, TypeError, ValueError, UnicodeError) as exc:
        raise ValueError("SemanticRelationContinuationInvalid") from exc


def _relation_view_request_page(
    continuation: str, *, entity_ref: str, current_only: bool,
    authority_binding: str, request_binding: str,
) -> tuple[int, int | None, int]:
    """Recover the exact storage offset/frontier before requesting one page."""
    if not continuation:
        return 0, None, int(datetime.now(UTC).timestamp())
    state = _decode_relation_view_cursor(continuation)
    try:
        if state["entity_ref"] != entity_ref or state["view"] != (
            "current" if current_only else "history"
        ):
            raise ValueError("cursor selector mismatch")
        offset, frontier = state["row_offset"], state["frontier_count"]
        issued_at = state["issued_at"]
        if (
            type(offset) is not int or offset < 0
            or type(frontier) is not int or frontier < offset
            or state["row_limit"] != 50
            or type(issued_at) is not int
        ):
            raise ValueError("invalid storage cursor")
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("SemanticRelationContinuationInvalid") from exc
    if state.get("authority_binding") != authority_binding:
        raise RuntimeError("SemanticRelationContinuationAuthorityChanged")
    if state.get("request_binding") != request_binding:
        raise ValueError("SemanticRelationContinuationBindingMismatch")
    age = int(datetime.now(UTC).timestamp()) - issued_at
    if age < 0 or age > 30 * 60:
        raise RuntimeError("SemanticRelationContinuationExpired")
    return offset, frontier, issued_at


def _bounded_relation_view(
    payload: dict[str, Any], *, max_bytes: int, continuation: str,
) -> dict[str, Any]:
    """Bound each storage page and its exact canonical JSON byte continuation.

    Concatenate excerpt content until page_complete to restore one full storage
    page. The next continuation then advances its bounded row offset. Every
    cursor pins the global append frontier; intra-page cursors also pin bytes.
    """
    if type(max_bytes) is not int or not (
        _RELATION_VIEW_MIN_BYTES <= max_bytes <= _RELATION_VIEW_MAX_BYTES
    ):
        raise ValueError("SemanticRelationViewByteBudgetInvalid")
    raw = _canonical_json_bytes(payload)
    digest = hashlib.sha256(raw).hexdigest()
    storage_page = payload.get("storage_page")
    offset = 0
    if continuation:
        state = _decode_relation_view_cursor(continuation)
        try:
            if state["entity_ref"] != payload["entity_ref"] or state["view"] != payload["view"]:
                raise ValueError("cursor selector mismatch")
            offset, cursor_digest = state["offset_bytes"], state["sha256"]
            if type(offset) is not int or offset < 0 or not isinstance(cursor_digest, str):
                raise ValueError("invalid cursor offset")
            if cursor_digest == "":
                if storage_page is None or offset != 0:
                    raise ValueError("missing byte snapshot")
            elif len(cursor_digest) != 64:
                raise ValueError("invalid cursor hash")
            if storage_page is not None and (
                state["row_offset"] != storage_page["offset"]
                or state["frontier_count"] != storage_page["frontier_count"]
                or state["row_limit"] != storage_page["limit"]
                or state["authority_binding"] != storage_page["authority_binding"]
                or state["request_binding"] != storage_page["request_binding"]
                or state["issued_at"] != storage_page["issued_at"]
            ):
                raise ValueError("storage cursor mismatch")
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("SemanticRelationContinuationInvalid") from exc
        if cursor_digest and cursor_digest != digest:
            raise RuntimeError("SemanticRelationViewSnapshotChanged")
        if offset >= len(raw):
            raise ValueError("SemanticRelationContinuationInvalid")

    def next_cursor(next_offset: int) -> str:
        row_offset = storage_page["offset"] if storage_page else None
        snapshot_digest = digest
        if next_offset == len(raw):
            if storage_page is None or not storage_page["has_more"]:
                return ""
            row_offset = storage_page["next_offset"]
            next_offset, snapshot_digest = 0, ""
        state = {
            "projection": _RELATION_VIEW_PROJECTION,
            "entity_ref": payload["entity_ref"], "view": payload["view"],
            "sha256": snapshot_digest, "offset_bytes": next_offset,
        }
        if storage_page is not None:
            state.update({
                "row_offset": row_offset,
                "row_limit": storage_page["limit"],
                "frontier_count": storage_page["frontier_count"],
                "authority_binding": storage_page["authority_binding"],
                "request_binding": storage_page["request_binding"],
                "issued_at": storage_page["issued_at"],
            })
        return _relation_view_cursor(state)

    common = {
        "projection_version": _RELATION_VIEW_PROJECTION,
        "content_ref": "semantic-relation-view:" + digest,
        "content_sha256": digest,
        "content_bytes": len(raw),
        "max_bytes": max_bytes,
    }
    completion_cursor = next_cursor(len(raw))
    structured = {
        **payload, **common, "complete": not completion_cursor,
        "page_complete": True, "continuation": completion_cursor,
    }
    if offset == 0 and len(_canonical_json_bytes(structured)) <= max_bytes:
        return structured
    try:
        remainder = raw[offset:].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("SemanticRelationContinuationInvalid") from exc

    def page(character_count: int) -> dict[str, Any]:
        content = remainder[:character_count]
        next_offset = offset + len(content.encode("utf-8"))
        token = next_cursor(next_offset)
        result = {
            "action": "view", "entity_ref": payload["entity_ref"],
            "authority": "memory_semantic_relations", "view": payload["view"],
            **common,
            "serialization": "canonical-json-utf8",
            "projection_kind": "exact_serialized_view_excerpt",
            "complete": not token,
            "page_complete": next_offset == len(raw),
            "offset_bytes": offset, "next_offset_bytes": next_offset,
            "content": content, "continuation": token,
        }
        if storage_page is not None:
            result["storage_page"] = storage_page
        return result

    low, high = 0, len(remainder)
    while low < high:
        middle = (low + high + 1) // 2
        if len(_canonical_json_bytes(page(middle))) <= max_bytes:
            low = middle
        else:
            high = middle - 1
    if low == 0:
        raise ValueError("SemanticRelationViewBudgetCannotFitIdentity")
    return page(low)

_RELATION_PUBLIC_ERRORS = frozenset(
    "SemanticRelation" + suffix for suffix in (
        "ActorIdentityRequired", "ActorIsNotActive", "AddMustStartNewRoot",
        "AlreadyWithdrawn", "ContinuationAuthorityChanged",
        "ContinuationBindingMismatch", "ContinuationExpired", "ContinuationInvalid",
        "EndpointRequired", "EndpointsImmutable", "EndpointsMustDiffer",
        "EndpointsRequired", "ExplicitRootAndParentRequired", "IdentityRequired",
        "LegacyOwnerUnbound", "LineageMismatch", "OccurrenceConflict",
        "OperationInvalid", "OwnerMismatch", "OwnerRequired",
        "PageFrontierConflict", "PageFrontierInvalid", "PageFrontierRequired",
        "PageLimitInvalid", "PageOffsetInvalid", "ParentNotFound", "PathInvalid",
        "PathIneligible", "PredicateRequired", "ReasonRequired", "ReferencePathConflict",
        "RevisionIdentityRequired", "RevisionInvalid", "SchemaMissing",
        "SourceOccurrenceRequired", "StaleParent", "StreamOwnerRequired",
        "StrengthMustBeSubjectText", "ToolCallIdentityRequired",
        "ViewBudgetCannotFitIdentity", "ViewByteBudgetInvalid", "ViewSnapshotChanged",
        "WithdrawalPredicateMismatch",
    )
) | frozenset({
    "LifeEngineServiceUnavailable", "LifeMemoryServiceUnavailable",
    "SelectedSubjectStorageNotStarted", "ManagedFileDocumentNotFound",
    "ManagedFileReferenceInvalid", "ManagedFileReferenceDocumentConflict",
    "ManagedFileReferenceOutsideWorkspace", "ManagedFileReferencePathAlias",
    "ManagedFileReferencePathInvalid", "ManagedFileReferenceVersionConflict",
    "ManagedFileVersionDocumentConflict", "ManagedFilePathMustNameAFile",
})


def _relation_error_payload(exc: Exception) -> dict[str, Any]:
    """Expose protocol codes, never third-party SQL/paths/subject parameters."""
    message = exc.args[0] if exc.args and isinstance(exc.args[0], str) else ""
    code = message if message in _RELATION_PUBLIC_ERRORS else "SemanticRelationOperationFailed"
    if type(exc).__name__ == "SubjectDocumentNotFound":
        code = "ManagedFileReferenceNotFound"
    return {
        "error": code,
        "error_type": type(exc).__name__[:80],
        "status": "failed",
    }


def _bounded_relation_mutation_receipt(payload: dict[str, Any]) -> dict[str, Any]:
    """Keep full subject text in history while bounding an append acknowledgement."""
    limit = 16 * 1024
    if len(_canonical_json_bytes(payload)) <= limit:
        return payload
    read_from: dict[str, Any] = {
        "action": "view", "current_only": False, "max_bytes": limit,
    }
    if payload["source_ref"].startswith("document:"):
        read_from["file_path"] = payload["source_path"]
    else:
        read_from["entity_ref"] = payload["source_ref"]
    receipt = {
        key: payload[key] for key in (
            "action", "relation_id", "root_relation_id", "parent_relation_id",
            "revision", "operation", "owner_subject_id", "actor",
            "source_ref", "target_ref", "recorded_at", "source_occurrence_id",
            "idempotent_replay", "authority", "legacy_edge_written",
        )
    }
    receipt.update({
        "projection_kind": "bounded_relation_append_receipt",
        "receipt_only": True,
        "relation_ref": "memory-semantic-relation:" + payload["relation_id"],
        "subject_content_preserved_in_history": True,
        "read_from": read_from,
        "read_until_relation_id": payload["relation_id"],
    })
    if len(_canonical_json_bytes(receipt)) > limit:
        # Very long historical identifiers cannot enlarge a receipt indefinitely.
        # The caller still retains its exact request selectors for view.
        receipt = {
            key: receipt[key] for key in (
                "action", "relation_id", "projection_kind", "receipt_only",
                "relation_ref", "subject_content_preserved_in_history",
                "idempotent_replay", "authority",
            )
        }
        receipt["read_instruction"] = "Use view with the request's original source selector."
    return receipt

class NucleusRelationsTool(BaseTool):
    """Append and inspect subject-owned, immutable relation revisions."""

    tool_name: str = "nucleus_relations"
    tool_description: str = (
        "管理显式记忆关系。add 新建；revise/withdraw 追加修订/撤回，必须给出"
        " root_relation_id 与明确的 parent_relation_id，不覆盖历史或自动换父。"
        "source_ref/target_ref/entity_ref 接受 subject-file:doc_id 或精确版本"
        " subject-file:doc_id@ver_id；旧路径参数仅用于旧路径实体，不绑定当前同路径文件。"
        "view 区分完整历史与当前有效头。关系类型、理由和 subject_strength 均保留主体原话。"
        "修订省略 subject_strength 保留此前原话，显式空字符串清空。"
        "view 受 max_bytes 硬预算约束；大结果以 canonical JSON 片段给出 continuation，"
        "同一 storage_page 的 content 拼到 page_complete 后恢复完整该页 JSON，"
        "continuation 再续下一页，complete 表示全部读完。任意新增关系使旧接续过期。"
        "游标仅在同一授权实例/服务及存储世代内有效，30 分钟过期；重启后重新查看。"
        "大写入回执只返回关系身份与 view 续读入口，主体原文仍完整保存。"
    )
    chatter_allow: list[str] = ["life_engine_internal", "life_chatter"]

    async def _get_service(self) -> LifeMemoryService:
        """Resolve the canonical memory service for read-only projection."""
        from ..service import LifeEngineService

        service = LifeEngineService.get_instance()
        memory_service = None if service is None else service.memory_service
        if memory_service is None:
            raise RuntimeError("记忆服务未初始化")
        return memory_service

    async def _execute_mutation(
        self,
        *,
        action: str,
        source_ref: str,
        target_ref: str,
        source_path: str,
        target_path: str,
        relation_type: str,
        reason: str,
        root_relation_id: str,
        parent_relation_id: str,
        subject_strength: str | None,
    ) -> tuple[bool, dict[str, Any]]:
        """Append one explicit occurrence; the store owns atomic parent CAS."""
        if not isinstance(reason, str) or not reason.strip():
            return False, {"error": "reason 不能为空"}
        if action != "withdraw" and (
            not isinstance(relation_type, str) or not relation_type.strip()
        ):
            return False, {"error": "relation_type 不能为空"}
        if subject_strength is not None and not isinstance(subject_strength, str):
            return False, {"error": "SemanticRelationStrengthMustBeSubjectText"}

        try:
            runtime = await _resolve_relation_runtime(self)
            from .living import SemanticRelation

            relation_id = _stable_relation_id(runtime)
            parent = None
            if action == "add":
                if root_relation_id or parent_relation_id:
                    raise ValueError("SemanticRelationAddMustStartNewRoot")
                source_ref, source_path = await _resolve_relation_endpoint(
                    self, entity_ref=source_ref, file_path=source_path,
                    life_service=runtime.life_service,
                )
                target_ref, target_path = await _resolve_relation_endpoint(
                    self, entity_ref=target_ref, file_path=target_path,
                    life_service=runtime.life_service,
                )
                if source_ref == target_ref:
                    raise ValueError("SemanticRelationEndpointsMustDiffer")
                root_relation_id = relation_id
                revision = 1
            else:
                if not root_relation_id or not parent_relation_id:
                    raise ValueError("SemanticRelationExplicitRootAndParentRequired")
                parent = await runtime.memory_service.get_memory_semantic_relation(
                    parent_relation_id
                )
                if parent is None:
                    raise RuntimeError("SemanticRelationParentNotFound")
                if parent.owner_subject_id is None:
                    raise PermissionError("SemanticRelationLegacyOwnerUnbound")
                if parent.owner_subject_id != runtime.owner_subject_id:
                    raise PermissionError("SemanticRelationOwnerMismatch")
                if parent.root_relation_id != root_relation_id:
                    raise ValueError("SemanticRelationLineageMismatch")
                if parent.operation == "withdraw":
                    raise RuntimeError("SemanticRelationAlreadyWithdrawn")
                for supplied_ref, supplied_path, stored_ref in (
                    (source_ref, source_path, parent.source_ref),
                    (target_ref, target_path, parent.target_ref),
                ):
                    if supplied_ref or supplied_path:
                        resolved, _ = await _resolve_relation_endpoint(
                            self, entity_ref=supplied_ref, file_path=supplied_path,
                            life_service=runtime.life_service,
                        )
                        if resolved != stored_ref:
                            raise ValueError("SemanticRelationEndpointsImmutable")
                source_ref, source_path = await _authorize_stored_relation_endpoint(
                    self, parent.source_ref, life_service=runtime.life_service
                )
                target_ref, target_path = await _authorize_stored_relation_endpoint(
                    self, parent.target_ref, life_service=runtime.life_service
                )
                revision = parent.revision + 1
                if action == "withdraw":
                    if relation_type and relation_type != parent.predicate:
                        raise ValueError("SemanticRelationWithdrawalPredicateMismatch")
                    relation_type = parent.predicate

            metadata: dict[str, Any] = {
                "protocol": "subject_relation_revision_v1",
                "source_occurrence_id": runtime.source_occurrence_id,
                "source_occurrence_kind": runtime.source_occurrence_kind,
                "tool_call_id": runtime.tool_call_id,
            }
            if subject_strength is not None:
                metadata["subject_strength"] = subject_strength
            elif parent is not None and "subject_strength" in parent.metadata:
                metadata["subject_strength"] = parent.metadata["subject_strength"]

            proposed = SemanticRelation(
                relation_id=relation_id,
                source_ref=source_ref,
                target_ref=target_ref,
                predicate=relation_type,
                reason=reason,
                actor=runtime.actor_consciousness_instance_id,
                recorded_at=_relation_recorded_at(self),
                consciousness_instance_id=runtime.actor_consciousness_instance_id,
                stream_scope=runtime.stream_scope,
                metadata=metadata,
                owner_subject_id=runtime.owner_subject_id,
                root_relation_id=root_relation_id,
                parent_relation_id=parent_relation_id or None,
                revision=revision,
                operation=action,
            )
            existing = await runtime.memory_service.get_memory_semantic_relation(
                relation_id
            )
            idempotent_replay = existing is not None
            if existing is not None:
                if not _same_semantic_relation(existing, proposed):
                    raise RuntimeError("SemanticRelationOccurrenceConflict")
                semantic_relation = existing
            else:
                try:
                    semantic_relation = (
                        await runtime.memory_service.record_memory_semantic_relation(
                            proposed
                        )
                    )
                except Exception:
                    # An uncertain acknowledgement may follow a committed append.
                    # Recover only this same occurrence, never choose a newer parent.
                    existing = await runtime.memory_service.get_memory_semantic_relation(
                        relation_id
                    )
                    if existing is None or not _same_semantic_relation(existing, proposed):
                        raise
                    semantic_relation = existing
                    idempotent_replay = True

            logger.info(
                "SemanticRelation appended: "
                f"relation_id={semantic_relation.relation_id} "
                f"actor={runtime.actor_consciousness_instance_id}"
            )
            payload = {
                **_semantic_relation_payload(semantic_relation, center_ref=source_ref),
                "action": action,
                "source_path": source_path,
                "target_path": target_path,
                "relation_type": semantic_relation.predicate,
                "source_occurrence_id": runtime.source_occurrence_id,
                "authority": "memory_semantic_relations",
                "legacy_edge_written": False,
                "idempotent_replay": idempotent_replay,
            }
            return True, _bounded_relation_mutation_receipt(payload)
        except Exception as exc:
            logger.error(f"关系操作失败: {type(exc).__name__}")
            return False, _relation_error_payload(exc)

    async def _execute_view(
        self,
        *,
        entity_ref: str,
        file_path: str,
        current_only: bool,
        depth: int,
        min_strength: float,
        max_bytes: int,
        continuation: str,
    ) -> tuple[bool, dict[str, Any]]:
        """Read one bounded, snapshot-consistent relation page plus exact bytes."""
        try:
            if type(max_bytes) is not int or not (
                _RELATION_VIEW_MIN_BYTES <= max_bytes <= _RELATION_VIEW_MAX_BYTES
            ):
                raise ValueError("SemanticRelationViewByteBudgetInvalid")
            runtime = await _resolve_relation_runtime(self)
            service = runtime.memory_service
            authority_binding = _relation_view_authority(service)
            center_ref, file_path = await _resolve_relation_endpoint(
                self, entity_ref=entity_ref, file_path=file_path,
                life_service=runtime.life_service,
            )
            depth = max(1, min(3, depth))
            min_strength = max(0.0, min(1.0, min_strength))
            request_binding = hashlib.sha256(_canonical_json_bytes({
                "entity_ref": center_ref, "file_path": file_path,
                "current_only": current_only, "max_bytes": max_bytes,
                "depth": depth, "min_strength": min_strength,
                "actor": runtime.actor_consciousness_instance_id,
                "stream_scope": runtime.stream_scope,
            })).hexdigest()
            offset, expected_frontier, issued_at = _relation_view_request_page(
                continuation, entity_ref=center_ref, current_only=current_only,
                authority_binding=authority_binding, request_binding=request_binding,
            )
            relation_page = await service.page_memory_semantic_relations(
                center_ref, current_only=current_only, limit=50,
                offset=offset, expected_frontier_count=expected_frontier,
            )
            if authority_binding != _relation_view_authority(service):
                raise RuntimeError("SemanticRelationContinuationAuthorityChanged")
            current_ids = set(relation_page.current_relation_ids)
            semantic_payloads = [
                {
                    **_semantic_relation_payload(relation, center_ref=center_ref),
                    "is_current": relation.relation_id in current_ids,
                }
                for relation in relation_page.relations
            ]
            depth = max(1, min(3, depth))
            min_strength = max(0.0, min(1.0, min_strength))
            legacy_projection: dict[str, Any] = {
                "projection_kind": "legacy_memory_edges_compatibility",
                "authoritative": False,
                "read_only": True,
                "automatic_promotion_to_semantic_history": False,
                "strength_is_truth": False,
                "depth": depth,
                "min_strength": min_strength,
            }
            if not center_ref.startswith("document:") or offset > 0:
                legacy_projection.update({
                    "available": False,
                    "reason": (
                        "StableIdentityDoesNotAdoptLegacyPathRelations"
                        if not center_ref.startswith("document:") else "FirstStoragePageOnly"
                    ),
                    "center": None, "outgoing": [], "incoming": [],
                })
            else:
                try:
                    legacy_relations = await service.get_file_relations(
                        file_path=file_path, depth=depth, min_strength=min_strength,
                    )
                    if "error" in legacy_relations:
                        legacy_projection.update({
                            "available": False,
                            "error": "LegacyRelationProjectionUnavailable",
                            "center": None, "outgoing": [], "incoming": [],
                        })
                    else:
                        legacy_projection.update({
                            "available": True,
                            "center": legacy_relations.get("center"),
                            "outgoing": list(legacy_relations.get("outgoing") or []),
                            "incoming": list(legacy_relations.get("incoming") or []),
                        })
                except Exception as legacy_exc:
                    legacy_projection.update({
                        "available": False,
                        "error_type": type(legacy_exc).__name__,
                        "center": None, "outgoing": [], "incoming": [],
                    })

            # The append frontier counts ALL relations, not this endpoint's rows.
            matching_count = relation_page.matching_count
            stable_relation_hint: dict[str, Any] | None = None
            if (
                not semantic_payloads
                and center_ref.startswith("subject-file:")
                and "@" in center_ref
            ):
                # Exact-version references intentionally remain exact and must not
                # silently broaden to a document head.  A bounded, read-only hint
                # lets the subject choose relations anchored directly to the
                # stable document endpoint, not relations on sibling versions.
                stable_ref = center_ref.split("@", 1)[0]
                stable_page = await service.page_memory_semantic_relations(
                    stable_ref,
                    current_only=current_only,
                    limit=1,
                    offset=0,
                    expected_frontier_count=relation_page.frontier_count,
                )
                if authority_binding != _relation_view_authority(service):
                    raise RuntimeError("SemanticRelationContinuationAuthorityChanged")
                if stable_page.matching_count:
                    stable_relation_hint = {
                        "entity_ref": stable_ref,
                        "matching_relation_count": stable_page.matching_count,
                        "reason": (
                            "ExactVersionHasNoAnchoredRelations; "
                            "stable document endpoint is available by explicit choice"
                        ),
                    }
            payload = {
                "action": "view",
                "entity_ref": center_ref,
                "file_path": file_path,
                "authority": "memory_semantic_relations",
                "view": "current" if current_only else "history",
                "current_only": current_only,
                "semantic_relation_count": len(semantic_payloads),
                "semantic_relations": semantic_payloads,
                "matching_relation_count": matching_count,
                "history_relation_count": matching_count if not current_only else None,
                "current_relation_count": (
                    matching_count if current_only else (
                        len(current_ids) if offset == 0 and not relation_page.has_more else None
                    )
                ),
                "current_relation_ids": list(relation_page.current_relation_ids),
                "current_relation_ids_scope": "this_storage_page",
                "stable_relation_hint": stable_relation_hint,
                "storage_page": {
                    "offset": relation_page.offset,
                    "limit": 50,
                    "next_offset": relation_page.next_offset,
                    "has_more": relation_page.has_more,
                    "frontier_count": relation_page.frontier_count,
                    "frontier_scope": "all_immutable_relation_appends",
                    "authority_binding": authority_binding,
                    "request_binding": request_binding,
                    "issued_at": issued_at,
                    "continuation_lifetime": "same_service_and_authority_30_minutes",
                },
                "legacy_compatibility_projection": legacy_projection,
            }
            return True, _bounded_relation_view(
                payload, max_bytes=max_bytes, continuation=continuation,
            )
        except Exception as exc:
            logger.error(f"查看关联失败: {type(exc).__name__}")
            return False, _relation_error_payload(exc)

    async def execute(
        self,
        action: Annotated[
            Literal["add", "revise", "withdraw", "view"],
            "add 新建；revise 修订；withdraw 追加撤回；view 查看",
        ] = "view",
        source_path: Annotated[str, "旧路径实体的源路径，不自动绑定受管文件"] = "",
        target_path: Annotated[str, "旧路径实体的目标路径，不自动绑定受管文件"] = "",
        relation_type: Annotated[str, "add/revise 的开放关系原话"] = "",
        reason: Annotated[str, "add/revise/withdraw 时主体明确写下的理由"] = "",
        file_path: Annotated[str, "view 的旧路径实体，与 entity_ref 互斥"] = "",
        depth: Annotated[int, "view 的 legacy compatibility 深度 1-3"] = 1,
        min_strength: Annotated[float, "仅用于 legacy compatibility 可达性"] = 0.2,
        source_ref: Annotated[str, "源 subject-file:doc_id 或 doc_id@ver_id 引用"] = "",
        target_ref: Annotated[str, "目标 subject-file:doc_id 或 doc_id@ver_id 引用"] = "",
        entity_ref: Annotated[str, "view 的 subject-file 稳定实体或精确版本引用"] = "",
        root_relation_id: Annotated[str, "revise/withdraw 的明确关系根 ID"] = "",
        parent_relation_id: Annotated[str, "revise/withdraw 预期的当前父记录 ID；冲突不自动换父"] = "",
        subject_strength: Annotated[Optional[str], "可选关系强弱原话；非数值排名或真值"] = None,
        current_only: Annotated[bool, "view 仅返回当前未撤回头；默认展示全部历史"] = False,
        max_bytes: Annotated[int, "view JSON 返回硬预算，2048-65536 字节"] = 16384,
        continuation: Annotated[str, "view 精确接续游标，须保持相同 entity/path 与 view 参数"] = "",
    ) -> tuple[bool, str | dict[str, Any]]:
        action_value = str(action or "view").strip().lower()
        if action_value in {"forget", "remove", "delete", "weaken"}:
            return False, _legacy_relation_mutation_retired_payload(action_value)
        if action_value not in {"add", "revise", "withdraw", "view"}:
            return False, {
                "error": "UnsupportedRelationAction",
                "action": action_value,
                "allowed_actions": ["add", "revise", "withdraw", "view"],
            }
        if action_value == "view":
            return await self._execute_view(
                entity_ref=entity_ref, file_path=file_path, current_only=current_only,
                depth=depth, min_strength=min_strength,
                max_bytes=max_bytes, continuation=continuation,
            )
        return await self._execute_mutation(
            action=action_value, source_ref=source_ref, target_ref=target_ref,
            source_path=source_path, target_path=target_path,
            relation_type=relation_type, reason=reason,
            root_relation_id=root_relation_id, parent_relation_id=parent_relation_id,
            subject_strength=subject_strength,
        )


MEMORY_TOOLS = [
    LifeEngineSearchMemoryTool,
    NucleusRelationsTool,
    LifeEngineMemoryStatsTool,
]
