"""群组相关 API Mixin

覆盖：群信息查询、群管理（踢人/禁言/管理员/名片/群名/头衔）、
群公告、精华消息、群签到、群系统消息、请求处理等。
"""

from __future__ import annotations

from typing import Any


class GroupApiMixin:
    """群组相关 API。"""

    # ------------------------------------------------------------------
    # 群信息查询
    # ------------------------------------------------------------------

    async def get_group_info(self, group_id: int, no_cache: bool = False) -> dict[str, Any]:
        """获取群基本信息。"""
        return await self.call_data("get_group_info", {  # type: ignore[attr-defined]
            "group_id": group_id,
            "no_cache": no_cache,
        })

    async def get_group_list(self, no_cache: bool = False) -> list[dict[str, Any]]:
        """获取群列表。"""
        return await self.call_data("get_group_list", {"no_cache": no_cache}) or []  # type: ignore[attr-defined]

    async def get_group_info_ex(self, group_id: int) -> dict[str, Any]:
        """获取群组额外信息。"""
        return await self.call_data("get_group_info_ex", {"group_id": group_id})  # type: ignore[attr-defined]

    async def get_group_member_info(
        self,
        group_id: int,
        user_id: int,
        no_cache: bool = False,
    ) -> dict[str, Any]:
        """获取群成员信息。"""
        return await self.call_data("get_group_member_info", {  # type: ignore[attr-defined]
            "group_id": group_id,
            "user_id": user_id,
            "no_cache": no_cache,
        })

    async def get_group_member_list(self, group_id: int, no_cache: bool = False) -> list[dict[str, Any]]:
        """获取群成员列表。"""
        return await self.call_data("get_group_member_list", {  # type: ignore[attr-defined]
            "group_id": group_id,
            "no_cache": no_cache,
        }) or []

    async def get_group_honor_info(self, group_id: int, honor_type: str = "all") -> dict[str, Any]:
        """获取群荣誉信息。honor_type: all/talkative/performer/legend/strong_new_king/emotion。"""
        return await self.call_data("get_group_honor_info", {  # type: ignore[attr-defined]
            "group_id": group_id,
            "type": honor_type,
        })

    async def get_group_shut_list(self, group_id: int) -> list[dict[str, Any]]:
        """获取群聊被禁言用户列表。"""
        return await self.call_data("get_group_shut_list", {"group_id": group_id}) or []  # type: ignore[attr-defined]

    async def get_group_at_all_remain(self, group_id: int) -> dict[str, Any]:
        """获取群 @全体成员 剩余次数。"""
        return await self.call_data("get_group_at_all_remain", {"group_id": group_id})  # type: ignore[attr-defined]

    # ------------------------------------------------------------------
    # 群管理操作
    # ------------------------------------------------------------------

    async def set_group_kick(
        self,
        group_id: int,
        user_id: int,
        reject_add_request: bool = False,
    ) -> dict[str, Any]:
        """群组踢人。"""
        return await self.call("set_group_kick", {  # type: ignore[attr-defined]
            "group_id": group_id,
            "user_id": user_id,
            "reject_add_request": reject_add_request,
        })

    async def set_group_ban(
        self,
        group_id: int,
        user_id: int,
        duration: int = 1800,
    ) -> dict[str, Any]:
        """群组单人禁言。duration 单位秒，0 为解除禁言。"""
        return await self.call("set_group_ban", {  # type: ignore[attr-defined]
            "group_id": group_id,
            "user_id": user_id,
            "duration": duration,
        })

    async def set_group_whole_ban(self, group_id: int, enable: bool = True) -> dict[str, Any]:
        """群组全员禁言。"""
        return await self.call("set_group_whole_ban", {  # type: ignore[attr-defined]
            "group_id": group_id,
            "enable": enable,
        })

    async def set_group_admin(self, group_id: int, user_id: int, enable: bool = True) -> dict[str, Any]:
        """设置/取消群管理员。"""
        return await self.call("set_group_admin", {  # type: ignore[attr-defined]
            "group_id": group_id,
            "user_id": user_id,
            "enable": enable,
        })

    async def set_group_card(self, group_id: int, user_id: int, card: str = "") -> dict[str, Any]:
        """设置群名片（群备注）。card 为空则删除群名片。"""
        return await self.call("set_group_card", {  # type: ignore[attr-defined]
            "group_id": group_id,
            "user_id": user_id,
            "card": card,
        })

    async def set_group_name(self, group_id: int, group_name: str) -> dict[str, Any]:
        """设置群名。"""
        return await self.call("set_group_name", {  # type: ignore[attr-defined]
            "group_id": group_id,
            "group_name": group_name,
        })

    async def set_group_leave(self, group_id: int, is_dismiss: bool = False) -> dict[str, Any]:
        """退出群组。is_dismiss: 如果是群主则解散群。"""
        return await self.call("set_group_leave", {  # type: ignore[attr-defined]
            "group_id": group_id,
            "is_dismiss": is_dismiss,
        })

    async def set_group_special_title(
        self,
        group_id: int,
        user_id: int,
        special_title: str = "",
        duration: int = -1,
    ) -> dict[str, Any]:
        """设置群组专属头衔。duration=-1 表示永久。"""
        return await self.call("set_group_special_title", {  # type: ignore[attr-defined]
            "group_id": group_id,
            "user_id": user_id,
            "special_title": special_title,
            "duration": duration,
        })

    async def set_group_portrait(self, group_id: int, file: str) -> dict[str, Any]:
        """设置群头像。file: 图片路径或 URL。"""
        return await self.call("set_group_portrait", {  # type: ignore[attr-defined]
            "group_id": group_id,
            "file": file,
        })

    # ------------------------------------------------------------------
    # 群签到
    # ------------------------------------------------------------------

    async def set_group_sign(self, group_id: int) -> dict[str, Any]:
        """群签到。"""
        return await self.call("set_group_sign", {"group_id": group_id})  # type: ignore[attr-defined]

    async def send_group_sign(self, group_id: int) -> dict[str, Any]:
        """群打卡（同 set_group_sign）。"""
        return await self.call("send_group_sign", {"group_id": group_id})  # type: ignore[attr-defined]

    # ------------------------------------------------------------------
    # 群公告
    # ------------------------------------------------------------------

    async def send_group_notice(
        self,
        group_id: int,
        content: str,
        image: str = "",
    ) -> dict[str, Any]:
        """发送群公告。"""
        params: dict[str, Any] = {"group_id": group_id, "content": content}
        if image:
            params["image"] = image
        return await self.call("_send_group_notice", params)  # type: ignore[attr-defined]

    async def get_group_notice(self, group_id: int) -> list[dict[str, Any]]:
        """获取群公告列表。"""
        return await self.call_data("_get_group_notice", {"group_id": group_id}) or []  # type: ignore[attr-defined]

    async def delete_group_notice(self, group_id: int, notice_id: str) -> dict[str, Any]:
        """删除群公告。"""
        return await self.call("_del_group_notice", {  # type: ignore[attr-defined]
            "group_id": group_id,
            "notice_id": notice_id,
        })

    # ------------------------------------------------------------------
    # 精华消息
    # ------------------------------------------------------------------

    async def set_essence_msg(self, message_id: int) -> dict[str, Any]:
        """设置精华消息。"""
        return await self.call("set_essence_msg", {"message_id": message_id})  # type: ignore[attr-defined]

    async def delete_essence_msg(self, message_id: int) -> dict[str, Any]:
        """移出精华消息。"""
        return await self.call("delete_essence_msg", {"message_id": message_id})  # type: ignore[attr-defined]

    async def get_essence_msg_list(self, group_id: int) -> list[dict[str, Any]]:
        """获取精华消息列表。"""
        return await self.call_data("get_essence_msg_list", {"group_id": group_id}) or []  # type: ignore[attr-defined]

    # ------------------------------------------------------------------
    # 群系统消息与请求
    # ------------------------------------------------------------------

    async def get_group_system_msg(self) -> dict[str, Any]:
        """获取群系统消息（包含加群请求/邀请）。"""
        return await self.call_data("get_group_system_msg")  # type: ignore[attr-defined]

    async def get_group_ignore_add_request(self) -> dict[str, Any]:
        """获取群组忽略的加群通知。"""
        return await self.call_data("get_group_ignore_add_request")  # type: ignore[attr-defined]

    async def set_friend_add_request(self, flag: str, approve: bool = True, remark: str = "") -> dict[str, Any]:
        """处理加好友请求。"""
        return await self.call("set_friend_add_request", {  # type: ignore[attr-defined]
            "flag": flag,
            "approve": approve,
            "remark": remark,
        })

    async def set_group_add_request(
        self,
        flag: str,
        approve: bool = True,
        reason: str = "",
    ) -> dict[str, Any]:
        """处理加群请求/邀请。"""
        return await self.call("set_group_add_request", {  # type: ignore[attr-defined]
            "flag": flag,
            "approve": approve,
            "reason": reason,
        })
