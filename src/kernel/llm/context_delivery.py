"""Content-free receipts for exact delivery of transient prompt parts."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from .payload import LLMPayload, Text, ToolResult


def _utf8_fingerprint(value: str) -> tuple[int, str]:
    encoded = value.encode("utf-8")
    return len(encoded), hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class ContextDeliveryExpectation:
    """One exact transient text part registered for the next model attempt."""

    delivery_id: str
    marker: str = field(repr=False)
    expected_text: str = field(repr=False)
    expected_utf8_bytes: int
    expected_sha256: str
    part_kind: str = "text"

    @classmethod
    def create(
        cls,
        delivery_id: str,
        expected_text: str,
        *,
        marker: str | None = None,
        part_kind: str = "text",
    ) -> "ContextDeliveryExpectation":
        identity = str(delivery_id or "").strip()
        text = str(expected_text or "")
        marker_text = str(marker if marker is not None else identity)
        normalized_part_kind = str(part_kind or "text").strip().lower()
        if not identity:
            raise ValueError("context delivery_id must not be empty")
        if not text:
            raise ValueError("context delivery expected_text must not be empty")
        if normalized_part_kind not in {"text", "tool_result"}:
            raise ValueError("context delivery part_kind is unsupported")
        if not marker_text or marker_text not in text:
            raise ValueError("context delivery marker must occur in expected_text")
        expected_bytes, expected_sha256 = _utf8_fingerprint(text)
        return cls(
            delivery_id=identity,
            marker=marker_text,
            expected_text=text,
            expected_utf8_bytes=expected_bytes,
            expected_sha256=expected_sha256,
            part_kind=normalized_part_kind,
        )


@dataclass(frozen=True, slots=True)
class EffectiveContextReceipt:
    """Proof about one tracked text part in the final successful attempt.

    The receipt intentionally contains no prompt text.  ``exact_present`` is
    true only when exactly one effective ``Text`` part equals the registered
    text byte-for-byte.  Missing, duplicated, or trimmed marker-bearing parts
    all fail closed.
    """

    delivery_id: str
    exact_present: bool
    expected_utf8_bytes: int
    expected_sha256: str
    effective_utf8_bytes: int | None
    effective_sha256: str | None
    part_kind: str = "text"


def build_effective_context_receipts(
    expectations: dict[str, ContextDeliveryExpectation],
    payloads: list[LLMPayload],
) -> dict[str, EffectiveContextReceipt]:
    """Inspect effective attempt payloads without exposing their text."""

    text_parts = [
        part.text
        for payload in payloads
        for part in payload.content
        if isinstance(part, Text)
    ]
    tool_result_parts = [
        part.to_text()
        for payload in payloads
        for part in payload.content
        if isinstance(part, ToolResult)
    ]
    receipts: dict[str, EffectiveContextReceipt] = {}
    for delivery_id, expectation in expectations.items():
        source_parts = (
            tool_result_parts if expectation.part_kind == "tool_result" else text_parts
        )
        candidates = [value for value in source_parts if expectation.marker in value]
        effective_text = candidates[0] if len(candidates) == 1 else None
        effective_bytes: int | None = None
        effective_sha256: str | None = None
        if effective_text is not None:
            effective_bytes, effective_sha256 = _utf8_fingerprint(effective_text)
        exact_present = bool(
            effective_text == expectation.expected_text
            and effective_bytes == expectation.expected_utf8_bytes
            and effective_sha256 == expectation.expected_sha256
        )
        receipts[delivery_id] = EffectiveContextReceipt(
            delivery_id=delivery_id,
            exact_present=exact_present,
            expected_utf8_bytes=expectation.expected_utf8_bytes,
            expected_sha256=expectation.expected_sha256,
            effective_utf8_bytes=effective_bytes,
            effective_sha256=effective_sha256,
            part_kind=expectation.part_kind,
        )
    return receipts


__all__ = [
    "ContextDeliveryExpectation",
    "EffectiveContextReceipt",
    "build_effective_context_receipts",
]
