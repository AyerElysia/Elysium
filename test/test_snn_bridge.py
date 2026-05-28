"""SNN 桥接层测试。

覆盖：特征提取、奖赏计算、prompt 格式化。
"""

import time
from dataclasses import dataclass
from enum import Enum
from unittest.mock import MagicMock

import numpy as np
import pytest

from plugins.life_engine.snn.bridge import SNNBridge, compute_reward, extract_features


# ── 模拟事件类 ──

class MockEventType(Enum):
    MESSAGE = "message"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    HEARTBEAT = "heartbeat"


@dataclass
class MockEvent:
    event_type: MockEventType
    timestamp: float = 0.0
    source: str = ""
    source_detail: str = ""
    content_type: str = ""
    content: str = ""
    tool_name: str = ""
    tool_success: bool = True


# ================================================================
# extract_features
# ================================================================


class TestExtractFeatures:
    """特征提取测试。"""

    def test_empty_events(self):
        """空事件列表应返回合理值。"""
        features = extract_features([], window_seconds=600)
        assert features.shape == (8,)
        assert np.all(np.isfinite(features))
        # 前 7 维应为 0（无事件 → tanh(0) = 0）
        np.testing.assert_array_almost_equal(features[:7], np.zeros(7))
        # x7 (silence): window=600s → silence_minutes=10 → factor=10/60=0.167 → mapped=-0.667
        assert -1.0 <= features[7] <= 1.0

    def test_inbound_message_feature(self):
        """入站消息应提升 x0。"""
        now = time.time()
        events = [
            MockEvent(MockEventType.MESSAGE, timestamp=now, source="user", source_detail="入站"),
            MockEvent(MockEventType.MESSAGE, timestamp=now, source="user", source_detail="入站"),
        ]
        features = extract_features(events, window_seconds=600)
        assert features[0] > 0.0, "入站消息应使 x0 > 0"

    def test_outbound_message_feature(self):
        """出站消息应提升 x1。"""
        now = time.time()
        events = [
            MockEvent(MockEventType.MESSAGE, timestamp=now, source="bot", source_detail="出站"),
        ]
        features = extract_features(events, window_seconds=600)
        assert features[1] > 0.0, "出站消息应使 x1 > 0"

    def test_tool_success_feature(self):
        """工具成功应提升 x2。"""
        now = time.time()
        events = [
            MockEvent(MockEventType.TOOL_RESULT, timestamp=now, tool_success=True, tool_name="nucleus_read_file"),
        ]
        features = extract_features(events, window_seconds=600)
        assert features[2] > 0.0, "工具成功应使 x2 > 0"

    def test_tool_fail_feature(self):
        """工具失败应提升 x3。"""
        now = time.time()
        events = [
            MockEvent(MockEventType.TOOL_RESULT, timestamp=now, tool_success=False, tool_name="nucleus_read_file"),
        ]
        features = extract_features(events, window_seconds=600)
        assert features[3] > 0.0, "工具失败应使 x3 > 0"

    def test_new_content_feature(self):
        """写文件应提升 x6。"""
        now = time.time()
        events = [
            MockEvent(MockEventType.TOOL_RESULT, timestamp=now, tool_success=True, tool_name="nucleus_write_file"),
        ]
        features = extract_features(events, window_seconds=600)
        assert features[6] > 0.0, "写文件应使 x6 > 0"

    def test_features_bounded(self):
        """所有特征应在 [-1, 1] 范围内。"""
        now = time.time()
        events = [
            MockEvent(MockEventType.MESSAGE, timestamp=now, source="user", source_detail="入站"),
        ] * 50  # 大量事件
        features = extract_features(events, window_seconds=600)
        assert np.all(features >= -1.0), "特征应 >= -1"
        assert np.all(features <= 1.0), "特征应 <= 1"

    def test_old_events_excluded(self):
        """窗口外的事件不应被计入。"""
        old_time = time.time() - 1200  # 20 分钟前
        events = [
            MockEvent(MockEventType.MESSAGE, timestamp=old_time, source="user", source_detail="入站"),
        ]
        features = extract_features(events, window_seconds=600)
        assert features[0] == pytest.approx(0.0, abs=0.01), "窗口外事件不应影响特征"


# ================================================================
# compute_reward
# ================================================================


class TestComputeReward:
    """奖赏计算测试。"""

    def test_tool_success_positive_reward(self):
        """工具成功应产生正奖赏。"""
        reward = compute_reward(tool_event_count=2, tool_success_count=1)
        assert reward > 0.0

    def test_idle_negative_reward(self):
        """连续空闲应产生负奖赏。"""
        reward = compute_reward(tool_event_count=0, idle_heartbeat_count=5)
        assert reward < 0.0

    def test_tool_fail_negative_reward(self):
        """工具失败应产生负奖赏。"""
        reward = compute_reward(tool_event_count=2, tool_fail_count=2)
        assert reward < 0.0

    def test_reward_bounded(self):
        """奖赏应在 [-1, 1] 范围内。"""
        reward = compute_reward(tool_event_count=100, tool_success_count=50, tool_fail_count=50, idle_heartbeat_count=100)
        assert -1.0 <= reward <= 1.0


# ================================================================
# SNNBridge
# ================================================================


class TestSNNBridge:
    """SNNBridge 测试。"""

    def _make_bridge(self):
        mock_service = MagicMock()
        return SNNBridge(mock_service)

    def test_format_drive_for_prompt(self):
        """prompt 格式化应生成中文描述。"""
        bridge = self._make_bridge()
        drives = {
            "arousal": "高",
            "valence": "中",
            "social_drive": "低",
            "task_drive": "高",
            "exploration_drive": "抑制",
            "rest_drive": "低",
        }
        text = bridge.format_drive_for_prompt(drives)
        assert "SNN快层" in text
        assert "激活高" in text
        assert "任务高" in text

    def test_format_drive_empty(self):
        """空字典应返回空字符串。"""
        bridge = self._make_bridge()
        assert bridge.format_drive_for_prompt({}) == ""

    def test_record_heartbeat_result(self):
        """记录心跳结果后 last_reward 应更新。"""
        bridge = self._make_bridge()
        reward = bridge.record_heartbeat_result(
            tool_event_count=4, tool_success_count=2, idle_count=0
        )
        assert bridge.get_last_reward() == reward
        assert isinstance(reward, float)

    def test_get_snapshot(self):
        """快照应包含 last_reward 和 last_features。"""
        bridge = self._make_bridge()
        snap = bridge.get_snapshot()
        assert "last_reward" in snap
        assert "last_features" in snap
        assert len(snap["last_features"]) == 8
