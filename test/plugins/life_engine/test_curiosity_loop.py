"""好奇心回路（阶段一：疏通管道）测试。

覆盖三段管道：
1. 调质脉冲通道（apply_pulse）——满足回路的奖赏端
2. 好奇牵引注入心跳 prompt——刺点可见性
3. 思考流承接刺点 + 推进/闭合的满足反馈——回路闭合
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from plugins.life_engine.core.config import LifeEngineConfig
from plugins.life_engine.curiosity.engine import CuriosityEngine, CuriositySignal
from plugins.life_engine.neuromod.engine import InnerStateEngine, ModulatorSystem
from plugins.life_engine.prompts.sections import CuriositySection, SectionContext
from plugins.life_engine.service import LifeEngineService
from plugins.life_engine.streams.manager import ThoughtStreamManager
from plugins.life_engine.streams.tools import LifeEngineManageThoughtStreamTool


@dataclass
class _DummyPlugin:
    config: LifeEngineConfig


def _make_config(tmp_path: Path) -> LifeEngineConfig:
    cfg = LifeEngineConfig()
    cfg.settings.enabled = True
    cfg.settings.workspace_path = str(tmp_path)
    return cfg


def _make_service(tmp_path: Path) -> LifeEngineService:
    return LifeEngineService(_DummyPlugin(config=_make_config(tmp_path)))


# ── 1. 调质脉冲通道 ─────────────────────────────────────────


def test_modulator_apply_pulse_adjusts_and_clamps() -> None:
    system = ModulatorSystem()
    mod = system.get("contentment")
    assert mod is not None
    mod.value = 0.5

    assert system.apply_pulse("contentment", 0.1) == pytest.approx(0.6)
    assert system.apply_pulse("contentment", 1.0) == 1.0
    assert system.apply_pulse("contentment", -5.0) == 0.0


def test_modulator_apply_pulse_unknown_name_returns_none() -> None:
    assert ModulatorSystem().apply_pulse("nonexistent", 0.1) is None


def test_inner_state_apply_pulse_batch() -> None:
    engine = InnerStateEngine()
    engine.modulators.get("contentment").value = 0.5
    engine.modulators.get("curiosity").value = 0.5

    engine.apply_pulse(
        {"contentment": 0.04, "curiosity": 0.02, "nonexistent": 0.5},
        reason="test",
    )

    assert engine.modulators.get("contentment").value == pytest.approx(0.54)
    assert engine.modulators.get("curiosity").value == pytest.approx(0.52)


# ── 2. 好奇牵引注入心跳 prompt ──────────────────────────────


def _active_signal() -> CuriositySignal:
    return CuriositySignal(
        active=True,
        anchor="反复出现的旋律",
        why="同一段旋律在不同对话里出现了三次",
        unknown="它对说话的人意味着什么",
        approach="可以回忆相关记忆，或下次轻轻问一句",
        confidence=0.7,
    )


def _section_ctx(service: LifeEngineService) -> SectionContext:
    return SectionContext(
        service=service,
        config=service.plugin.config,
        today_str="2026-06-11",
    )


def test_curiosity_section_contains_signal_and_guidance(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    engine = CuriosityEngine(workspace_path=str(tmp_path))
    asyncio.run(engine.save_signal(_active_signal()))

    text = asyncio.run(CuriositySection().render(_section_ctx(service)))

    assert text is not None
    assert "### 好奇牵引" in text
    assert "反复出现的旋律" in text
    assert "absorb_curiosity" in text
    assert "放下它也很好" in text


def test_curiosity_section_empty_without_signal(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    assert asyncio.run(CuriositySection().render(_section_ctx(service))) is None


def test_curiosity_section_respects_config_switch(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    service.plugin.config.curiosity.inject_to_heartbeat = False
    assert CuriositySection().enabled(_section_ctx(service)) is False


def test_render_heartbeat_sections_includes_curiosity(tmp_path: Path) -> None:
    """全链路：心跳渲染入口能产出好奇牵引段落。"""
    service = _make_service(tmp_path)
    engine = CuriosityEngine(workspace_path=str(tmp_path))
    asyncio.run(engine.save_signal(_active_signal()))

    texts = asyncio.run(service._render_heartbeat_sections())

    assert any("反复出现的旋律" in text for text in texts)


def test_heartbeat_model_prompt_injects_section_texts(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    prompt = service._build_heartbeat_model_prompt(
        "",
        section_texts=["### 好奇牵引\n- 刺点：测试刺点"],
    )
    assert "测试刺点" in prompt

    prompt_without = service._build_heartbeat_model_prompt("")
    assert "测试刺点" not in prompt_without


# ── 3. 思考流承接刺点 + 满足反馈 ─────────────────────────────


def _make_tool_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[LifeEngineManageThoughtStreamTool, SimpleNamespace]:
    manager = ThoughtStreamManager(workspace_path=str(tmp_path))
    curiosity_engine = CuriosityEngine(workspace_path=str(tmp_path))
    fake_service = SimpleNamespace(
        _thought_manager=manager,
        _inner_state=InnerStateEngine(),
        _get_curiosity_engine=lambda: curiosity_engine,
    )
    monkeypatch.setattr(
        "plugins.life_engine.streams.tools._get_service",
        lambda: fake_service,
    )
    tool = LifeEngineManageThoughtStreamTool(plugin=_DummyPlugin(config=_make_config(tmp_path)))
    return tool, fake_service


def test_create_with_absorb_curiosity_clears_signal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tool, service = _make_tool_env(tmp_path, monkeypatch)
    engine = service._get_curiosity_engine()
    asyncio.run(engine.save_signal(_active_signal()))

    ok, msg = asyncio.run(
        tool.execute(action="create", title="那段旋律", absorb_curiosity=True)
    )

    assert ok
    assert "承接" in msg
    assert asyncio.run(engine.load_signal()).active is False


def test_create_without_absorb_keeps_signal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tool, service = _make_tool_env(tmp_path, monkeypatch)
    engine = service._get_curiosity_engine()
    asyncio.run(engine.save_signal(_active_signal()))

    ok, _ = asyncio.run(tool.execute(action="create", title="别的事"))

    assert ok
    assert asyncio.run(engine.load_signal()).active is True


def test_advance_applies_satisfaction_pulse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tool, service = _make_tool_env(tmp_path, monkeypatch)
    service._inner_state.modulators.get("contentment").value = 0.5
    service._inner_state.modulators.get("curiosity").value = 0.5
    ts = service._thought_manager.create(title="那段旋律")

    ok, _ = asyncio.run(
        tool.execute(action="advance", stream_id=ts.id, thought="想到一个线索")
    )

    assert ok
    assert service._inner_state.modulators.get("contentment").value == pytest.approx(0.54)
    assert service._inner_state.modulators.get("curiosity").value == pytest.approx(0.52)


def test_retire_completed_with_conclusion_gives_stronger_pulse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tool, service = _make_tool_env(tmp_path, monkeypatch)
    service._inner_state.modulators.get("contentment").value = 0.5
    ts = service._thought_manager.create(title="那段旋律")

    ok, _ = asyncio.run(
        tool.execute(
            action="retire",
            stream_id=ts.id,
            new_status="completed",
            conclusion="原来是她小时候的童谣",
        )
    )

    assert ok
    assert service._inner_state.modulators.get("contentment").value == pytest.approx(0.60)


def test_retire_dormant_gives_no_pulse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tool, service = _make_tool_env(tmp_path, monkeypatch)
    service._inner_state.modulators.get("contentment").value = 0.5
    ts = service._thought_manager.create(title="那段旋律")

    ok, _ = asyncio.run(
        tool.execute(action="retire", stream_id=ts.id, new_status="dormant")
    )

    assert ok
    assert service._inner_state.modulators.get("contentment").value == pytest.approx(0.5)
