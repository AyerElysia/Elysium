"""元事件处理器"""
from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

from src.app.plugin_system.api.log_api import get_logger
from src.kernel.concurrency import get_task_manager

from ...event_models import MetaEventType

if TYPE_CHECKING:
    from ....plugin import NapcatAdapter

logger = get_logger("napcat_adapter")


class MetaEventHandler:
    """处理 Napcat 元事件（心跳、生命周期）"""

    def __init__(self, adapter: "NapcatAdapter"):
        self.adapter = adapter
        self.plugin_config: dict[str, Any] | None = None
        self.last_heart_beat = 0.0
        self.interval = 30.0
        self._interval_checking = False
        self._heartbeat_task: Any | None = None
        self._reconnecting = False

    def set_plugin_config(self, config: dict[str, Any]) -> None:
        """设置插件配置"""
        self.plugin_config = config

    async def handle_meta_event(self, raw: dict[str, Any]):
        event_type = raw.get("meta_event_type")
        if event_type == MetaEventType.lifecycle:
            sub_type = raw.get("sub_type")
            if sub_type == MetaEventType.Lifecycle.connect:
                self_id = raw.get("self_id")
                self.last_heart_beat = time.time()
                logger.info(f"Bot {self_id} 连接成功")
                # 不在连接时立即启动心跳检查，等第一个心跳包到达后再启动
        elif event_type == MetaEventType.heartbeat:
            if raw["status"].get("online") and raw["status"].get("good"):
                self_id = raw.get("self_id")
                interval = raw.get("interval")
                if interval:
                    self.interval = interval / 1000
                if not self._interval_checking and self_id:
                    # 第一次收到心跳包时才启动心跳检查
                    tm = get_task_manager()
                    self._heartbeat_task = tm.create_task(
                        self.check_heartbeat(self_id),
                        name="napcat_adapter_heartbeat_check",
                        daemon=True,
                    )
                self.last_heart_beat = time.time()
            else:
                self_id = raw.get("self_id")
                logger.warning(f"Bot {self_id} Napcat 端异常！")
                await self._reconnect_after_heartbeat_failure(
                    self_id,
                    "心跳状态异常",
                )

    async def _reconnect_after_heartbeat_failure(
        self,
        bot_id: int | None,
        reason: str,
    ) -> None:
        """在心跳异常后触发一次重连。"""
        if self._reconnecting:
            return

        self._reconnecting = True
        try:
            logger.error(f"Bot {bot_id} 检测到 Napcat 连接异常，开始重连：{reason}")
            await self.adapter.reconnect()
        except Exception as e:
            logger.error(f"Bot {bot_id} Napcat 自动重连失败: {e}")
        finally:
            self._reconnecting = False

    async def check_heartbeat(self, id: int) -> None:
        self._interval_checking = True
        try:
            while True:
                now_time = time.time()
                if now_time - self.last_heart_beat > self.interval * 2:
                    await self._reconnect_after_heartbeat_failure(
                        id,
                        "心跳超时",
                    )
                    break
                await asyncio.sleep(self.interval)
        finally:
            self._interval_checking = False
            self._heartbeat_task = None
