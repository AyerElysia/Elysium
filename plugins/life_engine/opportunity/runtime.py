"""Subject-governed capability lifecycle over injected durable opportunity ports.

This service never opens storage, runs Skill text, selects a workflow for the
subject, or equates publishing an opportunity with accepting it.  The owning
LifeEngineService supplies existing executors, event publication and wake-up.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import asdict, dataclass
from typing import Any

from src.kernel.storage import canonical_json

from ..service.event_bus import LifeEvent
from ..storage.opportunity_contracts import (
    OpportunityAction,
    OpportunityDeliveryReceipt,
    OpportunityHistoryFamily,
    OpportunityRegistrationCommand,
    OpportunitySchedule,
    OpportunityStatus,
    OpportunityStores,
    ProviderAction,
    ProviderBindingCommand,
    ProviderStatus,
    WorkflowVersionCommand,
)
from .catalog import CapabilityCatalog, CapabilityDescriptor
from .registry import CapabilityRuntimeRegistry


@dataclass(frozen=True, slots=True)
class OpportunityCaller:
    actor_consciousness_instance_id: str
    source_instance_id: str
    source_occurrence_id: str
    decision_occurrence_id: str
    occurred_at: str
    caller_tool: Any = None

    def attribution(self) -> dict[str, Any]:
        return {
            "occurrence_id": self.decision_occurrence_id,
            "actor_consciousness_instance_id": self.actor_consciousness_instance_id,
            "source_instance_id": self.source_instance_id,
            "source_occurrence_ids": (self.source_occurrence_id,),
            "causation_occurrence_id": self.source_occurrence_id,
            "occurred_at": self.occurred_at,
        }


class OpportunityRuntime:
    """One runtime facade, with durable authority checked on every execution.

    Local executor reconciliation is deliberately separate from the authority
    decision: a failed close/install cannot erase a committed subject decision.
    No factory is installed by discovery, and a restart never seeds defaults.
    """

    def __init__(
        self,
        stores: OpportunityStores,
        catalog: CapabilityCatalog,
        registry: CapabilityRuntimeRegistry,
        *,
        check_actor: Callable[[str], Awaitable[None]],
        publish_event: Callable[[LifeEvent], Awaitable[LifeEvent]],
        wake: Callable[[], None],
        lifecycle: Callable[[str, ProviderStatus], Awaitable[None]],
    ) -> None:
        self.stores = stores
        self.catalog = catalog
        self.registry = registry
        self._check_actor = check_actor
        self._publish_event = publish_event
        self._wake = wake
        self._lifecycle = lifecycle
        self._locks: dict[str, asyncio.Lock] = {}
        self._pump_lock = asyncio.Lock()
        self._closed = False
        self._close_complete = False
        self._quiesced = False
        self._errors: dict[str, str] = {}

    def _lock(self, capability_id: str) -> asyncio.Lock:
        # Only ids in a bounded, discovered catalog allocate execution locks.
        self.catalog.require(capability_id)
        return self._locks.setdefault(capability_id, asyncio.Lock())

    def _require_open(self) -> None:
        if self._closed:
            raise RuntimeError("OpportunityRuntimeClosed")

    async def _reconcile(self, capability_id: str) -> None:
        binding = await self.stores.authority.get_provider(capability_id)
        state = await self.registry.state(capability_id)
        if binding is None or binding.status == ProviderStatus.UNINSTALLED:
            if state.installed:
                await self.registry.uninstall(capability_id)
            await self._lifecycle(capability_id, ProviderStatus.UNINSTALLED)
            return
        descriptor = self.catalog.require(capability_id)
        if (
            descriptor.package_version != binding.descriptor_version
            or descriptor.package_sha256 != binding.descriptor_sha256
        ):
            if state.installed:
                await self.registry.disable(capability_id)
            await self._lifecycle(capability_id, ProviderStatus.PAUSED)
            raise RuntimeError("OpportunityCapabilityPackageChanged")
        await self.registry.install(capability_id)
        if binding.status == ProviderStatus.ENABLED:
            await self._lifecycle(capability_id, ProviderStatus.ENABLED)
            await self.registry.enable(capability_id)
        else:
            await self.registry.disable(capability_id)
            await self._lifecycle(capability_id, ProviderStatus.PAUSED)

    async def restore(self) -> None:
        """Restore exact persisted decisions, never adopt package templates."""
        self._require_open()
        for descriptor in self.catalog.list_descriptors():
            async with self._lock(descriptor.capability_id):
                try:
                    await self._reconcile(descriptor.capability_id)
                except (OSError, RuntimeError, ValueError) as exc:
                    self._errors[descriptor.capability_id] = type(exc).__name__

    async def manage(
        self,
        action: str,
        target_id: str,
        expected_revision: int,
        arguments: Mapping[str, Any],
        reason: str,
        caller: OpportunityCaller,
    ) -> dict[str, Any]:
        self._require_open()
        await self._check_actor(caller.actor_consciousness_instance_id)
        if isinstance(expected_revision, bool) or expected_revision < 0:
            raise ValueError("OpportunityExpectedRevisionRequired")
        if action.startswith("capability."):
            return await self._manage_provider(
                action,
                target_id,
                expected_revision,
                arguments,
                reason,
                caller,
            )
        if action == "workflow.replace":
            _require_argument_names(
                arguments, {"capability_id", "content", "content_sha256"}
            )
            provider_id = str(arguments["capability_id"])
            self.catalog.require(provider_id)
            text = arguments["content"]
            if not isinstance(text, str):
                raise ValueError("WorkflowContentMustBeExactUTF8Text")
            content = text.encode("utf-8")
            # Hash the exact subject-supplied bytes; the model need not perform
            # cryptography. If it supplies a hash, the storage contract checks
            # it. This never loads or adopts an engineering template.
            result = await self.stores.authority.append_workflow(
                WorkflowVersionCommand(
                    **caller.attribution(),
                    workflow_id=target_id,
                    provider_id=provider_id,
                    expected_revision=expected_revision,
                    schema_version=1,
                    content_bytes=content,
                    content_sha256=(
                        str(arguments["content_sha256"])
                        if "content_sha256" in arguments
                        else hashlib.sha256(content).hexdigest()
                    ),
                    reason=reason,
                )
            )
            value = asdict(result)
            value.pop("content_bytes")
            value.pop("reason")
            return {
                "authority_committed": True,
                "workflow": value,
                "binding_changed": False,
            }
        return await self._manage_registration(
            action,
            target_id,
            expected_revision,
            arguments,
            reason,
            caller,
        )

    async def _manage_provider(
        self,
        action: str,
        target_id: str,
        expected_revision: int,
        arguments: Mapping[str, Any],
        reason: str,
        caller: OpportunityCaller,
    ) -> dict[str, Any]:
        descriptor = self.catalog.require(target_id)
        _require_argument_names(
            arguments, {"workflow_id", "workflow_revision", "workflow_sha256"}
        )
        operation = action.removeprefix("capability.")
        provider_action = ProviderAction(
            "install" if operation == "reinstall" else operation
        )
        async with self._lock(target_id):
            previous = await self.stores.authority.get_provider(target_id)
            if operation == "reinstall" and (
                previous is None or previous.status != ProviderStatus.UNINSTALLED
            ):
                raise ValueError("ReinstallRequiresUninstalledProvider")
            workflow = {}
            for name in ("workflow_id", "workflow_revision", "workflow_sha256"):
                if name in arguments:
                    workflow[name] = arguments[name]
                elif previous is not None and provider_action not in {
                    ProviderAction.INSTALL,
                    ProviderAction.BIND_WORKFLOW,
                }:
                    workflow[name] = getattr(previous, name)
                else:
                    raise ValueError("ExplicitWorkflowReferenceRequired")
            result = await self.stores.authority.manage_provider(
                ProviderBindingCommand(
                    **caller.attribution(),
                    provider_id=target_id,
                    action=provider_action,
                    expected_revision=expected_revision,
                    descriptor_version=descriptor.package_version,
                    descriptor_sha256=descriptor.package_sha256,
                    **workflow,
                    reason=reason,
                )
            )
            error_type = ""
            try:
                await self._reconcile(target_id)
                self._errors.pop(target_id, None)
            except (OSError, RuntimeError, ValueError) as exc:
                error_type = type(exc).__name__
                self._errors[target_id] = error_type
            return {
                "authority_committed": True,
                "provider": asdict(result),
                "runtime_applied": not error_type,
                "runtime_error_type": error_type,
                "history_deleted": False,
            }

    async def _manage_registration(
        self,
        action: str,
        target_id: str,
        expected_revision: int,
        arguments: Mapping[str, Any],
        reason: str,
        caller: OpportunityCaller,
    ) -> dict[str, Any]:
        operation = action.removeprefix("opportunity.")
        if not action.startswith("opportunity."):
            raise ValueError("UnknownOpportunityCommand")
        operation = "configure" if operation in {"schedule", "snooze"} else operation
        registration_action = OpportunityAction(operation)
        previous = await self.stores.authority.get_opportunity(target_id)
        values: dict[str, Any] = {}
        fields = (
            "provider_id",
            "referent_kind",
            "referent_id",
            "referent_revision",
            "referent_sha256",
            "workflow_id",
            "workflow_revision",
            "workflow_sha256",
            "schedule",
            "first_due_at",
            "interval_seconds",
        )
        _require_argument_names(arguments, set(fields))
        for name in fields:
            if name in arguments:
                values[name] = arguments[name]
            elif previous is not None:
                values[name] = getattr(previous, name)
            elif name in {"referent_revision", "interval_seconds"}:
                values[name] = 0
            elif name == "first_due_at":
                values[name] = ""
            elif name == "schedule":
                values[name] = OpportunitySchedule.MANUAL
            else:
                raise ValueError(f"OpportunityFieldRequired:{name}")
        self.catalog.require(values["provider_id"])
        result = await self.stores.authority.decide_opportunity(
            OpportunityRegistrationCommand(
                **caller.attribution(),
                opportunity_id=target_id,
                action=registration_action,
                expected_revision=expected_revision,
                **values,
                reason=reason,
            )
        )
        return {"authority_committed": True, "opportunity": asdict(result)}

    async def call(
        self,
        capability_id: str,
        operation: str,
        arguments: Mapping[str, Any],
        caller: OpportunityCaller,
    ) -> Any:
        self._require_open()
        await self._check_actor(caller.actor_consciousness_instance_id)
        binding = await self.stores.authority.get_provider(capability_id)
        if binding is None or binding.status != ProviderStatus.ENABLED:
            raise PermissionError("OpportunityCapabilityNotEnabled")
        descriptor = self.catalog.require(capability_id)
        if descriptor.package_sha256 != binding.descriptor_sha256:
            raise RuntimeError("OpportunityCapabilityPackageChanged")
        # Registry owns execution admission and cancellation. Holding the
        # management lock over an LLM call would prevent pause/uninstall and
        # deadlock a self-awakening capability managing its own registration.
        return await self.registry.execute(
            capability_id,
            operation,
            arguments,
            caller_context=caller,
        )

    async def query(
        self,
        *,
        resource: str,
        record_id: str = "",
        continuation: str = "",
        offset_bytes: int = 0,
        max_bytes: int = 8192,
        limit: int = 20,
        revision: int = 0,
        operation: str = "",
        action: str = "",
        family: str = "registration",
        actor_consciousness_instance_id: str,
    ) -> dict[str, Any]:
        """Bounded progressive disclosure, never an automatic workflow choice."""
        self._require_open()
        await self._check_actor(actor_consciousness_instance_id)
        if not 512 <= max_bytes <= 32768 or not 1 <= limit <= 100:
            raise ValueError("OpportunityQueryBudgetOutOfRange")
        # Conservative serialized-byte headroom; every final result is checked.
        page_limit = min(limit, max(1, (max_bytes - 512) // 2200))
        authority = self.stores.authority
        if resource == "protocol":
            from .protocol import describe_protocol

            result = describe_protocol(record_id)
        elif resource == "operation_schema":
            from .native_dispatch import describe_operation_schema

            descriptor = self.catalog.require(record_id)
            if not descriptor.declares_operation(operation):
                raise PermissionError("OpportunityOperationNotDeclared")
            schema = describe_operation_schema(record_id, operation, action=action)
            result = _artifact_chunk(
                canonical_json(schema).encode("utf-8"),
                offset_bytes,
                max(4, (max_bytes - 1024) // 6),
            )
            result.update(
                {
                    "capability_id": record_id,
                    "operation": operation,
                    "action": action,
                    "encoding": "canonical_json_utf8",
                    "origin": "engineering_operation_schema",
                    "grants_permission": False,
                }
            )
        elif resource == "catalog":
            descriptors = [
                item
                for item in self.catalog.list_descriptors()
                if item.capability_id > continuation
            ]
            selected = descriptors[:page_limit]
            result = {
                "items": [self.describe(item) for item in selected],
                "continuation": selected[-1].capability_id
                if len(descriptors) > page_limit
                else "",
            }
        elif resource == "providers":
            result = asdict(
                await authority.page_providers(
                    limit=page_limit,
                    continuation=continuation,
                )
            )
        elif resource == "opportunities":
            result = asdict(
                await authority.page_opportunities(
                    limit=page_limit,
                    continuation=continuation,
                )
            )
        elif resource == "capability":
            binding = await authority.get_provider(record_id)
            result = {
                "package": self.describe(self.catalog.require(record_id)),
                "binding": asdict(binding) if binding is not None else None,
            }
        elif resource == "opportunity":
            registration = await authority.get_opportunity(record_id)
            result = {"opportunity": asdict(registration) if registration else None}
        elif resource in {"manual", "default_skill"}:
            descriptor = self.catalog.require(record_id)
            artifact = (
                descriptor.technical_manual
                if resource == "manual"
                else descriptor.default_skill_template
            )
            result = _artifact_chunk(
                artifact.content_bytes, offset_bytes, max(1, (max_bytes - 768) // 6)
            )
            result.update(
                {
                    "capability_id": record_id,
                    "artifact": resource,
                    "origin": "engineering_package",
                    "subject_adopted": False,
                }
            )
        elif resource == "workflow":
            if revision <= 0:
                raise ValueError("ExactWorkflowRevisionRequired")
            result = asdict(
                await authority.read_workflow_chunk(
                    record_id,
                    revision,
                    offset_bytes=offset_bytes,
                    max_bytes=max(4, (max_bytes - 768) // 6),
                )
            )
        elif resource == "history":
            result = asdict(
                await authority.page_history(
                    OpportunityHistoryFamily(family),
                    aggregate_id=record_id,
                    limit=page_limit,
                    continuation=continuation,
                )
            )
        elif resource == "history_reason":
            result = asdict(
                await authority.read_history_reason_chunk(
                    OpportunityHistoryFamily(family),
                    record_id,
                    offset_bytes=offset_bytes,
                    max_bytes=max(4, (max_bytes - 768) // 6),
                )
            )
        elif resource == "deliveries":
            if self.stores.delivery is None:
                raise RuntimeError("OpportunityDeliveryStoreUnavailable")
            result = {
                "items": [
                    asdict(item)
                    for item in await self.stores.delivery.list_deliveries(
                        record_id,
                        limit=page_limit,
                    )
                ],
                "view": "bounded_recent_receipts",
                "limit": page_limit,
            }
        elif resource == "uninstall_impact":
            descriptor = self.catalog.require(record_id)
            result = {
                "capability_id": descriptor.capability_id,
                "stops": [
                    "new_availability",
                    "new_capability_calls",
                    "owned_cognitive_work",
                ],
                "preserves": [
                    "immutable_history",
                    "subject_documents",
                    "adopted_skills",
                ],
                "startup_reinstall": False,
                "authority_changed": False,
            }
        elif resource == "health":
            result = await self.health()
        else:
            raise ValueError("OpportunityQueryResourceUnknown")
        if len(canonical_json(result).encode("utf-8")) > max_bytes:
            raise ValueError("OpportunityQueryBudgetTooSmall:increase_max_bytes")
        return result

    async def record_seen(
        self,
        *,
        events: list[LifeEvent],
        expected_text: str,
        receipt: Any,
        consumer_instance_id: str,
        final_request_id: str,
        final_attempt_id: str,
        perceived_at: str,
    ) -> int:
        """Append perception facts only for complete availability in exact text.

        The expected text is the sealed producer text, not reconstructed from
        the receipt. Omitted/excerpt-only events remain pending. No body is
        persisted in the delivery ledger.
        """
        self._require_open()
        if self.stores.delivery is None:
            raise RuntimeError("OpportunityDeliveryStoreUnavailable")
        encoded = expected_text.encode("utf-8")
        digest = hashlib.sha256(encoded).hexdigest()
        if (
            receipt is None
            or not receipt.exact_present
            or receipt.expected_utf8_bytes != len(encoded)
            or receipt.effective_utf8_bytes != len(encoded)
            or receipt.expected_sha256 != digest
            or receipt.effective_sha256 != digest
            or not final_request_id
            or not final_attempt_id
        ):
            raise RuntimeError("OpportunityExactDeliveryRequired")
        committed = 0
        for event in events:
            if event.event_type != "opportunity.available":
                continue
            if not event.content or expected_text.count(event.content) != 1:
                continue
            event_id = event.occurrence_id or event.event_id
            publication = await self.stores.delivery.get_publication(event_id)
            if publication is None:
                raise RuntimeError("OpportunityPublicationEvidenceMissing")
            receipt_id = (
                "opportunity:seen:"
                + hashlib.sha256(
                    f"{publication.occurrence_id}\0{consumer_instance_id}\0{final_attempt_id}".encode()
                ).hexdigest()
            )
            await self.stores.delivery.commit_exact(
                OpportunityDeliveryReceipt(
                    receipt_id=receipt_id,
                    occurrence_id=publication.occurrence_id,
                    life_event_occurrence_id=event_id,
                    consumer_consciousness_instance_id=consumer_instance_id,
                    context_delivery_id=receipt.delivery_id,
                    final_request_id=final_request_id,
                    final_attempt_id=final_attempt_id,
                    exact_present=True,
                    expected_bytes=len(encoded),
                    effective_bytes=len(encoded),
                    expected_sha256=digest,
                    effective_sha256=digest,
                    perceived_at=perceived_at,
                )
            )
            committed += 1
        return committed

    @staticmethod
    def describe(descriptor: CapabilityDescriptor) -> dict[str, Any]:
        return {
            "capability_id": descriptor.capability_id,
            "package_version": descriptor.package_version,
            "package_sha256": descriptor.package_sha256,
            "removability": descriptor.removability,
            "provider_kind": descriptor.provider_kind,
            "operations": list(descriptor.operations),
            "dependencies": list(descriptor.dependencies),
            "manual_sha256": descriptor.manual_sha256,
            "manual_bytes": descriptor.technical_manual.utf8_bytes,
            "default_skill_sha256": descriptor.default_skill_sha256,
            "default_skill_bytes": descriptor.default_skill_template.utf8_bytes,
            "default_skill_adopted": False,
        }

    async def pump(self, *, limit: int = 32) -> int:
        """Materialize and publish bounded availability, never execute a Skill."""
        self._require_open()
        if self._quiesced or self.stores.scheduler is None:
            # Lack of scheduler ownership stops scheduling, not perception of
            # already-published facts from the one authoritative scheduler.
            if self.stores.delivery is not None:
                if await self.stores.delivery.awaiting_delivery(limit=1):
                    self._wake()
            return 0
        if not 1 <= limit <= 100:
            raise ValueError("OpportunityPumpLimitOutOfRange")
        async with self._pump_lock:
            scheduler = self.stores.scheduler
            await scheduler.reconcile_deliveries(limit=limit)
            await scheduler.materialize_due(limit=limit)
            published = 0
            for publication in await scheduler.pending_publications(limit=limit):
                occurrence = await scheduler.get_occurrence(publication.occurrence_id)
                if occurrence is None:
                    raise RuntimeError("OpportunityOutboxEvidenceMissing")
                binding = await self.stores.authority.get_provider(
                    occurrence.provider_id
                )
                registration = await self.stores.authority.get_opportunity(
                    occurrence.opportunity_id
                )
                if (
                    binding is None
                    or binding.status != ProviderStatus.ENABLED
                    or registration is None
                    or registration.status != OpportunityStatus.OPEN
                ):
                    continue
                payload = asdict(occurrence)
                payload["schema_version"] = 1
                payload["meaning"] = "availability_only"
                content = canonical_json(payload)
                event = LifeEvent(
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
                    metadata={
                        "opportunity_occurrence_id": occurrence.occurrence_id,
                        "capability_id": occurrence.provider_id,
                    },
                )
                # At-least-once outbox retries reuse the exact event identity/payload.
                await self._publish_event(event)
                await scheduler.mark_published(
                    publication.outbox_id,
                    expected_revision=publication.revision,
                    life_event_sha256=hashlib.sha256(
                        content.encode("utf-8")
                    ).hexdigest(),
                )
                published += 1
            awaiting = (
                await self.stores.delivery.awaiting_delivery(limit=1)
                if self.stores.delivery is not None
                else ()
            )
            if published or awaiting:
                self._wake()
            return published

    async def health(self) -> dict[str, Any]:
        scheduler = (
            await self.stores.scheduler.scheduler_health_snapshot()
            if self.stores.scheduler is not None
            else {"status": "disabled", "reason": "scheduler_owner_elsewhere"}
        )
        if self._quiesced:
            scheduler = {
                **scheduler,
                "status": "degraded",
                "reason": "scheduler_quiesced",
            }
        return {
            "component": "opportunity_runtime",
            "status": "closed"
            if self._closed
            else "degraded"
            if self._errors or self._quiesced
            else "ready",
            "scheduler_owner": self.stores.scheduler is not None and not self._quiesced,
            "delivery_available": self.stores.delivery is not None,
            "runtime_errors": dict(self._errors),
            "authority": await self.stores.authority.health_snapshot(),
            "scheduler": scheduler,
            "registry": await self.registry.health_snapshot(),
        }

    def quiesce_scheduler(self) -> None:
        """Confirmed claim loss stops scheduling, not immutable evidence intake."""
        self._quiesced = True

    async def close(self) -> None:
        if self._close_complete:
            return
        self._closed = True
        self._quiesced = True
        errors: list[Exception] = []
        for descriptor in self.catalog.list_descriptors():
            try:
                async with asyncio.timeout(15.0), self._lock(descriptor.capability_id):
                    state = await self.registry.state(descriptor.capability_id)
                    if state.installed:
                        await self.registry.disable(descriptor.capability_id)
                        await self._lifecycle(
                            descriptor.capability_id, ProviderStatus.PAUSED
                        )
                        await self.registry.uninstall(descriptor.capability_id)
            except Exception as exc:
                self._errors[descriptor.capability_id] = type(exc).__name__
                errors.append(exc)
        # The injected stores and shared StorageBackendRuntime are never closed here.
        if errors:
            raise ExceptionGroup("opportunity capability shutdown failed", errors)
        self._close_complete = True


def _require_argument_names(arguments: Mapping[str, Any], allowed: set[str]) -> None:
    if any(name not in allowed for name in arguments):
        raise ValueError("OpportunityUnknownArgument:query_protocol")


def _artifact_chunk(content: bytes, offset: int, budget: int) -> dict[str, Any]:
    if isinstance(offset, bool) or not 0 <= offset <= len(content):
        raise ValueError("ArtifactByteOffsetOutOfRange")
    try:
        content[:offset].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("ArtifactOffsetMustBeUTF8Boundary") from exc
    end = min(len(content), offset + max(4, budget))
    while end > offset:
        try:
            text = content[offset:end].decode("utf-8")
            break
        except UnicodeDecodeError:
            end -= 1
    else:
        text = ""
    return {
        "content": text,
        "offset_bytes": offset,
        "next_offset_bytes": end,
        "total_bytes": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
        "complete": end == len(content),
    }
