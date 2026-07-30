"""NapCat 适配器（全面重写版 v3.0）

核心架构：
- NapCatClient: API 调用层（100+ OneBot API）
- EventRouter: 事件路由层（message/notice/request/meta_event）
- OutgoingSender: 出站消息发送
- CommandHandler: 命令系统（旧式兼容 + 新式透传）

接口兼容：
- 继承 BaseAdapter（from src.core.components.base）
- 实现 from_platform_message(raw) -> MessageEnvelope | None
- 实现 _send_platform_message(envelope) -> None
- 实现 get_bot_info() -> dict
- send_napcat_api(action, params) 向后兼容
"""

from __future__ import annotations

from typing import Any, cast

from mofox_wire import CoreSink, MessageEnvelope, WebSocketAdapterOptions

from src.app.plugin_system.api.log_api import get_logger
from src.core.components.base import BaseAdapter, BasePlugin
from src.core.components.loader import register_plugin

from .client import NapCatClient
from .config import NapcatAdapterConfig
from .events import EventRouter
from .outgoing import CommandHandler, OutgoingSender

logger = get_logger("napcat_adapter")


def _validate_bot_identity(config: NapcatAdapterConfig) -> None:
    """校验 Bot 身份配置。"""
    qq_id = str(config.bot.qq_id).strip()
    qq_nickname = str(config.bot.qq_nickname).strip()

    invalid_id_values = {"", "0", "none", "null", "undefined", "pydanticundefined"}
    if qq_id.lower() in invalid_id_values or not qq_id.isdigit():
        raise ValueError("配置项 bot.qq_id 无效：必须为非空数字字符串")

    invalid_nickname_values = {"", "none", "null", "undefined", "pydanticundefined"}
    if qq_nickname.lower() in invalid_nickname_values:
        raise ValueError("配置项 bot.qq_nickname 无效：必须为非空昵称")


class NapcatAdapter(BaseAdapter):
    """NapCat 适配器 v3.0 — 全功能 OneBot 11 适配器。"""

    adapter_name = "napcat_adapter"
    adapter_version = "3.0.0"
    adapter_author = "Elysium Team"
    adapter_description = "全功能 NapCat/OneBot 11 适配器（100+ API，全量事件感知）"
    platform = "qq"

    run_in_subprocess = False

    def __init__(self, core_sink: CoreSink, plugin: "NapcatAdapterPlugin | None" = None, **kwargs):
        """初始化 NapCat 适配器。"""
        # 从配置读取 WebSocket 参数
        if plugin and plugin.config:
            config = cast(NapcatAdapterConfig, plugin.config)
            host = config.napcat_server.host
            port = config.napcat_server.port
            access_token = config.napcat_server.access_token
            mode_str = config.napcat_server.mode
            ws_mode = "client" if mode_str == "direct" else "server"

            ws_url = f"ws://{host}:{port}"
            headers = {}
            if access_token:
                headers["Authorization"] = f"Bearer {access_token}"
        else:
            ws_url = "ws://127.0.0.1:8095"
            headers = {}
            ws_mode = "server"

        transport = WebSocketAdapterOptions(
            mode=ws_mode,
            url=ws_url,
            headers=headers if headers else None,
        )

        super().__init__(core_sink, plugin=plugin, transport=transport, **kwargs)

        # 核心组件
        self._client = NapCatClient()
        self._router = EventRouter(self._client, self._get_config)
        self._sender = OutgoingSender(self._client, self._get_config)
        self._command_handler = CommandHandler(self._client, self._get_config, core_sink)

        # 注入重连回调
        self._router.meta_handler.set_reconnect_callback(self.reconnect)

        # WebSocket 健康检查任务
        self._health_check_task: asyncio.Task | None = None
        self._health_check_running = False

    def _get_config(self) -> NapcatAdapterConfig | None:
        """获取当前插件配置。"""
        if self.plugin and self.plugin.config:
            return cast(NapcatAdapterConfig, self.plugin.config)
        return None

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def on_adapter_loaded(self) -> None:
        """适配器加载。"""
        logger.info("NapCat 适配器 v3.0 正在启动...")

        if not self.plugin or not self.plugin.config:
            raise RuntimeError("NapCat 适配器启动失败：缺少插件配置")

        config = cast(NapcatAdapterConfig, self.plugin.config)
        _validate_bot_identity(config)

        # 启动 WebSocket 健康检查
        self._health_check_running = True
        self._health_check_task = asyncio.create_task(self._ws_health_check())

        logger.info("NapCat 适配器已加载")

    async def on_adapter_unloaded(self) -> None:
        """适配器卸载。"""
        logger.info("NapCat 适配器正在关闭...")

        # 停止健康检查
        self._health_check_running = False
        if self._health_check_task:
            self._health_check_task.cancel()
            try:
                await self._health_check_task
            except asyncio.CancelledError:
                pass

        # 停止心跳检查
        self._router.meta_handler.stop()

        # 解绑 WebSocket
        self._client.unbind_ws()

        logger.info("NapCat 适配器已关闭")

    async def _ws_health_check(self) -> None:
        """WebSocket 健康检查，检测到断开时记录并尝试触发重连。"""
        import asyncio

        consecutive_failures = 0
        last_log_time = 0.0

        while self._health_check_running:
            try:
                await asyncio.sleep(5)  # 每 5 秒检查一次

                # 检查 _ws 状态
                if self._client._ws is None:
                    consecutive_failures += 1
                    current_time = asyncio.get_event_loop().time()

                    # 避免日志刷屏，每 30 秒记录一次
                    if current_time - last_log_time > 30:
                        logger.warning(
                            f"检测到 WebSocket 连接丢失（_ws=None），已持续 {consecutive_failures * 5} 秒。"
                            "这通常是 mofox_wire 的 _ws_listen_loop 异常退出导致。"
                            "NapCat 会自动重连，重连后连接将恢复。"
                        )
                        last_log_time = current_time
                else:
                    # 连接正常，重置计数
                    if consecutive_failures > 0:
                        logger.info("WebSocket 连接已恢复")
                        consecutive_failures = 0

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"健康检查异常: {e}")
                await asyncio.sleep(10)

    # ------------------------------------------------------------------
    # WebSocket 连接钩子（由 BaseAdapter 调用）
    # ------------------------------------------------------------------

    async def on_ws_connected(self, ws: Any) -> None:
        """WebSocket 连接建立时调用。"""
        self._client.bind_ws(ws)
        # 检查这是初次连接还是重连
        bot_qq = self.plugin.config.bot.qq_id if self.plugin and self.plugin.config else "未知"
        logger.info(f"NapCat WebSocket 已连接 (Bot {bot_qq})")

    async def on_ws_disconnected(self) -> None:
        """WebSocket 连接断开时调用。"""
        self._client.unbind_ws()
        logger.warning("NapCat WebSocket 已断开")

    # ------------------------------------------------------------------
    # BaseAdapter 接口实现
    # ------------------------------------------------------------------

    async def from_platform_message(self, raw: dict[str, Any]) -> MessageEnvelope | None:  # type: ignore[override]
        """将 OneBot 原始消息转换为 MessageEnvelope。

        这是核心入站方法，由 mofox-wire 的传输层调用。
        所有事件（message/notice/request/meta_event/API响应）都经过这里。
        """
        return await self._router.dispatch(raw)

    async def _send_platform_message(self, envelope: MessageEnvelope) -> None:  # type: ignore[override]
        """将 MessageEnvelope 发送到 NapCat。

        这是核心出站方法，由 mofox-wire 的核心推送调用。
        根据消息段类型分发到 sender 或 command_handler。
        """
        # 检查是否是命令类消息
        segment = envelope.get("message_segment", {})
        if isinstance(segment, list):
            first_seg = segment[0] if segment else {}
        else:
            first_seg = segment

        seg_type = first_seg.get("type") if isinstance(first_seg, dict) else None

        try:
            if seg_type in ("command", "adapter_command", "adapter_response"):
                await self._command_handler.handle(envelope)
            else:
                await self._sender.send(envelope)
        except Exception as e:
            logger.error(f"发送消息失败: {e}")
            raise

    async def get_bot_info(self) -> dict[str, Any]:  # type: ignore[override]
        """获取 Bot 信息。"""
        config = self._get_config()
        if not config:
            return {}
        return {
            "bot_id": config.bot.qq_id,
            "bot_name": config.bot.qq_nickname,
            "platform": self.platform,
        }

    # ------------------------------------------------------------------
    # 向后兼容 API
    # ------------------------------------------------------------------

    async def send_napcat_api(
        self, action: str, params: dict[str, Any], timeout: float = 30.0
    ) -> dict[str, Any]:
        """向后兼容的 API 调用方法。

        旧代码（如 life_engine 的 chat_history_tools）可能直接调用此方法。
        """
        return await self._client.call(action, params, timeout=timeout)

    @property
    def client(self) -> NapCatClient:
        """获取 NapCatClient 实例（供高级用途）。"""
        return self._client


@register_plugin
class NapcatAdapterPlugin(BasePlugin):
    """NapCat 适配器插件。"""

    plugin_name = "napcat_adapter"
    plugin_version = "3.0.0"
    plugin_author = "Elysium Team"
    plugin_description = "全功能 NapCat/OneBot 11 适配器（100+ API，全量事件感知）"
    configs = [NapcatAdapterConfig]

    def get_components(self) -> list[type]:
        """获取插件内所有组件类。"""
        return [NapcatAdapter]
