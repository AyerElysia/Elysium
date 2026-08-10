"""Contracts for immutable Learning intake without a projector owner."""

from __future__ import annotations

from typing import Any

from plugins.life_engine.learning.event_only import LearningEventOnlyRecorder
from plugins.life_engine.storage.learning_contracts import LearningCommitResult


class _RecordingLearningStore:
    def __init__(self) -> None:
        self.commits: list[dict[str, Any]] = []

    async def commit(self, **kwargs: Any) -> LearningCommitResult:
        self.commits.append(dict(kwargs))
        return LearningCommitResult(events=(), projections=())


async def test_event_only_recorder_appends_exact_evidence_without_projection() -> None:
    store = _RecordingLearningStore()
    recorder = LearningEventOnlyRecorder(
        store,  # type: ignore[arg-type]
        writer_instance_id="writer-a",
        reason="projector owned elsewhere",
    )

    result = await recorder.submit_reflection(
        reflection_kind="interaction",
        reflection_text="experience",
        context="bounded context",
        source_event_ids=["event-1"],
        actor_consciousness_instance_id="chat_global",
    )

    assert result is None
    assert len(store.commits) == 1
    assert store.commits[0]["projections"] == []
    draft = store.commits[0]["events"][0]
    assert draft.event_kind == "reflection.enqueued"
    assert draft.occurrence_id == draft.payload["job_id"]
    assert draft.actor_consciousness_instance_id == "chat_global"
    assert draft.provenance == {
        "schema_version": 1,
        "queue": "pending_reflections_v1",
        "writer_instance_id": "writer-a",
        "projector_owner": False,
    }
    assert not hasattr(recorder, "store")
    assert not hasattr(recorder, "skill_store")


async def test_event_only_recorder_exposes_no_stale_prompt_projection() -> None:
    recorder = LearningEventOnlyRecorder(
        _RecordingLearningStore(),  # type: ignore[arg-type]
        writer_instance_id="writer-a",
        reason="writer lease lost",
        error_type="SingletonWriterClaimLost",
    )

    assert recorder.get_knowledge_for_prompt(max_chars=10_000) == ""
    assert recorder.get_skill_catalog_for_prompt(max_chars=10_000) == ""
    assert recorder.get_progress_for_prompt() == ""
    assert await recorder.get_subject_review_prompt() == ""
    health = recorder.get_state()
    assert health["status"] == "degraded"
    assert health["mode"] == "event_only"
    assert health["projector_owner"] is False
    assert health["event_append_available"] is True
    assert health["selected_persistence"]["status"] == "disabled"


async def test_event_only_recorder_accepts_service_interaction_contract() -> None:
    store = _RecordingLearningStore()
    recorder = LearningEventOnlyRecorder(
        store,  # type: ignore[arg-type]
        writer_instance_id="writer-a",
        reason="writer lease lost",
    )

    await recorder.on_interaction_end(
        interaction_text="public response",
        context="bounded perception",
        source_event_ids=[" event-1 "],
        actor_consciousness_instance_id="chat_global",
    )
    await recorder.on_thought_closed(
        thought_summary="public summary",
        source_event_ids=["event-2"],
        actor_consciousness_instance_id="chat_global",
    )
    await recorder.on_attention_thread_closed(
        public_statement="I am done with this thread",
        source_event_ids=[" event-3 "],
        actor_consciousness_instance_id="chat_global",
    )

    assert len(store.commits) == 3
    interaction = store.commits[0]["events"][0]
    introspection = store.commits[1]["events"][0]
    attention_close = store.commits[2]["events"][0]
    assert interaction.event_kind == "reflection.enqueued"
    assert interaction.payload["reflection_text"] == "public response"
    assert interaction.payload["source_event_ids"] == ["event-1"]
    assert introspection.payload["reflection_text"] == "public summary"
    assert attention_close.payload["reflection_text"] == (
        "I am done with this thread"
    )
    assert attention_close.payload["source_event_ids"] == ["event-3"]
    assert all(commit["projections"] == [] for commit in store.commits)
