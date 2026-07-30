"""优先级事件队列。

实现带速率限制和弹幕聚合的优先级队列：
- 4 级优先级：SC(0) > 礼物(1) > 弹幕(2) > 进场(3)
- 速率限制：max_responses_per_minute
- 弹幕聚合窗口：batch_window_seconds 内的弹幕打包
- 队列溢出策略：丢弃低优先级
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ..platform.base import EventPriority, PlatformEvent

if TYPE_CHECKING:
    from ..config import LivestreamConfig


@dataclass(slots=True)
class QueuedItem:
    """队列条目。"""

    event: PlatformEvent
    priority: int
    enqueue_time: float = field(default_factory=time.time)
    # 聚合的弹幕列表（仅弹幕类型使用）
    batch: list[PlatformEvent] = field(default_factory=list)


class PriorityEventQueue:
    """优先级事件队列，带速率限制和弹幕聚合。"""

    def __init__(self, config: "LivestreamConfig") -> None:
        pipeline_cfg = config.pipeline
        self._max_size = pipeline_cfg.max_queue_size
        self._max_per_minute = pipeline_cfg.max_responses_per_minute
        self._batch_window = pipeline_cfg.batch_window_seconds

        # 按优先级分桶
        self._buckets: dict[int, list[QueuedItem]] = {
            p.value: [] for p in EventPriority
        }
        # 速率限制：最近 60s 内的响应时间戳
        self._response_times: list[float] = []
        # 弹幕聚合缓冲
        self._danmaku_buffer: list[PlatformEvent] = []
        self._danmaku_buffer_time: float = 0
        # 锁
        self._lock = asyncio.Lock()

    @property
    def size(self) -> int:
        """当前队列总大小。"""
        return sum(len(bucket) for bucket in self._buckets.values())

    @property
    def is_empty(self) -> bool:
        return self.size == 0

    async def push(self, event: PlatformEvent) -> bool:
        """将事件推入队列。

        Args:
            event: 平台事件。

        Returns:
            是否成功入队（队列满时低优先级可能被丢弃）。
        """
        async with self._lock:
            priority = event.priority.value

            # 弹幕类型进入聚合缓冲
            if event.kind == "danmaku":
                return self._buffer_danmaku(event)

            # 队列溢出检查
            if self.size >= self._max_size:
                # 丢弃最低优先级
                if not self._evict_lowest(priority):
                    return False

            item = QueuedItem(event=event, priority=priority)
            self._buckets[priority].append(item)
            return True

    def _buffer_danmaku(self, event: PlatformEvent) -> bool:
        """弹幕聚合缓冲。"""
        now = time.time()
        if not self._danmaku_buffer:
            self._danmaku_buffer_time = now
        self._danmaku_buffer.append(event)
        return True

    async def flush_danmaku_buffer(self) -> None:
        """将聚合缓冲中的弹幕打包为一个队列条目。

        由调度器在聚合窗口结束时调用。
        """
        async with self._lock:
            if not self._danmaku_buffer:
                return

            # 取第一条作为主事件，其余放入 batch
            primary = self._danmaku_buffer[0]
            item = QueuedItem(
                event=primary,
                priority=EventPriority.DANMAKU.value,
                batch=list(self._danmaku_buffer),
            )

            # 队列溢出检查
            if self.size >= self._max_size:
                self._evict_lowest(EventPriority.DANMAKU.value)

            self._buckets[EventPriority.DANMAKU.value].append(item)
            self._danmaku_buffer.clear()

    async def pop(self) -> QueuedItem | None:
        """取出最高优先级的队列条目。

        Returns:
            最高优先级条目，队列为空时返回 None。
        """
        async with self._lock:
            for priority in sorted(self._buckets.keys()):
                bucket = self._buckets[priority]
                if bucket:
                    return bucket.pop(0)
            return None

    def can_respond(self) -> bool:
        """检查速率限制是否允许新的响应。"""
        now = time.time()
        # 清理 60s 前的记录
        self._response_times = [
            t for t in self._response_times if now - t < 60.0
        ]
        return len(self._response_times) < self._max_per_minute

    def record_response(self) -> None:
        """记录一次响应（用于速率限制）。"""
        self._response_times.append(time.time())

    def _evict_lowest(self, incoming_priority: int) -> bool:
        """驱逐最低优先级的条目，为 incoming_priority 腾出空间。

        Returns:
            是否成功驱逐。
        """
        for priority in sorted(self._buckets.keys(), reverse=True):
            if priority > incoming_priority and self._buckets[priority]:
                self._buckets[priority].pop()
                return True
        return False

    @property
    def danmaku_buffer_ready(self) -> bool:
        """弹幕聚合窗口是否已到期。"""
        if not self._danmaku_buffer:
            return False
        return time.time() - self._danmaku_buffer_time >= self._batch_window

    def clear(self) -> None:
        """清空队列。"""
        for bucket in self._buckets.values():
            bucket.clear()
        self._danmaku_buffer.clear()
