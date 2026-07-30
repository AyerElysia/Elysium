"""响应调度器 — 核心编排器。

状态机：idle → thinking → speaking → idle
职责：
- 从优先级队列取任务
- 调用 LLM 编排器生成回复
- 将回复送入 TTS 队列
- 控制形象状态
- 空闲检测 → 触发主动行为引擎
- 支持高优先级打断
"""

from __future__ import annotations

import asyncio
import logging
import time
from enum import Enum
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from ..platform.base import PlatformEvent
from .priority_queue import PriorityEventQueue, QueuedItem

if TYPE_CHECKING:
    from ..config import LivestreamConfig
    from ..output.avatar_controller import AvatarController
    from ..output.tts_queue import TTSQueue
    from .llm_orchestrator import LLMOrchestrator
    from .proactive import ProactiveEngine

logger = logging.getLogger(__name__)


class SchedulerState(str, Enum):
    """调度器状态。"""

    IDLE = "idle"
    THINKING = "thinking"
    SPEAKING = "speaking"


# 状态变更回调
StateChangeCallback = Callable[[SchedulerState], Awaitable[None]]


class PipelineScheduler:
    """核心管线调度器。

    编排整个互动流程：事件 → LLM → TTS → 形象。
    """

    def __init__(
        self,
        config: "LivestreamConfig",
        queue: PriorityEventQueue,
        llm: "LLMOrchestrator",
        tts: "TTSQueue",
        avatar: "AvatarController",
        proactive: "ProactiveEngine",
    ) -> None:
        self._config = config
        self._queue = queue
        self._llm = llm
        self._tts = tts
        self._avatar = avatar
        self._proactive = proactive

        self._state = SchedulerState.IDLE
        self._running = False
        self._task: asyncio.Task | None = None
        self._last_interaction_time = time.time()
        self._current_task: asyncio.Task | None = None
        self._state_callbacks: list[StateChangeCallback] = []

        # 统计
        self._total_responses = 0
        self._total_events_received = 0

    @property
    def state(self) -> SchedulerState:
        return self._state

    @property
    def stats(self) -> dict[str, Any]:
        return {
            "state": self._state.value,
            "total_responses": self._total_responses,
            "total_events_received": self._total_events_received,
            "queue_size": self._queue.size,
            "idle_seconds": time.time() - self._last_interaction_time,
        }

    def on_state_change(self, callback: StateChangeCallback) -> None:
        """注册状态变更回调。"""
        self._state_callbacks.append(callback)

    async def start(self) -> None:
        """启动调度器主循环。"""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._main_loop())
        logger.info("管线调度器已启动")

    async def stop(self) -> None:
        """停止调度器。"""
        self._running = False
        if self._current_task:
            self._current_task.cancel()
            try:
                await self._current_task
            except asyncio.CancelledError:
                pass
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        await self._set_state(SchedulerState.IDLE)
        logger.info("管线调度器已停止")

    async def handle_event(self, event: PlatformEvent) -> None:
        """接收平台事件（由平台适配器回调）。"""
        self._total_events_received += 1
        self._last_interaction_time = time.time()
        await self._queue.push(event)

    async def interrupt(self) -> None:
        """打断当前发言（高优先级事件触发）。"""
        if self._state == SchedulerState.SPEAKING:
            await self._tts.clear()
            if self._current_task:
                self._current_task.cancel()
            logger.info("当前发言被打断")

    async def _main_loop(self) -> None:
        """主调度循环。"""
        while self._running:
            try:
                await asyncio.sleep(0.1)  # 100ms tick

                # 检查弹幕聚合窗口
                if self._queue.danmaku_buffer_ready:
                    await self._queue.flush_danmaku_buffer()

                # 如果正在处理，跳过
                if self._state != SchedulerState.IDLE:
                    continue

                # 速率限制检查
                if not self._queue.can_respond():
                    continue

                # 尝试取任务
                item = await self._queue.pop()
                if item:
                    self._current_task = asyncio.create_task(
                        self._process_item(item)
                    )
                    continue

                # 空闲检测 → 主动行为
                idle_seconds = time.time() - self._last_interaction_time
                if idle_seconds >= self._config.proactive.idle_timeout_seconds:
                    proactive_text = await self._proactive.get_idle_action()
                    if proactive_text:
                        self._current_task = asyncio.create_task(
                            self._speak_text(proactive_text)
                        )
                        self._last_interaction_time = time.time()

            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error(f"调度循环异常: {exc}", exc_info=True)
                await asyncio.sleep(1.0)

    async def _process_item(self, item: QueuedItem) -> None:
        """处理单个队列条目。"""
        try:
            # 高优先级打断当前发言
            if item.priority <= 1 and self._state == SchedulerState.SPEAKING:
                await self.interrupt()

            await self._set_state(SchedulerState.THINKING)

            # 构建 LLM 输入
            context_events = self._build_context_from_item(item)

            # 调用 LLM 生成回复（流式）
            response_text = await self._llm.generate_response(
                events=context_events,
                event_kind=item.event.kind,
            )

            if not response_text:
                await self._set_state(SchedulerState.IDLE)
                return

            # 记录响应
            self._queue.record_response()
            self._total_responses += 1
            self._last_interaction_time = time.time()

            # 送入 TTS + 形象控制
            await self._speak_text(response_text)

        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.error(f"处理队列条目异常: {exc}", exc_info=True)
        finally:
            if self._state != SchedulerState.IDLE:
                await self._set_state(SchedulerState.IDLE)

    async def _speak_text(self, text: str) -> None:
        """将文本送入 TTS 播放并控制形象。"""
        await self._set_state(SchedulerState.SPEAKING)
        await self._avatar.set_expression("neutral")
        await self._tts.speak(text)
        # TTS 播放完成后回到 idle（由 tts_queue 的完成回调触发）

    def _build_context_from_item(self, item: QueuedItem) -> list[PlatformEvent]:
        """从队列条目构建 LLM 上下文事件列表。"""
        if item.batch:
            return item.batch
        return [item.event]

    async def _set_state(self, new_state: SchedulerState) -> None:
        """设置状态并通知回调。"""
        if self._state == new_state:
            return
        self._state = new_state
        for callback in self._state_callbacks:
            try:
                await callback(new_state)
            except Exception as exc:
                logger.warning(f"状态回调异常: {exc}")
