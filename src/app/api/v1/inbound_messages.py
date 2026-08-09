"""Inbound message injection: hand app-originated messages to the standard pipeline.

独立应用收到用户消息后，通过 ``POST /chat/messages:inject`` 把消息交给
Elysium 主链（``ON_MESSAGE_RECEIVED`` → Distributor → Chatter）触发爱莉
思考；回复由发送/回复命令端点发回应用侧，本端点不直接产生对外发送，
因此不依赖任何平台 Adapter。
"""

# FastAPI dependencies are intentionally declared in endpoint defaults.
# ruff: noqa: B008

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends

from src.core.components.types import EventType
from src.core.managers.event_manager import get_event_manager
from src.core.models.message import Message

from .auth_store import SessionRecord
from .chat import ChatQueryFailure, ChatQueryService
from .runtime import ERROR_RESPONSES, APIError
from .schemas.inbound_message import (
    InboundMessageInjectRequest,
    InboundMessageInjectResult,
)

_INJECT_PLATFORM = "api"


class InboundInjector:
    """Publish one app-originated message into the standard receive pipeline."""

    def __init__(
        self,
        *,
        queries: ChatQueryService,
        store_provider: Callable[[], Any] | None = None,
    ) -> None:
        self._queries = queries
        self._store_provider = store_provider or getattr(
            queries, "_store_provider", None
        )

    def _event_manager(self) -> Any:
        return get_event_manager()

    async def inject(
        self,
        *,
        request: InboundMessageInjectRequest,
        session: SessionRecord,
    ) -> InboundMessageInjectResult:
        # 1. 解析 platform/chat_type：
        #    显式提供时直接采用（快速路径，不扫描账本，也不依赖平台历史投影）；
        #    省略时从权威账本投影该 stream 的既有 provider 身份；账本不可投影则拒绝。
        platform = (request.platform or "").strip()
        chat_type = (request.chat_type or "").strip()
        if not platform or not chat_type:
            try:
                summary, identity = await self._queries.find_stream_target(
                    request.stream_id,
                    session,
                )
            except ChatQueryFailure as exc:
                if exc.code == "resource_not_found":
                    raise APIError(
                        "stream_not_found",
                        "请求的聊天流不存在，无法注入消息。",
                        status_code=404,
                    ) from exc
                raise APIError(
                    exc.code,
                    exc.message,
                    status_code=exc.status_code,
                    retryable=exc.retryable,
                ) from exc

            platform = str(
                identity.get("platform") or summary.provider or _INJECT_PLATFORM
            )
            chat_type = str(identity.get("chat_type") or summary.chat_type or "private")

        message_id = f"inject_{uuid4().hex}"
        message = Message(
            message_id=message_id,
            content=request.content,
            processed_plain_text=request.content,
            sender_id=request.sender_id or f"api_{session.actor_id}",
            sender_name=request.sender_name or request.sender_cardname or "外部应用",
            sender_cardname=request.sender_cardname,
            platform=platform,
            chat_type=chat_type,
            stream_id=request.stream_id,
            extra={
                "api_injected": True,
                "api_actor_id": session.actor_id,
                "api_session_id": session.session_id,
            },
        )

        event_manager = self._event_manager()
        await event_manager.publish_event(
            EventType.ON_MESSAGE_RECEIVED,
            {
                "message": message,
                "envelope": {},
                "adapter_signature": "api-inject",
            },
        )
        return InboundMessageInjectResult(
            message_id=message_id,
            stream_id=request.stream_id,
            accepted=True,
        )


def create_inbound_inject_router(
    *,
    injector: InboundInjector,
    require_scope: Callable[..., Callable[[SessionRecord], SessionRecord]],
) -> APIRouter:
    """Public route for injecting inbound messages into the standard pipeline."""

    router = APIRouter()

    async def write_session(
        session: SessionRecord = Depends(require_scope("chat:write")),
    ) -> SessionRecord:
        return session

    @router.post(
        "/chat/messages:inject",
        response_model=InboundMessageInjectResult,
        status_code=202,
        operation_id="injectChatMessage",
        responses=ERROR_RESPONSES,
    )
    async def inject_message(
        body: InboundMessageInjectRequest,
        session: SessionRecord = Depends(write_session),
    ) -> InboundMessageInjectResult:
        return await injector.inject(request=body, session=session)

    return router


__all__ = [
    "InboundInjector",
    "create_inbound_inject_router",
]
