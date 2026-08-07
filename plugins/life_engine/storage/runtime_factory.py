"""Coherent factory for selected technical runtime state and events."""

from __future__ import annotations

from .contracts import StorageBackendRuntime, StorageRuntimeDisabled
from .models import BackendKind
from .runtime_adapters import LocalRuntimeStateStore, MySQLRuntimeStateStore
from .runtime_contracts import RuntimeStateStorePort
from .runtime_schema import ensure_runtime_state_schema


async def open_runtime_state_store(
    runtime: StorageBackendRuntime,
    *,
    initialize_schema: bool = False,
) -> RuntimeStateStorePort:
    """Build one runtime-state adapter from the coherent selected runtime."""

    if not runtime.enabled:
        raise StorageRuntimeDisabled(
            "runtime state adapter requires enabled storage runtime"
        )
    if initialize_schema:
        await ensure_runtime_state_schema(runtime)
    if runtime.backend == BackendKind.LOCAL:
        return LocalRuntimeStateStore(runtime)
    return MySQLRuntimeStateStore(runtime)


__all__ = ["open_runtime_state_store"]
