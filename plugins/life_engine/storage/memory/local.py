"""SQLite adapters for the shared Life Memory ports."""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Callable, Sequence
from typing import Any

from ...memory.edges import (
    EdgeType,
    MemoryEdge,
    create_or_update_edge,
    get_edges_from,
    get_edges_to,
)
from ...memory.epistemic import (
    ClaimEvidence,
    EpistemicConflict,
    MemoryBelief,
    MemoryClaim,
    MemoryStateEvent,
    RetrievalEpisode,
    RetrievalExposure,
    RetrievalFeedback,
    append_belief,
    append_claim,
    append_claim_evidence,
    append_conflict,
    append_retrieval_episode,
    append_retrieval_exposure,
    append_retrieval_feedback,
    append_state_event,
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
    update_witness_state,
)
from ...memory.indexing import (
    DocumentIndexResult,
    IndexJob,
    claim_index_jobs,
    delete_document_rows,
    list_index_jobs,
    move_document_rows,
    set_index_job_status,
    upsert_document_rows,
)
from ...memory.lineage import MemoryCorrection, list_memory_corrections
from ...memory.living import (
    ArtifactHead,
    CoRecallEvent,
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
    get_artifact_head_state,
    list_artifact_history,
)
from ...memory.nodes import (
    MemoryNode,
    get_node_by_file_path,
    get_or_create_file_node,
)
from ...memory.search import get_node_by_id
from ...memory.sqlite_runtime import run_db
from ..models import StorageAvailability
from .contracts import MemoryStorageBundle

ConnectionProvider = Callable[[], sqlite3.Connection]


class _LocalPort:
    def __init__(self, connection: ConnectionProvider, lock: asyncio.Lock) -> None:
        self._connection = connection
        self._lock = lock

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
        async with self._lock:
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
        async with self._lock:
            return await run_db(delete_document_rows, self._db(), path)

    async def move_document(self, old_path: str, new_path: str) -> bool:
        async with self._lock:
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
        async with self._lock:
            return await run_db(claim_index_jobs, self._db(), limit=limit)

    async def set_job_status(
        self,
        job_id: str,
        status: str,
        *,
        error: str = "",
    ) -> bool:
        async with self._lock:
            return await run_db(
                set_index_job_status,
                self._db(),
                job_id,
                status,
                error=error,
            )

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
        async with self._lock:
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
        async with self._lock:
            return await run_db(insert_witness_memory, self._db(), **kwargs)

    async def mark_projection(
        self,
        witness_id: str,
        *,
        projection_path: str,
        status: str,
        error: str = "",
    ) -> bool:
        async with self._lock:
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
        async with self._lock:
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


class LocalLivingMemoryStore(_LocalPort):
    async def append_artifact(
        self,
        version: MemoryArtifactVersion,
        *,
        derivations: Sequence[MemoryDerivation] = (),
        expected_head_revision: int,
    ) -> MemoryArtifactVersion:
        async with self._lock:
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

    async def append_interpretation(
        self,
        interpretation: MemoryInterpretation,
        *,
        sources: Sequence[InterpretationSource] = (),
    ) -> MemoryInterpretation:
        async with self._lock:
            return await run_db(
                append_interpretation,
                self._db(),
                interpretation,
                sources=sources,
            )

    async def append_relation(self, relation: SemanticRelation) -> SemanticRelation:
        async with self._lock:
            return await run_db(append_semantic_relation, self._db(), relation)

    async def begin_recall(self, **kwargs: Any) -> RecallEpisode:
        async with self._lock:
            return await run_db(begin_recall_episode, self._db(), **kwargs)

    async def append_recall_events(
        self,
        events: Sequence[RecallEvent],
    ) -> tuple[RecallEvent, ...]:
        async with self._lock:
            return await run_db(append_recall_events, self._db(), events)

    async def append_corecall(self, event: CoRecallEvent) -> CoRecallEvent:
        async with self._lock:
            return await run_db(append_corecall_event, self._db(), event)


class LocalEpistemicMemoryStore(_LocalPort):
    async def _append(self, operation: Callable[..., Any], value: Any) -> Any:
        async with self._lock:
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


class LocalLegacyGraphStore(_LocalPort):
    async def get_or_create_file_node(
        self,
        file_path: str,
        title: str,
        content: str,
    ) -> MemoryNode:
        async with self._lock:
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

    async def create_or_update_edge(
        self,
        source_id: str,
        target_id: str,
        edge_type: str,
        **kwargs: Any,
    ) -> MemoryEdge:
        async with self._lock:
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
) -> MemoryStorageBundle:
    """Bind every Memory port to one SQLite connection owner and lock."""

    lock = asyncio.Lock()
    return MemoryStorageBundle(
        document_index=LocalDocumentIndexProjection(connection, lock),
        experiences=LocalExperienceLedgerStore(connection, lock),
        witnesses=LocalWitnessLedgerStore(connection, lock),
        living=LocalLivingMemoryStore(connection, lock),
        epistemic=LocalEpistemicMemoryStore(connection, lock),
        legacy_graph=LocalLegacyGraphStore(connection, lock),
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
