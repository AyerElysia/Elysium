"""Read-only adapters that normalize Elysium's local memory stores.

SQLite files are opened in URI read-only mode and workspace files are checked
before and after each read.  The adapters never update source databases,
checkpoint WAL files, or rewrite subject-authored content.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .models import ArchiveMode, ArchiveRecord, canonical_json


class ArchiveSourceError(RuntimeError):
    """A source changed, was incomplete, or could not be represented exactly."""


@dataclass(frozen=True, slots=True)
class SQLiteSource:
    domain: str
    relative_path: Path
    required: bool = True


@dataclass(frozen=True, slots=True)
class SQLiteTableArchiveContract:
    """Engineering archive behavior for one SQLite table.

    The role is deliberately open text and describes storage/rebuild
    behavior only. It must never be consumed as evidence about truth or
    subjective meaning.
    """

    mode: ArchiveMode
    archive_role: str


DEFAULT_SQLITE_SOURCES = (
    SQLiteSource("core", Path("Elysium.db")),
    SQLiteSource("life_events", Path("life_engine_workspace/life_events.sqlite3")),
    SQLiteSource("life_memory", Path("life_engine_workspace/.memory/memory.db")),
    SQLiteSource(
        "consciousness_presence",
        Path("life_engine_workspace/runtime/consciousness_presence.sqlite3"),
    ),
    SQLiteSource(
        "world_projection",
        Path("life_engine_workspace/runtime/world_projection.sqlite3"),
        required=False,
    ),
)


_IMMUTABLE_TABLES: dict[str, frozenset[str]] = {
    "life_events": frozenset({"raw_life_events", "raw_event_import_issues"}),
    "life_memory": frozenset(
        {
            "memory_artifact_derivations",
            "memory_artifact_versions",
            "memory_beliefs",
            "memory_claim_evidence",
            "memory_claims",
            "memory_corecall_events",
            "memory_corrections",
            "memory_epistemic_conflicts",
            "memory_experience_occurrence_aliases",
            "memory_experiences",
            "memory_interpretation_sources",
            "memory_interpretations",
            "memory_recall_events",
            "memory_recall_sessions",
            "memory_retrieval_episodes",
            "memory_retrieval_feedback",
            "memory_semantic_relations",
            "memory_state_events",
            "memory_witness_migrations",
            "memory_witness_sources",
        }
    ),
}


_PROJECTION_TABLES = frozenset(
    {
        "memory_artifact_heads",
        "memory_association_projection",
        "memory_chunks",
        "memory_edges",
        "memory_index_jobs",
        "memory_index_state",
        "memory_nodes",
        "memory_schema",
        "memory_vector_tombstones",
        "memory_witness_state",
    }
)


_VERSIONED_TABLE_ROLES: dict[tuple[str, str], str] = {
    ("life_events", "raw_event_consumer_offsets"): "consumer_cursor_state",
    ("life_events", "raw_event_store_meta"): "ledger_engineering_state",
    ("life_memory", "memory_retrieval_exposures"): "versioned_retrieval_trace",
    ("life_memory", "memory_witnesses"): "versioned_witness_record",
}


_DECLARED_SUBJECT_WORKSPACE_FILES = frozenset(
    {
        "MEMORY.md",
        "SOUL.md",
        "USER.md",
    }
)
_DECLARED_SUBJECT_WORKSPACE_PREFIXES = ("diaries/",)


_OPERATIONAL_PREFIXES = ("sync_",)
_WORKSPACE_EXCLUDED_PARTS = frozenset({".git", ".memory", "__pycache__"})
_WORKSPACE_EXCLUDED_SUFFIXES = frozenset(
    {".db", ".sqlite", ".sqlite3", ".wal", ".shm", ".pyc"}
)
_INLINE_FILE_LIMIT = 512 * 1024
_FILE_CHUNK_SIZE = 512 * 1024


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def verify_backup_manifest(backup_root: Path) -> dict[str, int]:
    """Verify every declared snapshot and workspace file before migration."""

    root = backup_root.resolve()
    manifest_path = root / "manifest.json"
    if not manifest_path.is_file():
        raise ArchiveSourceError(f"backup manifest does not exist: {manifest_path}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ArchiveSourceError(f"backup manifest is unreadable: {exc}") from exc
    if not isinstance(manifest, dict):
        raise ArchiveSourceError("backup manifest root must be an object")
    recorded_root = Path(str(manifest.get("output", "")))
    groups = {
        "sqlite": manifest.get("sqlite", []),
        "workspace": manifest.get("workspace_files", []),
    }
    verified: dict[str, int] = {}
    for group, raw_entries in groups.items():
        if not isinstance(raw_entries, list):
            raise ArchiveSourceError(f"backup manifest {group} must be a list")
        count = 0
        for entry in raw_entries:
            if not isinstance(entry, dict):
                raise ArchiveSourceError(
                    f"backup manifest {group} entry must be an object"
                )
            recorded_path = Path(str(entry.get("backup", "")))
            try:
                relative = recorded_path.relative_to(recorded_root)
            except ValueError as exc:
                raise ArchiveSourceError(
                    f"backup path escapes recorded root: {recorded_path}"
                ) from exc
            actual = (root / relative).resolve()
            if not actual.is_relative_to(root) or not actual.is_file():
                raise ArchiveSourceError(f"backup file is missing: {actual}")
            if actual.stat().st_size != int(entry.get("bytes", -1)):
                raise ArchiveSourceError(f"backup size mismatch: {actual}")
            expected_hash = str(entry.get("sha256", ""))
            if not expected_hash or _sha256_file(actual) != expected_hash:
                raise ArchiveSourceError(f"backup SHA-256 mismatch: {actual}")
            count += 1
        verified[group] = count
    expected_workspace = int(manifest.get("workspace_file_count", -1))
    if verified["workspace"] != expected_workspace:
        raise ArchiveSourceError("backup workspace file count does not match manifest")
    return verified


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _encode_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        return {"$elysium_type": "float", "value": repr(value)}
    if isinstance(value, bytes):
        return {
            "$elysium_type": "bytes",
            "base64": base64.b64encode(value).decode("ascii"),
        }
    raise ArchiveSourceError(f"unsupported SQLite value type: {type(value).__name__}")


def decode_value(value: Any) -> Any:
    """Reverse the archive's lossless SQLite JSON representation."""

    if not isinstance(value, dict) or "$elysium_type" not in value:
        return value
    value_type = value.get("$elysium_type")
    if value_type == "bytes":
        return base64.b64decode(str(value.get("base64", "")), validate=True)
    if value_type == "float":
        raw = str(value.get("value", ""))
        if raw == "nan":
            return float("nan")
        if raw == "inf":
            return float("inf")
        if raw == "-inf":
            return float("-inf")
    raise ArchiveSourceError(f"unsupported archived value type: {value_type!r}")


def _readonly_connection(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(
        f"file:{path.resolve().as_posix()}?mode=ro",
        uri=True,
        timeout=30,
    )
    connection.row_factory = sqlite3.Row
    return connection


def sqlite_table_archive_contract(
    domain: str,
    table: str,
) -> SQLiteTableArchiveContract:
    """Return a conservative technical contract without inferring meaning."""

    if domain == "core":
        return SQLiteTableArchiveContract(
            mode=ArchiveMode.VERSIONED,
            archive_role="application_storage_snapshot",
        )
    if domain in {"consciousness_presence", "world_projection"}:
        return SQLiteTableArchiveContract(
            mode=ArchiveMode.VERSIONED,
            archive_role="runtime_projection",
        )
    if table in _PROJECTION_TABLES:
        return SQLiteTableArchiveContract(
            mode=ArchiveMode.VERSIONED,
            archive_role="rebuildable_projection",
        )
    if table in _IMMUTABLE_TABLES.get(domain, frozenset()):
        return SQLiteTableArchiveContract(
            mode=ArchiveMode.IMMUTABLE,
            archive_role="immutable_history_replica",
        )
    explicit_role = _VERSIONED_TABLE_ROLES.get((domain, table))
    if explicit_role:
        return SQLiteTableArchiveContract(
            mode=ArchiveMode.VERSIONED,
            archive_role=explicit_role,
        )
    return SQLiteTableArchiveContract(
        mode=ArchiveMode.VERSIONED,
        archive_role="unclassified_storage_record",
    )


def _is_archivable_table(name: str) -> bool:
    if name.startswith("sqlite_") or name.startswith(_OPERATIONAL_PREFIXES):
        return False
    return "_fts" not in name and not name.endswith("_fts")


def _primary_key_columns(
    connection: sqlite3.Connection,
    table: str,
) -> list[str]:
    rows = connection.execute(
        f"PRAGMA table_info({_quote_identifier(table)})"
    ).fetchall()
    return [
        str(row["name"])
        for row in sorted(rows, key=lambda item: int(item["pk"] or 0))
        if int(row["pk"] or 0) > 0
    ]


def _row_recorded_at(row: sqlite3.Row) -> str:
    candidates = (
        "recorded_at",
        "created_at",
        "occurred_at",
        "updated_at",
        "last_seen_at",
        "last_run_at",
    )
    keys = set(row.keys())
    for name in candidates:
        if name in keys and row[name] not in {None, ""}:
            return str(row[name])
    return ""


def _row_visibility(row: sqlite3.Row) -> str:
    if "visibility" not in set(row.keys()):
        return "owner_private"
    return str(row["visibility"] or "owner_private").lower()


def _schema_records(
    connection: sqlite3.Connection,
    *,
    source_node_id: str,
    domain: str,
) -> Iterator[ArchiveRecord]:
    rows = connection.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master "
        "WHERE name NOT LIKE 'sqlite_%' AND sql IS NOT NULL "
        "ORDER BY CASE type WHEN 'table' THEN 0 WHEN 'view' THEN 1 "
        "WHEN 'index' THEN 2 ELSE 3 END, name"
    ).fetchall()
    for ordinal, row in enumerate(rows, start=1):
        name = str(row["name"])
        table_name = str(row["tbl_name"])
        if (
            "_fts" in name
            or "_fts" in table_name
            or not _is_archivable_table(table_name)
        ):
            continue
        object_type = str(row["type"])
        logical_key = canonical_json(
            {"name": name, "table": table_name, "type": object_type}
        )
        yield ArchiveRecord.build(
            source_node_id=source_node_id,
            source_domain=domain,
            record_kind=f"sqlite_schema:{object_type}",
            logical_key=logical_key,
            mode=ArchiveMode.VERSIONED,
            source_sequence=ordinal,
            recorded_at="",
            visibility="owner_private",
            archive_role="engineering_schema",
            payload={
                "name": name,
                "table": table_name,
                "type": object_type,
                "sql": str(row["sql"]),
            },
        )


def iter_sqlite_records(
    path: Path,
    *,
    source_node_id: str,
    domain: str,
    batch_size: int = 1000,
) -> Iterator[ArchiveRecord]:
    """Yield a transactionally stable logical snapshot of one SQLite file."""

    if not path.is_file():
        raise ArchiveSourceError(f"SQLite source does not exist: {path}")
    connection = _readonly_connection(path)
    try:
        quick_check = connection.execute("PRAGMA quick_check").fetchall()
        if [tuple(row) for row in quick_check] != [("ok",)]:
            raise ArchiveSourceError(f"SQLite quick_check failed: {path}")
        connection.execute("BEGIN")
        yield from _schema_records(
            connection,
            source_node_id=source_node_id,
            domain=domain,
        )
        tables = [
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
            if _is_archivable_table(str(row[0]))
        ]
        for table in tables:
            contract = sqlite_table_archive_contract(domain, table)
            pk_columns = _primary_key_columns(connection, table)
            table_sql_row = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
            without_rowid = bool(
                table_sql_row
                and "WITHOUT ROWID" in str(table_sql_row["sql"] or "").upper()
            )
            quoted = _quote_identifier(table)
            if without_rowid:
                select_sql = f"SELECT * FROM {quoted}"
            else:
                select_sql = f"SELECT rowid AS __archive_rowid__, * FROM {quoted}"
            if pk_columns:
                select_sql += " ORDER BY " + ", ".join(
                    _quote_identifier(name) for name in pk_columns
                )
            elif not without_rowid:
                select_sql += " ORDER BY rowid"
            cursor = connection.execute(select_sql)
            ordinal = 0
            while rows := cursor.fetchmany(max(1, int(batch_size))):
                for row in rows:
                    ordinal += 1
                    row_keys = tuple(row.keys())
                    columns = {
                        key: _encode_value(row[key])
                        for key in row_keys
                        if key != "__archive_rowid__"
                    }
                    if pk_columns:
                        identity_values = {name: columns[name] for name in pk_columns}
                    else:
                        identity_values = {
                            "rowid": int(row["__archive_rowid__"])
                            if "__archive_rowid__" in row_keys
                            else ordinal
                        }
                    source_sequence = (
                        int(row["__archive_rowid__"])
                        if "__archive_rowid__" in row_keys
                        else ordinal
                    )
                    yield ArchiveRecord.build(
                        source_node_id=source_node_id,
                        source_domain=domain,
                        record_kind=f"sqlite_row:{table}",
                        logical_key=canonical_json(identity_values),
                        mode=contract.mode,
                        source_sequence=source_sequence,
                        recorded_at=_row_recorded_at(row),
                        visibility=_row_visibility(row),
                        archive_role=contract.archive_role,
                        payload={
                            "table": table,
                            "primary_key": identity_values,
                            "columns": columns,
                        },
                    )
        connection.rollback()
    finally:
        connection.close()


def _workspace_file_allowed(path: Path, workspace_root: Path) -> bool:
    relative = path.relative_to(workspace_root)
    if any(part in _WORKSPACE_EXCLUDED_PARTS for part in relative.parts):
        return False
    lowered = path.name.lower()
    if lowered.startswith("life_events.jsonl"):
        return False
    return not any(lowered.endswith(suffix) for suffix in _WORKSPACE_EXCLUDED_SUFFIXES)


def workspace_file_archive_role(path: Path, data_root: Path) -> str:
    """Classify only explicitly declared subject paths; never guess ownership."""

    root = data_root.resolve()
    resolved = path.resolve()
    external_diaries = root / "diaries"
    try:
        resolved.relative_to(external_diaries)
    except ValueError:
        pass
    else:
        return "declared_subject_artifact_exact_bytes"

    workspace_root = root / "life_engine_workspace"
    try:
        relative = resolved.relative_to(workspace_root).as_posix()
    except ValueError:
        return "unclassified_workspace_exact_bytes"
    if relative in _DECLARED_SUBJECT_WORKSPACE_FILES or relative.startswith(
        _DECLARED_SUBJECT_WORKSPACE_PREFIXES
    ):
        return "declared_subject_artifact_exact_bytes"
    return "unclassified_workspace_exact_bytes"


def iter_workspace_records(
    data_root: Path,
    *,
    source_node_id: str,
) -> Iterator[ArchiveRecord]:
    """Archive workspace bytes without inferring authorship from location."""

    workspace_root = data_root / "life_engine_workspace"
    roots = [workspace_root, data_root / "diaries"]
    for root in roots:
        if not root.exists():
            continue
        for path in sorted(item for item in root.rglob("*") if item.is_file()):
            if root == workspace_root and not _workspace_file_allowed(
                path, workspace_root
            ):
                continue
            before = path.stat()
            content = path.read_bytes()
            after = path.stat()
            if (before.st_size, before.st_mtime_ns) != (
                after.st_size,
                after.st_mtime_ns,
            ):
                raise ArchiveSourceError(
                    f"workspace file changed while archiving: {path}"
                )
            logical_path = path.relative_to(data_root).as_posix()
            archive_role = workspace_file_archive_role(path, data_root)
            file_hash = hashlib.sha256(content).hexdigest()
            chunk_ids: list[str] = []
            inline_data = ""
            if len(content) <= _INLINE_FILE_LIMIT:
                inline_data = base64.b64encode(content).decode("ascii")
            else:
                for index in range(0, len(content), _FILE_CHUNK_SIZE):
                    chunk = content[index : index + _FILE_CHUNK_SIZE]
                    chunk_hash = hashlib.sha256(chunk).hexdigest()
                    chunk_key = canonical_json(
                        {
                            "path": logical_path,
                            "file_hash": file_hash,
                            "index": index // _FILE_CHUNK_SIZE,
                        }
                    )
                    chunk_record = ArchiveRecord.build(
                        source_node_id=source_node_id,
                        source_domain="workspace",
                        record_kind="workspace_file_chunk",
                        logical_key=chunk_key,
                        mode=ArchiveMode.IMMUTABLE,
                        source_sequence=index // _FILE_CHUNK_SIZE,
                        recorded_at=datetime.fromtimestamp(
                            before.st_mtime, tz=UTC
                        ).isoformat(),
                        visibility="owner_private",
                        archive_role=archive_role,
                        payload={
                            "path": logical_path,
                            "file_hash": file_hash,
                            "chunk_hash": chunk_hash,
                            "index": index // _FILE_CHUNK_SIZE,
                            "base64": base64.b64encode(chunk).decode("ascii"),
                        },
                    )
                    chunk_ids.append(chunk_record.record_id)
                    yield chunk_record
            yield ArchiveRecord.build(
                source_node_id=source_node_id,
                source_domain="workspace",
                record_kind="workspace_file",
                logical_key=logical_path,
                mode=ArchiveMode.VERSIONED,
                source_sequence=0,
                recorded_at=datetime.fromtimestamp(before.st_mtime, tz=UTC).isoformat(),
                visibility="owner_private",
                archive_role=archive_role,
                payload={
                    "path": logical_path,
                    "bytes": len(content),
                    "mode": before.st_mode,
                    "mtime_ns": before.st_mtime_ns,
                    "sha256": file_hash,
                    "inline_base64": inline_data,
                    "chunk_record_ids": chunk_ids,
                },
            )


def iter_data_root_records(
    data_root: Path,
    *,
    source_node_id: str,
    batch_size: int = 1000,
) -> Iterator[ArchiveRecord]:
    """Yield all configured SQLite and workspace domains from one data root."""

    root = data_root.resolve()
    for source in DEFAULT_SQLITE_SOURCES:
        path = root / source.relative_path
        if not path.is_file():
            if source.required:
                raise ArchiveSourceError(f"required archive source missing: {path}")
            continue
        yield from iter_sqlite_records(
            path,
            source_node_id=source_node_id,
            domain=source.domain,
            batch_size=batch_size,
        )
    yield from iter_workspace_records(root, source_node_id=source_node_id)
