"""Backend-neutral Life Memory ports and adapters."""

from .contracts import (
    DocumentIndexProjection,
    EpistemicMemoryStore,
    ExperienceLedgerStore,
    LegacyGraphStore,
    LivingMemoryStore,
    MemoryStorageBundle,
    MemoryStoreCharacterization,
    MemoryStoreRole,
    WitnessLedgerStore,
    memory_store_characterizations,
)
from .factory import open_mysql_memory_storage
from .local import create_local_memory_storage_bundle
from .mysql import create_mysql_memory_storage_bundle
from .schema import MEMORY_SCHEMA_VERSION, ensure_memory_storage_schema

__all__ = [
    "MEMORY_SCHEMA_VERSION",
    "DocumentIndexProjection",
    "EpistemicMemoryStore",
    "ExperienceLedgerStore",
    "LegacyGraphStore",
    "LivingMemoryStore",
    "MemoryStorageBundle",
    "MemoryStoreCharacterization",
    "MemoryStoreRole",
    "WitnessLedgerStore",
    "create_local_memory_storage_bundle",
    "create_mysql_memory_storage_bundle",
    "ensure_memory_storage_schema",
    "memory_store_characterizations",
    "open_mysql_memory_storage",
]
