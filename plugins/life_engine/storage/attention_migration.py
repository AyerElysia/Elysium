"""Lossless legacy ThoughtStream archival and snapshot-only candidate migration.

The legacy ``thoughts/streams.json`` file is a current-state snapshot, not an
event ledger. This module preserves its exact bytes and imports its rows only
into a migration candidate namespace. It never fabricates canonical
``AttentionThreadEvent`` history and never activates a backend generation.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import text

from src.kernel.storage import canonical_json

from ..streams.legacy_snapshot import (
    LegacyStreamsSnapshot,
    read_legacy_streams_snapshot,
)
from .contracts import StorageBackendRuntime, StorageWriterRole

ATTENTION_LEGACY_ARCHIVE_SCHEMA_VERSION = 1
ATTENTION_LEGACY_IMPORT_MODE = "snapshot_only"
ATTENTION_LEGACY_SOURCE_LABEL = "life_engine_workspace/thoughts/streams.json"
ATTENTION_LEGACY_MAX_ARCHIVE_BYTES = 256 * 1024 * 1024

_ARCHIVE_FILE_NAME = "streams.json"
_ARCHIVE_MANIFEST_NAME = "manifest.json"
_ARCHIVE_INCOMPLETE = "ATTENTION_ARCHIVE_INCOMPLETE"


class AttentionLegacyMigrationError(RuntimeError):
    """Raised when exact archival or snapshot-only equivalence cannot be proven."""


@dataclass(frozen=True, slots=True)
class AttentionLegacyArchiveReport:
    """Content-free evidence for one immutable filesystem archive."""

    archive_directory: str
    snapshot_sha256: str
    byte_length: int
    row_count: int
    rows_root_sha256: str
    manifest_sha256: str
    verified: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class AttentionLegacyCopyReport:
    """Content-free evidence for one candidate-copy database import."""

    snapshot_sha256: str
    byte_length: int
    row_count: int
    rows_root_sha256: str
    canonical_event_count_before: int
    canonical_event_count_after: int
    canonical_head_count: int
    canonical_focus_count: int
    canonical_root_sha256: str
    idempotent_replay: bool
    verified: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _manifest_body(value: dict[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if key != "manifest_sha256"}


def _manifest_sha256(value: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(_manifest_body(value)).encode()).hexdigest()


def _rows_root(snapshot: LegacyStreamsSnapshot) -> str:
    material = [
        {
            "source_ordinal": row.source_ordinal,
            "row_sha256": row.row_sha256,
        }
        for row in snapshot.rows
    ]
    return hashlib.sha256(canonical_json(material).encode()).hexdigest()


def _write_new_bytes(path: Path, content: bytes) -> None:
    with path.open("xb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def _archive_manifest(
    snapshot: LegacyStreamsSnapshot,
    *,
    source_label: str,
) -> dict[str, Any]:
    created_at = datetime.now(UTC).isoformat()
    manifest: dict[str, Any] = {
        "schema_version": ATTENTION_LEGACY_ARCHIVE_SCHEMA_VERSION,
        "format": "elysium-legacy-attention-snapshot-v1",
        "created_at": created_at,
        "source_label": str(source_label or ATTENTION_LEGACY_SOURCE_LABEL),
        "archive_file": _ARCHIVE_FILE_NAME,
        "snapshot_sha256": snapshot.sha256,
        "byte_length": snapshot.byte_length,
        "legacy_schema_version": snapshot.schema_version,
        "legacy_global_revision": snapshot.global_revision,
        "row_count": len(snapshot.rows),
        "status_counts": dict(snapshot.status_counts),
        "rows_root_sha256": _rows_root(snapshot),
        "row_hashes": [
            {
                "source_ordinal": row.source_ordinal,
                "row_sha256": row.row_sha256,
            }
            for row in snapshot.rows
        ],
        "import_mode": ATTENTION_LEGACY_IMPORT_MODE,
        "history_claim": "snapshot_only_no_fabricated_events",
        "generation_eligible": False,
    }
    manifest["manifest_sha256"] = _manifest_sha256(manifest)
    return manifest


def _validate_archive_manifest(archive: Path, manifest: dict[str, Any]) -> None:
    if manifest.get("schema_version") != ATTENTION_LEGACY_ARCHIVE_SCHEMA_VERSION:
        raise AttentionLegacyMigrationError("attention archive schema is incompatible")
    if manifest.get("format") != "elysium-legacy-attention-snapshot-v1":
        raise AttentionLegacyMigrationError("attention archive format is incompatible")
    if manifest.get("archive_file") != _ARCHIVE_FILE_NAME:
        raise AttentionLegacyMigrationError("attention archive file identity changed")
    if manifest.get("import_mode") != ATTENTION_LEGACY_IMPORT_MODE:
        raise AttentionLegacyMigrationError("attention archive is not snapshot-only")
    if manifest.get("generation_eligible") is not False:
        raise AttentionLegacyMigrationError(
            "legacy attention archive cannot be activatable"
        )
    if _manifest_sha256(manifest) != str(manifest.get("manifest_sha256") or ""):
        raise AttentionLegacyMigrationError(
            "attention archive manifest checksum mismatch"
        )
    archive_file = (archive / _ARCHIVE_FILE_NAME).resolve()
    try:
        archive_file.relative_to(archive)
    except ValueError as exc:
        raise AttentionLegacyMigrationError(
            "attention archive path escapes root"
        ) from exc


def _verify_snapshot_against_manifest(
    snapshot: LegacyStreamsSnapshot,
    manifest: dict[str, Any],
) -> None:
    expected_rows = [
        {
            "source_ordinal": row.source_ordinal,
            "row_sha256": row.row_sha256,
        }
        for row in snapshot.rows
    ]
    checks = (
        str(manifest.get("snapshot_sha256") or "") == snapshot.sha256,
        int(manifest.get("byte_length", -1)) == snapshot.byte_length,
        int(manifest.get("legacy_schema_version", -1)) == snapshot.schema_version,
        manifest.get("legacy_global_revision") == snapshot.global_revision,
        int(manifest.get("row_count", -1)) == len(snapshot.rows),
        dict(manifest.get("status_counts") or {}) == dict(snapshot.status_counts),
        str(manifest.get("rows_root_sha256") or "") == _rows_root(snapshot),
        list(manifest.get("row_hashes") or []) == expected_rows,
    )
    if not all(checks):
        raise AttentionLegacyMigrationError("attention archive evidence differs")


def create_legacy_attention_archive(
    source_path: str | Path,
    archive_directory: str | Path,
    *,
    source_label: str = ATTENTION_LEGACY_SOURCE_LABEL,
) -> AttentionLegacyArchiveReport:
    """Create a new exact-byte archive without modifying the legacy source."""

    snapshot = read_legacy_streams_snapshot(source_path)
    if snapshot.byte_length > ATTENTION_LEGACY_MAX_ARCHIVE_BYTES:
        raise AttentionLegacyMigrationError(
            "legacy attention snapshot exceeds the explicit archive limit"
        )
    archive = Path(archive_directory).resolve()
    if archive.exists():
        raise AttentionLegacyMigrationError(
            "attention archive destination already exists; refusing overwrite"
        )
    archive.mkdir(parents=True)
    marker = archive / _ARCHIVE_INCOMPLETE
    _write_new_bytes(marker, b"legacy attention archive is incomplete\n")
    _write_new_bytes(archive / _ARCHIVE_FILE_NAME, snapshot.raw_bytes)
    manifest = _archive_manifest(snapshot, source_label=source_label)
    manifest_bytes = (
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    _write_new_bytes(archive / _ARCHIVE_MANIFEST_NAME, manifest_bytes)
    loaded_snapshot = read_legacy_streams_snapshot(archive / _ARCHIVE_FILE_NAME)
    _validate_archive_manifest(archive, manifest)
    _verify_snapshot_against_manifest(loaded_snapshot, manifest)
    if loaded_snapshot.raw_bytes != snapshot.raw_bytes:
        raise AttentionLegacyMigrationError(
            "attention archive bytes differ after write"
        )
    marker.unlink()
    _, loaded_manifest = load_legacy_attention_archive(archive)
    return AttentionLegacyArchiveReport(
        archive_directory=str(archive),
        snapshot_sha256=snapshot.sha256,
        byte_length=snapshot.byte_length,
        row_count=len(snapshot.rows),
        rows_root_sha256=_rows_root(snapshot),
        manifest_sha256=str(loaded_manifest["manifest_sha256"]),
        verified=True,
    )


def load_legacy_attention_archive(
    archive_directory: str | Path,
) -> tuple[LegacyStreamsSnapshot, dict[str, Any]]:
    """Read and independently verify one completed exact-byte archive."""

    archive = Path(archive_directory).resolve()
    if not archive.is_dir():
        raise AttentionLegacyMigrationError("attention archive directory is missing")
    if (archive / _ARCHIVE_INCOMPLETE).exists():
        raise AttentionLegacyMigrationError("attention archive is marked incomplete")
    manifest_path = archive / _ARCHIVE_MANIFEST_NAME
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AttentionLegacyMigrationError(
            "attention archive manifest is unreadable"
        ) from exc
    if not isinstance(manifest, dict):
        raise AttentionLegacyMigrationError(
            "attention archive manifest must be an object"
        )
    _validate_archive_manifest(archive, manifest)
    snapshot = read_legacy_streams_snapshot(archive / _ARCHIVE_FILE_NAME)
    _verify_snapshot_against_manifest(snapshot, manifest)
    return snapshot, manifest


def _snapshot_from_exact_bytes(raw_bytes: bytes) -> LegacyStreamsSnapshot:
    with tempfile.TemporaryDirectory(prefix="elysium-attention-snapshot-") as temporary:
        path = Path(temporary) / _ARCHIVE_FILE_NAME
        path.write_bytes(raw_bytes)
        return read_legacy_streams_snapshot(path)


def _snapshot_row(
    snapshot: LegacyStreamsSnapshot, *, source_label: str
) -> dict[str, Any]:
    return {
        "snapshot_sha256": snapshot.sha256,
        "legacy_schema_version": snapshot.schema_version,
        "legacy_global_revision": snapshot.global_revision,
        "byte_length": snapshot.byte_length,
        "raw_bytes": snapshot.raw_bytes,
        "row_count": len(snapshot.rows),
        "status_counts_json": canonical_json(dict(snapshot.status_counts)),
        "rows_root_sha256": _rows_root(snapshot),
        "import_mode": ATTENTION_LEGACY_IMPORT_MODE,
        "generation_eligible": False,
        "source_label": str(source_label or ATTENTION_LEGACY_SOURCE_LABEL),
        "imported_at": datetime.now(UTC),
    }


def _candidate_rows(snapshot: LegacyStreamsSnapshot) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in snapshot.rows:
        fields = dict(row.original_fields)
        rows.append(
            {
                "snapshot_sha256": snapshot.sha256,
                "source_ordinal": row.source_ordinal,
                "legacy_stream_id": str(fields.get("id") or ""),
                "legacy_status": str(fields.get("status") or ""),
                "row_sha256": row.row_sha256,
                "original_fields_json": canonical_json(fields),
                "candidate_state": ATTENTION_LEGACY_IMPORT_MODE,
            }
        )
    return rows


def _bytes(value: Any) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, (bytearray, memoryview)):
        return bytes(value)
    raise AttentionLegacyMigrationError("stored attention archive bytes are invalid")


def _stored_snapshot_matches(
    stored: dict[str, Any],
    expected: dict[str, Any],
) -> bool:
    return all(
        (
            str(stored["snapshot_sha256"]) == expected["snapshot_sha256"],
            int(stored["legacy_schema_version"]) == expected["legacy_schema_version"],
            stored["legacy_global_revision"] == expected["legacy_global_revision"],
            int(stored["byte_length"]) == expected["byte_length"],
            _bytes(stored["raw_bytes"]) == expected["raw_bytes"],
            int(stored["row_count"]) == expected["row_count"],
            _canonical_database_json(stored["status_counts_json"])
            == expected["status_counts_json"],
            str(stored["rows_root_sha256"]) == expected["rows_root_sha256"],
            str(stored["import_mode"]) == ATTENTION_LEGACY_IMPORT_MODE,
            not bool(stored["generation_eligible"]),
            str(stored["source_label"]) == expected["source_label"],
        )
    )


def _canonical_database_json(value: Any) -> str:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise AttentionLegacyMigrationError(
                "stored attention migration JSON is invalid"
            ) from exc
    if not isinstance(value, (dict, list)):
        raise AttentionLegacyMigrationError(
            "stored attention migration JSON has an invalid type"
        )
    return canonical_json(value)


def _normalize_candidate_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "snapshot_sha256": str(row["snapshot_sha256"]),
        "source_ordinal": int(row["source_ordinal"]),
        "legacy_stream_id": str(row["legacy_stream_id"]),
        "legacy_status": str(row["legacy_status"]),
        "row_sha256": str(row["row_sha256"]),
        "original_fields_json": _canonical_database_json(row["original_fields_json"]),
        "candidate_state": str(row["candidate_state"]),
    }


async def _canonical_authority_counts(
    runtime: StorageBackendRuntime,
) -> dict[str, int]:
    async with runtime.unit_of_work() as uow:
        counts: dict[str, int] = {}
        for name, table in (
            ("events", "attention_thread_events"),
            ("heads", "attention_thread_heads"),
            ("focus", "attention_instance_focus"),
        ):
            counts[name] = int(
                await uow.session.scalar(text(f"SELECT COUNT(*) FROM {table}")) or 0
            )
        return counts


def _canonical_authority_root(counts: dict[str, int]) -> str:
    return hashlib.sha256(canonical_json(counts).encode()).hexdigest()


async def import_legacy_attention_snapshot(
    source_path: str | Path,
    runtime: StorageBackendRuntime,
    *,
    source_label: str = ATTENTION_LEGACY_SOURCE_LABEL,
) -> AttentionLegacyCopyReport:
    """Import exact legacy evidence as non-activatable snapshot-only candidates."""

    if runtime.writer_role != StorageWriterRole.CANDIDATE_COPY:
        raise AttentionLegacyMigrationError(
            "legacy attention import requires a candidate-copy writer"
        )
    snapshot = read_legacy_streams_snapshot(source_path)
    if snapshot.byte_length > ATTENTION_LEGACY_MAX_ARCHIVE_BYTES:
        raise AttentionLegacyMigrationError(
            "legacy attention snapshot exceeds the explicit archive limit"
        )
    await runtime.validate_writer()
    canonical_before = await _canonical_authority_counts(runtime)
    expected_snapshot = _snapshot_row(snapshot, source_label=source_label)
    expected_candidates = _candidate_rows(snapshot)
    idempotent = False

    async with runtime.unit_of_work() as uow:
        session = uow.session
        stored_row = (
            (
                await session.execute(
                    text(
                        """SELECT snapshot_sha256, legacy_schema_version,
                        legacy_global_revision, byte_length, raw_bytes, row_count,
                        status_counts_json, rows_root_sha256, import_mode,
                        generation_eligible, source_label
                        FROM attention_legacy_snapshots
                        WHERE snapshot_sha256 = :snapshot_sha256"""
                    ),
                    {"snapshot_sha256": snapshot.sha256},
                )
            )
            .mappings()
            .one_or_none()
        )
        stored_candidates = (
            (
                await session.execute(
                    text(
                        """SELECT snapshot_sha256, source_ordinal,
                        legacy_stream_id, legacy_status, row_sha256,
                        original_fields_json, candidate_state
                        FROM attention_legacy_candidates
                        WHERE snapshot_sha256 = :snapshot_sha256
                        ORDER BY source_ordinal"""
                    ),
                    {"snapshot_sha256": snapshot.sha256},
                )
            )
            .mappings()
            .all()
        )
        if stored_row is not None:
            if not _stored_snapshot_matches(dict(stored_row), expected_snapshot):
                raise AttentionLegacyMigrationError(
                    "stored legacy attention snapshot conflicts with source"
                )
            if [
                _normalize_candidate_row(dict(row)) for row in stored_candidates
            ] != expected_candidates:
                raise AttentionLegacyMigrationError(
                    "stored legacy attention candidates conflict with source"
                )
            idempotent = True
        else:
            if stored_candidates:
                raise AttentionLegacyMigrationError(
                    "orphan legacy attention candidates exist without snapshot"
                )
            await session.execute(
                text(
                    """INSERT INTO attention_legacy_snapshots (
                        snapshot_sha256, legacy_schema_version,
                        legacy_global_revision, byte_length, raw_bytes,
                        row_count, status_counts_json, rows_root_sha256,
                        import_mode, generation_eligible, source_label, imported_at
                    ) VALUES (
                        :snapshot_sha256, :legacy_schema_version,
                        :legacy_global_revision, :byte_length, :raw_bytes,
                        :row_count, :status_counts_json, :rows_root_sha256,
                        :import_mode, :generation_eligible, :source_label, :imported_at
                    )"""
                ),
                expected_snapshot,
            )
            if expected_candidates:
                await session.execute(
                    text(
                        """INSERT INTO attention_legacy_candidates (
                            snapshot_sha256, source_ordinal, legacy_stream_id,
                            legacy_status, row_sha256, original_fields_json,
                            candidate_state
                        ) VALUES (
                            :snapshot_sha256, :source_ordinal, :legacy_stream_id,
                            :legacy_status, :row_sha256, :original_fields_json,
                            :candidate_state
                        )"""
                    ),
                    expected_candidates,
                )
        canonical_inside = {
            name: int(await session.scalar(text(f"SELECT COUNT(*) FROM {table}")) or 0)
            for name, table in (
                ("events", "attention_thread_events"),
                ("heads", "attention_thread_heads"),
                ("focus", "attention_instance_focus"),
            )
        }
        if canonical_inside != canonical_before:
            raise AttentionLegacyMigrationError(
                "legacy attention import attempted to change canonical authority"
            )

    await runtime.validate_writer()
    canonical_after = await _canonical_authority_counts(runtime)
    if canonical_after != canonical_before:
        raise AttentionLegacyMigrationError(
            "legacy attention import changed canonical authority"
        )
    verification = await verify_legacy_attention_import(source_path, runtime)
    return AttentionLegacyCopyReport(
        snapshot_sha256=snapshot.sha256,
        byte_length=snapshot.byte_length,
        row_count=len(snapshot.rows),
        rows_root_sha256=_rows_root(snapshot),
        canonical_event_count_before=canonical_before["events"],
        canonical_event_count_after=canonical_after["events"],
        canonical_head_count=canonical_after["heads"],
        canonical_focus_count=canonical_after["focus"],
        canonical_root_sha256=_canonical_authority_root(canonical_after),
        idempotent_replay=idempotent,
        verified=bool(verification["verified"]),
    )


async def _load_database_snapshot(
    runtime: StorageBackendRuntime,
    snapshot_sha256: str,
) -> tuple[LegacyStreamsSnapshot, dict[str, Any], list[dict[str, Any]]]:
    identity = str(snapshot_sha256 or "").strip().lower()
    if len(identity) != 64 or any(char not in "0123456789abcdef" for char in identity):
        raise ValueError("snapshot_sha256 must be 64 hexadecimal characters")
    async with runtime.unit_of_work() as uow:
        stored = (
            (
                await uow.session.execute(
                    text(
                        """SELECT snapshot_sha256, legacy_schema_version,
                        legacy_global_revision, byte_length, raw_bytes, row_count,
                        status_counts_json, rows_root_sha256, import_mode,
                        generation_eligible, source_label
                        FROM attention_legacy_snapshots
                        WHERE snapshot_sha256 = :snapshot_sha256"""
                    ),
                    {"snapshot_sha256": identity},
                )
            )
            .mappings()
            .one_or_none()
        )
        if stored is None:
            raise KeyError(identity)
        rows = (
            (
                await uow.session.execute(
                    text(
                        """SELECT snapshot_sha256, source_ordinal,
                        legacy_stream_id, legacy_status, row_sha256,
                        original_fields_json, candidate_state
                        FROM attention_legacy_candidates
                        WHERE snapshot_sha256 = :snapshot_sha256
                        ORDER BY source_ordinal"""
                    ),
                    {"snapshot_sha256": identity},
                )
            )
            .mappings()
            .all()
        )
    stored_dict = dict(stored)
    snapshot = _snapshot_from_exact_bytes(_bytes(stored_dict["raw_bytes"]))
    return snapshot, stored_dict, [_normalize_candidate_row(dict(row)) for row in rows]


async def verify_legacy_attention_import(
    source_path: str | Path,
    runtime: StorageBackendRuntime,
) -> dict[str, Any]:
    """Prove exact bytes and every snapshot-only row equal the legacy source."""

    source = read_legacy_streams_snapshot(source_path)
    target, stored, candidates = await _load_database_snapshot(runtime, source.sha256)
    expected_snapshot = _snapshot_row(
        source,
        source_label=str(stored["source_label"]),
    )
    expected_candidates = _candidate_rows(source)
    exact_bytes_match = target.raw_bytes == source.raw_bytes
    row_match = candidates == expected_candidates
    metadata_match = _stored_snapshot_matches(stored, expected_snapshot)
    return {
        "verified": exact_bytes_match and row_match and metadata_match,
        "snapshot_sha256": source.sha256,
        "byte_length": source.byte_length,
        "row_count": len(source.rows),
        "rows_root_sha256": _rows_root(source),
        "exact_bytes_match": exact_bytes_match,
        "candidate_rows_match": row_match,
        "metadata_match": metadata_match,
        "import_mode": ATTENTION_LEGACY_IMPORT_MODE,
        "generation_eligible": False,
    }


async def export_legacy_attention_snapshot(
    runtime: StorageBackendRuntime,
    *,
    snapshot_sha256: str,
    archive_directory: str | Path,
) -> AttentionLegacyArchiveReport:
    """Reverse-export the exact archived bytes into a new verified directory."""

    snapshot, stored, candidates = await _load_database_snapshot(
        runtime,
        snapshot_sha256,
    )
    if candidates != _candidate_rows(snapshot):
        raise AttentionLegacyMigrationError(
            "stored legacy attention candidate rows are not reversible"
        )
    if not _stored_snapshot_matches(
        stored,
        _snapshot_row(snapshot, source_label=str(stored["source_label"])),
    ):
        raise AttentionLegacyMigrationError(
            "stored legacy attention metadata is not reversible"
        )
    with tempfile.TemporaryDirectory(prefix="elysium-attention-export-") as temporary:
        source = Path(temporary) / _ARCHIVE_FILE_NAME
        source.write_bytes(snapshot.raw_bytes)
        report = create_legacy_attention_archive(
            source,
            archive_directory,
            source_label=str(stored["source_label"]),
        )
    exported, _ = load_legacy_attention_archive(archive_directory)
    if exported.raw_bytes != snapshot.raw_bytes:
        raise AttentionLegacyMigrationError(
            "reverse-exported legacy attention bytes differ"
        )
    return report


__all__ = [
    "ATTENTION_LEGACY_ARCHIVE_SCHEMA_VERSION",
    "ATTENTION_LEGACY_IMPORT_MODE",
    "ATTENTION_LEGACY_MAX_ARCHIVE_BYTES",
    "ATTENTION_LEGACY_SOURCE_LABEL",
    "AttentionLegacyArchiveReport",
    "AttentionLegacyCopyReport",
    "AttentionLegacyMigrationError",
    "create_legacy_attention_archive",
    "export_legacy_attention_snapshot",
    "import_legacy_attention_snapshot",
    "load_legacy_attention_archive",
    "verify_legacy_attention_import",
]
