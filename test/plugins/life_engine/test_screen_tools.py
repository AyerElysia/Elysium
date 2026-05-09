"""life_engine 屏幕观察工具测试。"""

from __future__ import annotations

import io
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image as PILImage

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


# ---------------------------------------------------------------------------
# WSL 检测与黑图过滤测试
# ---------------------------------------------------------------------------

def test_is_wsl_returns_bool() -> None:
    from plugins.life_engine.tools.screen_tools import _is_wsl
    result = _is_wsl()
    assert isinstance(result, bool)


def test_is_blank_image_detects_black_image(tmp_path: Path) -> None:
    from plugins.life_engine.tools.screen_tools import _is_blank_image

    black_img = PILImage.new("RGB", (10, 10), color=(0, 0, 0))
    p = tmp_path / "black.png"
    black_img.save(p, format="PNG")
    assert _is_blank_image(p) is True


def test_is_blank_image_passes_normal_image(tmp_path: Path) -> None:
    from plugins.life_engine.tools.screen_tools import _is_blank_image

    normal_img = PILImage.new("RGB", (10, 10), color=(128, 64, 200))
    p = tmp_path / "normal.png"
    normal_img.save(p, format="PNG")
    assert _is_blank_image(p) is False


def test_is_blank_image_returns_true_for_missing_file(tmp_path: Path) -> None:
    from plugins.life_engine.tools.screen_tools import _is_blank_image

    assert _is_blank_image(tmp_path / "nonexistent.png") is True


@pytest.mark.asyncio
async def test_auto_mode_skips_blank_capture_and_tries_next(monkeypatch: pytest.MonkeyPatch) -> None:
    """auto 模式下，黑图捕获应被跳过，继续尝试下一个方法。"""
    from plugins.life_engine.tools import screen_tools

    plugin = _make_plugin()
    plugin.config.screen.capture_method = "auto"
    monkeypatch.setattr(screen_tools, "_is_wsl", lambda: False)

    call_order: list[str] = []
    normal_capture_done = False

    async def fake_ffmpeg(path: object, cfg: object) -> tuple[bool, str]:
        import tempfile, os
        from pathlib import Path as _Path
        call_order.append("ffmpeg")
        # ffmpeg 写入一个全黑图
        black = PILImage.new("RGB", (4, 4), color=(0, 0, 0))
        black.save(str(path), format="PNG")  # type: ignore[arg-type]
        return True, ""

    async def fake_grim(path: object, cfg: object) -> tuple[bool, str]:
        call_order.append("grim")
        normal = PILImage.new("RGB", (4, 4), color=(100, 100, 100))
        normal.save(str(path), format="PNG")  # type: ignore[arg-type]
        return True, ""

    async def fake_pil(path: object, cfg: object) -> tuple[bool, str]:
        call_order.append("pil")
        return False, "PIL 未测试"

    monkeypatch.setattr(screen_tools, "_capture_with_ffmpeg", fake_ffmpeg)
    monkeypatch.setattr(screen_tools, "_capture_with_grim", fake_grim)
    monkeypatch.setattr(screen_tools, "_capture_with_pil", fake_pil)

    captured = await screen_tools._capture_screen(plugin)
    assert captured.method == "grim"
    assert "ffmpeg" in call_order
    assert "grim" in call_order
    assert "pil" not in call_order


@pytest.mark.asyncio
async def test_wsl_auto_mode_tries_powershell_first(monkeypatch: pytest.MonkeyPatch) -> None:
    """WSL auto 模式下应最先尝试 powershell 截图方法。"""
    from plugins.life_engine.tools import screen_tools

    plugin = _make_plugin()
    plugin.config.screen.capture_method = "auto"
    monkeypatch.setattr(screen_tools, "_is_wsl", lambda: True)

    call_order: list[str] = []

    async def fake_powershell(path: object, cfg: object) -> tuple[bool, str]:
        call_order.append("powershell")
        normal = PILImage.new("RGB", (4, 4), color=(200, 100, 50))
        normal.save(str(path), format="PNG")  # type: ignore[arg-type]
        return True, ""

    async def fake_ffmpeg(path: object, cfg: object) -> tuple[bool, str]:
        call_order.append("ffmpeg")
        return False, "不应调用 ffmpeg"

    monkeypatch.setattr(screen_tools, "_capture_with_powershell", fake_powershell)
    monkeypatch.setattr(screen_tools, "_capture_with_ffmpeg", fake_ffmpeg)

    captured = await screen_tools._capture_screen(plugin)
    assert captured.method == "powershell"
    assert call_order[0] == "powershell"
    assert "ffmpeg" not in call_order
