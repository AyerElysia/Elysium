"""Inner dialogue sink/return and originating-stream wake."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from plugins.life_engine.core.chat_history import is_visible_chat_history_message
from plugins.life_engine.core.chatter import LifeChatter
from plugins.life_engine.core.compat_tools import LifeInnerDialogueTool
from plugins.life_engine.core.config import LifeEngineConfig
from plugins.life_engine.inner_dialogue.protocol import (
    INNER_DIALOGUE_KIND,
    InnerDialogueOpenLimitExceeded,
    InnerDialogueReturnBlocked,
    inner_dialogue_summary,
)
from plugins.life_engine.proactive.tools import (
    LifeEngineProactiveCommandTool,
    LifeEngineProactiveQueryTool,
)
from plugins.life_engine.service import LifeEngineService
from src.core.config.core_config import CoreConfig
from src.core.models.message import Message
from src.core.models.stream import StreamContext
from src.kernel.llm import ToolRegistry


class _DummyPlugin:
    def __init__(self, config: LifeEngineConfig) -> None:
        self.config = config
        self.global_storage_config = CoreConfig(
            storage=CoreConfig.StorageSection(backend="local")
        )


def _make_service(tmp_path: Path) -> LifeEngineService:
    config = LifeEngineConfig()
    config.settings.enabled = True
    config.settings.workspace_path = str(tmp_path)
    return LifeEngineService(_DummyPlugin(config))


def _bind_heartbeat(tool: object, *, tool_call_id: str) -> None:
    tool._runtime_task_name = "core"  # type: ignore[attr-defined]
    tool._bind_runtime_context(  # type: ignore[attr-defined]
        stream_id="chat_global",
        tool_call_id=tool_call_id,
    )
    tool._life_source_occurrence_id = "heartbeat:run:inner"  # type: ignore[attr-defined]
    tool._life_source_occurred_at = "2026-09-03T00:00:00+00:00"  # type: ignore[attr-defined]
    tool._life_source_instance_id = "chat_global"  # type: ignore[attr-defined]


def _patch_expression_wake(monkeypatch: pytest.MonkeyPatch, stream_id: str):
    context = StreamContext(stream_id=stream_id)
    chat_stream = SimpleNamespace(
        stream_id=stream_id,
        platform="qq",
        context=context,
    )

    class _Streams:
        async def get_or_create_stream(self, *, stream_id: str):
            assert stream_id == chat_stream.stream_id
            return chat_stream

    loop_manager = SimpleNamespace(
        _wait_states={stream_id: object()},
        start_stream_loop=AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        "src.core.managers.get_stream_manager",
        lambda: _Streams(),
    )
    monkeypatch.setattr(
        "src.core.transport.distribution.stream_loop_manager.get_stream_loop_manager",
        lambda: loop_manager,
    )
    return context, loop_manager


@pytest.mark.asyncio
async def test_inner_dialogue_sink_stores_structured_payload(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    receipt = await service.enqueue_inner_dialogue(
        "其实我有点犹豫。",
        mode="reflect",
        expect_surface=True,
        stream_id="stream-origin",
        platform="qq",
        chat_type="private",
        sender_name="主意识",
        source_instance_id="chat_global",
    )

    assert receipt["queued"] is True
    assert receipt["expect_surface"] is True
    assert receipt["stream_id"] == "stream-origin"
    event = service._pending_events[0]
    assert event.content_type == INNER_DIALOGUE_KIND
    assert event.occurrence_id == receipt["receipt_id"]
    payload = json.loads(str(event.raw_content))
    assert payload["receipt_id"] == receipt["receipt_id"]
    assert payload["expect_surface"] is True
    assert payload["stream_id"] == "stream-origin"
    assert payload["thought"] == "其实我有点犹豫。"
    assert payload["source_instance_id"] == "chat_global"


@pytest.mark.asyncio
async def test_expect_surface_false_is_not_returnable(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    receipt = await service.enqueue_inner_dialogue(
        "只记一下，不必浮回。",
        expect_surface=False,
        stream_id="stream-origin",
    )
    assert await service.list_inner_dialogue_records() == ()
    with pytest.raises(InnerDialogueReturnBlocked, match="expect_surface_false"):
        await service.return_inner_dialogue(
            receipt_id=receipt["receipt_id"],
            statement="不该交还。",
            occurrence_id="inner:return:blocked",
            actor_consciousness_instance_id="chat_global",
        )


@pytest.mark.asyncio
async def test_missing_stream_return_fails_closed(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    receipt = await service.enqueue_inner_dialogue(
        "窗口身份丢了。",
        expect_surface=True,
        stream_id="",
    )
    opened = await service.list_inner_dialogue_records()
    assert len(opened) == 1
    assert opened[0].receipt_id == receipt["receipt_id"]
    summaries = [
        item.receipt_id for item in opened
    ]
    assert inner_dialogue_summary(opened[0])["return_blocked"] == "missing_stream"
    assert summaries == [receipt["receipt_id"]]
    with pytest.raises(InnerDialogueReturnBlocked, match="missing_stream"):
        await service.return_inner_dialogue(
            receipt_id=receipt["receipt_id"],
            statement="交不回去。",
            occurrence_id="inner:return:missing",
            actor_consciousness_instance_id="chat_global",
        )


@pytest.mark.asyncio
async def test_open_receipt_survives_pending_drain(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    receipt = await service.enqueue_inner_dialogue(
        "心跳游标推进后还应该看见。",
        stream_id="stream-origin",
    )
    drained = await service.drain_pending_events()
    assert drained
    opened = await service.list_inner_dialogue_records()
    assert len(opened) == 1
    assert opened[0].receipt_id == receipt["receipt_id"]
    assert opened[0].status == "open"


@pytest.mark.asyncio
async def test_open_limit_rejects_new_expect_surface_sink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "plugins.life_engine.inner_dialogue.protocol.INNER_DIALOGUE_OPEN_LIMIT",
        1,
    )
    service = _make_service(tmp_path)
    await service.enqueue_inner_dialogue("第一条。", stream_id="stream-a")
    with pytest.raises(InnerDialogueOpenLimitExceeded):
        await service.enqueue_inner_dialogue("第二条。", stream_id="stream-b")


@pytest.mark.asyncio
async def test_inner_return_wakes_originating_stream_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _make_service(tmp_path)
    context, loop_manager = _patch_expression_wake(monkeypatch, "stream-origin")
    other = StreamContext(stream_id="stream-other")
    receipt = await service.enqueue_inner_dialogue(
        "我想把这句话交还这个窗口。",
        stream_id="stream-origin",
        platform="qq",
    )
    result = await service.return_inner_dialogue(
        receipt_id=receipt["receipt_id"],
        statement="我想过了，还是想轻轻说一声。",
        occurrence_id="inner:return:origin",
        actor_consciousness_instance_id="chat_global",
    )

    assert result["authority_committed"] is True
    assert result["record_family"] == "inner_dialogue"
    assert result["message_sent"] is False
    assert result["expression_wake_enqueued"] is True
    assert result["stream_id"] == "stream-origin"
    assert len(context.unread_messages) == 1
    message = context.unread_messages[0]
    assert message.message_id.startswith("inner_return_")
    assert message.extra["is_inner_return_trigger"] is True
    assert message.extra["bypass_message_buffer"] is True
    assert message.sender_id == "life_engine_inner_return"
    assert is_visible_chat_history_message(message) is False
    assert "不是用户" in message.processed_plain_text
    assert not message.extra.get("is_initiative_outreach_trigger")
    assert other.unread_messages == []
    loop_manager.start_stream_loop.assert_awaited_with("stream-origin")
    types = [event.content_type for event in service._pending_events]
    assert "inner_dialogue_return" in types
    assert "inner_dialogue_return_delivery" in types

    replay = await service.return_inner_dialogue(
        receipt_id=receipt["receipt_id"],
        statement="我想过了，还是想轻轻说一声。",
        occurrence_id="inner:return:origin",
        actor_consciousness_instance_id="chat_global",
    )
    assert replay["idempotent_replay"] is True
    assert len(context.unread_messages) == 1


@pytest.mark.asyncio
async def test_inner_return_does_not_force_reply_or_outreach_claim() -> None:
    chatter = LifeChatter.__new__(LifeChatter)
    trigger = SimpleNamespace(
        is_inner_return_trigger=True,
        is_initiative_outreach_trigger=False,
        is_proactive_opportunity_trigger=False,
        sender_role="other",
        extra={"is_inner_return_trigger": True},
    )
    decision = await chatter._should_respond(
        "inner return",
        [trigger],  # type: ignore[list-item]
        SimpleNamespace(),  # type: ignore[arg-type]
    )
    assert decision["should_respond"] is True
    assert decision["force_reply"] is False
    assert LifeChatter._initiative_outreach_occurrence_scope(  # type: ignore[arg-type]
        [trigger]
    ) == []
    assert LifeChatter._is_proactive_trigger_message(trigger) is True  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_proactive_tools_query_and_heartbeat_return(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _make_service(tmp_path)
    _patch_expression_wake(monkeypatch, "stream-origin")
    monkeypatch.setattr(
        "plugins.life_engine.service.registry.get_life_engine_service",
        lambda: service,
    )
    sink = await service.enqueue_inner_dialogue(
        "请心跳把回声交还给我。",
        stream_id="stream-origin",
    )
    query = LifeEngineProactiveQueryTool(SimpleNamespace())
    _bind_heartbeat(query, tool_call_id="call:inner-query")
    listed_ok, listed = await query.execute(resource="inner_dialogue")
    assert listed_ok is True
    assert isinstance(listed, dict)
    assert listed["resource"] == "inner_dialogue"
    assert sink["receipt_id"] in str(listed.get("receipts") or listed)

    command = LifeEngineProactiveCommandTool(SimpleNamespace())
    _bind_heartbeat(command, tool_call_id="call:inner-return")
    ok, payload = await command.execute(
        action="inner.return",
        record_id=sink["receipt_id"],
        statement="我想过了，还是想轻轻说一声。",
    )
    assert ok is True
    assert isinstance(payload, dict)
    assert payload["record_family"] == "inner_dialogue"
    assert payload["expression_wake_enqueued"] is True

    chatter_command = LifeEngineProactiveCommandTool(SimpleNamespace())
    chatter_command._bind_runtime_context(
        stream_id="chat_global",
        message=Message(
            message_id="message:inner:1",
            time=1785960000.0,
            stream_id="chat_global",
        ),
        tool_call_id="call:inner-from-chat",
    )
    chatter_command._life_source_occurrence_id = "life:event:inner:1"
    chatter_command._life_source_occurred_at = "2026-09-03T00:00:00+00:00"
    chatter_command._life_source_instance_id = "chat_global"
    chatter_command._runtime_task_name = "life_chatter"
    denied_ok, denied = await chatter_command.execute(
        action="inner.return",
        record_id=sink["receipt_id"],
        statement="表达层不能自己把还没想完的对话浮回。",
    )
    assert denied_ok is False
    assert isinstance(denied, dict)
    assert denied["error"] == "InnerDialogueReturnRequiresHeartbeat"


@pytest.mark.asyncio
async def test_heartbeat_inner_return_tool_prefix_executes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _make_service(tmp_path)
    _patch_expression_wake(monkeypatch, "stream-origin")
    monkeypatch.setattr(
        "plugins.life_engine.service.registry.get_life_engine_service",
        lambda: service,
    )
    sink = await service.enqueue_inner_dialogue(
        "用带前缀的工具名交还。",
        stream_id="stream-origin",
    )
    registry = ToolRegistry()
    registry.register(LifeEngineProactiveCommandTool)
    heartbeat = LifeEngineService.__new__(LifeEngineService)
    heartbeat.plugin = SimpleNamespace()
    result, success = await heartbeat._run_heartbeat_tool_call_execution(
        "tool-nucleus_proactive_command",
        {
            "action": "inner.return",
            "record_id": sink["receipt_id"],
            "statement": "前缀也能交还。",
        },
        registry,
        tool_call_id="call:prefix-inner-return",
        source_occurrence_id="heartbeat:run:prefix",
        source_occurred_at="2026-09-03T00:00:00+00:00",
    )
    assert success is True
    assert isinstance(result, dict)
    assert result["expression_wake_enqueued"] is True


@pytest.mark.asyncio
async def test_compat_inner_dialogue_tool_no_longer_promises_auto_float(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _make_service(tmp_path)
    fake_plugin = SimpleNamespace(service=service)
    monkeypatch.setattr(
        "plugins.life_engine.core.compat_tools.get_plugin_manager",
        lambda: SimpleNamespace(
            get_plugin=lambda name: fake_plugin if name == "life_engine" else None
        ),
    )
    tool = LifeInnerDialogueTool.__new__(LifeInnerDialogueTool)
    tool.chat_stream = SimpleNamespace(
        stream_id="stream-origin",
        platform="qq",
        chat_type="private",
        bot_nickname="爱莉",
    )
    ok, text = await tool.execute(thought="先沉下去。")
    assert ok is True
    assert "inner.return" in text
    assert "会自己浮上来" not in text
