from __future__ import annotations

import pytest

from src.kernel.storage.outbox_primitives import (
    CursorConflict,
    ImmutableIdentity,
    StableIdentityConflict,
    canonical_json,
    canonical_json_sha256,
    compare_and_advance_cursor,
)


def test_canonical_json_is_order_independent_and_rejects_nan() -> None:
    left = {"emoji": "爱莉", "nested": {"b": 2, "a": 1}}
    right = {"nested": {"a": 1, "b": 2}, "emoji": "爱莉"}

    assert canonical_json(left) == canonical_json(right)
    assert canonical_json_sha256(left) == canonical_json_sha256(right)
    with pytest.raises(ValueError):
        canonical_json({"bad": float("nan")})


def test_immutable_identity_accepts_exact_replay_and_rejects_conflict() -> None:
    identity = ImmutableIdentity("event-1", canonical_json_sha256({"version": 1}))

    identity.assert_same_payload(canonical_json_sha256({"version": 1}))
    with pytest.raises(StableIdentityConflict):
        identity.assert_same_payload(canonical_json_sha256({"version": 2}))


def test_cursor_requires_exact_revision_and_monotonic_position() -> None:
    assert compare_and_advance_cursor(
        current_position=10,
        current_revision=3,
        expected_position=10,
        expected_revision=3,
        next_position=11,
    ) == (11, 4)

    with pytest.raises(CursorConflict):
        compare_and_advance_cursor(
            current_position=10,
            current_revision=3,
            expected_position=9,
            expected_revision=3,
            next_position=11,
        )
    with pytest.raises(CursorConflict):
        compare_and_advance_cursor(
            current_position=10,
            current_revision=3,
            expected_position=10,
            expected_revision=2,
            next_position=11,
        )
    with pytest.raises(CursorConflict):
        compare_and_advance_cursor(
            current_position=10,
            current_revision=3,
            expected_position=10,
            expected_revision=3,
            next_position=9,
        )
