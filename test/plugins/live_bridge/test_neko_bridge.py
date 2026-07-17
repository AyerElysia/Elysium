from __future__ import annotations

from dataclasses import dataclass
import json

from plugins.live_bridge.neko_bridge import (
    NekoToolAdapter,
    build_neko_tool_adapters,
    build_pending_tool_exchange_text,
    last_user_content,
)


@dataclass(slots=True)
class MessageStub:
    role: str
    content: str
    tool_calls: list[dict] | None = None
    tool_call_id: str | None = None
    name: str | None = None


def test_last_user_content_prefers_final_user_message() -> None:
    messages = [
        MessageStub("system", "persona"),
        MessageStub("user", "first"),
        MessageStub("assistant", "reply"),
        MessageStub("user", "second"),
    ]

    assert last_user_content(messages) == "second"


def test_last_user_content_falls_back_to_last_message_when_no_user() -> None:
    messages = [MessageStub("system", "persona"), MessageStub("assistant", "hi")]

    assert last_user_content(messages) == "hi"


def test_last_user_content_handles_empty_messages() -> None:
    assert last_user_content([]) == ""


def test_build_pending_tool_exchange_text_empty_when_user_is_latest() -> None:
    messages = [MessageStub("system", "persona"), MessageStub("user", "hello")]

    assert build_pending_tool_exchange_text(messages) == ""


def test_build_pending_tool_exchange_text_preserves_call_result_associations() -> None:
    long_result = "done:" + "x" * 4100
    messages = [
        MessageStub("system", "persona"),
        MessageStub("user", "帮我动耳朵并切换表情"),
        MessageStub(
            "assistant",
            "先试一下。",
            tool_calls=[
                {
                    "id": "call_ear",
                    "type": "function",
                    "function": {
                        "name": "play_ear_animation",
                        "arguments": '{"speed":2}',
                    },
                },
                {
                    "id": "call_emote",
                    "type": "function",
                    "function": {
                        "name": "play_emote",
                        "arguments": '{"emote":"happy"}',
                    },
                },
            ],
        ),
        MessageStub("tool", long_result, tool_call_id="call_emote"),
        MessageStub("tool", "ok:played", tool_call_id="call_ear", name="play_ear_animation"),
    ]

    text = build_pending_tool_exchange_text(messages)
    records = [
        json.loads(line.split("] ", 1)[1])
        for line in text.splitlines()
        if line.startswith(("[assistant 工具调用]", "[工具结果]"))
    ]

    assert records == [
        {
            "id": "call_ear",
            "name": "play_ear_animation",
            "arguments": '{"speed":2}',
        },
        {
            "id": "call_emote",
            "name": "play_emote",
            "arguments": '{"emote":"happy"}',
        },
        {
            "tool_call_id": "call_emote",
            "name": "play_emote",
            "result": long_result,
        },
        {
            "tool_call_id": "call_ear",
            "name": "play_ear_animation",
            "result": "ok:played",
        },
    ]
    assert "[assistant]: 先试一下。" in text


def test_build_pending_tool_exchange_text_respects_limit() -> None:
    messages = [MessageStub("user", "hi")]
    for i in range(10):
        messages.append(MessageStub("assistant", f"thinking {i}"))

    text = build_pending_tool_exchange_text(messages, limit=2)

    assert text.count("[assistant]") == 2
    assert "thinking 9" in text
    assert "thinking 0" not in text


def test_neko_tool_adapter_preserves_schema_before_kernel_handling() -> None:
    raw_tool = {
        "type": "function",
        "function": {
            "name": "play_emote",
            "description": "play a Live2D emote",
            "parameters": {"type": "object", "properties": {"emote": {"type": "string"}}},
        },
    }
    adapter = NekoToolAdapter(raw_tool)

    assert adapter.to_schema() is raw_tool
    assert adapter.to_schema()["function"]["name"] == "play_emote"


def test_neko_tool_adapter_execute_raises_not_implemented() -> None:
    adapter = NekoToolAdapter({"type": "function", "function": {"name": "noop"}})

    try:
        adapter.execute(reason="test")
    except NotImplementedError:
        pass
    else:
        raise AssertionError("execute() should raise NotImplementedError")


def test_build_neko_tool_adapters_wraps_each_tool() -> None:
    tools = [
        {"type": "function", "function": {"name": "a"}},
        {"type": "function", "function": {"name": "b"}},
    ]

    adapters = build_neko_tool_adapters(tools)

    assert len(adapters) == 2
    assert [a.to_schema()["function"]["name"] for a in adapters] == ["a", "b"]


def test_build_neko_tool_adapters_returns_empty_list_for_none() -> None:
    assert build_neko_tool_adapters(None) == []


def test_build_neko_tool_adapters_skips_non_mapping_entries() -> None:
    adapters = build_neko_tool_adapters(["not-a-dict"])  # type: ignore[list-item]

    assert adapters == []
