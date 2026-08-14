"""Runtime send target helpers for life_chatter."""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class SendTarget:
    target_key: str
    stream_id: str
    platform: str
    chat_type: str
    display_name: str
    group_id: str = ""
    group_name: str = ""
    target_user_id: str = ""
    target_user_name: str = ""
    is_current: bool = False
    last_active_time: float = 0.0


def _target_prefix(chat_type: str) -> str:
    return "g" if str(chat_type or "").lower() == "group" else "p"


def _unique_target_keys(targets: list[SendTarget]) -> None:
    by_stream = {target.stream_id: target for target in targets}
    for target in targets:
        prefix = _target_prefix(target.chat_type)
        for length in range(8, min(len(target.stream_id), 32) + 1):
            candidate = f"{prefix}-{target.stream_id[:length]}"
            conflicts = [
                item
                for item in by_stream.values()
                if _target_prefix(item.chat_type) == prefix
                and item.stream_id.startswith(target.stream_id[:length])
            ]
            if len(conflicts) == 1:
                target.target_key = candidate
                break
        if not target.target_key:
            target.target_key = f"{prefix}-{target.stream_id}"


async def _private_user_for_person_id(platform: str, person_id: str) -> tuple[str, str]:
    if not person_id:
        return "", ""
    try:
        from src.core.utils.user_query_helper import get_user_query_helper

        person = await get_user_query_helper().person_crud.get_by(person_id=person_id)
        if not person:
            return "", ""
        return (
            str(getattr(person, "user_id", "") or ""),
            str(getattr(person, "nickname", "") or ""),
        )
    except Exception:
        return "", ""


async def _stream_to_target(stream: Any, *, current_stream_id: str) -> SendTarget | None:
    stream_id = str(getattr(stream, "stream_id", "") or "").strip()
    platform = str(getattr(stream, "platform", "") or "").strip()
    chat_type = str(getattr(stream, "chat_type", "") or "").strip().lower()
    if not stream_id or not platform or chat_type not in {"group", "private"}:
        return None

    try:
        from src.core.managers.stream_manager import get_stream_manager

        stream_info = await get_stream_manager().get_stream_info(stream_id)
    except Exception:
        stream_info = None

    if not isinstance(stream_info, dict):
        stream_info = {}

    last_active = float(
        getattr(stream, "last_active_time", 0.0)
        or stream_info.get("last_active_time")
        or 0.0
    )
    stream_name = str(getattr(stream, "stream_name", "") or "").strip()

    if chat_type == "group":
        group_id = str(stream_info.get("group_id") or "").strip()
        group_name = str(stream_info.get("group_name") or stream_name or "").strip()
        if not group_id:
            return None
        return SendTarget(
            target_key="",
            stream_id=stream_id,
            platform=platform,
            chat_type=chat_type,
            display_name=group_name or f"群聊 {group_id}",
            group_id=group_id,
            group_name=group_name,
            is_current=stream_id == current_stream_id,
            last_active_time=last_active,
        )

    person_id = str(stream_info.get("person_id") or "").strip()
    target_user_id, target_user_name = await _private_user_for_person_id(platform, person_id)
    if not target_user_id:
        return None
    display_name = target_user_name or stream_name or f"私聊 {target_user_id}"
    return SendTarget(
        target_key="",
        stream_id=stream_id,
        platform=platform,
        chat_type=chat_type,
        display_name=display_name,
        target_user_id=target_user_id,
        target_user_name=target_user_name or display_name,
        is_current=stream_id == current_stream_id,
        last_active_time=last_active,
    )


def _event_timestamp_epoch(event: Any) -> float:
    """LifeEvent.timestamp（ISO 字符串）→ epoch 秒；解析失败返回 0。"""
    raw = str(getattr(event, "timestamp", "") or "")
    if not raw:
        return 0.0
    try:
        from datetime import datetime, timezone

        normalized = raw.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except Exception:
        return 0.0


async def _recover_targets_from_event_ledger(
    *,
    limit: int = 8,
    active_window_hours: float = 24.0,
) -> list[SendTarget]:
    """从 Life Event 账本恢复最近活跃流（进程重启后内存流缺失时的兜底）。

    真实场景（2026-08-12）：重启后飞书流未重建（stream_manager 内存 _streams 为空），
    心跳「你可以触达的人和地方」只剩内存里活着的 ayla 流——想主动找飞书对话却
    找不到流。事件账本是权威历史，按 stream_id 恢复最近聊过的私聊流。
    """
    try:
        from ..service.registry import get_life_engine_service

        service = get_life_engine_service()
        if service is None:
            return []
        events = await service._get_life_event_store().read_tail(limit=500)
    except Exception:
        return []

    now = time.time()
    cutoff = now - max(0.1, float(active_window_hours or 24.0)) * 3600.0
    best_by_stream: dict[str, Any] = {}
    for event in events:
        stream_id = str(getattr(event, "stream_id", "") or "").strip()
        if not stream_id or stream_id.startswith("life_engine"):
            continue
        if str(getattr(event, "channel", "") or "") != "chat":
            continue
        event_ts = _event_timestamp_epoch(event)
        if event_ts <= 0.0 or event_ts < cutoff:
            continue
        prev = best_by_stream.get(stream_id)
        if prev is None or _event_timestamp_epoch(prev) < event_ts:
            best_by_stream[stream_id] = event

    targets: list[SendTarget] = []
    for stream_id, event in best_by_stream.items():
        metadata = getattr(event, "metadata", None) or {}
        chat_type = str(metadata.get("chat_type") or "").lower()
        platform = str(getattr(event, "source", "") or "").strip()
        if not platform or chat_type not in {"private", "group"}:
            continue
        event_ts = _event_timestamp_epoch(event)
        if chat_type == "private":
            person_id = str(metadata.get("sender_id") or "").strip()
            target_user_id, target_user_name = await _private_user_for_person_id(
                platform, person_id
            )
            if not target_user_id:
                continue
            display_name = target_user_name or f"私聊 {target_user_id}"
            targets.append(
                SendTarget(
                    target_key="",
                    stream_id=stream_id,
                    platform=platform,
                    chat_type=chat_type,
                    display_name=display_name,
                    target_user_id=target_user_id,
                    target_user_name=target_user_name,
                    last_active_time=event_ts,
                )
            )
        # 群聊：事件缺少 group_id/name 完整信息，恢复留给内存流路径（不硬造占位）
    targets.sort(key=lambda item: -float(item.last_active_time or 0.0))
    return targets[: max(1, int(limit or 8))]


async def _targets_from_stream_table(
) -> list[SendTarget]:
    """从 chat_streams 持久化表读取真实可触达流（platform 非空、真实流 ID）。

    权威数据源：chat_streams 表是"她注册过的所有真实会话"的持久化注册表，
    内存 stream_manager 重启后可能只重建部分流且 platform 为空，导致
    心跳「你可以触达的人和地方」漏掉真实可触达的流（2026-08-13：ayla/飞书
    汐汐流明明在表里且当天有消息，心跳却看不到）。
    """
    try:
        from sqlalchemy import select

        from src.core.models.sql_alchemy import ChatStreams
        from src.kernel.db import get_db_session

        async with get_db_session() as session:
            result = await session.execute(
                select(
                    ChatStreams.stream_id,
                    ChatStreams.platform,
                    ChatStreams.chat_type,
                    ChatStreams.person_id,
                    ChatStreams.group_id,
                    ChatStreams.group_name,
                    ChatStreams.last_active_time,
                ).where(
                    ChatStreams.platform.is_not(None),
                    ChatStreams.platform != "",
                    ChatStreams.chat_type.in_(["private", "group"]),
                )
            )
            rows = result.all()
    except Exception:
        return []

    targets: list[SendTarget] = []
    for row in rows:
        if hasattr(row, "_mapping"):
            mapping = row._mapping
        else:
            mapping = row
        _get = (
            (lambda key: mapping[key])
            if isinstance(mapping, Mapping)
            else (lambda key: getattr(mapping, key, None))
        )
        stream_id = str(_get("stream_id") or "").strip()
        platform = str(_get("platform") or "").strip()
        chat_type = str(_get("chat_type") or "").strip().lower()
        if not stream_id or stream_id == "chat_global" or not platform:
            continue
        person_id = str(_get("person_id") or "").strip()
        last_active = float(_get("last_active_time") or 0.0)
        if chat_type == "group":
            group_id = str(_get("group_id") or "").strip()
            group_name = str(_get("group_name") or "").strip()
            if not group_id:
                continue
            targets.append(
                SendTarget(
                    target_key="",
                    stream_id=stream_id,
                    platform=platform,
                    chat_type="group",
                    display_name=group_name or f"群聊 {group_id}",
                    group_id=group_id,
                    group_name=group_name,
                    last_active_time=last_active,
                )
            )
            continue
        target_user_id, target_user_name = await _private_user_for_person_id(
            platform, person_id
        )
        display_name = target_user_name or str(_get("group_name") or "") or f"私聊 {target_user_id or stream_id[:8]}"
        targets.append(
            SendTarget(
                target_key="",
                stream_id=stream_id,
                platform=platform,
                chat_type="private",
                display_name=display_name,
                target_user_id=target_user_id,
                target_user_name=target_user_name or display_name,
                last_active_time=last_active,
            )
        )
    return targets


async def list_recent_send_targets(
    *,
    current_stream_id: str = "",
    limit: int = 8,
    active_window_hours: float = 24.0,
) -> list[SendTarget]:
    # 权威主源：chat_streams 持久化表（platform 完整、真实流）。
    targets: list[SendTarget] = await _targets_from_stream_table()

    # 补充：内存 stream_manager 当前活跃流（表源缺失时兜底）。
    try:
        from src.core.managers.stream_manager import get_stream_manager

        streams = list(getattr(get_stream_manager(), "_streams", {}).values())
    except Exception:
        streams = []

    now = time.time()
    cutoff = now - max(0.1, float(active_window_hours or 24.0)) * 3600.0
    known_streams = {target.stream_id for target in targets}
    for stream in streams:
        stream_id = str(getattr(stream, "stream_id", "") or "").strip()
        if stream_id in known_streams:
            continue
        target = await _stream_to_target(stream, current_stream_id=current_stream_id)
        if target is None:
            continue
        if not target.is_current and (
            not target.last_active_time or target.last_active_time < cutoff
        ):
            continue
        targets.append(target)
        known_streams.add(target.stream_id)

    # 事件账本兜底：进程重启后内存流缺失的最近流（如飞书）从这里恢复，
    # 保证心跳「你可以触达的人和地方」不因重启而只剩单个流。
    recovered = await _recover_targets_from_event_ledger(
        limit=limit,
        active_window_hours=active_window_hours,
    )
    for recovered_target in recovered:
        if recovered_target.stream_id not in known_streams:
            targets.append(recovered_target)
            known_streams.add(recovered_target.stream_id)

    targets.sort(key=lambda item: (not item.is_current, -float(item.last_active_time or 0.0)))
    targets = targets[: max(1, int(limit or 8))]
    _unique_target_keys(targets)
    return targets


async def resolve_send_target_key(
    target_key: str,
    *,
    current_stream_id: str = "",
    limit: int = 8,
    active_window_hours: float = 24.0,
) -> SendTarget | None:
    key = str(target_key or "").strip()
    if not key:
        return None
    targets = await list_recent_send_targets(
        current_stream_id=current_stream_id,
        limit=limit,
        active_window_hours=active_window_hours,
    )
    for target in targets:
        if target.target_key == key:
            return target
    return None


def format_send_targets_for_prompt(targets: list[SendTarget]) -> str:
    if not targets:
        return ""

    lines = [
        "可选：`life_send_text.target_key` 可以把文字发到下面某个近期聊天；",
        "通常留空，表示按旧逻辑回复当前聊天。只有明确要跨聊天发送时才填写列表里的 target_key。",
    ]
    for target in targets:
        chat_label = "群聊" if target.chat_type == "group" else "私聊"
        current = " | 当前聊天" if target.is_current else ""
        lines.append(
            f"- target_key={target.target_key} | {target.platform}{chat_label} | "
            f"{target.display_name}{current}"
        )
    return "\n".join(lines)
