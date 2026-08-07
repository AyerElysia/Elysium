"""P3-11 管理设置、审计、集成事件和受管日志 store。"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from src.kernel.storage.outbox_primitives import canonical_json

_SETTING_SPECS: dict[str, dict[str, Any]] = {
    "api.max_concurrency": {
        "type": "integer",
        "minimum": 1,
        "maximum": 512,
        "default": 32,
        "restart_required": False,
    },
    "api.max_websocket_connections": {
        "type": "integer",
        "minimum": 1,
        "maximum": 4096,
        "default": 64,
        "restart_required": False,
    },
    "media.max_upload_bytes": {
        "type": "integer",
        "minimum": 1048576,
        "maximum": 134217728,
        "default": 33554432,
        "restart_required": False,
    },
    "admin.log_retention_days": {
        "type": "integer",
        "minimum": 1,
        "maximum": 90,
        "default": 14,
        "restart_required": False,
    },
    "admin.integration_test_timeout_seconds": {
        "type": "integer",
        "minimum": 1,
        "maximum": 30,
        "default": 5,
        "restart_required": False,
    },
}


class AdminStore:
    """拥有 P3-11 技术管理数据；不保存 secret、token、路径或私人原文。"""

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path).resolve()
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            str(self.database_path), check_same_thread=False, timeout=30.0
        )
        self._connection.row_factory = sqlite3.Row
        self._create_schema()

    def close(self) -> None:
        """关闭本 store 拥有的连接。"""

        with self._lock:
            self._connection.close()

    def _create_schema(self) -> None:
        with self._lock, self._connection:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS api_admin_settings (
                    setting_key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    updated_at TEXT NOT NULL,
                    updated_by TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS api_admin_state (
                    state_key TEXT PRIMARY KEY,
                    revision INTEGER NOT NULL
                );
                INSERT OR IGNORE INTO api_admin_state (state_key, revision)
                VALUES ('settings', 0);
                CREATE TABLE IF NOT EXISTS api_admin_audit_events (
                    audit_id TEXT PRIMARY KEY,
                    event_type TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    target_type TEXT NOT NULL,
                    target_id TEXT,
                    outcome TEXT NOT NULL,
                    safe_detail TEXT,
                    request_id TEXT,
                    occurred_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_admin_audit_occurred
                    ON api_admin_audit_events(occurred_at DESC, audit_id DESC);
                CREATE TABLE IF NOT EXISTS api_admin_integration_events (
                    event_id TEXT PRIMARY KEY,
                    integration_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    state TEXT NOT NULL,
                    safe_detail TEXT,
                    occurred_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_admin_integration_events
                    ON api_admin_integration_events(
                        integration_id, occurred_at DESC
                    );
                CREATE TABLE IF NOT EXISTS api_admin_logs (
                    log_id TEXT PRIMARY KEY,
                    component TEXT NOT NULL,
                    level TEXT NOT NULL,
                    message TEXT NOT NULL,
                    request_id TEXT,
                    occurred_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_admin_logs_occurred
                    ON api_admin_logs(occurred_at DESC, log_id DESC);
                """
            )

    def settings(self) -> tuple[int, tuple[dict[str, Any], ...]]:
        """读取当前 allowlist setting 及全局 revision。"""

        with self._lock:
            revision = int(
                self._connection.execute(
                    "SELECT revision FROM api_admin_state "
                    "WHERE state_key = 'settings'"
                ).fetchone()["revision"]
            )
            rows = {
                row["setting_key"]: row
                for row in self._connection.execute(
                    "SELECT * FROM api_admin_settings"
                ).fetchall()
            }
        values = []
        for key, spec in _SETTING_SPECS.items():
            row = rows.get(key)
            values.append(
                {
                    "key": key,
                    "value": json.loads(row["value_json"])
                    if row
                    else spec["default"],
                    "source": "admin" if row else "default",
                    "revision": int(row["revision"]) if row else 0,
                    "restart_required": bool(spec["restart_required"]),
                    "value_schema": {
                        name: value
                        for name, value in spec.items()
                        if name not in {"default", "restart_required"}
                    },
                }
            )
        return revision, tuple(values)

    @staticmethod
    def validate_settings(
        values: dict[str, Any],
    ) -> tuple[dict[str, Any], tuple[str, ...]]:
        """按固定 allowlist schema 验证候选值。"""

        normalized: dict[str, Any] = {}
        errors: list[str] = []
        for key, value in values.items():
            spec = _SETTING_SPECS.get(key)
            if spec is None:
                errors.append(f"unknown_setting:{key}")
                continue
            if isinstance(value, bool) or not isinstance(value, int):
                errors.append(f"invalid_type:{key}")
                continue
            if value < spec["minimum"] or value > spec["maximum"]:
                errors.append(f"out_of_range:{key}")
                continue
            normalized[key] = value
        return normalized, tuple(errors)

    def update_settings(
        self,
        *,
        actor_id: str,
        expected_revision: int,
        values: dict[str, Any],
        request_id: str | None,
    ) -> tuple[int, tuple[dict[str, Any], ...]]:
        """原子更新设置和审计；revision 不一致时不写入。"""

        normalized, errors = self.validate_settings(values)
        if errors:
            raise ValueError("settings_invalid:" + ",".join(errors))
        now = datetime.now(UTC).isoformat()
        with self._lock, self._connection:
            current = int(
                self._connection.execute(
                    "SELECT revision FROM api_admin_state "
                    "WHERE state_key = 'settings'"
                ).fetchone()["revision"]
            )
            if current != expected_revision:
                raise ValueError("revision_conflict")
            new_revision = current + 1
            for key, value in normalized.items():
                self._connection.execute(
                    """
                    INSERT INTO api_admin_settings (
                        setting_key, value_json, revision, updated_at, updated_by
                    ) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(setting_key) DO UPDATE SET
                        value_json = excluded.value_json,
                        revision = excluded.revision,
                        updated_at = excluded.updated_at,
                        updated_by = excluded.updated_by
                    """,
                    (key, canonical_json(value), new_revision, now, actor_id),
                )
            self._connection.execute(
                "UPDATE api_admin_state SET revision = ? "
                "WHERE state_key = 'settings'",
                (new_revision,),
            )
            self._append_audit_locked(
                event_type="admin.settings.updated",
                actor_id=actor_id,
                target_type="settings",
                target_id=None,
                outcome="succeeded",
                safe_detail=(
                    f"revision:{current}->{new_revision};"
                    f"keys:{','.join(sorted(normalized))}"
                ),
                request_id=request_id,
            )
        return self.settings()

    def append_audit(
        self,
        *,
        event_type: str,
        actor_id: str,
        target_type: str,
        target_id: str | None,
        outcome: str,
        safe_detail: str | None = None,
        request_id: str | None = None,
    ) -> str:
        """追加一条不含高敏正文的管理审计事件。"""

        with self._lock, self._connection:
            return self._append_audit_locked(
                event_type=event_type,
                actor_id=actor_id,
                target_type=target_type,
                target_id=target_id,
                outcome=outcome,
                safe_detail=safe_detail,
                request_id=request_id,
            )

    def _append_audit_locked(
        self,
        *,
        event_type: str,
        actor_id: str,
        target_type: str,
        target_id: str | None,
        outcome: str,
        safe_detail: str | None,
        request_id: str | None,
    ) -> str:
        audit_id = f"audit_{uuid4().hex}"
        self._connection.execute(
            "INSERT INTO api_admin_audit_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                audit_id,
                event_type,
                actor_id,
                target_type,
                target_id,
                outcome,
                safe_detail,
                request_id,
                datetime.now(UTC).isoformat(),
            ),
        )
        return audit_id

    def audits(
        self, *, limit: int = 100, audit_id: str | None = None
    ) -> tuple[dict[str, Any], ...]:
        """读取有界审计投影。"""

        query = "SELECT * FROM api_admin_audit_events"
        values: list[Any] = []
        if audit_id is not None:
            query += " WHERE audit_id = ?"
            values.append(audit_id)
        query += " ORDER BY occurred_at DESC, audit_id DESC LIMIT ?"
        values.append(max(1, min(500, int(limit))))
        with self._lock:
            rows = self._connection.execute(query, values).fetchall()
        return tuple(dict(row) for row in rows)

    def append_integration_event(
        self,
        *,
        integration_id: str,
        event_type: str,
        state: str,
        safe_detail: str | None = None,
    ) -> str:
        """追加集成状态事件，不执行 reconnect。"""

        event_id = f"integration_evt_{uuid4().hex}"
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO api_admin_integration_events VALUES (?, ?, ?, ?, ?, ?)",
                (
                    event_id,
                    integration_id,
                    event_type,
                    state,
                    safe_detail,
                    datetime.now(UTC).isoformat(),
                ),
            )
        return event_id

    def integration_events(
        self, integration_id: str, *, limit: int = 100
    ) -> tuple[dict[str, Any], ...]:
        """读取一个集成的有界状态历史。"""

        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM api_admin_integration_events "
                "WHERE integration_id = ? ORDER BY occurred_at DESC LIMIT ?",
                (integration_id, max(1, min(500, int(limit)))),
            ).fetchall()
        return tuple(dict(row) for row in rows)

    def logs(
        self,
        *,
        component: str | None = None,
        level: str | None = None,
        request_id: str | None = None,
        limit: int = 100,
    ) -> tuple[dict[str, Any], ...]:
        """只读取受管结构化日志投影，不接受路径参数。"""

        clauses: list[str] = []
        values: list[Any] = []
        for field, value in (
            ("component", component),
            ("level", level),
            ("request_id", request_id),
        ):
            if value:
                clauses.append(f"{field} = ?")
                values.append(value)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        values.append(max(1, min(500, int(limit))))
        with self._lock:
            rows = self._connection.execute(
                f"SELECT * FROM api_admin_logs{where} "
                "ORDER BY occurred_at DESC LIMIT ?",
                values,
            ).fetchall()
        return tuple(dict(row) for row in rows)


__all__ = ["AdminStore"]
