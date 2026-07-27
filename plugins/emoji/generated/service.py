"""Elysia 生成表情包服务。

这是独立的 NovelAI 客户端，不依赖旧画图插件，也不查旧表情包库。
"""

from __future__ import annotations

import asyncio
import base64
import io
import os
import random
import string
import time
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiohttp
from PIL import Image, ImageDraw, ImageFont

from src.core.components.base.service import BaseService
from src.kernel.concurrency import get_task_manager
from src.kernel.logger import get_logger

from ..config import EmojiConfig
from .prompt import EmojiStylePreset

logger = get_logger("emoji.generated")

_ENV_KEY_NAMES = ("NOVELAI_API_KEY", "NOVELAI_TOKEN", "NAI_API_KEY")


@dataclass(frozen=True, slots=True)
class EmojiGenerationRequest:
    """一次生成表情包请求。"""

    prompt: str
    style: EmojiStylePreset
    caption: str = ""
    resolution: str = ""
    negative_prompt: str = ""


class ElysiaGeneratedEmojiService(BaseService):
    """生成表情包服务：构图、调用 NovelAI、保存、叠字。"""

    service_name = "elysia_generated_emoji"
    service_description = "Elysia 现场生成表情包服务"
    version = "0.1.0"

    _queue: asyncio.Queue[tuple[EmojiGenerationRequest, asyncio.Future[tuple[bool, str, str | None]]]] = asyncio.Queue()
    _worker_started = False
    _worker_lock = asyncio.Lock()

    def __init__(self, plugin: Any) -> None:
        super().__init__(plugin)
        self._last_request_at = 0.0
        self._key_index = 0

    def _cfg(self) -> EmojiConfig.GeneratedSection:
        cfg = self.plugin.config
        if not isinstance(cfg, EmojiConfig):
            raise RuntimeError("emoji plugin config 未正确加载")
        return cfg.generated

    async def initialize(self) -> None:
        """启动生成表情包队列。"""

        Path(self._cfg().generation.output_dir).mkdir(parents=True, exist_ok=True)
        async with self.__class__._worker_lock:
            if not self.__class__._worker_started:
                self.__class__._worker_started = True
                get_task_manager().create_task(
                    self._queue_worker(),
                    name="elysia_generated_emoji_queue_worker",
                )
        logger.info("Elysia 生成表情包服务已初始化")

    async def generate(self, request: EmojiGenerationRequest) -> tuple[bool, str, str | None]:
        """排队生成图片。"""

        future: asyncio.Future[tuple[bool, str, str | None]] = asyncio.get_event_loop().create_future()
        await self.__class__._queue.put((request, future))
        logger.info(f"生成表情包任务已入队，队列长度={self.__class__._queue.qsize()}")
        return await future

    async def _queue_worker(self) -> None:
        while True:
            request: EmojiGenerationRequest | None = None
            future: asyncio.Future[tuple[bool, str, str | None]] | None = None
            try:
                request, future = await self.__class__._queue.get()
                result = await self._generate_now(request)
                if not future.done():
                    future.set_result(result)
            except Exception as exc:  # noqa: BLE001
                logger.error(f"生成表情包队列任务失败: {exc}", exc_info=exc)
                if future is not None and not future.done():
                    future.set_result((False, str(exc), None))
            finally:
                if request is not None:
                    self.__class__._queue.task_done()

    def _api_keys(self) -> list[str]:
        keys = [key.strip() for key in self._cfg().api.api_keys if str(key).strip()]
        for name in _ENV_KEY_NAMES:
            value = os.getenv(name, "").strip()
            if value:
                keys.append(value)
        return keys

    def _current_key(self) -> str | None:
        keys = self._api_keys()
        if not keys:
            return None
        return keys[self._key_index % len(keys)]

    def _rotate_key(self) -> None:
        if len(self._api_keys()) > 1:
            self._key_index += 1

    async def _generate_now(self, request: EmojiGenerationRequest) -> tuple[bool, str, str | None]:
        cfg = self._cfg()
        api_key = self._current_key()
        if not api_key:
            return False, "NovelAI API key 未配置", None

        elapsed = time.time() - self._last_request_at
        cooldown = max(0, int(cfg.api.cooldown_seconds))
        if elapsed < cooldown:
            await asyncio.sleep(cooldown - elapsed)
        self._last_request_at = time.time()

        width, height = self._parse_resolution(
            request.resolution or request.style.aspect_ratio or cfg.generation.default_resolution
        )
        prompt = self._build_prompt(request)
        negative_prompt = self._merge_negative(request.negative_prompt, request.style.negative)
        payload = self._build_payload(prompt, negative_prompt, width, height)
        try:
            image_bytes = await self._call_novelai(payload, api_key)
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            logger.warning(f"NovelAI 网络请求失败: {exc}", exc_info=exc)
            return False, f"NovelAI 网络请求失败：{exc}", None
        if image_bytes is None:
            return False, "NovelAI 没有返回可用图片", None

        path = self._save_image(image_bytes)
        if request.caption and cfg.caption.enabled:
            path = self._overlay_caption(path, request.caption)
        return True, "表情包生成成功", str(path)

    def _build_prompt(self, request: EmojiGenerationRequest) -> str:
        cfg = self._cfg()
        parts = [
            cfg.identity.character_prompt,
            request.style.prompt,
            request.prompt,
            cfg.identity.studio_signature,
        ]
        return ", ".join(part.strip(" ,") for part in parts if part.strip())

    def _merge_negative(self, *items: str) -> str:
        tags: list[str] = []
        for item in (self._cfg().generation.negative_prompt, *items):
            for tag in str(item or "").split(","):
                normalized = tag.strip()
                if normalized and normalized not in tags:
                    tags.append(normalized)
        return ", ".join(tags)

    def _build_payload(self, prompt: str, negative_prompt: str, width: int, height: int) -> dict[str, Any]:
        cfg = self._cfg()
        model = cfg.generation.model
        is_v4 = "diffusion-4" in model

        parameters: dict[str, Any] = {
            "width": width,
            "height": height,
            "scale": cfg.generation.scale,
            "steps": cfg.generation.steps,
            "sampler": cfg.generation.sampler,
            "seed": random.randint(0, 999999999),
            "n_samples": 1,
            "ucPreset": 0,
            "qualityToggle": True,
            "sm": False,
            "sm_dyn": False,
            "noise_schedule": cfg.generation.noise_schedule if is_v4 else "native",
            "negative_prompt": negative_prompt,
        }

        if is_v4:
            parameters.update(
                {
                    "params_version": 3,
                    "cfg_rescale": cfg.generation.cfg_rescale,
                    "autoSmea": False,
                    "legacy": False,
                    "legacy_v3_extend": False,
                    "legacy_uc": False,
                    "add_original_image": True,
                    "controlnet_strength": 1,
                    "dynamic_thresholding": False,
                    "prefer_brownian": True,
                    "normalize_reference_strength_multiple": True,
                    "use_coords": True,
                    "inpaintImg2ImgStrength": 1,
                    "deliberate_euler_ancestral_bug": False,
                    "skip_cfg_above_sigma": None,
                    "characterPrompts": [],
                    "reference_image_multiple": [],
                    "reference_information_extracted_multiple": [],
                    "reference_strength_multiple": [],
                    "v4_prompt": {
                        "caption": {"base_caption": prompt, "char_captions": []},
                        "use_coords": True,
                        "use_order": True,
                    },
                    "v4_negative_prompt": {
                        "caption": {"base_caption": negative_prompt, "char_captions": []},
                        "legacy_uc": False,
                    },
                }
            )

        return {
            "input": prompt,
            "model": model,
            "action": "generate",
            "parameters": parameters,
        }

    async def _call_novelai(self, payload: dict[str, Any], api_key: str) -> bytes | None:
        cfg = self._cfg()
        proxy = str(cfg.api.proxy or "").strip()
        total_timeout = max(1, int(cfg.api.timeout_seconds))
        connect_timeout = min(20, total_timeout)
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/zip",
            "Origin": "https://novelai.net",
            "Referer": "https://novelai.net",
            "User-Agent": "Neo-MoFox ElysiaGeneratedEmoji/0.1",
            "x-correlation-id": self._correlation_id(),
        }
        timeout = aiohttp.ClientTimeout(
            total=total_timeout,
            connect=connect_timeout,
            sock_connect=connect_timeout,
        )

        async with aiohttp.ClientSession(timeout=timeout) as session:
            if not proxy:
                return await self._post_novelai(session, payload, headers, proxy=None)

            try:
                return await self._post_novelai(session, payload, headers, proxy=proxy)
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
                logger.warning(f"NovelAI 代理请求失败，准备直连重试: {exc}")
                return await self._post_novelai(session, payload, headers, proxy=None)

    async def _post_novelai(
        self,
        session: aiohttp.ClientSession,
        payload: dict[str, Any],
        headers: dict[str, str],
        *,
        proxy: str | None,
    ) -> bytes | None:
        cfg = self._cfg()
        logger.info(
            "开始请求 NovelAI 生图接口"
            f" model={payload.get('model')}"
            f" timeout={max(1, int(cfg.api.timeout_seconds))}s"
            f" proxy={'yes' if proxy else 'no'}"
        )
        kwargs: dict[str, Any] = {"json": payload, "headers": headers}
        if proxy:
            kwargs["proxy"] = proxy
        async with session.post(cfg.api.generate_url, **kwargs) as response:
            body = await response.read()
            if response.status in (401, 402, 429):
                self._rotate_key()
            if response.status not in (200, 201):
                detail = body[:500].decode("utf-8", errors="replace")
                logger.error(f"NovelAI 生图失败 status={response.status} body={detail}")
                return None
            return self._extract_image_bytes(body)

    @staticmethod
    def _extract_image_bytes(body: bytes) -> bytes | None:
        if body.startswith(b"PK\x03\x04"):
            with zipfile.ZipFile(io.BytesIO(body)) as archive:
                for name in archive.namelist():
                    if name.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
                        return archive.read(name)
            return None
        if body.startswith((b"\x89PNG", b"\xff\xd8\xff", b"RIFF")):
            return body
        logger.warning(f"NovelAI 返回未知图片格式，前 8 字节={body[:8].hex()}")
        return body if body else None

    def _save_image(self, image_bytes: bytes) -> Path:
        base = Path(self._cfg().generation.output_dir)
        month_dir = base / time.strftime("%Y-%m")
        month_dir.mkdir(parents=True, exist_ok=True)
        path = month_dir / f"{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}.png"
        path.write_bytes(image_bytes)
        return path

    def _overlay_caption(self, image_path: Path, caption: str) -> Path:
        cfg = self._cfg()
        text = str(caption or "").strip()[: max(1, int(cfg.caption.max_chars))]
        if not text:
            return image_path

        with Image.open(image_path).convert("RGBA") as image:
            draw = ImageDraw.Draw(image)
            font = self._load_font(max(16, int(cfg.caption.font_size)))
            bbox = draw.textbbox((0, 0), text, font=font, stroke_width=3)
            text_w = bbox[2] - bbox[0]
            text_h = bbox[3] - bbox[1]
            pad_x = max(20, image.width // 32)
            pad_y = max(12, image.height // 70)
            box_w = min(image.width - 32, text_w + pad_x * 2)
            box_h = text_h + pad_y * 2
            x0 = (image.width - box_w) // 2
            y0 = image.height - box_h - max(24, image.height // 28)
            x1 = x0 + box_w
            y1 = y0 + box_h
            overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
            overlay_draw = ImageDraw.Draw(overlay)
            overlay_draw.rounded_rectangle(
                (x0, y0, x1, y1),
                radius=max(16, box_h // 3),
                fill=(255, 245, 250, 218),
                outline=(255, 170, 205, 220),
                width=3,
            )
            image = Image.alpha_composite(image, overlay)
            draw = ImageDraw.Draw(image)
            draw.text(
                ((image.width - text_w) // 2, y0 + pad_y - bbox[1]),
                text,
                font=font,
                fill=(58, 38, 52, 255),
                stroke_width=3,
                stroke_fill=(255, 255, 255, 240),
            )
            captioned_path = image_path.with_name(f"{image_path.stem}_caption.png")
            image.convert("RGB").save(captioned_path, "PNG")
            return captioned_path

    def _load_font(self, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
        for raw_path in self._cfg().caption.font_paths:
            path = Path(raw_path)
            if path.exists():
                try:
                    return ImageFont.truetype(str(path), size=size)
                except Exception:
                    continue
        return ImageFont.load_default()

    def _parse_resolution(self, value: str) -> tuple[int, int]:
        aliases = {
            "square": (1024, 1024),
            "1:1": (1024, 1024),
            "landscape": (1216, 832),
            "horizontal": (1216, 832),
            "16:9": (1216, 704),
            "portrait": (832, 1216),
            "vertical": (832, 1216),
            "9:16": (704, 1216),
        }
        normalized = str(value or "").strip().lower()
        if normalized in aliases:
            return aliases[normalized]
        try:
            width_raw, height_raw = normalized.split("x", 1)
            width = int(width_raw.strip())
            height = int(height_raw.strip())
        except Exception:
            return self._parse_resolution(self._cfg().generation.default_resolution)
        width = max(64, min(1600, round(width / 64) * 64))
        height = max(64, min(1600, round(height / 64) * 64))
        return width, height

    @staticmethod
    def _correlation_id() -> str:
        alphabet = string.ascii_letters + string.digits
        return "".join(random.choice(alphabet) for _ in range(6))

    @staticmethod
    def read_image_base64(path: str) -> str:
        """读取图片为 base64。"""

        return base64.b64encode(Path(path).read_bytes()).decode("utf-8")
