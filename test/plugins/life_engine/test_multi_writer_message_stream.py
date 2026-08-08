from __future__ import annotations

from pathlib import Path

import pytest

from plugins.life_engine.storage.authority import FileAuthorityRegistry
from plugins.life_engine.storage.factory import LocalBackendSettings, StorageFactorySettings, open_storage_backend
from plugins.life_engine.storage.message_stream_adapters import SQLMessageStreamStore
from plugins.life_engine.storage.message_stream_contracts import InboundMessage, MessageConflict, StreamTurn, TurnClaimLost, TurnStatus
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


def _message(message_id: str, digest: str = "a" * 64) -> InboundMessage:
    return InboundMessage(message_id, "feishu", "event-1", "occ-1", digest, "stream-1", "chat-1", "adapter", "2026-08-07T00:00:00+00:00", "2026-08-07T00:00:01+00:00", "payload:1")


@pytest.mark.asyncio
async def test_message_occurrence_is_idempotent_and_conflict_is_explicit(tmp_path: Path) -> None:
    runtime = await _runtime(tmp_path)
    try:
        store = SQLMessageStreamStore(runtime)
        assert await store.record_message(_message("m1")) == _message("m1")
        assert await store.record_message(_message("m1")) == _message("m1")
        with pytest.raises(MessageConflict):
            await store.record_message(_message("m2", "b" * 64))
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_stream_turn_claim_commit_and_stale_owner_fencing(tmp_path: Path) -> None:
    runtime = await _runtime(tmp_path)
    try:
        store = SQLMessageStreamStore(runtime)
        turn = StreamTurn("t1", "stream-1", 1, "m1", TurnStatus.PENDING, None, 0, None, {}, None, None, 0, "2026-08-07T00:00:00+00:00", "2026-08-07T00:00:00+00:00")
        await store.create_turn(turn)
        claimed = await store.claim_turn("t1", owner_id="node-a", lease_seconds=30)
        assert claimed is not None
        with pytest.raises(TurnClaimLost):
            await store.commit_turn("t1", owner_id="node-b", claim_epoch=claimed.claim_epoch, result_ref="r", result_digest="c" * 64)
        completed = await store.commit_turn("t1", owner_id="node-a", claim_epoch=claimed.claim_epoch, result_ref="r", result_digest="c" * 64)
        assert completed.status == TurnStatus.COMPLETED
    finally:
        await runtime.close()
