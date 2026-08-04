"""Selectable life-domain storage contracts and infrastructure adapters."""

from .contracts import StorageBackendRuntime
from .event_contracts import (
    LifeEventConsumerConflict,
    LifeEventConsumerCursor,
    LifeEventOccurrenceConflict,
    LifeEventStorePort,
)
from .event_factory import open_life_event_store
from .factory import (
    AuthorityProvider,
    LocalBackendSettings,
    MySQLBackendSettings,
    StorageFactorySettings,
    open_storage_backend,
    settings_from_life_engine_config,
)
from .models import (
    AuthorityToken,
    BackendGeneration,
    BackendKind,
    GenerationStatus,
    StorageAvailability,
)

__all__ = [
    "AuthorityProvider",
    "AuthorityToken",
    "BackendGeneration",
    "BackendKind",
    "GenerationStatus",
    "LifeEventConsumerConflict",
    "LifeEventConsumerCursor",
    "LifeEventOccurrenceConflict",
    "LifeEventStorePort",
    "LocalBackendSettings",
    "MySQLBackendSettings",
    "StorageAvailability",
    "StorageBackendRuntime",
    "StorageFactorySettings",
    "open_life_event_store",
    "open_storage_backend",
    "settings_from_life_engine_config",
]
