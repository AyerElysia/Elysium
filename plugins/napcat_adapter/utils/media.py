"""媒体处理工具：图片/视频/音频下载与格式转换。"""

from __future__ import annotations

import asyncio
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


async def download_image_base64(url: str, max_attempts: int = 3) -> str:
    """下载图片并返回 Base64 编码。

    WSL 出站网络存在间歇性丢包（TCP 握手偶发十余秒），单次下载容易踩中，
    这里做多次尝试 + 递增退避；单次内部仍保留代理失败降级直连的逻辑。
    HTTP 状态错误（4xx/5xx）是服务器的明确响应，重试不会改变结果，
    直接抛出，不重试、不降级直连。
    """
    if not url:
        raise ValueError("图片URL为空")

    timeout = httpx.Timeout(timeout=6.0, connect=3.0)
    last_error: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            image_bytes = await _download_image_once(url, timeout)
            if not image_bytes:
                raise ValueError("图片内容为空")
            return await get_task_manager().to_thread(base64_encode_bytes, image_bytes)
        except httpx.HTTPStatusError:
            # 服务器明确返回的状态错误是确定结果：不重试、不降级直连。
            raise
        except Exception as e:  # noqa: BLE001 - 穷尽尝试后再抛出
            last_error = e
            if attempt < max_attempts:
                logger.warning(
                    f"图片下载第 {attempt} 次尝试失败: {e!s}，稍后重试"
                )
                await asyncio.sleep(0.5 * attempt)
    raise RuntimeError(
        f"图片下载失败（已尝试 {max_attempts} 次）: {last_error!s}"
    ) from last_error


async def _download_image_once(url: str, timeout: httpx.Timeout) -> bytes:
    """单次下载：代理/连接类错误自动降级直连再试一次。"""
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(url)
            response.raise_for_status()
            return response.content
    except (httpx.ProxyError, httpx.ConnectError, httpx.ConnectTimeout) as e:
        logger.warning(f"图片下载代理/连接失败，重试直连: {e!s}")
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            response = await client.get(url)
            response.raise_for_status()
            return response.content


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
