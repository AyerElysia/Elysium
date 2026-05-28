"""做梦调度与执行引擎。

包含 DreamScheduler 类主体、DreamPhase 枚举，
负责协调 NREM 回放、种子收集、REM 联想、场景生成等阶段。
"""

from __future__ import annotations

import logging
import time
import uuid
from collections import deque
from enum import Enum
from pathlib import Path
from typing import Any, TYPE_CHECKING

import numpy as np

from .residue import (
    NREMReport,
    REMReport,
    DreamReport,
    DreamResidue,
    _report_to_dict,
    _report_from_dict,
    _residue_to_dict,
    _residue_from_payload,
    archive_dream,
    integrate_archive_into_memory,
    _REMINDER_MAX_HISTORY,
    _RESIDUE_TTL_SECONDS,
)
from .seeds import (
    DreamSeed,
    DreamSeedType,
    load_memory_candidates,
    collect_day_residue,
    collect_unfinished_tension,
    collect_dream_lag,
    collect_self_theme,
    collect_thought_stream_seeds,
    select_seed_candidates,
    collect_seed_node_ids,
    _event_type_value,
    _event_value,
)
from .scenes import (
    DreamTrace,
    build_dream_scene,
    build_recent_context_summary,
    build_reference_previews,
)

if TYPE_CHECKING:
    from ..memory.service import LifeMemoryService
    from ..neuromod.engine import InnerStateEngine
    from ..snn.bridge import SNNBridge
    from ..snn.core import DriveCoreNetwork


logger = logging.getLogger("life_engine.dream")

# 常量
_DREAM_HISTORY_WINDOW = 5
_REPETITION_DECAY = 0.3


class DreamPhase(str, Enum):
    """做梦阶段。"""

    AWAKE = "awake"
    NREM = "nrem"
    REM = "rem"
    WAKING_UP = "waking_up"


class DreamScheduler:
    """做梦调度与执行引擎。"""

    def __init__(
        self,
        *,
        snn: DriveCoreNetwork | None = None,
        inner_state: InnerStateEngine | None = None,
        memory_service: LifeMemoryService | None = None,
        snn_bridge: SNNBridge | None = None,
        workspace_path: str | Path | None = None,
        model_task_name: str = "life",
        # NREM 参数
        nrem_replay_episodes: int = 3,
        nrem_events_per_episode: int = 20,
        nrem_speed_multiplier: float = 5.0,
        nrem_homeostatic_rate: float = 0.02,
        # REM 参数
        rem_walk_rounds: int = 2,
        rem_seeds_per_round: int = 5,
        rem_max_depth: int = 3,
        rem_decay_factor: float = 0.6,
        rem_learning_rate: float = 0.05,
        rem_edge_prune_threshold: float = 0.08,
        # 调度参数
        dream_interval_minutes: int = 90,
        idle_trigger_heartbeats: int = 10,
        nap_enabled: bool = True,
    ) -> None:
        self._snn = snn
        self._inner_state = inner_state
        self._memory = memory_service
        self._snn_bridge = snn_bridge
        self._workspace = Path(workspace_path).resolve() if workspace_path else None
        self._model_task_name = str(model_task_name or "life").strip() or "life"

        self._nrem_replay_episodes = nrem_replay_episodes
        self._nrem_events_per_episode = nrem_events_per_episode
        self._nrem_speed_multiplier = nrem_speed_multiplier
        self._nrem_homeostatic_rate = nrem_homeostatic_rate

        self._rem_walk_rounds = rem_walk_rounds
        self._rem_seeds_per_round = rem_seeds_per_round
        self._rem_max_depth = rem_max_depth
        self._rem_decay_factor = rem_decay_factor
        self._rem_learning_rate = rem_learning_rate
        self._rem_edge_prune_threshold = rem_edge_prune_threshold

        self._dream_interval_seconds = dream_interval_minutes * 60
        self._idle_trigger_heartbeats = idle_trigger_heartbeats
        self._nap_enabled = nap_enabled

        self._current_phase = DreamPhase.AWAKE
        self._dream_history: list[DreamReport] = []
        self._last_dream_time: float = 0.0
        self._is_dreaming: bool = False
        self._active_residue: DreamResidue | None = None
        self._last_archive_path: str = ""

        # 仿生状态：海马体重复抑制 + REM 渐变 + 梦境避重
        self._recent_seed_titles: deque[set[str]] = deque(maxlen=_DREAM_HISTORY_WINDOW)
        self._recent_dream_summaries: deque[str] = deque(maxlen=_DREAM_HISTORY_WINDOW)
        self._dreams_since_sleep_start: int = 0

    @property
    def is_dreaming(self) -> bool:
        return self._is_dreaming

    @property
    def current_phase(self) -> DreamPhase:
        return self._current_phase

    def should_dream(self, idle_heartbeat_count: int, in_sleep_window: bool) -> bool:
        """判断是否应该开始做梦。"""
        if self._is_dreaming:
            return False

        now = time.time()
        if now - self._last_dream_time < self._dream_interval_seconds:
            return False

        if in_sleep_window:
            return True

        if self._nap_enabled and idle_heartbeat_count >= self._idle_trigger_heartbeats:
            return True

        return False

    async def run_dream_cycle(
        self,
        event_history: list[Any],
    ) -> DreamReport:
        """执行完整做梦周期：NREM → DreamSeed → REM → DreamScene → Wake。"""
        from .residue import _seed_to_dict

        self._is_dreaming = True
        report = DreamReport(
            dream_id=str(uuid.uuid4())[:8],
            started_at=time.time(),
        )

        try:
            # NREM 阶段
            self._current_phase = DreamPhase.NREM
            self._emit_visual_event(
                "dream.phase_change", {"phase": "nrem", "dream_id": report.dream_id}
            )
            report.phase_sequence.append("nrem")
            logger.info(f"🌙 Dream [{report.dream_id}] NREM phase started")

            if self._snn is not None and self._snn_bridge is not None:
                report.nrem = await self._run_nrem(event_history)
                logger.info(
                    f"🌙 Dream [{report.dream_id}] NREM phase completed: "
                    f"{report.nrem.episodes_replayed if report.nrem else 0} episodes replayed"
                )

            # 梦种子生成
            report.seed_report = await self._generate_dream_seeds(event_history)
            logger.info(
                f"🌙 Dream [{report.dream_id}] Seeds generated: {len(report.seed_report)}"
            )
            self._emit_visual_event(
                "dream.seeds_extracted",
                {"seeds": [_seed_to_dict(s) for s in report.seed_report]},
            )

            # REM 阶段
            self._current_phase = DreamPhase.REM
            self._emit_visual_event(
                "dream.phase_change", {"phase": "rem", "dream_id": report.dream_id}
            )
            report.phase_sequence.append("rem")
            logger.info(f"🌙 Dream [{report.dream_id}] REM phase started")

            seed_node_ids = collect_seed_node_ids(report.seed_report)
            if self._memory is not None:
                report.rem = await self._run_rem(seed_node_ids)
                logger.info(
                    f"🌙 Dream [{report.dream_id}] REM phase completed: "
                    f"{report.rem.nodes_activated} nodes activated, "
                    f"{report.rem.new_edges_created} new edges created"
                )

            self._emit_visual_event(
                "dream.rem_stats",
                {
                    "nodes_activated": report.rem.nodes_activated,
                    "new_edges_created": report.rem.new_edges_created,
                "edges_pruned": report.rem.edges_pruned
            })

            result = await self._build_dream_scene(
                seeds=report.seed_report,
                rem_report=report.rem,
                event_history=event_history,
            )

            if result is None:
                report.dream_text = ""
                report.narrative = ""
                logger.info(f"🌙 Dream [{report.dream_id}] 本次未形成清晰梦境")
            else:
                trace, dream_text, residue = result
                report.dream_trace = trace
                report.dream_text = dream_text
                report.narrative = dream_text
                report.dream_residue = residue
                if residue and residue.summary:
                    self._recent_dream_summaries.append(residue.summary)

                if self._workspace is not None:
                    report.archive_path = await archive_dream(
                        report,
                        self._workspace,
                        report.dream_trace,
                    )
                    self._last_archive_path = report.archive_path
                report.memory_effects = await integrate_archive_into_memory(
                    report,
                    self._workspace,
                    self._memory,
                    report.seed_report,
                )

            self._current_phase = DreamPhase.WAKING_UP
            self._emit_visual_event("dream.phase_change", {"phase": "waking_up", "dream_id": report.dream_id})
            report.phase_sequence.append("waking_up")
            logger.info(f"🌙 Dream [{report.dream_id}] 觉醒过渡")

            if report.dream_residue is not None:
                report.dream_residue.expires_at = time.time() + _RESIDUE_TTL_SECONDS
                self._active_residue = report.dream_residue

            if self._inner_state is not None:
                self._inner_state.wake_up()

            report.ended_at = time.time()
            report.duration_seconds = report.ended_at - report.started_at

            self._dream_history.append(report)
            self._dream_history = self._dream_history[-_REMINDER_MAX_HISTORY:]
            self._last_dream_time = time.time()
            self._dreams_since_sleep_start += 1
            self._emit_visual_event("dream.finished", _report_to_dict(report))

            logger.info(
                f"🌙 Dream [{report.dream_id}] 完成 "
                f"耗时={report.duration_seconds:.1f}s | "
                f"seed={len(report.seed_report)} | "
                f"REM: {report.rem.nodes_activated}节点 "
                f"+{report.rem.new_edges_created}边 -{report.rem.edges_pruned}剪枝"
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(f"做梦周期异常: {exc}", exc_info=True)
            report.ended_at = time.time()
            report.duration_seconds = report.ended_at - report.started_at
        finally:
            self._current_phase = DreamPhase.AWAKE
            self._is_dreaming = False
            self._emit_visual_event("dream.phase_change", {"phase": "awake"})

        return report

    def _emit_visual_event(
        self,
        event_type: str,
        payload: dict[str, Any],
        source: str = "dream",
    ) -> None:
        """向可视化层广播做梦事件。"""
        try:
            from ..memory.router import MemoryRouter

            MemoryRouter.broadcast(event_type, payload, source=source)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"做梦可视化广播失败: {exc}")

    async def _run_nrem(self, event_history: list[Any]) -> NREMReport:
        """NREM 阶段：将历史事件通过 SNN 回放。"""
        report = NREMReport()

        if not self._snn or not self._snn_bridge:
            return report

        report.weight_before = {
            "syn_in_hid": self._snn.syn_in_hid.get_weight_stats(),
            "syn_hid_out": self._snn.syn_hid_out.get_weight_stats(),
        }

        episodes = self._sample_replay_episodes(event_history)
        for features_list in episodes:
            if features_list:
                stats = self._snn.replay_episodes(
                    features_list,
                    speed_multiplier=self._nrem_speed_multiplier,
                    reward_signal=0.0,
                )
                report.total_steps += stats.get("steps", 0)
                report.episodes_replayed += 1

        if report.episodes_replayed > 0:
            self._snn.homeostatic_scaling(self._nrem_homeostatic_rate)
            report.homeostatic_applied = True

        report.weight_after = {
            "syn_in_hid": self._snn.syn_in_hid.get_weight_stats(),
            "syn_hid_out": self._snn.syn_hid_out.get_weight_stats(),
        }
        return report

    def _sample_replay_episodes(self, event_history: list[Any]) -> list[list[np.ndarray]]:
        """按重要性加权采样事件片段。"""
        if not event_history or not self._snn_bridge:
            return []

        chunk_size = max(self._nrem_events_per_episode, 1)
        chunks: list[list[Any]] = []
        for i in range(0, len(event_history), chunk_size):
            chunks.append(event_history[i : i + chunk_size])
        if not chunks:
            return []

        scores: list[float] = []
        for chunk in chunks:
            score = 0.0
            for event in chunk:
                etype_val = _event_type_value(event)
                if etype_val == "message":
                    score += 2.0
                elif etype_val == "tool_call":
                    score += 1.5
                elif etype_val == "tool_result":
                    score += 1.0 if bool(_event_value(event, "tool_success", False)) else 0.5
                elif etype_val == "heartbeat":
                    score += 0.3
            scores.append(max(score, 0.1))

        total = sum(scores)
        probs = np.array([score / total for score in scores], dtype=np.float64)
        n_samples = min(self._nrem_replay_episodes, len(chunks))
        indices = np.random.choice(len(chunks), size=n_samples, replace=False, p=probs)

        episodes: list[list[np.ndarray]] = []
        window_seconds = self._nrem_events_per_episode * 180.0
        for idx in sorted(indices):
            chunk = chunks[idx]
            features = self._snn_bridge.extract_features_from_events(
                chunk,
                window_seconds=window_seconds,
            )
            episodes.append([features])
        return episodes

    async def _generate_dream_seeds(self, event_history: list[Any]) -> list[DreamSeed]:
        """从多路材料中生成入梦种子。"""
        memory_candidates = await load_memory_candidates(self._memory)
        candidates: list[DreamSeed] = []
        candidates.extend(await collect_day_residue(event_history, memory_candidates, self._workspace))
        candidates.extend(await collect_unfinished_tension(memory_candidates, self._workspace))
        candidates.extend(await collect_dream_lag(memory_candidates, self._workspace))
        candidates.extend(await collect_self_theme(memory_candidates))
        # 思考流作为入梦种子
        from ..service.registry import get_life_engine_service
        life_service = get_life_engine_service()
        if life_service and life_service._thought_manager:
            candidates.extend(await collect_thought_stream_seeds(life_service._thought_manager))

        # 海马体重复抑制
        recent_titles: set[str] = set()
        for title_set in self._recent_seed_titles:
            recent_titles.update(title_set)

        selected = select_seed_candidates(
            candidates,
            recent_seed_titles=recent_titles,
            repetition_decay=_REPETITION_DECAY,
        )

        # 记录本次选中的种子标题
        if selected:
            self._recent_seed_titles.append({s.title for s in selected})

        if selected:
            return selected

        return [
            DreamSeed(
                seed_id="fallback_seed",
                seed_type=DreamSeedType.DAY_RESIDUE.value,
                title="今天留下的模糊余波",
                summary="白天的事件并不充分，但仍有一些尚未消散的情绪和线索在轻轻晃动。",
                affect_arousal=0.25,
                importance=0.3,
                dreamability=0.4,
                score=0.3,
                tension_reason="材料稀薄时的低强度自发整理。",
            )
        ]

    async def _run_rem(self, seed_node_ids: list[str]) -> REMReport:
        """REM 阶段：渐进式联想扩散。"""
        report = REMReport()

        if not self._memory:
            return report

        # 渐进参数：后半夜更深、更广、衰减更慢
        n = self._dreams_since_sleep_start
        effective_depth = self._rem_max_depth + min(n, 3)
        effective_decay = min(self._rem_decay_factor + n * 0.05, 0.85)
        effective_seeds = self._rem_seeds_per_round + n
        effective_rounds = self._rem_walk_rounds + (1 if n >= 2 else 0)

        actual_seed_ids = seed_node_ids[: effective_seeds]
        for _ in range(effective_rounds):
            result = await self._memory.dream_walk(
                num_seeds=effective_seeds,
                seed_ids=actual_seed_ids or None,
                max_depth=effective_depth,
                decay_factor=effective_decay,
                learning_rate=self._rem_learning_rate,
            )
            report.nodes_activated += int(result.get("nodes_activated", 0) or 0)
            report.new_edges_created += int(result.get("new_edges_created", 0) or 0)
            report.walk_rounds += 1
            if not report.seed_node_ids:
                report.seed_node_ids = list(result.get("seed_ids") or [])

        pruned = await self._memory.prune_weak_edges(
            threshold=self._rem_edge_prune_threshold,
        )
        if isinstance(pruned, dict):
            report.edges_pruned = int(pruned.get("pruned", 0) or 0)
        else:
            report.edges_pruned = int(pruned or 0)

        return report

    async def _build_dream_scene(
        self,
        *,
        seeds: list[DreamSeed],
        rem_report: REMReport,
        event_history: list[Any],
    ) -> tuple[DreamTrace, str, DreamResidue] | None:
        """用 LLM 将入梦种子变形成梦境。"""
        from .seeds import _read_preview, _normalize_ref

        return await build_dream_scene(
            seeds=seeds,
            rem_report=rem_report,
            event_history=event_history,
            model_task_name=self._model_task_name,
            inner_state_summary=self._format_inner_state_summary(),
            recent_context_summary=build_recent_context_summary(event_history),
            reference_previews=build_reference_previews(
                seeds,
                self._workspace,
                lambda v: _normalize_ref(v, self._workspace),
                lambda p, mc: _read_preview(p, max_chars=mc),
            ),
            recent_dream_summaries=list(self._recent_dream_summaries),
            emit_visual_event=self._emit_visual_event,
        )

    def _format_inner_state_summary(self) -> str:
        """获取简洁的内在状态文本。"""
        if self._inner_state is None:
            return ""
        try:
            discrete = self._inner_state.modulators.get_discrete_dict()
            items: list[str] = []
            for key in ("curiosity", "energy", "contentment"):
                if key in discrete:
                    mod = self._inner_state.modulators.get(key)
                    if mod is not None:
                        items.append(f"{mod.cn_name}{discrete[key]}")
            return "、".join(items)
        except Exception:  # noqa: BLE001
            return ""

    def enter_sleep(self) -> None:
        """通知调质层进入睡眠。"""
        self._dreams_since_sleep_start = 0
        if self._inner_state is not None:
            self._inner_state.enter_sleep()

    def get_active_residue(self) -> DreamResidue | None:
        """获取仍在生效的梦后余韵。"""
        residue = self._active_residue
        if residue is None:
            return None
        if residue.expires_at and residue.expires_at <= time.time():
            self._active_residue = None
            return None
        return residue

    def get_active_residue_payload(self, target: str) -> str:
        """获取给指定系统消费的 payload 文本。"""
        residue = self.get_active_residue()
        if residue is None:
            return ""
        normalized = str(target or "").strip().lower()
        if normalized == "life":
            return residue.life_payload
        if normalized == "dfc":
            return residue.dfc_payload
        return residue.summary

    def get_dream_history(self, limit: int = 10) -> list[dict[str, Any]]:
        """获取最近的做梦报告。"""
        reports = self._dream_history[-limit:]
        return [_report_to_dict(report) for report in reports]

    def get_state(self) -> dict[str, Any]:
        """获取做梦系统当前状态。"""
        residue = self.get_active_residue()
        return {
            "is_dreaming": self._is_dreaming,
            "current_phase": self._current_phase.value,
            "last_dream_time": self._last_dream_time,
            "total_dreams": len(self._dream_history),
            "last_archive_path": self._last_archive_path,
            "active_residue": _residue_to_dict(residue) if residue else None,
            "last_report": (
                _report_to_dict(self._dream_history[-1]) if self._dream_history else None
            ),
        }

    def serialize(self) -> dict[str, Any]:
        """序列化做梦状态用于持久化。"""
        return {
            "last_dream_time": self._last_dream_time,
            "last_archive_path": self._last_archive_path,
            "active_residue": _residue_to_dict(self.get_active_residue()),
            "dream_history": [_report_to_dict(report) for report in self._dream_history[-_REMINDER_MAX_HISTORY:]],
            "recent_seed_titles": [list(s) for s in self._recent_seed_titles],
            "recent_dream_summaries": list(self._recent_dream_summaries),
            "dreams_since_sleep_start": self._dreams_since_sleep_start,
        }

    def deserialize(self, data: dict[str, Any]) -> None:
        """从持久化数据恢复状态。"""
        if not isinstance(data, dict):
            return
        self._last_dream_time = float(data.get("last_dream_time", 0.0))
        self._last_archive_path = str(data.get("last_archive_path") or "")
        residue_raw = data.get("active_residue")
        if isinstance(residue_raw, dict):
            self._active_residue = _residue_from_payload(residue_raw)

        history_raw = data.get("dream_history")
        if isinstance(history_raw, list):
            restored: list[DreamReport] = []
            for item in history_raw[-_REMINDER_MAX_HISTORY:]:
                if isinstance(item, dict):
                    restored.append(_report_from_dict(item))
            self._dream_history = restored

        # 恢复仿生状态
        seed_titles_raw = data.get("recent_seed_titles")
        if isinstance(seed_titles_raw, list):
            self._recent_seed_titles = deque(
                (set(s) for s in seed_titles_raw if isinstance(s, list)),
                maxlen=_DREAM_HISTORY_WINDOW,
            )
        summaries_raw = data.get("recent_dream_summaries")
        if isinstance(summaries_raw, list):
            self._recent_dream_summaries = deque(
                (str(s) for s in summaries_raw if isinstance(s, str)),
                maxlen=_DREAM_HISTORY_WINDOW,
            )
        self._dreams_since_sleep_start = int(data.get("dreams_since_sleep_start", 0))

        logger.info(f"做梦系统状态已恢复: last_dream={self._last_dream_time:.0f}")
