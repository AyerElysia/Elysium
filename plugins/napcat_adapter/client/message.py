"""消息相关 API Mixin

覆盖：消息收发、历史记录、转发、已读标记、互动（点赞/表情回应/戳一戳）、AI语音等。
"""

from __future__ import annotations

from typing import Any


class MessageApiMixin:
    """消息相关 API。"""

    # ------------------------------------------------------------------
    # 消息发送
    # ------------------------------------------------------------------

    async def send_private_msg(
        self,
        user_id: int,
        message: list[dict[str, Any]] | str,
        auto_escape: bool = False,
    ) -> dict[str, Any]:
        """发送私聊消息。"""
        return await self.call("send_private_msg", {  # type: ignore[attr-defined]
            "user_id": user_id,
            "message": message,
            "auto_escape": auto_escape,
        })

    async def send_group_msg(
        self,
        group_id: int,
        message: list[dict[str, Any]] | str,
        auto_escape: bool = False,
    ) -> dict[str, Any]:
        """发送群消息。"""
        return await self.call("send_group_msg", {  # type: ignore[attr-defined]
            "group_id": group_id,
            "message": message,
            "auto_escape": auto_escape,
        })

    async def send_msg(
        self,
        message: list[dict[str, Any]] | str,
        user_id: int | None = None,
        group_id: int | None = None,
        auto_escape: bool = False,
    ) -> dict[str, Any]:
        """发送消息（自动判断私聊/群聊）。"""
        params: dict[str, Any] = {"message": message, "auto_escape": auto_escape}
        if user_id:
            params["user_id"] = user_id
        if group_id:
            params["group_id"] = group_id
        return await self.call("send_msg", params)  # type: ignore[attr-defined]

    async def delete_msg(self, message_id: int) -> dict[str, Any]:
        """撤回消息。"""
        return await self.call("delete_msg", {"message_id": message_id})  # type: ignore[attr-defined]

    # ------------------------------------------------------------------
    # 消息查询
    # ------------------------------------------------------------------

    async def get_msg(self, message_id: int | str) -> dict[str, Any]:
        """获取消息详情。"""
        return await self.call_data("get_msg", {"message_id": message_id})  # type: ignore[attr-defined]

    async def get_forward_msg(self, message_id: str) -> dict[str, Any]:
        """获取合并转发消息内容。"""
        return await self.call_data("get_forward_msg", {"message_id": message_id})  # type: ignore[attr-defined]

    async def get_group_msg_history(
        self,
        group_id: int,
        message_seq: int | None = None,
        count: int = 20,
        reverse_order: bool = False,
    ) -> dict[str, Any]:
        """获取群消息历史记录。"""
        params: dict[str, Any] = {"group_id": group_id, "count": count, "reverseOrder": reverse_order}
        if message_seq is not None:
            params["message_seq"] = message_seq
        return await self.call_data("get_group_msg_history", params)  # type: ignore[attr-defined]

    async def get_friend_msg_history(
        self,
        user_id: int,
        message_seq: int | None = None,
        count: int = 20,
        reverse_order: bool = False,
    ) -> dict[str, Any]:
        """获取私聊消息历史记录。"""
        params: dict[str, Any] = {"user_id": user_id, "count": count, "reverseOrder": reverse_order}
        if message_seq is not None:
            params["message_seq"] = message_seq
        return await self.call_data("get_friend_msg_history", params)  # type: ignore[attr-defined]

    async def get_recent_contact(self, count: int = 10) -> dict[str, Any]:
        """获取最近的聊天记录。"""
        return await self.call_data("get_recent_contact", {"count": count})  # type: ignore[attr-defined]

    # ------------------------------------------------------------------
    # 转发消息
    # ------------------------------------------------------------------

    async def send_forward_msg(
        self,
        messages: list[dict[str, Any]],
        user_id: int | None = None,
        group_id: int | None = None,
    ) -> dict[str, Any]:
        """发送合并转发消息。"""
        params: dict[str, Any] = {"messages": messages}
        if user_id:
            params["user_id"] = user_id
        if group_id:
            params["group_id"] = group_id
        return await self.call("send_forward_msg", params)  # type: ignore[attr-defined]

    async def send_group_forward_msg(
        self,
        group_id: int,
        messages: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """发送群聊合并转发消息。"""
        return await self.call("send_group_forward_msg", {  # type: ignore[attr-defined]
            "group_id": group_id,
            "messages": messages,
        })

    async def send_private_forward_msg(
        self,
        user_id: int,
        messages: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """发送私聊合并转发消息。"""
        return await self.call("send_private_forward_msg", {  # type: ignore[attr-defined]
            "user_id": user_id,
            "messages": messages,
        })

    async def forward_friend_single_msg(self, message_id: int, user_id: int) -> dict[str, Any]:
        """转发单条消息到私聊。"""
        return await self.call("forward_friend_single_msg", {  # type: ignore[attr-defined]
            "message_id": message_id,
            "user_id": user_id,
        })

    async def forward_group_single_msg(self, message_id: int, group_id: int) -> dict[str, Any]:
        """转发单条消息到群聊。"""
        return await self.call("forward_group_single_msg", {  # type: ignore[attr-defined]
            "message_id": message_id,
            "group_id": group_id,
        })

    # ------------------------------------------------------------------
    # 已读标记
    # ------------------------------------------------------------------

    async def mark_msg_as_read(self, message_id: int) -> dict[str, Any]:
        """标记消息已读。"""
        return await self.call("mark_msg_as_read", {"message_id": message_id})  # type: ignore[attr-defined]

    async def mark_private_msg_as_read(self, user_id: int) -> dict[str, Any]:
        """标记私聊消息已读。"""
        return await self.call("mark_private_msg_as_read", {"user_id": user_id})  # type: ignore[attr-defined]

    async def mark_group_msg_as_read(self, group_id: int) -> dict[str, Any]:
        """标记群聊消息已读。"""
        return await self.call("mark_group_msg_as_read", {"group_id": group_id})  # type: ignore[attr-defined]

    async def mark_all_as_read(self) -> dict[str, Any]:
        """标记所有消息为已读。"""
        return await self.call("_mark_all_as_read")  # type: ignore[attr-defined]

    # ------------------------------------------------------------------
    # 互动：点赞、表情回应、戳一戳
    # ------------------------------------------------------------------

    async def send_like(self, user_id: int, times: int = 1) -> dict[str, Any]:
        """发送好友赞。"""
        return await self.call("send_like", {"user_id": user_id, "times": times})  # type: ignore[attr-defined]

    async def set_msg_emoji_like(
        self,
        message_id: int,
        emoji_id: str,
        set_like: bool = True,
    ) -> dict[str, Any]:
        """设置消息的表情回应。"""
        return await self.call("set_msg_emoji_like", {  # type: ignore[attr-defined]
            "message_id": message_id,
            "emoji_id": emoji_id,
            "set": set_like,
        })

    async def fetch_emoji_like(
        self,
        message_id: int,
        emoji_id: str,
        emoji_type: str = "1",
    ) -> dict[str, Any]:
        """拉取表情回应列表。"""
        return await self.call_data("fetch_emoji_like", {  # type: ignore[attr-defined]
            "message_id": message_id,
            "emojiId": emoji_id,
            "emojiType": emoji_type,
        })

    async def send_poke(self, user_id: int, group_id: int | None = None) -> dict[str, Any]:
        """戳一戳（群聊/私聊通用）。"""
        params: dict[str, Any] = {"user_id": user_id}
        if group_id:
            params["group_id"] = group_id
        return await self.call("send_poke", params)  # type: ignore[attr-defined]

    async def friend_poke(self, user_id: int) -> dict[str, Any]:
        """私聊戳一戳。"""
        return await self.call("friend_poke", {"user_id": user_id})  # type: ignore[attr-defined]

    async def group_poke(self, group_id: int, user_id: int) -> dict[str, Any]:
        """群聊戳一戳。"""
        return await self.call("group_poke", {"group_id": group_id, "user_id": user_id})  # type: ignore[attr-defined]

    # ------------------------------------------------------------------
    # AI 语音
    # ------------------------------------------------------------------

    async def get_ai_record(
        self,
        character: str,
        text: str,
        group_id: int | None = None,
    ) -> dict[str, Any]:
        """AI 文字转语音。"""
        params: dict[str, Any] = {"character": character, "text": text}
        if group_id:
            params["group_id"] = group_id
        return await self.call_data("get_ai_record", params)  # type: ignore[attr-defined]

    async def get_ai_characters(self, group_id: int | None = None) -> dict[str, Any]:
        """获取 AI 语音角色列表。"""
        params: dict[str, Any] = {}
        if group_id:
            params["group_id"] = group_id
        return await self.call_data("get_ai_characters", params)  # type: ignore[attr-defined]

    async def send_group_ai_record(
        self,
        group_id: int,
        character: str,
        text: str,
    ) -> dict[str, Any]:
        """群聊发送 AI 语音。"""
        return await self.call("send_group_ai_record", {  # type: ignore[attr-defined]
            "group_id": group_id,
            "character": character,
            "text": text,
        })

    # ------------------------------------------------------------------
    # 分享与卡片
    # ------------------------------------------------------------------

    async def ark_share_peer(self, user_id: int, group_id: int | None = None) -> dict[str, Any]:
        """推荐联系人/群聊（Ark 分享）。"""
        params: dict[str, Any] = {"user_id": user_id}
        if group_id:
            params["group_id"] = group_id
        return await self.call("ArkSharePeer", params)  # type: ignore[attr-defined]

    async def ark_share_group(self, group_id: int) -> dict[str, Any]:
        """推荐群聊（Ark 分享）。"""
        return await self.call("ArkShareGroup", {"group_id": group_id})  # type: ignore[attr-defined]

    async def get_mini_app_ark(
        self,
        app: str,
        title: str,
        desc: str,
        jump_url: str,
        preview_url: str = "",
    ) -> dict[str, Any]:
        """签名小程序卡片。"""
        return await self.call_data("get_mini_app_ark", {  # type: ignore[attr-defined]
            "app": app,
            "title": title,
            "desc": desc,
            "jumpUrl": jump_url,
            "previewUrl": preview_url,
        })
