"""请求事件处理器

处理 request 事件：
- request.friend: 好友申请
- request.group (sub_type=add): 加群申请
- request.group (sub_type=invite): 邀请入群

所有请求事件都构建 MessageEnvelope 推送给核心，让生命引擎统一决策。
"""

from __future__ import annotations

import hashlib
import time
from typing import TYPE_CHECKING, Any

from src.app.plugin_system.api.log_api import get_logger
from src.core.transport.wire import MessageBuilder, SegPayload, UserInfoPayload

from ..utils.cache import GROUP_INFO_TTL, STRANGER_INFO_TTL, get_cached, set_cached
from ..utils.constants import ACCEPT_FORMAT, RequestType

if TYPE_CHECKING:
    from ..client import NapCatClient
    from ..config import NapcatAdapterConfig

logger = get_logger("napcat_adapter")


class RequestEventHandler:
    """处理 NapCat 请求事件（好友申请、加群申请、邀请入群）。"""

    def __init__(self, client: "NapCatClient", get_config: Any) -> None:
        self._client = client
        self._get_config = get_config

    def _config(self) -> "NapcatAdapterConfig | None":
        return self._get_config()

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------

    async def handle(self, raw: dict[str, Any]) -> Any:
        """处理 request 事件。"""
        request_type = raw.get("request_type")

        match request_type:
            case RequestType.friend:
                return await self._handle_friend_request(raw)
            case RequestType.group:
                sub_type = raw.get("sub_type")
                if sub_type == RequestType.Group.add:
                    return await self._handle_group_add_request(raw)
                elif sub_type == RequestType.Group.invite:
                    return await self._handle_group_invite_request(raw)
                else:
                    logger.debug(f"未知 group request 子类型: {sub_type}")
                    return None
            case _:
                logger.debug(f"未知 request 类型: {request_type}")
                return None

    # ------------------------------------------------------------------
    # 好友申请
    # ------------------------------------------------------------------

    async def _handle_friend_request(self, raw: dict) -> Any:
        """处理好友申请。"""
        user_id = raw.get("user_id")
        comment = raw.get("comment", "")
        flag = raw.get("flag", "")
        request_time = time.time()

        # 获取申请者信息
        info = await self._get_stranger_info(user_id)
        nickname = info.get("nickname", "QQ用户") if info else "QQ用户"

        logger.info(f"收到好友申请: user_id={user_id}, nickname={nickname}, comment={comment}")

        # 构建消息段
        comment_text = f"\n验证消息: {comment}" if comment else ""
        seg: SegPayload = {
            "type": "text",
            "data": f"[好友申请] {nickname}({user_id}) 请求添加你为好友{comment_text}",
        }

        user_info: UserInfoPayload = {
            "platform": "qq",
            "user_id": str(user_id),
            "user_nickname": nickname,
        }

        # 构建请求配置（供核心决策）
        request_config = {
            "is_request": True,
            "request_type": "friend",
            "flag": flag,
            "user_id": user_id,
            "comment": comment,
        }

        return self._build_request_envelope(
            raw, request_config, seg, user_info, request_time
        )

    # ------------------------------------------------------------------
    # 加群申请
    # ------------------------------------------------------------------

    async def _handle_group_add_request(self, raw: dict) -> Any:
        """处理加群申请。"""
        group_id = raw.get("group_id")
        user_id = raw.get("user_id")
        comment = raw.get("comment", "")
        flag = raw.get("flag", "")
        request_time = time.time()

        # 获取申请者信息
        info = await self._get_stranger_info(user_id)
        nickname = info.get("nickname", "QQ用户") if info else "QQ用户"

        # 获取群信息
        group_info = await self._get_group_info(group_id)
        group_name = group_info.get("group_name", str(group_id)) if group_info else str(group_id)

        logger.info(f"收到加群申请: group={group_name}, user_id={user_id}, nickname={nickname}, comment={comment}")

        comment_text = f"\n验证消息: {comment}" if comment else ""
        seg: SegPayload = {
            "type": "text",
            "data": f"[加群申请] {nickname}({user_id}) 申请加入群 {group_name}({group_id}){comment_text}",
        }

        user_info: UserInfoPayload = {
            "platform": "qq",
            "user_id": str(user_id),
            "user_nickname": nickname,
        }

        request_config = {
            "is_request": True,
            "request_type": "group_add",
            "flag": flag,
            "user_id": user_id,
            "group_id": group_id,
            "group_name": group_name,
            "comment": comment,
        }

        return self._build_request_envelope(
            raw, request_config, seg, user_info, request_time, group_id=group_id
        )

    # ------------------------------------------------------------------
    # 邀请入群
    # ------------------------------------------------------------------

    async def _handle_group_invite_request(self, raw: dict) -> Any:
        """处理邀请入群请求。"""
        group_id = raw.get("group_id")
        user_id = raw.get("user_id")  # 邀请者
        flag = raw.get("flag", "")
        request_time = time.time()

        # 获取邀请者信息
        info = await self._get_stranger_info(user_id)
        nickname = info.get("nickname", "QQ用户") if info else "QQ用户"

        # 获取群信息
        group_info = await self._get_group_info(group_id)
        group_name = group_info.get("group_name", str(group_id)) if group_info else str(group_id)

        logger.info(f"收到入群邀请: group={group_name}, inviter={nickname}({user_id})")

        seg: SegPayload = {
            "type": "text",
            "data": f"[入群邀请] {nickname}({user_id}) 邀请你加入群 {group_name}({group_id})",
        }

        user_info: UserInfoPayload = {
            "platform": "qq",
            "user_id": str(user_id),
            "user_nickname": nickname,
        }

        request_config = {
            "is_request": True,
            "request_type": "group_invite",
            "flag": flag,
            "user_id": user_id,
            "group_id": group_id,
            "group_name": group_name,
        }

        return self._build_request_envelope(
            raw, request_config, seg, user_info, request_time, group_id=group_id
        )

    # ------------------------------------------------------------------
    # Envelope 构建
    # ------------------------------------------------------------------

    def _build_request_envelope(
        self,
        raw: dict,
        request_config: dict,
        segment: SegPayload,
        user_info: UserInfoPayload,
        request_time: float,
        group_id: Any = None,
    ) -> Any:
        """构建请求事件的 MessageEnvelope。"""
        request_type = raw.get("request_type", "unknown")
        user_id = raw.get("user_id")

        # 唯一 ID
        id_raw = f"request_{request_type}_{user_id}_{group_id}_{request_time}"
        unique_id = "request_" + hashlib.md5(id_raw.encode()).hexdigest()[:16]

        msg_builder = MessageBuilder()
        (
            msg_builder.direction("incoming")
            .message_id(unique_id)
            .timestamp_ms(int(request_time * 1000))
            .from_user(
                user_id=str(user_info.get("user_id", "")),
                platform="qq",
                nickname=user_info.get("user_nickname", ""),
            )
        )

        # 群信息（如果是群相关请求）
        if group_id:
            msg_builder.from_group(group_id=str(group_id), platform="qq", name="")

        msg_builder.format_info(
            content_format=["text", "request"],
            accept_format=ACCEPT_FORMAT,
        )
        msg_builder.seg_list([segment])

        envelope = msg_builder.build()
        envelope["message_info"]["extra"] = request_config
        envelope["message_info"]["message_type"] = "request"
        return envelope

    # ------------------------------------------------------------------
    # 缓存辅助
    # ------------------------------------------------------------------

    async def _get_stranger_info(self, user_id: Any) -> dict | None:
        key = str(user_id)
        cached = await get_cached("stranger_info", key, STRANGER_INFO_TTL)
        if cached:
            return cached
        try:
            data = await self._client.call_data("get_stranger_info", {"user_id": user_id})
            if data:
                await set_cached("stranger_info", key, data)
            return data
        except Exception:
            return None

    async def _get_group_info(self, group_id: Any) -> dict | None:
        key = str(group_id)
        cached = await get_cached("group_info", key, GROUP_INFO_TTL)
        if cached:
            return cached
        try:
            data = await self._client.call_data("get_group_info", {"group_id": group_id})
            if data:
                await set_cached("group_info", key, data)
            return data
        except Exception:
            return None
