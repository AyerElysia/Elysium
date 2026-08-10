"""Immutable Learning evidence intake without a projection owner.

This runtime deliberately has no InsightStore, SkillStore, maintenance journal,
or prompt projection.  It exists so any generation writer can keep appending
exact learning opportunities while another instance owns (or has lost) the
single database-fenced Learning projector lease.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from ..storage.learning_contracts import LearningEventDraft, LearningStorePort
from .reflection_queue import (
    REFLECTION_QUEUE_STATE_KEY,
    LearningReflectionJob,
    ReflectionJobKind,
)

logger = logging.getLogger("life_engine.learning.event_only")


class LearningEventOnlyRecorder:
    """Append immutable learning opportunities without deriving a second view."""

    projector_owner = False

    def __init__(
        self,
        store: LearningStorePort,
        *,
        writer_instance_id: str,
        reason: str,
        error_type: str = "",
    ) -> None:
        self._store = store
        self._writer_instance_id = str(writer_instance_id).strip()
        self._reason = str(reason).strip() or "learning projector is not owned"
        self._error_type = str(error_type).strip()
        self._last_event_append_at = ""
        self._last_event_error_type = ""

    async def initialize(self) -> None:
        """Initialize without opening a store or constructing projections."""

    async def flush(self) -> None:
        """No-op because each immutable event is committed before returning."""

    async def close(self) -> None:
        """Release no resources; the service owns the injected runtime."""

    async def enqueue_reflection(
        self,
        *,
        reflection_kind: ReflectionJobKind,
        reflection_text: str,
        context: str = "",
        source_event_ids: list[str] | None = None,
        actor_consciousness_instance_id: str = "",
    ) -> str:
        """Append one exact reflection opportunity without running cognition."""

        job = LearningReflectionJob.create(
            reflection_kind=reflection_kind,
            reflection_text=reflection_text,
            context=context,
            source_event_ids=source_event_ids,
            actor_consciousness_instance_id=actor_consciousness_instance_id,
        )
        draft = LearningEventDraft(
            occurrence_id=job.job_id,
            event_kind="reflection.enqueued",
            occurred_at=job.created_at,
            source=f"learning.{job.reflection_kind}",
            actor_consciousness_instance_id=job.actor_consciousness_instance_id,
            subject_revision="",
            provenance={
                "schema_version": 1,
                "queue": REFLECTION_QUEUE_STATE_KEY,
                "writer_instance_id": self._writer_instance_id,
                "projector_owner": False,
            },
            payload=job.to_dict(),
        )
        try:
            await self._store.commit(events=[draft], projections=[])
        except BaseException as exc:
            self._last_event_error_type = type(exc).__name__
            raise
        self._last_event_append_at = datetime.now(UTC).isoformat()
        self._last_event_error_type = ""
        return job.job_id

    async def submit_reflection(self, **kwargs: Any) -> None:
        """Persist a request and leave execution to a future projector owner."""

        await self.enqueue_reflection(**kwargs)

    async def on_interaction_end(
        self,
        *,
        interaction_text: str,
        context: str = "",
        source_event_ids: list[str] | None = None,
        actor_consciousness_instance_id: str = "",
    ) -> None:
        """Record one interaction opportunity; foreground expression still wins."""

        try:
            await self.enqueue_reflection(
                reflection_kind="interaction",
                reflection_text=interaction_text,
                context=context,
                source_event_ids=source_event_ids,
                actor_consciousness_instance_id=actor_consciousness_instance_id,
            )
        except Exception as exc:  # noqa: BLE001 - foreground must remain available
            logger.warning(
                "interaction learning evidence append failed: %s",
                type(exc).__name__,
            )

    async def on_thought_closed(
        self,
        *,
        thought_summary: str,
        context: str = "",
        source_event_ids: list[str] | None = None,
        actor_consciousness_instance_id: str = "",
    ) -> None:
        """Record a public thought-close opportunity without raw CoT handling."""

        try:
            await self.enqueue_reflection(
                reflection_kind="introspection",
                reflection_text=thought_summary,
                context=context,
                source_event_ids=source_event_ids,
                actor_consciousness_instance_id=actor_consciousness_instance_id,
            )
        except Exception as exc:  # noqa: BLE001 - source event remains authoritative
            logger.warning(
                "thought-close learning evidence append failed: %s",
                type(exc).__name__,
            )

    async def on_attention_thread_closed(
        self,
        *,
        public_statement: str,
        source_event_ids: list[str],
        actor_consciousness_instance_id: str,
    ) -> None:
        """Record an explicit public close statement, never a private trace."""

        statement = str(public_statement or "").strip()
        actor = str(actor_consciousness_instance_id or "").strip()
        sources = [
            str(value).strip() for value in source_event_ids if str(value).strip()
        ]
        if not statement or not actor or not sources:
            raise ValueError(
                "attention close learning requires statement, actor, and sources"
            )
        try:
            await self.enqueue_reflection(
                reflection_kind="introspection",
                reflection_text=statement,
                context="subject-authored attention thread close statement",
                source_event_ids=sources,
                actor_consciousness_instance_id=actor,
            )
        except Exception as exc:  # noqa: BLE001 - authority close already committed
            logger.warning(
                "attention-close learning evidence append failed: %s",
                type(exc).__name__,
            )

    async def run(self, stop_event: Any, **_: Any) -> None:
        """Wait for shutdown; event-only instances never run maintenance."""

        await stop_event.wait()

    def get_state(self) -> dict[str, Any]:
        """Return content-free health that cannot masquerade as a projection."""

        return {
            "status": "degraded",
            "mode": "event_only",
            "projector_owner": False,
            "event_append_available": True,
            "reason": self._reason,
            "error_type": self._error_type,
            "last_event_append_at": self._last_event_append_at,
            "last_event_error_type": self._last_event_error_type,
            "reflection_available": False,
            "reflection_queue": {
                "status": "degraded",
                "count_known": False,
                "reason": "queue projection requires the singleton owner",
            },
            "maintenance": {
                "status": "disabled",
                "reason": "singleton projector is not owned",
            },
            "worker": {"status": "disabled", "running": False},
            "selected_persistence": {
                "status": "disabled",
                "projector_owner": False,
            },
            "prompt_projections": {
                "status": "disabled",
                "reason": "stale projections are not exposed by a non-owner",
            },
        }

    def get_knowledge_for_prompt(self, max_chars: int = 0) -> str:
        del max_chars
        return ""

    def get_skill_catalog_for_prompt(self, max_chars: int = 0) -> str:
        del max_chars
        return ""

    def get_progress_for_prompt(self) -> str:
        return ""

    async def get_subject_review_prompt(self) -> str:
        return ""

    def get_pending_subject_review_offer(self) -> None:
        return None

    async def commit_subject_review_offer_delivery(self, *_: Any, **__: Any) -> bool:
        return False


__all__ = ["LearningEventOnlyRecorder"]
