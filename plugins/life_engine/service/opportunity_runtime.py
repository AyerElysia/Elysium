"""Lifecycle binding for the canonical opportunity domain.

Only LifeEngineService constructs this binding. All storage/claims are borrowed
from its already-open runtime. Schema installation is a separate maintenance
operation, never an effect of starting a cognitive capability.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from ..opportunity.catalog import CapabilityCatalog
from ..opportunity.native_dispatch import (
    NativeCapabilityDispatch,
    NativeCapabilityExecutor,
)
from ..opportunity.registry import CapabilityRuntimeRegistry
from ..opportunity.runtime import OpportunityRuntime
from ..storage.opportunity_contracts import (
    OPPORTUNITY_SCHEDULER_CLAIM_NAMESPACE,
    OPPORTUNITY_SCHEDULER_CLAIM_STATE_KEY,
)
from ..storage.opportunity_factory import open_opportunity_stores
from ..storage.writer_claims import SingletonWriterClaimConflict


async def attach_opportunity_runtime(service: Any) -> OpportunityRuntime:
    """Attach one domain to one existing service-owned runtime, without seeding."""
    storage = service.storage_runtime
    if storage is None or not service._selectable_storage_enabled:
        # The legacy proactive SQLite and legacy Life Event SQLite are two
        # different authorities. They cannot satisfy exact publication proof.
        raise RuntimeError("OpportunitySelectedStorageRequired")
    # First verify schema before acquiring a lease; a missing migration must not
    # leave a managed claim behind during an otherwise unrelated startup.
    await open_opportunity_stores(storage, initialize_schema=False)
    lease_seconds = service._storage_factory_settings.authority_lease_seconds
    try:
        claim = await storage.acquire_singleton_writer(
            namespace=OPPORTUNITY_SCHEDULER_CLAIM_NAMESPACE,
            state_key=OPPORTUNITY_SCHEDULER_CLAIM_STATE_KEY,
            owner_instance_id=service._storage_writer_instance_id,
            lease_seconds=lease_seconds,
        )
    except SingletonWriterClaimConflict:
        claim = None
    stores = await open_opportunity_stores(
        storage,
        initialize_schema=False,
        writer_claim=claim,
        validate_active_actor=None,
        actor_decision_guard=None,
    )
    catalog = CapabilityCatalog()
    await asyncio.to_thread(
        catalog.discover,
        Path(__file__).resolve().parents[1] / "opportunity" / "capabilities",
    )
    dispatch = NativeCapabilityDispatch(
        opportunity_runtime=lambda: service._opportunity_runtime
    )
    registry = CapabilityRuntimeRegistry(
        catalog,
        dispatch=dispatch,
        factories={
            item.capability_id: NativeCapabilityExecutor
            for item in catalog.list_descriptors()
        },
    )

    async def check_actor(actor: str) -> None:
        if not await service._validate_learning_decision_actor(actor):
            raise PermissionError("OpportunityActiveActorRequired")

    runtime = OpportunityRuntime(
        stores,
        catalog,
        registry,
        check_actor=check_actor,
        publish_event=service._get_life_event_store().append,
        wake=service._opportunity_wake_event.set,
        lifecycle=service._apply_opportunity_capability_state,
    )
    return runtime
