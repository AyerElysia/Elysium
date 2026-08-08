"""KOOK 出站消息发送

将 MessageEnvelope 转换为 KOOK API 调用。
纯传输层：不修改内容、不添加规则。

出站段类型支持（与 napcat 对齐）：
    text / at / reply / image / emoji / voice / voiceurl /
    videourl / file / face / music / share / json / forward / seglist
"""
from __future__ import annotations

from typing import Any, Callable

from mofox_wire import MessageEnvelope

from src.app.plugin_system.api.log_api import get_logger

from .client import KookAPIClient
from .config import KookAdapterConfig

logger = get_logger("kook_adapter")

# QQ face ID → KOOK 内置表情名（部分常用映射，未命中时降级为文字描述）
_QQ_FACE_TO_KOOK: dict[str, str] = {
    "14": "微笑", "1": "(no)", "2": "(yes)", "5": "(cry)",
    "124": "(heart)", "66": "(666)", "13": "(bigcry)",
}


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
        """发送 MessageEnvelope 到 KOOK（支持多段混合：文本+媒体分批发送）。"""
        message_info = envelope.get("message_info") or {}
        if not isinstance(message_info, dict):
            message_info = {}
        user_info = message_info.get("user_info") or {}
        group_info = message_info.get("group_info") or {}
        if not isinstance(user_info, dict):
            user_info = {}
        if not isinstance(group_info, dict):
            group_info = {}

        # 目标判定：优先入站自定义字段，回退到标准 message_info
        kook_channel_type = envelope.get("kook_channel_type")
        if not kook_channel_type:
            kook_channel_type = "GROUP" if group_info else "PERSON"

        target_id = (
            envelope.get("kook_target_id")
            or group_info.get("group_id")
            or envelope.get("group_id", "")
        )
        user_id = user_info.get("user_id") or envelope.get("user_id", "")

        # 引用回复（reply 段或显式 reply_to_message_id）
        config = self._config()
        quote_msg_id: str | None = None
        if config and config.features.reply_with_quote:
            quote_msg_id = (
                envelope.get("reply_to_message_id")
                or self._extract_reply_id(envelope)
                or None
            )

        # 展开消息段（支持 seglist 嵌套）
        segments = self._flatten_segments(envelope.get("message_segment", []))
        if not segments:
            # 回退到纯 content
            content = envelope.get("content", "")
            if content:
                segments = [{"type": "text", "data": str(content)}]

        if not segments:
            logger.debug("KOOK 发送跳过：空内容")
            return

        # 分离文本段与媒体段：文本合并为一条 KMarkdown，媒体各自单独发送
        text_parts: list[str] = []
        media_segs: list[dict[str, Any]] = []
        for seg in segments:
            seg_type = seg.get("type")
            data = seg.get("data")
            if seg_type == "text" and data:
                text_parts.append(str(data))
            elif seg_type == "at" and data:
                uid = str(data).split(":")[-1] if ":" in str(data) else str(data)
                text_parts.append(f"(met){uid}(met)")
            elif seg_type == "reply":
                continue  # 已通过 quote 参数处理
            elif seg_type == "face" and data:
                text_parts.append(self._face_to_kook(str(data)))
            elif seg_type in ("image", "emoji", "voice", "voiceurl", "videourl", "file", "music", "share", "json", "forward"):
                media_segs.append({"type": seg_type, "data": data})
            elif seg_type == "text":
                continue
            else:
                logger.debug(f"KOOK 未处理的出站段类型: {seg_type}")

        sent_any = False

        # 1) 媒体段逐个发送（图片/语音/视频/文件）
        for seg in media_segs:
            try:
                await self._send_media_seg(
                    kook_channel_type, target_id, user_id, seg, quote_msg_id
                )
                sent_any = True
                quote_msg_id = None  # 引用只挂在第一条消息上
            except Exception as exc:
                logger.error(f"KOOK 媒体发送失败 type={seg.get('type')}: {exc}")

        # 2) 文本段合并发送
        text_content = "".join(text_parts).strip()
        if text_content:
            msg_type = 9 if (config and config.features.use_kmarkdown) else 1
            await self._send_text(
                kook_channel_type, target_id, user_id, text_content, msg_type, quote_msg_id
            )
            sent_any = True

        if not sent_any:
            logger.debug("KOOK 发送跳过：无有效内容")

    # ─── 文本发送 ───────────────────────────────────────────

    async def _send_text(
        self,
        channel_type: str,
        target_id: str,
        user_id: str,
        content: str,
        msg_type: int,
        quote: str | None,
    ) -> None:
        try:
            if channel_type == "PERSON":
                await self._client.send_direct_message(
                    target_id=user_id, content=content, msg_type=msg_type, quote=quote
                )
            else:
                await self._client.send_channel_message(
                    target_id=target_id, content=content, msg_type=msg_type, quote=quote
                )
            logger.debug(f"KOOK 文本已发送 → {target_id or user_id}: {content[:50]}...")
        except Exception as exc:
            logger.error(f"KOOK 发送失败: {exc}")
            raise

    # ─── 媒体发送 ───────────────────────────────────────────

    async def _send_media_seg(
        self,
        channel_type: str,
        target_id: str,
        user_id: str,
        seg: dict[str, Any],
        quote: str | None,
    ) -> None:
        """媒体段：先上传到 KOOK CDN，再按类型发送。"""
        seg_type = seg["type"]
        data = seg.get("data")

        if seg_type in ("music", "share"):
            # KOOK 无音乐/分享卡片等价物，降级为链接文本
            url = self._extract_share_url(seg)
            if url:
                await self._send_text(channel_type, target_id, user_id, url, 1, quote)
            return

        if seg_type in ("json", "forward"):
            # QQ 卡片/合并转发无等价物，降级为占位文本
            await self._send_text(
                channel_type, target_id, user_id, "[该消息类型暂不支持在 KOOK 展示]", 1, quote
            )
            return

        data_str = self._media_data_to_str(data)
        if not data_str:
            logger.debug(f"KOOK 媒体段数据为空，跳过: {seg_type}")
            return

        if seg_type in ("image", "emoji"):
            url = await self._client.resolve_and_upload(data_str, "image.png")
            await self._send_raw(channel_type, target_id, user_id, url, 2, quote)
        elif seg_type in ("voice", "voiceurl"):
            url = await self._client.resolve_and_upload(data_str, "voice.mp3")
            try:
                await self._send_raw(channel_type, target_id, user_id, url, 8, quote)
            except Exception as exc:
                # type=8 若 Bot 权限不支持，降级为文件消息
                logger.warning(f"KOOK 语音消息发送失败({exc})，降级为文件")
                await self._send_raw(channel_type, target_id, user_id, url, 4, quote)
        elif seg_type == "videourl":
            url = await self._client.resolve_and_upload(data_str, "video.mp4")
            await self._send_raw(channel_type, target_id, user_id, url, 3, quote)
        elif seg_type == "file":
            filename = self._extract_filename(data)
            url = await self._client.resolve_and_upload(data_str, filename)
            await self._send_raw(channel_type, target_id, user_id, url, 4, quote)

    async def _send_raw(
        self,
        channel_type: str,
        target_id: str,
        user_id: str,
        content: str,
        msg_type: int,
        quote: str | None,
    ) -> None:
        if channel_type == "PERSON":
            await self._client.send_direct_message(
                target_id=user_id, content=content, msg_type=msg_type, quote=quote
            )
        else:
            await self._client.send_channel_message(
                target_id=target_id, content=content, msg_type=msg_type, quote=quote
            )
        logger.debug(f"KOOK 媒体已发送 type={msg_type} → {target_id or user_id}")

    # ─── 段展开与提取辅助 ───────────────────────────────────

    def _flatten_segments(self, segments: Any) -> list[dict[str, Any]]:
        """展开段列表（支持 dict / list / seglist 嵌套）。"""
        if isinstance(segments, dict):
            segments = [segments]
        if not isinstance(segments, list):
            return []
        flat: list[dict[str, Any]] = []
        for seg in segments:
            if not isinstance(seg, dict):
                continue
            if seg.get("type") == "seglist":
                flat.extend(self._flatten_segments(seg.get("data", [])))
            else:
                flat.append(seg)
        return flat

    def _extract_reply_id(self, envelope: MessageEnvelope) -> str | None:
        """从 reply 段提取引用消息 ID。"""
        for seg in self._flatten_segments(envelope.get("message_segment", [])):
            if seg.get("type") == "reply":
                data = seg.get("data")
                if isinstance(data, dict):
                    return str(data.get("id", "")) or None
                return str(data) if data else None
        return None

    @staticmethod
    def _media_data_to_str(data: Any) -> str:
        """媒体段 data 归一为字符串（base64 / URL / 路径）。"""
        if isinstance(data, str):
            return data
        if isinstance(data, dict):
            for key in ("base64", "data", "url", "file", "path", "audio_base64", "voice_base64"):
                value = data.get(key)
                if isinstance(value, str) and value.strip():
                    return value
        return ""

    @staticmethod
    def _extract_filename(data: Any) -> str:
        """从 file 段提取文件名。"""
        if isinstance(data, dict):
            return str(data.get("name") or data.get("filename") or "file")
        return "file"

    @staticmethod
    def _extract_share_url(seg: dict[str, Any]) -> str:
        """从 share/music 段提取链接。"""
        data = seg.get("data")
        if isinstance(data, dict):
            return str(data.get("url") or data.get("audio") or "")
        return str(data) if isinstance(data, str) else ""

    @staticmethod
    def _face_to_kook(face_id: str) -> str:
        """QQ face ID → KOOK 内置表情语法，未命中时降级为文字描述。"""
        name = _QQ_FACE_TO_KOOK.get(face_id)
        if name:
            return f"({name})"
        return f"[表情{face_id}]"
