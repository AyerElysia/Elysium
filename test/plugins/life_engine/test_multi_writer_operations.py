from __future__ import annotations

from pathlib import Path

import pytest

from plugins.life_engine.storage.contracts import StorageBackendRuntime
from plugins.life_engine.storage.factory import (
    LocalBackendSettings,
    StorageFactorySettings,
    open_storage_backend,
)
from plugins.life_engine.storage.authority import FileAuthorityRegistry
from plugins.life_engine.storage.models import BackendGeneration, GenerationStatus
from plugins.life_engine.storage.models import BackendKind
from plugins.life_engine.storage.operation_adapters import SQLOperationStore
from plugins.life_engine.storage.operation_contracts import (
    OperationClaimLost,
    OperationConflict,
    RuntimeDelta,
)
from plugins.life_engine.storage.runtime_schema import ensure_runtime_state_schema


async def _runtime(tmp_path: Path) -> StorageBackendRuntime:
    authority_path = tmp_path / "authority.json"
    registry = FileAuthorityRegistry(authority_path, registry_id="test")
    await registry.register_generation(
        BackendGeneration(
            generation_id="local-test",
            backend=BackendKind.LOCAL,
            schema_version=1,
            source_snapshot_sha256="a" * 64,
            root_hashes={"runtime": "b" * 64},
            frontiers={"runtime": 0},
            created_at="2026-08-07T00:00:00+00:00",
            verified_at="2026-08-07T00:00:00+00:00",
            status=GenerationStatus.VERIFIED,
        )
    )
    token = await registry.activate_generation(
        "local-test",
        expected_epoch=0,
        owner_id="test-owner",
        lease_seconds=120,
        confirm_previous_writers_stopped=True,
    )
    runtime = await open_storage_backend(
        StorageFactorySettings(
            enabled=True,
            authoritative_backend=BackendKind.LOCAL,
            backend_generation="local-test",
            authority_provider="file",
            registry_id="test",
            authority_epoch=token.authority_epoch,
            authority_owner_id="test-owner",
            local=LocalBackendSettings(
                database_path=tmp_path / "shared.sqlite3",
                authority_state_path=authority_path,
            ),
        ),
        environment={"ELYSIUM_LIFE_STORAGE_FENCING_TOKEN": token.fencing_token},
    )
    await ensure_runtime_state_schema(runtime)
    return runtime


@pytest.mark.asyncio
async def test_operation_claim_and_idempotent_delta_commit(tmp_path: Path) -> None:
    runtime = await _runtime(tmp_path)
    try:
        store = SQLOperationStore(runtime)
        await store.register_operation(
            operation_id="op-1",
            operation_type="stream_turn",
            scope_key="stream-a",
            sequence=1,
        )
        claimed = await store.claim_operation("op-1", owner_id="instance-a", lease_seconds=30)
        assert claimed is not None
        delta = RuntimeDelta(
            operation_id="op-1",
            namespace="life_engine.pending:stream-a",
            state_key="checkpoint",
            delta_type="append_pending_message",
            schema_version=1,
            payload={"identity": "message-1", "text": "payload"},
            actor="instance-a",
            source="test",
            causation_id="message-1",
            created_at="2026-08-07T12:00:00+00:00",
        )
        receipt = await store.commit_runtime_delta(
            delta,
            owner_id="instance-a",
            claim_epoch=claimed.claim_epoch,
            result_ref="result-1",
            result_sha256="a" * 64,
        )
        replay = await store.commit_runtime_delta(
            delta,
            owner_id="instance-a",
            claim_epoch=claimed.claim_epoch,
            result_ref="result-1",
            result_sha256="a" * 64,
        )
        assert replay == receipt
        assert (await store.claim_operation("op-1", owner_id="instance-b", lease_seconds=30)) is None
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_same_operation_identity_conflict_is_explicit(tmp_path: Path) -> None:
    runtime = await _runtime(tmp_path)
    try:
        store = SQLOperationStore(runtime)
        await store.register_operation(operation_id="op-1", operation_type="stream_turn", scope_key="s", sequence=1)
        with pytest.raises(OperationConflict):
            await store.register_operation(operation_id="op-1", operation_type="stream_turn", scope_key="s", sequence=1, input_frontier={"different": True})
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_stale_owner_cannot_commit_after_claim_epoch_changes(tmp_path: Path) -> None:
    runtime = await _runtime(tmp_path)
    try:
        store = SQLOperationStore(runtime)
        await store.register_operation(operation_id="op-1", operation_type="heartbeat", scope_key="chat_global", sequence=1)
        first = await store.claim_operation("op-1", owner_id="instance-a", lease_seconds=1)
        assert first is not None
        with pytest.raises(OperationClaimLost):
            await store.commit_runtime_delta(
                RuntimeDelta("op-1", "life_engine.heartbeat:chat_global", "checkpoint", "set_technical_projection", 1, {"value": 1}, "instance-a", "test", "cause", "2026-08-07T12:00:00+00:00"),
                owner_id="instance-b",
                claim_epoch=first.claim_epoch,
                result_ref="result",
                result_sha256="b" * 64,
            )
    finally:
        await runtime.close()
