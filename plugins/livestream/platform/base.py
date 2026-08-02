"""Livestream platform adapter boundary."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from ..domain import PlatformEvent


@dataclass(frozen=True, slots=True)
class PlatformHealth:
    """Read-only technical health without credentials."""

    connected: bool
    reconnect_count: int = 0
    connected_at: float | None = None
    last_event_at: float | None = None
    last_error: str = ""


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

    @property
    def health(self) -> PlatformHealth:
        return PlatformHealth(connected=self._connected)

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
