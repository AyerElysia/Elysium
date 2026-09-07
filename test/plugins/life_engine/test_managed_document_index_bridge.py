"""Selected subject authority bridged to memory projection ports, temp-only."""

from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from plugins.life_engine.memory.managed_documents import (
    association_document_metadata,
    build_managed_bundle,
    project_current_document,
    rebuild_managed_documents,
    upsert_document_projection,
    validate_search_results,
)
from plugins.life_engine.memory.search import (
    DetailedSearchResult,
    SearchDiagnostics,
    SearchResult,
)
from test.plugins.life_engine.test_managed_file_lifecycle_tools import (
    _patch_tool,
    _read,
    _write,
)
from test.plugins.life_engine.test_minimal_subject_file_continuity import _memory_plugin
from test.plugins.life_engine.test_subject_document_storage_contract import _local_store


class _Index:
    def __init__(self):
        self.snapshots = {}
        self.legacy_writes = []

    async def project_managed_document(self, snapshot):
        snapshot.validate()
        self.snapshots[snapshot.document_id] = snapshot
        return SimpleNamespace(
            node_id="subject-file:" + snapshot.document_id,
            document_id=snapshot.document_id,
            version_id=snapshot.version_id,
            document_revision=snapshot.document_revision,
            indexed=snapshot.content is not None and not snapshot.deleted,
            idempotent_replay=False,
        )

    async def upsert_document(self, *args, **kwargs):
        self.legacy_writes.append(args)
        return SimpleNamespace(indexed=True)

    def node(self, document_id):
        snap = self.snapshots[document_id]
        return SimpleNamespace(
            node_id="subject-file:" + snap.document_id,
            file_path=snap.path,
            title=snap.title,
            subject_document_id=snap.document_id,
            subject_version_id=snap.version_id,
            subject_document_revision=snap.document_revision,
            subject_binding_revision=snap.binding_revision,
            subject_content_sha256=snap.content_sha256,
            is_deleted=snap.deleted,
        )

    async def get_document_metadata(self, path):
        nodes = [
            self.node(key)
            for key, snap in self.snapshots.items()
            if snap.path == path and not snap.deleted
        ]
        return nodes[-1] if nodes else None


class _Memory:
    def __init__(self, store):
        self._subject_document_store = store
        self._subject_document_store_required = True
        self.index = _Index()

    def _require_memory_storage(self):
        return SimpleNamespace(document_index=self.index)

    async def project_managed_document(self, document_id):
        return await project_current_document(self, document_id)

    async def _get_node_by_id_wrapper(self, node_id):
        return self.index.node(node_id.removeprefix("subject-file:"))

    async def read_lineage_edges(self, node_id):
        return [], []

    async def read_memory_corrections(self, **kwargs):
        return []


def _hit(memory, document_id):
    snap = memory.index.snapshots[document_id]
    return SearchResult(
        file_path=snap.path,
        title=snap.title,
        snippet=snap.content or "",
        relevance=1.0,
        source="direct",
        node_id="subject-file:" + document_id,
        document_id=document_id,
        version_id=snap.version_id,
        document_revision=snap.document_revision,
        binding_revision=snap.binding_revision,
        content_sha256=snap.content_sha256,
    )


async def test_file_lifecycle_refreshes_stable_index_even_when_projection_fails(
    tmp_path: Path, monkeypatch
):
    async with _local_store(tmp_path) as (_, store, _):
        plugin, service, _ = _memory_plugin(
            store, data_root=tmp_path / "data", monkeypatch=monkeypatch
        )
        memory = _Memory(store)
        service._memory_service = memory
        first = await _write(
            plugin, "notes/a.md", "authoritative\n", "activity:index-first"
        )
        assert first["index_projection"]["status"] == "updated"
        pin = await _read(plugin, "notes/a.md")
        ok, renamed = await _patch_tool(plugin, "activity:index-rename").execute(
            "*** Begin Patch\n*** Update File: notes/a.md\n*** Move to: notes/b.md\n*** End Patch\n",
            expected_versions={"notes/a.md": pin["expected_version"]},
        )
        assert ok, renamed
        snap = memory.index.snapshots[first["document_id"]]
        assert snap.path == "notes/b.md"
        assert snap.content == "authoritative\n"
        assert (
            renamed["files"][0]["index_projection"]["node_id"]
            == first["index_projection"]["node_id"]
        )
        pin = await _read(plugin, "notes/b.md")

        async def broken_projection(**kwargs):
            raise RuntimeError("SyntheticDiskFailure")

        monkeypatch.setattr(service, "_project_subject_version", broken_projection)
        ok, deleted = await _patch_tool(plugin, "activity:index-delete").execute(
            "*** Begin Patch\n*** Delete File: notes/b.md\n*** End Patch\n",
            expected_versions={"notes/b.md": pin["expected_version"]},
        )
        assert ok, deleted
        assert deleted["files"][0]["projection"]["status"] == "pending_recovery"
        assert deleted["files"][0]["index_projection"]["status"] == "not_indexed"
        assert memory.index.snapshots[first["document_id"]].deleted
        assert (tmp_path / "data/life_engine_workspace/notes/b.md").exists()


async def test_legacy_upsert_cannot_replace_authority_or_resurrect_released_path(
    tmp_path: Path, monkeypatch
):
    async with _local_store(tmp_path) as (_, store, _):
        plugin, _, _ = _memory_plugin(
            store, data_root=tmp_path / "data", monkeypatch=monkeypatch
        )
        first = await _write(
            plugin, "notes/a.md", "real content\n", "activity:guard-first"
        )
        memory = _Memory(store)
        await upsert_document_projection(
            memory, "notes/a.md", "stale disk", "stale title", None
        )
        assert memory.index.snapshots[first["document_id"]].content == "real content\n"
        assert not memory.index.legacy_writes
        pin = await _read(plugin, "notes/a.md")
        ok, result = await _patch_tool(plugin, "activity:guard-delete").execute(
            "*** Begin Patch\n*** Delete File: notes/a.md\n*** End Patch\n",
            expected_versions={"notes/a.md": pin["expected_version"]},
        )
        assert ok, result
        with pytest.raises(RuntimeError, match="ManagedIndexPathReleased"):
            await upsert_document_projection(
                memory, "notes/a.md", "old cache", "", None
            )
        assert len(await store.list_document_history(first["document_id"])) == 1


async def test_search_and_bundle_reject_same_path_different_identity(
    tmp_path: Path, monkeypatch
):
    async with _local_store(tmp_path) as (_, store, _):
        plugin, service, _ = _memory_plugin(
            store, data_root=tmp_path / "data", monkeypatch=monkeypatch
        )
        memory = _Memory(store)
        service._memory_service = memory
        first = await _write(
            plugin, "notes/a.md", "old evidence\n", "activity:hit-first"
        )
        old_hit = _hit(memory, first["document_id"])
        bundle = await build_managed_bundle(memory, "evidence", old_hit)
        assert bundle.primary_document_id == first["document_id"]
        assert bundle.evidence[0].version_id == first["version_id"]
        pin = await _read(plugin, "notes/a.md")
        ok, renamed = await _patch_tool(plugin, "activity:hit-rename").execute(
            "*** Begin Patch\n*** Update File: notes/a.md\n*** Move to: notes/b.md\n*** End Patch\n",
            expected_versions={"notes/a.md": pin["expected_version"]},
        )
        assert ok, renamed
        second = await _write(
            plugin, "notes/a.md", "new evidence\n", "activity:hit-new"
        )
        detailed = DetailedSearchResult(
            results=[old_hit, _hit(memory, second["document_id"])],
            diagnostics=SearchDiagnostics(fts_success=True),
        )
        checked = await validate_search_results(memory, detailed)
        assert checked.degraded
        assert len(checked.results) == 1
        assert checked.results[0].document_id == second["document_id"]
        assert checked.error_types["subject_identity"] == "ManagedIndexProjectionStale"
        with pytest.raises(RuntimeError, match="ManagedIndexProjectionStale"):
            await build_managed_bundle(memory, "evidence", old_hit)
        assert (
            await association_document_metadata(memory, "document:notes/a.md") is None
        )
        metadata = await association_document_metadata(
            memory, "subject-file:" + first["document_id"]
        )
        assert metadata.file_path == "notes/b.md"


async def test_restart_rebuild_includes_deleted_id_and_ignores_missing_projection(
    tmp_path: Path, monkeypatch
):
    async with _local_store(tmp_path) as (_, store, _):
        plugin, service, _ = _memory_plugin(
            store, data_root=tmp_path / "data", monkeypatch=monkeypatch
        )
        memory = _Memory(store)
        service._memory_service = memory
        first = await _write(
            plugin, "notes/gone.md", "preserved\n", "activity:restart-gone"
        )
        pin = await _read(plugin, "notes/gone.md")
        ok, result = await _patch_tool(plugin, "activity:restart-delete").execute(
            "*** Begin Patch\n*** Delete File: notes/gone.md\n*** End Patch\n",
            expected_versions={"notes/gone.md": pin["expected_version"]},
        )
        assert ok, result
        old_nodes = [memory.index.node(first["document_id"])]
        service._memory_service = None

        async def no_disk(**kwargs):
            raise RuntimeError("DiskUnavailable")

        monkeypatch.setattr(service, "_project_subject_version", no_disk)
        second = await _write(
            plugin, "notes/virtual.md", "database only\n", "activity:restart-virtual"
        )
        new_memory = _Memory(store)
        registered = await rebuild_managed_documents(new_memory, old_nodes)
        assert registered == {"notes/gone.md", "notes/virtual.md"}
        assert new_memory.index.snapshots[first["document_id"]].deleted
        virtual = new_memory.index.snapshots[second["document_id"]]
        assert virtual.content == "database only\n"
        assert virtual.content_sha256 == hashlib.sha256(b"database only\n").hexdigest()
        assert not (tmp_path / "data/life_engine_workspace/notes/virtual.md").exists()


async def test_real_memory_service_and_subject_store_lifecycle_restart(
    tmp_path: Path, monkeypatch
):
    from plugins.life_engine.memory.service import LifeMemoryService

    async with _local_store(tmp_path) as (_, store, _):
        plugin, service, _ = _memory_plugin(
            store, data_root=tmp_path / "data", monkeypatch=monkeypatch
        )
        workspace = tmp_path / "data/life_engine_workspace"
        memory = LifeMemoryService(
            workspace,
            vector_backend_enabled=False,
            index_worker_enabled=False,
            subject_document_store=store,
            subject_document_store_required=True,
        )
        await memory.initialize()
        service._memory_service = memory
        try:
            first = await _write(
                plugin,
                "notes/a.md",
                "continuityalpha original\n",
                "activity:real-first",
            )
            assert first["index_projection"]["status"] == "updated", first
            results = await memory.search_memory(
                "continuityalpha", return_bundles=False
            )
            assert len(results) == 1 and results[0].document_id == first["document_id"]
            pin = await _read(plugin, "notes/a.md")
            ok, renamed = await _patch_tool(plugin, "activity:real-rename").execute(
                "*** Begin Patch\n*** Update File: notes/a.md\n*** Move to: notes/b.md\n*** End Patch\n",
                expected_versions={"notes/a.md": pin["expected_version"]},
            )
            assert (
                ok and renamed["files"][0]["index_projection"]["status"] == "updated"
            ), renamed
            bundles = await memory.search_memory("continuityalpha")
            assert len(bundles) == 1 and bundles[0].primary_path == "notes/b.md"
            assert bundles[0].primary_document_id == first["document_id"]
            pin = await _read(plugin, "notes/b.md")
            ok, deleted = await _patch_tool(plugin, "activity:real-delete").execute(
                "*** Begin Patch\n*** Delete File: notes/b.md\n*** End Patch\n",
                expected_versions={"notes/b.md": pin["expected_version"]},
            )
            assert (
                ok
                and deleted["files"][0]["index_projection"]["status"] == "not_indexed"
            ), deleted
            assert (
                await memory.search_memory("continuityalpha", return_bundles=False)
                == []
            )
            second = await _write(
                plugin,
                "notes/b.md",
                "continuitybeta replacement\n",
                "activity:real-new",
            )
            assert second["document_id"] != first["document_id"]
            old_node = await memory._get_node_by_id_wrapper(
                "subject-file:" + first["document_id"]
            )
            assert (
                old_node.is_deleted
                and old_node.subject_version_id == first["version_id"]
            )
        finally:
            await memory.close()
            service._memory_service = None

        restarted = LifeMemoryService(
            workspace,
            vector_backend_enabled=False,
            index_worker_enabled=False,
            subject_document_store=store,
            subject_document_store_required=True,
        )
        await restarted.initialize()
        try:
            results = await restarted.search_memory(
                "continuitybeta", return_bundles=False
            )
            assert len(results) == 1 and results[0].document_id == second["document_id"]
            assert (
                await restarted.search_memory("continuityalpha", return_bundles=False)
                == []
            )
            assert len(await store.list_document_history(first["document_id"])) == 1
            assert not await restarted.living_memory_store.list_artifact_heads()
        finally:
            await restarted.close()


async def test_explicit_recovery_repairs_real_index_without_new_subject_version(
    tmp_path: Path, monkeypatch
):
    from plugins.life_engine.memory.service import LifeMemoryService

    async with _local_store(tmp_path) as (_, store, _):
        plugin, service, _ = _memory_plugin(
            store, data_root=tmp_path / "data", monkeypatch=monkeypatch
        )
        memory = LifeMemoryService(
            tmp_path / "data/life_engine_workspace",
            vector_backend_enabled=False,
            index_worker_enabled=False,
            subject_document_store=store,
            subject_document_store_required=True,
        )
        await memory.initialize()
        service._memory_service = memory
        original_project = memory.project_managed_document

        async def failed_index(document_id):
            raise RuntimeError("SyntheticIndexFailure")

        monkeypatch.setattr(memory, "project_managed_document", failed_index)
        try:
            first = await _write(
                plugin,
                "notes/recover.md",
                "continuityrepair value\n",
                "activity:real-recovery",
            )
            assert first["index_projection"]["status"] == "pending_rebuild"
            incomplete = await memory.search_memory_detailed("continuityrepair")
            assert incomplete.results == [] and incomplete.degraded
            assert (
                incomplete.error_types["subject_identity"]
                == "ManagedIndexProjectionStale"
            )
            with pytest.raises(RuntimeError, match="ManagedIndexProjectionStale"):
                await memory.search_memory("continuityrepair", return_bundles=False)
            health = await memory.health_snapshot()
            assert health["subject_file_index"]["status"] == "pending_rebuild"
            monkeypatch.setattr(memory, "project_managed_document", original_project)
            ok, repaired = await _patch_tool(
                plugin, "activity:real-repair-only"
            ).execute(
                "",
                recover_occurrence_id=first["occurrence_id"],
            )
            assert ok and repaired["index_projection"]["status"] == "updated", repaired
            assert len(await store.list_document_history(first["document_id"])) == 1
            results = await memory.search_memory(
                "continuityrepair", return_bundles=False
            )
            assert len(results) == 1 and results[0].version_id == first["version_id"]
            ready = await memory.search_memory_detailed("continuityrepair")
            assert "subject_identity" not in ready.error_types
        finally:
            await memory.close()
            service._memory_service = None


async def test_legacy_history_neighbour_keeps_old_node_when_path_is_reused(
    tmp_path: Path, monkeypatch
):
    from plugins.life_engine.memory.service import LifeMemoryService

    async with _local_store(tmp_path) as (_, store, _):
        plugin, service, _ = _memory_plugin(
            store, data_root=tmp_path / "data", monkeypatch=monkeypatch
        )
        workspace = tmp_path / "data/life_engine_workspace"
        (workspace / "notes").mkdir()
        (workspace / "notes/a.md").write_text("legacyanchor only", encoding="utf-8")
        (workspace / "notes/b.md").write_text("legacy old neighbour", encoding="utf-8")
        memory = LifeMemoryService(
            workspace,
            vector_backend_enabled=False,
            index_worker_enabled=False,
            subject_document_store=store,
            subject_document_store_required=True,
        )
        await memory.initialize()
        service._memory_service = memory
        try:
            old_a = await memory.get_node_by_file_path("notes/a.md")
            old_b = await memory.get_node_by_file_path("notes/b.md")
            await memory._require_memory_storage().legacy_graph.create_or_update_edge(
                old_a.node_id,
                old_b.node_id,
                "relates",
                reason="synthetic old relation",
            )
            pin = await _read(plugin, "notes/b.md")
            replacement = await _write(
                plugin,
                "notes/b.md",
                "new subject-owned content\n",
                "activity:legacy-enroll",
                pin["expected_version"],
            )
            assert replacement["index_projection"]["status"] == "updated", replacement
            bundles = await memory.search_memory("legacyanchor")
            assert len(bundles) == 1
            history = next(
                item
                for item in bundles[0].history_trace
                if item.node_id == old_b.node_id
            )
            assert history.file_path == "notes/b.md" and not history.exists
            assert not history.document_id and not history.version_id
            assert "legacy old neighbour" in history.snippet
            assert bundles[0].primary_node_id == old_a.node_id
            managed = await _write(
                plugin,
                "notes/c.md",
                "managedanchor current content\n",
                "activity:managed-historical-neighbour",
            )
            await memory._require_memory_storage().legacy_graph.create_or_update_edge(
                managed["index_projection"]["node_id"],
                old_b.node_id,
                "relates",
                reason="synthetic imported relation to an exact retired ID",
            )
            managed_bundles = await memory.search_memory("managedanchor")
            assert len(managed_bundles) == 1
            managed_history = next(
                item
                for item in managed_bundles[0].history_trace
                if item.node_id == old_b.node_id
            )
            assert managed_bundles[0].primary_document_id == managed["document_id"]
            assert not managed_history.exists and not managed_history.document_id
            assert managed_history.file_path == "notes/b.md"
            assert "legacy old neighbour" in managed_history.snippet
        finally:
            await memory.close()
            service._memory_service = None
