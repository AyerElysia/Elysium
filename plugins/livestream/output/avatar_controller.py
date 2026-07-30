"""形象控制器。

控制 Live2D 虚拟形象的表情、口型和动作：
- 表情映射：根据情绪标签切换表情
- 口型同步：基于 TTS 音频时长驱动
- 指令协议：JSON 帧 → 前端 Live2D 渲染器
- 空闲动画：眨眼、呼吸、随机小动作
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import TYPE_CHECKING, Any, Awaitable, Callable

if TYPE_CHECKING:
    from ..config import LivestreamConfig

logger = logging.getLogger(__name__)

# 形象指令回调：(command_dict)
AvatarCommandCallback = Callable[[dict[str, Any]], Awaitable[None]]


class AvatarController:
    """Live2D 形象控制器。"""

    def __init__(self, config: "LivestreamConfig") -> None:
        avatar_cfg = config.avatar
        self._expression_mapping = avatar_cfg.expression_mapping
        self._idle_animation_enabled = avatar_cfg.idle_animation_enabled

        self._command_callback: AvatarCommandCallback | None = None
        self._idle_task: asyncio.Task | None = None
        self._running = False
        self._speaking = False
        self._current_expression = "neutral"

    def on_command(self, callback: AvatarCommandCallback) -> None:
        """注册指令回调（发送到前端 WebSocket）。"""
        self._command_callback = callback

    async def start(self) -> None:
        """启动形象控制器（含空闲动画循环）。"""
        self._running = True
        if self._idle_animation_enabled:
            self._idle_task = asyncio.create_task(self._idle_animation_loop())
        logger.info("形象控制器已启动")

    async def stop(self) -> None:
        """停止形象控制器。"""
        self._running = False
        if self._idle_task:
            self._idle_task.cancel()
            try:
                await self._idle_task
            except asyncio.CancelledError:
                pass
        logger.info("形象控制器已停止")

    async def set_expression(self, emotion: str) -> None:
        """设置表情。

        Args:
            emotion: 情绪标签（happy/sad/angry/surprised/neutral）。
        """
        param_name = self._expression_mapping.get(emotion, "exp_00")
        self._current_expression = emotion
        await self._send_command({
            "type": "expression",
            "emotion": emotion,
            "param": param_name,
            "timestamp": time.time(),
        })

    async def set_speaking(self, speaking: bool) -> None:
        """设置口型状态。

        Args:
            speaking: 是否正在说话。
        """
        self._speaking = speaking
        await self._send_command({
            "type": "mouth",
            "open": speaking,
            "value": 1.0 if speaking else 0.0,
            "timestamp": time.time(),
        })

    async def set_mouth_value(self, value: float) -> None:
        """设置口型开合度（0.0~1.0）。

        用于精细口型同步。
        """
        await self._send_command({
            "type": "mouth",
            "open": value > 0.1,
            "value": max(0.0, min(1.0, value)),
            "timestamp": time.time(),
        })

    async def trigger_action(self, action: str) -> None:
        """触发动作（点头、摇头、挥手等）。"""
        await self._send_command({
            "type": "action",
            "action": action,
            "timestamp": time.time(),
        })

    async def _send_command(self, command: dict[str, Any]) -> None:
        """发送指令到前端。"""
        if self._command_callback:
            try:
                await self._command_callback(command)
            except Exception as exc:
                logger.warning(f"形象指令发送失败: {exc}")

    async def _idle_animation_loop(self) -> None:
        """空闲动画循环：眨眼、呼吸、随机小动作。"""
        while self._running:
            try:
                # 随机间隔 3-8 秒
                await asyncio.sleep(random.uniform(3.0, 8.0))

                if not self._running or self._speaking:
                    continue

                # 随机选择空闲动画
                action = random.choice([
                    "blink",
                    "blink",
                    "blink",  # 眨眼概率更高
                    "breath",
                    "look_around",
                    "tilt_head",
                ])

                await self._send_command({
                    "type": "idle_animation",
                    "action": action,
                    "timestamp": time.time(),
                })

            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning(f"空闲动画异常: {exc}")
                await asyncio.sleep(5.0)
