"""Durability contracts for the staged Experience-to-Witness pipeline."""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import replace

import pytest

from plugins.life_engine.memory.experience import (
    ExperienceRecord,
    create_life_memory_schema,
)
from plugins.life_engine.memory.witness_pipeline import (
    WitnessDecision,
    WitnessPipelineConflict,
    WitnessWindow,
)
from plugins.life_engine.storage.memory import create_local_memory_storage_bundle
from src.kernel.storage import CursorConflict


def _db() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.row_factory = sqlite3.Row
    create_life_memory_schema(db)
    return db


def _experience(
    event_id: str,
    sequence: int,
    *,
    source_event_id: str = "",
) -> ExperienceRecord:
    return ExperienceRecord(
        event_id=event_id,
        source_event_id=source_event_id,
        sequence=sequence,
        occurred_at=f"2026-08-12T10:00:{sequence:02d}+08:00",
        recorded_at=f"2026-08-12T10:01:{sequence:02d}+08:00",
        source="test",
        channel="direct",
        event_type="message",
        content=f"occurrence {sequence}",
        stream_id="test:stream",
        consciousness_instance_id="core",
        actor="user:test",
        metadata={"fixture": True},
    )


@pytest.mark.asyncio
async def test_occurrence_report_and_view_preserve_input_order_and_aliases() -> None:
    db = _db()
    try:
        bundle = create_local_memory_storage_bundle(lambda: db)
        legacy = _experience("source-1", 1)
        await bundle.experiences.append((legacy,))
        alias = replace(
            legacy,
            event_id="occurrence-2",
            source_event_id="source-1",
            sequence=2,
            recorded_at="2026-08-12T10:02:00+08:00",
        )
        third = _experience("occurrence-3", 3, source_event_id="source-3")

        report = await bundle.experiences.append((third, alias))

        assert [item.occurrence_id for item in report.occurrences] == [
            "occurrence-3",
            "occurrence-2",
        ]
        assert report.occurrences[1].is_alias is True
        assert report.occurrences[1].canonical_event_id == "source-1"
        assert report.occurrences[1].experience.event_id == legacy.event_id
        assert report.occurrences[1].experience.content == legacy.content
        view = await bundle.experiences.list_occurrences_after(0, 10)
        assert [(item.ingest_position, item.occurrence_id) for item in view] == [
            (1, "source-1"),
            (2, "occurrence-2"),
            (3, "occurrence-3"),
        ]
        assert await bundle.experiences.health_snapshot() == {
            "status": "healthy",
            "canonical_count": 2,
            "alias_count": 1,
            "occurrence_count": 3,
            "frontier": 3,
            "frontier_cursor": {
                "ingest_position": 3,
                "occurrence_id_sha256": hashlib.sha256(
                    b"occurrence-3"
                ).hexdigest(),
            },
            "latest_recorded_at": "2026-08-12T10:01:03+08:00",
        }

        with pytest.raises(ValueError, match="ExperienceAliasConflict"):
            await bundle.experiences.append((replace(alias, sequence=4),))
    finally:
        db.close()


@pytest.mark.asyncio
async def test_window_and_decision_are_idempotent_but_conflicts_fail_closed() -> None:
    db = _db()
    try:
        bundle = create_local_memory_storage_bundle(lambda: db)
        report = await bundle.experiences.append(
            (_experience("event-1", 1), _experience("event-2", 2))
        )
        window = WitnessWindow(
            window_id="window-1",
            consciousness_instance_id="core",
            stream_scope="test:stream",
            start_position=1,
            end_position=2,
            occurrences=report.occurrences,
            created_at="2026-08-12T10:10:00+08:00",
            planner_version="fixture-v1",
        )

        first = await bundle.witnesses.append_window(window)
        replay = await bundle.witnesses.append_window(window)
        assert first == replay
        assert (await bundle.witnesses.get_window("window-1")) == first
        assert (await bundle.witnesses.next_pending_window("core")) == first
        with pytest.raises(WitnessPipelineConflict):
            await bundle.witnesses.append_window(
                replace(window, planner_version="different")
            )

        decision = WitnessDecision(
            decision_id="decision-1",
            window_id="window-1",
            consciousness_instance_id="core",
            decision_kind="witness",
            witness_id="witness-1",
            model_task_name="life_memory_witness",
            model_request_id="request-1",
            response_sha256="a" * 64,
            decided_at="2026-08-12T10:11:00+08:00",
        )
        payloads = {
            "world": {"delivery_id": "world-1", "projection_sha256": "b" * 64},
            "projection": {"path": "diaries/2026-08-12.md", "bytes": 12},
        }
        persisted = await bundle.witnesses.append_decision(
            decision, delivery_payloads=payloads
        )
        assert (
            await bundle.witnesses.append_decision(
                decision, delivery_payloads=payloads
            )
            == persisted
        )
        assert await bundle.witnesses.get_decision("decision-1") == persisted
        assert await bundle.witnesses.next_pending_window("core") is None
        jobs = await bundle.witnesses.list_delivery_jobs(statuses=("pending",))
        assert {job.delivery_kind for job in jobs} == {"projection", "world"}
        with pytest.raises(WitnessPipelineConflict):
            await bundle.witnesses.append_decision(
                decision,
                delivery_payloads={**payloads, "world": {"delivery_id": "changed"}},
            )

        with pytest.raises(ValueError, match="WitnessDecisionKindUnsupported"):
            await bundle.witnesses.append_decision(
                replace(decision, decision_id="decision-invalid", decision_kind="skip"),
                delivery_payloads={},
            )
    finally:
        db.close()


@pytest.mark.asyncio
async def test_delivery_jobs_use_revision_cas_and_keep_authority_immutable() -> None:
    db = _db()
    try:
        bundle = create_local_memory_storage_bundle(lambda: db)
        report = await bundle.experiences.append((_experience("event-1", 1),))
        await bundle.witnesses.append_window(
            WitnessWindow(
                window_id="window-1",
                consciousness_instance_id="core",
                start_position=1,
                end_position=1,
                occurrences=report.occurrences,
                created_at="2026-08-12T10:10:00+08:00",
            )
        )
        await bundle.witnesses.append_decision(
            WitnessDecision(
                decision_id="decision-1",
                window_id="window-1",
                consciousness_instance_id="core",
                decision_kind="witness",
                witness_id="witness-1",
                decided_at="2026-08-12T10:11:00+08:00",
            ),
            delivery_payloads={
                "world": {"delivery_id": "world-1"},
                "projection": {"path": "diaries/witness.md"},
            },
        )
        jobs = await bundle.witnesses.list_delivery_jobs(statuses=("pending",))
        world = next(job for job in jobs if job.delivery_kind == "world")
        processing = await bundle.witnesses.mark_delivery_job(
            world.job_id,
            expected_revision=0,
            status="processing",
            lease_owner="worker-1",
            lease_expires_at="2026-08-12T10:20:00+08:00",
        )
        assert (processing.status, processing.revision, processing.attempt_count) == (
            "processing",
            1,
            1,
        )
        succeeded = await bundle.witnesses.mark_delivery_job(
            world.job_id,
            expected_revision=1,
            status="succeeded",
        )
        assert succeeded.status == "succeeded"
        assert succeeded.revision == 2
        with pytest.raises(CursorConflict):
            await bundle.witnesses.mark_delivery_job(
                world.job_id,
                expected_revision=1,
                status="failed",
                error_type="TimeoutError",
            )

        projection = (
            await bundle.witnesses.list_projection_records(statuses=("pending",))
        )[0]
        failed = await bundle.witnesses.mark_delivery_job(
            projection.job_id,
            expected_revision=0,
            status="failed",
            error_type="ProjectionUnavailable",
            available_at="2026-08-12T10:30:00+08:00",
        )
        assert failed.status == "failed"
        health = await bundle.witnesses.projection_health()
        assert health["status"] == "degraded"
        assert health["counts"]["failed"] == 1
        assert health["total"] == 1

        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "UPDATE memory_witness_delivery_jobs SET payload_json = '{}' "
                "WHERE job_id = ?",
                (projection.job_id,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "UPDATE memory_witness_windows SET end_position = 2 "
                "WHERE window_id = 'window-1'"
            )
    finally:
        db.close()
