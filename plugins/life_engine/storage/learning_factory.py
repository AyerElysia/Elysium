"""Coherent factory for selectable life-learning storage."""

from __future__ import annotations

from .contracts import StorageBackendRuntime, StorageRuntimeDisabled
from .learning_adapters import LocalLearningStore, MySQLLearningStore
from .learning_contracts import LearningStores
from .learning_schema import ensure_learning_schema
from .models import BackendKind


async def open_learning_stores(
    runtime: StorageBackendRuntime,
    *,
    initialize_schema: bool = False,
    require_database_immutability: bool = True,
) -> LearningStores:
    """Build learning adapters from the already-owned coherent runtime."""

    if not runtime.enabled:
        raise StorageRuntimeDisabled(
            "learning adapters require an enabled storage runtime"
        )
    if initialize_schema:
        await ensure_learning_schema(
            runtime,
            require_database_immutability=require_database_immutability,
        )
    if runtime.backend == BackendKind.LOCAL:
        store = LocalLearningStore(runtime)
    else:
        store = MySQLLearningStore(runtime)
    return LearningStores(store=store)


__all__ = ["open_learning_stores"]
