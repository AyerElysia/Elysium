"""长河（经历留痕）测试。

长河法则：事件流是她的现在，长河是她的来路。
覆盖：
1. record_moment 轻量留痕 + kind 过滤 + v1 旧记录兼容
2. origin 源头概览
3. 文件改写携带语境（stream_id / source_event_id 断链修复）
4. 意图归宿入河（formed / silence）
5. 思考流闭合与好奇承接入河
6. chatter 注入行对非文件留痕的渲染
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from plugins.life_engine.core.config import LifeEngineConfig
from plugins.life_engine.service import LifeEngineService
from plugins.life_engine.streams.manager import ThoughtStreamManager
from plugins.life_engine.streams.tools import LifeEngineManageThoughtStreamTool
from plugins.life_engine.trace.store import LifeTraceStore


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


# ── 1. record_moment 与兼容性 ───────────────────────────────


def test_record_moment_roundtrip_and_kind_filter(tmp_path: Path) -> None:
    store = LifeTraceStore(tmp_path)
    store.record_change(
        path="SOUL.md",
        before_content=None,
        after_content="第一行",
        operation="write",
        tool_name="nucleus_write_file",
    )
    store.record_moment(
        kind="intent",
        summary="形成意向（speak）：想他了",
        operation="formed",
        stream_id="abcd1234",
    )

    all_records = store.recent(limit=10)
    assert len(all_records) == 2

    intents = store.recent(limit=10, kind="intent")
    assert len(intents) == 1
    assert intents[0].summary == "形成意向（speak）：想他了"
    assert intents[0].stream_id == "abcd1234"
    assert intents[0].path == ""

    files = store.recent(limit=10, kind="file_change")
    assert len(files) == 1
    assert files[0].path == "SOUL.md"


def test_record_moment_requires_kind_and_summary(tmp_path: Path) -> None:
    store = LifeTraceStore(tmp_path)
    with pytest.raises(ValueError):
        store.record_moment(kind="", summary="x", operation="formed")
    with pytest.raises(ValueError):
        store.record_moment(kind="intent", summary="  ", operation="formed")


def test_v1_records_remain_readable(tmp_path: Path) -> None:
    """v1 旧索引行（无 kind/summary 字段）默认视为 file_change。"""
    store = LifeTraceStore(tmp_path)
    store.trace_root.mkdir(parents=True, exist_ok=True)
    v1_line = {
        "trace_id": "trace_old00000000",
        "timestamp": "2026-04-01T10:00:00+08:00",
        "path": "MEMORY.md",
        "operation": "edit",
        "tool_name": "nucleus_edit_file",
        "actor": "life_engine",
        "reason": "",
        "before_exists": True,
        "after_exists": True,
        "before_hash": "a" * 64,
        "after_hash": "b" * 64,
        "before_size": 10,
        "after_size": 12,
        "diff_path": "diffs/trace_old00000000.diff",
        "version": 1,
    }
    store.index_path.write_text(json.dumps(v1_line) + "\n", encoding="utf-8")

    records = store.recent(limit=5)
    assert len(records) == 1
    assert records[0].kind == "file_change"
    assert records[0].summary == ""


# ── 2. origin 源头概览 ──────────────────────────────────────


def test_origin_empty_river(tmp_path: Path) -> None:
    assert LifeTraceStore(tmp_path).origin() == {"total": 0}


def test_origin_overview_counts_kinds(tmp_path: Path) -> None:
    store = LifeTraceStore(tmp_path)
    store.record_change(
        path="SOUL.md",
        before_content=None,
        after_content="我",
        operation="write",
        tool_name="nucleus_write_file",
        reason="第一次写下自己",
    )
    store.record_moment(kind="intent", summary="形成意向", operation="formed")
    store.record_moment(kind="thought_stream", summary="闭合思考流", operation="completed")

    overview = store.origin()
    assert overview["total"] == 3
    assert overview["counts_by_kind"] == {
        "file_change": 1,
        "intent": 1,
        "thought_stream": 1,
    }
    assert overview["first_record"]["path"] == "SOUL.md"
    assert overview["first_record"]["summary"] == "第一次写下自己"
    assert overview["span_days"] == 0


# ── 3. 文件改写携带语境（断链修复） ─────────────────────────


def test_file_write_records_stream_context(tmp_path: Path) -> None:
    from plugins.life_engine.tools.file_tools import LifeEngineWriteFileTool

    tool = LifeEngineWriteFileTool(plugin=_DummyPlugin(config=_make_config(tmp_path)))
    tool._bind_runtime_context(
        stream_id="stream_ffff",
        message=SimpleNamespace(message_id="msg_0001", stream_id="stream_ffff"),
    )

    ok, result = asyncio.run(tool.execute(path="notes/today.md", content="今天的事"))

    assert ok, result
    records = LifeTraceStore(tmp_path).recent(limit=1)
    assert records[0].stream_id == "stream_ffff"
    assert records[0].source_event_id == "msg_0001"


# ── 4. 意图归宿入河 ─────────────────────────────────────────


def _prepare_autonomy(service: LifeEngineService, monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_register(plugin, intent) -> None:  # noqa: ANN001
        intent.schedule_id = "test_schedule"

    async def fake_queue(event) -> None:  # noqa: ANN001
        return None

    monkeypatch.setattr(
        "plugins.life_engine.service.core.register_autonomy_schedule",
        fake_register,
    )
    monkeypatch.setattr(service, "_queue_pending_event", fake_queue)


def test_intent_formed_enters_river(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = _make_service(tmp_path)
    _prepare_autonomy(service, monkeypatch)

    result = asyncio.run(
        service.schedule_autonomy_intent(
            kind="reflect", motivation="想整理一下今天的感受", delay_minutes=5
        )
    )

    assert result["created"] is True
    moments = LifeTraceStore(tmp_path).recent(limit=5, kind="intent")
    assert len(moments) == 1
    assert moments[0].operation == "formed"
    assert "想整理一下今天的感受" in moments[0].summary
    assert moments[0].source_event_id == result["intent_id"]


def test_intent_silence_outcome_enters_river(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _make_service(tmp_path)
    _prepare_autonomy(service, monkeypatch)

    created = asyncio.run(
        service.schedule_autonomy_intent(
            kind="silence", motivation="此刻不需要说话", delay_minutes=5
        )
    )
    outcome = asyncio.run(service.trigger_autonomy_intent(created["intent_id"]))

    assert outcome["dispatch"] == "silence"
    operations = [
        record.operation
        for record in LifeTraceStore(tmp_path).recent(limit=5, kind="intent")
    ]
    # 形成与归宿都留痕：选择沉默与做了同等地位
    assert set(operations) == {"formed", "silence"}


# ── 5. 思考流闭合与好奇承接入河 ─────────────────────────────


def _make_tool_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[LifeEngineManageThoughtStreamTool, list[dict], SimpleNamespace]:
    from plugins.life_engine.curiosity.engine import CuriosityEngine, CuriositySignal
    from plugins.life_engine.neuromod.engine import InnerStateEngine

    captured: list[dict] = []
    curiosity_engine = CuriosityEngine(workspace_path=str(tmp_path))
    asyncio.run(
        curiosity_engine.save_signal(
            CuriositySignal(active=True, anchor="反复出现的旋律", why="", unknown="", approach="")
        )
    )
    fake_service = SimpleNamespace(
        _thought_manager=ThoughtStreamManager(workspace_path=str(tmp_path)),
        _inner_state=InnerStateEngine(),
        _get_curiosity_engine=lambda: curiosity_engine,
        _record_life_moment=lambda **kwargs: captured.append(kwargs),
    )
    monkeypatch.setattr(
        "plugins.life_engine.streams.tools._get_service",
        lambda: fake_service,
    )
    tool = LifeEngineManageThoughtStreamTool(plugin=_DummyPlugin(config=_make_config(tmp_path)))
    return tool, captured, fake_service


def test_retire_completed_enters_river(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tool, captured, service = _make_tool_env(tmp_path, monkeypatch)
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
    moments = [item for item in captured if item["kind"] == "thought_stream"]
    assert len(moments) == 1
    assert moments[0]["operation"] == "completed"
    assert "那段旋律" in moments[0]["summary"]
    assert "童谣" in moments[0]["summary"]


def test_absorb_curiosity_enters_river(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tool, captured, _ = _make_tool_env(tmp_path, monkeypatch)

    ok, _ = asyncio.run(
        tool.execute(action="create", title="旋律之谜", absorb_curiosity=True)
    )

    assert ok
    moments = [item for item in captured if item["kind"] == "curiosity"]
    assert len(moments) == 1
    assert moments[0]["operation"] == "absorbed"
    assert "反复出现的旋律" in moments[0]["summary"]
    assert "旋律之谜" in moments[0]["summary"]


# ── 6. chatter 注入行渲染 ───────────────────────────────────


def test_trace_line_renders_moment_kind(tmp_path: Path) -> None:
    store = LifeTraceStore(tmp_path)
    moment = store.record_moment(
        kind="intent", summary="形成意向（speak）：想他了", operation="formed"
    )
    file_record = store.record_change(
        path="SOUL.md",
        before_content=None,
        after_content="我",
        operation="write",
        tool_name="nucleus_write_file",
    )

    moment_line = LifeEngineService._format_trace_record_line(moment)
    file_line = LifeEngineService._format_trace_record_line(file_record)

    assert "[intent]" in moment_line
    assert "形成意向（speak）：想他了" in moment_line
    assert "SOUL.md" in file_line
    assert "[file_change]" not in file_line
