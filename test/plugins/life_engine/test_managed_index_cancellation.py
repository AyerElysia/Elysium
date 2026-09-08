"""Owned index writes retain namespace fences across cancellation, temp SQLite only."""

from __future__ import annotations

import asyncio
import hashlib
import socket
import sqlite3
import threading
from contextlib import asynccontextmanager, closing
from types import SimpleNamespace

import pytest

from plugins.life_engine.memory import managed_documents, sqlite_runtime


@pytest.fixture(autouse=True)
def _deny_network(monkeypatch):
    for name in ("connect", "connect_ex", "bind"):
        original = getattr(socket.socket, name)

        def guarded(self, address, *args, _original=original, **kwargs):
            if self.family in (socket.AF_INET, socket.AF_INET6):
                pytest.fail(
                    "managed cancellation tests must not use real network/ports"
                )
            return _original(self, address, *args, **kwargs)

        monkeypatch.setattr(socket.socket, name, guarded)


@pytest.mark.parametrize(
    "operation", ["managed", "legacy_upsert", "startup_observation"]
)
@pytest.mark.parametrize("worker_error", [False, True])
async def test_namespace_fence_is_held_until_sqlite_worker_stops_after_repeated_cancel(
    tmp_path, operation, worker_error
):
    loop = asyncio.get_running_loop()
    started, stopped = asyncio.Event(), asyncio.Event()
    release = threading.Event()
    namespace_lock = asyncio.Lock()
    trace: list[str] = []
    state = SimpleNamespace(
        inside=False, worker_name="", committed=False, stopped_inside=False
    )
    database = tmp_path / "synthetic-cancellation.sqlite3"
    logical_path = "life_engine_workspace/notes/cancellation.md"
    content = b"synthetic exact source\n"
    digest = hashlib.sha256(content).hexdigest()

    @asynccontextmanager
    async def fence():
        async with namespace_lock:
            contender = (
                asyncio.current_task().get_name() == "synthetic-namespace-contender"
            )
            owner = "contender" if contender else "parent"
            state.inside = True
            trace.append(f"{owner}_enter")
            try:
                yield
            finally:
                trace.append(f"{owner}_exit")
                state.inside = False

    async def get_document_head(document_id):
        assert state.inside and namespace_lock.locked()
        assert document_id == "synthetic-document"
        return SimpleNamespace(
            document_id=document_id,
            logical_path=logical_path,
            current_version_id="synthetic-version",
            revision=1,
            binding_revision=1,
            deleted=False,
        )

    async def get_version(version_id):
        assert state.inside and namespace_lock.locked()
        assert version_id == "synthetic-version"
        return SimpleNamespace(
            version_id=version_id,
            document_id="synthetic-document",
            content_bytes=content,
            content_hash=digest,
            byte_length=len(content),
            encoding="utf-8",
        )

    async def get_binding(path):
        assert state.inside and namespace_lock.locked()
        assert path == logical_path
        return (
            SimpleNamespace(document_id="synthetic-document", revision=1)
            if operation == "managed"
            else None
        )

    def sqlite_write():
        state.worker_name = threading.current_thread().name
        assert state.worker_name.startswith("life-memory-db")
        try:
            with closing(sqlite3.connect(database, timeout=1)) as connection:
                connection.execute(
                    "CREATE TABLE synthetic_rows (content BLOB NOT NULL)"
                )
                connection.commit()
                connection.execute("BEGIN IMMEDIATE")
                connection.execute("INSERT INTO synthetic_rows VALUES (?)", (content,))
                assert state.inside and namespace_lock.locked()
                trace.append("worker_started")
                loop.call_soon_threadsafe(started.set)
                if not release.wait(timeout=5):
                    raise AssertionError("synthetic worker release deadline exceeded")
                assert state.inside and namespace_lock.locked()
                if worker_error:
                    connection.rollback()
                    trace.append("worker_rolled_back")
                    raise sqlite3.OperationalError("synthetic worker failure")
                connection.commit()
                state.committed = True
                trace.append("worker_committed")
                return True
        finally:
            state.stopped_inside = state.inside and namespace_lock.locked()
            trace.append("worker_stopped")
            loop.call_soon_threadsafe(stopped.set)

    async def managed_write(snapshot):
        assert operation == "managed"
        snapshot.validate()
        assert snapshot.content == content.decode("utf-8")
        assert snapshot.content_sha256 == digest
        return await sqlite_runtime.run_db(sqlite_write)

    async def legacy_write(path, text, title, source_mtime, **kwargs):
        assert operation == "legacy_upsert"
        assert path == "notes/cancellation.md" and text == content.decode("utf-8")
        return await sqlite_runtime.run_db(sqlite_write)

    async def observation_write(**kwargs):
        assert operation == "startup_observation"
        assert kwargs["logical_key"] == "notes/cancellation.md"
        return await sqlite_runtime.run_db(sqlite_write)

    store = SimpleNamespace(
        workspace_namespace_fence=fence,
        get_document_head=get_document_head,
        get_version=get_version,
        get_path_binding=get_binding,
    )
    index = SimpleNamespace(
        project_managed_document=managed_write, upsert_document=legacy_write
    )
    memory = SimpleNamespace(
        _subject_document_store=store,
        _subject_document_store_required=True,
        _require_memory_storage=lambda: SimpleNamespace(document_index=index),
        _append_workspace_observation=observation_write,
    )
    if operation == "managed":
        work = managed_documents.project_current_document(memory, "synthetic-document")
    elif operation == "legacy_upsert":
        work = managed_documents.upsert_document_projection(
            memory,
            "notes/cancellation.md",
            content.decode("utf-8"),
            "synthetic",
            None,
        )
    else:
        work = managed_documents.append_unregistered_observation(
            memory,
            logical_key="notes/cancellation.md",
            content=content.decode("utf-8"),
        )
    parent = asyncio.create_task(work, name="synthetic-managed-cancellation-parent")
    contender = None

    async def contend():
        async with store.workspace_namespace_fence():
            trace.append("contender_acquired")

    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        contender = asyncio.create_task(contend(), name="synthetic-namespace-contender")
        await asyncio.sleep(0)
        for attempt in range(3):
            parent.cancel()
            # Give both the shield and its cancellation-join loop a scheduling turn.
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            trace.append(f"cancel_{attempt}")
            assert not parent.done()
            assert namespace_lock.locked() and state.inside
            assert "parent_exit" not in trace
            assert not contender.done() and "contender_acquired" not in trace
            assert not state.committed and not stopped.is_set()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(parent), timeout=3)
        await asyncio.wait_for(stopped.wait(), timeout=2)
        await asyncio.wait_for(contender, timeout=2)
        assert state.stopped_inside
        assert trace.index("worker_stopped") < trace.index("parent_exit")
        assert trace.index("parent_exit") < trace.index("contender_acquired")
        assert state.committed is (not worker_error)
        assert not namespace_lock.locked()
        assert not any(
            task.get_name() == "managed_file_index_projection" and not task.done()
            for task in asyncio.all_tasks()
        )

        def read_rows():
            with closing(sqlite3.connect(database, timeout=1)) as connection:
                return connection.execute(
                    "SELECT content FROM synthetic_rows"
                ).fetchall()

        rows = await sqlite_runtime.run_db(read_rows)
        assert rows == ([] if worker_error else [(content,)])
    finally:
        # A failing assertion must never strand a dedicated executor worker.
        release.set()
        if not parent.done():
            parent.cancel()
        pending = [parent] if contender is None else [parent, contender]
        await asyncio.wait_for(
            asyncio.gather(*pending, return_exceptions=True), timeout=6
        )
        if started.is_set():
            await asyncio.wait_for(stopped.wait(), timeout=6)
