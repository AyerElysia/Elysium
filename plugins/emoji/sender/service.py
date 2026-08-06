"""表情包候选感知、主体收藏、检索与发送服务。

后台 VLM 只登记客观候选，不决定主体是否喜欢或收藏。收藏与跳过必须
通过主体可见工具显式发生；旧自动人格判定入口保留为 fail-closed 兼容面。
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import math
import random
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.app.plugin_system.api.llm_api import (
    create_embedding_request,
    create_llm_request,
    get_model_set_by_task,
)
from src.app.plugin_system.api.send_api import send_emoji
from src.core.components.base.service import BaseService
from src.core.utils.base64_helper import base64_encode_bytes
from src.kernel.concurrency import get_task_manager
from src.kernel.logger import get_logger
from src.kernel.vector_db import get_vector_db_service

from ..config import EmojiConfig
from .meme_store import MemeStore
from .visual_embedder import VisualEmbedder, VisualEmbedError

try:
    from PIL import Image as PILImage
    from PIL import ImageOps as PILImageOps
except ImportError:
    PILImage = None
    PILImageOps = None


logger = get_logger("emoji.sender")


EMOTION_TAG_PRESET: tuple[str, ...] = (
    "开心",
    "难过",
    "生气",
    "惊讶",
    "害羞",
    "尴尬",
    "无语",
    "委屈",
    "嘲讽",
    "疑惑",
    "赞同",
    "否定",
    "兴奋",
    "疲惫",
    "害怕",
    "厌恶",
    "紧张",
    "冷漠",
)

_ALLOWED_SUFFIXES: frozenset[str] = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp"})

# VLM 侧解码器比 PIL 严格得多：越界的 EXIF orientation、异常 ICC profile、
# CMYK/16bit 采样、渐进式扫描等都可能让上游 prefill 直接报
# "Multimodal data is corrupted or cannot be processed."。
# 因此发给 VLM 的静态图一律重编码为干净的 baseline RGB JPEG，不做原样透传。
_VLM_MAX_LONG_SIDE = 1568
_VLM_JPEG_QUALITY = 85

_INGEST_LOCK = asyncio.Lock()


@dataclass(frozen=True, slots=True)
class MemeCandidate:
    """检索得到的候选表情包。"""

    meme_id: str
    tag: str
    path: str
    description: str
    distance: float


class EmojiSenderService(BaseService):
    """emoji_sender 服务。

    对外提供：
    - search_best：按 tag + 向量检索，返回满足阈值并经温度采样的表情包
    - send_best：发送检索到的表情包
    - ingest_once：执行一次入库任务（对齐→抽取→VLM 决策→写入）
    """

    service_name: str = "emoji_sender"
    service_description: str = "表情包收藏、检索与发送服务"
    version: str = "1.0.0"

    def _selection_temperature(self) -> float:
        """获取检索候选采样温度。"""
        return max(0.0, float(self._cfg().vector.temperature))

    def _select_candidate(self, candidates: list[MemeCandidate]) -> MemeCandidate | None:
        """按距离与温度从候选中选择一个表情包。"""
        if not candidates:
            return None

        ordered_candidates = sorted(candidates, key=lambda candidate: candidate.distance)
        temperature = self._selection_temperature()
        if temperature <= 0.0 or len(ordered_candidates) == 1:
            return ordered_candidates[0]

        base_distance = ordered_candidates[0].distance
        weights = [
            math.exp(-max(0.0, candidate.distance - base_distance) / temperature)
            for candidate in ordered_candidates
        ]
        if not any(weight > 0.0 for weight in weights):
            return ordered_candidates[0]

        return random.choices(ordered_candidates, weights=weights, k=1)[0]

    def _cfg(self) -> EmojiConfig.SenderSection:
        """获取插件配置实例（sender 命名空间）。"""
        cfg = self.plugin.config
        if not isinstance(cfg, EmojiConfig):
            raise RuntimeError("emoji plugin config 未正确加载")
        return cfg.sender

    @staticmethod
    def _media_cache_dir() -> Path:
        """media cache 的表情包目录。"""
        return Path("data") / "media_cache" / "emojis"

    def _manual_memes_dir(self) -> Path:
        """手动表情包目录。"""
        path = Path(self._cfg().ingest.manual_memes_dir)
        path.mkdir(parents=True, exist_ok=True)
        return path

    async def _pick_next_manual_meme_file(self) -> Path | None:
        """从手动目录获取下一个未入库的表情包文件。"""
        manual_dir = self._manual_memes_dir()
        
        candidates: list[Path] = [
            p
            for p in sorted(manual_dir.iterdir())
            if p.is_file() and p.suffix.lower() in _ALLOWED_SUFFIXES
        ]
        if not candidates:
            return None

        # 逐个检查，找到第一个未入库的
        for candidate in candidates:
            try:
                payload = candidate.read_bytes()
            except Exception:
                continue
            
            meme_id = self._sha256_bytes(payload)
            if not await self._already_ingested(meme_id):
                return candidate
        
        return None

    async def _already_ingested(self, source_hash: str) -> bool:
        """检查某个表情包（按 hash）是否已入库。"""
        vdb = self._vector_db()
        collection = self._collection_name()
        await vdb.get_or_create_collection(collection)
        data = await vdb.get(
            collection_name=collection,
            where={"source_hash": source_hash},
            limit=1,
            include=["metadatas"],
        )
        ids: list[str] = list(data.get("ids") or [])
        return bool(ids)

    def _data_dir(self) -> Path:
        """插件表情包复制目录。"""
        return Path(self._cfg().storage.data_dir)

    def _vector_db_path(self) -> str:
        """向量数据库路径。"""
        return str(self._cfg().vector.db_path)

    def _collection_name(self) -> str:
        """向量集合名。"""
        return str(self._cfg().vector.collection_name)

    def _vector_db(self):
        """获取（缓存的）向量数据库服务实例。"""
        return get_vector_db_service(self._vector_db_path())

    # ── 视觉检索 + 仿生收藏 基础设施 ────────────────────────

    _visual_embedder_cache: VisualEmbedder | None = None
    _meme_store_cache: MemeStore | None = None

    def _visual_embedder(self) -> VisualEmbedder:
        """获取（缓存的）视觉嵌入客户端。"""
        v = self._cfg().visual
        if EmojiSenderService._visual_embedder_cache is None:
            EmojiSenderService._visual_embedder_cache = VisualEmbedder(
                endpoint=str(v.embed_endpoint),
                timeout=float(v.request_timeout),
                query_instruction=str(v.query_instruction or ""),
            )
        return EmojiSenderService._visual_embedder_cache

    def _meme_store(self) -> MemeStore:
        """获取（缓存的）表情包存储（候选池 + 图片库 + 视觉向量库）。"""
        if EmojiSenderService._meme_store_cache is None:
            c = self._cfg().collection
            EmojiSenderService._meme_store_cache = MemeStore(
                db_path=str(c.meme_db_path),
                image_dir=str(c.meme_image_dir),
                vector_db=get_vector_db_service(str(self._cfg().vector.db_path)),
                collection_name=str(self._cfg().visual.collection_name),
            )
        return EmojiSenderService._meme_store_cache

    @staticmethod
    def _build_candidate(*, distance: float, metadata: dict[str, Any]) -> MemeCandidate | None:
        """从向量检索元数据中构建候选表情包。"""
        path_value = str(metadata.get("path") or "").strip()
        tag = str(metadata.get("tag") or "").strip()
        description = str(metadata.get("description") or metadata.get("documents") or "").strip()
        meme_id = str(metadata.get("meme_id") or "").strip()

        if not path_value or not tag or not meme_id:
            return None

        return MemeCandidate(
            meme_id=meme_id,
            tag=tag,
            path=path_value,
            description=description,
            distance=distance,
        )

    @staticmethod
    def _path_to_store_value(path: Path) -> str:
        """将路径转为存储在向量库 metadata 的字符串。"""
        return path.resolve().as_posix()

    @staticmethod
    def _sha256_bytes(data: bytes) -> str:
        """计算 bytes 的 sha256 十六进制值。"""
        return hashlib.sha256(data).hexdigest()

    @staticmethod
    def _detect_mime(data: bytes, suffix: str = "") -> str:
        """从文件内容（magic bytes）检测真实 MIME，后缀仅作 fallback。"""
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            return "image/png"
        if data[:2] == b"\xff\xd8":
            return "image/jpeg"
        if data[:6] in (b"GIF87a", b"GIF89a"):
            return "image/gif"
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            return "image/webp"
        # fallback: 按后缀猜
        suffix = suffix.lower()
        return {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".gif": "image/gif",
            ".webp": "image/webp",
        }.get(suffix, "image/png")

    @staticmethod
    def _normalize_static_for_vlm(
        image_bytes: bytes,
        max_bytes: int,
    ) -> tuple[bytes, str]:
        """把静态图重编码为干净的 baseline RGB JPEG。

        PIL 的解码容忍度远高于上游 VLM 的 prefill 解码器，因此"PIL 能打开"
        并不代表"上游能解析"。这里统一做一次彻底的重建：

        - 按 EXIF orientation 摆正，并容忍越界的 orientation 值（如 0）
        - 丢弃全部 EXIF / ICC profile / 其他附带 metadata
        - alpha / 调色板 / 灰度 / CMYK / 16bit 一律拍平为 8bit RGB（透明填白）
        - 长边限制在 _VLM_MAX_LONG_SIDE 以内
        - 输出 baseline（非渐进式）JPEG

        Returns:
            (重编码后的 bytes, "image/jpeg")
        """
        img = PILImage.open(io.BytesIO(image_bytes))
        img.load()

        # EXIF orientation：越界值（例如 orientation=0）会让部分严格解码器直接拒绝，
        # 这里吞掉异常，摆不正就按原样继续。
        if PILImageOps is not None:
            try:
                img = PILImageOps.exif_transpose(img) or img
            except Exception as e:
                logger.debug(f"EXIF orientation 处理失败，忽略: {e}")

        # 透明通道拍平到白底，其余色彩模式统一转 RGB
        if img.mode in ("RGBA", "LA", "PA") or (
            img.mode == "P" and "transparency" in img.info
        ):
            rgba = img.convert("RGBA")
            canvas = PILImage.new("RGB", rgba.size, (255, 255, 255))
            canvas.paste(rgba, mask=rgba.split()[-1])
            img = canvas
        elif img.mode != "RGB":
            img = img.convert("RGB")

        # 长边限制
        long_side = max(img.size)
        if long_side > _VLM_MAX_LONG_SIDE:
            ratio = _VLM_MAX_LONG_SIDE / long_side
            img = img.resize(
                (max(1, int(img.width * ratio)), max(1, int(img.height * ratio))),
                PILImage.Resampling.LANCZOS,
            )

        def _encode(image: Any, quality: int) -> bytes:
            buf = io.BytesIO()
            # exif=b"" / icc_profile=None：显式清空，避免旧 metadata 被带出去
            image.save(
                buf,
                format="JPEG",
                quality=quality,
                optimize=True,
                progressive=False,
                subsampling="4:2:0",
                exif=b"",
                icc_profile=None,
            )
            return buf.getvalue()

        out = _encode(img, _VLM_JPEG_QUALITY)

        # 仍超限则降质量，再不行降分辨率
        quality = _VLM_JPEG_QUALITY
        while len(out) > max_bytes and quality > 30:
            quality = max(30, quality - 10)
            out = _encode(img, quality)

        while len(out) > max_bytes and max(img.size) > 320:
            img = img.resize(
                (max(1, int(img.width * 0.8)), max(1, int(img.height * 0.8))),
                PILImage.Resampling.LANCZOS,
            )
            out = _encode(img, quality)

        return out, "image/jpeg"

    @staticmethod
    def _compress_image_for_vlm(image_bytes: bytes, mime: str, max_size_mb: float = 5.0) -> tuple[bytes, str, bool]:
        """把图片处理成可安全发给 VLM 的形式。

        - 静态图（JPG/PNG/WebP）：一律重编码为干净的 baseline RGB JPEG
          （详见 _normalize_static_for_vlm），不做原样透传
        - GIF：均匀采样最多 6 帧拼成网格图，以 JPEG 发给 VLM（仅用于识别，不影响入库的原文件）

        入库保存的始终是源文件，这里的输出只用于 VLM 识别。

        Returns:
            (用于 VLM 的 bytes, mime 类型, is_gif_frames_collage)
        """
        if PILImage is None:
            raise RuntimeError("PIL 未安装。请运行: uv add pillow")

        max_bytes = int(max_size_mb * 1024 * 1024)

        # 完整性校验：截断/损坏的图片直接拒绝，避免原样透传给 VLM 触发 400
        try:
            probe = PILImage.open(io.BytesIO(image_bytes))
            probe.load()
        except Exception as e:
            raise RuntimeError(f"图片损坏或不完整，无法解码: {e}") from e

        # GIF：提取多个关键帧拼成网格图
        if mime == "image/gif":
            try:
                img = PILImage.open(io.BytesIO(image_bytes))
                total_frames: int = getattr(img, "n_frames", 1)

                # 均匀采样，最多取 6 帧
                max_frames = 6
                if total_frames <= max_frames:
                    frame_indices = list(range(total_frames))
                else:
                    step = total_frames / max_frames
                    frame_indices = [int(i * step) for i in range(max_frames)]

                frames: list[Any] = []
                for idx in frame_indices:
                    try:
                        img.seek(idx)
                        frames.append(img.convert("RGB").copy())
                    except EOFError:
                        break

                if not frames:
                    raise RuntimeError("无法提取 GIF 帧")

                # 拼成网格（最多 3 列）
                cols = min(3, len(frames))
                rows = (len(frames) + cols - 1) // cols
                fw, fh = frames[0].size
                grid_img = PILImage.new("RGB", (fw * cols, fh * rows), (255, 255, 255))
                for i, frame in enumerate(frames):
                    x = (i % cols) * fw
                    y = (i // cols) * fh
                    grid_img.paste(frame.resize((fw, fh)), (x, y))

                output = io.BytesIO()
                grid_img.save(output, format="JPEG", quality=80)
                result_bytes = output.getvalue()

                # 如果网格图还是超限，缩小分辨率
                if len(result_bytes) > max_bytes:
                    scale = (max_bytes / len(result_bytes)) ** 0.5
                    new_w = max(1, int(grid_img.width * scale))
                    new_h = max(1, int(grid_img.height * scale))
                    grid_img = grid_img.resize((new_w, new_h), PILImage.Resampling.LANCZOS)
                    output = io.BytesIO()
                    grid_img.save(output, format="JPEG", quality=75)
                    result_bytes = output.getvalue()

                logger.debug(
                    f"GIF 提取 {len(frames)} 帧拼成网格用于 VLM: "
                    f"{len(image_bytes)} → {len(result_bytes)} 字节 "
                    f"(总帧数 {total_frames})"
                )
                return result_bytes, "image/jpeg", True

            except Exception as e:
                raise RuntimeError(f"GIF 处理失败: {e}") from e

        # 静态图：一律重编码，不做原样透传。
        # 原样透传会把越界 EXIF orientation、异常 ICC profile、CMYK/16bit 采样、
        # 渐进式扫描等直接送到上游，触发 400 "Multimodal data is corrupted"。
        try:
            normalized, output_mime = EmojiSenderService._normalize_static_for_vlm(
                image_bytes, max_bytes
            )
        except Exception as e:
            raise RuntimeError(f"图片规范化失败: {e}") from e

        if len(normalized) > max_bytes:
            logger.warning(f"图片重编码后仍超限，使用最后结果: {len(normalized)} 字节")
        else:
            logger.debug(
                f"图片已规范化用于 VLM: {len(image_bytes)} → {len(normalized)} 字节 "
                f"({mime} → {output_mime})"
            )
        return normalized, output_mime, False
    
    @staticmethod
    def _build_persona_prompt():
        """拒绝重新启用已退役的配置人格旁路。"""

        raise RuntimeError(
            "LegacyEmojiPersonaSourceRetired: background perception may only "
            "produce objective candidates"
        )

    @staticmethod
    def _extract_json_object(text: str) -> dict[str, Any] | None:
        """从模型输出中提取 JSON object。

        先尝试最后一个 { 到最后一个 }（推理模型在末尾输出答案时有效），
        再 fallback 到第一个 { 到最后一个 }（普通模型前后加文字时有效）。
        """
        if not text:
            return None

        end = text.rfind("}")
        if end == -1:
            return None

        # 尝试1：最后一个 JSON 对象（推理模型将答案放在末尾）
        start = text.rfind("{", 0, end + 1)
        if start != -1:
            try:
                obj = json.loads(text[start : end + 1])
                if isinstance(obj, dict):
                    return obj
            except Exception:
                pass

        # 尝试2：第一个 { 到最后一个 }（普通模型在前后添加了额外文字）
        start = text.find("{")
        if start != -1:
            try:
                obj = json.loads(text[start : end + 1])
                if isinstance(obj, dict):
                    return obj
            except Exception:
                pass

        return None

    async def _align_data_dir_with_db(self) -> None:
        """对齐 data_dir 与向量库记录。

        规则：
        - data_dir 中被删除的文件：清除向量库对应条目
        - data_dir 中多余的文件（库里无记录）：删除该文件

        该方法应在每次入库任务开头执行。
        """
        data_dir = self._data_dir()
        data_dir.mkdir(parents=True, exist_ok=True)

        vdb = self._vector_db()
        collection = self._collection_name()
        await vdb.get_or_create_collection(collection)

        # 1) 扫描磁盘文件
        files_on_disk: set[str] = set()
        for p in data_dir.iterdir():
            if not p.is_file():
                continue
            if p.suffix.lower() not in _ALLOWED_SUFFIXES:
                continue
            files_on_disk.add(self._path_to_store_value(p))

        # 2) 扫描向量库记录（全量 get）
        paths_in_db: set[str] = set()
        offset = 0
        limit = 512
        while True:
            data = await vdb.get(
                collection_name=collection,
                limit=limit,
                offset=offset,
                include=["metadatas"],
            )
            ids: list[str] = list(data.get("ids") or [])
            metadatas: list[dict[str, Any]] = list(data.get("metadatas") or [])

            if not ids:
                break

            # 容错：metadatas 可能长度不一致
            for i, record_id in enumerate(ids):
                meta = metadatas[i] if i < len(metadatas) else {}
                path_value = str(meta.get("path") or "").strip()
                if not path_value:
                    # metadata 缺失 path，直接删掉该条
                    await vdb.delete(collection_name=collection, ids=[record_id])
                    continue
                paths_in_db.add(path_value)

            offset += len(ids)

        # 3) data 被删 -> 清库
        missing_files = sorted(paths_in_db - files_on_disk)
        for missing_path in missing_files:
            await vdb.delete(collection_name=collection, where={"path": missing_path})

        # 4) 磁盘多余 -> 删文件
        orphan_files = sorted(files_on_disk - paths_in_db)
        for orphan_path in orphan_files:
            try:
                Path(orphan_path).unlink(missing_ok=True)
            except Exception as e:
                logger.warning(f"删除孤儿文件失败: {orphan_path} - {e}")

    def _pick_random_media_cache_file(self) -> Path | None:
        """从 media cache 的 emojis 目录随机挑选一个文件。"""
        root = self._media_cache_dir()
        if not root.exists():
            return None

        candidates: list[Path] = [
            p
            for p in root.iterdir()
            if p.is_file() and p.suffix.lower() in _ALLOWED_SUFFIXES
        ]
        if not candidates:
            return None
        return random.choice(candidates)

    async def _already_ingested(self, source_hash: str) -> bool:
        """检查某个表情包（按 hash）是否已入库。"""
        vdb = self._vector_db()
        collection = self._collection_name()
        await vdb.get_or_create_collection(collection)
        data = await vdb.get(
            collection_name=collection,
            where={"source_hash": source_hash},
            limit=1,
            include=["metadatas"],
        )
        ids: list[str] = list(data.get("ids") or [])
        return bool(ids)

    async def _vlm_decide_and_label(
        self,
        *,
        image_base64: str,
        mime: str,
        is_gif_collage: bool = False,
    ) -> dict[str, Any] | None:
        """调用 VLM 对表情包做收藏决策与标注。"""
        try:
            model_set = get_model_set_by_task("vision")
        except Exception:
            logger.debug("未配置 VLM 任务模型，跳过入库")
            return None

        persona = self._build_persona_prompt()
        tag_list = "、".join(EMOTION_TAG_PRESET)

        gif_hint = (
            "注意：这是一个 GIF 动图表情包的关键帧截图（网格排列），请综合所有帧的内容进行描述。\n"
            if is_gif_collage else ""
        )

        prompt = (
            "你将看到一张表情包图片。你的任务：根据人设，决定你是否愿意把它收藏起来以后自己使用。\n"
            + gif_hint
            + "你必须输出严格 JSON（不要输出任何额外文字），格式如下：\n"
            '{"keep": true/false, "description": "描述内容，文字：\'图中文字\'", "emotion_tags": ["标签1", "标签2"]}\n\n'
            "description 要求（文字部分不计入字数限制）：\n"
            "- 概括表情包传达的核心情绪、氛围和画面主要特征\n"
            "- 准确复述图中所有文字，格式为：文字：'逐字抄录'，放在末尾\n"
            "- 如果确保认出表情包的具体来源（作品名、角色名等），请补充说明\n"
            "- 无法确定出处则省略，只做客观描述\n"
            "- 总体 40 字以内（不计图中文字）\n"
            "- 无文字则省略文字部分\n\n"
            "JSON 字段说明：\n"
            "- keep：根据人设决定是否收藏\n"
            "- emotion_tags：必须从预设标签中选择（可多选，keep=false 时可为空）\n"
            "- 预设标签："
            + tag_list
            + "\n\n"
            "收藏标准：\n"
            "- 质量高且表达生动的表情包\n"
            "- 避免收藏低质、冒犯、违规或与人设不符的\n\n"
            "人设（来自主配置）：\n"
            + persona
        )

        from src.kernel.llm import Image, LLMContextManager, LLMPayload, ROLE, Text

        context_manager = LLMContextManager()
        request = create_llm_request(
            model_set=model_set,
            request_name="emoji_sender_label",
            context_manager=context_manager,
        )

        image_value = f"data:{mime};base64,{image_base64}"
        request.add_payload(LLMPayload(ROLE.USER, [Text(prompt), Image(image_value)]))

        try:
            response = await request.send(stream=False)
            await response
        except Exception as e:
            logger.warning(f"VLM 标注失败: {e}")
            return None

        raw = (response.message or response.reasoning_content or "").strip()
        obj = self._extract_json_object(raw)

        # 推理模型（如 MiMo）的 reasoning 长度不可控，偶发把输出预算吃光导致
        # content 是半截 JSON。此时换用任务列表里的后备模型重试一次。
        if obj is None and len(model_set) > 1:
            logger.debug(f"VLM 首选模型输出无法解析，降级重试 | raw={raw[:200]!r}")
            retry_request = create_llm_request(
                model_set=model_set[1:],
                request_name="emoji_sender_label",
                context_manager=LLMContextManager(),
            )
            retry_request.add_payload(LLMPayload(ROLE.USER, [Text(prompt), Image(image_value)]))
            try:
                retry_response = await retry_request.send(stream=False)
                await retry_response
                raw = (retry_response.message or retry_response.reasoning_content or "").strip()
                obj = self._extract_json_object(raw)
            except Exception as e:
                logger.warning(f"VLM 降级重试失败: {e}")

        if obj is None:
            logger.warning(f"VLM 输出无法解析为 JSON，跳过 | raw={raw[:300]!r}")
            return None

        keep = bool(obj.get("keep"))
        description = str(obj.get("description") or "").strip()
        tags = obj.get("emotion_tags")
        if not isinstance(tags, list):
            tags = []

        filtered_tags = [
            str(t).strip() for t in tags if isinstance(t, (str, int, float)) and str(t).strip() in EMOTION_TAG_PRESET
        ]

        if keep and (not description or not filtered_tags):
            keep = False

        if len(description) > 200:
            description = description[:197] + "..."

        return {
            "keep": keep,
            "description": description,
            "emotion_tags": filtered_tags,
        }

    async def ingest_once(self) -> None:
        """拒绝旧的自动人格判定收藏入口。

        调用方应使用 ``perception_scan`` 登记候选，再由 active consciousness
        通过收藏工具作出显式选择。保留方法名只为让旧调用明确失败。
        """
        raise RuntimeError(
            "AutomaticEmojiCollectionRetired: use perception_scan and explicit "
            "nucleus_collect_meme"
        )

        # 以下旧实现暂留作历史兼容参考，永远不会在 fail-closed 门之后执行。
        if _INGEST_LOCK.locked():
            logger.debug("上一轮入库尚未结束，跳过本轮")
            return

        async with _INGEST_LOCK:
            await self._align_data_dir_with_db()

            max_memes = int(self._cfg().storage.max_memes)
            if max_memes > 0:
                data_dir = self._data_dir()
                try:
                    current_count = sum(
                        1
                        for p in data_dir.iterdir()
                        if p.is_file() and p.suffix.lower() in _ALLOWED_SUFFIXES
                    )
                except FileNotFoundError:
                    current_count = 0

                if current_count >= max_memes:
                    logger.info(
                        f"表情包数量已达上限，跳过入库: {current_count}/{max_memes}"
                    )
                    return

            # 优先从手动目录获取表情包，否则才从随机缓存
            source = await self._pick_next_manual_meme_file()
            if source is None:
                if not self._cfg().ingest.sample_from_media_cache:
                    return
                source = self._pick_random_media_cache_file()
            
            if source is None:
                return

            try:
                payload = source.read_bytes()
            except Exception as e:
                logger.warning(f"读取候选表情包失败: {source} - {e}")
                return

            meme_id = self._sha256_bytes(payload)
            if await self._already_ingested(meme_id):
                return

            # 压缩图片用于 VLM
            try:
                mime = self._detect_mime(payload, source.suffix)
                vlm_bytes, vlm_mime, is_gif_collage = self._compress_image_for_vlm(payload, mime)
            except Exception as e:
                logger.warning(f"压缩图片失败: {source} - {e}")
                return

            image_base64 = await get_task_manager().to_thread(base64_encode_bytes, vlm_bytes)

            labeled = await self._vlm_decide_and_label(
                image_base64=image_base64, 
                mime=vlm_mime,
                is_gif_collage=is_gif_collage
            )
            if not labeled or not labeled.get("keep"):
                return

            description = str(labeled.get("description") or "").strip()
            tags: list[str] = list(labeled.get("emotion_tags") or [])
            tags = [t for t in tags if t in EMOTION_TAG_PRESET]
            if not description or not tags:
                return

            # 复制文件到插件 data 目录
            data_dir = self._data_dir()
            data_dir.mkdir(parents=True, exist_ok=True)
            suffix = source.suffix.lower() if source.suffix.lower() in _ALLOWED_SUFFIXES else ".png"
            target_path = data_dir / f"{meme_id}{suffix}"
            try:
                shutil.copy2(source, target_path)
            except Exception as e:
                logger.warning(f"复制表情包失败: {source} -> {target_path} - {e}")
                return

            # 生成 embedding
            try:
                embedding_model_set = get_model_set_by_task("embedding")
            except Exception:
                logger.warning("未配置 embedding 任务模型，跳过入库")
                return

            try:
                emb_req = create_embedding_request(
                    model_set=embedding_model_set,
                    request_name="emoji_sender_embedding",
                    inputs=[description],
                )
                emb_resp = await emb_req.send()
                embedding = emb_resp.embeddings[0]
            except Exception as e:
                logger.warning(f"生成 embedding 失败: {e}")
                return

            # 写入向量库：每个 tag 一条记录（metadata 全标量）
            vdb = self._vector_db()
            collection = self._collection_name()
            await vdb.get_or_create_collection(collection)

            ids: list[str] = []
            embeddings: list[list[float]] = []
            documents: list[str] = []
            metadatas: list[dict[str, Any]] = []

            stored_path = self._path_to_store_value(target_path)
            now_ts = time.time()

            for tag in tags:
                ids.append(f"{meme_id}:{tag}")
                embeddings.append(list(embedding))
                documents.append(description)
                metadatas.append(
                    {
                        "meme_id": meme_id,
                        "tag": tag,
                        "path": stored_path,
                        "description": description,
                        "source_hash": meme_id,
                        "source_cache_path": self._path_to_store_value(source),
                        "created_at": float(now_ts),
                    }
                )

            try:
                await vdb.add(
                    collection_name=collection,
                    ids=ids,
                    embeddings=embeddings,
                    documents=documents,
                    metadatas=metadatas,
                )
                logger.info(f"收藏表情包: {meme_id[:8]}... tags={tags}")
            except Exception as e:
                logger.warning(f"写入向量库失败: {e}")

    async def search_best(
        self,
        description_query: str,
        emotion_tags: list[str] | None = None,
    ) -> dict[str, Any] | None:
        """检索最贴合“想表达的意图”的表情包。

        纯视觉优先：把文本意图 embed 到多模态空间，直接检索表情包图像向量。
        视觉不可用/未启用/视觉库为空时，回退到旧的文本描述检索（应急）。
        """
        query = str(description_query or "").strip()
        if not query:
            raise ValueError("description_query 不能为空")

        if self._cfg().visual.embed_enabled:
            try:
                visual_result = await self._search_best_visual(query)
                if visual_result is not None:
                    return visual_result
            except VisualEmbedError as exc:
                logger.warning(f"视觉检索失败，回退文本检索: {exc}")
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"视觉检索异常，回退文本检索: {exc}")

        # 回退：旧的文本描述检索
        return await self._search_best_text(query, emotion_tags)

    async def _search_best_visual(self, query: str) -> dict[str, Any] | None:
        """纯视觉检索：文本意图 → 表情包图像向量。"""
        store = self._meme_store()
        await store.initialize()
        # 视觉库为空时返回 None，让上层回退文本检索
        if await store.count_collected() <= 0:
            return None

        embedder = self._visual_embedder()
        query_embedding = await embedder.embed_text(query, with_instruction=True)

        top_n = int(self._cfg().vector.top_n)
        results = await store.search_visual(query_embedding, top_n=top_n)

        ids_list: list[list[str]] = list(results.get("ids") or [])
        distances_list: list[list[float]] = list(results.get("distances") or [])
        metadatas_list: list[list[dict[str, Any]]] = list(results.get("metadatas") or [])
        if not ids_list or not ids_list[0]:
            return None

        # ChromaDB 返回 L2 距离；对归一化向量 cosine = 1 - dist^2/2。
        # 统一用 cosine 作为“distance”（越大越相似），与阈值比较更直观。
        min_cosine = float(self._cfg().visual.match_min_cosine)
        candidates: list[MemeCandidate] = []
        for i, meme_id in enumerate(ids_list[0]):
            l2 = float(distances_list[0][i]) if distances_list and distances_list[0] and i < len(distances_list[0]) else 2.0
            cosine = 1.0 - (l2 * l2) / 2.0
            meta = metadatas_list[0][i] if metadatas_list and metadatas_list[0] and i < len(metadatas_list[0]) else {}
            path_value = str(meta.get("path") or "")
            if not path_value:
                continue
            candidates.append(
                MemeCandidate(
                    meme_id=str(meme_id),
                    tag="",
                    path=path_value,
                    description=str(meta.get("note") or ""),
                    distance=cosine,  # 此处 distance 存的是 cosine（越大越相似）
                )
            )

        if not candidates:
            return None

        # cosine 越高越相似：阈值内温度采样；阈值外仍返回最接近的（她想表达时总给一张）
        # 注意：_select_candidate 按 distance 升序，而 cosine 是越大越好，故按 -cosine 排序
        candidates.sort(key=lambda c: c.distance, reverse=True)
        under_threshold = [c for c in candidates if c.distance >= min_cosine]
        pool = under_threshold if under_threshold else candidates
        best = self._select_candidate_visual(pool)
        if best is None:
            return None

        return {
            "meme_id": best.meme_id,
            "tag": best.tag,
            "path": best.path,
            "description": best.description,
            "distance": best.distance,
            "fallback_used": not under_threshold,
        }

    def _select_candidate_visual(self, candidates: list[MemeCandidate]) -> MemeCandidate | None:
        """视觉候选采样：cosine 越大越相似，按相似度温度采样。"""
        if not candidates:
            return None
        ordered = sorted(candidates, key=lambda c: c.distance, reverse=True)
        temperature = self._selection_temperature()
        if temperature <= 0.0 or len(ordered) == 1:
            return ordered[0]
        best_sim = ordered[0].distance
        weights = [
            math.exp(-max(0.0, best_sim - c.distance) / temperature)
            for c in ordered
        ]
        if not any(w > 0.0 for w in weights):
            return ordered[0]
        return random.choices(ordered, weights=weights, k=1)[0]

    async def _search_best_text(
        self,
        description_query: str,
        emotion_tags: list[str] | None = None,
    ) -> dict[str, Any] | None:
        """（回退）按 tag 过滤后执行文本描述向量检索，返回温度采样后的候选。"""
        query = str(description_query or "").strip()
        if not query:
            raise ValueError("description_query 不能为空")

        tags: list[str] = []
        if emotion_tags:
            tags = [str(t).strip() for t in emotion_tags if str(t).strip() in EMOTION_TAG_PRESET]

        try:
            embedding_model_set = get_model_set_by_task("embedding")
        except Exception:
            return None

        try:
            emb_req = create_embedding_request(
                model_set=embedding_model_set,
                request_name="emoji_sender_search",
                inputs=[query],
            )
            emb_resp = await emb_req.send()
            query_embedding = emb_resp.embeddings[0]
        except Exception as e:
            logger.warning(f"生成查询 embedding 失败: {e}")
            return None

        vdb = self._vector_db()
        collection = self._collection_name()
        await vdb.get_or_create_collection(collection)

        where: dict[str, Any] | None = None
        if tags:
            where = {"tag": tags}

        top_n = int(self._cfg().vector.top_n)
        max_distance = float(self._cfg().vector.max_distance)

        results = await vdb.query(
            collection_name=collection,
            query_embeddings=[list(query_embedding)],
            n_results=top_n,
            where=where,
        )

        ids_list: list[list[str]] = list(results.get("ids") or [])
        distances_list: list[list[float]] = list(results.get("distances") or [])
        metadatas_list: list[list[dict[str, Any]]] = list(results.get("metadatas") or [])

        if not ids_list or not ids_list[0]:
            return None

        all_candidates: list[MemeCandidate] = []
        best_any: MemeCandidate | None = None
        candidates_under_threshold: list[MemeCandidate] = []
        for i, _ in enumerate(ids_list[0]):
            distance = float(distances_list[0][i]) if distances_list and distances_list[0] and i < len(distances_list[0]) else 999.0
            meta = metadatas_list[0][i] if metadatas_list and metadatas_list[0] and i < len(metadatas_list[0]) else {}
            cand = self._build_candidate(distance=distance, metadata=meta)
            if cand is None:
                continue

            all_candidates.append(cand)
            if best_any is None or cand.distance < best_any.distance:
                best_any = cand

            if cand.distance <= max_distance:
                candidates_under_threshold.append(cand)

        fallback_used = False

        # 正常情况：在阈值内候选中按温度采样
        if candidates_under_threshold:
            best = self._select_candidate(candidates_under_threshold)
        else:
            # fallback：仅当“给了标签且标签有效（过滤后非空）”时，允许在指定标签内继续采样
            if tags and best_any is not None:
                best = self._select_candidate(all_candidates)
                fallback_used = True
            else:
                return None

        if best is None:
            return None

        return {
            "meme_id": best.meme_id,
            "tag": best.tag,
            "path": best.path,
            "description": best.description,
            "distance": best.distance,
            "fallback_used": fallback_used,
        }

    async def send_best_detailed(
        self,
        *,
        stream_id: str,
        platform: str | None,
        description_query: str,
        emotion_tags: list[str] | None = None,
    ) -> tuple[bool, dict[str, Any] | None, str]:
        """检索并发送最佳表情包，返回详细信息。

        Returns:
            (ok, result, reason)
            - ok: 是否发送成功
            - result: search_best 的返回值（成功与否都会尽量返回，便于上层展示细节）
            - reason: 失败原因或简短状态说明
        """
        result = await self.search_best(
            description_query=description_query,
            emotion_tags=emotion_tags,
        )
        if not result:
            return False, None, "没有找到满足条件的表情包"

        path = Path(str(result["path"]))
        if not path.exists():
            # 用户可能手动删了，下一次入库会对齐；这里直接失败
            return False, result, "表情包文件已被删除"

        try:
            payload = path.read_bytes()
        except Exception as e:
            logger.warning(f"读取表情包失败: {path} - {e}")
            return False, result, "读取表情包文件失败"

        image_base64 = await get_task_manager().to_thread(base64_encode_bytes, payload)
        desc = str(result.get("description") or "").strip()
        tag = str(result.get("tag") or "").strip()
        processed_plain_text = f"[表情包:{tag}:{desc}]" if desc else f"[表情包:{tag}]"

        ok = await send_emoji(
            emoji_data=image_base64,
            stream_id=stream_id,
            platform=platform,
            processed_plain_text=processed_plain_text,
        )

        if ok:
            return True, result, "发送成功"
        return False, result, "发送失败"

    async def send_best(
        self,
        *,
        stream_id: str,
        platform: str | None,
        description_query: str,
        emotion_tags: list[str] | None = None,
    ) -> bool:
        """检索并发送最佳表情包。"""
        ok, _, _ = await self.send_best_detailed(
            stream_id=stream_id,
            platform=platform,
            description_query=description_query,
            emotion_tags=emotion_tags,
        )
        return ok

    # ── 使用表情包：知情权 + 选择权 ─────────────────────

    async def recall_collected_memes(
        self,
        *,
        mode: str = "intent",
        description: str = "",
        count: int = 6,
    ) -> list[dict[str, Any]]:
        """召回她收藏的表情包候选（带着她当初的备注），由她自己挑。

        两种模式：
        - mode="visual"：按描述做视觉检索，返回最贴合的 count 张（算法按外观找候选）
        - mode="intent"：纯随机采 count 张（算法不猜意图，随机递一捼，她自己想起来、自己挑）

        两种都返回带备注的候选，选择权始终在她手里。
        """
        store = self._meme_store()
        await store.initialize()
        n = max(1, int(count or 6))

        mode = str(mode or "intent").strip().lower()
        if mode == "visual" and str(description or "").strip():
            # 视觉模式：按描述检索 top-n（带着备注）
            try:
                embedder = self._visual_embedder()
                query_embedding = await embedder.embed_text(
                    str(description).strip(), with_instruction=True
                )
                results = await store.search_visual(query_embedding, top_n=n)
                ids = list(results.get("ids") or [])
                metadatas = list(results.get("metadatas") or [])
                candidates: list[dict[str, Any]] = []
                if ids and ids[0]:
                    for i, mid in enumerate(ids[0]):
                        meta = metadatas[0][i] if metadatas and metadatas[0] and i < len(metadatas[0]) else {}
                        meta = meta or {}
                        path = str(meta.get("path") or "")
                        if not path:
                            continue
                        candidates.append(
                            {"meme_id": str(mid), "path": path, "note": str(meta.get("note") or "")}
                        )
                if candidates:
                    return candidates[:n]
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"视觉召回失败，转为随机采样: {exc}")

        # 意图模式（默认）：纯随机采样。算法不猜意图，随机递一捼，她自己挑。
        all_memes = await store.get_all_collected()
        if not all_memes:
            return []
        sampled = random.sample(all_memes, k=min(n, len(all_memes)))
        return sampled

    async def send_meme_by_id(
        self,
        *,
        meme_id: str,
        stream_id: str,
        platform: str | None,
    ) -> tuple[bool, str]:
        """发送她选中的那张表情包（按 meme_id）。"""
        store = self._meme_store()
        await store.initialize()
        meme = await store.get_meme_by_id(str(meme_id or "").strip())
        if not meme:
            return False, "找不到这张表情包（可能 id 不对）"

        path = Path(str(meme.get("path") or ""))
        if not path.exists():
            return False, "表情包文件已不存在"

        try:
            payload = path.read_bytes()
        except Exception as e:
            logger.warning(f"读取表情包失败: {path} - {e}")
            return False, "读取表情包文件失败"

        image_base64 = await get_task_manager().to_thread(base64_encode_bytes, payload)
        note = str(meme.get("note") or "").strip()
        processed_plain_text = f"[表情包:{note}]" if note else "[表情包]"

        ok = await send_emoji(
            emoji_data=image_base64,
            stream_id=stream_id,
            platform=platform,
            processed_plain_text=processed_plain_text,
        )
        if ok:
            return True, f"已发送表情包{('：' + note) if note else ''}"
        return False, "发送失败"

    # ── 仿生收藏：感知筛选 + 浏览 + 收藏（她的自主行为）──────────

    async def _vlm_perceive_meme(
        self,
        *,
        image_base64: str,
        mime: str,
        is_gif_collage: bool = False,
    ) -> dict[str, Any] | None:
        """感知层：轻量识别“这是不是表情包/贴纸”并给一句简短描述。

        这是前注意感知，只做“认出”，不做“是否收藏”的决定——那是她的选择。
        """
        try:
            model_set = get_model_set_by_task("vision")
        except Exception:
            logger.debug("未配置 VLM 任务模型，跳过感知筛选")
            return None

        gif_hint = (
            "注意：这是一个 GIF 动图的关键帧截图（网格排列），请综合所有帧判断。\n"
            if is_gif_collage else ""
        )
        prompt = (
            "判断这张图片是不是表情包/贴纸/meme（用于聊天表达情绪的图片），还是普通照片/截图/其他。\n"
            + gif_hint
            + "输出严格 JSON（不要额外文字）：\n"
            '{"is_meme": true/false, "brief": "一句话描述这张表情包在表达什么（20字内）"}\n'
            "判断依据：表情包通常有夸张表情、情绪表达、棗/二次元角色、叠字文案；"
            "普通照片/截图/文档不算。is_meme=false 时 brief 可为空。"
        )

        from src.kernel.llm import Image, LLMContextManager, LLMPayload, ROLE, Text

        request = create_llm_request(
            model_set=model_set,
            request_name="emoji_perceive",
            context_manager=LLMContextManager(),
        )
        image_value = f"data:{mime};base64,{image_base64}"
        request.add_payload(LLMPayload(ROLE.USER, [Text(prompt), Image(image_value)]))

        try:
            response = await request.send(stream=False)
            await response
        except Exception as e:
            logger.warning(f"感知筛选 VLM 调用失败: {e}")
            return None

        raw = (response.message or response.reasoning_content or "").strip()
        obj = self._extract_json_object(raw)

        # 与 _vlm_decide_and_label 同理：输出无法解析不是异常，request 层的
        # failover 不会因此换模型，于是每轮重扫都会撞在同一个首选模型上。
        # 推理模型（MiMo）的 reasoning 长度不可控，偶发吃光输出预算只留半截
        # JSON——这里主动降级到后备模型重试一次。
        if obj is None and len(model_set) > 1:
            logger.debug(f"感知筛选首选模型输出无法解析，降级重试 | raw={raw[:200]!r}")
            retry_request = create_llm_request(
                model_set=model_set[1:],
                request_name="emoji_perceive",
                context_manager=LLMContextManager(),
            )
            retry_request.add_payload(LLMPayload(ROLE.USER, [Text(prompt), Image(image_value)]))
            try:
                retry_response = await retry_request.send(stream=False)
                await retry_response
                raw = (retry_response.message or retry_response.reasoning_content or "").strip()
                obj = self._extract_json_object(raw)
            except Exception as e:
                logger.warning(f"感知筛选降级重试失败: {e}")

        if obj is None:
            logger.debug(f"感知筛选输出无法解析为 JSON | raw={raw[:200]!r}")
            return None
        return {
            "is_meme": bool(obj.get("is_meme")),
            "brief": str(obj.get("brief") or "").strip()[:60],
        }

    async def perception_scan(self, max_scan: int = 20) -> int:
        """感知筛选：扫描 media cache 新图片，识别表情包并登记到候选池。

        前注意、低频、只登记不决策。返回本次新增候选数。
        """
        store = self._meme_store()
        await store.initialize()
        root = self._media_cache_dir()
        if not root.exists():
            return 0

        files = [
            p for p in root.iterdir()
            if p.is_file() and p.suffix.lower() in _ALLOWED_SUFFIXES
        ]
        if not files:
            return 0

        added = 0
        scanned = 0
        for path in files:
            if scanned >= max_scan:
                break
            try:
                payload = path.read_bytes()
            except Exception:
                continue
            source_hash = self._sha256_bytes(payload)
            if await store.has_hash(source_hash):
                continue  # 已登记过（任意状态）
            scanned += 1

            try:
                mime = self._detect_mime(payload, path.suffix)
                vlm_bytes, vlm_mime, is_gif_collage = self._compress_image_for_vlm(payload, mime)
                image_b64 = await get_task_manager().to_thread(base64_encode_bytes, vlm_bytes)
            except Exception as e:
                logger.debug(f"感知筛选预处理失败 {path.name}: {e}")
                continue

            perceived = await self._vlm_perceive_meme(
                image_base64=image_b64, mime=vlm_mime, is_gif_collage=is_gif_collage
            )

            # 关键：区分「VLM 说不是表情包」和「VLM 没能给出结论」。
            # 上游 prefill 偶发 400（Multimodal data is corrupted）、超时、限流、
            # 输出截断等都会让 perceived 为 None，这是暂时性失败而非判定结果。
            # 若在此登记 dismissed，has_hash 会让这张图永久不再被感知——
            # 一次抖动就永久丢掉一张表情包。因此失败时不落库，留给下一轮重扫。
            if perceived is None:
                logger.debug(f"感知筛选未得出结论，留待下轮重试: {path.name}")
                continue

            if not perceived.get("is_meme"):
                # 明确判定不是表情包：登记为 dismissed，避免反复感知同一张
                await store.add_candidate(
                    source_hash=source_hash, source_path=str(path), mime=mime,
                )
                await store.mark_dismissed(source_hash)
                continue

            ok = await store.add_candidate(
                source_hash=source_hash,
                source_path=str(path),
                mime=mime,
                brief=str(perceived.get("brief") or ""),
            )
            if ok:
                added += 1

        if added:
            logger.info(f"感知筛选：新增 {added} 张表情包候选（扫描 {scanned} 张新图）")
        return added

    async def browse_candidates(self, limit: int | None = None) -> list[dict[str, Any]]:
        """浏览未收藏的表情包候选（供她判断要不要收藏）。"""
        store = self._meme_store()
        await store.initialize()
        if limit is None:
            limit = int(self._cfg().collection.browse_page_size)
        candidates = await store.list_unreviewed(limit=limit)
        return [
            {
                "candidate_id": c.candidate_id,
                "brief": c.brief,
                "source_path": c.source_path,
                "mime": c.mime,
            }
            for c in candidates
        ]

    async def get_unreviewed_count(self) -> int:
        """未浏览的表情包候选数量（供好奇心上下文感知）。"""
        try:
            store = self._meme_store()
            await store.initialize()
            return await store.count_unreviewed()
        except Exception:  # noqa: BLE001
            return 0

    async def collect_meme(self, candidate_id: str, note: str = "") -> tuple[bool, str]:
        """收藏一张表情包（她的决定）：视觉 embed → 入库 → 去重 → 标记收藏。"""
        store = self._meme_store()
        await store.initialize()

        candidate = await store.get_candidate(candidate_id)
        if candidate is None:
            return False, "找不到这张候选表情包"
        if candidate.status == "collected":
            return False, "这张已经收藏过了"

        source_path = Path(candidate.source_path)
        if not source_path.exists():
            return False, "原图已不存在，无法收藏"

        try:
            image_bytes = source_path.read_bytes()
        except Exception as e:
            return False, f"读取原图失败: {e}"

        # 视觉去重：已有近似图则不重复收藏
        embedder = self._visual_embedder()
        try:
            embedding = await embedder.embed_image_bytes(image_bytes)
        except VisualEmbedError as e:
            return False, f"视觉嵌入失败，无法收藏: {e}"

        threshold = float(self._cfg().collection.visual_dedup_threshold)
        if await store.is_visual_duplicate(embedding, threshold=threshold):
            await store.mark_dismissed(candidate_id)
            return False, "已经有一张几乎一样的表情包了，不重复收藏"

        # 复制图片到图片库 + 写入视觉向量库
        meme_id = candidate.source_hash
        try:
            stored_path = store.save_image(meme_id, str(source_path), candidate.mime)
            await store.store_visual(
                meme_id=meme_id,
                embedding=embedding,
                source_hash=candidate.source_hash,
                image_path=stored_path,
                source_stream=candidate.source_stream,
                source_message_id=candidate.source_message_id,
                note=str(note or ""),
            )
        except Exception as e:
            return False, f"入库失败: {e}"

        await store.mark_collected(candidate_id, meme_id, note=str(note or ""))
        brief = candidate.brief or "一张表情包"
        logger.info(f"收藏表情包: {meme_id[:8]}... | {brief} | note={note!r}")
        return True, f"已收藏这张表情包（{brief}）"

    async def dismiss_meme(self, candidate_id: str) -> tuple[bool, str]:
        """跳过一张候选（不想收藏）。"""
        store = self._meme_store()
        await store.initialize()
        if await store.get_candidate(candidate_id) is None:
            return False, "找不到这张候选表情包"
        await store.mark_dismissed(candidate_id)
        return True, "好的，不收藏这张"

    async def update_meme_description(self, meme_id: str, new_description: str) -> bool:
        """更新向量库中某个表情包的描述并重新生成 embedding。

        适用场景：WebUI 修改了 SQLite 里的描述后，同步更新向量库 metadata，
        避免检索时仍使用旧描述。

        如果该表情包尚未入库（例如因 max_memes 限制被跳过），会尝试
        在磁盘上找到对应文件后自动入库。

        Args:
            meme_id: 表情包的 SHA256 hash（与 source_hash / meme_id 字段一致）
            new_description: 新的描述文本

        Returns:
            True 表示成功更新；False 表示无条目可更新或出现错误
        """
        new_desc = str(new_description or "").strip()
        if not meme_id or not new_desc:
            return False

        vdb = self._vector_db()
        collection = self._collection_name()
        await vdb.get_or_create_collection(collection)

        # 取出该 meme_id 的全部向量条目
        data = await vdb.get(
            collection_name=collection,
            where={"meme_id": meme_id},
            include=["metadatas", "documents"],
        )
        ids: list[str] = list(data.get("ids") or [])
        metadatas: list[dict] = list(data.get("metadatas") or [])

        # 重新生成 embedding
        try:
            embedding_model_set = get_model_set_by_task("embedding")
        except Exception:
            logger.warning("update_meme_description: 未配置 embedding 任务模型，无法更新向量库")
            return False

        try:
            emb_req = create_embedding_request(
                model_set=embedding_model_set,
                request_name="emoji_sender_update_embedding",
                inputs=[new_desc],
            )
            emb_resp = await emb_req.send()
            new_embedding = list(emb_resp.embeddings[0])
        except Exception as e:
            logger.warning(f"update_meme_description: 生成 embedding 失败: {e}")
            return False

        if not ids:
            # 向量库中无条目，尝试在磁盘上找到文件后自动入库
            return await self._ingest_missing_meme(meme_id, new_desc, new_embedding)

        # 删除旧条目
        try:
            await vdb.delete(collection_name=collection, ids=ids)
        except Exception as e:
            logger.warning(f"update_meme_description: 删除旧条目失败: {e}")
            return False

        # 用更新后的描述和 embedding 重新写入
        new_metadatas = [
            {**meta, "description": new_desc}
            for meta in metadatas
        ]

        try:
            await vdb.add(
                collection_name=collection,
                ids=ids,
                embeddings=[new_embedding] * len(ids),
                documents=[new_desc] * len(ids),
                metadatas=new_metadatas,
            )
        except Exception as e:
            logger.warning(f"update_meme_description: 重新写入向量库失败: {e}")
            return False

        logger.info(f"update_meme_description: 已同步 meme_id={meme_id[:8]}... ({len(ids)} 条)")
        return True

    async def _ingest_missing_meme(
        self,
        meme_id: str,
        description: str,
        embedding: list[float],
    ) -> bool:
        """当表情包在向量库中缺失时，尝试从磁盘找到文件并入库。

        会在以下位置查找文件：
        1. media_cache/emojis/
        2. 手动表情包目录
        3. emoji_sender data_dir（可能已存在但向量库记录丢失）

        Args:
            meme_id: 表情包的 SHA256 hash
            description: 要写入向量库的描述文本
            embedding: 预计算好的 embedding 向量

        Returns:
            True 表示成功入库；False 表示文件不存在或入库失败
        """
        data_dir = self._data_dir()
        source_path: Path | None = None

        # 在 media_cache 和手动目录中按文件名查找
        search_dirs = [self._media_cache_dir(), self._manual_memes_dir()]
        for search_dir in search_dirs:
            if not search_dir.exists():
                continue
            for suffix in _ALLOWED_SUFFIXES:
                candidate = search_dir / f"{meme_id}{suffix}"
                if candidate.is_file():
                    source_path = candidate
                    break
            if source_path:
                break

        # 如果在 data_dir 中找到了（向量库记录丢失但文件还在）
        if source_path is None and data_dir.exists():
            for suffix in _ALLOWED_SUFFIXES:
                candidate = data_dir / f"{meme_id}{suffix}"
                if candidate.is_file():
                    source_path = candidate
                    break

        if source_path is None:
            logger.debug(
                f"update_meme_description: 向量库和磁盘均未找到 meme_id={meme_id[:8]}...，无法入库"
            )
            return False

        # 确保文件在 data_dir 中
        data_dir.mkdir(parents=True, exist_ok=True)
        target_path = data_dir / f"{meme_id}{source_path.suffix.lower()}"
        if not target_path.is_file():
            try:
                shutil.copy2(source_path, target_path)
            except Exception as e:
                logger.warning(f"update_meme_description: 复制表情包失败: {source_path} -> {target_path} - {e}")
                return False

        # 从新描述中提取 emotion_tags
        tags = self._extract_tags_from_description(description)
        if not tags:
            tags = ["neutral"]

        # 写入向量库
        vdb = self._vector_db()
        collection = self._collection_name()
        stored_path = self._path_to_store_value(target_path)
        now_ts = time.time()

        new_ids: list[str] = []
        new_embeddings: list[list[float]] = []
        new_documents: list[str] = []
        new_metadatas: list[dict[str, Any]] = []

        for tag in tags:
            new_ids.append(f"{meme_id}:{tag}")
            new_embeddings.append(list(embedding))
            new_documents.append(description)
            new_metadatas.append({
                "meme_id": meme_id,
                "tag": tag,
                "path": stored_path,
                "description": description,
                "source_hash": meme_id,
                "source_cache_path": self._path_to_store_value(source_path),
                "created_at": float(now_ts),
            })

        try:
            await vdb.add(
                collection_name=collection,
                ids=new_ids,
                embeddings=new_embeddings,
                documents=new_documents,
                metadatas=new_metadatas,
            )
            logger.info(f"update_meme_description: 自动入库 meme_id={meme_id[:8]}... tags={tags}")
            return True
        except Exception as e:
            logger.warning(f"update_meme_description: 自动入库写入向量库失败: {e}")
            return False

    @staticmethod
    def _extract_tags_from_description(description: str) -> list[str]:
        """从描述文本中提取 emotion_tags。

        尝试解析描述中的情感标签，支持以下格式：
        - "Keywords: [tag1, tag2] Desc: ..."
        - 描述文本中包含 EMOTION_TAG_PRESET 中的关键词

        Returns:
            匹配到的 emotion_tags 列表（已去重，保持在 EMOTION_TAG_PRESET 内）
        """
        tags: list[str] = []

        # 尝试解析 "Keywords: [xxx, yyy]" 格式
        kw_match = re.search(r"Keywords:\s*\[([^\]]*)\]", description, re.IGNORECASE)
        if kw_match:
            raw_tags = [t.strip().strip("'\"") for t in kw_match.group(1).split(",") if t.strip()]
            tags.extend(t for t in raw_tags if t in EMOTION_TAG_PRESET)

        # 从整个描述中扫描情感关键词
        desc_lower = description.lower()
        for tag in EMOTION_TAG_PRESET:
            if tag not in tags and tag in desc_lower:
                tags.append(tag)

        return tags
