from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from plugins.life_engine.core.config import LifeEngineConfig
from plugins.life_engine.tools.file_tools import (
    LifeEngineEditFileTool,
    LifeEngineWriteFileTool,
)
from plugins.life_engine.trace.store import LifeTraceStore
from plugins.life_engine.trace.tools import (
    LifeTraceFileHistoryTool,
    LifeTracePreviewVersionTool,
    LifeTraceRecentChangesTool,
    LifeTraceShowDiffTool,
)


def _plugin(tmp_path: Path) -> SimpleNamespace:
    config = LifeEngineConfig()
    config.settings.workspace_path = str(tmp_path)
    return SimpleNamespace(config=config)


async def test_write_and_edit_file_records_trace(tmp_path: Path) -> None:
    plugin = _plugin(tmp_path)
    writer = LifeEngineWriteFileTool(plugin=plugin)
    editor = LifeEngineEditFileTool(plugin=plugin)

    ok, write_payload = await writer.execute(
        "SOUL.md",
        "hello\n",
        reason="initial soul draft",
    )
    assert ok is True
    assert isinstance(write_payload, dict)
    assert write_payload["trace_id"].startswith("trace_")

    ok, edit_payload = await editor.execute(
        "SOUL.md",
        "hello",
        "hello elysia",
        reason="add name",
    )
    assert ok is True
    assert isinstance(edit_payload, dict)
    assert edit_payload["trace_id"].startswith("trace_")

    records = LifeTraceStore(tmp_path).history("SOUL.md")
    assert len(records) == 2
    assert records[0].operation == "edit"
    assert records[0].reason == "add name"
    assert records[1].operation == "write"
    assert records[1].reason == "initial soul draft"


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
