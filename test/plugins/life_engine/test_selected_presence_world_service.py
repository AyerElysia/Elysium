"""Service-level contracts for selectable Presence, World, and Life Events."""

from __future__ import annotations

import asyncio
import copy
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.exc import OperationalError

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
from plugins.life_engine.storage.contracts import ManagedSingletonWriterClaimLost
from plugins.life_engine.storage.models import BackendKind
from plugins.life_engine.storage.multi_writer_protocol import (
    MultiWriterProtocolError,
    MultiWriterRuntimeState,
)
from plugins.life_engine.storage.writer_claims import (
    SingletonWriterClaim,
    SingletonWriterClaimConflict,
    SingletonWriterClaimLost,
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


class _EmptyMappingResult:
    """SQLAlchemy-shaped empty result for contract-only runtime scans."""

    def mappings(self) -> _EmptyMappingResult:
        return self

    def all(self) -> list[dict[str, Any]]:
        return []


class _FakeSession:
    async def execute(self, _statement: Any, _parameters: Any = None) -> Any:
        return _EmptyMappingResult()


class _FakeRuntime:
    def __init__(self, backend: BackendKind) -> None:
        self.enabled = True
        self.backend = backend
        self.backend_identity = f"{backend.value}://service-contract"
        self.generation = SimpleNamespace(
            generation_id="fake-generation",
            schema_version=3,
            source_snapshot_sha256="f" * 64,
        )
        self.close_calls = 0
        self.renew_calls: list[int] = []
        self.revoke_calls = 0
        self.invalidated = False
        self.renew_error: Exception | None = None
        self.claim_calls: list[dict[str, Any]] = []
        self.learning_open_writer_claims: list[Any | None] = []
        self.invalidated_claims: list[Any] = []

    @asynccontextmanager
    async def unit_of_work(self, **_kwargs: Any) -> Any:
        """Expose the same transactional boundary as StorageBackendRuntime.

        These service tests start with no historical proactive decisions, so
        the startup reconciliation legitimately observes two empty ledgers.
        Dedicated proactive-runtime tests cover populated rows and conflicts.
        """

        yield SimpleNamespace(session=_FakeSession())

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

    def invalidate_managed_singleton_writer(self, claim: Any) -> bool:
        self.invalidated_claims.append(claim)
        return True

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


class _FakeInitiativeRecordStore:
    async def health_snapshot(self) -> dict[str, Any]:
        return {
            "component": "proactive_initiative",
            "status": "healthy",
            "open_count": 0,
            "released_count": 0,
            "namespaces": {},
        }

    async def list_seeds(self, *, include_released: bool = False) -> tuple:
        del include_released
        return ()

    async def get_seed(self, _seed_id: str) -> None:
        return None

    async def due_reencounters(self, *, now: str) -> tuple:
        del now
        return ()

    async def pending_outreach(self, *, limit: int = 32) -> tuple:
        del limit
        return ()


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
        # Immutable evidence and singleton projections deliberately use two
        # handles from the same runtime with different write capabilities.
        assert runtime.backend == backend
        runtime.learning_open_writer_claims.append(writer_claim)
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

    async def _open_initiative(runtime: _FakeRuntime) -> _FakeInitiativeRecordStore:
        assert runtime.backend == backend
        factory_calls.append(("initiative", False))
        return _FakeInitiativeRecordStore()

    async def _ensure_proactive_binding(**_kwargs: Any) -> dict[str, Any]:
        return {
            "schema_version": 2,
            "binding_epoch": 1,
            "binding_sha256": "b" * 64,
        }

    async def _verify_proactive_binding(**_kwargs: Any) -> dict[str, Any]:
        return {
            "component": "proactive_backend_binding",
            "status": "healthy",
            "binding_epoch": 1,
            "binding_sha256": "b" * 64,
        }

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
    monkeypatch.setattr(
        "plugins.life_engine.storage.initiative_factory.open_initiative_record_store",
        _open_initiative,
    )
    monkeypatch.setattr(
        "plugins.life_engine.proactive.backend_binding.ensure_proactive_backend_binding",
        _ensure_proactive_binding,
    )
    monkeypatch.setattr(
        "plugins.life_engine.proactive.backend_binding.verify_proactive_backend_binding",
        _verify_proactive_binding,
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
        ("learning", False),
        ("initiative", False),
    ]
    assert first.storage_runtime is runtimes[0]
    assert [
        (call["namespace"], call["state_key"]) for call in runtimes[0].claim_calls
    ] == [
        ("life_engine.runtime_context", "global"),
        ("life_engine.learning", "selected_persistence"),
    ]
    assert len({call["owner_instance_id"] for call in runtimes[0].claim_calls}) == 1
    assert runtimes[0].learning_open_writer_claims[0] is None
    assert (
        runtimes[0].learning_open_writer_claims[1]
        is first._learning_writer_claim
    )
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


async def test_storage_authority_loop_retries_connectivity_unknown_without_invalidation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _selected_service(tmp_path, BackendKind.MYSQL)
    runtime = _FakeRuntime(BackendKind.MYSQL)
    service._storage_runtime = runtime
    service._stop_event = asyncio.Event()
    service._storage_factory_settings = replace(
        service._storage_factory_settings,
        authority_lease_seconds=3,
        authority_renew_interval_seconds=0,
    )
    attempts = 0

    async def _renew(*, lease_seconds: int) -> None:
        nonlocal attempts
        runtime.renew_calls.append(lease_seconds)
        attempts += 1
        if attempts == 1:
            raise OperationalError("SELECT 1", {}, ConnectionError("lost"))
        service._stop_event.set()

    runtime.renew_authority = _renew  # type: ignore[method-assign]
    monkeypatch.setattr(
        core_module,
        "_storage_renewal_backoff_seconds",
        lambda *_args, **_kwargs: 0.0,
    )

    await service._renew_storage_authority_loop()

    assert runtime.renew_calls == [3, 3]
    assert runtime.invalidated is False
    renewal = service.health()["storage_runtime"]["authority_renewal"]
    assert renewal["status"] == "healthy"
    assert renewal["consecutive_failures"] == 0
    assert renewal["last_success_at"]


async def test_storage_authority_loop_exposes_connectivity_unknown_health(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _selected_service(tmp_path, BackendKind.MYSQL)
    runtime = _FakeRuntime(BackendKind.MYSQL)
    service._storage_runtime = runtime
    service._stop_event = asyncio.Event()
    service._storage_factory_settings = replace(
        service._storage_factory_settings,
        authority_lease_seconds=3,
        authority_renew_interval_seconds=0,
    )

    async def _renew(*, lease_seconds: int) -> None:
        runtime.renew_calls.append(lease_seconds)
        service._stop_event.set()
        raise OperationalError("SELECT 1", {}, ConnectionError("lost"))

    runtime.renew_authority = _renew  # type: ignore[method-assign]
    monkeypatch.setattr(
        core_module,
        "_storage_renewal_backoff_seconds",
        lambda *_args, **_kwargs: 4.25,
    )

    await service._renew_storage_authority_loop()

    health = service.health()["storage_runtime"]
    assert runtime.invalidated is False
    assert health["status"] == "degraded"
    assert health["authority_renewal"]["reason"] == "renewal_unknown"
    assert health["authority_renewal"]["error_type"] == "OperationalError"
    assert health["authority_renewal"]["retry_in_seconds"] == 4.25
    assert health["authority_renewal"]["next_retry_at"]


async def test_storage_authority_loop_propagates_cancellation_without_invalidation(
    tmp_path: Path,
) -> None:
    service = _selected_service(tmp_path, BackendKind.MYSQL)
    runtime = _FakeRuntime(BackendKind.MYSQL)
    service._storage_runtime = runtime
    service._stop_event = asyncio.Event()
    service._storage_factory_settings = replace(
        service._storage_factory_settings,
        authority_lease_seconds=3,
        authority_renew_interval_seconds=0,
    )

    async def _renew(*, lease_seconds: int) -> None:
        runtime.renew_calls.append(lease_seconds)
        raise asyncio.CancelledError

    runtime.renew_authority = _renew  # type: ignore[method-assign]

    with pytest.raises(asyncio.CancelledError):
        await service._renew_storage_authority_loop()

    assert runtime.invalidated is False
    assert runtime.invalidated_claims == []


async def test_learning_claim_loss_quiesces_only_projector_and_keeps_events(
    tmp_path: Path,
) -> None:
    service = _selected_service(tmp_path, BackendKind.MYSQL)
    runtime = _FakeRuntime(BackendKind.MYSQL)
    service._storage_runtime = runtime
    service._stop_event = asyncio.Event()
    service._storage_factory_settings = replace(
        service._storage_factory_settings,
        authority_lease_seconds=3,
        authority_renew_interval_seconds=0,
    )
    claim = SingletonWriterClaim(
        generation_id="generation-a",
        namespace="life_engine.learning",
        state_key="selected_persistence",
        owner_instance_id="writer-a",
        lease_epoch=7,
        lease_until="2026-08-11T00:00:00+00:00",
        fencing_token="opaque",
    )
    loss = ManagedSingletonWriterClaimLost(
        claim,
        SingletonWriterClaimLost("expired"),
    )
    attempts = 0

    async def _renew(*, lease_seconds: int) -> None:
        nonlocal attempts
        runtime.renew_calls.append(lease_seconds)
        attempts += 1
        if attempts == 1:
            raise loss
        service._stop_event.set()

    class _OwnedScheduler:
        projector_owner = True

        def __init__(self) -> None:
            self.quiesced = False

        def quiesce_projector(self, **_kwargs: Any) -> None:
            self.quiesced = True

    owned_scheduler = _OwnedScheduler()
    event_store = _FakeLearningStore()
    runtime.renew_authority = _renew  # type: ignore[method-assign]
    service._learning_writer_claim = claim
    service._learning_event_store = event_store
    service._learning_stores = SimpleNamespace(store=event_store)
    service._learning_scheduler = owned_scheduler

    await service._renew_storage_authority_loop()

    assert runtime.invalidated is False
    assert runtime.invalidated_claims == [claim]
    assert owned_scheduler.quiesced is True
    assert service._learning_writer_claim is None
    assert service._learning_stores is None
    assert service._learning_event_store is event_store
    assert service._learning_scheduler.projector_owner is False
    assert not hasattr(service._learning_scheduler, "store")
    learning_health = service.health()["storage_runtime"]["learning"]
    assert learning_health["status"] == "degraded"
    assert learning_health["projector_owner"] is False
    assert learning_health["event_append_available"] is True


async def test_learning_claim_loss_snapshot_mismatch_quiesces_worker_too(
    tmp_path: Path,
) -> None:
    """removed=False (snapshot mismatch) must quiesce the learning worker as
    well as failing closed, so worker and renewal loop die together (F1-A)."""

    service = _selected_service(tmp_path, BackendKind.MYSQL)
    runtime = _FakeRuntime(BackendKind.MYSQL)

    def _refuse_invalidation(claim: Any) -> bool:
        runtime.invalidated_claims.append(claim)
        return False  # snapshot mismatch: the local claim cannot be removed

    runtime.invalidate_managed_singleton_writer = _refuse_invalidation  # type: ignore[method-assign]
    service._storage_runtime = runtime
    service._stop_event = asyncio.Event()
    service._storage_factory_settings = replace(
        service._storage_factory_settings,
        authority_lease_seconds=3,
        authority_renew_interval_seconds=0,
    )
    claim = SingletonWriterClaim(
        generation_id="generation-a",
        namespace="life_engine.learning",
        state_key="selected_persistence",
        owner_instance_id="writer-a",
        lease_epoch=7,
        lease_until="2026-08-11T00:00:00+00:00",
        fencing_token="opaque",
    )
    loss = ManagedSingletonWriterClaimLost(
        claim,
        SingletonWriterClaimLost("expired"),
    )
    runtime.renew_error = loss

    class _OwnedScheduler:
        projector_owner = True

        def __init__(self) -> None:
            self.quiesced = False

        def quiesce_projector(self, **_kwargs: Any) -> None:
            self.quiesced = True

    owned_scheduler = _OwnedScheduler()
    event_store = _FakeLearningStore()
    service._learning_writer_claim = claim
    service._learning_event_store = event_store
    service._learning_stores = SimpleNamespace(store=event_store)
    service._learning_scheduler = owned_scheduler

    await asyncio.wait_for(service._renew_storage_authority_loop(), timeout=10)

    assert runtime.invalidated is True
    assert runtime.invalidated_claims == [claim]
    assert owned_scheduler.quiesced is True
    assert service._learning_writer_claim is None
    assert service._learning_stores is None
    assert service._learning_scheduler.projector_owner is False
    assert not hasattr(service._learning_scheduler, "store")
    health = service.health()["storage_runtime"]
    assert health["status"] == "failed"
    learning_health = health["learning"]
    assert learning_health["status"] == "degraded"
    assert learning_health["projector_owner"] is False
    assert learning_health["event_append_available"] is True


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
    assert await service.proactive_authority.get_attention_focus(actor.instance_id) == focus
    assert await service.suspend_consciousness_instance(actor.instance_id)
    assert await service.proactive_authority.get_attention_focus(actor.instance_id) is None
    # Suspending an instance clears only its ephemeral focus; the subject event
    # and resulting thread remain untouched.
    assert page.source_frontier == 1
    assert (await service.refresh_storage_health())["status"] == "healthy"
    assert ("attention", False) in factory_calls

    await service._close_selected_storage()
    assert runtimes[0].close_calls == 1
    with pytest.raises(RuntimeError, match="ProactiveAuthorityNotStarted"):
        _ = service.proactive_authority


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


async def test_service_stop_releases_writer_when_runtime_context_save_fails(
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

    async def _failed_save(
        *,
        recoverable_on_shared_conflict: bool = False,
    ) -> None:
        raise RuntimeError("injected runtime-context revision conflict")

    async def _no_op() -> None:
        return None

    monkeypatch.setattr(service, "_save_runtime_context", _failed_save)
    monkeypatch.setattr(
        core_module,
        "cleanup_autonomy_schedules",
        lambda *_args, **_kwargs: _no_op(),
    )

    with pytest.raises(ExceptionGroup, match="consumer failures") as captured:
        await service.stop()

    rendered = "\n".join(str(item) for item in captured.value.exceptions)
    assert "injected runtime-context revision conflict" in rendered
    assert runtimes[0].revoke_calls == 1
    assert runtimes[0].close_calls == 1
    assert service._storage_runtime is None


async def test_failed_selected_storage_start_releases_partial_writer_claims(
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

    async def _failed_learning_open(
        *_args: Any,
        writer_claim: Any | None = None,
        **_kwargs: Any,
    ) -> Any:
        if writer_claim is not None:
            raise RuntimeError("injected learning store attachment failure")
        return SimpleNamespace(store=_FakeLearningStore())

    monkeypatch.setattr(
        "plugins.life_engine.storage.learning_factory.open_learning_stores",
        _failed_learning_open,
    )

    with pytest.raises(RuntimeError, match="attachment failure"):
        await service._start_selected_storage()

    assert len(runtimes[0].claim_calls) == 2
    assert service._runtime_state_store is None
    await service.stop()

    assert runtimes[0].revoke_calls == 1
    assert runtimes[0].close_calls == 1
    assert service._storage_runtime is None


async def test_selected_storage_waits_for_crash_left_claim_then_takes_over(
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
    await service._open_selected_storage_runtime()
    runtime = runtimes[0]
    original_acquire = runtime.acquire_singleton_writer
    runtime_context_attempts = 0

    async def _acquire_after_expiry(**kwargs: Any) -> Any:
        nonlocal runtime_context_attempts
        if kwargs["namespace"] == "life_engine.runtime_context":
            runtime_context_attempts += 1
            if runtime_context_attempts < 3:
                raise SingletonWriterClaimConflict(
                    "SingletonWriterAlreadyClaimed:life_engine.runtime_context:"
                    "global:owner=previous:epoch=9"
                )
        return await original_acquire(**kwargs)

    async def _fast_sleep(_delay: float) -> None:
        return None

    runtime.acquire_singleton_writer = _acquire_after_expiry
    monkeypatch.setattr(core_module.asyncio, "sleep", _fast_sleep)

    await service._start_selected_storage()

    assert runtime_context_attempts == 3
    assert [call["namespace"] for call in runtime.claim_calls] == [
        "life_engine.runtime_context",
        "life_engine.learning",
    ]
    await service._close_selected_storage()


async def test_selected_storage_live_claim_times_out_without_stealing(
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
    await service._open_selected_storage_runtime()
    runtime = runtimes[0]
    attempts = 0

    async def _always_conflict(**_kwargs: Any) -> Any:
        nonlocal attempts
        attempts += 1
        raise SingletonWriterClaimConflict(
            "SingletonWriterAlreadyClaimed:life_engine.runtime_context:"
            "global:owner=live:epoch=10"
        )

    async def _fast_sleep(_delay: float) -> None:
        return None

    runtime.acquire_singleton_writer = _always_conflict
    monotonic_values = iter((100.0, 100.0, 221.1))
    monkeypatch.setattr(
        core_module,
        "time",
        SimpleNamespace(monotonic=lambda: next(monotonic_values)),
    )
    monkeypatch.setattr(core_module.asyncio, "sleep", _fast_sleep)

    async def _start_selected_only() -> None:
        await service._start_selected_storage()

    monkeypatch.setattr(service, "_start_impl", _start_selected_only)

    with pytest.raises(SingletonWriterClaimConflict, match="owner=live:epoch=10"):
        await service.start()

    assert attempts == 2
    assert runtime.revoke_calls == 1
    assert runtime.close_calls == 1


async def test_selected_storage_claim_wait_is_cancellable_and_cleans_runtime(
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
    await service._open_selected_storage_runtime()
    runtime = runtimes[0]
    conflict_seen = asyncio.Event()
    sleep_started = asyncio.Event()

    async def _always_conflict(**_kwargs: Any) -> Any:
        conflict_seen.set()
        raise SingletonWriterClaimConflict(
            "SingletonWriterAlreadyClaimed:life_engine.runtime_context:"
            "global:owner=previous:epoch=9"
        )

    async def _cancellable_sleep(_delay: float) -> None:
        sleep_started.set()
        await asyncio.Event().wait()

    runtime.acquire_singleton_writer = _always_conflict
    monkeypatch.setattr(core_module.asyncio, "sleep", _cancellable_sleep)

    async def _start_selected_only() -> None:
        await service._start_selected_storage()

    monkeypatch.setattr(service, "_start_impl", _start_selected_only)

    startup = asyncio.create_task(service.start())
    await asyncio.wait_for(conflict_seen.wait(), timeout=1.0)
    await asyncio.wait_for(sleep_started.wait(), timeout=1.0)
    startup.cancel()
    with pytest.raises(asyncio.CancelledError):
        await startup

    assert runtime.revoke_calls == 1
    assert runtime.close_calls == 1


async def test_selected_storage_learning_claim_degrades_when_other_owner_holds_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second instance never blocks plugin startup over the Learning
    projector singleton.  When another live owner holds the claim past the
    shared deadline, this instance degrades the mutable selected
    projections/maintenance domain to ``None`` while immutable learning
    evidence stays appendable through the unclaimed handle."""

    stores = build_fake_stores()
    ledger = _FakeLifeEventStore()
    runtimes, _ = _install_selected_factories(
        monkeypatch,
        BackendKind.MYSQL,
        stores,
        ledger,
    )
    service = _selected_service(tmp_path, BackendKind.MYSQL)
    await service._open_selected_storage_runtime()
    runtime = runtimes[0]
    original_acquire = runtime.acquire_singleton_writer
    learning_attempts = 0

    async def _learning_conflict(**kwargs: Any) -> Any:
        nonlocal learning_attempts
        if kwargs["namespace"] == "life_engine.learning":
            learning_attempts += 1
            raise SingletonWriterClaimConflict(
                "SingletonWriterAlreadyClaimed:life_engine.learning:"
                "selected_persistence:owner=live:epoch=4"
            )
        return await original_acquire(**kwargs)

    runtime.acquire_singleton_writer = _learning_conflict
    # 100.0 = claim_deadline base; 221.1 > 100.0 + 121.0 so the learning claim
    # hits the shared deadline on its first conflict and degrades to None
    # instead of raising and failing plugin startup.
    monotonic_values = iter((100.0, 221.1))
    monkeypatch.setattr(
        core_module,
        "time",
        SimpleNamespace(monotonic=lambda: next(monotonic_values)),
    )

    async def _start_selected_only() -> None:
        await service._start_selected_storage()

    monkeypatch.setattr(service, "_start_impl", _start_selected_only)

    # Startup must succeed despite the live other-owner learning claim.
    await service.start()

    assert learning_attempts == 1
    assert service._learning_writer_claim is None
    assert service._learning_stores is None
    # Immutable evidence handle stays attached so this instance keeps
    # appending learning events; the projector handle is dropped.
    assert service._learning_event_store is not None
    event_only = service._build_learning_runtime(workspace_path=tmp_path)
    assert event_only.projector_owner is False
    assert not hasattr(event_only, "store")
    assert event_only.get_state()["event_append_available"] is True
    # The fake records the unclaimed handle only (writer_claim=None).
    assert runtime.learning_open_writer_claims == [None]
    # The runtime-context claim (legacy path) was acquired successfully and is
    # still owned, so no revoke/close happens during a normal startup.
    assert runtime.revoke_calls == 0
    assert runtime.close_calls == 0


def _mysql_lock_wait_timeout() -> OperationalError:
    """Build a MySQL 1205 row-lock wait timeout wrapped by SQLAlchemy."""
    return OperationalError(
        "SELECT ... FOR UPDATE",
        {},
        Exception(
            1205,
            "Lock wait timeout exceeded; try restarting transaction",
        ),
    )


async def test_selected_storage_1205_retries_then_succeeds_within_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MySQL 1205 is transient row-lock contention, not ownership evidence.

    A second node racing the same singleton claim row must wait out the
    transient lock timeout and take over as soon as the owning session
    releases the row -- exactly like SingletonWriterClaimConflict.
    """

    stores = build_fake_stores()
    ledger = _FakeLifeEventStore()
    runtimes, _ = _install_selected_factories(
        monkeypatch,
        BackendKind.MYSQL,
        stores,
        ledger,
    )
    service = _selected_service(tmp_path, BackendKind.MYSQL)
    await service._open_selected_storage_runtime()
    runtime = runtimes[0]
    original_acquire = runtime.acquire_singleton_writer
    runtime_context_attempts = 0

    async def _acquire_1205_then_succeed(**kwargs: Any) -> Any:
        nonlocal runtime_context_attempts
        if kwargs["namespace"] == "life_engine.runtime_context":
            runtime_context_attempts += 1
            if runtime_context_attempts < 3:
                raise _mysql_lock_wait_timeout()
        return await original_acquire(**kwargs)

    async def _fast_sleep(_delay: float) -> None:
        return None

    runtime.acquire_singleton_writer = _acquire_1205_then_succeed
    monkeypatch.setattr(core_module.asyncio, "sleep", _fast_sleep)

    await service._start_selected_storage()

    assert runtime_context_attempts == 3
    assert [call["namespace"] for call in runtime.claim_calls] == [
        "life_engine.runtime_context",
        "life_engine.learning",
    ]
    await service._close_selected_storage()


async def test_selected_storage_learning_claim_1205_degrades_not_fails_startup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Persistent MySQL 1205 on the learning claim must not fail startup.

    The owning Linux instance holds the claim and keeps renewing it with short
    ``FOR UPDATE`` transactions.  An occasional Windows guest can hit 1205 on
    every attempt; past the shared deadline it degrades the mutable selected
    projections/maintenance domain to ``None`` while immutable learning
    evidence stays appendable through the unclaimed handle.
    """

    stores = build_fake_stores()
    ledger = _FakeLifeEventStore()
    runtimes, _ = _install_selected_factories(
        monkeypatch,
        BackendKind.MYSQL,
        stores,
        ledger,
    )
    service = _selected_service(tmp_path, BackendKind.MYSQL)
    await service._open_selected_storage_runtime()
    runtime = runtimes[0]
    original_acquire = runtime.acquire_singleton_writer
    learning_attempts = 0

    async def _learning_1205(**kwargs: Any) -> Any:
        nonlocal learning_attempts
        if kwargs["namespace"] == "life_engine.learning":
            learning_attempts += 1
            raise _mysql_lock_wait_timeout()
        return await original_acquire(**kwargs)

    runtime.acquire_singleton_writer = _learning_1205
    # 100.0 = claim_deadline base; 221.1 > 100.0 + 121.0 so the learning claim
    # hits the shared deadline on its first conflict and degrades to None
    # instead of raising and failing plugin startup.
    monotonic_values = iter((100.0, 221.1))
    monkeypatch.setattr(
        core_module,
        "time",
        SimpleNamespace(monotonic=lambda: next(monotonic_values)),
    )

    async def _start_selected_only() -> None:
        await service._start_selected_storage()

    monkeypatch.setattr(service, "_start_impl", _start_selected_only)

    # Startup must succeed despite the live other-owner learning claim.
    await service.start()

    assert learning_attempts == 1
    assert service._learning_writer_claim is None
    assert service._learning_stores is None
    # Immutable evidence handle stays attached so this instance keeps
    # appending learning events; the projector handle is dropped.
    assert service._learning_event_store is not None
    event_only = service._build_learning_runtime(workspace_path=tmp_path)
    assert event_only.projector_owner is False
    assert not hasattr(event_only, "store")
    assert event_only.get_state()["event_append_available"] is True
    assert runtime.learning_open_writer_claims == [None]
    assert runtime.revoke_calls == 0
    assert runtime.close_calls == 0


async def test_selected_storage_1205_never_swallows_unrelated_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only MySQL 1205 joins the wait-then-degrade path.

    A different SQLAlchemy OperationalError (e.g. 2006 server gone away,
    1064 syntax) must propagate unchanged instead of being mistaken for
    transient row-lock contention.
    """

    stores = build_fake_stores()
    ledger = _FakeLifeEventStore()
    runtimes, _ = _install_selected_factories(
        monkeypatch,
        BackendKind.MYSQL,
        stores,
        ledger,
    )
    service = _selected_service(tmp_path, BackendKind.MYSQL)
    await service._open_selected_storage_runtime()
    runtime = runtimes[0]

    async def _unrelated_error(**_kwargs: Any) -> Any:
        raise OperationalError(
            "SELECT ... FOR UPDATE",
            {},
            Exception(
                2006,
                "MySQL server has gone away",
            ),
        )

    async def _fast_sleep(_delay: float) -> None:
        return None

    runtime.acquire_singleton_writer = _unrelated_error
    monkeypatch.setattr(core_module.asyncio, "sleep", _fast_sleep)

    async def _start_selected_only() -> None:
        await service._start_selected_storage()

    monkeypatch.setattr(service, "_start_impl", _start_selected_only)

    with pytest.raises(OperationalError, match="MySQL server has gone away"):
        await service.start()

    assert runtime.revoke_calls == 1
    assert runtime.close_calls == 1


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
    _runtimes, factory_calls = _install_selected_factories(
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
        source_snapshot_sha256="c" * 64,
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


async def test_multi_writer_gate_keeps_learning_projector_singleton(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Multi-writer retires runtime-context serialization, not Learning's
    dedicated projector owner. Evidence remains multi-writer while the global
    selected projection and maintenance loop retain one database-fenced owner.
    """

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
        source_snapshot_sha256="c" * 64,
    )
    service._storage_runtime = runtime
    service._storage_factory_settings = replace(
        service._storage_factory_settings,
        multi_writer_enabled=True,
        multi_writer_protocol_version=1,
    )

    await service._start_selected_storage()

    assert [
        (call["namespace"], call["state_key"])
        for call in runtime.claim_calls
    ] == [("life_engine.learning", "selected_persistence")]
    assert runtime.learning_open_writer_claims == [
        None,
        service._learning_writer_claim,
    ]

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


async def test_async_registry_get_for_stream_falls_back_to_chat_global() -> None:
    """未绑定实例的流必须回退到 chat_global，与同步 registry 契约一致。

    心跳工具以 stream_id="chat_global" 为身份调用 conversation_evidence；
    若 get_for_stream 对未绑定流返回 None，会触发 instance_unverified /
    cross_instance_denied，即使流和消息都存在。回退到 chat_global 使
    未绑定流归属默认全局聊天窗口，读取证据不再误判。
    """
    stores = build_fake_stores()
    registry = await AsyncConsciousnessRegistry.load(stores.presence)
    await registry._ensure_chat_global()

    # 绑定实例显式占有的流 → 返回该实例
    await registry.register(
        ConsciousnessInstance(
            instance_id="instance:voice",
            kind="voice",
            stream_ids=["stream:voice-owned"],
        )
    )
    owner = registry.get_for_stream("stream:voice-owned")
    assert owner is not None
    assert owner.instance_id == "instance:voice"

    # 未绑定流 → 回退 chat_global（而非 None）
    fallback = registry.get_for_stream("20403fdb0e6df94137c9071e62c44c09eb8090b534279ef5695c4b4aa5fae7bc")
    assert fallback is not None
    assert fallback.instance_id == CHAT_GLOBAL_INSTANCE_ID

    # 心跳占位身份 chat_global 同样解析到 chat_global 实例
    heartbeat = registry.get_for_stream(CHAT_GLOBAL_INSTANCE_ID)
    assert heartbeat is not None
    assert heartbeat.instance_id == CHAT_GLOBAL_INSTANCE_ID


async def test_local_file_authority_activation_takes_over_crashed_lease(
    tmp_path,
) -> None:
    """本地文件权威激活：接管崩溃残留租约并推进 epoch。

    bootstrap 只注册 generation；service 以进程唯一 owner 激活。前任的
    活动租约（同机实例锁已排除活写者）被 confirm 接管，epoch 单调推进，
    旧 owner 失效。未注册 generation 时显式失败，不静默回退。
    """

    from plugins.life_engine.service.core import _LIFE_LOCAL_FENCING_ENV
    from plugins.life_engine.storage.authority import FileAuthorityRegistry
    from plugins.life_engine.storage.factory import (
        settings_from_life_engine_config,
    )
    from plugins.life_engine.storage.models import (
        BackendGeneration,
        BackendKind as StorageBackendKind,
        GenerationStatus,
    )

    service = _selected_service(tmp_path, StorageBackendKind.LOCAL)
    global_config = CoreConfig(
        storage=CoreConfig.StorageSection(
            backend="local",
            backend_generation="local-activation-v1",
            local_selectable_enabled=True,
            authority_owner_id="activation-contract",
        )
    )
    config = LifeEngineConfig()
    config.settings.workspace_path = str(tmp_path)
    from dataclasses import replace as dc_replace

    settings = settings_from_life_engine_config(
        config,
        global_config=global_config,
    )
    settings = dc_replace(
        settings,
        local=dc_replace(
            settings.local,
            authority_state_path=tmp_path / "authority.json",
        ),
    )

    registry = FileAuthorityRegistry(
        settings.local.authority_state_path,
        registry_id=settings.registry_id,
    )
    generation = BackendGeneration(
        generation_id="local-activation-v1",
        backend=StorageBackendKind.LOCAL,
        schema_version=int(settings.schema_version),
        source_snapshot_sha256="0" * 64,
        root_hashes={},
        frontiers={},
        created_at="2026-08-23T00:00:00+00:00",
        verified_at="2026-08-23T00:01:00+00:00",
        status=GenerationStatus.VERIFIED,
    )
    await registry.register_generation(generation)

    activated_settings, env = await service._activate_local_file_authority(
        settings
    )

    assert activated_settings.authority_epoch >= 1
    assert activated_settings.authority_owner_id.startswith(
        "activation-contract:pid-"
    )
    assert _LIFE_LOCAL_FENCING_ENV in env and env[_LIFE_LOCAL_FENCING_ENV]

    health = await registry.health()
    assert health["active_generation"] == "local-activation-v1"
    first_epoch = int(health["authority_epoch"])

    # 模拟崩溃后重启：新进程 owner 直接 confirm 接管，epoch 推进。
    restarted_settings, _ = await service._activate_local_file_authority(
        activated_settings
    )
    health_after = await registry.health()
    assert int(health_after["authority_epoch"]) == first_epoch + 1
    assert restarted_settings.authority_epoch == first_epoch + 1

    # 未注册的 generation 显式失败，不回退 legacy。
    missing = dc_replace(settings, backend_generation="not-registered")
    with pytest.raises(RuntimeError, match="LocalSelectableGenerationNotRegistered"):
        await service._activate_local_file_authority(missing)
