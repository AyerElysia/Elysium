"""Livestream 路由。

提供 FastAPI WebSocket 端点和静态页面服务：
- GET /          -> 主页面（Live2D + 弹幕面板）
- WS /ws         -> 双向 WebSocket（音频流 + 控制帧 + 弹幕数据）
- GET /health    -> 健康检查
- POST /api/start -> 开始直播
- POST /api/stop  -> 停止直播
- POST /api/say   -> 手动触发发言
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from src.core.components.base.router import BaseRouter
from src.kernel.logger import get_logger

from .config import LivestreamConfig
from .consciousness import LivestreamConsciousnessManager
from .output.avatar_controller import AvatarController
from .output.tts_queue import TTSQueue
from .pipeline.event_filter import EventFilter
from .pipeline.llm_orchestrator import LLMOrchestrator
from .pipeline.priority_queue import PriorityEventQueue
from .pipeline.proactive import ProactiveEngine
from .pipeline.scheduler import PipelineScheduler, SchedulerState
from .platform.base import PlatformEvent
from .platform.factory import create_platform_adapter

logger = get_logger("livestream.router", display="AI直播 Router")

# 静态文件目录
STATIC_DIR = Path(__file__).parent / "static"


class SayRequest(BaseModel):
    """手动发言请求。"""
    text: str


class LivestreamRouter(BaseRouter):
    """AI 直播路由组件。"""

    router_name = "livestream"
    router_description = "AI 直播框架"
    custom_route_path = "/livestream"
    cors_origins = ["*"]

    def __init__(self, plugin: Any) -> None:
        self._config: LivestreamConfig = plugin.config
        self._ws_clients: list[WebSocket] = []
        self._running = False
        self._start_time: float = 0

        # 核心组件（延迟初始化）
        self._platform_adapter = None
        self._event_filter: EventFilter | None = None
        self._queue: PriorityEventQueue | None = None
        self._scheduler: PipelineScheduler | None = None
        self._llm: LLMOrchestrator | None = None
        self._tts: TTSQueue | None = None
        self._avatar: AvatarController | None = None
        self._proactive: ProactiveEngine | None = None
        self._consciousness = LivestreamConsciousnessManager(self._config)

        super().__init__(plugin)

    def register_endpoints(self) -> None:
        """注册路由端点。"""

        @self.app.get("/", response_class=HTMLResponse)
        async def index():
            """提供直播主页面。"""
            html_file = STATIC_DIR / "index.html"
            if html_file.exists():
                return HTMLResponse(content=html_file.read_text(encoding="utf-8"))
            return HTMLResponse(
                content="<h1>AI Livestream</h1><p>页面未找到</p>",
                status_code=404,
            )

        @self.app.get("/health")
        async def health():
            """健康检查。"""
            return JSONResponse({
                "status": "running" if self._running else "stopped",
                "uptime": time.time() - self._start_time if self._running else 0,
                "ws_clients": len(self._ws_clients),
                "scheduler": self._scheduler.stats if self._scheduler else None,
                "platform_connected": (
                    self._platform_adapter.connected
                    if self._platform_adapter else False
                ),
            })

        @self.app.post("/api/start")
        async def start_stream():
            """开始直播。"""
            if self._running:
                return JSONResponse({"message": "已在直播中"}, status_code=400)
            try:
                await self._start_pipeline()
                return JSONResponse({"message": "直播已开始"})
            except Exception as exc:
                await self._stop_pipeline()
                logger.error(f"启动直播失败: {exc}", exc_info=True)
                return JSONResponse(
                    {"message": f"启动失败: {exc}"}, status_code=500
                )

        @self.app.post("/api/stop")
        async def stop_stream():
            """停止直播。"""
            if not self._running:
                return JSONResponse({"message": "未在直播"}, status_code=400)
            await self._stop_pipeline()
            return JSONResponse({"message": "直播已停止"})

        @self.app.post("/api/say")
        async def say(request: SayRequest):
            """手动触发发言（调试用）。"""
            if not self._running or not self._tts:
                return JSONResponse({"message": "未在直播"}, status_code=400)
            await self._tts.speak(request.text)
            if self._llm:
                self._llm.record_external_response(request.text)
            return JSONResponse({"message": "已加入播放队列"})

        @self.app.websocket("/ws")
        async def websocket_endpoint(ws: WebSocket):
            """主 WebSocket 端点。"""
            await ws.accept()

            # 认证（可选）
            auth_token = self._config.server.auth_token
            if auth_token:
                try:
                    msg = await asyncio.wait_for(ws.receive_text(), timeout=5.0)
                    data = json.loads(msg)
                    if data.get("token") != auth_token:
                        await ws.close(code=4001, reason="认证失败")
                        return
                except (asyncio.TimeoutError, json.JSONDecodeError):
                    await ws.close(code=4001, reason="认证超时")
                    return

            self._ws_clients.append(ws)
            logger.info(f"WebSocket 客户端已连接，当前 {len(self._ws_clients)} 个")

            try:
                # 发送当前状态
                await ws.send_json({
                    "type": "state",
                    "state": self._scheduler.state.value if self._scheduler else "idle",
                    "running": self._running,
                })

                # 监听上行消息
                while True:
                    raw = await ws.receive_text()
                    try:
                        data = json.loads(raw)
                        await self._handle_ws_command(ws, data)
                    except json.JSONDecodeError:
                        pass

            except WebSocketDisconnect:
                pass
            finally:
                if ws in self._ws_clients:
                    self._ws_clients.remove(ws)
                logger.info(f"WebSocket 客户端断开，剩余 {len(self._ws_clients)} 个")

    async def _handle_ws_command(self, ws: WebSocket, data: dict) -> None:
        """处理 WebSocket 上行命令。"""
        cmd = data.get("command", "")
        match cmd:
            case "start":
                if not self._running:
                    await self._start_pipeline()
            case "stop":
                if self._running:
                    await self._stop_pipeline()
            case "interrupt":
                if self._scheduler:
                    await self._scheduler.interrupt()

    async def _start_pipeline(self) -> None:
        """启动完整的直播管线。"""
        config = self._config

        # 初始化组件
        self._event_filter = EventFilter(config)
        self._queue = PriorityEventQueue(config)
        await self._consciousness.activate()
        self._llm = LLMOrchestrator(
            config,
            consciousness=self._consciousness,
        )
        self._tts = TTSQueue(config)
        self._avatar = AvatarController(config)
        self._proactive = ProactiveEngine(config)

        # 初始化调度器
        self._scheduler = PipelineScheduler(
            config=config,
            queue=self._queue,
            llm=self._llm,
            tts=self._tts,
            avatar=self._avatar,
            proactive=self._proactive,
        )

        # 注册回调
        self._tts.on_audio_frame(self._broadcast_audio)
        self._avatar.on_command(self._broadcast_avatar_command)
        self._scheduler.on_state_change(self._broadcast_state)

        # 启动组件
        await self._llm.start()
        await self._tts.start()
        await self._avatar.start()
        await self._scheduler.start()

        # 启动平台适配器
        self._platform_adapter = create_platform_adapter(config)
        self._platform_adapter.on_event(self._on_platform_event)
        await self._platform_adapter.connect()

        self._running = True
        self._start_time = time.time()
        self._proactive.reset()

        logger.info("直播管线已全部启动")

    async def _stop_pipeline(self) -> None:
        """停止完整的直播管线。"""
        self._running = False

        if self._platform_adapter:
            await self._platform_adapter.disconnect()
        if self._scheduler:
            await self._scheduler.stop()
        if self._tts:
            await self._tts.stop()
        if self._avatar:
            await self._avatar.stop()
        if self._llm:
            await self._llm.stop()
        await self._consciousness.suspend()

        logger.info("直播管线已全部停止")

    async def _on_platform_event(self, event: PlatformEvent) -> None:
        """平台事件回调：过滤 → 调度器。"""
        if not self._event_filter or not self._scheduler:
            return

        # 广播弹幕到前端显示
        if event.kind == "danmaku":
            await self._broadcast_danmaku(event)

        # 进场事件交给主动行为引擎
        if event.kind == "enter":
            if self._proactive:
                self._proactive.add_welcome(event.user_name)

        # 过滤
        if not self._event_filter.should_pass(event):
            return

        # 送入调度器
        await self._scheduler.handle_event(event)

    async def _broadcast_audio(self, audio_bytes: bytes, metadata: dict) -> None:
        """广播音频帧到所有 WebSocket 客户端。"""
        # 先发元数据
        control = json.dumps({
            "type": "audio_meta",
            **metadata,
        }).encode()
        for ws in list(self._ws_clients):
            try:
                await ws.send_bytes(control)
                await ws.send_bytes(audio_bytes)
            except Exception:
                self._ws_clients.remove(ws)

    async def _broadcast_avatar_command(self, command: dict) -> None:
        """广播形象指令到所有 WebSocket 客户端。"""
        msg = json.dumps({"type": "avatar", **command})
        for ws in list(self._ws_clients):
            try:
                await ws.send_text(msg)
            except Exception:
                self._ws_clients.remove(ws)

    async def _broadcast_state(self, state: SchedulerState) -> None:
        """广播状态变更到所有 WebSocket 客户端。"""
        msg = json.dumps({"type": "state", "state": state.value})
        for ws in list(self._ws_clients):
            try:
                await ws.send_text(msg)
            except Exception:
                self._ws_clients.remove(ws)

    async def _broadcast_danmaku(self, event: PlatformEvent) -> None:
        """广播弹幕到前端显示。"""
        msg = json.dumps({
            "type": "danmaku",
            "user": event.user_name,
            "content": event.content,
            "kind": event.kind,
        })
        for ws in list(self._ws_clients):
            try:
                await ws.send_text(msg)
            except Exception:
                self._ws_clients.remove(ws)
