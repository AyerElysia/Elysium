"""Fail-closed construction for selectable MySQL Life Memory stores."""

from __future__ import annotations

from ..contracts import StorageBackendRuntime
from ..models import BackendKind
from .contracts import MemoryStorageBundle
from .mysql import create_mysql_memory_storage_bundle
from .schema import ensure_memory_storage_schema


async def open_mysql_memory_storage(
    runtime: StorageBackendRuntime,
    *,
    initialize_schema: bool = False,
) -> MemoryStorageBundle:
    """Open one coherent MySQL bundle without fallback or data migration.

    Schema initialization is explicit because MySQL DDL may auto-commit.  This
    function never changes backend authority, copies formal data, or starts a
    background worker.
    """

    if not runtime.enabled or runtime.backend != BackendKind.MYSQL:
        raise RuntimeError("MySQL Memory storage requires the selected MySQL runtime")
    if initialize_schema:
        await ensure_memory_storage_schema(runtime)
    return create_mysql_memory_storage_bundle(runtime)


__all__ = ["open_mysql_memory_storage"]
