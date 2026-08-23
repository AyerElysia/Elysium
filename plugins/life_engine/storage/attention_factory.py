"""Factory for the coherent selectable AttentionThread authority."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import AsyncContextManager

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
from .runtime_schema import ensure_runtime_state_schema


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
    validate_active_actor: Callable[[str], Awaitable[bool]] | None = None,
    actor_decision_guard: (
        Callable[[str], AsyncContextManager[None]] | None
    ) = None,
) -> AttentionThreadStores:
    """Attach attention to an already-owned coherent runtime."""

    if not runtime.enabled:
        raise StorageRuntimeDisabled(
            "attention adapters require an enabled storage runtime"
        )
    if initialize_schema:
        await ensure_runtime_state_schema(runtime)
        await ensure_attention_thread_schema(
            runtime,
            require_database_immutability=require_database_immutability,
        )
    if runtime.backend == BackendKind.LOCAL:
        store = LocalAttentionThreadStore(
            runtime,
            validate_active_actor=validate_active_actor,
            actor_decision_guard=actor_decision_guard,
        )
    else:
        if validate_active_actor is not None or actor_decision_guard is not None:
            raise ValueError(
                "MySQL attention actor validation must use transactional Presence"
            )
        store = MySQLAttentionThreadStore(runtime)
    return AttentionThreadStores(authority=store, focus=store)


__all__ = ["AttentionThreadStores", "open_attention_thread_stores"]
