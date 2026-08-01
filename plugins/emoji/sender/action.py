"""emoji_sender Action：发送表情包。

该 Action 面向 LLM Tool Calling：
- 输入：你想表达的情绪/意图描述
- 行为：纯视觉语义检索——把意图 embed 到多模态空间，匹配最贴合的表情包图像并发送
"""

from __future__ import annotations

from typing import Annotated
from typing import cast

from src.app.plugin_system.api.service_api import get_service
from src.core.components.base.action import BaseAction

from .service import EmojiSenderService


class SendEmojiMemeAction(BaseAction):
    """发送表情包动作。"""

    action_name: str = "send_emoji_meme"
    action_description: str = (
        "根据你想表达的情绪或意图，检索并发送一张最贴合的表情包来生动地表达。"
        "只需要描述你此刻想表达什么（例如：'俏皮地吐舌头卖萌'、'无语地翻白眼'、'开心地大笑'），"
        "系统会按视觉语义匹配最合适的表情包。"
        "不要忘记在聊天时使用这个动作，比起简单的文字它往往更受欢迎。"
        "此动作可以单独使用也可以和发送文字一起使用，更符合日常聊天习惯。"
    )
    primary_action: bool = False
    chatter_allow: list[str] = ["life_chatter"]

    async def execute(
        self,
        description: Annotated[
            str,
            "你想表达的情绪或意图（例如：'俏皮地吐舌头卖萌'、'无语地翻白眼'），用于视觉语义匹配",
        ],
    ) -> tuple[bool, str]:
        """执行发送表情包动作。"""
        service = get_service("emoji:service:emoji_sender")
        if service is None:
            return False, "emoji_sender service 未加载"

        service = cast(EmojiSenderService, service)

        ok, result, reason = await service.send_best_detailed(
            stream_id=self.chat_stream.stream_id,
            platform=self.chat_stream.platform,
            description_query=description,
        )

        if ok:
            if not result:
                return True, "已发送表情包"

            desc = str(result.get("description") or "").strip()
            distance = result.get("distance")
            fallback_used = bool(result.get("fallback_used"))

            dist_text = f"{float(distance):.4f}" if isinstance(distance, (int, float)) else "unknown"
            fallback_text = "（视觉库为空，已回退文本检索）" if fallback_used else ""

            detail = f"已发送表情包{fallback_text}\n- 描述: {desc or '（无）'}\n- 匹配距离: {dist_text}"
            return True, detail

        # 失败：尽量带上原因
        return False, reason


class RecallEmojiAction(BaseAction):
    """召回收藏的表情包候选（带备注），由她自己挑。"""

    action_name: str = "recall_emoji"
    action_description: str = (
        "从你自己收藏的表情包里召回一批候选，每张都带着你当初收藏时写的备注，由你自己挑一张。"
        "两种用法："
        "(1) 意图模式（mode='intent'，默认）：随机递给你几张，你自己想起来哪张合适、自己挑；"
        "(2) 视觉模式（mode='visual'）：描述你想表达什么（description），按外观相似度找候选。"
        "看到候选后，用 send_emoji_by_id 发送你选中的那张。"
        "如果这批没有合适的，可以再调一次换一批。"
    )
    primary_action: bool = False
    chatter_allow: list[str] = ["life_chatter"]

    async def execute(
        self,
        mode: Annotated[
            str,
            "召回模式：'intent'（默认，随机递几张你自己挑）或 'visual'（按描述的外观相似度找）",
        ] = "intent",
        description: Annotated[
            str,
            "visual 模式下你想表达的情绪/意图（例如'俏皮地吐舌头'）；intent 模式可不填",
        ] = "",
        count: Annotated[int, "想看几张候选，默认 6"] = 6,
    ) -> tuple[bool, str]:
        service = get_service("emoji:service:emoji_sender")
        if service is None:
            return False, "emoji_sender service 未加载"
        service = cast(EmojiSenderService, service)

        candidates = await service.recall_collected_memes(
            mode=mode,
            description=description,
            count=count,
        )
        if not candidates:
            return True, "你还没有收藏表情包，或者这次没有召回到。可以先用 nucleus_browse_memes 看看最近收到的，收藏几张。"

        lines = [f"这里是 {len(candidates)} 张你收藏的表情包，挑一张用 send_emoji_by_id 发送："]
        for i, c in enumerate(candidates, 1):
            note = str(c.get("note") or "").strip() or "（当时没写备注）"
            lines.append(f"{i}. [{c.get('meme_id', '')}] 备注：{note}")
        return True, "\n".join(lines)


class SendEmojiByIdAction(BaseAction):
    """发送她选中的那张表情包（按 meme_id）。"""

    action_name: str = "send_emoji_by_id"
    action_description: str = (
        "发送你选中的那张表情包。先用 recall_emoji 看到候选及其 meme_id，然后用这个动作发送你挑中的那张。"
    )
    primary_action: bool = False
    chatter_allow: list[str] = ["life_chatter"]

    async def execute(
        self,
        meme_id: Annotated[str, "要发送的表情包 meme_id（recall_emoji 返回的）"],
    ) -> tuple[bool, str]:
        service = get_service("emoji:service:emoji_sender")
        if service is None:
            return False, "emoji_sender service 未加载"
        service = cast(EmojiSenderService, service)

        ok, msg = await service.send_meme_by_id(
            meme_id=meme_id,
            stream_id=self.chat_stream.stream_id,
            platform=self.chat_stream.platform,
        )
        return ok, msg
