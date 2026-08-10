"""Tool-level contracts for subject-owned long-memory continuity."""

from __future__ import annotations

import hashlib
from types import SimpleNamespace
from typing import Any

import pytest

from plugins.life_engine.learning.decisions import (
    LearningCandidate,
    LearningDecisionReceipt,
)
from plugins.life_engine.memory import boundary_tools
from plugins.life_engine.memory.boundary import (
    MEMORY_BOUNDARY_ARTIFACT_KIND,
    MemoryBoundaryManifest,
    MemoryBoundaryNotFound,
    MemoryBoundarySegment,
    StoredMemoryBoundary,
    memory_boundary_uri,
)
from plugins.life_engine.memory.boundary_tools import (
    MEMORY_BOUNDARY_TOOLS,
    LifeCreateMemoryBoundaryTool,
    LifeInspectMemoryContinuityTool,
    LifeProposeMemoryContinuityRevisionTool,
)
from plugins.life_engine.memory.living import new_artifact_version
from plugins.life_engine.service.tool_manifests import get_tool_manifest

SUBJECT_REVISION = "c" * 64


def _stored(manifest: MemoryBoundaryManifest) -> StoredMemoryBoundary:
    artifact = new_artifact_version(
        logical_key=manifest.logical_key,
        artifact_kind=MEMORY_BOUNDARY_ARTIFACT_KIND,
        content=manifest.canonical_json,
        authored_by=manifest.actor_id,
        consciousness_instance_id=manifest.consciousness_instance_id,
        stream_scope=manifest.stream_scope,
        visibility=manifest.visibility,
    )
    return StoredMemoryBoundary(
        manifest=manifest,
        artifact=artifact,
        head_revision=manifest.manifest_revision,
        exact_uri=memory_boundary_uri(
            manifest.boundary_id,
            artifact.artifact_id,
            manifest.root_sha256,
        ),
    )


class _Repository:
    def __init__(self) -> None:
        self.records: dict[str, StoredMemoryBoundary] = {}
        self.appended: list[MemoryBoundaryManifest] = []

    async def append(
        self,
        manifest: MemoryBoundaryManifest,
        *,
        expected_head_revision: int,
    ) -> StoredMemoryBoundary:
        assert expected_head_revision == manifest.manifest_revision - 1
        self.appended.append(manifest)
        stored = _stored(manifest)
        self.records[stored.exact_uri] = stored
        return stored

    async def read_exact(self, uri: str) -> StoredMemoryBoundary:
        if uri not in self.records:
            raise MemoryBoundaryNotFound(uri)
        return self.records[uri]


class _Ledger:
    def __init__(self) -> None:
        self.candidates: list[LearningCandidate] = []

    async def append_candidate(
        self,
        candidate: LearningCandidate,
    ) -> LearningDecisionReceipt:
        self.candidates.append(candidate)
        return LearningDecisionReceipt(
            candidate_id=candidate.candidate_id,
            candidate_revision=candidate.candidate_revision,
            candidate_sha256=candidate.candidate_sha256,
            status="open",
            decision_occurrence_id="",
        )


class _Scheduler:
    def __init__(
        self,
        memory: bytes,
        ledger: _Ledger | None = None,
        *,
        review_error: Exception | None = None,
    ) -> None:
        self.memory = memory
        self.decision_ledger = ledger
        self.review_outcomes: list[dict[str, Any]] = []
        self.review_error = review_error

    async def read_subject_document_with_identity(
        self,
        path: str,
    ) -> tuple[bytes, str, str]:
        assert path == "MEMORY.md"
        return self.memory, "subject-memory-version-3", SUBJECT_REVISION

    async def current_subject_revision(self) -> str:
        return SUBJECT_REVISION

    async def validate_subject_review_context(
        self,
        *,
        actor_consciousness_instance_id: str,
        expected_subject_revision: str,
    ) -> str:
        assert actor_consciousness_instance_id == "chat-main"
        if expected_subject_revision != SUBJECT_REVISION:
            raise RuntimeError("LearningSubjectRevisionConflict")
        return SUBJECT_REVISION

    async def record_subject_review_outcome(self, **kwargs: Any) -> dict[str, Any]:
        if self.review_error is not None:
            raise self.review_error
        self.review_outcomes.append(dict(kwargs))
        return dict(kwargs)


def _runtime(
    repository: _Repository,
    scheduler: _Scheduler,
) -> boundary_tools._BoundaryToolRuntime:
    return boundary_tools._BoundaryToolRuntime(
        service=SimpleNamespace(),
        memory_service=SimpleNamespace(),
        scheduler=scheduler,
        repository=repository,  # type: ignore[arg-type]
        actor_consciousness_instance_id="chat-main",
        stream_scope="chat:one",
    )


def _tool(tool_type):
    tool = tool_type(plugin=SimpleNamespace())
    tool._bind_runtime_context(
        stream_id="chat:one",
        message=SimpleNamespace(
            message_id="message:one",
            stream_id="chat:one",
            time="2026-08-10T00:00:00+00:00",
            extra={
                "life_turn_scope": {
                    "stream_id": "chat:one",
                    "turn_key": "turn:one",
                }
            },
        ),
        tool_call_id="tool-call:one",
    )
    tool._runtime_task_name = "core"
    return tool


async def test_create_boundary_records_active_actor_but_never_writes_memory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _Repository()
    scheduler = _Scheduler(b"# MEMORY\n")

    async def resolve(_tool):
        return _runtime(repository, scheduler)

    monkeypatch.setattr(boundary_tools, "_resolve_runtime", resolve)
    success, payload = await _tool(LifeCreateMemoryBoundaryTool).execute(
        boundary_id="one-morning",
        title="那一天",
        scope="当时发生的交谈",
        current_meaning="我现在愿意这样记住它。",
        non_generalization="不推及所有未来。",
        segments=[
            {
                "segment_id": "scene",
                "title": "完整场景",
                "content": "没有被摘要掉的正文。",
                "source_refs": ["experience:one"],
            }
        ],
        source_occurrence_id="event:one",
    )

    assert success is True
    assert payload["authority"] == "immutable_memory_artifact_not_MEMORY_md"
    assert payload["exact_uri"].startswith("memory://boundary/one-morning@")
    assert len(repository.appended) == 1
    manifest = repository.appended[0]
    assert manifest.actor_id == "chat-main"
    assert manifest.consciousness_instance_id == "chat-main"
    assert manifest.subject_revision == SUBJECT_REVISION
    assert scheduler.memory == b"# MEMORY\n"


async def test_inspection_reports_exact_and_broken_links_without_deleting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _Repository()
    segment = MemoryBoundarySegment.create(
        segment_id="scene",
        title="场景",
        content="完整正文",
        source_refs=("experience:one",),
        source_occurrence_ids=("event:one",),
        scope="这次经历",
        visibility="private",
    )
    manifest = MemoryBoundaryManifest(
        boundary_id="one-morning",
        manifest_revision=1,
        operation_occurrence_id="boundary:create:one",
        title="那一天",
        scope="这次经历",
        current_meaning="我现在的理解",
        non_generalization="不自动泛化",
        actor_id="elysia",
        consciousness_instance_id="chat-main",
        stream_scope="chat:one",
        decision_occurrence_id="boundary:decision:one",
        source_occurrence_id="event:one",
        subject_revision=SUBJECT_REVISION,
        segments=(segment,),
    )
    stored = _stored(manifest)
    repository.records[stored.exact_uri] = stored
    broken_uri = stored.exact_uri.replace(stored.manifest.root_sha256, "f" * 64)
    memory = (
        f"[还可取回]({stored.exact_uri})\n"
        f"[已经损坏]({broken_uri.replace('one-morning', 'broken-entry')})\n"
    ).encode()
    scheduler = _Scheduler(memory)

    async def resolve(_tool):
        return _runtime(repository, scheduler)

    monkeypatch.setattr(boundary_tools, "_resolve_runtime", resolve)
    success, payload = await _tool(LifeInspectMemoryContinuityTool).execute()

    assert success is False
    assert payload["health"]["broken"] == 1
    assert payload["health"]["automatic_deletion_recommended"] is False
    assert {item["resolution_status"] for item in payload["entries"]} == {
        "exact",
        "unresolved",
    }
    assert len(repository.records) == 1


async def test_propose_tool_validates_links_and_stops_at_open_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _Repository()
    ledger = _Ledger()
    scheduler = _Scheduler(b"# MEMORY\n", ledger)

    async def resolve(_tool):
        return _runtime(repository, scheduler)

    monkeypatch.setattr(boundary_tools, "_resolve_runtime", resolve)
    create_success, created = await _tool(LifeCreateMemoryBoundaryTool).execute(
        boundary_id="one-morning",
        title="那一天",
        scope="这次经历",
        current_meaning="我现在的理解",
        non_generalization="不自动泛化",
        segments=[
            {
                "segment_id": "scene",
                "title": "场景",
                "content": "完整正文",
                "source_refs": ["experience:one"],
            }
        ],
    )
    assert create_success
    proposed = f"# MEMORY\n\n[那一天]({created['exact_uri']})\n"

    success, payload = await _tool(LifeProposeMemoryContinuityRevisionTool).execute(
        proposed_content=proposed,
        reviewed_content_sha256=hashlib.sha256(scheduler.memory).hexdigest(),
        expected_subject_revision=SUBJECT_REVISION,
        reason="我愿意让这个边界成为当前连续性的一部分。",
    )

    assert success is True
    assert payload["status"] == "open"
    assert payload["authority"] == "candidate_only"
    assert len(ledger.candidates) == 1
    assert ledger.candidates[0].candidate_content_bytes == proposed.encode()
    assert scheduler.review_outcomes == [
        {
            "target_path": "MEMORY.md",
            "outcome": "candidate_proposed",
            "actor_consciousness_instance_id": "chat-main",
            "subject_revision": SUBJECT_REVISION,
            "occurrence_id": ledger.candidates[0].candidate_occurrence_id,
            "reason": "我愿意让这个边界成为当前连续性的一部分。",
            "candidate_id": ledger.candidates[0].candidate_id,
            "candidate_sha256": ledger.candidates[0].candidate_sha256,
        }
    ]
    assert scheduler.memory == b"# MEMORY\n"


async def test_propose_tool_refuses_unresolvable_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _Repository()
    ledger = _Ledger()
    scheduler = _Scheduler(b"# MEMORY\n", ledger)

    async def resolve(_tool):
        return _runtime(repository, scheduler)

    monkeypatch.setattr(boundary_tools, "_resolve_runtime", resolve)
    missing_uri = (
        "memory://boundary/missing@artifact_" + "b" * 64 + "#sha256=" + "a" * 64
    )
    success, payload = await _tool(LifeProposeMemoryContinuityRevisionTool).execute(
        proposed_content=f"[找不到]({missing_uri})",
        reviewed_content_sha256=hashlib.sha256(scheduler.memory).hexdigest(),
        expected_subject_revision=SUBJECT_REVISION,
        reason="测试精确目标缺失。",
    )

    assert success is False
    assert payload["error"] == "MemoryBoundaryNotFound"
    assert ledger.candidates == []


async def test_proposed_candidate_survives_review_health_projection_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _Repository()
    ledger = _Ledger()
    scheduler = _Scheduler(
        b"# MEMORY\n",
        ledger,
        review_error=RuntimeError("projection unavailable"),
    )

    async def resolve(_tool):
        return _runtime(repository, scheduler)

    monkeypatch.setattr(boundary_tools, "_resolve_runtime", resolve)
    create_success, created = await _tool(LifeCreateMemoryBoundaryTool).execute(
        boundary_id="one-morning",
        title="那一天",
        scope="这次经历",
        current_meaning="我现在的理解",
        non_generalization="不自动泛化",
        segments=[
            {
                "segment_id": "scene",
                "title": "场景",
                "content": "完整正文",
                "source_refs": ["experience:one"],
            }
        ],
    )
    assert create_success

    success, payload = await _tool(LifeProposeMemoryContinuityRevisionTool).execute(
        proposed_content=f"# MEMORY\n\n[那一天]({created['exact_uri']})\n",
        reviewed_content_sha256=hashlib.sha256(scheduler.memory).hexdigest(),
        expected_subject_revision=SUBJECT_REVISION,
        reason="我明确提出这个完整候选。",
    )

    assert success is True
    assert payload["status"] == "open"
    assert payload["review_health_warning"] == "RuntimeError"
    assert len(ledger.candidates) == 1


def test_all_boundary_tools_have_valid_schemas_and_are_registered() -> None:
    assert [item.tool_name for item in MEMORY_BOUNDARY_TOOLS] == [
        "nucleus_create_memory_boundary",
        "nucleus_read_memory_boundary",
        "nucleus_inspect_memory_continuity",
        "nucleus_propose_memory_continuity_revision",
    ]
    for tool_type in MEMORY_BOUNDARY_TOOLS:
        schema: dict[str, Any] = tool_type.to_schema()
        assert schema["function"]["name"] == f"tool-{tool_type.tool_name}"


def test_chat_can_follow_a_continuity_link_without_loading_maintenance_tools() -> None:
    manifest = get_tool_manifest("chat")

    assert "tool-nucleus_read_memory_boundary" in manifest
    assert "tool-nucleus_create_memory_boundary" not in manifest
    assert "tool-nucleus_propose_memory_continuity_revision" not in manifest
