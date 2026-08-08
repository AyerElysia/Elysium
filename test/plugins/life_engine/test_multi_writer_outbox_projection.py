from __future__ import annotations

from pathlib import Path

import pytest

from plugins.life_engine.storage.authority import FileAuthorityRegistry
from plugins.life_engine.storage.factory import LocalBackendSettings, StorageFactorySettings, open_storage_backend
from plugins.life_engine.storage.models import BackendGeneration, BackendKind, GenerationStatus
from plugins.life_engine.storage.outbox_adapters import SQLOutboxStore
from plugins.life_engine.storage.outbox_contracts import OutboxAction, OutboxClaimLost, OutboxConflict, OutboxStatus
from plugins.life_engine.storage.projection_progress import ProjectionProgressConflict, SQLProjectionProgressStore
from plugins.life_engine.storage.runtime_schema import ensure_runtime_state_schema


async def _runtime(tmp_path: Path):
    authority_path = tmp_path / "authority.json"
    registry = FileAuthorityRegistry(authority_path, registry_id="test")
    await registry.register_generation(BackendGeneration("local-test", BackendKind.LOCAL, 1, "a" * 64, {"x": "b" * 64}, {"x": 0}, "2026-08-07T00:00:00+00:00", "2026-08-07T00:00:00+00:00", GenerationStatus.VERIFIED))
    token = await registry.activate_generation("local-test", expected_epoch=0, owner_id="owner", lease_seconds=120, confirm_previous_writers_stopped=True)
    runtime = await open_storage_backend(StorageFactorySettings(enabled=True, authoritative_backend=BackendKind.LOCAL, backend_generation="local-test", authority_provider="file", registry_id="test", authority_epoch=token.authority_epoch, authority_owner_id="owner", local=LocalBackendSettings(tmp_path / "db.sqlite3", authority_path)), environment={"ELYSIUM_LIFE_STORAGE_FENCING_TOKEN": token.fencing_token})
    await ensure_runtime_state_schema(runtime)
    return runtime


@pytest.mark.asyncio
async def test_outbox_unknown_is_not_claimable_and_identity_is_idempotent(tmp_path: Path) -> None:
    runtime = await _runtime(tmp_path)
    try:
        store = SQLOutboxStore(runtime)
        action = OutboxAction("a1", "key1", "event1", "stream1", "feishu:chat", "payload:1", "c" * 64, OutboxStatus.PENDING, None, 0, None, None, None, 0, None, "2026-08-07T00:00:00+00:00", "2026-08-07T00:00:00+00:00")
        assert await store.create_action(action) == await store.create_action(action)
        claimed = await store.claim_action("a1", owner_id="node-a", lease_seconds=30)
        assert claimed is not None
        await store.mark_unknown("a1", owner_id="node-a", claim_epoch=claimed.claim_epoch, error_type="timeout")
        assert await store.claim_action("a1", owner_id="node-b", lease_seconds=30) is None
        with pytest.raises(OutboxConflict):
            await store.create_action(action.__class__("a1", "key1", "event2", "stream1", "feishu:chat", "payload:1", "d" * 64, OutboxStatus.PENDING, None, 0, None, None, None, 0, None, action.created_at, action.updated_at))
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_outbox_stale_owner_is_fenced(tmp_path: Path) -> None:
    runtime = await _runtime(tmp_path)
    try:
        store = SQLOutboxStore(runtime)
        action = OutboxAction("a1", "key1", "event1", "stream1", "target", "ref", "c" * 64, OutboxStatus.PENDING, None, 0, None, None, None, 0, None, "2026-08-07T00:00:00+00:00", "2026-08-07T00:00:00+00:00")
        await store.create_action(action)
        claimed = await store.claim_action("a1", owner_id="node-a", lease_seconds=30)
        assert claimed is not None
        with pytest.raises(OutboxClaimLost):
            await store.mark_sent("a1", owner_id="node-b", claim_epoch=claimed.claim_epoch, provider_receipt_id="receipt")
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_projection_progress_is_per_node_and_contiguous(tmp_path: Path) -> None:
    runtime = await _runtime(tmp_path)
    try:
        store = SQLProjectionProgressStore(runtime)
        first = await store.advance(projection_name="memory", projection_node_id="node-a", expected_frontier=0, next_frontier=1, source_digest="a" * 64, config_digest="cfg")
        assert first.source_frontier == 1
        assert await store.get("memory", "node-b") is None
        with pytest.raises(ProjectionProgressConflict):
            await store.advance(projection_name="memory", projection_node_id="node-a", expected_frontier=1, next_frontier=3, source_digest="b" * 64, config_digest="cfg")
        with pytest.raises(ProjectionProgressConflict):
            await store.advance(projection_name="memory", projection_node_id="node-a", expected_frontier=1, next_frontier=2, source_digest="b" * 64, config_digest="other")
    finally:
        await runtime.close()
