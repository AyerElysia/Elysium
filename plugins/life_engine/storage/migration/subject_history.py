"""Exact, versioned subject snapshots and fenced candidate-only import.

All eight schema-v5 subject tables are copied without replaying append commands,
changing record identities, activating authority, or erasing target history.
This is not a backup of other life domains, authority registry or external blobs.
Wire timestamps preserve UTC instants at microsecond precision; JSON columns
preserve parsed values, not backend whitespace. Version bytes and semantic NULLs
are never normalized. Outbox timestamp empty sentinels use NULL on the wire.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from ..contracts import StorageBackendRuntime, StorageWriterRole
from ..models import BackendKind

FORMAT = "elysium-subject-history-v1"
SCHEMA_VERSION = 5
_BUNDLE_NAME = "subject-history.json"
_INCOMPLETE = "SUBJECT_HISTORY_INCOMPLETE"
_DEFAULT_MAX_ROWS = 200_000
_DEFAULT_MAX_BYTES = 256 * 1024 * 1024


class SubjectHistoryError(RuntimeError):
    """Reject unsupported, incomplete, conflicting or unauthorized history."""


@dataclass(frozen=True, slots=True)
class SubjectHistoryReport:
    """Content-free receipt; successful import never means activation."""

    bundle_sha256: str
    table_counts: dict[str, int]
    table_roots: dict[str, str]
    content_bytes: int
    idempotent_replay: bool = False


@dataclass(frozen=True, slots=True)
class _Table:
    name: str
    primary_key: str
    columns: dict[str, str]


def _columns(**groups: str) -> dict[str, str]:
    return {name: kind for kind, names in groups.items() for name in names.split()}


# Identifiers exclusively use this versioned allowlist. Insertion order follows
# foreign keys. No table/column name from a bundle is interpolated into SQL.
_TABLES = (
    _Table(
        "subject_documents",
        "document_id",
        _columns(
            string="document_id logical_path current_version_id",
            nullable_string="declared_owner",
            integer="revision binding_revision is_deleted",
        ),
    ),
    _Table(
        "subject_document_versions",
        "version_id",
        _columns(
            string=(
                "version_id document_id logical_path parent_version_id occurrence_id "
                "recorded_by recorded_source provenance_status content_hash byte_fidelity"
            ),
            nullable_string="semantic_actor_id semantic_source_id encoding newline_style",
            timestamp="recorded_at",
            nullable_timestamp="occurred_at",
            integer="byte_length",
            binary="content_bytes",
            json="change_context_json",
        ),
    ),
    _Table(
        "subject_document_head_events",
        "head_event_id",
        _columns(
            string=(
                "head_event_id document_id previous_version_id next_version_id "
                "occurrence_id actor_id source_id"
            ),
            timestamp="occurred_at",
            integer="authority_epoch",
            json="change_context_json",
        ),
    ),
    _Table(
        "subject_projection_outbox",
        "outbox_id",
        _columns(
            string=(
                "head_event_id document_id logical_path version_id content_hash state "
                "last_error lease_owner operation previous_logical_path "
                "previous_version_id previous_content_hash"
            ),
            integer=(
                "outbox_id attempt_count revision binding_revision "
                "previous_binding_revision"
            ),
            timestamp="created_at",
            outbox_timestamp="confirmed_at lease_until",
        ),
    ),
    _Table(
        "subject_authority_decisions",
        "decision_occurrence_id",
        _columns(
            string=(
                "decision_occurrence_id authority_occurrence_id candidate_id "
                "candidate_sha256 candidate_occurrence_id actor_consciousness_instance_id "
                "expected_subject_revision target_path accepted_content_sha256 "
                "previous_subject_revision new_subject_revision document_version_id "
                "command_sha256"
            ),
            integer="candidate_revision document_revision",
            timestamp="occurred_at committed_at",
        ),
    ),
    _Table(
        "subject_document_path_bindings",
        "logical_path",
        _columns(
            string="logical_path",
            nullable_string="document_id",
            integer="revision",
        ),
    ),
    _Table(
        "subject_document_path_events",
        "event_id",
        _columns(
            string="event_id logical_path occurrence_id",
            nullable_string="previous_document_id document_id",
            integer="previous_revision revision",
            timestamp="recorded_at",
        ),
    ),
    _Table(
        "subject_document_operations",
        "occurrence_id",
        _columns(
            string="occurrence_id operation document_id command_digest",
            json="result_json change_context_json",
            timestamp="recorded_at",
        ),
    ),
)
_TABLE_BY_NAME = {table.name: table for table in _TABLES}


def _canonical(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise SubjectHistoryError("history contains a non-JSON value") from exc


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise SubjectHistoryError("history JSON contains a duplicate key")
        result[key] = value
    return result


def _load_json(value: str | bytes) -> Any:
    try:
        return json.loads(value, object_pairs_hook=_object_pairs)
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise SubjectHistoryError("history JSON is invalid") from exc


def _digest(value: Any) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(char not in "0123456789abcdef" for char in value)
    ):
        raise SubjectHistoryError("history digest is not lowercase SHA-256")
    return value


def _timestamp(value: Any) -> str:
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value)
        return (
            parsed.replace(tzinfo=parsed.tzinfo or UTC)
            .astimezone(UTC)
            .isoformat(timespec="microseconds")
        )
    except (TypeError, ValueError, OverflowError) as exc:
        raise SubjectHistoryError("history timestamp is invalid") from exc


def _wire_value(kind: str, value: Any, *, from_wire: bool) -> Any:
    if kind.startswith("nullable_") and value is None:
        return None
    if kind in {"string", "nullable_string"}:
        if not isinstance(value, str):
            raise SubjectHistoryError("history string column has invalid type")
        return value
    if kind == "integer":
        if type(value) is not int or not 0 <= value <= (1 << 63) - 1:
            raise SubjectHistoryError("history integer column is out of range")
        return value
    if kind == "outbox_timestamp" and value in (None, ""):
        return None
    if kind in {"timestamp", "nullable_timestamp", "outbox_timestamp"}:
        normalized = _timestamp(value)
        if from_wire and normalized != value:
            raise SubjectHistoryError("history wire timestamp is not canonical")
        return normalized
    if kind == "json":
        parsed = value if isinstance(value, dict) else _load_json(value)
        if not isinstance(parsed, dict):
            raise SubjectHistoryError("history JSON column must be an object")
        _canonical(parsed)
        return parsed
    if kind == "binary":
        if from_wire:
            if not isinstance(value, str):
                raise SubjectHistoryError("history bytes must be base64 text")
            try:
                raw = base64.b64decode(value, validate=True)
            except (ValueError, binascii.Error) as exc:
                raise SubjectHistoryError(
                    "history bytes contain invalid base64"
                ) from exc
            if base64.b64encode(raw).decode("ascii") != value:
                raise SubjectHistoryError("history bytes are not canonical base64")
            return value
        if not isinstance(value, (bytes, bytearray, memoryview)):
            raise SubjectHistoryError("history content_bytes is not binary")
        return base64.b64encode(bytes(value)).decode("ascii")
    raise SubjectHistoryError("unsupported history column kind")


def _wire_rows(
    table: _Table,
    rows: Sequence[Mapping[str, Any]],
    *,
    from_wire: bool,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[Any] = set()
    for row in rows:
        if not isinstance(row, Mapping) or set(row) != set(table.columns):
            raise SubjectHistoryError(
                f"history columns do not match schema: {table.name}"
            )
        encoded = {
            name: _wire_value(kind, row[name], from_wire=from_wire)
            for name, kind in table.columns.items()
        }
        identity = encoded[table.primary_key]
        if not identity or identity in seen:
            raise SubjectHistoryError(
                f"history duplicate or empty identity: {table.name}"
            )
        seen.add(identity)
        result.append(encoded)
    return sorted(result, key=lambda row: row[table.primary_key])


def _validate_closure(tables: dict[str, list[dict[str, Any]]]) -> int:
    documents = {row["document_id"]: row for row in tables["subject_documents"]}
    versions = {row["version_id"]: row for row in tables["subject_document_versions"]}
    events = {
        row["head_event_id"]: row for row in tables["subject_document_head_events"]
    }

    def document(identity: Any) -> dict[str, Any]:
        if not isinstance(identity, str) or identity not in documents:
            raise SubjectHistoryError("history document reference is missing")
        return documents[identity]

    def version(identity: Any, owner: str | None = None) -> dict[str, Any]:
        found = versions.get(identity) if isinstance(identity, str) else None
        if found is None or (owner is not None and found["document_id"] != owner):
            raise SubjectHistoryError(
                "history version reference is missing or cross-document"
            )
        return found

    total_bytes = 0
    occurrences: set[tuple[str, str]] = set()
    for row in versions.values():
        document(row["document_id"])
        if row["parent_version_id"]:
            version(row["parent_version_id"], row["document_id"])
        raw = base64.b64decode(row["content_bytes"], validate=True)
        if hashlib.sha256(raw).hexdigest() != _digest(row["content_hash"]):
            raise SubjectHistoryError("history version content checksum mismatch")
        if row["byte_length"] != len(raw):
            raise SubjectHistoryError("history version byte length mismatch")
        total_bytes += len(raw)
        occurrence = (row["document_id"], row["occurrence_id"])
        if not row["occurrence_id"] or occurrence in occurrences:
            raise SubjectHistoryError("history version occurrence is duplicated")
        occurrences.add(occurrence)

    # Iterative traversal avoids recursion limits for long immutable histories.
    visited: set[str] = set()
    for identity in versions:
        chain: set[str] = set()
        cursor = identity
        while cursor and cursor not in visited:
            if cursor in chain:
                raise SubjectHistoryError(
                    "history version parent chain contains a cycle"
                )
            chain.add(cursor)
            cursor = versions[cursor]["parent_version_id"]
        visited.update(chain)

    for row in documents.values():
        version(row["current_version_id"], row["document_id"])
        if row["revision"] < 1 or row["is_deleted"] not in (0, 1):
            raise SubjectHistoryError("history document head is invalid")

    event_occurrences: set[tuple[str, str]] = set()
    covered_versions: set[str] = set()
    for row in events.values():
        document(row["document_id"])
        version(row["next_version_id"], row["document_id"])
        if row["previous_version_id"]:
            version(row["previous_version_id"], row["document_id"])
        key = (row["document_id"], row["occurrence_id"])
        if key in event_occurrences:
            raise SubjectHistoryError("history head-event occurrence is duplicated")
        event_occurrences.add(key)
        covered_versions.add(row["next_version_id"])
    if covered_versions != set(versions):
        raise SubjectHistoryError("history version has no head event")

    outbox_events: set[str] = set()
    for row in tables["subject_projection_outbox"]:
        head_event = events.get(row["head_event_id"])
        item = version(row["version_id"], row["document_id"])
        if (
            head_event is None
            or head_event["document_id"] != row["document_id"]
            or head_event["next_version_id"] != row["version_id"]
            or row["content_hash"] != item["content_hash"]
            or row["head_event_id"] in outbox_events
            or row["state"] not in {"pending", "confirmed", "failed"}
        ):
            raise SubjectHistoryError("history projection reference is invalid")
        if row["previous_version_id"]:
            previous = version(row["previous_version_id"], row["document_id"])
            if row["previous_content_hash"] != previous["content_hash"]:
                raise SubjectHistoryError("history previous projection hash mismatch")
        elif row["previous_content_hash"]:
            raise SubjectHistoryError("history previous projection version is missing")
        outbox_events.add(row["head_event_id"])
    if outbox_events != set(events):
        raise SubjectHistoryError("history head event has no projection record")

    decision_authorities: set[str] = set()
    for row in tables["subject_authority_decisions"]:
        item = version(row["document_version_id"])
        if row["accepted_content_sha256"] != item["content_hash"]:
            raise SubjectHistoryError("history authority decision hash mismatch")
        if row["authority_occurrence_id"] in decision_authorities:
            raise SubjectHistoryError("history authority occurrence is duplicated")
        decision_authorities.add(row["authority_occurrence_id"])

    path_events: dict[str, list[dict[str, Any]]] = {}
    for row in tables["subject_document_path_events"]:
        for field in ("document_id", "previous_document_id"):
            if row[field] is not None:
                document(row[field])
        path_events.setdefault(row["logical_path"], []).append(row)
    bindings = {
        row["logical_path"]: row for row in tables["subject_document_path_bindings"]
    }
    if set(bindings) != set(path_events):
        raise SubjectHistoryError("history path binding/event coverage mismatch")
    bound_documents: set[str] = set()
    for path, binding in bindings.items():
        previous_revision, previous_document = 0, None
        for event in sorted(path_events[path], key=lambda row: row["revision"]):
            if (
                event["previous_revision"] != previous_revision
                or event["revision"] != previous_revision + 1
                or event["previous_document_id"] != previous_document
            ):
                raise SubjectHistoryError("history path event chain is discontinuous")
            previous_revision, previous_document = (
                event["revision"],
                event["document_id"],
            )
        if (binding["revision"], binding["document_id"]) != (
            previous_revision,
            previous_document,
        ):
            raise SubjectHistoryError("history path binding does not match its history")
        if binding["document_id"] is not None:
            head = document(binding["document_id"])
            if (
                head["is_deleted"]
                or head["logical_path"] != path
                or head["binding_revision"] != binding["revision"]
                or head["document_id"] in bound_documents
            ):
                raise SubjectHistoryError("history current binding/head mismatch")
            bound_documents.add(head["document_id"])
    if bound_documents != {
        identity for identity, row in documents.items() if not row["is_deleted"]
    }:
        raise SubjectHistoryError("history live document has no current binding")

    for row in tables["subject_document_operations"]:
        document(row["document_id"])
        _digest(row["command_digest"])
        result = row["result_json"]
        head = result.get("head")
        if not isinstance(head, dict) or head.get("document_id") != row["document_id"]:
            raise SubjectHistoryError("history operation result document is invalid")
        version(result.get("version_id"), row["document_id"])
    return total_bytes


def _limits(max_rows: int, max_bytes: int) -> None:
    if (
        type(max_rows) is not int
        or max_rows <= 0
        or type(max_bytes) is not int
        or max_bytes <= 0
    ):
        raise ValueError("history resource limits must be positive integers")


def encode_subject_history_bundle(
    rows: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    source_identity_sha256: str,
    captured_at: str,
    max_rows: int = _DEFAULT_MAX_ROWS,
    max_bytes: int = _DEFAULT_MAX_BYTES,
) -> bytes:
    """Encode complete schema-v5 rows; reject gaps, never silently truncate.

    Callers supplying rows own the consistent-snapshot guarantee. Prefer
    ``capture_subject_history`` for a database. No IDs are recomputed.
    """
    _limits(max_rows, max_bytes)
    if set(rows) != set(_TABLE_BY_NAME):
        raise SubjectHistoryError("history table set does not match schema v5")
    if sum(len(items) for items in rows.values()) > max_rows:
        raise SubjectHistoryError("history row limit exceeded; snapshot not truncated")
    wire = {
        table.name: _wire_rows(table, rows[table.name], from_wire=False)
        for table in _TABLES
    }
    content_bytes = _validate_closure(wire)
    tables = {
        table.name: {
            "columns": sorted(table.columns),
            "count": len(wire[table.name]),
            "sha256": _hash(wire[table.name]),
            "rows": wire[table.name],
        }
        for table in _TABLES
    }
    envelope = {
        "format": FORMAT,
        "schema_version": SCHEMA_VERSION,
        "scope": "subject-domain-only",
        "source_identity_sha256": _digest(source_identity_sha256),
        "captured_at": _timestamp(captured_at),
        "content_bytes": content_bytes,
        "tables": tables,
    }
    envelope["bundle_sha256"] = _hash(envelope)
    payload = _canonical(envelope)
    if len(payload) > max_bytes:
        raise SubjectHistoryError("history byte limit exceeded; snapshot not truncated")
    return payload


def _decode(
    payload: bytes,
    *,
    max_rows: int,
    max_bytes: int,
) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]]]:
    _limits(max_rows, max_bytes)
    if not isinstance(payload, bytes) or len(payload) > max_bytes:
        raise SubjectHistoryError("history bundle exceeds byte limit or is not bytes")
    envelope = _load_json(payload)
    expected = {
        "format",
        "schema_version",
        "scope",
        "source_identity_sha256",
        "captured_at",
        "content_bytes",
        "tables",
        "bundle_sha256",
    }
    if not isinstance(envelope, dict) or set(envelope) != expected:
        raise SubjectHistoryError("history bundle envelope is invalid")
    if (
        envelope["format"] != FORMAT
        or type(envelope["schema_version"]) is not int
        or envelope["schema_version"] != SCHEMA_VERSION
        or envelope["scope"] != "subject-domain-only"
    ):
        raise SubjectHistoryError("unsupported history format, schema, or scope")
    unsigned = {key: value for key, value in envelope.items() if key != "bundle_sha256"}
    if _digest(envelope["bundle_sha256"]) != _hash(unsigned):
        raise SubjectHistoryError("history bundle checksum mismatch")
    _digest(envelope["source_identity_sha256"])
    _wire_value("timestamp", envelope["captured_at"], from_wire=True)
    table_data = envelope["tables"]
    if not isinstance(table_data, dict) or set(table_data) != set(_TABLE_BY_NAME):
        raise SubjectHistoryError("history table set does not match schema v5")
    wire: dict[str, list[dict[str, Any]]] = {}
    count = 0
    for table in _TABLES:
        entry = table_data[table.name]
        if not isinstance(entry, dict) or set(entry) != {
            "columns",
            "count",
            "sha256",
            "rows",
        }:
            raise SubjectHistoryError("history table manifest is invalid")
        rows = entry["rows"]
        if (
            not isinstance(rows, list)
            or type(entry["count"]) is not int
            or entry["count"] != len(rows)
        ):
            raise SubjectHistoryError("history table count mismatch")
        count += len(rows)
        if count > max_rows:
            raise SubjectHistoryError(
                "history row limit exceeded; snapshot not truncated"
            )
        if entry["columns"] != sorted(table.columns):
            raise SubjectHistoryError("history columns do not match schema v5")
        wire[table.name] = _wire_rows(table, rows, from_wire=True)
        if wire[table.name] != rows or _digest(entry["sha256"]) != _hash(rows):
            raise SubjectHistoryError("history table checksum/order mismatch")
    content_bytes = _validate_closure(wire)
    if (
        type(envelope["content_bytes"]) is not int
        or envelope["content_bytes"] != content_bytes
    ):
        raise SubjectHistoryError("history total content byte length mismatch")
    return envelope, wire


def _report(envelope: dict[str, Any], *, replay: bool = False) -> SubjectHistoryReport:
    return SubjectHistoryReport(
        bundle_sha256=envelope["bundle_sha256"],
        table_counts={
            name: entry["count"] for name, entry in envelope["tables"].items()
        },
        table_roots={
            name: entry["sha256"] for name, entry in envelope["tables"].items()
        },
        content_bytes=envelope["content_bytes"],
        idempotent_replay=replay,
    )


def verify_subject_history_bundle(
    payload: bytes,
    *,
    max_rows: int = _DEFAULT_MAX_ROWS,
    max_bytes: int = _DEFAULT_MAX_BYTES,
) -> SubjectHistoryReport:
    """Check the complete manifest, exact bytes and subject reference closure."""
    envelope, _ = _decode(payload, max_rows=max_rows, max_bytes=max_bytes)
    return _report(envelope)


async def _database_identity(
    connection: AsyncConnection, runtime: StorageBackendRuntime
) -> str:
    if runtime.backend == BackendKind.MYSQL:
        row = (await connection.execute(text("SELECT @@server_uuid, DATABASE()"))).one()
        return _hash(["mysql", str(row[0]), str(row[1])])
    database = runtime.engine.url.database if runtime.engine is not None else None
    if not database or database == ":memory:":
        raise SubjectHistoryError(
            "history migration requires an identifiable database file"
        )
    return _hash(["local", str(Path(database).resolve())])


async def _reject_active_database(connection: AsyncConnection) -> None:
    """Do not trust a mislabelled copy runtime when this DB declares authority."""
    mysql = connection.dialect.name == "mysql"
    query = (
        "SELECT COUNT(*) FROM information_schema.tables "
        "WHERE table_schema = DATABASE() AND table_name = 'storage_authority_registry'"
        if mysql
        else "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' "
        "AND name = 'storage_authority_registry'"
    )
    if await connection.scalar(text(query)):
        suffix = " FOR UPDATE" if mysql else ""
        active = (
            await connection.execute(
                text(
                    "SELECT active_generation FROM storage_authority_registry" + suffix
                )
            )
        ).scalars()
        if any(str(generation or "") for generation in active):
            raise SubjectHistoryError(
                "history import target database declares active authority"
            )


async def _check_table_set(executor: AsyncConnection | AsyncSession) -> None:
    dialect = (
        executor.bind.dialect.name
        if isinstance(executor, AsyncSession)
        else executor.dialect.name
    )
    if dialect == "mysql":
        query = text(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = DATABASE() AND LEFT(table_name, 8) = 'subject_'"
        )
    else:
        query = text(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND substr(name, 1, 8) = 'subject_'"
        )
    names = set((await executor.execute(query)).scalars())
    allowed = set(_TABLE_BY_NAME) | {
        "subject_document_schema_migrations",
        "subject_document_immutability_migrations",
    }
    if not set(_TABLE_BY_NAME) <= names or not names <= allowed:
        raise SubjectHistoryError("database subject table set does not match schema v5")
    if dialect == "mysql":
        current = await executor.scalar(
            text("SELECT MAX(version) FROM subject_document_schema_migrations")
        )
        if current != SCHEMA_VERSION:
            raise SubjectHistoryError("database subject schema version is not v5")


async def _read_tables(
    executor: AsyncConnection | AsyncSession,
    *,
    max_rows: int,
    max_bytes: int,
) -> dict[str, list[dict[str, Any]]]:
    await _check_table_set(executor)
    result: dict[str, list[dict[str, Any]]] = {}
    total, observed_bytes = 0, 0
    for table in _TABLES:
        query = await executor.stream(
            text(f"SELECT * FROM {table.name} LIMIT :row_limit"),
            {
                "row_limit": max_rows - total + 1,
            },
        )
        try:
            if set(query.keys()) != set(table.columns):
                raise SubjectHistoryError(
                    f"database columns do not match schema v5: {table.name}"
                )
            result[table.name] = []
            async for row in query.mappings():
                total += 1
                observed_bytes += sum(
                    len(value)
                    if isinstance(value, (str, bytes, bytearray, memoryview))
                    else 8
                    for value in row.values()
                )
                if total > max_rows or observed_bytes > max_bytes:
                    raise SubjectHistoryError(
                        "history resource limit exceeded; snapshot not truncated"
                    )
                result[table.name].append(dict(row))
        finally:
            await query.close()
    return result


async def capture_subject_history(
    runtime: StorageBackendRuntime,
    *,
    max_rows: int = _DEFAULT_MAX_ROWS,
    max_bytes: int = _DEFAULT_MAX_BYTES,
) -> bytes:
    """Capture all subject rows in one SQLite/RR-MySQL read transaction.

    This reads no workspace files or credentials and acquires no writer
    authority. Limits reject oversized snapshots, never silently omit rows.
    """
    _limits(max_rows, max_bytes)
    if runtime._closed or not runtime.enabled or runtime.engine is None:
        raise SubjectHistoryError("history source runtime is not open")
    try:
        async with runtime.engine.connect() as connection:
            if runtime.backend == BackendKind.MYSQL:
                connection = await connection.execution_options(
                    isolation_level="REPEATABLE READ"
                )
            async with connection.begin():
                if runtime.backend == BackendKind.LOCAL:
                    # aiosqlite legacy mode does not BEGIN for SELECT by itself.
                    await connection.exec_driver_sql("BEGIN")
                source_identity = await _database_identity(connection, runtime)
                rows = await _read_tables(
                    connection, max_rows=max_rows, max_bytes=max_bytes
                )
                captured_at = datetime.now(UTC).isoformat(timespec="microseconds")
    except SQLAlchemyError as exc:
        raise SubjectHistoryError(
            f"database rejected history snapshot ({type(exc).__name__})"
        ) from None
    return await asyncio.to_thread(
        encode_subject_history_bundle,
        rows,
        source_identity_sha256=source_identity,
        captured_at=captured_at,
        max_rows=max_rows,
        max_bytes=max_bytes,
    )


def _bind_value(kind: str, value: Any, backend: BackendKind) -> Any:
    if kind == "binary":
        return base64.b64decode(value, validate=True)
    if kind == "json":
        return _canonical(value).decode("utf-8")
    if kind == "outbox_timestamp" and value is None:
        return "" if backend == BackendKind.LOCAL else None
    if (
        kind in {"timestamp", "nullable_timestamp", "outbox_timestamp"}
        and value is not None
        and backend == BackendKind.MYSQL
    ):
        return datetime.fromisoformat(value).astimezone(UTC).replace(tzinfo=None)
    return value


async def _import_rows(
    envelope: dict[str, Any],
    wire: dict[str, list[dict[str, Any]]],
    target: StorageBackendRuntime,
    *,
    max_rows: int,
    max_bytes: int,
) -> SubjectHistoryReport:
    inserted = 0
    async with target.unit_of_work() as uow:
        connection = await uow.session.connection()
        if target.backend == BackendKind.LOCAL:
            await connection.exec_driver_sql("BEGIN IMMEDIATE")
        # Lock the copy lease before inspecting/inserting rows. The runtime
        # checks the same fence again immediately before committing this UoW.
        assert target._write_fence is not None
        await target._write_fence(uow.session)
        await _reject_active_database(connection)
        if (
            await _database_identity(connection, target)
            == envelope["source_identity_sha256"]
        ):
            raise SubjectHistoryError(
                "history import source and target are the same database"
            )
        current = await _read_tables(
            uow.session, max_rows=max_rows, max_bytes=max_bytes
        )
        missing: dict[str, list[dict[str, Any]]] = {}
        for table in _TABLES:
            expected = {row[table.primary_key]: row for row in wire[table.name]}
            present = _wire_rows(table, current[table.name], from_wire=False)
            identities: set[Any] = set()
            for row in present:
                identity = row[table.primary_key]
                if expected.get(identity) != row:
                    raise SubjectHistoryError(
                        f"history target has extra or conflicting rows: {table.name}"
                    )
                identities.add(identity)
            missing[table.name] = [
                row for identity, row in expected.items() if identity not in identities
            ]
        for table in _TABLES:
            names = tuple(table.columns)
            statement = text(
                f"INSERT INTO {table.name} ({', '.join(names)}) "
                f"VALUES ({', '.join(':' + name for name in names)})"
            )
            for offset in range(0, len(missing[table.name]), 100):
                batch = missing[table.name][offset : offset + 100]
                await uow.session.execute(
                    statement,
                    [
                        {
                            name: _bind_value(
                                table.columns[name], row[name], target.backend
                            )
                            for name in names
                        }
                        for row in batch
                    ],
                )
                inserted += len(batch)
        copied = await _read_tables(uow.session, max_rows=max_rows, max_bytes=max_bytes)
        for table in _TABLES:
            actual = _wire_rows(table, copied[table.name], from_wire=False)
            if actual != wire[table.name]:
                raise SubjectHistoryError(
                    f"history target verification mismatch: {table.name}"
                )
    return _report(envelope, replay=inserted == 0)


async def import_subject_history_bundle(
    payload: bytes,
    target: StorageBackendRuntime,
    *,
    max_rows: int = _DEFAULT_MAX_ROWS,
    max_bytes: int = _DEFAULT_MAX_BYTES,
) -> SubjectHistoryReport:
    """Insert an exact bundle only into a fenced, non-active candidate.

    Existing rows must be an exact subset. Extra or conflicting target rows
    abort the entire transaction. No UPDATE/DELETE, append command, schema
    migration or activation occurs. Outbox leases are evidence, never executed.
    """
    if (
        target.writer_role != StorageWriterRole.CANDIDATE_COPY
        or target.authority_token is not None
        or target.authority_registry is not None
        or target.generation is not None
        or target._write_fence is None
        or target._writer_validator is None
    ):
        raise SubjectHistoryError(
            "history import requires isolated CANDIDATE_COPY authority"
        )
    envelope, wire = await asyncio.to_thread(
        _decode,
        payload,
        max_rows=max_rows,
        max_bytes=max_bytes,
    )
    await target.validate_writer()
    try:
        return await _import_rows(
            envelope, wire, target, max_rows=max_rows, max_bytes=max_bytes
        )
    except SQLAlchemyError as exc:
        # Database exceptions may contain bound subject bytes; expose only type.
        raise SubjectHistoryError(
            f"database rejected exact history import ({type(exc).__name__})"
        ) from None


def _write_bundle(directory: Path, payload: bytes) -> None:
    directory.mkdir(mode=0o700, parents=False, exist_ok=False)
    marker = directory / _INCOMPLETE
    marker.touch(mode=0o600, exist_ok=False)
    destination = directory / _BUNDLE_NAME
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    if destination.read_bytes() != payload:
        raise SubjectHistoryError("history bundle write verification failed")
    marker.unlink()


def _read_bundle(directory: Path, max_bytes: int) -> bytes:
    if (
        not directory.is_dir()
        or directory.is_symlink()
        or (directory / _INCOMPLETE).exists()
    ):
        raise SubjectHistoryError(
            "history bundle directory is missing, linked, or incomplete"
        )
    source = directory / _BUNDLE_NAME
    if source.is_symlink() or not source.is_file():
        raise SubjectHistoryError("history bundle file is missing or linked")
    with source.open("rb") as stream:
        payload = stream.read(max_bytes + 1)
    if len(payload) > max_bytes:
        raise SubjectHistoryError("history bundle exceeds byte limit")
    return payload


async def export_subject_history(
    source: StorageBackendRuntime,
    destination_directory: str | Path,
    *,
    max_rows: int = _DEFAULT_MAX_ROWS,
    max_bytes: int = _DEFAULT_MAX_BYTES,
) -> SubjectHistoryReport:
    """Write a new private bundle directory; never overwrite an existing export."""
    payload = await capture_subject_history(
        source, max_rows=max_rows, max_bytes=max_bytes
    )
    await asyncio.to_thread(_write_bundle, Path(destination_directory), payload)
    return await asyncio.to_thread(
        verify_subject_history_bundle,
        payload,
        max_rows=max_rows,
        max_bytes=max_bytes,
    )


async def import_subject_history(
    source_directory: str | Path,
    target: StorageBackendRuntime,
    *,
    max_rows: int = _DEFAULT_MAX_ROWS,
    max_bytes: int = _DEFAULT_MAX_BYTES,
) -> SubjectHistoryReport:
    """Read a completed bundle and perform candidate-only exact import."""
    _limits(max_rows, max_bytes)
    payload = await asyncio.to_thread(_read_bundle, Path(source_directory), max_bytes)
    return await import_subject_history_bundle(
        payload,
        target,
        max_rows=max_rows,
        max_bytes=max_bytes,
    )


__all__ = [
    "SubjectHistoryError",
    "SubjectHistoryReport",
    "capture_subject_history",
    "encode_subject_history_bundle",
    "export_subject_history",
    "import_subject_history",
    "import_subject_history_bundle",
    "verify_subject_history_bundle",
]
