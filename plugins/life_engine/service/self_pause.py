"""life_engine 主动休息锁状态 helper。"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Any

from .event_builder import LifeEngineState

SELF_PAUSE_MINUTES_MIN = 5
SELF_PAUSE_MINUTES_MAX = 480


def parse_self_pause_until(state: LifeEngineState) -> datetime | None:
    """解析主动休息锁的结束时间。"""
    raw = state.self_pause_until
    if not raw:
        return None
    try:
        paused_until = datetime.fromisoformat(raw)
    except Exception:
        return None
    if paused_until.tzinfo is None:
        paused_until = paused_until.replace(tzinfo=timezone.utc).astimezone()
    return paused_until


def self_pause_status(
    state: LifeEngineState,
) -> tuple[bool, int | None, str | None, str | None]:
    """返回主动休息锁状态。"""
    paused_until = parse_self_pause_until(state)
    if paused_until is None:
        return False, None, None, state.self_pause_reason

    now = datetime.now(paused_until.tzinfo or timezone.utc)
    remaining_seconds = (paused_until - now).total_seconds()
    if remaining_seconds <= 0:
        return False, 0, paused_until.isoformat(), state.self_pause_reason

    remaining_minutes = max(1, int((remaining_seconds + 59) // 60))
    return (
        True,
        remaining_minutes,
        paused_until.isoformat(),
        state.self_pause_reason,
    )


def build_self_pause_status(
    state: LifeEngineState,
    status: tuple[bool, int | None, str | None, str | None],
) -> dict[str, Any]:
    """构建主动休息锁状态 payload。"""
    paused, remaining_minutes, paused_until, reason = status
    return {
        "paused": paused,
        "remaining_minutes": remaining_minutes,
        "paused_until": paused_until,
        "reason": reason,
        "started_at": state.self_pause_started_at,
        "duration_minutes": state.self_pause_duration_minutes,
        "will_wake_on_external_message": True,
    }


def clear_self_pause_state(state: LifeEngineState) -> bool:
    """清除主动休息锁。返回是否有变化。"""
    changed = any(
        (
            state.self_pause_until,
            state.self_pause_started_at,
            state.self_pause_reason,
            state.self_pause_duration_minutes,
        )
    )
    if changed:
        state.self_pause_until = None
        state.self_pause_started_at = None
        state.self_pause_reason = None
        state.self_pause_duration_minutes = 0
        # 连续休息计数在此不清零，由外部决定何时重置
    return changed


def apply_self_pause(
    state: LifeEngineState,
    *,
    duration_minutes: int,
    reason: str = "",
) -> dict[str, Any]:
    """设置主动休息锁并返回对外 payload。"""
    requested_minutes = int(duration_minutes or 0)
    clamped_minutes = max(
        SELF_PAUSE_MINUTES_MIN,
        min(SELF_PAUSE_MINUTES_MAX, requested_minutes),
    )
    started_at = datetime.now(timezone.utc).astimezone()
    paused_until = started_at + timedelta(minutes=clamped_minutes)
    cleaned_reason = " ".join(str(reason or "").split())

    state.self_pause_started_at = started_at.isoformat()
    state.self_pause_until = paused_until.isoformat()
    state.self_pause_reason = cleaned_reason
    state.self_pause_duration_minutes = clamped_minutes
    
    # 递增连续休息计数
    state.consecutive_rest_count += 1

    return {
        "paused": True,
        "duration_minutes": clamped_minutes,
        "requested_minutes": requested_minutes,
        "paused_until": paused_until.isoformat(),
        "reason": cleaned_reason,
        "consecutive_count": state.consecutive_rest_count,
        "min_minutes": SELF_PAUSE_MINUTES_MIN,
        "max_minutes": SELF_PAUSE_MINUTES_MAX,
        "will_wake_on_external_message": True,
    }
