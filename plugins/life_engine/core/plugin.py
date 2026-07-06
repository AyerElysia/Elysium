"""life_engine 插件入口。"""

from __future__ import annotations

from src.app.plugin_system.base import BasePlugin, register_plugin
from src.kernel.logger import get_logger

from ..service.audit import (
    get_life_log_file,
    log_lifecycle,
    setup_life_audit_logger,
    teardown_life_audit_logger,
)
from ..service.command_handler import LifeEngineCommandHandler
from .config import LifeEngineConfig
from ..service.event_handler import LifeEngineMessageCollectorHandler
from ..service import LifeEngineService
from ..tools import ALL_TOOLS
from ..tools.todo_tools import TODO_TOOLS
from ..memory.tools import MEMORY_TOOLS
from ..tools.grep_tools import GREP_TOOLS
from ..tools.web_tools import WEB_TOOLS
from ..tools.schedule_tools import SCHEDULE_TOOLS
from ..tools.autonomy_tools import AUTONOMY_TOOLS
from ..tools.skill_tools import SKILL_TOOLS
from ..tools.event_grep_tools import EVENT_GREP_TOOLS
from .compat_tools import (
    LifeThinkAction,
    LifeRecordInnerMonologueAction,
    LifeScheduleFollowupMessageAction,
    LifeMessageNucleusTool,
    LifeConsultNucleusTool,
)
from ..agents import register_builtin_agents


logger = get_logger("life_engine", display="life_engine")


@register_plugin
class LifeEnginePlugin(BasePlugin):
    """生命中枢插件。

    提供同一主体的内外运行模式切换能力，使用统一的事件流模型处理
    消息、心跳、工具调用等交互，保持时间连续性。

    特性：
    - 统一事件流：所有交互都是事件，按时间顺序展示
    - 文件系统操作：提供限定在 workspace 内的文件操作工具
    - Grep 搜索：在私人文件系统中搜索内容
    - TODO 系统：为数字生命设计的生活愿望系统
    - 仿生记忆系统：语义检索、联想、遗忘机制
    - 子代理系统：启动独立代理处理复杂任务
    - 可配置可见事件数：通过 context_history_max_events 控制
    """

    plugin_name: str = "life_engine"
    plugin_description: str = "生命中枢，维护并行心跳与统一事件流上下文"
    plugin_version: str = "3.4.0"

    configs: list[type] = [LifeEngineConfig]
    dependent_components: list[str] = []

    def __init__(self, config: LifeEngineConfig | None = None) -> None:
        super().__init__(config)
        self._service: LifeEngineService | None = None

    @property
    def service(self) -> LifeEngineService:
        """获取插件内部服务实例。"""
        if self._service is None:
            self._service = LifeEngineService(self)
        return self._service

    def get_components(self) -> list[type]:
        """返回插件提供的组件。"""
        from ..snn.router import SNNRouter
        from ..memory.router import MemoryRouter
        from ..dream.router import DreamRouter
        from ..monitor.router import MessageTimelineRouter

        components: list[type] = [
            LifeEngineService,
            LifeEngineMessageCollectorHandler,
            LifeEngineCommandHandler,
            SNNRouter,
            MemoryRouter,
            DreamRouter,
            MessageTimelineRouter,
            *ALL_TOOLS,
            *TODO_TOOLS,
            *MEMORY_TOOLS,
            *GREP_TOOLS,
            *WEB_TOOLS,
            *SCHEDULE_TOOLS,
            *AUTONOMY_TOOLS,
            *SKILL_TOOLS,
            *EVENT_GREP_TOOLS,
        ]

        # 启用 LifeChatter 时注册对话器及其专用 Action / Tool
        if isinstance(self.config, LifeEngineConfig) and getattr(
            getattr(self.config, "chatter", None), "enabled", False
        ):
            from .chatter import (
                LifeChatter,
                LifeInspectMediaTool,
                LifePassAndWaitAction,
                LifeSendFileAction,
                LifeSendTextAction,
            )

            components.extend([
                LifeChatter,
                LifeSendTextAction,
                LifeSendFileAction,
                LifeInspectMediaTool,
                LifePassAndWaitAction,
                LifeThinkAction,
                LifeRecordInnerMonologueAction,
                LifeScheduleFollowupMessageAction,
                LifeMessageNucleusTool,
                LifeConsultNucleusTool,
            ])

        return components

    async def on_plugin_loaded(self) -> None:
        """插件加载后启动心跳。"""
        register_builtin_agents()
        setup_life_audit_logger()
        if isinstance(self.config, LifeEngineConfig) and not self.config.settings.enabled:
            logger.info("life_engine 已禁用，未启动")
            log_lifecycle(
                "disabled",
                enabled=False,
                model_task_name=self.config.model.task_name,
                log_file_path=str(get_life_log_file()),
            )
            await self.service.clear_runtime_context()
            return

        await self.service.start()

    async def on_plugin_unloaded(self) -> None:
        """插件卸载前停止心跳。"""
        if self._service is not None:
            await self._service.stop()
        try:
            from .chatter import LifeChatter

            LifeChatter.reset_global_runtime()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"重置 chatter 全局运行时失败: {exc}")
        log_lifecycle(
            "unloaded",
            log_file_path=str(get_life_log_file()),
        )
        teardown_life_audit_logger()
