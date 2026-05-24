"""消息处理器 - 将 Napcat OneBot 消息转换为 MessageEnvelope"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import orjson
from mofox_wire import MessageBuilder, SegPayload
from mofox_wire.types import UserRole

from src.app.plugin_system.api.log_api import get_logger
from src.core.utils.base64_helper import base64_encode_bytes
from src.kernel.concurrency import get_task_manager

from ....config import NapcatAdapterConfig
from ...event_models import ACCEPT_FORMAT, QQ_FACE, RealMessageType
from ..utils import (
    get_forward_message,
    get_group_info,
    get_image_base64,
    get_member_info,
    get_message_detail,
    get_record_detail,
    get_self_info,
)

if TYPE_CHECKING:
    from ....plugin import NapcatAdapter

logger = get_logger("napcat_adapter")


class MessageHandler:
    """处理来自 Napcat 的消息事件"""

    _AUDIO_FILE_EXTENSIONS = frozenset(
        {
            ".mp3",
            ".wav",
            ".m4a",
            ".aac",
            ".flac",
            ".ogg",
            ".oga",
            ".opus",
            ".amr",
            ".silk",
        }
    )
    _VIDEO_FILE_EXTENSIONS = frozenset(
        {
            ".mp4",
            ".mov",
            ".m4v",
            ".webm",
            ".mkv",
            ".avi",
            ".flv",
            ".wmv",
            ".mpeg",
            ".mpg",
            ".3gp",
            ".ts",
            ".m2ts",
        }
    )

    def __init__(self, adapter: "NapcatAdapter"):
        self.adapter = adapter
        self._video_downloader = None
    
    def _init_video_downloader(self) -> None:
        """根据配置初始化视频下载器"""
        # 通过 adapter.plugin 访问配置
        if not self.adapter.plugin or not self.adapter.plugin.config:
            return

        config = cast(NapcatAdapterConfig, self.adapter.plugin.config)

        # 如果启用了视频处理，根据配置初始化视频下载器
        if config.features.enable_video_processing:
            from ..video_handler import VideoDownloader

            max_size = config.features.video_max_size_mb
            timeout = config.features.video_download_timeout

            self._video_downloader = VideoDownloader(max_size_mb=max_size, download_timeout=timeout)
            logger.debug(f"视频下载器已初始化: max_size={max_size}MB, timeout={timeout}s")

    async def handle_raw_message(self, raw: dict[str, Any]):
        """
        处理原始消息并转换为 MessageEnvelope

        Args:
            raw: OneBot 原始消息数据

        Returns:
            MessageEnvelope (dict) or None

        Note:
            黑白名单过滤已移动到 NapcatAdapter.from_platform_message 顶层执行，
            确保所有类型的事件（消息、通知等）都能被统一过滤。
        """
        sender_info = raw.get("sender", {}) or {}
        sender_user_id = str(sender_info.get("user_id", "") or "").strip()
        self_id = str(raw.get("self_id", "") or "").strip()
        if sender_user_id and self_id and sender_user_id == self_id:
            logger.debug(f"忽略机器人自己的消息回声: message_id={raw.get('message_id', '')}")
            return None

        message_type = raw.get("message_type")
        message_id = str(raw.get("message_id", ""))
        message_time = time.time()

        msg_builder = MessageBuilder()

        # 构造用户信息
        role = sender_info.get("role", "")
        if role == "owner":
            sender_info["role"] = UserRole.OWNER
        elif role == "admin":
            sender_info["role"] = UserRole.OPERATOR
        elif role == "member":
            sender_info["role"] = UserRole.MEMBER

        (
            msg_builder.direction("incoming")
            .message_id(message_id)
            .timestamp_ms(int(message_time * 1000))
            .from_user(
                user_id=str(sender_info.get("user_id", "")),
                platform="qq",
                nickname=sender_info.get("nickname", ""),
                cardname=sender_info.get("card", ""),
                user_avatar=sender_info.get("avatar", ""),
                role=sender_info.get("role", ""),
            )
        )

        # 构造群组信息（如果是群消息）
        if message_type == "group":
            group_id = raw.get("group_id")
            if group_id:
                fetched_group_info = await get_group_info(group_id)
                (
                    msg_builder.from_group(
                        group_id=str(group_id),
                        platform="qq",
                        name=(
                            fetched_group_info.get("group_name", "")
                            if fetched_group_info
                            else raw.get("group_name", "")
                        ),
                    )
                )

        # 解析消息段
        message_segments = raw.get("message", [])
        seg_list: list[SegPayload] = []

        for segment in message_segments:
            seg_message = await self.handle_single_segment(segment, raw)
            if seg_message:
                seg_list.append(seg_message)

        # 防御性检查：确保至少有一个消息段，避免消息为空导致构建失败
        if not seg_list:
            logger.warning("消息内容为空，添加占位符文本")
            seg_list.append({"type": "text", "data": "[消息内容为空]"})

        msg_builder.format_info(
            content_format=[seg["type"] for seg in seg_list],
            accept_format=ACCEPT_FORMAT,
        )

        msg_builder.seg_list(seg_list)

        return msg_builder.build()

    async def handle_single_segment(
        self, segment: dict, raw_message: dict, in_reply: bool = False
    ) -> SegPayload | None:
        """
        处理单一消息段并转换为 MessageEnvelope

        Args:
            segment: 单一原始消息段
            raw_message: 完整的原始消息数据

        Returns:
            SegPayload | None
        """
        seg_type = segment.get("type")

        self.adapter.plugin.config = cast(NapcatAdapterConfig, self.adapter.plugin.config) if self.adapter.plugin and self.adapter.plugin.config else None
        
        match seg_type:
            case RealMessageType.text:
                return await self._handle_text_message(segment)
            case RealMessageType.image:
                return await self._handle_image_message(segment)
            case RealMessageType.face:
                return await self._handle_face_message(segment)
            case RealMessageType.at:
                return await self._handle_at_message(segment, raw_message)
            case RealMessageType.reply:
                return await self._handle_reply_message(segment, raw_message, in_reply)
            case RealMessageType.record:
                return await self._handle_record_message(segment)
            case RealMessageType.video:
                # 检查是否启用了视频处理
                if (
                    self.adapter.plugin
                    and self.adapter.plugin.config
                    and not self.adapter.plugin.config.features.enable_video_processing
                ):
                    logger.debug("视频消息处理已禁用，跳过")
                    return {"type": "text", "data": "[视频消息]"}
                return await self._handle_video_message(segment)
            case RealMessageType.rps:
                return await self._handle_rps_message(segment)
            case RealMessageType.dice:
                return await self._handle_dice_message(segment)
            case RealMessageType.forward:
                messages = await get_forward_message(segment, adapter=self.adapter)
                if not messages:
                    logger.warning("转发消息内容为空或获取失败")
                    return None
                return await self.handle_forward_message(messages)
            case RealMessageType.json:
                return await self._handle_json_message(segment)
            case RealMessageType.file:
                return await self._handle_file_message(segment, raw_message)

            case _:
                logger.warning(f"Unsupported segment type: {seg_type}")
                return None

    # Utility methods for handling different message types

    async def _handle_text_message(self, segment: dict) -> SegPayload:
        """处理纯文本消息"""
        message_data = segment.get("data", {})
        plain_text = message_data.get("text", "")
        return {"type": "text", "data": plain_text}

    async def _handle_face_message(self, segment: dict) -> SegPayload | None:
        """处理表情消息"""
        message_data = segment.get("data", {})
        face_raw_id = str(message_data.get("id", ""))
        if face_raw_id in QQ_FACE:
            face_content = QQ_FACE.get(face_raw_id, "[未知表情]")
            return {"type": "text", "data": face_content}
        else:
            logger.warning(f"不支持的表情：{face_raw_id}")
            return None

    async def _handle_image_message(self, segment: dict) -> SegPayload | None:
        """处理图片消息与表情包消息"""
        message_data = segment.get("data", {})
        image_sub_type = message_data.get("sub_type")
        image_url = message_data.get("url", "")

        if not image_url:
            logger.warning("图片消息缺少URL")
            return None

        try:
            async with asyncio.timeout(10): # 兜底超时处理
                image_base64 = await get_image_base64(image_url)
        except TimeoutError:
            logger.error(f"图片消息处理超时: {image_url}")
            return {"type": "text", "data": "[图片处理超时]"}
        except Exception as e:
            logger.error(f"图片消息处理失败: {e!s}")
            return None
        if image_sub_type == 0:
            return {"type": "image", "data": image_base64}
        elif image_sub_type not in [4, 9]:
            return {"type": "emoji", "data": image_base64}
        else:
            logger.warning(f"不支持的图片子类型：{image_sub_type}")
            return None

    async def _handle_at_message(self, segment: dict, raw_message: dict) -> SegPayload | None:
        """处理@消息"""
        seg_data = segment.get("data", {})
        if not seg_data:
            return None

        qq_id = seg_data.get("qq")
        self_id = raw_message.get("self_id")
        group_id = raw_message.get("group_id")

        if str(self_id) == str(qq_id):
            logger.debug("机器人被at")
            self_info = await get_self_info()
            if self_info:
                return {"type": "at", "data": f"{self_info.get('nickname')}:{self_info.get('user_id')}"}
            return None
        else:
            if qq_id and group_id:
                member_info = await get_member_info(group_id=group_id, user_id=qq_id)
                if member_info:
                    return {"type": "at", "data": f"{member_info.get('nickname')}:{member_info.get('user_id')}"}
                return None

    async def _handle_reply_message(self, segment: dict, raw_message: dict, in_reply: bool) -> SegPayload | None:
        """处理回复消息"""
        if in_reply:
            return None

        seg_data = segment.get("data", {})
        if not seg_data:
            return None

        message_id = seg_data.get("id")
        if not message_id:
            return None

        message_detail = await get_message_detail(message_id)
        if not message_detail:
            logger.warning("获取被引用的消息详情失败")
            return {"type": "text", "data": "[无法获取被引用的消息]"}

        # 递归处理被引用的消息
        reply_segments: list[SegPayload] = []
        for reply_seg in message_detail.get("message", []):
            if isinstance(reply_seg, dict):
                reply_result = await self.handle_single_segment(reply_seg, raw_message, in_reply=True)
                if reply_result:
                    reply_segments.append(reply_result)

        sender_info = message_detail.get("sender", {})
        sender_nickname = sender_info.get("nickname") or "未知用户"
        sender_id = sender_info.get("user_id")

        prefix_text = f"[回复<{sender_nickname}({sender_id})>：" if sender_id else f"[回复<{sender_nickname}>："
        suffix_text = "]，说："

        # 将被引用的消息段落转换为可读的文本占位，避免嵌套的 base64 污染
        brief_segments = [
            {"type": seg.get("type", "text"), "data": seg.get("data", "")} for seg in reply_segments
        ] or [{"type": "text", "data": "[无法获取被引用的消息]"}]

        return {
            "type": "seglist",
            "data": [{"type": "text", "data": prefix_text}, *brief_segments, {"type": "text", "data": suffix_text}],
        }

    async def _handle_record_message(self, segment: dict) -> SegPayload | None:
        """处理语音消息"""
        message_data = segment.get("data", {})
        file = message_data.get("file", "")
        if not file:
            logger.warning("语音消息缺少文件信息")
            return None

        try:
            record_detail = await get_record_detail(file)
            if not record_detail:
                logger.warning("获取语音消息详情失败")
                return None
            audio_base64 = record_detail.get("base64", "")
        except Exception as e:
            logger.error(f"语音消息处理失败: {e!s}")
            return None

        if not audio_base64:
            logger.error("语音消息处理失败，未获取到音频数据")
            return None

        return {"type": "voice", "data": audio_base64}

    async def _handle_video_message(self, segment: dict) -> SegPayload | None:
        """处理视频消息"""
        message_data = segment.get("data", {})

        video_base64 = message_data.get("base64")
        if isinstance(video_base64, str) and video_base64:
            if video_base64.startswith("base64://"):
                video_base64 = video_base64[len("base64://") :]
            size_mb = message_data.get("size_mb")
            if size_mb is None:
                try:
                    size_mb = len(video_base64) * 3 / 4 / (1024 * 1024)
                except Exception:
                    size_mb = None
            payload: dict[str, Any] = {
                "base64": video_base64,
                "filename": message_data.get("filename", "video.mp4"),
            }
            if size_mb is not None:
                payload["size_mb"] = size_mb
            return {"type": "video", "data": payload}

        video_url = message_data.get("url")
        file_path = message_data.get("filePath") or message_data.get("file_path")

        video_source = file_path if file_path else video_url
        if not video_source:
            logger.warning("视频消息缺少URL或文件路径信息")
            return {"type": "text", "data": "[视频消息]"}

        try:
            if file_path and Path(file_path).exists():
                # 本地文件处理
                video_data = await asyncio.to_thread(Path(file_path).read_bytes)
                video_base64 = await get_task_manager().to_process(
                    base64_encode_bytes,
                    video_data,
                )
                logger.debug(f"视频文件大小: {len(video_data) / (1024 * 1024):.2f} MB")

                return {
                    "type": "video",
                    "data": {
                        "base64": video_base64,
                        "filename": Path(file_path).name,
                        "size_mb": len(video_data) / (1024 * 1024),
                    },
                }
            elif video_url:
                # URL下载处理 - 使用配置中的下载器实例
                downloader = self._video_downloader
                if not downloader:
                    from ..video_handler import get_video_downloader
                    downloader = get_video_downloader()

                download_result = await downloader.download_video(video_url)

                if not download_result["success"]:
                    logger.warning(f"视频下载失败: {download_result.get('error', '未知错误')}")
                    return {"type": "text", "data": f"[视频消息] ({download_result.get('error', '下载失败')})"}

                video_base64 = await get_task_manager().to_process(
                    base64_encode_bytes,
                    download_result["data"],
                )
                logger.debug(f"视频下载成功，大小: {len(download_result['data']) / (1024 * 1024):.2f} MB")

                return {
                    "type": "video",
                    "data": {
                        "base64": video_base64,
                        "filename": download_result.get("filename", "video.mp4"),
                        "size_mb": len(download_result["data"]) / (1024 * 1024),
                        "url": video_url,
                    },
                }
            else:
                logger.warning("既没有有效的本地文件路径，也没有有效的视频URL")
                return {"type": "text", "data": "[视频消息]"}

        except Exception as e:
            logger.error(f"视频消息处理失败: {e!s}")
            return {"type": "text", "data": "[视频消息处理出错]"}

    async def _handle_rps_message(self, segment: dict) -> SegPayload:
        """处理猜拳消息"""
        message_data = segment.get("data", {})
        res = message_data.get("result", "")
        shape_map = {"1": "布", "2": "剪刀"}
        shape = shape_map.get(res, "石头")
        return {"type": "text", "data": f"[发送了一个魔法猜拳表情，结果是：{shape}]"}

    async def _handle_dice_message(self, segment: dict) -> SegPayload:
        """处理骰子消息"""
        message_data = segment.get("data", {})
        res = message_data.get("result", "")
        return {"type": "text", "data": f"[扔了一个骰子，点数是{res}]"}


    async def handle_forward_message(self, message_list: list) -> SegPayload | None:
        """
        递归处理转发消息，并按照动态方式确定图片处理方式
        Parameters:
            message_list: list: 转发消息列表
        """
        handled_message, image_count = await self._handle_forward_message(message_list, 0)
        if not handled_message:
            return None

        if 0 < image_count < 5:
            logger.debug("图片数量小于5，开始解析图片为base64")
            processed_message = await self._recursive_parse_image_seg(handled_message, True)
        elif image_count > 0:
            logger.debug("图片数量大于等于5，开始解析图片为占位符")
            processed_message = await self._recursive_parse_image_seg(handled_message, False)
        else:
            logger.debug("没有图片，直接返回")
            processed_message = handled_message

        forward_hint = {"type": "text", "data": "这是一条转发消息：\n"}
        return {"type": "seglist", "data": [forward_hint, processed_message]}

    async def _recursive_parse_image_seg(self, seg_data: SegPayload, to_image: bool) -> SegPayload:
        # sourcery skip: merge-else-if-into-elif
        if seg_data.get("type") == "seglist":
            new_seg_list = []
            for i_seg in seg_data.get("data", []):
                parsed_seg = await self._recursive_parse_image_seg(i_seg, to_image)
                new_seg_list.append(parsed_seg)
            return {"type": "seglist", "data": new_seg_list}

        if to_image:
            if seg_data.get("type") == "image":
                image_url = seg_data.get("data")
                try:
                    encoded_image = await get_image_base64(image_url)
                except Exception as e:
                    logger.error(f"图片处理失败: {e!s}")
                    return {"type": "text", "data": "[图片]"}
                return {"type": "image", "data": encoded_image}
            if seg_data.get("type") == "emoji":
                image_url = seg_data.get("data")
                try:
                    encoded_image = await get_image_base64(image_url)
                except Exception as e:
                    logger.error(f"图片处理失败: {e!s}")
                    return {"type": "text", "data": "[表情包]"}
                return {"type": "emoji", "data": encoded_image}
            logger.debug(f"不处理类型: {seg_data.get('type')}")
            return seg_data

        if seg_data.get("type") == "image":
            return {"type": "text", "data": "[图片]"}
        if seg_data.get("type") == "emoji":
            return {"type": "text", "data": "[动画表情]"}
        logger.debug(f"不处理类型: {seg_data.get('type')}")
        return seg_data

    async def _handle_forward_message(self, message_list: list, layer: int) -> tuple[SegPayload | None, int]:
        # sourcery skip: low-code-quality
        """
        递归处理实际转发消息
        Parameters:
            message_list: list: 转发消息列表，首层对应messages字段，后面对应content字段
            layer: int: 当前层级
        Returns:
            seg_data: Seg: 处理后的消息段
            image_count: int: 图片数量
        """
        seg_list: list[SegPayload] = []
        image_count = 0
        if message_list is None:
            return None, 0
        for sub_message in message_list:
            sender_info: dict = sub_message.get("sender", {})
            user_nickname: str = sender_info.get("nickname", "QQ用户")
            user_nickname_str = f"【{user_nickname}】:"
            break_seg: SegPayload = {"type": "text", "data": "\n"}
            message_of_sub_message_list: list[dict[str, Any]] = sub_message.get("message")
            if not message_of_sub_message_list:
                logger.warning("转发消息内容为空")
                continue
            message_of_sub_message = message_of_sub_message_list[0]
            message_type = message_of_sub_message.get("type")
            if message_type == RealMessageType.forward:
                if layer >= 3:
                    full_seg_data: SegPayload = {
                        "type": "text",
                        "data": ("--" * layer) + f"【{user_nickname}】:【转发消息】\n",
                    }
                else:
                    sub_message_data = message_of_sub_message.get("data")
                    if not sub_message_data:
                        continue
                    contents = sub_message_data.get("content")
                    seg_data, count = await self._handle_forward_message(contents, layer + 1)
                    if seg_data is None:
                        continue
                    image_count += count
                    head_tip: SegPayload = {
                        "type": "text",
                        "data": ("--" * layer) + f"【{user_nickname}】: 合并转发消息内容：\n",
                    }
                    full_seg_data = {"type": "seglist", "data": [head_tip, seg_data]}
                seg_list.append(full_seg_data)
            elif message_type == RealMessageType.text:
                sub_message_data = message_of_sub_message.get("data")
                if not sub_message_data:
                    continue
                text_message = sub_message_data.get("text")
                seg_data: SegPayload = {"type": "text", "data": text_message}
                nickname_prefix = ("--" * layer) + user_nickname_str if layer > 0 else user_nickname_str
                data_list: list[SegPayload] = [
                    {"type": "text", "data": nickname_prefix},
                    seg_data,
                    break_seg,
                ]
                seg_list.append({"type": "seglist", "data": data_list})
            elif message_type == RealMessageType.image:
                image_count += 1
                image_data = message_of_sub_message.get("data", {})
                image_url = image_data.get("url")
                if not image_url:
                    logger.warning("转发消息图片缺少URL")
                    continue
                sub_type = image_data.get("sub_type")
                if sub_type == 0:
                    seg_data = {"type": "image", "data": image_url}
                else:
                    seg_data = {"type": "emoji", "data": image_url}
                nickname_prefix = ("--" * layer) + user_nickname_str if layer > 0 else user_nickname_str
                data_list = [
                    {"type": "text", "data": nickname_prefix},
                    seg_data,
                    break_seg,
                ]
                full_seg_data = {"type": "seglist", "data": data_list}
                seg_list.append(full_seg_data)
        return {"type": "seglist", "data": seg_list}, image_count

    @classmethod
    def _is_audio_file_name(cls, file_name: Any) -> bool:
        if not isinstance(file_name, str) or not file_name:
            return False
        normalized_name = file_name.split("?", 1)[0].split("#", 1)[0].lower()
        return Path(normalized_name).suffix in cls._AUDIO_FILE_EXTENSIONS

    @classmethod
    def _is_video_file_name(cls, file_name: Any) -> bool:
        if not isinstance(file_name, str) or not file_name:
            return False
        normalized_name = file_name.split("?", 1)[0].split("#", 1)[0].lower()
        return Path(normalized_name).suffix in cls._VIDEO_FILE_EXTENSIONS

    @staticmethod
    def _extract_media_source_from_file_data(file_data: dict[str, Any]) -> tuple[str | None, str | None]:
        """从 file 段 data 中提取可用的媒体 URL/文件路径。"""
        if not isinstance(file_data, dict):
            return None, None

        # 先尝试常见 URL 字段
        for key in ("url", "file_url", "download_url", "downloadUrl", "src"):
            value = file_data.get(key)
            if isinstance(value, str) and value.startswith(("http://", "https://")):
                return value, None

        # 再尝试本地路径字段
        for key in ("filePath", "file_path", "path", "local_path", "localPath", "temp_path", "filepath"):
            value = file_data.get(key)
            if isinstance(value, str) and value:
                if value.startswith("file://"):
                    return None, value[7:]
                return None, value

        # 某些实现会把 URL/路径塞在 file 字段
        raw_file = file_data.get("file")
        if isinstance(raw_file, str) and raw_file:
            if raw_file.startswith(("http://", "https://")):
                return raw_file, None
            if raw_file.startswith("file://"):
                return None, raw_file[7:]
            # 某些 get_file 返回值会把本地路径直接放在 file 字段
            if (
                raw_file.startswith(("/", "./", "../"))
                or "\\" in raw_file
                or ":" in raw_file[:3]  # 兼容 Windows 盘符路径
            ):
                return None, raw_file

        return None, None

    _extract_video_source_from_file_data = _extract_media_source_from_file_data

    @staticmethod
    def _extract_media_base64_from_file_data(file_data: dict[str, Any]) -> str | None:
        if not isinstance(file_data, dict):
            return None
        raw = file_data.get("base64")
        if isinstance(raw, str) and raw:
            if raw.startswith("base64://"):
                return raw[len("base64://") :]
            return raw
        return None

    _extract_video_base64_from_file_data = _extract_media_base64_from_file_data

    async def _try_resolve_video_source_by_file_api(
        self,
        *,
        file_id: str | None,
        file_name: str | None,
        group_id: str | int | None,
    ) -> tuple[str | None, str | None, str | None, dict[str, Any] | None]:
        """通过 Napcat File API 尝试解析视频源（base64 / url / path）。"""
        if not self.adapter:
            return None, None, None, None

        api_calls: list[tuple[str, dict[str, Any]]] = []
        if file_id:
            api_calls.append(("get_file", {"file_id": file_id}))
        if file_name:
            api_calls.append(("get_file", {"file": file_name}))
        if file_id:
            api_calls.append(("get_private_file_url", {"file_id": file_id}))
            if group_id:
                # 某些实现需要 busid，这里先尝试最小参数；失败会自动忽略
                api_calls.append(("get_group_file_url", {"group_id": str(group_id), "file_id": file_id}))

        for action, params in api_calls:
            try:
                resp = await self.adapter.send_napcat_api(action, params, timeout=8.0)
            except Exception as e:
                logger.debug(f"{action} 调用失败，params={params}: {e!s}")
                continue

            if not isinstance(resp, dict):
                continue
            if resp.get("status") not in ("ok", None):
                logger.debug(f"{action} 返回非成功状态: {resp}")
                continue

            data = resp.get("data")
            payload: dict[str, Any] | None = data if isinstance(data, dict) else None

            if isinstance(data, str) and data.startswith(("http://", "https://")):
                logger.info(f"{action} 成功获取视频 URL")
                return data, None, None, None

            if not payload:
                continue

            base64_data = self._extract_video_base64_from_file_data(payload)
            if base64_data:
                logger.info(f"{action} 成功获取视频 base64 数据")
                return None, None, base64_data, payload

            video_url, file_path = self._extract_video_source_from_file_data(payload)
            if video_url or file_path:
                logger.info(f"{action} 成功获取视频源: {'url' if video_url else 'path'}")
                return video_url, file_path, None, payload

        return None, None, None, None

    async def _try_resolve_audio_source_by_file_api(
        self,
        *,
        file_id: str | None,
        file_name: str | None,
        group_id: str | int | None,
    ) -> tuple[str | None, str | None, str | None, dict[str, Any] | None]:
        """通过 Napcat File API 尝试解析音频源（base64 / url / path）。"""
        if not self.adapter:
            return None, None, None, None

        api_calls: list[tuple[str, dict[str, Any]]] = []
        if file_id:
            api_calls.append(("get_file", {"file_id": file_id}))
        if file_name:
            api_calls.append(("get_file", {"file": file_name}))
        if file_id:
            api_calls.append(("get_private_file_url", {"file_id": file_id}))
            if group_id:
                api_calls.append(("get_group_file_url", {"group_id": str(group_id), "file_id": file_id}))

        for action, params in api_calls:
            try:
                resp = await self.adapter.send_napcat_api(action, params, timeout=8.0)
            except Exception as e:
                logger.debug(f"{action} 调用失败，params={params}: {e!s}")
                continue

            if not isinstance(resp, dict):
                continue
            if resp.get("status") not in ("ok", None):
                logger.debug(f"{action} 返回非成功状态: {resp}")
                continue

            data = resp.get("data")
            payload: dict[str, Any] | None = data if isinstance(data, dict) else None

            if isinstance(data, str) and data.startswith(("http://", "https://")):
                logger.info(f"{action} 成功获取音频 URL")
                return data, None, None, None

            if not payload:
                continue

            base64_data = self._extract_media_base64_from_file_data(payload)
            if base64_data:
                logger.info(f"{action} 成功获取音频 base64 数据")
                return None, None, base64_data, payload

            audio_url, file_path = self._extract_media_source_from_file_data(payload)
            if audio_url or file_path:
                logger.info(f"{action} 成功获取音频源: {'url' if audio_url else 'path'}")
                return audio_url, file_path, None, payload

        return None, None, None, None

    @staticmethod
    def _audio_mime_for_file_name(file_name: Any) -> str:
        suffix = Path(str(file_name or "").split("?", 1)[0].split("#", 1)[0]).suffix.lower()
        return {
            ".mp3": "audio/mpeg",
            ".wav": "audio/wav",
            ".m4a": "audio/mp4",
            ".aac": "audio/aac",
            ".flac": "audio/flac",
            ".ogg": "audio/ogg",
            ".oga": "audio/ogg",
            ".opus": "audio/ogg",
            ".amr": "audio/amr",
            ".silk": "audio/silk",
        }.get(suffix, "audio/mpeg")

    @staticmethod
    async def _read_audio_file_as_base64(file_path: str) -> tuple[str | None, float | None]:
        if not file_path or not Path(file_path).exists():
            return None, None
        audio_data = await asyncio.to_thread(Path(file_path).read_bytes)
        audio_base64 = await get_task_manager().to_process(
            base64_encode_bytes,
            audio_data,
        )
        return audio_base64, len(audio_data) / (1024 * 1024)

    async def _download_audio_url_as_base64(self, url: str) -> tuple[str | None, float | None]:
        if not url.startswith(("http://", "https://")):
            return None, None
        max_size_mb = 30.0
        timeout_seconds = 30.0
        if self.adapter.plugin and self.adapter.plugin.config:
            try:
                max_size_mb = float(self.adapter.plugin.config.features.video_max_size_mb)
                timeout_seconds = float(self.adapter.plugin.config.features.video_download_timeout)
            except Exception:
                max_size_mb = 30.0
                timeout_seconds = 30.0

        try:
            import aiohttp

            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout_seconds)) as response:
                    if response.status != 200:
                        logger.warning(f"音频下载失败: status={response.status}, url={url}")
                        return None, None
                    content_length = response.headers.get("content-length")
                    if content_length:
                        size_mb = int(content_length) / (1024 * 1024)
                        if size_mb > max_size_mb:
                            logger.warning(f"音频文件过大，跳过下载: {size_mb:.2f}MB > {max_size_mb:.2f}MB")
                            return None, None
                    audio_data = await response.read()
        except Exception as e:
            logger.warning(f"音频下载失败: {e!s}")
            return None, None

        size_mb = len(audio_data) / (1024 * 1024)
        if size_mb > max_size_mb:
            logger.warning(f"音频文件过大，跳过处理: {size_mb:.2f}MB > {max_size_mb:.2f}MB")
            return None, None
        audio_base64 = await get_task_manager().to_process(
            base64_encode_bytes,
            audio_data,
        )
        return audio_base64, size_mb

    async def _handle_file_message(self, segment: dict, raw_message: dict | None = None) -> SegPayload | None:
        """处理文件消息"""
        message_data = segment.get("data", {})
        if not message_data:
            logger.warning("文件消息缺少 data 字段")
            return None

        # 提取文件信息
        file_name = message_data.get("file")
        file_size = message_data.get("file_size")
        file_id = message_data.get("file_id")

        logger.info(f"收到文件消息: name={file_name}, size={file_size}, id={file_id}")

        # 兼容 Napcat 将视频作为 file 段上报的场景：
        # 若文件后缀是视频，优先尝试转为 video 段，让后续链路产出视频摘要。
        if self._is_video_file_name(file_name):
            video_processing_enabled = True
            if self.adapter.plugin and self.adapter.plugin.config:
                video_processing_enabled = bool(self.adapter.plugin.config.features.enable_video_processing)
            if not video_processing_enabled:
                logger.info("检测到视频文件扩展名，但视频处理功能已禁用，保持为普通文件")
            else:
                video_url, file_path = self._extract_video_source_from_file_data(message_data)
                video_base64 = self._extract_video_base64_from_file_data(message_data)
                api_payload: dict[str, Any] | None = None

                # 某些平台回调里 file 段没有 URL/路径，补拉一次 get_msg 详情再尝试提取。
                if not video_url and not file_path and not video_base64 and raw_message:
                    message_id = raw_message.get("message_id")
                    if message_id:
                        try:
                            detail = await get_message_detail(message_id=message_id, adapter=self.adapter)
                        except Exception as e:
                            logger.warning(f"视频文件消息补拉详情失败(message_id={message_id}): {e!s}")
                            detail = None

                        if isinstance(detail, dict):
                            for sub_seg in detail.get("message", []):
                                if not isinstance(sub_seg, dict):
                                    continue
                                if sub_seg.get("type") != RealMessageType.file:
                                    continue
                                sub_data = sub_seg.get("data", {})
                                if not isinstance(sub_data, dict):
                                    continue
                                video_url, file_path = self._extract_video_source_from_file_data(sub_data)
                                video_base64 = self._extract_video_base64_from_file_data(sub_data)
                                if video_url or file_path or video_base64:
                                    logger.info("视频文件消息补拉详情成功，已提取可用视频源")
                                    break

                # 仍没有可用来源时，尝试通过 file_id 走 Napcat File API 补全
                if not video_url and not file_path and not video_base64:
                    group_id = raw_message.get("group_id") if raw_message else None
                    video_url, file_path, video_base64, api_payload = await self._try_resolve_video_source_by_file_api(
                        file_id=str(file_id) if file_id else None,
                        file_name=str(file_name) if file_name else None,
                        group_id=group_id,
                    )

                if video_base64:
                    logger.info("检测到视频文件扩展名，已通过补全拿到 base64，按视频链路处理")
                    filename = str(
                        (api_payload or {}).get("file_name")
                        or (api_payload or {}).get("name")
                        or file_name
                        or "video.mp4"
                    )
                    size_field = (api_payload or {}).get("file_size") or (api_payload or {}).get("size")
                    size_mb: float | None = None
                    if size_field:
                        try:
                            size_mb = float(size_field) / (1024 * 1024)
                        except Exception:
                            size_mb = None
                    payload: dict[str, Any] = {
                        "base64": video_base64,
                        "filename": filename,
                    }
                    if size_mb is not None:
                        payload["size_mb"] = size_mb
                    return {"type": "video", "data": payload}

                if video_url or file_path:
                    logger.info("检测到视频文件扩展名，按视频链路处理")
                    return await self._handle_video_message(
                        {
                            "type": RealMessageType.video,
                            "data": {
                                "url": video_url,
                                "filePath": file_path,
                            },
                        }
                    )

                if file_id:
                    logger.info("视频文件 File API 补全失败：未获取到 base64/url/path")
                logger.info("检测到视频文件扩展名，但缺少可用 URL/路径，回退为普通文件消息")

        if self._is_audio_file_name(file_name):
            audio_url, file_path = self._extract_media_source_from_file_data(message_data)
            audio_base64 = self._extract_media_base64_from_file_data(message_data)
            api_payload: dict[str, Any] | None = None

            if not audio_url and not file_path and not audio_base64 and raw_message:
                message_id = raw_message.get("message_id")
                if message_id:
                    try:
                        detail = await get_message_detail(message_id=message_id, adapter=self.adapter)
                    except Exception as e:
                        logger.warning(f"音频文件消息补拉详情失败(message_id={message_id}): {e!s}")
                        detail = None

                    if isinstance(detail, dict):
                        for sub_seg in detail.get("message", []):
                            if not isinstance(sub_seg, dict):
                                continue
                            if sub_seg.get("type") != RealMessageType.file:
                                continue
                            sub_data = sub_seg.get("data", {})
                            if not isinstance(sub_data, dict):
                                continue
                            audio_url, file_path = self._extract_media_source_from_file_data(sub_data)
                            audio_base64 = self._extract_media_base64_from_file_data(sub_data)
                            if audio_url or file_path or audio_base64:
                                logger.info("音频文件消息补拉详情成功，已提取可用音频源")
                                break

            if not audio_url and not file_path and not audio_base64:
                group_id = raw_message.get("group_id") if raw_message else None
                audio_url, file_path, audio_base64, api_payload = await self._try_resolve_audio_source_by_file_api(
                    file_id=str(file_id) if file_id else None,
                    file_name=str(file_name) if file_name else None,
                    group_id=group_id,
                )

            filename = str(
                (api_payload or {}).get("file_name")
                or (api_payload or {}).get("name")
                or file_name
                or "audio.mp3"
            )
            size_field = (api_payload or {}).get("file_size") or (api_payload or {}).get("size") or file_size
            size_mb: float | None = None
            if size_field:
                try:
                    size_mb = float(size_field) / (1024 * 1024)
                except Exception:
                    size_mb = None

            if file_path and not audio_base64:
                try:
                    audio_base64, local_size_mb = await self._read_audio_file_as_base64(file_path)
                    size_mb = local_size_mb if local_size_mb is not None else size_mb
                except Exception as e:
                    logger.warning(f"读取音频文件失败(file_path={file_path}): {e!s}")

            if audio_url and not audio_base64:
                audio_base64, downloaded_size_mb = await self._download_audio_url_as_base64(audio_url)
                size_mb = downloaded_size_mb if downloaded_size_mb is not None else size_mb

            payload: dict[str, Any] = {
                "filename": filename,
                "mime_type": self._audio_mime_for_file_name(filename),
            }
            if size_mb is not None:
                payload["size_mb"] = size_mb

            if audio_base64:
                logger.info("检测到音频文件扩展名，已按语音链路处理")
                payload["base64"] = audio_base64
                return {"type": "voice", "data": payload}

            if file_path:
                logger.info("检测到音频文件扩展名，已保留本地路径供按需原生音频观察")
                payload["path"] = file_path
                return {"type": "voice", "data": payload}

            if file_id:
                logger.info("音频文件 File API 补全失败：未获取到 base64/url/path")
            logger.info("检测到音频文件扩展名，但缺少可用 URL/路径，回退为普通文件消息")

        # 将文件信息打包成字典
        file_data = {
            "name": file_name,
            "size": file_size,
            "id": file_id,
        }

        return {"type": "file", "data": file_data}

    async def _handle_json_message(self, segment: dict) -> SegPayload | None:
        """
        处理JSON消息
        Parameters:
            segment: dict: 消息段
        Returns:
            SegPayload | None: 处理后的消息段
        """
        message_data = segment.get("data", {})
        json_data = message_data.get("data", "")

        # 检查JSON消息格式
        if not message_data or "data" not in message_data:
            logger.warning("JSON消息格式不正确")
            return {"type": "json", "data": str(message_data)}

        try:
            # 尝试将json_data解析为Python对象
            nested_data = orjson.loads(json_data)

            # 检查是否是机器人自己上传文件的回声
            if self._is_file_upload_echo(nested_data):
                logger.info("检测到机器人发送文件的回声消息，将作为文件消息处理")
                # 从回声消息中提取文件信息
                file_info = self._extract_file_info_from_echo(nested_data)
                if file_info:
                    return {"type": "file", "data": file_info}

            # 检查是否是QQ小程序分享消息
            if "app" in nested_data and "com.tencent.miniapp" in str(nested_data.get("app", "")):
                logger.debug("检测到QQ小程序分享消息，开始提取信息")

                # 提取目标字段
                extracted_info = {}

                # 提取 meta.detail_1 中的信息
                meta = nested_data.get("meta", {})
                detail_1 = meta.get("detail_1", {})

                if detail_1:
                    extracted_info["title"] = detail_1.get("title", "")
                    extracted_info["desc"] = detail_1.get("desc", "")
                    qqdocurl = detail_1.get("qqdocurl", "")

                    # 从qqdocurl中提取b23.tv短链接
                    if qqdocurl and "b23.tv" in qqdocurl:
                        # 查找b23.tv链接的起始位置
                        start_pos = qqdocurl.find("https://b23.tv/")
                        if start_pos != -1:
                            # 提取从https://b23.tv/开始的部分
                            b23_part = qqdocurl[start_pos:]
                            # 查找第一个?的位置，截取到?之前
                            question_pos = b23_part.find("?")
                            if question_pos != -1:
                                extracted_info["short_url"] = b23_part[:question_pos]
                            else:
                                extracted_info["short_url"] = b23_part
                        else:
                            extracted_info["short_url"] = qqdocurl
                    else:
                        extracted_info["short_url"] = qqdocurl

                # 如果成功提取到关键信息，返回格式化的文本
                if extracted_info.get("title") or extracted_info.get("desc") or extracted_info.get("short_url"):
                    content_parts = []

                    if extracted_info.get("title"):
                        content_parts.append(f"来源: {extracted_info['title']}")

                    if extracted_info.get("desc"):
                        content_parts.append(f"标题: {extracted_info['desc']}")

                    if extracted_info.get("short_url"):
                        content_parts.append(f"链接: {extracted_info['short_url']}")

                    formatted_content = "\n".join(content_parts)
                    return{
                        "type": "text",
                        "data": f"这是一条小程序分享消息，可以根据来源，考虑使用对应解析工具\n{formatted_content}",
                    }



            # 检查是否是音乐分享 (QQ音乐类型)
            if nested_data.get("view") == "music" and "com.tencent.music" in str(nested_data.get("app", "")):
                meta = nested_data.get("meta", {})
                music = meta.get("music", {})
                if music:
                    tag = music.get("tag", "未知来源")
                    logger.debug(f"检测到【{tag}】音乐分享消息 (music view)，开始提取信息")

                    title = music.get("title", "未知歌曲")
                    desc = music.get("desc", "未知艺术家")
                    jump_url = music.get("jumpUrl", "")
                    preview_url = music.get("preview", "")

                    artist = "未知艺术家"
                    song_title = title

                    if "网易云音乐" in tag:
                        artist = desc
                    elif "QQ音乐" in tag:
                        if " - " in title:
                            parts = title.split(" - ", 1)
                            song_title = parts[0]
                            artist = parts[1]
                        else:
                            artist = desc

                    formatted_content = (
                        f"这是一张来自【{tag}】的音乐分享卡片：\n"
                        f"歌曲: {song_title}\n"
                        f"艺术家: {artist}\n"
                        f"跳转链接: {jump_url}\n"
                        f"封面图: {preview_url}"
                    )
                    return {"type": "text", "data": formatted_content}

            # 检查是否是新闻/图文分享 (网易云音乐可能伪装成这种)
            elif nested_data.get("view") == "news" and "com.tencent.tuwen" in str(nested_data.get("app", "")):
                meta = nested_data.get("meta", {})
                news = meta.get("news", {})
                if news and "网易云音乐" in news.get("tag", ""):
                    tag = news.get("tag")
                    logger.debug(f"检测到【{tag}】音乐分享消息 (news view)，开始提取信息")

                    title = news.get("title", "未知歌曲")
                    desc = news.get("desc", "未知艺术家")
                    jump_url = news.get("jumpUrl", "")
                    preview_url = news.get("preview", "")

                    formatted_content = (
                        f"这是一张来自【{tag}】的音乐分享卡片：\n"
                        f"标题: {title}\n"
                        f"描述: {desc}\n"
                        f"跳转链接: {jump_url}\n"
                        f"封面图: {preview_url}"
                    )
                    return {"type": "text", "data": formatted_content}

            # 如果没有提取到关键信息，返回None
            return None

        except orjson.JSONDecodeError:
            # 如果解析失败，我们假设它不是我们关心的任何一种结构化JSON，
            # 而是普通的文本或者无法解析的格式。
            logger.debug(f"无法将data字段解析为JSON: {json_data}")
            return None
        except Exception as e:
            logger.error(f"处理JSON消息时发生未知错误: {e}")
            return None

    def _is_file_upload_echo(self, nested_data: Any) -> bool:
        """检查一个JSON对象是否是机器人自己上传文件的回声消息"""
        if not isinstance(nested_data, dict):
            return False

        # 检查 'app' 和 'meta' 字段是否存在
        if "app" not in nested_data or "meta" not in nested_data:
            return False

        # 检查 'app' 字段是否包含 'com.tencent.miniapp'
        if "com.tencent.miniapp" not in str(nested_data.get("app", "")):
            return False

        # 检查 'meta' 内部的 'detail_1' 的 'busi_id' 是否为 '1014'
        meta = nested_data.get("meta", {})
        detail_1 = meta.get("detail_1", {})
        if detail_1.get("busi_id") == "1014":
            return True

        return False

    def _extract_file_info_from_echo(self, nested_data: dict) -> dict | None:
        """从文件上传的回声消息中提取文件信息"""
        try:
            meta = nested_data.get("meta", {})
            detail_1 = meta.get("detail_1", {})

            # 文件名在 'desc' 字段
            file_name = detail_1.get("desc")

            # 文件大小在 'summary' 字段，格式为 "大小：1.7MB"
            summary = detail_1.get("summary", "")
            file_size_str = summary.replace("大小：", "").strip() # 移除前缀和空格

            # QQ API有时返回的大小不标准，这里我们只提取它给的字符串
            # 实际大小已经由Napcat在发送时记录，这里主要是为了保持格式一致

            if file_name and file_size_str:
                return {"file": file_name, "file_size": file_size_str, "file_id": None} # file_id在回声中不可用
        except Exception as e:
            logger.error(f"从文件回声中提取信息失败: {e}")

        return None
