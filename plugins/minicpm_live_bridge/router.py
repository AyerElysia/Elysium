"""MiniCPM-o live 外部服务器桥接路由。"""

from __future__ import annotations

import asyncio
import json
import inspect
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import deque
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from src.core.components.base.router import BaseRouter
from src.core.components.types import EventType
from src.core.models.message import Message, MessageType
from src.core.utils.security import VerifiedDep
from src.kernel.logger import get_logger

from .config import MiniCPMLiveBridgeConfig
from .debug_log import live_terminal_log
from .realtime_adapter import build_realtime_adapter

logger = get_logger("MiniCPM_Live", display="MiniCPM Live", color="#A6E3A1")

_PLUGIN_ROOT = Path(__file__).resolve().parent
_STATIC_ROOT = _PLUGIN_ROOT / "static"

_LIVE_BRIDGE_OUTPUT_CONTRACT = (
    "<live_bridge_output_contract>\n"
    "当前调用运行在 live bridge 的本地单轮 API 中，不是 life_chatter 工具执行器本体。\n"
    "你必须沿用上方 life_chatter 的人格、记忆、边界、历史格式和 life_runtime_context 来判断。\n"
    "当你在 life_chatter 中会使用 action-life_send_text 给用户说话时，这里直接输出要口播给用户的正文。\n"
    "不要输出 tool call JSON，不要输出 action 名称，不要把 thought/reason 等元信息写给用户。\n"
    "如果确实需要 nucleus_bash、nucleus_view_screen 或其他工具，当前通道不能直接执行；请用一句自然的话说明你需要用户稍等或改走正式聊天通道。\n"
    "</live_bridge_output_contract>"
)


class LiveSessionCreateRequest(BaseModel):
    client_name: str = "neo-web"
    mode: str = "screen_voice"
    metadata: dict[str, Any] = Field(default_factory=dict)


class LiveEventRequest(BaseModel):
    session_id: str
    event_type: str
    role: str = ""
    text: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)


class LiveTurnRequest(BaseModel):
    session_id: str
    text: str = ""
    screen_image: str = ""
    audio_data: str = ""
    audio_mime_type: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)


class LiveClientLogRequest(BaseModel):
    session_id: str = ""
    level: str = "info"
    source: str = "web"
    message: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)


class MiniCPMLiveRouter(BaseRouter):
    """MiniCPM-o live 外部服务器桥接路由。"""

    router_name = "minicpm_live"
    router_description = "MiniCPM-o live external bridge"
    custom_route_path = "/minicpm-live"
    cors_origins: list[str] = []

    _sessions: dict[str, dict[str, Any]] = {}
    _events: dict[str, list[dict[str, Any]]] = {}
    _max_events_per_session = 300
    _unified_sequence: int = 0
    _unified_events: deque[dict[str, Any]] = deque()
    _unified_connections: set[WebSocket] = set()
    _unified_lock: asyncio.Lock = asyncio.Lock()

    def register_endpoints(self) -> None:
        @self.app.get("/", response_class=HTMLResponse)
        async def get_dashboard() -> HTMLResponse:
            html_path = _STATIC_ROOT / "minicpm_live.html"
            if not html_path.exists():
                return HTMLResponse("<h1>MiniCPM Live page not found</h1>", status_code=404)
            return HTMLResponse(
                html_path.read_text(encoding="utf-8"),
                headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
            )

        @self.app.get("/api/config")
        async def get_client_config(_: str = VerifiedDep) -> dict[str, Any]:
            client_config = self._client_config()
            self._log_live(
                "MiniCPM Live config requested: "
                f"enabled={client_config.get('enabled')} "
                f"mode={client_config.get('mode') or '-'} "
                f"local_api={client_config.get('session', {}).get('enable_local_api_turn')} "
                f"ws={bool(client_config.get('server', {}).get('websocket_url'))}"
            )
            return client_config

        @self.app.post("/api/debug/log")
        async def append_client_debug_log(
            request: LiveClientLogRequest,
            _: str = VerifiedDep,
        ) -> dict[str, Any]:
            config = self._config()
            if not bool(getattr(config.debug, "log_client_events", True)):
                return {"success": True, "logged": False}

            level = str(request.level or "info").strip().lower()
            if level not in {"debug", "info", "warning", "error"}:
                level = "info"

            payload = self._truncate_payload(request.payload)
            payload_preview = ""
            if payload:
                payload_preview = f" payload={self._preview(payload)}"
            self._log_live(
                "MiniCPM Live client log: "
                f"session={self._short_id(request.session_id)} "
                f"source={request.source or 'web'} "
                f"message={self._preview(request.message)}"
                f"{payload_preview}",
                level=level,
            )
            return {"success": True, "logged": True}

        @self.app.get("/api/status")
        async def get_status(_: str = VerifiedDep) -> dict[str, Any]:
            config = self._config()
            client_config = self._client_config()
            if not config.plugin.enabled:
                self._log_live("MiniCPM Live status requested: plugin disabled", level="warning")
                return {
                    "enabled": False,
                    "configured": False,
                    "server_reachable": False,
                    "checked_url": "",
                    "detail": "MiniCPM Live Bridge disabled",
                    "client_config": client_config,
                }

            health_url = self._health_url(config)
            if not health_url:
                self._log_live("MiniCPM Live status requested: external server unset", level="warning")
                return {
                    "enabled": True,
                    "configured": False,
                    "server_reachable": False,
                    "checked_url": "",
                    "detail": "external live server is not configured",
                    "client_config": client_config,
                }

            result = await asyncio.to_thread(
                self._request_json_sync,
                "GET",
                health_url,
                None,
                self._server_headers(config),
                float(config.server.request_timeout_seconds),
            )
            self._log_live(
                "MiniCPM Live status checked: "
                f"url={health_url} reachable={bool(result.get('ok'))} "
                f"status={result.get('status_code') or '-'} detail={self._preview(result.get('detail'))}",
                level="info" if bool(result.get("ok")) else "warning",
            )
            return {
                "enabled": True,
                "configured": True,
                "server_reachable": bool(result.get("ok")),
                "checked_url": health_url,
                "status_code": result.get("status_code"),
                "detail": result.get("detail", ""),
                "body": result.get("body"),
                "client_config": client_config,
            }

        @self.app.post("/api/sessions")
        async def create_session(
            request: LiveSessionCreateRequest,
            _: str = VerifiedDep,
        ) -> dict[str, Any]:
            config = self._config()
            if not config.plugin.enabled:
                raise HTTPException(status_code=503, detail="MiniCPM Live Bridge disabled")

            session_id = f"mclive_{uuid.uuid4().hex}"
            now = time.time()
            local_payload = {
                "session_id": session_id,
                "client_name": request.client_name,
                "mode": request.mode,
                "metadata": request.metadata,
                "created_at": now,
            }

            remote: dict[str, Any] | None = None
            session_url = self._resolve_url(config, config.server.session_url)
            if session_url:
                remote = await asyncio.to_thread(
                    self._request_json_sync,
                    "POST",
                    session_url,
                    local_payload,
                    self._server_headers(config),
                    float(config.server.request_timeout_seconds),
                )
                remote_body = remote.get("body")
                if isinstance(remote_body, dict):
                    remote_session_id = remote_body.get("session_id") or remote_body.get("id")
                    if remote_session_id:
                        session_id = str(remote_session_id)
                        local_payload["session_id"] = session_id

            self._sessions[session_id] = {
                **local_payload,
                "remote": remote,
                "updated_at": now,
            }
            self._events.setdefault(session_id, [])
            self._log_live(
                "MiniCPM Live session created: "
                f"session={self._short_id(session_id)} "
                f"client={request.client_name} mode={request.mode} "
                f"local_api={bool(config.session.enable_local_api_turn)} "
                f"external_ws={bool(self._resolve_ws_url(config, config.server.websocket_url))}"
            )
            return {
                "success": True,
                "session_id": session_id,
                "session": self._sessions[session_id],
                "client_config": self._client_config(session_id=session_id),
            }

        @self.app.post("/api/events")
        async def append_event(request: LiveEventRequest, _: str = VerifiedDep) -> dict[str, Any]:
            if not request.session_id:
                raise HTTPException(status_code=400, detail="session_id is required")

            now = time.time()
            event = {
                "id": f"evt_{uuid.uuid4().hex}",
                "session_id": request.session_id,
                "event_type": request.event_type,
                "role": request.role,
                "text": request.text,
                "payload": request.payload,
                "time": now,
            }
            events = self._events.setdefault(request.session_id, [])
            events.append(event)
            if len(events) > self._max_events_per_session:
                del events[: len(events) - self._max_events_per_session]

            if request.session_id in self._sessions:
                self._sessions[request.session_id]["updated_at"] = now

            self._log_live(
                "MiniCPM Live event: "
                f"session={self._short_id(request.session_id)} "
                f"type={request.event_type} role={request.role or '-'} "
                f"text={self._preview(request.text)} "
                f"payload_keys={list((request.payload or {}).keys())[:8]}"
            )
            await self._ingest_live_event(request, event)

            return {"success": True, "event": event}

        @self.app.post("/api/turn")
        async def local_api_turn(request: LiveTurnRequest, _: str = VerifiedDep) -> dict[str, Any]:
            config = self._config()
            if not config.plugin.enabled:
                raise HTTPException(status_code=503, detail="MiniCPM Live Bridge disabled")
            if not config.session.enable_local_api_turn:
                raise HTTPException(status_code=503, detail="local API turn disabled")
            if not request.session_id:
                raise HTTPException(status_code=400, detail="session_id is required")
            if not (request.text.strip() or request.screen_image.strip() or request.audio_data.strip()):
                raise HTTPException(status_code=400, detail="text, screen_image or audio_data is required")

            t_turn_start = time.perf_counter()
            self._log_live(
                "MiniCPM Live local turn start: "
                f"session={self._short_id(request.session_id)} "
                f"text={self._preview(request.text)} "
                f"has_audio={bool(request.audio_data.strip())} "
                f"audio_mime={request.audio_mime_type or '-'} "
                f"has_screen={bool(request.screen_image.strip())} "
                f"payload_keys={list((request.payload or {}).keys())[:8]}"
            )
            try:
                text = await self._run_local_api_turn(request)
            except Exception as exc:  # noqa: BLE001
                logger.error(f"MiniCPM Live 本地 API 单轮调用失败: {exc}", exc_info=True)
                self._log_live(
                    "MiniCPM Live local turn failed: "
                    f"session={self._short_id(request.session_id)} error={exc}",
                    level="error",
                )
                raise HTTPException(status_code=500, detail=str(exc)) from exc

            self._log_live(
                "MiniCPM Live local turn done: "
                f"session={self._short_id(request.session_id)} "
                f"reply_chars={len(text)} reply={self._preview(text)}"
            )

            tts_audio: str | None = None
            tts_mime_type: str = "audio/wav"
            tts_style = (config.session.tts_style or "").strip()
            t_tts_start = time.perf_counter()
            if tts_style:
                try:
                    tts_service = self._get_tts_service()
                    if tts_service is not None:
                        try:
                            media_type = str(
                                getattr(getattr(tts_service, "_config", None), "tts_advanced", None) and
                                getattr(tts_service._config.tts_advanced, "media_type", None) or "wav"
                            ).strip().lstrip(".") or "wav"
                            tts_mime_type = f"audio/{media_type}"
                        except Exception:
                            pass
                        tts_audio = await tts_service.generate_voice(text, tts_style)
                        if tts_audio:
                            self._log_live(
                                "MiniCPM Live TTS done: "
                                f"session={self._short_id(request.session_id)} "
                                f"style={tts_style} b64_len={len(tts_audio)} mime={tts_mime_type}"
                            )
                        else:
                            self._log_live(
                                "MiniCPM Live TTS returned None, frontend will use browser TTS",
                                level="warning",
                            )
                    else:
                        self._log_live(
                            "MiniCPM Live TTS skipped: tts_voice_plugin service unavailable",
                            level="warning",
                        )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"MiniCPM Live TTS 合成失败: {exc}")

            t_done = time.perf_counter()
            self._log_live(f"Local turn total latency: {(t_done - t_turn_start):.3f}s (TTS: {(t_done - t_tts_start):.3f}s)")
            return {"success": True, "text": text, "tts_audio": tts_audio, "tts_mime_type": tts_mime_type}

        @self.app.post("/api/turn/stream")
        async def local_api_turn_stream(request: LiveTurnRequest, _: str = VerifiedDep) -> StreamingResponse:
            config = self._config()
            if not config.plugin.enabled:
                raise HTTPException(status_code=503, detail="MiniCPM Live Bridge disabled")
            if not config.session.enable_local_api_turn:
                raise HTTPException(status_code=503, detail="local API turn disabled")
            if not request.session_id:
                raise HTTPException(status_code=400, detail="session_id is required")
            if not (request.text.strip() or request.screen_image.strip() or request.audio_data.strip()):
                raise HTTPException(status_code=400, detail="text, screen_image or audio_data is required")

            self._log_live(
                "MiniCPM Live full-duplex turn start: "
                f"session={self._short_id(request.session_id)} "
                f"text={self._preview(request.text)} "
                f"has_audio={bool(request.audio_data.strip())} "
                f"has_screen={bool(request.screen_image.strip())}"
            )

            generator = self._stream_local_api_turn(request)
            return StreamingResponse(
                generator,
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "X-Accel-Buffering": "no",
                    "Connection": "keep-alive",
                },
            )

        @self.app.get("/api/sessions/{session_id}")
        async def get_session(session_id: str, _: str = VerifiedDep) -> JSONResponse:
            session = self._sessions.get(session_id)
            if not session:
                return JSONResponse({"error": "session not found"}, status_code=404)
            return JSONResponse({"session": session})

        @self.app.get("/api/sessions/{session_id}/events")
        async def get_events(session_id: str, _: str = VerifiedDep) -> dict[str, Any]:
            return {
                "session_id": session_id,
                "events": self._events.get(session_id, []),
                "limit": self._max_events_per_session,
            }

        @self.app.get("/api/context")
        async def get_live_context(
            session_id: str = Query("", description="live session id"),
            _: str = VerifiedDep,
        ) -> dict[str, Any]:
            snapshot = await self._build_live_context_snapshot(session_id=session_id)
            self._log_live(
                "MiniCPM Live context snapshot: "
                f"session={self._short_id(session_id)} "
                f"life={snapshot.get('life_runtime_available')} "
                f"life_chars={len(str(snapshot.get('life_runtime_context') or ''))} "
                f"unified={len(snapshot.get('unified_events') or [])} "
                f"session_events={len(snapshot.get('session_events') or [])}"
            )
            return snapshot

        @self.app.get("/api/unified/events")
        async def get_unified_events(
            since: int = Query(0, description="只返回 sequence 大于该值的事件"),
            limit: int = Query(100, ge=1, le=500, description="最大返回数量"),
            _: str = VerifiedDep,
        ) -> dict[str, Any]:
            events = await self.tail_unified_events(since=since, limit=limit)
            latest = events[-1]["sequence"] if events else int(since or 0)
            return {"events": events, "latest_sequence": latest}

        @self.app.websocket("/api/unified/ws")
        async def unified_event_ws(
            websocket: WebSocket,
            api_key: str = Query(..., description="API 密钥"),
        ) -> None:
            if not self._is_valid_api_key(api_key):
                await websocket.close(code=4003, reason="无效的 API 密钥")
                return

            await websocket.accept()
            async with self._unified_lock:
                self._unified_connections.add(websocket)
                snapshot = list(self._unified_events)[-50:]
                connection_count = len(self._unified_connections)
            self._log_live(
                "MiniCPM Live unified ws connected: "
                f"connections={connection_count} snapshot={len(snapshot)}"
            )

            try:
                await websocket.send_json({"type": "snapshot", "events": snapshot})
                while True:
                    text = await websocket.receive_text()
                    if not text or text.strip() == "ping":
                        await websocket.send_json({"type": "pong"})
            except WebSocketDisconnect:
                pass
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"MiniCPM Live unified ws 断开: {exc}")
            finally:
                async with self._unified_lock:
                    self._unified_connections.discard(websocket)
                    connection_count = len(self._unified_connections)
                self._log_live(
                    "MiniCPM Live unified ws disconnected: "
                    f"connections={connection_count}"
                )

        @self.app.websocket("/api/realtime/ws")
        async def realtime_proxy_ws(
            websocket: WebSocket,
            session_id: str = Query(..., description="live session id"),
            api_key: str = Query(..., description="API 密钥"),
        ) -> None:
            if not self._is_valid_api_key(api_key):
                await websocket.close(code=4003, reason="无效的 API 密钥")
                return
            await self._serve_realtime_proxy(websocket=websocket, session_id=session_id)

    def _config(self) -> MiniCPMLiveBridgeConfig:
        config = getattr(self.plugin, "config", None)
        if isinstance(config, MiniCPMLiveBridgeConfig):
            return config
        return MiniCPMLiveBridgeConfig()

    def _terminal_log_enabled(self) -> bool:
        return bool(getattr(self._config().debug, "terminal_log_enabled", True))

    def _terminal_preview_chars(self) -> int:
        try:
            return max(80, int(getattr(self._config().debug, "preview_chars", 360) or 360))
        except (TypeError, ValueError):
            return 360

    def _log_live(self, message: str, *, level: str = "info") -> None:
        live_terminal_log(logger, self._config(), message, level=level)

    def _preview(self, value: Any, *, limit: int | None = None) -> str:
        max_chars = self._terminal_preview_chars() if limit is None else max(20, int(limit))
        if isinstance(value, str):
            text = value
        else:
            try:
                text = json.dumps(value, ensure_ascii=False, default=str)
            except Exception:
                text = str(value)
        text = " ".join(str(text or "").split())
        if len(text) <= max_chars:
            return text
        return text[: max_chars - 1] + "…"

    @staticmethod
    def _short_id(value: str) -> str:
        text = str(value or "")
        if len(text) <= 12:
            return text
        return text[:8] + "…"

    @classmethod
    async def tail_unified_events(cls, *, since: int = 0, limit: int = 100) -> list[dict[str, Any]]:
        async with cls._unified_lock:
            events = [
                dict(event)
                for event in cls._unified_events
                if int(event.get("sequence") or 0) > int(since or 0)
            ]
        return events[-max(1, int(limit or 1)) :]

    @classmethod
    async def publish_unified_event(
        cls,
        event: dict[str, Any],
        *,
        max_backlog: int = 800,
    ) -> dict[str, Any]:
        payload = dict(event)
        async with cls._unified_lock:
            cls._unified_sequence += 1
            payload.setdefault("sequence", cls._unified_sequence)
            payload.setdefault("published_at", time.time())
            cls._unified_events.append(payload)
            backlog = max(50, int(max_backlog or 800))
            while len(cls._unified_events) > backlog:
                cls._unified_events.popleft()
            connections = list(cls._unified_connections)

        if connections:
            message = {"type": "unified.event", "event": payload}
            disconnected: set[WebSocket] = set()
            for websocket in connections:
                try:
                    await websocket.send_json(message)
                except Exception:
                    disconnected.add(websocket)
            if disconnected:
                async with cls._unified_lock:
                    cls._unified_connections.difference_update(disconnected)

        return payload

    @staticmethod
    def _get_tts_service() -> Any | None:
        """获取 tts_voice_plugin 的 TTSService 实例，失败时返回 None。"""
        try:
            from src.core.managers import get_plugin_manager

            plugin = get_plugin_manager().get_plugin("tts_voice_plugin")
            service = getattr(plugin, "tts_service", None) if plugin is not None else None
            if service is not None and hasattr(service, "generate_voice"):
                return service
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"MiniCPM Live 获取 TTSService (plugin manager) 失败: {exc}")

        try:
            from src.app.plugin_system.api.service_api import get_service

            service = get_service("tts_voice_plugin:service:tts")
            if service is not None and hasattr(service, "generate_voice"):
                return service
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"MiniCPM Live 获取 TTSService (service API) 失败: {exc}")

        return None

    @staticmethod
    def _is_valid_api_key(api_key: str) -> bool:
        try:
            from src.core.config.core_config import get_core_config

            valid_keys = get_core_config().http_router.api_keys
            return bool(valid_keys and api_key in valid_keys)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"MiniCPM Live WebSocket API key 校验失败: {exc}")
            return False

    @staticmethod
    def _transport_mode(config: MiniCPMLiveBridgeConfig) -> str:
        mode = str(getattr(config.server, "transport_mode", "browser_direct") or "browser_direct").strip().lower()
        if mode not in {"browser_direct", "neo_proxy"}:
            return "browser_direct"
        return mode

    @staticmethod
    def _protocol_adapter_name(config: MiniCPMLiveBridgeConfig) -> str:
        name = str(getattr(config.server, "protocol_adapter", "passthrough") or "passthrough").strip().lower()
        if name not in {"passthrough", "minicpm_realtime_v0"}:
            return "passthrough"
        return name

    def _upstream_websocket_url(self, config: MiniCPMLiveBridgeConfig, *, session_id: str = "") -> str:
        url = self._resolve_ws_url(config, config.server.websocket_url)
        return self._with_session_placeholder(url, session_id)

    def _client_websocket_url(self, config: MiniCPMLiveBridgeConfig, *, session_id: str = "") -> str:
        upstream_url = self._upstream_websocket_url(config, session_id=session_id)
        if not upstream_url:
            return ""
        if self._transport_mode(config) == "neo_proxy":
            proxy_url = (
                f"{self.custom_route_path}/api/realtime/ws"
                f"?session_id={{session_id}}&api_key={{api_key}}"
            )
            return self._with_session_placeholder(proxy_url, session_id)
        return upstream_url

    def _client_config(self, *, session_id: str = "") -> dict[str, Any]:
        config = self._config()
        external_frontend_url = self._resolve_url(config, config.server.frontend_url)
        websocket_url = self._client_websocket_url(config, session_id=session_id)
        upstream_websocket_url = self._upstream_websocket_url(config, session_id=session_id)
        session_url = self._resolve_url(config, config.server.session_url)
        livekit_url = self._resolve_ws_url(config, config.server.livekit_url)
        token_url = self._resolve_url(config, config.server.token_url)
        transport_mode = self._transport_mode(config)
        protocol_adapter = self._protocol_adapter_name(config)

        mode = "unconfigured"
        if websocket_url:
            mode = "neo_proxy_ws" if transport_mode == "neo_proxy" else "ws_ingest"
        elif external_frontend_url:
            mode = "external_frontend"
        elif bool(config.session.enable_local_api_turn):
            mode = "local_api_turn"
        elif livekit_url or token_url:
            mode = "webrtc_pending_adapter"

        return {
            "enabled": bool(config.plugin.enabled),
            "mode": mode,
            "server": {
                "base_url": config.server.base_url.strip(),
                "health_url": self._health_url(config),
                "session_url": session_url,
                "websocket_url": websocket_url,
                "upstream_websocket_url": upstream_websocket_url,
                "transport_mode": transport_mode,
                "protocol_adapter": protocol_adapter,
                "frontend_url": external_frontend_url,
                "livekit_url": livekit_url,
                "token_url": token_url,
            },
            "capture": {
                "screen_fps": float(config.capture.screen_fps),
                "screen_max_width": int(config.capture.screen_max_width),
                "jpeg_quality": float(config.capture.jpeg_quality),
                "audio_mime_type": config.capture.audio_mime_type,
                "audio_chunk_ms": int(config.capture.audio_chunk_ms),
            },
            "session": {
                "stream_id": config.session.stream_id,
                "stream_name": config.session.stream_name,
                "user_id": config.session.user_id,
                "user_name": config.session.user_name,
                "assistant_id": config.session.assistant_id,
                "assistant_name": config.session.assistant_name,
                "model_task_name": config.session.model_task_name,
                "enable_local_api_turn": bool(config.session.enable_local_api_turn),
                "dispatch_user_transcript_to_chatter": bool(
                    config.session.dispatch_user_transcript_to_chatter
                ),
                "tts_style": config.session.tts_style,
                "full_duplex_default": bool(config.session.full_duplex_default),
                "full_duplex_sentence_min_chars": int(config.session.full_duplex_sentence_min_chars),
            },
            "unified_event_stream": {
                "sync_core_events_to_live": bool(config.unified_event_stream.sync_core_events_to_live),
                "ignore_live_echo_to_live": bool(config.unified_event_stream.ignore_live_echo_to_live),
                "max_backlog_events": int(config.unified_event_stream.max_backlog_events),
                "record_screen_summary_to_life": bool(
                    config.unified_event_stream.record_screen_summary_to_life
                ),
            },
            "context": {
                "include_life_runtime_context": bool(config.context.include_life_runtime_context),
                "include_unified_events": bool(config.context.include_unified_events),
                "include_live_session_events": bool(config.context.include_live_session_events),
                "life_event_limit": int(config.context.life_event_limit),
                "unified_event_limit": int(config.context.unified_event_limit),
                "live_session_event_limit": int(config.context.live_session_event_limit),
                "mark_life_context_seen": bool(config.context.mark_life_context_seen),
            },
            "debug": {
                "terminal_log_enabled": bool(config.debug.terminal_log_enabled),
                "log_core_events": bool(config.debug.log_core_events),
                "log_prompt_preview": bool(config.debug.log_prompt_preview),
                "log_client_events": bool(config.debug.log_client_events),
                "stderr_mirror_enabled": bool(config.debug.stderr_mirror_enabled),
                "preview_chars": int(config.debug.preview_chars),
            },
            "vad": {
                "threshold": float(config.vad.threshold),
                "silence_ms": int(config.vad.silence_ms),
                "min_speech_ms": int(config.vad.min_speech_ms),
                "max_ms": int(config.vad.max_ms),
                "pre_speech_ms": int(config.vad.pre_speech_ms),
            },
            "protocol": "neo-minicpm-live-v0",
        }

    async def _ingest_live_event(
        self,
        request: LiveEventRequest,
        local_event: dict[str, Any],
    ) -> None:
        """把 live 侧事件写入实时统一事件流，并按需写入 Neo 事件/消息历史。"""
        config = self._config()
        event_type = str(request.event_type or "").strip()
        role = str(request.role or "").strip().lower()
        text = str(request.text or "").strip()
        payload = dict(request.payload or {})

        unified_event = {
            "origin": "minicpm_live_bridge",
            "channel": "live",
            "source": "live",
            "event_type": event_type,
            "role": role,
            "text": text,
            "stream_id": config.session.stream_id,
            "session_id": request.session_id,
            "time": local_event.get("time", time.time()),
            "payload": payload,
        }
        await self.publish_unified_event(
            unified_event,
            max_backlog=int(config.unified_event_stream.max_backlog_events),
        )

        normalized_type = event_type.lower()
        if normalized_type.startswith("screen"):
            if config.unified_event_stream.record_screen_summary_to_life:
                summary = text or str(payload.get("summary") or payload.get("description") or "").strip()
                if summary:
                    await self._record_life_event_only(
                        content=summary,
                        content_type=normalized_type.replace(".", "_") or "live_screen",
                        sender="Live 屏幕",
                        metadata=payload,
                    )
            return

        if not text:
            return

        if role in {"user", "human"}:
            message = self._build_live_message(
                content=text,
                role="user",
                message_id=str(payload.get("message_id") or f"live_user_{uuid.uuid4().hex}"),
                event_type=event_type,
                metadata=payload,
            )
            if bool(config.session.dispatch_user_transcript_to_chatter):
                await self._publish_core_received_message(message)
            else:
                await self._record_message_without_chatter(message, direction="received")
            return

        if role in {"assistant", "model", "bot"}:
            message = self._build_live_message(
                content=text,
                role="assistant",
                message_id=str(payload.get("message_id") or f"live_assistant_{uuid.uuid4().hex}"),
                event_type=event_type,
                metadata=payload,
            )
            await self._record_message_without_chatter(message, direction="sent")

    async def _prepare_local_api_turn(self, request: LiveTurnRequest) -> dict[str, Any]:
        """构建 LLM 请求所需的全部信息；半双工和全双工共享此逻辑。"""
        from src.app.plugin_system.api.llm_api import create_llm_request, get_model_set_by_task
        from src.kernel.llm import Audio, Image, LLMPayload, ROLE, Text

        config = self._config()
        model_task_name = (config.session.model_task_name or "live").strip() or "live"
        model_set = get_model_set_by_task(model_task_name)
        llm_request = create_llm_request(model_set, request_name="minicpm_live_turn")

        user_text = request.text.strip()
        turn_event_type = "voice_input" if request.audio_data.strip() and not user_text else "text_input"
        display_user_text, turn_payload_patch = await self._resolve_live_turn_user_text(request)
        turn_payload = dict(request.payload or {})
        turn_payload.update(turn_payload_patch)

        t_prompt_start = time.perf_counter()
        prompt_bundle = await self._build_life_chatter_prompt_for_live_turn(
            request=request,
            display_user_text=display_user_text,
            payload=turn_payload,
        )
        self._log_live(f"Prompt built in {(time.perf_counter() - t_prompt_start):.3f}s")
        self._log_prompt_bundle_summary(
            prompt_bundle,
            session_id=request.session_id,
            user_text=display_user_text,
        )

        await self._ingest_current_turn_input(
            request=request,
            event_type=turn_event_type,
            text=display_user_text,
            payload=turn_payload,
        )

        system_prompt = str(prompt_bundle.get("system_prompt") or "").strip()
        if system_prompt:
            llm_request.add_payload(LLMPayload(ROLE.SYSTEM, Text(system_prompt)))
        llm_request.add_payload(LLMPayload(ROLE.SYSTEM, Text(_LIVE_BRIDGE_OUTPUT_CONTRACT)))

        recent_events = list(prompt_bundle.get("unified_events") or [])
        session_events = list(prompt_bundle.get("session_events") or [])
        content: list[Any] = []
        user_prompt = str(prompt_bundle.get("user_prompt") or "").strip()
        content.append(Text(user_prompt or display_user_text))

        dynamic_context = str(prompt_bundle.get("dynamic_context") or "").strip()
        if dynamic_context:
            content.append(
                Text(
                    "<transient_life_context>\n"
                    f"{dynamic_context}\n"
                    "</transient_life_context>"
                )
            )

        if session_events:
            content.append(
                Text(
                    "当前 live session 最近事件（本通道短期上下文）：\n"
                    + json.dumps(session_events, ensure_ascii=False)
                )
            )

        if recent_events:
            content.append(
                Text(
                    "实时统一事件流补充（QQ、live 和其他通道会混在这里，按 sequence 递增；life_chatter 主上下文不足时参考）：\n"
                    + json.dumps(recent_events, ensure_ascii=False)
                )
            )

        if request.screen_image.strip():
            content.append(Text("当前电脑屏幕截图："))
            content.append(Image(request.screen_image.strip()))

        if request.audio_data.strip():
            content.append(Text("用户语音片段："))
            content.append(
                Audio(
                    request.audio_data.strip(),
                    mime_type=(
                        request.audio_mime_type.strip()
                        or config.capture.audio_mime_type
                        or "audio/webm"
                    ),
                )
            )

        llm_request.add_payload(LLMPayload(ROLE.USER, content))

        return {
            "llm_request": llm_request,
            "model_task_name": model_task_name,
            "display_user_text": display_user_text,
            "turn_payload": turn_payload,
        }

    async def _finalize_local_api_turn(
        self,
        *,
        request: LiveTurnRequest,
        prep: dict[str, Any],
        text: str,
    ) -> None:
        """LLM 回复完成后写入 ingest_live_event；半双工和全双工共用。"""
        turn_payload = prep.get("turn_payload") or {}
        model_task_name = prep.get("model_task_name") or "live"
        await self._ingest_live_event(
            LiveEventRequest(
                session_id=request.session_id,
                event_type="local_api.reply",
                role="assistant",
                text=text,
                payload={
                    "model_task_name": model_task_name,
                    "mode": "local_api_turn",
                    "has_screen_image": bool(request.screen_image.strip()),
                    "has_audio_data": bool(request.audio_data.strip()),
                    "voice_transcript": turn_payload.get("voice_transcript", ""),
                    "voice_transcript_available": bool(turn_payload.get("voice_transcript_available")),
                    "voice_transcript_source": turn_payload.get("voice_transcript_source", ""),
                    "request_payload": self._truncate_payload(request.payload),
                },
            ),
            {
                "id": f"evt_{uuid.uuid4().hex}",
                "session_id": request.session_id,
                "event_type": "local_api.reply",
                "role": "assistant",
                "text": text,
                "payload": {},
                "time": time.time(),
            },
        )

    async def _run_local_api_turn(self, request: LiveTurnRequest) -> str:
        """使用 config/model.toml 的 live 任务跑一轮半双工多模态 API。"""
        t0 = time.perf_counter()
        prep = await self._prepare_local_api_turn(request)
        llm_request = prep["llm_request"]

        t_llm_start = time.perf_counter()
        response = await llm_request.send(stream=False)
        text = (await response).strip()
        self._log_live(f"LLM request done in {(time.perf_counter() - t_llm_start):.3f}s")
        if not text:
            text = "我这边没有拿到有效回复。"

        await self._finalize_local_api_turn(request=request, prep=prep, text=text)
        return text

    async def _stream_local_api_turn(self, request: LiveTurnRequest) -> AsyncIterator[bytes]:
        """全双工 SSE 流：流式 LLM → 按句 TTS → 顺序推送 audio chunk。"""
        config = self._config()
        tts_style = (config.session.tts_style or "").strip()
        min_chars = max(1, int(getattr(config.session, "full_duplex_sentence_min_chars", 8)))
        sentence_punct = set("。！？.!?\n；;")

        def sse(event: str, data: dict[str, Any]) -> bytes:
            return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode("utf-8")

        try:
            prep = await self._prepare_local_api_turn(request)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"MiniCPM Live 全双工 prep 失败: {exc}", exc_info=True)
            yield sse("error", {"error": str(exc)})
            return

        llm_request = prep["llm_request"]
        tts_service = self._get_tts_service() if tts_style else None
        tts_mime_type = "audio/wav"
        if tts_service is not None:
            try:
                media_type = str(
                    getattr(getattr(tts_service, "_config", None), "tts_advanced", None) and
                    getattr(tts_service._config.tts_advanced, "media_type", None) or "wav"
                ).strip().lstrip(".") or "wav"
                tts_mime_type = f"audio/{media_type}"
            except Exception:
                pass

        sentence_queue: asyncio.Queue[tuple[int, str] | None] = asyncio.Queue()
        audio_queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        full_text_parts: list[str] = []

        async def tts_worker() -> None:
            """从 sentence_queue 取句子，按 seq 顺序串行 TTS，结果丢进 audio_queue。"""
            while True:
                item = await sentence_queue.get()
                if item is None:
                    await audio_queue.put(None)
                    return
                seq, sentence = item
                if tts_service is None or not sentence.strip():
                    await audio_queue.put({
                        "seq": seq, "text": sentence, "audio": None, "mime": tts_mime_type,
                    })
                    continue
                try:
                    audio_b64 = await tts_service.generate_voice(sentence, tts_style)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"MiniCPM Live 全双工 TTS 失败 seq={seq}: {exc}")
                    audio_b64 = None
                await audio_queue.put({
                    "seq": seq, "text": sentence, "audio": audio_b64, "mime": tts_mime_type,
                })

        tts_task = asyncio.create_task(tts_worker())

        async def llm_worker() -> None:
            """流式跑 LLM；按句切并入 sentence_queue。"""
            buffer: list[str] = []
            seq = [0]

            async def flush_if_ready(force: bool) -> None:
                joined = "".join(buffer)
                if not joined.strip():
                    if force:
                        buffer.clear()
                    return
                if not force:
                    last = joined[-1]
                    if last not in sentence_punct and len(joined.strip()) < min_chars:
                        return
                    if last not in sentence_punct and len(joined.strip()) < min_chars * 2:
                        return
                seq[0] += 1
                await sentence_queue.put((seq[0], joined.strip()))
                buffer.clear()

            try:
                response = await llm_request.send(stream=True)

                async def on_chunk(text_delta: str) -> None:
                    if not text_delta:
                        return
                    full_text_parts.append(text_delta)
                    for ch in text_delta:
                        buffer.append(ch)
                        if ch in sentence_punct:
                            await flush_if_ready(force=True)
                    if buffer and "".join(buffer).strip().__len__() >= min_chars * 3:
                        await flush_if_ready(force=False)

                await response.stream_with_callback(on_chunk)
                await flush_if_ready(force=True)
            finally:
                await sentence_queue.put(None)

        llm_task = asyncio.create_task(llm_worker())

        text_seq = 0
        try:
            yield sse("start", {"session_id": request.session_id})

            async def text_pump() -> AsyncIterator[bytes]:
                nonlocal text_seq
                # 我们靠 audio_queue 的 text 字段把文字也带回去；但希望 token 级延迟低，
                # 因此每收到一个 audio 项就一起发 text_delta + audio。
                while True:
                    item = await audio_queue.get()
                    if item is None:
                        return
                    text_seq += 1
                    yield sse("text_delta", {"seq": text_seq, "text": item["text"]})
                    if item.get("audio"):
                        yield sse("audio", {
                            "seq": text_seq,
                            "audio_base64": item["audio"],
                            "mime_type": item["mime"],
                            "text": item["text"],
                        })

            async for chunk in text_pump():
                yield chunk

            await llm_task
            await tts_task

            final_text = "".join(full_text_parts).strip() or "我这边没有拿到有效回复。"
            await self._finalize_local_api_turn(request=request, prep=prep, text=final_text)
            yield sse("done", {"text": final_text})
        except asyncio.CancelledError:
            llm_task.cancel()
            tts_task.cancel()
            self._log_live("MiniCPM Live 全双工流被取消（客户端断开）")
            raise
        except Exception as exc:  # noqa: BLE001
            logger.error(f"MiniCPM Live 全双工流失败: {exc}", exc_info=True)
            llm_task.cancel()
            tts_task.cancel()
            yield sse("error", {"error": str(exc)})


    async def _resolve_live_turn_user_text(
        self,
        request: LiveTurnRequest,
    ) -> tuple[str, dict[str, Any]]:
        user_text = request.text.strip()
        if user_text:
            return user_text, {}

        audio_data = request.audio_data.strip()
        if not audio_data:
            return "", {}

        transcript = await self._transcribe_live_audio(
            audio_data=audio_data,
            audio_mime_type=request.audio_mime_type.strip(),
        )
        if transcript:
            return transcript, {
                "voice_transcript": transcript,
                "voice_transcript_available": True,
                "voice_transcript_source": "neo_media_manager_asr",
            }

        return "[语音输入]", {
            "voice_transcript": "",
            "voice_transcript_available": False,
            "voice_transcript_source": "neo_media_manager_asr",
        }

    async def _transcribe_live_audio(
        self,
        *,
        audio_data: str,
        audio_mime_type: str,
    ) -> str:
        try:
            from src.core.managers.media_manager import get_media_manager

            payload: dict[str, Any] = {"base64": audio_data}
            if audio_mime_type:
                payload["mime_type"] = audio_mime_type

            transcript = await get_media_manager().recognize_voice(payload, use_cache=True)
            text = str(transcript or "").strip()
            if text:
                self._log_live(
                    "MiniCPM Live ASR transcript: "
                    f"chars={len(text)} text={self._preview(text)}"
                )
            else:
                self._log_live(
                    "MiniCPM Live ASR returned empty transcript",
                    level="warning",
                )
            return text
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"MiniCPM Live 语音转写失败: {exc}", exc_info=True)
            self._log_live(
                f"MiniCPM Live ASR failed: {exc}",
                level="warning",
            )
            return ""

    def _log_prompt_bundle_summary(
        self,
        prompt_bundle: dict[str, Any],
        *,
        session_id: str,
        user_text: str,
    ) -> None:
        config = self._config()
        if not self._terminal_log_enabled() or not bool(config.debug.log_prompt_preview):
            return

        system_prompt = str(prompt_bundle.get("system_prompt") or "")
        user_prompt = str(prompt_bundle.get("user_prompt") or "")
        dynamic_context = str(prompt_bundle.get("dynamic_context") or "")
        self._log_live(
            "MiniCPM Live prompt bundle: "
            f"session={self._short_id(session_id)} "
            f"source={prompt_bundle.get('prompt_source') or '-'} "
            f"system_chars={len(system_prompt)} "
            f"user_chars={len(user_prompt)} "
            f"dynamic_chars={len(dynamic_context)} "
            f"high_water={prompt_bundle.get('life_context_high_water') or 0} "
            f"unified={len(prompt_bundle.get('unified_events') or [])} "
            f"session_events={len(prompt_bundle.get('session_events') or [])} "
            f"input={self._preview(user_text)}"
        )
        self._log_live(
            "MiniCPM Live user_prompt preview: "
            f"{self._preview(user_prompt)}"
        )
        if dynamic_context:
            self._log_live(
                "MiniCPM Live dynamic_context preview: "
                f"{self._preview(dynamic_context)}"
            )

    async def _build_life_chatter_prompt_for_live_turn(
        self,
        *,
        request: LiveTurnRequest,
        display_user_text: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        config = self._config()
        context_config = config.context
        prompt_bundle: dict[str, Any] = {}
        message_payload = dict(payload or request.payload or {})

        chat_stream = await self._get_live_chat_stream()
        current_message = self._build_live_message(
            content=display_user_text,
            role="user",
            message_id=str(
                request.payload.get("message_id")
                or f"live_current_{uuid.uuid4().hex}"
            ),
            event_type="voice_input" if request.audio_data.strip() and not request.text.strip() else "text_input",
            metadata=message_payload,
        )

        try:
            from plugins.life_engine.core.chatter import LifeChatter

            life_plugin, service = self._get_life_plugin_and_service()
            if life_plugin is not None:
                chatter = LifeChatter(
                    stream_id=config.session.stream_id,
                    plugin=life_plugin,
                )
                unread_lines = LifeChatter.format_message_line(current_message)
                build_prompt = getattr(chatter, "build_live_bridge_prompt", None)
                if callable(build_prompt):
                    prompt_bundle = await build_prompt(
                        chat_stream,
                        service,
                        unread_lines=unread_lines,
                        include_history_in_prompt=True,
                        include_recent_chat_history=True,
                        commit_cursors=bool(context_config.mark_life_context_seen),
                        event_cursor_override=None,
                    )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"构建 life_chatter 同源 live prompt 失败，降级为最小 prompt: {exc}", exc_info=True)

        if not prompt_bundle:
            prompt_bundle = {
                "system_prompt": "",
                "user_prompt": display_user_text,
                "dynamic_context": "",
                "life_context_high_water": 0,
                "prompt_source": "fallback",
            }

        session_events: list[dict[str, Any]] = []
        if bool(context_config.include_live_session_events):
            events = list(self._events.get(request.session_id, []) or [])
            limit = max(1, int(context_config.live_session_event_limit or 24))
            session_events = [self._compact_session_event(event) for event in events[-limit:]]

        unified_events: list[dict[str, Any]] = []
        if bool(context_config.include_unified_events):
            unified_events = [
                self._compact_unified_event(event)
                for event in await self.tail_unified_events(
                    limit=max(1, int(context_config.unified_event_limit or 80))
                )
            ]

        prompt_bundle["session_events"] = session_events
        prompt_bundle["unified_events"] = unified_events
        return prompt_bundle

    async def _ingest_current_turn_input(
        self,
        *,
        request: LiveTurnRequest,
        event_type: str,
        text: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        payload_data = dict(payload or request.payload or {})
        payload_data.setdefault("mode", "local_api_turn")
        payload_data.setdefault("has_audio_data", bool(request.audio_data.strip()))
        payload_data.setdefault("has_screen_image", bool(request.screen_image.strip()))
        if request.audio_mime_type.strip():
            payload_data.setdefault("audio_mime_type", request.audio_mime_type.strip())
        await self._ingest_live_event(
            LiveEventRequest(
                session_id=request.session_id,
                event_type=event_type,
                role="user",
                text=text,
                payload=payload_data,
            ),
            {
                "id": f"evt_{uuid.uuid4().hex}",
                "session_id": request.session_id,
                "event_type": event_type,
                "role": "user",
                "text": text,
                "payload": payload_data,
                "time": time.time(),
            },
        )

    async def _build_live_context_snapshot(self, *, session_id: str = "") -> dict[str, Any]:
        """构建 live 可读的统一上下文快照。

        这个快照是只读上下文：默认不推进 life_chatter 的全局 cursor，避免 live
        消耗 QQ 主链路尚未读取的运行态事件。
        """
        config = self._config()
        context_config = config.context

        life_runtime_context = ""
        life_runtime_high_water = 0
        life_context_available = False
        life_context_error = ""
        life_chatter_prompt: dict[str, Any] = {}
        if bool(context_config.include_life_runtime_context):
            try:
                life_runtime_context, life_runtime_high_water, life_chatter_prompt = (
                    await self._build_life_runtime_context_for_live()
                )
                life_context_available = bool(life_runtime_context)
            except Exception as exc:  # noqa: BLE001
                life_context_error = str(exc)
                logger.debug(f"构建 live 统一意识上下文失败: {exc}", exc_info=True)

        unified_events: list[dict[str, Any]] = []
        if bool(context_config.include_unified_events):
            unified_events = [
                self._compact_unified_event(event)
                for event in await self.tail_unified_events(
                    limit=max(1, int(context_config.unified_event_limit or 80))
                )
            ]

        session_events: list[dict[str, Any]] = []
        if bool(context_config.include_live_session_events) and session_id:
            events = list(self._events.get(session_id, []) or [])
            limit = max(1, int(context_config.live_session_event_limit or 24))
            session_events = [self._compact_session_event(event) for event in events[-limit:]]

        return {
            "session_id": session_id,
            "stream_id": config.session.stream_id,
            "life_runtime_available": life_context_available,
            "life_runtime_high_water": life_runtime_high_water,
            "life_runtime_error": life_context_error,
            "life_runtime_context": life_runtime_context,
            "life_chatter_prompt": life_chatter_prompt,
            "unified_events": unified_events,
            "session_events": session_events,
            "limits": {
                "life_event_limit": int(context_config.life_event_limit),
                "unified_event_limit": int(context_config.unified_event_limit),
                "live_session_event_limit": int(context_config.live_session_event_limit),
            },
        }

    async def _build_life_runtime_context_for_live(self) -> tuple[str, int, dict[str, Any]]:
        config = self._config()
        context_config = config.context
        life_plugin, service = self._get_life_plugin_and_service()
        if service is None:
            return "", 0, {}

        chat_stream = await self._get_live_chat_stream()
        try:
            from plugins.life_engine.core.chatter import LifeChatter

            if life_plugin is not None:
                chatter = LifeChatter(
                    stream_id=config.session.stream_id,
                    plugin=life_plugin,
                )
                build_prompt = getattr(chatter, "build_live_bridge_prompt", None)
                if callable(build_prompt):
                    prompt_bundle = await build_prompt(
                        chat_stream,
                        service,
                        unread_lines="",
                        include_history_in_prompt=True,
                        include_recent_chat_history=True,
                        commit_cursors=bool(context_config.mark_life_context_seen),
                        event_cursor_override=None,
                    )
                    dynamic_context = str(prompt_bundle.get("dynamic_context") or "").strip()
                    high_water = int(prompt_bundle.get("life_context_high_water") or 0)
                    prompt_view = {
                        "prompt_source": prompt_bundle.get("prompt_source") or "life_chatter",
                        "system_prompt": prompt_bundle.get("system_prompt") or "",
                        "user_prompt": prompt_bundle.get("user_prompt") or "",
                        "dynamic_context": dynamic_context,
                        "bridge_output_contract": _LIVE_BRIDGE_OUTPUT_CONTRACT,
                    }
                    if bool(context_config.mark_life_context_seen) and high_water > 0:
                        mark_seen = getattr(service, "mark_chatter_runtime_context_seen", None)
                        if callable(mark_seen):
                            await mark_seen(
                                config.session.stream_id,
                                high_water,
                                unified_chatter_context=True,
                            )
                    return dynamic_context, high_water, prompt_view
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"构建 life_chatter prompt 快照失败: {exc}", exc_info=True)

        build_context = getattr(service, "build_chatter_runtime_context", None)
        if not callable(build_context):
            return "", 0, {}
        context_text, high_water = await build_context(
            chat_stream,
            event_limit=max(1, int(context_config.life_event_limit or 80)),
            unified_chatter_context=True,
            include_recent_chat_history=True,
            commit_cursors=bool(context_config.mark_life_context_seen),
            event_cursor_override=None,
        )

        if bool(context_config.mark_life_context_seen) and int(high_water or 0) > 0:
            mark_seen = getattr(service, "mark_chatter_runtime_context_seen", None)
            if callable(mark_seen):
                await mark_seen(
                    config.session.stream_id,
                    int(high_water),
                    unified_chatter_context=True,
                )

        return str(context_text or "").strip(), int(high_water or 0), {}

    @staticmethod
    def _get_life_plugin_and_service() -> tuple[Any | None, Any | None]:
        try:
            from src.core.managers.plugin_manager import get_plugin_manager

            life_plugin = get_plugin_manager().get_plugin("life_engine")
            service = getattr(life_plugin, "service", None) if life_plugin is not None else None
            return life_plugin, service
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"读取 life_engine 插件失败: {exc}")
            return None, None

    async def _get_live_chat_stream(self) -> Any:
        from src.core.managers.stream_manager import get_stream_manager

        config = self._config()
        return await get_stream_manager().get_or_create_stream(
            stream_id=config.session.stream_id,
            platform="live",
            user_id=config.session.user_id,
            group_name=config.session.stream_name,
            chat_type="private",
        )

    @staticmethod
    def _compact_unified_event(event: dict[str, Any]) -> dict[str, Any]:
        compact: dict[str, Any] = {}
        for key in (
            "sequence",
            "origin",
            "channel",
            "source",
            "event_type",
            "direction",
            "stream_id",
            "session_id",
            "sender_name",
            "sender_role",
            "role",
            "text",
            "time",
        ):
            value = event.get(key)
            if value is None or value == "":
                continue
            if key == "text":
                value = str(value)
                if len(value) > 1200:
                    value = value[:1200] + "...[truncated]"
            compact[key] = value
        return compact

    @staticmethod
    def _compact_session_event(event: dict[str, Any]) -> dict[str, Any]:
        compact: dict[str, Any] = {}
        for key in ("event_type", "role", "text", "time", "payload"):
            value = event.get(key)
            if value is None or value == "":
                continue
            if key == "text":
                value = str(value)
                if len(value) > 800:
                    value = value[:800] + "...[truncated]"
            elif key == "payload" and isinstance(value, dict):
                payload: dict[str, Any] = {}
                for payload_key, payload_value in value.items():
                    if isinstance(payload_value, str) and len(payload_value) > 240:
                        payload[payload_key] = payload_value[:240] + "...[truncated]"
                    else:
                        payload[payload_key] = payload_value
                value = payload
            compact[key] = value
        return compact

    @staticmethod
    def _truncate_payload(payload: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in dict(payload or {}).items():
            if isinstance(value, str) and len(value) > 500:
                result[key] = value[:500] + "...[truncated]"
            else:
                result[key] = value
        return result

    def _build_live_message(
        self,
        *,
        content: str,
        role: str,
        message_id: str,
        event_type: str,
        metadata: dict[str, Any],
    ) -> Message:
        config = self._config()
        is_assistant = role == "assistant"
        return Message(
            message_id=message_id,
            time=time.time(),
            content=content,
            processed_plain_text=content,
            message_type=MessageType.TEXT,
            sender_id=config.session.assistant_id if is_assistant else config.session.user_id,
            sender_name=config.session.assistant_name if is_assistant else config.session.user_name,
            sender_role="bot" if is_assistant else "user",
            platform="live",
            chat_type="private",
            stream_id=config.session.stream_id,
            event_type=event_type,
            minicpm_live_ingested=True,
            live_metadata=metadata,
        )

    async def _publish_core_received_message(self, message: Message) -> None:
        from src.core.managers.event_manager import get_event_manager

        await get_event_manager().publish_event(
            EventType.ON_MESSAGE_RECEIVED,
            {
                "message": message,
                "adapter_signature": "minicpm_live_bridge:router:minicpm_live",
            },
        )

    async def _record_message_without_chatter(self, message: Message, *, direction: str) -> None:
        """写入 stream 历史和 life_engine 事件流，但不唤醒 Chatter。"""
        from src.core.managers.stream_manager import get_stream_manager

        config = self._config()
        stream_manager = get_stream_manager()
        chat_stream = await stream_manager.get_or_create_stream(
            stream_id=config.session.stream_id,
            platform="live",
            user_id=config.session.user_id,
            group_name=config.session.stream_name,
            chat_type="private",
        )

        if direction == "sent":
            await stream_manager.add_sent_message_to_history(message)
        else:
            await stream_manager.add_message(message)
            chat_stream.context.flush_unreads_to_history()
        self._log_live(
            "MiniCPM Live message stored: "
            f"direction={direction} stream={config.session.stream_id} "
            f"message_id={self._short_id(getattr(message, 'message_id', '') or '')} "
            f"text={self._preview(getattr(message, 'processed_plain_text', '') or getattr(message, 'content', ''))}"
        )

        try:
            from src.core.managers.plugin_manager import get_plugin_manager

            life_plugin = get_plugin_manager().get_plugin("life_engine")
            service = getattr(life_plugin, "service", None) if life_plugin is not None else None
            if service is not None and hasattr(service, "record_message"):
                await service.record_message(message, direction=direction)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"live 消息写入 life_engine 事件流失败: {exc}")

    async def _record_life_event_only(
        self,
        *,
        content: str,
        content_type: str,
        sender: str,
        metadata: dict[str, Any],
    ) -> None:
        """写入 life_engine 统一事件流，不创建普通聊天未读消息。"""
        try:
            from src.core.managers.plugin_manager import get_plugin_manager
            from plugins.life_engine.service.event_builder import EventType as LifeEventType
            from plugins.life_engine.service.event_builder import LifeEngineEvent, _now_iso

            life_plugin = get_plugin_manager().get_plugin("life_engine")
            service = getattr(life_plugin, "service", None) if life_plugin is not None else None
            next_sequence = getattr(service, "_next_sequence", None)
            queue_event = getattr(service, "_queue_pending_event", None)
            if service is None or not callable(next_sequence) or not callable(queue_event):
                return

            sequence = int(next_sequence())
            event = LifeEngineEvent(
                event_id=f"minicpm_live_{content_type}_{sequence}",
                event_type=LifeEventType.MESSAGE,
                timestamp=_now_iso(),
                sequence=sequence,
                source="live",
                source_detail=(
                    "live | 入站 | 实时状态 | "
                    f"{self._config().session.stream_name} | metadata={bool(metadata)}"
                ),
                content=content,
                content_type=content_type,
                sender=sender,
                chat_type="private",
                stream_id=self._config().session.stream_id,
            )
            await queue_event(event)
            self._log_live(
                "MiniCPM Live life event stored: "
                f"type={content_type} sender={sender} text={self._preview(content)}"
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"live 状态事件写入 life_engine 失败: {exc}")

    def _ensure_live_session(self, session_id: str) -> None:
        now = time.time()
        session = self._sessions.setdefault(
            session_id,
            {
                "session_id": session_id,
                "client_name": "neo-web",
                "mode": "screen_voice",
                "metadata": {},
                "created_at": now,
            },
        )
        session["updated_at"] = now
        self._events.setdefault(session_id, [])

    @staticmethod
    def _parse_realtime_message(raw: str | bytes) -> Any:
        if isinstance(raw, (bytes, bytearray)):
            return bytes(raw)
        text = str(raw or "")
        try:
            return json.loads(text)
        except Exception:
            return text

    async def _send_client_ws_payload(
        self,
        websocket: WebSocket,
        payload: str | bytes | dict[str, Any],
    ) -> None:
        if isinstance(payload, (bytes, bytearray)):
            await websocket.send_bytes(bytes(payload))
            return
        if isinstance(payload, dict):
            await websocket.send_json(payload)
            return
        await websocket.send_text(str(payload))

    @staticmethod
    async def _send_upstream_ws_payload(
        websocket: Any,
        payload: str | bytes | dict[str, Any],
    ) -> None:
        if isinstance(payload, (bytes, bytearray)):
            await websocket.send(bytes(payload))
            return
        if isinstance(payload, dict):
            await websocket.send(json.dumps(payload, ensure_ascii=False))
            return
        await websocket.send(str(payload))

    async def _flush_realtime_adapter_result(
        self,
        *,
        client_websocket: WebSocket,
        upstream_websocket: Any,
        result: Any,
    ) -> None:
        for payload in list(getattr(result, "upstream_messages", []) or []):
            await self._send_upstream_ws_payload(upstream_websocket, payload)
        for payload in list(getattr(result, "client_messages", []) or []):
            await self._send_client_ws_payload(client_websocket, payload)

    async def _proxy_client_messages(
        self,
        *,
        client_websocket: WebSocket,
        upstream_websocket: Any,
        adapter: Any,
        session_id: str,
    ) -> None:
        try:
            while True:
                message = await client_websocket.receive()
                if message.get("type") == "websocket.disconnect":
                    raise WebSocketDisconnect(code=int(message.get("code") or 1000))
                raw = message.get("bytes")
                if raw is None:
                    raw = message.get("text", "")
                payload = self._parse_realtime_message(raw)
                result = await adapter.on_client_message(payload)
                await self._flush_realtime_adapter_result(
                    client_websocket=client_websocket,
                    upstream_websocket=upstream_websocket,
                    result=result,
                )
                self._ensure_live_session(session_id)
        except Exception as exc:
            if self._is_benign_realtime_close(exc):
                return
            raise

    @staticmethod
    def _connect_upstream_websocket(
        websockets_module: Any,
        *,
        url: str,
        headers: dict[str, str],
        open_timeout: float,
    ) -> Any:
        connect = websockets_module.connect
        kwargs: dict[str, Any] = {
            "open_timeout": open_timeout,
            "close_timeout": 3,
            "max_size": 16 * 1024 * 1024,
        }
        try:
            parameters = inspect.signature(connect).parameters
        except (TypeError, ValueError):
            parameters = {}

        if "additional_headers" in parameters:
            kwargs["additional_headers"] = headers
        elif "extra_headers" in parameters:
            kwargs["extra_headers"] = headers
        elif headers:
            kwargs["additional_headers"] = headers

        if "proxy" in parameters:
            kwargs["proxy"] = None

        return connect(url, **kwargs)

    async def _proxy_upstream_messages(
        self,
        *,
        client_websocket: WebSocket,
        upstream_websocket: Any,
        adapter: Any,
        session_id: str,
    ) -> None:
        try:
            async for raw in upstream_websocket:
                payload = self._parse_realtime_message(raw)
                result = await adapter.on_upstream_message(payload)
                await self._flush_realtime_adapter_result(
                    client_websocket=client_websocket,
                    upstream_websocket=upstream_websocket,
                    result=result,
                )
                self._ensure_live_session(session_id)
        except Exception as exc:
            if self._is_benign_realtime_close(exc):
                return
            raise

    @staticmethod
    def _is_benign_realtime_close(exc: Exception) -> bool:
        text = str(exc).lower()
        name = exc.__class__.__name__
        if name in {"ConnectionClosed", "ConnectionClosedOK", "ConnectionClosedError"}:
            return True
        return "no close frame received or sent" in text

    async def _safe_send_proxy_error(self, websocket: WebSocket, message: str) -> None:
        try:
            await websocket.send_json({"type": "error", "message": message})
        except Exception:
            return

    async def _proxy_upstream_sse_loop(
        self,
        *,
        client_websocket: WebSocket,
        adapter: Any,
        session_id: str,
    ) -> None:
        """Poll the upstream SSE endpoint and forward events to the client WebSocket.

        Re-subscribes after each response stream ends so that continuous
        conversation works (the model server's SSE stream ends after each turn).
        """
        import aiohttp  # lightweight HTTP client already available in the venv

        sse_url = adapter.upstream_sse_url()
        if not sse_url:
            return
        method = str(getattr(adapter, "upstream_sse_method", lambda: "GET")() or "GET").upper()
        payload = getattr(adapter, "upstream_sse_payload", lambda: None)()
        headers = adapter.upstream_sse_headers()

        while True:
            try:
                timeout = aiohttp.ClientTimeout(total=None, connect=10, sock_read=120)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.request(
                        method,
                        sse_url,
                        headers=headers,
                        json=payload,
                    ) as resp:
                        if resp.status != 200:
                            body_preview = (await resp.text())[:200]
                            self._log_live(
                                f"SSE upstream returned {resp.status} via {method}: {body_preview}",
                                level="warning",
                            )
                            await asyncio.sleep(2)
                            continue
                        async for raw_line in resp.content:
                            line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
                            if not line.startswith("data:"):
                                continue
                            data_str = line[5:].strip()
                            if not data_str or data_str == "[DONE]":
                                continue
                            try:
                                data = json.loads(data_str)
                            except Exception:
                                continue
                            result = await adapter.on_sse_event(data)
                            for msg in list(getattr(result, "client_messages", []) or []):
                                try:
                                    if isinstance(msg, (str, bytes)):
                                        await client_websocket.send_text(
                                            msg if isinstance(msg, str) else msg.decode()
                                        )
                                    else:
                                        await client_websocket.send_json(msg)
                                except Exception:
                                    return
                # Brief pause before re-connecting for next response turn
                await asyncio.sleep(0.3)
            except asyncio.CancelledError:
                return
            except Exception as exc:
                self._log_live(
                    f"SSE upstream error for session={self._short_id(session_id)}: {exc}",
                    level="warning",
                )
                await asyncio.sleep(2)

    async def _serve_realtime_proxy(
        self,
        *,
        websocket: WebSocket,
        session_id: str,
    ) -> None:
        config = self._config()
        if not config.plugin.enabled:
            await websocket.close(code=4403, reason="MiniCPM Live Bridge disabled")
            return
        if self._transport_mode(config) != "neo_proxy":
            await websocket.close(code=4404, reason="realtime proxy disabled")
            return

        upstream_url = self._upstream_websocket_url(config, session_id=session_id)
        if not upstream_url:
            await websocket.close(code=4404, reason="upstream websocket_url is empty")
            return

        self._ensure_live_session(session_id)
        await websocket.accept()

        adapter = build_realtime_adapter(
            adapter_name=self._protocol_adapter_name(config),
            session_id=session_id,
            upstream_url=upstream_url,
            upstream_headers=self._server_headers(config),
        )

        self._log_live(
            "MiniCPM Live realtime proxy opening: "
            f"session={self._short_id(session_id)} "
            f"adapter={adapter.adapter_name} "
            f"upstream={self._preview(upstream_url)}"
        )

        try:
            import websockets

            # Reset the upstream server's uid state before opening the WebSocket.
            # For MiniCPM-o the server only resets stream_manager.uid on POST
            # /api/v1/completions, so without this every first WS message gets
            # "UID changed in stream".
            await adapter.upstream_pre_connect()

            async with self._connect_upstream_websocket(
                websockets,
                url=adapter.upstream_connect_url(),
                headers=adapter.upstream_connect_headers(),
                open_timeout=float(config.server.request_timeout_seconds),
            ) as upstream_websocket:
                self._log_live(
                    "MiniCPM Live realtime proxy connected: "
                    f"session={self._short_id(session_id)} "
                    f"state={self._preview(adapter.describe_state())}"
                )
                client_task = asyncio.create_task(
                    self._proxy_client_messages(
                        client_websocket=websocket,
                        upstream_websocket=upstream_websocket,
                        adapter=adapter,
                        session_id=session_id,
                    )
                )
                upstream_task = asyncio.create_task(
                    self._proxy_upstream_messages(
                        client_websocket=websocket,
                        upstream_websocket=upstream_websocket,
                        adapter=adapter,
                        session_id=session_id,
                    )
                )

                all_tasks: set[asyncio.Task] = {client_task, upstream_task}

                # Optional: SSE side-channel for adapters that respond via HTTP SSE
                sse_task: asyncio.Task | None = None
                if adapter.upstream_sse_url():
                    sse_task = asyncio.create_task(
                        self._proxy_upstream_sse_loop(
                            client_websocket=websocket,
                            adapter=adapter,
                            session_id=session_id,
                        )
                    )
                    all_tasks.add(sse_task)

                done, pending = await asyncio.wait(
                    all_tasks,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in pending:
                    task.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
                for task in done:
                    if task.cancelled():
                        continue
                    exc = task.exception()
                    # SSE task ending on its own is not an error
                    if exc is not None and task is not sse_task:
                        raise exc
        except WebSocketDisconnect:
            pass
        except Exception as exc:  # noqa: BLE001
            if self._is_benign_realtime_close(exc):
                return
            logger.warning(f"MiniCPM Live realtime proxy 失败: {exc}", exc_info=True)
            self._log_live(
                "MiniCPM Live realtime proxy failed: "
                f"session={self._short_id(session_id)} "
                f"adapter={adapter.adapter_name} error={exc}",
                level="error",
            )
            await self._safe_send_proxy_error(websocket, f"realtime proxy error: {exc}")
        finally:
            self._log_live(
                "MiniCPM Live realtime proxy closed: "
                f"session={self._short_id(session_id)} "
                f"adapter={adapter.adapter_name} "
                f"state={self._preview(adapter.describe_state())}"
            )
            try:
                await websocket.close()
            except Exception:
                return

    def _health_url(self, config: MiniCPMLiveBridgeConfig) -> str:
        if config.server.health_url.strip():
            return self._resolve_url(config, config.server.health_url)
        return config.server.base_url.strip()

    def _resolve_url(self, config: MiniCPMLiveBridgeConfig, value: str) -> str:
        raw = (value or "").strip()
        if not raw:
            return ""
        parsed = urllib.parse.urlparse(raw)
        if parsed.scheme in {"http", "https"}:
            return raw

        base = config.server.base_url.strip()
        if not base:
            return ""
        return urllib.parse.urljoin(base.rstrip("/") + "/", raw.lstrip("/"))

    def _resolve_ws_url(self, config: MiniCPMLiveBridgeConfig, value: str) -> str:
        raw = (value or "").strip()
        if not raw:
            return ""
        parsed = urllib.parse.urlparse(raw)
        if parsed.scheme in {"ws", "wss"}:
            return raw

        resolved = self._resolve_url(config, raw)
        parsed_resolved = urllib.parse.urlparse(resolved)
        if parsed_resolved.scheme == "http":
            return urllib.parse.urlunparse(parsed_resolved._replace(scheme="ws"))
        if parsed_resolved.scheme == "https":
            return urllib.parse.urlunparse(parsed_resolved._replace(scheme="wss"))
        return resolved

    def _with_session_placeholder(self, url: str, session_id: str) -> str:
        if not url or not session_id:
            return url
        return url.replace("{session_id}", urllib.parse.quote(session_id, safe=""))

    def _server_headers(self, config: MiniCPMLiveBridgeConfig) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        token = config.server.auth_token.strip()
        if not token:
            return headers

        header_name = config.server.auth_header.strip() or "Authorization"
        if header_name.lower() == "authorization" and not token.lower().startswith(("bearer ", "basic ")):
            token = f"Bearer {token}"
        headers[header_name] = token
        return headers

    @staticmethod
    def _request_json_sync(
        method: str,
        url: str,
        payload: dict[str, Any] | None,
        headers: dict[str, str],
        timeout: float,
    ) -> dict[str, Any]:
        body: bytes | None = None
        request_headers = dict(headers)
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            request_headers["Content-Type"] = "application/json; charset=utf-8"

        request = urllib.request.Request(
            url,
            data=body,
            headers=request_headers,
            method=method.upper(),
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - user configured endpoint
                status_code = int(response.getcode() or 0)
                raw = response.read(65536).decode("utf-8", errors="replace")
                return {
                    "ok": 200 <= status_code < 400,
                    "status_code": status_code,
                    "body": MiniCPMLiveRouter._parse_body(raw),
                    "detail": "ok",
                }
        except urllib.error.HTTPError as exc:
            raw = exc.read(65536).decode("utf-8", errors="replace")
            return {
                "ok": False,
                "status_code": int(exc.code or 0),
                "body": MiniCPMLiveRouter._parse_body(raw),
                "detail": str(exc),
            }
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            return {
                "ok": False,
                "status_code": None,
                "body": None,
                "detail": str(exc),
            }

    @staticmethod
    def _parse_body(raw: str) -> Any:
        text = (raw or "").strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text[:4096]

    async def startup(self) -> None:
        self._log_live(f"MiniCPM Live Bridge 路由已启动，路径: {self.custom_route_path}")

    async def shutdown(self) -> None:
        pass
