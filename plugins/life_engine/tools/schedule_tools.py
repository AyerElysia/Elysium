"""life_engine 定时任务管理工具集。

让生命中枢自己管理自己的定时任务：
- 创建
- 修改
- 删除
- 查询/列表

仅允许两类安全模板：
- heartbeat: 触发一次心跳
- message: 向生命中枢队列注入一条提醒消息

历史 ``dream`` 记录仍可加载和列出，但执行时会明确报告已退役；
新建和更新不再接受这种会按分数改写关系的旧模板。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Annotated, Any, Literal
from uuid import uuid4

from src.app.plugin_system.api import log_api
from src.app.plugin_system.base import BaseTool
from src.kernel.scheduler import TriggerType, get_unified_scheduler

from ..storage_utils import atomic_write_text
from ._utils import _get_workspace

logger = log_api.get_logger("life_engine.schedule_tools")

ScheduleKind = Literal["heartbeat", "message"]
TriggerMode = Literal["at", "delay", "interval"]
ScheduleAction = Literal["create", "update", "delete"]

_REGISTRY_FILE = "life_engine_schedules.json"
_REGISTRY_VERSION = 1
_REGISTRY_LOCK: asyncio.Lock | None = None


def _get_registry_lock() -> asyncio.Lock:
    global _REGISTRY_LOCK
    if _REGISTRY_LOCK is None:
        _REGISTRY_LOCK = asyncio.Lock()
    return _REGISTRY_LOCK


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()


def _normalize_text(value: Any) -> str:
    return str(value or "").strip()


def _normalize_task_name(record_id: str) -> str:
    return f"life_schedule::{record_id[:8]}"


def _parse_trigger_at(value: str) -> datetime:
    text = _normalize_text(value)
    if not text:
        raise ValueError("trigger_at 不能为空")
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    trigger_at = datetime.fromisoformat(text)
    if trigger_at.tzinfo is not None:
        trigger_at = trigger_at.astimezone().replace(tzinfo=None)
    return trigger_at


def _serialize_trigger_config(config: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key, value in config.items():
        if isinstance(value, datetime):
            payload[key] = value.isoformat()
        else:
            payload[key] = value
    return payload


def _deserialize_trigger_config(config: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key, value in config.items():
        if key == "trigger_at" and isinstance(value, str) and value:
            payload[key] = _parse_trigger_at(value)
        else:
            payload[key] = value
    return payload


@dataclass
class ScheduleRecord:
    """生命中枢定时任务记录。"""

    record_id: str
    title: str
    kind: str
    task_name: str
    trigger_mode: TriggerMode
    trigger_config: dict[str, Any]
    recurring: bool
    message: str = ""
    notes: str = ""
    schedule_id: str | None = None
    created_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["trigger_config"] = _serialize_trigger_config(self.trigger_config)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ScheduleRecord":
        trigger_config_raw = data.get("trigger_config") or {}
        trigger_config = (
            _deserialize_trigger_config(trigger_config_raw)
            if isinstance(trigger_config_raw, dict)
            else {}
        )
        return cls(
            record_id=str(data.get("record_id") or uuid4().hex),
            title=str(data.get("title") or "").strip(),
            kind=str(data.get("kind") or "message"),
            task_name=str(data.get("task_name") or "").strip(),
            trigger_mode=str(data.get("trigger_mode") or "delay"),
            trigger_config=trigger_config,
            recurring=bool(data.get("recurring")),
            message=str(data.get("message") or ""),
            notes=str(data.get("notes") or ""),
            schedule_id=(
                str(data.get("schedule_id") or "").strip() or None
            ),
            created_at=str(data.get("created_at") or _now_iso()),
            updated_at=str(data.get("updated_at") or _now_iso()),
        )


class ScheduleStore:
    """生命中枢定时任务登记册。"""

    def __init__(self, plugin: Any) -> None:
        self._plugin = plugin
        self._path = _get_workspace(plugin) / _REGISTRY_FILE

    def load(self) -> list[ScheduleRecord]:
        if not self._path.exists():
            return []
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"读取定时任务登记册失败: {exc}")
            return []

        if not isinstance(raw, dict):
            return []
        records_raw = raw.get("records")
        if not isinstance(records_raw, list):
            return []

        records: list[ScheduleRecord] = []
        for item in records_raw:
            if isinstance(item, dict):
                try:
                    records.append(ScheduleRecord.from_dict(item))
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"解析定时任务记录失败: {exc}")
        return records

    def save(self, records: list[ScheduleRecord]) -> None:
        payload = {
            "version": _REGISTRY_VERSION,
            "updated_at": _now_iso(),
            "records": [record.to_dict() for record in records],
        }
        atomic_write_text(
            self._path,
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def upsert(self, record: ScheduleRecord) -> None:
        records = self.load()
        replaced = False
        for index, item in enumerate(records):
            if item.record_id == record.record_id:
                records[index] = record
                replaced = True
                break
        if not replaced:
            records.append(record)
        self.save(records)

    def remove(self, record_id: str) -> bool:
        records = self.load()
        next_records = [item for item in records if item.record_id != record_id]
        if len(next_records) == len(records):
            return False
        self.save(next_records)
        return True

    def find_matches(self, task_ref: str) -> list[ScheduleRecord]:
        ref = _normalize_text(task_ref)
        if not ref:
            return []
        return _resolve_records(self.load(), ref)

    def list_records(self) -> list[ScheduleRecord]:
        return self.load()


def _get_store(plugin: Any) -> ScheduleStore:
    return ScheduleStore(plugin)


def _get_service(plugin: Any):
    service = getattr(plugin, "service", None)
    if service is None:
        raise RuntimeError("life_engine 服务不可用")
    return service


def _normalize_match_key(value: Any) -> str:
    return _normalize_text(value).lower()


def _record_title_matches(record: ScheduleRecord, title: str) -> bool:
    return _normalize_match_key(record.title) == _normalize_match_key(title)


def _resolve_records(records: list[ScheduleRecord], task_ref: str) -> list[ScheduleRecord]:
    ref = _normalize_text(task_ref)
    if not ref:
        return []

    ref_lower = ref.lower()
    exact_matches: list[ScheduleRecord] = []
    prefix_matches: list[ScheduleRecord] = []

    for record in records:
        record_id = _normalize_text(record.record_id)
        schedule_id = _normalize_text(record.schedule_id)
        task_name = _normalize_text(record.task_name)
        title = _normalize_match_key(record.title)

        if (
            record_id == ref
            or schedule_id == ref
            or task_name == ref
            or title == ref_lower
        ):
            exact_matches.append(record)
            continue

        if (
            record_id.startswith(ref)
            or schedule_id.startswith(ref)
            or task_name.startswith(ref)
        ):
            prefix_matches.append(record)

    return exact_matches if exact_matches else prefix_matches


def _find_records_by_title(records: list[ScheduleRecord], title: str) -> list[ScheduleRecord]:
    normalized = _normalize_match_key(title)
    if not normalized:
        return []
    return [record for record in records if _normalize_match_key(record.title) == normalized]


def _build_trigger_spec(
    trigger_mode: TriggerMode,
    trigger_at: str | None,
    delay_seconds: float | None,
    interval_seconds: float | None,
    recurring: bool,
) -> tuple[TriggerType, dict[str, Any], bool]:
    if trigger_mode == "at":
        trigger_dt = _parse_trigger_at(trigger_at or "")
        if recurring:
            interval = interval_seconds if interval_seconds is not None else delay_seconds
            if interval is None:
                raise ValueError("重复触发时必须提供 interval_seconds 或 delay_seconds")
            return TriggerType.TIME, {"trigger_at": trigger_dt, "interval_seconds": float(interval)}, True
        return TriggerType.TIME, {"trigger_at": trigger_dt}, False

    if trigger_mode == "delay":
        delay = delay_seconds if delay_seconds is not None else interval_seconds
        if delay is None:
            raise ValueError("delay 模式需要 delay_seconds")
        if recurring:
            return TriggerType.TIME, {"interval_seconds": float(delay)}, True
        return TriggerType.TIME, {"delay_seconds": float(delay)}, False

    if trigger_mode == "interval":
        interval = interval_seconds if interval_seconds is not None else delay_seconds
        if interval is None:
            raise ValueError("interval 模式需要 interval_seconds")
        return TriggerType.TIME, {"interval_seconds": float(interval)}, True

    raise ValueError(f"未知 trigger_mode: {trigger_mode}")


def _build_callback(plugin: Any, record: ScheduleRecord):
    async def _callback() -> None:
        service = _get_service(plugin)
        kind = record.kind
        logger.info(
            f"执行定时任务: title={record.title} kind={kind} task_name={record.task_name}"
        )

        if kind == "heartbeat":
            await service.trigger_heartbeat_manually()
            return
        if kind == "dream":
            logger.warning(
                f"定时任务 {record.title} 使用已退役的 dream 模板；"
                "未执行任何记忆写入: error=LegacyDreamScheduleRetired"
            )
            return
        if kind == "message":
            message = _normalize_text(record.message)
            if not message:
                logger.warning(f"定时任务 {record.title} 缺少 message，跳过")
                return
            await service.enqueue_direct_message(
                message,
                sender_name="生命定时任务",
                sender_id="life_schedule",
            )
            return

        raise ValueError(f"不支持的定时任务 kind: {kind}")

    return _callback


async def _remove_scheduler_task_by_record(plugin: Any, record: ScheduleRecord) -> bool:
    scheduler = get_unified_scheduler()
    removed = False
    try:
        if record.schedule_id:
            removed = await scheduler.remove_schedule(record.schedule_id)
    except Exception:
        removed = False

    if removed:
        return True

    try:
        found_id = await scheduler.find_schedule_by_name(record.task_name)
        if found_id:
            return await scheduler.remove_schedule(found_id)
    except Exception:
        pass

    return False


async def _schedule_record(plugin: Any, record: ScheduleRecord) -> str:
    scheduler = get_unified_scheduler()
    callback = _build_callback(plugin, record)
    schedule_id = await scheduler.create_schedule(
        callback=callback,
        trigger_type=TriggerType.TIME,
        trigger_config=record.trigger_config,
        is_recurring=record.recurring,
        task_name=record.task_name,
        force_overwrite=True,
    )
    record.schedule_id = schedule_id
    record.updated_at = _now_iso()
    return schedule_id


def _record_to_summary(record: ScheduleRecord, task_info: dict[str, Any] | None) -> dict[str, Any]:
    summary = {
        "record_id": record.record_id,
        "title": record.title,
        "kind": record.kind,
        "task_name": record.task_name,
        "schedule_id": record.schedule_id,
        "trigger_mode": record.trigger_mode,
        "trigger_config": _serialize_trigger_config(record.trigger_config),
        "recurring": record.recurring,
        "message": record.message,
        "notes": record.notes,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
        "status": "missing",
    }
    if task_info:
        summary.update(
            {
                "schedule_id": task_info.get("schedule_id") or record.schedule_id,
                "status": task_info.get("status") or "unknown",
                "trigger_count": task_info.get("trigger_count", 0),
                "success_count": task_info.get("success_count", 0),
                "failure_count": task_info.get("failure_count", 0),
                "last_triggered_at": task_info.get("last_triggered_at"),
                "last_error": task_info.get("last_error"),
                "trigger_type": task_info.get("trigger_type"),
                "is_running": task_info.get("is_running", False),
            }
        )
    return summary


async def _resolve_live_task_info(record: ScheduleRecord) -> dict[str, Any] | None:
    scheduler = get_unified_scheduler()
    if record.schedule_id:
        try:
            info = await scheduler.get_task_info(record.schedule_id)
            if info:
                return info
        except Exception:
            pass

    try:
        found_id = await scheduler.find_schedule_by_name(record.task_name)
        if found_id:
            info = await scheduler.get_task_info(found_id)
            if info:
                return info
    except Exception:
        pass

    return None


async def restore_life_schedules_when_ready(plugin: Any) -> dict[str, str]:
    """等待调度器就绪后，仅恢复当前受支持的登记任务。

    历史 ``dream`` 或未知 kind 继续留在登记册中供审计，但绝不重新
    注册 callback。这样旧的关系改写机制不会因为一次进程重启重新
    进入活跃运行面。
    """
    if not getattr(getattr(plugin, "config", None), "settings", None):
        return {}

    store = _get_store(plugin)
    records = store.list_records()
    if not records:
        return {}

    scheduler = get_unified_scheduler()
    for _ in range(600):
        try:
            await scheduler.list_tasks()
            break
        except RuntimeError:
            await asyncio.sleep(0.5)
    else:
        logger.warning("等待调度器就绪超时，未恢复生命定时任务")
        return {}

    restored: dict[str, str] = {}
    async with _get_registry_lock():
        current_records = store.list_records()
        for record in current_records:
            if record.kind not in {"heartbeat", "message"}:
                logger.warning(
                    "跳过已退役或未知的生命定时任务恢复: "
                    f"task_name={record.task_name} kind={record.kind} "
                    "error=LegacyScheduleKindNotRestored"
                )
                continue

            live_info = await _resolve_live_task_info(record)
            if live_info:
                record.schedule_id = str(live_info.get("schedule_id") or record.schedule_id or "")
                if record.schedule_id:
                    record.updated_at = _now_iso()
                    store.upsert(record)
                continue

            try:
                schedule_id = await _schedule_record(plugin, record)
                store.upsert(record)
                restored[record.record_id] = schedule_id
                logger.info(
                    f"已恢复生命定时任务: title={record.title} task_name={record.task_name} schedule_id={schedule_id}"
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    f"恢复生命定时任务失败: title={record.title} task_name={record.task_name} error={exc}"
                )

    return restored


async def cleanup_life_schedules(plugin: Any) -> int:
    """移除当前运行中的生命定时任务，但保留登记册。"""
    store = _get_store(plugin)
    records = store.list_records()
    if not records:
        return 0

    removed = 0
    for record in records:
        try:
            if await _remove_scheduler_task_by_record(plugin, record):
                removed += 1
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                f"清理生命定时任务失败: title={record.title} task_name={record.task_name} error={exc}"
            )
    return removed


def _resolve_single_record(store: ScheduleStore, task_ref: str) -> tuple[bool, ScheduleRecord | str]:
    """解析 task_ref 到单条记录，失败返回 (False, error_msg)。"""
    ref = _normalize_text(task_ref)
    if not ref:
        return False, "task_ref 不能为空"
    matches = store.find_matches(ref)
    if not matches:
        return False, "没有找到对应的定时任务"
    if len(matches) > 1:
        return False, "task_ref 对应多个任务，请改用更精确的 record_id 或 schedule_id"
    return True, matches[0]


class LifeEngineManageScheduleTool(BaseTool):
    """管理生命中枢定时任务（创建/修改/删除合一）。"""

    tool_name = "nucleus_manage_schedule"
    tool_description = (
        "管理生命中枢定时任务：创建、修改或删除。"
        "仅允许安全模板：heartbeat / message。旧 dream 记录只读保留且不会执行。"
        "\n\n"
        "**action=create** — 创建新的定时任务。"
        "\ntrigger_mode 说明：at（绝对时间）/ delay（相对延迟）/ interval（周期触发）。"
        "如果想安排每天/每隔一段时间执行一次，请优先选择 interval。"
        "\n\n"
        "**action=update** — 修改已有的定时任务。只改部分字段，其他保持不变。"
        "\n\n"
        "**action=delete** — 删除定时任务，从调度器和登记册中移除。"
    )
    chatter_allow: list[str] = ["life_engine_internal", "life_chatter"]

    async def execute(
        self,
        action: Annotated[ScheduleAction, "操作：create / update / delete"],
        # create & update 共用参数
        title: Annotated[str, "任务标题（create 必填，update 可选）"] = "",
        kind: Annotated[ScheduleKind, "任务模板：heartbeat / message"] = "message",
        trigger_mode: Annotated[TriggerMode, "触发方式：at / delay / interval"] = "delay",
        trigger_at: Annotated[str | None, "绝对时间（ISO 格式）"] = None,
        delay_seconds: Annotated[float | None, "延迟秒数"] = None,
        interval_seconds: Annotated[float | None, "周期秒数"] = None,
        recurring: Annotated[bool | None, "是否循环执行"] = None,
        message: Annotated[str, "kind=message 时要注入的提醒内容"] = "",
        notes: Annotated[str, "补充说明"] = "",
        # create 专用
        replace_existing: Annotated[bool, "create 时若已存在同标题任务，先移除旧任务再创建"] = True,
        # update & delete 专用
        task_ref: Annotated[str, "任务引用：record_id / schedule_id / task_name / title（update/delete 必填）"] = "",
    ) -> tuple[bool, str | dict]:
        if action == "create":
            return await self._create(title, kind, trigger_mode, trigger_at,
                                      delay_seconds, interval_seconds, recurring,
                                      message, notes, replace_existing)
        if action == "update":
            return await self._update(task_ref, title, kind, trigger_mode, trigger_at,
                                      delay_seconds, interval_seconds, recurring,
                                      message, notes)
        if action == "delete":
            return await self._delete(task_ref)
        return False, f"未知 action: {action}，请使用 create/update/delete"

    async def _create(
        self, title, kind, trigger_mode, trigger_at,
        delay_seconds, interval_seconds, recurring,
        message, notes, replace_existing,
    ) -> tuple[bool, str | dict]:
        title_text = _normalize_text(title)
        if not title_text:
            return False, "title 不能为空"

        kind_value = _normalize_text(kind)
        if kind_value not in {"heartbeat", "message"}:
            return False, "kind 只能是 heartbeat / message；dream 已退役"

        trigger_mode_value = _normalize_text(trigger_mode)
        if trigger_mode_value not in {"at", "delay", "interval"}:
            return False, "trigger_mode 只能是 at / delay / interval"

        if kind_value == "message" and not _normalize_text(message):
            return False, "kind=message 时必须提供 message"

        try:
            trigger_type, trigger_config, recurring_value = _build_trigger_spec(
                trigger_mode_value, trigger_at, delay_seconds, interval_seconds,
                bool(recurring) if recurring is not None else False,
            )
        except Exception as exc:  # noqa: BLE001
            return False, f"触发配置无效: {exc}"

        record = ScheduleRecord(
            record_id=uuid4().hex,
            title=title_text,
            kind=kind_value,  # type: ignore[arg-type]
            task_name=_normalize_task_name(uuid4().hex),
            trigger_mode=trigger_mode_value,  # type: ignore[arg-type]
            trigger_config=trigger_config,
            recurring=recurring_value,
            message=_normalize_text(message),
            notes=_normalize_text(notes),
        )
        # task_name 固定用 record_id，便于后续查找和更新
        record.task_name = _normalize_task_name(record.record_id)

        store = _get_store(self.plugin)
        async with _get_registry_lock():
            if replace_existing:
                for existed in _find_records_by_title(store.list_records(), title_text):
                    await _remove_scheduler_task_by_record(self.plugin, existed)
                records = store.list_records()
                records = [
                    item for item in records if not _record_title_matches(item, title_text)
                ]
                store.save(records)

            try:
                scheduler = get_unified_scheduler()
                schedule_id = await scheduler.create_schedule(
                    callback=_build_callback(self.plugin, record),
                    trigger_type=trigger_type,
                    trigger_config=record.trigger_config,
                    is_recurring=record.recurring,
                    task_name=record.task_name,
                    force_overwrite=True,
                )
                record.schedule_id = schedule_id
                store.upsert(record)
            except RuntimeError:
                return False, "调度器尚未启动，稍后再试"
            except Exception as exc:  # noqa: BLE001
                return False, f"创建定时任务失败: {exc}"

        return True, {
            "created": True,
            "record_id": record.record_id,
            "schedule_id": record.schedule_id,
            "task_name": record.task_name,
            "title": record.title,
            "kind": record.kind,
            "trigger_mode": record.trigger_mode,
            "trigger_config": _serialize_trigger_config(record.trigger_config),
            "recurring": record.recurring,
            "message": record.message,
            "notes": record.notes,
        }

    async def _update(
        self, task_ref, title, kind, trigger_mode, trigger_at,
        delay_seconds, interval_seconds, recurring,
        message, notes,
    ) -> tuple[bool, str | dict]:
        store = _get_store(self.plugin)
        ok, result = _resolve_single_record(store, task_ref)
        if not ok:
            return False, str(result)
        record = result

        new_title = _normalize_text(title) or record.title
        new_kind = _normalize_text(kind) or record.kind
        if new_kind not in {"heartbeat", "message"}:
            return False, "kind 只能是 heartbeat / message；dream 已退役"

        new_trigger_mode = _normalize_text(trigger_mode) or record.trigger_mode
        if new_trigger_mode not in {"at", "delay", "interval"}:
            return False, "trigger_mode 只能是 at / delay / interval"

        new_message = record.message if message is None else _normalize_text(message)
        new_notes = record.notes if notes is None else _normalize_text(notes)

        try:
            trigger_type, trigger_config, recurring_value = _build_trigger_spec(
                new_trigger_mode,
                trigger_at,
                delay_seconds,
                interval_seconds,
                record.recurring if recurring is None else bool(recurring),
            )
        except Exception as exc:  # noqa: BLE001
            return False, f"触发配置无效: {exc}"

        if new_kind == "message" and not new_message:
            return False, "kind=message 时必须提供 message"

        await _remove_scheduler_task_by_record(self.plugin, record)

        record.title = new_title
        record.kind = new_kind  # type: ignore[assignment]
        record.trigger_mode = new_trigger_mode  # type: ignore[assignment]
        record.trigger_config = trigger_config
        record.recurring = recurring_value
        record.message = new_message
        record.notes = new_notes
        record.updated_at = _now_iso()

        try:
            schedule_id = await _schedule_record(self.plugin, record)
        except RuntimeError:
            return False, "调度器尚未启动，稍后再试"
        except Exception as exc:  # noqa: BLE001
            return False, f"更新定时任务失败: {exc}"

        store.upsert(record)
        return True, {
            "updated": True,
            "record_id": record.record_id,
            "schedule_id": schedule_id,
            "task_name": record.task_name,
            "title": record.title,
            "kind": record.kind,
            "trigger_mode": record.trigger_mode,
            "trigger_config": _serialize_trigger_config(record.trigger_config),
            "recurring": record.recurring,
            "message": record.message,
            "notes": record.notes,
        }

    async def _delete(self, task_ref: str) -> tuple[bool, str | dict]:
        store = _get_store(self.plugin)
        ok, result = _resolve_single_record(store, task_ref)
        if not ok:
            return False, str(result)
        record = result

        await _remove_scheduler_task_by_record(self.plugin, record)
        removed = store.remove(record.record_id)
        if not removed:
            return False, "任务已从登记册中不存在"

        return True, {
            "deleted": True,
            "record_id": record.record_id,
            "schedule_id": record.schedule_id,
            "task_name": record.task_name,
            "title": record.title,
            "kind": record.kind,
        }


class LifeEngineListSchedulesTool(BaseTool):
    """列出/查询生命中枢定时任务。"""

    tool_name = "nucleus_list_schedules"
    tool_description = (
        "列出生命中枢所有定时任务，或查询单条任务详情。"
        "\n\n"
        "- 不带 task_ref → 列表模式：返回所有任务，可按 kind/status 过滤"
        "- 带 task_ref → 详情模式：返回该条任务的登记册信息和调度器实时状态"
        "\n\n"
        "status 取值来自调度器：pending/running/completed/failed/cancelled/paused/timeout，"
        "若登记册里有但调度器中已不存在则显示 missing。"
    )
    chatter_allow: list[str] = ["life_engine_internal", "life_chatter"]

    async def execute(
        self,
        task_ref: Annotated[str, "任务引用（填写则返回单条详情，留空返回列表）"] = "",
        kind: Annotated[str, "列表模式过滤 kind：heartbeat / dream / message / all"] = "all",
        status: Annotated[str, "列表模式过滤状态：pending/running/completed/failed/cancelled/paused/timeout/missing/all"] = "all",
    ) -> tuple[bool, str | dict]:
        # 单条详情模式
        if _normalize_text(task_ref):
            store = _get_store(self.plugin)
            ok, result = _resolve_single_record(store, task_ref)
            if not ok:
                return False, str(result)
            record = result
            task_info = await _resolve_live_task_info(record)
            return True, _record_to_summary(record, task_info)

        # 列表模式
        kind_value = _normalize_text(kind).lower()
        status_value = _normalize_text(status).lower()
        if kind_value not in {"all", "heartbeat", "dream", "message"}:
            return False, "kind 只能是 heartbeat / dream / message / all"
        if status_value not in {
            "all",
            "pending",
            "running",
            "completed",
            "failed",
            "cancelled",
            "paused",
            "timeout",
            "missing",
        }:
            return False, "status 取值不合法"

        store = _get_store(self.plugin)
        records = store.list_records()
        items: list[dict[str, Any]] = []
        for record in records:
            if kind_value != "all" and record.kind != kind_value:
                continue
            task_info = await _resolve_live_task_info(record)
            summary = _record_to_summary(record, task_info)
            if status_value != "all" and str(summary.get("status") or "").lower() != status_value:
                continue
            items.append(summary)

        items.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
        return True, {"count": len(items), "tasks": items}


class NucleusScheduleTool(BaseTool):
    """统一的日程管理工具（合并原 nucleus_manage_schedule + nucleus_list_schedules）。"""

    tool_name = "nucleus_schedule"
    tool_description = (
        "管理日程/定时任务。\n\n"
        "action=list：查看当前日程列表\n"
        "action=add/create：添加新日程\n"
        "action=remove/delete：删除日程\n"
        "action=pause：暂停日程\n"
        "action=resume：恢复日程"
    )
    chatter_allow: list[str] = ["life_engine_internal", "life_chatter"]

    async def execute(self, action: Annotated[str, "操作：list/add/create/remove/delete/pause/resume"] = "list", **kwargs: object) -> tuple[bool, str | dict]:
        action_value = str(action or "list").strip().lower()
        if action_value in ("list", "show", "query"):
            tool = LifeEngineListSchedulesTool(plugin=self.plugin)
            return await tool.execute(**kwargs)  # type: ignore[arg-type]
        else:
            tool = LifeEngineManageScheduleTool(plugin=self.plugin)
            return await tool.execute(action=action_value, **kwargs)  # type: ignore[arg-type]


SCHEDULE_TOOLS = [
    NucleusScheduleTool,
]

__all__ = [
    "ScheduleRecord",
    "ScheduleStore",
    "restore_life_schedules_when_ready",
    "cleanup_life_schedules",
    "NucleusScheduleTool",
    "SCHEDULE_TOOLS",
]
