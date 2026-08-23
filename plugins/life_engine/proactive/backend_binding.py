"""Fail-closed binding between proactive history and one storage generation.

The database carries the authority anchor as an append-only hash chain plus a
current head.  The workspace JSON file is only a recoverable local cache.  A
deleted cache is rebuilt from the database; a conflicting cache, database
anchor, or generation always fails closed.  Runtime startup never performs a
backend migration.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.runtime.single_instance import SingleInstanceLock
from src.kernel.storage import canonical_json

from ..storage.authority import FileAuthorityRegistry, MySQLAuthorityRegistry
from ..storage.contracts import StorageBackendRuntime
from ..storage.models import BackendKind
from .history import (
    PROACTIVE_MIGRATION_EVENT_KIND,
    PROACTIVE_MIGRATION_NAMESPACE,
    read_proactive_history_in_session,
)

_SCHEMA_VERSION = 3
_LEGACY_SCHEMA_VERSIONS = frozenset({1, 2})
_BINDING_NAMESPACE = "life_proactive.backend_binding"
_BINDING_STATE_KEY = "active"


class ProactiveBackendBindingConflict(RuntimeError):
    """The workspace or durable history is bound to another authority."""


def _inside_workspace(workspace: Path, value: str) -> Path:
    candidate = Path(str(value or "").strip())
    if not str(candidate):
        raise ValueError("proactive backend binding path must not be empty")
    resolved = (
        candidate.resolve()
        if candidate.is_absolute()
        else (workspace / candidate).resolve()
    )
    try:
        resolved.relative_to(workspace)
    except ValueError as exc:
        raise ValueError(
            "proactive backend binding path must stay inside the workspace"
        ) from exc
    return resolved


def _identity(runtime: StorageBackendRuntime) -> dict[str, object]:
    generation = runtime.generation
    registry = runtime.authority_registry
    token = runtime.authority_token
    if generation is None or registry is None or token is None:
        raise RuntimeError("ProactiveBackendGenerationRequired")
    if token.registry_id != getattr(registry, "registry_id", ""):
        raise RuntimeError("ProactiveBackendAuthorityRegistryMismatch")
    if isinstance(registry, FileAuthorityRegistry):
        authority_provider = "file"
        provider_material = {
            "provider": authority_provider,
            "registry_id": token.registry_id,
            "state_path_sha256": hashlib.sha256(
                str(registry.state_path.resolve()).encode("utf-8")
            ).hexdigest(),
        }
    elif isinstance(registry, MySQLAuthorityRegistry):
        authority_provider = "mysql"
        provider_material = {
            "provider": authority_provider,
            "registry_id": token.registry_id,
            "backend_identity_sha256": hashlib.sha256(
                runtime.backend_identity.encode("utf-8")
            ).hexdigest(),
        }
    else:
        raise TypeError("ProactiveBackendAuthorityProviderUnsupported")
    return {
        "backend": runtime.backend.value,
        "backend_identity_sha256": hashlib.sha256(
            runtime.backend_identity.encode("utf-8")
        ).hexdigest(),
        "generation_id": generation.generation_id,
        "generation_source_sha256": generation.source_snapshot_sha256,
        "generation_manifest_sha256": generation.manifest_sha256,
        "authority_registry_id": token.registry_id,
        "authority_provider": authority_provider,
        "authority_provider_identity_sha256": hashlib.sha256(
            canonical_json(provider_material).encode("utf-8")
        ).hexdigest(),
        "scope": "proactive_authority",
    }


def _binding_payload(
    identity: dict[str, object],
    *,
    binding_epoch: int,
    previous_binding_sha256: str,
    migration: dict[str, object] | None = None,
) -> dict[str, object]:
    base: dict[str, object] = {
        "schema_version": _SCHEMA_VERSION,
        "binding_epoch": int(binding_epoch),
        "previous_binding_sha256": str(previous_binding_sha256),
        "identity": dict(identity),
    }
    if migration is not None:
        base["migration"] = dict(migration)
    digest = hashlib.sha256(canonical_json(base).encode("utf-8")).hexdigest()
    return {**base, "binding_sha256": digest}


def _payload_digest(payload: dict[str, object]) -> str:
    base = dict(payload)
    claimed = str(base.pop("binding_sha256", ""))
    actual = hashlib.sha256(canonical_json(base).encode("utf-8")).hexdigest()
    if claimed != actual:
        raise ProactiveBackendBindingConflict(
            "ProactiveBackendBindingDigestMismatch"
        )
    return actual


def _decode_payload(raw_value: Any, expected_sha256: Any) -> dict[str, object]:
    raw = (
        raw_value.decode("utf-8")
        if isinstance(raw_value, (bytes, bytearray))
        else str(raw_value)
    )
    if hashlib.sha256(raw.encode("utf-8")).hexdigest() != str(
        expected_sha256 or ""
    ):
        raise ProactiveBackendBindingConflict(
            "ProactiveBackendBindingRecordCorrupt"
        )
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProactiveBackendBindingConflict(
            "ProactiveBackendBindingRecordUnreadable"
        ) from exc
    if not isinstance(value, dict):
        raise ProactiveBackendBindingConflict(
            "ProactiveBackendBindingRecordInvalid"
        )
    payload = dict(value)
    if int(payload.get("schema_version") or 0) != _SCHEMA_VERSION:
        raise ProactiveBackendBindingConflict(
            "ProactiveBackendBindingSchemaUnsupported"
        )
    _payload_digest(payload)
    return payload


def _legacy_identity(marker: dict[str, object]) -> dict[str, object]:
    source = marker.get("identity")
    if not isinstance(source, dict):
        source = marker
    return {
        key: source.get(key)
        for key in (
            "backend",
            "backend_identity_sha256",
            "generation_id",
            "generation_source_sha256",
            "scope",
        )
    }


def _marker_matches_identity(
    marker: dict[str, object],
    identity: dict[str, object],
) -> bool:
    schema_version = int(marker.get("schema_version") or 0)
    if schema_version in _LEGACY_SCHEMA_VERSIONS:
        legacy = _legacy_identity(marker)
        return all(identity.get(key) == value for key, value in legacy.items())
    if schema_version != _SCHEMA_VERSION:
        return False
    marker_identity = marker.get("identity")
    return isinstance(marker_identity, dict) and marker_identity == identity


def _read_marker(path: Path) -> dict[str, object] | None:
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProactiveBackendBindingConflict(
            "ProactiveBackendBindingUnreadable"
        ) from exc
    if not isinstance(value, dict):
        raise ProactiveBackendBindingConflict(
            "ProactiveBackendBindingInvalid"
        )
    return dict(value)


def _write_atomic(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    encoded = (canonical_json(payload) + "\n").encode("utf-8")
    try:
        with temporary.open("xb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
        if os.name != "nt":
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        if temporary.exists():
            temporary.unlink()


def _insert_prefix(backend: BackendKind) -> str:
    return "INSERT IGNORE" if backend == BackendKind.MYSQL else "INSERT OR IGNORE"


def _bind_time(runtime: StorageBackendRuntime, value: Any) -> Any:
    if runtime.backend == BackendKind.MYSQL and hasattr(value, "replace"):
        return value.replace(tzinfo=None)
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


async def _database_now(
    session: AsyncSession,
    runtime: StorageBackendRuntime,
) -> Any:
    statement = (
        "SELECT CURRENT_TIMESTAMP(6)"
        if runtime.backend == BackendKind.MYSQL
        else "SELECT STRFTIME('%Y-%m-%dT%H:%M:%f+00:00', 'now')"
    )
    return await session.scalar(text(statement))


async def _load_chain(session: AsyncSession) -> list[dict[str, object]]:
    rows = (
        (
            await session.execute(
                text(
                    """SELECT position, occurrence_id, event_kind,
                        payload_json, payload_sha256
                    FROM runtime_events WHERE namespace = :namespace
                    ORDER BY position"""
                ),
                {"namespace": _BINDING_NAMESPACE},
            )
        )
        .mappings()
        .all()
    )
    chain: list[dict[str, object]] = []
    previous = ""
    for expected_epoch, row in enumerate(rows, start=1):
        if str(row["event_kind"]) != "proactive_backend_bound":
            raise ProactiveBackendBindingConflict(
                "ProactiveBackendBindingEventKindInvalid"
            )
        payload = _decode_payload(row["payload_json"], row["payload_sha256"])
        digest = str(payload["binding_sha256"])
        if (
            int(payload.get("binding_epoch") or 0) != expected_epoch
            or str(payload.get("previous_binding_sha256") or "") != previous
            or str(row["occurrence_id"])
            != f"proactive:backend-binding:{digest}"
        ):
            raise ProactiveBackendBindingConflict(
                "ProactiveBackendBindingChainBroken"
            )
        chain.append(payload)
        previous = digest
    return chain


async def _load_head(session: AsyncSession) -> dict[str, object] | None:
    row = (
        (
            await session.execute(
                text(
                    """SELECT revision, schema_version, payload_json,
                        payload_sha256 FROM runtime_states
                    WHERE namespace = :namespace AND state_key = :state_key"""
                ),
                {
                    "namespace": _BINDING_NAMESPACE,
                    "state_key": _BINDING_STATE_KEY,
                },
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        return None
    payload = _decode_payload(row["payload_json"], row["payload_sha256"])
    if (
        int(row["revision"]) != int(payload["binding_epoch"])
        or int(row["schema_version"]) != _SCHEMA_VERSION
    ):
        raise ProactiveBackendBindingConflict(
            "ProactiveBackendBindingHeadCorrupt"
        )
    return payload


async def _store_head(
    session: AsyncSession,
    runtime: StorageBackendRuntime,
    payload: dict[str, object],
) -> None:
    encoded = canonical_json(payload)
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    now = await _database_now(session, runtime)
    await session.execute(
        text(
            f"""{_insert_prefix(runtime.backend)} INTO runtime_states (
                namespace, state_key, revision, schema_version,
                payload_json, payload_sha256, updated_at
            ) VALUES (
                :namespace, :state_key, :revision, :schema_version,
                :payload_json, :payload_sha256, :updated_at
            )"""
        ),
        {
            "namespace": _BINDING_NAMESPACE,
            "state_key": _BINDING_STATE_KEY,
            "revision": int(payload["binding_epoch"]),
            "schema_version": _SCHEMA_VERSION,
            "payload_json": encoded,
            "payload_sha256": digest,
            "updated_at": _bind_time(runtime, now),
        },
    )
    stored = await _load_head(session)
    if stored != payload:
        raise ProactiveBackendBindingConflict(
            "ProactiveBackendBindingHeadConflict"
        )


async def _advance_head(
    session: AsyncSession,
    runtime: StorageBackendRuntime,
    *,
    previous: dict[str, object],
    current: dict[str, object],
) -> None:
    """Advance the mutable head with an exact previous-anchor CAS."""

    encoded = canonical_json(current)
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    previous_encoded = canonical_json(previous)
    previous_digest = hashlib.sha256(
        previous_encoded.encode("utf-8")
    ).hexdigest()
    now = await _database_now(session, runtime)
    result = await session.execute(
        text(
            """UPDATE runtime_states SET revision = :revision,
                schema_version = :schema_version,
                payload_json = :payload_json,
                payload_sha256 = :payload_sha256,
                updated_at = :updated_at
            WHERE namespace = :namespace AND state_key = :state_key
              AND revision = :expected_revision
              AND payload_sha256 = :expected_payload_sha256"""
        ),
        {
            "namespace": _BINDING_NAMESPACE,
            "state_key": _BINDING_STATE_KEY,
            "revision": int(current["binding_epoch"]),
            "schema_version": _SCHEMA_VERSION,
            "payload_json": encoded,
            "payload_sha256": digest,
            "updated_at": _bind_time(runtime, now),
            "expected_revision": int(previous["binding_epoch"]),
            "expected_payload_sha256": previous_digest,
        },
    )
    if int(result.rowcount or 0) != 1:
        raise ProactiveBackendBindingConflict(
            "ProactiveBackendBindingHeadAdvanceConflict"
        )
    stored = await _load_head(session)
    if stored != current:
        raise ProactiveBackendBindingConflict(
            "ProactiveBackendBindingHeadAdvanceLost"
        )


async def _append_initial_anchor(
    session: AsyncSession,
    runtime: StorageBackendRuntime,
    identity: dict[str, object],
    *,
    migration: dict[str, object] | None = None,
) -> dict[str, object]:
    payload = _binding_payload(
        identity,
        binding_epoch=1,
        previous_binding_sha256="",
        migration=migration,
    )
    encoded = canonical_json(payload)
    payload_sha256 = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    binding_sha256 = str(payload["binding_sha256"])
    now = await _database_now(session, runtime)
    await session.execute(
        text(
            f"""{_insert_prefix(runtime.backend)} INTO runtime_events (
                namespace, occurrence_id, event_kind, payload_json,
                payload_sha256, occurred_at, recorded_at
            ) VALUES (
                :namespace, :occurrence_id, :event_kind, :payload_json,
                :payload_sha256, :occurred_at, :recorded_at
            )"""
        ),
        {
            "namespace": _BINDING_NAMESPACE,
            "occurrence_id": f"proactive:backend-binding:{binding_sha256}",
            "event_kind": "proactive_backend_bound",
            "payload_json": encoded,
            "payload_sha256": payload_sha256,
            "occurred_at": _bind_time(runtime, now),
            "recorded_at": _bind_time(runtime, now),
        },
    )
    chain = await _load_chain(session)
    if chain != [payload]:
        raise ProactiveBackendBindingConflict(
            "ProactiveBackendBindingCreationConflict"
        )
    await _store_head(session, runtime, payload)
    return payload


async def _append_rebinding_anchor(
    session: AsyncSession,
    runtime: StorageBackendRuntime,
    identity: dict[str, object],
    *,
    previous: dict[str, object],
    migration: dict[str, object],
) -> dict[str, object]:
    """Append one verified backend reactivation to an existing chain."""

    payload = _binding_payload(
        identity,
        binding_epoch=int(previous["binding_epoch"]) + 1,
        previous_binding_sha256=str(previous["binding_sha256"]),
        migration=migration,
    )
    encoded = canonical_json(payload)
    payload_sha256 = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    binding_sha256 = str(payload["binding_sha256"])
    now = await _database_now(session, runtime)
    await session.execute(
        text(
            f"""{_insert_prefix(runtime.backend)} INTO runtime_events (
                namespace, occurrence_id, event_kind, payload_json,
                payload_sha256, occurred_at, recorded_at
            ) VALUES (
                :namespace, :occurrence_id, :event_kind, :payload_json,
                :payload_sha256, :occurred_at, :recorded_at
            )"""
        ),
        {
            "namespace": _BINDING_NAMESPACE,
            "occurrence_id": f"proactive:backend-binding:{binding_sha256}",
            "event_kind": "proactive_backend_bound",
            "payload_json": encoded,
            "payload_sha256": payload_sha256,
            "occurred_at": _bind_time(runtime, now),
            "recorded_at": _bind_time(runtime, now),
        },
    )
    chain = await _load_chain(session)
    if (
        len(chain) != int(payload["binding_epoch"])
        or chain[-2] != previous
        or chain[-1] != payload
    ):
        raise ProactiveBackendBindingConflict(
            "ProactiveBackendRebindingChainConflict"
        )
    await _advance_head(
        session,
        runtime,
        previous=previous,
        current=payload,
    )
    return payload


async def _history_count(session: AsyncSession) -> int:
    # Binding must precede *every* canonical proactive row, not merely the two
    # event ledgers.  Counting through the same backend-neutral inventory used
    # by migration also catches orphan heads/focus/inbox/turn rows and prevents
    # a missing marker from laundering partially written history into a fresh
    # authority generation.
    return (await read_proactive_history_in_session(session)).row_count


def _decode_migration_payload(raw_value: Any, expected_sha256: Any) -> dict[str, Any]:
    raw = (
        raw_value.decode("utf-8")
        if isinstance(raw_value, (bytes, bytearray))
        else str(raw_value)
    )
    if hashlib.sha256(raw.encode("utf-8")).hexdigest() != str(
        expected_sha256 or ""
    ):
        raise ProactiveBackendBindingConflict(
            "ProactiveMigrationCertificateCorrupt"
        )
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProactiveBackendBindingConflict(
            "ProactiveMigrationCertificateUnreadable"
        ) from exc
    if not isinstance(payload, dict):
        raise ProactiveBackendBindingConflict(
            "ProactiveMigrationCertificateInvalid"
        )
    return dict(payload)


async def _verified_migration_certificate(
    session: AsyncSession,
    runtime: StorageBackendRuntime,
    identity: dict[str, object],
    marker: dict[str, object] | None,
) -> dict[str, Any] | None:
    """Return one exact migration proof for the selected generation, if any."""

    rows = (
        (
            await session.execute(
                text(
                    """SELECT occurrence_id, event_kind, payload_json,
                        payload_sha256 FROM runtime_events
                    WHERE namespace = :namespace ORDER BY position"""
                ),
                {"namespace": PROACTIVE_MIGRATION_NAMESPACE},
            )
        )
        .mappings()
        .all()
    )
    if not rows:
        return None
    if marker is None or int(marker.get("schema_version") or 0) != _SCHEMA_VERSION:
        raise ProactiveBackendBindingConflict(
            "ProactiveMigrationSourceBindingMissing"
        )
    source_binding_sha256 = _payload_digest(marker)
    source_identity = marker.get("identity")
    if not isinstance(source_identity, dict):
        raise ProactiveBackendBindingConflict(
            "ProactiveMigrationSourceIdentityMissing"
        )
    source_identity_sha256 = hashlib.sha256(
        canonical_json(source_identity).encode("utf-8")
    ).hexdigest()
    generation = runtime.generation
    if generation is None:
        raise ProactiveBackendBindingConflict(
            "ProactiveMigrationGenerationMissing"
        )
    history = await read_proactive_history_in_session(session)
    expected_root = str(
        generation.root_hashes.get(
            f"{runtime.backend.value}:proactive_authority",
            "",
        )
    )
    if expected_root != history.root_sha256:
        raise ProactiveBackendBindingConflict(
            "ProactiveMigrationGenerationRootMismatch"
        )
    matching: list[dict[str, Any]] = []
    source_mismatch = False
    for row in rows:
        if str(row["event_kind"]) != PROACTIVE_MIGRATION_EVENT_KIND:
            raise ProactiveBackendBindingConflict(
                "ProactiveMigrationCertificateEventKindInvalid"
            )
        payload = _decode_migration_payload(
            row["payload_json"], row["payload_sha256"]
        )
        table_roots = payload.get("table_roots")
        table_counts = payload.get("table_row_counts")
        if not isinstance(table_roots, dict) or not isinstance(table_counts, dict):
            raise ProactiveBackendBindingConflict(
                "ProactiveMigrationCertificateEvidenceMissing"
            )
        expected_tables = {
            item.name: item.root_sha256 for item in history.tables
        }
        expected_counts = {item.name: item.row_count for item in history.tables}
        target_candidate = all(
            (
                int(payload.get("schema_version") or 0) == 1,
                payload.get("writer_frozen") is True,
                payload.get("verified") is True,
                str(payload.get("source_snapshot_sha256") or "")
                == generation.source_snapshot_sha256,
                str(payload.get("source_root_sha256") or "")
                == history.root_sha256,
                str(payload.get("target_root_sha256") or "")
                == history.root_sha256,
                str(payload.get("target_backend") or "")
                == runtime.backend.value,
                str(payload.get("target_backend_identity_sha256") or "")
                == str(identity["backend_identity_sha256"]),
                str(payload.get("history_algorithm_version") or "")
                == history.algorithm_version,
                dict(table_roots) == expected_tables,
                {str(key): int(value) for key, value in dict(table_counts).items()}
                == expected_counts,
            )
        )
        source_matches = all(
            (
                str(payload.get("source_binding_sha256") or "")
                == source_binding_sha256,
                str(payload.get("source_identity_sha256") or "")
                == source_identity_sha256,
            )
        )
        candidate = target_candidate and source_matches
        if target_candidate and not source_matches:
            source_mismatch = True
        if candidate:
            expected_occurrence = hashlib.sha256(
                (
                    str(payload.get("source_snapshot_sha256") or "")
                    + "\0"
                    + str(payload.get("target_root_sha256") or "")
                ).encode("utf-8")
            ).hexdigest()
            if str(row["occurrence_id"]) != (
                f"proactive:migration:{expected_occurrence}"
            ):
                raise ProactiveBackendBindingConflict(
                    "ProactiveMigrationCertificateOccurrenceInvalid"
                )
            evidence = dict(payload)
            evidence["_certificate_sha256"] = str(row["payload_sha256"])
            matching.append(evidence)
    if not matching and source_mismatch:
        raise ProactiveBackendBindingConflict(
            "ProactiveMigrationSourceBindingMismatch"
        )
    if len(matching) != 1:
        raise ProactiveBackendBindingConflict(
            "ProactiveMigrationCertificateMissingOrAmbiguous"
        )
    return matching[0]


def _migration_binding_evidence(
    certificate: dict[str, Any],
) -> dict[str, object]:
    """Project one verified certificate into the append-only binding chain."""

    return {
        "migration_id": str(certificate.get("migration_id") or ""),
        "source_binding_sha256": str(
            certificate.get("source_binding_sha256") or ""
        ),
        "certificate_sha256": str(
            certificate.get("_certificate_sha256") or ""
        ),
        "source_snapshot_sha256": str(
            certificate.get("source_snapshot_sha256") or ""
        ),
        "target_root_sha256": str(
            certificate.get("target_root_sha256") or ""
        ),
    }


async def _ensure_database_anchor(
    runtime: StorageBackendRuntime,
    *,
    identity: dict[str, object],
    marker: dict[str, object] | None,
) -> dict[str, object]:
    async with runtime.unit_of_work() as uow:
        chain = await _load_chain(uow.session)
        head = await _load_head(uow.session)
        if chain:
            latest = chain[-1]
            if head is None:
                await _store_head(uow.session, runtime, latest)
                head = latest
            if head != latest:
                raise ProactiveBackendBindingConflict(
                    "ProactiveBackendBindingHeadNotLatest"
                )
            latest_matches = latest.get("identity") == identity
            if marker is None:
                if not latest_matches:
                    raise ProactiveBackendBindingConflict(
                        "ProactiveBackendSwitchRequiresVerifiedMigration"
                    )
                return latest
            if latest_matches and (marker == latest or marker in chain):
                return latest
            if (
                latest_matches
                and int(marker.get("schema_version") or 0)
                in _LEGACY_SCHEMA_VERSIONS
                and _marker_matches_identity(marker, identity)
            ):
                return latest
            migration = await _verified_migration_certificate(
                uow.session,
                runtime,
                identity,
                marker,
            )
            if migration is None:
                raise ProactiveBackendBindingConflict(
                    "ProactiveBackendSwitchRequiresVerifiedMigration"
                )
            return await _append_rebinding_anchor(
                uow.session,
                runtime,
                identity,
                previous=latest,
                migration=_migration_binding_evidence(migration),
            )
        if head is not None:
            raise ProactiveBackendBindingConflict(
                "ProactiveBackendBindingHistoryMissing"
            )
        migration = await _verified_migration_certificate(
            uow.session,
            runtime,
            identity,
            marker,
        )
        if migration is not None:
            return await _append_initial_anchor(
                uow.session,
                runtime,
                identity,
                migration=_migration_binding_evidence(migration),
            )
        history_count = await _history_count(uow.session)
        if marker is not None:
            if not _marker_matches_identity(marker, identity):
                raise ProactiveBackendBindingConflict(
                    "ProactiveBackendSwitchRequiresVerifiedMigration"
                )
            schema_version = int(marker.get("schema_version") or 0)
            if schema_version not in _LEGACY_SCHEMA_VERSIONS:
                raise ProactiveBackendBindingConflict(
                    "ProactiveBackendBindingHistoryMissing"
                )
            if history_count > 0:
                raise ProactiveBackendBindingConflict(
                    "ProactiveLegacyBindingRequiresVerifiedMigration"
                )
        elif history_count > 0:
            raise ProactiveBackendBindingConflict(
                "ProactiveBackendBindingMissingForExistingHistory"
            )
        return await _append_initial_anchor(uow.session, runtime, identity)


async def ensure_proactive_backend_binding(
    *,
    workspace_path: str | Path,
    binding_path: str,
    runtime: StorageBackendRuntime,
) -> dict[str, object]:
    """Verify/repair the cache and bind history to one exact generation."""

    workspace = Path(workspace_path).resolve()
    path = _inside_workspace(workspace, binding_path)
    lock = SingleInstanceLock(path.with_suffix(path.suffix + ".lock"))
    await asyncio.to_thread(lock.acquire)
    try:
        marker = await asyncio.to_thread(_read_marker, path)
        identity = _identity(runtime)
        payload = await _ensure_database_anchor(
            runtime,
            identity=identity,
            marker=marker,
        )
        if marker != payload:
            await asyncio.to_thread(_write_atomic, path, payload)
        verified = await asyncio.to_thread(_read_marker, path)
        if verified != payload:
            raise ProactiveBackendBindingConflict(
                "ProactiveBackendBindingCacheRepairFailed"
            )
        return dict(payload)
    finally:
        await asyncio.to_thread(lock.release)


async def verify_proactive_backend_binding(
    *,
    workspace_path: str | Path,
    binding_path: str,
    runtime: StorageBackendRuntime,
) -> dict[str, object]:
    """Read both anchors without repairing or changing either one."""

    workspace = Path(workspace_path).resolve()
    path = _inside_workspace(workspace, binding_path)
    marker = await asyncio.to_thread(_read_marker, path)
    identity = _identity(runtime)
    if marker is None or int(marker.get("schema_version") or 0) != _SCHEMA_VERSION:
        raise ProactiveBackendBindingConflict(
            "ProactiveBackendBindingCacheMissingOrLegacy"
        )
    async with runtime.unit_of_work() as uow:
        chain = await _load_chain(uow.session)
        head = await _load_head(uow.session)
    if not chain or head != chain[-1] or marker != head:
        raise ProactiveBackendBindingConflict(
            "ProactiveBackendBindingAnchorsDiverged"
        )
    if head.get("identity") != identity:
        raise ProactiveBackendBindingConflict(
            "ProactiveBackendSwitchRequiresVerifiedMigration"
        )
    return {
        "component": "proactive_backend_binding",
        "status": "healthy",
        "binding_epoch": int(head["binding_epoch"]),
        "binding_sha256": str(head["binding_sha256"]),
        "backend": str(identity["backend"]),
        "generation_id": str(identity["generation_id"]),
        "cache_present": True,
        "database_anchor_present": True,
    }


__all__ = [
    "ProactiveBackendBindingConflict",
    "ensure_proactive_backend_binding",
    "verify_proactive_backend_binding",
]
