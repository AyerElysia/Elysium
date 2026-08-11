"""Service-level lifecycle contracts for selectable Life Memory storage."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from plugins.life_engine.memory.service import LifeMemoryService
from plugins.life_engine.memory.workspace_projection_identity import (
    WorkspaceProjectionDeleteEvidenceError,
    WorkspaceProjectionEventKind,
)
from plugins.life_engine.storage.memory import MemoryStorageBundle
from plugins.life_engine.storage.memory.mysql import MySQLMemoryReadinessProbeError
from plugins.life_engine.storage.models import BackendKind, StorageAvailability

_MEMORY_DOMAINS = (
    "document_index",
    "experiences",
    "witnesses",
    "living",
    "epistemic",
    "legacy_graph",
)


class _AvailablePort:
    def __init__(
        self,
        availability: StorageAvailability = StorageAvailability.HEALTHY,
    ) -> None:
        self._availability = availability
        self.availability_calls = 0

    async def availability(self) -> StorageAvailability:
        self.availability_calls += 1
        return self._availability


class _InjectedRuntime:
    enabled = True
    backend = BackendKind.MYSQL

    def __init__(
        self,
        *,
        health_status: str = "healthy",
        health_error: Exception | None = None,
        health_delay: float = 0.0,
        generation_id: str = "test-memory-generation",
        owner_id: str = "test-memory-owner",
    ) -> None:
        self.close_calls = 0
        self.health_calls = 0
        self.health_status = health_status
        self.health_error = health_error
        self.health_delay = health_delay
        self.generation = SimpleNamespace(generation_id=generation_id)
        self.authority_token = SimpleNamespace(owner_id=owner_id)

    async def close(self) -> None:
        self.close_calls += 1

    async def health(self) -> dict[str, Any]:
        self.health_calls += 1
        if self.health_delay:
            await asyncio.sleep(self.health_delay)
        if self.health_error is not None:
            raise self.health_error
        return {"status": self.health_status, "backend": "mysql"}


class _ConcurrentDocumentIndex(_AvailablePort):
    def __init__(self) -> None:
        super().__init__()
        self.active_writes = 0
        self.max_active_writes = 0
        self.upsert_calls = 0

    async def list_indexed_documents(self) -> list[Any]:
        return []

    async def upsert_document(self, *_args: Any, **_kwargs: Any) -> None:
        self.upsert_calls += 1
        self.active_writes += 1
        self.max_active_writes = max(self.max_active_writes, self.active_writes)
        try:
            await asyncio.sleep(0.01)
        finally:
            self.active_writes -= 1

    async def mark_documents_deleted(self, _node_ids: Any) -> int:
        return 0


class _RecoveryLiving(_AvailablePort):
    async def list_artifact_heads(self) -> list[Any]:
        return []

    async def append_artifact(self, *_args: Any, **_kwargs: Any) -> None:
        return None


class _RecoveryLegacyGraph(_AvailablePort):
    async def prune_orphan_edges(self) -> int:
        return 0


class _ProjectionBindingStore:
    def __init__(self) -> None:
        self.binding: Any = None
        self.events: list[Any] = []

    async def load_binding(self, _storage_generation_id: str) -> Any:
        return self.binding

    async def commit_transition(self, transition: Any) -> Any:
        self.binding = transition.binding
        self.events.append(transition.event)
        return self.binding

    async def list_events(self, *_args: Any, **_kwargs: Any) -> list[Any]:
        return list(self.events)


def _bundle(
    *,
    availability: StorageAvailability = StorageAvailability.HEALTHY,
) -> MemoryStorageBundle:
    port = _AvailablePort(availability)
    return MemoryStorageBundle(
        backend=BackendKind.MYSQL,
        document_index=port,  # type: ignore[arg-type]
        experiences=port,  # type: ignore[arg-type]
        witnesses=port,  # type: ignore[arg-type]
        living=port,  # type: ignore[arg-type]
        epistemic=port,  # type: ignore[arg-type]
        legacy_graph=port,  # type: ignore[arg-type]
        workspace_projection=_ProjectionBindingStore(),
    )


@pytest.fixture(autouse=True)
def _healthy_mysql_readiness(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _inspect(_runtime: Any) -> dict[str, StorageAvailability]:
        return {name: StorageAvailability.HEALTHY for name in _MEMORY_DOMAINS}

    monkeypatch.setattr(
        "plugins.life_engine.memory.service.inspect_mysql_memory_readiness",
        _inspect,
    )


@pytest.mark.asyncio
async def test_selected_storage_requires_injected_coherent_runtime(
    tmp_path: Path,
) -> None:
    service = LifeMemoryService(
        tmp_path,
        vector_backend_enabled=False,
        selectable_storage_enabled=True,
    )

    with pytest.raises(RuntimeError, match="no coherent runtime was injected"):
        await service.initialize()

    assert not (tmp_path / ".memory" / "memory.db").exists()


@pytest.mark.asyncio
async def test_mysql_service_never_opens_sqlite_and_never_closes_shared_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _InjectedRuntime()
    stores = _bundle()

    async def _open_mysql(*_args: Any, **_kwargs: Any) -> MemoryStorageBundle:
        return stores

    async def _skip_recovery() -> None:
        return None

    monkeypatch.setattr(
        "plugins.life_engine.memory.service.open_mysql_memory_storage",
        _open_mysql,
    )
    service = LifeMemoryService(
        tmp_path,
        vector_backend_enabled=False,
        storage_runtime=runtime,  # type: ignore[arg-type]
        selectable_storage_enabled=True,
    )
    monkeypatch.setattr(service, "_startup_recovery", _skip_recovery)

    await service.initialize()
    assert service.available is True
    assert runtime.health_calls == 1
    assert stores.document_index.availability_calls == 0
    assert service._db is None
    assert not (tmp_path / ".memory" / "memory.db").exists()
    assert (await service.health_snapshot())["backend"] == "mysql"
    assert runtime.health_calls == 2
    assert stores.document_index.availability_calls == 0

    await service.close()
    await service.close()
    assert runtime.close_calls == 0


@pytest.mark.asyncio
async def test_mysql_projection_binding_degrades_on_a_different_workspace_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second node with a different workspace root must not fail startup.

    Linux is the resident owner and bound the projection to its workspace root.
    An occasional Windows guest has a different root; it must degrade to a
    read-only projection handle (memory read/write still works, only the
    projection inventory commit is skipped) instead of raising
    WorkspaceProjectionRebuildRequired and failing plugin startup.
    """

    runtime = _InjectedRuntime()
    stores = _bundle()

    async def _open_mysql(*_args: Any, **_kwargs: Any) -> MemoryStorageBundle:
        return stores

    async def _skip_recovery() -> None:
        return None

    monkeypatch.setattr(
        "plugins.life_engine.memory.service.open_mysql_memory_storage",
        _open_mysql,
    )
    first_root = tmp_path / "workspace-a"
    second_root = tmp_path / "workspace-b"
    first_root.mkdir()
    second_root.mkdir()
    first = LifeMemoryService(
        first_root,
        vector_backend_enabled=False,
        storage_runtime=runtime,  # type: ignore[arg-type]
        selectable_storage_enabled=True,
    )
    monkeypatch.setattr(first, "_startup_recovery", _skip_recovery)
    await first.initialize()
    await first.close()

    second = LifeMemoryService(
        second_root,
        vector_backend_enabled=False,
        storage_runtime=runtime,  # type: ignore[arg-type]
        selectable_storage_enabled=True,
    )
    monkeypatch.setattr(second, "_startup_recovery", _skip_recovery)

    # The different-root guest degrades to a read-only projection instead of
    # failing startup: memory stays available, only the projection owner
    # handle is dropped.
    await second.initialize()
    assert second.available is True
    assert second._workspace_projection_binding is None
    assert second._workspace_projection_permit is None

    binding_store = stores.workspace_projection
    assert isinstance(binding_store, _ProjectionBindingStore)
    # Only the first (owning) instance appended its initial bind event; the
    # degraded guest never appends a projection transition.
    assert len(binding_store.events) == 1

    await second.close()


@pytest.mark.asyncio
async def test_mysql_projection_binding_degrades_when_other_owner_holds_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second node sharing the workspace must not fail over the owner.

    The resident Linux instance already owns the projection for this exact
    workspace root.  An occasional Windows guest is configured with a
    different authority owner but the same storage generation; it must degrade
    to a read-only projection (memory stays available) instead of raising
    WorkspaceProjectionBindingConflict and failing plugin startup.
    """

    linux_runtime = _InjectedRuntime(owner_id="elysium-linux-primary")
    binding_store = _ProjectionBindingStore()
    passive = _AvailablePort()
    stores = MemoryStorageBundle(
        backend=BackendKind.MYSQL,
        document_index=_ConcurrentDocumentIndex(),  # type: ignore[arg-type]
        experiences=passive,  # type: ignore[arg-type]
        witnesses=passive,  # type: ignore[arg-type]
        living=_RecoveryLiving(),  # type: ignore[arg-type]
        epistemic=passive,  # type: ignore[arg-type]
        legacy_graph=_RecoveryLegacyGraph(),  # type: ignore[arg-type]
        workspace_projection=binding_store,
    )

    async def _open_mysql(*_args: Any, **_kwargs: Any) -> MemoryStorageBundle:
        return stores

    async def _skip_recovery() -> None:
        return None

    monkeypatch.setattr(
        "plugins.life_engine.memory.service.open_mysql_memory_storage",
        _open_mysql,
    )
    workspace = tmp_path / "shared-workspace"
    workspace.mkdir()
    owner = LifeMemoryService(
        workspace,
        vector_backend_enabled=False,
        storage_runtime=linux_runtime,  # type: ignore[arg-type]
        selectable_storage_enabled=True,
    )
    monkeypatch.setattr(owner, "_startup_recovery", _skip_recovery)
    await owner.initialize()
    await owner.close()
    assert len(binding_store.events) == 1

    windows_runtime = _InjectedRuntime(owner_id="elysium-windows-primary")
    guest = LifeMemoryService(
        workspace,
        vector_backend_enabled=False,
        storage_runtime=windows_runtime,  # type: ignore[arg-type]
        selectable_storage_enabled=True,
    )
    monkeypatch.setattr(guest, "_startup_recovery", _skip_recovery)

    # The guest with a different owner but the same root degrades to a
    # read-only projection instead of failing startup.
    await guest.initialize()
    assert guest.available is True
    assert guest._workspace_projection_binding is None
    assert guest._workspace_projection_permit is None
    # The degraded guest never appends a projection transition.
    assert len(binding_store.events) == 1

    await guest.close()


@pytest.mark.asyncio
async def test_selected_workspace_destructive_projection_mutations_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _InjectedRuntime()
    stores = _bundle()

    async def _open_mysql(*_args: Any, **_kwargs: Any) -> MemoryStorageBundle:
        return stores

    async def _skip_recovery() -> None:
        return None

    monkeypatch.setattr(
        "plugins.life_engine.memory.service.open_mysql_memory_storage",
        _open_mysql,
    )
    service = LifeMemoryService(
        tmp_path,
        vector_backend_enabled=False,
        storage_runtime=runtime,  # type: ignore[arg-type]
        selectable_storage_enabled=True,
    )
    monkeypatch.setattr(service, "_startup_recovery", _skip_recovery)
    await service.initialize()

    with pytest.raises(
        WorkspaceProjectionDeleteEvidenceError,
        match="occurrence-bound audited deletion port",
    ):
        await service.delete_document("notes/old.md")
    with pytest.raises(
        WorkspaceProjectionDeleteEvidenceError,
        match="occurrence-bound audited port",
    ):
        await service.move_document("notes/old.md", "notes/new.md")

    await service.close()


@pytest.mark.asyncio
async def test_mysql_workspace_recovery_does_not_block_plugin_availability(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _InjectedRuntime()
    started = asyncio.Event()
    release = asyncio.Event()

    async def _open_mysql(*_args: Any, **_kwargs: Any) -> MemoryStorageBundle:
        return _bundle()

    async def _blocking_recovery() -> None:
        started.set()
        await release.wait()

    monkeypatch.setattr(
        "plugins.life_engine.memory.service.open_mysql_memory_storage",
        _open_mysql,
    )
    service = LifeMemoryService(
        tmp_path,
        vector_backend_enabled=False,
        storage_runtime=runtime,  # type: ignore[arg-type]
        selectable_storage_enabled=True,
    )
    monkeypatch.setattr(service, "_startup_recovery", _blocking_recovery)

    await asyncio.wait_for(service.initialize(), timeout=1.0)
    await asyncio.wait_for(started.wait(), timeout=1.0)

    assert service.available is True
    assert service._startup_recovery_task is not None
    assert not service._startup_recovery_task.done()
    snapshot = await service.health_snapshot()
    assert snapshot["status"] == "degraded"
    assert snapshot["startup_recovery"]["status"] == "running"

    release.set()
    await asyncio.wait_for(service._startup_recovery_task, timeout=1.0)
    assert service._startup_recovery_progress.status == "completed"
    await service.close()


@pytest.mark.asyncio
async def test_mysql_workspace_recovery_uses_bounded_write_concurrency(
    tmp_path: Path,
) -> None:
    for index in range(24):
        path = tmp_path / "notes" / f"recovery-{index:02d}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"workspace memory {index}", encoding="utf-8")

    document_index = _ConcurrentDocumentIndex()
    passive = _AvailablePort()
    storage = MemoryStorageBundle(
        backend=BackendKind.MYSQL,
        document_index=document_index,  # type: ignore[arg-type]
        experiences=passive,  # type: ignore[arg-type]
        witnesses=passive,  # type: ignore[arg-type]
        living=_RecoveryLiving(),  # type: ignore[arg-type]
        epistemic=passive,  # type: ignore[arg-type]
        legacy_graph=_RecoveryLegacyGraph(),  # type: ignore[arg-type]
    )
    service = LifeMemoryService(
        tmp_path,
        vector_backend_enabled=False,
        memory_storage=storage,
    )

    await service.initialize()
    task = service._startup_recovery_task
    assert task is not None
    await asyncio.wait_for(task, timeout=2.0)

    assert document_index.upsert_calls == 24
    assert 1 < document_index.max_active_writes <= 8
    assert service._startup_recovery_progress.processed_documents == 24
    assert service._startup_recovery_progress.artifact_processed == 24
    await service.close()


@pytest.mark.asyncio
async def test_mysql_successful_recovery_appends_inventory_commit_after_bind(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = tmp_path / "notes" / "continuity.md"
    document.parent.mkdir(parents=True)
    document.write_text("one stable memory", encoding="utf-8")
    runtime = _InjectedRuntime()
    binding_store = _ProjectionBindingStore()
    passive = _AvailablePort()
    stores = MemoryStorageBundle(
        backend=BackendKind.MYSQL,
        document_index=_ConcurrentDocumentIndex(),  # type: ignore[arg-type]
        experiences=passive,  # type: ignore[arg-type]
        witnesses=passive,  # type: ignore[arg-type]
        living=_RecoveryLiving(),  # type: ignore[arg-type]
        epistemic=passive,  # type: ignore[arg-type]
        legacy_graph=_RecoveryLegacyGraph(),  # type: ignore[arg-type]
        workspace_projection=binding_store,
    )

    async def _open_mysql(*_args: Any, **_kwargs: Any) -> MemoryStorageBundle:
        return stores

    monkeypatch.setattr(
        "plugins.life_engine.memory.service.open_mysql_memory_storage",
        _open_mysql,
    )
    service = LifeMemoryService(
        tmp_path,
        vector_backend_enabled=False,
        storage_runtime=runtime,  # type: ignore[arg-type]
        selectable_storage_enabled=True,
    )

    await service.initialize()
    task = service._startup_recovery_task
    if task is not None:
        await asyncio.wait_for(task, timeout=2.0)

    assert [event.event_kind for event in binding_store.events] == [
        WorkspaceProjectionEventKind.OWNER_BOUND,
        WorkspaceProjectionEventKind.INVENTORY_COMMITTED,
    ]
    assert binding_store.events[-1].eligible_inventory_sha256 == (
        binding_store.events[0].eligible_inventory_sha256
    )
    await service.close()


@pytest.mark.asyncio
async def test_mysql_workspace_recovery_is_single_flight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _InjectedRuntime()
    started = asyncio.Event()
    release = asyncio.Event()
    recovery_calls = 0

    async def _open_mysql(*_args: Any, **_kwargs: Any) -> MemoryStorageBundle:
        return _bundle()

    async def _blocking_recovery() -> None:
        nonlocal recovery_calls
        recovery_calls += 1
        started.set()
        await release.wait()

    monkeypatch.setattr(
        "plugins.life_engine.memory.service.open_mysql_memory_storage",
        _open_mysql,
    )
    service = LifeMemoryService(
        tmp_path,
        vector_backend_enabled=False,
        storage_runtime=runtime,  # type: ignore[arg-type]
        selectable_storage_enabled=True,
    )
    monkeypatch.setattr(service, "_startup_recovery", _blocking_recovery)

    await service.initialize()
    await asyncio.wait_for(started.wait(), timeout=1.0)
    task = service._startup_recovery_task
    await service.initialize()

    assert recovery_calls == 1
    assert service._startup_recovery_task is task
    release.set()
    assert task is not None
    await asyncio.wait_for(task, timeout=1.0)
    await service.close()


@pytest.mark.asyncio
async def test_close_cancels_and_joins_mysql_workspace_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _InjectedRuntime()
    started = asyncio.Event()
    finished = asyncio.Event()

    async def _open_mysql(*_args: Any, **_kwargs: Any) -> MemoryStorageBundle:
        return _bundle()

    async def _blocking_recovery() -> None:
        started.set()
        try:
            await asyncio.Future()
        finally:
            finished.set()

    monkeypatch.setattr(
        "plugins.life_engine.memory.service.open_mysql_memory_storage",
        _open_mysql,
    )
    service = LifeMemoryService(
        tmp_path,
        vector_backend_enabled=False,
        storage_runtime=runtime,  # type: ignore[arg-type]
        selectable_storage_enabled=True,
    )
    monkeypatch.setattr(service, "_startup_recovery", _blocking_recovery)

    await service.initialize()
    await asyncio.wait_for(started.wait(), timeout=1.0)
    await asyncio.wait_for(service.close(), timeout=1.0)

    assert finished.is_set()
    assert service._startup_recovery_task is None
    assert service._startup_recovery_progress.status == "cancelled"
    assert service.available is False


@pytest.mark.asyncio
async def test_mysql_recovery_failure_is_content_free_degraded_health(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _InjectedRuntime()
    secret = "mysql://elysia:do-not-leak@localhost/life"

    async def _open_mysql(*_args: Any, **_kwargs: Any) -> MemoryStorageBundle:
        return _bundle()

    async def _failed_recovery() -> None:
        raise OSError(secret)

    monkeypatch.setattr(
        "plugins.life_engine.memory.service.open_mysql_memory_storage",
        _open_mysql,
    )
    service = LifeMemoryService(
        tmp_path,
        vector_backend_enabled=False,
        storage_runtime=runtime,  # type: ignore[arg-type]
        selectable_storage_enabled=True,
    )
    monkeypatch.setattr(service, "_startup_recovery", _failed_recovery)

    await service.initialize()
    task = service._startup_recovery_task
    assert task is not None
    await asyncio.wait_for(task, timeout=1.0)
    snapshot = await service.health_snapshot()

    assert service.available is True
    assert snapshot["status"] == "degraded"
    assert snapshot["startup_recovery"]["status"] == "failed"
    assert snapshot["startup_recovery"]["error_type"] == "OSError"
    assert secret not in str(snapshot)
    await service.close()


@pytest.mark.asyncio
async def test_local_recovery_failure_still_fails_initialization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = LifeMemoryService(tmp_path, vector_backend_enabled=False)

    async def _failed_recovery() -> None:
        raise RuntimeError("projection failed")

    monkeypatch.setattr(service, "_startup_recovery", _failed_recovery)

    with pytest.raises(RuntimeError, match="projection failed"):
        await service.initialize()

    assert service.available is False
    assert service._startup_recovery_progress.status == "failed"


@pytest.mark.asyncio
async def test_backend_failure_is_fail_closed_and_preserves_runtime_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _InjectedRuntime(health_status="failed")

    async def _open_mysql(*_args: Any, **_kwargs: Any) -> MemoryStorageBundle:
        return _bundle()

    monkeypatch.setattr(
        "plugins.life_engine.memory.service.open_mysql_memory_storage",
        _open_mysql,
    )
    service = LifeMemoryService(
        tmp_path,
        vector_backend_enabled=False,
        storage_runtime=runtime,  # type: ignore[arg-type]
        selectable_storage_enabled=True,
    )

    with pytest.raises(
        RuntimeError,
        match=(
            "MemoryBackendUnavailable:shared_runtime=failed,"
            "error_type=Unavailable"
        ),
    ):
        await service.initialize()

    assert service.available is False
    assert service._db is None
    assert not (tmp_path / ".memory" / "memory.db").exists()
    assert runtime.close_calls == 0
    assert runtime.health_calls == 1


@pytest.mark.asyncio
async def test_degraded_shared_runtime_still_requires_one_readiness_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _InjectedRuntime(health_status="degraded")
    readiness_calls = 0

    async def _open_mysql(*_args: Any, **_kwargs: Any) -> MemoryStorageBundle:
        return _bundle()

    async def _inspect(_runtime: Any) -> dict[str, StorageAvailability]:
        nonlocal readiness_calls
        readiness_calls += 1
        return {name: StorageAvailability.HEALTHY for name in _MEMORY_DOMAINS}

    async def _skip_recovery() -> None:
        return None

    monkeypatch.setattr(
        "plugins.life_engine.memory.service.open_mysql_memory_storage",
        _open_mysql,
    )
    monkeypatch.setattr(
        "plugins.life_engine.memory.service.inspect_mysql_memory_readiness",
        _inspect,
    )
    service = LifeMemoryService(
        tmp_path,
        vector_backend_enabled=False,
        storage_runtime=runtime,  # type: ignore[arg-type]
        selectable_storage_enabled=True,
    )
    monkeypatch.setattr(service, "_startup_recovery", _skip_recovery)

    await service.initialize()

    assert runtime.health_calls == 1
    assert readiness_calls == 1
    await service.close()


@pytest.mark.asyncio
async def test_shared_runtime_failure_is_content_free_and_skips_readiness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "mysql://elysia:do-not-leak@localhost/life"
    runtime = _InjectedRuntime(health_error=OSError(secret))
    readiness_calls = 0

    async def _open_mysql(*_args: Any, **_kwargs: Any) -> MemoryStorageBundle:
        return _bundle()

    async def _inspect(_runtime: Any) -> dict[str, StorageAvailability]:
        nonlocal readiness_calls
        readiness_calls += 1
        return {name: StorageAvailability.HEALTHY for name in _MEMORY_DOMAINS}

    monkeypatch.setattr(
        "plugins.life_engine.memory.service.open_mysql_memory_storage",
        _open_mysql,
    )
    monkeypatch.setattr(
        "plugins.life_engine.memory.service.inspect_mysql_memory_readiness",
        _inspect,
    )
    service = LifeMemoryService(
        tmp_path,
        vector_backend_enabled=False,
        storage_runtime=runtime,  # type: ignore[arg-type]
        selectable_storage_enabled=True,
    )

    with pytest.raises(RuntimeError) as raised:
        await service.initialize()

    assert str(raised.value) == (
        "MemoryBackendUnavailable:shared_runtime=failed,error_type=OSError"
    )
    assert secret not in str(raised.value)
    assert readiness_calls == 0


@pytest.mark.asyncio
async def test_shared_runtime_probe_has_one_total_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _InjectedRuntime(health_delay=0.2)

    async def _open_mysql(*_args: Any, **_kwargs: Any) -> MemoryStorageBundle:
        return _bundle()

    monkeypatch.setattr(
        "plugins.life_engine.memory.service.open_mysql_memory_storage",
        _open_mysql,
    )
    monkeypatch.setattr(
        "plugins.life_engine.memory.service._MYSQL_MEMORY_STARTUP_PROBE_TIMEOUT_SECONDS",
        0.01,
    )
    service = LifeMemoryService(
        tmp_path,
        vector_backend_enabled=False,
        storage_runtime=runtime,  # type: ignore[arg-type]
        selectable_storage_enabled=True,
    )

    with pytest.raises(RuntimeError) as raised:
        await service.initialize()

    assert str(raised.value) == (
        "MemoryBackendUnavailable:shared_runtime=failed,error_type=TimeoutError"
    )
    assert runtime.health_calls == 1


@pytest.mark.asyncio
async def test_shared_runtime_probe_propagates_cancellation_without_orphan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _InjectedRuntime()
    started = asyncio.Event()
    finished = asyncio.Event()

    async def _open_mysql(*_args: Any, **_kwargs: Any) -> MemoryStorageBundle:
        return _bundle()

    async def _blocking_health() -> dict[str, Any]:
        runtime.health_calls += 1
        started.set()
        try:
            await asyncio.Future()
        finally:
            finished.set()
        return {"status": "healthy"}

    monkeypatch.setattr(
        "plugins.life_engine.memory.service.open_mysql_memory_storage",
        _open_mysql,
    )
    monkeypatch.setattr(runtime, "health", _blocking_health)
    service = LifeMemoryService(
        tmp_path,
        vector_backend_enabled=False,
        storage_runtime=runtime,  # type: ignore[arg-type]
        selectable_storage_enabled=True,
    )

    initialize_task = asyncio.create_task(service.initialize())
    await started.wait()
    initialize_task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await initialize_task

    assert initialize_task.done()
    assert finished.is_set()
    assert runtime.health_calls == 1


@pytest.mark.asyncio
async def test_readiness_failure_names_only_the_missing_domain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _InjectedRuntime()

    async def _open_mysql(*_args: Any, **_kwargs: Any) -> MemoryStorageBundle:
        return _bundle()

    async def _inspect(_runtime: Any) -> dict[str, StorageAvailability]:
        result = {name: StorageAvailability.HEALTHY for name in _MEMORY_DOMAINS}
        result["epistemic"] = StorageAvailability.FAILED
        return result

    monkeypatch.setattr(
        "plugins.life_engine.memory.service.open_mysql_memory_storage",
        _open_mysql,
    )
    monkeypatch.setattr(
        "plugins.life_engine.memory.service.inspect_mysql_memory_readiness",
        _inspect,
    )
    service = LifeMemoryService(
        tmp_path,
        vector_backend_enabled=False,
        storage_runtime=runtime,  # type: ignore[arg-type]
        selectable_storage_enabled=True,
    )

    with pytest.raises(RuntimeError) as raised:
        await service.initialize()

    assert str(raised.value) == "MemoryBackendUnavailable:epistemic=failed"
    assert runtime.health_calls == 1


@pytest.mark.asyncio
async def test_readiness_probe_error_is_content_free(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _InjectedRuntime()

    async def _open_mysql(*_args: Any, **_kwargs: Any) -> MemoryStorageBundle:
        return _bundle()

    async def _inspect(_runtime: Any) -> dict[str, StorageAvailability]:
        raise MySQLMemoryReadinessProbeError("OperationalError")

    monkeypatch.setattr(
        "plugins.life_engine.memory.service.open_mysql_memory_storage",
        _open_mysql,
    )
    monkeypatch.setattr(
        "plugins.life_engine.memory.service.inspect_mysql_memory_readiness",
        _inspect,
    )
    service = LifeMemoryService(
        tmp_path,
        vector_backend_enabled=False,
        storage_runtime=runtime,  # type: ignore[arg-type]
        selectable_storage_enabled=True,
    )

    with pytest.raises(RuntimeError) as raised:
        await service.initialize()

    assert str(raised.value) == (
        "MemoryBackendUnavailable:shared_runtime=failed,"
        "error_type=OperationalError"
    )


@pytest.mark.asyncio
async def test_new_service_initialization_rechecks_same_runtime_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _InjectedRuntime()
    readiness_calls = 0

    async def _open_mysql(*_args: Any, **_kwargs: Any) -> MemoryStorageBundle:
        return _bundle()

    async def _inspect(_runtime: Any) -> dict[str, StorageAvailability]:
        nonlocal readiness_calls
        readiness_calls += 1
        return {name: StorageAvailability.HEALTHY for name in _MEMORY_DOMAINS}

    async def _skip_recovery() -> None:
        return None

    monkeypatch.setattr(
        "plugins.life_engine.memory.service.open_mysql_memory_storage",
        _open_mysql,
    )
    monkeypatch.setattr(
        "plugins.life_engine.memory.service.inspect_mysql_memory_readiness",
        _inspect,
    )

    for _ in range(2):
        service = LifeMemoryService(
            tmp_path,
            vector_backend_enabled=False,
            storage_runtime=runtime,  # type: ignore[arg-type]
            selectable_storage_enabled=True,
        )
        monkeypatch.setattr(service, "_startup_recovery", _skip_recovery)
        await service.initialize()
        await service.close()

    assert runtime.health_calls == 2
    assert readiness_calls == 2


@pytest.mark.asyncio
async def test_default_local_service_restarts_without_behavior_change(
    tmp_path: Path,
) -> None:
    first = LifeMemoryService(tmp_path, vector_backend_enabled=False)
    await first.initialize()
    node = await first.get_or_create_file_node(
        "notes/restart.md",
        title="restart",
        content="local remains the compatibility default",
    )
    await first.close()

    second = LifeMemoryService(tmp_path, vector_backend_enabled=False)
    await second.initialize()
    restored = await second.get_node_by_file_path("notes/restart.md")
    assert restored is not None and restored.node_id == node.node_id
    await second.close()


@pytest.mark.asyncio
async def test_memory_integration_consumes_owner_runtime_without_opening_another(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from plugins.life_engine.memory import service as memory_module
    from plugins.life_engine.service.integrations import MemoryIntegration

    runtime = _InjectedRuntime()
    captured: dict[str, Any] = {}

    class _MemoryService:
        def __init__(self, workspace: Path, **kwargs: Any) -> None:
            captured.update(workspace=workspace, **kwargs)

        async def initialize(self) -> None:
            captured["initialized"] = True

    monkeypatch.setattr(memory_module, "LifeMemoryService", _MemoryService)

    class _Owner:
        _memory_service = None
        _selectable_storage_enabled = True

        @property
        def storage_runtime(self) -> _InjectedRuntime:
            return runtime

        @staticmethod
        def _cfg() -> SimpleNamespace:
            return SimpleNamespace(
                settings=SimpleNamespace(workspace_path=str(tmp_path)),
                memory_index=SimpleNamespace(backend_enabled=False),
            )

    owner = _Owner()

    await MemoryIntegration(owner).init_memory_service()

    assert captured["storage_runtime"] is runtime
    assert captured["selectable_storage_enabled"] is True
    assert captured["initialized"] is True
    assert runtime.close_calls == 0
