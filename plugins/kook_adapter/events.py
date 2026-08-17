"""KOOK 事件处理

将 KOOK 事件（s=0 的 d 字段）转换为 MessageEnvelope。
纯传输层：不做内容过滤、不做行为判断。

消息类型（type）:
    1=文本, 2=图片, 3=视频, 4=文件, 8=语音, 9=KMarkdown, 10=卡片, 255=系统事件
"""
from __future__ import annotations

import json
import re
from typing import Any, Callable

from src.app.plugin_system.api.log_api import get_logger
from src.core.transport.received_files import persist_received_file
from src.core.transport.wire import (
    MessageBuilder,
    MessageEnvelope,
    SegPayload,
    UserRole,
)

from .client import KookAPIClient
from .config import KookAdapterConfig

logger = get_logger("kook_adapter")

# KMarkdown 内嵌媒体语法: (img)/(video)/(audio)/(file)[url]
_KMD_MEDIA_RE = re.compile(r"\((img|video|audio|file)\)\[([^\]]+)\]")
# KMarkdown 提及语法: (met)用户ID/here/all(met)
_KMD_MET_RE = re.compile(r"\(met\)([^()]+)\(met\)")
# KMarkdown 服务器表情: (emj)表情名(emj)[表情ID]
_KMD_EMJ_RE = re.compile(r"\(emj\)([^()]+)\(emj\)\[[^\]]+\]")
# unicode emoji shortcode: :name:
_KMD_SHORTCODE_RE = re.compile(r":([a-zA-Z0-9_+\-]+):")
# 频道/角色提及
_KMD_CHN_RE = re.compile(r"\(chn\)([^()]+)\(chn\)")
_KMD_ROL_RE = re.compile(r"\(rol\)([^()]+)\(rol\)")


class KookEventHandler:
    """KOOK 事件 → MessageEnvelope 转换器。"""

    def __init__(
        self,
        get_config: Callable[[], KookAdapterConfig | None],
        bot_id: str,
        client: KookAPIClient | None = None,
    ) -> None:
        self._get_config = get_config
        self._bot_id = bot_id
        self._client = client

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

        # 频道过滤（配置驱动，非内容规则）
        if not self._should_process(channel_type, event):
            return None

        return await self._build_envelope(event)

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

    async def _build_envelope(self, event: dict[str, Any]) -> MessageEnvelope:
        """将 KOOK 消息事件构建为 MessageEnvelope（与 napcat 段格式对齐）。"""
        extra = event.get("extra", {})
        author = extra.get("author", {})
        channel_type = event.get("channel_type", "GROUP")

        author_id = event.get("author_id", "")
        author_name = author.get("username", "") or author.get("nickname", "") or author_id
        content = event.get("content", "")
        msg_id = event.get("msg_id", "")
        msg_type = event.get("type", 1)
        target_id = event.get("target_id", "")
        guild_id = extra.get("guild_id", "")
        channel_name = extra.get("channel_name", "")
        mention_list = extra.get("mention", [])

        # 构建消息段（媒体类消息需下载媒体数据）
        segments: list[SegPayload] = await self._build_segments(msg_type, content, extra)

        # 是否被 @（metadata mention 列表或 KMarkdown (met) 语法）
        kmd_met_ids: list[str] = []
        if msg_type == 9:
            kmd_met_ids = _KMD_MET_RE.findall(content)
        is_mentioned = self._bot_id in mention_list or self._bot_id in kmd_met_ids

        if not segments:
            logger.debug(f"KOOK 消息无可解析内容 type={msg_type}")
            segments = [SegPayload(type="text", data="")]

        # 角色映射（KOOK 服务器内置角色: 0=创建者, 1=管理员）
        author_roles = author.get("roles") or []
        if author.get("is_bot"):
            user_role = UserRole.BOT
        elif 0 in author_roles:
            user_role = UserRole.OWNER
        elif 1 in author_roles:
            user_role = UserRole.OPERATOR
        elif channel_type == "PERSON":
            user_role = UserRole.OTHER
        else:
            user_role = UserRole.MEMBER

        # 构建 envelope（MessageBuilder 为链式 API，无构造参数）
        is_dm = channel_type == "PERSON"
        builder = (
            MessageBuilder()
            .direction("incoming")
            .message_id(msg_id)
            .platform("kook")
            .from_user(
                user_id=author_id,
                platform="kook",
                nickname=author_name,
                role=user_role,
            )
        )
        if not is_dm:
            builder.from_group(
                group_id=target_id,
                platform="kook",
                name=channel_name,
            )

        builder.seg_list(segments)
        builder.metadata(
            {
                "is_mentioned": is_mentioned,
                "kook_guild_id": guild_id,
                "kook_channel_type": channel_type,
                "kook_target_id": target_id,
            }
        )

        envelope = builder.build()
        # 顶层快捷字段（兼容旧消费方）+ 原始事件
        envelope["raw_message"] = event
        envelope["kook_guild_id"] = guild_id
        envelope["kook_channel_type"] = channel_type
        envelope["kook_target_id"] = target_id
        envelope["kook_is_mentioned"] = is_mentioned

        return envelope

    # ─── 消息段构建 ─────────────────────────────────────────

    async def _build_segments(
        self, msg_type: int, content: str, extra: dict[str, Any]
    ) -> list[SegPayload]:
        """根据消息类型构建消息段列表。"""
        if msg_type == 2:  # 图片
            seg = await self._download_media_seg(content, "image", "image.png")
            return [seg] if seg else [SegPayload(type="text", data="[图片]")]

        if msg_type == 3:  # 视频（体积大，占位处理）
            return [SegPayload(type="text", data="[视频消息]")]

        if msg_type == 4:  # 文件
            return await self._build_file_segments(content, extra)

        if msg_type == 8:  # 语音（听语音：下载音频进多模态链路）
            seg = await self._download_media_seg(content, "voice", "voice.mp3")
            return [seg] if seg else [SegPayload(type="text", data="[语音]")]

        if msg_type == 9:  # KMarkdown
            return await self._parse_kmarkdown(content)

        if msg_type == 10:  # 卡片消息
            return [SegPayload(type="text", data=self._card_to_text(content))]

        # type=1 纯文本及未知类型
        if content:
            return [SegPayload(type="text", data=content)]
        return []

    async def _download_media_seg(
        self, url: str, seg_type: str, fallback_name: str
    ) -> SegPayload | None:
        """下载媒体并构建 base64 数据段（框架禁止消费远程 URL，必须内联字节）。"""
        if not url or not self._client:
            return None
        try:
            media_bytes = await self._client.download_media_bytes(url)
        except Exception as exc:
            logger.error(f"KOOK 媒体下载失败: {exc}")
            return None
        import base64

        b64 = base64.b64encode(media_bytes).decode("ascii")
        return SegPayload(type=seg_type, data=b64)

    async def _build_file_segments(
        self, content: str, extra: dict[str, Any]
    ) -> list[SegPayload]:
        """文件消息：下载到本地并构建 file 段。"""
        attachments = extra.get("attachments") or {}
        file_name = attachments.get("name") or "file"
        file_size = attachments.get("size")

        if content and self._client:
            try:
                media_bytes = await self._client.download_media_bytes(content)
                reference = await persist_received_file(
                    media_bytes,
                    filename=file_name,
                    platform="kook",
                )
                return [
                    SegPayload(
                        type="file",
                        data={
                            "name": reference.filename,
                            "size": reference.size_bytes,
                            "path": str(reference.path),
                            "sha256": reference.sha256,
                            "storage_key": reference.storage_key,
                            "materialized": True,
                        },
                    )
                ]
            except Exception as exc:
                logger.error(
                    "KOOK 文件下载失败，保留元数据: "
                    f"error_type={type(exc).__name__}"
                )
        return [
            SegPayload(
                type="file",
                data={
                    "name": file_name,
                    "size": file_size,
                    "materialized": False,
                },
            )
        ]

    async def _parse_kmarkdown(self, content: str) -> list[SegPayload]:
        """解析 KMarkdown：文本/@/内嵌媒体/表情。"""
        segments: list[SegPayload] = []
        # 提取内嵌媒体
        media_matches = list(_KMD_MEDIA_RE.finditer(content))
        text = _KMD_MEDIA_RE.sub("\u0001", content)

        # 提及 → at 段
        def _met_repl(m: re.Match) -> str:
            return f"\u0002{m.group(1)}\u0003"

        text = _KMD_MET_RE.sub(_met_repl, text)

        # 服务器表情 / unicode shortcode / 频道 / 角色 → 文本占位
        text = _KMD_EMJ_RE.sub(lambda m: f"[表情:{m.group(1)}]", text)
        text = _KMD_SHORTCODE_RE.sub(lambda m: f"[表情:{m.group(1)}]", text)
        text = _KMD_CHN_RE.sub(lambda m: f"[频道:{m.group(1)}]", text)
        text = _KMD_ROL_RE.sub(lambda m: f"[角色:{m.group(1)}]", text)

        # 按占位符切分还原顺序
        media_iter = iter(media_matches)
        tokens = re.split("(\u0001|\u0002[^\u0003]*\u0003)", text)
        for token in tokens:
            if not token:
                continue
            if token == "\u0001":
                m = next(media_iter, None)
                if m:
                    seg = await self._kmd_media_to_seg(m.group(1), m.group(2))
                    if seg:
                        segments.append(seg)
            elif token.startswith("\u0002"):
                uid = token[1:-1]
                if uid in ("here", "all"):
                    segments.append(SegPayload(type="text", data=f"@{uid}"))
                else:
                    segments.append(SegPayload(type="at", data=uid))
            else:
                stripped = token.strip()
                if stripped:
                    segments.append(SegPayload(type="text", data=token))
        return segments

    async def _kmd_media_to_seg(self, kind: str, url: str) -> SegPayload | None:
        """KMarkdown 内嵌媒体 → 消息段。"""
        if kind == "img":
            seg = await self._download_media_seg(url, "image", "image.png")
            return seg or SegPayload(type="text", data="[图片]")
        if kind == "audio":
            seg = await self._download_media_seg(url, "voice", "voice.mp3")
            return seg or SegPayload(type="text", data="[语音]")
        if kind == "video":
            return SegPayload(type="text", data="[视频消息]")
        if kind == "file":
            return SegPayload(type="file", data={"url": url})
        return None

    @staticmethod
    def _card_to_text(content: str) -> str:
        """卡片消息提取纯文本内容。"""
        try:
            cards = json.loads(content)
        except (json.JSONDecodeError, TypeError):
            return "[卡片消息]"

        parts: list[str] = []

        def _walk(node: Any) -> None:
            if isinstance(node, dict):
                value = node.get("content")
                if isinstance(value, str) and value.strip():
                    parts.append(value.strip())
                for v in node.values():
                    _walk(v)
            elif isinstance(node, list):
                for item in node:
                    _walk(item)

        _walk(cards)
        return "\n".join(parts) if parts else "[卡片消息]"

    def _handle_system_event(self, event: dict[str, Any]) -> None:
        """记录系统事件（仅日志，不转发）。"""
        extra = event.get("extra", {})
        event_type = extra.get("type", "unknown")
        body = extra.get("body", {})
        logger.debug(f"KOOK 系统事件: type={event_type} body_keys={list(body.keys())}")
