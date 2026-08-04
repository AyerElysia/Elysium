"""Read-only verification for copied local life-domain snapshots."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.kernel.storage.outbox_primitives import canonical_json_sha256

from .manifest import LifeSnapshotError, load_snapshot_manifest
from .snapshot import inspect_sqlite_database, sha256_file


def _safe_snapshot_path(snapshot_root: Path, relative: str) -> Path:
    candidate = (snapshot_root / relative).resolve()
    try:
        candidate.relative_to(snapshot_root)
    except ValueError as exc:
        raise LifeSnapshotError("snapshot manifest path escapes snapshot root") from exc
    return candidate


def verify_local_snapshot(snapshot_root: str | Path) -> dict[str, Any]:
    """Recompute file, table, database and aggregate roots without repair."""

    snapshot_root = Path(snapshot_root).resolve()
    if (snapshot_root / "SNAPSHOT_INCOMPLETE").exists():
        raise LifeSnapshotError("snapshot is marked incomplete")
    manifest = load_snapshot_manifest(snapshot_root / "manifest.json")
    failures: list[dict[str, str]] = []
    verified_items: list[dict[str, Any]] = []

    for item in list(manifest.get("sqlite") or []):
        path = _safe_snapshot_path(snapshot_root, str(item["backup_relative"]))
        try:
            actual_sha = sha256_file(path)
            inspection = inspect_sqlite_database(path)
            if actual_sha != item["backup_sha256"]:
                raise LifeSnapshotError("physical SQLite checksum mismatch")
            if inspection["database_root_sha256"] != item["database_root_sha256"]:
                raise LifeSnapshotError("logical SQLite root mismatch")
            if inspection["tables"] != item["tables"]:
                raise LifeSnapshotError("SQLite table manifest mismatch")
            verified_items.append(
                {
                    "kind": "sqlite",
                    "path": item["backup_relative"],
                    "sha256": actual_sha,
                    "logical_root_sha256": inspection["database_root_sha256"],
                }
            )
        except (OSError, LifeSnapshotError) as exc:
            failures.append(
                {
                    "kind": "sqlite",
                    "path": str(item.get("backup_relative") or ""),
                    "reason": str(exc),
                }
            )

    for item in list(manifest.get("exact_files") or []):
        path = _safe_snapshot_path(snapshot_root, str(item["backup_relative"]))
        try:
            actual_sha = sha256_file(path)
            if actual_sha != item["sha256"]:
                raise LifeSnapshotError("exact file checksum mismatch")
            if path.stat().st_size != int(item["bytes"]):
                raise LifeSnapshotError("exact file byte count mismatch")
            verified_items.append(
                {
                    "kind": "file",
                    "path": item["backup_relative"],
                    "sha256": actual_sha,
                }
            )
        except (OSError, LifeSnapshotError) as exc:
            failures.append(
                {
                    "kind": "file",
                    "path": str(item.get("backup_relative") or ""),
                    "reason": str(exc),
                }
            )

    verification_root = canonical_json_sha256(verified_items)
    return {
        "verified": not failures,
        "verified_at": datetime.now(UTC).isoformat(),
        "writer_frozen": bool(manifest.get("writer_frozen")),
        "item_count": len(verified_items),
        "failure_count": len(failures),
        "failures": failures,
        "manifest_sha256": manifest["manifest_sha256"],
        "verification_root_sha256": verification_root,
    }


__all__ = ["verify_local_snapshot"]
