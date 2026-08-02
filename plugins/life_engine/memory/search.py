"""记忆检索数据结构与操作函数。

包含 SearchResult 数据类、混合检索、RRF 融合、
激活扩散等函数。
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple, TYPE_CHECKING

from src.app.plugin_system.api import log_api

if TYPE_CHECKING:
    pass

from .edges import EdgeType, EXPLICIT_RELATION_EDGE_TYPES, row_to_edge
from .eligibility import (
    assess_document_path,
    assess_workspace_document,
    eligible_document_path_sql,
    is_eligible_indexed_document_path,
    register_indexed_path_sql_function,
)
from .nodes import MemoryNode, NodeType, canonical_file_node_id, row_to_node
from .temporal import parse_temporal_date
from .sqlite_runtime import run_db

# NOTE: Every ``register_indexed_path_sql_function(db)`` call below is
# deliberately unconditional and repeated per query. This is safe and cheap
# because ``eligibility.register_indexed_path_sql_function`` already
# serializes installation with its own ``_SQL_FUNCTION_LOCK`` and probes the
# connection (via ``pragma_function_list``, or a direct call on older
# SQLite) before ever calling ``sqlite3.Connection.create_function`` again.
# None of these call sites hold ``indexing._TRANSACTION_LOCK`` or
# ``edges._EDGE_WRITE_LOCK`` while registering, so there is no lock-ordering
# cycle with those transaction/edge-write locks either. Do not remove these
# calls as a "de-duplication" cleanup: read paths here can run against a
# freshly opened connection that never went through ``create_memory_schema``.

logger = log_api.get_logger("life_engine.memory.search")


# ============================================================
# 常量
# ============================================================

RRF_K = 60  # RRF 融合参数
SPREAD_DECAY = 0.7  # 激活扩散衰减系数
SPREAD_THRESHOLD = 0.3  # 激活扩散阈值


# ============================================================
# 数据类型定义
# ============================================================


@dataclass
class SearchResult:
    """检索结果。"""

    file_path: str
    title: str
    snippet: str
    relevance: float
    source: str  # 'direct' | 'associated'
    association_path: List[str] = field(default_factory=list)
    association_reason: str = ""
    score_kind: str = "rank"


@dataclass(frozen=True)
class ChunkSearchResult:
    """A chunk-level full-text hit."""

    node_id: str
    chunk_id: str
    score: float
    snippet: str
    chunk_index: int


@dataclass(frozen=True)
class EmbeddingResult:
    """Validated embedding batch with the provider model retained."""

    embeddings: List[List[float]]
    model_name: str = ""

    @property
    def dimension(self) -> int:
        return len(self.embeddings[0]) if self.embeddings else 0

    def __iter__(self):
        return iter(self.embeddings)

    def __len__(self) -> int:
        return len(self.embeddings)

    def __getitem__(self, index: int) -> List[float]:
        return self.embeddings[index]


@dataclass
class SearchDiagnostics:
    """Read-only diagnostics for one search execution."""

    degraded: bool = False
    fts_success: bool = False
    vector_success: bool = False
    fts_candidate_count: int = 0
    vector_candidate_count: int = 0
    phase_timings: Dict[str, float] = field(default_factory=dict)
    error_types: Dict[str, str] = field(default_factory=dict)
    errors: Dict[str, str] = field(default_factory=dict)


@dataclass
class DetailedSearchResult:
    """Search results together with non-sensitive execution diagnostics."""

    results: List[SearchResult]
    diagnostics: SearchDiagnostics

    @property
    def degraded(self) -> bool:
        return self.diagnostics.degraded

    @property
    def fts_success(self) -> bool:
        return self.diagnostics.fts_success

    @property
    def vector_success(self) -> bool:
        return self.diagnostics.vector_success

    @property
    def fts_candidate_count(self) -> int:
        return self.diagnostics.fts_candidate_count

    @property
    def vector_candidate_count(self) -> int:
        return self.diagnostics.vector_candidate_count

    @property
    def phase_timings(self) -> Dict[str, float]:
        return self.diagnostics.phase_timings

    @property
    def error_types(self) -> Dict[str, str]:
        return self.diagnostics.error_types

    @property
    def errors(self) -> Dict[str, str]:
        return self.diagnostics.errors


def _query_metadata(query: str) -> Dict[str, Any]:
    """Return safe query metadata for optional visual events."""
    value = str(query or "")
    return {
        "query_length": len(value),
        "query_hash": hashlib.sha256(value.encode("utf-8")).hexdigest()[:16],
    }


def _short_error(exc: BaseException) -> str:
    """Return only an error type so query text cannot enter diagnostics."""
    return type(exc).__name__


@dataclass
class _FtsSearchOutcome:
    results: List[ChunkSearchResult]
    success: bool
    degraded: bool = False
    error_type: str | None = None
    error: str = ""


@dataclass(frozen=True)
class _DeferredChunkVectorHit:
    """A chunk-vector hit awaiting SQLite revision validation."""

    chunk_id: str
    node_id: str
    document_hash: str
    index_revision: Any
    similarity: float


@dataclass
class _VectorSearchOutcome:
    results: List[Tuple[str, float]]
    success: bool
    degraded: bool = False
    error_type: str | None = None
    error: str = ""
    deferred_chunk_hits: List[_DeferredChunkVectorHit] = field(default_factory=list)


@dataclass(frozen=True)
class _LoadedNode:
    node_id: str
    node_type: str
    file_path: str
    title: str
    event_date: str | None
    is_deleted: bool
    source_mtime: float | None
    created_at: float
    preview_content: str


# ============================================================
# 向量检索
# ============================================================


async def get_chroma_collection(db_path: str) -> Any:
    """获取 ChromaDB collection。

    Args:
        db_path: 向量数据库路径

    Returns:
        ChromaDB collection 对象
    """
    from src.kernel.vector_db import get_vector_db_service

    vector_service = get_vector_db_service(db_path)
    return await vector_service.get_or_create_collection("life_memory")


async def embed_texts(texts: Iterable[str]) -> EmbeddingResult:
    """Generate one validated embedding batch and retain the provider model."""
    values = [str(text) for text in texts]
    if not values:
        raise ValueError("Embedding 输入不能为空")

    from src.app.plugin_system.api.llm_api import (
        create_embedding_request,
        get_model_set_by_task,
    )

    try:
        model_set = get_model_set_by_task("embedding")
        request = create_embedding_request(
            model_set=model_set,
            request_name="life_memory_embedding",
            inputs=values,
        )
        response = await request.send()
        raw_embeddings = getattr(response, "embeddings", None)
        if raw_embeddings is None:
            raise RuntimeError("Embedding 响应缺少向量")
        if len(raw_embeddings) != len(values):
            raise ValueError(
                f"Embedding 数量不匹配: expected={len(values)}, actual={len(raw_embeddings)}"
            )

        normalized: List[List[float]] = []
        dimension: int | None = None
        for index, raw_vector in enumerate(raw_embeddings):
            if raw_vector is None:
                raise ValueError(f"Embedding 向量为空: index={index}")
            vector = [float(value) for value in raw_vector]
            if not vector:
                raise ValueError(f"Embedding 向量为空: index={index}")
            if dimension is None:
                dimension = len(vector)
            elif len(vector) != dimension:
                raise ValueError("Embedding 向量维度不一致")
            normalized.append(vector)

        return EmbeddingResult(
            embeddings=normalized,
            model_name=str(getattr(response, "model_name", "") or ""),
        )
    except Exception as exc:
        logger.error(f"Embedding 生成失败: {type(exc).__name__}")
        raise


async def embed_text(text: str) -> List[float]:
    """Generate one embedding while preserving the historical API."""
    result = await embed_texts([text])
    return result.embeddings[0]


async def sync_embedding(
    db: sqlite3.Connection,
    collection: Any,
    file_path: str,
    content: str,
    get_node_by_file_path_func: Any = None,
) -> None:
    """兼容旧调用：仅更新 SQLite 文档/ outbox，不直接写入 Chroma。

    Chroma 只能由 index worker 写入。已有 SQLite 修订会保留其标题和
    内容；空内容不会清空已索引的文档，只会重新排入其现有 outbox 工作。
    """
    del collection, get_node_by_file_path_func
    eligibility = assess_document_path(file_path)
    if not eligibility.eligible:
        return

    from .indexing import enqueue_index_job, upsert_document_rows

    text = str(content or "")
    canonical_path, node_id = canonical_file_node_id(eligibility.path)

    def _enqueue() -> str | None:
        existing = None
        if _table_exists(db, "memory_nodes"):
            existing = db.execute(
                "SELECT node_type, file_path, content_hash, title "
                "FROM memory_nodes WHERE node_id = ?",
                (node_id,),
            ).fetchone()
        if existing is not None:
            node_type, stored_path, content_hash, title = existing
            if (
                str(node_type or NodeType.FILE.value).lower() != NodeType.FILE.value
                or not is_eligible_indexed_document_path(stored_path)
                or str(stored_path) != canonical_path
            ):
                return None
            if text:
                result = upsert_document_rows(
                    db,
                    canonical_path,
                    text,
                    title=str(title or ""),
                )
                return result.file_path
            if content_hash:
                enqueue_index_job(db, node_id, str(content_hash))
                return canonical_path
            return None
        if not text:
            return None
        result = upsert_document_rows(db, canonical_path, text)
        return result.file_path

    try:
        queued_path = await run_db(_enqueue)
        if queued_path:
            logger.debug(f"已排入 embedding outbox: {queued_path}")
    except Exception as exc:
        logger.error(f"排入 embedding outbox 失败 ({canonical_path}): {type(exc).__name__}")


def _collection_metadata(collection: Any) -> Dict[str, Any]:
    metadata = getattr(collection, "metadata", None)
    if isinstance(metadata, dict):
        return dict(metadata)
    return {}


def _is_chunk_collection(collection: Any) -> bool:
    if collection is None:
        return False
    metadata = _collection_metadata(collection)
    kind = str(metadata.get("collection_kind") or metadata.get("kind") or "").lower()
    if "chunk" in kind or metadata.get("chunk_index_version") is not None:
        return True
    name = str(getattr(collection, "name", "") or "").lower()
    return "life_memory_chunks" in name


def _first_query_values(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        if not value:
            return []
        first = value[0]
        if isinstance(first, (list, tuple)):
            return list(first)
        return list(value)
    return [value]


def _current_chunk_map(
    db: sqlite3.Connection,
    chunk_ids: Sequence[str],
) -> Dict[str, Tuple[str, str, int]]:
    """Map only current SQLite chunks to their owning document revision."""
    ordered = list(dict.fromkeys(str(value) for value in chunk_ids if value))
    if (
        not ordered
        or not _table_exists(db, "memory_chunks")
        or not _table_exists(db, "memory_nodes")
    ):
        return {}
    register_indexed_path_sql_function(db)
    node_columns = _table_columns(db, "memory_nodes")
    clauses = [f"c.chunk_id IN ({','.join('?' for _ in ordered)})"]
    if "is_deleted" in node_columns:
        clauses.append("COALESCE(n.is_deleted, 0) = 0")
    if "node_type" in node_columns:
        clauses.append("lower(COALESCE(n.node_type, 'file')) = 'file'")
    eligibility_sql, eligibility_params = eligible_document_path_sql("n.file_path")
    clauses.append(eligibility_sql)
    rows = db.execute(
        "SELECT c.chunk_id, c.node_id, n.content_hash, n.index_revision "
        "FROM memory_chunks c JOIN memory_nodes n ON n.node_id = c.node_id "
        f"WHERE {' AND '.join(clauses)}",
        [*ordered, *eligibility_params],
    ).fetchall()
    return {
        str(row["chunk_id"]): (
            str(row["node_id"]),
            str(row["content_hash"] or ""),
            int(row["index_revision"] or 0),
        )
        for row in rows
    }


async def _query_vector_collection(
    collection: Any,
    query_embedding: Sequence[float],
    top_k: int,
    *,
    chunk_mode: bool,
    db: sqlite3.Connection | None,
    defer_chunk_validation: bool = False,
) -> _VectorSearchOutcome:
    """Query and normalize one vector collection without generating embeddings."""
    try:
        include = ["metadatas", "distances"] if chunk_mode else ["distances"]
        results = await asyncio.to_thread(
            collection.query,
            query_embeddings=[list(query_embedding)],
            n_results=max(0, int(top_k)),
            include=include,
        )
        ids = _first_query_values(results.get("ids") if isinstance(results, dict) else None)
        distances = _first_query_values(
            results.get("distances") if isinstance(results, dict) else None
        )
        metadatas = _first_query_values(
            results.get("metadatas") if isinstance(results, dict) else None
        )
        if not ids:
            return _VectorSearchOutcome(results=[], success=True)

        deferred_validation = chunk_mode and db is not None and defer_chunk_validation
        degraded = False
        error_type: str | None = None
        raw_pairs: List[Tuple[str, float]] = []
        deferred_hits: List[_DeferredChunkVectorHit] = []
        result_ids = [str(value) for value in ids]
        current_chunks = (
            await _async_db_read(_current_chunk_map, db, result_ids)
            if chunk_mode and db is not None and not deferred_validation
            else {}
        )
        best_by_node: Dict[str, float] = {}
        for index, result_id in enumerate(result_ids):
            metadata = metadatas[index] if index < len(metadatas) else {}
            metadata = metadata if isinstance(metadata, dict) else {}
            resolved_id = result_id
            distance = distances[index] if index < len(distances) else 0.0
            similarity = 1.0 / (1.0 + float(distance))
            if chunk_mode:
                metadata_node_id = str(metadata.get("node_id") or "")
                metadata_hash = str(metadata.get("document_hash") or "")
                metadata_revision = metadata.get("index_revision")
                if deferred_validation:
                    deferred_hits.append(
                        _DeferredChunkVectorHit(
                            chunk_id=result_id,
                            node_id=metadata_node_id,
                            document_hash=metadata_hash,
                            index_revision=metadata_revision,
                            similarity=similarity,
                        )
                    )
                    if not metadata_node_id:
                        continue
                    resolved_id = metadata_node_id
                else:
                    current = current_chunks.get(result_id)
                    if db is not None and current is None:
                        degraded = True
                        error_type = error_type or "StaleChunkVector"
                        continue
                    if current is not None:
                        node_id, document_hash, index_revision = current
                        try:
                            revision_mismatch = (
                                metadata_revision is not None
                                and int(metadata_revision) != index_revision
                            )
                        except (TypeError, ValueError):
                            revision_mismatch = True
                        if (
                            (metadata_node_id and metadata_node_id != node_id)
                            or (metadata_hash and metadata_hash != document_hash)
                            or revision_mismatch
                        ):
                            degraded = True
                            error_type = error_type or "StaleChunkVector"
                            continue
                        resolved_id = node_id
                    else:
                        resolved_id = metadata_node_id
                        if not resolved_id:
                            degraded = True
                            error_type = error_type or "ChunkMetadataUnavailable"
                            continue

            if chunk_mode:
                best_by_node[resolved_id] = max(
                    best_by_node.get(resolved_id, 0.0), similarity
                )
            else:
                raw_pairs.append((resolved_id, similarity))
        if chunk_mode:
            raw_pairs = list(best_by_node.items())
        return _VectorSearchOutcome(
            results=raw_pairs,
            success=True,
            degraded=degraded,
            error_type=error_type,
            deferred_chunk_hits=deferred_hits,
        )
    except Exception as exc:
        logger.warning(f"向量检索降级: {type(exc).__name__}")
        return _VectorSearchOutcome(
            results=[],
            success=False,
            degraded=True,
            error_type=type(exc).__name__,
            error=_short_error(exc),
        )


def _resolve_deferred_chunk_hits(
    db: sqlite3.Connection,
    hits: Sequence[_DeferredChunkVectorHit],
) -> tuple[List[Tuple[str, float]], bool, str | None]:
    """Validate deferred chunk-vector hits against the current SQLite revision."""
    current_chunks = _current_chunk_map(db, [hit.chunk_id for hit in hits])
    best_by_node: Dict[str, float] = {}
    degraded = False
    error_type: str | None = None
    for hit in hits:
        current = current_chunks.get(hit.chunk_id)
        if current is None:
            degraded = True
            error_type = error_type or "StaleChunkVector"
            continue
        node_id, document_hash, index_revision = current
        try:
            revision_mismatch = (
                hit.index_revision is not None and int(hit.index_revision) != index_revision
            )
        except (TypeError, ValueError):
            revision_mismatch = True
        if (
            (hit.node_id and hit.node_id != node_id)
            or (hit.document_hash and hit.document_hash != document_hash)
            or revision_mismatch
        ):
            degraded = True
            error_type = error_type or "StaleChunkVector"
            continue
        best_by_node[node_id] = max(best_by_node.get(node_id, 0.0), hit.similarity)
    return list(best_by_node.items()), degraded, error_type


async def _filter_vector_outcome(
    outcome: _VectorSearchOutcome,
    filter_existing_func: Any,
    db: sqlite3.Connection | None,
    *,
    chunk_mode: bool,
    validate_db: bool = True,
) -> _VectorSearchOutcome:
    raw_pairs = outcome.results
    if filter_existing_func:
        filtered_pairs, stale_ids = await filter_existing_func(raw_pairs)
        if stale_ids:
            logger.warning(f"向量检索命中 {len(stale_ids)} 个脏节点ID（节点表不存在），已忽略")
        raw_pairs = filtered_pairs
    if validate_db and db is not None:
        raw_pairs, _ = await filter_existing_scores(db, raw_pairs)
    outcome.results = raw_pairs
    return outcome


async def _resolve_deferred_vector_outcome(
    db: sqlite3.Connection,
    outcome: _VectorSearchOutcome,
) -> _VectorSearchOutcome:
    """Replace provisional chunk results after concurrent readers have finished."""
    if not outcome.deferred_chunk_hits:
        return outcome
    try:
        results, degraded, error_type = await _async_db_read(
            _resolve_deferred_chunk_hits,
            db,
            outcome.deferred_chunk_hits,
        )
    except Exception as exc:
        logger.warning(f"向量检索降级: {type(exc).__name__}")
        outcome.results = []
        outcome.success = False
        outcome.degraded = True
        outcome.error_type = type(exc).__name__
        outcome.error = _short_error(exc)
        outcome.deferred_chunk_hits = []
        return outcome
    outcome.results = results
    outcome.degraded = outcome.degraded or degraded
    outcome.error_type = outcome.error_type or error_type
    outcome.deferred_chunk_hits = []
    return outcome


async def _vector_search_outcome(
    query: str,
    collection: Any,
    top_k: int,
    filter_existing_func: Any = None,
    *,
    db: sqlite3.Connection | None = None,
    chunk_collection: Any = None,
    validate_db: bool = True,
    defer_chunk_validation: bool = False,
) -> _VectorSearchOutcome:
    """Prefer chunk vectors, with an explicit degraded legacy-node fallback."""
    target = chunk_collection if chunk_collection is not None else collection
    chunk_mode = chunk_collection is not None or _is_chunk_collection(target)
    if target is None:
        return _VectorSearchOutcome(
            results=[],
            success=False,
            degraded=True,
            error_type="CollectionUnavailable",
            error="vector collection unavailable",
        )
    try:
        query_embedding = await embed_text(query)
        primary = await _query_vector_collection(
            target,
            query_embedding,
            top_k,
            chunk_mode=chunk_mode,
            db=db,
            defer_chunk_validation=defer_chunk_validation,
        )

        has_legacy_fallback = (
            chunk_mode
            and collection is not None
            and collection is not target
            and not _is_chunk_collection(collection)
        )
        if has_legacy_fallback and (not primary.success or not primary.results):
            legacy = await _query_vector_collection(
                collection,
                query_embedding,
                top_k,
                chunk_mode=False,
                db=db,
            )
            if legacy.success:
                legacy.degraded = True
                legacy.error_type = "LegacyVectorFallback"
                legacy.error = primary.error
                return await _filter_vector_outcome(
                    legacy,
                    filter_existing_func,
                    db,
                    chunk_mode=False,
                    validate_db=validate_db,
                )
            return legacy

        return await _filter_vector_outcome(
            primary,
            filter_existing_func,
            db,
            chunk_mode=chunk_mode,
            validate_db=validate_db,
        )
    except Exception as exc:
        logger.warning(f"向量检索降级: {type(exc).__name__}")
        return _VectorSearchOutcome(
            results=[],
            success=False,
            degraded=True,
            error_type=type(exc).__name__,
            error=_short_error(exc),
        )


async def vector_search(
    query: str,
    collection: Any,
    top_k: int = 10,
    filter_existing_func: Any = None,
    *,
    db: sqlite3.Connection | None = None,
    chunk_collection: Any = None,
) -> List[Tuple[str, float]]:
    """向量相似度检索，保留旧 ``(node_id, score)`` API。"""
    outcome = await _vector_search_outcome(
        query,
        collection,
        top_k,
        filter_existing_func,
        db=db,
        chunk_collection=chunk_collection,
    )
    return outcome.results


# ============================================================
# 全文检索
# ============================================================


def _fts_terms(query: str) -> List[str]:
    """Extract safe literal terms without exposing FTS query syntax."""
    terms = re.findall(r"[\w\u3400-\u9fff]+", str(query or ""), flags=re.UNICODE)
    if not terms and str(query or "").strip():
        terms = [str(query).strip()]
    result: List[str] = []
    seen: set[str] = set()
    for term in terms:
        normalized = term.casefold()
        if normalized in seen:
            continue
        seen.add(normalized)
        result.append(term)
        if len(result) >= 16:
            break
    return result


def _sanitize_fts_query(query: str) -> str:
    """Build a safe OR query so multiple terms need not form one phrase."""
    quoted = []
    for term in _fts_terms(query):
        quoted.append(f'"{term.replace(chr(34), chr(34) * 2)}"')
    return " OR ".join(quoted)


def _normalize_file_types(file_types: Optional[List[str]]) -> tuple[str, ...]:
    suffixes: List[str] = []
    for value in file_types or []:
        suffix = str(value or "").strip().lower()
        if not suffix:
            continue
        suffix = "." + suffix.lstrip(".")
        if suffix not in suffixes:
            suffixes.append(suffix)
    return tuple(suffixes)


def _matches_file_type(file_path: str, file_types: Optional[List[str]]) -> bool:
    suffixes = _normalize_file_types(file_types)
    return not suffixes or str(file_path or "").lower().endswith(suffixes)


def _table_exists(db: sqlite3.Connection, table: str) -> bool:
    return db.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table', 'virtual table') AND name = ?",
        (table,),
    ).fetchone() is not None


def _table_columns(db: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in db.execute(f"PRAGMA table_info({table})").fetchall()}


async def _async_db_read(func: Any, *args: Any) -> Any:
    """Run a SQLite read off-loop, falling back for thread-bound test connections."""
    try:
        return await run_db(func, *args)
    except sqlite3.ProgrammingError as exc:
        if "created in a thread" not in str(exc):
            raise
        return func(*args)


def _node_filter_sql(
    db: sqlite3.Connection,
    *,
    alias: str,
    event_date: date | str | None,
    file_types: Optional[List[str]],
    min_event_date: date | str | None = None,
) -> tuple[str, List[Any]]:
    register_indexed_path_sql_function(db)
    columns = _table_columns(db, "memory_nodes")
    clauses = [f"{alias}.file_path IS NOT NULL", f"TRIM({alias}.file_path) <> ''"]
    params: List[Any] = []
    if "node_type" in columns:
        clauses.append(f"lower(COALESCE({alias}.node_type, 'file')) = 'file'")
    if "is_deleted" in columns:
        clauses.append(f"COALESCE({alias}.is_deleted, 0) = 0")
    if event_date is not None:
        if "event_date" not in columns:
            clauses.append("1 = 0")
        else:
            target = event_date.isoformat() if isinstance(event_date, date) else str(event_date)
            clauses.append(f"{alias}.event_date = ?")
            params.append(target)
    elif min_event_date is not None and "event_date" in columns:
        target = (
            min_event_date.isoformat()
            if isinstance(min_event_date, date)
            else str(min_event_date)
        )
        clauses.append(f"({alias}.event_date IS NULL OR {alias}.event_date >= ?)")
        params.append(target)
    suffixes = _normalize_file_types(file_types)
    if suffixes:
        suffix_clauses: List[str] = []
        for suffix in suffixes:
            suffix_clauses.append(
                f"substr(lower({alias}.file_path), -length(?)) = ?"
            )
            params.extend((suffix, suffix))
        clauses.append("(" + " OR ".join(suffix_clauses) + ")")
    eligibility_sql, eligibility_params = eligible_document_path_sql(f"{alias}.file_path")
    clauses.append(eligibility_sql)
    params.extend(eligibility_params)
    return " AND ".join(clauses), params


def _make_snippet(content: str, query: str, max_chars: int = 300) -> str:
    """Return a bounded excerpt around the first literal query term."""
    text = str(content or "")
    limit = max(1, int(max_chars))
    if len(text) <= limit:
        return text
    lowered = text.casefold()
    positions = [
        lowered.find(term.casefold())
        for term in _fts_terms(query)
        if term and lowered.find(term.casefold()) >= 0
    ]
    hit = min(positions) if positions else 0
    start = max(0, hit - limit // 3)
    end = min(len(text), start + limit)
    start = max(0, end - limit)
    snippet = text[start:end]
    if start > 0 and snippet:
        snippet = "…" + snippet[1:]
    if end < len(text) and snippet:
        snippet = snippet[:-1] + "…"
    return snippet


def _chunk_fts_rows(
    db: sqlite3.Connection,
    query: str,
    top_k: int,
    event_date: date | str | None,
    file_types: Optional[List[str]],
) -> List[ChunkSearchResult]:
    if not _table_exists(db, "memory_chunks_fts") or not _table_exists(db, "memory_chunks"):
        raise RuntimeError("chunk FTS unavailable")
    match_query = _sanitize_fts_query(query)
    if not match_query:
        return []
    filter_sql, filter_params = _node_filter_sql(
        db,
        alias="n",
        event_date=event_date,
        file_types=file_types,
    )
    raw_limit = max(max(0, int(top_k)) * 4, max(0, int(top_k)))
    rows = db.execute(
        f"""
        SELECT c.node_id, c.chunk_id, c.chunk_index, c.content,
               bm25(memory_chunks_fts) AS fts_rank
        FROM memory_chunks_fts
        JOIN memory_chunks AS c
          ON c.chunk_id = memory_chunks_fts.chunk_id
         AND c.node_id = memory_chunks_fts.node_id
        JOIN memory_nodes AS n ON n.node_id = c.node_id
        WHERE memory_chunks_fts MATCH ? AND {filter_sql}
        ORDER BY fts_rank, c.node_id, c.chunk_index
        LIMIT ?
        """,
        [match_query, *filter_params, raw_limit],
    ).fetchall()
    results: List[ChunkSearchResult] = []
    seen_nodes: set[str] = set()
    for row in rows:
        node_id = str(row["node_id"])
        if node_id in seen_nodes:
            continue
        seen_nodes.add(node_id)
        results.append(
            ChunkSearchResult(
                node_id=node_id,
                chunk_id=str(row["chunk_id"]),
                score=abs(float(row["fts_rank"] or 0.0)),
                snippet=_make_snippet(str(row["content"] or ""), query),
                chunk_index=int(row["chunk_index"] or 0),
            )
        )
        if len(results) >= max(0, int(top_k)):
            break
    return results


def _like_match_score(content: str, terms: List[str]) -> float:
    lowered = str(content or "").casefold()
    score = 0.0
    for term in terms:
        normalized = term.casefold()
        if normalized:
            score += float(lowered.count(normalized))
    return score


def _chunk_like_rows(
    db: sqlite3.Connection,
    query: str,
    top_k: int,
    event_date: date | str | None,
    file_types: Optional[List[str]],
) -> List[ChunkSearchResult]:
    """Unicode-safe fallback which does not depend on an FTS tokenizer."""
    if not _table_exists(db, "memory_chunks"):
        raise RuntimeError("chunk table unavailable")
    terms = _fts_terms(query)
    if not terms:
        return []
    filter_sql, filter_params = _node_filter_sql(
        db,
        alias="n",
        event_date=event_date,
        file_types=file_types,
    )
    term_clauses: List[str] = []
    term_params: List[Any] = []
    for term in terms:
        term_clauses.append(
            "(instr(lower(COALESCE(c.content, '')), lower(?)) > 0 "
            "OR instr(lower(COALESCE(c.title, '')), lower(?)) > 0)"
        )
        term_params.extend((term, term))
    raw_limit = max(100, max(1, int(top_k)) * 16)
    rows = db.execute(
        f"""
        SELECT c.node_id, c.chunk_id, c.chunk_index, c.content
        FROM memory_chunks AS c
        JOIN memory_nodes AS n ON n.node_id = c.node_id
        WHERE ({' OR '.join(term_clauses)}) AND {filter_sql}
        LIMIT ?
        """,
        [*term_params, *filter_params, raw_limit],
    ).fetchall()
    ranked = sorted(
        rows,
        key=lambda row: (
            -_like_match_score(str(row["content"] or ""), terms),
            int(row["chunk_index"] or 0),
            str(row["node_id"]),
        ),
    )
    results: List[ChunkSearchResult] = []
    seen_nodes: set[str] = set()
    for row in ranked:
        node_id = str(row["node_id"])
        if node_id in seen_nodes:
            continue
        seen_nodes.add(node_id)
        results.append(
            ChunkSearchResult(
                node_id=node_id,
                chunk_id=str(row["chunk_id"]),
                score=_like_match_score(str(row["content"] or ""), terms),
                snippet=_make_snippet(str(row["content"] or ""), query),
                chunk_index=int(row["chunk_index"] or 0),
            )
        )
        if len(results) >= max(0, int(top_k)):
            break
    return results


def _legacy_like_rows(
    db: sqlite3.Connection,
    query: str,
    top_k: int,
    event_date: date | str | None,
    file_types: Optional[List[str]],
) -> List[ChunkSearchResult]:
    if not _table_exists(db, "memory_fts"):
        raise RuntimeError("legacy FTS unavailable")
    terms = _fts_terms(query)
    if not terms:
        return []
    filter_sql, filter_params = _node_filter_sql(
        db,
        alias="n",
        event_date=event_date,
        file_types=file_types,
    )
    term_clauses: List[str] = []
    term_params: List[Any] = []
    for term in terms:
        term_clauses.append(
            "(instr(lower(COALESCE(memory_fts.content, '')), lower(?)) > 0 "
            "OR instr(lower(COALESCE(memory_fts.title, '')), lower(?)) > 0)"
        )
        term_params.extend((term, term))
    rows = db.execute(
        f"""
        SELECT memory_fts.node_id, memory_fts.content
        FROM memory_fts
        JOIN memory_nodes AS n ON n.node_id = memory_fts.node_id
        WHERE ({' OR '.join(term_clauses)}) AND {filter_sql}
        LIMIT ?
        """,
        [*term_params, *filter_params, max(100, max(1, int(top_k)) * 16)],
    ).fetchall()
    ranked = sorted(
        rows,
        key=lambda row: (
            -_like_match_score(str(row["content"] or ""), terms),
            str(row["node_id"]),
        ),
    )
    return [
        ChunkSearchResult(
            node_id=str(row["node_id"]),
            chunk_id=f"legacy:{row['node_id']}",
            score=_like_match_score(str(row["content"] or ""), terms),
            snippet=_make_snippet(str(row["content"] or ""), query),
            chunk_index=0,
        )
        for row in ranked[: max(0, int(top_k))]
    ]


def _legacy_fts_rows(
    db: sqlite3.Connection,
    query: str,
    top_k: int,
    event_date: date | str | None,
    file_types: Optional[List[str]],
) -> List[ChunkSearchResult]:
    if not _table_exists(db, "memory_fts"):
        raise RuntimeError("legacy FTS unavailable")
    filter_sql, filter_params = _node_filter_sql(
        db,
        alias="n",
        event_date=event_date,
        file_types=file_types,
    )
    sql_row = db.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'memory_fts'"
    ).fetchone()
    is_virtual_fts = "using fts" in str(sql_row[0] if sql_row else "").lower()
    limit = max(0, int(top_k))
    if is_virtual_fts:
        match_query = _sanitize_fts_query(query)
        if not match_query:
            return []
        rows = db.execute(
            f"""
            SELECT memory_fts.node_id, memory_fts.content,
                   bm25(memory_fts) AS fts_rank
            FROM memory_fts
            JOIN memory_nodes AS n ON n.node_id = memory_fts.node_id
            WHERE memory_fts MATCH ? AND {filter_sql}
            ORDER BY fts_rank, memory_fts.node_id
            LIMIT ?
            """,
            [match_query, *filter_params, limit],
        ).fetchall()
    else:
        terms = _fts_terms(query)
        if not terms:
            return []
        term_clauses: List[str] = []
        term_params: List[Any] = []
        for term in terms:
            term_clauses.append(
                "(lower(COALESCE(memory_fts.title, '')) LIKE ? "
                "OR lower(COALESCE(memory_fts.content, '')) LIKE ?)"
            )
            pattern = f"%{term.casefold()}%"
            term_params.extend((pattern, pattern))
        rows = db.execute(
            f"""
            SELECT memory_fts.node_id, memory_fts.content, 0.0 AS fts_rank
            FROM memory_fts
            JOIN memory_nodes AS n ON n.node_id = memory_fts.node_id
            WHERE ({' OR '.join(term_clauses)}) AND {filter_sql}
            ORDER BY memory_fts.node_id
            LIMIT ?
            """,
            [*term_params, *filter_params, limit],
        ).fetchall()
    return [
        ChunkSearchResult(
            node_id=str(row["node_id"]),
            chunk_id=f"legacy:{row['node_id']}",
            score=abs(float(row["fts_rank"] or 0.0)),
            snippet=_make_snippet(str(row["content"] or ""), query),
            chunk_index=0,
        )
        for row in rows
    ]


async def _chunk_fts_search_outcome(
    db: sqlite3.Connection,
    query: str,
    top_k: int = 10,
    event_date: date | str | None = None,
    file_types: Optional[List[str]] = None,
) -> _FtsSearchOutcome:
    primary_error: BaseException | None = None
    primary_succeeded = False
    try:
        primary = await _async_db_read(
            _chunk_fts_rows,
            db,
            query,
            top_k,
            event_date,
            file_types,
        )
        primary_succeeded = True
        if primary:
            return _FtsSearchOutcome(results=primary, success=True)
    except Exception as exc:
        primary_error = exc

    # A unicode61 index cannot tokenize arbitrary Chinese text reliably.  The
    # bounded literal fallback is only used after FTS misses or is unavailable.
    literal_succeeded = False
    try:
        chunk_fallback = await _async_db_read(
            _chunk_like_rows,
            db,
            query,
            top_k,
            event_date,
            file_types,
        )
        literal_succeeded = True
        if chunk_fallback:
            return _FtsSearchOutcome(
                results=chunk_fallback,
                success=True,
                degraded=True,
                error_type=type(primary_error).__name__ if primary_error else "TokenizerFallback",
            )
    except Exception as exc:
        if primary_error is None:
            primary_error = exc

    try:
        legacy = await _async_db_read(
            _legacy_fts_rows,
            db,
            query,
            top_k,
            event_date,
            file_types,
        )
        if not legacy:
            legacy = await _async_db_read(
                _legacy_like_rows,
                db,
                query,
                top_k,
                event_date,
                file_types,
            )
        if legacy:
            return _FtsSearchOutcome(
                results=legacy,
                success=True,
                degraded=True,
                error_type=type(primary_error).__name__ if primary_error else "LegacyFtsFallback",
            )
    except Exception as fallback_error:
        if not primary_succeeded and not literal_succeeded:
            error = primary_error or fallback_error
            logger.warning(f"全文检索降级失败: {type(error).__name__}")
            return _FtsSearchOutcome(
                results=[],
                success=False,
                degraded=True,
                error_type=type(error).__name__,
            )

    return _FtsSearchOutcome(
        results=[],
        success=True,
        degraded=primary_error is not None,
        error_type=type(primary_error).__name__ if primary_error else None,
    )


async def chunk_fts_search(
    db: sqlite3.Connection,
    query: str,
    top_k: int = 10,
    event_date: date | str | None = None,
    file_types: Optional[List[str]] = None,
) -> List[ChunkSearchResult]:
    """Search chunk FTS first and fall back to the legacy node FTS."""
    outcome = await _chunk_fts_search_outcome(
        db,
        query,
        top_k,
        event_date,
        file_types,
    )
    return outcome.results


async def fts_search(
    db: sqlite3.Connection,
    query: str,
    top_k: int = 10,
) -> List[Tuple[str, float]]:
    """全文搜索，保留旧 ``(node_id, score)`` API。"""
    results = await chunk_fts_search(db, query, top_k)
    return [
        (result.node_id, min(abs(float(result.score)) / 10.0, 1.0))
        for result in results
    ]


# ============================================================
# RRF 融合
# ============================================================


def rrf_fusion(
    fts_results: List[Tuple[str, float]],
    vector_results: List[Tuple[str, float]],
    k: int = RRF_K,
) -> List[Tuple[str, float]]:
    """Reciprocal Rank Fusion 融合。

    Args:
        fts_results: FTS 检索结果
        vector_results: 向量检索结果
        k: RRF 参数

    Returns:
        融合后的 (node_id, score) 列表
    """
    scores: Dict[str, float] = {}

    # FTS 结果
    for rank, (node_id, _) in enumerate(fts_results):
        scores[node_id] = scores.get(node_id, 0) + 1.0 / (k + rank + 1)

    # Vector 结果
    for rank, (node_id, _) in enumerate(vector_results):
        scores[node_id] = scores.get(node_id, 0) + 1.0 / (k + rank + 1)

    # 排序
    return sorted(scores.items(), key=lambda x: -x[1])


# ============================================================
# 激活扩散
# ============================================================


def _normalize_allowed_edge_types(
    allowed_edge_types: Optional[Iterable[EdgeType | str]],
) -> set[EdgeType]:
    """规范化扩散允许的边类型；默认只允许显式关系。"""
    if allowed_edge_types is None:
        return set(EXPLICIT_RELATION_EDGE_TYPES)

    if isinstance(allowed_edge_types, (EdgeType, str)):
        values: Iterable[EdgeType | str] = [allowed_edge_types]
    else:
        values = allowed_edge_types

    normalized: set[EdgeType] = set()
    for value in values:
        if isinstance(value, EdgeType):
            normalized.add(value)
            continue
        try:
            normalized.add(EdgeType(str(value).strip().lower()))
        except ValueError:
            logger.debug(f"忽略未知扩散边类型: {value}")
    return normalized


def _read_edges_from(
    db: sqlite3.Connection,
    node_id: str,
    min_weight: float,
) -> List[Any]:
    if not _table_exists(db, "memory_edges"):
        return []
    rows = db.execute(
        "SELECT * FROM memory_edges WHERE source_id = ? AND weight >= ? "
        "ORDER BY weight DESC",
        (node_id, min_weight),
    ).fetchall()
    return [row_to_edge(row) for row in rows]


async def _get_edges_from_readonly(
    db: sqlite3.Connection,
    node_id: str,
    min_weight: float,
) -> List[Any]:
    return await _async_db_read(_read_edges_from, db, node_id, min_weight)


def _read_traversable_node_ids(db: sqlite3.Connection) -> set[str]:
    """Load graph nodes that are safe for a read-only retrieval traversal."""
    if not _table_exists(db, "memory_nodes"):
        return set()
    register_indexed_path_sql_function(db)
    columns = _table_columns(db, "memory_nodes")
    node_type_expr = "COALESCE(node_type, 'file')" if "node_type" in columns else "'file'"
    deleted_expr = "COALESCE(is_deleted, 0)" if "is_deleted" in columns else "0"
    eligibility_sql, eligibility_params = eligible_document_path_sql("file_path")
    rows = db.execute(
        "SELECT node_id, "
        f"{node_type_expr} AS node_type, file_path, {deleted_expr} AS is_deleted "
        "FROM memory_nodes "
        f"WHERE {deleted_expr} = 0 "
        f"AND (lower({node_type_expr}) <> ? OR {eligibility_sql})",
        [NodeType.FILE.value, *eligibility_params],
    ).fetchall()
    visible_ids: set[str] = set()
    for row in rows:
        node_type = str(row["node_type"] or NodeType.FILE.value).lower()
        if node_type == NodeType.CONCEPT.value:
            visible_ids.add(str(row["node_id"]))
        elif (
            node_type == NodeType.FILE.value
            and is_eligible_indexed_document_path(row["file_path"])
        ):
            visible_ids.add(str(row["node_id"]))
    return visible_ids


async def spread_activation(
    db: sqlite3.Connection,
    seed_ids: List[str],
    max_depth: int = 2,
    max_results: int = 10,
    spread_decay: float = SPREAD_DECAY,
    spread_threshold: float = SPREAD_THRESHOLD,
    allowed_edge_types: Optional[Iterable[EdgeType | str]] = None,
) -> List[Tuple[str, float, List[str], str]]:
    """激活扩散联想。

    Args:
        db: SQLite 数据库连接
        seed_ids: 种子节点 ID 列表
        max_depth: 最大扩散深度
        max_results: 最大返回数量
        spread_decay: 扩散衰减系数
        spread_threshold: 扩散阈值
        allowed_edge_types: 允许参与扩散的边类型；默认仅显式关系。

    Returns:
        [(node_id, activation_score, path, reason), ...]
    """
    try:
        normalized_depth = max(0, int(max_depth))
        normalized_results = max(0, int(max_results))
    except (TypeError, ValueError) as exc:
        raise ValueError("max_depth 和 max_results 必须是整数") from exc

    normalized_seed_ids = list(
        dict.fromkeys(
            str(seed_id)
            for seed_id in (seed_ids or [])
            if str(seed_id or "").strip()
        )
    )
    allowed_types = _normalize_allowed_edge_types(allowed_edge_types)
    if (
        not normalized_seed_ids
        or not allowed_types
        or normalized_depth == 0
        or normalized_results == 0
    ):
        return []
    traversable_node_ids = await _async_db_read(_read_traversable_node_ids, db)
    normalized_seed_ids = [
        node_id for node_id in normalized_seed_ids if node_id in traversable_node_ids
    ]
    if not normalized_seed_ids:
        return []

    activation: Dict[str, float] = {seed: 1.0 for seed in normalized_seed_ids}
    paths: Dict[str, List[str]] = {seed: [seed] for seed in normalized_seed_ids}
    reasons: Dict[str, str] = {}
    frontier: List[Tuple[str, float, List[str]]] = [
        (seed, 1.0, [seed]) for seed in normalized_seed_ids
    ]

    for _ in range(normalized_depth):
        next_candidates: Dict[str, Tuple[float, List[str], str]] = {}

        for node_id, current_activation, current_path in frontier:
            edges = await _get_edges_from_readonly(db, node_id, spread_threshold)

            for edge in edges:
                if edge.edge_type not in allowed_types:
                    continue

                neighbor = edge.target_id
                if neighbor not in traversable_node_ids or neighbor in current_path:
                    continue

                propagated = current_activation * edge.weight * spread_decay
                if propagated < spread_threshold:
                    continue

                previous_score = activation.get(neighbor, float("-inf"))
                candidate = next_candidates.get(neighbor)
                candidate_score = candidate[0] if candidate else float("-inf")
                if propagated <= max(previous_score, candidate_score):
                    continue

                next_candidates[neighbor] = (
                    propagated,
                    current_path + [neighbor],
                    f"{edge.edge_type.value}: {edge.reason}",
                )

        if not next_candidates:
            break

        frontier = []
        for node_id, (score, path, reason) in next_candidates.items():
            activation[node_id] = score
            paths[node_id] = path
            reasons[node_id] = reason
            frontier.append((node_id, score, path))

    # 移除种子节点，返回联想到的节点
    for seed in normalized_seed_ids:
        activation.pop(seed, None)

    sorted_items = sorted(activation.items(), key=lambda item: (-item[1], item[0]))
    return [
        (node_id, score, paths.get(node_id, []), reasons.get(node_id, ""))
        for node_id, score in sorted_items[:normalized_results]
    ]


# ============================================================
# 辅助函数
# ============================================================


async def filter_existing_scores(
    db: sqlite3.Connection,
    scores: List[Tuple[str, float]],
) -> Tuple[List[Tuple[str, float]], List[str]]:
    """仅保留节点表中存在的结果。

    Args:
        db: SQLite 数据库连接
        scores: (node_id, score) 列表

    Returns:
        (filtered_scores, stale_node_ids)
    """
    if not scores:
        return [], []

    ordered_ids: List[str] = []
    seen = set()
    for node_id, _ in scores:
        if node_id not in seen:
            ordered_ids.append(node_id)
            seen.add(node_id)

    placeholders = ",".join("?" for _ in ordered_ids)

    def _do_db_work() -> set:
        cursor = db.cursor()
        register_indexed_path_sql_function(db)
        columns = _table_columns(db, "memory_nodes")
        clauses = [f"node_id IN ({placeholders})"]
        if "node_type" in columns:
            clauses.append("lower(COALESCE(node_type, 'file')) = 'file'")
        if "is_deleted" in columns:
            clauses.append("COALESCE(is_deleted, 0) = 0")
        if "file_path" in columns:
            clauses.extend(["file_path IS NOT NULL", "TRIM(file_path) <> ''"])
            eligibility_sql, eligibility_params = eligible_document_path_sql("file_path")
            clauses.append(eligibility_sql)
        else:
            eligibility_params = []
        cursor.execute(
            f"SELECT node_id FROM memory_nodes WHERE {' AND '.join(clauses)}",
            [*ordered_ids, *eligibility_params],
        )
        return {row["node_id"] for row in cursor.fetchall()}

    existing_ids = await _async_db_read(_do_db_work)
    stale_ids = [node_id for node_id in ordered_ids if node_id not in existing_ids]
    filtered_scores = [(node_id, score) for node_id, score in scores if node_id in existing_ids]
    return filtered_scores, stale_ids


async def get_node_by_id(db: sqlite3.Connection, node_id: str) -> Optional[MemoryNode]:
    """根据 ID 获取节点。

    Args:
        db: SQLite 数据库连接
        node_id: 节点 ID

    Returns:
        MemoryNode 或 None
    """
    def _do_db_work() -> Optional[MemoryNode]:
        cursor = db.cursor()
        cursor.execute("SELECT * FROM memory_nodes WHERE node_id = ?", (node_id,))
        row = cursor.fetchone()
        if row is None:
            return None
        if "is_deleted" in row.keys() and bool(row["is_deleted"]):
            return None
        node = row_to_node(row)
        if (
            node.node_type == NodeType.FILE
            and not is_eligible_indexed_document_path(node.file_path)
        ):
            return None
        return node

    return await _async_db_read(_do_db_work)


async def get_snippet(db: sqlite3.Connection, node_id: str) -> str:
    """获取节点内容摘要，优先读取新分块表。"""

    def _do_db_work() -> str:
        if _table_exists(db, "memory_chunks"):
            row = db.execute(
                "SELECT content FROM memory_chunks WHERE node_id = ? "
                "ORDER BY chunk_index LIMIT 1",
                (node_id,),
            ).fetchone()
            if row:
                return _make_snippet(str(row["content"] or ""), "")
        if _table_exists(db, "memory_fts"):
            row = db.execute(
                "SELECT content FROM memory_fts WHERE node_id = ? LIMIT 1",
                (node_id,),
            ).fetchone()
            if row:
                return _make_snippet(str(row["content"] or ""), "")
        return ""

    return await _async_db_read(_do_db_work)


@dataclass(frozen=True)
class LineageNodeView:
    """组装记忆包时需要的节点视图。

    只保留调用方真正读取的字段。血缘遍历一次可能牵出上百个节点，把完整的
    :class:`MemoryNode` 全列取回纯属浪费。
    """

    node_id: str
    file_path: str
    title: str
    snippet: str


async def get_lineage_node_views(
    db: sqlite3.Connection,
    node_ids: Iterable[str],
) -> Dict[str, LineageNodeView]:
    """批量取回可见节点及其摘要。

    可见性判定与 :func:`get_node_by_id` 完全一致：已删除的节点、以及路径不再
    合规的文件节点都不会出现在结果里；摘要的生成方式与 :func:`get_snippet`
    一致。区别只在往返次数——原先每条血缘边要两次查询，现在整批一次。

    Args:
        db: SQLite 连接。
        node_ids: 待取回的节点 ID，重复项自动去重。

    Returns:
        Dict[str, LineageNodeView]: node_id 到视图的映射。不可见的节点不在其中，
        调用方据此判断该节点应被跳过。
    """

    def _do_db_work() -> Dict[str, LineageNodeView]:
        views: Dict[str, LineageNodeView] = {}
        for node_id, node in _load_nodes_by_ids(db, node_ids).items():
            if node.is_deleted:
                continue
            if node.node_type == NodeType.FILE.value and not is_eligible_indexed_document_path(
                node.file_path
            ):
                continue
            views[node_id] = LineageNodeView(
                node_id=node.node_id,
                file_path=node.file_path,
                title=node.title,
                snippet=_make_snippet(node.preview_content, ""),
            )
        return views

    return await _async_db_read(_do_db_work)


def _load_nodes_by_ids(
    db: sqlite3.Connection,
    node_ids: Iterable[str],
) -> Dict[str, _LoadedNode]:
    ordered_ids = list(dict.fromkeys(str(node_id) for node_id in node_ids if node_id))
    if not ordered_ids:
        return {}
    columns = _table_columns(db, "memory_nodes")
    placeholders = ",".join("?" for _ in ordered_ids)
    event_expr = "n.event_date" if "event_date" in columns else "NULL"
    deleted_expr = "COALESCE(n.is_deleted, 0)" if "is_deleted" in columns else "0"
    mtime_expr = "n.source_mtime" if "source_mtime" in columns else "NULL"
    chunk_expr = (
        "(SELECT c.content FROM memory_chunks AS c "
        "WHERE c.node_id = n.node_id ORDER BY c.chunk_index LIMIT 1)"
        if _table_exists(db, "memory_chunks")
        else "NULL"
    )
    fts_expr = (
        "(SELECT f.content FROM memory_fts AS f WHERE f.node_id = n.node_id LIMIT 1)"
        if _table_exists(db, "memory_fts")
        else "NULL"
    )
    node_type_expr = "n.node_type" if "node_type" in columns else "'file'"
    rows = db.execute(
        f"""
        SELECT n.node_id, {node_type_expr} AS node_type, n.file_path, n.title,
               {event_expr} AS event_date, {deleted_expr} AS is_deleted,
               {mtime_expr} AS source_mtime, n.created_at,
               COALESCE({chunk_expr}, {fts_expr}, '') AS preview_content
        FROM memory_nodes AS n
        WHERE n.node_id IN ({placeholders})
        """,
        ordered_ids,
    ).fetchall()
    return {
        str(row["node_id"]): _LoadedNode(
            node_id=str(row["node_id"]),
            node_type=str(row["node_type"] or NodeType.FILE.value).lower(),
            file_path=str(row["file_path"] or ""),
            title=str(row["title"] or ""),
            event_date=str(row["event_date"]) if row["event_date"] else None,
            is_deleted=bool(row["is_deleted"]),
            source_mtime=float(row["source_mtime"]) if row["source_mtime"] is not None else None,
            created_at=float(row["created_at"] or 0.0),
            preview_content=str(row["preview_content"] or ""),
        )
        for row in rows
    }


def _coerce_search_date(now: Any) -> date | None:
    if callable(now):
        now = now()
    if isinstance(now, datetime):
        return now.date()
    if isinstance(now, date):
        return now
    return None


def _node_event_date(node: _LoadedNode) -> date | None:
    if not node.event_date:
        return None
    try:
        return date.fromisoformat(node.event_date[:10])
    except ValueError:
        return None


def _node_effective_date(node: _LoadedNode) -> date | None:
    event_date = _node_event_date(node)
    if event_date is not None:
        return event_date
    timestamp = node.source_mtime if node.source_mtime is not None else node.created_at
    if timestamp <= 0:
        return None
    try:
        return datetime.fromtimestamp(timestamp).date()
    except (OverflowError, OSError, ValueError):
        return None


def _workspace_file_exists(workspace_path: str | Path | None, file_path: str) -> bool:
    if workspace_path is None:
        return True
    return assess_workspace_document(workspace_path, file_path).eligible


def _node_matches_filters(
    node: _LoadedNode,
    *,
    file_types: Optional[List[str]],
    explicit_date: date | None,
    cutoff_date: date | None,
    workspace_path: str | Path | None,
) -> bool:
    if node.node_type != NodeType.FILE.value or node.is_deleted or not node.file_path:
        return False
    if not is_eligible_indexed_document_path(node.file_path):
        return False
    if not _matches_file_type(node.file_path, file_types):
        return False
    if not _workspace_file_exists(workspace_path, node.file_path):
        return False
    if explicit_date is not None:
        if _node_event_date(node) != explicit_date:
            return False
    elif cutoff_date is not None:
        effective_date = _node_effective_date(node)
        if effective_date is None or effective_date < cutoff_date:
            return False
    return True


async def filter_results(
    db: sqlite3.Connection,
    results: List[Tuple[str, float]],
    file_types: Optional[List[str]] = None,
    time_range_days: int = 0,
    *,
    now: date | datetime | None = None,
    workspace_path: str | Path | None = None,
    event_date: date | None = None,
) -> List[Tuple[str, float]]:
    """过滤旧式分数列表；保留兼容 API 并使用严格路径/日期规则。"""
    current_date = _coerce_search_date(now)
    if current_date is None and time_range_days > 0:
        current_date = datetime.now().astimezone().date()
    cutoff_date = (
        current_date - timedelta(days=max(0, int(time_range_days)))
        if current_date is not None and time_range_days > 0 and event_date is None
        else None
    )
    nodes = await _async_db_read(
        _load_nodes_by_ids,
        db,
        (node_id for node_id, _ in results),
    )
    return [
        (node_id, score)
        for node_id, score in results
        if node_id in nodes
        and _node_matches_filters(
            nodes[node_id],
            file_types=file_types,
            explicit_date=event_date,
            cutoff_date=cutoff_date,
            workspace_path=workspace_path,
        )
    ]


# ============================================================
# 混合检索
# ============================================================


async def search_memory_detailed(
    db: sqlite3.Connection,
    query: str,
    collection: Any,
    top_k: int = 5,
    enable_association: bool = True,
    file_types: Optional[List[str]] = None,
    time_range_days: int = 0,
    emit_visual_event: Any = None,
    increment_access_func: Any = None,
    reinforce_coactivated_func: Any = None,
    *,
    now: date | datetime | Any | None = None,
    workspace_path: str | Path | None = None,
    chunk_collection: Any = None,
) -> DetailedSearchResult:
    """并行执行 chunk FTS/向量召回，并返回只读诊断信息。"""
    del increment_access_func, reinforce_coactivated_func
    total_started = time.perf_counter()
    diagnostics = SearchDiagnostics()
    query_meta = _query_metadata(query)
    resolved_now = now() if callable(now) else now
    explicit_date = parse_temporal_date(str(query or ""), now=resolved_now)
    current_date = _coerce_search_date(resolved_now)
    if current_date is None and time_range_days > 0:
        current_date = datetime.now().astimezone().date()
    cutoff_date = (
        current_date - timedelta(days=max(0, int(time_range_days)))
        if explicit_date is None and current_date is not None and time_range_days > 0
        else None
    )
    if emit_visual_event:
        emit_visual_event(
            "memory.search.started",
            {**query_meta, "top_k": top_k, "enable_association": enable_association},
        )

    async def _run_fts() -> tuple[_FtsSearchOutcome, float]:
        started = time.perf_counter()
        outcome = await _chunk_fts_search_outcome(
            db, query, max(0, int(top_k)) * 4, explicit_date, file_types
        )
        return outcome, time.perf_counter() - started

    async def _run_vector() -> tuple[_VectorSearchOutcome, float]:
        started = time.perf_counter()
        outcome = await _vector_search_outcome(
            query,
            collection,
            max(0, int(top_k)) * 4,
            db=db,
            chunk_collection=chunk_collection,
            validate_db=False,
            defer_chunk_validation=True,
        )
        return outcome, time.perf_counter() - started

    gathered = await asyncio.gather(_run_fts(), _run_vector(), return_exceptions=True)
    fts_outcome = _FtsSearchOutcome([], False, True, "UnknownError")
    vector_outcome = _VectorSearchOutcome([], False, True, "UnknownError")
    for phase, value in zip(("fts", "vector"), gathered):
        if isinstance(value, BaseException):
            diagnostics.degraded = True
            diagnostics.error_types[phase] = type(value).__name__
            diagnostics.errors[phase] = _short_error(value)
            continue
        outcome, elapsed = value
        diagnostics.phase_timings[phase] = elapsed
        if phase == "fts":
            fts_outcome = outcome
        else:
            vector_outcome = outcome

    validation_started = time.perf_counter()
    vector_outcome = await _resolve_deferred_vector_outcome(db, vector_outcome)
    diagnostics.phase_timings["vector_validation"] = time.perf_counter() - validation_started

    diagnostics.fts_success = fts_outcome.success
    diagnostics.vector_success = vector_outcome.success
    diagnostics.fts_candidate_count = len(fts_outcome.results)
    diagnostics.vector_candidate_count = len(vector_outcome.results)
    diagnostics.degraded = (
        diagnostics.degraded or fts_outcome.degraded or vector_outcome.degraded
        or not fts_outcome.success or not vector_outcome.success
    )
    for phase, outcome in (("fts", fts_outcome), ("vector", vector_outcome)):
        if outcome.error_type:
            diagnostics.error_types[phase] = outcome.error_type
        if outcome.error:
            diagnostics.errors[phase] = outcome.error

    fts_scores = [(item.node_id, item.score) for item in fts_outcome.results]
    fused_scores = rrf_fusion(fts_scores, vector_outcome.results)
    best_chunks = {item.node_id: item for item in fts_outcome.results}
    load_started = time.perf_counter()
    candidate_nodes = await _async_db_read(
        _load_nodes_by_ids, db, (node_id for node_id, _ in fused_scores)
    )
    diagnostics.phase_timings["load"] = time.perf_counter() - load_started
    filtered_scores = [
        (node_id, score)
        for node_id, score in fused_scores
        if node_id in candidate_nodes
        and _node_matches_filters(
            candidate_nodes[node_id],
            file_types=file_types,
            explicit_date=explicit_date,
            cutoff_date=cutoff_date,
            workspace_path=workspace_path,
        )
    ]
    seeds = filtered_scores[: max(0, int(top_k))]
    seed_ids = [node_id for node_id, _ in seeds]

    association_started = time.perf_counter()
    associated: List[Tuple[str, float, List[str], str]] = []
    if enable_association and seed_ids:
        associated = await spread_activation(
            db,
            seed_ids,
            max_depth=2,
            max_results=max(0, int(top_k)) * 2,
            allowed_edge_types=EXPLICIT_RELATION_EDGE_TYPES,
        )
    diagnostics.phase_timings["association"] = time.perf_counter() - association_started
    associated_nodes = await _async_db_read(
        _load_nodes_by_ids, db, (node_id for node_id, *_ in associated)
    )

    results: List[SearchResult] = []
    seen_paths: set[str] = set()
    seed_payload: List[Dict[str, Any]] = []
    associated_payload: List[Dict[str, Any]] = []
    for node_id, score in seeds:
        node = candidate_nodes[node_id]
        chunk = best_chunks.get(node_id)
        results.append(
            SearchResult(
                file_path=node.file_path,
                title=node.title,
                snippet=chunk.snippet if chunk else _make_snippet(node.preview_content, query),
                relevance=score,
                source="direct",
                score_kind="rank",
            )
        )
        seen_paths.add(node.file_path)
        seed_payload.append(
            {"id": node_id, "title": node.title, "path": node.file_path, "score": score}
        )

    for node_id, score, path, reason in associated:
        node = associated_nodes.get(node_id)
        if node is None or node.file_path in seen_paths:
            continue
        if not _node_matches_filters(
            node,
            file_types=file_types,
            explicit_date=explicit_date,
            cutoff_date=cutoff_date,
            workspace_path=workspace_path,
        ):
            continue
        results.append(
            SearchResult(
                file_path=node.file_path,
                title=node.title,
                snippet=_make_snippet(node.preview_content, query),
                relevance=score * 0.8,
                source="associated",
                association_path=path,
                association_reason=reason,
                score_kind="rank",
            )
        )
        seen_paths.add(node.file_path)
        associated_payload.append(
            {
                "id": node_id,
                "title": node.title,
                "path": node.file_path,
                "score": score,
                "association_path": path,
                "association_reason": reason,
            }
        )

    diagnostics.phase_timings["total"] = time.perf_counter() - total_started
    if emit_visual_event:
        emit_visual_event(
            "memory.search.seeds",
            {**query_meta, "seed_ids": seed_ids, "results": seed_payload},
        )
        if associated_payload:
            emit_visual_event(
                "memory.activation.spread",
                {**query_meta, "seed_ids": seed_ids, "results": associated_payload},
            )
        emit_visual_event(
            "memory.search.finished",
            {
                **query_meta,
                "total_found": len(results),
                "degraded": diagnostics.degraded,
                "error_types": diagnostics.error_types,
            },
        )
    return DetailedSearchResult(
        results=results[: max(0, int(top_k)) * 2], diagnostics=diagnostics
    )


async def search_memory(
    db: sqlite3.Connection,
    query: str,
    collection: Any,
    top_k: int = 5,
    enable_association: bool = True,
    file_types: Optional[List[str]] = None,
    time_range_days: int = 0,
    emit_visual_event: Any = None,
    increment_access_func: Any = None,
    reinforce_coactivated_func: Any = None,
    *,
    now: date | datetime | Any | None = None,
    workspace_path: str | Path | None = None,
    chunk_collection: Any = None,
) -> List[SearchResult]:
    """混合检索 + 联想（只读），保留旧列表返回 API。"""
    detailed = await search_memory_detailed(
        db=db,
        query=query,
        collection=collection,
        top_k=top_k,
        enable_association=enable_association,
        file_types=file_types,
        time_range_days=time_range_days,
        emit_visual_event=emit_visual_event,
        increment_access_func=increment_access_func,
        reinforce_coactivated_func=reinforce_coactivated_func,
        now=now,
        workspace_path=workspace_path,
        chunk_collection=chunk_collection,
    )
    return detailed.results
