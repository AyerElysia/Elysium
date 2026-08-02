"""Contract tests for append-only epistemic memory primitives."""

from __future__ import annotations

import sqlite3

import pytest

from plugins.life_engine.memory.epistemic import (
    AuthorityClass,
    ClaimEvidence,
    ClaimStatus,
    EpistemicConflict,
    EvidenceStance,
    MemoryBelief,
    MemoryStateEvent,
    RetrievalEpisode,
    RetrievalExposure,
    RetrievalFeedback,
    append_belief,
    append_claim,
    append_claim_evidence,
    append_conflict,
    append_state_event,
    append_retrieval_episode,
    append_retrieval_exposure,
    append_retrieval_feedback,
    build_memory_audit_trail,
    create_epistemic_schema,
    get_claim_state,
    get_memory_disposition,
    get_retrieval_plasticity,
    list_claim_states,
    new_claim,
    project_current_facts,
    reduce_claim_state,
)


def _db() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    create_epistemic_schema(db)
    return db


def _claim(
    *,
    claim_id: str = "claim-1",
    source: str = "reflection",
    valid_from: str = "2026-07-01T00:00:00+08:00",
    valid_to: str = "",
    recorded_at: str = "2026-07-01T01:00:00+08:00",
):
    return new_claim(
        claim_id=claim_id,
        subject_key="project:elysia:location",
        content="爱莉当前在花园。",
        claim_kind="world_fact",
        source=source,
        valid_from=valid_from,
        valid_to=valid_to,
        recorded_at=recorded_at,
    )


def test_claim_is_immutable_and_identity_conflicts_are_explicit() -> None:
    db = _db()
    claim = append_claim(db, _claim())

    assert append_claim(db, claim) == claim
    with pytest.raises(ValueError, match="ClaimIdentityConflict:claim-1"):
        append_claim(db, _claim(source="user"))
    with pytest.raises(sqlite3.IntegrityError, match="EpistemicRecordImmutable"):
        db.execute("UPDATE memory_claims SET content = '伪造' WHERE claim_id = ?", (claim.claim_id,))
    with pytest.raises(sqlite3.IntegrityError, match="EpistemicRecordImmutable"):
        db.execute("DELETE FROM memory_claims WHERE claim_id = ?", (claim.claim_id,))


def test_evidence_requires_existing_claim_and_remains_append_only() -> None:
    db = _db()
    claim = append_claim(db, _claim())
    evidence = ClaimEvidence(
        evidence_link_id="evidence-1",
        claim_id=claim.claim_id,
        evidence_kind="experience",
        evidence_ref="event-123",
        stance=EvidenceStance.SUPPORTS.value,
        source_excerpt="用户说：我在花园。",
        recorded_at="2026-07-01T01:01:00+08:00",
    )

    assert append_claim_evidence(db, evidence) == evidence
    with pytest.raises(ValueError, match="EpistemicEntityMissing:claim:missing"):
        append_claim_evidence(
            db,
            ClaimEvidence(
                evidence_link_id="missing-evidence",
                claim_id="missing",
                evidence_kind="experience",
                evidence_ref="event-404",
                stance=EvidenceStance.CONTEXT.value,
                source_excerpt="",
                recorded_at="2026-07-01T01:02:00+08:00",
            ),
        )
    with pytest.raises(sqlite3.IntegrityError, match="EpistemicRecordImmutable"):
        db.execute("DELETE FROM memory_claim_evidence WHERE evidence_link_id = 'evidence-1'")


def test_state_events_preserve_open_authority_declarations() -> None:
    db = _db()
    claim = append_claim(db, _claim())

    reflection_event = append_state_event(
        db,
        MemoryStateEvent(
            event_id="reflection-confirm",
            entity_type="claim",
            entity_id=claim.claim_id,
            event_type="claim_confirmed",
            actor="reflection",
            authority="independent_reflection_assessment",
            reason="独立反思过程显式作出判断",
            recorded_at="2026-07-01T01:10:00+08:00",
            valid_at="2026-07-01T01:10:00+08:00",
        ),
    )

    event = append_state_event(
        db,
        MemoryStateEvent(
            event_id="user-confirm",
            entity_type="claim",
            entity_id=claim.claim_id,
            event_type="claim_confirmed",
            actor="user",
            authority=AuthorityClass.EXPLICIT_USER.value,
            reason="用户明确确认",
            recorded_at="2026-07-01T01:11:00+08:00",
            valid_at="2026-07-01T01:11:00+08:00",
        ),
    )

    state = get_claim_state(db, claim.claim_id)
    assert state is not None
    assert state.status == ClaimStatus.CONFIRMED.value
    assert state.active_event_ids == (reflection_event.event_id, event.event_id)


def test_supersession_preserves_history_and_bitemporal_query() -> None:
    db = _db()
    old = append_claim(db, _claim(claim_id="old", source="user"))
    new = append_claim(
        db,
        new_claim(
            claim_id="new",
            subject_key=old.subject_key,
            content="爱莉当前在海边。",
            claim_kind="world_fact",
            source="user",
            valid_from="2026-07-05T00:00:00+08:00",
            recorded_at="2026-07-06T00:00:00+08:00",
        ),
    )
    append_state_event(
        db,
        MemoryStateEvent(
            event_id="old-superseded",
            entity_type="claim",
            entity_id=old.claim_id,
            event_type="claim_superseded",
            actor="user",
            authority=AuthorityClass.EXPLICIT_USER.value,
            reason="世界状态变化",
            recorded_at="2026-07-06T00:00:00+08:00",
            valid_at="2026-07-05T00:00:00+08:00",
            payload={"successor_claim_id": new.claim_id},
        ),
    )

    historical = list_claim_states(
        db,
        old.subject_key,
        recorded_as_of="2026-07-04T00:00:00+08:00",
        valid_at="2026-07-02T00:00:00+08:00",
    )
    current = list_claim_states(
        db,
        old.subject_key,
        recorded_as_of="2026-07-07T00:00:00+08:00",
        valid_at="2026-07-07T00:00:00+08:00",
    )

    assert [state.claim.claim_id for state in historical] == [old.claim_id]
    assert [state.claim.claim_id for state in current] == [old.claim_id, new.claim_id]
    assert current[0].status == ClaimStatus.SUPERSEDED.value
    assert current[0].superseded_by == (new.claim_id,)
    assert current[1].status == ClaimStatus.PROPOSED.value


def test_current_projection_excludes_superseded_claim_and_keeps_conflicts() -> None:
    db = _db()
    old = append_claim(db, _claim(claim_id="old-current", source="user"))
    current = append_claim(
        db,
        new_claim(
            claim_id="current",
            subject_key=old.subject_key,
            content="爱莉当前在海边。",
            claim_kind="world_fact",
            source="user",
            valid_from="2026-07-05T00:00:00+08:00",
            recorded_at="2026-07-06T00:00:00+08:00",
        ),
    )
    alternative = append_claim(
        db,
        new_claim(
            claim_id="alternative",
            subject_key=old.subject_key,
            content="爱莉当前在山上。",
            claim_kind="world_fact",
            source="reflection",
            valid_from="2026-07-05T00:00:00+08:00",
            recorded_at="2026-07-06T00:01:00+08:00",
        ),
    )
    append_state_event(
        db,
        MemoryStateEvent(
            event_id="supersede-old",
            entity_type="claim",
            entity_id=old.claim_id,
            event_type="claim_superseded",
            actor="user",
            authority=AuthorityClass.EXPLICIT_USER.value,
            reason="位置变化",
            recorded_at="2026-07-06T00:02:00+08:00",
            valid_at="2026-07-05T00:00:00+08:00",
            payload={"successor_claim_id": current.claim_id},
        ),
    )
    append_conflict(
        db,
        EpistemicConflict(
            conflict_id="current-conflict",
            left_claim_id=current.claim_id,
            right_claim_id=alternative.claim_id,
            relation="contradicts",
            reason="地点互斥",
            recorded_at="2026-07-06T00:03:00+08:00",
        ),
    )

    projection = project_current_facts(
        db,
        old.subject_key,
        valid_at="2026-07-07T00:00:00+08:00",
        recorded_as_of="2026-07-07T00:00:00+08:00",
    )

    assert [state.claim.claim_id for state in projection.active_claims] == [
        current.claim_id,
        alternative.claim_id,
    ]
    assert projection.conflicts[0].conflict_id == "current-conflict"
    assert "未裁决冲突" in projection.uncertainty[0]


def test_reversal_replays_prior_state_without_mutating_history() -> None:
    db = _db()
    claim = append_claim(db, _claim(source="user"))
    disputed = append_state_event(
        db,
        MemoryStateEvent(
            event_id="dispute-1",
            entity_type="claim",
            entity_id=claim.claim_id,
            event_type="claim_disputed",
            actor="user",
            authority=AuthorityClass.EXPLICIT_USER.value,
            reason="暂时发现矛盾",
            recorded_at="2026-07-02T01:00:00+08:00",
            valid_at="2026-07-02T01:00:00+08:00",
        ),
    )
    append_state_event(
        db,
        MemoryStateEvent(
            event_id="undo-dispute-1",
            entity_type="claim",
            entity_id=claim.claim_id,
            event_type="state_event_reversed",
            actor="user",
            authority=AuthorityClass.EXPLICIT_USER.value,
            reason="矛盾已排除",
            recorded_at="2026-07-02T02:00:00+08:00",
            valid_at="2026-07-02T02:00:00+08:00",
            reverses_event_id=disputed.event_id,
        ),
    )

    state = get_claim_state(db, claim.claim_id)
    assert state is not None
    assert state.status == ClaimStatus.PROPOSED.value
    assert state.active_event_ids == ("undo-dispute-1",)
    assert db.execute("SELECT COUNT(*) FROM memory_state_events").fetchone()[0] == 2


def test_subjective_forgetting_dimensions_are_independent_and_reversible() -> None:
    db = _db()
    claim = append_claim(db, _claim(source="user"))
    events = [
        MemoryStateEvent(
            event_id="seal-access",
            entity_type="claim",
            entity_id=claim.claim_id,
            event_type="accessibility_set",
            actor="elysia",
            authority=AuthorityClass.SUBJECT.value,
            reason="暂时不想触及",
            recorded_at="2026-07-03T01:00:00+08:00",
            valid_at="2026-07-03T01:00:00+08:00",
            payload={"accessibility": "sealed"},
        ),
        MemoryStateEvent(
            event_id="inhibit-context",
            entity_type="claim",
            entity_id=claim.claim_id,
            event_type="context_inhibited",
            actor="elysia",
            authority=AuthorityClass.SUBJECT.value,
            reason="避免在公开场景被唤起",
            recorded_at="2026-07-03T01:01:00+08:00",
            valid_at="2026-07-03T01:01:00+08:00",
            payload={"context": "public"},
        ),
        MemoryStateEvent(
            event_id="lower-salience",
            entity_type="claim",
            entity_id=claim.claim_id,
            event_type="narrative_salience_set",
            actor="elysia",
            authority=AuthorityClass.SUBJECT.value,
            reason="不再放在自我叙事中心",
            recorded_at="2026-07-03T01:02:00+08:00",
            valid_at="2026-07-03T01:02:00+08:00",
            payload={"narrative_salience": 0.2},
        ),
    ]
    for event in events:
        append_state_event(db, event)
    append_state_event(
        db,
        MemoryStateEvent(
            event_id="undo-seal",
            entity_type="claim",
            entity_id=claim.claim_id,
            event_type="state_event_reversed",
            actor="elysia",
            authority=AuthorityClass.SUBJECT.value,
            reason="现在愿意重新接触",
            recorded_at="2026-07-03T01:03:00+08:00",
            valid_at="2026-07-03T01:03:00+08:00",
            reverses_event_id="seal-access",
        ),
    )

    disposition = get_memory_disposition(db, "claim", claim.claim_id)
    assert disposition.accessibility == "available"
    assert disposition.contextual_inhibition == ("public",)
    assert disposition.narrative_salience == 0.2
    assert db.execute("SELECT COUNT(*) FROM memory_claims").fetchone()[0] == 1


def test_belief_is_distinct_from_claim_and_conflicts_never_auto_resolve() -> None:
    db = _db()
    first = append_claim(db, _claim(claim_id="first", source="user"))
    second = append_claim(
        db,
        new_claim(
            claim_id="second",
            subject_key=first.subject_key,
            content="爱莉不在花园。",
            claim_kind="world_fact",
            source="reflection",
            recorded_at="2026-07-02T01:00:00+08:00",
        ),
    )
    belief = append_belief(
        db,
        MemoryBelief(
            belief_id="belief-1",
            claim_id=first.claim_id,
            perspective_subject_id="elysia",
            consciousness_instance_id="main",
            recorded_at="2026-07-02T01:01:00+08:00",
        ),
    )
    conflict = append_conflict(
        db,
        EpistemicConflict(
            conflict_id="conflict-1",
            left_claim_id=first.claim_id,
            right_claim_id=second.claim_id,
            relation="contradicts",
            reason="两条主张不能同时为真",
            recorded_at="2026-07-02T01:02:00+08:00",
        ),
    )

    assert belief.claim_id == first.claim_id
    assert conflict.left_claim_id == first.claim_id
    assert get_claim_state(db, first.claim_id).status == ClaimStatus.PROPOSED.value
    assert get_claim_state(db, second.claim_id).status == ClaimStatus.PROPOSED.value


def test_retrieval_feedback_changes_only_retrieval_hint_not_claim_truth() -> None:
    db = _db()
    claim = append_claim(db, _claim(source="reflection"))
    episode = append_retrieval_episode(
        db,
        RetrievalEpisode(
            episode_id="episode-1",
            query="爱莉在哪里",
            mode="current_fact",
            consciousness_instance_id="main",
            stream_scope="chat-1",
            recorded_at="2026-07-04T02:00:00+08:00",
        ),
    )
    exposure = append_retrieval_exposure(
        db,
        RetrievalExposure(
            exposure_id="exposure-1",
            episode_id=episode.episode_id,
            entity_type="claim",
            entity_id=claim.claim_id,
            rank_position=1,
            retrieval_source="hybrid",
            recorded_at="2026-07-04T02:00:01+08:00",
        ),
    )
    append_retrieval_feedback(
        db,
        RetrievalFeedback(
            feedback_id="feedback-1",
            exposure_id=exposure.exposure_id,
            feedback="accepted",
            actor="elysia",
            reason="这次检索有帮助",
            recorded_at="2026-07-04T02:01:00+08:00",
        ),
    )

    plasticity = get_retrieval_plasticity(db, "claim", claim.claim_id)
    claim_state = get_claim_state(db, claim.claim_id)
    assert plasticity.accepted_count == 1
    assert plasticity.retrieval_affinity == 1.0
    assert "not evidence of truth" in plasticity.epistemic_note
    assert claim_state is not None
    assert claim_state.status == ClaimStatus.PROPOSED.value


def test_audit_trail_preserves_actor_reason_cause_and_compensation() -> None:
    db = _db()
    claim = append_claim(db, _claim(source="user"))
    source = append_state_event(
        db,
        MemoryStateEvent(
            event_id="source-event",
            entity_type="claim",
            entity_id=claim.claim_id,
            event_type="claim_disputed",
            actor="elysia",
            authority=AuthorityClass.SUBJECT.value,
            reason="我感到这里有矛盾",
            recorded_at="2026-07-04T01:00:00+08:00",
            valid_at="2026-07-04T01:00:00+08:00",
        ),
    )
    followup = append_state_event(
        db,
        MemoryStateEvent(
            event_id="followup-event",
            entity_type="claim",
            entity_id=claim.claim_id,
            event_type="narrative_salience_set",
            actor="elysia",
            authority=AuthorityClass.SUBJECT.value,
            reason="先降低它在叙事里的位置",
            recorded_at="2026-07-04T01:01:00+08:00",
            valid_at="2026-07-04T01:01:00+08:00",
            caused_by_event_id=source.event_id,
            payload={"narrative_salience": 0.1},
        ),
    )
    append_state_event(
        db,
        MemoryStateEvent(
            event_id="compensation-event",
            entity_type="claim",
            entity_id=claim.claim_id,
            event_type="state_event_reversed",
            actor="elysia",
            authority=AuthorityClass.SUBJECT.value,
            reason="重新审视后撤销降低显著性的决定",
            recorded_at="2026-07-04T01:02:00+08:00",
            valid_at="2026-07-04T01:02:00+08:00",
            reverses_event_id=followup.event_id,
        ),
    )

    trail = build_memory_audit_trail(db, "claim", claim.claim_id)
    assert [entry.event.event_id for entry in trail] == [
        source.event_id,
        followup.event_id,
        "compensation-event",
    ]
    assert trail[0].active is True
    assert trail[1].active is False
    assert trail[1].reversed_by == ("compensation-event",)
    assert trail[1].cause == source


def test_pure_reducer_does_not_require_database() -> None:
    claim = _claim(source="user")
    state = reduce_claim_state(
        claim,
        [
            MemoryStateEvent(
                event_id="confirm",
                entity_type="claim",
                entity_id=claim.claim_id,
                event_type="claim_confirmed",
                actor="user",
                authority=AuthorityClass.EXPLICIT_USER.value,
                reason="确认",
                recorded_at="2026-07-01T02:00:00+08:00",
                valid_at="2026-07-01T02:00:00+08:00",
            )
        ],
    )

    assert state.status == ClaimStatus.CONFIRMED.value
