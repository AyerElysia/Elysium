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
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .auditor import InsightAuditor
from .knowledge import SelfKnowledgeCompressor
from .metrics import LearningMetrics
from .reflection import ReflectionEngine
from .skill_distiller import SkillDistiller
from .skill_store import SkillStore
from .store import InsightStore
from ..minecraft.consciousness import MinecraftSession
from ..minecraft.launcher import MCConfig

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
        # Minecraft 参数
        minecraft_enabled: bool = False,
        minecraft_config: dict[str, Any] | None = None,
        # 记忆服务（用于把"修正型洞察"落成显式修正记录，形成记忆演化链）
        memory_service: Any | None = None,
    ) -> None:
        self._workspace = Path(workspace_path).resolve()
        self._model_task_name = model_task_name
        self._memory_service = memory_service

        # 初始化核心组件
        self.store = InsightStore(self._workspace)
        self.skill_store = SkillStore(self._workspace)
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
        )
        self.metrics = LearningMetrics(store=self.store)

        # Minecraft 具身体验
        self.minecraft_session: MinecraftSession | None = None
        if minecraft_enabled:
            # 过滤掉非 MCConfig 字段（如 vla_model）
            _mc_fields = {f.name for f in MCConfig.__dataclass_fields__.values()} if hasattr(MCConfig, "__dataclass_fields__") else set()
            _mc_raw = minecraft_config or {}
            _mc_kwargs = {k: v for k, v in _mc_raw.items() if not _mc_fields or k in _mc_fields}
            mc_cfg = MCConfig(**_mc_kwargs)

            self.minecraft_session = MinecraftSession(
                workspace=self._workspace,
                mc_config=mc_cfg,
                llm_helper=None,  # TODO: 可选注入 LLM 辅助意图解析
            )
            logger.info("Minecraft 具身体验已初始化（对话式控制）")

        # 调度参数
        self._audit_interval_hours = max(1.0, audit_interval_hours)
        self._metrics_interval_hours = max(1.0, metrics_interval_hours)
        self._staleness_check_interval_hours = max(24.0, staleness_check_interval_hours)
        self._staleness_threshold_days = max(30, staleness_threshold_days)

        self._running = False
        self._last_audit_at: str = ""
        self._last_metrics_at: str = ""
        self._last_staleness_check_at: str = ""

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

    # ── 事件驱动入口 ─────────────────────────────────────────

    async def on_interaction_end(
        self,
        *,
        interaction_text: str,
        context: str = "",
        source_event_ids: list[str] | None = None,
    ) -> None:
        """交互结束事件：触发快环反思。"""
        try:
            insights = await self.reflection.reflect_on_interaction(
                interaction_text=interaction_text,
                context=context,
                source_event_ids=source_event_ids,
            )
            if insights:
                logger.info(f"交互反思产生 {len(insights)} 条洞察")
                # 反思后检查是否需要触发审计
                await self._maybe_run_audit()
        except Exception as exc:
            logger.warning(f"交互反思异常: {exc}")

    async def on_thought_closed(
        self,
        *,
        thought_summary: str,
        context: str = "",
        source_event_ids: list[str] | None = None,
    ) -> None:
        """思考流闭合事件：触发内省反思。"""
        try:
            insights = await self.reflection.reflect_on_internal(
                internal_text=thought_summary,
                context=context,
                source_event_ids=source_event_ids,
            )
            if insights:
                logger.info(f"思考闭合反思产生 {len(insights)} 条洞察")
        except Exception as exc:
            logger.warning(f"思考闭合反思异常: {exc}")

    # ── 心跳驱动入口 ─────────────────────────────────────────

    async def on_heartbeat(self) -> None:
        """心跳触发：检查是否需要执行审计/压缩/蒸馏/指标快照/陈旧检查。

        由 life_engine 心跳周期调用（低频，不必每次心跳都调用）。
        """
        try:
            await self._maybe_run_audit()
            await self._maybe_run_compression()
            await self._maybe_run_distillation()
            await self._maybe_snapshot_metrics()
            await self._maybe_check_staleness()
        except Exception as exc:
            logger.warning(f"学习调度心跳异常: {exc}")

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
            # 审计后检查是否需要压缩
            await self._maybe_run_compression()

    async def _maybe_run_compression(self) -> None:
        """检查是否需要压缩。"""
        if not self.compressor.should_compress():
            return
        logger.info("📝 触发慢环压缩")
        await self.compressor.run_compression()

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
        self.metrics.snapshot()
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
            now = datetime.now(timezone.utc).astimezone()
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
            now = datetime.now(timezone.utc).astimezone()
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
            now = datetime.now(timezone.utc).astimezone()
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
        return {
            "insights": stats,
            "knowledge_version": manifest.get("current_version", 0),
            "last_audit_at": state.get("last_audit_at", ""),
            "last_compress_at": state.get("last_compress_at", ""),
            "last_metrics_at": state.get("last_metrics_at", ""),
            "reflection_available": self.reflection.can_reflect,
        }

    def get_knowledge_for_prompt(self, max_chars: int = 2000) -> str:
        """获取自我认知文档（供 prompt 注入）。"""
        return self.compressor.get_knowledge_for_prompt(max_chars=max_chars)

    def get_skill_catalog_for_prompt(self, max_chars: int = 600) -> str:
        """获取技能目录文本（L1，供 prompt 注入）。"""
        return self.skill_store.get_catalog_text(max_chars=max_chars)

    def get_progress_for_prompt(self) -> str:
        """获取学习进展（供 prompt 注入）。"""
        return self.metrics.format_progress_for_prompt()


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()
