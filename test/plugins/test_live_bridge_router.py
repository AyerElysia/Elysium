"""live_bridge 输入规范化测试。"""

from plugins.live_bridge.router.openai_router import (
    ChatMessage,
    MinecraftDecisionResult,
    OpenAIRouter,
    _get_last_user_content,
    _normalize_live_comment,
)
from plugins.live_bridge.minecraft_operator import parse_minecraft_decision_request


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
