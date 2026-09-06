"""Minimal subject-file continuity using only temporary authority and trace data.

These tests exercise the real file tools, service actor checks, exact-byte
SQLite subject store and local projector. They do not start Life Engine,
invoke a model, or claim to demonstrate autonomous subject learning.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from plugins.life_engine.service import LifeEngineService
from plugins.life_engine.service.consciousness import (
    ConsciousnessInstance,
    ConsciousnessRegistry,
)
from plugins.life_engine.storage.subject_contracts import SubjectDocumentStorePort
from plugins.life_engine.storage.subject_factory import open_subject_document_store
from plugins.life_engine.tools import file_tools
from plugins.life_engine.tools.file_tools import (
    LifeEngineApplyPatchTool,
    LifeEngineEditFileTool,
    LifeEngineReadFileTool,
    LifeEngineWriteFileTool,
)
from plugins.life_engine.trace.store import AsyncLocalLifeTraceStore
from test.plugins.life_engine.test_subject_document_storage_contract import (
    _local_store,
    _selected_local_service,
)

_ACTOR_ID = "consciousness-memory-fixture"
_STREAM_ID = "chat:memory-fixture"
_OCCURRED_AT = "2026-09-06T03:00:00+00:00"
_MEMORY_PATH = "life_engine_workspace/MEMORY.md"


def _memory_plugin(
    store: SubjectDocumentStorePort,
    *,
    data_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    actor_status: str = "active",
) -> tuple[SimpleNamespace, LifeEngineService, AsyncLocalLifeTraceStore]:
    """Bind real temporary persistence and an in-memory operational registry."""

    service = _selected_local_service(store, data_root=data_root)
    registry = ConsciousnessRegistry(bootstrap=False)
    registry.register(
        ConsciousnessInstance(
            instance_id=_ACTOR_ID,
            stream_ids=[_STREAM_ID],
            status=actor_status,
            session_id="session-memory-fixture",
        )
    )
    service._consciousness_registry = registry
    service._memory_service = None
    plugin = service.plugin
    plugin.service = service
    trace_store = AsyncLocalLifeTraceStore(data_root / "life_engine_workspace")
    monkeypatch.setattr(service, "life_trace_store", lambda: trace_store)
    # Ordinary file helpers still consult the singleton for optional indexing.
    # Bind both routes explicitly so no test can reach a live service or data.
    monkeypatch.setattr(
        LifeEngineService,
        "get_instance",
        classmethod(lambda cls: service),
    )
    monkeypatch.setattr(file_tools, "_get_life_engine_service", lambda plugin: service)
    return plugin, service, trace_store


def _bound_write_tool(
    plugin: SimpleNamespace,
    *,
    source_id: str,
    actor_id: str = _ACTOR_ID,
) -> LifeEngineWriteFileTool:
    """Supply trusted call bindings, never model-authored actor arguments."""

    tool = LifeEngineWriteFileTool(plugin=plugin)
    tool._bind_runtime_context(
        stream_id=_STREAM_ID,
        message=SimpleNamespace(
            stream_id=_STREAM_ID,
            message_id=source_id,
            extra={},
            time=_OCCURRED_AT,
        ),
        tool_call_id=f"tool-call:{source_id}",
    )
    tool._life_source_instance_id = actor_id
    tool._life_source_occurrence_id = source_id
    tool._life_source_occurred_at = _OCCURRED_AT
    tool._runtime_task_name = "life_chatter"
    return tool


async def test_memory_tool_versions_survive_new_service_and_keep_exact_origin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _local_store(tmp_path) as (runtime, store, _):
        data_root = tmp_path / "data"
        plugin, service, trace = _memory_plugin(
            store, data_root=data_root, monkeypatch=monkeypatch
        )
        first_text = "# 记忆\n蓝色笔记本在抽屉里。\n"
        second_text = "# 记忆\n蓝色笔记本后来移到了书架。\n"
        versions = []
        for ordinal, content in enumerate((first_text, second_text), start=1):
            source = f"activity:memory-write:{ordinal}"
            ok, result = await _bound_write_tool(
                plugin, source_id=source
            ).execute("MEMORY.md", content, reason=f"fixture revision {ordinal}")
            assert ok, result
            assert isinstance(result, dict)
            assert result["trace_id"].startswith("trace_")
            version = await service.read_subject_authority_file("MEMORY.md")
            assert version.content_bytes == content.encode("utf-8")
            assert version.content_hash == hashlib.sha256(
                content.encode("utf-8")
            ).hexdigest()
            assert version.semantic_actor_id == _ACTOR_ID
            assert version.semantic_source_id == source
            assert version.occurred_at == _OCCURRED_AT
            assert version.provenance_status == "complete"
            assert version.recorded_by == "life_engine"
            assert version.recorded_source == "nucleus_file_tool"
            assert version.occurrence_id.startswith("file-tool:")
            versions.append(version)

        first, second = versions
        assert second.parent_version_id == first.version_id
        assert second.document_id == first.document_id
        for _ in range(3):
            ok, result = await LifeEngineReadFileTool(plugin=plugin).execute(
                "MEMORY.md", limit=0
            )
            assert ok, result
            assert isinstance(result, dict)
            assert result["source_authority"] == "subject_document_store"
            assert result["subject_version_id"] == second.version_id
            assert result["content"] == "1\t# 记忆\n2\t蓝色笔记本后来移到了书架。"
            assert result["file_content_sha256"] == second.content_hash

        assert len(await trace.history("MEMORY.md")) == 2
        # A fresh adapter and service read the same real database, rather than
        # sharing a file-tool result cache or the old service's in-memory state.
        reopened_store = await open_subject_document_store(
            runtime, initialize_schema=False
        )
        reopened_plugin, reopened_service, _ = _memory_plugin(
            reopened_store, data_root=data_root, monkeypatch=monkeypatch
        )
        assert reopened_service is not service
        assert reopened_store is not store
        current = await reopened_service.read_subject_authority_file("MEMORY.md")
        assert current == second
        ok, result = await LifeEngineReadFileTool(plugin=reopened_plugin).execute(
            "MEMORY.md", limit=0
        )
        assert ok, result
        assert isinstance(result, dict)
        assert result["subject_version_id"] == second.version_id
        assert "后来移到了书架" in result["content"]
        history = await reopened_store.list_history(_MEMORY_PATH)
        assert {item.version_id: item for item in history} == {
            first.version_id: first,
            second.version_id: second,
        }
        assert await reopened_store.get_version(first.version_id) == first


@pytest.mark.parametrize("disk_state", ["stale", "absent"])
async def test_memory_reads_selected_latest_without_trusting_disk_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    disk_state: str,
) -> None:
    async with _local_store(tmp_path) as (_, store, _):
        data_root = tmp_path / "data"
        plugin, service, _ = _memory_plugin(
            store, data_root=data_root, monkeypatch=monkeypatch
        )
        content = "当前由主体写下的记忆。\n"
        ok, result = await _bound_write_tool(
            plugin, source_id="activity:latest-memory"
        ).execute("MEMORY.md", content)
        assert ok, result
        current = await service.read_subject_authority_file("MEMORY.md")
        before_history = await store.list_history(_MEMORY_PATH)
        disk = data_root / "life_engine_workspace" / "MEMORY.md"
        if disk_state == "stale":
            disk.write_bytes(b"stale disk must not become authority\n")
        else:
            disk.unlink()

        ok, result = await LifeEngineReadFileTool(plugin=plugin).execute(
            "MEMORY.md", limit=0
        )
        assert ok, result
        assert isinstance(result, dict)
        assert result["subject_version_id"] == current.version_id
        assert result["content"] == "1\t当前由主体写下的记忆。"
        assert result["source_authority"] == "subject_document_store"
        assert await store.list_history(_MEMORY_PATH) == before_history
        if disk_state == "stale":
            assert disk.read_bytes() == b"stale disk must not become authority\n"
        else:
            assert not disk.exists()


@pytest.mark.parametrize(
    ("failure", "error_name"),
    [
        ("missing_head", "SelectedSubjectHeadMissing"),
        ("missing_store", "SelectedSubjectStorageNotStarted"),
    ],
)
async def test_missing_subject_authority_never_falls_back_to_existing_disk(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
    error_name: str,
) -> None:
    async with _local_store(tmp_path) as (_, store, _):
        data_root = tmp_path / "data"
        plugin, service, _ = _memory_plugin(
            store, data_root=data_root, monkeypatch=monkeypatch
        )
        disk = data_root / "life_engine_workspace" / "MEMORY.md"
        disk.write_bytes(b"disk-only content must not masquerade as memory\n")
        if failure == "missing_store":
            service._subject_document_store = None

        ok, error = await LifeEngineReadFileTool(plugin=plugin).execute("MEMORY.md")
        assert ok is False
        assert error_name in str(error)
        assert "disk-only content" not in str(error)
        assert await store.get_head(_MEMORY_PATH) is None
        assert disk.read_bytes() == (
            b"disk-only content must not masquerade as memory\n"
        )


@pytest.mark.parametrize("missing_origin", ["actor", "source", "both"])
async def test_memory_write_without_bound_subject_origin_preserves_disk_and_head(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    missing_origin: str,
) -> None:
    async with _local_store(tmp_path) as (_, store, _):
        data_root = tmp_path / "data"
        plugin, _, trace = _memory_plugin(
            store, data_root=data_root, monkeypatch=monkeypatch
        )
        disk = data_root / "life_engine_workspace" / "MEMORY.md"
        disk.write_bytes(b"unchanged local bytes\n")
        tool = _bound_write_tool(
            plugin,
            source_id="" if missing_origin in {"source", "both"} else "activity:bound",
            actor_id="" if missing_origin in {"actor", "both"} else _ACTOR_ID,
        )
        ok, error = await tool.execute("MEMORY.md", "must not be written\n")
        assert ok is False
        assert "SubjectFileWriteOriginRequired" in str(error)
        assert await store.get_head(_MEMORY_PATH) is None
        assert await store.list_history(_MEMORY_PATH) == []
        assert await trace.history("MEMORY.md") == []
        assert disk.read_bytes() == b"unchanged local bytes\n"


async def test_inactive_subject_cannot_commit_memory_via_real_file_tool(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _local_store(tmp_path) as (_, store, _):
        data_root = tmp_path / "data"
        plugin, service, trace = _memory_plugin(
            store,
            data_root=data_root,
            monkeypatch=monkeypatch,
            actor_status="suspended",
        )
        assert not await service._validate_learning_decision_actor(_ACTOR_ID)
        disk = data_root / "life_engine_workspace" / "MEMORY.md"
        disk.write_bytes(b"still unchanged\n")

        ok, error = await _bound_write_tool(
            plugin, source_id="activity:inactive-subject"
        ).execute("MEMORY.md", "inactive actor replacement\n")
        assert ok is False
        assert "SubjectFileWriteActorIsNotActive" in str(error)
        assert await store.get_head(_MEMORY_PATH) is None
        assert await trace.history("MEMORY.md") == []
        assert disk.read_bytes() == b"still unchanged\n"


async def test_authority_append_failure_does_not_write_disk_or_file_trace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _local_store(tmp_path) as (_, store, _):
        data_root = tmp_path / "data"
        plugin, service, trace = _memory_plugin(
            store, data_root=data_root, monkeypatch=monkeypatch
        )
        first_content = "原始、已提交的记忆。\n"
        ok, result = await _bound_write_tool(
            plugin, source_id="activity:before-storage-fault"
        ).execute("MEMORY.md", first_content)
        assert ok, result
        before_head = await store.get_head(_MEMORY_PATH)
        before_history = await store.list_history(_MEMORY_PATH)
        before_trace = await trace.history("MEMORY.md")
        append_attempts = []

        async def reject_append(command):
            append_attempts.append(command)
            raise RuntimeError("fixture subject append rejected")

        monkeypatch.setattr(store, "append_version", reject_append)
        ok, error = await _bound_write_tool(
            plugin, source_id="activity:rejected-storage-write"
        ).execute("MEMORY.md", "不应写入的新字节。\n")
        assert ok is False
        assert "fixture subject append rejected" in str(error)
        assert len(append_attempts) == 1
        assert append_attempts[0].semantic_actor_id == _ACTOR_ID
        assert append_attempts[0].semantic_source_id == "activity:rejected-storage-write"
        assert await store.get_head(_MEMORY_PATH) == before_head
        assert await store.list_history(_MEMORY_PATH) == before_history
        assert await trace.history("MEMORY.md") == before_trace
        assert (data_root / "life_engine_workspace" / "MEMORY.md").read_bytes() == (
            first_content.encode("utf-8")
        )
        current = await service.read_subject_authority_file("MEMORY.md")
        assert current.content_bytes == first_content.encode("utf-8")


async def _edit_memory_position(
    plugin: SimpleNamespace,
    *,
    operation: str,
    source_id: str,
) -> tuple[bool, str | dict]:
    """Invoke an existing edit tool with the same trusted fixture bindings."""

    origin = _bound_write_tool(plugin, source_id=source_id)
    tool = (
        LifeEngineEditFileTool(plugin=plugin)
        if operation == "edit"
        else LifeEngineApplyPatchTool(plugin=plugin)
    )
    tool._bind_runtime_context(
        stream_id=_STREAM_ID,
        message=origin.trigger_message,
        tool_call_id=origin._tool_call_id,
    )
    tool._life_source_instance_id = _ACTOR_ID
    tool._life_source_occurrence_id = source_id
    tool._life_source_occurred_at = _OCCURRED_AT
    tool._runtime_task_name = "life_chatter"
    if operation == "edit":
        return await tool.execute(
            "MEMORY.md",
            "位置：待更新",
            "位置：已核对",
            reason="fixture edit must preserve the selected current version",
        )
    assert operation == "patch"
    return await tool.execute(
        "*** Begin Patch\n"
        "*** Update File: MEMORY.md\n"
        "@@\n"
        "-位置：待更新\n"
        "+位置：已核对\n"
        "*** End Patch\n",
        reason="fixture patch must preserve the selected current version",
    )


@pytest.mark.parametrize("operation", ["edit", "patch"])
@pytest.mark.parametrize("disk_state", ["stale", "absent"])
async def test_subject_edits_use_latest_authority_and_keep_projection_guard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    disk_state: str,
) -> None:
    async with _local_store(tmp_path) as (_, store, _):
        data_root = tmp_path / "data"
        plugin, service, trace = _memory_plugin(
            store, data_root=data_root, monkeypatch=monkeypatch
        )
        first_text = "# 记忆\n位置：待更新\n旧尾注。\n"
        second_text = "# 记忆\n位置：待更新\n新版保留：请先核对来源。\n"
        for ordinal, content in enumerate((first_text, second_text), start=1):
            ok, result = await _bound_write_tool(
                plugin, source_id=f"activity:edit-base:{ordinal}"
            ).execute("MEMORY.md", content)
            assert ok, result
        second = await service.read_subject_authority_file("MEMORY.md")
        before_history = await store.list_history(_MEMORY_PATH)
        before_trace = await trace.history("MEMORY.md")
        assert len(before_history) == 2
        disk = data_root / "life_engine_workspace" / "MEMORY.md"
        if disk_state == "stale":
            disk.write_bytes(first_text.encode("utf-8"))
        else:
            disk.unlink()

        source_id = f"activity:{operation}-latest:{disk_state}"
        ok, result = await _edit_memory_position(
            plugin, operation=operation, source_id=source_id
        )
        # Current local commits append authority before projecting. The
        # projector deliberately refuses to overwrite divergent/missing
        # parent bytes. This test does not claim the whole tool succeeded:
        # only its authoritative edit base is fixed in this increment.
        assert ok is False
        assert "SubjectProjectionFailed" in str(result)
        assert "workspace bytes diverged from the authoritative parent" in str(result)
        current = await service.read_subject_authority_file("MEMORY.md")
        expected = second_text.replace("位置：待更新", "位置：已核对")
        assert current.content_bytes == expected.encode("utf-8")
        assert current.parent_version_id == second.version_id
        assert current.document_id == second.document_id
        assert current.semantic_actor_id == _ACTOR_ID
        assert current.semantic_source_id == source_id
        assert current.provenance_status == "complete"
        history = await store.list_history(_MEMORY_PATH)
        assert len(history) == 3
        stored_versions = {version.version_id: version for version in history}
        for version in before_history:
            assert stored_versions[version.version_id] == version
        assert stored_versions[current.version_id] == current
        projection = await store.get_projection_task(_MEMORY_PATH, current.version_id)
        assert projection is not None
        assert projection.state == "failed"
        assert await trace.history("MEMORY.md") == before_trace
        if disk_state == "stale":
            assert disk.read_bytes() == first_text.encode("utf-8")
        else:
            assert not disk.exists()

        # After the partial failure the actual committed current version
        # remains discoverable, including the v2 text absent from stale disk.
        read_ok, read_result = await LifeEngineReadFileTool(plugin=plugin).execute(
            "MEMORY.md", limit=0
        )
        assert read_ok, read_result
        assert isinstance(read_result, dict)
        assert read_result["subject_version_id"] == current.version_id
        assert "位置：已核对" in read_result["content"]
        assert "新版保留：请先核对来源。" in read_result["content"]
        assert "旧尾注" not in read_result["content"]


@pytest.mark.parametrize("operation", ["edit", "patch"])
@pytest.mark.parametrize(
    ("failure", "error_name"),
    [
        ("missing_head", "SelectedSubjectHeadMissing"),
        ("missing_store", "SelectedSubjectStorageNotStarted"),
    ],
)
async def test_subject_edit_input_does_not_fall_back_when_authority_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    failure: str,
    error_name: str,
) -> None:
    async with _local_store(tmp_path) as (_, store, _):
        data_root = tmp_path / "data"
        plugin, service, trace = _memory_plugin(
            store, data_root=data_root, monkeypatch=monkeypatch
        )
        disk = data_root / "life_engine_workspace" / "MEMORY.md"
        original = "位置：待更新\n只有磁盘知道的内容。\n".encode("utf-8")
        disk.write_bytes(original)
        if failure == "missing_store":
            service._subject_document_store = None

        ok, error = await _edit_memory_position(
            plugin, operation=operation, source_id="activity:missing-edit-authority"
        )
        assert ok is False
        assert error_name in str(error)
        assert "只有磁盘知道的内容" not in str(error)
        assert await store.get_head(_MEMORY_PATH) is None
        assert await store.list_history(_MEMORY_PATH) == []
        assert await trace.history("MEMORY.md") == []
        assert disk.read_bytes() == original
