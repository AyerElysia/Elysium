"""Local contract for the subject-managed Opportunity authority."""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from plugins.life_engine.service.event_bus import LifeEvent
from plugins.life_engine.storage.authority import (
    FileAuthorityRegistry,
    StaleAuthorityToken,
)
from plugins.life_engine.storage.contracts import StorageBackendRuntime
from plugins.life_engine.storage.domain_factory import open_presence_world_stores
from plugins.life_engine.storage.event_factory import open_life_event_store
from plugins.life_engine.storage.factory import (
    LocalBackendSettings,
    StorageFactorySettings,
    open_storage_backend,
)
from plugins.life_engine.storage.models import (
    BackendGeneration,
    BackendKind,
    GenerationStatus,
)
from plugins.life_engine.storage.opportunity_contracts import (
    OPPORTUNITY_SCHEDULER_CLAIM_NAMESPACE,
    OPPORTUNITY_SCHEDULER_CLAIM_STATE_KEY,
    OpportunityAction,
    OpportunityActorInactive,
    OpportunityConflict,
    OpportunityDeliveryReceipt,
    OpportunityDeliveryRejected,
    OpportunityHistoryFamily,
    OpportunityRegistrationCommand,
    OpportunitySchedule,
    OpportunitySchedulerClaimRequired,
    OpportunityStatus,
    ProviderAction,
    ProviderBindingCommand,
    ProviderStatus,
    PublicationStatus,
    WorkflowVersion,
    WorkflowVersionCommand,
)
from plugins.life_engine.storage.opportunity_factory import open_opportunity_stores
from plugins.life_engine.storage.opportunity_schema import (
    OpportunitySchemaNotReady,
    mark_opportunity_runtime_managed,
    read_opportunity_runtime_marker,
)
from src.kernel.storage import canonical_json

_ACTOR = "consciousness:opportunity-contract:1"
_OCCURRED_AT = "2026-09-05T00:00:00+00:00"


def _sha(value: str | bytes) -> str:
    raw = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(raw).hexdigest()


async def _append_publication_event(
    runtime: StorageBackendRuntime,
    publication,
    occurrence,
) -> str:
    store = await open_life_event_store(runtime, initialize_schema=True)
    payload = asdict(occurrence)
    payload["schema_version"] = 1
    payload["meaning"] = "availability_only"
    content = canonical_json(payload)
    await store.append(
        LifeEvent(
            event_id=publication.life_event_occurrence_id,
            sequence=0,
            occurrence_id=publication.life_event_occurrence_id,
            timestamp=occurrence.available_at,
            source="opportunity_runtime",
            channel="life",
            event_type="opportunity.available",
            content=content,
            source_instance_id="infrastructure:opportunity-scheduler",
            causation_id=occurrence.occurrence_id,
            metadata={"opportunity_occurrence_id": occurrence.occurrence_id},
        )
    )
    return _sha(content)


def _generation() -> BackendGeneration:
    return BackendGeneration(
        generation_id="opportunity-local-v1",
        backend=BackendKind.LOCAL,
        schema_version=1,
        source_snapshot_sha256="1" * 64,
        root_hashes={"opportunity": "2" * 64},
        frontiers={"opportunity": 0},
        created_at="2026-09-05T00:00:00+00:00",
        verified_at="2026-09-05T00:01:00+00:00",
        status=GenerationStatus.VERIFIED,
    )


@asynccontextmanager
async def _local_runtime(tmp_path: Path) -> AsyncIterator[StorageBackendRuntime]:
    authority_path = tmp_path / "authority.json"
    registry = FileAuthorityRegistry(authority_path)
    generation = _generation()
    await registry.register_generation(generation)
    token = await registry.activate_generation(
        generation.generation_id,
        expected_epoch=0,
        owner_id="opportunity-contract",
        lease_seconds=300,
        confirm_previous_writers_stopped=True,
    )
    runtime = await open_storage_backend(
        StorageFactorySettings(
            enabled=True,
            authoritative_backend=BackendKind.LOCAL,
            backend_generation=generation.generation_id,
            schema_version=1,
            authority_epoch=token.authority_epoch,
            authority_owner_id=token.owner_id,
            fencing_token_env="TEST_OPPORTUNITY_FENCE",
            local=LocalBackendSettings(
                database_path=tmp_path / "life.sqlite3",
                authority_state_path=authority_path,
            ),
        ),
        environment={"TEST_OPPORTUNITY_FENCE": token.fencing_token},
    )
    try:
        yield runtime
    finally:
        await runtime.close()
        try:
            await registry.revoke(token)
        except StaleAuthorityToken:
            pass


async def _register_actor(runtime: StorageBackendRuntime) -> None:
    stores = await open_presence_world_stores(runtime, initialize_schema=True)
    await stores.presence.commit(
        {
            "instance_id": _ACTOR,
            "kind": "opportunity-contract",
            "display_name": "Opportunity contract actor",
            "status": "active",
            "created_at": _OCCURRED_AT,
            "last_active_at": _OCCURRED_AT,
            "suspended_at": "",
            "stream_ids": ["stream:opportunity-contract"],
            "perception_filter": {},
            "metadata": {},
            "session_id": "session:opportunity-contract",
            "process_epoch": "process:opportunity-contract",
            "lease_expires_at": "",
            "lease_duration_seconds": None,
            "revision": 0,
        },
        expected_revision=None,
        event_type="consciousness.instance_registered",
        event_payload={"occurred_at": _OCCURRED_AT},
    )


async def _open_ready(runtime: StorageBackendRuntime):
    unclaimed = await open_opportunity_stores(runtime, initialize_schema=True)
    claim = await runtime.acquire_singleton_writer(
        namespace=OPPORTUNITY_SCHEDULER_CLAIM_NAMESPACE,
        state_key=OPPORTUNITY_SCHEDULER_CLAIM_STATE_KEY,
        owner_instance_id="opportunity-scheduler:contract",
        lease_seconds=120,
    )
    claimed = await open_opportunity_stores(runtime, writer_claim=claim)
    assert claimed.scheduler is not None
    return unclaimed, claimed, claim


def _workflow_command(
    provider_id: str,
    identity: str,
    *,
    expected_revision: int = 0,
    actor: str = _ACTOR,
    content: str | None = None,
    reason: str = "我明确采用这个流程版本。",
) -> WorkflowVersionCommand:
    raw = (
        content or f"version: {expected_revision + 1}\nprovider: {provider_id}\n🌸"
    ).encode("utf-8")
    return WorkflowVersionCommand(
        occurrence_id=f"opportunity:workflow-decision:{identity}",
        workflow_id=f"workflow:{provider_id}",
        provider_id=provider_id,
        actor_consciousness_instance_id=actor,
        source_instance_id=actor,
        source_occurrence_ids=(f"life:event:{identity}",),
        causation_occurrence_id=f"life:cause:{identity}",
        expected_revision=expected_revision,
        schema_version=1,
        content_bytes=raw,
        content_sha256=_sha(raw),
        reason=reason,
        occurred_at=_OCCURRED_AT,
    )


def _provider_command(
    provider_id: str,
    workflow: WorkflowVersion,
    identity: str,
    *,
    action: ProviderAction = ProviderAction.INSTALL,
    expected_revision: int = 0,
    actor: str = _ACTOR,
) -> ProviderBindingCommand:
    return ProviderBindingCommand(
        occurrence_id=f"opportunity:provider-decision:{identity}",
        provider_id=provider_id,
        action=action,
        actor_consciousness_instance_id=actor,
        source_instance_id=actor,
        source_occurrence_ids=(f"life:event:{identity}",),
        causation_occurrence_id=f"life:cause:{identity}",
        expected_revision=expected_revision,
        descriptor_version="1",
        descriptor_sha256=_sha(f"descriptor:{provider_id}:1"),
        workflow_id=workflow.workflow_id,
        workflow_revision=workflow.revision,
        workflow_sha256=workflow.content_sha256,
        reason=f"我明确执行 provider {action.value}。",
        occurred_at=_OCCURRED_AT,
    )


def _registration_command(
    opportunity_id: str,
    provider_id: str,
    workflow: WorkflowVersion,
    identity: str,
    *,
    action: OpportunityAction = OpportunityAction.OPEN,
    expected_revision: int = 0,
    schedule: OpportunitySchedule = OpportunitySchedule.MANUAL,
    first_due_at: str = "",
    interval_seconds: int = 0,
    referent_id: str = "learning:reflection",
    referent_revision: int = 1,
    reason: str = "我明确打开这次机会。",
) -> OpportunityRegistrationCommand:
    return OpportunityRegistrationCommand(
        occurrence_id=f"opportunity:registration-decision:{identity}",
        opportunity_id=opportunity_id,
        provider_id=provider_id,
        action=action,
        actor_consciousness_instance_id=_ACTOR,
        source_instance_id=_ACTOR,
        source_occurrence_ids=(f"life:event:{identity}",),
        causation_occurrence_id=f"life:cause:{identity}",
        expected_revision=expected_revision,
        referent_kind="skill",
        referent_id=referent_id,
        referent_revision=referent_revision,
        referent_sha256=_sha(f"{referent_id}:{referent_revision}"),
        workflow_id=workflow.workflow_id,
        workflow_revision=workflow.revision,
        workflow_sha256=workflow.content_sha256,
        schedule=schedule,
        first_due_at=first_due_at,
        interval_seconds=interval_seconds,
        reason=reason,
        occurred_at=_OCCURRED_AT,
    )


async def _install_provider(authority, provider_id: str) -> WorkflowVersion:
    workflow = await authority.append_workflow(
        _workflow_command(provider_id, f"{provider_id}:workflow")
    )
    await authority.manage_provider(
        _provider_command(provider_id, workflow, f"{provider_id}:install")
    )
    return workflow


async def test_local_readiness_requires_exact_immutability_trigger_contract(
    tmp_path: Path,
) -> None:
    trigger_name = "opportunity_workflow_versions_immutable_update_v1"
    async with _local_runtime(tmp_path) as runtime:
        await open_opportunity_stores(runtime, initialize_schema=True)

        async with runtime.unit_of_work() as uow:
            await uow.session.execute(text(f"DROP TRIGGER {trigger_name}"))
        with pytest.raises(
            OpportunitySchemaNotReady,
            match=f"missing_trigger:{trigger_name}",
        ):
            await open_opportunity_stores(runtime)
        await open_opportunity_stores(runtime, initialize_schema=True)

        async with runtime.unit_of_work() as uow:
            await uow.session.execute(text(f"DROP TRIGGER {trigger_name}"))
            await uow.session.execute(
                text(
                    f"""CREATE TRIGGER {trigger_name}
                    BEFORE UPDATE ON opportunity_provider_events BEGIN
                        SELECT RAISE(ABORT, 'OpportunityImmutable');
                    END"""
                )
            )
        with pytest.raises(
            OpportunitySchemaNotReady,
            match=f"trigger_table_mismatch:{trigger_name}",
        ):
            await open_opportunity_stores(runtime)

        async with runtime.unit_of_work() as uow:
            await uow.session.execute(text(f"DROP TRIGGER {trigger_name}"))
            await uow.session.execute(
                text(
                    f"""CREATE TRIGGER {trigger_name}
                    BEFORE UPDATE ON opportunity_workflow_versions BEGIN
                        SELECT RAISE(ABORT, 'WrongImmutabilityPolicy');
                    END"""
                )
            )
        with pytest.raises(
            OpportunitySchemaNotReady,
            match=f"trigger_definition_mismatch:{trigger_name}",
        ):
            await open_opportunity_stores(runtime)

        async with runtime.unit_of_work() as uow:
            await uow.session.execute(text(f"DROP TRIGGER {trigger_name}"))
        await open_opportunity_stores(runtime, initialize_schema=True)
        reopened = await open_opportunity_stores(runtime)
        assert reopened.authority is not None


async def test_schema_is_explicit_and_authority_is_actor_gated_and_immutable(
    tmp_path: Path,
) -> None:
    async with _local_runtime(tmp_path) as runtime:
        with pytest.raises(OpportunitySchemaNotReady):
            await open_opportunity_stores(runtime)
        assert await read_opportunity_runtime_marker(runtime) is None

        unclaimed, claimed, _ = await _open_ready(runtime)
        assert await read_opportunity_runtime_marker(runtime) is None
        assert unclaimed.scheduler is None
        assert claimed.scheduler is not None
        with pytest.raises(OpportunitySchedulerClaimRequired):
            await unclaimed.authority.materialize_due()  # type: ignore[attr-defined]

        await open_presence_world_stores(runtime, initialize_schema=True)
        missing_actor = _workflow_command(
            "provider:actor-gate",
            "missing-actor",
            actor="consciousness:missing",
        )
        with pytest.raises(OpportunityActorInactive):
            await unclaimed.authority.append_workflow(missing_actor)

        await _register_actor(runtime)
        workflow_command = _workflow_command(
            "provider:learning",
            "learning-v1",
            content="步骤一：观察。\n步骤二：反思。🌸",
        )
        workflow = await unclaimed.authority.append_workflow(workflow_command)
        replay = await unclaimed.authority.append_workflow(workflow_command)
        assert replay == replace(workflow, idempotent_replay=True)
        changed = "同一个 occurrence 不能换内容".encode()
        with pytest.raises(OpportunityConflict):
            await unclaimed.authority.append_workflow(
                replace(
                    workflow_command,
                    content_bytes=changed,
                    content_sha256=_sha(changed),
                )
            )

        install_command = _provider_command(
            "provider:learning",
            workflow,
            "learning-install",
        )
        installed = await unclaimed.authority.manage_provider(install_command)
        assert installed.status == ProviderStatus.ENABLED
        assert await unclaimed.authority.manage_provider(install_command) == replace(
            installed,
            idempotent_replay=True,
        )
        with pytest.raises(OpportunityConflict) as conflict:
            await unclaimed.authority.manage_provider(
                _provider_command(
                    "provider:learning",
                    workflow,
                    "learning-stale",
                    action=ProviderAction.PAUSE,
                    expected_revision=9,
                )
            )
        assert conflict.value.actual_revision == 1

        registration_command = _registration_command(
            "opportunity:learning",
            "provider:learning",
            workflow,
            "learning-open",
        )
        opened = await unclaimed.authority.decide_opportunity(registration_command)
        assert opened.status == OpportunityStatus.OPEN
        assert await unclaimed.authority.decide_opportunity(
            registration_command
        ) == replace(opened, idempotent_replay=True)

        with pytest.raises(DBAPIError, match="OpportunityImmutable"):
            async with runtime.unit_of_work() as uow:
                await uow.session.execute(
                    text(
                        """UPDATE opportunity_workflow_versions
                        SET reason='tampered' WHERE occurrence_id=:occurrence_id"""
                    ),
                    {"occurrence_id": workflow.occurrence_id},
                )

        health = await unclaimed.authority.health_snapshot()
        assert health["status"] == "healthy"
        assert health["workflow_version_count"] == 1
        assert "步骤一" not in str(health)
        assert "reason" not in str(health).lower()

        marker = await mark_opportunity_runtime_managed(
            runtime,
            migration_occurrence_id="opportunity:migration:contract-v1",
        )
        assert marker.schema_version == 2
        assert await read_opportunity_runtime_marker(runtime) == marker
        assert (
            await mark_opportunity_runtime_managed(
                runtime,
                migration_occurrence_id=marker.migration_occurrence_id,
            )
            == marker
        )
        with pytest.raises(OpportunityConflict, match="runtime_marker"):
            await mark_opportunity_runtime_managed(
                runtime,
                migration_occurrence_id="opportunity:migration:different",
            )
        with pytest.raises(DBAPIError, match="OpportunityImmutable"):
            async with runtime.unit_of_work() as uow:
                await uow.session.execute(
                    text(
                        """UPDATE opportunity_runtime_meta
                        SET schema_version=99 WHERE marker_key=:marker_key"""
                    ),
                    {"marker_key": marker.marker_key},
                )


async def test_provider_opportunity_and_history_pages_are_stable_and_complete(
    tmp_path: Path,
) -> None:
    async with _local_runtime(tmp_path) as runtime:
        await _register_actor(runtime)
        unclaimed, _, _ = await _open_ready(runtime)
        authority = unclaimed.authority

        workflows: dict[str, WorkflowVersion] = {}
        for index in range(3):
            provider_id = f"provider:{index}"
            workflows[provider_id] = await _install_provider(authority, provider_id)

        provider_first = await authority.page_providers(limit=2)
        assert [item.provider_id for item in provider_first.items] == [
            "provider:0",
            "provider:1",
        ]
        assert provider_first.continuation
        await _install_provider(authority, "provider:3")
        provider_second = await authority.page_providers(
            limit=2,
            continuation=provider_first.continuation,
        )
        assert [item.provider_id for item in provider_second.items] == ["provider:2"]
        assert provider_second.source_frontier == provider_first.source_frontier
        assert not provider_second.continuation
        assert len((await authority.page_providers(limit=10)).items) == 4

        workflow = workflows["provider:0"]
        for index in range(3):
            await authority.decide_opportunity(
                _registration_command(
                    f"opportunity:{index}",
                    "provider:0",
                    workflow,
                    f"opportunity-{index}",
                )
            )
        opportunity_first = await authority.page_opportunities(limit=2)
        await authority.decide_opportunity(
            _registration_command(
                "opportunity:3",
                "provider:0",
                workflow,
                "opportunity-3",
            )
        )
        opportunity_second = await authority.page_opportunities(
            limit=2,
            continuation=opportunity_first.continuation,
        )
        assert [item.opportunity_id for item in opportunity_first.items] == [
            "opportunity:0",
            "opportunity:1",
        ]
        assert [item.opportunity_id for item in opportunity_second.items] == [
            "opportunity:2"
        ]
        assert opportunity_second.source_frontier == opportunity_first.source_frontier

        await authority.manage_provider(
            _provider_command(
                "provider:0",
                workflow,
                "provider-0-pause",
                action=ProviderAction.PAUSE,
                expected_revision=1,
            )
        )
        resume_reason = "她自己决定恢复这个 provider；这是历史理由🌸。"
        resume = _provider_command(
            "provider:0",
            workflow,
            "provider-0-resume",
            action=ProviderAction.RESUME,
            expected_revision=2,
        )
        resume = replace(resume, reason=resume_reason)
        await authority.manage_provider(resume)

        history_ids: list[str] = []
        continuation = ""
        while True:
            page = await authority.page_history(
                OpportunityHistoryFamily.PROVIDER,
                aggregate_id="provider:0",
                limit=1,
                continuation=continuation,
            )
            history_ids.extend(item.occurrence_id for item in page.items)
            continuation = page.continuation
            if not continuation:
                break
        assert history_ids == [
            "opportunity:provider-decision:provider:0:install",
            "opportunity:provider-decision:provider-0-pause",
            "opportunity:provider-decision:provider-0-resume",
        ]
        chunk = await authority.read_history_reason_chunk(
            OpportunityHistoryFamily.PROVIDER,
            resume.occurrence_id,
            offset_bytes=0,
            max_bytes=19,
        )
        assert resume_reason.startswith(chunk.content)
        assert chunk.total_bytes == len(resume_reason.encode())
        with pytest.raises(ValueError, match="splits a UTF-8"):
            await authority.read_history_reason_chunk(
                OpportunityHistoryFamily.PROVIDER,
                resume.occurrence_id,
                offset_bytes=1,
                max_bytes=20,
            )

        workflow_chunk = await authority.read_workflow_chunk(
            workflow.workflow_id,
            workflow.revision,
            offset_bytes=0,
            max_bytes=13,
        )
        assert workflow.content_bytes.decode().startswith(workflow_chunk.content)
        assert workflow_chunk.total_bytes == len(workflow.content_bytes)


async def test_subject_can_bind_an_explicit_empty_workflow_version(
    tmp_path: Path,
) -> None:
    async with _local_runtime(tmp_path) as runtime:
        await _register_actor(runtime)
        unclaimed, _, _ = await _open_ready(runtime)
        authority = unclaimed.authority
        first = await _install_provider(authority, "provider:empty-workflow")
        empty = b""
        empty_version = await authority.append_workflow(
            replace(
                _workflow_command(
                    "provider:empty-workflow",
                    "explicit-empty",
                    expected_revision=first.revision,
                ),
                content_bytes=empty,
                content_sha256=_sha(empty),
            )
        )
        assert empty_version.revision == first.revision + 1
        assert empty_version.content_bytes == b""
        assert empty_version.content_sha256 == _sha(b"")

        await authority.manage_provider(
            _provider_command(
                "provider:empty-workflow",
                empty_version,
                "bind-explicit-empty",
                action=ProviderAction.BIND_WORKFLOW,
                expected_revision=1,
            )
        )
        binding = await authority.get_provider("provider:empty-workflow")
        assert binding is not None
        assert binding.workflow_revision == empty_version.revision
        assert binding.workflow_sha256 == _sha(b"")
        chunk = await authority.read_workflow_chunk(
            empty_version.workflow_id,
            empty_version.revision,
            offset_bytes=0,
            max_bytes=1,
        )
        assert chunk.content == ""
        assert chunk.total_bytes == 0
        assert chunk.next_offset_bytes == 0
        assert chunk.complete is True
        assert chunk.content_sha256 == _sha(b"")


async def test_due_publication_exact_delivery_and_restart_reconcile_are_separate(
    tmp_path: Path,
) -> None:
    async with _local_runtime(tmp_path) as runtime:
        await _register_actor(runtime)
        unclaimed, claimed, claim = await _open_ready(runtime)
        authority = unclaimed.authority
        scheduler = claimed.scheduler
        assert scheduler is not None
        workflow = await _install_provider(authority, "provider:heartbeat")
        first_due = (datetime.now(UTC) - timedelta(seconds=2)).isoformat()
        await authority.decide_opportunity(
            _registration_command(
                "opportunity:heartbeat",
                "provider:heartbeat",
                workflow,
                "heartbeat-open",
                schedule=OpportunitySchedule.INTERVAL,
                first_due_at=first_due,
                interval_seconds=3600,
                referent_id="heartbeat:active",
                referent_revision=7,
            )
        )

        materialized = await scheduler.materialize_due()
        assert len(materialized) == 1
        occurrence = materialized[0]
        assert occurrence.referent_id == "heartbeat:active"
        assert occurrence.referent_revision == 7
        assert occurrence.referent_sha256 == _sha("heartbeat:active:7")
        assert await scheduler.get_occurrence(occurrence.occurrence_id) == occurrence
        assert await scheduler.materialize_due() == ()

        publication = (await scheduler.pending_publications())[0]
        event_digest = await _append_publication_event(
            runtime,
            publication,
            occurrence,
        )
        published = await scheduler.mark_published(
            publication.outbox_id,
            expected_revision=publication.revision,
            life_event_sha256=event_digest,
        )
        assert published.status == PublicationStatus.PUBLISHED
        assert (
            await unclaimed.delivery.get_publication(published.life_event_occurrence_id)
            == published
        )
        assert await unclaimed.delivery.awaiting_delivery() == (published,)

        reopened = await open_opportunity_stores(runtime, writer_claim=claim)
        assert reopened.scheduler is not None
        assert await reopened.delivery.awaiting_delivery() == (published,)
        assert await reopened.scheduler.materialize_due() == ()

        context = b"<opportunity_available id='heartbeat'/>"
        context_sha = _sha(context)
        invalid = OpportunityDeliveryReceipt(
            receipt_id="opportunity:receipt:invalid",
            occurrence_id=occurrence.occurrence_id,
            life_event_occurrence_id=published.life_event_occurrence_id,
            consumer_consciousness_instance_id="consciousness:heartbeat:1",
            context_delivery_id="context:heartbeat:1",
            final_request_id="request:heartbeat:1",
            final_attempt_id="attempt:heartbeat:1",
            exact_present=True,
            expected_bytes=len(context),
            effective_bytes=len(context) - 1,
            expected_sha256=context_sha,
            effective_sha256=_sha(context[:-1]),
            perceived_at=datetime.now(UTC).isoformat(),
        )
        with pytest.raises(OpportunityDeliveryRejected):
            await unclaimed.delivery.commit_exact(invalid)

        receipt = replace(
            invalid,
            receipt_id="opportunity:receipt:exact",
            effective_bytes=len(context),
            effective_sha256=context_sha,
        )
        committed = await unclaimed.delivery.commit_exact(receipt)
        replay = await unclaimed.delivery.commit_exact(receipt)
        assert replay.record == replace(committed.record, idempotent_replay=True)
        second_attempt = replace(
            receipt,
            receipt_id="opportunity:receipt:exact:attempt-2",
            final_request_id="request:heartbeat:2",
            final_attempt_id="attempt:heartbeat:2",
        )
        second_consumer = replace(
            receipt,
            receipt_id="opportunity:receipt:exact:consumer-2",
            consumer_consciousness_instance_id="consciousness:heartbeat:2",
            final_request_id="request:heartbeat:consumer-2",
            final_attempt_id="attempt:heartbeat:consumer-2",
        )
        await unclaimed.delivery.commit_exact(second_attempt)
        await unclaimed.delivery.commit_exact(second_consumer)
        with pytest.raises(OpportunityConflict, match="delivery_receipt"):
            await unclaimed.delivery.commit_exact(
                replace(receipt, final_attempt_id="attempt:heartbeat:conflict")
            )
        deliveries = await unclaimed.delivery.list_deliveries("opportunity:heartbeat")
        assert {item.receipt.receipt_id for item in deliveries} == {
            receipt.receipt_id,
            second_attempt.receipt_id,
            second_consumer.receipt_id,
        }
        assert await reopened.delivery.awaiting_delivery() == ()

        # Receipt recording is an immutable fact.  Only the scheduler claim is
        # allowed to project it into next_due / activation state.
        before = await reopened.scheduler.scheduler_health_snapshot()
        assert before["pending_delivery_count"] == 1
        assert await reopened.scheduler.reconcile_deliveries() == (
            occurrence.occurrence_id,
        )
        assert await reopened.scheduler.reconcile_deliveries() == ()
        after = await reopened.scheduler.scheduler_health_snapshot()
        assert after["pending_delivery_count"] == 0
        assert await reopened.scheduler.materialize_due() == ()


async def test_pause_blocks_stale_publish_and_resume_reuses_only_published_history(
    tmp_path: Path,
) -> None:
    async with _local_runtime(tmp_path) as runtime:
        await _register_actor(runtime)
        unclaimed, claimed, _ = await _open_ready(runtime)
        authority = unclaimed.authority
        scheduler = claimed.scheduler
        assert scheduler is not None
        workflow = await _install_provider(authority, "provider:pause")
        due = (datetime.now(UTC) - timedelta(seconds=2)).isoformat()
        for suffix in ("published", "unpublished"):
            await authority.decide_opportunity(
                _registration_command(
                    f"opportunity:{suffix}",
                    "provider:pause",
                    workflow,
                    f"{suffix}-open",
                    schedule=OpportunitySchedule.AT,
                    first_due_at=due,
                    referent_id=f"referent:{suffix}:old",
                    referent_revision=4,
                )
            )
        occurrences = {
            item.opportunity_id: item for item in await scheduler.materialize_due()
        }
        published_occurrence = occurrences["opportunity:published"]
        unpublished_occurrence = occurrences["opportunity:unpublished"]
        publications = {
            item.opportunity_id: item for item in await scheduler.pending_publications()
        }
        published_digest = await _append_publication_event(
            runtime,
            publications["opportunity:published"],
            published_occurrence,
        )
        published = await scheduler.mark_published(
            publications["opportunity:published"].outbox_id,
            expected_revision=1,
            life_event_sha256=published_digest,
        )

        await authority.manage_provider(
            _provider_command(
                "provider:pause",
                workflow,
                "pause-provider",
                action=ProviderAction.PAUSE,
                expected_revision=1,
            )
        )
        assert await scheduler.pending_publications() == ()
        assert await unclaimed.delivery.awaiting_delivery() == ()
        with pytest.raises(OpportunityConflict, match="publication_evidence_missing"):
            await scheduler.mark_published(
                publications["opportunity:unpublished"].outbox_id,
                expected_revision=1,
                life_event_sha256=_sha("must-not-publish"),
            )
        cancelled = await unclaimed.delivery.get_publication(
            publications["opportunity:unpublished"].life_event_occurrence_id
        )
        assert cancelled is not None
        assert cancelled.status == PublicationStatus.CANCELLED

        await authority.manage_provider(
            _provider_command(
                "provider:pause",
                workflow,
                "resume-provider",
                action=ProviderAction.RESUME,
                expected_revision=2,
            )
        )
        # A published historical occurrence is woken again, not re-created.
        assert await unclaimed.delivery.awaiting_delivery() == (published,)
        assert (await scheduler.get_occurrence(published_occurrence.occurrence_id)) == (
            published_occurrence
        )
        replacements = await scheduler.materialize_due()
        assert len(replacements) == 1
        replacement = replacements[0]
        assert replacement.opportunity_id == "opportunity:unpublished"
        assert replacement.occurrence_id != unpublished_occurrence.occurrence_id
        assert replacement.due_index == unpublished_occurrence.due_index + 1
        assert replacement.referent_id == unpublished_occurrence.referent_id
        assert len(await scheduler.list_occurrences("opportunity:unpublished")) == 2


async def test_pause_after_life_event_append_preserves_publication_fact(
    tmp_path: Path,
) -> None:
    async with _local_runtime(tmp_path) as runtime:
        await _register_actor(runtime)
        unclaimed, claimed, _ = await _open_ready(runtime)
        authority = unclaimed.authority
        scheduler = claimed.scheduler
        assert scheduler is not None
        workflow = await _install_provider(authority, "provider:publish-race")
        await authority.decide_opportunity(
            _registration_command(
                "opportunity:publish-race",
                "provider:publish-race",
                workflow,
                "publish-race-open",
                schedule=OpportunitySchedule.AT,
                first_due_at=(datetime.now(UTC) - timedelta(seconds=2)).isoformat(),
            )
        )
        occurrence = (await scheduler.materialize_due())[0]
        publication = (await scheduler.pending_publications())[0]
        event_digest = await _append_publication_event(
            runtime,
            publication,
            occurrence,
        )

        await authority.manage_provider(
            _provider_command(
                "provider:publish-race",
                workflow,
                "publish-race-pause",
                action=ProviderAction.PAUSE,
                expected_revision=1,
            )
        )
        recovered = await scheduler.mark_published(
            publication.outbox_id,
            expected_revision=publication.revision,
            life_event_sha256=event_digest,
        )
        assert recovered.status == PublicationStatus.PUBLISHED
        assert recovered.revision == publication.revision + 2
        assert (
            await unclaimed.delivery.latest_publication("opportunity:publish-race")
            == recovered
        )
        assert (
            await unclaimed.delivery.latest_publication("opportunity:missing") is None
        )
        assert await unclaimed.delivery.awaiting_delivery() == ()

        context = b"<opportunity_available id='publish-race'/>"
        digest = _sha(context)
        commit = await unclaimed.delivery.commit_exact(
            OpportunityDeliveryReceipt(
                receipt_id="opportunity:receipt:publish-race",
                occurrence_id=occurrence.occurrence_id,
                life_event_occurrence_id=recovered.life_event_occurrence_id,
                consumer_consciousness_instance_id="consciousness:heartbeat:race",
                context_delivery_id="context:publish-race",
                final_request_id="request:publish-race",
                final_attempt_id="attempt:publish-race",
                exact_present=True,
                expected_bytes=len(context),
                effective_bytes=len(context),
                expected_sha256=digest,
                effective_sha256=digest,
                perceived_at=datetime.now(UTC).isoformat(),
            )
        )
        assert commit.record.receipt.occurrence_id == occurrence.occurrence_id
        assert await scheduler.reconcile_deliveries() == (occurrence.occurrence_id,)

        await authority.manage_provider(
            _provider_command(
                "provider:publish-race",
                workflow,
                "publish-race-resume",
                action=ProviderAction.RESUME,
                expected_revision=2,
            )
        )
        assert await unclaimed.delivery.awaiting_delivery() == ()
        assert await scheduler.materialize_due() == ()
        assert await scheduler.list_occurrences("opportunity:publish-race") == (
            occurrence,
        )


async def test_restart_recovers_cancelled_outbox_from_exact_life_event(
    tmp_path: Path,
) -> None:
    async with _local_runtime(tmp_path) as runtime:
        await _register_actor(runtime)
        unclaimed, claimed, claim = await _open_ready(runtime)
        authority = unclaimed.authority
        scheduler = claimed.scheduler
        assert scheduler is not None
        workflow = await _install_provider(authority, "provider:crash-window")
        await authority.decide_opportunity(
            _registration_command(
                "opportunity:crash-window",
                "provider:crash-window",
                workflow,
                "crash-window-open",
                schedule=OpportunitySchedule.AT,
                first_due_at=(datetime.now(UTC) - timedelta(seconds=2)).isoformat(),
            )
        )
        occurrence = (await scheduler.materialize_due())[0]
        publication = (await scheduler.pending_publications())[0]
        event_digest = await _append_publication_event(
            runtime,
            publication,
            occurrence,
        )

        # The event append committed, but the process lost its connection before
        # mark_published. A concurrent subject pause then cancelled the stale
        # projection. Restart reconciliation must recover the immutable fact
        # without appending a second event or waking the paused provider.
        await authority.manage_provider(
            _provider_command(
                "provider:crash-window",
                workflow,
                "crash-window-pause",
                action=ProviderAction.PAUSE,
                expected_revision=1,
            )
        )
        assert await scheduler.materialize_due() == ()
        cancelled = await unclaimed.delivery.get_publication(
            publication.life_event_occurrence_id
        )
        assert cancelled is not None
        assert cancelled.status == PublicationStatus.CANCELLED

        reopened = await open_opportunity_stores(runtime, writer_claim=claim)
        assert reopened.scheduler is not None
        assert await reopened.scheduler.reconcile_deliveries() == ()
        recovered = await reopened.delivery.get_publication(
            publication.life_event_occurrence_id
        )
        assert recovered is not None
        assert recovered.status == PublicationStatus.PUBLISHED
        assert recovered.life_event_sha256 == event_digest
        assert await reopened.delivery.awaiting_delivery() == ()

        context = b"<opportunity_available id='crash-window'/>"
        context_digest = _sha(context)
        await reopened.delivery.commit_exact(
            OpportunityDeliveryReceipt(
                receipt_id="opportunity:receipt:crash-window",
                occurrence_id=occurrence.occurrence_id,
                life_event_occurrence_id=recovered.life_event_occurrence_id,
                consumer_consciousness_instance_id="consciousness:heartbeat:crash",
                context_delivery_id="context:crash-window",
                final_request_id="request:crash-window",
                final_attempt_id="attempt:crash-window",
                exact_present=True,
                expected_bytes=len(context),
                effective_bytes=len(context),
                expected_sha256=context_digest,
                effective_sha256=context_digest,
                perceived_at=datetime.now(UTC).isoformat(),
            )
        )
        assert await reopened.scheduler.reconcile_deliveries() == (
            occurrence.occurrence_id,
        )
        await authority.manage_provider(
            _provider_command(
                "provider:crash-window",
                workflow,
                "crash-window-resume",
                action=ProviderAction.RESUME,
                expected_revision=2,
            )
        )
        assert await reopened.delivery.awaiting_delivery() == ()
        assert await reopened.scheduler.materialize_due() == ()
        assert await reopened.scheduler.list_occurrences(
            "opportunity:crash-window"
        ) == (occurrence,)
