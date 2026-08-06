"""LLM 响应模块

提供 LLMResponse 类，统一处理流式和非流式响应。

LLMResponse 支持：
- await 模式：收集完整响应
- async for 模式：流式处理响应
- 自动追加响应到上下文
- 工具调用处理
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Self

from json_repair import repair_json

from .context_delivery import (
    ContextDeliveryExpectation,
    EffectiveContextReceipt,
)
from .exceptions import LLMContextError, LLMResponseConsumedError
from .model_client import StreamEvent
from .payload import LLMPayload, ReasoningText, Text, ToolCall
from .roles import ROLE
from .tool_call_compat import parse_tool_call_compat_response

if TYPE_CHECKING:
    from .context import LLMContextManager
    from .request import LLMRequest
    from .types import ModelSet


@dataclass(slots=True)
class LLMResponse:
    """LLMResponse：既可 await（收集全量）也可 async for（流式吐出）。"""

    _stream: AsyncIterator[StreamEvent] | None
    _upper: "LLMRequest | LLMResponse"
    _auto_append_response: bool

    payloads: list[LLMPayload]
    model_set: "ModelSet"
    context_manager: LLMContextManager | None = None

    message: str | None = None
    reasoning_content: str | None = None
    reasoning_parts: list[ReasoningText] | None = None
    call_list: list[ToolCall] | None = None
    tool_call_compat: bool = False
    request_record_id: int | None = None
    effective_context_receipts: dict[str, EffectiveContextReceipt] | None = None
    _on_complete: Callable[["LLMResponse", BaseException | None], None] | None = None
    _context_delivery_expectations: dict[
        str, ContextDeliveryExpectation
    ] | None = field(default=None, repr=False)

    _consumed: bool = False
    _appended_to_context: bool = False
    _inspector_response_attached: bool = False
    _completion_notified: bool = False

    def __post_init__(self) -> None:
        """确保 message 和 call_list 不为 None，方便后续处理；如果 context_manager 为空且上层存在，则继承上层的 context_manager。"""
        if self.call_list is None:
            self.call_list = []
        if self.effective_context_receipts is None:
            self.effective_context_receipts = {}
        if self._context_delivery_expectations is None:
            self._context_delivery_expectations = {}
        if self.reasoning_parts is None:
            self.reasoning_parts = []
        elif self.reasoning_content is None:
            self.reasoning_content = (
                "".join(part.text for part in self.reasoning_parts if isinstance(part.text, str)) or None
            )
        if self.context_manager is None:
            ctx = getattr(self._upper, "context_manager", None)
            if ctx:
                self.context_manager = ctx

    def register_context_delivery(
        self,
        delivery_id: str,
        expected_text: str,
        *,
        marker: str | None = None,
    ) -> Self:
        """Track one exact transient ``Text`` part for this response's next send."""

        expectation = ContextDeliveryExpectation.create(
            delivery_id,
            expected_text,
            marker=marker,
        )
        assert self._context_delivery_expectations is not None
        existing = self._context_delivery_expectations.get(expectation.delivery_id)
        if existing is not None and existing != expectation:
            raise ValueError(
                f"context delivery_id already registered: {expectation.delivery_id}"
            )
        self._context_delivery_expectations[expectation.delivery_id] = expectation
        return self

    def effective_context_receipt(
        self,
        delivery_id: str,
    ) -> EffectiveContextReceipt | None:
        """Return one content-free receipt from the final successful attempt."""

        receipts = self.effective_context_receipts or {}
        return receipts.get(str(delivery_id or "").strip())

    def _notify_complete(self, error: BaseException | None = None) -> None:
        """Notify the request owner once after a stream has been finalized."""
        if self._completion_notified or self._on_complete is None:
            return
        self._completion_notified = True
        try:
            self._on_complete(self, error)
        except Exception:
            # Observability callbacks must never alter response consumption.
            return

    def _maybe_apply_tool_call_compat(self) -> None:
        """如果启用了 tool_call_compat 模式且尚未解析过工具调用，则尝试从 message 中解析工具调用信息。"""
        if not self.tool_call_compat:
            return
        if self.call_list:
            return
        if not self.message:
            return

        parsed_message, parsed_calls = parse_tool_call_compat_response(self.message)
        self.message = parsed_message
        self.call_list = [
            ToolCall(id=call.get("id"), name=call.get("name", ""), args=call.get("args", {}))
            for call in parsed_calls
        ]

    def attach_to_inspector(self) -> None:
        """将当前轮响应挂回请求检视器，补齐 thinking / tool_calls 展示。"""
        if self._inspector_response_attached:
            return
        if not isinstance(self.request_record_id, int) or self.request_record_id <= 0:
            return

        response: dict[str, Any] = {"role": ROLE.ASSISTANT.value}
        if self.message is not None:
            response["content"] = self.message
        if self.reasoning_content:
            response["reasoning_content"] = self.reasoning_content
        if self.call_list:
            response["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(call.args, ensure_ascii=False, default=str),
                    },
                }
                for call in self.call_list
            ]

        if len(response) <= 1:
            return
        try:
            from src.kernel.llm.request_inspector import attach_response
            if attach_response(self.request_record_id, response):
                self._inspector_response_attached = True
        except Exception:
            return

    def __await__(self):
        """await 模式：收集完整响应，适用于需要一次性获取完整结果的场景。"""
        return self._collect_full_response().__await__()

    async def __aiter__(self):
        """async for 模式：流式处理响应，适用于需要边接收边处理的场景（如 UI 更新）。"""
        if self._consumed:
            raise LLMResponseConsumedError("Response has already been consumed.")
        self._consumed = True

        if self._stream is None:
            # 兼容非流式直接返回完整响应的情况，尝试解析工具调用信息后再返回文本内容
            self._maybe_apply_tool_call_compat()
            content = self.message or ""
            if content:
                yield content
            self._notify_complete(None)
            return

        full_content: list[str] = []
        tool_acc = _ToolCallAccumulator()
        reasoning_acc = _ReasoningBlockAccumulator()
        stream_error: Exception | None = None

        # 防止 streaming 响应卡死：stream 可能因网络/服务端问题进入 CLOSE-WAIT，
        # 导致 async for 永久阻塞。300s 总预算覆盖整个 stream 消费过程。
        STREAM_TOTAL_TIMEOUT = 300.0

        async def _consume_stream():
            nonlocal stream_error
            try:
                async for event in self._stream:
                    if event.text_delta:
                        full_content.append(event.text_delta)
                        yield event.text_delta
                    if event.reasoning_block_type or event.reasoning_delta or event.reasoning_signature_delta:
                        reasoning_acc.apply(event)
                    if event.tool_name or event.tool_args_delta or event.tool_call_id:
                        tool_acc.apply(event)
            except Exception as e:
                # 部分 provider/SDK 会在流尾抛出"连接关闭"等异常。
                # 先记录异常，确保已收集的内容能正确落库，再重新抛出。
                stream_error = e

        try:
            async with asyncio.timeout(STREAM_TOTAL_TIMEOUT):
                async for chunk in _consume_stream():
                    yield chunk
        except asyncio.TimeoutError:
            from .exceptions import LLMTimeoutError
            stream_error = LLMTimeoutError(f"Stream 消费超时 ({STREAM_TOTAL_TIMEOUT:.0f}s)")

        self.message = "".join(full_content)
        self.reasoning_parts = reasoning_acc.finalize() or self.reasoning_parts
        if self.reasoning_parts and not self.reasoning_content:
            self.reasoning_content = "".join(part.text for part in self.reasoning_parts) or None
        self.call_list = tool_acc.finalize()
        self._maybe_apply_tool_call_compat()
        self.attach_to_inspector()
        self._maybe_append_response_to_context()
        self._notify_complete(stream_error)

        if stream_error is not None:
            raise stream_error

    async def _collect_full_response(self) -> str:
        """收集完整响应，适用于需要一次性获取完整结果的场景。"""
        if self._consumed:
            raise LLMResponseConsumedError("Response has already been consumed.")
        self._consumed = True

        if self._stream is None:
            # 非流式直接返回完整响应的情况，尝试解析工具调用信息后再返回文本内容
            self._maybe_apply_tool_call_compat()
            self.attach_to_inspector()
            self._maybe_append_response_to_context()
            return self.message or ""

        # 流式处理的情况，收集完整文本并解析工具调用信息
        full_content: list[str] = []
        tool_acc = _ToolCallAccumulator()
        reasoning_acc = _ReasoningBlockAccumulator()
        stream_error: Exception | None = None

        # 防止 streaming 响应卡死：300s 总预算覆盖整个 stream 消费过程
        STREAM_TOTAL_TIMEOUT = 300.0

        try:
            async def _consume():
                async for event in self._stream:
                    if event.text_delta:
                        full_content.append(event.text_delta)
                    if event.reasoning_block_type or event.reasoning_delta or event.reasoning_signature_delta:
                        reasoning_acc.apply(event)
                    if event.tool_name or event.tool_args_delta or event.tool_call_id:
                        tool_acc.apply(event)

            await asyncio.wait_for(_consume(), timeout=STREAM_TOTAL_TIMEOUT)
        except asyncio.TimeoutError:
            from .exceptions import LLMTimeoutError
            stream_error = LLMTimeoutError(f"Stream 消费超时 ({STREAM_TOTAL_TIMEOUT:.0f}s)")
        except Exception as e:
            # 部分 provider/SDK 会在流尾抛出"连接关闭"等异常。
            # 先记录异常，确保已收集的内容能正确落库，再重新抛出。
            stream_error = e

        self.message = "".join(full_content)
        self.reasoning_parts = reasoning_acc.finalize() or self.reasoning_parts
        if self.reasoning_parts and not self.reasoning_content:
            self.reasoning_content = "".join(part.text for part in self.reasoning_parts) or None
        self.call_list = tool_acc.finalize()
        self._maybe_apply_tool_call_compat()
        self.attach_to_inspector()
        self._maybe_append_response_to_context()
        self._notify_complete(stream_error)

        if stream_error is not None:
            raise stream_error

        return self.message

    async def precollect_stream_for_non_stream(self) -> None:
        """预先收集强制流式响应，但不标记 consumed，也不追加到上下文。

        用于 LLMRequest.send(stream=False) 遇到 force_stream_mode 模型时，
        让流读取错误仍发生在请求重试循环内部。调用成功后，上层仍可正常
        ``await response``，此时会按非流式响应追加上下文。
        """
        if self._stream is None:
            return
        if self._consumed:
            raise LLMResponseConsumedError("Response has already been consumed.")

        full_content: list[str] = []
        tool_acc = _ToolCallAccumulator()
        reasoning_acc = _ReasoningBlockAccumulator()
        stream_error: Exception | None = None

        # 防止 streaming 响应卡死：300s 总预算
        STREAM_TOTAL_TIMEOUT = 300.0

        try:
            async def _consume():
                async for event in self._stream:
                    if event.text_delta:
                        full_content.append(event.text_delta)
                    if event.reasoning_block_type or event.reasoning_delta or event.reasoning_signature_delta:
                        reasoning_acc.apply(event)
                    if event.tool_name or event.tool_args_delta or event.tool_call_id:
                        tool_acc.apply(event)

            await asyncio.wait_for(_consume(), timeout=STREAM_TOTAL_TIMEOUT)
        except asyncio.TimeoutError:
            from .exceptions import LLMTimeoutError
            stream_error = LLMTimeoutError(f"Stream 消费超时 ({STREAM_TOTAL_TIMEOUT:.0f}s)")
        except Exception as e:
            stream_error = e

        self.message = "".join(full_content)
        self.reasoning_parts = reasoning_acc.finalize() or self.reasoning_parts
        if self.reasoning_parts and not self.reasoning_content:
            self.reasoning_content = "".join(part.text for part in self.reasoning_parts) or None
        self.call_list = tool_acc.finalize()
        self._maybe_apply_tool_call_compat()
        self._stream = None
        self._notify_complete(stream_error)

        if stream_error is not None:
            raise stream_error

    def _maybe_append_response_to_context(self) -> None:
        """如果启用了自动追加响应到上下文，并且当前响应有内容，则将其作为新的 ASSISTANT 消息追加到 payloads 中，供后续请求使用。"""
        if not self._auto_append_response:
            return

        self._append_current_response_payload()

    def _append_current_response_payload(self) -> None:
        """将当前响应对应的 assistant payload 写回上下文。"""
        if self._appended_to_context:
            return

        if not self._has_response_payload_content():
            return

        content_parts: list[object] = []
        if self.reasoning_parts:
            content_parts.extend(self.reasoning_parts)
        elif self.reasoning_content:
            content_parts.append(ReasoningText(self.reasoning_content))
        if self.message:
            content_parts.append(Text(self.message))
        if self.call_list:
            content_parts.extend(self.call_list)

        if not content_parts:
            return

        assistant_payload = LLMPayload(ROLE.ASSISTANT, content_parts)  # type: ignore[arg-type]
        if self.context_manager is not None:
            self.payloads = self.context_manager.add_payload(self.payloads, assistant_payload)
            self._appended_to_context = True
            return

        self.payloads.append(assistant_payload)
        self._maybe_apply_context_manager()
        self._appended_to_context = True

    def _has_response_payload_content(self) -> bool:
        """判断当前响应是否已有可写回上下文的内容。"""
        return bool(
            self.reasoning_parts
            or self.reasoning_content
            or self.message
            or self.call_list
        )

    def _maybe_apply_context_manager(self) -> None:
        """如果启用了上下文管理器，则尝试裁剪 payloads，以适应上下文限制。"""
        if not self.context_manager:
            return
        self.payloads = self.context_manager.maybe_trim(self.payloads)

    def to_payload(self) -> LLMPayload:
        """将当前响应转换为一个 LLMPayload 对象，适用于需要将响应作为消息追加到上下文中的场景。"""
        content_parts: list[object] = []
        if self.reasoning_parts:
            content_parts.extend(self.reasoning_parts)
        elif self.reasoning_content:
            content_parts.append(ReasoningText(self.reasoning_content))
        if self.message:
            content_parts.append(Text(self.message))
        if self.call_list:
            content_parts.extend(self.call_list)
        if not content_parts:
            content_parts.append(Text(""))
        return LLMPayload(ROLE.ASSISTANT, content_parts)  # type: ignore[arg-type]

    def add_payload(self, payload: "LLMPayload | LLMResponse", position=None) -> Self:
        """在当前响应的 payloads 中追加一个新的 payload，可以是一个 LLMPayload 对象，也可以是另一个 LLMResponse 对象（会被转换为 LLMPayload）。"""
        if isinstance(payload, LLMResponse):
            payload = payload.to_payload()

        if payload.role == ROLE.TOOL_RESULT and self.call_list:
            self._append_current_response_payload()

        if self.context_manager is not None:
            self.payloads = self.context_manager.add_payload(
                self.payloads,
                payload,
                position=int(position) if position is not None else None,
            )
            return self

        if position is not None:
            self.payloads.insert(int(position), payload)
        else:
            if self.payloads and self.payloads[-1].role == payload.role:
                self.payloads[-1].content.extend(payload.content)
            else:
                self.payloads.append(payload)
        self._maybe_apply_context_manager()
        return self

    def add_call_reflex(self, results: list[LLMPayload]) -> Self:
        """在当前响应的 payloads 中追加一个新的工具调用结果列表，适用于工具调用完成后需要将结果写回上下文的场景。"""
        if results and self.call_list:
            self._append_current_response_payload()

        if self.context_manager is not None:
            for payload in results:
                try:
                    self.payloads = self.context_manager.add_payload(self.payloads, payload)
                except LLMContextError:
                    continue
            return self

        for payload in results:
            self.payloads.append(payload)
        self._maybe_apply_context_manager()
        return self

    async def send(self, auto_append_response: bool = True, *, stream: bool = True) -> "LLMResponse":
        """
        将当前响应作为新的请求发送，适用于需要基于当前响应继续对话的场景。

        Args:
            auto_append_response: 是否自动将当前响应追加到上下文中，默认为 True。
            stream: 是否以流式方式发送请求，默认为 True。

        Returns:
            LLMResponse: 新的请求的响应对象。
        """
        if not self._consumed:
            await self._collect_full_response()

        if not self._appended_to_context:
            self.add_payload(self.to_payload())
            self._appended_to_context = True

        # 延迟导入，避免循环依赖
        from .request import LLMRequest

        # 创建一个新的 LLMRequest 对象，继承当前响应的 model_set 和 context_manager
        # 并将当前响应的 payloads 作为新的请求的初始 payloads，然后发送请求并返回响应对象
        req = LLMRequest(
            self.model_set,
            request_name=getattr(self._upper, "request_name", ""),
            context_manager=self.context_manager,
            trajectory_metadata=dict(
                getattr(self._upper, "trajectory_metadata", {}) or {}
            ),
            trace_id=getattr(self._upper, "trace_id", None),
            stream_id=getattr(self._upper, "stream_id", None),
            heartbeat_run_id=getattr(self._upper, "heartbeat_run_id", None),
            call_id=getattr(self._upper, "call_id", None),
        )
        req.payloads = list(self.payloads)
        pending = dict(self._context_delivery_expectations or {})
        req._context_delivery_expectations = pending
        response = await req.send(
            auto_append_response=auto_append_response,
            stream=stream,
        )
        if self._context_delivery_expectations is not None:
            self._context_delivery_expectations.clear()
        return response

    async def stream_with_callback(self, on_chunk: Callable[[str], Awaitable[None]]) -> str:
        """流式响应 + 实时回调。

        适用场景：需要在接收到每个 chunk 时立即执行某些操作（如 UI 更新）。

        Args:
            on_chunk: 异步回调函数，接收每个文本 chunk。

        Returns:
            完整的响应文本。

        Raises:
            LLMResponseConsumedError: 如果响应已被消费。
        """
        if self._consumed:
            raise LLMResponseConsumedError("Response has already been consumed.")
        self._consumed = True

        if self._stream is None:
            self._maybe_apply_tool_call_compat()
            content = self.message or ""
            if content:
                await on_chunk(content)
            self.attach_to_inspector()
            self._maybe_append_response_to_context()
            return content

        full_content: list[str] = []
        tool_acc = _ToolCallAccumulator()
        reasoning_acc = _ReasoningBlockAccumulator()
        stream_error: Exception | None = None
        try:
            async for event in self._stream:
                if event.text_delta:
                    full_content.append(event.text_delta)
                    await on_chunk(event.text_delta)
                if event.reasoning_block_type or event.reasoning_delta or event.reasoning_signature_delta:
                    reasoning_acc.apply(event)
                if event.tool_name or event.tool_args_delta or event.tool_call_id:
                    tool_acc.apply(event)
        except Exception as exc:
            stream_error = exc

        self.message = "".join(full_content)
        self.reasoning_parts = reasoning_acc.finalize() or self.reasoning_parts
        if self.reasoning_parts and not self.reasoning_content:
            self.reasoning_content = "".join(part.text for part in self.reasoning_parts) or None
        self.call_list = tool_acc.finalize()
        self._maybe_apply_tool_call_compat()
        self.attach_to_inspector()
        self._maybe_append_response_to_context()
        self._notify_complete(stream_error)
        if stream_error is not None:
            raise stream_error
        return self.message

    async def stream_with_buffer(self, buffer_size: int = 10) -> AsyncIterator[str]:
        """带缓冲的流式响应。

        适用场景：减少回调次数，累积多个 chunk 后再 yield。

        Args:
            buffer_size: 缓冲区大小（字符数），达到此大小后才 yield。

        Yields:
            缓冲后的文本块。

        Raises:
            LLMResponseConsumedError: 如果响应已被消费。
        """
        if self._consumed:
            raise LLMResponseConsumedError("Response has already been consumed.")
        self._consumed = True

        if self._stream is None:
            self._maybe_apply_tool_call_compat()
            content = self.message or ""
            if content:
                yield content
            self.attach_to_inspector()
            self._maybe_append_response_to_context()
            return

        full_content: list[str] = []
        buffer: list[str] = []
        buffer_len = 0
        tool_acc = _ToolCallAccumulator()
        reasoning_acc = _ReasoningBlockAccumulator()

        stream_error: Exception | None = None
        try:
            async for event in self._stream:
                if event.text_delta:
                    full_content.append(event.text_delta)
                    buffer.append(event.text_delta)
                    buffer_len += len(event.text_delta)

                    if buffer_len >= buffer_size:
                        buffered = "".join(buffer)
                        yield buffered
                        buffer.clear()
                        buffer_len = 0

                if event.reasoning_block_type or event.reasoning_delta or event.reasoning_signature_delta:
                    reasoning_acc.apply(event)
                if event.tool_name or event.tool_args_delta or event.tool_call_id:
                    tool_acc.apply(event)
        except Exception as e:
            # 有些 provider/SDK 会在流尾抛出“连接关闭”等异常。
            # 对于带 buffer 的消费方式，这会导致最后未 flush 的片段丢失。
            # 这里先记录异常，确保尾段 flush，再把异常抛出。
            stream_error = e

        # 剩余内容（无论正常结束还是异常结束，都尽量 flush）
        if buffer:
            yield "".join(buffer)

        self.message = "".join(full_content)
        self.reasoning_parts = reasoning_acc.finalize() or self.reasoning_parts
        if self.reasoning_parts and not self.reasoning_content:
            self.reasoning_content = "".join(part.text for part in self.reasoning_parts) or None
        self.call_list = tool_acc.finalize()
        self._maybe_apply_tool_call_compat()
        self.attach_to_inspector()
        self._maybe_append_response_to_context()
        self._notify_complete(stream_error)

        if stream_error is not None:
            raise stream_error


class _ToolCallAccumulator:
    """把 OpenAI 风格的 tool_call 增量拼成最终 ToolCall 列表。

    OpenAI 流式协议中，工具调用分多个 chunk 传输：
    - 首个 chunk：携带 tool_call_id + tool_name（以及可能的首段 args）
    - 后续 chunk：tool_call_id 可能为 None，仅携带 tool_args_delta

    因此需要追踪"当前活跃 id"，将无 id 的增量归属到最近一次出现的工具调用。
    """

    def __init__(self) -> None:
        self._by_id: dict[str, dict[str, Any]] = {}
        self._order: list[str] = []
        self._current_id: str | None = None  # 追踪最近一次有效的 tool_call_id

    def apply(self, event: StreamEvent) -> None:
        """处理一个新的 StreamEvent，更新工具调用的积累状态。"""
        # 优先使用事件携带的 id；若无则沿用上一次的 id（OpenAI 后续 chunk 不重复发送 id）
        effective_id = event.tool_call_id or self._current_id
        if not effective_id:
            # 既无新 id 又无历史 id，无法归属，丢弃
            return

        if effective_id not in self._by_id:
            self._by_id[effective_id] = {"id": effective_id, "name": None, "args": ""}
            self._order.append(effective_id)

        # 更新当前活跃 id
        if event.tool_call_id:
            self._current_id = event.tool_call_id

        rec = self._by_id[effective_id]
        if event.tool_name:
            rec["name"] = event.tool_name
        if event.tool_args_delta:
            rec["args"] = (rec.get("args") or "") + event.tool_args_delta

    def finalize(self) -> list[ToolCall]:
        """
        将当前积累的工具调用记录转换为 ToolCall 对象列表。
        """
        out: list[ToolCall] = []
        for tool_call_id in self._order:
            rec = self._by_id[tool_call_id]
            name = rec.get("name") or ""
            args_raw = rec.get("args") or ""
            args: dict[str, Any] | str
            if not args_raw:
                args = {}
            else:
                try:
                    args = json.loads(args_raw)
                except Exception:
                    try:
                        repaired = repair_json(args_raw, return_objects=True)
                    except Exception:
                        args = args_raw
                    else:
                        args = args_raw if repaired in ("", None) else repaired

            out.append(ToolCall(id=tool_call_id, name=name, args=args))
        return out


class _ReasoningBlockAccumulator:
    """把 Anthropic thinking/redacted_thinking 增量拼成最终 ReasoningText 列表。"""

    def __init__(self) -> None:
        self._current_type: str | None = None
        self._current_text: list[str] = []
        self._current_signature: str | None = None
        self._blocks: list[ReasoningText] = []

    def apply(self, event: StreamEvent) -> None:
        """处理一个新的 reasoning 相关流事件。"""
        if event.reasoning_block_type:
            self._flush_current()
            self._current_type = event.reasoning_block_type
            self._current_text = []
            self._current_signature = None

        if event.reasoning_delta:
            if self._current_type is None:
                self._current_type = "thinking"
            self._current_text.append(event.reasoning_delta)

        if event.reasoning_signature_delta:
            if self._current_type is None:
                self._current_type = "thinking"
            self._current_signature = (self._current_signature or "") + event.reasoning_signature_delta

    def finalize(self) -> list[ReasoningText]:
        """返回所有已累积的 reasoning block。"""
        self._flush_current()
        return list(self._blocks)

    def _flush_current(self) -> None:
        if self._current_type == "thinking":
            if self._current_text or self._current_signature:
                self._blocks.append(
                    ReasoningText(
                        "".join(self._current_text),
                        signature=self._current_signature,
                    )
                )
        elif self._current_type == "redacted_thinking" and self._current_signature:
            self._blocks.append(ReasoningText("", redacted_data=self._current_signature))

        self._current_type = None
        self._current_text = []
        self._current_signature = None
