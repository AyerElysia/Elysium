"""Health contracts for exact MEMORY Boundary references."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from types import SimpleNamespace

from plugins.life_engine.memory.boundary import (
    MemoryBoundaryManifest,
    MemoryBoundaryRepository,
    MemoryBoundarySegment,
)
from plugins.life_engine.memory.continuity_delivery import (
    ContinuityCandidateDeliveryCoordinator,
)
from plugins.life_engine.memory.continuity_health import (
    collect_continuity_memory_health,
)
from plugins.life_engine.memory.service import LifeMemoryService
from plugins.life_engine.storage.subject_contracts import (
    SUBJECT_AUTHORITY_PATHS,
    SubjectAuthoritySnapshot,
    SubjectDocumentCommit,
    SubjectDocumentHead,
    SubjectDocumentVersion,
    subject_authority_logical_path,
    subject_revision_from_contents,
)


def _manifest() -> MemoryBoundaryManifest:
    segment = MemoryBoundarySegment.create(
        segment_id="segment-health",
        title="完整经历",
        content="这是可以沿索引精确取回的完整记忆。",
        source_refs=("experience:health",),
        source_occurrence_ids=("occurrence-health",),
        scope="这次经历",
        visibility="private",
    )
    return MemoryBoundaryManifest(
        boundary_id="boundary-health",
        manifest_revision=1,
        operation_occurrence_id="operation-health",
        title="健康检查边界",
        scope="只用于验证精确引用",
        current_meaning="索引仍然可以打开完整正文。",
        non_generalization="健康检查不判断记忆的重要性。",
        actor_id="consciousness-health",
        consciousness_instance_id="consciousness-health",
        stream_scope="chat:health",
        decision_occurrence_id="decision-health",
        source_occurrence_id="source-health",
        subject_revision="a" * 64,
        segments=(segment,),
        visibility="private",
    )


class _SubjectStore:
    def __init__(self, contents: dict[str, bytes]) -> None:
        revision = subject_revision_from_contents(contents)  # type: ignore[arg-type]
        commits: dict[str, SubjectDocumentCommit] = {}
        for index, path in enumerate(SUBJECT_AUTHORITY_PATHS, start=1):
            content = contents[path]
            version_id = f"subject-version-{index}"
            logical_path = subject_authority_logical_path(path)
            commits[path] = SubjectDocumentCommit(
                version=SubjectDocumentVersion(
                    version_id=version_id,
                    document_id=f"subject-document-{index}",
                    logical_path=logical_path,
                    parent_version_id="",
                    occurrence_id=f"subject-occurrence-{index}",
                    semantic_actor_id="elysia",
                    semantic_source_id="subject-health-test",
                    occurred_at="2026-08-12T10:00:00+08:00",
                    recorded_by="test",
                    recorded_source="test",
                    recorded_at="2026-08-12T10:00:00+08:00",
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
                    document_id=f"subject-document-{index}",
                    logical_path=logical_path,
                    declared_owner="elysia",
                    current_version_id=version_id,
                    revision=1,
                ),
            )
        self.snapshot = SubjectAuthoritySnapshot(  # type: ignore[arg-type]
            commits=commits,
            revision=revision,
        )
        self.versions = {
            commit.version.version_id: commit.version for commit in commits.values()
        }

    async def read_subject_authority(self) -> SubjectAuthoritySnapshot:
        return self.snapshot

    async def get_version(self, version_id: str) -> SubjectDocumentVersion:
        if version_id not in self.versions:
            raise KeyError(version_id)
        return self.versions[version_id]


def _subject_source_manifest(
    *,
    source_ref: str,
    source_content: bytes,
) -> MemoryBoundaryManifest:
    segment = MemoryBoundarySegment.create(
        segment_id="segment-subject-source-health",
        title="受控主体来源",
        content=source_content.decode("utf-8"),
        source_refs=(source_ref,),
        source_occurrence_ids=("subject-occurrence-2",),
        scope="验证完整来源仍可精确读取",
        visibility="private",
    )
    return replace(
        _manifest(),
        boundary_id="boundary-subject-source-health",
        operation_occurrence_id="operation-subject-source-health",
        segments=(segment,),
    )


def _subject_source_ref(
    source_content: bytes,
    *,
    version_id: str = "subject-version-2",
    source_sha256: str | None = None,
) -> str:
    digest = hashlib.sha256(source_content).hexdigest()
    return (
        "subject://life_engine_workspace/USER.md@"
        f"{version_id}#sha256={source_sha256 or digest}"
        f"&bytes=0-{len(source_content)}&range_sha256={digest}"
    )


async def test_continuity_health_proves_every_exact_boundary_link(tmp_path) -> None:
    memory = LifeMemoryService(tmp_path)
    memory._vector_backend_enabled = False
    await memory.initialize()
    living = memory._require_memory_storage().living
    stored = await MemoryBoundaryRepository(living).append(
        _manifest(),
        expected_head_revision=0,
    )
    contents = {
        "SOUL.md": b"# SOUL\n",
        "USER.md": b"# USER\n",
        "MEMORY.md": f"# MEMORY\n[完整经历]({stored.exact_uri})\n".encode(),
    }

    health = await collect_continuity_memory_health(
        subject_store=_SubjectStore(contents),  # type: ignore[arg-type]
        living_store=living,
        delivery_coordinator=ContinuityCandidateDeliveryCoordinator(),
    )

    assert health["status"] == "healthy"
    assert health["index_entry_count"] == 1
    assert health["verified_boundary_count"] == 1
    assert health["broken_boundary_count"] == 0
    assert health["automatic_importance_judgment"] is False
    assert health["delivery"]["pending_pages"] == 0
    await memory.close()


async def test_continuity_health_reports_hash_drift_without_content(tmp_path) -> None:
    memory = LifeMemoryService(tmp_path)
    memory._vector_backend_enabled = False
    await memory.initialize()
    living = memory._require_memory_storage().living
    stored = await MemoryBoundaryRepository(living).append(
        _manifest(),
        expected_head_revision=0,
    )
    broken_uri = stored.exact_uri.rsplit("=", 1)[0] + "=" + "f" * 64
    contents = {
        "SOUL.md": b"# SOUL\n",
        "USER.md": b"# USER\n",
        "MEMORY.md": f"# MEMORY\n[完整经历]({broken_uri})\n".encode(),
    }

    health = await collect_continuity_memory_health(
        subject_store=_SubjectStore(contents),  # type: ignore[arg-type]
        living_store=living,
        delivery_coordinator=ContinuityCandidateDeliveryCoordinator(),
    )

    assert health["status"] == "degraded"
    assert health["verified_boundary_count"] == 0
    assert health["broken_boundary_count"] == 1
    assert health["boundary_error_types"]
    assert "content" not in repr(health).lower()
    await memory.close()


async def test_continuity_health_proves_exact_subject_source_version(tmp_path) -> None:
    source_content = b"# USER\nexact source bytes\n"
    memory = LifeMemoryService(tmp_path)
    memory._vector_backend_enabled = False
    await memory.initialize()
    living = memory._require_memory_storage().living
    stored = await MemoryBoundaryRepository(living).append(
        _subject_source_manifest(
            source_ref=_subject_source_ref(source_content),
            source_content=source_content,
        ),
        expected_head_revision=0,
    )
    contents = {
        "SOUL.md": b"# SOUL\n",
        "USER.md": source_content,
        "MEMORY.md": f"# MEMORY\n[完整来源]({stored.exact_uri})\n".encode(),
    }

    health = await collect_continuity_memory_health(
        subject_store=_SubjectStore(contents),  # type: ignore[arg-type]
        living_store=living,
        delivery_coordinator=ContinuityCandidateDeliveryCoordinator(),
    )

    assert health["status"] == "healthy"
    assert health["verified_subject_source_count"] == 1
    assert health["broken_subject_source_count"] == 0
    assert "exact source bytes" not in repr(health)
    await memory.close()


async def test_continuity_health_degrades_when_subject_source_is_unreachable(
    tmp_path,
) -> None:
    source_content = b"# USER\nsource must remain reachable\n"
    memory = LifeMemoryService(tmp_path)
    memory._vector_backend_enabled = False
    await memory.initialize()
    living = memory._require_memory_storage().living
    stored = await MemoryBoundaryRepository(living).append(
        _subject_source_manifest(
            source_ref=_subject_source_ref(
                source_content,
                version_id="missing-subject-version",
            ),
            source_content=source_content,
        ),
        expected_head_revision=0,
    )
    contents = {
        "SOUL.md": b"# SOUL\n",
        "USER.md": source_content,
        "MEMORY.md": f"# MEMORY\n[断开的来源]({stored.exact_uri})\n".encode(),
    }

    health = await collect_continuity_memory_health(
        subject_store=_SubjectStore(contents),  # type: ignore[arg-type]
        living_store=living,
        delivery_coordinator=ContinuityCandidateDeliveryCoordinator(),
    )

    assert health["status"] == "degraded"
    assert health["verified_boundary_count"] == 1
    assert health["broken_subject_source_count"] == 1
    assert health["subject_source_error_types"] == {"KeyError": 1}
    assert "source must remain reachable" not in repr(health)
    await memory.close()


async def test_continuity_health_degrades_when_subject_source_hash_drifts(
    tmp_path,
) -> None:
    source_content = b"# USER\nsource hash must remain exact\n"
    memory = LifeMemoryService(tmp_path)
    memory._vector_backend_enabled = False
    await memory.initialize()
    living = memory._require_memory_storage().living
    stored = await MemoryBoundaryRepository(living).append(
        _subject_source_manifest(
            source_ref=_subject_source_ref(
                source_content,
                source_sha256="f" * 64,
            ),
            source_content=source_content,
        ),
        expected_head_revision=0,
    )
    contents = {
        "SOUL.md": b"# SOUL\n",
        "USER.md": source_content,
        "MEMORY.md": f"# MEMORY\n[哈希漂移]({stored.exact_uri})\n".encode(),
    }

    health = await collect_continuity_memory_health(
        subject_store=_SubjectStore(contents),  # type: ignore[arg-type]
        living_store=living,
        delivery_coordinator=ContinuityCandidateDeliveryCoordinator(),
    )

    assert health["status"] == "degraded"
    assert health["broken_subject_source_count"] == 1
    assert health["subject_source_error_types"] == {
        "ContinuitySourceReferenceError": 1
    }
    assert "source hash must remain exact" not in repr(health)
    await memory.close()


async def test_continuity_health_fails_closed_on_subject_revision_drift() -> None:
    store = _SubjectStore(
        {
            "SOUL.md": b"# SOUL\n",
            "USER.md": b"# USER\n",
            "MEMORY.md": b"# MEMORY\n",
        }
    )
    store.snapshot = SimpleNamespace(
        commits=store.snapshot.commits,
        revision="f" * 64,
    )

    health = await collect_continuity_memory_health(
        subject_store=store,  # type: ignore[arg-type]
        living_store=SimpleNamespace(),  # type: ignore[arg-type]
        delivery_coordinator=ContinuityCandidateDeliveryCoordinator(),
    )

    assert health["status"] == "failed"
    assert health["error_type"] == "RuntimeError"
    assert "MEMORY" not in repr(health)
