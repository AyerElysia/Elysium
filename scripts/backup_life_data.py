#!/usr/bin/env python3
"""Create and independently verify a non-destructive life-domain snapshot."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import replace
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
    core_sqlite_relative: Path = Path("Elysium.db"),
    proactive_sqlite_relative: Path = Path(
        "life_engine_workspace/runtime/proactive/proactive.sqlite3"
    ),
    precreated_output: bool = False,
) -> dict[str, Any]:
    """Compatibility wrapper around the versioned snapshot implementation."""

    if output.resolve().exists() and not precreated_output:
        raise LifeBackupError("输出目录已存在，拒绝覆盖")

    default_layout = LifeStorageLayout()
    if (
        core_sqlite_relative.is_absolute()
        or not core_sqlite_relative.parts
        or any(part in {"", ".", ".."} for part in core_sqlite_relative.parts)
    ):
        raise LifeBackupError("Core SQLite 路径必须是 data 下的安全相对路径")
    if (
        proactive_sqlite_relative.is_absolute()
        or not proactive_sqlite_relative.parts
        or any(part in {"", ".", ".."} for part in proactive_sqlite_relative.parts)
    ):
        raise LifeBackupError("主动系统 SQLite 路径必须是 data 下的安全相对路径")
    default_proactive = Path(
        "life_engine_workspace/runtime/proactive/proactive.sqlite3"
    )
    layout = replace(
        default_layout,
        sqlite_sources=tuple(
            core_sqlite_relative
            if relative == default_layout.sqlite_sources[0]
            else proactive_sqlite_relative
            if relative == default_proactive
            else relative
            for relative in default_layout.sqlite_sources
        ),
    )

    manifest = create_local_snapshot(
        data_root,
        output,
        layout=layout,
        writer_frozen=writer_frozen,
        precreated_output=precreated_output,
    )
    verification = verify_local_snapshot(output)
    if not verification["verified"]:
        failure_marker = output / "VERIFICATION_FAILED.json"
        with failure_marker.open("x", encoding="utf-8") as handle:
            json.dump(
                {
                    "manifest_sha256": manifest["manifest_sha256"],
                    "verified_at": verification["verified_at"],
                    "failure_count": verification["failure_count"],
                    "failures": verification["failures"],
                },
                handle,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            handle.write("\n")
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
        "--core-sqlite-relative",
        type=Path,
        default=Path("Elysium.db"),
        help="Core SQLite path relative to --data-root (default: Elysium.db)",
    )
    parser.add_argument(
        "--proactive-sqlite-relative",
        type=Path,
        default=Path("life_engine_workspace/runtime/proactive/proactive.sqlite3"),
        help=(
            "Proactive SQLite path relative to --data-root "
            "(default: life_engine_workspace/runtime/proactive/proactive.sqlite3)"
        ),
    )
    parser.add_argument(
        "--precreated-output",
        action="store_true",
        help=argparse.SUPPRESS,
    )
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
            core_sqlite_relative=args.core_sqlite_relative,
            proactive_sqlite_relative=args.proactive_sqlite_relative,
            precreated_output=args.precreated_output,
        )
    except (LifeBackupError, OSError, sqlite3.Error) as error:
        print(
            json.dumps({"status": "failed", "reason": str(error)}, ensure_ascii=False)
        )
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
