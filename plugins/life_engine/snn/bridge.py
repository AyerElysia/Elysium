"""SNN 桥接层 v2：事件流 ↔ SNN 驱动核 ↔ 调质层。

职责：
- 特征提取：从事件列表中提取 8 维归一化 SNN 输入
- 奖赏计算：从心跳结果中计算 STDP 奖赏信号
- 事件统计：为调质层提供事件级统计量
- 状态格式化：将 SNN + 调质综合状态格式化为 prompt 文本
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any, TYPE_CHECKING

import numpy as np

from ..service.event_builder import is_life_heartbeat_event

if TYPE_CHECKING:
    from ..service import LifeEngineService

logger = logging.getLogger("life_engine.snn")


def _event_time_seconds(event: Any) -> float:
    """从事件中提取时间戳（秒）。"""
    ts = getattr(event, "timestamp", None)
    if ts is None:
        return 0.0
    if isinstance(ts, (int, float)):
        return float(ts)
    if isinstance(ts, str):
        try:
            dt = datetime.fromisoformat(ts)
            return dt.timestamp()
        except (ValueError, TypeError):
            return 0.0
    return 0.0


def extract_features(
    events: list[Any],
    window_seconds: float = 600.0,
) -> np.ndarray:
    """从最近的事件中提取 SNN 输入特征（8 维，[-1, 1]）。"""
    now = time.time()
    cutoff = now - window_seconds

    recent: list[Any] = []
    for e in events:
        t = _event_time_seconds(e)
        if t > cutoff or t == 0.0:
            recent.append(e)

    msg_in = 0
    msg_out = 0
    tool_success = 0
    tool_fail = 0
    idle_beats = 0
    tell_dfc_count = 0
    new_content = 0
    last_external_time = 0.0

    for e in recent:
        etype = getattr(e, "event_type", None)
        etype_val = getattr(etype, "value", str(etype)) if etype else ""

        if etype_val == "message":
            source_detail = str(getattr(e, "source_detail", "") or "")
            content_type = str(getattr(e, "content_type", "") or "")
            source = str(getattr(e, "source", "") or "")

            if "入站" in source_detail and source != "life_engine":
                msg_in += 1
                t = _event_time_seconds(e)
                if t > last_external_time:
                    last_external_time = t
            elif "出站" in source_detail:
                msg_out += 1
            elif content_type == "dfc_message":
                msg_in += 1
            elif content_type == "direct_message":
                msg_in += 1
                t = _event_time_seconds(e)
                if t > last_external_time:
                    last_external_time = t

        elif etype_val == "tool_result":
            success = getattr(e, "tool_success", None)
            tool_name = str(getattr(e, "tool_name", "") or "")
            if success:
                tool_success += 1
                if tool_name in ("nucleus_write_file", "nucleus_manage_todo", "nucleus_relate_file"):
                    new_content += 1
            else:
                tool_fail += 1
            if tool_name == "nucleus_tell_dfc":
                tell_dfc_count += 1

        elif etype_val == "tool_call":
            tool_name = str(getattr(e, "tool_name", "") or "")
            if tool_name == "nucleus_tell_dfc":
                tell_dfc_count += 1

        elif etype_val == "heartbeat" and is_life_heartbeat_event(e):
            content = str(getattr(e, "content", "") or "")
            if "安静" in content or "持续感受" in content or "等待" in content:
                idle_beats += 1

    if last_external_time > 0:
        silence_minutes = (now - last_external_time) / 60.0
    else:
        silence_minutes = window_seconds / 60.0

    silence_factor = min(silence_minutes / 60.0, 1.0)

    raw = np.array(
        [msg_in, msg_out, tool_success, tool_fail,
         idle_beats, tell_dfc_count, new_content, silence_factor],
        dtype=np.float64,
    )
    raw[:7] = np.tanh(raw[:7] / 3.0)
    raw[7] = raw[7] * 2.0 - 1.0
    return raw


def extract_event_stats(
    events: list[Any],
    window_seconds: float = 600.0,
) -> dict[str, Any]:
    """为调质层提取事件统计量。"""
    now = time.time()
    cutoff = now - window_seconds

    msg_in = 0
    msg_out = 0
    tool_success = 0
    tool_fail = 0
    idle_beats = 0
    web_search_count = 0
    last_external_time = 0.0

    for e in events:
        t = _event_time_seconds(e)
        if t < cutoff and t > 0:
            continue

        etype = getattr(e, "event_type", None)
        etype_val = getattr(etype, "value", str(etype)) if etype else ""

        if etype_val == "message":
            source_detail = str(getattr(e, "source_detail", "") or "")
            source = str(getattr(e, "source", "") or "")
            if "入站" in source_detail and source != "life_engine":
                msg_in += 1
                t = _event_time_seconds(e)
                if t > last_external_time:
                    last_external_time = t
            elif "出站" in source_detail:
                msg_out += 1

        elif etype_val == "tool_result":
            tool_name = str(getattr(e, "tool_name", "") or "")
            if getattr(e, "tool_success", False):
                tool_success += 1
            else:
                tool_fail += 1
            if "web_search" in tool_name:
                web_search_count += 1

        elif etype_val == "heartbeat" and is_life_heartbeat_event(e):
            content = str(getattr(e, "content", "") or "")
            if "安静" in content or "持续感受" in content or "等待" in content:
                idle_beats += 1

    silence_minutes = (now - last_external_time) / 60.0 if last_external_time > 0 else window_seconds / 60.0

    return {
        "msg_in": msg_in,
        "msg_out": msg_out,
        "tool_success": tool_success,
        "tool_fail": tool_fail,
        "idle_beats": idle_beats,
        "web_search_count": web_search_count,
        "silence_minutes": silence_minutes,
    }


def compute_reward(
    tool_event_count: int = 0,
    tool_success_count: int = 0,
    tool_fail_count: int = 0,
    idle_heartbeat_count: int = 0,
    had_text_reply: bool = True,
) -> float:
    """从心跳结果计算 STDP 奖赏信号。"""
    reward = 0.0
    tool_calls = tool_event_count // 2 if tool_event_count > 0 else 0

    if tool_calls > 0:
        reward += 0.3
    if tool_success_count > 0:
        reward += min(tool_success_count * 0.15, 0.4)

    if tool_calls == 0:
        reward -= 0.2
    if tool_fail_count > 0:
        reward -= min(tool_fail_count * 0.2, 0.4)
    if idle_heartbeat_count >= 5:
        reward -= 0.3
    elif idle_heartbeat_count >= 2:
        reward -= 0.15

    return float(np.clip(reward, -1.0, 1.0))


class SNNBridge:
    """SNN 桥接层 v2。"""

    def __init__(self, service: LifeEngineService) -> None:
        self._service = service
        self._last_reward: float = 0.0
        self._last_features: np.ndarray = np.zeros(8, dtype=np.float64)
        self._last_event_stats: dict[str, Any] = {}

    def extract_features_from_events(
        self,
        events: list[Any],
        window_seconds: float = 600.0,
    ) -> np.ndarray:
        features = extract_features(events, window_seconds=window_seconds)
        self._last_features = features.copy()
        # 同时提取事件统计供调质层使用
        self._last_event_stats = extract_event_stats(events, window_seconds=window_seconds)
        return features

    def get_last_event_stats(self) -> dict[str, Any]:
        return self._last_event_stats.copy()

    def get_last_reward(self) -> float:
        return self._last_reward

    def record_heartbeat_result(
        self,
        tool_event_count: int = 0,
        tool_success_count: int = 0,
        tool_fail_count: int = 0,
        idle_count: int = 0,
        had_text_reply: bool = True,
    ) -> float:
        self._last_reward = compute_reward(
            tool_event_count=tool_event_count,
            tool_success_count=tool_success_count,
            tool_fail_count=tool_fail_count,
            idle_heartbeat_count=idle_count,
            had_text_reply=had_text_reply,
        )
        return self._last_reward

    def format_drive_for_prompt(self, drive_discrete: dict[str, str]) -> str:
        """将 SNN 离散驱动格式化为 prompt 文本。"""
        if not drive_discrete:
            return ""

        name_map = {
            "arousal": "激活",
            "valence": "情绪",
            "social_drive": "社交",
            "task_drive": "任务",
            "exploration_drive": "探索",
            "rest_drive": "休息",
        }

        parts = []
        for key, level in drive_discrete.items():
            cn_name = name_map.get(key, key)
            parts.append(f"{cn_name}{level}")

        return "【SNN快层】" + "、".join(parts)

    def get_snapshot(self) -> dict[str, Any]:
        return {
            "last_reward": round(self._last_reward, 4),
            "last_features": self._last_features.tolist(),
            "last_event_stats": self._last_event_stats,
        }
