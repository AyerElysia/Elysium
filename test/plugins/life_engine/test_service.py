"""life_engine 服务测试。"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from plugins.life_engine.core.config import LifeEngineConfig
from plugins.life_engine.constants import LIFE_CHATTER_GLOBAL_CURSOR_KEY
from plugins.life_engine.service import LifeEngineService
from plugins.life_engine.service.event_bus import RAW_EVENT_LOG_FILE
from src.kernel.llm import ROLE, ToolRegistry


@dataclass
class _DummyPlugin:
    config: object


class _FakeResponse:
    def __init__(self) -> None:
        self.payloads: list[object] = []

    def add_payload(self, payload: object) -> None:
        self.payloads.append(payload)


def _make_service(tmp_path: Path) -> LifeEngineService:
    config = LifeEngineConfig()
    config.settings.enabled = True
    config.settings.workspace_path = str(tmp_path)
    return LifeEngineService(_DummyPlugin(config=config))


def test_memory_service_property_aliases_private_field(tmp_path: Path) -> None:
    """memory_service 公共属性应兼容映射到内部 _memory_service。"""
    service = _make_service(tmp_path)

    assert service.memory_service is None

    sentinel = object()
    service._memory_service = sentinel  # type: ignore[assignment]

    assert service.memory_service is sentinel


def test_cfg_auto_migrates_legacy_config_without_thresholds(tmp_path: Path) -> None:
    """旧版配置对象缺少 thresholds 时，_cfg 应自动迁移为新结构。"""

    class _LegacyConfig:
        def model_dump(self, mode: str = "python") -> dict[str, object]:
            return {
                "settings": {
                    "enabled": True,
                    "workspace_path": str(tmp_path),
                    "heartbeat_interval_seconds": 30,
                    "context_history_max_events": 100,
                    "max_rounds_per_heartbeat": 3,
                    "sleep_time": "",
                    "wake_time": "",
                    "log_heartbeat": True,
                },
                "model": {"task_name": "life"},
                "web": {},
                "snn": {"enabled": False, "shadow_only": True, "inject_to_heartbeat": False},
                "neuromod": {"enabled": True, "inject_to_heartbeat": True},
                "dream": {"enabled": True},
                "chatter": {"enabled": False, "mode": "enhanced", "max_rounds_per_chat": 5},
            }

    plugin = _DummyPlugin(config=_LegacyConfig())
    service = LifeEngineService(plugin)

    cfg = service._cfg()
    assert isinstance(cfg, LifeEngineConfig)
    assert hasattr(cfg, "thresholds")
    assert hasattr(cfg, "memory_algorithm")
    assert isinstance(plugin.config, LifeEngineConfig)


def test_heartbeat_system_prompt_filters_memory_sections(tmp_path: Path) -> None:
    """心跳态应只注入结构化 MEMORY 摘要，不带 Fading 和编辑说明。"""
    (tmp_path / "SOUL.md").write_text("SOUL_CONTENT", encoding="utf-8")
    (tmp_path / "TOOL.md").write_text("TOOL_CONTENT", encoding="utf-8")
    (tmp_path / "MEMORY.md").write_text(
        "\n".join(
            [
                "# 值得记住的事",
                "",
                "给编辑者看的说明",
                "",
                "### Durable（持久）",
                "- D1",
                "",
                "### Active（活跃）",
                "- A1",
                "",
                "### Fading（待审视）",
                "- F1",
            ]
        ),
        encoding="utf-8",
    )
    service = _make_service(tmp_path)

    prompt = service._build_heartbeat_system_prompt()

    assert "SOUL_CONTENT" in prompt
    assert "TOOL_CONTENT" in prompt
    assert "D1" in prompt
    assert "A1" in prompt
    assert "F1" not in prompt
    assert "给编辑者看的说明" not in prompt


def test_memory_maintenance_prompt_emits_once_per_interval(tmp_path: Path) -> None:
    """MEMORY 超限时，维护提醒不应在短时间内重复刷屏。"""
    oversize_item = "很长的叙事内容" * 80
    (tmp_path / "MEMORY.md").write_text(
        "\n".join(
            [
                "# 值得记住的事",
                "",
                "### Durable（持久）",
                *(f"- {oversize_item}{i}" for i in range(45)),
            ]
        ),
        encoding="utf-8",
    )
    service = _make_service(tmp_path)

    first = service._build_memory_maintenance_prompt_if_due()
    second = service._build_memory_maintenance_prompt_if_due()

    assert "MEMORY 维护任务" in first
    assert second == ""


@pytest.mark.asyncio
async def test_enqueue_dfc_message_appends_pending_event(tmp_path: Path) -> None:
    """DFC 留言应进入 pending 队列并持久化。"""
    service = _make_service(tmp_path)

    receipt = await service.enqueue_dfc_message(
        "另一个我最近有什么想法么？",
        stream_id="stream-1",
        platform="qq",
        chat_type="private",
        sender_name="DFC",
    )

    assert receipt["queued"] is True
    assert receipt["stream_id"] == "stream-1"
    assert receipt["pending_event_count"] == 1

    assert len(service._pending_events) == 1
    event = service._pending_events[0]
    assert event.event_id == receipt["event_id"]
    assert event.event_type.value == "message"
    assert event.source == "qq"
    assert event.stream_id == "stream-1"
    assert event.chat_type == "private"
    assert event.sender == "DFC"
    assert event.content == "另一个我最近有什么想法么？"
    assert "DFC 留言给生命中枢" in event.source_detail

    persisted = json.loads((tmp_path / "life_engine_context.json").read_text(encoding="utf-8"))
    assert len(persisted["pending_events"]) == 1
    assert persisted["pending_events"][0]["event_id"] == event.event_id
    assert persisted["pending_events"][0]["content_type"] == "dfc_message"

    raw_events = [
        json.loads(line)
        for line in (tmp_path / RAW_EVENT_LOG_FILE).read_text(encoding="utf-8").splitlines()
    ]
    assert raw_events[0]["event_id"] == event.event_id
    assert raw_events[0]["channel"] == "chat"
    assert raw_events[0]["reply_target"]["stream_id"] == "stream-1"


@pytest.mark.asyncio
async def test_enqueue_dfc_message_rejects_empty_message(tmp_path: Path) -> None:
    """空留言必须被拒绝。"""
    service = _make_service(tmp_path)

    with pytest.raises(ValueError, match="message 不能为空"):
        await service.enqueue_dfc_message("   ")


@pytest.mark.asyncio
async def test_heartbeat_tool_batch_executes_parallel_and_preserves_payload_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """心跳安全工具批次应并行执行，但 TOOL_RESULT 按模型调用顺序写回。"""
    service = _make_service(tmp_path)
    completion_order: list[str] = []
    tool_calls: list[str] = []
    tool_results: list[str] = []

    async def _record_tool_call(tool_name: str, _tool_args: dict) -> None:
        tool_calls.append(tool_name)

    async def _record_tool_result(tool_name: str, _result: str, _success: bool) -> None:
        tool_results.append(tool_name)

    monkeypatch.setattr(service, "record_tool_call", _record_tool_call)
    monkeypatch.setattr(service, "record_tool_result", _record_tool_result)

    class SlowReadTool:
        def __init__(self, plugin: object) -> None:
            self.plugin = plugin

        async def execute(self, **_kwargs: object) -> tuple[bool, str]:
            await asyncio.sleep(0.03)
            completion_order.append("slow")
            return True, "slow-result"

    class FastListTool:
        def __init__(self, plugin: object) -> None:
            self.plugin = plugin

        async def execute(self, **_kwargs: object) -> tuple[bool, str]:
            await asyncio.sleep(0)
            completion_order.append("fast")
            return True, "fast-result"

    registry = ToolRegistry()
    registry.register(SlowReadTool, name="nucleus_read_file")
    registry.register(FastListTool, name="nucleus_list_files")
    response = _FakeResponse()
    calls = [
        SimpleNamespace(id="slow-id", name="nucleus_read_file", args={"path": "a.md"}),
        SimpleNamespace(id="fast-id", name="nucleus_list_files", args={"path": ""}),
    ]

    event_count = await service._execute_heartbeat_tool_call_batch(
        calls,
        response,
        registry,
    )

    assert event_count == 4
    assert completion_order == ["fast", "slow"]
    assert tool_calls == ["nucleus_read_file", "nucleus_list_files"]
    assert tool_results == ["nucleus_read_file", "nucleus_list_files"]
    assert [payload.role for payload in response.payloads] == [ROLE.TOOL_RESULT, ROLE.TOOL_RESULT]
    assert [payload.content[0].name for payload in response.payloads] == [
        "nucleus_read_file",
        "nucleus_list_files",
    ]
    assert [payload.content[0].value for payload in response.payloads] == [
        "slow-result",
        "fast-result",
    ]


@pytest.mark.asyncio
async def test_heartbeat_tool_execution_strips_auto_reason_when_signature_rejects_it(
    tmp_path: Path,
) -> None:
    """心跳工具执行应剥离模型自动 reason，避免不接受该参数的工具报错。"""
    service = _make_service(tmp_path)
    seen_args: list[str] = []

    class NoReasonTool:
        def __init__(self, plugin: object) -> None:
            self.plugin = plugin

        async def execute(self, path: str) -> tuple[bool, str]:
            seen_args.append(path)
            return True, f"read:{path}"

    registry = ToolRegistry()
    registry.register(NoReasonTool, name="nucleus_read_file")

    result_text, success = await service._run_heartbeat_tool_call_execution(
        "nucleus_read_file",
        {"path": "diaries/2026-04-29.md", "reason": "模型解释"},
        registry,
    )

    assert success is True
    assert result_text == "read:diaries/2026-04-29.md"
    assert seen_args == ["diaries/2026-04-29.md"]


@pytest.mark.asyncio
async def test_heartbeat_tool_execution_keeps_declared_reason_parameter(
    tmp_path: Path,
) -> None:
    """工具显式声明 reason 时仍应保留该参数。"""
    service = _make_service(tmp_path)
    seen_reason: list[str] = []

    class ReasonTool:
        def __init__(self, plugin: object) -> None:
            self.plugin = plugin

        async def execute(self, content: str, reason: str = "") -> tuple[bool, str]:
            seen_reason.append(reason)
            return True, content

    registry = ToolRegistry()
    registry.register(ReasonTool, name="nucleus_dummy_action")

    result_text, success = await service._run_heartbeat_tool_call_execution(
        "nucleus_dummy_action",
        {"content": "hello", "reason": "主动表达"},
        registry,
    )

    assert success is True
    assert result_text == "hello"
    assert seen_reason == ["主动表达"]


@pytest.mark.asyncio
async def test_chatter_context_cursor_persists_across_restart(tmp_path: Path) -> None:
    """life_chatter 事件流游标应持久化，避免重启后重复注入旧事件。"""
    service = _make_service(tmp_path)

    await service.mark_chatter_runtime_context_seen("stream-1", 42)
    await service._save_runtime_context()

    restored = _make_service(tmp_path)
    await restored._load_runtime_context()

    assert restored._state.chatter_context_cursors["stream-1"] == 42


@pytest.mark.asyncio
async def test_chatter_think_snapshot_persists_across_restart(tmp_path: Path) -> None:
    service = _make_service(tmp_path)

    await service.record_chatter_think_snapshot(
        stream_id="stream-1",
        thought="先接住她的不安，再继续聊。",
        mood="认真",
        decision="先安抚",
        expected_response="她会安心一点",
    )
    await service._save_runtime_context()

    restored = _make_service(tmp_path)
    await restored._load_runtime_context()

    snapshot = restored._state.last_chatter_think_by_stream["stream-1"]
    global_snapshot = restored._state.last_chatter_think_by_stream[
        LIFE_CHATTER_GLOBAL_CURSOR_KEY
    ]
    assert snapshot["thought"] == "先接住她的不安，再继续聊。"
    assert global_snapshot["thought"] == "先接住她的不安，再继续聊。"
    assert snapshot["mood"] == "认真"
    assert snapshot["decision"] == "先安抚"
    assert snapshot["expected_response"] == "她会安心一点"
    assert snapshot["recorded_at"]


@pytest.mark.asyncio
async def test_enqueue_dfc_message_rejects_when_disabled(tmp_path: Path) -> None:
    """life_engine 禁用时不应接受 DFC 留言。"""
    config = LifeEngineConfig()
    config.settings.enabled = False
    config.settings.workspace_path = str(tmp_path)
    service = LifeEngineService(_DummyPlugin(config=config))

    with pytest.raises(RuntimeError, match="life_engine 未启用"):
        await service.enqueue_dfc_message("帮我记一下")
