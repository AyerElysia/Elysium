"""Coherent factory wiring for selectable Presence and World adapters."""

from __future__ import annotations

from .contracts import StorageBackendRuntime, StorageRuntimeDisabled
from .domain_contracts import PresenceWorldStores
from .domain_schema import ensure_presence_world_schema
from .models import BackendKind
from .presence_adapters import LocalPresenceStore, MySQLPresenceStore
from .world_adapters import LocalWorldProjectionStore, MySQLWorldProjectionStore


async def open_presence_world_stores(
    runtime: StorageBackendRuntime,
    *,
    initialize_schema: bool = False,
) -> PresenceWorldStores:
    """Build both domain adapters from exactly one selected backend runtime.

    Schema initialization is explicit so constructing this bundle cannot migrate
    or activate storage as a side effect.  Cutover tooling may request it only
    after the generation/authority workflow has selected an isolated backend.
    """

    if not runtime.enabled:
        raise StorageRuntimeDisabled(
            "Presence/World adapters require an enabled storage runtime"
        )
    if initialize_schema:
        await ensure_presence_world_schema(runtime)
    if runtime.backend == BackendKind.LOCAL:
        presence = LocalPresenceStore(runtime)
        world = LocalWorldProjectionStore(runtime)
    else:
        presence = MySQLPresenceStore(runtime)
        world = MySQLWorldProjectionStore(runtime)
    if initialize_schema:
        await world.initialize_contract()
    return PresenceWorldStores(presence=presence, world=world)


__all__ = ["open_presence_world_stores"]
