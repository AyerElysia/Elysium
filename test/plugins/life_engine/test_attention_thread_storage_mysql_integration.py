"""Opt-in real MySQL contract for AttentionThread authority."""

from __future__ import annotations

import os
from dataclasses import replace
from uuid import uuid4

import pytest

from plugins.life_engine.attention_threads import AttentionThreadCommand
from plugins.life_engine.storage.attention_factory import (
    open_attention_thread_stores,
)
from plugins.life_engine.storage.authority import MySQLAuthorityRegistry
from plugins.life_engine.storage.contracts import StorageBackendRuntime
from plugins.life_engine.storage.domain_factory import open_presence_world_stores
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
from src.kernel.storage.engine import MySQLStorageConfig, create_mysql_storage_engine


def _mysql_config() -> MySQLStorageConfig:
    if os.environ.get("ELYSIUM_TEST_MYSQL_ATTENTION_ISOLATED") != "1":
        pytest.skip("isolated AttentionThread MySQL contract is not enabled")
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
            "ELYSIUM_TEST_MYSQL_SSL_MODE",
            "disabled",
        ),
    )


def _generation() -> BackendGeneration:
    return BackendGeneration(
        generation_id="mysql-life-attention-contract-v1",
        backend=BackendKind.MYSQL,
        schema_version=1,
        source_snapshot_sha256="5" * 64,
        root_hashes={"attention_threads": "6" * 64},
        frontiers={"attention_threads": 0},
        created_at="2026-08-06T00:00:00+00:00",
        verified_at="2026-08-06T00:01:00+00:00",
        status=GenerationStatus.VERIFIED,
    )


@pytest.mark.timeout(180)
async def test_mysql_attention_actor_gate_cas_and_restart() -> None:
    config = _mysql_config()
    engine = create_mysql_storage_engine(config)
    registry = MySQLAuthorityRegistry(engine, registry_id="life-attention-integration")
    runtime: StorageBackendRuntime | None = None
    token = None
    suffix = uuid4().hex
    try:
        generation = _generation()
        await registry.register_generation(generation)
        health = await registry.health()
        token = await registry.activate_generation(
            generation.generation_id,
            expected_epoch=int(health.get("authority_epoch") or 0),
            owner_id="life-attention-integration-writer",
            lease_seconds=180,
            confirm_previous_writers_stopped=True,
        )
        runtime = await open_storage_backend(
            StorageFactorySettings(
                enabled=True,
                authoritative_backend=BackendKind.MYSQL,
                backend_generation=generation.generation_id,
                schema_version=1,
                registry_id="life-attention-integration",
                authority_provider="mysql",
                authority_epoch=token.authority_epoch,
                authority_owner_id=token.owner_id,
                fencing_token_env="TEST_ATTENTION_MYSQL_FENCE",
                mysql=MySQLBackendSettings(
                    host=config.host,
                    port=config.port,
                    database=config.database,
                    user=config.user,
                    password_env="TEST_ATTENTION_MYSQL_PASSWORD",
                    ssl_mode=config.ssl_mode,
                ),
            ),
            environment={
                "TEST_ATTENTION_MYSQL_FENCE": token.fencing_token,
                "TEST_ATTENTION_MYSQL_PASSWORD": config.password,
            },
        )
        presence_world = await open_presence_world_stores(
            runtime,
            initialize_schema=True,
        )
        actor = f"attention:mysql:actor:{suffix}"
        await presence_world.presence.commit(
            {
                "instance_id": actor,
                "kind": "mysql-contract",
                "display_name": "",
                "status": "active",
                "created_at": "2026-08-06T00:00:00+00:00",
                "last_active_at": "2026-08-06T00:00:00+00:00",
                "suspended_at": "",
                "stream_ids": [f"stream:{suffix}"],
                "perception_filter": {},
                "metadata": {},
                "session_id": f"session:{suffix}",
                "process_epoch": f"process:{suffix}",
                "lease_expires_at": "",
                "lease_duration_seconds": None,
                "revision": 0,
            },
            expected_revision=None,
            event_type="consciousness.instance_registered",
            event_payload={"occurred_at": "2026-08-06T00:00:00+00:00"},
        )
        stores = await open_attention_thread_stores(
            runtime,
            initialize_schema=True,
        )
        command = AttentionThreadCommand(
            occurrence_id=f"attention:mysql:decision:{suffix}",
            thread_id=f"attention:mysql:thread:{suffix}",
            action="open",
            actor_consciousness_instance_id=actor,
            source_instance_id=actor,
            source_occurrence_ids=(f"source:{suffix}",),
            causation_occurrence_id=f"cause:{suffix}",
            expected_revision=0,
            public_statement="我明确选择保留这条 MySQL UTF-8 线索🌸。",
            occurred_at="2026-08-06T01:02:03.123456+00:00",
        )
        first = await stores.authority.decide(command)
        assert await stores.authority.decide(command) == replace(
            first,
            idempotent_replay=True,
        )
        reopened = await open_attention_thread_stores(runtime)
        view = await reopened.authority.get(command.thread_id)
        assert view is not None
        assert view.current_statement == command.public_statement
        assert (await reopened.authority.health_snapshot())["status"] == "healthy"
    finally:
        try:
            if runtime is not None:
                await runtime.close()
        finally:
            try:
                if token is not None:
                    await registry.revoke(token)
            finally:
                await engine.dispose()
