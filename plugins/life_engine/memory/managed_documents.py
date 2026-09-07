"""Identity-aware bridges from subject authority to rebuildable memory indexes.

No bytes flow back to authority. Namespace fences serialize the separate
projection transaction with subject path claims; readers verify exact pins
and never turn a stale path hit into a different document.
"""

from __future__ import annotations

import asyncio
import hashlib
from typing import Any

from .eligibility import DEFAULT_MAX_DOCUMENT_BYTES, assess_document_path

_PREFIX = "life_engine_workspace/"
_MAX_BINDINGS = 10_000
_MAX_CATCHUP_CHANGES = 128


async def _owned_projection(awaitable: Any) -> Any:
    """Keep the caller's namespace fence until its owned port write has stopped.

    Cancellation of an await on the SQLite executor does not stop a running
    transaction. Shield the whole port operation and join it before propagating
    cancellation; never detach a worker that can still commit behind the fence.
    """
    task = asyncio.create_task(awaitable, name="managed_file_index_projection")
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:  # noqa: BLE001 - consume child failure, preserving original cancellation
                break
        if not task.cancelled():
            task.exception()
        raise


def selected_subject_store(memory: Any) -> Any:
    store = getattr(memory, "_subject_document_store", None)
    if store is None and getattr(memory, "_subject_document_store_required", False):
        raise RuntimeError("SelectedSubjectStorageNotStarted")
    return store


async def _project_locked(memory: Any, store: Any, document_id: str) -> Any:
    from ..storage.memory.contracts import ManagedDocumentIndexSnapshot

    head = await store.get_document_head(document_id)
    if head is None or not head.logical_path.startswith(_PREFIX):
        raise RuntimeError("ManagedIndexDocumentMissing")
    version = await store.get_version(head.current_version_id)
    raw = bytes(version.content_bytes)
    if (
        version.document_id != head.document_id
        or version.byte_length != len(raw)
        or hashlib.sha256(raw).hexdigest() != version.content_hash
    ):
        raise RuntimeError("ManagedIndexVersionIntegrityError")
    path = head.logical_path.removeprefix(_PREFIX)
    binding_revision = head.binding_revision
    if not head.deleted:
        binding = await store.get_path_binding(head.logical_path)
        if (
            binding is None
            or binding.document_id != head.document_id
            or binding.revision != head.binding_revision
        ):
            raise RuntimeError("ManagedIndexBindingMismatch")
        binding_revision = binding.revision
    content = None
    if (
        not head.deleted
        and assess_document_path(path).eligible
        and len(raw) <= DEFAULT_MAX_DOCUMENT_BYTES
    ):
        try:
            content = raw.decode(version.encoding or "utf-8")
        except (UnicodeError, LookupError):
            # Unsupported bytes remain exact authority, not silently altered text.
            content = None
    return await _owned_projection(
        memory._require_memory_storage().document_index.project_managed_document(
            ManagedDocumentIndexSnapshot(
                document_id=head.document_id,
                version_id=version.version_id,
                path=path,
                document_revision=head.revision,
                binding_revision=binding_revision,
                content_sha256=version.content_hash,
                content=content,
                deleted=head.deleted,
                title=path.rsplit("/", 1)[-1].rsplit(".", 1)[0],
            )
        )
    )


async def project_current_document(memory: Any, document_id: str) -> Any:
    """Rebuild one current document projection, including an explicit deletion."""
    store = selected_subject_store(memory)
    if store is None:
        raise RuntimeError("ManagedIndexRequiresSubjectAuthority")
    async with store.workspace_namespace_fence():
        result = await _project_locked(memory, store, document_id)
        await _sync_index_locked(memory, store, already_projected={document_id})
        return result


async def append_unregistered_observation(memory: Any, **kwargs: Any) -> bool:
    """Startup must not re-enroll a registered file's stale cache in a legacy log."""
    store = selected_subject_store(memory)
    if store is None:
        return await _owned_projection(memory._append_workspace_observation(**kwargs))
    async with store.workspace_namespace_fence():
        binding = await store.get_path_binding(_PREFIX + kwargs["logical_key"])
        if binding is not None:
            return False
        return await _owned_projection(memory._append_workspace_observation(**kwargs))


async def current_node_for_path(memory: Any, path: str) -> Any:
    """Resolve a current path without letting a stale index supply a new owner."""
    node = await memory._require_memory_storage().legacy_graph.get_node_by_file_path(
        path
    )
    store = selected_subject_store(memory)
    if store is None:
        return node
    binding = await store.get_path_binding(_PREFIX + path)
    if binding is not None and binding.document_id is None:
        return None
    if node is None:
        if binding is not None:
            raise RuntimeError("ManagedIndexProjectionStale")
        return None
    if not await is_current_node(memory, node):
        raise RuntimeError("ManagedIndexProjectionStale")
    return node


async def upsert_document_projection(
    memory: Any,
    path: str,
    content: str,
    title: str,
    source_mtime: float | None,
    **kwargs: Any,
) -> Any:
    """Legacy callers cannot overwrite a registered path with stale disk text."""
    store = selected_subject_store(memory)
    index = memory._require_memory_storage().document_index
    if store is None:
        return await index.upsert_document(path, content, title, source_mtime, **kwargs)
    eligibility = assess_document_path(path)
    if not eligibility.eligible:
        raise ValueError("ManagedIndexPathNotEligible")
    async with store.workspace_namespace_fence():
        binding = await store.get_path_binding(_PREFIX + eligibility.path)
        if binding is not None:
            if binding.document_id is None:
                raise RuntimeError("ManagedIndexPathReleased")
            result = await _project_locked(memory, store, binding.document_id)
            await _sync_index_locked(
                memory, store, already_projected={binding.document_id}
            )
            return result
        return await _owned_projection(
            index.upsert_document(
                eligibility.path,
                content,
                title,
                source_mtime,
                **kwargs,
            )
        )


async def _sync_index_locked(
    memory: Any,
    store: Any,
    *,
    already_projected: set[str] | None = None,
) -> None:
    """Advance only after every change in this bounded page is projected."""
    frontier = await store.get_document_projection_frontier(logical_path_prefix=_PREFIX)
    prior = getattr(memory, "_managed_index_frontier", None)
    cursor = 0 if prior is None else int(prior)
    if cursor > frontier:
        raise RuntimeError("ManagedIndexFrontierRegressed")
    rows = await store.list_document_projection_changes(
        after_outbox_id=cursor,
        through_outbox_id=frontier,
        logical_path_prefix=_PREFIX,
        limit=_MAX_CATCHUP_CHANGES,
    )
    completed = set(already_projected or ())
    for row in rows:
        position = row["outbox_id"]
        document_id = row["document_id"]
        if type(position) is not int or not cursor < position <= frontier:
            raise RuntimeError("ManagedIndexFrontierPageInvalid")
        if document_id not in completed:
            await _project_locked(memory, store, document_id)
            completed.add(document_id)
        cursor = position
    if cursor != frontier and len(rows) < _MAX_CATCHUP_CHANGES:
        raise RuntimeError("ManagedIndexFrontierGap")
    memory._managed_index_frontier = cursor
    if cursor != frontier:
        raise RuntimeError("ManagedIndexCatchupPending")


async def index_readiness(memory: Any) -> dict[str, Any]:
    """Read-only readiness independent of search hits, including an empty result."""
    store = selected_subject_store(memory)
    if store is None:
        return {"status": "disabled"}
    frontier = await store.get_document_projection_frontier(logical_path_prefix=_PREFIX)
    synchronized = getattr(memory, "_managed_index_frontier", None)
    return {
        "status": "ready" if synchronized == frontier else "pending_rebuild",
        "authority_frontier": frontier,
        "synchronized_frontier": synchronized,
    }


async def rebuild_managed_documents(memory: Any, indexed_nodes: list[Any]) -> set[str]:
    """Reconcile known identities; return all shadowed paths, including releases."""
    store = selected_subject_store(memory)
    if store is None:
        return set()
    start_frontier = await store.get_document_projection_frontier(
        logical_path_prefix=_PREFIX
    )
    registered: set[str] = set()
    document_ids = {
        str(node.subject_document_id)
        for node in indexed_nodes
        if getattr(node, "subject_document_id", "")
    }
    cursor = ""
    while True:
        rows = await store.list_file_bindings(
            logical_path_prefix=_PREFIX,
            after_logical_path=cursor,
            limit=500,
        )
        if not rows:
            break
        for row in rows:
            logical = str(row["logical_path"])
            if not logical.startswith(_PREFIX) or logical <= cursor:
                raise RuntimeError("ManagedIndexBindingPageInvalid")
            cursor = logical
            registered.add(logical.removeprefix(_PREFIX))
            if row["document_id"]:
                document_ids.add(str(row["document_id"]))
        if len(registered) > _MAX_BINDINGS or len(document_ids) > _MAX_BINDINGS:
            raise RuntimeError("ManagedIndexRecoveryBudgetExceeded")
    for document_id in sorted(document_ids):
        async with store.workspace_namespace_fence():
            await _project_locked(memory, store, document_id)
    async with store.workspace_namespace_fence():
        memory._managed_index_frontier = start_frontier
        await _sync_index_locked(memory, store)
    return registered


def result_identity(result: Any) -> dict[str, Any]:
    """Exact document references survive search, evidence conversion and fetch."""
    document_id = str(getattr(result, "document_id", "") or "")
    version_id = str(getattr(result, "version_id", "") or "")
    return {
        "node_id": str(getattr(result, "node_id", "") or ""),
        "document_id": document_id,
        "version_id": version_id,
        "document_revision": int(getattr(result, "document_revision", 0) or 0),
        "binding_revision": int(getattr(result, "binding_revision", 0) or 0),
        "content_sha256": str(getattr(result, "content_sha256", "") or ""),
        "file_ref": f"subject-file:{document_id}@{version_id}" if document_id else "",
    }


async def is_current_result(memory: Any, result: Any) -> bool:
    """Read-only identity check. False denotes stale projection, never empty truth."""
    store = selected_subject_store(memory)
    if store is None:
        return True
    identity = result_identity(result)
    logical = _PREFIX + str(result.file_path)
    binding = await store.get_path_binding(logical)
    if not identity["document_id"]:
        return binding is None
    if (
        binding is None
        or binding.document_id != identity["document_id"]
        or binding.revision != identity["binding_revision"]
    ):
        return False
    head = await store.get_head(logical)
    if (
        head is None
        or head.deleted
        or head.document_id != identity["document_id"]
        or head.current_version_id != identity["version_id"]
        or head.revision != identity["document_revision"]
        or identity["node_id"] != "subject-file:" + head.document_id
    ):
        return False
    version = await store.get_version_descriptor(identity["version_id"])
    return version["content_hash"] == identity["content_sha256"]


def node_identity(node: Any) -> dict[str, Any]:
    """Translate internal projection metadata without guessing an old identity."""
    return {
        "node_id": node.node_id,
        "document_id": getattr(node, "subject_document_id", ""),
        "version_id": getattr(node, "subject_version_id", ""),
        "document_revision": getattr(node, "subject_document_revision", 0),
        "binding_revision": getattr(node, "subject_binding_revision", 0),
        "content_sha256": getattr(node, "subject_content_sha256", ""),
    }


def node_reference(node: Any) -> dict[str, Any]:
    identity = node_identity(node)
    doc, version = identity["document_id"], identity["version_id"]
    return {**identity, "file_ref": f"subject-file:{doc}@{version}" if doc else ""}


async def is_current_node(memory: Any, node: Any) -> bool:
    from types import SimpleNamespace

    if getattr(node, "is_deleted", False):
        return False
    return await is_current_result(
        memory,
        SimpleNamespace(file_path=node.file_path, **node_identity(node)),
    )


async def association_document_metadata(memory: Any, entity_ref: str) -> Any:
    """Resolve stable refs; legacy path relations never adopt a managed occupant."""
    store = selected_subject_store(memory)
    index = memory._require_memory_storage().document_index
    if entity_ref.startswith("subject-file:"):
        document_id = entity_ref.removeprefix("subject-file:")
        if store is None or not document_id or "@" in document_id:
            return None
        head = await store.get_document_head(document_id)
        if head is None or head.deleted or not head.logical_path.startswith(_PREFIX):
            return None
        metadata = await index.get_document_metadata(
            head.logical_path.removeprefix(_PREFIX)
        )
        if metadata is None or metadata.subject_document_id != document_id:
            raise RuntimeError("ManagedIndexProjectionStale")
    elif entity_ref.startswith("document:"):
        path = entity_ref.removeprefix("document:")
        if (
            store is not None
            and await store.get_path_binding(_PREFIX + path) is not None
        ):
            return None
        metadata = await index.get_document_metadata(path)
    else:
        return None
    if metadata is not None and not await is_current_node(memory, metadata):
        raise RuntimeError("ManagedIndexProjectionStale")
    return metadata


async def validate_search_results(memory: Any, detailed: Any) -> Any:
    """Exclude stale hits with an explicit degraded diagnostic on the response."""
    if selected_subject_store(memory) is None:
        return detailed
    readiness = await index_readiness(memory)
    if readiness["status"] != "ready":
        detailed.diagnostics.degraded = True
        detailed.diagnostics.error_types["subject_identity"] = (
            "ManagedIndexProjectionStale"
        )
        detailed.diagnostics.errors["subject_identity"] = (
            "subject document index has pending authority changes; results may be incomplete"
        )
    valid = []
    stale_count = 0
    for result in detailed.results:
        if await is_current_result(memory, result):
            valid.append(result)
        else:
            stale_count += 1
    detailed.results = valid
    if stale_count:
        detailed.diagnostics.degraded = True
        detailed.diagnostics.error_types["subject_identity"] = (
            "ManagedIndexProjectionStale"
        )
        detailed.diagnostics.errors["subject_identity"] = (
            f"{stale_count} stale document hit(s); current authority must be reindexed"
        )
    return detailed


async def build_managed_bundle(memory: Any, query: str, result: Any) -> Any:
    """Build an exact source bundle without resolving its old path to a new file."""
    from .lineage import MemoryBundle, MemoryEvidence, MemoryTrace

    if not await is_current_result(memory, result):
        raise RuntimeError("ManagedIndexProjectionStale")
    identity = result_identity(result)
    node = await memory._get_node_by_id_wrapper(identity["node_id"])
    if (
        node is None
        or node.subject_document_id != identity["document_id"]
        or node.subject_version_id != identity["version_id"]
        or node.subject_document_revision != identity["document_revision"]
    ):
        raise RuntimeError("ManagedIndexProjectionStale")
    evidence = [
        MemoryEvidence(
            file_path=result.file_path,
            title=result.title,
            snippet=result.snippet,
            relevance=result.relevance,
            source=result.source,
            exists=True,
            **identity,
        )
    ]
    outgoing, incoming = await memory.read_lineage_edges(node.node_id)
    lineage_ids = list(
        dict.fromkeys(
            [edge.target_id for edge in outgoing]
            + [edge.source_id for edge in incoming]
        )
    )
    lineage_views = (
        await memory._require_memory_storage().legacy_graph.get_lineage_node_views(
            lineage_ids
        )
        if lineage_ids
        else {}
    )
    trace = []
    related = [node.node_id]
    for edge, direction in [
        *((edge, "later") for edge in outgoing),
        *((edge, "earlier") for edge in incoming),
    ]:
        neighbour_id = edge.target_id if direction == "later" else edge.source_id
        neighbour = lineage_views.get(neighbour_id)
        if neighbour is None or not assess_document_path(neighbour.file_path).eligible:
            continue
        related.append(neighbour_id)
        doc = str(getattr(neighbour, "subject_document_id", "") or "")
        ver = str(getattr(neighbour, "subject_version_id", "") or "")
        trace.append(
            MemoryTrace(
                relation=edge.edge_type.value,
                file_path=neighbour.file_path or "",
                title=neighbour.title,
                snippet=neighbour.snippet,
                reason=edge.reason,
                direction=direction,
                exists=False,
                node_id=neighbour_id,
                document_id=doc,
                version_id=ver,
                document_revision=getattr(neighbour, "subject_document_revision", 0),
                binding_revision=getattr(neighbour, "subject_binding_revision", 0),
                content_sha256=getattr(neighbour, "subject_content_sha256", ""),
                file_ref=f"subject-file:{doc}@{ver}" if doc else "",
            )
        )
    corrections = await memory.read_memory_corrections(
        query=query,
        related_node_ids=list(dict.fromkeys(related)),
        limit=5,
    )
    return MemoryBundle(
        query=query,
        current_understanding=result.snippet,
        primary_path=result.file_path,
        evidence=evidence,
        history_trace=trace,
        corrections=corrections,
        uncertainty="检索节选不是事实裁决；历史关系保留原节点身份，不按当前路径重绑。",
        primary_node_id=identity["node_id"],
        primary_document_id=identity["document_id"],
        primary_version_id=identity["version_id"],
    )
