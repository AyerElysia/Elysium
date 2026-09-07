"""Shared heartbeat deadlines must not hide an inner timeout's identity."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from plugins.life_engine.service import core as service_module


@pytest.fixture
def heartbeat_clock(monkeypatch):
    """Control only heartbeat time accounting, never the event loop clock."""

    clock = SimpleNamespace(now=100.0)

    def remaining(deadline, *, reserve_seconds=5.0):
        return deadline - clock.now - max(0.0, reserve_seconds)

    monkeypatch.setattr(
        service_module, "_heartbeat_remaining_seconds", remaining,
    )
    return clock


@pytest.mark.parametrize("per_call_timeout", [None, 255.0, 1.0])
@pytest.mark.parametrize("synchronous_factory", [False, True])
async def test_inner_timeout_keeps_identity_while_shared_budget_remains(
    heartbeat_clock, per_call_timeout, synchronous_factory,
):
    inner_timeout = TimeoutError("synthetic provider timeout")
    provider_cause = ValueError("synthetic provider transport cause")

    async def fail_before_deadline():
        heartbeat_clock.now += 0.25
        raise inner_timeout from provider_cause

    def fail_synchronously():
        heartbeat_clock.now += 0.25
        raise inner_timeout from provider_cause

    with pytest.raises(TimeoutError) as raised:
        await service_module._await_with_heartbeat_deadline(
            fail_synchronously if synchronous_factory else fail_before_deadline,
            deadline=130.0,
            stage="followup_request",
            per_call_timeout=per_call_timeout,
        )

    assert raised.value is inner_timeout
    assert raised.value.__cause__ is provider_cause
    assert not isinstance(raised.value, service_module.HeartbeatBudgetExhausted)
    assert service_module._heartbeat_remaining_seconds(130.0) == 24.75


@pytest.mark.parametrize("per_call_timeout", [None, 255.0, 1.0])
async def test_elapsed_shared_budget_retains_stage_and_timeout_cause(
    heartbeat_clock, per_call_timeout,
):
    elapsed_timeout = TimeoutError("synthetic awaited step timed out")

    async def fail_after_usable_deadline():
        heartbeat_clock.now = 125.0
        raise elapsed_timeout

    with pytest.raises(service_module.HeartbeatBudgetExhausted) as raised:
        await service_module._await_with_heartbeat_deadline(
            fail_after_usable_deadline,
            deadline=130.0,
            stage="followup_request",
            per_call_timeout=per_call_timeout,
        )

    assert raised.value.stage == "followup_request"
    assert raised.value.__cause__ is elapsed_timeout


async def test_factory_is_not_started_when_shared_budget_already_elapsed(
    heartbeat_clock,
):
    heartbeat_clock.now = 125.0
    invoked = False

    async def must_not_start():
        nonlocal invoked
        invoked = True

    with pytest.raises(service_module.HeartbeatBudgetExhausted) as raised:
        await service_module._await_with_heartbeat_deadline(
            must_not_start,
            deadline=130.0,
            stage="response_read",
            per_call_timeout=255.0,
        )

    assert raised.value.stage == "response_read"
    assert invoked is False


@pytest.mark.parametrize("exhaust_budget", [False, True])
async def test_inner_cancellation_is_not_reclassified_as_timeout(
    heartbeat_clock, exhaust_budget,
):
    cancellation = asyncio.CancelledError("synthetic provider cancellation")

    async def cancelled_factory():
        if exhaust_budget:
            heartbeat_clock.now = 125.0
        raise cancellation

    with pytest.raises(asyncio.CancelledError) as raised:
        await service_module._await_with_heartbeat_deadline(
            cancelled_factory,
            deadline=130.0,
            stage="followup_request",
            per_call_timeout=255.0,
        )

    assert raised.value is cancellation


@pytest.mark.parametrize("exhaust_budget", [False, True])
async def test_nested_budget_error_keeps_original_stage_and_identity(
    heartbeat_clock, exhaust_budget,
):
    inner_budget_error = service_module.HeartbeatBudgetExhausted(
        "nested_provider_step"
    )

    async def exhausted_factory():
        if exhaust_budget:
            heartbeat_clock.now = 125.0
        raise inner_budget_error

    with pytest.raises(service_module.HeartbeatBudgetExhausted) as raised:
        await service_module._await_with_heartbeat_deadline(
            exhausted_factory,
            deadline=130.0,
            stage="followup_request",
            per_call_timeout=255.0,
        )

    assert raised.value is inner_budget_error
    assert raised.value.stage == "nested_provider_step"


async def test_success_preserves_result_and_invokes_factory_once(heartbeat_clock):
    result = object()
    call_count = 0

    async def successful_factory():
        nonlocal call_count
        call_count += 1
        return result

    observed = await service_module._await_with_heartbeat_deadline(
        successful_factory,
        deadline=130.0,
        stage="followup_request",
        per_call_timeout=255.0,
    )

    assert observed is result
    assert call_count == 1


async def test_caller_cancellation_reaches_awaited_factory():
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def blocked_factory():
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    task = asyncio.create_task(
        service_module._await_with_heartbeat_deadline(
            blocked_factory,
            deadline=asyncio.get_running_loop().time() + 60.0,
            stage="followup_request",
            per_call_timeout=255.0,
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), timeout=1.0)
        task.cancel("synthetic caller cancellation")
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cancelled.is_set()
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
