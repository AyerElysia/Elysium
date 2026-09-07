"""Offline identity, lifecycle, and migration invariants using temporary SQLite."""

from __future__ import annotations

import asyncio
import hashlib
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from plugins.life_engine.storage.contracts import (
    StorageBackendRuntime,
    StorageWriterRole,
)
from plugins.life_engine.storage.migration.subject_history import (
    capture_subject_history,
    import_subject_history_bundle,
    verify_subject_history_bundle,
)
from plugins.life_engine.storage.models import BackendKind
from plugins.life_engine.storage.subject_adapters import SQLSubjectDocumentStore
from plugins.life_engine.storage.subject_contracts import (
    AppendSubjectDocumentVersion,
    SubjectDocumentConflict,
    SubjectDocumentMutation,
    SubjectDocumentNotFound,
)
from plugins.life_engine.storage.subject_schema import (
    LOCAL_SUBJECT_SCHEMA_STATEMENTS,
    ensure_subject_document_schema,
)


async def _no_fence(*_args) -> None:
    """Only this test's private, temporary database has no external authority."""


@asynccontextmanager
async def _store(tmp_path: Path, *, initialize: bool = True):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'lifecycle.sqlite3'}")
    runtime = StorageBackendRuntime(
        enabled=True, backend=BackendKind.LOCAL, backend_identity="test://temporary",
        generation=None, authority_registry=None, authority_token=None,
        engine=engine, session_factory=async_sessionmaker(engine, expire_on_commit=False),
        _write_fence=_no_fence, _writer_validator=_no_fence,
    )
    try:
        if initialize:
            await ensure_subject_document_schema(runtime)
        yield runtime, SQLSubjectDocumentStore(runtime)
    finally:
        await runtime.close()


def _create(path: str = "notes/a.bin", *, occurrence: str = "create:a",
            content: bytes = b"\x00\xff\xef\xbb\xbf\r\n") -> AppendSubjectDocumentVersion:
    return AppendSubjectDocumentVersion(
        logical_path=path, expected_revision=0, expected_head_version_id="",
        expected_document_id="", expected_binding_revision=0,
        content_bytes=content, occurrence_id=occurrence,
        recorded_by="test-recorder", recorded_source="test-tool-call",
        declared_owner="elysia", semantic_actor_id=None, semantic_source_id=None,
        provenance_status="semantic_source_missing",
        change_context={"file_tool_request_sha256": occurrence},
    )


def _mutation(head, operation: str, *, occurrence: str, target: str = "", **kwargs):
    return SubjectDocumentMutation(
        operation=operation, logical_path=head.logical_path,
        expected_document_id=head.document_id, expected_revision=head.revision,
        expected_head_version_id=head.current_version_id,
        expected_binding_revision=head.binding_revision,
        occurrence_id=occurrence, recorded_by="test-recorder",
        recorded_source="test-tool-call", target_logical_path=target,
        semantic_actor_id="active-consciousness", semantic_source_id="tool-call",
        occurred_at="2026-09-07T00:00:00+00:00",
        change_context={"file_tool_request_sha256": occurrence}, **kwargs,
    )


async def test_rename_reuse_and_stable_operation_replay(tmp_path: Path) -> None:
    async with _store(tmp_path) as (runtime, store):
        command = _create()
        first = await store.append_version(command)
        moved = await store.mutate_document(_mutation(
            first.head, "rename", occurrence="rename:a", target="notes/b.bin",
        ))
        assert moved.head.document_id == first.head.document_id
        assert moved.version == first.version
        assert moved.head.logical_path == "notes/b.bin"
        assert moved.version.logical_path == "notes/a.bin"
        assert await store.get_head("notes/a.bin") is None
        released = await store.get_path_binding("notes/a.bin")
        assert released.document_id is None and released.revision == 2
        recreated = await store.append_version(replace(
            _create(occurrence="create:new-a"), expected_binding_revision=2,
        ))
        assert recreated.head.document_id != first.head.document_id
        assert recreated.version.version_id != first.version.version_id
        replay = await store.append_version(command)
        assert replay == first
        assert (await store.get_head("notes/a.bin")).document_id == recreated.head.document_id
        assert await store.list_document_history(first.head.document_id) == [first.version]
        assert await store.list_history("notes/a.bin") == [recreated.version]
        assert await store.list_history("notes/b.bin") == [first.version]
        assert {item.head.document_id for item in await store.list_current_versions()} == {
            first.head.document_id, recreated.head.document_id,
        }
        with pytest.raises(SubjectDocumentConflict):
            await store.append_version(replace(
                command, expected_revision=first.head.revision,
                expected_head_version_id=first.version.version_id,
                expected_document_id=first.head.document_id,
                expected_binding_revision=first.head.binding_revision,
                occurrence_id="stale:a",
            ))
        with pytest.raises(SubjectDocumentConflict, match="identity"):
            await store.append_version(replace(command, content_bytes=b"different"))
        await ensure_subject_document_schema(runtime)
        assert (await store.get_head("notes/b.bin")).document_id == first.head.document_id
        assert (await store.get_path_binding("notes/a.bin")).revision == 3


async def test_delete_retains_history_and_exact_operation_projection(tmp_path: Path) -> None:
    async with _store(tmp_path) as (_, store):
        first = await store.append_version(_create())
        moved = await store.mutate_document(_mutation(
            first.head, "rename", occurrence="rename:a", target="notes/b.bin",
        ))
        delete_command = _mutation(moved.head, "delete", occurrence="delete:b")
        deleted = await store.mutate_document(delete_command)
        assert deleted.head.deleted
        assert await store.get_head("notes/b.bin") is None
        assert await store.get_document_head(first.head.document_id) == deleted.head
        assert await store.get_version(first.version.version_id) == first.version
        assert await store.list_document_history(first.head.document_id) == [first.version]
        renamed_task = await store.get_projection_task(
            "notes/b.bin", first.version.version_id, occurrence_id="rename:a",
        )
        deleted_task = await store.get_projection_task(
            "notes/b.bin", first.version.version_id, occurrence_id="delete:b",
        )
        assert renamed_task.operation == "rename"
        assert deleted_task.operation == "delete"
        assert renamed_task.head_event_id != deleted_task.head_event_id
        assert deleted_task.previous_content_hash == first.version.content_hash
        assert deleted_task.previous_version_id == first.version.version_id
        assert (await store.get_projection_task(
            "notes/b.bin", first.version.version_id,
        )).outbox_id == deleted_task.outbox_id
        replay = await store.mutate_document(delete_command)
        assert replay.idempotent_replay and replay.head == deleted.head
        receipt = await store.get_document_operation("delete:b")
        assert receipt.result["head"]["deleted"] is True
        assert receipt.change_context["operation_actor_id"] == "active-consciousness"
        operations = await store.list_document_operations(first.head.document_id)
        assert {item.operation for item in operations} == {"write", "rename", "delete"}


async def test_copy_preserves_original_attribution_and_records_copy_actor(tmp_path: Path) -> None:
    async with _store(tmp_path) as (_, store):
        first = await store.append_version(_create())
        command = _mutation(
            first.head, "copy", occurrence="copy:a", target="notes/copy.bin",
        )
        copied = await store.mutate_document(command)
        assert copied.head.document_id != first.head.document_id
        assert copied.version.content_bytes == first.version.content_bytes
        assert copied.version.content_hash == first.version.content_hash
        assert copied.version.semantic_actor_id is None
        assert copied.version.semantic_source_id is None
        assert copied.version.occurred_at is None
        assert copied.version.provenance_status == "semantic_source_missing"
        assert copied.version.change_context["copied_from_version_id"] == first.version.version_id
        assert copied.version.change_context["copy_actor_id"] == "active-consciousness"
        assert await store.get_document_head(first.head.document_id) == first.head
        assert (await store.mutate_document(command)).idempotent_replay
        edited_copy = await store.mutate_document(_mutation(
            first.head, "copy", occurrence="copy:edited", target="notes/edited.bin",
            content_bytes=b"\xfe\x00new", encoding=None,
        ))
        assert edited_copy.version.content_bytes == b"\xfe\x00new"
        assert edited_copy.version.semantic_actor_id == "active-consciousness"


async def test_rename_with_replacement_bytes_is_one_revision(tmp_path: Path) -> None:
    async with _store(tmp_path) as (_, store):
        first = await store.append_version(_create())
        command = _mutation(
            first.head, "rename", occurrence="move-and-write",
            target="notes/revised.bin", content_bytes=b"\x00changed\r\n",
        )
        moved = await store.mutate_document(command)
        assert moved.head.document_id == first.head.document_id
        assert moved.head.revision == first.head.revision + 1
        assert moved.version.parent_version_id == first.version.version_id
        assert moved.version.content_bytes == b"\x00changed\r\n"
        assert moved.version.semantic_actor_id == "active-consciousness"
        assert len(await store.list_document_history(first.head.document_id)) == 2
        task = await store.get_projection_task(
            moved.head.logical_path, moved.version.version_id,
            occurrence_id="move-and-write",
        )
        assert task.previous_content_hash == first.version.content_hash
        assert task.content_hash == moved.version.content_hash
        assert (await store.mutate_document(command)).head == moved.head


async def test_batch_conflict_and_lost_fence_roll_back_every_row(tmp_path: Path) -> None:
    async with _store(tmp_path) as (runtime, store):
        first = await store.append_version(_create())
        with pytest.raises(SubjectDocumentConflict):
            await store.apply_document_batch([
                _create("notes/second.bin", occurrence="batch:second"),
                replace(_create(occurrence="batch:stale"), expected_revision=99),
            ])
        assert await store.get_head("notes/second.bin") is None
        assert await store.get_document_operation("batch:second") is None
        with pytest.raises(ValueError, match="disjoint"):
            await store.apply_document_batch([
                _create("notes/second.bin", occurrence="overlap:first"),
                _create("notes/second.bin", occurrence="overlap:second"),
            ])

        async def lost_fence(*_args):
            raise RuntimeError("test writer fence lost")

        runtime._write_fence = lost_fence
        with pytest.raises(RuntimeError, match="fence lost"):
            await store.mutate_document(_mutation(
                first.head, "rename", occurrence="fenced:rename", target="notes/lost.bin",
            ))
        runtime._write_fence = _no_fence
        assert await store.get_document_head(first.head.document_id) == first.head
        assert await store.get_path_binding("notes/lost.bin") is None
        assert await store.get_document_operation("fenced:rename") is None
        commands = [
            _create("notes/second.bin", occurrence="batch:good-second"),
            _create("notes/third.bin", occurrence="batch:good-third"),
        ]
        assert await store.apply_document_batch(commands) == await store.apply_document_batch(commands)


@pytest.mark.parametrize("operation", ["rename", "delete", "copy"])
async def test_fixed_authority_slots_reject_generic_lifecycle(
    tmp_path: Path, operation: str,
) -> None:
    async with _store(tmp_path) as (_, store):
        first = await store.append_version(_create(
            "life_engine_workspace/SOUL.md", occurrence="fixed:soul",
        ))
        with pytest.raises(SubjectDocumentConflict, match="fixed subject"):
            await store.mutate_document(_mutation(
                first.head, operation, occurrence=f"fixed:{operation}",
                target="" if operation == "delete" else "notes/elsewhere.bin",
            ))
        assert await store.get_head(first.head.logical_path) == first.head


async def test_v4_upgrade_preserves_old_identity_bytes_and_missing_provenance(
    tmp_path: Path,
) -> None:
    async with _store(tmp_path, initialize=False) as (runtime, store):
        legacy_head = """CREATE TABLE subject_documents (
            document_id TEXT PRIMARY KEY, logical_path TEXT NOT NULL UNIQUE,
            declared_owner TEXT NULL, current_version_id TEXT NOT NULL DEFAULT '',
            revision INTEGER NOT NULL DEFAULT 0)"""
        content = b"\xef\xbb\xbflegacy\r\n\xff"
        async with runtime.unit_of_work() as uow:
            for statement in (legacy_head, *LOCAL_SUBJECT_SCHEMA_STATEMENTS[1:]):
                await uow.session.execute(text(statement))
            await uow.session.execute(text(
                """INSERT INTO subject_documents VALUES
                ('doc_legacy', 'notes/legacy.bin', NULL, 'ver_legacy', 7)"""
            ))
            await uow.session.execute(text(
                """INSERT INTO subject_document_versions
                (version_id, document_id, logical_path, parent_version_id,
                 occurrence_id, semantic_actor_id, semantic_source_id, occurred_at,
                 recorded_by, recorded_source, recorded_at, provenance_status,
                 content_bytes, content_hash, byte_length, byte_fidelity,
                 encoding, newline_style, change_context_json)
                VALUES ('ver_legacy', 'doc_legacy', 'notes/legacy.bin', '',
                 'legacy-occurrence', NULL, NULL, NULL, 'legacy-import', 'snapshot',
                 '2025-01-01T00:00:00+00:00', 'semantic_source_missing',
                 :content, :content_hash, :length, 'exact_bytes', NULL, NULL, '{}')"""
            ), {"content": content, "content_hash": hashlib.sha256(content).hexdigest(),
                "length": len(content)})
        before = await store.get_version("ver_legacy")
        await ensure_subject_document_schema(runtime)
        await ensure_subject_document_schema(runtime)
        assert await store.get_version("ver_legacy") == before
        head = await store.get_head("notes/legacy.bin")
        assert head.document_id == "doc_legacy" and head.revision == 7
        assert head.binding_revision == 1
        async with runtime.unit_of_work() as uow:
            assert (await uow.session.execute(text("PRAGMA foreign_key_check"))).first() is None
            assert await uow.session.scalar(text("PRAGMA foreign_keys")) == 1
        with pytest.raises(DBAPIError, match="Immutable"):
            async with runtime.unit_of_work() as uow:
                await uow.session.execute(text(
                    "DELETE FROM subject_document_path_events WHERE document_id = 'doc_legacy'"
                ))
        moved = await store.mutate_document(_mutation(
            head, "rename", occurrence="legacy:rename", target="notes/legacy-moved.bin",
        ))
        assert moved.head.document_id == "doc_legacy" and moved.version == before


async def test_version_descriptors_page_without_loading_content(tmp_path: Path) -> None:
    async with _store(tmp_path) as (runtime, store):
        first = await store.append_version(_create())
        second = await store.append_version(replace(
            _create(occurrence="descriptor:second", content=b"second"),
            expected_revision=first.head.revision,
            expected_head_version_id=first.version.version_id,
            expected_document_id=first.head.document_id,
            expected_binding_revision=first.head.binding_revision,
        ))
        queries: list[str] = []

        def capture(_connection, _cursor, statement, _parameters, _context, _many):
            queries.append(statement)

        event.listen(runtime.engine.sync_engine, "before_cursor_execute", capture)
        try:
            first_page = await store.list_document_version_descriptors(
                first.head.document_id, limit=1,
            )
            second_page = await store.list_document_version_descriptors(
                first.head.document_id, after_recorded_at=first_page[0]["recorded_at"],
                after_version_id=first_page[0]["version_id"], limit=1,
            )
            descriptor = await store.get_version_descriptor(first.version.version_id)
        finally:
            event.remove(runtime.engine.sync_engine, "before_cursor_execute", capture)
        assert {row["version_id"] for row in first_page + second_page} == {
            first.version.version_id, second.version.version_id,
        }
        assert all("content_bytes" not in statement.lower() for statement in queries)
        assert all("content_bytes" not in row for row in first_page + second_page)
        assert first_page[0]["semantic_actor_id"] is None
        assert first_page[0]["occurred_at"] is None
        assert descriptor["version_id"] == first.version.version_id
        assert descriptor["byte_fidelity"] == first.version.byte_fidelity
        assert descriptor["encoding"] is None
        assert descriptor["recorded_by"] == first.version.recorded_by
        assert descriptor["recorded_source"] == first.version.recorded_source
        with pytest.raises(SubjectDocumentNotFound):
            await store.get_version_descriptor("ver_missing")


async def test_projection_fence_keeps_reads_live_and_blocks_same_store_writes(
    tmp_path: Path,
) -> None:
    async with _store(tmp_path) as (_, store):
        first = await store.append_version(_create())
        async with store.workspace_projection_fence():
            assert await asyncio.wait_for(
                store.get_head(first.head.logical_path), timeout=1,
            ) == first.head
            assert await asyncio.wait_for(
                store.get_version(first.version.version_id), timeout=1,
            ) == first.version
            assert (await asyncio.wait_for(
                store.get_path_binding(first.head.logical_path), timeout=1,
            )).document_id == first.head.document_id
            pending = asyncio.create_task(store.append_version(
                _create("notes/waiting.bin", occurrence="fence:waiting"),
            ))
            await asyncio.sleep(0.03)
            assert not pending.done()
            with pytest.raises(RuntimeError, match="forbidden inside"):
                await store.append_version(_create(
                    "notes/forbidden.bin", occurrence="fence:nested-write",
                ))
            with pytest.raises(RuntimeError, match="forbidden inside"):
                async with store.workspace_projection_fence():
                    pytest.fail("nested fence must fail before yielding")
        assert (await asyncio.wait_for(pending, timeout=1)).head.logical_path == "notes/waiting.bin"


async def test_projection_fence_blocks_another_adapter_without_blocking_event_loop(
    tmp_path: Path,
) -> None:
    async with _store(tmp_path) as (_, store), _store(tmp_path) as (_, other):
        first = await store.append_version(_create())
        async with store.workspace_projection_fence():
            pending = asyncio.create_task(other.mutate_document(_mutation(
                first.head, "delete", occurrence="cross-adapter:delete",
            )))
            await asyncio.sleep(0.05)
            assert not pending.done()
            assert await asyncio.wait_for(
                other.get_head(first.head.logical_path), timeout=1,
            ) == first.head
        assert (await asyncio.wait_for(pending, timeout=1)).head.deleted


async def test_projection_fence_cancellation_and_gate_deadline_release(
    tmp_path: Path, monkeypatch,
) -> None:
    from plugins.life_engine.storage import subject_adapters

    async with _store(tmp_path) as (_, store):
        entered = asyncio.Event()

        async def project_forever():
            async with store.workspace_projection_fence():
                entered.set()
                await asyncio.Event().wait()

        task = asyncio.create_task(project_forever())
        await asyncio.wait_for(entered.wait(), timeout=1)
        monkeypatch.setattr(subject_adapters, "_LOCAL_WRITE_GATE_TIMEOUT_SECONDS", 0.01)
        with pytest.raises(SubjectDocumentConflict, match="deadline"):
            await store.append_version(_create(occurrence="deadline:never-committed"))
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        created = await asyncio.wait_for(
            store.append_version(_create(occurrence="after:cancel")), timeout=1,
        )
        assert created.head.revision == 1
        assert await store.get_document_operation("deadline:never-committed") is None
        store.backend = BackendKind.MYSQL
        with pytest.raises(RuntimeError, match="LOCAL-only"):
            async with store.workspace_projection_fence():
                pytest.fail("MYSQL must not acquire a workspace fence")


async def test_file_binding_inventory_keeps_released_paths_and_literal_prefix(
    tmp_path: Path,
) -> None:
    async with _store(tmp_path) as (runtime, store):
        first = await store.append_version(_create(
            "notes/a_%/file.bin", occurrence="inventory:literal",
        ))
        await store.append_version(_create(
            "notes/aXX/file.bin", occurrence="inventory:not-literal",
        ))
        moved = await store.mutate_document(_mutation(
            first.head, "rename", occurrence="inventory:move",
            target="notes/moved.bin",
        ))
        queries: list[str] = []

        def capture(_connection, _cursor, statement, _parameters, _context, _many):
            queries.append(statement)

        event.listen(runtime.engine.sync_engine, "before_cursor_execute", capture)
        try:
            rows = await store.list_file_bindings(logical_path_prefix="notes/a_%/")
            first_page = await store.list_file_bindings(limit=1)
            rest = await store.list_file_bindings(
                after_logical_path=first_page[0]["logical_path"],
            )
        finally:
            event.remove(runtime.engine.sync_engine, "before_cursor_execute", capture)
        assert len(rows) == 1
        assert rows[0]["logical_path"] == "notes/a_%/file.bin"
        assert rows[0]["document_id"] is None
        assert rows[0]["binding_revision"] == 2
        assert rows[0]["current_version_id"] is None
        assert rows[0]["document_revision"] == 0
        assert len(first_page + rest) == 3
        current = next(row for row in rest if row["logical_path"] == moved.head.logical_path)
        assert current["current_version_id"] == first.version.version_id
        assert current["byte_length"] == first.version.byte_length
        assert all("content_bytes" not in statement.lower() for statement in queries)


async def test_real_adapter_lifecycle_history_round_trips_into_candidate(
    tmp_path: Path,
) -> None:
    source_path, target_path = tmp_path / "source", tmp_path / "target"
    source_path.mkdir()
    target_path.mkdir()
    async with _store(source_path) as (source, store):
        first = await store.append_version(_create())
        second = await store.append_version(replace(
            _create(occurrence="archive:second", content=b"second\r\n"),
            expected_revision=first.head.revision,
            expected_head_version_id=first.version.version_id,
            expected_document_id=first.head.document_id,
            expected_binding_revision=first.head.binding_revision,
        ))
        copied = await store.mutate_document(_mutation(
            second.head, "copy", occurrence="archive:copy", target="notes/copy.bin",
        ))
        moved = await store.mutate_document(_mutation(
            second.head, "rename", occurrence="archive:rename",
            target="notes/moved.bin", content_bytes=b"\xffrenamed\x00",
        ))
        deleted = await store.mutate_document(_mutation(
            moved.head, "delete", occurrence="archive:delete",
        ))
        reused = await store.append_version(replace(
            _create(occurrence="archive:reused"), expected_binding_revision=2,
        ))
        payload = await capture_subject_history(source)
        verified = verify_subject_history_bundle(payload)
        assert verified.table_counts["subject_document_versions"] == 5
        assert verified.table_counts["subject_document_head_events"] == 6
        assert verified.table_counts["subject_projection_outbox"] == 6
        async with _store(target_path) as (target, imported):
            target.writer_role = StorageWriterRole.CANDIDATE_COPY
            report = await import_subject_history_bundle(payload, target)
            assert report.table_roots == verified.table_roots
            assert (await import_subject_history_bundle(payload, target)).idempotent_replay
            for commit in (first, second, copied, moved, reused):
                assert await imported.get_version(commit.version.version_id) == commit.version
            assert await imported.get_document_head(deleted.head.document_id) == deleted.head
            assert await imported.list_file_bindings() == await store.list_file_bindings()
            assert await imported.get_document_operation("archive:rename") == (
                await store.get_document_operation("archive:rename")
            )


async def test_file_path_shape_rejects_bound_ancestors_descendants_and_batch_overlap(
    tmp_path: Path,
) -> None:
    async with _store(tmp_path) as (_, store):
        parent = await store.append_version(_create(
            "notes/parent.md", occurrence="shape:parent",
        ))
        child = await store.append_version(_create(
            "notes/tree/leaf.md", occurrence="shape:leaf",
        ))
        with pytest.raises(SubjectDocumentConflict, match="ancestor"):
            await store.append_version(_create(
                "notes/parent.md/child.md", occurrence="shape:invalid-child",
            ))
        with pytest.raises(SubjectDocumentConflict, match="descendant"):
            await store.append_version(_create(
                "notes/tree", occurrence="shape:invalid-parent",
            ))
        with pytest.raises(SubjectDocumentConflict, match="ancestor"):
            await store.mutate_document(_mutation(
                child.head, "rename", occurrence="shape:invalid-rename",
                target="notes/parent.md/renamed.md",
            ))
        with pytest.raises(SubjectDocumentConflict, match="descendant"):
            await store.mutate_document(_mutation(
                parent.head, "copy", occurrence="shape:invalid-copy",
                target="notes/tree",
            ))
        with pytest.raises(ValueError, match="non-hierarchical"):
            await store.apply_document_batch([
                _create("notes/batch", occurrence="shape:batch-parent"),
                _create("notes/batch/child", occurrence="shape:batch-child"),
            ])
        assert await store.get_head("notes/batch") is None
        # Prefix neighbours and case variants are not ancestors.
        await store.append_version(_create("notes/tree0", occurrence="shape:neighbour"))
        await store.append_version(_create("notes/Parent.md/child", occurrence="shape:case"))
        assert await store.get_head(parent.head.logical_path) == parent.head


async def test_two_adapters_cannot_concurrently_bind_parent_and_child(tmp_path: Path) -> None:
    async with _store(tmp_path) as (_, store), _store(tmp_path) as (_, other):
        results = await asyncio.gather(
            store.append_version(_create("notes/race", occurrence="shape:race-parent")),
            other.append_version(_create("notes/race/child", occurrence="shape:race-child")),
            return_exceptions=True,
        )
        assert sum(isinstance(item, SubjectDocumentConflict) for item in results) == 1
        assert sum(not isinstance(item, BaseException) for item in results) == 1
        assert not (
            await store.get_head("notes/race")
            and await store.get_head("notes/race/child")
        )


async def test_mysql_namespace_mutex_contract_model_without_database_connection() -> None:
    calls: list[str] = []
    schema_version = 5

    async def scalar(statement):
        assert "subject_document_schema_migrations" in str(statement)
        assert "WHERE version = 5 FOR UPDATE" in str(statement)
        calls.append("schema-lock")
        return schema_version

    async def validate(_session):
        calls.append("authority-check")

    @asynccontextmanager
    async def unit_of_work():
        calls.append("begin")
        try:
            yield SimpleNamespace(session=SimpleNamespace(scalar=scalar))
        except BaseException:
            calls.append("rollback")
            raise
        else:
            calls.append("commit")

    runtime = SimpleNamespace(
        enabled=True, backend=BackendKind.MYSQL, engine=object(),
        unit_of_work=unit_of_work, _write_fence=validate,
    )
    store = SQLSubjectDocumentStore(runtime)

    async def body(_session):
        calls.append("body")
        return "written"

    assert await store._write(body) == "written"
    assert calls == ["begin", "schema-lock", "body", "commit"]
    calls.clear()
    async with store.workspace_namespace_fence():
        calls.append("publish")
        with pytest.raises(RuntimeError, match="forbidden inside"):
            await store._write(body)
    assert calls == ["begin", "schema-lock", "authority-check", "publish", "commit"]
    schema_version = None
    calls.clear()
    with pytest.raises(SubjectDocumentConflict, match="guard is missing"):
        await store._write(body)
    assert calls == ["begin", "schema-lock", "rollback"]
