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
    from ..config import NapcatAdapterConfig

logger = get_logger("napcat_adapter")


class MetaEventHandler:
    """处理 NapCat 元事件（心跳、生命周期）。"""

    def __init__(self, client: "NapCatClient", get_config: Any) -> None:
        self._client = client
        self._get_config = get_config

        self._last_heartbeat: float = 0.0
        self._interval: float = 30.0
        self._checking: bool = False
        self._heartbeat_task: Any | None = None
        self._reconnecting: bool = False

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
                self._last_heartbeat = time.time()
                logger.info(f"Bot {self_id} 连接成功")
            return None

        elif event_type == "heartbeat":
            status = raw.get("status", {})
            if status.get("online") and status.get("good"):
                self_id = raw.get("self_id")
                interval = raw.get("interval")
                if interval:
                    self._interval = interval / 1000

                if not self._checking and self_id:
                    # 首次收到心跳，启动心跳检查任务
                    tm = get_task_manager()
                    self._heartbeat_task = tm.create_task(
                        self._check_heartbeat_loop(self_id),
                        name="napcat_heartbeat_check",
                        daemon=True,
                    )
                self._last_heartbeat = time.time()
            else:
                self_id = raw.get("self_id")
                logger.warning(f"Bot {self_id} NapCat 端状态异常！")
                await self._trigger_reconnect(self_id, "心跳状态异常")

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
                now = time.time()
                if now - self._last_heartbeat > self._interval * 2:
                    await self._trigger_reconnect(bot_id, "心跳超时")
                    break
                await asyncio.sleep(self._interval)
        finally:
            self._checking = False
            self._heartbeat_task = None

    def stop(self) -> None:
        """停止心跳检查。"""
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            self._heartbeat_task = None
        self._checking = False
