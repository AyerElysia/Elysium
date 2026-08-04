"""SQLite adapters for the shared Life Memory ports."""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Callable, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from ...memory.decay import (
    apply_decay,
    dream_walk,
    get_stats,
    list_dream_candidate_nodes,
    list_random_file_nodes,
    prune_weak_edges,
)
from ...memory.edges import (
    EdgeType,
    MemoryEdge,
    create_or_update_edge,
    delete_edge,
    get_edges_from,
    get_edges_to,
    reinforce_coactivated,
)
from ...memory.epistemic import (
    ClaimEvidence,
    ClaimSearchResult,
    ClaimState,
    CurrentFactProjection,
    EpistemicConflict,
    MemoryAuditEntry,
    MemoryBelief,
    MemoryClaim,
    MemoryDisposition,
    MemoryStateEvent,
    RetrievalEpisode,
    RetrievalExposure,
    RetrievalFeedback,
    RetrievalPlasticity,
    append_belief,
    append_claim,
    append_claim_evidence,
    append_conflict,
    append_retrieval_episode,
    append_retrieval_exposure,
    append_retrieval_feedback,
    append_state_event,
    build_memory_audit_trail,
    get_claim_state,
    get_memory_disposition,
    get_retrieval_plasticity,
    list_claim_evidence,
    list_claim_states,
    list_state_events,
    project_current_facts,
    search_epistemic_claims,
)
from ...memory.experience import (
    ExperienceAppendReport,
    ExperienceRecord,
    WitnessMemory,
    append_experiences_detailed,
    get_witness_by_projection_path,
    get_witness_state,
    insert_witness_memory,
    list_experiences_after,
    list_pending_witness_projections,
    mark_witness_projection,
    migrate_legacy_witness,
    migration_exists,
    record_witness_migration,
    search_witness_memories,
    update_witness_state,
)
from ...memory.indexing import (
    ACTIVE_CHUNK_STATE_KEY,
    ChunkIndexState,
    DocumentIndexResult,
    IndexJob,
    claim_index_jobs,
    delete_document_rows,
    enqueue_index_job,
    list_index_jobs,
    move_document_rows,
    read_active_chunk_index_state,
    set_index_job_status,
    transaction,
    upsert_document_rows,
)
from ...memory.lineage import (
    MemoryCorrection,
    insert_memory_correction,
    list_memory_corrections,
)
from ...memory.living import (
    ArtifactHead,
    AssociationEvidence,
    AssociationSelection,
    CoRecallEvent,
    InterpretationSearchResult,
    InterpretationSource,
    MemoryArtifactVersion,
    MemoryDerivation,
    MemoryInterpretation,
    RecallEpisode,
    RecallEvent,
    SemanticRelation,
    append_artifact_version,
    append_corecall_event,
    append_interpretation,
    append_recall_events,
    append_semantic_relation,
    begin_recall_episode,
    choose_association_neighbours,
    get_artifact_head_state,
    get_interpretation,
    list_artifact_heads,
    list_artifact_history,
    list_association_evidence,
    list_interpretations,
    list_semantic_relations,
    rebuild_association_projection,
    search_interpretations,
)
from ...memory.nodes import (
    MemoryNode,
    generate_file_node_id,
    get_node_by_file_path,
    get_or_create_file_node,
    increment_access,
    row_to_node,
)
from ...memory.search import (
    DetailedSearchResult,
    LineageNodeView,
    filter_existing_scores,
    fts_search,
    get_lineage_node_views,
    get_node_by_id,
    get_snippet,
    search_memory_detailed,
    spread_activation,
    vector_search,
)
from ...memory.sqlite_runtime import run_db
from ...memory.worker import (
    IndexWorkerReport,
    process_index_jobs,
)
from ...memory.worker import (
    consume_vector_tombstones as consume_sqlite_vector_tombstones,
)
from ..authority import FileAuthorityRegistry
from ..contracts import StorageBackendRuntime
from ..models import BackendKind, StorageAvailability
from .contracts import MemoryStorageBundle

ConnectionProvider = Callable[[], sqlite3.Connection]


def _list_active_file_nodes(db: sqlite3.Connection) -> list[MemoryNode]:
    rows = db.execute(
        "SELECT * FROM memory_nodes WHERE node_type = 'file' "
        "AND COALESCE(is_deleted, 0) = 0 ORDER BY file_path, node_id"
    ).fetchall()
    return [row_to_node(row) for row in rows]


def _invalidate_vector_projection(db: sqlite3.Connection) -> int:
    rows = db.execute(
        "SELECT node_id, content_hash FROM memory_nodes "
        "WHERE node_type = 'file' AND COALESCE(is_deleted, 0) = 0"
    ).fetchall()
    with transaction(db):
        db.execute(
            "DELETE FROM memory_index_state WHERE state_key = ?",
            (ACTIVE_CHUNK_STATE_KEY,),
        )
        db.execute(
            "UPDATE memory_nodes SET embedding_synced = 0 "
            "WHERE node_type = 'file' AND COALESCE(is_deleted, 0) = 0"
        )
        count = 0
        for row in rows:
            content_hash = str(row["content_hash"] or "")
            if not content_hash:
                continue
            enqueue_index_job(db, str(row["node_id"]), content_hash)
            count += 1
    return count


def _mark_documents_deleted(
    db: sqlite3.Connection,
    node_ids: Sequence[str],
) -> int:
    identifiers = tuple(dict.fromkeys(str(node_id).strip() for node_id in node_ids))
    identifiers = tuple(node_id for node_id in identifiers if node_id)
    if not identifiers:
        return 0
    changed = 0
    with transaction(db) as cursor:
        for node_id in identifiers:
            cursor.execute(
                "UPDATE memory_nodes SET is_deleted = 1 "
                "WHERE node_id = ? AND COALESCE(is_deleted, 0) = 0",
                (node_id,),
            )
            changed += max(0, int(cursor.rowcount))
    return changed


def _prune_orphan_edges(db: sqlite3.Connection) -> int:
    with transaction(db) as cursor:
        cursor.execute(
            """DELETE FROM memory_edges
            WHERE source_id NOT IN (SELECT node_id FROM memory_nodes)
               OR target_id NOT IN (SELECT node_id FROM memory_nodes)"""
        )
        return max(0, int(cursor.rowcount))


def _list_artifact_head_records(
    db: sqlite3.Connection,
) -> list[tuple[MemoryArtifactVersion, ArtifactHead]]:
    versions = list_artifact_heads(db)
    records: list[tuple[MemoryArtifactVersion, ArtifactHead]] = []
    for logical_key, version in versions.items():
        head = get_artifact_head_state(db, logical_key)
        if head is not None:
            records.append((version, head))
    return records


class _LocalPort:
    def __init__(
        self,
        connection: ConnectionProvider,
        lock: asyncio.Lock,
        runtime: StorageBackendRuntime | None = None,
    ) -> None:
        self._connection = connection
        self._lock = lock
        self._runtime = runtime

    def _db(self) -> sqlite3.Connection:
        db = self._connection()
        if not isinstance(db, sqlite3.Connection):
            raise TypeError("local memory backend has no SQLite connection")
        return db

    async def availability(self) -> StorageAvailability:
        try:
            await run_db(lambda: self._db().execute("SELECT 1").fetchone())
        except (RuntimeError, sqlite3.Error):
            return StorageAvailability.FAILED
        return StorageAvailability.HEALTHY

    @asynccontextmanager
    async def _write_scope(self):
        runtime = self._runtime
        if runtime is None:
            yield
            return
        registry = runtime.authority_registry
        token = runtime.authority_token
        if not isinstance(registry, FileAuthorityRegistry) or token is None:
            raise RuntimeError("local Memory runtime has no file authority fence")
        async with registry.fenced(token):
            yield


class LocalDocumentIndexProjection(_LocalPort):
    async def upsert_document(
        self,
        path: str,
        content: str,
        title: str = "",
        source_mtime: float | None = None,
        *,
        max_chars: int | None = None,
        overlap_chars: int | None = None,
    ) -> DocumentIndexResult:
        kwargs: dict[str, Any] = {}
        if max_chars is not None:
            kwargs["max_chars"] = max_chars
        if overlap_chars is not None:
            kwargs["overlap_chars"] = overlap_chars
        async with self._write_scope():
            return await run_db(
                upsert_document_rows,
                self._db(),
                path,
                content,
                title,
                source_mtime,
                **kwargs,
            )

    async def delete_document(self, path: str) -> bool:
        async with self._write_scope():
            return await run_db(delete_document_rows, self._db(), path)

    async def move_document(self, old_path: str, new_path: str) -> bool:
        async with self._write_scope():
            return await run_db(move_document_rows, self._db(), old_path, new_path)

    async def list_jobs(
        self,
        *,
        status: str = "pending",
        limit: int = 100,
    ) -> list[IndexJob]:
        return await run_db(
            list_index_jobs,
            self._db(),
            status=status,
            limit=limit,
        )

    async def claim_jobs(self, *, limit: int = 10) -> list[IndexJob]:
        async with self._write_scope():
            return await run_db(claim_index_jobs, self._db(), limit=limit)

    async def set_job_status(
        self,
        job_id: str,
        status: str,
        *,
        error: str = "",
    ) -> bool:
        async with self._write_scope():
            return await run_db(
                set_index_job_status,
                self._db(),
                job_id,
                status,
                error=error,
            )

    async def enqueue_job(self, node_id: str, content_hash: str) -> str:
        async with self._write_scope():
            return await run_db(enqueue_index_job, self._db(), node_id, content_hash)

    async def list_indexed_documents(self) -> list[MemoryNode]:
        return await run_db(_list_active_file_nodes, self._db())

    async def mark_documents_deleted(self, node_ids: Sequence[str]) -> int:
        async with self._write_scope():
            return await run_db(_mark_documents_deleted, self._db(), node_ids)

    async def read_chunk_index_state(self) -> ChunkIndexState | None:
        return await run_db(read_active_chunk_index_state, self._db())

    async def invalidate_vector_projection(self) -> int:
        async with self._write_scope():
            return await run_db(_invalidate_vector_projection, self._db())

    async def consume_vector_tombstones(
        self,
        collection: Any,
        *,
        limit: int = 200,
    ) -> int:
        async with self._write_scope():
            return await consume_sqlite_vector_tombstones(
                self._db(),
                collection,
                limit=limit,
            )

    async def run_index_worker(
        self,
        *,
        limit: int,
        collection: Any,
        embed_texts_func: Any,
        collection_resolver: Any,
        collection_upsert_func: Any,
        retry_failed: bool,
        reclaim_after: float | None,
    ) -> IndexWorkerReport:
        async with self._write_scope():
            return await process_index_jobs(
                self._db(),
                collection,
                limit=limit,
                embed_texts_func=embed_texts_func,
                collection_resolver=collection_resolver,
                collection_upsert_func=collection_upsert_func,
                retry_failed=retry_failed,
                reclaim_after=reclaim_after,
            )

    async def search_detailed(
        self,
        query: str,
        *,
        collection: Any,
        chunk_collection: Any,
        top_k: int,
        enable_association: bool,
        file_types: Sequence[str] | None,
        time_range_days: int,
        now: Any,
        workspace_path: str | Path | None,
        emit_visual_event: Any,
    ) -> DetailedSearchResult:
        return await search_memory_detailed(
            db=self._db(),
            query=query,
            collection=collection,
            top_k=top_k,
            enable_association=enable_association,
            file_types=list(file_types) if file_types is not None else None,
            time_range_days=time_range_days,
            emit_visual_event=emit_visual_event,
            now=now,
            workspace_path=workspace_path,
            chunk_collection=chunk_collection,
        )

    async def vector_search(
        self,
        query: str,
        *,
        collection: Any,
        chunk_collection: Any,
        top_k: int,
    ) -> list[tuple[Any, ...]]:
        return await vector_search(
            query=query,
            collection=collection,
            top_k=top_k,
            filter_existing_func=self.filter_existing_scores,
            db=self._db(),
            chunk_collection=chunk_collection,
        )

    async def fts_search(self, query: str, *, top_k: int) -> list[tuple[Any, ...]]:
        return await fts_search(self._db(), query, top_k)

    async def get_snippet(self, node_id: str) -> str:
        return await get_snippet(self._db(), node_id)

    async def filter_existing_scores(
        self,
        scores: Sequence[tuple[Any, ...]],
    ) -> tuple[Any, ...]:
        return await filter_existing_scores(self._db(), list(scores))

    async def graph_snapshot(
        self,
        *,
        limit_nodes: int,
        min_weight: float,
        focus_id: str | None,
    ) -> dict[str, Any]:
        from ...memory.router import _read_graph_payload

        return await _read_graph_payload(
            self._db(),
            limit_nodes=limit_nodes,
            min_weight=min_weight,
            focus_id=focus_id,
        )


class LocalExperienceLedgerStore(_LocalPort):
    async def append(
        self,
        records: Sequence[ExperienceRecord],
    ) -> ExperienceAppendReport:
        async with self._write_scope():
            return await run_db(append_experiences_detailed, self._db(), records)

    async def list_after(
        self,
        sequence: int,
        *,
        limit: int = 100,
        stream_scope: str | None = None,
    ) -> list[ExperienceRecord]:
        return await run_db(
            list_experiences_after,
            self._db(),
            sequence,
            limit=limit,
            stream_scope=stream_scope,
        )


class LocalWitnessLedgerStore(_LocalPort):
    async def append(self, **kwargs: Any) -> WitnessMemory:
        async with self._write_scope():
            return await run_db(insert_witness_memory, self._db(), **kwargs)

    async def mark_projection(
        self,
        witness_id: str,
        *,
        projection_path: str,
        status: str,
        error: str = "",
    ) -> bool:
        async with self._write_scope():
            return await run_db(
                mark_witness_projection,
                self._db(),
                witness_id,
                projection_path=projection_path,
                status=status,
                error=error,
            )

    async def list_pending(self, *, limit: int = 100) -> list[WitnessMemory]:
        return await run_db(
            list_pending_witness_projections,
            self._db(),
            limit=limit,
        )

    async def get_by_projection_path(
        self,
        projection_path: str,
    ) -> WitnessMemory | None:
        return await run_db(
            get_witness_by_projection_path,
            self._db(),
            projection_path,
        )

    async def get_state(self, consciousness_instance_id: str) -> dict[str, Any]:
        return await run_db(get_witness_state, self._db(), consciousness_instance_id)

    async def compare_and_advance_state(
        self,
        consciousness_instance_id: str,
        *,
        expected_sequence: int,
        expected_revision: int,
        next_sequence: int,
        last_run_at: str | None = None,
        last_success_at: str | None = None,
        last_error: str | None = None,
    ) -> dict[str, Any]:
        async with self._write_scope():
            return await run_db(
                update_witness_state,
                self._db(),
                consciousness_instance_id,
                last_sequence=next_sequence,
                last_run_at=last_run_at,
                last_success_at=last_success_at,
                last_error=last_error,
                expected_sequence=expected_sequence,
                expected_revision=expected_revision,
            )

    async def search(
        self,
        query: str,
        *,
        mode: Any,
        top_k: int,
        stream_scope: str | None,
        visibility: Sequence[str],
    ) -> list[Any]:
        return await run_db(
            search_witness_memories,
            self._db(),
            query,
            mode=mode,
            top_k=top_k,
            stream_scope=stream_scope,
            visibility=tuple(visibility),
        )

    async def migrate_legacy(self, **kwargs: Any) -> WitnessMemory | None:
        async with self._write_scope():
            return await run_db(migrate_legacy_witness, self._db(), **kwargs)

    async def migration_exists(self, migration_key: str) -> bool:
        return await run_db(migration_exists, self._db(), migration_key)

    async def record_migration(self, **kwargs: Any) -> None:
        async with self._write_scope():
            await run_db(record_witness_migration, self._db(), **kwargs)


class LocalLivingMemoryStore(_LocalPort):
    async def append_artifact(
        self,
        version: MemoryArtifactVersion,
        *,
        derivations: Sequence[MemoryDerivation] = (),
        expected_head_revision: int,
    ) -> MemoryArtifactVersion:
        async with self._write_scope():
            return await run_db(
                append_artifact_version,
                self._db(),
                version,
                derivations=derivations,
                expected_head_revision=expected_head_revision,
            )

    async def get_artifact_head(self, logical_key: str) -> ArtifactHead | None:
        return await run_db(get_artifact_head_state, self._db(), logical_key)

    async def list_artifact_history(
        self,
        logical_key: str,
    ) -> list[MemoryArtifactVersion]:
        return await run_db(list_artifact_history, self._db(), logical_key)

    async def list_artifact_heads(
        self,
    ) -> list[tuple[MemoryArtifactVersion, ArtifactHead]]:
        return await run_db(_list_artifact_head_records, self._db())

    async def append_interpretation(
        self,
        interpretation: MemoryInterpretation,
        *,
        sources: Sequence[InterpretationSource] = (),
    ) -> MemoryInterpretation:
        async with self._write_scope():
            return await run_db(
                append_interpretation,
                self._db(),
                interpretation,
                sources=sources,
            )

    async def append_relation(self, relation: SemanticRelation) -> SemanticRelation:
        async with self._write_scope():
            return await run_db(append_semantic_relation, self._db(), relation)

    async def list_relations(self, entity_ref: str) -> list[SemanticRelation]:
        return await run_db(list_semantic_relations, self._db(), entity_ref)

    async def list_interpretations(
        self,
        subject_id: str,
        *,
        recorded_as_of: str = "",
    ) -> list[MemoryInterpretation]:
        return await run_db(
            list_interpretations,
            self._db(),
            subject_id,
            recorded_as_of=recorded_as_of,
        )

    async def search_interpretations(
        self,
        query: str,
        *,
        top_k: int,
        stream_scope: str | None,
        visibility: Sequence[str],
        recorded_as_of: str = "",
    ) -> list[InterpretationSearchResult]:
        return await run_db(
            search_interpretations,
            self._db(),
            query,
            top_k=top_k,
            stream_scope=stream_scope,
            visibility=tuple(visibility),
            recorded_as_of=recorded_as_of,
        )

    async def get_interpretation(
        self,
        interpretation_id: str,
    ) -> tuple[MemoryInterpretation, tuple[InterpretationSource, ...]] | None:
        return await run_db(get_interpretation, self._db(), interpretation_id)

    async def choose_association_neighbours(
        self,
        seed_refs: Sequence[str],
        *,
        context_key: str,
        random_seed: int,
        limit: int,
    ) -> list[AssociationSelection]:
        return await run_db(
            choose_association_neighbours,
            self._db(),
            seed_refs,
            context_key=context_key,
            random_seed=random_seed,
            limit=limit,
        )

    async def list_association_evidence(
        self,
        entity_ref: str,
        *,
        context_key: str | None = None,
    ) -> list[AssociationEvidence]:
        return await run_db(
            list_association_evidence,
            self._db(),
            entity_ref,
            context_key=context_key,
        )

    async def rebuild_association_projection(self) -> int:
        async with self._write_scope():
            return await run_db(rebuild_association_projection, self._db())

    async def begin_recall(self, **kwargs: Any) -> RecallEpisode:
        async with self._write_scope():
            return await run_db(begin_recall_episode, self._db(), **kwargs)

    async def append_recall_events(
        self,
        events: Sequence[RecallEvent],
    ) -> tuple[RecallEvent, ...]:
        async with self._write_scope():
            return await run_db(append_recall_events, self._db(), events)

    async def append_corecall(self, event: CoRecallEvent) -> CoRecallEvent:
        async with self._write_scope():
            return await run_db(append_corecall_event, self._db(), event)


class LocalEpistemicMemoryStore(_LocalPort):
    async def _append(self, operation: Callable[..., Any], value: Any) -> Any:
        async with self._write_scope():
            return await run_db(operation, self._db(), value)

    async def append_claim(self, claim: MemoryClaim) -> MemoryClaim:
        return await self._append(append_claim, claim)

    async def append_evidence(self, evidence: ClaimEvidence) -> ClaimEvidence:
        return await self._append(append_claim_evidence, evidence)

    async def append_belief(self, belief: MemoryBelief) -> MemoryBelief:
        return await self._append(append_belief, belief)

    async def append_conflict(self, conflict: EpistemicConflict) -> EpistemicConflict:
        return await self._append(append_conflict, conflict)

    async def append_state_event(self, event: MemoryStateEvent) -> MemoryStateEvent:
        return await self._append(append_state_event, event)

    async def append_retrieval_episode(
        self,
        episode: RetrievalEpisode,
    ) -> RetrievalEpisode:
        return await self._append(append_retrieval_episode, episode)

    async def append_retrieval_exposure(
        self,
        exposure: RetrievalExposure,
    ) -> RetrievalExposure:
        return await self._append(append_retrieval_exposure, exposure)

    async def append_retrieval_feedback(
        self,
        feedback: RetrievalFeedback,
    ) -> RetrievalFeedback:
        return await self._append(append_retrieval_feedback, feedback)

    async def get_retrieval_plasticity(
        self,
        entity_type: str,
        entity_id: str,
    ) -> RetrievalPlasticity:
        return await run_db(
            get_retrieval_plasticity,
            self._db(),
            entity_type,
            entity_id,
        )

    async def get_disposition(
        self,
        entity_type: str,
        entity_id: str,
        *,
        recorded_as_of: str = "",
    ) -> MemoryDisposition:
        return await run_db(
            get_memory_disposition,
            self._db(),
            entity_type,
            entity_id,
            recorded_as_of=recorded_as_of,
        )

    async def get_claim_state(
        self,
        claim_id: str,
        *,
        recorded_as_of: str = "",
    ) -> ClaimState | None:
        return await run_db(
            get_claim_state,
            self._db(),
            claim_id,
            recorded_as_of=recorded_as_of,
        )

    async def list_claim_states(
        self,
        subject_key: str,
        *,
        recorded_as_of: str = "",
        valid_at: str = "",
        stream_scope: str | None = None,
        visibility: Sequence[str] = ("private",),
    ) -> list[ClaimState]:
        return await run_db(
            list_claim_states,
            self._db(),
            subject_key,
            recorded_as_of=recorded_as_of,
            valid_at=valid_at,
            stream_scope=stream_scope,
            visibility=tuple(visibility),
        )

    async def project_current_facts(
        self,
        subject_key: str,
        *,
        valid_at: str,
        recorded_as_of: str = "",
        stream_scope: str | None = None,
        visibility: Sequence[str] = ("private",),
    ) -> CurrentFactProjection:
        return await run_db(
            project_current_facts,
            self._db(),
            subject_key,
            valid_at=valid_at,
            recorded_as_of=recorded_as_of,
            stream_scope=stream_scope,
            visibility=tuple(visibility),
        )

    async def get_audit_trail(
        self,
        entity_type: str,
        entity_id: str,
        *,
        recorded_as_of: str = "",
    ) -> list[MemoryAuditEntry]:
        return await run_db(
            build_memory_audit_trail,
            self._db(),
            entity_type,
            entity_id,
            recorded_as_of=recorded_as_of,
        )

    async def list_state_events(
        self,
        entity_type: str,
        entity_id: str,
        *,
        recorded_as_of: str = "",
    ) -> list[MemoryStateEvent]:
        return await run_db(
            list_state_events,
            self._db(),
            entity_type,
            entity_id,
            recorded_as_of=recorded_as_of,
        )

    async def list_claim_evidence(self, claim_id: str) -> list[ClaimEvidence]:
        return await run_db(list_claim_evidence, self._db(), claim_id)

    async def search_claims(
        self,
        query: str,
        *,
        mode: str,
        top_k: int,
        stream_scope: str | None,
        visibility: Sequence[str],
        valid_at: str = "",
        recorded_as_of: str = "",
    ) -> list[ClaimSearchResult]:
        return await run_db(
            search_epistemic_claims,
            self._db(),
            query,
            mode=mode,
            top_k=top_k,
            stream_scope=stream_scope,
            visibility=tuple(visibility),
            valid_at=valid_at,
            recorded_as_of=recorded_as_of,
        )


class LocalLegacyGraphStore(_LocalPort):
    async def get_or_create_file_node(
        self,
        file_path: str,
        title: str,
        content: str,
    ) -> MemoryNode:
        async with self._write_scope():
            return await get_or_create_file_node(
                self._db(),
                file_path,
                title,
                content,
            )

    async def get_node_by_file_path(self, file_path: str) -> MemoryNode | None:
        return await get_node_by_file_path(self._db(), file_path)

    async def get_node_by_id(self, node_id: str) -> MemoryNode | None:
        return await get_node_by_id(self._db(), node_id)

    async def get_lineage_node_views(
        self,
        node_ids: Sequence[str],
    ) -> dict[str, LineageNodeView]:
        return await get_lineage_node_views(self._db(), list(node_ids))

    async def create_or_update_edge(
        self,
        source_id: str,
        target_id: str,
        edge_type: str,
        **kwargs: Any,
    ) -> MemoryEdge:
        async with self._write_scope():
            return await create_or_update_edge(
                self._db(),
                source_id,
                target_id,
                EdgeType(edge_type),
                **kwargs,
            )

    async def get_edges_from(
        self,
        node_id: str,
        min_weight: float = 0.0,
    ) -> list[MemoryEdge]:
        return await get_edges_from(self._db(), node_id, min_weight)

    async def get_edges_to(
        self,
        node_id: str,
        min_weight: float = 0.0,
    ) -> list[MemoryEdge]:
        return await get_edges_to(self._db(), node_id, min_weight)

    async def delete_edge(
        self,
        source_path: str,
        target_path: str,
        edge_type: Any = None,
    ) -> bool:
        async with self._write_scope():
            return await delete_edge(
                self._db(),
                source_path,
                target_path,
                edge_type=edge_type,
                generate_file_node_id_func=generate_file_node_id,
            )

    async def reinforce_coactivated(
        self,
        node_ids: Sequence[str],
        *,
        learning_rate: float,
    ) -> None:
        async with self._write_scope():
            await reinforce_coactivated(
                self._db(),
                list(node_ids),
                learning_rate=learning_rate,
                filter_existing_func=self._filter_existing_scores,
            )

    async def _filter_existing_scores(
        self,
        scores: list[tuple[str, float]],
    ) -> tuple[Any, ...]:
        return await filter_existing_scores(self._db(), scores)

    async def increment_access(self, node_id: str) -> None:
        async with self._write_scope():
            await increment_access(self._db(), node_id)

    async def spread_activation(
        self,
        seed_ids: Sequence[str],
        *,
        max_depth: int,
        max_results: int,
        spread_decay: float,
        spread_threshold: float,
        allowed_edge_types: Sequence[Any],
    ) -> list[tuple[Any, ...]]:
        return await spread_activation(
            self._db(),
            list(seed_ids),
            max_depth=max_depth,
            max_results=max_results,
            spread_decay=spread_decay,
            spread_threshold=spread_threshold,
            allowed_edge_types=list(allowed_edge_types),
        )

    async def insert_correction(
        self,
        *,
        topic: str,
        message: str,
        source: str,
        related_node_id: str | None,
        query: str,
        stream_id: str | None,
    ) -> MemoryCorrection:
        async with self._write_scope():
            return await insert_memory_correction(
                self._db(),
                topic=topic,
                message=message,
                source=source,
                related_node_id=related_node_id,
                query=query,
                stream_id=stream_id,
            )

    async def apply_decay(self) -> int:
        async with self._write_scope():
            return await apply_decay(self._db())

    async def stats(self) -> dict[str, Any]:
        return await get_stats(self._db())

    async def dream_walk(self, **kwargs: Any) -> dict[str, Any]:
        async with self._write_scope():
            return await dream_walk(self._db(), **kwargs)

    async def list_dream_candidate_nodes(self, limit: int) -> list[dict[str, Any]]:
        return await list_dream_candidate_nodes(self._db(), limit)

    async def list_random_file_nodes(self, limit: int) -> list[dict[str, Any]]:
        return await list_random_file_nodes(self._db(), limit)

    async def prune_weak_edges(self, threshold: float) -> int:
        async with self._write_scope():
            return await prune_weak_edges(self._db(), threshold)

    async def prune_orphan_edges(self) -> int:
        async with self._write_scope():
            return await run_db(_prune_orphan_edges, self._db())

    async def list_corrections(
        self,
        *,
        query: str = "",
        related_node_ids: Sequence[str] = (),
        limit: int = 20,
    ) -> list[MemoryCorrection]:
        return await list_memory_corrections(
            self._db(),
            query=query,
            related_node_ids=list(related_node_ids),
            limit=limit,
        )


def create_local_memory_storage_bundle(
    connection: ConnectionProvider,
    *,
    runtime: StorageBackendRuntime | None = None,
) -> MemoryStorageBundle:
    """Bind every Memory port to one SQLite connection owner and lock."""

    lock = asyncio.Lock()
    if runtime is not None and runtime.backend != BackendKind.LOCAL:
        raise RuntimeError("local Memory bundle requires a local runtime")
    return MemoryStorageBundle(
        backend=BackendKind.LOCAL,
        document_index=LocalDocumentIndexProjection(connection, lock, runtime),
        experiences=LocalExperienceLedgerStore(connection, lock, runtime),
        witnesses=LocalWitnessLedgerStore(connection, lock, runtime),
        living=LocalLivingMemoryStore(connection, lock, runtime),
        epistemic=LocalEpistemicMemoryStore(connection, lock, runtime),
        legacy_graph=LocalLegacyGraphStore(connection, lock, runtime),
    )


__all__ = [
    "LocalDocumentIndexProjection",
    "LocalEpistemicMemoryStore",
    "LocalExperienceLedgerStore",
    "LocalLegacyGraphStore",
    "LocalLivingMemoryStore",
    "LocalWitnessLedgerStore",
    "create_local_memory_storage_bundle",
]
