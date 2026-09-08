"""Heartbeat-only exact transport protection through actual kernel attempts.

All providers are local fakes; trajectories, model inspectors, formal stores,
and services are never invoked.  The old strict receipt remains the gate.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from plugins.life_engine.core.context_stewardship import (
    HEARTBEAT_CHECKPOINT_FEEDBACK_TEXT,
    is_compression_required_part,
)
from plugins.life_engine.service.core import LifeEngineService
from plugins.life_engine.service.heartbeat_rolling import (
    copy_rolling_payloads,
    snapshot_dict,
)
from src.kernel.llm import ROLE, LLMPayload, Text, ToolResult
from src.kernel.llm.context import LLMContextManager
from src.kernel.llm.exceptions import LLMContextError, LLMTimeoutError
from src.kernel.llm.policy import create_policy
from src.kernel.llm.request import LLMRequest
from src.kernel.llm.response import LLMResponse
from test.plugins.life_engine.test_heartbeat_compression_recovery import (
    _prepared,
    _ScriptedRequest,
)
from test.plugins.life_engine.test_heartbeat_followup_projection_loss import (
    _DELIVERY_ID,
    _MARKER,
    _WAKE,
    _characters,
    _initial_payloads,
    _manager,
    _receipt,
)


class _Client:
    def __init__(self, outcomes=()):
        self.outcomes = list(outcomes)
        self.sent = []

    async def create(self, *, payloads, **_kwargs):
        self.sent.append(copy_rolling_payloads(payloads))
        outcome = self.outcomes.pop(0) if self.outcomes else ("synthetic response", None, None)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


@pytest.fixture
def local_kernel(monkeypatch):
    client = _Client()
    registry = SimpleNamespace(get_client_for_model=lambda _model: client)
    monkeypatch.setattr("src.kernel.llm.request.get_default_model_client_registry", lambda: registry)
    monkeypatch.setattr("src.kernel.llm.request.record_trajectory", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("src.kernel.llm.request.count_payload_tokens", lambda payloads, **_kwargs: _characters(payloads))
    monkeypatch.setattr(LLMResponse, "attach_to_inspector", lambda _self: None)
    return client


def _model(budget, identifier="synthetic-protected-model"):
    return {
        "api_provider": "openai", "client_type": "openai",
        "model_identifier": identifier, "api_key": "synthetic-test-key",
        "base_url": "https://example.invalid", "max_context": 1000000,
        "max_tokens": 1, "context_tokens": budget, "max_retry": 0,
        "retry_interval": 0, "timeout": 2,
        "price_in": 0, "price_out": 0, "temperature": 0.7,
        "extra_params": {"context_reserve_ratio": 0, "context_reserve_tokens": 0},
    }


def _request(budget, *, models=None):
    request = LLMRequest(
        models or [_model(budget)], "synthetic-heartbeat-protected",
        context_manager=_manager(), policy=create_policy("load_balanced"),
        enable_metrics=False,
    )
    request.payloads = _initial_payloads()
    _register(request)
    return request


def _register(holder):
    holder.register_context_delivery(_DELIVERY_ID, _WAKE, marker=_MARKER)
    LifeEngineService._protect_heartbeat_exact_projection(holder, _WAKE)


def test_heartbeat_protection_rebuilds_only_current_user_controls_and_wake():
    holder = SimpleNamespace(context_manager=LLMContextManager(), payloads=_initial_payloads())
    control = next(
        part for payload in holder.payloads for part in payload.content
        if is_compression_required_part(part)
    )
    LifeEngineService._protect_heartbeat_exact_projection(holder, _WAKE)
    assert holder.context_manager.protected_exact_texts == {_WAKE, control.text}
    assert holder.context_manager.reprojectable_exact_texts == {control.text}
    assert _WAKE not in repr(holder.context_manager)

    holder.payloads = [
        LLMPayload(ROLE.USER, Text(_WAKE)),
        LLMPayload(ROLE.ASSISTANT, Text(control.text)),
    ]
    LifeEngineService._protect_heartbeat_exact_projection(holder, _WAKE)
    assert holder.context_manager.protected_exact_texts == {_WAKE}
    assert holder.context_manager.reprojectable_exact_texts == frozenset()
    assert LLMContextManager().protected_exact_texts == frozenset()


def test_old_control_can_reproject_without_pinning_its_large_historical_group():
    initial = _initial_payloads()
    control = next(
        part for payload in initial for part in payload.content
        if is_compression_required_part(part)
    )
    payloads = [
        initial[0],
        LLMPayload(ROLE.USER, [Text("synthetic old large group " * 2000), control]),
        LLMPayload(ROLE.ASSISTANT, Text("synthetic old response")),
        LLMPayload(ROLE.USER, Text("synthetic intermediate old group " * 2000)),
        LLMPayload(ROLE.ASSISTANT, Text("synthetic intermediate response")),
        LLMPayload(ROLE.USER, Text(_WAKE)),
    ]
    holder = SimpleNamespace(context_manager=_manager(), payloads=payloads)
    before = snapshot_dict(payloads)
    LifeEngineService._protect_heartbeat_exact_projection(holder, _WAKE)
    budget = _characters([initial[0], LLMPayload(ROLE.USER, [Text(_WAKE), control])]) + 20

    effective = holder.context_manager.maybe_trim(
        payloads, max_token_budget=budget, token_counter=_characters,
    )

    holder.context_manager.validate_for_send(effective)
    assert _characters(effective) <= budget
    assert _receipt(effective).exact_present is True
    assert [
        part.text for payload in effective for part in payload.content
        if is_compression_required_part(part)
    ] == [control.text]
    assert snapshot_dict(payloads) == before


async def test_actual_followup_attempt_retains_exact_wake_and_control(local_kernel):
    initial = _initial_payloads()
    budget = _characters([initial[0], initial[-1]]) + 200
    request = _request(budget)
    response = await request.send(stream=False)
    first_receipt = response.effective_context_receipt(_DELIVERY_ID)
    assert first_receipt.exact_present is True
    await response
    response.add_payload(LLMPayload(ROLE.USER, Text(HEARTBEAT_CHECKPOINT_FEEDBACK_TEXT)))
    _register(response)
    before = snapshot_dict(response.payloads)

    followup = await response.send(stream=False)

    receipt = followup.effective_context_receipt(_DELIVERY_ID)
    assert receipt is not first_receipt
    assert receipt.exact_present is True
    assert receipt.expected_sha256 == receipt.effective_sha256
    assert receipt.expected_utf8_bytes == receipt.effective_utf8_bytes
    assert _receipt(local_kernel.sent[-1]).exact_present is True
    assert snapshot_dict(response.payloads) == before
    assert all(
        any(isinstance(part, Text) and part.text == text for payload in local_kernel.sent[-1] for part in payload.content)
        for text in response.context_manager.protected_exact_texts
    )


@pytest.mark.parametrize("result_fits", [True, False])
async def test_structured_followup_fits_exactly_or_fails_before_client(local_kernel, result_fits):
    initial = _initial_payloads()
    budget = _characters([initial[0], initial[-1]]) + (11000 if result_fits else 200)
    call = {"id": "synthetic-read", "name": "read_context_group", "args": {}}
    local_kernel.outcomes = [("", [call], None)]
    response = await _request(budget).send(stream=False)
    first_receipt = response.effective_context_receipt(_DELIVERY_ID)
    await response
    response.add_payload(LLMPayload(ROLE.TOOL_RESULT, ToolResult(
        call_id=call["id"], name=call["name"], value={"synthetic_raw": "r" * 10000},
    )))
    _register(response)
    before = snapshot_dict(response.payloads)

    if result_fits:
        final_response = await response.send(stream=False)
        final_receipt = final_response.effective_context_receipt(_DELIVERY_ID)
        assert final_receipt is not first_receipt
        assert final_receipt.exact_present is True
        assert len(local_kernel.sent) == 2
    else:
        with pytest.raises(LLMContextError, match="protected exact delivery text"):
            await response.send(stream=False)
        assert len(local_kernel.sent) == 1
    assert response.effective_context_receipt(_DELIVERY_ID) is first_receipt
    assert snapshot_dict(response.payloads) == before


@pytest.mark.parametrize("retry_fits", [True, False])
async def test_retry_model_must_independently_fit_protected_delivery(local_kernel, retry_fits):
    initial = _initial_payloads()
    exact_size = _characters([initial[0], initial[-1]])
    local_kernel.outcomes = [LLMTimeoutError("synthetic first attempt failed")]
    second_budget = exact_size + 200 if retry_fits else 100
    request = _request(exact_size + 200, models=[
        _model(exact_size + 200, "synthetic-first-model"),
        _model(second_budget, "synthetic-second-model"),
    ])
    before = snapshot_dict(request.payloads)
    if retry_fits:
        response = await request.send(stream=False)
        assert len(local_kernel.sent) == 2
        assert response.effective_context_receipt(_DELIVERY_ID).exact_present is True
        assert _receipt(local_kernel.sent[-1]).exact_present is True
    else:
        with pytest.raises(LLMContextError, match="protected exact delivery text"):
            await request.send(stream=False)
        assert len(local_kernel.sent) == 1
    assert snapshot_dict(request.payloads) == before


async def test_followup_cancellation_keeps_protection_and_current_expectation(local_kernel):
    initial = _initial_payloads()
    request = _request(_characters([initial[0], initial[-1]]) + 500)
    response = await request.send(stream=False)
    await response
    _register(response)
    cancellation = asyncio.CancelledError("synthetic cancellation")
    local_kernel.outcomes = [cancellation]
    before = snapshot_dict(response.payloads)

    with pytest.raises(asyncio.CancelledError) as raised:
        await response.send(stream=False)

    assert raised.value is cancellation
    assert _WAKE in response.context_manager.protected_exact_texts
    assert _DELIVERY_ID in response._context_delivery_expectations
    assert snapshot_dict(response.payloads) == before


class _BudgetedScriptedRequest(_ScriptedRequest):
    def __init__(self, budget):
        super().__init__()
        self.budget = budget

    async def respond(self, payloads):
        self.context_manager.maybe_trim(
            payloads, max_token_budget=self.budget, token_counter=_characters,
        )
        return await super().respond(payloads)


async def test_initial_protected_overflow_returns_unresolved_without_model_or_fallback(tmp_path, monkeypatch):
    request = _BudgetedScriptedRequest(100)
    service, baseline, path, _ = await _prepared(tmp_path, monkeypatch, request)

    result = await service._run_heartbeat_model(_WAKE, heartbeat_run_id="synthetic-protected-overflow")

    assert request.sent_rounds == []
    assert result.compression_unresolved is True
    assert result.subconscious_receipt is None
    assert result.text == ""
    assert path.exists()
    assert _WAKE not in str(snapshot_dict(result.rolling_payloads))
    assert snapshot_dict(baseline) == snapshot_dict(list(result.rolling_payloads))


async def test_followup_protected_overflow_clears_old_receipt_and_preserves_pending(tmp_path, monkeypatch):
    class _FollowupOverflowRequest(_BudgetedScriptedRequest):
        async def respond(self, payloads):
            self.budget = 100 if self.sent_rounds else 1000000
            return await super().respond(payloads)

    request = _FollowupOverflowRequest(1000000)
    service, baseline, _, _ = await _prepared(tmp_path, monkeypatch, request)

    result = await service._run_heartbeat_model(_WAKE, heartbeat_run_id="synthetic-followup-overflow")

    assert len(request.sent_rounds) == 1
    assert result.compression_unresolved is True
    assert result.subconscious_receipt is None
    assert result.text == ""
    assert snapshot_dict(baseline) == snapshot_dict(list(result.rolling_payloads))


async def test_utility_fallback_gets_its_own_exact_projection_protection(tmp_path, monkeypatch):
    class _FailingRequest(_ScriptedRequest):
        async def send(self, *, stream=False):
            assert _WAKE in self.context_manager.protected_exact_texts
            raise RuntimeError("synthetic primary failure")

    primary = _FailingRequest()
    fallback = _ScriptedRequest()
    service, _, _, _ = await _prepared(tmp_path, monkeypatch, primary, compression=False)
    requests = iter([primary, fallback])
    monkeypatch.setattr(
        "plugins.life_engine.service.core.create_llm_request",
        lambda **_kwargs: next(requests),
    )

    result = await service._run_heartbeat_model(_WAKE, heartbeat_run_id="synthetic-protected-fallback")

    assert _WAKE in primary.context_manager.protected_exact_texts
    assert _WAKE in fallback.context_manager.protected_exact_texts
    assert primary.context_manager is not fallback.context_manager
    assert len(fallback.sent_rounds) == 1
    assert result.compression_unresolved is False
    assert result.subconscious_receipt is not None
