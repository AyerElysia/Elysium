from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from plugins.life_engine.core.config import LifeEngineConfig
from plugins.life_engine.core.plugin import LifeEnginePlugin
from plugins.life_engine.memory.boundary_tools import MEMORY_BOUNDARY_TOOLS
from plugins.life_engine.memory.continuity_tools import CONTINUITY_REVIEW_TOOLS
from plugins.life_engine.memory.edges import EdgeType
from plugins.life_engine.memory.service import LifeMemoryService
from plugins.life_engine.memory.tools import MEMORY_TOOLS, LifeEngineMemoryStatsTool
from plugins.life_engine.service.integrations import MemoryIntegration
from plugins.life_engine.service.tool_manifests import get_tool_manifest
from plugins.life_engine.tools.schedule_tools import (
    ScheduleRecord,
    _build_callback,
    restore_life_schedules_when_ready,
)


async def test_daily_decay_compatibility_hook_never_mutates_memory() -> None:
    memory = SimpleNamespace(apply_decay=AsyncMock())
    service = SimpleNamespace(memory_service=memory, _memory_service=memory)

    await MemoryIntegration(service).maybe_run_daily_decay()

    memory.apply_decay.assert_not_awaited()


def test_canonical_relation_tool_is_reachable_from_chat_runtime() -> None:
    from plugins.life_engine.memory.tools import NucleusRelationsTool

    assert "life_engine_internal" in NucleusRelationsTool.chatter_allow
    assert "life_chatter" in NucleusRelationsTool.chatter_allow


async def test_direct_legacy_decay_entry_point_fails_closed() -> None:
    service = object.__new__(LifeMemoryService)

    with pytest.raises(RuntimeError, match="LegacyMemoryDecayRetired"):
        await service.apply_decay()


async def test_direct_legacy_pruning_entry_point_fails_closed() -> None:
    service = object.__new__(LifeMemoryService)

    with pytest.raises(RuntimeError, match="LegacyMemoryPruningRetired"):
        await service.prune_weak_edges()


async def test_runtime_legacy_graph_writers_fail_closed() -> None:
    service = object.__new__(LifeMemoryService)

    assert not hasattr(LifeMemoryService, "memory_storage")

    with pytest.raises(RuntimeError, match="LegacyGraphNodeMutationRetired"):
        await service.get_or_create_file_node("notes/legacy.md")
    with pytest.raises(RuntimeError, match="LegacyGraphActivationMutationRetired"):
        await service.increment_access("legacy-node")
    with pytest.raises(RuntimeError, match="LegacyGraphMutationRetired"):
        await service.create_or_update_edge(
            "legacy-a",
            "legacy-b",
            EdgeType.RELATES,
        )
    with pytest.raises(RuntimeError, match="LegacyGraphMutationRetired"):
        await service.delete_edge("notes/a.md", "notes/b.md")
    with pytest.raises(RuntimeError, match="LegacyGraphMutationRetired"):
        await service._reinforce_coactivated_wrapper(["legacy-a", "legacy-b"])


async def test_runtime_legacy_lineage_and_correction_writers_fail_closed() -> None:
    service = object.__new__(LifeMemoryService)

    with pytest.raises(RuntimeError, match="LegacyLineageMutationRetired"):
        await service.create_memory_lineage_edge(
            "notes/old.md",
            "notes/new.md",
            EdgeType.REFINES,
        )
    with pytest.raises(RuntimeError, match="LegacyCorrectionMutationRetired"):
        await service.record_memory_correction("topic", "message")


async def test_model_memory_stats_reads_unified_health_not_legacy_stats(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    memory = SimpleNamespace(
        health_snapshot=AsyncMock(
            return_value={
                "status": "healthy",
                "backend": "local",
                "behavior": {"status": "healthy"},
            }
        ),
        get_stats=AsyncMock(),
    )

    async def get_service(_self: object) -> object:
        return memory

    monkeypatch.setattr(LifeEngineMemoryStatsTool, "_get_service", get_service)
    success, payload = await LifeEngineMemoryStatsTool(
        plugin=SimpleNamespace()
    ).execute()

    assert success is True
    assert payload == {
        "action": "memory_stats",
        "projection_kind": "memory_health_snapshot",
        "authority": False,
        "read_only": True,
        "health": {
            "status": "healthy",
            "backend": "local",
            "behavior": {"status": "healthy"},
        },
    }
    memory.health_snapshot.assert_awaited_once_with()
    memory.get_stats.assert_not_awaited()


async def test_direct_legacy_dream_mutation_entry_point_fails_closed() -> None:
    service = object.__new__(LifeMemoryService)

    with pytest.raises(RuntimeError, match="LegacyDreamMutationRetired"):
        await service.dream_walk(persist_learning=True)


@pytest.mark.parametrize(
    "method_name",
    [
        "record_retrieval_episode",
        "record_retrieval_exposure",
        "record_retrieval_feedback",
    ],
)
async def test_second_retrieval_trace_writers_fail_closed(method_name: str) -> None:
    service = object.__new__(LifeMemoryService)

    with pytest.raises(RuntimeError, match="LegacyRetrievalTraceRetired"):
        await getattr(service, method_name)(object())


def test_model_visible_memory_tools_have_one_authoring_and_relation_path() -> None:
    assert [tool.tool_name for tool in MEMORY_BOUNDARY_TOOLS] == [
        "nucleus_read_memory_boundary"
    ]
    assert [tool.tool_name for tool in CONTINUITY_REVIEW_TOOLS] == [
        "nucleus_memory_continuity_review"
    ]
    assert [tool.tool_name for tool in MEMORY_TOOLS] == [
        "nucleus_search_memory",
        "nucleus_relations",
        "nucleus_memory_stats",
    ]


def test_formal_components_and_manifests_expose_only_canonical_memory_surfaces() -> None:
    components = LifeEnginePlugin(LifeEngineConfig()).get_components()
    component_classes = {str(getattr(item, "__name__", "")) for item in components}
    component_tools = {str(getattr(item, "tool_name", "")) for item in components}
    canonical_tools = {
        "nucleus_search_memory",
        "nucleus_relations",
        "nucleus_memory_stats",
        "nucleus_read_memory_boundary",
        "nucleus_memory_continuity_review",
    }
    retired_tools = {
        "nucleus_relate_file",
        "nucleus_view_relations",
        "nucleus_forget_relation",
    }

    assert "MemoryRouter" not in component_classes
    assert canonical_tools <= component_tools
    assert retired_tools.isdisjoint(component_tools)

    repository_root = Path(__file__).resolve().parents[3]
    manifest = json.loads(
        (repository_root / "plugins/life_engine/manifest.json").read_text(
            encoding="utf-8"
        )
    )
    declared_tools = {
        str(item.get("component_name") or "")
        for item in manifest["include"]
        if item.get("component_type") == "tool" and item.get("enabled") is True
    }
    assert canonical_tools <= declared_tools
    assert retired_tools.isdisjoint(declared_tools)

    chat_manifest = set(get_tool_manifest("chat"))
    assert {f"tool-{name}" for name in canonical_tools} <= chat_manifest
    assert {f"tool-{name}" for name in retired_tools}.isdisjoint(chat_manifest)


def test_legacy_memory_dashboard_asset_is_absent() -> None:
    repository_root = Path(__file__).resolve().parents[3]

    assert not (
        repository_root / "plugins/life_engine/static/memory_dashboard.html"
    ).exists()


def test_unregistered_legacy_memory_maintenance_design_is_absent() -> None:
    repository_root = Path(__file__).resolve().parents[3]

    assert not (repository_root / "plugins/life_engine/memory/repair.py").exists()
    assert not (repository_root / "plugins/life_engine/vis_plan.md").exists()


def test_package_root_no_longer_exports_second_retrieval_trace() -> None:
    from plugins.life_engine import memory

    for name in (
        "RetrievalEpisode",
        "RetrievalExposure",
        "RetrievalFeedback",
        "RetrievalPlasticity",
    ):
        assert name not in memory.__all__
        assert not hasattr(memory, name)


async def test_legacy_dream_schedule_is_visible_but_never_executes() -> None:
    service = SimpleNamespace(
        trigger_heartbeat_manually=AsyncMock(),
        enqueue_direct_message=AsyncMock(),
        trigger_dream_manually=AsyncMock(),
    )
    plugin = SimpleNamespace(service=service)
    record = ScheduleRecord(
        record_id="legacy-dream",
        title="historical dream",
        kind="dream",
        task_name="life_schedule::legacy",
        trigger_mode="interval",
        trigger_config={"interval_seconds": 60.0},
        recurring=True,
    )

    await _build_callback(plugin, record)()

    service.trigger_heartbeat_manually.assert_not_awaited()
    service.enqueue_direct_message.assert_not_awaited()
    service.trigger_dream_manually.assert_not_awaited()


async def test_legacy_dream_schedule_is_not_restored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from plugins.life_engine.tools import schedule_tools

    record = ScheduleRecord(
        record_id="legacy-dream",
        title="historical dream",
        kind="dream",
        task_name="life_schedule::legacy",
        trigger_mode="interval",
        trigger_config={"interval_seconds": 60.0},
        recurring=True,
    )
    store = SimpleNamespace(list_records=Mock(return_value=[record]))
    scheduler = SimpleNamespace(list_tasks=AsyncMock(return_value=[]))
    resolve_live = AsyncMock()
    schedule_record = AsyncMock()
    monkeypatch.setattr(schedule_tools, "_get_store", lambda _plugin: store)
    monkeypatch.setattr(
        schedule_tools,
        "get_unified_scheduler",
        lambda: scheduler,
    )
    monkeypatch.setattr(schedule_tools, "_resolve_live_task_info", resolve_live)
    monkeypatch.setattr(schedule_tools, "_schedule_record", schedule_record)
    plugin = SimpleNamespace(config=SimpleNamespace(settings=object()))

    assert await restore_life_schedules_when_ready(plugin) == {}
    resolve_live.assert_not_awaited()
    schedule_record.assert_not_awaited()


@pytest.mark.parametrize("kind", ["dream", "unknown"])
def test_new_schedule_kind_contract_excludes_legacy_mutators(kind: str) -> None:
    # Persisted historical records deliberately remain strings for audit, but
    # the public ScheduleKind contract only exposes heartbeat/message.
    from plugins.life_engine.tools import schedule_tools

    assert kind not in schedule_tools.ScheduleKind.__args__
