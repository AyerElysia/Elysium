import pytest

from plugins.life_engine.curiosity.engine import (
    CuriosityEngine,
    CuriositySignal,
    format_curiosity_signal,
)


def test_empty_signal_formats_to_empty_text():
    assert format_curiosity_signal(CuriositySignal.empty()) == ""


def test_active_signal_formats_as_non_command_suffix():
    text = format_curiosity_signal(
        CuriositySignal(
            active=True,
            anchor="反复问在不在",
            why="它不像普通问候，更像在确认关系连续性。",
            unknown="她真正想确认的是在线状态，还是被记得。",
            approach="如果你愿意，可以轻轻问一句她刚刚是不是在确认你还在。",
            tags=["关系牵引"],
        )
    )
    assert "### 好奇牵引" in text
    assert "不是命令" in text
    assert "反复问在不在" in text
    assert "关系牵引" in text


def test_parse_signal_repairs_json():
    signal = CuriosityEngine._parse_signal(
        """
        {
          active: true,
          anchor: "图片气质没闭合",
          why: "摘要说温柔，但用户在问是否适合电台封面",
          unknown: "关键可能是气质是否像爱莉",
          approach: "可以选择亲自看图",
          confidence: 0.8,
          tags: ["图片", "审美"]
        }
        """,
        source_event_id="evt1",
        source_stream_id="stream1",
    )
    assert signal.active is True
    assert signal.anchor == "图片气质没闭合"
    assert signal.source_event_id == "evt1"
    assert signal.source_stream_id == "stream1"
    assert signal.confidence == pytest.approx(0.8)
    assert signal.tags == ["图片", "审美"]


async def test_signal_persistence_roundtrip(tmp_path):
    engine = CuriosityEngine(workspace_path=str(tmp_path), model_task_name="life")
    signal = CuriositySignal(
        active=True,
        anchor="一句话没说透",
        why="表层是玩笑，底下像是在试探。",
        unknown="她想被接住还是想转移话题。",
        approach="先保留这个刺点。",
        confidence=0.7,
    )
    await engine.save_signal(signal)
    loaded = await engine.load_signal()
    assert loaded.active is True
    assert loaded.anchor == "一句话没说透"
    assert loaded.confidence == pytest.approx(0.7)

    await engine.clear()
    cleared = await engine.load_signal()
    assert cleared.active is False
