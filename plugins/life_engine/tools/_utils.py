"""life_engine 工具层公共工具函数。

本模块集中存放各工具模块之间共享的基础工具函数，
避免跨文件重复定义，消除耦合。

公共函数：
  _get_workspace(plugin)               → Path
  _resolve_path(plugin, relative_path) → (bool, Path | str)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..core.config import LifeEngineConfig


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
