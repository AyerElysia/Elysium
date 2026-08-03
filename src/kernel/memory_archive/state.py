"""Local rebuildable state for incremental unified-memory archive passes."""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from .models import ArchivePublishResult, ArchiveRecord


def _now_iso() -> str:
    return datetime.now(UTC).astimezone().isoformat()


class ArchiveState:
    """Track exact remote acknowledgements without modifying authority databases."""

    def __init__(self, path: Path) -> None:
        self.path = path.resolve()

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def ensure_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS archive_node_identity (
                    singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
                    node_id TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS archive_known_records (
                    record_id TEXT PRIMARY KEY,
                    payload_hash TEXT NOT NULL,
                    archive_position INTEGER NOT NULL,
                    first_confirmed_at TEXT NOT NULL,
                    last_confirmed_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS archive_runtime_state (
                    singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
                    last_attempt_at TEXT NOT NULL DEFAULT '',
                    last_success_at TEXT NOT NULL DEFAULT '',
                    last_manifest_id TEXT NOT NULL DEFAULT '',
                    last_error TEXT NOT NULL DEFAULT '',
                    remote_available INTEGER NOT NULL DEFAULT 0
                );
                INSERT OR IGNORE INTO archive_runtime_state(singleton_id)
                VALUES (1);
                """
            )

    def node_id(self) -> str:
        self.ensure_schema()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT node_id FROM archive_node_identity WHERE singleton_id = 1"
            ).fetchone()
            if row is not None:
                return str(row["node_id"])
            node_id = f"elysium-{uuid.uuid4()}"
            connection.execute(
                """INSERT INTO archive_node_identity(
                    singleton_id, node_id, created_at
                ) VALUES (1, ?, ?)""",
                (node_id, _now_iso()),
            )
            return node_id

    def exact_known_ids(self, records: Sequence[ArchiveRecord]) -> set[str]:
        if not records:
            return set()
        self.ensure_schema()
        known: set[str] = set()
        with self._connect() as connection:
            for start in range(0, len(records), 500):
                batch = records[start : start + 500]
                placeholders = ",".join("?" for _ in batch)
                hashes = {record.record_id: record.payload_hash for record in batch}
                rows = connection.execute(
                    "SELECT record_id, payload_hash FROM archive_known_records "
                    f"WHERE record_id IN ({placeholders})",
                    tuple(hashes),
                ).fetchall()
                known.update(
                    str(row["record_id"])
                    for row in rows
                    if hashes.get(str(row["record_id"])) == str(row["payload_hash"])
                )
        return known

    def remember(
        self,
        records: Sequence[ArchiveRecord],
        results: Sequence[ArchivePublishResult],
    ) -> None:
        if len(records) != len(results):
            raise ValueError("records and publish results must have equal length")
        now = _now_iso()
        rows = [
            (
                record.record_id,
                record.payload_hash,
                result.archive_position,
                now,
                now,
            )
            for record, result in zip(records, results, strict=True)
            if result.accepted
        ]
        if not rows:
            return
        self.ensure_schema()
        with self._connect() as connection:
            connection.executemany(
                """INSERT INTO archive_known_records (
                    record_id, payload_hash, archive_position,
                    first_confirmed_at, last_confirmed_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(record_id) DO UPDATE SET
                    payload_hash = excluded.payload_hash,
                    archive_position = excluded.archive_position,
                    last_confirmed_at = excluded.last_confirmed_at""",
                rows,
            )

    def update_runtime(
        self,
        *,
        success: bool,
        manifest_id: str = "",
        error: str = "",
        remote_available: bool,
    ) -> None:
        self.ensure_schema()
        now = _now_iso()
        with self._connect() as connection:
            connection.execute(
                """UPDATE archive_runtime_state SET
                    last_attempt_at = ?,
                    last_success_at = CASE WHEN ? THEN ? ELSE last_success_at END,
                    last_manifest_id = CASE WHEN ? THEN ? ELSE last_manifest_id END,
                    last_error = ?,
                    remote_available = ?
                WHERE singleton_id = 1""",
                (
                    now,
                    int(success),
                    now,
                    int(bool(manifest_id)),
                    manifest_id,
                    str(error)[:1000],
                    int(remote_available),
                ),
            )

    def health(self) -> dict[str, object]:
        self.ensure_schema()
        with self._connect() as connection:
            runtime = connection.execute(
                "SELECT * FROM archive_runtime_state WHERE singleton_id = 1"
            ).fetchone()
            known = int(
                connection.execute(
                    "SELECT COUNT(*) FROM archive_known_records"
                ).fetchone()[0]
            )
        return {
            "known_records": known,
            "last_attempt_at": str(runtime["last_attempt_at"]),
            "last_success_at": str(runtime["last_success_at"]),
            "last_manifest_id": str(runtime["last_manifest_id"]),
            "last_error": str(runtime["last_error"]),
            "remote_available": bool(runtime["remote_available"]),
        }
