"""Factory for the coherent selectable AttentionThread authority."""

from __future__ import annotations

from dataclasses import dataclass

from ..attention_threads.contracts import (
    AttentionThreadAuthorityPort,
    InstanceFocusPort,
)
from .attention_adapters import (
    LocalAttentionThreadStore,
    MySQLAttentionThreadStore,
)
from .attention_schema import ensure_attention_thread_schema
from .contracts import StorageBackendRuntime, StorageRuntimeDisabled
from .models import BackendKind


@dataclass(frozen=True, slots=True)
class AttentionThreadStores:
    """One authority object exposed through its two explicit capabilities."""

    authority: AttentionThreadAuthorityPort
    focus: InstanceFocusPort


async def open_attention_thread_stores(
    runtime: StorageBackendRuntime,
    *,
    initialize_schema: bool = False,
    require_database_immutability: bool = True,
) -> AttentionThreadStores:
    """Attach attention to an already-owned coherent runtime."""

    if not runtime.enabled:
        raise StorageRuntimeDisabled(
            "attention adapters require an enabled storage runtime"
        )
    if initialize_schema:
        await ensure_attention_thread_schema(
            runtime,
            require_database_immutability=require_database_immutability,
        )
    if runtime.backend == BackendKind.LOCAL:
        store = LocalAttentionThreadStore(runtime)
    else:
        store = MySQLAttentionThreadStore(runtime)
    return AttentionThreadStores(authority=store, focus=store)


__all__ = ["AttentionThreadStores", "open_attention_thread_stores"]
