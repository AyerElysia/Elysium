"""Durable TODO board: coding-agent write contract, no ranking."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from plugins.life_engine.core.config import LifeEngineConfig
from plugins.life_engine.prompts.sections import DEFAULT_HEARTBEAT_SECTIONS
from plugins.life_engine.tools.todo_tools import (
    LifeTodo,
    NucleusTodoTool,
    TodoStorage,
    apply_todo_write,
    format_todo_board,
    todo_health_snapshot,
)


def _make_plugin(tmp_path: Path) -> SimpleNamespace:
    config = LifeEngineConfig()
    config.settings.workspace_path = str(tmp_path)
    return SimpleNamespace(config=config)


def _tool(tmp_path: Path) -> NucleusTodoTool:
    return NucleusTodoTool(plugin=_make_plugin(tmp_path))  # type: ignore[arg-type]


def test_write_merge_upserts_and_keeps_other_open_items(tmp_path: Path) -> None:
    storage = TodoStorage(tmp_path)
    storage.save(
        [
            LifeTodo(id="todo_keep", content="留下的事", status="pending"),
            LifeTodo(id="todo_old", content="旧表述", status="pending"),
        ]
    )
    board = apply_todo_write(
        storage.load(),
        [{"id": "todo_old", "content": "新表述", "status": "in_progress"}],
        merge=True,
    )
    by_id = {item.id: item for item in board}
    assert by_id["todo_keep"].status == "pending"
    assert by_id["todo_old"].content == "新表述"
    assert by_id["todo_old"].status == "in_progress"


def test_write_replace_cancels_omitted_open_items(tmp_path: Path) -> None:
    current = [
        LifeTodo(id="todo_a", content="A", status="pending"),
        LifeTodo(id="todo_b", content="B", status="pending"),
        LifeTodo(id="todo_done", content="C", status="completed"),
    ]
    board = apply_todo_write(
        current,
        [{"id": "todo_a", "content": "A", "status": "in_progress"}],
        merge=False,
    )
    by_id = {item.id: item for item in board}
    assert by_id["todo_a"].status == "in_progress"
    assert by_id["todo_b"].status == "cancelled"
    assert by_id["todo_done"].status == "completed"


def test_at_most_one_in_progress() -> None:
    current = [LifeTodo(id="todo_a", content="A", status="in_progress")]
    with pytest.raises(ValueError, match="in_progress"):
        apply_todo_write(
            current,
            [{"id": "todo_b", "content": "B", "status": "in_progress"}],
            merge=True,
        )


def test_ranking_fields_are_rejected() -> None:
    with pytest.raises(ValueError, match="ranking"):
        apply_todo_write(
            [],
            [{"content": "x", "status": "pending", "priority": "urgent"}],
            merge=True,
        )


def test_board_sorts_by_status_then_id() -> None:
    board = apply_todo_write(
        [],
        [
            {"id": "todo_b", "content": "B", "status": "pending"},
            {"id": "todo_a", "content": "A", "status": "pending"},
            {"id": "todo_now", "content": "Now", "status": "in_progress"},
        ],
        merge=True,
    )
    assert [item.id for item in board] == ["todo_now", "todo_a", "todo_b"]
    text = format_todo_board(board)
    assert text is not None
    assert text.index("todo_now") < text.index("todo_a")
    assert "urgent" not in text
    assert "优先级" not in text


async def test_nucleus_todo_write_and_list_roundtrip(tmp_path: Path) -> None:
    tool = _tool(tmp_path)
    ok, payload = await tool.execute(
        action="write",
        todos=[
            {"id": "todo_one", "content": "先写合同", "status": "in_progress"},
            {"id": "todo_two", "content": "再补测试", "status": "pending"},
        ],
        merge=True,
    )
    assert ok is True
    assert isinstance(payload, dict)
    assert payload["open_count"] == 2
    assert payload["in_progress_id"] == "todo_one"

    ok, listed = await tool.execute(action="list")
    assert ok is True
    assert isinstance(listed, dict)
    assert [item["id"] for item in listed["todos"]] == ["todo_one", "todo_two"]
    assert "priority" not in listed["todos"][0]
    assert "next_action" not in listed["todos"][0]


async def test_list_is_capped_and_hides_closed_by_default(tmp_path: Path) -> None:
    storage = TodoStorage(tmp_path)
    storage.save(
        [
            LifeTodo(id=f"todo_{index:02d}", content=f"item {index}", status="pending")
            for index in range(30)
        ]
        + [LifeTodo(id="todo_done", content="done", status="completed")]
    )
    tool = _tool(tmp_path)
    ok, payload = await tool.execute(action="list", limit=100)
    assert ok is True
    assert isinstance(payload, dict)
    assert payload["returned"] == 30
    assert payload["open_count"] == 30
    assert payload["all_count"] == 31
    assert all(item["status"] == "pending" for item in payload["todos"])


async def test_get_returns_history(tmp_path: Path) -> None:
    tool = _tool(tmp_path)
    await tool.execute(
        action="write",
        todos=[{"id": "todo_x", "content": "一事", "status": "pending"}],
    )
    await tool.execute(
        action="write",
        todos=[{"id": "todo_x", "content": "一事", "status": "completed"}],
        merge=True,
    )
    ok, payload = await tool.execute(action="get", todo_id="todo_x")
    assert ok is True
    assert isinstance(payload, dict)
    history = payload["todo"]["history"]
    assert any(entry.get("kind") == "create" for entry in history)
    assert any(entry.get("to") == "completed" for entry in history)


def test_v2_migrates_title_without_priority(tmp_path: Path) -> None:
    (tmp_path / "todos.json").write_text(
        json.dumps(
            {
                "version": 2,
                "tasks": [
                    {
                        "id": "todo_old",
                        "title": "每周分享",
                        "next_action": "找一个话题",
                        "status": "blocked",
                        "priority": "urgent",
                        "schedule_record_id": "schedule_1",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    storage = TodoStorage(tmp_path)
    todos = storage.load()
    assert len(todos) == 1
    assert todos[0].status == "pending"
    assert "每周分享" in todos[0].content
    assert "找一个话题" in todos[0].content
    dumped = json.loads((tmp_path / "todos.json").read_text(encoding="utf-8"))
    assert dumped["version"] == 3
    assert "priority" not in dumped["tasks"][0]
    assert dumped["legacy_schedule_ids"] == ["schedule_1"]
    health = todo_health_snapshot(todos)
    assert "urgent" not in str(health)
    assert health["open_count"] == 1


def test_legacy_list_todos_are_archived_on_load(tmp_path: Path) -> None:
    legacy = [{"id": "old", "title": "旧愿望", "status": "enjoying"}]
    (tmp_path / "todos.json").write_text(
        json.dumps(legacy, ensure_ascii=False),
        encoding="utf-8",
    )
    storage = TodoStorage(tmp_path)
    assert storage.load() == []
    payload = json.loads((tmp_path / "todos.json").read_text(encoding="utf-8"))
    assert payload["version"] == 3
    assert payload["tasks"] == []
    archives = list(tmp_path.glob("todos_legacy_*.json"))
    assert len(archives) == 1
    assert json.loads(archives[0].read_text(encoding="utf-8")) == legacy


def test_health_snapshot_has_counts_without_content(tmp_path: Path) -> None:
    storage = TodoStorage(tmp_path)
    storage.save(
        [LifeTodo(id="todo_secret", content="私人正文", status="pending")]
    )
    dumped = str(todo_health_snapshot(storage.load(persist_migration=False)))
    assert "私人正文" not in dumped
    assert "todo_secret" not in dumped


def test_heartbeat_includes_todo_board_as_state_not_offer() -> None:
    section_ids = [section.section_id for section in DEFAULT_HEARTBEAT_SECTIONS]
    assert section_ids.index("todo_board") < section_ids.index("opportunity_page")
    assert section_ids.count("todo_board") == 1
