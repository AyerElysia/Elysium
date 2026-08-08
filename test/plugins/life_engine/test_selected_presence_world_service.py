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
from plugins.life_engine.attention_threads import (
    AttentionThreadCommand,
    AttentionThreadCommit,
    AttentionThreadPageQuery,
    InstanceFocus,
    build_attention_thread_projection,
)
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
from plugins.life_engine.service.perception_gateway import (
    PerceptionDeliveryReceipt,
    PreparedPerception,
)
from plugins.life_engine.service.presence_store import PresenceRevisionConflict
from plugins.life_engine.storage.models import BackendKind
from plugins.life_engine.storage.multi_writer_protocol import (
    MultiWriterProtocolError,
    MultiWriterRuntimeState,
)
from src.core.config.core_config import CoreConfig
from test.plugins.life_engine.presence_world_fakes import build_fake_stores


def _exact_receipt(prepared: PreparedPerception) -> PerceptionDeliveryReceipt:
    return PerceptionDeliveryReceipt(
        delivery_id=prepared.delivery_id,
        projection_sha256=prepared.projection_sha256,
        delivered_bytes=prepared.delivered_bytes,
        exact=True,
    )


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
        self.renew_calls: list[int] = []
        self.revoke_calls = 0
        self.invalidated = False
        self.renew_error: Exception | None = None
        self.claim_calls: list[dict[str, Any]] = []

    async def acquire_singleton_writer(self, **kwargs: Any) -> Any:
        self.claim_calls.append(dict(kwargs))
        return SimpleNamespace(
            generation_id="fake-generation",
            namespace=kwargs["namespace"],
            state_key=kwargs["state_key"],
            owner_instance_id=kwargs["owner_instance_id"],
            lease_epoch=1,
            lease_until="2026-08-07T23:59:59+00:00",
            fencing_token="fake-token",
        )

    async def renew_authority(self, *, lease_seconds: int) -> None:
        self.renew_calls.append(lease_seconds)
        if self.renew_error is not None:
            raise self.renew_error

    async def revoke_authority(self) -> int:
        self.revoke_calls += 1
        return self.revoke_calls

    def invalidate_writer(self) -> None:
        self.invalidated = True

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


class _FakeRuntimeStateStore:
    async def health_snapshot(self) -> dict[str, Any]:
        return {
            "status": "healthy",
            "backend": "fake",
            "state_count": 0,
            "event_count": 0,
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


class _FakeAttentionStore:
    def __init__(self) -> None:
        self.commands: list[AttentionThreadCommand] = []
        self.focuses: dict[str, InstanceFocus] = {}

    async def decide(
        self,
        command: AttentionThreadCommand,
    ) -> AttentionThreadCommit:
        idempotent_replay = any(
            item.occurrence_id == command.occurrence_id for item in self.commands
        )
        if not idempotent_replay:
            self.commands.append(command)
        return AttentionThreadCommit(
            event_id="attention:event:fake",
            occurrence_id=command.occurrence_id,
            thread_id=command.thread_id,
            revision=command.expected_revision + 1,
            status={"pause": "paused", "close": "closed"}.get(
                command.action,
                "open",
            ),
            idempotent_replay=idempotent_replay,
        )

    async def page(self, query: AttentionThreadPageQuery) -> Any:
        return build_attention_thread_projection(
            (),
            source_frontier=len(self.commands),
            projection_revision=len(self.commands),
            max_bytes=query.max_bytes,
            projection_kind=query.projection_kind,
        )

    async def set_focus(self, focus: InstanceFocus) -> InstanceFocus:
        self.focuses[focus.instance_id] = focus
        return focus

    async def get_focus(self, instance_id: str) -> InstanceFocus | None:
        return self.focuses.get(instance_id)

    async def clear_focus(
        self,
        instance_id: str,
        *,
        expected_revision: int,
    ) -> None:
        focus = self.focuses.get(instance_id)
        if focus is None or focus.revision != expected_revision:
            raise RuntimeError("fake focus revision conflict")
        self.focuses.pop(instance_id)

    async def health_snapshot(self) -> dict[str, Any]:
        return {
            "status": "healthy",
            "event_count": len(self.commands),
            "source_frontier": len(self.commands),
            "threads": {"open": len(self.commands), "paused": 0, "closed": 0},
            "instance_focus_count": len(self.focuses),
            "schema_version": 1,
        }


def _selected_service(tmp_path: Path, backend: BackendKind) -> LifeEngineService:
    config = LifeEngineConfig()
    config.settings.enabled = True
    config.settings.workspace_path = str(tmp_path)
    global_config = CoreConfig(
        storage=CoreConfig.StorageSection(
            backend=backend.value,
            backend_generation=f"contract-{backend.value}-v1",
            authority_owner_id="service-contract",
        )
    )
    return LifeEngineService(
        SimpleNamespace(config=config, global_storage_config=global_config)
    )


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


def test_selected_runtime_consumers_fail_closed_before_store_attach(
    tmp_path: Path,
) -> None:
    """Selected runtime consumers must never resurrect local JSON state."""

    service = _selected_service(tmp_path, BackendKind.MYSQL)

    for factory in (
        service._autonomy_store,
        service.life_trace_store,
        service.narrative_store,
    ):
        with pytest.raises(
            RuntimeError,
            match="SelectedRuntimeStateStorageNotStarted",
        ):
            factory()

    assert list(tmp_path.iterdir()) == []


def _install_selected_factories(
    monkeypatch: pytest.MonkeyPatch,
    backend: BackendKind,
    stores: Any,
    ledger: _FakeLifeEventStore,
) -> tuple[list[_FakeRuntime], list[tuple[str, bool]]]:
    runtimes: list[_FakeRuntime] = []
    factory_calls: list[tuple[str, bool]] = []
    subject_store = _FakeSubjectStore()
    runtime_state_store = _FakeRuntimeStateStore()
    learning_store = _FakeLearningStore()
    attention_store = _FakeAttentionStore()

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

    async def _open_runtime_state(
        runtime: _FakeRuntime,
        *,
        initialize_schema: bool = False,
    ) -> _FakeRuntimeStateStore:
        assert runtime.backend == backend
        factory_calls.append(("runtime-state", initialize_schema))
        return runtime_state_store

    async def _open_learning(
        runtime: _FakeRuntime,
        *,
        initialize_schema: bool = False,
        writer_claim: Any | None = None,
    ) -> Any:
        # The legacy generation-scoped singleton claim is retired on the
        # multi-writer path (writer_claim=None); per-test assertions on claim
        # scopes live in the individual tests, not here.
        assert runtime.backend == backend
        factory_calls.append(("learning", initialize_schema))
        return SimpleNamespace(store=learning_store)

    async def _open_attention(
        runtime: _FakeRuntime,
        *,
        initialize_schema: bool = False,
        require_database_immutability: bool = True,
    ) -> Any:
        assert runtime.backend == backend
        assert require_database_immutability is True
        factory_calls.append(("attention", initialize_schema))
        return SimpleNamespace(authority=attention_store, focus=attention_store)

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
        "plugins.life_engine.storage.runtime_factory.open_runtime_state_store",
        _open_runtime_state,
    )
    monkeypatch.setattr(
        "plugins.life_engine.storage.learning_factory.open_learning_stores",
        _open_learning,
    )
    monkeypatch.setattr(
        "plugins.life_engine.storage.attention_factory.open_attention_thread_stores",
        _open_attention,
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


@pytest.mark.parametrize("backend", [BackendKind.MYSQL])
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
        ("runtime-state", False),
        ("attention", False),
        ("learning", False),
    ]
    assert first.storage_runtime is runtimes[0]
    assert [
        (call["namespace"], call["state_key"]) for call in runtimes[0].claim_calls
    ] == [
        ("life_engine.runtime_context", "global"),
        ("life_engine.learning", "selected_persistence"),
    ]
    assert len({call["owner_instance_id"] for call in runtimes[0].claim_calls}) == 1
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
    committed = await first.commit_perception_delivery(
        prepared.commit_checkpoint(),
        _exact_receipt(prepared),
    )
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
    assert await first.commit_perception(
        prepared,
        _exact_receipt(prepared),
    ) == committed

    assert await first.rebuild_world_projection() == report["ingest_position"]
    assert await stores.world.perception_cursor(observer.instance_id) == committed
    health = await first.refresh_storage_health()
    assert health["status"] == "healthy"
    assert health["backend"] == backend.value
    assert first.health()["world_projection"]["rebuild_state"] == "idle"
    assert first.health()["subject_document"]["documents"] == 0
    assert (
        first.health()["storage_runtime"]["components"]["learning"]["event_count"] == 0
    )
    assert (
        first.health()["storage_runtime"]["components"]["attention_threads"][
            "event_count"
        ]
        == 0
    )

    await first._close_selected_storage()
    assert runtimes[0].revoke_calls == 1
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


async def test_storage_authority_start_requires_initialized_stop_event(
    tmp_path: Path,
) -> None:
    service = _selected_service(tmp_path, BackendKind.MYSQL)
    service._storage_runtime = _FakeRuntime(BackendKind.MYSQL)

    with pytest.raises(
        RuntimeError,
        match="StorageAuthorityRenewalStopEventNotInitialized",
    ):
        service._start_storage_authority_renewal()


async def test_service_renews_authority_while_memory_is_initializing(
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
    service._storage_factory_settings = replace(
        service._storage_factory_settings,
        authority_lease_seconds=3,
        authority_renew_interval_seconds=0.01,
    )
    memory_init_started = asyncio.Event()
    hold_memory_init = asyncio.Event()

    async def _delayed_memory_init(_integration: object) -> None:
        memory_init_started.set()
        await hold_memory_init.wait()

    monkeypatch.setattr(
        "plugins.life_engine.service.integrations.MemoryIntegration.init_memory_service",
        _delayed_memory_init,
    )

    startup = asyncio.create_task(service._start_impl())
    await asyncio.wait_for(memory_init_started.wait(), timeout=1.0)
    await asyncio.sleep(0.03)

    assert runtimes[0].renew_calls
    assert not startup.done()

    startup.cancel()
    with pytest.raises(asyncio.CancelledError):
        await startup
    service._stop_event.set()
    await service._await_managed_task(
        service._storage_authority_renew_task_id,
        timeout=1.0,
    )
    service._storage_authority_renew_task_id = None
    await service._close_selected_storage()


async def test_storage_authority_loop_renews_current_writer(
    tmp_path: Path,
) -> None:
    service = _selected_service(tmp_path, BackendKind.MYSQL)
    runtime = _FakeRuntime(BackendKind.MYSQL)
    service._storage_runtime = runtime
    service._stop_event = asyncio.Event()
    service._storage_factory_settings = replace(
        service._storage_factory_settings,
        authority_lease_seconds=3,
        authority_renew_interval_seconds=1,
    )

    task = asyncio.create_task(service._renew_storage_authority_loop())
    await asyncio.sleep(1.1)
    service._stop_event.set()
    await task

    assert runtime.renew_calls == [3]
    assert runtime.invalidated is False


async def test_storage_authority_loop_invalidates_writer_on_renew_failure(
    tmp_path: Path,
) -> None:
    service = _selected_service(tmp_path, BackendKind.MYSQL)
    runtime = _FakeRuntime(BackendKind.MYSQL)
    runtime.renew_error = RuntimeError("lease lost")
    service._storage_runtime = runtime
    service._stop_event = asyncio.Event()
    service._storage_factory_settings = replace(
        service._storage_factory_settings,
        authority_lease_seconds=3,
        authority_renew_interval_seconds=1,
    )

    await service._renew_storage_authority_loop()

    assert runtime.renew_calls == [3]
    assert runtime.invalidated is True
    assert service.health()["storage_runtime"]["status"] == "failed"


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
    assert runtimes[0].revoke_calls == 1
    assert runtimes[0].close_calls == 1
    assert service.health()["storage_runtime"]["status"] == "failed"
    await service._close_selected_storage()
    assert runtimes[0].close_calls == 1


@pytest.mark.parametrize("backend", [BackendKind.MYSQL])
async def test_selected_service_injects_attention_and_clears_only_instance_focus(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    backend: BackendKind,
) -> None:
    stores = build_fake_stores()
    ledger = _FakeLifeEventStore()
    runtimes, factory_calls = _install_selected_factories(
        monkeypatch,
        backend,
        stores,
        ledger,
    )
    service = _selected_service(tmp_path, backend)
    await service._start_selected_storage()
    actor = ConsciousnessInstance(
        instance_id="attention:service:actor",
        kind="contract",
        stream_ids=["stream:attention:service"],
    )
    await service.register_consciousness_instance(actor)
    command = AttentionThreadCommand(
        occurrence_id="attention:service:decision:1",
        thread_id="attention:service:thread:1",
        action="open",
        actor_consciousness_instance_id=actor.instance_id,
        source_instance_id=actor.instance_id,
        source_occurrence_ids=("life:event:attention:1",),
        causation_occurrence_id="life:event:attention:1",
        expected_revision=0,
        public_statement="我明确选择跨意识实例保留这条关注。",
        occurred_at="2026-08-06T01:02:03+00:00",
    )
    commit = await service.decide_attention_thread(command)
    assert commit.thread_id == command.thread_id
    page = await service.page_attention_threads(
        AttentionThreadPageQuery(projection_kind="service-contract")
    )
    assert page.source_frontier == 1

    class _LearningObserver:
        def __init__(self) -> None:
            self.closes: list[dict[str, Any]] = []

        async def on_attention_thread_closed(self, **payload: Any) -> None:
            self.closes.append(payload)

    learning = _LearningObserver()
    service._learning_scheduler = learning
    close_command = replace(
        command,
        occurrence_id="attention:service:decision:close",
        action="close",
        expected_revision=1,
        public_statement="我明确选择结束，并允许学习系统只读取这句公开表述。",
    )
    await service.decide_attention_thread(close_command)
    await service.decide_attention_thread(close_command)
    assert learning.closes == [
        {
            "public_statement": close_command.public_statement,
            "source_event_ids": [
                "attention:event:fake",
                *close_command.source_occurrence_ids,
            ],
            "actor_consciousness_instance_id": actor.instance_id,
        }
    ]

    focus = InstanceFocus(
        instance_id=actor.instance_id,
        focus_occurrence_id="attention:focus:service:1",
        source_occurrence_id="life:event:attention:1",
        entered_at="2026-08-06T01:02:03+00:00",
        expires_at="2099-08-06T01:07:03+00:00",
        revision=1,
        thread_id=command.thread_id,
    )
    await service.set_instance_attention_focus(focus)
    assert await service.attention_thread_service.get_focus(actor.instance_id) == focus
    assert await service.suspend_consciousness_instance(actor.instance_id)
    assert await service.attention_thread_service.get_focus(actor.instance_id) is None
    # Suspending an instance clears only its ephemeral focus; the subject event
    # and resulting thread remain untouched.
    assert page.source_frontier == 1
    assert (await service.refresh_storage_health())["status"] == "healthy"
    assert ("attention", False) in factory_calls

    await service._close_selected_storage()
    assert runtimes[0].close_calls == 1
    with pytest.raises(RuntimeError, match="AttentionThreadAuthorityNotStarted"):
        _ = service.attention_thread_service


async def test_service_stop_aggregates_consumer_failures_and_closes_runtime_last(
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

    class _FailingMemoryConsumer:
        async def close(self) -> None:
            assert runtimes[0].close_calls == 0
            raise RuntimeError("injected memory consumer close failure")

    class _FailingLearningConsumer:
        async def close(self) -> None:
            assert runtimes[0].close_calls == 0
            raise RuntimeError("injected learning consumer close failure")

    async def _no_op(*, recoverable_on_shared_conflict: bool = False) -> None:
        return None

    async def _noop_cleanup(_workspace_path: Any, *, store: Any | None = None) -> int:
        return 0

    registry = service.consciousness_registry
    assert isinstance(registry, AsyncConsciousnessRegistry)
    await registry.touch(CHAT_GLOBAL_INSTANCE_ID)
    ledger.fail_appends = True
    service._memory_service = _FailingMemoryConsumer()  # type: ignore[assignment]
    service._learning_scheduler = _FailingLearningConsumer()
    monkeypatch.setattr(service, "_save_runtime_context", _no_op)
    monkeypatch.setattr(core_module, "cleanup_autonomy_schedules", _noop_cleanup)

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


async def test_multi_writer_gate_default_keeps_legacy_global_singleton_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default configuration must not change the legacy startup posture."""

    stores = build_fake_stores()
    ledger = _FakeLifeEventStore()
    runtimes, factory_calls = _install_selected_factories(
        monkeypatch,
        BackendKind.MYSQL,
        stores,
        ledger,
    )
    service = _selected_service(tmp_path, BackendKind.MYSQL)
    assert service._storage_factory_settings.multi_writer_enabled is False

    await service._start_selected_storage()

    claim_scopes = {
        (call["namespace"], call["state_key"]) for call in runtimes[0].claim_calls
    }
    assert ("life_engine.runtime_context", "global") in claim_scopes
    assert ("life_engine.learning", "selected_persistence") in claim_scopes
    assert factory_calls


async def test_multi_writer_gate_fails_closed_before_any_claim_or_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicitly enabled multi-writer must refuse startup while hot paths are
    not migrated, before acquiring any claim or attaching any domain store."""

    stores = build_fake_stores()
    ledger = _FakeLifeEventStore()
    runtimes, factory_calls = _install_selected_factories(
        monkeypatch,
        BackendKind.MYSQL,
        stores,
        ledger,
    )
    monkeypatch.setattr(
        "plugins.life_engine.storage.multi_writer_protocol.MULTI_WRITER_HOT_PATHS_READY",
        False,
    )
    service = _selected_service(tmp_path, BackendKind.MYSQL)
    service._storage_factory_settings = replace(
        service._storage_factory_settings,
        multi_writer_enabled=True,
        multi_writer_protocol_version=1,
    )

    with pytest.raises(
        MultiWriterProtocolError,
        match="hot paths are not fully migrated",
    ):
        await service._start_selected_storage()

    assert runtimes[0].claim_calls == []
    assert factory_calls == []


async def test_multi_writer_gate_fails_closed_on_unretired_singleton(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even with hot paths ready, an explicitly enabled node refuses startup
    while the legacy global singleton writer still holds a live claim."""

    stores = build_fake_stores()
    ledger = _FakeLifeEventStore()
    runtimes, factory_calls = _install_selected_factories(
        monkeypatch,
        BackendKind.MYSQL,
        stores,
        ledger,
    )

    async def _fake_observe(_runtime: Any) -> MultiWriterRuntimeState:
        return MultiWriterRuntimeState(
            legacy_singleton_table_present=True,
            total_legacy_global_claims=1,
            live_legacy_global_claims=1,
            multi_writer_tables_present=True,
        )

    monkeypatch.setattr(
        "plugins.life_engine.storage.multi_writer_protocol.observe_multi_writer_state",
        _fake_observe,
    )

    service = _selected_service(tmp_path, BackendKind.MYSQL)
    runtime = _FakeRuntime(BackendKind.MYSQL)
    runtime.generation = SimpleNamespace(
        generation_id="contract-g",
        schema_version=3,
    )
    service._storage_runtime = runtime
    service._storage_factory_settings = replace(
        service._storage_factory_settings,
        multi_writer_enabled=True,
        multi_writer_protocol_version=1,
    )

    with pytest.raises(
        MultiWriterProtocolError,
        match="global singleton writer has not been retired",
    ):
        await service._start_selected_storage()

    assert runtime.claim_calls == []
    assert factory_calls == []


async def test_multi_writer_gate_ready_retires_all_legacy_singleton_claims(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fully migrated node must skip the legacy runtime-context singleton
    claim and the generation-scoped learning singleton claim: both domains
    are shared across nodes in the multi-writer generation (spec 5.2 / 16.2),
    with occurrence identity and projection revision/CAS as the write-conflict
    boundary."""

    stores = build_fake_stores()
    ledger = _FakeLifeEventStore()
    _runtimes, _factory_calls = _install_selected_factories(
        monkeypatch,
        BackendKind.MYSQL,
        stores,
        ledger,
    )
    monkeypatch.setattr(
        "plugins.life_engine.storage.multi_writer_protocol.MULTI_WRITER_HOT_PATHS_READY",
        True,
    )

    async def _fake_observe(_runtime: Any) -> MultiWriterRuntimeState:
        return MultiWriterRuntimeState(
            legacy_singleton_table_present=True,
            total_legacy_global_claims=0,
            live_legacy_global_claims=0,
            multi_writer_tables_present=True,
        )

    monkeypatch.setattr(
        "plugins.life_engine.storage.multi_writer_protocol.observe_multi_writer_state",
        _fake_observe,
    )

    service = _selected_service(tmp_path, BackendKind.MYSQL)
    runtime = _FakeRuntime(BackendKind.MYSQL)
    runtime.generation = SimpleNamespace(
        generation_id="contract-g",
        schema_version=3,
    )
    service._storage_runtime = runtime
    service._storage_factory_settings = replace(
        service._storage_factory_settings,
        multi_writer_enabled=True,
        multi_writer_protocol_version=1,
    )

    await service._start_selected_storage()

    assert runtime.claim_calls == []

    # The ready path attaches a hot-path bridge and registers global transport
    # hook slots; teardown must fully unregister them so later tests never
    # observe a stale bridge from this service instance.
    await service._close_selected_storage()

    assert service._multi_writer_bridge is None
    assert runtime.close_calls == 1
    from src.core.transport import multi_writer_hooks as _mw_hooks

    assert _mw_hooks._inbound_fact_hook is None
    assert _mw_hooks._outbox_intent_hook is None
    assert _mw_hooks._outbox_settle_hook is None
