"""Bounded conversation-evidence and legacy-facade contracts."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest

from plugins.life_engine.tools.chat_history_tools import LifeEngineFetchChatHistoryTool
from plugins.life_engine.tools.conversation_evidence import (
    ConversationEvidenceError,
    LifeEngineConversationEvidenceTool,
    _EvidenceRow,
    _message_ref,
)
from plugins.life_engine.tools.platform_history_sync import (
    LifeEngineSyncPlatformHistoryTool,
)


def _plugin() -> Any:
    history = SimpleNamespace(
        enabled=True,
        tool_max_limit=100,
        max_scan_rows_per_stream=240,
        core_max_result_bytes=8192,
        chat_max_result_bytes=16384,
        voice_max_result_bytes=8192,
        livestream_max_result_bytes=8192,
        minecraft_max_result_bytes=8192,
    )
    return SimpleNamespace(config=SimpleNamespace(history_retrieval=history))


def _row(position: int, text: str, *, stream_id: str = "s1") -> _EvidenceRow:
    return _EvidenceRow(
        position=position,
        message_id=f"m{position}",
        stream_id=stream_id,
        person_id="qq:u1",
        occurred_at=1_700_000_000.0 + position,
        message_type="text",
        text=text,
        reply_to="",
        platform="qq",
    )


def _tool() -> LifeEngineConversationEvidenceTool:
    tool = LifeEngineConversationEvidenceTool(plugin=_plugin())  # type: ignore[arg-type]
    tool._runtime_task_name = "core"
    return tool


def _stub_scope(
    monkeypatch: pytest.MonkeyPatch, tool: LifeEngineConversationEvidenceTool
) -> None:
    async def _resolve(_requested: list[str] | None) -> tuple[str, ...]:
        return ("s1",)

    async def _frontier(_streams: tuple[str, ...]) -> int:
        return 300

    monkeypatch.setattr(tool, "_resolve_streams", _resolve)
    monkeypatch.setattr(tool, "_frontier", _frontier)


async def test_heartbeat_replay_result_is_bounded_and_deduplicated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The #577 shape must not reproduce payload/context copies or exceed 8 KiB."""

    tool = _tool()
    _stub_scope(monkeypatch, tool)
    source = [
        _row(300 - index, f"第 {index} 条消息：" + "爱莉" * 80) for index in range(31)
    ]

    async def _load(**_kwargs: Any) -> list[_EvidenceRow]:
        return source

    monkeypatch.setattr(tool, "_load_rows", _load)
    ok, rendered = await tool.execute(operation="page", stream_ids=["s1"], limit=30)

    assert ok is True
    assert len(rendered.encode("utf-8")) <= 8192
    payload = json.loads(rendered)
    assert payload["delivered_bytes"] == len(rendered.encode("utf-8"))
    positions = [item["position"] for item in payload["items"]]
    assert len(positions) == len(set(positions))
    assert payload["task"] == "core"
    assert len(positions) < 30
    assert payload["has_more"] is True
    assert payload["continuation"]
    assert payload["stats"]["omitted_count"] == 0
    for forbidden in (
        "content_full",
        "payload",
        "context_before",
        "context_after",
        "tool_events",
    ):
        assert forbidden not in rendered


async def test_search_returns_one_merged_unique_neighborhood(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool = _tool()
    _stub_scope(monkeypatch, tool)
    rows = [
        _row(10, "before"),
        _row(9, "needle one"),
        _row(8, "between"),
        _row(7, "needle two"),
        _row(6, "after"),
    ]

    async def _load(**_kwargs: Any) -> list[_EvidenceRow]:
        return rows

    monkeypatch.setattr(tool, "_load_rows", _load)
    ok, rendered = await tool.execute(
        operation="search",
        query="needle",
        stream_ids=["s1"],
        limit=2,
        context_radius=1,
    )

    assert ok is True
    payload = json.loads(rendered)
    assert payload["delivered_bytes"] == len(rendered.encode("utf-8"))
    assert [item["position"] for item in payload["items"]] == [10, 9, 8, 7, 6]


async def test_keyset_cursor_has_no_skip_or_duplicate_when_frontier_is_fixed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool = _tool()
    _stub_scope(monkeypatch, tool)

    async def _load(**kwargs: Any) -> list[_EvidenceRow]:
        before = kwargs["before"] or 11
        return [
            _row(position, "x")
            for position in range(before - 1, max(0, before - 4), -1)
        ]

    monkeypatch.setattr(tool, "_load_rows", _load)
    ok1, first = await tool.execute(operation="page", stream_ids=["s1"], limit=2)
    cursor = json.loads(first)["continuation"]
    ok2, second = await tool.execute(
        operation="page", stream_ids=["s1"], limit=2, cursor=cursor
    )

    assert ok1 is ok2 is True
    first_positions = {item["position"] for item in json.loads(first)["items"]}
    second_positions = {item["position"] for item in json.loads(second)["items"]}
    assert first_positions == {10, 9}
    assert second_positions == {8, 7}
    assert first_positions.isdisjoint(second_positions)


async def test_search_limit_continuation_does_not_skip_later_matches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool = _tool()
    _stub_scope(monkeypatch, tool)
    rows = [
        _row(10, "needle one"),
        _row(9, "context"),
        _row(8, "needle two"),
        _row(7, "context"),
        _row(6, "needle three"),
    ]

    async def _load(**kwargs: Any) -> list[_EvidenceRow]:
        before = kwargs["before"]
        return [row for row in rows if before is None or row.position < before]

    monkeypatch.setattr(tool, "_load_rows", _load)
    ok1, first = await tool.execute(
        operation="search",
        query="needle",
        stream_ids=["s1"],
        limit=1,
        context_radius=0,
    )
    first_payload = json.loads(first)
    ok2, second = await tool.execute(
        operation="search",
        query="needle",
        stream_ids=["s1"],
        limit=1,
        context_radius=0,
        cursor=first_payload["continuation"],
    )

    assert ok1 is ok2 is True
    assert [item["position"] for item in first_payload["items"]] == [10]
    assert [item["position"] for item in json.loads(second)["items"]] == [8]


async def test_invalid_cursor_fails_explicitly(monkeypatch: pytest.MonkeyPatch) -> None:
    tool = _tool()
    _stub_scope(monkeypatch, tool)
    ok, rendered = await tool.execute(
        operation="page", stream_ids=["s1"], cursor="tampered"
    )
    assert ok is False
    assert json.loads(rendered)["error"]["code"] == "cursor_invalid"


async def test_internal_call_never_falls_back_to_latest_stream() -> None:
    tool = _tool()
    with pytest.raises(
        ConversationEvidenceError, match="must provide stream_ids"
    ):
        await tool._resolve_streams(None)


async def test_heartbeat_placeholder_requires_explicit_streams() -> None:
    """心跳绑定 stream_id=chat_global 占位符时不得误当真实流读取。"""
    tool = _tool()
    tool._bind_runtime_context(stream_id="chat_global")
    with pytest.raises(
        ConversationEvidenceError, match="must provide stream_ids"
    ):
        await tool._resolve_streams(None)


async def test_target_key_resolves_to_full_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    """「你可以触达的人和地方」的 target_key（p-20403fdb）应解析为完整 stream_id。"""
    tool = _tool()

    resolved = {"p-20403fdb": "20403fdb0e6df94137c9071e62c44c09eb8090b534279ef5695c4b4aa5fae7bc"}

    async def _resolve_key(ref: str) -> str | None:
        return resolved.get(ref)

    monkeypatch.setattr(tool, "_resolve_target_key", _resolve_key)

    class _Result:
        def __init__(self, rows: list[str]) -> None:
            self._rows = rows

        def scalars(self) -> _Result:
            return self

        def all(self) -> list[str]:
            return self._rows

    class _Session:
        def __init__(self, rows: list[str]) -> None:
            self._rows = rows

        async def execute(self, _statement: Any) -> _Result:
            return _Result(self._rows)

    @asynccontextmanager
    async def _session() -> Any:
        yield _Session(["20403fdb0e6df94137c9071e62c44c09eb8090b534279ef5695c4b4aa5fae7bc"])

    registry = SimpleNamespace(
        get_for_stream=lambda _stream_id: SimpleNamespace(instance_id="chat_global")
    )
    monkeypatch.setattr(
        "plugins.life_engine.tools.conversation_evidence.get_db_session", _session
    )
    monkeypatch.setattr(
        "plugins.life_engine.tools.conversation_evidence.LifeEngineService.get_instance",
        lambda: SimpleNamespace(consciousness_registry=registry),
    )

    streams = await tool._resolve_streams(["p-20403fdb"])
    assert streams == (
        "20403fdb0e6df94137c9071e62c44c09eb8090b534279ef5695c4b4aa5fae7bc",
    )


async def test_target_key_unresolvable_keeps_original_and_fails_explicitly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """target_key 解析失败时保留原值；精确匹配不到即显式 stream_not_found，不伪造结果。"""
    tool = _tool()

    async def _resolve_key(_ref: str) -> str | None:
        return None  # 活跃列表与前缀兜底都解析不到

    monkeypatch.setattr(tool, "_resolve_target_key", _resolve_key)

    class _Result:
        def scalars(self) -> _Result:
            return self

        def all(self) -> list[str]:
            return []

    class _Session:
        async def execute(self, _statement: Any) -> _Result:
            return _Result()

    @asynccontextmanager
    async def _session() -> Any:
        yield _Session()

    registry = SimpleNamespace(
        get_for_stream=lambda _stream_id: SimpleNamespace(instance_id="chat_global")
    )
    monkeypatch.setattr(
        "plugins.life_engine.tools.conversation_evidence.get_db_session", _session
    )
    monkeypatch.setattr(
        "plugins.life_engine.tools.conversation_evidence.LifeEngineService.get_instance",
        lambda: SimpleNamespace(consciousness_registry=registry),
    )

    with pytest.raises(ConversationEvidenceError) as error:
        await tool._resolve_streams(["p-20403fdb"])
    assert error.value.code == "stream_not_found"


async def test_resolve_target_key_prefix_scan_prefers_real_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_resolve_target_key 前缀扫描：排除空 platform 占位，返回唯一真实流。"""
    tool = _tool()

    # send_targets 活跃列表解析失败
    async def _no_target(_key: str) -> Any:
        return None

    monkeypatch.setattr(
        "plugins.life_engine.core.send_targets.resolve_send_target_key", _no_target
    )

    class _Rows:
        def __init__(self, rows: list[tuple[str, str]]) -> None:
            self._rows = rows

        def all(self) -> list[tuple[str, str]]:
            return self._rows

    class _Session:
        def __init__(self, rows: list[tuple[str, str]]) -> None:
            self._rows = rows

        async def execute(self, _statement: Any) -> _Rows:
            return _Rows(self._rows)

    rows = [
        ("20403fdb", ""),
        ("20403fdb0e6df94137c9071e62c44c09eb8090b534279ef5695c4b4aa5fae7bc", "ayla"),
    ]

    @asynccontextmanager
    async def _session() -> Any:
        yield _Session(rows)

    monkeypatch.setattr(
        "plugins.life_engine.tools.conversation_evidence.get_db_session", _session
    )

    full = await tool._resolve_target_key("p-20403fdb")
    assert full == "20403fdb0e6df94137c9071e62c44c09eb8090b534279ef5695c4b4aa5fae7bc"


async def test_resolve_target_key_passthrough_for_full_stream_id() -> None:
    """完整 stream_id 不是 target_key 格式，应原样返回。"""
    tool = _tool()
    full = "20403fdb0e6df94137c9071e62c44c09eb8090b534279ef5695c4b4aa5fae7bc"
    assert await tool._resolve_target_key(full) == full
    assert await tool._resolve_target_key("") == ""


async def test_cross_instance_stream_read_is_denied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool = _tool()
    tool._bind_runtime_context(stream_id="s1")

    class _Result:
        def scalars(self) -> _Result:
            return self

        def all(self) -> list[str]:
            return ["s1", "s2"]

    class _Session:
        async def execute(self, _statement: Any) -> _Result:
            return _Result()

    @asynccontextmanager
    async def _session() -> Any:
        yield _Session()

    owners = {
        "s1": SimpleNamespace(instance_id="chat-one"),
        "s2": SimpleNamespace(instance_id="voice-two"),
    }
    service = SimpleNamespace(
        consciousness_registry=SimpleNamespace(
            get_for_stream=lambda stream_id: owners[stream_id]
        )
    )
    monkeypatch.setattr(
        "plugins.life_engine.tools.conversation_evidence.get_db_session", _session
    )
    monkeypatch.setattr(
        "plugins.life_engine.tools.conversation_evidence.LifeEngineService.get_instance",
        lambda: service,
    )

    with pytest.raises(ConversationEvidenceError) as error:
        await tool._resolve_streams(["s1", "s2"])
    assert error.value.code == "cross_instance_denied"


async def test_large_utf8_message_uses_verifiable_chunks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool = _tool()
    _stub_scope(monkeypatch, tool)
    row = _row(42, "爱莉希雅🌸" * 100_000)

    async def _load(**_kwargs: Any) -> list[_EvidenceRow]:
        return [row]

    monkeypatch.setattr(tool, "_load_rows", _load)
    ok, rendered = await tool.execute(
        operation="read",
        stream_ids=["s1"],
        message_ref=_message_ref(row),
    )

    assert ok is True
    assert len(rendered.encode("utf-8")) <= 8192
    payload = json.loads(rendered)
    item = payload["items"][0]
    assert item["exact"] is False
    assert item["content_sha256"]
    assert payload["continuation"]
    item["text"].encode("utf-8").decode("utf-8")


async def test_store_failure_is_not_reported_as_empty_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool = _tool()
    _stub_scope(monkeypatch, tool)

    async def _load(**_kwargs: Any) -> list[_EvidenceRow]:
        raise RuntimeError("database detail must not leak")

    monkeypatch.setattr(tool, "_load_rows", _load)
    ok, rendered = await tool.execute(operation="page", stream_ids=["s1"])
    assert ok is False
    assert json.loads(rendered) == {
        "error": {
            "code": "evidence_store_unavailable",
            "message": "conversation evidence store is unavailable",
        }
    }


async def test_legacy_backfill_is_explicitly_separated() -> None:
    legacy = LifeEngineFetchChatHistoryTool(plugin=_plugin())  # type: ignore[arg-type]
    ok, rendered = await legacy.execute(stream_ids=["s1"], source_mode="napcat")
    assert ok is False
    assert json.loads(rendered)["error"]["code"] == "platform_sync_separated"


async def test_platform_sync_is_explicit_content_free_and_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    existing: set[str] = set()
    stream = SimpleNamespace(
        stream_id="s1",
        person_id="qq:u1",
        platform="qq",
        chat_type="private",
        group_id=None,
    )

    class _ScalarResult:
        def __init__(self, value: Any) -> None:
            self.value = value

        def scalar_one_or_none(self) -> Any:
            return self.value

        def scalars(self) -> _ScalarResult:
            return self

        def all(self) -> list[str]:
            return list(self.value)

    class _Session:
        async def execute(self, statement: Any) -> _ScalarResult:
            sql = str(statement)
            if "FROM chat_streams" in sql:
                return _ScalarResult(stream)
            if "FROM person_info" in sql:
                return _ScalarResult("u1")
            if "FROM messages" in sql:
                return _ScalarResult(existing)
            raise AssertionError(sql)

        def add_all(self, rows: list[Any]) -> None:
            existing.update(row.message_id for row in rows)

    @asynccontextmanager
    async def _session() -> Any:
        yield _Session()

    async def _adapter(**_kwargs: Any) -> dict[str, Any]:
        return {
            "status": "ok",
            "data": {
                "messages": [
                    {
                        "message_id": "platform-m1",
                        "time": 1_700_000_000,
                        "sender": {"user_id": "u1"},
                        "message": [{"type": "text", "data": {"text": "private body"}}],
                    }
                ]
            },
        }

    monkeypatch.setattr(
        "plugins.life_engine.tools.platform_history_sync.get_db_session", _session
    )
    monkeypatch.setattr(
        "plugins.life_engine.tools.platform_history_sync.send_adapter_command", _adapter
    )
    tool = LifeEngineSyncPlatformHistoryTool(plugin=_plugin())  # type: ignore[arg-type]

    ok1, first = await tool.execute(stream_id="s1")
    ok2, second = await tool.execute(stream_id="s1")

    assert ok1 is ok2 is True
    assert isinstance(first, dict) and isinstance(second, dict)
    assert first["inserted_count"] == 1
    assert second["inserted_count"] == 0
    assert first["experience_semantics"] == "platform_cache_not_live_experience"
    assert "private body" not in json.dumps(first)
