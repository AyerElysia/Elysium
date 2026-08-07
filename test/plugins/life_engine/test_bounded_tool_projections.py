"""Regression coverage for bounded model-visible Life Engine tool results."""

from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from plugins.life_engine.core.config import LifeEngineConfig
from plugins.life_engine.tools.autonomy_tools import (
    LifeEngineManageAutonomyIntentTool,
)
from plugins.life_engine.tools.bounded_projection import (
    CHAT_TOOL_RESULT_MAX_BYTES,
    CORE_TOOL_RESULT_MAX_BYTES,
    project_bounded_items,
)
from plugins.life_engine.tools.event_grep_tools import LifeEngineGrepEventsTool
from plugins.life_engine.tools.file_tools import (
    LifeEngineListFilesTool,
    LifeEngineReadFileTool,
)
from plugins.life_engine.tools.grep_tools import LifeEngineGrepFileTool


def _serialized_bytes(payload: object) -> int:
    return len(str(payload).encode("utf-8"))


def _workspace_plugin(tmp_path: Path) -> SimpleNamespace:
    config = LifeEngineConfig()
    config.settings.workspace_path = str(tmp_path)
    return SimpleNamespace(config=config)


def _assert_exact_budget(payload: dict[str, Any], cap: int) -> None:
    actual = _serialized_bytes(payload)
    assert actual == payload["delivered_bytes"]
    assert actual <= payload["budget_bytes"] <= cap


def test_bounded_items_use_task_caps_and_stable_utf8_pages() -> None:
    items = [
        {"ordinal": index, "content": f"第{index}项-" + "你好世界" * 900}
        for index in range(7)
    ]
    refs = [f"item:{index}" for index in range(len(items))]
    continuation = ""
    delivered_refs: list[str] = []

    for _ in range(20):
        payload = project_bounded_items(
            projection_name="test-items",
            task_name="unknown-task",
            requested_max_bytes=None,
            binding={"query": "稳定"},
            frontier={"revision": 7},
            base_payload={"action": "test"},
            items_key="items",
            items=items,
            item_refs=refs,
            continuation=continuation,
        )
        _assert_exact_budget(payload, CORE_TOOL_RESULT_MAX_BYTES)
        assert payload["task_name"] == "core"
        for item in payload["items"]:
            item["excerpt"].encode("utf-8")
            delivered_refs.append(item["_projection"]["ref"])
        continuation = payload["continuation"]
        if not continuation:
            break

    assert delivered_refs == refs

    chat_payload = project_bounded_items(
        projection_name="test-chat-items",
        task_name="expression",
        requested_max_bytes=None,
        binding={},
        frontier="stable",
        base_payload={},
        items_key="items",
        items=[{"value": "ok"}],
        item_refs=["item:ok"],
    )
    _assert_exact_budget(chat_payload, CHAT_TOOL_RESULT_MAX_BYTES)
    assert chat_payload["budget_bytes"] == CHAT_TOOL_RESULT_MAX_BYTES

    tightened = project_bounded_items(
        projection_name="test-tight-items",
        task_name="life_chatter",
        requested_max_bytes=4096,
        binding={},
        frontier="stable",
        base_payload={},
        items_key="items",
        items=[{"value": "你好" * 3000}],
        item_refs=["item:tight"],
    )
    _assert_exact_budget(tightened, 4096)


def test_bounded_items_reject_tampered_or_stale_continuations() -> None:
    kwargs = {
        "projection_name": "test-cursor",
        "task_name": "core",
        "requested_max_bytes": 2048,
        "binding": {"query": "q"},
        "frontier": {"revision": 1},
        "base_payload": {},
        "items_key": "items",
        "items": [{"value": "内容" * 1500}, {"value": "下一项"}],
        "item_refs": ["item:1", "item:2"],
    }
    first = project_bounded_items(**kwargs)
    cursor = first["continuation"]
    assert cursor

    replacement = "A" if cursor[-1] != "A" else "B"
    with pytest.raises(ValueError, match="continuation"):
        project_bounded_items(**kwargs, continuation=cursor[:-1] + replacement)

    with pytest.raises(ValueError, match="query/task/frontier"):
        project_bounded_items(
            **{**kwargs, "frontier": {"revision": 2}},
            continuation=cursor,
        )


@pytest.mark.asyncio
async def test_autonomy_list_is_bounded_but_mutations_are_unchanged() -> None:
    intents = [
        {
            "intent_id": f"intent-{index}",
            "motivation": "继续看看这件事" * 1000,
            "status": "scheduled",
        }
        for index in range(12)
    ]
    calls: list[dict[str, Any]] = []

    class FakeService:
        async def manage_autonomy_intent(self, **kwargs: Any) -> dict[str, Any]:
            calls.append(dict(kwargs))
            if kwargs["action"] == "list":
                return {"action": "list", "intents": intents}
            return {"action": kwargs["action"], "mutated": True}

    tool = LifeEngineManageAutonomyIntentTool(
        plugin=SimpleNamespace(service=FakeService())
    )
    tool._runtime_task_name = "core"

    ok, first = await tool.execute(action="list")
    assert ok is True
    assert isinstance(first, dict)
    _assert_exact_budget(first, CORE_TOOL_RESULT_MAX_BYTES)
    assert first["continuation"]

    ok, second = await tool.execute(
        action="list",
        continuation=first["continuation"],
    )
    assert ok is True
    assert isinstance(second, dict)
    _assert_exact_budget(second, CORE_TOOL_RESULT_MAX_BYTES)
    assert (
        first["intents"][0]["_projection"]["ref"]
        != second["intents"][0]["_projection"]["ref"]
    )

    ok, mutation = await tool.execute(
        action="pause",
        intent_id="intent-1",
        continuation="not-a-list-cursor",
        max_bytes=1024,
    )
    assert ok is True
    assert mutation == {"action": "pause", "mutated": True}
    assert calls[-1]["action"] == "pause"


@pytest.mark.asyncio
async def test_event_grep_projection_pages_and_rejects_source_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_revision = {"value": 1}

    async def fake_grep_life_events(**_kwargs: Any) -> dict[str, Any]:
        matches = [
            {
                "event": {
                    "event_id": f"event-{index}",
                    "content": "事件正文" * 1200,
                },
                "context_before": [],
                "context_after": [],
            }
            for index in range(6)
        ]
        return {
            "query": "事件",
            "matches": matches,
            "stats": {"matched_events": len(matches)},
            "source_frontier": {"revision": source_revision["value"]},
        }

    monkeypatch.setattr(
        "plugins.life_engine.tools.event_grep_tools.grep_life_events",
        fake_grep_life_events,
    )
    tool = LifeEngineGrepEventsTool(plugin=SimpleNamespace())
    tool._runtime_task_name = "core"

    ok, first = await tool.execute(query="事件")
    assert ok is True
    assert isinstance(first, dict)
    _assert_exact_budget(first, CORE_TOOL_RESULT_MAX_BYTES)
    assert first["continuation"]

    ok, second = await tool.execute(
        query="事件",
        continuation=first["continuation"],
    )
    assert ok is True
    assert isinstance(second, dict)
    assert (
        first["matches"][0]["_projection"]["ref"]
        != second["matches"][0]["_projection"]["ref"]
    )

    source_revision["value"] = 2
    ok, error = await tool.execute(
        query="事件",
        continuation=first["continuation"],
    )
    assert ok is False
    assert "query/task/frontier" in str(error)


@pytest.mark.asyncio
async def test_read_file_limit_zero_uses_utf8_safe_bounded_pages(
    tmp_path: Path,
) -> None:
    lines = [f"第{index}行：" + "星光与故事" * 300 for index in range(80)]
    target = tmp_path / "large.txt"
    target.write_text("\n".join(lines), encoding="utf-8")
    before_mtime = target.stat().st_mtime_ns
    before_hash = hashlib.sha256(target.read_bytes()).hexdigest()
    tool = LifeEngineReadFileTool(plugin=_workspace_plugin(tmp_path))
    tool._runtime_task_name = "core"
    continuation = ""
    chunks: list[str] = []

    for _ in range(100):
        ok, payload = await tool.execute(
            path="large.txt",
            limit=0,
            continuation=continuation,
        )
        assert ok is True
        assert isinstance(payload, dict)
        _assert_exact_budget(payload, CORE_TOOL_RESULT_MAX_BYTES)
        payload["content"].encode("utf-8")
        chunks.append(payload["content"])
        continuation = payload["continuation"]
        if not continuation:
            break

    expected = "\n".join(
        f"{index}\t{line}" for index, line in enumerate(lines, start=1)
    )
    assert "".join(chunks) == expected
    assert target.stat().st_mtime_ns == before_mtime
    assert hashlib.sha256(target.read_bytes()).hexdigest() == before_hash


@pytest.mark.asyncio
async def test_read_file_rejects_continuation_after_file_change(
    tmp_path: Path,
) -> None:
    target = tmp_path / "changing.txt"
    target.write_text("旧内容" * 10000, encoding="utf-8")
    tool = LifeEngineReadFileTool(plugin=_workspace_plugin(tmp_path))
    tool._runtime_task_name = "core"

    ok, first = await tool.execute(path="changing.txt", limit=0)
    assert ok is True
    assert isinstance(first, dict)
    assert first["continuation"]
    target.write_text("新内容" * 10000, encoding="utf-8")

    ok, error = await tool.execute(
        path="changing.txt",
        limit=0,
        continuation=first["continuation"],
    )
    assert ok is False
    assert "query/task/frontier" in str(error)


@pytest.mark.asyncio
async def test_list_files_recursive_pages_are_stable_and_change_safe(
    tmp_path: Path,
) -> None:
    for directory_index in range(4):
        directory = tmp_path / f"目录-{directory_index}"
        directory.mkdir()
        for file_index in range(40):
            (directory / f"文件-{file_index:03d}-星光故事.txt").write_text(
                "内容",
                encoding="utf-8",
            )
    tool = LifeEngineListFilesTool(plugin=_workspace_plugin(tmp_path))
    tool._runtime_task_name = "core"
    continuation = ""
    paths: list[str] = []
    first_cursor = ""

    for page_index in range(100):
        ok, payload = await tool.execute(
            path="",
            recursive=True,
            max_depth=3,
            continuation=continuation,
        )
        assert ok is True
        assert isinstance(payload, dict)
        _assert_exact_budget(payload, CORE_TOOL_RESULT_MAX_BYTES)
        paths.extend(item["path"] for item in payload["items"])
        continuation = payload["continuation"]
        if page_index == 0:
            first_cursor = continuation
        if not continuation:
            break

    expected = sorted(
        str(path.relative_to(tmp_path))
        for path in tmp_path.rglob("*")
    )
    assert paths == expected
    assert len(paths) == len(set(paths))

    (tmp_path / "后来新增.txt").write_text("new", encoding="utf-8")
    ok, error = await tool.execute(
        path="",
        recursive=True,
        max_depth=3,
        continuation=first_cursor,
    )
    assert ok is False
    assert "query/task/frontier" in str(error)


@pytest.mark.asyncio
async def test_file_grep_pages_are_bounded_and_reject_changed_files(
    tmp_path: Path,
) -> None:
    for index in range(8):
        (tmp_path / f"note-{index}.txt").write_text(
            "needle " + "很长的中文正文" * 1200,
            encoding="utf-8",
        )
    tool = LifeEngineGrepFileTool(plugin=_workspace_plugin(tmp_path))
    tool._runtime_task_name = "core"

    ok, first = await tool.execute(
        pattern="needle",
        output_mode="content",
        max_results=20,
    )
    assert ok is True
    assert isinstance(first, dict)
    _assert_exact_budget(first, CORE_TOOL_RESULT_MAX_BYTES)
    assert first["continuation"]

    ok, second = await tool.execute(
        pattern="needle",
        output_mode="content",
        max_results=20,
        continuation=first["continuation"],
    )
    assert ok is True
    assert isinstance(second, dict)
    _assert_exact_budget(second, CORE_TOOL_RESULT_MAX_BYTES)
    assert (
        first["results"][0]["_projection"]["ref"]
        != second["results"][0]["_projection"]["ref"]
    )

    (tmp_path / "note-0.txt").write_text(
        "needle 已变化" * 1000,
        encoding="utf-8",
    )
    ok, error = await tool.execute(
        pattern="needle",
        output_mode="content",
        max_results=20,
        continuation=first["continuation"],
    )
    assert ok is False
    assert "query/task/frontier" in str(error)
