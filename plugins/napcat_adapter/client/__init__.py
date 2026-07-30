"""NapCat API Client

组合所有 API Mixin 为统一的 NapCatClient 类。
"""

from __future__ import annotations

from .account import AccountApiMixin
from .base import NapCatClientBase
from .file import FileApiMixin
from .group import GroupApiMixin
from .message import MessageApiMixin


class NapCatClient(
    AccountApiMixin,
    MessageApiMixin,
    GroupApiMixin,
    FileApiMixin,
    NapCatClientBase,
):
    """NapCat 全量 API 客户端。

    继承自所有 API Mixin 和 ClientBase，提供 100+ 个类型化 API 方法。
    通过 call(action, params) 可调用任意 NapCat 支持的 action。
    """

    # ------------------------------------------------------------------
    # 系统 API
    # ------------------------------------------------------------------

    async def get_status(self) -> dict:
        """获取运行状态。"""
        return await self.call_data("get_status")  # type: ignore[attr-defined]

    async def get_version_info(self) -> dict:
        """获取 NapCat 版本信息。"""
        return await self.call_data("get_version_info")  # type: ignore[attr-defined]

    async def clean_cache(self) -> dict:
        """清理缓存。"""
        return await self.call("clean_cache")  # type: ignore[attr-defined]

    async def can_send_image(self) -> dict:
        """检查是否可以发送图片。"""
        return await self.call_data("can_send_image")  # type: ignore[attr-defined]

    async def can_send_record(self) -> dict:
        """检查是否可以发送语音。"""
        return await self.call_data("can_send_record")  # type: ignore[attr-defined]

    async def check_url_safely(self, url: str) -> dict:
        """检查链接安全性。"""
        return await self.call_data("check_url_safely", {"url": url})  # type: ignore[attr-defined]

    async def get_record(
        self,
        file: str,
        out_format: str = "wav",
        file_id: str | None = None,
    ) -> dict:
        """获取语音文件。"""
        params: dict = {"file": file, "out_format": out_format}
        if file_id:
            params["file_id"] = file_id
        return await self.call_data("get_record", params)  # type: ignore[attr-defined]

    async def get_image(self, file: str) -> dict:
        """获取图片文件。"""
        return await self.call_data("get_image", {"file": file})  # type: ignore[attr-defined]


__all__ = ["NapCatClient"]
