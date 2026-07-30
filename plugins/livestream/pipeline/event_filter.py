"""弹幕事件过滤器。

负责在事件进入优先级队列前进行预过滤：
- 最小长度过滤
- 敏感词/黑名单过滤
- 短时间重复弹幕去重
- 进场事件节流
"""

from __future__ import annotations

import time
from collections import defaultdict
from typing import TYPE_CHECKING

from ..platform.base import PlatformEvent

if TYPE_CHECKING:
    from ..config import LivestreamConfig


class EventFilter:
    """弹幕事件过滤器。"""

    def __init__(self, config: "LivestreamConfig") -> None:
        pipeline_cfg = config.pipeline
        self._min_length = pipeline_cfg.min_danmaku_length
        self._dedup_window = pipeline_cfg.dedup_window_seconds

        # 去重缓存：{content_hash: last_timestamp}
        self._recent_danmaku: dict[str, float] = {}
        # 进场节流：{user_name: last_enter_timestamp}
        self._recent_enters: dict[str, float] = {}
        # 敏感词列表（可从外部加载）
        self._blacklist_words: set[str] = set()
        # 黑名单用户
        self._blacklist_users: set[str] = set()

    def should_pass(self, event: PlatformEvent) -> bool:
        """判断事件是否应该通过过滤进入队列。

        Args:
            event: 平台事件。

        Returns:
            True 表示通过，False 表示被过滤。
        """
        now = time.time()

        # 黑名单用户直接拒绝
        if event.user_name in self._blacklist_users:
            return False

        match event.kind:
            case "danmaku":
                return self._filter_danmaku(event, now)
            case "enter":
                return self._filter_enter(event, now)
            case "like":
                # 点赞事件默认不进入互动队列（仅统计）
                return False
            case _:
                # SC、礼物、大航海始终通过
                return True

    def _filter_danmaku(self, event: PlatformEvent, now: float) -> bool:
        """弹幕过滤逻辑。"""
        content = event.content.strip()

        # 最小长度
        if len(content) < self._min_length:
            return False

        # 敏感词
        if self._contains_blacklist_word(content):
            return False

        # 去重：相同内容在窗口内只保留一条
        dedup_key = f"{event.user_name}:{content}"
        last_time = self._recent_danmaku.get(dedup_key, 0)
        if now - last_time < self._dedup_window:
            return False
        self._recent_danmaku[dedup_key] = now

        # 定期清理过期的去重缓存
        self._cleanup_dedup_cache(now)

        return True

    def _filter_enter(self, event: PlatformEvent, now: float) -> bool:
        """进场事件节流：同一用户短时间内只触发一次。"""
        last_time = self._recent_enters.get(event.user_name, 0)
        if now - last_time < 60.0:  # 同一用户 60s 内不重复欢迎
            return False
        self._recent_enters[event.user_name] = now
        return True

    def _contains_blacklist_word(self, content: str) -> bool:
        """检查内容是否包含敏感词。"""
        content_lower = content.lower()
        return any(word in content_lower for word in self._blacklist_words)

    def _cleanup_dedup_cache(self, now: float) -> None:
        """清理过期的去重缓存（保留最近 100 条）。"""
        if len(self._recent_danmaku) > 200:
            expired = [
                k for k, v in self._recent_danmaku.items()
                if now - v > self._dedup_window * 2
            ]
            for k in expired:
                del self._recent_danmaku[k]

    def add_blacklist_word(self, word: str) -> None:
        """添加敏感词。"""
        self._blacklist_words.add(word.lower())

    def add_blacklist_user(self, user_name: str) -> None:
        """添加黑名单用户。"""
        self._blacklist_users.add(user_name)

    def remove_blacklist_user(self, user_name: str) -> None:
        """移除黑名单用户。"""
        self._blacklist_users.discard(user_name)
