"""Isolated authority and lifecycle contracts for OpportunityRuntime."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from plugins.life_engine.opportunity.catalog import CapabilityCatalog
from plugins.life_engine.opportunity.registry import (
    CapabilityInvocation,
    CapabilityOperationDenied,
    CapabilityRuntimeRegistry,
)
from plugins.life_engine.opportunity.runtime import (
    OpportunityCaller,
    OpportunityRuntime,
)
from plugins.life_engine.service.event_bus import LifeEvent
from plugins.life_engine.storage.opportunity_contracts import (
    OpportunityOccurrence,
    OpportunityOrigin,
    OpportunityPublication,
    OpportunityRegistration,
    OpportunitySchedule,
    OpportunityStatus,
    OpportunityStores,
    ProviderAction,
    ProviderBinding,
    ProviderCommit,
    ProviderStatus,
    PublicationStatus,
    WorkflowVersion,
)

_SHA_A = "a" * 64
_SHA_B = "b" * 64
_NOW = "2026-09-05T00:00:00+00:00"


def _catalog(tmp_path: Path) -> CapabilityCatalog:
    package = tmp_path / "learning"
    package.mkdir()
    manifest = {
        "schema_version": 1,
        "capability_id": "life.learning",
        "package_version": "1.0.0",
        "removability": "subject_removable",
        "provider_kind": "learning",
        "manual": "CAPABILITY.md",
        "default_skill": "DEFAULT_SKILL.md",
        "operations": ["nucleus_learn"],
        "dependencies": ["life.timeline"],
    }
    (package / "manifest.json").write_text(
        json.dumps(manifest, sort_keys=True), encoding="utf-8"
    )
    (package / "CAPABILITY.md").write_text("# Technical manual\n", encoding="utf-8")
    (package / "DEFAULT_SKILL.md").write_text(
        "# Never auto-adopt this workflow\n", encoding="utf-8"
    )
    catalog = CapabilityCatalog()
    catalog.discover_package(package)
    return catalog


def _caller(
    *,
    actor: str = "consciousness:chat",
    decision: str = "decision:1",
) -> OpportunityCaller:
    return OpportunityCaller(
        actor_consciousness_instance_id=actor,
        source_instance_id=actor,
        source_occurrence_id="source:1",
        decision_occurrence_id=decision,
        occurred_at=_NOW,
        caller_tool={"tool_names": {"nucleus_learn"}},
    )


def _workflow_arguments() -> dict[str, Any]:
    return {
        "workflow_id": "workflow:learning",
        "workflow_revision": 1,
        "workflow_sha256": _SHA_B,
    }


class _Authority:
    def __init__(self) -> None:
        self.providers: dict[str, ProviderBinding] = {}
        self.opportunities: dict[str, OpportunityRegistration] = {}
        self.provider_commands: list[Any] = []
        self.workflow_commands: list[Any] = []
        self.close_calls = 0

    async def manage_provider(self, command: Any) -> ProviderCommit:
        self.provider_commands.append(command)
        previous = self.providers.get(command.provider_id)
        revision = 1 if previous is None else previous.revision + 1
        if command.action == ProviderAction.BIND_WORKFLOW:
            if previous is None:
                raise AssertionError("bind requires an existing provider")
            status = previous.status
        else:
            status = {
                ProviderAction.INSTALL: ProviderStatus.ENABLED,
                ProviderAction.PAUSE: ProviderStatus.PAUSED,
                ProviderAction.RESUME: ProviderStatus.ENABLED,
                ProviderAction.UNINSTALL: ProviderStatus.UNINSTALLED,
            }[command.action]
        self.providers[command.provider_id] = ProviderBinding(
            provider_id=command.provider_id,
            status=status,
            revision=revision,
            descriptor_version=command.descriptor_version,
            descriptor_sha256=command.descriptor_sha256,
            workflow_id=command.workflow_id,
            workflow_revision=command.workflow_revision,
            workflow_sha256=command.workflow_sha256,
            last_occurrence_id=command.occurrence_id,
            last_event_position=revision,
            updated_at=command.occurred_at,
        )
        return ProviderCommit(
            provider_id=command.provider_id,
            occurrence_id=command.occurrence_id,
            revision=revision,
            status=status,
            event_sha256=_SHA_A,
            idempotent_replay=False,
        )

    async def append_workflow(self, command: Any) -> Any:
        self.workflow_commands.append(command)
        revision = sum(
            item.workflow_id == command.workflow_id for item in self.workflow_commands
        )
        return WorkflowVersion(
            position=len(self.workflow_commands),
            occurrence_id=command.occurrence_id,
            workflow_id=command.workflow_id,
            provider_id=command.provider_id,
            revision=revision,
            schema_version=command.schema_version,
            actor_consciousness_instance_id=(command.actor_consciousness_instance_id),
            source_instance_id=command.source_instance_id,
            source_occurrence_ids=command.source_occurrence_ids,
            causation_occurrence_id=command.causation_occurrence_id,
            content_bytes=command.content_bytes,
            content_sha256=command.content_sha256,
            reason=command.reason,
            occurred_at=command.occurred_at,
            recorded_at=command.occurred_at,
            event_sha256=_SHA_A,
        )

    async def get_provider(self, provider_id: str) -> ProviderBinding | None:
        return self.providers.get(provider_id)

    async def get_opportunity(
        self, opportunity_id: str
    ) -> OpportunityRegistration | None:
        return self.opportunities.get(opportunity_id)

    async def health_snapshot(self) -> dict[str, Any]:
        return {"status": "ready", "provider_count": len(self.providers)}

    async def close(self) -> None:
        self.close_calls += 1


class _Scheduler:
    def __init__(
        self,
        occurrence: OpportunityOccurrence | None = None,
        publication: OpportunityPublication | None = None,
    ) -> None:
        self.occurrence = occurrence
        self.publication = publication
        self.mark_failures = 0
        self.mark_calls: list[tuple[str, int, str]] = []
        self.materialize_calls = 0
        self.reconcile_calls = 0
        self.close_calls = 0

    async def reconcile_deliveries(self, *, limit: int = 100) -> tuple[str, ...]:
        self.reconcile_calls += 1
        return ()

    async def materialize_due(
        self, *, limit: int = 100
    ) -> tuple[OpportunityOccurrence, ...]:
        self.materialize_calls += 1
        return (self.occurrence,) if self.occurrence is not None else ()

    async def pending_publications(
        self, *, limit: int = 100
    ) -> tuple[OpportunityPublication, ...]:
        publication = self.publication
        if publication is None or publication.status != PublicationStatus.PENDING:
            return ()
        return (publication,)

    async def awaiting_delivery(
        self, *, limit: int = 100
    ) -> tuple[OpportunityPublication, ...]:
        publication = self.publication
        if publication is None or publication.status != PublicationStatus.PUBLISHED:
            return ()
        return (publication,)

    async def scheduler_health_snapshot(self) -> dict[str, Any]:
        publication = self.publication
        pending_publication_count = int(
            publication is not None and publication.status == PublicationStatus.PENDING
        )
        pending_delivery_count = int(
            publication is not None
            and publication.status == PublicationStatus.PUBLISHED
        )
        return {
            "status": "ready",
            "pending_publication_count": pending_publication_count,
            "pending_delivery_count": pending_delivery_count,
        }

    async def get_occurrence(self, occurrence_id: str) -> OpportunityOccurrence | None:
        if (
            self.occurrence is not None
            and self.occurrence.occurrence_id == occurrence_id
        ):
            return self.occurrence
        return None

    async def mark_published(
        self,
        outbox_id: str,
        *,
        expected_revision: int,
        life_event_sha256: str,
    ) -> OpportunityPublication:
        self.mark_calls.append((outbox_id, expected_revision, life_event_sha256))
        if self.mark_failures:
            self.mark_failures -= 1
            raise OSError("injected acknowledgement failure")
        if self.publication is None:
            raise AssertionError("missing publication")
        self.publication = replace(
            self.publication,
            status=PublicationStatus.PUBLISHED,
            revision=self.publication.revision + 1,
            life_event_sha256=life_event_sha256,
        )
        return self.publication

    async def close(self) -> None:
        self.close_calls += 1


class _Delivery:
    def __init__(self) -> None:
        self.close_calls = 0
        self.publications: dict[str, OpportunityPublication] = {}
        self.receipts: list[Any] = []

    async def get_publication(
        self, life_event_occurrence_id: str
    ) -> OpportunityPublication | None:
        return self.publications.get(life_event_occurrence_id)

    async def commit_exact(self, receipt: Any) -> Any:
        self.receipts.append(receipt)
        return SimpleNamespace(record=SimpleNamespace(receipt=receipt))

    async def list_deliveries(
        self, opportunity_id: str, *, limit: int = 100
    ) -> tuple[Any, ...]:
        return ()

    async def awaiting_delivery(
        self, *, limit: int = 100
    ) -> tuple[OpportunityPublication, ...]:
        seen = {
            item.life_event_occurrence_id
            for item in self.receipts
            if hasattr(item, "life_event_occurrence_id")
        }
        pending = [
            item
            for item in self.publications.values()
            if item.status == PublicationStatus.PUBLISHED
            and item.life_event_occurrence_id not in seen
        ]
        return tuple(pending[:limit])

    async def close(self) -> None:
        self.close_calls += 1


class _Executor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.close_calls = 0

    async def execute(
        self, operation_id: str, arguments: Mapping[str, Any]
    ) -> dict[str, Any]:
        self.calls.append((operation_id, dict(arguments)))
        return {"ok": True}

    async def close(self) -> None:
        self.close_calls += 1


class _Harness:
    def __init__(
        self,
        catalog: CapabilityCatalog,
        *,
        authority: _Authority | None = None,
        scheduler: _Scheduler | None = None,
        delivery: _Delivery | None = None,
        allowed_actors: set[str] | None = None,
        publish_event: Any = None,
    ) -> None:
        self.authority = authority or _Authority()
        self.scheduler = scheduler
        self.delivery = delivery
        self.executor = _Executor()
        self.dispatches: list[CapabilityInvocation] = []
        self.published: list[Any] = []
        self.wake_calls = 0
        self.lifecycle_calls: list[tuple[str, ProviderStatus]] = []
        self.allowed_actors = allowed_actors or {"consciousness:chat"}

        async def dispatch(
            executor: _Executor, invocation: CapabilityInvocation
        ) -> Any:
            self.dispatches.append(invocation)
            tool_names = set(invocation.caller_context.caller_tool["tool_names"])
            if invocation.operation_id not in tool_names:
                raise PermissionError("caller tool manifest denied operation")
            return await executor.execute(invocation.operation_id, invocation.arguments)

        async def check_actor(actor_id: str) -> None:
            if actor_id not in self.allowed_actors:
                raise PermissionError("inactive or foreign consciousness instance")

        async def publish(event: Any) -> Any:
            self.published.append(event)
            return event

        async def lifecycle(capability_id: str, status: ProviderStatus) -> None:
            self.lifecycle_calls.append((capability_id, status))

        self.publish = publish_event or publish
        self.registry = CapabilityRuntimeRegistry(
            catalog,
            dispatch=dispatch,
            factories={"life.learning": lambda _descriptor: self.executor},
        )
        self.runtime = OpportunityRuntime(
            OpportunityStores(
                authority=self.authority,
                scheduler=self.scheduler,
                delivery=self.delivery,
            ),
            catalog,
            self.registry,
            check_actor=check_actor,
            publish_event=self.publish,
            wake=self._wake,
            lifecycle=lifecycle,
        )

    def _wake(self) -> None:
        self.wake_calls += 1


def _enabled_provider(catalog: CapabilityCatalog) -> ProviderBinding:
    descriptor = catalog.require("life.learning")
    return ProviderBinding(
        provider_id=descriptor.capability_id,
        status=ProviderStatus.ENABLED,
        revision=1,
        descriptor_version=descriptor.package_version,
        descriptor_sha256=descriptor.package_sha256,
        workflow_id="workflow:learning",
        workflow_revision=1,
        workflow_sha256=_SHA_B,
        last_occurrence_id="decision:install",
        last_event_position=1,
        updated_at=_NOW,
    )


def _due_state(
    catalog: CapabilityCatalog,
) -> tuple[OpportunityRegistration, OpportunityOccurrence, OpportunityPublication]:
    descriptor = catalog.require("life.learning")
    registration = OpportunityRegistration(
        opportunity_id="opportunity:learning:1",
        provider_id=descriptor.capability_id,
        origin=OpportunityOrigin.SUBJECT,
        status=OpportunityStatus.OPEN,
        revision=1,
        referent_kind="learning.review",
        referent_id="referent:1",
        referent_revision=1,
        referent_sha256=_SHA_A,
        workflow_id="workflow:learning",
        workflow_revision=1,
        workflow_sha256=_SHA_B,
        schedule=OpportunitySchedule.AT,
        first_due_at=_NOW,
        interval_seconds=0,
        last_occurrence_id="decision:open",
        last_event_position=2,
        updated_at=_NOW,
    )
    occurrence = OpportunityOccurrence(
        position=1,
        occurrence_id="occurrence:due:1",
        opportunity_id=registration.opportunity_id,
        registration_revision=registration.revision,
        provider_id=descriptor.capability_id,
        provider_revision=1,
        referent_kind=registration.referent_kind,
        referent_id=registration.referent_id,
        referent_revision=registration.referent_revision,
        referent_sha256=registration.referent_sha256,
        workflow_id=registration.workflow_id,
        workflow_revision=registration.workflow_revision,
        workflow_sha256=registration.workflow_sha256,
        due_index=1,
        scheduled_for=_NOW,
        available_at=_NOW,
        source_frontier=2,
        occurrence_sha256=_SHA_A,
    )
    publication = OpportunityPublication(
        outbox_id="outbox:1",
        occurrence_id=occurrence.occurrence_id,
        opportunity_id=registration.opportunity_id,
        status=PublicationStatus.PENDING,
        revision=1,
        life_event_occurrence_id="life-event:opportunity:1",
        life_event_sha256="",
        created_at=_NOW,
        updated_at=_NOW,
    )
    return registration, occurrence, publication


def _effective_receipt(expected_text: str, **changes: Any) -> SimpleNamespace:
    encoded = expected_text.encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    values = {
        "delivery_id": "delivery:1",
        "exact_present": True,
        "expected_utf8_bytes": len(encoded),
        "effective_utf8_bytes": len(encoded),
        "expected_sha256": digest,
        "effective_sha256": digest,
    }
    values.update(changes)
    return SimpleNamespace(**values)


@pytest.mark.asyncio
async def test_restore_discovers_package_without_adopting_template(
    tmp_path: Path,
) -> None:
    catalog = _catalog(tmp_path)
    harness = _Harness(catalog)

    await harness.runtime.restore()

    state = await harness.registry.state("life.learning")
    assert state.installed is False
    assert state.enabled is False
    assert harness.authority.workflow_commands == []
    assert harness.executor.calls == []
    assert (
        OpportunityRuntime.describe(catalog.require("life.learning"))[
            "default_skill_adopted"
        ]
        is False
    )


@pytest.mark.asyncio
async def test_install_requires_explicit_workflow_and_never_uses_template(
    tmp_path: Path,
) -> None:
    catalog = _catalog(tmp_path)
    harness = _Harness(catalog)

    with pytest.raises(ValueError, match="ExplicitWorkflowReferenceRequired"):
        await harness.runtime.manage(
            "capability.install",
            "life.learning",
            0,
            {},
            "I choose my workflow",
            _caller(),
        )
    assert harness.authority.provider_commands == []
    assert harness.authority.workflow_commands == []

    result = await harness.runtime.manage(
        "capability.install",
        "life.learning",
        0,
        _workflow_arguments(),
        "I choose my workflow",
        _caller(decision="decision:install"),
    )

    assert result["authority_committed"] is True
    assert result["runtime_applied"] is True
    assert harness.authority.workflow_commands == []
    assert (await harness.registry.state("life.learning")).enabled is True


@pytest.mark.asyncio
async def test_subject_workflow_rewrite_does_not_change_binding_until_explicit_bind(
    tmp_path: Path,
) -> None:
    catalog = _catalog(tmp_path)
    authority = _Authority()
    authority.providers["life.learning"] = _enabled_provider(catalog)
    harness = _Harness(catalog, authority=authority)
    exact_text = "# My revised learning workflow\n\nPause when evidence is weak.\n"
    exact_sha256 = hashlib.sha256(exact_text.encode("utf-8")).hexdigest()

    written = await harness.runtime.manage(
        "workflow.replace",
        "workflow:learning:revised",
        0,
        {"capability_id": "life.learning", "content": exact_text},
        "I am revising this workflow before choosing whether to use it",
        _caller(decision="decision:rewrite"),
    )

    assert written["authority_committed"] is True
    assert written["binding_changed"] is False
    assert written["workflow"]["content_sha256"] == exact_sha256
    assert "content_bytes" not in written["workflow"]
    assert authority.providers["life.learning"].workflow_id == "workflow:learning"
    assert authority.providers["life.learning"].workflow_revision == 1
    assert authority.providers["life.learning"].workflow_sha256 == _SHA_B

    bound = await harness.runtime.manage(
        "capability.bind_workflow",
        "life.learning",
        1,
        {
            "workflow_id": "workflow:learning:revised",
            "workflow_revision": 1,
            "workflow_sha256": exact_sha256,
        },
        "I choose this exact revision for future learning calls",
        _caller(decision="decision:bind"),
    )

    assert bound["authority_committed"] is True
    assert bound["runtime_applied"] is True
    binding = authority.providers["life.learning"]
    assert binding.revision == 2
    assert binding.status == ProviderStatus.ENABLED
    assert binding.workflow_id == "workflow:learning:revised"
    assert binding.workflow_revision == 1
    assert binding.workflow_sha256 == exact_sha256


@pytest.mark.asyncio
async def test_workflow_optional_hash_is_verified_when_subject_supplies_it(
    tmp_path: Path,
) -> None:
    harness = _Harness(_catalog(tmp_path))

    with pytest.raises(ValueError, match="does not match workflow bytes"):
        await harness.runtime.manage(
            "workflow.replace",
            "workflow:learning:revised",
            0,
            {
                "capability_id": "life.learning",
                "content": "# exact subject text\n",
                "content_sha256": "f" * 64,
            },
            "",
            _caller(decision="decision:bad-hash"),
        )

    assert harness.authority.workflow_commands == []


@pytest.mark.asyncio
async def test_protocol_query_discloses_bind_arguments_without_mutation(
    tmp_path: Path,
) -> None:
    harness = _Harness(_catalog(tmp_path))

    result = await harness.runtime.query(
        resource="protocol",
        record_id="capability.bind_workflow",
        actor_consciousness_instance_id="consciousness:chat",
    )

    assert result["action"] == "capability.bind_workflow"
    assert set(result["arguments"]) == {
        "workflow_id",
        "workflow_revision",
        "workflow_sha256",
    }
    assert harness.authority.provider_commands == []
    assert harness.authority.workflow_commands == []


@pytest.mark.asyncio
async def test_operation_schema_query_reconstructs_exact_bounded_artifact(
    tmp_path: Path,
) -> None:
    harness = _Harness(_catalog(tmp_path))
    offset = 0
    chunks: list[str] = []
    expected_total = 0
    expected_sha256 = ""

    while True:
        result = await harness.runtime.query(
            resource="operation_schema",
            record_id="life.learning",
            operation="nucleus_learn",
            action="list_insights",
            offset_bytes=offset,
            max_bytes=4096,
            actor_consciousness_instance_id="consciousness:chat",
        )
        assert result["origin"] == "engineering_operation_schema"
        assert result["encoding"] == "canonical_json_utf8"
        assert result["grants_permission"] is False
        assert result["capability_id"] == "life.learning"
        assert result["operation"] == "nucleus_learn"
        assert result["action"] == "list_insights"
        assert len(json.dumps(result, ensure_ascii=False).encode("utf-8")) <= 4096
        if expected_total:
            assert result["total_bytes"] == expected_total
            assert result["sha256"] == expected_sha256
        else:
            expected_total = result["total_bytes"]
            expected_sha256 = result["sha256"]
        chunks.append(result["content"])
        offset = result["next_offset_bytes"]
        if result["complete"]:
            break

    exact_bytes = "".join(chunks).encode("utf-8")
    assert len(exact_bytes) == expected_total
    assert hashlib.sha256(exact_bytes).hexdigest() == expected_sha256
    disclosed = json.loads(exact_bytes)
    assert disclosed["capability_id"] == "life.learning"
    assert disclosed["action"] == "list_insights"
    assert disclosed["grants_permission"] is False
    assert disclosed["identity_bound_by_runtime"] is True
    assert disclosed["arguments_schema"]["additionalProperties"] is False
    assert harness.authority.provider_commands == []
    assert harness.authority.workflow_commands == []
    assert harness.executor.calls == []


@pytest.mark.asyncio
async def test_operation_schema_query_rejects_cross_package_operation(
    tmp_path: Path,
) -> None:
    harness = _Harness(_catalog(tmp_path))

    with pytest.raises(PermissionError, match="OperationNotDeclared"):
        await harness.runtime.query(
            resource="operation_schema",
            record_id="life.learning",
            operation="nucleus_read_file",
            max_bytes=4096,
            actor_consciousness_instance_id="consciousness:chat",
        )

    assert harness.executor.calls == []


@pytest.mark.asyncio
async def test_pause_uninstall_and_restart_do_not_resurrect_capability(
    tmp_path: Path,
) -> None:
    catalog = _catalog(tmp_path)
    authority = _Authority()
    first = _Harness(catalog, authority=authority)
    await first.runtime.manage(
        "capability.install",
        "life.learning",
        0,
        _workflow_arguments(),
        "install",
        _caller(decision="decision:install"),
    )
    await first.runtime.manage(
        "capability.pause",
        "life.learning",
        1,
        {},
        "pause",
        _caller(decision="decision:pause"),
    )
    assert (await first.registry.state("life.learning")).enabled is False

    await first.runtime.manage(
        "capability.uninstall",
        "life.learning",
        2,
        {},
        "remove from my runtime",
        _caller(decision="decision:uninstall"),
    )
    assert (await first.registry.state("life.learning")).installed is False

    after_restart = _Harness(catalog, authority=authority)
    await after_restart.runtime.restore()

    restored = await after_restart.registry.state("life.learning")
    assert restored.installed is False
    assert restored.enabled is False
    assert after_restart.authority.workflow_commands == []
    assert after_restart.executor.calls == []


@pytest.mark.asyncio
async def test_foreign_or_inactive_instance_cannot_pause_subject_authority(
    tmp_path: Path,
) -> None:
    catalog = _catalog(tmp_path)
    authority = _Authority()
    authority.providers["life.learning"] = _enabled_provider(catalog)
    harness = _Harness(catalog, authority=authority)

    with pytest.raises(PermissionError, match="foreign consciousness"):
        await harness.runtime.manage(
            "capability.pause",
            "life.learning",
            1,
            {},
            "foreign pause",
            _caller(actor="consciousness:foreign", decision="decision:foreign"),
        )

    assert authority.providers["life.learning"].status == ProviderStatus.ENABLED
    assert authority.provider_commands == []


@pytest.mark.asyncio
async def test_package_hash_drift_pauses_local_executor_and_fails_closed(
    tmp_path: Path,
) -> None:
    catalog = _catalog(tmp_path)
    authority = _Authority()
    authority.providers["life.learning"] = replace(
        _enabled_provider(catalog), descriptor_sha256="f" * 64
    )
    harness = _Harness(catalog, authority=authority)
    await harness.registry.install("life.learning")
    await harness.registry.enable("life.learning")

    await harness.runtime.restore()

    assert (await harness.registry.state("life.learning")).enabled is False
    assert harness.lifecycle_calls[-1] == (
        "life.learning",
        ProviderStatus.PAUSED,
    )
    health = await harness.runtime.health()
    assert health["runtime_errors"] == {"life.learning": "RuntimeError"}
    with pytest.raises(RuntimeError, match="PackageChanged"):
        await harness.runtime.call("life.learning", "nucleus_learn", {}, _caller())
    assert harness.executor.calls == []


@pytest.mark.asyncio
async def test_publish_success_ack_failure_replays_exact_same_life_event(
    tmp_path: Path,
) -> None:
    catalog = _catalog(tmp_path)
    registration, occurrence, publication = _due_state(catalog)
    authority = _Authority()
    authority.providers["life.learning"] = _enabled_provider(catalog)
    authority.opportunities[registration.opportunity_id] = registration
    scheduler = _Scheduler(occurrence, publication)
    scheduler.mark_failures = 1
    harness = _Harness(catalog, authority=authority, scheduler=scheduler)

    with pytest.raises(OSError, match="acknowledgement"):
        await harness.runtime.pump()
    assert harness.wake_calls == 0
    assert len(harness.published) == 1

    assert await harness.runtime.pump() == 1

    assert len(harness.published) == 2
    first, replay = harness.published
    assert replay.event_id == first.event_id
    assert replay.occurrence_id == first.occurrence_id
    assert replay.content == first.content
    assert replay.metadata == first.metadata
    expected_hash = hashlib.sha256(first.content.encode("utf-8")).hexdigest()
    assert scheduler.mark_calls == [
        (publication.outbox_id, publication.revision, expected_hash),
        (publication.outbox_id, publication.revision, expected_hash),
    ]
    assert harness.wake_calls == 1
    assert harness.dispatches == []
    assert harness.executor.calls == []


@pytest.mark.asyncio
async def test_due_occurrence_only_publishes_and_wakes_never_executes_skill(
    tmp_path: Path,
) -> None:
    catalog = _catalog(tmp_path)
    registration, occurrence, publication = _due_state(catalog)
    authority = _Authority()
    authority.providers["life.learning"] = _enabled_provider(catalog)
    authority.opportunities[registration.opportunity_id] = registration
    harness = _Harness(
        catalog,
        authority=authority,
        scheduler=_Scheduler(occurrence, publication),
    )
    await harness.registry.install("life.learning")
    await harness.registry.enable("life.learning")

    assert await harness.runtime.pump() == 1

    assert harness.wake_calls == 1
    assert len(harness.published) == 1
    event = harness.published[0]
    assert event.event_type == "opportunity.available"
    assert json.loads(event.content)["meaning"] == "availability_only"
    assert harness.dispatches == []
    assert harness.executor.calls == []


@pytest.mark.asyncio
async def test_published_unseen_occurrence_wakes_after_runtime_restart(
    tmp_path: Path,
) -> None:
    catalog = _catalog(tmp_path)
    registration, _occurrence, publication = _due_state(catalog)
    authority = _Authority()
    authority.providers["life.learning"] = _enabled_provider(catalog)
    authority.opportunities[registration.opportunity_id] = registration
    published = replace(
        publication,
        status=PublicationStatus.PUBLISHED,
        revision=publication.revision + 1,
        life_event_sha256=_SHA_A,
    )
    delivery = _Delivery()
    delivery.publications[published.life_event_occurrence_id] = published
    harness = _Harness(catalog, authority=authority, delivery=delivery)

    assert await harness.runtime.pump() == 0

    assert harness.scheduler is None
    assert harness.published == []
    assert harness.wake_calls == 1
    assert harness.dispatches == []
    assert harness.executor.calls == []


@pytest.mark.asyncio
async def test_pump_cancellation_propagates_and_keeps_publication_pending(
    tmp_path: Path,
) -> None:
    catalog = _catalog(tmp_path)
    registration, occurrence, publication = _due_state(catalog)
    authority = _Authority()
    authority.providers["life.learning"] = _enabled_provider(catalog)
    authority.opportunities[registration.opportunity_id] = registration
    scheduler = _Scheduler(occurrence, publication)

    async def cancelled_publish(_event: Any) -> Any:
        raise asyncio.CancelledError

    harness = _Harness(
        catalog,
        authority=authority,
        scheduler=scheduler,
        publish_event=cancelled_publish,
    )

    with pytest.raises(asyncio.CancelledError):
        await harness.runtime.pump()

    assert scheduler.publication is not None
    assert scheduler.publication.status == PublicationStatus.PENDING
    assert scheduler.mark_calls == []
    assert harness.wake_calls == 0


@pytest.mark.asyncio
async def test_call_cannot_cross_manifest_operation_boundary(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    authority = _Authority()
    authority.providers["life.learning"] = _enabled_provider(catalog)
    harness = _Harness(catalog, authority=authority)
    await harness.runtime.restore()

    with pytest.raises(CapabilityOperationDenied):
        await harness.runtime.call(
            "life.learning",
            "nucleus_write_file",
            {"skill_text": "please expand my permissions"},
            _caller(),
        )

    assert harness.dispatches == []
    assert harness.executor.calls == []


@pytest.mark.asyncio
async def test_close_is_idempotent_and_never_closes_injected_stores(
    tmp_path: Path,
) -> None:
    catalog = _catalog(tmp_path)
    authority = _Authority()
    scheduler = _Scheduler()
    delivery = _Delivery()
    harness = _Harness(
        catalog,
        authority=authority,
        scheduler=scheduler,
        delivery=delivery,
    )
    await harness.registry.install("life.learning")
    await harness.registry.enable("life.learning")

    await harness.runtime.close()
    await harness.runtime.close()

    assert (await harness.registry.state("life.learning")).installed is False
    assert harness.executor.close_calls == 0
    assert authority.close_calls == 0
    assert scheduler.close_calls == 0
    assert delivery.close_calls == 0
    assert harness.lifecycle_calls == [("life.learning", ProviderStatus.PAUSED)]


@pytest.mark.asyncio
async def test_manual_and_default_skill_query_remain_separate_package_artifacts(
    tmp_path: Path,
) -> None:
    catalog = _catalog(tmp_path)
    harness = _Harness(catalog)

    manual = await harness.runtime.query(
        resource="manual",
        record_id="life.learning",
        max_bytes=2048,
        actor_consciousness_instance_id="consciousness:chat",
    )
    workflow = await harness.runtime.query(
        resource="default_skill",
        record_id="life.learning",
        max_bytes=2048,
        actor_consciousness_instance_id="consciousness:chat",
    )

    assert manual["artifact"] == "manual"
    assert manual["origin"] == "engineering_package"
    assert manual["subject_adopted"] is False
    assert manual["content"] == "# Technical manual\n"
    assert workflow["artifact"] == "default_skill"
    assert workflow["origin"] == "engineering_package"
    assert workflow["subject_adopted"] is False
    assert workflow["content"] == "# Never auto-adopt this workflow\n"
    assert harness.authority.workflow_commands == []


@pytest.mark.asyncio
async def test_exact_seen_receipt_records_content_free_delivery_fact(
    tmp_path: Path,
) -> None:
    catalog = _catalog(tmp_path)
    registration, occurrence, publication = _due_state(catalog)
    authority = _Authority()
    authority.providers["life.learning"] = _enabled_provider(catalog)
    authority.opportunities[registration.opportunity_id] = registration
    delivery = _Delivery()
    harness = _Harness(
        catalog,
        authority=authority,
        scheduler=_Scheduler(occurrence, publication),
        delivery=delivery,
    )
    assert await harness.runtime.pump() == 1
    event: LifeEvent = harness.published[0]
    assert harness.scheduler is not None
    assert harness.scheduler.publication is not None
    delivery.publications[event.occurrence_id] = harness.scheduler.publication
    expected_text = f"<recent-life>\n{event.content}\n</recent-life>"

    committed = await harness.runtime.record_seen(
        events=[event],
        expected_text=expected_text,
        receipt=_effective_receipt(expected_text),
        consumer_instance_id="consciousness:chat",
        final_request_id="request:1",
        final_attempt_id="attempt:1",
        perceived_at=_NOW,
    )

    assert committed == 1
    assert len(delivery.receipts) == 1
    stored = delivery.receipts[0]
    assert stored.occurrence_id == occurrence.occurrence_id
    assert stored.life_event_occurrence_id == event.occurrence_id
    assert not hasattr(stored, "content")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "receipt_changes",
    [
        {"expected_sha256": "f" * 64},
        {"effective_utf8_bytes": 1},
        {"exact_present": False},
    ],
)
async def test_wrong_hash_or_truncated_context_never_records_seen(
    tmp_path: Path,
    receipt_changes: dict[str, Any],
) -> None:
    catalog = _catalog(tmp_path)
    delivery = _Delivery()
    harness = _Harness(catalog, delivery=delivery)
    event = LifeEvent(
        event_id="life-event:opportunity:1",
        sequence=1,
        occurrence_id="life-event:opportunity:1",
        timestamp=_NOW,
        source="opportunity_runtime",
        channel="life",
        event_type="opportunity.available",
        content='{"meaning":"availability_only"}',
    )
    expected_text = f"prefix\n{event.content}\nsuffix"

    with pytest.raises(RuntimeError, match="ExactDeliveryRequired"):
        await harness.runtime.record_seen(
            events=[event],
            expected_text=expected_text,
            receipt=_effective_receipt(expected_text, **receipt_changes),
            consumer_instance_id="consciousness:chat",
            final_request_id="request:1",
            final_attempt_id="attempt:1",
            perceived_at=_NOW,
        )

    assert delivery.receipts == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("request_id", "attempt_id"),
    [("", "attempt:1"), ("request:1", "")],
)
async def test_missing_final_request_or_attempt_never_records_seen(
    tmp_path: Path,
    request_id: str,
    attempt_id: str,
) -> None:
    catalog = _catalog(tmp_path)
    delivery = _Delivery()
    harness = _Harness(catalog, delivery=delivery)
    event = LifeEvent(
        event_id="life-event:opportunity:1",
        sequence=1,
        occurrence_id="life-event:opportunity:1",
        timestamp=_NOW,
        source="opportunity_runtime",
        channel="life",
        event_type="opportunity.available",
        content='{"meaning":"availability_only"}',
    )
    expected_text = event.content

    with pytest.raises(RuntimeError, match="ExactDeliveryRequired"):
        await harness.runtime.record_seen(
            events=[event],
            expected_text=expected_text,
            receipt=_effective_receipt(expected_text),
            consumer_instance_id="consciousness:chat",
            final_request_id=request_id,
            final_attempt_id=attempt_id,
            perceived_at=_NOW,
        )

    assert delivery.receipts == []
