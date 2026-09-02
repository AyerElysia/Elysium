"""Tests for LLMContextManager behavior."""

from __future__ import annotations

from typing import Any, cast

import pytest

from src.core.prompt import (
    SystemReminderConsumeType,
    SystemReminderInsertType,
    get_system_reminder_store,
    reset_system_reminder_store,
)
from src.kernel.llm.context import LLMContextManager
from src.kernel.llm.exceptions import LLMContextError
from src.kernel.llm.payload import LLMPayload, Text, ToolCall, ToolResult
from src.kernel.llm.request import LLMRequest
from src.kernel.llm.roles import ROLE


class DummyTool:
    @classmethod
    def to_schema(cls) -> dict[str, Any]:
        return {"name": "dummy"}


def dummy_model() -> dict[str, Any]:
    return {
        "api_provider": "openai",
        "base_url": "https://api.openai.com/v1",
        "model_identifier": "gpt-4",
        "api_key": "sk-test",
        "client_type": "openai",
        "max_retry": 0,
        "timeout": 1,
        "retry_interval": 0,
        "price_in": 0.0,
        "price_out": 0.0,
        "temperature": 0.1,
        "max_tokens": 10,
        "max_context": 4096,
        "extra_params": {},
    }


def test_context_manager_trims_full_groups() -> None:
    manager = LLMContextManager()
    payloads = [
        LLMPayload(ROLE.SYSTEM, Text("sys")),
        LLMPayload(ROLE.TOOL, DummyTool),
        LLMPayload(ROLE.USER, Text("q1")),
        LLMPayload(ROLE.ASSISTANT, Text("a1")),
        LLMPayload(ROLE.TOOL_RESULT, ToolResult({"ok": True})),
        LLMPayload(ROLE.USER, Text("q2")),
        LLMPayload(ROLE.ASSISTANT, Text("a2")),
    ]

    trimmed = manager.maybe_trim(
        payloads,
        max_token_budget=40,
        token_counter=lambda items: len(items) * 10,
    )

    assert len(trimmed) == 4
    assert trimmed[0].role == ROLE.SYSTEM
    assert trimmed[1].role == ROLE.TOOL
    assert trimmed[2].role == ROLE.USER
    assert trimmed[2].content[0].text == "q2"
    assert trimmed[3].role == ROLE.ASSISTANT


def test_context_manager_applies_hook() -> None:
    called = {"value": False}

    def hook(dropped_groups, remaining_payloads):
        called["value"] = True
        return [LLMPayload(ROLE.ASSISTANT, Text("summary"))]

    manager = LLMContextManager(compression_hook=hook)
    payloads = [
        LLMPayload(ROLE.SYSTEM, Text("sys")),
        LLMPayload(ROLE.USER, Text("q1")),
        LLMPayload(ROLE.ASSISTANT, Text("a1")),
        LLMPayload(ROLE.USER, Text("q2")),
        LLMPayload(ROLE.ASSISTANT, Text("a2")),
    ]

    trimmed = manager.maybe_trim(
        payloads,
        max_token_budget=40,
        token_counter=lambda items: len(items) * 10,
    )

    assert called["value"] is True
    assert len(trimmed) == 4
    assert trimmed[0].role == ROLE.SYSTEM
    assert trimmed[1].role == ROLE.ASSISTANT
    assert trimmed[1].content[0].text == "summary"
    assert trimmed[2].role == ROLE.USER
    assert trimmed[2].content[0].text == "q2"


def test_context_manager_rebuilds_hook_when_more_groups_are_dropped() -> None:
    seen_dropped_counts: list[int] = []

    def hook(dropped_groups, remaining_payloads):
        del remaining_payloads
        seen_dropped_counts.append(len(dropped_groups))
        return [
            LLMPayload(ROLE.ASSISTANT, Text(f"dropped={len(dropped_groups)}")),
            LLMPayload(ROLE.USER, Text("reference-overhead")),
        ]

    manager = LLMContextManager(compression_hook=hook)
    payloads = [LLMPayload(ROLE.SYSTEM, Text("sys"))]
    for index in range(1, 5):
        payloads.extend(
            [
                LLMPayload(ROLE.USER, Text(f"q{index}")),
                LLMPayload(ROLE.ASSISTANT, Text(f"a{index}")),
            ]
        )

    trimmed = manager.maybe_trim(
        payloads,
        max_token_budget=80,
        token_counter=lambda items: len(items) * 10,
    )

    assert seen_dropped_counts == [1, 2]
    assert any(
        isinstance(part, Text) and part.text == "dropped=2"
        for payload in trimmed
        for part in payload.content
    )
    assert not any(
        isinstance(part, Text) and part.text in {"q1", "a1", "q2", "a2"}
        for payload in trimmed
        for part in payload.content
    )
    assert any(
        isinstance(part, Text) and part.text == "q3"
        for payload in trimmed
        for part in payload.content
    )


def test_llm_request_uses_custom_context_manager() -> None:
    class CustomManager(LLMContextManager):
        def __init__(self) -> None:
            super().__init__()
            self.called = False

        def maybe_trim(self, payloads: list[LLMPayload]) -> list[LLMPayload]:
            self.called = True
            return payloads

    manager = CustomManager()
    request = LLMRequest([dummy_model()], context_manager=manager)
    request.add_payload(LLMPayload(ROLE.USER, Text("hello")))

    assert manager.called is True


def test_context_manager_trims_by_token_budget() -> None:
    manager = LLMContextManager()
    payloads = [
        LLMPayload(ROLE.USER, Text("q1")),
        LLMPayload(ROLE.ASSISTANT, Text("a1")),
        LLMPayload(ROLE.USER, Text("q2")),
        LLMPayload(ROLE.ASSISTANT, Text("a2")),
        LLMPayload(ROLE.USER, Text("q3")),
        LLMPayload(ROLE.ASSISTANT, Text("a3")),
    ]

    # 每条消息按 10 token 计，预算 25 时只能保留最后一组（2条消息）
    trimmed = manager.maybe_trim(
        payloads,
        max_token_budget=25,
        token_counter=lambda items: len(items) * 10,
    )

    assert len(trimmed) == 2
    assert trimmed[0].role == ROLE.USER
    assert trimmed[0].content[0].text == "q3"
    assert trimmed[1].role == ROLE.ASSISTANT


def test_context_manager_bounds_one_oversized_user_group() -> None:
    manager = LLMContextManager()
    original = "instruction-head:" + ("x" * 400) + ":recent-tail"
    payloads = [
        LLMPayload(ROLE.SYSTEM, Text("system")),
        LLMPayload(ROLE.USER, Text(original)),
    ]

    def count_text_chars(items: list[LLMPayload]) -> int:
        return sum(
            len(part.text)
            for payload in items
            for part in payload.content
            if isinstance(part, Text)
        )

    trimmed = manager.maybe_trim(
        payloads,
        max_token_budget=120,
        token_counter=count_text_chars,
    )

    rendered = cast(Text, trimmed[1].content[0]).text
    assert count_text_chars(trimmed) <= 120
    assert rendered.startswith("instruction")
    assert rendered.endswith(":recent-tail")
    assert "context omitted to fit the task token budget" in rendered
    assert cast(Text, payloads[1].content[0]).text == original


def test_context_manager_rejects_oversized_pinned_payloads() -> None:
    manager = LLMContextManager()
    payloads = [
        LLMPayload(ROLE.SYSTEM, Text("s" * 200)),
        LLMPayload(ROLE.USER, Text("question")),
    ]

    def count_text_chars(items: list[LLMPayload]) -> int:
        return sum(
            len(part.text)
            for payload in items
            for part in payload.content
            if isinstance(part, Text)
        )

    with pytest.raises(LLMContextError, match="pinned or structured"):
        manager.maybe_trim(
            payloads,
            max_token_budget=100,
            token_counter=count_text_chars,
        )


def test_context_manager_system_tool_equivalent_add_payload() -> None:
    manager = LLMContextManager()
    payloads: list[LLMPayload] = []

    payloads = manager.system(payloads, Text("sys"))
    payloads = manager.tool(payloads, DummyTool)

    assert len(payloads) == 2
    assert payloads[0].role == ROLE.SYSTEM
    assert payloads[0].content[0].text == "sys"
    assert payloads[1].role == ROLE.TOOL


def test_context_manager_reminder_only_registers_until_next_payload() -> None:
    manager = LLMContextManager()
    payloads = [LLMPayload(ROLE.SYSTEM, Text("sys"))]

    manager.reminder("你必须先输出结论")

    assert len(payloads) == 1
    assert payloads[0].role == ROLE.SYSTEM

    payloads = manager.add_payload(payloads, LLMPayload(ROLE.USER, Text("你好")))
    assert len(payloads) == 2
    assert payloads[1].role == ROLE.USER
    # reminder 注入到 USER block 首部
    assert cast(Text, payloads[1].content[0]).text == "你必须先输出结论"
    assert cast(Text, payloads[1].content[1]).text == "你好"


def test_context_manager_register_reminder_defers_until_first_user() -> None:
    manager = LLMContextManager()
    payloads: list[LLMPayload] = []

    manager.reminder("先给结论")

    payloads = manager.add_payload(payloads, LLMPayload(ROLE.SYSTEM, Text("sys")))
    assert len(payloads) == 1
    assert payloads[0].role == ROLE.SYSTEM

    payloads = manager.add_payload(payloads, LLMPayload(ROLE.USER, Text("你好")))
    assert len(payloads) == 2
    assert payloads[0].role == ROLE.SYSTEM
    assert payloads[1].role == ROLE.USER
    # reminder 注入到 USER block 首部
    assert cast(Text, payloads[1].content[0]).text == "先给结论"
    assert cast(Text, payloads[1].content[1]).text == "你好"


def test_context_manager_reminder_wraps_system_text() -> None:
    manager = LLMContextManager()
    payloads: list[LLMPayload] = []

    manager.reminder("[goal]\n先给结论", wrap_with_system_tag=True)

    payloads = manager.add_payload(payloads, LLMPayload(ROLE.SYSTEM, Text("sys")))
    payloads = manager.add_payload(payloads, LLMPayload(ROLE.USER, Text("你好")))

    assert len(payloads) == 2
    assert payloads[0].role == ROLE.SYSTEM
    assert payloads[1].role == ROLE.USER
    # reminder 注入到 USER block 首部
    assert cast(Text, payloads[1].content[0]).text == "<system_reminder>\n[goal]\n先给结论\n</system_reminder>"
    assert cast(Text, payloads[1].content[1]).text == "你好"


def test_context_manager_reminder_waits_through_tool_until_first_user() -> None:
    manager = LLMContextManager()
    payloads = [LLMPayload(ROLE.SYSTEM, Text("sys"))]

    manager.reminder("先给结论", wrap_with_system_tag=True)

    payloads = manager.add_payload(payloads, LLMPayload(ROLE.TOOL, DummyTool))
    assert len(payloads) == 2
    assert payloads[0].role == ROLE.SYSTEM
    assert payloads[1].role == ROLE.TOOL

    payloads = manager.add_payload(payloads, LLMPayload(ROLE.USER, Text("你好")))
    assert len(payloads) == 3
    assert payloads[2].role == ROLE.USER
    # reminder 注入到 USER block 首部
    assert cast(Text, payloads[2].content[0]).text == "<system_reminder>\n先给结论\n</system_reminder>"
    assert cast(Text, payloads[2].content[1]).text == "你好"


def test_context_manager_dynamic_reminder_targets_last_user() -> None:
    manager = LLMContextManager()
    payloads = [LLMPayload(ROLE.USER, Text("第一条"))]

    manager.reminder("跟进最近一条", insert_type=SystemReminderInsertType.DYNAMIC)

    payloads = manager.add_payload(payloads, LLMPayload(ROLE.ASSISTANT, Text("收到")))
    payloads = manager.add_payload(payloads, LLMPayload(ROLE.USER, Text("第二条")))

    assert cast(Text, payloads[0].content[0]).text == "第一条"
    assert cast(Text, payloads[2].content[0]).text == "跟进最近一条"
    assert cast(Text, payloads[2].content[1]).text == "第二条"


def test_context_manager_dynamic_reminder_moves_to_new_last_user() -> None:
    manager = LLMContextManager()
    payloads: list[LLMPayload] = []

    manager.reminder("只跟最后一条", insert_type=SystemReminderInsertType.DYNAMIC)

    payloads = manager.add_payload(payloads, LLMPayload(ROLE.USER, Text("第一条")))
    assert cast(Text, payloads[0].content[0]).text == "只跟最后一条"
    assert cast(Text, payloads[0].content[1]).text == "第一条"

    payloads = manager.add_payload(payloads, LLMPayload(ROLE.ASSISTANT, Text("回复")))
    payloads = manager.add_payload(payloads, LLMPayload(ROLE.USER, Text("第二条")))

    assert cast(Text, payloads[0].content[0]).text == "第一条"
    assert cast(Text, payloads[2].content[0]).text == "只跟最后一条"
    assert cast(Text, payloads[2].content[1]).text == "第二条"


def test_context_manager_fixed_and_dynamic_reminders_target_different_users() -> None:
    manager = LLMContextManager()
    payloads: list[LLMPayload] = []

    manager.reminder("固定开头")
    manager.reminder("最近一条", insert_type=SystemReminderInsertType.DYNAMIC)

    payloads = manager.add_payload(payloads, LLMPayload(ROLE.USER, Text("第一条")))
    payloads = manager.add_payload(payloads, LLMPayload(ROLE.ASSISTANT, Text("回复")))
    payloads = manager.add_payload(payloads, LLMPayload(ROLE.USER, Text("第二条")))

    assert cast(Text, payloads[0].content[0]).text == "固定开头"
    assert cast(Text, payloads[0].content[1]).text == "第一条"
    assert cast(Text, payloads[2].content[0]).text == "最近一条"
    assert cast(Text, payloads[2].content[1]).text == "第二条"


def test_context_manager_reminder_bucket_refreshes_updated_dynamic_content() -> None:
    reset_system_reminder_store()
    store = get_system_reminder_store()
    store.set("actor", "screen", "第一次", insert_type=SystemReminderInsertType.DYNAMIC)

    manager = LLMContextManager()
    payloads: list[LLMPayload] = []
    manager.reminder_bucket("actor", wrap_with_system_tag=True)

    payloads = manager.add_payload(payloads, LLMPayload(ROLE.USER, Text("第一条")))
    assert cast(Text, payloads[0].content[0]).text == (
        "<system_reminder>\n[screen]\n第一次\n</system_reminder>"
    )

    store.set("actor", "screen", "第二次", insert_type=SystemReminderInsertType.DYNAMIC)
    payloads = manager.add_payload(payloads, LLMPayload(ROLE.ASSISTANT, Text("回复")))
    payloads = manager.add_payload(payloads, LLMPayload(ROLE.USER, Text("第二条")))

    assert cast(Text, payloads[0].content[0]).text == "第一条"
    assert cast(Text, payloads[2].content[0]).text == (
        "<system_reminder>\n[screen]\n第二次\n</system_reminder>"
    )
    assert cast(Text, payloads[2].content[1]).text == "第二条"

    reset_system_reminder_store()


def test_context_manager_once_reminder_bucket_is_consumed_after_injection() -> None:
    reset_system_reminder_store()
    store = get_system_reminder_store()
    store.set(
        "actor",
        "notice",
        "只提醒一次",
        insert_type=SystemReminderInsertType.DYNAMIC,
        consume=SystemReminderConsumeType.ONCE,
    )

    manager = LLMContextManager()
    payloads: list[LLMPayload] = []
    manager.reminder_bucket("actor", wrap_with_system_tag=True)

    payloads = manager.add_payload(payloads, LLMPayload(ROLE.USER, Text("第一条")))
    assert cast(Text, payloads[0].content[0]).text == (
        "<system_reminder>\n[notice]\n只提醒一次\n</system_reminder>"
    )

    payloads = manager.add_payload(payloads, LLMPayload(ROLE.ASSISTANT, Text("回复")))
    payloads = manager.add_payload(payloads, LLMPayload(ROLE.USER, Text("第二条")))

    assert cast(Text, payloads[0].content[0]).text == "第一条"
    assert cast(Text, payloads[2].content[0]).text == "第二条"

    reset_system_reminder_store()


def test_context_manager_defers_missing_tool_result_placeholder_at_tail() -> None:
    manager = LLMContextManager()
    payloads = [LLMPayload(ROLE.USER, Text("帮我调用工具"))]

    payloads = manager.add_payload(
        payloads,
        LLMPayload(
            ROLE.ASSISTANT,
            [
                Text("我将调用工具"),
                ToolCall(id="call_1", name="get_weather", args={"city": "上海"}),
            ],
        ),
    )

    assert len(payloads) == 2
    assert payloads[0].role == ROLE.USER
    assert payloads[1].role == ROLE.ASSISTANT


def test_context_manager_keeps_multiple_tool_results_in_merged_payload() -> None:
    manager = LLMContextManager()
    payloads = [LLMPayload(ROLE.USER, Text("请执行两个工具"))]

    payloads = manager.add_payload(
        payloads,
        LLMPayload(
            ROLE.ASSISTANT,
            [
                Text("开始执行"),
                ToolCall(id="call_1", name="write_memory", args={"content": "A"}),
                ToolCall(id="call_2", name="finish_task", args={"content": "ok"}),
            ],
        ),
    )

    payloads = manager.add_payload(
        payloads,
        LLMPayload(
            ROLE.TOOL_RESULT,
            ToolResult(value="写入成功", call_id="call_1", name="write_memory"),
        ),
    )
    payloads = manager.add_payload(
        payloads,
        LLMPayload(
            ROLE.TOOL_RESULT,
            ToolResult(value="任务完成", call_id="call_2", name="finish_task"),
        ),
    )

    assert len(payloads) == 3
    assert payloads[2].role == ROLE.TOOL_RESULT

    results = [part for part in payloads[2].content if isinstance(part, ToolResult)]
    assert len(results) == 2

    result_by_id = {result.call_id: result for result in results}
    assert result_by_id["call_1"].value == "写入成功"
    assert result_by_id["call_1"].name == "write_memory"
    assert result_by_id["call_2"].value == "任务完成"
    assert result_by_id["call_2"].name == "finish_task"


def test_context_manager_raises_when_tool_chain_is_broken_by_new_user() -> None:
    """strict 模式下：不自动补齐 tool_result；若 tool_calls 未闭合就进入下一条 USER，应直接报错。"""
    manager = LLMContextManager()
    payloads = [LLMPayload(ROLE.USER, Text("帮我调用工具"))]

    payloads = manager.add_payload(
        payloads,
        LLMPayload(
            ROLE.ASSISTANT,
            [
                Text("我将调用工具"),
                ToolCall(id="call_1", name="get_weather", args={"city": "上海"}),
            ],
        ),
    )

    with pytest.raises(LLMContextError):
        manager.add_payload(payloads, LLMPayload(ROLE.USER, Text("继续")))


def test_context_manager_allows_user_after_closed_tool_result_chain() -> None:
    """工具结果已闭合后，允许新 USER 直接接入，避免工具执行期间的新消息被阻塞。"""
    manager = LLMContextManager()
    payloads = [LLMPayload(ROLE.USER, Text("先调用工具"))]

    payloads = manager.add_payload(
        payloads,
        LLMPayload(
            ROLE.ASSISTANT,
            [
                Text("我将调用工具"),
                ToolCall(id="call_1", name="web_search", args={"query": "x"}),
            ],
        ),
    )

    payloads = manager.add_payload(
        payloads,
        LLMPayload(
            ROLE.TOOL_RESULT,
            ToolResult(value="result", call_id="call_1", name="web_search"),
        ),
    )

    payloads = manager.add_payload(payloads, LLMPayload(ROLE.USER, Text("继续")))

    assert [payload.role for payload in payloads] == [
        ROLE.USER,
        ROLE.ASSISTANT,
        ROLE.TOOL_RESULT,
        ROLE.USER,
    ]
