"""消息转换器。

负责 ``MessageEnvelope``（wire 层传输格式）与 ``Message``（核心业务模型）之间的
双向转换。入站媒体只在这里解析、验证并附加为 ``MediaAttachment``；转换器不会
下载远程资源，也不会执行 VLM、ASR、ffmpeg 或 ``MediaManager`` 识别。

设计原则：
- 保留适配器传入的 legacy 媒体段与文本占位符，供旧调用方继续使用。
- 媒体逐项验证；单项失败不会丢弃整条消息或原始 legacy 媒体项。
- ``seglist`` / ``reply`` 嵌套最多递归 5 层，超出以占位符替代。
- 单个段解析失败不影响整体，用占位符保留位置。
"""

from __future__ import annotations

import base64
import binascii
import copy
import hashlib
import time
from typing import Any

from mofox_wire import MessageEnvelope, MessageInfoPayload, SegPayload

from src.core.models.media import MediaAttachment, MediaSegmentType
from src.core.models.message import Message, MessageType
from src.core.transport.message_receive.utils import (
    extract_stream_id,
    infer_chat_type,
    normalize_base64,
    safe_json_loads,
)
from src.kernel.logger import get_logger

logger = get_logger("message_converter")

# 递归深度硬上限
_MAX_NESTING_DEPTH: int = 5
_MEDIA_SEGMENT_TYPES = {segment_type.value for segment_type in MediaSegmentType}

_MESSAGE_INIT_KEYS = {
    "message_id",
    "time",
    "reply_to",
    "content",
    "processed_plain_text",
    "message_type",
    "sender_id",
    "sender_name",
    "sender_cardname",
    "sender_role",
    "platform",
    "chat_type",
    "stream_id",
    "raw_data",
    "attachments",
    "extra",
    "media",
    "media_errors",
    "at_users",
    "unknown_segments",
    "group_id",
    "group_name",
}


def _is_internal_target_id(value: Any) -> bool:
    raw = str(value or "").strip()
    if not raw:
        return False
    return (
        raw in {"life_engine_nucleus", "system"}
        or raw.startswith("life_engine_")
        or raw.startswith(("p-", "g-"))
    )


def _safe_copy_media_data(value: Any, memo: dict[int, Any] | None = None) -> Any:
    """复制适配器媒体容器；单个不可复制字段不会让整个段丢失。"""
    try:
        return copy.deepcopy(value)
    except Exception:
        pass

    if memo is None:
        memo = {}
    object_id = id(value)
    if object_id in memo:
        return memo[object_id]

    if isinstance(value, dict):
        copied: dict[Any, Any] = {}
        memo[object_id] = copied
        for key, item in value.items():
            try:
                copied_key = copy.deepcopy(key)
            except Exception:
                copied_key = key
            copied[copied_key] = _safe_copy_media_data(item, memo)
        return copied
    if isinstance(value, list):
        copied_list: list[Any] = []
        memo[object_id] = copied_list
        copied_list.extend(_safe_copy_media_data(item, memo) for item in value)
        return copied_list
    if isinstance(value, tuple):
        copied_tuple = tuple(_safe_copy_media_data(item, memo) for item in value)
        memo[object_id] = copied_tuple
        return copied_tuple
    if isinstance(value, set):
        copied_set = {_safe_copy_media_data(item, memo) for item in value}
        memo[object_id] = copied_set
        return copied_set

    try:
        return copy.copy(value)
    except Exception:
        return value


# ──────────────────────────────────────────────
#  段解析返回结构
# ──────────────────────────────────────────────

class _ParseResult:
    """段解析的聚合结果。

    Attributes:
        text_parts: 纯文本片段列表，最终用空字符串拼接
        media: legacy 媒体资源列表，每项为 ``{"type": str, "data": Any}``
        attachments: 已成功验证的 canonical 媒体附件
        media_errors: 不包含媒体 body 的逐项安全错误记录
        reply_to: 被回复消息的 ID（仅第一个 reply 段生效）
        at_users: 被 @ 用户列表 ``[{"nickname": str, "user_id": str}]``
        unknown_segments: 无法识别的段类型记录
    """

    __slots__ = (
        "text_parts",
        "media",
        "attachments",
        "media_errors",
        "reply_to",
        "at_users",
        "unknown_segments",
    )

    def __init__(self) -> None:
        self.text_parts: list[str] = []
        self.media: list[dict[str, Any]] = []
        self.attachments: list[MediaAttachment] = []
        self.media_errors: list[dict[str, Any]] = []
        self.reply_to: str | None = None
        self.at_users: list[dict[str, str]] = []
        self.unknown_segments: list[dict[str, Any]] = []

    # ---- 便捷方法 ----

    @property
    def plain_text(self) -> str:
        """拼接所有文本片段。"""
        return "".join(self.text_parts)

    def merge(self, other: "_ParseResult") -> None:
        """将另一个解析结果合并到自身。"""
        self.text_parts.extend(other.text_parts)
        self.media.extend(other.media)
        self.attachments.extend(other.attachments)
        self.media_errors.extend(other.media_errors)
        if other.reply_to and not self.reply_to:
            self.reply_to = other.reply_to
        self.at_users.extend(other.at_users)
        self.unknown_segments.extend(other.unknown_segments)


# ──────────────────────────────────────────────
#  MessageConverter
# ──────────────────────────────────────────────


class MessageConverter:
    """MessageEnvelope ↔ Message 双向转换器。

    实例无状态，可以作为单例在整个应用中复用。媒体处理仅限 legacy 段的
    规范化、验证和附件构造；媒体理解由转换器之外的上层流程负责。

    Examples:
        >>> converter = MessageConverter()
        >>> message = await converter.envelope_to_message(envelope)
        >>> envelope = await converter.message_to_envelope(message)
    """

    # ─── envelope → message ───────────────────

    async def envelope_to_message(self, envelope: MessageEnvelope) -> Message:
        """将 MessageEnvelope 转换为 Message。

        Args:
            envelope: mofox-wire 消息信封

        Returns:
            Message: 核心业务消息对象

        Raises:
            ValueError: envelope 缺少必要字段（message_info / message_segment）
        """
        # 从信封中提取 message_info，这是所有消息的元数据核心
        message_info: MessageInfoPayload = envelope.get("message_info")  # type: ignore[assignment]
        # 如果没有提供，说明数据格式不规范，抛出异常提醒上层调用
        if message_info is None:
            raise ValueError("MessageEnvelope 缺少 message_info 字段")

        raw_segments = envelope.get("message_segment")  # type: ignore[arg-type]
        if raw_segments is None:
            # 尝试 message_chain 别名
            raw_segments = envelope.get("message_chain")  # type: ignore[arg-type]

        if raw_segments is None:
            raise ValueError("MessageEnvelope 缺少 message_segment/message_chain 字段")

        # 规范化输入，适配单个段或段列表两种情况
        # mofox-wire 有时会直接用 dict 表示一个段，这里统一转为 list
        segments: list[SegPayload]
        if isinstance(raw_segments, dict):
            segments = [raw_segments]  # type: ignore[list-item]
        else:
            segments = list(raw_segments)

        # 先递归保留所有 legacy 媒体，再按消息 ID 逐项构造 canonical 附件。
        result = self._parse_segments(segments, depth=0)
        message_id = message_info.get("message_id", "")
        self._attach_validated_media(result, message_id=message_id)

        # 根据最终解析结果决定消息类型，比如 TEXT/IMAGE 等。
        message_type = self._infer_message_type(result)
        content = self._build_content(result, message_type)

        # 提取发送者及群组信息，user_info 可能为空。
        user_info = message_info.get("user_info") or {}
        group_info = message_info.get("group_info")
        group_id = group_info.get("group_id") if group_info else None
        group_name = group_info.get("group_name") if group_info else None

        raw_role = user_info.get("role")
        sender_role: str | None = None
        if raw_role is not None:
            sender_role = raw_role.value

        # 显式合并 extra，避免 canonical 字段与 legacy 兼容字段成为重复关键字。
        extra_data = self._sanitize_extra_data(message_info.get("extra") or {})
        extra_data.update(
            {
                "media": result.media,
                "at_users": result.at_users,
                "unknown_segments": result.unknown_segments,
                "group_id": group_id,
                "group_name": group_name,
            }
        )
        if result.media_errors:
            extra_data["media_errors"] = result.media_errors

        return Message(
            message_id=message_id,
            time=message_info.get("time", time.time()),
            reply_to=result.reply_to,
            content=content,
            processed_plain_text=result.plain_text or None,
            message_type=message_type,
            sender_id=user_info.get("user_id", ""),
            sender_name=user_info.get("user_nickname", ""),
            sender_cardname=user_info.get("user_cardname"),
            sender_role=sender_role,
            platform=message_info.get("platform", ""),
            chat_type=infer_chat_type(message_info),
            stream_id=extract_stream_id(message_info),
            raw_data=envelope.get("raw_message"),
            attachments=result.attachments,
            extra=extra_data,
        )

    @staticmethod
    def _attach_validated_media(result: _ParseResult, *, message_id: Any) -> None:
        source_message_id = (
            message_id
            if isinstance(message_id, str) and message_id.strip()
            else None
        )
        for index, item in enumerate(result.media):
            media_type = str(item.get("type", "unknown"))
            try:
                attachment = MediaAttachment.from_legacy(
                    item,
                    source_message_id=source_message_id,
                )
            except Exception as exc:
                error_name = type(exc).__name__
                result.media_errors.append(
                    {"index": index, "type": media_type, "error": error_name}
                )
                logger.warning(
                    f"媒体附件验证失败 (index={index}, type={media_type}, "
                    f"error={error_name})"
                )
                continue
            result.attachments.append(attachment)

    @staticmethod
    def _sanitize_extra_data(extra_data: Any) -> dict[str, Any]:
        if not isinstance(extra_data, dict):
            return {}

        sanitized: dict[str, Any] = {}
        for key, value in extra_data.items():
            if key in _MESSAGE_INIT_KEYS:
                sanitized[f"adapter_{key}"] = value
            else:
                sanitized[key] = value
        return sanitized

    # ─── message → envelope ───────────────────

    async def message_to_envelope(self, message: Message) -> MessageEnvelope:
        """将 Message 转换为 MessageEnvelope（用于向适配器发送）。

        Args:
            message: 核心业务消息对象

        Returns:
            MessageEnvelope: mofox-wire 消息信封
        """
        seg_list: list[SegPayload] = []

        text = self._extract_outbound_text(message)
        if text:
            seg_list.append({"type": "text", "data": text})

        # canonical 附件优先；descriptor-only 附件不物化、不发送，也不阻断 legacy 回退。
        seg_list.extend(self._collect_outbound_media_segments(message))

        # 万一消息内容完全为空，至少构造一个空文本段，以避免适配器解析异常
        if not seg_list:
            seg_list.append({"type": "text", "data": ""})

        # 构建 message_info
        # 构建要发送给适配器的 message_info 字段基础结构
        msg_info: MessageInfoPayload = {
            "platform": message.platform,
            "message_id": message.message_id,
            # 时间戳尽量使用已存在值，否则用当前时间
            "time": message.time if isinstance(message.time, float) else time.time(),
        }

        # 如果有 reply_to，在段列表前面插入 reply 段
        if message.reply_to:
            seg_list.insert(0, {"type": "reply", "data": message.reply_to})

        # 非引用回复时，支持显式 @ 指定用户。
        # 由上层在 message.extra["at_user_id"] 传入目标平台用户 ID。
        at_user_id = message.extra.get("at_user_id")
        if at_user_id and not message.reply_to:
            seg_list.insert(0, {"type": "at", "data": str(at_user_id)})

        target_user_id = message.extra.get("target_user_id")
        if _is_internal_target_id(target_user_id):
            target_user_id = None
        target_user_name = message.extra.get("target_user_name")

        stream_info: dict[str, Any] | None = None
        if message.stream_id and (message.chat_type == "group" or not target_user_id):
            from src.core.managers.stream_manager import get_stream_manager

            stream_info = await get_stream_manager().get_stream_info(message.stream_id)

        # 若目标用户未指定并且不是群聊，则尝试从流信息中回推 person_id -> user_id
        if not target_user_id and message.chat_type != "group" and stream_info:
            person_id = stream_info.get("person_id")
            if isinstance(person_id, str) and person_id:
                try:
                    from src.core.utils.user_query_helper import get_user_query_helper

                    person = await get_user_query_helper().person_crud.get_by(
                        person_id=person_id
                    )
                    if person and person.user_id:
                        target_user_id = str(person.user_id)
                except Exception:
                    target_user_id = None

        # 最后兜底：使用 sender_id，但跳过 life_engine 内部虚拟发送者
        # （如 "life_engine_nucleus"、"life_schedule" 等），避免下游 int() 转换失败
        if not target_user_id and not message.extra.get("is_life_engine_wake"):
            fallback_sender_id = str(message.sender_id or "").strip()
            if not _is_internal_target_id(fallback_sender_id):
                target_user_id = fallback_sender_id
        if not target_user_name:
            target_user_name = message.sender_name
        user_info_dict: dict[str, Any] = {
            "platform": message.platform,
            "user_id": target_user_id,
            "user_nickname": target_user_name,
        }
        if message.sender_cardname:
            user_info_dict["user_cardname"] = message.sender_cardname
        msg_info["user_info"] = user_info_dict  # type: ignore[typeddict-unknown-key]

        group_id = message.extra.get("target_group_id") or message.extra.get("group_id")
        group_name = message.extra.get("target_group_name") or message.extra.get("group_name")
        if message.chat_type == "group" and message.stream_id:
            if not group_id and stream_info:
                group_id = stream_info.get("group_id") or ""
                group_name = stream_info.get("group_name") or ""

            if group_id:
                msg_info["group_info"] = {  # type: ignore[typeddict-unknown-key]
                    "platform": message.platform,
                    "group_id": group_id,
                    "group_name": group_name or "",
                }

        envelope: MessageEnvelope = {
            "direction": "outgoing",
            "message_info": msg_info,
            "message_segment": seg_list,  # type: ignore[typeddict-item]
        }

        return envelope

    @staticmethod
    def _extract_outbound_text(message: Message) -> str:
        """提取适合直接发送的文本内容。"""
        if message.message_type == MessageType.TEXT:
            if message.processed_plain_text:
                return message.processed_plain_text
            if isinstance(message.content, str):
                return message.content
            if isinstance(message.content, dict):
                for key in ("text", "caption", "message", "content"):
                    value = message.content.get(key)
                    if isinstance(value, str) and value.strip():
                        return value
            return ""

        if isinstance(message.content, dict):
            for key in ("text", "caption", "message"):
                value = message.content.get(key)
                if isinstance(value, str) and value.strip():
                    return value

        if isinstance(message.content, str) and message.message_type == MessageType.TEXT:
            return message.content

        return ""

    @staticmethod
    def _find_outbound_media_source(
        raw_value: dict[str, Any],
        source_keys: tuple[str, ...],
    ) -> str:
        """Find a source in one priority class without crossing into another."""
        for key in source_keys:
            candidate = raw_value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()

        traversal_keys = (
            "data",
            "base64",
            "image_base64",
            "audio_base64",
            "voice_base64",
            "video_base64",
            "source",
            "image_url",
            "path",
            "file",
            "file_path",
            "filePath",
            "url",
            "uri",
            "src",
            "file_url",
            "download_url",
            "downloadUrl",
        )
        for key in traversal_keys:
            candidate = raw_value.get(key)
            if not isinstance(candidate, dict):
                continue
            nested = MessageConverter._find_outbound_media_source(
                candidate,
                source_keys,
            )
            if nested:
                return nested
        return ""

    @staticmethod
    def _normalize_outbound_media_value(media_type: str, raw_value: Any) -> str:
        """提取出站媒体 source；始终优先使用内联数据而非路径或 URL。"""
        value = ""
        if isinstance(raw_value, str):
            value = raw_value.strip()
        elif isinstance(raw_value, dict):
            inline_keys = (
                "data",
                "base64",
                "image_base64",
                "audio_base64",
                "voice_base64",
                "video_base64",
            )
            external_keys = (
                "path",
                "file",
                "file_path",
                "filePath",
                "url",
                "uri",
                "src",
                "file_url",
                "download_url",
                "downloadUrl",
            )
            value = MessageConverter._find_outbound_media_source(
                raw_value,
                inline_keys,
            ) or MessageConverter._find_outbound_media_source(
                raw_value,
                external_keys,
            )
        if not value:
            return ""

        if media_type != "file":
            if value.startswith("base64://"):
                return value[len("base64://") :]
            if value.startswith("base64|"):
                return value[len("base64|") :]
            if value.startswith("data:") and "base64," in value:
                return value.split("base64,", 1)[1]
        return value

    @staticmethod
    def _outbound_media_key(media_type: str, value: str) -> tuple[str, str]:
        """为 canonical/legacy 媒体构造不包含 body 的稳定去重键。"""
        encoded_value = value
        if encoded_value.startswith("base64://"):
            encoded_value = encoded_value[len("base64://") :]
        elif encoded_value.startswith("base64|"):
            encoded_value = encoded_value[len("base64|") :]
        elif encoded_value.startswith("data:") and "base64," in encoded_value:
            encoded_value = encoded_value.split("base64,", 1)[1]

        try:
            decoded = base64.b64decode(encoded_value, validate=True)
        except (ValueError, binascii.Error):
            digest = hashlib.sha256(
                value.encode("utf-8", errors="surrogatepass")
            ).hexdigest()
        else:
            digest = hashlib.sha256(decoded).hexdigest()
        return media_type, digest

    @classmethod
    def _outbound_media_keys(
        cls,
        media_type: str,
        raw_value: Any,
        normalized: str,
    ) -> set[tuple[str, str]]:
        """同时索引首选 source 与 legacy 字典中的 inline source。"""
        keys = {cls._outbound_media_key(media_type, normalized)}
        if not isinstance(raw_value, dict):
            return keys

        for field in ("data", "base64", "audio_base64", "voice_base64"):
            candidate = raw_value.get(field)
            if isinstance(candidate, str) and candidate.strip():
                keys.add(cls._outbound_media_key(media_type, candidate.strip()))
            elif isinstance(candidate, dict):
                nested = cls._normalize_outbound_media_value(media_type, candidate)
                if nested:
                    keys.update(cls._outbound_media_keys(media_type, candidate, nested))
        return keys

    @classmethod
    def _extract_primary_outbound_media(cls, message: Message) -> SegPayload | None:
        """提取主 legacy 媒体段（image/emoji/voice/video/file）。"""
        media_type = message.message_type.value
        if message.message_type not in {
            MessageType.IMAGE,
            MessageType.EMOJI,
            MessageType.VOICE,
            MessageType.VIDEO,
            MessageType.FILE,
        }:
            return None

        # Converter 入站消息把媒体统一放在 content.media；这里不再把整个容器
        # 猜成“主媒体”，否则验证失败的 adapter source 会绕过逐项隔离。
        if isinstance(message.content, dict) and isinstance(
            message.content.get("media"),
            list,
        ):
            return None

        normalized = cls._normalize_outbound_media_value(media_type, message.content)
        if not normalized:
            return None
        return {"type": media_type, "data": normalized}

    @staticmethod
    def _rejected_legacy_media(message: Message) -> dict[int, str]:
        """Return converter-rejected legacy indexes without trusting arbitrary shapes."""
        errors = message.extra.get("media_errors")
        if not isinstance(errors, list):
            return {}

        rejected: dict[int, str] = {}
        for error in errors:
            if not isinstance(error, dict):
                continue
            index = error.get("index")
            media_type = error.get("type")
            if (
                isinstance(index, int)
                and not isinstance(index, bool)
                and index >= 0
                and isinstance(media_type, str)
                and media_type.strip()
                and isinstance(error.get("error"), str)
                and error["error"].strip()
            ):
                rejected[index] = media_type.strip().lower()
        return rejected

    @classmethod
    def _collect_outbound_media_segments(
        cls,
        message: Message,
        *,
        seed: set[tuple[str, str]] | None = None,
    ) -> list[SegPayload]:
        """按 canonical、主 legacy、content.media、extra.media 的顺序收集媒体。"""
        collected: list[SegPayload] = []
        seen = {
            cls._outbound_media_key(media_type, value)
            for media_type, value in (seed or set())
        }

        for attachment in message.attachments:
            if attachment.media_ref.data is None:
                continue
            legacy = attachment.to_legacy()
            media_type = attachment.segment_type.value
            normalized = cls._normalize_outbound_media_value(media_type, legacy)
            if not normalized:
                continue
            key = (media_type, attachment.media_ref.sha256)
            if key in seen:
                continue
            seen.add(key)
            collected.append({"type": media_type, "data": normalized})

        def add(media_type: str, raw_value: Any) -> None:
            normalized_type = media_type.lower()
            normalized = cls._normalize_outbound_media_value(
                normalized_type,
                raw_value,
            )
            if not normalized:
                return
            keys = cls._outbound_media_keys(normalized_type, raw_value, normalized)
            if keys & seen:
                return
            seen.update(keys)
            collected.append({"type": normalized_type, "data": normalized})

        primary = cls._extract_primary_outbound_media(message)
        if primary is not None:
            add(str(primary.get("type", "unknown")), message.content)

        rejected = cls._rejected_legacy_media(message)

        def add_legacy_list(media_list: Any) -> None:
            if not isinstance(media_list, list):
                return
            for index, item in enumerate(media_list):
                if not isinstance(item, dict):
                    continue
                media_type = str(item.get("type", "unknown")).lower()
                if rejected.get(index) == media_type:
                    continue
                add(media_type, item)

        if isinstance(message.content, dict):
            add_legacy_list(message.content.get("media"))

        add_legacy_list(message.extra.get("media", []))

        return collected

    # ──────────────────────────────────────────
    # 内部方法
    # ──────────────────────────────────────────

    def _parse_segments(
        self,
        segments: list[SegPayload],
        depth: int = 0,
    ) -> _ParseResult:
        """递归地展开并解析段列表。

        depth 参数用于防止恶意或错误数据造成无限递归。
        返回值为 _ParseResult 对象，包含文本、媒体、@、reply 等信息。
        """
        result = _ParseResult()

        if depth > _MAX_NESTING_DEPTH:
            logger.warning(f"SegPayload 嵌套深度超过 {_MAX_NESTING_DEPTH} 层，截断")
            result.text_parts.append("[嵌套内容过深]")
            return result

        for seg in segments:
            try:
                # 每个段交给单段处理器；异常不会中断整个列表解析
                self._parse_single_segment(seg, result, depth)
            except Exception as exc:
                seg_type = seg.get("type", "unknown") if isinstance(seg, dict) else "invalid"
                error_name = type(exc).__name__
                logger.warning(
                    f"解析消息段失败 (type={seg_type}, error={error_name})"
                )
                # 记录错误位置，避免丢失整体文本结构
                result.text_parts.append(f"[解析失败:{seg_type}]")

        return result

    def _parse_single_segment(
        self,
        seg: SegPayload,
        result: _ParseResult,
        depth: int,
    ) -> None:
        """解析单个 SegPayload 并写入 result。

        Args:
            seg: 消息段
            result: 聚合结果（原地修改）
            depth: 当前递归深度
        """
        # 非 dict 类型的数据说明适配器异常，跳过处理同时记录警告
        if not isinstance(seg, dict):
            logger.warning(f"非法消息段类型: {type(seg)}")
            return

        seg_type: str = seg.get("type", "")
        data = seg.get("data", "")
        media_count = len(result.media)

        # 分发到专用 handler，便于各类型段独立演进
        match seg_type:
            case "text":
                self._handle_text(data, result)
            case "image":
                self._handle_image(data, result)
            case "emoji":
                self._handle_emoji(data, result)
            case "voice":
                self._handle_voice(data, result)
            case "video":
                self._handle_video(data, result)
            case "file":
                self._handle_file(data, result)
            case "at":
                self._handle_at(data, result)
            case "reply":
                self._handle_reply(data, seg, result, depth)
            case "seglist":
                self._handle_seglist(data, result, depth)
            case _:
                # 未知类型统一记录，后续可能用于统计或插件扩展
                self._handle_unknown(seg_type, data, result)

        if seg_type in _MEDIA_SEGMENT_TYPES and len(result.media) > media_count:
            media_item = result.media[-1]
            for key, value in seg.items():
                if key not in {"type", "data"}:
                    media_item[key] = _safe_copy_media_data(value)

    # ─── 段处理器 ─────────────────────────────

    @staticmethod
    def _handle_text(data: Any, result: _ParseResult) -> None:
        """处理文本段。"""
        if isinstance(data, str):
            result.text_parts.append(data)
        elif isinstance(data, list):
            # 理论上 text 的 data 是 str，但防御性处理
            result.text_parts.append(str(data))
        else:
            result.text_parts.append(str(data))

    @staticmethod
    def _handle_image(data: Any, result: _ParseResult) -> None:
        """保留图片 legacy 数据并添加占位符。"""
        if isinstance(data, str):
            media_data: Any = normalize_base64(data)
        else:
            media_data = _safe_copy_media_data(data)
        result.media.append({"type": "image", "data": media_data})
        result.text_parts.append("[图片]")

    @staticmethod
    def _handle_emoji(data: Any, result: _ParseResult) -> None:
        """保留表情包 legacy 数据并添加占位符。"""
        if isinstance(data, str):
            media_data: Any = normalize_base64(data)
        else:
            media_data = _safe_copy_media_data(data)
        result.media.append({"type": "emoji", "data": media_data})
        result.text_parts.append("[表情包]")

    @staticmethod
    def _handle_voice(data: Any, result: _ParseResult) -> None:
        """保留语音 legacy 数据并添加占位符。"""
        if isinstance(data, str):
            result.media.append({
                "type": "voice",
                "data": normalize_base64(data),
            })
            result.text_parts.append("[语音]")
            return

        normalized = _safe_copy_media_data(data)
        if isinstance(normalized, dict):
            for key in ("base64", "data", "audio_base64", "voice_base64"):
                value = normalized.get(key)
                if isinstance(value, str) and value.strip():
                    normalized[key] = normalize_base64(value)
                    break
            filename = normalized.get("filename") or normalized.get("name")
            result.text_parts.append(f"[语音文件:{filename}]" if filename else "[语音]")
        else:
            result.text_parts.append("[语音]")
        result.media.append({"type": "voice", "data": normalized})

    @staticmethod
    def _handle_video(data: Any, result: _ParseResult) -> None:
        """保留视频 legacy 数据并添加占位符。"""
        if isinstance(data, str):
            media_data: Any = {"base64": normalize_base64(data)}
        else:
            media_data = _safe_copy_media_data(data)
        result.media.append({"type": "video", "data": media_data})
        result.text_parts.append("[视频]")

    @staticmethod
    def _handle_file(data: Any, result: _ParseResult) -> None:
        """保真复制文件字段，并补齐兼容的 name/size/id aliases。"""
        parsed = safe_json_loads(data) if isinstance(data, str) else data

        if isinstance(parsed, dict):
            preserved = _safe_copy_media_data(parsed)
            preserved.setdefault(
                "name",
                parsed.get("name") or parsed.get("filename") or parsed.get("file", ""),
            )
            preserved.setdefault("size", parsed.get("size") or parsed.get("file_size"))
            preserved.setdefault("id", parsed.get("id") or parsed.get("file_id"))
            result.media.append({"type": "file", "data": preserved})
            file_name = preserved.get("name") or preserved.get("filename") or "文件"
            result.text_parts.append(f"[文件:{file_name}]")
        else:
            result.media.append({
                "type": "file",
                "data": _safe_copy_media_data(parsed),
            })
            result.text_parts.append("[文件]")

    @staticmethod
    def _handle_at(data: Any, result: _ParseResult) -> None:
        """处理 @ 段。

        data 格式约定: ``nickname:user_id``，或 ``user_id``。
        """
        if not isinstance(data, str):
            result.text_parts.append(f"@{data}")
            return

        if ":" in data:
            parts = data.split(":", 1)
            nickname = parts[0]
            user_id = parts[1]
        else:
            nickname = data
            user_id = data

        result.at_users.append({"nickname": nickname, "user_id": user_id})
        result.text_parts.append(f"@<{nickname}:{user_id}> ")

    def _handle_reply(
        self,
        data: Any,
        seg: SegPayload,
        result: _ParseResult,
        depth: int,
    ) -> None:
        """处理回复段。

        reply 段的 data 可以是：
        1. 字符串 — 被回复消息的 ID
        2. 嵌套段列表 — 回复内容的结构化表示
        """
        if isinstance(data, str):
            # data 是消息 ID
            if not result.reply_to:
                result.reply_to = data
            result.text_parts.append(f"[回复:{data}]")
        elif isinstance(data, list):
            # 嵌套段：递归解析；回复文本需要包裹，其他聚合字段完整合并。
            inner = self._parse_segments(data, depth + 1)
            if not result.reply_to:
                result.reply_to = inner.reply_to
            inner_text = inner.plain_text
            if inner_text:
                result.text_parts.append(f"「回复：{inner_text}」")
            else:
                result.text_parts.append("[回复]")
            result.media.extend(inner.media)
            result.attachments.extend(inner.attachments)
            result.media_errors.extend(inner.media_errors)
            result.at_users.extend(inner.at_users)
            result.unknown_segments.extend(inner.unknown_segments)

    def _handle_seglist(
        self,
        data: Any,
        result: _ParseResult,
        depth: int,
    ) -> None:
        """处理 seglist 段（嵌套段列表）。"""
        if isinstance(data, list):
            inner = self._parse_segments(data, depth + 1)
            result.merge(inner)
        else:
            logger.warning(f"seglist 的 data 不是列表: {type(data)}")
            result.text_parts.append(str(data))

    @staticmethod
    def _handle_unknown(seg_type: str, data: Any, result: _ParseResult) -> None:
        """处理未知类型的段。"""
        result.unknown_segments.append({"type": seg_type, "data": data})
        result.text_parts.append(f"[{seg_type}]")

    # ─── 辅助方法 ─────────────────────────────

    @staticmethod
    def _infer_message_type(result: _ParseResult) -> MessageType:
        """根据解析结果推断 MessageType。

        优先级：如果有媒体，按第一个媒体类型决定；否则为 TEXT。
        """
        # 无媒体时直接判定为文本消息
        if not result.media:
            return MessageType.TEXT

        first_media_type = result.media[0].get("type", "")

        # 媒体类型到枚举的映射表，可根据需要扩展
        type_mapping: dict[str, MessageType] = {
            "image": MessageType.IMAGE,
            "emoji": MessageType.EMOJI,
            "voice": MessageType.VOICE,
            "video": MessageType.VIDEO,
            "file": MessageType.FILE,
        }

        return type_mapping.get(first_media_type, MessageType.UNKNOWN)

    @staticmethod
    def _build_content(result: _ParseResult, message_type: MessageType) -> str | Any:
        """构建 Message.content 字段。

        - TEXT 类型: 返回纯文本
        - 含媒体: 返回结构化字典
        """
        # 文本消息直接提供纯字符串
        if message_type == MessageType.TEXT:
            return result.plain_text

        # 含媒体时返回一个包含文本和媒体列表的字典，保持兼容性
        return {
            "text": result.plain_text,
            "media": result.media,
        }
