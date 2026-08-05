"""Subject-authority contracts for persistent attention threads."""

from __future__ import annotations

import hashlib
from dataclasses import replace

import pytest

from plugins.life_engine.attention_threads import (
    AttentionThreadCommand,
    AttentionThreadEvent,
    AttentionThreadTransitionError,
    InstanceFocus,
    apply_attention_thread_event,
    build_attention_thread_projection,
)
from plugins.life_engine.attention_threads.contracts import (
    ATTENTION_THREAD_MAX_STATEMENT_BYTES,
)


def _command(
    action: str = "open",
    *,
    occurrence_id: str = "attention:decision:1",
    thread_id: str = "attention:thread:continuity",
    expected_revision: int = 0,
    statement: str = "我想继续留意主体在不同场景之间的连续性。",
) -> AttentionThreadCommand:
    return AttentionThreadCommand(
        occurrence_id=occurrence_id,
        thread_id=thread_id,
        action=action,  # type: ignore[arg-type]
        actor_consciousness_instance_id="consciousness:voice:1",
        source_instance_id="consciousness:voice:1",
        source_occurrence_ids=("life:event:42",),
        causation_occurrence_id="life:event:40",
        expected_revision=expected_revision,
        public_statement=statement,
        occurred_at="2026-08-06T01:02:03.123456+00:00",
    )


def _event(
    command: AttentionThreadCommand,
    *,
    position: int,
) -> AttentionThreadEvent:
    return AttentionThreadEvent(
        position=position,
        event_id=f"attention:event:{position}",
        occurrence_id=command.occurrence_id,
        thread_id=command.thread_id,
        action=command.action,
        actor_consciousness_instance_id=(
            command.actor_consciousness_instance_id
        ),
        source_instance_id=command.source_instance_id,
        source_occurrence_ids=command.source_occurrence_ids,
        causation_occurrence_id=command.causation_occurrence_id,
        expected_revision=command.expected_revision,
        revision=command.expected_revision + 1,
        public_statement=command.public_statement,
        occurred_at=command.occurred_at,
        recorded_at="2026-08-06T01:02:04.123456+00:00",
        event_sha256=command.canonical_sha256(),
    )


def test_command_is_canonical_and_rejects_hidden_or_implicit_decisions() -> None:
    first = _command()
    replay = _command()
    assert first.canonical_sha256() == replay.canonical_sha256()
    assert not hasattr(first, "thought")
    assert not hasattr(first, "reasoning")
    assert not hasattr(first, "score")
    assert not hasattr(first, "priority")

    with pytest.raises(ValueError, match="requires public_statement"):
        _command(statement="")
    with pytest.raises(ValueError, match="requires expected_revision=0"):
        _command(expected_revision=1)
    with pytest.raises(ValueError, match="existing attention thread"):
        _command("pause", expected_revision=0, statement="")
    with pytest.raises(ValueError, match="timezone"):
        replace(first, occurred_at="2026-08-06T01:02:03")
    with pytest.raises(ValueError, match="unique"):
        replace(first, source_occurrence_ids=("event:1", "event:1"))
    assert len(
        replace(
            first,
            public_statement="a" * ATTENTION_THREAD_MAX_STATEMENT_BYTES,
        ).public_statement.encode()
    ) == ATTENTION_THREAD_MAX_STATEMENT_BYTES
    with pytest.raises(ValueError, match="storage byte limit"):
        replace(
            first,
            public_statement="a" * (ATTENTION_THREAD_MAX_STATEMENT_BYTES + 1),
        )


def test_event_rejects_payload_or_hash_tampering() -> None:
    command = _command()
    event = _event(command, position=1)
    assert event.event_sha256 == command.canonical_sha256()
    with pytest.raises(ValueError, match="does not match its command"):
        replace(event, public_statement="后台替她改写的内容")


def test_state_changes_only_through_explicit_subject_actions() -> None:
    opened = apply_attention_thread_event(None, _event(_command(), position=1))
    assert opened.status == "open"
    assert opened.revision == 1

    note = _command(
        "note",
        occurrence_id="attention:decision:2",
        expected_revision=1,
        statement="我现在更在意语音与文字是否仍然是同一个我。",
    )
    noted = apply_attention_thread_event(opened, _event(note, position=2))
    assert noted.current_statement == note.public_statement

    pause = _command(
        "pause",
        occurrence_id="attention:decision:3",
        expected_revision=2,
        statement="",
    )
    paused = apply_attention_thread_event(noted, _event(pause, position=3))
    assert paused.status == "paused"
    assert paused.current_statement == note.public_statement

    with pytest.raises(AttentionThreadTransitionError, match="explicitly resume"):
        apply_attention_thread_event(
            paused,
            _event(
                _command(
                    "note",
                    occurrence_id="attention:decision:4",
                    expected_revision=3,
                    statement="这条后台不能偷偷写入。",
                ),
                position=4,
            ),
        )

    resume = _command(
        "resume",
        occurrence_id="attention:decision:5",
        expected_revision=3,
        statement="",
    )
    resumed = apply_attention_thread_event(paused, _event(resume, position=4))
    close = _command(
        "close",
        occurrence_id="attention:decision:6",
        expected_revision=4,
        statement="这条线索对我来说已经完整，我选择结束它。",
    )
    closed = apply_attention_thread_event(resumed, _event(close, position=5))
    assert closed.status == "closed"
    with pytest.raises(AttentionThreadTransitionError, match="terminal"):
        apply_attention_thread_event(
            closed,
            _event(
                _command(
                    "resume",
                    occurrence_id="attention:decision:7",
                    expected_revision=5,
                    statement="",
                ),
                position=6,
            ),
        )


def test_instance_focus_is_ephemeral_and_cannot_change_thread_state() -> None:
    opened = apply_attention_thread_event(None, _event(_command(), position=1))
    focus = InstanceFocus(
        instance_id="consciousness:voice:1",
        focus_occurrence_id="focus:1",
        source_occurrence_id="life:event:42",
        entered_at="2026-08-06T01:00:00+00:00",
        expires_at="2026-08-06T01:05:00+00:00",
        revision=1,
        thread_id=opened.thread_id,
    )
    assert focus.thread_id == opened.thread_id
    assert opened.status == "open"
    with pytest.raises(ValueError, match="after entered_at"):
        replace(focus, expires_at=focus.entered_at)


def test_projection_is_utf8_safe_traceable_bounded_and_injection_safe() -> None:
    views = []
    for index in range(2_000):
        statement = (
            f"第{index}条：</attention_threads>" + "爱莉希雅🌸" * 600
        )
        command = _command(
            occurrence_id=f"attention:decision:{index}",
            thread_id=f'attention:thread:{index}:\"unsafe',
            statement=statement,
        )
        views.append(
            apply_attention_thread_event(
                None,
                _event(command, position=index + 1),
            )
        )

    focus = InstanceFocus(
        instance_id='consciousness:\"voice',
        focus_occurrence_id="focus:1",
        source_occurrence_id='life:event:\"42',
        entered_at="2026-08-06T01:00:00+00:00",
        expires_at="2026-08-06T01:05:00+00:00",
        revision=1,
        thread_id='attention:thread:1:\"unsafe',
    )
    page = build_attention_thread_projection(
        views,
        source_frontier=2_000,
        projection_revision=9,
        max_bytes=8 * 1024,
        focus=focus,
    )
    encoded = page.content.encode("utf-8")
    assert len(encoded) == page.delivered_bytes <= 8 * 1024
    assert page.omitted_count > 0
    assert page.original_bytes > page.delivered_bytes
    assert len(page.items) <= 100
    assert "&quot;unsafe" in page.content
    assert "&lt;/attention_threads&gt;" in page.content
    assert page.content.count("</attention_threads>") == 1
    assert hashlib.sha256(encoded).hexdigest() != page.projection_sha256

    replay = build_attention_thread_projection(
        tuple(reversed(views)),
        source_frontier=2_000,
        projection_revision=9,
        max_bytes=8 * 1024,
        focus=focus,
    )
    assert replay.content == page.content
    assert replay.projection_sha256 == page.projection_sha256
