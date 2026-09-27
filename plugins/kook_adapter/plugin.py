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

import asyncio
from typing import Any, cast

from src.app.plugin_system.api.log_api import get_logger
from src.core.components.base import BaseAdapter, BasePlugin
from src.core.components.loader import register_plugin
from src.core.transport.wire import CoreSink, MessageEnvelope
from src.kernel.concurrency import get_task_manager

from .client import KookAPIClient
from .config import KookAdapterConfig
from .events import (
    KookEventHandler,
    rest_direct_message_to_person_event,
    select_unread_direct_messages,
)
from .gateway import KookGateway
from .sender import KookSender

logger = get_logger("kook_adapter")

_UNREAD_POLL_INTERVAL_SECONDS = 20.0
_UNREAD_POLL_PER_CHAT_LIMIT = 12
_UNREAD_POLL_TOTAL_LIMIT = 40
_SEEN_DIRECT_MESSAGE_LIMIT = 512


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
        self._unread_poll_task_info: Any | None = None
        self._seen_direct_message_ids: set[str] = set()
        self._unread_poll_lock = asyncio.Lock()

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
        self._unread_poll_task_info = get_task_manager().create_task(
            self._unread_direct_message_poll_loop(),
            name="kook-unread-poll",
            daemon=True,
        )

        logger.info("KOOK 适配器已加载")

    async def on_adapter_unloaded(self) -> None:
        """适配器卸载：断开连接并清理资源。"""
        logger.info("KOOK 适配器正在关闭...")

        poll = self._unread_poll_task_info
        if poll is not None:
            get_task_manager().cancel_task(poll.task_id)
            self._unread_poll_task_info = None

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
        msg_id = str(event.get("msg_id") or event.get("id") or "").strip()
        if msg_id:
            if msg_id in self._seen_direct_message_ids:
                return
            self._remember_direct_message_id(msg_id)
        await self._deliver_person_event(event)

    async def _deliver_person_event(self, event: dict[str, Any]) -> None:
        envelope = await self.from_platform_message(event)
        if envelope:
            await self.core_sink.send(envelope)

    def _remember_direct_message_id(self, msg_id: str) -> None:
        self._seen_direct_message_ids.add(msg_id)
        overflow = len(self._seen_direct_message_ids) - _SEEN_DIRECT_MESSAGE_LIMIT
        if overflow > 0:
            extra = list(self._seen_direct_message_ids)[:overflow]
            self._seen_direct_message_ids.difference_update(extra)

    async def _unread_direct_message_poll_loop(self) -> None:
        """Pull unread DMs via REST when Gateway HELLO/PING works but events do not."""

        await asyncio.sleep(2.0)
        while self._gateway is not None and self._gateway.alive:
            try:
                async with self._unread_poll_lock:
                    await self._catch_up_unread_direct_messages()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - poll must not kill the adapter
                logger.warning(f"KOOK 私信补拉失败: {type(exc).__name__}")
            await asyncio.sleep(_UNREAD_POLL_INTERVAL_SECONDS)

    async def _catch_up_unread_direct_messages(self) -> None:
        if self._client is None:
            return
        config = self._get_config()
        if config is not None and not config.features.enable_dm:
            return
        chats = await self._client.list_user_chats()
        chats.sort(key=lambda item: int(item.get("unread_count") or 0))
        ingested = 0
        for chat in chats:
            remaining = _UNREAD_POLL_TOTAL_LIMIT - ingested
            if remaining <= 0:
                break
            unread = int(chat.get("unread_count") or 0)
            if unread <= 0:
                continue
            target = chat.get("target_info") if isinstance(chat.get("target_info"), dict) else {}
            if target.get("is_sys"):
                continue
            chat_code = str(chat.get("code") or "").strip()
            target_id = str(target.get("id") or "").strip()
            if not chat_code or not target_id:
                continue
            # Large unread backlogs stay capped so one session cannot flood chatter.
            chat_limit = 3 if unread > 30 else min(unread, _UNREAD_POLL_PER_CHAT_LIMIT)
            chat_limit = min(chat_limit, remaining)
            page_size = 20
            messages = await self._client.list_direct_messages(
                chat_code,
                page_size=page_size,
            )
            pending = select_unread_direct_messages(
                messages,
                bot_id=self._bot_id,
                seen_ids=self._seen_direct_message_ids,
                unread_count=unread,
                limit=chat_limit,
            )
            for message in pending:
                if ingested >= _UNREAD_POLL_TOTAL_LIMIT:
                    break
                msg_id = str(message.get("id") or "").strip()
                if not msg_id or msg_id in self._seen_direct_message_ids:
                    continue
                self._remember_direct_message_id(msg_id)
                event = rest_direct_message_to_person_event(
                    message,
                    target_id=target_id,
                )
                await self._deliver_person_event(event)
                ingested += 1
        if ingested:
            logger.info(
                f"KOOK 已补拉未读私信: count={ingested} "
                f"seen={len(self._seen_direct_message_ids)}"
            )

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
