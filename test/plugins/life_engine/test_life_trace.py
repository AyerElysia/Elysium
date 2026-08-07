from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from plugins.life_engine.core.config import LifeEngineConfig
from plugins.life_engine.tools.file_tools import (
    LifeEngineEditFileTool,
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
