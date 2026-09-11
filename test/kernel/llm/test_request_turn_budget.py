"""A slow first provider must not consume the whole chat step indefinitely."""

import asyncio

import pytest

from src.kernel.llm import attempt_budget as budget_module
from src.kernel.llm.attempt_budget import (
    effective_attempt_timeout,
    model_turn_attempt_budget,
)
from src.kernel.llm.model_client import ModelClientRegistry
from src.kernel.llm.payload import LLMPayload, Text
from src.kernel.llm.request import LLMRequest
from src.kernel.llm.roles import ROLE


def test_budget_reserves_failover_without_widening_config(monkeypatch):
    monkeypatch.setattr(budget_module.time, "monotonic", lambda: 100.0)
    assert effective_attempt_timeout(600, model_count=2) == 600
    with model_turn_attempt_budget(295):
        assert effective_attempt_timeout(600, model_count=2) == 147.0
        assert effective_attempt_timeout(30, model_count=2) == 30.0
        assert effective_attempt_timeout(600, model_count=1) == 294.0
        assert effective_attempt_timeout(None, model_count=2) == 147.0
        with model_turn_attempt_budget(900):
            assert effective_attempt_timeout(600, model_count=2) == 147.0
        with model_turn_attempt_budget(21):
            assert effective_attempt_timeout(600, model_count=2) == 10.0
        assert effective_attempt_timeout(600, model_count=2) == 147.0
        monkeypatch.setattr(budget_module.time, "monotonic", lambda: 394.0)
        with pytest.raises(TimeoutError, match="budget exhausted"):
            effective_attempt_timeout(600, model_count=2)
    assert effective_attempt_timeout(600, model_count=2) == 600


@pytest.mark.parametrize("seconds", [0, -1, float("inf"), float("nan")])
def test_invalid_budget_is_explicit(seconds):
    with pytest.raises(ValueError, match="finite and positive"):
        with model_turn_attempt_budget(seconds):
            pass


async def test_slow_primary_fails_over_and_cools_within_outer_budget(
    mock_multi_model_set,
):
    models = [dict(item, timeout=600) for item in mock_multi_model_set]
    calls = []
    primary_cancelled = asyncio.Event()

    class Client:
        async def create(self, **kwargs):
            name = kwargs["model_name"]
            calls.append(name)
            if name == models[0]["model_identifier"]:
                try:
                    await asyncio.Event().wait()
                finally:
                    primary_cancelled.set()
            return "recovered", [], None

    request = LLMRequest(
        models,
        request_name="bounded_chat_recovery",
        clients=ModelClientRegistry(openai=Client()),
    )
    original = LLMPayload(ROLE.USER, Text("preserve this pending input"))
    request.payloads = [original]
    async with asyncio.timeout(3.0):
        with model_turn_attempt_budget(1.0):
            response = await request.send(stream=False)
    assert primary_cancelled.is_set()
    assert calls == [model["model_identifier"] for model in models]
    assert response.message == "recovered"
    assert response.final_request_id
    assert response.final_attempt_id
    assert request.payloads[0] is original
    assert all(model["timeout"] == 600 for model in models)
    # The inner timeout reached failover's cross-request health registry.
    again = await request.send(stream=False)
    assert again.message == "recovered"
    assert calls[-1] == models[1]["model_identifier"]
    assert calls.count(models[0]["model_identifier"]) == 1


async def test_budget_is_task_local_and_restores_after_exception():
    entered = asyncio.Event()
    release = asyncio.Event()

    async def constrained():
        with pytest.raises(RuntimeError):
            with model_turn_attempt_budget(10):
                entered.set()
                await release.wait()
                assert effective_attempt_timeout(600, model_count=2) <= 4.5
                raise RuntimeError("cleanup")
        assert effective_attempt_timeout(600, model_count=2) == 600

    task = asyncio.create_task(constrained())
    await entered.wait()
    assert effective_attempt_timeout(600, model_count=2) == 600
    release.set()
    await task


async def test_user_cancellation_does_not_trigger_fallback(mock_multi_model_set):
    calls = []
    started = asyncio.Event()
    stopped = asyncio.Event()

    class Client:
        async def create(self, **kwargs):
            calls.append(kwargs["model_name"])
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

    request = LLMRequest(
        mock_multi_model_set,
        request_name="explicit_cancel_stays_cancelled",
        clients=ModelClientRegistry(openai=Client()),
    )

    async def send():
        with model_turn_attempt_budget(10):
            return await request.send(stream=False)

    task = asyncio.create_task(send())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stopped.is_set()
    assert calls == [mock_multi_model_set[0]["model_identifier"]]


async def test_expired_budget_does_not_cool_uncontacted_models(
    mock_multi_model_set, monkeypatch,
):
    calls = []

    class Client:
        async def create(self, **kwargs):
            calls.append(kwargs["model_name"])
            return "ready", [], None

    request = LLMRequest(
        mock_multi_model_set,
        request_name="expired_before_transport",
        clients=ModelClientRegistry(openai=Client()),
    )
    monkeypatch.setattr(budget_module.time, "monotonic", lambda: 100.0)
    with model_turn_attempt_budget(1):
        monkeypatch.setattr(budget_module.time, "monotonic", lambda: 102.0)
        with pytest.raises(TimeoutError, match="budget exhausted"):
            await request.send(stream=False)
    assert calls == []
    monkeypatch.undo()
    response = await request.send(stream=False)
    assert response.message == "ready"
    assert calls == [mock_multi_model_set[0]["model_identifier"]]
