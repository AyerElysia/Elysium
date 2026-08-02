"""运动控制循环 + Reflex 保护层。

motor_loop: 对话式运动控制（她用自然语言指挥身体）
reflex: 独立快速感知增强循环（不自动执行，只强化感知）
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any


from .capture import WindowCapture
from .conversational_motor import ConversationalMotorController, create_conversational_motor
from .input_control import InputController

logger = logging.getLogger("life_engine.minecraft.motor")


@dataclass(slots=True)
class ExecutionReport:
    """意图执行报告。"""

    success: bool
    intent: str = ""
    steps: int = 0
    duration_seconds: float = 0.0
    reason: str = ""
    last_action: str = ""
    feeling: str = ""  # 她的第一人称感受


@dataclass(slots=True)
class ReflexState:
    """Reflex 层状态（感知增强，不自动执行）。"""

    health: float = 20.0
    hunger: float = 20.0
    in_danger: bool = False
    falling: bool = False
    last_damage_time: float = 0.0


class MotorLoop:
    """运动控制循环引擎。

    接收意识层的意图，通过对话式控制器执行动作，
    并提供第一人称感受反馈。
    """

    def __init__(
        self,
        capture: WindowCapture,
        input_ctrl: InputController,
        conversational_motor: ConversationalMotorController | None = None,
        llm_helper: Any = None,  # 可选的 LLM 辅助解析意图
    ) -> None:
        self._capture = capture
        self._input = input_ctrl
        self._motor = conversational_motor or create_conversational_motor(input_ctrl)
        self._llm_helper = llm_helper

        self._running = False
        self._current_intent: str = ""
        self._interrupted = False

        # Reflex（感知增强，不自动执行）
        self._reflex_enabled = True
        self._reflex_task: asyncio.Task | None = None
        self._reflex_state = ReflexState()

    @property
    def is_executing(self) -> bool:
        return self._running

    @property
    def current_intent(self) -> str:
        return self._current_intent

    async def execute_intent(
        self,
        intent: str,
        timeout: float | None = None,
    ) -> ExecutionReport:
        """执行她的自然语言意图。

        Args:
            intent: 她的意图，如 "我想往 Ayer 那边走几步"
            timeout: 超时时间（暂未使用，因为对话式控制是即时的）

        Returns:
            ExecutionReport 包含执行结果和她的感受
        """
        if self._running:
            return ExecutionReport(
                success=False,
                intent=intent,
                reason="还在执行上一个意图",
            )

        self._running = True
        self._current_intent = intent
        self._interrupted = False

        try:
            logger.info(f"执行意图: {intent}")
            
            # 对话式控制器执行
            feedback = await self._motor.execute_intent(intent, self._llm_helper)

            report = ExecutionReport(
                success=feedback.success,
                intent=intent,
                steps=len(feedback.actions_taken),
                duration_seconds=feedback.duration_seconds,
                reason="completed" if feedback.success else "failed",
                last_action=feedback.feeling,
                feeling=feedback.feeling,
            )

            logger.info(
                f"意图执行{'成功' if report.success else '失败'}: "
                f"{report.steps}步, {report.duration_seconds:.1f}s, {report.feeling}"
            )
            return report

        except Exception as exc:
            logger.error(f"意图执行异常: {exc}")
            return ExecutionReport(
                success=False,
                intent=intent,
                reason=f"exception: {exc}",
            )
        finally:
            self._running = False
            self._current_intent = ""

    def interrupt(self) -> None:
        """中断当前执行（她改变主意了）。"""
        if self._running:
            self._interrupted = True
            self._motor.interrupt()
            logger.info("意图执行被中断")

    # ═══ Reflex 保护层（感知增强，不自动执行）═════════════════

    async def start_reflex_loop(self) -> None:
        """启动 Reflex 感知增强循环。"""
        if self._reflex_task and not self._reflex_task.done():
            return
        self._reflex_task = asyncio.create_task(
            self._reflex_loop(), name="mc_reflex"
        )
        logger.info("Reflex 感知增强循环已启动")

    async def stop_reflex_loop(self) -> None:
        """停止 Reflex 循环。"""
        if self._reflex_task and not self._reflex_task.done():
            self._reflex_task.cancel()
            try:
                await self._reflex_task
            except asyncio.CancelledError:
                pass
        self._reflex_task = None
        logger.info("Reflex 循环已停止")

    async def _reflex_loop(self) -> None:
        """Reflex 后台循环：检测紧急状态，增强感知（不自动执行）。"""
        while True:
            try:
                await asyncio.sleep(2.0)  # 每 2 秒检查一次
                if self._reflex_enabled:
                    await self._check_reflex()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.debug(f"Reflex 循环异常: {exc}")

    async def _check_reflex(self) -> None:
        """检查紧急状态，通过日志增强感知（不自动执行）。
        
        原则：不替她做决定，只让她更强烈地感受到身体的警告。
        她可以选择：吃东西、逃跑、继续战斗、或接受死亡。
        """
        state = self._reflex_state

        # 极低血量 → 强化疼痛感知
        if state.health < 4.0:
            logger.warning("紧急状态：血量极低，身体本能强烈警告")
            # 她会在 bodily_feeling 中感受到：
            # "我现在很疼，身体状况很不好！身体本能在强烈警告我"
            return

        # 极度饥饿 → 强化饥饿感
        if state.hunger < 4.0:
            logger.warning("紧急状态：极度饥饿，身体强烈要求进食")
            # 她会感受到："我好饿，身体在强烈提示我需要马上吃点东西"
            return

        # 正在坠落 → 强化恐惧感
        if state.falling:
            logger.warning("紧急状态：正在坠落")
            # 她会感受到恐惧和失重感
            return

        # 受到伤害 → 强化疼痛感
        if state.in_danger and time.time() - state.last_damage_time < 5.0:
            logger.warning("紧急状态：刚刚受伤")
            # 她会感受到疼痛和危险

    def update_reflex_state(
        self,
        health: float | None = None,
        hunger: float | None = None,
        in_danger: bool | None = None,
        falling: bool | None = None,
    ) -> None:
        """更新 Reflex 状态（由意识层调用）。"""
        if health is not None:
            self._reflex_state.health = health
        if hunger is not None:
            self._reflex_state.hunger = hunger
        if in_danger is not None:
            self._reflex_state.in_danger = in_danger
            if in_danger:
                self._reflex_state.last_damage_time = time.time()
        if falling is not None:
            self._reflex_state.falling = falling

    def set_reflex_enabled(self, enabled: bool) -> None:
        """启用/禁用 Reflex 感知增强。"""
        self._reflex_enabled = enabled
