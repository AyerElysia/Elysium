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
from .schema import (
    MEMORY_IMMUTABILITY_MIGRATIONS,
    MEMORY_IMMUTABILITY_SCHEMA_VERSION,
    MEMORY_IMMUTABILITY_TRIGGER_CONTRACT,
    MEMORY_IMMUTABLE_TABLE_COLUMNS,
    MEMORY_IMMUTABLE_TABLES,
    MEMORY_MUTABLE_TABLES,
    MEMORY_SCHEMA_VERSION,
    MEMORY_WITNESS_IMMUTABLE_COLUMNS,
    MEMORY_WITNESS_MUTABLE_PROJECTION_COLUMNS,
    MemoryDatabaseImmutabilityError,
    MemoryImmutabilityPolicyError,
    ensure_memory_storage_schema,
    memory_database_immutability_required,
    verify_memory_storage_immutability,
)

__all__ = [
    "MEMORY_IMMUTABILITY_MIGRATIONS",
    "MEMORY_IMMUTABILITY_SCHEMA_VERSION",
    "MEMORY_IMMUTABILITY_TRIGGER_CONTRACT",
    "MEMORY_IMMUTABLE_TABLES",
    "MEMORY_IMMUTABLE_TABLE_COLUMNS",
    "MEMORY_MUTABLE_TABLES",
    "MEMORY_SCHEMA_VERSION",
    "MEMORY_WITNESS_IMMUTABLE_COLUMNS",
    "MEMORY_WITNESS_MUTABLE_PROJECTION_COLUMNS",
    "DocumentIndexProjection",
    "EpistemicMemoryStore",
    "ExperienceLedgerStore",
    "LegacyGraphStore",
    "LivingMemoryStore",
    "MemoryDatabaseImmutabilityError",
    "MemoryImmutabilityPolicyError",
    "MemoryStorageBundle",
    "MemoryStoreCharacterization",
    "MemoryStoreRole",
    "WitnessLedgerStore",
    "create_local_memory_storage_bundle",
    "create_mysql_memory_storage_bundle",
    "ensure_memory_storage_schema",
    "memory_database_immutability_required",
    "memory_store_characterizations",
    "open_mysql_memory_storage",
    "verify_memory_storage_immutability",
]
