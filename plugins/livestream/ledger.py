"""Append-only SQLite ledger for livestream facts and trace records."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiosqlite

from .domain import PlatformEvent

SCHEMA_VERSION = 1


class LedgerNotStartedError(RuntimeError):
    """Raised when a ledger operation is attempted before ``start``."""


class LedgerRecordConflictError(RuntimeError):
    """Raised when one stable record identity is reused with other content."""


@dataclass(frozen=True, slots=True)
class LedgerRecord:
    """One immutable row from the livestream ledger."""

    sequence: int
    record_id: str
    session_id: str
    kind: str
    occurred_at: float
    source: str
    correlation_id: str | None
    causation_id: str | None
    payload: dict[str, Any]
    payload_sha256: str
    recorded_at: float


@dataclass(frozen=True, slots=True)
class AppendResult:
    """Result of an idempotent ledger append."""

    sequence: int
    inserted: bool


def _canonical_payload(payload: dict[str, Any]) -> tuple[str, str]:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return encoded, hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _platform_source_hash(event: PlatformEvent) -> str:
    """Hash source meaning while ignoring local receipt identity and time.

    A reconnect can deliver the same source event through a new adapter object,
    which gives it a new local ``event_id`` and ``received_at``.  A declared
    ``dedup_key`` is therefore checked against the stable source observation,
    not against those local transport fields.
    """

    payload = event.to_payload()
    payload.pop("event_id", None)
    payload.pop("received_at", None)
    payload.pop("dedup_key", None)
    return _canonical_payload(payload)[1]


class LivestreamLedger:
    """Authoritative append-only history plus rebuildable consumer cursors.

    Records never change after insertion. Consumer cursors are projections and
    may advance only after the corresponding batch has been durably completed.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._db: aiosqlite.Connection | None = None
        self._write_lock = asyncio.Lock()

    async def start(self) -> None:
        """Open the database and apply the idempotent schema migration."""

        if self._db is not None:
            return
        await asyncio.to_thread(self.path.parent.mkdir, parents=True, exist_ok=True)
        db = await aiosqlite.connect(self.path)
        db.row_factory = aiosqlite.Row
        try:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("PRAGMA synchronous=FULL")
            await db.execute("PRAGMA foreign_keys=ON")
            await db.executescript(
                """
                CREATE TABLE IF NOT EXISTS livestream_schema (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    version INTEGER NOT NULL
                );
                INSERT INTO livestream_schema(singleton, version)
                VALUES (1, 1)
                ON CONFLICT(singleton) DO NOTHING;

                CREATE TABLE IF NOT EXISTS livestream_records (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id TEXT NOT NULL UNIQUE,
                    session_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    occurred_at REAL NOT NULL,
                    source TEXT NOT NULL,
                    correlation_id TEXT,
                    causation_id TEXT,
                    payload_json TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    recorded_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_livestream_records_session_sequence
                    ON livestream_records(session_id, sequence);
                CREATE INDEX IF NOT EXISTS idx_livestream_records_kind_sequence
                    ON livestream_records(kind, sequence);

                CREATE TABLE IF NOT EXISTS livestream_event_dedup (
                    platform TEXT NOT NULL,
                    room_id TEXT NOT NULL,
                    dedup_key TEXT NOT NULL,
                    record_id TEXT NOT NULL,
                    source_sha256 TEXT NOT NULL,
                    PRIMARY KEY(platform, room_id, dedup_key),
                    FOREIGN KEY(record_id) REFERENCES livestream_records(record_id)
                );

                CREATE TABLE IF NOT EXISTS livestream_consumer_cursors (
                    session_id TEXT NOT NULL,
                    consumer TEXT NOT NULL,
                    sequence INTEGER NOT NULL CHECK(sequence >= 0),
                    updated_at REAL NOT NULL,
                    PRIMARY KEY(session_id, consumer)
                );
                """
            )
            cursor = await db.execute(
                "SELECT version FROM livestream_schema WHERE singleton = 1"
            )
            row = await cursor.fetchone()
            version = int(row["version"]) if row else 0
            if version != SCHEMA_VERSION:
                raise RuntimeError(
                    f"unsupported livestream ledger schema {version}; "
                    f"expected {SCHEMA_VERSION}"
                )
            await db.commit()
        except BaseException:
            await db.close()
            raise
        self._db = db

    async def stop(self) -> None:
        """Close the owned database connection idempotently."""

        db = self._db
        if db is not None:
            await db.close()
            if self._db is db:
                self._db = None

    def _require_db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise LedgerNotStartedError("livestream ledger is not started")
        return self._db

    async def append(
        self,
        *,
        record_id: str,
        session_id: str,
        kind: str,
        source: str,
        payload: dict[str, Any],
        occurred_at: float | None = None,
        correlation_id: str | None = None,
        causation_id: str | None = None,
    ) -> AppendResult:
        """Append one record, accepting only content-equivalent replay."""

        for name, value in (
            ("record_id", record_id),
            ("session_id", session_id),
            ("kind", kind),
            ("source", source),
        ):
            if not str(value or "").strip():
                raise ValueError(f"{name} must not be empty")
        payload_json, payload_hash = _canonical_payload(payload)
        occurred = float(occurred_at or time.time())
        db = self._require_db()

        async with self._write_lock:
            try:
                cursor = await db.execute(
                    """
                    INSERT INTO livestream_records(
                        record_id, session_id, kind, occurred_at, source,
                        correlation_id, causation_id, payload_json,
                        payload_sha256, recorded_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record_id,
                        session_id,
                        kind,
                        occurred,
                        source,
                        correlation_id,
                        causation_id,
                        payload_json,
                        payload_hash,
                        time.time(),
                    ),
                )
                await db.commit()
                return AppendResult(sequence=int(cursor.lastrowid), inserted=True)
            except aiosqlite.IntegrityError:
                await db.rollback()
                cursor = await db.execute(
                    """
                    SELECT sequence, session_id, kind, occurred_at, source,
                           correlation_id, causation_id, payload_sha256
                    FROM livestream_records WHERE record_id = ?
                    """,
                    (record_id,),
                )
                row = await cursor.fetchone()
                envelope_conflict = row is not None and (
                    row["session_id"] != session_id
                    or row["kind"] != kind
                    or row["source"] != source
                    or row["correlation_id"] != correlation_id
                    or row["causation_id"] != causation_id
                    or (
                        occurred_at is not None
                        and float(row["occurred_at"]) != float(occurred_at)
                    )
                )
                if (
                    row is None
                    or row["payload_sha256"] != payload_hash
                    or envelope_conflict
                ):
                    raise LedgerRecordConflictError(
                        f"record identity conflict: {record_id}"
                    ) from None
                return AppendResult(sequence=int(row["sequence"]), inserted=False)

    async def append_platform_event(
        self,
        session_id: str,
        event: PlatformEvent,
    ) -> AppendResult:
        """Append a platform event and enforce proven source deduplication."""

        db = self._require_db()
        record_id = (
            f"platform:{event.platform}:{event.room_id}:"
            f"{event.kind}:{event.event_id}"
        )
        payload = event.to_payload()
        payload_json, payload_hash = _canonical_payload(payload)
        source_hash = _platform_source_hash(event)

        async with self._write_lock:
            if event.dedup_key:
                cursor = await db.execute(
                    """
                    SELECT r.sequence, d.source_sha256
                    FROM livestream_event_dedup d
                    JOIN livestream_records r ON r.record_id = d.record_id
                    WHERE d.platform = ? AND d.room_id = ? AND d.dedup_key = ?
                    """,
                    (event.platform, event.room_id, event.dedup_key),
                )
                row = await cursor.fetchone()
                if row is not None:
                    if row["source_sha256"] != source_hash:
                        raise LedgerRecordConflictError(
                            "platform dedup identity mapped to different content: "
                            f"{event.platform}/{event.room_id}/{event.dedup_key}"
                        )
                    return AppendResult(sequence=int(row["sequence"]), inserted=False)

            try:
                await db.execute("BEGIN IMMEDIATE")
                cursor = await db.execute(
                    """
                    INSERT INTO livestream_records(
                        record_id, session_id, kind, occurred_at, source,
                        correlation_id, causation_id, payload_json,
                        payload_sha256, recorded_at
                    ) VALUES (?, ?, 'platform.event', ?, ?, ?, NULL, ?, ?, ?)
                    """,
                    (
                        record_id,
                        session_id,
                        event.timestamp,
                        event.platform,
                        event.event_id,
                        payload_json,
                        payload_hash,
                        time.time(),
                    ),
                )
                if event.dedup_key:
                    await db.execute(
                        """
                        INSERT INTO livestream_event_dedup(
                            platform, room_id, dedup_key, record_id, source_sha256
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            event.platform,
                            event.room_id,
                            event.dedup_key,
                            record_id,
                            source_hash,
                        ),
                    )
                await db.commit()
                return AppendResult(sequence=int(cursor.lastrowid), inserted=True)
            except aiosqlite.IntegrityError:
                await db.rollback()
                cursor = await db.execute(
                    """
                    SELECT sequence, payload_sha256
                    FROM livestream_records WHERE record_id = ?
                    """,
                    (record_id,),
                )
                row = await cursor.fetchone()
                if row is None or row["payload_sha256"] != payload_hash:
                    raise LedgerRecordConflictError(
                        f"platform event identity conflict: {record_id}"
                    ) from None
                return AppendResult(sequence=int(row["sequence"]), inserted=False)

    async def read_since(
        self,
        sequence: int,
        *,
        session_id: str | None = None,
        kinds: Iterable[str] | None = None,
        limit: int = 100,
    ) -> list[LedgerRecord]:
        """Read immutable records in sequence order."""

        if sequence < 0:
            raise ValueError("sequence must be non-negative")
        if limit <= 0 or limit > 1000:
            raise ValueError("limit must be between 1 and 1000")
        db = self._require_db()
        clauses = ["sequence > ?"]
        params: list[Any] = [sequence]
        if session_id is not None:
            clauses.append("session_id = ?")
            params.append(session_id)
        kind_list = list(kinds or [])
        if kind_list:
            placeholders = ",".join("?" for _ in kind_list)
            clauses.append(f"kind IN ({placeholders})")
            params.extend(kind_list)
        params.append(limit)
        cursor = await db.execute(
            f"""
            SELECT sequence, record_id, session_id, kind, occurred_at,
                   source, correlation_id, causation_id, payload_json,
                   payload_sha256, recorded_at
            FROM livestream_records
            WHERE {' AND '.join(clauses)}
            ORDER BY sequence ASC
            LIMIT ?
            """,
            params,
        )
        rows = await cursor.fetchall()
        return [self._row_to_record(row) for row in rows]

    async def read_before(
        self,
        sequence: int | None,
        *,
        kinds: Iterable[str] | None = None,
        limit: int = 100,
    ) -> list[LedgerRecord]:
        """Read immutable records in descending order for keyset history pages."""

        if sequence is not None and sequence <= 0:
            raise ValueError("sequence must be positive")
        if limit <= 0 or limit > 1000:
            raise ValueError("limit must be between 1 and 1000")
        db = self._require_db()
        clauses: list[str] = []
        params: list[Any] = []
        if sequence is not None:
            clauses.append("sequence < ?")
            params.append(sequence)
        kind_list = list(kinds or [])
        if kind_list:
            placeholders = ",".join("?" for _ in kind_list)
            clauses.append(f"kind IN ({placeholders})")
            params.extend(kind_list)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        cursor = await db.execute(
            f"""
            SELECT sequence, record_id, session_id, kind, occurred_at,
                   source, correlation_id, causation_id, payload_json,
                   payload_sha256, recorded_at
            FROM livestream_records
            {where}
            ORDER BY sequence DESC
            LIMIT ?
            """,
            params,
        )
        rows = await cursor.fetchall()
        return [self._row_to_record(row) for row in rows]

    async def get_record(self, record_id: str) -> LedgerRecord | None:
        """Return one immutable record by stable identity."""

        db = self._require_db()
        cursor = await db.execute(
            """
            SELECT sequence, record_id, session_id, kind, occurred_at,
                   source, correlation_id, causation_id, payload_json,
                   payload_sha256, recorded_at
            FROM livestream_records WHERE record_id = ?
            """,
            (record_id,),
        )
        row = await cursor.fetchone()
        return self._row_to_record(row) if row is not None else None

    async def get_latest_record(self, kind: str) -> LedgerRecord | None:
        """Return the latest immutable record of one technical kind."""

        if not kind.strip():
            raise ValueError("kind must not be empty")
        db = self._require_db()
        cursor = await db.execute(
            """
            SELECT sequence, record_id, session_id, kind, occurred_at,
                   source, correlation_id, causation_id, payload_json,
                   payload_sha256, recorded_at
            FROM livestream_records WHERE kind = ?
            ORDER BY sequence DESC LIMIT 1
            """,
            (kind,),
        )
        row = await cursor.fetchone()
        return self._row_to_record(row) if row is not None else None

    async def get_cursor(self, session_id: str, consumer: str) -> int:
        """Read a rebuildable consumer cursor."""

        db = self._require_db()
        cursor = await db.execute(
            """
            SELECT sequence FROM livestream_consumer_cursors
            WHERE session_id = ? AND consumer = ?
            """,
            (session_id, consumer),
        )
        row = await cursor.fetchone()
        return int(row["sequence"]) if row else 0

    async def commit_cursor(
        self,
        session_id: str,
        consumer: str,
        sequence: int,
    ) -> None:
        """Advance a cursor monotonically after durable batch completion."""

        if sequence < 0:
            raise ValueError("sequence must be non-negative")
        db = self._require_db()
        async with self._write_lock:
            current = await self.get_cursor(session_id, consumer)
            if sequence < current:
                raise ValueError(
                    f"cursor rewind refused for {consumer}: {current} -> {sequence}"
                )
            await db.execute(
                """
                INSERT INTO livestream_consumer_cursors(
                    session_id, consumer, sequence, updated_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(session_id, consumer) DO UPDATE SET
                    sequence = excluded.sequence,
                    updated_at = excluded.updated_at
                """,
                (session_id, consumer, sequence, time.time()),
            )
            await db.commit()

    async def count_after(
        self,
        sequence: int,
        *,
        session_id: str,
        kind: str,
    ) -> int:
        """Count backlog rows for read-only health reporting."""

        db = self._require_db()
        cursor = await db.execute(
            """
            SELECT COUNT(*) AS total FROM livestream_records
            WHERE sequence > ? AND session_id = ? AND kind = ?
            """,
            (sequence, session_id, kind),
        )
        row = await cursor.fetchone()
        return int(row["total"]) if row else 0

    @staticmethod
    def _row_to_record(row: aiosqlite.Row) -> LedgerRecord:
        return LedgerRecord(
            sequence=int(row["sequence"]),
            record_id=str(row["record_id"]),
            session_id=str(row["session_id"]),
            kind=str(row["kind"]),
            occurred_at=float(row["occurred_at"]),
            source=str(row["source"]),
            correlation_id=row["correlation_id"],
            causation_id=row["causation_id"],
            payload=json.loads(row["payload_json"]),
            payload_sha256=str(row["payload_sha256"]),
            recorded_at=float(row["recorded_at"]),
        )
