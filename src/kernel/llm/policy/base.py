"""
定义了一个用于 LLM 重试策略的基础模块，
包括 ModelStep 数据类和 PolicySession、Policy 协议接口。

这些组件用于描述和执行基于模型输出的重试计划。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class ModelStep:
    """下一步执行计划。

    - model=None 表示当前没有可执行模型，应由上层抛出 error 或最后一次异常。
    - delay_seconds 由 policy 决定（例如 retry_interval）。
    - error 用于 policy 在没有可执行模型时传递可操作的终止原因。
    """

    model: dict[str, Any] | None
    delay_seconds: float = 0.0
    meta: dict[str, Any] | None = None
    error: BaseException | None = None


class PolicySession(Protocol):
    def first(self) -> ModelStep:
        ...

    def next_after_error(self, error: BaseException) -> ModelStep:
        ...

    def record_success(self, *, latency: float = 0.0, tokens: int = 0) -> None:
        ...


class Policy(Protocol):
    def new_session(self, *, model_set: Any, request_name: str) -> PolicySession:
        ...
