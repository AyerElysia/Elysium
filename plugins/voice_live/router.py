"""Voice Live 路由。

提供 FastAPI WebSocket 端点和静态页面服务，
实现浏览器端全双工语音通话的通信协议。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse

from src.core.components.base.router import BaseRouter
from src.kernel.logger import get_logger

from .session import CallSession, SessionState

logger = get_logger("voice_live.router", display="Voice Live Router")

# 静态文件目录
STATIC_DIR = Path(__file__).parent / "static"


class VoiceLiveRouter(BaseRouter):
    """Voice Live 路由组件。

    端点：
    - GET /          -> 语音通话 Web 页面
    - WS /ws         -> 主 WebSocket 端点
    - GET /health    -> 健康检查
    """

    router_name = "voice_live"
    router_description = "全双工实时语音通话"
    custom_route_path = "/voice-live"
    cors_origins = ["*"]

    def __init__(self, plugin: Any) -> None:
        # 活跃会话映射
        self._sessions: dict[str, CallSession] = {}
        super().__init__(plugin)

    def register_endpoints(self) -> None:
        """注册路由端点。"""

        @self.app.get("/", response_class=HTMLResponse)
        async def index():
            """提供语音通话 Web 页面。"""
            html_file = STATIC_DIR / "voice_live.html"
            if html_file.exists():
                return HTMLResponse(content=html_file.read_text(encoding="utf-8"))
            return HTMLResponse(content="<h1>Voice Live</h1><p>页面未找到</p>", status_code=404)

        @self.app.get("/health")
        async def health():
            """健康检查。"""
            config = self.plugin.config
            fd_config = config.full_duplex
            deg_config = config.degraded

            return JSONResponse(content={
                "status": "ok",
                "full_duplex_provider": fd_config.provider_type,
                "full_duplex_configured": bool(fd_config.upstream_url),
                "degraded_enabled": deg_config.enabled,
                "active_sessions": len(self._sessions),
            })

        @self.app.websocket("/ws")
        async def websocket_endpoint(ws: WebSocket):
            """主 WebSocket 端点。"""
            await self._handle_websocket(ws)

    # ------------------------------------------------------------------
    # WebSocket 处理
    # ------------------------------------------------------------------

    async def _handle_websocket(self, ws: WebSocket) -> None:
        """处理 WebSocket 连接。"""
        # 认证检查
        config = self.plugin.config
        server_config = config.server
        auth_token = server_config.auth_token

        await ws.accept()

        # 可选认证
        if auth_token:
            # 等待第一条消息作为认证
            try:
                first_msg = await asyncio.wait_for(ws.receive_text(), timeout=5.0)
                data = json.loads(first_msg)
                if data.get("type") != "auth" or data.get("token") != auth_token:
                    await ws.send_json({"type": "error", "message": "认证失败"})
                    await ws.close()
                    return
                await ws.send_json({"type": "auth_ok"})
            except (asyncio.TimeoutError, json.JSONDecodeError):
                await ws.send_json({"type": "error", "message": "认证超时"})
                await ws.close()
                return

        # 并发限制
        max_sessions = server_config.max_concurrent_sessions
        if len(self._sessions) >= max_sessions:
            await ws.send_json({"type": "error", "message": "达到最大并发数"})
            await ws.close()
            return

        # 创建会话
        session = CallSession(config)
        self._sessions[session.session_id] = session

        # 设置发送回调
        async def send_json(data: dict[str, Any]) -> None:
            try:
                await ws.send_json(data)
            except Exception:  # noqa: BLE001
                pass

        async def send_bytes(data: bytes) -> None:
            try:
                await ws.send_bytes(data)
            except Exception:  # noqa: BLE001
                pass

        session.set_send_callbacks(send_json, send_bytes)

        # 注入上下文（如果可用）
        await self._inject_context(session)

        logger.info(f"WebSocket 连接建立: {session.session_id}")

        try:
            await self._message_loop(ws, session)
        except WebSocketDisconnect:
            logger.info(f"WebSocket 断开: {session.session_id}")
        except Exception as exc:  # noqa: BLE001
            logger.error(f"WebSocket 异常: {exc}")
        finally:
            # 清理会话
            await session.stop()
            self._sessions.pop(session.session_id, None)

    async def _message_loop(self, ws: WebSocket, session: CallSession) -> None:
        """WebSocket 消息循环。"""
        while True:
            message = await ws.receive()

            if message.get("type") == "websocket.disconnect":
                break

            # 二进制帧 = 音频数据
            if "bytes" in message and message["bytes"]:
                await session.handle_audio(message["bytes"])
                continue

            # 文本帧 = JSON 控制消息
            if "text" in message and message["text"]:
                try:
                    data = json.loads(message["text"])
                    await session.handle_message(data)
                except json.JSONDecodeError:
                    logger.debug(f"无效 JSON: {message['text'][:100]}")

    # ------------------------------------------------------------------
    # 上下文注入
    # ------------------------------------------------------------------

    async def _inject_context(self, session: CallSession) -> None:
        """为会话注入意识上下文。"""
        try:
            from .context_bridge import ContextBridge

            bridge = ContextBridge(self.plugin.config)
            prompt = await bridge.build_system_prompt()
            if prompt:
                session.set_system_prompt(prompt)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"上下文注入跳过: {exc}")

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def startup(self) -> None:
        """路由启动。"""
        logger.info("Voice Live 路由已启动")

    async def shutdown(self) -> None:
        """路由关闭，清理所有会话。"""
        for session in list(self._sessions.values()):
            await session.stop()
        self._sessions.clear()
        logger.info("Voice Live 路由已关闭")
