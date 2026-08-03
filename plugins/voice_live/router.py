"""Secure same-origin browser and OBS gateway for Voice Live."""

from __future__ import annotations

import asyncio
import json
import os
import secrets
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse

from src.core.components.base.router import BaseRouter
from src.kernel.logger import get_logger

from .auth import TicketAuthority
from .protocol import SessionState
from .secrets import secret_readiness
from .session import CallSession

logger = get_logger("voice_live.router", display="Voice Live")
STATIC_DIR = Path(__file__).parent / "static"


class VoiceLiveRouter(BaseRouter):
    """Issue single-use tickets and own calls plus read-only OBS observers."""

    router_name = "voice_live"
    router_description = "商业级全双工实时语音通话"
    custom_route_path = "/voice-live"
    cors_origins: list[str] | None = None

    def __init__(self, plugin: Any) -> None:
        self._sessions: dict[str, CallSession] = {}
        self._sessions_lock = asyncio.Lock()
        self._observers: set[WebSocket] = set()
        config = plugin.config
        self.custom_route_path = config.server.route_path
        environment_name = str(config.server.ticket_secret_env or "").strip()
        configured_secret = os.environ.get(environment_name, "") if environment_name else ""
        ticket_secret = (
            configured_secret.encode("utf-8") if configured_secret else secrets.token_bytes(32)
        )
        self._ticket_authority = TicketAuthority(
            ticket_secret, config.server.ticket_ttl_seconds
        )
        super().__init__(plugin)

    @property
    def active_session_count(self) -> int:
        return sum(1 for session in self._sessions.values() if session.is_active)

    def session_snapshots(self) -> list[dict[str, Any]]:
        return [session.snapshot() for session in self._sessions.values()]

    def _readiness_snapshot(self) -> dict[str, Any]:
        """Return redacted local prerequisites without contacting dependencies."""

        config = self.plugin.config
        provider = config.full_duplex
        reasons: list[str] = []
        provider_enabled = provider.provider_type != "disabled"
        provider_endpoint = bool(str(provider.upstream_url or "").strip())
        provider_credential = True
        if provider.provider_type in {"qwen_realtime", "openai_realtime"}:
            provider_credential, reason = secret_readiness(
                provider.api_key_env,
                provider.api_key_file,
                label="Voice Live provider",
            )
            if reason:
                reasons.append(reason)
        if not provider_enabled:
            reasons.append("Voice Live provider is disabled")
        elif not provider_endpoint:
            reasons.append("Voice Live provider endpoint is not configured")

        conversion = config.voice_conversion
        conversion_credential = True
        conversion_endpoint = True
        if conversion.enabled:
            conversion_endpoint = bool(str(conversion.service_url or "").strip())
            conversion_credential, reason = secret_readiness(
                conversion.token_env,
                conversion.token_file,
                label="Voice conversion",
            )
            if not conversion_endpoint:
                reasons.append("Voice conversion endpoint is not configured")
            if reason:
                reasons.append(reason)

        ready = (
            provider_enabled
            and provider_endpoint
            and provider_credential
            and conversion_endpoint
            and conversion_credential
        )
        return {
            "ready": ready,
            "provider_credential": provider_credential,
            "voice_conversion": (
                "disabled"
                if not conversion.enabled
                else "ready"
                if conversion_endpoint and conversion_credential
                else "degraded"
            ),
            "degraded_reasons": reasons,
        }

    def register_endpoints(self) -> None:
        @self.app.get("/", response_class=HTMLResponse)
        async def index() -> HTMLResponse:
            return self._static_html("voice_live.html")

        @self.app.get("/overlay", response_class=HTMLResponse)
        async def overlay() -> HTMLResponse:
            return self._static_html("overlay.html")

        @self.app.post("/ticket")
        async def ticket(request: Request) -> JSONResponse:
            origin = request.headers.get("origin", "")
            if not self._origin_allowed(origin, request.headers.get("host", "")):
                raise HTTPException(status_code=403, detail="Origin 不在 Voice Live 白名单")
            return JSONResponse(
                {
                    "ticket": self._issue_ticket(),
                    "expires_in": self.plugin.config.server.ticket_ttl_seconds,
                },
                headers={"Cache-Control": "no-store"},
            )

        @self.app.get("/health")
        async def health() -> JSONResponse:
            provider = self.plugin.config.full_duplex
            readiness = self._readiness_snapshot()
            return JSONResponse(
                {
                    "status": "ok" if readiness["ready"] else "degraded",
                    "protocol": 1,
                    "provider": provider.provider_type,
                    "model": provider.model_name,
                    "configured": readiness["ready"],
                    "readiness": readiness,
                    "active_sessions": self.active_session_count,
                    "sessions": self.session_snapshots(),
                    "observers": len(self._observers),
                }
            )

        @self.app.websocket("/ws")
        async def websocket_endpoint(websocket: WebSocket) -> None:
            await self._handle_websocket(websocket)

        @self.app.websocket("/observe")
        async def observer_endpoint(websocket: WebSocket) -> None:
            await self._handle_observer(websocket)

    def _static_html(self, name: str) -> HTMLResponse:
        path = STATIC_DIR / name
        if not path.is_file():
            return HTMLResponse(f"Voice Live asset missing: {name}", status_code=404)
        return HTMLResponse(path.read_text(encoding="utf-8"))

    def _origin_allowed(self, origin: str, host: str = "") -> bool:
        try:
            candidate = urlsplit(origin)
        except ValueError:
            return False
        if candidate.scheme not in {"http", "https"} or not candidate.hostname:
            return False
        if host and candidate.netloc == host:
            return True
        for raw in self.plugin.config.server.allowed_origins:
            try:
                allowed = urlsplit(str(raw).rstrip("/"))
            except ValueError:
                continue
            if candidate.scheme != allowed.scheme or candidate.hostname != allowed.hostname:
                continue
            if allowed.port is None or candidate.port == allowed.port:
                return True
        return False

    def _issue_ticket(self) -> str:
        return self._ticket_authority.issue()

    def _consume_ticket(self, ticket: str) -> bool:
        return self._ticket_authority.consume(ticket)

    async def _accept_ticket_socket(self, websocket: WebSocket) -> bool:
        origin = websocket.headers.get("origin", "")
        host = websocket.headers.get("host", "")
        ticket = websocket.query_params.get("ticket", "")
        if not self._origin_allowed(origin, host) or not self._consume_ticket(ticket):
            await websocket.close(code=1008, reason="invalid or expired Voice Live ticket")
            return False
        await websocket.accept()
        return True

    async def _handle_websocket(self, websocket: WebSocket) -> None:
        if not await self._accept_ticket_socket(websocket):
            return
        config = self.plugin.config
        session = CallSession(config)
        async with self._sessions_lock:
            if len(self._sessions) >= config.server.max_concurrent_sessions:
                await websocket.close(code=1013, reason="Voice Live session capacity reached")
                return
            self._sessions[session.session_id] = session

        async def send_json(data: dict[str, Any]) -> None:
            await websocket.send_json(data)
            await self._broadcast_json(data)

        async def send_bytes(data: bytes) -> None:
            await websocket.send_bytes(data)
            await self._broadcast_bytes(data)

        session.set_send_callbacks(send_json, send_bytes)
        reason = "disconnect"
        logger.info(f"Voice Live WebSocket 已连接: {session.session_id}")
        try:
            async with asyncio.timeout(config.server.max_session_minutes * 60):
                while session.state not in {SessionState.ENDED, SessionState.FAILED}:
                    try:
                        message = await asyncio.wait_for(
                            websocket.receive(), timeout=config.server.idle_timeout_seconds
                        )
                    except TimeoutError:
                        reason = "idle_timeout"
                        break
                    if message.get("type") == "websocket.disconnect":
                        break
                    if message.get("bytes") is not None:
                        await session.handle_audio(message["bytes"])
                    elif message.get("text") is not None:
                        payload = json.loads(message["text"])
                        if not isinstance(payload, dict):
                            raise ValueError("Voice Live control event must be an object")
                        await session.handle_message(payload)
        except WebSocketDisconnect:
            pass
        except TimeoutError:
            reason = "session_limit"
        except Exception as exc:
            reason = "transport_error"
            logger.error(f"Voice Live WebSocket 异常: {exc}", exc_info=True)
            try:
                await websocket.send_json(
                    {"type": "error", "message": str(exc), "fatal": True}
                )
            except Exception:
                pass
        finally:
            await session.stop(reason=reason)
            async with self._sessions_lock:
                self._sessions.pop(session.session_id, None)

    async def _handle_observer(self, websocket: WebSocket) -> None:
        if not await self._accept_ticket_socket(websocket):
            return
        self._observers.add(websocket)
        await websocket.send_json(
            {"type": "observer.ready", "protocol": 1, "sessions": self.session_snapshots()}
        )
        try:
            while True:
                message = await websocket.receive()
                if message.get("type") == "websocket.disconnect":
                    return
                if message.get("text"):
                    event = json.loads(message["text"])
                    if event.get("type") == "ping":
                        await websocket.send_json({"type": "pong"})
        except WebSocketDisconnect:
            pass
        finally:
            self._observers.discard(websocket)

    async def _broadcast_json(self, data: dict[str, Any]) -> None:
        stale: list[WebSocket] = []
        for observer in tuple(self._observers):
            try:
                await observer.send_json(data)
            except Exception:
                stale.append(observer)
        for observer in stale:
            self._observers.discard(observer)

    async def _broadcast_bytes(self, data: bytes) -> None:
        stale: list[WebSocket] = []
        for observer in tuple(self._observers):
            try:
                await observer.send_bytes(data)
            except Exception:
                stale.append(observer)
        for observer in stale:
            self._observers.discard(observer)

    async def stop_all(self, *, reason: str = "router_shutdown") -> None:
        async with self._sessions_lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        await asyncio.gather(
            *(session.stop(reason=reason) for session in sessions),
            return_exceptions=True,
        )

    async def startup(self) -> None:
        logger.info(
            f"Voice Live 路由已启动: provider={self.plugin.config.full_duplex.provider_type}"
        )

    async def shutdown(self) -> None:
        await self.stop_all()
        observers = tuple(self._observers)
        self._observers.clear()
        for observer in observers:
            try:
                await observer.close(code=1001)
            except Exception:
                pass
        logger.info("Voice Live 路由已关闭")


__all__ = ["VoiceLiveRouter"]
