"""测试 Life State 集成到 DFC 的功能。"""

from unittest.mock import MagicMock

from plugins.life_engine.service import LifeEngineService, LifeEngineEvent, EventType
from src.core.config.core_config import CoreConfig


def _make_plugin() -> MagicMock:
    plugin = MagicMock()
    plugin.config = MagicMock()
    plugin.config.settings = MagicMock()
    plugin.config.settings.enabled = True
    plugin.config.settings.context_history_max_events = 300
    plugin.global_storage_config = CoreConfig(
        storage=CoreConfig.StorageSection(backend="local")
    )
    return plugin


class TestLifeStateIntegration:
    """测试 Life State 集成功能。"""

    async def test_get_state_digest_for_dfc_empty(self):
        """测试空状态时返回空字符串。"""
        # 创建 mock plugin
        mock_plugin = _make_plugin()

        service = LifeEngineService(mock_plugin)
        service._event_history = []
        service._inner_state = None

        result = await service.get_state_digest_for_dfc()
        assert result == ""

    async def test_get_state_digest_for_dfc_with_heartbeats(self):
        """测试包含心跳独白的状态摘要。"""
        mock_plugin = _make_plugin()

        service = LifeEngineService(mock_plugin)

        # 添加心跳事件
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).astimezone().isoformat()

        service._event_history = [
            LifeEngineEvent(
                event_id="1",
                event_type=EventType.HEARTBEAT,
                timestamp=now,
                sequence=1,
                source="life_engine",
                source_detail="heartbeat",
                content="思考了一下缓存优化的问题",
                heartbeat_index=1,
            ),
            LifeEngineEvent(
                event_id="2",
                event_type=EventType.HEARTBEAT,
                timestamp=now,
                sequence=2,
                source="life_engine",
                source_detail="heartbeat",
                content="想起了上次讨论的 Prompt Caching 机制",
                heartbeat_index=2,
            ),
        ]
        service._inner_state = None

        result = await service.get_state_digest_for_dfc()

        assert "【最近思考】" in result
        assert "缓存优化" in result or "Prompt Caching" in result

    async def test_get_state_digest_for_dfc_with_tool_calls(self):
        """测试包含工具调用的状态摘要。"""
        mock_plugin = _make_plugin()

        service = LifeEngineService(mock_plugin)

        # 添加工具调用事件
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).astimezone().isoformat()

        service._event_history = [
            LifeEngineEvent(
                event_id="1",
                event_type=EventType.TOOL_CALL,
                timestamp=now,
                sequence=1,
                source="life_engine",
                source_detail="tool_call",
                content="",
                tool_name="nucleus_read_file",
            ),
            LifeEngineEvent(
                event_id="2",
                event_type=EventType.TOOL_CALL,
                timestamp=now,
                sequence=2,
                source="life_engine",
                source_detail="tool_call",
                content="",
                tool_name="nucleus_search_memory",
            ),
            LifeEngineEvent(
                event_id="3",
                event_type=EventType.TOOL_CALL,
                timestamp=now,
                sequence=3,
                source="life_engine",
                source_detail="tool_call",
                content="",
                tool_name="nucleus_read_file",
            ),
        ]
        service._inner_state = None

        result = await service.get_state_digest_for_dfc()

        assert "【工具偏好】" in result
        assert "read_file" in result

    async def test_get_state_digest_length_control(self):
        """测试状态摘要长度控制在合理范围内。"""
        mock_plugin = _make_plugin()

        service = LifeEngineService(mock_plugin)

        # 添加大量事件
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).astimezone().isoformat()

        events = []
        for i in range(100):
            events.append(
                LifeEngineEvent(
                    event_id=str(i),
                    event_type=EventType.HEARTBEAT,
                    timestamp=now,
                    sequence=i,
                    source="life_engine",
                    source_detail="heartbeat",
                    content=f"这是第 {i} 条心跳独白，包含一些思考内容" * 10,  # 很长的内容
                    heartbeat_index=i,
                )
            )

        service._event_history = events
        service._inner_state = None

        result = await service.get_state_digest_for_dfc()

        # 验证长度控制（应该在 150-250 tokens，约 300-500 字符）
        assert len(result) < 800, f"状态摘要过长: {len(result)} chars"
