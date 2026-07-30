"""直播意识实例管理器。

将直播注册为 ConsciousnessInstance（kind="livestream"），
通过 WorldState 报告直播状态，实现跨场景感知。

生命周期：start → active → suspend
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..config import LivestreamConfig

logger = logging.getLogger(__name__)


class LivestreamConsciousnessManager:
    """直播意识实例管理器。

    参照 voice_live 的 VoiceLiveConsciousnessManager 模式。
    """

    def __init__(self, config: "LivestreamConfig") -> None:
        self._config = config
        self._instance: Any = None
        self._active = False
        self._registry: Any = None
        self._world_state: Any = None

    @property
    def is_active(self) -> bool:
        return self._active

    @property
    def instance_id(self) -> str:
        return "livestream_001"

    async def activate(self) -> bool:
        """注册并激活直播意识实例。"""
        try:
            from plugins.life_engine.service.consciousness import (
                ConsciousnessInstance,
                ConsciousnessRegistry,
            )
            from plugins.life_engine.service.world_state import PerceptionFilter

            # 获取全局 registry
            self._registry = ConsciousnessRegistry.get_instance()

            now = datetime.now(timezone.utc).isoformat()
            room_id = self._config.platform.room_id

            self._instance = ConsciousnessInstance(
                instance_id=self.instance_id,
                kind="livestream",
                display_name="直播意识",
                stream_ids=[f"livestream_{room_id}"],
                status="active",
                created_at=now,
                last_active_at=now,
                perception_filter=PerceptionFilter(
                    relationship_ids=[],
                    scene_ids=[],
                    thread_kinds=["topic"],
                    include_body_state=True,
                    include_commitments=False,
                ),
                metadata={
                    "platform": self._config.platform.platform_type,
                    "room_id": room_id,
                },
            )

            # 如果旧实例存在且已挂起，先终止
            existing = self._registry.get(self.instance_id)
            if existing and existing.status == "suspended":
                self._registry.terminate(self.instance_id)

            self._registry.register(self._instance)
            self._active = True
            logger.info(f"直播意识已激活: {self.instance_id} (room={room_id})")
            return True

        except Exception as exc:
            logger.error(f"直播意识激活失败: {exc}", exc_info=True)
            return False

    async def suspend(self) -> None:
        """挂起直播意识实例。"""
        if not self._active or not self._registry:
            return
        now = datetime.now(timezone.utc).isoformat()
        self._registry.suspend(self.instance_id, timestamp=now)
        self._active = False
        logger.info(f"直播意识已挂起: {self.instance_id}")

    async def report_state(self, state_text: str) -> None:
        """向 WorldState 报告直播状态。

        Args:
            state_text: 状态描述，如 "直播中，观众200人，话题：游戏"
        """
        try:
            from plugins.life_engine.service.world_state import WorldState

            ws = WorldState.get_instance()
            if ws:
                ws.update_scene_status(
                    scene_id=f"livestream_{self._config.platform.room_id}",
                    kind="livestream",
                    status_text=state_text,
                )
        except Exception as exc:
            logger.debug(f"WorldState 报告失败: {exc}")
