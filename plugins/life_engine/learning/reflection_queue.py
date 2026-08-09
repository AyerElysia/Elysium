"""Durable, content-safe retry envelopes for event-driven reflection work."""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import uuid4

ReflectionJobKind = Literal["interaction", "introspection"]
REFLECTION_QUEUE_STATE_KEY = "pending_reflections_v1"
REFLECTION_RUNTIME_STATE_KEY = "reflection_runtime_v1"
MAX_PENDING_REFLECTIONS = 512
MAX_REFLECTION_JOB_BYTES = 256 * 1024
MAX_REFLECTION_SOURCE_EVENTS = 256


def _timestamp(value: str, *, field: str) -> str:
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO timestamp") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat()


@dataclass(frozen=True, slots=True)
class LearningReflectionJob:
    """One preserved reflection request that may be retried without reclassification."""

    job_id: str
    reflection_kind: ReflectionJobKind
    reflection_text: str
    context: str
    source_event_ids: tuple[str, ...]
    actor_consciousness_instance_id: str
    created_at: str
    next_attempt_at: str
    attempt_count: int = 0
    last_error_type: str = ""
    last_error_fingerprint: str = ""

    @classmethod
    def create(
        cls,
        *,
        reflection_kind: ReflectionJobKind,
        reflection_text: str,
        context: str,
        source_event_ids: list[str] | None = None,
        actor_consciousness_instance_id: str = "",
        created_at: str = "",
        job_id: str = "",
    ) -> LearningReflectionJob:
        if reflection_kind not in {"interaction", "introspection"}:
            raise ValueError("invalid reflection job kind")
        text = str(reflection_text or "")
        projected_context = str(context or "")
        if not text.strip():
            raise ValueError("reflection job text must not be empty")
        delivered_bytes = len(text.encode("utf-8")) + len(
            projected_context.encode("utf-8")
        )
        if delivered_bytes > MAX_REFLECTION_JOB_BYTES:
            raise ValueError("reflection job exceeds the explicit storage budget")
        source_ids = tuple(str(item).strip() for item in (source_event_ids or []))
        if any(not item for item in source_ids):
            raise ValueError("reflection source event identities must not be empty")
        if any(len(item) > 255 for item in source_ids):
            raise ValueError("reflection source event identity exceeds limit")
        if len(source_ids) > MAX_REFLECTION_SOURCE_EVENTS:
            raise ValueError("reflection source event count exceeds the explicit limit")
        now = _timestamp(
            created_at or datetime.now(UTC).isoformat(), field="created_at"
        )
        identity = job_id or f"learning_reflection_{uuid4().hex}"
        actor = str(actor_consciousness_instance_id or "").strip()
        if len(identity) > 255 or len(actor) > 255:
            raise ValueError("reflection job/actor identity exceeds limit")
        return cls(
            job_id=identity,
            reflection_kind=reflection_kind,
            reflection_text=text,
            context=projected_context,
            source_event_ids=source_ids,
            actor_consciousness_instance_id=actor,
            created_at=now,
            next_attempt_at=now,
        )

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> LearningReflectionJob:
        source_ids = value.get("source_event_ids", [])
        if not isinstance(source_ids, list):
            raise TypeError("reflection source event identities must be a list")
        job = cls(
            job_id=str(value.get("job_id", "")).strip(),
            reflection_kind=str(value.get("reflection_kind", "")),  # type: ignore[arg-type]
            reflection_text=str(value.get("reflection_text", "")),
            context=str(value.get("context", "")),
            source_event_ids=tuple(str(item).strip() for item in source_ids),
            actor_consciousness_instance_id=str(
                value.get("actor_consciousness_instance_id", "")
            ).strip(),
            created_at=_timestamp(str(value.get("created_at", "")), field="created_at"),
            next_attempt_at=_timestamp(
                str(value.get("next_attempt_at", "")), field="next_attempt_at"
            ),
            attempt_count=int(value.get("attempt_count", 0) or 0),
            last_error_type=str(value.get("last_error_type", "")),
            last_error_fingerprint=str(value.get("last_error_fingerprint", "")),
        )
        if not job.job_id or job.reflection_kind not in {
            "interaction",
            "introspection",
        }:
            raise ValueError("reflection job identity/kind is invalid")
        if len(job.job_id) > 255 or len(job.actor_consciousness_instance_id) > 255:
            raise ValueError("reflection job/actor identity exceeds limit")
        if not job.reflection_text.strip() or any(
            not item for item in job.source_event_ids
        ):
            raise ValueError("reflection job content/source identity is invalid")
        if any(len(item) > 255 for item in job.source_event_ids):
            raise ValueError("reflection source event identity exceeds limit")
        if job.attempt_count < 0:
            raise ValueError("reflection attempt_count must be non-negative")
        if len(job.source_event_ids) > MAX_REFLECTION_SOURCE_EVENTS or (
            len(job.reflection_text.encode("utf-8")) + len(job.context.encode("utf-8"))
            > MAX_REFLECTION_JOB_BYTES
        ):
            raise ValueError("reflection job exceeds its explicit storage limit")
        return job

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["source_event_ids"] = list(self.source_event_ids)
        return payload

    def due(self, now: datetime | None = None) -> bool:
        current = now or datetime.now(UTC)
        next_attempt = datetime.fromisoformat(self.next_attempt_at)
        return next_attempt <= current.astimezone(UTC)

    def failed(self, error: Exception) -> LearningReflectionJob:
        attempts = self.attempt_count + 1
        delay_seconds = min(3_600, 30 * (2 ** min(attempts - 1, 7)))
        # Never derive persisted diagnostics from an exception message: provider
        # errors may contain prompt fragments, credentials, or private context.
        fingerprint = hashlib.sha256(
            f"{type(error).__module__}:{type(error).__qualname__}".encode("utf-8")
        ).hexdigest()
        return replace(
            self,
            attempt_count=attempts,
            next_attempt_at=(
                datetime.now(UTC) + timedelta(seconds=delay_seconds)
            ).isoformat(),
            last_error_type=type(error).__name__,
            last_error_fingerprint=fingerprint,
        )


def load_reflection_jobs(state: dict[str, Any]) -> list[LearningReflectionJob]:
    """Decode the queue strictly; malformed durable work must never become empty."""

    rows = state.get(REFLECTION_QUEUE_STATE_KEY, [])
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise RuntimeError("LearningReflectionQueueCorrupt")
    if len(rows) > MAX_PENDING_REFLECTIONS:
        raise RuntimeError("LearningReflectionQueueExceedsLimit")
    jobs = [LearningReflectionJob.from_dict(row) for row in rows]
    identities = [job.job_id for job in jobs]
    if len(identities) != len(set(identities)):
        raise RuntimeError("LearningReflectionQueueDuplicateIdentity")
    return jobs


def reflection_queue_health(
    jobs: list[LearningReflectionJob],
    *,
    runtime_state: dict[str, Any] | None = None,
    cooldown_minutes: float = 5.0,
) -> dict[str, Any]:
    """Return content-free backlog/retry evidence."""

    runtime = runtime_state if isinstance(runtime_state, dict) else {}
    now = datetime.now(UTC)
    due_count = sum(job.due(now) for job in jobs)
    never_attempted_count = sum(job.attempt_count == 0 for job in jobs)
    oldest_created_at = min((job.created_at for job in jobs), default="")
    oldest_age_seconds = 0.0
    if oldest_created_at:
        oldest = datetime.fromisoformat(oldest_created_at)
        if oldest.tzinfo is None:
            oldest = oldest.replace(tzinfo=UTC)
        oldest_age_seconds = max(0.0, (now - oldest.astimezone(UTC)).total_seconds())
    global_next_attempt_at = str(runtime.get("global_next_attempt_at") or "")
    breaker_open = False
    if global_next_attempt_at:
        try:
            retry_at = datetime.fromisoformat(global_next_attempt_at)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=UTC)
            breaker_open = retry_at.astimezone(UTC) > now
        except ValueError:
            breaker_open = True
    reasons: list[str] = []
    if len(jobs) >= MAX_PENDING_REFLECTIONS:
        reasons.append("queue_at_capacity")
    elif len(jobs) >= int(MAX_PENDING_REFLECTIONS * 0.8):
        reasons.append("queue_near_capacity")
    if oldest_age_seconds >= 3_600:
        reasons.append("oldest_job_stalled")
    if breaker_open:
        reasons.append("provider_circuit_open")
    if int(runtime.get("consecutive_failure_count", 0) or 0) > 0:
        reasons.append("recent_attempt_failed")
    nominal_capacity_per_day = 1_440.0 / max(0.1, float(cooldown_minutes))
    return {
        "status": "degraded" if reasons else "healthy",
        "reasons": reasons,
        "pending_count": len(jobs),
        "capacity": MAX_PENDING_REFLECTIONS,
        "capacity_utilization": round(len(jobs) / MAX_PENDING_REFLECTIONS, 4),
        "due_count": due_count,
        "never_attempted_count": never_attempted_count,
        "oldest_created_at": oldest_created_at,
        "oldest_age_seconds": round(oldest_age_seconds, 3),
        "next_attempt_at": min((job.next_attempt_at for job in jobs), default=""),
        "max_attempt_count": max((job.attempt_count for job in jobs), default=0),
        "nominal_drain_capacity_per_day": round(nominal_capacity_per_day, 3),
        "estimated_backlog_days": round(len(jobs) / nominal_capacity_per_day, 3),
        "circuit_state": "open" if breaker_open else "closed",
        "global_next_attempt_at": global_next_attempt_at,
        "consecutive_failure_count": int(
            runtime.get("consecutive_failure_count", 0) or 0
        ),
        "total_enqueued_count": int(runtime.get("total_enqueued_count", 0) or 0),
        "total_completed_count": int(runtime.get("total_completed_count", 0) or 0),
        "total_failed_attempt_count": int(
            runtime.get("total_failed_attempt_count", 0) or 0
        ),
        "last_attempt_at": str(runtime.get("last_attempt_at") or ""),
        "last_success_at": str(runtime.get("last_success_at") or ""),
        "last_error_type": str(runtime.get("last_error_type") or "")
        or next(
            (job.last_error_type for job in reversed(jobs) if job.last_error_type),
            "",
        ),
        "last_error_fingerprint": str(runtime.get("last_error_fingerprint") or "")
        or next(
            (
                job.last_error_fingerprint
                for job in reversed(jobs)
                if job.last_error_fingerprint
            ),
            "",
        ),
    }


__all__ = [
    "LearningReflectionJob",
    "MAX_PENDING_REFLECTIONS",
    "REFLECTION_RUNTIME_STATE_KEY",
    "REFLECTION_QUEUE_STATE_KEY",
    "ReflectionJobKind",
    "load_reflection_jobs",
    "reflection_queue_health",
]
