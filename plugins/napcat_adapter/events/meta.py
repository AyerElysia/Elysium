"""元事件处理器

处理 meta_event 事件：
- lifecycle.connect: WebSocket 连接成功
- heartbeat: 心跳监控 + 超时重连
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

from src.app.plugin_system.api.log_api import get_logger
from src.kernel.concurrency import get_task_manager

if TYPE_CHECKING:
    from ..client import NapCatClient

logger = get_logger("napcat_adapter")


class MetaEventHandler:
    """处理 NapCat 元事件（心跳、生命周期）。"""

    _HEARTBEAT_TIMEOUT_MULTIPLIER = 3.0
    _MIN_HEARTBEAT_TIMEOUT_SECONDS = 45.0

    def __init__(self, client: "NapCatClient", get_config: Any) -> None:
        self._client = client
        self._get_config = get_config

        self._last_heartbeat: float = 0.0
        self._interval: float = 30.0
        self._checking: bool = False
        self._heartbeat_task: Any | None = None
        self._reconnecting: bool = False
        self._reported_status_degraded: bool = False

        # 由 adapter 注入的重连回调
        self._reconnect_callback: Any | None = None

    def set_reconnect_callback(self, callback: Any) -> None:
        """设置重连回调（由 adapter 注入）。"""
        self._reconnect_callback = callback

    async def handle(self, raw: dict[str, Any]) -> Any:
        """处理 meta_event 事件。"""
        event_type = raw.get("meta_event_type")

        if event_type == "lifecycle":
            sub_type = raw.get("sub_type")
            if sub_type == "connect":
                self_id = raw.get("self_id")
                self._last_heartbeat = time.monotonic()
                self._reported_status_degraded = False
                logger.info(f"Bot {self_id} 连接成功")
            return None

        elif event_type == "heartbeat":
            status = raw.get("status", {})
            if not isinstance(status, dict):
                status = {}
            self_id = raw.get("self_id")
            interval = raw.get("interval")
            if interval:
                try:
                    parsed_interval = float(interval) / 1000.0
                except (TypeError, ValueError):
                    parsed_interval = 0.0
                if parsed_interval > 0:
                    self._interval = parsed_interval

            # Every heartbeat proves that the Elysium <-> NapCat WebSocket is
            # alive. ``status.online`` is an advisory QQ session field and is
            # known to be false even while OneBot APIs and message transport are
            # healthy; it must never restart the transport by itself.
            self._last_heartbeat = time.monotonic()
            if not self._checking and self_id:
                # Claim ownership before scheduling so back-to-back heartbeat
                # events cannot create duplicate checker tasks.
                self._checking = True
                tm = get_task_manager()
                self._heartbeat_task = tm.create_task(
                    self._check_heartbeat_loop(self_id),
                    name="napcat_heartbeat_check",
                    daemon=True,
                )

            status_degraded = (
                status.get("online") is False or status.get("good") is False
            )
            if status_degraded and not self._reported_status_degraded:
                logger.warning(
                    f"Bot {self_id} NapCat 上报会话状态降级 "
                    f"(online={status.get('online')}, good={status.get('good')})；"
                    "WebSocket 心跳仍正常，本次不重启适配器"
                )
                self._reported_status_degraded = True
            elif not status_degraded and self._reported_status_degraded:
                logger.info(f"Bot {self_id} NapCat 上报会话状态已恢复")
                self._reported_status_degraded = False

            return None

        return None

    async def _trigger_reconnect(self, bot_id: Any, reason: str) -> None:
        """触发重连。"""
        if self._reconnecting:
            return

        self._reconnecting = True
        try:
            logger.error(f"Bot {bot_id} 检测到连接异常，开始重连：{reason}")
            if self._reconnect_callback:
                await self._reconnect_callback()
        except Exception as e:
            logger.error(f"Bot {bot_id} 自动重连失败: {e}")
        finally:
            self._reconnecting = False

    async def _check_heartbeat_loop(self, bot_id: int) -> None:
        """心跳超时检查循环。"""
        self._checking = True
        try:
            while True:
                await asyncio.sleep(max(self._interval, 1.0))
                now = time.monotonic()
                if now - self._last_heartbeat > self._heartbeat_timeout_seconds():
                    # Reconnect from a separate owned task. Calling reconnect()
                    # inside the checker would cancel the checker from stop(),
                    # interrupting the lifecycle halfway through.
                    tm = get_task_manager()
                    tm.create_task(
                        self._trigger_reconnect(bot_id, "OneBot 心跳超时"),
                        name="napcat_heartbeat_reconnect",
                        daemon=True,
                    )
                    break
        finally:
            self._checking = False
            self._heartbeat_task = None

    def _heartbeat_timeout_seconds(self) -> float:
        return max(
            self._MIN_HEARTBEAT_TIMEOUT_SECONDS,
            self._interval * self._HEARTBEAT_TIMEOUT_MULTIPLIER,
        )

    def stop(self) -> None:
        """停止心跳检查。"""
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            self._heartbeat_task = None
        self._checking = False
        self._last_heartbeat = 0.0
        self._reported_status_degraded = False
