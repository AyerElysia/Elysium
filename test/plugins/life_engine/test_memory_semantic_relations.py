"""Tool contracts for canonical SemanticRelation history."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from plugins.life_engine.memory import tools as memory_tools
from plugins.life_engine.memory.living import SemanticRelation
from plugins.life_engine.memory.tools import (
    LEGACY_RELATION_MUTATION_RETIRED,
    MEMORY_TOOLS,
    NucleusRelationsTool,
)
from plugins.life_engine.service import registry as service_registry


class _MemoryService:
    def __init__(self) -> None:
        self.semantic_relations: list[SemanticRelation] = []
        self.recorded: list[SemanticRelation] = []
        self.read_order: list[str] = []
        self.legacy_result: dict[str, Any] = {
            "center": {
                "file_path": "notes/a.md",
                "title": "a",
                "activation_strength": 0.8,
                "access_count": 2,
            },
            "outgoing": [
                {
                    "file_path": "notes/legacy.md",
                    "title": "legacy",
                    "relation_type": "relates",
                    "strength": 0.7,
                    "reason": "legacy-only",
                    "depth": 1,
                    "edge_id": "legacy-edge",
                }
            ],
            "incoming": [],
        }

    async def list_memory_semantic_relations(
        self,
        entity_ref: str,
    ) -> list[SemanticRelation]:
        self.read_order.append("semantic")
        return [
            relation
            for relation in self.semantic_relations
            if entity_ref in {relation.source_ref, relation.target_ref}
        ]

    async def record_memory_semantic_relation(
        self,
        relation: SemanticRelation,
    ) -> SemanticRelation:
        self.recorded.append(relation)
        self.semantic_relations.append(relation)
        return relation

    async def get_file_relations(
        self,
        *,
        file_path: str,
        depth: int,
        min_strength: float,
    ) -> dict[str, Any]:
        assert file_path == "notes/a.md"
        assert depth == 2
        assert min_strength == pytest.approx(0.4)
        self.read_order.append("legacy")
        return self.legacy_result


class _ConsciousnessRegistry:
    def __init__(self, *, active: bool = True) -> None:
        self.active = active
        self.streams: list[str] = []

    def get_for_stream(self, stream_id: str) -> Any:
        self.streams.append(stream_id)
        return SimpleNamespace(
            instance_id="consciousness:elysia",
            is_active=self.active,
        )


def _install_runtime(
    monkeypatch: pytest.MonkeyPatch,
    memory_service: _MemoryService,
    *,
    active: bool = True,
) -> _ConsciousnessRegistry:
    registry = _ConsciousnessRegistry(active=active)
    life_service = SimpleNamespace(
        memory_service=memory_service,
        consciousness_registry=registry,
    )
    monkeypatch.setattr(
        service_registry,
        "get_life_engine_service",
        lambda: life_service,
    )
    return registry


def _tool(
    tool_type: type,
    *,
    tool_call_id: str = "tool-call:relation-one",
) -> Any:
    tool = tool_type(plugin=SimpleNamespace())
    tool._bind_runtime_context(
        stream_id="stream:chat-one",
        message=SimpleNamespace(
            message_id="message:one",
            stream_id="stream:chat-one",
            time="2026-08-12T01:02:03+00:00",
            extra={
                "life_turn_scope": {
                    "stream_id": "stream:chat-one",
                    "turn_key": "turn:one",
                }
            },
        ),
        tool_call_id=tool_call_id,
    )
    tool._runtime_task_name = "core"
    return tool


async def test_unified_add_appends_only_semantic_history_with_runtime_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    memory_service = _MemoryService()
    registry = _install_runtime(monkeypatch, memory_service)
    tool = _tool(NucleusRelationsTool)

    success, payload = await tool.execute(
        action="add",
        source_path="notes/a.md",
        target_path="notes/b.md",
        relation_type="后来让我重新理解",
        reason="这是我此刻明确写下的联系。",
    )

    assert success is True
    assert registry.streams == ["stream:chat-one"]
    assert len(memory_service.recorded) == 1
    relation = memory_service.recorded[0]
    assert relation.source_ref == "document:notes/a.md"
    assert relation.target_ref == "document:notes/b.md"
    assert relation.predicate == "后来让我重新理解"
    assert relation.reason == "这是我此刻明确写下的联系。"
    assert relation.actor == "consciousness:elysia"
    assert relation.consciousness_instance_id == "consciousness:elysia"
    assert relation.stream_scope == "stream:chat-one"
    assert relation.recorded_at == "2026-08-12T01:02:03+00:00"
    assert relation.metadata == {
        "source_occurrence_id": "turn:one",
        "source_occurrence_kind": "life_turn",
        "tool_call_id": "tool-call:relation-one",
    }
    assert payload["relation_id"] == relation.relation_id
    assert payload["actor"] == "consciousness:elysia"
    assert payload["source_occurrence_id"] == "turn:one"
    assert payload["authority"] == "memory_semantic_relations"
    assert payload["legacy_edge_written"] is False
    assert "legacy_edge_id" not in payload

    replay_success, replay_payload = await tool.execute(
        action="add",
        source_path="notes/a.md",
        target_path="notes/b.md",
        relation_type="后来让我重新理解",
        reason="这是我此刻明确写下的联系。",
    )

    assert replay_success is True
    assert replay_payload["relation_id"] == relation.relation_id
    assert len(memory_service.recorded) == 1


async def test_same_tool_occurrence_with_changed_relation_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    memory_service = _MemoryService()
    _install_runtime(monkeypatch, memory_service)
    tool = _tool(NucleusRelationsTool)
    success, _ = await tool.execute(
        action="add",
        source_path="notes/a.md",
        target_path="notes/b.md",
        relation_type="有联系",
        reason="第一次明确表达。",
    )
    assert success is True

    conflict_success, conflict = await tool.execute(
        action="add",
        source_path="notes/a.md",
        target_path="notes/b.md",
        relation_type="已经不是同一种联系",
        reason="同一 occurrence 不得悄悄换内容。",
    )

    assert conflict_success is False
    assert conflict["error"] == "SemanticRelationOccurrenceConflict"
    assert len(memory_service.recorded) == 1


@pytest.mark.parametrize(
    ("active", "tool_call_id", "expected_error"),
    [
        (False, "tool-call:inactive", "SemanticRelationActorIsNotActive"),
        (True, "", "SemanticRelationToolCallIdentityRequired"),
    ],
)
async def test_relation_append_requires_active_actor_and_stable_tool_occurrence(
    monkeypatch: pytest.MonkeyPatch,
    active: bool,
    tool_call_id: str,
    expected_error: str,
) -> None:
    memory_service = _MemoryService()
    _install_runtime(monkeypatch, memory_service, active=active)

    success, payload = await _tool(
        NucleusRelationsTool,
        tool_call_id=tool_call_id,
    ).execute(
        action="add",
        source_path="notes/a.md",
        target_path="notes/b.md",
        relation_type="有联系",
        reason="只有真实 active actor 才能留下这条历史。",
    )

    assert success is False
    assert payload["error"] == expected_error
    assert memory_service.recorded == []


async def test_view_reads_semantic_history_before_read_only_legacy_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    memory_service = _MemoryService()
    memory_service.semantic_relations.append(
        SemanticRelation(
            relation_id="relation:semantic",
            source_ref="document:notes/a.md",
            target_ref="document:notes/b.md",
            predicate="我现在把它们放在一起理解",
            reason="由当前主体明确留下。",
            actor="consciousness:elysia",
            recorded_at="2026-08-12T01:02:03+00:00",
            consciousness_instance_id="consciousness:elysia",
            stream_scope="stream:chat-one",
            metadata={"source_occurrence_id": "turn:one"},
        )
    )

    async def get_service(_self: Any) -> _MemoryService:
        return memory_service

    monkeypatch.setattr(NucleusRelationsTool, "_get_service", get_service)
    success, payload = await _tool(NucleusRelationsTool).execute(
        action="view",
        file_path="notes/a.md",
        depth=2,
        min_strength=0.4,
    )

    assert success is True
    assert memory_service.read_order == ["semantic", "legacy"]
    assert payload["authority"] == "memory_semantic_relations"
    assert payload["semantic_relation_count"] == 1
    semantic = payload["semantic_relations"][0]
    assert semantic["relation_id"] == "relation:semantic"
    assert semantic["direction"] == "outgoing"
    assert semantic["counterpart_ref"] == "document:notes/b.md"

    legacy = payload["legacy_compatibility_projection"]
    assert legacy["projection_kind"] == "legacy_memory_edges_compatibility"
    assert legacy["authoritative"] is False
    assert legacy["read_only"] is True
    assert legacy["automatic_promotion_to_semantic_history"] is False
    assert legacy["strength_is_truth"] is False
    assert legacy["outgoing"][0]["edge_id"] == "legacy-edge"
    assert memory_service.recorded == []


async def test_legacy_only_relation_is_not_promoted_to_semantic_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    memory_service = _MemoryService()

    async def get_service(_self: Any) -> _MemoryService:
        return memory_service

    monkeypatch.setattr(NucleusRelationsTool, "_get_service", get_service)
    success, payload = await _tool(NucleusRelationsTool).execute(
        action="view",
        file_path="notes/a.md",
        depth=2,
        min_strength=0.4,
    )

    assert success is True
    assert payload["semantic_relation_count"] == 0
    assert payload["semantic_relations"] == []
    assert payload["legacy_compatibility_projection"]["available"] is True
    assert memory_service.recorded == []


async def test_destructive_relation_actions_are_retired_without_side_effects() -> None:
    unified = _tool(NucleusRelationsTool)
    success, payload = await unified.execute(
        action="forget",  # type: ignore[arg-type]
        source_path="notes/a.md",
        target_path="notes/b.md",
    )
    assert success is False
    assert payload["error"] == LEGACY_RELATION_MUTATION_RETIRED
    assert payload["mutated"] is False


def test_relation_schema_exposes_only_append_and_view() -> None:
    schema = NucleusRelationsTool.to_schema()
    properties = schema["function"]["parameters"]["properties"]
    action = properties["action"]
    assert action["enum"] == ["add", "view"]
    assert "forget" not in action["enum"]
    assert {
        "source_path",
        "target_path",
        "relation_type",
        "reason",
        "file_path",
        "depth",
        "min_strength",
    } <= properties.keys()
    assert MEMORY_TOOLS.count(NucleusRelationsTool) == 1
    assert NucleusRelationsTool.chatter_allow == [
        "life_engine_internal",
        "life_chatter",
    ]


def test_ghost_relation_tools_are_physically_absent() -> None:
    legacy_class_suffixes = (
        "RelateFileTool",
        "ViewRelationsTool",
        "ForgetRelationTool",
    )
    legacy_tool_suffixes = ("relate_file", "view_relations", "forget_relation")

    for suffix in legacy_class_suffixes:
        assert not hasattr(memory_tools, f"LifeEngine{suffix}")
    source = memory_tools.__loader__.get_source(memory_tools.__name__)
    assert source is not None
    for suffix in legacy_tool_suffixes:
        assert f"nucleus_{suffix}" not in source
