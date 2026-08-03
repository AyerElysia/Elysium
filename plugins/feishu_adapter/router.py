"""Feishu adapter HTTP routes."""

from __future__ import annotations

import time
import uuid
from typing import Any

from fastapi import HTTPException, Request
from pydantic import BaseModel, Field

from src.core.components.base.router import BaseRouter
from src.kernel.logger import get_logger

from .adapter import PLATFORM, get_feishu_adapter

logger = get_logger("FeishuRouter", color="#00D6B9")


class FeishuLocalMessageRequest(BaseModel):
    content: str = Field(min_length=1)
    open_id: str = "local_feishu_user"
    sender_name: str = "Feishu User"
    chat_id: str = ""
    chat_type: str = "private"


class FeishuRouter(BaseRouter):
    router_name = "feishu"
    router_description = "Feishu self-built app callback API"
    custom_route_path = "/feishu"
    cors_origins = ["*"]

    def register_endpoints(self) -> None:
        def _get_adapter():
            adapter = get_feishu_adapter()
            if adapter is None:
                raise HTTPException(status_code=503, detail="FeishuAdapter 尚未就绪")
            return adapter

        @self.app.get("/api/status")
        async def status() -> dict[str, Any]:
            adapter = get_feishu_adapter()
            config = adapter._config() if adapter is not None else None
            return {
                "ok": adapter is not None,
                "platform": PLATFORM,
                "adapter_ready": adapter is not None,
                "subscription_mode": (
                    config.connection.subscription_mode if config is not None else None
                ),
                "connected": adapter.is_connected() if adapter is not None else False,
                "identity": (
                    adapter.identity_health_snapshot() if adapter is not None else None
                ),
                "time": time.time(),
            }

        @self.app.post("/events")
        async def events(request: Request) -> dict[str, Any]:
            adapter = _get_adapter()
            try:
                payload = await request.json()
            except Exception as exc:
                raise HTTPException(status_code=400, detail="invalid json") from exc

            if not isinstance(payload, dict):
                raise HTTPException(status_code=400, detail="invalid event payload")

            if not adapter.verify_callback_token(payload):
                logger.warning("飞书事件回调 token 校验失败")
                raise HTTPException(status_code=403, detail="invalid verification token")

            if payload.get("type") == "url_verification":
                challenge = payload.get("challenge")
                if not isinstance(challenge, str):
                    raise HTTPException(status_code=400, detail="missing challenge")
                return {"challenge": challenge}

            try:
                return await adapter.handle_event(payload)
            except ValueError as exc:
                logger.warning(f"飞书事件回调不支持: {exc}")
                raise HTTPException(status_code=400, detail=str(exc))
            except Exception as exc:
                logger.error(f"飞书事件处理失败: {exc}", exc_info=True)
                raise HTTPException(status_code=500, detail=str(exc))

        @self.app.post("/api/message")
        async def local_message(request: FeishuLocalMessageRequest) -> dict[str, Any]:
            adapter = _get_adapter()
            raw_message = {
                "event_id": f"local_{uuid.uuid4().hex}",
                "message_id": f"feishu_local_{uuid.uuid4().hex}",
                "open_id": request.open_id,
                "sender_name": request.sender_name,
                "chat_id": request.chat_id,
                "chat_type": request.chat_type,
                "content": request.content,
                "timestamp": time.time(),
            }
            try:
                await adapter.send_message(raw_message)
                return {"success": True, "message": raw_message}
            except Exception as exc:
                logger.error(f"本地飞书消息投递失败: {exc}", exc_info=True)
                raise HTTPException(status_code=500, detail=str(exc))
