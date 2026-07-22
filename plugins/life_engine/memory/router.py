"""记忆系统可视化 Web 端点。"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import TYPE_CHECKING, Any, AsyncIterator, ClassVar

from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

from src.app.plugin_system.base import BaseRouter

from ..service.static_page import render_dashboard

if TYPE_CHECKING:
    from ..core.plugin import LifeEnginePlugin

class ActivateRequest(BaseModel):
    seed_ids: list[str]
    max_depth: int = 2
    max_results: int = 20


class MemoryRouter(BaseRouter):
    """仿生记忆系统可视化路由。"""

    router_name = "memory"
    router_description = "Bionic Memory System Visualization"
    custom_route_path = "/memory_vis"

    _subscribers: ClassVar[list[asyncio.Queue[dict[str, Any] | None]]] = []

    @classmethod
    def broadcast(cls, event_type: str, payload: dict[str, Any], source: str = "memory") -> None:
        event = {
            "event_id": str(uuid.uuid4()),
            "ts": time.time(),
            "type": event_type,
            "source": source,
            "payload": payload,
        }
        stale: list[asyncio.Queue[dict[str, Any] | None]] = []
        for queue in cls._subscribers:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                stale.append(queue)
            except Exception:
                stale.append(queue)
        if stale:
            cls._subscribers = [queue for queue in cls._subscribers if queue not in stale]

    def register_endpoints(self) -> None:
        def get_memory_service() -> Any:
            plugin: "LifeEnginePlugin" = self.plugin  # type: ignore
            service = plugin.service
            return getattr(service, "_memory_service", None)

        async def build_graph_payload(
            limit_nodes: int = 80,
            min_weight: float = 0.15,
            focus_id: str | None = None,
        ) -> dict[str, Any]:
            memory = get_memory_service()
            if not memory or not memory._db:
                return {"status": "disabled", "nodes": [], "links": []}

            cursor = memory._db.cursor()
            safe_limit = max(10, min(int(limit_nodes), 200))
            safe_weight = max(0.0, min(float(min_weight), 1.0))

            if focus_id:
                node_ids: set[str] = {focus_id}
                cursor.execute(
                    "SELECT source_id, target_id FROM memory_edges WHERE weight >= ? AND (source_id = ? OR target_id = ?)",
                    (safe_weight, focus_id, focus_id),
                )
                for row in cursor.fetchall():
                    node_ids.add(row["source_id"])
                    node_ids.add(row["target_id"])
                ordered_ids = [focus_id] + [node_id for node_id in node_ids if node_id != focus_id]
                node_id_list = ordered_ids[:safe_limit]
                if not node_id_list:
                    return {"nodes": [], "links": []}
                placeholders = ",".join("?" for _ in node_id_list)
                cursor.execute(
                    f"""
                    SELECT n.*, (
                        SELECT COUNT(*) FROM memory_edges e
                        WHERE e.weight >= ? AND (e.source_id = n.node_id OR e.target_id = n.node_id)
                    ) AS degree
                    FROM memory_nodes n
                    WHERE n.node_id IN ({placeholders})
                    ORDER BY n.activation_strength DESC
                    """,
                    [safe_weight, *node_id_list],
                )
            else:
                cursor.execute(
                    """
                    SELECT n.*, (
                        SELECT COUNT(*) FROM memory_edges e
                        WHERE e.weight >= ? AND (e.source_id = n.node_id OR e.target_id = n.node_id)
                    ) AS degree
                    FROM memory_nodes n
                    ORDER BY n.activation_strength DESC
                    LIMIT ?
                    """,
                    (safe_weight, safe_limit),
                )

            node_rows = cursor.fetchall()
            nodes = []
            node_ids = []
            for row in node_rows:
                nodes.append(
                    {
                        "id": row["node_id"],
                        "type": str(row["node_type"] or "").upper(),
                        "title": row["title"] or row["file_path"] or "Untitled",
                        "path": row["file_path"],
                        "activation": float(row["activation_strength"] or 0.0),
                        "importance": float(row["importance"] or 0.0),
                        "valence": float(row["emotional_valence"] or 0.0),
                        "arousal": float(row["emotional_arousal"] or 0.0),
                        "access_count": int(row["access_count"] or 0),
                        "updated_at": row["updated_at"],
                        "last_accessed_at": row["last_accessed_at"],
                        "degree": int(row["degree"] or 0),
                    }
                )
                node_ids.append(row["node_id"])

            if not node_ids:
                return {"nodes": [], "links": []}

            placeholders = ",".join("?" for _ in node_ids)
            cursor.execute(
                f"""
                SELECT * FROM memory_edges
                WHERE weight >= ?
                  AND source_id IN ({placeholders})
                  AND target_id IN ({placeholders})
                ORDER BY weight DESC, activation_count DESC
                """,
                [safe_weight, *node_ids, *node_ids],
            )
            edge_rows = cursor.fetchall()
            links = []
            for row in edge_rows:
                links.append(
                    {
                        "id": row["edge_id"],
                        "source": row["source_id"],
                        "target": row["target_id"],
                        "type": row["edge_type"],
                        "weight": float(row["weight"] or 0.0),
                        "base_strength": float(row["base_strength"] or 0.0),
                        "reinforcement": float(row["reinforcement"] or 0.0),
                        "activation_count": int(row["activation_count"] or 0),
                        "last_activated_at": row["last_activated_at"],
                        "reason": row["reason"] or "",
                    }
                )

            return {
                "nodes": nodes,
                "links": links,
                "meta": {
                    "focus_id": focus_id,
                    "limit_nodes": safe_limit,
                    "min_weight": safe_weight,
                },
            }

        @self.app.get("/", response_class=HTMLResponse)
        async def get_dashboard() -> Any:
            """返回记忆系统面板 HTML。"""
            return render_dashboard("memory_dashboard.html", "Memory Dashboard")

        @self.app.get("/api/events")
        async def events_stream() -> StreamingResponse:
            queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(maxsize=100)
            self._subscribers.append(queue)

            async def generate() -> AsyncIterator[str]:
                try:
                    snapshot = await build_graph_payload(limit_nodes=80, min_weight=0.15)
                    yield f"event: snapshot\ndata: {json.dumps(snapshot, ensure_ascii=False)}\n\n"
                    while True:
                        try:
                            event = await asyncio.wait_for(queue.get(), timeout=25)
                        except asyncio.TimeoutError:
                            yield ": heartbeat\n\n"
                            continue
                        if event is None:
                            break
                        yield f"event: {event['type']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                finally:
                    try:
                        self._subscribers.remove(queue)
                    except ValueError:
                        pass

            return StreamingResponse(
                generate(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "X-Accel-Buffering": "no",
                },
            )

        @self.app.get("/api/stats")
        async def get_stats() -> Any:
            """获取记忆系统全局统计。"""
            memory = get_memory_service()
            if not memory:
                return JSONResponse(content={"status": "disabled"}, status_code=503)
            stats = await memory.get_stats()
            return stats

        @self.app.get("/api/health")
        async def get_health() -> Any:
            """返回只读记忆健康快照。"""
            memory = get_memory_service()
            if not memory:
                return JSONResponse(content={"status": "disabled"}, status_code=503)
            return await memory.health_snapshot()

        @self.app.get("/api/graph")
        async def get_graph(
            limit_nodes: int = 80,
            min_weight: float = 0.15,
            focus_id: str | None = None,
        ) -> Any:
            """返回记忆图谱数据（节点与边）。"""
            payload = await build_graph_payload(limit_nodes=limit_nodes, min_weight=min_weight, focus_id=focus_id)
            if payload.get("status") == "disabled":
                return JSONResponse(content={"status": "disabled"}, status_code=503)
            return payload

        @self.app.post("/api/activate")
        async def activate_memory(req: ActivateRequest) -> Any:
            """执行激活扩散并返回路径。"""
            memory = get_memory_service()
            if not memory:
                return JSONResponse(content={"status": "disabled"}, status_code=503)

            results = await memory.spread_activation(
                req.seed_ids,
                max_depth=req.max_depth,
                max_results=req.max_results,
            )

            association_data = []
            for node_id, score, path, reason in results:
                node = await memory._get_node_by_id(node_id)
                association_data.append(
                    {
                        "id": node_id,
                        "title": node.title if node else "Unknown",
                        "score": score,
                        "path": path,
                        "reason": reason,
                    }
                )

            self.broadcast(
                "memory.activation.spread",
                {
                    "seed_ids": req.seed_ids,
                    "results": association_data,
                },
                source="api",
            )
            return association_data

        @self.app.get("/api/search")
        async def search_memory(query: str, top_k: int = 5) -> Any:
            """搜索记忆节点，用于查找激活起点。"""
            memory = get_memory_service()
            if not memory:
                return JSONResponse(content={"status": "disabled"}, status_code=503)

            results = await memory.search_memory(query, top_k=top_k)
            response = [
                {
                    "file_path": item.file_path,
                    "title": item.title,
                    "snippet": item.snippet,
                    "relevance": item.relevance,
                    "source": item.source,
                    "association_path": item.association_path,
                    "association_reason": item.association_reason,
                }
                for item in results
            ]
            return response
