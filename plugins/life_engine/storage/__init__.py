"""Selectable life-domain storage contracts and infrastructure adapters."""

from .contracts import StorageBackendRuntime, StorageWriterRole
from .event_contracts import (
    LifeEventConsumerConflict,
    LifeEventConsumerCursor,
    LifeEventDigest,
    LifeEventOccurrenceConflict,
    LifeEventSnapshotCursor,
    LifeEventSnapshotImportPort,
    LifeEventSnapshotRecord,
    LifeEventSnapshotSourcePort,
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
from .subject_contracts import (
    AppendSubjectDocumentVersion,
    SubjectDocumentCommit,
    SubjectDocumentConflict,
    SubjectDocumentHead,
    SubjectDocumentNotFound,
    SubjectDocumentStorePort,
    SubjectDocumentVersion,
)
from .subject_factory import open_subject_document_store
from .subject_workspace import SubjectWorkspaceObserver, SubjectWorkspaceProjector

__all__ = [
    "AppendSubjectDocumentVersion",
    "AuthorityProvider",
    "AuthorityToken",
    "BackendGeneration",
    "BackendKind",
    "GenerationStatus",
    "LifeEventConsumerConflict",
    "LifeEventConsumerCursor",
    "LifeEventDigest",
    "LifeEventOccurrenceConflict",
    "LifeEventSnapshotCursor",
    "LifeEventSnapshotImportPort",
    "LifeEventSnapshotRecord",
    "LifeEventSnapshotSourcePort",
    "LifeEventStorePort",
    "LocalBackendSettings",
    "MySQLBackendSettings",
    "StorageAvailability",
    "StorageBackendRuntime",
    "StorageFactorySettings",
    "StorageWriterRole",
    "SubjectDocumentCommit",
    "SubjectDocumentConflict",
    "SubjectDocumentHead",
    "SubjectDocumentNotFound",
    "SubjectDocumentStorePort",
    "SubjectDocumentVersion",
    "SubjectWorkspaceObserver",
    "SubjectWorkspaceProjector",
    "open_life_event_store",
    "open_storage_backend",
    "open_subject_document_store",
    "settings_from_life_engine_config",
]
