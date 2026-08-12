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
    LifeCreateMemoryBoundaryFromSubjectRangeTool,
    LifeCreateMemoryBoundaryTool,
    LifeInspectMemoryContinuityTool,
    LifeProposeMemoryContinuityRevisionTool,
)
from plugins.life_engine.memory.living import (
    ArtifactHeadConflict,
    new_artifact_version,
)
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

    async def read_current(self, boundary_id: str) -> StoredMemoryBoundary | None:
        return next(
            (stored for stored in self.records.values()
             if stored.manifest.boundary_id == boundary_id),
            None,
        )


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

    async def read_subject_document_snapshot(self, path: str) -> SimpleNamespace:
        assert path == "MEMORY.md"
        return SimpleNamespace(
            content_bytes=self.memory,
            version_id="subject-memory-version-3",
            source_occurrence_id="subject-memory-occurrence-3",
            unified_subject_revision=SUBJECT_REVISION,
            content_sha256=hashlib.sha256(self.memory).hexdigest(),
            byte_length=len(self.memory),
            provenance_status="complete",
        )

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


async def test_create_boundary_from_reviewed_ranges_preserves_exact_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _Repository()
    prefix = "# MEMORY\n\n常驻线索。\n\n".encode()
    first = "这是一段需要完整保留的旧看法。\n".encode()
    separator = b"\n"
    second = "这是后来出现的新解释。\n".encode()
    memory = prefix + first + separator + second
    scheduler = _Scheduler(memory)

    async def resolve(_tool):
        return _runtime(repository, scheduler)

    monkeypatch.setattr(boundary_tools, "_resolve_runtime", resolve)
    first_start = len(prefix)
    first_end = first_start + len(first)
    second_start = first_end + len(separator)
    second_end = second_start + len(second)
    success, payload = await _tool(
        LifeCreateMemoryBoundaryFromSubjectRangeTool
    ).execute(
        boundary_id="changing-view",
        title="一次看法的变化",
        scope="旧看法与后来解释的边界",
        current_meaning="我愿意保留两次理解之间的差异。",
        non_generalization="不把这一次变化泛化成所有未来。",
        segments=[
            {
                "segment_id": "earlier-view",
                "title": "当时的看法",
                "byte_start": first_start,
                "byte_end": first_end,
            },
            {
                "segment_id": "later-meaning",
                "title": "后来的解释",
                "byte_start": second_start,
                "byte_end": second_end,
            },
        ],
        expected_subject_revision=SUBJECT_REVISION,
        reviewed_memory_version_id="subject-memory-version-3",
        reviewed_content_sha256=hashlib.sha256(memory).hexdigest(),
    )

    assert success is True
    assert payload["authority"] == "immutable_memory_artifact_not_MEMORY_md"
    assert payload["source_memory_version_id"] == "subject-memory-version-3"
    assert payload["source_occurrence_id"] == "subject-memory-occurrence-3"
    assert payload["segment_count"] == 2
    assert scheduler.memory == memory
    manifest = repository.appended[0]
    assert [segment.content.encode() for segment in manifest.segments] == [
        first,
        second,
    ]
    assert manifest.source_occurrence_id == "subject-memory-occurrence-3"
    for segment, receipt in zip(manifest.segments, payload["ranges"], strict=True):
        source_ref = segment.source_refs[0]
        assert "subject-memory-version-3" in source_ref
        assert f"sha256={hashlib.sha256(memory).hexdigest()}" in source_ref
        assert f"range_sha256={segment.content_sha256}" in source_ref
        assert receipt["content_sha256"] == segment.content_sha256


@pytest.mark.parametrize(
    ("version_id", "content_sha256", "expected_error"),
    [
        ("stale-version", None, "MemoryBoundarySourceVersionConflict"),
        (
            "subject-memory-version-3",
            "f" * 64,
            "MemoryBoundarySourceContentHashConflict",
        ),
    ],
)
async def test_create_boundary_from_reviewed_ranges_rejects_stale_source(
    monkeypatch: pytest.MonkeyPatch,
    version_id: str,
    content_sha256: str | None,
    expected_error: str,
) -> None:
    repository = _Repository()
    memory = "# MEMORY\n精确正文\n".encode()
    scheduler = _Scheduler(memory)

    async def resolve(_tool):
        return _runtime(repository, scheduler)

    monkeypatch.setattr(boundary_tools, "_resolve_runtime", resolve)
    success, payload = await _tool(
        LifeCreateMemoryBoundaryFromSubjectRangeTool
    ).execute(
        boundary_id="stale-source",
        title="不会保存",
        scope="测试",
        current_meaning="测试",
        non_generalization="测试",
        segments=[
            {
                "segment_id": "exact",
                "title": "正文",
                "byte_start": len(b"# MEMORY\n"),
                "byte_end": len(memory),
            }
        ],
        expected_subject_revision=SUBJECT_REVISION,
        reviewed_memory_version_id=version_id,
        reviewed_content_sha256=(content_sha256 or hashlib.sha256(memory).hexdigest()),
    )

    assert success is False
    assert payload["detail"] == expected_error
    assert repository.appended == []


async def test_create_boundary_from_reviewed_ranges_rejects_utf8_split_and_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _Repository()
    memory = "甲乙".encode()
    scheduler = _Scheduler(memory)

    async def resolve(_tool):
        return _runtime(repository, scheduler)

    monkeypatch.setattr(boundary_tools, "_resolve_runtime", resolve)
    base = {
        "boundary_id": "invalid-range",
        "title": "不会保存",
        "scope": "测试",
        "current_meaning": "测试",
        "non_generalization": "测试",
        "expected_subject_revision": SUBJECT_REVISION,
        "reviewed_memory_version_id": "subject-memory-version-3",
        "reviewed_content_sha256": hashlib.sha256(memory).hexdigest(),
    }
    success, payload = await _tool(
        LifeCreateMemoryBoundaryFromSubjectRangeTool
    ).execute(
        **base,
        segments=[
            {
                "segment_id": "split",
                "title": "错误边界",
                "byte_start": 1,
                "byte_end": len(memory),
            }
        ],
    )
    assert success is False
    assert payload["detail"] == "MemoryBoundaryReviewedRangeNotUtf8Boundary"

    success, payload = await _tool(
        LifeCreateMemoryBoundaryFromSubjectRangeTool
    ).execute(
        **base,
        segments=[
            {
                "segment_id": "rewritten",
                "title": "禁止回传正文",
                "byte_start": 0,
                "byte_end": len(memory),
                "content": "模型改写过的正文",
            }
        ],
    )
    assert success is False
    assert payload["detail"] == "MemoryBoundaryReviewedRangeFieldsInvalid:content"
    assert repository.appended == []


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
        "nucleus_create_memory_boundary_from_subject_range",
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
    assert "tool-nucleus_create_memory_boundary_from_subject_range" not in manifest
    assert "tool-nucleus_propose_memory_continuity_revision" not in manifest


def test_boundary_tool_schemas_describe_cas_semantics() -> None:
    """expected_head_revision/expected_subject_revision 参数描述必须说明乐观锁语义。

    真实缺陷（2026-08-12）：模型传"读取时快照"被冲突后困惑"我看的就是 34"——
    工具描述没告诉它是 CAS（提交时可能已过期、冲突后重读重试）。
    """
    for tool_type in MEMORY_BOUNDARY_TOOLS:
        schema: dict[str, Any] = tool_type.to_schema()
        parameters = schema["function"]["parameters"]
        props: dict[str, Any] = parameters.get("properties", {})
        for param_name, param in props.items():
            if param_name in ("expected_head_revision", "expected_subject_revision"):
                description = str(param.get("description", ""))
                assert ("乐观锁" in description or "CAS" in description), (
                    f"{tool_type.tool_name}.{param_name} 描述缺少乐观锁语义说明: {description}"
                )
                assert "重新读取" in description and "revision" in description


async def test_resolve_runtime_permission_errors_are_actionable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """无聊天流/实例不活跃时，PermissionError 消息必须可操作（说明为什么+怎么办）。

    真实缺陷（2026-08-12）：心跳里调边界工具收到裸 `PermissionError` 空 detail，
    模型不知道被拒原因只能放弃。
    """
    from types import SimpleNamespace as _NS

    class _FakeRegistry:
        @staticmethod
        def get_for_stream(stream: str):
            assert stream == "stream-1"
            return None  # 无活跃实例

    class _FakeService:
        _memory_service = object()
        _learning_scheduler = object()
        consciousness_registry = _FakeRegistry()

    monkeypatch.setattr(
        "plugins.life_engine.service.registry.get_life_engine_service",
        lambda: _FakeService(),
    )

    tool = _NS(get_current_stream_id=lambda: "stream-1")
    with pytest.raises(PermissionError) as excinfo:
        await boundary_tools._resolve_runtime(tool)  # type: ignore[arg-type]
    message = str(excinfo.value)
    assert "聊天流" in message, f"错误消息应可操作（含聊天流指引）: {message}"


async def test_create_boundary_conflict_returns_current_revisions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ArtifactHeadConflict must return a structured, recoverable error with
    the latest subject/head revisions and no server-side replay (F9-A)."""

    repository = _Repository()

    async def append(manifest, *, expected_head_revision):
        raise ArtifactHeadConflict(
            "memory boundary head revision conflict: "
            f"boundary_id='one-morning', expected={expected_head_revision}, actual=2"
        )

    repository.append = append  # type: ignore[method-assign]
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
                "content": "正文。",
                "source_refs": ["experience:one"],
            }
        ],
        expected_head_revision=0,
        source_occurrence_id="event:one",
    )

    assert success is False
    assert payload["error"] == "ArtifactHeadConflict"
    assert payload["detail"]
    assert payload["current_subject_revision"] == SUBJECT_REVISION
    assert payload["current_head_revision"] == 0
    assert payload["recoverable"] is True
    assert "重新调用读取工具" in payload["hint"]


async def test_propose_conflict_returns_recoverable_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale subject revision must yield the current revision so the model
    can re-read and retry instead of giving up (F9-A)."""

    repository = _Repository()
    scheduler = _Scheduler(b"# MEMORY\n", ledger=_Ledger())

    async def resolve(_tool):
        return _runtime(repository, scheduler)

    monkeypatch.setattr(boundary_tools, "_resolve_runtime", resolve)
    success, payload = await _tool(
        LifeProposeMemoryContinuityRevisionTool
    ).execute(
        proposed_content="# MEMORY\n\n新的常驻线索。\n",
        reviewed_content_sha256="a" * 64,
        expected_subject_revision="stale-revision",
        reason="我想更新对这段记忆的理解。",
    )

    assert success is False
    assert payload["error"] == "LearningSubjectRevisionConflict"
    assert payload["detail"]
    assert payload["current_subject_revision"] == SUBJECT_REVISION
    assert payload["recoverable"] is True
    assert "重新调用读取工具" in payload["hint"]


async def test_create_boundary_non_conflict_failure_keeps_original_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-conflict failures keep the original error shape (no recoverable
    fields), so only revision conflicts advertise a retry path."""

    repository = _Repository()

    async def append(manifest, *, expected_head_revision):
        raise RuntimeError("boom")

    repository.append = append  # type: ignore[method-assign]
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
                "content": "正文。",
                "source_refs": ["experience:one"],
            }
        ],
        expected_head_revision=0,
        source_occurrence_id="event:one",
    )

    assert success is False
    assert payload == {"error": "RuntimeError", "detail": "boom"}
    assert "recoverable" not in payload
