"""API v1 稳定错误响应 schema。"""

from typing import Any

from pydantic import Field

from .common import StrictModel


class RecoveryHint(StrictModel):
    """调用方可安全执行的恢复提示。"""

    action: str = Field(min_length=1, max_length=100)
    cursor: str | None = None


class ErrorBody(StrictModel):
    """稳定且可安全展示的错误主体。"""

    code: str = Field(min_length=1, max_length=100)
    message: str = Field(min_length=1, max_length=500)
    request_id: str = Field(min_length=1, max_length=100)
    retryable: bool = False
    details: dict[str, Any] = Field(default_factory=dict)
    recovery: RecoveryHint | None = None


class ErrorResponse(StrictModel):
    """所有 API v1 错误的统一 envelope。"""

    error: ErrorBody
