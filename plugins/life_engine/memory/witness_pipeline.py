"""Durable staged records for Experience-to-Witness processing.

The module persists engineering facts only: which immutable occurrences formed
a window, which explicit decision was recorded for that window, and which
delivery work remains. It never decides whether content is meaningful, true,
important, or worthy of projection.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any

from src.kernel.storage import CursorConflict, canonical_json

from .experience import ExperienceOccurrenceRef, get_experience_occurrence
from .indexing import transaction

DELIVERY_KINDS = frozenset({"world", "projection"})
DELIVERY_STATUSES = frozenset({"pending", "processing", "succeeded", "failed"})
WITNESS_DECISION_KINDS = frozenset({"witness", "no_witness"})


class WitnessPipelineConflict(RuntimeError):
    """An immutable pipeline identity was replayed with different evidence."""


@dataclass(frozen=True, slots=True)
class WitnessWindow:
    """Immutable ordered occurrence window offered to a witness worker."""

    window_id: str
    consciousness_instance_id: str
    start_position: int
    end_position: int
    occurrences: tuple[ExperienceOccurrenceRef, ...]
    created_at: str
    stream_scope: str = ""
    planner_version: str = ""
    source_digest: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    payload_sha256: str = ""


@dataclass(frozen=True, slots=True)
class WitnessDecision:
    """Immutable explicit outcome authored for one immutable window."""

    decision_id: str
    window_id: str
    consciousness_instance_id: str
    decision_kind: str
    decided_at: str
    witness_id: str = ""
    model_task_name: str = ""
    model_request_id: str = ""
    response_sha256: str = ""
    delivery_manifest_sha256: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    payload_sha256: str = ""


@dataclass(frozen=True, slots=True)
class WitnessDeliveryJob:
    """Immutable delivery payload plus revisioned technical outbox state."""

    job_id: str
    decision_id: str
    window_id: str
    delivery_kind: str
    payload: dict[str, Any]
    payload_sha256: str
    created_at: str
    status: str = "pending"
    revision: int = 0
    attempt_count: int = 0
    available_at: str = ""
    lease_owner: str = ""
    lease_expires_at: str = ""
    last_error_type: str = ""
    updated_at: str = ""
    completed_at: str = ""


def _now_iso() -> str:
    return datetime.now(UTC).astimezone().isoformat()


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _json_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    try:
        loaded = json.loads(str(value or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return dict(loaded) if isinstance(loaded, dict) else {}


def occurrence_identity_body(ref: ExperienceOccurrenceRef) -> dict[str, Any]:
    """Return the content-free immutable identity bound into a window."""

    return {
        "occurrence_id": ref.occurrence_id,
        "canonical_event_id": ref.canonical_event_id,
        "source_event_id": ref.source_event_id,
        "ingest_position": int(ref.ingest_position),
        "recorded_at": ref.recorded_at,
        "canonical_payload_sha256": ref.canonical_payload_sha256,
        "is_alias": bool(ref.is_alias),
    }


def witness_window_source_digest(
    occurrences: Sequence[ExperienceOccurrenceRef],
) -> str:
    return _sha256(
        canonical_json([occurrence_identity_body(item) for item in occurrences])
    )


def _window_body(window: WitnessWindow) -> dict[str, Any]:
    return {
        "window_id": window.window_id,
        "consciousness_instance_id": window.consciousness_instance_id,
        "stream_scope": window.stream_scope,
        "start_position": int(window.start_position),
        "end_position": int(window.end_position),
        "occurrence_count": len(window.occurrences),
        "source_digest": window.source_digest,
        "planner_version": window.planner_version,
        "created_at": window.created_at,
        "metadata": window.metadata,
    }


def normalize_witness_window(window: WitnessWindow) -> WitnessWindow:
    window_id = str(window.window_id or "").strip()
    instance_id = str(window.consciousness_instance_id or "").strip()
    occurrences = tuple(window.occurrences)
    created_at = str(window.created_at or "").strip()
    if not window_id or not instance_id:
        raise ValueError("WitnessWindowIdentityRequired")
    if not created_at:
        raise ValueError("WitnessWindowCreatedAtRequired")
    if not occurrences:
        raise ValueError("WitnessWindowOccurrencesRequired")
    if len({item.occurrence_id for item in occurrences}) != len(occurrences):
        raise ValueError("WitnessWindowOccurrenceDuplicate")
    ordering = tuple((item.ingest_position, item.occurrence_id) for item in occurrences)
    if ordering != tuple(sorted(ordering)):
        raise ValueError("WitnessWindowOccurrencesOutOfOrder")
    start_position = int(window.start_position)
    end_position = int(window.end_position)
    if start_position != occurrences[0].ingest_position:
        raise ValueError("WitnessWindowStartMismatch")
    if end_position != occurrences[-1].ingest_position or end_position < start_position:
        raise ValueError("WitnessWindowEndMismatch")
    source_digest = witness_window_source_digest(occurrences)
    if window.source_digest and window.source_digest != source_digest:
        raise WitnessPipelineConflict("WitnessWindowSourceDigestConflict")
    normalized = replace(
        window,
        window_id=window_id,
        consciousness_instance_id=instance_id,
        start_position=start_position,
        end_position=end_position,
        occurrences=occurrences,
        created_at=created_at,
        source_digest=source_digest,
        metadata=dict(window.metadata),
        payload_sha256="",
    )
    payload_sha256 = _sha256(canonical_json(_window_body(normalized)))
    if window.payload_sha256 and window.payload_sha256 != payload_sha256:
        raise WitnessPipelineConflict("WitnessWindowPayloadConflict")
    return replace(normalized, payload_sha256=payload_sha256)


def _delivery_manifest(
    delivery_payloads: Mapping[str, Mapping[str, Any]],
) -> tuple[dict[str, dict[str, Any]], str]:
    normalized: dict[str, dict[str, Any]] = {}
    manifest: list[dict[str, str]] = []
    for raw_kind, raw_payload in sorted(delivery_payloads.items()):
        kind = str(raw_kind or "").strip()
        if kind not in DELIVERY_KINDS:
            raise ValueError(f"WitnessDeliveryKindUnsupported:{kind}")
        payload = dict(raw_payload)
        normalized[kind] = payload
        manifest.append(
            {"delivery_kind": kind, "payload_sha256": _sha256(canonical_json(payload))}
        )
    return normalized, _sha256(canonical_json(manifest))


def _decision_body(decision: WitnessDecision) -> dict[str, Any]:
    return {
        "decision_id": decision.decision_id,
        "window_id": decision.window_id,
        "consciousness_instance_id": decision.consciousness_instance_id,
        "decision_kind": decision.decision_kind,
        "witness_id": decision.witness_id,
        "model_task_name": decision.model_task_name,
        "model_request_id": decision.model_request_id,
        "response_sha256": decision.response_sha256,
        "delivery_manifest_sha256": decision.delivery_manifest_sha256,
        "decided_at": decision.decided_at,
        "metadata": decision.metadata,
    }


def normalize_witness_decision(
    decision: WitnessDecision,
    delivery_payloads: Mapping[str, Mapping[str, Any]],
) -> tuple[WitnessDecision, dict[str, dict[str, Any]]]:
    decision_id = str(decision.decision_id or "").strip()
    window_id = str(decision.window_id or "").strip()
    instance_id = str(decision.consciousness_instance_id or "").strip()
    decision_kind = str(decision.decision_kind or "").strip()
    decided_at = str(decision.decided_at or "").strip()
    if not decision_id or not window_id or not instance_id or not decision_kind:
        raise ValueError("WitnessDecisionIdentityRequired")
    if decision_kind not in WITNESS_DECISION_KINDS:
        raise ValueError(f"WitnessDecisionKindUnsupported:{decision_kind}")
    if decision_kind == "witness" and not str(decision.witness_id or "").strip():
        raise ValueError("WitnessDecisionWitnessIdentityRequired")
    if decision_kind == "no_witness" and str(decision.witness_id or "").strip():
        raise ValueError("NoWitnessDecisionMustNotReferenceWitness")
    if not decided_at:
        raise ValueError("WitnessDecisionDecidedAtRequired")
    payloads, manifest_sha256 = _delivery_manifest(delivery_payloads)
    if (
        decision.delivery_manifest_sha256
        and decision.delivery_manifest_sha256 != manifest_sha256
    ):
        raise WitnessPipelineConflict("WitnessDecisionDeliveryManifestConflict")
    normalized = replace(
        decision,
        decision_id=decision_id,
        window_id=window_id,
        consciousness_instance_id=instance_id,
        decision_kind=decision_kind,
        decided_at=decided_at,
        delivery_manifest_sha256=manifest_sha256,
        metadata=dict(decision.metadata),
        payload_sha256="",
    )
    payload_sha256 = _sha256(canonical_json(_decision_body(normalized)))
    if decision.payload_sha256 and decision.payload_sha256 != payload_sha256:
        raise WitnessPipelineConflict("WitnessDecisionPayloadConflict")
    return replace(normalized, payload_sha256=payload_sha256), payloads


def build_delivery_jobs(
    decision: WitnessDecision,
    delivery_payloads: Mapping[str, Mapping[str, Any]],
) -> tuple[WitnessDeliveryJob, ...]:
    jobs: list[WitnessDeliveryJob] = []
    for kind, payload in sorted(delivery_payloads.items()):
        payload_dict = dict(payload)
        jobs.append(
            WitnessDeliveryJob(
                job_id="delivery-" + _sha256(f"{decision.decision_id}:{kind}"),
                decision_id=decision.decision_id,
                window_id=decision.window_id,
                delivery_kind=kind,
                payload=payload_dict,
                payload_sha256=_sha256(canonical_json(payload_dict)),
                created_at=decision.decided_at,
                updated_at=decision.decided_at,
            )
        )
    return tuple(jobs)


def create_witness_pipeline_schema(db: sqlite3.Connection) -> None:
    """Create the local durable pipeline schema and authority guards."""

    db.row_factory = sqlite3.Row
    with transaction(db):
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS memory_witness_windows (
                window_id TEXT PRIMARY KEY,
                consciousness_instance_id TEXT NOT NULL,
                stream_scope TEXT NOT NULL DEFAULT '',
                start_position INTEGER NOT NULL,
                end_position INTEGER NOT NULL,
                occurrence_count INTEGER NOT NULL,
                source_digest TEXT NOT NULL,
                planner_version TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                payload_sha256 TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_witness_windows_pending
                ON memory_witness_windows(
                    consciousness_instance_id, start_position, window_id
                );
            CREATE TRIGGER IF NOT EXISTS memory_witness_windows_immutable_update
            BEFORE UPDATE ON memory_witness_windows BEGIN
                SELECT RAISE(ABORT, 'WitnessWindowImmutable');
            END;
            CREATE TRIGGER IF NOT EXISTS memory_witness_windows_immutable_delete
            BEFORE DELETE ON memory_witness_windows BEGIN
                SELECT RAISE(ABORT, 'WitnessWindowImmutable');
            END;

            CREATE TABLE IF NOT EXISTS memory_witness_window_sources (
                window_id TEXT NOT NULL,
                ordinal INTEGER NOT NULL,
                occurrence_id TEXT NOT NULL,
                canonical_event_id TEXT NOT NULL,
                source_event_id TEXT NOT NULL,
                ingest_position INTEGER NOT NULL,
                occurrence_recorded_at TEXT NOT NULL,
                canonical_payload_sha256 TEXT NOT NULL,
                is_alias INTEGER NOT NULL,
                PRIMARY KEY (window_id, ordinal),
                UNIQUE (window_id, occurrence_id),
                FOREIGN KEY (window_id) REFERENCES memory_witness_windows(window_id)
                    ON DELETE RESTRICT,
                FOREIGN KEY (canonical_event_id) REFERENCES memory_experiences(event_id)
                    ON DELETE RESTRICT
            );
            CREATE INDEX IF NOT EXISTS idx_witness_window_source_occurrence
                ON memory_witness_window_sources(occurrence_id, window_id);
            CREATE TRIGGER IF NOT EXISTS memory_witness_window_sources_immutable_update
            BEFORE UPDATE ON memory_witness_window_sources BEGIN
                SELECT RAISE(ABORT, 'WitnessWindowSourceImmutable');
            END;
            CREATE TRIGGER IF NOT EXISTS memory_witness_window_sources_immutable_delete
            BEFORE DELETE ON memory_witness_window_sources BEGIN
                SELECT RAISE(ABORT, 'WitnessWindowSourceImmutable');
            END;

            CREATE TABLE IF NOT EXISTS memory_witness_decisions (
                decision_id TEXT PRIMARY KEY,
                window_id TEXT NOT NULL UNIQUE,
                consciousness_instance_id TEXT NOT NULL,
                decision_kind TEXT NOT NULL,
                witness_id TEXT NOT NULL DEFAULT '',
                model_task_name TEXT NOT NULL DEFAULT '',
                model_request_id TEXT NOT NULL DEFAULT '',
                response_sha256 TEXT NOT NULL DEFAULT '',
                delivery_manifest_sha256 TEXT NOT NULL,
                decided_at TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                payload_sha256 TEXT NOT NULL,
                FOREIGN KEY (window_id) REFERENCES memory_witness_windows(window_id)
                    ON DELETE RESTRICT
            );
            CREATE INDEX IF NOT EXISTS idx_witness_decisions_instance
                ON memory_witness_decisions(consciousness_instance_id, decided_at);
            CREATE TRIGGER IF NOT EXISTS memory_witness_decisions_immutable_update
            BEFORE UPDATE ON memory_witness_decisions BEGIN
                SELECT RAISE(ABORT, 'WitnessDecisionImmutable');
            END;
            CREATE TRIGGER IF NOT EXISTS memory_witness_decisions_immutable_delete
            BEFORE DELETE ON memory_witness_decisions BEGIN
                SELECT RAISE(ABORT, 'WitnessDecisionImmutable');
            END;

            CREATE TABLE IF NOT EXISTS memory_witness_delivery_jobs (
                job_id TEXT PRIMARY KEY,
                decision_id TEXT NOT NULL,
                window_id TEXT NOT NULL,
                delivery_kind TEXT NOT NULL CHECK (
                    delivery_kind IN ('world', 'projection')
                ),
                payload_json TEXT NOT NULL,
                payload_sha256 TEXT NOT NULL,
                created_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending' CHECK (
                    status IN ('pending', 'processing', 'succeeded', 'failed')
                ),
                revision INTEGER NOT NULL DEFAULT 0,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                available_at TEXT NOT NULL DEFAULT '',
                lease_owner TEXT NOT NULL DEFAULT '',
                lease_expires_at TEXT NOT NULL DEFAULT '',
                last_error_type TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL,
                completed_at TEXT NOT NULL DEFAULT '',
                UNIQUE (decision_id, delivery_kind),
                FOREIGN KEY (decision_id) REFERENCES memory_witness_decisions(decision_id)
                    ON DELETE RESTRICT,
                FOREIGN KEY (window_id) REFERENCES memory_witness_windows(window_id)
                    ON DELETE RESTRICT
            );
            CREATE INDEX IF NOT EXISTS idx_witness_delivery_pending
                ON memory_witness_delivery_jobs(
                    delivery_kind, status, available_at, created_at, job_id
                );
            CREATE TRIGGER IF NOT EXISTS memory_witness_delivery_authority_immutable_update
            BEFORE UPDATE ON memory_witness_delivery_jobs
            WHEN OLD.job_id IS NOT NEW.job_id
              OR OLD.decision_id IS NOT NEW.decision_id
              OR OLD.window_id IS NOT NEW.window_id
              OR OLD.delivery_kind IS NOT NEW.delivery_kind
              OR OLD.payload_json IS NOT NEW.payload_json
              OR OLD.payload_sha256 IS NOT NEW.payload_sha256
              OR OLD.created_at IS NOT NEW.created_at
            BEGIN
                SELECT RAISE(ABORT, 'WitnessDeliveryAuthorityImmutable');
            END;
            CREATE TRIGGER IF NOT EXISTS memory_witness_delivery_immutable_delete
            BEFORE DELETE ON memory_witness_delivery_jobs BEGIN
                SELECT RAISE(ABORT, 'WitnessDeliveryAuthorityImmutable');
            END;
            """
        )


def _window_from_row(db: sqlite3.Connection, row: sqlite3.Row) -> WitnessWindow:
    source_rows = db.execute(
        """SELECT occurrence_id FROM memory_witness_window_sources
        WHERE window_id = ? ORDER BY ordinal""",
        (str(row["window_id"]),),
    ).fetchall()
    occurrences: list[ExperienceOccurrenceRef] = []
    for source in source_rows:
        ref = get_experience_occurrence(db, str(source["occurrence_id"]))
        if ref is None:
            raise RuntimeError(
                f"WitnessWindowOccurrenceMissing:{source['occurrence_id']}"
            )
        occurrences.append(ref)
    return normalize_witness_window(
        WitnessWindow(
            window_id=str(row["window_id"]),
            consciousness_instance_id=str(row["consciousness_instance_id"]),
            stream_scope=str(row["stream_scope"]),
            start_position=int(row["start_position"]),
            end_position=int(row["end_position"]),
            occurrences=tuple(occurrences),
            source_digest=str(row["source_digest"]),
            planner_version=str(row["planner_version"]),
            created_at=str(row["created_at"]),
            metadata=_json_dict(row["metadata_json"]),
            payload_sha256=str(row["payload_sha256"]),
        )
    )


def append_witness_window(
    db: sqlite3.Connection,
    window: WitnessWindow,
) -> WitnessWindow:
    normalized = normalize_witness_window(window)
    with transaction(db):
        existing = db.execute(
            "SELECT * FROM memory_witness_windows WHERE window_id = ?",
            (normalized.window_id,),
        ).fetchone()
        if existing is not None:
            persisted = _window_from_row(db, existing)
            if persisted.payload_sha256 != normalized.payload_sha256:
                raise WitnessPipelineConflict(
                    f"WitnessWindowIdentityConflict:{normalized.window_id}"
                )
            return persisted
        for occurrence in normalized.occurrences:
            persisted_occurrence = get_experience_occurrence(
                db, occurrence.occurrence_id
            )
            if persisted_occurrence is None:
                raise ValueError(
                    f"WitnessWindowOccurrenceMissing:{occurrence.occurrence_id}"
                )
            if occurrence_identity_body(persisted_occurrence) != occurrence_identity_body(
                occurrence
            ):
                raise WitnessPipelineConflict(
                    f"WitnessWindowOccurrenceConflict:{occurrence.occurrence_id}"
                )
        db.execute(
            """INSERT INTO memory_witness_windows (
                window_id, consciousness_instance_id, stream_scope,
                start_position, end_position, occurrence_count, source_digest,
                planner_version, created_at, metadata_json, payload_sha256
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                normalized.window_id,
                normalized.consciousness_instance_id,
                normalized.stream_scope,
                normalized.start_position,
                normalized.end_position,
                len(normalized.occurrences),
                normalized.source_digest,
                normalized.planner_version,
                normalized.created_at,
                canonical_json(normalized.metadata),
                normalized.payload_sha256,
            ),
        )
        db.executemany(
            """INSERT INTO memory_witness_window_sources (
                window_id, ordinal, occurrence_id, canonical_event_id,
                source_event_id, ingest_position, occurrence_recorded_at,
                canonical_payload_sha256, is_alias
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    normalized.window_id,
                    ordinal,
                    item.occurrence_id,
                    item.canonical_event_id,
                    item.source_event_id,
                    item.ingest_position,
                    item.recorded_at,
                    item.canonical_payload_sha256,
                    int(item.is_alias),
                )
                for ordinal, item in enumerate(normalized.occurrences)
            ],
        )
    return normalized


def get_witness_window(
    db: sqlite3.Connection,
    window_id: str,
) -> WitnessWindow | None:
    row = db.execute(
        "SELECT * FROM memory_witness_windows WHERE window_id = ?",
        (window_id,),
    ).fetchone()
    return _window_from_row(db, row) if row is not None else None


def next_pending_witness_window(
    db: sqlite3.Connection,
    consciousness_instance_id: str | None = None,
) -> WitnessWindow | None:
    clause = ""
    params: list[Any] = []
    if consciousness_instance_id is not None:
        clause = "AND w.consciousness_instance_id = ?"
        params.append(consciousness_instance_id)
    row = db.execute(
        f"""SELECT w.* FROM memory_witness_windows AS w
        WHERE NOT EXISTS (
            SELECT 1 FROM memory_witness_decisions AS d
            WHERE d.window_id = w.window_id
        ) {clause}
        ORDER BY w.start_position, w.window_id LIMIT 1""",
        params,
    ).fetchone()
    return _window_from_row(db, row) if row is not None else None


def _decision_from_row(row: sqlite3.Row) -> WitnessDecision:
    decision = WitnessDecision(
        decision_id=str(row["decision_id"]),
        window_id=str(row["window_id"]),
        consciousness_instance_id=str(row["consciousness_instance_id"]),
        decision_kind=str(row["decision_kind"]),
        witness_id=str(row["witness_id"]),
        model_task_name=str(row["model_task_name"]),
        model_request_id=str(row["model_request_id"]),
        response_sha256=str(row["response_sha256"]),
        delivery_manifest_sha256=str(row["delivery_manifest_sha256"]),
        decided_at=str(row["decided_at"]),
        metadata=_json_dict(row["metadata_json"]),
        payload_sha256=str(row["payload_sha256"]),
    )
    if _sha256(canonical_json(_decision_body(decision))) != decision.payload_sha256:
        raise WitnessPipelineConflict(
            f"WitnessDecisionPayloadDrift:{decision.decision_id}"
        )
    return decision


def _job_from_row(row: sqlite3.Row) -> WitnessDeliveryJob:
    job = WitnessDeliveryJob(
        job_id=str(row["job_id"]),
        decision_id=str(row["decision_id"]),
        window_id=str(row["window_id"]),
        delivery_kind=str(row["delivery_kind"]),
        payload=_json_dict(row["payload_json"]),
        payload_sha256=str(row["payload_sha256"]),
        created_at=str(row["created_at"]),
        status=str(row["status"]),
        revision=int(row["revision"]),
        attempt_count=int(row["attempt_count"]),
        available_at=str(row["available_at"]),
        lease_owner=str(row["lease_owner"]),
        lease_expires_at=str(row["lease_expires_at"]),
        last_error_type=str(row["last_error_type"]),
        updated_at=str(row["updated_at"]),
        completed_at=str(row["completed_at"]),
    )
    if job.delivery_kind not in DELIVERY_KINDS or job.status not in DELIVERY_STATUSES:
        raise WitnessPipelineConflict(f"WitnessDeliveryStateDrift:{job.job_id}")
    if _sha256(canonical_json(job.payload)) != job.payload_sha256:
        raise WitnessPipelineConflict(f"WitnessDeliveryPayloadDrift:{job.job_id}")
    return job


def append_witness_decision(
    db: sqlite3.Connection,
    decision: WitnessDecision,
    *,
    delivery_payloads: Mapping[str, Mapping[str, Any]],
) -> WitnessDecision:
    normalized, payloads = normalize_witness_decision(decision, delivery_payloads)
    jobs = build_delivery_jobs(normalized, payloads)
    with transaction(db):
        window = db.execute(
            "SELECT consciousness_instance_id FROM memory_witness_windows WHERE window_id = ?",
            (normalized.window_id,),
        ).fetchone()
        if window is None:
            raise ValueError(f"WitnessDecisionWindowMissing:{normalized.window_id}")
        if str(window["consciousness_instance_id"]) != normalized.consciousness_instance_id:
            raise WitnessPipelineConflict("WitnessDecisionConsciousnessConflict")
        existing = db.execute(
            "SELECT * FROM memory_witness_decisions WHERE decision_id = ?",
            (normalized.decision_id,),
        ).fetchone()
        if existing is None:
            existing = db.execute(
                "SELECT * FROM memory_witness_decisions WHERE window_id = ?",
                (normalized.window_id,),
            ).fetchone()
        if existing is not None:
            persisted = _decision_from_row(existing)
            if (
                persisted.decision_id != normalized.decision_id
                or persisted.payload_sha256 != normalized.payload_sha256
            ):
                raise WitnessPipelineConflict(
                    f"WitnessDecisionIdentityConflict:{normalized.decision_id}"
                )
            rows = db.execute(
                """SELECT delivery_kind, payload_sha256
                FROM memory_witness_delivery_jobs WHERE decision_id = ?
                ORDER BY delivery_kind""",
                (normalized.decision_id,),
            ).fetchall()
            observed = tuple(
                (str(row["delivery_kind"]), str(row["payload_sha256"]))
                for row in rows
            )
            expected = tuple((job.delivery_kind, job.payload_sha256) for job in jobs)
            if observed != expected:
                raise WitnessPipelineConflict(
                    f"WitnessDecisionOutboxConflict:{normalized.decision_id}"
                )
            return persisted
        db.execute(
            """INSERT INTO memory_witness_decisions (
                decision_id, window_id, consciousness_instance_id,
                decision_kind, witness_id, model_task_name, model_request_id,
                response_sha256, delivery_manifest_sha256, decided_at,
                metadata_json, payload_sha256
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                normalized.decision_id,
                normalized.window_id,
                normalized.consciousness_instance_id,
                normalized.decision_kind,
                normalized.witness_id,
                normalized.model_task_name,
                normalized.model_request_id,
                normalized.response_sha256,
                normalized.delivery_manifest_sha256,
                normalized.decided_at,
                canonical_json(normalized.metadata),
                normalized.payload_sha256,
            ),
        )
        db.executemany(
            """INSERT INTO memory_witness_delivery_jobs (
                job_id, decision_id, window_id, delivery_kind, payload_json,
                payload_sha256, created_at, status, revision, attempt_count,
                available_at, lease_owner, lease_expires_at, last_error_type,
                updated_at, completed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    job.job_id,
                    job.decision_id,
                    job.window_id,
                    job.delivery_kind,
                    canonical_json(job.payload),
                    job.payload_sha256,
                    job.created_at,
                    job.status,
                    job.revision,
                    job.attempt_count,
                    job.available_at,
                    job.lease_owner,
                    job.lease_expires_at,
                    job.last_error_type,
                    job.updated_at,
                    job.completed_at,
                )
                for job in jobs
            ],
        )
    return normalized


def get_witness_decision(
    db: sqlite3.Connection,
    decision_id: str,
) -> WitnessDecision | None:
    row = db.execute(
        "SELECT * FROM memory_witness_decisions WHERE decision_id = ?",
        (decision_id,),
    ).fetchone()
    return _decision_from_row(row) if row is not None else None


def list_witness_delivery_jobs(
    db: sqlite3.Connection,
    *,
    delivery_kind: str | None = None,
    statuses: Sequence[str] = ("pending", "failed"),
    limit: int = 100,
) -> list[WitnessDeliveryJob]:
    normalized_statuses = tuple(dict.fromkeys(str(item) for item in statuses))
    if any(status not in DELIVERY_STATUSES for status in normalized_statuses):
        raise ValueError("WitnessDeliveryStatusUnsupported")
    clauses: list[str] = []
    params: list[Any] = []
    if delivery_kind is not None:
        if delivery_kind not in DELIVERY_KINDS:
            raise ValueError(f"WitnessDeliveryKindUnsupported:{delivery_kind}")
        clauses.append("delivery_kind = ?")
        params.append(delivery_kind)
    if normalized_statuses:
        clauses.append("status IN (" + ",".join("?" for _ in normalized_statuses) + ")")
        params.extend(normalized_statuses)
    where = "" if not clauses else "WHERE " + " AND ".join(clauses)
    params.append(max(1, min(int(limit), 1000)))
    rows = db.execute(
        f"""SELECT * FROM memory_witness_delivery_jobs {where}
        ORDER BY created_at, job_id LIMIT ?""",
        params,
    ).fetchall()
    return [_job_from_row(row) for row in rows]


def mark_witness_delivery_job(
    db: sqlite3.Connection,
    job_id: str,
    *,
    expected_revision: int,
    status: str,
    error_type: str = "",
    available_at: str = "",
    lease_owner: str = "",
    lease_expires_at: str = "",
    completed_at: str = "",
) -> WitnessDeliveryJob:
    target_status = str(status or "").strip()
    if target_status not in DELIVERY_STATUSES - {"pending"}:
        raise ValueError(f"WitnessDeliveryStatusUnsupported:{target_status}")
    with transaction(db):
        row = db.execute(
            "SELECT * FROM memory_witness_delivery_jobs WHERE job_id = ?",
            (job_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"WitnessDeliveryJobMissing:{job_id}")
        current = _job_from_row(row)
        if current.revision != int(expected_revision):
            raise CursorConflict(
                f"witness delivery revision changed: expected {expected_revision}, "
                f"actual {current.revision}"
            )
        if current.status == "succeeded":
            raise WitnessPipelineConflict(f"WitnessDeliveryAlreadySucceeded:{job_id}")
        if target_status == "processing" and current.status not in {"pending", "failed"}:
            raise WitnessPipelineConflict(
                f"WitnessDeliveryTransitionConflict:{current.status}->{target_status}"
            )
        if target_status in {"succeeded", "failed"} and current.status not in {
            "pending",
            "processing",
            "failed",
        }:
            raise WitnessPipelineConflict(
                f"WitnessDeliveryTransitionConflict:{current.status}->{target_status}"
            )
        if target_status == "failed" and not str(error_type or "").strip():
            raise ValueError("WitnessDeliveryFailureErrorTypeRequired")
        now = _now_iso()
        attempt_count = current.attempt_count + int(
            target_status == "processing" or current.status != "processing"
        )
        next_completed_at = (
            completed_at or now if target_status == "succeeded" else ""
        )
        next_lease_owner = lease_owner if target_status == "processing" else ""
        next_lease_expires_at = (
            lease_expires_at if target_status == "processing" else ""
        )
        updated = db.execute(
            """UPDATE memory_witness_delivery_jobs SET
                status = ?, revision = revision + 1, attempt_count = ?,
                available_at = ?, lease_owner = ?, lease_expires_at = ?,
                last_error_type = ?, updated_at = ?, completed_at = ?
            WHERE job_id = ? AND revision = ?""",
            (
                target_status,
                attempt_count,
                available_at,
                next_lease_owner,
                next_lease_expires_at,
                str(error_type or "") if target_status == "failed" else "",
                now,
                next_completed_at,
                job_id,
                int(expected_revision),
            ),
        )
        if updated.rowcount != 1:
            raise CursorConflict("witness delivery changed during CAS")
        persisted = db.execute(
            "SELECT * FROM memory_witness_delivery_jobs WHERE job_id = ?",
            (job_id,),
        ).fetchone()
        assert persisted is not None
        return _job_from_row(persisted)


def list_witness_projection_records(
    db: sqlite3.Connection,
    *,
    statuses: Sequence[str] = (),
    limit: int = 100,
) -> list[WitnessDeliveryJob]:
    return list_witness_delivery_jobs(
        db,
        delivery_kind="projection",
        statuses=statuses,
        limit=limit,
    )


def witness_projection_health(db: sqlite3.Connection) -> dict[str, Any]:
    rows = db.execute(
        """SELECT status, COUNT(*) AS count
        FROM memory_witness_delivery_jobs
        WHERE delivery_kind = 'projection' GROUP BY status"""
    ).fetchall()
    counts = {status: 0 for status in sorted(DELIVERY_STATUSES)}
    counts.update({str(row["status"]): int(row["count"]) for row in rows})
    oldest = db.execute(
        """SELECT MIN(created_at) AS value FROM memory_witness_delivery_jobs
        WHERE delivery_kind = 'projection' AND status IN ('pending', 'failed')"""
    ).fetchone()
    latest = db.execute(
        """SELECT MAX(completed_at) AS value FROM memory_witness_delivery_jobs
        WHERE delivery_kind = 'projection' AND status = 'succeeded'"""
    ).fetchone()
    return {
        "status": "degraded" if counts["failed"] else "healthy",
        "delivery_kind": "projection",
        "counts": counts,
        "total": sum(counts.values()),
        "oldest_pending_at": str(oldest["value"] or "") if oldest else "",
        "latest_completed_at": str(latest["value"] or "") if latest else "",
    }


__all__ = [
    "DELIVERY_KINDS",
    "DELIVERY_STATUSES",
    "WITNESS_DECISION_KINDS",
    "WitnessDecision",
    "WitnessDeliveryJob",
    "WitnessPipelineConflict",
    "WitnessWindow",
    "append_witness_decision",
    "append_witness_window",
    "build_delivery_jobs",
    "create_witness_pipeline_schema",
    "get_witness_decision",
    "get_witness_window",
    "list_witness_delivery_jobs",
    "list_witness_projection_records",
    "mark_witness_delivery_job",
    "next_pending_witness_window",
    "normalize_witness_decision",
    "normalize_witness_window",
    "occurrence_identity_body",
    "witness_projection_health",
    "witness_window_source_digest",
]
