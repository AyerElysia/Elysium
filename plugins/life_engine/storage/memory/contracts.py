"""Shared behavioral contracts for selectable Life Memory storage.

These ports classify engineering durability only.  They never classify the
meaning, truth, maturity, or importance of a memory.  Open cognitive fields
remain open strings in the domain records consumed by these interfaces.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from ...memory.edges import MemoryEdge
from ...memory.epistemic import (
    ClaimEvidence,
    EpistemicConflict,
    MemoryBelief,
    MemoryClaim,
    MemoryStateEvent,
    RetrievalEpisode,
    RetrievalExposure,
    RetrievalFeedback,
)
from ...memory.experience import (
    ExperienceAppendReport,
    ExperienceRecord,
    WitnessMemory,
)
from ...memory.indexing import DocumentIndexResult, IndexJob
from ...memory.lineage import MemoryCorrection
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
)
from ...memory.nodes import MemoryNode
from ..models import StorageAvailability


class MemoryStoreRole(StrEnum):
    """Engineering role of a memory store, never a cognitive category."""

    AUTHORITATIVE_HISTORY = "authoritative_history"
    REBUILDABLE_PROJECTION = "rebuildable_projection"
    COMPATIBILITY_HISTORY = "compatibility_history"


@dataclass(frozen=True, slots=True)
class MemoryStoreCharacterization:
    """Auditable storage behavior and migration order for one memory domain."""

    name: str
    role: MemoryStoreRole
    append_only: bool
    uses_compare_and_swap: bool
    rebuild_source: str
    migration_order: int


def memory_store_characterizations() -> tuple[MemoryStoreCharacterization, ...]:
    """Return the frozen engineering characterization used by both backends."""

    return (
        MemoryStoreCharacterization(
            name="document_index",
            role=MemoryStoreRole.REBUILDABLE_PROJECTION,
            append_only=False,
            uses_compare_and_swap=True,
            rebuild_source="workspace documents and immutable memory ledgers",
            migration_order=10,
        ),
        MemoryStoreCharacterization(
            name="experience",
            role=MemoryStoreRole.AUTHORITATIVE_HISTORY,
            append_only=True,
            uses_compare_and_swap=False,
            rebuild_source="",
            migration_order=20,
        ),
        MemoryStoreCharacterization(
            name="witness",
            role=MemoryStoreRole.AUTHORITATIVE_HISTORY,
            append_only=True,
            uses_compare_and_swap=True,
            rebuild_source="",
            migration_order=30,
        ),
        MemoryStoreCharacterization(
            name="living",
            role=MemoryStoreRole.AUTHORITATIVE_HISTORY,
            append_only=True,
            uses_compare_and_swap=True,
            rebuild_source="",
            migration_order=40,
        ),
        MemoryStoreCharacterization(
            name="epistemic",
            role=MemoryStoreRole.AUTHORITATIVE_HISTORY,
            append_only=True,
            uses_compare_and_swap=False,
            rebuild_source="",
            migration_order=50,
        ),
        MemoryStoreCharacterization(
            name="legacy_graph",
            role=MemoryStoreRole.COMPATIBILITY_HISTORY,
            append_only=False,
            uses_compare_and_swap=False,
            rebuild_source="legacy memory nodes, edges, and correction evidence",
            migration_order=60,
        ),
    )


class MemoryStorePort(Protocol):
    """Common secret-free availability contract."""

    async def availability(self) -> StorageAvailability: ...


@runtime_checkable
class DocumentIndexProjection(MemoryStorePort, Protocol):
    """Rebuildable lexical/chunk/vector-work projection."""

    async def upsert_document(
        self,
        path: str,
        content: str,
        title: str = "",
        source_mtime: float | None = None,
        *,
        max_chars: int | None = None,
        overlap_chars: int | None = None,
    ) -> DocumentIndexResult: ...

    async def delete_document(self, path: str) -> bool: ...

    async def move_document(self, old_path: str, new_path: str) -> bool: ...

    async def list_jobs(
        self,
        *,
        status: str = "pending",
        limit: int = 100,
    ) -> list[IndexJob]: ...

    async def claim_jobs(self, *, limit: int = 10) -> list[IndexJob]: ...

    async def set_job_status(
        self,
        job_id: str,
        status: str,
        *,
        error: str = "",
    ) -> bool: ...

    async def graph_snapshot(
        self,
        *,
        limit_nodes: int,
        min_weight: float,
        focus_id: str | None,
    ) -> dict[str, Any]: ...


@runtime_checkable
class ExperienceLedgerStore(MemoryStorePort, Protocol):
    """Immutable experience occurrence ledger."""

    async def append(
        self,
        records: Sequence[ExperienceRecord],
    ) -> ExperienceAppendReport: ...

    async def list_after(
        self,
        sequence: int,
        *,
        limit: int = 100,
        stream_scope: str | None = None,
    ) -> list[ExperienceRecord]: ...


@runtime_checkable
class WitnessLedgerStore(MemoryStorePort, Protocol):
    """Subjective witness history plus a monotonic consumer mirror."""

    async def append(self, **kwargs: Any) -> WitnessMemory: ...

    async def mark_projection(
        self,
        witness_id: str,
        *,
        projection_path: str,
        status: str,
        error: str = "",
    ) -> bool: ...

    async def list_pending(self, *, limit: int = 100) -> list[WitnessMemory]: ...

    async def get_by_projection_path(
        self,
        projection_path: str,
    ) -> WitnessMemory | None: ...

    async def get_state(self, consciousness_instance_id: str) -> dict[str, Any]: ...

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
    ) -> dict[str, Any]: ...


@runtime_checkable
class LivingMemoryStore(MemoryStorePort, Protocol):
    """Traceable artifact, interpretation, relation, and recall ledgers."""

    async def append_artifact(
        self,
        version: MemoryArtifactVersion,
        *,
        derivations: Sequence[MemoryDerivation] = (),
        expected_head_revision: int,
    ) -> MemoryArtifactVersion: ...

    async def get_artifact_head(self, logical_key: str) -> ArtifactHead | None: ...

    async def list_artifact_history(
        self,
        logical_key: str,
    ) -> list[MemoryArtifactVersion]: ...

    async def append_interpretation(
        self,
        interpretation: MemoryInterpretation,
        *,
        sources: Sequence[InterpretationSource] = (),
    ) -> MemoryInterpretation: ...

    async def append_relation(self, relation: SemanticRelation) -> SemanticRelation: ...

    async def begin_recall(self, **kwargs: Any) -> RecallEpisode: ...

    async def append_recall_events(
        self,
        events: Sequence[RecallEvent],
    ) -> tuple[RecallEvent, ...]: ...

    async def append_corecall(self, event: CoRecallEvent) -> CoRecallEvent: ...


@runtime_checkable
class EpistemicMemoryStore(MemoryStorePort, Protocol):
    """Claims, evidence, conflicts, state events, and retrieval traces."""

    async def append_claim(self, claim: MemoryClaim) -> MemoryClaim: ...

    async def append_evidence(self, evidence: ClaimEvidence) -> ClaimEvidence: ...

    async def append_belief(self, belief: MemoryBelief) -> MemoryBelief: ...

    async def append_conflict(self, conflict: EpistemicConflict) -> EpistemicConflict: ...

    async def append_state_event(self, event: MemoryStateEvent) -> MemoryStateEvent: ...

    async def append_retrieval_episode(
        self,
        episode: RetrievalEpisode,
    ) -> RetrievalEpisode: ...

    async def append_retrieval_exposure(
        self,
        exposure: RetrievalExposure,
    ) -> RetrievalExposure: ...

    async def append_retrieval_feedback(
        self,
        feedback: RetrievalFeedback,
    ) -> RetrievalFeedback: ...


@runtime_checkable
class LegacyGraphStore(MemoryStorePort, Protocol):
    """Compatibility node/edge graph; never a replacement for memory history."""

    async def get_or_create_file_node(
        self,
        file_path: str,
        title: str,
        content: str,
    ) -> MemoryNode: ...

    async def get_node_by_file_path(self, file_path: str) -> MemoryNode | None: ...

    async def get_node_by_id(self, node_id: str) -> MemoryNode | None: ...

    async def create_or_update_edge(
        self,
        source_id: str,
        target_id: str,
        edge_type: str,
        **kwargs: Any,
    ) -> MemoryEdge: ...

    async def get_edges_from(
        self,
        node_id: str,
        min_weight: float = 0.0,
    ) -> list[MemoryEdge]: ...

    async def get_edges_to(
        self,
        node_id: str,
        min_weight: float = 0.0,
    ) -> list[MemoryEdge]: ...

    async def list_corrections(
        self,
        *,
        query: str = "",
        related_node_ids: Sequence[str] = (),
        limit: int = 20,
    ) -> list[MemoryCorrection]: ...


@dataclass(frozen=True, slots=True)
class MemoryStorageBundle:
    """One coherent set of Memory ports from the same backend generation."""

    document_index: DocumentIndexProjection
    experiences: ExperienceLedgerStore
    witnesses: WitnessLedgerStore
    living: LivingMemoryStore
    epistemic: EpistemicMemoryStore
    legacy_graph: LegacyGraphStore
