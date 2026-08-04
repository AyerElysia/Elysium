from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text

from plugins.life_engine.core.config import LifeEngineConfig
from plugins.life_engine.storage.authority import FileAuthorityRegistry
from plugins.life_engine.storage.contracts import StorageRuntimeDisabled
from plugins.life_engine.storage.factory import (
    GenerationGuardError,
    LocalBackendSettings,
    StorageFactorySettings,
    open_storage_backend,
    settings_from_life_engine_config,
)
from plugins.life_engine.storage.models import (
    BackendGeneration,
    BackendKind,
    GenerationStatus,
)


def _generation() -> BackendGeneration:
    return BackendGeneration(
        generation_id="factory-local-v1",
        backend=BackendKind.LOCAL,
        schema_version=1,
        source_snapshot_sha256="a" * 64,
        root_hashes={"probe": "b" * 64},
        frontiers={"probe": 0},
        created_at="2026-08-04T00:00:00+00:00",
        verified_at="2026-08-04T00:01:00+00:00",
        status=GenerationStatus.VERIFIED,
    )


def test_life_engine_storage_config_defaults_are_inert_and_secret_free() -> None:
    config = LifeEngineConfig()
    settings = settings_from_life_engine_config(config)

    assert settings.enabled is False
    assert settings.authoritative_backend == BackendKind.LOCAL
    assert settings.backend_generation == ""
    assert settings.mysql.password_env == "ELYSIUM_LIFE_STORAGE_MYSQL_PASSWORD"
    assert not hasattr(config.storage_mysql, "password")


async def test_disabled_factory_does_not_create_local_files(tmp_path: Path) -> None:
    database_path = tmp_path / "not-created.sqlite3"
    runtime = await open_storage_backend(
        StorageFactorySettings(
            enabled=False,
            local=LocalBackendSettings(database_path=database_path),
        )
    )

    assert (await runtime.health())["status"] == "disabled"
    assert not database_path.exists()
    with pytest.raises(StorageRuntimeDisabled):
        runtime.unit_of_work()


async def test_local_factory_opens_one_fenced_generation_and_persists(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "storage.sqlite3"
    authority_path = tmp_path / "authority.json"
    registry = FileAuthorityRegistry(authority_path)
    await registry.register_generation(_generation())
    token = await registry.activate_generation(
        "factory-local-v1",
        expected_epoch=0,
        owner_id="factory-test",
        lease_seconds=60,
        confirm_previous_writers_stopped=True,
    )
    settings = StorageFactorySettings(
        enabled=True,
        authoritative_backend=BackendKind.LOCAL,
        backend_generation="factory-local-v1",
        schema_version=1,
        authority_epoch=1,
        authority_owner_id="factory-test",
        fencing_token_env="TEST_FENCING_TOKEN",
        local=LocalBackendSettings(
            database_path=database_path,
            authority_state_path=authority_path,
        ),
    )
    runtime = await open_storage_backend(
        settings,
        environment={"TEST_FENCING_TOKEN": token.fencing_token},
    )
    try:
        async with runtime.engine.begin() as connection:
            await connection.execute(text("CREATE TABLE probe (value INTEGER NOT NULL)"))
        async with runtime.unit_of_work() as uow:
            await uow.session.execute(text("INSERT INTO probe VALUES (42)"))
        health = await runtime.health()
        async with runtime.engine.connect() as connection:
            value = await connection.scalar(text("SELECT value FROM probe"))
    finally:
        await runtime.close()

    assert value == 42
    assert health["status"] == "healthy"
    assert health["generation_id"] == "factory-local-v1"


async def test_runtime_health_degrades_if_authority_is_revoked(tmp_path: Path) -> None:
    database_path = tmp_path / "storage.sqlite3"
    authority_path = tmp_path / "authority.json"
    registry = FileAuthorityRegistry(authority_path)
    await registry.register_generation(_generation())
    token = await registry.activate_generation(
        "factory-local-v1",
        expected_epoch=0,
        owner_id="factory-test",
        lease_seconds=60,
        confirm_previous_writers_stopped=True,
    )
    runtime = await open_storage_backend(
        StorageFactorySettings(
            enabled=True,
            authoritative_backend=BackendKind.LOCAL,
            backend_generation="factory-local-v1",
            schema_version=1,
            authority_epoch=1,
            authority_owner_id="factory-test",
            fencing_token_env="TEST_FENCING_TOKEN",
            local=LocalBackendSettings(
                database_path=database_path,
                authority_state_path=authority_path,
            ),
        ),
        environment={"TEST_FENCING_TOKEN": token.fencing_token},
    )
    try:
        await registry.revoke(token)
        health = await runtime.health()
    finally:
        await runtime.close()

    assert health["status"] == "degraded"
    assert health["authority_health"]["status"] == "disabled"


async def test_factory_rejects_generation_schema_mismatch(tmp_path: Path) -> None:
    authority_path = tmp_path / "authority.json"
    registry = FileAuthorityRegistry(authority_path)
    await registry.register_generation(_generation())
    token = await registry.activate_generation(
        "factory-local-v1",
        expected_epoch=0,
        owner_id="factory-test",
        lease_seconds=60,
        confirm_previous_writers_stopped=True,
    )
    settings = StorageFactorySettings(
        enabled=True,
        authoritative_backend=BackendKind.LOCAL,
        backend_generation="factory-local-v1",
        schema_version=2,
        authority_epoch=1,
        authority_owner_id="factory-test",
        fencing_token_env="TEST_FENCING_TOKEN",
        local=LocalBackendSettings(
            database_path=tmp_path / "storage.sqlite3",
            authority_state_path=authority_path,
        ),
    )

    with pytest.raises(GenerationGuardError):
        await open_storage_backend(
            settings,
            environment={"TEST_FENCING_TOKEN": token.fencing_token},
        )
