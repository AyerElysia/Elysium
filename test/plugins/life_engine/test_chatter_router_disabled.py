"""Router opt-out preserves the normal subject expression workflow."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import plugins.life_engine.core.router as router_module
from plugins.life_engine.core.chatter import LifeChatter, _Phase, _WorkflowRuntime
from plugins.life_engine.core.config import LifeEngineConfig
from plugins.life_engine.service.core import LifeEngineService
from src.core.components.base.chatter import Wait
from src.core.models.message import Message, MessageType
from src.kernel.llm import ROLE, LLMPayload, Text


def _chatter(config: object | None) -> LifeChatter:
    chatter = LifeChatter.__new__(LifeChatter)
    chatter.plugin = SimpleNamespace(config=config)
    chatter.stream_id = "synthetic-stream"
    return chatter


def _disabled_config() -> LifeEngineConfig:
    config = LifeEngineConfig()
    config.chatter.router_enabled = False
    return config


def _message(content: str = "Synthetic engineering question", **fields) -> Message:
    return Message(
        message_id="synthetic-message",
        stream_id="synthetic-stream",
        content=content,
        processed_plain_text=content,
        sender_role="other",
        message_type=MessageType.TEXT,
        **fields,
    )


def _stream(chat_type: str = "group") -> SimpleNamespace:
    return SimpleNamespace(
        stream_id="synthetic-stream",
        stream_name="Synthetic stream",
        platform="test",
        chat_type=chat_type,
        context=SimpleNamespace(history_messages=[]),
    )


@pytest.mark.parametrize("chat_type", ["group", "private"])
@pytest.mark.parametrize(
    "content",
    [
        "00:03:18 synthetic engineering log: build completed",
        "Synthetic casual conversation without a mention",
        "Synthetic expression of sadness",
    ],
)
async def test_disabled_router_has_no_content_filter_or_router_activity(
    monkeypatch, chat_type, content
) -> None:
    chatter = _chatter(_disabled_config())
    service = SimpleNamespace(record_conscious_model_turn=AsyncMock())
    service_lookup = Mock(return_value=service)
    history = AsyncMock(side_effect=AssertionError("Router history must not run"))
    projection = AsyncMock(side_effect=AssertionError("Router projection must not run"))
    route = AsyncMock(side_effect=AssertionError("Router model must not run"))
    create_request = Mock(side_effect=AssertionError("No front model request"))
    monkeypatch.setattr(chatter, "_get_life_service", service_lookup)
    monkeypatch.setattr(chatter, "_build_history_text_async", history)
    monkeypatch.setattr(chatter, "_build_chat_router_prefix_prompt", projection)
    monkeypatch.setattr(chatter, "create_request", create_request)
    monkeypatch.setattr(router_module, "route_should_respond", route)
    unread = _message(content)

    decision = await chatter._should_respond(content, [unread], _stream(chat_type))

    assert decision["should_respond"] is True
    assert decision["force_reply"] is False
    assert "Router 已停用" in decision["reason"]
    assert chatter._should_force_reply_for_decision(decision, [unread]) is False
    service_lookup.assert_not_called()
    history.assert_not_awaited()
    projection.assert_not_awaited()
    route.assert_not_awaited()
    create_request.assert_not_called()
    service.record_conscious_model_turn.assert_not_awaited()


async def test_disabled_router_keeps_retired_followup_rejected() -> None:
    chatter = _chatter(_disabled_config())
    unread = _message(is_proactive_followup_trigger=True)

    decision = await chatter._should_respond("synthetic retired trigger", [unread], _stream())

    assert decision == {
        "reason": "旧延迟续话触发已退役，不进入表达决策",
        "should_respond": False,
        "force_reply": False,
    }


@pytest.mark.parametrize(
    "flag",
    [
        "is_inner_return_trigger",
        "is_initiative_outreach_trigger",
        "is_proactive_opportunity_trigger",
    ],
)
async def test_disabled_router_keeps_existing_internal_trigger_contract(flag) -> None:
    unread = _message(**{flag: True})
    chatter = _chatter(_disabled_config())

    decision = await chatter._should_respond("synthetic trigger", [unread], _stream())

    assert decision["should_respond"] is True
    assert decision["force_reply"] is False
    assert "Router 已停用" not in decision["reason"]


async def test_disabled_router_keeps_account_mention_fact_branch() -> None:
    unread = _message(
        extra={"at_users": [{"user_id": "synthetic-bot"}]},
        raw_data={"self_id": "synthetic-bot"},
    )
    chatter = _chatter(_disabled_config())

    decision = await chatter._should_respond("synthetic account mention", [unread], _stream())

    assert decision["should_respond"] is True
    assert decision["force_reply"] is True
    assert "直接 @ 了她" in decision["reason"]


@pytest.mark.parametrize("config_mode", ["default", "true", "legacy", "missing"])
async def test_enabled_or_missing_switch_uses_existing_router(monkeypatch, config_mode) -> None:
    if config_mode == "missing":
        config = None
    elif config_mode == "legacy":
        config = SimpleNamespace(chatter=SimpleNamespace())
    else:
        config = LifeEngineConfig()
        if config_mode == "true":
            config.chatter.router_enabled = True
    chatter = _chatter(config)
    history = AsyncMock(return_value="synthetic history")
    projection = AsyncMock(return_value="synthetic projection")
    old_decision = {"should_respond": False, "reason": "synthetic existing decision"}
    route = AsyncMock(return_value=old_decision)

    async def immediate(awaitable):
        return await awaitable

    monkeypatch.setattr(chatter, "_get_life_service", lambda: None)
    monkeypatch.setattr(chatter, "_build_history_text_async", history)
    monkeypatch.setattr(chatter, "_build_chat_router_prefix_prompt", projection)
    monkeypatch.setattr(chatter, "_await_with_watchdog_keepalive", immediate)
    monkeypatch.setattr(router_module, "route_should_respond", route)

    decision = await chatter._should_respond("synthetic input", [_message()], _stream())

    assert decision is old_decision
    history.assert_awaited_once()
    projection.assert_awaited_once()
    route.assert_awaited_once()
    assert route.await_args.kwargs["history_text"] == "synthetic history"
    assert route.await_args.kwargs["prefix_prompt"] == "synthetic projection"
    assert callable(route.await_args.kwargs["activity_recorder"])


def test_router_config_defaults_true_accepts_false_and_is_visible() -> None:
    assert LifeEngineConfig().chatter.router_enabled is True
    section = LifeEngineConfig.ChatterSection(router_enabled=False)
    assert section.router_enabled is False
    assert section.model_dump()["router_enabled"] is False
    assert "router_enabled" in LifeEngineConfig.__config_schema_visible_fields__["chatter"]


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        (None, False),
        ({}, False),
        ({"enabled": True}, True),
        ({"enabled": True, "router_enabled": False}, False),
        ({"enabled": True, "router_context_projection_enabled": False}, False),
        ({"enabled": False, "router_enabled": True}, False),
        (
            {
                "enabled": True,
                "router_enabled": False,
                "router_context_projection_enabled": True,
            },
            False,
        ),
        (
            {
                "enabled": True,
                "router_enabled": True,
                "router_context_projection_enabled": True,
            },
            True,
        ),
    ],
)
def test_service_router_projection_start_gate(fields, expected) -> None:
    chatter_cfg = SimpleNamespace(**fields) if fields is not None else None
    assert LifeEngineService._should_start_router_context_projection(chatter_cfg) is expected


async def test_disabled_router_delivers_to_main_then_honors_explicit_pass(monkeypatch) -> None:
    LifeChatter.reset_global_runtime()
    chatter = _chatter(_disabled_config())
    unread = _message()
    pending = [unread]
    flushed = []
    prefix = "Synthetic subject-authority prefix"

    class MainRequest:
        def __init__(self):
            self.payloads = [LLMPayload(ROLE.SYSTEM, Text(prefix))]
            self.call_list = [
                SimpleNamespace(id="synthetic-pass", name="action-life_pass_and_wait", args={})
            ]
            self.message = ""
            self.send_calls = 0

        def add_payload(self, payload):
            self.payloads.append(payload)

        async def send(self, *, stream=False):
            assert stream is False
            assert flushed == [], "No unread consumption before main model delivery"
            assert rt.must_reply is False
            self.send_calls += 1
            return self

        def __await__(self):
            async def done():
                return self

            return done().__await__()

    request = MainRequest()
    rt = _WorkflowRuntime(
        response=request,
        phase=_Phase.WAIT_USER,
        history_merged=False,
        unreads=[],
        cross_round_seen_signatures=set(),
        unread_msgs_to_flush=[],
    )
    LifeChatter._GLOBAL_RUNTIME = rt
    LifeChatter._GLOBAL_USABLE_MAP = {}

    async def fetch_unreads():
        return [], list(pending)

    async def flush_unreads(messages):
        flushed.extend(messages)
        consumed = {msg.message_id for msg in messages}
        pending[:] = [msg for msg in pending if msg.message_id not in consumed]

    async def immediate(awaitable):
        return await awaitable

    prefix_builder = AsyncMock(return_value=prefix)
    router_projection = AsyncMock(side_effect=AssertionError("No Router projection"))
    route = AsyncMock(side_effect=AssertionError("No Router model"))
    tools = AsyncMock(side_effect=AssertionError("Pass must not send externally"))
    monkeypatch.setattr(chatter, "_build_chat_system_prompt", prefix_builder)
    monkeypatch.setattr(chatter, "_build_chat_router_prefix_prompt", router_projection)
    monkeypatch.setattr(router_module, "route_should_respond", route)
    monkeypatch.setattr(chatter, "fetch_unreads", fetch_unreads)
    monkeypatch.setattr(chatter, "flush_unreads", flush_unreads)
    monkeypatch.setattr(chatter, "_build_history_text_async", AsyncMock(return_value=""))
    monkeypatch.setattr(chatter, "_build_dynamic_context_text", AsyncMock(return_value=("", 0)))
    monkeypatch.setattr(chatter, "_await_model_turn", immediate)
    monkeypatch.setattr(chatter, "_maybe_compact_runtime_context", AsyncMock(return_value=None))
    monkeypatch.setattr(chatter, "_save_rolling_context_snapshot", AsyncMock(return_value=None))
    monkeypatch.setattr(chatter, "run_tool_call", tools)
    monkeypatch.setattr(
        "src.kernel.concurrency.get_watchdog",
        lambda: SimpleNamespace(feed_dog=lambda _stream_id: None),
    )
    try:
        result = await chatter._drive_global_runtime_until_yield(_stream(), service=None)

        assert isinstance(result, Wait)
        assert request.send_calls == 1
        assert flushed == [unread]
        assert pending == []
        assert rt.phase == _Phase.WAIT_USER
        assert rt.must_reply is False
        assert rt.sent_visible_reply is False
        prefix_builder.assert_awaited_once()
        router_projection.assert_not_awaited()
        route.assert_not_awaited()
        tools.assert_not_awaited()
    finally:
        LifeChatter.reset_global_runtime()
