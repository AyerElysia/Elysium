#!/usr/bin/env python3
"""Create and independently verify a non-destructive life-domain snapshot."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

from plugins.life_engine.storage.migration import (
    LifeSnapshotError,
    LifeStorageLayout,
    create_local_snapshot,
    verify_local_snapshot,
)

SQLITE_SOURCES = LifeStorageLayout().sqlite_sources
LifeBackupError = LifeSnapshotError


def create_life_backup(
    data_root: Path,
    output: Path,
    *,
    writer_frozen: bool = False,
) -> dict[str, Any]:
    """Compatibility wrapper around the versioned snapshot implementation."""

    if output.resolve().exists():
        raise LifeBackupError("输出目录已存在，拒绝覆盖")

    manifest = create_local_snapshot(
        data_root,
        output,
        writer_frozen=writer_frozen,
    )
    verification = verify_local_snapshot(output)
    result = dict(manifest)
    result.update(
        {
            "manifest": str((output / "manifest.json").resolve()),
            "sqlite_count": len(manifest["sqlite"]),
            "verification": verification,
            "generation_eligible": bool(
                manifest["writer_frozen"] and verification["verified"]
            ),
        }
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Copy and verify Elysium local life-domain data without overwriting sources"
    )
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--writer-frozen",
        action="store_true",
        help=(
            "assert that all known writers were manually stopped for this snapshot; "
            "without it the snapshot remains a non-activatable candidate"
        ),
    )
    args = parser.parse_args()
    try:
        result = create_life_backup(
            args.data_root,
            args.output,
            writer_frozen=args.writer_frozen,
        )
    except (LifeBackupError, OSError, sqlite3.Error) as error:
        print(json.dumps({"status": "failed", "reason": str(error)}, ensure_ascii=False))
        return 2
    summary = {
        "output": result["output"],
        "manifest": result["manifest"],
        "manifest_sha256": result["manifest_sha256"],
        "source_snapshot_sha256": result["source_snapshot_sha256"],
        "writer_frozen": result["writer_frozen"],
        "sqlite_count": result["sqlite_count"],
        "exact_file_count": result["exact_file_count"],
        "verification": result["verification"],
        "generation_eligible": result["generation_eligible"],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if result["verification"]["verified"] else 3


if __name__ == "__main__":
    raise SystemExit(main())
