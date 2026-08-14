"""事件分发器

根据 post_type 将 OneBot 事件路由到对应 handler：
- message / message_sent → MessageEventHandler
- notice → NoticeEventHandler
- request → RequestEventHandler
- meta_event → MetaEventHandler
- API 响应（echo）→ NapCatClient 响应池
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from src.app.plugin_system.api.log_api import get_logger
from src.core.transport.wire import MessageEnvelope

if TYPE_CHECKING:
    from ..client import NapCatClient
    from ..config import NapcatAdapterConfig
    from .meta import MetaEventHandler

logger = get_logger("napcat_adapter")


class EventRouter:
    """OneBot 事件分发器。

    职责：
    - 将 API 响应（echo）路由到 NapCatClient 的响应池
    - 根据 post_type 分发到对应事件处理器
    - 执行黑白名单过滤
    """

    def __init__(self, client: "NapCatClient", get_config: Any) -> None:
        """初始化事件路由器。

        Args:
            client: NapCatClient 实例（用于 API 响应分发和信息查询）
            get_config: 获取配置的回调函数，返回 NapcatAdapterConfig | None
        """
        self._client = client
        self._get_config = get_config

        # 延迟导入各 handler，避免循环依赖
        from .message import MessageEventHandler
        from .meta import MetaEventHandler
        from .notice import NoticeEventHandler
        from .request import RequestEventHandler

        self._message_handler = MessageEventHandler(client, get_config)
        self._notice_handler = NoticeEventHandler(client, get_config)
        self._request_handler = RequestEventHandler(client, get_config)
        self._meta_handler = MetaEventHandler(client, get_config)

    @property
    def meta_handler(self) -> "MetaEventHandler":
        """获取元事件处理器（供 adapter 调用 reconnect 等）。"""
        return self._meta_handler

    async def dispatch(self, raw: dict[str, Any]) -> MessageEnvelope | None:
        """分发一条原始 WebSocket 消息。

        Args:
            raw: OneBot 原始 JSON dict

        Returns:
            MessageEnvelope 或 None（无需推送到核心时）
        """
        post_type = raw.get("post_type")

        # 1. API 响应（没有 post_type，有 echo）→ 路由到 Client 响应池
        if post_type is None:
            if self._client.dispatch_response(raw):
                return None
            # 既不是事件也不是已知响应，忽略
            logger.debug(f"收到未知消息（无 post_type 且无匹配 echo）: {list(raw.keys())}")
            return None

        # 2. 黑白名单过滤（meta_event 不过滤）
        if post_type != "meta_event":
            if not self._should_process_event(raw):
                return None

        # 3. 根据 post_type 分发
        try:
            match post_type:
                case "message":
                    return await self._message_handler.handle(raw)
                case "message_sent":
                    # Bot 自己发送的消息回声（可选感知）
                    return await self._message_handler.handle_sent(raw)
                case "notice":
                    return await self._notice_handler.handle(raw)
                case "request":
                    return await self._request_handler.handle(raw)
                case "meta_event":
                    return await self._meta_handler.handle(raw)
                case _:
                    logger.debug(f"未知 post_type: {post_type}")
                    return None
        except Exception as e:
            logger.error(f"事件处理异常: post_type={post_type}, error={e}", exc_info=True)
            return None

    # ------------------------------------------------------------------
    # 黑白名单过滤
    # ------------------------------------------------------------------

    def _should_process_event(self, raw: dict[str, Any]) -> bool:
        """检查事件是否应该被处理（黑白名单过滤）。"""
        config = self._get_config()
        if not config:
            return True

        config = cast("NapcatAdapterConfig", config)
        features = config.features
        post_type = raw.get("post_type")

        # 提取 user_id
        user_id: str = ""
        if post_type == "message" or post_type == "message_sent":
            sender = raw.get("sender", {})
            user_id = str(sender.get("user_id", ""))
        elif post_type in ("notice", "request"):
            user_id = str(raw.get("user_id", ""))
        else:
            return True

        # 全局封禁用户
        ban_user_ids = [str(item) for item in features.ban_user_id]
        if user_id and user_id in ban_user_ids:
            logger.debug(f"用户 {user_id} 在封禁列表中，事件被过滤")
            return False

        # 确定消息类型
        message_type = raw.get("message_type")
        group_id = raw.get("group_id")

        if post_type in ("notice", "request"):
            message_type = "group" if group_id else "private"

        # 群聊过滤
        if message_type == "group" and group_id:
            group_id_str = str(group_id)
            group_list_type = features.group_list_type
            group_list = [str(item) for item in features.group_list]

            if group_list_type == "blacklist":
                if group_id_str in group_list:
                    logger.debug(f"群 {group_id_str} 在黑名单中，事件被过滤")
                    return False
            else:  # whitelist
                if group_list and group_id_str not in group_list:
                    logger.debug(f"群 {group_id_str} 不在白名单中，事件被过滤")
                    return False

        # 私聊过滤
        elif message_type == "private":
            private_list_type = features.private_list_type
            private_list = [str(item) for item in features.private_list]

            if private_list_type == "blacklist":
                if user_id in private_list:
                    logger.debug(f"用户 {user_id} 在私聊黑名单中，事件被过滤")
                    return False
            else:  # whitelist
                if private_list and user_id not in private_list:
                    logger.debug(f"用户 {user_id} 不在私聊白名单中，事件被过滤")
                    return False

        return True
