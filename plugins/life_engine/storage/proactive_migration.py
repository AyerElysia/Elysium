"""Verified offline copy for the single proactive authority.

Only a frozen, independently verified life snapshot may be copied.  The copy
is resumable when every already-present row is byte-semantically identical;
any extra or divergent proactive row fails closed.  A content-free immutable
migration certificate is appended only after the target root equals the
source root.  Normal runtime startup consumes that certificate to create the
new backend binding; this module never activates a generation or a process.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from sqlalchemy import text

from src.kernel.storage import canonical_json

from ..proactive.history import (
    PROACTIVE_HISTORY_ROOT_NAME,
    PROACTIVE_MIGRATION_EVENT_KIND,
    PROACTIVE_MIGRATION_NAMESPACE,
    copy_missing_proactive_rows,
    read_proactive_history_in_session,
    read_runtime_proactive_history,
    read_sqlite_proactive_history,
)
from .contracts import StorageBackendRuntime, StorageWriterRole
from .migration.manifest import load_snapshot_manifest
from .migration.snapshot import sha256_file
from .migration.verify import verify_local_snapshot
from .models import BackendKind

PROACTIVE_SNAPSHOT_SOURCE = PurePosixPath(
    "life_engine_workspace/runtime/proactive/proactive.sqlite3"
)
PROACTIVE_MIGRATION_SCHEMA_VERSION = 1


class ProactiveAuthorityMigrationError(RuntimeError):
    """Raised when source, target, or migration evidence cannot be proven."""


@dataclass(frozen=True, slots=True)
class ProactiveAuthorityCopyReport:
    """Content-free proof for one frozen proactive authority copy."""

    migration_id: str
    source_manifest_sha256: str
    source_snapshot_sha256: str
    source_database_sha256: str
    source_binding_sha256: str
    source_root_sha256: str
    target_root_sha256: str
    table_count: int
    row_count: int
    copied_row_count: int
    idempotent_replay: bool
    migration_certificate_sha256: str
    target_backend: str
    target_backend_identity_sha256: str
    writer_frozen: bool
    verified: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _snapshot_database(
    snapshot_directory: Path,
    manifest: dict[str, Any],
    source_relative: PurePosixPath,
) -> tuple[Path, dict[str, Any]]:
    identity = source_relative.as_posix()
    matches = [
        dict(item)
        for item in list(manifest.get("sqlite") or [])
        if str(dict(item).get("source_relative") or "") == identity
    ]
    if len(matches) != 1:
        raise ProactiveAuthorityMigrationError(
            "ProactiveSnapshotDatabaseEvidenceMissing"
        )
    evidence = matches[0]
    backup_relative = PurePosixPath(str(evidence.get("backup_relative") or ""))
    if backup_relative.is_absolute() or ".." in backup_relative.parts:
        raise ProactiveAuthorityMigrationError("ProactiveSnapshotPathInvalid")
    database = snapshot_directory.joinpath(*backup_relative.parts).resolve()
    try:
        database.relative_to(snapshot_directory)
    except ValueError as exc:
        raise ProactiveAuthorityMigrationError(
            "ProactiveSnapshotPathEscapesRoot"
        ) from exc
    if not database.is_file():
        raise ProactiveAuthorityMigrationError("ProactiveSnapshotDatabaseMissing")
    if sha256_file(database) != str(evidence.get("sha256") or ""):
        raise ProactiveAuthorityMigrationError(
            "ProactiveSnapshotDatabaseChecksumMismatch"
        )
    return database, evidence


def _decode_binding_payload(raw: Any, expected_sha256: Any) -> dict[str, Any]:
    value = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
    if hashlib.sha256(value.encode("utf-8")).hexdigest() != str(
        expected_sha256 or ""
    ):
        raise ProactiveAuthorityMigrationError("ProactiveSourceBindingCorrupt")
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ProactiveAuthorityMigrationError(
            "ProactiveSourceBindingUnreadable"
        ) from exc
    if not isinstance(payload, dict):
        raise ProactiveAuthorityMigrationError("ProactiveSourceBindingInvalid")
    claimed = str(payload.get("binding_sha256") or "")
    body = dict(payload)
    body.pop("binding_sha256", None)
    actual = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
    if claimed != actual:
        raise ProactiveAuthorityMigrationError(
            "ProactiveSourceBindingDigestMismatch"
        )
    return dict(payload)


def _source_binding(database: Path) -> dict[str, Any]:
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """SELECT position, occurrence_id, event_kind, payload_json,
                payload_sha256 FROM runtime_events
                WHERE namespace = 'life_proactive.backend_binding'
                ORDER BY position"""
        ).fetchall()
        head = connection.execute(
            """SELECT revision, schema_version, payload_json, payload_sha256
                FROM runtime_states
                WHERE namespace = 'life_proactive.backend_binding'
                  AND state_key = 'active'"""
        ).fetchone()
    except sqlite3.DatabaseError as exc:
        raise ProactiveAuthorityMigrationError(
            "ProactiveSourceBindingMissing"
        ) from exc
    finally:
        connection.close()
    if not rows or head is None:
        raise ProactiveAuthorityMigrationError("ProactiveSourceBindingMissing")
    chain: list[dict[str, Any]] = []
    previous = ""
    for epoch, row in enumerate(rows, start=1):
        if str(row["event_kind"]) != "proactive_backend_bound":
            raise ProactiveAuthorityMigrationError(
                "ProactiveSourceBindingChainInvalid"
            )
        payload = _decode_binding_payload(row["payload_json"], row["payload_sha256"])
        digest = str(payload.get("binding_sha256") or "")
        if (
            int(payload.get("schema_version") or 0) != 3
            or int(payload.get("binding_epoch") or 0) != epoch
            or str(payload.get("previous_binding_sha256") or "") != previous
            or str(row["occurrence_id"])
            != f"proactive:backend-binding:{digest}"
        ):
            raise ProactiveAuthorityMigrationError(
                "ProactiveSourceBindingChainInvalid"
            )
        chain.append(payload)
        previous = digest
    head_payload = _decode_binding_payload(
        head["payload_json"], head["payload_sha256"]
    )
    if (
        int(head["schema_version"]) != 3
        or int(head["revision"]) != len(chain)
        or head_payload != chain[-1]
    ):
        raise ProactiveAuthorityMigrationError("ProactiveSourceBindingHeadInvalid")
    return chain[-1]


def _migration_occurrence(source_snapshot_sha256: str, root_sha256: str) -> str:
    digest = hashlib.sha256(
        f"{source_snapshot_sha256}\0{root_sha256}".encode()
    ).hexdigest()
    return f"proactive:migration:{digest}"


def _bind_time(backend: BackendKind, value: str) -> Any:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    parsed = parsed.astimezone(UTC)
    if backend == BackendKind.MYSQL:
        return parsed.replace(tzinfo=None)
    return parsed.isoformat()


def _certificate_payload(
    *,
    migration_id: str,
    manifest: dict[str, Any],
    source_database_sha256: str,
    source_binding: dict[str, Any],
    source_root_sha256: str,
    target: StorageBackendRuntime,
    target_evidence: dict[str, Any],
) -> dict[str, Any]:
    source_identity = source_binding.get("identity")
    if not isinstance(source_identity, dict):
        raise ProactiveAuthorityMigrationError("ProactiveSourceBindingIdentityMissing")
    return {
        "schema_version": PROACTIVE_MIGRATION_SCHEMA_VERSION,
        "migration_id": migration_id,
        "source_manifest_sha256": str(manifest["manifest_sha256"]),
        "source_snapshot_sha256": str(manifest["source_snapshot_sha256"]),
        "source_database_sha256": source_database_sha256,
        "source_binding_sha256": str(source_binding["binding_sha256"]),
        "source_identity_sha256": hashlib.sha256(
            canonical_json(source_identity).encode("utf-8")
        ).hexdigest(),
        "source_root_sha256": source_root_sha256,
        "target_root_sha256": str(target_evidence["root_sha256"]),
        "target_backend": target.backend.value,
        "target_backend_identity_sha256": hashlib.sha256(
            target.backend_identity.encode("utf-8")
        ).hexdigest(),
        "history_algorithm_version": str(target_evidence["algorithm_version"]),
        "table_roots": {
            str(item["name"]): str(item["root_sha256"])
            for item in list(target_evidence["tables"])
        },
        "table_row_counts": {
            str(item["name"]): int(item["row_count"])
            for item in list(target_evidence["tables"])
        },
        "frontiers": dict(target_evidence["frontiers"]),
        "writer_frozen": True,
        "verified": True,
    }


async def _append_certificate(
    runtime: StorageBackendRuntime,
    session: Any,
    *,
    payload: dict[str, Any],
    occurred_at: str,
) -> str:
    encoded = canonical_json(payload)
    payload_sha256 = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    occurrence_id = _migration_occurrence(
        str(payload["source_snapshot_sha256"]),
        str(payload["target_root_sha256"]),
    )
    prefix = "INSERT IGNORE" if runtime.backend == BackendKind.MYSQL else "INSERT OR IGNORE"
    await session.execute(
        text(
            f"""{prefix} INTO runtime_events (
                namespace, occurrence_id, event_kind, payload_json,
                payload_sha256, occurred_at, recorded_at
            ) VALUES (
                :namespace, :occurrence_id, :event_kind, :payload_json,
                :payload_sha256, :occurred_at, :recorded_at
            )"""
        ),
        {
            "namespace": PROACTIVE_MIGRATION_NAMESPACE,
            "occurrence_id": occurrence_id,
            "event_kind": PROACTIVE_MIGRATION_EVENT_KIND,
            "payload_json": encoded,
            "payload_sha256": payload_sha256,
            "occurred_at": _bind_time(runtime.backend, occurred_at),
            "recorded_at": _bind_time(runtime.backend, occurred_at),
        },
    )
    row = (
        (
            await session.execute(
                text(
                    """SELECT namespace, event_kind, payload_json, payload_sha256
                    FROM runtime_events WHERE occurrence_id = :occurrence_id"""
                ),
                {"occurrence_id": occurrence_id},
            )
        )
        .mappings()
        .one()
    )
    stored_raw = (
        row["payload_json"].decode("utf-8")
        if isinstance(row["payload_json"], bytes)
        else str(row["payload_json"])
    )
    if (
        str(row["namespace"]) != PROACTIVE_MIGRATION_NAMESPACE
        or str(row["event_kind"]) != PROACTIVE_MIGRATION_EVENT_KIND
        or stored_raw != encoded
        or str(row["payload_sha256"]) != payload_sha256
    ):
        raise ProactiveAuthorityMigrationError(
            "ProactiveMigrationCertificateConflict"
        )
    return payload_sha256


async def copy_proactive_authority_from_snapshot(
    snapshot_directory: str | Path,
    runtime: StorageBackendRuntime,
    *,
    migration_id: str,
    source_relative: PurePosixPath = PROACTIVE_SNAPSHOT_SOURCE,
) -> ProactiveAuthorityCopyReport:
    """Copy and certify one frozen proactive authority into a candidate runtime."""

    identity = str(migration_id or "").strip()
    if not identity or len(identity) > 255:
        raise ValueError("migration_id must be 1..255 characters")
    if runtime.writer_role != StorageWriterRole.CANDIDATE_COPY:
        raise ProactiveAuthorityMigrationError(
            "ProactiveMigrationCandidateWriterRequired"
        )
    snapshot = Path(snapshot_directory).resolve(strict=True)
    manifest = load_snapshot_manifest(snapshot / "manifest.json")
    verification = verify_local_snapshot(snapshot)
    if not bool(verification.get("verified")):
        raise ProactiveAuthorityMigrationError(
            "ProactiveMigrationSnapshotVerificationFailed"
        )
    if not bool(manifest.get("writer_frozen")):
        raise ProactiveAuthorityMigrationError(
            "ProactiveMigrationWriterFreezeRequired"
        )
    database, database_evidence = _snapshot_database(
        snapshot,
        manifest,
        source_relative,
    )
    source = read_sqlite_proactive_history(database)
    source_binding = _source_binding(database)
    await runtime.validate_writer()
    occurred_at = str(manifest.get("created_at") or "")
    if not occurred_at:
        raise ProactiveAuthorityMigrationError("ProactiveSnapshotCreatedAtMissing")

    async with runtime.unit_of_work() as uow:
        copied, idempotent = await copy_missing_proactive_rows(
            uow.session,
            runtime,
            source,
        )
        target = await read_proactive_history_in_session(uow.session)
        if target.root_sha256 != source.root_sha256:
            raise ProactiveAuthorityMigrationError(
                "ProactiveMigrationRootMismatch"
            )
        payload = _certificate_payload(
            migration_id=identity,
            manifest=manifest,
            source_database_sha256=str(database_evidence["sha256"]),
            source_binding=source_binding,
            source_root_sha256=source.root_sha256,
            target=runtime,
            target_evidence=target.evidence(),
        )
        certificate_sha256 = await _append_certificate(
            runtime,
            uow.session,
            payload=payload,
            occurred_at=occurred_at,
        )

    await runtime.validate_writer()
    target = await read_runtime_proactive_history(runtime)
    if target.root_sha256 != source.root_sha256:
        raise ProactiveAuthorityMigrationError(
            "ProactiveMigrationPostCommitRootMismatch"
        )
    return ProactiveAuthorityCopyReport(
        migration_id=identity,
        source_manifest_sha256=str(manifest["manifest_sha256"]),
        source_snapshot_sha256=str(manifest["source_snapshot_sha256"]),
        source_database_sha256=str(database_evidence["sha256"]),
        source_binding_sha256=str(source_binding["binding_sha256"]),
        source_root_sha256=source.root_sha256,
        target_root_sha256=target.root_sha256,
        table_count=len(target.tables),
        row_count=target.row_count,
        copied_row_count=copied,
        idempotent_replay=idempotent,
        migration_certificate_sha256=certificate_sha256,
        target_backend=runtime.backend.value,
        target_backend_identity_sha256=hashlib.sha256(
            runtime.backend_identity.encode("utf-8")
        ).hexdigest(),
        writer_frozen=True,
        verified=True,
    )


async def verify_proactive_authority_copy(
    snapshot_directory: str | Path,
    runtime: StorageBackendRuntime,
    *,
    source_relative: PurePosixPath = PROACTIVE_SNAPSHOT_SOURCE,
) -> dict[str, Any]:
    """Independently compare source and target roots without changing either."""

    snapshot = Path(snapshot_directory).resolve(strict=True)
    manifest = load_snapshot_manifest(snapshot / "manifest.json")
    database, database_evidence = _snapshot_database(
        snapshot,
        manifest,
        source_relative,
    )
    source = read_sqlite_proactive_history(database)
    target = await read_runtime_proactive_history(runtime)
    return {
        "verified": (
            bool(manifest.get("writer_frozen"))
            and source.root_sha256 == target.root_sha256
            and source.row_count == target.row_count
        ),
        "writer_frozen": bool(manifest.get("writer_frozen")),
        "source_manifest_sha256": str(manifest["manifest_sha256"]),
        "source_snapshot_sha256": str(manifest["source_snapshot_sha256"]),
        "source_database_sha256": str(database_evidence["sha256"]),
        "source_root_sha256": source.root_sha256,
        "target_root_sha256": target.root_sha256,
        "source_row_count": source.row_count,
        "target_row_count": target.row_count,
        "canonical_authority": {
            "generation_eligible": bool(manifest.get("writer_frozen"))
            and source.root_sha256 == target.root_sha256,
            "root_sha256": target.root_sha256,
            "row_count": target.row_count,
            "frontiers": dict(target.frontiers),
            "root_name": PROACTIVE_HISTORY_ROOT_NAME,
        },
    }


__all__ = [
    "PROACTIVE_MIGRATION_SCHEMA_VERSION",
    "PROACTIVE_SNAPSHOT_SOURCE",
    "ProactiveAuthorityCopyReport",
    "ProactiveAuthorityMigrationError",
    "copy_proactive_authority_from_snapshot",
    "verify_proactive_authority_copy",
]
