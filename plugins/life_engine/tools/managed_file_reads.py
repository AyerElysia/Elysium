"""Bounded existing read-file views for stable document and operation identities."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .bounded_projection import project_bounded_items, project_bounded_text, sha256_json
from .managed_files import ManagedFileSession

_PREFIX = "life_engine_workspace/"


async def read_managed_file_view(
    session: ManagedFileSession,
    target: Path,
    *,
    view: str,
    document_id: str,
    version_id: str,
    occurrence_id: str,
    after_recorded_at: str,
    after_id: str,
    history_limit: int,
    continuation: str,
    max_bytes: int | None,
) -> dict[str, Any]:
    """Keep timeline metadata separate from exact content and bounded delivery."""
    if view not in {"metadata", "history", "operations", "operation"}:
        raise ValueError("ManagedFileReadViewInvalid")
    if any(len(value) > 255 for value in (document_id, version_id, occurrence_id)):
        raise ValueError("ManagedFileReadIdentityExceedsBudget")
    if bool(after_recorded_at) != bool(after_id):
        raise ValueError("ManagedFileHistoryCursorRequiresBothFields")
    store = session.store
    relative = session.relative(target)
    operation = None
    if view == "operation":
        if not occurrence_id:
            raise ValueError("ManagedFileOperationIdentityRequired")
        operation = await store.get_document_operation(occurrence_id)
        if operation is None:
            return project_bounded_text(
                projection_name="managed-file-operation-missing",
                task_name=getattr(session.tool, "_runtime_task_name", ""),
                requested_max_bytes=max_bytes,
                binding={"occurrence_id": occurrence_id},
                frontier={"state": "not_found"},
                base_payload={
                    "action": "read_file",
                    "view": view,
                    "occurrence_id": occurrence_id,
                    "commit_status": "not_found",
                    "note": "No committed receipt found; an in-flight call may still commit.",
                },
                content="",
                content_ref="file-operation-query:" + sha256_json(occurrence_id),
                continuation=continuation,
            )
        if document_id and document_id != operation.document_id:
            raise ValueError("ManagedFileOperationDocumentConflict")
        document_id = operation.document_id
    descriptor = None
    if version_id:
        descriptor = await store.get_version_descriptor(version_id)
        if document_id and document_id != descriptor["document_id"]:
            raise ValueError("ManagedFileVersionDocumentConflict")
        document_id = str(descriptor["document_id"])
    head = (
        await store.get_document_head(document_id)
        if document_id
        else await store.get_head(_PREFIX + relative)
    )
    if head is None:
        raise LookupError("ManagedFileDocumentNotFound")
    if not head.logical_path.startswith(_PREFIX):
        raise PermissionError("ManagedFileDocumentOutsideWorkspace")
    document_id = head.document_id
    bounded = min(100, max(1, int(history_limit)))
    next_page: dict[str, str] | None = None
    if view == "metadata":
        descriptor = descriptor or await store.get_version_descriptor(
            head.current_version_id
        )
        items = [descriptor]
    elif view == "history":
        rows = await store.list_document_version_descriptors(
            document_id,
            after_recorded_at=after_recorded_at,
            after_version_id=after_id,
            limit=bounded + 1,
        )
        items = rows[:bounded]
        if len(rows) > bounded:
            next_page = {
                "after_recorded_at": str(items[-1]["recorded_at"]),
                "after_id": str(items[-1]["version_id"]),
            }
    elif view == "operations":
        rows = await store.list_document_operations(
            document_id,
            after_recorded_at=after_recorded_at,
            after_occurrence_id=after_id,
            limit=bounded + 1,
        )
        items = [
            {
                "occurrence_id": item.occurrence_id,
                "operation": item.operation,
                "document_id": item.document_id,
                "recorded_at": item.recorded_at,
                "version_id": str(item.result["version_id"]),
                "metadata_only": True,
                "full_metadata_query": {
                    "view": "operation",
                    "occurrence_id": item.occurrence_id,
                },
            }
            for item in rows[:bounded]
        ]
        if len(rows) > bounded:
            next_page = {
                "after_recorded_at": str(items[-1]["recorded_at"]),
                "after_id": str(items[-1]["occurrence_id"]),
            }
    else:
        assert operation is not None
        items = [asdict(operation)]
    for item in items:
        stored_version_id = item.get("version_id")
        if stored_version_id:
            item["file_ref"] = f"subject-file:{document_id}@{stored_version_id}"
    if view in {"metadata", "operation"}:
        full = items[0]
        summary = (
            {
                key: full[key]
                for key in ("version_id", "document_id", "byte_length", "content_hash")
            }
            if view == "metadata"
            else {
                "occurrence_id": full["occurrence_id"],
                "operation": full["operation"],
                "result": {"version_id": full["result"]["version_id"]},
            }
        )
        return project_bounded_text(
            projection_name="managed-file-exact-metadata",
            task_name=getattr(session.tool, "_runtime_task_name", ""),
            requested_max_bytes=max_bytes,
            binding={
                "view": view,
                "document_id": document_id,
                "version_id": version_id,
                "occurrence_id": occurrence_id,
            },
            frontier={"metadata_sha256": sha256_json(full)},
            base_payload={
                "action": "read_file",
                "view": view,
                "document_id": document_id,
                "current_path": head.logical_path.removeprefix(_PREFIX),
                "current_version_id": head.current_version_id,
                "deleted": head.deleted,
                "items": [summary],
                "metadata_summary_only": True,
                "content_format": "Exact metadata JSON; concatenate content pages before parsing.",
            },
            content=json.dumps(
                full, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ),
            content_ref="file-metadata:" + sha256_json(full),
            continuation=continuation,
        )
    return project_bounded_items(
        projection_name="managed-file-metadata",
        task_name=getattr(session.tool, "_runtime_task_name", ""),
        requested_max_bytes=max_bytes,
        binding={
            "view": view,
            "document_id": document_id,
            "version_id": version_id,
            "occurrence_id": occurrence_id,
            "after_recorded_at": after_recorded_at,
            "after_id": after_id,
            "history_limit": bounded,
        },
        frontier={"items_sha256": sha256_json(items)},
        base_payload={
            "action": "read_file",
            "view": view,
            "document_id": document_id,
            "current_path": head.logical_path.removeprefix(_PREFIX),
            "current_version_id": head.current_version_id,
            "deleted": head.deleted,
            "source_authority": "subject_document_store",
            "next_history_page": next_page,
            "history_page_note": (
                "Finish continuation first; then use next_history_page with the same document_id."
            ),
        },
        items_key="items",
        items=items,
        item_refs=[
            f"subject-file-metadata:{document_id}:{item.get('occurrence_id') if view == 'operations' else item.get('version_id')}"
            for item in items
        ],
        continuation=continuation,
        compact=True,
    )
