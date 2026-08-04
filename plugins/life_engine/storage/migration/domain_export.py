"""Audited MySQL to source-compatible SQLite Presence/World export."""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from src.kernel.storage import canonical_json

from ..contracts import StorageBackendRuntime
from .domain_copy import (
    TABLE_SPECS,
    PresenceWorldCopyError,
    aggregate_domain_root,
    domain_reports,
    mysql_domain_rows,
    open_presence_world_sources,
    sqlite_domain_rows,
)
from .snapshot import sha256_file


class PresenceWorldExportError(RuntimeError):
    """Raised when a reverse export cannot prove domain equivalence."""


@dataclass(frozen=True, slots=True)
class PresenceWorldExportReport:
    destination_directory: str
    presence_database_path: str
    world_database_path: str
    table_count: int
    row_count: int
    root_sha256: str
    manifest_sha256: str
    verified: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _create_source_schema(
    template: sqlite3.Connection,
    destination: sqlite3.Connection,
    table_names: list[str],
) -> None:
    indexes: list[str] = []
    for table in table_names:
        row = template.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        if row is None or not str(row[0] or "").strip():
            raise PresenceWorldExportError(f"source schema missing table: {table}")
        destination.execute(str(row[0]))
        indexes.extend(
            str(item[0])
            for item in template.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE type = 'index' AND tbl_name = ? AND sql IS NOT NULL",
                (table,),
            )
        )
    for statement in indexes:
        destination.execute(statement)
    destination.commit()


def _source_values(spec: Any, row: dict[str, Any]) -> tuple[Any, ...]:
    target = dict(row)
    return tuple(
        target[target_column]
        for target_column in spec.target_columns
    )


def _write_rows(
    database: sqlite3.Connection,
    table_names: list[str],
    rows: dict[str, list[dict[str, Any]]],
) -> None:
    database.execute("BEGIN IMMEDIATE")
    try:
        for table in table_names:
            spec = TABLE_SPECS[table]
            columns = ", ".join(spec.source_columns)
            marks = ", ".join("?" for _ in spec.source_columns)
            database.executemany(
                f"INSERT INTO {table} ({columns}) VALUES ({marks})",
                [_source_values(spec, row) for row in rows[table]],
            )
        database.commit()
    except Exception:
        database.rollback()
        raise


def _open_export(path: Path) -> sqlite3.Connection:
    database = sqlite3.connect(path, timeout=30.0)
    database.row_factory = sqlite3.Row
    return database


async def export_presence_world_to_sqlite(
    runtime: StorageBackendRuntime,
    *,
    template_snapshot_directory: str | Path,
    destination_directory: str | Path,
) -> PresenceWorldExportReport:
    """Create two new legacy-compatible databases and prove their parity."""

    if runtime.engine is None:
        raise PresenceWorldExportError("Presence/World export runtime has no engine")
    destination = Path(destination_directory).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.mkdir(exist_ok=False)
    marker = destination / "EXPORT_INCOMPLETE"
    marker.write_text("Presence/World reverse export is incomplete.\n", encoding="utf-8")
    presence_path = destination / "consciousness_presence.sqlite3"
    world_path = destination / "world_projection.sqlite3"
    manifest_path = destination / "manifest.json"

    template_presence, template_world, _ = open_presence_world_sources(
        template_snapshot_directory
    )
    presence = _open_export(presence_path)
    world = _open_export(world_path)
    try:
        presence_tables = [
            name for name, spec in TABLE_SPECS.items() if spec.database == "presence"
        ]
        world_tables = [
            name for name, spec in TABLE_SPECS.items() if spec.database == "world"
        ]
        _create_source_schema(template_presence, presence, presence_tables)
        _create_source_schema(template_world, world, world_tables)
        source_meta_keys = {
            str(row[0])
            for row in template_world.execute(
                "SELECT key FROM world_projection_meta ORDER BY key"
            )
        }
        target_rows = await mysql_domain_rows(
            runtime.engine,
            source_world_meta_keys=source_meta_keys,
        )
        _write_rows(presence, presence_tables, target_rows)
        _write_rows(world, world_tables, target_rows)
        for database, label in ((presence, "Presence"), (world, "World")):
            integrity = database.execute("PRAGMA integrity_check").fetchone()
            if integrity is None or str(integrity[0]).lower() != "ok":
                raise PresenceWorldExportError(
                    f"reverse {label} database failed integrity_check"
                )
        exported_rows = sqlite_domain_rows(presence, world)
        target_reports = domain_reports(target_rows)
        exported_reports = domain_reports(exported_rows)
        if target_reports != exported_reports:
            raise PresenceWorldExportError("reverse Presence/World root mismatch")
        root = aggregate_domain_root(exported_reports)
        manifest = {
            "format": "elysium-presence-world-sqlite-export-v1",
            "databases": {
                presence_path.name: {
                    "sha256": sha256_file(presence_path),
                    "bytes": presence_path.stat().st_size,
                },
                world_path.name: {
                    "sha256": sha256_file(world_path),
                    "bytes": world_path.stat().st_size,
                },
            },
            "table_count": len(TABLE_SPECS),
            "row_count": sum(len(value) for value in exported_rows.values()),
            "root_sha256": root,
            "timestamp_policy": "UTC-normalized instants",
            "json_policy": "canonical semantic JSON",
            "verified": True,
        }
        encoded = canonical_json(manifest)
        manifest_sha256 = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        manifest_path.write_text(
            canonical_json({**manifest, "manifest_sha256": manifest_sha256}) + "\n",
            encoding="utf-8",
        )
        marker.unlink()
        return PresenceWorldExportReport(
            destination_directory=str(destination),
            presence_database_path=str(presence_path),
            world_database_path=str(world_path),
            table_count=len(TABLE_SPECS),
            row_count=sum(len(value) for value in exported_rows.values()),
            root_sha256=root,
            manifest_sha256=manifest_sha256,
            verified=True,
        )
    except (PresenceWorldCopyError, sqlite3.DatabaseError) as exc:
        raise PresenceWorldExportError(str(exc)) from exc
    finally:
        presence.close()
        world.close()
        template_presence.close()
        template_world.close()


__all__ = [
    "PresenceWorldExportError",
    "PresenceWorldExportReport",
    "export_presence_world_to_sqlite",
]
