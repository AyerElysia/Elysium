"""账号相关 API Mixin

覆盖：资料设置、在线状态、好友管理、凭证、收藏、OCR 等。
"""

from __future__ import annotations

from typing import Any


class AccountApiMixin:
    """账号与好友相关 API。"""

    # ------------------------------------------------------------------
    # 登录与资料
    # ------------------------------------------------------------------

    async def get_login_info(self) -> dict[str, Any]:
        """获取登录号信息（user_id, nickname）。"""
        return await self.call_data("get_login_info")  # type: ignore[attr-defined]

    async def set_qq_profile(
        self,
        nickname: str | None = None,
        company: str | None = None,
        email: str | None = None,
        college: str | None = None,
        personal_note: str | None = None,
    ) -> dict[str, Any]:
        """设置登录号资料。"""
        params: dict[str, Any] = {}
        if nickname is not None:
            params["nickname"] = nickname
        if company is not None:
            params["company"] = company
        if email is not None:
            params["email"] = email
        if college is not None:
            params["college"] = college
        if personal_note is not None:
            params["personal_note"] = personal_note
        return await self.call("set_qq_profile", params)  # type: ignore[attr-defined]

    async def set_qq_avatar(self, file: str) -> dict[str, Any]:
        """设置 QQ 头像。file: 图片路径或 URL。"""
        return await self.call("set_qq_avatar", {"file": file})  # type: ignore[attr-defined]

    async def set_self_longnick(self, long_nick: str) -> dict[str, Any]:
        """设置个性签名。"""
        return await self.call("set_self_longnick", {"longNick": long_nick})  # type: ignore[attr-defined]

    # ------------------------------------------------------------------
    # 在线状态
    # ------------------------------------------------------------------

    async def set_online_status(
        self,
        status: int,
        ext_status: int = 0,
        battery_status: int = 0,
    ) -> dict[str, Any]:
        """设置在线状态。

        status: 10=在线, 30=离开, 40=隐身, 50=忙碌, 60=Q我吧, 70=请勿打扰
        """
        return await self.call("set_online_status", {  # type: ignore[attr-defined]
            "status": status,
            "ext_status": ext_status,
            "battery_status": battery_status,
        })

    async def set_input_status(self, user_id: int, event_type: int) -> dict[str, Any]:
        """设置输入状态。event_type: 0=停止输入, 1=正在输入。"""
        return await self.call("set_input_status", {  # type: ignore[attr-defined]
            "user_id": user_id,
            "event_type": event_type,
        })

    async def get_online_clients(self) -> dict[str, Any]:
        """获取当前账号在线客户端列表。"""
        return await self.call_data("get_online_clients")  # type: ignore[attr-defined]

    async def nc_get_user_status(self, user_id: int) -> dict[str, Any]:
        """获取陌生人在线状态。"""
        return await self.call_data("nc_get_user_status", {"user_id": user_id})  # type: ignore[attr-defined]

    # ------------------------------------------------------------------
    # 好友管理
    # ------------------------------------------------------------------

    async def get_friend_list(self) -> list[dict[str, Any]]:
        """获取好友列表。"""
        return await self.call_data("get_friend_list") or []  # type: ignore[attr-defined]

    async def get_friends_with_category(self) -> dict[str, Any]:
        """获取好友分类列表。"""
        return await self.call_data("get_friends_with_category")  # type: ignore[attr-defined]

    async def get_stranger_info(self, user_id: int, no_cache: bool = False) -> dict[str, Any]:
        """获取陌生人信息。"""
        return await self.call_data("get_stranger_info", {  # type: ignore[attr-defined]
            "user_id": user_id,
            "no_cache": no_cache,
        })

    async def delete_friend(self, user_id: int) -> dict[str, Any]:
        """删除好友。"""
        return await self.call("delete_friend", {"user_id": user_id})  # type: ignore[attr-defined]

    async def get_profile_like(self) -> dict[str, Any]:
        """获取自身点赞列表。"""
        return await self.call_data("get_profile_like")  # type: ignore[attr-defined]

    # ------------------------------------------------------------------
    # 凭证
    # ------------------------------------------------------------------

    async def get_cookies(self, domain: str = "") -> dict[str, Any]:
        """获取 Cookies。"""
        params = {"domain": domain} if domain else {}
        return await self.call_data("get_cookies", params, timeout=40.0)  # type: ignore[attr-defined]

    async def get_csrf_token(self) -> dict[str, Any]:
        """获取 CSRF Token。"""
        return await self.call_data("get_csrf_token")  # type: ignore[attr-defined]

    async def get_credentials(self, domain: str = "") -> dict[str, Any]:
        """获取 QQ 相关接口凭证。"""
        params = {"domain": domain} if domain else {}
        return await self.call_data("get_credentials", params)  # type: ignore[attr-defined]

    # ------------------------------------------------------------------
    # 收藏与表情
    # ------------------------------------------------------------------

    async def create_collection(self, brief: str, content: str) -> dict[str, Any]:
        """创建文本收藏。"""
        return await self.call("create_collection", {  # type: ignore[attr-defined]
            "brief": brief,
            "content": content,
        })

    async def get_collection_list(self) -> dict[str, Any]:
        """获取收藏列表。"""
        return await self.call_data("get_collection_list")  # type: ignore[attr-defined]

    async def fetch_custom_face(self, count: int = 48) -> dict[str, Any]:
        """获取收藏表情。"""
        return await self.call_data("fetch_custom_face", {"count": count})  # type: ignore[attr-defined]

    # ------------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------------

    async def ocr_image(self, image: str) -> dict[str, Any]:
        """图片 OCR。image: 图片 URL 或 base64。"""
        return await self.call_data("ocr_image", {"image": image})  # type: ignore[attr-defined]

    async def translate_en2zh(self, words: list[str]) -> dict[str, Any]:
        """英译中翻译。"""
        return await self.call_data("translate_en2zh", {"words": words})  # type: ignore[attr-defined]

    async def get_robot_uin_range(self) -> dict[str, Any]:
        """获取机器人 QQ 号区间。"""
        return await self.call_data("get_robot_uin_range")  # type: ignore[attr-defined]
