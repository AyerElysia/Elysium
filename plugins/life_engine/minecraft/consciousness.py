"""意识层：Minecraft 会话管理 + 上下文管理。

MinecraftSession 是爱莉在 MC 中的"存在"。
她通过这里看画面、做决策、执行意图、内化经验。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .capture import WindowCapture
from .conversational_motor import ConversationalMotorController, create_conversational_motor
from .input_control import InputController
from .launcher import MCConfig, MinecraftLauncher
from .log_reader import MinecraftLogReader, create_log_reader
from .motor_loop import ExecutionReport, MotorLoop
from .prompts import (
    CONSCIOUSNESS_OBSERVATION,
    CONSCIOUSNESS_SYSTEM,
    HEARTBEAT_MINECRAFT_ACTIVE,
    HEARTBEAT_MINECRAFT_IDLE,
    PERCEPTION_PROMPT,
    SESSION_SUMMARY_PROMPT,
)
from .social import SocialPresence, MinecraftChat, create_social_system

logger = logging.getLogger("life_engine.minecraft.consciousness")


@dataclass(slots=True)
class SessionState:
    """会话状态。"""

    active: bool = False
    session_id: str = ""
    start_time: float = 0.0
    current_goal: str = ""
    goals_completed: list[str] = field(default_factory=list)
    events: list[str] = field(default_factory=list)
    items_gained: list[str] = field(default_factory=list)
    last_perception: str = ""
    last_result: str = ""
    # 游戏状态（从 HUD 读取或 VLA 报告）
    health: float = 20.0
    hunger: float = 20.0
    time_of_day: str = "白天"
    inventory_summary: str = ""

    @property
    def duration_minutes(self) -> float:
        if not self.active:
            return 0.0
        return (time.time() - self.start_time) / 60.0

    def to_summary(self) -> str:
        """生成压缩摘要（进主上下文）。"""
        if not self.active and not self.goals_completed:
            return ""
        parts = []
        if self.goals_completed:
            parts.append(f"完成了: {', '.join(self.goals_completed[-3:])}")
        if self.items_gained:
            parts.append(f"获得: {', '.join(self.items_gained[-5:])}")
        parts.append(f"游玩 {self.duration_minutes:.0f} 分钟")
        return " | ".join(parts)


class MinecraftSession:
    """Minecraft 具身体验会话。

    管理爱莉在 MC 中的完整体验：
    - 启动/停止游戏
    - 意识层决策循环
    - VLA 意图执行
    - 上下文管理（后缀 vs 主上下文）
    - 会话结束反思
    """

    def __init__(
        self,
        workspace: Path,
        mc_config: MCConfig | None = None,
        llm_helper: Any | None = None,  # LLM 辅助意图解析
    ) -> None:
        self._workspace = workspace
        self._mc_config = mc_config or MCConfig()
        self._llm_helper = llm_helper

        # 组件
        self._launcher = MinecraftLauncher(self._mc_config)
        self._capture = WindowCapture()
        self._input = InputController()
        self._conversational_motor = create_conversational_motor(self._input)
        self._motor: MotorLoop | None = None

        # 社交系统
        self._social, self._chat = create_social_system()

        # 日志读取器（实时感知游戏内聊天和事件）
        self._log_reader: MinecraftLogReader = create_log_reader()
        self._log_events_task: asyncio.Task | None = None

        # 状态
        self._state = SessionState()
        self._session_log: list[dict[str, Any]] = []

        # 回调（由 life_engine 注入）
        self._on_perception: Callable[[str], None] | None = None
        self._on_session_end: Callable[[SessionState], None] | None = None

    @property
    def state(self) -> SessionState:
        return self._state

    @property
    def is_active(self) -> bool:
        return self._state.active

    def set_callbacks(
        self,
        on_perception: Callable[[str], None] | None = None,
        on_session_end: Callable[[SessionState], None] | None = None,
    ) -> None:
        """设置回调（由 life_engine 注入）。"""
        self._on_perception = on_perception
        self._on_session_end = on_session_end

    # === 会话生命周期 ===

    async def start(self, goal: str = "") -> dict[str, Any]:
        """启动游戏会话。"""
        if self._state.active:
            return {"success": False, "error": "已有会话在运行"}

        # 1. 启动 MC
        result = await self._launcher.launch()
        if not result.success:
            return {"success": False, "error": f"MC 启动失败: {result.error}"}

        # 2. 等待窗口出现
        await asyncio.sleep(5)
        win_info = await self._launcher.find_window()
        if not win_info:
            # 再等一会儿
            await asyncio.sleep(10)
            win_info = await self._launcher.find_window()

        if win_info:
            self._capture._window_info = win_info
            self._input.window_info = win_info
        else:
            logger.warning("未找到 MC 窗口，尝试继续")

        # 3. 初始化 MotorLoop（对话式控制）
        self._motor = MotorLoop(
            capture=self._capture,
            input_ctrl=self._input,
            conversational_motor=self._conversational_motor,
            llm_helper=self._llm_helper,
        )
        await self._motor.start_reflex_loop()

        # 4. 启动日志读取器（感知游戏内聊天和事件）
        await self._log_reader.start()
        self._log_events_task = asyncio.create_task(
            self._process_log_events_loop(), name="mc_log_events"
        )

        # 5. 设置状态
        session_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        self._state = SessionState(
            active=True,
            session_id=session_id,
            start_time=time.time(),
            current_goal=goal or "自由探索",
        )

        logger.info(f"Minecraft 会话开始: {session_id}, 目标: {goal or '自由探索'}")
        return {
            "success": True,
            "session_id": session_id,
            "window_info": win_info,
            "control_mode": "conversational",  # 对话式控制
        }

    async def stop(self) -> dict[str, Any]:
        """结束游戏会话。"""
        if not self._state.active:
            return {"success": False, "error": "没有活跃的会话"}

        # 1. 停止日志读取器
        if self._log_events_task and not self._log_events_task.done():
            self._log_events_task.cancel()
            try:
                await self._log_events_task
            except asyncio.CancelledError:
                pass
        await self._log_reader.stop()

        # 2. 停止 Reflex
        if self._motor:
            await self._motor.stop_reflex_loop()

        # 3. 停止 MC（可选，保留窗口让她下次继续）
        # await self._launcher.stop()

        # 4. 生成摘要
        summary = self._state.to_summary()

        # 5. 触发反思回调
        if self._on_session_end:
            self._on_session_end(self._state)

        # 6. 保存会话日志
        await self._save_session_log()

        self._state.active = False
        logger.info(f"Minecraft 会话结束: {summary}")

        return {
            "success": True,
            "summary": summary,
            "duration_minutes": self._state.duration_minutes,
            "goals_completed": self._state.goals_completed,
        }

    # === 意图执行 ===

    async def do_intent(self, intent: str, timeout: float | None = None) -> dict[str, Any]:
        """执行一个意图。

        Args:
            intent: 自然语言意图，如 "砍那棵树"
            timeout: 超时时间
        """
        if not self._state.active:
            return {"success": False, "error": "没有活跃的会话"}
        if not self._motor:
            return {"success": False, "error": "MotorLoop 未初始化"}

        # 执行
        report = await self._motor.execute_intent(intent, timeout)

        # 更新状态
        self._state.last_result = (
            f"{'成功' if report.success else '失败'}: {intent} "
            f"({report.steps}步, {report.duration_seconds:.1f}s)"
        )
        if report.success:
            self._state.goals_completed.append(intent)

        # 记录日志
        self._session_log.append({
            "time": datetime.now(timezone.utc).isoformat(),
            "intent": intent,
            "success": report.success,
            "steps": report.steps,
            "duration": report.duration_seconds,
            "reason": report.reason,
        })

        return {
            "success": report.success,
            "steps": report.steps,
            "duration_seconds": report.duration_seconds,
            "reason": report.reason,
            "summary": self._state.last_result,
        }

    # === 感知 ===

    async def look(self) -> dict[str, Any]:
        """主动截取高清截图（她想仔细看）。"""
        frame = await self._capture.grab_consciousness_frame()
        if not frame:
            return {"success": False, "error": "截图失败"}

        # 保存截图
        screenshot_path = self._workspace / "minecraft" / "screenshots"
        await asyncio.to_thread(screenshot_path.mkdir, parents=True, exist_ok=True)
        path = frame.save(screenshot_path / f"look_{int(time.time())}.png")

        return {
            "success": True,
            "screenshot_path": str(path),
            "base64": frame.to_base64(),
            "width": frame.width,
            "height": frame.height,
        }

    async def perceive(self) -> str:
        """感知当前画面（意识层用）。"""
        frame = await self._capture.grab_consciousness_frame()
        if not frame:
            return "（截图失败，看不到画面）"

        # 这里应该调用她的 LLM 进行视觉理解
        # 简化版：返回截图路径，由上层处理
        screenshot_path = self._workspace / "minecraft" / "screenshots"
        await asyncio.to_thread(screenshot_path.mkdir, parents=True, exist_ok=True)
        path = frame.save(screenshot_path / f"perceive_{int(time.time())}.png")

        self._state.last_perception = f"截图已保存: {path}"
        if self._on_perception:
            self._on_perception(self._state.last_perception)

        return self._state.last_perception

    # === 上下文管理 ===

    def get_transient_suffix(self) -> str:
        """获取后缀提示词（每步刷新，不进历史）。"""
        if not self._state.active:
            return ""

        # 构建第一人称体验描述
        bodily_feeling = self.get_bodily_feeling()
        visual_context = self.get_visual_context()
        social_context = self.get_social_context()

        last_action = self._state.last_result or "你刚进入这个世界"
        current_thought = self._state.current_goal if self._state.current_goal else "你还没有特定的目标"

        return CONSCIOUSNESS_OBSERVATION.format(
            bodily_feeling=bodily_feeling,
            visual_context=visual_context,
            last_action=last_action,
            social_context=social_context,
            current_thought=current_thought,
        )

    def get_heartbeat_context(self) -> str:
        """获取心跳注入上下文。"""
        if self._state.active:
            # 游戏进行中
            bodily_feeling = self.get_bodily_feeling()
            social_context = self.get_social_context()
            current_activity = self._state.current_goal or "自由探索"

            state_feeling = f"{bodily_feeling}"
            if not social_context:
                social_context = "你独自在这个世界中"

            return HEARTBEAT_MINECRAFT_ACTIVE.format(
                social_presence=social_context,
                state_feeling=state_feeling,
                current_activity=current_activity,
                duration=f"{self._state.duration_minutes:.0f}",
            )
        else:
            # 游戏未进行，但可以选择进入
            memories = self._get_minecraft_memories()
            ayer_presence = self._detect_ayer_presence()
            current_mood = self._infer_current_mood()

            return HEARTBEAT_MINECRAFT_IDLE.format(
                memories=memories,
                ayer_presence=ayer_presence,
                current_mood=current_mood,
            )

    def _get_minecraft_memories(self) -> str:
        """获取 Minecraft 相关记忆摘要。"""
        if not self._state.goals_completed and not self._state.items_gained:
            return "你还没有在 Minecraft 中留下什么记忆"

        parts = []
        if self._state.goals_completed:
            recent_goals = self._state.goals_completed[-3:]
            parts.append(f"上次你做了：{', '.join(recent_goals)}")
        if self._state.items_gained:
            recent_items = self._state.items_gained[-5:]
            parts.append(f"你获得了：{', '.join(recent_items)}")

        return "\n".join(parts)

    def _detect_ayer_presence(self) -> str:
        """检测 Ayer 是否在游戏中（通过日志读取器感知）。"""
        # 查看近期日志事件中 Ayer 是否在线
        recent = self._log_reader.peek_events()
        for event in reversed(recent[-20:]):
            if event.player and "ayer" in event.player.lower():
                if event.type == "join":
                    return f"Ayer ({event.player}) 在你的世界里 💕"
                elif event.type == "leave":
                    return f"Ayer 刚才离开了，你还有他的气息"
                elif event.type == "chat":
                    return f"Ayer 刚才说了：「{event.message}」"
        return "你不确定 Ayer 现在是否在玩 Minecraft"

    def _infer_current_mood(self) -> str:
        """推断当前心情（基于最近的互动和状态）。"""
        # TODO: 基于 life_engine 的情绪状态和最近互动
        # 暂时返回中性描述
        return "你感觉还好，想做点什么"

    def get_main_context_summary(self) -> str:
        """获取主上下文压缩摘要。"""
        return self._state.to_summary()

    # === 状态更新 ===

    def update_game_state(
        self,
        health: float | None = None,
        hunger: float | None = None,
        time_of_day: str | None = None,
        inventory_summary: str | None = None,
    ) -> None:
        """更新游戏状态（从 HUD 或 VLA 报告）。"""
        if health is not None:
            self._state.health = health
        if hunger is not None:
            self._state.hunger = hunger
        if time_of_day is not None:
            self._state.time_of_day = time_of_day
        if inventory_summary is not None:
            self._state.inventory_summary = inventory_summary

        # 同步到 Reflex
        if self._motor:
            self._motor.update_reflex_state(
                health=self._state.health,
                hunger=self._state.hunger,
            )

    def get_bodily_feeling(self) -> str:
        """获取身体感受（第一人称主观描述）。

        这包括意识层的感受和身体本能的警告，但不会自动执行任何动作。
        """
        feelings = []

        # 健康状况（包含身体本能的紧急感）
        if self._state.health < 4:
            feelings.append("我现在很疼，身体状况很不好！身体本能在强烈警告我")
        elif self._state.health < 10:
            feelings.append("我感觉有点疼，需要小心")
        elif self._state.health < 15:
            feelings.append("有些轻微的疼痛，但还好")
        else:
            feelings.append("我的身体状况不错")

        # 饥饿感（包含身体本能的需求）
        if self._state.hunger < 4:
            feelings.append("我好饿，身体在强烈提示我需要马上吃点东西")
        elif self._state.hunger < 10:
            feelings.append("我有点饿了，想吃点东西")
        elif self._state.hunger < 15:
            feelings.append("有点饿，但还能坚持")

        # 时间感知
        time_feelings = {
            "白天": "阳光很温暖",
            "傍晚": "夕阳很美，我喜欢这个时刻",
            "夜晚": "天黑了，有点不安",
            "深夜": "夜深了，我有点困",
        }
        if self._state.time_of_day in time_feelings:
            feelings.append(time_feelings[self._state.time_of_day])

        return "、".join(feelings) if feelings else "我感觉还好"

    def get_visual_context(self) -> str:
        """获取视觉环境描述（需要结合截图分析）。"""
        # 这里需要调用视觉模型分析截图，暂时返回基础信息
        context = f"现在是{self._state.time_of_day}"
        if self._state.inventory_summary:
            context += f"，我的背包里有：{self._state.inventory_summary}"
        return context

    def get_social_context(self) -> str:
        """获取社交环境（其他玩家的存在）。"""
        return self._social.get_social_context()

    async def process_chat_message(self, player_name: str, message: str) -> dict[str, Any]:
        """处理收到的聊天消息。"""
        # 记录聊天
        chat_event = self._chat.add_incoming_chat(player_name, message)

        # 更新玩家信息
        self._social.update_player(player_name)

        # 获取回应上下文（不是判断，只是提供信息）
        response_context = self._social.get_response_context(player_name, message)

        result = {
            "perception": chat_event["perception"],
            "response_context": response_context,  # 改为提供上下文，而非判断
            "chat_event": chat_event,
        }

        # 注意：是否回应完全由她的意识层决定
        # 这里不做任何判断或建议

        return result

    async def send_chat(self, message: str) -> bool:
        """发送聊天消息。"""
        return await self._chat.send_chat(message, self._input)

    # === 日志事件处理 ===

    async def _process_log_events_loop(self) -> None:
        """后台任务：持续处理游戏日志事件，更新感知状态。"""
        while True:
            try:
                await asyncio.sleep(1.0)
                events = self._log_reader.drain_events()
                for event in events:
                    await self._handle_log_event(event)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.debug(f"日志事件处理异常: {exc}")

    async def _handle_log_event(self, event) -> None:
        """处理单条日志事件，更新感知和社交状态。"""
        from .log_reader import LogEvent

        if event.type == "chat" and event.player:
            # 玩家聊天 → 更新社交感知
            await self.process_chat_message(event.player, event.message)
            self._state.events.append(
                f"[{event.timestamp}] <{event.player}> {event.message}"
            )
            logger.debug(f"游戏内聊天: <{event.player}> {event.message}")

        elif event.type == "join":
            # 玩家进入 → 更新社交状态
            self._social.update_player(event.player)
            self._state.events.append(f"[{event.timestamp}] {event.message}")
            logger.info(f"玩家进入: {event.player}")

        elif event.type == "leave":
            self._state.events.append(f"[{event.timestamp}] {event.message}")
            logger.info(f"玩家离开: {event.player}")

        elif event.type == "death":
            self._state.events.append(f"[{event.timestamp}] {event.message}")
            if event.player and event.player.lower() == self._mc_config.offline_username.lower():
                # 爱莉自己死了 → 更新状态（视角感知，不自动操作）
                self._state.health = 0.0
                logger.info("爱莉在游戏中死亡了")

        elif event.type == "world_loaded":
            logger.info("Minecraft 世界已加载完毕")

        elif event.type == "world_closed":
            logger.info("Minecraft 世界正在关闭")

    def set_goal(self, goal: str) -> None:
        """设置当前目标。"""
        self._state.current_goal = goal

    def add_event(self, event: str) -> None:
        """记录事件。"""
        self._state.events.append(event)

    def add_item_gained(self, item: str) -> None:
        """记录获得的物品。"""
        self._state.items_gained.append(item)

    # === 持久化 ===

    async def _save_session_log(self) -> None:
        """保存会话日志。"""
        log_dir = self._workspace / "minecraft" / "sessions"
        await asyncio.to_thread(log_dir.mkdir, parents=True, exist_ok=True)

        log_file = log_dir / f"session_{self._state.session_id}.json"
        data = {
            "session_id": self._state.session_id,
            "start_time": self._state.start_time,
            "duration_minutes": self._state.duration_minutes,
            "goals_completed": self._state.goals_completed,
            "events": self._state.events,
            "items_gained": self._state.items_gained,
            "log": self._session_log,
        }

        try:
            log_file.write_text(json.dumps(data, ensure_ascii=False, indent=2))
            logger.info(f"会话日志已保存: {log_file}")
        except Exception as exc:
            logger.warning(f"保存会话日志失败: {exc}")

    async def get_status(self) -> dict[str, Any]:
        """获取当前状态。"""
        return {
            "active": self._state.active,
            "session_id": self._state.session_id,
            "duration_minutes": self._state.duration_minutes,
            "current_goal": self._state.current_goal,
            "goals_completed": self._state.goals_completed,
            "health": self._state.health,
            "hunger": self._state.hunger,
            "last_result": self._state.last_result,
            "mc_running": self._launcher.is_running,
        }
