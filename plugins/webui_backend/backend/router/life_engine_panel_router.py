"""life_engine 可视化面板路由。

提供事件流与待办（TODO）的实时读取接口，供前端面板展示：
- HTTP 快照接口
- WebSocket 实时推送接口
"""

from __future__ import annotations

import asyncio
import json
import tomllib
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Query, WebSocket, WebSocketDisconnect

from src.core.components.base.router import BaseRouter
from src.core.config.core_config import get_core_config
from src.core.utils.security import VerifiedDep
from src.kernel.logger import get_logger

logger = get_logger(name="LifeEnginePanel", color="#F2CDCD")

_LIFE_CONFIG_PATH = Path("config/plugins/life_engine/config.toml")
_DEFAULT_WORKSPACE = Path("data/life_engine_workspace")
_CONTEXT_FILE = "life_engine_context.json"
_TODO_FILE = "todos.json"
_INACTIVE_TODO_STATUSES = {"completed", "cancelled", "archived", "released", "cherished"}


def _load_json_file(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"读取 JSON 失败: {path} ({exc})")
        return default


def _resolve_life_workspace() -> Path:
    """解析 life_engine workspace 路径。"""
    workspace = _DEFAULT_WORKSPACE

    if _LIFE_CONFIG_PATH.exists():
        try:
            config_data = tomllib.loads(_LIFE_CONFIG_PATH.read_text(encoding="utf-8"))
            settings = config_data.get("settings")
            if isinstance(settings, dict):
                raw_workspace = settings.get("workspace_path")
                if isinstance(raw_workspace, str) and raw_workspace.strip():
                    workspace = Path(raw_workspace.strip())
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"读取 life_engine 配置失败，回退默认 workspace: {exc}")

    resolved = workspace.resolve()
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def _extract_todos_payload(todos_data: Any) -> tuple[int, list[dict[str, Any]], bool]:
    """兼容旧 list 型 TODO 和 v2 结构。"""
    if isinstance(todos_data, dict):
        version = int(todos_data.get("version") or 0)
        tasks = todos_data.get("tasks")
        return version, [item for item in tasks if isinstance(item, dict)] if isinstance(tasks, list) else [], False
    if isinstance(todos_data, list):
        return 1, [item for item in todos_data if isinstance(item, dict)], True
    return 0, [], False


def _todo_needs_review(todo: dict[str, Any]) -> bool:
    review_at = str(todo.get("next_review_at") or "").strip()
    if not review_at:
        return False
    try:
        text = f"{review_at[:-1]}+00:00" if review_at.endswith("Z") else review_at
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.astimezone()
        else:
            dt = dt.astimezone()
        return dt <= datetime.now().astimezone()
    except Exception:
        return False


def _todo_is_overdue(todo: dict[str, Any]) -> bool:
    due_at = str(todo.get("due_at") or todo.get("deadline") or "").strip()
    if not due_at:
        return False
    try:
        text = f"{due_at[:-1]}+00:00" if due_at.endswith("Z") else due_at
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.astimezone()
        else:
            dt = dt.astimezone()
        return dt < datetime.now().astimezone()
    except Exception:
        return False


def _todo_is_recurring(todo: dict[str, Any]) -> bool:
    recurrence = todo.get("recurrence")
    if isinstance(recurrence, dict):
        return str(recurrence.get("kind") or "none") != "none"
    return bool(recurrence and str(recurrence) != "none")


def _build_panel_snapshot(event_limit: int = 200, include_completed_todos: bool = False) -> dict[str, Any]:
    """构建 life_engine 面板快照。"""
    workspace = _resolve_life_workspace()
    context_path = workspace / _CONTEXT_FILE
    todo_path = workspace / _TODO_FILE

    context_data = _load_json_file(context_path, default={})
    todos_data = _load_json_file(todo_path, default={})

    state = context_data.get("state", {}) if isinstance(context_data, dict) else {}
    history_events = context_data.get("event_history", []) if isinstance(context_data, dict) else []
    pending_events = context_data.get("pending_events", []) if isinstance(context_data, dict) else []

    if not isinstance(history_events, list):
        history_events = []
    if not isinstance(pending_events, list):
        pending_events = []

    all_events: list[dict[str, Any]] = []
    for item in history_events + pending_events:
        if isinstance(item, dict):
            all_events.append(item)

    all_events.sort(key=lambda e: int(e.get("sequence") or 0))
    if event_limit > 0:
        all_events = all_events[-event_limit:]

    todo_version, todo_items, legacy_todos = _extract_todos_payload(todos_data)
    todos: list[dict[str, Any]] = []
    for item in todo_items:
        status = str(item.get("status") or "")
        if not include_completed_todos and status in _INACTIVE_TODO_STATUSES:
            continue
        todos.append(item)

    todos.sort(key=lambda t: str(t.get("updated_at") or t.get("created_at") or ""), reverse=True)
    active_todos = [item for item in todo_items if str(item.get("status") or "") not in _INACTIVE_TODO_STATUSES]

    return {
        "workspace_path": str(workspace),
        "context_file": str(context_path),
        "todo_file": str(todo_path),
        "state": state if isinstance(state, dict) else {},
        "stats": {
            "event_count": len(all_events),
            "history_event_count": len(history_events),
            "pending_event_count": len(pending_events),
            "todo_count": len(todos),
            "todo_version": todo_version,
            "legacy_todos": legacy_todos,
            "active_todo_count": len(active_todos),
            "overdue_todo_count": sum(1 for item in active_todos if _todo_is_overdue(item)),
            "needs_review_todo_count": sum(1 for item in active_todos if _todo_needs_review(item)),
            "recurring_todo_count": sum(1 for item in active_todos if _todo_is_recurring(item)),
        },
        "events": all_events,
        "todos": todos,
    }


class LifeEnginePanelRouter(BaseRouter):
    """life_engine 面板路由。"""

    router_name = "LifeEnginePanel"
    router_description = "生命中枢事件流与待办可视化接口"
    custom_route_path = "/webui/api/life_panel"
    cors_origins = []

    def register_endpoints(self) -> None:
        @self.app.get("/snapshot")
        async def get_snapshot(
            event_limit: int = Query(200, ge=1, le=2000, description="事件返回上限"),
            include_completed_todos: bool = Query(False, description="是否包含已完成/已释怀 TODO"),
            _=VerifiedDep,
        ) -> dict[str, Any]:
            """获取面板快照（事件流 + TODO）。"""
            try:
                return _build_panel_snapshot(
                    event_limit=event_limit,
                    include_completed_todos=include_completed_todos,
                )
            except Exception as exc:  # noqa: BLE001
                logger.error(f"获取 life_panel 快照失败: {exc}", exc_info=True)
                raise HTTPException(status_code=500, detail=str(exc))

        @self.app.get("/events")
        async def get_events(
            limit: int = Query(200, ge=1, le=2000, description="返回事件条数"),
            _=VerifiedDep,
        ) -> dict[str, Any]:
            """仅获取事件流。"""
            snapshot = _build_panel_snapshot(event_limit=limit, include_completed_todos=True)
            return {
                "stats": snapshot.get("stats", {}),
                "events": snapshot.get("events", []),
            }

        @self.app.get("/todos")
        async def get_todos(
            include_completed: bool = Query(False, description="是否包含已完成/已释怀 TODO"),
            _=VerifiedDep,
        ) -> dict[str, Any]:
            """仅获取 TODO 列表。"""
            snapshot = _build_panel_snapshot(
                event_limit=1,
                include_completed_todos=include_completed,
            )
            return {
                "todo_count": len(snapshot.get("todos", [])),
                "todos": snapshot.get("todos", []),
            }

        @self.app.websocket("/ws")
        async def ws_snapshot(
            websocket: WebSocket,
            api_key: str = Query(..., description="API 密钥"),
            event_limit: int = Query(200, ge=1, le=2000, description="事件返回上限"),
            include_completed_todos: bool = Query(False, description="是否包含已完成/已释怀 TODO"),
        ):
            """实时推送 life_engine 面板快照。"""
            try:
                config = get_core_config()
                valid_keys = config.http_router.api_keys
            except RuntimeError:
                await websocket.close(code=4001, reason="服务配置未初始化")
                return

            if not valid_keys or api_key not in valid_keys:
                await websocket.close(code=4003, reason="无效的 API 密钥")
                return

            await websocket.accept()
            last_payload = ""

            try:
                while True:
                    snapshot = _build_panel_snapshot(
                        event_limit=event_limit,
                        include_completed_todos=include_completed_todos,
                    )
                    payload = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, default=str)
                    if payload != last_payload:
                        await websocket.send_json({"type": "snapshot", "data": snapshot})
                        last_payload = payload

                    try:
                        client_msg = await asyncio.wait_for(websocket.receive_text(), timeout=2.0)
                        if client_msg.strip().lower() == "ping":
                            await websocket.send_text("pong")
                    except asyncio.TimeoutError:
                        pass
            except WebSocketDisconnect:
                logger.info("life_panel WebSocket 客户端已断开")
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"life_panel WebSocket 异常: {exc}")

    async def startup(self) -> None:
        logger.info(f"life_panel 路由已启动，路径: {self.custom_route_path}")

    async def shutdown(self) -> None:
        logger.info("life_panel 路由已关闭")
