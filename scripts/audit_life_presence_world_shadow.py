#!/usr/bin/env python3
"""Independent read-only audit of Presence/World MySQL and reverse snapshots."""

from __future__ import annotations

import argparse
import asyncio
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

from plugins.life_engine.storage.migration.domain_copy import (
    aggregate_domain_root,
    domain_reports,
    mysql_domain_rows,
    open_presence_world_sources,
    sqlite_domain_rows,
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
    return parser.parse_args()


def _open_reverse(directory: Path) -> tuple[sqlite3.Connection, sqlite3.Connection]:
    databases: list[sqlite3.Connection] = []
    for name in ("consciousness_presence.sqlite3", "world_projection.sqlite3"):
        path = (directory / name).resolve()
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
            for opened in databases:
                opened.close()
            raise RuntimeError(f"reverse database failed integrity_check: {name}")
        databases.append(database)
    return databases[0], databases[1]


async def _schema_evidence(engine: Any) -> dict[str, Any]:
    async with engine.connect() as connection:
        versions = [
            dict(row)
            for row in (
                (
                    await connection.execute(
                        text(
                            "SELECT version, name FROM "
                            "life_presence_world_schema_migrations ORDER BY version"
                        )
                    )
                )
                .mappings()
                .all()
            )
        ]
        tables = {
            str(row[0])
            for row in (
                await connection.execute(
                    text(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema = DATABASE() AND "
                        "(table_name LIKE 'consciousness_%' OR "
                        "table_name LIKE 'world_%')"
                    )
                )
            )
        }
        metadata = {
            str(row[0]): str(row[1])
            for row in (
                await connection.execute(
                    text(
                        "SELECT meta_key, meta_value FROM world_projection_meta "
                        "ORDER BY meta_key"
                    )
                )
            )
        }
    expected_tables = {
        "consciousness_presence",
        "consciousness_stream_owners",
        "consciousness_presence_outbox",
        "world_projection_meta",
        "world_assertions",
        "world_projection_changes",
        "world_perception_cursors",
    }
    return {
        "versions": versions,
        "tables_present": sorted(expected_tables & tables),
        "tables_complete": expected_tables <= tables,
        "projector_contract_present": {
            "projector_policy",
            "projector_schema_version",
            "rebuild_state",
        }
        <= metadata.keys(),
        "metadata_keys": sorted(metadata),
    }


async def _audit(args: argparse.Namespace) -> dict[str, Any]:
    source_presence, source_world, evidence = open_presence_world_sources(
        args.snapshot.resolve()
    )
    reverse_presence, reverse_world = _open_reverse(args.reverse_export.resolve())
    try:
        source_rows = sqlite_domain_rows(source_presence, source_world)
        reverse_rows = sqlite_domain_rows(reverse_presence, reverse_world)
    finally:
        source_presence.close()
        source_world.close()
        reverse_presence.close()
        reverse_world.close()
    source_meta_keys = {
        str(row["meta_key"]) for row in source_rows["world_projection_meta"]
    }
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
        mysql_rows = await mysql_domain_rows(
            engine,
            source_world_meta_keys=source_meta_keys,
        )
        schema = await _schema_evidence(engine)
    finally:
        await engine.dispose()
    source_reports = domain_reports(source_rows)
    mysql_reports = domain_reports(mysql_rows)
    reverse_reports = domain_reports(reverse_rows)
    source_map = {row["table_name"]: row for row in source_reports}
    mysql_map = {row["table_name"]: row for row in mysql_reports}
    reverse_map = {row["table_name"]: row for row in reverse_reports}
    mismatches = [
        name
        for name, report in source_map.items()
        if report != mysql_map.get(name) or report != reverse_map.get(name)
    ]
    source_root = aggregate_domain_root(source_reports)
    mysql_root = aggregate_domain_root(mysql_reports)
    reverse_root = aggregate_domain_root(reverse_reports)
    manifest = json.loads(
        (args.reverse_export.resolve() / "manifest.json").read_text(encoding="utf-8")
    )
    verified = (
        not mismatches
        and source_root == mysql_root == reverse_root
        and not (args.reverse_export.resolve() / "EXPORT_INCOMPLETE").exists()
        and manifest.get("verified") is True
        and str(manifest.get("root_sha256")) == reverse_root
        and schema["tables_complete"]
        and schema["projector_contract_present"]
        and [int(row["version"]) for row in schema["versions"]] == [1]
    )
    return {
        "verified": verified,
        "backend_identity": config.safe_identity,
        "source_databases": {
            "presence_sha256": str(
                evidence["presence_entry"].get("backup_sha256")
                or evidence["presence_entry"].get("sha256")
            ),
            "world_sha256": str(
                evidence["world_entry"].get("backup_sha256")
                or evidence["world_entry"].get("sha256")
            ),
        },
        "table_count": len(source_reports),
        "row_count": sum(int(row["row_count"]) for row in source_reports),
        "source_root_sha256": source_root,
        "mysql_root_sha256": mysql_root,
        "reverse_root_sha256": reverse_root,
        "mismatch_tables": mismatches,
        "reverse_manifest_sha256": str(manifest.get("manifest_sha256") or ""),
        "reverse_incomplete_marker": (
            args.reverse_export.resolve() / "EXPORT_INCOMPLETE"
        ).exists(),
        "schema": schema,
    }


def main() -> None:
    print(canonical_json(asyncio.run(_audit(_arguments()))))


if __name__ == "__main__":
    main()
