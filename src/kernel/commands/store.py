"""SQLite command ledger with transactional state events in the shared Outbox."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from src.kernel.storage.outbox_primitives import canonical_json, canonical_json_sha256
from src.kernel.sync.local_store import create_local_sync_schema, enqueue_in_transaction

from .models import (
    TERMINAL_STATUSES,
    CommandNotCancellable,
    CommandNotFound,
    CommandRecord,
    CommandStatus,
    IdempotencyConflict,
)


class CommandStore:
    """Own one durable command ledger and its append-only technical events."""

    _initialize_lock = threading.Lock()

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path).resolve()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            str(self.database_path),
            check_same_thread=False,
            timeout=30.0,
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA busy_timeout = 30000")
        with self._initialize_lock:
            self._connection.execute("PRAGMA journal_mode = WAL")
            self._connection.execute("PRAGMA synchronous = FULL")
            self._create_schema()

    def close(self) -> None:
        """Close the owned SQLite connection."""

        with self._lock:
            self._connection.close()

    def _create_schema(self) -> None:
        with self._lock, self._connection:
            create_local_sync_schema(self._connection)
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS api_commands (
                    command_id TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    command_type TEXT NOT NULL,
                    schema_version INTEGER NOT NULL,
                    actor_id TEXT NOT NULL,
                    caller_role TEXT NOT NULL,
                    scope_snapshot_json TEXT NOT NULL,
                    target_json TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    accepted_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    result_event_id TEXT,
                    result_json TEXT,
                    error_code TEXT,
                    safe_error_detail TEXT,
                    correlation_id TEXT,
                    causation_id TEXT,
                    expected_revision INTEGER,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    cancellation_requested INTEGER NOT NULL DEFAULT 0,
                    task_id TEXT,
                    UNIQUE(actor_id, idempotency_key)
                );
                CREATE INDEX IF NOT EXISTS idx_api_commands_actor_created
                    ON api_commands(actor_id, created_at DESC, command_id DESC);
                CREATE INDEX IF NOT EXISTS idx_api_commands_status_created
                    ON api_commands(status, created_at DESC, command_id DESC);
                CREATE TABLE IF NOT EXISTS api_command_transitions (
                    transition_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    command_id TEXT NOT NULL,
                    from_status TEXT,
                    to_status TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    event_id TEXT NOT NULL UNIQUE,
                    error_code TEXT,
                    safe_error_detail TEXT,
                    FOREIGN KEY(command_id) REFERENCES api_commands(command_id)
                );
                CREATE INDEX IF NOT EXISTS idx_api_command_transitions_command
                    ON api_command_transitions(command_id, transition_id);
                """
            )
            command_columns = {
                str(row["name"])
                for row in self._connection.execute("PRAGMA table_info(api_commands)")
            }
            if "expected_revision" not in command_columns:
                self._connection.execute(
                    "ALTER TABLE api_commands ADD COLUMN expected_revision INTEGER"
                )

    @staticmethod
    def request_hash(
        *,
        command_type: str,
        schema_version: int,
        target: dict[str, Any],
        payload: dict[str, Any],
        correlation_id: str | None,
        expected_revision: int | None,
    ) -> str:
        """Hash all semantic command request fields deterministically."""

        return canonical_json_sha256(
            {
                "command_type": command_type,
                "schema_version": schema_version,
                "target": target,
                "payload": payload,
                "correlation_id": correlation_id,
                "expected_revision": expected_revision,
            }
        )

    def accept(
        self,
        *,
        idempotency_key: str,
        request_hash: str,
        command_type: str,
        schema_version: int,
        actor_id: str,
        caller_role: str,
        scopes: tuple[str, ...],
        target: dict[str, Any],
        payload: dict[str, Any],
        correlation_id: str | None = None,
        causation_id: str | None = None,
        expected_revision: int | None = None,
    ) -> tuple[CommandRecord, bool]:
        """Durably accept a command or return its idempotent predecessor."""

        now = datetime.now(UTC)
        command_id = f"cmd_{uuid4().hex}"
        with self._lock, self._transaction() as db:
            existing = db.execute(
                "SELECT * FROM api_commands WHERE actor_id = ? AND idempotency_key = ?",
                (actor_id, idempotency_key),
            ).fetchone()
            if existing is not None:
                if str(existing["request_hash"]) != request_hash:
                    raise IdempotencyConflict(idempotency_key)
                return self._record(existing), False
            db.execute(
                """
                INSERT INTO api_commands (
                    command_id, idempotency_key, request_hash, command_type,
                    schema_version, actor_id, caller_role, scope_snapshot_json,
                    target_json, payload_json, status, created_at, accepted_at,
                    correlation_id, causation_id, expected_revision
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'accepted', ?, ?, ?, ?, ?)
                """,
                (
                    command_id,
                    idempotency_key,
                    request_hash,
                    command_type,
                    schema_version,
                    actor_id,
                    caller_role,
                    canonical_json(sorted(set(scopes))),
                    canonical_json(target),
                    canonical_json(payload),
                    now.isoformat(),
                    now.isoformat(),
                    correlation_id,
                    causation_id,
                    expected_revision,
                ),
            )
            self._append_transition(
                db,
                command_id=command_id,
                actor_id=actor_id,
                from_status=None,
                to_status=CommandStatus.ACCEPTED,
                occurred_at=now,
                correlation_id=correlation_id,
            )
            row = self._get_row(db, command_id)
        return self._record(row), True

    def get(self, command_id: str) -> CommandRecord:
        with self._lock:
            return self._record(self._get_row(self._connection, command_id))

    def list(
        self,
        *,
        actor_id: str | None,
        status: CommandStatus | None = None,
        command_type: str | None = None,
        limit: int = 50,
    ) -> tuple[CommandRecord, ...]:
        clauses: list[str] = []
        values: list[Any] = []
        if actor_id is not None:
            clauses.append("actor_id = ?")
            values.append(actor_id)
        if status is not None:
            clauses.append("status = ?")
            values.append(status.value)
        if command_type:
            clauses.append("command_type = ?")
            values.append(command_type)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        values.append(max(1, min(200, int(limit))))
        with self._lock:
            rows = self._connection.execute(
                f"SELECT * FROM api_commands{where} ORDER BY created_at DESC, command_id DESC LIMIT ?",
                values,
            ).fetchall()
        return tuple(self._record(row) for row in rows)

    def claim(self, command_id: str) -> CommandRecord | None:
        """Atomically move an accepted command to executing."""

        now = datetime.now(UTC)
        with self._lock, self._transaction() as db:
            row = self._get_row(db, command_id)
            if CommandStatus(row["status"]) is not CommandStatus.ACCEPTED:
                return None
            cursor = db.execute(
                """
                UPDATE api_commands
                SET status = 'executing', started_at = ?, attempt_count = attempt_count + 1
                WHERE command_id = ? AND status = 'accepted'
                """,
                (now.isoformat(), command_id),
            )
            if cursor.rowcount != 1:
                return None
            self._append_transition(
                db,
                command_id=command_id,
                actor_id=str(row["actor_id"]),
                from_status=CommandStatus.ACCEPTED,
                to_status=CommandStatus.EXECUTING,
                occurred_at=now,
                correlation_id=row["correlation_id"],
            )
            return self._record(self._get_row(db, command_id))

    def finish(
        self,
        command_id: str,
        *,
        status: CommandStatus,
        result: dict[str, Any] | None = None,
        error_code: str | None = None,
        safe_error_detail: str | None = None,
    ) -> CommandRecord:
        """Record an explicit terminal handler outcome exactly once."""

        if status not in TERMINAL_STATUSES:
            raise ValueError("finish requires a terminal status")
        now = datetime.now(UTC)
        result_event_id = f"command.{command_id}.{status.value}.{uuid4().hex}"
        with self._lock, self._transaction() as db:
            row = self._get_row(db, command_id)
            current = CommandStatus(row["status"])
            if current in TERMINAL_STATUSES:
                return self._record(row)
            if current is not CommandStatus.EXECUTING:
                raise ValueError(f"invalid command transition: {current}->{status}")
            db.execute(
                """
                UPDATE api_commands
                SET status = ?, finished_at = ?, result_event_id = ?,
                    result_json = ?, error_code = ?, safe_error_detail = ?
                WHERE command_id = ? AND status = 'executing'
                """,
                (
                    status.value,
                    now.isoformat(),
                    result_event_id,
                    canonical_json(result) if result is not None else None,
                    error_code,
                    safe_error_detail,
                    command_id,
                ),
            )
            self._append_transition(
                db,
                command_id=command_id,
                actor_id=str(row["actor_id"]),
                from_status=current,
                to_status=status,
                occurred_at=now,
                correlation_id=row["correlation_id"],
                event_id=result_event_id,
                error_code=error_code,
                safe_error_detail=safe_error_detail,
            )
            return self._record(self._get_row(db, command_id))

    def reject_unhandled(self, command_id: str) -> CommandRecord:
        """Reject an accepted command whose type has no public handler."""

        return self._finish_from_accepted(
            command_id,
            status=CommandStatus.REJECTED,
            error_code="command_type_unavailable",
            safe_error_detail="命令类型未开放或当前不可用。",
        )

    def cancel_before_start(self, command_id: str) -> CommandRecord:
        """Cancel only while durable state is still accepted."""

        return self._finish_from_accepted(
            command_id,
            status=CommandStatus.CANCELLED,
        )

    def request_running_cancellation(self, command_id: str) -> CommandRecord:
        with self._lock, self._connection:
            row = self._get_row(self._connection, command_id)
            if CommandStatus(row["status"]) is not CommandStatus.EXECUTING:
                raise CommandNotCancellable(command_id)
            self._connection.execute(
                "UPDATE api_commands SET cancellation_requested = 1 WHERE command_id = ?",
                (command_id,),
            )
            return self._record(self._get_row(self._connection, command_id))

    def mark_cancelled(self, command_id: str) -> CommandRecord:
        """Persist cancellation after the managed task confirms propagation."""

        return self.finish(command_id, status=CommandStatus.CANCELLED)

    def bind_task(self, command_id: str, task_id: str) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "UPDATE api_commands SET task_id = ? WHERE command_id = ? AND status = 'executing'",
                (task_id, command_id),
            )

    def recover(self) -> tuple[str, ...]:
        """Return accepted commands and fence uncertain pre-restart executions."""

        now = datetime.now(UTC)
        with self._lock, self._transaction() as db:
            executing = db.execute(
                "SELECT * FROM api_commands WHERE status = 'executing'"
            ).fetchall()
            for row in executing:
                event_id = f"command.{row['command_id']}.delivery_unknown.{uuid4().hex}"
                db.execute(
                    """
                    UPDATE api_commands SET status = 'delivery_unknown', finished_at = ?,
                        result_event_id = ?, error_code = 'process_restarted',
                        safe_error_detail = '执行期间进程重启，无法确认外部副作用结果。',
                        task_id = NULL
                    WHERE command_id = ? AND status = 'executing'
                    """,
                    (now.isoformat(), event_id, row["command_id"]),
                )
                self._append_transition(
                    db,
                    command_id=str(row["command_id"]),
                    actor_id=str(row["actor_id"]),
                    from_status=CommandStatus.EXECUTING,
                    to_status=CommandStatus.DELIVERY_UNKNOWN,
                    occurred_at=now,
                    correlation_id=row["correlation_id"],
                    event_id=event_id,
                    error_code="process_restarted",
                    safe_error_detail="执行期间进程重启，无法确认外部副作用结果。",
                )
            accepted = db.execute(
                "SELECT command_id FROM api_commands WHERE status = 'accepted' ORDER BY accepted_at"
            ).fetchall()
        return tuple(str(row["command_id"]) for row in accepted)

    def _finish_from_accepted(
        self,
        command_id: str,
        *,
        status: CommandStatus,
        error_code: str | None = None,
        safe_error_detail: str | None = None,
    ) -> CommandRecord:
        now = datetime.now(UTC)
        event_id = f"command.{command_id}.{status.value}.{uuid4().hex}"
        with self._lock, self._transaction() as db:
            row = self._get_row(db, command_id)
            current = CommandStatus(row["status"])
            if current in TERMINAL_STATUSES:
                return self._record(row)
            if current is not CommandStatus.ACCEPTED:
                raise CommandNotCancellable(command_id)
            db.execute(
                """
                UPDATE api_commands SET status = ?, finished_at = ?, result_event_id = ?,
                    error_code = ?, safe_error_detail = ?
                WHERE command_id = ? AND status = 'accepted'
                """,
                (status.value, now.isoformat(), event_id, error_code, safe_error_detail, command_id),
            )
            self._append_transition(
                db,
                command_id=command_id,
                actor_id=str(row["actor_id"]),
                from_status=current,
                to_status=status,
                occurred_at=now,
                correlation_id=row["correlation_id"],
                event_id=event_id,
                error_code=error_code,
                safe_error_detail=safe_error_detail,
            )
            return self._record(self._get_row(db, command_id))

    def _append_transition(
        self,
        db: sqlite3.Connection,
        *,
        command_id: str,
        actor_id: str,
        from_status: CommandStatus | None,
        to_status: CommandStatus,
        occurred_at: datetime,
        correlation_id: str | None,
        event_id: str | None = None,
        error_code: str | None = None,
        safe_error_detail: str | None = None,
    ) -> str:
        event_id = event_id or f"command.{command_id}.{to_status.value}.{uuid4().hex}"
        db.execute(
            """
            INSERT INTO api_command_transitions (
                command_id, from_status, to_status, occurred_at, event_id,
                error_code, safe_error_detail
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                command_id,
                from_status.value if from_status else None,
                to_status.value,
                occurred_at.isoformat(),
                event_id,
                error_code,
                safe_error_detail,
            ),
        )
        enqueue_in_transaction(
            db,
            event_id=event_id,
            occurred_at=occurred_at.isoformat(),
            recorded_at=occurred_at.isoformat(),
            event_type=f"command.{to_status.value}",
            actor_id=actor_id,
            visibility="private",
            causation_id=command_id,
            correlation_id=correlation_id or "",
            payload={
                "command_id": command_id,
                "from_status": from_status.value if from_status else None,
                "to_status": to_status.value,
                "error_code": error_code,
                "safe_error_detail": safe_error_detail,
            },
            export_requested=True,
        )
        return event_id

    @staticmethod
    def _get_row(db: sqlite3.Connection, command_id: str) -> sqlite3.Row:
        row = db.execute(
            "SELECT * FROM api_commands WHERE command_id = ?",
            (command_id,),
        ).fetchone()
        if row is None:
            raise CommandNotFound(command_id)
        return row

    @staticmethod
    def _record(row: sqlite3.Row) -> CommandRecord:
        return CommandRecord(
            command_id=str(row["command_id"]),
            idempotency_key=str(row["idempotency_key"]),
            request_hash=str(row["request_hash"]),
            command_type=str(row["command_type"]),
            schema_version=int(row["schema_version"]),
            actor_id=str(row["actor_id"]),
            caller_role=str(row["caller_role"]),
            scope_snapshot=tuple(json.loads(row["scope_snapshot_json"])),
            target=dict(json.loads(row["target_json"])),
            payload=dict(json.loads(row["payload_json"])),
            status=CommandStatus(row["status"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            accepted_at=datetime.fromisoformat(row["accepted_at"]),
            started_at=(
                datetime.fromisoformat(row["started_at"]) if row["started_at"] else None
            ),
            finished_at=(
                datetime.fromisoformat(row["finished_at"]) if row["finished_at"] else None
            ),
            result_event_id=row["result_event_id"],
            result=json.loads(row["result_json"]) if row["result_json"] else None,
            error_code=row["error_code"],
            safe_error_detail=row["safe_error_detail"],
            correlation_id=row["correlation_id"],
            causation_id=row["causation_id"],
            expected_revision=row["expected_revision"],
            attempt_count=int(row["attempt_count"]),
            cancellation_requested=bool(row["cancellation_requested"]),
            task_id=row["task_id"],
        )

    class _Transaction:
        def __init__(self, db: sqlite3.Connection) -> None:
            self.db = db

        def __enter__(self) -> sqlite3.Connection:
            self.db.execute("BEGIN IMMEDIATE")
            return self.db

        def __exit__(self, exc_type, exc, traceback) -> bool:
            if exc_type is None:
                self.db.commit()
            else:
                self.db.rollback()
            return False

    def _transaction(self) -> CommandStore._Transaction:
        return self._Transaction(self._connection)


__all__ = ["CommandStore"]
