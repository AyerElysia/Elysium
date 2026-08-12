"""Strict in-process proof coordination for continuity candidate delivery.

Producing a continuity-review ``ToolResult`` is not evidence that the model
received it. This module keeps a bounded, short-lived two-phase ledger:

1. validate and register the exact serialized ``candidate_read`` ToolResult;
2. commit its page only after the LLM kernel proves exact ToolResult delivery;
3. verify an acceptance claim from committed pages, byte-for-byte, against the
   immutable :class:`~plugins.life_engine.learning.decisions.LearningCandidate`.

The ledger is deliberately process-local. A restart or eviction loses the
proof and therefore fails closed; the active consciousness instance must read
the candidate again. Candidate text is excluded from reprs and exceptions.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from src.kernel.llm.context_delivery import (
    ContextDeliveryExpectation,
    EffectiveContextReceipt,
)

from ..learning.decisions import LearningCandidate
from .continuity_session import (
    CONTINUITY_REVIEW_MAX_PAGE_BYTES,
    CandidateDeliveryReceipt,
)

CONTINUITY_DELIVERY_MAX_PENDING = 256
CONTINUITY_DELIVERY_MAX_COMMITTED_PAGES = 1024
CONTINUITY_DELIVERY_MAX_TOOL_RESULT_BYTES = 128 * 1024
CONTINUITY_DELIVERY_MAX_CANDIDATE_BYTES = 4 * 1024 * 1024
CONTINUITY_DELIVERY_PENDING_TTL_SECONDS = 15 * 60.0
CONTINUITY_DELIVERY_COVERAGE_TTL_SECONDS = 30 * 60.0

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_DELIVERY_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,511}$")
_PAGE_KEYS = frozenset(
    {
        "offset",
        "next_offset",
        "delivered_bytes",
        "total_bytes",
        "page_sha256",
        "text",
    }
)
_BINDING_KEYS = frozenset(
    {
        "delivery_id",
        "candidate_id",
        "candidate_revision",
        "candidate_sha256",
        "page_offset",
        "page_sha256",
        "page_bytes",
        "total_bytes",
    }
)
_TOP_LEVEL_REQUIRED = frozenset(
    {
        "action",
        "candidate_id",
        "candidate_revision",
        "candidate_sha256",
        "page",
        "delivery_binding",
    }
)


class ContinuityCandidateDeliveryError(RuntimeError):
    """Base class for fail-closed continuity delivery errors."""


class ContinuityCandidateDeliveryInputError(ContinuityCandidateDeliveryError):
    """Raised when a ToolResult cannot be bound to one exact candidate page."""


class ContinuityCandidateDeliveryConflict(ContinuityCandidateDeliveryError):
    """Raised when one delivery identity is reused for different bytes."""


@dataclass(frozen=True, slots=True)
class ContinuityDeliverySnapshot:
    """Content-free bounded-ledger diagnostics."""

    pending_pages: int
    committed_pages: int
    candidate_coverages: int
    max_pending: int
    max_committed_pages: int


@dataclass(frozen=True, slots=True)
class _CandidateKey:
    candidate_id: str
    candidate_revision: int
    candidate_sha256: str


@dataclass(frozen=True, slots=True)
class _PendingPage:
    delivery_id: str
    candidate: _CandidateKey
    page_offset: int
    page_end: int
    total_bytes: int
    page_sha256: str
    page_content_bytes: bytes = field(repr=False)
    tool_result_text: str = field(repr=False)
    tool_result_utf8_bytes: int
    tool_result_sha256: str


@dataclass(frozen=True, slots=True)
class _CommittedPage:
    delivery_id: str
    candidate: _CandidateKey
    page_offset: int
    page_end: int
    total_bytes: int
    page_sha256: str
    page_content_bytes: bytes = field(repr=False)
    tool_result_utf8_bytes: int
    tool_result_sha256: str


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _bounded_identity(value: object, field_name: str, *, max_chars: int = 512) -> str:
    if not isinstance(value, str):
        raise ContinuityCandidateDeliveryInputError(
            f"ContinuityDeliveryInvalidIdentity:{field_name}"
        )
    if (
        not value
        or value != value.strip()
        or len(value) > max_chars
        or any(ord(character) < 32 for character in value)
    ):
        raise ContinuityCandidateDeliveryInputError(
            f"ContinuityDeliveryInvalidIdentity:{field_name}"
        )
    return value


def _nonnegative_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ContinuityCandidateDeliveryInputError(
            f"ContinuityDeliveryInvalidInteger:{field_name}"
        )
    return value


def _positive_int(value: object, field_name: str) -> int:
    result = _nonnegative_int(value, field_name)
    if result == 0:
        raise ContinuityCandidateDeliveryInputError(
            f"ContinuityDeliveryInvalidInteger:{field_name}"
        )
    return result


def _sha256_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ContinuityCandidateDeliveryInputError(
            f"ContinuityDeliveryInvalidSha256:{field_name}"
        )
    return value


def _mapping(value: object, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ContinuityCandidateDeliveryInputError(
            f"ContinuityDeliveryInvalidObject:{field_name}"
        )
    return value


def _exact_keys(
    value: Mapping[str, Any], expected: frozenset[str], field_name: str
) -> None:
    if frozenset(value) != expected:
        raise ContinuityCandidateDeliveryInputError(
            f"ContinuityDeliveryFieldMismatch:{field_name}"
        )


def _positive_limit(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} must be a positive integer")
    return value


def _positive_ttl(value: object, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} must be a finite positive number")
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise ValueError(f"{field_name} must be a finite positive number")
    return result


def _validate_tool_result_text(
    payload: Mapping[str, Any], actual_tool_result_text: object
) -> str:
    if not isinstance(actual_tool_result_text, str) or not actual_tool_result_text:
        raise ContinuityCandidateDeliveryInputError(
            "ContinuityDeliveryToolResultTextMissing"
        )
    try:
        expected = json.dumps(dict(payload), ensure_ascii=False)
    except (RecursionError, TypeError, ValueError) as exc:
        raise ContinuityCandidateDeliveryInputError(
            "ContinuityDeliveryToolResultNotSerializable"
        ) from exc
    if actual_tool_result_text != expected:
        raise ContinuityCandidateDeliveryInputError(
            "ContinuityDeliveryToolResultSerializationMismatch"
        )
    if len(actual_tool_result_text.encode("utf-8")) > (
        CONTINUITY_DELIVERY_MAX_TOOL_RESULT_BYTES
    ):
        raise ContinuityCandidateDeliveryInputError(
            "ContinuityDeliveryToolResultTooLarge"
        )
    return actual_tool_result_text


def _pending_from_tool_result(
    payload: Mapping[str, Any], actual_tool_result_text: object
) -> _PendingPage:
    if not _TOP_LEVEL_REQUIRED.issubset(payload):
        raise ContinuityCandidateDeliveryInputError(
            "ContinuityDeliveryCandidateReadFieldsMissing"
        )
    if payload.get("action") != "candidate_read":
        raise ContinuityCandidateDeliveryInputError(
            "ContinuityDeliveryActionMustBeCandidateRead"
        )

    candidate_id = _bounded_identity(payload["candidate_id"], "candidate_id")
    candidate_revision = _positive_int(
        payload["candidate_revision"], "candidate_revision"
    )
    candidate_sha256 = _sha256_text(payload["candidate_sha256"], "candidate_sha256")
    candidate = _CandidateKey(candidate_id, candidate_revision, candidate_sha256)

    page = _mapping(payload["page"], "page")
    binding = _mapping(payload["delivery_binding"], "delivery_binding")
    _exact_keys(page, _PAGE_KEYS, "page")
    _exact_keys(binding, _BINDING_KEYS, "delivery_binding")

    offset = _nonnegative_int(page["offset"], "page.offset")
    delivered_bytes = _nonnegative_int(page["delivered_bytes"], "page.delivered_bytes")
    total_bytes = _nonnegative_int(page["total_bytes"], "page.total_bytes")
    page_sha256 = _sha256_text(page["page_sha256"], "page.page_sha256")
    text = page["text"]
    if not isinstance(text, str):
        raise ContinuityCandidateDeliveryInputError(
            "ContinuityDeliveryPageTextMustBeString"
        )
    page_content = text.encode("utf-8")
    page_end = offset + delivered_bytes
    next_offset = page["next_offset"]
    if next_offset is not None:
        next_offset = _nonnegative_int(next_offset, "page.next_offset")
    expected_next = page_end if page_end < total_bytes else None
    if not all(
        (
            delivered_bytes == len(page_content),
            delivered_bytes <= CONTINUITY_REVIEW_MAX_PAGE_BYTES,
            total_bytes <= CONTINUITY_DELIVERY_MAX_CANDIDATE_BYTES,
            page_sha256 == _sha256(page_content),
            offset <= total_bytes,
            page_end <= total_bytes,
            next_offset == expected_next,
        )
    ):
        raise ContinuityCandidateDeliveryInputError(
            "ContinuityDeliveryPageEvidenceMismatch"
        )
    if delivered_bytes == 0 and offset != total_bytes:
        raise ContinuityCandidateDeliveryInputError(
            "ContinuityDeliveryEmptyPageBeforeEnd"
        )

    delivery_id = _bounded_identity(binding["delivery_id"], "delivery_id")
    if _DELIVERY_ID_RE.fullmatch(delivery_id) is None:
        raise ContinuityCandidateDeliveryInputError(
            "ContinuityDeliveryUnsafeMarkerIdentity"
        )
    binding_candidate_id = _bounded_identity(
        binding["candidate_id"], "delivery_binding.candidate_id"
    )
    binding_candidate_revision = _positive_int(
        binding["candidate_revision"], "delivery_binding.candidate_revision"
    )
    binding_candidate_sha256 = _sha256_text(
        binding["candidate_sha256"], "delivery_binding.candidate_sha256"
    )
    binding_offset = _nonnegative_int(
        binding["page_offset"], "delivery_binding.page_offset"
    )
    binding_page_sha256 = _sha256_text(
        binding["page_sha256"], "delivery_binding.page_sha256"
    )
    binding_page_bytes = _nonnegative_int(
        binding["page_bytes"], "delivery_binding.page_bytes"
    )
    binding_total_bytes = _nonnegative_int(
        binding["total_bytes"], "delivery_binding.total_bytes"
    )
    if not all(
        (
            binding_candidate_id == candidate_id,
            binding_candidate_revision == candidate_revision,
            binding_candidate_sha256 == candidate_sha256,
            binding_offset == offset,
            binding_page_sha256 == page_sha256,
            binding_page_bytes == delivered_bytes,
            binding_total_bytes == total_bytes,
        )
    ):
        raise ContinuityCandidateDeliveryInputError(
            "ContinuityDeliveryBindingEvidenceMismatch"
        )

    tool_result_text = _validate_tool_result_text(payload, actual_tool_result_text)
    if delivery_id not in tool_result_text:
        raise ContinuityCandidateDeliveryInputError(
            "ContinuityDeliveryMarkerMissingFromToolResult"
        )
    encoded_tool_result = tool_result_text.encode("utf-8")
    return _PendingPage(
        delivery_id=delivery_id,
        candidate=candidate,
        page_offset=offset,
        page_end=page_end,
        total_bytes=total_bytes,
        page_sha256=page_sha256,
        page_content_bytes=page_content,
        tool_result_text=tool_result_text,
        tool_result_utf8_bytes=len(encoded_tool_result),
        tool_result_sha256=_sha256(encoded_tool_result),
    )


def _same_page(left: _PendingPage, right: _PendingPage | _CommittedPage) -> bool:
    return all(
        (
            left.delivery_id == right.delivery_id,
            left.candidate == right.candidate,
            left.page_offset == right.page_offset,
            left.page_end == right.page_end,
            left.total_bytes == right.total_bytes,
            left.page_sha256 == right.page_sha256,
            left.page_content_bytes == right.page_content_bytes,
            left.tool_result_utf8_bytes == right.tool_result_utf8_bytes,
            left.tool_result_sha256 == right.tool_result_sha256,
        )
    )


class ContinuityCandidateDeliveryCoordinator:
    """Thread-safe, bounded proof ledger for exact candidate delivery.

    The coordinator owns no durable authority. Pending pages and committed
    coverage expire independently; eviction and restart intentionally make
    later verification return ``False``.
    """

    def __init__(
        self,
        *,
        max_pending: int = CONTINUITY_DELIVERY_MAX_PENDING,
        max_committed_pages: int = CONTINUITY_DELIVERY_MAX_COMMITTED_PAGES,
        pending_ttl_seconds: float = CONTINUITY_DELIVERY_PENDING_TTL_SECONDS,
        coverage_ttl_seconds: float = CONTINUITY_DELIVERY_COVERAGE_TTL_SECONDS,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._max_pending = _positive_limit(max_pending, "max_pending")
        self._max_committed_pages = _positive_limit(
            max_committed_pages, "max_committed_pages"
        )
        self._pending_ttl_seconds = _positive_ttl(
            pending_ttl_seconds, "pending_ttl_seconds"
        )
        self._coverage_ttl_seconds = _positive_ttl(
            coverage_ttl_seconds, "coverage_ttl_seconds"
        )
        self._clock = clock or time.monotonic
        self._lock = threading.RLock()
        self._pending: OrderedDict[str, tuple[float, _PendingPage]] = OrderedDict()
        self._coverage: OrderedDict[str, tuple[float, _CommittedPage]] = OrderedDict()

    def _now(self) -> float:
        now = float(self._clock())
        if not math.isfinite(now):
            raise RuntimeError("ContinuityDeliveryClockInvalid")
        return now

    def _prune_locked(self, now: float) -> None:
        expired_pending = [
            delivery_id
            for delivery_id, (created_at, _) in self._pending.items()
            if now - created_at >= self._pending_ttl_seconds
        ]
        for delivery_id in expired_pending:
            self._pending.pop(delivery_id, None)
        expired_coverage = [
            delivery_id
            for delivery_id, (committed_at, _) in self._coverage.items()
            if now - committed_at >= self._coverage_ttl_seconds
        ]
        for delivery_id in expired_coverage:
            self._coverage.pop(delivery_id, None)
        while len(self._pending) > self._max_pending:
            self._pending.popitem(last=False)
        while len(self._coverage) > self._max_committed_pages:
            self._coverage.popitem(last=False)

    @staticmethod
    def _expectation(page: _PendingPage) -> ContextDeliveryExpectation:
        return ContextDeliveryExpectation.create(
            page.delivery_id,
            page.tool_result_text,
            marker=page.delivery_id,
            part_kind="tool_result",
        )

    def register_pending_tool_result(
        self,
        payload: Mapping[str, Any],
        actual_tool_result_text: str,
    ) -> ContextDeliveryExpectation:
        """Validate a complete ToolResult and return its kernel expectation.

        ``payload`` must be the trusted, unmodified value used to construct the
        actual ``ToolResult``. Registration alone creates no coverage.
        """

        page = _pending_from_tool_result(
            _mapping(payload, "tool_result_payload"), actual_tool_result_text
        )
        now = self._now()
        with self._lock:
            self._prune_locked(now)
            pending = self._pending.get(page.delivery_id)
            if pending is not None:
                if pending[1] != page:
                    raise ContinuityCandidateDeliveryConflict(
                        "ContinuityDeliveryIdConflict"
                    )
                return self._expectation(pending[1])
            committed = self._coverage.get(page.delivery_id)
            if committed is not None and not _same_page(page, committed[1]):
                raise ContinuityCandidateDeliveryConflict(
                    "ContinuityDeliveryIdConflict"
                )
            self._pending[page.delivery_id] = (now, page)
            self._prune_locked(now)
        return self._expectation(page)

    def has_pending(self, delivery_id: str) -> bool:
        """Return whether a non-expired page awaits kernel proof."""

        identity = str(delivery_id or "").strip()
        if not identity:
            return False
        with self._lock:
            self._prune_locked(self._now())
            return identity in self._pending

    def discard_pending(self, delivery_id: str) -> None:
        """Forget one unverified ToolResult without changing coverage."""

        identity = str(delivery_id or "").strip()
        if not identity:
            return
        with self._lock:
            self._pending.pop(identity, None)

    @staticmethod
    def _receipt_matches(receipt: EffectiveContextReceipt, page: _PendingPage) -> bool:
        return all(
            (
                receipt.delivery_id == page.delivery_id,
                receipt.part_kind == "tool_result",
                receipt.exact_present is True,
                isinstance(receipt.expected_utf8_bytes, int),
                not isinstance(receipt.expected_utf8_bytes, bool),
                receipt.expected_utf8_bytes == page.tool_result_utf8_bytes,
                receipt.expected_sha256 == page.tool_result_sha256,
                isinstance(receipt.effective_utf8_bytes, int),
                not isinstance(receipt.effective_utf8_bytes, bool),
                receipt.effective_utf8_bytes == page.tool_result_utf8_bytes,
                receipt.effective_sha256 == page.tool_result_sha256,
                receipt.expected_utf8_bytes == receipt.effective_utf8_bytes,
                receipt.expected_sha256 == receipt.effective_sha256,
            )
        )

    def commit_effective_context_receipt(
        self, receipt: EffectiveContextReceipt
    ) -> bool:
        """Commit one page only after exact final-attempt ToolResult proof."""

        if not isinstance(receipt, EffectiveContextReceipt):
            return False
        now = self._now()
        with self._lock:
            self._prune_locked(now)
            pending_entry = self._pending.get(receipt.delivery_id)
            if pending_entry is None:
                return False
            page = pending_entry[1]
            if not self._receipt_matches(receipt, page):
                self._pending.pop(receipt.delivery_id, None)
                return False
            existing = self._coverage.get(page.delivery_id)
            if existing is not None and not _same_page(page, existing[1]):
                self._pending.pop(page.delivery_id, None)
                return False
            committed = _CommittedPage(
                delivery_id=page.delivery_id,
                candidate=page.candidate,
                page_offset=page.page_offset,
                page_end=page.page_end,
                total_bytes=page.total_bytes,
                page_sha256=page.page_sha256,
                page_content_bytes=page.page_content_bytes,
                tool_result_utf8_bytes=page.tool_result_utf8_bytes,
                tool_result_sha256=page.tool_result_sha256,
            )
            self._pending.pop(page.delivery_id, None)
            self._coverage.pop(page.delivery_id, None)
            self._coverage[page.delivery_id] = (now, committed)
            self._prune_locked(now)
            return page.delivery_id in self._coverage

    @staticmethod
    def _claim_matches_candidate(
        receipt: CandidateDeliveryReceipt,
        candidate: LearningCandidate,
        content: bytes,
    ) -> bool:
        return all(
            (
                isinstance(receipt.delivery_id, str),
                bool(receipt.delivery_id),
                receipt.candidate_id == candidate.candidate_id,
                isinstance(receipt.candidate_revision, int),
                not isinstance(receipt.candidate_revision, bool),
                receipt.candidate_revision == candidate.candidate_revision,
                receipt.candidate_sha256 == candidate.candidate_sha256,
                _SHA256_RE.fullmatch(candidate.candidate_sha256) is not None,
                _sha256(content) == candidate.candidate_sha256,
                isinstance(receipt.delivered_bytes, int),
                not isinstance(receipt.delivered_bytes, bool),
                isinstance(receipt.total_bytes, int),
                not isinstance(receipt.total_bytes, bool),
                receipt.delivered_bytes == len(content),
                receipt.total_bytes == len(content),
            )
        )

    async def verify_exact_candidate_delivery(
        self,
        receipt: CandidateDeliveryReceipt,
        candidate: LearningCandidate,
    ) -> bool:
        """Verify complete, gap-free exact coverage without trusting the claim."""

        if not isinstance(receipt, CandidateDeliveryReceipt) or not isinstance(
            candidate, LearningCandidate
        ):
            return False
        content = bytes(candidate.candidate_content_bytes)
        if (
            len(content) > CONTINUITY_DELIVERY_MAX_CANDIDATE_BYTES
            or not self._claim_matches_candidate(receipt, candidate, content)
        ):
            return False
        try:
            content.decode("utf-8")
        except UnicodeDecodeError:
            return False

        key = _CandidateKey(
            candidate.candidate_id,
            candidate.candidate_revision,
            candidate.candidate_sha256,
        )
        with self._lock:
            self._prune_locked(self._now())
            pages = tuple(
                item for _, item in self._coverage.values() if item.candidate == key
            )
        if not pages or receipt.delivery_id not in {page.delivery_id for page in pages}:
            return False

        ordered = sorted(
            pages,
            key=lambda page: (page.page_offset, page.page_end, page.delivery_id),
        )
        if not content:
            if len(ordered) != 1:
                return False
            page = ordered[0]
            return all(
                (
                    page.page_offset == 0,
                    page.page_end == 0,
                    page.total_bytes == 0,
                    page.page_content_bytes == b"",
                    page.page_sha256 == _sha256(b""),
                )
            )

        cursor = 0
        for page in ordered:
            if page.total_bytes != len(content):
                return False
            if page.page_offset != cursor or page.page_end <= page.page_offset:
                return False  # overlap or gap
            if page.page_end > len(content):
                return False
            exact_slice = content[page.page_offset : page.page_end]
            if not all(
                (
                    exact_slice == page.page_content_bytes,
                    _sha256(exact_slice) == page.page_sha256,
                    len(exact_slice) == page.page_end - page.page_offset,
                )
            ):
                return False
            try:
                content[: page.page_offset].decode("utf-8")
                exact_slice.decode("utf-8")
            except UnicodeDecodeError:
                return False
            cursor = page.page_end
        return cursor == len(content)

    def _snapshot_locked(self) -> ContinuityDeliverySnapshot:
        keys = {item.candidate for _, item in self._coverage.values()}
        return ContinuityDeliverySnapshot(
            pending_pages=len(self._pending),
            committed_pages=len(self._coverage),
            candidate_coverages=len(keys),
            max_pending=self._max_pending,
            max_committed_pages=self._max_committed_pages,
        )

    def prune(self) -> ContinuityDeliverySnapshot:
        """Prune expired entries and return content-free diagnostics."""

        with self._lock:
            self._prune_locked(self._now())
            return self._snapshot_locked()

    def snapshot(self) -> ContinuityDeliverySnapshot:
        """Return bounded content-free diagnostics after TTL pruning."""

        with self._lock:
            self._prune_locked(self._now())
            return self._snapshot_locked()


_CONTINUITY_DELIVERY_COORDINATOR = ContinuityCandidateDeliveryCoordinator()


def get_memory_continuity_delivery_coordinator() -> (
    ContinuityCandidateDeliveryCoordinator
):
    """Return the process-wide continuity candidate delivery coordinator."""

    return _CONTINUITY_DELIVERY_COORDINATOR


__all__ = [
    "CONTINUITY_DELIVERY_COVERAGE_TTL_SECONDS",
    "CONTINUITY_DELIVERY_MAX_CANDIDATE_BYTES",
    "CONTINUITY_DELIVERY_MAX_COMMITTED_PAGES",
    "CONTINUITY_DELIVERY_MAX_PENDING",
    "CONTINUITY_DELIVERY_MAX_TOOL_RESULT_BYTES",
    "CONTINUITY_DELIVERY_PENDING_TTL_SECONDS",
    "ContinuityCandidateDeliveryConflict",
    "ContinuityCandidateDeliveryCoordinator",
    "ContinuityCandidateDeliveryError",
    "ContinuityCandidateDeliveryInputError",
    "ContinuityDeliverySnapshot",
    "get_memory_continuity_delivery_coordinator",
]
