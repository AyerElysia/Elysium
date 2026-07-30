"""出站消息发送器

将 MessageEnvelope 转换为 NapCat OneBot API 调用：
- 支持：text / image / emoji / voice / music / video / file / at / reply / forward / face / json / share
- 消息拆分：按换行符拆分长消息
- reply 段自动前置
"""

from __future__ import annotations

import random
from typing import TYPE_CHECKING, Any

from mofox_wire import GroupInfoPayload, MessageEnvelope, MessageInfoPayload, SegPayload, UserInfoPayload

from src.app.plugin_system.api.log_api import get_logger

from ..utils.media import convert_image_to_gif, get_image_format

if TYPE_CHECKING:
    from ..client import NapCatClient
    from ..config import NapcatAdapterConfig

logger = get_logger("napcat_adapter")


class OutgoingSender:
    """出站消息发送器：MessageEnvelope → NapCat API。"""

    def __init__(self, client: "NapCatClient", get_config: Any) -> None:
        self._client = client
        self._get_config = get_config

    def _config(self) -> "NapcatAdapterConfig | None":
        return self._get_config()

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------

    async def send(self, envelope: MessageEnvelope) -> None:
        """处理并发送一条出站消息。"""
        if not envelope:
            logger.warning("空消息，跳过")
            return

        message_info: MessageInfoPayload = envelope.get("message_info", {})
        message_segment = envelope.get("message_segment", {})

        # 统一为 seglist
        if isinstance(message_segment, list):
            seg_data: SegPayload = {"type": "seglist", "data": message_segment}
        else:
            seg_data = message_segment or {}

        # 确定发送目标
        group_info: GroupInfoPayload | None = message_info.get("group_info")
        user_info: UserInfoPayload | None = message_info.get("user_info")

        # 递归处理消息段
        processed = await self._process_segment_recursive(seg_data, user_info or {})
        if not processed:
            logger.warning("消息处理结果为空，无法发送")
            return

        # reply 段前置
        processed.sort(key=lambda s: 0 if isinstance(s, dict) and s.get("type") == "reply" else 1)

        # 确定 action 和 target
        if group_info and group_info.get("group_id"):
            action = "send_group_msg"
            target_key = "group_id"
            target_id = int(group_info["group_id"])
        elif user_info and user_info.get("user_id"):
            action = "send_private_msg"
            target_key = "user_id"
            target_id = int(user_info["user_id"])
        else:
            logger.error("无法确定发送目标")
            return

        # 按换行拆分
        chunks = self._split_by_newline(processed)
        if len(chunks) > 1:
            logger.info(f"出站消息拆分发送: count={len(chunks)}, action={action}, target={target_id}")

        for i, chunk in enumerate(chunks, 1):
            logger.debug(f"发送消息 part={i}/{len(chunks)}: {str(chunk)[:300]}")
            resp = await self._client.call(action, {target_key: target_id, "message": chunk})
            if resp.get("status") != "ok":
                raise RuntimeError(f"消息发送失败: {resp}")

        logger.info("消息发送成功")

    # ------------------------------------------------------------------
    # 消息段递归处理
    # ------------------------------------------------------------------

    async def _process_segment_recursive(
        self, seg: SegPayload, user_info: UserInfoPayload
    ) -> list[dict[str, Any]]:
        """递归处理消息段，返回 OneBot 消息段列表。"""
        payload: list[dict[str, Any]] = []

        if seg.get("type") == "seglist":
            for sub_seg in seg.get("data", []):
                if isinstance(sub_seg, dict):
                    payload = await self._process_by_type(sub_seg, payload, user_info)
        else:
            payload = await self._process_by_type(seg, payload, user_info)

        return payload

    async def _process_by_type(
        self, seg: SegPayload, payload: list[dict], user_info: UserInfoPayload
    ) -> list[dict]:
        """根据消息段类型处理。"""
        seg_type = seg.get("type")

        match seg_type:
            case "reply":
                target_id = str(seg.get("data", ""))
                if target_id == "notice":
                    return payload
                reply_segs = await self._build_reply(target_id, user_info)
                return self._merge_reply(payload, reply_segs)

            case "text":
                text = seg.get("data")
                if text:
                    payload.append({"type": "text", "data": {"text": str(text)}})
                return payload

            case "at":
                at_data = seg.get("data")
                if at_data:
                    # at_data 格式: "nickname:user_id" 或纯 user_id
                    qq_id = str(at_data).split(":")[-1] if ":" in str(at_data) else str(at_data)
                    payload.append({"type": "at", "data": {"qq": qq_id}})
                    payload.append({"type": "text", "data": {"text": " "}})
                return payload

            case "image":
                image_data = str(seg.get("data", ""))
                payload.append(self._build_image(image_data, subtype=0))
                return payload

            case "emoji":
                emoji_data = str(seg.get("data", ""))
                emoji_seg = await self._build_emoji(emoji_data)
                if emoji_seg:
                    payload.append(emoji_seg)
                return payload

            case "voice":
                voice_data = str(seg.get("data", ""))
                if voice_data:
                    file_val = voice_data if voice_data.startswith(("base64://", "http://", "https://")) else f"base64://{voice_data}"
                    payload.append({"type": "record", "data": {"file": file_val}})
                return payload

            case "voiceurl":
                voice_url = str(seg.get("data", ""))
                if voice_url:
                    payload.append({"type": "record", "data": {"file": voice_url}})
                return payload

            case "music":
                song_id = str(seg.get("data", ""))
                if song_id:
                    payload.append({"type": "music", "data": {"type": "163", "id": song_id}})
                return payload

            case "videourl":
                video_url = str(seg.get("data", ""))
                if video_url:
                    payload.append({"type": "video", "data": {"file": video_url}})
                return payload

            case "file":
                file_path = str(seg.get("data", ""))
                if file_path:
                    payload.append({"type": "file", "data": {"file": f"file://{file_path}"}})
                return payload

            case "face":
                # QQ 原生表情
                face_id = seg.get("data")
                if face_id:
                    payload.append({"type": "face", "data": {"id": str(face_id)}})
                return payload

            case "forward":
                # 合并转发消息（需要特殊处理）
                forward_data = seg.get("data")
                if forward_data:
                    payload.append({"type": "forward", "data": {"id": str(forward_data)}})
                return payload

            case "json":
                # JSON 卡片消息
                json_data = seg.get("data")
                if json_data:
                    payload.append({"type": "json", "data": {"data": str(json_data)}})
                return payload

            case "share":
                # 链接分享
                share_data = seg.get("data", {})
                if isinstance(share_data, dict):
                    payload.append({
                        "type": "share",
                        "data": {
                            "url": share_data.get("url", ""),
                            "title": share_data.get("title", ""),
                            "content": share_data.get("content", ""),
                            "image": share_data.get("image", ""),
                        },
                    })
                return payload

            case "seglist":
                # 嵌套列表
                for sub_seg in seg.get("data", []):
                    if isinstance(sub_seg, dict):
                        payload = await self._process_by_type(sub_seg, payload, user_info)
                return payload

            case _:
                logger.debug(f"未处理的出站消息段类型: {seg_type}")
                return payload

    # ------------------------------------------------------------------
    # 消息段构建辅助
    # ------------------------------------------------------------------

    def _build_image(self, data: str, subtype: int = 0) -> dict:
        """构建图片消息段。"""
        if data.startswith(("base64://", "http://", "https://")):
            file_val = data
        else:
            file_val = f"base64://{data}"
        return {"type": "image", "data": {"file": file_val, "subtype": subtype}}

    async def _build_emoji(self, data: str) -> dict | None:
        """构建动画表情消息段（需转为 GIF）。"""
        if not data:
            return None
        try:
            image_data = data
            fmt = await get_image_format(data)
            if fmt != "gif":
                image_data = await convert_image_to_gif(data)
            file_val = image_data if image_data.startswith(("base64://", "http://", "https://")) else f"base64://{image_data}"
            return {"type": "image", "data": {"file": file_val, "subtype": 1, "summary": "[动画表情]"}}
        except Exception as e:
            logger.error(f"表情处理失败: {e}")
            return None

    async def _build_reply(self, message_id: str, user_info: UserInfoPayload) -> list[dict]:
        """构建回复消息段（含可选 @）。"""
        reply_seg = {"type": "reply", "data": {"id": message_id}}

        config = self._config()
        if not config or not config.features.enable_reply_at:
            return [reply_seg]

        try:
            resp = await self._client.call("get_msg", {"message_id": message_id})
            if resp.get("status") == "ok":
                sender = resp.get("data", {}).get("sender", {})
                replied_user_id = sender.get("user_id")
                if replied_user_id and random.random() < config.features.reply_at_rate:
                    return [
                        reply_seg,
                        {"type": "at", "data": {"qq": str(replied_user_id)}},
                        {"type": "text", "data": {"text": " "}},
                    ]
        except Exception as e:
            logger.debug(f"获取回复消息详情失败: {e}")

        return [reply_seg]

    def _merge_reply(self, payload: list[dict], reply_segs: list[dict]) -> list[dict]:
        """合并 reply 段（去重，保留最新）。"""
        result = list(reply_segs)
        for seg in payload:
            if isinstance(seg, dict) and seg.get("type") == "reply":
                continue  # 跳过旧的 reply
            result.append(seg)
        return result

    # ------------------------------------------------------------------
    # 消息拆分
    # ------------------------------------------------------------------

    @staticmethod
    def _has_body(seg: dict) -> bool:
        """判断消息段是否有正文内容。"""
        return seg.get("type", "") not in ("", "reply", "at")

    def _split_by_newline(self, processed: list[dict]) -> list[list[dict]]:
        """按换行符拆分消息。"""
        if not processed:
            return [processed]

        chunks: list[list[dict]] = []
        current: list[dict] = []
        saw_newline = False

        for seg in processed:
            if not isinstance(seg, dict):
                continue

            if seg.get("type") != "text":
                current.append(seg)
                continue

            seg_data = seg.get("data", {})
            if not isinstance(seg_data, dict):
                current.append(seg)
                continue

            text = seg_data.get("text")
            if not isinstance(text, str):
                current.append(seg)
                continue

            normalized = text.replace("\r\n", "\n").replace("\r", "\n")
            if "\n" not in normalized:
                current.append(seg)
                continue

            saw_newline = True
            parts = normalized.split("\n")
            for i, part in enumerate(parts):
                if part:
                    current.append({"type": "text", "data": {"text": part}})
                if i < len(parts) - 1:
                    if current and any(self._has_body(s) for s in current):
                        chunks.append(current)
                        current = []

        if current:
            chunks.append(current)

        if not saw_newline or not chunks:
            return [processed]

        return chunks
