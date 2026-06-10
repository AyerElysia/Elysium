"""桌宠桥接 HTTP/WS 路由。"""

from __future__ import annotations

import time
import uuid
from typing import Any

from fastapi import HTTPException, Query, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from src.core.components.base.router import BaseRouter
from src.kernel.logger import get_logger

from .adapter import PLATFORM, get_desktop_pet_adapter

logger = get_logger("DesktopPetRouter", color="#F5A6C8")


class DesktopPetMessageRequest(BaseModel):
    content: str = Field(min_length=1)
    user_id: str = "desktop_owner"
    nickname: str = "主人"
    client_id: str = "default"
    message_type: str = "text"


class DesktopPetRouter(BaseRouter):
    router_name = "desktop_pet"
    router_description = "本地桌宠桥接 API"
    custom_route_path = "/desktop-pet"
    cors_origins = ["*"]

    def register_endpoints(self) -> None:
        def _get_adapter():
            adapter = get_desktop_pet_adapter()
            if adapter is None:
                raise HTTPException(status_code=503, detail="DesktopPetAdapter 尚未就绪")
            return adapter

        @self.app.get("/api/status")
        async def status() -> dict[str, Any]:
            """返回桌宠桥接运行状态。"""
            adapter = get_desktop_pet_adapter()
            return {
                "ok": adapter is not None,
                "platform": PLATFORM,
                "adapter_ready": adapter is not None,
                "connected_clients": adapter.connected_client_count() if adapter else 0,
                "time": time.time(),
            }

        @self.app.post("/api/message")
        async def send_message(request: DesktopPetMessageRequest) -> dict[str, Any]:
            adapter = _get_adapter()
            raw_message = {
                "message_id": f"desktop_{uuid.uuid4().hex}",
                "user_id": request.user_id,
                "nickname": request.nickname,
                "content": request.content,
                "timestamp": time.time(),
                "message_type": request.message_type,
                "client_id": request.client_id,
            }
            try:
                await adapter.send_message(raw_message)
                return {"success": True, "message": raw_message}
            except Exception as exc:
                logger.error(f"桌宠消息投递失败: {exc}", exc_info=True)
                raise HTTPException(status_code=500, detail=str(exc))

        @self.app.get("/api/poll")
        async def poll(user_id: str | None = Query(None)) -> dict[str, Any]:
            adapter = _get_adapter()
            messages = await adapter.get_pending_responses(user_id=user_id)
            return {"messages": messages}

        @self.app.websocket("/ws")
        async def websocket_endpoint(websocket: WebSocket) -> None:
            adapter = get_desktop_pet_adapter()
            if adapter is None:
                await websocket.close(code=1013)
                return
            await websocket.accept()
            await adapter.register_client(websocket)
            try:
                await websocket.send_json({"type": "hello", "platform": PLATFORM})
                while True:
                    data = await websocket.receive_json()
                    if not isinstance(data, dict):
                        continue
                    if data.get("type") == "ping":
                        await websocket.send_json({"type": "pong", "time": time.time()})
                    elif data.get("type") == "message":
                        content = str(data.get("content") or "").strip()
                        if content:
                            await adapter.send_message({
                                "message_id": f"desktop_ws_{uuid.uuid4().hex}",
                                "user_id": str(data.get("user_id") or "desktop_owner"),
                                "nickname": str(data.get("nickname") or "主人"),
                                "content": content,
                                "timestamp": time.time(),
                                "message_type": str(data.get("message_type") or "text"),
                                "client_id": str(data.get("client_id") or "default"),
                            })
            except WebSocketDisconnect:
                pass
            except Exception as exc:
                logger.warning(f"桌宠 WebSocket 连接异常: {exc}")
            finally:
                await adapter.unregister_client(websocket)
