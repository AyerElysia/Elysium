"""Lossless Presence and World copy from one immutable SQLite snapshot.

The operational stores are small, but they carry concurrency state that cannot
be reconstructed safely from the Life Event ledger alone: Presence revisions,
published lifecycle outbox rows, World projector metadata, and per-instance
delivery cursors are copied as first-class evidence.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from src.kernel.storage import canonical_json

from ..contracts import StorageBackendRuntime, StorageWriterRole
from .copy_authority import CopyAuthorityToken, MySQLCopyAuthorityRegistry
from .manifest import LifeSnapshotError, load_snapshot_manifest
from .snapshot import sha256_file

_PRESENCE_SOURCE_RELATIVE = (
    "life_engine_workspace/runtime/consciousness_presence.sqlite3"
)
_WORLD_SOURCE_RELATIVE = "life_engine_workspace/runtime/world_projection.sqlite3"


class PresenceWorldCopyError(RuntimeError):
    """Raised when source evidence or target equivalence cannot be proven."""


@dataclass(frozen=True, slots=True)
class DomainTableSpec:
    """Explicit source/target mapping for one Presence or World table."""

    database: str
    name: str
    source_columns: tuple[str, ...]
    target_columns: tuple[str, ...]
    key_columns: tuple[str, ...]
    json_columns: frozenset[str] = frozenset()
    datetime_columns: frozenset[str] = frozenset()
    integer_columns: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class DomainTableCopyReport:
    table_name: str
    row_count: int
    source_root_sha256: str
    target_root_sha256: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class PresenceWorldCopyReport:
    presence_source_path: str
    world_source_path: str
    table_count: int
    copied_count: int
    generated_contract_metadata_count: int
    source_root_sha256: str
    target_root_sha256: str
    tables: tuple[DomainTableCopyReport, ...]
    verified: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "tables": [item.to_dict() for item in self.tables],
        }


_SPECS = (
    DomainTableSpec(
        "presence",
        "consciousness_presence",
        (
            "instance_id",
            "kind",
            "display_name",
            "status",
            "created_at",
            "last_active_at",
            "suspended_at",
            "stream_ids_json",
            "perception_filter_json",
            "metadata_json",
            "session_id",
            "process_epoch",
            "lease_expires_at",
            "lease_duration_seconds",
            "revision",
            "updated_at",
        ),
        (
            "instance_id",
            "kind",
            "display_name",
            "status",
            "created_at",
            "last_active_at",
            "suspended_at",
            "stream_ids_json",
            "perception_filter_json",
            "metadata_json",
            "session_id",
            "process_epoch",
            "lease_expires_at",
            "lease_duration_seconds",
            "revision",
            "updated_at",
        ),
        ("instance_id",),
        frozenset(
            {"stream_ids_json", "perception_filter_json", "metadata_json"}
        ),
        frozenset(
            {
                "created_at",
                "last_active_at",
                "suspended_at",
                "lease_expires_at",
                "updated_at",
            }
        ),
        frozenset({"lease_duration_seconds", "revision"}),
    ),
    DomainTableSpec(
        "presence",
        "consciousness_stream_owners",
        ("stream_id", "instance_id", "claimed_at"),
        ("stream_id", "instance_id", "claimed_at"),
        ("stream_id",),
        datetime_columns=frozenset({"claimed_at"}),
    ),
    DomainTableSpec(
        "presence",
        "consciousness_presence_outbox",
        (
            "outbox_id",
            "occurrence_id",
            "event_type",
            "instance_id",
            "stream_id",
            "occurred_at",
            "payload_json",
            "published_at",
        ),
        (
            "outbox_id",
            "occurrence_id",
            "event_type",
            "instance_id",
            "stream_id",
            "occurred_at",
            "payload_json",
            "published_at",
        ),
        ("outbox_id",),
        json_columns=frozenset({"payload_json"}),
        datetime_columns=frozenset({"occurred_at", "published_at"}),
        integer_columns=frozenset({"outbox_id"}),
    ),
    DomainTableSpec(
        "world",
        "world_projection_meta",
        ("key", "value", "updated_at"),
        ("meta_key", "meta_value", "updated_at"),
        ("meta_key",),
        datetime_columns=frozenset({"updated_at"}),
    ),
    DomainTableSpec(
        "world",
        "world_assertions",
        (
            "assertion_id",
            "subject",
            "predicate",
            "value_json",
            "domain",
            "status",
            "source_instance_id",
            "source_event_id",
            "occurrence_id",
            "observed_at",
            "valid_from",
            "valid_to",
            "recorded_at",
            "supersedes_assertion_id",
            "retracted_at",
            "retracted_by_assertion_id",
            "payload_json",
        ),
        (
            "assertion_id",
            "subject",
            "predicate",
            "value_json",
            "domain",
            "status",
            "source_instance_id",
            "source_event_id",
            "occurrence_id",
            "observed_at",
            "valid_from",
            "valid_to",
            "recorded_at",
            "supersedes_assertion_id",
            "retracted_at",
            "retracted_by_assertion_id",
            "payload_json",
        ),
        ("assertion_id",),
        json_columns=frozenset({"value_json", "payload_json"}),
        datetime_columns=frozenset(
            {
                "observed_at",
                "valid_from",
                "valid_to",
                "recorded_at",
                "retracted_at",
            }
        ),
    ),
    DomainTableSpec(
        "world",
        "world_projection_changes",
        (
            "ingest_position",
            "event_id",
            "occurrence_id",
            "event_type",
            "change_kind",
            "source_instance_id",
            "stream_id",
            "occurred_at",
            "recorded_at",
            "payload_json",
        ),
        (
            "ingest_position",
            "event_id",
            "occurrence_id",
            "event_type",
            "change_kind",
            "source_instance_id",
            "stream_id",
            "occurred_at",
            "recorded_at",
            "payload_json",
        ),
        ("ingest_position",),
        json_columns=frozenset({"payload_json"}),
        datetime_columns=frozenset({"occurred_at", "recorded_at"}),
        integer_columns=frozenset({"ingest_position"}),
    ),
    DomainTableSpec(
        "world",
        "world_perception_cursors",
        ("instance_id", "ingest_position", "revision", "updated_at"),
        ("instance_id", "ingest_position", "revision", "updated_at"),
        ("instance_id",),
        datetime_columns=frozenset({"updated_at"}),
        integer_columns=frozenset({"ingest_position", "revision"}),
    ),
)

TABLE_SPECS = {item.name: item for item in _SPECS}
TABLE_ORDER = tuple(item.name for item in _SPECS)


def _datetime_text(value: Any) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value))
        except ValueError as exc:
            raise PresenceWorldCopyError(
                f"invalid Presence/World timestamp: {value!r}"
            ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat()


def _json_text(value: Any) -> str:
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8")
    try:
        decoded = json.loads(value) if isinstance(value, str) else value
    except json.JSONDecodeError as exc:
        raise PresenceWorldCopyError("malformed Presence/World JSON") from exc
    return canonical_json(decoded)


def normalize_domain_row(
    spec: DomainTableSpec,
    row: Any,
    *,
    source: bool,
) -> dict[str, Any]:
    """Map one SQLite or MySQL row into a stable backend-neutral shape."""

    raw = dict(row)
    values: dict[str, Any] = {}
    for source_column, target_column in zip(
        spec.source_columns,
        spec.target_columns,
        strict=True,
    ):
        column = source_column if source else target_column
        value = raw[column]
        if target_column in spec.json_columns:
            value = _json_text(value)
        elif target_column in spec.datetime_columns:
            value = _datetime_text(value)
        elif target_column in spec.integer_columns and value is not None:
            value = int(value)
        elif isinstance(value, (bytes, bytearray)):
            value = value.decode("utf-8")
        values[target_column] = value
    return values


def _row_digest(spec: DomainTableSpec, row: dict[str, Any]) -> bytes:
    return hashlib.sha256(
        canonical_json(
            {
                "table": spec.name,
                "row": {column: row[column] for column in spec.target_columns},
            }
        ).encode("utf-8")
    ).digest()


def _table_root(spec: DomainTableSpec, rows: list[dict[str, Any]]) -> str:
    ordered = sorted(rows, key=lambda item: tuple(item[key] for key in spec.key_columns))
    digest = hashlib.sha256()
    for row in ordered:
        digest.update(_row_digest(spec, row))
    return digest.hexdigest()


def aggregate_domain_root(reports: list[dict[str, Any]]) -> str:
    """Combine ordered table roots into one stable domain root."""

    digest = hashlib.sha256()
    by_name = {str(item["table_name"]): item for item in reports}
    for table in TABLE_ORDER:
        item = by_name[table]
        digest.update(table.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(int(item["row_count"])).encode("ascii"))
        digest.update(b"\0")
        digest.update(str(item["root_sha256"]).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _declared_source(
    snapshot: Path,
    manifest: dict[str, Any],
    source_relative: str,
) -> tuple[Path, dict[str, Any]]:
    entries = [
        item
        for item in list(manifest.get("sqlite") or [])
        if isinstance(item, dict)
        and str(item.get("source_relative") or "") == source_relative
    ]
    if len(entries) != 1:
        raise PresenceWorldCopyError(
            f"snapshot has no unique database for {source_relative}"
        )
    entry = entries[0]
    source = (snapshot / str(entry.get("backup_relative") or "")).resolve()
    try:
        source.relative_to(snapshot)
    except ValueError as exc:
        raise PresenceWorldCopyError("snapshot database path escapes root") from exc
    expected_hash = str(entry.get("backup_sha256") or entry.get("sha256") or "")
    expected_bytes = int(entry.get("backup_bytes") or entry.get("bytes") or -1)
    if not source.is_file() or len(expected_hash) != 64:
        raise PresenceWorldCopyError("snapshot database is incomplete")
    if source.stat().st_size != expected_bytes or sha256_file(source) != expected_hash:
        raise PresenceWorldCopyError("snapshot database changed after manifest")
    return source, entry


def _open_readonly(path: Path) -> sqlite3.Connection:
    database = sqlite3.connect(
        f"file:{path.as_posix()}?mode=ro",
        uri=True,
        timeout=30.0,
    )
    database.row_factory = sqlite3.Row
    database.execute("PRAGMA query_only = ON")
    integrity = database.execute("PRAGMA integrity_check").fetchone()
    if integrity is None or str(integrity[0]).lower() != "ok":
        database.close()
        raise PresenceWorldCopyError(f"SQLite integrity_check failed: {path.name}")
    return database


def open_presence_world_sources(
    snapshot_directory: str | Path,
) -> tuple[sqlite3.Connection, sqlite3.Connection, dict[str, Any]]:
    """Open the two declared immutable source databases read-only."""

    snapshot = Path(snapshot_directory).resolve()
    try:
        manifest = load_snapshot_manifest(snapshot / "manifest.json")
    except LifeSnapshotError as exc:
        raise PresenceWorldCopyError(str(exc)) from exc
    if (snapshot / "SNAPSHOT_INCOMPLETE").exists():
        raise PresenceWorldCopyError("Presence/World snapshot is marked incomplete")
    presence_path, presence_entry = _declared_source(
        snapshot,
        manifest,
        _PRESENCE_SOURCE_RELATIVE,
    )
    world_path, world_entry = _declared_source(
        snapshot,
        manifest,
        _WORLD_SOURCE_RELATIVE,
    )
    presence = _open_readonly(presence_path)
    try:
        world = _open_readonly(world_path)
    except Exception:
        presence.close()
        raise
    return presence, world, {
        "manifest": manifest,
        "presence_entry": presence_entry,
        "world_entry": world_entry,
        "presence_path": presence_path,
        "world_path": world_path,
    }


def sqlite_domain_rows(
    presence: sqlite3.Connection,
    world: sqlite3.Connection,
) -> dict[str, list[dict[str, Any]]]:
    """Read all explicit source rows in stable identity order."""

    databases = {"presence": presence, "world": world}
    result: dict[str, list[dict[str, Any]]] = {}
    for spec in _SPECS:
        order = ", ".join(
            spec.source_columns[spec.target_columns.index(key)]
            for key in spec.key_columns
        )
        selected = ", ".join(spec.source_columns)
        rows = databases[spec.database].execute(
            f"SELECT {selected} FROM {spec.name} ORDER BY {order}"
        ).fetchall()
        result[spec.name] = [
            normalize_domain_row(spec, row, source=True) for row in rows
        ]
    _validate_cross_table(result)
    return result


def _validate_cross_table(rows: dict[str, list[dict[str, Any]]]) -> None:
    presence = {
        str(item["instance_id"]): item for item in rows["consciousness_presence"]
    }
    for owner in rows["consciousness_stream_owners"]:
        instance = presence.get(str(owner["instance_id"]))
        if instance is None:
            raise PresenceWorldCopyError("stream owner references missing Presence")
        streams = json.loads(str(instance["stream_ids_json"]))
        if instance["status"] != "active" or owner["stream_id"] not in streams:
            raise PresenceWorldCopyError("stream owner contradicts Presence snapshot")

    meta = {
        str(item["meta_key"]): str(item["meta_value"])
        for item in rows["world_projection_meta"]
    }
    try:
        frontier = int(meta["as_of_ingest_position"])
    except (KeyError, ValueError) as exc:
        raise PresenceWorldCopyError("World snapshot has no valid frontier") from exc
    changes = rows["world_projection_changes"]
    if changes and int(changes[-1]["ingest_position"]) > frontier:
        raise PresenceWorldCopyError("World change exceeds projector frontier")
    for cursor in rows["world_perception_cursors"]:
        if int(cursor["ingest_position"]) > frontier:
            raise PresenceWorldCopyError(
                f"World cursor exceeds projector frontier: {cursor['instance_id']}"
            )


def domain_reports(
    rows: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Return count/root evidence for every explicit table."""

    return [
        {
            "table_name": spec.name,
            "row_count": len(rows[spec.name]),
            "root_sha256": _table_root(spec, rows[spec.name]),
        }
        for spec in _SPECS
    ]


def _mysql_bindings(spec: DomainTableSpec, row: dict[str, Any]) -> dict[str, Any]:
    values = dict(row)
    for column in spec.datetime_columns:
        value = str(values[column] or "")
        values[column] = (
            datetime.fromisoformat(value).astimezone(UTC).replace(tzinfo=None)
            if value
            else None
        )
    return values


async def mysql_domain_rows(
    engine: AsyncEngine,
    *,
    source_world_meta_keys: set[str],
) -> dict[str, list[dict[str, Any]]]:
    """Read the MySQL candidate restricted to source-compatible evidence."""

    result: dict[str, list[dict[str, Any]]] = {}
    async with engine.connect() as connection:
        for spec in _SPECS:
            selected = ", ".join(spec.target_columns)
            order = ", ".join(spec.key_columns)
            statement = f"SELECT {selected} FROM {spec.name}"
            params: dict[str, Any] = {}
            if spec.name == "world_projection_meta":
                keys = sorted(source_world_meta_keys)
                if not keys:
                    raise PresenceWorldCopyError("source World metadata is empty")
                marks = ", ".join(f":meta_{index}" for index in range(len(keys)))
                statement += f" WHERE meta_key IN ({marks})"
                params = {f"meta_{index}": key for index, key in enumerate(keys)}
            statement += f" ORDER BY {order}"
            query = await connection.execute(text(statement), params)
            result[spec.name] = [
                normalize_domain_row(spec, row, source=False)
                for row in query.mappings()
            ]
    _validate_cross_table(result)
    return result


async def _insert_missing_rows(
    runtime: StorageBackendRuntime,
    source_rows: dict[str, list[dict[str, Any]]],
    target_rows: dict[str, list[dict[str, Any]]],
) -> int:
    target_by_table = {
        spec.name: {
            tuple(row[key] for key in spec.key_columns): row
            for row in target_rows[spec.name]
        }
        for spec in _SPECS
    }
    missing: dict[str, list[dict[str, Any]]] = {}
    metadata_repairs: list[dict[str, Any]] = []
    virgin_world_projection = not any(
        target_rows[name]
        for name in (
            "world_assertions",
            "world_projection_changes",
            "world_perception_cursors",
        )
    )
    for spec in _SPECS:
        missing[spec.name] = []
        existing = target_by_table[spec.name]
        for row in source_rows[spec.name]:
            identity = tuple(row[key] for key in spec.key_columns)
            target = existing.get(identity)
            if target is None:
                missing[spec.name].append(row)
            elif target != row:
                if (
                    spec.name == "world_projection_meta"
                    and identity == ("as_of_ingest_position",)
                    and str(target["meta_value"]) == "0"
                    and virgin_world_projection
                ):
                    # initialize_contract() creates a fenced, synthetic zero
                    # frontier before a first snapshot copy.  It is safe to
                    # replace only that virgin default; any materialized World
                    # row turns the same mismatch into a hard conflict.
                    metadata_repairs.append(row)
                    continue
                raise PresenceWorldCopyError(
                    f"candidate identity has different evidence: {spec.name}:{identity}"
                )

    copied = 0
    async with runtime.unit_of_work() as uow:
        for row in metadata_repairs:
            updated = await uow.session.execute(
                text(
                    "UPDATE world_projection_meta SET meta_value = :meta_value, "
                    "updated_at = :updated_at WHERE meta_key = :meta_key "
                    "AND meta_value = '0'"
                ),
                _mysql_bindings(TABLE_SPECS["world_projection_meta"], row),
            )
            if updated.rowcount != 1:
                raise PresenceWorldCopyError(
                    "virgin World frontier changed during candidate copy"
                )
        for spec in _SPECS:
            rows = missing[spec.name]
            if not rows:
                continue
            columns = ", ".join(spec.target_columns)
            marks = ", ".join(f":{column}" for column in spec.target_columns)
            await uow.session.execute(
                text(f"INSERT INTO {spec.name} ({columns}) VALUES ({marks})"),
                [_mysql_bindings(spec, row) for row in rows],
            )
            copied += len(rows)
    return copied


async def copy_presence_world_from_snapshot(
    snapshot_directory: str | Path,
    runtime: StorageBackendRuntime,
    *,
    copy_registry: MySQLCopyAuthorityRegistry,
    token: CopyAuthorityToken,
) -> PresenceWorldCopyReport:
    """Copy all explicit operational state and prove source/target parity."""

    if runtime.writer_role != StorageWriterRole.CANDIDATE_COPY:
        raise PresenceWorldCopyError("Presence/World copy requires candidate authority")
    presence, world, evidence = open_presence_world_sources(snapshot_directory)
    try:
        source_rows = sqlite_domain_rows(presence, world)
    finally:
        presence.close()
        world.close()
    source_meta_keys = {
        str(row["meta_key"]) for row in source_rows["world_projection_meta"]
    }
    if runtime.engine is None:
        raise PresenceWorldCopyError("Presence/World copy runtime has no engine")
    target_before = await mysql_domain_rows(
        runtime.engine,
        source_world_meta_keys=source_meta_keys,
    )
    try:
        copied = await _insert_missing_rows(runtime, source_rows, target_before)
    except PresenceWorldCopyError as exc:
        await copy_registry.record_conflict(
            token,
            domain_name="presence_world",
            source_identity="candidate",
            expected_hash=aggregate_domain_root(domain_reports(source_rows)),
            actual_hash=aggregate_domain_root(domain_reports(target_before)),
            detail=str(exc),
        )
        raise
    await copy_registry.set_progress(
        token,
        copied_records=sum(len(value) for value in source_rows.values()),
    )
    target_rows = await mysql_domain_rows(
        runtime.engine,
        source_world_meta_keys=source_meta_keys,
    )
    source_reports = domain_reports(source_rows)
    target_reports = domain_reports(target_rows)
    target_by_name = {item["table_name"]: item for item in target_reports}
    table_reports = tuple(
        DomainTableCopyReport(
            table_name=str(item["table_name"]),
            row_count=int(item["row_count"]),
            source_root_sha256=str(item["root_sha256"]),
            target_root_sha256=str(
                target_by_name[str(item["table_name"])]["root_sha256"]
            ),
        )
        for item in source_reports
    )
    source_root = aggregate_domain_root(source_reports)
    target_root = aggregate_domain_root(target_reports)
    if source_reports != target_reports or source_root != target_root:
        raise PresenceWorldCopyError("Presence/World candidate root mismatch")

    async with runtime.engine.connect() as connection:
        total_meta = int(
            (
                await connection.execute(
                    text("SELECT COUNT(*) FROM world_projection_meta")
                )
            ).scalar_one()
        )
    generated = total_meta - len(source_rows["world_projection_meta"])
    if generated < 0:
        raise PresenceWorldCopyError("World contract metadata count regressed")
    return PresenceWorldCopyReport(
        presence_source_path=str(evidence["presence_path"]),
        world_source_path=str(evidence["world_path"]),
        table_count=len(_SPECS),
        copied_count=copied,
        generated_contract_metadata_count=generated,
        source_root_sha256=source_root,
        target_root_sha256=target_root,
        tables=table_reports,
        verified=True,
    )


__all__ = [
    "TABLE_ORDER",
    "TABLE_SPECS",
    "DomainTableSpec",
    "PresenceWorldCopyError",
    "PresenceWorldCopyReport",
    "aggregate_domain_root",
    "copy_presence_world_from_snapshot",
    "domain_reports",
    "mysql_domain_rows",
    "normalize_domain_row",
    "open_presence_world_sources",
    "sqlite_domain_rows",
]
