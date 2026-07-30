"""平台适配器抽象基类。

定义统一的直播平台事件模型和适配器接口，
支持 B站、抖音、Twitch 等多平台扩展。
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Awaitable, Callable, Literal


class EventPriority(IntEnum):
    """事件优先级（数值越小优先级越高）。"""

    SUPER_CHAT = 0
    GIFT = 1
    DANMAKU = 2
    ENTER = 3
    LIKE = 4


@dataclass(slots=True)
class PlatformEvent:
    """统一的直播平台事件模型。"""

    kind: Literal["danmaku", "gift", "super_chat", "enter", "guard", "like"]
    user_name: str
    content: str = ""
    value: float = 0.0  # 礼物/SC 金额（元）
    timestamp: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def priority(self) -> EventPriority:
        """根据事件类型返回优先级。"""
        mapping = {
            "super_chat": EventPriority.SUPER_CHAT,
            "gift": EventPriority.GIFT,
            "guard": EventPriority.GIFT,
            "danmaku": EventPriority.DANMAKU,
            "enter": EventPriority.ENTER,
            "like": EventPriority.LIKE,
        }
        return mapping.get(self.kind, EventPriority.DANMAKU)

    @property
    def display_text(self) -> str:
        """用于 LLM 上下文的显示文本。"""
        if self.kind == "danmaku":
            return f"{self.user_name}：{self.content}"
        elif self.kind == "gift":
            gift_name = self.metadata.get("gift_name", "礼物")
            gift_num = self.metadata.get("gift_num", 1)
            return f"{self.user_name} 送了 {gift_name}x{gift_num}"
        elif self.kind == "super_chat":
            return f"[SC] {self.user_name}：{self.content}"
        elif self.kind == "enter":
            return f"{self.user_name} 进入了直播间"
        elif self.kind == "guard":
            level = self.metadata.get("guard_level", "舰长")
            return f"{self.user_name} 开通了{level}"
        elif self.kind == "like":
            return f"{self.user_name} 点赞了"
        return self.content


# 事件回调类型
EventCallback = Callable[[PlatformEvent], Awaitable[None]]


class BasePlatformAdapter(ABC):
    """直播平台适配器抽象基类。

    子类需实现：
    - connect(): 建立与平台的连接
    - disconnect(): 断开连接
    - send_danmaku(): 发送弹幕（可选）
    """

    def __init__(self) -> None:
        self._event_callback: EventCallback | None = None
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    def on_event(self, callback: EventCallback) -> None:
        """注册事件回调。"""
        self._event_callback = callback

    async def _emit(self, event: PlatformEvent) -> None:
        """向注册的回调发射事件。"""
        if self._event_callback:
            await self._event_callback(event)

    @abstractmethod
    async def connect(self) -> None:
        """建立与直播平台的连接。"""
        ...

    @abstractmethod
    async def disconnect(self) -> None:
        """断开与直播平台的连接。"""
        ...

    async def send_danmaku(self, text: str) -> bool:
        """发送弹幕到直播间（可选实现）。

        Returns:
            是否发送成功。
        """
        return False

    @abstractmethod
    def platform_name(self) -> str:
        """返回平台标识名。"""
        ...
