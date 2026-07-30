"""主动行为引擎。

当直播间空闲时，AI 主动发起互动：
- 空闲闲聊（随机话题）
- 观众进场欢迎（批量聚合）
- 定时话题切换
- 礼物感谢（即时短回复）
"""

from __future__ import annotations

import logging
import random
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..config import LivestreamConfig

logger = logging.getLogger(__name__)

# 默认空闲话题（当配置为空时使用）
_DEFAULT_IDLE_TOPICS = [
    "大家今天过得怎么样呀？",
    "有没有什么想聊的话题？",
    "今天天气不错呢，大家出门了吗？",
    "最近有没有看什么好看的番或者玩什么游戏？",
    "直播间人不多，咱们随便聊聊~",
    "大家想听我唱首歌吗？",
    "有没有什么好玩的事情分享一下？",
]


class ProactiveEngine:
    """主动行为引擎。"""

    def __init__(self, config: "LivestreamConfig") -> None:
        proactive_cfg = config.proactive
        self._idle_timeout = proactive_cfg.idle_timeout_seconds
        self._welcome_enabled = proactive_cfg.welcome_enabled
        self._welcome_batch_seconds = proactive_cfg.welcome_batch_seconds
        self._topic_interval = proactive_cfg.topic_switch_interval_seconds
        self._topics = proactive_cfg.topics or _DEFAULT_IDLE_TOPICS
        self._gift_thanks_enabled = proactive_cfg.gift_thanks_enabled

        # 状态追踪
        self._last_idle_action_time = time.time()
        self._last_topic_switch_time = time.time()
        self._pending_welcomes: list[str] = []
        self._last_welcome_flush_time = time.time()
        self._idle_action_count = 0

    async def get_idle_action(self) -> str | None:
        """获取空闲时的主动行为文本。

        Returns:
            要说的文本，None 表示暂不行动。
        """
        now = time.time()

        # 防止过于频繁的主动行为（至少间隔 idle_timeout）
        if now - self._last_idle_action_time < self._idle_timeout:
            return None

        # 检查是否有待处理的欢迎
        welcome_text = self._try_flush_welcomes(now)
        if welcome_text:
            self._last_idle_action_time = now
            return welcome_text

        # 话题切换
        if self._topic_interval > 0:
            if now - self._last_topic_switch_time >= self._topic_interval:
                self._last_topic_switch_time = now
                self._last_idle_action_time = now
                return self._pick_topic()

        # 随机闲聊（不是每次都触发，避免太话痨）
        self._idle_action_count += 1
        if self._idle_action_count % 2 == 0:  # 每两次空闲才触发一次
            self._last_idle_action_time = now
            return self._pick_topic()

        self._last_idle_action_time = now
        return None

    def add_welcome(self, user_name: str) -> None:
        """添加待欢迎的观众。"""
        if self._welcome_enabled:
            self._pending_welcomes.append(user_name)

    def build_gift_thanks(self, user_name: str, gift_name: str, gift_num: int) -> str | None:
        """构建礼物感谢文本。"""
        if not self._gift_thanks_enabled:
            return None
        if gift_num > 1:
            return f"谢谢{user_name}送的{gift_num}个{gift_name}！太感谢啦~"
        return f"谢谢{user_name}的{gift_name}！"

    def build_guard_thanks(self, user_name: str, level: str) -> str:
        """构建大航海感谢文本。"""
        return f"哇！欢迎{user_name}成为{level}！谢谢支持！"

    def build_sc_thanks(self, user_name: str, price: float) -> str:
        """构建 SC 感谢文本。"""
        return f"感谢{user_name}的{price}元SC！"

    def _try_flush_welcomes(self, now: float) -> str | None:
        """尝试批量输出欢迎语。"""
        if not self._pending_welcomes:
            return None
        if now - self._last_welcome_flush_time < self._welcome_batch_seconds:
            return None

        names = self._pending_welcomes[:5]  # 最多欢迎 5 人
        self._pending_welcomes = self._pending_welcomes[5:]
        self._last_welcome_flush_time = now

        if len(names) == 1:
            return f"欢迎{names[0]}来到直播间~"
        return f"欢迎{'、'.join(names)}来到直播间~"

    def _pick_topic(self) -> str:
        """随机选取一个话题。"""
        return random.choice(self._topics)

    def reset(self) -> None:
        """重置状态（直播开始时调用）。"""
        now = time.time()
        self._last_idle_action_time = now
        self._last_topic_switch_time = now
        self._last_welcome_flush_time = now
        self._pending_welcomes.clear()
        self._idle_action_count = 0
