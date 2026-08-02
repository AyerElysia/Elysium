"""Autonomy intent primitives for life_engine.

An autonomy intent is not a command. It is a delayed inner intention that may
surface later and be re-evaluated by the expression layer.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from src.app.plugin_system.api import log_api
from src.kernel.scheduler import TriggerType, get_unified_scheduler


logger = log_api.get_logger("life_engine.autonomy", display="Autonomy")

AutonomyIntentKind = Literal["speak", "reflect", "silence"]
AutonomyIntentStatus = Literal["scheduled", "triggered", "expired", "cancelled", "rejected"]

_STORE_FILE = "autonomy_intents.json"
_STORE_VERSION = 2
_LOCK: asyncio.Lock | None = None


def _get_lock() -> asyncio.Lock:
    global _LOCK
    if _LOCK is None:
        _LOCK = asyncio.Lock()
    return _LOCK


def now_local() -> datetime:
    return datetime.now(timezone.utc).astimezone()


def iso_now() -> str:
    return now_local().isoformat()


def parse_iso_datetime(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.astimezone()
    return dt.astimezone()


def normalize_intent_task_name(intent_id: str) -> str:
    return f"life_autonomy::{str(intent_id or '')[:12]}"


def _shorten(value: Any, *, max_length: int) -> str:
    text = str(value or "").strip()
    if len(text) <= max_length:
        return text
    return text[: max_length - 3].rstrip() + "..."


@dataclass(slots=True)
class AutonomyIntent:
    """A delayed intention created by life_engine itself."""

    intent_id: str
    kind: AutonomyIntentKind
    motivation: str
    delay_minutes: int
    scheduled_at: str
    status: AutonomyIntentStatus = "scheduled"
    target_hint: str = ""
    target_key: str = ""
    target_stream_id: str = ""
    constraints: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=iso_now)
    updated_at: str = field(default_factory=iso_now)
    triggered_at: str = ""
    rejected_reason: str = ""
    schedule_id: str = ""
    task_name: str = ""
    repeat: bool = False
    interval_minutes: int = 0
    occurrence_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AutonomyIntent":
        intent_id = str(data.get("intent_id") or uuid4().hex)
        task_name = str(data.get("task_name") or normalize_intent_task_name(intent_id))
        delay_minutes = int(data.get("delay_minutes") or 1)
        repeat = bool(data.get("repeat") or data.get("recurring"))
        interval_minutes = int(data.get("interval_minutes") or 0)
        if repeat and interval_minutes <= 0:
            interval_minutes = delay_minutes
        constraints_raw = data.get("constraints") or []
        constraints = [
            _shorten(item, max_length=120)
            for item in constraints_raw
            if str(item or "").strip()
        ] if isinstance(constraints_raw, list) else []
        return cls(
            intent_id=intent_id,
            kind=str(data.get("kind") or "reflect"),  # type: ignore[arg-type]
            motivation=_shorten(data.get("motivation"), max_length=600),
            delay_minutes=delay_minutes,
            scheduled_at=str(data.get("scheduled_at") or iso_now()),
            status=str(data.get("status") or "scheduled"),  # type: ignore[arg-type]
            target_hint=_shorten(data.get("target_hint"), max_length=160),
            target_key=_shorten(data.get("target_key"), max_length=80),
            target_stream_id=_shorten(data.get("target_stream_id"), max_length=128),
            constraints=constraints[:8],
            created_at=str(data.get("created_at") or iso_now()),
            updated_at=str(data.get("updated_at") or iso_now()),
            triggered_at=str(data.get("triggered_at") or ""),
            rejected_reason=_shorten(data.get("rejected_reason"), max_length=240),
            schedule_id=str(data.get("schedule_id") or ""),
            task_name=task_name,
            repeat=repeat,
            interval_minutes=interval_minutes,
            occurrence_count=max(0, int(data.get("occurrence_count") or 0)),
        )


class AutonomyIntentStore:
    """JSON store for autonomy intents in the life workspace."""

    def __init__(self, workspace_path: str | Path) -> None:
        self.path = Path(workspace_path) / _STORE_FILE

    def load(self) -> list[AutonomyIntent]:
        if not self.path.exists():
            return []
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"读取自主意向存储失败: {exc}")
            return []
        if not isinstance(raw, dict):
            return []
        items = raw.get("intents")
        if not isinstance(items, list):
            return []
        intents: list[AutonomyIntent] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            try:
                intents.append(AutonomyIntent.from_dict(item))
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"解析自主意向失败: {exc}")
        return intents

    def save(self, intents: list[AutonomyIntent]) -> None:
        parent = self.path.parent
        parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": _STORE_VERSION,
            "updated_at": iso_now(),
            "intents": [intent.to_dict() for intent in intents],
        }
        self.path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def upsert(self, intent: AutonomyIntent) -> None:
        intents = self.load()
        replaced = False
        for index, item in enumerate(intents):
            if item.intent_id == intent.intent_id:
                intents[index] = intent
                replaced = True
                break
        if not replaced:
            intents.append(intent)
        self.save(intents)

    def get(self, intent_id: str) -> AutonomyIntent | None:
        target = str(intent_id or "").strip()
        if not target:
            return None
        for intent in self.load():
            if intent.intent_id == target:
                return intent
        return None

    def list_scheduled(self) -> list[AutonomyIntent]:
        return [intent for intent in self.load() if intent.status == "scheduled"]


def build_intent(
    *,
    kind: str,
    motivation: str,
    delay_minutes: int,
    min_delay_minutes: int = 1,
    max_delay_minutes: int = 1440,
    target_hint: str = "",
    target_key: str = "",
    target_stream_id: str = "",
    constraints: list[str] | None = None,
    repeat: bool = False,
    interval_minutes: int | None = None,
) -> AutonomyIntent:
    kind_value = str(kind or "").strip().lower()
    if kind_value not in {"speak", "reflect", "silence"}:
        raise ValueError("kind 只能是 speak / reflect / silence")
    motivation_text = _shorten(motivation, max_length=600)
    if not motivation_text:
        raise ValueError("motivation 不能为空")

    delay = int(delay_minutes or 0)
    min_delay = max(1, int(min_delay_minutes or 1))
    max_delay = max(min_delay, int(max_delay_minutes or 1440))
    if delay < min_delay or delay > max_delay:
        raise ValueError(f"delay_minutes 必须在 {min_delay} 到 {max_delay} 之间")

    repeat_value = bool(repeat)
    interval = int(interval_minutes or delay)
    if repeat_value and (interval < min_delay or interval > max_delay):
        raise ValueError(f"interval_minutes 必须在 {min_delay} 到 {max_delay} 之间")

    created = iso_now()
    scheduled = (now_local() + timedelta(minutes=delay)).isoformat()
    intent_id = uuid4().hex
    clean_constraints = [
        _shorten(item, max_length=120)
        for item in constraints or []
        if str(item or "").strip()
    ][:8]
    return AutonomyIntent(
        intent_id=intent_id,
        kind=kind_value,  # type: ignore[arg-type]
        motivation=motivation_text,
        delay_minutes=delay,
        scheduled_at=scheduled,
        target_hint=_shorten(target_hint, max_length=160),
        target_key=_shorten(target_key, max_length=80),
        target_stream_id=_shorten(target_stream_id, max_length=128),
        constraints=clean_constraints,
        created_at=created,
        updated_at=created,
        task_name=normalize_intent_task_name(intent_id),
        repeat=repeat_value,
        interval_minutes=interval if repeat_value else 0,
    )


def format_due_message(intent: AutonomyIntent) -> str:
    title = "周期性自主意向浮现" if intent.repeat else "自主意向浮现"
    source = (
        "这是 life_engine 之前自己留下的一个周期性意向；请像平时一样重新判断：现在是否还适合承接、是否要开口、如何开口，或者选择 pass_and_wait。"
        if intent.repeat
        else "这是 life_engine 之前自己留下的一个延迟意向；请像平时一样重新判断：现在是否还适合承接、是否要开口、如何开口，或者选择 pass_and_wait。"
    )
    lines = [
        f"[{title}] 这不是用户的新消息，也不是系统命令。",
        source,
        f"- 意向类型：{intent.kind}",
        f"- 当时的动机：{intent.motivation}",
        f"- 主观延迟：{intent.delay_minutes} 分钟",
    ]
    if intent.repeat:
        lines.append(f"- 周期：每隔 {intent.interval_minutes or intent.delay_minutes} 分钟浮现一次")
        lines.append(f"- 浮现次数：第 {max(1, intent.occurrence_count)} 次")
    if intent.target_hint:
        lines.append(f"- 目标提示：{intent.target_hint}")
    if intent.constraints:
        lines.append("- 约束：" + "；".join(intent.constraints))
    lines.append("重要：不要机械执行，不要为了主动而主动；如果上下文已经变化，保持沉默也是有效选择。")
    return "\n".join(lines)


async def schedule_autonomy_intent(plugin: Any, intent: AutonomyIntent) -> str:
    scheduler = get_unified_scheduler()

    async def _callback() -> None:
        service = getattr(plugin, "service", None)
        if service is None:
            logger.warning(f"自主意向到点但 life_engine 服务不可用: intent_id={intent.intent_id}")
            return
        await service.trigger_autonomy_intent(intent.intent_id)

    scheduled_at = parse_iso_datetime(intent.scheduled_at)
    if scheduled_at is None:
        raise ValueError("scheduled_at 无效")
    trigger_config: dict[str, Any] = {"trigger_at": scheduled_at.replace(tzinfo=None)}
    is_recurring = bool(intent.repeat)
    if is_recurring:
        trigger_config["interval_seconds"] = float(intent.interval_minutes or intent.delay_minutes) * 60.0
    schedule_id = await scheduler.create_schedule(
        callback=_callback,
        trigger_type=TriggerType.TIME,
        trigger_config=trigger_config,
        is_recurring=is_recurring,
        task_name=intent.task_name or normalize_intent_task_name(intent.intent_id),
        force_overwrite=True,
    )
    intent.schedule_id = schedule_id
    intent.updated_at = iso_now()
    return schedule_id


async def restore_autonomy_intents(plugin: Any, workspace_path: str | Path) -> int:
    store = AutonomyIntentStore(workspace_path)
    intents = store.list_scheduled()
    if not intents:
        return 0

    scheduler = get_unified_scheduler()
    if not scheduler.is_running:
        logger.warning("调度器尚未运行，未恢复自主意向")
        return 0

    restored = 0
    async with _get_lock():
        for intent in store.list_scheduled():
            scheduled_at = parse_iso_datetime(intent.scheduled_at)
            if scheduled_at is None:
                intent.status = "rejected"
                intent.rejected_reason = "scheduled_at 无效"
                intent.updated_at = iso_now()
                store.upsert(intent)
                continue
            if scheduled_at <= now_local():
                # Missed while offline: surface soon, but still through the same path.
                intent.scheduled_at = (now_local() + timedelta(seconds=5)).isoformat()
            try:
                await schedule_autonomy_intent(plugin, intent)
                store.upsert(intent)
                restored += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"恢复自主意向失败: intent_id={intent.intent_id} error={exc}")
    if restored:
        logger.info(f"已恢复自主意向调度: count={restored}")
    return restored


async def cleanup_autonomy_schedules(workspace_path: str | Path) -> int:
    store = AutonomyIntentStore(workspace_path)
    scheduler = get_unified_scheduler()
    removed = 0
    for intent in store.list_scheduled():
        if intent.schedule_id:
            try:
                if await scheduler.remove_schedule(intent.schedule_id):
                    removed += 1
                    continue
            except Exception:
                pass
        if intent.task_name:
            try:
                found = await scheduler.find_schedule_by_name(intent.task_name)
                if found and await scheduler.remove_schedule(found):
                    removed += 1
            except Exception:
                pass
    return removed


__all__ = [
    "AutonomyIntent",
    "AutonomyIntentKind",
    "AutonomyIntentStatus",
    "AutonomyIntentStore",
    "build_intent",
    "cleanup_autonomy_schedules",
    "format_due_message",
    "restore_autonomy_intents",
    "schedule_autonomy_intent",
]
