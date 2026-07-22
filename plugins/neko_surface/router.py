"""HTTP and WebSocket entry points for the Surface Gateway."""

from __future__ import annotations

from fastapi import WebSocket

from src.core.components.base.router import BaseRouter


class NekoSurfaceRouter(BaseRouter):
    router_name = "neko_surface"
    router_description = "Authenticated elysia.surface.v1 gateway"
    custom_route_path = "/api/neko-surface"

    def register_endpoints(self) -> None:
        gateway = self.plugin.gateway

        @self.app.get("/status")
        async def status() -> dict:
            return await gateway.snapshot()

        @self.app.websocket("/ws")
        async def websocket_endpoint(websocket: WebSocket) -> None:
            await gateway.serve(websocket)
