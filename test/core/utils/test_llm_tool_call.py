from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

from src.core.components.base.action import ActionResultDetail
from src.core.components.base.tool import BaseTool
from src.core.utils.llm_tool_call import (
    ToolCallExecutionResult,
    run_llm_usable_executions,
    run_tool_call,
)
from src.kernel.llm import LLMUsableExecution, ToolCall, ToolRegistry


class _FakeResponse:
    def __init__(self) -> None:
        self.payloads: list[Any] = []

    def add_payload(self, payload: Any, position: object = None) -> None:
        _ = position
        self.payloads.append(payload)


async def test_ready_executions_resume_in_call_order() -> None:
    events: list[str] = []

    async def first():
        events.append("first-prepare")
        yield None
        await asyncio.sleep(0.01)
        events.append("first-final")
        yield (True, "first")

    async def second():
        events.append("second-prepare")
        yield None
        events.append("second-final")
        yield (True, "second")

    executions = [LLMUsableExecution(first()), LLMUsableExecution(second())]
    await run_llm_usable_executions(executions)

    assert events.index("second-prepare") < events.index("first-final")
    assert events.index("first-final") < events.index("second-final")
    assert [execution.result for execution in executions] == [
        (True, "first"),
        (True, "second"),
    ]


async def test_run_tool_call_runs_concurrently_and_appends_in_call_order() -> None:
    events: list[str] = []

    class SlowTool(BaseTool):
        tool_name = "slow"
        tool_description = "slow"

        async def execute(self) -> tuple[bool, str]:
            events.append("slow-start")
            await asyncio.sleep(0.02)
            events.append("slow-done")
            return True, "slow"

    class FastTool(BaseTool):
        tool_name = "fast"
        tool_description = "fast"

        async def execute(self) -> tuple[bool, str]:
            events.append("fast-start")
            events.append("fast-done")
            return True, "fast"

    registry = ToolRegistry()
    registry.register(SlowTool)
    registry.register(FastTool)
    response = _FakeResponse()

    result = await run_tool_call(
        calls=[
            ToolCall(id="1", name="tool-slow", args={}),
            ToolCall(id="2", name="tool-fast", args={}),
        ],
        response=response,
        usable_map=registry,
        trigger_msg=SimpleNamespace(message_id="m1"),
        plugin=MagicMock(),
        stream_id="s1",
    )

    assert result == [(True, True), (True, True)]
    assert events.index("fast-done") < events.index("slow-done")
    assert [payload.content[0].value for payload in response.payloads] == [
        "slow",
        "fast",
    ]


async def test_run_tool_call_binds_tool_runtime_stream_context() -> None:
    class StreamAwareTool(BaseTool):
        tool_name = "stream_aware"
        tool_description = "stream aware"

        async def execute(self) -> tuple[bool, str]:
            return True, self.get_current_stream_id()

    registry = ToolRegistry()
    registry.register(StreamAwareTool)
    response = _FakeResponse()

    result = await run_tool_call(
        calls=[ToolCall(id="1", name="tool-stream_aware", args={})],
        response=response,
        usable_map=registry,
        trigger_msg=SimpleNamespace(message_id="m1", stream_id="message-stream"),
        plugin=MagicMock(),
        stream_id="fallback-stream",
    )

    assert result == [(True, True)]
    assert response.payloads[0].content[0].value == "message-stream"


async def test_run_tool_call_preserves_structured_result_for_canonical_json() -> None:
    class StructuredTool(BaseTool):
        tool_name = "structured"
        tool_description = "structured"

        async def execute(self) -> tuple[bool, dict[str, Any]]:
            return True, {"schema": "example.v1", "items": [{"value": "爱莉"}]}

    registry = ToolRegistry()
    registry.register(StructuredTool)
    response = _FakeResponse()

    result = await run_tool_call(
        calls=[ToolCall(id="1", name="tool-structured", args={})],
        response=response,
        usable_map=registry,
        trigger_msg=SimpleNamespace(message_id="m1"),
        plugin=MagicMock(),
        stream_id="s1",
    )

    assert result == [(True, True)]
    tool_result = response.payloads[0].content[0]
    assert isinstance(tool_result.value, dict)
    assert tool_result.to_text() == '{"schema": "example.v1", "items": [{"value": "爱莉"}]}'


async def test_run_tool_call_preserves_technical_outcome_without_breaking_tuple_contract() -> None:
    class UnknownDeliveryTool(BaseTool):
        tool_name = "unknown_delivery"
        tool_description = "unknown delivery"

        async def execute(self) -> tuple[bool, str]:
            return False, ActionResultDetail(
                "投递状态未知",
                technical_outcome="delivery_unknown",
            )

    registry = ToolRegistry()
    registry.register(UnknownDeliveryTool)
    response = _FakeResponse()

    results = await run_tool_call(
        calls=[ToolCall(id="unknown-1", name="tool-unknown_delivery", args={})],
        response=response,
        usable_map=registry,
        trigger_msg=SimpleNamespace(message_id="m1"),
        plugin=MagicMock(),
        stream_id="s1",
    )

    assert results == [(True, False)]
    assert isinstance(results[0], ToolCallExecutionResult)
    assert results[0].technical_outcome == "delivery_unknown"
    assert response.payloads[0].content[0].value == "执行失败: 投递状态未知"


async def test_run_tool_call_propagates_exact_delivery_receipt() -> None:
    class DeliveredTool(BaseTool):
        tool_name = "delivered"
        tool_description = "delivered"

        async def execute(self) -> tuple[bool, str]:
            return True, ActionResultDetail(
                "已送达",
                technical_outcome="delivered",
                delivery_receipt_sha256="f" * 64,
                delivery_message_id="message:tool-delivered",
                delivery_proof_status="durable",
            )

    registry = ToolRegistry()
    registry.register(DeliveredTool)
    response = _FakeResponse()

    results = await run_tool_call(
        calls=[ToolCall(id="delivered-1", name="tool-delivered", args={})],
        response=response,
        usable_map=registry,
        trigger_msg=SimpleNamespace(message_id="m1"),
        plugin=MagicMock(),
        stream_id="s1",
    )

    assert results == [(True, True)]
    assert results[0].technical_outcome == "delivered"
    assert results[0].delivery_receipt_sha256 == "f" * 64
    assert results[0].delivery_message_id == "message:tool-delivered"
    assert results[0].delivery_proof_status == "durable"


async def test_tool_call_name_prefix_fallback_accepts_bare_name() -> None:
    """工具名前缀容错：模型用裸名调用（注册名是 tool-{name}）也能命中执行。

    真实缺陷（2026-08-12）：BaseTool 注册名统一带 tool- 前缀，模型记混时用
    裸名/前缀名调用都会报"未知的工具"；容错做双向归一（剥前缀/补前缀再查）。
    """
    executed: list[str] = []

    class GuardedTool(BaseTool):
        tool_name = "guarded_op"
        tool_description = "guarded"

        async def execute(self) -> tuple[bool, str]:
            executed.append("guarded_op")
            return True, "guarded-ok"

    registry = ToolRegistry()
    registry.register(GuardedTool)  # 注册 key = "tool-guarded_op"（BaseTool schema 名）
    response = _FakeResponse()

    # 裸名调用（缺少 tool- 前缀）→ 容错补前缀命中
    result = await run_tool_call(
        calls=[ToolCall(id="1", name="guarded_op", args={})],
        response=response,
        usable_map=registry,
        trigger_msg=SimpleNamespace(message_id="m1"),
        plugin=MagicMock(),
        stream_id="s1",
    )

    assert result == [(True, True)]
    assert executed == ["guarded_op"]
    assert response.payloads[0].content[0].value == "guarded-ok"


async def test_tool_call_name_prefix_fallback_accepts_prefixed_name() -> None:
    """工具名前缀容错：带 tool- 前缀的正常调用不受影响（直接命中）。"""
    registry = ToolRegistry()

    class PrefixedTool(BaseTool):
        tool_name = "prefixed_op"
        tool_description = "prefixed"

        async def execute(self) -> tuple[bool, str]:
            return True, "prefixed-ok"

    registry.register(PrefixedTool)
    response = _FakeResponse()

    result = await run_tool_call(
        calls=[ToolCall(id="1", name="tool-prefixed_op", args={})],
        response=response,
        usable_map=registry,
        trigger_msg=SimpleNamespace(message_id="m1"),
        plugin=MagicMock(),
        stream_id="s1",
    )

    assert result == [(True, True)]
    assert response.payloads[0].content[0].value == "prefixed-ok"
