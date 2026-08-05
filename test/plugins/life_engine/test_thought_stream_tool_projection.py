from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from plugins.life_engine.streams.manager import ThoughtStreamManager
from plugins.life_engine.streams.tools import LifeEngineManageThoughtStreamTool


def _make_tool(tmp_path, monkeypatch):
    manager = ThoughtStreamManager(workspace_path=str(tmp_path), max_active=20)
    service = SimpleNamespace(_thought_manager=manager)
    monkeypatch.setattr(
        "plugins.life_engine.streams.tools._get_service",
        lambda: service,
    )
    return LifeEngineManageThoughtStreamTool(SimpleNamespace()), manager


def _cursor(result: str) -> str:
    match = re.search(r"\bnext_cursor=([^\]]+)", result)
    assert match is not None
    return match.group(1)


def _stream_ids(result: str) -> set[str]:
    return set(re.findall(r"^- (ts_[0-9a-f]+):", result, flags=re.MULTILINE))


def test_include_dormant_never_mixes_completed(tmp_path, monkeypatch) -> None:
    tool, manager = _make_tool(tmp_path, monkeypatch)
    dormant = manager.create("dormant-thread")
    completed = manager.create("completed-thread")
    manager.retire(dormant.id, new_status="dormant")
    manager.retire(completed.id, new_status="completed")

    ok, result = asyncio.run(tool.execute(action="list", include_dormant=True))

    assert ok is True
    assert "dormant-thread" in result
    assert "[dormant]" in result
    assert "completed-thread" not in result
    assert "[completed]" not in result


def test_list_is_stably_paginated_and_bounded(tmp_path, monkeypatch) -> None:
    tool, manager = _make_tool(tmp_path, monkeypatch)
    for index in range(7):
        manager.create(f"thread-{index}")

    ok, first = asyncio.run(tool.execute(action="list", page_size=3, max_bytes=2048))
    first_cursor = _cursor(first)
    ok_next, second = asyncio.run(
        tool.execute(
            action="list",
            page_size=3,
            max_bytes=2048,
            cursor=first_cursor,
        )
    )
    second_cursor = _cursor(second)
    ok_last, last = asyncio.run(
        tool.execute(
            action="list",
            page_size=3,
            max_bytes=2048,
            cursor=second_cursor,
        )
    )

    assert ok is True
    assert ok_next is True
    assert ok_last is True
    assert len(first.encode("utf-8")) <= 2048
    assert len(second.encode("utf-8")) <= 2048
    assert len(last.encode("utf-8")) <= 2048
    assert "returned=3" in first
    assert "has_more=true" in first
    assert _stream_ids(first).isdisjoint(_stream_ids(second))
    assert _stream_ids(first) | _stream_ids(second) | _stream_ids(last) == {
        stream.id for stream in manager.list_for_projection()
    }
    assert "has_more=false" in last
    assert _cursor(last) == "-"


def test_list_cursor_rejects_filter_change_and_stale_snapshot(
    tmp_path, monkeypatch
) -> None:
    tool, manager = _make_tool(tmp_path, monkeypatch)
    for index in range(3):
        manager.create(f"thread-{index}")

    ok, first = asyncio.run(tool.execute(action="list", page_size=1))
    cursor = _cursor(first)
    filter_ok, filter_error = asyncio.run(
        tool.execute(action="list", include_dormant=True, cursor=cursor)
    )
    manager.create("snapshot-changed")
    stale_ok, stale_error = asyncio.run(tool.execute(action="list", cursor=cursor))

    assert ok is True
    assert filter_ok is False
    assert "filter does not match" in filter_error
    assert stale_ok is False
    assert "snapshot changed" in stale_error


def test_list_truncates_utf8_projection_without_mutating_authority(
    tmp_path, monkeypatch
) -> None:
    tool, manager = _make_tool(tmp_path, monkeypatch)
    title = "爱莉希雅" * 200
    thought = "持续关注这件事" * 50
    stream = manager.create(title)
    manager.advance(stream.id, thought)

    ok, result = asyncio.run(tool.execute(action="list", page_size=1, max_bytes=2048))

    assert ok is True
    assert len(result.encode("utf-8")) <= 2048
    assert "omitted_field_bytes=0" not in result
    assert "�" not in result
    assert stream.title == title
    assert stream.last_thought == thought


def test_list_projection_does_not_decay_or_write_legacy_snapshot(
    tmp_path, monkeypatch
) -> None:
    tool, manager = _make_tool(tmp_path, monkeypatch)
    stream = manager.create("pure-read")
    snapshot_before = manager._index_file.read_bytes()
    stream.curiosity_score = 0.9
    stream.last_decay_at = (datetime.now(UTC) - timedelta(days=30)).isoformat()

    ok, _ = asyncio.run(tool.execute(action="list"))

    assert ok is True
    assert stream.curiosity_score == 0.9
    assert manager._index_file.read_bytes() == snapshot_before


def test_list_rejects_malformed_cursor_and_invalid_budget(
    tmp_path, monkeypatch
) -> None:
    tool, manager = _make_tool(tmp_path, monkeypatch)
    manager.create("thread")

    cursor_ok, cursor_error = asyncio.run(
        tool.execute(action="list", cursor="not-a-cursor")
    )
    budget_ok, budget_error = asyncio.run(tool.execute(action="list", max_bytes=1024))

    assert cursor_ok is False
    assert "cursor is malformed" in cursor_error
    assert budget_ok is False
    assert "max_bytes must be between" in budget_error
