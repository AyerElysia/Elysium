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
