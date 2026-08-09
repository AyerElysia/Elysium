"""学习环 LLM 调用的超时预算。

这里只做两件事：把"这条链路最多等多久"从散落各处的字面量收拢成一处可覆盖的
配置，以及保证一次"发请求 + 读回复"只受一个截止时间约束。

为什么值得单独一个模块：反思环曾经配 45s，而同一 provider 上成功样本的
p50=23.4s、p90=39.1s、max=44.5s——分布右侧正好被 45s 削掉。审计环配 60s，
成功样本 p99=57.7s、max=58.0s，同一个削法。被削掉的调用不是"慢"，是根本
没机会写完，于是那段经历只能一轮轮重试到退避的尽头。同一 provider 上
life_memory_witness 的成功样本能跑到 180.3s，说明 180s 才是这条链路真实的
上界。这类数字不是"偏保守"，是错的，而且只看均值时完全看不出来——收拢到
一处，是为了下次再有人调这些数字时先读到这段话。
"""

from __future__ import annotations

import asyncio
import math
import os
from collections.abc import Awaitable
from typing import Any, Protocol

__all__ = ["resolve_timeout_seconds", "send_with_deadline"]


class _SendableRequest(Protocol):
    """The subset of the LLM request surface this module needs."""

    def send(self, *, auto_append_response: bool, stream: bool) -> Awaitable[Any]:
        """Dispatch the request and return an awaitable response handle."""
        ...


def resolve_timeout_seconds(
    explicit: float | None,
    *,
    env_var: str,
    default: float,
    minimum: float,
) -> float:
    """Resolve an LLM timeout from an explicit value or a deployment env var.

    Precedence: ``explicit``, then ``env_var``, then ``default``. A malformed or
    non-positive env var raises instead of silently falling back to the default:
    running for days on a wrong timeout is exactly how the reflection queue
    starved, so a deployment typo must fail loudly at construction rather than
    quietly at 03:00 five days later.

    Args:
        explicit: Caller-supplied timeout in seconds, or ``None`` to resolve from
            the environment.
        env_var: Name of the environment variable holding the override.
        default: Timeout in seconds used when neither source supplies a value.
        minimum: Floor applied to whichever value wins, so a too-small override
            cannot make the call unsatisfiable.

    Returns:
        The resolved timeout in seconds, never below ``minimum``.

    Raises:
        ValueError: If ``env_var`` is set but is not a positive finite number.
    """

    if explicit is not None:
        return max(minimum, float(explicit))

    raw = os.environ.get(env_var, "").strip()
    if not raw:
        return default
    try:
        parsed = float(raw)
    except ValueError as exc:
        raise ValueError(f"{env_var} must be a positive number of seconds") from exc
    if not math.isfinite(parsed) or parsed <= 0:
        raise ValueError(f"{env_var} must be a positive number of seconds")
    return max(minimum, parsed)


async def send_with_deadline(request: _SendableRequest, timeout: float) -> str:
    """Send an LLM request and read its response under one shared deadline.

    An LLM round trip here is two awaits: dispatching the request, then reading
    the response handle. Giving each await the full timeout makes the real
    ceiling twice the configured one, so the number in the config stops
    describing the system — a 60s budget could legitimately spend 120s. One
    monotonic deadline spans both waits instead.

    Args:
        request: The prepared LLM request to dispatch.
        timeout: Total seconds allowed for dispatch and response read combined.

    Returns:
        The response text, or an empty string if the provider returned nothing.

    Raises:
        TimeoutError: If the combined deadline elapses.
    """

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    response = await asyncio.wait_for(
        request.send(auto_append_response=False, stream=False),
        timeout=timeout,
    )
    remaining = deadline - loop.time()
    if remaining <= 0:
        raise TimeoutError("LLMDeadlineExhaustedBeforeResponseRead")
    raw_text = await asyncio.wait_for(response, timeout=remaining)
    return str(raw_text or "")
