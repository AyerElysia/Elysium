"""Backend-neutral value objects for life-domain storage selection."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from src.kernel.storage.outbox_primitives import canonical_json

_GENERATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class BackendKind(StrEnum):
    """Supported complete life-domain authority backends."""

    LOCAL = "local"
    MYSQL = "mysql"


class GenerationStatus(StrEnum):
    """Lifecycle of an immutable backend-generation manifest."""

    CANDIDATE = "candidate"
    VERIFIED = "verified"
    SEALED = "sealed"


class StorageAvailability(StrEnum):
    """Operational status without conflating disabled, degraded, and failed."""

    DISABLED = "disabled"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class BackendGeneration:
    """Secret-free immutable identity of one copied and verified backend state."""

    generation_id: str
    backend: BackendKind
    schema_version: int
    source_snapshot_sha256: str
    root_hashes: dict[str, str]
    frontiers: dict[str, int]
    created_at: str
    verified_at: str = ""
    status: GenerationStatus = GenerationStatus.CANDIDATE
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not _GENERATION_ID.fullmatch(self.generation_id):
            raise ValueError(f"invalid backend generation id: {self.generation_id!r}")
        if int(self.schema_version) <= 0:
            raise ValueError("backend generation schema_version must be positive")
        if not re.fullmatch(r"[0-9a-f]{64}", self.source_snapshot_sha256):
            raise ValueError("source_snapshot_sha256 must be a lowercase SHA-256")
        for name, value in self.root_hashes.items():
            if not name or not re.fullmatch(r"[0-9a-f]{64}", value):
                raise ValueError(f"invalid root hash for {name!r}")
        for name, value in self.frontiers.items():
            if not name or int(value) < 0:
                raise ValueError(f"invalid frontier for {name!r}")
        if self.status == GenerationStatus.VERIFIED and not self.verified_at:
            raise ValueError("verified generation requires verified_at")

    def to_dict(self) -> dict[str, Any]:
        """Return a deterministic JSON-compatible manifest body."""

        return {
            "generation_id": self.generation_id,
            "backend": self.backend.value,
            "schema_version": int(self.schema_version),
            "source_snapshot_sha256": self.source_snapshot_sha256,
            "root_hashes": dict(sorted(self.root_hashes.items())),
            "frontiers": {
                key: int(value) for key, value in sorted(self.frontiers.items())
            },
            "created_at": self.created_at,
            "verified_at": self.verified_at,
            "status": self.status.value,
            "metadata": self.metadata,
        }

    @property
    def manifest_sha256(self) -> str:
        """Hash the canonical manifest body for conflict and audit checks."""

        return hashlib.sha256(canonical_json(self.to_dict()).encode("utf-8")).hexdigest()

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> BackendGeneration:
        """Validate and restore one generation from persisted JSON."""

        return cls(
            generation_id=str(value["generation_id"]),
            backend=BackendKind(str(value["backend"])),
            schema_version=int(value["schema_version"]),
            source_snapshot_sha256=str(value["source_snapshot_sha256"]),
            root_hashes={
                str(key): str(item)
                for key, item in dict(value.get("root_hashes") or {}).items()
            },
            frontiers={
                str(key): int(item)
                for key, item in dict(value.get("frontiers") or {}).items()
            },
            created_at=str(value["created_at"]),
            verified_at=str(value.get("verified_at") or ""),
            status=GenerationStatus(str(value.get("status") or "candidate")),
            metadata=dict(value.get("metadata") or {}),
        )


@dataclass(frozen=True, slots=True)
class AuthorityToken:
    """One short-lived writer lease; the raw fencing secret is never serialized."""

    registry_id: str
    backend: BackendKind
    generation_id: str
    authority_epoch: int
    owner_id: str
    lease_until: str
    fencing_token: str = field(repr=False)

    def __post_init__(self) -> None:
        if not self.registry_id.strip():
            raise ValueError("authority registry_id must not be empty")
        if not _GENERATION_ID.fullmatch(self.generation_id):
            raise ValueError("authority token generation_id is invalid")
        if int(self.authority_epoch) <= 0:
            raise ValueError("authority epoch must be positive")
        if not self.owner_id.strip():
            raise ValueError("authority owner_id must not be empty")
        if not self.fencing_token:
            raise ValueError("fencing token must not be empty")

    @property
    def fencing_token_sha256(self) -> str:
        return hashlib.sha256(self.fencing_token.encode("utf-8")).hexdigest()

    @property
    def expired_by_local_clock(self) -> bool:
        """Return a local-mode hint; MySQL validation always uses DB time."""

        value = datetime.fromisoformat(self.lease_until)
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value <= datetime.now(UTC)

    def safe_dict(self) -> dict[str, Any]:
        """Return health/audit fields without the reusable fencing secret."""

        return {
            "registry_id": self.registry_id,
            "backend": self.backend.value,
            "generation_id": self.generation_id,
            "authority_epoch": int(self.authority_epoch),
            "owner_id": self.owner_id,
            "lease_until": self.lease_until,
        }


__all__ = [
    "AuthorityToken",
    "BackendGeneration",
    "BackendKind",
    "GenerationStatus",
    "StorageAvailability",
]
