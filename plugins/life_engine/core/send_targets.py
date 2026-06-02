"""Runtime send target helpers for life_chatter."""

from __future__ import annotations

import time
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


async def list_recent_send_targets(
    *,
    current_stream_id: str = "",
    limit: int = 8,
    active_window_hours: float = 24.0,
) -> list[SendTarget]:
    try:
        from src.core.managers.stream_manager import get_stream_manager

        streams = list(getattr(get_stream_manager(), "_streams", {}).values())
    except Exception:
        return []

    now = time.time()
    cutoff = now - max(0.1, float(active_window_hours or 24.0)) * 3600.0
    targets: list[SendTarget] = []
    for stream in streams:
        target = await _stream_to_target(stream, current_stream_id=current_stream_id)
        if target is None:
            continue
        if not target.is_current and (
            not target.last_active_time or target.last_active_time < cutoff
        ):
            continue
        targets.append(target)

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
