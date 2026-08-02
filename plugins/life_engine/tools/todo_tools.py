"""life_engine 承诺行动 TODO 工具集。

TODO 不再是愿望清单，而是需要被推进、复盘、完成或放弃的承诺行动。
"""

from __future__ import annotations

import asyncio
import calendar
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Annotated, Any, Literal
from uuid import uuid4

from src.app.plugin_system.api import log_api
from src.app.plugin_system.base import BaseTool

from ..storage_utils import atomic_write_text
from ._utils import _get_workspace

logger = log_api.get_logger("life_engine.todos")

_TODO_FILE = "todos.json"
_TODO_VERSION = 2
_MAX_LIST_LIMIT = 25
_TODO_WRITE_LOCK: asyncio.Lock | None = None


def _get_todo_write_lock() -> asyncio.Lock:
    """Return the process-local lock for TODO read-modify-write transactions."""

    global _TODO_WRITE_LOCK
    if _TODO_WRITE_LOCK is None:
        _TODO_WRITE_LOCK = asyncio.Lock()
    return _TODO_WRITE_LOCK

TodoStatusLiteral = Literal[
    "pending",
    "in_progress",
    "blocked",
    "completed",
    "cancelled",
    "archived",
]
TodoPriorityLiteral = Literal["low", "normal", "high", "urgent"]
TodoVisibilityLiteral = Literal["private", "shared"]
TodoRecurrenceLiteral = Literal["none", "daily", "weekly", "monthly", "interval"]
TodoDetailLevel = Literal["summary", "full"]
TodoDueFilter = Literal["all", "overdue", "due", "none"]
TodoAction = Literal[
    "create",
    "update",
    "edit",
    "start",
    "log_progress",
    "complete",
    "cancel",
    "archive",
    "delete",
    "review",
]


class TodoStatus(str, Enum):
    """状态常量。

    保留部分旧状态名，避免依赖方在过渡期导入失败。
    """

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    ARCHIVED = "archived"
    RELEASED = "cancelled"
    CHERISHED = "completed"


def _now() -> datetime:
    return datetime.now(timezone.utc).astimezone()


def _now_iso() -> str:
    return _now().isoformat()


def _now_stamp() -> str:
    return _now().strftime("%Y%m%d%H%M%S")


def _normalize_text(value: Any) -> str:
    return str(value or "").strip()


def _parse_datetime(value: str | None) -> datetime | None:
    text = _normalize_text(value)
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.astimezone()
    else:
        dt = dt.astimezone()
    return dt


def _normalize_datetime(value: str | None) -> str | None:
    dt = _parse_datetime(value)
    return dt.isoformat() if dt else None


def _add_month(dt: datetime) -> datetime:
    month = dt.month + 1
    year = dt.year
    if month > 12:
        month = 1
        year += 1
    last_day = calendar.monthrange(year, month)[1]
    return dt.replace(year=year, month=month, day=min(dt.day, last_day))


def _normalize_recurrence(
    recurrence: str | dict[str, Any] | None,
    interval_seconds: float | None = None,
) -> dict[str, Any]:
    if isinstance(recurrence, dict):
        kind = _normalize_text(recurrence.get("kind") or "none").lower()
        interval_seconds = recurrence.get("interval_seconds", interval_seconds)
    else:
        kind = _normalize_text(recurrence or "none").lower()

    if kind not in {"none", "daily", "weekly", "monthly", "interval"}:
        kind = "none"

    payload: dict[str, Any] = {"kind": kind}
    if kind == "interval":
        try:
            interval = float(interval_seconds or 0)
        except (TypeError, ValueError):
            interval = 0
        if interval <= 0:
            kind = "none"
            payload = {"kind": "none"}
        else:
            payload["interval_seconds"] = interval
    return payload


def _next_from_recurrence(recurrence: dict[str, Any], start: datetime | None = None) -> datetime | None:
    base = start or _now()
    kind = _normalize_text(recurrence.get("kind") or "none")
    if kind == "daily":
        return base + timedelta(days=1)
    if kind == "weekly":
        return base + timedelta(days=7)
    if kind == "monthly":
        return _add_month(base)
    if kind == "interval":
        try:
            seconds = float(recurrence.get("interval_seconds") or 0)
        except (TypeError, ValueError):
            seconds = 0
        if seconds > 0:
            return base + timedelta(seconds=seconds)
    return None


def _is_inactive_status(status: str) -> bool:
    return status in {"completed", "cancelled", "archived"}


def _normalize_status(value: str | None, default: str = "pending") -> str:
    status = _normalize_text(value).lower() or default
    legacy_map = {
        "idea": "pending",
        "planning": "pending",
        "waiting": "pending",
        "enjoying": "in_progress",
        "paused": "blocked",
        "released": "cancelled",
        "cherished": "completed",
    }
    status = legacy_map.get(status, status)
    if status not in {"pending", "in_progress", "blocked", "completed", "cancelled", "archived"}:
        return default
    return status


def _normalize_priority(value: str | None) -> str:
    priority = _normalize_text(value).lower() or "normal"
    return priority if priority in {"low", "normal", "high", "urgent"} else "normal"


def _normalize_visibility(value: str | None) -> str:
    visibility = _normalize_text(value).lower() or "private"
    return visibility if visibility in {"private", "shared"} else "private"


def _generate_todo_id() -> str:
    return f"todo_{uuid4().hex[:8]}"


@dataclass
class LifeTodo:
    """一条需要被推进的承诺行动。"""

    id: str
    title: str
    description: str = ""
    status: str = "pending"
    priority: str = "normal"
    next_action: str = ""
    due_at: str | None = None
    remind_at: str | None = None
    next_review_at: str | None = None
    recurrence: dict[str, Any] = field(default_factory=lambda: {"kind": "none"})
    source: str = "life_engine"
    visibility: str = "private"
    related_stream_id: str = ""
    schedule_record_id: str = ""
    progress_log: list[dict[str, Any]] = field(default_factory=list)
    completion_log: list[dict[str, Any]] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""

    def __post_init__(self) -> None:
        self.status = _normalize_status(self.status)
        self.priority = _normalize_priority(self.priority)
        self.visibility = _normalize_visibility(self.visibility)
        self.recurrence = _normalize_recurrence(self.recurrence)
        if not self.created_at:
            self.created_at = _now_iso()
        if not self.updated_at:
            self.updated_at = _now_iso()

    @classmethod
    def from_dict(cls, item: dict[str, Any]) -> "LifeTodo":
        return cls(
            id=str(item.get("id") or _generate_todo_id()),
            title=str(item.get("title") or "").strip(),
            description=str(item.get("description") or ""),
            status=_normalize_status(str(item.get("status") or "")),
            priority=_normalize_priority(str(item.get("priority") or "")),
            next_action=str(item.get("next_action") or ""),
            due_at=_normalize_datetime(item.get("due_at") or item.get("deadline")),
            remind_at=_normalize_datetime(item.get("remind_at")),
            next_review_at=_normalize_datetime(item.get("next_review_at") or item.get("target_time")),
            recurrence=_normalize_recurrence(item.get("recurrence")),
            source=str(item.get("source") or "life_engine"),
            visibility=_normalize_visibility(str(item.get("visibility") or "")),
            related_stream_id=str(item.get("related_stream_id") or ""),
            schedule_record_id=str(item.get("schedule_record_id") or ""),
            progress_log=list(item.get("progress_log") or []),
            completion_log=list(item.get("completion_log") or []),
            created_at=str(item.get("created_at") or ""),
            updated_at=str(item.get("updated_at") or ""),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def due_datetime(self) -> datetime | None:
        return _parse_datetime(self.due_at)

    def review_datetime(self) -> datetime | None:
        return _parse_datetime(self.next_review_at)

    def remind_datetime(self) -> datetime | None:
        return _parse_datetime(self.remind_at)

    def days_until_due(self) -> int | None:
        due = self.due_datetime()
        if due is None:
            return None
        return (due.date() - _now().date()).days

    def is_overdue(self) -> bool:
        due = self.due_datetime()
        return due is not None and due < _now() and not _is_inactive_status(self.status)

    def needs_review(self) -> bool:
        if _is_inactive_status(self.status):
            return False
        review_at = self.review_datetime()
        return review_at is not None and review_at <= _now()

    def is_recurring(self) -> bool:
        return _normalize_text(self.recurrence.get("kind") or "none") != "none"


class TodoStorage:
    """TODO v2 持久化存储。"""

    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.file_path = workspace / _TODO_FILE

    def _archive_legacy_list(self, items: list[Any]) -> None:
        archive_path = self.workspace / f"todos_legacy_{_now_stamp()}.json"
        suffix = 1
        while archive_path.exists():
            archive_path = self.workspace / f"todos_legacy_{_now_stamp()}_{suffix}.json"
            suffix += 1
        atomic_write_text(
            archive_path,
            json.dumps(items, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self.save([])
        logger.info(f"已归档旧 TODO 数据: {archive_path}")

    def load(self) -> list[LifeTodo]:
        if not self.file_path.exists():
            self.save([])
            return []

        try:
            raw = json.loads(self.file_path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            logger.error(f"加载 TODO 失败: {exc}", exc_info=True)
            return []

        if isinstance(raw, list):
            self._archive_legacy_list(raw)
            return []

        if not isinstance(raw, dict):
            return []
        if int(raw.get("version") or 0) != _TODO_VERSION:
            return []

        tasks_raw = raw.get("tasks")
        if not isinstance(tasks_raw, list):
            return []

        todos: list[LifeTodo] = []
        for item in tasks_raw:
            if not isinstance(item, dict):
                continue
            try:
                todos.append(LifeTodo.from_dict(item))
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"解析 TODO 失败: {exc}")
        return todos

    def save(self, todos: list[LifeTodo]) -> None:
        payload = {
            "version": _TODO_VERSION,
            "updated_at": _now_iso(),
            "tasks": [todo.to_dict() for todo in todos],
        }
        atomic_write_text(
            self.file_path,
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def get(self, todo_id: str) -> LifeTodo | None:
        ref = _normalize_text(todo_id)
        for todo in self.load():
            if todo.id == ref or todo.id.startswith(ref):
                return todo
        return None

    def upsert(self, todo: LifeTodo) -> None:
        todos = self.load()
        replaced = False
        for index, item in enumerate(todos):
            if item.id == todo.id:
                todos[index] = todo
                replaced = True
                break
        if not replaced:
            todos.append(todo)
        self.save(todos)

    def delete(self, todo_id: str) -> LifeTodo | None:
        ref = _normalize_text(todo_id)
        todos = self.load()
        kept: list[LifeTodo] = []
        removed: LifeTodo | None = None
        for todo in todos:
            if todo.id == ref or todo.id.startswith(ref):
                removed = todo
                continue
            kept.append(todo)
        if removed is not None:
            self.save(kept)
        return removed


def _get_storage(plugin: Any) -> TodoStorage:
    return TodoStorage(_get_workspace(plugin))


def _normalize_todo_limit(limit: int | None) -> int:
    if limit is None:
        return 10
    try:
        value = int(limit)
    except (TypeError, ValueError):
        return 10
    return max(1, min(value, _MAX_LIST_LIMIT))


def _default_remind_at(todo: LifeTodo) -> str | None:
    if todo.remind_at:
        return todo.remind_at
    due = todo.due_datetime()
    if due is not None:
        return due.isoformat()
    next_time = _next_from_recurrence(todo.recurrence)
    return next_time.isoformat() if next_time else None


def _append_log(todo: LifeTodo, kind: str, note: str, extra: dict[str, Any] | None = None) -> None:
    entry = {
        "at": _now_iso(),
        "kind": kind,
        "note": _normalize_text(note),
    }
    if extra:
        entry.update(extra)
    todo.progress_log.append(entry)


def _todo_summary(todo: LifeTodo) -> dict[str, Any]:
    return {
        "id": todo.id,
        "title": todo.title,
        "status": todo.status,
        "priority": todo.priority,
        "next_action": todo.next_action,
        "due_at": todo.due_at,
        "remind_at": todo.remind_at,
        "next_review_at": todo.next_review_at,
        "days_left": todo.days_until_due(),
        "overdue": todo.is_overdue(),
        "needs_review": todo.needs_review(),
        "recurrence": todo.recurrence,
        "visibility": todo.visibility,
        "source": todo.source,
        "schedule_record_id": todo.schedule_record_id,
        "progress_count": len(todo.progress_log),
        "completion_count": len(todo.completion_log),
        "has_description": bool(todo.description),
    }


def _build_reminder_message(todo: LifeTodo) -> str:
    return (
        "TODO 提醒机会\n"
        f"- todo_id: {todo.id}\n"
        f"- 标题: {todo.title}\n"
        f"- 下一步: {todo.next_action or '未填写'}\n"
        f"- 状态: {todo.status}\n"
        f"- 可见性: {todo.visibility}\n"
        "这是一次主动机会。请先判断现在是否适合推进、复盘或向用户自然提起，"
        "不要把提醒当成必须立刻发送的消息。"
    )


async def _delete_todo_schedule(plugin: Any, todo: LifeTodo) -> str | None:
    if not todo.schedule_record_id:
        return None
    try:
        from .schedule_tools import LifeEngineManageScheduleTool

        tool = LifeEngineManageScheduleTool(plugin=plugin)
        ok, result = await tool.execute(action="delete", task_ref=todo.schedule_record_id)
        todo.schedule_record_id = ""
        if not ok:
            return str(result)
        return None
    except Exception as exc:  # noqa: BLE001
        todo.schedule_record_id = ""
        return str(exc)


async def _sync_todo_schedule(plugin: Any, todo: LifeTodo) -> str | None:
    warning = await _delete_todo_schedule(plugin, todo)
    if _is_inactive_status(todo.status):
        return warning

    todo.remind_at = _default_remind_at(todo)
    if not todo.remind_at:
        return warning

    try:
        from .schedule_tools import LifeEngineManageScheduleTool

        tool = LifeEngineManageScheduleTool(plugin=plugin)
        ok, result = await tool.execute(
            action="create",
            title=f"TODO 提醒: {todo.title}",
            kind="message",
            trigger_mode="at",
            trigger_at=todo.remind_at,
            message=_build_reminder_message(todo),
            notes=f"todo_id={todo.id}",
            replace_existing=True,
        )
        if ok and isinstance(result, dict):
            todo.schedule_record_id = str(result.get("record_id") or "")
            return warning
        todo.schedule_record_id = ""
        return str(result)
    except Exception as exc:  # noqa: BLE001
        todo.schedule_record_id = ""
        return str(exc)


def _validate_create_contract(
    *,
    title: str,
    next_action: str,
    due_at: str | None,
    remind_at: str | None,
    next_review_at: str | None,
    recurrence: dict[str, Any],
) -> str | None:
    if not _normalize_text(title):
        return "创建 TODO 需要 title"
    if not _normalize_text(next_action):
        return "创建 TODO 需要 next_action，必须说明下一步怎么推进"
    has_time_anchor = bool(due_at or remind_at or next_review_at)
    if not has_time_anchor and _normalize_text(recurrence.get("kind") or "none") == "none":
        return "创建 TODO 需要 due_at/remind_at/next_review_at/recurrence 至少一项，避免只添加不复盘"
    return None


def _apply_common_updates(todo: LifeTodo, updates: dict[str, Any]) -> list[str]:
    changed: list[str] = []
    for field_name, value in updates.items():
        if value is None:
            continue
        if field_name == "status":
            value = _normalize_status(str(value), default=todo.status)
        elif field_name == "priority":
            value = _normalize_priority(str(value))
        elif field_name == "visibility":
            value = _normalize_visibility(str(value))
        elif field_name in {"due_at", "remind_at", "next_review_at"}:
            value = _normalize_datetime(value)
        elif field_name == "recurrence":
            value = _normalize_recurrence(value, updates.get("recurrence_interval_seconds"))
        elif field_name == "recurrence_interval_seconds":
            continue
        elif field_name in {"title", "description", "next_action", "source", "related_stream_id"}:
            value = str(value or "").strip() if field_name != "description" else str(value or "")
            if value == "":
                continue
        if getattr(todo, field_name, None) != value and hasattr(todo, field_name):
            setattr(todo, field_name, value)
            changed.append(field_name)
    if changed:
        todo.updated_at = _now_iso()
    return changed


class LifeEngineManageTodoTool(BaseTool):
    """管理承诺行动 TODO。"""

    tool_name: str = "nucleus_manage_todo"
    tool_description: str = (
        "管理承诺行动 TODO：创建、推进、记录进展、完成、取消、归档或删除。"
        "TODO 是需要被行动和复盘的承诺，不是随手记愿望。"
        "\n\n"
        "创建规则：必须提供 title 和 next_action，并且 due_at/remind_at/next_review_at/recurrence 至少一项。"
        "如果 visibility=shared，表示这件事涉及用户或对用户的承诺，创建后需要自然告知用户。"
        "\n\n"
        "周期任务：recurrence 可选 none/daily/weekly/monthly/interval。"
        "complete 周期任务时会记录一次完成，并自动排下一次提醒。"
    )
    chatter_allow: list[str] = ["life_engine_internal", "life_chatter"]

    async def execute(
        self,
        action: Annotated[TodoAction, "操作：create/update/edit/start/log_progress/complete/cancel/archive/delete/review"],
        todo_id: Annotated[str, "TODO ID（除 create 外通常必填）"] = "",
        title: Annotated[str, "标题"] = "",
        description: Annotated[str, "详细说明"] = "",
        status: Annotated[TodoStatusLiteral | None, "状态"] = None,
        priority: Annotated[TodoPriorityLiteral | None, "优先级：low/normal/high/urgent"] = None,
        next_action: Annotated[str, "下一步具体行动"] = "",
        due_at: Annotated[str | None, "截止时间，ISO 或 YYYY-MM-DD"] = None,
        remind_at: Annotated[str | None, "提醒时间，ISO 或 YYYY-MM-DD"] = None,
        next_review_at: Annotated[str | None, "下次复盘时间，ISO 或 YYYY-MM-DD"] = None,
        recurrence: Annotated[TodoRecurrenceLiteral | None, "周期：none/daily/weekly/monthly/interval"] = None,
        recurrence_interval_seconds: Annotated[float | None, "recurrence=interval 时的周期秒数"] = None,
        visibility: Annotated[TodoVisibilityLiteral | None, "可见性：private/shared"] = None,
        source: Annotated[str, "来源：life_engine/life_chatter/user 等"] = "",
        related_stream_id: Annotated[str, "相关聊天流 ID"] = "",
        note: Annotated[str, "进展、复盘、完成或取消说明"] = "",
    ) -> tuple[bool, str | dict]:
        async with _get_todo_write_lock():
            return await self._execute_locked(
                action=action,
                todo_id=todo_id,
                title=title,
                description=description,
                status=status,
                priority=priority,
                next_action=next_action,
                due_at=due_at,
                remind_at=remind_at,
                next_review_at=next_review_at,
                recurrence=recurrence,
                recurrence_interval_seconds=recurrence_interval_seconds,
                visibility=visibility,
                source=source,
                related_stream_id=related_stream_id,
                note=note,
            )

    async def _execute_locked(
        self,
        action: Annotated[TodoAction, "操作：create/update/edit/start/log_progress/complete/cancel/archive/delete/review"],
        todo_id: Annotated[str, "TODO ID（除 create 外通常必填）"] = "",
        title: Annotated[str, "标题"] = "",
        description: Annotated[str, "详细说明"] = "",
        status: Annotated[TodoStatusLiteral | None, "状态"] = None,
        priority: Annotated[TodoPriorityLiteral | None, "优先级：low/normal/high/urgent"] = None,
        next_action: Annotated[str, "下一步具体行动"] = "",
        due_at: Annotated[str | None, "截止时间，ISO 或 YYYY-MM-DD"] = None,
        remind_at: Annotated[str | None, "提醒时间，ISO 或 YYYY-MM-DD"] = None,
        next_review_at: Annotated[str | None, "下次复盘时间，ISO 或 YYYY-MM-DD"] = None,
        recurrence: Annotated[TodoRecurrenceLiteral | None, "周期：none/daily/weekly/monthly/interval"] = None,
        recurrence_interval_seconds: Annotated[float | None, "recurrence=interval 时的周期秒数"] = None,
        visibility: Annotated[TodoVisibilityLiteral | None, "可见性：private/shared"] = None,
        source: Annotated[str, "来源：life_engine/life_chatter/user 等"] = "",
        related_stream_id: Annotated[str, "相关聊天流 ID"] = "",
        note: Annotated[str, "进展、复盘、完成或取消说明"] = "",
    ) -> tuple[bool, str | dict]:
        try:
            storage = _get_storage(self.plugin)
            action_value = _normalize_text(action).lower()
            if action_value == "edit":
                action_value = "update"

            if action_value == "create":
                recurrence_payload = _normalize_recurrence(recurrence, recurrence_interval_seconds)
                normalized_due = _normalize_datetime(due_at)
                normalized_remind = _normalize_datetime(remind_at)
                normalized_review = _normalize_datetime(next_review_at)
                error = _validate_create_contract(
                    title=title,
                    next_action=next_action,
                    due_at=normalized_due,
                    remind_at=normalized_remind,
                    next_review_at=normalized_review,
                    recurrence=recurrence_payload,
                )
                if error:
                    return False, error

                todo = LifeTodo(
                    id=_generate_todo_id(),
                    title=title.strip(),
                    description=description,
                    status="pending",
                    priority=_normalize_priority(priority),
                    next_action=next_action.strip(),
                    due_at=normalized_due,
                    remind_at=normalized_remind,
                    next_review_at=normalized_review,
                    recurrence=recurrence_payload,
                    source=_normalize_text(source) or "life_engine",
                    visibility=_normalize_visibility(visibility),
                    related_stream_id=_normalize_text(related_stream_id),
                )
                if todo.next_review_at is None:
                    todo.next_review_at = todo.remind_at or todo.due_at
                if note:
                    _append_log(todo, "create", note)

                warning = await _sync_todo_schedule(self.plugin, todo)
                storage.upsert(todo)
                return True, {
                    "action": "create_todo",
                    "todo": todo.to_dict(),
                    "requires_user_disclosure": todo.visibility == "shared",
                    "reminder_warning": warning,
                    "message": f"已创建承诺行动: {todo.title}",
                }

            todo = storage.get(todo_id)
            if todo is None:
                return False, f"找不到 TODO: {todo_id}"

            warning: str | None = None
            if action_value == "update":
                changes = _apply_common_updates(
                    todo,
                    {
                        "title": title,
                        "description": description,
                        "status": status,
                        "priority": priority,
                        "next_action": next_action,
                        "due_at": due_at,
                        "remind_at": remind_at,
                        "next_review_at": next_review_at,
                        "recurrence": recurrence,
                        "recurrence_interval_seconds": recurrence_interval_seconds,
                        "visibility": visibility,
                        "source": source,
                        "related_stream_id": related_stream_id,
                    },
                )
                if note:
                    _append_log(todo, "update", note, {"changes": changes})
                    changes.append("progress_log")
                if not changes:
                    return False, "没有提供任何要修改的字段"
                warning = await _sync_todo_schedule(self.plugin, todo)
                storage.upsert(todo)
                return True, {
                    "action": "update_todo",
                    "todo": todo.to_dict(),
                    "changes": changes,
                    "reminder_warning": warning,
                }

            if action_value == "start":
                todo.status = "in_progress"
                if next_action.strip():
                    todo.next_action = next_action.strip()
                _append_log(todo, "start", note or "开始推进")
                todo.updated_at = _now_iso()

            elif action_value == "log_progress":
                if not _normalize_text(note):
                    return False, "记录进展需要 note"
                if next_action.strip():
                    todo.next_action = next_action.strip()
                _append_log(todo, "progress", note)
                todo.updated_at = _now_iso()

            elif action_value == "review":
                if next_action.strip():
                    todo.next_action = next_action.strip()
                if status is not None:
                    todo.status = _normalize_status(status, default=todo.status)
                if remind_at is not None:
                    todo.remind_at = _normalize_datetime(remind_at)
                if next_review_at is not None:
                    todo.next_review_at = _normalize_datetime(next_review_at)
                _append_log(todo, "review", note or "已复盘")
                todo.updated_at = _now_iso()

            elif action_value == "complete":
                completion = {
                    "at": _now_iso(),
                    "note": _normalize_text(note) or "完成一次",
                    "next_action": todo.next_action,
                }
                todo.completion_log.append(completion)
                if todo.is_recurring():
                    next_time = _next_from_recurrence(todo.recurrence)
                    todo.status = "pending"
                    todo.remind_at = next_time.isoformat() if next_time else None
                    todo.next_review_at = todo.remind_at
                    if next_action.strip():
                        todo.next_action = next_action.strip()
                else:
                    todo.status = "completed"
                    todo.remind_at = None
                    todo.next_review_at = None
                todo.updated_at = _now_iso()

            elif action_value == "cancel":
                todo.status = "cancelled"
                todo.remind_at = None
                todo.next_review_at = None
                _append_log(todo, "cancel", note or "取消 TODO")
                todo.updated_at = _now_iso()

            elif action_value == "archive":
                todo.status = "archived"
                todo.remind_at = None
                todo.next_review_at = None
                _append_log(todo, "archive", note or "归档 TODO")
                todo.updated_at = _now_iso()

            elif action_value == "delete":
                warning = await _delete_todo_schedule(self.plugin, todo)
                removed = storage.delete(todo.id)
                if removed is None:
                    return False, f"找不到 TODO: {todo_id}"
                return True, {
                    "action": "delete_todo",
                    "deleted_id": removed.id,
                    "reminder_warning": warning,
                }

            else:
                return False, f"未知 action: {action}"

            warning = await _sync_todo_schedule(self.plugin, todo)
            storage.upsert(todo)
            return True, {
                "action": f"{action_value}_todo",
                "todo": todo.to_dict(),
                "reminder_warning": warning,
            }

        except Exception as exc:  # noqa: BLE001
            logger.error(f"TODO 管理失败: {exc}", exc_info=True)
            return False, f"操作失败: {exc}"


class LifeEngineListTodosTool(BaseTool):
    """列出 TODO 或查询单条详情。"""

    tool_name: str = "nucleus_list_todos"
    tool_description: str = (
        "查看承诺行动 TODO。默认只返回未完成、未取消、未归档的关注项。"
        "可按状态、优先级、逾期、需要复盘、周期任务和可见性筛选。"
    )
    chatter_allow: list[str] = ["life_engine_internal", "life_chatter"]

    async def execute(
        self,
        todo_id: Annotated[str, "TODO ID（填写则返回单条详情）"] = "",
        status: Annotated[TodoStatusLiteral | None, "筛选状态"] = None,
        priority: Annotated[TodoPriorityLiteral | None, "筛选优先级"] = None,
        due: Annotated[TodoDueFilter, "due 过滤：all/overdue/due/none"] = "all",
        needs_review: Annotated[bool | None, "是否只看需要复盘的 TODO"] = None,
        recurring: Annotated[bool | None, "是否只看周期任务"] = None,
        visibility: Annotated[TodoVisibilityLiteral | None, "筛选 private/shared"] = None,
        include_completed: Annotated[bool, "是否包含 completed/cancelled/archived"] = False,
        limit: Annotated[int, "最多返回多少条（默认 10，最大 25）"] = 10,
        detail_level: Annotated[TodoDetailLevel, "summary/full"] = "summary",
    ) -> tuple[bool, str | dict]:
        try:
            storage = _get_storage(self.plugin)

            if todo_id.strip():
                todo = storage.get(todo_id)
                if todo is None:
                    return False, f"找不到 TODO: {todo_id}"
                return True, {"action": "get_todo", "todo": todo.to_dict()}

            todos = storage.load()
            filtered: list[LifeTodo] = []
            for todo in todos:
                if status is not None and todo.status != _normalize_status(status):
                    continue
                if priority is not None and todo.priority != _normalize_priority(priority):
                    continue
                if visibility is not None and todo.visibility != _normalize_visibility(visibility):
                    continue
                if not include_completed and status is None and _is_inactive_status(todo.status):
                    continue
                if needs_review is not None and todo.needs_review() is not bool(needs_review):
                    continue
                if recurring is not None and todo.is_recurring() is not bool(recurring):
                    continue

                due_filter = _normalize_text(due).lower() or "all"
                if due_filter == "overdue" and not todo.is_overdue():
                    continue
                if due_filter == "due" and todo.due_datetime() is None:
                    continue
                if due_filter == "none" and todo.due_datetime() is not None:
                    continue
                filtered.append(todo)

            priority_order = {"urgent": 0, "high": 1, "normal": 2, "low": 3}

            def sort_key(todo: LifeTodo) -> tuple[int, int, str]:
                attention = 0
                if todo.is_overdue():
                    attention = -3
                elif todo.needs_review():
                    attention = -2
                elif todo.remind_datetime() is not None:
                    attention = -1
                time_anchor = (
                    todo.due_at
                    or todo.next_review_at
                    or todo.remind_at
                    or todo.updated_at
                )
                return (attention, priority_order.get(todo.priority, 2), str(time_anchor or ""))

            filtered.sort(key=sort_key)
            normalized_limit = _normalize_todo_limit(limit)
            returned = filtered[:normalized_limit]
            payload_items = [todo.to_dict() for todo in returned] if detail_level == "full" else [_todo_summary(todo) for todo in returned]

            active_todos = [todo for todo in todos if not _is_inactive_status(todo.status)]
            overdue_count = sum(1 for todo in active_todos if todo.is_overdue())
            review_count = sum(1 for todo in active_todos if todo.needs_review())
            recurring_count = sum(1 for todo in active_todos if todo.is_recurring())

            return True, {
                "action": "list_todos",
                "todos": payload_items,
                "total": len(filtered),
                "returned": len(returned),
                "truncated": len(filtered) > len(returned),
                "limit": normalized_limit,
                "detail_level": detail_level,
                "all_count": len(todos),
                "active_count": len(active_todos),
                "overdue_count": overdue_count,
                "needs_review_count": review_count,
                "recurring_count": recurring_count,
                "detail_hint": "列表默认只给摘要；需要完整内容请传 todo_id 查看单条详情。",
                "filters_applied": {
                    "status": status,
                    "priority": priority,
                    "due": due,
                    "needs_review": needs_review,
                    "recurring": recurring,
                    "visibility": visibility,
                    "include_completed": include_completed,
                    "limit": normalized_limit,
                    "detail_level": detail_level,
                },
            }
        except Exception as exc:  # noqa: BLE001
            logger.error(f"列出 TODO 失败: {exc}", exc_info=True)
            return False, f"列出失败: {exc}"


class NucleusTodoTool(BaseTool):
    """统一的 TODO 管理工具（合并原 nucleus_manage_todo + nucleus_list_todos）。"""

    tool_name: str = "nucleus_todo"
    tool_description: str = (
        "管理承诺行动 TODO。\n\n"
        "action=list：查看 TODO 列表（默认只返回未完成项，可按状态/优先级/逾期筛选）\n"
        "action=create：创建新 TODO（必须提供 title 和 next_action，并且 due_at/remind_at/next_review_at/recurrence 至少一项）\n"
        "action=update/edit：修改 TODO 字段\n"
        "action=start：开始执行\n"
        "action=log_progress：记录进展\n"
        "action=complete：完成（周期任务会自动排下一次）\n"
        "action=cancel/archive/delete：取消/归档/删除\n"
        "action=review：复盘\n\n"
        "TODO 是需要被行动和复盘的承诺，不是随手记愿望。"
    )
    chatter_allow: list[str] = ["life_engine_internal", "life_chatter"]

    async def execute(self, action: Annotated[str, "操作：list/create/update/edit/start/log_progress/complete/cancel/archive/delete/review"] = "list", **kwargs: object) -> tuple[bool, str | dict]:
        action_value = str(action or "list").strip().lower()

        if action_value in ("list", "get", "query", "show"):
            # 委托给 list 逻辑
            tool = LifeEngineListTodosTool(plugin=self.plugin)
            return await tool.execute(**kwargs)  # type: ignore[arg-type]
        else:
            # 委托给 manage 逻辑
            tool = LifeEngineManageTodoTool(plugin=self.plugin)
            return await tool.execute(action=action_value, **kwargs)  # type: ignore[arg-type]


TODO_TOOLS = [
    NucleusTodoTool,
]
