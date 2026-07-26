"""fetch_chat_history 工具测试。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from plugins.life_engine.core.config import LifeEngineConfig
from plugins.life_engine.tools.chat_history_tools import LifeEngineFetchChatHistoryTool


def _make_tool() -> LifeEngineFetchChatHistoryTool:
    plugin = SimpleNamespace(config=LifeEngineConfig())
    return LifeEngineFetchChatHistoryTool(plugin=plugin)  # type: ignore[arg-type]


async def test_fetch_chat_history_auto_merge_dedup(monkeypatch: pytest.MonkeyPatch) -> None:
    """auto 模式下应合并本地与回补，并做去重。"""
    tool = _make_tool()

    async def _fake_candidates(**_kwargs: Any) -> list[dict[str, Any]]:
        return [
            {
                "stream_id": "s1",
                "platform": "qq",
                "chat_type": "private",
                "group_id": "",
                "group_name": "",
            }
        ]

    async def _fake_local(**_kwargs: Any) -> tuple[list[dict[str, Any]], dict[str, list[Any]]]:
        return (
            [
                {
                    "source": "local_db",
                    "platform": "qq",
                    "chat_type": "private",
                    "stream_id": "s1",
                    "message_id": "m1",
                    "time_ts": 100.0,
                    "time": "2026-04-18T00:00:00+08:00",
                    "sender_id": "u1",
                    "sender_name": "u1",
                    "sender_role": "member",
                    "content": "hello",
                    "content_full": "hello",
                    "payload": {},
                    "context_before": [],
                    "context_after": [],
                }
            ],
            {"s1": []},
        )

    async def _fake_backfill(**_kwargs: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        return (
            [
                # 与本地重复（同平台+stream+message_id）
                {
                    "source": "adapter_backfill",
                    "platform": "qq",
                    "chat_type": "private",
                    "stream_id": "s1",
                    "message_id": "m1",
                    "time_ts": 110.0,
                    "time": "2026-04-18T00:01:00+08:00",
                    "sender_id": "u1",
                    "sender_name": "u1",
                    "sender_role": "member",
                    "content": "hello duplicate",
                    "content_full": "hello duplicate",
                    "payload": {},
                    "context_before": [],
                    "context_after": [],
                },
                {
                    "source": "adapter_backfill",
                    "platform": "qq",
                    "chat_type": "private",
                    "stream_id": "s1",
                    "message_id": "m2",
                    "time_ts": 120.0,
                    "time": "2026-04-18T00:02:00+08:00",
                    "sender_id": "u1",
                    "sender_name": "u1",
                    "sender_role": "member",
                    "content": "world",
                    "content_full": "world",
                    "payload": {},
                    "context_before": [],
                    "context_after": [],
                },
            ],
            [{"status": "ok"}],
        )

    async def _fake_tool_events(**_kwargs: Any) -> list[dict[str, Any]]:
        return [{"event_type": "tool_call"}]

    monkeypatch.setattr(tool, "_resolve_stream_candidates", _fake_candidates)
    monkeypatch.setattr(tool, "_collect_local_matches", _fake_local)
    monkeypatch.setattr(tool, "_collect_backfill_matches", _fake_backfill)
    monkeypatch.setattr(tool, "_collect_tool_events", _fake_tool_events)

    ok, payload = await tool.execute(
        query="",
        source_mode="auto",
        force_backfill=True,
        limit=5,
        include_tool_calls=True,
    )

    assert ok is True
    assert isinstance(payload, dict)
    matches = payload["matches"]
    assert len(matches) == 2
    assert {item["message_id"] for item in matches} == {"m1", "m2"}
    assert payload["stats"]["backfill_attempted"] is True
    assert len(payload["tool_events"]) == 1


async def test_fetch_chat_history_auto_does_not_backfill_without_force(monkeypatch: pytest.MonkeyPatch) -> None:
    """auto 模式默认不应偷偷触发 NapCat 回补，保持当前流检索轻量。"""
    tool = _make_tool()

    async def _fake_candidates(**kwargs: Any) -> list[dict[str, Any]]:
        assert kwargs["cross_stream"] is False
        return [{"stream_id": "s1", "platform": "qq", "chat_type": "private", "group_id": "", "group_name": ""}]

    async def _fake_local(**_kwargs: Any) -> tuple[list[dict[str, Any]], dict[str, list[Any]]]:
        return ([], {"s1": []})

    async def _should_not_call_backfill(**_kwargs: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        raise AssertionError("auto 模式未 force_backfill 时不应调用 backfill")

    monkeypatch.setattr(tool, "_resolve_stream_candidates", _fake_candidates)
    monkeypatch.setattr(tool, "_collect_local_matches", _fake_local)
    monkeypatch.setattr(tool, "_collect_backfill_matches", _should_not_call_backfill)

    ok, payload = await tool.execute(query="", source_mode="auto", limit=5)

    assert ok is True
    assert isinstance(payload, dict)
    assert payload["scope"] == "current_stream"
    assert payload["stats"]["backfill_attempted"] is False


async def test_fetch_chat_history_without_chat_stream_uses_latest_external_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """life_engine heartbeat 没有当前 chat_stream 时，应默认落到最近外部流。"""
    tool = _make_tool()

    monkeypatch.setattr(
        "plugins.life_engine.tools.chat_history_tools._pick_latest_target_stream_id",
        lambda _plugin: "latest-stream",
    )

    class _FakeQuery:
        def __init__(self, _model: object) -> None:
            self.filters: dict[str, Any] = {}

        def filter(self, **kwargs: Any) -> "_FakeQuery":
            self.filters.update(kwargs)
            return self

        async def first(self) -> Any:
            assert self.filters["stream_id"] == "latest-stream"
            return SimpleNamespace(
                stream_id="latest-stream",
                platform="qq",
                chat_type="private",
                group_id="",
                group_name="",
            )

    monkeypatch.setattr("plugins.life_engine.tools.chat_history_tools.QueryBuilder", _FakeQuery)

    candidates = await tool._resolve_stream_candidates(
        stream_ids=[],
        cross_stream=False,
        platform="",
    )

    assert candidates == [
        {
            "stream_id": "latest-stream",
            "platform": "qq",
            "chat_type": "private",
            "group_id": "",
            "group_name": "",
        }
    ]


async def test_fetch_chat_history_local_db_no_backfill(monkeypatch: pytest.MonkeyPatch) -> None:
    """local_db 模式下不应触发回补。"""
    tool = _make_tool()

    async def _fake_candidates(**_kwargs: Any) -> list[dict[str, Any]]:
        return [{"stream_id": "s1", "platform": "qq", "chat_type": "private", "group_id": "", "group_name": ""}]

    async def _fake_local(**_kwargs: Any) -> tuple[list[dict[str, Any]], dict[str, list[Any]]]:
        return ([], {"s1": []})

    async def _should_not_call_backfill(**_kwargs: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        raise AssertionError("local_db 模式不应调用 backfill")

    monkeypatch.setattr(tool, "_resolve_stream_candidates", _fake_candidates)
    monkeypatch.setattr(tool, "_collect_local_matches", _fake_local)
    monkeypatch.setattr(tool, "_collect_backfill_matches", _should_not_call_backfill)

    ok, payload = await tool.execute(query="x", source_mode="local_db", limit=10)
    assert ok is True
    assert isinstance(payload, dict)
    assert payload["stats"]["backfill_attempted"] is False


async def test_fetch_chat_history_invalid_regex_returns_error() -> None:
    """非法正则应返回错误。"""
    tool = _make_tool()
    ok, payload = await tool.execute(query="(abc", use_regex=True)
    assert ok is False
    assert "正则表达式错误" in str(payload)
