"""Concurrency tests for life_engine runtime state persistence."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

from plugins.life_engine.service import state_manager as state_manager_module
from plugins.life_engine.service.event_builder import LifeEngineState
from plugins.life_engine.service.state_manager import StatePersistence


async def test_runtime_context_writes_are_serialized(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """Concurrent saves must not replace staging files in parallel."""

    original_write = state_manager_module.atomic_write_text
    counter_lock = threading.Lock()
    active_writers = 0
    maximum_writers = 0

    def tracked_write(path: Path, content: str) -> None:
        nonlocal active_writers, maximum_writers
        with counter_lock:
            active_writers += 1
            maximum_writers = max(maximum_writers, active_writers)
        try:
            time.sleep(0.01)
            original_write(path, content)
        finally:
            with counter_lock:
                active_writers -= 1

    monkeypatch.setattr(state_manager_module, "atomic_write_text", tracked_write)
    persistence = StatePersistence(str(tmp_path), lambda: 100)
    state = LifeEngineState()

    await asyncio.gather(
        *(
            persistence.save_runtime_context(state, [], [])
            for _ in range(12)
        )
    )

    assert maximum_writers == 1
    payload = json.loads((tmp_path / "life_engine_context.json").read_text())
    assert payload["version"] == 2
    assert not list(tmp_path.glob("*.tmp"))


async def test_selected_runtime_context_concurrent_saves_serialize_cas(
    tmp_path: Path,
) -> None:
    class _StrictRuntimeStore:
        def __init__(self) -> None:
            self.revision = 0
            self.records: list[SimpleNamespace] = []
            self.first_write_started = asyncio.Event()
            self.allow_first_write = asyncio.Event()

        async def put_state(self, **kwargs):
            expected = int(kwargs["expected_revision"])
            if expected == 0:
                self.first_write_started.set()
                await self.allow_first_write.wait()
            if expected != self.revision:
                raise RuntimeError(
                    f"revision conflict: expected={expected}:actual={self.revision}"
                )
            self.revision += 1
            record = SimpleNamespace(
                revision=self.revision,
                payload=dict(kwargs["payload"]),
            )
            self.records.append(record)
            return record

    store = _StrictRuntimeStore()
    state = LifeEngineState(heartbeat_count=1)
    state_lock = asyncio.Lock()
    dirty = True

    def mark_persisted() -> None:
        nonlocal dirty
        dirty = False

    persistence = StatePersistence(
        str(tmp_path),
        lambda: 100,
        lock=state_lock,
        runtime_store=store,
        on_persisted=mark_persisted,
    )

    async def update_then_save() -> None:
        nonlocal dirty
        async with state_lock:
            state.heartbeat_count = 2
            dirty = True
        await persistence.save_runtime_context(state, [], [])

    first = asyncio.create_task(persistence.save_runtime_context(state, [], []))
    await asyncio.wait_for(store.first_write_started.wait(), timeout=1.0)
    second = asyncio.create_task(update_then_save())
    await asyncio.sleep(0)
    assert state.heartbeat_count == 1
    assert dirty is True
    store.allow_first_write.set()

    await asyncio.gather(first, second)

    assert dirty is False
    assert [record.revision for record in store.records] == [1, 2]
    assert store.records[0].payload["state"]["heartbeat_count"] == 1
    assert store.records[1].payload["state"]["heartbeat_count"] == 2
    assert persistence._runtime_state_revision == 2


async def test_selected_runtime_context_never_uses_local_json(tmp_path: Path) -> None:
    class _RuntimeStore:
        def __init__(self) -> None:
            self.record = None

        async def get_state(self, namespace: str, state_key: str):
            assert (namespace, state_key) == ("life_engine.runtime_context", "global")
            return self.record

        async def put_state(self, **kwargs):
            self.record = SimpleNamespace(
                revision=int(kwargs["expected_revision"]) + 1,
                payload=dict(kwargs["payload"]),
            )
            return self.record

    store = _RuntimeStore()
    state = LifeEngineState(heartbeat_count=7, event_sequence=9)
    persistence = StatePersistence(
        str(tmp_path),
        lambda: 100,
        runtime_store=store,
    )

    await persistence.save_runtime_context(state, [], [])
    assert not (tmp_path / "life_engine_context.json").exists()

    restored = LifeEngineState()
    second = StatePersistence(
        str(tmp_path),
        lambda: 100,
        runtime_store=store,
    )
    pending, history, _ = await second.load_runtime_context(restored, lambda: 1)
    assert pending == []
    assert history == []
    assert restored.heartbeat_count == 7
    assert restored.event_sequence == 9
    assert not (tmp_path / "life_engine_context.json").exists()


async def test_independent_persistence_instances_use_unique_staging_files(
    tmp_path: Path,
) -> None:
    """Accidental duplicate instances must not collide on a fixed .tmp path."""

    first = StatePersistence(str(tmp_path), lambda: 100)
    second = StatePersistence(str(tmp_path), lambda: 100)
    state = LifeEngineState()

    await asyncio.gather(
        *(
            writer.save_runtime_context(state, [], [])
            for writer in (first, second)
            for _ in range(10)
        )
    )

    payload = json.loads((tmp_path / "life_engine_context.json").read_text())
    assert payload["version"] == 2
    assert not list(tmp_path.glob("*.tmp"))
