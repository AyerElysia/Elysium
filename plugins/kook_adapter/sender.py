"""KOOK 出站消息发送

将 MessageEnvelope 转换为 KOOK API 调用。
纯传输层：不修改内容、不添加规则。
"""
from __future__ import annotations

from typing import Any, Callable

from mofox_wire import MessageEnvelope

from src.app.plugin_system.api.log_api import get_logger

from .client import KookAPIClient
from .config import KookAdapterConfig

logger = get_logger("kook_adapter")


class KookSender:
    """KOOK 出站消息发送器。"""

    def __init__(
        self,
        client: KookAPIClient,
        get_config: Callable[[], KookAdapterConfig | None],
    ) -> None:
        self._client = client
        self._get_config = get_config

    def _config(self) -> KookAdapterConfig | None:
        return self._get_config()

    async def send(self, envelope: MessageEnvelope) -> None:
        """发送 MessageEnvelope 到 KOOK。"""
        # 提取目标信息
        kook_channel_type = envelope.get("kook_channel_type", "GROUP")
        target_id = envelope.get("kook_target_id", "") or envelope.get("group_id", "")
        user_id = envelope.get("user_id", "")

        # 提取消息内容
        content = self._extract_content(envelope)
        if not content:
            logger.debug("KOOK 发送跳过：空内容")
            return

        # 消息类型
        config = self._config()
        msg_type = 9 if (config and config.features.use_kmarkdown) else 1

        # 引用回复
        quote_msg_id: str | None = None
        if config and config.features.reply_with_quote:
            quote_msg_id = envelope.get("reply_to_message_id") or None

        try:
            if kook_channel_type == "PERSON":
                # 私信
                await self._client.send_direct_message(
                    target_id=user_id,
                    content=content,
                    msg_type=msg_type,
                    quote=quote_msg_id,
                )
            else:
                # 频道消息
                await self._client.send_channel_message(
                    target_id=target_id,
                    content=content,
                    msg_type=msg_type,
                    quote=quote_msg_id,
                )
            logger.debug(f"KOOK 消息已发送 → {target_id or user_id}: {content[:50]}...")
        except Exception as exc:
            logger.error(f"KOOK 发送失败: {exc}")
            raise

    def _extract_content(self, envelope: MessageEnvelope) -> str:
        """从 envelope 提取纯文本内容。"""
        # 优先从 message_segment 提取
        segments = envelope.get("message_segment", [])
        if isinstance(segments, list):
            texts = []
            for seg in segments:
                if isinstance(seg, dict) and seg.get("type") == "text":
                    texts.append(seg.get("data", {}).get("text", ""))
            if texts:
                return "".join(texts)

        # 回退到 content 字段
        content = envelope.get("content", "")
        if isinstance(content, str):
            return content
        return str(content) if content else ""
