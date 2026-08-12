"""Tool-level contracts for exact, read-only long-memory boundaries."""

from __future__ import annotations

import importlib
import json
from types import SimpleNamespace
from typing import Any

import pytest

from plugins.life_engine.memory import boundary_tools
from plugins.life_engine.memory.boundary import (
    MEMORY_BOUNDARY_ARTIFACT_KIND,
    MemoryBoundaryManifest,
    MemoryBoundaryNotFound,
    MemoryBoundaryRepository,
    MemoryBoundarySegment,
    StoredMemoryBoundary,
    memory_boundary_uri,
)
from plugins.life_engine.memory.boundary_tools import (
    MEMORY_BOUNDARY_TOOLS,
    LifeReadMemoryBoundaryTool,
)
from plugins.life_engine.memory.living import new_artifact_version
from plugins.life_engine.service import registry as service_registry
from plugins.life_engine.service.tool_manifests import get_tool_manifest

SUBJECT_REVISION = "c" * 64
RETIRED_TOOL_TYPES = (
    "LifeCreateMemoryBoundaryTool",
    "LifeCreateMemoryBoundaryFromSubjectRangeTool",
    "LifeInspectMemoryContinuityTool",
    "LifeProposeMemoryContinuityRevisionTool",
)
RETIRED_TOOL_NAMES = (
    "nucleus_create_memory_boundary",
    "nucleus_create_memory_boundary_from_subject_range",
    "nucleus_inspect_memory_continuity",
    "nucleus_propose_memory_continuity_revision",
)


def _stored(
    *,
    boundary_id: str = "one-morning",
    revision: int = 1,
    segment_content: str = "没有被摘要掉的正文。",
    current_meaning: str = "我现在愿意这样记住它。",
) -> StoredMemoryBoundary:
    segment = MemoryBoundarySegment.create(
        segment_id="scene",
        title="完整场景",
        content=segment_content,
        source_refs=("experience:one",),
        source_occurrence_ids=("event:one",),
        scope="当时发生的交谈",
        visibility="private",
    )
    manifest = MemoryBoundaryManifest(
        boundary_id=boundary_id,
        manifest_revision=revision,
        operation_occurrence_id=f"boundary:record:{boundary_id}:{revision}",
        title="那一天",
        scope="当时发生的交谈",
        current_meaning=current_meaning,
        non_generalization="不推及所有未来。",
        actor_id="elysia",
        consciousness_instance_id="chat-main",
        stream_scope="chat:one",
        decision_occurrence_id=f"boundary:decision:{boundary_id}:{revision}",
        source_occurrence_id=f"event:{boundary_id}:{revision}",
        subject_revision=SUBJECT_REVISION,
        segments=(segment,),
    )
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
        head_revision=revision,
        exact_uri=memory_boundary_uri(
            manifest.boundary_id,
            artifact.artifact_id,
            manifest.root_sha256,
        ),
        current_head_revision=revision,
        is_current=True,
    )


class _Repository:
    def __init__(self, *records: StoredMemoryBoundary) -> None:
        self.records = {item.exact_uri: item for item in records}
        self.read_calls: list[str] = []
        self.history_calls: list[str] = []

    async def read_exact(self, uri: str) -> StoredMemoryBoundary:
        self.read_calls.append(uri)
        try:
            return self.records[uri]
        except KeyError as exc:
            raise MemoryBoundaryNotFound(
                f"MemoryBoundaryArtifactNotFound:{uri}"
            ) from exc

    async def history_descriptors(self, boundary_id: str):
        self.history_calls.append(boundary_id)
        return tuple(
            item.descriptor
            for item in sorted(
                (
                    record
                    for record in self.records.values()
                    if record.manifest.boundary_id == boundary_id
                ),
                key=lambda record: record.manifest.manifest_revision,
            )
        )


class _RecallMustRemainPending:
    """Fail if producing a tool result is mistaken for exact model delivery."""

    async def begin_memory_recall(self, **_kwargs: Any):
        raise AssertionError("recall must wait for an exact delivery receipt")

    async def append_memory_recall_events(self, _events):
        raise AssertionError("recall must wait for an exact delivery receipt")

    async def append_memory_corecall(self, _event):
        raise AssertionError("recall must wait for an exact delivery receipt")


def _runtime(
    repository: _Repository,
    recall: _RecallMustRemainPending | None = None,
) -> boundary_tools._BoundaryToolRuntime:
    return boundary_tools._BoundaryToolRuntime(
        service=SimpleNamespace(),
        memory_service=recall or _RecallMustRemainPending(),
        scheduler=SimpleNamespace(),
        repository=repository,  # type: ignore[arg-type]
        actor_consciousness_instance_id="chat-main",
        stream_scope="chat:one",
    )


def _install_runtime(
    monkeypatch: pytest.MonkeyPatch,
    repository: _Repository,
) -> None:
    runtime = _runtime(repository)

    async def resolve(_tool):
        return runtime

    monkeypatch.setattr(boundary_tools, "_resolve_runtime", resolve)


def _tool(identity: str) -> LifeReadMemoryBoundaryTool:
    tool = LifeReadMemoryBoundaryTool(plugin=SimpleNamespace())
    tool._bind_runtime_context(
        stream_id="chat:one",
        message=SimpleNamespace(
            message_id=f"message:{identity}",
            stream_id="chat:one",
            time="2026-08-10T00:00:00+00:00",
            extra={
                "life_turn_scope": {
                    "stream_id": "chat:one",
                    "turn_key": f"turn:{identity}",
                }
            },
        ),
        tool_call_id=f"tool-call:{identity}",
    )
    tool._runtime_task_name = "core"
    return tool


def test_retired_boundary_authoring_tools_are_not_public_or_registered() -> None:
    module = importlib.import_module("plugins.life_engine.memory.boundary_tools")

    assert not hasattr(module, "LEGACY_MEMORY_BOUNDARY_AUTHORING_TOOLS")
    for type_name in RETIRED_TOOL_TYPES:
        assert type_name not in module.__all__
    assert module.__all__ == ["MEMORY_BOUNDARY_TOOLS", "LifeReadMemoryBoundaryTool"]
    assert MEMORY_BOUNDARY_TOOLS == [LifeReadMemoryBoundaryTool]
    assert [item.tool_name for item in MEMORY_BOUNDARY_TOOLS] == [
        "nucleus_read_memory_boundary"
    ]

    for tool_type in MEMORY_BOUNDARY_TOOLS:
        schema: dict[str, Any] = tool_type.to_schema()
        assert schema["function"]["name"] == "tool-nucleus_read_memory_boundary"

    manifest = get_tool_manifest("chat")
    assert "tool-nucleus_read_memory_boundary" in manifest
    for tool_name in RETIRED_TOOL_NAMES:
        assert f"tool-{tool_name}" not in manifest


async def test_read_tool_preserves_all_exact_read_modes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stored = _stored()
    repository = _Repository(stored)
    _install_runtime(monkeypatch, repository)

    success, overview = await _tool("overview").execute(
        mode="overview",
        exact_uri=stored.exact_uri,
        max_bytes=8192,
    )
    assert success is True
    assert overview["action"] == "read_memory_boundary_overview"
    assert overview["exact_uri"] == stored.exact_uri
    assert overview["root_sha256"] == stored.manifest.root_sha256
    assert overview["recall_trace_state"] == "pending_exact_tool_result_delivery"
    assert overview["memory_recall_delivery_id"]
    assert overview["boundary_items"][0]["item_kind"] == "boundary_context"

    success, context = await _tool("context").execute(
        mode="context",
        exact_uri=stored.exact_uri,
        max_bytes=8192,
    )
    assert success is True
    assert context["action"] == "read_memory_boundary_context"
    assert json.loads(context["content"]) == {
        "current_meaning": stored.manifest.current_meaning,
        "non_generalization": stored.manifest.non_generalization,
        "scope": stored.manifest.scope,
        "title": stored.manifest.title,
    }

    success, provenance = await _tool("provenance").execute(
        mode="provenance",
        exact_uri=stored.exact_uri,
        max_bytes=8192,
    )
    assert success is True
    assert provenance["action"] == "read_memory_boundary_provenance"
    provenance_content = json.loads(provenance["content"])
    assert provenance_content["provenance_status"] == "external_unverified"
    assert provenance_content["segments"][0]["source_refs"] == ["experience:one"]
    assert provenance_content["segments"][0]["source_occurrence_ids"] == [
        "event:one"
    ]

    success, segment = await _tool("segment").execute(
        mode="segment",
        exact_uri=stored.exact_uri,
        segment_id="scene",
        max_bytes=8192,
    )
    assert success is True
    assert segment["action"] == "read_memory_boundary_segment"
    assert segment["content"] == stored.manifest.segments[0].content
    assert segment["content_sha256"] == stored.manifest.segments[0].content_sha256
    assert repository.read_calls == [stored.exact_uri] * 4


async def test_history_mode_is_bounded_content_free_and_revision_ordered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _stored(revision=1, segment_content="第一版完整正文")
    second = _stored(
        revision=2,
        segment_content="第二版绝不能出现在 history 投影中的完整正文",
        current_meaning="第二版当前理解",
    )
    repository = _Repository(second, first)
    _install_runtime(monkeypatch, repository)

    success, payload = await _tool("history").execute(
        mode="history",
        boundary_id="one-morning",
        max_bytes=8192,
    )

    assert success is True
    assert payload["action"] == "read_memory_boundary_history"
    assert [item["manifest_revision"] for item in payload["revisions"]] == [1, 2]
    assert all("exact_uri" in item for item in payload["revisions"])
    assert "第二版绝不能出现在 history 投影中的完整正文" not in str(payload)
    assert len(str(payload).encode("utf-8")) <= 8192
    assert repository.history_calls == ["one-morning"]
    assert repository.read_calls == []


async def test_segment_pagination_reconstructs_exact_utf8_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = "花瓣落下。🌸\n" * 700
    stored = _stored(segment_content=content)
    repository = _Repository(stored)
    _install_runtime(monkeypatch, repository)
    tool = _tool("segment-pages")
    continuation = ""
    chunks: list[str] = []
    delivery_ids: set[str] = set()

    while True:
        success, payload = await tool.execute(
            mode="segment",
            exact_uri=stored.exact_uri,
            segment_id="scene",
            continuation=continuation,
            max_bytes=2048,
        )
        assert success is True
        assert len(str(payload).encode("utf-8")) <= 2048
        chunks.append(payload["content"])
        delivery_ids.add(payload["memory_recall_delivery_id"])
        continuation = payload["continuation"]
        if not continuation:
            break

    assert "".join(chunks) == content
    assert len(delivery_ids) == len(chunks)
    assert len(chunks) > 1


async def test_read_tool_returns_stable_errors_without_floating(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stored = _stored()
    repository = _Repository(stored)
    _install_runtime(monkeypatch, repository)

    success, invalid = await _tool("invalid-mode").execute(mode="invented")
    assert success is False
    assert invalid == {
        "error": "mode must be overview, context, provenance, segment, or history"
    }
    assert repository.read_calls == []

    missing_uri = memory_boundary_uri(
        "missing",
        "artifact_" + "b" * 64,
        "a" * 64,
    )
    success, missing = await _tool("missing-boundary").execute(
        mode="overview",
        exact_uri=missing_uri,
    )
    assert success is False
    assert missing["error"] == "MemoryBoundaryNotFound"
    assert "MemoryBoundaryArtifactNotFound" in missing["detail"]

    success, missing_segment = await _tool("missing-segment").execute(
        mode="segment",
        exact_uri=stored.exact_uri,
        segment_id="not-there",
    )
    assert success is False
    assert missing_segment["error"] == "MemoryBoundarySegmentNotFound"
    assert "not-there" in missing_segment["detail"]


def test_read_identity_helpers_remain_deterministic_and_source_bound() -> None:
    tool = _tool("identity")
    material = {"b": 2, "a": ["甲", "乙"]}

    assert boundary_tools._canonical_hash(material) == boundary_tools._canonical_hash(
        {"a": ["甲", "乙"], "b": 2}
    )
    assert boundary_tools._stable_occurrence(
        tool, "delivery", material
    ) == boundary_tools._stable_occurrence(tool, "delivery", material)
    assert boundary_tools._stable_recall_chain(
        tool, "recall", material
    ) == boundary_tools._stable_recall_chain(tool, "recall", material)
    assert boundary_tools._stable_recall_time(tool) == "2026-08-10T00:00:00+00:00"

    unbound = LifeReadMemoryBoundaryTool(plugin=SimpleNamespace())
    with pytest.raises(RuntimeError, match="MemoryBoundaryToolCallIdentityRequired"):
        boundary_tools._stable_occurrence(unbound, "delivery", material)
    with pytest.raises(RuntimeError, match="MemoryBoundaryRecallTurnIdentityRequired"):
        boundary_tools._stable_recall_chain(unbound, "recall", material)


async def test_resolve_runtime_preserves_service_and_active_actor_guards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool = _tool("resolve-runtime")
    monkeypatch.setattr(service_registry, "get_life_engine_service", lambda: None)
    with pytest.raises(RuntimeError, match="LifeEngineServiceUnavailable"):
        await boundary_tools._resolve_runtime(tool)

    living = object()
    memory_service = SimpleNamespace(
        living_memory_store=living
    )
    inactive_service = SimpleNamespace(
        memory_service=memory_service,
        _learning_scheduler=object(),
        consciousness_registry=SimpleNamespace(
            get_for_stream=lambda _stream: SimpleNamespace(
                is_active=False,
                instance_id="chat-main",
            )
        ),
    )
    monkeypatch.setattr(
        service_registry,
        "get_life_engine_service",
        lambda: inactive_service,
    )
    with pytest.raises(PermissionError, match="MemoryBoundaryActorIsNotActive"):
        await boundary_tools._resolve_runtime(tool)

    active_service = SimpleNamespace(
        memory_service=memory_service,
        _learning_scheduler=object(),
        consciousness_registry=SimpleNamespace(
            get_for_stream=lambda _stream: SimpleNamespace(
                is_active=True,
                instance_id="chat-main",
            )
        ),
    )
    monkeypatch.setattr(
        service_registry,
        "get_life_engine_service",
        lambda: active_service,
    )
    runtime = await boundary_tools._resolve_runtime(tool)
    assert runtime.service is active_service
    assert runtime.memory_service is memory_service
    assert isinstance(runtime.repository, MemoryBoundaryRepository)
    assert runtime.actor_consciousness_instance_id == "chat-main"
    assert runtime.stream_scope == "chat:one"
