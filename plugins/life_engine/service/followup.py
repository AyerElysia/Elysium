"""life_engine 延迟续话状态模型。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass
class PendingFollowup:
    """等待中的延迟续话任务。"""

    topic: str
    thought: str
    followup_type: str
    delay_seconds: float
    scheduled_at: datetime
    check_at: datetime
    source: str = "post_reply"


@dataclass
class FollowupState:
    stream_id: str
    pending_followup: PendingFollowup | None = None
    followup_chain_count: int = 0
    followup_cooldown_until: datetime | None = None
    next_check_time: datetime | None = None
    is_waiting: bool = False
    scheduler_task_name: str | None = None
    active_check_kind: str | None = None
