"""Unified event bus primitives for life_engine.

The first version is intentionally compatibility-first: existing
``LifeEngineEvent`` callers continue to work, while every published event is
also mirrored into an append-only raw event log for future high-volume
channels.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import threading
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import Enum, IntEnum
from pathlib import Path
from typing import Any, ClassVar

from src.kernel.sync.local_store import create_local_sync_schema, enqueue_in_transaction

from .event_builder import EventType, LifeEngineEvent

RAW_EVENT_LOG_FILE = "life_events.jsonl"
RAW_EVENT_DB_FILE = "life_events.sqlite3"


class RawEventGapError(RuntimeError):
    """Raised when a requested cursor predates every retained raw event."""

    def __init__(self, requested_sequence: int, earliest_available: int) -> None:
        self.requested_sequence = requested_sequence
        self.earliest_available = earliest_available
        super().__init__(
            "raw event history gap: requested after "
            f"{requested_sequence}, earliest retained is {earliest_available}"
        )


class LifeEventPriority(IntEnum):
    LOW = 10
    NORMAL = 50
    HIGH = 80
    URGENT = 100


class LifeEventChannel(str, Enum):
    CHAT = "chat"
    LIFE = "life"
    TOOL = "tool"
    AGENT = "agent"
    PROACTIVE = "proactive"
    SYSTEM = "system"


@dataclass(slots=True)
class LifeEvent:
    """Channel-agnostic raw event for the unified consciousness pipeline."""

    event_id: str
    sequence: int
    timestamp: str
    source: str
    channel: str
    event_type: str
    content: str
    stream_id: str = ""
    reply_target: dict[str, Any] | None = None
    priority: int = int(LifeEventPriority.NORMAL)
    salience: float = 0.5
    ttl_seconds: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    occurrence_id: str = ""
    source_sequence: int = 0
    recorded_at: str = ""
    source_instance_id: str = ""
    causation_id: str = ""
    correlation_id: str = ""
    content_ref: str = ""


def _legacy_channel(event: LifeEngineEvent) -> LifeEventChannel:
    content_type = str(event.content_type or "").strip().lower()
    if event.event_type == EventType.SUMMARY:
        return LifeEventChannel.SYSTEM
    if event.event_type in {EventType.TOOL_CALL, EventType.TOOL_RESULT}:
        return LifeEventChannel.TOOL
    if event.event_type == EventType.AGENT_RESULT:
        return LifeEventChannel.AGENT
    if content_type == "proactive_opportunity":
        return LifeEventChannel.PROACTIVE
    if content_type.startswith("autonomy_intent_"):
        return LifeEventChannel.LIFE
    if event.event_type == EventType.HEARTBEAT:
        return LifeEventChannel.LIFE
    if str(event.source or "").strip().lower() == "system":
        return LifeEventChannel.SYSTEM
    return LifeEventChannel.CHAT


def _legacy_priority_and_salience(event: LifeEngineEvent) -> tuple[int, float]:
    content_type = str(event.content_type or "").strip().lower()
    if event.event_type == EventType.TOOL_RESULT and event.tool_success is False:
        return int(LifeEventPriority.URGENT), 1.0
    if content_type in {"direct_message", "dfc_message", "inner_dialogue"}:
        return int(LifeEventPriority.HIGH), 0.9
    if content_type == "proactive_opportunity":
        return int(LifeEventPriority.HIGH), 0.82
    if content_type == "autonomy_intent_due":
        return int(LifeEventPriority.HIGH), 0.86
    if content_type.startswith("autonomy_intent_"):
        return int(LifeEventPriority.NORMAL), 0.74
    if event.event_type == EventType.AGENT_RESULT:
        return int(LifeEventPriority.HIGH), 0.8
    if content_type == "chatter_inner_monologue":
        return int(LifeEventPriority.NORMAL), 0.72
    if event.event_type == EventType.MESSAGE:
        return int(LifeEventPriority.NORMAL), 0.6
    if event.event_type == EventType.TOOL_CALL:
        return int(LifeEventPriority.LOW), 0.25
    if event.event_type == EventType.TOOL_RESULT:
        return int(LifeEventPriority.LOW), 0.35
    return int(LifeEventPriority.LOW), 0.3


def life_event_from_legacy(event: LifeEngineEvent) -> LifeEvent:
    """Convert the existing service event to the new raw event model."""

    priority, salience = _legacy_priority_and_salience(event)
    stream_id = str(event.stream_id or "").strip()
    reply_target = None
    if stream_id:
        reply_target = {
            "stream_id": stream_id,
            "source": event.source,
            "chat_type": event.chat_type,
            "sender": event.sender,
            "sender_id": event.sender_id,
            "sender_platform_account_key": event.sender_platform_account_key,
            "canonical_person_key": event.canonical_person_key,
            "identity_resolution_status": event.identity_resolution_status,
        }

    metadata: dict[str, Any] = {
        "legacy_event_type": event.event_type.value,
        "source_detail": event.source_detail,
        "content_type": event.content_type,
    }
    if event.sender is not None:
        metadata["sender"] = event.sender
    for field_name in (
        "sender_id",
        "sender_platform_account_key",
        "canonical_person_key",
        "identity_resolution_status",
    ):
        value = getattr(event, field_name, None)
        if value is not None:
            metadata[field_name] = value
    if event.chat_type is not None:
        metadata["chat_type"] = event.chat_type
    if event.heartbeat_index is not None:
        metadata["heartbeat_index"] = event.heartbeat_index
    for field_name in (
        "heartbeat_run_id",
        "call_id",
        "parent_event_id",
        "causation_id",
    ):
        value = getattr(event, field_name, None)
        if value is not None:
            metadata[field_name] = value
    source_instance_id = str(getattr(event, "source_instance_id", None) or "")
    correlation_id = str(getattr(event, "correlation_id", None) or "")
    content_ref = str(getattr(event, "content_ref", None) or "")
    if source_instance_id:
        metadata["source_instance_id"] = source_instance_id
    if correlation_id:
        metadata["correlation_id"] = correlation_id
    if content_ref:
        metadata["content_ref"] = content_ref
    if event.tool_name is not None:
        metadata["tool_name"] = event.tool_name
    if event.tool_args is not None:
        metadata["tool_args"] = event.tool_args
    if event.tool_success is not None:
        metadata["tool_success"] = event.tool_success
    if event.heartbeat_context_consumed:
        metadata["heartbeat_context_consumed"] = True

    return LifeEvent(
        event_id=event.event_id,
        sequence=int(event.sequence or 0),
        timestamp=event.timestamp,
        source=event.source,
        channel=_legacy_channel(event).value,
        event_type=event.content_type or event.event_type.value,
        content=(getattr(event, "raw_content", None) or event.content or ""),
        stream_id=stream_id,
        reply_target=reply_target,
        priority=priority,
        salience=salience,
        metadata=metadata,
        source_sequence=int(event.sequence or 0),
        source_instance_id=source_instance_id,
        causation_id=str(getattr(event, "causation_id", None) or ""),
        correlation_id=correlation_id,
        content_ref=content_ref,
    )


def life_event_to_dict(event: LifeEvent) -> dict[str, Any]:
    return {
        "event_id": event.event_id,
        "sequence": event.sequence,
        "timestamp": event.timestamp,
        "source": event.source,
        "channel": event.channel,
        "event_type": event.event_type,
        "content": event.content,
        "stream_id": event.stream_id,
        "reply_target": event.reply_target,
        "priority": int(event.priority),
        "salience": float(event.salience),
        "ttl_seconds": event.ttl_seconds,
        "metadata": event.metadata,
        "occurrence_id": event.occurrence_id,
        "source_sequence": int(event.source_sequence or 0),
        "recorded_at": event.recorded_at,
        "source_instance_id": event.source_instance_id,
        "causation_id": event.causation_id,
        "correlation_id": event.correlation_id,
        "content_ref": event.content_ref,
    }


def life_event_from_dict(data: dict[str, Any]) -> LifeEvent:
    metadata = data.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    return LifeEvent(
        event_id=str(data.get("event_id") or ""),
        sequence=int(data.get("sequence") or 0),
        timestamp=str(data.get("timestamp") or ""),
        source=str(data.get("source") or "unknown"),
        channel=str(data.get("channel") or LifeEventChannel.SYSTEM.value),
        event_type=str(data.get("event_type") or "unknown"),
        content=str(data.get("content") or ""),
        stream_id=str(data.get("stream_id") or ""),
        reply_target=data.get("reply_target") if isinstance(data.get("reply_target"), dict) else None,
        priority=int(data.get("priority") or int(LifeEventPriority.NORMAL)),
        salience=float(data.get("salience") or 0.0),
        ttl_seconds=(
            int(data["ttl_seconds"])
            if data.get("ttl_seconds") is not None
            else None
        ),
        metadata=metadata,
        occurrence_id=str(data.get("occurrence_id") or ""),
        source_sequence=int(data.get("source_sequence") or data.get("sequence") or 0),
        recorded_at=str(data.get("recorded_at") or ""),
        source_instance_id=str(
            data.get("source_instance_id")
            or metadata.get("source_instance_id")
            or ""
        ),
        causation_id=str(
            data.get("causation_id")
            or metadata.get("causation_id")
            or ""
        ),
        correlation_id=str(
            data.get("correlation_id")
            or metadata.get("correlation_id")
            or ""
        ),
        content_ref=str(
            data.get("content_ref")
            or metadata.get("content_ref")
            or ""
        ),
    )


class _LegacyJSONLEventStore:
    """Append-only JSONL store for raw life events.

    当文件超过 max_bytes 时自动轮转：当前文件重命名为 .1，
    旧的 .1 重命名为 .2，超出 max_archives 的最旧归档被删除。
    """

    _path_locks_guard: ClassVar[Any] = threading.Lock()
    _path_locks: ClassVar[dict[Path, threading.RLock]] = {}

    def __init__(
        self,
        workspace_path: str | Path,
        filename: str = RAW_EVENT_LOG_FILE,
        max_bytes: int = 50 * 1024 * 1024,
        max_archives: int = 2,
    ) -> None:
        self._path = Path(workspace_path).resolve() / filename
        self._max_bytes = max_bytes
        self._max_archives = max_archives
        with self._path_locks_guard:
            self._path_lock = self._path_locks.setdefault(
                self._path,
                threading.RLock(),
            )

    @property
    def path(self) -> Path:
        return self._path

    def _archive_path(self, index: int) -> Path:
        return self._path.with_name(f"{self._path.name}.{index}")

    def _paths_oldest_first(self) -> list[Path]:
        archives = [
            self._archive_path(index)
            for index in range(self._max_archives, 0, -1)
            if self._archive_path(index).exists()
        ]
        if self._path.exists():
            archives.append(self._path)
        return archives

    def _paths_newest_first(self) -> list[Path]:
        paths = [self._path] if self._path.exists() else []
        paths.extend(
            self._archive_path(index)
            for index in range(1, self._max_archives + 1)
            if self._archive_path(index).exists()
        )
        return paths

    def _maybe_rotate(self) -> None:
        """尺寸轮转：超过阈值时将当前文件归档。"""
        try:
            if not self._path.exists() or self._path.stat().st_size < self._max_bytes:
                return
            if self._max_archives <= 0:
                self._path.unlink()
                return
            # 删除最旧的归档
            oldest = self._archive_path(self._max_archives)
            if oldest.exists():
                oldest.unlink()
            # 依次后移已有归档
            for i in range(self._max_archives - 1, 0, -1):
                src = self._archive_path(i)
                dst = self._archive_path(i + 1)
                if src.exists():
                    src.rename(dst)
            # 当前文件 -> .1
            self._path.rename(self._archive_path(1))
        except OSError:
            pass  # 轮转失败不阻塞写入

    def _append_sync(self, event: LifeEvent) -> None:
        with self._path_lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._maybe_rotate()
            line = json.dumps(life_event_to_dict(event), ensure_ascii=False)
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")

    async def append(self, event: LifeEvent) -> None:
        await asyncio.to_thread(self._append_sync, event)

    def _append_many_sync(self, events: list[LifeEvent]) -> None:
        with self._path_lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._maybe_rotate()
            lines = [
                json.dumps(life_event_to_dict(event), ensure_ascii=False)
                for event in events
            ]
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write("\n".join(lines) + "\n")

    async def append_many(self, events: list[LifeEvent]) -> None:
        if not events:
            return
        await asyncio.to_thread(self._append_many_sync, events)

    def _read_tail_from_path(self, path: Path, limit: int) -> list[LifeEvent]:
        if limit <= 0 or not path.exists():
            return []
        block_size = 64 * 1024
        chunks: list[bytes] = []
        newline_count = 0
        with path.open("rb") as handle:
            handle.seek(0, 2)
            position = handle.tell()
            while position > 0 and newline_count <= limit:
                read_size = min(block_size, position)
                position -= read_size
                handle.seek(position)
                chunk = handle.read(read_size)
                chunks.append(chunk)
                newline_count += chunk.count(b"\n")
        text = b"".join(reversed(chunks)).decode("utf-8", errors="replace")
        return self._decode_lines(text.splitlines()[-limit:])

    def _read_tail_sync(self, limit: int) -> list[LifeEvent]:
        if limit <= 0:
            return []
        with self._path_lock:
            remaining = limit
            chunks: list[list[LifeEvent]] = []
            for path in self._paths_newest_first():
                chunk = self._read_tail_from_path(path, remaining)
                if chunk:
                    chunks.append(chunk)
                    remaining -= len(chunk)
                if remaining <= 0:
                    break
            return [event for chunk in reversed(chunks) for event in chunk]

    async def read_tail(self, limit: int = 100) -> list[LifeEvent]:
        return await asyncio.to_thread(self._read_tail_sync, limit)

    def _read_since_sync(self, sequence: int, limit: int | None) -> list[LifeEvent]:
        with self._path_lock:
            paths = self._paths_oldest_first()
            if not paths:
                return []
            result: list[LifeEvent] = []
            earliest_available: int | None = None
            for path in paths:
                with path.open("r", encoding="utf-8") as handle:
                    for line in handle:
                        events = self._decode_lines([line])
                        if not events:
                            continue
                        event = events[0]
                        if earliest_available is None:
                            earliest_available = event.sequence
                            if sequence > 0 and earliest_available > sequence + 1:
                                raise RawEventGapError(sequence, earliest_available)
                        if event.sequence <= sequence:
                            continue
                        result.append(event)
                        if limit is not None and len(result) >= limit:
                            return result
            return result

    async def read_since(
        self,
        sequence: int,
        *,
        limit: int | None = None,
    ) -> list[LifeEvent]:
        return await asyncio.to_thread(self._read_since_sync, sequence, limit)

    def read_since_sync(
        self,
        sequence: int,
        *,
        limit: int | None = None,
    ) -> list[LifeEvent]:
        """Synchronously read events after one durable ingest position."""

        return self._read_since_sync(sequence, limit)

    @staticmethod
    def _decode_lines(lines: list[str]) -> list[LifeEvent]:
        events: list[LifeEvent] = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(raw, dict):
                events.append(life_event_from_dict(raw))
        return events


class RawEventStore(_LegacyJSONLEventStore):
    """Authoritative append-only ledger with a JSONL compatibility mirror.

    SQLite assigns a durable ingest position in one transaction.  The
    producer's transient sequence is retained as ``source_sequence`` while
    callers consume the durable position through ``LifeEvent.sequence``.
    JSONL rotation can therefore never remove authoritative history.
    """

    def __init__(
        self,
        workspace_path: str | Path,
        filename: str = RAW_EVENT_LOG_FILE,
        max_bytes: int = 50 * 1024 * 1024,
        max_archives: int = 2,
    ) -> None:
        super().__init__(
            workspace_path,
            filename=filename,
            max_bytes=max_bytes,
            max_archives=max_archives,
        )
        self._database_path = self._path.with_name(RAW_EVENT_DB_FILE)
        self._ready = False

    @property
    def database_path(self) -> Path:
        """Return the authoritative ledger path."""

        return self._database_path

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(UTC).astimezone().isoformat()

    def _connect(self) -> sqlite3.Connection:
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(
            self._database_path,
            timeout=30.0,
            isolation_level=None,
        )
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode = WAL")
        db.execute("PRAGMA synchronous = FULL")
        db.execute("PRAGMA busy_timeout = 30000")
        db.execute("PRAGMA foreign_keys = ON")
        return db

    @staticmethod
    def _create_schema(db: sqlite3.Connection) -> None:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS raw_life_events (
                ingest_position INTEGER PRIMARY KEY AUTOINCREMENT,
                occurrence_id TEXT NOT NULL UNIQUE,
                source_event_id TEXT NOT NULL,
                source_sequence INTEGER NOT NULL DEFAULT 0,
                occurred_at TEXT NOT NULL,
                recorded_at TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                payload_hash TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_raw_life_events_source
                ON raw_life_events(source_event_id, occurred_at, ingest_position);
            CREATE TABLE IF NOT EXISTS raw_event_consumer_offsets (
                consumer_id TEXT PRIMARY KEY,
                ingest_position INTEGER NOT NULL,
                updated_at TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS raw_event_store_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS raw_event_import_issues (
                issue_id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_path TEXT NOT NULL,
                line_number INTEGER NOT NULL DEFAULT 0,
                error_type TEXT NOT NULL,
                detail TEXT NOT NULL DEFAULT '',
                recorded_at TEXT NOT NULL
            );
            CREATE TRIGGER IF NOT EXISTS raw_life_events_immutable_update
            BEFORE UPDATE ON raw_life_events BEGIN
                SELECT RAISE(ABORT, 'RawLifeEventImmutable');
            END;
            CREATE TRIGGER IF NOT EXISTS raw_life_events_immutable_delete
            BEFORE DELETE ON raw_life_events BEGIN
                SELECT RAISE(ABORT, 'RawLifeEventImmutable');
            END;
            """
        )
        create_local_sync_schema(db)

    @staticmethod
    def _canonical_payload(event: LifeEvent) -> dict[str, Any]:
        source_sequence = int(event.source_sequence or event.sequence or 0)
        return life_event_to_dict(
            replace(
                event,
                sequence=source_sequence,
                source_sequence=source_sequence,
                recorded_at="",
            )
        )

    @classmethod
    def _default_occurrence_id(cls, event: LifeEvent) -> str:
        payload = cls._canonical_payload(replace(event, occurrence_id=""))
        payload.pop("occurrence_id", None)
        material = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return "occ_" + hashlib.sha256(material.encode("utf-8")).hexdigest()

    @staticmethod
    def _payload_hash(payload_json: str) -> str:
        return hashlib.sha256(payload_json.encode("utf-8")).hexdigest()

    def _record_issue(
        self,
        db: sqlite3.Connection,
        *,
        source_path: str,
        line_number: int,
        error_type: str,
        detail: str,
    ) -> None:
        db.execute(
            """INSERT INTO raw_event_import_issues
            (source_path, line_number, error_type, detail, recorded_at)
            VALUES (?, ?, ?, ?, ?)""",
            (
                source_path,
                int(line_number),
                error_type,
                detail[:2000],
                self._now_iso(),
            ),
        )

    def _insert_event(
        self,
        db: sqlite3.Connection,
        event: LifeEvent,
    ) -> tuple[LifeEvent, bool]:
        source_sequence = int(event.source_sequence or event.sequence or 0)
        occurrence_id = str(event.occurrence_id or "").strip()
        if not occurrence_id:
            occurrence_id = self._default_occurrence_id(event)
        recorded_at = event.recorded_at or self._now_iso()
        normalized = replace(
            event,
            occurrence_id=occurrence_id,
            source_sequence=source_sequence,
            sequence=source_sequence,
            recorded_at=recorded_at,
        )
        payload = self._canonical_payload(normalized)
        payload["occurrence_id"] = occurrence_id
        payload_json = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        payload_hash = self._payload_hash(payload_json)
        cursor = db.execute(
            """INSERT OR IGNORE INTO raw_life_events (
                occurrence_id, source_event_id, source_sequence, occurred_at,
                recorded_at, payload_json, payload_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                occurrence_id,
                normalized.event_id,
                source_sequence,
                normalized.timestamp,
                recorded_at,
                payload_json,
                payload_hash,
            ),
        )
        inserted = cursor.rowcount > 0
        row = db.execute(
            """SELECT ingest_position, payload_hash, recorded_at FROM raw_life_events
            WHERE occurrence_id = ?""",
            (occurrence_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError(f"RawEventInsertLost:{occurrence_id}")
        if str(row["payload_hash"]) != payload_hash:
            raise ValueError(f"RawEventOccurrenceConflict:{occurrence_id}")
        if inserted:
            metadata = normalized.metadata if isinstance(normalized.metadata, dict) else {}
            enqueue_in_transaction(
                db,
                event_id=occurrence_id,
                occurred_at=normalized.timestamp,
                recorded_at=str(row["recorded_at"]),
                event_type=normalized.event_type,
                payload=payload,
                actor_id=str(
                    metadata.get("actor_id")
                    or metadata.get("user_id")
                    or normalized.source
                    or ""
                ),
                consciousness_instance_id=str(
                    normalized.source_instance_id
                    or metadata.get("consciousness_instance_id")
                    or ""
                ),
                visibility=str(metadata.get("visibility") or "private"),
                causation_id=normalized.causation_id,
                correlation_id=normalized.correlation_id,
                export_requested=metadata.get("sync_export") is True,
            )
        return replace(
            normalized,
            sequence=int(row["ingest_position"]),
            recorded_at=str(row["recorded_at"]),
        ), inserted

    def _import_legacy_jsonl(self, db: sqlite3.Connection) -> None:
        db.execute("BEGIN IMMEDIATE")
        try:
            migrated = db.execute(
                """SELECT 1 FROM raw_event_store_meta
                WHERE key = 'legacy_jsonl_imported'"""
            ).fetchone()
            if migrated is not None:
                db.commit()
                return
            for path in self._paths_oldest_first():
                try:
                    handle = path.open("r", encoding="utf-8")
                except OSError as exc:
                    self._record_issue(
                        db,
                        source_path=str(path),
                        line_number=0,
                        error_type=type(exc).__name__,
                        detail=str(exc),
                    )
                    continue
                with handle:
                    for line_number, line in enumerate(handle, start=1):
                        stripped = line.strip()
                        if not stripped:
                            continue
                        try:
                            raw = json.loads(stripped)
                            if not isinstance(raw, dict):
                                raise TypeError("raw event row is not an object")
                            self._insert_event(db, life_event_from_dict(raw))
                        except (json.JSONDecodeError, TypeError, ValueError) as exc:
                            self._record_issue(
                                db,
                                source_path=str(path),
                                line_number=line_number,
                                error_type=type(exc).__name__,
                                detail=str(exc),
                            )
            db.execute(
                """INSERT INTO raw_event_store_meta (key, value, updated_at)
                VALUES ('legacy_jsonl_imported', '1', ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value, updated_at = excluded.updated_at""",
                (self._now_iso(),),
            )
            db.commit()
        except BaseException:
            db.rollback()
            raise

    def _ensure_ready_sync(self) -> None:
        if self._ready:
            return
        with self._path_lock:
            if self._ready:
                return
            with self._connect() as db:
                self._create_schema(db)
                self._import_legacy_jsonl(db)
            self._ready = True

    def _maybe_rotate(self) -> None:
        """Rotate only the mirror and record failures without losing events."""

        try:
            if not self._path.exists() or self._path.stat().st_size < self._max_bytes:
                return
            if self._max_archives <= 0:
                return
            oldest = self._archive_path(self._max_archives)
            if oldest.exists():
                oldest.unlink()
            for index in range(self._max_archives - 1, 0, -1):
                source = self._archive_path(index)
                target = self._archive_path(index + 1)
                if source.exists():
                    source.rename(target)
            self._path.rename(self._archive_path(1))
        except OSError as exc:
            with self._connect() as db:
                self._record_issue(
                    db,
                    source_path=str(self._path),
                    line_number=0,
                    error_type=type(exc).__name__,
                    detail=f"mirror rotation failed: {exc}",
                )

    def _append_mirror(self, events: list[LifeEvent]) -> None:
        if not events:
            return
        with self._path_lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._maybe_rotate()
            lines = [
                json.dumps(life_event_to_dict(event), ensure_ascii=False)
                for event in events
            ]
            try:
                with self._path.open("a", encoding="utf-8") as handle:
                    handle.write("\n".join(lines) + "\n")
            except OSError as exc:
                with self._connect() as db:
                    self._record_issue(
                        db,
                        source_path=str(self._path),
                        line_number=0,
                        error_type=type(exc).__name__,
                        detail=f"mirror append failed: {exc}",
                    )

    def _append_many_sync(self, events: list[LifeEvent]) -> list[LifeEvent]:
        if not events:
            return []
        self._ensure_ready_sync()
        persisted: list[LifeEvent] = []
        mirrored: list[LifeEvent] = []
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                for event in events:
                    stored, inserted = self._insert_event(db, event)
                    persisted.append(stored)
                    if inserted:
                        mirrored.append(stored)
                db.commit()
            except BaseException:
                db.rollback()
                raise
        self._append_mirror(mirrored)
        return persisted

    def _append_sync(self, event: LifeEvent) -> LifeEvent:
        return self._append_many_sync([event])[0]

    async def append(self, event: LifeEvent) -> LifeEvent:
        return await asyncio.to_thread(self._append_sync, event)

    async def append_many(self, events: list[LifeEvent]) -> list[LifeEvent]:
        if not events:
            return []
        return await asyncio.to_thread(self._append_many_sync, events)

    @staticmethod
    def _event_from_row(row: sqlite3.Row) -> LifeEvent:
        raw = json.loads(str(row["payload_json"]))
        event = life_event_from_dict(raw)
        return replace(
            event,
            sequence=int(row["ingest_position"]),
            source_sequence=int(row["source_sequence"]),
            occurrence_id=str(row["occurrence_id"]),
            recorded_at=str(row["recorded_at"]),
        )

    def append_sync(self, event: LifeEvent) -> LifeEvent:
        """Synchronously append one event for transactional outbox bridges."""

        return self._append_sync(event)

    def _read_tail_sync(self, limit: int) -> list[LifeEvent]:
        if limit <= 0:
            return []
        self._ensure_ready_sync()
        with self._connect() as db:
            rows = db.execute(
                """SELECT * FROM raw_life_events
                ORDER BY ingest_position DESC LIMIT ?""",
                (int(limit),),
            ).fetchall()
        return [self._event_from_row(row) for row in reversed(rows)]

    async def read_tail(self, limit: int = 100) -> list[LifeEvent]:
        return await asyncio.to_thread(self._read_tail_sync, limit)

    def _read_since_sync(self, sequence: int, limit: int | None) -> list[LifeEvent]:
        self._ensure_ready_sync()
        sql = """SELECT * FROM raw_life_events
        WHERE ingest_position > ? ORDER BY ingest_position"""
        params: list[Any] = [int(sequence)]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(max(0, int(limit)))
        with self._connect() as db:
            bounds = db.execute(
                """SELECT MIN(ingest_position) AS earliest,
                MAX(ingest_position) AS latest FROM raw_life_events"""
            ).fetchone()
            earliest = int(bounds["earliest"] or 0) if bounds is not None else 0
            if sequence > 0 and earliest > sequence + 1:
                raise RawEventGapError(sequence, earliest)
            rows = db.execute(sql, params).fetchall()
        return [self._event_from_row(row) for row in rows]

    async def read_since(
        self,
        sequence: int,
        *,
        limit: int | None = None,
    ) -> list[LifeEvent]:
        return await asyncio.to_thread(self._read_since_sync, sequence, limit)

    def read_since_sync(
        self,
        sequence: int,
        *,
        limit: int | None = None,
    ) -> list[LifeEvent]:
        """Synchronously read events after one durable ingest position."""

        return self._read_since_sync(sequence, limit)

    def _get_consumer_offset_sync(self, consumer_id: str) -> int:
        self._ensure_ready_sync()
        with self._connect() as db:
            row = db.execute(
                """SELECT ingest_position FROM raw_event_consumer_offsets
                WHERE consumer_id = ?""",
                (str(consumer_id),),
            ).fetchone()
        return int(row["ingest_position"]) if row is not None else 0

    async def get_consumer_offset(self, consumer_id: str) -> int:
        """Return one consumer's durable ingest cursor."""

        return await asyncio.to_thread(self._get_consumer_offset_sync, consumer_id)

    def _commit_consumer_offset_sync(
        self,
        consumer_id: str,
        ingest_position: int,
        metadata: dict[str, Any] | None,
    ) -> int:
        self._ensure_ready_sync()
        requested = max(0, int(ingest_position))
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                current_row = db.execute(
                    """SELECT ingest_position FROM raw_event_consumer_offsets
                    WHERE consumer_id = ?""",
                    (str(consumer_id),),
                ).fetchone()
                current = int(current_row["ingest_position"]) if current_row else 0
                committed = max(current, requested)
                db.execute(
                    """INSERT INTO raw_event_consumer_offsets (
                        consumer_id, ingest_position, updated_at, metadata_json
                    ) VALUES (?, ?, ?, ?)
                    ON CONFLICT(consumer_id) DO UPDATE SET
                        ingest_position = excluded.ingest_position,
                        updated_at = excluded.updated_at,
                        metadata_json = excluded.metadata_json""",
                    (
                        str(consumer_id),
                        committed,
                        self._now_iso(),
                        json.dumps(metadata or {}, ensure_ascii=False),
                    ),
                )
                db.commit()
            except BaseException:
                db.rollback()
                raise
        return committed

    async def commit_consumer_offset(
        self,
        consumer_id: str,
        ingest_position: int,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> int:
        """Atomically advance a consumer cursor without allowing regression."""

        return await asyncio.to_thread(
            self._commit_consumer_offset_sync,
            consumer_id,
            ingest_position,
            metadata,
        )

    def _health_sync(self) -> dict[str, Any]:
        self._ensure_ready_sync()
        with self._connect() as db:
            row = db.execute(
                """SELECT COUNT(*) AS total,
                MIN(ingest_position) AS earliest,
                MAX(ingest_position) AS latest FROM raw_life_events"""
            ).fetchone()
            issue_row = db.execute(
                "SELECT COUNT(*) AS total FROM raw_event_import_issues"
            ).fetchone()
            consumers = db.execute(
                """SELECT consumer_id, ingest_position, updated_at
                FROM raw_event_consumer_offsets ORDER BY consumer_id"""
            ).fetchall()
        latest = int(row["latest"] or 0) if row is not None else 0
        return {
            "database_path": str(self._database_path),
            "total": int(row["total"] or 0) if row is not None else 0,
            "earliest_position": int(row["earliest"] or 0) if row is not None else 0,
            "latest_position": latest,
            "import_issue_count": (
                int(issue_row["total"] or 0) if issue_row is not None else 0
            ),
            "consumers": [
                {
                    "consumer_id": str(item["consumer_id"]),
                    "position": int(item["ingest_position"]),
                    "lag": max(0, latest - int(item["ingest_position"])),
                    "updated_at": str(item["updated_at"]),
                }
                for item in consumers
            ],
        }

    async def health(self) -> dict[str, Any]:
        """Return ledger bounds, migration issues, and consumer lag."""

        return await asyncio.to_thread(self._health_sync)

    def health_snapshot(self) -> dict[str, Any]:
        """Synchronous lightweight snapshot for existing service health APIs."""

        return self._health_sync()


class LifeEventBus:
    """Compatibility event bus that mirrors legacy events to raw storage."""

    def __init__(self, store: RawEventStore) -> None:
        self._store = store
        self._lock = asyncio.Lock()

    @property
    def store(self) -> RawEventStore:
        return self._store

    async def publish(self, event: LifeEvent) -> LifeEvent:
        async with self._lock:
            return await self._store.append(event)

    async def publish_many(self, events: list[LifeEvent]) -> list[LifeEvent]:
        if not events:
            return []
        async with self._lock:
            return await self._store.append_many(events)

    async def publish_legacy_event(self, event: LifeEngineEvent) -> LifeEvent:
        return await self.publish(life_event_from_legacy(event))

    async def publish_legacy_events(self, events: list[LifeEngineEvent]) -> list[LifeEvent]:
        return await self.publish_many([life_event_from_legacy(event) for event in events])
