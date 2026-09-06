"""Trusted lifecycle and attribution gate for optional Learning operations.

The opportunity package is an execution permission boundary, not merely a
prompt hint.  Legacy Learning tools therefore resolve through this adapter on
every call.  A paused or uninstalled capability cannot keep using a stale
``LearningScheduler`` reference, and a read-only selected projector never
falls back to workspace JSON state.
"""

from __future__ import annotations

import hashlib
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncIterator

from src.app.plugin_system.base import BaseTool

from ..storage.opportunity_contracts import ProviderBinding, ProviderStatus
from .selectable import LearningMutationContext

LEARNING_CAPABILITY_ID = "life.learning"


class LearningCapabilityUnavailable(RuntimeError):
    """The subject-managed Learning capability is not executable now."""


class LearningCapabilityReadOnly(RuntimeError):
    """The Learning projection owner is absent or has been quiesced."""


@dataclass(frozen=True, slots=True)
class LearningOpportunityCapability:
    """One call-scoped, revalidated view of the Learning capability."""

    tool: BaseTool
    service: Any
    scheduler: Any
    actor_consciousness_instance_id: str
    shared_state: Any | None
    opportunity_runtime: Any | None
    binding: ProviderBinding | None

    @classmethod
    async def bind(
        cls,
        tool: BaseTool,
        *,
        require_writable: bool = False,
    ) -> LearningOpportunityCapability:
        """Revalidate actor, durable lifecycle and local executor state.

        When the opportunity runtime is disabled by configuration, the legacy
        service-owned scheduler remains usable for compatibility.  Once the
        opportunity runtime is configured, every call requires the durable
        provider binding *and* its local registry executor to be enabled.
        """

        from ..service.registry import get_life_engine_service

        service = get_life_engine_service()
        if service is None:
            raise LearningCapabilityUnavailable("LifeEngineServiceUnavailable")

        stream_id = str(tool.get_current_stream_id() or "").strip()
        try:
            actor = str(service.resolve_consciousness_instance(stream_id) or "").strip()
        except Exception as exc:
            raise LearningCapabilityUnavailable(
                "LearningCapabilityActorResolutionFailed"
            ) from exc
        instance = service.consciousness_registry.get(actor) if actor else None
        if instance is None or not instance.is_active:
            raise PermissionError("LearningCapabilityActiveActorRequired")

        runtime = getattr(service, "_opportunity_runtime", None)
        managed = bool(getattr(service, "opportunity_managed", False))
        binding: ProviderBinding | None = None
        if runtime is None:
            if managed:
                raise LearningCapabilityUnavailable("OpportunityRuntimeNotReady")
        else:
            from ..opportunity.execution_context import (
                current_capability_execution,
            )

            if current_capability_execution() != LEARNING_CAPABILITY_ID:
                raise LearningCapabilityUnavailable(
                    "LearningCapabilityCallRequired:use nucleus_capability_call"
                )
            binding = await runtime.stores.authority.get_provider(
                LEARNING_CAPABILITY_ID
            )
            state = await runtime.registry.state(LEARNING_CAPABILITY_ID)
            if (
                binding is None
                or binding.status is not ProviderStatus.ENABLED
                or not state.installed
                or not state.enabled
            ):
                raise LearningCapabilityUnavailable(
                    "LearningCapabilityNotInstalledOrEnabled"
                )

        shared_state = getattr(service, "_shared_learning_state", None)
        if runtime is not None and (
            shared_state is None
            or not bool(getattr(shared_state, "initialized", False))
            or bool(getattr(shared_state, "closed", False))
        ):
            raise LearningCapabilityUnavailable("SharedLearningStateUnavailable")
        scheduler = getattr(service, "_learning_scheduler", None)
        if scheduler is None:
            raise LearningCapabilityUnavailable("LearningSchedulerUnavailable")
        if shared_state is None and (
            not hasattr(scheduler, "store") or not hasattr(scheduler, "skill_store")
        ):
            raise LearningCapabilityUnavailable("LearningProjectionViewUnavailable")
        if require_writable:
            if shared_state is not None:
                if not bool(getattr(shared_state, "writable", False)):
                    raise LearningCapabilityReadOnly(
                        "LearningProjectionOwnerUnavailable"
                    )
            else:
                persistence = getattr(scheduler, "_selected_persistence", None)
                if persistence is not None and not bool(persistence.writable):
                    raise LearningCapabilityReadOnly(
                        "LearningProjectionOwnerUnavailable"
                    )
            if bool(getattr(scheduler, "_projector_quiesced", False)):
                raise LearningCapabilityReadOnly("LearningProjectorQuiesced")

        return cls(
            tool=tool,
            service=service,
            scheduler=scheduler,
            actor_consciousness_instance_id=actor,
            shared_state=shared_state,
            opportunity_runtime=runtime,
            binding=binding,
        )

    @property
    def store(self) -> Any:
        if self.shared_state is not None:
            return self.shared_state.store
        return self.scheduler.store

    @property
    def skill_store(self) -> Any:
        if self.shared_state is not None:
            return self.shared_state.skill_store
        return self.scheduler.skill_store

    @property
    def decision_ledger(self) -> Any | None:
        if self.shared_state is not None:
            return self.shared_state.decision_ledger
        return getattr(self.scheduler, "decision_ledger", None)

    @asynccontextmanager
    async def mutation_context(
        self,
        operation_id: str,
        *,
        reason: str = "",
    ) -> AsyncIterator[None]:
        """Attribute one subject-chosen operation without granting permission."""

        shared = self.shared_state
        if shared is None:
            yield
            return
        shared.require_writable()
        from ..opportunity.tools import _source_instance, _source_occurrence

        source_occurrence_id = _source_occurrence(self.tool)
        source_instance_id = _source_instance(
            self.tool,
            self.actor_consciousness_instance_id,
        )
        subject_revision = str(await self.scheduler.current_subject_revision()).strip()
        reason_sha256 = (
            hashlib.sha256(str(reason).encode("utf-8")).hexdigest() if reason else ""
        )
        context = LearningMutationContext(
            source=f"subject.learning_opportunity.{operation_id}",
            actor_consciousness_instance_id=(self.actor_consciousness_instance_id),
            subject_revision=subject_revision,
            provenance={
                "capability_id": LEARNING_CAPABILITY_ID,
                "operation_id": str(operation_id),
                "source_occurrence_id": source_occurrence_id,
                "source_instance_id": source_instance_id,
                "tool_call_id": str(getattr(self.tool, "_tool_call_id", "") or ""),
                "reason_sha256": reason_sha256,
            },
        )
        with shared.mutation_context(context):
            yield

    async def read_selected_workflow(
        self,
        *,
        offset_bytes: int = 0,
        max_bytes: int = 8192,
    ) -> dict[str, Any]:
        """Read the exact subject-selected workflow; never adopt a template."""

        runtime = self.opportunity_runtime
        binding = self.binding
        if runtime is None or binding is None:
            raise LearningCapabilityUnavailable("SelectedLearningWorkflowUnavailable")
        chunk = await runtime.stores.authority.read_workflow_chunk(
            binding.workflow_id,
            binding.workflow_revision,
            offset_bytes=int(offset_bytes),
            max_bytes=max(1, min(32768, int(max_bytes))),
        )
        if chunk.content_sha256 != binding.workflow_sha256:
            raise LearningCapabilityUnavailable("LearningWorkflowBindingMismatch")
        return {
            "action": "help",
            "skill": "learning",
            "source": "subject_selected_opportunity_workflow",
            "subject_adopted": True,
            "workflow_id": chunk.workflow_id,
            "workflow_revision": chunk.revision,
            "workflow_sha256": chunk.content_sha256,
            "offset_bytes": chunk.offset_bytes,
            "next_offset_bytes": chunk.next_offset_bytes,
            "original_bytes": chunk.total_bytes,
            "truncated": not chunk.complete,
            "content": chunk.content,
        }


__all__ = [
    "LEARNING_CAPABILITY_ID",
    "LearningCapabilityReadOnly",
    "LearningCapabilityUnavailable",
    "LearningOpportunityCapability",
]
