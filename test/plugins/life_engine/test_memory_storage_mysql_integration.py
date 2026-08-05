"""Opt-in real MySQL contract test for selectable Life Memory storage."""

from __future__ import annotations

import os
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from plugins.life_engine.memory.epistemic import MemoryClaim
from plugins.life_engine.memory.experience import ExperienceRecord
from plugins.life_engine.memory.living import new_artifact_version
from plugins.life_engine.storage.authority import MySQLAuthorityRegistry
from plugins.life_engine.storage.factory import (
    MySQLBackendSettings,
    StorageFactorySettings,
    open_storage_backend,
)
from plugins.life_engine.storage.memory import open_mysql_memory_storage
from plugins.life_engine.storage.models import (
    BackendGeneration,
    BackendKind,
    GenerationStatus,
)
from src.kernel.storage.engine import MySQLStorageConfig, create_mysql_storage_engine


def _mysql_config() -> MySQLStorageConfig:
    host = os.environ.get("ELYSIUM_TEST_MYSQL_HOST", "")
    database = os.environ.get("ELYSIUM_TEST_MYSQL_DATABASE", "")
    user = os.environ.get("ELYSIUM_TEST_MYSQL_USER", "")
    if not host or not database or not user:
        pytest.skip("isolated MySQL integration database is not configured")
    if os.environ.get("ELYSIUM_TEST_MYSQL_MEMORY_ISOLATED") != "1":
        pytest.skip("Memory MySQL contract requires an isolated database")
    return MySQLStorageConfig(
        host=host,
        port=int(os.environ.get("ELYSIUM_TEST_MYSQL_PORT", "3306")),
        database=database,
        user=user,
        password=os.environ.get("ELYSIUM_TEST_MYSQL_PASSWORD", ""),
        ssl_mode=os.environ.get("ELYSIUM_TEST_MYSQL_SSL_MODE", "disabled"),  # type: ignore[arg-type]
    )


def _generation() -> BackendGeneration:
    return BackendGeneration(
        generation_id="mysql-life-memory-contract-v8",
        backend=BackendKind.MYSQL,
        schema_version=8,
        source_snapshot_sha256="7" * 64,
        root_hashes={"life-memory": "8" * 64},
        frontiers={"experience": 0, "witness": 0, "living": 0},
        created_at="2026-08-04T00:00:00+00:00",
        verified_at="2026-08-04T00:01:00+00:00",
        status=GenerationStatus.VERIFIED,
    )


@pytest.mark.timeout(180)
async def test_mysql_memory_bundle_preserves_identity_and_cas_contracts() -> None:
    config = _mysql_config()
    engine = create_mysql_storage_engine(config)
    registry = MySQLAuthorityRegistry(
        engine,
        registry_id="life-memory-integration",
    )
    runtime = None
    token = None
    suffix = uuid4().hex
    event_id = f"memory-event-{suffix}"
    witness_id = f"memory-witness-{suffix}"
    instance_id = f"memory-instance-{suffix}"
    claim_id = f"memory-claim-{suffix}"
    logical_key = f"memory:contract:{suffix}"
    document_path = f"notes/mysql-contract-{suffix}.md"
    try:
        generation = _generation()
        await registry.register_generation(generation)
        health = await registry.health()
        token = await registry.activate_generation(
            generation.generation_id,
            expected_epoch=int(health.get("authority_epoch") or 0),
            owner_id="life-memory-integration-writer",
            lease_seconds=180,
            confirm_previous_writers_stopped=True,
        )
        runtime = await open_storage_backend(
            StorageFactorySettings(
                enabled=True,
                authoritative_backend=BackendKind.MYSQL,
                backend_generation=generation.generation_id,
                schema_version=8,
                registry_id="life-memory-integration",
                authority_provider="mysql",
                authority_epoch=token.authority_epoch,
                authority_owner_id=token.owner_id,
                fencing_token_env="TEST_MEMORY_MYSQL_FENCE",
                mysql=MySQLBackendSettings(
                    host=config.host,
                    port=config.port,
                    database=config.database,
                    user=config.user,
                    password_env="TEST_MEMORY_MYSQL_PASSWORD",
                    ssl_mode=config.ssl_mode,
                ),
            ),
            environment={
                "TEST_MEMORY_MYSQL_FENCE": token.fencing_token,
                "TEST_MEMORY_MYSQL_PASSWORD": config.password,
            },
        )
        stores = await open_mysql_memory_storage(runtime, initialize_schema=True)

        indexed = await stores.document_index.upsert_document(
            document_path,
            "MySQL 也必须留下可追溯痕迹",
            "mysql contract",
        )
        assert (
            await stores.document_index.upsert_document(
                document_path,
                "MySQL 也必须留下可追溯痕迹",
                "mysql contract",
            )
        ).job_id == indexed.job_id

        experience = ExperienceRecord(
            event_id=event_id,
            source_event_id=f"producer-{suffix}",
            sequence=1,
            occurred_at="2026-08-04T12:00:00+08:00",
            recorded_at="2026-08-04T12:00:01+08:00",
            source="integration",
            channel="test",
            event_type="memory.contract",
            content="一次不可变经历",
        )
        assert (await stores.experiences.append((experience,))).inserted_count == 1
        assert (await stores.experiences.append((experience,))).inserted_count == 0

        witness = await stores.witnesses.append(
            witness_id=witness_id,
            content="一次带来源链的见证",
            consciousness_instance_id=instance_id,
            perspective_subject_id="elysia",
            epistemic_kind="subjective_witness",
            source_kind="experience_window",
            stream_scope="",
            visibility="private",
            valid_from=experience.occurred_at,
            valid_to=experience.occurred_at,
            source_event_ids=(event_id,),
        )
        assert witness.source_event_ids == (event_id,)
        assert await stores.witnesses.mark_projection(
            witness_id,
            projection_path=f"diaries/mysql-contract-{suffix}.md",
            status="projected",
        )
        state = await stores.witnesses.compare_and_advance_state(
            instance_id,
            expected_sequence=0,
            expected_revision=0,
            next_sequence=1,
        )
        assert (state["last_sequence"], state["revision"]) == (1, 1)

        artifact = new_artifact_version(
            logical_key=logical_key,
            artifact_kind="self_narrative",
            content="MySQL 中的第一版解释",
        )
        await stores.living.append_artifact(artifact, expected_head_revision=0)
        assert (await stores.living.get_artifact_head(logical_key)).revision == 1  # type: ignore[union-attr]
        next_artifact = new_artifact_version(
            logical_key=logical_key,
            artifact_kind="self_narrative",
            content="MySQL ä¸­çš„ç¬¬äºŒç‰ˆè§£é‡Š",
            parent_artifact_ids=(artifact.artifact_id,),
        )
        await stores.living.append_artifact(next_artifact, expected_head_revision=1)
        assert (await stores.living.get_artifact_head(logical_key)).revision == 2  # type: ignore[union-attr]

        claim = MemoryClaim(
            claim_id=claim_id,
            subject_key=f"contract:{suffix}",
            content="检索排名不是事实真值",
            claim_kind="contract",
            source="integration",
            authority="test",
            valid_from="",
            valid_to="",
            recorded_at="2026-08-04T12:00:02+08:00",
        )
        assert await stores.epistemic.append_claim(claim) == claim

        with pytest.raises(DBAPIError, match="MemoryAuthorityRecordImmutable"):
            async with runtime.unit_of_work() as uow:
                await uow.session.execute(
                    text(
                        "UPDATE memory_experiences SET content = 'forged' "
                        "WHERE event_id = :event_id"
                    ),
                    {"event_id": event_id},
                )
        with pytest.raises(DBAPIError, match="MemoryWitnessAuthorityImmutable"):
            async with runtime.unit_of_work() as uow:
                await uow.session.execute(
                    text(
                        "UPDATE memory_witnesses SET content = 'forged' "
                        "WHERE witness_id = :witness_id"
                    ),
                    {"witness_id": witness_id},
                )
        with pytest.raises(DBAPIError, match="MemoryAuthorityRecordImmutable"):
            async with runtime.unit_of_work() as uow:
                await uow.session.execute(
                    text("DELETE FROM memory_claims WHERE claim_id = :claim_id"),
                    {"claim_id": claim_id},
                )
    finally:
        if runtime is not None:
            await runtime.close()
        if token is not None:
            await registry.revoke(token)
        await engine.dispose()
