"""Durable SQLite state for the offline-first synchronization kernel."""

from __future__ import annotations

import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from .models import (
    ClaimedOutboxEvent,
    SyncEnvelope,
    SyncStatus,
    canonical_json,
    sha256_text,
)


def _now_iso() -> str:
    return datetime.now(UTC).astimezone().isoformat()


def create_local_sync_schema(db: sqlite3.Connection) -> None:
    """Create sync tables without changing any existing application table."""

    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS sync_node_identity (
            singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
            node_id TEXT NOT NULL UNIQUE,
            next_sequence INTEGER NOT NULL DEFAULT 1 CHECK (next_sequence > 0),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sync_outbox (
            outbox_id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL UNIQUE,
            origin_node_id TEXT NOT NULL,
            origin_sequence INTEGER,
            occurred_at TEXT NOT NULL,
            recorded_at TEXT NOT NULL,
            event_type TEXT NOT NULL,
            actor_id TEXT NOT NULL DEFAULT '',
            consciousness_instance_id TEXT NOT NULL DEFAULT '',
            visibility TEXT NOT NULL DEFAULT 'private',
            causation_id TEXT NOT NULL DEFAULT '',
            correlation_id TEXT NOT NULL DEFAULT '',
            payload_json TEXT NOT NULL,
            payload_hash TEXT NOT NULL,
            schema_version INTEGER NOT NULL DEFAULT 1,
            state TEXT NOT NULL CHECK (
                state IN ('held', 'pending', 'inflight', 'retry', 'confirmed', 'conflict')
            ),
            attempt_count INTEGER NOT NULL DEFAULT 0,
            available_at REAL NOT NULL DEFAULT 0,
            lease_token TEXT NOT NULL DEFAULT '',
            lease_until REAL NOT NULL DEFAULT 0,
            last_attempt_at TEXT NOT NULL DEFAULT '',
            last_error TEXT NOT NULL DEFAULT '',
            remote_position INTEGER NOT NULL DEFAULT 0,
            confirmed_at TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(origin_node_id, origin_sequence)
        );
        CREATE INDEX IF NOT EXISTS idx_sync_outbox_delivery
            ON sync_outbox(state, available_at, origin_sequence);
        CREATE TABLE IF NOT EXISTS sync_inbox (
            inbox_id INTEGER PRIMARY KEY AUTOINCREMENT,
            remote_position INTEGER NOT NULL UNIQUE,
            event_id TEXT NOT NULL UNIQUE,
            origin_node_id TEXT NOT NULL,
            origin_sequence INTEGER NOT NULL,
            occurred_at TEXT NOT NULL,
            recorded_at TEXT NOT NULL,
            event_type TEXT NOT NULL,
            actor_id TEXT NOT NULL DEFAULT '',
            consciousness_instance_id TEXT NOT NULL DEFAULT '',
            visibility TEXT NOT NULL,
            causation_id TEXT NOT NULL DEFAULT '',
            correlation_id TEXT NOT NULL DEFAULT '',
            payload_json TEXT NOT NULL,
            payload_hash TEXT NOT NULL,
            schema_version INTEGER NOT NULL DEFAULT 1,
            state TEXT NOT NULL CHECK (state IN ('staged', 'applied', 'conflict')),
            last_error TEXT NOT NULL DEFAULT '',
            received_at TEXT NOT NULL,
            applied_at TEXT NOT NULL DEFAULT '',
            UNIQUE(origin_node_id, origin_sequence)
        );
        CREATE INDEX IF NOT EXISTS idx_sync_inbox_apply
            ON sync_inbox(state, remote_position);
        CREATE TABLE IF NOT EXISTS sync_cursors (
            consumer_id TEXT PRIMARY KEY,
            remote_position INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sync_conflicts (
            conflict_id INTEGER PRIMARY KEY AUTOINCREMENT,
            direction TEXT NOT NULL,
            conflict_key TEXT NOT NULL,
            event_id TEXT NOT NULL DEFAULT '',
            expected_hash TEXT NOT NULL DEFAULT '',
            actual_hash TEXT NOT NULL DEFAULT '',
            detail TEXT NOT NULL DEFAULT '',
            state TEXT NOT NULL DEFAULT 'open',
            created_at TEXT NOT NULL,
            resolved_at TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_sync_conflicts_open
            ON sync_conflicts(state, created_at);
        CREATE TABLE IF NOT EXISTS sync_runtime_state (
            singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
            last_attempt_at TEXT NOT NULL DEFAULT '',
            last_success_at TEXT NOT NULL DEFAULT '',
            last_error TEXT NOT NULL DEFAULT '',
            remote_available INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL
        );
        CREATE TRIGGER IF NOT EXISTS sync_outbox_immutable_payload
        BEFORE UPDATE OF event_id, origin_node_id, occurred_at,
            recorded_at, event_type, actor_id, consciousness_instance_id,
            visibility, causation_id, correlation_id, payload_json, payload_hash,
            schema_version ON sync_outbox BEGIN
            SELECT RAISE(ABORT, 'SyncOutboxPayloadImmutable');
        END;
        CREATE TRIGGER IF NOT EXISTS sync_outbox_sequence_once
        BEFORE UPDATE OF origin_sequence ON sync_outbox
        WHEN NOT (
            OLD.state = 'held' AND OLD.origin_sequence IS NULL
            AND NEW.state = 'pending' AND NEW.origin_sequence IS NOT NULL
        ) BEGIN
            SELECT RAISE(ABORT, 'SyncOutboxSequenceImmutable');
        END;
        CREATE TRIGGER IF NOT EXISTS sync_outbox_no_delete
        BEFORE DELETE ON sync_outbox BEGIN
            SELECT RAISE(ABORT, 'SyncOutboxDeleteForbidden');
        END;
        CREATE TRIGGER IF NOT EXISTS sync_inbox_immutable_payload
        BEFORE UPDATE OF remote_position, event_id, origin_node_id,
            origin_sequence, occurred_at, recorded_at, event_type, actor_id,
            consciousness_instance_id, visibility, causation_id,
            correlation_id, payload_json, payload_hash, schema_version
        ON sync_inbox BEGIN
            SELECT RAISE(ABORT, 'SyncInboxPayloadImmutable');
        END;
        CREATE TRIGGER IF NOT EXISTS sync_inbox_no_delete
        BEFORE DELETE ON sync_inbox BEGIN
            SELECT RAISE(ABORT, 'SyncInboxDeleteForbidden');
        END;
        """
    )
    now = _now_iso()
    db.execute(
        """INSERT OR IGNORE INTO sync_runtime_state
        (singleton_id, updated_at) VALUES (1, ?)""",
        (now,),
    )


def _node_identity(db: sqlite3.Connection) -> tuple[str, int]:
    row = db.execute(
        "SELECT node_id, next_sequence FROM sync_node_identity WHERE singleton_id = 1"
    ).fetchone()
    if row is None:
        now = _now_iso()
        node_id = "node_" + uuid4().hex
        db.execute(
            """INSERT OR IGNORE INTO sync_node_identity
            (singleton_id, node_id, next_sequence, created_at, updated_at)
            VALUES (1, ?, 1, ?, ?)""",
            (node_id, now, now),
        )
        row = db.execute(
            "SELECT node_id, next_sequence FROM sync_node_identity WHERE singleton_id = 1"
        ).fetchone()
        if row is None:
            raise RuntimeError("SyncNodeIdentityInsertLost")
    return str(row["node_id"]), int(row["next_sequence"])


def _allocate_sequence(db: sqlite3.Connection) -> tuple[str, int]:
    node_id, sequence = _node_identity(db)
    db.execute(
        """UPDATE sync_node_identity SET next_sequence = ?, updated_at = ?
        WHERE singleton_id = 1""",
        (sequence + 1, _now_iso()),
    )
    return node_id, sequence


def enqueue_in_transaction(
    db: sqlite3.Connection,
    *,
    event_id: str,
    occurred_at: str,
    recorded_at: str,
    event_type: str,
    payload: Any,
    actor_id: str = "",
    consciousness_instance_id: str = "",
    visibility: str = "private",
    causation_id: str = "",
    correlation_id: str = "",
    export_requested: bool = False,
) -> str:
    """Append to the local Outbox inside the caller's SQLite transaction.

    Events without an export request remain only in their authoritative local
    ledger. Requested but private events are retained in ``held`` state and
    receive no origin sequence. This makes accidental export impossible and
    prevents sequence gaps in the remote stream.
    """

    normalized_visibility = str(visibility or "private").strip().lower()
    payload_json = canonical_json(payload)
    payload_hash = sha256_text(payload_json)
    existing = db.execute(
        "SELECT payload_hash, state FROM sync_outbox WHERE event_id = ?",
        (str(event_id),),
    ).fetchone()
    if existing is not None:
        if str(existing["payload_hash"]) != payload_hash:
            raise ValueError(f"SyncOutboxEventConflict:{event_id}")
        return str(existing["state"])
    if not export_requested:
        return "local_only"
    export_allowed = normalized_visibility in {"shared", "public"}

    if export_allowed:
        node_id, sequence = _allocate_sequence(db)
        state = SyncStatus.PENDING.value
    else:
        node_id, _ = _node_identity(db)
        sequence = None
        state = SyncStatus.HELD.value
    now = _now_iso()
    db.execute(
        """INSERT INTO sync_outbox (
            event_id, origin_node_id, origin_sequence, occurred_at, recorded_at,
            event_type, actor_id, consciousness_instance_id, visibility,
            causation_id, correlation_id, payload_json, payload_hash,
            schema_version, state, available_at, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, 0, ?, ?)""",
        (
            str(event_id),
            node_id,
            sequence,
            str(occurred_at),
            str(recorded_at),
            str(event_type),
            str(actor_id),
            str(consciousness_instance_id),
            normalized_visibility,
            str(causation_id),
            str(correlation_id),
            payload_json,
            payload_hash,
            state,
            now,
            now,
        ),
    )
    return state


class LocalSyncStore:
    """Own local Outbox, Inbox, cursor, conflict and health state."""

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path).resolve()

    def _connect(self) -> sqlite3.Connection:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.database_path, timeout=30.0, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode = WAL")
        db.execute("PRAGMA synchronous = FULL")
        db.execute("PRAGMA busy_timeout = 30000")
        return db

    def ensure_schema(self) -> None:
        with self._connect() as db:
            create_local_sync_schema(db)
            _node_identity(db)

    def node_id(self) -> str:
        self.ensure_schema()
        with self._connect() as db:
            node_id, _ = _node_identity(db)
        return node_id

    def enqueue(
        self,
        *,
        event_id: str,
        occurred_at: str,
        recorded_at: str,
        event_type: str,
        payload: Any,
        actor_id: str = "",
        consciousness_instance_id: str = "",
        visibility: str = "private",
        causation_id: str = "",
        correlation_id: str = "",
        export_requested: bool = False,
    ) -> str:
        self.ensure_schema()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                state = enqueue_in_transaction(
                    db,
                    event_id=event_id,
                    occurred_at=occurred_at,
                    recorded_at=recorded_at,
                    event_type=event_type,
                    payload=payload,
                    actor_id=actor_id,
                    consciousness_instance_id=consciousness_instance_id,
                    visibility=visibility,
                    causation_id=causation_id,
                    correlation_id=correlation_id,
                    export_requested=export_requested,
                )
                db.commit()
                return state
            except BaseException:
                db.rollback()
                raise

    def release_held(self, event_id: str) -> int:
        """Explicitly authorize a held shared/public event for delivery."""

        self.ensure_schema()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                row = db.execute(
                    "SELECT state, visibility FROM sync_outbox WHERE event_id = ?",
                    (str(event_id),),
                ).fetchone()
                if row is None:
                    raise KeyError(event_id)
                if str(row["state"]) != SyncStatus.HELD.value:
                    raise ValueError(f"SyncOutboxNotHeld:{event_id}")
                if str(row["visibility"]) not in {"shared", "public"}:
                    raise PermissionError(f"SyncOutboxPrivate:{event_id}")
                _, sequence = _allocate_sequence(db)
                db.execute(
                    """UPDATE sync_outbox SET origin_sequence = ?, state = 'pending',
                    available_at = 0, updated_at = ? WHERE event_id = ?""",
                    (sequence, _now_iso(), str(event_id)),
                )
                db.commit()
                return sequence
            except BaseException:
                db.rollback()
                raise

    @staticmethod
    def _envelope(row: sqlite3.Row) -> SyncEnvelope:
        return SyncEnvelope(
            event_id=str(row["event_id"]),
            origin_node_id=str(row["origin_node_id"]),
            origin_sequence=int(row["origin_sequence"]),
            occurred_at=str(row["occurred_at"]),
            recorded_at=str(row["recorded_at"]),
            event_type=str(row["event_type"]),
            actor_id=str(row["actor_id"]),
            consciousness_instance_id=str(row["consciousness_instance_id"]),
            visibility=str(row["visibility"]),
            causation_id=str(row["causation_id"]),
            correlation_id=str(row["correlation_id"]),
            payload_json=str(row["payload_json"]),
            payload_hash=str(row["payload_hash"]),
            schema_version=int(row["schema_version"]),
        )

    def claim_next(
        self,
        *,
        lease_seconds: float,
        allowed_visibilities: set[str],
    ) -> ClaimedOutboxEvent | None:
        """Claim the first unresolved sequence; never skip a conflict/failure."""

        self.ensure_schema()
        now_epoch = time.time()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute(
                    """UPDATE sync_outbox SET state = 'retry', lease_token = '',
                    lease_until = 0, available_at = 0,
                    last_error = CASE WHEN last_error = '' THEN 'stale lease recovered'
                                      ELSE last_error END,
                    updated_at = ?
                    WHERE state = 'inflight' AND lease_until <= ?""",
                    (_now_iso(), now_epoch),
                )
                row = db.execute(
                    """SELECT * FROM sync_outbox
                    WHERE origin_sequence IS NOT NULL
                      AND state IN ('pending', 'retry', 'inflight', 'conflict')
                    ORDER BY origin_sequence LIMIT 1"""
                ).fetchone()
                if row is None:
                    db.commit()
                    return None
                state = str(row["state"])
                if state in {SyncStatus.CONFLICT.value, SyncStatus.INFLIGHT.value}:
                    db.commit()
                    return None
                if float(row["available_at"] or 0) > now_epoch:
                    db.commit()
                    return None
                if str(row["visibility"]) not in allowed_visibilities:
                    self._record_conflict(
                        db,
                        direction="push",
                        conflict_key=f"visibility:{row['event_id']}",
                        event_id=str(row["event_id"]),
                        expected_hash=str(row["payload_hash"]),
                        actual_hash="",
                        detail=f"visibility {row['visibility']!r} is not exportable",
                    )
                    db.execute(
                        """UPDATE sync_outbox SET state = 'conflict',
                        last_error = 'visibility policy rejected event', updated_at = ?
                        WHERE outbox_id = ?""",
                        (_now_iso(), int(row["outbox_id"])),
                    )
                    db.commit()
                    return None
                lease_token = uuid4().hex
                attempt_count = int(row["attempt_count"]) + 1
                now_iso = _now_iso()
                db.execute(
                    """UPDATE sync_outbox SET state = 'inflight', attempt_count = ?,
                    lease_token = ?, lease_until = ?, last_attempt_at = ?,
                    updated_at = ? WHERE outbox_id = ?""",
                    (
                        attempt_count,
                        lease_token,
                        now_epoch + max(1.0, float(lease_seconds)),
                        now_iso,
                        now_iso,
                        int(row["outbox_id"]),
                    ),
                )
                db.commit()
                return ClaimedOutboxEvent(
                    envelope=self._envelope(row),
                    lease_token=lease_token,
                    attempt_count=attempt_count,
                )
            except BaseException:
                db.rollback()
                raise

    def confirm(self, event_id: str, lease_token: str, remote_position: int) -> None:
        self._finish_claim(
            event_id,
            lease_token,
            state=SyncStatus.CONFIRMED.value,
            remote_position=int(remote_position),
            error="",
            available_at=0,
        )

    def retry(
        self,
        event_id: str,
        lease_token: str,
        *,
        error: str,
        delay_seconds: float,
    ) -> None:
        self._finish_claim(
            event_id,
            lease_token,
            state=SyncStatus.RETRY.value,
            remote_position=0,
            error=error,
            available_at=time.time() + max(0.0, float(delay_seconds)),
        )

    def conflict(
        self,
        event_id: str,
        lease_token: str,
        *,
        expected_hash: str,
        actual_hash: str,
        detail: str,
    ) -> None:
        self.ensure_schema()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                self._assert_claim(db, event_id, lease_token)
                self._record_conflict(
                    db,
                    direction="push",
                    conflict_key=f"event:{event_id}",
                    event_id=event_id,
                    expected_hash=expected_hash,
                    actual_hash=actual_hash,
                    detail=detail,
                )
                db.execute(
                    """UPDATE sync_outbox SET state = 'conflict', lease_token = '',
                    lease_until = 0, last_error = ?, updated_at = ?
                    WHERE event_id = ?""",
                    (str(detail)[:2000], _now_iso(), str(event_id)),
                )
                db.commit()
            except BaseException:
                db.rollback()
                raise

    @staticmethod
    def _assert_claim(db: sqlite3.Connection, event_id: str, lease_token: str) -> None:
        row = db.execute(
            "SELECT state, lease_token FROM sync_outbox WHERE event_id = ?",
            (str(event_id),),
        ).fetchone()
        if row is None:
            raise KeyError(event_id)
        if str(row["state"]) != SyncStatus.INFLIGHT.value or str(
            row["lease_token"]
        ) != str(lease_token):
            raise RuntimeError(f"SyncOutboxLeaseLost:{event_id}")

    def _finish_claim(
        self,
        event_id: str,
        lease_token: str,
        *,
        state: str,
        remote_position: int,
        error: str,
        available_at: float,
    ) -> None:
        self.ensure_schema()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                self._assert_claim(db, event_id, lease_token)
                now = _now_iso()
                db.execute(
                    """UPDATE sync_outbox SET state = ?, available_at = ?,
                    lease_token = '', lease_until = 0, last_error = ?,
                    remote_position = CASE WHEN ? > 0 THEN ? ELSE remote_position END,
                    confirmed_at = CASE WHEN ? = 'confirmed' THEN ? ELSE confirmed_at END,
                    updated_at = ? WHERE event_id = ?""",
                    (
                        state,
                        float(available_at),
                        str(error)[:2000],
                        int(remote_position),
                        int(remote_position),
                        state,
                        now,
                        now,
                        str(event_id),
                    ),
                )
                db.commit()
            except BaseException:
                db.rollback()
                raise

    @staticmethod
    def _record_conflict(
        db: sqlite3.Connection,
        *,
        direction: str,
        conflict_key: str,
        event_id: str,
        expected_hash: str,
        actual_hash: str,
        detail: str,
    ) -> None:
        db.execute(
            """INSERT INTO sync_conflicts (
                direction, conflict_key, event_id, expected_hash, actual_hash,
                detail, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                str(direction),
                str(conflict_key),
                str(event_id),
                str(expected_hash),
                str(actual_hash),
                str(detail)[:2000],
                _now_iso(),
            ),
        )

    def stage_inbox(self, remote_position: int, envelope: SyncEnvelope) -> str:
        """Persist a remote event before application; detect every key collision."""

        self.ensure_schema()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                row = db.execute(
                    """SELECT * FROM sync_inbox WHERE remote_position = ?
                    OR event_id = ? OR (origin_node_id = ? AND origin_sequence = ?)
                    ORDER BY inbox_id LIMIT 1""",
                    (
                        int(remote_position),
                        envelope.event_id,
                        envelope.origin_node_id,
                        envelope.origin_sequence,
                    ),
                ).fetchone()
                if row is not None:
                    same = (
                        int(row["remote_position"]) == int(remote_position)
                        and str(row["event_id"]) == envelope.event_id
                        and str(row["origin_node_id"]) == envelope.origin_node_id
                        and int(row["origin_sequence"]) == envelope.origin_sequence
                        and str(row["payload_hash"]) == envelope.payload_hash
                    )
                    if same:
                        db.commit()
                        return "duplicate"
                    detail = "remote position, event id, or origin sequence collision"
                    self._record_conflict(
                        db,
                        direction="pull",
                        conflict_key=f"remote:{remote_position}",
                        event_id=envelope.event_id,
                        expected_hash=str(row["payload_hash"]),
                        actual_hash=envelope.payload_hash,
                        detail=detail,
                    )
                    db.commit()
                    return "conflict"
                db.execute(
                    """INSERT INTO sync_inbox (
                        remote_position, event_id, origin_node_id, origin_sequence,
                        occurred_at, recorded_at, event_type, actor_id,
                        consciousness_instance_id, visibility, causation_id,
                        correlation_id, payload_json, payload_hash, schema_version,
                        state, received_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'staged', ?)""",
                    (
                        int(remote_position),
                        envelope.event_id,
                        envelope.origin_node_id,
                        envelope.origin_sequence,
                        envelope.occurred_at,
                        envelope.recorded_at,
                        envelope.event_type,
                        envelope.actor_id,
                        envelope.consciousness_instance_id,
                        envelope.visibility,
                        envelope.causation_id,
                        envelope.correlation_id,
                        envelope.payload_json,
                        envelope.payload_hash,
                        envelope.schema_version,
                        _now_iso(),
                    ),
                )
                db.commit()
                return "staged"
            except BaseException:
                db.rollback()
                raise

    def staged_inbox(
        self, consumer_id: str, *, limit: int
    ) -> list[tuple[int, SyncEnvelope]]:
        self.ensure_schema()
        cursor = self.cursor(consumer_id)
        with self._connect() as db:
            rows = db.execute(
                """SELECT * FROM sync_inbox WHERE state = 'staged'
                AND remote_position > ? ORDER BY remote_position LIMIT ?""",
                (cursor, max(1, int(limit))),
            ).fetchall()
        return [(int(row["remote_position"]), self._envelope(row)) for row in rows]

    def cursor(self, consumer_id: str) -> int:
        self.ensure_schema()
        with self._connect() as db:
            row = db.execute(
                "SELECT remote_position FROM sync_cursors WHERE consumer_id = ?",
                (str(consumer_id),),
            ).fetchone()
        return int(row["remote_position"]) if row is not None else 0

    def mark_inbox_applied(self, consumer_id: str, remote_position: int) -> None:
        self.ensure_schema()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                row = db.execute(
                    "SELECT state FROM sync_inbox WHERE remote_position = ?",
                    (int(remote_position),),
                ).fetchone()
                if row is None:
                    raise KeyError(remote_position)
                now = _now_iso()
                db.execute(
                    """UPDATE sync_inbox SET state = 'applied', last_error = '',
                    applied_at = ? WHERE remote_position = ?""",
                    (now, int(remote_position)),
                )
                current = db.execute(
                    "SELECT remote_position FROM sync_cursors WHERE consumer_id = ?",
                    (str(consumer_id),),
                ).fetchone()
                position = max(
                    int(remote_position),
                    int(current["remote_position"]) if current is not None else 0,
                )
                db.execute(
                    """INSERT INTO sync_cursors (consumer_id, remote_position, updated_at)
                    VALUES (?, ?, ?) ON CONFLICT(consumer_id) DO UPDATE SET
                    remote_position = excluded.remote_position,
                    updated_at = excluded.updated_at""",
                    (str(consumer_id), position, now),
                )
                db.commit()
            except BaseException:
                db.rollback()
                raise

    def record_inbox_error(self, remote_position: int, error: str) -> None:
        self.ensure_schema()
        with self._connect() as db:
            db.execute(
                """UPDATE sync_inbox SET last_error = ?
                WHERE remote_position = ? AND state = 'staged'""",
                (str(error)[:2000], int(remote_position)),
            )

    def update_runtime(
        self,
        *,
        success: bool,
        error: str = "",
        remote_available: bool | None = None,
    ) -> None:
        self.ensure_schema()
        now = _now_iso()
        available = (
            bool(success) if remote_available is None else bool(remote_available)
        )
        with self._connect() as db:
            db.execute(
                """UPDATE sync_runtime_state SET last_attempt_at = ?,
                last_success_at = CASE WHEN ? THEN ? ELSE last_success_at END,
                last_error = ?, remote_available = ?, updated_at = ?
                WHERE singleton_id = 1""",
                (now, int(success), now, str(error)[:2000], int(available), now),
            )

    def health_snapshot(self) -> dict[str, Any]:
        """Return counters and timestamps only; never expose payload or secrets."""

        self.ensure_schema()
        with self._connect() as db:
            counts = {
                str(row["state"]): int(row["total"])
                for row in db.execute(
                    "SELECT state, COUNT(*) AS total FROM sync_outbox GROUP BY state"
                ).fetchall()
            }
            inbox = {
                str(row["state"]): int(row["total"])
                for row in db.execute(
                    "SELECT state, COUNT(*) AS total FROM sync_inbox GROUP BY state"
                ).fetchall()
            }
            conflicts = db.execute(
                "SELECT COUNT(*) AS total FROM sync_conflicts WHERE state = 'open'"
            ).fetchone()
            runtime = db.execute(
                "SELECT * FROM sync_runtime_state WHERE singleton_id = 1"
            ).fetchone()
            node_id, next_sequence = _node_identity(db)
        unresolved = sum(
            counts.get(state, 0) for state in ("pending", "retry", "inflight")
        )
        conflict_count = int(conflicts["total"] or 0) if conflicts is not None else 0
        last_error = str(runtime["last_error"] or "") if runtime is not None else ""
        degraded_reason = ""
        if conflict_count:
            degraded_reason = "open synchronization conflict"
        elif last_error:
            degraded_reason = last_error
        return {
            "component": "offline_sync",
            "database_path": str(self.database_path),
            "node_id": node_id,
            "next_origin_sequence": next_sequence,
            "outbox": counts,
            "outbox_backlog": unresolved,
            "held_count": counts.get("held", 0),
            "inbox": inbox,
            "open_conflict_count": conflict_count,
            "last_attempt_at": str(runtime["last_attempt_at"] or "") if runtime else "",
            "last_success_at": str(runtime["last_success_at"] or "") if runtime else "",
            "remote_available": bool(runtime["remote_available"]) if runtime else False,
            "degraded_reason": degraded_reason,
        }

    def debug_outbox_row(self, event_id: str) -> dict[str, Any] | None:
        """Test/operations helper; callers must not publish its payload."""

        self.ensure_schema()
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM sync_outbox WHERE event_id = ?", (str(event_id),)
            ).fetchone()
        return dict(row) if row is not None else None
