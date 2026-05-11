from __future__ import annotations

from dataclasses import dataclass

from plugins.live_bridge.minecraft_operator import (
    build_decision_prompt,
    build_fallback_decision,
    build_persistent_event_text,
    extract_decision_result,
    parse_minecraft_decision_request,
)


@dataclass(slots=True)
class MessageStub:
    role: str
    content: str
    tool_calls: list[dict] | None = None
    tool_call_id: str | None = None


def _tools() -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": "query_game_context",
                "description": "query live context",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "category_id": {
                            "type": "string",
                            "enum": ["position", "nearby_entities"],
                        }
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "switch_sit",
                "description": "switch sit state",
                "parameters": {
                    "type": "object",
                    "properties": {"sit": {"type": "boolean"}},
                },
            },
        },
    ]


def test_parse_minecraft_decision_request_detects_tlm_tools() -> None:
    request = parse_minecraft_decision_request(
        [
            MessageStub("system", "maid setting"),
            MessageStub("user", "follow me"),
        ],
        _tools(),
        model="elysia-minecraft",
    )

    assert request is not None
    assert request.model == "elysia-minecraft"
    assert request.tool_names == ["query_game_context", "switch_sit"]
    assert request.latest_user_content == "follow me"


def test_build_decision_prompt_keeps_transport_details_out_of_prompt() -> None:
    request = parse_minecraft_decision_request([MessageStub("user", "爱莉，是你么？")], _tools())
    assert request is not None

    prompt = build_decision_prompt(request)

    assert "action-life_send_text" in prompt
    assert "单行 JSON" in prompt
    assert "Adapter" not in prompt
    assert "life_chatter" not in prompt
    assert "私聊流" not in prompt
    assert "爱莉，是你么？" not in prompt


def test_persistent_event_keeps_game_state_but_drops_tool_schema() -> None:
    request = parse_minecraft_decision_request(
        [
            MessageStub("system", "你是一个 Minecraft AI，请遵守大量使用说明"),
            MessageStub("assistant", "我先稳一下，等看清楚情况再行动。"),
            MessageStub(
                "user",
                "<context>- Time: 12:04, - Weather: Sunny\nSelf health: 20.0</context>",
            ),
            MessageStub("assistant", "", [{"function": {"name": "query_game_context"}}]),
            MessageStub("tool", "Main-hand item: Empty\nBackpack items: [泥土]x11"),
        ],
        _tools(),
    )
    assert request is not None

    event_text = build_persistent_event_text(request)

    assert "Minecraft事件" in event_text
    assert "Weather: Sunny" in event_text
    assert "Main-hand item: Empty" in event_text
    assert "Backpack items" in event_text
    assert "assistant_tool_calls: query_game_context" in event_text
    assert "大量使用说明" not in event_text
    assert "parameters" not in event_text
    assert "query live context" not in event_text


def test_extract_decision_result_returns_valid_tool_call() -> None:
    request = parse_minecraft_decision_request([MessageStub("user", "sit down")], _tools())
    assert request is not None

    result = extract_decision_result(
        '{"mode":"tool","tool_name":"switch_sit","arguments":{"sit":true},"reason":"休息一下"}',
        request,
    )

    assert result is not None
    assert result.is_tool_call
    assert result.tool_name == "switch_sit"
    assert result.arguments == {"sit": True}


def test_extract_decision_result_rejects_unknown_tool() -> None:
    request = parse_minecraft_decision_request([MessageStub("user", "do it")], _tools())
    assert request is not None

    result = extract_decision_result(
        '{"mode":"tool","tool_name":"teleport_player","arguments":{}}',
        request,
    )

    assert result is None


def test_extract_decision_result_accepts_plain_text_as_say() -> None:
    request = parse_minecraft_decision_request([MessageStub("user", "hello")], _tools())
    assert request is not None

    result = extract_decision_result("听得到，我在。", request)

    assert result is not None
    assert not result.is_tool_call
    assert result.mode == "say"
    assert result.content == "听得到，我在。"
    assert result.source == "elysia_plain_text"


def test_fallback_queries_context_before_any_tool_result() -> None:
    request = parse_minecraft_decision_request([MessageStub("user", "what now")], _tools())
    assert request is not None

    result = build_fallback_decision(request, "bad reply")

    assert result.is_tool_call
    assert result.tool_name == "query_game_context"
    assert result.arguments == {"category_id": "position"}


def test_fallback_says_safe_text_after_tool_result() -> None:
    request = parse_minecraft_decision_request(
        [
            MessageStub("user", "what now"),
            MessageStub("assistant", "", [{"id": "call_1"}]),
            MessageStub("tool", "position: x=1 y=64 z=1", tool_call_id="call_1"),
        ],
        _tools(),
    )
    assert request is not None

    result = build_fallback_decision(request, "bad reply")

    assert not result.is_tool_call
    assert result.content
