"""日记插件入口。"""

from __future__ import annotations

from src.app.plugin_system.base import BasePlugin, register_plugin
from src.kernel.logger import get_logger

from .action import WriteDiaryAction
from .config import DiaryConfig
from .event_handler import AutoDiaryEventHandler, ContinuousMemoryPromptInjector
from .prompts import build_diary_actor_reminder
from .service import DiaryService
from .tool import ReadDiaryTool


logger = get_logger("diary_plugin")


def sync_diary_actor_reminder(plugin: BasePlugin) -> str:
    """同步 diary_plugin 的 actor reminder。"""

    from src.core.prompt import get_system_reminder_store

    store = get_system_reminder_store()
    config = getattr(plugin, "config", None)
    if isinstance(config, DiaryConfig):
        bucket = config.reminder.bucket
        name = config.reminder.name
    else:
        bucket = "actor"
        name = "关于写日记"

    reminder_content = (
        build_diary_actor_reminder(config)
        if isinstance(config, DiaryConfig)
        else ""
    )

    if not reminder_content:
        store.delete(bucket, name)
        logger.debug("日记 actor reminder 已清理")
        return ""

    store.set(bucket, name=name, content=reminder_content)
    logger.debug("日记 actor reminder 已同步")
    return reminder_content


@register_plugin
class DiaryPlugin(BasePlugin):
    """日记插件。"""

    plugin_name: str = "diary_plugin"
    plugin_description: str = "日记插件 - 保留按天日记能力并新增按聊天隔离的连续记忆"
    plugin_version: str = "2.0.0"

    configs: list[type] = [DiaryConfig]
    dependent_components: list[str] = []

    def get_components(self) -> list[type]:
        """返回本插件提供的组件类。"""

        components: list[type] = []
        if isinstance(self.config, DiaryConfig) and not self.config.plugin.enabled:
            logger.info("日记插件已在配置中禁用")
            return components

        components.extend(
            [
                DiaryService,
                ReadDiaryTool,
                WriteDiaryAction,
                AutoDiaryEventHandler,
                ContinuousMemoryPromptInjector,
            ]
        )
        return components

    async def on_plugin_loaded(self) -> None:
        """插件加载后的初始化。"""

        sync_diary_actor_reminder(self)
        logger.info("日记插件已加载")

    async def on_plugin_unloaded(self) -> None:
        """插件卸载前的清理。"""

        from src.core.prompt import get_system_reminder_store

        store = get_system_reminder_store()
        config = getattr(self, "config", None)
        if isinstance(config, DiaryConfig):
            bucket = config.reminder.bucket
            name = config.reminder.name
        else:
            bucket = "actor"
            name = "关于写日记"

        store.delete(bucket, name)
        logger.debug("日记 actor reminder 已清理")
        logger.info("日记插件已卸载")
