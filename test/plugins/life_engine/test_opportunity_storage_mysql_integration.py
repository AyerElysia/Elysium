"""Opt-in real MySQL contract for the Opportunity authority."""

from __future__ import annotations

import hashlib
import os
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from plugins.life_engine.service.event_bus import LifeEvent
from plugins.life_engine.storage.authority import MySQLAuthorityRegistry
from plugins.life_engine.storage.contracts import StorageBackendRuntime
from plugins.life_engine.storage.domain_factory import open_presence_world_stores
from plugins.life_engine.storage.event_factory import open_life_event_store
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
from plugins.life_engine.storage.opportunity_contracts import (
    OPPORTUNITY_SCHEDULER_CLAIM_NAMESPACE,
    OPPORTUNITY_SCHEDULER_CLAIM_STATE_KEY,
    OpportunityAction,
    OpportunityDeliveryReceipt,
    OpportunityRegistrationCommand,
    OpportunitySchedule,
    ProviderAction,
    ProviderBindingCommand,
    PublicationStatus,
    WorkflowVersionCommand,
)
from plugins.life_engine.storage.opportunity_factory import open_opportunity_stores
from plugins.life_engine.storage.opportunity_schema import (
    mark_opportunity_runtime_managed,
    read_opportunity_runtime_marker,
)
from src.kernel.storage import canonical_json
from src.kernel.storage.engine import MySQLStorageConfig, create_mysql_storage_engine


def _sha(value: str | bytes) -> str:
    raw = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(raw).hexdigest()


def _mysql_config() -> MySQLStorageConfig:
    if os.environ.get("ELYSIUM_TEST_MYSQL_OPPORTUNITY_ISOLATED") != "1":
        pytest.skip("isolated Opportunity MySQL contract is not enabled")
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


def _generation(suffix: str) -> BackendGeneration:
    return BackendGeneration(
        generation_id=f"mysql-life-opportunity-contract-{suffix}",
        backend=BackendKind.MYSQL,
        schema_version=1,
        source_snapshot_sha256="5" * 64,
        root_hashes={"opportunity": "6" * 64},
        frontiers={"opportunity": 0},
        created_at="2026-09-05T00:00:00+00:00",
        verified_at="2026-09-05T00:01:00+00:00",
        status=GenerationStatus.VERIFIED,
    )


@pytest.mark.timeout(180)
async def test_mysql_opportunity_actor_scheduler_and_receipt_contract() -> None:
    config = _mysql_config()
    engine = create_mysql_storage_engine(config)
    registry_id = "life-opportunity-integration"
    registry = MySQLAuthorityRegistry(engine, registry_id=registry_id)
    runtime: StorageBackendRuntime | None = None
    token = None
    suffix = uuid4().hex
    try:
        generation = _generation(suffix)
        await registry.register_generation(generation)
        health = await registry.health()
        token = await registry.activate_generation(
            generation.generation_id,
            expected_epoch=int(health.get("authority_epoch") or 0),
            owner_id=f"opportunity-integration-writer:{suffix}",
            lease_seconds=180,
            confirm_previous_writers_stopped=True,
        )
        runtime = await open_storage_backend(
            StorageFactorySettings(
                enabled=True,
                authoritative_backend=BackendKind.MYSQL,
                backend_generation=generation.generation_id,
                schema_version=1,
                registry_id=registry_id,
                authority_provider="mysql",
                authority_epoch=token.authority_epoch,
                authority_owner_id=token.owner_id,
                fencing_token_env="TEST_OPPORTUNITY_MYSQL_FENCE",
                mysql=MySQLBackendSettings(
                    host=config.host,
                    port=config.port,
                    database=config.database,
                    user=config.user,
                    password_env="TEST_OPPORTUNITY_MYSQL_PASSWORD",
                    ssl_mode=config.ssl_mode,
                ),
            ),
            environment={
                "TEST_OPPORTUNITY_MYSQL_FENCE": token.fencing_token,
                "TEST_OPPORTUNITY_MYSQL_PASSWORD": config.password,
            },
        )
        presence_world = await open_presence_world_stores(
            runtime,
            initialize_schema=True,
        )
        actor = f"consciousness:opportunity:mysql:{suffix}"
        occurred_at = datetime.now(UTC).isoformat()
        await presence_world.presence.commit(
            {
                "instance_id": actor,
                "kind": "opportunity-mysql-contract",
                "display_name": "",
                "status": "active",
                "created_at": occurred_at,
                "last_active_at": occurred_at,
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
            event_payload={"occurred_at": occurred_at},
        )
        unclaimed = await open_opportunity_stores(
            runtime,
            initialize_schema=True,
        )
        marker = await mark_opportunity_runtime_managed(
            runtime,
            migration_occurrence_id="opportunity:migration:mysql-contract-v1",
        )
        assert await read_opportunity_runtime_marker(runtime) == marker
        workflow_content = "MySQL 机会流程🌸".encode()
        workflow = await unclaimed.authority.append_workflow(
            WorkflowVersionCommand(
                occurrence_id=f"opportunity:mysql:workflow:{suffix}",
                workflow_id=f"workflow:mysql:{suffix}",
                provider_id=f"provider:mysql:{suffix}",
                actor_consciousness_instance_id=actor,
                source_instance_id=actor,
                source_occurrence_ids=(f"life:event:{suffix}:workflow",),
                causation_occurrence_id=f"life:cause:{suffix}:workflow",
                expected_revision=0,
                schema_version=1,
                content_bytes=workflow_content,
                content_sha256=_sha(workflow_content),
                reason="她明确采用 MySQL 合同流程。",
                occurred_at=occurred_at,
            )
        )
        await unclaimed.authority.manage_provider(
            ProviderBindingCommand(
                occurrence_id=f"opportunity:mysql:provider:{suffix}",
                provider_id=workflow.provider_id,
                action=ProviderAction.INSTALL,
                actor_consciousness_instance_id=actor,
                source_instance_id=actor,
                source_occurrence_ids=(f"life:event:{suffix}:provider",),
                causation_occurrence_id=f"life:cause:{suffix}:provider",
                expected_revision=0,
                descriptor_version="1",
                descriptor_sha256=_sha(f"descriptor:{suffix}"),
                workflow_id=workflow.workflow_id,
                workflow_revision=workflow.revision,
                workflow_sha256=workflow.content_sha256,
                reason="她明确安装这个 provider。",
                occurred_at=occurred_at,
            )
        )
        opportunity_id = f"opportunity:mysql:{suffix}"
        first_due = (datetime.now(UTC) - timedelta(seconds=2)).isoformat()
        await unclaimed.authority.decide_opportunity(
            OpportunityRegistrationCommand(
                occurrence_id=f"opportunity:mysql:registration:{suffix}",
                opportunity_id=opportunity_id,
                provider_id=workflow.provider_id,
                action=OpportunityAction.OPEN,
                actor_consciousness_instance_id=actor,
                source_instance_id=actor,
                source_occurrence_ids=(f"life:event:{suffix}:registration",),
                causation_occurrence_id=f"life:cause:{suffix}:registration",
                expected_revision=0,
                referent_kind="skill",
                referent_id=f"learning:mysql:{suffix}",
                referent_revision=1,
                referent_sha256=_sha(f"learning:mysql:{suffix}:1"),
                workflow_id=workflow.workflow_id,
                workflow_revision=workflow.revision,
                workflow_sha256=workflow.content_sha256,
                schedule=OpportunitySchedule.AT,
                first_due_at=first_due,
                interval_seconds=0,
                reason="她明确打开这次 MySQL 机会。",
                occurred_at=occurred_at,
            )
        )
        claim = await runtime.acquire_singleton_writer(
            namespace=OPPORTUNITY_SCHEDULER_CLAIM_NAMESPACE,
            state_key=OPPORTUNITY_SCHEDULER_CLAIM_STATE_KEY,
            owner_instance_id=f"opportunity-scheduler:{suffix}",
            lease_seconds=120,
        )
        claimed = await open_opportunity_stores(runtime, writer_claim=claim)
        assert claimed.scheduler is not None
        occurrence = (await claimed.scheduler.materialize_due())[0]
        publication = (await claimed.scheduler.pending_publications())[0]
        event_store = await open_life_event_store(runtime, initialize_schema=True)
        event_payload = asdict(occurrence)
        event_payload["schema_version"] = 1
        event_payload["meaning"] = "availability_only"
        event_content = canonical_json(event_payload)
        await event_store.append(
            LifeEvent(
                event_id=publication.life_event_occurrence_id,
                sequence=0,
                occurrence_id=publication.life_event_occurrence_id,
                timestamp=occurrence.available_at,
                source="opportunity_runtime",
                channel="life",
                event_type="opportunity.available",
                content=event_content,
                source_instance_id="infrastructure:opportunity-scheduler",
                causation_id=occurrence.occurrence_id,
                metadata={"opportunity_occurrence_id": occurrence.occurrence_id},
            )
        )
        publication = await claimed.scheduler.mark_published(
            publication.outbox_id,
            expected_revision=publication.revision,
            life_event_sha256=_sha(event_content),
        )
        assert publication.status == PublicationStatus.PUBLISHED

        # A generation writer records exact perception without impersonating
        # the singleton scheduler.  The claimed scheduler reconciles later.
        context = f"<opportunity id='{suffix}'/>".encode()
        receipt = OpportunityDeliveryReceipt(
            receipt_id=f"opportunity:mysql:receipt:{suffix}",
            occurrence_id=occurrence.occurrence_id,
            life_event_occurrence_id=publication.life_event_occurrence_id,
            consumer_consciousness_instance_id=actor,
            context_delivery_id=f"context:{suffix}",
            final_request_id=f"request:{suffix}",
            final_attempt_id=f"attempt:{suffix}",
            exact_present=True,
            expected_bytes=len(context),
            effective_bytes=len(context),
            expected_sha256=_sha(context),
            effective_sha256=_sha(context),
            perceived_at=datetime.now(UTC).isoformat(),
        )
        await unclaimed.delivery.commit_exact(receipt)
        assert await unclaimed.delivery.awaiting_delivery() == ()
        assert await claimed.scheduler.reconcile_deliveries() == (
            occurrence.occurrence_id,
        )
        reopened = await open_opportunity_stores(runtime, writer_claim=claim)
        assert reopened.scheduler is not None
        assert await reopened.scheduler.get_occurrence(occurrence.occurrence_id) == (
            occurrence
        )
        assert await reopened.scheduler.materialize_due() == ()

        with pytest.raises(DBAPIError, match="OpportunityImmutable"):
            async with runtime.unit_of_work() as uow:
                await uow.session.execute(
                    text(
                        """UPDATE opportunity_occurrences
                        SET occurrence_sha256=:digest
                        WHERE occurrence_id=:occurrence_id"""
                    ),
                    {"digest": "0" * 64, "occurrence_id": occurrence.occurrence_id},
                )
        with pytest.raises(DBAPIError, match="OpportunitySchedulerClaimRequired"):
            async with runtime.unit_of_work() as uow:
                await uow.session.execute(
                    text(
                        """UPDATE opportunity_activation_states
                        SET revision=revision+1
                        WHERE opportunity_id=:opportunity_id"""
                    ),
                    {"opportunity_id": opportunity_id},
                )
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
