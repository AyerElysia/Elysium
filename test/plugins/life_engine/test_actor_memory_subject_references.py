"""Actor-facing exact source references, fake memory only, no models or stores."""

from __future__ import annotations

import socket
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from plugins.life_engine.memory import tools as memory_tools
from plugins.life_engine.memory.experience import EvidenceAwareMemoryResult
from plugins.life_engine.memory.lineage import MemoryBundle, MemoryEvidence, MemoryTrace
from plugins.life_engine.memory.search import SearchResult
from plugins.life_engine.service import core


@pytest.fixture(autouse=True)
def _deny_network(monkeypatch):
    for name in ("connect", "connect_ex", "bind"):
        original = getattr(socket.socket, name)

        def guarded(self, address, *args, _original=original, **kwargs):
            if self.family in (socket.AF_INET, socket.AF_INET6):
                pytest.fail("actor display tests must not use network/ports")
            return _original(self, address, *args, **kwargs)

        monkeypatch.setattr(socket.socket, name, guarded)


def _hit(
    path="notes/current.md",
    *,
    document_id="doc-current",
    version_id="version-current",
    source="direct",
):
    return SearchResult(
        file_path=path,
        title="Synthetic title",
        snippet="Synthetic captured snippet",
        relevance=0.8,
        source=source,
        node_id="subject-file:" + document_id if document_id else "legacy-node",
        document_id=document_id,
        version_id=version_id,
        document_revision=3 if document_id else 0,
        binding_revision=2 if document_id else 0,
    )


def _service(tmp_path, monkeypatch, results, bundles, *, selected=True, error=None):
    service = object.__new__(core.LifeEngineService)
    service._selectable_storage_enabled = selected
    service._memory_service = SimpleNamespace(
        _subject_document_store_required=selected,
        search_memory=AsyncMock(return_value=results),
        build_memory_bundles=AsyncMock(return_value=bundles, side_effect=error),
    )
    service._workspace_dir = Mock(return_value=tmp_path)
    metadata = Mock(
        side_effect=AssertionError("managed output must not read stale disk metadata")
    )
    monkeypatch.setattr(core, "get_file_metadata", metadata)
    log = SimpleNamespace(info=Mock(), warning=Mock())
    monkeypatch.setattr(core, "logger", log)
    return service, metadata, log


def _assert_exact_read_hint(output: str) -> None:
    assert "version_ids={路径: version_id}" in output
    assert "nucleus_read_file（read_file）传 path、version_id" in output
    assert "正文不传 document_id" in output
    assert "仅传完整 file_ref" in output
    assert "不与 path、document_id、version_id 混用" in output
    assert "续读保留原选择器并传回 continuation" in output
    assert "document_id、version_id 精确续读" not in output


def _search_tool(monkeypatch, *, canonical=True):
    """Keep the tool call detached from runtime actors, stores and recall writes."""
    direct = _hit()
    results = [direct]
    evidence = EvidenceAwareMemoryResult(
        record_id=direct.node_id,
        kind="document_evidence",
        content=direct.snippet,
        rank_score=direct.relevance,
        confidence=None,
        source="document_direct",
        metadata={
            "document_id": direct.document_id,
            "version_id": direct.version_id,
            "file_path": direct.file_path,
            "file_ref": f"subject-file:{direct.document_id}@{direct.version_id}",
        },
    )
    memory = SimpleNamespace(
        search_memory=AsyncMock(return_value=results),
        build_memory_bundles=AsyncMock(return_value=[]),
        search_evidence_aware=AsyncMock(return_value=[evidence]),
    )
    if canonical:
        memory.expand_living_document_associations = AsyncMock(return_value=results)
    tool = memory_tools.LifeEngineSearchMemoryTool(plugin=SimpleNamespace())
    monkeypatch.setattr(tool, "_get_service", AsyncMock(return_value=memory))
    monkeypatch.setattr(
        memory_tools, "_resolve_search_recall_identity", Mock(return_value=None)
    )
    return tool, memory, results


@pytest.mark.parametrize("canonical", [True, False])
@pytest.mark.parametrize("options", [{}, {"enable_association": False}])
async def test_search_tool_association_off_skips_all_extra_branches(
    monkeypatch, canonical, options
):
    tool, memory, results = _search_tool(monkeypatch, canonical=canonical)
    if canonical:
        memory.expand_living_document_associations.side_effect = AssertionError(
            "baseline must not expand living associations"
        )
    memory.build_memory_bundles.side_effect = AssertionError(
        "baseline must not read bundle lineage or corrections"
    )

    ok, payload = await tool.execute(
        " synthetic ", top_k=3, **options
    )

    assert ok, payload
    memory.search_memory.assert_awaited_once_with(
        "synthetic", top_k=3, enable_association=False,
        file_types=None, time_range_days=0, return_bundles=False,
    )
    if canonical:
        memory.expand_living_document_associations.assert_not_awaited()
    memory.build_memory_bundles.assert_not_awaited()
    memory.search_evidence_aware.assert_awaited_once()
    evidence_call = memory.search_evidence_aware.await_args
    assert evidence_call.args == ("synthetic",)
    assert evidence_call.kwargs["enable_association"] is False
    assert evidence_call.kwargs["document_results"] is results
    assert "association_context_key" in evidence_call.kwargs
    assert type(evidence_call.kwargs["association_random_seed"]) is int
    assert payload["memory_bundles"] == []
    assert payload["evidence_results"][0]["source"] == "document_direct"
    assert "subject-file:doc-current@version-current" in str(payload)
    assert "Synthetic captured snippet" in str(payload)
    assert payload["recall_episode"]["persisted"] is False
    assert payload["recall_episode"]["trace_state"] == "unavailable"
    assert payload["recall_delivery_binding"] is None


async def test_search_tool_explicit_on_preserves_bundle_and_evidence_paths(
    monkeypatch
):
    tool, memory, results = _search_tool(monkeypatch)
    direct = results[0]
    memory.build_memory_bundles.return_value = [
        MemoryBundle(
            query="synthetic",
            current_understanding="Synthetic bundle context",
            primary_path=direct.file_path,
            primary_node_id=direct.node_id,
            primary_document_id=direct.document_id,
            primary_version_id=direct.version_id,
        )
    ]

    ok, payload = await tool.execute("synthetic", top_k=3, enable_association=True)

    assert ok, payload
    assert memory.search_memory.await_args.kwargs["enable_association"] is False
    memory.expand_living_document_associations.assert_awaited_once()
    memory.build_memory_bundles.assert_awaited_once_with(
        query="synthetic", results=results, top_k=3
    )
    memory.search_evidence_aware.assert_awaited_once()
    evidence_call = memory.search_evidence_aware.await_args
    assert evidence_call.kwargs["enable_association"] is True
    assert evidence_call.kwargs["document_results"] is results
    assert payload["memory_bundles"]
    assert "Synthetic bundle context" in str(payload)


async def test_bundle_primary_evidence_and_history_preserve_exact_identity_without_disk_metadata(
    tmp_path, monkeypatch
):
    current = _hit()
    bundle = MemoryBundle(
        query="synthetic",
        current_understanding="Synthetic understanding stays unchanged.",
        primary_path=current.file_path,
        primary_node_id=current.node_id,
        primary_document_id=current.document_id,
        primary_version_id=current.version_id,
        evidence=[
            MemoryEvidence(
                file_path=current.file_path,
                title="Current",
                snippet="Current captured text",
                node_id=current.node_id,
                document_id=current.document_id,
                version_id=current.version_id,
            ),
            MemoryEvidence(
                file_path="notes/reused.md",
                title="Old managed evidence",
                snippet="Old captured text",
                exists=False,
                node_id="subject-file:doc-old",
                document_id="doc-old",
                version_id="version-old",
            ),
        ],
        history_trace=[
            MemoryTrace(
                relation="revises",
                file_path="notes/reused.md",
                title="Old managed trace",
                direction="earlier",
                exists=False,
                node_id="subject-file:doc-old",
                document_id="doc-old",
                version_id="version-old",
            ),
            MemoryTrace(
                relation="relates",
                file_path="notes/reused.md",
                title="Retired legacy trace",
                direction="earlier",
                exists=False,
                node_id="legacy-old-neighbour",
            ),
        ],
    )
    service, metadata, _ = _service(tmp_path, monkeypatch, [current], [bundle])
    output = await service.search_actor_memory("synthetic", enable_association=True)
    assert "权威版本" in output
    assert "Synthetic understanding stays unchanged." in output
    assert output.count("file_ref=subject-file:doc-current@version-current") >= 2
    assert output.count("file_ref=subject-file:doc-old@version-old") == 2
    assert "document_id=doc-old" in output and "version_id=version-old" in output
    assert "node_id=legacy-old-neighbour" in output
    assert "非当前绑定、不能按当前路径回取" in output
    assert "当前路径不存在" not in output
    _assert_exact_read_hint(output)
    assert "旧记忆作为历史证据保留，当前理解优先参考后续整理和显式修正" in output
    metadata.assert_not_called()
    service._workspace_dir.assert_not_called()


async def test_direct_and_associated_display_keep_refs_when_no_bundle_was_returned(
    tmp_path, monkeypatch
):
    results = [
        _hit(),
        _hit(
            "notes/related.md",
            document_id="doc-associated",
            version_id="version-associated",
            source="associated",
        ),
    ]
    service, metadata, _ = _service(tmp_path, monkeypatch, results, [])
    output = await service.search_actor_memory("synthetic", top_k=3)
    assert "【直接命中的记忆】" in output and "【联想扩散结果】" in output
    for result in results:
        assert (
            f"file_ref=subject-file:{result.document_id}@{result.version_id}" in output
        )
        assert f"node_id={result.node_id}" in output
    assert output.count("权威版本") == 2
    _assert_exact_read_hint(output)
    metadata.assert_not_called()
    service._workspace_dir.assert_not_called()


async def test_explicit_association_keeps_existing_expansion(
    tmp_path, monkeypatch
):
    direct = _hit()
    associated = _hit(
        "notes/related.md",
        document_id="doc-associated",
        version_id="version-associated",
        source="associated",
    )
    results = [direct]
    expanded = [direct, associated]
    service, metadata, _ = _service(tmp_path, monkeypatch, results, [])
    memory = service._memory_service
    memory.expand_living_document_associations = AsyncMock(return_value=expanded)

    output = await service.search_actor_memory(" synthetic ", top_k=3, enable_association=True)

    memory.search_memory.assert_awaited_once_with(
        "synthetic", top_k=3, enable_association=False, return_bundles=False
    )
    memory.expand_living_document_associations.assert_awaited_once()
    expansion = memory.expand_living_document_associations.await_args
    assert expansion.args == (results,)
    assert expansion.kwargs["context_key"] == "life_engine/memory"
    assert expansion.kwargs["limit"] == 3
    assert type(expansion.kwargs["random_seed"]) is int
    memory.build_memory_bundles.assert_awaited_once_with(
        query="synthetic", results=expanded, top_k=3
    )
    assert "【直接命中的记忆】" in output
    assert "【联想扩散结果】" in output
    assert "file_ref=subject-file:doc-associated@version-associated" in output
    _assert_exact_read_hint(output)
    metadata.assert_not_called()
    service._workspace_dir.assert_not_called()


@pytest.mark.parametrize("selected", [True, False])
@pytest.mark.parametrize("options", [{}, {"enable_association": False}])
async def test_association_off_skips_expansion_and_bundle_lineage(
    tmp_path, monkeypatch, selected, options
):
    direct = _hit()
    service, metadata, _ = _service(
        tmp_path, monkeypatch, [direct], [], selected=selected
    )
    memory = service._memory_service
    memory.expand_living_document_associations = AsyncMock(
        side_effect=AssertionError("baseline must not expand living associations")
    )
    memory.build_memory_bundles.side_effect = AssertionError(
        "baseline must not read bundle lineage or corrections"
    )

    output = await service.search_actor_memory(
        " synthetic ", top_k=3, **options
    )

    memory.search_memory.assert_awaited_once_with(
        "synthetic", top_k=3, enable_association=False, return_bundles=False
    )
    memory.expand_living_document_associations.assert_not_awaited()
    memory.build_memory_bundles.assert_not_awaited()
    assert "【直接命中的记忆】" in output
    assert "Synthetic captured snippet" in output
    assert "file_ref=subject-file:doc-current@version-current" in output
    assert "【联想扩散结果】" not in output
    assert "【可追溯记忆包】" not in output
    _assert_exact_read_hint(output)
    metadata.assert_not_called()
    service._workspace_dir.assert_not_called()


async def test_association_off_empty_results_do_not_expand_or_build_bundles(
    tmp_path, monkeypatch
):
    service, _, _ = _service(tmp_path, monkeypatch, [], [])
    memory = service._memory_service
    memory.expand_living_document_associations = AsyncMock()

    assert await service.search_actor_memory(
        "synthetic", enable_association=False
    ) == ""

    memory.search_memory.assert_awaited_once()
    memory.expand_living_document_associations.assert_not_awaited()
    memory.build_memory_bundles.assert_not_awaited()


async def test_association_off_preserves_search_failure(
    tmp_path, monkeypatch
):
    service, _, _ = _service(tmp_path, monkeypatch, [], [])
    memory = service._memory_service
    memory.search_memory.side_effect = RuntimeError("ManagedIndexProjectionStale")
    memory.expand_living_document_associations = AsyncMock()

    with pytest.raises(RuntimeError, match="ManagedIndexProjectionStale"):
        await service.search_actor_memory("synthetic", enable_association=False)

    memory.search_memory.assert_awaited_once()
    memory.expand_living_document_associations.assert_not_awaited()
    memory.build_memory_bundles.assert_not_awaited()


@pytest.mark.parametrize("invalid", [None, "false", 0, 1])
async def test_association_flag_requires_bool_before_search(
    tmp_path, monkeypatch, invalid
):
    service, _, _ = _service(tmp_path, monkeypatch, [], [])
    memory = service._memory_service
    memory.expand_living_document_associations = AsyncMock()

    with pytest.raises(TypeError, match="enable_association must be a bool"):
        await service.search_actor_memory("synthetic", enable_association=invalid)

    memory.search_memory.assert_not_awaited()
    memory.expand_living_document_associations.assert_not_awaited()
    memory.build_memory_bundles.assert_not_awaited()


async def test_selected_bundle_failure_reports_recovery_without_stale_fallback_or_private_error(
    tmp_path, monkeypatch
):
    result = _hit()
    result.snippet = "STALE PRIVATE SNIPPET"
    service, metadata, log = _service(
        tmp_path,
        monkeypatch,
        [result],
        [],
        error=RuntimeError("ManagedIndexProjectionStale private-token-value"),
    )
    output = await service.search_actor_memory("synthetic", enable_association=True)
    assert "待恢复" in output and "RuntimeError" in output
    assert "STALE PRIVATE SNIPPET" not in output
    assert "【直接命中的记忆】" not in output
    assert "private-token-value" not in output
    log.warning.assert_called_once()
    assert "error_type=RuntimeError" in log.warning.call_args.args[0]
    assert "private-token-value" not in str(log.warning.call_args)
    metadata.assert_not_called()
    service._workspace_dir.assert_not_called()


async def test_unselected_legacy_fallback_keeps_original_metadata_and_footer(
    tmp_path, monkeypatch
):
    result = _hit(document_id="", version_id="")
    service, metadata, log = _service(
        tmp_path,
        monkeypatch,
        [result],
        [],
        selected=False,
        error=RuntimeError("private legacy failure"),
    )
    metadata.side_effect = None
    metadata.return_value = {"ext": ".md", "time_ago": "2小时前", "size": "25 B"}
    output = await service.search_actor_memory("synthetic")
    assert "Synthetic captured snippet" in output and "【直接命中的记忆】" in output
    assert ".md | 2小时前 | 25 B" in output
    assert (
        "以上仅为摘要。如需查看完整内容，可使用 fetch_life_memory 工具读取文件。"
        in output
    )
    assert "node_id=legacy-node" in output and "version_ids" not in output
    assert "private legacy failure" not in str(log.warning.call_args)
    metadata.assert_called_once_with(tmp_path / result.file_path)
