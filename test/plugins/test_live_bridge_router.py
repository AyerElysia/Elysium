"""live_bridge 输入规范化测试。"""

import asyncio
import base64
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException

from plugins.live_bridge.router.openai_router import (
    ChatCompletionRequest,
    ChatMessage,
    MinecraftTTSRequest,
    MinecraftDecisionResult,
    OpenAIRouter,
    _get_last_user_content,
    _normalize_live_comment,
    _send_and_collect_llm_response,
)
from plugins.live_bridge.minecraft_operator import parse_minecraft_decision_request
from src.core.components.types import EventType


def test_live_bridge_prefers_last_user_message() -> None:
    messages = [
        type("Msg", (), {"role": "system", "content": "sys"})(),
        type("Msg", (), {"role": "user", "content": "first"})(),
        type("Msg", (), {"role": "assistant", "content": "reply"})(),
        type("Msg", (), {"role": "user", "content": "last"})(),
    ]

    assert _get_last_user_content(messages) == "last"


def test_live_bridge_normalizes_legacy_prefixed_viewer_template() -> None:
    viewer, comment = _normalize_live_comment('请简要回复:观众“测试用户”说：000')

    assert viewer == "测试用户"
    assert comment == "000"


def test_live_bridge_keeps_plain_comment_when_no_template() -> None:
    viewer, comment = _normalize_live_comment("今晚吃什么")

    assert viewer == ""
    assert comment == "今晚吃什么"


def test_minecraft_completion_response_serializes_tool_calls() -> None:
    response = OpenAIRouter._minecraft_completion_response(
        "elysia-minecraft",
        MinecraftDecisionResult(
            mode="tool",
            tool_name="switch_sit",
            arguments={"sit": True},
            reason="休息",
        ),
    )

    choice = response.choices[0]

    assert choice.finish_reason == "tool_calls"
    assert choice.message.role == "assistant"
    assert choice.message.content == ""
    assert choice.message.tool_calls is not None
    assert choice.message.tool_calls[0].function.name == "switch_sit"
    assert choice.message.tool_calls[0].function.arguments == '{"sit":true}'


def test_minecraft_completion_response_keeps_text_reply_shape() -> None:
    response = OpenAIRouter._minecraft_completion_response(
        "elysia-minecraft",
        MinecraftDecisionResult(mode="say", content="先等等。"),
    )

    assert response.choices[0].message == ChatMessage(role="assistant", content="先等等。")


def test_minecraft_tts_mirror_payload_uses_ai_vtuber_send_contract(monkeypatch) -> None:
    monkeypatch.delenv("LIVE_BRIDGE_MINECRAFT_TTS_TYPE", raising=False)
    monkeypatch.delenv("LIVE_BRIDGE_MINECRAFT_TTS_USERNAME", raising=False)

    payload = OpenAIRouter._minecraft_tts_payload("  爱莉听到了。 ")

    assert payload == {
        "type": "reread_top_priority",
        "data": {
            "type": "reread_top_priority",
            "username": "Minecraft",
            "content": "爱莉听到了。",
            "source": "minecraft",
        },
    }


def test_minecraft_player2_tts_request_accepts_alias_fields() -> None:
    request = MinecraftTTSRequest(
        text="早呀",
        speed=1.2,
        play_in_app=True,
        voice_ids=["voice-a"],
    )

    assert request.text == "早呀"
    assert request.speed == 1.2
    assert request.play_in_app is True
    assert request.voice_ids == ["voice-a"]


async def test_minecraft_tts_synthesis_returns_audio_bytes(monkeypatch) -> None:
    router = object.__new__(OpenAIRouter)
    audio = b"RIFFfake-wav"

    class FakeTTSService:
        async def generate_voice(self, text, style):
            assert text == "早呀"
            assert style == "default"
            return base64.b64encode(audio).decode("ascii")

    monkeypatch.setattr(router, "_get_tts_voice_service", lambda: FakeTTSService())

    assert await router._synthesize_minecraft_tts("早呀") == audio


async def test_minecraft_tts_synthesis_requires_service(monkeypatch) -> None:
    router = object.__new__(OpenAIRouter)
    monkeypatch.setattr(router, "_get_tts_voice_service", lambda: None)

    with pytest.raises(HTTPException) as exc_info:
        await router._synthesize_minecraft_tts("早呀")

    assert exc_info.value.status_code == 503


async def test_minecraft_say_decision_triggers_ai_vtuber_tts(monkeypatch) -> None:
    router = object.__new__(OpenAIRouter)
    request = SimpleNamespace(latest_user_content="爱莉，是你么？")
    queued = []

    class FakeOperator:
        async def decide(self, decision_request, ask_callback):
            return MinecraftDecisionResult(mode="say", content="在呢。", source="elysia")

        async def record_life_event(self, text, *, stream_id):
            return None

    monkeypatch.setattr(router, "_minecraft_operator", FakeOperator())
    monkeypatch.setattr(
        router,
        "_queue_minecraft_say_tts",
        lambda content, *, source="": queued.append((content, source)),
    )

    result = await router._handle_minecraft_decision(request)

    assert result.content == "在呢。"
    assert queued == [("在呢。", "elysia")]


async def test_minecraft_tool_decision_does_not_trigger_tts(monkeypatch) -> None:
    router = object.__new__(OpenAIRouter)
    request = SimpleNamespace(latest_user_content="坐下")
    queued = []

    class FakeOperator:
        async def decide(self, decision_request, ask_callback):
            return MinecraftDecisionResult(
                mode="tool",
                tool_name="switch_sit",
                arguments={"sit": True},
                source="elysia",
            )

        async def record_life_event(self, text, *, stream_id):
            return None

    monkeypatch.setattr(router, "_minecraft_operator", FakeOperator())
    monkeypatch.setattr(
        router,
        "_queue_minecraft_say_tts",
        lambda content, *, source="": queued.append((content, source)),
    )

    result = await router._handle_minecraft_decision(request)

    assert result.is_tool_call
    assert queued == []


async def test_minecraft_decision_dispatch_bypasses_chat_buffer(monkeypatch) -> None:
    request = parse_minecraft_decision_request(
        [ChatMessage(role="user", content="爱莉，是你么？")],
        [
            {
                "type": "function",
                "function": {
                    "name": "query_game_context",
                    "description": "query context",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ],
        model="elysia-minecraft",
    )
    assert request is not None

    router = object.__new__(OpenAIRouter)
    captured = {}

    async def fake_dispatch(**kwargs):
        captured.update(kwargs)
        return '{"mode":"say","content":"在呢。","reason":"回应玩家"}'

    monkeypatch.setattr(router, "_dispatch_message_and_collect", fake_dispatch)

    reply = await router._ask_elysia_for_minecraft_decision(request, "temporary protocol")

    assert reply
    assert captured["platform"] == "game.minecraft.operator"
    assert captured["bypass_message_buffer"] is True
    assert captured["total_timeout"] == router._GAME_DECISION_TOTAL_TIMEOUT
    assert captured["segment_timeout"] == router._SEGMENT_TIMEOUT


async def test_live_chat_uses_full_chatter_by_default(monkeypatch) -> None:
    router = object.__new__(OpenAIRouter)
    captured = {}

    async def fake_fast(**kwargs):
        raise AssertionError("fast reply path should be opt-in")

    async def fake_dispatch(**kwargs):
        captured.update(kwargs)
        return "在呢。"

    monkeypatch.delenv("LIVE_BRIDGE_FAST_REPLY_ENABLED", raising=False)
    monkeypatch.setattr(router, "_handle_live_chat_fast", fake_fast)
    monkeypatch.setattr(router, "_dispatch_message_and_collect", fake_dispatch)

    reply = await router._handle_live_chat(
        [ChatMessage(role="user", content="观众“测试用户”说：爱莉爱莉")]
    )

    assert reply == "在呢。"
    assert captured["stream_id"] == "live_broadcast"
    assert captured["platform"] == "live"
    assert captured["sender_name"] == "测试用户"


async def test_live_chat_uses_fast_reply_when_enabled(monkeypatch) -> None:
    router = object.__new__(OpenAIRouter)
    captured = {}

    async def fake_fast(**kwargs):
        captured.update(kwargs)
        return "在呢。"

    async def fake_dispatch(**kwargs):
        raise AssertionError("full chatter path should not be used")

    monkeypatch.setenv("LIVE_BRIDGE_FAST_REPLY_ENABLED", "true")
    monkeypatch.setattr(router, "_handle_live_chat_fast", fake_fast)
    monkeypatch.setattr(router, "_dispatch_message_and_collect", fake_dispatch)

    reply = await router._handle_live_chat(
        [ChatMessage(role="user", content="观众“测试用户”说：爱莉爱莉")]
    )

    assert reply == "在呢。"
    assert captured["stream_id"] == "live_broadcast"
    assert captured["platform"] == "live"
    assert captured["viewer_name"] == "测试用户"


async def test_live_chat_fast_failure_does_not_fallback_to_full_chatter_by_default(monkeypatch) -> None:
    router = object.__new__(OpenAIRouter)

    async def fake_fast(**kwargs):
        raise RuntimeError("fast model timeout")

    async def fake_dispatch(**kwargs):
        raise AssertionError("full chatter fallback should be opt-in")

    monkeypatch.setenv("LIVE_BRIDGE_FAST_REPLY_ENABLED", "true")
    monkeypatch.delenv("LIVE_BRIDGE_FAST_REPLY_FALLBACK_TO_CHATTER", raising=False)
    monkeypatch.setattr(router, "_handle_live_chat_fast", fake_fast)
    monkeypatch.setattr(router, "_dispatch_message_and_collect", fake_dispatch)

    reply = await router._handle_live_chat([ChatMessage(role="user", content="爱莉爱莉")])

    assert "卡了一下" in reply


def test_live_fast_reply_sanitizer_strips_wrappers() -> None:
    assert OpenAIRouter._sanitize_live_fast_reply("```text\n爱莉：看到啦。\n```") == "看到啦。"


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked_stage", ["send", "response"])
async def test_bridge_total_timeout_covers_send_and_response(blocked_stage: str) -> None:
    class _BlockingResponse:
        def __await__(self):
            async def _collect():
                await asyncio.Event().wait()

            return _collect().__await__()

    class _BlockingRequest:
        async def send(self, *, stream: bool):
            assert stream is False
            if blocked_stage == "send":
                await asyncio.Event().wait()
            return _BlockingResponse()

    with pytest.raises(TimeoutError):
        await _send_and_collect_llm_response(
            _BlockingRequest(),
            timeout=0.01,
        )


@pytest.mark.asyncio
async def test_chat_completions_rejects_unsupported_streaming() -> None:
    router = OpenAIRouter.__new__(OpenAIRouter)
    handlers: dict[str, object] = {}

    class _FakeApp:
        def post(self, path, **_kwargs):
            def _decorator(func):
                handlers[path] = func
                return func

            return _decorator

    router.app = _FakeApp()
    router.register_endpoints()

    with pytest.raises(HTTPException) as exc_info:
        await handlers["/chat/completions"](
            ChatCompletionRequest(
                model="elysia",
                messages=[ChatMessage(role="user", content="你好")],
                stream=True,
            )
        )

    assert exc_info.value.status_code == 400
    assert "stream=true" in exc_info.value.detail


@pytest.mark.asyncio
async def test_router_subscribes_reply_queue_to_delivered_event(monkeypatch) -> None:
    router = OpenAIRouter.__new__(OpenAIRouter)
    event_bus = SimpleNamespace(subscribe=Mock())
    monkeypatch.setattr("src.kernel.event.get_event_bus", lambda: event_bus)

    await router.startup()

    event_bus.subscribe.assert_called_once()
    event_name, callback = event_bus.subscribe.call_args.args
    assert event_name == getattr(
        EventType,
        "ON_MESSAGE_DELIVERED",
        "on_message_delivered",
    )
    assert event_name != EventType.ON_MESSAGE_SENT
    assert callback == router._on_message_sent


@pytest.mark.asyncio
async def test_chat_completions_routes_sister_marker_to_isolated_handler(monkeypatch) -> None:
    router = OpenAIRouter.__new__(OpenAIRouter)

    async def fake_sister_handler(request: ChatCompletionRequest):
        return SimpleNamespace(model=request.model, routed="sister")

    monkeypatch.setattr(router, "_handle_sister_chat", fake_sister_handler)

    handlers: dict[str, object] = {}

    class _FakeApp:
        def post(self, path, **_kwargs):
            def _decorator(func):
                handlers[path] = func
                return func

            return _decorator

    router.app = _FakeApp()
    router.register_endpoints()
    request = ChatCompletionRequest(
        model="elysia-sister",
        messages=[ChatMessage(role="user", content="姐姐好")],
    )

    result = await handlers["/chat/completions"](request)

    assert getattr(result, "routed", None) == "sister"


@pytest.mark.asyncio
async def test_record_sister_reply_persists_directly_to_stream_history(monkeypatch) -> None:
    router = OpenAIRouter.__new__(OpenAIRouter)
    stream_manager = SimpleNamespace(
        get_or_create_stream=AsyncMock(return_value=SimpleNamespace()),
        add_sent_message_to_history=AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "src.core.managers.stream_manager.get_stream_manager",
        lambda: stream_manager,
    )

    await router._record_sister_reply("妹妹，晚上好。")

    stream_manager.get_or_create_stream.assert_awaited_once_with(
        stream_id="sister_bridge_private",
        platform="sister_bridge",
        user_id="astrbot_little_elysia",
        chat_type="private",
    )
    stream_manager.add_sent_message_to_history.assert_awaited_once()
    reply = stream_manager.add_sent_message_to_history.await_args.args[0]
    assert reply.stream_id == "sister_bridge_private"
    assert reply.platform == "sister_bridge"
    assert reply.sender_id == "elysia"
    assert reply.content == "妹妹，晚上好。"
    assert reply.extra["sister_bridge"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("generation_result", "expected_status"),
    [
        (RuntimeError("provider failed"), 502),
        (TimeoutError(), 504),
        ("", 502),
    ],
)
async def test_sister_generation_failures_remain_retryable_http_errors(
    monkeypatch,
    generation_result: object,
    expected_status: int,
) -> None:
    router = OpenAIRouter.__new__(OpenAIRouter)
    event_manager = SimpleNamespace(publish_event=AsyncMock(return_value=None))
    record_sister = AsyncMock(return_value=None)
    generation = AsyncMock()
    if isinstance(generation_result, BaseException):
        generation.side_effect = generation_result
    else:
        generation.return_value = generation_result

    monkeypatch.setattr(
        "src.core.managers.event_manager.get_event_manager",
        lambda: event_manager,
    )
    monkeypatch.setattr(router, "_record_sister_reply", record_sister)

    with pytest.raises(HTTPException) as exc_info:
        monkeypatch.setattr(router, "_generate_sister_reply", generation)
        await router._handle_sister_chat(
            ChatCompletionRequest(
                model="elysia-sister",
                messages=[ChatMessage(role="user", content="姐姐好")],
            )
        )

    assert exc_info.value.status_code == expected_status
    record_sister.assert_not_awaited()


@pytest.mark.asyncio
async def test_sister_prompt_reads_only_isolated_stream_history(monkeypatch) -> None:
    from src.core.models.message import Message

    router = OpenAIRouter.__new__(OpenAIRouter)
    sister_message = Message(
        message_id="sister_old",
        time=1.0,
        content="姐姐，今天过得好吗？",
        processed_plain_text="姐姐，今天过得好吗？",
        sender_id="astrbot_little_elysia",
        sender_name="妹妹爱莉希雅",
        platform="sister_bridge",
        chat_type="private",
        stream_id="sister_bridge_private",
    )
    unrelated_message = Message(
        message_id="other_old",
        time=2.0,
        content="这条来自其他会话",
        processed_plain_text="这条来自其他会话",
        sender_id="someone_else",
        sender_name="其他人",
        platform="qq",
        chat_type="private",
        stream_id="unrelated_stream",
    )
    current_message = Message(
        message_id="sister_current",
        time=3.0,
        content="姐姐在吗？",
        processed_plain_text="姐姐在吗？",
        sender_id="astrbot_little_elysia",
        sender_name="妹妹爱莉希雅",
        platform="sister_bridge",
        chat_type="private",
        stream_id="sister_bridge_private",
    )
    stream_manager = SimpleNamespace(
        get_or_create_stream=AsyncMock(return_value=SimpleNamespace()),
        get_stream_messages=AsyncMock(
            return_value=[sister_message, unrelated_message, current_message]
        ),
    )
    monkeypatch.setenv("LIVE_BRIDGE_SISTER_HISTORY_LIMIT", "2")
    monkeypatch.setattr(
        "src.core.managers.stream_manager.get_stream_manager",
        lambda: stream_manager,
    )

    prompt = await router._build_sister_user_prompt(current_message)

    stream_manager.get_stream_messages.assert_awaited_once_with(
        "sister_bridge_private",
        limit=3,
        defer_content=False,
    )
    assert "姐姐，今天过得好吗？" in prompt
    assert "这条来自其他会话" not in prompt
    assert prompt.count("姐姐在吗？") == 1
