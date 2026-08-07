"""Shared semantic contract for selectable learning event/projection storage."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, OperationalError

from plugins.life_engine.learning.decisions import (
    AcceptSubjectCandidate,
    LearningCandidate,
    LearningDecision,
    LearningDecisionConflict,
    LearningDecisionLedger,
    SubjectAuthorityCommit,
    SubjectAuthorityUnavailable,
)
from plugins.life_engine.learning.maintenance import (
    LearningMaintenanceEvent,
    LearningPhase,
)
from plugins.life_engine.learning.models import (
    Insight,
    InsightNextAction,
    InsightStatus,
)
from plugins.life_engine.learning.scheduler import LearningScheduler
from plugins.life_engine.learning.selectable import (
    SelectedLearningMaintenanceJournal,
)
from plugins.life_engine.learning.skill_store import (
    SkillCandidate,
    SkillPattern,
    SkillStore,
)
from plugins.life_engine.learning.store import InsightStore
from plugins.life_engine.storage.authority import (
    FileAuthorityRegistry,
    StaleAuthorityToken,
)
from plugins.life_engine.storage.contracts import StorageBackendRuntime
from plugins.life_engine.storage.factory import (
    LocalBackendSettings,
    StorageFactorySettings,
    open_storage_backend,
)
from plugins.life_engine.storage.learning_contracts import (
    LEARNING_WRITER_CLAIM_NAMESPACE,
    LEARNING_WRITER_CLAIM_STATE_KEY,
    LearningEventDraft,
    LearningOccurrenceConflict,
    LearningProjectionConflict,
    LearningProjectionWrite,
    LearningStorePort,
)
from plugins.life_engine.storage.learning_factory import open_learning_stores
from plugins.life_engine.storage.learning_migration import (
    export_learning_legacy_snapshot,
    import_legacy_learning_snapshot,
    verify_learning_legacy_export,
    verify_legacy_learning_import,
)
from plugins.life_engine.storage.learning_schema import (
    LEARNING_SCHEMA_VERSION,
    MYSQL_LEARNING_CLAIM_GUARD_MIGRATION,
    MYSQL_LEARNING_CLAIM_GUARD_TRIGGERS,
)
from plugins.life_engine.storage.models import (
    BackendGeneration,
    BackendKind,
    GenerationStatus,
)
from plugins.life_engine.storage.writer_claims import SingletonWriterClaimLost
from scripts.migrate_life_learning import (
    _is_transient_mysql_disconnect,
    _retry_transient_mysql,
    _verify_manifest_learning_files,
)


class _SubjectAuthority:
    def __init__(self) -> None:
        self.commands: list[AcceptSubjectCandidate] = []

    async def current_subject_revision(self) -> str:
        return "a" * 64

    async def accept_candidate(
        self,
        command: AcceptSubjectCandidate,
    ) -> SubjectAuthorityCommit:
        self.commands.append(command)
        return SubjectAuthorityCommit(
            authority_occurrence_id="authority:commit:1",
            candidate_id=command.candidate_id,
            decision_occurrence_id=command.decision_occurrence_id,
            actor_consciousness_instance_id=(command.actor_consciousness_instance_id),
            previous_subject_revision=command.expected_subject_revision,
            new_subject_revision="b" * 64,
            document_version_id="subject-version:2",
            document_revision=2,
            accepted_content_sha256=command.accepted_content_sha256,
            idempotent_replay=False,
        )


def _generation() -> BackendGeneration:
    return BackendGeneration(
        generation_id="learning-local-v1",
        backend=BackendKind.LOCAL,
        schema_version=1,
        source_snapshot_sha256="1" * 64,
        root_hashes={"learning": "2" * 64},
        frontiers={"learning": 0},
        created_at="2026-08-04T00:00:00+00:00",
        verified_at="2026-08-04T00:01:00+00:00",
        status=GenerationStatus.VERIFIED,
    )


@asynccontextmanager
async def _local_store(
    tmp_path: Path,
) -> AsyncIterator[
    tuple[StorageBackendRuntime, LearningStorePort, FileAuthorityRegistry, object]
]:
    authority_path = tmp_path / "authority.json"
    registry = FileAuthorityRegistry(authority_path)
    await registry.register_generation(_generation())
    token = await registry.activate_generation(
        _generation().generation_id,
        expected_epoch=0,
        owner_id="learning-contract",
        lease_seconds=300,
        confirm_previous_writers_stopped=True,
    )
    runtime = await open_storage_backend(
        StorageFactorySettings(
            enabled=True,
            authoritative_backend=BackendKind.LOCAL,
            backend_generation=_generation().generation_id,
            schema_version=1,
            authority_epoch=token.authority_epoch,
            authority_owner_id=token.owner_id,
            fencing_token_env="TEST_LEARNING_FENCE",
            local=LocalBackendSettings(
                database_path=tmp_path / "life.sqlite3",
                authority_state_path=authority_path,
            ),
        ),
        environment={"TEST_LEARNING_FENCE": token.fencing_token},
    )
    stores = await open_learning_stores(runtime, initialize_schema=True)
    try:
        yield runtime, stores.store, registry, token
    finally:
        await runtime.close()
        try:
            await registry.revoke(token)
        except StaleAuthorityToken:
            pass


def _event(identity: str, *, value: str = "evidence") -> LearningEventDraft:
    return LearningEventDraft(
        occurrence_id=f"learning:{identity}",
        event_kind="insight.observed",
        occurred_at="2026-08-04T01:02:03.123456+00:00",
        source="contract.learning",
        actor_consciousness_instance_id="instance:contract",
        subject_revision="a" * 64,
        provenance={"source_occurrence_id": f"source:{identity}"},
        payload={"value": value},
    )


def _projection(
    *,
    expected_revision: int,
    expected_frontier: int,
    value: str,
    state: str = "ready",
) -> LearningProjectionWrite:
    return LearningProjectionWrite(
        projection_name="learning_state",
        expected_revision=expected_revision,
        expected_source_frontier=expected_frontier,
        schema_version=1,
        projector_version="learning-test-v1",
        rebuild_state=state,
        payload={"value": value},
    )


async def test_learning_commit_is_atomic_idempotent_and_conflict_explicit(
    tmp_path: Path,
) -> None:
    async with _local_store(tmp_path) as (_, store, _, _):
        first = await store.commit(
            events=[_event("first")],
            projections=[
                _projection(
                    expected_revision=0,
                    expected_frontier=0,
                    value="one",
                )
            ],
        )
        replay = await store.commit(
            events=[_event("first")],
            projections=[
                _projection(
                    expected_revision=0,
                    expected_frontier=0,
                    value="one",
                )
            ],
        )
        assert replay == first
        assert first.events[0].position > 0
        assert first.projections[0].revision == 1
        assert first.projections[0].source_frontier == first.events[0].position

        with pytest.raises(LearningOccurrenceConflict):
            await store.commit(
                events=[_event("rolled-back"), _event("first", value="changed")],
                projections=[],
            )
        assert await store.event_by_occurrence("learning:rolled-back") is None

        with pytest.raises(LearningProjectionConflict) as raised:
            await store.commit(
                events=[_event("cas-rolled-back")],
                projections=[
                    _projection(
                        expected_revision=0,
                        expected_frontier=0,
                        value="stale",
                    )
                ],
            )
        diagnostic = raised.value.diagnostic()
        assert diagnostic == {
            "error_type": "LearningProjectionConflict",
            "projection_name": "learning_state",
            "expected_revision": 0,
            "expected_source_frontier": 0,
            "actual_revision": 1,
            "actual_source_frontier": first.events[0].position,
            "actual_projection_sha256": first.projections[0].projection_sha256,
        }
        assert await store.event_by_occurrence("learning:cas-rolled-back") is None


async def test_learning_projection_revision_frontier_and_rebuild_state(
    tmp_path: Path,
) -> None:
    async with _local_store(tmp_path) as (_, store, _, _):
        first = await store.commit(
            events=[_event("first")],
            projections=[
                _projection(
                    expected_revision=0,
                    expected_frontier=0,
                    value="one",
                )
            ],
        )
        current = first.projections[0]
        rebuilding = (
            await store.commit(
                events=[],
                projections=[
                    _projection(
                        expected_revision=current.revision,
                        expected_frontier=current.source_frontier,
                        value="one",
                        state="rebuilding",
                    )
                ],
            )
        ).projections[0]
        assert rebuilding.revision == 2
        assert rebuilding.source_frontier == current.source_frontier
        assert rebuilding.rebuild_state == "rebuilding"

        with pytest.raises(LearningProjectionConflict):
            await store.commit(
                events=[],
                projections=[
                    _projection(
                        expected_revision=1,
                        expected_frontier=current.source_frontier,
                        value="forged",
                    )
                ],
            )

        failed = (
            await store.commit(
                events=[],
                projections=[
                    _projection(
                        expected_revision=rebuilding.revision,
                        expected_frontier=rebuilding.source_frontier,
                        value="one",
                        state="failed",
                    )
                ],
            )
        ).projections[0]
        health = await store.health_snapshot()
        assert failed.rebuild_state == "failed"
        assert health["status"] == "failed"
        assert health["projection_states"]["failed"] == 1
        assert "value" not in str(health)


async def test_learning_portable_identifier_and_query_limits_fail_closed(
    tmp_path: Path,
) -> None:
    async with _local_store(tmp_path) as (_, store, _, _):
        with pytest.raises(ValueError, match="occurrence_id exceeds"):
            await store.commit(
                events=[replace(_event("bounded"), occurrence_id="x" * 256)],
                projections=[],
            )
        with pytest.raises(ValueError, match="event_kind exceeds"):
            await store.commit(
                events=[replace(_event("kind"), event_kind="x" * 129)],
                projections=[],
            )
        with pytest.raises(ValueError, match="projection_name exceeds"):
            await store.commit(
                events=[],
                projections=[
                    replace(
                        _projection(
                            expected_revision=0,
                            expected_frontier=0,
                            value="bounded",
                        ),
                        projection_name="x" * 129,
                    )
                ],
            )
        with pytest.raises(ValueError, match="query limits"):
            await store.read_events(0, event_kinds=("x" * 129,))

        assert await store.read_events(0) == []


async def test_learning_stable_pagination_and_occurrence_lookup(tmp_path: Path) -> None:
    async with _local_store(tmp_path) as (_, store, _, _):
        records = (
            await store.commit(
                events=[_event("a"), _event("b"), _event("c")],
                projections=[],
            )
        ).events
        first_page = await store.read_events(0, limit=2)
        second_page = await store.read_events(first_page[-1].position, limit=2)
        assert [record.occurrence_id for record in first_page] == [
            "learning:a",
            "learning:b",
        ]
        assert second_page == [records[-1]]
        assert (
            await store.read_events(
                0,
                limit=10,
                event_kinds=("unknown.kind",),
            )
            == []
        )
        assert await store.event_by_occurrence("learning:b") == records[1]


async def test_learning_concurrent_projection_cas_and_database_immutability(
    tmp_path: Path,
) -> None:
    async with _local_store(tmp_path) as (runtime, store, _, _):
        current = (
            await store.commit(
                events=[_event("base")],
                projections=[
                    _projection(
                        expected_revision=0,
                        expected_frontier=0,
                        value="base",
                    )
                ],
            )
        ).projections[0]

        async def advance(label: str) -> str:
            try:
                await store.commit(
                    events=[_event(f"race-{label}")],
                    projections=[
                        _projection(
                            expected_revision=current.revision,
                            expected_frontier=current.source_frontier,
                            value=label,
                        )
                    ],
                )
            except LearningProjectionConflict:
                return "conflict"
            return "committed"

        assert sorted(await asyncio.gather(advance("a"), advance("b"))) == [
            "committed",
            "conflict",
        ]

        assert runtime.engine is not None
        with pytest.raises(DBAPIError, match="LearningEventImmutable"):
            async with runtime.engine.begin() as connection:
                await connection.execute(
                    text(
                        "UPDATE learning_events SET source = 'forged' "
                        "WHERE occurrence_id = 'learning:base'"
                    )
                )
        with pytest.raises(DBAPIError, match="LearningEventImmutable"):
            async with runtime.engine.begin() as connection:
                await connection.execute(
                    text(
                        "DELETE FROM learning_events "
                        "WHERE occurrence_id = 'learning:base'"
                    )
                )


async def test_learning_restart_and_stale_writer_fencing(tmp_path: Path) -> None:
    async with _local_store(tmp_path) as (_runtime, store, registry, token):
        persisted = (
            await store.commit(events=[_event("restart")], projections=[])
        ).events[0]
        settings = StorageFactorySettings(
            enabled=True,
            authoritative_backend=BackendKind.LOCAL,
            backend_generation=_generation().generation_id,
            schema_version=1,
            authority_epoch=token.authority_epoch,
            authority_owner_id=token.owner_id,
            fencing_token_env="TEST_LEARNING_FENCE",
            local=LocalBackendSettings(
                database_path=tmp_path / "life.sqlite3",
                authority_state_path=tmp_path / "authority.json",
            ),
        )
        second_runtime = await open_storage_backend(
            settings,
            environment={"TEST_LEARNING_FENCE": token.fencing_token},
        )
        try:
            second = await open_learning_stores(second_runtime)
            assert (
                await second.store.event_by_occurrence(persisted.occurrence_id)
                == persisted
            )
        finally:
            await second_runtime.close()

        await registry.revoke(token)
        with pytest.raises(RuntimeError):
            await store.commit(
                events=[replace(_event("stale"), payload={"value": "stale"})],
                projections=[],
            )


def _candidate() -> LearningCandidate:
    return LearningCandidate.create(
        candidate_id="candidate:self-knowledge:1",
        candidate_revision=1,
        candidate_occurrence_id="candidate-occurrence:1",
        candidate_kind="subject_document_change",
        candidate_content_bytes=b"suggested wording",
        source_occurrence_id="learning-compression:1",
        source="learning.compression",
        subject_revision="a" * 64,
        target_path="MEMORY.md",
        occurred_at="2026-08-04T03:00:00+00:00",
        provenance={"algorithm": "compression-v1"},
    )


def _decision(
    *,
    kind: str = "accept_requested",
    occurrence_id: str = "decision-occurrence:1",
    revision: str = "a" * 64,
    accepted: bytes = b"my rewritten wording",
) -> LearningDecision:
    candidate = _candidate()
    return LearningDecision(
        decision_occurrence_id=occurrence_id,
        decision_kind=kind,  # type: ignore[arg-type]
        candidate_id=candidate.candidate_id,
        candidate_revision=candidate.candidate_revision,
        candidate_sha256=candidate.candidate_sha256,
        candidate_occurrence_id=candidate.candidate_occurrence_id,
        actor_consciousness_instance_id="instance:active-subject",
        expected_subject_revision=revision,
        occurred_at="2026-08-04T03:05:00+00:00",
        reason="This is how I choose to express it.",
        target_path="MEMORY.md" if kind == "accept_requested" else None,
        accepted_content_bytes=accepted if kind == "accept_requested" else b"",
        accepted_content_sha256=(
            hashlib.sha256(accepted).hexdigest() if kind == "accept_requested" else ""
        ),
        provenance={"source_instance_id": "instance:active-subject"},
    )


async def test_learning_candidate_and_non_accepting_decisions_never_write_subject(
    tmp_path: Path,
) -> None:
    async with _local_store(tmp_path) as (_, store, _, _):
        ledger = LearningDecisionLedger(store)
        candidate = _candidate()
        first = await ledger.append_candidate(candidate)
        replay = await ledger.append_candidate(candidate)
        assert first == replay
        assert first.status == "open"

        rejected = await ledger.record_decision(
            _decision(kind="rejected", occurrence_id="decision:rejected")
        )
        assert rejected.status == "rejected"
        kinds = [record.event_kind for record in await store.read_events(0, limit=20)]
        assert kinds == ["candidate.proposed", "candidate.rejected"]
        assert "candidate.committed" not in kinds


async def test_learning_schema_relaxation_is_candidate_copy_only(
    tmp_path: Path,
) -> None:
    async with _local_store(tmp_path) as (runtime, _, _, _):
        with pytest.raises(RuntimeError, match="candidate copy"):
            await open_learning_stores(
                runtime,
                initialize_schema=True,
                require_database_immutability=False,
            )


async def test_accept_request_without_authority_remains_requested(
    tmp_path: Path,
) -> None:
    async with _local_store(tmp_path) as (_, store, _, _):
        ledger = LearningDecisionLedger(store)
        await ledger.append_candidate(_candidate())
        with pytest.raises(SubjectAuthorityUnavailable):
            await ledger.accept_subject_candidate(_decision())

        projection = await store.get_projection("learning_candidate_decisions")
        assert projection is not None
        candidate = projection.payload["candidates"][_candidate().candidate_id]
        assert candidate["status"] == "accept_requested"
        assert candidate["authority_occurrence_id"] == ""
        assert [event.event_kind for event in await store.read_events(0, limit=20)] == [
            "candidate.proposed",
            "candidate.accept_requested",
        ]


async def test_authority_commit_is_the_only_source_of_committed_status(
    tmp_path: Path,
) -> None:
    async with _local_store(tmp_path) as (_, store, _, _):
        authority = _SubjectAuthority()
        projected: list[tuple[str, SubjectAuthorityCommit]] = []

        async def project_subject(path, commit):
            projected.append((path, commit))

        ledger = LearningDecisionLedger(
            store,
            subject_authority=authority,
            project_subject_commit=project_subject,
        )
        await ledger.append_candidate(_candidate())
        receipt = await ledger.accept_subject_candidate(_decision())
        replay = await ledger.accept_subject_candidate(_decision())

        assert receipt.status == "committed"
        assert replay == receipt
        assert receipt.authority_occurrence_id == "authority:commit:1"
        assert len(authority.commands) == 1
        command = authority.commands[0]
        assert command.expected_subject_revision == "a" * 64
        assert command.candidate_sha256 == _candidate().candidate_sha256
        assert command.accepted_content_bytes == b"my rewritten wording"
        assert command.accepted_content_sha256 != command.candidate_sha256
        assert len(projected) == 1
        assert projected[0][0] == "MEMORY.md"
        assert projected[0][1].authority_occurrence_id == "authority:commit:1"
        assert [event.event_kind for event in await store.read_events(0, limit=20)] == [
            "candidate.proposed",
            "candidate.accept_requested",
            "candidate.committed",
        ]


async def test_compression_persists_reviewable_subject_candidate_without_accepting(
    tmp_path: Path,
) -> None:
    async with _local_store(tmp_path / "backend") as (_, store, _, _):
        authority = _SubjectAuthority()
        scheduler = LearningScheduler(
            workspace_path=tmp_path / "selected-workspace",
            learning_store=store,
            subject_authority=authority,
        )
        await scheduler.initialize()
        insight = Insight.create(
            category="situated observation",
            claim="A revisable observation",
            rationale="one source occurrence",
        )
        insight.status = InsightStatus.VALIDATED.value
        insight.next_action = InsightNextAction.PROMOTE.value
        assert scheduler.store.add_insight(insight)

        async def compress(**_kwargs):
            return "# Derived observation candidate\nExact candidate wording\n"

        async def recommend(**_kwargs):
            return True

        scheduler.compressor._compress = compress  # type: ignore[method-assign]
        scheduler.compressor._selection_gate = recommend  # type: ignore[method-assign]
        await scheduler._maybe_run_compression()

        assert scheduler.decision_ledger is not None
        summaries = await scheduler.decision_ledger.list_candidates(status="open")
        assert len(summaries) == 1
        candidate = await scheduler.decision_ledger.read_candidate(
            str(summaries[0]["candidate_id"])
        )
        assert candidate is not None
        assert candidate.target_path == "MEMORY.md"
        assert candidate.subject_revision == "a" * 64
        assert candidate.candidate_content_bytes == (
            b"# Derived observation candidate\nExact candidate wording\n"
        )
        assert authority.commands == []
        state = scheduler.store.load_state()
        assert state["last_knowledge_candidate_ledgered_version"] == 1
        assert not (tmp_path / "selected-workspace" / ".life_learning").exists()

        restarted = LearningScheduler(
            workspace_path=tmp_path / "restart-workspace",
            learning_store=store,
            subject_authority=authority,
        )
        await restarted.initialize()
        assert restarted.decision_ledger is not None
        assert len(await restarted.decision_ledger.list_candidates(status="open")) == 1


async def test_decision_rejects_stale_subject_revision_and_payload_reuse(
    tmp_path: Path,
) -> None:
    async with _local_store(tmp_path) as (_, store, _, _):
        ledger = LearningDecisionLedger(store)
        await ledger.append_candidate(_candidate())
        with pytest.raises(LearningDecisionConflict, match="subject revision"):
            await ledger.record_decision(_decision(revision="b" * 64))

        await ledger.record_decision(
            _decision(kind="kept_open", occurrence_id="decision:one")
        )
        with pytest.raises(LearningOccurrenceConflict):
            await ledger.record_decision(
                _decision(kind="rejected", occurrence_id="decision:one")
            )


async def test_selected_scheduler_uses_only_sql_and_survives_restart(
    tmp_path: Path,
) -> None:
    async with _local_store(tmp_path / "backend") as (_, store, _, _):
        workspace = tmp_path / "workspace"
        scheduler = LearningScheduler(
            workspace_path=workspace,
            learning_store=store,
        )
        await scheduler.initialize()
        insight = Insight.create(
            category="open reflection",
            claim="One situated observation",
            rationale="The experience left this trace",
        )
        skill = SkillPattern.create(
            name="patient-listening",
            description="Keep enough room for the other person's cadence",
        )
        assert scheduler.store.add_insight(insight) is True
        assert scheduler.skill_store.add_skill(skill) is True
        scheduler.store.save_state({"last_audit_at": "2026-08-04T04:00:00+00:00"})
        scheduler.store.write_knowledge_version(
            content="# situated self knowledge\n",
            version=1,
            insight_ids=[insight.insight_id],
            edit_count=1,
            promoted=True,
            reason="explicit test fixture",
        )
        await scheduler.flush()

        assert not (workspace / ".life_learning").exists()
        health = scheduler.get_state()["selected_persistence"]
        assert health["status"] == "healthy"
        assert health["dirty_projection_count"] == 0

        restarted = LearningScheduler(
            workspace_path=workspace,
            learning_store=store,
        )
        await restarted.initialize()
        assert restarted.store.get_insight(insight.insight_id) is not None
        assert restarted.skill_store.get_skill(skill.skill_id) is not None
        assert restarted.store.read_current_knowledge() == (
            "# situated self knowledge\n"
        )
        assert restarted.store.load_state()["last_audit_at"] == (
            "2026-08-04T04:00:00+00:00"
        )
        assert not (workspace / ".life_learning").exists()

        event_kinds = [
            event.event_kind for event in await store.read_events(0, limit=100)
        ]
        assert "learning_insights.insight_created" in event_kinds
        assert "learning_skills.skill_created" in event_kinds
        assert "learning_insights.snapshot" in event_kinds
        assert "learning_skills.snapshot" in event_kinds


async def test_legacy_learning_snapshot_import_is_lossless_idempotent_and_exportable(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "legacy-workspace"
    legacy_store = InsightStore(workspace)
    legacy_skill_store = SkillStore(workspace)
    insight = Insight.create(
        category="subject-open-wording",
        claim="A situated observation that must survive migration.",
        rationale="A concrete experience was retained.",
    )
    skill = SkillPattern.create(
        name="situated-listening",
        description="Leave room for this particular cadence.",
        instructions="Attend to the actual person and context.",
    )
    assert legacy_store.add_insight(insight) is True
    assert legacy_skill_store.add_skill(skill) is True
    legacy_store.save_state({"last_audit_at": "2026-08-04T04:00:00+00:00"})
    legacy_store.write_knowledge_version(
        content="# exact legacy self knowledge\n",
        version=1,
        insight_ids=[insight.insight_id],
        edit_count=1,
        promoted=True,
        reason="legacy fixture",
    )

    legacy_root = workspace / ".life_learning"
    (legacy_root / "future-format.bin").write_bytes(b"x" * (1024 * 1024 + 7))
    before_hashes = {
        path.relative_to(legacy_root).as_posix(): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in sorted(legacy_root.rglob("*"))
        if path.is_file()
    }

    async with _local_store(tmp_path / "backend") as (_, store, _, _):
        first = await import_legacy_learning_snapshot(
            workspace,
            store,
            subject_revision="a" * 64,
        )
        replay = await import_legacy_learning_snapshot(
            workspace,
            store,
            subject_revision="a" * 64,
        )

        assert first.snapshot_sha256 == replay.snapshot_sha256
        assert first.file_hashes == before_hashes
        assert replay.projection_revisions == first.projection_revisions
        import_verification = await verify_legacy_learning_import(workspace, store)
        assert import_verification["verified"] is True
        assert import_verification["exact_bytes_match"] is True
        migration_events = await store.read_events(0, limit=100)
        manifest_event = next(
            event
            for event in migration_events
            if event.event_kind == "legacy.snapshot.manifested"
        )
        chunk_events = [
            event
            for event in migration_events
            if event.event_kind == "legacy.snapshot.file_chunk"
        ]
        assert "content_base64" not in str(manifest_event.payload)
        assert chunk_events
        assert max(int(event.payload["size"]) for event in chunk_events) <= 1024 * 1024
        assert any(
            event.provenance.get("path") == "future-format.bin"
            for event in chunk_events
        )
        assert {
            path.relative_to(legacy_root).as_posix(): hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
            for path in sorted(legacy_root.rglob("*"))
            if path.is_file()
        } == before_hashes

        selected = LearningScheduler(
            workspace_path=tmp_path / "selected-workspace",
            learning_store=store,
        )
        await selected.initialize()
        assert selected.store.get_insight(insight.insight_id).claim == insight.claim
        assert selected.skill_store.get_skill(skill.skill_id).instructions == (
            skill.instructions
        )
        assert selected.store.read_current_knowledge() == (
            "# exact legacy self knowledge\n"
        )
        assert selected.store.load_state()["last_audit_at"] == (
            "2026-08-04T04:00:00+00:00"
        )

        exported_root = tmp_path / "exported-workspace" / ".life_learning"
        exported = await export_learning_legacy_snapshot(store, exported_root)
        assert exported.file_count > 0
        assert (await verify_learning_legacy_export(store, exported_root))[
            "verified"
        ] is True
        exported_store = InsightStore(exported_root.parent)
        exported_skills = SkillStore(exported_root.parent)
        assert exported_store.get_insight(insight.insight_id).claim == insight.claim
        assert exported_skills.get_skill(skill.skill_id).instructions == (
            skill.instructions
        )
        assert exported_store.read_current_knowledge() == (
            "# exact legacy self knowledge\n"
        )

        with pytest.raises(FileExistsError):
            await export_learning_legacy_snapshot(store, exported_root)


async def test_legacy_import_refuses_to_overwrite_selected_projection(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "legacy-workspace"
    legacy_store = InsightStore(workspace)
    assert legacy_store.add_insight(
        Insight.create(
            category="legacy",
            claim="Legacy evidence",
            rationale="Legacy source",
        )
    )

    async with _local_store(tmp_path / "backend") as (_, store, _, _):
        selected = LearningScheduler(
            workspace_path=tmp_path / "selected-workspace",
            learning_store=store,
        )
        await selected.initialize()
        assert selected.store.add_insight(
            Insight.create(
                category="selected",
                claim="Already selected evidence",
                rationale="Selected source",
            )
        )
        await selected.flush()

        before = await store.get_projection("learning_insights")
        with pytest.raises(LearningProjectionConflict, match="refuses to overwrite"):
            await import_legacy_learning_snapshot(
                workspace,
                store,
                subject_revision="a" * 64,
            )
        after = await store.get_projection("learning_insights")
        assert after == before
        assert not any(
            event.event_kind == "legacy.snapshot.imported"
            for event in await store.read_events(0, limit=100)
        )


async def test_selected_flush_keeps_mutations_arriving_during_commit(
    tmp_path: Path,
) -> None:
    """An in-flight commit must not clear a later coroutine's evidence."""

    async with _local_store(tmp_path / "backend") as (_, store, _, _):
        selected = LearningScheduler(
            workspace_path=tmp_path / "selected-workspace",
            learning_store=store,
        )
        await selected.initialize()
        first = Insight.create(
            category="concurrency",
            claim="The first mutation",
            rationale="first source",
        )
        second = Insight.create(
            category="concurrency",
            claim="The second mutation",
            rationale="second source",
        )
        assert selected.store.add_insight(first)

        original_commit = store.commit
        commit_entered = asyncio.Event()
        release_commit = asyncio.Event()
        block_once = True

        async def blocking_commit(*, events, projections):
            nonlocal block_once
            if block_once:
                block_once = False
                commit_entered.set()
                await release_commit.wait()
            return await original_commit(events=events, projections=projections)

        store.commit = blocking_commit  # type: ignore[method-assign]
        first_flush = asyncio.create_task(selected.flush())
        await commit_entered.wait()
        assert selected.store.add_insight(second)
        release_commit.set()
        await first_flush

        health = selected.get_state()["selected_persistence"]
        assert health["pending_events"] > 0
        assert health["dirty_projection_count"] == 1
        await selected.flush()

        restarted = LearningScheduler(
            workspace_path=tmp_path / "restart-workspace",
            learning_store=store,
        )
        await restarted.initialize()
        assert restarted.store.get_insight(first.insight_id) is not None
        assert restarted.store.get_insight(second.insight_id) is not None


async def test_selected_projection_conflict_is_content_free_and_stays_failed_closed(
    tmp_path: Path,
) -> None:
    """A stale writer records exact CAS evidence and never retries blindly."""

    async with _local_store(tmp_path / "backend") as (_, store, _, _):
        winner = LearningScheduler(
            workspace_path=tmp_path / "winner",
            learning_store=store,
        )
        stale = LearningScheduler(
            workspace_path=tmp_path / "stale",
            learning_store=store,
        )
        await winner.initialize()
        await stale.initialize()
        assert winner.store.add_insight(
            Insight.create(
                category="cas",
                claim="winner payload must not enter diagnostics",
                rationale="winner source",
            )
        )
        await winner.flush()
        winner_projection = await store.get_projection("learning_insights")
        assert winner_projection is not None

        assert stale.store.add_insight(
            Insight.create(
                category="cas",
                claim="stale payload must not enter diagnostics",
                rationale="stale source",
            )
        )
        with pytest.raises(LearningProjectionConflict):
            await stale.flush()

        state = stale.get_state()
        assert state["status"] == "failed"
        selected = state["selected_persistence"]
        failure = selected["failure"]
        assert selected["status"] == "failed"
        assert (
            selected["writer_instance_id"]
            != (winner.get_state()["selected_persistence"]["writer_instance_id"])
        )
        assert failure["projection_name"] == "learning_insights"
        assert failure["expected_revision"] == 0
        assert failure["expected_source_frontier"] == 0
        assert failure["actual_revision"] == winner_projection.revision
        assert failure["actual_source_frontier"] == (winner_projection.source_frontier)
        assert failure["actual_projection_sha256"] == (
            winner_projection.projection_sha256
        )
        serialized = str(failure)
        assert "winner payload" not in serialized
        assert "stale payload" not in serialized

        before = await store.health_snapshot()
        with pytest.raises(RuntimeError, match="failed closed"):
            await stale.flush()
        after = await store.health_snapshot()
        assert after["event_count"] == before["event_count"]
        assert after["event_frontier"] == before["event_frontier"]


async def test_selected_learning_writer_claim_fences_commit_before_restart(
    tmp_path: Path,
) -> None:
    """Learning commits use the service-owned claim and never rebase on loss."""

    async with _local_store(tmp_path / "backend") as (runtime, _, _, _):
        claim = await runtime.acquire_singleton_writer(
            namespace=LEARNING_WRITER_CLAIM_NAMESPACE,
            state_key=LEARNING_WRITER_CLAIM_STATE_KEY,
            owner_instance_id="life-engine:test-learning-writer",
            lease_seconds=30,
        )
        claimed = (await open_learning_stores(runtime, writer_claim=claim)).store
        first = await claimed.commit(
            events=[_event("claimed-first")],
            projections=[],
        )
        assert len(first.events) == 1
        health = await claimed.health_snapshot()
        assert health["singleton_writer"] == {
            "status": "claimed",
            "generation_id": claim.generation_id,
            "namespace": LEARNING_WRITER_CLAIM_NAMESPACE,
            "state_key": LEARNING_WRITER_CLAIM_STATE_KEY,
            "owner_instance_id": "life-engine:test-learning-writer",
            "lease_epoch": claim.lease_epoch,
        }

        assert await runtime.release_singleton_writer(claim) is True
        with pytest.raises(SingletonWriterClaimLost):
            await claimed.commit(
                events=[_event("stale-after-release")],
                projections=[],
            )
        assert await claimed.event_by_occurrence("stale-after-release") is None


async def test_learning_store_rejects_claim_from_another_singleton_scope(
    tmp_path: Path,
) -> None:
    """A live claim for another domain must not authorize Learning writes."""

    async with _local_store(tmp_path / "backend") as (runtime, _, _, _):
        wrong_claim = await runtime.acquire_singleton_writer(
            namespace="life_engine.runtime_context",
            state_key="global",
            owner_instance_id="life-engine:test-wrong-learning-scope",
            lease_seconds=30,
        )
        with pytest.raises(ValueError, match="LearningWriterClaimScopeMismatch"):
            await open_learning_stores(runtime, writer_claim=wrong_claim)


async def test_claimed_mysql_learning_startup_requires_trigger_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Business startup fails before store construction when guards are absent."""

    async def _missing_guard(_runtime: object) -> None:
        raise RuntimeError("missing Learning singleton trigger guard")

    monkeypatch.setattr(
        "plugins.life_engine.storage.learning_factory."
        "verify_learning_writer_claim_guard",
        _missing_guard,
    )
    runtime = SimpleNamespace(enabled=True, backend=BackendKind.MYSQL)
    claim = SimpleNamespace(
        namespace=LEARNING_WRITER_CLAIM_NAMESPACE,
        state_key=LEARNING_WRITER_CLAIM_STATE_KEY,
    )
    with pytest.raises(RuntimeError, match="missing Learning singleton trigger"):
        await open_learning_stores(runtime, writer_claim=claim)  # type: ignore[arg-type]


def test_learning_mysql_claim_guard_covers_all_mutation_surfaces() -> None:
    """A registered Learning scope blocks old adapters and direct SQL writes."""

    assert LEARNING_SCHEMA_VERSION == 2
    assert MYSQL_LEARNING_CLAIM_GUARD_MIGRATION.version == 2
    assert {
        (trigger.table, trigger.manipulation)
        for trigger in MYSQL_LEARNING_CLAIM_GUARD_TRIGGERS
    } == {
        ("learning_events", "INSERT"),
        ("learning_projections", "INSERT"),
        ("learning_projections", "UPDATE"),
        ("learning_projections", "DELETE"),
    }
    for statement in MYSQL_LEARNING_CLAIM_GUARD_MIGRATION.statements:
        assert LEARNING_WRITER_CLAIM_NAMESPACE in statement
        assert LEARNING_WRITER_CLAIM_STATE_KEY in statement
        assert "runtime_singleton_writer_bindings" in statement
        assert "LearningSingletonWriterClaimRequired" in statement


async def test_selected_maintenance_conflict_stops_repeated_sql_attempts(
    tmp_path: Path,
) -> None:
    """One stale journal conflict is enough; later calls stay local fail-closed."""

    async with _local_store(tmp_path / "backend") as (_, store, _, _):
        winner = SelectedLearningMaintenanceJournal(
            store,
            writer_instance_id="learning_writer_winner",
        )
        stale = SelectedLearningMaintenanceJournal(
            store,
            writer_instance_id="learning_writer_stale",
        )
        await winner.initialize()
        await stale.initialize()
        started_at = datetime.now(UTC)
        await winner.append(
            LearningMaintenanceEvent.started(
                run_id="winner-run",
                phase=LearningPhase.REFLECTION,
                started_at=started_at,
                pending_count=1,
            )
        )
        with pytest.raises(LearningProjectionConflict):
            await stale.append(
                LearningMaintenanceEvent.started(
                    run_id="stale-run",
                    phase=LearningPhase.REFLECTION,
                    started_at=started_at,
                    pending_count=1,
                )
            )
        health = stale.health_snapshot()
        assert health["status"] == "failed"
        assert health["failure"]["writer_instance_id"] == "learning_writer_stale"
        assert health["failure"]["actual_revision"] == 1

        before = await store.health_snapshot()
        with pytest.raises(RuntimeError, match="failed closed"):
            await stale.append(
                LearningMaintenanceEvent.started(
                    run_id="stale-run-2",
                    phase=LearningPhase.AUDIT,
                    started_at=started_at,
                    pending_count=0,
                )
            )
        after = await store.health_snapshot()
        assert after["event_count"] == before["event_count"]
        assert after["event_frontier"] == before["event_frontier"]


def test_learning_migration_manifest_verification_is_exact(tmp_path: Path) -> None:
    snapshot = tmp_path / "snapshot"
    source = (
        snapshot
        / "workspace"
        / "life_engine_workspace"
        / ".life_learning"
        / "state.json"
    )
    source.parent.mkdir(parents=True)
    source.write_bytes(b'{"stable": true}')
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    manifest = {
        "exact_files": [
            {
                "source_relative": "life_engine_workspace/.life_learning/state.json",
                "backup_relative": (
                    "workspace/life_engine_workspace/.life_learning/state.json"
                ),
                "bytes": source.stat().st_size,
                "sha256": digest,
            }
        ]
    }

    assert _verify_manifest_learning_files(snapshot, manifest) == {
        "state.json": digest
    }
    source.write_bytes(b'{"stable": false}')
    with pytest.raises(RuntimeError, match="differs from manifest"):
        _verify_manifest_learning_files(snapshot, manifest)


async def test_learning_migration_retries_only_dropped_mysql_connections() -> None:
    attempts = 0

    async def transient_then_success() -> str:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise OperationalError(
                "SELECT 1",
                {},
                Exception(2013, "Lost connection during query"),
            )
        return "verified"

    assert await _retry_transient_mysql(transient_then_success) == "verified"
    assert attempts == 3

    non_transient = OperationalError(
        "SELECT 1",
        {},
        Exception(1045, "Access denied"),
    )
    assert _is_transient_mysql_disconnect(non_transient) is False

    async def access_denied() -> None:
        raise non_transient

    with pytest.raises(OperationalError):
        await _retry_transient_mysql(access_denied)


async def test_selected_maintenance_health_is_restart_safe_and_content_free(
    tmp_path: Path,
) -> None:
    async with _local_store(tmp_path / "backend") as (_, store, _, _):
        workspace = tmp_path / "workspace"
        scheduler = LearningScheduler(
            workspace_path=workspace,
            learning_store=store,
        )
        await scheduler.initialize()
        await scheduler.on_heartbeat()
        health = scheduler.get_state()["maintenance"]
        assert health["journal"] == "selected_sql"
        assert health["observed_events"] > 0
        assert "situated self knowledge" not in str(health)

        restarted = LearningScheduler(
            workspace_path=workspace,
            learning_store=store,
        )
        await restarted.initialize()
        assert restarted.get_state()["maintenance"] == health
        assert not (workspace / ".life_learning").exists()


async def test_skill_candidate_requires_active_actor_and_exact_subject_revision(
    tmp_path: Path,
) -> None:
    async def current_subject_revision() -> str:
        return "a" * 64

    async def active_actor(instance_id: str) -> bool:
        return instance_id == "instance:active-subject"

    async with _local_store(tmp_path / "backend") as (_, store, _, _):
        scheduler = LearningScheduler(
            workspace_path=tmp_path / "workspace",
            learning_store=store,
            current_subject_revision=current_subject_revision,
            validate_active_consciousness_instance=active_actor,
        )
        await scheduler.initialize()
        insight = Insight.create(
            category="subject-open-wording",
            claim="A concrete way of acting may be worth keeping.",
            rationale="It came from one situated experience.",
        )
        insight.status = InsightStatus.VALIDATED.value
        insight.next_action = InsightNextAction.PROMOTE.value
        assert scheduler.store.add_insight(insight)
        candidate = SkillCandidate.create(
            candidate_id="skill-candidate:one",
            candidate_occurrence_id="skill-candidate-occurrence:one",
            subject_revision="a" * 64,
            source_occurrence_id="skill-distillation:one",
            target_skill_id="",
            proposed_skill_id="skill:one",
            name="candidate-wording",
            description="A proposed description.",
            instructions="A proposed instruction.",
            insight_ids=[insight.insight_id],
            source_event_ids=["life-event:one"],
            gate_recommended=True,
            occurred_at="2026-08-04T05:00:00+00:00",
        )
        assert scheduler.skill_store.append_candidate(candidate)
        await scheduler.flush()

        with pytest.raises(PermissionError, match="NotActive"):
            await scheduler.decide_skill_candidate(
                candidate_id=candidate.candidate_id,
                candidate_revision=candidate.candidate_revision,
                candidate_sha256=candidate.candidate_sha256,
                decision_occurrence_id="skill-decision:inactive",
                decision_kind="accepted",
                actor_consciousness_instance_id="instance:inactive",
                expected_subject_revision="a" * 64,
                reason="I choose this.",
            )
        with pytest.raises(RuntimeError, match="SubjectRevisionConflict"):
            await scheduler.decide_skill_candidate(
                candidate_id=candidate.candidate_id,
                candidate_revision=candidate.candidate_revision,
                candidate_sha256=candidate.candidate_sha256,
                decision_occurrence_id="skill-decision:stale",
                decision_kind="accepted",
                actor_consciousness_instance_id="instance:active-subject",
                expected_subject_revision="b" * 64,
                reason="I choose this.",
            )

        receipt = await scheduler.decide_skill_candidate(
            candidate_id=candidate.candidate_id,
            candidate_revision=candidate.candidate_revision,
            candidate_sha256=candidate.candidate_sha256,
            decision_occurrence_id="skill-decision:accepted",
            decision_kind="accepted",
            actor_consciousness_instance_id="instance:active-subject",
            expected_subject_revision="a" * 64,
            reason="This wording matches how I choose to act.",
            accepted_name="my-own-wording",
            accepted_description="My final description.",
            accepted_instructions="My final instructions.",
        )
        assert receipt["status"] == "accepted"
        accepted = scheduler.skill_store.get_skill("skill:one")
        assert accepted is not None
        assert accepted.name == "my-own-wording"
        assert scheduler.store.get_insight(insight.insight_id).next_action == (
            InsightNextAction.ARCHIVE.value
        )

        decision_event = await store.event_by_occurrence(
            "skill-decision:accepted"
        )
        assert decision_event is not None
        assert decision_event.actor_consciousness_instance_id == (
            "instance:active-subject"
        )
        assert decision_event.subject_revision == "a" * 64

        restarted = LearningScheduler(
            workspace_path=tmp_path / "workspace",
            learning_store=store,
            current_subject_revision=current_subject_revision,
            validate_active_consciousness_instance=active_actor,
        )
        await restarted.initialize()
        assert restarted.skill_store.get_candidate(candidate.candidate_id).status == (
            "accepted"
        )
        assert restarted.skill_store.get_skill("skill:one").instructions == (
            "My final instructions."
        )
