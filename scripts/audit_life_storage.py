#!/usr/bin/env python3
"""Read-only, content-free inventory for Elysium durable data."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

from plugins.life_engine.storage.migration.snapshot import (
    LifeStorageLayout,
)


def audit_sqlite(path: Path) -> dict[str, Any]:
    """Report schema names, counts and timings without exposing row contents."""

    database = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
    try:
        quick_check = database.execute("PRAGMA quick_check").fetchall()
        tables = [
            str(row[0])
            for row in database.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        started = time.perf_counter()
        table_records: list[dict[str, Any]] = []
        for table in tables:
            escaped = table.replace('"', '""')
            row_count = int(
                database.execute(f'SELECT COUNT(*) FROM "{escaped}"').fetchone()[0]
            )
            columns = [
                {"name": str(row[1]), "declared_type": str(row[2] or "")}
                for row in database.execute(f'PRAGMA table_info("{escaped}")')
            ]
            indexes = [
                str(row[1])
                for row in database.execute(f'PRAGMA index_list("{escaped}")')
            ]
            table_records.append(
                {
                    "name": table,
                    "row_count": row_count,
                    "columns": columns,
                    "indexes": sorted(indexes),
                }
            )
        count_scan_ms = (time.perf_counter() - started) * 1000
        return {
            "bytes": path.stat().st_size,
            "quick_check": "ok" if quick_check == [("ok",)] else "failed",
            "table_count": len(table_records),
            "total_rows": sum(item["row_count"] for item in table_records),
            "count_scan_ms": round(count_scan_ms, 3),
            "tables": table_records,
        }
    finally:
        database.close()


def audit_file_root(path: Path) -> dict[str, Any]:
    """Report aggregate exact-file counts/sizes by suffix, never file contents."""

    if not path.exists():
        return {"exists": False, "file_count": 0, "bytes": 0, "suffixes": {}}
    files = [path] if path.is_file() else [item for item in path.rglob("*") if item.is_file()]
    suffixes: dict[str, dict[str, int]] = {}
    total_bytes = 0
    max_file_bytes = 0
    for file_path in files:
        size = file_path.stat().st_size
        total_bytes += size
        max_file_bytes = max(max_file_bytes, size)
        suffix = file_path.suffix.lower() or "<none>"
        record = suffixes.setdefault(suffix, {"count": 0, "bytes": 0})
        record["count"] += 1
        record["bytes"] += size
    return {
        "exists": True,
        "file_count": len(files),
        "bytes": total_bytes,
        "max_file_bytes": max_file_bytes,
        "suffixes": dict(sorted(suffixes.items())),
    }


def audit_life_storage(data_root: Path) -> dict[str, Any]:
    """Audit the explicit life-storage layout using read-only database handles."""

    root = data_root.resolve()
    layout = LifeStorageLayout()
    sqlite_records: dict[str, Any] = {}
    for relative in layout.sqlite_sources:
        source = root / relative
        sqlite_records[relative.as_posix()] = (
            audit_sqlite(source)
            if source.is_file()
            else {"missing": True}
        )
    file_records = {
        relative.as_posix(): audit_file_root(root / relative)
        for relative in layout.exact_roots
    }
    return {
        "data_root": str(root),
        "audited_at": datetime.now(UTC).isoformat(),
        "sqlite": sqlite_records,
        "exact_file_roots": file_records,
        "excluded_rebuildable_roots": [
            item.as_posix() for item in layout.excluded_rebuildable_roots
        ],
        "excluded_preserved_backup_roots": [
            item.as_posix() for item in layout.excluded_preserved_backup_roots
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only life storage inventory")
    parser.add_argument("--data-root", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = audit_life_storage(args.data_root)
    except (OSError, sqlite3.Error) as exc:
        print(json.dumps({"status": "failed", "reason": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
