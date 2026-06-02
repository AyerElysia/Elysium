"""消息转换器。

负责 ``MessageEnvelope``（wire 层传输格式）与 ``Message``（核心业务模型）之间的
双向转换。核心解析逻辑在 ``_parse_segments`` 中实现递归的 ``SegPayload`` 展开。

设计原则：
- 适配器传入的媒体数据（图片、语音、视频等）**已经是 base64 编码**，转换器不做下载。
- 嵌套 seglist 最多递归 3 层，超出以占位符替代。
- 单个段解析失败不影响整体，用占位符保留位置。
- 图片和表情包会通过 VLM 识别转换为文字描述；视频走“抽帧+总结”的非原生链路。
"""

from __future__ import annotations

import time
from typing import Any

from mofox_wire import MessageEnvelope, MessageInfoPayload, SegPayload

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
}


# ──────────────────────────────────────────────
#  段解析返回结构
# ──────────────────────────────────────────────

class _ParseResult:
    """段解析的聚合结果。

    Attributes:
        text_parts: 纯文本片段列表，最终用空字符串拼接
        media: 媒体资源列表，每项为 ``{"type": str, "data": Any}``
        reply_to: 被回复消息的 ID（仅第一个 reply 段生效）
        at_users: 被 @ 用户列表 ``[{"nickname": str, "user_id": str}]``
        unknown_segments: 无法识别的段类型记录
    """

    __slots__ = ("text_parts", "media", "reply_to", "at_users", "unknown_segments")

    def __init__(self) -> None:
        self.text_parts: list[str] = []
        self.media: list[dict[str, Any]] = []
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
        if other.reply_to and not self.reply_to:
            self.reply_to = other.reply_to
        self.at_users.extend(other.at_users)
        self.unknown_segments.extend(other.unknown_segments)


# ──────────────────────────────────────────────
#  MessageConverter
# ──────────────────────────────────────────────


class MessageConverter:
    """MessageEnvelope ↔ Message 双向转换器。

    实例无状态，可以作为单例在整个应用中复用。
    
支持媒体识别功能：
- 图片/表情包：VLM 识别
- 语音：ASR 转写
- 视频：关键帧摘要（非原生视频）

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

        # 递归解析段列表
        result = self._parse_segments(segments, depth=0)
        
        # 如果解析过程中发现有媒体资源，则后续需要考虑是否运行视觉语言模型识别。
        # 上层流程可按 stream 注册跳过 image/emoji VLM，让原生多模态模型直接看媒体。
        if result.media:
            # 提前提取 stream_id 用于判断是否跳过 VLM
            stream_id = extract_stream_id(message_info)
            skip_vlm_types = self._skip_vlm_media_types_for_stream(
                stream_id,
                {"image", "emoji"},
            )
            if skip_vlm_types:
                logger.debug(
                    f"聊天流 {stream_id[:8]} 已跳过 VLM 图片识别，"
                    f"media_types={sorted(skip_vlm_types)}；video 仍会走非原生摘要链路"
                )
            result = await self._recognize_media_with_manager(
                result,
                skip_vlm_types=skip_vlm_types,
            )

        # 确定消息类型
        # 根据最终解析结果决定消息类型，比如 TEXT/IMAGE 等
        message_type = self._infer_message_type(result)

        # 构建内容
        content = self._build_content(result, message_type)

        # 提取用户/群信息
        # 提取发送者及群组信息，注意 user_info 可能为空，因此使用空 dict 作为 fallback
        user_info = message_info.get("user_info") or {}
        group_info = message_info.get("group_info")
        group_id = group_info.get("group_id") if group_info else None
        group_name = group_info.get("group_name") if group_info else None

        # 提取发送者角色（UserRole 枚举转字符串）
        raw_role = user_info.get("role")
        sender_role: str | None = None
        if raw_role is not None:
            sender_role = raw_role.value

        # 提取 extra 元数据。附加字段允许上层扩展，但不能覆盖 Message 核心构造字段。
        extra_data = self._sanitize_extra_data(message_info.get("extra") or {})
        
        return Message(
            message_id=message_info.get("message_id", ""),
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
            media=result.media,
            at_users=result.at_users,
            unknown_segments=result.unknown_segments,
            group_id=group_id,
            group_name=group_name,
            **extra_data,
        )

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

        # 去重：对 seg_list 中已有的媒体段建 key 集合，避免主媒体重复追加
        media_seed = {
            (str(seg.get("type", "")), str(seg.get("data", "")))
            for seg in seg_list
            if isinstance(seg, dict) and seg.get("type") != "text"
        }

        primary_media = self._extract_primary_outbound_media(message)
        if primary_media:
            key = (str(primary_media.get("type", "")), str(primary_media.get("data", "")))
            if key not in media_seed:
                media_seed.add(key)
                seg_list.append(primary_media)

        seg_list.extend(self._collect_outbound_media_segments(message, seed=media_seed))

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
            target_user_id = message.sender_id
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
    def _normalize_outbound_media_value(media_type: str, raw_value: Any) -> str:
        """把媒体值规范成适合发送到 Napcat 的形式。"""
        value = ""
        if isinstance(raw_value, str):
            value = raw_value.strip()
        elif isinstance(raw_value, dict):
            keys = ("path", "file", "url", "data", "base64") if media_type != "file" else (
                "path",
                "file",
                "url",
            )
            for key in keys:
                candidate = raw_value.get(key)
                if isinstance(candidate, str) and candidate.strip():
                    value = candidate.strip()
                    break
                if isinstance(candidate, dict):
                    nested = MessageConverter._normalize_outbound_media_value(
                        media_type,
                        candidate,
                    )
                    if nested:
                        value = nested
                        break
        if not value:
            return ""

        if media_type != "file":
            if value.startswith("base64://"):
                value = value[len("base64://") :]
            elif value.startswith("base64|"):
                value = value[len("base64|") :]
            elif value.startswith("data:") and "base64," in value:
                value = value.split("base64,", 1)[1]

        return value

    @classmethod
    def _extract_primary_outbound_media(cls, message: Message) -> SegPayload | None:
        """提取主媒体段（image/emoji/voice/video/file）。"""
        media_type = message.message_type.value
        if message.message_type not in {
            MessageType.IMAGE,
            MessageType.EMOJI,
            MessageType.VOICE,
            MessageType.VIDEO,
            MessageType.FILE,
        }:
            return None

        content = message.content
        normalized = cls._normalize_outbound_media_value(media_type, content)
        if not normalized and isinstance(content, dict):
            normalized = cls._normalize_outbound_media_value(media_type, content)
        if not normalized:
            return None

        return {"type": media_type, "data": normalized}

    @classmethod
    def _collect_outbound_media_segments(
        cls,
        message: Message,
        *,
        seed: set[tuple[str, str]] | None = None,
    ) -> list[SegPayload]:
        """收集附加媒体段，来源包括 content.media 和 extra.media。"""
        collected: list[SegPayload] = []
        seen: set[tuple[str, str]] = set(seed or set())

        def add(media_type: str, raw_value: Any) -> None:
            normalized = cls._normalize_outbound_media_value(media_type, raw_value)
            if not normalized:
                return
            key = (media_type, normalized)
            if key in seen:
                return
            seen.add(key)
            collected.append({"type": media_type, "data": normalized})

        if isinstance(message.content, dict):
            media_list = message.content.get("media")
            if isinstance(media_list, list):
                for item in media_list:
                    if not isinstance(item, dict):
                        continue
                    add(str(item.get("type", "unknown")).lower(), item)

        media_list = message.extra.get("media", [])
        if isinstance(media_list, list):
            for item in media_list:
                if not isinstance(item, dict):
                    continue
                add(str(item.get("type", "unknown")).lower(), item)

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

        if depth >= _MAX_NESTING_DEPTH:
            logger.warning(f"SegPayload 嵌套深度超过 {_MAX_NESTING_DEPTH} 层，截断")
            result.text_parts.append("[嵌套内容过深]")
            return result

        for seg in segments:
            try:
                # 每个段交给单段处理器；异常不会中断整个列表解析
                self._parse_single_segment(seg, result, depth)
            except Exception as e:
                seg_type = seg.get("type", "unknown") if isinstance(seg, dict) else "invalid"
                logger.warning(f"解析消息段失败 (type={seg_type}): {e}")
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
        """处理图片段（适配器已编码为 base64）。
        
        如果启用了 VLM，会尝试识别图片内容并添加到文本中。
        """
        if isinstance(data, str):
            normalized_data = normalize_base64(data)
            result.media.append({
                "type": "image",
                "data": normalized_data,
            })
            
            # 添加图片描述占位符，等待异步识别
            result.text_parts.append("[图片]")
        elif isinstance(data, list):
            # data 是嵌套段 — 不常见，但规范允许
            result.media.append({"type": "image", "data": str(data)})
            result.text_parts.append("[图片]")

    @staticmethod
    def _handle_emoji( data: Any, result: _ParseResult) -> None:
        """处理表情包段（适配器已编码为 base64）。
        
        如果启用了 VLM，会尝试识别表情包内容并添加到文本中。
        """
        if isinstance(data, str):
            normalized_data = normalize_base64(data)
            result.media.append({
                "type": "emoji",
                "data": normalized_data,
            })
            
            # 表情包同样支持 VLM 识别，文本先占位
            result.text_parts.append("[表情包]")
        elif isinstance(data, list):
            result.media.append({"type": "emoji", "data": str(data)})
            result.text_parts.append("[表情包]")

    @staticmethod
    def _handle_voice(data: Any, result: _ParseResult) -> None:
        """处理语音段（适配器已编码为 base64）。"""
        if isinstance(data, str):
            result.media.append({
                "type": "voice",
                "data": normalize_base64(data),
            })
            result.text_parts.append("[语音]")
            return

        if isinstance(data, dict):
            normalized = dict(data)
            for key in ("base64", "data", "audio_base64"):
                value = normalized.get(key)
                if isinstance(value, str) and value.strip():
                    normalized[key] = normalize_base64(value)
                    break
            result.media.append({
                "type": "voice",
                "data": normalized,
            })
            filename = normalized.get("filename") or normalized.get("name")
            result.text_parts.append(f"[语音文件:{filename}]" if filename else "[语音]")
            return

    @staticmethod
    def _handle_video(data: Any, result: _ParseResult) -> None:
        """处理视频段。

        目前视频会走“非原生摘要链路”，因此这里先保留原始视频数据并打占位符。
        """
        if isinstance(data, dict):
            result.media.append({
                "type": "video",
                "data": data,
            })
            result.text_parts.append("[视频]")
            return

        if isinstance(data, str):
            result.media.append({
                "type": "video",
                "data": {"base64": normalize_base64(data)},
            })
            result.text_parts.append("[视频]")
            return

        result.media.append({"type": "video", "data": data})
        result.text_parts.append("[视频]")

    @staticmethod
    def _handle_file(data: Any, result: _ParseResult) -> None:
        """处理文件段。

        data 可能是 JSON 字符串或已解析的字典。
        """
        parsed = data
        if isinstance(data, str):
            parsed = safe_json_loads(data)

        if isinstance(parsed, dict):
            result.media.append({
                "type": "file",
                "data": {
                    "name": parsed.get("name") or parsed.get("file", ""),
                    "size": parsed.get("size") or parsed.get("file_size"),
                    "id": parsed.get("id") or parsed.get("file_id"),
                },
            })
            file_name = parsed.get("name") or parsed.get("file", "文件")
            result.text_parts.append(f"[文件:{file_name}]")
        else:
            # 无法解析结构，保留原始信息
            result.media.append({"type": "file", "data": parsed})
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
            # 嵌套段：递归解析
            inner = self._parse_segments(data, depth + 1)
            if not result.reply_to:
                result.reply_to = inner.reply_to
            inner_text = inner.plain_text
            if inner_text:
                result.text_parts.append(f"「回复：{inner_text}」")
            else:
                result.text_parts.append("[回复]")
            # 合并媒体等内容
            result.media.extend(inner.media)
            result.at_users.extend(inner.at_users)

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

    async def _recognize_media_with_manager(
        self,
        result: _ParseResult,
        *,
        skip_vlm_types: set[str] | None = None,
    ) -> _ParseResult:
        """使用 MediaManager 识别媒体内容（图片、表情包、语音、视频）并更新文本描述。
        
        Args:
            result: 解析结果
            skip_vlm_types: 要跳过 VLM 文本识别的媒体类型
            
        Returns:
            更新后的解析结果
        """
        try:
            # 延迟导入避免循环依赖
            from src.core.managers.media_manager import get_media_manager
            
            manager = get_media_manager()
            skip_vlm_types = skip_vlm_types or set()
            
            image_descriptions: list[str | None] = []
            emoji_descriptions: list[str | None] = []
            voice_descriptions: list[str | None] = []
            video_descriptions: list[str | None] = []

            has_recognition_target = False
            for media in result.media:
                media_type = str(media.get("type", ""))
                if media_type in ("image", "emoji"):
                    has_recognition_target = True
                    if media_type in skip_vlm_types:
                        if media_type == "image":
                            image_descriptions.append(None)
                        else:
                            emoji_descriptions.append(None)
                        continue
                    try:
                        description = await manager.recognize_media(
                            media["data"],
                            media_type,
                            use_cache=True,
                        )
                    except Exception as e:
                        logger.warning(f"识别{media_type}失败: {e}")
                        description = None

                    if media_type == "image":
                        image_descriptions.append(description)
                    else:
                        emoji_descriptions.append(description)
                elif media_type == "voice":
                    has_recognition_target = True
                    try:
                        description = await manager.recognize_voice(
                            media.get("data"),
                            use_cache=True,
                        )
                    except Exception as e:
                        logger.warning(f"识别 voice 失败: {e}")
                        description = None
                    voice_descriptions.append(description)
                elif media_type == "video":
                    has_recognition_target = True
                    try:
                        description = await manager.recognize_video(
                            media.get("data"),
                            use_cache=True,
                        )
                    except Exception as e:
                        logger.warning(f"识别 video 失败: {e}")
                        description = None
                    video_descriptions.append(description)

            # 早退策略：没有待识别媒体就直接返回原结果
            if not has_recognition_target:
                return result

            # 将识别结果应用回 text_parts，按占位符顺序替换
            new_text_parts = []
            image_idx = 0
            emoji_idx = 0
            voice_idx = 0
            video_idx = 0
            for part in result.text_parts:
                if part == "[图片]":
                    description = image_descriptions[image_idx] if image_idx < len(image_descriptions) else None
                    image_idx += 1
                    new_text_parts.append(f"[图片:{description}]" if description else part)
                elif part == "[表情包]":
                    description = emoji_descriptions[emoji_idx] if emoji_idx < len(emoji_descriptions) else None
                    emoji_idx += 1
                    new_text_parts.append(f"[表情包:{description}]" if description else part)
                elif part == "[语音]" or part.startswith("[语音文件:"):
                    description = voice_descriptions[voice_idx] if voice_idx < len(voice_descriptions) else None
                    voice_idx += 1
                    if description:
                        if part.startswith("[语音文件:") and part.endswith("]"):
                            new_text_parts.append(f"{part[:-1]} | {description}]")
                        else:
                            new_text_parts.append(f"[语音:{description}]")
                    else:
                        new_text_parts.append(part)
                elif part == "[视频]":
                    description = video_descriptions[video_idx] if video_idx < len(video_descriptions) else None
                    video_idx += 1
                    new_text_parts.append(f"[视频:{description}]" if description else part)
                else:
                    new_text_parts.append(part)
            
            result.text_parts = new_text_parts
            
        except Exception as e:
            # 如果整个识别流程失败，不应阻止消息继续处理，仅记录错误
            logger.error(f"MediaManager 识别失败: {e}", exc_info=True)
        
        return result
    
    @staticmethod
    def _skip_vlm_media_types_for_stream(
        stream_id: str,
        media_types: set[str],
    ) -> set[str]:
        """返回指定聊天流已注册跳过 VLM 识别的媒体类型。

        Args:
            stream_id: 聊天流 ID
            media_types: 待检查媒体类型

        Returns:
            已注册跳过的媒体类型集合
        """
        try:
            from src.core.managers.media_manager import get_media_manager

            manager = get_media_manager()
            return {
                media_type
                for media_type in media_types
                if manager.should_skip_vlm(stream_id, media_type)
            }
        except Exception:
            return set()

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
