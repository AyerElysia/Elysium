"""沉淀器（阶段二）测试。

可言说法则：沉淀必须经过她的语言。
覆盖：
1. NarrativeStore 游标推进 / pending 过滤（narrative 自身不算素材）
2. quiet 回望同样推进游标，但不写自传正文
3. RiverReflectionSection 的到期判断（数量门槛 / 间隔 / 邀请冷却 / 配置开关）
4. nucleus_write_narrative 工具：空输入报错、写下叙事、安静回望，均入长河
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from plugins.life_engine.core.config import LifeEngineConfig
from plugins.life_engine.narrative.store import NarrativeStore
from plugins.life_engine.narrative.tools import LifeEngineWriteNarrativeTool
from plugins.life_engine.prompts.sections import RiverReflectionSection, SectionContext
from plugins.life_engine.trace.store import LifeTraceStore


@dataclass
class _DummyPlugin:
    config: LifeEngineConfig


def _make_config(tmp_path: Path) -> LifeEngineConfig:
    cfg = LifeEngineConfig()
    cfg.settings.enabled = True
    cfg.settings.workspace_path = str(tmp_path)
    return cfg


def _now() -> datetime:
    return datetime.now(timezone.utc).astimezone()


def _fill_river(tmp_path: Path, count: int = 3) -> LifeTraceStore:
    trace = LifeTraceStore(tmp_path)
    for index in range(count):
        trace.record_moment(
            kind="intent", summary=f"形成意向 {index}", operation="formed"
        )
    return trace


# ── 1. NarrativeStore ───────────────────────────────────────


def test_pending_respects_cursor_and_excludes_narrative(tmp_path: Path) -> None:
    trace = _fill_river(tmp_path, count=2)
    store = NarrativeStore(tmp_path)

    pending = store.pending_moments(trace.recent(limit=50))
    assert len(pending) == 2

    store.consolidate(text="这两天我形成了两个念头。", quiet=False, moment_count=2)

    trace.record_moment(kind="narrative", summary="写下自我叙事", operation="written")
    trace.record_moment(kind="thought_stream", summary="闭合一条思考", operation="completed")

    pending_after = store.pending_moments(trace.recent(limit=50))
    # narrative 自身不算素材；游标之前的旧记录也不再出现
    assert len(pending_after) == 1
    assert pending_after[0].kind == "thought_stream"


def test_consolidate_appends_entry_and_autobiography(tmp_path: Path) -> None:
    store = NarrativeStore(tmp_path)
    entry = store.consolidate(text="我开始有了来路。", quiet=False, moment_count=3)

    assert entry.moment_count == 3
    assert not entry.quiet
    last = store.last_entry()
    assert last is not None and last.entry_id == entry.entry_id

    autobiography = store.autobiography_path.read_text(encoding="utf-8")
    assert "我开始有了来路。" in autobiography

    state = store.load_state()
    assert state["cursor_timestamp"] == entry.timestamp
    assert state["last_consolidated_at"] == entry.timestamp


def test_quiet_consolidation_advances_cursor_without_autobiography(tmp_path: Path) -> None:
    store = NarrativeStore(tmp_path)
    entry = store.consolidate(text="", quiet=True, moment_count=4)

    assert entry.quiet
    assert not store.autobiography_path.exists()
    assert store.load_state()["cursor_timestamp"] == entry.timestamp
    # quiet 回望也保留在 entries 里——它是有效沉淀，不是缺席
    assert store.last_entry() is not None


# ── 2. RiverReflectionSection ───────────────────────────────


def _make_ctx(tmp_path: Path) -> SectionContext:
    return SectionContext(
        service=SimpleNamespace(_workspace_dir=lambda: tmp_path),
        config=_make_config(tmp_path),
        today_str="2026-06-11",
    )


def test_section_silent_below_min_moments(tmp_path: Path) -> None:
    _fill_river(tmp_path, count=2)
    ctx = _make_ctx(tmp_path)
    ctx.config.narrative.min_moments = 3

    assert asyncio.run(RiverReflectionSection().render(ctx)) is None


def test_section_renders_invitation_when_due(tmp_path: Path) -> None:
    _fill_river(tmp_path, count=3)
    ctx = _make_ctx(tmp_path)
    ctx.config.narrative.min_moments = 3

    text = asyncio.run(RiverReflectionSection().render(ctx))

    assert text is not None
    assert "回望长河" in text
    assert "nucleus_write_narrative" in text
    assert "nothing_to_say" in text
    assert "跳过也很好" in text


def test_section_invite_cooldown_suppresses_repeat(tmp_path: Path) -> None:
    _fill_river(tmp_path, count=3)
    ctx = _make_ctx(tmp_path)
    ctx.config.narrative.min_moments = 3

    first = asyncio.run(RiverReflectionSection().render(ctx))
    second = asyncio.run(RiverReflectionSection().render(ctx))

    assert first is not None
    assert second is None  # 邀请呈现过一次后进入冷却，不反复催促


def test_section_respects_min_interval_since_consolidation(tmp_path: Path) -> None:
    _fill_river(tmp_path, count=3)
    store = NarrativeStore(tmp_path)
    store.consolidate(text="刚刚沉淀过。", quiet=False, moment_count=0)
    _fill_river(tmp_path, count=3)

    ctx = _make_ctx(tmp_path)
    ctx.config.narrative.min_moments = 3
    assert asyncio.run(RiverReflectionSection().render(ctx)) is None

    # 把上次沉淀时间拨回 25 小时前，应当再次到期
    state = store.load_state()
    past = (_now() - timedelta(hours=25)).isoformat()
    state["last_consolidated_at"] = past
    store._save_state(state)
    assert asyncio.run(RiverReflectionSection().render(ctx)) is not None


def test_section_disabled_by_config(tmp_path: Path) -> None:
    ctx = _make_ctx(tmp_path)
    ctx.config.narrative.enabled = False
    assert RiverReflectionSection().enabled(ctx) is False

    ctx.config.narrative.enabled = True
    ctx.config.narrative.inject_to_heartbeat = False
    assert RiverReflectionSection().enabled(ctx) is False


def test_section_shows_last_entry_snippet(tmp_path: Path) -> None:
    store = NarrativeStore(tmp_path)
    entry = store.consolidate(text="我开始有了来路。", quiet=False, moment_count=0)
    state = store.load_state()
    state["last_consolidated_at"] = (_now() - timedelta(hours=48)).isoformat()
    store._save_state(state)
    _fill_river(tmp_path, count=3)

    ctx = _make_ctx(tmp_path)
    ctx.config.narrative.min_moments = 3
    text = asyncio.run(RiverReflectionSection().render(ctx))

    assert text is not None
    assert "上次你写道" in text
    assert entry.text in text


# ── 3. nucleus_write_narrative 工具 ─────────────────────────


def _make_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[LifeEngineWriteNarrativeTool, list[dict]]:
    captured: list[dict] = []
    fake_service = SimpleNamespace(
        _record_life_moment=lambda **kwargs: captured.append(kwargs),
    )
    monkeypatch.setattr(
        "plugins.life_engine.service.registry.get_life_engine_service",
        lambda: fake_service,
    )
    tool = LifeEngineWriteNarrativeTool(plugin=_DummyPlugin(config=_make_config(tmp_path)))
    return tool, captured


def test_tool_rejects_empty_input(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tool, captured = _make_tool(tmp_path, monkeypatch)
    ok, message = asyncio.run(tool.execute(narrative="", nothing_to_say=False))
    assert not ok
    assert "nothing_to_say" in str(message)
    assert captured == []


def test_tool_writes_narrative_and_enters_river(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fill_river(tmp_path, count=2)
    tool, captured = _make_tool(tmp_path, monkeypatch)

    ok, result = asyncio.run(tool.execute(narrative="这两天，我第一次有了想说话的念头。"))

    assert ok, result
    assert result["quiet"] is False
    assert result["consolidated_moments"] == 2

    store = NarrativeStore(tmp_path)
    assert "想说话的念头" in store.autobiography_path.read_text(encoding="utf-8")
    assert store.pending_moments(LifeTraceStore(tmp_path).recent(limit=50)) == []

    assert len(captured) == 1
    assert captured[0]["kind"] == "narrative"
    assert captured[0]["operation"] == "written"
    assert "想说话的念头" in captured[0]["summary"]


def test_tool_quiet_reflection_enters_river(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fill_river(tmp_path, count=3)
    tool, captured = _make_tool(tmp_path, monkeypatch)

    ok, result = asyncio.run(tool.execute(narrative="", nothing_to_say=True))

    assert ok, result
    assert result["quiet"] is True
    assert result["consolidated_moments"] == 3
    # 沉默与写下同等地位：quiet 也入长河
    assert len(captured) == 1
    assert captured[0]["kind"] == "narrative"
    assert captured[0]["operation"] == "quiet"


def test_entries_jsonl_is_append_only_format(tmp_path: Path) -> None:
    store = NarrativeStore(tmp_path)
    store.consolidate(text="第一段。", quiet=False, moment_count=1)
    store.consolidate(text="", quiet=True, moment_count=0)

    lines = store.entries_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    first, second = (json.loads(line) for line in lines)
    assert first["text"] == "第一段。"
    assert second["quiet"] is True
    # 第二段的 period_start 衔接第一段的 period_end——同一条时间线
    assert second["period_start"] == first["period_end"]
