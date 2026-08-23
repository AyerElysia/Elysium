"""Factory for the Initiative record family owned by ProactiveAuthority."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import AsyncContextManager

from ..initiative.contracts import InitiativeRecordStorePort
from .contracts import StorageBackendRuntime, StorageRuntimeDisabled
from .initiative_adapters import SQLInitiativeRecordStore
from .models import BackendKind


async def open_initiative_record_store(
    runtime: StorageBackendRuntime,
    *,
    validate_active_actor: Callable[[str], Awaitable[bool]] | None = None,
    actor_decision_guard: (
        Callable[[str], AsyncContextManager[None]] | None
    ) = None,
) -> InitiativeRecordStorePort:
    """Attach Initiative to the same coherent runtime as AttentionThread."""

    if not runtime.enabled:
        raise StorageRuntimeDisabled(
            "initiative authority requires an enabled storage runtime"
        )
    if runtime.backend == BackendKind.MYSQL:
        validate_active_actor = None
        actor_decision_guard = None
    authority = SQLInitiativeRecordStore(
        runtime,
        validate_active_actor=validate_active_actor,
        actor_decision_guard=actor_decision_guard,
    )
    await authority.reconcile()
    return authority


__all__ = ["open_initiative_record_store"]
