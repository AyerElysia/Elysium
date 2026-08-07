"""Coherent factory for selectable life-learning storage."""

from __future__ import annotations

from .contracts import StorageBackendRuntime, StorageRuntimeDisabled
from .learning_adapters import LocalLearningStore, MySQLLearningStore
from .learning_contracts import (
    LEARNING_WRITER_CLAIM_NAMESPACE,
    LEARNING_WRITER_CLAIM_STATE_KEY,
    LearningStores,
)
from .learning_schema import (
    ensure_learning_schema,
    verify_learning_writer_claim_guard,
)
from .models import BackendKind
from .writer_claims import SingletonWriterClaim


async def open_learning_stores(
    runtime: StorageBackendRuntime,
    *,
    initialize_schema: bool = False,
    require_database_immutability: bool = True,
    writer_claim: SingletonWriterClaim | None = None,
) -> LearningStores:
    """Build learning adapters from the already-owned coherent runtime."""

    if not runtime.enabled:
        raise StorageRuntimeDisabled(
            "learning adapters require an enabled storage runtime"
        )
    if writer_claim is not None and (
        writer_claim.namespace != LEARNING_WRITER_CLAIM_NAMESPACE
        or writer_claim.state_key != LEARNING_WRITER_CLAIM_STATE_KEY
    ):
        raise ValueError("LearningWriterClaimScopeMismatch")
    if initialize_schema:
        await ensure_learning_schema(
            runtime,
            require_database_immutability=require_database_immutability,
        )
    if writer_claim is not None:
        await verify_learning_writer_claim_guard(runtime)
    if runtime.backend == BackendKind.LOCAL:
        store = LocalLearningStore(runtime, writer_claim=writer_claim)
    else:
        store = MySQLLearningStore(runtime, writer_claim=writer_claim)
    return LearningStores(store=store)


__all__ = ["open_learning_stores"]
