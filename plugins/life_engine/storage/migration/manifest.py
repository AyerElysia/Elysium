"""Immutable snapshot manifest and backend-generation construction."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.kernel.storage.outbox_primitives import canonical_json_sha256

from ..models import BackendGeneration, BackendKind, GenerationStatus

SNAPSHOT_MANIFEST_SCHEMA_VERSION = 1


class LifeSnapshotError(RuntimeError):
    """Raised when a copy-only snapshot or its evidence is incomplete."""


def _manifest_body(value: dict[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if key != "manifest_sha256"}


def snapshot_manifest_sha256(value: dict[str, Any]) -> str:
    """Hash a manifest without its self-referential checksum field."""

    return canonical_json_sha256(_manifest_body(value))


def load_snapshot_manifest(path: str | Path) -> dict[str, Any]:
    """Load and checksum-validate one snapshot manifest."""

    manifest_path = Path(path)
    try:
        value = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LifeSnapshotError("snapshot manifest is unreadable") from exc
    if not isinstance(value, dict):
        raise LifeSnapshotError("snapshot manifest root must be an object")
    if value.get("schema_version") != SNAPSHOT_MANIFEST_SCHEMA_VERSION:
        raise LifeSnapshotError("snapshot manifest schema is incompatible")
    expected = str(value.get("manifest_sha256") or "")
    if snapshot_manifest_sha256(value) != expected:
        raise LifeSnapshotError("snapshot manifest checksum mismatch")
    return value


def build_backend_generation(
    manifest: dict[str, Any],
    *,
    generation_id: str,
    backend: BackendKind = BackendKind.LOCAL,
    backend_schema_version: int = 1,
    verification: dict[str, Any],
) -> BackendGeneration:
    """Build a candidate or verified generation without inventing evidence."""

    if snapshot_manifest_sha256(manifest) != str(manifest.get("manifest_sha256") or ""):
        raise LifeSnapshotError("cannot build generation from an invalid manifest")
    verified = bool(verification.get("verified")) and bool(manifest.get("writer_frozen"))
    if bool(verification.get("verified")) and not bool(manifest.get("writer_frozen")):
        status = GenerationStatus.CANDIDATE
        verified_at = ""
    elif verified:
        status = GenerationStatus.VERIFIED
        verified_at = str(verification.get("verified_at") or datetime.now(UTC).isoformat())
    else:
        status = GenerationStatus.CANDIDATE
        verified_at = ""
    roots = {
        str(key): str(value)
        for key, value in dict(manifest.get("root_hashes") or {}).items()
    }
    frontiers = {
        str(key): int(value)
        for key, value in dict(manifest.get("frontiers") or {}).items()
    }
    return BackendGeneration(
        generation_id=generation_id,
        backend=backend,
        schema_version=int(backend_schema_version),
        source_snapshot_sha256=str(manifest["source_snapshot_sha256"]),
        root_hashes=roots,
        frontiers=frontiers,
        created_at=str(manifest["created_at"]),
        verified_at=verified_at,
        status=status,
        metadata={
            "snapshot_manifest_sha256": str(manifest["manifest_sha256"]),
            "writer_frozen": bool(manifest.get("writer_frozen")),
            "verification_root_sha256": str(
                verification.get("verification_root_sha256") or ""
            ),
        },
    )


__all__ = [
    "SNAPSHOT_MANIFEST_SCHEMA_VERSION",
    "LifeSnapshotError",
    "build_backend_generation",
    "load_snapshot_manifest",
    "snapshot_manifest_sha256",
]
