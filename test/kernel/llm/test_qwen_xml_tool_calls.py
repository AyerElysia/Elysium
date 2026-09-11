from __future__ import annotations

from types import SimpleNamespace

from src.kernel.llm.qwen_xml_tool_calls import (
    parse_qwen_xml_tool_calls,
    recover_qwen_xml_tool_markup,
    split_qwen_think_tags,
)


def test_parse_qwen_xml_read_context_group() -> None:
    raw = """
</think>
<tool_call>
<function=tool-nucleus_read_context_group>
<parameter=group_ref>
ctxg_3dba962a23b6bbcf8511615f331281006e029de32d729ef9ac906a274640f1e3
</parameter>
<parameter=reason>
上下文压缩。需要读取第1组原文。
</parameter>
</function>
</tool_call>
"""
    message, reasoning, calls = recover_qwen_xml_tool_markup(raw)
    assert not message
    assert not reasoning
    assert len(calls) == 1
    assert calls[0]["name"] == "tool-nucleus_read_context_group"
    assert (
        calls[0]["args"]["group_ref"]
        == "ctxg_3dba962a23b6bbcf8511615f331281006e029de32d729ef9ac906a274640f1e3"
    )
    assert "上下文压缩" in calls[0]["args"]["reason"]


def test_split_think_tags_and_leave_plain_text() -> None:
    reasoning, rest = split_qwen_think_tags(
        "<think>先读组</think>\n准备调用工具"
    )
    assert reasoning == "先读组"
    assert rest == "准备调用工具"


def test_json_inside_tool_call_block() -> None:
    message, calls = parse_qwen_xml_tool_calls(
        '<tool_call>{"name": "nucleus_todo", "arguments": {"action": "list"}}</tool_call>'
    )
    assert message == ""
    assert calls[0]["name"] == "nucleus_todo"
    assert calls[0]["args"] == {"action": "list"}


def test_native_calls_skip_xml_recovery() -> None:
    message, reasoning, calls = recover_qwen_xml_tool_markup(
        "<tool_call><function=tool-nucleus_todo></function></tool_call>",
        existing_calls=[{"name": "already"}],
    )
    assert calls == []
    assert "<tool_call>" in message
    assert reasoning == ""


def test_parse_completion_message_recovers_qwen_xml() -> None:
    from src.kernel.llm.model_client.openai_client import _parse_completion_message

    msg = SimpleNamespace(
        content=(
            "<think>先读第一组</think>\n"
            "<tool_call>\n"
            "<function=tool-nucleus_read_context_group>\n"
            "<parameter=group_ref>\n"
            "ctxg_abc\n"
            "</parameter>\n"
            "</function>\n"
            "</tool_call>\n"
        ),
        tool_calls=None,
        function_call=None,
        reasoning_content=None,
        reasoning=None,
    )
    message, tool_calls, reasoning = _parse_completion_message(msg)
    assert message == ""
    assert reasoning == "先读第一组"
    assert tool_calls[0]["name"] == "tool-nucleus_read_context_group"
    assert tool_calls[0]["args"]["group_ref"] == "ctxg_abc"
