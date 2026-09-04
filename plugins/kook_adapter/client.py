"""KOOK HTTP API 客户端

封装 KOOK REST API 调用（消息发送、文件上传、Gateway 获取等）。
"""
from __future__ import annotations

import os
from typing import Any

import httpx

from src.app.plugin_system.api.log_api import get_logger

logger = get_logger("kook_adapter")

KOOK_API_BASE = "https://www.kookapp.cn/api/v3"


class KookAPIClient:
    """KOOK REST API 客户端。"""

    def __init__(self, token: str) -> None:
        self._token = token
        self._http: httpx.AsyncClient | None = None

    async def start(self) -> None:
        """初始化 HTTP 客户端。"""
        self._http = httpx.AsyncClient(
            base_url=KOOK_API_BASE,
            headers={"Authorization": f"Bot {self._token}"},
            timeout=30.0,
        )

    async def close(self) -> None:
        """关闭 HTTP 客户端。"""
        if self._http:
            await self._http.aclose()
            self._http = None

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        """发送 API 请求并返回 data 字段。"""
        if not self._http:
            raise RuntimeError("KookAPIClient 未启动")
        resp = await self._http.request(method, path, **kwargs)
        resp.raise_for_status()
        body = resp.json()
        if body.get("code") != 0:
            raise RuntimeError(f"KOOK API 错误: code={body.get('code')} message={body.get('message')}")
        return body.get("data", {})

    # ─── Gateway ────────────────────────────────────────────

    async def get_gateway(self, compress: int = 0) -> str:
        """获取 WebSocket Gateway 地址。"""
        data = await self._request("GET", "/gateway/index", params={"compress": compress})
        url = data.get("url", "")
        if not url:
            raise RuntimeError("KOOK Gateway 返回空 URL")
        logger.info(f"获取 KOOK Gateway: {url[:60]}...")
        return url

    # ─── 消息 ───────────────────────────────────────────────

    async def send_channel_message(
        self,
        target_id: str,
        content: str,
        msg_type: int = 9,
        quote: str | None = None,
        temp_target_id: str | None = None,
    ) -> dict[str, Any]:
        """发送频道消息。

        Args:
            target_id: 频道 ID
            content: 消息内容
            msg_type: 消息类型（9=KMarkdown, 1=文本, 2=图片, 10=卡片）
            quote: 引用消息 ID
            temp_target_id: 临时消息目标用户 ID
        """
        payload: dict[str, Any] = {
            "target_id": target_id,
            "content": content,
            "type": msg_type,
        }
        if quote:
            payload["quote"] = quote
        if temp_target_id:
            payload["temp_target_id"] = temp_target_id
        return await self._request("POST", "/message/create", json=payload)

    async def send_direct_message(
        self,
        target_id: str,
        content: str,
        msg_type: int = 9,
        quote: str | None = None,
    ) -> dict[str, Any]:
        """发送私信消息。

        Args:
            target_id: 用户 ID
            content: 消息内容
            msg_type: 消息类型
            quote: 引用消息 ID
        """
        payload: dict[str, Any] = {
            "target_id": target_id,
            "content": content,
            "type": msg_type,
        }
        if quote:
            payload["quote"] = quote
        return await self._request("POST", "/direct-message/create", json=payload)

    async def update_message(self, msg_id: str, content: str) -> None:
        """编辑消息（仅 KMarkdown/Card）。"""
        await self._request("POST", "/message/update", json={"msg_id": msg_id, "content": content})

    async def delete_message(self, msg_id: str) -> None:
        """撤回消息。"""
        await self._request("POST", "/message/delete", json={"msg_id": msg_id})

    # ─── 文件上传 ───────────────────────────────────────────

    async def upload_asset(
        self,
        file_data: bytes,
        filename: str,
        content_type: str | None = None,
    ) -> str:
        """上传文件到 KOOK CDN，返回 URL。"""
        if not self._http:
            raise RuntimeError("KookAPIClient 未启动")
        if content_type:
            files: dict[str, tuple[str, bytes] | tuple[str, bytes, str]] = {
                "file": (filename, file_data, content_type)
            }
        else:
            files = {"file": (filename, file_data)}
        resp = await self._http.post("/asset/create", files=files)
        resp.raise_for_status()
        body = resp.json()
        if body.get("code") != 0:
            raise RuntimeError(f"KOOK 上传失败: {body.get('message')}")
        url = body.get("data", {}).get("url", "")
        logger.info(
            f"KOOK 文件已上传: name={filename} bytes={len(file_data)} "
            f"url={url[:80]}"
        )
        return url

    # ─── 用户/频道信息 ──────────────────────────────────────

    # ─── 媒体下载 ───────────────────────────────────────────

    async def download_media_bytes(self, url: str, timeout: float = 30.0) -> bytes:
        """下载媒体资源（KOOK CDN 或外部 URL）。

        使用独立于 API 客户端的连接（不带 Bot 鉴权头），超时与重定向自动跟随。
        """
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            return resp.content

    @staticmethod
    def _decode_base64_payload(b64_data: str) -> bytes:
        """解码 base64（兼容 base64| 前缀与 data: URL）。"""
        import base64 as _base64

        raw = b64_data
        if raw.startswith("data:"):
            raw = raw.split(",", 1)[1] if "," in raw else ""
        elif raw.startswith("base64|"):
            raw = raw[len("base64|"):]
        elif raw.startswith("base64://"):
            raw = raw[len("base64://"):]
        return _base64.b64decode(raw)

    async def upload_asset_from_base64(
        self,
        b64_data: str,
        filename: str,
        content_type: str | None = None,
    ) -> str:
        """解码 base64（兼容 base64| 前缀与 data: URL）并上传，返回 CDN URL。"""
        return await self.upload_asset(
            self._decode_base64_payload(b64_data),
            filename,
            content_type=content_type,
        )

    async def resolve_media_bytes(self, data: str) -> bytes:
        """将任意来源媒体解析为原始字节，尚未上传。"""
        if not data:
            raise ValueError("媒体数据为空")
        if data.startswith(("http://", "https://")):
            return await self.download_media_bytes(data)
        if data.startswith(("base64|", "base64://", "data:")) or not os.path.exists(data):
            return self._decode_base64_payload(data)
        with open(data, "rb") as f:
            return f.read()

    async def resolve_and_upload(
        self,
        data: str,
        filename: str,
        content_type: str | None = None,
    ) -> str:
        """将任意来源媒体（base64 / http(s) URL / 本地路径）上传到 KOOK CDN。

        KOOK 要求消息中的媒体资源必须由本 Bot 上传（否则"找不到资源"），
        因此外部 URL 需先下载再转存。
        """
        file_data = await self.resolve_media_bytes(data)
        return await self.upload_asset(file_data, filename, content_type=content_type)

    async def get_me(self) -> dict[str, Any]:
        """获取当前 Bot 信息。"""
        return await self._request("GET", "/user/me")

    async def get_channel_list(self, guild_id: str) -> list[dict[str, Any]]:
        """获取服务器频道列表。"""
        data = await self._request("GET", "/channel/list", params={"guild_id": guild_id})
        return data.get("items", [])
