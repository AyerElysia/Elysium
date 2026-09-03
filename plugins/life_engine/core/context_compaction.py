"""Compatibility surface for bounded Life Chatter context projections.

The retired implementation copied clipped messages, tool arguments and tool
results into a synthetic USER summary. That was not semantic compression and
it let infrastructure impersonate the subject. Production callers now use
subject-authored checkpoints from :mod:`context_stewardship`; this module keeps
the old import surface while making every automatic fallback mechanical and
content-neutral.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from src.kernel.llm import LLMPayload

from .context_stewardship import (
    DEFAULT_EMERGENCY_REFERENCE_MAX_BYTES,
    LEGACY_SUMMARY_CLOSE,
    LEGACY_SUMMARY_INTRO,
    LEGACY_SUMMARY_OPEN,
    build_conversation_groups,
    build_mechanical_omission_payloads,
    is_legacy_summary_payload,
    mechanically_bound_payloads,
    split_pinned_and_tail,
)

DEFAULT_MAX_GROUPS = 12
DEFAULT_MAX_PART_CHARS = 360
DEFAULT_SNAPSHOT_CHAR_BUDGET = 320_000
DEFAULT_TRIGGER_CHARS = 120_000
DEFAULT_TARGET_CHARS = 80_000
DEFAULT_MIN_RECENT_GROUPS = 2
DEFAULT_SUMMARY_MAX_CHARS = 12_000

# Read-only aliases allow old snapshots to be identified and retired. New
# output never emits these markers.
SUMMARY_OPEN = LEGACY_SUMMARY_OPEN
SUMMARY_CLOSE = LEGACY_SUMMARY_CLOSE
SUMMARY_INTRO = LEGACY_SUMMARY_INTRO


@dataclass(slots=True)
class ContextCompactionResult:
    """Legacy-shaped result carrying a mechanical projection outcome."""

    triggered: bool
    before_chars: int
    after_chars: int
    payloads: list[LLMPayload]
    dropped_groups: int = 0
    summary_updated: bool = False
    target_reached: bool = True


def is_summary_payload(payload: LLMPayload) -> bool:
    """Recognize the retired envelope only for compatibility reads."""

    return is_legacy_summary_payload(payload)


def compress_dropped_payload_groups(
    dropped_groups: list[list[LLMPayload]],
    remaining_payloads: list[LLMPayload],
    *,
    max_groups: int = DEFAULT_MAX_GROUPS,
    max_part_chars: int = DEFAULT_MAX_PART_CHARS,
) -> list[LLMPayload]:
    """Kernel hook: refs only, never copied message or tool bodies."""

    del remaining_payloads, max_part_chars
    return build_mechanical_omission_payloads(
        dropped_groups,
        max_group_refs=max(1, int(max_groups)),
        max_bytes=DEFAULT_EMERGENCY_REFERENCE_MAX_BYTES,
    )


def hierarchical_compact_payloads(
    payloads: list[LLMPayload],
    *,
    estimate: Callable[[list[LLMPayload]], int],
    trigger_chars: int = DEFAULT_TRIGGER_CHARS,
    target_chars: int = DEFAULT_TARGET_CHARS,
    min_recent_groups: int = DEFAULT_MIN_RECENT_GROUPS,
    summary_max_chars: int = DEFAULT_SUMMARY_MAX_CHARS,
    max_part_chars: int = DEFAULT_MAX_PART_CHARS,
    force: bool = False,
) -> ContextCompactionResult:
    """Compatibility wrapper around mechanical hard-budget omission."""

    del min_recent_groups, summary_max_chars, max_part_chars
    before = estimate(payloads)
    if not force and before <= max(1, int(trigger_chars)):
        return ContextCompactionResult(
            triggered=False,
            before_chars=before,
            after_chars=before,
            payloads=list(payloads),
        )
    result, _ = mechanically_bound_payloads(
        payloads,
        estimate=estimate,
        hard_budget=max(1, int(target_chars)),
        reference_max_groups=DEFAULT_MAX_GROUPS,
        reference_max_bytes=DEFAULT_EMERGENCY_REFERENCE_MAX_BYTES,
    )
    after = estimate(result.payloads)
    return ContextCompactionResult(
        triggered=result.triggered,
        before_chars=before,
        after_chars=after,
        payloads=result.payloads,
        dropped_groups=result.released_groups,
        summary_updated=False,
        target_reached=after <= max(1, int(target_chars)),
    )


def compact_payloads(
    payloads: list[LLMPayload],
    *,
    estimate: Callable[[list[LLMPayload]], int],
    char_budget: int = DEFAULT_SNAPSHOT_CHAR_BUDGET,
    max_groups: int = DEFAULT_MAX_GROUPS,
    max_part_chars: int = DEFAULT_MAX_PART_CHARS,
    trigger_chars: int | None = None,
    target_chars: int | None = None,
    min_recent_groups: int = DEFAULT_MIN_RECENT_GROUPS,
    summary_max_chars: int = DEFAULT_SUMMARY_MAX_CHARS,
) -> tuple[list[LLMPayload], int, int]:
    """Bound a snapshot mechanically; semantic text is never generated."""

    del (
        max_part_chars,
        trigger_chars,
        target_chars,
        min_recent_groups,
        summary_max_chars,
    )
    result, _ = mechanically_bound_payloads(
        payloads,
        estimate=estimate,
        hard_budget=max(1, int(char_budget)),
        reference_max_groups=max(1, int(max_groups)),
        reference_max_bytes=DEFAULT_EMERGENCY_REFERENCE_MAX_BYTES,
    )
    return result.payloads, estimate(payloads), estimate(result.payloads)


__all__ = [
    "DEFAULT_MAX_GROUPS",
    "DEFAULT_MAX_PART_CHARS",
    "DEFAULT_MIN_RECENT_GROUPS",
    "DEFAULT_SNAPSHOT_CHAR_BUDGET",
    "DEFAULT_SUMMARY_MAX_CHARS",
    "DEFAULT_TARGET_CHARS",
    "DEFAULT_TRIGGER_CHARS",
    "SUMMARY_CLOSE",
    "SUMMARY_INTRO",
    "SUMMARY_OPEN",
    "ContextCompactionResult",
    "build_conversation_groups",
    "compact_payloads",
    "compress_dropped_payload_groups",
    "hierarchical_compact_payloads",
    "is_summary_payload",
    "split_pinned_and_tail",
]
