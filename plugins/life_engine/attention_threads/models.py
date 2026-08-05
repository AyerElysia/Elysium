"""Pure AttentionThread state transitions derived from immutable events."""

from __future__ import annotations

import hashlib

from .contracts import (
    ATTENTION_THREAD_ACTIONS,
    AttentionThreadEvent,
    AttentionThreadTransitionError,
    AttentionThreadView,
)


def apply_attention_thread_event(
    current: AttentionThreadView | None,
    event: AttentionThreadEvent,
) -> AttentionThreadView:
    """Apply one structurally valid event without inferring subjective meaning."""

    action = event.action
    if action not in ATTENTION_THREAD_ACTIONS:
        raise AttentionThreadTransitionError(f"unsupported attention action: {action}")
    if current is None:
        if action != "open" or event.expected_revision != 0 or event.revision != 1:
            raise AttentionThreadTransitionError(
                "an attention thread must begin with revision-1 open"
            )
        statement = event.public_statement
        return AttentionThreadView(
            thread_id=event.thread_id,
            status="open",
            revision=event.revision,
            opened_at=event.occurred_at,
            last_changed_at=event.occurred_at,
            current_statement=statement,
            statement_event_id=event.event_id,
            statement_sha256=hashlib.sha256(statement.encode("utf-8")).hexdigest(),
            statement_bytes=len(statement.encode("utf-8")),
            last_event_id=event.event_id,
            last_occurrence_id=event.occurrence_id,
            last_event_position=event.position,
        )

    if event.thread_id != current.thread_id:
        raise AttentionThreadTransitionError("attention event thread identity changed")
    if event.expected_revision != current.revision:
        raise AttentionThreadTransitionError("attention event expected revision is stale")
    if event.revision != current.revision + 1:
        raise AttentionThreadTransitionError("attention event revision is not monotonic")
    if event.position <= current.last_event_position:
        raise AttentionThreadTransitionError("attention event position is not monotonic")
    if current.status == "closed":
        raise AttentionThreadTransitionError("closed attention threads are terminal")

    if action == "open":
        raise AttentionThreadTransitionError("an existing attention thread cannot reopen")
    if action == "note" and current.status != "open":
        raise AttentionThreadTransitionError("a paused thread must explicitly resume")
    if action == "pause" and current.status != "open":
        raise AttentionThreadTransitionError("only an open thread can pause")
    if action == "resume" and current.status != "paused":
        raise AttentionThreadTransitionError("only a paused thread can resume")

    status = current.status
    if action == "pause":
        status = "paused"
    elif action == "resume":
        status = "open"
    elif action == "close":
        status = "closed"

    statement = current.current_statement
    statement_event_id = current.statement_event_id
    statement_sha256 = current.statement_sha256
    statement_bytes = current.statement_bytes
    if event.public_statement:
        statement = event.public_statement
        statement_event_id = event.event_id
        statement_sha256 = hashlib.sha256(statement.encode("utf-8")).hexdigest()
        statement_bytes = len(statement.encode("utf-8"))

    return AttentionThreadView(
        thread_id=current.thread_id,
        status=status,
        revision=event.revision,
        opened_at=current.opened_at,
        last_changed_at=event.occurred_at,
        current_statement=statement,
        statement_event_id=statement_event_id,
        statement_sha256=statement_sha256,
        statement_bytes=statement_bytes,
        last_event_id=event.event_id,
        last_occurrence_id=event.occurrence_id,
        last_event_position=event.position,
    )


__all__ = ["apply_attention_thread_event"]
