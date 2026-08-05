from __future__ import annotations

import hashlib
import json
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

    def get_pending_chatter_runtime_delivery(
        self,
        stream_id: str,
        *,
        unified_chatter_context: bool,
    ):
        assert stream_id == "livestream:bilibili:42"
        assert unified_chatter_context is True
        return SimpleNamespace(
            delivery_id="suffix-delivery-1",
            delivery_marker="life-chatter-runtime:suffix-delivery-1",
        )

    def create_chatter_runtime_commit_checkpoint(
        self,
        stream_id: str,
        *,
        delivery_id: str,
        effective_suffix_sha256: str,
        effective_suffix_bytes: int,
        unified_chatter_context: bool,
    ):
        assert stream_id == "livestream:bilibili:42"
        assert delivery_id == "suffix-delivery-1"
        assert unified_chatter_context is True
        return SimpleNamespace(
            cursor_key="__life_chatter_global__",
            delivery_id=delivery_id,
            effective_suffix_sha256=effective_suffix_sha256,
            effective_suffix_bytes=effective_suffix_bytes,
            event_through_sequence=73,
            thought_through_revision=9,
            perception=SimpleNamespace(
                instance_id="livestream_42",
                from_position=11,
                through_position=17,
                cursor_revision=3,
                delivery_id="world-delivery-1",
                projection_sha256="a" * 64,
                delivered_bytes=321,
            ),
        )

    async def commit_runtime(self) -> None:
        self.mark_calls += 1
        decision = await self.ledger.get_latest_record("director.decision")
        assert decision is not None, "context advanced before decision was durable"
        assert decision.session_id == "session-1"
        if self.mark_calls == 1:
            raise OSError("injected LifeEngine cursor failure")
        self.saved += 1


class _FakeResponse:
    def __init__(self, request: _FakeRequest) -> None:
        self._request = request
        self.message = (
            '{"should_speak":false,"reason":"I choose to listen.",'
            '"addressed_event_ids":["event-0"]}'
        )
        self.request_record_id = 91

    def __await__(self):
        async def consume():
            return self.message

        return consume().__await__()

    def effective_context_receipt(self, delivery_id: str):
        expected = self._request.deliveries.get(delivery_id)
        if expected is None:
            return None
        text, _marker = expected
        encoded = text.encode("utf-8")
        digest = hashlib.sha256(encoded).hexdigest()
        return SimpleNamespace(
            exact_present=True,
            expected_utf8_bytes=len(encoded),
            expected_sha256=digest,
            effective_utf8_bytes=len(encoded),
            effective_sha256=digest,
        )


class _FakeRequest:
    def __init__(self) -> None:
        self.trajectory_metadata: dict = {}
        self.payloads: list = []
        self.send_calls = 0
        self.deliveries: dict[str, tuple[str, str]] = {}

    def add_payload(self, payload) -> None:
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

    async def send(self, *, stream: bool):
        assert stream is False
        self.send_calls += 1
        return _FakeResponse(self)


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
        dynamic_context = (
            '<life_runtime_context><life_chatter_runtime_delivery '
            'marker="life-chatter-runtime:suffix-delivery-1">'
            "other-instance-is-present"
            "</life_chatter_runtime_delivery></life_runtime_context>"
        )
        return {
            "system_prompt": "same subject",
            "user_prompt": "live room",
            "dynamic_context": dynamic_context,
            "life_context_high_water": 73,
        }

    def create_request(self, _model_task: str, *, request_name: str) -> _FakeRequest:
        assert request_name == "livestream_director"
        return self.request


class _FakeConsciousness:
    is_active = True

    def __init__(self, service: _FakeLifeService) -> None:
        self.service = service
        self.commits = []

    async def commit_chatter_runtime_checkpoint(self, checkpoint):
        self.commits.append(checkpoint)
        await self.service.commit_runtime()


async def test_life_context_cursor_replays_after_durable_decision(tmp_path) -> None:
    ledger = await _ledger_with_events(tmp_path, count=1)
    service = _FakeLifeService(ledger)
    chatter = _FakeChatter(service)
    consciousness = _FakeConsciousness(service)
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
    assert durable.payload["world_perception"] is None
    assert durable.payload["chatter_runtime"]["delivery_id"] == (
        "suffix-delivery-1"
    )
    assert durable.payload["chatter_runtime"]["perception"] == {
        "instance_id": "livestream_42",
        "from_position": 11,
        "through_position": 17,
        "cursor_revision": 3,
        "delivery_id": "world-delivery-1",
        "projection_sha256": "a" * 64,
        "delivered_bytes": 321,
    }
    assert "other-instance-is-present" not in json.dumps(durable.payload)

    decision = await director.run_once()
    cursor = await ledger.get_cursor("session-1", "livestream.director.v1")
    await ledger.stop()

    assert decision is not None
    assert chatter.commit_cursors is True
    assert chatter.request.send_calls == 1
    assert service.mark_calls == 2
    assert service.saved == 1
    assert len(consciousness.commits) == 2
    assert consciousness.commits[0] == consciousness.commits[1]
    assert chatter.runtime_context_text == ""
    assert "suffix-delivery-1" in chatter.request.deliveries
    assert cursor == 1
