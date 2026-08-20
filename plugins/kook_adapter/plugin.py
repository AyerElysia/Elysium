"""KOOK 适配器插件

架构：
- KookAPIClient: HTTP REST API 调用层
- KookGateway: WebSocket 网关（心跳、重连、事件接收）
- KookEventHandler: 事件 → MessageEnvelope 转换
- KookSender: MessageEnvelope → KOOK API 发送

设计原则：
- 纯传输层，不做内容过滤或行为规则
- 频道选择是配置驱动的路由，不是内容审查
- 遵循 BaseAdapter 接口契约
"""
from __future__ import annotations

from typing import Any, cast

from src.app.plugin_system.api.log_api import get_logger
from src.core.components.base import BaseAdapter, BasePlugin
from src.core.components.loader import register_plugin
from src.core.transport.wire import CoreSink, MessageEnvelope

from .client import KookAPIClient
from .config import KookAdapterConfig
from .events import KookEventHandler
from .gateway import KookGateway
from .sender import KookSender

logger = get_logger("kook_adapter")


class KookAdapter(BaseAdapter):
    """KOOK 平台适配器 — 直连 KOOK WebSocket Gateway。"""

    adapter_name = "kook_adapter"
    adapter_version = "1.0.0"
    adapter_author = "Elysium Team"
    adapter_description = "KOOK 平台适配器（WebSocket 直连，频道+私信）"
    platform = "kook"

    run_in_subprocess = False

    def __init__(self, core_sink: CoreSink, plugin: "KookAdapterPlugin | None" = None, **kwargs: Any):
        super().__init__(core_sink, plugin=plugin, **kwargs)

        self._client: KookAPIClient | None = None
        self._gateway: KookGateway | None = None
        self._event_handler: KookEventHandler | None = None
        self._sender: KookSender | None = None
        self._bot_id: str = ""

    def _get_config(self) -> KookAdapterConfig | None:
        if self.plugin and self.plugin.config:
            return cast(KookAdapterConfig, self.plugin.config)
        return None

    # ─── 生命周期 ───────────────────────────────────────────

    async def on_adapter_loaded(self) -> None:
        """适配器加载：初始化客户端并连接 Gateway。"""
        config = self._get_config()
        if not config:
            raise RuntimeError("KOOK 适配器启动失败：缺少插件配置")

        token = config.bot.token.strip()
        if not token:
            raise RuntimeError("KOOK 适配器启动失败：bot.token 未配置")

        logger.info("KOOK 适配器 v1.0 正在启动...")

        # 初始化 API 客户端
        self._client = KookAPIClient(token)
        await self._client.start()

        # 获取 Bot 身份
        me = await self._client.get_me()
        self._bot_id = me.get("id", "")
        bot_name = config.bot.bot_name or me.get("username", "KOOK Bot")
        logger.info(f"KOOK Bot 已认证: {bot_name} (id={self._bot_id})")

        # 初始化事件处理器和发送器
        self._event_handler = KookEventHandler(self._get_config, self._bot_id, self._client)
        self._sender = KookSender(self._client, self._get_config)

        # 初始化 Gateway 并连接
        self._gateway = KookGateway(
            token=token,
            get_gateway_url=lambda: self._client.get_gateway(compress=0),  # type: ignore[union-attr]
            on_event=self._on_gateway_event,
        )
        await self._gateway.start()

        logger.info("KOOK 适配器已加载")

    async def on_adapter_unloaded(self) -> None:
        """适配器卸载：断开连接并清理资源。"""
        logger.info("KOOK 适配器正在关闭...")

        if self._gateway:
            await self._gateway.stop()
            self._gateway = None

        if self._client:
            await self._client.close()
            self._client = None

        logger.info("KOOK 适配器已关闭")

    # ─── BaseAdapter 接口实现 ───────────────────────────────

    async def from_platform_message(self, raw: dict[str, Any]) -> MessageEnvelope | None:
        """入站：KOOK 事件 → MessageEnvelope。

        由 Gateway 事件回调触发，而非 Elysium 通用传输层。
        此方法保留用于接口兼容。
        """
        if self._event_handler:
            return await self._event_handler.handle_event(raw)
        return None

    async def _send_platform_message(self, envelope: MessageEnvelope) -> None:
        """出站：MessageEnvelope → KOOK API。"""
        if self._sender:
            await self._sender.send(envelope)

    async def get_bot_info(self) -> dict[str, Any]:
        """获取 Bot 信息。"""
        config = self._get_config()
        return {
            "bot_id": self._bot_id,
            "bot_name": (config.bot.bot_name if config else "") or "KOOK Bot",
            "platform": self.platform,
        }

    async def health_check(self) -> bool:
        """健康检查：确认 Gateway 的生命周期任务仍在运行。

        本适配器未使用 Elysium wire 内置传输层，基类默认的 is_connected()
        恒为 False，会导致框架每 30 秒误判"不健康"并触发 reconnect，
        进而把适配器停掉。Gateway 自己拥有断线退避重连循环，因此在
        重连窗口内不能因为暂时没有 WebSocket 而再次 stop；只有管理任务
        已经结束时才需要适配器级重启。
        """
        return self._gateway is not None and self._gateway.alive

    # ─── 内部 ───────────────────────────────────────────────

    async def _on_gateway_event(self, event: dict[str, Any]) -> None:
        """Gateway 事件回调：转换并推送到核心。"""
        envelope = await self.from_platform_message(event)
        if envelope:
            await self.core_sink.send(envelope)

    @property
    def client(self) -> KookAPIClient | None:
        """获取 API 客户端实例（供高级用途）。"""
        return self._client


@register_plugin
class KookAdapterPlugin(BasePlugin):
    """KOOK 适配器插件。"""

    plugin_name = "kook_adapter"
    plugin_version = "1.0.0"
    plugin_author = "Elysium Team"
    plugin_description = "KOOK 平台适配器（WebSocket 直连，频道+私信）"
    configs = [KookAdapterConfig]

    def get_components(self) -> list[type]:
        """获取插件内所有组件类。"""
        config = cast(KookAdapterConfig | None, self.config)
        if config is None or not config.plugin.enabled:
            return []
        return [KookAdapter]
