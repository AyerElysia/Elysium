"""B站直播平台适配器。

基于 blivedm 库实现 B站直播间弹幕采集，支持：
- 弹幕消息
- 礼物消息
- 醒目留言（Super Chat）
- 进场消息
- 大航海（舰长/提督/总督）
- 点赞消息

连接方式：Web 端 WebSocket 直连（room_id + 可选 sessdata）
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

try:
    import blivedm
    from blivedm.models.web import (
        DanmakuMessage,
        EntryEffectMessage,
        GiftMessage,
        GuardBuyMessage,
        LikeMessage,
        SuperChatMessage,
    )

    _BLIVEDM_AVAILABLE = True
except ImportError:
    _BLIVEDM_AVAILABLE = False

from .base import BasePlatformAdapter, PlatformEvent

logger = logging.getLogger(__name__)


class BilibiliAdapter(BasePlatformAdapter):
    """B站直播平台适配器。

    使用 blivedm 库连接 B站直播间 WebSocket，
    实时接收弹幕、礼物、SC 等事件并转换为统一 PlatformEvent。
    """

    def __init__(
        self,
        room_id: str,
        sessdata: str = "",
        buvid3: str = "",
        reconnect_interval: float = 5.0,
    ) -> None:
        super().__init__()
        self._room_id = int(room_id) if room_id.isdigit() else 0
        self._sessdata = sessdata
        self._buvid3 = buvid3
        self._reconnect_interval = reconnect_interval
        self._client: Any = None
        self._task: asyncio.Task | None = None
        self._running = False

    def platform_name(self) -> str:
        return "bilibili"

    async def connect(self) -> None:
        """建立与 B站直播间的 WebSocket 连接。"""
        if not _BLIVEDM_AVAILABLE:
            raise RuntimeError(
                "blivedm 未安装，请执行: pip install blivedm"
            )
        if not self._room_id:
            raise ValueError("room_id 不能为空")

        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info(f"B站直播适配器已启动: room_id={self._room_id}")

    async def disconnect(self) -> None:
        """断开与 B站直播间的连接。"""
        self._running = False
        if self._client:
            await self._client.stop()
            self._client = None
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        self._connected = False
        logger.info("B站直播适配器已断开")

    async def send_danmaku(self, text: str) -> bool:
        """发送弹幕（需要 sessdata 认证）。

        注意：blivedm 本身不支持发送弹幕，
        此处预留接口，后续可通过 bilibili-api 实现。
        """
        # TODO: 集成 bilibili-api 实现弹幕发送
        logger.debug(f"弹幕发送（未实现）: {text}")
        return False

    async def _run_loop(self) -> None:
        """主运行循环，含自动重连逻辑。"""
        while self._running:
            try:
                await self._connect_and_listen()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning(
                    f"B站直播连接异常: {exc}，"
                    f"{self._reconnect_interval}s 后重连"
                )
                self._connected = False
                await asyncio.sleep(self._reconnect_interval)

    async def _connect_and_listen(self) -> None:
        """建立连接并监听消息。"""
        # 构建 session 配置
        session_kwargs: dict[str, Any] = {}
        if self._sessdata:
            session_kwargs["cookies"] = {
                "SESSDATA": self._sessdata,
            }
            if self._buvid3:
                session_kwargs["cookies"]["buvid3"] = self._buvid3

        self._client = blivedm.BLiveClient(
            self._room_id,
            **session_kwargs,
        )

        # 注册消息处理器
        handler = _BilibiliHandler(self)
        self._client.set_handler(handler)

        await self._client.start()
        self._connected = True
        logger.info(f"B站直播间已连接: {self._room_id}")

        # 等待连接结束
        await self._client.join()

    async def _handle_danmaku(self, message: Any) -> None:
        """处理弹幕消息。"""
        event = PlatformEvent(
            kind="danmaku",
            user_name=message.uname,
            content=message.msg,
            metadata={
                "uid": message.uid,
                "fans_medal_name": getattr(message, "fans_medal_name", ""),
                "fans_medal_level": getattr(message, "fans_medal_level", 0),
            },
        )
        await self._emit(event)

    async def _handle_gift(self, message: Any) -> None:
        """处理礼物消息。"""
        event = PlatformEvent(
            kind="gift",
            user_name=message.uname,
            content=f"送出 {message.gift_name}x{message.num}",
            value=getattr(message, "price", 0) * message.num / 1000,  # 金瓜子转元
            metadata={
                "uid": message.uid,
                "gift_name": message.gift_name,
                "gift_num": message.num,
                "coin_type": getattr(message, "coin_type", ""),
            },
        )
        await self._emit(event)

    async def _handle_super_chat(self, message: Any) -> None:
        """处理醒目留言（SC）。"""
        event = PlatformEvent(
            kind="super_chat",
            user_name=message.uname,
            content=message.message,
            value=float(message.price),
            metadata={
                "uid": message.uid,
                "price": message.price,
                "time": getattr(message, "time", 0),
            },
        )
        await self._emit(event)

    async def _handle_enter(self, message: Any) -> None:
        """处理进场消息。"""
        # EntryEffectMessage 包含进场特效（大航海等）
        uid = getattr(message, "uid", 0)
        # 尝试从 copy_writing 提取用户名
        copy_writing = getattr(message, "copy_writing", "")
        user_name = ""
        if "欢迎" in copy_writing:
            # 格式通常为 "欢迎 <%xxx%> 进入直播间"
            import re
            match = re.search(r"<%(.*?)%>", copy_writing)
            if match:
                user_name = match.group(1)
        if not user_name:
            user_name = f"用户{uid}"

        event = PlatformEvent(
            kind="enter",
            user_name=user_name,
            content="进入直播间",
            metadata={"uid": uid},
        )
        await self._emit(event)

    async def _handle_guard(self, message: Any) -> None:
        """处理大航海（舰长/提督/总督）消息。"""
        guard_level_map = {1: "总督", 2: "提督", 3: "舰长"}
        level = guard_level_map.get(message.guard_level, "舰长")
        event = PlatformEvent(
            kind="guard",
            user_name=message.username,
            content=f"开通了{level}",
            value=getattr(message, "price", 0) / 1000,
            metadata={
                "uid": message.uid,
                "guard_level": level,
                "num": getattr(message, "num", 1),
            },
        )
        await self._emit(event)

    async def _handle_like(self, message: Any) -> None:
        """处理点赞消息。"""
        event = PlatformEvent(
            kind="like",
            user_name=getattr(message, "uname", "观众"),
            content="点赞",
            metadata={"uid": getattr(message, "uid", 0)},
        )
        await self._emit(event)


class _BilibiliHandler(blivedm.BaseHandler if _BLIVEDM_AVAILABLE else object):
    """blivedm 消息处理器，将原始消息转发给 BilibiliAdapter。"""

    def __init__(self, adapter: BilibiliAdapter) -> None:
        if _BLIVEDM_AVAILABLE:
            super().__init__()
        self._adapter = adapter

    async def _on_danmaku(self, client: Any, message: Any) -> None:
        await self._adapter._handle_danmaku(message)

    async def _on_gift(self, client: Any, message: Any) -> None:
        await self._adapter._handle_gift(message)

    async def _on_super_chat(self, client: Any, message: Any) -> None:
        await self._adapter._handle_super_chat(message)

    async def _on_entry_effect(self, client: Any, message: Any) -> None:
        await self._adapter._handle_enter(message)

    async def _on_guard_buy(self, client: Any, message: Any) -> None:
        await self._adapter._handle_guard(message)

    async def _on_like(self, client: Any, message: Any) -> None:
        await self._adapter._handle_like(message)
