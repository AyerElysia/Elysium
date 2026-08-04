"""Backend-neutral identity, hash, and cursor primitives for durable storage."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any


class StableIdentityConflict(RuntimeError):
    """Raised when one immutable identity is reused with different content."""


class CursorConflict(RuntimeError):
    """Raised when a compare-and-swap cursor does not match its expectation."""


def canonical_json(value: Any) -> str:
    """Serialize JSON deterministically without changing its semantic content."""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def canonical_json_sha256(value: Any) -> str:
    """Return the SHA-256 of :func:`canonical_json` encoded as UTF-8."""

    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ImmutableIdentity:
    """Stable identity plus the canonical payload hash stored under it."""

    identity: str
    payload_hash: str

    def assert_same_payload(self, incoming_hash: str) -> None:
        """Accept an idempotent replay or reject an identity collision."""

        if self.payload_hash != incoming_hash:
            raise StableIdentityConflict(
                f"immutable identity reused with different content: {self.identity}"
            )


def compare_and_advance_cursor(
    *,
    current_position: int,
    current_revision: int,
    expected_position: int,
    expected_revision: int,
    next_position: int,
) -> tuple[int, int]:
    """Validate exact position/revision CAS and return the next cursor state."""

    current_value = max(0, int(current_position))
    current_revision_value = max(0, int(current_revision))
    expected_value = max(0, int(expected_position))
    expected_revision_value = max(0, int(expected_revision))
    next_value = max(0, int(next_position))
    if current_value != expected_value:
        raise CursorConflict(
            f"cursor conflict: expected {expected_value}, actual {current_value}"
        )
    if current_revision_value != expected_revision_value:
        raise CursorConflict(
            "cursor revision conflict: "
            f"expected {expected_revision_value}, actual {current_revision_value}"
        )
    if next_value < current_value:
        raise CursorConflict(
            f"cursor cannot regress from {current_value} to {next_value}"
        )
    if next_value == current_value:
        return current_value, current_revision_value
    return next_value, current_revision_value + 1


__all__ = [
    "CursorConflict",
    "ImmutableIdentity",
    "StableIdentityConflict",
    "canonical_json",
    "canonical_json_sha256",
    "compare_and_advance_cursor",
]
