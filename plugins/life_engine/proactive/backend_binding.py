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
import sqlite3
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
_REPAIR_NAMESPACE = "life_proactive.generation_repairs"
_REPAIR_EVENT_KIND = "proactive_generation_repaired"
_INITIAL_BIND_NAMESPACE = "life_proactive.initial_bindings"
_INITIAL_BIND_EVENT_KIND = "proactive_initial_binding_completed"


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


_BACKEND_AUTHORITY_IDENTITY_KEYS = (
    "backend",
    "backend_identity_sha256",
    "authority_provider",
    "authority_registry_id",
    "authority_provider_identity_sha256",
    "scope",
)
_GENERATION_IDENTITY_KEYS = (
    "generation_id",
    "generation_manifest_sha256",
    "generation_source_sha256",
)


def _same_backend_authority(
    left: dict[str, object],
    right: dict[str, object],
) -> bool:
    return all(
        str(left.get(key) or "") == str(right.get(key) or "")
        for key in _BACKEND_AUTHORITY_IDENTITY_KEYS
    )


def _generation_repair_required(
    bound: dict[str, object],
    identity: dict[str, object],
) -> bool:
    """Return True when only the generation identity changed on one backend."""

    if bound == identity:
        return False
    if not _same_backend_authority(bound, identity):
        return False
    return any(
        str(bound.get(key) or "") != str(identity.get(key) or "")
        for key in _GENERATION_IDENTITY_KEYS
    )


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


def _certificate_history_and_identity(
    payload: dict[str, Any],
    *,
    runtime: StorageBackendRuntime,
    identity: dict[str, object],
    history: Any,
) -> tuple[bool, bool]:
    """Return (matches live history, matches current backend identity)."""

    table_roots = payload.get("table_roots")
    table_counts = payload.get("table_row_counts")
    if not isinstance(table_roots, dict) or not isinstance(table_counts, dict):
        raise ProactiveBackendBindingConflict(
            "ProactiveMigrationCertificateEvidenceMissing"
        )
    generation = runtime.generation
    if generation is None:
        raise ProactiveBackendBindingConflict(
            "ProactiveMigrationGenerationMissing"
        )
    expected_tables = {item.name: item.root_sha256 for item in history.tables}
    expected_counts = {item.name: item.row_count for item in history.tables}
    history_ok = all(
        (
            int(payload.get("schema_version") or 0) == 1,
            payload.get("writer_frozen") is True,
            payload.get("verified") is True,
            str(payload.get("source_snapshot_sha256") or "")
            == generation.source_snapshot_sha256,
            str(payload.get("source_root_sha256") or "") == history.root_sha256,
            str(payload.get("target_root_sha256") or "") == history.root_sha256,
            str(payload.get("target_backend") or "") == runtime.backend.value,
            str(payload.get("history_algorithm_version") or "")
            == history.algorithm_version,
            dict(table_roots) == expected_tables,
            {str(key): int(value) for key, value in dict(table_counts).items()}
            == expected_counts,
        )
    )
    identity_ok = str(
        payload.get("target_backend_identity_sha256") or ""
    ) == str(identity.get("backend_identity_sha256") or "")
    return history_ok, identity_ok


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
    identity_mismatch_only = False
    for row in rows:
        if str(row["event_kind"]) != PROACTIVE_MIGRATION_EVENT_KIND:
            raise ProactiveBackendBindingConflict(
                "ProactiveMigrationCertificateEventKindInvalid"
            )
        payload = _decode_migration_payload(
            row["payload_json"], row["payload_sha256"]
        )
        history_ok, identity_ok = _certificate_history_and_identity(
            payload,
            runtime=runtime,
            identity=identity,
            history=history,
        )
        source_matches = all(
            (
                str(payload.get("source_binding_sha256") or "")
                == source_binding_sha256,
                str(payload.get("source_identity_sha256") or "")
                == source_identity_sha256,
            )
        )
        target_candidate = history_ok and identity_ok
        candidate = target_candidate and source_matches
        if history_ok and not identity_ok:
            identity_mismatch_only = True
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
    if not matching and identity_mismatch_only and len(rows) == 1:
        raise ProactiveBackendBindingConflict(
            "ProactiveMigrationCertificateBackendIdentityMismatch"
        )
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
            bound_identity = latest.get("identity")
            generation_repair = isinstance(
                bound_identity, dict
            ) and _generation_repair_required(bound_identity, identity)
            if marker is None:
                if generation_repair:
                    raise ProactiveBackendBindingConflict(
                        "ProactiveGenerationRepairRequired"
                    )
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
            if generation_repair:
                raise ProactiveBackendBindingConflict(
                    "ProactiveGenerationRepairRequired"
                )
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


async def _append_generation_repair_event(
    session: AsyncSession,
    runtime: StorageBackendRuntime,
    *,
    payload: dict[str, object],
) -> str:
    """Append one idempotent, content-free generation-repair certificate."""

    encoded = canonical_json(payload)
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    occurrence_id = "proactive:generation-repair:" + str(payload["repair_id"])
    existing = (
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
        .one_or_none()
    )
    if existing is not None:
        raw = (
            existing["payload_json"].decode("utf-8")
            if isinstance(existing["payload_json"], bytes)
            else str(existing["payload_json"])
        )
        if (
            str(existing["namespace"]) != _REPAIR_NAMESPACE
            or str(existing["event_kind"]) != _REPAIR_EVENT_KIND
            or raw != encoded
            or str(existing["payload_sha256"]) != digest
        ):
            raise ProactiveBackendBindingConflict(
                "ProactiveGenerationRepairCertificateConflict"
            )
        return digest
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
            "namespace": _REPAIR_NAMESPACE,
            "occurrence_id": occurrence_id,
            "event_kind": _REPAIR_EVENT_KIND,
            "payload_json": encoded,
            "payload_sha256": digest,
            "occurred_at": _bind_time(runtime, now),
            "recorded_at": _bind_time(runtime, now),
        },
    )
    return digest


async def _load_generation_repair_event(
    session: AsyncSession,
    *,
    repair_id: str,
) -> dict[str, object] | None:
    """Load and verify one content-free generation-repair certificate."""

    occurrence_id = "proactive:generation-repair:" + str(repair_id)
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
        .one_or_none()
    )
    if row is None:
        return None
    raw = (
        row["payload_json"].decode("utf-8")
        if isinstance(row["payload_json"], bytes)
        else str(row["payload_json"])
    )
    if (
        str(row["namespace"]) != _REPAIR_NAMESPACE
        or str(row["event_kind"]) != _REPAIR_EVENT_KIND
        or hashlib.sha256(raw.encode("utf-8")).hexdigest()
        != str(row["payload_sha256"])
    ):
        raise ProactiveBackendBindingConflict(
            "ProactiveGenerationRepairCertificateCorrupt"
        )
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProactiveBackendBindingConflict(
            "ProactiveGenerationRepairCertificateUnreadable"
        ) from exc
    if not isinstance(value, dict) or str(value.get("repair_id") or "") != repair_id:
        raise ProactiveBackendBindingConflict(
            "ProactiveGenerationRepairCertificateInvalid"
        )
    return dict(value)


async def _load_history_matching_copy_certificate(
    session: AsyncSession,
    runtime: StorageBackendRuntime,
    identity: dict[str, object],
) -> dict[str, Any]:
    """Load the unique copy certificate that proves live history."""

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
        raise ProactiveBackendBindingConflict(
            "ProactiveMigrationCertificateMissingOrAmbiguous"
        )
    generation = runtime.generation
    if generation is None:
        raise ProactiveBackendBindingConflict("ProactiveMigrationGenerationMissing")
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
    for row in rows:
        if str(row["event_kind"]) != PROACTIVE_MIGRATION_EVENT_KIND:
            raise ProactiveBackendBindingConflict(
                "ProactiveMigrationCertificateEventKindInvalid"
            )
        payload = _decode_migration_payload(
            row["payload_json"], row["payload_sha256"]
        )
        history_ok, _identity_ok = _certificate_history_and_identity(
            payload,
            runtime=runtime,
            identity=identity,
            history=history,
        )
        if not history_ok:
            continue
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
    if len(matching) != 1:
        raise ProactiveBackendBindingConflict(
            "ProactiveMigrationCertificateMissingOrAmbiguous"
        )
    return matching[0]


async def _append_initial_binding_event(
    session: AsyncSession,
    runtime: StorageBackendRuntime,
    *,
    payload: dict[str, object],
) -> str:
    """Append one idempotent, content-free initial-binding certificate."""

    encoded = canonical_json(payload)
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    occurrence_id = "proactive:initial-binding:" + str(payload["repair_id"])
    existing = (
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
        .one_or_none()
    )
    if existing is not None:
        raw = (
            existing["payload_json"].decode("utf-8")
            if isinstance(existing["payload_json"], bytes)
            else str(existing["payload_json"])
        )
        if (
            str(existing["namespace"]) != _INITIAL_BIND_NAMESPACE
            or str(existing["event_kind"]) != _INITIAL_BIND_EVENT_KIND
            or raw != encoded
            or str(existing["payload_sha256"]) != digest
        ):
            raise ProactiveBackendBindingConflict(
                "ProactiveInitialBindingCertificateConflict"
            )
        return digest
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
            "namespace": _INITIAL_BIND_NAMESPACE,
            "occurrence_id": occurrence_id,
            "event_kind": _INITIAL_BIND_EVENT_KIND,
            "payload_json": encoded,
            "payload_sha256": digest,
            "occurred_at": _bind_time(runtime, now),
            "recorded_at": _bind_time(runtime, now),
        },
    )
    return digest


async def _load_initial_binding_event(
    session: AsyncSession,
    *,
    repair_id: str,
) -> dict[str, object] | None:
    occurrence_id = "proactive:initial-binding:" + str(repair_id)
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
        .one_or_none()
    )
    if row is None:
        return None
    raw = (
        row["payload_json"].decode("utf-8")
        if isinstance(row["payload_json"], bytes)
        else str(row["payload_json"])
    )
    if (
        str(row["namespace"]) != _INITIAL_BIND_NAMESPACE
        or str(row["event_kind"]) != _INITIAL_BIND_EVENT_KIND
        or hashlib.sha256(raw.encode("utf-8")).hexdigest()
        != str(row["payload_sha256"])
    ):
        raise ProactiveBackendBindingConflict(
            "ProactiveInitialBindingCertificateCorrupt"
        )
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProactiveBackendBindingConflict(
            "ProactiveInitialBindingCertificateUnreadable"
        ) from exc
    if not isinstance(value, dict) or str(value.get("repair_id") or "") != repair_id:
        raise ProactiveBackendBindingConflict(
            "ProactiveInitialBindingCertificateInvalid"
        )
    return dict(value)


async def _replay_completed_generation_repair(
    *,
    marker: dict[str, object] | None,
    runtime: StorageBackendRuntime,
    identity: dict[str, object],
    previous_generation_id: str,
    previous_generation_manifest_sha256: str,
    repair_id: str,
) -> dict[str, object] | None:
    """Recover a committed repair whose workspace cache write was interrupted."""

    async with runtime.unit_of_work() as uow:
        chain = await _load_chain(uow.session)
        head = await _load_head(uow.session)
        if (
            not chain
            or head != chain[-1]
            or head.get("identity") != identity
        ):
            return None
        certificate = await _load_generation_repair_event(
            uow.session,
            repair_id=repair_id,
        )
        if certificate is None:
            return None
        if marker != head and (
            marker is not None
            and (len(chain) < 2 or marker != chain[-2])
        ):
            raise ProactiveBackendBindingConflict(
                "ProactiveBackendBindingAnchorsDiverged"
            )
        migration = head.get("migration")
        history = await read_proactive_history_in_session(uow.session)
        expected_certificate_sha256 = hashlib.sha256(
            canonical_json(certificate).encode("utf-8")
        ).hexdigest()
        if not isinstance(migration, dict) or any(
            (
                str(certificate.get("previous_generation_id") or "")
                != previous_generation_id,
                str(certificate.get("previous_generation_manifest_sha256") or "")
                != previous_generation_manifest_sha256,
                str(certificate.get("target_generation_id") or "")
                != str(identity["generation_id"]),
                str(certificate.get("target_generation_manifest_sha256") or "")
                != str(identity["generation_manifest_sha256"]),
                str(certificate.get("source_snapshot_sha256") or "")
                != runtime.generation.source_snapshot_sha256,
                str(certificate.get("target_root_sha256") or "")
                != history.root_sha256,
                str(certificate.get("migration_certificate_sha256") or "")
                != str(migration.get("migration_certificate_sha256") or ""),
                str(migration.get("repair_id") or "") != repair_id,
                str(migration.get("repair_certificate_sha256") or "")
                != expected_certificate_sha256,
                str(migration.get("previous_binding_sha256") or "")
                != str(certificate.get("previous_binding_sha256") or ""),
            )
        ):
            raise ProactiveBackendBindingConflict(
                "ProactiveGenerationRepairCertificateConflict"
            )
        return dict(head)


async def repair_proactive_generation_binding(
    *,
    workspace_path: str | Path,
    binding_path: str,
    runtime: StorageBackendRuntime,
    previous_generation_id: str,
    previous_generation_manifest_sha256: str,
    repair_id: str,
) -> dict[str, object]:
    """Repair one known generation identity under an explicit audited proof.

    This is an operator-run control-plane operation.  It changes only the
    rebuildable proactive binding cache/anchor and appends a content-free repair
    certificate; it never edits proactive history or subject-owned files.
    """

    if runtime.generation is None or runtime.authority_token is None:
        raise ProactiveBackendBindingConflict("ProactiveBackendGenerationRequired")
    repair = str(repair_id or "").strip()
    previous_id = str(previous_generation_id or "").strip()
    previous_manifest = str(previous_generation_manifest_sha256 or "").strip()
    if not repair or not previous_id or len(previous_manifest) != 64:
        raise ValueError("generation repair identity is incomplete")
    workspace = Path(workspace_path).resolve()
    path = _inside_workspace(workspace, binding_path)
    lock = SingleInstanceLock(path.with_suffix(path.suffix + ".lock"))
    await asyncio.to_thread(lock.acquire)
    try:
        marker = await asyncio.to_thread(_read_marker, path)
        identity = _identity(runtime)
        replayed = await _replay_completed_generation_repair(
            marker=marker,
            runtime=runtime,
            identity=identity,
            previous_generation_id=previous_id,
            previous_generation_manifest_sha256=previous_manifest,
            repair_id=repair,
        )
        if replayed is not None:
            await asyncio.to_thread(_write_atomic, path, replayed)
            verified = await asyncio.to_thread(_read_marker, path)
            if verified != replayed:
                raise ProactiveBackendBindingConflict(
                    "ProactiveBackendBindingCacheRepairFailed"
                )
            return dict(replayed)
        async with runtime.unit_of_work() as uow:
            chain = await _load_chain(uow.session)
            head = await _load_head(uow.session)
            if not chain:
                raise ProactiveBackendBindingConflict(
                    "ProactiveBackendBindingChainMissing"
                )
            if head is None:
                await _store_head(uow.session, runtime, chain[-1])
                head = chain[-1]
            if head != chain[-1] or marker != head:
                raise ProactiveBackendBindingConflict(
                    "ProactiveBackendBindingAnchorsDiverged"
                )
            previous_identity = head.get("identity")
            if not isinstance(previous_identity, dict):
                raise ProactiveBackendBindingConflict(
                    "ProactiveGenerationRepairPreviousIdentityMissing"
                )
            if (
                str(previous_identity.get("generation_id") or "") != previous_id
                or str(previous_identity.get("generation_manifest_sha256") or "")
                != previous_manifest
            ):
                raise ProactiveBackendBindingConflict(
                    "ProactiveGenerationRepairPreviousIdentityMismatch"
                )
            if previous_identity == identity:
                raise ProactiveBackendBindingConflict(
                    "ProactiveGenerationRepairTargetIdentityUnchanged"
                )
            if str(previous_identity.get("backend") or "") != str(
                identity.get("backend") or ""
            ) or str(
                previous_identity.get("backend_identity_sha256") or ""
            ) != str(identity.get("backend_identity_sha256") or ""):
                raise ProactiveBackendBindingConflict(
                    "ProactiveGenerationRepairBackendIdentityMismatch"
                )
            if str(previous_identity.get("authority_provider") or "") != str(
                identity.get("authority_provider") or ""
            ) or str(
                previous_identity.get("authority_registry_id") or ""
            ) != str(identity.get("authority_registry_id") or "") or str(
                previous_identity.get("authority_provider_identity_sha256") or ""
            ) != str(identity.get("authority_provider_identity_sha256") or ""):
                raise ProactiveBackendBindingConflict(
                    "ProactiveGenerationRepairAuthorityIdentityMismatch"
                )
            migration = head.get("migration")
            if not isinstance(migration, dict):
                raise ProactiveBackendBindingConflict(
                    "ProactiveGenerationRepairMigrationEvidenceMissing"
                )
            history = await read_proactive_history_in_session(uow.session)
            expected_root = str(
                runtime.generation.root_hashes.get(
                    f"{runtime.backend.value}:proactive_authority",
                    "",
                )
            )
            if expected_root != history.root_sha256:
                raise ProactiveBackendBindingConflict(
                    "ProactiveGenerationRepairRootMismatch"
                )
            if str(migration.get("target_root_sha256") or "") != history.root_sha256:
                raise ProactiveBackendBindingConflict(
                    "ProactiveGenerationRepairMigrationRootMismatch"
                )
            if str(migration.get("source_snapshot_sha256") or "") != (
                runtime.generation.source_snapshot_sha256
            ):
                raise ProactiveBackendBindingConflict(
                    "ProactiveGenerationRepairSnapshotMismatch"
                )
            repair_payload: dict[str, object] = {
                "schema_version": 1,
                "repair_id": repair,
                "previous_binding_sha256": str(head["binding_sha256"]),
                "previous_generation_id": previous_id,
                "previous_generation_manifest_sha256": previous_manifest,
                "target_generation_id": str(identity["generation_id"]),
                "target_generation_manifest_sha256": str(
                    identity["generation_manifest_sha256"]
                ),
                "source_snapshot_sha256": runtime.generation.source_snapshot_sha256,
                "target_root_sha256": history.root_sha256,
                "migration_certificate_sha256": str(
                    migration.get("certificate_sha256") or ""
                ),
                "verified": True,
            }
            repair_certificate_sha256 = await _append_generation_repair_event(
                uow.session,
                runtime,
                payload=repair_payload,
            )
            payload = await _append_rebinding_anchor(
                uow.session,
                runtime,
                identity,
                previous=head,
                migration={
                    "repair_id": repair,
                    "repair_certificate_sha256": repair_certificate_sha256,
                    "migration_certificate_sha256": str(
                        migration.get("certificate_sha256") or ""
                    ),
                    "previous_binding_sha256": str(head["binding_sha256"]),
                    "previous_generation_id": previous_id,
                    "source_snapshot_sha256": runtime.generation.source_snapshot_sha256,
                    "target_root_sha256": history.root_sha256,
                },
            )
        await asyncio.to_thread(_write_atomic, path, payload)
        verified = await asyncio.to_thread(_read_marker, path)
        if verified != payload:
            raise ProactiveBackendBindingConflict(
                "ProactiveBackendBindingCacheRepairFailed"
            )
        return dict(payload)
    finally:
        await asyncio.to_thread(lock.release)


async def complete_proactive_initial_binding(
    *,
    workspace_path: str | Path,
    binding_path: str,
    runtime: StorageBackendRuntime,
    source_binding: dict[str, object],
    repair_id: str,
    certificate_backend_identity_sha256: str = "",
) -> dict[str, object]:
    """Complete the first production bind after a verified copy.

    Startup never does this.  Use it when selectable history and a copy
    certificate exist, but the binding chain was never written — including
    after the candidate SQLite file was relocated to the production path.
    The workspace JSON cache is not authority and is never promoted.
    """

    if runtime.generation is None or runtime.authority_token is None:
        raise ProactiveBackendBindingConflict("ProactiveBackendGenerationRequired")
    repair = str(repair_id or "").strip()
    if not repair:
        raise ValueError("initial binding repair identity is incomplete")
    source_digest = _payload_digest(source_binding)
    source_identity = source_binding.get("identity")
    if not isinstance(source_identity, dict):
        raise ProactiveBackendBindingConflict(
            "ProactiveMigrationSourceIdentityMissing"
        )
    source_identity_sha256 = hashlib.sha256(
        canonical_json(source_identity).encode("utf-8")
    ).hexdigest()
    workspace = Path(workspace_path).resolve()
    path = _inside_workspace(workspace, binding_path)
    lock = SingleInstanceLock(path.with_suffix(path.suffix + ".lock"))
    await asyncio.to_thread(lock.acquire)
    try:
        identity = _identity(runtime)
        async with runtime.unit_of_work() as uow:
            chain = await _load_chain(uow.session)
            head = await _load_head(uow.session)
            existing_event = await _load_initial_binding_event(
                uow.session,
                repair_id=repair,
            )
            if chain:
                if (
                    head == chain[-1]
                    and head.get("identity") == identity
                    and existing_event is not None
                ):
                    payload = dict(head)
                else:
                    raise ProactiveBackendBindingConflict(
                        "ProactiveBackendBindingChainAlreadyPresent"
                    )
            else:
                if head is not None:
                    raise ProactiveBackendBindingConflict(
                        "ProactiveBackendBindingHeadWithoutChain"
                    )
                certificate = await _load_history_matching_copy_certificate(
                    uow.session,
                    runtime,
                    identity,
                )
                cert_identity = str(
                    certificate.get("target_backend_identity_sha256") or ""
                )
                live_identity = str(identity.get("backend_identity_sha256") or "")
                acknowledged = str(certificate_backend_identity_sha256 or "").strip()
                if cert_identity != live_identity:
                    if acknowledged != cert_identity:
                        raise ProactiveBackendBindingConflict(
                            "ProactiveMigrationCertificateBackendIdentityMismatch"
                        )
                elif acknowledged and acknowledged != live_identity:
                    raise ProactiveBackendBindingConflict(
                        "ProactiveMigrationCertificateBackendIdentityMismatch"
                    )
                if (
                    source_digest != str(
                        certificate.get("source_binding_sha256") or ""
                    )
                    or source_identity_sha256
                    != str(certificate.get("source_identity_sha256") or "")
                ):
                    raise ProactiveBackendBindingConflict(
                        "ProactiveMigrationSourceBindingMismatch"
                    )
                history = await read_proactive_history_in_session(uow.session)
                repair_payload: dict[str, object] = {
                    "schema_version": 1,
                    "repair_id": repair,
                    "repair_kind": "initial_binding_after_verified_copy",
                    "certificate_sha256": str(
                        certificate.get("_certificate_sha256") or ""
                    ),
                    "certificate_backend_identity_sha256": cert_identity,
                    "live_backend_identity_sha256": live_identity,
                    "source_binding_sha256": source_digest,
                    "source_generation_id": str(
                        source_identity.get("generation_id") or ""
                    ),
                    "target_generation_id": str(identity["generation_id"]),
                    "target_generation_manifest_sha256": str(
                        identity["generation_manifest_sha256"]
                    ),
                    "source_snapshot_sha256": runtime.generation.source_snapshot_sha256,
                    "target_root_sha256": history.root_sha256,
                    "verified": True,
                }
                repair_certificate_sha256 = await _append_initial_binding_event(
                    uow.session,
                    runtime,
                    payload=repair_payload,
                )
                migration = _migration_binding_evidence(certificate)
                migration["initial_binding_repair_id"] = repair
                migration["initial_binding_certificate_sha256"] = (
                    repair_certificate_sha256
                )
                migration["certificate_backend_identity_sha256"] = cert_identity
                payload = await _append_initial_anchor(
                    uow.session,
                    runtime,
                    identity,
                    migration=migration,
                )
        await asyncio.to_thread(_write_atomic, path, payload)
        verified = await asyncio.to_thread(_read_marker, path)
        if verified != payload:
            raise ProactiveBackendBindingConflict(
                "ProactiveBackendBindingCacheRepairFailed"
            )
        return dict(payload)
    finally:
        await asyncio.to_thread(lock.release)


async def ensure_proactive_backend_binding(
    *,
    workspace_path: str | Path,
    binding_path: str,
    runtime: StorageBackendRuntime,
) -> dict[str, object]:
    """Verify/repair the cache and bind history to one exact generation.

    Startup never rewrites generation identity.  A same-backend generation
    bump must go through ``repair_proactive_generation_binding``; leftover
    backend-migration certificates are not a substitute.
    """

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


def read_sqlite_proactive_backend_binding(database: str | Path) -> dict[str, Any]:
    """Read the durable source binding from one leftover proactive SQLite file."""

    path = Path(database)
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """SELECT position, occurrence_id, event_kind, payload_json,
                payload_sha256 FROM runtime_events
                WHERE namespace = ? ORDER BY position""",
            (_BINDING_NAMESPACE,),
        ).fetchall()
        head = connection.execute(
            """SELECT revision, schema_version, payload_json, payload_sha256
                FROM runtime_states
                WHERE namespace = ? AND state_key = ?""",
            (_BINDING_NAMESPACE, _BINDING_STATE_KEY),
        ).fetchone()
    except sqlite3.DatabaseError as exc:
        raise ProactiveBackendBindingConflict(
            "ProactiveSourceBindingMissing"
        ) from exc
    finally:
        connection.close()
    if not rows or head is None:
        raise ProactiveBackendBindingConflict("ProactiveSourceBindingMissing")
    chain: list[dict[str, Any]] = []
    previous = ""
    for epoch, row in enumerate(rows, start=1):
        if str(row["event_kind"]) != "proactive_backend_bound":
            raise ProactiveBackendBindingConflict(
                "ProactiveSourceBindingChainInvalid"
            )
        payload = _decode_payload(row["payload_json"], row["payload_sha256"])
        digest = str(payload.get("binding_sha256") or "")
        if (
            int(payload.get("schema_version") or 0) != _SCHEMA_VERSION
            or int(payload.get("binding_epoch") or 0) != epoch
            or str(payload.get("previous_binding_sha256") or "") != previous
            or str(row["occurrence_id"]) != f"proactive:backend-binding:{digest}"
        ):
            raise ProactiveBackendBindingConflict(
                "ProactiveSourceBindingChainInvalid"
            )
        chain.append(payload)
        previous = digest
    head_payload = _decode_payload(head["payload_json"], head["payload_sha256"])
    if (
        int(head["schema_version"]) != _SCHEMA_VERSION
        or int(head["revision"]) != len(chain)
        or head_payload != chain[-1]
    ):
        raise ProactiveBackendBindingConflict("ProactiveSourceBindingHeadInvalid")
    return chain[-1]


__all__ = [
    "ProactiveBackendBindingConflict",
    "complete_proactive_initial_binding",
    "ensure_proactive_backend_binding",
    "read_sqlite_proactive_backend_binding",
    "repair_proactive_generation_binding",
    "verify_proactive_backend_binding",
]
