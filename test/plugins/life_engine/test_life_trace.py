from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from plugins.life_engine.core.config import LifeEngineConfig
from plugins.life_engine.tools.file_tools import (
    LifeEngineEditFileTool,
    LifeEngineMakeDirectoryTool,
    LifeEngineWriteFileTool,
)
from plugins.life_engine.trace.store import AsyncLocalLifeTraceStore, LifeTraceStore
from plugins.life_engine.trace.tools import (
    LifeTraceFileHistoryTool,
    LifeTracePreviewVersionTool,
    LifeTraceRecentChangesTool,
    LifeTraceShowDiffTool,
)


def _plugin(tmp_path: Path) -> SimpleNamespace:
    config = LifeEngineConfig()
    config.settings.workspace_path = str(tmp_path)
    service = SimpleNamespace(
        _selectable_storage_enabled=False,
        life_trace_store=lambda: AsyncLocalLifeTraceStore(tmp_path),
    )
    return SimpleNamespace(config=config, service=service)


async def test_write_and_edit_file_records_trace(tmp_path: Path) -> None:
    plugin = _plugin(tmp_path)
    writer = LifeEngineWriteFileTool(plugin=plugin)
    editor = LifeEngineEditFileTool(plugin=plugin)

    ok, write_payload = await writer.execute(
        "notes/self-observation.md",
        "hello\n",
        reason="initial observation",
    )
    assert ok is True
    assert isinstance(write_payload, dict)
    assert write_payload["trace_id"].startswith("trace_")

    ok, edit_payload = await editor.execute(
        "notes/self-observation.md",
        "hello",
        "hello elysia",
        reason="add name",
    )
    assert ok is True
    assert isinstance(edit_payload, dict)
    assert edit_payload["trace_id"].startswith("trace_")

    records = LifeTraceStore(tmp_path).history("notes/self-observation.md")
    assert len(records) == 2
    assert records[0].operation == "edit"
    assert records[0].reason == "add name"
    assert records[1].operation == "write"
    assert records[1].reason == "initial observation"


@pytest.mark.parametrize("path", ["SOUL.md", "USER.md", "MEMORY.md"])
async def test_generic_file_tools_block_subject_authority_paths(
    tmp_path: Path,
    path: str,
) -> None:
    plugin = _plugin(tmp_path)
    target = tmp_path / path
    target.write_text("original\n", encoding="utf-8")

    ok, write_error = await LifeEngineWriteFileTool(plugin=plugin).execute(
        path,
        "replacement\n",
        reason="must not bypass authority",
    )
    assert ok is False
    assert "SubjectAuthorityDirectMutationBlocked" in str(write_error)

    ok, edit_error = await LifeEngineEditFileTool(plugin=plugin).execute(
        f"./{path}",
        "original",
        "replacement",
        reason="must not bypass authority",
    )
    assert ok is False
    assert "SubjectAuthorityDirectMutationBlocked" in str(edit_error)
    assert target.read_text(encoding="utf-8") == "original\n"
    assert LifeTraceStore(tmp_path).history(path) == []


async def test_generic_file_tool_blocks_symlink_to_subject_authority(
    tmp_path: Path,
) -> None:
    plugin = _plugin(tmp_path)
    target = tmp_path / "SOUL.md"
    target.write_text("original\n", encoding="utf-8")
    (tmp_path / "notes").mkdir()
    (tmp_path / "notes" / "alias.md").symlink_to(target)

    ok, error = await LifeEngineWriteFileTool(plugin=plugin).execute(
        "notes/alias.md",
        "replacement\n",
        reason="symlink must not bypass authority",
    )

    assert ok is False
    assert "SubjectAuthorityDirectMutationBlocked" in str(error)
    assert target.read_text(encoding="utf-8") == "original\n"


@pytest.mark.parametrize(
    "path",
    (
        "runtime/proactive/proactive.sqlite3",
        "runtime/proactive/proactive.sqlite3-wal",
        "runtime/proactive/authority.json",
        "runtime/proactive/authority.writer.lock",
        "runtime/proactive/backend-binding.json",
        "runtime/proactive/backend-binding.json.lock",
        "runtime/proactive/.authority.json.test.tmp",
        "thoughts/streams.json",
    ),
)
async def test_generic_file_tools_cannot_mutate_proactive_authority_or_archive(
    tmp_path: Path,
    path: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin = _plugin(tmp_path)
    target = tmp_path / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"original-authority-bytes\n")
    index_calls: list[str] = []

    async def record_index_call(*_args: object, **_kwargs: object) -> None:
        index_calls.append(path)

    monkeypatch.setattr(
        "plugins.life_engine.tools.file_tools._sync_memory_embedding_for_file",
        record_index_call,
    )

    write_ok, write_error = await LifeEngineWriteFileTool(plugin=plugin).execute(
        path,
        "replacement\n",
        reason="must not bypass proactive authority",
    )
    edit_ok, edit_error = await LifeEngineEditFileTool(plugin=plugin).execute(
        path,
        "original",
        "replacement",
        reason="must not bypass proactive authority",
    )

    assert write_ok is False and edit_ok is False
    assert "WorkspaceAuthorityMutationBlocked" in str(write_error)
    assert "WorkspaceAuthorityMutationBlocked" in str(edit_error)
    assert target.read_bytes() == b"original-authority-bytes\n"
    assert LifeTraceStore(tmp_path).history(path) == []
    assert index_calls == []


async def test_proactive_mutation_guard_uses_custom_config_and_resolves_symlinks(
    tmp_path: Path,
) -> None:
    plugin = _plugin(tmp_path)
    plugin.config.proactive.local_database_path = "state/custom.db"
    database = tmp_path / "state" / "custom.db"
    database.parent.mkdir(parents=True)
    database.write_bytes(b"database-authority")
    (tmp_path / "notes").mkdir()
    alias = tmp_path / "notes" / "alias.db"
    alias.symlink_to(database)

    ok, error = await LifeEngineWriteFileTool(plugin=plugin).execute(
        "notes/alias.db",
        "replacement",
    )

    assert ok is False
    assert "WorkspaceAuthorityMutationBlocked" in str(error)
    assert database.read_bytes() == b"database-authority"


async def test_mkdir_cannot_preempt_a_configured_authority_file(
    tmp_path: Path,
) -> None:
    plugin = _plugin(tmp_path)

    ok, error = await LifeEngineMakeDirectoryTool(plugin=plugin).execute(
        "runtime/proactive/proactive.sqlite3"
    )

    assert ok is False
    assert "WorkspaceAuthorityMutationBlocked" in str(error)
    assert not (tmp_path / "runtime" / "proactive" / "proactive.sqlite3").exists()


async def test_trace_query_tools(tmp_path: Path) -> None:
    plugin = _plugin(tmp_path)
    writer = LifeEngineWriteFileTool(plugin=plugin)
    editor = LifeEngineEditFileTool(plugin=plugin)

    await writer.execute("notes/a.md", "one\n", reason="create note")
    ok, edit_payload = await editor.execute("notes/a.md", "one", "two", reason="revise note")
    assert ok is True
    assert isinstance(edit_payload, dict)
    trace_id = edit_payload["trace_id"]

    ok, recent = await LifeTraceRecentChangesTool(plugin=plugin).execute(limit=5)
    assert ok is True
    assert isinstance(recent, dict)
    assert recent["count"] == 2

    ok, history = await LifeTraceFileHistoryTool(plugin=plugin).execute("notes/a.md")
    assert ok is True
    assert isinstance(history, dict)
    assert history["count"] == 2

    ok, diff_payload = await LifeTraceShowDiffTool(plugin=plugin).execute(trace_id)
    assert ok is True
    assert isinstance(diff_payload, dict)
    assert "-one" in diff_payload["diff"]
    assert "+two" in diff_payload["diff"]

    ok, before_payload = await LifeTracePreviewVersionTool(plugin=plugin).execute(
        trace_id,
        side="before",
    )
    assert ok is True
    assert isinstance(before_payload, dict)
    assert before_payload["content"] == "one\n"


def test_trace_store_rejects_internal_trace_path(tmp_path: Path) -> None:
    store = LifeTraceStore(tmp_path)
    with pytest.raises(ValueError):
        store.record_change(
            path=".life_trace/index.jsonl",
            before_content=None,
            after_content="bad",
            operation="write",
            tool_name="test",
        )
