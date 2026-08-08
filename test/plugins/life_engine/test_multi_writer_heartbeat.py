from __future__ import annotations

from pathlib import Path

import pytest

from plugins.life_engine.storage.authority import FileAuthorityRegistry
from plugins.life_engine.storage.factory import LocalBackendSettings, StorageFactorySettings, open_storage_backend
from plugins.life_engine.storage.heartbeat_adapters import SQLHeartbeatStore
from plugins.life_engine.storage.heartbeat_contracts import HeartbeatClaimLost, HeartbeatConflict, HeartbeatOperation, HeartbeatStatus
from plugins.life_engine.storage.models import BackendGeneration, BackendKind, GenerationStatus
from plugins.life_engine.storage.runtime_schema import ensure_runtime_state_schema


async def _runtime(tmp_path: Path):
    authority_path = tmp_path / "authority.json"
    registry = FileAuthorityRegistry(authority_path, registry_id="test")
    await registry.register_generation(BackendGeneration("local-test", BackendKind.LOCAL, 1, "a" * 64, {"x": "b" * 64}, {"x": 0}, "2026-08-07T00:00:00+00:00", "2026-08-07T00:00:00+00:00", GenerationStatus.VERIFIED))
    token = await registry.activate_generation("local-test", expected_epoch=0, owner_id="owner", lease_seconds=120, confirm_previous_writers_stopped=True)
    runtime = await open_storage_backend(StorageFactorySettings(enabled=True, authoritative_backend=BackendKind.LOCAL, backend_generation="local-test", authority_provider="file", registry_id="test", authority_epoch=token.authority_epoch, authority_owner_id="owner", local=LocalBackendSettings(tmp_path / "db.sqlite3", authority_path)), environment={"ELYSIUM_LIFE_STORAGE_FENCING_TOKEN": token.fencing_token})
    await ensure_runtime_state_schema(runtime)
    return runtime


def _operation() -> HeartbeatOperation:
    return HeartbeatOperation("hb-1", "chat_global", 1, {"frontier": 0}, "a" * 64, HeartbeatStatus.PENDING, None, 0, None, None, None, None, None, 0, "2026-08-07T00:00:00+00:00", "2026-08-07T00:00:00+00:00")


@pytest.mark.asyncio
async def test_heartbeat_claim_commit_and_replay(tmp_path: Path) -> None:
    runtime = await _runtime(tmp_path)
    try:
        store = SQLHeartbeatStore(runtime)
        await store.register(_operation())
        claimed = await store.claim("hb-1", owner_id="node-a", lease_seconds=30)
        assert claimed is not None
        with pytest.raises(HeartbeatClaimLost):
            await store.commit("hb-1", owner_id="node-b", claim_epoch=claimed.claim_epoch, input_frontier=0, committed_frontier=1, result_ref="req", result_digest="b" * 64)
        completed = await store.commit("hb-1", owner_id="node-a", claim_epoch=claimed.claim_epoch, input_frontier=0, committed_frontier=1, result_ref="req", result_digest="b" * 64)
        assert completed.status == HeartbeatStatus.COMPLETED
        assert await store.claim("hb-1", owner_id="node-b", lease_seconds=30) is None
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_heartbeat_failure_does_not_commit_frontier(tmp_path: Path) -> None:
    runtime = await _runtime(tmp_path)
    try:
        store = SQLHeartbeatStore(runtime)
        await store.register(_operation())
        claimed = await store.claim("hb-1", owner_id="node-a", lease_seconds=30)
        assert claimed is not None
        failed = await store.mark_failed("hb-1", owner_id="node-a", claim_epoch=claimed.claim_epoch, retryable=True)
        assert failed.status == HeartbeatStatus.RETRYABLE
        assert failed.committed_frontier is None
    finally:
        await runtime.close()
