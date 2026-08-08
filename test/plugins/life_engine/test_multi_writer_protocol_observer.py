from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from plugins.life_engine.storage.authority import FileAuthorityRegistry
from plugins.life_engine.storage.contracts import StorageBackendRuntime
from plugins.life_engine.storage.factory import (
    LocalBackendSettings,
    StorageFactorySettings,
    open_storage_backend,
)
from plugins.life_engine.storage.models import BackendGeneration, BackendKind, GenerationStatus
from plugins.life_engine.storage.multi_writer_protocol import (
    LEGACY_RUNTIME_CONTEXT_NAMESPACE,
    LEGACY_RUNTIME_CONTEXT_STATE_KEY,
    MultiWriterProtocolConfig,
    MultiWriterProtocolError,
    MultiWriterRuntimeState,
    observe_multi_writer_state,
    validate_multi_writer_readiness,
)
from plugins.life_engine.storage.runtime_schema import ensure_runtime_state_schema
from plugins.life_engine.storage.writer_claims import ensure_singleton_writer_claim_schema


async def _runtime(tmp_path: Path) -> StorageBackendRuntime:
    authority_path = tmp_path / "authority.json"
    registry = FileAuthorityRegistry(authority_path, registry_id="test")
    await registry.register_generation(
        BackendGeneration(
            generation_id="local-test",
            backend=BackendKind.LOCAL,
            schema_version=1,
            source_snapshot_sha256="a" * 64,
            root_hashes={"runtime": "b" * 64},
            frontiers={"runtime": 0},
            created_at="2026-08-07T00:00:00+00:00",
            verified_at="2026-08-07T00:00:00+00:00",
            status=GenerationStatus.VERIFIED,
        )
    )
    token = await registry.activate_generation(
        "local-test",
        expected_epoch=0,
        owner_id="test-owner",
        lease_seconds=120,
        confirm_previous_writers_stopped=True,
    )
    runtime = await open_storage_backend(
        StorageFactorySettings(
            enabled=True,
            authoritative_backend=BackendKind.LOCAL,
            backend_generation="local-test",
            authority_provider="file",
            registry_id="test",
            authority_epoch=token.authority_epoch,
            authority_owner_id="test-owner",
            local=LocalBackendSettings(
                database_path=tmp_path / "shared.sqlite3",
                authority_state_path=authority_path,
            ),
        ),
        environment={"ELYSIUM_LIFE_STORAGE_FENCING_TOKEN": token.fencing_token},
    )
    await ensure_runtime_state_schema(runtime)
    return runtime


class _BareRuntime:
    """Minimal stand-in exposing only what observe_multi_writer_state reads."""

    enabled = True
    generation = None
    session_factory: async_sessionmaker  # type: ignore[assignment]


async def _seed_legacy_claim(
    runtime: StorageBackendRuntime, *, released: bool, generation_id: str = "local-test"
) -> None:
    await ensure_singleton_writer_claim_schema(runtime)
    released_at = None if not released else "2026-08-07T00:05:00+00:00"
    async with runtime.session_factory() as session:
        await session.execute(
            text(
                """
                INSERT INTO runtime_singleton_writer_claims (
                    generation_id, namespace, state_key, owner_instance_id,
                    lease_epoch, fencing_token_sha256, acquired_at, renewed_at,
                    lease_until, released_at
                ) VALUES (
                    :generation_id, :namespace, :state_key, :owner,
                    1, :token, :now, :now, :now, :released_at
                )
                """
            ),
            {
                "generation_id": generation_id,
                "namespace": LEGACY_RUNTIME_CONTEXT_NAMESPACE,
                "state_key": LEGACY_RUNTIME_CONTEXT_STATE_KEY,
                "owner": "legacy-writer",
                "token": "c" * 64,
                "now": "2026-08-07T00:00:00+00:00",
                "released_at": released_at,
            },
        )
        await session.commit()


@pytest.mark.asyncio
async def test_observer_safe_when_tables_absent(tmp_path: Path) -> None:
    db_path = tmp_path / "bare.sqlite3"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    runtime = _BareRuntime()
    runtime.session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        state = await observe_multi_writer_state(runtime)
        assert isinstance(state, MultiWriterRuntimeState)
        assert state.legacy_singleton_table_present is False
        assert state.total_legacy_global_claims == 0
        assert state.live_legacy_global_claims == 0
        assert state.multi_writer_tables_present is False
        assert state.legacy_singleton_retired is True
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_observer_reports_deployed_schema_without_legacy_claim(tmp_path: Path) -> None:
    runtime = await _runtime(tmp_path)
    try:
        state = await observe_multi_writer_state(runtime)
        assert state.legacy_singleton_table_present is True
        assert state.total_legacy_global_claims == 0
        assert state.live_legacy_global_claims == 0
        assert state.multi_writer_tables_present is True
        assert state.legacy_singleton_retired is True
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_observer_detects_live_legacy_global_claim(tmp_path: Path) -> None:
    runtime = await _runtime(tmp_path)
    try:
        await _seed_legacy_claim(runtime, released=False)
        state = await observe_multi_writer_state(runtime)
        assert state.legacy_singleton_table_present is True
        assert state.total_legacy_global_claims == 1
        assert state.live_legacy_global_claims == 1
        assert state.legacy_singleton_retired is False
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_observer_treats_released_claim_as_retired(tmp_path: Path) -> None:
    runtime = await _runtime(tmp_path)
    try:
        await _seed_legacy_claim(runtime, released=True)
        state = await observe_multi_writer_state(runtime)
        assert state.legacy_singleton_table_present is True
        assert state.total_legacy_global_claims == 1
        assert state.live_legacy_global_claims == 0
        assert state.legacy_singleton_retired is True
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_readiness_gate_uses_observed_singleton_state(tmp_path: Path) -> None:
    runtime = await _runtime(tmp_path)
    try:
        await _seed_legacy_claim(runtime, released=False)
        state = await observe_multi_writer_state(runtime)
        with pytest.raises(MultiWriterProtocolError):
            validate_multi_writer_readiness(
                config=MultiWriterProtocolConfig(require_singleton_retired=True),
                generation_schema_version=3,
                observed_protocol_version=1,
                singleton_retired=state.legacy_singleton_retired,
            )
    finally:
        await runtime.close()
