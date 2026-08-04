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
import logging
from collections.abc import Awaitable, Callable
from contextlib import nullcontext
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from ..storage.learning_contracts import LearningStorePort
from ..storage.subject_contracts import (
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

logger = logging.getLogger("life_engine.learning.scheduler")

# 默认调度参数
_DEFAULT_AUDIT_INTERVAL_HOURS = 6.0
_DEFAULT_AUDIT_BATCH_SIZE = 3
_DEFAULT_COMPRESS_TRIGGER_COUNT = 5
_DEFAULT_COMPRESS_INTERVAL_HOURS = 48.0
_DEFAULT_REFLECTION_COOLDOWN_MINUTES = 30.0
_DEFAULT_METRICS_INTERVAL_HOURS = 12.0
_DEFAULT_SKILL_DISTILL_TRIGGER_COUNT = 3
_DEFAULT_SKILL_DISTILL_INTERVAL_HOURS = 24.0
_DEFAULT_STALENESS_CHECK_INTERVAL_HOURS = 168.0  # 每周检查一次
_DEFAULT_STALENESS_THRESHOLD_DAYS = 90


class LearningScheduler:
    """三环自学习调度协调器。

    集成入口：由 life_engine 心跳或事件触发调用。
    """

    def __init__(
        self,
        *,
        workspace_path: str | Path,
        model_task_name: str = "life",
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
        # 记忆服务（用于把"修正型洞察"落成显式修正记录，形成记忆演化链）
        memory_service: Any | None = None,
        maintenance_journal: LearningMaintenanceJournalPort | None = None,
        learning_store: LearningStorePort | None = None,
        subject_authority: SubjectAuthorityPort | None = None,
        project_subject_commit: (
            Callable[[SubjectDocumentPath, SubjectAuthorityCommit], Awaitable[None]]
            | None
        ) = None,
        current_subject_revision: Callable[[], Awaitable[str]] | None = None,
        validate_active_consciousness_instance: (
            Callable[[str], Awaitable[bool]] | None
        ) = None,
    ) -> None:
        self._workspace = Path(workspace_path).resolve()
        self._model_task_name = model_task_name
        self._memory_service = memory_service
        self._current_subject_revision = current_subject_revision or (
            subject_authority.current_subject_revision
            if subject_authority is not None
            else None
        )
        self._validate_active_consciousness_instance = (
            validate_active_consciousness_instance
        )

        # 初始化核心组件
        self._selected_persistence: SelectedLearningPersistence | None = None
        self.decision_ledger: LearningDecisionLedger | None = None
        if learning_store is None:
            self.store = InsightStore(self._workspace)
            self.skill_store = SkillStore(self._workspace)
        else:
            persistence = SelectedLearningPersistence(learning_store)
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
            cooldown_seconds=reflection_cooldown_minutes * 60,
            skill_store=self.skill_store,
            memory_service=memory_service,
        )
        self.auditor = InsightAuditor(
            store=self.store,
            workspace_path=self._workspace,
            model_task_name=model_task_name,
            batch_size=audit_batch_size,
        )
        self.compressor = SelfKnowledgeCompressor(
            store=self.store,
            workspace_path=self._workspace,
            model_task_name=model_task_name,
            trigger_count=compress_trigger_count,
            interval_hours=compress_interval_hours,
        )
        self.distiller = SkillDistiller(
            store=self.store,
            skill_store=self.skill_store,
            workspace_path=self._workspace,
            model_task_name=model_task_name,
            trigger_count=skill_distill_trigger_count,
            interval_hours=skill_distill_interval_hours,
            current_subject_revision=self._current_subject_revision,
        )
        self.metrics = LearningMetrics(store=self.store)
        if maintenance_journal is not None:
            self.maintenance_journal = maintenance_journal
        elif learning_store is not None:
            self.maintenance_journal = SelectedLearningMaintenanceJournal(
                learning_store
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

        self._running = False
        self._last_audit_at: str = ""
        self._last_metrics_at: str = ""
        self._last_staleness_check_at: str = ""
        self._epistemic_backfilled = False
        self._maintenance_lock = asyncio.Lock()
        self._reflection_queue_lock = asyncio.Lock()

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

        if self._selected_persistence is not None:
            await self._selected_persistence.close()

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

    def _save_reflection_jobs(self, jobs: list[LearningReflectionJob]) -> None:
        state = self.store.load_state()
        state[REFLECTION_QUEUE_STATE_KEY] = [job.to_dict() for job in jobs]
        self.store.save_state(state)

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

        ``None`` means the request remains queued because the engine is cooling
        down. An empty list means the request ran successfully and produced no
        new insight. Failures raise only after the retry envelope is durable.
        """

        job = LearningReflectionJob.create(
            reflection_kind=reflection_kind,
            reflection_text=reflection_text,
            context=context,
            source_event_ids=source_event_ids,
            actor_consciousness_instance_id=actor_consciousness_instance_id,
        )
        async with self._reflection_queue_lock:
            jobs = self._reflection_jobs()
            if len(jobs) >= MAX_PENDING_REFLECTIONS:
                raise RuntimeError("LearningReflectionQueueFull")
            jobs.append(job)
            self._save_reflection_jobs(jobs)
            await self.flush()
        result = await self._run_pending_reflection(preferred_job_id=job.job_id)
        if result is None:
            return None
        _, insights = result
        return insights

    def _reflection_work_due(self) -> bool:
        return self.reflection.can_reflect and any(
            job.due() for job in self._reflection_jobs()
        )

    def _reflection_pending_count(self) -> int:
        return len(self._reflection_jobs())

    async def _run_pending_reflection_phase(self) -> None:
        await self._run_pending_reflection()

    async def _run_pending_reflection(
        self,
        *,
        preferred_job_id: str = "",
    ) -> tuple[str, list[Any]] | None:
        async with self._reflection_queue_lock:
            jobs = self._reflection_jobs()
            if not jobs or not self.reflection.can_reflect:
                return None
            due = [job for job in jobs if job.due()]
            if not due:
                return None
            job = next(
                (item for item in due if item.job_id == preferred_job_id),
                due[0],
            )
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
                jobs[jobs.index(job)] = job.failed(exc)
                self._save_reflection_jobs(jobs)
                await self.flush()
                raise
            jobs.remove(job)
            self._save_reflection_jobs(jobs)
            await self.flush()
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
            insights = await self.submit_reflection(
                reflection_kind="interaction",
                reflection_text=interaction_text,
                context=context,
                source_event_ids=source_event_ids,
                actor_consciousness_instance_id=actor_consciousness_instance_id,
            )
            if insights:
                logger.info(f"交互反思产生 {len(insights)} 条洞察")
            await self.on_heartbeat()
        except Exception as exc:
            logger.warning("交互反思异常: %s", type(exc).__name__)

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
            insights = await self.submit_reflection(
                reflection_kind="introspection",
                reflection_text=thought_summary,
                context=context,
                source_event_ids=source_event_ids,
                actor_consciousness_instance_id=actor_consciousness_instance_id,
            )
            if insights:
                logger.info(f"思考闭合反思产生 {len(insights)} 条洞察")
            await self.on_heartbeat()
        except Exception as exc:
            logger.warning("思考闭合反思异常: %s", type(exc).__name__)

    # ── 心跳驱动入口 ─────────────────────────────────────────

    async def on_heartbeat(self) -> None:
        """心跳触发：检查是否需要执行审计/压缩/蒸馏/指标快照/陈旧检查。

        由 life_engine 心跳周期调用（低频，不必每次心跳都调用）。
        """
        async with self._maintenance_lock:
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
        ledgered = int(
            state.get("last_knowledge_candidate_ledgered_version", 0) or 0
        )
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

    def get_state(self) -> dict[str, Any]:
        """获取学习系统当前状态。"""
        stats = self.store.get_stats()
        state = self.store.load_state()
        manifest = self.store.load_knowledge_manifest()
        skill_candidates = self.skill_store.list_candidates()
        return {
            "insights": stats,
            "knowledge_version": manifest.get("current_version", 0),
            "last_audit_at": state.get("last_audit_at", ""),
            "last_compress_at": state.get("last_compress_at", ""),
            "last_metrics_at": state.get("last_metrics_at", ""),
            "reflection_available": self.reflection.can_reflect,
            "reflection_queue": reflection_queue_health(
                load_reflection_jobs(state)
            ),
            "skill_candidates": {
                "open": sum(item.status == "open" for item in skill_candidates),
                "accepted": sum(item.status == "accepted" for item in skill_candidates),
                "rejected": sum(item.status == "rejected" for item in skill_candidates),
                "gate_recommended": sum(
                    item.gate_recommended for item in skill_candidates
                ),
            },
            "maintenance": self.maintenance_journal.health_snapshot(),
            "selected_persistence": (
                self._selected_persistence.health_snapshot()
                if self._selected_persistence is not None
                else {"status": "disabled", "backend": "legacy_local"}
            ),
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
        return self.compressor.get_knowledge_for_prompt(max_chars=max_chars)

    def get_skill_catalog_for_prompt(self, max_chars: int = 0) -> str:
        """获取技能目录文本（L1，供 prompt 注入）。"""
        return self.skill_store.get_catalog_text(max_chars=max_chars)

    def get_progress_for_prompt(self) -> str:
        """获取学习进展（供 prompt 注入）。"""
        return self.metrics.format_progress_for_prompt()


def _now_iso() -> str:
    return datetime.now(UTC).astimezone().isoformat()
