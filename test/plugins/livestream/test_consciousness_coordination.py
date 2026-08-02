"""Livestream contracts for real presence and retry-safe world perception."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from plugins.life_engine.service.consciousness import ConsciousnessRegistry
from plugins.life_engine.service.perception_gateway import PreparedPerception
from plugins.livestream.config import LivestreamConfig
from plugins.livestream.consciousness import LivestreamConsciousnessManager
from plugins.livestream.director import LifeChatterDeliberator, LivestreamDirector
from plugins.livestream.domain import PlatformEvent, WorldPerceptionCheckpoint
from plugins.livestream.ledger import LivestreamLedger

pytestmark = pytest.mark.asyncio


class _Service:
    """Minimal supported LifeEngine integration used by the manager."""

    def __init__(self) -> None:
        self.consciousness_registry = ConsciousnessRegistry()
        self.observations: list[dict[str, Any]] = []
        self.saves = 0
        self.commits: list[PreparedPerception] = []
        self.current_position = 0

    def save_consciousness_registry(self) -> None:
        self.saves += 1

    async def report_world_observation(
        self,
        report: str,
        **kwargs: Any,
    ) -> dict[str, str]:
        self.observations.append({"report": report, **kwargs})
        return {"assertion_id": f"a-{len(self.observations)}"}

    def prepare_perception(self, instance_id: str) -> PreparedPerception:
        return PreparedPerception(
            instance_id=instance_id,
            from_position=self.current_position,
            through_position=max(self.current_position, 5),
            cursor_revision=len(self.commits),
            content="other-instance-is-present",
            assertion_ids=("assertion-1",),
            change_positions=(5,) if self.current_position < 5 else (),
        )

    def commit_perception(self, prepared: PreparedPerception) -> tuple[int, int]:
        assert prepared.from_position == self.current_position
        self.current_position = prepared.through_position
        self.commits.append(prepared)
        return self.current_position, len(self.commits)


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


class _Request:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.trajectory_metadata: dict[str, Any] = {}
        self.payloads: list[Any] = []

    def add_payload(self, payload: Any) -> None:
        self.payloads.append(payload)

    async def send(self, *, stream: bool) -> Any:
        assert stream is False
        if self.fail:
            raise RuntimeError("provider unavailable")
        return SimpleNamespace(
            message=(
                '{"should_speak":false,"reason":"I choose to listen.",'
                '"addressed_event_ids":["event-1"]}'
            )
        )


class _Chatter:
    chatter_name = "same-life-consciousness"

    def __init__(self, service: _Service, *, fail: bool = False) -> None:
        self.service = service
        self.request = _Request(fail=fail)
        self.runtime_context_text = ""

    def _get_life_service(self) -> _Service:
        return self.service

    async def build_live_bridge_prompt(self, _stream: Any, _service: Any, **kwargs: Any):
        self.runtime_context_text = kwargs["runtime_context_text"]
        return {
            "system_prompt": "same subject",
            "user_prompt": "live room",
            "dynamic_context": "",
            "life_context_high_water": 0,
        }

    def create_request(self, _model_task: str, *, request_name: str) -> _Request:
        assert request_name == "livestream_director"
        return self.request


async def _director_fixture(tmp_path, *, fail: bool = False):
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
    chatter = _Chatter(service, fail=fail)
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
    assert "other-instance-is-present" in chatter.runtime_context_text
    assert len(service.commits) == 1
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


async def test_world_perception_checkpoint_commit_is_idempotent() -> None:
    service = _Service()
    manager = LivestreamConsciousnessManager(
        _config(),
        "session-1",
        service=service,
    )
    await manager.activate()
    checkpoint = WorldPerceptionCheckpoint(
        instance_id=manager.instance_id,
        from_position=0,
        through_position=5,
        cursor_revision=0,
    )

    assert manager.commit_perception_checkpoint(checkpoint) == (5, 1)
    assert manager.commit_perception_checkpoint(checkpoint) == (5, 1)
    assert len(service.commits) == 1
    await manager.suspend(reason="test-ended")
