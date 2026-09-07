"""Actual service startup ordering with synthetic ports and owned idle tasks."""

from __future__ import annotations

import asyncio
import socket
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from plugins.life_engine.memory import service as memory_module
from plugins.life_engine.memory.managed_documents import selected_subject_store
from plugins.life_engine.service import core as core_module
from plugins.life_engine.service.integrations import MemoryIntegration
from plugins.life_engine.storage import subject_factory
from plugins.life_engine.storage.models import BackendKind
from test.plugins.life_engine.presence_world_fakes import build_fake_stores
from test.plugins.life_engine.test_selected_presence_world_service import (
    _FakeLifeEventStore,
    _install_selected_factories,
    _selected_service,
)

_REAL_SUBJECT_FACTORY = subject_factory.open_subject_document_store
_REAL_MEMORY_CLOSE = memory_module.LifeMemoryService.close


@pytest.fixture(autouse=True)
def _deny_external_effects(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("startup-order tests must not touch real DB/network/schema")

    monkeypatch.setattr(sqlite3, "connect", forbidden)
    monkeypatch.setattr(subject_factory, "ensure_subject_document_schema", forbidden)
    for name in ("connect", "connect_ex", "bind"):
        original = getattr(socket.socket, name)

        def guarded(self, address, *args, _original=original, **kwargs):
            if self.family in (socket.AF_INET, socket.AF_INET6):
                forbidden()
            return _original(self, address, *args, **kwargs)

        monkeypatch.setattr(socket.socket, name, guarded)


class _OwnedTasks:
    def __init__(self, events):
        self.events = events
        self.tasks = {}

    def create_task(self, coroutine, *, name, daemon=False):
        task_id = f"{name}:{len(self.tasks)}"
        task = asyncio.create_task(coroutine, name=task_id)
        result = SimpleNamespace(task_id=task_id, task=task)
        self.tasks[task_id] = result
        self.events.append(("task.created", name))
        return result

    def get_task(self, task_id):
        return self.tasks[task_id]

    def assert_quiescent(self):
        assert all(info.task.done() for info in self.tasks.values())


def _harness(tmp_path, monkeypatch, backend, *, selected=True, memory_mode="success"):
    events = []
    runtimes, calls = _install_selected_factories(
        monkeypatch, backend, build_fake_stores(), _FakeLifeEventStore()
    )
    service = _selected_service(tmp_path, backend)
    service._selectable_storage_enabled = selected
    config = service._cfg()
    config.learning.enabled = False
    config.memory_index.enabled = False
    config.memory_witness.enabled = False
    config.chatter.enabled = False
    config.memory_archive_sync.enabled = False
    subject_handles = []
    memory_instances = []
    memory_started = asyncio.Event()
    hold_memory = asyncio.Event()
    state = {"loaded": False, "memory_mode": memory_mode}
    manager = _OwnedTasks(events)
    monkeypatch.setattr(core_module, "get_task_manager", lambda: manager)
    monkeypatch.setattr(core_module, "log_lifecycle", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        core_module, "get_life_log_file", lambda: tmp_path / "unused.log"
    )
    monkeypatch.setattr(
        "plugins.life_engine.service.registry.register_life_engine_service",
        lambda owner: events.append(("service.registered", owner)),
    )
    monkeypatch.setattr(
        "plugins.life_engine.service.registry.unregister_life_engine_service",
        lambda: events.append(("service.unregistered", None)),
    )
    monkeypatch.setattr(
        core_module, "cleanup_autonomy_schedules", AsyncMock(return_value=0)
    )
    monkeypatch.setattr(service, "_validate_local_subject_authority", AsyncMock())
    monkeypatch.setattr(service, "_initialize_local_runtime_state", lambda: None)

    async def activate_local(settings):
        return settings, None

    monkeypatch.setattr(service, "_activate_local_file_authority", activate_local)
    monkeypatch.setattr(service, "_start_local_proactive_authority", AsyncMock())
    monkeypatch.setattr(service, "_load_opportunity_runtime_mode", AsyncMock())
    monkeypatch.setattr(service, "_initialize_shared_learning_state", AsyncMock())
    monkeypatch.setattr(service, "_initialize_opportunity_runtime", AsyncMock())
    monkeypatch.setattr(service, "_start_shared_sync", AsyncMock())
    monkeypatch.setattr(service, "_attach_proactive_delivery_proof_hook", lambda: None)

    async def idle_worker(label):
        events.append(("worker.started", label))
        stop = service._stop_event
        assert stop is not None
        try:
            await stop.wait()
        finally:
            events.append(("worker.stopped", label))

    for method in (
        "_renew_storage_authority_loop",
        "_heartbeat_loop",
        "_subject_projection_loop",
        "_initiative_reencounter_loop",
    ):
        monkeypatch.setattr(service, method, lambda label=method: idle_worker(label))

    async def open_subject(runtime, *, initialize_schema=False, **kwargs):
        assert service._storage_runtime is runtime
        assert service._storage_authority_renew_task_id is not None
        assert service._runtime_state_store is None
        assert service._presence_world_stores is None
        assert runtime.claim_calls == []
        assert initialize_schema is False
        # The real adapter only checks that its runtime has an engine handle.
        # Keep this fixture synthetic while retaining the real factory and
        # adapter identity; no SQL engine or database connection is created.
        runtime.engine = object()
        handle = await _REAL_SUBJECT_FACTORY(runtime, initialize_schema=False)
        subject_handles.append(handle)
        calls.append(("subject", initialize_schema))
        events.append(("subject.attached", handle))
        return handle

    monkeypatch.setattr(subject_factory, "open_subject_document_store", open_subject)

    async def initialize_memory(memory):
        memory_instances.append(memory)
        events.append(("memory.initialize", memory))
        assert memory._selectable_storage_enabled is selected
        assert memory._subject_document_store_required is selected
        if selected:
            assert memory.storage_runtime is runtimes[-1]
            assert selected_subject_store(memory) is subject_handles[-1]
            assert selected_subject_store(memory) is service._subject_document_store
            assert service._runtime_state_store is None
            assert service._presence_world_stores is None
            assert service._runtime_context_writer_claim is None
            assert service._learning_writer_claim is None
            assert runtimes[-1].claim_calls == []
        else:
            assert memory.storage_runtime is None
            assert memory._subject_document_store is None
        memory_started.set()
        if state["memory_mode"] == "fail":
            raise RuntimeError("SyntheticMemoryRecoveryFailed")
        if state["memory_mode"] == "wait":
            await hold_memory.wait()
        memory._initialized = True

    async def close_memory(memory):
        events.append(("memory.close", memory))
        await _REAL_MEMORY_CLOSE(memory)

    monkeypatch.setattr(
        memory_module.LifeMemoryService, "initialize", initialize_memory
    )
    monkeypatch.setattr(memory_module.LifeMemoryService, "close", close_memory)

    async def load_context():
        assert service._memory_service in memory_instances
        if selected:
            assert service._runtime_state_store is not None
            assert service._subject_document_store is subject_handles[-1]
        state["loaded"] = True
        events.append(("context.loaded", None))

    async def save_context(**kwargs):
        events.append(("context.saved", None))
        assert state["loaded"], "must never checkpoint an unhydrated startup context"

    load_spy = AsyncMock(side_effect=load_context)
    save_spy = AsyncMock(side_effect=save_context)
    monkeypatch.setattr(service, "_load_runtime_context", load_spy)
    monkeypatch.setattr(service, "_save_runtime_context", save_spy)
    # Only the success phase gets a synthetic autonomy read view. Before that,
    # the real selected guard still blocks accidental legacy fallback on rollback.
    original_autonomy_store = service._autonomy_store

    def autonomy_store():
        if selected and service._runtime_state_store is None:
            return original_autonomy_store()
        return SimpleNamespace(list_scheduled=AsyncMock(return_value=[]))

    monkeypatch.setattr(service, "_autonomy_store", autonomy_store)
    return SimpleNamespace(
        service=service,
        events=events,
        runtimes=runtimes,
        calls=calls,
        subjects=subject_handles,
        memories=memory_instances,
        memory_started=memory_started,
        hold_memory=hold_memory,
        state=state,
        tasks=manager,
        load_context=load_spy,
        save_context=save_spy,
    )


def _assert_detached(harness):
    service = harness.service
    assert service._storage_runtime is None
    assert service._subject_document_store is None
    assert service._runtime_state_store is None
    assert service._presence_world_stores is None
    assert service._runtime_context_writer_claim is None
    assert service._learning_writer_claim is None
    assert service._learning_stores is None
    assert service._learning_event_store is None
    assert service._storage_authority_renew_task_id is None
    assert service._memory_service is None
    assert service._state.running is False
    harness.tasks.assert_quiescent()


@pytest.mark.parametrize("backend", [BackendKind.LOCAL, BackendKind.MYSQL])
async def test_real_start_attaches_same_subject_before_memory_and_reuses_it_for_domains(
    tmp_path, monkeypatch, backend
):
    h = _harness(tmp_path, monkeypatch, backend)
    assert h.service._subject_document_store is None
    assert h.service._storage_runtime is None
    try:
        await h.service.start()
        assert h.service._state.running
        assert len(h.runtimes) == len(h.subjects) == len(h.memories) == 1
        assert h.memories[0]._subject_document_store is h.subjects[0]
        assert h.service._subject_document_store is h.subjects[0]
        assert h.calls[0] == ("subject", False)
        assert h.calls.count(("subject", False)) == 1
        assert all(initialize_schema is False for _, initialize_schema in h.calls)
        assert h.runtimes[0].claim_calls
        h.load_context.assert_awaited_once()
        await h.service._attach_selected_subject_store()
        await h.service.start()
        assert len(h.runtimes) == len(h.subjects) == len(h.memories) == 1
    finally:
        await h.service.stop()
    h.save_context.assert_awaited_once()
    assert h.runtimes[0].revoke_calls == h.runtimes[0].close_calls == 1
    _assert_detached(h)


@pytest.mark.parametrize("backend", [BackendKind.LOCAL, BackendKind.MYSQL])
async def test_memory_failure_rolls_back_only_early_owned_store_without_empty_checkpoint(
    tmp_path, monkeypatch, backend
):
    h = _harness(tmp_path, monkeypatch, backend, memory_mode="fail")
    with pytest.raises(RuntimeError, match="SelectedMemoryStorageInitializationFailed"):
        await h.service.start()
    assert len(h.runtimes) == len(h.subjects) == len(h.memories) == 1
    assert h.calls == [("subject", False)]
    assert h.runtimes[0].claim_calls == []
    assert h.runtimes[0].revoke_calls == h.runtimes[0].close_calls == 1
    h.load_context.assert_not_awaited()
    h.save_context.assert_not_awaited()
    _assert_detached(h)


@pytest.mark.parametrize("backend", [BackendKind.LOCAL, BackendKind.MYSQL])
async def test_memory_cancellation_releases_renewal_and_store_without_context_write(
    tmp_path, monkeypatch, backend
):
    h = _harness(tmp_path, monkeypatch, backend, memory_mode="wait")
    startup = asyncio.create_task(h.service.start())
    try:
        await asyncio.wait_for(h.memory_started.wait(), 2)
        assert h.service._subject_document_store is h.subjects[0]
        startup.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(startup, 3)
    finally:
        h.hold_memory.set()
        await asyncio.gather(startup, return_exceptions=True)
        if h.service._storage_runtime is not None:
            await h.service.stop()
    assert h.calls == [("subject", False)]
    assert h.runtimes[0].claim_calls == []
    assert h.runtimes[0].revoke_calls == h.runtimes[0].close_calls == 1
    assert ("memory.close", h.memories[0]) in h.events
    h.load_context.assert_not_awaited()
    h.save_context.assert_not_awaited()
    _assert_detached(h)


@pytest.mark.parametrize("backend", [BackendKind.LOCAL, BackendKind.MYSQL])
async def test_retry_after_memory_failure_gets_new_runtime_and_new_subject_handle(
    tmp_path, monkeypatch, backend
):
    h = _harness(tmp_path, monkeypatch, backend, memory_mode="fail")
    with pytest.raises(RuntimeError, match="SelectedMemoryStorageInitializationFailed"):
        await h.service.start()
    first_runtime, first_subject = h.runtimes[0], h.subjects[0]
    h.state["memory_mode"] = "success"
    try:
        await h.service.start()
        assert len(h.runtimes) == len(h.subjects) == len(h.memories) == 2
        assert h.runtimes[1] is not first_runtime
        assert h.subjects[1] is not first_subject
        assert h.memories[1]._subject_document_store is h.subjects[1]
        assert h.service._subject_document_store is h.subjects[1]
        assert first_runtime.revoke_calls == first_runtime.close_calls == 1
        assert h.calls.count(("subject", False)) == 2
    finally:
        await h.service.stop()
    h.save_context.assert_awaited_once()
    assert h.runtimes[1].revoke_calls == h.runtimes[1].close_calls == 1
    _assert_detached(h)


async def test_missing_subject_store_fails_before_memory_construction(
    tmp_path, monkeypatch
):
    h = _harness(tmp_path, monkeypatch, BackendKind.MYSQL)
    await h.service._open_selected_storage_runtime()
    try:
        assert h.service._subject_document_store is None
        await MemoryIntegration(h.service).init_memory_service()
        assert h.service._memory_service is None
        assert h.memories == []
        assert h.subjects == []
        with pytest.raises(
            RuntimeError, match="SelectedMemoryStorageInitializationFailed"
        ):
            h.service._require_selected_memory_service()
    finally:
        await h.service._close_selected_storage()
    assert h.runtimes[0].close_calls == 1


async def test_nonselected_start_keeps_optional_memory_path_without_subject_factory(
    tmp_path, monkeypatch
):
    h = _harness(tmp_path, monkeypatch, BackendKind.LOCAL, selected=False)
    try:
        await h.service.start()
        assert h.service._state.running
        assert h.runtimes == h.subjects == h.calls == []
        assert len(h.memories) == 1
        assert h.memories[0]._subject_document_store_required is False
        assert h.memories[0]._subject_document_store is None
    finally:
        await h.service.stop()
    h.save_context.assert_awaited_once()
    _assert_detached(h)
