from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from plugins.life_engine.storage.authority import FileAuthorityRegistry
from plugins.life_engine.storage.contracts import StorageBackendRuntime
from plugins.life_engine.storage.factory import (
    LocalBackendSettings,
    StorageFactorySettings,
    open_storage_backend,
)
from plugins.life_engine.storage.models import (
    BackendGeneration,
    BackendKind,
    GenerationStatus,
)
from plugins.life_engine.storage.multi_writer_health import (
    observe_multi_writer_health,
)
from plugins.life_engine.storage.operation_adapters import SQLOperationStore
from plugins.life_engine.storage.operation_contracts import RuntimeDelta
from plugins.life_engine.storage.outbox_adapters import SQLOutboxStore
from plugins.life_engine.storage.outbox_contracts import OutboxAction, OutboxStatus
from plugins.life_engine.storage.projection_progress import SQLProjectionProgressStore
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
            created_at="2026-08-08T00:00:00+00:00",
            verified_at="2026-08-08T00:00:00+00:00",
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
                database_path=tmp_path / "health.sqlite3",
                authority_state_path=authority_path,
            ),
        ),
        environment={"ELYSIUM_LIFE_STORAGE_FENCING_TOKEN": token.fencing_token},
    )
    await ensure_runtime_state_schema(runtime)
    return runtime


@pytest.mark.asyncio
async def test_health_ready_when_no_problems(tmp_path: Path) -> None:
    runtime = await _runtime(tmp_path)
    try:
        snapshot = await observe_multi_writer_health(
            runtime,
            local_owner="deploy-a:instance-1",
            generation_id="local-test",
            schema_version=1,
            protocol_version=1,
        )
        assert snapshot.status == "ready"
        assert snapshot.generation_id == "local-test"
        assert snapshot.missing_tables == []
        assert snapshot.expired_claim_count == 0
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_health_reports_unknown_outbox_as_degraded(tmp_path: Path) -> None:
    runtime = await _runtime(tmp_path)
    try:
        outbox = SQLOutboxStore(runtime)
        action = await outbox.create_action(
            OutboxAction(
                action_id="action-1",
                idempotency_key="key-1",
                source_event_id="evt-1",
                stream_id="stream-a",
                target="feishu:u:100",
                payload_ref="ref://1",
                payload_sha256="d" * 64,
                status=OutboxStatus.PENDING,
                claim_owner=None,
                claim_epoch=0,
                lease_until=None,
                provider_request_id=None,
                provider_receipt_id=None,
                attempts=0,
                last_error_type=None,
                created_at="2026-08-08T00:00:00+00:00",
                updated_at="2026-08-08T00:00:00+00:00",
            )
        )
        claimed = await outbox.claim_action(
            "action-1",
            owner_id="deploy-a:instance-1",
            lease_seconds=30,
        )
        assert claimed is not None
        await outbox.mark_unknown(
            "action-1",
            owner_id="deploy-a:instance-1",
            claim_epoch=claimed.claim_epoch,
            error_type="provider_timeout",
        )

        snapshot = await observe_multi_writer_health(
            runtime,
            local_owner="deploy-a:instance-1",
            protocol_version=1,
        )
        assert snapshot.status == "degraded"
        assert snapshot.outbox_counts.get("unknown", 0) == 1
        assert any("unknown outbox" in note for note in snapshot.notes)
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_health_is_content_free(tmp_path: Path) -> None:
    runtime = await _runtime(tmp_path)
    try:
        store = SQLOperationStore(runtime)
        await store.register_operation(
            operation_id="op-health",
            operation_type="stream_turn",
            scope_key="stream-a",
            sequence=1,
        )
        claimed = await store.claim_operation(
            "op-health",
            owner_id="deploy-a:instance-1",
            lease_seconds=30,
        )
        assert claimed is not None
        await store.commit_runtime_delta(
            RuntimeDelta(
                operation_id="op-health",
                namespace="life_engine.pending:stream-a",
                state_key="checkpoint",
                delta_type="append_pending_message",
                schema_version=1,
                payload={"identity": "secret-message-body", "text": "private"},
                actor="deploy-a:instance-1",
                source="test",
                causation_id="message-1",
                created_at="2026-08-08T00:00:00+00:00",
            ),
            owner_id="deploy-a:instance-1",
            claim_epoch=claimed.claim_epoch,
            result_ref="result-1",
            result_sha256="e" * 64,
        )

        snapshot = await observe_multi_writer_health(
            runtime,
            local_owner="deploy-a:instance-1",
            protocol_version=1,
        )
        rendered = repr(snapshot)
        assert "secret-message-body" not in rendered
        assert "private" not in rendered
        assert snapshot.operation_counts.get("stream_turn:completed", 0) == 1
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_health_not_ready_when_tables_absent(tmp_path: Path) -> None:
    runtime = await _runtime(tmp_path)
    try:
        # Drop the anchor table to simulate a partial/undeployed generation.
        async with runtime.session_factory() as session:
            from sqlalchemy import text

            await session.execute(text("DROP TABLE operations"))
        snapshot = await observe_multi_writer_health(
            runtime,
            protocol_version=1,
        )
        assert snapshot.status == "not_ready"
        assert "operations" in snapshot.missing_tables
        assert any("not deployed" in note for note in snapshot.notes)
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_health_disabled_for_inactive_runtime() -> None:
    runtime = SimpleNamespace(enabled=False, session_factory=None)
    snapshot = await observe_multi_writer_health(
        runtime,
        protocol_version=1,
    )
    assert snapshot.status == "disabled"


@pytest.mark.asyncio
async def test_health_reports_projection_and_expired_claims(tmp_path: Path) -> None:
    runtime = await _runtime(tmp_path)
    try:
        store = SQLOperationStore(runtime)
        await store.register_operation(
            operation_id="op-stale",
            operation_type="index_job",
            scope_key="job-1",
            sequence=1,
        )
        await store.claim_operation(
            "op-stale",
            owner_id="deploy-b:instance-9",
            lease_seconds=30,
        )
        # Force the claim to expire.
        async with runtime.session_factory() as session:
            from sqlalchemy import text

            await session.execute(
                text(
                    "UPDATE operations SET lease_until = "
                    "'2000-01-01T00:00:00+00:00' WHERE operation_id = 'op-stale'"
                )
            )
            await session.commit()

        projection = SQLProjectionProgressStore(runtime)
        await projection.advance(
            projection_name="memory_index",
            projection_node_id="node-a",
            expected_frontier=0,
            next_frontier=1,
            source_digest="s" * 64,
            config_digest="c" * 64,
            backlog=3,
        )

        snapshot = await observe_multi_writer_health(
            runtime,
            local_owner="deploy-a:instance-1",
            protocol_version=1,
        )
        assert snapshot.expired_claim_count >= 1
        assert snapshot.local_claim_count == 0
        assert any(
            item.projection_name == "memory_index"
            and item.projection_node_id == "node-a"
            and item.source_frontier == 1
            and item.backlog == 3
            for item in snapshot.projections
        )
    finally:
        await runtime.close()
