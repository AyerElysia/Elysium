"""消息事件处理器

处理 message / message_sent 事件，将 OneBot 消息段转换为 MessageEnvelope。
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import orjson
from mofox_wire import MessageBuilder, SegPayload
from mofox_wire.types import UserRole

from src.app.plugin_system.api.log_api import get_logger
from src.core.utils.base64_helper import base64_encode_bytes
from src.kernel.concurrency import get_task_manager

from ..utils.cache import GROUP_INFO_TTL, MEMBER_INFO_TTL, SELF_INFO_TTL, get_cached, set_cached
from ..utils.constants import ACCEPT_FORMAT, QQ_FACE, RealMessageType
from ..utils.media import download_image_base64

if TYPE_CHECKING:
    from ..client import NapCatClient
    from ..config import NapcatAdapterConfig

logger = get_logger("napcat_adapter")


class MessageEventHandler:
    """处理来自 NapCat 的消息事件。"""

    _AUDIO_EXTENSIONS = frozenset({
        ".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".oga", ".opus", ".amr", ".silk",
    })
    _VIDEO_EXTENSIONS = frozenset({
        ".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi", ".flv", ".wmv", ".mpeg", ".mpg", ".3gp", ".ts", ".m2ts",
    })

    def __init__(self, client: "NapCatClient", get_config: Any) -> None:
        self._client = client
        self._get_config = get_config

    def _config(self) -> "NapcatAdapterConfig | None":
        return self._get_config()

    def _canonical_person_key(self, user_id: str) -> str:
        """Return an explicitly configured cross-platform person key."""
        config = self._config()
        if config is None:
            return ""
        aliases: dict[str, str] = {}
        for item in config.identity.account_identity_aliases:
            raw = str(item or "").strip()
            if not raw or "=" not in raw:
                continue
            account_id, canonical_key = raw.split("=", 1)
            account_id = account_id.strip()
            canonical_key = canonical_key.strip()
            if account_id and canonical_key:
                aliases[account_id] = canonical_key
        return aliases.get(str(user_id or "").strip(), "")

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------

    async def handle(self, raw: dict[str, Any]) -> Any:
        """处理 message 事件。"""
        # 忽略 Bot 自己的消息回声
        sender = raw.get("sender", {}) or {}
        sender_id = str(sender.get("user_id", "") or "").strip()
        self_id = str(raw.get("self_id", "") or "").strip()
        if sender_id and self_id and sender_id == self_id:
            logger.debug(f"忽略 Bot 自己的消息回声: message_id={raw.get('message_id', '')}")
            return None

        return await self._build_envelope(raw)

    async def handle_sent(self, raw: dict[str, Any]) -> Any:
        """处理 message_sent 事件（Bot 自己发送的消息回声感知）。

        默认不处理，避免重复。如需感知可在此扩展。
        """
        # 目前不推送 Bot 自己发的消息到核心
        return None

    # ------------------------------------------------------------------
    # Envelope 构建
    # ------------------------------------------------------------------

    async def _build_envelope(self, raw: dict[str, Any]) -> Any:
        """将原始消息构建为 MessageEnvelope。"""
        sender = raw.get("sender", {}) or {}
        message_type = raw.get("message_type")
        message_id = str(raw.get("message_id", ""))
        message_time = time.time()

        msg_builder = MessageBuilder()

        # 角色映射
        role = sender.get("role", "")
        if role == "owner":
            sender["role"] = UserRole.OWNER
        elif role == "admin":
            sender["role"] = UserRole.OPERATOR
        elif role == "member":
            sender["role"] = UserRole.MEMBER

        (
            msg_builder.direction("incoming")
            .message_id(message_id)
            .timestamp_ms(int(message_time * 1000))
            .from_user(
                user_id=str(sender.get("user_id", "")),
                platform="qq",
                nickname=sender.get("nickname", ""),
                cardname=sender.get("card", ""),
                user_avatar=sender.get("avatar", ""),
                role=sender.get("role", ""),
            )
        )

        # 群消息：附加群信息
        if message_type == "group":
            group_id = raw.get("group_id")
            if group_id:
                group_info = await self._get_group_info(group_id)
                msg_builder.from_group(
                    group_id=str(group_id),
                    platform="qq",
                    name=(group_info.get("group_name", "") if group_info else ""),
                )

        # 解析消息段
        segments = raw.get("message", [])
        seg_list: list[SegPayload] = []

        for segment in segments:
            seg = await self._handle_segment(segment, raw)
            if seg:
                seg_list.append(seg)

        if not seg_list:
            logger.warning("消息内容为空，添加占位符文本")
            seg_list.append({"type": "text", "data": "[消息内容为空]"})

        msg_builder.format_info(
            content_format=[seg["type"] for seg in seg_list],
            accept_format=ACCEPT_FORMAT,
        )
        msg_builder.seg_list(seg_list)

        envelope = msg_builder.build()
        sender_id = str(sender.get("user_id", "") or "").strip()
        canonical_person_key = self._canonical_person_key(sender_id)
        message_info = envelope["message_info"]
        extra = message_info.get("extra")
        if not isinstance(extra, dict):
            extra = {}
            message_info["extra"] = extra
        extra.update({
            "sender_platform_account_key": f"qq:{sender_id}" if sender_id else "",
            "canonical_person_key": canonical_person_key,
            "identity_resolution_status": "resolved",
            "identity_display_name_source": "onebot_sender",
        })
        return envelope

    # ------------------------------------------------------------------
    # 消息段分发
    # ------------------------------------------------------------------

    async def _handle_segment(
        self, segment: dict, raw_message: dict, in_reply: bool = False
    ) -> SegPayload | None:
        """处理单个消息段。"""
        seg_type = segment.get("type")

        match seg_type:
            case RealMessageType.text:
                return self._handle_text(segment)
            case RealMessageType.face:
                return self._handle_face(segment)
            case RealMessageType.image:
                return await self._handle_image(segment)
            case RealMessageType.at:
                return await self._handle_at(segment, raw_message)
            case RealMessageType.reply:
                return await self._handle_reply(segment, raw_message, in_reply)
            case RealMessageType.record:
                return await self._handle_record(segment)
            case RealMessageType.video:
                return await self._handle_video(segment)
            case RealMessageType.rps:
                return self._handle_rps(segment)
            case RealMessageType.dice:
                return self._handle_dice(segment)
            case RealMessageType.forward:
                return await self._handle_forward(segment)
            case RealMessageType.json:
                return self._handle_json(segment)
            case RealMessageType.file:
                return await self._handle_file(segment, raw_message)
            case _:
                logger.debug(f"不支持的消息段类型: {seg_type}")
                return None

    # ------------------------------------------------------------------
    # 各类型消息段处理
    # ------------------------------------------------------------------

    def _handle_text(self, segment: dict) -> SegPayload:
        data = segment.get("data", {})
        return {"type": "text", "data": data.get("text", "")}

    def _handle_face(self, segment: dict) -> SegPayload | None:
        data = segment.get("data", {})
        face_id = str(data.get("id", ""))
        if face_id in QQ_FACE:
            return {"type": "text", "data": QQ_FACE[face_id]}
        logger.debug(f"未知表情 ID: {face_id}")
        return None

    async def _handle_image(self, segment: dict) -> SegPayload | None:
        data = segment.get("data", {})
        if not isinstance(data, dict):
            return {"type": "text", "data": "[无法解析的图片]"}

        image_url = data.get("url", "")
        if not isinstance(image_url, str) or not image_url:
            logger.warning("图片消息缺少 URL")
            return None

        # 解析 sub_type
        raw_sub_type = data.get("sub_type")
        if raw_sub_type is None:
            image_sub_type = 1
        elif isinstance(raw_sub_type, bool):
            return {"type": "text", "data": "[无法解析的图片]"}
        elif isinstance(raw_sub_type, int):
            image_sub_type = raw_sub_type
        elif isinstance(raw_sub_type, str):
            try:
                image_sub_type = int(raw_sub_type.strip(), 10)
            except ValueError:
                return {"type": "text", "data": "[无法解析的图片]"}
        else:
            return {"type": "text", "data": "[无法解析的图片]"}

        if image_sub_type not in {0, 1, 2, 3, 4, 5, 6, 7, 9}:
            return {"type": "text", "data": "[无法解析的图片]"}

        try:
            async with asyncio.timeout(10):
                image_base64 = await download_image_base64(image_url)
        except TimeoutError:
            logger.error(f"图片下载超时: {image_url}")
            return {"type": "text", "data": "[图片处理超时]"}
        except Exception as e:
            logger.error(f"图片处理失败: {e}")
            return None

        if image_sub_type == 0:
            return {"type": "image", "data": image_base64}
        return {"type": "emoji", "data": image_base64}

    async def _handle_at(self, segment: dict, raw_message: dict) -> SegPayload | None:
        data = segment.get("data", {})
        if not data:
            return None

        qq_id = data.get("qq")
        self_id = raw_message.get("self_id")
        group_id = raw_message.get("group_id")

        if str(self_id) == str(qq_id):
            # 被 @ 了
            self_info = await self._get_self_info()
            if self_info:
                return {"type": "at", "data": f"{self_info.get('nickname')}:{self_info.get('user_id')}"}
            return None
        else:
            if qq_id and group_id:
                member_info = await self._get_member_info(group_id, qq_id)
                if member_info:
                    return {"type": "at", "data": f"{member_info.get('nickname')}:{member_info.get('user_id')}"}
            return None

    async def _handle_reply(
        self, segment: dict, raw_message: dict, in_reply: bool
    ) -> SegPayload | None:
        if in_reply:
            return None

        data = segment.get("data", {})
        if not data:
            return None

        message_id = data.get("id")
        if not message_id:
            return None

        message_detail = await self._get_message_detail(message_id)
        if not message_detail:
            return {"type": "text", "data": "[无法获取被引用的消息]"}

        # 递归处理被引用消息
        reply_segments: list[SegPayload] = []
        for reply_seg in message_detail.get("message", []):
            if isinstance(reply_seg, dict):
                result = await self._handle_segment(reply_seg, raw_message, in_reply=True)
                if result:
                    reply_segments.append(result)

        sender_info = message_detail.get("sender", {})
        sender_nickname = sender_info.get("nickname") or "未知用户"
        sender_id = sender_info.get("user_id")

        prefix = f"[回复<{sender_nickname}({sender_id})>：" if sender_id else f"[回复<{sender_nickname}>："
        suffix = "]，说："

        brief_segments = [
            {"type": seg.get("type", "text"), "data": seg.get("data", "")} for seg in reply_segments
        ] or [{"type": "text", "data": "[无法获取被引用的消息]"}]

        return {
            "type": "seglist",
            "data": [{"type": "text", "data": prefix}, *brief_segments, {"type": "text", "data": suffix}],
        }

    async def _handle_record(self, segment: dict) -> SegPayload | None:
        data = segment.get("data", {})
        file = data.get("file", "")
        if not file:
            return None

        try:
            resp = await self._client.call("get_record", {"file": file, "out_format": "mp3"})
            record_data = resp.get("data", {})
            audio_base64 = record_data.get("base64", "")
            if not audio_base64:
                # 尝试从 URL 下载
                url = record_data.get("url") or record_data.get("file")
                if url and isinstance(url, str) and url.startswith("http"):
                    import httpx
                    async with httpx.AsyncClient(timeout=15) as client:
                        r = await client.get(url)
                        if r.status_code == 200:
                            audio_base64 = await get_task_manager().to_thread(base64_encode_bytes, r.content)
        except Exception as e:
            logger.error(f"语音消息处理失败: {e}")
            return None

        if not audio_base64:
            logger.warning("语音消息未获取到音频数据")
            return None

        return {"type": "voice", "data": audio_base64}

    async def _handle_video(self, segment: dict) -> SegPayload | None:
        config = self._config()
        if config and not config.features.enable_video_processing:
            return {"type": "text", "data": "[视频消息]"}

        data = segment.get("data", {})

        # 直接有 base64
        video_base64 = data.get("base64")
        if isinstance(video_base64, str) and video_base64:
            if video_base64.startswith("base64://"):
                video_base64 = video_base64[9:]
            return {
                "type": "video",
                "data": {
                    "base64": video_base64,
                    "filename": data.get("filename", "video.mp4"),
                },
            }

        # 从 URL 或本地路径获取
        video_url = data.get("url")
        file_path = data.get("filePath") or data.get("file_path")

        if file_path and Path(file_path).exists():
            video_bytes = await asyncio.to_thread(Path(file_path).read_bytes)
            video_base64 = await get_task_manager().to_thread(base64_encode_bytes, video_bytes)
            return {
                "type": "video",
                "data": {
                    "base64": video_base64,
                    "filename": Path(file_path).name,
                    "size_mb": len(video_bytes) / (1024 * 1024),
                },
            }
        elif video_url:
            try:
                import httpx
                timeout_val = config.features.video_download_timeout if config else 60
                max_size = (config.features.video_max_size_mb if config else 200) * 1024 * 1024
                async with httpx.AsyncClient(timeout=timeout_val, follow_redirects=True) as client:
                    r = await client.get(video_url)
                    if r.status_code == 200 and len(r.content) <= max_size:
                        video_base64 = await get_task_manager().to_thread(base64_encode_bytes, r.content)
                        return {
                            "type": "video",
                            "data": {
                                "base64": video_base64,
                                "filename": "video.mp4",
                                "size_mb": len(r.content) / (1024 * 1024),
                                "url": video_url,
                            },
                        }
            except Exception as e:
                logger.error(f"视频下载失败: {e}")

        return {"type": "text", "data": "[视频消息]"}

    def _handle_rps(self, segment: dict) -> SegPayload:
        data = segment.get("data", {})
        res = data.get("result", "")
        shape = {"1": "布", "2": "剪刀"}.get(res, "石头")
        return {"type": "text", "data": f"[发送了一个魔法猜拳表情，结果是：{shape}]"}

    def _handle_dice(self, segment: dict) -> SegPayload:
        data = segment.get("data", {})
        res = data.get("result", "")
        return {"type": "text", "data": f"[扔了一个骰子，点数是{res}]"}

    async def _handle_forward(self, segment: dict) -> SegPayload | None:
        """处理合并转发消息。"""
        data = segment.get("data", {})
        forward_id = data.get("id")
        if not forward_id:
            return None

        try:
            resp = await self._client.call("get_forward_msg", {"id": forward_id})
            messages = resp.get("data", {}).get("messages", [])
        except Exception as e:
            logger.warning(f"获取转发消息失败: {e}")
            return None

        if not messages:
            return None

        handled, image_count = await self._parse_forward_messages(messages, 0)
        if not handled:
            return None

        # 图片数量少则解析为 base64，否则用占位符
        if 0 < image_count < 5:
            processed = await self._resolve_forward_images(handled, to_image=True)
        elif image_count > 0:
            processed = await self._resolve_forward_images(handled, to_image=False)
        else:
            processed = handled

        return {
            "type": "seglist",
            "data": [{"type": "text", "data": "这是一条转发消息：\n"}, processed],
        }

    async def _parse_forward_messages(self, message_list: list, layer: int) -> tuple[SegPayload | None, int]:
        """递归解析转发消息列表。"""
        seg_list: list[SegPayload] = []
        image_count = 0

        if not message_list:
            return None, 0

        for sub_msg in message_list:
            sender_info = sub_msg.get("sender", {})
            nickname = sender_info.get("nickname", "QQ用户")
            nickname_prefix = ("--" * layer) + f"【{nickname}】:" if layer > 0 else f"【{nickname}】:"

            msg_content = sub_msg.get("message")
            if not msg_content:
                continue

            first_seg = msg_content[0]
            seg_type = first_seg.get("type")

            if seg_type == RealMessageType.forward:
                if layer >= 3:
                    seg_list.append({"type": "text", "data": f"{nickname_prefix}【转发消息】\n"})
                else:
                    sub_data = first_seg.get("data", {})
                    contents = sub_data.get("content")
                    if contents:
                        nested, count = await self._parse_forward_messages(contents, layer + 1)
                        image_count += count
                        if nested:
                            seg_list.append({
                                "type": "seglist",
                                "data": [
                                    {"type": "text", "data": f"{nickname_prefix} 合并转发消息内容：\n"},
                                    nested,
                                ],
                            })
            elif seg_type == RealMessageType.text:
                text = first_seg.get("data", {}).get("text", "")
                seg_list.append({
                    "type": "seglist",
                    "data": [
                        {"type": "text", "data": nickname_prefix},
                        {"type": "text", "data": text},
                        {"type": "text", "data": "\n"},
                    ],
                })
            elif seg_type == RealMessageType.image:
                image_count += 1
                img_data = first_seg.get("data", {})
                img_url = img_data.get("url", "")
                sub_type = img_data.get("sub_type", 0)
                img_type = "image" if sub_type == 0 else "emoji"
                seg_list.append({
                    "type": "seglist",
                    "data": [
                        {"type": "text", "data": nickname_prefix},
                        {"type": img_type, "data": img_url},
                        {"type": "text", "data": "\n"},
                    ],
                })

        return {"type": "seglist", "data": seg_list}, image_count

    async def _resolve_forward_images(self, seg: SegPayload, to_image: bool) -> SegPayload:
        """递归处理转发消息中的图片段。"""
        if seg.get("type") == "seglist":
            new_list = [await self._resolve_forward_images(s, to_image) for s in seg.get("data", [])]
            return {"type": "seglist", "data": new_list}

        if seg.get("type") in ("image", "emoji"):
            if to_image:
                url = seg.get("data", "")
                if isinstance(url, str) and url.startswith("http"):
                    try:
                        b64 = await download_image_base64(url)
                        return {"type": seg["type"], "data": b64}
                    except Exception:
                        pass
                return {"type": "text", "data": "[图片]" if seg["type"] == "image" else "[表情包]"}
            else:
                return {"type": "text", "data": "[图片]" if seg["type"] == "image" else "[动画表情]"}

        return seg

    def _handle_json(self, segment: dict) -> SegPayload | None:
        """处理 JSON 卡片消息。"""
        data = segment.get("data", {})
        json_str = data.get("data", "")
        if not json_str:
            return None

        try:
            nested = orjson.loads(json_str)
        except Exception:
            return None

        if not isinstance(nested, dict):
            return None

        # 小程序分享
        if "app" in nested and "com.tencent.miniapp" in str(nested.get("app", "")):
            meta = nested.get("meta", {})
            detail = meta.get("detail_1", {})
            if detail:
                parts = []
                if detail.get("title"):
                    parts.append(f"来源: {detail['title']}")
                if detail.get("desc"):
                    parts.append(f"标题: {detail['desc']}")
                qqdocurl = detail.get("qqdocurl", "")
                if qqdocurl:
                    parts.append(f"链接: {qqdocurl}")
                if parts:
                    return {
                        "type": "text",
                        "data": "这是一条小程序分享消息，可以根据来源，考虑使用对应解析工具\n" + "\n".join(parts),
                    }

        # 音乐分享
        if nested.get("view") == "music" and "com.tencent.music" in str(nested.get("app", "")):
            music = nested.get("meta", {}).get("music", {})
            if music:
                tag = music.get("tag", "未知来源")
                title = music.get("title", "未知歌曲")
                desc = music.get("desc", "未知艺术家")
                jump_url = music.get("jumpUrl", "")
                return {
                    "type": "text",
                    "data": f"这是一张来自【{tag}】的音乐分享卡片：\n歌曲: {title}\n艺术家: {desc}\n跳转链接: {jump_url}",
                }

        # 图文分享
        if nested.get("view") == "news" and "com.tencent.tuwen" in str(nested.get("app", "")):
            news = nested.get("meta", {}).get("news", {})
            if news:
                tag = news.get("tag", "")
                title = news.get("title", "")
                desc = news.get("desc", "")
                jump_url = news.get("jumpUrl", "")
                return {
                    "type": "text",
                    "data": f"这是一条来自【{tag}】的分享：\n标题: {title}\n描述: {desc}\n链接: {jump_url}",
                }

        return None

    async def _handle_file(self, segment: dict, raw_message: dict | None = None) -> SegPayload | None:
        """处理文件消息（含视频/音频文件智能识别）。"""
        data = segment.get("data", {})
        if not data:
            return None

        file_name = data.get("file")
        file_size = data.get("file_size")
        file_id = data.get("file_id")

        # 视频文件检测
        if self._is_video_file(file_name):
            config = self._config()
            if not config or config.features.enable_video_processing:
                video_result = await self._try_resolve_video_from_file_data(data, raw_message)
                if video_result:
                    return video_result

        # 音频文件检测
        if self._is_audio_file(file_name):
            audio_result = await self._try_resolve_audio_from_file_data(data, raw_message)
            if audio_result:
                return audio_result

        # 普通文件
        return {
            "type": "file",
            "data": {"name": file_name, "size": file_size, "id": file_id},
        }

    # ------------------------------------------------------------------
    # 文件类型辅助
    # ------------------------------------------------------------------

    @classmethod
    def _is_video_file(cls, file_name: Any) -> bool:
        if not isinstance(file_name, str) or not file_name:
            return False
        normalized = file_name.split("?", 1)[0].split("#", 1)[0].lower()
        return Path(normalized).suffix in cls._VIDEO_EXTENSIONS

    @classmethod
    def _is_audio_file(cls, file_name: Any) -> bool:
        if not isinstance(file_name, str) or not file_name:
            return False
        normalized = file_name.split("?", 1)[0].split("#", 1)[0].lower()
        return Path(normalized).suffix in cls._AUDIO_EXTENSIONS

    @staticmethod
    def _extract_media_url(data: dict) -> str | None:
        """从 file 段 data 中提取 URL。"""
        for key in ("url", "file_url", "download_url", "downloadUrl", "src"):
            val = data.get(key)
            if isinstance(val, str) and val.startswith(("http://", "https://")):
                return val
        return None

    @staticmethod
    def _extract_media_path(data: dict) -> str | None:
        """从 file 段 data 中提取本地路径。"""
        for key in ("filePath", "file_path", "path", "local_path", "localPath"):
            val = data.get(key)
            if isinstance(val, str) and val:
                if val.startswith("file://"):
                    return val[7:]
                return val
        return None

    @staticmethod
    def _extract_media_base64(data: dict) -> str | None:
        raw = data.get("base64")
        if isinstance(raw, str) and raw:
            if raw.startswith("base64://"):
                return raw[9:]
            return raw
        return None

    async def _try_resolve_video_from_file_data(
        self, data: dict, raw_message: dict | None
    ) -> SegPayload | None:
        """尝试从文件数据中解析视频。"""
        # 直接有 base64
        b64 = self._extract_media_base64(data)
        if b64:
            return {"type": "video", "data": {"base64": b64, "filename": data.get("file", "video.mp4")}}

        # 有本地路径
        path = self._extract_media_path(data)
        if path and Path(path).exists():
            video_bytes = await asyncio.to_thread(Path(path).read_bytes)
            b64 = await get_task_manager().to_thread(base64_encode_bytes, video_bytes)
            return {
                "type": "video",
                "data": {"base64": b64, "filename": Path(path).name, "size_mb": len(video_bytes) / (1024 * 1024)},
            }

        # 有 URL
        url = self._extract_media_url(data)
        if url:
            return await self._handle_video({"type": "video", "data": {"url": url}})

        return None

    async def _try_resolve_audio_from_file_data(
        self, data: dict, raw_message: dict | None
    ) -> SegPayload | None:
        """尝试从文件数据中解析音频。"""
        b64 = self._extract_media_base64(data)
        if b64:
            return {"type": "voice", "data": {"base64": b64, "filename": data.get("file", "audio.mp3")}}

        path = self._extract_media_path(data)
        if path and Path(path).exists():
            audio_bytes = await asyncio.to_thread(Path(path).read_bytes)
            b64 = await get_task_manager().to_thread(base64_encode_bytes, audio_bytes)
            return {"type": "voice", "data": {"base64": b64, "filename": Path(path).name}}

        url = self._extract_media_url(data)
        if url:
            try:
                import httpx
                async with httpx.AsyncClient(timeout=30) as client:
                    r = await client.get(url)
                    if r.status_code == 200:
                        b64 = await get_task_manager().to_thread(base64_encode_bytes, r.content)
                        return {"type": "voice", "data": {"base64": b64, "filename": data.get("file", "audio.mp3")}}
            except Exception as e:
                logger.warning(f"音频下载失败: {e}")

        return None

    # ------------------------------------------------------------------
    # 缓存辅助方法
    # ------------------------------------------------------------------

    async def _get_group_info(self, group_id: Any) -> dict | None:
        key = str(group_id)
        cached = await get_cached("group_info", key, GROUP_INFO_TTL)
        if cached:
            return cached
        try:
            data = await self._client.call_data("get_group_info", {"group_id": group_id})
            if data:
                await set_cached("group_info", key, data)
            return data
        except Exception:
            return None

    async def _get_member_info(self, group_id: Any, user_id: Any) -> dict | None:
        key = f"{group_id}_{user_id}"
        cached = await get_cached("member_info", key, MEMBER_INFO_TTL)
        if cached:
            return cached
        try:
            data = await self._client.call_data(
                "get_group_member_info", {"group_id": group_id, "user_id": user_id}
            )
            if data:
                await set_cached("member_info", key, data)
            return data
        except Exception:
            return None

    async def _get_self_info(self) -> dict | None:
        cached = await get_cached("self_info", "self", SELF_INFO_TTL)
        if cached:
            return cached
        try:
            data = await self._client.call_data("get_login_info")
            if data:
                await set_cached("self_info", "self", data)
            return data
        except Exception:
            return None

    async def _get_message_detail(self, message_id: Any) -> dict | None:
        try:
            return await self._client.call_data("get_msg", {"message_id": message_id})
        except Exception:
            return None
