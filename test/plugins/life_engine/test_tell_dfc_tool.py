"""Retirement contract for the legacy recent-stream wake adapter."""

from __future__ import annotations

import pytest

from plugins.life_engine.tools import ALL_TOOLS
from plugins.life_engine.tools.file_tools import LifeEngineWakeDFCTool


@pytest.mark.asyncio
async def test_tell_dfc_direct_call_fails_closed_without_routing() -> None:
    tool = LifeEngineWakeDFCTool(plugin=object())

    ok, result = await tool.execute(
        message="subject-authored context",
        reason="historical direct call",
        stream_id="recent-stream-must-not-be-used",
        target_type="auto",
        target_user_name="nickname-must-not-be-resolved",
    )

    assert ok is False
    assert isinstance(result, str)
    assert result.startswith("LegacyRecentStreamWakeRetired:")
    assert "audience_ref" in result
    assert "surface_ref" in result


def test_tell_dfc_is_not_registered_for_runtime_use() -> None:
    assert LifeEngineWakeDFCTool not in ALL_TOOLS
    assert "nucleus_tell_dfc" not in {tool.tool_name for tool in ALL_TOOLS}
