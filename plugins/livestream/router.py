"""Secure manual control plane and acknowledged OBS/browser stage."""

from __future__ import annotations

import asyncio
import json
import os
import secrets
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from src.core.components.base.router import BaseRouter
from src.kernel.logger import get_logger

from .auth import TicketAuthority
from .domain import LIVESTREAM_PROTOCOL_VERSION, StageMessage
from .runtime import LivestreamRuntime
from .stage import StageHub, StageProtocolError

logger = get_logger("livestream.router", display="直播控制台")
STATIC_DIR = Path(__file__).parent / "static"


class SayRequest(BaseModel):
    text: str = Field(min_length=1, max_length=10000)


class LivestreamRouter(BaseRouter):
    """Expose read-only health and authenticated, manual state mutations."""

    router_name = "livestream"
    router_description = "B站直播导演、舞台与回放控制面"
    custom_route_path = "/livestream"
    cors_origins: list[str] | None = None

    def __init__(
        self,
        plugin: Any,
        *,
        stage: StageHub | None = None,
        runtime: LivestreamRuntime | None = None,
    ) -> None:
        self._config = plugin.config
        self.custom_route_path = self._config.server.route_path
        self._stage = stage or StageHub(
            send_timeout_seconds=self._config.server.stage_send_timeout_seconds,
            max_clients=self._config.server.max_stage_clients,
        )
        self._runtime = runtime or LivestreamRuntime(self._config, self._stage)
        self.cors_origins = list(self._config.server.allowed_origins) or None
        environment_name = str(self._config.server.ticket_secret_env or "").strip()
        configured = os.environ.get(environment_name, "") if environment_name else ""
        secret = configured.encode("utf-8") if configured else secrets.token_bytes(32)
        self._tickets = TicketAuthority(
            secret,
            self._config.server.ticket_ttl_seconds,
        )
        super().__init__(plugin)

    def register_endpoints(self) -> None:
        @self.app.get("/", response_class=HTMLResponse)
        async def index() -> HTMLResponse:
            return self._asset("index.html", "text/html; charset=utf-8")

        @self.app.get("/app.js")
        async def app_js() -> Response:
            return self._asset("app.js", "application/javascript; charset=utf-8")

        @self.app.get("/style.css")
        async def style_css() -> Response:
            return self._asset("style.css", "text/css; charset=utf-8")

        @self.app.post("/ticket")
        async def ticket(request: Request) -> JSONResponse:
            self._require_allowed_request(request)
            return JSONResponse(
                {
                    "ticket": self._tickets.issue(),
                    "expires_in": self._config.server.ticket_ttl_seconds,
                },
                headers={"Cache-Control": "no-store"},
            )

        @self.app.get("/health")
        async def health() -> JSONResponse:
            snapshot = await self._runtime.health()
            return JSONResponse(
                {
                    "protocol": LIVESTREAM_PROTOCOL_VERSION,
                    **snapshot.model_dump(mode="json"),
                },
                headers={"Cache-Control": "no-store"},
            )

        @self.app.post("/api/start")
        async def start(request: Request) -> JSONResponse:
            self._authorize_mutation(request)
            session_id = await self._runtime.start()
            return JSONResponse({"status": "running", "session_id": session_id})

        @self.app.post("/api/stop")
        async def stop(request: Request) -> JSONResponse:
            self._authorize_mutation(request)
            await self._runtime.stop(reason="authenticated operator stop")
            return JSONResponse({"status": "stopped"})

        @self.app.post("/api/interrupt")
        async def interrupt(request: Request) -> JSONResponse:
            self._authorize_mutation(request)
            interrupted = await self._runtime.interrupt()
            return JSONResponse({"interrupted": interrupted})

        @self.app.post("/api/say")
        async def say(request: Request, body: SayRequest) -> JSONResponse:
            self._authorize_mutation(request)
            utterance_id = await self._runtime.manual_say(body.text)
            return JSONResponse({"utterance_id": utterance_id})

        @self.app.websocket("/ws")
        async def websocket_endpoint(websocket: WebSocket) -> None:
            await self._handle_stage_socket(websocket)

    def _asset(self, name: str, media_type: str) -> Response:
        path = STATIC_DIR / name
        if not path.is_file():
            return Response(f"livestream asset missing: {name}", status_code=404)
        return Response(
            path.read_text(encoding="utf-8"),
            media_type=media_type,
            headers={
                "Cache-Control": "no-store",
                "Content-Security-Policy": (
                    "default-src 'self'; script-src 'self'; style-src 'self'; "
                    "connect-src 'self' ws: wss:; img-src 'self' data:; "
                    "media-src 'self' blob:; object-src 'none'; base-uri 'none'; "
                    "form-action 'self'"
                ),
                "Referrer-Policy": "no-referrer",
                "X-Content-Type-Options": "nosniff",
            },
        )

    def _origin_allowed(self, origin: str, host: str) -> bool:
        try:
            candidate = urlsplit(origin)
            request_host = urlsplit(f"//{host}")
            default_port = 443 if candidate.scheme == "https" else 80
            candidate_port = candidate.port or default_port
            request_port = request_host.port or default_port
        except ValueError:
            return False
        if candidate.scheme not in {"http", "https"} or not candidate.hostname:
            return False
        if (
            request_host.hostname
            and candidate.hostname.casefold() == request_host.hostname.casefold()
            and candidate_port == request_port
        ):
            return True
        for raw in self._config.server.allowed_origins:
            try:
                allowed = urlsplit(str(raw).rstrip("/"))
                allowed_default_port = 443 if allowed.scheme == "https" else 80
                allowed_port = allowed.port or allowed_default_port
            except ValueError:
                continue
            if (
                candidate.scheme != allowed.scheme
                or not allowed.hostname
                or candidate.hostname.casefold() != allowed.hostname.casefold()
            ):
                continue
            if candidate_port == allowed_port:
                return True
        return False

    def _require_allowed_request(self, request: Request) -> None:
        origin = request.headers.get("origin", "")
        host = request.headers.get("host", "")
        if origin and self._origin_allowed(origin, host):
            return
        client_host = request.client.host if request.client else ""
        if not origin and client_host in {"127.0.0.1", "::1"}:
            return
        raise HTTPException(status_code=403, detail="livestream origin is not allowed")

    def _authorize_mutation(self, request: Request) -> None:
        self._require_allowed_request(request)
        authorization = request.headers.get("authorization", "")
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not self._tickets.consume(token):
            raise HTTPException(
                status_code=401,
                detail="invalid, expired, or replayed livestream ticket",
            )

    async def _handle_stage_socket(self, websocket: WebSocket) -> None:
        origin = websocket.headers.get("origin", "")
        host = websocket.headers.get("host", "")
        ticket = websocket.query_params.get("ticket", "")
        if not self._origin_allowed(origin, host) or not self._tickets.consume(ticket):
            await websocket.close(code=1008, reason="invalid livestream stage ticket")
            return
        if self._stage.client_count >= self._config.server.max_stage_clients:
            await websocket.close(code=1013, reason="livestream stage capacity reached")
            return
        client_id = websocket.query_params.get("client_id", "") or uuid4().hex
        request_primary = websocket.query_params.get("primary", "1") == "1"
        await websocket.accept()
        try:
            is_primary = await self._stage.attach(
                client_id,
                websocket,
                request_primary=request_primary,
            )
        except StageProtocolError as exc:
            await websocket.close(code=1008, reason=str(exc))
            return
        try:
            await asyncio.wait_for(
                websocket.send_json(
                    StageMessage(
                        type="stage.ready",
                        payload={
                            "client_id": client_id,
                            "primary": is_primary,
                            "primary_stage_connected": (
                                self._stage.primary_client_id is not None
                            ),
                            "runtime_status": self._runtime.state,
                        },
                    ).model_dump(mode="json")
                ),
                timeout=self._config.server.stage_send_timeout_seconds,
            )
            missed_heartbeat = False
            while True:
                try:
                    raw = await asyncio.wait_for(
                        websocket.receive_text(),
                        timeout=self._config.server.ws_heartbeat_interval,
                    )
                except TimeoutError:
                    if missed_heartbeat:
                        raise StageProtocolError("stage heartbeat timed out")
                    await asyncio.wait_for(
                        websocket.send_json(
                            StageMessage(type="ping").model_dump(mode="json")
                        ),
                        timeout=self._config.server.stage_send_timeout_seconds,
                    )
                    missed_heartbeat = True
                    continue
                payload = json.loads(raw)
                if not isinstance(payload, dict):
                    raise StageProtocolError("stage message must be an object")
                await self._stage.handle_message(client_id, payload)
                if payload.get("type") == "pong":
                    missed_heartbeat = False
        except WebSocketDisconnect:
            pass
        except (json.JSONDecodeError, StageProtocolError, TimeoutError) as exc:
            logger.warning(f"直播舞台协议错误: client={client_id} error={exc}")
            try:
                await websocket.close(code=1008, reason="stage protocol error")
            except Exception as close_error:  # noqa: BLE001
                logger.debug(f"关闭违规舞台连接失败: {close_error}")
        finally:
            await self._stage.detach(client_id)

    async def startup(self) -> None:
        # Manual-start invariant: mounting the router never connects Bilibili,
        # starts TTS, creates a consciousness, or schedules the director.
        logger.info("直播控制台已就绪；等待操作者手动开始")

    async def shutdown(self) -> None:
        try:
            if self._runtime.state != "stopped":
                await self._runtime.stop(reason="application shutdown")
        finally:
            await self._stage.close()
