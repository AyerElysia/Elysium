"""Actor-facing exact source references, fake memory only, no models or stores."""

from __future__ import annotations

import socket
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

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
    output = await service.search_actor_memory("synthetic")
    assert "权威版本" in output
    assert "Synthetic understanding stays unchanged." in output
    assert output.count("file_ref=subject-file:doc-current@version-current") >= 2
    assert output.count("file_ref=subject-file:doc-old@version-old") == 2
    assert "document_id=doc-old" in output and "version_id=version-old" in output
    assert "node_id=legacy-old-neighbour" in output
    assert "非当前绑定、不能按当前路径回取" in output
    assert "当前路径不存在" not in output
    assert "version_ids={路径: version_id}" in output and "nucleus_read_file" in output
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
    assert "version_ids={路径: version_id}" in output
    metadata.assert_not_called()
    service._workspace_dir.assert_not_called()


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
    output = await service.search_actor_memory("synthetic")
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
