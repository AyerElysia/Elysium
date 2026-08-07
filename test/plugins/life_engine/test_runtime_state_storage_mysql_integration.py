"""Opt-in real MySQL contract for singleton runtime-state writers."""

from __future__ import annotations

import os
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from plugins.life_engine.storage.authority import MySQLAuthorityRegistry
from plugins.life_engine.storage.contracts import StorageBackendRuntime
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
from plugins.life_engine.storage.runtime_factory import open_runtime_state_store
from plugins.life_engine.storage.writer_claims import (
    SingletonWriterClaimConflict,
    SingletonWriterClaimLost,
)
from src.kernel.storage.engine import MySQLStorageConfig, create_mysql_storage_engine


def _mysql_config() -> MySQLStorageConfig:
    if os.environ.get("ELYSIUM_TEST_MYSQL_RUNTIME_ISOLATED") != "1":
        pytest.skip("isolated runtime-state MySQL contract is not explicitly enabled")
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
        ssl_mode=os.environ.get(  # type: ignore[arg-type]
            "ELYSIUM_TEST_MYSQL_SSL_MODE", "disabled"
        ),
    )


def _generation(suffix: str) -> BackendGeneration:
    return BackendGeneration(
        generation_id=f"mysql-runtime-writer-contract-{suffix}",
        backend=BackendKind.MYSQL,
        schema_version=1,
        source_snapshot_sha256="7" * 64,
        root_hashes={"runtime-state": "8" * 64},
        frontiers={"runtime-state": 0},
        created_at="2026-08-07T00:00:00+00:00",
        verified_at="2026-08-07T00:01:00+00:00",
        status=GenerationStatus.VERIFIED,
    )


async def _runtime(
    config: MySQLStorageConfig,
    *,
    registry_id: str,
    generation_id: str,
    owner_id: str,
) -> StorageBackendRuntime:
    return await open_storage_backend(
        StorageFactorySettings(
            enabled=True,
            authoritative_backend=BackendKind.MYSQL,
            backend_generation=generation_id,
            schema_version=1,
            registry_id=registry_id,
            authority_provider="mysql",
            authority_owner_id=owner_id,
            authority_lease_seconds=60,
            mysql=MySQLBackendSettings(
                host=config.host,
                port=config.port,
                database=config.database,
                user=config.user,
                password_env="TEST_RUNTIME_MYSQL_PASSWORD",
                ssl_mode=config.ssl_mode,
            ),
        ),
        environment={"TEST_RUNTIME_MYSQL_PASSWORD": config.password},
    )


@pytest.mark.timeout(180)
async def test_mysql_singleton_writer_claim_and_database_trigger_contract() -> None:
    config = _mysql_config()
    suffix = uuid4().hex
    registry_id = f"runtime-writer-{suffix}"
    engine = create_mysql_storage_engine(config)
    registry = MySQLAuthorityRegistry(engine, registry_id=registry_id)
    first_runtime: StorageBackendRuntime | None = None
    second_runtime: StorageBackendRuntime | None = None
    try:
        generation = _generation(suffix)
        await registry.register_generation(generation)
        first_runtime = await _runtime(
            config,
            registry_id=registry_id,
            generation_id=generation.generation_id,
            owner_id=f"writer-a-{suffix}",
        )
        first_store = await open_runtime_state_store(
            first_runtime,
            initialize_schema=True,
        )
        namespace = f"runtime.contract.{suffix}"
        claim = await first_runtime.acquire_singleton_writer(
            namespace=namespace,
            state_key="global",
            owner_instance_id=f"host-a:pid-100:{suffix}",
            lease_seconds=60,
        )
        first = await first_store.put_state(
            namespace=namespace,
            state_key="global",
            expected_revision=0,
            schema_version=1,
            payload={"revision": 1},
            writer_claim=claim,
        )
        assert first.revision == 1

        second_runtime = await _runtime(
            config,
            registry_id=registry_id,
            generation_id=generation.generation_id,
            owner_id=f"writer-b-{suffix}",
        )
        second_store = await open_runtime_state_store(second_runtime)
        with pytest.raises(SingletonWriterClaimConflict, match="host-a:pid-100"):
            await second_runtime.acquire_singleton_writer(
                namespace=namespace,
                state_key="global",
                owner_instance_id=f"host-b:pid-200:{suffix}",
                lease_seconds=60,
            )
        with pytest.raises(SingletonWriterClaimLost, match="ClaimRequired"):
            await second_store.put_state(
                namespace=namespace,
                state_key="global",
                expected_revision=1,
                schema_version=1,
                payload={"revision": 2},
            )

        # Prove the database trigger rejects an old client that bypasses the
        # adapter and therefore never creates a transaction binding.
        with pytest.raises(DBAPIError, match="RuntimeStateSingletonWriterClaimRequired"):
            async with second_runtime.unit_of_work() as uow:
                await uow.session.execute(
                    text(
                        """UPDATE runtime_states SET revision = revision + 1
                        WHERE namespace = :namespace AND state_key = 'global'"""
                    ),
                    {"namespace": namespace},
                )

        assert await first_runtime.release_singleton_writer(claim) is True
        takeover = await second_runtime.acquire_singleton_writer(
            namespace=namespace,
            state_key="global",
            owner_instance_id=f"host-b:pid-200:{suffix}",
            lease_seconds=60,
        )
        assert takeover.lease_epoch == 2
        with pytest.raises(SingletonWriterClaimLost, match="ClaimLost"):
            await first_store.put_state(
                namespace=namespace,
                state_key="global",
                expected_revision=1,
                schema_version=1,
                payload={"revision": 2},
                writer_claim=claim,
            )
        second = await second_store.put_state(
            namespace=namespace,
            state_key="global",
            expected_revision=1,
            schema_version=1,
            payload={"revision": 2},
            writer_claim=takeover,
        )
        assert second.revision == 2
    finally:
        for runtime in (second_runtime, first_runtime):
            if runtime is None:
                continue
            try:
                await runtime.revoke_authority()
            finally:
                await runtime.close()
        await engine.dispose()
