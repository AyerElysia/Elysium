"""HTTP and WebSocket entry points for the Surface Gateway."""

from __future__ import annotations

from fastapi import WebSocket

from src.core.components.base.router import BaseRouter


class NekoSurfaceRouter(BaseRouter):
    router_name = "neko_surface"
    router_description = "Authenticated elysia.surface.v1 gateway（旧路由，已由 /api/v1/surfaces 取代）"
    custom_route_path = "/api/neko-surface"
    deprecation_notice = "已被 /api/v1/surfaces 统一接口取代，请迁移；迁移期保留"
    deprecation_sunset_date = "2027-02-01"
    deprecation_migration_link = "https://github.com/AyerElysia/Elysium/tree/main/docs/api"

    def register_endpoints(self) -> None:
        gateway = self.plugin.gateway

        @self.app.get("/status")
        async def status() -> dict:
            return await gateway.snapshot()

        @self.app.websocket("/ws")
        async def websocket_endpoint(websocket: WebSocket) -> None:
            await gateway.serve(websocket)
