#!/usr/bin/env python3
"""为 Elysium 本地生命域权威数据创建不可覆盖的一致性备份。"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import shutil
import sqlite3
from pathlib import Path
from typing import Any

SQLITE_SOURCES = (
    Path("Elysium.db"),
    Path("life_engine_workspace/.memory/memory.db"),
    Path("life_engine_workspace/life_events.sqlite3"),
    Path("life_engine_workspace/runtime/consciousness_presence.sqlite3"),
    Path("life_engine_workspace/runtime/world_projection.sqlite3"),
)

WORKSPACE_SOURCES = (
    Path("diaries"),
    Path("life_engine_workspace/.life_learning"),
    Path("life_engine_workspace/.life_narrative"),
    Path("life_engine_workspace/.life_trace"),
    Path("life_engine_workspace/diaries"),
    Path("life_engine_workspace/dreams"),
    Path("life_engine_workspace/narrative"),
    Path("life_engine_workspace/notes"),
    Path("life_engine_workspace/received"),
    Path("life_engine_workspace/skills"),
    Path("life_engine_workspace/thoughts"),
)


class LifeBackupError(RuntimeError):
    """生命域备份的输入、快照或完整性检查失败。"""


def _sha256(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file_handle:
        while chunk := file_handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def _backup_sqlite(source: Path, destination: Path) -> dict[str, Any]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_db = sqlite3.connect(f"file:{source.resolve()}?mode=ro", uri=True)
    target_db = sqlite3.connect(destination)
    try:
        quick_check = source_db.execute("PRAGMA quick_check").fetchall()
        if quick_check != [("ok",)]:
            raise LifeBackupError(f"SQLite quick_check 失败: {source}")
        source_db.backup(target_db)
    finally:
        target_db.close()
        source_db.close()

    verification = sqlite3.connect(
        f"file:{destination.resolve()}?mode=ro", uri=True
    )
    try:
        integrity = verification.execute("PRAGMA integrity_check").fetchall()
        if integrity != [("ok",)]:
            raise LifeBackupError(f"SQLite integrity_check 失败: {destination}")
        tables = [
            str(row[0])
            for row in verification.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
    finally:
        verification.close()
    return {
        "source": str(source),
        "backup": str(destination),
        "bytes": destination.stat().st_size,
        "sha256": _sha256(destination),
        "integrity_check": "ok",
        "tables": tables,
    }


def _copy_workspace_tree(source: Path, destination: Path) -> list[dict[str, Any]]:
    if not source.exists():
        return []
    records: list[dict[str, Any]] = []
    for source_file in sorted(path for path in source.rglob("*") if path.is_file()):
        relative = source_file.relative_to(source)
        target_file = destination / relative
        target_file.parent.mkdir(parents=True, exist_ok=True)
        before = source_file.stat()
        shutil.copy2(source_file, target_file)
        after = source_file.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise LifeBackupError(f"备份期间文件发生变化，请重试: {source_file}")
        records.append(
            {
                "source": str(source_file),
                "backup": str(target_file),
                "bytes": target_file.stat().st_size,
                "sha256": _sha256(target_file),
            }
        )
    return records


def create_life_backup(data_root: Path, output: Path) -> dict[str, Any]:
    """备份权威 SQLite 与生命工作区文件，不复制可重建向量投影。"""
    data_root = data_root.resolve()
    output = output.resolve()
    if not data_root.is_dir():
        raise LifeBackupError(f"数据目录不存在: {data_root}")
    if output.exists():
        raise LifeBackupError("输出目录已存在，拒绝覆盖")
    output.mkdir(parents=True)

    sqlite_records: list[dict[str, Any]] = []
    for relative in SQLITE_SOURCES:
        source = data_root / relative
        if not source.is_file():
            raise LifeBackupError(f"权威 SQLite 不存在: {source}")
        sqlite_records.append(
            _backup_sqlite(source, output / "sqlite" / relative)
        )

    workspace_records: list[dict[str, Any]] = []
    for relative in WORKSPACE_SOURCES:
        workspace_records.extend(
            _copy_workspace_tree(
                data_root / relative,
                output / "workspace" / relative,
            )
        )

    result: dict[str, Any] = {
        "created_at_utc": dt.datetime.now(dt.UTC).isoformat(),
        "data_root": str(data_root),
        "output": str(output),
        "sqlite": sqlite_records,
        "workspace_file_count": len(workspace_records),
        "workspace_files": workspace_records,
        "excluded_rebuildable_projections": [
            "data/chroma_db",
            "data/life_engine_workspace/.memory/chroma",
        ],
    }
    manifest = output / "manifest.json"
    manifest.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    result["manifest"] = str(manifest)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="备份 Elysium 本地生命域权威数据")
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = create_life_backup(args.data_root, args.output)
    except (LifeBackupError, OSError, sqlite3.Error) as error:
        print(f"生命域备份失败: {error}")
        return 2
    summary = {
        "output": result["output"],
        "manifest": result["manifest"],
        "sqlite": [
            {
                "source": item["source"],
                "bytes": item["bytes"],
                "sha256": item["sha256"],
                "integrity_check": item["integrity_check"],
            }
            for item in result["sqlite"]
        ],
        "workspace_file_count": result["workspace_file_count"],
        "excluded_rebuildable_projections": result[
            "excluded_rebuildable_projections"
        ],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
