"""End-to-end acceptance closure for bounded long-term memory continuity."""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text

from plugins.life_engine.learning.decisions import (
    LearningDecision,
    LearningDecisionConflict,
    LearningDecisionLedger,
)
from plugins.life_engine.memory.boundary import (
    MemoryBoundaryManifest,
    MemoryBoundaryRepository,
    MemoryBoundarySegment,
)
from plugins.life_engine.memory.boundary_resolver import (
    MemoryBoundaryRecallCoordinator,
    MemoryBoundaryResolver,
)
from plugins.life_engine.memory.continuity_index import (
    parse_continuity_memory_index,
)
from plugins.life_engine.memory.continuity_stewardship import (
    ContinuityMemoryStewardship,
)
from plugins.life_engine.memory.living import (
    ArtifactHeadConflict,
    create_living_memory_schema,
)
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
from plugins.life_engine.storage.learning_factory import open_learning_stores
from plugins.life_engine.storage.memory import create_local_memory_storage_bundle
from plugins.life_engine.storage.models import (
    BackendGeneration,
    BackendKind,
    GenerationStatus,
)
from plugins.life_engine.storage.subject_contracts import (
    AppendSubjectDocumentVersion,
    SubjectDocumentStorePort,
)
from plugins.life_engine.storage.subject_factory import open_subject_document_store
from src.kernel.llm.context_delivery import EffectiveContextReceipt
from src.kernel.llm.payload import ToolResult

_ACTOR = "consciousness:continuity-test"
_NOW = "2026-08-10T08:00:00+00:00"


def _generation() -> BackendGeneration:
    return BackendGeneration(
        generation_id="continuity-acceptance-local-v1",
        backend=BackendKind.LOCAL,
        schema_version=1,
        source_snapshot_sha256="1" * 64,
        root_hashes={"learning": "2" * 64, "subject": "3" * 64},
        frontiers={"learning": 0, "subject": 0},
        created_at="2026-08-10T07:58:00+00:00",
        verified_at="2026-08-10T07:59:00+00:00",
        status=GenerationStatus.VERIFIED,
    )


@asynccontextmanager
async def _selected_runtime(
    tmp_path: Path,
) -> AsyncIterator[
    tuple[StorageBackendRuntime, SubjectDocumentStorePort, LearningDecisionLedger]
]:
    authority_path = tmp_path / "authority.json"
    registry = FileAuthorityRegistry(authority_path)
    generation = _generation()
    await registry.register_generation(generation)
    token = await registry.activate_generation(
        generation.generation_id,
        expected_epoch=0,
        owner_id="continuity-acceptance-contract",
        lease_seconds=300,
        confirm_previous_writers_stopped=True,
    )
    runtime = await open_storage_backend(
        StorageFactorySettings(
            enabled=True,
            authoritative_backend=BackendKind.LOCAL,
            backend_generation=generation.generation_id,
            schema_version=1,
            authority_epoch=token.authority_epoch,
            authority_owner_id=token.owner_id,
            fencing_token_env="TEST_CONTINUITY_ACCEPTANCE_FENCE",
            local=LocalBackendSettings(
                database_path=tmp_path / "continuity.sqlite3",
                authority_state_path=authority_path,
            ),
        ),
        environment={
            "TEST_CONTINUITY_ACCEPTANCE_FENCE": token.fencing_token,
        },
    )
    subject_store = await open_subject_document_store(
        runtime,
        initialize_schema=True,
    )
    learning_store = (await open_learning_stores(runtime, initialize_schema=True)).store
    await ensure_presence_world_schema(runtime)
    try:
        yield (
            runtime,
            subject_store,
            LearningDecisionLedger(
                learning_store,
                subject_authority=subject_store,
            ),
        )
    finally:
        await runtime.close()
        try:
            await registry.revoke(token)
        except StaleAuthorityToken:
            pass


async def _seed_subject_authority(store: SubjectDocumentStorePort) -> None:
    for path, content in (
        ("SOUL.md", b"# SOUL\ncontinuous self\n"),
        ("USER.md", b"# USER\ntrusted companion\n"),
        ("MEMORY.md", b"# MEMORY\n"),
    ):
        await store.append_version(
            AppendSubjectDocumentVersion(
                logical_path=f"life_engine_workspace/{path}",
                expected_revision=0,
                expected_head_version_id="",
                content_bytes=content,
                occurrence_id=f"continuity-seed:{path}",
                recorded_by="test-migration",
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
                    :instance_id, 'chat', 'Continuity', 'active', :now,
                    :now, '', '[]', '{}', '{}', 'session',
                    'process', '', NULL, 1, :now
                )"""
            ),
            {"instance_id": _ACTOR, "now": _NOW},
        )


class _LivingRecallAdapter:
    def __init__(self, living: Any) -> None:
        self._living = living

    async def begin_memory_recall(self, **kwargs: Any):
        return await self._living.begin_recall(**kwargs)

    async def append_memory_recall_events(self, events):
        return await self._living.append_recall_events(events)

    async def append_memory_corecall(self, event):
        return await self._living.append_corecall(event)


async def test_long_memory_boundary_survives_subject_acceptance_and_exact_recall(
    tmp_path: Path,
) -> None:
    boundary_db_path = tmp_path / "continuity-memory.sqlite3"
    boundary_db: sqlite3.Connection | None = sqlite3.connect(
        boundary_db_path,
        check_same_thread=False,
    )
    boundary_db.row_factory = sqlite3.Row
    create_living_memory_schema(boundary_db)
    living = create_local_memory_storage_bundle(lambda: boundary_db).living
    repository = MemoryBoundaryRepository(living)
    complete_text = (
        "那天的完整对话没有被摘要替代。她先确认发生过什么，"
        "再写下自己现在怎样理解它。🌸\n"
    ) * 180

    try:
        async with _selected_runtime(tmp_path) as (
            runtime,
            subject_store,
            decision_ledger,
        ):
            await _seed_subject_authority(subject_store)
            await _seed_active_actor(runtime)
            subject_revision = await subject_store.current_subject_revision()
            boundary = await repository.append(
                MemoryBoundaryManifest(
                    boundary_id="one-complete-conversation",
                    manifest_revision=1,
                    operation_occurrence_id="boundary:create:conversation-one",
                    title="一次仍能完整回去看的对话",
                    scope="这次对话本身和我此刻写下的理解",
                    current_meaning="我愿意让它留下清晰边界，而不是只剩一句结论。",
                    non_generalization="它不能替我概括所有关系或所有未来。",
                    actor_id="elysia",
                    consciousness_instance_id=_ACTOR,
                    stream_scope="chat:continuity",
                    decision_occurrence_id="boundary:decision:conversation-one",
                    source_occurrence_id="message:conversation-one",
                    subject_revision=subject_revision,
                    segments=(
                        MemoryBoundarySegment.create(
                            segment_id="complete-dialogue",
                            title="完整对话与当时感受",
                            content=complete_text,
                            source_refs=("experience:conversation-one",),
                            source_occurrence_ids=("message:conversation-one",),
                            scope="这次对话",
                            visibility="private",
                        ),
                    ),
                    visibility="private",
                ),
                expected_head_revision=0,
                recorded_at=_NOW,
            )
            reopened_repository = MemoryBoundaryRepository(
                create_local_memory_storage_bundle(lambda: boundary_db).living
            )
            assert (
                await reopened_repository.read_exact(boundary.exact_uri)
            ).manifest == boundary.manifest
            with pytest.raises(ArtifactHeadConflict):
                await reopened_repository.append(
                    replace(
                        boundary.manifest,
                        manifest_revision=2,
                        operation_occurrence_id=(
                            "boundary:create:conversation-one:stale"
                        ),
                        decision_occurrence_id=(
                            "boundary:decision:conversation-one:stale"
                        ),
                        current_meaning="这个并发版本没有读到最新 head。",
                    ),
                    expected_head_revision=0,
                )
            current_memory = b"# MEMORY\n"
            proposed_memory = (
                "# MEMORY\n\n"
                f"[我愿意沿着这个边界回到那次完整对话]({boundary.exact_uri})\n"
            ).encode()
            proposal = await ContinuityMemoryStewardship(
                repository,
                decision_ledger,
            ).propose(
                current_memory_bytes=current_memory,
                current_memory_version_id="continuity-seed:MEMORY.md",
                reviewed_current_memory_sha256=hashlib.sha256(
                    current_memory
                ).hexdigest(),
                proposed_memory_bytes=proposed_memory,
                unified_subject_revision=subject_revision,
                actor_consciousness_instance_id=_ACTOR,
                source_occurrence_id="message:review-memory",
                proposal_occurrence_id="subject-review:memory:conversation-one",
                reason="我选择把完整正文留在边界里，让当前 MEMORY 保持一条可追溯索引。",
                stream_scope="chat:continuity",
            )

            assert proposal.receipt.status == "open"
            unchanged_head = await subject_store.get_head(
                "life_engine_workspace/MEMORY.md"
            )
            assert unchanged_head is not None
            unchanged_version = await subject_store.get_version(
                unchanged_head.current_version_id
            )
            assert unchanged_version.content_bytes == current_memory

            candidate = await decision_ledger.read_candidate(
                proposal.candidate.candidate_id
            )
            assert candidate is not None
            decision = LearningDecision(
                decision_occurrence_id="subject-decision:memory:conversation-one",
                decision_kind="accept_requested",
                candidate_id=candidate.candidate_id,
                candidate_revision=candidate.candidate_revision,
                candidate_sha256=candidate.candidate_sha256,
                candidate_occurrence_id=candidate.candidate_occurrence_id,
                actor_consciousness_instance_id=_ACTOR,
                expected_subject_revision=subject_revision,
                occurred_at=datetime(2026, 8, 10, 8, 1, tzinfo=UTC).isoformat(),
                reason="我重新读过完整候选，现在明确接受这条索引。",
                target_path="MEMORY.md",
                accepted_content_bytes=candidate.candidate_content_bytes,
                accepted_content_sha256=candidate.candidate_sha256,
                provenance={"surface": "contract-test"},
            )
            tampered_content = proposed_memory.replace(
                boundary.exact_uri.encode("utf-8"),
                b"memory://boundary/missing@artifact_missing#sha256=" + b"0" * 64,
            )
            with pytest.raises(
                LearningDecisionConflict,
                match="accepted byte-for-byte",
            ):
                await decision_ledger.accept_subject_candidate(
                    replace(
                        decision,
                        decision_occurrence_id=(
                            "subject-decision:memory:conversation-one:tampered"
                        ),
                        accepted_content_bytes=tampered_content,
                        accepted_content_sha256=hashlib.sha256(
                            tampered_content
                        ).hexdigest(),
                    )
                )
            accepted = await decision_ledger.accept_subject_candidate(decision)

            assert accepted.status == "committed"
            assert accepted.authority_occurrence_id
            accepted_head = await subject_store.get_head(
                "life_engine_workspace/MEMORY.md"
            )
            assert accepted_head is not None
            accepted_version = await subject_store.get_version(
                accepted_head.current_version_id
            )
            assert accepted_version.content_bytes == proposed_memory
            accepted_subject_revision = await subject_store.current_subject_revision()
            assert accepted_subject_revision != subject_revision

            index = parse_continuity_memory_index(
                accepted_version.content_bytes,
                subject_document_version_id=accepted_version.version_id,
                unified_subject_revision=accepted_subject_revision,
            )
            assert len(index.entries) == 1
            exact = await repository.read_exact(boundary.exact_uri)
            assert exact.manifest.root_sha256 == index.entries[0].root_sha256

            recall_coordinator = MemoryBoundaryRecallCoordinator()
            resolver = MemoryBoundaryResolver(
                repository,
                recall=_LivingRecallAdapter(living),
                coordinator=recall_coordinator,
            )
            continuation = ""
            chunks: list[str] = []
            page_number = 0
            while True:
                page = await resolver.read_segment(
                    boundary.exact_uri,
                    "complete-dialogue",
                    task_name="core",
                    consciousness_instance_id=_ACTOR,
                    stream_scope="chat:continuity",
                    continuation=continuation,
                    max_bytes=2048,
                    recall_chain_id="recall-chain:accepted-continuity",
                    delivery_occurrence_id=(
                        f"delivery:accepted-continuity:{page_number}"
                    ),
                    recorded_at="2026-08-10T08:02:00+00:00",
                )
                chunks.append(page["content"])
                expected = ToolResult(value=page).to_text()
                expected_bytes = len(expected.encode())
                expected_sha256 = hashlib.sha256(expected.encode()).hexdigest()
                assert await recall_coordinator.commit_exact(
                    str(page["memory_recall_delivery_id"]),
                    EffectiveContextReceipt(
                        delivery_id=str(page["memory_recall_delivery_id"]),
                        exact_present=True,
                        expected_utf8_bytes=expected_bytes,
                        expected_sha256=expected_sha256,
                        effective_utf8_bytes=expected_bytes,
                        effective_sha256=expected_sha256,
                        part_kind="tool_result",
                    ),
                )
                continuation = page["continuation"]
                page_number += 1
                if not continuation:
                    break
            assert "".join(chunks) == complete_text
            assert (
                boundary_db.execute(
                    "SELECT COUNT(*) FROM memory_recall_sessions"
                ).fetchone()[0]
                == 1
            )
            assert (
                boundary_db.execute(
                    "SELECT COUNT(*) FROM memory_corecall_events"
                ).fetchone()[0]
                == 1
            )

        boundary_db.close()
        boundary_db = None
        reopened_db = sqlite3.connect(boundary_db_path, check_same_thread=False)
        reopened_db.row_factory = sqlite3.Row
        try:
            reopened_living = create_local_memory_storage_bundle(
                lambda: reopened_db
            ).living
            reopened = MemoryBoundaryRepository(reopened_living)
            assert (
                await reopened.read_exact(boundary.exact_uri)
            ).manifest.root_sha256 == boundary.manifest.root_sha256
            assert (
                reopened_db.execute(
                    "SELECT COUNT(*) FROM memory_recall_sessions"
                ).fetchone()[0]
                == 1
            )
            assert (
                reopened_db.execute(
                    "SELECT COUNT(*) FROM memory_corecall_events"
                ).fetchone()[0]
                == 1
            )
        finally:
            reopened_db.close()
    finally:
        if boundary_db is not None:
            boundary_db.close()
