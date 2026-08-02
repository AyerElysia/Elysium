"""Transactional runtime presence storage for consciousness instances.

Presence is operational truth: which runtime instance exists, which streams it
owns, and whether its lease is still alive. It is deliberately separate from
the subjective WorldState projection.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

PRESENCE_DB_FILE = "consciousness_presence.sqlite3"


class PresenceRevisionConflict(RuntimeError):
    """Raised when a caller attempts to replace a stale presence revision."""


class StreamOwnershipConflict(ValueError):
    """Raised when two active instances attempt to own the same stream."""

    def __init__(self, stream_id: str, owner_id: str, claimant_id: str) -> None:
        """Describe the current owner and the rejected claimant."""

        self.stream_id = stream_id
        self.owner_id = owner_id
        self.claimant_id = claimant_id
        super().__init__(
            f"stream '{stream_id}' is already owned by active instance "
            f"'{owner_id}', cannot assign it to '{claimant_id}'"
        )


class SQLitePresenceStore:
    """SQLite-backed presence registry with a transactional event outbox."""

    def __init__(self, path: str | Path) -> None:
        """Initialize the durable store and its idempotent schema."""

        self.path = Path(path)
        self._ensure_schema()

    @staticmethod
    def _now_iso() -> str:
        """Return the current timezone-aware ingestion timestamp."""

        return datetime.now(UTC).astimezone().isoformat()

    def _connect(self) -> sqlite3.Connection:
        """Open one configured SQLite connection for a short transaction."""

        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=30.0, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode = WAL")
        db.execute("PRAGMA synchronous = FULL")
        db.execute("PRAGMA busy_timeout = 30000")
        db.execute("PRAGMA foreign_keys = ON")
        return db

    def _ensure_schema(self) -> None:
        """Create idempotent presence, ownership, and outbox tables."""

        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS consciousness_presence (
                    instance_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    display_name TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT '',
                    last_active_at TEXT NOT NULL DEFAULT '',
                    suspended_at TEXT NOT NULL DEFAULT '',
                    stream_ids_json TEXT NOT NULL DEFAULT '[]',
                    perception_filter_json TEXT NOT NULL DEFAULT '{}',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    session_id TEXT NOT NULL DEFAULT '',
                    process_epoch TEXT NOT NULL DEFAULT '',
                    lease_expires_at TEXT NOT NULL DEFAULT '',
                    lease_duration_seconds INTEGER,
                    revision INTEGER NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS consciousness_stream_owners (
                    stream_id TEXT PRIMARY KEY,
                    instance_id TEXT NOT NULL,
                    claimed_at TEXT NOT NULL,
                    FOREIGN KEY(instance_id)
                        REFERENCES consciousness_presence(instance_id)
                        ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_consciousness_presence_status
                    ON consciousness_presence(status, kind);
                CREATE INDEX IF NOT EXISTS idx_consciousness_presence_lease
                    ON consciousness_presence(status, lease_expires_at);
                CREATE TABLE IF NOT EXISTS consciousness_presence_outbox (
                    outbox_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    occurrence_id TEXT NOT NULL UNIQUE,
                    event_type TEXT NOT NULL,
                    instance_id TEXT NOT NULL,
                    stream_id TEXT NOT NULL DEFAULT '',
                    occurred_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    published_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_presence_outbox_pending
                    ON consciousness_presence_outbox(published_at, outbox_id);
                """
            )

    @staticmethod
    def _decode_row(row: sqlite3.Row) -> dict[str, Any]:
        """Decode one durable row into the registry transfer shape."""

        return {
            "instance_id": str(row["instance_id"]),
            "kind": str(row["kind"]),
            "display_name": str(row["display_name"]),
            "status": str(row["status"]),
            "created_at": str(row["created_at"]),
            "last_active_at": str(row["last_active_at"]),
            "suspended_at": str(row["suspended_at"]),
            "stream_ids": json.loads(str(row["stream_ids_json"])),
            "perception_filter": json.loads(
                str(row["perception_filter_json"])
            ),
            "metadata": json.loads(str(row["metadata_json"])),
            "session_id": str(row["session_id"]),
            "process_epoch": str(row["process_epoch"]),
            "lease_expires_at": str(row["lease_expires_at"]),
            "lease_duration_seconds": (
                int(row["lease_duration_seconds"])
                if row["lease_duration_seconds"] is not None
                else None
            ),
            "revision": int(row["revision"]),
        }

    def list_instances(self) -> list[dict[str, Any]]:
        """Return the latest durable snapshot of all presence instances."""

        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM consciousness_presence ORDER BY instance_id"
            ).fetchall()
        return [self._decode_row(row) for row in rows]

    def commit(
        self,
        instance: dict[str, Any],
        *,
        expected_revision: int | None,
        event_type: str,
        event_payload: dict[str, Any] | None = None,
    ) -> int:
        """Atomically persist presence, stream ownership, and an outbox event."""

        instance_id = str(instance.get("instance_id") or "").strip()
        if not instance_id:
            raise ValueError("consciousness instance_id must not be empty")
        streams = list(
            dict.fromkeys(
                str(value).strip()
                for value in (instance.get("stream_ids") or [])
                if str(value).strip()
            )
        )
        status = str(instance.get("status") or "").strip()
        now = self._now_iso()

        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                existing = db.execute(
                    "SELECT revision, status FROM consciousness_presence "
                    "WHERE instance_id = ?",
                    (instance_id,),
                ).fetchone()
                if expected_revision is None:
                    if existing is not None and str(existing["status"]) != "terminated":
                        raise ValueError(
                            f"consciousness instance '{instance_id}' already exists "
                            f"with status {existing['status']}"
                        )
                    previous_revision = (
                        int(existing["revision"]) if existing is not None else 0
                    )
                else:
                    if existing is None or int(existing["revision"]) != int(
                        expected_revision
                    ):
                        actual = int(existing["revision"]) if existing else None
                        raise PresenceRevisionConflict(
                            f"presence revision conflict for '{instance_id}': "
                            f"expected {expected_revision}, actual {actual}"
                        )
                    previous_revision = int(existing["revision"])

                revision = previous_revision + 1
                db.execute(
                    "DELETE FROM consciousness_stream_owners WHERE instance_id = ?",
                    (instance_id,),
                )
                db.execute(
                    """INSERT INTO consciousness_presence (
                        instance_id, kind, display_name, status, created_at,
                        last_active_at, suspended_at, stream_ids_json,
                        perception_filter_json, metadata_json, session_id,
                        process_epoch, lease_expires_at, lease_duration_seconds,
                        revision, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(instance_id) DO UPDATE SET
                        kind = excluded.kind,
                        display_name = excluded.display_name,
                        status = excluded.status,
                        created_at = excluded.created_at,
                        last_active_at = excluded.last_active_at,
                        suspended_at = excluded.suspended_at,
                        stream_ids_json = excluded.stream_ids_json,
                        perception_filter_json = excluded.perception_filter_json,
                        metadata_json = excluded.metadata_json,
                        session_id = excluded.session_id,
                        process_epoch = excluded.process_epoch,
                        lease_expires_at = excluded.lease_expires_at,
                        lease_duration_seconds = excluded.lease_duration_seconds,
                        revision = excluded.revision,
                        updated_at = excluded.updated_at""",
                    (
                        instance_id,
                        str(instance.get("kind") or ""),
                        str(instance.get("display_name") or ""),
                        status,
                        str(instance.get("created_at") or ""),
                        str(instance.get("last_active_at") or ""),
                        str(instance.get("suspended_at") or ""),
                        json.dumps(streams, ensure_ascii=False, separators=(",", ":")),
                        json.dumps(
                            instance.get("perception_filter") or {},
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                        json.dumps(
                            instance.get("metadata") or {},
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                        str(instance.get("session_id") or ""),
                        str(instance.get("process_epoch") or ""),
                        str(instance.get("lease_expires_at") or ""),
                        instance.get("lease_duration_seconds"),
                        revision,
                        now,
                    ),
                )

                if status == "active":
                    for stream_id in streams:
                        owner = db.execute(
                            "SELECT instance_id FROM consciousness_stream_owners "
                            "WHERE stream_id = ?",
                            (stream_id,),
                        ).fetchone()
                        if owner is not None and str(owner["instance_id"]) != instance_id:
                            raise StreamOwnershipConflict(
                                stream_id,
                                str(owner["instance_id"]),
                                instance_id,
                            )
                        db.execute(
                            "INSERT INTO consciousness_stream_owners "
                            "(stream_id, instance_id, claimed_at) VALUES (?, ?, ?)",
                            (stream_id, instance_id, now),
                        )

                if event_type:
                    occurrence_id = "presence_" + uuid4().hex
                    payload = dict(event_payload or {})
                    payload["instance"] = {**instance, "revision": revision}
                    payload["previous_revision"] = previous_revision
                    payload["revision"] = revision
                    occurred_at = str(
                        payload.get("occurred_at")
                        or instance.get("last_active_at")
                        or now
                    )
                    db.execute(
                        """INSERT INTO consciousness_presence_outbox (
                            occurrence_id, event_type, instance_id, stream_id,
                            occurred_at, payload_json
                        ) VALUES (?, ?, ?, ?, ?, ?)""",
                        (
                            occurrence_id,
                            event_type,
                            instance_id,
                            streams[0] if streams else "",
                            occurred_at,
                            json.dumps(
                                payload,
                                ensure_ascii=False,
                                sort_keys=True,
                                separators=(",", ":"),
                            ),
                        ),
                    )
                db.commit()
            except BaseException:
                db.rollback()
                raise
        return revision

    def pending_events(self, limit: int = 200) -> list[dict[str, Any]]:
        """Return unpublished lifecycle events without advancing the outbox."""

        with self._connect() as db:
            rows = db.execute(
                """SELECT * FROM consciousness_presence_outbox
                WHERE published_at IS NULL ORDER BY outbox_id LIMIT ?""",
                (max(1, int(limit)),),
            ).fetchall()
        return [
            {
                "outbox_id": int(row["outbox_id"]),
                "occurrence_id": str(row["occurrence_id"]),
                "event_type": str(row["event_type"]),
                "instance_id": str(row["instance_id"]),
                "stream_id": str(row["stream_id"]),
                "occurred_at": str(row["occurred_at"]),
                "payload": json.loads(str(row["payload_json"])),
            }
            for row in rows
        ]

    def acknowledge_events(self, outbox_ids: list[int]) -> None:
        """Mark events published only after the authoritative ledger accepts them."""

        if not outbox_ids:
            return
        placeholders = ",".join("?" for _ in outbox_ids)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute(
                    f"UPDATE consciousness_presence_outbox "
                    f"SET published_at = ? WHERE outbox_id IN ({placeholders})",
                    (self._now_iso(), *[int(value) for value in outbox_ids]),
                )
                db.commit()
            except BaseException:
                db.rollback()
                raise

    def health_snapshot(self) -> dict[str, Any]:
        """Return compact integrity and outbox health for service diagnostics."""

        with self._connect() as db:
            counts = db.execute(
                """SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN status = 'active' THEN 1 ELSE 0 END) AS active
                FROM consciousness_presence"""
            ).fetchone()
            pending = db.execute(
                """SELECT COUNT(*) AS count
                FROM consciousness_presence_outbox WHERE published_at IS NULL"""
            ).fetchone()
            owners = db.execute(
                "SELECT COUNT(*) AS count FROM consciousness_stream_owners"
            ).fetchone()
        return {
            "database_path": str(self.path),
            "instance_count": int(counts["total"] or 0),
            "active_count": int(counts["active"] or 0),
            "owned_stream_count": int(owners["count"] or 0),
            "pending_event_count": int(pending["count"] or 0),
        }
