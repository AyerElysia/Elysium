"""life 与 chatter 联合消息时间线可视化。"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from fastapi.responses import HTMLResponse, JSONResponse

from src.app.plugin_system.base import BaseRouter

from ..service.static_page import render_dashboard

if TYPE_CHECKING:
    from ..core.plugin import LifeEnginePlugin

class MessageTimelineRouter(BaseRouter):
    """联合消息时间线面板。"""

    router_name = "message_timeline"
    router_description = "Life / Chatter Message Timeline"
    custom_route_path = "/message_timeline"

    def register_endpoints(self) -> None:
        @self.app.get("/", response_class=HTMLResponse)
        async def get_dashboard() -> Any:
            return render_dashboard(
                "life_message_dashboard.html",
                "Message Timeline Dashboard",
            )

        @self.app.get("/api/snapshot")
        async def get_snapshot(
            event_limit: int = 24,
            stream_limit: int = 12,
            message_limit: int = 8,
        ) -> Any:
            plugin: "LifeEnginePlugin" = self.plugin  # type: ignore
            service = plugin.service
            snapshot = await service.get_message_observability_snapshot(
                event_limit=event_limit,
                stream_limit=stream_limit,
                message_limit=message_limit,
            )
            return snapshot

        @self.app.get("/api/stream/{stream_id}")
        async def get_stream_snapshot(stream_id: str) -> Any:
            plugin: "LifeEnginePlugin" = self.plugin  # type: ignore
            service = plugin.service
            snapshot = await service.get_message_observability_snapshot(stream_limit=50, message_limit=20)
            for stream in snapshot.get("streams", []):
                if str(stream.get("stream_id", "")) == stream_id:
                    return stream
            return JSONResponse(content={"error": "stream not found"}, status_code=404)

        @self.app.get("/api/history_search")
        async def history_search(
            query: str = "",
            stream_id: str = "",
            cross_stream: bool = True,
            limit: int = 20,
            source_mode: str = "auto",
            include_tool_calls: bool = True,
        ) -> Any:
            from ..tools.conversation_evidence import LifeEngineConversationEvidenceTool

            plugin: "LifeEnginePlugin" = self.plugin  # type: ignore
            tool = LifeEngineConversationEvidenceTool(plugin=plugin)
            tool._bind_runtime_context(stream_id=stream_id.strip())
            ok, data = await tool.execute(
                operation="search" if query else "page",
                query=query,
                stream_ids=[stream_id] if stream_id.strip() else None,
                limit=limit,
            )
            if ok:
                return json.loads(data)
            return JSONResponse(content=json.loads(data), status_code=400)
