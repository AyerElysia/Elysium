"""Selected-backend compatibility projections for the existing learning engines."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from src.kernel.storage import canonical_json

from ..storage.learning_contracts import (
    LearningEventDraft,
    LearningProjection,
    LearningProjectionConflict,
    LearningProjectionWrite,
    LearningStorePort,
)
from .maintenance import (
    LearningMaintenanceEvent,
    LearningMaintenanceJournalPort,
    LearningPhaseOutcome,
)
from .models import Insight, KnowledgeVersion, ValidationExperiment
from .skill_store import SkillCandidate, SkillPattern, SkillStore
from .store import STORE_VERSION, InsightStore

_INSIGHT_PROJECTION = "learning_insights"
_SKILL_PROJECTION = "learning_skills"
_MAINTENANCE_PROJECTION = "learning_maintenance_health"
_STATE_PROJECTOR_VERSION = "learning-state-compat-v1"
_MAINTENANCE_PROJECTOR_VERSION = "learning-maintenance-health-v1"

logger = logging.getLogger("life_engine.learning.persistence")


@dataclass(frozen=True, slots=True)
class LearningPersistenceFailure:
    """Content-free evidence retained for the first failed CAS attempt."""

    occurred_at: str
    writer_instance_id: str
    error_type: str
    projection_name: str
    expected_revision: int | None
    expected_source_frontier: int | None
    actual_revision: int | None
    actual_source_frontier: int | None
    actual_projection_sha256: str
    attempted_projection_sha256: str
    buffered_event_count: int
    dirty_projections: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "occurred_at": self.occurred_at,
            "writer_instance_id": self.writer_instance_id,
            "error_type": self.error_type,
            "projection_name": self.projection_name,
            "expected_revision": self.expected_revision,
            "expected_source_frontier": self.expected_source_frontier,
            "actual_revision": self.actual_revision,
            "actual_source_frontier": self.actual_source_frontier,
            "actual_projection_sha256": self.actual_projection_sha256,
            "attempted_projection_sha256": self.attempted_projection_sha256,
            "buffered_event_count": self.buffered_event_count,
            "dirty_projections": list(self.dirty_projections),
        }

    def log_summary(self) -> str:
        return (
            f"writer={self.writer_instance_id} "
            f"projection={self.projection_name or 'unknown'} "
            f"expected={self.expected_revision}/{self.expected_source_frontier} "
            f"actual={self.actual_revision}/{self.actual_source_frontier} "
            f"actual_sha256={self.actual_projection_sha256 or '-'} "
            f"attempted_sha256={self.attempted_projection_sha256 or '-'} "
            f"events={self.buffered_event_count}"
        )


def _payload_sha256(payload: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def _conflict_failure(
    exc: LearningProjectionConflict,
    *,
    writer_instance_id: str,
    writes: list[LearningProjectionWrite],
    buffered_event_count: int,
    dirty_projections: set[str],
) -> LearningPersistenceFailure:
    attempted = next(
        (write for write in writes if write.projection_name == exc.projection_name),
        None,
    )
    return LearningPersistenceFailure(
        occurred_at=datetime.now(UTC).isoformat(),
        writer_instance_id=writer_instance_id,
        error_type=type(exc).__name__,
        projection_name=exc.projection_name,
        expected_revision=(
            exc.expected_revision
            if exc.expected_revision is not None
            else attempted.expected_revision
            if attempted is not None
            else None
        ),
        expected_source_frontier=(
            exc.expected_source_frontier
            if exc.expected_source_frontier is not None
            else attempted.expected_source_frontier
            if attempted is not None
            else None
        ),
        actual_revision=exc.actual_revision,
        actual_source_frontier=exc.actual_source_frontier,
        actual_projection_sha256=exc.actual_projection_sha256,
        attempted_projection_sha256=(
            _payload_sha256(attempted.payload) if attempted is not None else ""
        ),
        buffered_event_count=buffered_event_count,
        dirty_projections=tuple(sorted(dirty_projections)),
    )


@dataclass(frozen=True, slots=True)
class LearningMutationContext:
    """Trace metadata inherited by synchronous compatibility mutations."""

    source: str = "learning.runtime"
    actor_consciousness_instance_id: str = ""
    subject_revision: str = ""
    provenance: dict[str, object] | None = None


_MUTATION_CONTEXT: ContextVar[LearningMutationContext] = ContextVar(
    "learning_mutation_context",
    default=LearningMutationContext(),
)


class SelectedLearningPersistence:
    """Buffer sync engine mutations and atomically flush event+projection state."""

    def __init__(
        self,
        store: LearningStorePort,
        *,
        writer_instance_id: str = "",
    ) -> None:
        self.store = store
        self.writer_instance_id = (
            str(writer_instance_id).strip() or f"learning_writer_{uuid4().hex}"
        )
        self.insight_store: SelectedInsightStore | None = None
        self.skill_store: SelectedSkillStore | None = None
        self._projections: dict[str, LearningProjection | None] = {
            _INSIGHT_PROJECTION: None,
            _SKILL_PROJECTION: None,
        }
        self._pending_events: list[LearningEventDraft] = []
        self._dirty: set[str] = set()
        self._initialized = False
        self._failed = False
        self._failure: LearningPersistenceFailure | None = None
        self._flush_lock = asyncio.Lock()
        self._last_flush_at = ""

    def bind(
        self,
        insight_store: SelectedInsightStore,
        skill_store: SelectedSkillStore,
    ) -> None:
        if self.insight_store is not None or self.skill_store is not None:
            raise RuntimeError("selected learning persistence is already bound")
        self.insight_store = insight_store
        self.skill_store = skill_store

    @contextmanager
    def mutation_context(
        self,
        context: LearningMutationContext,
    ) -> Iterator[None]:
        token: Token[LearningMutationContext] = _MUTATION_CONTEXT.set(context)
        try:
            yield
        finally:
            _MUTATION_CONTEXT.reset(token)

    async def initialize(self) -> None:
        """Load selected projections before any compatibility read is exposed."""

        if self._initialized:
            return
        if self.insight_store is None or self.skill_store is None:
            raise RuntimeError("selected learning stores are not bound")
        insight_projection, skill_projection = await asyncio.gather(
            self.store.get_projection(_INSIGHT_PROJECTION),
            self.store.get_projection(_SKILL_PROJECTION),
        )
        self._validate_projection(insight_projection, _INSIGHT_PROJECTION)
        self._validate_projection(skill_projection, _SKILL_PROJECTION)
        self.insight_store.hydrate(
            insight_projection.payload if insight_projection is not None else {}
        )
        self.skill_store.hydrate(
            skill_projection.payload if skill_projection is not None else {}
        )
        self._projections[_INSIGHT_PROJECTION] = insight_projection
        self._projections[_SKILL_PROJECTION] = skill_projection
        self._initialized = True

    @staticmethod
    def _validate_projection(
        projection: LearningProjection | None,
        expected_name: str,
    ) -> None:
        if projection is None:
            return
        if projection.projection_name != expected_name:
            raise RuntimeError("selected learning projection identity mismatch")
        if projection.schema_version != 1:
            raise RuntimeError(
                f"unsupported selected learning schema: {projection.schema_version}"
            )
        if projection.projector_version != _STATE_PROJECTOR_VERSION:
            raise RuntimeError(
                "unsupported selected learning projector: "
                f"{projection.projector_version}"
            )
        if projection.rebuild_state != "ready":
            raise RuntimeError(
                f"selected learning projection is {projection.rebuild_state}"
            )

    def _require_writable(self) -> None:
        if not self._initialized:
            raise RuntimeError("selected learning persistence is not initialized")
        if self._failed:
            raise RuntimeError(
                "selected learning persistence failed closed; restart after diagnosis"
            )

    def queue_audit(self, domain: str, event: dict[str, Any]) -> None:
        """Queue exact audit evidence; persistence happens at async boundaries."""

        self._require_writable()
        context = _MUTATION_CONTEXT.get()
        payload = dict(event)
        occurrence_id = str(
            payload.setdefault("audit_event_id", f"learning_audit_{uuid4().hex}")
        )
        occurred_at = str(
            payload.setdefault("timestamp", datetime.now(UTC).isoformat())
        )
        action = str(payload.get("action") or "changed")
        event_kind = f"{domain}.{action}"
        if len(event_kind) > 128:
            event_kind = (
                f"{domain}.oversized_action."
                f"{hashlib.sha256(action.encode('utf-8')).hexdigest()[:16]}"
            )
        self._pending_events.append(
            LearningEventDraft(
                occurrence_id=occurrence_id,
                event_kind=event_kind,
                occurred_at=occurred_at,
                source=context.source,
                actor_consciousness_instance_id=(
                    context.actor_consciousness_instance_id
                ),
                subject_revision=context.subject_revision,
                provenance={
                    "projection": domain,
                    **dict(context.provenance or {}),
                    "writer_instance_id": self.writer_instance_id,
                },
                payload=payload,
            )
        )

    def mark_dirty(self, projection_name: str) -> None:
        self._require_writable()
        if projection_name not in self._projections:
            raise ValueError(f"unknown learning projection: {projection_name}")
        self._dirty.add(projection_name)

    def _snapshot_payload(self, projection_name: str) -> dict[str, Any]:
        if projection_name == _INSIGHT_PROJECTION:
            if self.insight_store is None:
                raise RuntimeError("selected insight store is not bound")
            return self.insight_store.snapshot_payload()
        if self.skill_store is None:
            raise RuntimeError("selected skill store is not bound")
        return self.skill_store.snapshot_payload()

    async def flush(self) -> None:
        """Atomically commit buffered evidence and every dirty projection."""

        async with self._flush_lock:
            self._require_writable()
            if not self._pending_events and not self._dirty:
                return
            # Detach the current buffer before the first await.  A different
            # coroutine may perform a synchronous compatibility mutation while
            # this SQL commit is in flight; that later mutation must stay in the
            # next buffer instead of being erased by this flush.
            pending_events = list(self._pending_events)
            dirty = set(self._dirty)
            self._pending_events.clear()
            self._dirty.clear()
            writes: list[LearningProjectionWrite] = []
            snapshot_events: list[LearningEventDraft] = []
            context = _MUTATION_CONTEXT.get()
            now = datetime.now(UTC).isoformat()
            for projection_name in sorted(dirty):
                projection = self._projections[projection_name]
                payload = self._snapshot_payload(projection_name)
                snapshot_event = LearningEventDraft(
                    occurrence_id=f"learning_snapshot_{uuid4().hex}",
                    event_kind=f"{projection_name}.snapshot",
                    occurred_at=now,
                    source="learning.projector",
                    actor_consciousness_instance_id="",
                    subject_revision=context.subject_revision,
                    provenance={
                        "projection": projection_name,
                        "projector_version": _STATE_PROJECTOR_VERSION,
                        "trigger_source": context.source,
                        **dict(context.provenance or {}),
                        "writer_instance_id": self.writer_instance_id,
                    },
                    payload=payload,
                )
                snapshot_events.append(snapshot_event)
                writes.append(
                    LearningProjectionWrite(
                        projection_name=projection_name,
                        expected_revision=(projection.revision if projection else 0),
                        expected_source_frontier=(
                            projection.source_frontier if projection else 0
                        ),
                        schema_version=1,
                        projector_version=_STATE_PROJECTOR_VERSION,
                        rebuild_state="ready",
                        payload=payload,
                    )
                )
            events = [*pending_events, *snapshot_events]
            try:
                result = await self.store.commit(
                    events=events,
                    projections=writes,
                )
            except LearningProjectionConflict as exc:
                self._pending_events[0:0] = pending_events
                self._dirty.update(dirty)
                self._failed = True
                self._failure = _conflict_failure(
                    exc,
                    writer_instance_id=self.writer_instance_id,
                    writes=writes,
                    buffered_event_count=len(events),
                    dirty_projections=dirty,
                )
                # 双实例共享 MySQL 时学习持久化投影的 CAS 冲突是合法竞争，
                # 事件已放回缓冲保留待处理，属可恢复路径，用 WARNING 而非
                # ERROR 避免后台持久化把日志刷成错误。
                logger.warning(
                    "selected learning persistence CAS 竞争（可恢复），"
                    "事件保留待重试: %s",
                    self._failure.log_summary(),
                )
                raise
            except BaseException:
                self._pending_events[0:0] = pending_events
                self._dirty.update(dirty)
                self._failed = True
                raise
            for projection in result.projections:
                self._projections[projection.projection_name] = projection
            self._last_flush_at = now

    async def close(self) -> None:
        """Flush selected learning state without closing the injected runtime."""

        if self._initialized and not self._failed:
            await self.flush()

    def health_snapshot(self) -> dict[str, Any]:
        """Return cached content-free selected-learning diagnostics."""

        projection_health = {
            name: {
                "revision": projection.revision if projection else 0,
                "source_frontier": projection.source_frontier if projection else 0,
                "rebuild_state": (projection.rebuild_state if projection else "ready"),
                "projection_sha256": (
                    projection.projection_sha256 if projection else ""
                ),
            }
            for name, projection in sorted(self._projections.items())
        }
        return {
            "status": (
                "failed"
                if self._failed
                else "healthy"
                if self._initialized
                else "initializing"
            ),
            "backend": "selected",
            "writer_instance_id": self.writer_instance_id,
            "initialized": self._initialized,
            "pending_events": len(self._pending_events),
            "dirty_projection_count": len(self._dirty),
            "last_flush_at": self._last_flush_at,
            "failure": self._failure.to_dict() if self._failure else None,
            "projections": projection_health,
        }


class SelectedInsightStore(InsightStore):
    """Existing synchronous insight API backed only by selected SQL state."""

    def __init__(
        self,
        workspace: Path | str,
        persistence: SelectedLearningPersistence,
    ) -> None:
        super().__init__(workspace)
        self._persistence = persistence
        self._loaded = True
        self._experiments_loaded = True
        self._knowledge_manifest: dict[str, Any] = {
            "versions": [],
            "current_version": 0,
        }
        self._knowledge_versions_content: dict[str, str] = {}
        self._current_knowledge = ""
        self._state: dict[str, Any] = {}

    def hydrate(self, payload: dict[str, Any]) -> None:
        insights = payload.get("insights", [])
        unreadable = payload.get("unreadable_rows", [])
        experiments = payload.get("experiments", {})
        manifest = payload.get("knowledge_manifest", {})
        versions = payload.get("knowledge_versions_content", {})
        state = payload.get("state", {})
        if not all(
            (
                isinstance(insights, list),
                isinstance(unreadable, list),
                isinstance(experiments, dict),
                isinstance(manifest, dict),
                isinstance(versions, dict),
                isinstance(state, dict),
            )
        ):
            raise TypeError("selected insight projection has invalid structure")
        self._insights = [
            Insight.from_dict(row) for row in insights if isinstance(row, dict)
        ]
        if len(self._insights) != len(insights):
            raise TypeError("selected insight projection contains a non-object row")
        self._unreadable_rows = [
            dict(row) for row in unreadable if isinstance(row, dict)
        ]
        if len(self._unreadable_rows) != len(unreadable):
            raise TypeError("selected unreadable insight rows are malformed")
        pending = experiments.get("pending", [])
        completed = experiments.get("completed", [])
        if not isinstance(pending, list) or not isinstance(completed, list):
            raise TypeError("selected experiment projection is malformed")
        self._experiments = {
            "pending": [
                ValidationExperiment.from_dict(row)
                for row in pending
                if isinstance(row, dict)
            ],
            "completed": [
                ValidationExperiment.from_dict(row)
                for row in completed
                if isinstance(row, dict)
            ],
        }
        if len(self._experiments["pending"]) != len(pending) or len(
            self._experiments["completed"]
        ) != len(completed):
            raise TypeError("selected experiment projection contains a non-object")
        self._knowledge_manifest = dict(manifest) or {
            "versions": [],
            "current_version": 0,
        }
        self._knowledge_versions_content = {
            str(key): str(value) for key, value in versions.items()
        }
        current_version = int(self._knowledge_manifest.get("current_version", 0) or 0)
        self._current_knowledge = self._knowledge_versions_content.get(
            str(current_version), ""
        )
        self._state = dict(state)
        self._load_failed = False

    def snapshot_payload(self) -> dict[str, Any]:
        return {
            "version": STORE_VERSION,
            "insights": [insight.to_dict() for insight in self._insights],
            "unreadable_rows": list(self._unreadable_rows),
            "experiments": {
                "pending": [
                    experiment.to_dict() for experiment in self._experiments["pending"]
                ],
                "completed": [
                    experiment.to_dict()
                    for experiment in self._experiments["completed"]
                ],
            },
            "knowledge_manifest": dict(self._knowledge_manifest),
            "knowledge_versions_content": dict(self._knowledge_versions_content),
            "state": dict(self._state),
        }

    def load(self) -> None:
        return

    def _ensure_dirs(self) -> None:
        return

    def _save(self) -> None:
        self._persistence.mark_dirty(_INSIGHT_PROJECTION)

    def _append_audit(self, event: dict[str, Any]) -> None:
        self._persistence.queue_audit(_INSIGHT_PROJECTION, event)

    def _load_experiments(self) -> None:
        return

    def _save_experiments(self) -> None:
        self._persistence.queue_audit(
            _INSIGHT_PROJECTION,
            {
                "action": "experiments_changed",
                "pending_count": len(self._experiments["pending"]),
                "completed_count": len(self._experiments["completed"]),
            },
        )
        self._persistence.mark_dirty(_INSIGHT_PROJECTION)

    def load_knowledge_manifest(self) -> dict[str, Any]:
        return dict(self._knowledge_manifest)

    def save_knowledge_manifest(self, manifest: dict[str, Any]) -> None:
        self._knowledge_manifest = dict(manifest)
        self._persistence.mark_dirty(_INSIGHT_PROJECTION)

    def get_current_knowledge_path(self) -> Path:
        raise RuntimeError("selected learning knowledge has no writable workspace path")

    def read_current_knowledge(self) -> str:
        return self._current_knowledge

    def read_knowledge_version(self, version: int) -> str:
        identity = str(int(version))
        if int(identity) <= 0:
            raise ValueError("knowledge version must be positive")
        try:
            return self._knowledge_versions_content[identity]
        except KeyError as exc:
            raise FileNotFoundError(
                f"selected://learning/knowledge/v{identity}"
            ) from exc

    def write_knowledge_version(
        self,
        content: str,
        version: int,
        insight_ids: list[str],
        edit_count: int,
        promoted: bool,
        reason: str = "",
    ) -> KnowledgeVersion:
        if str(version) in self._knowledge_versions_content:
            raise ValueError(f"KnowledgeVersionConflict:{version}")
        knowledge_version = KnowledgeVersion(
            version=int(version),
            timestamp=datetime.now(UTC).astimezone().isoformat(),
            file_path=f"selected://learning/knowledge/v{version}",
            insight_ids=list(insight_ids),
            edit_count=int(edit_count),
            promoted=bool(promoted),
            selection_reason=str(reason),
        )
        self._knowledge_versions_content[str(version)] = str(content)
        versions = self._knowledge_manifest.get("versions", [])
        if not isinstance(versions, list):
            raise TypeError("selected knowledge manifest versions must be a list")
        versions = [*versions, knowledge_version.to_dict()]
        self._knowledge_manifest["versions"] = versions
        if promoted:
            self._knowledge_manifest["current_version"] = int(version)
            self._current_knowledge = str(content)
        self._append_audit(
            {
                "action": "knowledge_version_written",
                "version": int(version),
                "promoted": bool(promoted),
                "insight_count": len(insight_ids),
                "edit_count": int(edit_count),
            }
        )
        self._persistence.mark_dirty(_INSIGHT_PROJECTION)
        return knowledge_version

    def load_state(self) -> dict[str, Any]:
        return dict(self._state)

    def save_state(self, state: dict[str, Any]) -> None:
        self._state = dict(state)
        self._persistence.mark_dirty(_INSIGHT_PROJECTION)


class SelectedSkillStore(SkillStore):
    """Existing synchronous skill API backed only by selected SQL state."""

    def __init__(
        self,
        workspace: Path | str,
        persistence: SelectedLearningPersistence,
    ) -> None:
        super().__init__(workspace)
        self._persistence = persistence
        self._loaded = True

    def hydrate(self, payload: dict[str, Any]) -> None:
        skills = payload.get("skills", [])
        candidates = payload.get("candidates", [])
        unreadable = payload.get("unreadable_rows", [])
        unreadable_candidates = payload.get("unreadable_candidate_rows", [])
        if not all(
            isinstance(value, list)
            for value in (skills, candidates, unreadable, unreadable_candidates)
        ):
            raise TypeError("selected skill projection has invalid structure")
        self._skills = [
            SkillPattern.from_dict(row) for row in skills if isinstance(row, dict)
        ]
        if len(self._skills) != len(skills):
            raise TypeError("selected skill projection contains a non-object row")
        self._unreadable_rows = [
            dict(row) for row in unreadable if isinstance(row, dict)
        ]
        if len(self._unreadable_rows) != len(unreadable):
            raise TypeError("selected unreadable skill rows are malformed")
        self._candidates = [
            SkillCandidate.from_dict(row) for row in candidates if isinstance(row, dict)
        ]
        if len(self._candidates) != len(candidates):
            raise TypeError("selected skill candidates contain a non-object row")
        self._unreadable_candidate_rows = [
            dict(row) for row in unreadable_candidates if isinstance(row, dict)
        ]
        if len(self._unreadable_candidate_rows) != len(unreadable_candidates):
            raise TypeError("selected unreadable skill candidates are malformed")
        self._load_failed = False

    def snapshot_payload(self) -> dict[str, Any]:
        return {
            "version": 1,
            "skills": [skill.to_dict() for skill in self._skills],
            "candidates": [candidate.to_dict() for candidate in self._candidates],
            "unreadable_rows": list(self._unreadable_rows),
            "unreadable_candidate_rows": list(self._unreadable_candidate_rows),
        }

    def load(self) -> None:
        return

    def _ensure_dirs(self) -> None:
        return

    def _save(self) -> None:
        self._persistence.mark_dirty(_SKILL_PROJECTION)

    def _append_audit(self, event: dict[str, Any]) -> None:
        self._persistence.queue_audit(_SKILL_PROJECTION, event)


class SelectedLearningMaintenanceJournal(LearningMaintenanceJournalPort):
    """Maintenance evidence and health projection on the selected backend."""

    def __init__(
        self,
        store: LearningStorePort,
        *,
        writer_instance_id: str = "",
    ) -> None:
        self._store = store
        self.writer_instance_id = (
            str(writer_instance_id).strip() or f"learning_writer_{uuid4().hex}"
        )
        self._projection: LearningProjection | None = None
        self._latest: dict[str, LearningMaintenanceEvent] = {}
        self._observed_events = 0
        self._initialized = False
        self._failed = False
        self._failure: LearningPersistenceFailure | None = None
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        async with self._lock:
            if self._initialized:
                return
            projection = await self._store.get_projection(_MAINTENANCE_PROJECTION)
            if projection is not None:
                if (
                    projection.schema_version != 1
                    or projection.projector_version != _MAINTENANCE_PROJECTOR_VERSION
                    or projection.rebuild_state != "ready"
                ):
                    raise RuntimeError(
                        "selected maintenance projection contract mismatch"
                    )
                latest = projection.payload.get("latest_by_phase", {})
                if not isinstance(latest, dict):
                    raise TypeError("maintenance health projection is malformed")
                self._latest = {
                    str(phase): LearningMaintenanceEvent.from_dict(dict(event))
                    for phase, event in latest.items()
                    if isinstance(event, dict)
                }
                if len(self._latest) != len(latest):
                    raise TypeError("maintenance health contains a non-object event")
                self._observed_events = max(
                    0,
                    int(projection.payload.get("observed_events", 0) or 0),
                )
            self._projection = projection
            self._initialized = True

    async def append(self, event: LearningMaintenanceEvent) -> None:
        await self.initialize()
        async with self._lock:
            if self._failed:
                raise RuntimeError(
                    "selected maintenance journal failed closed; "
                    "restart after diagnosis"
                )
            previous = self._projection
            latest = dict(self._latest)
            latest[event.phase] = event
            payload = {
                "observed_events": self._observed_events + 1,
                "latest_by_phase": {
                    phase: item.to_dict() for phase, item in sorted(latest.items())
                },
            }
            draft = LearningEventDraft(
                occurrence_id=event.event_id,
                event_kind=(f"maintenance.{event.phase}.{event.outcome}"),
                occurred_at=event.started_at,
                source="learning.scheduler",
                actor_consciousness_instance_id="",
                subject_revision="",
                provenance={
                    "run_id": event.run_id,
                    "schema_version": event.schema_version,
                    "writer_instance_id": self.writer_instance_id,
                },
                payload=event.to_dict(),
            )
            write = LearningProjectionWrite(
                projection_name=_MAINTENANCE_PROJECTION,
                expected_revision=previous.revision if previous else 0,
                expected_source_frontier=(previous.source_frontier if previous else 0),
                schema_version=1,
                projector_version=_MAINTENANCE_PROJECTOR_VERSION,
                rebuild_state="ready",
                payload=payload,
            )
            try:
                result = await self._store.commit(
                    events=[draft],
                    projections=[write],
                )
            except LearningProjectionConflict as exc:
                self._failed = True
                self._failure = _conflict_failure(
                    exc,
                    writer_instance_id=self.writer_instance_id,
                    writes=[write],
                    buffered_event_count=1,
                    dirty_projections={_MAINTENANCE_PROJECTION},
                )
                # 双实例共享 MySQL 时学习维护投影的 CAS 冲突是合法竞争
                # （两实例各自推进同一 projection revision），属可恢复路径，
                # 用 WARNING 而非 ERROR 避免后台维护把日志刷成错误。
                logger.warning(
                    "selected learning maintenance CAS 竞争（可恢复），"
                    "保留待处理工作: %s",
                    self._failure.log_summary(),
                )
                raise
            self._projection = result.projections[0]
            self._latest = latest
            self._observed_events += 1

    def health_snapshot(self) -> dict[str, Any]:
        incomplete_or_failed = sum(
            event.outcome != LearningPhaseOutcome.SUCCEEDED.value
            for event in self._latest.values()
        )
        return {
            "status": (
                "failed"
                if self._failed
                else "degraded"
                if incomplete_or_failed
                else "healthy"
            ),
            "journal": "selected_sql",
            "initialized": self._initialized,
            "observed_events": self._observed_events,
            "projection": {
                "revision": self._projection.revision if self._projection else 0,
                "source_frontier": (
                    self._projection.source_frontier if self._projection else 0
                ),
                "projection_sha256": (
                    self._projection.projection_sha256 if self._projection else ""
                ),
            },
            "failure": self._failure.to_dict() if self._failure else None,
            "latest_by_phase": {
                phase: event.to_dict() for phase, event in sorted(self._latest.items())
            },
        }


__all__ = [
    "LearningMutationContext",
    "LearningPersistenceFailure",
    "SelectedInsightStore",
    "SelectedLearningMaintenanceJournal",
    "SelectedLearningPersistence",
    "SelectedSkillStore",
]
