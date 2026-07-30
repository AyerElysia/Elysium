"""Append-only epistemic memory primitives for Life Engine.

This module separates claims, evidence, beliefs, conflicts, and state-changing
memory events. Source authority describes provenance permission; it never turns
retrieval rank, repetition, or model confidence into truth.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Iterable, Sequence
from uuid import uuid4

from .indexing import transaction


class AuthorityClass(str, Enum):
    """Provenance permission attached to a record or explicit state event."""

    SUBJECT = "subject"
    EXPLICIT_USER = "explicit_user"
    VERIFIED = "verified"
    AUTHORITATIVE = "authoritative"
    OBSERVED = "observed"
    WITNESS = "witness"
    REFLECTION = "reflection"
    INFERRED = "inferred"
    UNKNOWN = "unknown"


class ClaimStatus(str, Enum):
    """Reduced status of a claim; the source claim itself stays immutable."""

    PROPOSED = "proposed"
    CONFIRMED = "confirmed"
    DISPUTED = "disputed"
    SUPERSEDED = "superseded"
    RETRACTED = "retracted"


class BeliefStatus(str, Enum):
    """Reduced relationship between one perspective and one claim."""

    UNREVIEWED = "unreviewed"
    ENDORSED = "endorsed"
    REJECTED = "rejected"
    SUSPENDED = "suspended"


class EvidenceStance(str, Enum):
    """How an evidence reference relates to a claim."""

    SUPPORTS = "supports"
    CHALLENGES = "challenges"
    CONTEXT = "context"


@dataclass(frozen=True, slots=True)
class MemoryClaim:
    claim_id: str
    subject_key: str
    content: str
    claim_kind: str
    source: str
    authority: str
    valid_from: str
    valid_to: str
    recorded_at: str
    stream_scope: str = ""
    visibility: str = "private"
    consciousness_instance_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ClaimEvidence:
    evidence_link_id: str
    claim_id: str
    evidence_kind: str
    evidence_ref: str
    stance: str
    source_excerpt: str
    recorded_at: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class MemoryBelief:
    belief_id: str
    claim_id: str
    perspective_subject_id: str
    consciousness_instance_id: str
    recorded_at: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class EpistemicConflict:
    conflict_id: str
    left_claim_id: str
    right_claim_id: str
    relation: str
    reason: str
    recorded_at: str
    detected_by: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class MemoryStateEvent:
    event_id: str
    entity_type: str
    entity_id: str
    event_type: str
    actor: str
    authority: str
    reason: str
    recorded_at: str
    valid_at: str
    caused_by_event_id: str = ""
    reverses_event_id: str = ""
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ClaimState:
    claim: MemoryClaim
    status: str = ClaimStatus.PROPOSED.value
    superseded_by: tuple[str, ...] = ()
    active_event_ids: tuple[str, ...] = ()
    last_changed_at: str = ""


@dataclass(frozen=True, slots=True)
class BeliefState:
    belief: MemoryBelief
    status: str = BeliefStatus.UNREVIEWED.value
    active_event_ids: tuple[str, ...] = ()
    last_changed_at: str = ""


@dataclass(frozen=True, slots=True)
class CurrentFactProjection:
    """A bitemporal current-fact view that preserves unresolved alternatives."""

    subject_key: str
    valid_at: str
    recorded_as_of: str
    active_claims: tuple[ClaimState, ...]
    conflicts: tuple[EpistemicConflict, ...]
    uncertainty: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class MemoryDisposition:
    """Independent, reversible access dimensions for one memory entity."""

    entity_type: str
    entity_id: str
    accessibility: str = "available"
    endorsement: str = "unreviewed"
    contextual_inhibition: tuple[str, ...] = ()
    narrative_salience: float = 0.5
    visibility: str = "private"
    active_event_ids: tuple[str, ...] = ()
    last_changed_at: str = ""


@dataclass(frozen=True, slots=True)
class MemoryAuditEntry:
    """An auditable state transition with causal and reversal relationships."""

    event: MemoryStateEvent
    active: bool
    reversed_by: tuple[str, ...] = ()
    cause: MemoryStateEvent | None = None


@dataclass(frozen=True, slots=True)
class RetrievalEpisode:
    """One retrieval context; exposure itself is never evidence of truth."""

    episode_id: str
    query: str
    mode: str
    consciousness_instance_id: str
    stream_scope: str
    recorded_at: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RetrievalExposure:
    """A candidate shown in an episode, with later subject feedback if any."""

    exposure_id: str
    episode_id: str
    entity_type: str
    entity_id: str
    rank_position: int
    retrieval_source: str
    recorded_at: str
    feedback: str = "unreviewed"
    feedback_reason: str = ""
    feedback_at: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RetrievalFeedback:
    """Append-only subject feedback about an exposure, not about factual truth."""

    feedback_id: str
    exposure_id: str
    feedback: str
    actor: str
    reason: str
    recorded_at: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RetrievalPlasticity:
    """Retrieval-derived ranking hints explicitly separated from epistemic status."""

    entity_type: str
    entity_id: str
    accepted_count: int = 0
    rejected_count: int = 0
    corrected_count: int = 0
    retrieval_affinity: float = 0.0
    epistemic_note: str = "retrieval feedback is not evidence of truth"


@dataclass(frozen=True, slots=True)
class ClaimSearchResult:
    """One epistemic claim candidate with explicit state and unresolved conflicts."""

    state: ClaimState
    rank_score: float
    evidence: tuple[ClaimEvidence, ...] = ()
    conflicts: tuple[EpistemicConflict, ...] = ()
    plasticity: RetrievalPlasticity | None = None


_CONFIRMING_AUTHORITIES = {
    AuthorityClass.SUBJECT.value,
    AuthorityClass.EXPLICIT_USER.value,
    AuthorityClass.VERIFIED.value,
    AuthorityClass.AUTHORITATIVE.value,
}


def now_iso() -> str:
    """Return an offset-aware timestamp suitable for recorded time."""

    return datetime.now(timezone.utc).astimezone().isoformat()


def authority_for_source(source: str) -> AuthorityClass:
    """Map legacy source labels to provenance classes without scoring truth."""

    normalized = str(source or "").strip().lower()
    aliases = {
        "subject": AuthorityClass.SUBJECT,
        "self": AuthorityClass.SUBJECT,
        "user": AuthorityClass.EXPLICIT_USER,
        "explicit_user": AuthorityClass.EXPLICIT_USER,
        "verified": AuthorityClass.VERIFIED,
        "authoritative": AuthorityClass.AUTHORITATIVE,
        "observation": AuthorityClass.OBSERVED,
        "observed": AuthorityClass.OBSERVED,
        "experience": AuthorityClass.OBSERVED,
        "witness": AuthorityClass.WITNESS,
        "memory_witness": AuthorityClass.WITNESS,
        "reflection": AuthorityClass.REFLECTION,
        "inference": AuthorityClass.INFERRED,
        "inferred": AuthorityClass.INFERRED,
        "learning_system": AuthorityClass.VERIFIED,
    }
    return aliases.get(normalized, AuthorityClass.UNKNOWN)


def create_epistemic_schema(db: sqlite3.Connection) -> None:
    """Create additive append-only epistemic tables and integrity triggers."""

    db.row_factory = sqlite3.Row
    with transaction(db):
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS memory_claims (
                claim_id TEXT PRIMARY KEY,
                subject_key TEXT NOT NULL,
                content TEXT NOT NULL,
                claim_kind TEXT NOT NULL,
                source TEXT NOT NULL,
                authority TEXT NOT NULL,
                valid_from TEXT NOT NULL DEFAULT '',
                valid_to TEXT NOT NULL DEFAULT '',
                recorded_at TEXT NOT NULL,
                stream_scope TEXT NOT NULL DEFAULT '',
                visibility TEXT NOT NULL DEFAULT 'private',
                consciousness_instance_id TEXT NOT NULL DEFAULT '',
                metadata_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS idx_claims_subject_time
                ON memory_claims(subject_key, recorded_at DESC);
            CREATE INDEX IF NOT EXISTS idx_claims_scope
                ON memory_claims(stream_scope, visibility);

            CREATE TABLE IF NOT EXISTS memory_claim_evidence (
                evidence_link_id TEXT PRIMARY KEY,
                claim_id TEXT NOT NULL,
                evidence_kind TEXT NOT NULL,
                evidence_ref TEXT NOT NULL,
                stance TEXT NOT NULL,
                source_excerpt TEXT NOT NULL DEFAULT '',
                recorded_at TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                FOREIGN KEY (claim_id) REFERENCES memory_claims(claim_id)
                    ON DELETE RESTRICT
            );
            CREATE INDEX IF NOT EXISTS idx_claim_evidence_claim
                ON memory_claim_evidence(claim_id, recorded_at);
            CREATE INDEX IF NOT EXISTS idx_claim_evidence_ref
                ON memory_claim_evidence(evidence_kind, evidence_ref);

            CREATE TABLE IF NOT EXISTS memory_beliefs (
                belief_id TEXT PRIMARY KEY,
                claim_id TEXT NOT NULL,
                perspective_subject_id TEXT NOT NULL,
                consciousness_instance_id TEXT NOT NULL DEFAULT '',
                recorded_at TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                FOREIGN KEY (claim_id) REFERENCES memory_claims(claim_id)
                    ON DELETE RESTRICT,
                UNIQUE (claim_id, perspective_subject_id, consciousness_instance_id)
            );
            CREATE INDEX IF NOT EXISTS idx_beliefs_perspective
                ON memory_beliefs(perspective_subject_id, recorded_at DESC);

            CREATE TABLE IF NOT EXISTS memory_epistemic_conflicts (
                conflict_id TEXT PRIMARY KEY,
                left_claim_id TEXT NOT NULL,
                right_claim_id TEXT NOT NULL,
                relation TEXT NOT NULL,
                reason TEXT NOT NULL DEFAULT '',
                recorded_at TEXT NOT NULL,
                detected_by TEXT NOT NULL DEFAULT '',
                metadata_json TEXT NOT NULL DEFAULT '{}',
                FOREIGN KEY (left_claim_id) REFERENCES memory_claims(claim_id)
                    ON DELETE RESTRICT,
                FOREIGN KEY (right_claim_id) REFERENCES memory_claims(claim_id)
                    ON DELETE RESTRICT,
                CHECK (left_claim_id <> right_claim_id)
            );
            CREATE INDEX IF NOT EXISTS idx_epistemic_conflict_left
                ON memory_epistemic_conflicts(left_claim_id, recorded_at);
            CREATE INDEX IF NOT EXISTS idx_epistemic_conflict_right
                ON memory_epistemic_conflicts(right_claim_id, recorded_at);

            CREATE TABLE IF NOT EXISTS memory_state_events (
                event_id TEXT PRIMARY KEY,
                entity_type TEXT NOT NULL,
                entity_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                actor TEXT NOT NULL DEFAULT '',
                authority TEXT NOT NULL DEFAULT 'unknown',
                reason TEXT NOT NULL DEFAULT '',
                recorded_at TEXT NOT NULL,
                valid_at TEXT NOT NULL DEFAULT '',
                caused_by_event_id TEXT NOT NULL DEFAULT '',
                reverses_event_id TEXT NOT NULL DEFAULT '',
                payload_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS idx_memory_state_entity
                ON memory_state_events(entity_type, entity_id, recorded_at, event_id);
            CREATE INDEX IF NOT EXISTS idx_memory_state_reverse
                ON memory_state_events(reverses_event_id)
                WHERE reverses_event_id <> '';

            CREATE TABLE IF NOT EXISTS memory_retrieval_episodes (
                episode_id TEXT PRIMARY KEY,
                query TEXT NOT NULL,
                mode TEXT NOT NULL,
                consciousness_instance_id TEXT NOT NULL DEFAULT '',
                stream_scope TEXT NOT NULL DEFAULT '',
                recorded_at TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS idx_retrieval_episodes_time
                ON memory_retrieval_episodes(recorded_at DESC);
            CREATE TABLE IF NOT EXISTS memory_retrieval_exposures (
                exposure_id TEXT PRIMARY KEY,
                episode_id TEXT NOT NULL,
                entity_type TEXT NOT NULL,
                entity_id TEXT NOT NULL,
                rank_position INTEGER NOT NULL,
                retrieval_source TEXT NOT NULL,
                recorded_at TEXT NOT NULL,
                feedback TEXT NOT NULL DEFAULT 'unreviewed',
                feedback_reason TEXT NOT NULL DEFAULT '',
                feedback_at TEXT NOT NULL DEFAULT '',
                metadata_json TEXT NOT NULL DEFAULT '{}',
                FOREIGN KEY (episode_id) REFERENCES memory_retrieval_episodes(episode_id)
                    ON DELETE RESTRICT,
                UNIQUE (episode_id, entity_type, entity_id)
            );
            CREATE INDEX IF NOT EXISTS idx_retrieval_exposure_entity
                ON memory_retrieval_exposures(entity_type, entity_id, recorded_at DESC);
            CREATE TABLE IF NOT EXISTS memory_retrieval_feedback (
                feedback_id TEXT PRIMARY KEY,
                exposure_id TEXT NOT NULL,
                feedback TEXT NOT NULL,
                actor TEXT NOT NULL DEFAULT '',
                reason TEXT NOT NULL DEFAULT '',
                recorded_at TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                FOREIGN KEY (exposure_id) REFERENCES memory_retrieval_exposures(exposure_id)
                    ON DELETE RESTRICT
            );
            CREATE INDEX IF NOT EXISTS idx_retrieval_feedback_exposure
                ON memory_retrieval_feedback(exposure_id, recorded_at);
            """
        )
        for table in (
            "memory_claims",
            "memory_claim_evidence",
            "memory_beliefs",
            "memory_epistemic_conflicts",
            "memory_state_events",
            "memory_retrieval_episodes",
            "memory_retrieval_exposures",
            "memory_retrieval_feedback",
        ):
            db.executescript(
                f"""
                CREATE TRIGGER IF NOT EXISTS {table}_immutable_update
                BEFORE UPDATE ON {table} BEGIN
                    SELECT RAISE(ABORT, 'EpistemicRecordImmutable');
                END;
                CREATE TRIGGER IF NOT EXISTS {table}_immutable_delete
                BEFORE DELETE ON {table} BEGIN
                    SELECT RAISE(ABORT, 'EpistemicRecordImmutable');
                END;
                """
            )


def append_claim(db: sqlite3.Connection, claim: MemoryClaim) -> MemoryClaim:
    """Idempotently append one immutable claim, rejecting identity reuse."""

    normalized = replace(
        claim,
        content=claim.content.strip(),
        recorded_at=claim.recorded_at or now_iso(),
        authority=claim.authority or authority_for_source(claim.source).value,
    )
    if not normalized.claim_id or not normalized.subject_key or not normalized.content:
        raise ValueError("ClaimIdentityAndContentRequired")
    with transaction(db):
        row = db.execute(
            "SELECT * FROM memory_claims WHERE claim_id = ?", (normalized.claim_id,)
        ).fetchone()
        if row is not None:
            persisted = _claim_from_row(row)
            if persisted != normalized:
                raise ValueError(f"ClaimIdentityConflict:{normalized.claim_id}")
            return persisted
        db.execute(
            """INSERT INTO memory_claims (
                claim_id, subject_key, content, claim_kind, source, authority,
                valid_from, valid_to, recorded_at, stream_scope, visibility,
                consciousness_instance_id, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                normalized.claim_id,
                normalized.subject_key,
                normalized.content,
                normalized.claim_kind,
                normalized.source,
                normalized.authority,
                normalized.valid_from,
                normalized.valid_to,
                normalized.recorded_at,
                normalized.stream_scope,
                normalized.visibility,
                normalized.consciousness_instance_id,
                _json_dump(normalized.metadata),
            ),
        )
    return normalized


def append_claim_evidence(
    db: sqlite3.Connection,
    evidence: ClaimEvidence,
) -> ClaimEvidence:
    """Append an evidence link; rank or repetition is not stored as truth."""

    normalized = replace(
        evidence,
        recorded_at=evidence.recorded_at or now_iso(),
        stance=evidence.stance or EvidenceStance.CONTEXT.value,
    )
    if not normalized.evidence_link_id or not normalized.evidence_ref:
        raise ValueError("EvidenceIdentityAndReferenceRequired")
    _require_entity(db, "claim", normalized.claim_id)
    return _append_identity_record(
        db,
        table="memory_claim_evidence",
        id_column="evidence_link_id",
        identifier=normalized.evidence_link_id,
        record=normalized,
        row_loader=_evidence_from_row,
        sql="""INSERT INTO memory_claim_evidence (
            evidence_link_id, claim_id, evidence_kind, evidence_ref, stance,
            source_excerpt, recorded_at, metadata_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        params=(
            normalized.evidence_link_id,
            normalized.claim_id,
            normalized.evidence_kind,
            normalized.evidence_ref,
            normalized.stance,
            normalized.source_excerpt,
            normalized.recorded_at,
            _json_dump(normalized.metadata),
        ),
    )


def append_belief(db: sqlite3.Connection, belief: MemoryBelief) -> MemoryBelief:
    """Append a perspective-to-claim identity; endorsement lives in events."""

    normalized = replace(belief, recorded_at=belief.recorded_at or now_iso())
    _require_entity(db, "claim", normalized.claim_id)
    return _append_identity_record(
        db,
        table="memory_beliefs",
        id_column="belief_id",
        identifier=normalized.belief_id,
        record=normalized,
        row_loader=_belief_from_row,
        sql="""INSERT INTO memory_beliefs (
            belief_id, claim_id, perspective_subject_id,
            consciousness_instance_id, recorded_at, metadata_json
        ) VALUES (?, ?, ?, ?, ?, ?)""",
        params=(
            normalized.belief_id,
            normalized.claim_id,
            normalized.perspective_subject_id,
            normalized.consciousness_instance_id,
            normalized.recorded_at,
            _json_dump(normalized.metadata),
        ),
    )


def append_conflict(
    db: sqlite3.Connection,
    conflict: EpistemicConflict,
) -> EpistemicConflict:
    """Append an explicit conflict without silently choosing a winning claim."""

    normalized = replace(conflict, recorded_at=conflict.recorded_at or now_iso())
    if normalized.left_claim_id == normalized.right_claim_id:
        raise ValueError("ConflictRequiresDistinctClaims")
    _require_entity(db, "claim", normalized.left_claim_id)
    _require_entity(db, "claim", normalized.right_claim_id)
    return _append_identity_record(
        db,
        table="memory_epistemic_conflicts",
        id_column="conflict_id",
        identifier=normalized.conflict_id,
        record=normalized,
        row_loader=_conflict_from_row,
        sql="""INSERT INTO memory_epistemic_conflicts (
            conflict_id, left_claim_id, right_claim_id, relation, reason,
            recorded_at, detected_by, metadata_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        params=(
            normalized.conflict_id,
            normalized.left_claim_id,
            normalized.right_claim_id,
            normalized.relation,
            normalized.reason,
            normalized.recorded_at,
            normalized.detected_by,
            _json_dump(normalized.metadata),
        ),
    )


def append_state_event(
    db: sqlite3.Connection,
    event: MemoryStateEvent,
) -> MemoryStateEvent:
    """Append a state event after entity and epistemic-authority validation."""

    normalized = replace(
        event,
        recorded_at=event.recorded_at or now_iso(),
        valid_at=event.valid_at or event.recorded_at or now_iso(),
        authority=event.authority or AuthorityClass.UNKNOWN.value,
    )
    _require_entity(db, normalized.entity_type, normalized.entity_id)
    if normalized.reverses_event_id:
        target = db.execute(
            "SELECT * FROM memory_state_events WHERE event_id = ?",
            (normalized.reverses_event_id,),
        ).fetchone()
        if target is None:
            raise ValueError(f"ReversedEventMissing:{normalized.reverses_event_id}")
        if str(target["entity_type"]) != normalized.entity_type or str(
            target["entity_id"]
        ) != normalized.entity_id:
            raise ValueError("ReversalEntityMismatch")
    if (
        normalized.event_type in {"claim_confirmed", "claim_authoritatively_revised"}
        and normalized.authority not in _CONFIRMING_AUTHORITIES
    ):
        raise PermissionError(
            f"AuthorityCannotConfirmClaim:{normalized.authority}"
        )
    return _append_identity_record(
        db,
        table="memory_state_events",
        id_column="event_id",
        identifier=normalized.event_id,
        record=normalized,
        row_loader=_event_from_row,
        sql="""INSERT INTO memory_state_events (
            event_id, entity_type, entity_id, event_type, actor, authority,
            reason, recorded_at, valid_at, caused_by_event_id,
            reverses_event_id, payload_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        params=(
            normalized.event_id,
            normalized.entity_type,
            normalized.entity_id,
            normalized.event_type,
            normalized.actor,
            normalized.authority,
            normalized.reason,
            normalized.recorded_at,
            normalized.valid_at,
            normalized.caused_by_event_id,
            normalized.reverses_event_id,
            _json_dump(normalized.payload),
        ),
    )


def append_retrieval_episode(
    db: sqlite3.Connection,
    episode: RetrievalEpisode,
) -> RetrievalEpisode:
    """Append one retrieval context without inferring any semantic relation."""

    normalized = replace(episode, recorded_at=episode.recorded_at or now_iso())
    return _append_identity_record(
        db,
        table="memory_retrieval_episodes",
        id_column="episode_id",
        identifier=normalized.episode_id,
        record=normalized,
        row_loader=_episode_from_row,
        sql="""INSERT INTO memory_retrieval_episodes (
            episode_id, query, mode, consciousness_instance_id, stream_scope,
            recorded_at, metadata_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
        params=(
            normalized.episode_id,
            normalized.query,
            normalized.mode,
            normalized.consciousness_instance_id,
            normalized.stream_scope,
            normalized.recorded_at,
            _json_dump(normalized.metadata),
        ),
    )


def append_retrieval_exposure(
    db: sqlite3.Connection,
    exposure: RetrievalExposure,
) -> RetrievalExposure:
    """Append a displayed candidate after confirming its referenced entity exists."""

    normalized = replace(exposure, recorded_at=exposure.recorded_at or now_iso())
    episode = db.execute(
        "SELECT 1 FROM memory_retrieval_episodes WHERE episode_id = ?",
        (normalized.episode_id,),
    ).fetchone()
    if episode is None:
        raise ValueError(f"RetrievalEpisodeMissing:{normalized.episode_id}")
    _require_entity(db, normalized.entity_type, normalized.entity_id)
    return _append_identity_record(
        db,
        table="memory_retrieval_exposures",
        id_column="exposure_id",
        identifier=normalized.exposure_id,
        record=normalized,
        row_loader=_exposure_from_row,
        sql="""INSERT INTO memory_retrieval_exposures (
            exposure_id, episode_id, entity_type, entity_id, rank_position,
            retrieval_source, recorded_at, feedback, feedback_reason,
            feedback_at, metadata_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        params=(
            normalized.exposure_id,
            normalized.episode_id,
            normalized.entity_type,
            normalized.entity_id,
            normalized.rank_position,
            normalized.retrieval_source,
            normalized.recorded_at,
            normalized.feedback,
            normalized.feedback_reason,
            normalized.feedback_at,
            _json_dump(normalized.metadata),
        ),
    )


def append_retrieval_feedback(
    db: sqlite3.Connection,
    feedback: RetrievalFeedback,
) -> RetrievalFeedback:
    """Append accepted/rejected/corrected feedback without updating factual state."""

    normalized = replace(feedback, recorded_at=feedback.recorded_at or now_iso())
    exposure = db.execute(
        "SELECT 1 FROM memory_retrieval_exposures WHERE exposure_id = ?",
        (normalized.exposure_id,),
    ).fetchone()
    if exposure is None:
        raise ValueError(f"RetrievalExposureMissing:{normalized.exposure_id}")
    if normalized.feedback not in {"accepted", "rejected", "corrected"}:
        raise ValueError(f"UnsupportedRetrievalFeedback:{normalized.feedback}")
    return _append_identity_record(
        db,
        table="memory_retrieval_feedback",
        id_column="feedback_id",
        identifier=normalized.feedback_id,
        record=normalized,
        row_loader=_feedback_from_row,
        sql="""INSERT INTO memory_retrieval_feedback (
            feedback_id, exposure_id, feedback, actor, reason, recorded_at,
            metadata_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
        params=(
            normalized.feedback_id,
            normalized.exposure_id,
            normalized.feedback,
            normalized.actor,
            normalized.reason,
            normalized.recorded_at,
            _json_dump(normalized.metadata),
        ),
    )


def get_retrieval_plasticity(
    db: sqlite3.Connection,
    entity_type: str,
    entity_id: str,
) -> RetrievalPlasticity:
    """Derive a bounded retrieval hint; it never changes claim status or authority."""

    rows = db.execute(
        """SELECT f.feedback, f.recorded_at FROM memory_retrieval_exposures e
        JOIN memory_retrieval_feedback f ON f.exposure_id = e.exposure_id
        WHERE e.entity_type = ? AND e.entity_id = ?
        ORDER BY f.recorded_at, f.feedback_id""",
        (entity_type, entity_id),
    ).fetchall()
    counts = {"accepted": 0, "rejected": 0, "corrected": 0}
    for row in rows:
        value = str(row["feedback"])
        if value in counts:
            counts[value] += 1
    denominator = max(1, counts["accepted"] + counts["rejected"] + counts["corrected"])
    affinity = (counts["accepted"] - counts["rejected"]) / denominator
    return RetrievalPlasticity(
        entity_type=entity_type,
        entity_id=entity_id,
        accepted_count=counts["accepted"],
        rejected_count=counts["rejected"],
        corrected_count=counts["corrected"],
        retrieval_affinity=max(-1.0, min(1.0, affinity)),
    )


def list_claim_evidence(
    db: sqlite3.Connection,
    claim_id: str,
) -> list[ClaimEvidence]:
    rows = db.execute(
        """SELECT * FROM memory_claim_evidence WHERE claim_id = ?
        ORDER BY recorded_at, evidence_link_id""",
        (claim_id,),
    ).fetchall()
    return [_evidence_from_row(row) for row in rows]


def list_state_events(
    db: sqlite3.Connection,
    entity_type: str,
    entity_id: str,
    *,
    recorded_as_of: str = "",
) -> list[MemoryStateEvent]:
    params: list[Any] = [entity_type, entity_id]
    clause = ""
    if recorded_as_of:
        clause = " AND recorded_at <= ?"
        params.append(recorded_as_of)
    rows = db.execute(
        f"""SELECT * FROM memory_state_events
        WHERE entity_type = ? AND entity_id = ?{clause}
        ORDER BY recorded_at, event_id""",
        params,
    ).fetchall()
    return [_event_from_row(row) for row in rows]


def build_memory_audit_trail(
    db: sqlite3.Connection,
    entity_type: str,
    entity_id: str,
    *,
    recorded_as_of: str = "",
) -> list[MemoryAuditEntry]:
    """Return the complete event trail, including compensations and causes."""

    events = list_state_events(
        db,
        entity_type,
        entity_id,
        recorded_as_of=recorded_as_of,
    )
    reversed_by: dict[str, list[str]] = {}
    by_id = {event.event_id: event for event in events}
    for event in events:
        if event.reverses_event_id:
            reversed_by.setdefault(event.reverses_event_id, []).append(event.event_id)
    active_ids = {event.event_id for event in _active_events(events)}
    return [
        MemoryAuditEntry(
            event=event,
            active=event.event_id in active_ids,
            reversed_by=tuple(reversed_by.get(event.event_id, ())),
            cause=by_id.get(event.caused_by_event_id),
        )
        for event in events
    ]


def reduce_claim_state(
    claim: MemoryClaim,
    events: Sequence[MemoryStateEvent],
) -> ClaimState:
    """Purely rebuild claim state from the append-only event history."""

    active = _active_events(events)
    status = ClaimStatus.PROPOSED.value
    superseded_by: list[str] = []
    last_changed_at = ""
    for event in active:
        if event.entity_type != "claim" or event.entity_id != claim.claim_id:
            continue
        if event.event_type == "claim_confirmed":
            status = ClaimStatus.CONFIRMED.value
        elif event.event_type == "claim_disputed":
            status = ClaimStatus.DISPUTED.value
        elif event.event_type == "claim_superseded":
            status = ClaimStatus.SUPERSEDED.value
            successor = str(event.payload.get("successor_claim_id", "") or "")
            if successor and successor not in superseded_by:
                superseded_by.append(successor)
        elif event.event_type == "claim_retracted":
            status = ClaimStatus.RETRACTED.value
        elif event.event_type == "claim_restored":
            status = str(
                event.payload.get("status", ClaimStatus.PROPOSED.value)
                or ClaimStatus.PROPOSED.value
            )
        last_changed_at = event.recorded_at
    return ClaimState(
        claim=claim,
        status=status,
        superseded_by=tuple(superseded_by),
        active_event_ids=tuple(item.event_id for item in active),
        last_changed_at=last_changed_at,
    )


def reduce_belief_state(
    belief: MemoryBelief,
    events: Sequence[MemoryStateEvent],
) -> BeliefState:
    """Purely rebuild one perspective's endorsement without changing a claim."""

    active = _active_events(events)
    status = BeliefStatus.UNREVIEWED.value
    last_changed_at = ""
    for event in active:
        if event.entity_type != "belief" or event.entity_id != belief.belief_id:
            continue
        if event.event_type == "belief_endorsed":
            status = BeliefStatus.ENDORSED.value
        elif event.event_type == "belief_rejected":
            status = BeliefStatus.REJECTED.value
        elif event.event_type == "belief_suspended":
            status = BeliefStatus.SUSPENDED.value
        elif event.event_type == "belief_restored":
            status = str(
                event.payload.get("status", BeliefStatus.UNREVIEWED.value)
                or BeliefStatus.UNREVIEWED.value
            )
        last_changed_at = event.recorded_at
    return BeliefState(
        belief=belief,
        status=status,
        active_event_ids=tuple(item.event_id for item in active),
        last_changed_at=last_changed_at,
    )


def reduce_memory_disposition(
    entity_type: str,
    entity_id: str,
    events: Sequence[MemoryStateEvent],
) -> MemoryDisposition:
    """Rebuild independent forgetting dimensions from reversible state events."""

    active = _active_events(events)
    accessibility = "available"
    endorsement = "unreviewed"
    inhibitions: list[str] = []
    salience = 0.5
    visibility = "private"
    last_changed_at = ""
    for event in active:
        if event.entity_type != entity_type or event.entity_id != entity_id:
            continue
        payload = event.payload
        if event.event_type == "accessibility_set":
            accessibility = str(payload.get("accessibility", accessibility) or accessibility)
        elif event.event_type == "endorsement_set":
            endorsement = str(payload.get("endorsement", endorsement) or endorsement)
        elif event.event_type == "context_inhibited":
            context = str(payload.get("context", "") or "")
            if context and context not in inhibitions:
                inhibitions.append(context)
        elif event.event_type == "context_released":
            context = str(payload.get("context", "") or "")
            if context:
                inhibitions = [item for item in inhibitions if item != context]
        elif event.event_type == "narrative_salience_set":
            value = payload.get("narrative_salience", salience)
            try:
                salience = max(0.0, min(1.0, float(value)))
            except (TypeError, ValueError):
                pass
        elif event.event_type == "visibility_set":
            visibility = str(payload.get("visibility", visibility) or visibility)
        elif event.event_type == "disposition_restored":
            accessibility = str(payload.get("accessibility", accessibility) or accessibility)
            endorsement = str(payload.get("endorsement", endorsement) or endorsement)
            contexts = payload.get("contextual_inhibition", inhibitions)
            if isinstance(contexts, list):
                inhibitions = list(dict.fromkeys(str(item) for item in contexts if item))
            value = payload.get("narrative_salience", salience)
            try:
                salience = max(0.0, min(1.0, float(value)))
            except (TypeError, ValueError):
                pass
            visibility = str(payload.get("visibility", visibility) or visibility)
        last_changed_at = event.recorded_at
    return MemoryDisposition(
        entity_type=entity_type,
        entity_id=entity_id,
        accessibility=accessibility,
        endorsement=endorsement,
        contextual_inhibition=tuple(inhibitions),
        narrative_salience=salience,
        visibility=visibility,
        active_event_ids=tuple(item.event_id for item in active),
        last_changed_at=last_changed_at,
    )


def get_memory_disposition(
    db: sqlite3.Connection,
    entity_type: str,
    entity_id: str,
    *,
    recorded_as_of: str = "",
) -> MemoryDisposition:
    """Read an entity's reversible disposition without deleting its record."""

    _require_entity(db, entity_type, entity_id)
    return reduce_memory_disposition(
        entity_type,
        entity_id,
        list_state_events(
            db,
            entity_type,
            entity_id,
            recorded_as_of=recorded_as_of,
        ),
    )


def get_claim_state(
    db: sqlite3.Connection,
    claim_id: str,
    *,
    recorded_as_of: str = "",
) -> ClaimState | None:
    row = db.execute(
        "SELECT * FROM memory_claims WHERE claim_id = ?", (claim_id,)
    ).fetchone()
    if row is None:
        return None
    claim = _claim_from_row(row)
    return reduce_claim_state(
        claim,
        list_state_events(db, "claim", claim_id, recorded_as_of=recorded_as_of),
    )


def list_claim_states(
    db: sqlite3.Connection,
    subject_key: str,
    *,
    recorded_as_of: str = "",
    valid_at: str = "",
    stream_scope: str | None = None,
    visibility: Iterable[str] = ("private",),
) -> list[ClaimState]:
    """List bitemporally eligible claim states without hiding conflicts."""

    visible = tuple(dict.fromkeys(str(item) for item in visibility if item))
    if not visible:
        return []
    marks = ",".join("?" for _ in visible)
    params: list[Any] = [subject_key, *visible]
    clauses = ["subject_key = ?", f"visibility IN ({marks})"]
    if recorded_as_of:
        clauses.append("recorded_at <= ?")
        params.append(recorded_as_of)
    if valid_at:
        clauses.extend(["(valid_from = '' OR valid_from <= ?)", "(valid_to = '' OR valid_to > ?)"])
        params.extend([valid_at, valid_at])
    if stream_scope is None:
        clauses.append("stream_scope = ''")
    else:
        clauses.append("stream_scope IN (?, '')")
        params.append(stream_scope)
    rows = db.execute(
        "SELECT * FROM memory_claims WHERE " + " AND ".join(clauses)
        + " ORDER BY recorded_at, claim_id",
        params,
    ).fetchall()
    return [
        reduce_claim_state(
            _claim_from_row(row),
            list_state_events(
                db,
                "claim",
                str(row["claim_id"]),
                recorded_as_of=recorded_as_of,
            ),
        )
        for row in rows
    ]


def search_epistemic_claims(
    db: sqlite3.Connection,
    query: str,
    *,
    mode: str,
    top_k: int = 5,
    stream_scope: str | None = None,
    visibility: Iterable[str] = ("private",),
    valid_at: str = "",
    recorded_as_of: str = "",
) -> list[ClaimSearchResult]:
    """Lexically recall claims after privacy/time filtering and expose state."""

    query_text = str(query or "").strip()
    visible = tuple(dict.fromkeys(str(item) for item in visibility if item))
    if not query_text or not visible:
        return []
    marks = ",".join("?" for _ in visible)
    clauses = [f"visibility IN ({marks})", "instr(content, ?) > 0"]
    params: list[Any] = [*visible, query_text]
    if recorded_as_of:
        clauses.append("recorded_at <= ?")
        params.append(recorded_as_of)
    if valid_at:
        clauses.extend(["(valid_from = '' OR valid_from <= ?)", "(valid_to = '' OR valid_to > ?)"])
        params.extend([valid_at, valid_at])
    if stream_scope is None:
        clauses.append("stream_scope = ''")
    else:
        clauses.append("stream_scope IN (?, '')")
        params.append(stream_scope)
    params.append(max(1, int(top_k)) * 4)
    rows = db.execute(
        "SELECT * FROM memory_claims WHERE " + " AND ".join(clauses)
        + " ORDER BY recorded_at DESC, claim_id LIMIT ?",
        params,
    ).fetchall()
    results: list[ClaimSearchResult] = []
    for index, row in enumerate(rows):
        claim = _claim_from_row(row)
        state = reduce_claim_state(
            claim,
            list_state_events(
                db,
                "claim",
                claim.claim_id,
                recorded_as_of=recorded_as_of,
            ),
        )
        if mode == "current_fact" and state.status in {
            ClaimStatus.SUPERSEDED.value,
            ClaimStatus.RETRACTED.value,
        }:
            continue
        conflict_rows = db.execute(
            """SELECT * FROM memory_epistemic_conflicts
            WHERE left_claim_id = ? OR right_claim_id = ?
            ORDER BY recorded_at, conflict_id""",
            (claim.claim_id, claim.claim_id),
        ).fetchall()
        results.append(
            ClaimSearchResult(
                state=state,
                rank_score=1.0 / (1.0 + index),
                evidence=tuple(list_claim_evidence(db, claim.claim_id)),
                conflicts=tuple(_conflict_from_row(item) for item in conflict_rows),
                plasticity=get_retrieval_plasticity(db, "claim", claim.claim_id),
            )
        )
    results.sort(
        key=lambda item: (
            -item.rank_score,
            -(
                item.plasticity.retrieval_affinity
                if item.plasticity is not None
                else 0.0
            ),
            item.state.claim.recorded_at,
        )
    )
    return results[: max(1, int(top_k))]


def project_current_facts(
    db: sqlite3.Connection,
    subject_key: str,
    *,
    valid_at: str,
    recorded_as_of: str = "",
    stream_scope: str | None = None,
    visibility: Iterable[str] = ("private",),
) -> CurrentFactProjection:
    """Rebuild current facts without discarding disputed or successor history.

    A claim is eligible only when it was valid in the requested world-time and
    had been recorded by the requested knowledge-time. Superseded and retracted
    claims remain retrievable historically but are absent from the active view.
    Conflicts are returned alongside eligible claims rather than silently solved.
    """

    states = list_claim_states(
        db,
        subject_key,
        recorded_as_of=recorded_as_of,
        valid_at=valid_at,
        stream_scope=stream_scope,
        visibility=visibility,
    )
    active = tuple(
        state
        for state in states
        if state.status not in {
            ClaimStatus.SUPERSEDED.value,
            ClaimStatus.RETRACTED.value,
        }
    )
    active_ids = {state.claim.claim_id for state in active}
    if not active_ids:
        return CurrentFactProjection(
            subject_key=subject_key,
            valid_at=valid_at,
            recorded_as_of=recorded_as_of,
            active_claims=(),
            conflicts=(),
            uncertainty=("没有满足当前双时间条件的主张。",),
        )
    marks = ",".join("?" for _ in active_ids)
    params: list[Any] = [*active_ids, *active_ids]
    clauses = [f"left_claim_id IN ({marks})", f"right_claim_id IN ({marks})"]
    if recorded_as_of:
        clauses.append("recorded_at <= ?")
        params.append(recorded_as_of)
    rows = db.execute(
        "SELECT * FROM memory_epistemic_conflicts WHERE "
        + " AND ".join(clauses)
        + " ORDER BY recorded_at, conflict_id",
        params,
    ).fetchall()
    conflicts = tuple(_conflict_from_row(row) for row in rows)
    uncertainty: list[str] = []
    if conflicts:
        uncertainty.append("当前有效主张之间存在未裁决冲突。")
    if any(state.status == ClaimStatus.DISPUTED.value for state in active):
        uncertainty.append("至少一条当前有效主张处于争议状态。")
    if len(active) > 1 and not conflicts:
        uncertainty.append("存在多个当前有效主张；它们不被系统自动合并。")
    return CurrentFactProjection(
        subject_key=subject_key,
        valid_at=valid_at,
        recorded_as_of=recorded_as_of,
        active_claims=active,
        conflicts=conflicts,
        uncertainty=tuple(uncertainty),
    )


def new_claim(
    *,
    subject_key: str,
    content: str,
    claim_kind: str,
    source: str,
    valid_from: str = "",
    valid_to: str = "",
    stream_scope: str = "",
    visibility: str = "private",
    consciousness_instance_id: str = "",
    metadata: dict[str, Any] | None = None,
    claim_id: str = "",
    recorded_at: str = "",
) -> MemoryClaim:
    """Construct a claim with an explicit provenance authority class."""

    return MemoryClaim(
        claim_id=claim_id or f"clm_{uuid4().hex}",
        subject_key=subject_key,
        content=content,
        claim_kind=claim_kind,
        source=source,
        authority=authority_for_source(source).value,
        valid_from=valid_from,
        valid_to=valid_to,
        recorded_at=recorded_at or now_iso(),
        stream_scope=stream_scope,
        visibility=visibility,
        consciousness_instance_id=consciousness_instance_id,
        metadata=metadata or {},
    )


def _active_events(events: Sequence[MemoryStateEvent]) -> list[MemoryStateEvent]:
    ordered = sorted(events, key=lambda item: (item.recorded_at, item.event_id))
    reversed_ids: set[str] = set()
    active_reversed: list[MemoryStateEvent] = []
    for event in reversed(ordered):
        if event.event_id in reversed_ids:
            continue
        active_reversed.append(event)
        if event.reverses_event_id:
            reversed_ids.add(event.reverses_event_id)
    return list(reversed(active_reversed))


def _append_identity_record(
    db: sqlite3.Connection,
    *,
    table: str,
    id_column: str,
    identifier: str,
    record: Any,
    row_loader: Any,
    sql: str,
    params: Sequence[Any],
) -> Any:
    if not identifier:
        raise ValueError(f"{id_column}Required")
    with transaction(db):
        row = db.execute(
            f"SELECT * FROM {table} WHERE {id_column} = ?", (identifier,)
        ).fetchone()
        if row is not None:
            persisted = row_loader(row)
            if persisted != record:
                raise ValueError(f"EpistemicIdentityConflict:{identifier}")
            return persisted
        db.execute(sql, params)
    return record


def _require_entity(db: sqlite3.Connection, entity_type: str, entity_id: str) -> None:
    tables = {
        "claim": ("memory_claims", "claim_id"),
        "belief": ("memory_beliefs", "belief_id"),
        "conflict": ("memory_epistemic_conflicts", "conflict_id"),
        "experience": ("memory_experiences", "event_id"),
        "witness": ("memory_witnesses", "witness_id"),
    }
    location = tables.get(entity_type)
    if location is None:
        raise ValueError(f"UnsupportedEpistemicEntity:{entity_type}")
    table, column = location
    row = db.execute(
        f"SELECT 1 FROM {table} WHERE {column} = ?", (entity_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"EpistemicEntityMissing:{entity_type}:{entity_id}")


def _json_dump(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _json_dict(value: str | None) -> dict[str, Any]:
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _episode_from_row(row: sqlite3.Row) -> RetrievalEpisode:
    return RetrievalEpisode(
        episode_id=str(row["episode_id"]),
        query=str(row["query"]),
        mode=str(row["mode"]),
        consciousness_instance_id=str(row["consciousness_instance_id"]),
        stream_scope=str(row["stream_scope"]),
        recorded_at=str(row["recorded_at"]),
        metadata=_json_dict(row["metadata_json"]),
    )


def _exposure_from_row(row: sqlite3.Row) -> RetrievalExposure:
    return RetrievalExposure(
        exposure_id=str(row["exposure_id"]),
        episode_id=str(row["episode_id"]),
        entity_type=str(row["entity_type"]),
        entity_id=str(row["entity_id"]),
        rank_position=int(row["rank_position"]),
        retrieval_source=str(row["retrieval_source"]),
        recorded_at=str(row["recorded_at"]),
        feedback=str(row["feedback"]),
        feedback_reason=str(row["feedback_reason"]),
        feedback_at=str(row["feedback_at"]),
        metadata=_json_dict(row["metadata_json"]),
    )


def _feedback_from_row(row: sqlite3.Row) -> RetrievalFeedback:
    return RetrievalFeedback(
        feedback_id=str(row["feedback_id"]),
        exposure_id=str(row["exposure_id"]),
        feedback=str(row["feedback"]),
        actor=str(row["actor"]),
        reason=str(row["reason"]),
        recorded_at=str(row["recorded_at"]),
        metadata=_json_dict(row["metadata_json"]),
    )


def _claim_from_row(row: sqlite3.Row) -> MemoryClaim:
    return MemoryClaim(
        claim_id=str(row["claim_id"]),
        subject_key=str(row["subject_key"]),
        content=str(row["content"]),
        claim_kind=str(row["claim_kind"]),
        source=str(row["source"]),
        authority=str(row["authority"]),
        valid_from=str(row["valid_from"]),
        valid_to=str(row["valid_to"]),
        recorded_at=str(row["recorded_at"]),
        stream_scope=str(row["stream_scope"]),
        visibility=str(row["visibility"]),
        consciousness_instance_id=str(row["consciousness_instance_id"]),
        metadata=_json_dict(row["metadata_json"]),
    )


def _evidence_from_row(row: sqlite3.Row) -> ClaimEvidence:
    return ClaimEvidence(
        evidence_link_id=str(row["evidence_link_id"]),
        claim_id=str(row["claim_id"]),
        evidence_kind=str(row["evidence_kind"]),
        evidence_ref=str(row["evidence_ref"]),
        stance=str(row["stance"]),
        source_excerpt=str(row["source_excerpt"]),
        recorded_at=str(row["recorded_at"]),
        metadata=_json_dict(row["metadata_json"]),
    )


def _belief_from_row(row: sqlite3.Row) -> MemoryBelief:
    return MemoryBelief(
        belief_id=str(row["belief_id"]),
        claim_id=str(row["claim_id"]),
        perspective_subject_id=str(row["perspective_subject_id"]),
        consciousness_instance_id=str(row["consciousness_instance_id"]),
        recorded_at=str(row["recorded_at"]),
        metadata=_json_dict(row["metadata_json"]),
    )


def _conflict_from_row(row: sqlite3.Row) -> EpistemicConflict:
    return EpistemicConflict(
        conflict_id=str(row["conflict_id"]),
        left_claim_id=str(row["left_claim_id"]),
        right_claim_id=str(row["right_claim_id"]),
        relation=str(row["relation"]),
        reason=str(row["reason"]),
        recorded_at=str(row["recorded_at"]),
        detected_by=str(row["detected_by"]),
        metadata=_json_dict(row["metadata_json"]),
    )


def _event_from_row(row: sqlite3.Row) -> MemoryStateEvent:
    return MemoryStateEvent(
        event_id=str(row["event_id"]),
        entity_type=str(row["entity_type"]),
        entity_id=str(row["entity_id"]),
        event_type=str(row["event_type"]),
        actor=str(row["actor"]),
        authority=str(row["authority"]),
        reason=str(row["reason"]),
        recorded_at=str(row["recorded_at"]),
        valid_at=str(row["valid_at"]),
        caused_by_event_id=str(row["caused_by_event_id"]),
        reverses_event_id=str(row["reverses_event_id"]),
        payload=_json_dict(row["payload_json"]),
    )


__all__ = [
    "AuthorityClass",
    "BeliefState",
    "BeliefStatus",
    "ClaimEvidence",
    "ClaimSearchResult",
    "ClaimState",
    "ClaimStatus",
    "CurrentFactProjection",
    "EpistemicConflict",
    "EvidenceStance",
    "MemoryAuditEntry",
    "MemoryBelief",
    "MemoryClaim",
    "MemoryDisposition",
    "MemoryStateEvent",
    "RetrievalEpisode",
    "RetrievalExposure",
    "RetrievalFeedback",
    "RetrievalPlasticity",
    "append_belief",
    "append_claim",
    "append_claim_evidence",
    "append_conflict",
    "append_state_event",
    "append_retrieval_episode",
    "append_retrieval_exposure",
    "append_retrieval_feedback",
    "authority_for_source",
    "build_memory_audit_trail",
    "create_epistemic_schema",
    "get_claim_state",
    "get_memory_disposition",
    "get_retrieval_plasticity",
    "list_claim_evidence",
    "list_claim_states",
    "list_state_events",
    "new_claim",
    "now_iso",
    "project_current_facts",
    "reduce_belief_state",
    "search_epistemic_claims",
    "reduce_claim_state",
    "reduce_memory_disposition",
]
