"""Coherent factory for selectable subject document history."""

from __future__ import annotations

from .contracts import StorageBackendRuntime, StorageRuntimeDisabled
from .models import BackendKind
from .subject_adapters import LocalSubjectDocumentStore, MySQLSubjectDocumentStore
from .subject_contracts import SubjectDocumentStorePort
from .subject_schema import ensure_subject_document_schema


async def open_subject_document_store(
    runtime: StorageBackendRuntime,
    *,
    initialize_schema: bool = False,
    require_database_immutability: bool = True,
) -> SubjectDocumentStorePort:
    """Build one subject adapter from the already-selected coherent runtime."""

    if not runtime.enabled:
        raise StorageRuntimeDisabled(
            "subject document adapter requires enabled storage runtime"
        )
    if initialize_schema:
        await ensure_subject_document_schema(
            runtime,
            require_database_immutability=require_database_immutability,
        )
    if runtime.backend == BackendKind.LOCAL:
        return LocalSubjectDocumentStore(runtime)
    return MySQLSubjectDocumentStore(runtime)


__all__ = ["open_subject_document_store"]
