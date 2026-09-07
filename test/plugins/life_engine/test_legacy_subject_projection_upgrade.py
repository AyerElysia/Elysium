"""Legacy v4 projection binding recovery, using only temporary fenced SQLite."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy import text

from plugins.life_engine.storage import subject_schema
from plugins.life_engine.storage.subject_adapters import SQLSubjectDocumentStore
from plugins.life_engine.storage.subject_contracts import (
    AppendSubjectDocumentVersion,
    SubjectDocumentMutation,
)
from plugins.life_engine.storage.subject_workspace import SubjectWorkspaceProjector
from test.plugins.life_engine.test_s2_schema_upgrade_restore import (
    _NOTE,
    _old_history,
    _runtime,
    _seed_v4,
)


async def _seed_legacy(runtime, state="pending"):
    await _seed_v4(runtime)
    async with runtime.unit_of_work() as uow:
        await uow.session.execute(text(
            "UPDATE subject_projection_outbox SET state = 'confirmed' "
            "WHERE head_event_id = 'event-1'"
        ))
        await uow.session.execute(text(
            "UPDATE subject_projection_outbox SET state = :state, "
            "attempt_count = 7, revision = 11, last_error = :error "
            "WHERE head_event_id = 'event-3'"
        ), {
            "state": state,
            "error": "synthetic legacy failure" if state == "failed" else "",
        })


async def _original_v5_upgrade(runtime, monkeypatch):
    # Freeze the original bug: v5 exists but legacy outbox metadata stayed zero.
    with monkeypatch.context() as patch:
        patch.setattr(
            subject_schema, "_legacy_projection_binding_repair_sql",
            lambda _backend: "SELECT 1",
        )
        await subject_schema.ensure_subject_document_schema(runtime)


async def _legacy_task(store):
    task = await store.get_projection_task(
        _NOTE, "old-v2", occurrence_id="event-occurrence-3",
    )
    assert task is not None
    return task


def _target(root, content):
    target = root / _NOTE
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    return target


def _mutation(head, target, occurrence, *, target_binding=0):
    return SubjectDocumentMutation(
        operation="rename", logical_path=head.logical_path,
        expected_document_id=head.document_id,
        expected_revision=head.revision,
        expected_head_version_id=head.current_version_id,
        expected_binding_revision=head.binding_revision,
        expected_target_binding_revision=target_binding,
        target_logical_path=target,
        occurrence_id=occurrence,
        recorded_by="synthetic-legacy-outbox-test",
        recorded_source="test:rename",
    )


def _create(path, *, occurrence, binding=0, content=b"synthetic new occupant"):
    return AppendSubjectDocumentVersion(
        logical_path=path, expected_revision=0, expected_head_version_id="",
        expected_document_id="", expected_binding_revision=binding,
        content_bytes=content, occurrence_id=occurrence,
        recorded_by="synthetic-legacy-outbox-test",
        recorded_source="test:append",
        provenance_status="semantic_source_missing",
    )


@pytest.mark.parametrize("state", ["pending", "failed"])
@pytest.mark.parametrize("already_v5", [False, True])
async def test_legacy_projection_upgrade_preserves_old_columns_and_recovers(
    tmp_path: Path, monkeypatch, state, already_v5
):
    async with _runtime(tmp_path / "legacy.sqlite3", initialize=False) as runtime:
        await _seed_legacy(runtime, state)
        before = await _old_history(runtime)
        store = SQLSubjectDocumentStore(runtime)
        if already_v5:
            await _original_v5_upgrade(runtime, monkeypatch)
            assert (await _legacy_task(store)).binding_revision == 0
        await subject_schema.ensure_subject_document_schema(runtime)
        assert await _old_history(runtime) == before
        task = await _legacy_task(store)
        assert task.binding_revision == 1 and task.operation == "write"
        assert task.state == state and task.attempt_count == 7 and task.revision == 11
        assert (
            task.previous_logical_path, task.previous_binding_revision,
            task.previous_version_id, task.previous_content_hash,
        ) == ("", 0, "", "")
        await subject_schema.ensure_subject_document_schema(runtime)
        assert await _old_history(runtime) == before
        assert await _legacy_task(store) == task

        root = tmp_path / "data"
        parent = await store.get_version("old-v1")
        current = await store.get_version("old-v2")
        target = _target(root, parent.content_bytes)
        projector = SubjectWorkspaceProjector(store, data_root=root, worker_id="test")
        if state == "failed":
            assert (await projector.project_one()).status == "idle"
            task = await store.retry_projection(task, worker_id="test")
            assert task.binding_revision == 1
        result = await projector.project_one()
        assert result.status == "projected", result.detail
        assert result.outbox_id == task.outbox_id
        assert result.head_event_id == task.head_event_id
        assert target.read_bytes() == current.content_bytes
        assert (await _legacy_task(store)).state == "confirmed"


@pytest.mark.parametrize("return_same_document", [False, True])
async def test_existing_v5_repair_uses_bootstrap_not_reused_current_binding(
    tmp_path: Path, monkeypatch, return_same_document
):
    async with _runtime(tmp_path / "reuse.sqlite3", initialize=False) as runtime:
        await _seed_legacy(runtime, "failed")
        await _original_v5_upgrade(runtime, monkeypatch)
        store = SQLSubjectDocumentStore(runtime)
        original = await store.get_head(_NOTE)
        moved = await store.mutate_document(_mutation(
            original, "life_engine_workspace/moved.bin", "test:legacy-move",
        ))
        if return_same_document:
            await store.mutate_document(_mutation(
                moved.head, _NOTE, "test:legacy-return", target_binding=2,
            ))
        else:
            await store.append_version(_create(
                _NOTE, occurrence="test:new-occupant", binding=2,
            ))
        binding = await store.get_path_binding(_NOTE)
        assert binding.revision == 3
        before = await _old_history(runtime)
        await subject_schema.ensure_subject_document_schema(runtime)
        assert await _old_history(runtime) == before
        task = await _legacy_task(store)
        assert task.binding_revision == 1
        assert (await store.get_path_binding(_NOTE)) == binding
        await store.retry_projection(task, worker_id="test")

        root = tmp_path / "data"
        target = _target(root, b"synthetic current occupant bytes must not change")
        original_bytes = target.read_bytes()
        result = await SubjectWorkspaceProjector(
            store, data_root=root, worker_id="test"
        ).project_one(logical_path=_NOTE)
        assert result.status == "superseded", result.detail
        assert result.outbox_id == task.outbox_id
        assert target.read_bytes() == original_bytes


async def test_legacy_failed_task_keeps_unknown_bytes_failure_until_explicit_resolution(
    tmp_path: Path,
):
    async with _runtime(tmp_path / "unknown.sqlite3", initialize=False) as runtime:
        await _seed_legacy(runtime, "failed")
        before = await _old_history(runtime)
        await subject_schema.ensure_subject_document_schema(runtime)
        assert await _old_history(runtime) == before
        store = SQLSubjectDocumentStore(runtime)
        task = await _legacy_task(store)
        root = tmp_path / "data"
        target = _target(root, b"synthetic unknown bytes")
        projector = SubjectWorkspaceProjector(store, data_root=root, worker_id="test")
        assert (await projector.project_one()).status == "idle"
        await store.retry_projection(task, worker_id="test")
        result = await projector.project_one()
        assert result.status == "failed"
        assert "workspace bytes diverged from the authoritative parent" in result.detail
        assert target.read_bytes() == b"synthetic unknown bytes"
        failed = await _legacy_task(store)
        assert failed.state == "failed" and failed.binding_revision == 1
        assert (await projector.project_one()).status == "idle"

        # Explicit synthetic conflict resolution restores a known exact parent.
        target.write_bytes((await store.get_version("old-v1")).content_bytes)
        await store.retry_projection(failed, worker_id="test")
        assert (await projector.project_one()).status == "projected"
        assert target.read_bytes() == (await store.get_version("old-v2")).content_bytes


@pytest.mark.parametrize(("field", "value"), [
    ("operation", "copy"),
    ("operation", "WRITE"),
    ("previous_logical_path", _NOTE),
    ("previous_binding_revision", 2),
    ("previous_version_id", "old-v1"),
    ("previous_content_hash", "a" * 64),
    ("binding_revision", 7),
])
async def test_partial_or_nonlegacy_metadata_is_not_reinterpreted(
    tmp_path: Path, monkeypatch, field, value
):
    async with _runtime(tmp_path / "partial.sqlite3", initialize=False) as runtime:
        await _seed_legacy(runtime)
        await _original_v5_upgrade(runtime, monkeypatch)
        async with runtime.unit_of_work() as uow:
            await uow.session.execute(text(
                f"UPDATE subject_projection_outbox SET {field} = :value "
                "WHERE head_event_id = 'event-3'"
            ), {"value": value})
        before = await _old_history(runtime)
        store = SQLSubjectDocumentStore(runtime)
        task = await _legacy_task(store)
        await subject_schema.ensure_subject_document_schema(runtime)
        assert await _old_history(runtime) == before
        assert await _legacy_task(store) == task


@pytest.mark.parametrize(("field", "value"), [
    ("logical_path", "life_engine_workspace/not-bootstrap.bin"),
    ("content_hash", "a" * 64),
    ("version_id", "old-v1"),
])
async def test_inconsistent_immutable_reference_closure_is_not_enriched(
    tmp_path: Path, monkeypatch, field, value
):
    async with _runtime(tmp_path / "closure.sqlite3", initialize=False) as runtime:
        await _seed_legacy(runtime)
        await _original_v5_upgrade(runtime, monkeypatch)
        async with runtime.unit_of_work() as uow:
            await uow.session.execute(text(
                f"UPDATE subject_projection_outbox SET {field} = :value "
                "WHERE head_event_id = 'event-3'"
            ), {"value": value})
        before = await _old_history(runtime)
        await subject_schema.ensure_subject_document_schema(runtime)
        assert await _old_history(runtime) == before
        async with runtime.unit_of_work() as uow:
            binding = await uow.session.scalar(text(
                "SELECT binding_revision FROM subject_projection_outbox "
                "WHERE head_event_id = 'event-3'"
            ))
        assert binding == 0


async def test_modern_registered_write_on_bootstrapped_document_is_not_legacy(
    tmp_path: Path, monkeypatch
):
    async with _runtime(tmp_path / "modern.sqlite3", initialize=False) as runtime:
        await _seed_legacy(runtime)
        await _original_v5_upgrade(runtime, monkeypatch)
        store = SQLSubjectDocumentStore(runtime)
        head = await store.get_head(_NOTE)
        modern = await store.append_version(replace(
            _create(_NOTE, occurrence="test:modern"),
            expected_document_id=head.document_id,
            expected_revision=head.revision,
            expected_head_version_id=head.current_version_id,
            expected_binding_revision=head.binding_revision,
        ))
        async with runtime.unit_of_work() as uow:
            await uow.session.execute(text(
                "UPDATE subject_projection_outbox SET binding_revision = 0 "
                "WHERE version_id = :version"
            ), {"version": modern.version.version_id})
        before = await _old_history(runtime)
        await subject_schema.ensure_subject_document_schema(runtime)
        assert await _old_history(runtime) == before
        assert (await _legacy_task(store)).binding_revision == 1
        task = await store.get_projection_task(_NOTE, modern.version.version_id)
        assert task is not None and task.binding_revision == 0
