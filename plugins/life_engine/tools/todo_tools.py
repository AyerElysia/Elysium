"""Durable TODO board with a coding-agent write contract.

The board is persistent because she is a continuous being, not a one-shot
session. The write shape follows Codex ``update_plan`` / Claude ``TodoWrite``:
a list of ``{id, content, status}`` plus ``merge``. Status is the only
workflow dimension. Infrastructure must not rank, urge, or auto-advance items.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Annotated, Any, ClassVar, Literal
from uuid import uuid4

from src.app.plugin_system.api import log_api
from src.app.plugin_system.base import BaseTool

from ..storage_utils import atomic_write_text
from ._utils import _get_workspace

logger = log_api.get_logger("life_engine.todos")

_TODO_FILE = "todos.json"
_TODO_VERSION = 3
_MAX_LIST_LIMIT = 40
_MAX_OPEN_ITEMS = 40
_MAX_CONTENT_BYTES = 400
_BOARD_MAX_BYTES = 1024
_TODO_WRITE_LOCK: asyncio.Lock | None = None

TodoStatusLiteral = Literal["pending", "in_progress", "completed", "cancelled"]
TodoAction = Literal["write", "list", "get"]

STATUS_ORDER: dict[str, int] = {
    "in_progress": 0,
    "pending": 1,
    "completed": 2,
    "cancelled": 3,
}
OPEN_STATUSES = frozenset({"pending", "in_progress"})
CLOSED_STATUSES = frozenset({"completed", "cancelled"})


class TodoStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


def _now() -> datetime:
    return datetime.now(UTC).astimezone()


def _now_iso() -> str:
    return _now().isoformat()


def _now_stamp() -> str:
    return _now().strftime("%Y%m%d%H%M%S")


def _normalize_text(value: Any) -> str:
    return str(value or "").strip()


def _clip_utf8(text: str, max_bytes: int) -> str:
    if max_bytes <= 0:
        return ""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    clipped = encoded[:max_bytes]
    while clipped:
        try:
            return clipped.decode("utf-8")
        except UnicodeDecodeError:
            clipped = clipped[:-1]
    return ""


def _utf8_size(text: str) -> int:
    return len(text.encode("utf-8"))


def _parse_datetime(value: str | None) -> datetime | None:
    text = _normalize_text(value)
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.astimezone()
    return parsed.astimezone()


def _normalize_datetime(value: str | None) -> str | None:
    parsed = _parse_datetime(value)
    return parsed.isoformat() if parsed else None


def _normalize_status(value: str | None, default: str = "pending") -> str:
    status = _normalize_text(value).lower() or default
    legacy = {
        "idea": "pending",
        "planning": "pending",
        "waiting": "pending",
        "blocked": "pending",
        "paused": "pending",
        "enjoying": "in_progress",
        "archived": "cancelled",
        "released": "cancelled",
        "cherished": "completed",
        "done": "completed",
    }
    status = legacy.get(status, status)
    if status not in STATUS_ORDER:
        return default
    return status


def _normalize_visibility(value: str | None) -> str:
    visibility = _normalize_text(value).lower() or "private"
    return visibility if visibility in {"private", "shared"} else "private"


def _generate_todo_id() -> str:
    return f"todo_{uuid4().hex[:8]}"


def _get_todo_write_lock() -> asyncio.Lock:
    global _TODO_WRITE_LOCK
    if _TODO_WRITE_LOCK is None:
        _TODO_WRITE_LOCK = asyncio.Lock()
    return _TODO_WRITE_LOCK


def _content_from_legacy(item: dict[str, Any]) -> str:
    content = _normalize_text(item.get("content"))
    if content:
        return _clip_utf8(content, _MAX_CONTENT_BYTES)
    title = _normalize_text(item.get("title"))
    next_action = _normalize_text(item.get("next_action"))
    description = _normalize_text(item.get("description"))
    if title and next_action and next_action not in title:
        content = f"{title} — {next_action}"
    else:
        content = title or next_action or description
    return _clip_utf8(content, _MAX_CONTENT_BYTES)


def _history_from_legacy(item: dict[str, Any]) -> list[dict[str, Any]]:
    history: list[dict[str, Any]] = []
    for raw in list(item.get("history") or []):
        if isinstance(raw, dict):
            history.append(dict(raw))
    for raw in list(item.get("progress_log") or []):
        if isinstance(raw, dict):
            history.append(
                {
                    "at": str(raw.get("at") or ""),
                    "kind": str(raw.get("kind") or "progress"),
                    "note": _normalize_text(raw.get("note")),
                }
            )
    for raw in list(item.get("completion_log") or []):
        if isinstance(raw, dict):
            history.append(
                {
                    "at": str(raw.get("at") or ""),
                    "kind": "complete",
                    "note": _normalize_text(raw.get("note")),
                }
            )
    return history


@dataclass
class LifeTodo:
    """One board item. Ignore is not rejection; status changes are hers."""

    id: str
    content: str
    status: str = "pending"
    due_at: str | None = None
    source: str = "life_engine"
    visibility: str = "private"
    created_at: str = ""
    updated_at: str = ""
    history: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.id = _normalize_text(self.id) or _generate_todo_id()
        self.content = _clip_utf8(_normalize_text(self.content), _MAX_CONTENT_BYTES)
        self.status = _normalize_status(self.status)
        self.due_at = _normalize_datetime(self.due_at)
        self.source = _normalize_text(self.source) or "life_engine"
        self.visibility = _normalize_visibility(self.visibility)
        if not self.created_at:
            self.created_at = _now_iso()
        if not self.updated_at:
            self.updated_at = self.created_at

    @classmethod
    def from_dict(cls, item: dict[str, Any]) -> LifeTodo:
        return cls(
            id=str(item.get("id") or _generate_todo_id()),
            content=_content_from_legacy(item),
            status=_normalize_status(str(item.get("status") or "")),
            due_at=_normalize_datetime(item.get("due_at") or item.get("deadline")),
            source=str(item.get("source") or "life_engine"),
            visibility=_normalize_visibility(str(item.get("visibility") or "")),
            created_at=str(item.get("created_at") or ""),
            updated_at=str(item.get("updated_at") or ""),
            history=_history_from_legacy(item),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "content": self.content,
            "status": self.status,
            "due_at": self.due_at,
            "visibility": self.visibility,
            "updated_at": self.updated_at,
        }

    def sort_key(self) -> tuple[int, str]:
        return (STATUS_ORDER.get(self.status, 9), self.id)

    def is_open(self) -> bool:
        return self.status in OPEN_STATUSES

    def append_history(self, kind: str, note: str = "", extra: dict[str, Any] | None = None) -> None:
        entry: dict[str, Any] = {
            "at": _now_iso(),
            "kind": kind,
            "note": _normalize_text(note),
        }
        if extra:
            entry.update(extra)
        self.history.append(entry)
        self.updated_at = entry["at"]


def _sorted_todos(todos: list[LifeTodo]) -> list[LifeTodo]:
    return sorted(todos, key=lambda item: item.sort_key())


class TodoStorage:
    """TODO v3 durable board. Rebuildable JSON; not a Life Event ledger."""

    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.file_path = workspace / _TODO_FILE
        self.legacy_schedule_ids: list[str] = []

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
        logger.info(f"archived legacy TODO list: path={archive_path.name}")

    def load(self, *, persist_migration: bool = True) -> list[LifeTodo]:
        if not self.file_path.exists():
            self.save([])
            return []
        try:
            raw = json.loads(self.file_path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            logger.error(f"load TODO board failed: error_type={type(exc).__name__}")
            return []
        if isinstance(raw, list):
            self._archive_legacy_list(raw)
            return []
        if not isinstance(raw, dict):
            return []
        version = int(raw.get("version") or 0)
        tasks_raw = raw.get("tasks")
        if not isinstance(tasks_raw, list):
            return []
        todos: list[LifeTodo] = []
        migrated = version != _TODO_VERSION
        leftover = [
            _normalize_text(item)
            for item in list(raw.get("legacy_schedule_ids") or [])
            if _normalize_text(item)
        ]
        self.legacy_schedule_ids = leftover
        for item in tasks_raw:
            if not isinstance(item, dict):
                continue
            schedule_id = _normalize_text(item.get("schedule_record_id"))
            if schedule_id:
                self.legacy_schedule_ids.append(schedule_id)
                migrated = True
            try:
                todos.append(LifeTodo.from_dict(item))
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    f"parse TODO item failed: error_type={type(exc).__name__}"
                )
        if migrated and persist_migration:
            self.save(todos)
        return todos

    def save(self, todos: list[LifeTodo]) -> None:
        payload = {
            "version": _TODO_VERSION,
            "updated_at": _now_iso(),
            "legacy_schedule_ids": list(dict.fromkeys(self.legacy_schedule_ids)),
            "tasks": [todo.to_dict() for todo in _sorted_todos(todos)],
        }
        atomic_write_text(
            self.file_path,
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def get(self, todo_id: str) -> LifeTodo | None:
        ref = _normalize_text(todo_id)
        if not ref:
            return None
        prefix_hits: list[LifeTodo] = []
        for todo in self.load():
            if todo.id == ref:
                return todo
            if todo.id.startswith(ref):
                prefix_hits.append(todo)
        if len(prefix_hits) == 1:
            return prefix_hits[0]
        return None


def _coerce_todo_items(value: Any) -> list[dict[str, Any]]:
    payload = value
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ValueError("todos must be a JSON array") from exc
    if payload is None:
        return []
    if not isinstance(payload, list):
        raise TypeError("todos must be a list")
    items: list[dict[str, Any]] = []
    for raw in payload:
        if not isinstance(raw, dict):
            raise TypeError("each todo must be an object with content and status")
        items.append(dict(raw))
    return items


def apply_todo_write(
    current: list[LifeTodo],
    items: list[dict[str, Any]],
    *,
    merge: bool,
) -> list[LifeTodo]:
    """Apply a Codex/Claude-style board write. Rejects ranking fields."""

    incoming: list[LifeTodo] = []
    seen_ids: set[str] = set()
    by_id = {todo.id: todo for todo in current}
    for raw in items:
        for banned in ("importance", "priority", "score"):
            if banned in raw:
                raise ValueError("TODO items cannot carry ranking fields")
        raw_id = _normalize_text(raw.get("id"))
        existing = by_id.get(raw_id) if raw_id else None
        content = _content_from_legacy(raw)
        if not content:
            if existing is None:
                raise ValueError("write requires content for new items")
            content = existing.content
        status = _normalize_status(
            raw.get("status"),
            default=existing.status if existing is not None else "pending",
        )
        todo = LifeTodo(
            id=existing.id if existing is not None else (raw_id or _generate_todo_id()),
            content=content,
            status=status,
            due_at=raw.get("due_at") if "due_at" in raw else (
                existing.due_at if existing is not None else None
            ),
            source=_normalize_text(raw.get("source"))
            or (existing.source if existing is not None else "life_engine"),
            visibility=_normalize_visibility(
                raw.get("visibility")
                if "visibility" in raw
                else (existing.visibility if existing is not None else "private")
            ),
            created_at=existing.created_at if existing is not None else "",
            updated_at=existing.updated_at if existing is not None else "",
            history=list(existing.history) if existing is not None else [],
        )
        if existing is None:
            todo.append_history("create")
        elif existing.status != todo.status or existing.content != todo.content:
            todo.append_history(
                "update",
                extra={"from": existing.status, "to": todo.status},
            )
        else:
            todo.updated_at = _now_iso()
        if todo.id in seen_ids:
            raise ValueError("duplicate todo id in one write")
        seen_ids.add(todo.id)
        incoming.append(todo)

    in_progress = [todo.id for todo in incoming if todo.status == "in_progress"]
    if merge:
        kept_progress = [
            todo.id
            for todo in current
            if todo.status == "in_progress" and todo.id not in seen_ids
        ]
        in_progress.extend(kept_progress)
    if len(in_progress) > 1:
        raise ValueError("at most one in_progress item is allowed")

    if merge:
        merged = {todo.id: todo for todo in current}
        merged.update({todo.id: todo for todo in incoming})
        result = list(merged.values())
    else:
        result = list(incoming)
        for todo in current:
            if todo.id in seen_ids:
                continue
            if todo.is_open():
                todo.status = "cancelled"
                todo.append_history("cancel", extra={"reason": "replaced_board"})
            result.append(todo)

    open_count = sum(1 for todo in result if todo.is_open())
    if open_count > _MAX_OPEN_ITEMS:
        raise ValueError(f"open TODO board cannot exceed {_MAX_OPEN_ITEMS} items")
    return _sorted_todos(result)


def format_todo_board(
    todos: list[LifeTodo],
    *,
    max_bytes: int = _BOARD_MAX_BYTES,
    include_closed: bool = False,
) -> str | None:
    """Compact current board. Technical status+id order; never ranking."""

    visible = [
        todo
        for todo in _sorted_todos(todos)
        if include_closed or todo.is_open()
    ]
    if not visible:
        return None
    budget = max(256, int(max_bytes))
    marker = "### TODO 板"
    intro = "这是你留下的工作板，不是任务队列，也不按重要性排序。"
    shown: list[LifeTodo] = []
    omitted: list[str] = []

    def assemble(current: list[LifeTodo], missing: list[str]) -> str:
        parts = [marker, intro]
        if missing:
            parts.append("omitted: " + ", ".join(missing))
        for todo in current:
            due = f" due={todo.due_at}" if todo.due_at else ""
            parts.append(f"- [{todo.status}] `{todo.id}` {todo.content}{due}")
        return "\n".join(parts).rstrip() + "\n"

    for todo in visible:
        trial = assemble([*shown, todo], omitted)
        if _utf8_size(trial) <= budget:
            shown.append(todo)
            continue
        omitted.append(todo.id)
        trial_omit = assemble(shown, omitted)
        if _utf8_size(trial_omit) <= budget:
            continue
        if shown:
            moved = shown.pop()
            omitted.insert(0, moved.id)
    text = assemble(shown, omitted)
    if _utf8_size(text) > budget:
        text = _clip_utf8(text, budget)
    return text


def todo_health_snapshot(todos: list[LifeTodo]) -> dict[str, Any]:
    counts = {name: 0 for name in STATUS_ORDER}
    for todo in todos:
        counts[todo.status] = counts.get(todo.status, 0) + 1
    return {
        "component": "todo_board",
        "status": "ready" if todos else "empty",
        "counts": counts,
        "open_count": sum(counts[name] for name in OPEN_STATUSES),
        "error_type": "",
    }


def _get_storage(plugin: Any) -> TodoStorage:
    return TodoStorage(_get_workspace(plugin))


async def _reap_legacy_schedules(
    plugin: Any,
    storage: TodoStorage,
    todos: list[LifeTodo],
) -> None:
    ids = list(dict.fromkeys(storage.legacy_schedule_ids))
    if not ids:
        return
    storage.legacy_schedule_ids = []
    storage.save(todos)
    try:
        from .schedule_tools import LifeEngineManageScheduleTool

        tool = LifeEngineManageScheduleTool(plugin=plugin)
        for record_id in ids:
            try:
                await tool.execute(action="delete", task_ref=record_id)
            except Exception:  # noqa: BLE001
                logger.debug(
                    "legacy TODO schedule cleanup skipped: "
                    f"record_id={record_id}"
                )
    except Exception:  # noqa: BLE001
        logger.debug("legacy TODO schedule cleanup unavailable")


class NucleusTodoTool(BaseTool):
    """One TODO tool: write the board, or read a compact projection."""

    tool_name: str = "nucleus_todo"
    tool_description: str = (
        "管理你的 TODO 板。写法对齐常见 coding agent："
        "一次提交一组 `{id, content, status}`，用 merge 决定是补丁还是整板替换。\n"
        "status 只能是 pending / in_progress / completed / cancelled。"
        "同一时刻最多一条 in_progress。禁止 importance/priority/score。\n"
        "action=write：写入或合并。merge=true（默认）按 id 更新；"
        "merge=false 以本次列表为开放板，未出现的开放项会被取消，已结束项保留。\n"
        "action=list：只读当前板，按 status+id 排列，默认不含已结束项。\n"
        "action=get：读一条完整记录。\n"
        "这不是任务队列，忽略不等于拒绝，系统不会替你排序或催办。"
    )
    chatter_allow: ClassVar[list[str]] = ["life_engine_internal", "life_chatter"]

    async def execute(
        self,
        action: Annotated[TodoAction, "write / list / get"] = "list",
        todos: Annotated[
            list[dict[str, Any]] | None,
            "write 时的整组条目，每项含 content、status，可选 id/due_at",
        ] = None,
        merge: Annotated[bool, "true=按 id 合并；false=替换开放板"] = True,
        todo_id: Annotated[str, "get 时的 TODO id"] = "",
        status: Annotated[TodoStatusLiteral | None, "list 时按状态筛选"] = None,
        include_completed: Annotated[bool, "list 是否包含 completed/cancelled"] = False,
        limit: Annotated[int, "list 最多返回条数，最大 40"] = 20,
    ) -> tuple[bool, str | dict[str, Any]]:
        action_value = _normalize_text(action).lower() or "list"
        async with _get_todo_write_lock():
            try:
                storage = _get_storage(self.plugin)
                current = storage.load()
                await _reap_legacy_schedules(self.plugin, storage, current)
                if action_value in {"write", "update_plan", "todo_write"}:
                    try:
                        items = _coerce_todo_items(todos)
                    except (TypeError, ValueError) as exc:
                        return False, str(exc)
                    if not items:
                        return False, "write 需要至少一条 todos 项"
                    try:
                        next_board = apply_todo_write(
                            current,
                            items,
                            merge=bool(merge),
                        )
                    except ValueError as exc:
                        return False, str(exc)
                    storage.save(next_board)
                    open_items = [todo for todo in next_board if todo.is_open()]
                    return True, {
                        "action": "write_todo_board",
                        "merge": bool(merge),
                        "todos": [todo.summary() for todo in open_items],
                        "open_count": len(open_items),
                        "all_count": len(next_board),
                        "in_progress_id": next(
                            (todo.id for todo in open_items if todo.status == "in_progress"),
                            "",
                        ),
                    }
                if action_value in {"get"}:
                    todo = storage.get(todo_id)
                    if todo is None:
                        return False, f"找不到 TODO: {todo_id}"
                    return True, {"action": "get_todo", "todo": todo.to_dict()}
                if action_value not in {"list", "show", "query"}:
                    return False, (
                        "未知 action。只用 write / list / get。"
                    )
                filtered = [
                    todo
                    for todo in current
                    if (include_completed or todo.is_open())
                    and (status is None or todo.status == _normalize_status(status))
                ]
                try:
                    cap = max(1, min(int(limit), _MAX_LIST_LIMIT))
                except (TypeError, ValueError):
                    cap = 20
                returned = filtered[:cap]
                return True, {
                    "action": "list_todos",
                    "todos": [todo.summary() for todo in returned],
                    "total": len(filtered),
                    "returned": len(returned),
                    "truncated": len(filtered) > len(returned),
                    "open_count": sum(1 for todo in current if todo.is_open()),
                    "all_count": len(current),
                    "in_progress_id": next(
                        (
                            todo.id
                            for todo in current
                            if todo.status == "in_progress"
                        ),
                        "",
                    ),
                }
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    f"TODO board failed: error_type={type(exc).__name__}"
                )
                return False, f"操作失败: {type(exc).__name__}"


TODO_TOOLS = [NucleusTodoTool]
