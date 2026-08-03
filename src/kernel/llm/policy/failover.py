"""严格顺序故障转移策略（Failover Policy）。

设计目标：让 ``model_list`` 的顺序成为可预期的主备链。

- 正常情况下从列表第一个模型开始；
- 临时故障模型按请求类型进入跨请求冷却，避免连续撞击已知故障端点；
- 当前模型失败后立即切换到下一个；
- 不做同模型反复重试，冷却到期后自动恢复主模型探测。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any

from ..exceptions import (
    LLMModelsCoolingDownError,
    is_gateway_resource_overload,
    is_transient_llm_error,
)
from .base import ModelStep, Policy, PolicySession

# A local gateway can recover from a brief channel-capacity 503 in seconds.  A
# five-minute first cooldown made every model in a task remain unavailable long
# after the gateway had recovered.  Keep the cross-request breaker, but probe
# again soon and reserve the longer windows for repeated failures.
_DEFAULT_COOLDOWN_SECONDS = 30.0
_MAX_COOLDOWN_SECONDS = 300.0


@dataclass(slots=True)
class _CooldownState:
    """单个模型在一种请求类型下的临时故障状态。"""

    until: float
    failure_count: int


class _ModelCooldownRegistry:
    """进程级模型冷却表，并单独维护跨请求的网关冷却。"""

    def __init__(self) -> None:
        self._entries: dict[tuple[str, str, str, str], _CooldownState] = {}
        self._gateway_entries: dict[tuple[str, str], _CooldownState] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _key(
        request_name: str,
        model: dict[str, Any],
    ) -> tuple[str, str, str, str]:
        return (
            request_name or "__default__",
            str(model.get("api_provider") or ""),
            str(model.get("base_url") or ""),
            str(model.get("model_identifier") or ""),
        )

    @staticmethod
    def _gateway_key(model: dict[str, Any]) -> tuple[str, str]:
        return (
            str(model.get("api_provider") or "").strip().lower(),
            str(model.get("base_url") or "").strip().rstrip("/").lower(),
        )

    def record_failure(
        self,
        request_name: str,
        model: dict[str, Any],
        error: BaseException,
        *,
        base_cooldown_seconds: float,
    ) -> None:
        """记录可恢复故障；永久错误继续由每次请求显式暴露。"""

        if not is_transient_llm_error(error):
            return

        gateway_scope = is_gateway_resource_overload(error)
        key = (
            self._gateway_key(model)
            if gateway_scope
            else self._key(request_name, model)
        )
        with self._lock:
            entries = self._gateway_entries if gateway_scope else self._entries
            previous = entries.get(key)
            failure_count = 1 if previous is None else previous.failure_count + 1
            exponent = min(10, failure_count - 1)
            duration = min(
                _MAX_COOLDOWN_SECONDS,
                base_cooldown_seconds * (2**exponent),
            )
            entries[key] = _CooldownState(
                until=time.monotonic() + duration,
                failure_count=failure_count,
            )

    def record_success(
        self,
        request_name: str,
        model: dict[str, Any],
    ) -> None:
        """成功探测后立即恢复该模型的主备优先级。"""

        key = self._key(request_name, model)
        with self._lock:
            self._entries.pop(key, None)
            self._gateway_entries.pop(self._gateway_key(model), None)

    def choose_index(
        self,
        request_name: str,
        models: list[dict[str, Any]],
        *,
        start: int,
    ) -> tuple[int | None, tuple[str, ...], float]:
        """选择首个已就绪模型，并返回全部冷却时的最短剩余时间。"""

        candidates = list(range(start, len(models)))
        if not candidates:
            return None, (), 0.0

        now = time.monotonic()
        cooling: list[tuple[int, _CooldownState]] = []
        skipped: list[str] = []
        with self._lock:
            for index in candidates:
                model = models[index]
                states = (
                    self._entries.get(self._key(request_name, model)),
                    self._gateway_entries.get(self._gateway_key(model)),
                )
                active_states = [
                    state for state in states if state is not None and state.until > now
                ]
                if not active_states:
                    return index, tuple(skipped), 0.0
                state = max(active_states, key=lambda item: item.until)
                cooling.append((index, state))
                skipped.append(str(model.get("model_identifier") or "unknown"))

        retry_after = max(1.0, min(state.until for _, state in cooling) - now)
        return None, tuple(skipped), retry_after

    def reset(self) -> None:
        """清空健康状态，供确定性测试使用。"""

        with self._lock:
            self._entries.clear()
            self._gateway_entries.clear()


_MODEL_COOLDOWNS = _ModelCooldownRegistry()


def _reset_model_cooldowns_for_tests() -> None:
    _MODEL_COOLDOWNS.reset()


def _routing_meta(model: dict[str, Any], index: int) -> dict[str, Any]:
    """Return secret-free immutable route identity for logs and trajectories."""

    return {
        "model_index": index,
        "model_name": model.get("model_identifier", "unknown"),
        "routing_task": model.get("routing_task", ""),
        "routing_model_alias": model.get("routing_model_alias", ""),
        "routing_priority": model.get("routing_priority", index),
        "routing_snapshot": model.get("routing_snapshot", ""),
    }


class FailoverPolicy(Policy):
    """按模型集顺序切换，并避开仍在冷却的临时故障模型。"""

    def __init__(
        self,
        *,
        cooldown_seconds: float = _DEFAULT_COOLDOWN_SECONDS,
    ) -> None:
        try:
            parsed = float(cooldown_seconds)
        except (TypeError, ValueError):
            parsed = _DEFAULT_COOLDOWN_SECONDS
        self._cooldown_seconds = min(
            _MAX_COOLDOWN_SECONDS,
            max(1.0, parsed),
        )

    def new_session(self, *, model_set: Any, request_name: str) -> PolicySession:
        if not isinstance(model_set, list) or not model_set:
            raise ValueError("model_set 必须是非空 list[dict]")
        if not all(isinstance(item, dict) for item in model_set):
            raise ValueError("model_set 必须是 list[dict]")
        return _FailoverSession(
            model_set=model_set,
            request_name=request_name,
            cooldown_seconds=self._cooldown_seconds,
        )


class _FailoverSession(PolicySession):
    def __init__(
        self,
        *,
        model_set: list[dict[str, Any]],
        request_name: str,
        cooldown_seconds: float,
    ) -> None:
        self._models = model_set
        self._request_name = request_name
        self._cooldown_seconds = cooldown_seconds
        self._idx = 0
        self._attempts_used = 0
        self._started = False

    def first(self) -> ModelStep:
        self._started = True
        selected, skipped, retry_after = _MODEL_COOLDOWNS.choose_index(
            self._request_name,
            self._models,
            start=0,
        )
        if selected is None:
            primary = self._models[0]
            return ModelStep(
                model=None,
                meta={
                    **_routing_meta(primary, 0),
                    "reason": "all_models_cooling",
                    "strategy": "failover",
                    "cooldown_skipped": skipped,
                    "retry_after": retry_after,
                },
                error=LLMModelsCoolingDownError(
                    request_name=self._request_name,
                    retry_after=retry_after,
                    models=skipped,
                    routing_task=str(primary.get("routing_task") or ""),
                    routing_snapshot=str(primary.get("routing_snapshot") or ""),
                ),
            )
        self._idx = selected
        self._attempts_used = 1
        model = self._models[self._idx]
        return ModelStep(
            model=model,
            meta={
                **_routing_meta(model, self._idx),
                "attempt": 1,
                "strategy": "failover",
                "cooldown_skipped": skipped,
            },
        )

    def next_after_error(self, error: BaseException) -> ModelStep:
        if not self._started:
            return self.first()

        _MODEL_COOLDOWNS.record_failure(
            self._request_name,
            self._models[self._idx],
            error,
            base_cooldown_seconds=self._cooldown_seconds,
        )
        next_idx, skipped, retry_after = _MODEL_COOLDOWNS.choose_index(
            self._request_name,
            self._models,
            start=self._idx + 1,
        )
        if next_idx is None:
            if is_gateway_resource_overload(error) and retry_after > 0:
                if hasattr(error, "retry_after"):
                    error.retry_after = retry_after
                cooling_error = error
            else:
                cooling_error = (
                    LLMModelsCoolingDownError(
                        request_name=self._request_name,
                        retry_after=retry_after,
                        models=skipped,
                        routing_task=str(
                            self._models[self._idx].get("routing_task") or ""
                        ),
                        routing_snapshot=str(
                            self._models[self._idx].get("routing_snapshot") or ""
                        ),
                    )
                    if retry_after > 0
                    else None
                )
            return ModelStep(
                model=None,
                meta={
                    "reason": (
                        "remaining_models_cooling"
                        if cooling_error is not None
                        else "exhausted"
                    ),
                    "strategy": "failover",
                    "attempt": self._attempts_used,
                    "cooldown_skipped": skipped,
                    "retry_after": retry_after,
                },
                error=cooling_error,
            )

        self._idx = next_idx
        self._attempts_used += 1
        model = self._models[self._idx]
        return ModelStep(
            model=model,
            delay_seconds=0.0,
            meta={
                **_routing_meta(model, self._idx),
                "attempt": self._attempts_used,
                "switch": True,
                "strategy": "failover",
                "error_type": type(error).__name__,
                "cooldown_skipped": skipped,
            },
        )

    def record_success(self, *, latency: float = 0.0, tokens: int = 0) -> None:
        if self._started:
            _MODEL_COOLDOWNS.record_success(
                self._request_name,
                self._models[self._idx],
            )
