"""Opt-in real MySQL contract for the additive production Memory upgrade."""

from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from plugins.life_engine.storage.authority import MySQLAuthorityRegistry
from plugins.life_engine.storage.memory.schema import (
    MEMORY_IMMUTABILITY_MIGRATIONS,
    MEMORY_MIGRATIONS,
)
from plugins.life_engine.storage.models import (
    BackendGeneration,
    BackendKind,
    GenerationStatus,
)
from plugins.life_engine.storage.writer_claims import (
    MYSQL_SINGLETON_WRITER_MIGRATION,
)
from scripts import adopt_life_mysql_baseline as baseline
from src.kernel.storage import MySQLMigrationRunner
from src.kernel.storage.engine import MySQLStorageConfig, create_mysql_storage_engine


def _mysql_config() -> MySQLStorageConfig:
    host = os.environ.get("ELYSIUM_TEST_MYSQL_HOST", "")
    database = os.environ.get("ELYSIUM_TEST_MYSQL_DATABASE", "")
    user = os.environ.get("ELYSIUM_TEST_MYSQL_USER", "")
    if not host or not database or not user:
        pytest.skip("isolated MySQL integration database is not configured")
    if os.environ.get("ELYSIUM_TEST_MYSQL_MEMORY_UPGRADE_ISOLATED") != "1":
        pytest.skip("Memory upgrade requires its dedicated isolated database")
    return MySQLStorageConfig(
        host=host,
        port=int(os.environ.get("ELYSIUM_TEST_MYSQL_PORT", "3306")),
        database=database,
        user=user,
        password=os.environ.get("ELYSIUM_TEST_MYSQL_PASSWORD", ""),
        ssl_mode=os.environ.get("ELYSIUM_TEST_MYSQL_SSL_MODE", "disabled"),  # type: ignore[arg-type]
        pool_size=2,
        max_overflow=0,
        application_query_timeout_seconds=300,
    )


def _generation(generation_id: str) -> BackendGeneration:
    now = datetime.now(UTC).isoformat()
    return BackendGeneration(
        generation_id=generation_id,
        backend=BackendKind.MYSQL,
        schema_version=1,
        source_snapshot_sha256="7" * 64,
        root_hashes={"life-memory-upgrade": "8" * 64},
        frontiers={"memory": 0},
        created_at=now,
        verified_at=now,
        status=GenerationStatus.VERIFIED,
    )


async def _prepare_v8_database(engine: object) -> None:
    async with engine.begin() as connection:  # type: ignore[union-attr]
        for name, _event, _table in baseline.MEMORY_IMMUTABILITY_TRIGGER_CONTRACT:
            if name.startswith("memory_workspace_projection_events_"):
                await connection.execute(text(f"DROP TRIGGER IF EXISTS `{name}`"))
        await connection.execute(
            text("DROP TABLE IF EXISTS memory_workspace_projection_heads")
        )
        await connection.execute(
            text("DROP TABLE IF EXISTS memory_workspace_projection_events")
        )
        await connection.execute(
            text("DELETE FROM life_memory_schema_migrations WHERE version = 9")
        )
        await connection.execute(
            text(
                "DELETE FROM life_memory_immutability_schema_migrations "
                "WHERE version = 2"
            )
        )
    await MySQLMigrationRunner(
        engine,  # type: ignore[arg-type]
        table_name="life_memory_schema_migrations",
        lock_name="elysium:life-memory-schema",
    ).apply(MEMORY_MIGRATIONS[:-1])
    await MySQLMigrationRunner(
        engine,  # type: ignore[arg-type]
        table_name="life_memory_immutability_schema_migrations",
        lock_name="elysium:life-memory-immutability",
    ).apply(MEMORY_IMMUTABILITY_MIGRATIONS[:1])
    await MySQLMigrationRunner(
        engine,  # type: ignore[arg-type]
        table_name="life_singleton_writer_schema_migrations",
        lock_name="elysium:life-singleton-writer-schema",
    ).apply((MYSQL_SINGLETON_WRITER_MIGRATION,))


@pytest.mark.timeout(300)
async def test_real_mysql_memory_v8_to_v9_upgrade_is_additive_and_guarded(
    tmp_path: Path,
) -> None:
    config = _mysql_config()
    engine = create_mysql_storage_engine(config)
    suffix = uuid4().hex
    registry_id = f"memory-upgrade-{suffix}"
    generation_id = f"memory-upgrade-generation-{suffix}"
    registry = MySQLAuthorityRegistry(engine, registry_id=registry_id)
    token = None
    try:
        await _prepare_v8_database(engine)
        generation = _generation(generation_id)
        await registry.register_generation(generation)
        token = await registry.activate_generation(
            generation_id,
            expected_epoch=0,
            owner_id=f"memory-upgrade-owner-{suffix}",
            lease_seconds=300,
            confirm_previous_writers_stopped=True,
        )
        args = argparse.Namespace(
            confirm_memory_upgrade=True,
            output=tmp_path / "evidence",
            registry_id=registry_id,
        )

        result = await baseline._upgrade_memory(
            args,
            engine,
            backend_identity=config.safe_identity,
        )

        assert result["schema_versions"] == list(range(1, 10))
        assert result["immutability_versions"] == [1, 2]
        assert result["verified_memory_trigger_count"] == 44
        before = json.loads((args.output / "memory-before.json").read_text())
        after = json.loads((args.output / "memory-after.json").read_text())
        assert before["existing_memory"] == after["existing_memory"]
        assert before["authority"] == after["authority"]
        assert before["workspace_projection"]["present_tables"] == []
        assert after["workspace_projection"]["row_count"] == 0

        event_sha256 = suffix * 2
        insert = text(
            """INSERT INTO memory_workspace_projection_events (
                event_sha256, event_kind, storage_generation_id,
                projection_generation_id, previous_projection_generation_id,
                owner_id, previous_owner_id, workspace_root_sha256,
                previous_workspace_root_sha256, source_root_sha256,
                eligible_inventory_sha256, revision, expected_revision,
                actor_id, audit_occurrence_id, reason_code, occurred_at,
                previous_event_sha256, payload_sha256
            ) VALUES (
                :event_sha256, 'inventory_committed', :generation_id,
                'projection-1', '', 'owner-1', '', :root, :root, :root, :root,
                1, 0, 'actor-1', :occurrence, 'integration_test',
                '2026-08-11T00:00:00+00:00', :root, :root
            )"""
        )
        async with engine.begin() as connection:
            await connection.execute(
                insert,
                {
                    "event_sha256": event_sha256,
                    "generation_id": generation_id,
                    "occurrence": f"upgrade-{suffix}",
                    "root": "0" * 64,
                },
            )
        with pytest.raises(DBAPIError, match="MemoryAuthorityRecordImmutable"):
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "UPDATE memory_workspace_projection_events "
                        "SET reason_code = 'forged' WHERE event_sha256 = :event_sha256"
                    ),
                    {"event_sha256": event_sha256},
                )
        with pytest.raises(DBAPIError, match="MemoryAuthorityRecordImmutable"):
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "DELETE FROM memory_workspace_projection_events "
                        "WHERE event_sha256 = :event_sha256"
                    ),
                    {"event_sha256": event_sha256},
                )
    finally:
        if token is not None:
            await registry.revoke(token)
        await engine.dispose()
