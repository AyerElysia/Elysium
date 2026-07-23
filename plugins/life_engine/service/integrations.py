"""life_engine 子系统集成模块。

包含 DFC 集成、记忆集成的初始化与管理。
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from src.app.plugin_system.api.log_api import get_logger

from .event_builder import (
    EventType,
    LifeEngineEvent,
    _format_time_display,
    _now_iso,
    _shorten_text,
    INTERNAL_PLATFORM,
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

    负责与 DFC（对话流控制器）的交互，包括状态摘要生成、
    梦记录注入、异步消息传递等。
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
        - recent_diary_lines: 最近日记的短行摘要
        """
        async with self._service._get_lock():
            state_digest = self._build_state_digest_locked()
            todo_lines = self._load_active_todo_lines()
            diary_lines = self._load_recent_diary_lines()

        return {
            "generated_at": _now_iso(),
            "state_digest": state_digest,
            "active_todo_lines": todo_lines,
            "recent_diary_lines": diary_lines,
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

    async def pick_latest_external_stream_id(self) -> str:
        """选择最近的外部对话流作为注入目标。

        Returns:
            最近的 stream_id，如果无可用则返回空字符串
        """
        async with self._service._get_lock():
            candidates = list(self._service._pending_events) + list(self._service._event_history)

        for event in reversed(candidates):
            if event.event_type != EventType.MESSAGE:
                continue
            stream_id = str(event.stream_id or "").strip()
            if not stream_id:
                continue
            source = str(event.source or "").strip()
            if source == INTERNAL_PLATFORM:
                continue
            return stream_id
        return ""

    async def query_actor_context(self) -> str:
        """供 DFC 同步查询当前状态、TODO 与最近日记。

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

        diary_lines = [str(line).strip() for line in snapshot.get("recent_diary_lines") or [] if str(line).strip()]
        if diary_lines:
            parts.append("【最近日记】\n" + "\n".join(diary_lines))

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

    def _load_recent_diary_lines(self, *, limit: int = 2) -> list[str]:
        """读取最近几篇日记的预览。"""
        diary_dir = self._workspace_dir() / "diary"
        if not diary_dir.exists():
            return []

        lines: list[str] = []
        for diary_file in sorted(diary_dir.glob("*.md"), reverse=True)[:limit]:
            try:
                content = " ".join(diary_file.read_text(encoding="utf-8").split())
            except Exception:
                continue
            if not content:
                continue
            preview = _shorten_text(content, max_length=120)
            lines.append(f"- {diary_file.stem}: {preview}")
        return lines


class MemoryIntegration:
    """记忆系统集成管理器。

    负责记忆服务的初始化与日常衰减任务。
    """

    def __init__(self, service: "LifeEngineService") -> None:
        """初始化记忆集成管理器。

        Args:
            service: LifeEngineService 实例
        """
        self._service = service
        self._last_decay_date: str | None = None

    async def init_memory_service(self) -> None:
        """初始化仿生记忆服务。"""
        try:
            from ..memory.service import LifeMemoryService

            cfg = self._service._cfg()
            workspace = Path(cfg.settings.workspace_path)
            self._service._memory_service = LifeMemoryService(workspace)
            await self._service._memory_service.initialize()
            logger.info("life_engine 仿生记忆服务已初始化")
        except Exception as e:
            logger.error(f"记忆服务初始化失败: {e}", exc_info=True)
            self._service._memory_service = None

    async def maybe_run_daily_decay(self) -> None:
        """每日运行一次记忆衰减任务。"""
        if not self._service._memory_service:
            return

        today = datetime.now().strftime("%Y-%m-%d")
        if self._last_decay_date == today:
            return

        try:
            update_count = await self._service._memory_service.apply_decay()
            self._last_decay_date = today
            if update_count > 0:
                logger.info(
                    f"life_engine 记忆衰减完成: 更新节点={update_count}"
                )
        except Exception as e:
            logger.error(f"记忆衰减任务失败: {e}", exc_info=True)
