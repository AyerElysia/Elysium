"""桌宠 Adapter。

桌宠是 unified consciousness 的一个身体/渠道，不创建第二套会话心智：
桌宠输入进入 platform=desktop.pet 的聊天流，life_chatter 的回复再回到桌宠窗口。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

from fastapi import WebSocket
from mofox_wire import CoreSink, MessageEnvelope

from src.core.components.base.adapter import BaseAdapter
from src.kernel.logger import get_logger

logger = get_logger("DesktopPetAdapter", color="#F5A6C8")

PLATFORM = "desktop.pet"
_ADAPTER_INSTANCE: "DesktopPetAdapter | None" = None


def get_desktop_pet_adapter() -> "DesktopPetAdapter | None":
    return _ADAPTER_INSTANCE


def set_desktop_pet_adapter(adapter: "DesktopPetAdapter | None") -> None:
    global _ADAPTER_INSTANCE
    _ADAPTER_INSTANCE = adapter


class DesktopPetAdapter(BaseAdapter):
    """本地桌宠适配器。

    入方向：HTTP/WS 调用 ``send_message``，转为 MessageEnvelope 交给 CoreSink。
    出方向：life_chatter 发送到 ``desktop.pet``，写入内存队列并广播给连接的桌宠。
    """

    adapter_name = "desktop_pet_adapter"
    adapter_version = "0.1.0"
    adapter_description = "本地桌宠适配器"
    platform = PLATFORM
    run_in_subprocess = False

    def __init__(self, core_sink: CoreSink, plugin: Any | None = None, **kwargs: Any) -> None:
        super().__init__(core_sink, plugin=plugin, transport=None, **kwargs)
        self._pending_responses: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._message_cache: dict[str, dict[str, Any]] = {}
        self._cache_max_size = 1000
        self._clients: set[WebSocket] = set()
        set_desktop_pet_adapter(self)
        logger.info("DesktopPetAdapter 初始化完成")

    async def on_adapter_loaded(self) -> None:
        logger.info("DesktopPetAdapter 已加载，等待桌宠接入")

    async def on_adapter_unloaded(self) -> None:
        set_desktop_pet_adapter(None)
        self._clients.clear()
        self._message_cache.clear()
        while not self._pending_responses.empty():
            try:
                self._pending_responses.get_nowait()
            except asyncio.QueueEmpty:
                break

    async def health_check(self) -> bool:
        return True

    def is_connected(self) -> bool:  # type: ignore[override]
        return True

    async def get_bot_info(self) -> dict[str, str]:  # type: ignore[override]
        return {"bot_id": "elysia_desktop_pet", "bot_name": "爱莉"}

    async def register_client(self, websocket: WebSocket) -> None:
        """注册桌宠 WebSocket 客户端，并刷新桌宠聊天流活跃时间。"""
        self._clients.add(websocket)
        try:
            await self._touch_desktop_stream()
        except Exception as exc:
            logger.warning(f"刷新桌宠聊天流活跃时间失败: {exc}", exc_info=True)

    async def unregister_client(self, websocket: WebSocket) -> None:
        """注销桌宠 WebSocket 客户端，不写入聊天历史。"""
        self._clients.discard(websocket)
        logger.info(f"桌宠 WebSocket 客户端已断开，当前连接数: {len(self._clients)}")

    def is_client_connected(self) -> bool:
        """返回是否存在已连接的桌宠客户端。"""
        return bool(self._clients)

    def connected_client_count(self) -> int:
        """返回当前已注册的桌宠客户端数量。"""
        return len(self._clients)

    async def _touch_desktop_stream(self) -> None:
        """确保桌宠私聊流存在，并刷新最后活跃时间。"""
        from src.core.managers.stream_manager import get_stream_manager

        stream_manager = get_stream_manager()
        stream = await stream_manager.get_or_create_stream(
            platform=PLATFORM,
            user_id="desktop_owner",
            group_name="主人",
            chat_type="private",
        )
        await stream_manager.activate_stream(stream.stream_id)
        await self._touch_desktop_person()

    async def _touch_desktop_person(self) -> None:
        """确保桌宠主人用户资料存在，并刷新用户交互信息。"""
        from src.core.utils.user_query_helper import get_user_query_helper

        await get_user_query_helper().update_person_info(
            platform=PLATFORM,
            user_id="desktop_owner",
            nickname="主人",
        )

    async def send_message(self, raw_message: dict[str, Any]) -> dict[str, Any]:
        """将桌宠用户输入推入 Neo-MoFox 核心。"""
        envelope = await self.from_platform_message(raw_message)
        if envelope is None:
            raise ValueError("无法转换桌宠消息")
        await self.core_sink.send(envelope)
        return raw_message

    async def from_platform_message(  # type: ignore[override]
        self,
        raw: dict[str, Any],
    ) -> MessageEnvelope | None:
        try:
            message_id = str(raw.get("message_id") or f"desktop_{uuid.uuid4().hex}")
            user_id = str(raw.get("user_id") or "desktop_owner")
            nickname = str(raw.get("nickname") or "主人")
            content = str(raw.get("content") or "")
            timestamp = float(raw.get("timestamp") or time.time())
            client_id = str(raw.get("client_id") or "default")
            message_type = str(raw.get("message_type") or "text")

            self._cache_message({
                "message_id": message_id,
                "user_id": user_id,
                "nickname": nickname,
                "content": content,
                "timestamp": timestamp,
                "message_type": message_type,
                "client_id": client_id,
            })

            metadata = {
                "raw": raw,
                "desktop_pet": True,
                "client_id": client_id,
            }
            message_info = {
                "platform": PLATFORM,
                "message_id": message_id,
                "time": timestamp,
                "user_info": {
                    "platform": PLATFORM,
                    "user_id": user_id,
                    "user_nickname": nickname,
                },
                "extra": {
                    "source": "desktop_pet",
                    "client_id": client_id,
                    "format_info": {"accept_format": ["text", "file"]},
                },
            }
            envelope: MessageEnvelope = {  # type: ignore[typeddict-item]
                "direction": "incoming",
                "message_info": message_info,
                "message_segment": [{"type": message_type, "data": content}],  # type: ignore[typeddict-item]
                "metadata": metadata,
            }
            return envelope
        except Exception as exc:
            logger.error(f"桌宠消息转换失败: {exc}", exc_info=True)
            return None

    async def _send_platform_message(  # type: ignore[override]
        self,
        envelope: MessageEnvelope,
    ) -> None:
        payload = self._outgoing_payload(envelope)
        self._cache_message(payload)
        await self._pending_responses.put(payload)
        await self._broadcast(payload)
        logger.info(f"桌宠回复已入队: {str(payload.get('content') or '')[:80]}")

    async def get_pending_responses(self, user_id: str | None = None) -> list[dict[str, Any]]:
        responses: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        while not self._pending_responses.empty():
            try:
                msg = self._pending_responses.get_nowait()
            except asyncio.QueueEmpty:
                break
            if user_id and msg.get("target_user_id") not in (None, "", user_id):
                skipped.append(msg)
                continue
            responses.append(msg)
        for msg in skipped:
            await self._pending_responses.put(msg)
        return responses

    def _outgoing_payload(self, envelope: MessageEnvelope) -> dict[str, Any]:
        message_info = envelope.get("message_info", {}) or {}
        segments = envelope.get("message_segment", []) or []
        if isinstance(segments, dict):
            segments = [segments]

        text_parts: list[str] = []
        files: list[dict[str, str]] = []
        images: list[str] = []
        voices: list[str] = []

        for seg in segments:
            if not isinstance(seg, dict):
                continue
            seg_type = str(seg.get("type") or "")
            data = seg.get("data")
            if seg_type == "text":
                text_parts.append(str(data or ""))
            elif seg_type == "file":
                files.append({"path": str(data or ""), "name": str(seg.get("name") or "")})
            elif seg_type == "image":
                images.append(str(data or ""))
            elif seg_type == "voice":
                voices.append(str(data or ""))

        user_info = message_info.get("user_info") or {}
        payload = {
            "message_id": str(message_info.get("message_id") or f"desktop_out_{uuid.uuid4().hex}"),
            "user_id": "elysia_desktop_pet",
            "nickname": "爱莉",
            "content": "".join(text_parts),
            "files": files,
            "images": images,
            "voices": voices,
            "timestamp": time.time(),
            "message_type": "file" if files else ("image" if images else "text"),
            "target_user_id": str(user_info.get("user_id") or ""),
        }
        return payload

    async def _broadcast(self, payload: dict[str, Any]) -> None:
        stale: list[WebSocket] = []
        for client in list(self._clients):
            try:
                await client.send_json({"type": "message", "message": payload})
            except Exception:
                stale.append(client)
        for client in stale:
            self._clients.discard(client)

    def _cache_message(self, message: dict[str, Any]) -> None:
        message_id = message.get("message_id")
        if not message_id:
            return
        self._message_cache[str(message_id)] = message
        if len(self._message_cache) > self._cache_max_size:
            oldest_key = next(iter(self._message_cache))
            del self._message_cache[oldest_key]
