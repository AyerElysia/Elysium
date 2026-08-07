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
WORLD_REFERENCE_INLINE_MAX_BYTES = 1024
WORLD_VALUE_CHUNK_MAX_BYTES = 64 * 1024
WORLD_ASSERTION_SCOPE_HISTORY = "history"
WORLD_ASSERTION_SCOPE_CURRENT_SNAPSHOT = "current_snapshot"
WORLD_ASSERTION_ORDER_OLDEST_FIRST = "oldest_first"
WORLD_ASSERTION_ORDER_NEWEST_FIRST = "newest_first"


def normalize_world_assertion_scope(value: str) -> str:
    """Validate one explicit assertion delivery scope."""

    scope = str(value or WORLD_ASSERTION_SCOPE_HISTORY).strip()
    if scope not in {
        WORLD_ASSERTION_SCOPE_HISTORY,
        WORLD_ASSERTION_SCOPE_CURRENT_SNAPSHOT,
    }:
        raise ValueError(f"unsupported World assertion delivery scope: {scope!r}")
    return scope


def is_current_snapshot_eligible(
    *,
    predicate: str,
    status: str,
    retracted_at: str = "",
) -> bool:
    """Classify structured evidence that may describe present-tense World state."""

    if str(retracted_at or ""):
        return False
    typed_predicate = str(predicate or "")
    typed_status = str(status or "")
    if typed_predicate == "session_state":
        return False
    return not (
        typed_predicate == "legacy_snapshot" and typed_status == "legacy_import"
    )


class WorldProjectionConflict(RuntimeError):
    """Raised when one projection identity is reused for different evidence."""


class PerceptionCursorConflict(RuntimeError):
    """Raised when a stale perception delivery attempts to advance a cursor."""


class WorldProjectionUnavailable(RuntimeError):
    """Raised when a projection cannot safely serve transient perception."""


class PromptProjectionPersistenceError(ValueError):
    """Raised when one-turn prompt material is offered as durable World evidence."""


@dataclass(frozen=True, slots=True)
class PromptProjectionValue:
    """Typed one-turn projection that must never enter a durable assertion."""

    delivery_id: str
    projection_sha256: str
    content: str


def is_known_transport_echo_value(
    value: Any,
    *,
    domain: str = "",
    predicate: str = "",
) -> bool:
    """Recognize the exact legacy Minecraft intent-trace recursion shape."""

    if str(domain or "") != "minecraft" or str(predicate or "") != "embodied_trace":
        return False
    if not isinstance(value, dict) or value.get("trace_kind") != "intent.issued":
        return False
    payload = value.get("payload")
    if not isinstance(payload, dict):
        return False
    context = payload.get("context")
    return isinstance(context, dict) and "transient_world_perception" in context


def reject_prompt_projection_persistence(
    value: Any,
    *,
    domain: str = "",
    predicate: str = "",
) -> None:
    """Fail closed for typed prompt projections and the known recursive echo."""

    pending: list[Any] = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, PromptProjectionValue):
            raise PromptProjectionPersistenceError(
                "prompt_projection values are transport-only and cannot be persisted"
            )
        if isinstance(item, dict):
            pending.extend(item.values())
        elif isinstance(item, (list, tuple, set)):
            pending.extend(item)
    if is_known_transport_echo_value(
        value,
        domain=domain,
        predicate=predicate,
    ):
        raise PromptProjectionPersistenceError(
            "known Minecraft transient perception echo cannot be persisted"
        )


@dataclass(frozen=True, slots=True)
class WorldAssertionReference:
    """Bounded assertion descriptor; the exact value remains in the store."""

    assertion_id: str
    subject: str
    predicate: str
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
    value_bytes: int
    value_inlined: bool
    value: Any
    transport_echo: bool


@dataclass(frozen=True, slots=True)
class WorldAssertionReferencePage:
    """One stable evidence-ordered assertion page with aggregate coverage."""

    items: tuple[WorldAssertionReference, ...]
    total_items: int
    total_value_bytes: int
    next_after_observed_at: str
    next_after_assertion_id: str
    delivery_scope: str = WORLD_ASSERTION_SCOPE_HISTORY
    result_order: str = WORLD_ASSERTION_ORDER_OLDEST_FIRST


@dataclass(frozen=True, slots=True)
class WorldChangeReference:
    """Bounded change descriptor; large payloads remain addressable by position."""

    ingest_position: int
    event_id: str
    occurrence_id: str
    event_type: str
    change_kind: str
    source_instance_id: str
    stream_id: str
    occurred_at: str
    recorded_at: str
    payload_bytes: int
    payload_inlined: bool
    payload: dict[str, Any]
    transport_echo: bool


@dataclass(frozen=True, slots=True)
class WorldChangeReferencePage:
    """One ordered change page and the exact size of its stable source window."""

    items: tuple[WorldChangeReference, ...]
    total_items: int
    total_payload_bytes: int
    has_more: bool


@dataclass(frozen=True, slots=True)
class WorldValueChunk:
    """UTF-8-safe chunk of one canonical assertion value or change payload."""

    reference_kind: str
    reference_id: str
    offset_bytes: int
    next_offset_bytes: int
    total_bytes: int
    content: str
    full_sha256: str
    complete: bool


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

    def list_assertion_references_page(
        self,
        *,
        include_retracted: bool = False,
        delivery_scope: str = WORLD_ASSERTION_SCOPE_HISTORY,
        after_observed_at: str = "",
        after_assertion_id: str = "",
        limit: int = 128,
        inline_max_bytes: int = WORLD_REFERENCE_INLINE_MAX_BYTES,
    ) -> WorldAssertionReferencePage:
        """Read compact assertion metadata without materializing giant values."""

        scope = normalize_world_assertion_scope(delivery_scope)
        current_snapshot = scope == WORLD_ASSERTION_SCOPE_CURRENT_SNAPSHOT
        if current_snapshot and include_retracted:
            raise ValueError("current World snapshot cannot include retracted evidence")
        page_limit = max(1, min(int(limit), 1000))
        inline_limit = max(0, min(int(inline_max_bytes), 16 * 1024))
        predicates: list[str] = []
        params: list[Any] = []
        if not include_retracted:
            predicates.append("retracted_at = ''")
        if current_snapshot:
            predicates.extend(
                [
                    "predicate <> 'session_state'",
                    "NOT (predicate = 'legacy_snapshot' AND status = 'legacy_import')",
                ]
            )
        if after_observed_at or after_assertion_id:
            operator = "<" if current_snapshot else ">"
            predicates.append(
                f"(observed_at {operator} ? OR "
                f"(observed_at = ? AND assertion_id {operator} ?))"
            )
            params.extend(
                [
                    str(after_observed_at or ""),
                    str(after_observed_at or ""),
                    str(after_assertion_id or ""),
                ]
            )
        where = f" WHERE {' AND '.join(predicates)}" if predicates else ""
        order = "DESC" if current_snapshot else "ASC"
        transport_echo_sql = """
            domain = 'minecraft' AND predicate = 'embodied_trace'
            AND json_extract(payload_json, '$.value.trace_kind') = 'intent.issued'
            AND json_type(
                payload_json,
                '$.value.payload.context.transient_world_perception'
            ) IS NOT NULL
        """
        with self._connect() as db:
            totals = db.execute(
                "SELECT COUNT(*) AS total_items, "
                "COALESCE(SUM(length(CAST(value_json AS BLOB))), 0) "
                f"AS total_value_bytes FROM world_assertions{where}",
                params,
            ).fetchone()
            rows = db.execute(
                "SELECT assertion_id, subject, predicate, domain, status, "
                "source_instance_id, source_event_id, occurrence_id, observed_at, "
                "valid_from, valid_to, recorded_at, supersedes_assertion_id, "
                "length(CAST(value_json AS BLOB)) AS value_bytes, "
                "CASE WHEN length(CAST(value_json AS BLOB)) <= ? "
                "THEN value_json ELSE NULL END AS inline_value_json, "
                f"CASE WHEN {transport_echo_sql} THEN 1 ELSE 0 END AS transport_echo "
                f"FROM world_assertions{where} "
                f"ORDER BY observed_at {order}, assertion_id {order} LIMIT ?",
                [inline_limit, *params, page_limit + 1],
            ).fetchall()
        has_more = len(rows) > page_limit
        selected = rows[:page_limit]
        items = tuple(
            WorldAssertionReference(
                assertion_id=str(row["assertion_id"]),
                subject=str(row["subject"]),
                predicate=str(row["predicate"]),
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
                value_bytes=int(row["value_bytes"]),
                value_inlined=row["inline_value_json"] is not None,
                value=(
                    json.loads(str(row["inline_value_json"]))
                    if row["inline_value_json"] is not None
                    else None
                ),
                transport_echo=bool(row["transport_echo"]),
            )
            for row in selected
        )
        last = selected[-1] if selected and has_more else None
        return WorldAssertionReferencePage(
            items=items,
            total_items=int(totals["total_items"] if totals is not None else 0),
            total_value_bytes=int(
                totals["total_value_bytes"] if totals is not None else 0
            ),
            next_after_observed_at=(str(last["observed_at"]) if last else ""),
            next_after_assertion_id=(str(last["assertion_id"]) if last else ""),
            delivery_scope=scope,
            result_order=(
                WORLD_ASSERTION_ORDER_NEWEST_FIRST
                if current_snapshot
                else WORLD_ASSERTION_ORDER_OLDEST_FIRST
            ),
        )

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

    def change_references_page(
        self,
        ingest_position: int,
        *,
        through_position: int,
        limit: int = 128,
        inline_max_bytes: int = WORLD_REFERENCE_INLINE_MAX_BYTES,
    ) -> WorldChangeReferencePage:
        """Read one compact cursor page without loading oversized payload JSON."""

        start = int(ingest_position)
        through = int(through_position)
        if start < 0 or through < start:
            raise ValueError("world change reference window is invalid")
        page_limit = max(1, min(int(limit), 1000))
        inline_limit = max(0, min(int(inline_max_bytes), 16 * 1024))
        transport_echo_sql = """
            change_kind = 'world_observation'
            AND json_extract(
                payload_json,
                '$.assertion.value.trace_kind'
            ) = 'intent.issued'
            AND json_extract(payload_json, '$.assertion.domain') = 'minecraft'
            AND json_extract(payload_json, '$.assertion.predicate') = 'embodied_trace'
            AND json_type(
                payload_json,
                '$.assertion.value.payload.context.transient_world_perception'
            ) IS NOT NULL
        """
        with self._connect() as db:
            totals = db.execute(
                "SELECT COUNT(*) AS total_items, "
                "COALESCE(SUM(length(CAST(payload_json AS BLOB))), 0) "
                "AS total_payload_bytes FROM world_projection_changes "
                "WHERE ingest_position > ? AND ingest_position <= ?",
                (start, through),
            ).fetchone()
            rows = db.execute(
                "SELECT ingest_position, event_id, occurrence_id, event_type, "
                "change_kind, source_instance_id, stream_id, occurred_at, "
                "recorded_at, length(CAST(payload_json AS BLOB)) AS payload_bytes, "
                "CASE WHEN length(CAST(payload_json AS BLOB)) <= ? "
                "THEN payload_json ELSE NULL END AS inline_payload_json, "
                f"CASE WHEN {transport_echo_sql} THEN 1 ELSE 0 END AS transport_echo "
                "FROM world_projection_changes WHERE ingest_position > ? "
                "AND ingest_position <= ? ORDER BY ingest_position LIMIT ?",
                (inline_limit, start, through, page_limit + 1),
            ).fetchall()
        selected = rows[:page_limit]
        items = tuple(
            WorldChangeReference(
                ingest_position=int(row["ingest_position"]),
                event_id=str(row["event_id"]),
                occurrence_id=str(row["occurrence_id"]),
                event_type=str(row["event_type"]),
                change_kind=str(row["change_kind"]),
                source_instance_id=str(row["source_instance_id"]),
                stream_id=str(row["stream_id"]),
                occurred_at=str(row["occurred_at"]),
                recorded_at=str(row["recorded_at"]),
                payload_bytes=int(row["payload_bytes"]),
                payload_inlined=row["inline_payload_json"] is not None,
                payload=(
                    json.loads(str(row["inline_payload_json"]))
                    if row["inline_payload_json"] is not None
                    else {}
                ),
                transport_echo=bool(row["transport_echo"]),
            )
            for row in selected
        )
        return WorldChangeReferencePage(
            items=items,
            total_items=int(totals["total_items"] if totals is not None else 0),
            total_payload_bytes=int(
                totals["total_payload_bytes"] if totals is not None else 0
            ),
            has_more=len(rows) > page_limit,
        )

    @staticmethod
    def _value_chunk(
        raw: str,
        *,
        reference_kind: str,
        reference_id: str,
        offset_bytes: int,
        max_bytes: int,
    ) -> WorldValueChunk:
        """Slice canonical JSON on UTF-8 boundaries and include its full digest."""

        encoded = raw.encode("utf-8")
        offset = int(offset_bytes)
        limit = int(max_bytes)
        if offset < 0 or offset > len(encoded):
            raise ValueError("world value chunk offset is outside the source")
        if limit < 4 or limit > WORLD_VALUE_CHUNK_MAX_BYTES:
            raise ValueError("world value chunk max_bytes is outside the safe range")
        if offset < len(encoded) and encoded[offset] & 0xC0 == 0x80:
            raise ValueError("world value chunk offset must be a UTF-8 boundary")
        end = min(len(encoded), offset + limit)
        while end > offset and end < len(encoded) and encoded[end] & 0xC0 == 0x80:
            end -= 1
        content = encoded[offset:end].decode("utf-8")
        return WorldValueChunk(
            reference_kind=reference_kind,
            reference_id=reference_id,
            offset_bytes=offset,
            next_offset_bytes=end,
            total_bytes=len(encoded),
            content=content,
            full_sha256=hashlib.sha256(encoded).hexdigest(),
            complete=end == len(encoded),
        )

    def read_assertion_value_chunk(
        self,
        assertion_id: str,
        *,
        offset_bytes: int = 0,
        max_bytes: int = 16 * 1024,
    ) -> WorldValueChunk:
        """Read one explicit assertion value chunk without changing projection state."""

        identity = str(assertion_id or "").strip()
        if not identity:
            raise ValueError("assertion_id must not be empty")
        with self._connect() as db:
            row = db.execute(
                "SELECT value_json FROM world_assertions WHERE assertion_id = ?",
                (identity,),
            ).fetchone()
        if row is None:
            raise KeyError(identity)
        return self._value_chunk(
            str(row["value_json"]),
            reference_kind="assertion_value",
            reference_id=identity,
            offset_bytes=offset_bytes,
            max_bytes=max_bytes,
        )

    def read_change_payload_chunk(
        self,
        ingest_position: int,
        *,
        offset_bytes: int = 0,
        max_bytes: int = 16 * 1024,
    ) -> WorldValueChunk:
        """Read one explicit change payload chunk by its stable ledger position."""

        position = int(ingest_position)
        if position < 0:
            raise ValueError("ingest_position must not be negative")
        with self._connect() as db:
            row = db.execute(
                "SELECT payload_json FROM world_projection_changes "
                "WHERE ingest_position = ?",
                (position,),
            ).fetchone()
        if row is None:
            raise KeyError(str(position))
        return self._value_chunk(
            str(row["payload_json"]),
            reference_kind="change_payload",
            reference_id=str(position),
            offset_bytes=offset_bytes,
            max_bytes=max_bytes,
        )

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
