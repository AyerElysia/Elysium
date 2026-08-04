"""Selectable life-domain storage contracts and infrastructure adapters."""

from .contracts import StorageBackendRuntime
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
    "LocalBackendSettings",
    "MySQLBackendSettings",
    "StorageAvailability",
    "StorageBackendRuntime",
    "StorageFactorySettings",
    "open_storage_backend",
    "settings_from_life_engine_config",
]
