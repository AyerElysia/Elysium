"""视觉嵌入客户端：调用本地 Qwen3-VL-Embedding 服务。

将文本意图与表情包图像映射到同一语义空间，用于纯视觉检索与仿生收藏。
服务不可用时抛出 VisualEmbedError，由调用方决定降级策略。
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

import httpx

from src.kernel.logger import get_logger

logger = get_logger("emoji.visual_embedder")


class VisualEmbedError(RuntimeError):
    """视觉嵌入服务调用失败。"""


class VisualEmbedder:
    """本地视觉嵌入服务的异步客户端（OpenAI 兼容 /v1/embeddings）。"""

    def __init__(
        self,
        endpoint: str,
        *,
        timeout: float = 30.0,
        query_instruction: str | None = None,
    ) -> None:
        self._endpoint = endpoint
        self._timeout = timeout
        self._query_instruction = query_instruction or None

    async def _post(self, payload: dict[str, Any]) -> list[float]:
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(self._endpoint, json=payload)
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPError as exc:
            raise VisualEmbedError(f"视觉嵌入服务调用失败: {exc}") from exc
        except Exception as exc:  # noqa: BLE001
            raise VisualEmbedError(f"视觉嵌入服务异常: {exc}") from exc

        try:
            return list(data["data"][0]["embedding"])
        except (KeyError, IndexError, TypeError) as exc:
            raise VisualEmbedError(f"视觉嵌入服务返回格式异常: {data!r}") from exc

    async def embed_text(self, text: str, *, with_instruction: bool = True) -> list[float]:
        """嵌入文本（检索 query）。默认附带指令前缀以提升文本→图像匹配。"""
        instruction = self._query_instruction if with_instruction else None
        payload: dict[str, Any] = {"input": text}
        if instruction:
            payload["instruction"] = instruction
        return await self._post(payload)

    async def embed_image_base64(self, image_b64: str) -> list[float]:
        """嵌入 base64 编码的图像（入库时用，不带指令）。"""
        return await self._post({"image": image_b64})

    async def embed_image_bytes(self, image_bytes: bytes) -> list[float]:
        """嵌入图像字节（自动 base64 编码）。"""
        return await self.embed_image_base64(base64.b64encode(image_bytes).decode())

    async def embed_image_file(self, path: str | Path) -> list[float]:
        """嵌入本地图像文件。"""
        data = Path(path).read_bytes()
        return await self.embed_image_bytes(data)

    async def is_available(self) -> bool:
        """探测服务是否可用（健康检查）。"""
        health_url = self._endpoint.rsplit("/", 2)[0] + "/health"
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(health_url)
                return resp.status_code == 200
        except Exception:  # noqa: BLE001
            return False
