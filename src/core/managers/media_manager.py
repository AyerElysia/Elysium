"""媒体管理器。

负责图片和表情包的识别、存储和管理。

功能：
- 使用 VLM 识别图片和表情包内容
- 缓存识别结果到数据库，避免重复识别
- 管理媒体文件的存储和检索
- 支持按哈希值去重，节省存储和计算资源

设计原则：
- 优先从缓存读取，减少 VLM 调用
- 使用哈希值标识图片，避免重复处理
- 异步处理，不阻塞主流程
- 异常友好，识别失败不影响消息流转
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import shutil
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from tempfile import TemporaryDirectory
from pathlib import Path
from threading import Lock as ThreadLock
from typing import Any
from sqlalchemy import or_, select

from src.kernel.logger import get_logger
from src.app.plugin_system.api.llm_api import get_model_set_by_task
from src.kernel.llm.model_client.registry import ModelClientRegistry
from src.core.prompt import PromptTemplate, get_prompt_manager
from src.core.config import get_core_config
from src.core.utils.base64_helper import base64_decode_to_bytes
from src.kernel.concurrency import get_task_manager
from src.kernel.scheduler import get_unified_scheduler, TriggerType
from src.kernel.db.core.session import get_db_session
from src.core.models.sql_alchemy import Images, ImageDescriptions
from src.kernel.llm import LLMContextManager, LLMPayload, ROLE, Text, Image, Audio

logger = get_logger("media_manager")

# 单例实例
_media_manager: "MediaManager | None" = None
_MAX_MEDIA_DATA_BYTES = 8 * 1024 * 1024
_MAX_RECOGNITION_LOCKS = 1024
_MAX_VIDEO_DATA_BYTES = 200 * 1024 * 1024
_FAILURE_ALERT_WINDOW_SECONDS = 300.0
_FAILURE_ALERT_THRESHOLD = 5


@dataclass
class MediaChainStats:
    """媒体链路统计。"""

    received: int = 0
    rejected_too_large: int = 0
    cache_hits: int = 0
    cache_misses: int = 0
    dedup_hits: int = 0
    success: int = 0
    failure: int = 0
    bytes_received: int = 0
    bytes_rejected: int = 0
    recent_failures: dict[str, deque[float]] = field(default_factory=dict)
    failure_types: dict[str, int] = field(default_factory=dict)

    @property
    def cache_hit_rate(self) -> float:
        total = self.cache_hits + self.cache_misses
        return self.cache_hits / total if total else 0.0

    @property
    def failure_rate(self) -> float:
        total = self.success + self.failure
        return self.failure / total if total else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "received": self.received,
            "rejected_too_large": self.rejected_too_large,
            "cache_hits": self.cache_hits,
            "cache_misses": self.cache_misses,
            "cache_hit_rate": self.cache_hit_rate,
            "dedup_hits": self.dedup_hits,
            "success": self.success,
            "failure": self.failure,
            "failure_rate": self.failure_rate,
            "bytes_received": self.bytes_received,
            "bytes_rejected": self.bytes_rejected,
            "failure_types": dict(self.failure_types),
        }


class MediaManager:
    """媒体管理器。
    
    管理图片、表情包等媒体资源的识别、存储和检索。
    
    主要功能：
    1. VLM 识别：调用 VLM 模型识别图片/表情包内容
    2. 缓存管理：使用哈希值缓存识别结果
    3. 数据库存储：持久化媒体信息
    4. 去重优化：相同内容的图片只识别一次
    
    Examples:
        >>> manager = get_media_manager()
        >>> description = await manager.recognize_image(base64_data, "image")
        >>> await manager.save_media_info(...)
    """

    def __init__(self):
        """初始化媒体管理器。"""
        self._vlm_model_set = None
        self._voice_model_set = None
        self._video_model_set = None
        self._audio_understanding_model_set = None
        self._vlm_available = False
        self._voice_available = False
        self._skip_vlm_stream_ids: set[str] = set()  # 已注册跳过所有 VLM 识别的聊天流 ID
        self._skip_vlm_media_types_by_stream: dict[str, set[str]] = {}
        self._media_chain_stats = MediaChainStats()
        self._media_stats_lock = ThreadLock()
        self._recognition_locks: OrderedDict[str, asyncio.Lock] = OrderedDict()
        self._initialize_vlm()
        self._initialize_asr()
        self._register_prompts()
        self._setup_media_folders()
        self._cleanup_task_id = None
        self._start_cleanup_scheduler()

    def _initialize_vlm(self) -> None:
        """初始化 VLM/视频/ASR 模型配置。"""
        try:
            self._vlm_model_set = get_model_set_by_task("vlm")
            self._vlm_available = self._vlm_model_set is not None
            self._voice_model_set = get_model_set_by_task("voice")
            self._voice_available = self._voice_model_set is not None
            self._video_model_set = get_model_set_by_task("video")
            self._audio_understanding_model_set = None
            for task_name in ("audio_observer", "media_observer"):
                try:
                    self._audio_understanding_model_set = get_model_set_by_task(task_name)
                    if self._audio_understanding_model_set:
                        break
                except ValueError:
                    continue

            
            if self._vlm_available:
                logger.info("VLM 模型已加载，媒体识别功能可用")
            else:
                logger.info("未配置 VLM 模型，媒体识别功能不可用")

            if self._voice_available:
                logger.info("ASR 模型已加载，语音转写功能可用")
            else:
                logger.info("未配置 voice 任务模型，语音转写功能不可用")

            if self._video_model_set:
                logger.info("视频摘要模型已加载（非原生视频，将走抽帧摘要链路）")
            else:
                logger.info("未配置 video 任务模型，视频摘要将回退到关键帧描述拼接")

            if self._audio_understanding_model_set:
                logger.info("音频理解模型已加载，语音/音频摘要将优先走原生 Audio 链路")
            else:
                logger.info("未配置 audio_observer/media_observer 任务模型，语音摘要将回退到 ASR")
        except Exception as e:
            logger.error(f"初始化 VLM 模型失败: {e}")

    def _initialize_asr(self) -> None:
        """初始化 ASR 模型配置。"""
        try:
            self._asr_model_set = get_model_set_by_task("voice")
            self._asr_available = self._asr_model_set is not None

            if self._asr_available:
                logger.info("ASR 模型已加载，语音识别功能可用")
            else:
                logger.info("未配置 ASR 模型，语音识别功能不可用")
        except Exception as e:
            self._asr_model_set = None
            self._asr_available = False
            logger.error(f"初始化 ASR 模型失败: {e}")

    def _register_prompts(self) -> None:
        """注册媒体识别相关的提示词模板。"""
        try:
            manager = get_prompt_manager()
            
            # 注册图片识别提示词
            custom_prompt = get_core_config().chat.image_recognition_prompt
            default_template = "描述这张图片的内容，包含主题、主要元素。若有文字或代码，完整转述。"
            image_prompt = PromptTemplate(
                name="media.image_recognition",
                template=custom_prompt if custom_prompt else default_template
            )
            manager.register_template(image_prompt)
            
            # 注册表情包识别提示词
            emoji_prompt = PromptTemplate(
                name="media.emoji_recognition",
                template="请简要描述这个表情包的内容和含义，用一句话概括。"
            )
            manager.register_template(emoji_prompt)
            
            logger.debug("媒体识别提示词模板已注册")
        except Exception as e:
            logger.warning(f"注册提示词模板失败: {e}")

    def _setup_media_folders(self) -> None:
        """设置媒体文件夹结构。"""
        try:
            # 媒体根目录
            self.media_root = Path("data/media_cache")
            
            # 子文件夹
            self.pending_folder = self.media_root / "pending"  # 待识别
            self.images_folder = self.media_root / "images"    # 识别完成的图片
            self.emojis_folder = self.media_root / "emojis"    # 识别完成的表情包
            
            # 创建所有必要的文件夹
            for folder in [self.pending_folder, self.images_folder, self.emojis_folder]:
                folder.mkdir(parents=True, exist_ok=True)
            
            logger.info(f"媒体文件夹已初始化: {self.media_root}")
        except Exception as e:
            logger.error(f"创建媒体文件夹失败: {e}")

    def _start_cleanup_scheduler(self) -> None:
        """启动定时清理任务（每5分钟清理一次缓存）。"""
        try:
            # 延迟导入，避免循环依赖
            # 确保在异步上下文中创建任务
            asyncio.create_task(self._register_cleanup_task())
            
            logger.info("媒体缓存清理调度器已启动(每5分钟)")
        except Exception as e:
            logger.error(f"启动清理调度器失败: {e}")

    async def _register_cleanup_task(self) -> None:
        """注册定时清理任务到调度器。"""
        try:
            scheduler = get_unified_scheduler()
            
            # 创建周期性清理任务（每5分钟 = 300秒）
            schedule_id = await scheduler.create_schedule(
                callback=self._cleanup_pending_folder,
                trigger_type=TriggerType.TIME,
                trigger_config={"delay_seconds": 300},  # 5分钟
                is_recurring=True,
                task_name="media_cache_cleanup"
            )
            
            self._cleanup_task_id = schedule_id
            logger.info(f"媒体缓存清理任务已注册: {schedule_id}")
        except Exception as e:
            logger.error(f"注册清理任务失败: {e}")

    async def _cleanup_pending_folder(self) -> None:
        """清理待识别文件夹中的陈旧文件。"""
        try:
            if not self.pending_folder.exists():
                return
            
            current_time = time.time()
            cleanup_count = 0
            
            # 遍历所有待识别文件
            for file_path in self.pending_folder.iterdir():
                if not file_path.is_file():
                    continue
                
                # 获取文件修改时间
                file_mtime = file_path.stat().st_mtime
                
                # 如果文件超过5分钟未处理，删除它
                if current_time - file_mtime >= 300:  # 5分钟 = 300秒
                    try:
                        file_path.unlink()
                        cleanup_count += 1
                    except Exception as e:
                        logger.warning(f"删除文件失败 {file_path.name}: {e}")
            
            if cleanup_count > 0:
                logger.info(f"媒体缓存清理完成，删除了 {cleanup_count} 个陈旧文件")
        except Exception as e:
            logger.error(f"清理待识别文件夹失败: {e}")

    # ──────────────────────────────────────────
    # 公共 API：VLM 识别控制
    # ──────────────────────────────────────────

    def skip_vlm_for_stream(
        self,
        stream_id: str,
        media_types: list[str] | tuple[str, ...] | set[str] | None = None,
    ) -> None:
        """注册指定聊天流跳过 VLM 识别。

        调用后，该 stream_id 的消息在 MessageConverter 中将不再触发
        指定媒体类型的 VLM 识别，媒体数据仅保留原始 base64。
        适用于聊天流程自行处理多模态内容的场景。

        Args:
            stream_id: 要跳过 VLM 识别的聊天流 ID
            media_types: 要跳过的媒体类型。为 None 时跳过该流全部 VLM 识别。
        """
        if media_types is None:
            self._skip_vlm_stream_ids.add(stream_id)
            self._skip_vlm_media_types_by_stream.pop(stream_id, None)
            logger.debug(f"已注册跳过全部 VLM 识别: stream_id={stream_id[:8]}")
            return

        normalized = {str(item).strip().lower() for item in media_types if str(item).strip()}
        if not normalized:
            return
        existing = self._skip_vlm_media_types_by_stream.setdefault(stream_id, set())
        existing.update(normalized)
        logger.debug(
            f"已注册跳过 VLM 识别: stream_id={stream_id[:8]}, media_types={sorted(existing)}"
        )

    def unskip_vlm_for_stream(
        self,
        stream_id: str,
        media_types: list[str] | tuple[str, ...] | set[str] | None = None,
    ) -> None:
        """取消指定聊天流的 VLM 识别跳过。

        Args:
            stream_id: 要恢复 VLM 识别的聊天流 ID
            media_types: 要恢复的媒体类型。为 None 时清除该流全部跳过配置。
        """
        if media_types is None:
            self._skip_vlm_stream_ids.discard(stream_id)
            self._skip_vlm_media_types_by_stream.pop(stream_id, None)
            logger.debug(f"已取消跳过全部 VLM 识别: stream_id={stream_id[:8]}")
            return

        configured = self._skip_vlm_media_types_by_stream.get(stream_id)
        if configured is None:
            return
        for item in media_types:
            configured.discard(str(item).strip().lower())
        if not configured:
            self._skip_vlm_media_types_by_stream.pop(stream_id, None)
        logger.debug(
            f"已取消部分 VLM 跳过: stream_id={stream_id[:8]}, media_types={list(media_types)}"
        )

    def should_skip_vlm(self, stream_id: str, media_type: str | None = None) -> bool:
        """查询指定聊天流是否应跳过 VLM 识别。

        Args:
            stream_id: 聊天流 ID
            media_type: 媒体类型。为 None 时只要该流存在任意跳过配置就返回 True。

        Returns:
            True 表示该聊天流已注册跳过指定 VLM 识别
        """
        if stream_id in self._skip_vlm_stream_ids:
            return True
        configured = self._skip_vlm_media_types_by_stream.get(stream_id)
        if not configured:
            return False
        if media_type is None:
            return True
        return str(media_type).strip().lower() in configured

    # ──────────────────────────────────────────
    # 公共 API：媒体识别
    # ──────────────────────────────────────────

    async def get_media_chain_stats(self) -> dict[str, Any]:
        """获取媒体链路统计。"""
        with self._media_stats_lock:
            return self._media_chain_stats.to_dict()

    async def reset_media_chain_stats(self) -> None:
        """重置媒体链路统计。"""
        with self._media_stats_lock:
            self._media_chain_stats = MediaChainStats()

    async def _record_media_event(
        self,
        *,
        event: str,
        media_type: str,
        media_bytes: int = 0,
        failure_type: str | None = None,
    ) -> None:
        with self._media_stats_lock:
            stats = self._media_chain_stats
            if event == "received":
                stats.received += 1
                stats.bytes_received += max(0, media_bytes)
                return
            if event == "rejected_too_large":
                stats.rejected_too_large += 1
                stats.bytes_rejected += max(0, media_bytes)
                return
            if event == "cache_hit":
                stats.cache_hits += 1
                return
            if event == "cache_miss":
                stats.cache_misses += 1
                return
            if event == "dedup_hit":
                stats.dedup_hits += 1
                return
            if event == "success":
                stats.success += 1
                return
            if event == "failure":
                stats.failure += 1
                if failure_type:
                    stats.failure_types[failure_type] = stats.failure_types.get(failure_type, 0) + 1
                    failure_bucket = stats.recent_failures.setdefault(media_type, deque())
                    now = time.time()
                    failure_bucket.append(now)
                    while failure_bucket and now - failure_bucket[0] > _FAILURE_ALERT_WINDOW_SECONDS:
                        failure_bucket.popleft()
                    if len(failure_bucket) >= _FAILURE_ALERT_THRESHOLD:
                        logger.warning(
                            f"媒体链路失败告警: media_type={media_type}, "
                            f"recent_failures={len(failure_bucket)}, "
                            f"failure_type={failure_type}"
                        )
                return

    def _estimate_media_size_bytes(self, base64_data: str) -> int:
        """估算 base64 数据对应的原始字节大小。"""
        clean = self._extract_clean_base64(base64_data)
        try:
            return len(base64.b64decode(clean, validate=False))
        except Exception:
            return len(clean.encode("utf-8"))

    def _get_recognition_lock(self, media_hash: str) -> asyncio.Lock:
        """获取指定媒体哈希的去重锁（LRU 淘汰，最多保留 _MAX_RECOGNITION_LOCKS 条）。"""
        lock = self._recognition_locks.get(media_hash)
        if lock is not None:
            self._recognition_locks.move_to_end(media_hash)
            return lock
        lock = asyncio.Lock()
        self._recognition_locks[media_hash] = lock
        while len(self._recognition_locks) > _MAX_RECOGNITION_LOCKS:
            self._recognition_locks.popitem(last=False)
        return lock

    def _extract_voice_payload(self, voice_data: str | dict[str, Any]) -> tuple[str, dict[str, Any]]:
        """提取语音 base64 数据和元信息。"""
        if isinstance(voice_data, dict):
            for key in ("base64", "data", "voice_base64", "audio_base64"):
                candidate = voice_data.get(key)
                if isinstance(candidate, str) and candidate.strip():
                    return candidate, voice_data
            return "", voice_data

        if isinstance(voice_data, str):
            return voice_data, {}

        return "", {}

    async def _resolve_voice_payload(self, voice_data: str | dict[str, Any]) -> tuple[str, dict[str, Any]]:
        """解析语音输入为 base64 数据。

        支持适配器直接给 base64，也支持音频文件消息携带的本地路径或下载 URL。
        """
        base64_data, metadata = self._extract_voice_payload(voice_data)
        if base64_data:
            return base64_data, metadata

        if not isinstance(voice_data, dict):
            return "", metadata

        for key in ("path", "file", "file_path", "local_path", "localPath", "temp_path"):
            value = voice_data.get(key)
            if not isinstance(value, str) or not value.strip():
                continue
            path_value = value.strip()
            if path_value.startswith("file://"):
                path_value = path_value[7:]
            try:
                path = Path(path_value)
                if path.exists() and path.is_file():
                    raw = await asyncio.to_thread(path.read_bytes)
                    return base64.b64encode(raw).decode("utf-8"), voice_data
            except OSError:
                continue

        for key in ("url", "file_url", "download_url", "downloadUrl", "src"):
            value = voice_data.get(key)
            if not isinstance(value, str) or not value.startswith(("http://", "https://")):
                continue
            downloaded = await self._download_media_url_as_base64(value, max_bytes=_MAX_MEDIA_DATA_BYTES)
            if downloaded:
                return downloaded, voice_data

        return "", voice_data

    async def _download_media_url_as_base64(self, url: str, *, max_bytes: int) -> str | None:
        """下载小型媒体 URL 并编码为 base64。"""
        try:
            import aiohttp

            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=30.0)) as response:
                    if response.status != 200:
                        logger.warning(f"媒体下载失败: status={response.status}, url={url}")
                        return None
                    content_length = response.headers.get("content-length")
                    if content_length and int(content_length) > max_bytes:
                        logger.warning(f"媒体过大，跳过下载: bytes={content_length}, url={url}")
                        return None
                    raw = await response.read()
        except Exception as e:
            logger.warning(f"媒体下载失败: {e!s}")
            return None

        if len(raw) > max_bytes:
            logger.warning(f"媒体过大，跳过处理: bytes={len(raw)}, url={url}")
            return None
        return base64.b64encode(raw).decode("utf-8")

    async def recognize_media(
        self, 
        base64_data: str, 
        media_type: str,
        use_cache: bool = True
    ) -> str | None:
        """识别媒体内容（图片或表情包）。
        
        Args:
            base64_data: base64 编码的媒体数据
            media_type: 媒体类型，"image" 或 "emoji"
            use_cache: 是否使用缓存（默认 True）
            
        Returns:
            媒体的文字描述，识别失败返回 None
        """
        try:
            if media_type == "voice":
                return await self.recognize_voice(base64_data, use_cache=use_cache)

            # 计算哈希值
            media_hash = self._compute_hash(base64_data)
            media_bytes = self._estimate_media_size_bytes(base64_data)
            await self._record_media_event(
                event="received",
                media_type=media_type,
                media_bytes=media_bytes,
            )

            if media_bytes > _MAX_MEDIA_DATA_BYTES:
                await self._record_media_event(
                    event="rejected_too_large",
                    media_type=media_type,
                    media_bytes=media_bytes,
                )
                logger.warning(
                    f"媒体过大，跳过识别: type={media_type}, "
                    f"bytes={media_bytes}, hash={media_hash[:8]}..."
                )
                return None

            lock = self._get_recognition_lock(media_hash)
            async with lock:
                # 尝试从缓存读取
                if use_cache:
                    cached_description = await self._get_cached_description(
                        media_hash,
                        media_type
                    )
                    if cached_description:
                        await self._record_media_event(
                            event="cache_hit",
                            media_type=media_type,
                        )
                        await self._record_media_event(
                            event="dedup_hit",
                            media_type=media_type,
                        )
                        await self._record_media_event(
                            event="success",
                            media_type=media_type,
                        )
                        logger.debug(f"从缓存获取{media_type}描述: {media_hash[:8]}...")
                        return cached_description
                await self._record_media_event(
                    event="cache_miss",
                    media_type=media_type,
                )

                # 保存到待识别文件夹
                pending_file_path = await self._save_to_pending(
                    base64_data,
                    media_hash,
                    media_type
                )

                # VLM 识别
                description = await self._recognize_with_vlm(base64_data, media_type)

                if description:
                    await self._record_media_event(
                        event="success",
                        media_type=media_type,
                    )
                    # 保存到缓存
                    await self._save_description_cache(
                        media_hash,
                        media_type,
                        description
                    )
                    logger.info(f"成功识别{media_type}: {description[:50]}...")

                    # 移动到对应的分类文件夹
                    await self._move_to_category_folder(
                        pending_file_path,
                        media_type,
                        media_hash
                    )

                    # 保存媒体信息到数据库
                    target_folder = self.images_folder if media_type == "image" else self.emojis_folder
                    target_file_path = target_folder / pending_file_path.name
                    await self.save_media_info(
                        media_hash=media_hash,
                        media_type=media_type,
                        file_path=str(target_file_path),
                        description=description,
                        vlm_processed=True
                    )
                else:
                    await self._record_media_event(
                        event="failure",
                        media_type=media_type,
                        failure_type="recognize_failed",
                    )
                    # 识别失败，保持在待识别文件夹，等待定时清理
                    logger.warning(f"识别失败，文件保留在待识别文件夹: {pending_file_path.name}")

                return description
            
        except Exception as e:
            logger.error(f"识别{media_type}失败: {e}", exc_info=True)
            await self._record_media_event(
                event="failure",
                media_type=media_type,
                failure_type=type(e).__name__,
            )
            return None

    async def recognize_voice(
        self,
        voice_data: str | dict[str, Any],
        use_cache: bool = True,
    ) -> str | None:
        """理解语音/音频内容并返回摘要文本。

        优先使用原生 Audio 多模态模型理解音频；失败时才回退到 ASR 转写。
        """
        try:
            base64_data, metadata = await self._resolve_voice_payload(voice_data)
            if not base64_data:
                return None

            voice_hash = self._compute_hash(base64_data)
            voice_bytes = self._estimate_media_size_bytes(base64_data)
            await self._record_media_event(
                event="received",
                media_type="voice",
                media_bytes=voice_bytes,
            )

            if voice_bytes > _MAX_MEDIA_DATA_BYTES:
                await self._record_media_event(
                    event="rejected_too_large",
                    media_type="voice",
                    media_bytes=voice_bytes,
                )
                logger.warning(
                    f"语音过大，跳过理解: bytes={voice_bytes}, hash={voice_hash[:8]}..."
                )
                return None

            lock = self._get_recognition_lock(voice_hash)
            async with lock:
                if use_cache:
                    cached_description = await self._get_cached_description(
                        voice_hash,
                        "voice_understanding",
                    )
                    if cached_description:
                        await self._record_media_event(
                            event="cache_hit",
                            media_type="voice",
                        )
                        await self._record_media_event(
                            event="dedup_hit",
                            media_type="voice",
                        )
                        await self._record_media_event(
                            event="success",
                            media_type="voice",
                        )
                        logger.debug(f"从缓存获取 voice 理解摘要: {voice_hash[:8]}...")
                        return cached_description
                await self._record_media_event(
                    event="cache_miss",
                    media_type="voice",
                )

                summary = await self._recognize_with_audio_understanding(base64_data, metadata)
                if not summary:
                    summary = await self._recognize_with_asr(base64_data)
                    if summary:
                        summary = f"语音转写：{summary}"

                if not summary:
                    await self._record_media_event(
                        event="failure",
                        media_type="voice",
                        failure_type="audio_understanding_failed",
                    )
                    return None

                await self._record_media_event(
                    event="success",
                    media_type="voice",
                )
                await self._save_description_cache(
                    voice_hash,
                    "voice_understanding",
                    summary,
                )
                await self.save_media_info(
                    media_hash=voice_hash,
                    media_type="voice",
                    file_path=str(metadata.get("filename") or f"voice:{voice_hash[:16]}"),
                    description=summary,
                    vlm_processed=True,
                )
                return summary
        except Exception as e:
            logger.error(f"语音理解失败: {e}", exc_info=True)
            await self._record_media_event(
                event="failure",
                media_type="voice",
                failure_type=type(e).__name__,
            )
            return None


    async def recognize_batch(
        self,
        media_list: list[tuple[str, str]],
        use_cache: bool = True
    ) -> list[tuple[int, str | None]]:
        """批量识别多个媒体。
        
        Args:
            media_list: [(base64_data, media_type), ...] 列表
            use_cache: 是否使用缓存
            
        Returns:
            [(index, description), ...] 列表，description 为 None 表示识别失败
        """
        results = []
        for idx, (base64_data, media_type) in enumerate(media_list):
            description = await self.recognize_media(
                base64_data,
                media_type,
                use_cache=use_cache
            )
            results.append((idx, description))
        return results

    async def recognize_video(
        self,
        video_data: str | dict[str, Any],
        use_cache: bool = True,
        max_frames: int = 3,
    ) -> str | None:
        """识别视频内容（非原生视频：抽关键帧 -> 图片识别 -> 文本总结）。

        Args:
            video_data: 视频数据（base64 字符串，或包含 base64 的字典）
            use_cache: 是否使用缓存
            max_frames: 最多抽取关键帧数量

        Returns:
            视频摘要文本，失败返回 None
        """
        try:
            base64_data, metadata = self._extract_video_payload(video_data)
            if not base64_data:
                return None

            video_hash = self._compute_hash(base64_data)
            video_bytes = self._estimate_media_size_bytes(base64_data)
            await self._record_media_event(
                event="received",
                media_type="video",
                media_bytes=video_bytes,
            )

            if video_bytes > _MAX_VIDEO_DATA_BYTES:
                await self._record_media_event(
                    event="rejected_too_large",
                    media_type="video",
                    media_bytes=video_bytes,
                )
                logger.warning(
                    f"视频过大，跳过摘要: bytes={video_bytes}, "
                    f"limit={_MAX_VIDEO_DATA_BYTES}, hash={video_hash[:8]}..."
                )
                return None

            lock = self._get_recognition_lock(video_hash)
            async with lock:
                if use_cache:
                    cached = await self._get_cached_description(video_hash, "video")
                    if cached:
                        await self._record_media_event(event="cache_hit", media_type="video")
                        await self._record_media_event(event="dedup_hit", media_type="video")
                        await self._record_media_event(event="success", media_type="video")
                        logger.debug(f"从缓存获取 video 描述: {video_hash[:8]}...")
                        return cached
                await self._record_media_event(event="cache_miss", media_type="video")

                frame_images = await self._extract_video_keyframes(
                    base64_data=base64_data,
                    filename=str(metadata.get("filename", "video.mp4") or "video.mp4"),
                    max_frames=max_frames,
                )
                if not frame_images:
                    await self._record_media_event(
                        event="failure",
                        media_type="video",
                        failure_type="extract_frames_failed",
                    )
                    return None

                frame_descriptions: list[str] = []
                for idx, frame_base64 in enumerate(frame_images, start=1):
                    try:
                        description = await self.recognize_media(
                            frame_base64,
                            "image",
                            use_cache=True,
                        )
                        if description:
                            frame_descriptions.append(f"关键帧{idx}: {description}")
                    except Exception as e:
                        logger.debug(f"视频关键帧识别失败(frame={idx}): {e}")

                if not frame_descriptions:
                    await self._record_media_event(
                        event="failure",
                        media_type="video",
                        failure_type="frame_descriptions_empty",
                    )
                    return None

                summary = await self._summarize_video_frames(frame_descriptions, metadata)
                if not summary:
                    summary = "；".join(frame_descriptions[:max(1, min(3, len(frame_descriptions)))])

                await self._save_description_cache(video_hash, "video", summary)
                await self.save_media_info(
                    media_hash=video_hash,
                    media_type="video",
                    file_path=str(metadata.get("filename") or f"video:{video_hash[:16]}"),
                    description=summary,
                    vlm_processed=True,
                )
                await self._record_media_event(event="success", media_type="video")
                return summary
        except Exception as e:
            logger.error(f"识别 video 失败: {e}", exc_info=True)
            await self._record_media_event(
                event="failure",
                media_type="video",
                failure_type=type(e).__name__,
            )
            return None

    # ──────────────────────────────────────────
    # 公共 API：数据库操作
    # ──────────────────────────────────────────

    async def save_media_info(
        self,
        media_hash: str,
        media_type: str,
        file_path: str | None = None,
        description: str | None = None,
        vlm_processed: bool = False
    ) -> None:
        """保存媒体信息到数据库。
        
        Args:
            media_hash: 媒体哈希值（作为唯一标识）
            media_type: 媒体类型（image/emoji）
            file_path: 文件路径（可选）
            description: 描述文本（可选）
            vlm_processed: 是否已经过 VLM 处理
        """
        resolved_path = self._normalize_media_db_path(media_hash, media_type, file_path)
        try:
            async with get_db_session() as session:
                # 查找现有记录（image_id 或 path 任一命中都视为同一条媒体记录）
                # path 在 images 表上有唯一约束，重复文件名（如 video.mp4）不能裸 insert。
                # 这里使用 scalars().first() 来避免数据库中存在多条重复记录导致的 MultipleResultsFound 错误
                stmt = (
                    select(Images)
                    .where(or_(Images.image_id == media_hash, Images.path == resolved_path))
                    .order_by(Images.timestamp.desc())
                    .limit(1)
                )
                result = await session.execute(stmt)
                existing = result.scalars().first()

                if existing:
                    # 更新现有记录
                    existing.count += 1
                    existing.image_id = media_hash
                    existing.path = resolved_path
                    existing.type = media_type
                    existing.timestamp = time.time()
                    if description:
                        existing.description = description
                    if vlm_processed:
                        existing.vlm_processed = True
                    logger.debug(f"更新媒体记录: {media_hash[:8]}... count={existing.count}")
                else:
                    # 创建新记录
                    new_image = Images(
                        image_id=media_hash,
                        path=resolved_path,
                        type=media_type,
                        description=description,
                        timestamp=time.time(),
                        vlm_processed=vlm_processed,
                        count=1
                    )
                    session.add(new_image)
                    logger.debug(f"创建新媒体记录: {media_hash[:8]}...")

                await session.commit()

        except Exception as e:
            logger.error(f"保存媒体信息失败: {e}", exc_info=True)

    @staticmethod
    def _normalize_media_db_path(
        media_hash: str,
        media_type: str,
        file_path: str | None,
    ) -> str:
        """生成数据库中唯一且稳定的媒体 path。

        图片/表情通常有真实落盘路径；视频/语音经常只有平台给的普通文件名
        （如 video.mp4），直接写入会撞 images.path 唯一约束。对这类无目录的
        普通文件名加上 hash 前缀，既保留原始文件名，也保证不同媒体不会冲突。
        """
        media_hash_text = str(media_hash or "").strip()
        media_type_text = str(media_type or "").strip().lower()
        raw_path = str(file_path or "").strip()
        if not raw_path:
            return media_hash_text

        path_obj = Path(raw_path)
        has_directory = bool(path_obj.parent and str(path_obj.parent) not in {"", "."})
        if media_type_text in {"video", "voice", "audio"} and not has_directory:
            suffix = media_hash_text[:16] or "unknown"
            return f"{media_type_text}:{suffix}:{path_obj.name or raw_path}"
        return raw_path

    async def get_media_info(self, media_hash: str) -> dict[str, Any] | None:
        """根据哈希值获取媒体信息。
        
        Args:
            media_hash: 媒体哈希值
            
        Returns:
            媒体信息字典，不存在返回 None
        """
        try:
            async with get_db_session() as session:
                # 如果存在多条重复记录，取最新一条返回
                stmt = (
                    select(Images)
                    .where(Images.image_id == media_hash)
                    .order_by(Images.timestamp.desc())
                    .limit(1)
                )
                result = await session.execute(stmt)
                media = result.scalars().first()

                if media:
                    return {
                        "id": media.id,
                        "image_id": media.image_id,
                        "path": media.path,
                        "type": media.type,
                        "description": media.description,
                        "count": media.count,
                        "timestamp": media.timestamp,
                        "vlm_processed": media.vlm_processed
                    }
                return None

        except Exception as e:
            logger.error(f"查询媒体信息失败: {e}", exc_info=True)
            return None

    # ──────────────────────────────────────────
    # 内部方法
    # ──────────────────────────────────────────

    async def _recognize_with_vlm(
        self, 
        base64_data: str, 
        media_type: str
    ) -> str | None:
        """使用 VLM 识别单个媒体。
        
        Args:
            base64_data: base64 编码的媒体数据
            media_type: 媒体类型（image 或 emoji）
            
        Returns:
            识别结果文本，失败返回 None
        """
        try:
            from src.app.plugin_system.api.llm_api import create_llm_request
            
            # 检查 VLM 模型是否可用
            if not self._vlm_model_set:
                logger.debug("VLM 模型不可用")
                return None

            # 创建 VLM 请求
            context_manager = LLMContextManager()
            request = create_llm_request(
                self._vlm_model_set,
                "image_recognition",
                context_manager=context_manager,
            )

            # 从提示词管理器获取提示词模板
            prompt_manager = get_prompt_manager()
            if media_type == "emoji":
                template = prompt_manager.get_template("media.emoji_recognition")
            else:
                template = prompt_manager.get_template("media.image_recognition")
            
            # 构建提示词（模板不需要参数，直接build）
            if template:
                prompt = await template.build()

            # 处理 base64 数据：提取纯净的 base64 内容，并尽量保留原始 MIME
            clean_base64 = self._extract_clean_base64(base64_data)
            mime_type = self._extract_image_mime_type(base64_data)

            # 使用标准的 data URL 格式（大多数 VLM API 都支持）
            image_value = f"data:{mime_type};base64,{clean_base64}"

            # 添加 payload 并发送请求
            request.add_payload(LLMPayload(ROLE.USER, [Text(prompt), Image(image_value)]))
            response = await request.send(stream=False)
            await response

            # 提取并处理描述
            description = response.message.strip() if response.message else ""
            
            # 限制长度
            if len(description) > 100:
                description = description[:97] + "..."

            return description if description else None

        except Exception as e:
            logger.error(f"VLM 识别失败: {e}", exc_info=True)
            return None

    async def _recognize_with_asr(self, audio_base64: str) -> str | None:
        """调用 ASR 客户端执行语音转文字。

        Args:
            audio_base64: base64 编码的 WAV 音频数据。

        Returns:
            识别出的文字，失败返回 None。
        """
        try:
            registry = ModelClientRegistry()
            model_set = self._asr_model_set
            # model_set 是 list[dict]，每个元素即一个 ModelEntry
            if not isinstance(model_set, list) or not model_set:
                logger.debug("ASR model_set 中无可用模型")
                return None

            model_entry = model_set[0]
            client = registry.get_asr_client_for_model(model_entry)
            model_name = model_entry.get("model_identifier") if isinstance(model_entry, dict) else str(model_entry)

            clean_b64 = self._extract_clean_base64(audio_base64)
            audio_bytes = await get_task_manager().to_process(
                base64_decode_to_bytes,
                clean_b64,
            )

            text = await client.create_transcription(
                model_name=model_name,
                audio_bytes=audio_bytes,
                request_name="voice_recognition",
                model_set=model_entry,
            )
            return text.strip() if text else None
        except Exception as e:
            logger.error(f"ASR 请求失败: {e}", exc_info=True)
            return None

    async def _recognize_with_audio_understanding(
        self,
        audio_base64: str,
        metadata: dict[str, Any],
    ) -> str | None:
        """用原生音频多模态模型理解音频，不只做 ASR。"""
        model_set = getattr(self, "_audio_understanding_model_set", None)
        if not model_set:
            return None

        try:
            from src.app.plugin_system.api.llm_api import create_llm_request

            request = create_llm_request(
                model_set,
                "audio_understanding",
                context_manager=LLMContextManager(),
            )

            filename = str(metadata.get("filename") or metadata.get("name") or "audio")
            mime_type = self._guess_audio_mime(metadata)
            prompt = (
                "请直接听这段音频，生成一段中文理解摘要。不要只做逐字 ASR。\n"
                "请覆盖：\n"
                "1. 如果有人声，概括说了什么、语气和情绪；\n"
                "2. 如果是音乐/环境声，概括旋律氛围、节奏、情绪、主要声音元素；\n"
                "3. 说明它在当前聊天里可能传达的感觉；\n"
                "4. 不确定的地方明确说不确定，不要编造。\n"
                f"文件名：{filename}\n"
                f"MIME：{mime_type}"
            )
            request.add_payload(
                LLMPayload(
                    ROLE.USER,
                    [
                        Text(prompt),
                        Audio(audio_base64, mime_type=mime_type),
                    ],
                )
            )
            response = await request.send(stream=False)
            await response

            message = (response.message or "").strip()
            if not message:
                return None
            return message[:800] if len(message) > 800 else message
        except Exception as e:
            logger.warning(f"原生音频理解失败，准备回退 ASR: {e}")
            return None

    @staticmethod
    def _guess_audio_mime(metadata: dict[str, Any]) -> str:
        raw = str(
            metadata.get("mime_type")
            or metadata.get("mime")
            or metadata.get("format")
            or ""
        ).strip().lower()
        if raw.startswith("audio/"):
            return raw
        filename = str(metadata.get("filename") or metadata.get("name") or "").lower()
        suffix = Path(filename.split("?", 1)[0].split("#", 1)[0]).suffix
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

    async def _get_cached_description(
        self,
        media_hash: str,
        media_type: str
    ) -> str | None:
        """从数据库缓存获取描述。
        
        Args:
            media_hash: 媒体哈希值
            media_type: 媒体类型
            
        Returns:
            缓存的描述，不存在返回 None
        """
        try:
            async with get_db_session() as session:
                stmt = select(ImageDescriptions).where(
                    ImageDescriptions.image_description_hash == media_hash,
                    ImageDescriptions.type == media_type
                )
                result = await session.execute(stmt)
                # 使用 scalars().first() 避免 MultipleResultsFound 错误
                desc = result.scalars().first()

                return desc.description if desc else None

        except Exception as e:
            logger.debug(f"查询缓存失败: {e}")
            return None

    async def _save_description_cache(
        self,
        media_hash: str,
        media_type: str,
        description: str
    ) -> None:
        """保存描述到缓存。
        
        Args:
            media_hash: 媒体哈希值
            media_type: 媒体类型
            description: 描述文本
        """
        try:
            async with get_db_session() as session:
                # 检查是否已存在（避免重复记录导致 MultipleResultsFound）
                stmt = (
                    select(ImageDescriptions)
                    .where(
                        ImageDescriptions.image_description_hash == media_hash,
                        ImageDescriptions.type == media_type
                    )
                    .order_by(ImageDescriptions.timestamp.desc())
                    .limit(1)
                )
                result = await session.execute(stmt)
                # 使用 scalars().first() 避免 MultipleResultsFound 错误
                existing = result.scalars().first()

                if not existing:
                    # 创建新缓存记录
                    new_desc = ImageDescriptions(
                        image_description_hash=media_hash,
                        type=media_type,
                        description=description,
                        timestamp=time.time()
                    )
                    session.add(new_desc)
                    await session.commit()
                    logger.debug(f"保存描述缓存: {media_hash[:8]}...")

        except Exception as e:
            logger.error(f"保存描述缓存失败: {e}", exc_info=True)

    async def _summarize_video_frames(
        self,
        frame_descriptions: list[str],
        metadata: dict[str, Any],
    ) -> str | None:
        """基于关键帧描述生成视频摘要。"""
        if not frame_descriptions:
            return None

        model_set = self._video_model_set or self._vlm_model_set
        if not model_set:
            return None

        try:
            from src.app.plugin_system.api.llm_api import create_llm_request
            from src.kernel.llm import LLMContextManager, LLMPayload, ROLE, Text

            request = create_llm_request(
                model_set,
                "video_frame_summary",
                context_manager=LLMContextManager(),
            )

            filename = str(metadata.get("filename", "video.mp4") or "video.mp4")
            size_mb = metadata.get("size_mb")
            size_text = f"{float(size_mb):.2f}MB" if isinstance(size_mb, (int, float)) else "未知"

            prompt = (
                "你会收到一个视频的关键帧识别结果，请生成一段 60~120 字的中文摘要。\n"
                "要求：\n"
                "1. 只基于给定关键帧，不要编造。\n"
                "2. 用“视频大致在讲什么 + 主要对象/动作 + 场景线索”的结构。\n"
                "3. 若信息不足，请明确说“画面信息有限”。\n"
                f"视频文件：{filename}，大小：{size_text}\n\n"
                "关键帧描述：\n"
                + "\n".join(frame_descriptions)
            )

            request.add_payload(LLMPayload(ROLE.USER, [Text(prompt)]))
            response = await request.send(stream=False)
            await response

            message = (response.message or "").strip()
            if not message:
                return None
            if len(message) > 160:
                return message[:157] + "..."
            return message
        except Exception as e:
            logger.debug(f"视频关键帧总结失败: {e}")
            return None

    async def _extract_video_keyframes(
        self,
        base64_data: str,
        filename: str = "video.mp4",
        max_frames: int = 3,
    ) -> list[str]:
        """从视频中抽取关键帧并返回 base64 图片列表。"""
        if max_frames <= 0:
            return []

        if shutil.which("ffmpeg") is None:
            logger.warning("未找到 ffmpeg，无法进行视频抽帧")
            return []

        try:
            clean_base64 = self._extract_clean_base64(base64_data)
            binary_data = await asyncio.to_thread(base64.b64decode, clean_base64)
        except Exception as e:
            logger.debug(f"视频 base64 解码失败: {e}")
            return []

        suffix = Path(filename).suffix or ".mp4"
        frame_results: list[str] = []

        try:
            with TemporaryDirectory(prefix="mofox_video_") as temp_dir:
                temp_path = Path(temp_dir)
                input_path = temp_path / f"input{suffix}"
                await asyncio.to_thread(input_path.write_bytes, binary_data)
                frame_pattern = str(temp_path / "frame_%03d.jpg")

                proc = await asyncio.create_subprocess_exec(
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-i",
                    str(input_path),
                    "-frames:v",
                    str(max_frames),
                    "-q:v",
                    "2",
                    frame_pattern,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                _, stderr = await proc.communicate()
                if proc.returncode != 0:
                    err_text = stderr.decode("utf-8", errors="ignore").strip()
                    logger.debug(f"ffmpeg 抽帧失败: {err_text or 'unknown'}")
                    return []

                frame_files = sorted(temp_path.glob("frame_*.jpg"))
                for frame_file in frame_files[:max_frames]:
                    try:
                        frame_bytes = await asyncio.to_thread(frame_file.read_bytes)
                        frame_results.append(f"base64|{base64.b64encode(frame_bytes).decode('utf-8')}")
                    except Exception as e:
                        logger.debug(f"读取关键帧失败({frame_file.name}): {e}")
        except Exception as e:
            logger.debug(f"视频抽帧流程失败: {e}")
            return []

        return frame_results

    @staticmethod
    def _extract_clean_base64(data: str) -> str:
        """提取纯净的 base64 数据（移除前缀和多余字符）。
        
        Args:
            data: 可能包含前缀的 base64 字符串
            
        Returns:
            纯净的 base64 字符串
        """
        # 移除可能的 data URL 前缀
        if data.startswith("data:"):
            # 提取 base64 部分
            if "base64," in data:
                data = data.split("base64,", 1)[1]
        elif data.startswith("base64|"):
            data = data[7:]
        
        # 移除可能的换行符和空格
        data = data.replace("\n", "").replace("\r", "").replace(" ", "")
        
        return data

    @staticmethod
    def _extract_image_mime_type(data: str) -> str:
        """从图片 data URL 中提取 MIME 类型。

        Args:
            data: 原始图片数据，可能是 data URL 或纯 base64

        Returns:
            图片 MIME 类型，无法识别时回退为 ``image/png``
        """
        if data.startswith("data:") and ";base64," in data:
            mime_type = data.split(";", 1)[0][len("data:") :].strip().lower()
            if mime_type.startswith("image/"):
                return mime_type
        return "image/png"

    @staticmethod
    def _extract_video_payload(video_data: str | dict[str, Any]) -> tuple[str, dict[str, Any]]:
        """提取视频 base64 数据和元信息。"""
        if isinstance(video_data, dict):
            for key in ("base64", "data", "video_base64"):
                candidate = video_data.get(key)
                if isinstance(candidate, str) and candidate.strip():
                    return candidate, video_data
            return "", video_data

        if isinstance(video_data, str):
            return video_data, {}

        return "", {}
    
    async def _save_to_pending(
        self,
        base64_data: str,
        media_hash: str,
        media_type: str
    ) -> Path:
        """保存媒体文件到待识别文件夹。
        
        Args:
            base64_data: base64 编码的媒体数据
            media_hash: 媒体哈希值
            media_type: 媒体类型
            
        Returns:
            保存的文件路径
        """
        try:
            # 提取纯净的 base64 数据
            clean_base64 = self._extract_clean_base64(base64_data)
            
            # 解码为二进制数据
            binary_data = await get_task_manager().to_process(
                base64_decode_to_bytes,
                clean_base64,
            )
            
            # 根据类型确定文件扩展名
            ext = ".jpg" if media_type == "image" else ".png"
            
            # 生成文件名（哈希值前16位 + 类型标记 + 扩展名）
            filename = f"{media_hash[:16]}_{media_type}{ext}"
            file_path = self.pending_folder / filename
            
            # 写入文件
            file_path.write_bytes(binary_data)
            logger.debug(f"媒体已保存到待识别文件夹: {filename}")
            
            return file_path
        except Exception as e:
            logger.error(f"保存到待识别文件夹失败: {e}")
            # 返回一个虚拟路径，不影响后续流程
            return self.pending_folder / f"{media_hash[:16]}_error.tmp"

    async def _move_to_category_folder(
        self,
        source_path: Path,
        media_type: str,
        media_hash: str
    ) -> None:
        """将识别完成的文件移动到对应的分类文件夹。
        
        Args:
            source_path: 源文件路径（待识别文件夹中的文件）
            media_type: 媒体类型
            media_hash: 媒体哈希值
        """
        try:
            if not source_path.exists():
                logger.debug(f"源文件不存在，跳过移动: {source_path.name}")
                return
            
            # 确定目标文件夹
            target_folder = self.images_folder if media_type == "image" else self.emojis_folder
            
            # 确定目标文件名
            target_path = target_folder / source_path.name
            
            # 如果目标文件已存在，删除源文件即可（去重）
            if target_path.exists():
                source_path.unlink()
                logger.debug(f"目标文件已存在，删除源文件: {source_path.name}")
                return
            
            # 移动文件
            source_path.rename(target_path)
            logger.debug(f"文件已移动到 {media_type} 文件夹: {target_path.name}")
        except Exception as e:
            logger.error(f"移动文件失败: {e}")

    @staticmethod
    def _compute_hash(data: str) -> str:
        """计算数据的 SHA256 哈希值。
        
        Args:
            data: 待哈希的数据（base64 字符串）
            
        Returns:
            十六进制哈希字符串
        """
        # 使用提取的纯净 base64 数据计算哈希
        clean_data = MediaManager._extract_clean_base64(data)
        return hashlib.sha256(clean_data.encode()).hexdigest()


# ──────────────────────────────────────────
# 单例访问
# ──────────────────────────────────────────


def get_media_manager() -> MediaManager:
    """获取媒体管理器单例。
    
    Returns:
        MediaManager 实例
    """
    global _media_manager
    if _media_manager is None:
        _media_manager = MediaManager()
    return _media_manager


def initialize_media_manager() -> MediaManager:
    """初始化媒体管理器（用于显式初始化）。
    
    Returns:
        MediaManager 实例
    """
    global _media_manager
    _media_manager = MediaManager()
    return _media_manager
