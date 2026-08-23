"""Canonical, backend-neutral snapshot of the one proactive authority.

The proactive domain shares generic runtime tables with other Life Engine
subsystems.  This module is the only inventory used by migration and backend
binding: it selects AttentionThread authority, Initiative authority, and the
durable outreach inbox/turn state without copying unrelated runtime rows.
Physical positions from the shared ``runtime_events`` table are deliberately
excluded because they are backend-local allocation details.  Attention event
positions remain part of the contract because heads refer to them directly.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.kernel.storage import canonical_json

from ..storage.contracts import StorageBackendRuntime
from ..storage.models import BackendKind

PROACTIVE_HISTORY_ALGORITHM_VERSION = "proactive-authority-history-v1"
PROACTIVE_HISTORY_ROOT_NAME = "proactive_authority"
PROACTIVE_MIGRATION_NAMESPACE = "life_proactive.migrations"
PROACTIVE_MIGRATION_EVENT_KIND = "proactive_authority_migrated"


@dataclass(frozen=True, slots=True)
class ProactiveTableSpec:
    """Stable projection contract for one physical table slice."""

    name: str
    columns: tuple[str, ...]
    key_columns: tuple[str, ...]
    select_sql: str
    json_columns: frozenset[str] = frozenset()
    datetime_columns: frozenset[str] = frozenset()
    integer_columns: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class ProactiveTableSnapshot:
    """Canonical rows and evidence for one selected table slice."""

    name: str
    rows: tuple[dict[str, Any], ...]
    row_count: int
    root_sha256: str


@dataclass(frozen=True, slots=True)
class ProactiveHistorySnapshot:
    """Content-addressed state of both proactive record families."""

    tables: tuple[ProactiveTableSnapshot, ...]
    row_count: int
    root_sha256: str
    frontiers: dict[str, int]
    algorithm_version: str = PROACTIVE_HISTORY_ALGORITHM_VERSION

    def table(self, name: str) -> ProactiveTableSnapshot:
        for item in self.tables:
            if item.name == name:
                return item
        raise KeyError(name)

    def evidence(self) -> dict[str, Any]:
        return {
            "algorithm_version": self.algorithm_version,
            "row_count": int(self.row_count),
            "root_sha256": self.root_sha256,
            "frontiers": dict(sorted(self.frontiers.items())),
            "tables": [
                {
                    "name": item.name,
                    "row_count": int(item.row_count),
                    "root_sha256": item.root_sha256,
                }
                for item in self.tables
            ],
        }


_ATTENTION_EVENT_COLUMNS = (
    "position",
    "event_id",
    "occurrence_id",
    "thread_id",
    "action",
    "actor_consciousness_instance_id",
    "source_instance_id",
    "source_occurrence_ids_json",
    "causation_occurrence_id",
    "expected_revision",
    "revision",
    "public_statement",
    "occurred_at",
    "recorded_at",
    "event_sha256",
)
_ATTENTION_HEAD_COLUMNS = (
    "thread_id",
    "status",
    "revision",
    "opened_at",
    "last_changed_at",
    "current_statement",
    "statement_event_id",
    "statement_sha256",
    "statement_bytes",
    "last_event_id",
    "last_occurrence_id",
    "last_event_position",
    "updated_at",
)
_ATTENTION_FOCUS_COLUMNS = (
    "instance_id",
    "focus_occurrence_id",
    "source_occurrence_id",
    "entered_at",
    "expires_at",
    "revision",
    "thread_id",
    "updated_at",
)
_RUNTIME_EVENT_COLUMNS = (
    "namespace",
    "occurrence_id",
    "event_kind",
    "payload_json",
    "payload_sha256",
    "occurred_at",
    "recorded_at",
)
_RUNTIME_STATE_COLUMNS = (
    "namespace",
    "state_key",
    "revision",
    "schema_version",
    "payload_json",
    "payload_sha256",
    "updated_at",
)
_INBOUND_COLUMNS = (
    "message_id",
    "platform",
    "platform_event_id",
    "occurrence_id",
    "payload_sha256",
    "stream_id",
    "reply_target",
    "source",
    "occurred_at",
    "received_at",
    "raw_payload_ref",
)
_TURN_COLUMNS = (
    "turn_id",
    "stream_id",
    "stream_sequence",
    "source_message_id",
    "status",
    "claim_owner",
    "claim_epoch",
    "lease_until",
    "input_frontier_json",
    "result_ref",
    "result_digest",
    "attempts",
    "created_at",
    "updated_at",
)


def _columns(values: tuple[str, ...], *, prefix: str = "") -> str:
    return ", ".join(f"{prefix}{value}" for value in values)


PROACTIVE_TABLE_SPECS: tuple[ProactiveTableSpec, ...] = (
    ProactiveTableSpec(
        name="attention_thread_events",
        columns=_ATTENTION_EVENT_COLUMNS,
        key_columns=("position",),
        select_sql=(
            f"SELECT {_columns(_ATTENTION_EVENT_COLUMNS)} "
            "FROM attention_thread_events ORDER BY position"
        ),
        json_columns=frozenset({"source_occurrence_ids_json"}),
        datetime_columns=frozenset({"occurred_at", "recorded_at"}),
        integer_columns=frozenset({"position", "expected_revision", "revision"}),
    ),
    ProactiveTableSpec(
        name="attention_thread_heads",
        columns=_ATTENTION_HEAD_COLUMNS,
        key_columns=("thread_id",),
        select_sql=(
            f"SELECT {_columns(_ATTENTION_HEAD_COLUMNS)} "
            "FROM attention_thread_heads ORDER BY thread_id"
        ),
        datetime_columns=frozenset(
            {"opened_at", "last_changed_at", "updated_at"}
        ),
        integer_columns=frozenset(
            {"revision", "statement_bytes", "last_event_position"}
        ),
    ),
    ProactiveTableSpec(
        name="attention_instance_focus",
        columns=_ATTENTION_FOCUS_COLUMNS,
        key_columns=("instance_id",),
        select_sql=(
            f"SELECT {_columns(_ATTENTION_FOCUS_COLUMNS)} "
            "FROM attention_instance_focus ORDER BY instance_id"
        ),
        datetime_columns=frozenset({"entered_at", "expires_at", "updated_at"}),
        integer_columns=frozenset({"revision"}),
    ),
    ProactiveTableSpec(
        name="runtime_events",
        columns=_RUNTIME_EVENT_COLUMNS,
        key_columns=("occurrence_id",),
        select_sql=(
            f"SELECT {_columns(_RUNTIME_EVENT_COLUMNS)} FROM runtime_events "
            "WHERE namespace LIKE 'life_initiative.%' "
            "OR namespace = 'life_proactive.decision_guards' "
            "ORDER BY namespace, occurrence_id"
        ),
        json_columns=frozenset({"payload_json"}),
        datetime_columns=frozenset({"occurred_at", "recorded_at"}),
    ),
    ProactiveTableSpec(
        name="runtime_states",
        columns=_RUNTIME_STATE_COLUMNS,
        key_columns=("namespace", "state_key"),
        select_sql=(
            f"SELECT {_columns(_RUNTIME_STATE_COLUMNS)} FROM runtime_states "
            "WHERE namespace LIKE 'life_initiative.%' "
            "ORDER BY namespace, state_key"
        ),
        json_columns=frozenset({"payload_json"}),
        datetime_columns=frozenset({"updated_at"}),
        integer_columns=frozenset({"revision", "schema_version"}),
    ),
    ProactiveTableSpec(
        name="inbound_messages",
        columns=_INBOUND_COLUMNS,
        key_columns=("message_id",),
        select_sql=(
            f"SELECT {_columns(_INBOUND_COLUMNS)} FROM inbound_messages "
            "WHERE source = 'life_engine.proactive' ORDER BY message_id"
        ),
        datetime_columns=frozenset({"occurred_at", "received_at"}),
    ),
    ProactiveTableSpec(
        name="stream_turns",
        columns=_TURN_COLUMNS,
        key_columns=("turn_id",),
        select_sql=(
            f"SELECT {_columns(_TURN_COLUMNS, prefix='t.')} FROM stream_turns AS t "
            "JOIN inbound_messages AS m ON m.message_id = t.source_message_id "
            "WHERE m.source = 'life_engine.proactive' ORDER BY t.turn_id"
        ),
        json_columns=frozenset({"input_frontier_json"}),
        datetime_columns=frozenset(
            {"lease_until", "created_at", "updated_at"}
        ),
        integer_columns=frozenset(
            {"stream_sequence", "claim_epoch", "attempts"}
        ),
    ),
)


def _datetime_text(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        raw = str(value).strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat(timespec="microseconds")


def _json_text(value: Any) -> str:
    if isinstance(value, (bytes, bytearray, memoryview)):
        value = bytes(value).decode("utf-8")
    try:
        decoded = json.loads(value) if isinstance(value, str) else value
    except json.JSONDecodeError as exc:
        raise RuntimeError("ProactiveHistoryJSONCorrupt") from exc
    return canonical_json(decoded)


def _normalize_row(spec: ProactiveTableSpec, row: Any) -> dict[str, Any]:
    raw = dict(row)
    normalized: dict[str, Any] = {}
    for column in spec.columns:
        value = raw[column]
        if column in spec.json_columns:
            value = _json_text(value)
        elif column in spec.datetime_columns:
            value = _datetime_text(value)
        elif column in spec.integer_columns and value is not None:
            value = int(value)
        elif isinstance(value, (bytes, bytearray, memoryview)):
            value = bytes(value).decode("utf-8")
        normalized[column] = value
    return normalized


def _table_snapshot(
    spec: ProactiveTableSpec,
    rows: list[dict[str, Any]],
) -> ProactiveTableSnapshot:
    ordered = sorted(
        rows,
        key=lambda item: tuple(item[column] for column in spec.key_columns),
    )
    digest = hashlib.sha256()
    for row in ordered:
        digest.update(
            hashlib.sha256(
                canonical_json(
                    {
                        "table": spec.name,
                        "row": {column: row[column] for column in spec.columns},
                    }
                ).encode("utf-8")
            ).digest()
        )
    return ProactiveTableSnapshot(
        name=spec.name,
        rows=tuple(ordered),
        row_count=len(ordered),
        root_sha256=digest.hexdigest(),
    )


def _history_snapshot(
    selected: dict[str, list[dict[str, Any]]],
) -> ProactiveHistorySnapshot:
    tables = tuple(
        _table_snapshot(spec, selected.get(spec.name, []))
        for spec in PROACTIVE_TABLE_SPECS
    )
    root = hashlib.sha256(
        canonical_json(
            {
                "algorithm_version": PROACTIVE_HISTORY_ALGORITHM_VERSION,
                "tables": [
                    {
                        "name": item.name,
                        "row_count": item.row_count,
                        "root_sha256": item.root_sha256,
                    }
                    for item in tables
                ],
            }
        ).encode("utf-8")
    ).hexdigest()
    attention_events = next(
        item for item in tables if item.name == "attention_thread_events"
    )
    initiative_events = next(item for item in tables if item.name == "runtime_events")
    attention_frontier = max(
        (int(row["position"]) for row in attention_events.rows),
        default=0,
    )
    return ProactiveHistorySnapshot(
        tables=tables,
        row_count=sum(item.row_count for item in tables),
        root_sha256=root,
        frontiers={
            "attention_event_position": attention_frontier,
            "initiative_event_count": initiative_events.row_count,
        },
    )


def read_sqlite_proactive_history(
    database_path: str | Path,
) -> ProactiveHistorySnapshot:
    """Read one frozen local proactive database without changing it."""

    path = Path(database_path).resolve(strict=True)
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        quick_check = connection.execute("PRAGMA quick_check").fetchone()
        if quick_check is None or str(quick_check[0]).lower() != "ok":
            raise RuntimeError("ProactiveHistorySQLiteIntegrityFailed")
        selected: dict[str, list[dict[str, Any]]] = {}
        for spec in PROACTIVE_TABLE_SPECS:
            selected[spec.name] = [
                _normalize_row(spec, row)
                for row in connection.execute(spec.select_sql).fetchall()
            ]
        return _history_snapshot(selected)
    except sqlite3.DatabaseError as exc:
        raise RuntimeError("ProactiveHistorySQLiteUnreadable") from exc
    finally:
        connection.close()


async def read_proactive_history_in_session(
    session: AsyncSession,
) -> ProactiveHistorySnapshot:
    """Read one coherent proactive snapshot inside the caller's transaction."""

    selected: dict[str, list[dict[str, Any]]] = {}
    for spec in PROACTIVE_TABLE_SPECS:
        rows = (await session.execute(text(spec.select_sql))).mappings().all()
        selected[spec.name] = [_normalize_row(spec, row) for row in rows]
    return _history_snapshot(selected)


async def read_runtime_proactive_history(
    runtime: StorageBackendRuntime,
) -> ProactiveHistorySnapshot:
    """Read one coherent proactive snapshot from an owned runtime."""

    async with runtime.unit_of_work() as uow:
        return await read_proactive_history_in_session(uow.session)


def _key(spec: ProactiveTableSpec, row: dict[str, Any]) -> tuple[Any, ...]:
    return tuple(row[column] for column in spec.key_columns)


def _bind_datetime(backend: BackendKind, value: Any) -> Any:
    if value in {None, ""}:
        return None
    parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    parsed = parsed.astimezone(UTC)
    if backend == BackendKind.MYSQL:
        return parsed.replace(tzinfo=None)
    return parsed.isoformat()


def _bind_row(
    spec: ProactiveTableSpec,
    row: dict[str, Any],
    backend: BackendKind,
) -> dict[str, Any]:
    bound = dict(row)
    for column in spec.datetime_columns:
        bound[column] = _bind_datetime(backend, bound[column])
    return bound


async def copy_missing_proactive_rows(
    session: AsyncSession,
    runtime: StorageBackendRuntime,
    source: ProactiveHistorySnapshot,
) -> tuple[int, bool]:
    """Resume an exact candidate copy; any divergent or extra row fails closed."""

    target = await read_proactive_history_in_session(session)
    copied = 0
    for spec in PROACTIVE_TABLE_SPECS:
        source_table = source.table(spec.name)
        target_table = target.table(spec.name)
        source_by_key = {_key(spec, row): row for row in source_table.rows}
        target_by_key = {_key(spec, row): row for row in target_table.rows}
        extras = set(target_by_key) - set(source_by_key)
        conflicts = {
            key
            for key in set(target_by_key) & set(source_by_key)
            if target_by_key[key] != source_by_key[key]
        }
        if extras or conflicts:
            raise RuntimeError(f"ProactiveMigrationTargetConflict:{spec.name}")
        missing = [
            row for key, row in source_by_key.items() if key not in target_by_key
        ]
        if missing:
            columns = ", ".join(spec.columns)
            parameters = ", ".join(f":{column}" for column in spec.columns)
            await session.execute(
                text(
                    f"INSERT INTO {spec.name} ({columns}) VALUES ({parameters})"
                ),
                [_bind_row(spec, row, runtime.backend) for row in missing],
            )
            copied += len(missing)
    final = await read_proactive_history_in_session(session)
    if final.root_sha256 != source.root_sha256 or final.row_count != source.row_count:
        raise RuntimeError("ProactiveMigrationTargetVerificationFailed")
    return copied, copied == 0


__all__ = [
    "PROACTIVE_HISTORY_ALGORITHM_VERSION",
    "PROACTIVE_HISTORY_ROOT_NAME",
    "PROACTIVE_MIGRATION_EVENT_KIND",
    "PROACTIVE_MIGRATION_NAMESPACE",
    "PROACTIVE_TABLE_SPECS",
    "ProactiveHistorySnapshot",
    "ProactiveTableSnapshot",
    "copy_missing_proactive_rows",
    "read_proactive_history_in_session",
    "read_runtime_proactive_history",
    "read_sqlite_proactive_history",
]
