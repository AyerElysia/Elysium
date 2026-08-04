"""Rebuildable, provenance-aware projection of the shared subjective world.

The immutable :class:`LifeEvent` ledger remains the authority for experience.
This module only maintains a queryable read model.  Assertions are never
deduplicated by meaning and conflicting observations therefore remain visible
side by side until a later event explicitly retracts one of them.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

from .event_bus import LifeEvent, RawEventStore

WORLD_PROJECTION_DB_FILE = "world_projection.sqlite3"
WORLD_PROJECTION_SCHEMA_VERSION = 2
WORLD_PROJECTOR_POLICY = "source-preserving-v1"
WORLD_PROJECTOR_SCHEMA_VERSION = 1
WORLD_REBUILD_IDLE = "idle"
WORLD_REBUILDING = "rebuilding"
WORLD_REBUILD_FAILED = "failed"
WORLD_OBSERVATION_EVENT = "world.observation_reported"
WORLD_LEGACY_IMPORT_EVENT = "world.legacy_snapshot_imported"


class WorldProjectionConflict(RuntimeError):
    """Raised when one projection identity is reused for different evidence."""


class PerceptionCursorConflict(RuntimeError):
    """Raised when a stale perception delivery attempts to advance a cursor."""


class WorldProjectionUnavailable(RuntimeError):
    """Raised when a projection cannot safely serve transient perception."""


@dataclass(frozen=True, slots=True)
class WorldAssertion:
    """One source-attributed subjective assertion retained by the projection."""

    assertion_id: str
    subject: str
    predicate: str
    value: Any
    domain: str
    status: str
    source_instance_id: str
    source_event_id: str
    occurrence_id: str
    observed_at: str
    valid_from: str
    valid_to: str
    recorded_at: str
    supersedes_assertion_id: str
    retracted_at: str
    retracted_by_assertion_id: str
    payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        """Serialize the assertion without losing its original payload."""

        return {
            "assertion_id": self.assertion_id,
            "subject": self.subject,
            "predicate": self.predicate,
            "value": self.value,
            "domain": self.domain,
            "status": self.status,
            "source_instance_id": self.source_instance_id,
            "source_event_id": self.source_event_id,
            "occurrence_id": self.occurrence_id,
            "observed_at": self.observed_at,
            "valid_from": self.valid_from,
            "valid_to": self.valid_to,
            "recorded_at": self.recorded_at,
            "supersedes_assertion_id": self.supersedes_assertion_id,
            "retracted_at": self.retracted_at,
            "retracted_by_assertion_id": self.retracted_by_assertion_id,
            "payload": dict(self.payload),
        }


@dataclass(frozen=True, slots=True)
class WorldProjectionChange:
    """One projection-relevant ledger change delivered through a cursor."""

    ingest_position: int
    event_id: str
    occurrence_id: str
    event_type: str
    change_kind: str
    source_instance_id: str
    stream_id: str
    occurred_at: str
    recorded_at: str
    payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        """Serialize one delta for diagnostics or prompt rendering."""

        return {
            "ingest_position": self.ingest_position,
            "event_id": self.event_id,
            "occurrence_id": self.occurrence_id,
            "event_type": self.event_type,
            "change_kind": self.change_kind,
            "source_instance_id": self.source_instance_id,
            "stream_id": self.stream_id,
            "occurred_at": self.occurred_at,
            "recorded_at": self.recorded_at,
            "payload": dict(self.payload),
        }


class WorldProjectionStore:
    """SQLite read model rebuilt by a single serialized ledger projector."""

    _locks_guard: ClassVar[threading.Lock] = threading.Lock()
    _locks: ClassVar[dict[Path, threading.RLock]] = {}

    def __init__(self, path: str | Path) -> None:
        """Open a projection database and create its idempotent schema."""

        self.path = Path(path)
        resolved = self.path.resolve()
        with self._locks_guard:
            self._lock = self._locks.setdefault(resolved, threading.RLock())
        self._ensure_schema()

    @staticmethod
    def _now_iso() -> str:
        """Return a timezone-aware technical update timestamp."""

        return datetime.now(UTC).astimezone().isoformat()

    def _connect(self) -> sqlite3.Connection:
        """Open one short-lived configured SQLite connection."""

        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=30.0, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode = WAL")
        db.execute("PRAGMA synchronous = FULL")
        db.execute("PRAGMA busy_timeout = 30000")
        return db

    def _ensure_schema(self) -> None:
        """Create projection, change, cursor, and metadata tables."""

        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS world_projection_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS world_assertions (
                    assertion_id TEXT PRIMARY KEY,
                    subject TEXT NOT NULL,
                    predicate TEXT NOT NULL,
                    value_json TEXT NOT NULL,
                    domain TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT '',
                    source_instance_id TEXT NOT NULL DEFAULT '',
                    source_event_id TEXT NOT NULL,
                    occurrence_id TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    valid_from TEXT NOT NULL DEFAULT '',
                    valid_to TEXT NOT NULL DEFAULT '',
                    recorded_at TEXT NOT NULL DEFAULT '',
                    supersedes_assertion_id TEXT NOT NULL DEFAULT '',
                    retracted_at TEXT NOT NULL DEFAULT '',
                    retracted_by_assertion_id TEXT NOT NULL DEFAULT '',
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_world_assertions_subject
                    ON world_assertions(subject, predicate, observed_at);
                CREATE INDEX IF NOT EXISTS idx_world_assertions_source
                    ON world_assertions(source_instance_id, source_event_id);
                CREATE TABLE IF NOT EXISTS world_projection_changes (
                    ingest_position INTEGER PRIMARY KEY,
                    event_id TEXT NOT NULL,
                    occurrence_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    change_kind TEXT NOT NULL,
                    source_instance_id TEXT NOT NULL DEFAULT '',
                    stream_id TEXT NOT NULL DEFAULT '',
                    occurred_at TEXT NOT NULL,
                    recorded_at TEXT NOT NULL DEFAULT '',
                    payload_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS world_perception_cursors (
                    instance_id TEXT PRIMARY KEY,
                    ingest_position INTEGER NOT NULL,
                    revision INTEGER NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
            schema_version = self._get_meta(db, "schema_version")
            if schema_version is None:
                self._set_meta(
                    db,
                    "schema_version",
                    str(WORLD_PROJECTION_SCHEMA_VERSION),
                )
            elif int(schema_version) > WORLD_PROJECTION_SCHEMA_VERSION:
                raise WorldProjectionConflict(
                    "world projection schema is newer than this runtime: "
                    f"{schema_version}"
                )
            elif int(schema_version) < WORLD_PROJECTION_SCHEMA_VERSION:
                self._set_meta(
                    db,
                    "schema_version",
                    str(WORLD_PROJECTION_SCHEMA_VERSION),
                )
            if self._get_meta(db, "as_of_ingest_position") is None:
                self._set_meta(db, "as_of_ingest_position", "0")
            self._ensure_meta_contract(
                db,
                "projector_policy",
                WORLD_PROJECTOR_POLICY,
            )
            self._ensure_meta_contract(
                db,
                "projector_schema_version",
                str(WORLD_PROJECTOR_SCHEMA_VERSION),
            )
            if self._get_meta(db, "rebuild_state") is None:
                self._set_meta(db, "rebuild_state", WORLD_REBUILD_IDLE)

    @staticmethod
    def _get_meta(db: sqlite3.Connection, key: str) -> str | None:
        """Read one metadata value inside an existing connection."""

        row = db.execute(
            "SELECT value FROM world_projection_meta WHERE key = ?",
            (key,),
        ).fetchone()
        return str(row["value"]) if row is not None else None

    def _set_meta(self, db: sqlite3.Connection, key: str, value: str) -> None:
        """Upsert one metadata value inside an existing transaction."""

        db.execute(
            """INSERT INTO world_projection_meta (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at""",
            (key, value, self._now_iso()),
        )

    def _ensure_meta_contract(
        self,
        db: sqlite3.Connection,
        key: str,
        expected: str,
    ) -> None:
        """Initialize one projector contract or reject incompatible state."""

        actual = self._get_meta(db, key)
        if actual is None:
            self._set_meta(db, key, expected)
            return
        if actual != expected:
            raise WorldProjectionConflict(
                f"world projector {key} mismatch: expected {expected!r}, "
                f"actual {actual!r}"
            )

    @staticmethod
    def _decode_object(content: str) -> dict[str, Any]:
        """Decode an event object while preserving non-object content."""

        try:
            value = json.loads(content)
        except (TypeError, json.JSONDecodeError):
            return {"content": str(content or "")}
        if isinstance(value, dict):
            return value
        return {"content": value}

    @staticmethod
    def _assertion_id(
        event: LifeEvent,
        assertion: dict[str, Any],
        index: int,
    ) -> str:
        """Build a stable assertion identity from immutable occurrence evidence."""

        supplied = str(assertion.get("assertion_id") or "").strip()
        if supplied:
            return supplied
        occurrence = event.occurrence_id or event.event_id
        material = f"{occurrence}:{index}".encode()
        return "assertion_" + hashlib.sha256(material).hexdigest()

    @staticmethod
    def _assertion_payloads(payload: dict[str, Any]) -> list[dict[str, Any]]:
        """Extract explicitly reported assertion objects without classifying them."""

        raw_many = payload.get("assertions")
        if isinstance(raw_many, list):
            return [dict(item) for item in raw_many if isinstance(item, dict)]
        raw_one = payload.get("assertion")
        if isinstance(raw_one, dict):
            return [dict(raw_one)]
        if "subject" in payload or "predicate" in payload:
            return [dict(payload)]
        return []

    def _insert_assertion(
        self,
        db: sqlite3.Connection,
        event: LifeEvent,
        assertion: dict[str, Any],
        index: int,
    ) -> None:
        """Insert one assertion or verify an idempotent replay."""

        assertion_id = self._assertion_id(event, assertion, index)
        subject = str(assertion.get("subject") or "").strip()
        predicate = str(assertion.get("predicate") or "").strip()
        if not subject or not predicate:
            raise ValueError("world assertions require non-empty subject and predicate")
        observed_at = str(assertion.get("observed_at") or event.timestamp)
        value = assertion.get("value")
        value_json = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        payload_json = json.dumps(
            assertion,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        values = (
            assertion_id,
            subject,
            predicate,
            value_json,
            str(assertion.get("domain") or ""),
            str(assertion.get("status") or ""),
            str(event.source_instance_id or assertion.get("source_instance_id") or ""),
            event.event_id,
            event.occurrence_id or event.event_id,
            observed_at,
            str(assertion.get("valid_from") or observed_at),
            str(assertion.get("valid_to") or ""),
            event.recorded_at,
            str(assertion.get("supersedes_assertion_id") or ""),
            payload_json,
        )
        existing = db.execute(
            "SELECT payload_json, occurrence_id FROM world_assertions "
            "WHERE assertion_id = ?",
            (assertion_id,),
        ).fetchone()
        if existing is not None:
            if str(existing["payload_json"]) != payload_json or str(
                existing["occurrence_id"]
            ) != (event.occurrence_id or event.event_id):
                raise WorldProjectionConflict(
                    f"assertion identity reused with different evidence: {assertion_id}"
                )
            return
        db.execute(
            """INSERT INTO world_assertions (
                assertion_id, subject, predicate, value_json, domain, status,
                source_instance_id, source_event_id, occurrence_id, observed_at,
                valid_from, valid_to, recorded_at, supersedes_assertion_id,
                payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            values,
        )
        retracts = str(assertion.get("retracts_assertion_id") or "").strip()
        if retracts:
            db.execute(
                """UPDATE world_assertions SET
                    retracted_at = ?, retracted_by_assertion_id = ?
                WHERE assertion_id = ? AND retracted_at = ''""",
                (observed_at, assertion_id, retracts),
            )

    def _insert_change(
        self,
        db: sqlite3.Connection,
        event: LifeEvent,
        *,
        change_kind: str,
        payload: dict[str, Any],
    ) -> None:
        """Insert one cursor-visible projection change idempotently."""

        payload_json = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        values = (
            event.sequence,
            event.event_id,
            event.occurrence_id or event.event_id,
            event.event_type,
            change_kind,
            event.source_instance_id,
            event.stream_id,
            event.timestamp,
            event.recorded_at,
            payload_json,
        )
        existing = db.execute(
            """SELECT event_id, occurrence_id, event_type, change_kind,
                source_instance_id, stream_id, occurred_at, recorded_at,
                payload_json FROM world_projection_changes
                WHERE ingest_position = ?""",
            (event.sequence,),
        ).fetchone()
        if existing is not None:
            actual = tuple(str(value) for value in existing)
            expected = tuple(str(value) for value in values[1:])
            if actual != expected:
                raise WorldProjectionConflict(
                    "world ingest position reused with different evidence: "
                    f"{event.sequence}"
                )
            return
        db.execute(
            """INSERT INTO world_projection_changes (
                ingest_position, event_id, occurrence_id, event_type,
                change_kind, source_instance_id, stream_id, occurred_at,
                recorded_at, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            values,
        )

    def _apply_event(self, db: sqlite3.Connection, event: LifeEvent) -> None:
        """Apply one ledger event without inventing semantic conclusions."""

        payload = self._decode_object(event.content)
        if event.event_type in {WORLD_OBSERVATION_EVENT, WORLD_LEGACY_IMPORT_EVENT}:
            for index, assertion in enumerate(self._assertion_payloads(payload)):
                self._insert_assertion(db, event, assertion, index)
            self._insert_change(
                db,
                event,
                change_kind="world_observation",
                payload=payload,
            )
            return
        if event.event_type.startswith("consciousness.instance_") or (
            event.event_type == "consciousness.chat_global_recovered"
        ):
            self._insert_change(
                db,
                event,
                change_kind="consciousness_presence",
                payload=payload,
            )

    def apply_events(self, events: list[LifeEvent]) -> int:
        """Atomically advance the projection across an ordered ledger batch."""

        if not events:
            return self.as_of_position()
        ordered = sorted(events, key=lambda item: item.sequence)
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                current = int(self._get_meta(db, "as_of_ingest_position") or 0)
                for event in ordered:
                    if event.sequence <= current:
                        self._apply_event(db, event)
                        continue
                    self._apply_event(db, event)
                    current = event.sequence
                self._set_meta(db, "as_of_ingest_position", str(current))
                db.commit()
            except BaseException:
                db.rollback()
                raise
        return current

    def catch_up(self, ledger: RawEventStore, *, batch_size: int = 500) -> int:
        """Consume the authoritative ledger until the projection is current."""

        if batch_size <= 0:
            raise ValueError("world projection batch_size must be positive")
        with self._lock:
            while True:
                position = self.as_of_position()
                batch = ledger.read_since_sync(position, limit=batch_size)
                if not batch:
                    return position
                self.apply_events(batch)

    def rebuild(self, ledger: RawEventStore, *, batch_size: int = 500) -> int:
        """Replay derived data while preserving independent delivery cursors."""

        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                self._set_meta(db, "rebuild_state", WORLD_REBUILDING)
                db.execute("DELETE FROM world_assertions")
                db.execute("DELETE FROM world_projection_changes")
                self._set_meta(db, "as_of_ingest_position", "0")
                db.commit()
            except BaseException:
                db.rollback()
                raise
        try:
            frontier = self.catch_up(ledger, batch_size=batch_size)
        except BaseException as primary:
            try:
                with self._lock, self._connect() as db:
                    self._set_meta(db, "rebuild_state", WORLD_REBUILD_FAILED)
            except Exception as state_error:  # noqa: BLE001 - preserve replay failure
                primary.add_note(
                    "world rebuild state could not be marked failed: "
                    f"{type(state_error).__name__}"
                )
            raise
        with self._lock, self._connect() as db:
            self._set_meta(db, "rebuild_state", WORLD_REBUILD_IDLE)
        return frontier

    def projector_contract(self) -> dict[str, Any]:
        """Return the persisted policy, schema, and rebuild state."""

        with self._connect() as db:
            return {
                "policy": self._get_meta(db, "projector_policy") or "",
                "schema_version": int(
                    self._get_meta(db, "projector_schema_version") or 0
                ),
                "rebuild_state": self._get_meta(db, "rebuild_state") or "",
            }

    def ensure_deliverable(self) -> None:
        """Fail closed while a rebuild is incomplete or has failed."""

        contract = self.projector_contract()
        if contract["rebuild_state"] != WORLD_REBUILD_IDLE:
            raise WorldProjectionUnavailable(
                "world projection is not deliverable: "
                f"rebuild_state={contract['rebuild_state']}"
            )

    def as_of_position(self) -> int:
        """Return the durable ledger frontier represented by this projection."""

        with self._connect() as db:
            return int(self._get_meta(db, "as_of_ingest_position") or 0)

    @staticmethod
    def _assertion_from_row(row: sqlite3.Row) -> WorldAssertion:
        """Decode one assertion row."""

        return WorldAssertion(
            assertion_id=str(row["assertion_id"]),
            subject=str(row["subject"]),
            predicate=str(row["predicate"]),
            value=json.loads(str(row["value_json"])),
            domain=str(row["domain"]),
            status=str(row["status"]),
            source_instance_id=str(row["source_instance_id"]),
            source_event_id=str(row["source_event_id"]),
            occurrence_id=str(row["occurrence_id"]),
            observed_at=str(row["observed_at"]),
            valid_from=str(row["valid_from"]),
            valid_to=str(row["valid_to"]),
            recorded_at=str(row["recorded_at"]),
            supersedes_assertion_id=str(row["supersedes_assertion_id"]),
            retracted_at=str(row["retracted_at"]),
            retracted_by_assertion_id=str(row["retracted_by_assertion_id"]),
            payload=json.loads(str(row["payload_json"])),
        )

    def list_assertions(
        self,
        *,
        include_retracted: bool = True,
    ) -> list[WorldAssertion]:
        """Return assertions in evidence order without resolving conflicts."""

        sql = "SELECT * FROM world_assertions"
        if not include_retracted:
            sql += " WHERE retracted_at = ''"
        sql += " ORDER BY observed_at, assertion_id"
        with self._connect() as db:
            rows = db.execute(sql).fetchall()
        return [self._assertion_from_row(row) for row in rows]

    @staticmethod
    def _change_from_row(row: sqlite3.Row) -> WorldProjectionChange:
        """Decode one projection change row."""

        return WorldProjectionChange(
            ingest_position=int(row["ingest_position"]),
            event_id=str(row["event_id"]),
            occurrence_id=str(row["occurrence_id"]),
            event_type=str(row["event_type"]),
            change_kind=str(row["change_kind"]),
            source_instance_id=str(row["source_instance_id"]),
            stream_id=str(row["stream_id"]),
            occurred_at=str(row["occurred_at"]),
            recorded_at=str(row["recorded_at"]),
            payload=json.loads(str(row["payload_json"])),
        )

    def changes_since(
        self,
        ingest_position: int,
        *,
        through_position: int | None = None,
    ) -> list[WorldProjectionChange]:
        """Return relevant changes within one stable cursor window."""

        sql = "SELECT * FROM world_projection_changes WHERE ingest_position > ?"
        params: list[Any] = [max(0, int(ingest_position))]
        if through_position is not None:
            sql += " AND ingest_position <= ?"
            params.append(max(0, int(through_position)))
        sql += " ORDER BY ingest_position"
        with self._connect() as db:
            rows = db.execute(sql, params).fetchall()
        return [self._change_from_row(row) for row in rows]

    def perception_cursor(self, instance_id: str) -> tuple[int, int]:
        """Return one instance's projection position and cursor revision."""

        with self._connect() as db:
            row = db.execute(
                """SELECT ingest_position, revision
                FROM world_perception_cursors WHERE instance_id = ?""",
                (instance_id,),
            ).fetchone()
        if row is None:
            return 0, 0
        return int(row["ingest_position"]), int(row["revision"])

    def commit_perception_cursor(
        self,
        instance_id: str,
        *,
        expected_position: int,
        expected_revision: int,
        through_position: int,
    ) -> tuple[int, int]:
        """CAS-advance one instance cursor after successful context delivery."""

        identity = str(instance_id or "").strip()
        if not identity:
            raise ValueError("perception cursor instance_id must not be empty")
        expected = int(expected_position)
        expected_cursor_revision = int(expected_revision)
        through = int(through_position)
        if expected < 0 or expected_cursor_revision < 0 or through < 0:
            raise ValueError("perception cursor values must not be negative")
        if through < expected:
            raise ValueError("perception cursor cannot move backwards")
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                frontier = int(self._get_meta(db, "as_of_ingest_position") or 0)
                if through > frontier:
                    raise ValueError(
                        "perception cursor cannot advance beyond projection frontier"
                    )
                row = db.execute(
                    """SELECT ingest_position, revision
                    FROM world_perception_cursors WHERE instance_id = ?""",
                    (identity,),
                ).fetchone()
                current = int(row["ingest_position"]) if row is not None else 0
                revision = int(row["revision"]) if row is not None else 0
                if current != expected or revision != expected_cursor_revision:
                    raise PerceptionCursorConflict(
                        f"stale perception cursor for '{identity}': "
                        f"expected ({expected}, {expected_cursor_revision}), "
                        f"actual ({current}, {revision})"
                    )
                if through == current:
                    db.commit()
                    return current, revision
                revision += 1
                if row is None:
                    db.execute(
                        """INSERT INTO world_perception_cursors (
                            instance_id, ingest_position, revision, updated_at
                        ) VALUES (?, ?, ?, ?)""",
                        (identity, through, revision, self._now_iso()),
                    )
                else:
                    updated = db.execute(
                        """UPDATE world_perception_cursors
                        SET ingest_position = ?, revision = ?, updated_at = ?
                        WHERE instance_id = ? AND ingest_position = ? AND revision = ?""",
                        (
                            through,
                            revision,
                            self._now_iso(),
                            identity,
                            expected,
                            expected_cursor_revision,
                        ),
                    )
                    if updated.rowcount != 1:
                        raise PerceptionCursorConflict(
                            f"concurrent perception cursor update for '{identity}'"
                        )
                db.commit()
            except BaseException:
                db.rollback()
                raise
        return through, revision

    def legacy_imported(self) -> bool:
        """Return whether the compatibility WorldState snapshot was migrated."""

        with self._connect() as db:
            return self._get_meta(db, "legacy_world_import_hash") is not None

    def mark_legacy_imported(self, snapshot_hash: str) -> None:
        """Record a completed compatibility import without deleting its source."""

        with self._connect() as db:
            self._set_meta(db, "legacy_world_import_hash", snapshot_hash)

    def canonical_snapshot(self) -> dict[str, Any]:
        """Return deterministic derived content for rebuild equivalence tests."""

        return {
            "as_of_ingest_position": self.as_of_position(),
            "assertions": [item.to_dict() for item in self.list_assertions()],
            "changes": [item.to_dict() for item in self.changes_since(0)],
        }

    def health_snapshot(self) -> dict[str, Any]:
        """Return projection frontier, row counts, and cursor lag."""

        with self._connect() as db:
            assertions = db.execute(
                "SELECT COUNT(*) AS total FROM world_assertions"
            ).fetchone()
            changes = db.execute(
                "SELECT COUNT(*) AS total FROM world_projection_changes"
            ).fetchone()
            cursor_rows = db.execute(
                """SELECT instance_id, ingest_position, revision, updated_at
                FROM world_perception_cursors ORDER BY instance_id"""
            ).fetchall()
            frontier = int(self._get_meta(db, "as_of_ingest_position") or 0)
            projector_policy = self._get_meta(db, "projector_policy") or ""
            projector_schema = int(self._get_meta(db, "projector_schema_version") or 0)
            rebuild_state = self._get_meta(db, "rebuild_state") or ""
        return {
            "database_path": str(self.path),
            "as_of_ingest_position": frontier,
            "assertion_count": int(assertions["total"] if assertions else 0),
            "change_count": int(changes["total"] if changes else 0),
            "projector_policy": projector_policy,
            "projector_schema_version": projector_schema,
            "rebuild_state": rebuild_state,
            "cursors": [
                {
                    "instance_id": str(row["instance_id"]),
                    "position": int(row["ingest_position"]),
                    "revision": int(row["revision"]),
                    "lag": max(0, frontier - int(row["ingest_position"])),
                    "updated_at": str(row["updated_at"]),
                }
                for row in cursor_rows
            ],
        }


def legacy_snapshot_assertions(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """Translate the fixed legacy JSON shape into attributed migration evidence."""

    assertions: list[dict[str, Any]] = []
    relationships = snapshot.get("relationships")
    if isinstance(relationships, dict):
        for entity_id, value in relationships.items():
            if not isinstance(value, dict):
                continue
            observed_at = str(value.get("last_interaction_at") or "")
            assertions.append(
                {
                    "subject": f"relationship:{entity_id}",
                    "predicate": "legacy_snapshot",
                    "value": value,
                    "domain": "relationship",
                    "observed_at": observed_at,
                    "valid_from": observed_at,
                    "status": "legacy_import",
                }
            )
    threads = snapshot.get("open_threads")
    if isinstance(threads, list):
        for index, value in enumerate(threads):
            if not isinstance(value, dict):
                continue
            thread_id = str(value.get("thread_id") or index)
            observed_at = str(value.get("updated_at") or value.get("created_at") or "")
            assertions.append(
                {
                    "subject": f"thread:{thread_id}",
                    "predicate": "legacy_snapshot",
                    "value": value,
                    "domain": "thread",
                    "observed_at": observed_at,
                    "valid_from": observed_at,
                    "status": "legacy_import",
                }
            )
    embodied = snapshot.get("embodied_state")
    if isinstance(embodied, dict) and any(embodied.values()):
        observed_at = str(embodied.get("updated_at") or "")
        assertions.append(
            {
                "subject": "embodied:self",
                "predicate": "legacy_snapshot",
                "value": embodied,
                "domain": "embodied",
                "observed_at": observed_at,
                "valid_from": observed_at,
                "status": "legacy_import",
            }
        )
    scenes = snapshot.get("active_scenes")
    if isinstance(scenes, dict):
        for scene_id, value in scenes.items():
            if not isinstance(value, dict):
                continue
            observed_at = str(value.get("last_active_at") or "")
            assertions.append(
                {
                    "subject": f"scene:{scene_id}",
                    "predicate": "legacy_snapshot",
                    "value": value,
                    "domain": "scene",
                    "observed_at": observed_at,
                    "valid_from": observed_at,
                    "status": "legacy_import",
                }
            )
    return assertions
