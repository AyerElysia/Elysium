from __future__ import annotations

from types import SimpleNamespace

import pytest

from plugins.life_engine.core.compat_tools import LifeReportStateAction


@pytest.mark.asyncio
async def test_report_state_updates_the_active_voice_scene(monkeypatch: pytest.MonkeyPatch) -> None:
    reports: list[dict[str, object]] = []

    async def report_world_observation(
        report: str,
        **kwargs: object,
    ) -> dict[str, str]:
        reports.append({"report": report, **kwargs})
        return {
            "assertion_id": "assertion-test",
            "source_instance_id": "voice-live-test",
        }

    service = SimpleNamespace(
        resolve_consciousness_instance=lambda stream_id: "voice-live-test",
        report_world_observation=report_world_observation,
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
    assert "assertion-test" in result
    assert "不是 MEMORY.md 主体文档提交" in result
    assert reports == [
        {
            "report": "工具链真实验证成功",
            "source_instance_id": "voice-live-test",
            "subject": "voice_live_case",
            "predicate": "state_report",
            "domain": "scene",
            "stream_id": "external_stream_hash",
        }
    ]
