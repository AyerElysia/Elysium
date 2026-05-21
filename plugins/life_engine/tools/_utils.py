"""life_engine 工具层公共工具函数。

本模块集中存放各工具模块之间共享的基础工具函数，
避免跨文件重复定义，消除耦合。

公共函数：
  _get_workspace(plugin)               → Path
  _resolve_path(plugin, relative_path) → (bool, Path | str)
  _load_life_context_events(plugin)    → list[dict]
  _pick_latest_target_stream_id(plugin)→ str | None
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.app.plugin_system.api import log_api

from ..core.config import LifeEngineConfig

logger = log_api.get_logger("life_engine.tools")


def _get_workspace(plugin: Any) -> Path:
    """获取工作空间路径。"""
    config = getattr(plugin, "config", None)
    if isinstance(config, LifeEngineConfig):
        workspace = config.settings.workspace_path
    else:
        workspace = str(Path(__file__).parent.parent.parent.parent / "data" / "life_engine_workspace")
    path = Path(workspace).resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


def _resolve_path(plugin: Any, relative_path: str) -> tuple[bool, Path | str]:
    """解析并验证路径在 workspace 内。

    Returns:
        (True, Path) 如果路径有效
        (False, error_message) 如果路径无效或超出 workspace
    """
    workspace = _get_workspace(plugin)

    clean_path = relative_path.strip().lstrip("/\\")
    if not clean_path:
        clean_path = "."

    try:
        target = (workspace / clean_path).resolve()
    except Exception as e:
        return False, f"路径解析失败: {e}"

    try:
        target.relative_to(workspace)
    except ValueError:
        return False, f"路径超出工作空间范围。工作空间: {workspace}"

    return True, target


def _load_life_context_events(plugin: Any) -> list[dict[str, Any]]:
    """加载 life_engine 持久化上下文中的事件列表。"""
    workspace = _get_workspace(plugin)
    context_file = workspace / "life_engine_context.json"
    if not context_file.exists():
        return []

    try:
        data = json.loads(context_file.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        logger.warning(f"读取 life_engine_context.json 失败: {e}")
        return []

    if not isinstance(data, dict):
        return []

    history = data.get("event_history")
    pending = data.get("pending_events")
    if not isinstance(history, list):
        history = []
    if not isinstance(pending, list):
        pending = []

    events: list[dict[str, Any]] = []
    for item in history + pending:
        if isinstance(item, dict):
            events.append(item)
    events.sort(key=lambda e: int(e.get("sequence") or 0))
    return events


def _pick_latest_target_stream_id(plugin: Any) -> str | None:
    """从事件流中挑选最近可用的目标 stream_id。"""
    events = _load_life_context_events(plugin)
    if not events:
        return None

    # 优先：最近一条外部入站消息
    for event in reversed(events):
        if str(event.get("event_type") or "") != "message":
            continue
        stream_id = str(event.get("stream_id") or "").strip()
        if not stream_id:
            continue
        source = str(event.get("source") or "")
        source_detail = str(event.get("source_detail") or "")
        if source != "life_engine" and "入站" in source_detail:
            return stream_id

    # 退化：最近一条外部消息（不区分入站/出站）
    for event in reversed(events):
        if str(event.get("event_type") or "") != "message":
            continue
        stream_id = str(event.get("stream_id") or "").strip()
        if not stream_id:
            continue
        source = str(event.get("source") or "")
        if source != "life_engine":
            return stream_id

    return None
