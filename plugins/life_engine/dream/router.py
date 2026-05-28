"""做梦系统（潜意识观测台） Web 端点。"""

from __future__ import annotations

from typing import Any

from fastapi.responses import HTMLResponse

from src.app.plugin_system.base import BaseRouter

from ..service.static_page import render_dashboard

class DreamRouter(BaseRouter):
    """潜意识观测台可视化路由。"""

    router_name = "dream"
    router_description = "Subconscious Observatory"
    custom_route_path = "/dream_vis"

    def register_endpoints(self) -> None:
        @self.app.get("/", response_class=HTMLResponse)
        async def get_dashboard() -> Any:
            """返回潜意识观测台面板 HTML。"""
            return render_dashboard("dream_dashboard.html", "Dream Dashboard")

        @self.app.get("/api/events")
        async def events_stream() -> Any:
            """代理到 memory_vis 的 SSE 端点，前端可直接访问 /memory_vis/api/events。"""
            from fastapi.responses import JSONResponse
            return JSONResponse(
                content={"redirect": "/memory_vis/api/events",
                         "hint": "请直接访问 /memory_vis/api/events 获取 SSE 事件流"},
                status_code=307,
                headers={"Location": "/memory_vis/api/events"},
            )
