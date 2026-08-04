"""Non-destructive real-MySQL verification for candidate-copy fencing."""

from __future__ import annotations

import os
from dataclasses import replace
from uuid import uuid4

import pytest

from plugins.life_engine.storage.authority import MySQLAuthorityRegistry
from plugins.life_engine.storage.event_factory import open_life_event_store
from plugins.life_engine.storage.migration.copy_authority import (
    CopyAuthorityConflict,
    MySQLCopyAuthorityRegistry,
    StaleCopyAuthority,
    open_mysql_copy_runtime,
)
from src.kernel.storage.engine import (
    MySQLStorageConfig,
    create_mysql_storage_engine,
)
from test.plugins.life_engine.test_life_event_storage_contract import _event


def _config() -> MySQLStorageConfig:
    host = os.environ.get("ELYSIUM_TEST_MYSQL_HOST", "")
    database = os.environ.get("ELYSIUM_TEST_MYSQL_DATABASE", "")
    user = os.environ.get("ELYSIUM_TEST_MYSQL_USER", "")
    if not host or not database or not user:
        pytest.skip("MySQL copy-control integration is not configured")
    if os.environ.get("ELYSIUM_TEST_MYSQL_COPY_CONTROL") != "1":
        pytest.skip("MySQL copy-control integration requires explicit opt-in")
    return MySQLStorageConfig(
        host=host,
        port=int(os.environ.get("ELYSIUM_TEST_MYSQL_PORT", "3306")),
        database=database,
        user=user,
        password=os.environ.get("ELYSIUM_TEST_MYSQL_PASSWORD", ""),
        ssl_mode=os.environ.get("ELYSIUM_TEST_MYSQL_SSL_MODE", "disabled"),  # type: ignore[arg-type]
    )


@pytest.mark.timeout(120)
async def test_candidate_copy_does_not_activate_backend_authority() -> None:
    """Copy real data under an independent lease and retain candidate status."""

    config = _config()
    engine = create_mysql_storage_engine(config)
    active_registry = MySQLAuthorityRegistry(engine, registry_id="life-domain")
    copy_registry = MySQLCopyAuthorityRegistry(engine)
    identity = uuid4().hex
    run_id = f"copy-contract-{identity}"
    runtime = None
    try:
        active_before = await active_registry.health()
        await copy_registry.create_run(
            run_id=run_id,
            source_manifest_sha256="5" * 64,
            source_snapshot_sha256="6" * 64,
            writer_frozen=False,
            metadata={"contract": identity},
        )
        with pytest.raises(CopyAuthorityConflict):
            await copy_registry.create_run(
                run_id=run_id,
                source_manifest_sha256="5" * 64,
                source_snapshot_sha256="6" * 64,
                writer_frozen=False,
                metadata={"contract": "different"},
            )
        token = await copy_registry.acquire(
            run_id,
            expected_epoch=0,
            owner_id=f"copy-writer-{identity}",
            lease_seconds=120,
        )
        runtime = open_mysql_copy_runtime(
            copy_registry,
            token,
            backend_identity=config.safe_identity,
        )
        store = await open_life_event_store(
            runtime,
            initialize_schema=True,
            require_database_immutability=False,
        )
        event = _event(identity, sync_export=True, visibility="shared")
        persisted = await store.append(event)
        assert await store.append(event) == persisted
        await copy_registry.add_progress(token, copied_records=1)
        completed = await copy_registry.complete(
            token,
            verification={"verified": True, "event_position": persisted.sequence},
        )
        assert completed["state"] == "copied"
        assert completed["writer_frozen"] is False
        assert completed["copied_records"] == 1
        with pytest.raises(StaleCopyAuthority):
            await store.append(replace(_event(f"{identity}-stale"), sequence=18))

        active_after = await active_registry.health()
        assert active_after.get("active_generation") == active_before.get(
            "active_generation"
        )
        assert active_after.get("authority_epoch") == active_before.get(
            "authority_epoch"
        )
    finally:
        if runtime is not None:
            await runtime.close()
        else:
            await engine.dispose()
