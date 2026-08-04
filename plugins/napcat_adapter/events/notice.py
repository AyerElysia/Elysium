"""通知事件处理器（全量覆盖）

处理所有 notice 事件类型：
- friend_add: 新好友通知
- friend_recall: 私聊消息撤回
- group_recall: 群消息撤回
- group_admin: 管理员变动
- group_ban: 群禁言/解除禁言
- group_card: 群名片更新
- group_decrease: 成员减少（退群/被踢/自己被踢）
- group_increase: 成员增加（审批/邀请）
- group_upload: 群文件上传
- group_msg_emoji_like: 表情回应
- essence: 精华消息
- notify: poke / title / profile_like / input_status
- bot_offline: Bot 掉线
"""

from __future__ import annotations

import hashlib
import time
from typing import TYPE_CHECKING, Any

from mofox_wire import MessageBuilder, SegPayload, UserInfoPayload

from src.app.plugin_system.api.log_api import get_logger

from ..utils.cache import (
    GROUP_INFO_TTL,
    MEMBER_INFO_TTL,
    SELF_INFO_TTL,
    STRANGER_INFO_TTL,
    get_cached,
    set_cached,
)
from ..utils.constants import ACCEPT_FORMAT, QQ_FACE, NoticeType, RealMessageType

if TYPE_CHECKING:
    from ..client import NapCatClient
    from ..config import NapcatAdapterConfig

logger = get_logger("napcat_adapter")


class NoticeEventHandler:
    """处理 NapCat 通知事件（全量覆盖）。"""

    def __init__(self, client: NapCatClient, get_config: Any) -> None:
        self._client = client
        self._get_config = get_config
        self._last_poke_time: float = 0.0

    def _config(self) -> NapcatAdapterConfig | None:
        return self._get_config()

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------

    async def handle(self, raw: dict[str, Any]) -> Any:
        """处理 notice 事件。"""
        notice_type = raw.get("notice_type")
        message_time = time.time()

        self_id = raw.get("self_id")
        group_id = raw.get("group_id")
        user_id = raw.get("user_id")
        target_id = raw.get("target_id")

        handled_segment: SegPayload | None = None
        user_info: UserInfoPayload | None = None
        notice_config: dict[str, Any] = {
            "is_notice": True,
            "is_public_notice": False,
            "target_id": target_id,
            "notice_type": notice_type,
            "sub_type": raw.get("sub_type"),
            "provider_raw_identity": self._provider_raw_identity(raw),
        }

        match notice_type:
            # ---- 好友相关 ----
            case NoticeType.friend_add:
                handled_segment, user_info = await self._handle_friend_add(raw)

            case NoticeType.friend_recall:
                handled_segment, user_info = await self._handle_friend_recall(raw)

            # ---- 群消息撤回 ----
            case NoticeType.group_recall:
                handled_segment, user_info = await self._handle_group_recall(raw)

            # ---- 管理员变动 ----
            case NoticeType.group_admin:
                handled_segment, user_info = await self._handle_group_admin(raw)

            # ---- 群禁言 ----
            case NoticeType.group_ban:
                handled_segment, user_info = await self._handle_group_ban(raw)
                if handled_segment:
                    sub_type = raw.get("sub_type")
                    ban_user_id = raw.get("user_id")
                    if ban_user_id == 0:
                        notice_config["notice_type"] = f"group_whole_{sub_type}"
                    else:
                        notice_config["notice_type"] = f"group_{sub_type}"

            # ---- 群名片更新 ----
            case NoticeType.group_card:
                handled_segment, user_info = await self._handle_group_card(raw)

            # ---- 成员减少 ----
            case NoticeType.group_decrease:
                handled_segment, user_info = await self._handle_group_decrease(raw)

            # ---- 成员增加 ----
            case NoticeType.group_increase:
                handled_segment, user_info = await self._handle_group_increase(raw)

            # ---- 群文件上传 ----
            case NoticeType.group_upload:
                if user_id == self_id:
                    logger.debug("忽略 Bot 自己上传文件的通知")
                    return None
                handled_segment, user_info = await self._handle_group_upload(raw)

            # ---- 表情回应 ----
            case NoticeType.group_msg_emoji_like:
                config = self._config()
                if config and not config.features.enable_emoji_like:
                    return None
                if str(user_id) == str(self_id):
                    logger.debug("忽略 Bot 自己贴表情的回声")
                    return None
                handled_segment, user_info = await self._handle_emoji_like(raw)

            # ---- 精华消息 ----
            case NoticeType.essence:
                handled_segment, user_info = await self._handle_essence(raw)

            # ---- notify 子类 ----
            case NoticeType.notify:
                sub_type = raw.get("sub_type")
                match sub_type:
                    case NoticeType.Notify.poke:
                        config = self._config()
                        if config and not config.features.enable_poke:
                            return None
                        handled_segment, user_info = await self._handle_poke(raw)
                    case NoticeType.Notify.title:
                        handled_segment, user_info = await self._handle_title_notify(raw)
                    case NoticeType.Notify.profile_like:
                        handled_segment, user_info = await self._handle_profile_like(raw)
                    case NoticeType.Notify.input_status:
                        logger.debug(f"用户 {user_id} 正在输入...")
                        handled_segment, user_info = self._fallback_notice(
                            raw,
                            text="用户输入状态发生变化",
                        )
                    case _:
                        logger.debug(f"未知 notify 子类型: {sub_type}")
                        handled_segment, user_info = self._fallback_notice(raw)

            # ---- Bot 掉线 ----
            case NoticeType.bot_offline:
                reason = raw.get("reason", "未知原因")
                logger.error(f"Bot {self_id} 收到掉线通知，原因: {reason}")
                handled_segment, user_info = self._fallback_notice(
                    raw,
                    text=f"Bot 连接状态变化：{reason}",
                )

            case _:
                logger.debug(f"未处理的 notice 类型: {notice_type}")
                handled_segment, user_info = self._fallback_notice(raw)

        if not handled_segment or not user_info:
            return None

        # 生成 text_description
        self._fill_text_description(notice_config, handled_segment, user_info)

        # 构建 envelope
        return await self._build_notice_envelope(
            raw, notice_config, handled_segment, user_info, message_time
        )

    # ------------------------------------------------------------------
    # 各通知类型处理
    # ------------------------------------------------------------------

    async def _handle_friend_add(self, raw: dict) -> tuple[SegPayload | None, UserInfoPayload | None]:
        """新好友通知。"""
        user_id = raw.get("user_id")
        info = await self._get_stranger_info(user_id)
        nickname = info.get("nickname", "QQ用户") if info else "QQ用户"

        user_info: UserInfoPayload = {
            "platform": "qq",
            "user_id": str(user_id),
            "user_nickname": nickname,
        }
        seg: SegPayload = {"type": "text", "data": f"{nickname} 成为了你的新好友！"}
        return seg, user_info

    async def _handle_friend_recall(self, raw: dict) -> tuple[SegPayload | None, UserInfoPayload | None]:
        """私聊消息撤回。"""
        user_id = raw.get("user_id")
        message_id = raw.get("message_id")

        info = await self._get_stranger_info(user_id)
        nickname = info.get("nickname", "QQ用户") if info else "QQ用户"

        # 尝试获取撤回消息内容
        recalled_content = await self._get_recalled_message_preview(message_id)

        user_info: UserInfoPayload = {
            "platform": "qq",
            "user_id": str(user_id),
            "user_nickname": nickname,
        }
        content_text = f"（内容：{recalled_content}）" if recalled_content else ""
        seg: SegPayload = {"type": "text", "data": f"{nickname} 撤回了一条私聊消息{content_text}"}
        return seg, user_info

    async def _handle_group_recall(self, raw: dict) -> tuple[SegPayload | None, UserInfoPayload | None]:
        """群消息撤回。"""
        group_id = raw.get("group_id")
        user_id = raw.get("user_id")
        operator_id = raw.get("operator_id")
        message_id = raw.get("message_id")

        member_info = await self._get_member_info(group_id, user_id)
        nickname = member_info.get("nickname", "QQ用户") if member_info else "QQ用户"

        recalled_content = await self._get_recalled_message_preview(message_id)

        user_info: UserInfoPayload = {
            "platform": "qq",
            "user_id": str(user_id),
            "user_nickname": nickname,
            "user_cardname": member_info.get("card", "") if member_info else "",
        }

        if operator_id and operator_id != user_id:
            operator_info = await self._get_member_info(group_id, operator_id)
            operator_name = operator_info.get("nickname", "管理员") if operator_info else "管理员"
            content_text = f"（内容：{recalled_content}）" if recalled_content else ""
            text = f"{operator_name} 撤回了 {nickname} 的群消息{content_text}"
        else:
            content_text = f"（内容：{recalled_content}）" if recalled_content else ""
            text = f"{nickname} 撤回了一条群消息{content_text}"

        seg: SegPayload = {"type": "text", "data": text}
        return seg, user_info

    async def _handle_group_admin(self, raw: dict) -> tuple[SegPayload | None, UserInfoPayload | None]:
        """管理员变动。"""
        group_id = raw.get("group_id")
        user_id = raw.get("user_id")
        sub_type = raw.get("sub_type")  # set / unset

        member_info = await self._get_member_info(group_id, user_id)
        nickname = member_info.get("nickname", "QQ用户") if member_info else "QQ用户"

        user_info: UserInfoPayload = {
            "platform": "qq",
            "user_id": str(user_id),
            "user_nickname": nickname,
            "user_cardname": member_info.get("card", "") if member_info else "",
        }

        action_text = "被设置为管理员" if sub_type == NoticeType.GroupAdmin.set else "被取消了管理员"
        seg: SegPayload = {"type": "text", "data": f"{nickname} {action_text}"}
        return seg, user_info

    async def _handle_group_ban(self, raw: dict) -> tuple[SegPayload | None, UserInfoPayload | None]:
        """群禁言/解除禁言。"""
        group_id = raw.get("group_id")
        operator_id = raw.get("operator_id")
        user_id = raw.get("user_id")
        duration = raw.get("duration", 0)
        sub_type = raw.get("sub_type")

        operator_info = await self._get_member_info(group_id, operator_id)
        operator_name = operator_info.get("nickname", "QQ用户") if operator_info else "QQ用户"

        user_info: UserInfoPayload = {
            "platform": "qq",
            "user_id": str(operator_id),
            "user_nickname": operator_name,
            "user_cardname": operator_info.get("card", "") if operator_info else "",
        }

        if sub_type == NoticeType.GroupBan.ban:
            if user_id == 0:
                # 全体禁言
                seg: SegPayload = {
                    "type": "notify",
                    "data": {"sub_type": "whole_ban"},
                }
            else:
                banned_info = await self._get_member_info(group_id, user_id)
                banned_name = banned_info.get("nickname", "QQ用户") if banned_info else "QQ用户"
                seg = {
                    "type": "notify",
                    "data": {
                        "sub_type": "ban",
                        "duration": duration,
                        "banned_user_info": {
                            "platform": "qq",
                            "user_id": str(user_id),
                            "user_nickname": banned_name,
                        },
                    },
                }
        else:  # lift_ban
            if user_id == 0:
                seg = {"type": "notify", "data": {"sub_type": "whole_lift_ban"}}
            else:
                lifted_info = await self._get_member_info(group_id, user_id)
                lifted_name = lifted_info.get("nickname", "QQ用户") if lifted_info else "QQ用户"
                seg = {
                    "type": "notify",
                    "data": {
                        "sub_type": "lift_ban",
                        "lifted_user_info": {
                            "platform": "qq",
                            "user_id": str(user_id),
                            "user_nickname": lifted_name,
                        },
                    },
                }

        return seg, user_info

    async def _handle_group_card(self, raw: dict) -> tuple[SegPayload | None, UserInfoPayload | None]:
        """群名片更新。"""
        group_id = raw.get("group_id")
        user_id = raw.get("user_id")
        card_new = raw.get("card_new", "")
        card_old = raw.get("card_old", "")

        member_info = await self._get_member_info(group_id, user_id)
        nickname = member_info.get("nickname", "QQ用户") if member_info else "QQ用户"

        user_info: UserInfoPayload = {
            "platform": "qq",
            "user_id": str(user_id),
            "user_nickname": nickname,
            "user_cardname": card_new,
        }

        old_text = f"「{card_old}」" if card_old else "无"
        new_text = f"「{card_new}」" if card_new else "无"
        seg: SegPayload = {"type": "text", "data": f"{nickname} 的群名片从 {old_text} 变更为 {new_text}"}
        return seg, user_info

    async def _handle_group_decrease(self, raw: dict) -> tuple[SegPayload | None, UserInfoPayload | None]:
        """成员减少。"""
        group_id = raw.get("group_id")
        user_id = raw.get("user_id")
        operator_id = raw.get("operator_id")
        sub_type = raw.get("sub_type")  # leave / kick / kick_me
        self_id = raw.get("self_id")

        # Bot 自己被踢
        if sub_type == NoticeType.GroupDecrease.kick_me:
            logger.warning(f"Bot 被踢出群 {group_id}")
            user_info: UserInfoPayload = {"platform": "qq", "user_id": str(operator_id or ""), "user_nickname": "管理员"}
            seg: SegPayload = {"type": "text", "data": f"你被移出了群聊 {group_id}"}
            return seg, user_info

        member_info = await self._get_member_info(group_id, user_id)
        nickname = member_info.get("nickname", "QQ用户") if member_info else "QQ用户"

        user_info = {
            "platform": "qq",
            "user_id": str(user_id),
            "user_nickname": nickname,
            "user_cardname": member_info.get("card", "") if member_info else "",
        }

        if sub_type == NoticeType.GroupDecrease.leave:
            text = f"{nickname} 退出了群聊"
        elif sub_type == NoticeType.GroupDecrease.kick:
            operator_info = await self._get_member_info(group_id, operator_id)
            operator_name = operator_info.get("nickname", "管理员") if operator_info else "管理员"
            text = f"{nickname} 被 {operator_name} 移出了群聊"
        else:
            text = f"{nickname} 离开了群聊"

        seg = {"type": "text", "data": text}
        return seg, user_info

    async def _handle_group_increase(self, raw: dict) -> tuple[SegPayload | None, UserInfoPayload | None]:
        """成员增加。"""
        group_id = raw.get("group_id")
        user_id = raw.get("user_id")
        operator_id = raw.get("operator_id")
        sub_type = raw.get("sub_type")  # approve / invite

        member_info = await self._get_member_info(group_id, user_id)
        nickname = member_info.get("nickname", "QQ用户") if member_info else "QQ用户"

        user_info: UserInfoPayload = {
            "platform": "qq",
            "user_id": str(user_id),
            "user_nickname": nickname,
            "user_cardname": member_info.get("card", "") if member_info else "",
        }

        if sub_type == NoticeType.GroupIncrease.invite:
            operator_info = await self._get_member_info(group_id, operator_id)
            operator_name = operator_info.get("nickname", "QQ用户") if operator_info else "QQ用户"
            text = f"{nickname} 被 {operator_name} 邀请加入了群聊"
        else:
            text = f"{nickname} 加入了群聊"

        seg: SegPayload = {"type": "text", "data": text}
        return seg, user_info

    async def _handle_group_upload(self, raw: dict) -> tuple[SegPayload | None, UserInfoPayload | None]:
        """群文件上传。"""
        group_id = raw.get("group_id")
        user_id = raw.get("user_id")
        file_info = raw.get("file", {})

        member_info = await self._get_member_info(group_id, user_id)
        nickname = member_info.get("nickname", "QQ用户") if member_info else "QQ用户"

        user_info: UserInfoPayload = {
            "platform": "qq",
            "user_id": str(user_id),
            "user_nickname": nickname,
            "user_cardname": member_info.get("card", "") if member_info else "",
        }

        file_name = file_info.get("name", "未知文件")
        file_size = file_info.get("size", 0)
        seg: SegPayload = {"type": "text", "data": f"{nickname} 上传了文件: {file_name} (大小: {file_size} 字节)"}
        return seg, user_info

    async def _handle_emoji_like(self, raw: dict) -> tuple[SegPayload | None, UserInfoPayload | None]:
        """表情回应。"""
        group_id = raw.get("group_id")
        user_id = raw.get("user_id")
        message_id = raw.get("message_id")
        likes = raw.get("likes", [])

        member_info = await self._get_member_info(group_id, user_id)
        nickname = member_info.get("nickname", "QQ用户") if member_info else "QQ用户"

        user_info: UserInfoPayload = {
            "platform": "qq",
            "user_id": str(user_id),
            "user_nickname": nickname,
            "user_cardname": member_info.get("card", "") if member_info else "",
        }

        # 获取被回应消息的预览
        preview = await self._get_recalled_message_preview(message_id) or "一条消息"

        emoji_id = ""
        if likes:
            emoji_id = str(likes[0].get("emoji_id", ""))
        emoji_text = QQ_FACE.get(emoji_id, f"[表情{emoji_id}]")

        # 触发事件
        try:
            from src.app.plugin_system.api import event_api
            await event_api.publish_event(
                "napcat.on_received.emoji_like",
                {"message_id": message_id, "emoji_id": emoji_id, "group_id": group_id, "user_id": user_id},
            )
        except Exception:
            pass

        seg: SegPayload = {"type": "text", "data": f"{nickname} 使用表情 {emoji_text} 回应了消息 [{preview}]"}
        return seg, user_info

    async def _handle_essence(self, raw: dict) -> tuple[SegPayload | None, UserInfoPayload | None]:
        """精华消息。"""
        group_id = raw.get("group_id")
        sender_id = raw.get("sender_id")
        operator_id = raw.get("operator_id")
        sub_type = raw.get("sub_type")  # add / delete

        sender_info = await self._get_member_info(group_id, sender_id)
        sender_name = sender_info.get("nickname", "QQ用户") if sender_info else "QQ用户"

        operator_info = await self._get_member_info(group_id, operator_id)
        operator_name = operator_info.get("nickname", "QQ用户") if operator_info else "QQ用户"

        user_info: UserInfoPayload = {
            "platform": "qq",
            "user_id": str(operator_id),
            "user_nickname": operator_name,
        }

        action = "设置为精华消息" if sub_type == NoticeType.Essence.add else "移除了精华消息"
        seg: SegPayload = {"type": "text", "data": f"{operator_name} 将 {sender_name} 的消息{action}"}
        return seg, user_info

    async def _handle_poke(self, raw: dict) -> tuple[SegPayload | None, UserInfoPayload | None]:
        """戳一戳。"""
        self_id = raw.get("self_id")
        user_id = raw.get("user_id")
        target_id = raw.get("target_id")
        group_id = raw.get("group_id")

        self_info = await self._get_self_info()
        if not self_info:
            return None, None

        # Bot 戳别人 → 忽略
        if str(self_id) == str(user_id):
            return None, None

        # 防抖
        if str(self_id) == str(target_id):
            config = self._config()
            debounce = config.features.poke_debounce_seconds if config else 2.0
            now = time.time()
            if now - self._last_poke_time < debounce:
                return None, None
            self._last_poke_time = now

        # 非针对自己的戳一戳
        if str(self_id) != str(target_id):
            config = self._config()
            if config and config.features.ignore_non_self_poke:
                return None, None

        # 获取戳人者信息
        if group_id:
            poker_info = await self._get_member_info(group_id, user_id)
        else:
            poker_info = await self._get_stranger_info(user_id)

        poker_name = poker_info.get("nickname", "QQ用户") if poker_info else "QQ用户"
        poker_card = poker_info.get("card", "") if poker_info else ""

        # 获取被戳者名称
        if str(self_id) == str(target_id):
            target_name = self_info.get("nickname", "")
            display_name = ""
        else:
            if group_id:
                target_info = await self._get_member_info(group_id, target_id)
                target_name = target_info.get("nickname", "QQ用户") if target_info else "QQ用户"
            else:
                return None, None
            display_name = poker_name

        # 解析戳一戳文本
        raw_info = raw.get("raw_info", [])
        first_txt = "戳了戳"
        second_txt = ""
        try:
            if len(raw_info) > 2:
                first_txt = raw_info[2].get("txt", "戳了戳")
            if len(raw_info) > 4:
                second_txt = raw_info[4].get("txt", "")
        except Exception:
            pass

        user_info: UserInfoPayload = {
            "platform": "qq",
            "user_id": str(user_id),
            "user_nickname": poker_name,
            "user_cardname": poker_card,
        }
        seg: SegPayload = {
            "type": "text",
            "data": f"{display_name}{first_txt}{target_name}{second_txt}（这是QQ的一个功能，用于提及某人，但没那么明显）",
        }
        return seg, user_info

    async def _handle_title_notify(self, raw: dict) -> tuple[SegPayload | None, UserInfoPayload | None]:
        """群头衔变更。"""
        group_id = raw.get("group_id")
        user_id = raw.get("user_id")
        title = raw.get("title", "")

        member_info = await self._get_member_info(group_id, user_id)
        nickname = member_info.get("nickname", "QQ用户") if member_info else "QQ用户"

        user_info: UserInfoPayload = {
            "platform": "qq",
            "user_id": str(user_id),
            "user_nickname": nickname,
        }
        seg: SegPayload = {"type": "text", "data": f"{nickname} 的群头衔变更为「{title}」"}
        return seg, user_info

    async def _handle_profile_like(self, raw: dict) -> tuple[SegPayload | None, UserInfoPayload | None]:
        """点赞通知。"""
        user_id = raw.get("user_id")
        operator_id = raw.get("operator_id", user_id)

        info = await self._get_stranger_info(operator_id)
        nickname = info.get("nickname", "QQ用户") if info else "QQ用户"

        user_info: UserInfoPayload = {
            "platform": "qq",
            "user_id": str(operator_id),
            "user_nickname": nickname,
        }
        seg: SegPayload = {"type": "text", "data": f"{nickname} 赞了你的资料卡"}
        return seg, user_info

    # ------------------------------------------------------------------
    # Envelope 构建
    # ------------------------------------------------------------------

    async def _build_notice_envelope(
        self,
        raw: dict,
        notice_config: dict,
        segment: SegPayload,
        user_info: UserInfoPayload,
        message_time: float,
    ) -> Any:
        """构建通知事件的 MessageEnvelope。"""
        notice_type = raw.get("notice_type", "unknown")
        user_id = raw.get("user_id")
        group_id = raw.get("group_id")

        # 唯一 ID
        id_raw = f"notice_{notice_type}_{user_id}_{group_id}_{message_time}"
        unique_id = "notice_" + hashlib.md5(id_raw.encode()).hexdigest()[:16]

        msg_builder = MessageBuilder()
        (
            msg_builder.direction("incoming")
            .message_id(unique_id)
            .timestamp_ms(int(message_time * 1000))
            .from_user(
                user_id=str(user_info.get("user_id", "")),
                platform="qq",
                nickname=user_info.get("user_nickname", ""),
                cardname=user_info.get("user_cardname", ""),
            )
        )

        # 群信息
        if group_id:
            group_info = await self._get_group_info(group_id)
            group_name = group_info.get("group_name", "") if group_info else ""
            msg_builder.from_group(group_id=str(group_id), platform="qq", name=group_name)

        # 格式
        content_format = [segment.get("type", "text")]
        if "notify" not in content_format:
            content_format.append("notify")
        msg_builder.format_info(content_format=content_format, accept_format=ACCEPT_FORMAT)
        msg_builder.seg_list([segment])

        envelope = msg_builder.build()
        envelope["message_info"]["extra"] = notice_config
        envelope["message_info"]["message_type"] = "notice"
        return envelope

    def _fill_text_description(
        self, notice_config: dict, segment: SegPayload, user_info: UserInfoPayload
    ) -> None:
        """填充 text_description 供下游使用。"""
        seg_type = segment.get("type", "text")
        seg_data = segment.get("data", "")

        if seg_type == "text" and isinstance(seg_data, str):
            notice_config["text_description"] = seg_data
        elif seg_type == "notify" and isinstance(seg_data, dict):
            operator_name = user_info.get("user_nickname") or user_info.get("user_cardname") or "某人"
            sub_type = seg_data.get("sub_type", "")
            if sub_type == "ban":
                banned = seg_data.get("banned_user_info") or {}
                banned_name = banned.get("user_nickname") or "某人"
                duration = seg_data.get("duration", 0)
                notice_config["text_description"] = f"{operator_name} 将 {banned_name} 禁言了 {duration} 秒"
            elif sub_type == "whole_ban":
                notice_config["text_description"] = f"{operator_name} 开启了全体禁言"
            elif sub_type == "lift_ban":
                lifted = seg_data.get("lifted_user_info") or {}
                lifted_name = lifted.get("user_nickname") or "某人"
                notice_config["text_description"] = f"{operator_name} 解除了 {lifted_name} 的禁言"
            elif sub_type == "whole_lift_ban":
                notice_config["text_description"] = f"{operator_name} 关闭了全体禁言"
            else:
                notice_config["text_description"] = str(seg_data)
        else:
            notice_config["text_description"] = str(seg_data)

    # ------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------

    @staticmethod
    def _provider_raw_identity(raw: dict[str, Any]) -> dict[str, Any]:
        """Retain provider identifiers without exporting the whole raw payload."""

        keys = (
            "post_type",
            "notice_type",
            "sub_type",
            "time",
            "self_id",
            "group_id",
            "user_id",
            "target_id",
            "operator_id",
            "message_id",
            "sender_id",
        )
        return {
            key: raw[key]
            for key in keys
            if raw.get(key) not in {None, ""}
        }

    @staticmethod
    def _fallback_notice(
        raw: dict[str, Any],
        *,
        text: str | None = None,
    ) -> tuple[SegPayload, UserInfoPayload]:
        """Keep an open provider notice as a non-cognitive transport fact."""

        notice_type = str(raw.get("notice_type") or "unknown")
        sub_type = str(raw.get("sub_type") or "")
        label = f"{notice_type}/{sub_type}" if sub_type else notice_type
        user_id = raw.get("user_id") or raw.get("self_id") or ""
        return (
            {"type": "text", "data": text or f"收到平台通知：{label}"},
            {
                "platform": "qq",
                "user_id": str(user_id),
                "user_nickname": "QQ平台",
            },
        )

    async def _get_recalled_message_preview(self, message_id: Any) -> str | None:
        """尝试获取被撤回消息的内容预览。"""
        if not message_id:
            return None
        try:
            detail = await self._client.call_data("get_msg", {"message_id": message_id})
            if not detail:
                return None
            return await self._extract_message_preview(detail)
        except Exception:
            return None

    async def _extract_message_preview(self, detail: dict, depth: int = 0) -> str:
        """提取消息的可读摘要。"""
        if depth > 3:
            return "..."

        parts: list[str] = []
        for seg in detail.get("message", []):
            seg_type = seg.get("type")
            seg_data = seg.get("data", {})

            if seg_type == RealMessageType.text:
                parts.append(seg_data.get("text", ""))
            elif seg_type == RealMessageType.face:
                face_id = str(seg_data.get("id", ""))
                parts.append(QQ_FACE.get(face_id, f"[表情{face_id}]"))
            elif seg_type == RealMessageType.image:
                parts.append("[图片]" if seg_data.get("sub_type") == 0 else "[表情包]")
            elif seg_type == RealMessageType.at:
                parts.append(f"@{seg_data.get('text') or seg_data.get('qq') or '某人'}")
            elif seg_type == RealMessageType.reply:
                parts.append("[回复]")
            elif seg_type == RealMessageType.forward:
                parts.append("[转发消息]")
            elif seg_type == RealMessageType.file:
                parts.append(f"[文件:{seg_data.get('file') or seg_data.get('name') or '文件'}]")
            elif seg_type == RealMessageType.record:
                parts.append("[语音]")
            elif seg_type == RealMessageType.video:
                parts.append("[视频]")
            elif seg_type:
                parts.append(f"[{seg_type}]")

        preview = "".join(parts).strip() or "[消息]"
        return preview[:60] + "..." if len(preview) > 60 else preview

    # ------------------------------------------------------------------
    # 缓存辅助
    # ------------------------------------------------------------------

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

    async def _get_member_info(self, group_id: Any, user_id: Any) -> dict | None:
        key = f"{group_id}_{user_id}"
        cached = await get_cached("member_info", key, MEMBER_INFO_TTL)
        if cached:
            return cached
        try:
            data = await self._client.call_data(
                "get_group_member_info", {"group_id": group_id, "user_id": user_id}
            )
            if data:
                await set_cached("member_info", key, data)
            return data
        except Exception:
            return None

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

    async def _get_self_info(self) -> dict | None:
        cached = await get_cached("self_info", "self", SELF_INFO_TTL)
        if cached:
            return cached
        try:
            data = await self._client.call_data("get_login_info")
            if data:
                await set_cached("self_info", "self", data)
            return data
        except Exception:
            return None
