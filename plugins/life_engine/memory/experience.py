"""Experience ledger and first-person witness persistence for Life Engine.

Raw experiences are immutable factual evidence. Witness memories record how one
consciousness instance experienced a bounded evidence window; they are not
promoted to objective truth merely because they were authored later.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import Enum
from typing import Any
from uuid import uuid4

from src.kernel.storage import (
    CursorConflict,
    canonical_json,
    compare_and_advance_cursor,
)

from .indexing import transaction


class MemorySearchMode(str, Enum):
    """Explicit epistemic intent for memory retrieval."""

    CURRENT_FACT = "current_fact"
    AUTOBIOGRAPHICAL = "autobiographical"
    HISTORICAL = "historical"
    EXPLORATORY = "exploratory"


class EpistemicKind(str, Enum):
    """What a record can legitimately claim to represent."""

    OBSERVED_EVENT = "observed_event"
    SUBJECTIVE_WITNESS = "subjective_witness"
    LEGACY_WITNESS = "legacy_witness"
    DOCUMENT_EVIDENCE = "document_evidence"
    SELF_NARRATIVE = "self_narrative"


@dataclass(frozen=True, slots=True)
class ExperienceRecord:
    event_id: str
    sequence: int
    occurred_at: str
    recorded_at: str
    source: str
    channel: str
    event_type: str
    content: str
    source_event_id: str = ""
    stream_id: str = ""
    consciousness_instance_id: str = ""
    actor: str = ""
    visibility: str = "private"
    valid_from: str = ""
    valid_to: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ExperienceOccurrenceRef:
    """One immutable ingest occurrence resolved to canonical evidence."""

    occurrence_id: str
    source_event_id: str
    ingest_position: int
    canonical_event_id: str
    canonical_payload_sha256: str
    recorded_at: str
    experience: ExperienceRecord
    is_alias: bool = False


@dataclass(frozen=True, slots=True, order=True)
class ExperienceOccurrenceCursor:
    """Stable immutable ordering key for occurrence-ledger pagination."""

    ingest_position: int
    occurrence_id: str

    def __post_init__(self) -> None:
        if int(self.ingest_position) < 0:
            raise ValueError("ExperienceOccurrenceCursorPositionInvalid")
        if not str(self.occurrence_id):
            raise ValueError("ExperienceOccurrenceCursorIdentityRequired")


@dataclass(frozen=True, slots=True)
class ExperienceOccurrencePage:
    """One bounded page tied to an immutable composite frontier."""

    items: tuple[ExperienceOccurrenceRef, ...]
    next_cursor: ExperienceOccurrenceCursor | None
    frontier: ExperienceOccurrenceCursor | None
    has_more: bool


@dataclass(frozen=True, slots=True)
class ExperienceAppendReport:
    """Canonical records affected by an idempotent ledger append."""

    inserted: tuple[ExperienceRecord, ...] = ()
    existing: tuple[ExperienceRecord, ...] = ()
    occurrences: tuple[ExperienceOccurrenceRef, ...] = ()

    @property
    def inserted_count(self) -> int:
        return len(self.inserted)


@dataclass(frozen=True, slots=True)
class WitnessMemory:
    witness_id: str
    content: str
    consciousness_instance_id: str
    perspective_subject_id: str
    epistemic_kind: str
    source_kind: str
    status: str
    stream_scope: str
    visibility: str
    valid_from: str
    valid_to: str
    recorded_at: str
    source_sequence_start: int
    source_sequence_end: int
    source_event_ids: tuple[str, ...]
    model_task_name: str = ""
    projection_path: str = ""
    projection_status: str = "pending"
    projection_error: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    payload_sha256: str = ""


class WitnessIdentityConflict(RuntimeError):
    """One witness identity was replayed with different persisted bytes."""


@dataclass(frozen=True, slots=True)
class WitnessSearchResult:
    witness: WitnessMemory
    rank_score: float
    retrieval_source: str = "witness_fts"
    epistemic_note: str = "subjective witness, not objective truth"


@dataclass(frozen=True, slots=True)
class EvidenceAwareMemoryResult:
    """Retrieval candidate with rank and epistemic confidence kept separate."""

    record_id: str
    kind: str
    content: str
    rank_score: float
    confidence: float | None
    source: str
    valid_from: str = ""
    valid_to: str = ""
    recorded_at: str = ""
    stream_scope: str = ""
    visibility: str = "private"
    status: str = "active"
    provenance: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)


def _now_iso() -> str:
    return datetime.now(UTC).astimezone().isoformat()


def _json_dict(value: str | None) -> dict[str, Any]:
    try:
        loaded = json.loads(value or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def create_life_memory_schema(db: sqlite3.Connection) -> None:
    """Create additive life-memory tables without replacing document indexes."""

    db.row_factory = sqlite3.Row
    with transaction(db):
        existing_table = db.execute(
            """SELECT 1 FROM sqlite_master
            WHERE type = 'table' AND name = 'memory_experiences'"""
        ).fetchone()
        if existing_table is not None:
            columns = {
                str(row["name"])
                for row in db.execute("PRAGMA table_info(memory_experiences)")
            }
            if "source_event_id" not in columns:
                db.execute(
                    """ALTER TABLE memory_experiences
                    ADD COLUMN source_event_id TEXT NOT NULL DEFAULT ''"""
                )
        existing_witness_table = db.execute(
            """SELECT 1 FROM sqlite_master
            WHERE type = 'table' AND name = 'memory_witnesses'"""
        ).fetchone()
        if existing_witness_table is not None:
            witness_columns = {
                str(row["name"])
                for row in db.execute("PRAGMA table_info(memory_witnesses)")
            }
            if "payload_sha256" not in witness_columns:
                db.execute(
                    "ALTER TABLE memory_witnesses "
                    "ADD COLUMN payload_sha256 TEXT NOT NULL DEFAULT ''"
                )
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS memory_experiences (
                event_id TEXT PRIMARY KEY,
                source_event_id TEXT NOT NULL DEFAULT '',
                sequence INTEGER NOT NULL,
                occurred_at TEXT NOT NULL,
                recorded_at TEXT NOT NULL,
                source TEXT NOT NULL,
                channel TEXT NOT NULL,
                event_type TEXT NOT NULL,
                content TEXT NOT NULL,
                stream_id TEXT NOT NULL DEFAULT '',
                consciousness_instance_id TEXT NOT NULL DEFAULT '',
                actor TEXT NOT NULL DEFAULT '',
                visibility TEXT NOT NULL DEFAULT 'private',
                valid_from TEXT NOT NULL DEFAULT '',
                valid_to TEXT NOT NULL DEFAULT '',
                metadata_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS idx_experiences_sequence
                ON memory_experiences(sequence, event_id);
            CREATE INDEX IF NOT EXISTS idx_experiences_source_event
                ON memory_experiences(source_event_id, occurred_at, event_id);
            CREATE INDEX IF NOT EXISTS idx_experiences_stream
                ON memory_experiences(stream_id, sequence);
            CREATE INDEX IF NOT EXISTS idx_experiences_time
                ON memory_experiences(occurred_at);
            CREATE TRIGGER IF NOT EXISTS memory_experiences_immutable_update
            BEFORE UPDATE ON memory_experiences BEGIN
                SELECT RAISE(ABORT, 'ExperienceLedgerImmutable');
            END;
            CREATE TRIGGER IF NOT EXISTS memory_experiences_immutable_delete
            BEFORE DELETE ON memory_experiences BEGIN
                SELECT RAISE(ABORT, 'ExperienceLedgerImmutable');
            END;

            CREATE TABLE IF NOT EXISTS memory_experience_occurrence_aliases (
                occurrence_id TEXT PRIMARY KEY,
                event_id TEXT NOT NULL,
                source_event_id TEXT NOT NULL DEFAULT '',
                ingest_position INTEGER NOT NULL DEFAULT 0,
                recorded_at TEXT NOT NULL,
                FOREIGN KEY (event_id) REFERENCES memory_experiences(event_id)
                    ON DELETE RESTRICT
            );
            CREATE INDEX IF NOT EXISTS idx_experience_alias_event
                ON memory_experience_occurrence_aliases(event_id, ingest_position);
            CREATE TRIGGER IF NOT EXISTS experience_aliases_immutable_update
            BEFORE UPDATE ON memory_experience_occurrence_aliases BEGIN
                SELECT RAISE(ABORT, 'ExperienceAliasImmutable');
            END;
            CREATE TRIGGER IF NOT EXISTS experience_aliases_immutable_delete
            BEFORE DELETE ON memory_experience_occurrence_aliases BEGIN
                SELECT RAISE(ABORT, 'ExperienceAliasImmutable');
            END;

            CREATE TABLE IF NOT EXISTS memory_witnesses (
                witness_id TEXT PRIMARY KEY,
                content TEXT NOT NULL,
                consciousness_instance_id TEXT NOT NULL,
                perspective_subject_id TEXT NOT NULL,
                epistemic_kind TEXT NOT NULL,
                source_kind TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                stream_scope TEXT NOT NULL DEFAULT '',
                visibility TEXT NOT NULL DEFAULT 'private',
                valid_from TEXT NOT NULL DEFAULT '',
                valid_to TEXT NOT NULL DEFAULT '',
                recorded_at TEXT NOT NULL,
                source_sequence_start INTEGER NOT NULL DEFAULT 0,
                source_sequence_end INTEGER NOT NULL DEFAULT 0,
                model_task_name TEXT NOT NULL DEFAULT '',
                projection_path TEXT NOT NULL DEFAULT '',
                projection_status TEXT NOT NULL DEFAULT 'pending',
                projection_error TEXT NOT NULL DEFAULT '',
                metadata_json TEXT NOT NULL DEFAULT '{}',
                payload_sha256 TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_witnesses_scope_time
                ON memory_witnesses(stream_scope, recorded_at DESC);
            CREATE INDEX IF NOT EXISTS idx_witnesses_status
                ON memory_witnesses(status, epistemic_kind);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_witnesses_projection_path
                ON memory_witnesses(projection_path) WHERE projection_path <> '';
            CREATE TRIGGER IF NOT EXISTS memory_witnesses_authority_immutable_update
            BEFORE UPDATE ON memory_witnesses
            WHEN OLD.witness_id IS NOT NEW.witness_id
              OR OLD.content IS NOT NEW.content
              OR OLD.consciousness_instance_id IS NOT NEW.consciousness_instance_id
              OR OLD.perspective_subject_id IS NOT NEW.perspective_subject_id
              OR OLD.epistemic_kind IS NOT NEW.epistemic_kind
              OR OLD.source_kind IS NOT NEW.source_kind
              OR OLD.status IS NOT NEW.status
              OR OLD.stream_scope IS NOT NEW.stream_scope
              OR OLD.visibility IS NOT NEW.visibility
              OR OLD.valid_from IS NOT NEW.valid_from
              OR OLD.valid_to IS NOT NEW.valid_to
              OR OLD.recorded_at IS NOT NEW.recorded_at
              OR OLD.source_sequence_start IS NOT NEW.source_sequence_start
              OR OLD.source_sequence_end IS NOT NEW.source_sequence_end
              OR OLD.model_task_name IS NOT NEW.model_task_name
              OR OLD.metadata_json IS NOT NEW.metadata_json
              OR OLD.payload_sha256 IS NOT NEW.payload_sha256
            BEGIN
                SELECT RAISE(ABORT, 'MemoryWitnessAuthorityImmutable');
            END;
            CREATE TRIGGER IF NOT EXISTS memory_witnesses_authority_immutable_delete
            BEFORE DELETE ON memory_witnesses BEGIN
                SELECT RAISE(ABORT, 'MemoryWitnessAuthorityImmutable');
            END;

            CREATE TABLE IF NOT EXISTS memory_witness_sources (
                witness_id TEXT NOT NULL,
                event_id TEXT NOT NULL,
                ordinal INTEGER NOT NULL,
                PRIMARY KEY (witness_id, event_id),
                FOREIGN KEY (witness_id) REFERENCES memory_witnesses(witness_id)
                    ON DELETE CASCADE,
                FOREIGN KEY (event_id) REFERENCES memory_experiences(event_id)
                    ON DELETE RESTRICT
            );
            CREATE INDEX IF NOT EXISTS idx_witness_sources_event
                ON memory_witness_sources(event_id, witness_id);
            CREATE TRIGGER IF NOT EXISTS memory_witness_sources_immutable_update
            BEFORE UPDATE ON memory_witness_sources BEGIN
                SELECT RAISE(ABORT, 'MemoryWitnessSourceImmutable');
            END;
            CREATE TRIGGER IF NOT EXISTS memory_witness_sources_immutable_delete
            BEFORE DELETE ON memory_witness_sources BEGIN
                SELECT RAISE(ABORT, 'MemoryWitnessSourceImmutable');
            END;

            CREATE TABLE IF NOT EXISTS memory_witness_state (
                consciousness_instance_id TEXT PRIMARY KEY,
                last_sequence INTEGER NOT NULL DEFAULT 0,
                revision INTEGER NOT NULL DEFAULT 0,
                last_run_at TEXT NOT NULL DEFAULT '',
                last_success_at TEXT NOT NULL DEFAULT '',
                last_error TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS memory_witness_reconciliation_state (
                scan_name TEXT PRIMARY KEY,
                cursor_order_value TEXT NOT NULL DEFAULT '',
                cursor_identity TEXT NOT NULL DEFAULT '',
                frontier_order_value TEXT NOT NULL DEFAULT '',
                frontier_identity TEXT NOT NULL DEFAULT '',
                revision INTEGER NOT NULL DEFAULT 0,
                cycle_started_at TEXT NOT NULL DEFAULT '',
                last_completed_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL,
                state_sha256 TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS memory_witness_migrations (
                migration_key TEXT PRIMARY KEY,
                source_path TEXT NOT NULL,
                source_hash TEXT NOT NULL,
                witness_id TEXT NOT NULL,
                migrated_at TEXT NOT NULL,
                FOREIGN KEY (witness_id) REFERENCES memory_witnesses(witness_id)
                    ON DELETE RESTRICT
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS memory_witness_fts USING fts5(
                witness_id UNINDEXED, content, tokenize='unicode61'
            );
            """
        )
        state_columns = {
            str(row[1])
            for row in db.execute("PRAGMA table_info(memory_witness_state)")
        }
        if "revision" not in state_columns:
            db.execute(
                "ALTER TABLE memory_witness_state "
                "ADD COLUMN revision INTEGER NOT NULL DEFAULT 0"
            )
        reconciliation_columns = {
            str(row[1])
            for row in db.execute(
                "PRAGMA table_info(memory_witness_reconciliation_state)"
            )
        }
        if "state_sha256" not in reconciliation_columns:
            db.execute(
                "ALTER TABLE memory_witness_reconciliation_state "
                "ADD COLUMN state_sha256 TEXT NOT NULL DEFAULT ''"
            )

    # Kept additive so historical local ledgers gain the staged pipeline
    # without replacing any existing authority table.
    from .witness_pipeline import create_witness_pipeline_schema

    create_witness_pipeline_schema(db)


def experience_evidence_body(record: ExperienceRecord) -> dict[str, Any]:
    """Canonical source evidence shared by local and selected backends."""

    return {
        "occurred_at": record.occurred_at,
        "source": record.source,
        "channel": record.channel,
        "event_type": record.event_type,
        "content": record.content,
        "stream_id": record.stream_id,
        "consciousness_instance_id": record.consciousness_instance_id,
        "actor": record.actor,
        "visibility": record.visibility,
        "valid_from": record.valid_from or record.occurred_at,
        "valid_to": record.valid_to,
        "metadata": record.metadata,
    }


def experience_payload_sha256(record: ExperienceRecord) -> str:
    return hashlib.sha256(
        canonical_json(experience_evidence_body(record)).encode("utf-8")
    ).hexdigest()


def witness_append_payload_sha256(
    *,
    witness_id: str,
    content: str,
    consciousness_instance_id: str,
    perspective_subject_id: str,
    epistemic_kind: str,
    source_kind: str,
    status: str,
    stream_scope: str,
    visibility: str,
    valid_from: str,
    valid_to: str,
    recorded_at: str,
    source_sequence_start: int,
    source_sequence_end: int,
    model_task_name: str,
    projection_path: str,
    metadata: Mapping[str, Any],
    source_event_ids: Sequence[str],
) -> str:
    """Hash the exact bytes and initial fields accepted by witness append."""

    body = {
        "witness_id": str(witness_id),
        "content": str(content),
        "consciousness_instance_id": str(consciousness_instance_id),
        "perspective_subject_id": str(perspective_subject_id),
        "epistemic_kind": str(epistemic_kind),
        "source_kind": str(source_kind),
        "status": str(status),
        "stream_scope": str(stream_scope),
        "visibility": str(visibility),
        "valid_from": str(valid_from),
        "valid_to": str(valid_to),
        "recorded_at": str(recorded_at),
        "source_sequence_start": max(0, int(source_sequence_start)),
        "source_sequence_end": max(0, int(source_sequence_end)),
        "model_task_name": str(model_task_name),
        "projection_path": str(projection_path),
        "projection_status": "pending",
        "projection_error": "",
        "metadata_json": canonical_json(dict(metadata)),
        "source_event_ids": tuple(str(item) for item in source_event_ids),
    }
    return hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()


def make_experience_occurrence_ref(
    experience: ExperienceRecord,
    *,
    occurrence_id: str | None = None,
    source_event_id: str | None = None,
    ingest_position: int | None = None,
    recorded_at: str | None = None,
    is_alias: bool = False,
) -> ExperienceOccurrenceRef:
    return ExperienceOccurrenceRef(
        occurrence_id=str(occurrence_id or experience.event_id),
        source_event_id=str(source_event_id or experience.source_event_id),
        ingest_position=(
            int(experience.sequence)
            if ingest_position is None
            else int(ingest_position)
        ),
        canonical_event_id=experience.event_id,
        canonical_payload_sha256=experience_payload_sha256(experience),
        recorded_at=str(recorded_at or experience.recorded_at),
        experience=experience,
        is_alias=bool(is_alias),
    )


def experience_occurrence_cursor(
    occurrence: ExperienceOccurrenceRef,
) -> ExperienceOccurrenceCursor:
    """Return the stable composite key for one occurrence."""

    return ExperienceOccurrenceCursor(
        ingest_position=int(occurrence.ingest_position),
        occurrence_id=str(occurrence.occurrence_id),
    )


def _same_experience_occurrence(
    persisted: ExperienceRecord,
    incoming: ExperienceRecord,
) -> bool:
    """Compare source evidence without conflating ingest and producer order."""

    return (
        persisted.occurred_at == incoming.occurred_at
        and persisted.source == incoming.source
        and persisted.channel == incoming.channel
        and persisted.event_type == incoming.event_type
        and persisted.content == incoming.content
        and persisted.stream_id == incoming.stream_id
        and persisted.consciousness_instance_id
        == incoming.consciousness_instance_id
        and persisted.actor == incoming.actor
        and persisted.visibility == incoming.visibility
        and persisted.valid_from == (incoming.valid_from or incoming.occurred_at)
        and persisted.valid_to == incoming.valid_to
        and persisted.metadata == incoming.metadata
    )


def append_experiences_detailed(
    db: sqlite3.Connection,
    records: Sequence[ExperienceRecord],
) -> ExperienceAppendReport:
    """Append occurrences and return canonical newly inserted evidence.

    Historical rows used ``event_id`` as both source identity and occurrence
    identity.  During replay, an exact legacy row is linked through an
    immutable alias instead of being duplicated.  A repeated source event with
    different occurrence evidence receives its own row.
    """

    inserted: list[ExperienceRecord] = []
    existing_records: list[ExperienceRecord] = []
    occurrences: list[ExperienceOccurrenceRef] = []
    with transaction(db):
        for raw_record in records:
            source_event_id = str(
                raw_record.source_event_id or raw_record.event_id
            )
            record = replace(
                raw_record,
                source_event_id=source_event_id,
                recorded_at=raw_record.recorded_at or _now_iso(),
                valid_from=raw_record.valid_from or raw_record.occurred_at,
            )
            existing = db.execute(
                "SELECT * FROM memory_experiences WHERE event_id = ?",
                (record.event_id,),
            ).fetchone()
            if existing is not None:
                persisted = _experience_from_row(existing)
                if not _same_experience_occurrence(persisted, record):
                    raise ValueError(f"ExperienceIdentityConflict:{record.event_id}")
                existing_records.append(persisted)
                occurrences.append(make_experience_occurrence_ref(persisted))
                continue

            alias = db.execute(
                """SELECT * FROM memory_experience_occurrence_aliases
                WHERE occurrence_id = ?""",
                (record.event_id,),
            ).fetchone()
            if alias is not None:
                row = db.execute(
                    "SELECT * FROM memory_experiences WHERE event_id = ?",
                    (str(alias["event_id"]),),
                ).fetchone()
                if row is None:
                    raise RuntimeError(f"ExperienceAliasTargetMissing:{record.event_id}")
                persisted = _experience_from_row(row)
                if (
                    not _same_experience_occurrence(persisted, record)
                    or str(alias["source_event_id"]) != source_event_id
                    or int(alias["ingest_position"]) != int(record.sequence)
                ):
                    raise ValueError(f"ExperienceAliasConflict:{record.event_id}")
                existing_records.append(persisted)
                occurrences.append(
                    make_experience_occurrence_ref(
                        persisted,
                        occurrence_id=record.event_id,
                        source_event_id=str(alias["source_event_id"]),
                        ingest_position=int(alias["ingest_position"]),
                        recorded_at=str(alias["recorded_at"]),
                        is_alias=True,
                    )
                )
                continue

            legacy = None
            if record.event_id != source_event_id:
                legacy = db.execute(
                    "SELECT * FROM memory_experiences WHERE event_id = ?",
                    (source_event_id,),
                ).fetchone()
            if legacy is not None:
                persisted = _experience_from_row(legacy)
                if _same_experience_occurrence(persisted, record):
                    alias_recorded_at = _now_iso()
                    db.execute(
                        """INSERT INTO memory_experience_occurrence_aliases (
                            occurrence_id, event_id, source_event_id,
                            ingest_position, recorded_at
                        ) VALUES (?, ?, ?, ?, ?)""",
                        (
                            record.event_id,
                            persisted.event_id,
                            source_event_id,
                            int(record.sequence),
                            alias_recorded_at,
                        ),
                    )
                    existing_records.append(persisted)
                    occurrences.append(
                        make_experience_occurrence_ref(
                            persisted,
                            occurrence_id=record.event_id,
                            source_event_id=source_event_id,
                            ingest_position=int(record.sequence),
                            recorded_at=alias_recorded_at,
                            is_alias=True,
                        )
                    )
                    continue

            db.execute(
                """
                INSERT INTO memory_experiences (
                    event_id, source_event_id, sequence, occurred_at,
                    recorded_at, source, channel, event_type, content,
                    stream_id, consciousness_instance_id, actor, visibility,
                    valid_from, valid_to, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.event_id,
                    source_event_id,
                    int(record.sequence),
                    record.occurred_at,
                    record.recorded_at,
                    record.source,
                    record.channel,
                    record.event_type,
                    record.content,
                    record.stream_id,
                    record.consciousness_instance_id,
                    record.actor,
                    record.visibility,
                    record.valid_from,
                    record.valid_to,
                    json.dumps(record.metadata, ensure_ascii=False),
                ),
            )
            inserted.append(record)
            occurrences.append(make_experience_occurrence_ref(record))
    return ExperienceAppendReport(
        inserted=tuple(inserted),
        existing=tuple(existing_records),
        occurrences=tuple(occurrences),
    )


def insert_experiences(
    db: sqlite3.Connection,
    records: Sequence[ExperienceRecord],
) -> int:
    """Compatibility wrapper returning the count of new occurrences."""

    return append_experiences_detailed(db, records).inserted_count


def list_experiences_after(
    db: sqlite3.Connection,
    sequence: int,
    *,
    limit: int = 100,
    stream_scope: str | None = None,
) -> list[ExperienceRecord]:
    params: list[Any] = [int(sequence)]
    scope_clause = ""
    if stream_scope is not None:
        scope_clause = " AND stream_id = ?"
        params.append(stream_scope)
    params.append(max(1, int(limit)))
    rows = db.execute(
        f"""SELECT * FROM memory_experiences
        WHERE sequence > ?{scope_clause}
        ORDER BY sequence, event_id LIMIT ?""",
        params,
    ).fetchall()
    return [_experience_from_row(row) for row in rows]


_EXPERIENCE_OCCURRENCE_VIEW_SQL = """
SELECT
    occurrence.occurrence_id,
    occurrence.canonical_event_id,
    occurrence.occurrence_source_event_id,
    occurrence.ingest_position,
    occurrence.occurrence_recorded_at,
    occurrence.is_alias,
    experience.*
FROM (
    SELECT
        event_id AS occurrence_id,
        event_id AS canonical_event_id,
        source_event_id AS occurrence_source_event_id,
        sequence AS ingest_position,
        recorded_at AS occurrence_recorded_at,
        0 AS is_alias
    FROM memory_experiences
    UNION ALL
    SELECT
        occurrence_id,
        event_id AS canonical_event_id,
        source_event_id AS occurrence_source_event_id,
        ingest_position,
        recorded_at AS occurrence_recorded_at,
        1 AS is_alias
    FROM memory_experience_occurrence_aliases
) AS occurrence
JOIN memory_experiences AS experience
  ON experience.event_id = occurrence.canonical_event_id
"""


def _experience_occurrence_from_row(row: sqlite3.Row) -> ExperienceOccurrenceRef:
    experience = _experience_from_row(row)
    return make_experience_occurrence_ref(
        experience,
        occurrence_id=str(row["occurrence_id"]),
        source_event_id=str(row["occurrence_source_event_id"]),
        ingest_position=int(row["ingest_position"]),
        recorded_at=str(row["occurrence_recorded_at"]),
        is_alias=bool(row["is_alias"]),
    )


def get_experience_occurrence(
    db: sqlite3.Connection,
    occurrence_id: str,
) -> ExperienceOccurrenceRef | None:
    row = db.execute(
        _EXPERIENCE_OCCURRENCE_VIEW_SQL + " WHERE occurrence.occurrence_id = ?",
        (occurrence_id,),
    ).fetchone()
    return _experience_occurrence_from_row(row) if row is not None else None


def list_experience_occurrences_after(
    db: sqlite3.Connection,
    position: int,
    limit: int = 100,
) -> list[ExperienceOccurrenceRef]:
    rows = db.execute(
        _EXPERIENCE_OCCURRENCE_VIEW_SQL
        + """ WHERE occurrence.ingest_position > ?
        ORDER BY occurrence.ingest_position, occurrence.occurrence_id LIMIT ?""",
        (max(0, int(position)), max(1, min(int(limit), 1000))),
    ).fetchall()
    return [_experience_occurrence_from_row(row) for row in rows]


def list_experience_occurrence_page(
    db: sqlite3.Connection,
    *,
    position_after: int = 0,
    after: ExperienceOccurrenceCursor | None = None,
    through: ExperienceOccurrenceCursor | None = None,
    limit: int = 100,
) -> ExperienceOccurrencePage:
    """Read one stable composite-key page without guessing a numeric offset."""

    lower_position = max(0, int(position_after))
    page_limit = max(1, min(int(limit), 1000))
    if after is not None and int(after.ingest_position) <= lower_position:
        raise ValueError("ExperienceOccurrenceCursorOutsideScan")
    frontier = through
    if frontier is not None and int(frontier.ingest_position) <= lower_position:
        raise ValueError("ExperienceOccurrenceFrontierOutsideScan")
    if frontier is None:
        frontier_row = db.execute(
            _EXPERIENCE_OCCURRENCE_VIEW_SQL
            + """ WHERE occurrence.ingest_position > ?
            ORDER BY occurrence.ingest_position DESC,
                     occurrence.occurrence_id DESC LIMIT 1""",
            (lower_position,),
        ).fetchone()
        if frontier_row is None:
            return ExperienceOccurrencePage((), None, None, False)
        frontier = ExperienceOccurrenceCursor(
            ingest_position=int(frontier_row["ingest_position"]),
            occurrence_id=str(frontier_row["occurrence_id"]),
        )
    if after is not None and after > frontier:
        raise ValueError("ExperienceOccurrenceCursorBeyondFrontier")

    clauses = ["occurrence.ingest_position > ?"]
    params: list[Any] = [lower_position]
    if after is not None:
        clauses.append(
            "(occurrence.ingest_position > ? OR "
            "(occurrence.ingest_position = ? AND occurrence.occurrence_id > ?))"
        )
        params.extend(
            [after.ingest_position, after.ingest_position, after.occurrence_id]
        )
    clauses.append(
        "(occurrence.ingest_position < ? OR "
        "(occurrence.ingest_position = ? AND occurrence.occurrence_id <= ?))"
    )
    params.extend(
        [frontier.ingest_position, frontier.ingest_position, frontier.occurrence_id]
    )
    params.append(page_limit + 1)
    rows = db.execute(
        _EXPERIENCE_OCCURRENCE_VIEW_SQL
        + " WHERE "
        + " AND ".join(clauses)
        + " ORDER BY occurrence.ingest_position, occurrence.occurrence_id LIMIT ?",
        params,
    ).fetchall()
    has_more = len(rows) > page_limit
    items = tuple(
        _experience_occurrence_from_row(row) for row in rows[:page_limit]
    )
    next_cursor = experience_occurrence_cursor(items[-1]) if items else after
    return ExperienceOccurrencePage(items, next_cursor, frontier, has_more)


def experience_health_snapshot(db: sqlite3.Connection) -> dict[str, Any]:
    row = db.execute(
        """SELECT
            (SELECT COUNT(*) FROM memory_experiences) AS canonical_count,
            (SELECT COUNT(*) FROM memory_experience_occurrence_aliases) AS alias_count,
            (SELECT MAX(sequence) FROM memory_experiences) AS canonical_frontier,
            (SELECT MAX(ingest_position)
             FROM memory_experience_occurrence_aliases) AS alias_frontier,
            (SELECT MAX(recorded_at) FROM memory_experiences) AS latest_recorded_at
        """
    ).fetchone()
    canonical_count = int(row["canonical_count"] or 0)
    alias_count = int(row["alias_count"] or 0)
    frontier_row = db.execute(
        _EXPERIENCE_OCCURRENCE_VIEW_SQL
        + " ORDER BY occurrence.ingest_position DESC, "
        "occurrence.occurrence_id DESC LIMIT 1"
    ).fetchone()
    return {
        "status": "healthy",
        "canonical_count": canonical_count,
        "alias_count": alias_count,
        "occurrence_count": canonical_count + alias_count,
        "frontier": max(
            int(row["canonical_frontier"] or 0),
            int(row["alias_frontier"] or 0),
        ),
        "frontier_cursor": (
            {
                "ingest_position": int(frontier_row["ingest_position"]),
                "occurrence_id_sha256": hashlib.sha256(
                    str(frontier_row["occurrence_id"]).encode("utf-8")
                ).hexdigest(),
            }
            if frontier_row is not None
            else None
        ),
        "latest_recorded_at": str(row["latest_recorded_at"] or ""),
    }


def insert_witness_memory(
    db: sqlite3.Connection,
    *,
    content: str,
    consciousness_instance_id: str,
    perspective_subject_id: str,
    epistemic_kind: str,
    source_kind: str,
    stream_scope: str,
    visibility: str,
    valid_from: str,
    valid_to: str,
    source_event_ids: Sequence[str],
    source_sequence_start: int = 0,
    source_sequence_end: int = 0,
    model_task_name: str = "",
    projection_path: str = "",
    status: str = "active",
    metadata: dict[str, Any] | None = None,
    witness_id: str | None = None,
    recorded_at: str | None = None,
) -> WitnessMemory:
    """Append a subjective witness with an explicit immutable evidence chain."""

    source_ids = tuple(dict.fromkeys(str(item) for item in source_event_ids if item))
    if source_kind == "experience_window" and not source_ids:
        raise ValueError("WitnessSourceRequired")
    new_id = witness_id or f"wit_{uuid4().hex}"
    recorded = recorded_at or _now_iso()
    metadata_body = dict(metadata or {})
    normalized_sequence_start = max(0, int(source_sequence_start))
    normalized_sequence_end = max(0, int(source_sequence_end))
    payload_sha256 = witness_append_payload_sha256(
        witness_id=new_id,
        content=content,
        consciousness_instance_id=consciousness_instance_id,
        perspective_subject_id=perspective_subject_id,
        epistemic_kind=epistemic_kind,
        source_kind=source_kind,
        status=status,
        stream_scope=stream_scope,
        visibility=visibility,
        valid_from=valid_from,
        valid_to=valid_to,
        recorded_at=recorded,
        source_sequence_start=normalized_sequence_start,
        source_sequence_end=normalized_sequence_end,
        model_task_name=model_task_name,
        projection_path=projection_path,
        metadata=metadata_body,
        source_event_ids=source_ids,
    )
    with transaction(db):
        existing = db.execute(
            "SELECT * FROM memory_witnesses WHERE witness_id = ?",
            (new_id,),
        ).fetchone()
        if existing is not None:
            persisted = _witness_from_row(db, existing)
            if persisted.payload_sha256 != payload_sha256:
                raise WitnessIdentityConflict(
                    f"WitnessIdentityConflict:{new_id}"
                )
            return persisted
        db.execute(
            """
            INSERT INTO memory_witnesses (
                witness_id, content, consciousness_instance_id,
                perspective_subject_id, epistemic_kind, source_kind, status,
                stream_scope, visibility, valid_from, valid_to, recorded_at,
                source_sequence_start, source_sequence_end, model_task_name,
                projection_path, projection_status, metadata_json,
                payload_sha256
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                      'pending', ?, ?)
            """,
            (
                new_id,
                content,
                consciousness_instance_id,
                perspective_subject_id,
                epistemic_kind,
                source_kind,
                status,
                stream_scope,
                visibility,
                valid_from,
                valid_to,
                recorded,
                normalized_sequence_start,
                normalized_sequence_end,
                model_task_name,
                projection_path,
                canonical_json(metadata_body),
                payload_sha256,
            ),
        )
        for ordinal, event_id in enumerate(source_ids):
            db.execute(
                "INSERT INTO memory_witness_sources VALUES (?, ?, ?)",
                (new_id, event_id, ordinal),
            )
        db.execute(
            "INSERT INTO memory_witness_fts (witness_id, content) VALUES (?, ?)",
            (new_id, content),
        )
    row = db.execute(
        "SELECT * FROM memory_witnesses WHERE witness_id = ?", (new_id,)
    ).fetchone()
    if row is None:
        raise RuntimeError("WitnessInsertLost")
    return _witness_from_row(db, row)


def mark_witness_projection(
    db: sqlite3.Connection,
    witness_id: str,
    *,
    projection_path: str,
    status: str,
    error: str = "",
) -> bool:
    with transaction(db):
        cursor = db.execute(
            """UPDATE memory_witnesses SET projection_path = ?,
            projection_status = ?, projection_error = ? WHERE witness_id = ?""",
            (projection_path, status, error, witness_id),
        )
    return cursor.rowcount > 0


def list_pending_witness_projections(
    db: sqlite3.Connection,
    *,
    limit: int = 100,
) -> list[WitnessMemory]:
    rows = db.execute(
        """SELECT * FROM memory_witnesses
        WHERE projection_status IN ('pending', 'failed') AND projection_path <> ''
        ORDER BY recorded_at, witness_id LIMIT ?""",
        (max(1, int(limit)),),
    ).fetchall()
    return [_witness_from_row(db, row) for row in rows]


def get_witness_by_projection_path(
    db: sqlite3.Connection,
    projection_path: str,
) -> WitnessMemory | None:
    row = db.execute(
        "SELECT * FROM memory_witnesses WHERE projection_path = ?",
        (projection_path,),
    ).fetchone()
    return _witness_from_row(db, row) if row is not None else None


def get_witness_state(
    db: sqlite3.Connection,
    consciousness_instance_id: str,
) -> dict[str, Any]:
    row = db.execute(
        "SELECT * FROM memory_witness_state WHERE consciousness_instance_id = ?",
        (consciousness_instance_id,),
    ).fetchone()
    return dict(row) if row is not None else {
        "consciousness_instance_id": consciousness_instance_id,
        "last_sequence": 0,
        "revision": 0,
        "last_run_at": "",
        "last_success_at": "",
        "last_error": "",
        "updated_at": "",
    }


def update_witness_state(
    db: sqlite3.Connection,
    consciousness_instance_id: str,
    *,
    last_sequence: int | None = None,
    last_run_at: str | None = None,
    last_success_at: str | None = None,
    last_error: str | None = None,
    expected_sequence: int | None = None,
    expected_revision: int | None = None,
) -> dict[str, Any]:
    """Advance the witness mirror with position+revision CAS.

    Metadata-only updates retain the cursor revision.  A caller that observed
    an earlier state can provide both expectations; stale writers and cursor
    regression are explicit conflicts instead of last-write-wins mirrors.
    """

    with transaction(db):
        current = get_witness_state(db, consciousness_instance_id)
        current_sequence = int(current["last_sequence"])
        current_revision = int(current.get("revision", 0))
        expected_position = (
            current_sequence
            if expected_sequence is None
            else max(0, int(expected_sequence))
        )
        expected_revision_value = (
            current_revision
            if expected_revision is None
            else max(0, int(expected_revision))
        )
        next_sequence, next_revision = compare_and_advance_cursor(
            current_position=current_sequence,
            current_revision=current_revision,
            expected_position=expected_position,
            expected_revision=expected_revision_value,
            next_position=(
                current_sequence if last_sequence is None else int(last_sequence)
            ),
        )
        db.execute(
            """
            INSERT INTO memory_witness_state (
                consciousness_instance_id, last_sequence, revision, last_run_at,
                last_success_at, last_error, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(consciousness_instance_id) DO UPDATE SET
                last_sequence = excluded.last_sequence,
                revision = excluded.revision,
                last_run_at = excluded.last_run_at,
                last_success_at = excluded.last_success_at,
                last_error = excluded.last_error,
                updated_at = excluded.updated_at
            WHERE memory_witness_state.last_sequence = ?
              AND memory_witness_state.revision = ?
            """,
            (
                consciousness_instance_id,
                next_sequence,
                next_revision,
                current["last_run_at"] if last_run_at is None else last_run_at,
                current["last_success_at"] if last_success_at is None else last_success_at,
                current["last_error"] if last_error is None else last_error,
                _now_iso(),
                current_sequence,
                current_revision,
            ),
        )
        persisted = get_witness_state(db, consciousness_instance_id)
        if (
            int(persisted["last_sequence"]) != next_sequence
            or int(persisted.get("revision", 0)) != next_revision
        ):
            raise CursorConflict(
                "witness state changed before compare-and-swap could commit"
            )
    return persisted


def _fts_query(query: str) -> str:
    tokens = re.findall(r"[\w\u3400-\u9fff]+", query, flags=re.UNICODE)
    return " OR ".join(f'"{token}"' for token in tokens)


def search_witness_memories(
    db: sqlite3.Connection,
    query: str,
    *,
    mode: MemorySearchMode | str = "",
    top_k: int = 5,
    stream_scope: str | None = None,
    visibility: Sequence[str] = ("private",),
) -> list[WitnessSearchResult]:
    """Search witnesses while preserving perspective and visibility boundaries."""

    match = _fts_query(query)
    visible = tuple(dict.fromkeys(str(item) for item in visibility if item))
    if not match or not visible:
        return []
    mode_text = mode.value if isinstance(mode, MemorySearchMode) else str(mode or "")
    allowed_kinds = tuple(item.value for item in EpistemicKind)
    params: list[Any] = [match, *allowed_kinds, *visible]
    if stream_scope is None:
        scope_clause = " AND w.stream_scope = ''"
    else:
        scope_clause = " AND w.stream_scope IN (?, '')"
        params.append(stream_scope)
    params.append(max(1, int(top_k)))
    kind_marks = ",".join("?" for _ in allowed_kinds)
    visible_marks = ",".join("?" for _ in visible)
    rows = db.execute(
        f"""SELECT w.*, bm25(memory_witness_fts) AS lexical_rank
        FROM memory_witness_fts JOIN memory_witnesses w
          ON w.witness_id = memory_witness_fts.witness_id
        WHERE memory_witness_fts MATCH ?
          AND w.epistemic_kind IN ({kind_marks})
          AND w.visibility IN ({visible_marks}) {scope_clause}
          AND w.status NOT IN ('privacy_sealed', 'suppressed')
        ORDER BY lexical_rank, w.recorded_at DESC LIMIT ?""",
        params,
    ).fetchall()
    if not rows:
        fallback_params: list[Any] = [query, *allowed_kinds, *visible]
        if stream_scope is not None:
            fallback_params.append(stream_scope)
        fallback_params.append(max(1, int(top_k)))
        rows = db.execute(
            f"""SELECT w.*, 0.0 AS lexical_rank
            FROM memory_witnesses w
            WHERE instr(w.content, ?) > 0
              AND w.epistemic_kind IN ({kind_marks})
              AND w.visibility IN ({visible_marks}) {scope_clause}
              AND w.status NOT IN ('privacy_sealed', 'suppressed')
            ORDER BY w.recorded_at DESC LIMIT ?""",
            fallback_params,
        ).fetchall()
    results = []
    for index, row in enumerate(rows):
        note = "subjective witness, not objective truth"
        if row["epistemic_kind"] == EpistemicKind.LEGACY_WITNESS.value:
            note = "legacy subjective witness with incomplete provenance"
        if mode_text == MemorySearchMode.CURRENT_FACT.value:
            note += "; corroboration required for current facts"
        results.append(
            WitnessSearchResult(
                witness=_witness_from_row(db, row),
                rank_score=1.0 / (1.0 + index),
                epistemic_note=note,
            )
        )
    return results


def migrate_legacy_witness(
    db: sqlite3.Connection,
    *,
    migration_key: str,
    source_path: str,
    source_hash: str,
    content: str,
    valid_from: str,
    recorded_at: str,
) -> WitnessMemory | None:
    """Atomically migrate one legacy diary entry without altering its source."""

    deterministic_id = "legacy_" + hashlib.sha256(
        migration_key.encode("utf-8")
    ).hexdigest()[:32]
    with transaction(db):
        if migration_exists(db, migration_key):
            return None
        witness = insert_witness_memory(
            db,
            content=content,
            consciousness_instance_id="legacy_diary_plugin",
            perspective_subject_id="elysia",
            epistemic_kind=EpistemicKind.LEGACY_WITNESS.value,
            source_kind="legacy_diary",
            stream_scope="",
            visibility="private",
            valid_from=valid_from,
            valid_to=valid_from,
            source_event_ids=(),
            witness_id=deterministic_id,
            recorded_at=recorded_at,
            metadata={
                "legacy_source_path": source_path,
                "legacy_source_hash": source_hash,
                "provenance_quality": "incomplete",
            },
        )
        record_witness_migration(
            db,
            migration_key=migration_key,
            source_path=source_path,
            source_hash=source_hash,
            witness_id=witness.witness_id,
        )
        return witness


def migration_exists(db: sqlite3.Connection, migration_key: str) -> bool:
    return db.execute(
        "SELECT 1 FROM memory_witness_migrations WHERE migration_key = ?",
        (migration_key,),
    ).fetchone() is not None


def record_witness_migration(
    db: sqlite3.Connection,
    *,
    migration_key: str,
    source_path: str,
    source_hash: str,
    witness_id: str,
) -> None:
    with transaction(db):
        db.execute(
            """INSERT OR IGNORE INTO memory_witness_migrations
            (migration_key, source_path, source_hash, witness_id, migrated_at)
            VALUES (?, ?, ?, ?, ?)""",
            (migration_key, source_path, source_hash, witness_id, _now_iso()),
        )


def _experience_from_row(row: sqlite3.Row) -> ExperienceRecord:
    return ExperienceRecord(
        event_id=str(row["event_id"]),
        sequence=int(row["sequence"]),
        occurred_at=str(row["occurred_at"]),
        recorded_at=str(row["recorded_at"]),
        source=str(row["source"]),
        channel=str(row["channel"]),
        event_type=str(row["event_type"]),
        content=str(row["content"]),
        source_event_id=str(row["source_event_id"] or row["event_id"]),
        stream_id=str(row["stream_id"]),
        consciousness_instance_id=str(row["consciousness_instance_id"]),
        actor=str(row["actor"]),
        visibility=str(row["visibility"]),
        valid_from=str(row["valid_from"]),
        valid_to=str(row["valid_to"]),
        metadata=_json_dict(row["metadata_json"]),
    )


def _witness_from_row(db: sqlite3.Connection, row: sqlite3.Row) -> WitnessMemory:
    source_rows = db.execute(
        """SELECT event_id FROM memory_witness_sources
        WHERE witness_id = ? ORDER BY ordinal""",
        (row["witness_id"],),
    ).fetchall()
    source_event_ids = tuple(str(item["event_id"]) for item in source_rows)
    metadata = _json_dict(row["metadata_json"])
    payload_sha256 = str(row["payload_sha256"] or "")
    if not payload_sha256:
        payload_sha256 = witness_append_payload_sha256(
            witness_id=str(row["witness_id"]),
            content=str(row["content"]),
            consciousness_instance_id=str(row["consciousness_instance_id"]),
            perspective_subject_id=str(row["perspective_subject_id"]),
            epistemic_kind=str(row["epistemic_kind"]),
            source_kind=str(row["source_kind"]),
            status=str(row["status"]),
            stream_scope=str(row["stream_scope"]),
            visibility=str(row["visibility"]),
            valid_from=str(row["valid_from"]),
            valid_to=str(row["valid_to"]),
            recorded_at=str(row["recorded_at"]),
            source_sequence_start=int(row["source_sequence_start"]),
            source_sequence_end=int(row["source_sequence_end"]),
            model_task_name=str(row["model_task_name"]),
            projection_path=str(row["projection_path"] or ""),
            metadata=metadata,
            source_event_ids=source_event_ids,
        )
    return WitnessMemory(
        witness_id=str(row["witness_id"]),
        content=str(row["content"]),
        consciousness_instance_id=str(row["consciousness_instance_id"]),
        perspective_subject_id=str(row["perspective_subject_id"]),
        epistemic_kind=str(row["epistemic_kind"]),
        source_kind=str(row["source_kind"]),
        status=str(row["status"]),
        stream_scope=str(row["stream_scope"]),
        visibility=str(row["visibility"]),
        valid_from=str(row["valid_from"]),
        valid_to=str(row["valid_to"]),
        recorded_at=str(row["recorded_at"]),
        source_sequence_start=int(row["source_sequence_start"]),
        source_sequence_end=int(row["source_sequence_end"]),
        source_event_ids=source_event_ids,
        model_task_name=str(row["model_task_name"]),
        projection_path=str(row["projection_path"]),
        projection_status=str(row["projection_status"]),
        projection_error=str(row["projection_error"]),
        metadata=metadata,
        payload_sha256=payload_sha256,
    )


def witness_memory_from_row(
    db: sqlite3.Connection,
    row: sqlite3.Row,
) -> WitnessMemory:
    """Deserialize one authoritative local witness row without rewriting it."""

    return _witness_from_row(db, row)


__all__ = [
    "EpistemicKind",
    "EvidenceAwareMemoryResult",
    "ExperienceAppendReport",
    "ExperienceOccurrenceCursor",
    "ExperienceOccurrencePage",
    "ExperienceOccurrenceRef",
    "ExperienceRecord",
    "MemorySearchMode",
    "WitnessMemory",
    "WitnessIdentityConflict",
    "WitnessSearchResult",
    "append_experiences_detailed",
    "create_life_memory_schema",
    "experience_evidence_body",
    "experience_health_snapshot",
    "experience_occurrence_cursor",
    "experience_payload_sha256",
    "get_experience_occurrence",
    "get_witness_by_projection_path",
    "get_witness_state",
    "insert_experiences",
    "insert_witness_memory",
    "list_experience_occurrences_after",
    "list_experience_occurrence_page",
    "list_experiences_after",
    "list_pending_witness_projections",
    "make_experience_occurrence_ref",
    "mark_witness_projection",
    "migrate_legacy_witness",
    "migration_exists",
    "record_witness_migration",
    "search_witness_memories",
    "update_witness_state",
    "witness_append_payload_sha256",
    "witness_memory_from_row",
]
