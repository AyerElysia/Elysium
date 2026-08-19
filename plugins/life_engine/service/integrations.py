"""life_engine 子系统集成模块。

包含 DFC 集成、记忆集成的初始化与管理。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from src.app.plugin_system.api.log_api import get_logger

from .event_builder import (
    EventType,
    _format_time_display,
    _now_iso,
    _shorten_text,
    is_life_heartbeat_event,
)

if TYPE_CHECKING:
    from .core import LifeEngineService


logger = get_logger("life_engine", display="life_engine")


def to_jsonable(value: Any) -> Any:
    """将复杂对象转换为 JSON 可序列化结构。

    Args:
        value: 要转换的值

    Returns:
        JSON 可序列化的值
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [to_jsonable(v) for v in value]
    if isinstance(value, datetime):
        return value.isoformat()

    tolist = getattr(value, "tolist", None)
    if callable(tolist):
        try:
            return to_jsonable(tolist())
        except (TypeError, ValueError, AttributeError) as e:
            logger.debug(f"tolist() conversion failed for {type(value)}: {e}")

    item = getattr(value, "item", None)
    if callable(item):
        try:
            return item()
        except (TypeError, ValueError, AttributeError) as e:
            logger.debug(f"item() conversion failed for {type(value)}: {e}")

    enum_value = getattr(value, "value", None)
    if isinstance(enum_value, (str, int, float, bool)):
        return enum_value

    return str(value)


class DFCIntegration:
    """DFC 集成管理器。

    负责与 DFC（对话流控制器）的交互，包括运行状态摘要生成与
    异步消息传递。长期经历和日记不在这里另建旁路；需要回忆时统一
    通过 LifeMemoryService 的可追溯检索与记忆边界读取链取得。
    """

    def __init__(self, service: "LifeEngineService") -> None:
        """初始化 DFC 集成管理器。

        Args:
            service: LifeEngineService 实例
        """
        self._service = service

    async def get_state_digest(self) -> str:
        """生成给 DFC 的状态摘要。

        设计原则：
        1. 控制在 150-200 tokens
        2. 只包含对当前对话有用的信息
        3. 使用简单模板，不调用 LLM
        4. 不会保存到历史消息中

        Returns:
            格式化的状态摘要文本
        """
        snapshot = await self.get_dfc_snapshot()
        return str(snapshot.get("state_digest") or "")

    async def get_dfc_snapshot(self) -> dict[str, Any]:
        """生成供 DFC 消费的结构化快照。

        这个快照是 DFC 的单一状态来源：
        - state_digest: 给 prompt / 状态查询用的简短摘要
        - active_todo_lines: 活跃 TODO 的短行摘要

        日记和长期记忆不复制到该瞬时快照，避免绕过统一记忆检索、
        来源版本和边界回读证明。
        """
        async with self._service._get_lock():
            state_digest = self._build_state_digest_locked()
            todo_lines = self._load_active_todo_lines()

        return {
            "generated_at": _now_iso(),
            "state_digest": state_digest,
            "active_todo_lines": todo_lines,
        }

    def _build_state_digest_locked(self) -> str:
        """在持锁前提下构建轻量状态摘要。"""
        parts = []

        # 最近思考（最近1-2条心跳独白）
        heartbeat_events = [
            e for e in self._service._event_history
            if is_life_heartbeat_event(e)
        ][-2:]

        if heartbeat_events:
            thoughts = []
            for event in heartbeat_events:
                time_display = _format_time_display(event.timestamp)
                thought = _shorten_text(event.content, max_length=40)
                thoughts.append(f"  [{time_display}] {thought}")
            if thoughts:
                parts.append("【最近思考】")
                parts.extend(thoughts)

        # 3. 工具使用偏好
        tool_events = [
            e for e in self._service._event_history[-30:]
            if e.event_type == EventType.TOOL_CALL
        ]

        if tool_events:
            tool_counts: dict[str, int] = {}
            for event in tool_events:
                name = event.tool_name
                if name and name.startswith("nucleus_"):
                    short_name = name.replace("nucleus_", "")
                    tool_counts[short_name] = tool_counts.get(short_name, 0) + 1

            if tool_counts:
                top_tools = sorted(tool_counts.items(), key=lambda x: x[1], reverse=True)[:2]
                tool_names = [name for name, _ in top_tools]
                parts.append(f"【工具偏好】{', '.join(tool_names)}")

        return "\n".join(parts) if parts else ""

    async def query_actor_context(self) -> str:
        """供 DFC 同步查询当前运行状态与 TODO。

        Returns:
            格式化的上下文摘要
        """
        snapshot = await self.get_dfc_snapshot()
        parts: list[str] = []

        state_digest = str(snapshot.get("state_digest") or "").strip()
        if state_digest:
            parts.append(state_digest)

        todo_lines = [str(line).strip() for line in snapshot.get("active_todo_lines") or [] if str(line).strip()]
        if todo_lines:
            parts.append("【活跃 TODO】\n" + "\n".join(todo_lines))

        return "\n\n".join(part for part in parts if part.strip())

    def _workspace_dir(self) -> Path:
        """返回工作空间目录。"""
        return self._service._workspace_dir()

    def _load_active_todo_lines(self, *, limit: int = 5) -> list[str]:
        """读取当前活跃 TODO 的简短摘要。"""
        from ..tools.todo_tools import TodoStatus, TodoStorage

        storage = TodoStorage(self._workspace_dir())
        inactive_statuses = {
            TodoStatus.COMPLETED.value,
            TodoStatus.CANCELLED.value,
            TodoStatus.ARCHIVED.value,
        }
        todos = [todo for todo in storage.load() if todo.status not in inactive_statuses]
        lines: list[str] = []
        for todo in todos[:limit]:
            next_action = str(getattr(todo, "next_action", "") or "").strip()
            suffix = f"；下一步：{next_action}" if next_action else ""
            lines.append(f"- {todo.title} ({todo.status}/{todo.priority}){suffix}")
        return lines

class MemoryIntegration:
    """记忆系统集成管理器。

    负责把记忆服务接入 Life Engine 拥有的唯一 coherent runtime。
    旧日衰减入口只保留为显式 no-op 兼容边界。
    """

    def __init__(self, service: "LifeEngineService") -> None:
        """初始化记忆集成管理器。

        Args:
            service: LifeEngineService 实例
        """
        self._service = service

    async def init_memory_service(self) -> None:
        """初始化可追溯生命记忆服务。"""
        try:
            from ..memory.service import LifeMemoryService

            cfg = self._service._cfg()
            workspace = Path(cfg.settings.workspace_path)
            index_config = getattr(cfg, "memory_index", None)
            storage_enabled = bool(self._service._selectable_storage_enabled)
            storage_runtime = self._service.storage_runtime if storage_enabled else None
            if storage_enabled and storage_runtime is None:
                raise RuntimeError(
                    "selectable Memory storage requires the Life Engine coherent runtime"
                )
            self._service._memory_service = LifeMemoryService(
                workspace,
                vector_backend_enabled=bool(
                    getattr(index_config, "backend_enabled", True)
                ),
                # The outbox consumer loop is gated on `memory_index.enabled`
                # (see service/core.py). Health must know whether the consumer
                # is expected to run, otherwise a disabled worker with a
                # growing backlog reports as a silent `ok`.
                index_worker_enabled=bool(getattr(index_config, "enabled", True)),
                storage_runtime=storage_runtime,
                selectable_storage_enabled=storage_enabled,
            )
            await self._service._memory_service.initialize()
            logger.info("life_engine 生命记忆服务已初始化")
        except Exception as e:
            logger.error(f"记忆服务初始化失败: {e}", exc_info=True)
            self._service._memory_service = None

    async def maybe_run_daily_decay(self) -> None:
        """Compatibility no-op for the retired score-driven decay loop.

        Recall history and explicit subject interpretation now drive living
        accessibility.  Infrastructure must not delete or weaken a relation
        because a score, age, access count, or emotion field crossed a limit.
        """

        return None
