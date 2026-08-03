"""Stable records used by the unified memory archive.

The archive transports technical representations only.  It never infers the
meaning, truth, importance, or ownership of subjective content.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any

ARCHIVE_SCHEMA_VERSION = 1


def canonical_json(value: Any) -> str:
    """Serialize JSON deterministically and reject non-portable float values."""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class ArchiveMode(StrEnum):
    """Whether a logical identity may acquire later byte-exact versions."""

    IMMUTABLE = "immutable"
    VERSIONED = "versioned"


@dataclass(frozen=True, slots=True)
class ArchiveRecord:
    """One byte-verifiable row, schema object, or workspace file version."""

    record_id: str
    source_node_id: str
    source_domain: str
    record_kind: str
    logical_key: str
    logical_key_hash: str
    immutable_key: str | None
    mode: ArchiveMode
    source_sequence: int
    recorded_at: str
    visibility: str
    authority: str
    payload_json: str
    payload_hash: str
    schema_version: int = ARCHIVE_SCHEMA_VERSION

    @classmethod
    def build(
        cls,
        *,
        source_node_id: str,
        source_domain: str,
        record_kind: str,
        logical_key: str,
        mode: ArchiveMode,
        source_sequence: int,
        recorded_at: str,
        visibility: str,
        authority: str,
        payload: Any,
        schema_version: int = ARCHIVE_SCHEMA_VERSION,
    ) -> ArchiveRecord:
        payload_json = canonical_json(payload)
        payload_hash = sha256_text(payload_json)
        identity = canonical_json(
            {
                "source_node_id": str(source_node_id),
                "source_domain": str(source_domain),
                "record_kind": str(record_kind),
                "logical_key": str(logical_key),
            }
        )
        logical_key_hash = sha256_text(str(logical_key))
        immutable_key = sha256_text(identity) if mode is ArchiveMode.IMMUTABLE else None
        record_id = immutable_key or sha256_text(f"{identity}\n{payload_hash}")
        return cls(
            record_id=record_id,
            source_node_id=str(source_node_id),
            source_domain=str(source_domain),
            record_kind=str(record_kind),
            logical_key=str(logical_key),
            logical_key_hash=logical_key_hash,
            immutable_key=immutable_key,
            mode=mode,
            source_sequence=max(0, int(source_sequence)),
            recorded_at=str(recorded_at),
            visibility=str(visibility or "owner_private").lower(),
            authority=str(authority),
            payload_json=payload_json,
            payload_hash=payload_hash,
            schema_version=int(schema_version),
        )

    def payload(self) -> Any:
        return json.loads(self.payload_json)

    def as_dict(self) -> dict[str, Any]:
        values = asdict(self)
        values["mode"] = self.mode.value
        return values


@dataclass(frozen=True, slots=True)
class ArchivePublishResult:
    """Outcome for one published record."""

    record_id: str
    status: str
    archive_position: int = 0
    conflict_reason: str = ""
    existing_hash: str = ""

    @property
    def accepted(self) -> bool:
        return self.status in {"accepted", "duplicate"}


@dataclass(frozen=True, slots=True)
class ArchiveRunSummary:
    """Auditable result of one finite archive synchronization pass."""

    manifest_id: str
    source_node_id: str
    scanned: int
    accepted: int
    duplicates: int
    conflicts: int
    root_hash: str
    source_counts: dict[str, int]
    status: str
