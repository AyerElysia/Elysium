"""desktop_pet 路由测试。"""

from __future__ import annotations

from typing import Any, cast

from fastapi import WebSocket
from fastapi.testclient import TestClient

from plugins.desktop_pet.adapter import DesktopPetAdapter, set_desktop_pet_adapter
from plugins.desktop_pet.router import DesktopPetRouter


class _DummyPlugin:
    """测试用插件占位。"""

    config = None


class _DummySink:
    """记录发送到核心的消息。"""

    def __init__(self) -> None:
        """初始化空消息列表。"""
        self.messages: list[dict[str, Any]] = []

    async def send(self, message: dict[str, Any]) -> None:
        """保存一条发送到核心的消息。"""
        self.messages.append(message)


def test_status_includes_connected_clients() -> None:
    """状态接口应返回当前桌宠 WebSocket 客户端数量。"""
    adapter = DesktopPetAdapter(_DummySink())
    adapter._clients.add(cast(WebSocket, object()))
    adapter._clients.add(cast(WebSocket, object()))
    set_desktop_pet_adapter(adapter)
    try:
        router = DesktopPetRouter(plugin=_DummyPlugin())
        client = TestClient(router.app)

        response = client.get("/api/status")

        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is True
        assert data["adapter_ready"] is True
        assert data["platform"] == "desktop.pet"
        assert data["connected_clients"] == 2
    finally:
        set_desktop_pet_adapter(None)
