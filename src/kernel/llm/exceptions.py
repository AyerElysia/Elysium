from __future__ import annotations


class LLMError(RuntimeError):
    """LLM 操作基础异常。"""


class LLMContextError(LLMError):
    """上下文结构错误。

    用于在严格模式下，当上下文 messages 不满足协议约束时直接抛出，
    以避免“自动修复”掩盖上游链路问题。
    """


class LLMConfigurationError(LLMError):
    """配置错误。"""


class MediaValidationError(LLMError):
    """媒体输入无法通过结构、编码或文件签名校验。"""


class MediaLimitError(MediaValidationError):
    """媒体输入超过单项或请求限制。"""


class UnsupportedModalityError(LLMError):
    """当前模型或 provider 不支持请求中的媒体模态。"""


class LLMResponseConsumedError(LLMError):
    """响应已被消费。"""


class LLMRateLimitError(LLMError):
    """速率限制错误。"""

    def __init__(
        self, message: str, retry_after: float | None = None, model: str | None = None
    ):
        super().__init__(message)
        self.retry_after = retry_after
        self.model = model


class LLMTimeoutError(LLMError):
    """超时错误。"""

    def __init__(
        self, message: str, timeout: float | None = None, model: str | None = None
    ):
        super().__init__(message)
        self.timeout = timeout
        self.model = model


class LLMContentFilterError(LLMError):
    """内容过滤错误（内容违反安全策略）。"""

    def __init__(
        self, message: str, filter_type: str | None = None, model: str | None = None
    ):
        super().__init__(message)
        self.filter_type = filter_type
        self.model = model


class LLMTokenLimitError(LLMError):
    """Token 超限错误。"""

    def __init__(
        self,
        message: str,
        max_tokens: int | None = None,
        requested_tokens: int | None = None,
        model: str | None = None,
    ):
        super().__init__(message)
        self.max_tokens = max_tokens
        self.requested_tokens = requested_tokens
        self.model = model


class LLMAuthenticationError(LLMError):
    """认证错误（API key 无效等）。"""

    def __init__(self, message: str, model: str | None = None):
        super().__init__(message)
        self.model = model


class LLMAPIError(LLMError):
    """API 调用错误（通用）。"""

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        error_code: str | None = None,
        model: str | None = None,
        retry_after: float | None = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code
        self.model = model
        self.retry_after = retry_after


class LLMModelsCoolingDownError(LLMError):
    """所有候选模型均处于临时故障冷却期。"""

    def __init__(
        self,
        *,
        request_name: str,
        retry_after: float,
        models: tuple[str, ...] = (),
        routing_task: str = "",
        routing_snapshot: str = "",
    ) -> None:
        self.request_name = request_name or "__default__"
        self.retry_after = max(1.0, float(retry_after))
        self.models = models
        self.routing_task = routing_task
        self.routing_snapshot = routing_snapshot
        route_context = ""
        if routing_task or routing_snapshot:
            route_context = (
                f", routing_task={routing_task or '<direct>'}, "
                f"snapshot={routing_snapshot or '<none>'}"
            )
        super().__init__(
            "all candidate models are cooling down: "
            f"request={self.request_name}, retry_after={self.retry_after:.1f}s"
            f"{route_context}"
        )


def is_transient_llm_error(error: BaseException) -> bool:
    """Return whether an LLM failure is likely to recover without reconfiguration."""

    if isinstance(
        error,
        (
            LLMTimeoutError,
            LLMRateLimitError,
            LLMModelsCoolingDownError,
            TimeoutError,
        ),
    ):
        return True
    if isinstance(error, LLMAPIError):
        status_code = error.status_code
        return status_code is None or status_code == 429 or status_code >= 500
    return False


_GATEWAY_RESOURCE_OVERLOAD_CODES = frozenset(
    {
        "system_cpu_overloaded",
        "system_memory_overloaded",
        "system_disk_overloaded",
    }
)


def is_gateway_resource_overload(error: BaseException) -> bool:
    """Return whether a structured API error applies to the whole gateway."""

    if not isinstance(error, LLMAPIError):
        return False
    error_code = str(error.error_code or "").strip().lower()
    return error_code in _GATEWAY_RESOURCE_OVERLOAD_CODES


def should_retry_same_model(error: BaseException) -> bool:
    """判断同一模型是否值得继续重试。

    说明：
    - 返回 ``True`` 表示当前模型还可以重试一次；
    - 返回 ``False`` 表示应直接切到下一个模型，不要在当前模型上空转。
    """
    if isinstance(error, LLMModelsCoolingDownError):
        return False

    if isinstance(error, (LLMTimeoutError, LLMRateLimitError)):
        return True

    if isinstance(
        error,
        (
            LLMConfigurationError,
            MediaValidationError,
            UnsupportedModalityError,
            LLMAuthenticationError,
            LLMContentFilterError,
            LLMTokenLimitError,
        ),
    ):
        return False

    if isinstance(error, LLMAPIError):
        status_code = error.status_code
        if status_code is None:
            return True
        if status_code == 429:
            return True
        return status_code >= 500

    if isinstance(error, TimeoutError):
        return True

    return True


def classify_exception(error: BaseException, model: str | None = None) -> BaseException:
    """将第三方 SDK 异常转换为标准化的 LLM 异常。

    这个函数尝试识别常见的 API 错误类型并转换为更具体的异常类型。
    如果无法识别，返回原始异常。
    """
    error_msg = str(error).lower()

    # OpenAI SDK 异常处理
    try:
        from openai import (
            APIError,
            APITimeoutError,
            AuthenticationError,
            BadRequestError,
            RateLimitError,
        )

        if isinstance(error, RateLimitError):
            # 尝试从错误中提取 retry_after
            retry_after = getattr(error, "retry_after", None)
            return LLMRateLimitError(str(error), retry_after=retry_after, model=model)

        if isinstance(error, APITimeoutError):
            return LLMTimeoutError(str(error), model=model)

        if isinstance(error, AuthenticationError):
            return LLMAuthenticationError(str(error), model=model)

        if isinstance(error, BadRequestError):
            # 检查是否是 token 限制
            if (
                "maximum context length" in error_msg
                or "token" in error_msg
                and "limit" in error_msg
            ):
                return LLMTokenLimitError(str(error), model=model)
            # 检查是否是内容过滤
            if "content_filter" in error_msg or "content policy" in error_msg:
                return LLMContentFilterError(str(error), model=model)

        if isinstance(error, APIError):
            status_code = getattr(error, "status_code", None)
            error_code = getattr(error, "code", None)
            return LLMAPIError(
                str(error), status_code=status_code, error_code=error_code, model=model
            )

    except ImportError:
        pass

    # Anthropic SDK 异常处理
    try:
        from anthropic import (
            APIError as AnthropicAPIError,
        )
        from anthropic import (
            APIStatusError as AnthropicAPIStatusError,
        )
        from anthropic import (
            APITimeoutError as AnthropicAPITimeoutError,
        )
        from anthropic import (
            AuthenticationError as AnthropicAuthenticationError,
        )
        from anthropic import (
            BadRequestError as AnthropicBadRequestError,
        )
        from anthropic import (
            RateLimitError as AnthropicRateLimitError,
        )

        if isinstance(error, AnthropicRateLimitError):
            retry_after = getattr(error, "retry_after", None)
            return LLMRateLimitError(str(error), retry_after=retry_after, model=model)

        if isinstance(error, AnthropicAPITimeoutError):
            return LLMTimeoutError(str(error), model=model)

        if isinstance(error, AnthropicAuthenticationError):
            return LLMAuthenticationError(str(error), model=model)

        if isinstance(error, AnthropicBadRequestError):
            if (
                "maximum context length" in error_msg
                or "token" in error_msg
                and "limit" in error_msg
            ):
                return LLMTokenLimitError(str(error), model=model)
            if "content" in error_msg and (
                "filter" in error_msg or "policy" in error_msg
            ):
                return LLMContentFilterError(str(error), model=model)

        for error_type in (AnthropicAPIStatusError, AnthropicAPIError):
            if isinstance(error, error_type):
                status_code = getattr(error, "status_code", None)
                error_code = getattr(error, "type", None) or getattr(
                    error, "error_code", None
                )
                return LLMAPIError(
                    str(error),
                    status_code=status_code,
                    error_code=error_code,
                    model=model,
                )

    except ImportError:
        pass

    # 通用错误检查（基于错误消息）
    if "rate" in error_msg and "limit" in error_msg:
        return LLMRateLimitError(str(error), model=model)

    if "timeout" in error_msg or "timed out" in error_msg:
        return LLMTimeoutError(str(error), model=model)

    if (
        "authentication" in error_msg
        or "unauthorized" in error_msg
        or "api key" in error_msg
    ):
        return LLMAuthenticationError(str(error), model=model)

    if "token" in error_msg and "limit" in error_msg:
        return LLMTokenLimitError(str(error), model=model)

    if "content" in error_msg and ("filter" in error_msg or "policy" in error_msg):
        return LLMContentFilterError(str(error), model=model)

    return error
