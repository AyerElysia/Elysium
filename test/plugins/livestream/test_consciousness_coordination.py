"""Livestream contracts for real presence and retry-safe world perception."""

from __future__ import annotations

import hashlib
from types import SimpleNamespace
from typing import Any

import pytest

from plugins.life_engine.service.consciousness import ConsciousnessRegistry
from plugins.life_engine.service.core import (
    ChatterRuntimeCommitCheckpoint,
    ChatterRuntimeDeliveryReceipt,
)
from plugins.life_engine.service.perception_gateway import PreparedPerception
from plugins.livestream.config import LivestreamConfig
from plugins.livestream.consciousness import LivestreamConsciousnessManager
from plugins.livestream.director import (
    DirectorProtocolError,
    LifeChatterDeliberator,
    LivestreamDirector,
)
from plugins.livestream.domain import (
    ChatterRuntimeCheckpoint,
    PerceptionCommitCheckpoint,
    PlatformEvent,
    WorldPerceptionCheckpoint,
)
from plugins.livestream.ledger import LivestreamLedger

pytestmark = pytest.mark.asyncio


class _Service:
    """Minimal supported LifeEngine integration used by the manager."""

    def __init__(self) -> None:
        self.consciousness_registry = ConsciousnessRegistry()
        self.observations: list[dict[str, Any]] = []
        self.commits: list[ChatterRuntimeCommitCheckpoint] = []
        self.current_position = 0
        self.lifecycle_calls: list[str] = []
        self.pending_delivery: Any | None = None
        self.committed_delivery_ids: set[str] = set()

    async def register_consciousness_instance(
        self,
        instance: Any,
    ) -> Any:
        self.lifecycle_calls.append("register")
        return self.consciousness_registry.register(instance)

    async def touch_consciousness_instance(
        self,
        instance_id: str,
        **kwargs: Any,
    ) -> None:
        self.lifecycle_calls.append("touch")
        self.consciousness_registry.touch(instance_id, **kwargs)

    async def resume_consciousness_instance(
        self,
        instance_id: str,
        **kwargs: Any,
    ) -> bool:
        self.lifecycle_calls.append("resume")
        return self.consciousness_registry.resume(instance_id, **kwargs)

    async def suspend_consciousness_instance(
        self,
        instance_id: str,
        **kwargs: Any,
    ) -> bool:
        self.lifecycle_calls.append("suspend")
        return self.consciousness_registry.suspend(instance_id, **kwargs)

    async def terminate_consciousness_instance(
        self,
        instance_id: str,
        **kwargs: Any,
    ) -> bool:
        self.lifecycle_calls.append("terminate")
        return self.consciousness_registry.terminate(instance_id, **kwargs)

    async def report_world_observation(
        self,
        report: str,
        **kwargs: Any,
    ) -> dict[str, str]:
        self.observations.append({"report": report, **kwargs})
        return {"assertion_id": f"a-{len(self.observations)}"}

    async def prepare_perception(self, instance_id: str) -> PreparedPerception:
        content = (
            '<world_perception_delivery marker="world-perception:world-delivery-1">'
            "other-instance-is-present"
            "</world_perception_delivery>"
        )
        return PreparedPerception(
            instance_id=instance_id,
            projection_kind="livestream",
            from_position=self.current_position,
            through_position=max(self.current_position, 5),
            source_frontier=max(self.current_position, 5),
            cursor_revision=len(self.commits),
            content=content,
            assertion_ids=("assertion-1",),
            change_positions=(5,) if self.current_position < 5 else (),
            delivery_id="world-delivery-1",
            projection_sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
            algorithm_version="world-perception-page-v2",
            delivered_bytes=len(content.encode("utf-8")),
            source_payload_bytes=len(content.encode("utf-8")),
            omitted_assertion_count=0,
            omitted_change_count=0,
            omitted_source_bytes=0,
            snapshot_continuation_token="",
            has_more_changes=False,
        )

    def get_pending_chatter_runtime_delivery(
        self,
        stream_id: str,
        *,
        unified_chatter_context: bool,
    ) -> Any | None:
        assert stream_id == "livestream:bilibili:42"
        assert unified_chatter_context is True
        return self.pending_delivery

    def create_chatter_runtime_commit_checkpoint(
        self,
        stream_id: str,
        *,
        delivery_id: str,
        effective_suffix_sha256: str,
        effective_suffix_bytes: int,
        unified_chatter_context: bool,
    ) -> ChatterRuntimeCommitCheckpoint:
        pending = self.get_pending_chatter_runtime_delivery(
            stream_id,
            unified_chatter_context=unified_chatter_context,
        )
        assert pending is not None
        assert delivery_id == pending.delivery_id
        return ChatterRuntimeCommitCheckpoint(
            cursor_key="__life_chatter_global__",
            delivery_id=delivery_id,
            effective_suffix_sha256=effective_suffix_sha256,
            effective_suffix_bytes=effective_suffix_bytes,
            event_through_sequence=5,
            thought_through_revision=2,
            perception=pending.prepared_perception.commit_checkpoint(),
        )

    async def commit_chatter_runtime_delivery(
        self,
        checkpoint: ChatterRuntimeCommitCheckpoint,
        receipt: ChatterRuntimeDeliveryReceipt,
    ) -> Any:
        assert receipt.exact
        assert receipt.delivery_id == checkpoint.delivery_id
        assert receipt.effective_suffix_sha256 == checkpoint.effective_suffix_sha256
        assert receipt.effective_suffix_bytes == checkpoint.effective_suffix_bytes
        if checkpoint.delivery_id not in self.committed_delivery_ids:
            assert checkpoint.perception.from_position == self.current_position
            self.current_position = checkpoint.perception.through_position
            self.commits.append(checkpoint)
            self.committed_delivery_ids.add(checkpoint.delivery_id)
        return SimpleNamespace(
            delivery_id=checkpoint.delivery_id,
            event_through_sequence=checkpoint.event_through_sequence,
            thought_through_revision=checkpoint.thought_through_revision,
            world_position=self.current_position,
            world_revision=len(self.commits),
        )


def _config() -> LivestreamConfig:
    return LivestreamConfig(platform={"room_id": "42"})


@pytest.mark.asyncio
async def test_livestream_uses_presence_lease_and_observation_events() -> None:
    service = _Service()
    manager = LivestreamConsciousnessManager(
        _config(),
        "session-1",
        service=service,
    )

    instance = await manager.activate()

    assert instance is service.consciousness_registry.get(manager.instance_id)
    assert instance.is_active
    assert instance.session_id == "session-1"
    assert instance.lease_duration_seconds == 300
    assert service.lifecycle_calls[:2] == ["register", "touch"]
    assert service.observations[-1]["source_instance_id"] == manager.instance_id
    assert service.observations[-1]["subject"] == manager.stream_id
    previous_revision = instance.revision
    await manager.renew()
    assert instance.revision > previous_revision

    await manager.suspend(reason="test-ended")
    assert instance.status == "suspended"
    assert "test-ended" in service.observations[-1]["report"]


async def test_active_room_rejects_another_session_then_allows_clean_handoff() -> None:
    service = _Service()
    first = LivestreamConsciousnessManager(
        _config(),
        "session-1",
        service=service,
    )
    second = LivestreamConsciousnessManager(
        _config(),
        "session-2",
        service=service,
    )
    await first.activate()

    with pytest.raises(RuntimeError, match="another active session"):
        await second.activate()

    await first.suspend(reason="handoff")
    instance = await second.activate()
    assert instance.is_active
    assert instance.session_id == "session-2"
    assert instance.stream_ids == [second.stream_id]
    await second.suspend(reason="test-ended")


class _Response:
    def __init__(self, request: _Request) -> None:
        self._request = request
        self.message = (
            '{"should_speak":false,"reason":"I choose to listen.",'
            '"addressed_event_ids":["event-1"]}'
        )
        self.request_record_id = 42

    def __await__(self):
        async def consume():
            return self.message

        return consume().__await__()

    def effective_context_receipt(self, delivery_id: str) -> Any | None:
        expected = self._request.deliveries.get(delivery_id)
        if expected is None or self._request.receipt_mode == "missing":
            return None
        text, _marker = expected
        expected_bytes = len(text.encode("utf-8"))
        expected_sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if self._request.receipt_mode == "duplicate":
            effective_bytes = None
            effective_sha256 = None
            exact = False
        elif self._request.receipt_mode == "trimmed":
            effective_text = text[:-1]
            effective_bytes = len(effective_text.encode("utf-8"))
            effective_sha256 = hashlib.sha256(
                effective_text.encode("utf-8")
            ).hexdigest()
            exact = False
        else:
            effective_bytes = expected_bytes
            effective_sha256 = expected_sha256
            exact = True
        return SimpleNamespace(
            exact_present=exact,
            expected_utf8_bytes=expected_bytes,
            expected_sha256=expected_sha256,
            effective_utf8_bytes=effective_bytes,
            effective_sha256=effective_sha256,
        )


class _Request:
    def __init__(
        self,
        *,
        fail: bool = False,
        receipt_mode: str = "exact",
    ) -> None:
        self.fail = fail
        self.receipt_mode = receipt_mode
        self.trajectory_metadata: dict[str, Any] = {}
        self.payloads: list[Any] = []
        self.deliveries: dict[str, tuple[str, str]] = {}

    def add_payload(self, payload: Any) -> None:
        self.payloads.append(payload)

    def register_context_delivery(
        self,
        delivery_id: str,
        text: str,
        *,
        marker: str,
    ) -> None:
        assert marker in text
        self.deliveries[delivery_id] = (text, marker)

    async def send(self, *, stream: bool) -> Any:
        assert stream is False
        if self.fail:
            raise RuntimeError("provider unavailable")
        return _Response(self)


class _Chatter:
    chatter_name = "same-life-consciousness"

    def __init__(
        self,
        service: _Service,
        *,
        fail: bool = False,
        receipt_mode: str = "exact",
    ) -> None:
        self.service = service
        self.request = _Request(fail=fail, receipt_mode=receipt_mode)
        self.runtime_context_text = ""
        self.dynamic_context = ""

    def _get_life_service(self) -> _Service:
        return self.service

    async def build_live_bridge_prompt(self, _stream: Any, _service: Any, **kwargs: Any):
        self.runtime_context_text = kwargs["runtime_context_text"]
        marker = "life-chatter-runtime:suffix-delivery-1"
        self.dynamic_context = (
            '<life_runtime_context><life_chatter_runtime_delivery '
            f'marker="{marker}">other-instance-is-present'
            "</life_chatter_runtime_delivery></life_runtime_context>"
        )
        prepared = await self.service.prepare_perception("livestream_42")
        self.service.pending_delivery = SimpleNamespace(
            delivery_id="suffix-delivery-1",
            delivery_marker=marker,
            prepared_perception=prepared,
        )
        return {
            "system_prompt": "same subject",
            "user_prompt": "live room",
            "dynamic_context": self.dynamic_context,
            "life_context_high_water": 5,
        }

    def create_request(self, _model_task: str, *, request_name: str) -> _Request:
        assert request_name == "livestream_director"
        return self.request


async def _director_fixture(
    tmp_path,
    *,
    fail: bool = False,
    receipt_mode: str = "exact",
):
    service = _Service()
    manager = LivestreamConsciousnessManager(
        _config(),
        "session-1",
        service=service,
    )
    await manager.activate()
    ledger = LivestreamLedger(tmp_path / "livestream.sqlite3")
    await ledger.start()
    await ledger.append_platform_event(
        "session-1",
        PlatformEvent(
            kind="danmaku",
            user_name="Ayer",
            content="你好",
            event_id="event-1",
            room_id="42",
        ),
    )
    chatter = _Chatter(service, fail=fail, receipt_mode=receipt_mode)
    deliberator = LifeChatterDeliberator(
        room_id="42",
        consciousness=manager,
        chatter_resolver=lambda *_args: chatter,
    )
    director = LivestreamDirector(ledger, deliberator, session_id="session-1")
    return service, manager, ledger, chatter, director


async def test_perception_is_transient_and_committed_after_durable_decision(
    tmp_path,
) -> None:
    service, manager, ledger, chatter, director = await _director_fixture(tmp_path)

    decision = await director.run_once()

    assert decision is not None
    assert chatter.runtime_context_text == ""
    assert "other-instance-is-present" in chatter.dynamic_context
    assert chatter.request.deliveries == {
        "suffix-delivery-1": (
            chatter.dynamic_context,
            "life-chatter-runtime:suffix-delivery-1",
        )
    }
    assert len(service.commits) == 1
    assert decision.chatter_runtime is not None
    assert decision.chatter_runtime.perception.delivery_id == "world-delivery-1"
    assert await ledger.get_record(f"director:{decision.decision_id}") is not None
    assert await ledger.get_cursor("session-1", "livestream.director.v1") == 1
    await ledger.stop()
    await manager.suspend(reason="test-ended")


async def test_model_failure_keeps_world_perception_retryable(tmp_path) -> None:
    service, manager, ledger, _chatter, director = await _director_fixture(
        tmp_path,
        fail=True,
    )

    with pytest.raises(RuntimeError, match="provider unavailable"):
        await director.run_once()

    assert service.commits == []
    assert await ledger.get_cursor("session-1", "livestream.director.v1") == 0
    assert await ledger.get_latest_record("director.decision") is None
    await ledger.stop()
    await manager.suspend(reason="test-ended")


@pytest.mark.parametrize("receipt_mode", ["missing", "trimmed", "duplicate"])
async def test_unverified_effective_context_keeps_all_cursors_retryable(
    tmp_path,
    receipt_mode: str,
) -> None:
    service, manager, ledger, _chatter, director = await _director_fixture(
        tmp_path,
        receipt_mode=receipt_mode,
    )

    with pytest.raises(DirectorProtocolError, match="absent, duplicated, or trimmed"):
        await director.run_once()

    assert service.commits == []
    assert await ledger.get_cursor("session-1", "livestream.director.v1") == 0
    assert await ledger.get_latest_record("director.decision") is None
    await ledger.stop()
    await manager.suspend(reason="test-ended")


async def test_chatter_runtime_checkpoint_commit_is_idempotent() -> None:
    service = _Service()
    manager = LivestreamConsciousnessManager(
        _config(),
        "session-1",
        service=service,
    )
    await manager.activate()
    suffix_text = (
        '<life_runtime_context><life_chatter_runtime_delivery marker="marker">'
        "other-instance-is-present"
        "</life_chatter_runtime_delivery></life_runtime_context>"
    )
    checkpoint = ChatterRuntimeCheckpoint(
        schema_version="livestream.chatter-runtime.v1",
        cursor_key="__life_chatter_global__",
        delivery_id="suffix-delivery-1",
        effective_suffix_sha256=hashlib.sha256(
            suffix_text.encode("utf-8")
        ).hexdigest(),
        effective_suffix_bytes=len(suffix_text.encode("utf-8")),
        event_through_sequence=5,
        thought_through_revision=2,
        perception=PerceptionCommitCheckpoint(
            instance_id=manager.instance_id,
            from_position=0,
            through_position=5,
            cursor_revision=0,
            delivery_id="world-delivery-1",
            projection_sha256="a" * 64,
            delivered_bytes=128,
        ),
        exact=True,
        transport_request_id="request-1",
    )

    first = await manager.commit_chatter_runtime_checkpoint(checkpoint)
    replay = await manager.commit_chatter_runtime_checkpoint(checkpoint)
    assert first.world_position == 5
    assert replay.world_position == 5
    assert len(service.commits) == 1
    await manager.suspend(reason="test-ended")


async def test_legacy_world_checkpoint_cannot_fake_exact_delivery() -> None:
    service = _Service()
    manager = LivestreamConsciousnessManager(
        _config(),
        "session-1",
        service=service,
    )
    await manager.activate()

    with pytest.raises(RuntimeError, match="no exact delivery receipt"):
        await manager.commit_perception_checkpoint(
            WorldPerceptionCheckpoint(
                instance_id=manager.instance_id,
                from_position=0,
                through_position=5,
                cursor_revision=0,
            )
        )

    assert service.commits == []
    await manager.suspend(reason="test-ended")
