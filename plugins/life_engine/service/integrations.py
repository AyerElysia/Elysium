"""life_engine 子系统集成模块。

包含 DFC 集成、SNN 皮层下系统、调质层、做梦系统的初始化与管理。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from src.app.plugin_system.api.log_api import get_logger

from .event_builder import (
    EventType,
    LifeEngineEvent,
    _format_time_display,
    _now_iso,
    _shorten_text,
    INTERNAL_PLATFORM,
    is_life_heartbeat_event,
)

if TYPE_CHECKING:
    from .core import LifeEngineService


logger = get_logger("life_engine", display="life_engine")


def to_jsonable(value: Any) -> Any:
    """将复杂对象转换为 JSON 可序列化结构。

    Args:
        value: 要转换的值

    Returns:
        JSON 可序列化的值
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [to_jsonable(v) for v in value]
    if isinstance(value, datetime):
        return value.isoformat()

    tolist = getattr(value, "tolist", None)
    if callable(tolist):
        try:
            return to_jsonable(tolist())
        except (TypeError, ValueError, AttributeError) as e:
            logger.debug(f"tolist() conversion failed for {type(value)}: {e}")

    item = getattr(value, "item", None)
    if callable(item):
        try:
            return item()
        except (TypeError, ValueError, AttributeError) as e:
            logger.debug(f"item() conversion failed for {type(value)}: {e}")

    enum_value = getattr(value, "value", None)
    if isinstance(enum_value, (str, int, float, bool)):
        return enum_value

    return str(value)


class DFCIntegration:
    """DFC 集成管理器。

    负责与 DFC（对话流控制器）的交互，包括状态摘要生成、
    梦记录注入、异步消息传递等。
    """

    def __init__(self, service: "LifeEngineService") -> None:
        """初始化 DFC 集成管理器。

        Args:
            service: LifeEngineService 实例
        """
        self._service = service
        self._injected_dream_ids: set[str] = set()

    async def get_state_digest(self) -> str:
        """生成给 DFC 的状态摘要。

        设计原则：
        1. 控制在 150-200 tokens
        2. 只包含对当前对话有用的信息
        3. 使用简单模板，不调用 LLM
        4. 不会保存到历史消息中

        Returns:
            格式化的状态摘要文本
        """
        snapshot = await self.get_dfc_snapshot()
        return str(snapshot.get("state_digest") or "")

    async def get_dfc_snapshot(self) -> dict[str, Any]:
        """生成供 DFC 消费的结构化快照。

        这个快照是 DFC 的单一状态来源：
        - state_digest: 给 prompt / 状态查询用的简短摘要
        - active_todo_lines: 活跃 TODO 的短行摘要
        - recent_diary_lines: 最近日记的短行摘要
        """
        async with self._service._get_lock():
            state_digest = self._build_state_digest_locked()
            todo_lines = self._load_active_todo_lines()
            diary_lines = self._load_recent_diary_lines()

        return {
            "generated_at": _now_iso(),
            "state_digest": state_digest,
            "active_todo_lines": todo_lines,
            "recent_diary_lines": diary_lines,
        }

    def _build_state_digest_locked(self) -> str:
        """在持锁前提下构建轻量状态摘要。"""
        parts = []

        # 1. 调质层状态（如果启用）
        if self._service._inner_state is not None:
            try:
                mood_dict = self._service._inner_state.modulators.get_discrete_dict()
                mood_items = []
                priority_dims = ["curiosity", "energy", "contentment"]
                for name in priority_dims:
                    if name in mood_dict:
                        mod = self._service._inner_state.modulators.get(name)
                        if mod:
                            mood_items.append(f"{mod.cn_name}{mood_dict[name]}")
                if mood_items:
                    parts.append(f"【内在状态】{'、'.join(mood_items)}")
            except Exception as e:
                logger.warning(f"获取调质层状态失败: {e}")

        # 2. 最近思考（最近1-2条心跳独白）
        heartbeat_events = [
            e for e in self._service._event_history
            if is_life_heartbeat_event(e)
        ][-2:]

        if heartbeat_events:
            thoughts = []
            for event in heartbeat_events:
                time_display = _format_time_display(event.timestamp)
                thought = _shorten_text(event.content, max_length=40)
                thoughts.append(f"  [{time_display}] {thought}")
            if thoughts:
                parts.append("【最近思考】")
                parts.extend(thoughts)

        # 3. 工具使用偏好
        tool_events = [
            e for e in self._service._event_history[-30:]
            if e.event_type == EventType.TOOL_CALL
        ]

        if tool_events:
            tool_counts: dict[str, int] = {}
            for event in tool_events:
                name = event.tool_name
                if name and name.startswith("nucleus_"):
                    short_name = name.replace("nucleus_", "")
                    tool_counts[short_name] = tool_counts.get(short_name, 0) + 1

            if tool_counts:
                top_tools = sorted(tool_counts.items(), key=lambda x: x[1], reverse=True)[:2]
                tool_names = [name for name, _ in top_tools]
                parts.append(f"【工具偏好】{', '.join(tool_names)}")

        if self._service._dream_scheduler is not None:
            try:
                dream_payload = str(
                    self._service._dream_scheduler.get_active_residue_payload("dfc") or ""
                ).strip()
                if dream_payload:
                    parts.append(dream_payload)
            except Exception as e:
                logger.debug(f"读取 DFC 梦后余韵失败: {e}")

        return "\n".join(parts) if parts else ""

    def build_dream_record_payload_text(self, report: Any) -> str:
        """把 DreamReport 构建为完整 assistant payload 文本。

        Args:
            report: DreamReport 对象

        Returns:
            格式化的梦记录文本
        """
        try:
            payload_obj = to_jsonable(asdict(report))
            payload_text = json.dumps(payload_obj, ensure_ascii=False, indent=2)
        except Exception as exc:
            logger.warning(f"序列化梦记录失败: {exc}")
            return ""

        return (
            "[梦境记录]\n"
            "<dream_record>\n"
            f"{payload_text}\n"
            "</dream_record>"
        )

    async def pick_latest_external_stream_id(self) -> str:
        """选择最近的外部对话流作为注入目标。

        Returns:
            最近的 stream_id，如果无可用则返回空字符串
        """
        async with self._service._get_lock():
            candidates = list(self._service._pending_events) + list(self._service._event_history)

        for event in reversed(candidates):
            if event.event_type != EventType.MESSAGE:
                continue
            stream_id = str(event.stream_id or "").strip()
            if not stream_id:
                continue
            source = str(event.source or "").strip()
            if source == INTERNAL_PLATFORM:
                continue
            return stream_id
        return ""

    async def inject_dream_report(self, report: Any, trigger: str) -> None:
        """把完整梦境记录作为 assistant payload 注入 DFC。

        Args:
            report: DreamReport 对象
            trigger: 触发类型（sleep_window, daytime_nap, manual）
        """
        dream_id = str(getattr(report, "dream_id", "") or "").strip()
        if not dream_id:
            return
        if dream_id in self._injected_dream_ids:
            return

        stream_id = await self.pick_latest_external_stream_id()
        if not stream_id:
            logger.info(
                f"梦记录未注入 DFC（无可用目标流）: dream_id={dream_id} trigger={trigger}"
            )
            return

        payload_text = self.build_dream_record_payload_text(report)
        if not payload_text:
            return

        try:
            from default_chatter import plugin as default_chatter_plugin_module

            push_runtime_assistant_injection = getattr(
                default_chatter_plugin_module,
                "push_runtime_assistant_injection",
                None,
            )
            if not callable(push_runtime_assistant_injection):
                logger.warning("默认聊天未提供 runtime assistant 注入接口，跳过梦记录注入")
                return

            push_runtime_assistant_injection(stream_id, payload_text)
            self._injected_dream_ids.add(dream_id)
            if len(self._injected_dream_ids) > 512:
                self._injected_dream_ids.clear()
                self._injected_dream_ids.add(dream_id)
            logger.info(
                f"已将完整梦记录注入 DFC payload 队列: "
                f"stream={stream_id[:8]} dream_id={dream_id} trigger={trigger}"
            )
        except Exception as exc:
            logger.warning(f"注入梦记录到 DFC payload 失败: {exc}")

    async def query_actor_context(self) -> str:
        """供 DFC 同步查询当前状态、TODO 与最近日记。

        Returns:
            格式化的上下文摘要
        """
        snapshot = await self.get_dfc_snapshot()
        parts: list[str] = []

        state_digest = str(snapshot.get("state_digest") or "").strip()
        if state_digest:
            parts.append(state_digest)

        todo_lines = [str(line).strip() for line in snapshot.get("active_todo_lines") or [] if str(line).strip()]
        if todo_lines:
            parts.append("【活跃 TODO】\n" + "\n".join(todo_lines))

        diary_lines = [str(line).strip() for line in snapshot.get("recent_diary_lines") or [] if str(line).strip()]
        if diary_lines:
            parts.append("【最近日记】\n" + "\n".join(diary_lines))

        return "\n\n".join(part for part in parts if part.strip())

    def _workspace_dir(self) -> Path:
        """返回工作空间目录。"""
        return self._service._workspace_dir()

    def _load_active_todo_lines(self, *, limit: int = 5) -> list[str]:
        """读取当前活跃 TODO 的简短摘要。"""
        from ..tools.todo_tools import TodoStatus, TodoStorage

        storage = TodoStorage(self._workspace_dir())
        inactive_statuses = {
            TodoStatus.COMPLETED.value,
            TodoStatus.CANCELLED.value,
            TodoStatus.ARCHIVED.value,
        }
        todos = [todo for todo in storage.load() if todo.status not in inactive_statuses]
        lines: list[str] = []
        for todo in todos[:limit]:
            next_action = str(getattr(todo, "next_action", "") or "").strip()
            suffix = f"；下一步：{next_action}" if next_action else ""
            lines.append(f"- {todo.title} ({todo.status}/{todo.priority}){suffix}")
        return lines

    def _load_recent_diary_lines(self, *, limit: int = 2) -> list[str]:
        """读取最近几篇日记的预览。"""
        diary_dir = self._workspace_dir() / "diary"
        if not diary_dir.exists():
            return []

        lines: list[str] = []
        for diary_file in sorted(diary_dir.glob("*.md"), reverse=True)[:limit]:
            try:
                content = " ".join(diary_file.read_text(encoding="utf-8").split())
            except Exception:
                continue
            if not content:
                continue
            preview = _shorten_text(content, max_length=120)
            lines.append(f"- {diary_file.stem}: {preview}")
        return lines


class SNNIntegration:
    """SNN 皮层下系统集成管理器。

    负责 SNN 网络的初始化、tick 循环、心跳前后更新等。
    """

    def __init__(self, service: "LifeEngineService") -> None:
        """初始化 SNN 集成管理器。

        Args:
            service: LifeEngineService 实例
        """
        self._service = service

    async def init_snn(self) -> None:
        """初始化 SNN 皮层下驱动核。"""
        cfg = self._service._cfg()
        if not cfg.snn.enabled:
            logger.debug("SNN 未启用，跳过初始化")
            return

        try:
            from ..snn.core import DriveCoreNetwork
            from ..snn.bridge import SNNBridge

            self._service._snn_network = DriveCoreNetwork()
            self._service._snn_bridge = SNNBridge(self._service)

            # 从持久化恢复
            snn_persisted = getattr(self._service, "_snn_persisted_state", None)
            if snn_persisted and isinstance(snn_persisted, dict):
                self._service._snn_network.deserialize(snn_persisted)
                logger.info(
                    f"SNN 状态已恢复，tick_count={self._service._snn_network.tick_count}"
                )
            self._service._snn_persisted_state = None

            # 启动独立 tick 循环
            tick_interval = cfg.snn.tick_interval_seconds
            from src.kernel.concurrency import get_task_manager
            task = get_task_manager().create_task(
                self._snn_tick_loop(tick_interval),
                name="snn_tick_loop",
                daemon=True,
            )
            self._service._snn_tick_task_id = task.task_id

            mode = "shadow" if cfg.snn.shadow_only else "active"
            logger.info(
                f"SNN 皮层下系统已初始化: mode={mode} "
                f"tick_interval={tick_interval}s "
                f"inject={cfg.snn.inject_to_heartbeat}"
            )
            from .audit import log_snn_snapshot
            log_snn_snapshot(
                action="init",
                mode=mode,
                tick_count=self._service._snn_network.tick_count,
                health=self._service._snn_network.get_health(),
            )
        except Exception as e:
            logger.error(f"SNN 初始化失败: {e}", exc_info=True)
            self._service._snn_network = None
            self._service._snn_bridge = None

        # 初始化调质层
        await self._init_neuromod()

        # 初始化做梦系统
        await self._init_dream_scheduler()

    async def _init_neuromod(self) -> None:
        """初始化神经调质层。"""
        cfg = self._service._cfg()
        neuromod_cfg = getattr(cfg, "neuromod", None)
        if neuromod_cfg and neuromod_cfg.enabled:
            try:
                from ..neuromod import InnerStateEngine
                self._service._inner_state = InnerStateEngine()

                neuromod_persisted = getattr(self._service, "_neuromod_persisted_state", None)
                if neuromod_persisted and isinstance(neuromod_persisted, dict):
                    self._service._inner_state.deserialize(neuromod_persisted)
                self._service._neuromod_persisted_state = None

                logger.info(
                    f"调质层已初始化: inject={neuromod_cfg.inject_to_heartbeat} "
                    f"habits={neuromod_cfg.habit_tracking}"
                )
            except Exception as e:
                logger.error(f"调质层初始化失败: {e}", exc_info=True)
                self._service._inner_state = None
        else:
            logger.debug("调质层未启用")

    async def _init_dream_scheduler(self) -> None:
        """初始化做梦系统。"""
        cfg = self._service._cfg()
        dream_cfg = getattr(cfg, "dream", None)
        if dream_cfg and dream_cfg.enabled:
            try:
                from ..dream import DreamScheduler
                self._service._dream_scheduler = DreamScheduler(
                    snn=self._service._snn_network,
                    inner_state=self._service._inner_state,
                    memory_service=self._service._memory_service,
                    snn_bridge=self._service._snn_bridge,
                    workspace_path=cfg.settings.workspace_path,
                    model_task_name=cfg.model.task_name,
                    nrem_replay_episodes=dream_cfg.nrem_replay_episodes,
                    nrem_events_per_episode=dream_cfg.nrem_events_per_episode,
                    nrem_speed_multiplier=dream_cfg.nrem_speed_multiplier,
                    nrem_homeostatic_rate=dream_cfg.nrem_homeostatic_rate,
                    rem_walk_rounds=dream_cfg.rem_walk_rounds,
                    rem_seeds_per_round=dream_cfg.rem_seeds_per_round,
                    rem_max_depth=dream_cfg.rem_max_depth,
                    rem_decay_factor=dream_cfg.rem_decay_factor,
                    rem_learning_rate=dream_cfg.rem_learning_rate,
                    rem_edge_prune_threshold=dream_cfg.rem_edge_prune_threshold,
                    dream_interval_minutes=dream_cfg.dream_interval_minutes,
                    idle_trigger_heartbeats=dream_cfg.idle_trigger_heartbeats,
                    nap_enabled=dream_cfg.nap_enabled,
                )

                dream_persisted = getattr(self._service, "_dream_persisted_state", None)
                if dream_persisted and isinstance(dream_persisted, dict):
                    self._service._dream_scheduler.deserialize(dream_persisted)
                self._service._dream_persisted_state = None

                logger.info("做梦系统已初始化")
            except Exception as e:
                logger.error(f"做梦系统初始化失败: {e}", exc_info=True)
                self._service._dream_scheduler = None
        else:
            logger.debug("做梦系统未启用")

    async def _snn_tick_loop(self, interval: float) -> None:
        """SNN 独立 tick 循环。

        只做膜电位衰减和 trace 衰减，不执行完整 step()。
        """
        persist_interval = max(1, int(60 / interval))
        snapshot_interval = max(1, int(300 / interval))

        while self._service._state.running:
            try:
                await asyncio.sleep(interval)
                if not self._service._state.running:
                    break
                if self._service._snn_network is None:
                    break

                # 仅衰减，不学习
                self._service._snn_network.decay_only()

                tick = self._service._snn_network.tick_count

                if tick % persist_interval == 0:
                    await self._service._save_runtime_context()

                if tick % snapshot_interval == 0:
                    from .audit import log_snn_snapshot
                    log_snn_snapshot(
                        action="periodic",
                        tick_count=tick,
                        health=self._service._snn_network.get_health(),
                    )

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"SNN tick 异常: {e}")
                await asyncio.sleep(interval)

        logger.info("SNN tick 循环已停止")

    async def _snapshot_events_for_snn(self) -> list[LifeEngineEvent]:
        """获取 SNN 分析用事件快照。"""
        async with self._service._get_lock():
            events = list(self._service._event_history)
            if self._service._pending_events:
                events.extend(self._service._pending_events)

        events.sort(key=lambda event: (event.sequence, event.timestamp))
        return events

    @staticmethod
    def _is_real_heartbeat_event(event: LifeEngineEvent) -> bool:
        """判断是否为生命中枢自身产生的真实心跳。"""
        return is_life_heartbeat_event(event)

    @staticmethod
    def _collect_tool_metrics(events: list[LifeEngineEvent]) -> tuple[int, int, int]:
        """统计事件段中的工具调用指标。"""
        tool_event_count = 0
        tool_success_count = 0
        tool_fail_count = 0

        for event in events:
            if event.event_type == EventType.TOOL_CALL:
                tool_event_count += 1
            elif event.event_type == EventType.TOOL_RESULT:
                tool_event_count += 1
                if getattr(event, "tool_success", False):
                    tool_success_count += 1
                else:
                    tool_fail_count += 1

        return tool_event_count, tool_success_count, tool_fail_count

    async def heartbeat_pre(self) -> None:
        """心跳前 SNN + 调质层更新。"""
        if self._service._snn_network is None or self._service._snn_bridge is None:
            return

        try:
            cfg = self._service._cfg()
            events = await self._snapshot_events_for_snn()
            features = self._service._snn_bridge.extract_features_from_events(
                events,
                window_seconds=cfg.snn.feature_window_seconds,
            )
            reward = self._service._snn_bridge.get_last_reward()
            output = self._service._snn_network.step(features, reward=reward)

            from .audit import log_snn_tick
            log_snn_tick(
                action="heartbeat_pre",
                tick_count=self._service._snn_network.tick_count,
                real_steps=self._service._snn_network._real_step_count,
                features=features.tolist(),
                reward=round(reward, 4),
                output=output.tolist(),
                drives=self._service._snn_network.get_drive_dict(),
            )

            # 更新调质层
            if self._service._inner_state is not None:
                snn_drives = self._service._snn_network.get_drive_dict()
                event_stats = self._service._snn_bridge.get_last_event_stats()
                self._service._inner_state.tick(snn_drives=snn_drives, event_stats=event_stats)

        except Exception as e:
            logger.warning(f"SNN heartbeat_pre 异常: {e}")

    async def heartbeat_post(self) -> None:
        """心跳后 SNN 更新：根据心跳结果计算奖赏信号。"""
        if self._service._snn_bridge is None:
            return

        try:
            events = await self._snapshot_events_for_snn()
            if not events:
                return

            heartbeat_positions = [
                idx
                for idx, event in enumerate(events)
                if self._is_real_heartbeat_event(event)
            ]

            segment: list[LifeEngineEvent]
            if heartbeat_positions:
                current_hb_idx = heartbeat_positions[-1]
                current_hb = events[current_hb_idx]
                current_hb_round = getattr(current_hb, "heartbeat_index", None)

                if current_hb_round == self._service._state.heartbeat_count:
                    if len(heartbeat_positions) >= 2:
                        prev_hb_idx = heartbeat_positions[-2]
                        segment = events[prev_hb_idx + 1 : current_hb_idx]
                    else:
                        segment = events[max(0, current_hb_idx - 120) : current_hb_idx]
                else:
                    segment = events[current_hb_idx + 1 :]
            else:
                segment = events[-120:]

            tool_event_count, tool_success_count, tool_fail_count = self._collect_tool_metrics(segment)
            had_text_reply = bool((self._service._state.last_model_reply or "").strip())

            reward = self._service._snn_bridge.record_heartbeat_result(
                tool_event_count=tool_event_count,
                tool_success_count=tool_success_count,
                tool_fail_count=tool_fail_count,
                idle_count=self._service._state.idle_heartbeat_count,
                had_text_reply=had_text_reply,
            )

            from .audit import log_snn_tick
            log_snn_tick(
                action="heartbeat_post",
                tick_count=self._service._snn_network.tick_count if self._service._snn_network else 0,
                reward=round(reward, 4),
                idle_count=self._service._state.idle_heartbeat_count,
                tool_events=tool_event_count,
                tool_success=tool_success_count,
                tool_fail=tool_fail_count,
            )

            # 习惯追踪
            if self._service._inner_state is not None:
                today_str = datetime.now().strftime("%Y-%m-%d")
                for event in segment:
                    if event.event_type == EventType.TOOL_CALL:
                        tool_name = str(getattr(event, "tool_name", "") or "")
                        if tool_name:
                            self._service._inner_state.record_tool_use(tool_name, today_str)

        except Exception as e:
            logger.warning(f"SNN heartbeat_post 异常: {e}")


class MemoryIntegration:
    """记忆系统集成管理器。

    负责记忆服务的初始化与日常衰减任务。
    """

    def __init__(self, service: "LifeEngineService") -> None:
        """初始化记忆集成管理器。

        Args:
            service: LifeEngineService 实例
        """
        self._service = service
        self._last_decay_date: str | None = None

    async def init_memory_service(self) -> None:
        """初始化仿生记忆服务。"""
        try:
            from ..memory.service import LifeMemoryService

            cfg = self._service._cfg()
            workspace = Path(cfg.settings.workspace_path)
            self._service._memory_service = LifeMemoryService(workspace)
            await self._service._memory_service.initialize()
            logger.info("life_engine 仿生记忆服务已初始化")
        except Exception as e:
            logger.error(f"记忆服务初始化失败: {e}", exc_info=True)
            self._service._memory_service = None

    async def maybe_run_daily_decay(self) -> None:
        """每日运行一次记忆衰减任务。"""
        if not self._service._memory_service:
            return

        today = datetime.now().strftime("%Y-%m-%d")
        if self._last_decay_date == today:
            return

        try:
            update_count = await self._service._memory_service.apply_decay()
            self._last_decay_date = today
            if update_count > 0:
                logger.info(
                    f"life_engine 记忆衰减完成: 更新节点={update_count}"
                )
        except Exception as e:
            logger.error(f"记忆衰减任务失败: {e}", exc_info=True)
