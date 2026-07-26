"""kernel 层测试 fixtures。"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture
def mock_event_bus():
    """模拟事件总线。"""
    bus = MagicMock()
    bus.publish = AsyncMock()
    bus.subscribe = MagicMock()
    bus.unsubscribe = MagicMock()
    return bus


@pytest.fixture
def mock_config():
    """模拟全局配置。"""
    config = MagicMock()
    config.bot.log_level = "DEBUG"
    config.bot.master_users = ["master_id"]
    return config
