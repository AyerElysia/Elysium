"""记忆系统可视化 Web 端点。"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
import uuid
from typing import TYPE_CHECKING, Any, AsyncIterator, ClassVar

from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from src.app.plugin_system.base import BaseRouter

from ..service.static_page import render_dashboard
from .eligibility import (
    assess_indexed_document_path,
    eligible_document_path_sql,
    register_indexed_path_sql_function,
)
from .sqlite_runtime import run_db

if TYPE_CHECKING:
    from ..core.plugin import LifeEnginePlugin

class ActivateRequest(BaseModel):
    seed_ids: list[str]
    max_depth: int = 2
    max_results: int = 20


class VectorHealthResponse(BaseModel):
    """Stable API contract for configured vector-backend health."""

    model_config = ConfigDict(extra="allow")

    available: bool
    expected: bool = True
    disabled: bool = False
    degraded: bool = False


class MemoryHealthResponse(BaseModel):
    """Public read-only memory health response; detailed sections may evolve."""

    model_config = ConfigDict(extra="allow")

    status: str
    sqlite: dict[str, Any] = Field(default_factory=dict)
    index: dict[str, Any] = Field(default_factory=dict)
    outbox: dict[str, Any] = Field(default_factory=dict)
    edges: dict[str, Any] = Field(default_factory=dict)
    living_memory: dict[str, Any] = Field(default_factory=dict)
    vector: VectorHealthResponse


def _table_exists(db: sqlite3.Connection, table: str) -> bool:
    try:
        return db.execute(
            "SELECT 1 FROM sqlite_master WHERE type IN ('table', 'virtual table') AND name = ?",
            (table,),
        ).fetchone() is not None
    except sqlite3.ProgrammingError:
        raise
    except sqlite3.Error:
        return False


def _table_columns(db: sqlite3.Connection, table: str) -> set[str]:
    if not _table_exists(db, table):
        return set()
    try:
        return {str(row[1]) for row in db.execute(f"PRAGMA table_info({table})").fetchall()}
    except sqlite3.ProgrammingError:
        raise
    except sqlite3.Error:
        return set()


def _row_value(row: sqlite3.Row, name: str, default: Any = None) -> Any:
    return row[name] if name in row.keys() else default


def _is_deleted(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    try:
        return bool(int(value or 0))
    except (TypeError, ValueError):
        return bool(value)


def _node_type(value: Any) -> str:
    raw = getattr(value, "value", value)
    return str("file" if raw is None else raw).lower()


def _graph_node_type(row: sqlite3.Row) -> str | None:
    if not _row_value(row, "node_id") or _is_deleted(_row_value(row, "is_deleted")):
        return None
    node_type = _node_type(_row_value(row, "node_type"))
    if node_type == "concept":
        return node_type
    if node_type != "file":
        return None
    if not assess_indexed_document_path(_row_value(row, "file_path")).eligible:
        return None
    return node_type


def _activation_value(row: sqlite3.Row) -> float:
    try:
        return float(_row_value(row, "activation_strength", 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _graph_visibility_sql(columns: set[str], alias: str) -> tuple[str, list[Any]]:
    node_type_expr = (
        f"lower(COALESCE({alias}.node_type, 'file'))"
        if "node_type" in columns
        else "'file'"
    )
    if "file_path" in columns:
        eligibility_sql, eligibility_params = eligible_document_path_sql(f"{alias}.file_path")
        file_clause = (
            f"{alias}.file_path IS NOT NULL AND TRIM({alias}.file_path) <> '' "
            f"AND {eligibility_sql}"
        )
    else:
        file_clause = "0 = 1"
        eligibility_params = []
    clauses = [
        f"({node_type_expr} = 'concept' OR ({node_type_expr} = 'file' AND ({file_clause})))"
    ]
    if "is_deleted" in columns:
        clauses.append(f"COALESCE({alias}.is_deleted, 0) = 0")
    return " AND ".join(clauses), eligibility_params


def _empty_graph() -> dict[str, list[dict[str, Any]]]:
    return {"nodes": [], "links": []}


def _serialize_graph_node(row: sqlite3.Row, node_type: str, degree: int) -> dict[str, Any]:
    file_path = _row_value(row, "file_path") if node_type == "file" else None
    return {
        "id": _row_value(row, "node_id"),
        "type": node_type.upper(),
        "title": _row_value(row, "title") or file_path or "Untitled",
        "path": file_path,
        "activation": _activation_value(row),
        "importance": float(_row_value(row, "importance", 0.0) or 0.0),
        "valence": float(_row_value(row, "emotional_valence", 0.0) or 0.0),
        "arousal": float(_row_value(row, "emotional_arousal", 0.0) or 0.0),
        "access_count": int(_row_value(row, "access_count", 0) or 0),
        "updated_at": _row_value(row, "updated_at"),
        "last_accessed_at": _row_value(row, "last_accessed_at"),
        "degree": degree,
    }


def _serialize_graph_link(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": _row_value(row, "edge_id"),
        "source": _row_value(row, "source_id"),
        "target": _row_value(row, "target_id"),
        "type": _row_value(row, "edge_type"),
        "weight": float(_row_value(row, "weight", 0.0) or 0.0),
        "base_strength": float(_row_value(row, "base_strength", 0.0) or 0.0),
        "reinforcement": float(_row_value(row, "reinforcement", 0.0) or 0.0),
        "activation_count": int(_row_value(row, "activation_count", 0) or 0),
        "last_activated_at": _row_value(row, "last_activated_at"),
        "reason": _row_value(row, "reason") or "",
    }


async def _read_graph_payload(
    db: sqlite3.Connection,
    *,
    limit_nodes: int,
    min_weight: float,
    focus_id: str | None,
) -> dict[str, list[dict[str, Any]]]:
    """Read and filter a graph snapshot without writing SQLite state."""

    def _read() -> dict[str, list[dict[str, Any]]]:
        if not _table_exists(db, "memory_nodes"):
            return _empty_graph()

        node_columns = _table_columns(db, "memory_nodes")
        if "node_id" not in node_columns:
            return _empty_graph()
        register_indexed_path_sql_function(db)
        visibility_sql, visibility_params = _graph_visibility_sql(node_columns, "n")
        cursor = db.cursor()

        if focus_id is not None:
            focus_row = cursor.execute(
                f"SELECT n.* FROM memory_nodes AS n WHERE n.node_id = ? AND {visibility_sql}",
                [focus_id, *visibility_params],
            ).fetchone()
            if focus_row is None or _graph_node_type(focus_row) is None:
                return _empty_graph()

            candidate_ids = [focus_id]
            if _table_exists(db, "memory_edges"):
                edge_columns = _table_columns(db, "memory_edges")
                if {"source_id", "target_id", "weight"} <= edge_columns:
                    source_visibility_sql, source_visibility_params = _graph_visibility_sql(
                        node_columns, "source_node"
                    )
                    target_visibility_sql, target_visibility_params = _graph_visibility_sql(
                        node_columns, "target_node"
                    )
                    edge_rows = cursor.execute(
                        f"SELECT e.source_id, e.target_id FROM memory_edges AS e "
                        f"JOIN memory_nodes AS source_node ON source_node.node_id = e.source_id "
                        f"JOIN memory_nodes AS target_node ON target_node.node_id = e.target_id "
                        f"WHERE e.weight >= ? AND (e.source_id = ? OR e.target_id = ?) "
                        f"AND {source_visibility_sql} AND {target_visibility_sql} "
                        "ORDER BY e.weight DESC",
                        [
                            min_weight,
                            focus_id,
                            focus_id,
                            *source_visibility_params,
                            *target_visibility_params,
                        ],
                    ).fetchall()
                    seen_ids = {focus_id}
                    for edge in edge_rows:
                        source_id = str(_row_value(edge, "source_id") or "")
                        target_id = str(_row_value(edge, "target_id") or "")
                        neighbor_id = target_id if source_id == focus_id else source_id
                        if neighbor_id and neighbor_id not in seen_ids:
                            candidate_ids.append(neighbor_id)
                            seen_ids.add(neighbor_id)
                        if len(candidate_ids) >= limit_nodes:
                            break

            placeholders = ",".join("?" for _ in candidate_ids)
            rows = cursor.execute(
                f"SELECT n.* FROM memory_nodes AS n "
                f"WHERE n.node_id IN ({placeholders}) AND {visibility_sql}",
                [*candidate_ids, *visibility_params],
            ).fetchall()
            rows_by_id = {
                str(_row_value(row, "node_id")): row
                for row in rows
                if _graph_node_type(row) is not None
            }
            if focus_id not in rows_by_id:
                return _empty_graph()
            selected_rows = [rows_by_id[focus_id]]
            selected_rows.extend(
                sorted(
                    (
                        row
                        for node_id, row in rows_by_id.items()
                        if node_id != focus_id
                    ),
                    key=lambda row: (-_activation_value(row), str(_row_value(row, "node_id"))),
                )
            )
        else:
            order_sql = (
                "COALESCE(n.activation_strength, 0.0) DESC, n.node_id"
                if "activation_strength" in node_columns
                else "n.node_id"
            )
            rows = cursor.execute(
                f"SELECT n.* FROM memory_nodes AS n WHERE {visibility_sql} "
                f"ORDER BY {order_sql} LIMIT ?",
                [*visibility_params, limit_nodes],
            ).fetchall()
            selected_rows = [row for row in rows if _graph_node_type(row) is not None]

        selected_ids = [str(_row_value(row, "node_id")) for row in selected_rows]
        if not selected_ids:
            return _empty_graph()

        degree_by_id = {node_id: 0 for node_id in selected_ids}
        links: list[dict[str, Any]] = []
        if _table_exists(db, "memory_edges"):
            edge_columns = _table_columns(db, "memory_edges")
            if {"source_id", "target_id", "weight"} <= edge_columns:
                placeholders = ",".join("?" for _ in selected_ids)
                edge_order = "weight DESC"
                if "activation_count" in edge_columns:
                    edge_order += ", activation_count DESC"
                edge_rows = cursor.execute(
                    f"SELECT * FROM memory_edges WHERE weight >= ? "
                    f"AND source_id IN ({placeholders}) AND target_id IN ({placeholders}) "
                    f"ORDER BY {edge_order}",
                    [min_weight, *selected_ids, *selected_ids],
                ).fetchall()
                selected_id_set = set(selected_ids)
                for edge in edge_rows:
                    source_id = str(_row_value(edge, "source_id") or "")
                    target_id = str(_row_value(edge, "target_id") or "")
                    if source_id not in selected_id_set or target_id not in selected_id_set:
                        continue
                    links.append(_serialize_graph_link(edge))
                    degree_by_id[source_id] += 1
                    if target_id != source_id:
                        degree_by_id[target_id] += 1

        nodes = []
        for row in selected_rows:
            node_id = str(_row_value(row, "node_id"))
            node_type = _graph_node_type(row)
            if node_type is not None:
                nodes.append(_serialize_graph_node(row, node_type, degree_by_id[node_id]))
        return {"nodes": nodes, "links": links}

    try:
        return await run_db(_read)
    except sqlite3.ProgrammingError as exc:
        if "created in a thread" not in str(exc):
            raise
        return _read()


def _is_visible_activation_node(node: Any) -> bool:
    if _is_deleted(getattr(node, "is_deleted", 0)):
        return False
    node_type = _node_type(getattr(node, "node_type", None))
    if node_type == "concept":
        return True
    return node_type == "file" and assess_indexed_document_path(
        getattr(node, "file_path", None)
    ).eligible


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
            if not memory or not memory.available:
                return {"status": "disabled", "nodes": [], "links": []}

            safe_limit = max(10, min(int(limit_nodes), 200))
            safe_weight = max(0.0, min(float(min_weight), 1.0))
            payload = await memory.read_graph_projection(
                limit_nodes=safe_limit,
                min_weight=safe_weight,
                focus_id=focus_id,
            )
            payload["meta"] = {
                "focus_id": focus_id,
                "limit_nodes": safe_limit,
                "min_weight": safe_weight,
            }
            return payload

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

        @self.app.get("/api/health", response_model=MemoryHealthResponse)
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
                if node is None or not _is_visible_activation_node(node):
                    continue
                association_data.append(
                    {
                        "id": node_id,
                        "title": node.title,
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

            results = await memory.search_memory(query, top_k=top_k, return_bundles=False)
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
                if assess_indexed_document_path(item.file_path).eligible
            ]
            return response
