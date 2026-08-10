"""Deadline and cancellation contracts for the asynchronous memory witness."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from plugins.life_engine.memory.experience import ExperienceRecord
from plugins.life_engine.service.consciousness import (
    ConsciousnessInstance,
    ConsciousnessRegistry,
)
from plugins.life_engine.service.event_bus import LifeEvent
from plugins.life_engine.service.memory_witness import MemoryWitnessCoordinator


class _MemoryStub:
    def __init__(self) -> None:
        self.state_updates: list[dict[str, Any]] = []

    async def get_witness_state(self, _instance_id: str) -> dict[str, Any]:
        return {"last_sequence": 0, "revision": 0}

    async def list_pending_witness_projections(self, *, limit: int) -> list[Any]:
        assert limit == 20
        return []

    async def append_experiences(
        self,
        records: list[ExperienceRecord],
    ) -> int:
        return len(records)

    async def get_witness_by_projection_path(self, _path: str) -> None:
        return None

    async def update_witness_state(
        self,
        _instance_id: str,
        **kwargs: Any,
    ) -> None:
        self.state_updates.append(kwargs)


class _EventStoreStub:
    def __init__(self) -> None:
        self.offset_commits: list[int] = []
        self.event = LifeEvent(
            event_id="event-private",
            sequence=1,
            timestamp="2026-08-10T08:00:00+08:00",
            source="chat",
            channel="chat",
            event_type="text",
            content="private-event-content",
            stream_id="stream-private",
            occurrence_id="occ-private",
        )

    async def get_consumer_offset(self, _consumer_id: str) -> int:
        return 0

    async def read_since(self, sequence: int, *, limit: int) -> list[LifeEvent]:
        assert sequence == 0
        assert limit == 80
        return [self.event]

    async def commit_consumer_offset(
        self,
        _consumer_id: str,
        sequence: int,
        *,
        metadata: dict[str, Any],
    ) -> None:
        assert metadata == {"witness_state_mirror": True}
        self.offset_commits.append(sequence)


class _Response:
    message = ""
    request_record_id = "request-private"

    def __init__(
        self,
        *,
        response_started: asyncio.Event,
        response_cleaned: asyncio.Event,
    ) -> None:
        self._response_started = response_started
        self._response_cleaned = response_cleaned

    async def _consume(self) -> str:
        self._response_started.set()
        try:
            await asyncio.sleep(3600)
        finally:
            self._response_cleaned.set()
        return "private-response-content"

    def __await__(self):
        return self._consume().__await__()


def _service(
    *,
    timeout_seconds: float,
) -> tuple[SimpleNamespace, _MemoryStub, _EventStoreStub, list[object]]:
    registry = ConsciousnessRegistry()
    memory = _MemoryStub()
    event_store = _EventStoreStub()
    perception_commits: list[object] = []
    perception = SimpleNamespace(
        delivery_id="witness-delivery",
        delivery_marker="world-perception:witness-delivery",
        content="world-perception:witness-delivery\nprivate-world-content",
        projection_sha256="projection-sha256",
        delivered_bytes=21,
    )

    async def register(
        instance: ConsciousnessInstance,
    ) -> ConsciousnessInstance:
        return registry.register(instance)

    async def resume(instance_id: str, **kwargs: Any) -> bool:
        return registry.resume(instance_id, **kwargs)

    async def touch(instance_id: str, **kwargs: Any) -> None:
        registry.touch(instance_id, **kwargs)

    async def prepare_perception(_instance_id: str) -> object:
        return perception

    async def commit_perception(_prepared: object, receipt: object) -> None:
        perception_commits.append(receipt)

    async def read_subject_authority_texts() -> dict[str, str]:
        return {"SOUL.md": "subject authority", "USER.md": "", "MEMORY.md": ""}

    config = SimpleNamespace(
        enabled=True,
        max_events_per_run=80,
        model_task_name="witness",
        timeout_seconds=timeout_seconds,
        migrate_legacy_diaries=False,
    )
    service = SimpleNamespace(
        consciousness_registry=registry,
        register_consciousness_instance=register,
        resume_consciousness_instance=resume,
        touch_consciousness_instance=touch,
        memory_service=memory,
        _cfg=lambda: SimpleNamespace(memory_witness=config),
        _get_life_event_store=lambda: event_store,
        prepare_perception=prepare_perception,
        commit_perception=commit_perception,
        read_subject_authority_texts=read_subject_authority_texts,
    )
    return service, memory, event_store, perception_commits


def _request_type(
    *,
    response_started: asyncio.Event,
    response_cleaned: asyncio.Event,
    send_delay: float = 0.0,
):
    class _Request:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            self.expected_text = ""

        def add_payload(self, _payload: object) -> None:
            return None

        def register_context_delivery(
            self,
            _delivery_id: str,
            expected_text: str,
            *,
            marker: str,
        ) -> None:
            assert marker in expected_text
            self.expected_text = expected_text

        async def send(self) -> _Response:
            if send_delay:
                await asyncio.sleep(send_delay)
            return _Response(
                response_started=response_started,
                response_cleaned=response_cleaned,
            )

    return _Request


@pytest.mark.asyncio
async def test_witness_uses_one_total_deadline_and_preserves_cursor_on_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, memory, event_store, perception_commits = _service(
        timeout_seconds=10.0
    )
    response_started = asyncio.Event()
    response_cleaned = asyncio.Event()
    timeout_deadlines: list[float] = []

    class _FastTimeout:
        def __init__(self, deadline: float) -> None:
            timeout_deadlines.append(deadline)
            self._scope = asyncio.timeout(0.04)

        async def __aenter__(self) -> None:
            await self._scope.__aenter__()

        async def __aexit__(self, *args: object) -> bool | None:
            return await self._scope.__aexit__(*args)

        def expired(self) -> bool:
            return self._scope.expired()

    def fast_timeout_at(deadline: float) -> _FastTimeout:
        return _FastTimeout(deadline)

    monkeypatch.setattr(
        "plugins.life_engine.service.memory_witness.get_model_set_by_task",
        lambda _task: ({"model_identifier": "test"},),
    )
    monkeypatch.setattr(
        "plugins.life_engine.service.memory_witness.LLMRequest",
        _request_type(
            response_started=response_started,
            response_cleaned=response_cleaned,
            send_delay=0.02,
        ),
    )
    monkeypatch.setattr(
        "plugins.life_engine.service.memory_witness.asyncio.timeout_at",
        fast_timeout_at,
    )

    started = asyncio.get_running_loop().time()
    with pytest.raises(TimeoutError) as exc_info:
        await MemoryWitnessCoordinator(service).run_once()

    assert response_started.is_set()
    assert response_cleaned.is_set()
    assert len(timeout_deadlines) == 1
    assert 9.9 <= timeout_deadlines[0] - started <= 10.1
    assert perception_commits == []
    assert event_store.offset_commits == []
    assert not any("last_sequence" in item for item in memory.state_updates)
    message = str(exc_info.value)
    assert "configured_timeout=10.000" in message
    assert "task_name=witness" in message
    assert "private-event-content" not in message
    assert "private-world-content" not in message
    assert "private-response-content" not in message


@pytest.mark.asyncio
async def test_witness_external_cancellation_propagates_without_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, memory, event_store, perception_commits = _service(
        timeout_seconds=600.0
    )
    response_started = asyncio.Event()
    response_cleaned = asyncio.Event()
    monkeypatch.setattr(
        "plugins.life_engine.service.memory_witness.get_model_set_by_task",
        lambda _task: ({"model_identifier": "test"},),
    )
    monkeypatch.setattr(
        "plugins.life_engine.service.memory_witness.LLMRequest",
        _request_type(
            response_started=response_started,
            response_cleaned=response_cleaned,
        ),
    )

    task = asyncio.create_task(MemoryWitnessCoordinator(service).run_once())
    await asyncio.wait_for(response_started.wait(), timeout=1.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert response_cleaned.is_set()
    assert perception_commits == []
    assert event_store.offset_commits == []
    assert not any("last_sequence" in item for item in memory.state_updates)
