"""life_engine 屏幕观察工具测试。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from plugins.life_engine.core.config import LifeEngineConfig
from plugins.life_engine.tools import ALL_TOOLS, LifeEngineViewScreenTool
from plugins.life_engine.tools.screen_tools import CapturedScreen


def _make_plugin() -> SimpleNamespace:
    cfg = LifeEngineConfig()
    cfg.screen.enabled = True
    cfg.screen.native_task_name = "vlm"
    cfg.multimodal.enabled = True
    cfg.multimodal.native_image = False
    cfg.model.task_name = "life"
    return SimpleNamespace(config=cfg)


def _fake_capture() -> CapturedScreen:
    return CapturedScreen(
        base64_data=(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8"
            "/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
        ),
        width=1,
        height=1,
        image_format="png",
        captured_at="2026-05-03T16:00:00+08:00",
        method="test",
    )


@pytest.mark.asyncio
async def test_view_screen_uses_native_when_screen_native_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    from plugins.life_engine.tools import screen_tools

    plugin = _make_plugin()
    calls: list[str] = []

    async def fake_capture(_plugin: object) -> CapturedScreen:
        return _fake_capture()

    async def fake_analyze(**kwargs: object) -> str:
        calls.append(str(kwargs["model_task_name"]))
        return "屏幕上有一个测试窗口。"

    monkeypatch.setattr(screen_tools, "_capture_screen", fake_capture)
    monkeypatch.setattr(screen_tools, "_analyze_screenshot_with_model", fake_analyze)

    tool = LifeEngineViewScreenTool(plugin=plugin)  # type: ignore[arg-type]
    success, result = await tool.execute(focus="看看当前窗口")

    assert success is True
    assert isinstance(result, dict)
    assert result["mode"] == "native_image"
    assert result["observation"] == "屏幕上有一个测试窗口。"
    assert calls == ["vlm"]


@pytest.mark.asyncio
async def test_view_screen_falls_back_when_native_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    from plugins.life_engine.tools import screen_tools

    plugin = _make_plugin()
    plugin.config.screen.native_when_available = False
    calls: list[str] = []

    async def fake_capture(_plugin: object) -> CapturedScreen:
        return _fake_capture()

    async def fake_analyze(**kwargs: object) -> str:
        calls.append(str(kwargs["model_task_name"]))
        return "降级链路看到屏幕。"

    monkeypatch.setattr(screen_tools, "_capture_screen", fake_capture)
    monkeypatch.setattr(screen_tools, "_analyze_screenshot_with_model", fake_analyze)

    tool = LifeEngineViewScreenTool(plugin=plugin)  # type: ignore[arg-type]
    success, result = await tool.execute()

    assert success is True
    assert isinstance(result, dict)
    assert result["mode"] == "vlm_fallback"
    assert calls == ["vlm"]


@pytest.mark.asyncio
async def test_view_screen_rejects_when_disabled() -> None:
    plugin = _make_plugin()
    plugin.config.screen.enabled = False

    tool = LifeEngineViewScreenTool(plugin=plugin)  # type: ignore[arg-type]
    success, result = await tool.execute()

    assert success is False
    assert "未启用" in str(result)


def test_view_screen_registered_in_life_tools() -> None:
    assert LifeEngineViewScreenTool in ALL_TOOLS
    assert "life_chatter" in LifeEngineViewScreenTool.chatter_allow
    assert "life_engine_internal" in LifeEngineViewScreenTool.chatter_allow
