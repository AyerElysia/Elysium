"""Coherent factory wiring for the selectable Life Event ledger."""

from __future__ import annotations

from .contracts import StorageBackendRuntime, StorageRuntimeDisabled
from .event_adapters import LocalLifeEventStore, MySQLLifeEventStore
from .event_contracts import LifeEventStorePort
from .event_schema import ensure_life_event_schema
from .models import BackendKind


async def open_life_event_store(
    runtime: StorageBackendRuntime,
    *,
    initialize_schema: bool = False,
) -> LifeEventStorePort:
    """Build one event adapter from the already-selected backend runtime."""

    if not runtime.enabled:
        raise StorageRuntimeDisabled(
            "Life Event adapter requires an enabled storage runtime"
        )
    if initialize_schema:
        await ensure_life_event_schema(runtime)
    if runtime.backend == BackendKind.LOCAL:
        return LocalLifeEventStore(runtime)
    return MySQLLifeEventStore(runtime)


__all__ = ["open_life_event_store"]
