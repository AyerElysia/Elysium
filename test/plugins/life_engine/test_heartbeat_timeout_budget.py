"""心跳超时预算与续问重试的回归测试。

背景：外层 ``asyncio.wait_for`` 超时值若等于 provider 的单模型超时，两个定时器
同时到点、外层取消先赢，抛出的 ``CancelledError`` 在 request.py 里是裸 raise，
不进 failover——于是 life 任务里 6 个候补模型一个都轮不到。
"""

from __future__ import annotations

import asyncio

import pytest

from plugins.life_engine.service.core import _resolve_heartbeat_timeout


def _model_set(*timeouts: float) -> list[dict[str, object]]:
    return [{"model_identifier": f"m{i}", "timeout": t} for i, t in enumerate(timeouts)]


def test_outer_budget_exceeds_single_attempt_timeout() -> None:
    """核心回归：外层预算必须严格大于单模型超时，否则 failover 永远轮不到。"""
    per_attempt = 120.0
    budget = _resolve_heartbeat_timeout(120.0, _model_set(*([per_attempt] * 6)))

    assert budget > per_attempt
    # 至少要容得下两次完整尝试，才谈得上"换一个模型"
    assert budget >= per_attempt * 2


def test_live_config_shape_no_longer_collides() -> None:
    """复现线上配置：heartbeat_timeout=120 且 provider timeout=120。"""
    budget = _resolve_heartbeat_timeout(120.0, _model_set(120.0, 120.0, 120.0))

    assert budget == pytest.approx(255.0)


def test_uses_slowest_model_in_set() -> None:
    """预算按集合里最慢的模型算，不能被快模型拉低。"""
    budget = _resolve_heartbeat_timeout(60.0, _model_set(30.0, 300.0, 45.0))

    assert budget == pytest.approx(615.0)


def test_configured_value_wins_when_larger() -> None:
    """配置值已经足够大时不再上抬。"""
    budget = _resolve_heartbeat_timeout(500.0, _model_set(60.0))

    assert budget == pytest.approx(500.0)


@pytest.mark.parametrize(
    "model_set",
    [
        None,
        [],
        [{"model_identifier": "m0"}],
        [{"timeout": 0}],
        [{"timeout": -5}],
        [{"timeout": "not-a-number"}],
        ["garbage"],
    ],
)
def test_degrades_to_configured_value_on_unusable_model_set(model_set: object) -> None:
    """模型集读不出超时时退回配置值，绝不抛异常打断心跳。"""
    assert _resolve_heartbeat_timeout(120.0, model_set) == pytest.approx(120.0)


def test_clamped_to_sane_bounds() -> None:
    assert _resolve_heartbeat_timeout(0.0, None) == pytest.approx(10.0)
    assert _resolve_heartbeat_timeout(99999.0, None) == pytest.approx(900.0)
    assert _resolve_heartbeat_timeout(120.0, _model_set(5000.0)) == pytest.approx(900.0)


async def test_retry_exhausted_error_is_the_contract_on_timeout() -> None:
    """续问的 except 子句依赖这个契约：耗尽后抛 RetryExhaustedError。"""
    from plugins.life_engine.service.error_handling import (
        RetryExhaustedError,
        retry_with_backoff,
    )

    attempts = 0

    async def _always_timeout() -> None:
        nonlocal attempts
        attempts += 1
        raise asyncio.TimeoutError

    with pytest.raises(RetryExhaustedError):
        await retry_with_backoff(
            _always_timeout,
            max_retries=1,
            initial_delay=0.0,
            backoff_factor=1.0,
            exceptions=(asyncio.TimeoutError,),
        )

    # max_retries=1 => 首次 + 1 次重试
    assert attempts == 2


async def test_followup_retry_recovers_on_second_attempt() -> None:
    """首次续问超时、第二次成功时，心跳不应被判死。"""
    from plugins.life_engine.service.error_handling import retry_with_backoff

    attempts = 0

    async def _flaky() -> str:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise asyncio.TimeoutError
        return "ok"

    result = await retry_with_backoff(
        _flaky,
        max_retries=1,
        initial_delay=0.0,
        backoff_factor=1.0,
        exceptions=(asyncio.TimeoutError,),
    )

    assert result == "ok"
    assert attempts == 2
