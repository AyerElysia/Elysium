"""Concurrency tests for life_engine runtime state persistence."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from plugins.life_engine.service import state_manager as state_manager_module
from plugins.life_engine.service.event_builder import LifeEngineState
from plugins.life_engine.service.state_manager import PersistenceError, StatePersistence
from plugins.life_engine.storage.runtime_contracts import RuntimeStateConflict


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


class _SharedConflictStore:
    """shared 多写者模式假 store：首次 put 抛 CAS 冲突，之后可配置行为。"""

    def __init__(
        self,
        remote_revision: int = 3,
        remote_state: dict | None = None,
    ) -> None:
        self.revision = remote_revision
        self.latest_state = dict(remote_state or {})
        self.put_state_calls: list[dict] = []
        self.get_state_calls = 0
        self.conflict_on_first_put = True
        self.conflict_on_retry = False
        self.fail_get = False

    async def put_state(self, **kwargs):
        self.put_state_calls.append(kwargs)
        if self.conflict_on_first_put:
            self.conflict_on_first_put = False
            raise RuntimeStateConflict(
                f"revision conflict: expected={kwargs['expected_revision']}"
                f":actual={self.revision}"
            )
        if self.conflict_on_retry:
            raise RuntimeStateConflict(
                f"revision conflict: expected={kwargs['expected_revision']}"
                f":actual={self.revision}"
            )
        self.revision = int(kwargs["expected_revision"]) + 1
        self.latest_state = dict(kwargs["payload"]["state"])
        return SimpleNamespace(
            revision=self.revision,
            payload=dict(kwargs["payload"]),
        )

    async def get_state(self, namespace: str, state_key: str):
        self.get_state_calls += 1
        if self.fail_get:
            raise RuntimeError("remote unavailable")
        return SimpleNamespace(
            revision=self.revision,
            payload={"version": 2, "state": dict(self.latest_state)},
        )


async def test_shared_mode_conflict_merges_and_retries(tmp_path: Path) -> None:
    """shared 多写者模式：CAS 冲突时重读最新 revision，合并技术 checkpoint 后重试提交。"""
    store = _SharedConflictStore(
        remote_revision=3,
        remote_state={
            "heartbeat_count": 5,
            "event_sequence": 10,
            "heartbeat_context_cursor": 2,
            "last_model_reply": "remote",
        },
    )
    state = LifeEngineState(heartbeat_count=1, event_sequence=3)
    state.last_model_reply = "local"
    persistence = StatePersistence(str(tmp_path), lambda: 100, runtime_store=store)

    assert await persistence.save_runtime_context(state, [], []) is True

    assert len(store.put_state_calls) == 2
    assert store.put_state_calls[0]["expected_revision"] == 0
    assert store.put_state_calls[1]["expected_revision"] == 3
    assert store.get_state_calls == 1
    retried_state = store.put_state_calls[1]["payload"]["state"]
    # 技术 checkpoint 字段取两侧 max，防止并发提交导致计数倒退
    assert retried_state["heartbeat_count"] == 5
    assert retried_state["event_sequence"] == 10
    assert retried_state["heartbeat_context_cursor"] == 2
    # 其余业务字段以本地快照为准
    assert retried_state["last_model_reply"] == "local"
    assert persistence._runtime_state_revision == 4


async def test_shared_mode_conflict_reload_failure_raises_persistence_error(
    tmp_path: Path,
) -> None:
    """shared 模式冲突后远端不可读：保持可见失败，不静默丢状态。"""
    store = _SharedConflictStore(remote_revision=3)
    store.fail_get = True
    state = LifeEngineState(heartbeat_count=1)
    persistence = StatePersistence(str(tmp_path), lambda: 100, runtime_store=store)

    with pytest.raises(PersistenceError):
        await persistence.save_runtime_context(state, [], [])

    assert len(store.put_state_calls) == 1
    assert store.get_state_calls == 1


async def test_shared_mode_conflict_retry_conflict_raises_persistence_error(
    tmp_path: Path,
) -> None:
    """shared 模式合并重试窗口内再次被推进：抛 PersistenceError，但推进本地 revision。"""
    store = _SharedConflictStore(remote_revision=3)
    store.conflict_on_retry = True
    state = LifeEngineState(heartbeat_count=1)
    persistence = StatePersistence(str(tmp_path), lambda: 100, runtime_store=store)

    with pytest.raises(PersistenceError):
        await persistence.save_runtime_context(state, [], [])

    assert len(store.put_state_calls) == 2
    # 本地 revision 更新为已看到的最新值，下一轮重读即可成功
    assert persistence._runtime_state_revision == 3


async def test_shared_mode_conflict_retry_conflict_recoverable_succeeds(
    tmp_path: Path,
) -> None:
    """shared 模式 recoverable 路径：合并重试窗口内再次被推进视为合法竞争，
    采纳远端最新值，返回 True 且不抛错、不置脏。

    心跳等可恢复技术 checkpoint 使用该路径：global 只是技术 checkpoint，
    心跳与感知状态已通过 operation 持久化，无需覆盖另一实例刚写入的更新。
    """
    store = _SharedConflictStore(remote_revision=3)
    store.conflict_on_retry = True
    state = LifeEngineState(heartbeat_count=1)
    persisted_called = False

    def on_persisted() -> None:
        nonlocal persisted_called
        persisted_called = True

    persistence = StatePersistence(
        str(tmp_path),
        lambda: 100,
        runtime_store=store,
        on_persisted=on_persisted,
    )

    result = await persistence.save_runtime_context(
        state,
        [],
        [],
        recoverable_on_shared_conflict=True,
    )

    assert result is True
    assert len(store.put_state_calls) == 2
    # 本地 revision 更新为已看到的最新值，下一轮重读即可成功
    assert persistence._runtime_state_revision == 3
    # 采纳远端为最新，on_persisted 被调用（状态视为已持久化，不置脏）
    assert persisted_called is True


async def test_shared_mode_conflict_retry_conflict_recoverable_false_still_raises(
    tmp_path: Path,
) -> None:
    """recoverable_on_shared_conflict 默认 False：合并重试窗口内再次被推进
    仍抛 PersistenceError，供 chatter checkpoint 等必须耐久写入的路径使用。"""
    store = _SharedConflictStore(remote_revision=3)
    store.conflict_on_retry = True
    state = LifeEngineState(heartbeat_count=1)
    persistence = StatePersistence(str(tmp_path), lambda: 100, runtime_store=store)

    with pytest.raises(PersistenceError):
        await persistence.save_runtime_context(state, [], [])

    assert len(store.put_state_calls) == 2
    assert persistence._runtime_state_revision == 3


async def test_single_writer_mode_conflict_raises_without_merge(tmp_path: Path) -> None:
    """单写者模式：CAS 冲突是真实错误，直接 PersistenceError，不触发共享合并。"""
    store = _SharedConflictStore(remote_revision=3)
    claim = SimpleNamespace(lease_token="writer-1")
    state = LifeEngineState(heartbeat_count=1)
    persistence = StatePersistence(
        str(tmp_path),
        lambda: 100,
        runtime_store=store,
        runtime_writer_claim=claim,
    )

    with pytest.raises(PersistenceError):
        await persistence.save_runtime_context(state, [], [])

    assert len(store.put_state_calls) == 1
    assert store.get_state_calls == 0
