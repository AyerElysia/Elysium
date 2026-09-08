"""Exact selected-authority memory fetch with bounded, non-truncating delivery.

This is an adapter for the existing fetch_life_memory tool, not a new tool or
memory index. Each selected version is independently pinned; the batch is not a
cross-document snapshot. Unregistered bytes remain explicitly unarchived.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from ..memory.eligibility import (
    DEFAULT_MAX_DOCUMENT_BYTES,
    assess_indexed_document_path,
)
from .bounded_projection import resolve_tool_result_budget
from .managed_files import FileSnapshot, ManagedFileSession, selected_file_session

MAX_FETCH_FILES = 64
MAX_FETCH_PATH_BYTES = 1024
MAX_FETCH_DOCUMENT_BYTES = DEFAULT_MAX_DOCUMENT_BYTES
MAX_FETCH_READ_BYTES = 16 * 1024 * 1024
_PREFIX = "life_engine_workspace/"
_RETRY = "Reduce file_paths, or use read_file with the returned exact version_id."


def _wire_bytes(value: Any) -> int:
    """Bound both the current wrapper representation and JSON transports."""
    return max(
        len(str(value).encode("utf-8")),
        len(json.dumps(value, ensure_ascii=False).encode("utf-8")),
    )


def _failure(error_type: str) -> tuple[bool, dict[str, Any]]:
    return False, {
        "action": "fetch_life_memory",
        "error_type": error_type,
        "error": "No complete batch was delivered.",
        "retry_guidance": _RETRY,
    }


def _validate_request(file_paths: Any, version_ids: Any) -> dict[str, str]:
    if not isinstance(file_paths, list) or not 1 <= len(file_paths) <= MAX_FETCH_FILES:
        raise ValueError("ManagedMemoryFetchFileCountInvalid")
    if any(
        not isinstance(path, str)
        or not path
        or len(path.encode("utf-8")) > MAX_FETCH_PATH_BYTES
        for path in file_paths
    ):
        raise ValueError("ManagedMemoryFetchPathInvalid")
    if len(set(file_paths)) != len(file_paths):
        raise ValueError("ManagedMemoryFetchDuplicatePath")
    if version_ids is None:
        return {}
    if not isinstance(version_ids, dict) or any(
        not isinstance(path, str)
        or path not in file_paths
        or not isinstance(version, str)
        or not version
        or len(version.encode("utf-8")) > 255
        for path, version in version_ids.items()
    ):
        raise ValueError("ManagedMemoryFetchVersionPinsInvalid")
    return dict(version_ids)


def _descriptor_reference(path: str, descriptor: dict[str, Any]) -> dict[str, Any]:
    """Validate only technical identity/size fields; do not infer provenance."""
    for key in ("document_id", "version_id"):
        value = descriptor.get(key)
        if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 255:
            raise ValueError("SelectedSubjectDescriptorInvalid")
    size = descriptor.get("byte_length")
    digest = descriptor.get("content_hash")
    logical_path = descriptor.get("logical_path")
    if (
        type(size) is not int
        or size < 0
        or not isinstance(digest, str)
        or len(digest) != 64
        or any(char not in "0123456789abcdef" for char in digest)
        or not isinstance(logical_path, str)
        or not logical_path.startswith(_PREFIX)
    ):
        raise ValueError("SelectedSubjectDescriptorInvalid")
    version_id = descriptor["version_id"]
    document_id = descriptor["document_id"]
    return {
        "path": path,
        "document_id": document_id,
        "version_id": version_id,
        "subject_version_id": version_id,
        "content_hash": digest,
        "file_content_sha256": digest,
        "byte_length": size,
        "file_ref": f"subject-file:{document_id}@{version_id}",
        "source_authority": "subject_document_store",
        "archived": True,
        "read_file": {"path": path, "version_id": version_id},
    }


def _snapshot_reference(snapshot: FileSnapshot) -> dict[str, Any]:
    assert snapshot.content is not None
    if snapshot.version is not None:
        version = snapshot.version
        return _descriptor_reference(
            snapshot.path,
            {
                "document_id": version.document_id,
                "version_id": version.version_id,
                "byte_length": version.byte_length,
                "content_hash": version.content_hash,
                "logical_path": version.logical_path,
            },
        )
    digest = hashlib.sha256(snapshot.content).hexdigest()
    return {
        "path": snapshot.path,
        "content_hash": digest,
        "file_content_sha256": digest,
        "legacy_sha256": digest,
        "byte_length": len(snapshot.content),
        "source_authority": "unregistered_filesystem",
        "archived": False,
        "read_file": {"path": snapshot.path},
        "read_warning": "Unarchived bytes: later reads may change; compare legacy_sha256.",
    }


def _reference_only(reference: dict[str, Any], error_type: str) -> dict[str, Any]:
    return {
        **reference,
        "error_type": error_type,
        "delivery": "reference_only",
        "complete": False,
        "truncated": False,
    }


def _result(files: list[dict[str, Any]], count: int, cap: int) -> dict[str, Any]:
    successful = sum(item.get("complete") is True for item in files)
    return {
        "action": "fetch_life_memory",
        "total_files": count,
        "successful": successful,
        "failed": len(files) - successful,
        "files": files,
        "max_result_bytes": cap,
        "max_document_bytes": MAX_FETCH_DOCUMENT_BYTES,
        "max_batch_read_bytes": MAX_FETCH_READ_BYTES,
        "note": "Complete UTF-8 files or explicit errors/references; no content truncation.",
    }


def _batch_overflow(
    files: list[dict[str, Any]], cap: int
) -> tuple[bool, dict[str, Any]]:
    _, result = _failure("ManagedMemoryFetchResultBudgetExceeded")
    result["max_result_bytes"] = cap
    result["read_references"] = []
    for item in files:
        if "read_file" not in item:
            continue
        reference = {
            key: item[key]
            for key in (
                "path",
                "file_ref",
                "content_hash",
                "legacy_sha256",
                "archived",
                "read_file",
            )
            if key in item
        }
        candidate = {
            **result,
            "read_references": [*result["read_references"], reference],
        }
        if _wire_bytes(candidate) <= cap:
            result = candidate
        if len(result["read_references"]) == 3:
            break
    return False, result


async def _read_one(
    session: ManagedFileSession,
    path: str,
    version_id: str,
    remaining_read_bytes: int,
    include_metadata: bool,
) -> tuple[dict[str, Any], int]:
    eligibility = assess_indexed_document_path(path)
    if not eligibility.eligible:
        return {
            "path": path,
            "error_type": "MemoryDocumentIneligible",
            "reason": eligibility.reason,
        }, 0
    target = session.workspace / path
    # Never silently resolve a stale disk symlink to a different path identity.
    if session.relative(target) != path:
        raise ValueError("ManagedMemoryFetchPathAliasRejected")
    selected_version = version_id
    current_document_id = ""
    if not selected_version:
        head = await session.store.get_head(_PREFIX + path)
        selected_version = head.current_version_id if head is not None else ""
        current_document_id = head.document_id if head is not None else ""
    reference = None
    if selected_version:
        descriptor = await session.store.get_version_descriptor(selected_version)
        if descriptor.get("version_id") != selected_version:
            raise ValueError("SelectedSubjectDescriptorVersionConflict")
        reference = _descriptor_reference(path, descriptor)
        if current_document_id and reference["document_id"] != current_document_id:
            raise ValueError("SelectedSubjectHeadDocumentConflict")
        document_head = await session.store.get_document_head(reference["document_id"])
        if document_head is None or not document_head.logical_path.startswith(_PREFIX):
            raise PermissionError("ManagedFileDocumentOutsideWorkspace")
        if reference["byte_length"] > MAX_FETCH_DOCUMENT_BYTES:
            return _reference_only(reference, "ManagedMemoryFetchDocumentTooLarge"), 0
        if reference["byte_length"] > remaining_read_bytes:
            return _reference_only(reference, "ManagedMemoryFetchReadBudgetExceeded"), 0
    elif remaining_read_bytes <= 0:
        return {"path": path, "error_type": "ManagedMemoryFetchReadBudgetExceeded"}, 0
    # Explicit IDs are independent of current path occupants and old path lineage.
    snapshot = await session.read(
        target,
        version_id=selected_version,
        max_bytes=min(MAX_FETCH_DOCUMENT_BYTES, max(1, remaining_read_bytes)),
    )
    if snapshot.content is None:
        raise FileNotFoundError("ManagedMemoryFetchNotFound")
    if snapshot.path != path or (
        selected_version
        and (
            snapshot.version is None or snapshot.version.version_id != selected_version
        )
    ):
        raise ValueError("ManagedMemoryFetchReadIdentityChanged")
    if (
        snapshot.legacy
        and await session.store.get_path_binding(_PREFIX + path) is not None
    ):
        raise ValueError("ManagedMemoryFetchLegacyBindingChanged")
    reference = _snapshot_reference(snapshot)
    size = len(snapshot.content)
    if size > MAX_FETCH_DOCUMENT_BYTES:
        return _reference_only(reference, "ManagedMemoryFetchDocumentTooLarge"), size
    if size > remaining_read_bytes:
        return _reference_only(reference, "ManagedMemoryFetchReadBudgetExceeded"), size
    try:
        content = snapshot.content.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return _reference_only(reference, "ManagedMemoryFetchRequiresUTF8"), size
    result = {
        **reference,
        "title": Path(path).stem,
        "content": content,
        "delivery": "complete_document",
        "complete": True,
        "truncated": False,
    }
    if include_metadata:
        result["metadata"] = {
            "size_bytes": size,
            "ext": Path(path).suffix,
            "encoding": "utf-8",
        }
        if snapshot.version is not None:
            result["metadata"]["recorded_at"] = snapshot.version.recorded_at
            result["metadata"]["source_encoding"] = snapshot.version.encoding
    return result, size


async def fetch_managed_memories(
    tool: Any,
    file_paths: list[str],
    version_ids: dict[str, str] | None = None,
    include_metadata: bool = True,
    *,
    service: Any = None,
) -> tuple[bool, dict[str, Any]] | None:
    """Handle the whole selected batch; return None only for unselected legacy.

    Callers supply their resolved service explicitly. Missing selected storage,
    released bindings, malformed UTF-8 and failed reads never fall back to disk.
    Complete documents are delivered only if they fit both transport budgets;
    otherwise immutable references enable the existing read_file paging tool.
    Ordinary unregistered files are never auto-imported or given invented IDs.
    """
    try:
        session = selected_file_session(tool, service)
    except Exception as exc:  # noqa: BLE001 - startup failures must stay content-free
        return _failure(type(exc).__name__)
    if session is None:
        if version_ids:
            return _failure("HistoricalMemoryFetchRequiresSelectedStorage")
        return None
    try:
        pins = _validate_request(file_paths, version_ids)
        if type(include_metadata) is not bool:
            raise ValueError("ManagedMemoryFetchMetadataFlagInvalid")
    except (ValueError, TypeError, UnicodeError):
        return _failure("ManagedMemoryFetchRequestInvalid")
    _, cap = resolve_tool_result_budget(getattr(tool, "_runtime_task_name", ""), None)
    files: list[dict[str, Any]] = []
    bytes_read = 0
    for path in file_paths:
        try:
            item, consumed = await _read_one(
                session,
                path,
                pins.get(path, ""),
                MAX_FETCH_READ_BYTES - bytes_read,
                include_metadata,
            )
            bytes_read += consumed
        except Exception as exc:  # noqa: BLE001 - isolate reads without exposing driver text
            item = {"path": path, "error_type": type(exc).__name__}
        candidate = _result([*files, item], len(file_paths), cap)
        if _wire_bytes(candidate) > cap and item.get("complete") is True:
            item = _reference_only(
                {
                    key: value
                    for key, value in item.items()
                    if key
                    not in {
                        "content",
                        "metadata",
                        "title",
                        "delivery",
                        "complete",
                        "truncated",
                    }
                },
                "ManagedMemoryFetchResultBudgetExceeded",
            )
        files.append(item)
        if _wire_bytes(_result(files, len(file_paths), cap)) > cap:
            return _batch_overflow(files, cap)
    result = _result(files, len(file_paths), cap)
    return bool(result["successful"]), result
