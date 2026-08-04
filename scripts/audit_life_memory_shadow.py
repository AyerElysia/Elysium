#!/usr/bin/env python3
"""Read-only parity audit for a Life Memory MySQL shadow and reverse export."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

from sqlalchemy import text

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

from plugins.life_engine.storage.migration.memory_copy import (
    TABLE_SPECS,
    _row_digest,
    _source_context,
    _table_root,
    iter_transformed_source_rows,
    normalize_target_row,
    open_memory_source,
)
from src.kernel.storage import (
    MySQLStorageConfig,
    canonical_json,
    create_mysql_storage_engine,
)


def _required_environment(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"required environment variable is missing: {name}")
    return value


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--reverse-export", type=Path, required=True)
    parser.add_argument("--batch-size", type=int, default=2_000)
    return parser.parse_args()


def _global_root(reports: list[dict[str, Any]], root_key: str) -> str:
    digest = hashlib.sha256()
    for report in reports:
        digest.update(str(report["table_name"]).encode())
        digest.update(b"\0")
        digest.update(str(int(report["row_count"])).encode())
        digest.update(b"\0")
        digest.update(str(report[root_key]).encode())
        digest.update(b"\n")
    return digest.hexdigest()


def _sqlite_roots(
    database: sqlite3.Connection,
    *,
    batch_size: int,
) -> list[dict[str, Any]]:
    context = _source_context(database)
    reports: list[dict[str, Any]] = []
    for spec in TABLE_SPECS.values():
        digests: list[bytes] = []
        count = 0
        for batch in iter_transformed_source_rows(
            database,
            spec,
            context,
            batch_size=batch_size,
        ):
            digests.extend(_row_digest(spec.name, row) for row in batch)
            count += len(batch)
        reports.append(
            {
                "table_name": spec.name,
                "row_count": count,
                "root_sha256": _table_root(digests),
            }
        )
    return reports


def _open_reverse(path: Path) -> sqlite3.Connection:
    database_path = (path / "memory.db").resolve()
    database = sqlite3.connect(
        f"file:{database_path.as_posix()}?mode=ro",
        uri=True,
        timeout=30.0,
    )
    database.row_factory = sqlite3.Row
    database.execute("PRAGMA query_only = ON")
    if database.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        database.close()
        raise RuntimeError("reverse Memory database failed integrity_check")
    return database


async def _mysql_roots(engine: Any) -> list[dict[str, Any]]:
    reports: list[dict[str, Any]] = []
    async with engine.connect() as connection:
        for spec in TABLE_SPECS.values():
            columns = ", ".join(
                "`signal`" if column == "signal" else column
                for column in spec.columns
            )
            order = ", ".join(spec.key_columns)
            result = await connection.stream(
                text(f"SELECT {columns} FROM {spec.name} ORDER BY {order}")
            )
            digests: list[bytes] = []
            count = 0
            async for row in result.mappings():
                digests.append(
                    _row_digest(spec.name, normalize_target_row(spec, row))
                )
                count += 1
            reports.append(
                {
                    "table_name": spec.name,
                    "row_count": count,
                    "root_sha256": _table_root(digests),
                }
            )
    return reports


async def _schema_evidence(engine: Any) -> dict[str, Any]:
    async with engine.connect() as connection:
        versions = (
            (
                await connection.execute(
                    text(
                        "SELECT version, name FROM life_memory_schema_migrations "
                        "ORDER BY version"
                    )
                )
            )
            .mappings()
            .all()
        )
        columns = (
            (
                await connection.execute(
                    text(
                        """SELECT TABLE_NAME AS table_name,
                               COLUMN_NAME AS column_name,
                               DATA_TYPE AS data_type
                        FROM information_schema.columns
                        WHERE table_schema = DATABASE()
                          AND (
                            (table_name = 'memory_nodes' AND column_name IN
                              ('is_deleted', 'event_date', 'legacy_fts_present'))
                            OR (table_name = 'memory_witnesses' AND column_name =
                              'projection_path_sha256')
                            OR (table_name LIKE 'memory_%' AND column_name IN
                              ('metadata_json', 'parent_artifact_ids_json',
                               'context_json', 'entity_refs_json', 'payload_json'))
                          )
                        ORDER BY table_name, ordinal_position"""
                    )
                )
            )
            .mappings()
            .all()
        )
        foreign_keys = int(
            (
                await connection.execute(
                    text(
                        """SELECT COUNT(*) FROM information_schema.referential_constraints
                        WHERE constraint_schema = DATABASE()
                          AND table_name LIKE 'memory_%'"""
                    )
                )
            ).scalar_one()
        )
    json_columns = [
        dict(row)
        for row in columns
        if str(row["column_name"]).endswith("_json")
    ]
    required = {
        (str(row["table_name"]), str(row["column_name"]))
        for row in columns
    }
    return {
        "versions": [dict(row) for row in versions],
        "required_columns_present": {
            "memory_nodes.is_deleted": ("memory_nodes", "is_deleted") in required,
            "memory_nodes.event_date": ("memory_nodes", "event_date") in required,
            "memory_nodes.legacy_fts_present": (
                "memory_nodes",
                "legacy_fts_present",
            )
            in required,
            "memory_witnesses.projection_path_sha256": (
                "memory_witnesses",
                "projection_path_sha256",
            )
            in required,
        },
        "lossless_json_text_columns": json_columns,
        "lossless_json_text": bool(json_columns)
        and all(str(row["data_type"]).lower() == "longtext" for row in json_columns),
        "foreign_key_count": foreign_keys,
    }


async def _audit(args: argparse.Namespace) -> dict[str, Any]:
    if args.batch_size <= 0:
        raise ValueError("batch size must be positive")
    snapshot = args.snapshot.resolve()
    reverse_path = args.reverse_export.resolve()
    source, evidence = open_memory_source(snapshot)
    reverse = _open_reverse(reverse_path)
    try:
        source_reports = _sqlite_roots(source, batch_size=args.batch_size)
        reverse_reports = _sqlite_roots(reverse, batch_size=args.batch_size)
    finally:
        source.close()
        reverse.close()

    config = MySQLStorageConfig(
        host=_required_environment("ELYSIUM_LIFE_STORAGE_MYSQL_HOST"),
        port=int(_required_environment("ELYSIUM_LIFE_STORAGE_MYSQL_PORT")),
        database=_required_environment("ELYSIUM_LIFE_STORAGE_MYSQL_DATABASE"),
        user=_required_environment("ELYSIUM_LIFE_STORAGE_MYSQL_USER"),
        password=_required_environment("ELYSIUM_LIFE_STORAGE_MYSQL_PASSWORD"),
        ssl_mode="disabled",
        pool_size=2,
        max_overflow=0,
        application_query_timeout_seconds=120,
    )
    engine = create_mysql_storage_engine(config)
    try:
        mysql_reports = await _mysql_roots(engine)
        schema = await _schema_evidence(engine)
    finally:
        await engine.dispose()

    source_root = _global_root(source_reports, "root_sha256")
    mysql_root = _global_root(mysql_reports, "root_sha256")
    reverse_root = _global_root(reverse_reports, "root_sha256")
    reverse_manifest = json.loads((reverse_path / "manifest.json").read_text())
    source_map = {row["table_name"]: row for row in source_reports}
    mysql_map = {row["table_name"]: row for row in mysql_reports}
    reverse_map = {row["table_name"]: row for row in reverse_reports}
    mismatches = [
        name
        for name in source_map
        if source_map[name] != mysql_map.get(name)
        or source_map[name] != reverse_map.get(name)
    ]
    deleted_nodes = int(
        next(
            row["row_count"]
            for row in source_reports
            if row["table_name"] == "memory_nodes"
        )
    )
    verified = (
        not mismatches
        and source_root == mysql_root == reverse_root
        and not (reverse_path / "EXPORT_INCOMPLETE").exists()
        and str(reverse_manifest["root_sha256"]) == reverse_root
        and int(reverse_manifest["row_count"])
        == sum(int(row["row_count"]) for row in source_reports)
        and all(schema["required_columns_present"].values())
        and schema["lossless_json_text"]
        and [int(row["version"]) for row in schema["versions"]] == list(range(1, 9))
    )
    return {
        "verified": verified,
        "backend_identity": config.safe_identity,
        "source_database_sha256": str(
            evidence["entry"].get("backup_sha256")
            or evidence["entry"].get("sha256")
        ),
        "table_count": len(source_reports),
        "row_count": sum(int(row["row_count"]) for row in source_reports),
        "memory_node_count": deleted_nodes,
        "source_root_sha256": source_root,
        "mysql_root_sha256": mysql_root,
        "reverse_root_sha256": reverse_root,
        "mismatch_tables": mismatches,
        "reverse_manifest_sha256": str(reverse_manifest["manifest_sha256"]),
        "reverse_incomplete_marker": (
            reverse_path / "EXPORT_INCOMPLETE"
        ).exists(),
        "schema": schema,
    }


def main() -> None:
    print(canonical_json(asyncio.run(_audit(_arguments()))))


if __name__ == "__main__":
    main()
