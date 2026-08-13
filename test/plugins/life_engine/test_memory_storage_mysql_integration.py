"""Opt-in real MySQL contract test for selectable Life Memory storage."""

from __future__ import annotations

import asyncio
import os
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from plugins.life_engine.memory.epistemic import MemoryClaim
from plugins.life_engine.memory.experience import (
    ExperienceRecord,
    WitnessIdentityConflict,
)
from plugins.life_engine.memory.living import new_artifact_version
from plugins.life_engine.memory.witness_pipeline import (
    WitnessDecision,
    WitnessWindow,
    witness_window_source_digest,
)
from plugins.life_engine.storage.authority import MySQLAuthorityRegistry
from plugins.life_engine.storage.factory import (
    MySQLBackendSettings,
    StorageFactorySettings,
    open_storage_backend,
)
from plugins.life_engine.storage.memory import open_mysql_memory_storage
from plugins.life_engine.storage.memory.contracts import (
    WitnessReconciliationStateCorrupt,
)
from plugins.life_engine.storage.models import (
    BackendGeneration,
    BackendKind,
    GenerationStatus,
)
from src.kernel.storage import CursorConflict
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
        runtime = await open_storage_backend(
            StorageFactorySettings(
                enabled=True,
                authoritative_backend=BackendKind.MYSQL,
                backend_generation=generation.generation_id,
                schema_version=8,
                registry_id="life-memory-integration",
                authority_provider="mysql",
                authority_owner_id="life-memory-integration-writer",
                authority_lease_seconds=180,
                mysql=MySQLBackendSettings(
                    host=config.host,
                    port=config.port,
                    database=config.database,
                    user=config.user,
                    password_env="TEST_MEMORY_MYSQL_PASSWORD",
                    ssl_mode=config.ssl_mode,
                ),
            ),
            environment={"TEST_MEMORY_MYSQL_PASSWORD": config.password},
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
        document_metadata = await stores.document_index.get_document_metadata(
            document_path
        )
        assert document_metadata is not None
        assert document_metadata.node_id == indexed.node_id
        assert document_metadata.content_hash == indexed.content_hash
        assert document_metadata.title == "mysql contract"
        assert document_metadata.is_deleted is False

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
        peers = tuple(
            ExperienceRecord(
                event_id=f"{event_id}-peer-{index}",
                source_event_id=f"producer-{suffix}-peer-{index}",
                sequence=1,
                occurred_at=experience.occurred_at,
                recorded_at=experience.recorded_at,
                source="integration",
                channel="test",
                event_type="memory.contract",
                content=f"同一位置的不可变经历 {index}",
            )
            for index in range(1004)
        )
        assert (await stores.experiences.append(peers)).inserted_count == 1004
        occurrence_page = await stores.experiences.list_occurrence_page(
            position_after=0,
            limit=137,
        )
        occurrence_refs = list(occurrence_page.items)
        occurrence_ids = [item.occurrence_id for item in occurrence_refs]
        occurrence_frontier = occurrence_page.frontier
        while occurrence_page.has_more:
            assert occurrence_page.next_cursor is not None
            occurrence_page = await stores.experiences.list_occurrence_page(
                position_after=0,
                after=occurrence_page.next_cursor,
                through=occurrence_frontier,
                limit=137,
            )
            occurrence_refs.extend(occurrence_page.items)
            occurrence_ids.extend(item.occurrence_id for item in occurrence_page.items)
        assert set(occurrence_ids) == {experience.event_id, *(item.event_id for item in peers)}

        delivery_jobs = []
        for index, occurrence in enumerate(occurrence_refs[:6]):
            created_at = f"2000-01-01T00:00:0{index}+00:00"
            window = WitnessWindow(
                window_id=f"mysql-delivery-window-{suffix}-{index}",
                consciousness_instance_id=instance_id,
                start_position=occurrence.ingest_position,
                end_position=occurrence.ingest_position,
                occurrences=(occurrence,),
                created_at=created_at,
                planner_version="mysql-contract",
                source_digest=witness_window_source_digest((occurrence,)),
            )
            await stores.witnesses.append_window(window)
            decision = WitnessDecision(
                decision_id=f"mysql-delivery-decision-{suffix}-{index}",
                window_id=window.window_id,
                consciousness_instance_id=instance_id,
                decision_kind="witness",
                witness_id=f"mysql-delivery-witness-{suffix}-{index}",
                decided_at=created_at,
                model_task_name="mysql-contract",
                model_request_id=f"mysql-contract-{index}",
                response_sha256="a" * 64,
            )
            await stores.witnesses.append_decision(
                decision,
                delivery_payloads={
                    "projection": {
                        "projection_path": f"diaries/{suffix}-{index}.md",
                        "witness_id": decision.witness_id,
                    }
                },
            )
            job = next(
                item
                for item in await stores.witnesses.list_delivery_jobs(
                    delivery_kind="projection",
                    statuses=("pending",),
                    limit=1000,
                )
                if item.decision_id == decision.decision_id
            )
            if index < 5:
                job = await stores.witnesses.mark_delivery_job(
                    job.job_id,
                    expected_revision=job.revision,
                    status="failed",
                    error_type="FutureRetry",
                    available_at="2999-01-01T00:00:00+00:00",
                )
            delivery_jobs.append(job)
        delivery_page = await stores.witnesses.list_delivery_jobs_page(
            delivery_kind="projection",
            statuses=("pending", "failed", "processing"),
            limit=4,
        )
        delivery_frontier = delivery_page.frontier
        seen_delivery_ids = [job.job_id for job in delivery_page.items]
        while delivery_page.has_more:
            assert delivery_page.next_cursor is not None
            delivery_page = await stores.witnesses.list_delivery_jobs_page(
                delivery_kind="projection",
                statuses=("pending", "failed", "processing"),
                after=delivery_page.next_cursor,
                through=delivery_frontier,
                limit=4,
            )
            seen_delivery_ids.extend(job.job_id for job in delivery_page.items)
        assert {job.job_id for job in delivery_jobs} <= set(seen_delivery_ids)
        assert delivery_jobs[-1].status == "pending"

        witness_content = "\n  一次带来源链的见证\t\n"
        witness_recorded_at = "2026-08-04T12:00:01+08:00"
        witness_projection_path = f"diaries/mysql-contract-{suffix}.md"
        witness = await stores.witnesses.append(
            witness_id=witness_id,
            content=witness_content,
            consciousness_instance_id=instance_id,
            perspective_subject_id="elysia",
            epistemic_kind="subjective_witness",
            source_kind="experience_window",
            stream_scope="",
            visibility="private",
            valid_from=experience.occurred_at,
            valid_to=experience.occurred_at,
            source_event_ids=(event_id,),
            recorded_at=witness_recorded_at,
            projection_path=witness_projection_path,
        )
        assert witness.content.encode("utf-8") == witness_content.encode("utf-8")
        assert len(witness.payload_sha256) == 64
        assert witness.source_event_ids == (event_id,)
        replay = await stores.witnesses.append(
            witness_id=witness_id,
            content=witness_content,
            consciousness_instance_id=instance_id,
            perspective_subject_id="elysia",
            epistemic_kind="subjective_witness",
            source_kind="experience_window",
            stream_scope="",
            visibility="private",
            valid_from=experience.occurred_at,
            valid_to=experience.occurred_at,
            source_event_ids=(event_id,),
            recorded_at=witness_recorded_at,
            projection_path=witness_projection_path,
        )
        assert replay.content.encode("utf-8") == witness_content.encode("utf-8")
        with pytest.raises(WitnessIdentityConflict):
            await stores.witnesses.append(
                witness_id=witness_id,
                content=witness_content.strip(),
                consciousness_instance_id=instance_id,
                perspective_subject_id="elysia",
                epistemic_kind="subjective_witness",
                source_kind="experience_window",
                stream_scope="",
                visibility="private",
                valid_from=experience.occurred_at,
                valid_to=experience.occurred_at,
                    source_event_ids=(event_id,),
                    recorded_at=witness_recorded_at,
                    projection_path=witness_projection_path,
            )
        legacy_content = "\n  旧见证也必须逐字节保留。\t\n"
        legacy = await stores.witnesses.migrate_legacy(
            migration_key=f"legacy-contract:{suffix}",
            source_path=f"legacy/{suffix}.md",
            source_hash="9" * 64,
            content=legacy_content,
            valid_from=experience.occurred_at,
            recorded_at="2026-08-04T12:00:03+08:00",
        )
        assert legacy is not None
        assert legacy.content.encode("utf-8") == legacy_content.encode("utf-8")
        pending_page = await stores.witnesses.list_pending_page(limit=1)
        assert pending_page.items[0].witness_id == witness_id
        scan_state = await stores.witnesses.get_reconciliation_state(
            f"mysql-contract:{suffix}"
        )
        scan_state = await stores.witnesses.compare_and_advance_reconciliation_state(
            f"mysql-contract:{suffix}",
            expected_revision=scan_state.revision,
            next_cursor=pending_page.next_cursor,
            frontier=pending_page.frontier,
            completed=False,
        )
        assert len(scan_state.state_sha256) == 64
        restarted_stores = await open_mysql_memory_storage(
            runtime,
            initialize_schema=False,
        )
        assert (
            await restarted_stores.witnesses.get_reconciliation_state(
                f"mysql-contract:{suffix}"
            )
            == scan_state
        )
        concurrent = await asyncio.gather(
            *(
                restarted_stores.witnesses.compare_and_advance_reconciliation_state(
                    f"mysql-contract:{suffix}",
                    expected_revision=scan_state.revision,
                    next_cursor=None,
                    frontier=None,
                    completed=True,
                )
                for _ in range(2)
            ),
            return_exceptions=True,
        )
        assert sum(isinstance(item, CursorConflict) for item in concurrent) == 1
        completed_state = next(
            item for item in concurrent if not isinstance(item, BaseException)
        )
        assert completed_state.last_completed_at

        tamper_name = f"mysql-tamper:{suffix}"
        tamper_state = await stores.witnesses.get_reconciliation_state(tamper_name)
        await stores.witnesses.compare_and_advance_reconciliation_state(
            tamper_name,
            expected_revision=tamper_state.revision,
            next_cursor=pending_page.next_cursor,
            frontier=pending_page.frontier,
            completed=False,
        )
        async with runtime.unit_of_work() as uow:
            await uow.session.execute(
                text(
                    "UPDATE memory_witness_reconciliation_state "
                    "SET cursor_identity = '0' WHERE scan_name = :scan_name"
                ),
                {"scan_name": tamper_name},
            )
        with pytest.raises(
            WitnessReconciliationStateCorrupt,
            match="ChecksumMismatch",
        ):
            await stores.witnesses.get_reconciliation_state(tamper_name)
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
            await runtime.revoke_authority()
            await runtime.close()
        await engine.dispose()
