from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

from src.core.components.base.tool import BaseTool
from src.core.components.base.action import ActionResultDetail
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
