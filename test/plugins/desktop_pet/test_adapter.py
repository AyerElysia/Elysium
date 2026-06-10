"""desktop_pet 适配器测试。"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi import WebSocket

from plugins.desktop_pet.adapter import DesktopPetAdapter, set_desktop_pet_adapter


class _DummySink:
    """记录发送到核心的消息。"""

    def __init__(self) -> None:
        """初始化空消息列表。"""
        self.messages: list[dict[str, Any]] = []

    async def send(self, message: dict[str, Any]) -> None:
        """保存一条发送到核心的消息。"""
        self.messages.append(message)


class _FakeStreamManager:
    """用于验证桌宠流创建和 touch 的假 StreamManager。"""

    def __init__(self) -> None:
        """初始化空流集合。"""
        self.streams: dict[str, SimpleNamespace] = {}
        self.create_calls: list[dict[str, Any]] = []
        self.activated_stream_ids: list[str] = []

    async def get_or_create_stream(
        self,
        *,
        platform: str,
        user_id: str,
        group_name: str,
        chat_type: str,
    ) -> SimpleNamespace:
        """创建或返回桌宠流。"""
        self.create_calls.append(
            {
                "platform": platform,
                "user_id": user_id,
                "group_name": group_name,
                "chat_type": chat_type,
            }
        )
        stream = self.streams.get("desktop_stream")
        if stream is None:
            stream = SimpleNamespace(
                stream_id="desktop_stream",
                platform=platform,
                user_id=user_id,
                stream_name=group_name,
                chat_type=chat_type,
                last_active_time=1.0,
            )
            self.streams[stream.stream_id] = stream
        return stream

    async def activate_stream(self, stream_id: str) -> SimpleNamespace | None:
        """刷新指定流的活跃时间。"""
        self.activated_stream_ids.append(stream_id)
        stream = self.streams.get(stream_id)
        if stream is None:
            return None
        stream.last_active_time = time.time()
        return stream


class _FakeUserQueryHelper:
    """用于验证桌宠用户资料更新的假 UserQueryHelper。"""

    def __init__(self) -> None:
        """初始化空调用记录。"""
        self.update_calls: list[dict[str, Any]] = []

    async def update_person_info(
        self,
        *,
        platform: str,
        user_id: str,
        nickname: str | None = None,
        cardname: str | None = None,
    ) -> bool:
        """记录用户资料更新请求。"""
        self.update_calls.append(
            {
                "platform": platform,
                "user_id": user_id,
                "nickname": nickname,
                "cardname": cardname,
            }
        )
        return True


def _make_adapter() -> DesktopPetAdapter:
    """创建测试用桌宠适配器。"""
    return DesktopPetAdapter(_DummySink())


def test_from_platform_message_converts_desktop_payload() -> None:
    """桌宠输入应转换为 desktop.pet 的标准消息信封。"""

    async def _run() -> None:
        """执行异步转换断言。"""
        adapter = _make_adapter()
        try:
            envelope = await adapter.from_platform_message(
                {
                    "message_id": "msg_1",
                    "user_id": "desktop_owner",
                    "nickname": "主人",
                    "content": "爱莉，早上好",
                    "timestamp": 123.0,
                    "client_id": "pet_1",
                    "message_type": "text",
                }
            )
            assert envelope is not None
            assert envelope["direction"] == "incoming"
            assert envelope["message_info"]["platform"] == "desktop.pet"
            assert envelope["message_info"]["message_id"] == "msg_1"
            assert envelope["message_info"]["time"] == 123.0
            assert envelope["message_info"]["user_info"] == {
                "platform": "desktop.pet",
                "user_id": "desktop_owner",
                "user_nickname": "主人",
            }
            assert envelope["message_segment"] == [
                {"type": "text", "data": "爱莉，早上好"}
            ]
            assert envelope["metadata"]["desktop_pet"] is True
            assert envelope["metadata"]["client_id"] == "pet_1"
        finally:
            set_desktop_pet_adapter(None)

    asyncio.run(_run())


def test_register_client_touches_desktop_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    """注册 WebSocket 客户端后应创建并刷新桌宠私聊流。"""

    async def _run() -> None:
        """执行异步注册断言。"""
        fake_stream_manager = _FakeStreamManager()
        fake_user_query_helper = _FakeUserQueryHelper()
        monkeypatch.setattr(
            "src.core.managers.stream_manager.get_stream_manager",
            lambda: fake_stream_manager,
        )
        monkeypatch.setattr(
            "src.core.utils.user_query_helper.get_user_query_helper",
            lambda: fake_user_query_helper,
        )
        adapter = _make_adapter()
        websocket = cast(WebSocket, object())
        try:
            await adapter.register_client(websocket)
            assert adapter.is_client_connected() is True
            assert adapter.connected_client_count() == 1
            assert fake_stream_manager.create_calls == [
                {
                    "platform": "desktop.pet",
                    "user_id": "desktop_owner",
                    "group_name": "主人",
                    "chat_type": "private",
                }
            ]
            assert fake_stream_manager.activated_stream_ids == ["desktop_stream"]
            assert fake_stream_manager.streams["desktop_stream"].last_active_time > 1.0
            assert fake_user_query_helper.update_calls == [
                {
                    "platform": "desktop.pet",
                    "user_id": "desktop_owner",
                    "nickname": "主人",
                    "cardname": None,
                }
            ]
        finally:
            await adapter.unregister_client(websocket)
            set_desktop_pet_adapter(None)

    asyncio.run(_run())


def test_get_pending_responses_filters_target_user_id() -> None:
    """轮询回复时应只取匹配 target_user_id 或无目标的消息。"""

    async def _run() -> None:
        """执行异步过滤断言。"""
        adapter = _make_adapter()
        try:
            await adapter._pending_responses.put(
                {"message_id": "global", "content": "全局"}
            )
            await adapter._pending_responses.put(
                {"message_id": "match", "content": "给 u1", "target_user_id": "u1"}
            )
            await adapter._pending_responses.put(
                {"message_id": "skip", "content": "给 u2", "target_user_id": "u2"}
            )

            responses = await adapter.get_pending_responses(user_id="u1")
            assert [item["message_id"] for item in responses] == ["global", "match"]

            remaining = await adapter.get_pending_responses()
            assert [item["message_id"] for item in remaining] == ["skip"]
        finally:
            set_desktop_pet_adapter(None)

    asyncio.run(_run())
