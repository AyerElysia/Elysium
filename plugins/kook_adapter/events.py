"""KOOK 事件处理

将 KOOK 事件（s=0 的 d 字段）转换为 MessageEnvelope。
纯传输层：不做内容过滤、不做行为判断。
"""
from __future__ import annotations

from typing import Any, Callable

from mofox_wire import MessageBuilder, MessageEnvelope, SegPayload
from mofox_wire.types import UserRole

from src.app.plugin_system.api.log_api import get_logger

from .config import KookAdapterConfig

logger = get_logger("kook_adapter")


class KookEventHandler:
    """KOOK 事件 → MessageEnvelope 转换器。"""

    def __init__(self, get_config: Callable[[], KookAdapterConfig | None], bot_id: str) -> None:
        self._get_config = get_config
        self._bot_id = bot_id

    def _config(self) -> KookAdapterConfig | None:
        return self._get_config()

    async def handle_event(self, event: dict[str, Any]) -> MessageEnvelope | None:
        """处理 KOOK 事件，返回 MessageEnvelope 或 None。"""
        msg_type = event.get("type")
        channel_type = event.get("channel_type", "")
        author_id = event.get("author_id", "")

        # 忽略 Bot 自己的消息
        if author_id == self._bot_id:
            return None

        # 系统事件（type=255）暂不转发到核心
        if msg_type == 255:
            self._handle_system_event(event)
            return None

        # 仅处理文字类消息（1=文本, 9=KMarkdown）
        if msg_type not in (1, 9):
            # 图片/视频/文件/音频 — 记录但暂不构建 envelope
            logger.debug(f"KOOK 非文字消息 type={msg_type}，跳过")
            return None

        # 频道过滤（配置驱动，非内容规则）
        if not self._should_process(channel_type, event):
            return None

        return self._build_envelope(event)

    def _should_process(self, channel_type: str, event: dict[str, Any]) -> bool:
        """根据配置判断是否处理该消息（频道黑白名单 / 私信开关）。"""
        config = self._config()
        if not config:
            return True

        if channel_type == "PERSON":
            return config.features.enable_dm

        if channel_type == "GROUP":
            target_id = event.get("target_id", "")
            list_type = config.features.channel_list_type
            channel_list = config.features.channel_list

            if not channel_list:
                return True  # 空名单 = 不过滤

            in_list = target_id in channel_list
            if list_type == "whitelist":
                return in_list
            else:  # blacklist
                return not in_list

        return True

    def _build_envelope(self, event: dict[str, Any]) -> MessageEnvelope:
        """将 KOOK 消息事件构建为 MessageEnvelope。"""
        extra = event.get("extra", {})
        author = extra.get("author", {})
        channel_type = event.get("channel_type", "GROUP")

        author_id = event.get("author_id", "")
        author_name = author.get("username", "") or author.get("nickname", "") or author_id
        content = event.get("content", "")
        msg_id = event.get("msg_id", "")
        target_id = event.get("target_id", "")
        guild_id = extra.get("guild_id", "")
        channel_name = extra.get("channel_name", "")
        mention_list = extra.get("mention", [])

        # 判断是否被 @
        is_mentioned = self._bot_id in mention_list

        # 构建消息段
        segments: list[SegPayload] = []

        # 文本内容
        if content:
            segments.append(SegPayload(type="text", data={"text": content}))

        # 构建 envelope
        is_dm = channel_type == "PERSON"
        builder = MessageBuilder(
            platform="kook",
            message_id=msg_id,
            user_id=author_id,
            user_name=author_name,
            user_role=UserRole.USER,
            content=content,
            is_group=not is_dm,
            group_id=target_id if not is_dm else "",
            group_name=channel_name,
            is_mentioned=is_mentioned,
            raw=event,
        )

        for seg in segments:
            builder.add_segment(seg)

        # 附加 KOOK 特有元数据
        envelope = builder.build()
        envelope["kook_guild_id"] = guild_id
        envelope["kook_channel_type"] = channel_type
        envelope["kook_target_id"] = target_id

        return envelope

    def _handle_system_event(self, event: dict[str, Any]) -> None:
        """记录系统事件（仅日志，不转发）。"""
        extra = event.get("extra", {})
        event_type = extra.get("type", "unknown")
        body = extra.get("body", {})
        logger.debug(f"KOOK 系统事件: type={event_type} body_keys={list(body.keys())}")
