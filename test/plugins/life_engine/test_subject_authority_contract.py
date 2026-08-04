"""Atomic SubjectAuthorityPort contract over selected local storage."""

from __future__ import annotations

import asyncio
import base64
import hashlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from plugins.life_engine.storage.authority import (
    FileAuthorityRegistry,
    StaleAuthorityToken,
)
from plugins.life_engine.storage.contracts import StorageBackendRuntime
from plugins.life_engine.storage.domain_schema import ensure_presence_world_schema
from plugins.life_engine.storage.factory import (
    LocalBackendSettings,
    StorageFactorySettings,
    open_storage_backend,
)
from plugins.life_engine.storage.models import (
    BackendGeneration,
    BackendKind,
    GenerationStatus,
)
from plugins.life_engine.storage.subject_contracts import (
    AcceptSubjectCandidate,
    AppendSubjectDocumentVersion,
    SubjectAuthorityActorInactive,
    SubjectAuthorityConflict,
    SubjectAuthorityEvidenceError,
    SubjectDocumentStorePort,
)
from plugins.life_engine.storage.subject_factory import open_subject_document_store
from src.kernel.storage import canonical_json

_ACTOR = "consciousness:voice-live"
_DECISION_TIME = "2026-08-04T10:00:00+00:00"


def _generation() -> BackendGeneration:
    return BackendGeneration(
        generation_id="subject-authority-local-v1",
        backend=BackendKind.LOCAL,
        schema_version=1,
        source_snapshot_sha256="a" * 64,
        root_hashes={"subject": "b" * 64},
        frontiers={"subject": 0},
        created_at="2026-08-04T00:00:00+00:00",
        verified_at="2026-08-04T00:01:00+00:00",
        status=GenerationStatus.VERIFIED,
    )


@asynccontextmanager
async def _local_store(
    tmp_path: Path,
) -> AsyncIterator[tuple[StorageBackendRuntime, SubjectDocumentStorePort]]:
    authority_path = tmp_path / "authority.json"
    registry = FileAuthorityRegistry(authority_path)
    await registry.register_generation(_generation())
    token = await registry.activate_generation(
        _generation().generation_id,
        expected_epoch=0,
        owner_id="subject-authority-contract",
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
            fencing_token_env="TEST_SUBJECT_AUTHORITY_FENCE",
            local=LocalBackendSettings(
                database_path=tmp_path / "subject-authority.sqlite3",
                authority_state_path=authority_path,
            ),
        ),
        environment={"TEST_SUBJECT_AUTHORITY_FENCE": token.fencing_token},
    )
    store = await open_subject_document_store(runtime, initialize_schema=True)
    await ensure_presence_world_schema(runtime)
    async with runtime.unit_of_work() as uow:
        await uow.session.execute(
            text(
                """CREATE TABLE learning_events (
                    position INTEGER PRIMARY KEY AUTOINCREMENT,
                    occurrence_id TEXT NOT NULL UNIQUE,
                    event_kind TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    recorded_at TEXT NOT NULL,
                    source TEXT NOT NULL,
                    actor_consciousness_instance_id TEXT NOT NULL DEFAULT '',
                    subject_revision TEXT NOT NULL DEFAULT '',
                    provenance_json TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    event_sha256 TEXT NOT NULL
                )"""
            )
        )
    try:
        yield runtime, store
    finally:
        await runtime.close()
        try:
            await registry.revoke(token)
        except StaleAuthorityToken:
            pass


async def _seed_authorities(store: SubjectDocumentStorePort) -> None:
    for path, content in (
        ("SOUL.md", b"# Soul\ncontinuous self\n"),
        ("USER.md", b"# User\ntrusted companion\n"),
        ("MEMORY.md", b"# Memory\nold interpretation\n"),
    ):
        await store.append_version(
            AppendSubjectDocumentVersion(
                logical_path=f"life_engine_workspace/{path}",
                expected_revision=0,
                expected_head_version_id="",
                content_bytes=content,
                occurrence_id=f"migration:{path}",
                recorded_by="storage-migration",
                recorded_source="snapshot:test",
                declared_owner="elysia",
                provenance_status="semantic_source_missing",
                encoding="utf-8",
                newline_style="lf",
            )
        )


async def _seed_active_actor(runtime: StorageBackendRuntime) -> None:
    async with runtime.unit_of_work() as uow:
        await uow.session.execute(
            text(
                """INSERT INTO consciousness_presence (
                    instance_id, kind, display_name, status, created_at,
                    last_active_at, suspended_at, stream_ids_json,
                    perception_filter_json, metadata_json, session_id,
                    process_epoch, lease_expires_at, lease_duration_seconds,
                    revision, updated_at
                ) VALUES (
                    :instance_id, 'voice', 'Voice', 'active', :now,
                    :now, '', '[]', '{}', '{}', 'session',
                    'process', '', NULL, 1, :now
                )"""
            ),
            {"instance_id": _ACTOR, "now": _DECISION_TIME},
        )


def _event_hash(material: dict[str, object]) -> str:
    return hashlib.sha256(canonical_json(material).encode()).hexdigest()


async def _insert_event(
    runtime: StorageBackendRuntime,
    *,
    occurrence_id: str,
    event_kind: str,
    occurred_at: str,
    source: str,
    actor: str,
    subject_revision: str,
    provenance: dict[str, object],
    payload: dict[str, object],
) -> None:
    material = {
        "occurrence_id": occurrence_id,
        "event_kind": event_kind,
        "occurred_at": datetime.fromisoformat(occurred_at).astimezone(UTC).isoformat(),
        "source": source,
        "actor_consciousness_instance_id": actor,
        "subject_revision": subject_revision,
        "provenance": provenance,
        "payload": payload,
    }
    async with runtime.unit_of_work() as uow:
        await uow.session.execute(
            text(
                """INSERT INTO learning_events (
                    occurrence_id, event_kind, occurred_at, recorded_at,
                    source, actor_consciousness_instance_id, subject_revision,
                    provenance_json, payload_json, event_sha256
                ) VALUES (
                    :occurrence_id, :event_kind, :occurred_at, :recorded_at,
                    :source, :actor, :subject_revision,
                    :provenance_json, :payload_json, :event_sha256
                )"""
            ),
            {
                "occurrence_id": occurrence_id,
                "event_kind": event_kind,
                "occurred_at": material["occurred_at"],
                "recorded_at": material["occurred_at"],
                "source": source,
                "actor": actor,
                "subject_revision": subject_revision,
                "provenance_json": canonical_json(provenance),
                "payload_json": canonical_json(payload),
                "event_sha256": _event_hash(material),
            },
        )


async def _seed_accept_evidence(
    runtime: StorageBackendRuntime,
    *,
    subject_revision: str,
    candidate_id: str = "candidate-1",
    candidate_occurrence: str = "candidate-occurrence-1",
    decision_occurrence: str = "decision-occurrence-1",
    accepted_content: bytes = b"# Memory\nnew self interpretation\n",
) -> AcceptSubjectCandidate:
    candidate_content = b"suggested interpretation"
    candidate_hash = hashlib.sha256(candidate_content).hexdigest()
    accepted_hash = hashlib.sha256(accepted_content).hexdigest()
    await _insert_event(
        runtime,
        occurrence_id=candidate_occurrence,
        event_kind="candidate.proposed",
        occurred_at="2026-08-04T09:59:00+00:00",
        source="learning.reflection",
        actor="",
        subject_revision=subject_revision,
        provenance={"source_occurrence_id": "experience-1"},
        payload={
            "candidate_id": candidate_id,
            "candidate_revision": 1,
            "candidate_kind": "subject_document_change",
            "candidate_sha256": candidate_hash,
            "target_path": "MEMORY.md",
            "candidate_content_base64": base64.b64encode(candidate_content).decode(),
        },
    )
    await _insert_event(
        runtime,
        occurrence_id=decision_occurrence,
        event_kind="candidate.accept_requested",
        occurred_at=_DECISION_TIME,
        source="learning.subject_decision",
        actor=_ACTOR,
        subject_revision=subject_revision,
        provenance={"surface": "voice"},
        payload={
            "candidate_id": candidate_id,
            "candidate_revision": 1,
            "candidate_sha256": candidate_hash,
            "candidate_occurrence_id": candidate_occurrence,
            "decision_kind": "accept_requested",
            "reason": "I choose this interpretation",
            "target_path": "MEMORY.md",
            "accepted_content_base64": base64.b64encode(accepted_content).decode(),
            "accepted_content_sha256": accepted_hash,
        },
    )
    return AcceptSubjectCandidate(
        candidate_id=candidate_id,
        candidate_revision=1,
        candidate_sha256=candidate_hash,
        candidate_occurrence_id=candidate_occurrence,
        decision_occurrence_id=decision_occurrence,
        actor_consciousness_instance_id=_ACTOR,
        expected_subject_revision=subject_revision,
        target_path="MEMORY.md",
        accepted_content_bytes=accepted_content,
        accepted_content_sha256=accepted_hash,
        occurred_at=_DECISION_TIME,
    )


async def test_subject_authority_accepts_active_will_with_atomic_audit(
    tmp_path: Path,
) -> None:
    async with _local_store(tmp_path) as (runtime, store):
        await _seed_authorities(store)
        await _seed_active_actor(runtime)
        previous_revision = await store.current_subject_revision()
        command = await _seed_accept_evidence(
            runtime,
            subject_revision=previous_revision,
        )

        committed = await store.accept_candidate(command)

        assert committed.previous_subject_revision == previous_revision
        assert committed.new_subject_revision == await store.current_subject_revision()
        assert committed.new_subject_revision != previous_revision
        assert committed.actor_consciousness_instance_id == _ACTOR
        assert committed.idempotent_replay is False
        memory = await store.get_head("life_engine_workspace/MEMORY.md")
        assert memory is not None
        version = await store.get_version(memory.current_version_id)
        assert version.content_bytes == command.accepted_content_bytes
        assert version.semantic_actor_id == _ACTOR
        task = await store.get_projection_task(
            "life_engine_workspace/MEMORY.md",
            committed.document_version_id,
        )
        assert task is not None and task.state == "pending"

        reopened = await open_subject_document_store(runtime)
        replay = await reopened.accept_candidate(command)
        assert replay.idempotent_replay is True
        assert replay.document_version_id == committed.document_version_id
        assert (await store.get_head("life_engine_workspace/MEMORY.md")) == memory
        conflicting_content = b"# Memory\nconflicting replay\n"
        with pytest.raises(SubjectAuthorityConflict, match="identity conflict"):
            await reopened.accept_candidate(
                replace(
                    command,
                    accepted_content_bytes=conflicting_content,
                    accepted_content_sha256=hashlib.sha256(
                        conflicting_content
                    ).hexdigest(),
                )
            )

        async with runtime.unit_of_work() as uow:
            with pytest.raises(DBAPIError, match="SubjectAuthorityDecisionImmutable"):
                await uow.session.execute(
                    text(
                        """UPDATE subject_authority_decisions
                        SET candidate_id = 'tampered'
                        WHERE decision_occurrence_id = :decision"""
                    ),
                    {"decision": command.decision_occurrence_id},
                )


async def test_subject_authority_fails_closed_for_actor_evidence_and_cas(
    tmp_path: Path,
) -> None:
    async with _local_store(tmp_path) as (runtime, store):
        await _seed_authorities(store)
        await _seed_active_actor(runtime)
        revision = await store.current_subject_revision()
        command = await _seed_accept_evidence(runtime, subject_revision=revision)
        before = await store.get_head("life_engine_workspace/MEMORY.md")

        async with runtime.unit_of_work() as uow:
            await uow.session.execute(
                text(
                    """UPDATE consciousness_presence SET status = 'suspended'
                    WHERE instance_id = :actor"""
                ),
                {"actor": _ACTOR},
            )
        with pytest.raises(SubjectAuthorityActorInactive):
            await store.accept_candidate(command)
        assert await store.get_head("life_engine_workspace/MEMORY.md") == before

        async with runtime.unit_of_work() as uow:
            await uow.session.execute(
                text(
                    """UPDATE consciousness_presence SET status = 'active'
                    WHERE instance_id = :actor"""
                ),
                {"actor": _ACTOR},
            )
            await uow.session.execute(
                text(
                    """UPDATE learning_events SET event_sha256 = :bad
                    WHERE occurrence_id = :candidate"""
                ),
                {"bad": "0" * 64, "candidate": command.candidate_occurrence_id},
            )
        with pytest.raises(SubjectAuthorityEvidenceError, match="hash mismatch"):
            await store.accept_candidate(command)
        assert await store.get_head("life_engine_workspace/MEMORY.md") == before


async def test_subject_authority_concurrent_stale_decisions_have_one_winner(
    tmp_path: Path,
) -> None:
    async with _local_store(tmp_path) as (runtime, store):
        await _seed_authorities(store)
        await _seed_active_actor(runtime)
        revision = await store.current_subject_revision()
        first = await _seed_accept_evidence(runtime, subject_revision=revision)
        second = await _seed_accept_evidence(
            runtime,
            subject_revision=revision,
            candidate_id="candidate-2",
            candidate_occurrence="candidate-occurrence-2",
            decision_occurrence="decision-occurrence-2",
            accepted_content=b"# Memory\nanother chosen interpretation\n",
        )

        results = await asyncio.gather(
            store.accept_candidate(first),
            store.accept_candidate(second),
            return_exceptions=True,
        )

        assert sum(not isinstance(result, Exception) for result in results) == 1
        failure = next(result for result in results if isinstance(result, Exception))
        assert isinstance(failure, SubjectAuthorityConflict)
        history = await store.list_history(
            "life_engine_workspace/MEMORY.md",
            limit=10,
        )
        assert len(history) == 2
