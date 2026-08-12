"""Subject-document review opportunities and fail-closed mutation contracts."""

from __future__ import annotations

import base64
import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from plugins.life_engine.core.config import LifeEngineConfig
from plugins.life_engine.learning import scheduler as scheduler_module
from plugins.life_engine.learning import tools as learning_tools
from plugins.life_engine.learning.scheduler import LearningScheduler
from plugins.life_engine.learning.tools import LifeReviewSubjectDocumentTool
from plugins.life_engine.memory.continuity_index import (
    CONTINUITY_MEMORY_REVIEW_GROWTH_BYTES,
    CONTINUITY_MEMORY_REVIEW_PRESSURE_BYTES,
    CONTINUITY_MEMORY_SOFT_TARGET_BYTES,
)
from plugins.life_engine.storage.learning_contracts import (
    LearningCommitResult,
    LearningEventDraft,
    LearningEventRecord,
    LearningOccurrenceConflict,
    LearningProjection,
    LearningProjectionConflict,
    LearningProjectionWrite,
)
from plugins.life_engine.storage.subject_contracts import (
    SubjectAuthoritySnapshot,
    SubjectDocumentCommit,
    SubjectDocumentHead,
    SubjectDocumentVersion,
)
from src.kernel.llm.context_delivery import EffectiveContextReceipt


def _workspace(tmp_path: Path) -> None:
    old = (datetime.now(UTC) - timedelta(days=3)).timestamp()
    for path in ("SOUL.md", "USER.md", "MEMORY.md"):
        target = tmp_path / path
        target.write_text(f"# {path}\ncurrent\n", encoding="utf-8")
        os.utime(target, (old, old))


def _scheduler(tmp_path: Path) -> LearningScheduler:
    return LearningScheduler(
        workspace_path=tmp_path,
        current_subject_revision=lambda: _revision("a"),
        validate_active_consciousness_instance=lambda actor: _is_active(actor),
        subject_review_soul_interval_hours=24.0,
        subject_review_user_interval_hours=24.0,
        subject_review_memory_interval_hours=24.0,
        subject_review_offer_cooldown_hours=24.0,
    )


async def _revision(character: str) -> str:
    return character * 64


async def _is_active(actor: str) -> bool:
    return actor == "consciousness-1"


def _remote_snapshot(
    contents: dict[str, bytes],
    *,
    head_revisions: dict[str, int] | None = None,
) -> SubjectAuthoritySnapshot:
    commits: dict[str, SubjectDocumentCommit] = {}
    for index, (path, content) in enumerate(contents.items(), start=1):
        logical_path = f"life_engine_workspace/{path}"
        head_revision = int((head_revisions or {}).get(path, 1))
        version_id = f"remote-version-{index}-{head_revision}"
        commits[path] = SubjectDocumentCommit(
            version=SubjectDocumentVersion(
                version_id=version_id,
                document_id=f"remote-document-{index}",
                logical_path=logical_path,
                parent_version_id="",
                occurrence_id=f"remote-occurrence-{index}",
                semantic_actor_id="elysia",
                semantic_source_id="remote-test",
                occurred_at="2026-08-06T00:00:00+00:00",
                recorded_by="test",
                recorded_source="mysql",
                recorded_at="2026-08-06T00:00:00+00:00",
                provenance_status="complete",
                content_bytes=content,
                content_hash=hashlib.sha256(content).hexdigest(),
                byte_length=len(content),
                byte_fidelity="exact_bytes",
                encoding="utf-8",
                newline_style="LF",
                change_context={},
            ),
            head=SubjectDocumentHead(
                document_id=f"remote-document-{index}",
                logical_path=logical_path,
                declared_owner="elysia",
                current_version_id=version_id,
                revision=head_revision,
            ),
        )
    return SubjectAuthoritySnapshot(commits=commits, revision="a" * 64)  # type: ignore[arg-type]


class _Ledger:
    def __init__(self, event_store: object | None = None) -> None:
        self.candidates: list[object] = []
        self.decisions: list[object] = []
        self.event_store = event_store

    async def append_candidate(self, candidate: object) -> SimpleNamespace:
        self.candidates.append(candidate)
        if self.event_store is not None:
            await self.event_store.commit(  # type: ignore[attr-defined]
                events=[
                    LearningEventDraft(
                        occurrence_id=getattr(candidate, "candidate_occurrence_id"),
                        event_kind="candidate.proposed",
                        occurred_at=getattr(candidate, "occurred_at"),
                        source=getattr(candidate, "source"),
                        actor_consciousness_instance_id=getattr(
                            candidate,
                            "actor_consciousness_instance_id",
                        ),
                        subject_revision=getattr(candidate, "subject_revision"),
                        provenance={
                            "source_occurrence_id": getattr(
                                candidate,
                                "source_occurrence_id",
                            ),
                            **dict(getattr(candidate, "provenance")),
                        },
                        payload={
                            "candidate_id": getattr(candidate, "candidate_id"),
                            "candidate_revision": getattr(
                                candidate,
                                "candidate_revision",
                            ),
                            "candidate_kind": getattr(candidate, "candidate_kind"),
                            "candidate_sha256": getattr(
                                candidate,
                                "candidate_sha256",
                            ),
                            "target_path": getattr(candidate, "target_path") or "",
                            "candidate_content_base64": base64.b64encode(
                                getattr(candidate, "candidate_content_bytes")
                            ).decode("ascii"),
                        },
                    )
                ],
                projections=[],
            )
        return SimpleNamespace(status="open")

    async def record_decision(self, decision: object) -> SimpleNamespace:
        self.decisions.append(decision)
        return SimpleNamespace(
            decision_occurrence_id=getattr(decision, "decision_occurrence_id"),
            status=getattr(decision, "decision_kind"),
        )


class _LearningEventStore:
    """Minimal strict append-only store for selected review evidence."""

    def __init__(self) -> None:
        self.records: list[LearningEventRecord] = []
        self.projections: dict[str, LearningProjection] = {}

    @staticmethod
    def _matches(record: LearningEventRecord, draft: LearningEventDraft) -> bool:
        return all(
            (
                record.occurrence_id == draft.occurrence_id,
                record.event_kind == draft.event_kind,
                record.occurred_at == draft.occurred_at,
                record.source == draft.source,
                record.actor_consciousness_instance_id
                == draft.actor_consciousness_instance_id,
                record.subject_revision == draft.subject_revision,
                record.provenance == draft.provenance,
                record.payload == draft.payload,
            )
        )

    async def commit(
        self,
        *,
        events: list[LearningEventDraft],
        projections: list[LearningProjectionWrite],
    ) -> LearningCommitResult:
        committed: list[LearningEventRecord] = []
        for draft in events:
            existing = await self.event_by_occurrence(draft.occurrence_id)
            if existing is not None:
                if not self._matches(existing, draft):
                    raise LearningOccurrenceConflict(draft.occurrence_id)
                committed.append(existing)
                continue
            record = LearningEventRecord(
                position=len(self.records) + 1,
                occurrence_id=draft.occurrence_id,
                event_kind=draft.event_kind,
                occurred_at=draft.occurred_at,
                recorded_at=draft.occurred_at,
                source=draft.source,
                actor_consciousness_instance_id=(draft.actor_consciousness_instance_id),
                subject_revision=draft.subject_revision,
                provenance=dict(draft.provenance),
                payload=dict(draft.payload),
                event_sha256=hashlib.sha256(
                    json.dumps(
                        {
                            "occurrence_id": draft.occurrence_id,
                            "payload": draft.payload,
                        },
                        sort_keys=True,
                    ).encode()
                ).hexdigest(),
            )
            self.records.append(record)
            committed.append(record)
        committed_projections: list[LearningProjection] = []
        for write in projections:
            current = self.projections.get(write.projection_name)
            current_revision = current.revision if current is not None else 0
            current_frontier = current.source_frontier if current is not None else 0
            if (
                current_revision != write.expected_revision
                or current_frontier != write.expected_source_frontier
            ):
                raise LearningProjectionConflict(
                    projection_name=write.projection_name,
                    expected_revision=write.expected_revision,
                    expected_source_frontier=write.expected_source_frontier,
                    actual_revision=current_revision,
                    actual_source_frontier=current_frontier,
                    actual_projection_sha256=(
                        current.projection_sha256 if current is not None else ""
                    ),
                )
            projection_sha256 = hashlib.sha256(
                json.dumps(
                    write.payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
            projection = LearningProjection(
                projection_name=write.projection_name,
                revision=current_revision + 1,
                source_frontier=len(self.records),
                schema_version=write.schema_version,
                projector_version=write.projector_version,
                rebuild_state=write.rebuild_state,
                payload=dict(write.payload),
                projection_sha256=projection_sha256,
                updated_at="2026-08-10T00:00:00+00:00",
            )
            self.projections[write.projection_name] = projection
            committed_projections.append(projection)
        return LearningCommitResult(
            events=tuple(committed),
            projections=tuple(committed_projections),
        )

    async def event_by_occurrence(
        self,
        occurrence_id: str,
    ) -> LearningEventRecord | None:
        return next(
            (
                record
                for record in self.records
                if record.occurrence_id == occurrence_id
            ),
            None,
        )

    async def get_projection(self, projection_name: str) -> LearningProjection | None:
        return self.projections.get(projection_name)

    async def list_projections(self) -> list[LearningProjection]:
        return list(self.projections.values())

    async def read_events(
        self,
        after_position: int,
        *,
        limit: int = 100,
        event_kinds: tuple[str, ...] = (),
    ) -> list[LearningEventRecord]:
        return [
            record
            for record in self.records
            if record.position > after_position
            and (not event_kinds or record.event_kind in event_kinds)
        ][:limit]

    async def health_snapshot(self) -> dict[str, object]:
        return {
            "status": "healthy",
            "event_count": len(self.records),
            "latest_position": len(self.records),
            "projection_states": {
                name: projection.rebuild_state
                for name, projection in self.projections.items()
            },
        }


class _Clock(datetime):
    current = datetime(2026, 8, 10, 0, 0, tzinfo=UTC)

    @classmethod
    def now(cls, tz=None) -> datetime:
        if tz is None:
            return cls.current.replace(tzinfo=None)
        return cls.current.astimezone(tz)


async def test_review_opportunity_is_bounded_and_offer_has_cooldown(
    tmp_path: Path,
) -> None:
    _workspace(tmp_path)
    scheduler = _scheduler(tmp_path)

    first = await scheduler.get_subject_review_snapshot(mark_offered=True)
    second = await scheduler.get_subject_review_snapshot(mark_offered=False)

    assert first["authority_status"] == "migration_required"
    assert first["direct_mutation_blocked"] is True
    assert first["subject_revision"] == "a" * 64
    assert first["due_count"] == 3
    assert all(len(item["content_sha256"]) == 64 for item in first["documents"])
    assert second["due_count"] == 0
    prompt = await scheduler.get_subject_review_prompt()
    assert prompt == ""
    assert scheduler.get_state()["subject_review"]["authority_status"] == (
        "migration_required"
    )


async def test_memory_engineering_pressure_invites_review_without_auto_deletion(
    tmp_path: Path,
) -> None:
    _workspace(tmp_path)
    memory = tmp_path / "MEMORY.md"
    memory.write_bytes(b"# MEMORY\n" + b"x" * CONTINUITY_MEMORY_REVIEW_PRESSURE_BYTES)
    scheduler = _scheduler(tmp_path)

    snapshot = await scheduler.get_subject_review_snapshot()
    item = next(
        document
        for document in snapshot["documents"]
        if document["target_path"] == "MEMORY.md"
    )

    assert item["due"] is True
    assert item["due_reasons"] == [
        "engineering_pressure",
        "continuity_index_review",
    ]
    assert item["soft_target_bytes"] == CONTINUITY_MEMORY_SOFT_TARGET_BYTES
    assert item["review_pressure_bytes"] == (CONTINUITY_MEMORY_REVIEW_PRESSURE_BYTES)
    assert item["review_pressure_reached"] is True
    assert item["pressure_semantics"] == "engineering_review_only"
    prompt = await scheduler.get_subject_review_prompt()
    assert "不授权自动删除" in prompt
    assert "nucleus_create_memory_boundary" in prompt
    assert memory.exists()


async def test_memory_above_soft_target_without_index_invites_structural_review(
    tmp_path: Path,
) -> None:
    for path in ("SOUL.md", "USER.md"):
        (tmp_path / path).write_text(f"# {path}\ncurrent\n", encoding="utf-8")
    memory = tmp_path / "MEMORY.md"
    memory.write_bytes(b"# MEMORY\n" + b"x" * (CONTINUITY_MEMORY_SOFT_TARGET_BYTES + 1))
    scheduler = LearningScheduler(
        workspace_path=tmp_path,
        current_subject_revision=lambda: _revision("a"),
        validate_active_consciousness_instance=lambda actor: _is_active(actor),
        subject_review_soul_interval_hours=720.0,
        subject_review_user_interval_hours=720.0,
        subject_review_memory_interval_hours=720.0,
    )

    snapshot = await scheduler.get_subject_review_snapshot()
    item = next(
        document
        for document in snapshot["documents"]
        if document["target_path"] == "MEMORY.md"
    )

    assert item["due"] is True
    assert item["due_reasons"] == ["continuity_index_review"]
    assert item["continuity_index_state"] == "absent"
    assert item["continuity_index_semantics"] == "structural_review_only"
    assert item["review_pressure_reached"] is False
    health_item = next(
        document
        for document in scheduler.get_state()["subject_review"]["documents"]
        if document["target_path"] == "MEMORY.md"
    )
    assert health_item["due"] is True
    assert health_item["continuity_index_state"] == "absent"
    prompt = await scheduler.get_subject_review_prompt()
    assert "不判断任何记忆是否重要" in prompt
    assert "nucleus_create_memory_boundary_from_subject_range" in prompt

    await scheduler.record_subject_review_outcome(
        target_path="MEMORY.md",
        outcome="unchanged",
        actor_consciousness_instance_id="consciousness-1",
        subject_revision="a" * 64,
        occurrence_id="review:memory:index-not-needed-now",
        reason="I reviewed this exact version and choose not to add an index now.",
    )
    acknowledged = await scheduler.get_subject_review_snapshot()
    acknowledged_item = next(
        document
        for document in acknowledged["documents"]
        if document["target_path"] == "MEMORY.md"
    )
    assert acknowledged_item["continuity_index_review_due"] is False
    assert acknowledged_item["due"] is False


async def test_explicit_boundary_index_suppresses_absence_review_signal(
    tmp_path: Path,
) -> None:
    for path in ("SOUL.md", "USER.md"):
        (tmp_path / path).write_text(f"# {path}\ncurrent\n", encoding="utf-8")
    uri = "memory://boundary/kept-memory@artifact_" + "b" * 64 + "#sha256=" + "a" * 64
    memory = tmp_path / "MEMORY.md"
    memory.write_text(
        "# MEMORY\n\n[完整记忆](" + uri + ")\n" + "x" * 17000,
        encoding="utf-8",
    )
    scheduler = LearningScheduler(
        workspace_path=tmp_path,
        current_subject_revision=lambda: _revision("a"),
        subject_review_memory_interval_hours=720.0,
    )

    snapshot = await scheduler.get_subject_review_snapshot()
    item = next(
        document
        for document in snapshot["documents"]
        if document["target_path"] == "MEMORY.md"
    )

    assert item["continuity_index_entry_count"] == 1
    assert item["continuity_index_state"] == "present"
    assert item["continuity_index_review_due"] is False
    assert item["due"] is False


async def test_open_subject_candidate_is_reoffered_only_after_exact_delivery(
    tmp_path: Path,
) -> None:
    for path in ("SOUL.md", "USER.md", "MEMORY.md"):
        (tmp_path / path).write_text(f"# {path}\ncurrent\n", encoding="utf-8")
    scheduler = LearningScheduler(
        workspace_path=tmp_path,
        current_subject_revision=lambda: _revision("a"),
        subject_review_soul_interval_hours=720.0,
        subject_review_user_interval_hours=720.0,
        subject_review_memory_interval_hours=720.0,
        subject_review_offer_cooldown_hours=24.0,
    )
    state, review = scheduler._subject_review_state()
    review["documents"]["MEMORY.md"] = {
        "last_outcome": "candidate_proposed",
        "last_candidate_id": "subject-candidate-memory-1",
        "last_candidate_sha256": "c" * 64,
    }
    state["subject_review_v1"] = review
    scheduler.store.save_state(state)

    snapshot = await scheduler.get_subject_review_snapshot()
    memory = next(
        item for item in snapshot["documents"] if item["target_path"] == "MEMORY.md"
    )
    assert memory["due"] is True
    assert memory["due_reasons"] == ["candidate_decision_pending"]
    prompt = await scheduler.get_subject_review_prompt()
    pending = scheduler.get_pending_subject_review_offer()
    assert "保持开放、拒绝或接受都有效" in prompt
    assert "nucleus_read_subject_candidate" in prompt
    assert pending is not None

    identity = str(pending["delivery_id"])
    receipt = EffectiveContextReceipt(
        delivery_id=identity,
        exact_present=True,
        expected_utf8_bytes=100,
        expected_sha256="d" * 64,
        effective_utf8_bytes=100,
        effective_sha256="d" * 64,
        part_kind="text",
    )
    assert await scheduler.commit_subject_review_offer_delivery(identity, receipt)
    assert (await scheduler.get_subject_review_snapshot())["due_count"] == 0


async def test_review_invitation_cooldown_starts_only_after_exact_delivery(
    tmp_path: Path,
) -> None:
    _workspace(tmp_path)
    scheduler = _scheduler(tmp_path)

    prompt = await scheduler.get_subject_review_prompt()
    pending = scheduler.get_pending_subject_review_offer()
    assert prompt
    assert pending is not None
    assert pending["delivery_marker"] in prompt
    assert (await scheduler.get_subject_review_snapshot())["due_count"] == 3

    identity = str(pending["delivery_id"])
    missing = EffectiveContextReceipt(
        delivery_id=identity,
        exact_present=False,
        expected_utf8_bytes=100,
        expected_sha256="a" * 64,
        effective_utf8_bytes=None,
        effective_sha256=None,
        part_kind="text",
    )
    assert (
        await scheduler.commit_subject_review_offer_delivery(identity, missing) is False
    )
    assert (await scheduler.get_subject_review_snapshot())["due_count"] == 3

    prompt = await scheduler.get_subject_review_prompt()
    pending = scheduler.get_pending_subject_review_offer()
    assert pending is not None and pending["delivery_marker"] in prompt
    identity = str(pending["delivery_id"])
    delivered = EffectiveContextReceipt(
        delivery_id=identity,
        exact_present=True,
        expected_utf8_bytes=100,
        expected_sha256="b" * 64,
        effective_utf8_bytes=100,
        effective_sha256="b" * 64,
        part_kind="text",
    )
    assert (
        await scheduler.commit_subject_review_offer_delivery(identity, delivered)
        is True
    )
    assert (await scheduler.get_subject_review_snapshot())["due_count"] == 0


async def test_memory_pressure_acknowledgement_waits_for_bounded_growth(
    tmp_path: Path,
) -> None:
    _workspace(tmp_path)
    memory = tmp_path / "MEMORY.md"
    initial = b"# MEMORY\n" + b"x" * CONTINUITY_MEMORY_REVIEW_PRESSURE_BYTES
    memory.write_bytes(initial)
    scheduler = _scheduler(tmp_path)

    await scheduler.record_subject_review_outcome(
        target_path="MEMORY.md",
        outcome="unchanged",
        actor_consciousness_instance_id="consciousness-1",
        subject_revision="a" * 64,
        occurrence_id="review:memory:pressure-ack",
        reason="I reviewed this exact version and choose to keep it.",
    )
    acknowledged = await scheduler.get_subject_review_snapshot()
    acknowledged_item = next(
        item for item in acknowledged["documents"] if item["target_path"] == "MEMORY.md"
    )
    assert acknowledged_item["review_pressure_acknowledged"] is True
    assert acknowledged_item["due"] is False

    memory.write_bytes(initial + b"y" * (CONTINUITY_MEMORY_REVIEW_GROWTH_BYTES - 1))
    still_acknowledged = await scheduler.get_subject_review_snapshot()
    still_item = next(
        item
        for item in still_acknowledged["documents"]
        if item["target_path"] == "MEMORY.md"
    )
    assert still_item["review_pressure_acknowledged"] is True
    assert still_item["due"] is False

    memory.write_bytes(initial + b"y" * CONTINUITY_MEMORY_REVIEW_GROWTH_BYTES)
    grown = await scheduler.get_subject_review_snapshot()
    grown_item = next(
        item for item in grown["documents"] if item["target_path"] == "MEMORY.md"
    )
    assert grown_item["review_pressure_acknowledged"] is False
    assert grown_item["due"] is True
    assert "engineering_pressure" in grown_item["due_reasons"]


async def test_memory_pressure_acknowledgement_does_not_mask_a_new_crossing(
    tmp_path: Path,
) -> None:
    _workspace(tmp_path)
    memory = tmp_path / "MEMORY.md"
    acknowledged_size = (
        CONTINUITY_MEMORY_REVIEW_PRESSURE_BYTES + CONTINUITY_MEMORY_REVIEW_GROWTH_BYTES
    )
    memory.write_bytes(b"x" * acknowledged_size)
    scheduler = _scheduler(tmp_path)

    await scheduler.record_subject_review_outcome(
        target_path="MEMORY.md",
        outcome="unchanged",
        actor_consciousness_instance_id="consciousness-1",
        subject_revision="a" * 64,
        occurrence_id="review:memory:pressure-before-shrink",
        reason="I reviewed this exact larger version.",
    )
    memory.write_bytes(b"x" * (CONTINUITY_MEMORY_REVIEW_PRESSURE_BYTES - 1))
    below = await scheduler.get_subject_review_snapshot()
    below_item = next(
        item for item in below["documents"] if item["target_path"] == "MEMORY.md"
    )
    assert below_item["review_pressure_reached"] is False
    assert below_item["due"] is False

    memory.write_bytes(b"x" * CONTINUITY_MEMORY_REVIEW_PRESSURE_BYTES)
    crossed_again = await scheduler.get_subject_review_snapshot()
    crossed_item = next(
        item
        for item in crossed_again["documents"]
        if item["target_path"] == "MEMORY.md"
    )
    assert crossed_item["review_pressure_acknowledged"] is False
    assert crossed_item["due"] is True
    assert "engineering_pressure" in crossed_item["due_reasons"]


async def test_selected_document_identity_is_from_one_authority_snapshot(
    tmp_path: Path,
) -> None:
    remote = {
        "SOUL.md": b"remote soul",
        "USER.md": b"remote user",
        "MEMORY.md": b"remote memory",
    }

    async def read_remote() -> SubjectAuthoritySnapshot:
        return _remote_snapshot(remote)

    scheduler = LearningScheduler(
        workspace_path=tmp_path,
        current_subject_revision=lambda: _revision("b"),
        read_subject_authority=read_remote,
    )

    content, version_id, revision = await scheduler.read_subject_document_with_identity(
        "MEMORY.md"
    )

    assert content == b"remote memory"
    assert version_id == "remote-version-3-1"
    assert revision == "a" * 64

    snapshot = await scheduler.read_subject_document_snapshot("MEMORY.md")
    assert snapshot.content_bytes == b"remote memory"
    assert snapshot.version_id == "remote-version-3-1"
    assert snapshot.source_occurrence_id == "remote-occurrence-3"
    assert snapshot.unified_subject_revision == "a" * 64
    assert snapshot.provenance_status == "complete"


async def test_selected_review_snapshot_reads_one_coherent_authority_snapshot(
    tmp_path: Path,
) -> None:
    remote = {
        "SOUL.md": b"remote soul",
        "USER.md": b"remote user",
        "MEMORY.md": b"remote memory",
    }
    reads = 0

    async def read_remote() -> SubjectAuthoritySnapshot:
        nonlocal reads
        reads += 1
        return _remote_snapshot(remote)

    scheduler = LearningScheduler(
        workspace_path=tmp_path,
        current_subject_revision=lambda: _revision("b"),
        read_subject_authority=read_remote,
    )

    snapshot = await scheduler.get_subject_review_snapshot()

    assert reads == 1
    assert snapshot["subject_revision"] == "a" * 64
    assert snapshot["revision_error"] == ""
    assert {item["size_bytes"] for item in snapshot["documents"]} == {
        len(value) for value in remote.values()
    }
    memory = next(
        item for item in snapshot["documents"] if item["target_path"] == "MEMORY.md"
    )
    assert memory["version_id"] == "remote-version-3-1"
    assert memory["source_occurrence_id"] == "remote-occurrence-3"


async def test_selected_review_baseline_survives_checks_restart_and_head_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _workspace(tmp_path)
    remote = {
        "SOUL.md": b"remote soul",
        "USER.md": b"remote user",
        "MEMORY.md": b"remote memory",
    }
    head_revisions = {path: 1 for path in remote}

    async def read_remote() -> SubjectAuthoritySnapshot:
        return _remote_snapshot(remote, head_revisions=head_revisions)

    selected_store = _LearningEventStore()

    async def selected_scheduler() -> LearningScheduler:
        scheduler = LearningScheduler(
            workspace_path=tmp_path,
            current_subject_revision=lambda: _revision("a"),
            read_subject_authority=read_remote,
            validate_active_consciousness_instance=lambda actor: _is_active(actor),
            learning_store=selected_store,
            learning_event_store=selected_store,
            subject_review_soul_interval_hours=24.0,
            subject_review_user_interval_hours=24.0,
            subject_review_memory_interval_hours=24.0,
        )
        await scheduler.initialize()
        assert scheduler.decision_ledger is not None
        return scheduler

    monkeypatch.setattr(scheduler_module, "datetime", _Clock)
    _Clock.current = datetime(2026, 8, 10, 0, 0, tzinfo=UTC)
    scheduler = await selected_scheduler()

    initial_health = scheduler.get_state()["subject_review"]
    assert initial_health["due_count"] == 0
    assert initial_health["due_count_known"] is False
    assert initial_health["unobserved_count"] == 3

    first = await scheduler.get_subject_review_snapshot()
    first_due = {item["target_path"]: item["due_at"] for item in first["documents"]}
    assert first["due_count"] == 0
    assert all(item["review_baseline_at"] for item in first["documents"])
    assert not (tmp_path / ".life_learning").exists()

    _Clock.current += timedelta(hours=12)
    second = await scheduler.get_subject_review_snapshot()
    assert {
        item["target_path"]: item["due_at"] for item in second["documents"]
    } == first_due

    _Clock.current += timedelta(hours=13)
    restarted = await selected_scheduler()
    health = restarted.get_state()["subject_review"]
    assert health["due_count"] == 3
    assert health["due_count_known"] is True
    assert health["unobserved_count"] == 0
    after_restart = await restarted.get_subject_review_snapshot()
    assert after_restart["due_count"] == 3
    assert {
        item["target_path"]: item["due_at"] for item in after_restart["documents"]
    } == first_due

    remote["MEMORY.md"] = b"remote memory changed"
    content_changed = await restarted.get_subject_review_snapshot()
    memory = next(
        item
        for item in content_changed["documents"]
        if item["target_path"] == "MEMORY.md"
    )
    assert memory["due"] is False
    assert memory["review_baseline_at"] == _Clock.current.isoformat()
    assert memory["due_at"] != first_due["MEMORY.md"]

    _Clock.current += timedelta(hours=1)
    head_revisions["USER.md"] = 2
    marker_changed = await restarted.get_subject_review_snapshot()
    user = next(
        item for item in marker_changed["documents"] if item["target_path"] == "USER.md"
    )
    assert user["due"] is False
    assert user["review_baseline_at"] == _Clock.current.isoformat()
    user_due_at = user["due_at"]

    _Clock.current += timedelta(hours=1)
    restarted_again = await selected_scheduler()
    stable = await restarted_again.get_subject_review_snapshot()
    restarted_user = next(
        item for item in stable["documents"] if item["target_path"] == "USER.md"
    )
    assert restarted_user["due_at"] == user_due_at


async def test_local_review_can_record_no_change_but_not_a_candidate(
    tmp_path: Path,
) -> None:
    _workspace(tmp_path)
    scheduler = _scheduler(tmp_path)
    revision = await scheduler.validate_subject_review_context(
        actor_consciousness_instance_id="consciousness-1",
        expected_subject_revision="a" * 64,
    )

    record = await scheduler.record_subject_review_outcome(
        target_path="MEMORY.md",
        outcome="unchanged",
        actor_consciousness_instance_id="consciousness-1",
        subject_revision=revision,
        occurrence_id="review:memory:1",
        reason="I read the current version and want to keep it.",
    )

    assert record["last_outcome"] == "unchanged"
    journal = tmp_path / ".life_learning" / "subject_reviews.jsonl"
    lines = journal.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    event = json.loads(lines[0])
    assert event["actor_consciousness_instance_id"] == "consciousness-1"
    assert event["subject_revision"] == "a" * 64
    assert event["authority"] == "review_evidence_only"

    repeated = await scheduler.record_subject_review_outcome(
        target_path="MEMORY.md",
        outcome="unchanged",
        actor_consciousness_instance_id="consciousness-1",
        subject_revision=revision,
        occurrence_id="review:memory:1",
        reason="I read the current version and want to keep it.",
    )
    assert repeated["last_occurrence_id"] == "review:memory:1"
    assert len(journal.read_text(encoding="utf-8").splitlines()) == 1

    with pytest.raises(RuntimeError, match="SubjectAuthorityMigrationRequired"):
        await scheduler.record_subject_review_outcome(
            target_path="MEMORY.md",
            outcome="candidate_proposed",
            actor_consciousness_instance_id="consciousness-1",
            subject_revision=revision,
            occurrence_id="review:memory:candidate",
            reason="candidate must not fall back to local files",
        )


async def test_selected_snooze_appends_exact_immutable_learning_event(
    tmp_path: Path,
) -> None:
    remote = {
        "SOUL.md": b"remote soul",
        "USER.md": b"remote user",
        "MEMORY.md": b"remote memory",
    }

    async def read_remote() -> SubjectAuthoritySnapshot:
        return _remote_snapshot(remote)

    event_store = _LearningEventStore()
    scheduler = LearningScheduler(
        workspace_path=tmp_path,
        current_subject_revision=lambda: _revision("a"),
        read_subject_authority=read_remote,
        validate_active_consciousness_instance=lambda actor: _is_active(actor),
        learning_store=event_store,
        learning_event_store=event_store,
    )
    await scheduler.initialize()
    assert scheduler.decision_ledger is not None

    record = await scheduler.record_subject_review_outcome(
        target_path="MEMORY.md",
        outcome="snoozed",
        actor_consciousness_instance_id="consciousness-1",
        subject_revision="a" * 64,
        occurrence_id="subject-review:snooze:1",
        reason="I want to return to this exact memory tomorrow.",
        snooze_hours=12.0,
    )

    snooze_events = [
        event
        for event in event_store.records
        if event.event_kind == "subject_review.snoozed"
    ]
    assert len(snooze_events) == 1
    event = snooze_events[0]
    assert event.event_kind == "subject_review.snoozed"
    assert event.actor_consciousness_instance_id == "consciousness-1"
    assert event.subject_revision == "a" * 64
    assert event.provenance["source_occurrence_id"] == ("subject-review:snooze:1")
    assert event.payload["target_path"] == "MEMORY.md"
    assert (
        event.payload["current_content_sha256"]
        == hashlib.sha256(remote["MEMORY.md"]).hexdigest()
    )
    assert event.payload["reason"] == (
        "I want to return to this exact memory tomorrow."
    )
    assert event.payload["snooze_hours"] == 12.0
    assert datetime.fromisoformat(str(event.payload["snooze_until"])) == (
        datetime.fromisoformat(event.occurred_at) + timedelta(hours=12)
    )
    assert (
        record["last_reviewed_content_sha256"]
        == event.payload["current_content_sha256"]
    )

    replay = await scheduler.record_subject_review_outcome(
        target_path="MEMORY.md",
        outcome="snoozed",
        actor_consciousness_instance_id="consciousness-1",
        subject_revision="a" * 64,
        occurrence_id="subject-review:snooze:1",
        reason="I want to return to this exact memory tomorrow.",
        snooze_hours=12.0,
    )
    assert replay["snooze_until"] == record["snooze_until"]
    assert (
        sum(
            event.event_kind == "subject_review.snoozed"
            for event in event_store.records
        )
        == 1
    )

    # Simulate a crash after the immutable event commit but before its
    # rebuildable scheduler-state projection became durable.
    event_store.projections.clear()
    restarted = LearningScheduler(
        workspace_path=tmp_path,
        current_subject_revision=lambda: _revision("a"),
        read_subject_authority=read_remote,
        validate_active_consciousness_instance=lambda actor: _is_active(actor),
        learning_store=event_store,
        learning_event_store=event_store,
    )
    await restarted.initialize()
    recovered = await restarted.record_subject_review_outcome(
        target_path="MEMORY.md",
        outcome="snoozed",
        actor_consciousness_instance_id="consciousness-1",
        subject_revision="a" * 64,
        occurrence_id="subject-review:snooze:1",
        reason="I want to return to this exact memory tomorrow.",
        snooze_hours=12.0,
    )
    assert recovered["snooze_until"] == record["snooze_until"]
    assert (
        sum(item.event_kind == "subject_review.snoozed" for item in event_store.records)
        == 1
    )

    with pytest.raises(
        LearningOccurrenceConflict,
        match="SubjectReviewSnoozeOccurrenceConflict",
    ):
        await scheduler.record_subject_review_outcome(
            target_path="MEMORY.md",
            outcome="snoozed",
            actor_consciousness_instance_id="consciousness-1",
            subject_revision="a" * 64,
            occurrence_id="subject-review:snooze:1",
            reason="A different reason must not reuse the occurrence.",
            snooze_hours=12.0,
        )
    with pytest.raises(PermissionError, match="LearningDecisionActorIsNotActive"):
        await scheduler.record_subject_review_outcome(
            target_path="MEMORY.md",
            outcome="snoozed",
            actor_consciousness_instance_id="inactive",
            subject_revision="a" * 64,
            occurrence_id="subject-review:snooze:inactive",
            reason="An inactive instance cannot create review evidence.",
            snooze_hours=12.0,
        )
    assert (
        sum(
            event.event_kind == "subject_review.snoozed"
            for event in event_store.records
        )
        == 1
    )


async def test_local_snooze_keeps_the_compatible_append_only_journal(
    tmp_path: Path,
) -> None:
    _workspace(tmp_path)
    scheduler = _scheduler(tmp_path)
    current = (tmp_path / "USER.md").read_bytes()

    record = await scheduler.record_subject_review_outcome(
        target_path="USER.md",
        outcome="snoozed",
        actor_consciousness_instance_id="consciousness-1",
        subject_revision="a" * 64,
        occurrence_id="subject-review:local-snooze:1",
        reason="I choose to revisit this exact version later.",
        snooze_hours=8.0,
    )

    journal = tmp_path / ".life_learning" / "subject_reviews.jsonl"
    event = json.loads(journal.read_text(encoding="utf-8").splitlines()[0])
    assert event["source_occurrence_id"] == "subject-review:local-snooze:1"
    assert event["current_content_sha256"] == hashlib.sha256(current).hexdigest()
    assert event["subject_revision"] == "a" * 64
    assert event["actor_consciousness_instance_id"] == "consciousness-1"
    assert event["reason"] == "I choose to revisit this exact version later."
    assert record["snooze_until"] == event["snooze_until"]


async def test_selected_snooze_fails_closed_without_durable_learning_event_store(
    tmp_path: Path,
) -> None:
    remote = {
        "SOUL.md": b"remote soul",
        "USER.md": b"remote user",
        "MEMORY.md": b"remote memory",
    }

    async def read_remote() -> SubjectAuthoritySnapshot:
        return _remote_snapshot(remote)

    scheduler = LearningScheduler(
        workspace_path=tmp_path,
        current_subject_revision=lambda: _revision("a"),
        read_subject_authority=read_remote,
        validate_active_consciousness_instance=lambda actor: _is_active(actor),
    )

    with pytest.raises(RuntimeError, match="SubjectReviewImmutableEvidenceRequired"):
        await scheduler.record_subject_review_outcome(
            target_path="MEMORY.md",
            outcome="snoozed",
            actor_consciousness_instance_id="consciousness-1",
            subject_revision="a" * 64,
            occurrence_id="subject-review:snooze:missing-store",
            reason="This must not be projected without immutable evidence.",
            snooze_hours=24.0,
        )

    assert not (tmp_path / ".life_learning" / "subject_reviews.jsonl").exists()
    documents = (
        scheduler.store.load_state()
        .get("subject_review_v1", {})
        .get(
            "documents",
            {},
        )
    )
    assert "MEMORY.md" not in documents

    shadow_workspace = tmp_path / "selected-with-local-shadow"
    shadow_workspace.mkdir()
    _workspace(shadow_workspace)
    selected_store = _LearningEventStore()
    no_reader = LearningScheduler(
        workspace_path=shadow_workspace,
        current_subject_revision=lambda: _revision("a"),
        validate_active_consciousness_instance=lambda actor: _is_active(actor),
        learning_store=selected_store,
    )
    await no_reader.initialize()
    with pytest.raises(RuntimeError, match="SelectedSubjectAuthorityReaderRequired"):
        await no_reader.record_subject_review_outcome(
            target_path="MEMORY.md",
            outcome="snoozed",
            actor_consciousness_instance_id="consciousness-1",
            subject_revision="a" * 64,
            occurrence_id="subject-review:snooze:no-reader",
            reason="Selected mode must never bind a local shadow.",
            snooze_hours=24.0,
        )
    assert not (shadow_workspace / ".life_learning").exists()


async def test_review_tool_fails_closed_for_proposal_before_migration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _workspace(tmp_path)
    scheduler = _scheduler(tmp_path)
    config = LifeEngineConfig()
    config.settings.workspace_path = str(tmp_path)
    tool = LifeReviewSubjectDocumentTool(
        plugin=SimpleNamespace(config=config),
    )
    monkeypatch.setattr(learning_tools, "_get_scheduler", lambda _plugin: scheduler)
    monkeypatch.setattr(
        learning_tools,
        "_decision_actor",
        lambda _tool: (None, "consciousness-1"),
    )
    current = (tmp_path / "MEMORY.md").read_bytes()
    current_hash = hashlib.sha256(current).hexdigest()

    ok, error = await tool.execute(
        action="propose",
        target_path="MEMORY.md",
        expected_subject_revision="a" * 64,
        reviewed_content_sha256=current_hash,
        reason="I want to consider a different interpretation.",
        proposed_content="# MEMORY.md\nnew interpretation\n",
    )

    assert ok is False
    assert "SubjectAuthorityMigrationRequired" in str(error)
    assert (tmp_path / "MEMORY.md").read_bytes() == current
    assert not (tmp_path / ".life_learning" / "subject_reviews.jsonl").exists()


async def test_review_tool_records_unchanged_against_exact_hash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _workspace(tmp_path)
    scheduler = _scheduler(tmp_path)
    config = LifeEngineConfig()
    config.settings.workspace_path = str(tmp_path)
    tool = LifeReviewSubjectDocumentTool(plugin=SimpleNamespace(config=config))
    monkeypatch.setattr(learning_tools, "_get_scheduler", lambda _plugin: scheduler)
    monkeypatch.setattr(
        learning_tools,
        "_decision_actor",
        lambda _tool: (None, "consciousness-1"),
    )
    current = (tmp_path / "USER.md").read_bytes()

    ok, payload = await tool.execute(
        action="unchanged",
        target_path="USER.md",
        expected_subject_revision="a" * 64,
        reviewed_content_sha256=hashlib.sha256(current).hexdigest(),
        reason="This still describes the relationship as I understand it.",
    )

    assert ok is True
    assert isinstance(payload, dict)
    assert payload["action"] == "subject_review_unchanged"
    assert payload["authority_status"] == "migration_required"
    assert (tmp_path / "USER.md").read_bytes() == current


async def test_selected_review_proposes_candidate_without_writing_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _workspace(tmp_path)
    scheduler = _scheduler(tmp_path)
    event_store = _LearningEventStore()
    ledger = _Ledger(event_store)
    scheduler._learning_event_store = event_store
    scheduler.decision_ledger = ledger  # type: ignore[assignment]
    config = LifeEngineConfig()
    config.settings.workspace_path = str(tmp_path)
    tool = LifeReviewSubjectDocumentTool(plugin=SimpleNamespace(config=config))
    monkeypatch.setattr(learning_tools, "_get_scheduler", lambda _plugin: scheduler)
    monkeypatch.setattr(
        learning_tools,
        "_decision_actor",
        lambda _tool: (None, "consciousness-1"),
    )
    target = tmp_path / "MEMORY.md"
    current = target.read_bytes()

    ok, payload = await tool.execute(
        action="propose",
        target_path="MEMORY.md",
        expected_subject_revision="a" * 64,
        reviewed_content_sha256=hashlib.sha256(current).hexdigest(),
        reason="I want to keep this alternative open for a separate decision.",
        proposed_content="# MEMORY.md\nproposed interpretation\n",
    )

    assert ok is True
    assert isinstance(payload, dict)
    assert payload["status"] == "open"
    assert len(ledger.candidates) == 1
    candidate = ledger.candidates[0]
    assert getattr(candidate, "actor_consciousness_instance_id") == ("consciousness-1")
    assert getattr(candidate, "subject_revision") == "a" * 64
    assert getattr(candidate, "target_path") == "MEMORY.md"
    assert target.read_bytes() == current
    assert not (tmp_path / ".life_learning" / "subject_reviews.jsonl").exists()


async def test_selected_review_reads_remote_memory_and_never_local_shadow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    remote = {
        "SOUL.md": b"remote soul",
        "USER.md": b"remote user",
        "MEMORY.md": b"# Remote memory\nchosen continuity\n",
    }
    (tmp_path / "MEMORY.md").write_text("LOCAL SHADOW", encoding="utf-8")

    async def read_remote() -> SubjectAuthoritySnapshot:
        return _remote_snapshot(remote)

    scheduler = LearningScheduler(
        workspace_path=tmp_path,
        current_subject_revision=lambda: _revision("a"),
        read_subject_authority=read_remote,
        validate_active_consciousness_instance=lambda actor: _is_active(actor),
    )
    event_store = _LearningEventStore()
    scheduler._learning_event_store = event_store
    scheduler.decision_ledger = _Ledger(event_store)  # type: ignore[assignment]
    config = LifeEngineConfig()
    config.settings.workspace_path = str(tmp_path)
    tool = LifeReviewSubjectDocumentTool(plugin=SimpleNamespace(config=config))
    monkeypatch.setattr(learning_tools, "_get_scheduler", lambda _plugin: scheduler)
    monkeypatch.setattr(
        learning_tools,
        "_decision_actor",
        lambda _tool: (None, "consciousness-1"),
    )

    ok, status = await tool.execute(action="status", target_path="MEMORY.md")

    assert ok is True
    assert isinstance(status, dict)
    assert status["content"] == remote["MEMORY.md"].decode("utf-8")
    assert (
        status["documents"][0]["content_sha256"]
        == hashlib.sha256(remote["MEMORY.md"]).hexdigest()
    )
    assert "LOCAL SHADOW" not in status["content"]

    ok, proposed = await tool.execute(
        action="propose",
        target_path="MEMORY.md",
        expected_subject_revision="a" * 64,
        reviewed_content_sha256=hashlib.sha256(remote["MEMORY.md"]).hexdigest(),
        reason="I choose to preserve a new memory in my remote authority.",
        proposed_content="# Remote memory\nchosen continuity\nnew memory\n",
    )

    assert ok is True
    assert isinstance(proposed, dict)
    assert proposed["status"] == "open"
    assert (tmp_path / "MEMORY.md").read_text(encoding="utf-8") == "LOCAL SHADOW"


def test_subject_memory_tools_are_available_to_chat_consciousness() -> None:
    assert "life_chatter" in LifeReviewSubjectDocumentTool.chatter_allow
    assert "life_chatter" in learning_tools.LifeListSubjectCandidatesTool.chatter_allow
    assert "life_chatter" in learning_tools.LifeReadSubjectCandidateTool.chatter_allow
    assert "life_chatter" in learning_tools.LifeDecideSubjectCandidateTool.chatter_allow


async def test_review_context_rejects_stale_revision_and_inactive_actor(
    tmp_path: Path,
) -> None:
    _workspace(tmp_path)
    scheduler = _scheduler(tmp_path)

    with pytest.raises(RuntimeError, match="LearningSubjectRevisionConflict"):
        await scheduler.validate_subject_review_context(
            actor_consciousness_instance_id="consciousness-1",
            expected_subject_revision="b" * 64,
        )
    with pytest.raises(PermissionError, match="LearningDecisionActorIsNotActive"):
        await scheduler.validate_subject_review_context(
            actor_consciousness_instance_id="inactive",
            expected_subject_revision="a" * 64,
        )


async def test_review_context_conflict_carries_actual_revision(
    tmp_path: Path,
) -> None:
    """The conflict error must embed the current revision so the model can
    re-read and retry instead of guessing (F9-A)."""

    _workspace(tmp_path)
    scheduler = _scheduler(tmp_path)

    with pytest.raises(
        RuntimeError,
        match=f"LearningSubjectRevisionConflict:actual={'a' * 64}",
    ):
        await scheduler.validate_subject_review_context(
            actor_consciousness_instance_id="consciousness-1",
            expected_subject_revision="b" * 64,
        )
