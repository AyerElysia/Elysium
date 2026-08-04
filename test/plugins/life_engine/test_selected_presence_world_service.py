"""Service-level contracts for selectable Presence, World, and Life Events."""

from __future__ import annotations

import asyncio
import copy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import plugins.life_engine.service.core as core_module
from plugins.life_engine.core.config import LifeEngineConfig
from plugins.life_engine.service import LifeEngineService
from plugins.life_engine.service.async_presence import (
    AsyncConsciousnessRegistry,
    flush_presence_lifecycle_events,
)
from plugins.life_engine.service.consciousness import (
    CHAT_GLOBAL_INSTANCE_ID,
    ConsciousnessInstance,
    ConsciousnessRegistry,
)
from plugins.life_engine.service.event_bus import LifeEvent
from plugins.life_engine.service.presence_store import PresenceRevisionConflict
from plugins.life_engine.service.world_projection import PerceptionCursorConflict
from plugins.life_engine.storage.models import BackendKind
from test.plugins.life_engine.presence_world_fakes import build_fake_stores


class _FakeLifeEventStore:
    """Small immutable-ledger reference sufficient for service routing tests."""

    def __init__(self) -> None:
        self._events: list[LifeEvent] = []
        self._occurrences: dict[str, LifeEvent] = {}
        self._lock = asyncio.Lock()
        self.fail_appends = False

    async def append(self, event: LifeEvent) -> LifeEvent:
        return (await self.append_many([event]))[0]

    async def append_many(self, events: list[LifeEvent]) -> list[LifeEvent]:
        if self.fail_appends:
            raise RuntimeError("injected life-event append failure")
        async with self._lock:
            persisted: list[LifeEvent] = []
            for event in events:
                occurrence_id = event.occurrence_id or event.event_id
                existing = self._occurrences.get(occurrence_id)
                if existing is not None:
                    if (
                        existing.event_id != event.event_id
                        or existing.content != event.content
                        or existing.event_type != event.event_type
                        or existing.source_instance_id != event.source_instance_id
                    ):
                        raise RuntimeError(
                            "life-event occurrence reused with different evidence"
                        )
                    persisted.append(copy.deepcopy(existing))
                    continue
                committed = replace(
                    event,
                    sequence=len(self._events) + 1,
                    occurrence_id=occurrence_id,
                    source_sequence=event.source_sequence or event.sequence,
                )
                self._events.append(copy.deepcopy(committed))
                self._occurrences[occurrence_id] = copy.deepcopy(committed)
                persisted.append(copy.deepcopy(committed))
            return persisted

    async def read_since(
        self,
        position: int,
        *,
        limit: int | None = None,
    ) -> list[LifeEvent]:
        values = [
            copy.deepcopy(item)
            for item in self._events
            if item.sequence > int(position)
        ]
        return values if limit is None else values[: max(0, int(limit))]

    async def health_snapshot(self) -> dict[str, Any]:
        return {
            "status": "healthy",
            "backend": "fake",
            "total": len(self._events),
            "latest_position": len(self._events),
        }

    async def health(self) -> dict[str, Any]:
        return await self.health_snapshot()


class _FakeRuntime:
    def __init__(self, backend: BackendKind) -> None:
        self.enabled = True
        self.backend = backend
        self.backend_identity = f"{backend.value}://service-contract"
        self.close_calls = 0

    async def health(self) -> dict[str, Any]:
        return {
            "status": "healthy",
            "backend": self.backend.value,
            "backend_identity": self.backend_identity,
        }

    async def close(self) -> None:
        self.close_calls += 1


class _FakeSubjectStore:
    async def health_snapshot(self) -> dict[str, Any]:
        return {
            "status": "healthy",
            "backend": "fake",
            "documents": 0,
            "versions": 0,
            "projection_outbox": {},
        }


class _FakeLearningStore:
    async def health_snapshot(self) -> dict[str, Any]:
        return {
            "status": "healthy",
            "backend": "fake",
            "event_count": 0,
            "latest_position": 0,
            "projection_states": {},
        }


def _selected_service(tmp_path: Path, backend: BackendKind) -> LifeEngineService:
    config = LifeEngineConfig()
    config.settings.enabled = True
    config.settings.workspace_path = str(tmp_path)
    config.storage.enabled = True
    config.storage.authoritative_backend = backend.value
    config.storage.backend_generation = f"contract-{backend.value}-v1"
    config.storage.authority_epoch = 1
    config.storage.authority_owner_id = "service-contract"
    return LifeEngineService(SimpleNamespace(config=config))


def test_selected_service_fails_closed_when_memory_did_not_attach(
    tmp_path: Path,
) -> None:
    service = _selected_service(tmp_path, BackendKind.MYSQL)

    with pytest.raises(
        RuntimeError,
        match="SelectedMemoryStorageInitializationFailed",
    ):
        service._require_selected_memory_service()

    service._memory_service = object()
    service._require_selected_memory_service()

    disabled = _selected_service(tmp_path / "disabled", BackendKind.LOCAL)
    disabled._selectable_storage_enabled = False
    disabled._require_selected_memory_service()


def _install_selected_factories(
    monkeypatch: pytest.MonkeyPatch,
    backend: BackendKind,
    stores: Any,
    ledger: _FakeLifeEventStore,
) -> tuple[list[_FakeRuntime], list[tuple[str, bool]]]:
    runtimes: list[_FakeRuntime] = []
    factory_calls: list[tuple[str, bool]] = []
    subject_store = _FakeSubjectStore()
    learning_store = _FakeLearningStore()

    async def _open_runtime(_settings: Any) -> _FakeRuntime:
        runtime = _FakeRuntime(backend)
        runtimes.append(runtime)
        return runtime

    async def _open_events(
        runtime: _FakeRuntime,
        *,
        initialize_schema: bool = False,
    ) -> _FakeLifeEventStore:
        assert runtime.backend == backend
        factory_calls.append(("events", initialize_schema))
        return ledger

    async def _open_domains(
        runtime: _FakeRuntime,
        *,
        initialize_schema: bool = False,
    ) -> Any:
        assert runtime.backend == backend
        factory_calls.append(("presence-world", initialize_schema))
        return stores

    async def _open_subject(
        runtime: _FakeRuntime,
        *,
        initialize_schema: bool = False,
        require_database_immutability: bool = True,
    ) -> _FakeSubjectStore:
        assert runtime.backend == backend
        assert require_database_immutability is True
        factory_calls.append(("subject", initialize_schema))
        return subject_store

    async def _open_learning(
        runtime: _FakeRuntime,
        *,
        initialize_schema: bool = False,
    ) -> Any:
        assert runtime.backend == backend
        factory_calls.append(("learning", initialize_schema))
        return SimpleNamespace(store=learning_store)

    monkeypatch.setattr(
        "plugins.life_engine.storage.factory.open_storage_backend",
        _open_runtime,
    )
    monkeypatch.setattr(
        "plugins.life_engine.storage.event_factory.open_life_event_store",
        _open_events,
    )
    monkeypatch.setattr(
        "plugins.life_engine.storage.domain_factory.open_presence_world_stores",
        _open_domains,
    )
    monkeypatch.setattr(
        "plugins.life_engine.storage.subject_factory.open_subject_document_store",
        _open_subject,
    )
    monkeypatch.setattr(
        "plugins.life_engine.storage.learning_factory.open_learning_stores",
        _open_learning,
    )
    return runtimes, factory_calls


def _forbid_legacy_stores(monkeypatch: pytest.MonkeyPatch) -> None:
    def _legacy_registry_load(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("selected storage opened the legacy Presence store")

    class _ForbiddenLegacyStore:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            raise AssertionError("selected storage opened a legacy SQLite store")

    monkeypatch.setattr(
        ConsciousnessRegistry,
        "load",
        classmethod(_legacy_registry_load),
    )
    monkeypatch.setattr(core_module, "RawEventStore", _ForbiddenLegacyStore)
    monkeypatch.setattr(core_module, "WorldProjectionStore", _ForbiddenLegacyStore)


@pytest.mark.parametrize("backend", [BackendKind.LOCAL, BackendKind.MYSQL])
async def test_selected_service_uses_one_backend_for_presence_world_and_events(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    backend: BackendKind,
) -> None:
    """Both configured backends obey the same async service and restart contract."""

    stores = build_fake_stores()
    ledger = _FakeLifeEventStore()
    runtimes, factory_calls = _install_selected_factories(
        monkeypatch,
        backend,
        stores,
        ledger,
    )
    _forbid_legacy_stores(monkeypatch)

    first = _selected_service(tmp_path, backend)
    with pytest.raises(RuntimeError, match="SelectedStorageRuntimeNotStarted"):
        _ = first.storage_runtime
    await first._start_selected_storage()

    assert factory_calls == [
        ("events", False),
        ("presence-world", False),
        ("subject", False),
        ("learning", False),
    ]
    assert first.storage_runtime is runtimes[0]
    assert first.consciousness_registry.get(CHAT_GLOBAL_INSTANCE_ID) is not None
    assert first.consciousness_registry.database_path is None
    assert not (tmp_path / "runtime" / "consciousness_presence.sqlite3").exists()
    assert not (tmp_path / "runtime" / "world_projection.sqlite3").exists()
    assert not (tmp_path / "life_events.sqlite3").exists()

    observer = ConsciousnessInstance(
        instance_id="voice:service-contract",
        kind="voice_live",
        display_name="service contract voice",
        stream_ids=["stream:voice-contract"],
        session_id="episode:service-contract",
    )
    await first.register_consciousness_instance(observer)
    report = await first.report_world_observation(
        "the same subject has an active voice window",
        source_instance_id=observer.instance_id,
        subject="subject:self",
        predicate="voice_window",
        stream_id=observer.stream_ids[0],
        value={"active": True},
    )
    assert report["projection_as_of"] == report["ingest_position"]

    prepared = await first.prepare_perception(observer.instance_id)
    assert prepared.from_position == 0
    assert prepared.through_position == report["ingest_position"]
    assert "voice:service-contract" in prepared.content
    assert await stores.world.perception_cursor(observer.instance_id) == (0, 0)
    committed = await first.commit_perception(prepared)
    assert committed == (prepared.through_position, 1)
    assert (
        await stores.world.commit_perception_cursor(
            observer.instance_id,
            expected_position=committed[0],
            expected_revision=committed[1],
            through_position=committed[0],
        )
        == committed
    )
    with pytest.raises(PerceptionCursorConflict):
        await first.commit_perception(prepared)

    assert await first.rebuild_world_projection() == report["ingest_position"]
    assert await stores.world.perception_cursor(observer.instance_id) == committed
    health = await first.refresh_storage_health()
    assert health["status"] == "healthy"
    assert health["backend"] == backend.value
    assert first.health()["world_projection"]["rebuild_state"] == "idle"
    assert first.health()["subject_document"]["documents"] == 0
    assert first.health()["storage_runtime"]["components"]["learning"][
        "event_count"
    ] == 0

    await first._close_selected_storage()
    assert runtimes[0].close_calls == 1

    second = _selected_service(tmp_path, backend)
    third = _selected_service(tmp_path, backend)
    await second._start_selected_storage()
    await third._start_selected_storage()
    restored = second.consciousness_registry.get(observer.instance_id)
    assert restored is not None
    assert restored.session_id == observer.session_id
    assert await stores.world.perception_cursor(observer.instance_id) == committed

    touches = await asyncio.gather(
        second.touch_consciousness_instance(observer.instance_id),
        third.touch_consciousness_instance(observer.instance_id),
        return_exceptions=True,
    )
    assert sum(not isinstance(item, BaseException) for item in touches) == 1
    assert sum(isinstance(item, PresenceRevisionConflict) for item in touches) == 1

    await second._close_selected_storage()
    await third._close_selected_storage()
    assert [runtime.close_calls for runtime in runtimes] == [1, 1, 1]


async def test_selected_service_close_releases_runtime_after_flush_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stores = build_fake_stores()
    ledger = _FakeLifeEventStore()
    runtimes, _ = _install_selected_factories(
        monkeypatch,
        BackendKind.MYSQL,
        stores,
        ledger,
    )
    service = _selected_service(tmp_path, BackendKind.MYSQL)
    await service._start_selected_storage()
    registry = service.consciousness_registry
    assert isinstance(registry, AsyncConsciousnessRegistry)
    await registry.touch(CHAT_GLOBAL_INSTANCE_ID)
    ledger.fail_appends = True

    with pytest.raises(ExceptionGroup, match="storage shutdown failed") as captured:
        await service._close_selected_storage()

    assert any(
        "injected life-event append failure" in str(item)
        for item in captured.value.exceptions
    )
    assert runtimes[0].close_calls == 1
    assert service.health()["storage_runtime"]["status"] == "failed"
    await service._close_selected_storage()
    assert runtimes[0].close_calls == 1


async def test_service_stop_aggregates_consumer_failures_and_closes_runtime_last(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stores = build_fake_stores()
    ledger = _FakeLifeEventStore()
    runtimes, _ = _install_selected_factories(
        monkeypatch,
        BackendKind.LOCAL,
        stores,
        ledger,
    )
    service = _selected_service(tmp_path, BackendKind.LOCAL)
    await service._start_selected_storage()

    class _FailingMemoryConsumer:
        async def close(self) -> None:
            assert runtimes[0].close_calls == 0
            raise RuntimeError("injected memory consumer close failure")

    class _FailingLearningConsumer:
        async def close(self) -> None:
            assert runtimes[0].close_calls == 0
            raise RuntimeError("injected learning consumer close failure")

    async def _no_op() -> None:
        return None

    registry = service.consciousness_registry
    assert isinstance(registry, AsyncConsciousnessRegistry)
    await registry.touch(CHAT_GLOBAL_INSTANCE_ID)
    ledger.fail_appends = True
    service._memory_service = _FailingMemoryConsumer()  # type: ignore[assignment]
    service._learning_scheduler = _FailingLearningConsumer()
    monkeypatch.setattr(service, "_save_runtime_context", _no_op)
    monkeypatch.setattr(core_module, "cleanup_autonomy_schedules", lambda *_: _no_op())

    with pytest.raises(ExceptionGroup, match="consumer failures") as captured:
        await service.stop()

    rendered = "\n".join(str(item) for item in captured.value.exceptions)
    assert "injected memory consumer close failure" in rendered
    assert "injected learning consumer close failure" in rendered
    assert "selected Presence/World storage shutdown failed" in rendered
    assert runtimes[0].close_calls == 1


async def test_presence_outbox_limit_fails_without_losing_remaining_evidence() -> None:
    stores = build_fake_stores()
    ledger = _FakeLifeEventStore()
    registry = await AsyncConsciousnessRegistry.load(stores.presence)
    for index in range(2):
        await registry.register(
            ConsciousnessInstance(instance_id=f"instance:bounded:{index}")
        )

    with pytest.raises(RuntimeError, match="PresenceLifecycleFlushLimit"):
        await flush_presence_lifecycle_events(
            stores.presence,
            ledger,
            batch_size=1,
            max_events=2,
        )

    assert len(await ledger.read_since(0)) == 2
    assert len(await stores.presence.pending_events()) == 1
