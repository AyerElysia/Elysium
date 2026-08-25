"""Offline migration and cutover contracts for the unified proactive authority."""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from plugins.life_engine.attention_threads import AttentionThreadCommand
from plugins.life_engine.initiative import InitiativeSeedCommand
from plugins.life_engine.proactive import backend_binding
from plugins.life_engine.proactive.backend_binding import (
    ProactiveBackendBindingConflict,
    complete_proactive_initial_binding,
    ensure_proactive_backend_binding,
    read_sqlite_proactive_backend_binding,
    repair_proactive_generation_binding,
    verify_proactive_backend_binding,
)
from plugins.life_engine.proactive.runtime import open_local_proactive_runtime
from plugins.life_engine.storage.attention_schema import (
    ensure_attention_thread_schema,
)
from plugins.life_engine.storage.authority import FileAuthorityRegistry
from plugins.life_engine.storage.contracts import (
    StorageBackendRuntime,
    StorageWriterRole,
)
from plugins.life_engine.storage.migration import (
    LifeStorageLayout,
    create_local_snapshot,
)
from plugins.life_engine.storage.migration.manifest import load_snapshot_manifest
from plugins.life_engine.storage.models import (
    BackendGeneration,
    BackendKind,
    GenerationStatus,
)
from plugins.life_engine.storage.proactive_migration import (
    PROACTIVE_SNAPSHOT_SOURCE,
    ProactiveAuthorityCopyReport,
    ProactiveAuthorityMigrationError,
    copy_proactive_authority_from_snapshot,
    verify_proactive_authority_copy,
)
from plugins.life_engine.storage.runtime_schema import ensure_runtime_state_schema
from src.kernel.storage import SQLiteStorageConfig, create_sqlite_storage_engine


def _config() -> SimpleNamespace:
    return SimpleNamespace(
        local_database_path="runtime/proactive/proactive.sqlite3",
        local_authority_state_path="runtime/proactive/authority.json",
        backend_binding_path="runtime/proactive/backend-binding.json",
        authority_lease_seconds=30,
        authority_renew_interval_seconds=5,
    )


async def _active_actor(instance_id: str) -> bool:
    return instance_id == "chat_global"


def _attention() -> AttentionThreadCommand:
    return AttentionThreadCommand(
        occurrence_id="migration:attention:open",
        thread_id="attention:migration",
        action="open",
        actor_consciousness_instance_id="chat_global",
        source_instance_id="chat_global",
        source_occurrence_ids=("event:migration",),
        causation_occurrence_id="event:migration",
        expected_revision=0,
        public_statement="我选择把这一件事留作持续关注。",
        occurred_at="2026-08-23T00:00:00+00:00",
    )


def _initiative() -> InitiativeSeedCommand:
    return InitiativeSeedCommand(
        occurrence_id="migration:initiative:hold",
        seed_id="initiative:migration",
        action="hold",
        actor_consciousness_instance_id="chat_global",
        source_instance_id="chat_global",
        source_occurrence_ids=("event:migration",),
        causation_occurrence_id="event:migration",
        expected_revision=0,
        public_statement="我明确选择以后再回来看它。",
        related_entity_refs=(),
        occurred_at="2026-08-23T00:00:01+00:00",
        reencounter_after_minutes=0,
    )


async def _frozen_source(
    tmp_path: Path,
    *,
    writer_frozen: bool = True,
) -> tuple[Path, Path]:
    data_root = tmp_path / "data"
    workspace = data_root / "life_engine_workspace"
    runtime = await open_local_proactive_runtime(
        workspace_path=workspace,
        config=_config(),
        validate_active_actor=_active_actor,
    )
    await runtime.authority.decide_attention(_attention())
    await runtime.authority.decide_initiative(_initiative())
    await runtime.close()
    snapshot = tmp_path / "snapshot"
    create_local_snapshot(
        data_root,
        snapshot,
        layout=LifeStorageLayout(
            sqlite_sources=(Path(PROACTIVE_SNAPSHOT_SOURCE.as_posix()),),
            exact_roots=(),
            excluded_rebuildable_roots=(),
            excluded_preserved_backup_roots=(),
        ),
        writer_frozen=writer_frozen,
    )
    return snapshot, workspace


async def _candidate_runtime(path: Path) -> StorageBackendRuntime:
    config = SQLiteStorageConfig(database_path=path, busy_timeout_seconds=10)
    engine = create_sqlite_storage_engine(config)

    async def validate() -> None:
        return None

    runtime = StorageBackendRuntime(
        enabled=True,
        backend=BackendKind.LOCAL,
        backend_identity=config.safe_identity,
        generation=None,
        authority_registry=None,
        authority_token=None,
        engine=engine,
        session_factory=async_sessionmaker(engine, expire_on_commit=False),
        _write_fence=lambda _session: validate(),
        _writer_validator=validate,
        writer_role=StorageWriterRole.CANDIDATE_COPY,
    )
    await ensure_runtime_state_schema(runtime)
    await ensure_attention_thread_schema(runtime)
    return runtime


async def _activate_migrated_runtime(
    snapshot: Path,
    target_workspace: Path,
    *,
    migration_id: str,
) -> tuple[StorageBackendRuntime, ProactiveAuthorityCopyReport]:
    target_database = target_workspace / "runtime/proactive/proactive.sqlite3"
    target_database.parent.mkdir(parents=True, exist_ok=True)
    candidate = await _candidate_runtime(target_database)
    target_identity = candidate.backend_identity
    copied = await copy_proactive_authority_from_snapshot(
        snapshot,
        candidate,
        migration_id=migration_id,
    )
    await candidate.close()

    manifest = load_snapshot_manifest(snapshot / "manifest.json")
    generation = BackendGeneration(
        generation_id=f"{migration_id}-generation",
        backend=BackendKind.LOCAL,
        schema_version=1,
        source_snapshot_sha256=str(manifest["source_snapshot_sha256"]),
        root_hashes={"local:proactive_authority": copied.target_root_sha256},
        frontiers={"proactive_rows": copied.row_count},
        created_at=str(manifest["created_at"]),
        verified_at=datetime.now(UTC).isoformat(),
        status=GenerationStatus.VERIFIED,
        metadata={"snapshot_manifest_sha256": str(manifest["manifest_sha256"])},
    )
    registry = FileAuthorityRegistry(
        target_workspace / "runtime/proactive/authority.json",
        registry_id=f"life-proactive-{migration_id}",
    )
    await registry.register_generation(generation)
    token = await registry.activate_generation(
        generation.generation_id,
        expected_epoch=0,
        owner_id="migration-test",
        lease_seconds=60,
        confirm_previous_writers_stopped=False,
    )
    engine = create_sqlite_storage_engine(
        SQLiteStorageConfig(database_path=target_database, busy_timeout_seconds=10)
    )
    return (
        StorageBackendRuntime(
            enabled=True,
            backend=BackendKind.LOCAL,
            backend_identity=target_identity,
            generation=generation,
            authority_registry=registry,
            authority_token=token,
            engine=engine,
            session_factory=async_sessionmaker(engine, expire_on_commit=False),
        ),
        copied,
    )


async def _activate_repaired_generation(
    active: StorageBackendRuntime,
    target_workspace: Path,
    *,
    proactive_root_sha256: str | None = None,
    source_snapshot_sha256: str | None = None,
) -> StorageBackendRuntime:
    """Activate a new immutable generation over unchanged copied authority."""

    previous = active.generation
    registry = active.authority_registry
    assert previous is not None
    assert isinstance(registry, FileAuthorityRegistry)
    backend_identity = active.backend_identity
    await active.revoke_authority()
    await active.close()

    generation = BackendGeneration(
        generation_id=f"{previous.generation_id}-v3",
        backend=previous.backend,
        schema_version=previous.schema_version,
        source_snapshot_sha256=(
            source_snapshot_sha256 or previous.source_snapshot_sha256
        ),
        root_hashes={
            **previous.root_hashes,
            "local:proactive_authority": (
                proactive_root_sha256
                or previous.root_hashes["local:proactive_authority"]
            ),
        },
        frontiers=dict(previous.frontiers),
        created_at=previous.created_at,
        verified_at=datetime.now(UTC).isoformat(),
        status=GenerationStatus.VERIFIED,
        metadata={**previous.metadata, "generation_repair_test": "v3"},
    )
    await registry.register_generation(generation)
    health = await registry.health()
    token = await registry.activate_generation(
        generation.generation_id,
        expected_epoch=int(health["authority_epoch"]),
        owner_id="generation-repair-test",
        lease_seconds=60,
        confirm_previous_writers_stopped=False,
    )
    database_path = target_workspace / "runtime/proactive/proactive.sqlite3"
    engine = create_sqlite_storage_engine(
        SQLiteStorageConfig(database_path=database_path, busy_timeout_seconds=10)
    )
    return StorageBackendRuntime(
        enabled=True,
        backend=BackendKind.LOCAL,
        backend_identity=backend_identity,
        generation=generation,
        authority_registry=registry,
        authority_token=token,
        engine=engine,
        session_factory=async_sessionmaker(engine, expire_on_commit=False),
    )


async def _prepare_generation_repair(
    tmp_path: Path,
    *,
    proactive_root_sha256: str | None = None,
    source_snapshot_sha256: str | None = None,
) -> tuple[StorageBackendRuntime, BackendGeneration, Path, str]:
    snapshot, source_workspace = await _frozen_source(tmp_path)
    target_workspace = tmp_path / "target-workspace"
    active, _copied = await _activate_migrated_runtime(
        snapshot,
        target_workspace,
        migration_id="copy-proactive-generation-repair",
    )
    binding_path = "runtime/proactive/backend-binding.json"
    target_binding = target_workspace / binding_path
    target_binding.parent.mkdir(parents=True, exist_ok=True)
    target_binding.write_bytes((source_workspace / binding_path).read_bytes())
    await ensure_proactive_backend_binding(
        workspace_path=target_workspace,
        binding_path=binding_path,
        runtime=active,
    )
    previous = active.generation
    assert previous is not None
    repaired = await _activate_repaired_generation(
        active,
        target_workspace,
        proactive_root_sha256=proactive_root_sha256,
        source_snapshot_sha256=source_snapshot_sha256,
    )
    return repaired, previous, target_workspace, binding_path


@pytest.mark.asyncio
async def test_frozen_proactive_copy_is_exact_resumable_and_certified(
    tmp_path: Path,
) -> None:
    snapshot, _ = await _frozen_source(tmp_path)
    target = await _candidate_runtime(tmp_path / "target.sqlite3")
    try:
        first = await copy_proactive_authority_from_snapshot(
            snapshot,
            target,
            migration_id="copy-proactive-1",
        )
        replay = await copy_proactive_authority_from_snapshot(
            snapshot,
            target,
            migration_id="copy-proactive-1",
        )
        verification = await verify_proactive_authority_copy(snapshot, target)
    finally:
        await target.close()

    assert first.verified is True
    assert first.copied_row_count > 0
    assert first.source_root_sha256 == first.target_root_sha256
    assert len(first.migration_certificate_sha256) == 64
    assert replay.idempotent_replay is True
    assert replay.copied_row_count == 0
    assert verification["verified"] is True
    assert verification["canonical_authority"]["row_count"] == first.row_count


@pytest.mark.asyncio
async def test_candidate_copy_rejects_extra_proactive_history(tmp_path: Path) -> None:
    snapshot, _ = await _frozen_source(tmp_path)
    target = await _candidate_runtime(tmp_path / "target-conflict.sqlite3")
    try:
        async with target.unit_of_work() as uow:
            await uow.session.execute(
                text(
                    """INSERT INTO runtime_states (
                        namespace, state_key, revision, schema_version,
                        payload_json, payload_sha256, updated_at
                    ) VALUES (
                        'life_initiative.seed_heads', 'extra', 1, 1,
                        '{}', :digest, :updated_at
                    )"""
                ),
                {
                    "digest": (
                        "44136fa355b3678a1146ad16f7e8649e"
                        "94fb4fc21fe77e8310c060f61caaff8a"
                    ),
                    "updated_at": datetime.now(UTC).isoformat(),
                },
            )
        with pytest.raises(
            RuntimeError,
            match="ProactiveMigrationTargetConflict",
        ):
            await copy_proactive_authority_from_snapshot(
                snapshot,
                target,
                migration_id="copy-proactive-conflict",
            )
    finally:
        await target.close()


@pytest.mark.asyncio
async def test_verified_certificate_allows_explicit_backend_rebinding(
    tmp_path: Path,
) -> None:
    snapshot, source_workspace = await _frozen_source(tmp_path)
    target_workspace = tmp_path / "target-workspace"
    active, _copied = await _activate_migrated_runtime(
        snapshot,
        target_workspace,
        migration_id="copy-proactive-rebind",
    )
    binding_relative = "runtime/proactive/backend-binding.json"
    target_binding = target_workspace / binding_relative
    target_binding.parent.mkdir(parents=True, exist_ok=True)
    target_binding.write_bytes(
        (source_workspace / binding_relative).read_bytes()
    )
    try:
        bound = await ensure_proactive_backend_binding(
            workspace_path=target_workspace,
            binding_path=binding_relative,
            runtime=active,
        )
        health = await verify_proactive_backend_binding(
            workspace_path=target_workspace,
            binding_path=binding_relative,
            runtime=active,
        )
    finally:
        await active.revoke_authority()
        await active.close()

    assert bound["schema_version"] == 3
    assert bound["identity"]["generation_id"] == (
        active.generation.generation_id
    )
    assert bound["identity"]["generation_manifest_sha256"] == (
        active.generation.manifest_sha256
    )
    assert bound["migration"]["source_binding_sha256"]
    assert bound["migration"]["certificate_sha256"]
    assert health["status"] == "healthy"


@pytest.mark.asyncio
async def test_generation_repair_is_audited_idempotent_and_recovers_cache_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active, previous, workspace, binding_path = await _prepare_generation_repair(
        tmp_path
    )
    try:
        with pytest.raises(
            ProactiveBackendBindingConflict,
            match="ProactiveGenerationRepairRequired",
        ):
            await ensure_proactive_backend_binding(
                workspace_path=workspace,
                binding_path=binding_path,
                runtime=active,
            )

        original_write = backend_binding._write_atomic
        calls = 0

        def fail_once(path: Path, payload: dict[str, object]) -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError("simulated cache interruption")
            original_write(path, payload)

        monkeypatch.setattr(backend_binding, "_write_atomic", fail_once)
        with pytest.raises(OSError, match="simulated cache interruption"):
            await repair_proactive_generation_binding(
                workspace_path=workspace,
                binding_path=binding_path,
                runtime=active,
                previous_generation_id=previous.generation_id,
                previous_generation_manifest_sha256=previous.manifest_sha256,
                repair_id="repair-v2-to-v3",
            )

        repaired = await repair_proactive_generation_binding(
            workspace_path=workspace,
            binding_path=binding_path,
            runtime=active,
            previous_generation_id=previous.generation_id,
            previous_generation_manifest_sha256=previous.manifest_sha256,
            repair_id="repair-v2-to-v3",
        )
        replay = await repair_proactive_generation_binding(
            workspace_path=workspace,
            binding_path=binding_path,
            runtime=active,
            previous_generation_id=previous.generation_id,
            previous_generation_manifest_sha256=previous.manifest_sha256,
            repair_id="repair-v2-to-v3",
        )
        health = await verify_proactive_backend_binding(
            workspace_path=workspace,
            binding_path=binding_path,
            runtime=active,
        )
        async with active.unit_of_work() as uow:
            repair_rows = (
                await uow.session.execute(
                    text(
                        "SELECT occurrence_id FROM runtime_events "
                        "WHERE namespace = 'life_proactive.generation_repairs'"
                    )
                )
            ).all()
    finally:
        await active.revoke_authority()
        await active.close()

    assert repaired["binding_epoch"] == 2
    assert repaired["migration"]["repair_id"] == "repair-v2-to-v3"
    assert replay == repaired
    assert health["status"] == "healthy"
    assert health["binding_epoch"] == 2
    assert len(repair_rows) == 1


@pytest.mark.asyncio
async def test_generation_repair_rejects_old_manifest_root_and_snapshot_mismatch(
    tmp_path: Path,
) -> None:
    active, previous, workspace, binding_path = await _prepare_generation_repair(
        tmp_path / "old-manifest"
    )
    try:
        with pytest.raises(
            ProactiveBackendBindingConflict,
            match="PreviousIdentityMismatch",
        ):
            await repair_proactive_generation_binding(
                workspace_path=workspace,
                binding_path=binding_path,
                runtime=active,
                previous_generation_id=previous.generation_id,
                previous_generation_manifest_sha256="0" * 64,
                repair_id="repair-old-manifest",
            )
    finally:
        await active.revoke_authority()
        await active.close()

    active, previous, workspace, binding_path = await _prepare_generation_repair(
        tmp_path / "root-mismatch",
        proactive_root_sha256="a" * 64,
    )
    try:
        with pytest.raises(
            ProactiveBackendBindingConflict,
            match="RootMismatch",
        ):
            await repair_proactive_generation_binding(
                workspace_path=workspace,
                binding_path=binding_path,
                runtime=active,
                previous_generation_id=previous.generation_id,
                previous_generation_manifest_sha256=previous.manifest_sha256,
                repair_id="repair-root-mismatch",
            )
    finally:
        await active.revoke_authority()
        await active.close()

    active, previous, workspace, binding_path = await _prepare_generation_repair(
        tmp_path / "snapshot-mismatch",
        source_snapshot_sha256="b" * 64,
    )
    try:
        with pytest.raises(
            ProactiveBackendBindingConflict,
            match="SnapshotMismatch",
        ):
            await repair_proactive_generation_binding(
                workspace_path=workspace,
                binding_path=binding_path,
                runtime=active,
                previous_generation_id=previous.generation_id,
                previous_generation_manifest_sha256=previous.manifest_sha256,
                repair_id="repair-snapshot-mismatch",
            )
    finally:
        await active.revoke_authority()
        await active.close()


@pytest.mark.asyncio
async def test_generation_repair_rejects_changed_backend_endpoint(
    tmp_path: Path,
) -> None:
    active, previous, workspace, binding_path = await _prepare_generation_repair(
        tmp_path / "backend-endpoint"
    )
    moved = replace(
        active,
        backend_identity=f"{active.backend_identity}#relocated-endpoint",
    )
    try:
        with pytest.raises(
            ProactiveBackendBindingConflict,
            match="ProactiveGenerationRepairBackendIdentityMismatch",
        ):
            await repair_proactive_generation_binding(
                workspace_path=workspace,
                binding_path=binding_path,
                runtime=moved,
                previous_generation_id=previous.generation_id,
                previous_generation_manifest_sha256=previous.manifest_sha256,
                repair_id="repair-backend-endpoint",
            )
    finally:
        await active.revoke_authority()
        await active.close()


@pytest.mark.asyncio
async def test_migration_certificate_cannot_rebind_another_workspace(
    tmp_path: Path,
) -> None:
    snapshot, _source_workspace = await _frozen_source(tmp_path / "source-a")
    _foreign_snapshot, foreign_workspace = await _frozen_source(
        tmp_path / "source-b"
    )
    target_workspace = tmp_path / "target-workspace"
    active, _copied = await _activate_migrated_runtime(
        snapshot,
        target_workspace,
        migration_id="copy-proactive-foreign-source",
    )
    binding_relative = Path("runtime/proactive/backend-binding.json")
    target_binding = target_workspace / binding_relative
    target_binding.parent.mkdir(parents=True, exist_ok=True)
    target_binding.write_bytes(
        (foreign_workspace / binding_relative).read_bytes()
    )
    try:
        with pytest.raises(
            RuntimeError,
            match="ProactiveMigrationSourceBindingMismatch",
        ):
            await ensure_proactive_backend_binding(
                workspace_path=target_workspace,
                binding_path=binding_relative.as_posix(),
                runtime=active,
            )
    finally:
        await active.revoke_authority()
        await active.close()


@pytest.mark.asyncio
async def test_live_snapshot_is_never_proactive_migration_source(
    tmp_path: Path,
) -> None:
    snapshot, _ = await _frozen_source(tmp_path, writer_frozen=False)
    target = await _candidate_runtime(tmp_path / "target-live.sqlite3")
    try:
        with pytest.raises(
            ProactiveAuthorityMigrationError,
            match="WriterFreezeRequired",
        ):
            await copy_proactive_authority_from_snapshot(
                snapshot,
                target,
                migration_id="copy-proactive-live",
            )
    finally:
        await target.close()


@pytest.mark.asyncio
async def test_generation_repair_restores_missing_head(tmp_path: Path) -> None:
    active, previous, workspace, binding_path = await _prepare_generation_repair(
        tmp_path / "missing-head"
    )
    try:
        async with active.unit_of_work() as uow:
            await uow.session.execute(
                text(
                    """DELETE FROM runtime_states
                    WHERE namespace = 'life_proactive.backend_binding'
                      AND state_key = 'active'"""
                )
            )
        repaired = await repair_proactive_generation_binding(
            workspace_path=workspace,
            binding_path=binding_path,
            runtime=active,
            previous_generation_id=previous.generation_id,
            previous_generation_manifest_sha256=previous.manifest_sha256,
            repair_id="repair-missing-head",
        )
        health = await verify_proactive_backend_binding(
            workspace_path=workspace,
            binding_path=binding_path,
            runtime=active,
        )
    finally:
        await active.revoke_authority()
        await active.close()

    assert repaired["identity"]["generation_id"] == (
        f"{previous.generation_id}-v3"
    )
    assert health["status"] == "healthy"


@pytest.mark.asyncio
async def test_generation_repair_rejects_empty_chain(tmp_path: Path) -> None:
    snapshot, source_workspace = await _frozen_source(tmp_path / "empty-chain")
    target_workspace = tmp_path / "target-workspace"
    active, _copied = await _activate_migrated_runtime(
        snapshot,
        target_workspace,
        migration_id="copy-proactive-empty-chain",
    )
    binding_path = "runtime/proactive/backend-binding.json"
    target_binding = target_workspace / binding_path
    target_binding.parent.mkdir(parents=True, exist_ok=True)
    target_binding.write_bytes((source_workspace / binding_path).read_bytes())
    source_binding = json.loads(
        (source_workspace / binding_path).read_text(encoding="utf-8")
    )
    try:
        with pytest.raises(
            ProactiveBackendBindingConflict,
            match="ProactiveBackendBindingChainMissing",
        ):
            await repair_proactive_generation_binding(
                workspace_path=target_workspace,
                binding_path=binding_path,
                runtime=active,
                previous_generation_id=str(
                    source_binding["identity"]["generation_id"]
                ),
                previous_generation_manifest_sha256=str(
                    source_binding["identity"]["generation_manifest_sha256"]
                ),
                repair_id="repair-empty-chain",
            )
    finally:
        await active.revoke_authority()
        await active.close()


@pytest.mark.asyncio
async def test_complete_initial_binding_after_relocated_verified_copy(
    tmp_path: Path,
) -> None:
    snapshot, source_workspace = await _frozen_source(tmp_path / "source")
    candidate_workspace = tmp_path / "candidate-workspace"
    active, _copied = await _activate_migrated_runtime(
        snapshot,
        candidate_workspace,
        migration_id="copy-proactive-relocate",
    )
    candidate_identity_sha256 = hashlib.sha256(
        active.backend_identity.encode("utf-8")
    ).hexdigest()
    generation = active.generation
    assert generation is not None
    await active.revoke_authority()
    await active.close()

    source_db = source_workspace / "runtime/proactive/proactive.sqlite3"
    source_binding = read_sqlite_proactive_backend_binding(source_db)
    candidate_db = candidate_workspace / "runtime/proactive/proactive.sqlite3"
    relocated_workspace = tmp_path / "relocated-workspace"
    binding_path = "runtime/proactive/backend-binding.json"
    relocated_db = relocated_workspace / "runtime/proactive/proactive.sqlite3"
    relocated_db.parent.mkdir(parents=True)
    shutil.copy2(candidate_db, relocated_db)
    for suffix in ("-wal", "-shm"):
        extra = Path(str(candidate_db) + suffix)
        if extra.exists():
            shutil.copy2(extra, Path(str(relocated_db) + suffix))
    stale_cache = relocated_workspace / binding_path
    stale_cache.write_bytes((source_workspace / binding_path).read_bytes())

    registry = FileAuthorityRegistry(
        relocated_workspace / "runtime/proactive/authority.json",
        registry_id="life-proactive-relocated",
    )
    await registry.register_generation(generation)
    token = await registry.activate_generation(
        generation.generation_id,
        expected_epoch=0,
        owner_id="relocated-test",
        lease_seconds=60,
        confirm_previous_writers_stopped=False,
    )
    sqlite_config = SQLiteStorageConfig(
        database_path=relocated_db,
        busy_timeout_seconds=10,
    )
    engine = create_sqlite_storage_engine(sqlite_config)
    relocated = StorageBackendRuntime(
        enabled=True,
        backend=BackendKind.LOCAL,
        backend_identity=sqlite_config.safe_identity,
        generation=generation,
        authority_registry=registry,
        authority_token=token,
        engine=engine,
        session_factory=async_sessionmaker(engine, expire_on_commit=False),
    )
    try:
        with pytest.raises(
            ProactiveBackendBindingConflict,
            match="ProactiveMigrationCertificateBackendIdentityMismatch",
        ):
            await ensure_proactive_backend_binding(
                workspace_path=relocated_workspace,
                binding_path=binding_path,
                runtime=relocated,
            )
        with pytest.raises(
            ProactiveBackendBindingConflict,
            match="ProactiveMigrationCertificateBackendIdentityMismatch",
        ):
            await complete_proactive_initial_binding(
                workspace_path=relocated_workspace,
                binding_path=binding_path,
                runtime=relocated,
                source_binding=source_binding,
                repair_id="initial-bind-relocated",
            )
        stale_identity = dict(source_binding["identity"])
        stale_identity["generation_id"] = "local-selectable-20260823-v2"
        stale_source = backend_binding._binding_payload(
            stale_identity,
            binding_epoch=1,
            previous_binding_sha256="",
            migration=(
                source_binding.get("migration")
                if isinstance(source_binding.get("migration"), dict)
                else None
            ),
        )
        with pytest.raises(
            ProactiveBackendBindingConflict,
            match="ProactiveMigrationSourceBindingMismatch",
        ):
            await complete_proactive_initial_binding(
                workspace_path=relocated_workspace,
                binding_path=binding_path,
                runtime=relocated,
                source_binding=stale_source,
                repair_id="initial-bind-stale-source",
                certificate_backend_identity_sha256=candidate_identity_sha256,
            )
        completed = await complete_proactive_initial_binding(
            workspace_path=relocated_workspace,
            binding_path=binding_path,
            runtime=relocated,
            source_binding=source_binding,
            repair_id="initial-bind-relocated",
            certificate_backend_identity_sha256=candidate_identity_sha256,
        )
        replayed = await complete_proactive_initial_binding(
            workspace_path=relocated_workspace,
            binding_path=binding_path,
            runtime=relocated,
            source_binding=source_binding,
            repair_id="initial-bind-relocated",
            certificate_backend_identity_sha256=candidate_identity_sha256,
        )
        health = await verify_proactive_backend_binding(
            workspace_path=relocated_workspace,
            binding_path=binding_path,
            runtime=relocated,
        )
        ensured = await ensure_proactive_backend_binding(
            workspace_path=relocated_workspace,
            binding_path=binding_path,
            runtime=relocated,
        )
    finally:
        await relocated.revoke_authority()
        await relocated.close()

    assert completed["binding_epoch"] == 1
    assert completed["identity"]["generation_id"] == generation.generation_id
    assert completed["identity"]["backend_identity_sha256"] == hashlib.sha256(
        sqlite_config.safe_identity.encode("utf-8")
    ).hexdigest()
    assert replayed == completed
    assert health["status"] == "healthy"
    assert ensured["identity"]["generation_id"] == generation.generation_id
