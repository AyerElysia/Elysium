"""life_engine 记忆演化链路回归测试。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from plugins.life_engine.core.config import LifeEngineConfig
from plugins.life_engine.memory import EdgeType, LifeMemoryService
from plugins.life_engine.memory.nodes import MemoryNode
from plugins.life_engine.tools.file_tools import FetchLifeMemoryTool


@dataclass
class _DummyPlugin:
    config: LifeEngineConfig


class _FakeCollection:
    def query(self, **_: Any) -> dict[str, list[list[Any]]]:
        return {"ids": [[]], "distances": [[]]}

    def get(self, **_: Any) -> dict[str, list[Any]]:
        return {"ids": [], "embeddings": [], "documents": [], "metadatas": []}

    def upsert(self, **_: Any) -> None:
        return None

    def delete(self, **_: Any) -> None:
        return None


def _make_plugin(tmp_path: Path) -> _DummyPlugin:
    config = LifeEngineConfig()
    config.settings.workspace_path = str(tmp_path)
    return _DummyPlugin(config=config)


async def _seed_legacy_migration_node(
    service: LifeMemoryService,
    file_path: str,
    *,
    title: str = "",
    content: str = "",
) -> MemoryNode:
    """Seed one historical node through the local migration-only adapter."""
    return await service._require_memory_storage().legacy_graph.get_or_create_file_node(
        file_path,
        title,
        content,
    )


async def _seed_legacy_migration_edge(
    service: LifeMemoryService,
    source: MemoryNode,
    target: MemoryNode,
    edge_type: EdgeType,
    *,
    reason: str,
    strength: float = 0.7,
) -> None:
    """Seed immutable legacy lineage evidence for migration/read tests."""
    await service._require_memory_storage().legacy_graph.create_or_update_edge(
        source.node_id,
        target.node_id,
        edge_type.value,
        reason=reason,
        strength=strength,
        bidirectional=False,
    )


async def _seed_legacy_migration_correction(
    service: LifeMemoryService,
    *,
    topic: str,
    message: str,
    related_node: MemoryNode,
    query: str,
) -> None:
    """Seed one old correction row without exposing it as a runtime capability."""
    await service._require_memory_storage().legacy_graph.insert_correction(
        topic=topic,
        message=message,
        source="migration_fixture",
        related_node_id=related_node.node_id,
        query=query,
        stream_id=None,
    )


def _legacy_row_counts(service: LifeMemoryService) -> tuple[int, int, int]:
    return tuple(
        service._db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("memory_nodes", "memory_edges", "memory_corrections")
    )


def test_historical_dream_system_lineage_remains_readable_but_not_authorable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """旧 lineage/correction 可读，但正式运行时不能再写第二套语义。"""

    async def _run() -> None:
        plugin = _make_plugin(tmp_path)
        service = LifeMemoryService(plugin)

        async def _fake_get_collection() -> Any:
            return _FakeCollection()

        async def _fake_embed_text(_: str) -> list[float]:
            return [0.0]

        monkeypatch.setattr(service, "_get_chroma_collection", _fake_get_collection)
        monkeypatch.setattr("plugins.life_engine.memory.search.embed_text", _fake_embed_text)
        await service.initialize()

        old_path = "notes/tech/dream_system_research.md"
        current_path = "notes/tech/dream_system.md"
        current_file = tmp_path / current_path
        current_file.parent.mkdir(parents=True)
        current_file.write_text(
            "# 做梦系统\n\n做梦系统已经做好了，会整理记忆，也会生成洞察。\n",
            encoding="utf-8",
        )

        old_file = tmp_path / old_path
        old_file.write_text("旧笔记内容", encoding="utf-8")
        old_node = await _seed_legacy_migration_node(
            service,
            old_path,
            title="dream_system_research",
            content="做梦系统还在研究阶段，旧笔记只记录了早期方案。",
        )
        current_node = await _seed_legacy_migration_node(
            service,
            current_path,
            title="dream_system",
            content=current_file.read_text(encoding="utf-8"),
        )
        await _seed_legacy_migration_edge(
            service,
            old_node,
            current_node,
            EdgeType.RENAMES,
            reason="显式整理到当前笔记",
        )

        resolution = await service.resolve_canonical_path(old_path)
        assert resolution["resolved"] is True
        assert resolution["resolved_path"] == current_path
        assert resolution["lineage"][0]["relation"] == "renames"

        correction_message = (
            "做梦系统早就做好了；旧研究笔记只能作为早期轨迹，不代表当前状态。"
        )
        await _seed_legacy_migration_correction(
            service,
            topic="做梦系统",
            message=correction_message,
            related_node=current_node,
            query="做梦系统",
        )

        before_rejected_writes = _legacy_row_counts(service)
        with pytest.raises(RuntimeError, match="LegacyLineageMutationRetired"):
            await service.create_memory_lineage_edge(
                old_path,
                current_path,
                EdgeType.RENAMES,
                reason="runtime authoring is retired",
            )
        with pytest.raises(RuntimeError, match="LegacyCorrectionMutationRetired"):
            await service.record_memory_correction(
                topic="做梦系统",
                message="runtime correction authoring is retired",
                related_paths=[current_path],
                query="做梦系统",
            )
        assert _legacy_row_counts(service) == before_rejected_writes

        bundles = await service.search_memory_bundles("做梦系统", top_k=3)
        assert bundles
        bundle = bundles[0]
        assert bundle.primary_path == current_path
        assert "已经做好了" in bundle.current_understanding
        assert correction_message in {
            correction.message for correction in bundle.corrections
        }

        evidence_paths = {item.file_path for item in bundle.evidence}
        assert old_path in evidence_paths
        assert current_path in evidence_paths
        assert any(item.file_path == old_path for item in bundle.history_trace)

        monkeypatch.setattr(
            "plugins.life_engine.tools.file_tools._get_life_engine_service",
            lambda _plugin: type("_Service", (), {"_memory_service": service})(),
        )
        tool = FetchLifeMemoryTool(plugin=plugin)
        ok, payload = await tool.execute([old_path], max_length_per_file=1000)
        assert ok is True
        assert payload["successful"] == 1
        file_payload = payload["files"][0]
        assert file_payload["path"] == current_path
        assert file_payload["requested_path"] == old_path
        assert "旧笔记内容" not in file_payload["content"]
        assert "已经做好了" in file_payload["content"]
        assert file_payload["path_resolution"]["resolved"] is True

    asyncio.run(_run())


async def test_explicit_lineage_prefers_deep_existing_target_over_source_and_dead_branch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin = _make_plugin(tmp_path)
    service = LifeMemoryService(plugin)

    async def _fake_get_collection() -> Any:
        return _FakeCollection()

    monkeypatch.setattr(service, "_get_chroma_collection", _fake_get_collection)
    await service.initialize()

    source_path = "notes/a.md"
    middle_path = "notes/b.md"
    current_path = "notes/c.md"
    notes = tmp_path / "notes"
    notes.mkdir()
    for path in (source_path, middle_path, current_path):
        (tmp_path / path).write_text(path, encoding="utf-8")

    source = await _seed_legacy_migration_node(service, source_path, title="A")
    middle = await _seed_legacy_migration_node(service, middle_path, title="B")
    current = await _seed_legacy_migration_node(service, current_path, title="C")
    dead = await _seed_legacy_migration_node(service, "notes/dead.md", title="dead")
    await _seed_legacy_migration_edge(
        service,
        source,
        dead,
        EdgeType.RENAMES,
        reason="dead preferred branch",
        strength=0.99,
    )
    await _seed_legacy_migration_edge(
        service,
        source,
        middle,
        EdgeType.RENAMES,
        reason="explicit first step",
        strength=0.5,
    )
    await _seed_legacy_migration_edge(
        service,
        middle,
        current,
        EdgeType.REFINES,
        reason="explicit current step",
        strength=0.5,
    )

    resolution = await service.resolve_canonical_path(source_path, max_depth=3)

    assert resolution["resolved"] is True
    assert resolution["resolved_path"] == current_path
    assert [step["to"] for step in resolution["lineage"]] == [middle_path, current_path]


async def test_fetch_does_not_persist_guessed_lineage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin = _make_plugin(tmp_path)
    service = LifeMemoryService(plugin)

    async def _fake_get_collection() -> Any:
        return _FakeCollection()

    monkeypatch.setattr(service, "_get_chroma_collection", _fake_get_collection)
    await service.initialize()

    guessed_path = "notes/topic.md"
    guessed_file = tmp_path / guessed_path
    guessed_file.parent.mkdir(parents=True)
    guessed_file.write_text("current", encoding="utf-8")
    before = (
        service._db.execute("SELECT COUNT(*) FROM memory_nodes").fetchone()[0],
        service._db.execute("SELECT COUNT(*) FROM memory_edges").fetchone()[0],
    )
    calls: list[dict[str, Any]] = []
    resolve_canonical_path = service.resolve_canonical_path

    async def _capture_resolution(file_path: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return await resolve_canonical_path(file_path, *args, **kwargs)

    monkeypatch.setattr(service, "resolve_canonical_path", _capture_resolution)
    monkeypatch.setattr(
        "plugins.life_engine.tools.file_tools._get_life_engine_service",
        lambda _plugin: type("_Service", (), {"_memory_service": service})(),
    )

    ok, payload = await FetchLifeMemoryTool(plugin=plugin).execute(
        ["notes/topic_research.md"],
    )

    assert ok is True
    assert payload["successful"] == 0
    assert payload["failed"] == 1
    assert calls == [{"persist_lineage": False, "allow_heuristic": False}]
    assert (
        service._db.execute("SELECT COUNT(*) FROM memory_nodes").fetchone()[0],
        service._db.execute("SELECT COUNT(*) FROM memory_edges").fetchone()[0],
    ) == before


async def test_runtime_lineage_authoring_fails_closed_and_keeps_historical_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin = _make_plugin(tmp_path)
    service = LifeMemoryService(plugin)

    async def _fake_get_collection() -> Any:
        return _FakeCollection()

    monkeypatch.setattr(service, "_get_chroma_collection", _fake_get_collection)
    await service.initialize()

    notes = tmp_path / "notes"
    notes.mkdir()
    current_path = "notes/current.md"
    (tmp_path / current_path).write_text("current", encoding="utf-8")
    historical_path = "notes/old.md"
    historical = await _seed_legacy_migration_node(
        service,
        historical_path,
        title="old",
        content="historical evidence",
    )
    current = await _seed_legacy_migration_node(
        service,
        current_path,
        title="current",
        content="current",
    )
    await _seed_legacy_migration_edge(
        service,
        historical,
        current,
        EdgeType.RENAMES,
        reason="保留旧文件作为历史证据",
    )

    resolution = await service.resolve_canonical_path(historical_path)
    assert resolution["resolved"] is True
    assert resolution["resolved_path"] == current_path

    before = _legacy_row_counts(service)
    with pytest.raises(RuntimeError, match="LegacyLineageMutationRetired"):
        await service.create_memory_lineage_edge(
            "notes/typo.md",
            current_path,
            EdgeType.RENAMES,
            reason="此路径不应创建空白节点",
        )

    assert _legacy_row_counts(service) == before
    assert await service.get_node_by_file_path(historical_path) is not None
