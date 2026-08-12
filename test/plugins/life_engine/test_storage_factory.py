from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from plugins.life_engine.core.config import LifeEngineConfig
from plugins.life_engine.storage import factory as factory_module
from plugins.life_engine.storage.authority import FileAuthorityRegistry
from plugins.life_engine.storage.contracts import StorageRuntimeDisabled
from plugins.life_engine.storage.factory import (
    GenerationGuardError,
    LocalBackendSettings,
    MySQLBackendSettings,
    StorageFactorySettings,
    open_storage_backend,
    settings_from_life_engine_config,
)
from plugins.life_engine.storage.models import (
    BackendGeneration,
    BackendKind,
    GenerationStatus,
)
from src.core.config.core_config import CoreConfig


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


def test_standalone_service_requires_explicit_global_storage_config() -> None:
    config = LifeEngineConfig()

    with pytest.raises(
        factory_module.StorageConfigurationError,
        match="global storage configuration is not initialized",
    ):
        settings_from_life_engine_config(config)


def test_global_local_mode_keeps_life_storage_runtime_inert() -> None:
    config = LifeEngineConfig()
    global_config = CoreConfig(
        storage=CoreConfig.StorageSection(backend="local"),
    )
    settings = settings_from_life_engine_config(
        config,
        global_config=global_config,
    )

    assert settings.enabled is False
    assert settings.authoritative_backend == BackendKind.LOCAL
    assert settings.backend_generation == ""
    assert settings.authority_provider == "file"


def test_global_local_mode_ignores_preconfigured_mysql_generation() -> None:
    """Switching to local changes only backend and ignores MySQL-only metadata."""

    settings = settings_from_life_engine_config(
        LifeEngineConfig(),
        global_config=CoreConfig(
            storage=CoreConfig.StorageSection(
                backend="local",
                backend_generation="remote-adopted-v1",
            )
        ),
    )

    assert settings.authoritative_backend == BackendKind.LOCAL
    assert settings.enabled is False
    assert settings.backend_generation == ""
    assert settings.authority_provider == "file"


def test_global_mysql_mode_uses_only_core_mysql_configuration() -> None:
    config = LifeEngineConfig()
    global_config = CoreConfig(
        storage=CoreConfig.StorageSection(
            backend="mysql",
            backend_generation="remote-adopted-v1",
            authority_owner_id="primary-writer",
        ),
        database=CoreConfig.DatabaseSection(
            mysql_host="db.example",
            mysql_port=3307,
            mysql_database="elysium_prod",
            mysql_user="elysia",
            mysql_password="resolved-secret",
            mysql_pool_recycle_seconds=120,
        ),
    )

    settings = settings_from_life_engine_config(
        config,
        global_config=global_config,
    )

    assert settings.enabled is True
    assert settings.authoritative_backend == BackendKind.MYSQL
    assert settings.backend_generation == "remote-adopted-v1"
    assert settings.authority_provider == "mysql"
    assert settings.authority_epoch == 0
    assert settings.authority_owner_id == "primary-writer"
    assert settings.mysql.host == "db.example"
    assert settings.mysql.port == 3307
    assert settings.mysql.database == "elysium_prod"
    assert settings.mysql.user == "elysia"
    assert settings.mysql.password == "resolved-secret"
    assert not hasattr(config, "storage_mysql")


def test_mysql_mode_reads_idle_session_timeout_from_config() -> None:
    """The engine-side wait_timeout must come from config, not a hardcoded 180."""

    settings = settings_from_life_engine_config(
        LifeEngineConfig(),
        global_config=CoreConfig(
            storage=CoreConfig.StorageSection(
                backend="mysql",
                backend_generation="remote-adopted-v1",
                authority_owner_id="primary-writer",
            ),
            database=CoreConfig.DatabaseSection(
                mysql_host="db.example",
                mysql_port=3307,
                mysql_database="elysium_prod",
                mysql_user="elysia",
                mysql_password="resolved-secret",
                mysql_pool_recycle_seconds=120,
                mysql_idle_session_timeout_seconds=240,
            ),
        ),
    )

    assert settings.mysql.pool_recycle_seconds == 120
    assert settings.mysql.idle_session_timeout_seconds == 240


async def test_mysql_mode_rejects_recycle_not_below_wait_timeout() -> None:
    """recycle >= wait_timeout is a startup error: idle connections would be
    killed by the server before the pool recycles them (MySQL 2013)."""

    settings = StorageFactorySettings(
        enabled=True,
        authoritative_backend=BackendKind.MYSQL,
        backend_generation="remote-adopted-v1",
        authority_provider="mysql",
        authority_owner_id="primary-writer",
        mysql=MySQLBackendSettings(
            host="db.example",
            port=3307,
            database="elysium_prod",
            user="elysia",
            password="resolved-secret",
            pool_recycle_seconds=1800,
            idle_session_timeout_seconds=180,
        ),
    )
    with pytest.raises(
        factory_module.StorageConfigurationError,
        match="must be smaller than",
    ):
        await open_storage_backend(settings)


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


async def test_mysql_factory_auto_acquires_without_static_epoch_or_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generation = BackendGeneration(
        generation_id="factory-mysql-v1",
        backend=BackendKind.MYSQL,
        schema_version=1,
        source_snapshot_sha256="c" * 64,
        root_hashes={"probe": "d" * 64},
        frontiers={"probe": 0},
        created_at="2026-08-04T00:00:00+00:00",
        verified_at="2026-08-04T00:01:00+00:00",
        status=GenerationStatus.VERIFIED,
    )
    acquired: dict[str, object] = {}

    class FakeEngine:
        async def dispose(self) -> None:
            acquired["disposed"] = True

    class FakeRegistry:
        def __init__(self, _engine: object, *, registry_id: str) -> None:
            acquired["registry_id"] = registry_id

        async def get_generation(self, generation_id: str) -> BackendGeneration | None:
            return generation if generation_id == generation.generation_id else None

        async def health(self) -> dict[str, object]:
            return {
                "status": "disabled",
                "authority_epoch": 7,
                "active_generation": "",
            }

        async def activate_generation(self, generation_id: str, **kwargs: object):
            acquired.update({"generation_id": generation_id, **kwargs})
            return SimpleNamespace(
                authority_epoch=8,
                owner_id=str(kwargs["owner_id"]),
            )

        async def validate_shared_in_transaction(
            self, _connection: object, _token: object
        ) -> None:
            return None

        async def validate_shared(self, _token: object) -> None:
            return None

    monkeypatch.setattr(factory_module, "create_mysql_storage_engine", lambda _cfg: FakeEngine())
    monkeypatch.setattr(factory_module, "MySQLAuthorityRegistry", FakeRegistry)
    monkeypatch.setattr(factory_module, "async_sessionmaker", lambda *_args, **_kwargs: object())

    runtime = await open_storage_backend(
        StorageFactorySettings(
            enabled=True,
            authoritative_backend=BackendKind.MYSQL,
            backend_generation=generation.generation_id,
            registry_id="factory-auto-acquire",
            authority_provider="mysql",
            authority_epoch=0,
            authority_owner_id="primary-writer",
            mysql=MySQLBackendSettings(password="secret", pool_recycle_seconds=120),
        ),
        environment={},
    )
    try:
        assert runtime.authority_token is not None
        assert runtime.shared_writers is True
        assert acquired["expected_epoch"] == 7
        assert acquired["owner_id"] == "primary-writer"
        assert acquired["confirm_previous_writers_stopped"] is False
    finally:
        await runtime.close()


async def test_mysql_factory_joins_live_shared_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generation = BackendGeneration(
        generation_id="factory-mysql-v1",
        backend=BackendKind.MYSQL,
        schema_version=1,
        source_snapshot_sha256="c" * 64,
        root_hashes={"probe": "d" * 64},
        frontiers={"probe": 0},
        created_at="2026-08-04T00:00:00+00:00",
        verified_at="2026-08-04T00:01:00+00:00",
        status=GenerationStatus.VERIFIED,
    )

    joined: dict[str, object] = {}
    shared_validations: list[object] = []

    class FakeEngine:
        async def dispose(self) -> None:
            return None

    class FakeRegistry:
        def __init__(self, _engine: object, *, registry_id: str) -> None:
            self.registry_id = registry_id

        async def get_generation(self, _generation_id: str) -> BackendGeneration:
            return generation

        async def health(self) -> dict[str, object]:
            return {
                "status": "healthy",
                "authority_epoch": 9,
                "active_generation": generation.generation_id,
            }

        async def join_generation(self, generation_id: str, **kwargs: object):
            joined.update({"generation_id": generation_id, **kwargs})
            return SimpleNamespace(authority_epoch=9, owner_id=kwargs["owner_id"])

        async def validate_shared_in_transaction(
            self, _connection: object, _token: object
        ) -> None:
            return None

        async def validate_shared(self, token: object) -> None:
            shared_validations.append(token)

    monkeypatch.setattr(factory_module, "create_mysql_storage_engine", lambda _cfg: FakeEngine())
    monkeypatch.setattr(factory_module, "MySQLAuthorityRegistry", FakeRegistry)
    monkeypatch.setattr(factory_module, "async_sessionmaker", lambda *_args, **_kwargs: object())

    runtime = await open_storage_backend(
        StorageFactorySettings(
            enabled=True,
            authoritative_backend=BackendKind.MYSQL,
            backend_generation=generation.generation_id,
            registry_id="factory-live-shared",
            authority_provider="mysql",
            authority_owner_id="developer-two",
            mysql=MySQLBackendSettings(password="secret", pool_recycle_seconds=120),
        ),
        environment={},
    )
    try:
        assert runtime.shared_writers is True
        assert joined == {
            "generation_id": generation.generation_id,
            "owner_id": "developer-two",
        }
        await runtime.renew_authority(lease_seconds=60)
        assert shared_validations == [runtime.authority_token]
    finally:
        await runtime.close()


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
