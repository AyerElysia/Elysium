"""life event grep tool tests."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from plugins.life_engine.service.core import LifeEngineService
from plugins.life_engine.service.event_builder import EventType, LifeEngineEvent
from plugins.life_engine.tools.event_grep_tools import (
    LifeEngineGrepEventsTool,
    LifeEngineReadEventTool,
    grep_life_events,
)


def _event(
    sequence: int,
    content: str,
    *,
    stream_id: str = "s1",
    event_type: EventType = EventType.MESSAGE,
    tool_name: str | None = None,
) -> LifeEngineEvent:
    return LifeEngineEvent(
        event_id=f"evt-{sequence}",
        event_type=event_type,
        timestamp=f"2026-04-25T00:0{sequence}:00+08:00",
        sequence=sequence,
        source="qq" if stream_id else "life_engine",
        source_detail=f"stream={stream_id}",
        content=content,
        sender="user",
        chat_type="private",
        stream_id=stream_id,
        tool_name=tool_name,
    )


@pytest.fixture(autouse=True)
def _reset_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    import plugins.life_engine.service.registry as registry

    monkeypatch.setattr(registry, "_life_engine_registry", registry.ServiceRegistry())


async def test_grep_life_events_searches_history_and_pending() -> None:
    service = LifeEngineService(SimpleNamespace(config=None))
    service._event_history = [
        _event(1, "我们刚才聊了天气"),
        _event(2, "调用天气工具", event_type=EventType.TOOL_CALL, tool_name="weather"),
    ]
    service._pending_events = [_event(3, "pending 里提到咖啡")]

    import plugins.life_engine.service.registry as registry

    registry.register_life_engine_service(service)

    result = await grep_life_events(query="咖啡")

    assert result["stats"]["scanned_events"] == 3
    assert result["stats"]["matched_events"] == 1
    assert result["matches"][0]["event"]["event_id"] == "evt-3"


async def test_life_chatter_grep_defaults_to_current_stream() -> None:
    service = LifeEngineService(SimpleNamespace(config=None))
    service._event_history = [
        _event(1, "同一个关键词", stream_id="s1"),
        _event(2, "同一个关键词", stream_id="s2"),
    ]

    import plugins.life_engine.service.registry as registry

    registry.register_life_engine_service(service)

    tool = LifeEngineGrepEventsTool(plugin=SimpleNamespace())
    tool.chat_stream = SimpleNamespace(stream_id="s1")

    ok, payload = await tool.execute(query="关键词")

    assert ok is True
    assert isinstance(payload, dict)
    assert payload["stream_ids"] == ["s1"]
    assert payload["stats"]["matched_events"] == 1
    assert payload["matches"][0]["event"]["stream_id"] == "s1"


async def test_life_chatter_grep_includes_life_internal_events_by_default() -> None:
    service = LifeEngineService(SimpleNamespace(config=None))
    service._event_history = [
        _event(1, "同一个关键词", stream_id="s1"),
        _event(2, "同一个关键词", stream_id="s2"),
        _event(
            3,
            "life 内部也提到同一个关键词",
            stream_id="",
            event_type=EventType.HEARTBEAT,
        ),
    ]

    import plugins.life_engine.service.registry as registry

    registry.register_life_engine_service(service)

    tool = LifeEngineGrepEventsTool(plugin=SimpleNamespace())
    tool.chat_stream = SimpleNamespace(stream_id="s1")

    ok, payload = await tool.execute(query="关键词")

    assert ok is True
    assert isinstance(payload, dict)
    stream_ids = [match["event"]["stream_id"] for match in payload["matches"]]
    assert stream_ids == ["", "s1"]


async def test_life_chatter_grep_context_stays_in_current_stream() -> None:
    service = LifeEngineService(SimpleNamespace(config=None))
    service._event_history = [
        _event(1, "当前流前文", stream_id="s1"),
        _event(2, "其它流前文", stream_id="s2"),
        _event(3, "目标关键词", stream_id="s1"),
        _event(4, "其它流后文", stream_id="s2"),
        _event(5, "当前流后文", stream_id="s1"),
    ]

    import plugins.life_engine.service.registry as registry

    registry.register_life_engine_service(service)

    tool = LifeEngineGrepEventsTool(plugin=SimpleNamespace())
    tool.chat_stream = SimpleNamespace(stream_id="s1")

    ok, payload = await tool.execute(query="目标", context_before=2, context_after=2)

    assert ok is True
    assert isinstance(payload, dict)
    match = payload["matches"][0]
    assert [item["stream_id"] for item in match["context_before"]] == ["s1"]
    assert [item["stream_id"] for item in match["context_after"]] == ["s1"]


async def test_life_engine_grep_can_search_tool_name() -> None:
    service = LifeEngineService(SimpleNamespace(config=None))
    service._event_history = [
        _event(1, "调用工具", stream_id="", event_type=EventType.TOOL_CALL, tool_name="nucleus_web_search"),
    ]

    import plugins.life_engine.service.registry as registry

    registry.register_life_engine_service(service)

    tool = LifeEngineGrepEventsTool(plugin=SimpleNamespace())
    ok, payload = await tool.execute(query="web_search", fields=["tool_name"])

    assert ok is True
    assert isinstance(payload, dict)
    assert payload["stats"]["matched_events"] == 1
    assert payload["matches"][0]["event"]["tool_name"] == "nucleus_web_search"


async def test_proactive_opportunity_is_searchable_from_event_stream() -> None:
    service = LifeEngineService(SimpleNamespace(config=None))
    await service.enqueue_proactive_opportunity(
        "主动续话机会：对方已经沉默约 20 分钟",
        stream_id="s-proactive",
        platform="qq",
        chat_type="private",
    )

    import plugins.life_engine.service.registry as registry

    registry.register_life_engine_service(service)

    result = await grep_life_events(
        query="主动续话机会",
        fields=["content", "content_type", "source"],
    )

    assert result["stats"]["matched_events"] == 1
    event = result["matches"][0]["event"]
    assert event["stream_id"] == "s-proactive"
    assert event["source"] == "proactive_message_plugin"
    assert event["content_type"] == "proactive_opportunity"


async def test_chatter_inner_monologue_is_searchable_from_event_stream() -> None:
    service = LifeEngineService(SimpleNamespace(config=None))
    await service.record_chatter_inner_monologue(
        "还是会忍不住想着他是不是在忙。",
        stream_id="s-monologue",
        platform="qq",
        chat_type="private",
        sender_name="爱莉",
        mood="想念",
        intent="先等一会儿",
        topic="沉默等待",
    )

    import plugins.life_engine.service.registry as registry

    registry.register_life_engine_service(service)

    result = await grep_life_events(
        query="沉默等待",
        fields=["content", "content_type", "source"],
    )

    assert result["stats"]["matched_events"] == 1
    event = result["matches"][0]["event"]
    assert event["stream_id"] == "s-monologue"
    assert event["source"] == "life_chatter"
    assert event["content_type"] == "chatter_inner_monologue"


async def test_read_event_pages_complete_authoritative_activity_by_occurrence() -> None:
    content = "完整意识活动🌸" * 1800
    event = SimpleNamespace(
        occurrence_id="activity-read:chosen",
        event_id="activity-read:chosen",
        event_type="conscious_activity_tool_call",
        channel="tool",
        source="life_chatter",
        stream_id="stream-1",
        source_instance_id="chat-instance-1",
        causation_id="turn-1",
        correlation_id="turn-1",
        content_ref="life-event-occurrence:activity-read:chosen",
        content="展示摘要，不是权威原文",
        raw_content=content,
        sequence=17,
    )

    class _Store:
        async def get_by_event_id(self, identity: str) -> object | None:
            return event if identity == event.occurrence_id else None

    service = LifeEngineService(SimpleNamespace(config=None))
    service._get_life_event_store = lambda: _Store()  # type: ignore[method-assign]

    import plugins.life_engine.service.registry as registry

    registry.register_life_engine_service(service)
    tool = LifeEngineReadEventTool(plugin=SimpleNamespace())
    tool._runtime_task_name = "core"

    continuation = ""
    chunks: list[str] = []
    for _ in range(20):
        ok, payload = await tool.execute(
            occurrence_id="life-event-occurrence:activity-read:chosen",
            continuation=continuation,
            max_bytes=4096,
        )
        assert ok is True
        assert isinstance(payload, dict)
        assert payload["occurrence_id"] == "activity-read:chosen"
        assert payload["content_ref"] == (
            "life-event-occurrence:activity-read:chosen"
        )
        assert payload["delivered_bytes"] <= 4096
        chunks.append(payload["content"])
        continuation = str(payload["continuation"])
        if not continuation:
            break
    else:  # pragma: no cover - protects the bounded continuation contract
        raise AssertionError("authoritative event continuation did not terminate")

    assert "".join(chunks) == content


async def test_read_event_rejects_tampered_continuation_content_free() -> None:
    event = SimpleNamespace(
        occurrence_id="activity-tamper:chosen",
        event_id="activity-tamper:chosen",
        event_type="conscious_activity_tool_call",
        channel="tool",
        source="life_chatter",
        stream_id="stream-1",
        source_instance_id="chat-instance-1",
        causation_id="turn-1",
        correlation_id="turn-1",
        content_ref="life-event-occurrence:activity-tamper:chosen",
        content="不能泄露的完整意识活动" * 1200,
        sequence=18,
    )

    class _Store:
        async def get_by_event_id(self, identity: str) -> object | None:
            return event if identity == event.occurrence_id else None

    service = LifeEngineService(SimpleNamespace(config=None))
    service._get_life_event_store = lambda: _Store()  # type: ignore[method-assign]

    import plugins.life_engine.service.registry as registry

    registry.register_life_engine_service(service)
    tool = LifeEngineReadEventTool(plugin=SimpleNamespace())
    tool._runtime_task_name = "core"
    ok, first = await tool.execute(
        occurrence_id=event.occurrence_id,
        max_bytes=4096,
    )
    assert ok is True and isinstance(first, dict) and first["continuation"]

    continuation = str(first["continuation"])
    replacement = "A" if continuation[-1] != "A" else "B"
    ok, failure = await tool.execute(
        occurrence_id=event.occurrence_id,
        continuation=continuation[:-1] + replacement,
        max_bytes=4096,
    )

    assert ok is False
    assert failure == "读取 Life Event 失败: BoundedContinuationError"
    assert "不能泄露" not in failure
