from __future__ import annotations

from types import SimpleNamespace

import pytest

from plugins.life_engine.core.compat_tools import LifeReportStateAction
from plugins.life_engine.service.world_state import SceneState, WorldState


@pytest.mark.asyncio
async def test_report_state_updates_the_active_voice_scene(monkeypatch: pytest.MonkeyPatch) -> None:
    world_state = WorldState(
        active_scenes={
            "voice_live_case": SceneState(
                scene_id="voice_live_case",
                kind="voice_live",
                display_name="实时通话意识",
            )
        }
    )
    saved: list[bool] = []
    service = SimpleNamespace(
        world_state=world_state,
        save_world_state=lambda: saved.append(True),
    )
    manager = SimpleNamespace(
        get_plugin=lambda name: SimpleNamespace(service=service)
        if name == "life_engine"
        else None
    )
    monkeypatch.setattr(
        "plugins.life_engine.core.compat_tools.get_plugin_manager",
        lambda: manager,
    )
    action = LifeReportStateAction(
        SimpleNamespace(stream_id="external_stream_hash", platform="voice_live"),
        SimpleNamespace(),
    )

    success, result = await action.execute(
        report="工具链真实验证成功",
        kind="scene",
        scene_id="voice_live_case",
    )

    assert success is True
    assert "工具链真实验证成功" in result
    assert world_state.active_scenes["voice_live_case"].status_summary == "工具链真实验证成功"
    assert world_state.active_scenes["voice_live_case"].last_active_at
    assert saved == [True]
