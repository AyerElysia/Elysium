"""Read-only decoder and scheduler cleanup for retired AutonomyIntent data.

The canonical writable authority is ``plugins.life_engine.proactive``.
AutonomyIntent remains solely so old snapshots can be inspected byte-for-byte
and obsolete scheduler entries can be removed; no class in this module may
create, update, or append live proactive state.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from src.kernel.scheduler import get_unified_scheduler

AutonomyIntentKind = Literal["speak", "reflect", "silence"]
AutonomyIntentStatus = Literal[
    "scheduled",
    "in_flight",
    "triggered",
    "renewal_required",
    "paused",
    "expired",
    "cancelled",
    "rejected",
    "failed",
]
_VALID_INTENT_KINDS = {"speak", "reflect", "silence"}
_VALID_INTENT_STATUSES = {
    "scheduled",
    "in_flight",
    "triggered",
    "renewal_required",
    "paused",
    "expired",
    "cancelled",
    "rejected",
    "failed",
}

_STORE_FILE = "autonomy_intents.json"
_STORE_VERSION = 3
_MAX_OCCURRENCES_LIMIT = 10_000
_MAX_LEASE_MINUTES = 7 * 24 * 60


class LegacyAutonomyReadOnly(RuntimeError):
    """Raised whenever retired code attempts to mutate legacy evidence."""


def _reject_legacy_mutation() -> None:
    raise LegacyAutonomyReadOnly(
        "LegacyAutonomyReadOnly: use the unified ProactiveAuthority"
    )


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
    """Decoded historical record from the retired autonomy archive."""

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
    max_occurrences: int = 0
    lease_until: str = ""
    active_occurrence_id: str = ""
    active_occurrence_status: str = ""
    active_occurrence_started_at: str = ""
    active_action_id: str = ""
    last_occurrence_id: str = ""
    last_outcome: str = ""
    renewal_reason: str = ""
    retry_count: int = 0
    last_error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AutonomyIntent":
        intent_id = str(data.get("intent_id") or "").strip()
        kind = str(data.get("kind") or "").strip()
        status = str(data.get("status") or "").strip()
        scheduled_at = str(data.get("scheduled_at") or "").strip()
        if not intent_id or not scheduled_at or "delay_minutes" not in data:
            raise ValueError("legacy autonomy identity/time fields are incomplete")
        if kind not in _VALID_INTENT_KINDS:
            raise ValueError("legacy autonomy kind is invalid")
        if status not in _VALID_INTENT_STATUSES:
            raise ValueError("legacy autonomy status is invalid")
        task_name = str(data.get("task_name") or normalize_intent_task_name(intent_id))
        delay_minutes = int(data.get("delay_minutes") or 1)
        repeat = bool(data.get("repeat") or data.get("recurring"))
        interval_minutes = int(data.get("interval_minutes") or 0)
        if repeat and interval_minutes <= 0:
            interval_minutes = delay_minutes
        constraints_raw = data.get("constraints") or []
        if not isinstance(constraints_raw, list):
            raise ValueError("legacy autonomy constraints must be a list")
        constraints = [
            _shorten(item, max_length=120)
            for item in constraints_raw
            if str(item or "").strip()
        ]
        return cls(
            intent_id=intent_id,
            kind=kind,  # type: ignore[arg-type]
            motivation=_shorten(data.get("motivation"), max_length=600),
            delay_minutes=delay_minutes,
            scheduled_at=scheduled_at,
            status=status,  # type: ignore[arg-type]
            target_hint=_shorten(data.get("target_hint"), max_length=160),
            target_key=_shorten(data.get("target_key"), max_length=80),
            target_stream_id=_shorten(data.get("target_stream_id"), max_length=128),
            constraints=constraints[:8],
            created_at=str(data.get("created_at") or scheduled_at),
            updated_at=str(data.get("updated_at") or scheduled_at),
            triggered_at=str(data.get("triggered_at") or ""),
            rejected_reason=_shorten(data.get("rejected_reason"), max_length=240),
            schedule_id=str(data.get("schedule_id") or ""),
            task_name=task_name,
            repeat=repeat,
            interval_minutes=interval_minutes,
            occurrence_count=max(0, int(data.get("occurrence_count") or 0)),
            max_occurrences=max(0, int(data.get("max_occurrences") or 0)),
            lease_until=str(data.get("lease_until") or ""),
            active_occurrence_id=str(data.get("active_occurrence_id") or ""),
            active_occurrence_status=str(data.get("active_occurrence_status") or ""),
            active_occurrence_started_at=str(data.get("active_occurrence_started_at") or ""),
            active_action_id=str(data.get("active_action_id") or ""),
            last_occurrence_id=str(data.get("last_occurrence_id") or ""),
            last_outcome=_shorten(data.get("last_outcome"), max_length=80),
            renewal_reason=_shorten(data.get("renewal_reason"), max_length=240),
            retry_count=max(0, int(data.get("retry_count") or 0)),
            last_error=_shorten(data.get("last_error"), max_length=240),
        )


class SelectedAutonomyIntentStore:
    """Read-only selected-backend view of the retired autonomy snapshot."""

    def __init__(self, runtime_store: Any) -> None:
        if runtime_store is None:
            raise RuntimeError("SelectedAutonomyStorageNotStarted")
        self.runtime_store = runtime_store
        self._revision = 0

    @staticmethod
    def _decode(payload: dict[str, Any]) -> list[AutonomyIntent]:
        if not isinstance(payload, dict):
            raise RuntimeError("AutonomyRemoteStateNotObject")
        version = int(payload.get("version") or 0)
        if version <= 0 or version > _STORE_VERSION:
            raise RuntimeError("AutonomyRemoteSchemaUnsupported")
        items = payload.get("intents")
        if not isinstance(items, list):
            raise RuntimeError("AutonomyRemoteIntentsNotList")
        if any(not isinstance(item, dict) for item in items):
            raise RuntimeError("AutonomyRemoteIntentNotObject")
        return [AutonomyIntent.from_dict(item) for item in items]

    async def load(self) -> list[AutonomyIntent]:
        record = await self.runtime_store.get_state(
            "life_autonomy.intents",
            "current",
        )
        if record is None:
            self._revision = 0
            return []
        self._revision = int(record.revision)
        return self._decode(record.payload)

    async def save(self, intents: list[AutonomyIntent]) -> None:
        del intents
        _reject_legacy_mutation()

    async def upsert(self, intent: AutonomyIntent) -> None:
        del intent
        _reject_legacy_mutation()

    async def get(self, intent_id: str) -> AutonomyIntent | None:
        target = str(intent_id or "").strip()
        if not target:
            return None
        for intent in await self.load():
            if intent.intent_id == target:
                return intent
        return None

    async def list_scheduled(self) -> list[AutonomyIntent]:
        return [
            intent for intent in await self.load()
            if intent.status == "scheduled"
        ]

    async def append_event(
        self,
        event_type: str,
        intent: AutonomyIntent,
        *,
        occurrence_id: str = "",
        action_id: str = "",
        detail: str = "",
    ) -> None:
        del event_type, intent, occurrence_id, action_id, detail
        _reject_legacy_mutation()


class AutonomyIntentStore:
    """Strict read-only JSON archive for old autonomy intents."""

    def __init__(self, workspace_path: str | Path) -> None:
        self.path = Path(workspace_path) / _STORE_FILE

    def load(self) -> list[AutonomyIntent]:
        if not self.path.exists():
            return []
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError("LegacyAutonomyArchiveUnreadable") from exc
        if not isinstance(raw, dict):
            raise RuntimeError("LegacyAutonomyArchiveNotObject")
        version = int(raw.get("version") or 0)
        if version <= 0 or version > _STORE_VERSION:
            raise RuntimeError("LegacyAutonomyArchiveSchemaUnsupported")
        items = raw.get("intents")
        if not isinstance(items, list):
            raise RuntimeError("LegacyAutonomyArchiveIntentsNotList")
        intents: list[AutonomyIntent] = []
        for item in items:
            if not isinstance(item, dict):
                raise RuntimeError("LegacyAutonomyArchiveIntentNotObject")
            try:
                intents.append(AutonomyIntent.from_dict(item))
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError("LegacyAutonomyArchiveIntentInvalid") from exc
        return intents

    def save(self, intents: list[AutonomyIntent]) -> None:
        del intents
        _reject_legacy_mutation()

    def upsert(self, intent: AutonomyIntent) -> None:
        del intent
        _reject_legacy_mutation()

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

    def append_event(
        self,
        event_type: str,
        intent: AutonomyIntent,
        *,
        occurrence_id: str = "",
        action_id: str = "",
        detail: str = "",
    ) -> None:
        del event_type, intent, occurrence_id, action_id, detail
        _reject_legacy_mutation()


class AsyncLocalAutonomyIntentStore:
    """Async wrapper used only when global storage explicitly selects local."""

    def __init__(self, workspace_path: str | Path) -> None:
        self.local = AutonomyIntentStore(workspace_path)

    async def load(self) -> list[AutonomyIntent]:
        return await asyncio.to_thread(self.local.load)

    async def save(self, intents: list[AutonomyIntent]) -> None:
        await asyncio.to_thread(self.local.save, intents)

    async def upsert(self, intent: AutonomyIntent) -> None:
        await asyncio.to_thread(self.local.upsert, intent)

    async def get(self, intent_id: str) -> AutonomyIntent | None:
        return await asyncio.to_thread(self.local.get, intent_id)

    async def list_scheduled(self) -> list[AutonomyIntent]:
        return await asyncio.to_thread(self.local.list_scheduled)

    async def append_event(
        self,
        event_type: str,
        intent: AutonomyIntent,
        *,
        occurrence_id: str = "",
        action_id: str = "",
        detail: str = "",
    ) -> None:
        await asyncio.to_thread(
            self.local.append_event,
            event_type,
            intent,
            occurrence_id=occurrence_id,
            action_id=action_id,
            detail=detail,
        )


def occurrence_id_for(intent: AutonomyIntent, occurrence_count: int | None = None) -> str:
    """Return the stable identity for one surfaced occurrence."""

    count = int(occurrence_count or intent.occurrence_count or 0)
    return f"{intent.intent_id}:{count}"


def recurring_lease_reason(
    intent: AutonomyIntent,
    *,
    at: datetime | None = None,
) -> str:
    """Return an engineering-only reason why recurrence may not execute."""

    if not intent.repeat:
        return ""
    if intent.max_occurrences <= 0 and not intent.lease_until:
        return "recurring intent has no explicit execution lease"
    if intent.max_occurrences > 0 and intent.occurrence_count >= intent.max_occurrences:
        return "maximum occurrence lease reached"
    if intent.lease_until:
        lease_until = parse_iso_datetime(intent.lease_until)
        if lease_until is None:
            return "lease_until is invalid"
        if lease_until <= (at or now_local()):
            return "time lease expired"
    return ""


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
    max_occurrences: int | None = None,
    lease_minutes: int | None = None,
) -> AutonomyIntent:
    """Build an in-memory legacy record for decoding/tests; never persist it."""

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

    occurrence_lease = int(max_occurrences or 0)
    time_lease_minutes = int(lease_minutes or 0)
    if repeat_value and occurrence_lease <= 0 and time_lease_minutes <= 0:
        raise ValueError(
            "repeat=true requires max_occurrences or lease_minutes; "
            "unbounded recurring intents are not allowed"
        )
    if occurrence_lease < 0 or occurrence_lease > _MAX_OCCURRENCES_LIMIT:
        raise ValueError(
            f"max_occurrences must be between 1 and {_MAX_OCCURRENCES_LIMIT}"
        )
    if time_lease_minutes < 0 or time_lease_minutes > _MAX_LEASE_MINUTES:
        raise ValueError(
            f"lease_minutes must be between 1 and {_MAX_LEASE_MINUTES}"
        )

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
        max_occurrences=occurrence_lease if repeat_value else 0,
        lease_until=(
            (now_local() + timedelta(minutes=time_lease_minutes)).isoformat()
            if repeat_value and time_lease_minutes > 0
            else ""
        ),
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
        if intent.max_occurrences > 0:
            lines.append(f"- 执行租约：最多 {intent.max_occurrences} 次")
        if intent.lease_until:
            lines.append(f"- 执行租约到：{intent.lease_until}")
    if intent.active_occurrence_id:
        lines.append(f"- 本次 occurrence：{intent.active_occurrence_id}")
    if intent.target_hint:
        lines.append(f"- 目标提示：{intent.target_hint}")
    if intent.constraints:
        lines.append("- 约束：" + "；".join(intent.constraints))
    lines.append("重要：不要机械执行，不要为了主动而主动；如果上下文已经变化，保持沉默也是有效选择。")
    return "\n".join(lines)


async def schedule_autonomy_intent(plugin: Any, intent: AutonomyIntent) -> str:
    """Reject the retired executor; callers may only read legacy snapshots."""

    del plugin, intent
    raise LegacyAutonomyReadOnly(
        "LegacyAutonomyReadOnly: stream-bound scheduling is retired"
    )


async def restore_autonomy_intents(
    plugin: Any,
    workspace_path: str | Path,
    *,
    store: Any | None = None,
) -> int:
    """Read the legacy ledger without scheduling or rewriting any row."""

    store = store or AsyncLocalAutonomyIntentStore(workspace_path)
    await store.load()
    return 0


async def cleanup_autonomy_schedules(
    workspace_path: str | Path,
    *,
    store: Any | None = None,
) -> int:
    store = store or AsyncLocalAutonomyIntentStore(workspace_path)
    scheduler = get_unified_scheduler()
    removed = 0
    for intent in await store.list_scheduled():
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
    "AsyncLocalAutonomyIntentStore",
    "LegacyAutonomyReadOnly",
    "SelectedAutonomyIntentStore",
    "build_intent",
    "cleanup_autonomy_schedules",
    "format_due_message",
    "occurrence_id_for",
    "recurring_lease_reason",
    "restore_autonomy_intents",
    "schedule_autonomy_intent",
]
