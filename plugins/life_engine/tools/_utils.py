"""life_engine 工具层公共工具函数。

本模块集中存放各工具模块之间共享的基础工具函数，
避免跨文件重复定义，消除耦合。

公共函数：
  _get_workspace(plugin)               → Path
  _resolve_path(plugin, relative_path) → (bool, Path | str)
  resolve_registry_tool(registry, name) → tool class or None
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..core.config import LifeEngineConfig


def resolve_registry_tool(registry: Any, name: str) -> Any | None:
    """Look up a tool by schema name, tolerating transport and namespace prefixes.

    BaseTool schemas register as ``tool-{tool_name}``. Models often call the bare
    name, so both spellings must resolve. Two further spellings matter: most
    heartbeat tools carry a ``nucleus_`` namespace segment (``tool-nucleus_foo``)
    while a minority register without it (``tool-read_context_group``), and a
    model that has just called twenty namespaced tools routinely prefixes the
    bare ones too. Refusing that spelling reported a real tool as unknown and
    made the maintenance turn impossible to complete. Every addressing prefix is
    therefore optional, and a stripped name is re-expanded with each transport
    prefix so the lookup is symmetric.
    """

    raw_name = str(name or "").strip()
    if not raw_name:
        return None
    getter = getattr(registry, "get", None)
    if not callable(getter):
        return None

    candidates: list[str] = []

    def _add(value: str) -> None:
        if value and value not in candidates:
            candidates.append(value)

    _add(raw_name)
    bare = raw_name
    for prefix in ("tool-", "action-"):
        if bare.startswith(prefix):
            bare = bare[len(prefix) :]
            break
    _add(bare)
    if bare.startswith("nucleus_"):
        _add(bare[len("nucleus_") :])
    for value in list(candidates):
        if value in {"tool", "action"}:
            continue
        _add(f"tool-{value}")
        _add(f"action-{value}")
        if not value.startswith("nucleus_") and value not in {"tool", "action"}:
            _add(f"tool-nucleus_{value}")
            _add(f"action-nucleus_{value}")

    # A model that has just called twenty "tool-nucleus_*" tools routinely
    # addresses the one tool that registers in another convention the same way:
    # "tool-nucleus_action_author_self_continuity_checkpoint". Reduction to the
    # bare identity yields "author_self_continuity_checkpoint"; re-expanding it
    # in every registered convention recovers
    # "action-author_self_continuity_checkpoint". The reduction must be
    # exhaustive because the prefixes stack and the model also swaps the "-"
    # separator for "_" inside the identity, so one pass over the transport
    # prefixes cannot reach it.
    identity = raw_name
    while True:
        for prefix in (
            "tool-",
            "action-",
            "tool_",
            "action_",
            "nucleus-",
            "nucleus_",
        ):
            if identity.startswith(prefix):
                identity = identity[len(prefix) :]
                break
        else:
            break
    if identity and identity != raw_name:
        _add(identity)
        for transport in ("tool-", "action-"):
            _add(f"{transport}{identity}")
            _add(f"{transport}nucleus_{identity}")

    for candidate in candidates:
        found = getter(candidate)
        if found is not None:
            return found
    return None


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
