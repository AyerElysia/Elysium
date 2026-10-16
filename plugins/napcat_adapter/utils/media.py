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

# httpx 的 read timeout 只约束两次读取之间的空闲。QQ 图片 CDN 的 IPv6
# 地址会抢先连上，然后以很低的速率持续吐数据，空闲超时不会触发，外层时限
# 会被慢速传输吃满。单次总时限到点就换路线。
_IMAGE_ATTEMPT_DEADLINE_SECONDS = 12.0


def _image_route(attempt: int, max_attempts: int) -> str:
    """前几次走 IPv4，最后一次再放开双栈。"""
    if max_attempts > 1 and attempt >= max_attempts:
        return "default"
    return "ipv4"


def _image_client_kwargs(
    timeout: httpx.Timeout,
    *,
    route: str,
    trust_env: bool,
) -> dict[str, object]:
    kwargs: dict[str, object] = {
        "timeout": timeout,
        "follow_redirects": True,
    }
    if not trust_env:
        kwargs["trust_env"] = False
    if route == "ipv4":
        # local_address 绑定 IPv4 通配地址后，解析和连接都只使用 AF_INET。
        kwargs["transport"] = httpx.AsyncHTTPTransport(
            local_address="0.0.0.0",
            trust_env=trust_env,
        )
    return kwargs


async def download_image_base64(url: str, max_attempts: int = 3) -> str:
    """下载图片并返回 Base64 编码。

    出站网络存在间歇性丢包，单次下载容易踩中，这里做多次尝试 + 递增退避。
    QQ multimedia CDN 在本机上 IPv6 能握手但传大图极慢，所以优先 IPv4，
    并用单次总时限打断慢速传输；最后一次才回退到系统默认双栈。
    单次内部仍保留代理失败降级直连的逻辑。
    HTTP 状态错误（4xx/5xx）是服务器的明确响应，重试不会改变结果，
    直接抛出，不重试、不降级直连。
    """
    if not url:
        raise ValueError("图片URL为空")

    timeout = httpx.Timeout(timeout=6.0, connect=3.0)
    last_error: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        route = _image_route(attempt, max_attempts)
        try:
            async with asyncio.timeout(_IMAGE_ATTEMPT_DEADLINE_SECONDS):
                image_bytes = await _download_image_once(url, timeout, route=route)
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
                    "图片下载第 "
                    f"{attempt} 次尝试失败 ({route}): {type(e).__name__}: {e}，稍后重试"
                )
                await asyncio.sleep(0.5 * attempt)
    raise RuntimeError(
        f"图片下载失败（已尝试 {max_attempts} 次）: {type(last_error).__name__}: {last_error}"
    ) from last_error


async def _download_image_once(
    url: str,
    timeout: httpx.Timeout,
    *,
    route: str = "default",
) -> bytes:
    """单次下载：代理/连接类错误自动降级直连再试一次。"""
    try:
        return await _get_image_bytes(url, timeout, route=route, trust_env=True)
    except (httpx.ProxyError, httpx.ConnectError, httpx.ConnectTimeout) as e:
        logger.warning(f"图片下载代理/连接失败，重试直连: {type(e).__name__}: {e}")
        return await _get_image_bytes(url, timeout, route=route, trust_env=False)


async def _get_image_bytes(
    url: str,
    timeout: httpx.Timeout,
    *,
    route: str,
    trust_env: bool,
) -> bytes:
    client_kwargs = _image_client_kwargs(timeout, route=route, trust_env=trust_env)
    async with httpx.AsyncClient(**client_kwargs) as client:
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
