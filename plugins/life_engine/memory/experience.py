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
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Sequence
from uuid import uuid4

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
    stream_id: str = ""
    consciousness_instance_id: str = ""
    actor: str = ""
    visibility: str = "private"
    valid_from: str = ""
    valid_to: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


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
    return datetime.now(timezone.utc).astimezone().isoformat()


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
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS memory_experiences (
                event_id TEXT PRIMARY KEY,
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
                metadata_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS idx_witnesses_scope_time
                ON memory_witnesses(stream_scope, recorded_at DESC);
            CREATE INDEX IF NOT EXISTS idx_witnesses_status
                ON memory_witnesses(status, epistemic_kind);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_witnesses_projection_path
                ON memory_witnesses(projection_path) WHERE projection_path <> '';

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

            CREATE TABLE IF NOT EXISTS memory_witness_state (
                consciousness_instance_id TEXT PRIMARY KEY,
                last_sequence INTEGER NOT NULL DEFAULT 0,
                last_run_at TEXT NOT NULL DEFAULT '',
                last_success_at TEXT NOT NULL DEFAULT '',
                last_error TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL
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


def insert_experiences(
    db: sqlite3.Connection,
    records: Sequence[ExperienceRecord],
) -> int:
    """Idempotently append raw evidence; existing events are never rewritten."""

    inserted = 0
    with transaction(db):
        for record in records:
            existing = db.execute(
                "SELECT * FROM memory_experiences WHERE event_id = ?",
                (record.event_id,),
            ).fetchone()
            if existing is not None:
                persisted = _experience_from_row(existing)
                if persisted != replace(
                    record,
                    recorded_at=persisted.recorded_at,
                    valid_from=record.valid_from or record.occurred_at,
                ):
                    raise ValueError(f"ExperienceIdentityConflict:{record.event_id}")
                continue
            cursor = db.execute(
                """
                INSERT OR IGNORE INTO memory_experiences (
                    event_id, sequence, occurred_at, recorded_at, source,
                    channel, event_type, content, stream_id,
                    consciousness_instance_id, actor, visibility,
                    valid_from, valid_to, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.event_id,
                    int(record.sequence),
                    record.occurred_at,
                    record.recorded_at or _now_iso(),
                    record.source,
                    record.channel,
                    record.event_type,
                    record.content,
                    record.stream_id,
                    record.consciousness_instance_id,
                    record.actor,
                    record.visibility,
                    record.valid_from or record.occurred_at,
                    record.valid_to,
                    json.dumps(record.metadata, ensure_ascii=False),
                ),
            )
            inserted += max(0, int(cursor.rowcount))
    return inserted


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
    with transaction(db):
        db.execute(
            """
            INSERT INTO memory_witnesses (
                witness_id, content, consciousness_instance_id,
                perspective_subject_id, epistemic_kind, source_kind, status,
                stream_scope, visibility, valid_from, valid_to, recorded_at,
                source_sequence_start, source_sequence_end, model_task_name,
                projection_path, projection_status, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
            """,
            (
                new_id,
                content.strip(),
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
                int(source_sequence_start),
                int(source_sequence_end),
                model_task_name,
                projection_path,
                json.dumps(metadata or {}, ensure_ascii=False),
            ),
        )
        for ordinal, event_id in enumerate(source_ids):
            db.execute(
                "INSERT INTO memory_witness_sources VALUES (?, ?, ?)",
                (new_id, event_id, ordinal),
            )
        db.execute(
            "INSERT INTO memory_witness_fts (witness_id, content) VALUES (?, ?)",
            (new_id, content.strip()),
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
) -> None:
    current = get_witness_state(db, consciousness_instance_id)
    with transaction(db):
        db.execute(
            """
            INSERT INTO memory_witness_state (
                consciousness_instance_id, last_sequence, last_run_at,
                last_success_at, last_error, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(consciousness_instance_id) DO UPDATE SET
                last_sequence = excluded.last_sequence,
                last_run_at = excluded.last_run_at,
                last_success_at = excluded.last_success_at,
                last_error = excluded.last_error,
                updated_at = excluded.updated_at
            """,
            (
                consciousness_instance_id,
                int(current["last_sequence"] if last_sequence is None else last_sequence),
                current["last_run_at"] if last_run_at is None else last_run_at,
                current["last_success_at"] if last_success_at is None else last_success_at,
                current["last_error"] if last_error is None else last_error,
                _now_iso(),
            ),
        )


def _fts_query(query: str) -> str:
    tokens = re.findall(r"[\w\u3400-\u9fff]+", query, flags=re.UNICODE)
    return " OR ".join(f'"{token}"' for token in tokens[:24])


def search_witness_memories(
    db: sqlite3.Connection,
    query: str,
    *,
    mode: MemorySearchMode = MemorySearchMode.AUTOBIOGRAPHICAL,
    top_k: int = 5,
    stream_scope: str | None = None,
    visibility: Sequence[str] = ("private",),
) -> list[WitnessSearchResult]:
    """Search witnesses while preserving perspective and visibility boundaries."""

    match = _fts_query(query)
    visible = tuple(dict.fromkeys(str(item) for item in visibility if item))
    if not match or not visible:
        return []
    allowed_kinds = {
        MemorySearchMode.CURRENT_FACT: (
            EpistemicKind.SUBJECTIVE_WITNESS.value,
            EpistemicKind.LEGACY_WITNESS.value,
        ),
        MemorySearchMode.AUTOBIOGRAPHICAL: (
            EpistemicKind.SUBJECTIVE_WITNESS.value,
            EpistemicKind.LEGACY_WITNESS.value,
            EpistemicKind.SELF_NARRATIVE.value,
        ),
        MemorySearchMode.HISTORICAL: tuple(item.value for item in EpistemicKind),
        MemorySearchMode.EXPLORATORY: tuple(item.value for item in EpistemicKind),
    }[mode]
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
        if mode is MemorySearchMode.CURRENT_FACT:
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
        source_event_ids=tuple(str(item["event_id"]) for item in source_rows),
        model_task_name=str(row["model_task_name"]),
        projection_path=str(row["projection_path"]),
        projection_status=str(row["projection_status"]),
        projection_error=str(row["projection_error"]),
        metadata=_json_dict(row["metadata_json"]),
    )


__all__ = [
    "EpistemicKind",
    "EvidenceAwareMemoryResult",
    "ExperienceRecord",
    "MemorySearchMode",
    "WitnessMemory",
    "WitnessSearchResult",
    "create_life_memory_schema",
    "get_witness_by_projection_path",
    "get_witness_state",
    "insert_experiences",
    "insert_witness_memory",
    "list_experiences_after",
    "list_pending_witness_projections",
    "mark_witness_projection",
    "migrate_legacy_witness",
    "migration_exists",
    "record_witness_migration",
    "search_witness_memories",
    "update_witness_state",
]
