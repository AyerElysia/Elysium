"""Voice Live 意识实例管理。

语音通话定义为独立意识实例（kind="voice_live"），
通过潜意识（WorldState + ConsciousnessRegistry）与其他意识协同。

参照 life_engine/livestream/__init__.py 的 LivestreamConsciousnessManager 模式。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class VoiceLiveConsciousnessConfig:
    """语音通话意识实例配置。"""

    instance_id: str = "voice_live_001"
    display_name: str = "语音通话意识"
    stream_id: str = "voice_live_main"
    cross_scene_awareness: bool = True


def build_voice_live_perception_filter() -> Any:
    """构建语音通话意识的感知过滤器。

    语音通话意识：
    - 看到所有关系（需要知道在和谁说话）
    - 看到身体状态（保持人格一致性）
    - 不需要承诺细节（通话中不处理任务）
    - 看到跨场景摘要
    """
    from plugins.life_engine.service.world_state import PerceptionFilter

    return PerceptionFilter(
        relationship_ids=[],  # 空 = 所有关系
        scene_ids=[],  # 空 = 所有场景
        thread_kinds=["topic"],  # 只要活跃话题
        include_body_state=True,
        include_commitments=False,  # 通话不需要承诺细节
    )


class VoiceLiveConsciousnessManager:
    """管理语音通话意识实例的生命周期。

    用法：
        manager = VoiceLiveConsciousnessManager(config)
        await manager.activate()   # 通话开始时注册并激活
        ...通话进行中...
        await manager.suspend()    # 通话结束时挂起
    """

    def __init__(self, config: VoiceLiveConsciousnessConfig | None = None) -> None:
        self._config = config or VoiceLiveConsciousnessConfig()
        self._instance: Any = None
        self._active = False
        self._registry: Any = None

    @property
    def is_active(self) -> bool:
        return self._active

    @property
    def instance_id(self) -> str:
        return self._config.instance_id

    def _get_registry(self) -> Any:
        """获取 ConsciousnessRegistry 实例。"""
        if self._registry is not None:
            return self._registry
        try:
            from plugins.life_engine.service.consciousness import ConsciousnessRegistry
            # 尝试从 life_engine 插件获取 registry
            from src.core.managers import get_plugin_manager
            plugin = get_plugin_manager().get_plugin("life_engine")
            if plugin:
                registry = getattr(plugin, "consciousness_registry", None)
                if registry:
                    self._registry = registry
                    return registry
        except Exception:  # noqa: BLE001
            pass
        return None

    async def activate(self) -> bool:
        """注册并激活语音通话意识实例。"""
        if self._active:
            return True

        registry = self._get_registry()
        if registry is None:
            logger.debug("ConsciousnessRegistry 不可用，跳过意识注册")
            return False

        try:
            from plugins.life_engine.service.consciousness import ConsciousnessInstance

            now = datetime.now(timezone.utc).isoformat()
            perception_filter = build_voice_live_perception_filter()

            self._instance = ConsciousnessInstance(
                instance_id=self._config.instance_id,
                kind="voice_live",
                display_name=self._config.display_name,
                stream_ids=[self._config.stream_id],
                status="active",
                created_at=now,
                last_active_at=now,
                perception_filter=perception_filter,
                metadata={
                    "cross_scene_awareness": self._config.cross_scene_awareness,
                    "type": "voice_call",
                },
            )

            # 如果存在同 ID 的旧实例，先终止
            existing = registry.get(self._config.instance_id)
            if existing and existing.status == "suspended":
                registry.terminate(self._config.instance_id)

            registry.register(self._instance)
            self._active = True
            logger.info(f"语音通话意识已激活: {self._config.instance_id}")
            return True

        except Exception as exc:  # noqa: BLE001
            logger.error(f"意识实例注册失败: {exc}")
            return False

    async def suspend(self) -> None:
        """挂起语音通话意识实例。"""
        if not self._active:
            return

        registry = self._get_registry()
        if registry:
            try:
                now = datetime.now(timezone.utc).isoformat()
                registry.suspend(self._config.instance_id, timestamp=now)
            except Exception:  # noqa: BLE001
                pass

        self._active = False
        self._instance = None
        logger.info(f"语音通话意识已挂起: {self._config.instance_id}")

    async def report_state(self, state_text: str) -> None:
        """向 WorldState 报告通话状态。

        例如："正在和用户语音通话中"
        """
        registry = self._get_registry()
        if not registry or not self._instance:
            return

        try:
            from plugins.life_engine.service.world_state import WorldState
            # 通过 life_engine 获取 world_state
            from src.core.managers import get_plugin_manager
            plugin = get_plugin_manager().get_plugin("life_engine")
            if plugin:
                world_state = getattr(plugin, "world_state", None)
                if world_state and hasattr(world_state, "update_scene_status"):
                    world_state.update_scene_status(
                        self._config.instance_id,
                        state_text,
                    )
        except Exception:  # noqa: BLE001
            pass

    def render_perception(self, world_state: Any) -> str:
        """渲染 WorldState 切片供语音通话上下文使用。"""
        if self._instance is None:
            return ""
        try:
            return world_state.render_for_perception(
                self._instance.perception_filter,
                max_chars=1500,
            )
        except Exception:  # noqa: BLE001
            return ""
