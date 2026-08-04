"""Non-destructive local SQLite/file snapshot creation."""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.kernel.storage.outbox_primitives import canonical_json_sha256

from .manifest import (
    SNAPSHOT_MANIFEST_SCHEMA_VERSION,
    LifeSnapshotError,
    snapshot_manifest_sha256,
)

_SQLITE_SUFFIXES = (
    ".db",
    ".db-shm",
    ".db-wal",
    ".sqlite",
    ".sqlite-shm",
    ".sqlite-wal",
    ".sqlite3",
    ".sqlite3-shm",
    ".sqlite3-wal",
)
_FRONTIER_COLUMNS = (
    "sequence",
    "position",
    "remote_position",
    "event_position",
    "ingest_position",
    "source_sequence",
    "last_sequence",
    "revision",
)


@dataclass(frozen=True, slots=True)
class LifeStorageLayout:
    """Explicit inventory of authoritative/operational files to preserve."""

    sqlite_sources: tuple[Path, ...] = (
        Path("Elysium.db"),
        Path("life_engine_workspace/.memory/memory.db"),
        Path("life_engine_workspace/life_events.sqlite3"),
        Path("life_engine_workspace/runtime/consciousness_presence.sqlite3"),
        Path("life_engine_workspace/runtime/world_projection.sqlite3"),
        Path("life_engine_workspace/.memory/archive_sync_state.sqlite3"),
    )
    exact_roots: tuple[Path, ...] = (
        Path("diaries"),
        Path("life_engine_workspace"),
        Path("media_cache"),
        Path("emoji_sender/memes"),
    )
    excluded_rebuildable_roots: tuple[Path, ...] = (
        Path("chroma_db"),
        Path("emoji_sender/vector_db"),
        Path("life_engine_workspace/.memory/chroma"),
        Path("life_engine_workspace/.memory/chroma.broken-20260802-1010"),
    )
    excluded_preserved_backup_roots: tuple[Path, ...] = (
        Path("life_engine_workspace/.memory/backups"),
    )


def sha256_file(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def _encode_sqlite_value(value: Any) -> list[str]:
    if value is None:
        return ["null", ""]
    if isinstance(value, bool):
        return ["int", "1" if value else "0"]
    if isinstance(value, int):
        return ["int", str(value)]
    if isinstance(value, float):
        if math.isnan(value):
            rendered = "nan"
        elif math.isinf(value):
            rendered = "+inf" if value > 0 else "-inf"
        else:
            rendered = value.hex()
        return ["float", rendered]
    if isinstance(value, str):
        return ["text", value]
    if isinstance(value, (bytes, bytearray, memoryview)):
        return ["blob", base64.b64encode(bytes(value)).decode("ascii")]
    raise LifeSnapshotError(f"unsupported SQLite value type: {type(value).__name__}")


def inspect_sqlite_database(path: Path) -> dict[str, Any]:
    """Return deterministic schema/row roots without changing the database."""

    connection = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
    try:
        quick_check = connection.execute("PRAGMA quick_check").fetchall()
        if quick_check != [("ok",)]:
            raise LifeSnapshotError(f"SQLite quick_check failed: {path}")
        table_rows = connection.execute(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        tables: list[dict[str, Any]] = []
        frontiers: dict[str, int] = {}
        for table_name_raw, schema_sql_raw in table_rows:
            table_name = str(table_name_raw)
            escaped = table_name.replace('"', '""')
            columns = [
                str(row[1])
                for row in connection.execute(f'PRAGMA table_info("{escaped}")')
            ]
            row_hashes: list[str] = []
            cursor = connection.execute(f'SELECT * FROM "{escaped}"')
            while rows := cursor.fetchmany(1000):
                for row in rows:
                    row_hashes.append(
                        canonical_json_sha256(
                            [_encode_sqlite_value(value) for value in row]
                        )
                    )
            row_hashes.sort()
            table_root = canonical_json_sha256(
                {
                    "table": table_name,
                    "columns": columns,
                    "row_count": len(row_hashes),
                    "row_hashes": row_hashes,
                }
            )
            schema_sql = str(schema_sql_raw or "")
            tables.append(
                {
                    "name": table_name,
                    "columns": columns,
                    "row_count": len(row_hashes),
                    "row_root_sha256": table_root,
                    "schema_sha256": hashlib.sha256(
                        schema_sql.encode("utf-8")
                    ).hexdigest(),
                }
            )
            for column in _FRONTIER_COLUMNS:
                if column not in columns:
                    continue
                value = connection.execute(
                    f'SELECT MAX("{column}") FROM "{escaped}"'
                ).fetchone()[0]
                if isinstance(value, int) and value >= 0:
                    frontiers[f"{table_name}.{column}"] = int(value)
        database_root = canonical_json_sha256(
            [
                {
                    "name": table["name"],
                    "row_count": table["row_count"],
                    "row_root_sha256": table["row_root_sha256"],
                    "schema_sha256": table["schema_sha256"],
                }
                for table in tables
            ]
        )
        return {
            "integrity_check": "ok",
            "tables": tables,
            "database_root_sha256": database_root,
            "frontiers": frontiers,
        }
    finally:
        connection.close()


def _stat_identity(path: Path) -> dict[str, int]:
    stat = path.stat()
    return {
        "device": int(stat.st_dev),
        "inode": int(stat.st_ino),
        "bytes": int(stat.st_size),
        "mtime_ns": int(stat.st_mtime_ns),
    }


def _sqlite_source_evidence(source: Path, *, include_hash: bool) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for candidate in (source, Path(f"{source}-wal"), Path(f"{source}-shm")):
        if not candidate.exists():
            continue
        record: dict[str, Any] = {
            "name": candidate.name,
            "stat": _stat_identity(candidate),
        }
        if include_hash:
            record["sha256"] = sha256_file(candidate)
            record["stat_after_hash"] = _stat_identity(candidate)
        records.append(record)
    return records


def _backup_sqlite(
    source: Path,
    destination: Path,
    *,
    writer_frozen: bool,
) -> dict[str, Any]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="elysium-life-snapshot-") as temporary:
        staged = Path(temporary) / source.name
        source_connection = sqlite3.connect(
            f"file:{source.resolve()}?mode=ro",
            uri=True,
        )
        target_connection = sqlite3.connect(staged)
        try:
            quick_check = source_connection.execute("PRAGMA quick_check").fetchall()
            if quick_check != [("ok",)]:
                raise LifeSnapshotError(f"SQLite quick_check failed: {source}")
            evidence_before = _sqlite_source_evidence(
                source,
                include_hash=writer_frozen,
            )
            source_connection.backup(target_connection)
            evidence_after = _sqlite_source_evidence(
                source,
                include_hash=writer_frozen,
            )
            if writer_frozen and evidence_before != evidence_after:
                raise LifeSnapshotError(
                    f"frozen SQLite source changed during backup: {source}"
                )
            journal_mode = target_connection.execute(
                "PRAGMA journal_mode = DELETE"
            ).fetchone()
            if journal_mode is None or str(journal_mode[0]).lower() != "delete":
                raise LifeSnapshotError(
                    f"SQLite backup journal normalization failed: {source}"
                )
            target_connection.commit()
        finally:
            target_connection.close()
            source_connection.close()
        inspection = inspect_sqlite_database(staged)
        staged_sha256 = sha256_file(staged)
        shutil.copy2(staged, destination)
        backup_sha256 = sha256_file(destination)
        if staged_sha256 != backup_sha256:
            raise LifeSnapshotError(f"copied SQLite checksum mismatch: {source}")
    return {
        "source_evidence_before": evidence_before,
        "source_evidence_after": evidence_after,
        "backup_bytes": destination.stat().st_size,
        "backup_sha256": backup_sha256,
        **inspection,
    }


def _is_excluded(relative: Path, layout: LifeStorageLayout) -> bool:
    excluded = (
        *layout.excluded_rebuildable_roots,
        *layout.excluded_preserved_backup_roots,
    )
    return any(relative == root or root in relative.parents for root in excluded)


def _copy_exact_files(
    data_root: Path,
    output: Path,
    layout: LifeStorageLayout,
) -> list[dict[str, Any]]:
    sqlite_sources = set(layout.sqlite_sources)
    records: list[dict[str, Any]] = []
    seen: set[Path] = set()
    for root in layout.exact_roots:
        source_root = data_root / root
        if not source_root.exists():
            continue
        candidates = [source_root] if source_root.is_file() else source_root.rglob("*")
        for source in sorted(path for path in candidates if path.is_file()):
            relative = source.relative_to(data_root)
            if relative in seen or relative in sqlite_sources or _is_excluded(relative, layout):
                continue
            if source.name.endswith(_SQLITE_SUFFIXES):
                continue
            if "__pycache__" in relative.parts or ".git" in relative.parts:
                continue
            seen.add(relative)
            before = _stat_identity(source)
            backup_relative = Path("workspace") / relative
            destination = output / backup_relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            after_copy = _stat_identity(source)
            source_hash = sha256_file(source)
            after_hash = _stat_identity(source)
            if before != after_copy or after_copy != after_hash:
                raise LifeSnapshotError(
                    f"source file changed while copying: {relative.as_posix()}"
                )
            backup_hash = sha256_file(destination)
            if source_hash != backup_hash:
                raise LifeSnapshotError(
                    f"copied file checksum mismatch: {relative.as_posix()}"
                )
            records.append(
                {
                    "source": str(source),
                    "backup": str(destination),
                    "source_relative": relative.as_posix(),
                    "backup_relative": backup_relative.as_posix(),
                    "bytes": destination.stat().st_size,
                    "sha256": backup_hash,
                    "source_stat": after_hash,
                }
            )
    return records


def create_local_snapshot(
    data_root: str | Path,
    output: str | Path,
    *,
    layout: LifeStorageLayout | None = None,
    writer_frozen: bool = False,
) -> dict[str, Any]:
    """Copy local sources into a new directory and write cryptographic evidence.

    The function never deletes, moves or modifies a source.  A live online
    backup is a valid candidate snapshot, but only an explicitly frozen source
    can later become a verified writable generation.
    """

    data_root = Path(data_root).resolve()
    output = Path(output).resolve()
    layout = LifeStorageLayout() if layout is None else layout
    if not data_root.is_dir():
        raise LifeSnapshotError(f"data root does not exist: {data_root}")
    if output.exists():
        raise LifeSnapshotError("snapshot output already exists; refusing overwrite")
    try:
        output.relative_to(data_root)
    except ValueError:
        pass
    else:
        raise LifeSnapshotError("snapshot output must be outside the source data root")
    output.mkdir(parents=True)
    incomplete_marker = output / "SNAPSHOT_INCOMPLETE"
    with incomplete_marker.open("xb") as handle:
        handle.write(b"snapshot creation did not complete\n")
        handle.flush()
        os.fsync(handle.fileno())

    sqlite_records: list[dict[str, Any]] = []
    frontiers: dict[str, int] = {}
    for relative in layout.sqlite_sources:
        source = data_root / relative
        if not source.is_file():
            raise LifeSnapshotError(f"required SQLite source is missing: {relative}")
        backup_relative = Path("sqlite") / relative
        record = _backup_sqlite(
            source,
            output / backup_relative,
            writer_frozen=writer_frozen,
        )
        source_key = relative.as_posix()
        sqlite_records.append(
            {
                "source": str(source),
                "backup": str(output / backup_relative),
                "source_relative": source_key,
                "backup_relative": backup_relative.as_posix(),
                "bytes": int(record["backup_bytes"]),
                "sha256": str(record["backup_sha256"]),
                **record,
            }
        )
        for name, value in dict(record["frontiers"]).items():
            frontiers[f"{source_key}:{name}"] = int(value)

    file_records = _copy_exact_files(data_root, output, layout)
    files_root = canonical_json_sha256(
        [
            {"path": item["source_relative"], "bytes": item["bytes"], "sha256": item["sha256"]}
            for item in file_records
        ]
    )
    root_hashes = {
        f"sqlite:{item['source_relative']}": str(item["database_root_sha256"])
        for item in sqlite_records
    }
    root_hashes["exact_files"] = files_root
    source_snapshot_sha256 = canonical_json_sha256(
        {
            "sqlite": [
                {
                    "source_relative": item["source_relative"],
                    "backup_sha256": item["backup_sha256"],
                    "database_root_sha256": item["database_root_sha256"],
                }
                for item in sqlite_records
            ],
            "exact_files_root_sha256": files_root,
            "writer_frozen": bool(writer_frozen),
        }
    )
    created_at = datetime.now(UTC).isoformat()
    manifest: dict[str, Any] = {
        "schema_version": SNAPSHOT_MANIFEST_SCHEMA_VERSION,
        "created_at": created_at,
        "created_at_utc": created_at,
        "data_root": str(data_root),
        "output": str(output),
        "writer_frozen": bool(writer_frozen),
        "source_snapshot_sha256": source_snapshot_sha256,
        "root_hashes": dict(sorted(root_hashes.items())),
        "frontiers": dict(sorted(frontiers.items())),
        "sqlite": sqlite_records,
        "exact_file_count": len(file_records),
        "exact_files": file_records,
        "workspace_file_count": len(file_records),
        "workspace_files": file_records,
        "excluded_rebuildable_roots": [
            path.as_posix() for path in layout.excluded_rebuildable_roots
        ],
        "excluded_preserved_backup_roots": [
            path.as_posix() for path in layout.excluded_preserved_backup_roots
        ],
        "excluded_rebuildable_projections": [
            path.as_posix() for path in layout.excluded_rebuildable_roots
        ],
    }
    manifest["manifest_sha256"] = snapshot_manifest_sha256(manifest)
    manifest_path = output / "manifest.json"
    encoded = (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )
    with manifest_path.open("xb") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    incomplete_marker.unlink()
    return manifest


__all__ = [
    "LifeStorageLayout",
    "create_local_snapshot",
    "inspect_sqlite_database",
    "sha256_file",
]
