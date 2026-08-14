#!/usr/bin/env python3
"""Independently verify MySQL subject heads and an optional reverse export."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

from sqlalchemy import text

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

from plugins.life_engine.storage.migration.manifest import load_snapshot_manifest
from plugins.life_engine.storage.subject_adapters import normalize_subject_path
from src.kernel.storage import (
    MySQLStorageConfig,
    canonical_json,
    create_mysql_storage_engine,
)

_ROOTS = {
    "life_engine_workspace/MEMORY.md",
    "life_engine_workspace/SOUL.md",
    "life_engine_workspace/USER.md",
}
_PREFIXES = (
    "diaries/",
    "life_engine_workspace/diaries/",
    "notes/",
    "life_engine_workspace/notes/",
)


def _required_environment(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"required environment variable is missing: {name}")
    return value


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--reverse-export", type=Path)
    return parser.parse_args()


def _selected_files(snapshot: Path) -> dict[str, tuple[Path, int, str]]:
    manifest = load_snapshot_manifest(snapshot / "manifest.json")
    selected: dict[str, tuple[Path, int, str]] = {}
    for raw in list(manifest.get("exact_files") or []):
        path = normalize_subject_path(str(raw["source_relative"]))
        if path not in _ROOTS and not path.startswith(_PREFIXES):
            continue
        output = (snapshot / str(raw["backup_relative"])).resolve()
        output.relative_to(snapshot)
        if path in selected:
            raise RuntimeError(f"duplicate subject snapshot path: {path}")
        selected[path] = (output, int(raw["bytes"]), str(raw["sha256"]))
    return selected


def _root_update(
    digest: Any,
    *,
    path: str,
    content_hash: str,
    byte_length: int,
) -> None:
    digest.update(path.encode())
    digest.update(b"\0")
    digest.update(content_hash.encode())
    digest.update(b"\0")
    digest.update(str(int(byte_length)).encode())
    digest.update(b"\n")


async def _audit(args: argparse.Namespace) -> dict[str, Any]:
    snapshot = args.snapshot.resolve()
    expected = _selected_files(snapshot)
    config = MySQLStorageConfig(
        host=_required_environment("ELYSIUM_LIFE_STORAGE_MYSQL_HOST"),
        port=int(_required_environment("ELYSIUM_LIFE_STORAGE_MYSQL_PORT")),
        database=_required_environment("ELYSIUM_LIFE_STORAGE_MYSQL_DATABASE"),
        user=_required_environment("ELYSIUM_LIFE_STORAGE_MYSQL_USER"),
        password=_required_environment("ELYSIUM_LIFE_STORAGE_MYSQL_PASSWORD"),
        ssl_mode="disabled",
        pool_size=2,
        max_overflow=0,
        application_query_timeout_seconds=60,
    )
    engine = create_mysql_storage_engine(config)
    root = hashlib.sha256()
    mismatches: list[dict[str, str]] = []
    seen: set[str] = set()
    total_bytes = 0
    try:
        async with engine.connect() as connection:
            result = await connection.stream(
                text(
                    """SELECT d.logical_path, d.revision, d.current_version_id,
                    v.version_id, v.content_bytes, v.content_hash, v.byte_length,
                    v.byte_fidelity
                    FROM subject_documents AS d
                    JOIN subject_document_versions AS v
                      ON v.version_id = d.current_version_id
                    ORDER BY d.logical_path"""
                )
            )
            async for row in result.mappings():
                path = str(row["logical_path"])
                seen.add(path)
                source = expected.get(path)
                content = row["content_bytes"]
                if isinstance(content, memoryview):
                    content = content.tobytes()
                content = bytes(content)
                actual_hash = hashlib.sha256(content).hexdigest()
                reasons: list[str] = []
                if source is None:
                    reasons.append("unexpected target path")
                else:
                    source_path, source_bytes, source_hash = source
                    if content != source_path.read_bytes():
                        reasons.append("content bytes differ")
                    if len(content) != source_bytes:
                        reasons.append("byte length differs")
                    if actual_hash != source_hash:
                        reasons.append("content hash differs")
                if str(row["current_version_id"]) != str(row["version_id"]):
                    reasons.append("head/version identity differs")
                if int(row["revision"]) != 1:
                    reasons.append("snapshot head revision is not one")
                if str(row["content_hash"]) != actual_hash:
                    reasons.append("stored hash differs from stored bytes")
                if int(row["byte_length"]) != len(content):
                    reasons.append("stored byte length differs")
                if str(row["byte_fidelity"]) != "exact_bytes":
                    reasons.append("byte fidelity is not exact_bytes")
                if reasons:
                    mismatches.append({"path": path, "reason": "; ".join(reasons)})
                _root_update(
                    root,
                    path=path,
                    content_hash=actual_hash,
                    byte_length=len(content),
                )
                total_bytes += len(content)
            counts = (
                (
                    await connection.execute(
                        text(
                            """SELECT
                            (SELECT COUNT(*) FROM subject_documents) AS documents,
                            (SELECT COUNT(*) FROM subject_document_versions) AS versions,
                            (SELECT COUNT(*) FROM subject_document_head_events) AS head_events,
                            (SELECT COUNT(*) FROM subject_projection_outbox) AS outbox,
                            (SELECT COUNT(*) FROM subject_document_versions AS v
                             LEFT JOIN subject_documents AS d
                               ON d.document_id = v.document_id
                             WHERE d.document_id IS NULL) AS orphan_versions,
                            (SELECT COUNT(*) FROM subject_projection_outbox AS o
                             LEFT JOIN subject_document_versions AS v
                               ON v.version_id = o.version_id
                             WHERE v.version_id IS NULL) AS orphan_outbox"""
                        )
                    )
                )
                .mappings()
                .one()
            )
    finally:
        await engine.dispose()
    for missing in sorted(set(expected) - seen):
        mismatches.append({"path": missing, "reason": "missing target path"})

    reverse: dict[str, Any] = {}
    if args.reverse_export is not None:
        export = args.reverse_export.resolve()
        export_manifest = json.loads((export / "manifest.json").read_text())
        export_mismatches = 0
        for path, (source_path, source_bytes, source_hash) in expected.items():
            content = (export / "workspace" / path).read_bytes()
            if (
                content != source_path.read_bytes()
                or len(content) != source_bytes
                or hashlib.sha256(content).hexdigest() != source_hash
            ):
                export_mismatches += 1
        reverse = {
            "directory": str(export),
            "document_count": int(export_manifest["document_count"]),
            "root_sha256": str(export_manifest["root_sha256"]),
            "manifest_sha256": str(export_manifest["manifest_sha256"]),
            "incomplete_marker": (export / "EXPORT_INCOMPLETE").exists(),
            "mismatch_count": export_mismatches,
        }
    count_result = {key: int(value) for key, value in counts.items()}
    verified = (
        not mismatches
        and len(seen) == len(expected)
        and count_result["documents"] == len(expected)
        and count_result["versions"] == len(expected)
        and count_result["head_events"] == len(expected)
        and count_result["outbox"] == len(expected)
        and count_result["orphan_versions"] == 0
        and count_result["orphan_outbox"] == 0
        and (
            not reverse
            or (
                reverse["document_count"] == len(expected)
                and not reverse["incomplete_marker"]
                and reverse["mismatch_count"] == 0
                and reverse["root_sha256"] == root.hexdigest()
            )
        )
    )
    return {
        "verified": verified,
        "backend_identity": config.safe_identity,
        "snapshot_directory": str(snapshot),
        "expected_documents": len(expected),
        "seen_documents": len(seen),
        "total_bytes": total_bytes,
        "root_sha256": root.hexdigest(),
        "counts": count_result,
        "mismatch_count": len(mismatches),
        "mismatches": mismatches[:20],
        "reverse_export": reverse,
    }


def main() -> None:
    print(canonical_json(asyncio.run(_audit(_arguments()))))


if __name__ == "__main__":
    main()
