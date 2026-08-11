"""LearningScheduler：三环调度协调器。

类比 VibeGamer 的 orchestrator.ts。
协调快环（反思）、审计环（验证）、慢环（压缩）的执行时机。

调度优先级：
1. 审计环：有待审洞察且到了审计间隔 → 执行审计
2. 慢环：validated 积累足够 → 执行压缩
3. 快环：由事件驱动（交互结束/梦境结束），不主动调度
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from collections.abc import Awaitable, Callable
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from ..memory.continuity_index import (
    CONTINUITY_MEMORY_REVIEW_GROWTH_BYTES,
    CONTINUITY_MEMORY_REVIEW_PRESSURE_BYTES,
    CONTINUITY_MEMORY_SOFT_TARGET_BYTES,
    diagnose_continuity_memory_index,
)
from ..storage.learning_contracts import (
    LearningEventDraft,
    LearningOccurrenceConflict,
    LearningStorePort,
)
from ..storage.subject_contracts import (
    SUBJECT_AUTHORITY_PATHS,
    SubjectAuthorityCommit,
    SubjectDocumentPath,
)
from .auditor import InsightAuditor
from .decisions import LearningCandidate, LearningDecisionLedger, SubjectAuthorityPort
from .knowledge import SelfKnowledgeCompressor
from .maintenance import (
    LearningMaintenanceEvent,
    LearningMaintenanceJournalPort,
    LearningPhase,
    LocalLearningMaintenanceJournal,
)
from .metrics import LearningMetrics
from .models import AuditVerdict, InsightNextAction
from .reflection import ReflectionEngine
from .reflection_queue import (
    MAX_PENDING_REFLECTIONS,
    REFLECTION_QUEUE_STATE_KEY,
    REFLECTION_RUNTIME_STATE_KEY,
    LearningReflectionJob,
    ReflectionJobKind,
    load_reflection_jobs,
    reflection_queue_health,
)
from .selectable import (
    LearningMutationContext,
    SelectedInsightStore,
    SelectedLearningMaintenanceJournal,
    SelectedLearningPersistence,
    SelectedSkillStore,
)
from .skill_distiller import SkillDistiller
from .skill_store import SkillDecisionKind, SkillStore
from .store import InsightStore
from .timeouts import DEFAULT_LEARNING_LLM_TIMEOUT_SECONDS

logger = logging.getLogger("life_engine.learning.scheduler")

# 默认调度参数
_DEFAULT_AUDIT_INTERVAL_HOURS = 6.0
_DEFAULT_AUDIT_BATCH_SIZE = 3
_DEFAULT_COMPRESS_TRIGGER_COUNT = 5
_DEFAULT_COMPRESS_INTERVAL_HOURS = 48.0
# 30 分钟冷却算不过来账：一次反思一次机会，一天上限 48 次，而实测到达率是
# 107~164 段经历/天。差额不是"稍微慢一点"，是每天一百多段经历排进队列再也出不来，
# 队列 5 天从 16 涨到 310，撞满 512 上限之后新的经历会被直接拒收。5 分钟对应
# 288 次/天，既盖住到达率也留出排空积压的余量。
_DEFAULT_REFLECTION_COOLDOWN_MINUTES = 5.0
_DEFAULT_METRICS_INTERVAL_HOURS = 12.0
_DEFAULT_SKILL_DISTILL_TRIGGER_COUNT = 3
_DEFAULT_SKILL_DISTILL_INTERVAL_HOURS = 24.0
_DEFAULT_STALENESS_CHECK_INTERVAL_HOURS = 168.0  # 每周检查一次
_DEFAULT_STALENESS_THRESHOLD_DAYS = 90
_DEFAULT_SUBJECT_REVIEW_INTERVAL_HOURS: dict[SubjectDocumentPath, float] = {
    "SOUL.md": 30.0 * 24.0,
    "USER.md": 30.0 * 24.0,
    "MEMORY.md": 7.0 * 24.0,
}
_DEFAULT_SUBJECT_REVIEW_OFFER_COOLDOWN_HOURS = 24.0
_SUBJECT_REVIEW_STATE_KEY = "subject_review_v1"
_SUBJECT_REVIEW_SNOOZED_EVENT_KIND = "subject_review.snoozed"
_DEFAULT_MAINTENANCE_POLL_SECONDS = 15.0
_REFLECTION_FAILURE_BACKOFF_BASE_SECONDS = 30.0
_REFLECTION_FAILURE_BACKOFF_MAX_SECONDS = 15.0 * 60.0
_REFLECTION_EVENT_CURSOR_STATE_KEY = "reflection_event_cursor_v1"
_REFLECTION_ENQUEUED_EVENT_KIND = "reflection.enqueued"
_REFLECTION_EVENT_PAGE_SIZE = 500


@dataclass(frozen=True, slots=True)
class SubjectDocumentReadSnapshot:
    """One coherent, exact subject-document read with immutable provenance."""

    content_bytes: bytes
    version_id: str
    source_occurrence_id: str
    unified_subject_revision: str
    content_sha256: str
    byte_length: int
    provenance_status: str


class LearningScheduler:
    """三环自学习调度协调器。

    集成入口：由 life_engine 心跳或事件触发调用。
    """

    projector_owner = True

    def __init__(
        self,
        *,
        workspace_path: str | Path,
        model_task_name: str = "life",
        llm_timeout_seconds: float = DEFAULT_LEARNING_LLM_TIMEOUT_SECONDS,
        # 审计参数
        audit_interval_hours: float = _DEFAULT_AUDIT_INTERVAL_HOURS,
        audit_batch_size: int = _DEFAULT_AUDIT_BATCH_SIZE,
        # 压缩参数
        compress_trigger_count: int = _DEFAULT_COMPRESS_TRIGGER_COUNT,
        compress_interval_hours: float = _DEFAULT_COMPRESS_INTERVAL_HOURS,
        # 反思参数
        reflection_cooldown_minutes: float = _DEFAULT_REFLECTION_COOLDOWN_MINUTES,
        # 指标参数
        metrics_interval_hours: float = _DEFAULT_METRICS_INTERVAL_HOURS,
        # 技能蒸馏参数
        skill_distill_trigger_count: int = _DEFAULT_SKILL_DISTILL_TRIGGER_COUNT,
        skill_distill_interval_hours: float = _DEFAULT_SKILL_DISTILL_INTERVAL_HOURS,
        # 陈旧检查参数
        staleness_check_interval_hours: float = _DEFAULT_STALENESS_CHECK_INTERVAL_HOURS,
        staleness_threshold_days: int = _DEFAULT_STALENESS_THRESHOLD_DAYS,
        subject_review_enabled: bool = True,
        subject_review_soul_interval_hours: float = (
            _DEFAULT_SUBJECT_REVIEW_INTERVAL_HOURS["SOUL.md"]
        ),
        subject_review_user_interval_hours: float = (
            _DEFAULT_SUBJECT_REVIEW_INTERVAL_HOURS["USER.md"]
        ),
        subject_review_memory_interval_hours: float = (
            _DEFAULT_SUBJECT_REVIEW_INTERVAL_HOURS["MEMORY.md"]
        ),
        subject_review_offer_cooldown_hours: float = (
            _DEFAULT_SUBJECT_REVIEW_OFFER_COOLDOWN_HOURS
        ),
        # 记忆服务（用于把"修正型洞察"落成显式修正记录，形成记忆演化链）
        memory_service: Any | None = None,
        maintenance_journal: LearningMaintenanceJournalPort | None = None,
        learning_store: LearningStorePort | None = None,
        learning_event_store: LearningStorePort | None = None,
        subject_authority: SubjectAuthorityPort | None = None,
        project_subject_commit: (
            Callable[[SubjectDocumentPath, SubjectAuthorityCommit], Awaitable[None]]
            | None
        ) = None,
        current_subject_revision: Callable[[], Awaitable[str]] | None = None,
        read_subject_authority: Callable[[], Awaitable[Any]] | None = None,
        validate_active_consciousness_instance: (
            Callable[[str], Awaitable[bool]] | None
        ) = None,
        writer_instance_id: str = "",
    ) -> None:
        self._workspace = Path(workspace_path).resolve()
        self._model_task_name = model_task_name
        self._llm_timeout_seconds = max(
            30.0,
            float(llm_timeout_seconds or DEFAULT_LEARNING_LLM_TIMEOUT_SECONDS),
        )
        self._memory_service = memory_service
        self._current_subject_revision = current_subject_revision or (
            subject_authority.current_subject_revision
            if subject_authority is not None
            else None
        )
        subject_reader = (
            getattr(subject_authority, "read_subject_authority", None)
            if subject_authority is not None
            else None
        )
        self._read_subject_authority = read_subject_authority or (
            subject_reader if callable(subject_reader) else None
        )
        self._validate_active_consciousness_instance = (
            validate_active_consciousness_instance
        )

        # 初始化核心组件
        self._selected_persistence: SelectedLearningPersistence | None = None
        self._learning_event_store = learning_event_store
        self.decision_ledger: LearningDecisionLedger | None = None
        self._writer_instance_id = (
            str(writer_instance_id).strip() or f"learning_writer_{uuid4().hex}"
        )
        if learning_store is None:
            self.store = InsightStore(self._workspace)
            self.skill_store = SkillStore(self._workspace)
        else:
            persistence = SelectedLearningPersistence(
                learning_store,
                writer_instance_id=self._writer_instance_id,
            )
            self.store = SelectedInsightStore(self._workspace, persistence)
            self.skill_store = SelectedSkillStore(self._workspace, persistence)
            persistence.bind(self.store, self.skill_store)
            self._selected_persistence = persistence
            self.decision_ledger = LearningDecisionLedger(
                learning_store,
                subject_authority=subject_authority,
                project_subject_commit=project_subject_commit,
            )
        self.reflection = ReflectionEngine(
            store=self.store,
            workspace_path=self._workspace,
            model_task_name=model_task_name,
            timeout_seconds=self._llm_timeout_seconds,
            cooldown_seconds=reflection_cooldown_minutes * 60,
            skill_store=self.skill_store,
            memory_service=memory_service,
        )
        self.auditor = InsightAuditor(
            store=self.store,
            workspace_path=self._workspace,
            model_task_name=model_task_name,
            timeout_seconds=self._llm_timeout_seconds,
            batch_size=audit_batch_size,
        )
        self.compressor = SelfKnowledgeCompressor(
            store=self.store,
            workspace_path=self._workspace,
            model_task_name=model_task_name,
            timeout_seconds=self._llm_timeout_seconds,
            trigger_count=compress_trigger_count,
            interval_hours=compress_interval_hours,
        )
        self.distiller = SkillDistiller(
            store=self.store,
            skill_store=self.skill_store,
            workspace_path=self._workspace,
            model_task_name=model_task_name,
            timeout_seconds=self._llm_timeout_seconds,
            trigger_count=skill_distill_trigger_count,
            interval_hours=skill_distill_interval_hours,
            current_subject_revision=self._current_subject_revision,
        )
        self.metrics = LearningMetrics(store=self.store)
        if maintenance_journal is not None:
            self.maintenance_journal = maintenance_journal
        elif learning_store is not None:
            self.maintenance_journal = SelectedLearningMaintenanceJournal(
                learning_store,
                writer_instance_id=self._writer_instance_id,
            )
        else:
            self.maintenance_journal = LocalLearningMaintenanceJournal(self._workspace)

        # 回填 knowledge_versions：v1 写在 record_knowledge_version 之前，
        # 那两条洞察不知道自己已经在知识文档里。她要是现在重新审视其中一条，
        # 修正会找不到对应表述。只搬 manifest 里的既有事实，不做判断。
        # 包起来：补账失败也不能让她起不来。
        if self._selected_persistence is None:
            try:
                self.store.reconcile_knowledge_versions()
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "回填 knowledge_versions 失败（不影响启动）: %s",
                    type(exc).__name__,
                )

        # 调度参数
        self._audit_interval_hours = max(1.0, audit_interval_hours)
        self._metrics_interval_hours = max(1.0, metrics_interval_hours)
        self._staleness_check_interval_hours = max(24.0, staleness_check_interval_hours)
        self._staleness_threshold_days = max(30, staleness_threshold_days)
        self._subject_review_enabled = bool(subject_review_enabled)
        self._subject_review_intervals: dict[SubjectDocumentPath, float] = {
            "SOUL.md": max(24.0, float(subject_review_soul_interval_hours)),
            "USER.md": max(24.0, float(subject_review_user_interval_hours)),
            "MEMORY.md": max(24.0, float(subject_review_memory_interval_hours)),
        }
        self._subject_review_offer_cooldown_hours = max(
            1.0,
            float(subject_review_offer_cooldown_hours),
        )
        self._subject_review_journal_path = (
            self._workspace / ".life_learning" / "subject_reviews.jsonl"
        )

        self._running = False
        self._last_audit_at: str = ""
        self._last_metrics_at: str = ""
        self._last_staleness_check_at: str = ""
        self._epistemic_backfilled = False
        self._maintenance_lock = asyncio.Lock()
        # 队列锁只护住"读改写 pending_reflections"这几步，不再跨 LLM 调用。
        # 反思本身由 _reflection_runner_lock 串行化：提交路径发现它被占用就把
        # 请求留在队列里，不排队等一次 180s 的调用。
        self._reflection_queue_lock = asyncio.Lock()
        self._reflection_runner_lock = asyncio.Lock()
        self._subject_review_lock = asyncio.Lock()
        self._pending_subject_review_offer: dict[str, Any] | None = None
        self._reflection_cooldown_minutes = max(
            0.1,
            float(reflection_cooldown_minutes),
        )
        self._maintenance_wakeup = asyncio.Event()
        self._worker_running = False
        self._worker_last_started_at = ""
        self._worker_last_completed_at = ""
        self._worker_last_error_type = ""
        self._projector_quiesced = False
        self._projector_quiesced_at = ""
        self._projector_quiesce_reason = ""
        self._projector_quiesce_error_type = ""

    async def initialize(self) -> None:
        """Restore bounded maintenance health without blocking the event loop."""

        if self._selected_persistence is not None:
            await self._selected_persistence.initialize()
            self.store.reconcile_knowledge_versions()
            await self._selected_persistence.flush()
        await self.maintenance_journal.initialize()

    async def flush(self) -> None:
        """Durably flush selected learning mutations at an async boundary."""

        if self._selected_persistence is not None:
            await self._selected_persistence.flush()

    async def close(self) -> None:
        """Flush learning consumers without closing the injected runtime."""

        self._maintenance_wakeup.set()
        if self._selected_persistence is not None and not self._projector_quiesced:
            await self._selected_persistence.close()

    def quiesce_projector(self, *, reason: str, error_type: str) -> None:
        """Stop derived writes after the exact singleton owner is lost.

        Immutable enqueue events use a separate unclaimed store and remain
        appendable. This transition never flushes, rebases, reacquires, or
        releases the lost claim; the service and storage runtime own fencing.
        """

        self._projector_quiesced = True
        self._projector_quiesced_at = _now_iso()
        self._projector_quiesce_reason = str(reason).strip()
        self._projector_quiesce_error_type = str(error_type).strip()
        self._pending_subject_review_offer = None
        self._maintenance_wakeup.set()

    def request_maintenance(self) -> None:
        """Wake the independent learning worker without awaiting LLM work."""

        self._maintenance_wakeup.set()

    async def run(
        self,
        stop_event: asyncio.Event,
        *,
        poll_interval_seconds: float = _DEFAULT_MAINTENANCE_POLL_SECONDS,
    ) -> None:
        """Run derived learning outside the subject's foreground heartbeat.

        Event handlers only append immutable work and wake this loop. A slow or
        unavailable learning model can therefore degrade learning health without
        consuming the main heartbeat's response budget or delaying expression.
        """

        poll_seconds = max(1.0, float(poll_interval_seconds))
        self._worker_running = True
        self.request_maintenance()
        try:
            while not stop_event.is_set():
                if self._projector_quiesced:
                    return
                self._maintenance_wakeup.clear()
                self._worker_last_started_at = _now_iso()
                try:
                    await self.on_heartbeat()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - keep the worker alive
                    self._worker_last_error_type = type(exc).__name__
                    logger.warning(
                        "learning maintenance worker cycle failed: %s",
                        type(exc).__name__,
                    )
                else:
                    self._worker_last_completed_at = _now_iso()
                    self._worker_last_error_type = ""
                if stop_event.is_set():
                    break
                try:
                    await asyncio.wait_for(
                        self._maintenance_wakeup.wait(),
                        timeout=poll_seconds,
                    )
                except TimeoutError:
                    pass
        finally:
            self._worker_running = False

    async def decide_skill_candidate(
        self,
        *,
        candidate_id: str,
        candidate_revision: int,
        candidate_sha256: str,
        decision_occurrence_id: str,
        decision_kind: SkillDecisionKind,
        actor_consciousness_instance_id: str,
        expected_subject_revision: str,
        reason: str,
        accepted_name: str = "",
        accepted_description: str = "",
        accepted_instructions: str = "",
        occurred_at: str = "",
    ) -> dict[str, Any]:
        """Commit one active-instance decision over an exact skill candidate."""

        actor = str(actor_consciousness_instance_id or "").strip()
        if self._validate_active_consciousness_instance is None:
            raise RuntimeError("LearningPresenceGateUnavailable")
        if not await self._validate_active_consciousness_instance(actor):
            raise PermissionError("LearningDecisionActorIsNotActive")
        if self._current_subject_revision is None:
            raise RuntimeError("LearningSubjectRevisionUnavailable")
        current_revision = str(await self._current_subject_revision()).strip().lower()
        expected_revision = str(expected_subject_revision).strip().lower()
        if current_revision != expected_revision:
            raise RuntimeError("LearningDecisionSubjectRevisionConflict")

        context = (
            self._selected_persistence.mutation_context(
                LearningMutationContext(
                    source="learning.subject_decision",
                    actor_consciousness_instance_id=actor,
                    subject_revision=current_revision,
                    provenance={
                        "candidate_id": candidate_id,
                        "decision_occurrence_id": decision_occurrence_id,
                    },
                )
            )
            if self._selected_persistence is not None
            else nullcontext()
        )
        with context:
            insight_ids = self.skill_store.record_candidate_decision(
                candidate_id=candidate_id,
                candidate_revision=candidate_revision,
                candidate_sha256=candidate_sha256,
                decision_occurrence_id=decision_occurrence_id,
                decision_kind=decision_kind,
                actor_consciousness_instance_id=actor,
                expected_subject_revision=expected_revision,
                reason=reason,
                accepted_name=accepted_name,
                accepted_description=accepted_description,
                accepted_instructions=accepted_instructions,
                occurred_at=occurred_at,
            )
            if decision_kind == "accepted":
                for insight_id in insight_ids:
                    insight = self.store.get_insight(insight_id)
                    if insight is None:
                        raise RuntimeError(
                            f"SkillCandidateInsightMissing: {insight_id}"
                        )
                    insight.next_action = InsightNextAction.ARCHIVE.value
                    self.store.update_insight(insight)
            await self.flush()
        candidate = self.skill_store.get_candidate(candidate_id)
        if candidate is None:
            raise RuntimeError("SkillCandidateProjectionMissing")
        return {
            "candidate_id": candidate.candidate_id,
            "candidate_revision": candidate.candidate_revision,
            "candidate_sha256": candidate.candidate_sha256,
            "status": candidate.status,
            "decision_occurrence_id": decision_occurrence_id,
            "actor_consciousness_instance_id": actor,
            "subject_revision": current_revision,
        }

    # ── 记忆服务晚绑定 ───────────────────────────────────────

    def attach_memory_service(self, memory_service: Any) -> None:
        """晚绑定记忆服务。

        LearningScheduler 可能在 memory_service 就绪之前构造。
        提供这个入口，避免反思环因为拿不到记忆服务而静默退化成
        "只写洞察、不写修正" 的半截状态。
        """
        if memory_service is None:
            return
        self._memory_service = memory_service
        self.reflection.attach_memory_service(memory_service)
        logger.debug("LearningScheduler 已绑定 memory_service")

    def _reflection_jobs(self) -> list[LearningReflectionJob]:
        return load_reflection_jobs(self.store.load_state())

    @staticmethod
    def _reflection_runtime_state(state: dict[str, Any]) -> dict[str, Any]:
        raw = state.get(REFLECTION_RUNTIME_STATE_KEY)
        runtime = dict(raw) if isinstance(raw, dict) else {}
        runtime.setdefault("schema_version", 1)
        return runtime

    def _save_reflection_jobs(
        self,
        jobs: list[LearningReflectionJob],
        *,
        state: dict[str, Any] | None = None,
        runtime: dict[str, Any] | None = None,
    ) -> None:
        state = self.store.load_state() if state is None else state
        state[REFLECTION_QUEUE_STATE_KEY] = [job.to_dict() for job in jobs]
        if runtime is not None:
            state[REFLECTION_RUNTIME_STATE_KEY] = runtime
        self.store.save_state(state)

    async def enqueue_reflection(
        self,
        *,
        reflection_kind: ReflectionJobKind,
        reflection_text: str,
        context: str = "",
        source_event_ids: list[str] | None = None,
        actor_consciousness_instance_id: str = "",
    ) -> str:
        """Durably enqueue one experience and return without running its LLM."""

        job = LearningReflectionJob.create(
            reflection_kind=reflection_kind,
            reflection_text=reflection_text,
            context=context,
            source_event_ids=source_event_ids,
            actor_consciousness_instance_id=actor_consciousness_instance_id,
        )
        if self._learning_event_store is not None:
            draft = LearningEventDraft(
                occurrence_id=job.job_id,
                event_kind=_REFLECTION_ENQUEUED_EVENT_KIND,
                occurred_at=job.created_at,
                source=f"learning.{job.reflection_kind}",
                actor_consciousness_instance_id=(job.actor_consciousness_instance_id),
                subject_revision="",
                provenance={
                    "schema_version": 1,
                    "queue": REFLECTION_QUEUE_STATE_KEY,
                    "writer_instance_id": self._writer_instance_id,
                },
                payload=job.to_dict(),
            )
            await self._learning_event_store.commit(
                events=[draft],
                projections=[],
            )
            self.request_maintenance()
            return job.job_id
        async with self._reflection_queue_lock:
            state = self.store.load_state()
            jobs = load_reflection_jobs(state)
            if len(jobs) >= MAX_PENDING_REFLECTIONS:
                logger.error(
                    "反思队列已满，本次经历无法入队：pending=%d cap=%d",
                    len(jobs),
                    MAX_PENDING_REFLECTIONS,
                )
                raise RuntimeError("LearningReflectionQueueFull")
            runtime = self._reflection_runtime_state(state)
            runtime["total_enqueued_count"] = (
                int(runtime.get("total_enqueued_count", 0) or 0) + 1
            )
            runtime["last_enqueued_at"] = _now_iso()
            jobs.append(job)
            self._save_reflection_jobs(jobs, state=state, runtime=runtime)
            await self.flush()
        self.request_maintenance()
        return job.job_id

    async def submit_reflection(
        self,
        *,
        reflection_kind: ReflectionJobKind,
        reflection_text: str,
        context: str = "",
        source_event_ids: list[str] | None = None,
        actor_consciousness_instance_id: str = "",
    ) -> list[Any] | None:
        """Persist a reflection request before attempting its LLM work.

        ``None`` means the request remains queued: the engine is cooling down,
        or another reflection is already in flight. An empty list means the
        request ran successfully and produced no new insight. Failures raise
        only after the retry envelope is durable.
        """

        job_id = await self.enqueue_reflection(
            reflection_kind=reflection_kind,
            reflection_text=reflection_text,
            context=context,
            source_event_ids=source_event_ids,
            actor_consciousness_instance_id=actor_consciousness_instance_id,
        )
        if self._projector_quiesced:
            return None
        if self._learning_event_store is not None:
            await self._ingest_reflection_events()
        result = await self._run_pending_reflection()
        if result is None:
            return None
        if result[0] != job_id:
            return None
        _, insights = result
        return insights

    def _reflection_work_due(self) -> bool:
        state = self.store.load_state()
        runtime = self._reflection_runtime_state(state)
        retry_at = str(runtime.get("global_next_attempt_at") or "")
        if retry_at:
            try:
                retry_time = datetime.fromisoformat(retry_at)
            except ValueError:
                return False
            if retry_time.tzinfo is None:
                retry_time = retry_time.replace(tzinfo=UTC)
            if retry_time.astimezone(UTC) > datetime.now(UTC):
                return False
        return self.reflection.can_reflect and any(
            job.due() for job in load_reflection_jobs(state)
        )

    def _reflection_pending_count(self) -> int:
        return len(self._reflection_jobs())

    async def _run_pending_reflection_phase(self) -> None:
        await self._run_pending_reflection()

    async def _ingest_reflection_events(self) -> int:
        """Project immutable cross-node enqueue facts into the owner queue."""

        if self._learning_event_store is None:
            return 0
        source_health = await self._learning_event_store.health_snapshot()
        source_frontier = max(
            0,
            int(
                source_health.get(
                    "event_frontier",
                    source_health.get("latest_position", 0),
                )
                or 0
            ),
        )
        ingested = 0
        async with self._reflection_queue_lock:
            state = self.store.load_state()
            jobs = load_reflection_jobs(state)
            known = {job.job_id for job in jobs}
            runtime = self._reflection_runtime_state(state)
            cursor = max(
                0,
                int(state.get(_REFLECTION_EVENT_CURSOR_STATE_KEY, 0) or 0),
            )
            source_frontier = max(source_frontier, cursor)
            original_cursor = cursor
            while True:
                page = await self._learning_event_store.read_events(
                    cursor,
                    limit=_REFLECTION_EVENT_PAGE_SIZE,
                    event_kinds=(_REFLECTION_ENQUEUED_EVENT_KIND,),
                )
                # ``source_frontier`` is captured before paging.  A concurrent
                # append may therefore appear in ``page`` but must remain for
                # the next pass instead of leaking across this delivery
                # window.  Non-reflection learning events are intentionally
                # excluded at the store so legacy projections without a cursor
                # never materialize large snapshot payloads just to skip them.
                window = [
                    record for record in page if record.position <= source_frontier
                ]
                stopped_at_capacity = False
                for record in window:
                    job = LearningReflectionJob.from_dict(record.payload)
                    if job.job_id != record.occurrence_id:
                        raise RuntimeError(
                            "LearningReflectionOccurrenceIdentityMismatch"
                        )
                    if job.job_id not in known:
                        if len(jobs) >= MAX_PENDING_REFLECTIONS:
                            stopped_at_capacity = True
                            break
                        jobs.append(job)
                        known.add(job.job_id)
                        ingested += 1
                        runtime["total_enqueued_count"] = (
                            int(runtime.get("total_enqueued_count", 0) or 0) + 1
                        )
                        runtime["last_enqueued_at"] = job.created_at
                    cursor = record.position
                if stopped_at_capacity:
                    break
                if (
                    not page
                    or len(page) < _REFLECTION_EVENT_PAGE_SIZE
                    or len(window) < len(page)
                ):
                    # Every relevant event in the captured window was consumed;
                    # skipped positions belong to other immutable event kinds.
                    # Advancing to the authoritative high-water is therefore
                    # lossless and prevents repeated scans across sparse logs.
                    cursor = source_frontier
                    break
            source_frontier = max(source_frontier, cursor)
            previous_frontier = int(runtime.get("event_frontier", 0) or 0)
            if (
                cursor == original_cursor
                and not ingested
                and source_frontier == previous_frontier
            ):
                return 0
            state[_REFLECTION_EVENT_CURSOR_STATE_KEY] = cursor
            runtime["event_cursor"] = cursor
            runtime["event_frontier"] = source_frontier
            self._save_reflection_jobs(
                jobs,
                state=state,
                runtime=runtime,
            )
            await self.flush()
        if ingested:
            logger.info("ingested %d immutable reflection enqueue events", ingested)
        return ingested

    @staticmethod
    def _reflection_order_key(job: LearningReflectionJob) -> tuple[datetime, datetime]:
        """Order due reflection jobs by readiness, then by arrival.

        Insertion order is the wrong queue discipline here. The durable list only
        ever grows at the tail, so always taking the head hands every attempt to
        the same few oldest jobs: in production one job had burned 65 attempts
        while 131 jobs had never been tried once. Sorting by ``next_attempt_at``
        puts whatever is actually ready first, and a never-attempted job carries
        ``next_attempt_at == created_at`` so it naturally outranks a job whose
        backoff was just pushed forward. ``created_at`` breaks ties so equal
        readiness still drains oldest-experience-first.

        Args:
            job: The queued reflection job being ranked.

        Returns:
            Sort key of ``(next_attempt_at, created_at)`` as aware datetimes.
        """

        return (
            datetime.fromisoformat(job.next_attempt_at),
            datetime.fromisoformat(job.created_at),
        )

    async def _claim_pending_reflection(self) -> LearningReflectionJob | None:
        """Pick one due reflection job under the queue lock.

        The claimed job deliberately stays in the durable queue. If the process
        dies mid-call the experience is re-offered on restart rather than lost;
        the runner lock, not removal, is what keeps two callers off the same job.

        Returns:
            The claimed job, or ``None`` when nothing is runnable right now.
        """

        async with self._reflection_queue_lock:
            state = self.store.load_state()
            jobs = load_reflection_jobs(state)
            if not jobs or not self.reflection.can_reflect:
                return None
            runtime = self._reflection_runtime_state(state)
            retry_at = str(runtime.get("global_next_attempt_at") or "")
            if retry_at:
                try:
                    retry_time = datetime.fromisoformat(retry_at)
                except ValueError as exc:
                    raise RuntimeError("LearningReflectionCircuitStateCorrupt") from exc
                if retry_time.tzinfo is None:
                    retry_time = retry_time.replace(tzinfo=UTC)
                if retry_time.astimezone(UTC) > datetime.now(UTC):
                    return None
            due = sorted(
                (job for job in jobs if job.due()),
                key=self._reflection_order_key,
            )
            if not due:
                return None
            runtime["last_attempt_at"] = _now_iso()
            self._save_reflection_jobs(jobs, state=state, runtime=runtime)
            await self.flush()
            return due[0]

    async def _record_reflection_failure(
        self,
        job: LearningReflectionJob,
        error: Exception,
    ) -> None:
        """Persist the retry envelope for a failed reflection job."""

        async with self._reflection_queue_lock:
            state = self.store.load_state()
            jobs = load_reflection_jobs(state)
            index = next(
                (
                    position
                    for position, item in enumerate(jobs)
                    if item.job_id == job.job_id
                ),
                -1,
            )
            if index < 0:
                return
            jobs[index] = jobs[index].failed(error)
            runtime = self._reflection_runtime_state(state)
            failures = int(runtime.get("consecutive_failure_count", 0) or 0) + 1
            delay_seconds = min(
                _REFLECTION_FAILURE_BACKOFF_MAX_SECONDS,
                _REFLECTION_FAILURE_BACKOFF_BASE_SECONDS * (2 ** min(failures - 1, 5)),
            )
            fingerprint = hashlib.sha256(
                f"{type(error).__module__}:{type(error).__qualname__}".encode("utf-8")
            ).hexdigest()
            runtime.update(
                {
                    "consecutive_failure_count": failures,
                    "total_failed_attempt_count": int(
                        runtime.get("total_failed_attempt_count", 0) or 0
                    )
                    + 1,
                    "global_next_attempt_at": (
                        datetime.now(UTC) + timedelta(seconds=delay_seconds)
                    ).isoformat(),
                    "last_error_type": type(error).__name__,
                    "last_error_fingerprint": fingerprint,
                }
            )
            self._save_reflection_jobs(jobs, state=state, runtime=runtime)
            await self.flush()

    async def _retire_reflection_job(self, job: LearningReflectionJob) -> None:
        """Drop a completed reflection job from the durable queue."""

        async with self._reflection_queue_lock:
            state = self.store.load_state()
            jobs = load_reflection_jobs(state)
            remaining = [item for item in jobs if item.job_id != job.job_id]
            if len(remaining) == len(jobs):
                return
            runtime = self._reflection_runtime_state(state)
            runtime.update(
                {
                    "consecutive_failure_count": 0,
                    "global_next_attempt_at": "",
                    "last_success_at": _now_iso(),
                    "last_error_type": "",
                    "last_error_fingerprint": "",
                    "total_completed_count": int(
                        runtime.get("total_completed_count", 0) or 0
                    )
                    + 1,
                }
            )
            self._save_reflection_jobs(remaining, state=state, runtime=runtime)
            await self.flush()

    async def _run_pending_reflection(self) -> tuple[str, list[Any]] | None:
        """Run at most one due reflection without holding the queue lock.

        The LLM call used to happen inside ``_reflection_queue_lock``, so every
        ``submit_reflection`` from a live interaction blocked behind a reflection
        that could legitimately run for minutes. The lock now only covers durable
        reads and writes; ``_reflection_runner_lock`` serializes the calls, and a
        caller who finds it held leaves its request queued instead of waiting.

        Returns:
            ``(job_id, insights)`` when a reflection ran, else ``None``.
        """

        if self._reflection_runner_lock.locked():
            return None
        async with self._reflection_runner_lock:
            job = await self._claim_pending_reflection()
            if job is None:
                return None
            watermark = self.reflection.last_reflection_at
            try:
                if job.reflection_kind == "interaction":
                    insights = await self.reflection.reflect_on_interaction(
                        interaction_text=job.reflection_text,
                        context=job.context,
                        source_event_ids=list(job.source_event_ids),
                        actor_consciousness_instance_id=(
                            job.actor_consciousness_instance_id
                        ),
                    )
                else:
                    insights = await self.reflection.reflect_on_internal(
                        internal_text=job.reflection_text,
                        context=job.context,
                        source_event_ids=list(job.source_event_ids),
                        actor_consciousness_instance_id=(
                            job.actor_consciousness_instance_id
                        ),
                    )
            except Exception as exc:
                # The original failure stays authoritative: a bookkeeping error
                # must not replace the reason the reflection actually failed.
                try:
                    await self._record_reflection_failure(job, exc)
                except Exception as record_exc:  # noqa: BLE001
                    logger.warning(
                        "反思失败记账失败: %s",
                        type(record_exc).__name__,
                    )
                raise
            if not insights and self.reflection.last_reflection_at <= watermark:
                # 引擎自己也有冷却门禁，关上时它返回空列表——和"想过了但没有新
                # 洞察"是同一个返回值。用水位线把两者分开：水位没动就说明这段
                # 经历根本没被想过，退休它等于静默丢掉一段经历，所以留在队列里。
                logger.debug("反思引擎未执行（冷却门禁），保留 job 待下一轮")
                return None
            await self._retire_reflection_job(job)
            return job.job_id, insights

    # ── 事件驱动入口 ─────────────────────────────────────────

    async def on_interaction_end(
        self,
        *,
        interaction_text: str,
        context: str = "",
        source_event_ids: list[str] | None = None,
        actor_consciousness_instance_id: str = "",
    ) -> None:
        """交互结束事件：触发快环反思。"""
        try:
            await self.enqueue_reflection(
                reflection_kind="interaction",
                reflection_text=interaction_text,
                context=context,
                source_event_ids=source_event_ids,
                actor_consciousness_instance_id=actor_consciousness_instance_id,
            )
        except Exception as exc:
            logger.warning("交互反思入队异常: %s", type(exc).__name__)

    async def on_thought_closed(
        self,
        *,
        thought_summary: str,
        context: str = "",
        source_event_ids: list[str] | None = None,
        actor_consciousness_instance_id: str = "",
    ) -> None:
        """思考流闭合事件：触发内省反思。"""
        try:
            await self.enqueue_reflection(
                reflection_kind="introspection",
                reflection_text=thought_summary,
                context=context,
                source_event_ids=source_event_ids,
                actor_consciousness_instance_id=actor_consciousness_instance_id,
            )
        except Exception as exc:
            logger.warning("思考闭合反思入队异常: %s", type(exc).__name__)

    async def on_attention_thread_closed(
        self,
        *,
        public_statement: str,
        source_event_ids: list[str],
        actor_consciousness_instance_id: str,
    ) -> None:
        """Learn only from an explicit public close statement, never raw CoT."""

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
                context="主体明确关闭的持续关注线索公开表述",
                source_event_ids=sources,
                actor_consciousness_instance_id=actor,
            )
        except Exception as exc:  # noqa: BLE001 - derived learning cannot undo authority
            logger.warning(
                "持续关注线索闭合反思入队异常: %s",
                type(exc).__name__,
            )

    # ── 主体文档复盘机会 ─────────────────────────────────────

    def _subject_review_state(self) -> tuple[dict[str, Any], dict[str, Any]]:
        state = self.store.load_state()
        review = state.get(_SUBJECT_REVIEW_STATE_KEY)
        if not isinstance(review, dict):
            review = {"schema_version": 1, "documents": {}}
        documents = review.get("documents")
        if not isinstance(documents, dict):
            documents = {}
            review["documents"] = documents
        return state, review

    @staticmethod
    def _parse_review_time(value: object) -> datetime | None:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)

    @staticmethod
    def _subject_commit_from_snapshot(snapshot: Any, path: SubjectDocumentPath) -> Any:
        """Resolve one exact subject commit from a coherent authority snapshot."""

        commits = getattr(snapshot, "commits", None)
        if isinstance(commits, dict):
            commit = commits.get(path) or commits.get(f"life_engine_workspace/{path}")
        else:
            commit = None
            for item in tuple(commits or ()):
                version = getattr(item, "version", None)
                logical_path = str(getattr(version, "logical_path", "") or "")
                if logical_path in {path, f"life_engine_workspace/{path}"}:
                    commit = item
                    break
        return commit

    @classmethod
    def _subject_read_from_authority_snapshot(
        cls,
        snapshot: Any,
        path: SubjectDocumentPath,
    ) -> SubjectDocumentReadSnapshot:
        """Validate and expose one exact document from one authority snapshot."""

        commit = cls._subject_commit_from_snapshot(snapshot, path)
        version = getattr(commit, "version", None)
        content = getattr(version, "content_bytes", None)
        if content is None:
            raise RuntimeError(f"SubjectAuthoritySourceMissing: {path}")
        exact_content = bytes(content)
        version_id = str(getattr(version, "version_id", "") or "").strip()
        occurrence_id = str(getattr(version, "occurrence_id", "") or "").strip()
        revision = str(getattr(snapshot, "revision", "") or "").strip().lower()
        expected_hash = str(getattr(version, "content_hash", "") or "").lower()
        actual_hash = hashlib.sha256(exact_content).hexdigest()
        expected_length = getattr(version, "byte_length", None)
        if not version_id:
            raise RuntimeError(f"SubjectAuthorityVersionMissing: {path}")
        if not occurrence_id:
            raise RuntimeError(f"SubjectAuthorityOccurrenceMissing: {path}")
        if not revision:
            raise RuntimeError("LearningSubjectRevisionUnavailable")
        if expected_hash != actual_hash:
            raise RuntimeError(f"SubjectAuthorityContentHashMismatch: {path}")
        if isinstance(expected_length, bool) or expected_length != len(exact_content):
            raise RuntimeError(f"SubjectAuthorityByteLengthMismatch: {path}")
        return SubjectDocumentReadSnapshot(
            content_bytes=exact_content,
            version_id=version_id,
            source_occurrence_id=occurrence_id,
            unified_subject_revision=revision,
            content_sha256=actual_hash,
            byte_length=len(exact_content),
            provenance_status=str(
                getattr(version, "provenance_status", "") or "unknown"
            ),
        )

    @classmethod
    def _subject_observation_from_snapshot(
        cls,
        snapshot: Any,
        path: SubjectDocumentPath,
    ) -> tuple[bytes, str]:
        """Return exact bytes and a content-free per-document head marker."""

        document = cls._subject_read_from_authority_snapshot(snapshot, path)
        commit = cls._subject_commit_from_snapshot(snapshot, path)
        version = getattr(commit, "version", None)
        head = getattr(commit, "head", None)
        marker_material = json.dumps(
            {
                "document_id": str(getattr(head, "document_id", "") or ""),
                "current_version_id": str(
                    getattr(head, "current_version_id", "") or ""
                ),
                "head_revision": int(getattr(head, "revision", 0) or 0),
                "version_id": str(getattr(version, "version_id", "") or ""),
                "content_hash": str(getattr(version, "content_hash", "") or ""),
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return document.content_bytes, hashlib.sha256(marker_material).hexdigest()

    @classmethod
    def _subject_content_from_snapshot(
        cls,
        snapshot: Any,
        path: SubjectDocumentPath,
    ) -> bytes:
        """Return one exact subject head from an authority snapshot."""

        content, _ = cls._subject_observation_from_snapshot(snapshot, path)
        return content

    async def read_subject_document(self, path: SubjectDocumentPath) -> bytes:
        """Read an exact current authority document from the selected source."""

        if path not in SUBJECT_AUTHORITY_PATHS:
            raise ValueError(f"unsupported subject authority path: {path}")
        if self._read_subject_authority is not None:
            snapshot = await self._read_subject_authority()
            return self._subject_read_from_authority_snapshot(
                snapshot,
                path,
            ).content_bytes
        target = self._workspace / path
        if not target.exists() or not target.is_file():
            raise RuntimeError(f"SubjectAuthoritySourceMissing: {path}")
        return target.read_bytes()

    async def read_subject_document_snapshot(
        self,
        path: SubjectDocumentPath,
    ) -> SubjectDocumentReadSnapshot:
        """Read exact bytes and immutable provenance from one coherent source."""

        if path not in SUBJECT_AUTHORITY_PATHS:
            raise ValueError(f"unsupported subject authority path: {path}")
        if self._read_subject_authority is not None:
            snapshot = await self._read_subject_authority()
            return self._subject_read_from_authority_snapshot(snapshot, path)

        content = await self.read_subject_document(path)
        revision = await self.current_subject_revision()
        digest = hashlib.sha256(content).hexdigest()
        version_id = f"workspace-{Path(path).stem.lower()}-sha256:{digest}"
        return SubjectDocumentReadSnapshot(
            content_bytes=content,
            version_id=version_id,
            source_occurrence_id=f"workspace-observation:{digest}",
            unified_subject_revision=revision,
            content_sha256=digest,
            byte_length=len(content),
            provenance_status="local_workspace_observation",
        )

    async def read_subject_document_with_identity(
        self,
        path: SubjectDocumentPath,
    ) -> tuple[bytes, str, str]:
        """Read one exact document with its immutable version and subject revision.

        Selected storage is sampled once, so callers never combine bytes from one
        authority snapshot with a revision from another.  The local compatibility
        path derives a content-addressed version identity without writing anything.
        """

        document = await self.read_subject_document_snapshot(path)
        return (
            document.content_bytes,
            document.version_id,
            document.unified_subject_revision,
        )

    @staticmethod
    def _continuity_memory_pressure(
        *,
        path: SubjectDocumentPath,
        size_bytes: int,
        record: dict[str, Any],
    ) -> dict[str, int | bool | str]:
        """Describe technical MEMORY pressure without ranking its meaning."""

        if path != "MEMORY.md":
            return {
                "soft_target_bytes": 0,
                "review_pressure_bytes": 0,
                "review_growth_bytes": 0,
                "soft_target_exceeded": False,
                "review_pressure_reached": False,
                "review_pressure_acknowledged": False,
                "review_pressure_due": False,
                "pressure_semantics": "not_applicable",
            }
        last_acknowledged_size = max(
            0,
            int(record.get("last_pressure_acknowledged_size_bytes") or 0),
        )
        reached = size_bytes >= CONTINUITY_MEMORY_REVIEW_PRESSURE_BYTES
        acknowledged = bool(
            reached
            and last_acknowledged_size >= CONTINUITY_MEMORY_REVIEW_PRESSURE_BYTES
            and size_bytes >= last_acknowledged_size
            and size_bytes
            < last_acknowledged_size + CONTINUITY_MEMORY_REVIEW_GROWTH_BYTES
        )
        return {
            "soft_target_bytes": CONTINUITY_MEMORY_SOFT_TARGET_BYTES,
            "review_pressure_bytes": CONTINUITY_MEMORY_REVIEW_PRESSURE_BYTES,
            "review_growth_bytes": CONTINUITY_MEMORY_REVIEW_GROWTH_BYTES,
            "soft_target_exceeded": (size_bytes > CONTINUITY_MEMORY_SOFT_TARGET_BYTES),
            "review_pressure_reached": reached,
            "review_pressure_acknowledged": acknowledged,
            "review_pressure_due": bool(reached and not acknowledged),
            "pressure_semantics": "engineering_review_only",
        }

    @staticmethod
    def _continuity_memory_index_review(
        *,
        path: SubjectDocumentPath,
        content: bytes,
        version_id: str,
        subject_revision: str,
        record: dict[str, Any],
    ) -> dict[str, int | bool | str]:
        """Invite structural review without inferring memory importance."""

        if path != "MEMORY.md":
            return {
                "continuity_index_entry_count": 0,
                "continuity_index_issue_count": 0,
                "continuity_index_absent": False,
                "continuity_index_review_due": False,
                "continuity_index_state": "not_applicable",
                "continuity_index_semantics": "not_applicable",
            }
        content_sha256 = hashlib.sha256(content).hexdigest()
        projection_revision = str(subject_revision or "").strip().lower()
        if len(projection_revision) != 64 or any(
            character not in "0123456789abcdef" for character in projection_revision
        ):
            projection_revision = "0" * 64
        projection_version = str(version_id or "").strip() or (
            f"subject-review-memory-sha256:{content_sha256}"
        )
        diagnostics = diagnose_continuity_memory_index(
            content,
            subject_document_version_id=projection_version,
            unified_subject_revision=projection_revision,
        )
        entry_count = len(diagnostics.index.entries)
        issue_count = len(diagnostics.issues)
        absent = entry_count == 0
        last_outcome = str(record.get("last_outcome") or "").strip().lower()
        exact_version_acknowledged = bool(
            last_outcome != "snoozed"
            and str(record.get("last_reviewed_content_sha256") or "").lower()
            == content_sha256
        )
        last_reviewed_size = max(
            0,
            int(record.get("last_reviewed_size_bytes") or 0),
        )
        absence_acknowledged = bool(
            last_outcome != "snoozed"
            and last_reviewed_size > CONTINUITY_MEMORY_SOFT_TARGET_BYTES
            and len(content)
            < last_reviewed_size + CONTINUITY_MEMORY_REVIEW_GROWTH_BYTES
        )
        review_due = bool(
            (issue_count > 0 and not exact_version_acknowledged)
            or (
                len(content) > CONTINUITY_MEMORY_SOFT_TARGET_BYTES
                and absent
                and not absence_acknowledged
            )
        )
        state = "invalid" if issue_count else "absent" if absent else "present"
        return {
            "continuity_index_entry_count": entry_count,
            "continuity_index_issue_count": issue_count,
            "continuity_index_absent": absent,
            "continuity_index_review_due": review_due,
            "continuity_index_state": state,
            "continuity_index_semantics": "structural_review_only",
        }

    async def _review_document_snapshot(
        self,
        *,
        path: SubjectDocumentPath,
        record: dict[str, Any],
        now: datetime,
        mark_offered: bool,
        subject_revision: str,
        authority_snapshot: Any | None = None,
    ) -> tuple[dict[str, Any], bool]:
        exists = False
        changed_at: datetime | None = None
        content = b""
        size_bytes = 0
        content_sha256 = ""
        change_marker = ""
        version_id = ""
        source_occurrence_id = ""
        source_provenance_status = ""
        changed = False
        if self._read_subject_authority is not None:
            if authority_snapshot is None:
                raise RuntimeError("SubjectAuthoritySnapshotUnavailable")
            document = self._subject_read_from_authority_snapshot(
                authority_snapshot,
                path,
            )
            content, change_marker = self._subject_observation_from_snapshot(
                authority_snapshot,
                path,
            )
            exists = True
            size_bytes = document.byte_length
            content_sha256 = document.content_sha256
            version_id = document.version_id
            source_occurrence_id = document.source_occurrence_id
            source_provenance_status = document.provenance_status
        else:
            target = self._workspace / path
            exists = target.exists() and target.is_file()
            if exists:
                stat = target.stat()
                changed_at = datetime.fromtimestamp(stat.st_mtime, tz=UTC)
                content = target.read_bytes()
                size_bytes = len(content)
                content_sha256 = hashlib.sha256(content).hexdigest()
                version_id = (
                    f"workspace-{Path(path).stem.lower()}-sha256:{content_sha256}"
                )
                source_occurrence_id = f"workspace-observation:{content_sha256}"
                source_provenance_status = "local_workspace_observation"

        last_reviewed = self._parse_review_time(record.get("last_reviewed_at"))
        if self._read_subject_authority is not None:
            baseline = self._parse_review_time(record.get("review_baseline_at"))
            baseline_hash = str(record.get("review_baseline_content_sha256") or "")
            baseline_marker = str(record.get("review_baseline_change_marker") or "")
            reset_baseline = baseline is None or (
                exists
                and (
                    baseline_hash != content_sha256
                    or bool(
                        change_marker
                        and baseline_marker
                        and change_marker != baseline_marker
                    )
                )
            )
            if reset_baseline:
                baseline = now
                record.update(
                    {
                        "review_baseline_at": now.isoformat(),
                        "review_baseline_content_sha256": content_sha256,
                        "review_baseline_change_marker": change_marker,
                        "review_baseline_subject_revision": subject_revision,
                        # An offer or snooze is bound to the prior exact head.
                        "last_offered_at": "",
                        "snooze_until": "",
                    }
                )
                changed = True
            elif change_marker and not baseline_marker:
                # A backend upgraded to expose head identity.  Preserve the
                # established clock while binding future changes to the marker.
                record["review_baseline_change_marker"] = change_marker
                changed = True
            observed_exists = record.get("last_observed_exists")
            if observed_exists is not exists:
                record["last_observed_exists"] = exists
                record["last_observed_at"] = now.isoformat()
                changed = True
            if int(record.get("last_observed_size_bytes") or 0) != size_bytes:
                record["last_observed_size_bytes"] = size_bytes
                changed = True
            baseline = max(
                (item for item in (baseline, last_reviewed) if item is not None),
                default=now,
            )
        else:
            baseline = max(
                (item for item in (changed_at, last_reviewed) if item is not None),
                default=now,
            )
        interval_hours = self._subject_review_intervals[path]
        due_at = baseline + timedelta(hours=interval_hours)
        snooze_until = self._parse_review_time(record.get("snooze_until"))
        last_offered = self._parse_review_time(record.get("last_offered_at"))
        offer_after = (
            last_offered + timedelta(hours=self._subject_review_offer_cooldown_hours)
            if last_offered is not None
            else None
        )
        pressure = self._continuity_memory_pressure(
            path=path,
            size_bytes=size_bytes,
            record=record,
        )
        index_review = self._continuity_memory_index_review(
            path=path,
            content=content,
            version_id=version_id,
            subject_revision=subject_revision,
            record=record,
        )
        observed_projection = {
            "last_observed_version_id": version_id,
            "last_observed_source_occurrence_id": source_occurrence_id,
            "last_observed_source_provenance_status": source_provenance_status,
            **index_review,
        }
        for key, value in observed_projection.items():
            if record.get(key) != value:
                record[key] = value
                changed = True
        interval_due = now >= due_at
        pressure_due = bool(pressure["review_pressure_due"])
        index_review_due = bool(index_review["continuity_index_review_due"])
        pending_candidate_due = bool(
            str(record.get("last_outcome") or "").strip().lower()
            == "candidate_proposed"
            and str(record.get("last_candidate_id") or "").strip()
        )
        due = bool(
            self._subject_review_enabled
            and exists
            and (
                interval_due
                or pressure_due
                or index_review_due
                or pending_candidate_due
            )
            and (snooze_until is None or now >= snooze_until)
            and (offer_after is None or now >= offer_after)
        )
        if due and mark_offered:
            record["last_offered_at"] = now.isoformat()
            changed = True

        return (
            {
                "target_path": path,
                "exists": exists,
                "size_bytes": size_bytes,
                "content_sha256": content_sha256,
                "version_id": version_id,
                "source_occurrence_id": source_occurrence_id,
                "source_provenance_status": source_provenance_status,
                "interval_hours": interval_hours,
                "changed_at": changed_at.isoformat() if changed_at else "",
                "review_baseline_at": str(record.get("review_baseline_at") or ""),
                "change_marker": change_marker,
                "due_at": due_at.isoformat(),
                "due": due,
                "due_reasons": [
                    reason
                    for reason, active in (
                        ("interval", interval_due),
                        ("engineering_pressure", pressure_due),
                        ("continuity_index_review", index_review_due),
                        ("candidate_decision_pending", pending_candidate_due),
                    )
                    if active
                ],
                **pressure,
                **index_review,
                "last_offered_at": str(record.get("last_offered_at") or ""),
                "last_reviewed_at": str(record.get("last_reviewed_at") or ""),
                "last_outcome": str(record.get("last_outcome") or ""),
                "last_actor_consciousness_instance_id": str(
                    record.get("last_actor_consciousness_instance_id") or ""
                ),
                "last_subject_revision": str(record.get("last_subject_revision") or ""),
                "last_occurrence_id": str(record.get("last_occurrence_id") or ""),
                "last_candidate_id": str(record.get("last_candidate_id") or ""),
                "last_candidate_sha256": str(record.get("last_candidate_sha256") or ""),
                "last_committed_subject_revision": str(
                    record.get("last_committed_subject_revision") or ""
                ),
                "last_authority_occurrence_id": str(
                    record.get("last_authority_occurrence_id") or ""
                ),
                "snooze_until": str(record.get("snooze_until") or ""),
            },
            changed,
        )

    async def get_subject_review_snapshot(
        self,
        *,
        mark_offered: bool = False,
    ) -> dict[str, Any]:
        """Return a content-free review invitation, never a semantic verdict."""

        revision = ""
        revision_error = ""
        authority_snapshot: Any | None = None
        if self._read_subject_authority is not None:
            try:
                authority_snapshot = await self._read_subject_authority()
                revision = (
                    str(getattr(authority_snapshot, "revision", "") or "")
                    .strip()
                    .lower()
                )
                if len(revision) != 64 or any(
                    character not in "0123456789abcdef" for character in revision
                ):
                    raise RuntimeError("LearningSubjectRevisionUnavailable")
            except Exception as exc:  # noqa: BLE001 - health must remain available
                revision_error = type(exc).__name__
        elif self._current_subject_revision is None:
            revision_error = "subject_revision_unavailable"
        else:
            try:
                revision = str(await self._current_subject_revision()).strip().lower()
            except Exception as exc:  # noqa: BLE001 - health must remain available
                revision_error = type(exc).__name__

        if self._read_subject_authority is not None and authority_snapshot is None:
            health = self._subject_review_health_snapshot()
            documents = [
                {**dict(item), "due": False, "due_reasons": []}
                for item in health.get("documents", [])
                if isinstance(item, dict)
            ]
            return {
                "status": "degraded",
                "authority_status": (
                    "selected_ready"
                    if self.decision_ledger is not None
                    else "migration_required"
                ),
                "direct_mutation_blocked": True,
                "subject_revision": "",
                "revision_error": revision_error,
                "due_count": 0,
                "pending_candidate_count": int(
                    health.get("pending_candidate_count", 0) or 0
                ),
                "documents": documents,
            }

        async with self._subject_review_lock:
            state, review = self._subject_review_state()
            documents = review["documents"]
            assert isinstance(documents, dict)
            now = datetime.now(UTC)
            snapshots: list[dict[str, Any]] = []
            changed = False
            for path in SUBJECT_AUTHORITY_PATHS:
                raw_record = documents.get(path)
                record = dict(raw_record) if isinstance(raw_record, dict) else {}
                snapshot, document_changed = await self._review_document_snapshot(
                    path=path,
                    record=record,
                    now=now,
                    mark_offered=mark_offered,
                    subject_revision=revision,
                    authority_snapshot=authority_snapshot,
                )
                snapshots.append(snapshot)
                if document_changed or not isinstance(raw_record, dict):
                    documents[path] = record
                    changed = True
            if (
                revision
                and str(review.get("last_observed_subject_revision") or "") != revision
            ):
                review["last_observed_subject_revision"] = revision
                review["last_observed_at"] = now.isoformat()
                changed = True
            if changed:
                state[_SUBJECT_REVIEW_STATE_KEY] = review
                self.store.save_state(state)
                await self.flush()

        due_count = sum(bool(item["due"]) for item in snapshots)
        pending_count = sum(
            str(item.get("last_outcome") or "") in {"candidate_proposed", "kept_open"}
            for item in snapshots
        )
        return {
            "status": (
                "disabled"
                if not self._subject_review_enabled
                else "ready"
                if not revision_error
                else "degraded"
            ),
            "authority_status": (
                "selected_ready"
                if self.decision_ledger is not None
                else "migration_required"
            ),
            "direct_mutation_blocked": True,
            "subject_revision": revision,
            "revision_error": revision_error,
            "due_count": due_count,
            "pending_candidate_count": pending_count,
            "documents": snapshots,
        }

    async def get_subject_review_prompt(self) -> str:
        """Render a bounded invitation; silence and no-change remain valid choices."""

        if self._projector_quiesced:
            return ""
        snapshot = await self.get_subject_review_snapshot(mark_offered=False)
        due = [item for item in snapshot["documents"] if item.get("due")]
        if not due:
            self._pending_subject_review_offer = None
            return ""
        offer_material = {
            "subject_revision": snapshot["subject_revision"],
            "documents": [
                {
                    "target_path": item["target_path"],
                    "content_sha256": item["content_sha256"],
                    "version_id": item["version_id"],
                    "change_marker": item["change_marker"],
                    "due_reasons": list(item["due_reasons"]),
                    "candidate_id": item["last_candidate_id"],
                    "candidate_sha256": item["last_candidate_sha256"],
                }
                for item in due
            ],
        }
        delivery_id = (
            "subject_review_offer_"
            + hashlib.sha256(
                json.dumps(
                    offer_material,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
        )
        delivery_marker = f"<subject_review_offer delivery_id={delivery_id}>"
        self._pending_subject_review_offer = {
            "delivery_id": delivery_id,
            "delivery_marker": delivery_marker,
            **offer_material,
        }
        lines = [
            delivery_marker,
            "### 主体文档复盘机会（邀请，不是任务）",
            "",
            "以下文件到了可以重新看一眼的工程时间点；这不表示内容有错，也不要求修改。",
        ]
        for item in due:
            reasons = item.get("due_reasons", [])
            if "candidate_decision_pending" in reasons:
                lines.append(
                    f"- `{item['target_path']}`：候选 `{item['last_candidate_id']}` "
                    "仍在等待你的独立决定；保持开放、拒绝或接受都有效，后台不会替你选择。"
                )
            elif "engineering_pressure" in reasons:
                lines.append(
                    f"- `{item['target_path']}`：当前 {item['size_bytes']} bytes，"
                    f"达到 {item['review_pressure_bytes']} bytes 工程复盘线；"
                    "这不表示任何条目不重要，也不授权自动删除。"
                )
            elif "continuity_index_review" in reasons:
                lines.append(
                    f"- `{item['target_path']}`：当前 {item['size_bytes']} bytes，"
                    "尚无显式长记忆 Boundary 索引或索引格式需要复核；"
                    "这只是结构性提醒，不判断任何记忆是否重要。"
                )
            else:
                lines.append(
                    f"- `{item['target_path']}`：上次内容变化 "
                    f"{item['changed_at'] or '未知'}"
                )
        lines.extend(
            [
                "- 你可以保持原样、稍后再看，或安静结束；后台不能替你解释或改写。",
                "- 想复盘时使用 `nucleus_review_subject_document` 查看状态并记录你的选择。",
            ]
        )
        if any(item["target_path"] == "MEMORY.md" for item in due):
            lines.append(
                "- 先用 `nucleus_review_subject_document` 分页看完当前精确 MEMORY；"
                "对其中很长但仍需保留的原文，优先用 "
                "`nucleus_create_memory_boundary_from_subject_range` 按 UTF-8 字节范围"
                "直接封存，避免重新转写；不来自当前 MEMORY 的完整经历仍可用 "
                "`nucleus_create_memory_boundary`。之后只能由你把返回的精确索引写进"
                "完整 MEMORY.md 候选；移除索引不会删除历史正文。"
            )
        if any(
            "candidate_decision_pending" in item.get("due_reasons", []) for item in due
        ):
            lines.append(
                "- 使用 `nucleus_list_subject_candidates` 和 "
                "`nucleus_read_subject_candidate` 重新核对候选；只有另行调用 "
                "`nucleus_decide_subject_candidate` 才会形成决定。"
            )
        if snapshot["authority_status"] == "selected_ready":
            lines.append(
                "- 若你形成了完整新版本，只能先提交候选，再单独使用主体候选决定工具接受；不会自动合并或自动接受。"
            )
        else:
            lines.append(
                "- 当前正式 Subject Authority 迁移尚未完成：可以记录保持不变或稍后再看，但新版本提交会明确拒绝，绝不退回直接写文件。"
            )
        return "\n".join(lines)

    def get_pending_subject_review_offer(self) -> dict[str, Any] | None:
        """Return content-free metadata for the prompt rendered this heartbeat."""

        pending = self._pending_subject_review_offer
        return dict(pending) if isinstance(pending, dict) else None

    async def commit_subject_review_offer_delivery(
        self,
        delivery_id: str,
        receipt: Any,
    ) -> bool:
        """Start cooldown only after exact final-attempt prompt delivery."""

        pending = self._pending_subject_review_offer
        identity = str(delivery_id or "").strip()
        if not isinstance(pending, dict) or pending.get("delivery_id") != identity:
            return False
        exact = bool(getattr(receipt, "exact_present", False))
        expected_bytes = getattr(receipt, "expected_utf8_bytes", None)
        effective_bytes = getattr(receipt, "effective_utf8_bytes", None)
        if not all(
            (
                str(getattr(receipt, "delivery_id", "") or "") == identity,
                str(getattr(receipt, "part_kind", "") or "") == "text",
                exact,
                isinstance(expected_bytes, int),
                isinstance(effective_bytes, int),
                expected_bytes == effective_bytes,
                str(getattr(receipt, "expected_sha256", "") or "")
                == str(getattr(receipt, "effective_sha256", "") or ""),
            )
        ):
            self._pending_subject_review_offer = None
            return False

        snapshot = await self.get_subject_review_snapshot(mark_offered=False)
        if str(snapshot.get("subject_revision") or "") != str(
            pending.get("subject_revision") or ""
        ):
            self._pending_subject_review_offer = None
            return False
        current_by_path = {
            str(item.get("target_path") or ""): item
            for item in snapshot.get("documents", [])
            if isinstance(item, dict)
        }
        prepared_documents = pending.get("documents")
        if not isinstance(prepared_documents, list) or not prepared_documents:
            self._pending_subject_review_offer = None
            return False
        for prepared in prepared_documents:
            if not isinstance(prepared, dict):
                self._pending_subject_review_offer = None
                return False
            current = current_by_path.get(str(prepared.get("target_path") or ""))
            if current is None or not all(
                (
                    bool(current.get("due")),
                    str(current.get("content_sha256") or "")
                    == str(prepared.get("content_sha256") or ""),
                    str(current.get("version_id") or "")
                    == str(prepared.get("version_id") or ""),
                    str(current.get("change_marker") or "")
                    == str(prepared.get("change_marker") or ""),
                    str(current.get("last_candidate_id") or "")
                    == str(prepared.get("candidate_id") or ""),
                    str(current.get("last_candidate_sha256") or "")
                    == str(prepared.get("candidate_sha256") or ""),
                )
            ):
                self._pending_subject_review_offer = None
                return False

        offered_at = datetime.now(UTC).isoformat()
        async with self._subject_review_lock:
            state, review = self._subject_review_state()
            documents = review["documents"]
            assert isinstance(documents, dict)
            for prepared in prepared_documents:
                path = str(prepared["target_path"])
                raw_record = documents.get(path)
                record = dict(raw_record) if isinstance(raw_record, dict) else {}
                record["last_offered_at"] = offered_at
                record["last_offered_content_sha256"] = str(prepared["content_sha256"])
                record["last_offered_delivery_id"] = identity
                documents[path] = record
            state[_SUBJECT_REVIEW_STATE_KEY] = review
            self.store.save_state(state)
            await self.flush()
        self._pending_subject_review_offer = None
        return True

    async def validate_subject_review_context(
        self,
        *,
        actor_consciousness_instance_id: str,
        expected_subject_revision: str,
    ) -> str:
        actor = str(actor_consciousness_instance_id or "").strip()
        if self._validate_active_consciousness_instance is None:
            raise RuntimeError("LearningPresenceGateUnavailable")
        if not await self._validate_active_consciousness_instance(actor):
            raise PermissionError("LearningDecisionActorIsNotActive")
        if self._current_subject_revision is None:
            raise RuntimeError("LearningSubjectRevisionUnavailable")
        current = str(await self._current_subject_revision()).strip().lower()
        expected = str(expected_subject_revision or "").strip().lower()
        if not expected or current != expected:
            raise RuntimeError("LearningSubjectRevisionConflict")
        return current

    async def current_subject_revision(self) -> str:
        if self._current_subject_revision is None:
            raise RuntimeError("LearningSubjectRevisionUnavailable")
        return str(await self._current_subject_revision()).strip().lower()

    def _append_local_subject_review_event(self, payload: dict[str, Any]) -> None:
        self._subject_review_journal_path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY
        descriptor = os.open(self._subject_review_journal_path, flags, 0o600)
        try:
            os.write(descriptor, f"{line}\n".encode())
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @classmethod
    def _validate_selected_snooze_event(
        cls,
        record: Any,
        *,
        occurrence_id: str,
        target_path: SubjectDocumentPath,
        actor_consciousness_instance_id: str,
        subject_revision: str,
        current_content_sha256: str,
        reason: str,
        snooze_hours: float,
    ) -> tuple[datetime, datetime]:
        """Validate and recover the immutable clock of one snooze occurrence."""

        payload = getattr(record, "payload", None)
        provenance = getattr(record, "provenance", None)
        expected_payload = {
            "schema_version": 1,
            "target_path": target_path,
            "outcome": "snoozed",
            "actor_consciousness_instance_id": actor_consciousness_instance_id,
            "subject_revision": subject_revision,
            "current_content_sha256": current_content_sha256,
            "source_occurrence_id": occurrence_id,
            "reason": reason,
            "snooze_hours": snooze_hours,
            "authority": "review_evidence_only",
        }
        identity_matches = all(
            (
                str(getattr(record, "occurrence_id", "") or "") == occurrence_id,
                str(getattr(record, "event_kind", "") or "")
                == _SUBJECT_REVIEW_SNOOZED_EVENT_KIND,
                str(getattr(record, "source", "") or "")
                == "subject.review.active_consciousness",
                str(getattr(record, "actor_consciousness_instance_id", "") or "")
                == actor_consciousness_instance_id,
                str(getattr(record, "subject_revision", "") or "") == subject_revision,
                isinstance(payload, dict),
                isinstance(provenance, dict),
            )
        )
        if not identity_matches or any(
            payload.get(key) != value for key, value in expected_payload.items()
        ):
            raise LearningOccurrenceConflict("SubjectReviewSnoozeOccurrenceConflict")
        if (
            provenance.get("source_occurrence_id") != occurrence_id
            or provenance.get("current_content_sha256") != current_content_sha256
        ):
            raise LearningOccurrenceConflict("SubjectReviewSnoozeOccurrenceConflict")
        occurred_at = cls._parse_review_time(getattr(record, "occurred_at", ""))
        snooze_until = cls._parse_review_time(payload.get("snooze_until"))
        if occurred_at is None or snooze_until is None:
            raise LearningOccurrenceConflict("SubjectReviewSnoozeOccurrenceConflict")
        expected_until = occurred_at + timedelta(hours=snooze_hours)
        if snooze_until != expected_until:
            raise LearningOccurrenceConflict("SubjectReviewSnoozeOccurrenceConflict")
        return occurred_at, snooze_until

    async def _append_selected_snooze_event(
        self,
        *,
        occurrence_id: str,
        target_path: SubjectDocumentPath,
        actor_consciousness_instance_id: str,
        subject_revision: str,
        current_content_sha256: str,
        reason: str,
        snooze_hours: float,
    ) -> tuple[datetime, datetime, str]:
        """Append one selected snooze event before updating its projection."""

        if self._learning_event_store is None:
            raise RuntimeError("SubjectReviewImmutableEvidenceRequired")
        existing = await self._learning_event_store.event_by_occurrence(occurrence_id)
        if existing is not None:
            occurred_at, snooze_until = self._validate_selected_snooze_event(
                existing,
                occurrence_id=occurrence_id,
                target_path=target_path,
                actor_consciousness_instance_id=actor_consciousness_instance_id,
                subject_revision=subject_revision,
                current_content_sha256=current_content_sha256,
                reason=reason,
                snooze_hours=snooze_hours,
            )
            return occurred_at, snooze_until, str(existing.event_sha256)

        occurred_at = datetime.now(UTC)
        snooze_until = occurred_at + timedelta(hours=snooze_hours)
        draft = LearningEventDraft(
            occurrence_id=occurrence_id,
            event_kind=_SUBJECT_REVIEW_SNOOZED_EVENT_KIND,
            occurred_at=occurred_at.isoformat(),
            source="subject.review.active_consciousness",
            actor_consciousness_instance_id=actor_consciousness_instance_id,
            subject_revision=subject_revision,
            provenance={
                "schema_version": 1,
                "source_occurrence_id": occurrence_id,
                "target_path": target_path,
                "current_content_sha256": current_content_sha256,
                "authority": "review_evidence_only",
                "writer_instance_id": self._writer_instance_id,
            },
            payload={
                "schema_version": 1,
                "target_path": target_path,
                "outcome": "snoozed",
                "actor_consciousness_instance_id": (actor_consciousness_instance_id),
                "subject_revision": subject_revision,
                "current_content_sha256": current_content_sha256,
                "source_occurrence_id": occurrence_id,
                "reason": reason,
                "snooze_hours": snooze_hours,
                "snooze_until": snooze_until.isoformat(),
                "authority": "review_evidence_only",
            },
        )
        try:
            result = await self._learning_event_store.commit(
                events=[draft],
                projections=[],
            )
        except LearningOccurrenceConflict:
            # A concurrent replay may have won between the lookup and append.
            existing = await self._learning_event_store.event_by_occurrence(
                occurrence_id
            )
            if existing is None:
                raise
            occurred_at, snooze_until = self._validate_selected_snooze_event(
                existing,
                occurrence_id=occurrence_id,
                target_path=target_path,
                actor_consciousness_instance_id=actor_consciousness_instance_id,
                subject_revision=subject_revision,
                current_content_sha256=current_content_sha256,
                reason=reason,
                snooze_hours=snooze_hours,
            )
            return occurred_at, snooze_until, str(existing.event_sha256)
        if len(result.events) != 1:
            raise RuntimeError("SubjectReviewImmutableEvidenceMissing")
        occurred_at, snooze_until = self._validate_selected_snooze_event(
            result.events[0],
            occurrence_id=occurrence_id,
            target_path=target_path,
            actor_consciousness_instance_id=actor_consciousness_instance_id,
            subject_revision=subject_revision,
            current_content_sha256=current_content_sha256,
            reason=reason,
            snooze_hours=snooze_hours,
        )
        return occurred_at, snooze_until, str(result.events[0].event_sha256)

    @staticmethod
    def _require_review_event_hash(record: Any) -> str:
        digest = str(getattr(record, "event_sha256", "") or "").strip().lower()
        if len(digest) != 64 or any(
            character not in "0123456789abcdef" for character in digest
        ):
            raise RuntimeError("SubjectReviewImmutableEvidenceHashMissing")
        return digest

    async def _verify_selected_review_evidence(
        self,
        *,
        target_path: SubjectDocumentPath,
        outcome: str,
        actor_consciousness_instance_id: str,
        subject_revision: str,
        occurrence_id: str,
        reason: str,
        candidate_id: str,
        candidate_sha256: str,
        authority_occurrence_id: str,
    ) -> tuple[datetime, str]:
        """Derive one review projection input from immutable Learning evidence."""

        if self._learning_event_store is None:
            raise RuntimeError("SubjectReviewImmutableEvidenceRequired")
        event = await self._learning_event_store.event_by_occurrence(occurrence_id)
        if event is None:
            raise RuntimeError("SubjectReviewImmutableEvidenceMissing")
        payload = getattr(event, "payload", None)
        provenance = getattr(event, "provenance", None)
        if not isinstance(payload, dict) or not isinstance(provenance, dict):
            raise RuntimeError("SubjectReviewImmutableEvidenceCorrupt")

        actor = str(actor_consciousness_instance_id)
        candidate = str(candidate_id)
        candidate_hash = str(candidate_sha256).lower()
        reason_hash = hashlib.sha256(reason.encode("utf-8")).hexdigest()
        common_matches = all(
            (
                str(getattr(event, "occurrence_id", "") or "") == occurrence_id,
                str(getattr(event, "actor_consciousness_instance_id", "") or "")
                == actor,
                str(payload.get("candidate_id") or "") == candidate,
                str(payload.get("candidate_sha256") or "").lower() == candidate_hash,
                str(payload.get("target_path") or "") == target_path,
            )
        )
        if outcome == "candidate_proposed":
            matches = all(
                (
                    common_matches,
                    str(getattr(event, "event_kind", "") or "") == "candidate.proposed",
                    str(getattr(event, "subject_revision", "") or "").lower()
                    == subject_revision,
                    str(provenance.get("review_reason_sha256") or "").lower()
                    == reason_hash,
                )
            )
        else:
            expected_kind = {
                "unchanged": "candidate.kept_open",
                "rejected": "candidate.rejected",
                "kept_open": "candidate.kept_open",
                "committed": "candidate.accept_requested",
            }.get(outcome)
            matches = all(
                (
                    common_matches,
                    bool(expected_kind),
                    str(getattr(event, "event_kind", "") or "") == expected_kind,
                    str(payload.get("reason") or "") == reason,
                    str(getattr(event, "source", "") or "")
                    == "learning.subject_decision",
                )
            )
            if outcome == "unchanged":
                matches = bool(
                    matches and provenance.get("review_outcome") == "unchanged"
                )
            elif outcome != "committed":
                matches = bool(
                    matches
                    and str(getattr(event, "subject_revision", "") or "").lower()
                    == subject_revision
                )
        if not matches:
            raise LearningOccurrenceConflict("SubjectReviewEvidenceConflict")

        evidence_hash = self._require_review_event_hash(event)
        occurred_at = self._parse_review_time(getattr(event, "occurred_at", ""))
        if occurred_at is None:
            raise RuntimeError("SubjectReviewImmutableEvidenceTimeMissing")
        if outcome != "committed":
            return occurred_at, evidence_hash

        authority_identity = str(authority_occurrence_id or "").strip()
        if not authority_identity:
            raise RuntimeError("SubjectReviewAuthorityEvidenceRequired")
        authority_event = await self._learning_event_store.event_by_occurrence(
            f"learning_authority:{authority_identity}"
        )
        if authority_event is None:
            raise RuntimeError("SubjectReviewAuthorityEvidenceMissing")
        authority_payload = getattr(authority_event, "payload", None)
        authority_provenance = getattr(authority_event, "provenance", None)
        if not isinstance(authority_payload, dict) or not isinstance(
            authority_provenance, dict
        ):
            raise RuntimeError("SubjectReviewAuthorityEvidenceCorrupt")
        if not all(
            (
                str(getattr(authority_event, "event_kind", "") or "")
                == "candidate.committed",
                str(getattr(authority_event, "subject_revision", "") or "").lower()
                == subject_revision,
                str(authority_payload.get("candidate_id") or "") == candidate,
                str(authority_payload.get("decision_occurrence_id") or "")
                == occurrence_id,
                str(authority_payload.get("new_subject_revision") or "").lower()
                == subject_revision,
                str(authority_provenance.get("authority_occurrence_id") or "")
                == authority_identity,
                str(authority_provenance.get("decision_occurrence_id") or "")
                == occurrence_id,
            )
        ):
            raise LearningOccurrenceConflict("SubjectReviewAuthorityEvidenceConflict")
        authority_hash = self._require_review_event_hash(authority_event)
        authority_time = self._parse_review_time(
            getattr(authority_event, "occurred_at", "")
        )
        if authority_time is None:
            raise RuntimeError("SubjectReviewAuthorityEvidenceTimeMissing")
        combined = hashlib.sha256(
            f"{evidence_hash}\0{authority_hash}".encode("ascii")
        ).hexdigest()
        return authority_time, combined

    async def record_subject_review_outcome(
        self,
        *,
        target_path: SubjectDocumentPath,
        outcome: str,
        actor_consciousness_instance_id: str,
        subject_revision: str,
        occurrence_id: str,
        reason: str,
        candidate_id: str = "",
        candidate_sha256: str = "",
        authority_occurrence_id: str = "",
        snooze_hours: float = 0.0,
    ) -> dict[str, Any]:
        """Project an active-instance review choice into bounded health state."""

        if target_path not in SUBJECT_AUTHORITY_PATHS:
            raise ValueError("invalid subject review target")
        normalized_outcome = str(outcome or "").strip().lower()
        if normalized_outcome not in {
            "unchanged",
            "candidate_proposed",
            "snoozed",
            "committed",
            "rejected",
            "kept_open",
        }:
            raise ValueError("invalid subject review outcome")
        reason_text = str(reason or "").strip()
        occurrence_text = str(occurrence_id or "").strip()
        actor = str(actor_consciousness_instance_id or "").strip()
        revision = str(subject_revision or "").strip().lower()
        if not reason_text:
            raise ValueError("subject review reason must not be empty")
        if not occurrence_text:
            raise ValueError("subject review occurrence must not be empty")
        occurred_at = datetime.now(UTC)
        event = {
            "schema_version": 1,
            "occurrence_id": occurrence_text,
            "occurred_at": occurred_at.isoformat(),
            "target_path": target_path,
            "outcome": normalized_outcome,
            "actor_consciousness_instance_id": actor,
            "subject_revision": revision,
            "reason": reason_text,
            "candidate_id": str(candidate_id),
            "candidate_sha256": str(candidate_sha256),
            "authority_occurrence_id": str(authority_occurrence_id),
            "authority": "review_evidence_only",
        }
        async with self._subject_review_lock:
            state, review = self._subject_review_state()
            documents = review["documents"]
            assert isinstance(documents, dict)
            raw_record = documents.get(target_path)
            record = dict(raw_record) if isinstance(raw_record, dict) else {}
            replayed = bool(
                str(record.get("last_occurrence_id") or "") == occurrence_text
                and str(record.get("last_outcome") or "") == normalized_outcome
            )
            selected_review_mode = bool(
                self._read_subject_authority is not None
                or self._selected_persistence is not None
                or self.decision_ledger is not None
            )
            reason_sha256 = hashlib.sha256(reason_text.encode("utf-8")).hexdigest()
            if replayed and not selected_review_mode:
                replay_matches = all(
                    (
                        str(record.get("last_actor_consciousness_instance_id") or "")
                        == actor,
                        str(record.get("last_subject_revision") or "").lower()
                        == revision,
                        str(record.get("last_candidate_id") or "") == str(candidate_id),
                        str(record.get("last_candidate_sha256") or "").lower()
                        == str(candidate_sha256).lower(),
                        str(record.get("last_authority_occurrence_id") or "")
                        == str(authority_occurrence_id),
                        str(record.get("last_review_reason_sha256") or "").lower()
                        == reason_sha256,
                    )
                )
                if not replay_matches:
                    raise LearningOccurrenceConflict(
                        "SubjectReviewProjectionReplayConflict"
                    )
                return record
            current_content_sha256 = ""
            current_size_bytes = 0
            snooze_until: datetime | None = None
            evidence_sha256 = ""
            if normalized_outcome == "snoozed":
                if selected_review_mode and self._read_subject_authority is None:
                    raise RuntimeError("SelectedSubjectAuthorityReaderRequired")
                revision = await self.validate_subject_review_context(
                    actor_consciousness_instance_id=actor,
                    expected_subject_revision=revision,
                )
                if self._read_subject_authority is not None:
                    authority_snapshot = await self._read_subject_authority()
                    snapshot_revision = (
                        str(getattr(authority_snapshot, "revision", "") or "")
                        .strip()
                        .lower()
                    )
                    if snapshot_revision and snapshot_revision != revision:
                        raise RuntimeError("LearningSubjectRevisionConflict")
                    current_content, _ = self._subject_observation_from_snapshot(
                        authority_snapshot,
                        target_path,
                    )
                else:
                    current_content = await self.read_subject_document(target_path)
                current_content_sha256 = hashlib.sha256(current_content).hexdigest()
                current_size_bytes = len(current_content)
                bounded_snooze_hours = max(
                    1.0,
                    min(30.0 * 24.0, float(snooze_hours)),
                )
                if selected_review_mode:
                    (
                        occurred_at,
                        snooze_until,
                        evidence_sha256,
                    ) = await self._append_selected_snooze_event(
                        occurrence_id=occurrence_text,
                        target_path=target_path,
                        actor_consciousness_instance_id=actor,
                        subject_revision=revision,
                        current_content_sha256=current_content_sha256,
                        reason=reason_text,
                        snooze_hours=bounded_snooze_hours,
                    )
                else:
                    snooze_until = occurred_at + timedelta(hours=bounded_snooze_hours)
                    event.update(
                        {
                            "occurred_at": occurred_at.isoformat(),
                            "current_content_sha256": current_content_sha256,
                            "source_occurrence_id": occurrence_text,
                            "snooze_hours": bounded_snooze_hours,
                            "snooze_until": snooze_until.isoformat(),
                        }
                    )
                    evidence_sha256 = hashlib.sha256(
                        json.dumps(
                            event,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ).encode("utf-8")
                    ).hexdigest()
            else:
                current_content = await self.read_subject_document(target_path)
                current_content_sha256 = hashlib.sha256(current_content).hexdigest()
                current_size_bytes = len(current_content)
                if selected_review_mode:
                    (
                        occurred_at,
                        evidence_sha256,
                    ) = await self._verify_selected_review_evidence(
                        target_path=target_path,
                        outcome=normalized_outcome,
                        actor_consciousness_instance_id=actor,
                        subject_revision=revision,
                        occurrence_id=occurrence_text,
                        reason=reason_text,
                        candidate_id=str(candidate_id),
                        candidate_sha256=str(candidate_sha256),
                        authority_occurrence_id=str(authority_occurrence_id),
                    )
                else:
                    evidence_sha256 = hashlib.sha256(
                        json.dumps(
                            event,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ).encode("utf-8")
                    ).hexdigest()

            if replayed:
                if str(record.get("last_evidence_sha256") or "") != evidence_sha256:
                    raise LearningOccurrenceConflict(
                        "SubjectReviewProjectionReplayConflict"
                    )
                return record
            last_evidence_at = self._parse_review_time(
                record.get("last_evidence_occurred_at")
            )
            if last_evidence_at is not None and occurred_at <= last_evidence_at:
                # The immutable occurrence is valid but older than the current
                # projection. Replaying it must never roll the review clock back.
                return record

            if not selected_review_mode:
                if normalized_outcome == "candidate_proposed":
                    raise RuntimeError("SubjectAuthorityMigrationRequired")
                if normalized_outcome != "snoozed":
                    await asyncio.to_thread(
                        self._append_local_subject_review_event,
                        event,
                    )
                elif evidence_sha256:
                    await asyncio.to_thread(
                        self._append_local_subject_review_event,
                        event,
                    )
            record.update(
                {
                    "last_outcome": normalized_outcome,
                    "last_actor_consciousness_instance_id": actor,
                    "last_subject_revision": revision,
                    "last_occurrence_id": occurrence_text,
                    "last_candidate_id": str(candidate_id),
                    "last_candidate_sha256": str(candidate_sha256),
                    "last_evidence_sha256": evidence_sha256,
                    "last_evidence_occurred_at": occurred_at.isoformat(),
                    "last_review_reason_sha256": reason_sha256,
                }
            )
            if normalized_outcome != "snoozed":
                record["last_reviewed_at"] = occurred_at.isoformat()
                record["last_reviewed_content_sha256"] = current_content_sha256
                record["last_reviewed_size_bytes"] = current_size_bytes
                if target_path == "MEMORY.md":
                    record["last_pressure_acknowledged_size_bytes"] = current_size_bytes
                record["snooze_until"] = ""
            else:
                if snooze_until is None:
                    raise RuntimeError("SubjectReviewImmutableEvidenceMissing")
                record["snooze_until"] = snooze_until.isoformat()
                record["last_reviewed_content_sha256"] = current_content_sha256
                record["last_snoozed_size_bytes"] = current_size_bytes
                record["last_reason_sha256"] = hashlib.sha256(
                    reason_text.encode("utf-8")
                ).hexdigest()
            if normalized_outcome == "committed":
                record["last_committed_subject_revision"] = revision
                record["last_authority_occurrence_id"] = str(authority_occurrence_id)
            documents[target_path] = record
            state[_SUBJECT_REVIEW_STATE_KEY] = review
            self.store.save_state(state)
            await self.flush()
            return dict(record)

    # ── 心跳驱动入口 ─────────────────────────────────────────

    async def on_heartbeat(self) -> None:
        """心跳触发：检查是否需要执行审计/压缩/蒸馏/指标快照/陈旧检查。

        由 life_engine 心跳周期调用（低频，不必每次心跳都调用）。
        """
        if self._projector_quiesced:
            return
        async with self._maintenance_lock:
            await self._ingest_reflection_events()
            run_id = f"learning_heartbeat_{uuid4().hex}"
            phases: tuple[
                tuple[
                    LearningPhase,
                    Callable[[], bool],
                    Callable[[], Awaitable[None]],
                    Callable[[], int | None],
                ],
                ...,
            ] = (
                (
                    LearningPhase.REFLECTION,
                    self._reflection_work_due,
                    self._run_pending_reflection_phase,
                    self._reflection_pending_count,
                ),
                (
                    LearningPhase.EPISTEMIC_BACKFILL,
                    lambda: not self._epistemic_backfilled,
                    self._run_epistemic_backfill,
                    lambda: len(self.store.list_validated()),
                ),
                (
                    LearningPhase.AUDIT,
                    self._should_audit,
                    self._maybe_run_audit,
                    lambda: len(self.store.list_candidates_for_review()),
                ),
                (
                    LearningPhase.COMPRESSION,
                    self._compression_work_due,
                    self._maybe_run_compression,
                    lambda: len(self.store.list_for_compression()),
                ),
                (
                    LearningPhase.DISTILLATION,
                    self.distiller.should_distill,
                    self._maybe_run_distillation,
                    self.distiller.pending_count,
                ),
                (
                    LearningPhase.METRICS,
                    self._should_snapshot_metrics,
                    self._maybe_snapshot_metrics,
                    lambda: None,
                ),
                (
                    LearningPhase.STALENESS,
                    self._should_check_staleness,
                    self._maybe_check_staleness,
                    lambda: len(
                        self.store.get_stale_insights(
                            staleness_threshold_days=(self._staleness_threshold_days)
                        )
                    ),
                ),
            )
            for phase, is_due, operation, pending_count in phases:
                await self._run_maintenance_phase(
                    run_id=run_id,
                    phase=phase,
                    is_due=is_due,
                    operation=operation,
                    pending_count=pending_count,
                )
            await self.flush()

    async def _run_epistemic_backfill(self) -> None:
        await self._backfill_epistemic_claims()
        self._epistemic_backfilled = True

    async def _run_maintenance_phase(
        self,
        *,
        run_id: str,
        phase: LearningPhase,
        is_due: Callable[[], bool],
        operation: Callable[[], Awaitable[None]],
        pending_count: Callable[[], int | None],
    ) -> None:
        """Run one due phase without allowing it to starve later phases."""

        try:
            due = bool(is_due())
        except Exception as exc:  # noqa: BLE001 - phase evidence boundary
            await self._record_phase_failure(
                run_id=run_id,
                phase=phase,
                started_at=datetime.now(UTC),
                pending_count=None,
                error=exc,
            )
            return
        if not due:
            return

        try:
            count = pending_count()
        except Exception:  # noqa: BLE001 - diagnostics must not block work
            count = None
        started_at = datetime.now(UTC)
        started = LearningMaintenanceEvent.started(
            run_id=run_id,
            phase=phase,
            started_at=started_at,
            pending_count=count,
        )
        try:
            await self.maintenance_journal.append(started)
        except Exception as exc:  # noqa: BLE001 - fail this phase closed
            logger.warning(
                "学习维护阶段无法记录开始证据，已拒绝执行 %s: %s",
                phase.value,
                type(exc).__name__,
            )
            return

        try:
            await operation()
            await self.flush()
        except Exception as exc:  # noqa: BLE001 - isolate each phase
            try:
                await self.flush()
            except Exception as flush_error:  # noqa: BLE001 - preserve both
                exc.add_note(
                    "partial learning mutations also failed to flush: "
                    f"{type(flush_error).__name__}"
                )
            await self._record_phase_failure(
                run_id=run_id,
                phase=phase,
                started_at=started_at,
                pending_count=count,
                error=exc,
            )
            logger.warning(
                "学习维护阶段失败但不会阻断后续阶段 %s: %s",
                phase.value,
                type(exc).__name__,
            )
            return

        succeeded = LearningMaintenanceEvent.succeeded(
            run_id=run_id,
            phase=phase,
            started_at=started_at,
            pending_count=count,
        )
        try:
            await self.maintenance_journal.append(succeeded)
        except Exception as exc:  # noqa: BLE001 - started event remains auditable
            logger.warning(
                "学习维护阶段完成但结果证据写入失败 %s: %s",
                phase.value,
                type(exc).__name__,
            )

    async def _record_phase_failure(
        self,
        *,
        run_id: str,
        phase: LearningPhase,
        started_at: datetime,
        pending_count: int | None,
        error: Exception,
    ) -> None:
        failed = LearningMaintenanceEvent.failed(
            run_id=run_id,
            phase=phase,
            started_at=started_at,
            pending_count=pending_count,
            error=error,
        )
        try:
            await self.maintenance_journal.append(failed)
        except Exception as journal_error:  # noqa: BLE001 - preserve original
            logger.warning(
                "学习维护失败证据写入失败 %s: %s",
                phase.value,
                type(journal_error).__name__,
            )

    async def _maybe_run_audit(self) -> None:
        """检查是否到了审计时间。"""
        # 回收必须在门禁之前：_should_audit 要求候选非空，而卡在 under_review 的
        # 洞察恰好不算候选。放在门禁之后，遗留洞察就永远等不到被回收的那一轮。
        try:
            await self.auditor.reclaim_stranded_reviews()
        except Exception as exc:  # noqa: BLE001
            logger.warning("回收审计遗留洞察失败: %s", type(exc).__name__)
        if not self._should_audit():
            return
        logger.info("🔍 触发审计环")
        records = await self.auditor.run_audit_cycle()
        if records:
            self._last_audit_at = _now_iso()
            state = self.store.load_state()
            state["last_audit_at"] = self._last_audit_at
            self.store.save_state(state)
            # 将新验证的洞察投影到认识论层
            await self._project_validated_to_epistemic(records)

    async def _maybe_run_compression(self) -> None:
        """检查是否需要压缩。"""
        proposed = False
        if self.compressor.should_compress():
            logger.info("📝 触发慢环压缩")
            proposed = await self.compressor.run_compression()
        if proposed:
            # Proposal evidence is durable, but no background service is allowed
            # to promote it into subject or self-authoritative content.
            await self.flush()
            self._snapshot_metrics_now()
        await self._bridge_pending_knowledge_candidate()

    def _compression_work_due(self) -> bool:
        if self.compressor.should_compress():
            return True
        if self.decision_ledger is None:
            return False
        state = self.store.load_state()
        return int(state.get("last_knowledge_candidate_version", 0) or 0) > int(
            state.get("last_knowledge_candidate_ledgered_version", 0) or 0
        )

    async def _bridge_pending_knowledge_candidate(self) -> None:
        """Persist a deterministic subject proposal without accepting it."""

        if self.decision_ledger is None:
            return
        state = self.store.load_state()
        version = int(state.get("last_knowledge_candidate_version", 0) or 0)
        ledgered = int(state.get("last_knowledge_candidate_ledgered_version", 0) or 0)
        if version <= 0 or version <= ledgered:
            return
        if self._current_subject_revision is None:
            raise RuntimeError("LearningSubjectAuthorityRevisionUnavailable")
        content = self.store.read_knowledge_version(version)
        content_bytes = content.encode("utf-8")
        content_sha256 = hashlib.sha256(content_bytes).hexdigest()
        manifest = self.store.load_knowledge_manifest()
        versions = manifest.get("versions", [])
        version_record = next(
            (
                item
                for item in versions
                if isinstance(item, dict)
                and int(item.get("version", 0) or 0) == version
            ),
            None,
        )
        if version_record is None:
            raise RuntimeError("LearningKnowledgeCandidateManifestMissing")
        occurred_at = str(version_record.get("timestamp", ""))
        if not occurred_at:
            raise RuntimeError("LearningKnowledgeCandidateTimestampMissing")
        subject_revision = await self._current_subject_revision()
        candidate_id = f"learning_knowledge_v{version}_{content_sha256[:16]}"
        candidate = LearningCandidate.create(
            candidate_id=candidate_id,
            candidate_revision=1,
            candidate_occurrence_id=f"learning_candidate:{candidate_id}:1",
            candidate_kind="derived_observation_document",
            candidate_content_bytes=content_bytes,
            source_occurrence_id=(f"knowledge_version:{version}:{content_sha256}"),
            source="learning.knowledge_compression",
            subject_revision=subject_revision,
            target_path="MEMORY.md",
            occurred_at=occurred_at,
            provenance={
                "knowledge_version": version,
                "independent_gate_recommended": bool(
                    state.get("last_knowledge_candidate_recommended", False)
                ),
                "authority": "candidate_only",
            },
        )
        await self.decision_ledger.append_candidate(candidate)
        state = self.store.load_state()
        state["last_knowledge_candidate_ledgered_version"] = version
        state["last_knowledge_candidate_id"] = candidate_id
        self.store.save_state(state)
        await self.flush()

    async def _backfill_epistemic_claims(self) -> None:
        """一次性回填：将所有历史 validated 洞察投影到认识论层。

        幂等——已存在的 claim_id 会被跳过。只在首次心跳时执行。
        """
        if self._memory_service is None:
            return
        validated_insights = self.store.list_validated()
        if not validated_insights:
            return

        projected = 0
        for insight in validated_insights:
            try:
                projected += int(await self._project_insight_to_epistemic(insight))
            except Exception as exc:
                logger.debug("回填洞察投影失败: %s", type(exc).__name__)

        if projected:
            logger.info(f"🧠 认识论回填: {projected} 条历史验证洞察 → claims")

    async def _project_validated_to_epistemic(self, records: list) -> None:
        """Project audit output as a retrievable observation, never as truth."""
        if self._memory_service is None:
            return
        validated = [
            r
            for r in records
            if getattr(r, "verdict", "") == AuditVerdict.VALIDATED.value
        ]
        if not validated:
            return

        projected = 0
        for record in validated:
            insight = self.store.get_insight(record.insight_id)
            if insight is None:
                continue
            try:
                projected += int(
                    await self._project_insight_to_epistemic(
                        insight,
                        audit_record=record,
                    )
                )
            except Exception as exc:
                logger.debug(
                    "洞察投影到认识论层失败: %s",
                    type(exc).__name__,
                )

        if projected:
            logger.info(f"🧠 认识论投影: {projected} 条验证洞察 → claims")

    async def _project_insight_to_epistemic(
        self,
        insight: Any,
        *,
        audit_record: Any | None = None,
    ) -> bool:
        """投影洞察及其证据，但不把学习系统标签升级成事实权限。"""

        from ..memory.epistemic import ClaimEvidence, new_claim

        claim_id = f"insight_{insight.insight_id}"
        try:
            existing = await self._memory_service.get_memory_claim_state(claim_id)
        except Exception as exc:
            logger.debug(
                "查询洞察投影状态失败，交由追加账本执行幂等判定 %s: %s",
                claim_id,
                type(exc).__name__,
            )
            existing = None
        created = existing is None
        if created:
            claim = new_claim(
                claim_id=claim_id,
                subject_key=f"learning_insight:{insight.insight_id}",
                content=insight.claim,
                claim_kind="learning_candidate_observation",
                source="learning_system",
                authority="learning_audit_observation",
                metadata={
                    "insight_id": insight.insight_id,
                    "evidence_count": len(insight.evidence),
                    "confidence_as_reported_by_learning_system": insight.confidence,
                    "category": insight.category,
                    "epistemic_note": "audit output and retrieval frequency are not truth",
                },
            )
            await self._memory_service.append_memory_claim(claim)

        for evidence in insight.evidence:
            await self._memory_service.append_claim_evidence(
                ClaimEvidence(
                    evidence_link_id=f"insight_evidence_{evidence.evidence_id}",
                    claim_id=claim_id,
                    evidence_kind=str(evidence.kind or "learning_evidence"),
                    evidence_ref=str(
                        evidence.source_ref
                        or f"learning_evidence:{evidence.evidence_id}"
                    ),
                    stance="supports" if evidence.supports else "challenges",
                    source_excerpt=str(evidence.description or ""),
                    recorded_at=str(evidence.timestamp or ""),
                    metadata={
                        "context": evidence.context,
                        "reported_weight": evidence.weight,
                        "weight_is_not_truth": True,
                    },
                )
            )

        if audit_record is not None:
            await self._memory_service.append_claim_evidence(
                ClaimEvidence(
                    evidence_link_id=f"insight_audit_{audit_record.audit_id}",
                    claim_id=claim_id,
                    evidence_kind="independent_learning_audit",
                    evidence_ref=f"learning_audit:{audit_record.audit_id}",
                    stance="context",
                    source_excerpt=str(audit_record.reasoning or ""),
                    recorded_at=str(audit_record.timestamp or ""),
                    metadata={
                        "verdict": audit_record.verdict,
                        "bias_detected": list(audit_record.bias_detected),
                        "evidence_sufficiency_as_reported": (
                            audit_record.evidence_sufficiency
                        ),
                        "audit_is_not_automatic_truth": True,
                    },
                )
            )
        return created

    async def _maybe_run_distillation(self) -> None:
        """检查是否需要技能蒸馏。"""
        if not self.distiller.should_distill():
            return
        logger.info("🧪 触发技能蒸馏")
        await self.distiller.run_distillation()

    async def _maybe_snapshot_metrics(self) -> None:
        """定期生成学习指标快照。"""
        if not self._should_snapshot_metrics():
            return
        self._snapshot_metrics_now()

    def _snapshot_metrics_now(self) -> None:
        """立刻写一个指标点。

        除了 12 小时的定期快照，晋升 / 压缩这类"状态刚变过"的时刻也要留点。
        否则 metrics.jsonl 会长期显示 validated_count=0、knowledge_version=0，
        而磁盘上其实已经有 validated 洞察和知识文档了——曲线看起来像没在学。
        （她通过工具看到的统计一直是实时读账本的，不受此影响。）
        """
        try:
            if self._selected_persistence is None:
                self.metrics.snapshot()
            else:
                point = self.metrics.build_snapshot()
                self._selected_persistence.queue_audit(
                    "learning_metrics",
                    {"action": "snapshot", **point.to_dict()},
                )
        except Exception as exc:
            if self._selected_persistence is not None:
                raise
            logger.warning("指标快照失败: %s", type(exc).__name__)
            return
        self._last_metrics_at = _now_iso()
        state = self.store.load_state()
        state["last_metrics_at"] = self._last_metrics_at
        self.store.save_state(state)

    async def _maybe_check_staleness(self) -> None:
        """定期观察陈旧洞察（不强制改变）。

        **尊重主体性**：系统只提供观察，不强制遗忘。
        主体在心跳时会看到这些信息，自己决定如何处理。
        """
        if not self._should_check_staleness():
            return

        stale_insights = self.store.get_stale_insights(
            staleness_threshold_days=self._staleness_threshold_days
        )

        if stale_insights:
            # 只记录观察，不改变状态
            logger.info(
                f"📊 观察到 {len(stale_insights)} 条洞察已久未验证 "
                f"(≥{self._staleness_threshold_days}天)"
            )
            # 这些信息会在心跳 prompt 中可见，供主体观察

        self._last_staleness_check_at = _now_iso()
        state = self.store.load_state()
        state["last_staleness_check_at"] = self._last_staleness_check_at
        self.store.save_state(state)

    # ── 调度判断 ─────────────────────────────────────────────

    def _should_audit(self) -> bool:
        """是否应该执行审计。"""
        # 有待审候选
        candidates = self.store.list_candidates_for_review()
        if not candidates:
            return False
        # 时间间隔
        state = self.store.load_state()
        last_audit = state.get("last_audit_at", "")
        if not last_audit:
            return True
        try:
            last_dt = datetime.fromisoformat(last_audit)
            now = datetime.now(UTC).astimezone()
            hours_elapsed = (now - last_dt).total_seconds() / 3600.0
            return hours_elapsed >= self._audit_interval_hours
        except (ValueError, TypeError):
            return True

    def _should_snapshot_metrics(self) -> bool:
        """是否应该生成指标快照。"""
        state = self.store.load_state()
        last_metrics = state.get("last_metrics_at", "")
        if not last_metrics:
            return True
        try:
            last_dt = datetime.fromisoformat(last_metrics)
            now = datetime.now(UTC).astimezone()
            hours_elapsed = (now - last_dt).total_seconds() / 3600.0
            return hours_elapsed >= self._metrics_interval_hours
        except (ValueError, TypeError):
            return True

    def _should_check_staleness(self) -> bool:
        """是否应该检查陈旧洞察。"""
        state = self.store.load_state()
        last_check = state.get("last_staleness_check_at", "")
        if not last_check:
            return True
        try:
            last_dt = datetime.fromisoformat(last_check)
            now = datetime.now(UTC).astimezone()
            hours_elapsed = (now - last_dt).total_seconds() / 3600.0
            return hours_elapsed >= self._staleness_check_interval_hours
        except (ValueError, TypeError):
            return True

    # ── 状态 ─────────────────────────────────────────────────

    def _subject_review_health_snapshot(self) -> dict[str, Any]:
        try:
            _, review = self._subject_review_state()
            documents = review["documents"]
            assert isinstance(documents, dict)
            if self._read_subject_authority is not None:
                now = datetime.now(UTC)
                snapshots: list[dict[str, Any]] = []
                for path in SUBJECT_AUTHORITY_PATHS:
                    raw = documents.get(path)
                    record = dict(raw) if isinstance(raw, dict) else {}
                    observed_exists = record.get("last_observed_exists")
                    observation_known = isinstance(observed_exists, bool)
                    exists = bool(observed_exists) if observation_known else False
                    baseline = self._parse_review_time(record.get("review_baseline_at"))
                    last_reviewed = self._parse_review_time(
                        record.get("last_reviewed_at")
                    )
                    effective_baseline = max(
                        (
                            item
                            for item in (baseline, last_reviewed)
                            if item is not None
                        ),
                        default=None,
                    )
                    due_at = (
                        effective_baseline
                        + timedelta(hours=self._subject_review_intervals[path])
                        if effective_baseline is not None
                        else None
                    )
                    snooze_until = self._parse_review_time(record.get("snooze_until"))
                    last_offered = self._parse_review_time(
                        record.get("last_offered_at")
                    )
                    offer_after = (
                        last_offered
                        + timedelta(hours=self._subject_review_offer_cooldown_hours)
                        if last_offered is not None
                        else None
                    )
                    size_bytes = max(
                        0,
                        int(record.get("last_observed_size_bytes") or 0),
                    )
                    pressure = self._continuity_memory_pressure(
                        path=path,
                        size_bytes=size_bytes,
                        record=record,
                    )
                    index_review = {
                        "continuity_index_entry_count": int(
                            record.get("continuity_index_entry_count") or 0
                        ),
                        "continuity_index_issue_count": int(
                            record.get("continuity_index_issue_count") or 0
                        ),
                        "continuity_index_absent": bool(
                            record.get("continuity_index_absent", False)
                        ),
                        "continuity_index_review_due": bool(
                            record.get("continuity_index_review_due", False)
                        ),
                        "continuity_index_state": str(
                            record.get("continuity_index_state") or "unobserved"
                        ),
                        "continuity_index_semantics": str(
                            record.get("continuity_index_semantics")
                            or "structural_review_only"
                        ),
                    }
                    interval_due = bool(due_at is not None and now >= due_at)
                    pressure_due = bool(pressure["review_pressure_due"])
                    index_review_due = bool(index_review["continuity_index_review_due"])
                    pending_candidate_due = bool(
                        str(record.get("last_outcome") or "").strip().lower()
                        == "candidate_proposed"
                        and str(record.get("last_candidate_id") or "").strip()
                    )
                    snapshots.append(
                        {
                            "target_path": path,
                            "exists": exists,
                            "observation_known": observation_known,
                            "size_bytes": size_bytes,
                            "content_sha256": str(
                                record.get("review_baseline_content_sha256") or ""
                            ),
                            "version_id": str(
                                record.get("last_observed_version_id") or ""
                            ),
                            "source_occurrence_id": str(
                                record.get("last_observed_source_occurrence_id") or ""
                            ),
                            "source_provenance_status": str(
                                record.get("last_observed_source_provenance_status")
                                or ""
                            ),
                            "review_baseline_at": str(
                                record.get("review_baseline_at") or ""
                            ),
                            "due_at": due_at.isoformat() if due_at else "",
                            "due": bool(
                                self._subject_review_enabled
                                and observation_known
                                and exists
                                and (
                                    interval_due
                                    or pressure_due
                                    or index_review_due
                                    or pending_candidate_due
                                )
                                and (snooze_until is None or now >= snooze_until)
                                and (offer_after is None or now >= offer_after)
                            ),
                            "due_reasons": [
                                reason
                                for reason, active in (
                                    ("interval", interval_due),
                                    ("engineering_pressure", pressure_due),
                                    (
                                        "continuity_index_review",
                                        index_review_due,
                                    ),
                                    (
                                        "candidate_decision_pending",
                                        pending_candidate_due,
                                    ),
                                )
                                if active
                            ],
                            **pressure,
                            **index_review,
                            "last_reviewed_at": str(
                                record.get("last_reviewed_at") or ""
                            ),
                            "last_outcome": str(record.get("last_outcome") or ""),
                        }
                    )
                pending = sum(
                    str(item.get("last_outcome") or "")
                    in {"candidate_proposed", "kept_open"}
                    for item in snapshots
                )
                due = sum(bool(item["due"]) for item in snapshots)
                missing = sum(
                    bool(item["observation_known"] and not item["exists"])
                    for item in snapshots
                )
                unobserved = sum(
                    not bool(item["observation_known"]) for item in snapshots
                )
                return {
                    "status": (
                        "disabled"
                        if not self._subject_review_enabled
                        else "degraded"
                        if missing
                        else "healthy"
                    ),
                    "authority_status": (
                        "selected_ready"
                        if self.decision_ledger is not None
                        else "migration_required"
                    ),
                    "source": "selected_subject_authority",
                    "direct_mutation_blocked": True,
                    "due_count": due,
                    "due_count_known": unobserved == 0,
                    "pending_candidate_count": pending,
                    "missing_count": missing,
                    "unobserved_count": unobserved,
                    "last_observed_subject_revision": str(
                        review.get("last_observed_subject_revision") or ""
                    ),
                    "last_observed_at": str(review.get("last_observed_at") or ""),
                    "documents": snapshots,
                }
            now = datetime.now(UTC)
            snapshots: list[dict[str, Any]] = []
            for path in SUBJECT_AUTHORITY_PATHS:
                raw = documents.get(path)
                record = dict(raw) if isinstance(raw, dict) else {}
                target = self._workspace / path
                exists = target.exists() and target.is_file()
                changed_at: datetime | None = None
                content = b""
                size_bytes = 0
                content_sha256 = ""
                version_id = ""
                if exists:
                    stat = target.stat()
                    changed_at = datetime.fromtimestamp(stat.st_mtime, tz=UTC)
                    content = target.read_bytes()
                    size_bytes = len(content)
                    content_sha256 = hashlib.sha256(content).hexdigest()
                    version_id = (
                        f"workspace-{Path(path).stem.lower()}-sha256:{content_sha256}"
                    )
                last_reviewed = self._parse_review_time(record.get("last_reviewed_at"))
                baseline = max(
                    (item for item in (changed_at, last_reviewed) if item is not None),
                    default=now,
                )
                due_at = baseline + timedelta(
                    hours=self._subject_review_intervals[path]
                )
                snooze_until = self._parse_review_time(record.get("snooze_until"))
                last_offered = self._parse_review_time(record.get("last_offered_at"))
                offer_after = (
                    last_offered
                    + timedelta(hours=self._subject_review_offer_cooldown_hours)
                    if last_offered is not None
                    else None
                )
                pressure = self._continuity_memory_pressure(
                    path=path,
                    size_bytes=size_bytes,
                    record=record,
                )
                index_review = self._continuity_memory_index_review(
                    path=path,
                    content=content,
                    version_id=version_id,
                    subject_revision=str(
                        review.get("last_observed_subject_revision") or ""
                    ),
                    record=record,
                )
                interval_due = now >= due_at
                pressure_due = bool(pressure["review_pressure_due"])
                index_review_due = bool(index_review["continuity_index_review_due"])
                pending_candidate_due = bool(
                    str(record.get("last_outcome") or "").strip().lower()
                    == "candidate_proposed"
                    and str(record.get("last_candidate_id") or "").strip()
                )
                snapshot = {
                    "target_path": path,
                    "exists": exists,
                    "size_bytes": size_bytes,
                    "content_sha256": content_sha256,
                    "version_id": version_id,
                    "source_occurrence_id": (
                        f"workspace-observation:{content_sha256}"
                        if content_sha256
                        else ""
                    ),
                    "source_provenance_status": (
                        "local_workspace_observation" if exists else ""
                    ),
                    "due": bool(
                        self._subject_review_enabled
                        and exists
                        and (
                            interval_due
                            or pressure_due
                            or index_review_due
                            or pending_candidate_due
                        )
                        and (snooze_until is None or now >= snooze_until)
                        and (offer_after is None or now >= offer_after)
                    ),
                    "due_reasons": [
                        reason
                        for reason, active in (
                            ("interval", interval_due),
                            ("engineering_pressure", pressure_due),
                            ("continuity_index_review", index_review_due),
                            (
                                "candidate_decision_pending",
                                pending_candidate_due,
                            ),
                        )
                        if active
                    ],
                    **pressure,
                    **index_review,
                    "last_outcome": str(record.get("last_outcome") or ""),
                }
                snapshots.append(snapshot)
            missing = sum(not bool(item["exists"]) for item in snapshots)
            due = sum(bool(item["due"]) for item in snapshots)
            pending = sum(
                str(item.get("last_outcome") or "")
                in {"candidate_proposed", "kept_open"}
                for item in snapshots
            )
            status = (
                "disabled"
                if not self._subject_review_enabled
                else "degraded"
                if missing
                else "healthy"
            )
            return {
                "status": status,
                "authority_status": (
                    "selected_ready"
                    if self.decision_ledger is not None
                    else "migration_required"
                ),
                "direct_mutation_blocked": True,
                "due_count": due,
                "due_count_known": True,
                "pending_candidate_count": pending,
                "missing_count": missing,
                "last_observed_subject_revision": str(
                    review.get("last_observed_subject_revision") or ""
                ),
                "last_observed_at": str(review.get("last_observed_at") or ""),
                "documents": snapshots,
            }
        except Exception as exc:  # noqa: BLE001 - health must not raise
            return {
                "status": "degraded",
                "authority_status": (
                    "selected_ready"
                    if self.decision_ledger is not None
                    else "migration_required"
                ),
                "direct_mutation_blocked": True,
                "due_count": None,
                "due_count_known": False,
                "pending_candidate_count": 0,
                "missing_count": 0,
                "error_type": type(exc).__name__,
                "documents": [],
            }

    def get_state(self) -> dict[str, Any]:
        """获取学习系统当前状态。"""
        if self._projector_quiesced:
            return {
                "status": "degraded",
                "mode": "event_only",
                "projector_owner": False,
                "event_append_available": self._learning_event_store is not None,
                "reason": self._projector_quiesce_reason,
                "error_type": self._projector_quiesce_error_type,
                "quiesced_at": self._projector_quiesced_at,
                "reflection_available": False,
                "maintenance": {
                    "status": "disabled",
                    "reason": "singleton projector ownership was lost",
                },
                "worker": {"status": "disabled", "running": False},
                "selected_persistence": {
                    "status": "disabled",
                    "projector_owner": False,
                },
                "prompt_projections": {
                    "status": "disabled",
                    "reason": "stale projections are not exposed after owner loss",
                },
            }
        stats = self.store.get_stats()
        state = self.store.load_state()
        manifest = self.store.load_knowledge_manifest()
        skill_candidates = self.skill_store.list_candidates()
        maintenance_health = self.maintenance_journal.health_snapshot()
        selected_health = (
            self._selected_persistence.health_snapshot()
            if self._selected_persistence is not None
            else {"status": "disabled", "backend": "legacy_local"}
        )
        reflection_health = reflection_queue_health(
            load_reflection_jobs(state),
            runtime_state=self._reflection_runtime_state(state),
            cooldown_minutes=self._reflection_cooldown_minutes,
        )
        subject_review_health = self._subject_review_health_snapshot()
        worker_health = {
            "status": (
                "healthy"
                if self._worker_running
                else "degraded"
                if self._worker_last_started_at
                else "initializing"
            ),
            "running": self._worker_running,
            "last_started_at": self._worker_last_started_at,
            "last_completed_at": self._worker_last_completed_at,
            "last_error_type": self._worker_last_error_type,
        }
        component_statuses = {
            str(maintenance_health.get("status") or "healthy"),
            str(selected_health.get("status") or "healthy"),
            str(reflection_health.get("status") or "healthy"),
            str(subject_review_health.get("status") or "healthy"),
            str(worker_health.get("status") or "healthy"),
        }
        if "failed" in component_statuses:
            learning_status = "failed"
        elif "degraded" in component_statuses:
            learning_status = "degraded"
        elif "initializing" in component_statuses:
            learning_status = "initializing"
        else:
            learning_status = "healthy"
        return {
            "status": learning_status,
            "mode": "projector",
            "projector_owner": True,
            "event_append_available": self._learning_event_store is not None,
            "insights": stats,
            "knowledge_version": manifest.get("current_version", 0),
            "last_audit_at": state.get("last_audit_at", ""),
            "last_compress_at": state.get("last_compress_at", ""),
            "last_metrics_at": state.get("last_metrics_at", ""),
            "reflection_available": self.reflection.can_reflect,
            "reflection_queue": reflection_health,
            "skill_candidates": {
                "open": sum(item.status == "open" for item in skill_candidates),
                "accepted": sum(item.status == "accepted" for item in skill_candidates),
                "rejected": sum(item.status == "rejected" for item in skill_candidates),
                "gate_recommended": sum(
                    item.gate_recommended for item in skill_candidates
                ),
            },
            "maintenance": maintenance_health,
            "subject_review": subject_review_health,
            "worker": worker_health,
            "selected_persistence": selected_health,
            "prompt_projections": {
                "knowledge": self.compressor.projection_health(),
                "skills": self.skill_store.catalog_projection_health(),
                "reflection": self.reflection.projection_health(),
                "audit": self.auditor.projection_health(),
                "distillation": self.distiller.projection_health(),
            },
        }

    def get_knowledge_for_prompt(self, max_chars: int = 0) -> str:
        """获取自我认知文档（供 prompt 注入）。"""
        if self._projector_quiesced:
            return ""
        return self.compressor.get_knowledge_for_prompt(max_chars=max_chars)

    def get_skill_catalog_for_prompt(self, max_chars: int = 0) -> str:
        """获取技能目录文本（L1，供 prompt 注入）。"""
        if self._projector_quiesced:
            return ""
        return self.skill_store.get_catalog_text(max_chars=max_chars)

    def get_progress_for_prompt(self) -> str:
        """获取学习进展（供 prompt 注入）。"""
        if self._projector_quiesced:
            return ""
        return self.metrics.format_progress_for_prompt()


def _now_iso() -> str:
    return datetime.now(UTC).astimezone().isoformat()
