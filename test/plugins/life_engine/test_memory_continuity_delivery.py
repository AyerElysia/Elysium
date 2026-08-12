"""Exact-delivery contracts for continuity-review candidates."""

from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from typing import Any

import pytest

from plugins.life_engine.learning.decisions import LearningCandidate
from plugins.life_engine.memory.continuity_delivery import (
    ContinuityCandidateDeliveryConflict,
    ContinuityCandidateDeliveryCoordinator,
    ContinuityCandidateDeliveryInputError,
    get_memory_continuity_delivery_coordinator,
)
from plugins.life_engine.memory.continuity_session import CandidateDeliveryReceipt
from src.kernel.llm.context_delivery import EffectiveContextReceipt
from src.kernel.llm.payload import ToolResult


class _Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def _candidate(
    content: bytes,
    *,
    candidate_id: str = "candidate-one",
    revision: int = 1,
) -> LearningCandidate:
    return LearningCandidate.create(
        candidate_id=candidate_id,
        candidate_revision=revision,
        candidate_kind="continuity_memory_review",
        candidate_occurrence_id=f"candidate-occurrence:{candidate_id}:{revision}",
        candidate_content_bytes=content,
        source_occurrence_id="source-occurrence",
        source="continuity-review",
        actor_consciousness_instance_id="consciousness-one",
        subject_revision="a" * 64,
        target_path="MEMORY.md",
        occurred_at="2026-08-12T00:00:00+00:00",
    )


def _page_payload(
    candidate: LearningCandidate,
    *,
    delivery_id: str,
    offset: int,
    end: int,
    page_content: bytes | None = None,
) -> dict[str, Any]:
    content = candidate.candidate_content_bytes
    delivered = bytes(content[offset:end] if page_content is None else page_content)
    text = delivered.decode("utf-8")
    page_sha256 = hashlib.sha256(delivered).hexdigest()
    next_offset = offset + len(delivered)
    if next_offset == len(content):
        next_offset = None
    payload: dict[str, Any] = {
        "action": "candidate_read",
        "session_id": "continuity-review-" + "b" * 32,
        "candidate_id": candidate.candidate_id,
        "candidate_revision": candidate.candidate_revision,
        "candidate_sha256": candidate.candidate_sha256,
        "subject_revision": candidate.subject_revision,
        "page": {
            "offset": offset,
            "next_offset": next_offset,
            "delivered_bytes": len(delivered),
            "total_bytes": len(content),
            "page_sha256": page_sha256,
            "text": text,
        },
        "delivery_binding": {
            "delivery_id": delivery_id,
            "candidate_id": candidate.candidate_id,
            "candidate_revision": candidate.candidate_revision,
            "candidate_sha256": candidate.candidate_sha256,
            "page_offset": offset,
            "page_sha256": page_sha256,
            "page_bytes": len(delivered),
            "total_bytes": len(content),
        },
        "delivery_binding_is_not_receipt": True,
        "authority_written": False,
    }
    return payload


def _register(
    coordinator: ContinuityCandidateDeliveryCoordinator,
    payload: dict[str, Any],
):
    result = ToolResult(
        value=payload,
        call_id="call-continuity-read",
        name="tool-nucleus_review_memory_continuity",
    )
    expectation = coordinator.register_pending_tool_result(payload, result.to_text())
    assert expectation.delivery_id == payload["delivery_binding"]["delivery_id"]
    assert expectation.expected_text == result.to_text()
    assert expectation.marker == expectation.delivery_id
    assert expectation.part_kind == "tool_result"
    return expectation


def _effective_receipt(expectation, **overrides: Any) -> EffectiveContextReceipt:
    values: dict[str, Any] = {
        "delivery_id": expectation.delivery_id,
        "exact_present": True,
        "expected_utf8_bytes": expectation.expected_utf8_bytes,
        "expected_sha256": expectation.expected_sha256,
        "effective_utf8_bytes": expectation.expected_utf8_bytes,
        "effective_sha256": expectation.expected_sha256,
        "part_kind": "tool_result",
    }
    values.update(overrides)
    return EffectiveContextReceipt(**values)


def _claim(
    candidate: LearningCandidate,
    delivery_id: str,
    **overrides: Any,
) -> CandidateDeliveryReceipt:
    values: dict[str, Any] = {
        "delivery_id": delivery_id,
        "candidate_id": candidate.candidate_id,
        "candidate_revision": candidate.candidate_revision,
        "candidate_sha256": candidate.candidate_sha256,
        "delivered_bytes": len(candidate.candidate_content_bytes),
        "total_bytes": len(candidate.candidate_content_bytes),
    }
    values.update(overrides)
    return CandidateDeliveryReceipt(**values)


def test_process_getter_is_stable_and_diagnostics_are_content_free() -> None:
    first = get_memory_continuity_delivery_coordinator()
    second = get_memory_continuity_delivery_coordinator()
    secret = "singleton-private-candidate-body"
    candidate = _candidate(secret.encode(), candidate_id="candidate-singleton")
    payload = _page_payload(
        candidate,
        delivery_id="continuity-delivery-singleton",
        offset=0,
        end=len(candidate.candidate_content_bytes),
    )

    assert first is second
    expectation = _register(first, payload)
    try:
        snapshot = second.snapshot()
        assert snapshot.pending_pages >= 1
        assert secret not in repr(first)
        assert secret not in repr(snapshot)
        assert expectation.delivery_id == payload["delivery_binding"]["delivery_id"]
    finally:
        first.discard_pending(expectation.delivery_id)

    assert first.has_pending(expectation.delivery_id) is False


async def test_single_page_requires_kernel_receipt_before_verification() -> None:
    candidate = _candidate("完整候选".encode())
    coordinator = ContinuityCandidateDeliveryCoordinator()
    payload = _page_payload(
        candidate,
        delivery_id="continuity-delivery-single",
        offset=0,
        end=len(candidate.candidate_content_bytes),
    )
    expectation = _register(coordinator, payload)
    claim = _claim(candidate, expectation.delivery_id)

    assert await coordinator.verify_exact_candidate_delivery(claim, candidate) is False
    assert coordinator.commit_effective_context_receipt(_effective_receipt(expectation))
    assert await coordinator.verify_exact_candidate_delivery(claim, candidate) is True
    assert coordinator.snapshot().pending_pages == 0
    assert coordinator.snapshot().committed_pages == 1


async def test_multiple_utf8_pages_may_commit_out_of_order() -> None:
    candidate = _candidate("你a好世界".encode())
    coordinator = ContinuityCandidateDeliveryCoordinator()
    ranges = ((4, 7), (0, 3), (7, len(candidate.candidate_content_bytes)), (3, 4))
    delivery_ids: list[str] = []

    for index, (offset, end) in enumerate(ranges):
        payload = _page_payload(
            candidate,
            delivery_id=f"continuity-delivery-page-{index}",
            offset=offset,
            end=end,
        )
        expectation = _register(coordinator, payload)
        delivery_ids.append(expectation.delivery_id)
        assert coordinator.commit_effective_context_receipt(
            _effective_receipt(expectation)
        )

    assert await coordinator.verify_exact_candidate_delivery(
        _claim(candidate, delivery_ids[1]), candidate
    )


async def test_missing_page_and_overlapping_pages_fail_closed() -> None:
    candidate = _candidate(b"abcdef")
    missing = ContinuityCandidateDeliveryCoordinator()
    first = _register(
        missing,
        _page_payload(
            candidate,
            delivery_id="continuity-delivery-missing",
            offset=0,
            end=3,
        ),
    )
    assert missing.commit_effective_context_receipt(_effective_receipt(first))
    assert not await missing.verify_exact_candidate_delivery(
        _claim(candidate, first.delivery_id), candidate
    )

    overlapping = ContinuityCandidateDeliveryCoordinator()
    for delivery_id, offset, end in (
        ("continuity-delivery-overlap-full", 0, 6),
        ("continuity-delivery-overlap-prefix", 0, 3),
    ):
        expectation = _register(
            overlapping,
            _page_payload(
                candidate,
                delivery_id=delivery_id,
                offset=offset,
                end=end,
            ),
        )
        assert overlapping.commit_effective_context_receipt(
            _effective_receipt(expectation)
        )
    assert not await overlapping.verify_exact_candidate_delivery(
        _claim(candidate, "continuity-delivery-overlap-full"), candidate
    )


async def test_internally_consistent_tampered_page_does_not_match_candidate() -> None:
    candidate = _candidate(b"hello")
    coordinator = ContinuityCandidateDeliveryCoordinator()
    expectation = _register(
        coordinator,
        _page_payload(
            candidate,
            delivery_id="continuity-delivery-tampered-page",
            offset=0,
            end=5,
            page_content=b"hullo",
        ),
    )
    assert coordinator.commit_effective_context_receipt(_effective_receipt(expectation))
    assert not await coordinator.verify_exact_candidate_delivery(
        _claim(candidate, expectation.delivery_id), candidate
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"delivery_id": "continuity-delivery-wrong"},
        {"part_kind": "text"},
        {"exact_present": False},
        {"expected_utf8_bytes": 1},
        {"effective_utf8_bytes": 1},
        {"expected_sha256": "0" * 64},
        {"effective_sha256": "0" * 64},
    ],
)
async def test_wrong_effective_tool_result_receipt_never_commits(
    overrides: dict[str, Any],
) -> None:
    candidate = _candidate(b"candidate")
    coordinator = ContinuityCandidateDeliveryCoordinator()
    expectation = _register(
        coordinator,
        _page_payload(
            candidate,
            delivery_id="continuity-delivery-receipt-check",
            offset=0,
            end=len(candidate.candidate_content_bytes),
        ),
    )

    assert not coordinator.commit_effective_context_receipt(
        _effective_receipt(expectation, **overrides)
    )
    assert not await coordinator.verify_exact_candidate_delivery(
        _claim(candidate, expectation.delivery_id), candidate
    )


async def test_wrong_candidate_and_forged_claim_fail_closed() -> None:
    candidate = _candidate(b"same", candidate_id="candidate-original")
    wrong_candidate = _candidate(b"same", candidate_id="candidate-other")
    coordinator = ContinuityCandidateDeliveryCoordinator()
    payload = _page_payload(
        candidate,
        delivery_id="continuity-delivery-candidate-binding",
        offset=0,
        end=4,
    )
    expectation = _register(coordinator, payload)

    # A syntactically perfect model-provided claim is not delivery evidence.
    assert not await coordinator.verify_exact_candidate_delivery(
        _claim(candidate, expectation.delivery_id), candidate
    )
    assert coordinator.commit_effective_context_receipt(_effective_receipt(expectation))
    assert not await coordinator.verify_exact_candidate_delivery(
        _claim(wrong_candidate, expectation.delivery_id), wrong_candidate
    )
    assert not await coordinator.verify_exact_candidate_delivery(
        _claim(candidate, expectation.delivery_id, delivered_bytes=3), candidate
    )


async def test_pending_and_committed_coverage_expire_independently() -> None:
    clock = _Clock()
    candidate = _candidate(b"ttl")
    coordinator = ContinuityCandidateDeliveryCoordinator(
        pending_ttl_seconds=5,
        coverage_ttl_seconds=10,
        clock=clock,
    )
    pending = _register(
        coordinator,
        _page_payload(
            candidate,
            delivery_id="continuity-delivery-expired-pending",
            offset=0,
            end=3,
        ),
    )
    clock.now += 5
    assert not coordinator.commit_effective_context_receipt(_effective_receipt(pending))

    committed = _register(
        coordinator,
        _page_payload(
            candidate,
            delivery_id="continuity-delivery-expired-coverage",
            offset=0,
            end=3,
        ),
    )
    assert coordinator.commit_effective_context_receipt(_effective_receipt(committed))
    clock.now += 10
    assert not await coordinator.verify_exact_candidate_delivery(
        _claim(candidate, committed.delivery_id), candidate
    )
    assert coordinator.prune().committed_pages == 0


async def test_pending_and_coverage_capacity_evict_oldest_proof() -> None:
    first_candidate = _candidate(b"one", candidate_id="candidate-one")
    second_candidate = _candidate(b"two", candidate_id="candidate-two")
    coordinator = ContinuityCandidateDeliveryCoordinator(
        max_pending=1,
        max_committed_pages=1,
    )
    first = _register(
        coordinator,
        _page_payload(
            first_candidate,
            delivery_id="continuity-delivery-capacity-one",
            offset=0,
            end=3,
        ),
    )
    second = _register(
        coordinator,
        _page_payload(
            second_candidate,
            delivery_id="continuity-delivery-capacity-two",
            offset=0,
            end=3,
        ),
    )
    assert not coordinator.commit_effective_context_receipt(_effective_receipt(first))
    assert coordinator.commit_effective_context_receipt(_effective_receipt(second))

    first_again = _register(
        coordinator,
        _page_payload(
            first_candidate,
            delivery_id="continuity-delivery-capacity-one-again",
            offset=0,
            end=3,
        ),
    )
    assert coordinator.commit_effective_context_receipt(_effective_receipt(first_again))
    assert not await coordinator.verify_exact_candidate_delivery(
        _claim(second_candidate, second.delivery_id), second_candidate
    )
    assert await coordinator.verify_exact_candidate_delivery(
        _claim(first_candidate, first_again.delivery_id), first_candidate
    )


def test_delivery_id_conflict_is_content_free_and_fails_closed() -> None:
    candidate = _candidate(b"abcd")
    coordinator = ContinuityCandidateDeliveryCoordinator()
    _register(
        coordinator,
        _page_payload(
            candidate,
            delivery_id="continuity-delivery-conflict",
            offset=0,
            end=2,
        ),
    )
    with pytest.raises(ContinuityCandidateDeliveryConflict) as captured:
        _register(
            coordinator,
            _page_payload(
                candidate,
                delivery_id="continuity-delivery-conflict",
                offset=2,
                end=4,
            ),
        )
    assert "abcd" not in str(captured.value)


@pytest.mark.parametrize(
    ("mutation", "expected_error"),
    [
        (lambda payload: payload.update(action="status"), "Action"),
        (
            lambda payload: payload["page"].update(next_offset=1),
            "PageEvidenceMismatch",
        ),
        (
            lambda payload: payload["delivery_binding"].update(page_bytes=1),
            "BindingEvidenceMismatch",
        ),
    ],
)
def test_structurally_inconsistent_tool_result_is_rejected_without_body(
    mutation,
    expected_error: str,
) -> None:
    secret = "candidate-secret-never-in-errors"
    candidate = _candidate(secret.encode())
    coordinator = ContinuityCandidateDeliveryCoordinator()
    payload = _page_payload(
        candidate,
        delivery_id="continuity-delivery-invalid",
        offset=0,
        end=len(candidate.candidate_content_bytes),
    )
    mutation(payload)
    tool_text = ToolResult(value=payload).to_text()
    with pytest.raises(ContinuityCandidateDeliveryInputError) as captured:
        coordinator.register_pending_tool_result(payload, tool_text)
    assert expected_error in str(captured.value)
    assert secret not in str(captured.value)


def test_truncated_or_different_serialized_tool_result_is_rejected() -> None:
    candidate = _candidate("完整工具结果".encode())
    coordinator = ContinuityCandidateDeliveryCoordinator()
    payload = _page_payload(
        candidate,
        delivery_id="continuity-delivery-serialized",
        offset=0,
        end=len(candidate.candidate_content_bytes),
    )
    full_text = ToolResult(value=payload).to_text()
    with pytest.raises(
        ContinuityCandidateDeliveryInputError,
        match="ToolResultSerializationMismatch",
    ):
        coordinator.register_pending_tool_result(payload, full_text[:-1])


async def test_utf8_non_boundary_coverage_fails_against_candidate_bytes() -> None:
    candidate = _candidate("你a".encode())
    coordinator = ContinuityCandidateDeliveryCoordinator()
    # Internally valid UTF-8 pages whose byte offsets do not match the candidate.
    for delivery_id, offset, end, page_content in (
        ("continuity-delivery-utf8-wrong-one", 0, 1, b"x"),
        ("continuity-delivery-utf8-wrong-two", 1, 4, "你".encode()),
    ):
        expectation = _register(
            coordinator,
            _page_payload(
                candidate,
                delivery_id=delivery_id,
                offset=offset,
                end=end,
                page_content=page_content,
            ),
        )
        assert coordinator.commit_effective_context_receipt(
            _effective_receipt(expectation)
        )
    assert not await coordinator.verify_exact_candidate_delivery(
        _claim(candidate, "continuity-delivery-utf8-wrong-one"), candidate
    )


def test_concurrent_idempotent_registration_stays_single_and_bounded() -> None:
    candidate = _candidate(b"thread-safe")
    coordinator = ContinuityCandidateDeliveryCoordinator(max_pending=2)
    payload = _page_payload(
        candidate,
        delivery_id="continuity-delivery-thread-safe",
        offset=0,
        end=len(candidate.candidate_content_bytes),
    )
    tool_text = ToolResult(value=payload).to_text()

    with ThreadPoolExecutor(max_workers=8) as pool:
        expectations = list(
            pool.map(
                lambda _: coordinator.register_pending_tool_result(payload, tool_text),
                range(64),
            )
        )

    assert {item.expected_sha256 for item in expectations} == {
        expectations[0].expected_sha256
    }
    assert coordinator.snapshot().pending_pages == 1


def test_wrong_top_level_serialization_and_extra_binding_fields_are_rejected() -> None:
    candidate = _candidate(b"strict")
    coordinator = ContinuityCandidateDeliveryCoordinator()
    payload = _page_payload(
        candidate,
        delivery_id="continuity-delivery-strict",
        offset=0,
        end=6,
    )
    payload["delivery_binding"]["unexpected"] = True
    with pytest.raises(
        ContinuityCandidateDeliveryInputError,
        match="FieldMismatch:delivery_binding",
    ):
        coordinator.register_pending_tool_result(
            payload, ToolResult(value=payload).to_text()
        )


def test_context_receipt_dataclass_cannot_be_substituted_by_lookalike() -> None:
    candidate = _candidate(b"typed")
    coordinator = ContinuityCandidateDeliveryCoordinator()
    expectation = _register(
        coordinator,
        _page_payload(
            candidate,
            delivery_id="continuity-delivery-typed-receipt",
            offset=0,
            end=5,
        ),
    )
    lookalike = replace(_effective_receipt(expectation), exact_present=True)
    assert coordinator.commit_effective_context_receipt(lookalike)
    assert not coordinator.commit_effective_context_receipt(object())  # type: ignore[arg-type]
