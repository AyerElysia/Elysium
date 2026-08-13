"""Shared behavioral contracts for selectable Life Memory storage.

These ports classify engineering durability only.  They never classify the
meaning, truth, maturity, or importance of a memory.  Open cognitive fields
remain open strings in the domain records consumed by these interfaces.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Generic, Protocol, TypeVar, runtime_checkable

from ...memory.edges import MemoryEdge
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
)
from ...memory.experience import (
    ExperienceAppendReport,
    ExperienceOccurrenceCursor,
    ExperienceOccurrencePage,
    ExperienceOccurrenceRef,
    ExperienceRecord,
    WitnessMemory,
)
from ...memory.indexing import ChunkIndexState, DocumentIndexResult, IndexJob
from ...memory.lineage import MemoryCorrection
from ...memory.living import (
    ArtifactHead,
    AssociationEvidence,
    AssociationSelection,
    CoRecallEvent,
    InterpretationSearchResult,
    InterpretationSource,
    MemoryArtifactDescriptor,
    MemoryArtifactVersion,
    MemoryDerivation,
    MemoryInterpretation,
    RecallEpisode,
    RecallEvent,
    SemanticRelation,
)
from ...memory.nodes import MemoryNode
from ...memory.search import DetailedSearchResult, LineageNodeView
from ...memory.witness_pipeline import (
    WitnessDecision,
    WitnessDeliveryJob,
    WitnessWindow,
)
from ...memory.worker import IndexWorkerReport
from ...memory.workspace_projection_identity import WorkspaceProjectionBindingStore
from ..models import BackendKind, StorageAvailability


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


_PageItemT = TypeVar("_PageItemT")


@dataclass(frozen=True, slots=True, order=True)
class StableLedgerCursor:
    """Stable string ordering value plus immutable identity tie-breaker."""

    order_value: str
    identity: str

    def __post_init__(self) -> None:
        if not str(self.order_value):
            raise ValueError("StableLedgerCursorOrderRequired")
        if not str(self.identity):
            raise ValueError("StableLedgerCursorIdentityRequired")


@dataclass(frozen=True, slots=True)
class StableLedgerPage(Generic[_PageItemT]):
    """Bounded page tied to a stable frontier for restartable scans."""

    items: tuple[_PageItemT, ...]
    next_cursor: StableLedgerCursor | None
    frontier: StableLedgerCursor | None
    has_more: bool


@dataclass(frozen=True, slots=True)
class WitnessReconciliationState:
    """Durable technical cursor for one idempotent Witness reconciliation scan."""

    scan_name: str
    cursor: StableLedgerCursor | None = None
    frontier: StableLedgerCursor | None = None
    revision: int = 0
    cycle_started_at: str = ""
    last_completed_at: str = ""
    updated_at: str = ""
    state_sha256: str = ""


class WitnessReconciliationStateCorrupt(RuntimeError):
    """A durable technical cursor row violates its composite-key invariants."""


def witness_reconciliation_state_sha256(
    *,
    scan_name: str,
    cursor: StableLedgerCursor | None,
    frontier: StableLedgerCursor | None,
    revision: int,
    cycle_started_at: str,
    last_completed_at: str,
    updated_at: str,
) -> str:
    """Fingerprint one persisted technical scan state without ledger content."""

    body = {
        "scan_name": str(scan_name),
        "cursor": (
            None
            if cursor is None
            else [str(cursor.order_value), str(cursor.identity)]
        ),
        "frontier": (
            None
            if frontier is None
            else [str(frontier.order_value), str(frontier.identity)]
        ),
        "revision": int(revision),
        "cycle_started_at": str(cycle_started_at),
        "last_completed_at": str(last_completed_at),
        "updated_at": str(updated_at),
    }
    encoded = json.dumps(
        body,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def validate_witness_reconciliation_state(
    state: WitnessReconciliationState,
) -> WitnessReconciliationState:
    """Fail closed on partial, regressed, or out-of-frontier durable cursors."""

    if not str(state.scan_name):
        raise WitnessReconciliationStateCorrupt(
            "WitnessReconciliationScanNameMissing"
        )
    if int(state.revision) < 0:
        raise WitnessReconciliationStateCorrupt(
            "WitnessReconciliationRevisionInvalid"
        )
    if state.cursor is not None and state.frontier is None:
        raise WitnessReconciliationStateCorrupt(
            "WitnessReconciliationCursorWithoutFrontier"
        )
    if (
        state.cursor is not None
        and state.frontier is not None
        and state.cursor > state.frontier
    ):
        raise WitnessReconciliationStateCorrupt(
            "WitnessReconciliationCursorBeyondFrontier"
        )
    if state.frontier is not None and not str(state.cycle_started_at):
        raise WitnessReconciliationStateCorrupt(
            "WitnessReconciliationCycleTimestampMissing"
        )
    persisted = bool(
        int(state.revision)
        or state.cursor is not None
        or state.frontier is not None
        or str(state.cycle_started_at)
        or str(state.last_completed_at)
        or str(state.updated_at)
    )
    if persisted:
        observed = str(state.state_sha256 or "").strip().lower()
        if len(observed) != 64 or any(
            character not in "0123456789abcdef" for character in observed
        ):
            raise WitnessReconciliationStateCorrupt(
                "WitnessReconciliationChecksumMissing"
            )
        expected = witness_reconciliation_state_sha256(
            scan_name=state.scan_name,
            cursor=state.cursor,
            frontier=state.frontier,
            revision=state.revision,
            cycle_started_at=state.cycle_started_at,
            last_completed_at=state.last_completed_at,
            updated_at=state.updated_at,
        )
        if observed != expected:
            raise WitnessReconciliationStateCorrupt(
                "WitnessReconciliationChecksumMismatch"
            )
    return state


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


@dataclass(frozen=True, slots=True)
class CanonicalDocumentMetadata:
    """Content-free canonical metadata for one indexed document path."""

    node_id: str
    file_path: str
    content_hash: str | None
    title: str
    source_mtime: float | None
    index_revision: int
    is_deleted: bool
    updated_at: float


@runtime_checkable
class DocumentIndexProjection(MemoryStorePort, Protocol):
    """Rebuildable lexical/chunk/vector-work projection."""

    async def get_document_metadata(
        self,
        path: str,
    ) -> CanonicalDocumentMetadata | None: ...

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
        job: IndexJob | str,
        status: str,
        *,
        error: str = "",
    ) -> bool:
        """CAS a claimed job by its revision and lease token.

        ``str`` remains accepted for source compatibility but must fail
        closed because it cannot prove ownership of a processing attempt.
        """
        ...

    async def enqueue_job(self, node_id: str, content_hash: str) -> str: ...

    async def list_indexed_documents(self) -> list[MemoryNode]: ...

    async def mark_documents_deleted(self, node_ids: Sequence[str]) -> int: ...

    async def read_chunk_index_state(self) -> ChunkIndexState | None: ...

    async def invalidate_vector_projection(self) -> int: ...

    async def consume_vector_tombstones(
        self,
        collection: Any,
        *,
        limit: int = 200,
    ) -> int: ...

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
    ) -> IndexWorkerReport: ...

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
    ) -> DetailedSearchResult: ...

    async def vector_search(
        self,
        query: str,
        *,
        collection: Any,
        chunk_collection: Any,
        top_k: int,
    ) -> list[tuple[Any, ...]]: ...

    async def fts_search(self, query: str, *, top_k: int) -> list[tuple[Any, ...]]: ...

    async def get_snippet(self, node_id: str) -> str: ...

    async def filter_existing_scores(
        self,
        scores: Sequence[tuple[Any, ...]],
    ) -> tuple[Any, ...]: ...

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

    async def list_occurrences_after(
        self,
        position: int,
        limit: int = 100,
    ) -> list[ExperienceOccurrenceRef]: ...

    async def list_occurrence_page(
        self,
        *,
        position_after: int = 0,
        after: ExperienceOccurrenceCursor | None = None,
        through: ExperienceOccurrenceCursor | None = None,
        limit: int = 100,
    ) -> ExperienceOccurrencePage: ...

    async def health_snapshot(self) -> dict[str, Any]: ...


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

    async def list_pending_page(
        self,
        *,
        after: StableLedgerCursor | None = None,
        through: StableLedgerCursor | None = None,
        limit: int = 100,
    ) -> StableLedgerPage[WitnessMemory]: ...

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

    async def search(
        self,
        query: str,
        *,
        mode: Any,
        top_k: int,
        stream_scope: str | None,
        visibility: Sequence[str],
    ) -> list[Any]: ...

    async def migrate_legacy(self, **kwargs: Any) -> WitnessMemory | None: ...

    async def migration_exists(self, migration_key: str) -> bool: ...

    async def record_migration(self, **kwargs: Any) -> None: ...

    async def append_window(self, window: WitnessWindow) -> WitnessWindow: ...

    async def get_window(self, window_id: str) -> WitnessWindow | None: ...

    async def next_pending_window(
        self,
        consciousness_instance_id: str | None = None,
    ) -> WitnessWindow | None: ...

    async def append_decision(
        self,
        decision: WitnessDecision,
        *,
        delivery_payloads: Mapping[str, Mapping[str, Any]],
    ) -> WitnessDecision: ...

    async def get_decision(self, decision_id: str) -> WitnessDecision | None: ...

    async def list_delivery_jobs(
        self,
        *,
        delivery_kind: str | None = None,
        statuses: Sequence[str] = ("pending", "failed"),
        limit: int = 100,
    ) -> list[WitnessDeliveryJob]: ...

    async def list_delivery_jobs_page(
        self,
        *,
        delivery_kind: str | None = None,
        statuses: Sequence[str] = ("pending", "failed"),
        after: StableLedgerCursor | None = None,
        through: StableLedgerCursor | None = None,
        limit: int = 100,
    ) -> StableLedgerPage[WitnessDeliveryJob]: ...

    async def mark_delivery_job(
        self,
        job_id: str,
        *,
        expected_revision: int,
        status: str,
        error_type: str = "",
        available_at: str = "",
        lease_owner: str = "",
        lease_expires_at: str = "",
        completed_at: str = "",
    ) -> WitnessDeliveryJob: ...

    async def list_projection_records(
        self,
        *,
        statuses: Sequence[str] = (),
        limit: int = 100,
    ) -> list[WitnessDeliveryJob]: ...

    async def list_projection_records_page(
        self,
        *,
        statuses: Sequence[str] = (),
        after: StableLedgerCursor | None = None,
        through: StableLedgerCursor | None = None,
        limit: int = 100,
    ) -> StableLedgerPage[WitnessDeliveryJob]: ...

    async def get_reconciliation_state(
        self,
        scan_name: str,
    ) -> WitnessReconciliationState: ...

    async def compare_and_advance_reconciliation_state(
        self,
        scan_name: str,
        *,
        expected_revision: int,
        next_cursor: StableLedgerCursor | None,
        frontier: StableLedgerCursor | None,
        completed: bool,
    ) -> WitnessReconciliationState: ...

    async def delivery_health(self) -> dict[str, Any]: ...

    async def projection_health(self) -> dict[str, Any]: ...


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

    async def get_artifact_version(
        self,
        artifact_id: str,
    ) -> MemoryArtifactVersion | None: ...

    async def list_artifact_descriptors(
        self,
        logical_key: str,
    ) -> list[MemoryArtifactDescriptor]: ...

    async def list_artifact_history(
        self,
        logical_key: str,
    ) -> list[MemoryArtifactVersion]: ...

    async def list_artifact_heads(
        self,
    ) -> list[tuple[MemoryArtifactVersion, ArtifactHead]]: ...

    async def append_interpretation(
        self,
        interpretation: MemoryInterpretation,
        *,
        sources: Sequence[InterpretationSource] = (),
    ) -> MemoryInterpretation: ...

    async def append_relation(self, relation: SemanticRelation) -> SemanticRelation: ...

    async def list_relations(self, entity_ref: str) -> list[SemanticRelation]: ...

    async def list_interpretations(
        self,
        subject_id: str,
        *,
        recorded_as_of: str = "",
    ) -> list[MemoryInterpretation]: ...

    async def search_interpretations(
        self,
        query: str,
        *,
        top_k: int,
        stream_scope: str | None,
        visibility: Sequence[str],
        recorded_as_of: str = "",
    ) -> list[InterpretationSearchResult]: ...

    async def get_interpretation(
        self,
        interpretation_id: str,
    ) -> tuple[MemoryInterpretation, tuple[InterpretationSource, ...]] | None: ...

    async def choose_association_neighbours(
        self,
        seed_refs: Sequence[str],
        *,
        context_key: str,
        random_seed: int,
        limit: int,
    ) -> list[AssociationSelection]: ...

    async def list_association_evidence(
        self,
        entity_ref: str,
        *,
        context_key: str | None = None,
    ) -> list[AssociationEvidence]: ...

    async def rebuild_association_projection(self) -> int: ...

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

    async def append_conflict(
        self, conflict: EpistemicConflict
    ) -> EpistemicConflict: ...

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

    async def get_retrieval_plasticity(
        self,
        entity_type: str,
        entity_id: str,
    ) -> RetrievalPlasticity: ...

    async def get_disposition(
        self,
        entity_type: str,
        entity_id: str,
        *,
        recorded_as_of: str = "",
    ) -> MemoryDisposition: ...

    async def get_claim_state(
        self,
        claim_id: str,
        *,
        recorded_as_of: str = "",
    ) -> ClaimState | None: ...

    async def list_claim_states(
        self,
        subject_key: str,
        *,
        recorded_as_of: str = "",
        valid_at: str = "",
        stream_scope: str | None = None,
        visibility: Sequence[str] = ("private",),
    ) -> list[ClaimState]: ...

    async def project_current_facts(
        self,
        subject_key: str,
        *,
        valid_at: str,
        recorded_as_of: str = "",
        stream_scope: str | None = None,
        visibility: Sequence[str] = ("private",),
    ) -> CurrentFactProjection: ...

    async def get_audit_trail(
        self,
        entity_type: str,
        entity_id: str,
        *,
        recorded_as_of: str = "",
    ) -> list[MemoryAuditEntry]: ...

    async def list_state_events(
        self,
        entity_type: str,
        entity_id: str,
        *,
        recorded_as_of: str = "",
    ) -> list[MemoryStateEvent]: ...

    async def list_claim_evidence(self, claim_id: str) -> list[ClaimEvidence]: ...

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
    ) -> list[ClaimSearchResult]: ...


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

    async def get_lineage_node_views(
        self,
        node_ids: Sequence[str],
    ) -> dict[str, LineageNodeView]: ...

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

    async def delete_edge(
        self,
        source_path: str,
        target_path: str,
        edge_type: Any = None,
    ) -> bool: ...

    async def reinforce_coactivated(
        self,
        node_ids: Sequence[str],
        *,
        learning_rate: float,
    ) -> None: ...

    async def increment_access(self, node_id: str) -> None: ...

    async def spread_activation(
        self,
        seed_ids: Sequence[str],
        *,
        max_depth: int,
        max_results: int,
        spread_decay: float,
        spread_threshold: float,
        allowed_edge_types: Sequence[Any],
    ) -> list[tuple[Any, ...]]: ...

    async def insert_correction(
        self,
        *,
        topic: str,
        message: str,
        source: str,
        related_node_id: str | None,
        query: str,
        stream_id: str | None,
    ) -> MemoryCorrection: ...

    async def apply_decay(self) -> int: ...

    async def stats(self) -> dict[str, Any]: ...

    async def dream_walk(self, **kwargs: Any) -> dict[str, Any]: ...

    async def list_dream_candidate_nodes(self, limit: int) -> list[dict[str, Any]]: ...

    async def list_random_file_nodes(self, limit: int) -> list[dict[str, Any]]: ...

    async def prune_weak_edges(self, threshold: float) -> int: ...

    async def prune_orphan_edges(self) -> int: ...

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

    backend: BackendKind
    document_index: DocumentIndexProjection
    experiences: ExperienceLedgerStore
    witnesses: WitnessLedgerStore
    living: LivingMemoryStore
    epistemic: EpistemicMemoryStore
    legacy_graph: LegacyGraphStore
    workspace_projection: WorkspaceProjectionBindingStore | None = None
