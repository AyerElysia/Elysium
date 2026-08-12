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
from datetime import UTC, datetime, timezone
from types import SimpleNamespace
from typing import Annotated, Any, List, Literal, Optional
from uuid import uuid4

from src.app.plugin_system.api import log_api
from src.app.plugin_system.base import BaseTool

from .eligibility import assess_document_path
from .lineage import MemoryBundle
from .service import LifeMemoryService

logger = log_api.get_logger("life_engine.memory_tools")

MEMORY_SEARCH_PROJECTION_VERSION = "memory-search-projection-v1"
MEMORY_SEARCH_CORE_MAX_BYTES = 16 * 1024
MEMORY_SEARCH_EXPRESSION_MAX_BYTES = 64 * 1024
MEMORY_SEARCH_MAX_ITEM_EXCERPT_BYTES = 2 * 1024
LEGACY_RELATION_MUTATION_RETIRED = "LegacyRelationMutationRetired"


@dataclass(frozen=True, slots=True)
class _RelationRuntime:
    """Trusted runtime identity for one explicit relation operation."""

    memory_service: LifeMemoryService
    actor_consciousness_instance_id: str
    stream_scope: str
    source_occurrence_id: str
    source_occurrence_kind: str
    tool_call_id: str


def _relation_source_occurrence(tool: BaseTool) -> tuple[str, str]:
    """Return the exact source occurrence already bound to this tool call."""

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


async def _resolve_relation_runtime(tool: BaseTool) -> _RelationRuntime:
    """Resolve an active consciousness actor from the current runtime stream."""

    from ..service.registry import get_life_engine_service

    service = get_life_engine_service()
    if service is None:
        raise RuntimeError("LifeEngineServiceUnavailable")
    memory_service = getattr(service, "_memory_service", None)
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
    }


def _legacy_relation_mutation_retired_payload(action: str) -> dict[str, Any]:
    """Return the stable fail-closed contract for retired graph mutations."""

    return {
        "error": LEGACY_RELATION_MUTATION_RETIRED,
        "error_type": LEGACY_RELATION_MUTATION_RETIRED,
        "action": str(action or "").strip().lower(),
        "mutated": False,
        "message": (
            "SemanticRelation history has no audited retract/supersede contract; "
            "legacy memory_edges are read-only compatibility data."
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


def _bundle_to_payload(bundle: MemoryBundle) -> dict[str, Any]:
    """将可追溯记忆包压成工具返回结构。"""
    return {
        "primary_path": bundle.primary_path,
        "current_understanding": bundle.current_understanding,
        "evidence_files": [
            {
                "file_path": item.file_path,
                "title": item.title,
                "snippet": item.snippet,
                "relevance": round(item.relevance, 3),
                "source": item.source,
                "relation": item.relation,
                "relation_reason": item.relation_reason,
                "exists": item.exists,
            }
            for item in bundle.evidence
        ],
        "history_trace": [
            {
                "direction": item.direction,
                "relation": item.relation,
                "file_path": item.file_path,
                "title": item.title,
                "snippet": item.snippet,
                "reason": item.reason,
                "exists": item.exists,
            }
            for item in bundle.history_trace
        ],
        "corrections": [
            {
                "topic": item.topic,
                "message": item.message,
                "source": item.source,
                "created_at": item.created_at,
            }
            for item in bundle.corrections
        ],
        "uncertainty": bundle.uncertainty,
    }


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
        if service is None or service._memory_service is None:
            raise RuntimeError("记忆服务未初始化")
        return service._memory_service

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
            entity_ref = (
                f"document:{item.record_id}"
                if item.kind == "document_evidence"
                else f"{item.kind}:{item.record_id}"
            )
            add_content(
                item.content,
                {
                    "link_type": "evidence",
                    "entity_ref": entity_ref,
                    "record_id": item.record_id,
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
            bundle_id = (
                "memory-bundle:"
                + hashlib.sha256(
                    f"{bundle_index}:{bundle.primary_path}".encode("utf-8")
                ).hexdigest()
            )
            if bundle.current_understanding:
                add_content(
                    bundle.current_understanding,
                    {
                        "link_type": "bundle_current",
                        "bundle_id": bundle_id,
                        "primary_path": bundle.primary_path,
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
                            "ordinal": index,
                            "file_path": item.file_path,
                        },
                    )
                add_content(
                    item.snippet,
                    {
                        "link_type": "bundle_evidence",
                        "bundle_id": bundle_id,
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
                            "ordinal": index,
                            "file_path": item.file_path,
                        },
                    )
                add_content(
                    item.snippet,
                    {
                        "link_type": "bundle_history",
                        "bundle_id": bundle_id,
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
                    },
                )
        return list(records.values())

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
        trace_persisted: bool,
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
                "persisted": trace_persisted,
            },
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
            actual = len(str(finalized).encode("utf-8"))
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

    async def execute(
        self,
        query: Annotated[str, "搜索问题"],
        top_k: Annotated[int, "返回数量"] = 5,
        enable_association: Annotated[bool, "是否启用联想"] = True,
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

        try:
            service = await self._get_service()
            from .living import CoRecallEvent, RecallEvent

            effective_stream = str(
                stream_scope or self.get_current_stream_id() or ""
            )
            context_key = "/".join(
                item for item in ("life_engine", effective_stream) if item
            )
            begin_recall = getattr(service, "begin_memory_recall", None)
            trace_persisted = callable(begin_recall)
            if trace_persisted:
                episode = await begin_recall(
                    query=normalized_query,
                    retrieval_intent=mode,
                    consciousness_instance_id="life_engine",
                    stream_scope=effective_stream,
                    context_key=context_key,
                    policy_version="living-recall-v1",
                    context={
                        "valid_at": valid_at,
                        "recorded_as_of": recorded_as_of,
                        "file_types": list(file_types or []),
                        "time_range_days": int(time_range_days),
                    },
                )
            else:
                episode = SimpleNamespace(
                    episode_id=f"unpersisted_recall_{uuid4().hex}",
                    policy_version="living-recall-v1",
                    random_seed=uuid4().int & ((1 << 63) - 1),
                    context_key=context_key,
                )
            retrieval_seed = (
                int(continuation_state.get("random_seed") or 0)
                if continuation_state is not None
                else int(episode.random_seed)
            )
            document_results = await service.search_memory(
                normalized_query,
                top_k=top_k,
                enable_association=enable_association,
                file_types=file_types,
                time_range_days=time_range_days,
                return_bundles=False,
            )
            expand_associations = getattr(
                service,
                "expand_living_document_associations",
                None,
            )
            if callable(expand_associations):
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
                if callable(build_bundles)
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

            now_iso = datetime.now(timezone.utc).astimezone().isoformat()

            def _entity_ref(item: Any) -> str:
                prefix = "document" if item.kind == "document_evidence" else item.kind
                return f"{prefix}:{item.record_id}"

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

            projection_episode = SimpleNamespace(
                episode_id=episode.episode_id,
                policy_version=episode.policy_version,
                random_seed=retrieval_seed,
                context_key=episode.context_key,
            )
            full_items = [
                self._project_record(record, delivery="full")
                for record in records
            ]
            original_bytes = 0
            for _ in range(12):
                original_payload = self._projection_payload(
                    query=normalized_query,
                    mode=mode,
                    stream_scope=stream_scope,
                    valid_at=valid_at,
                    recorded_as_of=recorded_as_of,
                    episode=projection_episode,
                    trace_persisted=trace_persisted,
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
                            }
                        )
                    candidate_payload = self._projection_payload(
                        query=normalized_query,
                        mode=mode,
                        stream_scope=stream_scope,
                        valid_at=valid_at,
                        recorded_as_of=recorded_as_of,
                        episode=projection_episode,
                        trace_persisted=trace_persisted,
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
                    trace_persisted=trace_persisted,
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

            delivered_evidence = {
                str(item["entity_ref"]): item
                for item in final_payload["evidence_results"]
            }
            recall_events = tuple(
                RecallEvent(
                    event_id=f"recall_event_{uuid4().hex}",
                    episode_id=episode.episode_id,
                    action="candidate_exposed",
                    entity_ref=_entity_ref(item),
                    ordinal=index,
                    source=item.source,
                    recorded_at=now_iso,
                    metadata={
                        "rank_score": item.rank_score,
                        "rank_is_not_truth": True,
                        "projection_version": MEMORY_SEARCH_PROJECTION_VERSION,
                        "content_delivery": delivered_evidence[_entity_ref(item)][
                            "content_delivery"
                        ],
                        "content_ref": delivered_evidence[_entity_ref(item)][
                            "content_ref"
                        ],
                    },
                )
                for index, item in enumerate(evidence_results)
                if _entity_ref(item) in delivered_evidence
            )
            append_events = getattr(service, "append_memory_recall_events", None)
            if recall_events and callable(append_events):
                await append_events(recall_events)
            recalled_refs = tuple(
                dict.fromkeys(event.entity_ref for event in recall_events)
            )
            if len(recalled_refs) >= 2:
                append_corecall = getattr(service, "append_memory_corecall", None)
                if callable(append_corecall):
                    await append_corecall(
                        CoRecallEvent(
                            corecall_id=f"corecall_{uuid4().hex}",
                            episode_id=episode.episode_id,
                            context_key=context_key,
                            signal="co_exposed_in_recall",
                            entity_refs=recalled_refs,
                            actor="life_engine",
                            reason="同一次有界检索投影中共同交付",
                            recorded_at=now_iso,
                            metadata={
                                "projection_version": MEMORY_SEARCH_PROJECTION_VERSION,
                                "frontier_sha256": frontier_sha256,
                            },
                        )
                    )

            if len(str(final_payload).encode("utf-8")) > budget:
                return False, {"error": "memory search projection exceeded hard budget"}
            return True, final_payload

        except Exception as e:
            logger.error(f"记忆搜索失败: {e}", exc_info=True)
            return False, {"error": f"搜索失败: {e}"}


# ============================================================
# nucleus_relate_file - 建立文件关联
# ============================================================


class LifeEngineRelateFileTool(BaseTool):
    """建立文件关联工具。"""

    tool_name: str = "nucleus_relate_file"
    tool_description: str = (
        "追加一条由当前 active consciousness 明确表达的开放词汇记忆关系。"
        "系统只保存原话、reason、actor、source occurrence 与时间，不推断关系类型、"
        "不计算主观强度，也不把检索分数当成真值。每次调用只写 SemanticRelation "
        "不可变历史；legacy memory_edges 不再被同步修改。"
    )
    chatter_allow: list[str] = ["life_engine_internal"]

    def __init__(self, plugin):
        super().__init__(plugin)

    async def execute(
        self,
        source_path: Annotated[str, "源文件路径"],
        target_path: Annotated[str, "目标文件路径"],
        relation_type: Annotated[str, "用自己的话描述关系；系统不限制固定类型"],
        reason: Annotated[str, "为什么建立这条关系"],
    ) -> tuple[bool, dict[str, Any]]:
        """Append one explicit SemanticRelation from the active runtime actor."""

        if not source_path or not target_path:
            return False, {"error": "source_path 和 target_path 不能为空"}

        relation_text = str(relation_type or "").strip()
        if not relation_text:
            return False, {"error": "relation_type 不能为空"}
        reason_text = str(reason or "").strip()
        if not reason_text:
            return False, {"error": "reason 不能为空"}

        source_path, source_error = _eligible_path_or_error(source_path)
        target_path, target_error = _eligible_path_or_error(target_path)
        if source_error or target_error:
            return False, {"error": source_error or target_error}
        if source_path == target_path:
            return False, {"error": "SemanticRelationEndpointsMustDiffer"}

        try:
            runtime = await _resolve_relation_runtime(self)
            from .living import SemanticRelation

            proposed = SemanticRelation(
                relation_id=_stable_relation_id(runtime),
                source_ref=f"document:{source_path}",
                target_ref=f"document:{target_path}",
                predicate=relation_text,
                reason=reason_text,
                actor=runtime.actor_consciousness_instance_id,
                recorded_at=_relation_recorded_at(self),
                consciousness_instance_id=runtime.actor_consciousness_instance_id,
                stream_scope=runtime.stream_scope,
                metadata={
                    "source_occurrence_id": runtime.source_occurrence_id,
                    "source_occurrence_kind": runtime.source_occurrence_kind,
                    "tool_call_id": runtime.tool_call_id,
                },
            )
            existing_relations = (
                await runtime.memory_service.list_memory_semantic_relations(
                    proposed.source_ref
                )
            )
            existing = next(
                (
                    relation
                    for relation in existing_relations
                    if relation.relation_id == proposed.relation_id
                ),
                None,
            )
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
                    replayed = (
                        await runtime.memory_service.list_memory_semantic_relations(
                            proposed.source_ref
                        )
                    )
                    existing = next(
                        (
                            relation
                            for relation in replayed
                            if relation.relation_id == proposed.relation_id
                        ),
                        None,
                    )
                    if existing is None or not _same_semantic_relation(
                        existing, proposed
                    ):
                        raise
                    semantic_relation = existing

            logger.info(
                "SemanticRelation appended: "
                f"relation_id={semantic_relation.relation_id} "
                f"actor={runtime.actor_consciousness_instance_id}"
            )

            return True, {
                "action": "relate_file",
                "source_path": source_path,
                "target_path": target_path,
                "relation_type": relation_text,
                "reason": reason_text,
                "relation_id": semantic_relation.relation_id,
                "actor": semantic_relation.actor,
                "source_occurrence_id": runtime.source_occurrence_id,
                "authority": "memory_semantic_relations",
                "legacy_edge_written": False,
            }

        except Exception as exc:
            logger.error(f"建立关联失败: {exc}", exc_info=True)
            return False, {
                "error": str(exc) or type(exc).__name__,
                "error_type": type(exc).__name__,
            }


# ============================================================
# nucleus_view_relations - 查看关联图谱
# ============================================================


class LifeEngineViewRelationsTool(BaseTool):
    """查看文件关联图谱工具。"""

    tool_name: str = "nucleus_view_relations"
    tool_description: str = (
        "查看文件相关的显式 SemanticRelation 历史。返回结果以主体明确写下的"
        "开放词汇关系为准；legacy memory_edges 仅作为带来源标记的只读兼容投影，"
        "不会被自动晋升、合并或解释为主体关系。"
    )
    chatter_allow: list[str] = ["life_engine_internal", "life_chatter"]

    def __init__(self, plugin):
        super().__init__(plugin)

    async def _get_service(self) -> LifeMemoryService:
        """获取记忆服务实例。"""
        from ..service import LifeEngineService

        service = LifeEngineService.get_instance()
        if service is None or service._memory_service is None:
            raise RuntimeError("记忆服务未初始化")
        return service._memory_service

    async def execute(
        self,
        file_path: Annotated[str, "文件路径"],
        depth: Annotated[int, "legacy compatibility projection 遍历深度 1-3"] = 1,
        min_strength: Annotated[
            float,
            "仅用于 legacy compatibility projection 的可达性阈值，不是真值判断",
        ] = 0.2,
    ) -> tuple[bool, dict[str, Any]]:
        """查看关联图谱。"""
        if not file_path:
            return False, {"error": "file_path 不能为空"}
        file_path, path_error = _eligible_path_or_error(file_path)
        if path_error:
            return False, {"error": path_error}

        depth = max(1, min(3, depth))
        min_strength = max(0.0, min(1.0, min_strength))

        try:
            service = await self._get_service()
            center_ref = f"document:{file_path}"
            semantic_relations = await service.list_memory_semantic_relations(
                center_ref
            )
            semantic_payloads = [
                _semantic_relation_payload(relation, center_ref=center_ref)
                for relation in semantic_relations
            ]

            legacy_projection: dict[str, Any] = {
                "projection_kind": "legacy_memory_edges_compatibility",
                "authoritative": False,
                "read_only": True,
                "automatic_promotion_to_semantic_history": False,
                "strength_is_truth": False,
                "depth": depth,
                "min_strength": min_strength,
            }
            try:
                legacy_relations = await service.get_file_relations(
                    file_path=file_path,
                    depth=depth,
                    min_strength=min_strength,
                )
                if "error" in legacy_relations:
                    legacy_projection.update(
                        {
                            "available": False,
                            "error": legacy_relations["error"],
                            "center": None,
                            "outgoing": [],
                            "incoming": [],
                        }
                    )
                else:
                    legacy_projection.update(
                        {
                            "available": True,
                            "center": legacy_relations.get("center"),
                            "outgoing": list(legacy_relations.get("outgoing") or []),
                            "incoming": list(legacy_relations.get("incoming") or []),
                        }
                    )
            except Exception as legacy_exc:
                legacy_projection.update(
                    {
                        "available": False,
                        "error_type": type(legacy_exc).__name__,
                        "center": None,
                        "outgoing": [],
                        "incoming": [],
                    }
                )

            return True, {
                "action": "view_relations",
                "file_path": file_path,
                "authority": "memory_semantic_relations",
                "semantic_relation_count": len(semantic_payloads),
                "semantic_relations": semantic_payloads,
                "legacy_compatibility_projection": legacy_projection,
            }

        except Exception as exc:
            logger.error(f"查看关联失败: {exc}", exc_info=True)
            return False, {
                "error": str(exc) or type(exc).__name__,
                "error_type": type(exc).__name__,
            }


# ============================================================
# nucleus_forget_relation - 删除/弱化关联
# ============================================================


class LifeEngineForgetRelationTool(BaseTool):
    """Historical compatibility shell for retired legacy graph mutations."""

    tool_name: str = "nucleus_forget_relation"
    tool_description: str = (
        "历史兼容入口；legacy memory_edges 删除/弱化已经退役。"
        "当前 SemanticRelation 只有不可变追加契约，在合法 retract/supersede "
        "协议落地前，本工具始终返回 LegacyRelationMutationRetired 且不修改数据。"
    )
    chatter_allow: list[str] = ["life_engine_internal"]

    def __init__(self, plugin):
        super().__init__(plugin)

    async def execute(
        self,
        source_path: Annotated[str, "源文件路径"],
        target_path: Annotated[str, "目标文件路径"],
        mode: Annotated[str, "操作模式: delete/weaken"] = "weaken",
    ) -> tuple[bool, dict[str, Any]]:
        """Reject every historical deletion/weaken request without side effects."""

        del source_path, target_path
        return False, _legacy_relation_mutation_retired_payload(mode)


# ============================================================
# nucleus_memory_stats - 记忆系统统计
# ============================================================


class LifeEngineMemoryStatsTool(BaseTool):
    """记忆系统统计工具。"""

    tool_name: str = "nucleus_memory_stats"
    tool_description: str = "获取记忆系统的统计信息：节点数量、关联数量、平均激活强度等。用于了解记忆网络的整体状态。"
    chatter_allow: list[str] = ["life_engine_internal", "life_chatter"]

    def __init__(self, plugin):
        super().__init__(plugin)

    async def _get_service(self) -> LifeMemoryService:
        """获取记忆服务实例。"""
        from ..service import LifeEngineService

        service = LifeEngineService.get_instance()
        if service is None or service._memory_service is None:
            raise RuntimeError("记忆服务未初始化")
        return service._memory_service

    async def execute(self) -> tuple[bool, dict[str, Any]]:
        """获取统计信息。"""
        try:
            service = await self._get_service()
            stats = await service.get_stats()

            return True, {
                "action": "memory_stats",
                **stats
            }

        except Exception as e:
            logger.error(f"获取统计失败: {e}", exc_info=True)
            return False, {"error": f"获取统计失败: {e}"}


# ============================================================
# 工具注册列表
# ============================================================


class NucleusRelationsTool(BaseTool):
    """Append or inspect explicit memory relation history."""

    tool_name: str = "nucleus_relations"
    tool_description: str = (
        "管理显式记忆关系历史。action=add 只追加 SemanticRelation；"
        "action=view 优先返回 SemanticRelation，并把 legacy graph 标为只读兼容投影。"
        "删除、弱化和按分数遗忘不属于当前协议。"
    )
    chatter_allow: list[str] = ["life_engine_internal"]

    async def execute(
        self,
        action: Annotated[Literal["add", "view"], "操作：add/view"] = "view",
        source_path: Annotated[str, "add 的源文件路径"] = "",
        target_path: Annotated[str, "add 的目标文件路径"] = "",
        relation_type: Annotated[
            str,
            "add 的开放词汇关系原话；系统不限制固定类型",
        ] = "",
        reason: Annotated[str, "add 时由当前主体写下的关系理由"] = "",
        file_path: Annotated[str, "view 的中心文件路径"] = "",
        depth: Annotated[int, "view 的 legacy compatibility 遍历深度 1-3"] = 1,
        min_strength: Annotated[
            float,
            "仅用于 view 的 legacy compatibility 可达性阈值",
        ] = 0.2,
    ) -> tuple[bool, str | dict[str, Any]]:
        action_value = str(action or "view").strip().lower()
        if action_value in {"forget", "remove", "delete", "weaken"}:
            return False, _legacy_relation_mutation_retired_payload(action_value)

        if action_value not in {"add", "view"}:
            return False, {
                "error": "UnsupportedRelationAction",
                "action": action_value,
                "allowed_actions": ["add", "view"],
            }
        cls = (
            LifeEngineRelateFileTool
            if action_value == "add"
            else LifeEngineViewRelationsTool
        )
        tool = cls(plugin=self.plugin)
        tool._bind_runtime_context(
            stream_id=self.get_current_stream_id(),
            message=self.trigger_message,
            tool_call_id=str(getattr(self, "_tool_call_id", "") or ""),
        )
        runtime_task_name = getattr(self, "_runtime_task_name", None)
        if runtime_task_name is not None:
            tool._runtime_task_name = runtime_task_name
        if action_value == "add":
            return await tool.execute(  # type: ignore[call-arg]
                source_path=source_path,
                target_path=target_path,
                relation_type=relation_type,
                reason=reason,
            )
        return await tool.execute(  # type: ignore[call-arg]
            file_path=file_path,
            depth=depth,
            min_strength=min_strength,
        )


MEMORY_TOOLS = [
    LifeEngineSearchMemoryTool,
    NucleusRelationsTool,
    LifeEngineMemoryStatsTool,
]
