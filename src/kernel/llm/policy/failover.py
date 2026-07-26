"""
严格顺序故障转移策略（Failover Policy）。

设计目标：让 model_list 的顺序成为可预期的主备链。
- 每次请求永远从列表第 1 个模型开始
- 当前模型失败后立刻切换到下一个
- 不做同模型反复重试，也不跨请求轮换起点
"""

from __future__ import annotations

from typing import Any

from .base import ModelStep, Policy, PolicySession


class FailoverPolicy(Policy):
    """按 model_set 顺序做一次主备切换。"""

    def new_session(self, *, model_set: Any, request_name: str) -> PolicySession:
        if not isinstance(model_set, list) or not model_set:
            raise ValueError("model_set 必须是非空 list[dict]")
        if not all(isinstance(x, dict) for x in model_set):
            raise ValueError("model_set 必须是 list[dict]")
        return _FailoverSession(model_set=model_set)


class _FailoverSession(PolicySession):
    def __init__(self, *, model_set: list[dict[str, Any]]) -> None:
        self._models = model_set
        self._idx = 0
        self._attempts_used = 0
        self._started = False

    def first(self) -> ModelStep:
        self._started = True
        self._idx = 0
        self._attempts_used = 1
        model = self._models[self._idx]
        return ModelStep(
            model=model,
            meta={
                "model_index": self._idx,
                "model_name": model.get("model_identifier", "unknown"),
                "attempt": 1,
                "strategy": "failover",
            },
        )

    def next_after_error(self, error: BaseException) -> ModelStep:
        if not self._started:
            return self.first()

        # 不做同模型重试：失败即前进到下一个模型。
        next_idx = self._idx + 1
        if next_idx >= len(self._models):
            return ModelStep(
                model=None,
                meta={
                    "reason": "exhausted",
                    "strategy": "failover",
                    "attempt": self._attempts_used,
                },
            )

        self._idx = next_idx
        self._attempts_used += 1
        model = self._models[self._idx]
        return ModelStep(
            model=model,
            delay_seconds=0.0,
            meta={
                "model_index": self._idx,
                "model_name": model.get("model_identifier", "unknown"),
                "attempt": self._attempts_used,
                "switch": True,
                "strategy": "failover",
                "error_type": type(error).__name__,
            },
        )

    def record_success(self, *, latency: float = 0.0, tokens: int = 0) -> None:
        return None
