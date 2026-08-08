"""Factory for multi-writer operation coordination storage."""

from __future__ import annotations

from .contracts import StorageBackendRuntime, StorageRuntimeDisabled
from .operation_adapters import SQLOperationStore
from .operation_contracts import OperationStorePort
from .runtime_schema import ensure_runtime_state_schema


async def open_operation_store(
    runtime: StorageBackendRuntime,
    *,
    initialize_schema: bool = False,
) -> OperationStorePort:
    """Open the coherent operation store for the selected generation."""

    if not runtime.enabled:
        raise StorageRuntimeDisabled("operation store requires enabled storage runtime")
    if initialize_schema:
        await ensure_runtime_state_schema(runtime)
    return SQLOperationStore(runtime)


__all__ = ["open_operation_store"]
