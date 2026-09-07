"""Lifecycle projector wiring against a synthetic fenced store and temp files."""

from __future__ import annotations

import hashlib
import socket
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace

import pytest

from plugins.life_engine.storage import subject_workspace as workspace
from plugins.life_engine.storage.subject_contracts import (
    SubjectDocumentHead,
    SubjectDocumentPathBinding,
    SubjectProjectionTask,
)
from plugins.life_engine.storage.workspace_file_io import WorkspaceFileRecoveryRequired


@pytest.fixture(autouse=True)
def _deny_network(monkeypatch):
    for name in ("connect", "connect_ex", "bind"):
        original = getattr(socket.socket, name)

        def guarded(self, address, *args, _original=original, **kwargs):
            if self.family in (socket.AF_INET, socket.AF_INET6):
                pytest.fail("synthetic projector tests cannot use network or ports")
            return _original(self, address, *args, **kwargs)

        monkeypatch.setattr(socket.socket, name, guarded)


def _sha(content):
    return hashlib.sha256(content).hexdigest()


class _Store:
    def __init__(self, operation="write", content=b"exact", *, parent=None):
        self.inside_fence = False
        self.state = "pending"
        self.errors = []
        path = "life_engine_workspace/registered-arbitrary/file.any"
        previous = "life_engine_workspace/old.any" if operation == "rename" else path
        binding = 2 if operation == "delete" else 1
        self.task = SubjectProjectionTask(
            outbox_id=17,
            head_event_id="head-event:exact",
            document_id="doc-1",
            logical_path=path,
            version_id="version-1",
            content_hash=_sha(content),
            state="pending",
            attempt_count=1,
            lease_owner="test",
            lease_until="later",
            revision=4,
            operation=operation,
            binding_revision=binding,
            previous_logical_path=previous if operation in {"delete", "rename"} else "",
            previous_binding_revision=2 if operation in {"delete", "rename"} else 0,
            previous_version_id="prior"
            if parent is not None
            else ("version-1" if operation in {"delete", "rename"} else ""),
            previous_content_hash=_sha(parent if parent is not None else content)
            if (parent is not None or operation in {"delete", "rename"})
            else "",
        )
        self.head = SubjectDocumentHead(
            document_id="doc-1",
            logical_path=path,
            declared_owner="elysia",
            current_version_id="version-1",
            revision=2,
            binding_revision=binding,
            deleted=operation == "delete",
        )
        self.bindings = {
            path: SubjectDocumentPathBinding(
                path, None if operation == "delete" else "doc-1", binding
            )
        }
        if operation == "rename":
            self.bindings[previous] = SubjectDocumentPathBinding(previous, None, 2)
        self.versions = {
            "version-1": SimpleNamespace(
                document_id="doc-1",
                content_hash=_sha(content),
                content_bytes=content,
                parent_version_id="prior" if parent is not None else "",
            )
        }
        if parent is not None:
            self.versions["prior"] = SimpleNamespace(
                document_id="doc-1",
                content_hash=_sha(parent),
                content_bytes=parent,
                parent_version_id="",
            )

    @asynccontextmanager
    async def workspace_projection_fence(self):
        assert not self.inside_fence
        self.inside_fence = True
        try:
            yield
        finally:
            self.inside_fence = False

    async def claim_projection(self, **kwargs):
        assert not self.inside_fence
        return self.task if self.state == "pending" else None

    async def get_version(self, identity):
        assert self.inside_fence
        return self.versions[identity]

    async def get_document_head(self, identity):
        assert self.inside_fence and identity == "doc-1"
        return self.head

    async def get_path_binding(self, path):
        assert self.inside_fence
        return self.bindings.get(path)

    async def confirm_projection(self, task, **kwargs):
        assert not self.inside_fence and task == self.task
        self.state = "confirmed"

    async def fail_projection(self, task, *, worker_id, error):
        assert not self.inside_fence and task == self.task
        self.errors.append(error)
        self.state = "failed"


def _projector(store, tmp_path):
    return workspace.SubjectWorkspaceProjector(
        store, data_root=tmp_path, worker_id="test"
    )


def _seed(tmp_path, logical_path, content=b"exact"):
    target = tmp_path / logical_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    return target


@pytest.mark.parametrize("operation", ["write", "copy"])
async def test_managed_arbitrary_path_projects_with_exact_task_identity(
    tmp_path, operation, monkeypatch
):
    store = _Store(operation)
    original = workspace.project_exact_bytes

    def fenced_write(*args, **kwargs):
        assert store.inside_fence
        return original(*args, **kwargs)

    monkeypatch.setattr(workspace, "project_exact_bytes", fenced_write)
    result = await _projector(store, tmp_path / "data").project_one()
    assert result.status == "projected", result.detail
    assert result.outbox_id == 17 and result.head_event_id == "head-event:exact"
    assert (tmp_path / "data" / store.task.logical_path).read_bytes() == b"exact"
    assert store.state == "confirmed"


async def test_exact_existing_bytes_confirm_without_inode_replacement(tmp_path):
    store = _Store()
    target = _seed(tmp_path, store.task.logical_path)
    identity = target.stat().st_ino
    result = await _projector(store, tmp_path).project_one()
    assert result.status == "confirmed_existing"
    assert target.stat().st_ino == identity


async def test_write_replaces_only_exact_parent(tmp_path):
    store = _Store(content=b"new", parent=b"old")
    target = _seed(tmp_path, store.task.logical_path, b"old")
    result = await _projector(store, tmp_path).project_one()
    assert result.status == "projected", result.detail
    assert target.read_bytes() == b"new"


async def test_unknown_bytes_fail_without_overwrite_and_can_explicitly_retry(tmp_path):
    store = _Store(content=b"new", parent=b"old")
    target = _seed(tmp_path, store.task.logical_path, b"unknown")
    projector = _projector(store, tmp_path)
    result = await projector.project_one()
    assert result.status == "failed"
    assert "workspace bytes diverged from the authoritative parent" in result.detail
    assert result.outbox_id == 17 and store.state == "failed"
    assert target.read_bytes() == b"unknown"
    assert (await projector.project_one()).status == "idle"
    target.write_bytes(b"old")
    store.state = "pending"  # explicit same-operation retry after caller resolution
    retried = await projector.project_one()
    assert retried.status == "projected"
    assert target.read_bytes() == b"new"


async def test_rename_publishes_new_before_exact_source_removal(tmp_path, monkeypatch):
    store = _Store("rename")
    source = _seed(tmp_path, store.task.previous_logical_path)
    target = tmp_path / store.task.logical_path
    original = workspace.remove_exact_bytes

    def checked_remove(*args, **kwargs):
        assert store.inside_fence
        assert target.read_bytes() == b"exact"
        return original(*args, **kwargs)

    monkeypatch.setattr(workspace, "remove_exact_bytes", checked_remove)
    result = await _projector(store, tmp_path).project_one()
    assert result.status == "renamed", result.detail
    assert not source.exists()
    assert target.read_bytes() == b"exact"


async def test_rename_target_conflict_never_removes_source(tmp_path):
    store = _Store("rename")
    source = _seed(tmp_path, store.task.previous_logical_path)
    target = _seed(tmp_path, store.task.logical_path, b"unknown target")
    result = await _projector(store, tmp_path).project_one()
    assert result.status == "failed"
    assert source.read_bytes() == b"exact"
    assert target.read_bytes() == b"unknown target"


async def test_rename_partial_cleanup_failure_is_retryable_without_republishing(
    tmp_path,
):
    store = _Store("rename")
    source = _seed(tmp_path, store.task.previous_logical_path, b"unknown source")
    target = tmp_path / store.task.logical_path
    projector = _projector(store, tmp_path)
    result = await projector.project_one()
    assert result.status == "failed"
    assert source.read_bytes() == b"unknown source"
    assert target.read_bytes() == b"exact"
    identity = target.stat().st_ino
    source.write_bytes(b"exact")
    store.state = "pending"
    retried = await projector.project_one()
    assert retried.status == "renamed"
    assert target.stat().st_ino == identity
    assert not source.exists()


async def test_rebound_rename_source_with_identical_bytes_is_never_removed(tmp_path):
    store = _Store("rename")
    source = _seed(tmp_path, store.task.previous_logical_path)
    store.bindings[store.task.previous_logical_path] = SubjectDocumentPathBinding(
        store.task.previous_logical_path, "new-document", 3
    )
    result = await _projector(store, tmp_path).project_one()
    assert result.status == "renamed"
    assert result.detail == "previous_path_rebound_preserved"
    assert source.read_bytes() == b"exact"
    assert (tmp_path / store.task.logical_path).read_bytes() == b"exact"


async def test_delete_and_delete_replay_keep_exact_task_identity(tmp_path):
    store = _Store("delete")
    target = _seed(tmp_path, store.task.logical_path)
    projector = _projector(store, tmp_path)
    result = await projector.project_one()
    assert result.status == "deleted", result.detail
    assert result.outbox_id == 17 and result.head_event_id == "head-event:exact"
    assert not target.exists()
    store.state = "pending"
    assert (await projector.project_one()).status == "deleted"


async def test_delete_old_generation_never_deletes_reused_same_bytes(tmp_path):
    store = _Store("delete")
    target = _seed(tmp_path, store.task.logical_path)
    store.bindings[store.task.logical_path] = SubjectDocumentPathBinding(
        store.task.logical_path, "new-document", 3
    )
    result = await _projector(store, tmp_path).project_one()
    assert result.status == "superseded"
    assert target.read_bytes() == b"exact"


async def test_same_version_after_rename_is_not_old_write_confirmation(tmp_path):
    store = _Store()
    store.head = replace(
        store.head, logical_path="life_engine_workspace/moved.any", binding_revision=5
    )
    store.bindings[store.task.logical_path] = SubjectDocumentPathBinding(
        store.task.logical_path, None, 2
    )
    result = await _projector(store, tmp_path).project_one()
    assert result.status == "superseded"
    assert result.outbox_id == 17
    assert not (tmp_path / store.task.logical_path).exists()


@pytest.mark.parametrize(
    "missing", ["head", "binding", "fence", "task_binding", "version_hash"]
)
async def test_incomplete_authority_fails_closed(tmp_path, missing):
    store = _Store()
    if missing == "head":
        store.head = None
    elif missing == "binding":
        store.bindings.clear()
    elif missing == "fence":
        store.workspace_projection_fence = None
    elif missing == "task_binding":
        store.task = replace(store.task, binding_revision=0)
    else:
        store.versions["version-1"].content_hash = _sha(b"other")
    result = await _projector(store, tmp_path).project_one()
    assert result.status == "failed"
    assert not list(tmp_path.iterdir())


async def test_recovery_reference_survives_result_and_failed_task(
    tmp_path, monkeypatch
):
    store = _Store("delete")
    _seed(tmp_path, store.task.logical_path)
    recovery = tmp_path / ".elysium-quarantine-test/original"
    recovery.parent.mkdir(mode=0o700)
    recovery.write_bytes(b"private synthetic unknown content")

    def fail(*args, **kwargs):
        raise WorkspaceFileRecoveryRequired(
            store.task.logical_path, recovery, reason="synthetic conflict"
        )

    monkeypatch.setattr(workspace, "remove_exact_bytes", fail)
    result = await _projector(store, tmp_path).project_one()
    assert result.status == "failed"
    assert str(recovery) in result.detail and "recovery_required" in result.detail
    assert "private synthetic unknown content" not in result.detail
    assert store.errors == [result.detail]
    assert recovery.read_bytes() == b"private synthetic unknown content"
