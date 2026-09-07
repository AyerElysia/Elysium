"""Factory for opportunity stores attached to one coherent runtime."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager

from .contracts import StorageBackendRuntime, StorageRuntimeDisabled
from .models import BackendKind
from .opportunity_adapters import LocalOpportunityStore, MySQLOpportunityStore
from .opportunity_contracts import (
    OPPORTUNITY_SCHEDULER_CLAIM_NAMESPACE,
    OPPORTUNITY_SCHEDULER_CLAIM_STATE_KEY,
    OpportunityStores,
)
from .opportunity_schema import ensure_opportunity_schema, verify_opportunity_schema
from .runtime_schema import ensure_runtime_state_schema
from .writer_claims import SingletonWriterClaim


async def open_opportunity_stores(
    runtime: StorageBackendRuntime,
    *,
    initialize_schema: bool = False,
    require_database_immutability: bool = True,
    writer_claim: SingletonWriterClaim | None = None,
    validate_active_actor: Callable[[str], Awaitable[bool]] | None = None,
    actor_decision_guard: (
        Callable[[str], AbstractAsyncContextManager[None]] | None
    ) = None,
) -> OpportunityStores:
    """Attach opportunity authority to the service-owned storage runtime.

    Business startup leaves ``initialize_schema`` false and therefore only
    verifies readiness. Schema creation belongs to an explicit migration.
    """

    if not runtime.enabled:
        raise StorageRuntimeDisabled(
            "opportunity adapters require an enabled storage runtime"
        )
    if writer_claim is not None and (
        writer_claim.namespace != OPPORTUNITY_SCHEDULER_CLAIM_NAMESPACE
        or writer_claim.state_key != OPPORTUNITY_SCHEDULER_CLAIM_STATE_KEY
    ):
        raise ValueError("OpportunitySchedulerClaimScopeMismatch")
    if initialize_schema:
        await ensure_runtime_state_schema(runtime)
        await ensure_opportunity_schema(
            runtime,
            require_database_immutability=require_database_immutability,
        )
    else:
        await verify_opportunity_schema(
            runtime,
            require_database_immutability=require_database_immutability,
            require_scheduler_claim_guard=writer_claim is not None,
        )
    if runtime.backend == BackendKind.MYSQL:
        if validate_active_actor is not None or actor_decision_guard is not None:
            raise ValueError(
                "MySQL opportunity actor validation must use transactional Presence"
            )
        store = MySQLOpportunityStore(runtime, writer_claim=writer_claim)
    else:
        store = LocalOpportunityStore(
            runtime,
            writer_claim=writer_claim,
            validate_active_actor=validate_active_actor,
            actor_decision_guard=actor_decision_guard,
        )
    return OpportunityStores(
        authority=store,
        scheduler=store if writer_claim is not None else None,
        delivery=store,
    )


__all__ = ["open_opportunity_stores"]
