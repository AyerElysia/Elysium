"""LLM 请求模块

提供 LLMRequest 类，用于构建和执行 LLM 请求。

LLMRequest 支持：
- 构建 LLMPayload 列表
- 负载均衡和重试策略
- 指标收集
- 流式和非流式响应
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import time
from dataclasses import dataclass, field
from typing import Any, Self

from src.kernel.llm.payload.tooling import LLMUsable
from src.kernel.logger import get_logger

from .context import LLMContextManager
from .context_delivery import (
    ContextDeliveryExpectation,
    build_effective_context_receipts,
)
from .exceptions import (
    LLMAPIError,
    LLMConfigurationError,
    LLMRateLimitError,
    LLMTimeoutError,
    UnsupportedModalityError,
    classify_exception,
)
from .media_capabilities import (
    extract_media_refs,
    filter_model_set_for_media,
    normalize_media_capabilities,
)
from .model_client import ModelClientRegistry, get_default_model_client_registry
from .monitor import RequestMetrics, RequestTimer, get_global_collector
from .payload import (
    Content,
    LLMPayload,
    MediaPart,
    ReasoningText,
    Text,
    ToolCall,
    ToolResult,
)
from .policy import create_default_policy
from .policy.base import Policy
from .response import LLMResponse
from .roles import ROLE
from .token_counter import count_payload_tokens
from .trajectory_collector import record_trajectory
from .trajectory_types import (
    derive_task_tags,
    new_trajectory_id,
    sanitize_text_only,
    utc_timestamp,
)
from .types import ModelEntry, ModelSet, RequestType, redact_secret

logger = get_logger("kernel.llm.request", display="LLM 请求")
_monotonic = time.monotonic


def _new_attempt_deadline(timeout_seconds: object) -> float | None:
    """Return one monotonic deadline for all transport phases of an attempt."""
    if isinstance(timeout_seconds, (int, float)) and timeout_seconds > 0:
        return _monotonic() + float(timeout_seconds)
    return None


def _remaining_attempt_timeout(deadline: float | None) -> float | None:
    """Return remaining attempt budget, failing before a new phase if exhausted."""
    if deadline is None:
        return None
    remaining = deadline - _monotonic()
    if remaining <= 0:
        raise asyncio.TimeoutError
    return remaining


def _trajectory_settings() -> tuple[bool, str, float, int, int, int]:
    """Read trajectory settings without importing core config at module load time."""
    try:
        from src.core.config import get_core_config

        section = get_core_config().llm
        return (
            bool(section.enable_trajectory_logging),
            str(section.trajectory_base_path),
            float(section.trajectory_flush_interval),
            int(section.trajectory_queue_limit),
            int(section.trajectory_raw_retention_days),
            int(section.trajectory_archive_retention_days),
        )
    except Exception:
        # Kernel LLM tests and low-level callers may intentionally run before
        # core initialization; the documented defaults remain usable there.
        return True, "data/training_data_lake", 5.0, 10000, 3, 0


def _safe_related_id(request: Any, name: str) -> str | None:
    """Read an optional association ID from the request or its context manager."""
    for source in (request, getattr(request, "context_manager", None)):
        if source is None:
            continue
        try:
            value = getattr(source, name, None)
        except Exception:
            value = None
        if value is not None and str(value).strip():
            return str(value)
    return None


def _media_placeholder(part: MediaPart) -> str:
    kind = getattr(part, "kind", None)
    kind_value = getattr(kind, "value", kind)
    normalized = str(kind_value or "file").lower()
    if normalized not in {"image", "audio", "video", "file"}:
        normalized = "file"
    return f"[{normalized}]"


def _safe_tool_arguments(arguments: Any) -> str:
    sanitized = sanitize_text_only(arguments)
    if isinstance(sanitized, str):
        return sanitized
    try:
        return json.dumps(sanitized, ensure_ascii=False, separators=(",", ":"))
    except Exception:
        return str(sanitized)


def _safe_tool_result_value(value: Any) -> str:
    sanitized = sanitize_text_only(value)
    if isinstance(sanitized, str):
        return sanitized
    try:
        return json.dumps(sanitized, ensure_ascii=False, separators=(",", ":"))
    except Exception:
        return str(sanitized)


def _serialize_tool_call(call: ToolCall) -> dict[str, Any]:
    return {
        "id": sanitize_text_only(call.id),
        "type": "function",
        "function": {
            "name": sanitize_text_only(call.name),
            "arguments": _safe_tool_arguments(call.args),
        },
    }


def _serialize_payloads(payloads: list[LLMPayload]) -> list[dict[str, Any]]:
    """Serialize payloads to text-only OpenAI-like messages."""
    messages: list[dict[str, Any]] = []
    for payload in payloads:
        role = getattr(payload.role, "value", str(payload.role))
        role_text = str(role)
        text_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        tool_results: list[dict[str, Any]] = []

        for part in payload.content:
            if isinstance(part, Text):
                text_parts.append(str(sanitize_text_only(part.text)))
            elif isinstance(part, ReasoningText):
                if part.text:
                    text_parts.append(str(sanitize_text_only(part.text)))
                elif part.redacted_data:
                    text_parts.append("[reasoning]")
            elif isinstance(part, MediaPart):
                text_parts.append(_media_placeholder(part))
            elif isinstance(part, ToolCall):
                tool_calls.append(_serialize_tool_call(part))
            elif isinstance(part, ToolResult):
                tool_results.append(
                    {
                        "tool_call_id": sanitize_text_only(part.call_id),
                        "name": sanitize_text_only(part.name),
                        "content": _safe_tool_result_value(part.value),
                    }
                )
            else:
                # Tool declarations are sent separately to providers. Keep a
                # text-only marker in the trajectory without serializing object
                # internals or credentials from a tool implementation.
                if role_text == ROLE.TOOL.value:
                    text_parts.append("[tool]")
                else:
                    text_parts.append(str(sanitize_text_only(part)))

        if tool_results or role_text == ROLE.TOOL_RESULT.value:
            if tool_results:
                for result in tool_results:
                    message: dict[str, Any] = {
                        "role": "tool",
                        "content": result["content"],
                    }
                    if result["tool_call_id"] is not None:
                        message["tool_call_id"] = result["tool_call_id"]
                    if result["name"] is not None:
                        message["name"] = result["name"]
                    messages.append(message)
            else:
                messages.append({"role": "tool", "content": " ".join(text_parts)})
            continue

        message = {
            "role": role_text,
            "content": " ".join(text_parts),
        }
        if tool_calls:
            message["tool_calls"] = tool_calls
        messages.append(message)
    return messages


def _serialize_tool_results(payloads: list[LLMPayload]) -> list[dict[str, Any]]:
    """Extract text-only tool results for the trajectory top-level field."""
    results: list[dict[str, Any]] = []
    for payload in payloads:
        for part in payload.content:
            if not isinstance(part, ToolResult):
                continue
            results.append(
                {
                    "call_id": sanitize_text_only(part.call_id),
                    "name": sanitize_text_only(part.name),
                    "content": _safe_tool_result_value(part.value),
                }
            )
    return results


def _tool_result_references(payloads: list[LLMPayload]) -> list[dict[str, Any]]:
    """Build content-free identities for tool results delivered in this attempt."""

    references: list[dict[str, Any]] = []
    for payload in payloads:
        for part in payload.content:
            if not isinstance(part, ToolResult):
                continue
            # Hash the exact text representation providers receive.  The
            # trajectory body is sanitized separately; this extension keeps
            # only content-free identity and size.
            rendered = part.to_text()
            encoded = rendered.encode("utf-8")
            reference: dict[str, Any] = {
                "call_id": sanitize_text_only(part.call_id),
                "name": sanitize_text_only(part.name),
                "sha256": hashlib.sha256(encoded).hexdigest(),
                "utf8_bytes": len(encoded),
            }
            try:
                structured = json.loads(rendered)
            except (TypeError, ValueError, json.JSONDecodeError):
                structured = None
            if isinstance(structured, dict):
                for key in (
                    "schema",
                    "delivery_id",
                    "projection_sha256",
                    "delivered_bytes",
                ):
                    value = structured.get(key)
                    if isinstance(value, (str, int)):
                        reference[key] = value
            references.append(reference)
    return references


def _serialize_response(response: LLMResponse) -> dict[str, Any]:
    """Serialize the response fields that were actually produced."""
    output: dict[str, Any] = {
        "content": sanitize_text_only(response.message),
    }
    if response.reasoning_content:
        output["reasoning_content"] = sanitize_text_only(response.reasoning_content)
    if response.call_list:
        output["tool_calls"] = [
            _serialize_tool_call(call) for call in response.call_list
        ]
    return output


def _normalize_tool_result_payload(payload: LLMPayload) -> LLMPayload:
    """
    规范化 TOOL_RESULT payload，确保内容中的 ToolResult 对象被保留，其他非 Text 对象被转换为 Text。
    """
    if payload.role != ROLE.TOOL_RESULT:
        return payload

    # 允许 ToolResult 或任意对象。
    # 重要：ToolResult 需要保留 call_id（用于 OpenAI tool message 的 tool_call_id）。
    out_content: list[Any] = []
    for part in payload.content:
        if isinstance(part, ToolResult):
            out_content.append(part)
        elif isinstance(part, Text):
            out_content.append(part)
        elif isinstance(part, Content):
            # 保留媒体 Content，让 provider 在其不支持时显式拒绝；
            # 不能把媒体的 repr 静默变成工具结果文本。
            out_content.append(part)
        else:
            out_content.append(Text(str(part)))

    return LLMPayload(ROLE.TOOL_RESULT, out_content)  # type: ignore[arg-type]


def _extract_tools(payloads: list[LLMPayload]) -> list[LLMUsable]:
    """从 payloads 中提取所有 TOOL 角色的 LLMUsable 对象，供 provider 端调用工具时使用。"""
    tools: list[LLMUsable] = []
    for payload in payloads:
        if payload.role != ROLE.TOOL:
            continue
        for part in payload.content:
            # TOOL payload 允许传入工具类（type）或工具实例。
            # 这里显式兼容两种形式，避免仅依赖 Protocol 的 isinstance 细节。
            if isinstance(part, type):
                if issubclass(part, LLMUsable):
                    tools.append(part)
                continue

            if isinstance(part, LLMUsable):
                tools.append(part)
    return tools


def _normalize_client_create_result(
    result: tuple[Any, ...],
) -> tuple[
    str | None,
    list[dict[str, Any]] | None,
    Any,
    str | list[ReasoningText] | None,
    int | None,
]:
    """兼容 provider client 的 3/4/5 元组返回格式。"""
    if len(result) == 5:
        message, tool_calls, stream_iter, reasoning_content, request_record_id = result
        return message, tool_calls, stream_iter, reasoning_content, request_record_id

    if len(result) == 4:
        message, tool_calls, stream_iter, reasoning_content = result
        return message, tool_calls, stream_iter, reasoning_content, None

    if len(result) == 3:
        message, tool_calls, stream_iter = result
        return message, tool_calls, stream_iter, None, None

    raise ValueError(
        "client.create 必须返回长度为 3、4 或 5 的元组："
        "(message, tool_calls, stream_iter[, reasoning_content[, request_record_id]])"
    )


def _split_reasoning_result(
    reasoning_result: str | list[ReasoningText] | None,
) -> tuple[str | None, list[ReasoningText] | None]:
    """将 provider 返回的 reasoning 结果拆分为文本摘要和结构化 block。"""
    if isinstance(reasoning_result, list):
        text = (
            "".join(
                part.text for part in reasoning_result if isinstance(part.text, str)
            )
            or None
        )
        return text, reasoning_result
    return reasoning_result, None


@dataclass(slots=True)
class LLMRequest:
    """LLMRequest：构建 payload 并执行请求。"""

    model_set: ModelSet
    request_name: str = ""
    trajectory_metadata: dict[str, Any] = field(default_factory=dict)
    trace_id: str | None = field(default=None, kw_only=True)
    stream_id: str | None = field(default=None, kw_only=True)
    heartbeat_run_id: str | None = field(default=None, kw_only=True)
    call_id: str | None = field(default=None, kw_only=True)

    payloads: list[LLMPayload] = field(default_factory=list)
    policy: Policy | None = None
    clients: ModelClientRegistry | None = None
    context_manager: LLMContextManager | None = None
    enable_metrics: bool = True  # 是否启用指标收集
    request_type: RequestType = RequestType.COMPLETIONS
    _context_delivery_expectations: dict[str, ContextDeliveryExpectation] = field(
        default_factory=dict, repr=False
    )

    def __post_init__(self) -> None:
        if self.payloads is None:
            self.payloads = []
        if self.policy is None:
            self.policy = create_default_policy()
        if self.clients is None:
            self.clients = get_default_model_client_registry()
        if self.context_manager is None:
            self.context_manager = LLMContextManager()

    def add_payload(self, payload: LLMPayload, position=None) -> Self:
        """
        添加一个新的 LLMPayload 到请求中。

        Args:
            payload: 要添加的 LLMPayload 对象。
            position: 可选的插入位置，如果为 None，则添加到末尾。

        Returns:
            Self: 返回当前 LLMRequest 实例，支持链式调用。
        """
        if self.context_manager is not None:
            self.payloads = self.context_manager.add_payload(
                self.payloads,
                payload,
                position=int(position) if position is not None else None,
            )
            return self

        if position is not None:
            self.payloads.insert(int(position), payload)
            return self

        if self.payloads and self.payloads[-1].role == payload.role:
            self.payloads[-1].content.extend(payload.content)
        else:
            self.payloads.append(payload)
        return self

    def register_context_delivery(
        self,
        delivery_id: str,
        expected_text: str,
        *,
        marker: str | None = None,
        part_kind: str = "text",
    ) -> Self:
        """Track one exact transient ``Text`` part for the next successful send."""

        expectation = ContextDeliveryExpectation.create(
            delivery_id,
            expected_text,
            marker=marker,
            part_kind=part_kind,
        )
        existing = self._context_delivery_expectations.get(expectation.delivery_id)
        if existing is not None and existing != expectation:
            raise ValueError(
                f"context delivery_id already registered: {expectation.delivery_id}"
            )
        self._context_delivery_expectations[expectation.delivery_id] = expectation
        return self

    def _compute_effective_context_budget(self, model: ModelEntry) -> int | None:
        """计算在考虑上下文保留策略后的有效上下文预算。

        1. 从 model.max_context 获取模型的最大上下文长度。
        2. 从 model.extra_params 中获取 context_reserve_tokens 和 context_reserve_ratio。
        3. 计算固定保留和比例保留，取两者的最大值作为总保留。
        4. 有效预算 = max_context - reserve，确保至少为 1。
        """
        # 验证 max_context
        max_context = model.get("max_context")
        if not isinstance(max_context, int) or max_context <= 0:
            return None

        # 验证 extra_params
        extra_params = model.get("extra_params")
        if not isinstance(extra_params, dict):
            extra_params = {}

        # 计算保留的上下文长度
        reserve_tokens = extra_params.get("context_reserve_tokens")
        configured_reserve = (
            reserve_tokens
            if isinstance(reserve_tokens, int) and reserve_tokens > 0
            else 0
        )
        output_reserve = model.get("max_tokens")
        fixed_reserve = max(
            configured_reserve,
            output_reserve
            if isinstance(output_reserve, int) and output_reserve > 0
            else 0,
        )

        # 计算比例保留的上下文长度
        reserve_ratio = extra_params.get("context_reserve_ratio")
        ratio = 0.0
        if isinstance(reserve_ratio, (int, float)):
            ratio = max(0.0, float(reserve_ratio))
        ratio_reserve = int(math.floor(max_context * ratio))

        # 取固定保留和比例保留的最大值作为总保留
        reserve = max(fixed_reserve, ratio_reserve)

        # 计算有效预算
        effective_budget = max_context - reserve
        return effective_budget if effective_budget > 0 else 1

    def _compute_context_compression_trigger(self, model: ModelEntry) -> int | None:
        """计算当前模型的上下文压缩触发阈值。

        生产任务从 ``tasks.<name>.context_tokens`` 取得统一输入预算，并随
        路由条目携带。模型只提供硬 ``max_context``；最终预算取任务预算与
        扣除输出保留量后的模型硬上限的较小值。显式单模型调用没有任务
        身份时，退回模型硬上限。
        """

        effective_budget = self._compute_effective_context_budget(model)
        if effective_budget is None:
            return None

        task_budget = model.get("context_tokens")
        if isinstance(task_budget, int) and task_budget > 0:
            return min(task_budget, effective_budget)

        return effective_budget

    def _maybe_trim_payloads_for_model(
        self, payloads: list[LLMPayload], model: ModelEntry
    ) -> list[LLMPayload]:
        """
        根据模型的上下文触发阈值，裁剪/压缩 payloads 以适应当前模型。
        """
        if not self.context_manager:
            return payloads

        budget = self._compute_context_compression_trigger(model)
        model_identifier = model.get("model_identifier")

        if (
            budget is None
            or not isinstance(model_identifier, str)
            or not model_identifier
        ):
            return payloads

        try:
            # 未达到触发阈值时不做任何历史裁剪，保持长生命周期上下文完整。
            if (
                count_payload_tokens(payloads, model_identifier=model_identifier)
                <= budget
            ):
                return payloads
        except RuntimeError:
            return payloads

        def token_counter(items: list[LLMPayload]) -> int:
            """
            计算给定 payloads 的 token 数量。
            """
            try:
                return count_payload_tokens(items, model_identifier=model_identifier)
            except RuntimeError:
                return 0

        return self.context_manager.maybe_trim(
            payloads,
            max_token_budget=budget,
            token_counter=token_counter,
        )

    async def send(
        self, auto_append_response: bool = True, *, stream: bool = True
    ) -> LLMResponse:
        """
        发送请求并返回响应。

        Args:
            auto_append_response: 是否自动将响应消息追加到 payloads 中，默认为 True。
            stream: 是否使用流式响应，默认为 True。
        Returns:
            LLMResponse: 包含响应消息和工具调用信息的对象。
        """
        model_set = _validate_model_set(self.model_set)
        request_id = new_trajectory_id("req")
        trace_id = _safe_related_id(self, "trace_id") or request_id
        stream_id = _safe_related_id(self, "stream_id")
        heartbeat_run_id = _safe_related_id(self, "heartbeat_run_id")
        call_id = _safe_related_id(self, "call_id")
        task_name = self.request_name or "llm"
        task_tags = derive_task_tags(task_name)
        (
            trajectory_enabled,
            trajectory_base_path,
            trajectory_flush_interval,
            trajectory_queue_limit,
            trajectory_raw_retention_days,
            trajectory_archive_retention_days,
        ) = _trajectory_settings()
        previous_attempt_id: str | None = None
        attempt_index = 0

        # Caller-supplied metadata is sanitized by the collector before persistence.
        request_metadata = dict(self.trajectory_metadata or {})
        request_metadata.setdefault(
            "request_type",
            self.request_type.value
            if hasattr(self.request_type, "value")
            else str(self.request_type),
        )

        def record_attempt(
            *,
            attempt_id: str,
            parent_attempt_id: str | None,
            attempt_number: int,
            model: dict[str, Any],
            step_meta: dict[str, Any] | None,
            trimmed: list[LLMPayload],
            latency_s: float,
            success: bool,
            response: Any = None,
            usage: dict[str, Any] | None = None,
            error: BaseException | None = None,
            response_obj: LLMResponse | None = None,
        ) -> None:
            """Persist one durable attempt event without affecting the request."""
            event: dict[str, Any] = {
                "trace_id": trace_id,
                "attempt_id": attempt_id,
                "request_id": request_id,
                "parent_attempt_id": parent_attempt_id,
                "timestamp": utc_timestamp(),
                "request_name": self.request_name,
                "task_name": task_name,
                "task_tags": task_tags,
                "stream_id": stream_id,
                "heartbeat_run_id": heartbeat_run_id,
                "call_id": call_id,
                "model": model.get("model_identifier"),
                "model_identifier": model.get("model_identifier"),
                "api_provider": model.get("api_provider"),
                "policy_meta": step_meta or {},
                "messages": _serialize_payloads(trimmed),
                "response": (
                    _serialize_response(response_obj)
                    if response_obj is not None
                    else sanitize_text_only(response)
                ),
                "tool_results": _serialize_tool_results(trimmed),
                "usage": usage or {},
                "latency_s": latency_s,
                "success": success,
                "error": str(error) if error is not None else None,
                "error_type": type(error).__name__ if error is not None else None,
                "metadata": {
                    **request_metadata,
                    "request_id": request_id,
                    "attempt_id": attempt_id,
                    "attempt_index": attempt_number,
                    "policy_meta": step_meta or {},
                    "request_name": self.request_name,
                    "task_name": task_name,
                    "task_tags": task_tags,
                    "stream_id": stream_id,
                    "heartbeat_run_id": heartbeat_run_id,
                    "retry_count": attempt_number - 1,
                    "model_index": (step_meta or {}).get("model_index", 0),
                    "stream": stream,
                },
                "extensions": {
                    "tool_result_refs": _tool_result_references(trimmed),
                },
            }
            if response_obj is not None and response_obj.call_list:
                event["metadata"]["call_ids"] = [
                    sanitize_text_only(item.id) for item in response_obj.call_list
                ]
            record_trajectory(
                event,
                base_path=trajectory_base_path,
                enabled=trajectory_enabled,
                flush_interval=trajectory_flush_interval,
                queue_limit=trajectory_queue_limit,
                raw_retention_days=trajectory_raw_retention_days,
                archive_retention_days=trajectory_archive_retention_days,
            )

        # TOOL_RESULT payload 规范化（确保 provider 端可读）
        payloads = [_normalize_tool_result_payload(p) for p in self.payloads]
        tools = _extract_tools(payloads)

        # 在创建策略会话之前按已验证媒体过滤模型，避免重试链将同一
        # 多模态请求交给声明为 text-only 的模型。
        media_refs = extract_media_refs(payloads)
        if media_refs:
            compatible_model_set = filter_model_set_for_media(model_set, media_refs)
            if not compatible_model_set:
                requested_modalities = ", ".join(
                    sorted({media.kind.value for media in media_refs})
                )
                raise UnsupportedModalityError(
                    f"没有模型支持本次请求的媒体模态: {requested_modalities}"
                )
            model_set = compatible_model_set  # type: ignore[assignment]

        # 创建策略会话
        assert self.policy is not None
        session = self.policy.new_session(
            model_set=model_set, request_name=self.request_name
        )

        last_error: BaseException | None = None
        retry_count = 0
        step = session.first()
        if step.model is not None:
            selected_model = str(step.model.get("model_identifier") or "<unknown>")
            configured_primary = str(
                model_set[0].get("model_identifier") or "<unknown>"
            )
            skipped_cooling = tuple(
                str(item) for item in step.meta.get("cooldown_skipped", ())
            )
            route_context = (
                f"task={step.meta.get('routing_task') or '<direct>'}, "
                f"snapshot={step.meta.get('routing_snapshot') or '<none>'}, "
                f"configured_primary={configured_primary}, "
                f"selected={selected_model}, "
                f"configured_priority={step.meta.get('routing_priority', 0)}"
            )
            if skipped_cooling:
                logger.info(
                    "LLM 路由已跳过冷却模型: "
                    f"request={self.request_name or '__default__'}, "
                    f"{route_context}, skipped={list(skipped_cooling)}"
                )
            else:
                logger.debug(
                    "LLM 路由已选择: "
                    f"request={self.request_name or '__default__'}, "
                    f"{route_context}"
                )

        # 循环直到找到可用模型或耗尽重试机会
        while step.model is not None:
            model = _validate_model_entry(step.model)
            attempt_index += 1
            attempt_id = new_trajectory_id("attempt")
            parent_attempt_id = previous_attempt_id
            attempt_state = {"recorded": False}
            attempt_started = time.perf_counter()

            model_identifier = model.get("model_identifier")
            if not isinstance(model_identifier, str) or not model_identifier:
                raise LLMConfigurationError("model.model_identifier 必须是非空字符串")

            # 如果当前步骤配置了 delay_seconds，则在发送请求前等待指定的时间（用于实现请求节流或冷却机制）
            if step.delay_seconds and step.delay_seconds > 0:
                await asyncio.sleep(step.delay_seconds)

            # 根据当前模型的上下文限制和保留策略，裁剪 payloads 以适应当前模型
            # 注意：裁剪结果仅用于本次请求，不回写 self.payloads，避免重试时基于已裁剪的结果再裁剪
            trimmed_payloads = self._maybe_trim_payloads_for_model(payloads, model)

            # 严格上下文校验：不允许带着不完整/不合法的 tool 链路发起请求。
            # 该错误属于“本地逻辑错误”，不应进入重试链。
            if self.context_manager is not None:
                self.context_manager.validate_for_send(list(trimmed_payloads))

            assert self.clients is not None
            client = self.clients.get_client_for_model(model)

            # 开始计时
            timer = RequestTimer()

            try:
                with timer:
                    timeout_seconds = model.get("timeout")
                    force_stream_mode = bool(model.get("force_stream_mode", False))
                    effective_stream = stream or force_stream_mode
                    attempt_deadline = _new_attempt_deadline(timeout_seconds)
                    create_timeout = _remaining_attempt_timeout(attempt_deadline)
                    create_awaitable = client.create(
                        model_name=model_identifier,
                        payloads=trimmed_payloads,
                        tools=tools,
                        request_name=self.request_name,
                        model_set=model,
                        stream=effective_stream,
                    )

                    if create_timeout is not None:
                        (
                            message,
                            tool_calls,
                            stream_iter,
                            reasoning_content,
                            request_record_id,
                        ) = _normalize_client_create_result(
                            await asyncio.wait_for(
                                create_awaitable,
                                timeout=create_timeout,
                            )
                        )
                    else:
                        (
                            message,
                            tool_calls,
                            stream_iter,
                            reasoning_content,
                            request_record_id,
                        ) = _normalize_client_create_result(await create_awaitable)

                provider_usage: dict[str, Any] = {}
                pop_last_usage = getattr(client, "pop_last_usage", None)
                if callable(pop_last_usage):
                    try:
                        raw_usage = pop_last_usage()
                        if isinstance(raw_usage, dict):
                            provider_usage = dict(raw_usage)
                    except Exception:
                        provider_usage = {}

                reasoning_text, reasoning_parts = _split_reasoning_result(
                    reasoning_content
                )

                resp = LLMResponse(
                    _stream=stream_iter,
                    _upper=self,
                    _auto_append_response=auto_append_response,
                    # trimmed_payloads 只代表“本次实际发给模型的窗口”。
                    # 响应对象会作为后续轮次的长生命周期上下文继续传递，不能把
                    # 本次临时裁剪结果写进去，否则一旦触发 token 裁剪，旧的
                    # user/assistant/tool_result 链路会被永久丢弃。
                    payloads=list(payloads),
                    model_set=model_set,
                    context_manager=self.context_manager,
                    tool_call_compat=bool(model.get("tool_call_compat", False)),
                    message=message,
                    reasoning_content=reasoning_text,
                    reasoning_parts=reasoning_parts,
                    call_list=[],
                    request_record_id=request_record_id,
                    effective_context_receipts=build_effective_context_receipts(
                        self._context_delivery_expectations,
                        list(trimmed_payloads),
                    ),
                )

                # 非流：立即解析 tool_calls
                if tool_calls:
                    from .payload import ToolCall

                    resp.call_list = [
                        ToolCall(
                            id=tc.get("id"),
                            name=tc.get("name", ""),
                            args=tc.get("args", {}),
                        )
                        for tc in tool_calls
                    ]

                if not stream and stream_iter is not None:
                    collect_timeout = _remaining_attempt_timeout(attempt_deadline)
                    if collect_timeout is not None:
                        collect_stream = resp.precollect_stream_for_non_stream()
                        await asyncio.wait_for(
                            collect_stream,
                            timeout=collect_timeout,
                        )
                    else:
                        await resp.precollect_stream_for_non_stream()
                resp.attach_to_inspector()

                # 记录成功指标
                if self.enable_metrics:
                    tokens_in = provider_usage.get("prompt_tokens")
                    tokens_out = provider_usage.get("completion_tokens")
                    metrics_extra: dict[str, Any] = {}
                    if provider_usage:
                        metrics_extra["provider_usage"] = provider_usage
                        if "cache_read_input_tokens" in provider_usage:
                            metrics_extra["cache_read_input_tokens"] = provider_usage[
                                "cache_read_input_tokens"
                            ]
                        if "cache_creation_input_tokens" in provider_usage:
                            metrics_extra["cache_creation_input_tokens"] = (
                                provider_usage["cache_creation_input_tokens"]
                            )

                    metrics = RequestMetrics(
                        model_name=model_identifier,
                        request_name=self.request_name,
                        latency=timer.elapsed,
                        tokens_in=tokens_in if isinstance(tokens_in, int) else None,
                        tokens_out=tokens_out if isinstance(tokens_out, int) else None,
                        success=True,
                        stream=stream,
                        retry_count=retry_count,
                        model_index=step.meta.get("model_index", 0) if step.meta else 0,
                        extra=metrics_extra,
                    )
                    get_global_collector().record_request(metrics)

                def _record_success(
                    response_obj: LLMResponse,
                    stream_error: BaseException | None = None,
                    *,
                    _attempt_id: str = attempt_id,
                    _parent_attempt_id: str | None = parent_attempt_id,
                    _attempt_number: int = attempt_index,
                    _model: dict[str, Any] = model,
                    _step_meta: dict[str, Any] | None = step.meta,
                    _trimmed: list[LLMPayload] = list(trimmed_payloads),  # noqa: B006 - freeze closure snapshot
                    _usage: dict[str, Any] = dict(provider_usage),  # noqa: B006 - freeze closure snapshot
                    _state: dict[str, Any] = attempt_state,
                    _started: float = attempt_started,
                ) -> None:
                    """Persist the attempt once its response content is final."""
                    if _state.get("recorded"):
                        return
                    _state["recorded"] = True
                    record_attempt(
                        attempt_id=_attempt_id,
                        parent_attempt_id=_parent_attempt_id,
                        attempt_number=_attempt_number,
                        model=_model,
                        step_meta=_step_meta,
                        trimmed=_trimmed,
                        latency_s=time.perf_counter() - _started,
                        success=stream_error is None,
                        usage=_usage,
                        error=stream_error,
                        response_obj=response_obj,
                    )

                if resp._stream is None:
                    # Non-stream (or pre-collected) responses are already complete here.
                    _record_success(resp)
                else:
                    resp._on_complete = _record_success

                session.record_success(latency=timer.elapsed)
                self._context_delivery_expectations.clear()
                return resp

            except asyncio.CancelledError:
                logger.debug(
                    f"LLM 请求被取消: model={model_identifier}, "
                    f"request={self.request_name or '__default__'}"
                )
                raise
            except BaseException as e:
                # 将原始异常转换为标准化 LLM 异常
                classified_error = classify_exception(e, model=model_identifier)
                last_error = classified_error

                _err_type = type(classified_error).__name__
                _5xx_status_code: int | None = (
                    classified_error.status_code
                    if isinstance(classified_error, LLMAPIError)
                    and isinstance(classified_error.status_code, int)
                    and classified_error.status_code >= 500
                    else None
                )
                if isinstance(classified_error, asyncio.CancelledError):
                    logger.debug(
                        f"LLM 请求被取消: model={model_identifier}, request={self.request_name or '__default__'}",
                        exc_info=True,
                    )
                elif (
                    isinstance(
                        classified_error,
                        (LLMTimeoutError, LLMRateLimitError, TimeoutError),
                    )
                    or _5xx_status_code is not None
                    or (
                        isinstance(classified_error, LLMAPIError)
                        and classified_error.status_code is None
                    )
                ):
                    _status_hint = (
                        f", status_code={_5xx_status_code}"
                        if _5xx_status_code is not None
                        else ""
                    )
                    logger.warning(
                        f"LLM 请求暂时失败: model={model_identifier}, "
                        f"request={self.request_name or '__default__'}, error_type={_err_type}{_status_hint}"
                    )
                    logger.debug(
                        f"LLM 请求暂时失败（详情）: model={model_identifier}, "
                        f"request={self.request_name or '__default__'}, reason={classified_error}",
                        exc_info=True,
                    )
                else:
                    logger.error(
                        f"LLM 请求失败: model={model_identifier}, request={self.request_name or '__default__'}, "
                        f"error_type={_err_type}, reason={classified_error}",
                        exc_info=True,
                    )

                # 记录失败指标
                if self.enable_metrics:
                    metrics = RequestMetrics(
                        model_name=model_identifier,
                        request_name=self.request_name,
                        latency=timer.elapsed,
                        success=False,
                        error=str(classified_error),
                        error_type=type(classified_error).__name__,
                        stream=stream,
                        retry_count=retry_count,
                        model_index=step.meta.get("model_index", 0) if step.meta else 0,
                    )
                    get_global_collector().record_request(metrics)

                if not attempt_state["recorded"]:
                    attempt_state["recorded"] = True
                    record_attempt(
                        attempt_id=attempt_id,
                        parent_attempt_id=parent_attempt_id,
                        attempt_number=attempt_index,
                        model=model,
                        step_meta=step.meta,
                        trimmed=list(trimmed_payloads),
                        latency_s=time.perf_counter() - attempt_started,
                        success=False,
                        error=classified_error,
                    )
                previous_attempt_id = attempt_id

                retry_count += 1
                next_step = session.next_after_error(classified_error)

                if next_step.model is None:
                    if next_step.error is None:
                        logger.error(
                            f"LLM 请求重试已耗尽: request={self.request_name or '__default__'}, "
                            f"retry_count={retry_count}, last_error={type(classified_error).__name__}: {classified_error}"
                        )
                    else:
                        logger.debug(
                            f"LLM 备用模型仍在冷却: request={self.request_name or '__default__'}, "
                            f"retry_after={getattr(next_step.error, 'retry_after', 0.0):.1f}s"
                        )
                else:
                    next_model_identifier = next_step.model.get("model_identifier")
                    next_model_name = (
                        next_model_identifier
                        if isinstance(next_model_identifier, str)
                        and next_model_identifier
                        else "<unknown>"
                    )
                    logger.warning(
                        f"LLM 请求将进行下一步重试: request={self.request_name or '__default__'}, "
                        f"retry_count={retry_count}, next_model={next_model_name}, "
                        f"configured_priority={next_step.meta.get('routing_priority', 0)}, "
                        f"routing_task={next_step.meta.get('routing_task') or '<direct>'}, "
                        f"snapshot={next_step.meta.get('routing_snapshot') or '<none>'}, "
                        f"skipped_cooling={list(next_step.meta.get('cooldown_skipped', ()))}, "
                        f"delay_seconds={float(next_step.delay_seconds):.2f}"
                    )

                step = next_step

        if step.error is not None:
            raise step.error
        assert last_error is not None
        raise last_error


def _validate_model_entry(model: dict[str, Any]) -> ModelEntry:
    """验证模型配置项的完整性和正确性，返回一个标准化的 ModelEntry 对象。"""
    required = [
        "api_provider",
        "base_url",
        "model_identifier",
        "api_key",
        "client_type",
        "max_retry",
        "timeout",
        "retry_interval",
        "price_in",
        "price_out",
        "temperature",
        "max_tokens",
        "max_context",
        "extra_params",
    ]

    missing = [k for k in required if k not in model]
    if missing:
        raise LLMConfigurationError(f"model_set 元素缺少字段: {missing}")

    if not isinstance(model.get("extra_params"), dict):
        raise LLMConfigurationError("model.extra_params 必须是 dict")

    if "tool_call_compat" in model and not isinstance(
        model.get("tool_call_compat"), bool
    ):
        raise LLMConfigurationError("model.tool_call_compat 必须是 bool")
    if "force_stream_mode" in model and not isinstance(
        model.get("force_stream_mode"), bool
    ):
        raise LLMConfigurationError("model.force_stream_mode 必须是 bool")
    if (
        not isinstance(model.get("max_context"), int)
        or model.get("max_context", 0) <= 0
    ):
        raise LLMConfigurationError("model.max_context 必须是正整数")

    extra_params = model.get("extra_params", {})
    if isinstance(extra_params, dict):
        if "context_reserve_ratio" in extra_params and not isinstance(
            extra_params.get("context_reserve_ratio"), (int, float)
        ):
            raise LLMConfigurationError(
                "model.extra_params.context_reserve_ratio 必须是 number"
            )
        if "context_reserve_tokens" in extra_params and not isinstance(
            extra_params.get("context_reserve_tokens"), int
        ):
            raise LLMConfigurationError(
                "model.extra_params.context_reserve_tokens 必须是 int"
            )
        if "context_compression_trigger_tokens" in extra_params and not isinstance(
            extra_params.get("context_compression_trigger_tokens"), int
        ):
            raise LLMConfigurationError(
                "model.extra_params.context_compression_trigger_tokens 必须是 int"
            )
        if "context_compression_trigger_ratio" in extra_params and not isinstance(
            extra_params.get("context_compression_trigger_ratio"), (int, float)
        ):
            raise LLMConfigurationError(
                "model.extra_params.context_compression_trigger_ratio 必须是 number"
            )

    model["api_key"] = redact_secret(model.get("api_key"))
    model["media_capabilities"] = normalize_media_capabilities(
        model.get("media_capabilities")
    )
    model.setdefault("tool_call_compat", False)
    model.setdefault("force_stream_mode", False)
    return model  # type: ignore[return-value]


def _validate_model_set(model_set: Any) -> ModelSet:
    """
    验证模型配置集合的完整性和正确性，返回一个标准化的 ModelSet 对象。
    """
    if not isinstance(model_set, list) or not model_set:
        raise LLMConfigurationError("model_set 必须是非空 list[dict]")
    if not all(isinstance(x, dict) for x in model_set):
        raise LLMConfigurationError("model_set 必须是 list[dict]")
    return [_validate_model_entry(x) for x in model_set]
