"""插件协议 v2。

目标：插件只看到 Plugin + Context，不暴露框架内部。

设计原则：
- 最小接口：Context 是插件与框架的唯一交互面
- 不预设：框架不规定插件"应该做什么"，只提供"能做什么"
- 向前兼容：老插件通过 compat 层继续运行

用法：
    from src.core.plugin_protocol import Plugin, Context

    class MyPlugin(Plugin):
        name = "hello"

        async def on_load(self, ctx: Context) -> None:
            ctx.on_message(self.handle)
            ctx.register_tool(
                name="greet",
                description="打招呼",
                fn=self.greet,
            )

        async def handle(self, msg: Any, ctx: Context) -> None:
            if "你好" in str(msg):
                await ctx.reply("你好呀！")

        async def greet(self, name: str) -> str:
            return f"你好, {name}!"
"""

from __future__ import annotations

from typing import Any, Callable, Coroutine

from src.kernel.logger import get_logger

logger = get_logger("plugin_v2", display="Plugin", enable_event_broadcast=False)

# 消息处理函数签名
MessageHandler = Callable[[Any, "Context"], Coroutine[Any, Any, None]]
# 定时任务函数签名
ScheduledFn = Callable[[], Coroutine[Any, Any, None]]


class Context:
    """插件上下文 —— 插件与框架的唯一接口。

    提供：
    - 消息订阅
    - 工具注册
    - 定时任务
    - 配置读取
    - 回复消息
    - 事件发布
    - 存储访问

    插件不应 import 框架内部模块，所有能力通过 Context 获取。
    """

    def __init__(self, plugin_name: str) -> None:
        self._plugin_name = plugin_name
        self._message_handlers: list[MessageHandler] = []
        self._tools: list[dict[str, Any]] = []
        self._schedules: list[dict[str, Any]] = []
        self._reply_fn: Callable[..., Coroutine[Any, Any, None]] | None = None
        self._event_fn: Callable[..., Coroutine[Any, Any, None]] | None = None
        self._config: dict[str, Any] = {}

    @property
    def plugin_name(self) -> str:
        return self._plugin_name

    # ─── 消息 ────────────────────────────────

    def on_message(self, handler: MessageHandler) -> None:
        """订阅消息。"""
        self._message_handlers.append(handler)

    async def reply(self, text: str, *, target: Any = None, **kwargs: Any) -> None:
        """回复消息。"""
        if self._reply_fn:
            await self._reply_fn(text, target=target, **kwargs)

    # ─── 工具 ────────────────────────────────

    def register_tool(
        self,
        *,
        name: str,
        description: str = "",
        fn: Callable[..., Coroutine[Any, Any, Any]],
        schema: dict[str, Any] | None = None,
    ) -> None:
        """注册工具。"""
        self._tools.append({
            "name": name,
            "description": description,
            "fn": fn,
            "schema": schema,
        })

    # ─── 定时任务 ─────────────────────────────

    def schedule(
        self,
        *,
        every: str | None = None,
        cron: str | None = None,
        fn: ScheduledFn,
        name: str | None = None,
    ) -> None:
        """注册定时任务。

        Args:
            every: 间隔（如 "30s", "5m", "1h"）
            cron: cron 表达式
            fn: 任务函数
            name: 任务名
        """
        self._schedules.append({
            "every": every,
            "cron": cron,
            "fn": fn,
            "name": name or f"{self._plugin_name}_task",
        })

    # ─── 事件 ────────────────────────────────

    async def emit(self, event: str, data: dict[str, Any] | None = None) -> None:
        """发布事件。"""
        if self._event_fn:
            await self._event_fn(event, data or {})

    # ─── 配置 ────────────────────────────────

    def get_config(self, key: str, default: Any = None) -> Any:
        """读取插件配置。"""
        return self._config.get(key, default)

    # ─── 存储 ────────────────────────────────

    async def store_get(self, key: str) -> Any:
        """从插件专属存储读取。"""
        # 桥接到 kernel storage（后续实现）
        return None

    async def store_set(self, key: str, value: Any) -> None:
        """写入插件专属存储。"""
        pass

    # ─── 内部（框架侧注入）─────────────────────

    def _inject_reply_fn(self, fn: Callable[..., Coroutine[Any, Any, None]]) -> None:
        self._reply_fn = fn

    def _inject_event_fn(self, fn: Callable[..., Coroutine[Any, Any, None]]) -> None:
        self._event_fn = fn

    def _inject_config(self, config: dict[str, Any]) -> None:
        self._config = config


class Plugin:
    """插件基类 v2。

    子类只需覆写 on_load()，通过 ctx 注册所有能力。
    """

    name: str = "unnamed_plugin"
    version: str = "0.1.0"
    description: str = ""

    async def on_load(self, ctx: Context) -> None:
        """插件加载入口。在此注册消息处理、工具、定时任务等。"""

    async def on_unload(self, ctx: Context) -> None:
        """插件卸载（可选）。清理资源。"""

    def __repr__(self) -> str:
        return f"<Plugin:{self.name} v{self.version}>"
