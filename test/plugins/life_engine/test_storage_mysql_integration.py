from __future__ import annotations

import os
from uuid import uuid4

import pytest
from sqlalchemy import text

from plugins.life_engine.storage.authority import MySQLAuthorityRegistry
from plugins.life_engine.storage.factory import (
    MySQLBackendSettings,
    StorageFactorySettings,
    open_storage_backend,
)
from plugins.life_engine.storage.models import (
    BackendGeneration,
    BackendKind,
    GenerationStatus,
)
from src.kernel.storage.engine import (
    MySQLStorageConfig,
    create_mysql_storage_engine,
    mysql_storage_health,
)
from src.kernel.storage.migration_runner import (
    MySQLMigrationRunner,
    SchemaMigration,
)


def _mysql_config_from_environment() -> MySQLStorageConfig:
    host = os.environ.get("ELYSIUM_TEST_MYSQL_HOST", "")
    database = os.environ.get("ELYSIUM_TEST_MYSQL_DATABASE", "")
    user = os.environ.get("ELYSIUM_TEST_MYSQL_USER", "")
    if not host or not database or not user:
        pytest.skip("isolated MySQL integration database is not configured")
    return MySQLStorageConfig(
        host=host,
        port=int(os.environ.get("ELYSIUM_TEST_MYSQL_PORT", "3306")),
        database=database,
        user=user,
        password=os.environ.get("ELYSIUM_TEST_MYSQL_PASSWORD", ""),
        ssl_mode=os.environ.get("ELYSIUM_TEST_MYSQL_SSL_MODE", "disabled"),  # type: ignore[arg-type]
    )


_PROBE_MIGRATION = SchemaMigration(
    version=1,
    name="storage_contract_probe",
    statements=(
        """CREATE TABLE IF NOT EXISTS storage_contract_probe (
            occurrence_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
            payload VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            authority_epoch BIGINT UNSIGNED NOT NULL,
            created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci""",
    ),
)


def _generation() -> BackendGeneration:
    return BackendGeneration(
        generation_id="mysql-integration-contract-v1",
        backend=BackendKind.MYSQL,
        schema_version=1,
        source_snapshot_sha256="c" * 64,
        root_hashes={"contract-probe": "d" * 64},
        frontiers={"contract-probe": 0},
        created_at="2026-08-04T00:00:00+00:00",
        verified_at="2026-08-04T00:01:00+00:00",
        status=GenerationStatus.VERIFIED,
    )


@pytest.mark.timeout(120)
async def test_mysql_runtime_migrates_fences_commits_and_recovers() -> None:
    config = _mysql_config_from_environment()
    engine = create_mysql_storage_engine(config)
    registry = MySQLAuthorityRegistry(engine, registry_id="life-storage-integration")
    runtime = None
    token = None
    occurrence_id = str(uuid4())
    try:
        health = await mysql_storage_health(
            engine,
            backend_identity=config.safe_identity,
        )
        assert health["status"] == "healthy"

        runner = MySQLMigrationRunner(
            engine,
            table_name="storage_contract_schema_migrations",
            lock_name="elysium:storage-contract-schema",
        )
        first = await runner.apply((_PROBE_MIGRATION,))
        second = await runner.apply((_PROBE_MIGRATION,))
        assert first.current_version == second.current_version == 1
        assert second.applied_versions == ()

        await registry.register_generation(_generation())
        authority_health = await registry.health()
        expected_epoch = int(authority_health.get("authority_epoch") or 0)
        token = await registry.activate_generation(
            "mysql-integration-contract-v1",
            expected_epoch=expected_epoch,
            owner_id="integration-writer",
            lease_seconds=120,
            confirm_previous_writers_stopped=True,
        )
        runtime = await open_storage_backend(
            StorageFactorySettings(
                enabled=True,
                authoritative_backend=BackendKind.MYSQL,
                backend_generation="mysql-integration-contract-v1",
                schema_version=1,
                registry_id="life-storage-integration",
                authority_provider="mysql",
                authority_epoch=token.authority_epoch,
                authority_owner_id="integration-writer",
                fencing_token_env="TEST_STORAGE_FENCE",
                mysql=MySQLBackendSettings(
                    host=config.host,
                    port=config.port,
                    database=config.database,
                    user=config.user,
                    password_env="TEST_STORAGE_MYSQL_PASSWORD",
                    ssl_mode=config.ssl_mode,
                ),
            ),
            environment={
                "TEST_STORAGE_FENCE": token.fencing_token,
                "TEST_STORAGE_MYSQL_PASSWORD": config.password,
            },
        )
        async with runtime.unit_of_work() as uow:
            await uow.session.execute(
                text(
                    "INSERT INTO storage_contract_probe "
                    "(occurrence_id, payload, authority_epoch) "
                    "VALUES (:occurrence_id, :payload, :authority_epoch)"
                ),
                {
                    "occurrence_id": occurrence_id,
                    "payload": "爱莉 storage contract",
                    "authority_epoch": token.authority_epoch,
                },
            )
        async with runtime.engine.connect() as connection:
            payload = await connection.scalar(
                text(
                    "SELECT payload FROM storage_contract_probe "
                    "WHERE occurrence_id = :occurrence_id"
                ),
                {"occurrence_id": occurrence_id},
            )
        assert payload == "爱莉 storage contract"
        assert (await runtime.health())["status"] == "healthy"
    finally:
        if runtime is not None:
            if runtime.engine is not None:
                async with runtime.engine.begin() as connection:
                    await connection.execute(
                        text(
                            "DELETE FROM storage_contract_probe "
                            "WHERE occurrence_id = :occurrence_id"
                        ),
                        {"occurrence_id": occurrence_id},
                    )
            await runtime.close()
        if token is not None:
            await registry.revoke(token)
        await engine.dispose()
