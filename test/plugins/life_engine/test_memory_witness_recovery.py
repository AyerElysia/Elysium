"""Recovery and fault-injection contracts for the staged Memory Witness worker."""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from plugins.life_engine.memory.experience import (
    EpistemicKind,
    ExperienceOccurrenceRef,
    ExperienceRecord,
    create_life_memory_schema,
)
from plugins.life_engine.service.consciousness import (
    ConsciousnessInstance,
    ConsciousnessRegistry,
)
from plugins.life_engine.service.event_bus import LifeEvent, RawEventGapError
from plugins.life_engine.service.memory_witness import (
    MEMORY_EXPERIENCE_CONSUMER_ID,
    MEMORY_WITNESS_INSTANCE_ID,
    MemoryExperienceRawLedgerGap,
    MemoryWitnessAuthoringEmptyResponse,
    MemoryWitnessCoordinator,
    MemoryWitnessCrossProcessClaimUnavailable,
    MemoryWitnessOccurrencePaginationUnavailable,
    MemoryWitnessProjectionFilesystemChanged,
    MemoryWitnessWindowTooLarge,
    _AuthoringResult,
)
from plugins.life_engine.service.perception_gateway import (
    PerceptionDeliveryReceipt,
)
from plugins.life_engine.storage.memory.local import (
    create_local_memory_storage_bundle,
)
from plugins.life_engine.storage.writer_claims import (
    SingletonWriterClaimConflict,
    SingletonWriterClaimLost,
)
from src.kernel.llm.exceptions import LLMAPIError
from src.kernel.storage import CursorConflict, canonical_json


def _now() -> str:
    return datetime.now(UTC).astimezone().isoformat()


def _event(
    position: int,
    *,
    content: str | None = None,
    event_id: str | None = None,
    occurrence_id: str | None = None,
    timestamp: str | None = None,
    source: str = "chat",
    channel: str = "chat",
    event_type: str = "text",
    source_instance_id: str = "chat_global",
) -> LifeEvent:
    return LifeEvent(
        event_id=event_id or f"source-{position}",
        sequence=position,
        timestamp=timestamp or f"2026-08-12T08:00:{position:02d}+08:00",
        source=source,
        channel=channel,
        event_type=event_type,
        content=content or f"experience-{position}",
        stream_id="stream-shared",
        occurrence_id=occurrence_id or f"occ-{position}",
        source_instance_id=source_instance_id,
    )


class _RawStore:
    def __init__(self, events: list[LifeEvent], *, gap: bool = False) -> None:
        self.events = sorted(events, key=lambda item: item.sequence)
        self.gap = gap
        self.offsets: dict[str, int] = {}
        self.commits: list[tuple[str, int, dict[str, Any]]] = []

    async def get_consumer_offset(self, consumer_id: str) -> int:
        return self.offsets.get(consumer_id, 0)

    async def read_since(self, position: int, *, limit: int) -> list[LifeEvent]:
        if self.gap:
            raise RawEventGapError(position, position + 10)
        return [item for item in self.events if item.sequence > position][:limit]

    async def commit_consumer_offset(
        self,
        consumer_id: str,
        position: int,
        *,
        metadata: dict[str, Any],
    ) -> int:
        committed = max(self.offsets.get(consumer_id, 0), int(position))
        self.offsets[consumer_id] = committed
        self.commits.append((consumer_id, committed, dict(metadata)))
        return committed

    async def health(self) -> dict[str, int]:
        positions = [item.sequence for item in self.events]
        return {
            "earliest_position": min(positions, default=0),
            "latest_position": max(positions, default=0),
        }


class _Memory:
    def __init__(self, db: sqlite3.Connection) -> None:
        self.bundle = create_local_memory_storage_bundle(lambda: db)
        self.document_upserts: list[str] = []

    def _require_memory_storage(self) -> Any:
        return self.bundle

    async def upsert_document(
        self,
        path: str,
        _body: str,
        *,
        title: str,
        source_mtime: float | None,
    ) -> None:
        del title, source_mtime
        self.document_upserts.append(path)

    async def mark_witness_projection(
        self,
        witness_id: str,
        *,
        projection_path: str,
        status: str,
        error: str = "",
    ) -> bool:
        return await self.bundle.witnesses.mark_projection(
            witness_id,
            projection_path=projection_path,
            status=status,
            error=error,
        )

    async def update_witness_state(
        self,
        instance_id: str,
        *,
        last_run_at: str | None = None,
        last_error: str | None = None,
    ) -> dict[str, Any]:
        state = await self.bundle.witnesses.get_state(instance_id)
        return await self.bundle.witnesses.compare_and_advance_state(
            instance_id,
            expected_sequence=int(state.get("last_sequence", 0)),
            expected_revision=int(state.get("revision", 0)),
            next_sequence=int(state.get("last_sequence", 0)),
            last_run_at=last_run_at,
            last_error=last_error,
        )


def _subject_snapshot() -> dict[str, Any]:
    source_digest = "c" * 64
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


class _Service:
    def __init__(
        self,
        tmp_path: Path,
        events: list[LifeEvent],
        *,
        gap: bool = False,
        **config_overrides: Any,
    ) -> None:
        self.db = sqlite3.connect(":memory:", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        create_life_memory_schema(self.db)
        self.memory_service = _Memory(self.db)
        self.raw_store = _RawStore(events, gap=gap)
        self.consciousness_registry = ConsciousnessRegistry()
        self.selected_subject_storage_enabled = False
        self.storage_runtime: Any | None = None
        self.world_commits: list[tuple[Any, Any]] = []
        self.touch_error: Exception | None = None
        self._tmp_path = tmp_path
        defaults = {
            "enabled": True,
            "run_on_startup": True,
            "interval_seconds": 300,
            "retry_delay_seconds": 10,
            "max_events_per_run": 80,
            "max_ingest_batches_per_run": 8,
            "max_witness_events_per_run": 40,
            "max_witness_context_bytes": 64 * 1024,
            "delivery_batch_size": 50,
            "projection_reconcile_batch_size": 100,
            "model_task_name": "witness",
            "timeout_seconds": 30.0,
            "migrate_legacy_diaries": False,
        }
        defaults.update(config_overrides)
        self.config = SimpleNamespace(**defaults)
        world_content = "world-perception:witness-world\ncurrent world"
        self.perception = SimpleNamespace(
            instance_id=MEMORY_WITNESS_INSTANCE_ID,
            from_position=0,
            through_position=3,
            cursor_revision=4,
            delivery_id="witness-world",
            delivery_marker="world-perception:witness-world",
            content=world_content,
            projection_sha256=hashlib.sha256(world_content.encode("utf-8")).hexdigest(),
            delivered_bytes=len(world_content.encode("utf-8")),
        )

    def _cfg(self) -> Any:
        return SimpleNamespace(memory_witness=self.config)

    def _get_life_event_store(self) -> _RawStore:
        return self.raw_store

    def _workspace_dir(self) -> Path:
        return self._tmp_path

    async def register_consciousness_instance(
        self,
        instance: ConsciousnessInstance,
    ) -> ConsciousnessInstance:
        return self.consciousness_registry.register(instance)

    async def resume_consciousness_instance(
        self,
        instance_id: str,
        **kwargs: Any,
    ) -> bool:
        return self.consciousness_registry.resume(instance_id, **kwargs)

    async def touch_consciousness_instance(
        self,
        instance_id: str,
        **kwargs: Any,
    ) -> None:
        if self.touch_error is not None:
            raise self.touch_error
        self.consciousness_registry.touch(instance_id, **kwargs)

    async def prepare_perception(self, _instance_id: str) -> Any:
        return self.perception

    async def commit_perception_delivery(
        self,
        checkpoint: Any,
        receipt: Any,
    ) -> tuple[int, int]:
        self.world_commits.append((checkpoint, receipt))
        return (
            checkpoint.through_position,
            checkpoint.cursor_revision
            + int(checkpoint.through_position > checkpoint.from_position),
        )

    async def get_subject_context_projection_snapshot(
        self,
        **kwargs: Any,
    ) -> dict[str, Any]:
        assert kwargs == {
            "projection_kind": "memory_witness",
            "max_bytes": 24 * 1024,
        }
        return _subject_snapshot()


def _coordinator(
    tmp_path: Path,
    events: list[LifeEvent],
    **config_overrides: Any,
) -> tuple[MemoryWitnessCoordinator, _Service]:
    service = _Service(tmp_path, events, **config_overrides)
    return MemoryWitnessCoordinator(service), service


def _authored(
    service: _Service, text: str = "first-person witness"
) -> _AuthoringResult:
    receipt = PerceptionDeliveryReceipt(
        delivery_id=service.perception.delivery_id,
        projection_sha256=service.perception.projection_sha256,
        delivered_bytes=service.perception.delivered_bytes,
        exact=True,
        transport_request_id="request-1",
    )
    response = text or "<no_witness>"
    return _AuthoringResult(
        text=text,
        model_task_name="witness",
        model_request_id="request-1",
        response_sha256=hashlib.sha256(response.encode("utf-8")).hexdigest(),
        response_bytes=len(response.encode("utf-8")),
        world_payload=MemoryWitnessCoordinator._world_delivery_payload(
            service.perception,
            receipt,
        ),
    )


def _occurrence(
    position: int,
    *,
    content: str | None = None,
    occurrence_id: str | None = None,
) -> ExperienceOccurrenceRef:
    record = ExperienceRecord(
        event_id=occurrence_id or f"occ-{position}",
        source_event_id=f"source-{position}",
        sequence=position,
        occurred_at=f"2026-08-12T08:00:{position:02d}+08:00",
        recorded_at=f"2026-08-12T08:01:{position:02d}+08:00",
        source="chat",
        channel="chat",
        event_type="text",
        content=content or f"experience-{position}",
        stream_id="stream-shared",
        consciousness_instance_id="chat_global",
        actor="user",
        valid_from=f"2026-08-12T08:00:{position:02d}+08:00",
    )
    return ExperienceOccurrenceRef(
        occurrence_id=record.event_id,
        source_event_id=record.source_event_id,
        ingest_position=position,
        canonical_event_id=record.event_id,
        canonical_payload_sha256=hashlib.sha256(
            record.content.encode("utf-8")
        ).hexdigest(),
        recorded_at=record.recorded_at,
        experience=record,
    )


@pytest.mark.asyncio
async def test_raw_ingest_uses_occurrence_report_across_multiple_batches_and_aliases(
    tmp_path: Path,
) -> None:
    timestamp = "2026-08-12T08:00:00+08:00"
    events = [
        _event(
            1,
            event_id="same-source",
            occurrence_id="alias-occ-1",
            timestamp=timestamp,
            content="same evidence",
        ),
        _event(
            2,
            event_id="same-source",
            occurrence_id="alias-occ-2",
            timestamp=timestamp,
            content="same evidence",
        ),
    ]
    coordinator, service = _coordinator(
        tmp_path,
        events,
        max_events_per_run=1,
        max_ingest_batches_per_run=2,
    )
    legacy_canonical = ExperienceRecord(
        event_id="same-source",
        source_event_id="same-source",
        sequence=0,
        occurred_at=timestamp,
        recorded_at=_now(),
        source="chat",
        channel="chat",
        event_type="text",
        content="same evidence",
        stream_id="stream-shared",
        consciousness_instance_id="chat_global",
        actor="chat",
        visibility="private",
        valid_from=timestamp,
    )
    await service.memory_service.bundle.experiences.append((legacy_canonical,))

    report = await coordinator._ingest_raw_experiences(
        service.memory_service.bundle.experiences
    )

    occurrences = (
        await service.memory_service.bundle.experiences.list_occurrences_after(0, 10)
    )
    assert report.batches == 2
    assert report.inserted_count == 0
    assert report.occurrence_count == 2
    assert report.raw_cursor == 2
    assert [item.occurrence_id for item in occurrences] == [
        "alias-occ-1",
        "alias-occ-2",
    ]
    assert occurrences[1].is_alias is True
    assert service.raw_store.offsets[MEMORY_EXPERIENCE_CONSUMER_ID] == 2


@pytest.mark.asyncio
async def test_raw_gap_fails_closed_without_experience_or_cursor_advance(
    tmp_path: Path,
) -> None:
    coordinator, service = _coordinator(tmp_path, [_event(10)], gap=True)

    with pytest.raises(MemoryExperienceRawLedgerGap):
        await coordinator._ingest_raw_experiences(
            service.memory_service.bundle.experiences
        )

    health = await service.memory_service.bundle.experiences.health_snapshot()
    assert health["occurrence_count"] == 0
    assert service.raw_store.offsets == {}


@pytest.mark.asyncio
async def test_experience_commit_survives_llm_failure_and_window_stays_pending(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator, service = _coordinator(tmp_path, [_event(1), _event(2)])

    async def fail_author(*_args: Any) -> _AuthoringResult:
        raise LLMAPIError("upstream", status_code=500)

    monkeypatch.setattr(coordinator, "_author_witness", fail_author)

    with pytest.raises(LLMAPIError):
        await coordinator.run_once()

    experience_health = (
        await service.memory_service.bundle.experiences.health_snapshot()
    )
    state = await service.memory_service.bundle.witnesses.get_state(
        MEMORY_WITNESS_INSTANCE_ID
    )
    pending = await service.memory_service.bundle.witnesses.next_pending_window(
        MEMORY_WITNESS_INSTANCE_ID
    )
    assert experience_health["occurrence_count"] == 2
    assert service.raw_store.offsets[MEMORY_EXPERIENCE_CONSUMER_ID] == 2
    assert state["last_sequence"] == 0
    assert pending is not None
    assert pending.start_position == 1
    assert pending.end_position == 2

    health = await coordinator.health_snapshot()
    pending_health = health["author"]["pending_windows"]
    assert pending_health["count"] == 1
    assert pending_health["exact"] is False
    assert pending_health["head"]["start_position"] == 1
    assert pending_health["head"]["end_position"] == 2
    assert pending_health["head"]["occurrence_count"] == 2
    assert len(pending_health["head"]["window_id_sha256"]) == 64
    assert pending.window_id not in str(health)


@pytest.mark.asyncio
async def test_decision_and_cursor_survive_projection_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator, service = _coordinator(tmp_path, [_event(1)])
    author_calls = 0

    async def author(*_args: Any) -> _AuthoringResult:
        nonlocal author_calls
        author_calls += 1
        return _authored(service)

    async def fail_projection(_witness: Any) -> None:
        raise OSError("projection unavailable")

    monkeypatch.setattr(coordinator, "_author_witness", author)
    await coordinator.run_once()
    monkeypatch.setattr(coordinator, "_project_witness", fail_projection)

    report = await coordinator.run_once()

    state = await service.memory_service.bundle.witnesses.get_state(
        MEMORY_WITNESS_INSTANCE_ID
    )
    jobs = await service.memory_service.bundle.witnesses.list_delivery_jobs(
        delivery_kind="projection",
        statuses=(),
        limit=10,
    )
    assert report.deliveries_failed == 1
    assert state["last_sequence"] == 1
    assert author_calls == 1
    assert jobs[0].status == "failed"
    assert jobs[0].last_error_type == "OSError"

    health = await coordinator.health_snapshot()
    assert health["raw_ingest"]["consumer_id"] == MEMORY_EXPERIENCE_CONSUMER_ID
    assert health["raw_ingest"]["cursor"] == 1
    assert health["raw_ingest"]["frontier"] == 1
    assert health["raw_ingest"]["backlog"] == 0
    assert health["author"]["cursor"] == 1
    assert health["author"]["frontier"] == 1
    assert health["author"]["backlog"] == 0
    assert health["author"]["pending_windows"] == {
        "count": 0,
        "exact": True,
        "head": {},
    }
    assert health["outbox"]["counts"]["projection"]["failed"] == 1
    assert health["projection"]["actionable"] == 1
    assert health["runtime"]["last_error_type"] == "OSError"
    continuity = health["continuity_delivery_verifier"]
    assert continuity["status"] == "healthy"
    assert continuity["pending_pages"] >= 0
    assert continuity["committed_pages"] >= 0
    assert continuity["candidate_coverages"] >= 0
    assert set(continuity["limits"]) == {"max_pending", "max_committed_pages"}
    assert health["legacy_writer"] == {
        "retired": True,
        "writes_enabled": False,
        "authoritative": False,
        "replacement": "staged_experience_witness_pipeline",
    }
    assert "projection unavailable" not in str(health)

    secret = "private error and experience body"
    current = await service.memory_service.bundle.witnesses.get_state(
        MEMORY_WITNESS_INSTANCE_ID
    )
    await service.memory_service.bundle.witnesses.compare_and_advance_state(
        MEMORY_WITNESS_INSTANCE_ID,
        expected_sequence=int(current["last_sequence"]),
        expected_revision=int(current["revision"]),
        next_sequence=int(current["last_sequence"]),
        last_error=secret,
    )

    async def polluted_experience_health() -> dict[str, Any]:
        return {
            "status": "healthy",
            "canonical_count": 1,
            "alias_count": 0,
            "occurrence_count": 1,
            "frontier": 1,
            "content": secret,
        }

    monkeypatch.setattr(
        service.memory_service.bundle.experiences,
        "health_snapshot",
        polluted_experience_health,
    )
    sanitized = await coordinator.health_snapshot()
    assert sanitized["runtime"]["last_error_type"] == "RecordedWitnessError"
    assert "content" not in sanitized["experience"]
    assert secret not in str(sanitized)


@pytest.mark.asyncio
async def test_no_witness_decision_is_restart_safe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator, service = _coordinator(tmp_path, [_event(1)])

    async def no_witness(*_args: Any) -> _AuthoringResult:
        return _authored(service, "")

    monkeypatch.setattr(coordinator, "_author_witness", no_witness)
    await coordinator.run_once()
    restarted = MemoryWitnessCoordinator(service)

    async def must_not_author(*_args: Any) -> _AuthoringResult:
        raise AssertionError("durable no_witness decision must not be re-authored")

    monkeypatch.setattr(restarted, "_author_witness", must_not_author)
    await restarted.run_once()

    window_id = MemoryWitnessCoordinator._window_id(
        MEMORY_WITNESS_INSTANCE_ID,
        (await service.memory_service.bundle.experiences.list_occurrences_after(0, 1))[
            0
        ],
    )
    decision = await service.memory_service.bundle.witnesses.get_decision(
        MemoryWitnessCoordinator._decision_id(window_id)
    )
    assert decision is not None
    assert decision.decision_kind == "no_witness"
    assert (
        await service.memory_service.bundle.witnesses.get_state(
            MEMORY_WITNESS_INSTANCE_ID
        )
    )["last_sequence"] == 1


@pytest.mark.asyncio
async def test_expired_processing_lease_is_recovered_by_cas(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator, service = _coordinator(tmp_path, [_event(1)])

    async def author(*_args: Any) -> _AuthoringResult:
        return _authored(service)

    monkeypatch.setattr(coordinator, "_author_witness", author)
    await coordinator.run_once()
    store = service.memory_service.bundle.witnesses
    projection = (
        await store.list_delivery_jobs(
            delivery_kind="projection", statuses=("pending",), limit=1
        )
    )[0]
    processing = await store.mark_delivery_job(
        projection.job_id,
        expected_revision=projection.revision,
        status="processing",
        lease_owner="crashed-worker",
        lease_expires_at="2000-01-01T00:00:00+00:00",
    )

    report = await coordinator._run_delivery_worker(store)
    recovered = (
        await store.list_delivery_jobs(delivery_kind="projection", statuses=(), limit=1)
    )[0]
    assert processing.status == "processing"
    assert report.succeeded >= 1
    assert recovered.status == "succeeded"
    assert recovered.attempt_count >= 2


@pytest.mark.asyncio
async def test_world_and_projection_workers_have_independent_bounded_progress(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator, service = _coordinator(
        tmp_path,
        [_event(1)],
        delivery_batch_size=1,
    )

    async def author(*_args: Any) -> _AuthoringResult:
        return _authored(service)

    monkeypatch.setattr(coordinator, "_author_witness", author)
    await coordinator.run_once()
    store = service.memory_service.bundle.witnesses

    report = await coordinator._run_delivery_worker(store)
    world = await store.list_delivery_jobs(
        delivery_kind="world",
        statuses=(),
        limit=10,
    )
    projection = await store.list_delivery_jobs(
        delivery_kind="projection",
        statuses=(),
        limit=10,
    )
    assert report == type(report)(processed=2, succeeded=2, failed=0)
    assert [job.status for job in world] == ["succeeded"]
    assert [job.status for job in projection] == ["succeeded"]
    assert len(service.world_commits) == 1


@pytest.mark.asyncio
async def test_delivery_worker_pages_past_future_due_head_jobs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator, service = _coordinator(
        tmp_path,
        [_event(1)],
        delivery_batch_size=1,
    )

    async def author(*_args: Any) -> _AuthoringResult:
        return _authored(service)

    monkeypatch.setattr(coordinator, "_author_witness", author)
    await coordinator.run_once()
    store = service.memory_service.bundle.witnesses
    due = (
        await store.list_delivery_jobs(
            delivery_kind="projection",
            statuses=("pending",),
            limit=1,
        )
    )[0]
    for index in range(5):
        window_id = f"future-window-{index}"
        decision_id = f"future-decision-{index}"
        job_id = f"future-job-{index}"
        created_at = f"2000-01-01T00:00:0{index}+00:00"
        payload = {
            "projection_path": f"diaries/witness/future-{index}.md",
            "witness_id": f"future-witness-{index}",
        }
        payload_json = canonical_json(payload)
        payload_sha256 = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        service.db.execute(
            """INSERT INTO memory_witness_windows (
                window_id, consciousness_instance_id, stream_scope,
                start_position, end_position, occurrence_count, source_digest,
                planner_version, created_at, metadata_json, payload_sha256
            ) VALUES (?, ?, '', 1, 1, 0, ?, ?, ?, '{}', ?)""",
            (
                window_id,
                MEMORY_WITNESS_INSTANCE_ID,
                "a" * 64,
                "test",
                created_at,
                "b" * 64,
            ),
        )
        service.db.execute(
            """INSERT INTO memory_witness_decisions (
                decision_id, window_id, consciousness_instance_id,
                decision_kind, witness_id, model_task_name, model_request_id,
                response_sha256, delivery_manifest_sha256, decided_at,
                metadata_json, payload_sha256
            ) VALUES (?, ?, ?, 'witness', ?, 'test', 'request', ?, ?, ?, '{}', ?)""",
            (
                decision_id,
                window_id,
                MEMORY_WITNESS_INSTANCE_ID,
                f"future-witness-{index}",
                "c" * 64,
                "d" * 64,
                created_at,
                "e" * 64,
            ),
        )
        service.db.execute(
            """INSERT INTO memory_witness_delivery_jobs (
                job_id, decision_id, window_id, delivery_kind, payload_json,
                payload_sha256, created_at, status, revision, attempt_count,
                available_at, lease_owner, lease_expires_at, last_error_type,
                updated_at, completed_at
            ) VALUES (?, ?, ?, 'projection', ?, ?, ?, 'pending', 0, 0,
                '2999-01-01T00:00:00+00:00', '', '', '', ?, '')""",
            (
                job_id,
                decision_id,
                window_id,
                payload_json,
                payload_sha256,
                created_at,
                created_at,
            ),
        )
    service.db.commit()

    report = await coordinator._run_delivery_kind(store, "projection", limit=1)
    persisted = (
        await store.list_delivery_jobs(
            delivery_kind="projection",
            statuses=(),
            limit=20,
        )
    )
    by_id = {job.job_id: job for job in persisted}
    assert report.processed == 1
    assert report.succeeded == 1
    assert by_id[due.job_id].status == "succeeded"
    assert all(by_id[f"future-job-{index}"].status == "pending" for index in range(5))


@pytest.mark.asyncio
async def test_world_receipt_mismatch_fails_without_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator, service = _coordinator(tmp_path, [_event(1)])
    authored = _authored(service)
    broken_payload = {
        **authored.world_payload,
        "receipt": {
            **authored.world_payload["receipt"],
            "projection_sha256": "d" * 64,
        },
    }
    broken = replace(authored, world_payload=broken_payload)

    async def author(*_args: Any) -> _AuthoringResult:
        return broken

    monkeypatch.setattr(coordinator, "_author_witness", author)
    await coordinator.run_once()
    report = await coordinator.run_once()
    world_jobs = await service.memory_service.bundle.witnesses.list_delivery_jobs(
        delivery_kind="world", statuses=(), limit=10
    )
    assert report.deliveries_failed == 1
    assert service.world_commits == []
    assert world_jobs[0].status == "failed"
    assert world_jobs[0].last_error_type == "MemoryWitnessDeliveryPayloadInvalid"


@pytest.mark.asyncio
async def test_missing_projection_is_rebuilt_and_orphan_is_diagnostic_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator, service = _coordinator(tmp_path, [_event(1)])

    async def author(*_args: Any) -> _AuthoringResult:
        return _authored(service)

    monkeypatch.setattr(coordinator, "_author_witness", author)
    await coordinator.run_once()
    await coordinator.run_once()
    projection_jobs = (
        await service.memory_service.bundle.witnesses.list_projection_records(
            statuses=("succeeded",), limit=10
        )
    )
    path = tmp_path / str(projection_jobs[0].payload["projection_path"])
    assert path.is_file()
    path.unlink()
    orphan = tmp_path / "diaries" / "witness" / "orphan.md"
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_text("orphan projection", encoding="utf-8")

    report = await coordinator._reconcile_projections(
        service.memory_service.bundle.witnesses
    )

    assert report.rebuilt == 1
    assert report.orphan == 1
    assert path.is_file()
    assert orphan.is_file()

    coordinator._last_reconciliation = report
    health = await coordinator.health_snapshot()
    projection = health["projection"]
    assert projection["missing"] == 0
    assert projection["orphan"] == 1
    assert projection["rebuilt"] == 1
    assert projection["ledger_rotation_cursor_scope"] == "durable_store"
    assert set(projection["ledger_rotation_cursors"]) == {
        "legacy_pending",
        "completed_projection",
        "projection_filesystem",
    }
    for scan in projection["ledger_rotation_cursors"].values():
        assert scan["revision"] >= 1
        assert set(scan) == {
            "revision",
            "cursor",
            "frontier",
            "cycle_started_at",
            "last_completed_at",
            "updated_at",
        }
    assert projection["filesystem_cursor_scope"] == "durable_store"
    assert set(projection["filesystem_cursor"]) == {
        "present",
        "order_value",
        "order_value_sha256",
        "identity_sha256",
    }
    assert projection["filesystem_scan_blocker"] == ""
    assert projection["legacy_no_path"]["actionable"] is False
    assert "orphan projection" not in str(health)
    assert "orphan.md" not in str(health)


@pytest.mark.asyncio
async def test_legacy_pending_without_projection_path_is_not_actionable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator, service = _coordinator(tmp_path, [])
    experience = ExperienceRecord(
        event_id="legacy-source",
        sequence=1,
        occurred_at=_now(),
        recorded_at=_now(),
        source="legacy",
        channel="internal",
        event_type="legacy",
        content="legacy content",
    )
    await service.memory_service.bundle.experiences.append((experience,))
    await service.memory_service.bundle.witnesses.append(
        content="legacy witness",
        consciousness_instance_id=MEMORY_WITNESS_INSTANCE_ID,
        perspective_subject_id="elysia",
        epistemic_kind=EpistemicKind.LEGACY_WITNESS.value,
        source_kind="legacy_diary",
        stream_scope="",
        visibility="private",
        valid_from=experience.occurred_at,
        valid_to=experience.occurred_at,
        source_event_ids=(experience.event_id,),
        projection_path="",
    )

    async def must_not_project(_witness: Any) -> None:
        raise AssertionError("empty legacy projection path is not actionable")

    monkeypatch.setattr(coordinator, "_project_witness", must_not_project)
    report = await coordinator._reconcile_projections(
        service.memory_service.bundle.witnesses
    )
    assert report.legacy_actionable == 0
    assert report.legacy_failed == 0


@pytest.mark.asyncio
async def test_decision_survives_cursor_cas_and_restart_recovers_without_reauthor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator, service = _coordinator(tmp_path, [_event(1)])
    store = service.memory_service.bundle.witnesses
    author_calls = 0

    async def author(*_args: Any) -> _AuthoringResult:
        nonlocal author_calls
        author_calls += 1
        return _authored(service)

    original_advance = store.compare_and_advance_state
    fail_once = True

    async def conflicted_advance(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal fail_once
        if fail_once and int(kwargs["next_sequence"]) > 0:
            fail_once = False
            raise CursorConflict("injected cursor CAS")
        return await original_advance(*args, **kwargs)

    monkeypatch.setattr(coordinator, "_author_witness", author)
    monkeypatch.setattr(store, "compare_and_advance_state", conflicted_advance)
    with pytest.raises(CursorConflict):
        await coordinator.run_once()
    assert (await store.get_state(MEMORY_WITNESS_INSTANCE_ID))["last_sequence"] == 0

    await coordinator.run_once()
    assert author_calls == 1
    assert (await store.get_state(MEMORY_WITNESS_INSTANCE_ID))["last_sequence"] == 1


@pytest.mark.asyncio
async def test_world_commit_cannot_precede_atomic_decision_and_crash_recovers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator, service = _coordinator(tmp_path, [_event(1)])
    store = service.memory_service.bundle.witnesses
    author_calls = 0

    async def author(*_args: Any) -> _AuthoringResult:
        nonlocal author_calls
        author_calls += 1
        return _authored(service)

    original_append_decision = store.append_decision

    async def crash_before_decision(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("injected decision crash")

    monkeypatch.setattr(coordinator, "_author_witness", author)
    monkeypatch.setattr(store, "append_decision", crash_before_decision)
    with pytest.raises(RuntimeError, match="decision crash"):
        await coordinator.run_once()
    assert service.world_commits == []
    assert (await store.get_state(MEMORY_WITNESS_INSTANCE_ID))["last_sequence"] == 0

    monkeypatch.setattr(store, "append_decision", original_append_decision)
    await coordinator.run_once()
    assert author_calls == 1
    assert service.world_commits == []
    await coordinator.run_once()
    assert len(service.world_commits) == 1


def test_multi_occurrence_window_counts_each_item_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator, _service = _coordinator(tmp_path, [])
    occurrences = (_occurrence(1), _occurrence(2), _occurrence(3))
    calls: list[str] = []
    original = coordinator._format_occurrence

    def tracked(item: ExperienceOccurrenceRef) -> str:
        calls.append(item.occurrence_id)
        return original(item)

    monkeypatch.setattr(coordinator, "_format_occurrence", tracked)
    window = coordinator._plan_window(MEMORY_WITNESS_INSTANCE_ID, occurrences)
    expected = "\n\n".join(original(item) for item in occurrences)
    assert calls == ["occ-1", "occ-2", "occ-3"]
    assert window.metadata["delivered_utf8_bytes"] == len(expected.encode("utf-8"))


@pytest.mark.asyncio
async def test_oversized_first_occurrence_fails_closed_and_is_health_visible(
    tmp_path: Path,
) -> None:
    coordinator, _service = _coordinator(
        tmp_path,
        [],
        max_witness_context_bytes=64,
    )
    secret_content = "private oversized evidence" * 20
    occurrence = _occurrence(1, content=secret_content)

    with pytest.raises(MemoryWitnessWindowTooLarge) as exc_info:
        coordinator._plan_window(MEMORY_WITNESS_INSTANCE_ID, (occurrence,))

    snapshot = await coordinator.health_snapshot()
    oversized = snapshot["author"]["oversized_window"]
    assert oversized["status"] == "blocked"
    assert oversized["position"] == 1
    assert oversized["cursor_advanced"] is False
    assert oversized["content_truncated"] is False
    assert secret_content not in str(exc_info.value)
    assert secret_content not in str(snapshot)


@pytest.mark.asyncio
async def test_unpageable_occurrence_frontier_fails_closed_and_is_health_visible(
    tmp_path: Path,
) -> None:
    coordinator, _service = _coordinator(tmp_path, [])
    occurrences = tuple(
        _occurrence(1, occurrence_id=f"alias-{index:04d}") for index in range(1000)
    )

    class _ExperienceStore:
        async def list_occurrences_after(
            self,
            position: int,
            limit: int,
        ) -> tuple[ExperienceOccurrenceRef, ...]:
            assert position == 0
            assert limit == 1000
            return occurrences

    class _WitnessStore:
        async def get_state(self, instance_id: str) -> dict[str, Any]:
            assert instance_id == MEMORY_WITNESS_INSTANCE_ID
            return {"last_sequence": 0, "revision": 0}

        async def get_window(self, _window_id: str) -> None:
            return None

    instance = ConsciousnessInstance(
        instance_id=MEMORY_WITNESS_INSTANCE_ID,
        kind="memory_witness",
    )
    with pytest.raises(MemoryWitnessOccurrencePaginationUnavailable) as exc_info:
        await coordinator._next_authoring_window(
            instance,
            _ExperienceStore(),
            _WitnessStore(),
        )

    health = await coordinator.health_snapshot()
    blocker = health["author"]["occurrence_pagination_blocker"]
    assert blocker["status"] == "blocked"
    assert blocker["returned_occurrences"] == 1000
    assert blocker["first_position"] == 1
    assert blocker["last_position"] == 1
    assert blocker["cursor_advanced"] is False
    assert "alias-" not in str(blocker)
    assert "alias-" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_capped_lookahead_can_plan_a_proven_complete_prefix(
    tmp_path: Path,
) -> None:
    coordinator, _service = _coordinator(tmp_path, [])
    occurrences = tuple(_occurrence(position) for position in range(1, 1001))
    appended: list[Any] = []

    class _ExperienceStore:
        async def list_occurrences_after(
            self,
            position: int,
            limit: int,
        ) -> tuple[ExperienceOccurrenceRef, ...]:
            assert position == 0
            assert limit == 1000
            return occurrences

    class _WitnessStore:
        async def get_state(self, _instance_id: str) -> dict[str, Any]:
            return {"last_sequence": 0, "revision": 0}

        async def get_window(self, _window_id: str) -> None:
            return None

        async def append_window(self, window: Any) -> Any:
            appended.append(window)
            return window

        async def get_decision(self, _decision_id: str) -> None:
            return None

    instance = ConsciousnessInstance(
        instance_id=MEMORY_WITNESS_INSTANCE_ID,
        kind="memory_witness",
    )
    window, state = await coordinator._next_authoring_window(
        instance,
        _ExperienceStore(),
        _WitnessStore(),
    )
    assert state["last_sequence"] == 0
    assert window is not None
    assert window.start_position == 1
    assert window.end_position == 40
    assert len(window.occurrences) == 40
    assert appended == [window]
    assert coordinator._occurrence_pagination_blocker == {}


@pytest.mark.asyncio
async def test_selected_runtime_uses_durable_singleton_claim(
    tmp_path: Path,
) -> None:
    coordinator, service = _coordinator(tmp_path, [])
    calls: list[str] = []
    claim = SimpleNamespace(lease_epoch=1, lease_until=_now())

    class _Runtime:
        enabled = True
        authority_token = SimpleNamespace(owner_id="test-owner")

        async def validate_writer(self) -> None:
            calls.append("validate")

        async def acquire_singleton_writer(self, **kwargs: Any) -> Any:
            assert kwargs["namespace"] == "life_engine.memory_witness"
            calls.append("acquire")
            return claim

        async def renew_singleton_writer(
            self,
            current: Any,
            **_kwargs: Any,
        ) -> Any:
            assert current is claim
            calls.append("renew")
            return claim

    service.storage_runtime = _Runtime()
    await coordinator._ensure_authoring_claim()
    await coordinator._ensure_authoring_claim()
    snapshot = await coordinator.health_snapshot()
    claim_health = snapshot["author"]["cross_process_window_claim"]
    assert calls == ["validate", "acquire", "validate", "renew"]
    assert claim_health["status"] == "durable_singleton_claim"
    assert claim_health["cross_process_safe"] is True
    assert "owner" not in claim_health


@pytest.mark.asyncio
async def test_local_author_lock_is_reported_as_process_only(
    tmp_path: Path,
) -> None:
    coordinator, _service = _coordinator(tmp_path, [])
    await coordinator._ensure_authoring_claim()
    snapshot = await coordinator.health_snapshot()
    claim_health = snapshot["author"]["cross_process_window_claim"]
    assert claim_health["status"] == "process_lock_only"
    assert claim_health["cross_process_safe"] is False
    assert claim_health["reason"] == "StorageRuntimeHasNoDurableSingletonClaim"


@pytest.mark.asyncio
async def test_selected_runtime_without_claim_fails_closed(
    tmp_path: Path,
) -> None:
    coordinator, service = _coordinator(tmp_path, [])
    service.storage_runtime = SimpleNamespace(enabled=True)
    with pytest.raises(MemoryWitnessCrossProcessClaimUnavailable):
        await coordinator._ensure_authoring_claim()
    snapshot = await coordinator.health_snapshot()
    claim_health = snapshot["author"]["cross_process_window_claim"]
    assert snapshot["status"] == "degraded"
    assert claim_health["status"] == "selected_runtime_claim_unavailable"
    assert claim_health["cross_process_safe"] is False


def test_projection_pages_rotate_instead_of_repeating_head() -> None:
    items = tuple(SimpleNamespace(job_id=f"job-{index}") for index in range(5))
    cursor = ""
    pages: list[tuple[str, ...]] = []
    for _ in range(3):
        page, cursor = MemoryWitnessCoordinator._rotating_page(
            items,
            key=lambda item: item.job_id,
            after=cursor,
            limit=2,
        )
        pages.append(tuple(item.job_id for item in page))
    assert pages == [
        ("job-0", "job-1"),
        ("job-2", "job-3"),
        ("job-4",),
    ]


@pytest.mark.asyncio
async def test_unpageable_projection_ledger_reports_explicit_blocker(
    tmp_path: Path,
) -> None:
    coordinator, _service = _coordinator(
        tmp_path,
        [],
        projection_reconcile_batch_size=2,
    )
    candidates = [
        SimpleNamespace(
            witness_id=f"legacy-{index:04d}",
            metadata={"pipeline_version": "memory-witness-decision-v1"},
            projection_path=f"path-{index}.md",
        )
        for index in range(1000)
    ]

    class _Store:
        async def list_pending(self, *, limit: int) -> list[Any]:
            assert limit == 1000
            return candidates

        async def list_projection_records(
            self,
            *,
            statuses: tuple[str, ...],
            limit: int,
        ) -> list[Any]:
            assert statuses == ("succeeded",)
            assert limit == 1000
            return []

        async def get_by_projection_path(self, _path: str) -> None:
            return None

    report = await coordinator._reconcile_projections(_Store())
    assert report.ledger_scan_complete is False
    assert report.ledger_scan_blocker == ("WitnessLedgerStoreNoProjectionPagination")
    assert report.filesystem_scan_blocker == (
        "WitnessProjectionFilesystemReconciliationStateUnavailable"
    )
    coordinator._last_reconciliation = report
    health = await coordinator.health_snapshot()
    assert health["status"] == "degraded"
    assert health["projection"]["ledger_scan_complete"] is False
    assert health["projection"]["ledger_scan_blocker"] == (
        "WitnessLedgerStoreNoProjectionPagination"
    )
    assert health["projection"]["filesystem_scan_blocker"] == (
        "WitnessProjectionFilesystemReconciliationStateUnavailable"
    )


@pytest.mark.asyncio
async def test_filesystem_projection_scan_resumes_from_durable_frontier(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator, service = _coordinator(
        tmp_path,
        [],
        projection_reconcile_batch_size=2,
    )
    root = tmp_path / "diaries" / "witness"
    root.mkdir(parents=True)
    expected = [f"projection-{index}.md" for index in range(5)]
    for name in expected:
        (root / name).write_text("private projection body", encoding="utf-8")

    store = service.memory_service.bundle.witnesses
    observed: list[str] = []
    get_by_projection_path = store.get_by_projection_path

    async def observe(path: str) -> Any:
        observed.append(path)
        return await get_by_projection_path(path)

    monkeypatch.setattr(store, "get_by_projection_path", observe)
    first = await coordinator._reconcile_projections(store)
    first_state = await store.get_reconciliation_state(
        "projection_filesystem:v1"
    )
    assert first.filesystem_scan_truncated is True
    assert first_state.cursor is not None
    assert first_state.frontier is not None

    restarted = MemoryWitnessCoordinator(service)
    second = await restarted._reconcile_projections(store)
    second_state = await store.get_reconciliation_state(
        "projection_filesystem:v1"
    )
    assert second.filesystem_scan_truncated is True
    assert second_state.cursor is not None
    assert second_state.cursor != first_state.cursor

    completed = await MemoryWitnessCoordinator(service)._reconcile_projections(store)
    completed_state = await store.get_reconciliation_state(
        "projection_filesystem:v1"
    )
    assert completed.filesystem_scan_truncated is False
    assert completed_state.cursor is None
    assert completed_state.frontier is None
    assert len(observed) == 5
    assert {Path(path).name for path in observed} == set(expected)
    persisted = service.db.execute(
        """SELECT cursor_order_value, cursor_identity,
        frontier_order_value, frontier_identity
        FROM memory_witness_reconciliation_state
        WHERE scan_name = 'projection_filesystem:v1'"""
    ).fetchone()
    assert "private projection body" not in str(tuple(persisted))
    assert not any(name in str(tuple(persisted)) for name in expected)


@pytest.mark.asyncio
async def test_filesystem_projection_scan_source_change_fails_closed(
    tmp_path: Path,
) -> None:
    coordinator, service = _coordinator(
        tmp_path,
        [],
        projection_reconcile_batch_size=2,
    )
    root = tmp_path / "diaries" / "witness"
    root.mkdir(parents=True)
    for index in range(5):
        (root / f"projection-{index}.md").write_text(
            "private projection body",
            encoding="utf-8",
        )
    store = service.memory_service.bundle.witnesses
    await coordinator._reconcile_projections(store)
    before = await store.get_reconciliation_state("projection_filesystem:v1")
    assert before.cursor is not None

    (root / "projection-4.md").write_text(
        "rewritten private projection body",
        encoding="utf-8",
    )
    # A mid-scan source rewrite must fail closed: the scan cursor is not
    # advanced and the whole reconciliation is not aborted.  It is surfaced as
    # a bounded filesystem-scan blocker and retried next cycle, without
    # spamming a fatal ERROR/traceback through the managed worker loop.
    report = await MemoryWitnessCoordinator(service)._reconcile_projections(store)
    after = await store.get_reconciliation_state("projection_filesystem:v1")
    assert report.filesystem_scan_blocker == (
        "WitnessProjectionFilesystemSourceChangedRetryNextCycle"
    )
    assert after == before  # cursor never advanced over an unstable scan


@pytest.mark.asyncio
async def test_author_preserves_markdown_and_only_exact_no_witness_is_empty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator, service = _coordinator(tmp_path, [])
    instance = ConsciousnessInstance(
        instance_id=MEMORY_WITNESS_INSTANCE_ID,
        kind="memory_witness",
    )

    async def invoke(response_text: str) -> _AuthoringResult:
        class _Response:
            message = response_text
            request_record_id = "request-preserve"

            def __await__(self):
                async def _done() -> str:
                    return self.message

                return _done().__await__()

            def effective_context_receipt(self, _delivery_id: str) -> Any:
                return SimpleNamespace(
                    delivery_id=service.perception.delivery_id,
                    exact_present=True,
                    expected_utf8_bytes=service.perception.delivered_bytes,
                    effective_utf8_bytes=service.perception.delivered_bytes,
                    expected_sha256=service.perception.projection_sha256,
                    effective_sha256=service.perception.projection_sha256,
                    part_kind="text",
                )

        class _Request:
            def __init__(self, *_args: Any, **_kwargs: Any) -> None:
                return None

            def add_payload(self, _payload: Any) -> None:
                return None

            def register_context_delivery(self, *_args: Any, **_kwargs: Any) -> None:
                return None

            async def send(self, *, stream: bool = True) -> _Response:
                assert stream is False
                return _Response()

        monkeypatch.setattr(
            "plugins.life_engine.service.memory_witness.get_model_set_by_task",
            lambda _task: ({"model_identifier": "test"},),
        )
        monkeypatch.setattr(
            "plugins.life_engine.service.memory_witness.LLMRequest",
            _Request,
        )
        return await coordinator._author_witness(instance, (_occurrence(1),))

    original = "  **我记得**\n```memory\n<no_witness> 只是被提到\n```  "
    preserved = await invoke(original)
    exact_token = await invoke(" \n<NO_WITNESS>\n ")
    with pytest.raises(MemoryWitnessAuthoringEmptyResponse):
        await invoke(" \n\t ")
    assert preserved.text == original
    assert (
        preserved.response_sha256
        == hashlib.sha256(original.encode("utf-8")).hexdigest()
    )
    assert exact_token.text == ""


@pytest.mark.asyncio
async def test_legacy_diary_migration_runs_once_per_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator, service = _coordinator(
        tmp_path,
        [],
        migrate_legacy_diaries=True,
        legacy_diary_path=str(tmp_path / "diaries"),
    )
    calls = 0

    async def migrate(_memory: Any, _source: Path) -> int:
        nonlocal calls
        calls += 1
        return 0

    monkeypatch.setattr(
        "plugins.life_engine.service.legacy_diary.migrate_legacy_diaries",
        migrate,
    )
    await coordinator._migrate_legacy_diaries()
    await coordinator._migrate_legacy_diaries()
    second_coordinator = MemoryWitnessCoordinator(service)
    await second_coordinator._migrate_legacy_diaries()
    assert calls == 1
    assert coordinator._legacy_migration_complete is True
    assert second_coordinator._legacy_migration_complete is True


@pytest.mark.asyncio
async def test_presence_failure_does_not_reverse_committed_decision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator, service = _coordinator(tmp_path, [_event(1)])

    async def author(*_args: Any) -> _AuthoringResult:
        return _authored(service)

    monkeypatch.setattr(coordinator, "_author_witness", author)
    service.touch_error = RuntimeError("presence unavailable")
    report = await coordinator.run_once()
    state = await service.memory_service.bundle.witnesses.get_state(
        MEMORY_WITNESS_INSTANCE_ID
    )
    assert report.decisions_committed == 1
    assert state["last_sequence"] == 1


@pytest.mark.asyncio
async def test_author_claim_conflict_returns_skip_report_not_failure(
    tmp_path: Path,
) -> None:
    """Another live instance (resident writer) holding the author claim is the
    expected multi-writer guest role. run_once must finish ingest/delivery/
    reconciliation, skip authoring, and report a bounded skip -- not raise."""

    coordinator, service = _coordinator(tmp_path, [_event(1)])

    class _ClaimingRuntime:
        enabled = True
        authority_token = SimpleNamespace(owner_id="elysium-linux-primary")

        async def validate_writer(self) -> None:
            return None

        async def acquire_singleton_writer(self, **_kwargs: Any) -> Any:
            raise SingletonWriterClaimConflict(
                "SingletonWriterAlreadyClaimed:life_engine.memory_witness:"
                "memory_witness:owner=elysium-linux-primary:memory_witness:"
                "pid-60412:epoch=3"
            )

        async def renew_singleton_writer(
            self,
            current: Any,
            **_kwargs: Any,
        ) -> Any:
            raise AssertionError("renew must not run on the non-authoring path")

    service.storage_runtime = _ClaimingRuntime()
    report = await coordinator.run_once()

    assert report.author_claim_conflict is True
    assert report.decisions_committed == 0
    assert report.synced_experiences >= 1  # Stage A still ingested
    assert coordinator._author_claim_mode == "not_authoring_writer"
    # cursor must not advance: this node is not the writer
    state = await service.memory_service.bundle.witnesses.get_state(
        MEMORY_WITNESS_INSTANCE_ID
    )
    assert state["last_sequence"] == 0


@pytest.mark.asyncio
async def test_author_claim_conflict_keeps_ingest_and_reconciliation(
    tmp_path: Path,
) -> None:
    """The non-authoring skip must not lose the already-completed ingest /
    delivery / reconciliation work, and must never trigger a fatal ERROR."""

    coordinator, service = _coordinator(tmp_path, [_event(1)])

    class _ClaimingRuntime:
        enabled = True
        authority_token = SimpleNamespace(owner_id="elysium-linux-primary")

        async def validate_writer(self) -> None:
            return None

        async def acquire_singleton_writer(self, **_kwargs: Any) -> Any:
            raise SingletonWriterClaimConflict(
                "SingletonWriterAlreadyClaimed:life_engine.memory_witness:"
                "memory_witness:owner=elysium-linux-primary:pid-60412:epoch=3"
            )

        async def renew_singleton_writer(
            self,
            current: Any,
            **_kwargs: Any,
        ) -> Any:
            raise AssertionError("renew must not run on the non-authoring path")

    service.storage_runtime = _ClaimingRuntime()
    reports = [await coordinator.run_once() for _ in range(2)]

    assert all(report.author_claim_conflict for report in reports)
    # repeated non-authoring skips are normal and stay quiet
    assert all(report.decisions_committed == 0 for report in reports)
    assert coordinator._author_claim_mode == "not_authoring_writer"


@pytest.mark.asyncio
async def test_author_claim_lost_skips_and_drops_stale_claim(
    tmp_path: Path,
) -> None:
    """Losing the author lease (takeover/expiry) must not spam ERROR forever:
    run_once surfaces a bounded skip and the stale claim is dropped so the
    next cycle re-acquires instead of renewing a dead lease."""

    coordinator, service = _coordinator(tmp_path, [_event(1)])

    class _LosingRuntime:
        enabled = True
        authority_token = SimpleNamespace(owner_id="elysium-linux-primary")

        async def validate_writer(self) -> None:
            return None

        async def acquire_singleton_writer(self, **_kwargs: Any) -> Any:
            raise AssertionError("acquire must not run while a claim is held")

        async def renew_singleton_writer(
            self,
            current: Any,
            **_kwargs: Any,
        ) -> Any:
            raise SingletonWriterClaimLost("SingletonWriterRenewalLost")

    # Simulate this node previously held the durable author claim.
    coordinator._author_claim = SimpleNamespace(owner_id="stale")
    service.storage_runtime = _LosingRuntime()
    report = await coordinator.run_once()

    assert report.author_claim_conflict is True
    assert report.decisions_committed == 0
    assert coordinator._author_claim is None  # stale claim dropped
    assert coordinator._author_claim_mode == "not_authoring_writer"
    # Stage A still ingested; this node keeps doing shared work
    assert report.synced_experiences >= 1


@pytest.mark.asyncio
async def test_author_claim_lost_then_reacquires_on_next_cycle(
    tmp_path: Path,
) -> None:
    """After the lease is lost, the next cycle re-acquires cleanly instead of
    being stuck renewing a dead lease forever."""

    coordinator, service = _coordinator(tmp_path, [])

    class _Runtime:
        enabled = True
        authority_token = SimpleNamespace(owner_id="elysium-linux-primary")

        async def validate_writer(self) -> None:
            return None

        async def acquire_singleton_writer(self, **_kwargs: Any) -> Any:
            return SimpleNamespace(
                generation_id="g",
                namespace="life_engine.memory_witness",
                state_key="memory_witness",
                owner_instance_id="elysium-linux-primary:memory_witness:pid-x",
                lease_epoch=1,
                lease_until="2099-01-01T00:00:00+00:00",
                fencing_token="tok",
            )

        async def renew_singleton_writer(
            self,
            current: Any,
            **_kwargs: Any,
        ) -> Any:
            raise SingletonWriterClaimLost("SingletonWriterRenewalLost")

    service.storage_runtime = _Runtime()
    coordinator._author_claim = SimpleNamespace(owner_id="stale")
    with pytest.raises(SingletonWriterClaimLost):
        await coordinator._ensure_authoring_claim()
    assert coordinator._author_claim is None
    assert coordinator._author_claim_mode == "selected_runtime_claim_failed"

    # next cycle re-acquires cleanly and becomes the durable writer again
    await coordinator._ensure_authoring_claim()
    assert coordinator._author_claim_mode == "durable_singleton_claim"
    assert coordinator._author_claim is not None


@pytest.mark.asyncio
async def test_author_claim_capture_identity_holds_under_dual_import(
    tmp_path: Path,
) -> None:
    """plugin_manager 加载插件时把 plugins 目录插入 sys.path，life_engine
    插件会以顶层包身份（life_engine.*）加载，writer_claims 随之出现第二份
    模块实例与第二份异常类。memory_witness 捕获的异常类必须与抛出方同身份
    （相对导入保证），否则 except 漏捕并把正常 guest 竞争刷成 ERROR。
    回归锚点：2026-08-13 双路径下 SingletonWriterClaimConflict 双类漏捕。"""

    import sys

    plugins_dir = str(Path(__file__).resolve().parents[3] / "plugins")
    saved = list(sys.path)
    sys.path.insert(0, plugins_dir)
    try:
        from life_engine.service.memory_witness import (
            SingletonWriterClaimConflict as CapturedConflict,
            SingletonWriterClaimLost as CapturedLost,
        )
        from life_engine.storage.writer_claims import (
            SingletonWriterClaimConflict as AltConflict,
            SingletonWriterClaimLost as AltLost,
        )

        assert CapturedConflict is AltConflict
        assert CapturedLost is AltLost
        # 真实抛出的异常必须被捕获方 isinstance 命中
        exc = AltConflict(
            "SingletonWriterAlreadyClaimed:life_engine.memory_witness:"
            "memory_witness:owner=remote:pid-1:epoch=3"
        )
        assert isinstance(exc, CapturedConflict)
    finally:
        sys.path[:] = saved
