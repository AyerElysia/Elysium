"""文件相关 API Mixin

覆盖：群文件上传/删除/查询、私聊文件、文件夹管理、文件下载等。
"""

from __future__ import annotations

from typing import Any


class FileApiMixin:
    """文件相关 API。"""

    # ------------------------------------------------------------------
    # 文件上传
    # ------------------------------------------------------------------

    async def upload_group_file(
        self,
        group_id: int,
        file: str,
        name: str,
        folder: str = "/",
    ) -> dict[str, Any]:
        """上传群文件。

        Args:
            group_id: 群号
            file: 本地文件路径
            name: 文件名
            folder: 目标文件夹路径，默认根目录
        """
        return await self.call("upload_group_file", {  # type: ignore[attr-defined]
            "group_id": group_id,
            "file": file,
            "name": name,
            "folder": folder,
        })

    async def upload_private_file(
        self,
        user_id: int,
        file: str,
        name: str,
    ) -> dict[str, Any]:
        """上传私聊文件。"""
        return await self.call("upload_private_file", {  # type: ignore[attr-defined]
            "user_id": user_id,
            "file": file,
            "name": name,
        })

    # ------------------------------------------------------------------
    # 群文件管理
    # ------------------------------------------------------------------

    async def delete_group_file(
        self,
        group_id: int,
        file_id: str,
        busid: int = 0,
    ) -> dict[str, Any]:
        """删除群文件。"""
        return await self.call("delete_group_file", {  # type: ignore[attr-defined]
            "group_id": group_id,
            "file_id": file_id,
            "busid": busid,
        })

    async def create_group_file_folder(
        self,
        group_id: int,
        name: str,
        parent_id: str = "/",
    ) -> dict[str, Any]:
        """创建群文件文件夹。"""
        return await self.call("create_group_file_folder", {  # type: ignore[attr-defined]
            "group_id": group_id,
            "name": name,
            "parent_id": parent_id,
        })

    async def delete_group_folder(self, group_id: int, folder_id: str) -> dict[str, Any]:
        """删除群文件文件夹。"""
        return await self.call("delete_group_folder", {  # type: ignore[attr-defined]
            "group_id": group_id,
            "folder_id": folder_id,
        })

    # ------------------------------------------------------------------
    # 群文件查询
    # ------------------------------------------------------------------

    async def get_group_file_system_info(self, group_id: int) -> dict[str, Any]:
        """获取群文件系统信息（容量等）。"""
        return await self.call_data("get_group_file_system_info", {"group_id": group_id})  # type: ignore[attr-defined]

    async def get_group_root_files(self, group_id: int) -> dict[str, Any]:
        """获取群根目录文件列表。"""
        return await self.call_data("get_group_root_files", {"group_id": group_id})  # type: ignore[attr-defined]

    async def get_group_files_by_folder(
        self,
        group_id: int,
        folder_id: str = "/",
    ) -> dict[str, Any]:
        """获取群子目录文件列表。"""
        return await self.call_data("get_group_files_by_folder", {  # type: ignore[attr-defined]
            "group_id": group_id,
            "folder_id": folder_id,
        })

    async def get_group_file_url(
        self,
        group_id: int,
        file_id: str,
        busid: int = 0,
    ) -> dict[str, Any]:
        """获取群文件资源链接。"""
        return await self.call_data("get_group_file_url", {  # type: ignore[attr-defined]
            "group_id": group_id,
            "file_id": file_id,
            "busid": busid,
        })

    # ------------------------------------------------------------------
    # 通用文件操作
    # ------------------------------------------------------------------

    async def get_file(self, file_id: str = "", file: str = "") -> dict[str, Any]:
        """获取文件信息（通过 file_id 或文件名）。"""
        params: dict[str, Any] = {}
        if file_id:
            params["file_id"] = file_id
        if file:
            params["file"] = file
        return await self.call_data("get_file", params)  # type: ignore[attr-defined]

    async def download_file(
        self,
        url: str = "",
        base64: str = "",
        name: str = "",
    ) -> dict[str, Any]:
        """下载文件到缓存目录。"""
        params: dict[str, Any] = {}
        if url:
            params["url"] = url
        if base64:
            params["base64"] = base64
        if name:
            params["name"] = name
        return await self.call_data("download_file", params)  # type: ignore[attr-defined]
