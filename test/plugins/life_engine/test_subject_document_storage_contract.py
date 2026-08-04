"""Shared local contract for exact-byte subject document history."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from plugins.life_engine.core.config import LifeEngineConfig
from plugins.life_engine.service import LifeEngineService
from plugins.life_engine.storage.authority import (
    FileAuthorityRegistry,
    StaleAuthorityToken,
)
from plugins.life_engine.storage.contracts import StorageBackendRuntime
from plugins.life_engine.storage.factory import (
    LocalBackendSettings,
    StorageFactorySettings,
    open_storage_backend,
)
from plugins.life_engine.storage.models import (
    BackendGeneration,
    BackendKind,
    GenerationStatus,
)
from plugins.life_engine.storage.subject_contracts import (
    AppendSubjectDocumentVersion,
    SubjectDocumentConflict,
    SubjectDocumentStorePort,
)
from plugins.life_engine.storage.subject_factory import open_subject_document_store
from plugins.life_engine.storage.subject_workspace import (
    SubjectWorkspaceObserver,
    SubjectWorkspaceProjector,
)


def _generation() -> BackendGeneration:
    return BackendGeneration(
        generation_id="subject-local-v1",
        backend=BackendKind.LOCAL,
        schema_version=1,
        source_snapshot_sha256="a" * 64,
        root_hashes={"subject": "b" * 64},
        frontiers={"subject": 0},
        created_at="2026-08-04T00:00:00+00:00",
        verified_at="2026-08-04T00:01:00+00:00",
        status=GenerationStatus.VERIFIED,
    )


@asynccontextmanager
async def _local_store(
    tmp_path: Path,
) -> AsyncIterator[tuple[StorageBackendRuntime, SubjectDocumentStorePort, object]]:
    authority_path = tmp_path / "authority.json"
    registry = FileAuthorityRegistry(authority_path)
    await registry.register_generation(_generation())
    token = await registry.activate_generation(
        _generation().generation_id,
        expected_epoch=0,
        owner_id="subject-contract",
        lease_seconds=300,
        confirm_previous_writers_stopped=True,
    )
    runtime = await open_storage_backend(
        StorageFactorySettings(
            enabled=True,
            authoritative_backend=BackendKind.LOCAL,
            backend_generation=_generation().generation_id,
            schema_version=1,
            authority_epoch=token.authority_epoch,
            authority_owner_id=token.owner_id,
            fencing_token_env="TEST_SUBJECT_FENCE",
            local=LocalBackendSettings(
                database_path=tmp_path / "subject.sqlite3",
                authority_state_path=authority_path,
            ),
        ),
        environment={"TEST_SUBJECT_FENCE": token.fencing_token},
    )
    store = await open_subject_document_store(runtime, initialize_schema=True)
    try:
        yield runtime, store, token
    finally:
        await runtime.close()
        try:
            await registry.revoke(token)
        except StaleAuthorityToken:
            pass


def _command(
    *,
    path: str = "SOUL.md",
    revision: int = 0,
    head: str = "",
    occurrence: str = "observation:soul-v1",
    content: bytes = b"\xef\xbb\xbf# Elysia\r\nexact bytes\r\n",
) -> AppendSubjectDocumentVersion:
    return AppendSubjectDocumentVersion(
        logical_path=path,
        expected_revision=revision,
        expected_head_version_id=head,
        content_bytes=content,
        occurrence_id=occurrence,
        recorded_by="storage-migration",
        recorded_source="snapshot:contract",
        declared_owner="elysia",
        semantic_actor_id=None,
        semantic_source_id=None,
        occurred_at=None,
        provenance_status="semantic_source_missing",
        byte_fidelity="exact_bytes",
        encoding="utf-8-sig",
        newline_style="crlf",
        change_context={"observation": True},
    )


async def test_subject_document_preserves_bytes_provenance_and_head_cas(
    tmp_path: Path,
) -> None:
    async with _local_store(tmp_path) as (runtime, store, _):
        first = await store.append_version(_command())
        assert first.version.content_bytes == _command().content_bytes
        assert first.version.semantic_actor_id is None
        assert first.version.semantic_source_id is None
        assert first.version.provenance_status == "semantic_source_missing"
        assert first.head.revision == 1
        assert first.head.current_version_id == first.version.version_id

        replay = await store.append_version(_command())
        assert replay.version == first.version
        assert replay.head == first.head
        with pytest.raises(SubjectDocumentConflict, match="identity"):
            await store.append_version(_command(content=b"different"))

        second = await store.append_version(
            _command(
                revision=1,
                head=first.version.version_id,
                occurrence="elysia:soul-v2",
                content=b"# Elysia\nsecond\n",
            )
        )
        assert second.version.parent_version_id == first.version.version_id
        assert second.head.revision == 2
        with pytest.raises(SubjectDocumentConflict, match="CAS"):
            await store.append_version(
                _command(
                    revision=1,
                    head=first.version.version_id,
                    occurrence="stale:soul-v3",
                )
            )

        history = await store.list_history("SOUL.md", limit=10)
        assert [item.version_id for item in history] == [
            first.version.version_id,
            second.version.version_id,
        ]
        paged = await store.list_history(
            "SOUL.md",
            after_recorded_at=first.version.recorded_at,
            after_version_id=first.version.version_id,
            limit=10,
        )
        assert paged == [second.version]
        assert await store.get_version(first.version.version_id) == first.version
        assert await store.get_head("SOUL.md") == second.head
        assert await store.list_heads(limit=1) == [second.head]
        assert await store.list_heads(after_logical_path="SOUL.md") == []
        assert await store.list_current_versions() == [second]
        assert await store.health_snapshot() == {
            "status": "healthy",
            "backend": "local",
            "backend_identity": runtime.backend_identity,
            "documents": 1,
            "versions": 2,
            "projection_outbox": {"pending": 2},
        }


async def test_subject_document_concurrent_creation_has_one_winner(
    tmp_path: Path,
) -> None:
    async with _local_store(tmp_path) as (_, store, _):

        async def create(label: str) -> str:
            try:
                await store.append_version(
                    _command(
                        path="USER.md",
                        occurrence=f"observation:user-{label}",
                        content=label.encode(),
                    )
                )
            except SubjectDocumentConflict:
                return "conflict"
            return "committed"

        outcomes = await asyncio.gather(create("a"), create("b"))
        assert sorted(outcomes) == ["committed", "conflict"]
        head = await store.get_head("USER.md")
        assert head is not None and head.revision == 1
        assert len(await store.list_history("USER.md")) == 1


async def test_subject_document_database_history_is_immutable(tmp_path: Path) -> None:
    async with _local_store(tmp_path) as (runtime, store, _):
        committed = await store.append_version(_command())
        assert runtime.engine is not None
        with pytest.raises(DBAPIError, match="SubjectDocumentVersionImmutable"):
            async with runtime.engine.begin() as connection:
                await connection.execute(
                    text(
                        "UPDATE subject_document_versions SET content_hash = '0' "
                        "WHERE version_id = :version_id"
                    ),
                    {"version_id": committed.version.version_id},
                )
        with pytest.raises(DBAPIError, match="SubjectDocumentHeadEventImmutable"):
            async with runtime.engine.begin() as connection:
                await connection.execute(
                    text("DELETE FROM subject_document_head_events")
                )


async def test_subject_projection_claim_confirmation_and_failure_are_cas(
    tmp_path: Path,
) -> None:
    async with _local_store(tmp_path) as (_, store, _):
        await store.append_version(_command())
        task = await store.claim_projection(worker_id="projector-a", lease_seconds=30)
        assert task is not None
        assert task.attempt_count == 1
        assert task.revision == 1
        assert task.lease_owner == "projector-a"
        assert (
            await store.claim_projection(worker_id="projector-b", lease_seconds=30)
            is None
        )
        with pytest.raises(SubjectDocumentConflict, match="confirmation CAS"):
            await store.confirm_projection(task, worker_id="projector-b")
        await store.confirm_projection(task, worker_id="projector-a")
        with pytest.raises(SubjectDocumentConflict, match="confirmation CAS"):
            await store.confirm_projection(task, worker_id="projector-a")
        assert (await store.health_snapshot())["projection_outbox"] == {"confirmed": 1}

        current = await store.get_head("SOUL.md")
        assert current is not None
        await store.append_version(
            _command(
                revision=1,
                head=current.current_version_id,
                occurrence="observation:soul-v2",
                content=b"second",
            )
        )
        failed = await store.claim_projection(worker_id="projector-a", lease_seconds=30)
        assert failed is not None
        await store.fail_projection(
            failed,
            worker_id="projector-a",
            error="external bytes diverged",
        )
        assert (await store.health_snapshot())["projection_outbox"] == {
            "confirmed": 1,
            "failed": 1,
        }
        loaded = await store.get_projection_task("SOUL.md", failed.version_id)
        assert loaded is not None
        assert loaded.state == "failed"


async def test_subject_workspace_projection_and_observation_never_overwrite_divergence(
    tmp_path: Path,
) -> None:
    async with _local_store(tmp_path) as (_, store, _):
        data_root = tmp_path / "data"
        logical_path = "life_engine_workspace/SOUL.md"
        first = await store.append_version(
            _command(path=logical_path, content=b"first\r\n")
        )
        projector = SubjectWorkspaceProjector(
            store,
            data_root=data_root,
            worker_id="workspace-projector",
        )
        first_result = await projector.project_one()
        workspace_file = data_root / logical_path
        assert first_result.status == "projected"
        assert workspace_file.read_bytes() == b"first\r\n"

        second = await store.append_version(
            _command(
                path=logical_path,
                revision=1,
                head=first.version.version_id,
                occurrence="subject:second",
                content=b"second\n",
            )
        )
        assert (await projector.project_one()).status == "projected"
        assert workspace_file.read_bytes() == b"second\n"

        third = await store.append_version(
            _command(
                path=logical_path,
                revision=2,
                head=second.version.version_id,
                occurrence="subject:third",
                content=b"third\n",
            )
        )
        workspace_file.write_bytes(b"external exact bytes\r\n")
        conflict = await projector.project_one()
        assert conflict.status == "failed"
        assert "diverged" in conflict.detail
        assert workspace_file.read_bytes() == b"external exact bytes\r\n"

        observer = SubjectWorkspaceObserver(
            store,
            data_root=data_root,
            recorded_source="workspace:test",
        )
        observed = await observer.observe_file(logical_path)
        assert observed.status == "appended"
        assert observed.commit is not None
        assert observed.commit.version.parent_version_id == third.version.version_id
        assert observed.commit.version.content_bytes == b"external exact bytes\r\n"
        assert observed.commit.version.semantic_actor_id is None
        assert observed.commit.version.semantic_source_id is None
        assert (await projector.project_one()).status == "confirmed_existing"
        assert (await observer.observe_file(logical_path)).status == "unchanged"
        assert workspace_file.read_bytes() == b"external exact bytes\r\n"
        assert len(await store.list_history(logical_path)) == 4
        assert (await store.health_snapshot())["projection_outbox"] == {
            "confirmed": 3,
            "failed": 1,
        }


async def test_targeted_projection_and_service_write_commit_before_workspace(
    tmp_path: Path,
) -> None:
    async with _local_store(tmp_path) as (_, store, _):
        data_root = tmp_path / "data"
        workspace = data_root / "life_engine_workspace"
        workspace.mkdir(parents=True)
        await store.append_version(
            _command(
                path="life_engine_workspace/USER.md",
                occurrence="subject:user-v1",
                content=b"user",
            )
        )
        await store.append_version(
            _command(
                path="life_engine_workspace/SOUL.md",
                occurrence="subject:soul-v1",
                content=b"soul",
            )
        )
        targeted = await store.claim_projection(
            worker_id="targeted-projector",
            lease_seconds=30,
            logical_path="life_engine_workspace/USER.md",
        )
        assert targeted is not None
        assert targeted.logical_path == "life_engine_workspace/USER.md"
        await store.confirm_projection(targeted, worker_id="targeted-projector")

        service = object.__new__(LifeEngineService)
        config = LifeEngineConfig()
        config.settings.workspace_path = str(workspace)
        service.plugin = SimpleNamespace(config=config)
        service._legacy_config_warning_emitted = False
        service._selectable_storage_enabled = True
        service._subject_document_store = store
        service._subject_workspace_observer = SubjectWorkspaceObserver(
            store,
            data_root=data_root,
            recorded_source="workspace:test",
        )
        service._subject_workspace_projector = SubjectWorkspaceProjector(
            store,
            data_root=data_root,
            worker_id="service-projector",
        )
        service._router_context_projection = None
        service._subject_context_projections = {}

        result = await service.write_selected_subject_document(
            workspace_relative_path="MEMORY.md",
            content_bytes=b"# exact memory\r\n",
            occurrence_id="subject:service-memory-v1",
            recorded_by="life-engine-file-tool",
            recorded_source="tool:nucleus_write_file",
            encoding="utf-8",
            semantic_actor_id="elysia",
            semantic_source_id="event:one",
            reason="remember this",
        )

        assert result is not None
        assert result["status"] == "committed"
        assert (workspace / "MEMORY.md").read_bytes() == b"# exact memory\r\n"
        head = await store.get_head("life_engine_workspace/MEMORY.md")
        assert head is not None and head.revision == 1
        version = await store.get_version(head.current_version_id)
        assert version.content_bytes == b"# exact memory\r\n"
        assert version.semantic_actor_id == "elysia"
        assert version.semantic_source_id == "event:one"

        repeated = await service.write_selected_subject_document(
            workspace_relative_path="MEMORY.md",
            content_bytes=b"# exact memory\r\n",
            occurrence_id="subject:service-memory-retry",
            recorded_by="life-engine-file-tool",
            recorded_source="tool:nucleus_write_file",
            encoding="utf-8",
        )
        assert repeated is not None and repeated["status"] == "unchanged"
        repeated_head = await store.get_head("life_engine_workspace/MEMORY.md")
        assert repeated_head is not None and repeated_head.revision == 1

        (workspace / "SOUL.md").write_bytes(b"external soul\n")
        reconciled = await service.write_selected_subject_document(
            workspace_relative_path="SOUL.md",
            content_bytes=b"authoritative soul\n",
            occurrence_id="subject:service-soul-v2",
            recorded_by="life-engine-file-tool",
            recorded_source="tool:nucleus_write_file",
            encoding="utf-8",
            semantic_actor_id="elysia",
            reason="reconcile an external exact-byte change",
        )
        assert reconciled is not None and reconciled["status"] == "committed"
        assert (workspace / "SOUL.md").read_bytes() == b"authoritative soul\n"
        soul_head = await store.get_head("life_engine_workspace/SOUL.md")
        assert soul_head is not None and soul_head.revision == 3
        soul_history = await store.list_history("life_engine_workspace/SOUL.md")
        assert [version.content_bytes for version in soul_history] == [
            b"soul",
            b"external soul\n",
            b"authoritative soul\n",
        ]
        assert (await store.health_snapshot())["projection_outbox"] == {
            "confirmed": 4,
            "failed": 1,
        }


@pytest.mark.parametrize(
    "path",
    ["", "/SOUL.md", "../SOUL.md", "notes/../SOUL.md", "notes\\SOUL.md"],
)
async def test_subject_document_rejects_unsafe_paths(
    tmp_path: Path,
    path: str,
) -> None:
    async with _local_store(tmp_path) as (_, store, _):
        with pytest.raises(ValueError, match="logical_path"):
            await store.append_version(_command(path=path))
