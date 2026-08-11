"""Source-compatible reverse export for a MySQL Life Memory candidate."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import text

from src.kernel.storage import canonical_json

from ..contracts import StorageBackendRuntime
from .memory_copy import (
    _SPECS,
    _row_digest,
    _source_context,
    _table_root,
    _target_table_root,
    iter_transformed_source_rows,
    normalize_target_row,
    open_memory_source,
)
from .snapshot import sha256_file

_FTS_TABLES = (
    "memory_fts",
    "memory_chunks_fts",
    "memory_witness_fts",
    "memory_claim_fts",
    "memory_interpretation_fts",
)

_LATEST_SOURCE_DDL = {
    "memory_index_jobs": """CREATE TABLE memory_index_jobs (
        job_id TEXT NOT NULL,
        node_id TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0,
        error TEXT NOT NULL DEFAULT '',
        index_revision INTEGER NOT NULL DEFAULT 0,
        claim_token TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (job_id, index_revision),
        UNIQUE (node_id, index_revision),
        FOREIGN KEY (node_id) REFERENCES memory_nodes(node_id) ON DELETE CASCADE
    )""",
    "memory_vector_tombstones": """CREATE TABLE memory_vector_tombstones (
        tombstone_id INTEGER PRIMARY KEY AUTOINCREMENT,
        node_id TEXT NOT NULL,
        chunk_id TEXT NOT NULL,
        collection_name TEXT NOT NULL DEFAULT '',
        created_at REAL NOT NULL,
        consumed_at REAL,
        force_delete INTEGER NOT NULL DEFAULT 0
    )""",
}


class MemoryExportError(RuntimeError):
    """Raised when a reverse export cannot prove domain equivalence."""


@dataclass(frozen=True, slots=True)
class MemoryExportReport:
    destination_directory: str
    database_path: str
    table_count: int
    row_count: int
    fts_row_count: int
    root_sha256: str
    manifest_sha256: str
    verified: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _create_source_schema(
    template: sqlite3.Connection,
    destination: sqlite3.Connection,
) -> dict[str, tuple[str, ...]]:
    columns: dict[str, tuple[str, ...]] = {}
    for spec in _SPECS:
        if spec.name in _LATEST_SOURCE_DDL:
            destination.execute(_LATEST_SOURCE_DDL[spec.name])
            columns[spec.name] = tuple(spec.columns)
            continue
        schema_row = template.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
            (spec.name,),
        ).fetchone()
        if schema_row is None or not str(schema_row[0] or "").strip():
            raise MemoryExportError(f"source schema missing table: {spec.name}")
        destination.execute(str(schema_row[0]))
        columns[spec.name] = tuple(
            str(row["name"])
            for row in template.execute(f"PRAGMA table_info({spec.name})")
        )
    for table in _FTS_TABLES:
        schema_row = template.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        if schema_row is None or not str(schema_row[0] or "").strip():
            raise MemoryExportError(f"source schema missing FTS table: {table}")
        destination.execute(str(schema_row[0]))
    destination.commit()
    return columns


def _source_row(
    table: str,
    target: dict[str, Any],
    source_columns: tuple[str, ...],
) -> tuple[Any, ...]:
    values: dict[str, Any] = dict(target)
    if table == "memory_schema":
        metadata = json.loads(str(target["metadata_json"]))
        values["tokenizer"] = str(metadata.get("legacy_tokenizer") or "")
    if table == "memory_witnesses" and values.get("projection_path") is None:
        values["projection_path"] = ""
    missing = [column for column in source_columns if column not in values]
    if missing:
        raise MemoryExportError(
            f"reverse mapping missing {table} columns: {', '.join(missing)}"
        )
    return tuple(values[column] for column in source_columns)


async def _export_explicit_tables(
    runtime: StorageBackendRuntime,
    destination: sqlite3.Connection,
    source_columns: dict[str, tuple[str, ...]],
    *,
    batch_size: int,
) -> int:
    if runtime.engine is None:
        raise MemoryExportError("Memory export runtime has no engine")
    total = 0
    for spec in _SPECS:
        columns = source_columns[spec.name]
        marks = ", ".join("?" for _ in columns)
        insert = (
            f"INSERT INTO {spec.name} ({', '.join(columns)}) VALUES ({marks})"
        )
        target_columns = ", ".join(
            "`signal`" if column == "signal" else column
            for column in spec.columns
        )
        order = ", ".join(spec.key_columns)
        pending: list[tuple[Any, ...]] = []
        async with runtime.engine.connect() as connection:
            result = await connection.stream(
                text(
                    f"SELECT {target_columns} FROM {spec.name} ORDER BY {order}"
                )
            )
            async for row in result.mappings():
                target = normalize_target_row(spec, row)
                pending.append(_source_row(spec.name, target, columns))
                if len(pending) >= int(batch_size):
                    destination.executemany(insert, pending)
                    destination.commit()
                    total += len(pending)
                    pending.clear()
        if pending:
            destination.executemany(insert, pending)
            destination.commit()
            total += len(pending)
    return total


async def _rebuild_fts(
    runtime: StorageBackendRuntime,
    destination: sqlite3.Connection,
    *,
    batch_size: int,
) -> int:
    if runtime.engine is None:
        raise MemoryExportError("Memory export runtime has no engine")
    queries = (
        (
            "memory_fts",
            (
                "SELECT node_id, title, document_content AS content "
                "FROM memory_nodes WHERE legacy_fts_present = TRUE ORDER BY node_id"
            ),
            ("node_id", "title", "content"),
        ),
        (
            "memory_chunks_fts",
            (
                "SELECT chunk_id, node_id, content, title "
                "FROM memory_chunks ORDER BY chunk_id"
            ),
            ("chunk_id", "node_id", "content", "title"),
        ),
        (
            "memory_witness_fts",
            "SELECT witness_id, content FROM memory_witnesses ORDER BY witness_id",
            ("witness_id", "content"),
        ),
        (
            "memory_claim_fts",
            "SELECT claim_id, subject_key, content FROM memory_claims ORDER BY claim_id",
            ("claim_id", "subject_key", "content"),
        ),
        (
            "memory_interpretation_fts",
            (
                "SELECT interpretation_id, subject_id, content "
                "FROM memory_interpretations ORDER BY interpretation_id"
            ),
            ("interpretation_id", "subject_id", "content"),
        ),
    )
    total = 0
    for table, query, columns in queries:
        marks = ", ".join("?" for _ in columns)
        insert = f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({marks})"
        pending: list[tuple[Any, ...]] = []
        async with runtime.engine.connect() as connection:
            result = await connection.stream(text(query))
            async for row in result.mappings():
                pending.append(tuple(row[column] for column in columns))
                if len(pending) >= int(batch_size):
                    destination.executemany(insert, pending)
                    destination.commit()
                    total += len(pending)
                    pending.clear()
        if pending:
            destination.executemany(insert, pending)
            destination.commit()
            total += len(pending)
    return total


async def _verify_export(
    runtime: StorageBackendRuntime,
    database: Path,
    *,
    batch_size: int,
) -> tuple[str, int, list[dict[str, Any]]]:
    exported = sqlite3.connect(
        f"file:{database.as_posix()}?mode=ro",
        uri=True,
        timeout=30.0,
    )
    exported.row_factory = sqlite3.Row
    exported.execute("PRAGMA query_only = ON")
    integrity = exported.execute("PRAGMA integrity_check").fetchone()
    if integrity is None or str(integrity[0]).lower() != "ok":
        exported.close()
        raise MemoryExportError("exported Memory SQLite failed integrity_check")
    context = _source_context(exported)
    global_root = hashlib.sha256()
    table_reports: list[dict[str, Any]] = []
    total = 0
    try:
        for spec in _SPECS:
            row_digests: list[bytes] = []
            count = 0
            for batch in iter_transformed_source_rows(
                exported,
                spec,
                context,
                batch_size=int(batch_size),
            ):
                row_digests.extend(_row_digest(spec.name, row) for row in batch)
                count += len(batch)
            export_root = _table_root(row_digests)
            target_count, target_root = await _target_table_root(runtime, spec)
            if count != target_count or export_root != target_root:
                raise MemoryExportError(
                    f"reverse export differs from target: {spec.name}"
                )
            table_reports.append(
                {
                    "table_name": spec.name,
                    "row_count": count,
                    "root_sha256": export_root,
                }
            )
            total += count
            global_root.update(spec.name.encode())
            global_root.update(b"\0")
            global_root.update(str(count).encode())
            global_root.update(b"\0")
            global_root.update(export_root.encode())
            global_root.update(b"\n")
    finally:
        exported.close()
    return global_root.hexdigest(), total, table_reports


async def export_memory_to_sqlite(
    runtime: StorageBackendRuntime,
    *,
    template_snapshot_directory: str | Path,
    destination_directory: str | Path,
    batch_size: int = 500,
) -> MemoryExportReport:
    """Create a new source-compatible SQLite DB and prove normalized parity."""

    if int(batch_size) <= 0:
        raise ValueError("Memory export batch_size must be positive")
    destination = Path(destination_directory).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.mkdir(exist_ok=False)
    marker = destination / "EXPORT_INCOMPLETE"
    marker.write_text("Life Memory reverse export is incomplete.\n", encoding="utf-8")
    database = destination / "memory.db"
    template, evidence = open_memory_source(template_snapshot_directory)
    exported = sqlite3.connect(database)
    exported.row_factory = sqlite3.Row
    try:
        exported.execute("PRAGMA foreign_keys = OFF")
        exported.execute("PRAGMA journal_mode = DELETE")
        source_columns = _create_source_schema(template, exported)
        row_count = await _export_explicit_tables(
            runtime,
            exported,
            source_columns,
            batch_size=int(batch_size),
        )
        fts_row_count = await _rebuild_fts(
            runtime,
            exported,
            batch_size=int(batch_size),
        )
        exported.execute("PRAGMA optimize")
        exported.commit()
    except BaseException:
        exported.close()
        template.close()
        raise
    exported.close()
    template.close()
    root_sha256, verified_rows, tables = await _verify_export(
        runtime,
        database,
        batch_size=int(batch_size),
    )
    if verified_rows != row_count:
        raise MemoryExportError("reverse export verified row count changed")
    manifest = {
        "format": "elysium-life-memory-reverse-export-v1",
        "source_snapshot_manifest_sha256": str(
            evidence["manifest"]["manifest_sha256"]
        ),
        "source_database_relative": "memory.db",
        "source_database_sha256": sha256_file(database),
        "table_count": len(tables),
        "row_count": row_count,
        "fts_row_count": fts_row_count,
        "root_sha256": root_sha256,
        "tables": tables,
        "fidelity": {
            "explicit_domain_rows": "normalized-equivalent",
            "legacy_sqlite_fts": "rebuilt-from-explicit-content",
            "sqlite_page_bytes": "not-preserved",
        },
        "verified": True,
    }
    encoded = canonical_json(manifest)
    manifest_sha256 = hashlib.sha256(encoded.encode()).hexdigest()
    (destination / "manifest.json").write_text(
        canonical_json({**manifest, "manifest_sha256": manifest_sha256}) + "\n",
        encoding="utf-8",
    )
    marker.unlink()
    return MemoryExportReport(
        destination_directory=str(destination),
        database_path=str(database),
        table_count=len(tables),
        row_count=row_count,
        fts_row_count=fts_row_count,
        root_sha256=root_sha256,
        manifest_sha256=manifest_sha256,
        verified=True,
    )


__all__ = [
    "MemoryExportError",
    "MemoryExportReport",
    "export_memory_to_sqlite",
]
