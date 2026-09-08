"""Synthetic diagnosis of strict heartbeat follow-up projection rejection.

These tests exercise real context trimming and receipt construction only.
No model, service, runtime store, formal data, or consumer cursor is used.
"""

from __future__ import annotations

from types import SimpleNamespace

from plugins.life_engine.core.context_stewardship import (
    HEARTBEAT_CHECKPOINT_FEEDBACK_TEXT,
    ensure_compression_required_appended,
    has_compression_required_payload,
    install_subject_context_recovery_hook,
)
from plugins.life_engine.service.core import LifeEngineService
from plugins.life_engine.service.heartbeat_rolling import (
    copy_rolling_payloads,
    snapshot_dict,
)
from src.kernel.llm import ROLE, LLMPayload, Text, ToolCall, ToolResult
from src.kernel.llm.context import LLMContextManager
from src.kernel.llm.context_delivery import (
    ContextDeliveryExpectation,
    build_effective_context_receipts,
)

_DELIVERY_ID = "synthetic-followup-projection"
_MARKER = f'<subconscious_activity_projection delivery_id="{_DELIVERY_ID}">'
_WAKE = f"{_MARKER}\n" + "synthetic exact experience " * 800 + "\n</subconscious_activity_projection>"


def _characters(payloads):
    return sum(
        len(
            part.text if isinstance(part, Text)
            else part.to_text() if isinstance(part, ToolResult)
            else str(part)
        )
        for payload in payloads
        for part in payload.content
    )


def _receipt(payloads):
    expectation = ContextDeliveryExpectation.create(
        _DELIVERY_ID, _WAKE, marker=_MARKER,
    )
    return build_effective_context_receipts({_DELIVERY_ID: expectation}, payloads)[
        _DELIVERY_ID
    ]


def _manager():
    manager = LLMContextManager()
    install_subject_context_recovery_hook(SimpleNamespace(context_manager=manager))
    return manager


def _initial_payloads():
    return ensure_compression_required_appended(
        [
            LLMPayload(ROLE.SYSTEM, Text("synthetic system")),
            LLMPayload(ROLE.USER, Text("synthetic old group " * 2000)),
            LLMPayload(ROLE.ASSISTANT, Text("synthetic old response")),
            LLMPayload(ROLE.USER, Text(_WAKE)),
        ],
        trigger_chars=1,
        force=True,
    )


def test_initial_emergency_projection_preserves_exact_current_wake():
    manager = _manager()
    initial = _initial_payloads()
    before = snapshot_dict(initial)
    budget = _characters([initial[0], initial[-1]]) + 20

    effective = manager.maybe_trim(
        initial, max_token_budget=budget, token_counter=_characters,
    )

    manager.validate_for_send(effective)
    assert has_compression_required_payload(effective)
    assert _receipt(effective).exact_present is True
    assert snapshot_dict(initial) == before


def test_feedback_user_turn_can_make_exact_wake_an_omitted_old_group():
    manager = _manager()
    initial = _initial_payloads()
    budget = _characters([initial[0], initial[-1]]) + 20
    assert _receipt(manager.maybe_trim(
        initial, max_token_budget=budget, token_counter=_characters,
    )).exact_present is True
    followup = copy_rolling_payloads(initial)
    followup = manager.add_payload(
        followup, LLMPayload(ROLE.ASSISTANT, Text("synthetic nonterminal narrative")),
    )
    followup = manager.add_payload(
        followup, LLMPayload(ROLE.USER, Text(HEARTBEAT_CHECKPOINT_FEEDBACK_TEXT)),
    )
    before = snapshot_dict(followup)

    effective = manager.maybe_trim(
        followup, max_token_budget=budget, token_counter=_characters,
    )

    manager.validate_for_send(effective)
    assert has_compression_required_payload(effective)
    assert _receipt(followup).exact_present is True
    receipt = _receipt(effective)
    assert receipt.exact_present is False
    assert receipt.effective_utf8_bytes is None
    assert receipt.effective_sha256 is None
    assert snapshot_dict(followup) == before
    response = SimpleNamespace(effective_context_receipt=lambda _identity: receipt)
    assert LifeEngineService._heartbeat_subconscious_receipt(
        response, _DELIVERY_ID,
    ) is None


def test_large_tool_result_can_trim_exact_wake_inside_last_indivisible_group():
    manager = _manager()
    initial = _initial_payloads()
    budget = _characters([initial[0], initial[-1]]) + 20
    call = ToolCall(id="synthetic-read-call", name="read_context_group", args={})
    result = ToolResult(
        call_id=call.id, name=call.name,
        value={"synthetic_exact_archive": "synthetic tool result " * 500},
    )
    followup = copy_rolling_payloads(initial)
    followup = manager.add_payload(followup, LLMPayload(ROLE.ASSISTANT, [call]))
    followup = manager.add_payload(followup, LLMPayload(ROLE.TOOL_RESULT, [result]))
    before = snapshot_dict(followup)

    effective = manager.maybe_trim(
        followup, max_token_budget=budget, token_counter=_characters,
    )

    manager.validate_for_send(effective)
    assert has_compression_required_payload(effective)
    assert _receipt(followup).exact_present is True
    receipt = _receipt(effective)
    assert receipt.exact_present is False
    assert receipt.effective_utf8_bytes is not None
    assert receipt.effective_utf8_bytes < receipt.expected_utf8_bytes
    assert receipt.effective_sha256 != receipt.expected_sha256
    assert any(
        isinstance(part, ToolResult) and part.to_text() == result.to_text()
        for payload in effective for part in payload.content
    )
    assert snapshot_dict(followup) == before
    response = SimpleNamespace(effective_context_receipt=lambda _identity: receipt)
    assert LifeEngineService._heartbeat_subconscious_receipt(
        response, _DELIVERY_ID,
    ) is None
