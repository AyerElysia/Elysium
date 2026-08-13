"""Deadline and cancellation contracts for the asynchronous memory witness."""

from __future__ import annotations

import asyncio
import hashlib
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.exc import OperationalError

from plugins.life_engine.memory.experience import (
    ExperienceOccurrenceRef,
    ExperienceRecord,
)
from plugins.life_engine.service.consciousness import (
    ConsciousnessInstance,
    ConsciousnessRegistry,
)
from plugins.life_engine.service.event_bus import LifeEvent
from plugins.life_engine.service.memory_witness import MemoryWitnessCoordinator
from plugins.life_engine.service.presence_store import PresenceRevisionConflict


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
        self.consumer_offset = 0
        self.read_cursors: list[int] = []
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
        return self.consumer_offset

    async def read_since(self, sequence: int, *, limit: int) -> list[LifeEvent]:
        self.read_cursors.append(sequence)
        assert limit == 80
        return [self.event] if sequence < self.event.sequence else []

    async def commit_consumer_offset(
        self,
        _consumer_id: str,
        sequence: int,
        *,
        metadata: dict[str, Any],
    ) -> None:
        assert metadata == {"witness_state_mirror": True}
        self.offset_commits.append(sequence)
        self.consumer_offset = sequence


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
    world_content = "world-perception:witness-delivery\nprivate-world-content"
    perception = SimpleNamespace(
        instance_id="memory_witness",
        from_position=0,
        through_position=1,
        cursor_revision=0,
        delivery_id="witness-delivery",
        delivery_marker="world-perception:witness-delivery",
        content=world_content,
        projection_sha256=hashlib.sha256(world_content.encode("utf-8")).hexdigest(),
        delivered_bytes=len(world_content.encode("utf-8")),
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

    async def subject_projection(**_kwargs: object) -> dict[str, object]:
        return _subject_projection()

    config = SimpleNamespace(
        enabled=True,
        run_on_startup=True,
        interval_seconds=60,
        retry_delay_seconds=10,
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
        get_subject_context_projection_snapshot=subject_projection,
        _state=SimpleNamespace(running=True),
        _stop_event=None,
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

        async def send(self, *, stream: bool = True) -> _Response:
            assert stream is False
            if send_delay:
                await asyncio.sleep(send_delay)
            return _Response(
                response_started=response_started,
                response_cleaned=response_cleaned,
            )

    return _Request


def _deadline_occurrence() -> ExperienceOccurrenceRef:
    record = ExperienceRecord(
        event_id="occ-private",
        source_event_id="event-private",
        sequence=1,
        occurred_at="2026-08-10T08:00:00+08:00",
        recorded_at="2026-08-10T08:00:01+08:00",
        source="chat",
        channel="chat",
        event_type="text",
        content="private-event-content",
        stream_id="stream-private",
        consciousness_instance_id="chat_global",
        actor="user",
        valid_from="2026-08-10T08:00:00+08:00",
    )
    return ExperienceOccurrenceRef(
        occurrence_id=record.event_id,
        source_event_id=record.source_event_id,
        ingest_position=record.sequence,
        canonical_event_id=record.event_id,
        canonical_payload_sha256="a" * 64,
        recorded_at=record.recorded_at,
        experience=record,
    )


def _subject_projection() -> dict[str, object]:
    source_digest = "b" * 64
    text = f"""# Subject Context Projection

- source_digest: `{source_digest}`
- projection_version: `3`

<subject-source path="SOUL.md">
SOUL projection
</subject-source>

<subject-source path="USER.md">
USER projection
</subject-source>

<subject-source path="MEMORY.md">
MEMORY projection
</subject-source>"""
    return {
        "text": text,
        "source_digest": source_digest,
        "projection_version": 3,
        "projection_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
    }


@pytest.mark.asyncio
async def test_witness_uses_one_total_deadline_and_preserves_cursor_on_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, memory, event_store, perception_commits = _service(timeout_seconds=10.0)
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

    coordinator = MemoryWitnessCoordinator(service)
    instance = ConsciousnessInstance(
        instance_id="memory_witness",
        kind="memory_witness",
    )
    started = asyncio.get_running_loop().time()
    with pytest.raises(TimeoutError) as exc_info:
        await coordinator._author_witness(instance, (_deadline_occurrence(),))

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
    service, memory, event_store, perception_commits = _service(timeout_seconds=600.0)
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

    coordinator = MemoryWitnessCoordinator(service)
    instance = ConsciousnessInstance(
        instance_id="memory_witness",
        kind="memory_witness",
    )
    task = asyncio.create_task(
        coordinator._author_witness(instance, (_deadline_occurrence(),))
    )
    await asyncio.wait_for(response_started.wait(), timeout=1.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert response_cleaned.is_set()
    assert perception_commits == []
    assert event_store.offset_commits == []
    assert not any("last_sequence" in item for item in memory.state_updates)


@pytest.mark.asyncio
async def test_witness_loop_recovers_presence_conflict_without_losing_cursor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, memory, event_store, _perception_commits = _service(timeout_seconds=600.0)
    coordinator = MemoryWitnessCoordinator(service)
    author_attempts = 0
    refresh_calls = 0
    delays: list[float] = []
    warning_records: list[tuple[str, dict[str, Any]]] = []

    async def refresh() -> None:
        nonlocal refresh_calls
        refresh_calls += 1

    async def author(*_args: object) -> str:
        nonlocal author_attempts
        author_attempts += 1
        if author_attempts == 1:
            raise PresenceRevisionConflict("stale witness presence")
        service._state.running = False
        return ""

    async def no_wait(delay: float) -> None:
        delays.append(delay)

    def capture_warning(message: str, **metadata: Any) -> None:
        warning_records.append((message, metadata))

    service.consciousness_registry.refresh = refresh
    # The loop contract is independent of the evolving Witness pipeline Ports.
    # Exercise the managed-worker boundary directly so this regression cannot
    # accidentally reintroduce a private LifeMemoryService test double.
    monkeypatch.setattr(coordinator, "run_once", author)
    monkeypatch.setattr(
        "plugins.life_engine.service.memory_witness.asyncio.sleep",
        no_wait,
    )
    monkeypatch.setattr(
        "plugins.life_engine.service.memory_witness.logger.warning",
        capture_warning,
    )
    monkeypatch.setattr(
        "plugins.life_engine.service.memory_witness.logger.info",
        lambda *_args, **_kwargs: None,
    )

    await coordinator.loop()

    assert author_attempts == 2
    assert refresh_calls == 1
    assert delays == [10]
    assert event_store.read_cursors == []
    assert event_store.offset_commits == []
    sequence_updates = [
        item["last_sequence"]
        for item in memory.state_updates
        if "last_sequence" in item
    ]
    assert sequence_updates == []
    assert len(warning_records) == 1
    assert warning_records[0][1]["exc_info"].__class__ is PresenceRevisionConflict


@pytest.mark.asyncio
async def test_witness_concurrency_conflict_escalates_only_after_eight_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PresenceRevisionConflict is normal contention on a shared presence row:
    it must not spam ERROR. Only the 9th consecutive conflict escalates (F3-B)."""

    service, _memory, _event_store, _perception_commits = _service(
        timeout_seconds=600.0
    )
    coordinator = MemoryWitnessCoordinator(service)
    attempts = 0
    error_records: list[tuple[str, dict[str, Any]]] = []

    async def author(*_args: object) -> str:
        nonlocal attempts
        attempts += 1
        if attempts >= 9:
            service._state.running = False
        raise PresenceRevisionConflict("stale witness presence")

    async def no_wait(delay: float) -> None:
        return None

    def capture_error(message: str, **metadata: Any) -> None:
        error_records.append((message, metadata))

    monkeypatch.setattr(coordinator, "run_once", author)
    monkeypatch.setattr(
        "plugins.life_engine.service.memory_witness.asyncio.sleep",
        no_wait,
    )
    monkeypatch.setattr(
        "plugins.life_engine.service.memory_witness.logger.error",
        capture_error,
    )
    for level in ("warning", "debug", "info"):
        monkeypatch.setattr(
            f"plugins.life_engine.service.memory_witness.logger.{level}",
            lambda *_args, **_kwargs: None,
        )

    await coordinator.loop()

    assert attempts == 9
    assert len(error_records) == 1
    assert "可恢复并发冲突" in error_records[0][0]
    assert "failure_count=9" in error_records[0][0]


@pytest.mark.asyncio
async def test_witness_loop_retries_mysql_2013_with_same_experience_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A selected-MySQL disconnect keeps the exact cursor window retryable."""

    service, _memory, event_store, _perception_commits = _service(timeout_seconds=600.0)

    memory = _MemoryStub()
    service.memory_service = memory
    coordinator = MemoryWitnessCoordinator(service)
    author_windows: list[tuple[str, ...]] = []
    delays: list[float] = []
    warnings: list[str] = []
    errors: list[str] = []
    infos: list[str] = []

    async def author(*_args: object) -> str:
        author_windows.append(("occ-private",))
        if len(author_windows) == 1:
            raise OperationalError(
                "SELECT selected presence",
                {},
                OSError(2013, "Lost connection to MySQL server during query"),
                connection_invalidated=True,
            )
        service._state.running = False
        return ""

    async def no_wait(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr(coordinator, "run_once", author)
    monkeypatch.setattr(
        "plugins.life_engine.service.memory_witness.asyncio.sleep",
        no_wait,
    )
    monkeypatch.setattr(
        "plugins.life_engine.service.memory_witness.logger.warning",
        warnings.append,
    )
    monkeypatch.setattr(
        "plugins.life_engine.service.memory_witness.logger.error",
        errors.append,
    )
    monkeypatch.setattr(
        "plugins.life_engine.service.memory_witness.logger.info",
        infos.append,
    )

    await coordinator.loop()

    assert author_windows == [("occ-private",), ("occ-private",)]
    assert event_store.read_cursors == []
    assert event_store.offset_commits == []
    assert delays == [10]
    assert errors == []
    assert len(warnings) == 1
    assert "待处理经历已保留" in warnings[0]
    assert "OperationalError(code=2013)" in warnings[0]
    assert infos == ["记忆见证上游已恢复: previous_failures=1"]
    sequence_updates = [
        item["last_sequence"]
        for item in memory.state_updates
        if "last_sequence" in item
    ]
    assert sequence_updates == []


@pytest.mark.asyncio
async def test_witness_tail_presence_conflict_keeps_committed_run_successful(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A post-commit Presence CAS race cannot invalidate durable witness work."""

    service, memory, event_store, _perception_commits = _service(timeout_seconds=600.0)
    coordinator = MemoryWitnessCoordinator(service)
    touch_calls = 0
    refresh_calls = 0
    projected: list[str] = []
    warnings: list[str] = []

    async def author(*_args: object) -> str:
        return "committed first-person witness"

    async def record_witness_memory(**kwargs: object) -> object:
        return SimpleNamespace(
            witness_id="witness-committed",
            projection_path=kwargs["projection_path"],
        )

    async def project(witness: object) -> None:
        projected.append(str(witness.witness_id))

    async def touch(_instance_id: str, **_kwargs: object) -> None:
        nonlocal touch_calls
        touch_calls += 1
        assert event_store.offset_commits == [1]
        assert memory.state_updates[-1]["last_sequence"] == 1
        assert memory.state_updates[-1]["last_error"] == ""
        raise PresenceRevisionConflict("stale post-commit presence")

    async def refresh() -> None:
        nonlocal refresh_calls
        refresh_calls += 1

    memory.record_witness_memory = record_witness_memory  # type: ignore[attr-defined]
    service.touch_consciousness_instance = touch
    service.consciousness_registry.refresh = refresh
    monkeypatch.setattr(coordinator, "_author_witness", author)
    monkeypatch.setattr(coordinator, "_project_witness", project)
    monkeypatch.setattr(
        "plugins.life_engine.service.memory_witness.logger.warning",
        warnings.append,
    )

    # The authoritative Witness/Experience commits have already succeeded when
    # this auxiliary Presence touch runs.  Test the tail operation directly;
    # do not rebuild the six-domain storage pipeline with a private stub.
    event_store.offset_commits.append(1)
    memory.state_updates.append({"last_sequence": 1, "last_error": ""})
    await coordinator._touch_presence_after_commit(
        "memory_witness",
        timestamp="2026-08-10T08:00:01+08:00",
    )

    assert projected == []
    assert event_store.offset_commits == [1]
    assert touch_calls == 2
    assert refresh_calls == 2
    assert warnings == [
        (
            "记忆见证已提交，Presence 尾触摸仍有 CAS 冲突，"
            "本轮成功状态保持不变: retry_count=1, "
            "error=PresenceRevisionConflict"
        )
    ]


@pytest.mark.asyncio
async def test_witness_loop_logs_unclassified_failure_with_exc_info_and_continues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _memory, _event_store, _perception_commits = _service(
        timeout_seconds=600.0
    )
    coordinator = MemoryWitnessCoordinator(service)
    failure = RuntimeError("injected witness failure")
    run_count = 0
    delays: list[float] = []
    error_records: list[tuple[str, dict[str, Any]]] = []

    async def run_once() -> None:
        nonlocal run_count
        run_count += 1
        if run_count == 1:
            raise failure
        service._state.running = False

    async def no_wait(delay: float) -> None:
        delays.append(delay)

    def capture_error(message: str, **metadata: Any) -> None:
        error_records.append((message, metadata))

    monkeypatch.setattr(coordinator, "run_once", run_once)
    monkeypatch.setattr(
        "plugins.life_engine.service.memory_witness.asyncio.sleep",
        no_wait,
    )
    monkeypatch.setattr(
        "plugins.life_engine.service.memory_witness.logger.error",
        capture_error,
    )

    await coordinator.loop()

    assert run_count == 2
    assert delays == [60]
    assert len(error_records) == 1
    assert error_records[0][1]["exc_info"] is failure


@pytest.mark.asyncio
async def test_witness_loop_survives_diagnostic_and_logger_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _memory, _event_store, _perception_commits = _service(
        timeout_seconds=600.0
    )
    coordinator = MemoryWitnessCoordinator(service)
    run_count = 0
    delays: list[float] = []
    log_attempts = 0

    async def run_once() -> None:
        nonlocal run_count
        run_count += 1
        if run_count == 1:
            raise RuntimeError("primary failure")
        service._state.running = False

    async def broken_record_error(_exc: Exception) -> None:
        raise RuntimeError("diagnostic state failure")

    async def no_wait(delay: float) -> None:
        delays.append(delay)

    def broken_logger(*_args: object, **_kwargs: object) -> None:
        nonlocal log_attempts
        log_attempts += 1
        raise RuntimeError("logger sink failure")

    monkeypatch.setattr(coordinator, "run_once", run_once)
    monkeypatch.setattr(coordinator, "_record_error", broken_record_error)
    monkeypatch.setattr(
        "plugins.life_engine.service.memory_witness.asyncio.sleep",
        no_wait,
    )
    monkeypatch.setattr(
        "plugins.life_engine.service.memory_witness.logger.error",
        broken_logger,
    )

    await coordinator.loop()

    assert run_count == 2
    assert delays == [60]
    assert log_attempts == 2


def _witness_instance() -> ConsciousnessInstance:
    from plugins.life_engine.service.world_state import PerceptionFilter

    return ConsciousnessInstance(
        instance_id="memory_witness",
        kind="memory_witness",
        display_name="爱莉的记忆见证意识",
        status="active",
        created_at="2026-08-11T12:00:00+08:00",
        last_active_at="2026-08-11T12:00:00+08:00",
        perception_filter=PerceptionFilter.full(),
        metadata={
            "role": "first_person_experience_witness",
            "epistemic_boundary": "subjective_witness_not_objective_truth",
            "reads": "immutable_experience_ledger",
        },
    )


@pytest.mark.asyncio
async def test_witness_ensure_instance_degrades_on_presence_contention() -> None:
    """A contended memory_witness presence must not fail plugin startup.

    In a multi-writer deployment the resident Linux node keeps touching the
    shared ``memory_witness`` presence row, so an occasional Windows guest
    frequently races a PresenceRevisionConflict on its startup touch.  The
    coordinator refreshes the snapshot and retries a bounded number of times,
    then degrades to a local read-only instance handle instead of raising.
    """

    service, memory, event_store, _perception_commits = _service(timeout_seconds=10.0)
    witness = _witness_instance()
    service.consciousness_registry.register(witness)

    touch_attempts = 0
    refresh_calls = 0

    async def contended_touch(_instance_id: str, **kwargs: Any) -> None:
        nonlocal touch_attempts
        touch_attempts += 1
        raise PresenceRevisionConflict(
            "presence revision conflict for 'memory_witness': "
            "expected 1637, actual 1638"
        )

    async def noop_refresh() -> None:
        nonlocal refresh_calls
        refresh_calls += 1

    service.touch_consciousness_instance = contended_touch
    service.consciousness_registry.refresh = noop_refresh  # type: ignore[method-assign]

    coordinator = MemoryWitnessCoordinator(service)
    result = await coordinator.ensure_instance()

    assert result.instance_id == "memory_witness"
    assert touch_attempts == 3
    assert refresh_calls == 2
    assert service.consciousness_registry.get("memory_witness") is not None


@pytest.mark.asyncio
async def test_witness_ensure_instance_recovers_after_refresh() -> None:
    """A transient contention recovers once the snapshot catches up."""

    service, memory, event_store, _perception_commits = _service(timeout_seconds=10.0)
    witness = _witness_instance()
    service.consciousness_registry.register(witness)

    touch_attempts = 0

    async def contended_then_ok(_instance_id: str, **kwargs: Any) -> None:
        nonlocal touch_attempts
        touch_attempts += 1
        if touch_attempts < 2:
            raise PresenceRevisionConflict(
                "presence revision conflict for 'memory_witness': "
                "expected 1637, actual 1638"
            )

    async def noop_refresh() -> None:
        return None

    service.touch_consciousness_instance = contended_then_ok
    service.consciousness_registry.refresh = noop_refresh  # type: ignore[method-assign]

    coordinator = MemoryWitnessCoordinator(service)
    result = await coordinator.ensure_instance()

    assert result.instance_id == "memory_witness"
    assert touch_attempts == 2


@pytest.mark.asyncio
async def test_witness_ensure_instance_registers_when_absent() -> None:
    """A fresh witness registers normally when no concurrent owner exists."""

    service, memory, event_store, _perception_commits = _service(timeout_seconds=10.0)
    coordinator = MemoryWitnessCoordinator(service)
    result = await coordinator.ensure_instance()

    assert result.instance_id == "memory_witness"
    assert service.consciousness_registry.get("memory_witness") is not None
    assert result.status == "active"
