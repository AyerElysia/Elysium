"""Service-level lifecycle contracts for selectable Life Memory storage."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from plugins.life_engine.memory.service import LifeMemoryService
from plugins.life_engine.storage.memory import MemoryStorageBundle
from plugins.life_engine.storage.models import BackendKind, StorageAvailability


class _AvailablePort:
    def __init__(
        self,
        availability: StorageAvailability = StorageAvailability.HEALTHY,
    ) -> None:
        self._availability = availability

    async def availability(self) -> StorageAvailability:
        return self._availability


class _InjectedRuntime:
    enabled = True
    backend = BackendKind.MYSQL

    def __init__(self) -> None:
        self.close_calls = 0

    async def close(self) -> None:
        self.close_calls += 1

    async def health(self) -> dict[str, Any]:
        return {"status": "healthy", "backend": "mysql"}


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
    runtime = _InjectedRuntime()

    async def _open_mysql(*_args: Any, **_kwargs: Any) -> MemoryStorageBundle:
        return _bundle(availability=StorageAvailability.FAILED)

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

    with pytest.raises(RuntimeError, match="MemoryBackendUnavailable"):
        await service.initialize()

    assert service.available is False
    assert service._db is None
    assert not (tmp_path / ".memory" / "memory.db").exists()
    assert runtime.close_calls == 0


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

        @property
        def storage_runtime(self) -> _InjectedRuntime:
            return runtime

        @staticmethod
        def _cfg() -> SimpleNamespace:
            return SimpleNamespace(
                settings=SimpleNamespace(workspace_path=str(tmp_path)),
                memory_index=SimpleNamespace(backend_enabled=False),
                storage=SimpleNamespace(enabled=True),
            )

    owner = _Owner()

    await MemoryIntegration(owner).init_memory_service()

    assert captured["storage_runtime"] is runtime
    assert captured["selectable_storage_enabled"] is True
    assert captured["initialized"] is True
    assert runtime.close_calls == 0
