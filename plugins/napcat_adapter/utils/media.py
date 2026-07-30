"""媒体处理工具：图片/视频/音频下载与格式转换。"""

from __future__ import annotations

import io
from typing import TYPE_CHECKING

import httpx
from PIL import Image

from src.app.plugin_system.api.log_api import get_logger
from src.core.utils.base64_helper import base64_decode_to_bytes, base64_encode_bytes
from src.kernel.concurrency import get_task_manager

if TYPE_CHECKING:
    pass

logger = get_logger("napcat_adapter")


async def download_image_base64(url: str) -> str:
    """下载图片并返回 Base64 编码。"""
    if not url:
        raise ValueError("图片URL为空")

    timeout = httpx.Timeout(timeout=10.0, connect=5.0)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(url)
            response.raise_for_status()
            image_bytes = response.content
    except (httpx.ProxyError, httpx.ConnectError, httpx.ConnectTimeout) as e:
        logger.warning(f"图片下载代理/连接失败，重试直连: {e!s}")
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            response = await client.get(url)
            response.raise_for_status()
            image_bytes = response.content

    if not image_bytes:
        raise ValueError("图片内容为空")
    return await get_task_manager().to_thread(base64_encode_bytes, image_bytes)


async def convert_image_to_gif(image_base64: str) -> str:
    """将 Base64 编码的图片转换为 GIF 格式。"""
    try:
        image_bytes = await get_task_manager().to_thread(base64_decode_to_bytes, image_base64)
        image = Image.open(io.BytesIO(image_bytes))
        output_buffer = io.BytesIO()
        image.save(output_buffer, format="GIF")
        output_buffer.seek(0)
        return await get_task_manager().to_thread(base64_encode_bytes, output_buffer.read())
    except Exception as e:
        logger.error(f"图片转换为GIF失败: {e!s}")
        return image_base64


async def get_image_format(raw_data: str) -> str:
    """从 Base64 数据中判断图片格式。"""
    image_bytes = await get_task_manager().to_thread(base64_decode_to_bytes, raw_data)
    return Image.open(io.BytesIO(image_bytes)).format.lower()
