"""Livestream consciousness: architecture skeleton.

This module provides the structural foundation for a dedicated livestream
consciousness instance. It registers with the ConsciousnessRegistry, has its
own PerceptionFilter, and will bind to a danmaku (bullet chat) stream.

Current status: SKELETON - architecture only. Full implementation (danmaku
parsing, OBS control, audience interaction logic) will be added when the
livestream feature is ready for production.

Architecture:
- LivestreamConsciousness registers as a ConsciousnessInstance (kind="livestream")
- It has its own rolling context (independent 320K budget)
- Its transient suffix renders from WorldState with a livestream-specific filter
- It can use action-report_state to update WorldState (e.g., "直播刚开始，观众200人")
- The subconscious coordinates cross-scene awareness via WorldState.active_scenes
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from ..service.consciousness import ConsciousnessInstance, ConsciousnessRegistry
from ..service.world_state import PerceptionFilter, WorldState

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class LivestreamConfig:
    """Configuration for a livestream consciousness instance."""

    # 直播平台
    platform: str = "bilibili"
    # 房间 ID
    room_id: str = ""
    # 弹幕流 ID（绑定到 ConsciousnessInstance.stream_ids）
    danmaku_stream_id: str = ""
    # 意识实例 ID
    instance_id: str = "livestream_001"
    # 显示名称
    display_name: str = "直播意识"
    # 是否启用跨场景感知（知道私聊/群聊的状态）
    cross_scene_awareness: bool = True
    # 最大观众互动频率（防止过度回复）
    max_responses_per_minute: int = 3
    # 是否允许观众触发话题切换
    allow_audience_topic_switch: bool = True


# ---------------------------------------------------------------------------
# Perception Filter for Livestream
# ---------------------------------------------------------------------------


def build_livestream_perception_filter(
    config: LivestreamConfig,
) -> PerceptionFilter:
    """Build the perception filter for a livestream consciousness.

    The livestream consciousness:
    - Sees all relationships (needs to know who's talking about)
    - Sees body state (maintains persona consistency)
    - Does NOT see detailed commitments (irrelevant to livestream)
    - Sees its own scene + cross-scene summaries if enabled
    """
    return PerceptionFilter(
        relationship_ids=[],  # empty = all relationships
        scene_ids=[],  # empty = all scenes (cross-scene awareness)
        thread_kinds=["topic"],  # only active topics, not commitments
        include_body_state=True,
        include_commitments=False,  # livestream doesn't need promise details
    )


# ---------------------------------------------------------------------------
# Livestream Consciousness Manager
# ---------------------------------------------------------------------------


class LivestreamConsciousnessManager:
    """Manages the lifecycle of a livestream consciousness instance.

    Usage:
        manager = LivestreamConsciousnessManager(config, registry)
        await manager.start()   # Register and activate
        ...livestream runs...
        await manager.stop()    # Suspend and release
    """

    def __init__(
        self,
        config: LivestreamConfig,
        registry: ConsciousnessRegistry,
    ) -> None:
        self._config = config
        self._registry = registry
        self._instance: ConsciousnessInstance | None = None
        self._active = False

    @property
    def is_active(self) -> bool:
        return self._active

    @property
    def instance(self) -> ConsciousnessInstance | None:
        return self._instance

    @property
    def instance_id(self) -> str:
        return self._config.instance_id

    async def start(self) -> ConsciousnessInstance:
        """Register and activate the livestream consciousness."""
        now = datetime.now(timezone.utc).isoformat()
        stream_id = self._config.danmaku_stream_id or f"livestream_{self._config.room_id}"

        self._instance = ConsciousnessInstance(
            instance_id=self._config.instance_id,
            kind="livestream",
            display_name=self._config.display_name,
            stream_ids=[stream_id],
            status="active",
            created_at=now,
            last_active_at=now,
            perception_filter=build_livestream_perception_filter(self._config),
            metadata={
                "platform": self._config.platform,
                "room_id": self._config.room_id,
                "cross_scene_awareness": self._config.cross_scene_awareness,
            },
        )

        # If an old instance with same ID exists and is suspended, terminate it first
        existing = self._registry.get(self._config.instance_id)
        if existing and existing.status == "suspended":
            self._registry.terminate(self._config.instance_id)

        self._registry.register(self._instance)
        self._active = True
        logger.info(
            f"直播意识已启动: {self._config.instance_id} "
            f"(platform={self._config.platform}, room={self._config.room_id})"
        )
        return self._instance

    async def stop(self) -> None:
        """Suspend the livestream consciousness."""
        if not self._active:
            return
        now = datetime.now(timezone.utc).isoformat()
        self._registry.suspend(self._config.instance_id, timestamp=now)
        self._active = False
        logger.info(f"直播意识已挂起: {self._config.instance_id}")

    def render_perception(self, world_state: WorldState) -> str:
        """Render the WorldState slice for this livestream consciousness."""
        if self._instance is None:
            return ""
        return world_state.render_for_perception(
            self._instance.perception_filter,
            max_chars=2000,  # Livestream needs less context per turn
        )

    def build_scene_status(self, *, viewer_count: int = 0, topic: str = "") -> str:
        """Build a scene status summary for WorldState updates."""
        parts = [f"直播中"]
        if viewer_count:
            parts.append(f"观众{viewer_count}人")
        if topic:
            parts.append(f"话题:{topic}")
        return "，".join(parts)
