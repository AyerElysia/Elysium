"""Synthetic regressions for opt-in, byte-exact transient context protection."""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from src.kernel.llm.context import LLMContextManager
from src.kernel.llm.context_delivery import (
    ContextDeliveryExpectation,
    build_effective_context_receipts,
)
from src.kernel.llm.exceptions import LLMContextError
from src.kernel.llm.payload import LLMPayload, Text, ToolCall, ToolResult
from src.kernel.llm.request import LLMRequest
from src.kernel.llm.roles import ROLE

WAKE = "[delivery:synthetic-wake]\n完整唤醒正文\nKeep these exact bytes."
CONTROL = "[compression:synthetic-control]\nCurrent technical control only."


class SyntheticTool:
    @classmethod
    def to_schema(cls) -> dict[str, Any]:
        return {"name": "synthetic_tool"}


def _texts(payloads: list[LLMPayload]) -> list[str]:
    return [
        part.text
        for payload in payloads
        for part in payload.content
        if isinstance(part, Text)
    ]


def _cost(payloads: list[LLMPayload]) -> int:
    """A deterministic local counter; no tokenizer or model is initialized."""
    return sum(
        len(part.text)
        if isinstance(part, Text)
        else len(part.to_text())
        if isinstance(part, ToolResult)
        else 10
        for payload in payloads
        for part in payload.content
    )


def _trim(
    manager: LLMContextManager, payloads: list[LLMPayload], budget: int
) -> list[LLMPayload]:
    return manager.maybe_trim(payloads, max_token_budget=budget, token_counter=_cost)


def _wake_receipt(payloads: list[LLMPayload]):
    expectation = ContextDeliveryExpectation.create(
        "synthetic-wake", WAKE, marker="[delivery:synthetic-wake]"
    )
    return build_effective_context_receipts(
        {expectation.delivery_id: expectation}, payloads
    )[expectation.delivery_id]


def test_protected_texts_default_empty_and_hidden_from_repr() -> None:
    assert LLMContextManager().protected_exact_texts == frozenset()
    assert LLMContextManager().reprojectable_exact_texts == frozenset()
    manager = LLMContextManager(
        protected_exact_texts=frozenset({WAKE, CONTROL}),
        reprojectable_exact_texts=frozenset({CONTROL}),
    )
    assert WAKE not in repr(manager)
    assert CONTROL not in repr(manager)


def test_default_manager_still_drops_old_groups() -> None:
    payloads = [
        LLMPayload(ROLE.USER, Text(WAKE)),
        LLMPayload(ROLE.ASSISTANT, Text("old answer")),
        LLMPayload(ROLE.USER, Text("latest")),
    ]
    assert _texts(_trim(LLMContextManager(), payloads, 10)) == ["latest"]


def test_default_manager_still_clips_oversized_last_group() -> None:
    original = "head:" + "x" * 600 + ":tail"
    payloads = [LLMPayload(ROLE.USER, Text(original))]
    trimmed = _trim(LLMContextManager(), payloads, 100)
    assert _cost(trimmed) <= 100
    assert _texts(trimmed) != [original]
    assert "context omitted to fit the task token budget" in _texts(trimmed)[0]
    assert _texts(payloads) == [original]


@pytest.mark.parametrize("budget", [None, 0, 10000])
def test_no_trim_path_preserves_payload_identity(budget: int | None) -> None:
    manager = LLMContextManager(protected_exact_texts=frozenset({WAKE}))
    payloads = [LLMPayload(ROLE.USER, Text(WAKE))]
    assert (
        manager.maybe_trim(payloads, max_token_budget=budget, token_counter=_cost)
        is payloads
    )
    assert _wake_receipt(payloads).exact_present is True


def test_feedback_across_groups_preserves_wake_control_and_tool_chain() -> None:
    manager = LLMContextManager(protected_exact_texts=frozenset({WAKE, CONTROL}))
    call = ToolCall(id="synthetic-call", name="synthetic_tool", args={"x": 1})
    result = ToolResult(value={"ok": True}, call_id=call.id, name=call.name)
    payloads = [
        LLMPayload(ROLE.SYSTEM, Text("pinned system")),
        LLMPayload(ROLE.TOOL, SyntheticTool),
        LLMPayload(ROLE.USER, Text("obsolete" * 100)),
        LLMPayload(ROLE.ASSISTANT, Text("obsolete answer")),
        LLMPayload(ROLE.USER, Text(WAKE)),
        LLMPayload(ROLE.ASSISTANT, Text("first answer " * 100)),
        LLMPayload(ROLE.USER, Text(CONTROL)),
        LLMPayload(ROLE.ASSISTANT, [Text("call explanation " * 100), call]),
        LLMPayload(ROLE.TOOL_RESULT, result),
        LLMPayload(ROLE.USER, Text("follow-up feedback")),
    ]
    before = deepcopy(payloads)
    trimmed = _trim(manager, payloads, 400)

    assert _cost(trimmed) <= 400
    assert _texts(trimmed).count(WAKE) == 1
    assert _texts(trimmed).count(CONTROL) == 1
    assert "follow-up feedback" in _texts(trimmed)
    assert "obsolete answer" not in _texts(trimmed)
    assert [payload.role for payload in trimmed] == [
        ROLE.SYSTEM,
        ROLE.TOOL,
        ROLE.USER,
        ROLE.ASSISTANT,
        ROLE.USER,
        ROLE.ASSISTANT,
        ROLE.TOOL_RESULT,
        ROLE.USER,
    ]
    assert trimmed[0].content == payloads[0].content
    assert trimmed[1].content == payloads[1].content
    assert call in trimmed[5].content
    assert trimmed[6].content == [result]
    manager.validate_for_send(trimmed)
    receipt = _wake_receipt(trimmed)
    assert receipt.exact_present is True
    assert receipt.effective_utf8_bytes == len(WAKE.encode("utf-8"))
    assert receipt.effective_sha256 == receipt.expected_sha256
    assert payloads == before


def test_protected_prefix_barrier_keeps_later_groups_in_original_order() -> None:
    manager = LLMContextManager(protected_exact_texts=frozenset({WAKE}))
    middle = "middle-head:" + "m" * 500 + ":middle-tail"
    payloads = [
        LLMPayload(ROLE.USER, Text(WAKE)),
        LLMPayload(ROLE.USER, Text(middle)),
        LLMPayload(ROLE.USER, Text("latest")),
    ]
    trimmed = _trim(manager, payloads, 180)
    assert len(trimmed) == 3
    assert _texts(trimmed)[0] == WAKE
    assert "context omitted to fit the task token budget" in _texts(trimmed)[1]
    assert _texts(trimmed)[2] == "latest"
    assert _texts(payloads) == [WAKE, middle, "latest"]


def test_hook_overhead_cannot_drop_protected_group_in_second_pass() -> None:
    seen: list[tuple[list[str], list[str]]] = []

    def hook(dropped, remaining):
        seen.append(
            (_texts([item for group in dropped for item in group]), _texts(remaining))
        )
        return [LLMPayload(ROLE.ASSISTANT, Text("summary " * 100))]

    manager = LLMContextManager(
        compression_hook=hook, protected_exact_texts=frozenset({WAKE, CONTROL})
    )
    payloads = [
        LLMPayload(ROLE.USER, Text("obsolete " * 100)),
        LLMPayload(ROLE.USER, Text(WAKE)),
        LLMPayload(ROLE.ASSISTANT, Text("answer")),
        LLMPayload(ROLE.USER, Text(CONTROL)),
        LLMPayload(ROLE.USER, Text("feedback")),
    ]
    before = deepcopy(payloads)
    trimmed = _trim(manager, payloads, 250)
    assert _cost(trimmed) <= 250
    assert len(seen) == 1
    assert seen[0][0] == ["obsolete " * 100]
    assert seen[0][1] == [WAKE, "answer", CONTROL, "feedback"]
    assert _texts(trimmed)[1:] == [WAKE, "answer", CONTROL, "feedback"]
    assert _wake_receipt(trimmed).exact_present is True
    assert payloads == before


def test_no_hook_is_called_when_no_unprotected_prefix_can_be_dropped() -> None:
    called = []

    def hook(dropped, remaining):
        called.append((dropped, remaining))
        return []

    manager = LLMContextManager(
        compression_hook=hook, protected_exact_texts=frozenset({WAKE})
    )
    payloads = [
        LLMPayload(ROLE.USER, Text(WAKE)),
        LLMPayload(ROLE.USER, Text("unprotected long feedback " * 100)),
    ]
    trimmed = _trim(manager, payloads, 200)
    assert _texts(trimmed)[0] == WAKE
    assert len(trimmed) == 2
    assert called == []


@pytest.mark.parametrize(
    "changed",
    [WAKE + " changed", "prefix " + WAKE, WAKE.replace("正文", "改写"), WAKE + "\n"],
)
def test_same_marker_with_different_text_is_not_protected(changed: str) -> None:
    manager = LLMContextManager(protected_exact_texts=frozenset({WAKE}))
    payloads = [
        LLMPayload(ROLE.USER, Text(changed)),
        LLMPayload(ROLE.USER, Text("latest")),
    ]
    assert "[delivery:synthetic-wake]" in changed
    assert _texts(_trim(manager, payloads, 10)) == ["latest"]


def test_unregistered_marker_text_can_still_be_clipped_in_last_group() -> None:
    changed = WAKE + "unregistered continuation " * 100
    manager = LLMContextManager(protected_exact_texts=frozenset({WAKE}))
    payloads = [LLMPayload(ROLE.USER, Text(changed))]
    trimmed = _trim(manager, payloads, 120)
    assert _cost(trimmed) <= 120
    assert _texts(trimmed) != [changed]
    assert _texts(payloads) == [changed]


def test_duplicate_exact_parts_are_not_deduplicated_and_receipt_rejects() -> None:
    manager = LLMContextManager(protected_exact_texts=frozenset({WAKE}))
    payloads = [
        LLMPayload(ROLE.USER, Text(WAKE)),
        LLMPayload(ROLE.ASSISTANT, Text("answer " * 100)),
        LLMPayload(ROLE.USER, Text(WAKE)),
        LLMPayload(ROLE.USER, Text("feedback")),
    ]
    trimmed = _trim(manager, payloads, 230)
    assert _cost(trimmed) <= 230
    assert _texts(trimmed).count(WAKE) == 2
    receipt = _wake_receipt(trimmed)
    assert receipt.exact_present is False
    assert receipt.effective_utf8_bytes is None
    assert receipt.effective_sha256 is None


def test_oversized_protected_text_fails_without_mutation_or_content_leak() -> None:
    manager = LLMContextManager(protected_exact_texts=frozenset({WAKE}))
    payloads = [LLMPayload(ROLE.USER, Text(WAKE))]
    before = deepcopy(payloads)
    with pytest.raises(LLMContextError, match="protected exact delivery text") as exc:
        _trim(manager, payloads, len(WAKE) - 1)
    assert WAKE not in str(exc.value)
    assert payloads == before


def test_protected_text_and_structured_result_fail_closed_when_they_cannot_fit() -> (
    None
):
    manager = LLMContextManager(protected_exact_texts=frozenset({WAKE}))
    call = ToolCall(id="synthetic-large", name="synthetic_tool", args={})
    result = ToolResult(
        value="structured-private-result:" + "r" * 1000, call_id=call.id
    )
    payloads = [
        LLMPayload(ROLE.USER, Text(WAKE)),
        LLMPayload(ROLE.ASSISTANT, call),
        LLMPayload(ROLE.TOOL_RESULT, result),
        LLMPayload(ROLE.USER, Text("feedback")),
    ]
    before = deepcopy(payloads)
    manager.validate_for_send(payloads)
    with pytest.raises(LLMContextError, match="protected exact delivery text") as exc:
        _trim(manager, payloads, 150)
    assert WAKE not in str(exc.value)
    assert "structured-private-result" not in str(exc.value)
    assert payloads == before
    assert payloads[2].content == [result]


@pytest.mark.asyncio
@pytest.mark.parametrize("with_structured_result", [False, True])
async def test_unfit_protected_request_fails_before_provider_selection_or_send(
    monkeypatch: pytest.MonkeyPatch, with_structured_result: bool
) -> None:
    model = {
        "api_provider": "openai",
        "base_url": "https://unused.invalid/v1",
        "model_identifier": "synthetic-no-send",
        "api_key": "synthetic-unused-key",
        "client_type": "openai",
        "max_retry": 0,
        "timeout": 1,
        "retry_interval": 0,
        "price_in": 0,
        "price_out": 0,
        "temperature": 0,
        "max_tokens": 10,
        "max_context": 1000,
        "context_tokens": len(WAKE) - 1,
        "extra_params": {},
    }
    payloads = [LLMPayload(ROLE.USER, Text(WAKE))]
    if with_structured_result:
        call = ToolCall(id="synthetic-call", name="synthetic_tool", args={})
        payloads.extend(
            [
                LLMPayload(ROLE.ASSISTANT, call),
                LLMPayload(ROLE.TOOL_RESULT, ToolResult("r" * 1000, call_id=call.id)),
            ]
        )
    before = deepcopy(payloads)
    client = Mock(create=AsyncMock())
    clients = Mock()
    clients.get_client_for_model.return_value = client
    policy = Mock()
    policy.new_session.return_value.first.return_value = SimpleNamespace(
        model=model, meta={}, delay_seconds=0
    )
    record = Mock()
    monkeypatch.setattr("src.kernel.llm.request.logger", Mock())
    monkeypatch.setattr("src.kernel.llm.request.record_trajectory", record)
    monkeypatch.setattr(
        "src.kernel.llm.request._trajectory_settings",
        lambda: (False, "unused", 1.0, 1, 1, 1),
    )
    monkeypatch.setattr(
        "src.kernel.llm.request.count_payload_tokens",
        lambda items, model_identifier: _cost(items),
    )
    request = LLMRequest(
        [model],
        "synthetic-protected-no-send",
        payloads=payloads,
        clients=clients,
        policy=policy,
        context_manager=LLMContextManager(protected_exact_texts=frozenset({WAKE})),
        enable_metrics=False,
    )
    request.register_context_delivery(
        "synthetic-wake", WAKE, marker="[delivery:synthetic-wake]"
    )

    with pytest.raises(LLMContextError, match="protected exact delivery text"):
        await request.send(stream=False)

    clients.get_client_for_model.assert_not_called()
    client.create.assert_not_called()
    record.assert_not_called()
    assert request.payloads == before


def test_old_control_group_can_be_dropped_when_hook_reprojects_exact_control() -> None:
    seen: list[tuple[list[str], list[str]]] = []

    def hook(dropped, remaining):
        seen.append(
            (_texts([item for group in dropped for item in group]), _texts(remaining))
        )
        return [LLMPayload(ROLE.USER, Text(CONTROL))]

    manager = LLMContextManager(
        compression_hook=hook,
        protected_exact_texts=frozenset({WAKE, CONTROL}),
        reprojectable_exact_texts=frozenset({CONTROL}),
    )
    call = ToolCall(id="synthetic-obsolete", name="synthetic_tool", args={})
    payloads = [
        LLMPayload(ROLE.USER, Text(CONTROL)),
        LLMPayload(ROLE.ASSISTANT, call),
        LLMPayload(ROLE.TOOL_RESULT, ToolResult("r" * 1000, call_id=call.id)),
        LLMPayload(ROLE.USER, Text(WAKE)),
        LLMPayload(ROLE.USER, Text("feedback")),
    ]
    before = deepcopy(payloads)
    manager.validate_for_send(payloads)
    trimmed = _trim(manager, payloads, 180)

    assert _cost(trimmed) <= 180
    assert seen == [([CONTROL], [WAKE, "feedback"])]
    assert _texts(trimmed) == [CONTROL, WAKE, "feedback"]
    assert [payload.role for payload in trimmed] == [ROLE.USER] * 3
    assert _texts(trimmed).count(CONTROL) == _texts(payloads).count(CONTROL) == 1
    assert _wake_receipt(trimmed).exact_present is True
    manager.validate_for_send(trimmed)
    assert payloads == before


@pytest.mark.parametrize(
    "replacement", [[], [CONTROL + " changed"], [CONTROL, CONTROL]]
)
def test_missing_changed_or_duplicated_control_reprojection_fails_closed(
    replacement: list[str],
) -> None:
    def hook(dropped, remaining):
        del dropped, remaining
        return [LLMPayload(ROLE.USER, Text(text)) for text in replacement]

    manager = LLMContextManager(
        compression_hook=hook,
        protected_exact_texts=frozenset({WAKE, CONTROL}),
        reprojectable_exact_texts=frozenset({CONTROL}),
    )
    payloads = [
        LLMPayload(ROLE.USER, Text(CONTROL)),
        LLMPayload(ROLE.ASSISTANT, Text("obsolete " * 100)),
        LLMPayload(ROLE.USER, Text(WAKE)),
        LLMPayload(ROLE.USER, Text("feedback")),
    ]
    before = deepcopy(payloads)
    with pytest.raises(LLMContextError) as exc:
        _trim(manager, payloads, 250)
    assert CONTROL not in str(exc.value)
    assert WAKE not in str(exc.value)
    assert payloads == before


def test_reprojectable_exact_control_is_never_a_text_clipping_candidate() -> None:
    manager = LLMContextManager(
        protected_exact_texts=frozenset({CONTROL}),
        reprojectable_exact_texts=frozenset({CONTROL}),
    )
    ordinary = "ordinary context " * 100
    payloads = [LLMPayload(ROLE.USER, [Text(CONTROL), Text(ordinary)])]
    before = deepcopy(payloads)
    trimmed = _trim(manager, payloads, len(CONTROL) + 90)

    assert _cost(trimmed) <= len(CONTROL) + 90
    assert _texts(trimmed)[0] == CONTROL
    assert _texts(trimmed)[1] != ordinary
    assert payloads == before
    with pytest.raises(LLMContextError) as exc:
        _trim(manager, payloads, len(CONTROL) - 1)
    assert CONTROL not in str(exc.value)
    assert payloads == before
