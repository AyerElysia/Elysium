"""life TODO tool tests."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from plugins.life_engine.core.config import LifeEngineConfig
from plugins.life_engine.tools.todo_tools import (
    LifeEngineListTodosTool,
    LifeEngineManageTodoTool,
    LifeTodo,
    TodoStorage,
)


def _make_plugin(tmp_path: Path) -> SimpleNamespace:
    config = LifeEngineConfig()
    config.settings.workspace_path = str(tmp_path)
    return SimpleNamespace(config=config)


def _make_list_tool(tmp_path: Path) -> LifeEngineListTodosTool:
    return LifeEngineListTodosTool(plugin=_make_plugin(tmp_path))  # type: ignore[arg-type]


def _make_manage_tool(tmp_path: Path) -> LifeEngineManageTodoTool:
    return LifeEngineManageTodoTool(plugin=_make_plugin(tmp_path))  # type: ignore[arg-type]


def _seed_todos(tmp_path: Path, count: int, *, completed: int = 0) -> None:
    todos = [
        LifeTodo(
            id=f"todo_{index:02d}",
            title=f"承诺行动 {index}",
            description=f"详细说明 {index}",
            priority="high" if index == 0 else "normal",
            next_action=f"下一步 {index}",
            next_review_at="2099-01-01T00:00:00+08:00",
            visibility="shared" if index == 0 else "private",
        )
        for index in range(count)
    ]
    todos.extend(
        LifeTodo(
            id=f"done_{index:02d}",
            title=f"已完成 {index}",
            status="completed",
            next_action="已完成",
        )
        for index in range(completed)
    )
    TodoStorage(tmp_path).save(todos)


@pytest.mark.asyncio
async def test_list_todos_defaults_to_compact_limited_summary(tmp_path: Path) -> None:
    _seed_todos(tmp_path, 12, completed=1)
    tool = _make_list_tool(tmp_path)

    ok, payload = await tool.execute()

    assert ok is True
    assert isinstance(payload, dict)
    assert payload["action"] == "list_todos"
    assert payload["total"] == 12
    assert payload["all_count"] == 13
    assert payload["active_count"] == 12
    assert payload["returned"] == 10
    assert payload["limit"] == 10
    assert payload["truncated"] is True
    assert payload["detail_level"] == "summary"

    first = payload["todos"][0]
    assert set(first) == {
        "id",
        "title",
        "status",
        "priority",
        "next_action",
        "due_at",
        "remind_at",
        "next_review_at",
        "days_left",
        "overdue",
        "needs_review",
        "recurrence",
        "visibility",
        "source",
        "schedule_record_id",
        "progress_count",
        "completion_count",
        "has_description",
    }
    assert first["next_action"]
    assert "description" not in first
    assert "created_at" not in first


@pytest.mark.asyncio
async def test_list_todos_full_detail_is_explicit_and_still_limited(tmp_path: Path) -> None:
    _seed_todos(tmp_path, 3)
    tool = _make_list_tool(tmp_path)

    ok, payload = await tool.execute(limit=2, detail_level="full")

    assert ok is True
    assert isinstance(payload, dict)
    assert payload["returned"] == 2
    assert payload["total"] == 3
    assert payload["truncated"] is True
    assert payload["detail_level"] == "full"
    assert "description" in payload["todos"][0]
    assert "progress_log" in payload["todos"][0]
    assert "created_at" in payload["todos"][0]


@pytest.mark.asyncio
async def test_list_todos_limit_has_hard_cap(tmp_path: Path) -> None:
    _seed_todos(tmp_path, 30)
    tool = _make_list_tool(tmp_path)

    ok, payload = await tool.execute(limit=100)

    assert ok is True
    assert isinstance(payload, dict)
    assert payload["limit"] == 25
    assert payload["returned"] == 25
    assert payload["truncated"] is True


@pytest.mark.asyncio
async def test_create_requires_next_action_and_review_anchor(tmp_path: Path) -> None:
    tool = _make_manage_tool(tmp_path)

    ok, message = await tool.execute(action="create", title="每周搜索话题")
    assert ok is False
    assert "next_action" in str(message)

    ok, message = await tool.execute(
        action="create",
        title="每周搜索话题",
        next_action="搜索一个走向现实相关话题并整理成分享",
    )
    assert ok is False
    assert "至少一项" in str(message)


@pytest.mark.asyncio
async def test_create_shared_recurring_todo_returns_disclosure_flag(tmp_path: Path) -> None:
    tool = _make_manage_tool(tmp_path)

    ok, payload = await tool.execute(
        action="create",
        title="每周搜索走向现实的话题",
        next_action="搜索一个具体话题并整理成可分享内容",
        recurrence="weekly",
        visibility="shared",
        source="life_chatter",
    )

    assert ok is True
    assert isinstance(payload, dict)
    assert payload["requires_user_disclosure"] is True
    todo = payload["todo"]
    assert todo["status"] == "pending"
    assert todo["visibility"] == "shared"
    assert todo["recurrence"] == {"kind": "weekly"}
    assert todo["remind_at"]


@pytest.mark.asyncio
async def test_complete_recurring_todo_records_once_and_schedules_next(tmp_path: Path) -> None:
    storage = TodoStorage(tmp_path)
    storage.save([
        LifeTodo(
            id="todo_weekly",
            title="每周分享",
            next_action="搜索一个话题",
            recurrence={"kind": "weekly"},
            remind_at="2099-01-01T00:00:00+08:00",
        )
    ])
    tool = _make_manage_tool(tmp_path)

    ok, payload = await tool.execute(
        action="complete",
        todo_id="todo_weekly",
        note="这周已经分享过一次",
        next_action="下次继续找一个走向现实的话题",
    )

    assert ok is True
    assert isinstance(payload, dict)
    todo = payload["todo"]
    assert todo["status"] == "pending"
    assert len(todo["completion_log"]) == 1
    assert todo["next_action"] == "下次继续找一个走向现实的话题"
    assert todo["remind_at"]


@pytest.mark.asyncio
async def test_reminder_schedule_is_created_and_replaced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, object]] = []

    class FakeScheduleTool:
        def __init__(self, plugin: object) -> None:
            self.plugin = plugin

        async def execute(self, **kwargs: object) -> tuple[bool, dict[str, object]]:
            calls.append(kwargs)
            if kwargs.get("action") == "delete":
                return True, {"deleted": True}
            return True, {"record_id": f"schedule_{len(calls)}"}

    import plugins.life_engine.tools.schedule_tools as schedule_tools

    monkeypatch.setattr(schedule_tools, "LifeEngineManageScheduleTool", FakeScheduleTool)
    tool = _make_manage_tool(tmp_path)

    ok, payload = await tool.execute(
        action="create",
        title="提醒测试",
        next_action="做一件具体事",
        remind_at="2099-01-01T00:00:00+08:00",
    )

    assert ok is True
    assert isinstance(payload, dict)
    todo_id = payload["todo"]["id"]
    assert payload["todo"]["schedule_record_id"] == "schedule_1"

    ok, payload = await tool.execute(
        action="update",
        todo_id=todo_id,
        remind_at="2099-01-02T00:00:00+08:00",
    )

    assert ok is True
    assert isinstance(payload, dict)
    assert payload["todo"]["schedule_record_id"] == "schedule_3"
    assert [call["action"] for call in calls] == ["create", "delete", "create"]


def test_legacy_list_todos_are_archived_on_load(tmp_path: Path) -> None:
    legacy = [{"id": "old", "title": "旧愿望", "status": "enjoying"}]
    (tmp_path / "todos.json").write_text(
        json.dumps(legacy, ensure_ascii=False),
        encoding="utf-8",
    )

    storage = TodoStorage(tmp_path)

    assert storage.load() == []
    payload = json.loads((tmp_path / "todos.json").read_text(encoding="utf-8"))
    assert payload["version"] == 2
    assert payload["tasks"] == []
    archives = list(tmp_path.glob("todos_legacy_*.json"))
    assert len(archives) == 1
    assert json.loads(archives[0].read_text(encoding="utf-8")) == legacy
