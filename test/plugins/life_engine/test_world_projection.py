"""Contract tests for event-sourced world projection and perception delivery."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from plugins.life_engine.core.config import LifeEngineConfig
from plugins.life_engine.service.consciousness import (
    ConsciousnessInstance,
    ConsciousnessRegistry,
)
from plugins.life_engine.service.core import LifeEngineService
from plugins.life_engine.service.event_bus import (
    LifeEvent,
    LifeEventChannel,
    RawEventStore,
)
from plugins.life_engine.service.perception_gateway import (
    PerceptionDeliveryReceipt,
    PerceptionDeliveryUnverified,
    PerceptionGateway,
    PreparedPerception,
)
from plugins.life_engine.service.tool_manifests import get_tool_manifest
from plugins.life_engine.service.world_projection import (
    WORLD_ASSERTION_ORDER_NEWEST_FIRST,
    WORLD_ASSERTION_SCOPE_CURRENT_SNAPSHOT,
    WORLD_OBSERVATION_EVENT,
    WORLD_PROJECTOR_POLICY,
    WORLD_PROJECTOR_SCHEMA_VERSION,
    PerceptionCursorConflict,
    PromptProjectionPersistenceError,
    PromptProjectionValue,
    WorldAssertionReference,
    WorldAssertionReferencePage,
    WorldChangeReferencePage,
    WorldProjectionConflict,
    WorldProjectionStore,
    WorldProjectionUnavailable,
)
from plugins.life_engine.service.world_state import (
    RelationshipState,
    WorldState,
    WorldStateMigrationError,
)


@dataclass
class _Plugin:
    """Minimal plugin wrapper for an isolated LifeEngine service."""

    config: object


def _service(tmp_path: Path) -> LifeEngineService:
    """Create one service whose complete durable runtime lives under tmp_path."""

    config = LifeEngineConfig()
    config.settings.enabled = True
    config.settings.workspace_path = str(tmp_path)
    return LifeEngineService(_Plugin(config))


def _exact_receipt(prepared: PreparedPerception) -> PerceptionDeliveryReceipt:
    """Build the content-free receipt an exact final provider attempt emits."""

    return PerceptionDeliveryReceipt(
        delivery_id=prepared.delivery_id,
        projection_sha256=prepared.projection_sha256,
        delivered_bytes=prepared.delivered_bytes,
        exact=True,
    )


def _observation(
    identity: str,
    *,
    source_instance_id: str,
    value: object,
    retracts: str = "",
    domain: str = "relationship",
    predicate: str = "current_state",
    status: str = "observed",
    observed_at: str = "2026-08-02T12:00:00+08:00",
) -> LifeEvent:
    """Build one immutable observation event for store-level tests."""

    assertion = {
        "assertion_id": identity,
        "subject": "person:ayer",
        "predicate": predicate,
        "value": value,
        "domain": domain,
        "status": status,
        "source_instance_id": "untrusted-content-spoof",
        "observed_at": observed_at,
        "valid_from": observed_at,
        "retracts_assertion_id": retracts,
    }
    return LifeEvent(
        event_id=f"event-{identity}",
        sequence=0,
        timestamp=observed_at,
        source="test.world",
        channel=LifeEventChannel.LIFE.value,
        event_type=WORLD_OBSERVATION_EVENT,
        content=json.dumps({"assertion": assertion}, ensure_ascii=False),
        stream_id="stream-test",
        occurrence_id=f"occurrence-{identity}",
        source_instance_id=source_instance_id,
    )


def test_projection_retains_contradictions_provenance_and_explicit_retraction(
    tmp_path: Path,
) -> None:
    """Conflicting testimony coexists and only explicit evidence retracts it."""

    ledger = RawEventStore(tmp_path)
    ledger.append_sync(
        _observation("a", source_instance_id="chat_global", value="calm")
    )
    ledger.append_sync(_observation("b", source_instance_id="voice_1", value="worried"))
    ledger.append_sync(
        _observation(
            "c",
            source_instance_id="chat_global",
            value="I no longer hold assertion a",
            retracts="a",
        )
    )
    projection = WorldProjectionStore(tmp_path / "projection.sqlite3")

    frontier = projection.catch_up(ledger)
    assertions = projection.list_assertions()

    assert frontier == 3
    assert [item.assertion_id for item in assertions] == ["a", "b", "c"]
    assert [item.value for item in assertions[:2]] == ["calm", "worried"]
    assert assertions[0].retracted_by_assertion_id == "c"
    assert assertions[1].retracted_at == ""
    assert assertions[0].source_instance_id == "chat_global"
    assert assertions[1].source_instance_id == "voice_1"
    assert assertions[0].source_event_id == "event-a"


def test_projection_rebuild_is_equivalent_and_idempotent(tmp_path: Path) -> None:
    """Deleting derived state and replaying produces the same canonical view."""

    ledger = RawEventStore(tmp_path)
    first = _observation(
        "stable",
        source_instance_id="chat_global",
        value={"complete": "content"},
    )
    ledger.append_sync(first)
    ledger.append_sync(first)
    original = WorldProjectionStore(tmp_path / "original.sqlite3")
    original.catch_up(ledger)
    expected = original.canonical_snapshot()

    rebuilt = WorldProjectionStore(tmp_path / "rebuilt.sqlite3")
    rebuilt.rebuild(ledger)

    assert rebuilt.canonical_snapshot() == expected
    assert len(rebuilt.list_assertions()) == 1


def test_projection_rebuild_preserves_delivery_cursor_and_contract(
    tmp_path: Path,
) -> None:
    """A derived rebuild must not erase independently committed perception."""

    ledger = RawEventStore(tmp_path)
    ledger.append_sync(
        _observation("cursor-stable", source_instance_id="chat_global", value="seen")
    )
    projection = WorldProjectionStore(tmp_path / "projection.sqlite3")
    frontier = projection.catch_up(ledger)
    committed = projection.commit_perception_cursor(
        "voice_1",
        expected_position=0,
        expected_revision=0,
        through_position=frontier,
    )

    assert projection.rebuild(ledger) == frontier
    assert projection.perception_cursor("voice_1") == committed
    assert projection.projector_contract() == {
        "policy": WORLD_PROJECTOR_POLICY,
        "schema_version": WORLD_PROJECTOR_SCHEMA_VERSION,
        "rebuild_state": "idle",
    }


def test_projection_rejects_same_position_with_different_evidence(
    tmp_path: Path,
) -> None:
    """Idempotency accepts exact replay but never masks ledger corruption."""

    ledger = RawEventStore(tmp_path)
    stored = ledger.append_sync(
        _observation("position-a", source_instance_id="chat_global", value="first")
    )
    projection = WorldProjectionStore(tmp_path / "projection.sqlite3")

    assert projection.apply_events([stored]) == stored.sequence
    assert projection.apply_events([stored]) == stored.sequence
    conflicting = replace(
        stored,
        source_instance_id="voice_position_conflict",
    )
    with pytest.raises(WorldProjectionConflict, match="ingest position"):
        projection.apply_events([conflicting])


def test_perception_cursor_uses_position_revision_cas_and_stable_noop(
    tmp_path: Path,
) -> None:
    """No-op delivery stays idempotent while stale revisions fail closed."""

    ledger = RawEventStore(tmp_path)
    ledger.append_sync(
        _observation("cursor-cas", source_instance_id="chat_global", value="new")
    )
    projection = WorldProjectionStore(tmp_path / "projection.sqlite3")
    frontier = projection.catch_up(ledger)
    position, revision = projection.commit_perception_cursor(
        "voice_1",
        expected_position=0,
        expected_revision=0,
        through_position=frontier,
    )

    assert projection.commit_perception_cursor(
        "voice_1",
        expected_position=position,
        expected_revision=revision,
        through_position=position,
    ) == (position, revision)
    with pytest.raises(PerceptionCursorConflict):
        projection.commit_perception_cursor(
            "voice_1",
            expected_position=position,
            expected_revision=revision - 1,
            through_position=position,
        )
    with pytest.raises(ValueError, match="move backwards"):
        projection.commit_perception_cursor(
            "voice_1",
            expected_position=position,
            expected_revision=revision,
            through_position=position - 1,
        )


def test_failed_rebuild_is_persisted_and_blocks_delivery(tmp_path: Path) -> None:
    """An interrupted replay remains diagnosable and never serves partial truth."""

    ledger = RawEventStore(tmp_path)
    ledger.append_sync(
        _observation("rebuild-failure", source_instance_id="chat_global", value="old")
    )
    projection = WorldProjectionStore(tmp_path / "projection.sqlite3")
    frontier = projection.catch_up(ledger)
    cursor = projection.commit_perception_cursor(
        "voice_1",
        expected_position=0,
        expected_revision=0,
        through_position=frontier,
    )

    class _FailingLedger:
        @staticmethod
        def read_since_sync(_position: int, *, limit: int) -> list[LifeEvent]:
            del limit
            raise RuntimeError("contract replay failure")

    with pytest.raises(RuntimeError, match="contract replay failure"):
        projection.rebuild(_FailingLedger())  # type: ignore[arg-type]

    assert projection.projector_contract()["rebuild_state"] == "failed"
    assert projection.perception_cursor("voice_1") == cursor
    with pytest.raises(WorldProjectionUnavailable):
        projection.ensure_deliverable()


def test_gateway_cursor_is_commit_after_delivery_and_survives_restart(
    tmp_path: Path,
) -> None:
    """Prepare is retryable; commit is CAS-protected and durable per instance."""

    ledger = RawEventStore(tmp_path)
    registry = ConsciousnessRegistry()
    voice = registry.register(
        ConsciousnessInstance(
            instance_id="voice_1",
            kind="voice_live",
            stream_ids=["voice-stream"],
        )
    )
    registry.flush_lifecycle_events(ledger.append_sync)
    ledger.append_sync(
        _observation("world-a", source_instance_id="chat_global", value="known")
    )
    projection_path = tmp_path / "projection.sqlite3"
    gateway = PerceptionGateway(
        registry,
        ledger,
        WorldProjectionStore(projection_path),
    )

    prepared = gateway.prepare(voice.instance_id)
    retry = gateway.prepare(voice.instance_id)

    assert "chat_global" in prepared.content
    assert "voice_1" in prepared.content
    assert "world-a" in prepared.content
    assert retry.delivery_id == prepared.delivery_id
    assert retry.projection_sha256 == prepared.projection_sha256
    assert retry.content == prepared.content
    assert gateway.projection.perception_cursor("voice_1") == (0, 0)
    with pytest.raises(PerceptionDeliveryUnverified):
        gateway.commit(prepared)
    assert gateway.projection.perception_cursor("voice_1") == (0, 0)
    checkpoint = prepared.commit_checkpoint()
    assert not hasattr(checkpoint, "content")
    committed_position, committed_revision = gateway.commit_delivery(
        checkpoint,
        _exact_receipt(prepared),
    )
    assert committed_position == prepared.through_position
    assert committed_revision == 1
    assert gateway.commit(prepared, _exact_receipt(prepared)) == (
        committed_position,
        committed_revision,
    )

    restarted = PerceptionGateway(
        registry,
        ledger,
        WorldProjectionStore(projection_path),
    )
    after_restart = restarted.prepare("voice_1")
    assert after_restart.from_position == prepared.through_position
    assert after_restart.change_positions == ()

    ledger.append_sync(
        _observation("world-b", source_instance_id="chat_global", value="new")
    )
    delta = restarted.prepare("voice_1")
    assert delta.from_position == prepared.through_position
    assert delta.change_positions
    assert "world-b" in delta.content


@pytest.mark.asyncio
async def test_service_end_to_end_syncs_instances_and_preserves_full_content(
    tmp_path: Path,
) -> None:
    """Presence, report, projection, delivery, and restart form one closed loop."""

    service = _service(tmp_path)
    voice = service.consciousness_registry.register(
        ConsciousnessInstance(
            instance_id="voice_episode",
            kind="voice_live",
            stream_ids=["voice-stream"],
            session_id="episode",
        )
    )
    service.save_consciousness_registry()
    full_report = "完整观察-" + ("没有被代码截断。" * 200)
    receipt = await service.report_world_observation(
        full_report,
        source_instance_id=voice.instance_id,
        subject="voice-stream",
        predicate="session_state",
        domain="voice_live",
        stream_id="voice-stream",
    )

    chat_view = await service.prepare_perception("chat_global")
    voice_view = await service.prepare_perception(voice.instance_id)

    assert voice.instance_id in chat_view.content
    assert "chat_global" in voice_view.content
    assert full_report not in chat_view.content
    assert f"assertion:{receipt['assertion_id']}" not in chat_view.content
    history_page = await service.list_world_assertion_references_page(
        include_retracted=True,
        inline_max_bytes=0,
    )
    assert receipt["assertion_id"] in {
        item.assertion_id for item in history_page.items
    }
    assertion = service.world_projection.list_assertions()[-1]
    assert assertion.value == full_report
    assert assertion.source_instance_id == voice.instance_id
    assert assertion.assertion_id == receipt["assertion_id"]
    chunk = await service.read_world_assertion_value_chunk(
        receipt["assertion_id"],
        max_bytes=16 * 1024,
    )
    assert chunk.complete is True
    assert json.loads(chunk.content) == full_report
    await service.commit_perception(voice_view, _exact_receipt(voice_view))

    restarted = _service(tmp_path)
    restarted_view = await restarted.prepare_perception(voice.instance_id)
    assert restarted_view.from_position == voice_view.through_position
    assert full_report not in restarted_view.content
    assert f"assertion:{receipt['assertion_id']}" not in restarted_view.content


def test_current_snapshot_excludes_history_and_lifecycle_without_data_loss(
    tmp_path: Path,
) -> None:
    """Present-tense delivery cannot be starved by retained lifecycle evidence."""

    ledger = RawEventStore(tmp_path)
    for index in range(21):
        ledger.append_sync(
            _observation(
                f"legacy-{index:03d}",
                source_instance_id="legacy-import",
                value=f"legacy-payload-{index:03d}",
                domain="scene",
                predicate="legacy_snapshot",
                status="legacy_import",
                observed_at=f"2026-08-02T10:{index:02d}:00+00:00",
            )
        )
    for index in range(100):
        ledger.append_sync(
            _observation(
                f"lifecycle-{index:03d}",
                source_instance_id="voice-instance",
                value=f"session-payload-{index:03d}",
                domain="voice_live",
                predicate="session_state",
                observed_at=(
                    f"2026-08-03T10:{index // 60:02d}:{index % 60:02d}+00:00"
                ),
            )
        )
    ledger.append_sync(
        _observation(
            "stale-current",
            source_instance_id="chat_global",
            value="stale-current-payload",
            observed_at="2026-08-05T10:00:00+00:00",
        )
    )
    ledger.append_sync(
        _observation(
            "retract-stale",
            source_instance_id="chat_global",
            value="stale evidence withdrawn",
            retracts="stale-current",
            observed_at="2026-08-05T10:01:00+00:00",
        )
    )
    ledger.append_sync(
        _observation(
            "current-fact",
            source_instance_id="chat_global",
            value="current-visible-fact",
            observed_at="2026-08-06T10:00:00+00:00",
        )
    )

    registry = ConsciousnessRegistry()
    registry.register(
        ConsciousnessInstance(
            instance_id="voice-suspended",
            kind="voice_live",
            stream_ids=["voice:suspended"],
        )
    )
    assert registry.suspend("voice-suspended") is True
    projection_path = tmp_path / "current-view.sqlite3"
    store = WorldProjectionStore(projection_path)
    gateway = PerceptionGateway(registry, ledger, store)
    first = gateway.prepare("chat_global")

    assert "suspended 窗口摘要: total=1" in first.content
    assert '"voice_live":1' in first.content
    assert "current-fact" in first.assertion_ids
    assert "stale-current" not in first.assertion_ids
    assert not any(identity.startswith("legacy-") for identity in first.assertion_ids)
    assert not any(
        identity.startswith("lifecycle-") for identity in first.assertion_ids
    )
    snapshot_text = first.content.split("### 自上次成功感知以来的有界变化", 1)[0]
    assert "current-visible-fact" in snapshot_text
    assert "legacy-payload" not in snapshot_text
    assert "session-payload" not in snapshot_text

    history = store.list_assertion_references_page(
        include_retracted=True,
        limit=1000,
    )
    assert history.total_items == 124
    assert {item.assertion_id for item in history.items} >= {
        "legacy-000",
        "lifecycle-099",
        "stale-current",
        "current-fact",
    }
    current = store.list_assertion_references_page(
        delivery_scope=WORLD_ASSERTION_SCOPE_CURRENT_SNAPSHOT,
        limit=1,
    )
    assert current.result_order == WORLD_ASSERTION_ORDER_NEWEST_FIRST
    assert current.items[0].assertion_id == "current-fact"
    assert current.next_after_assertion_id == "current-fact"
    older_current = store.list_assertion_references_page(
        delivery_scope=WORLD_ASSERTION_SCOPE_CURRENT_SNAPSHOT,
        after_observed_at=current.next_after_observed_at,
        after_assertion_id=current.next_after_assertion_id,
        limit=10,
    )
    assert [item.assertion_id for item in older_current.items] == ["retract-stale"]

    pending = first
    delivered_change_positions: set[int] = set()
    while pending.change_positions:
        assert delivered_change_positions.isdisjoint(pending.change_positions)
        delivered_change_positions.update(pending.change_positions)
        gateway.commit(pending, _exact_receipt(pending))
        pending = gateway.prepare("chat_global")
    settled = pending
    assert len(delivered_change_positions) == 124
    assert "legacy-payload" not in settled.content
    assert "session-payload" not in settled.content

    ledger.append_sync(
        _observation(
            "lifecycle-new",
            source_instance_id="voice-instance",
            value="one-time-lifecycle-change",
            domain="voice_live",
            predicate="session_state",
            observed_at="2026-08-06T11:00:00+00:00",
        )
    )
    delta = gateway.prepare("chat_global")
    assert "lifecycle-new" not in delta.assertion_ids
    assert "one-time-lifecycle-change" in delta.content
    gateway.commit(delta, _exact_receipt(delta))
    assert "one-time-lifecycle-change" not in gateway.prepare("chat_global").content

    restarted_store = WorldProjectionStore(projection_path)
    restarted_current = restarted_store.list_assertion_references_page(
        delivery_scope=WORLD_ASSERTION_SCOPE_CURRENT_SNAPSHOT,
        limit=1000,
    )
    before_rebuild_ids = tuple(item.assertion_id for item in restarted_current.items)
    full_count = len(restarted_store.list_assertions(include_retracted=True))
    restarted_store.rebuild(ledger)
    rebuilt_current = restarted_store.list_assertion_references_page(
        delivery_scope=WORLD_ASSERTION_SCOPE_CURRENT_SNAPSHOT,
        limit=1000,
    )
    assert tuple(item.assertion_id for item in rebuilt_current.items) == before_rebuild_ids
    assert len(restarted_store.list_assertions(include_retracted=True)) == full_count


@pytest.mark.asyncio
async def test_chatter_world_cursor_commits_only_after_model_success_hook(
    tmp_path: Path,
) -> None:
    """Prompt assembly alone does not consume a chatter instance's world delta."""

    service = _service(tmp_path)
    stream = SimpleNamespace(stream_id="chat-stream")

    context, high_water = await service.build_chatter_runtime_context(
        stream,
        unified_chatter_context=True,
        include_recent_chat_history=False,
    )
    prepared = service._pending_chatter_perceptions[
        service._chatter_cursor_key(
            "chat-stream",
            unified_chatter_context=True,
        )
    ]

    assert "chat_global" in context
    assert len(context.encode("utf-8")) <= 60 * 1024
    assert service.world_projection.perception_cursor("chat_global") == (0, 0)
    with pytest.raises(PerceptionDeliveryUnverified):
        await service.mark_chatter_runtime_context_seen(
            "chat-stream",
            high_water,
            unified_chatter_context=True,
        )
    assert service.has_pending_chatter_perception(
        "chat-stream",
        unified_chatter_context=True,
    )
    assert service.world_projection.perception_cursor("chat_global") == (0, 0)
    await service.mark_chatter_runtime_context_seen(
        "chat-stream",
        high_water,
        unified_chatter_context=True,
        receipt=_exact_receipt(prepared),
    )
    assert service.world_projection.perception_cursor("chat_global")[0] == (
        prepared.through_position
    )
    assert not service.has_pending_chatter_perception(
        "chat-stream",
        unified_chatter_context=True,
    )


def test_giant_world_value_is_referenced_and_utf8_chunked_without_loss(
    tmp_path: Path,
) -> None:
    """A 1.6MB+ value stays durable while the prompt remains below 32 KiB."""

    ledger = RawEventStore(tmp_path)
    registry = ConsciousnessRegistry()
    registry.register(
        ConsciousnessInstance(
            instance_id="observer",
            kind="contract",
            stream_ids=["stream:observer"],
        )
    )
    giant_value = {"transcript": "爱莉希雅" * 150_000}
    ledger.append_sync(
        _observation(
            "giant-value",
            source_instance_id="observer",
            value=giant_value,
        )
    )
    gateway = PerceptionGateway(
        registry,
        ledger,
        WorldProjectionStore(tmp_path / "giant.sqlite3"),
    )

    prepared = gateway.prepare(
        "observer",
        projection_kind="life_chatter",
        max_bytes=32 * 1024,
    )

    assert prepared.delivered_bytes == len(prepared.content.encode("utf-8"))
    assert prepared.delivered_bytes <= 32 * 1024
    assert prepared.source_payload_bytes > 1_600_000
    assert "assertion:giant-value" in prepared.content
    assert "爱莉希雅" * 100 not in prepared.content
    assert (
        prepared.projection_sha256
        == hashlib.sha256(prepared.content.encode("utf-8")).hexdigest()
    )
    with pytest.raises(PerceptionDeliveryUnverified):
        gateway.commit(
            prepared,
            replace(_exact_receipt(prepared), exact=False),
        )
    assert gateway.projection.perception_cursor("observer") == (0, 0)

    chunks: list[str] = []
    offset = 0
    while True:
        chunk = gateway.projection.read_assertion_value_chunk(
            "giant-value",
            offset_bytes=offset,
            max_bytes=64 * 1024,
        )
        assert len(chunk.content.encode("utf-8")) <= 64 * 1024
        chunks.append(chunk.content)
        if chunk.complete:
            break
        assert chunk.next_offset_bytes > offset
        offset = chunk.next_offset_bytes
    assert json.loads("".join(chunks)) == giant_value


@pytest.mark.asyncio
async def test_prompt_projection_and_known_transport_echo_fail_closed(
    tmp_path: Path,
) -> None:
    """New recursive prompt echoes are rejected while historical evidence remains."""

    service = _service(tmp_path)
    observer = service.consciousness_registry.register(
        ConsciousnessInstance(
            instance_id="minecraft:observer",
            kind="minecraft",
            stream_ids=["minecraft:stream"],
        )
    )
    echo = {
        "trace_kind": "intent.issued",
        "payload": {
            "context": {"transient_world_perception": "recursive-prompt-only-value"}
        },
    }
    with pytest.raises(PromptProjectionPersistenceError):
        await service.report_world_observation(
            "typed projection",
            source_instance_id=observer.instance_id,
            subject="minecraft:trace",
            predicate="state",
            domain="minecraft",
            value=PromptProjectionValue(
                delivery_id="delivery",
                projection_sha256="a" * 64,
                content="prompt only",
            ),
        )
    with pytest.raises(PromptProjectionPersistenceError):
        await service.report_world_observation(
            "trace",
            source_instance_id=observer.instance_id,
            subject="minecraft:trace",
            predicate="embodied_trace",
            domain="minecraft",
            value=echo,
        )
    assert service.world_projection.list_assertions() == []

    ledger = RawEventStore(tmp_path / "historical")
    ledger.append_sync(
        _observation(
            "historical-echo",
            source_instance_id=observer.instance_id,
            value=echo,
            domain="minecraft",
            predicate="embodied_trace",
        )
    )
    gateway = PerceptionGateway(
        service.consciousness_registry,
        ledger,
        WorldProjectionStore(tmp_path / "historical.sqlite3"),
    )
    prepared = gateway.prepare(observer.instance_id)
    assert "transport_echo=quarantined" in prepared.content
    assert "recursive-prompt-only-value" not in prepared.content
    assert gateway.projection.list_assertions()[0].value == echo


@pytest.mark.asyncio
async def test_stable_world_observation_identity_is_idempotent_and_conflicting(
    tmp_path: Path,
) -> None:
    """A retry after append-before-project failure cannot duplicate an experience."""

    service = _service(tmp_path)
    observer = service.consciousness_registry.register(
        ConsciousnessInstance(
            instance_id="minecraft:retry",
            kind="minecraft",
            stream_ids=["minecraft:retry"],
        )
    )
    kwargs = {
        "source_instance_id": observer.instance_id,
        "subject": "minecraft:trace",
        "predicate": "embodied_trace_ref",
        "domain": "minecraft",
        "observed_at": "2026-08-05T12:00:00+08:00",
        "occurrence_id": "minecraft-trace:projection-123",
        "assertion_id": "minecraft-assertion:projection-123",
        "value": {"projection_id": "projection-123"},
    }

    first = await service.report_world_observation("trace ref", **kwargs)
    replay = await service.report_world_observation("trace ref", **kwargs)

    assert replay == first
    assertions = service.world_projection.list_assertions()
    assert [item.assertion_id for item in assertions] == [
        "minecraft-assertion:projection-123"
    ]
    with pytest.raises(ValueError, match="OccurrenceConflict"):
        await service.report_world_observation("different trace ref", **kwargs)
    with pytest.raises(ValueError, match="explicit observed_at"):
        await service.report_world_observation(
            "unstable retry",
            source_instance_id=observer.instance_id,
            subject="minecraft:trace",
            occurrence_id="minecraft-trace:missing-time",
        )


def test_hundred_thousand_assertion_frontier_is_bounded_and_continuable(
    tmp_path: Path,
) -> None:
    """Large-cardinality metadata cannot force an unbounded prompt projection."""

    registry = ConsciousnessRegistry()
    registry.register(
        ConsciousnessInstance(
            instance_id="observer",
            kind="contract",
            stream_ids=["stream:observer"],
        )
    )
    gateway = PerceptionGateway(
        registry,
        RawEventStore(tmp_path),
        WorldProjectionStore(tmp_path / "cardinality.sqlite3"),
    )
    items = tuple(
        WorldAssertionReference(
            assertion_id=f"assertion-{index:06d}",
            subject="subject:self",
            predicate="state",
            domain="contract",
            status="observed",
            source_instance_id="observer",
            source_event_id=f"event-{index:06d}",
            occurrence_id=f"occurrence-{index:06d}",
            observed_at=f"2026-08-05T00:{index // 60:02d}:{index % 60:02d}+00:00",
            valid_from="",
            valid_to="",
            recorded_at="2026-08-05T00:00:00+00:00",
            supersedes_assertion_id="",
            value_bytes=4096,
            value_inlined=False,
            value=None,
            transport_echo=False,
        )
        for index in range(256)
    )
    prepared = gateway._build_prepared(
        identity="observer",
        projection_kind="stress",
        max_bytes=32 * 1024,
        from_position=0,
        cursor_revision=0,
        source_frontier=100_000,
        assertion_page=WorldAssertionReferencePage(
            items=items,
            total_items=100_000,
            total_value_bytes=100_000 * 4096,
            next_after_observed_at=items[-1].observed_at,
            next_after_assertion_id=items[-1].assertion_id,
        ),
        change_page=WorldChangeReferencePage(
            items=(),
            total_items=0,
            total_payload_bytes=0,
            has_more=False,
        ),
    )

    assert prepared.delivered_bytes <= 32 * 1024
    assert prepared.omitted_assertion_count >= 99_744
    assert prepared.snapshot_continuation_token
    assert prepared.source_frontier == 100_000
    assert prepared.through_position == 100_000
    continuation = gateway.decode_snapshot_continuation_token(
        prepared.snapshot_continuation_token
    )
    assert continuation["projection_kind"] == "stress"
    assert continuation["source_frontier"] == 100_000
    assert continuation["after_assertion_id"] == prepared.assertion_ids[-1]


def test_legacy_world_snapshot_is_imported_once_and_source_is_preserved(
    tmp_path: Path,
) -> None:
    """Legacy JSON becomes ledger evidence exactly once and remains on disk."""

    legacy_path = tmp_path / "runtime" / "world_state.json"
    legacy = WorldState(
        relationships={
            "ayer": RelationshipState(
                entity_id="ayer",
                display_name="Ayer",
                status_summary="legacy-state",
            )
        },
        revision=7,
    )
    legacy.save(legacy_path)
    source_before = legacy_path.read_text(encoding="utf-8")

    first = _service(tmp_path)
    first_assertions = first.world_projection.list_assertions()
    first_event_count = first._get_event_bus().store.health_snapshot()["total"]
    second = _service(tmp_path)
    second_assertions = second.world_projection.list_assertions()
    second_event_count = second._get_event_bus().store.health_snapshot()["total"]

    assert len(first_assertions) == 1
    assert first_assertions[0].value["status_summary"] == "legacy-state"
    assert [item.to_dict() for item in second_assertions] == [
        item.to_dict() for item in first_assertions
    ]
    assert second_event_count == first_event_count
    assert legacy_path.read_text(encoding="utf-8") == source_before


def test_corrupt_legacy_world_snapshot_blocks_lossy_reset(tmp_path: Path) -> None:
    """Corrupt migration input fails explicitly instead of becoming empty truth."""

    legacy_path = tmp_path / "runtime" / "world_state.json"
    legacy_path.parent.mkdir(parents=True)
    legacy_path.write_text("{not-json", encoding="utf-8")

    with pytest.raises(WorldStateMigrationError):
        _service(tmp_path)
    assert legacy_path.read_text(encoding="utf-8") == "{not-json"


def test_unknown_consciousness_kind_has_no_chat_capability_fallback() -> None:
    """A new runtime kind must declare its powers instead of inheriting chat."""

    with pytest.raises(KeyError):
        get_tool_manifest("undeclared-consciousness-kind")
