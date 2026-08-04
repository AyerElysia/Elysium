from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from plugins.livestream.director import (
    DirectorProtocolError,
    LifeChatterDeliberator,
    LivestreamDirector,
)
from plugins.livestream.domain import PerformancePlan, PlatformEvent
from plugins.livestream.ledger import LivestreamLedger

pytestmark = pytest.mark.asyncio


@dataclass
class FakeDeliberator:
    plan: PerformancePlan
    calls: int = 0
    actor: str = "same-life-consciousness"

    async def deliberate(
        self,
        events: Sequence[PlatformEvent],
        *,
        session_id: str,
        source_sequences: Sequence[int],
    ) -> PerformancePlan:
        self.calls += 1
        assert session_id == "session-1"
        assert len(events) == len(source_sequences)
        return self.plan


async def _ledger_with_events(tmp_path, count: int = 2) -> LivestreamLedger:
    ledger = LivestreamLedger(tmp_path / "livestream.sqlite3")
    await ledger.start()
    for index in range(count):
        await ledger.append_platform_event(
            "session-1",
            PlatformEvent(
                kind="danmaku",
                user_name=f"viewer-{index}",
                content=f"message-{index}",
                event_id=f"event-{index}",
                room_id="42",
                timestamp=100.0 + index,
            ),
        )
    return ledger


async def test_director_persists_decision_and_plan_before_cursor(tmp_path) -> None:
    ledger = await _ledger_with_events(tmp_path)
    deliberator = FakeDeliberator(
        PerformancePlan(
            should_speak=True,
            reason="I want to answer this conversation.",
            speech_text="Hello, everyone.",
            addressed_event_ids=["event-0", "event-1"],
        )
    )
    director = LivestreamDirector(
        ledger,
        deliberator,
        session_id="session-1",
    )

    decision = await director.run_once()
    records = await ledger.read_since(0, session_id="session-1")
    cursor = await ledger.get_cursor("session-1", "livestream.director.v1")
    await ledger.stop()

    assert decision is not None
    assert [record.kind for record in records] == [
        "platform.event",
        "platform.event",
        "director.decision",
        "performance.planned",
    ]
    assert cursor == 2
    assert deliberator.calls == 1


async def test_director_replay_reuses_decision_after_cursor_commit_failure(
    tmp_path, monkeypatch
) -> None:
    ledger = await _ledger_with_events(tmp_path, count=1)
    deliberator = FakeDeliberator(
        PerformancePlan(
            should_speak=True,
            reason="A chosen response.",
            speech_text="Hi.",
            addressed_event_ids=["event-0"],
        )
    )
    director = LivestreamDirector(ledger, deliberator, session_id="session-1")
    original_commit = ledger.commit_cursor
    failed = False

    async def fail_once(*args, **kwargs) -> None:
        nonlocal failed
        if not failed:
            failed = True
            raise OSError("injected cursor failure")
        await original_commit(*args, **kwargs)

    monkeypatch.setattr(ledger, "commit_cursor", fail_once)
    with pytest.raises(OSError, match="injected"):
        await director.run_once()

    decision = await director.run_once()
    records = await ledger.read_since(0, session_id="session-1")
    await ledger.stop()

    assert decision is not None
    assert deliberator.calls == 1
    assert [record.kind for record in records].count("director.decision") == 1
    assert [record.kind for record in records].count("performance.planned") == 1


async def test_silence_is_an_auditable_decision_without_performance_plan(tmp_path) -> None:
    ledger = await _ledger_with_events(tmp_path, count=1)
    deliberator = FakeDeliberator(
        PerformancePlan(
            should_speak=False,
            reason="I prefer to keep listening.",
        )
    )
    director = LivestreamDirector(ledger, deliberator, session_id="session-1")

    await director.run_once()
    records = await ledger.read_since(0, session_id="session-1")
    await ledger.stop()

    assert deliberator.calls == 1
    assert [record.kind for record in records] == [
        "platform.event",
        "director.decision",
    ]


async def test_plan_parser_repairs_json_but_rejects_unknown_event_references() -> None:
    event = PlatformEvent(
        kind="danmaku",
        user_name="viewer",
        event_id="event-1",
    )
    repaired = LifeChatterDeliberator.parse_plan(
        "```json\n{'should_speak': true, 'reason': 'chosen', "
        "'speech_text': 'hello', 'addressed_event_ids': ['event-1']}\n```",
        [event],
    )
    assert repaired.speech_text == "hello"

    with pytest.raises(DirectorProtocolError, match="unknown"):
        LifeChatterDeliberator.parse_plan(
            '{"should_speak": true, "reason": "chosen", '
            '"speech_text": "hello", "addressed_event_ids": ["missing"]}',
            [event],
        )


class _FakeLifeService:
    def __init__(self, ledger: LivestreamLedger) -> None:
        self.ledger = ledger
        self.mark_calls = 0
        self.saved = 0

    async def mark_chatter_runtime_context_seen(
        self,
        stream_id: str,
        sequence: int,
        *,
        unified_chatter_context: bool,
    ) -> None:
        self.mark_calls += 1
        decision = await self.ledger.get_latest_record("director.decision")
        assert decision is not None, "context advanced before decision was durable"
        assert decision.session_id == "session-1"
        assert stream_id == "livestream:bilibili:42"
        assert sequence == 73
        assert unified_chatter_context is True
        if self.mark_calls == 1:
            raise OSError("injected LifeEngine cursor failure")

    async def _save_runtime_context(self) -> None:
        self.saved += 1


class _FakeRequest:
    def __init__(self) -> None:
        self.trajectory_metadata: dict = {}
        self.payloads: list = []
        self.send_calls = 0

    def add_payload(self, payload) -> None:
        self.payloads.append(payload)

    async def send(self, *, stream: bool):
        assert stream is False
        self.send_calls += 1
        return SimpleNamespace(
            message=(
                '{"should_speak":false,"reason":"I choose to listen.",'
                '"addressed_event_ids":["event-0"]}'
            )
        )


class _FakeChatter:
    chatter_name = "same-life-consciousness"

    def __init__(self, service: _FakeLifeService) -> None:
        self.service = service
        self.request = _FakeRequest()
        self.commit_cursors: bool | None = None
        self.runtime_context_text = ""

    def _get_life_service(self) -> _FakeLifeService:
        return self.service

    async def build_live_bridge_prompt(self, _stream, _service, **kwargs):
        self.commit_cursors = kwargs["commit_cursors"]
        self.runtime_context_text = kwargs["runtime_context_text"]
        return {
            "system_prompt": "same subject",
            "user_prompt": "live room",
            "dynamic_context": "life context",
            "life_context_high_water": 73,
        }

    def create_request(self, _model_task: str, *, request_name: str) -> _FakeRequest:
        assert request_name == "livestream_director"
        return self.request


class _FakeConsciousness:
    is_active = True

    def __init__(self) -> None:
        self.commits = []

    async def prepare_perception(self):
        return SimpleNamespace(
            instance_id="livestream_42",
            from_position=11,
            through_position=17,
            cursor_revision=3,
            content="other-instance-is-present",
        )

    async def commit_perception_checkpoint(self, checkpoint):
        self.commits.append(checkpoint)
        return checkpoint.through_position, checkpoint.cursor_revision + 1


async def test_life_context_cursor_replays_after_durable_decision(tmp_path) -> None:
    ledger = await _ledger_with_events(tmp_path, count=1)
    service = _FakeLifeService(ledger)
    chatter = _FakeChatter(service)
    consciousness = _FakeConsciousness()
    deliberator = LifeChatterDeliberator(
        room_id="42",
        consciousness=consciousness,
        chatter_resolver=lambda *_args: chatter,
    )
    director = LivestreamDirector(ledger, deliberator, session_id="session-1")

    with pytest.raises(OSError, match="injected LifeEngine"):
        await director.run_once()
    assert await ledger.get_cursor("session-1", "livestream.director.v1") == 0
    durable = await ledger.get_latest_record("director.decision")
    assert durable is not None
    assert durable.payload["life_context_high_water"] == 73
    assert durable.payload["world_perception"] == {
        "instance_id": "livestream_42",
        "from_position": 11,
        "through_position": 17,
        "cursor_revision": 3,
    }

    decision = await director.run_once()
    cursor = await ledger.get_cursor("session-1", "livestream.director.v1")
    await ledger.stop()

    assert decision is not None
    assert chatter.commit_cursors is True
    assert chatter.request.send_calls == 1
    assert service.mark_calls == 2
    assert service.saved == 1
    assert len(consciousness.commits) == 1
    assert "other-instance-is-present" in chatter.runtime_context_text
    assert cursor == 1
