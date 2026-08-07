"""Service-level lifecycle contracts for selectable Life Memory storage."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from plugins.life_engine.memory.service import LifeMemoryService
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
    ) -> None:
        self.close_calls = 0
        self.health_calls = 0
        self.health_status = health_status
        self.health_error = health_error
        self.health_delay = health_delay

    async def close(self) -> None:
        self.close_calls += 1

    async def health(self) -> dict[str, Any]:
        self.health_calls += 1
        if self.health_delay:
            await asyncio.sleep(self.health_delay)
        if self.health_error is not None:
            raise self.health_error
        return {"status": self.health_status, "backend": "mysql"}


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

    await service.close()
    await service.close()
    assert runtime.close_calls == 0


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
