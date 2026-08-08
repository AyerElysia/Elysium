"""life_engine 服务测试。"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from plugins.life_engine.constants import LIFE_CHATTER_GLOBAL_CURSOR_KEY
from plugins.life_engine.core.config import LifeEngineConfig
from plugins.life_engine.service import LifeEngineService
from plugins.life_engine.service.core import (
    ChatterRuntimeDeliveryReceipt,
    HeartbeatModelResult,
)
from plugins.life_engine.service.event_builder import (
    RUNTIME_CONTEXT_FILE,
    EventType,
    LifeEngineEvent,
)
from plugins.life_engine.service.event_bus import RAW_EVENT_LOG_FILE
from plugins.life_engine.service.perception_gateway import (
    PerceptionDeliveryReceipt,
    PerceptionDeliveryUnverified,
)
from src.core.config.core_config import CoreConfig
from src.kernel.llm import ROLE, ToolRegistry


@dataclass
class _DummyPlugin:
    config: object
    global_storage_config: CoreConfig | None = None

    def __post_init__(self) -> None:
        if self.global_storage_config is None:
            self.global_storage_config = CoreConfig(
                storage=CoreConfig.StorageSection(backend="local")
            )


class _FakeResponse:
    def __init__(self) -> None:
        self.payloads: list[object] = []

    def add_payload(self, payload: object) -> None:
        self.payloads.append(payload)


def _make_service(tmp_path: Path) -> LifeEngineService:
    config = LifeEngineConfig()
    config.settings.enabled = True
    config.settings.workspace_path = str(tmp_path)
    plugin = _DummyPlugin(config=config)
    return LifeEngineService(plugin)


def _write_subject_authority(tmp_path: Path) -> None:
    """Create a complete local authority fixture for lifecycle tests."""

    for name in ("SOUL.md", "USER.md", "MEMORY.md"):
        (tmp_path / name).write_text(name, encoding="utf-8")


def _heartbeat_result(text: str, world_perception: Any) -> HeartbeatModelResult:
    return HeartbeatModelResult(
        text=text,
        perception_receipt=PerceptionDeliveryReceipt(
            delivery_id=world_perception.delivery_id,
            projection_sha256=world_perception.projection_sha256,
            delivered_bytes=world_perception.delivered_bytes,
            exact=True,
            transport_request_id="test-heartbeat",
        ),
    )


class _FakeMemoryIndexService:
    def __init__(self) -> None:
        self.run_calls = 0
        self.close_calls = 0

    async def run_index_worker(self, **_: object) -> object:
        self.run_calls += 1
        return SimpleNamespace(claimed=0, completed=(), failed=(), stale=())

    async def close(self) -> None:
        self.close_calls += 1


def test_memory_service_property_aliases_private_field(tmp_path: Path) -> None:
    """memory_service 公共属性应兼容映射到内部 _memory_service。"""
    service = _make_service(tmp_path)

    assert service.memory_service is None

    sentinel = object()
    service._memory_service = sentinel  # type: ignore[assignment]

    assert service.memory_service is sentinel


async def test_learning_maintenance_failure_does_not_escape_main_heartbeat(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Derived learning failure must not replace the heartbeat model result."""

    service = _make_service(tmp_path)

    class _FailingLearningScheduler:
        async def on_heartbeat(self) -> None:
            raise RuntimeError("selected learning persistence failed closed")

    messages: list[str] = []
    service._learning_scheduler = _FailingLearningScheduler()  # type: ignore[assignment]
    monkeypatch.setattr(
        "plugins.life_engine.service.core.logger.debug",
        messages.append,
    )

    await service._run_learning_heartbeat_maintenance()

    assert messages == ["学习系统心跳异常: RuntimeError"]


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


async def test_heartbeat_system_prompt_filters_memory_sections(tmp_path: Path) -> None:
    """心跳态应只注入结构化 MEMORY 摘要，不带 Fading 和编辑说明。"""
    (tmp_path / "SOUL.md").write_text("SOUL_CONTENT", encoding="utf-8")
    (tmp_path / "USER.md").write_text("USER_CONTENT", encoding="utf-8")
    (tmp_path / "TOOL.md").write_text(
        "\n".join(
            [
                "TOOL_CONTENT",
                "每次心跳必须调用至少一个工具",
                "先看待办再行动",
                "禁止连续发呆",
                "想到就做，现在就是合适的时机",
            ]
        ),
        encoding="utf-8",
    )
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

    prompt = await service._build_heartbeat_system_prompt()

    assert "SOUL_CONTENT" in prompt
    assert "USER_CONTENT" in prompt
    assert "TOOL_CONTENT" in prompt
    assert "心跳工具边界" in prompt
    assert "不是后台助手或任务执行器" in prompt
    assert "每次心跳必须调用至少一个工具" not in prompt
    assert "先看待办再行动" not in prompt
    assert "禁止连续发呆" not in prompt
    assert "想到就做" not in prompt
    assert "D1" in prompt
    assert "A1" in prompt
    assert "历史事实和关系线索，不是当前心跳的行动指令" in prompt
    assert "F1" not in prompt
    assert "给编辑者看的说明" not in prompt


def test_ensure_workspace_templates_creates_user_md(tmp_path: Path) -> None:
    """显式 local 模式应能为新工作空间补齐 USER.md 空模板。"""
    service = _make_service(tmp_path)

    service._ensure_workspace_templates()

    content = (tmp_path / "USER.md").read_text(encoding="utf-8")
    assert "这份文档用于记录" in content
    assert "具体内容由爱莉" in content
    assert "爱莉可以在这里慢慢填写" in content
    assert "什么时候更新" in content


@pytest.mark.parametrize("backend", ["local", "mysql"])
def test_retired_thought_manager_is_not_a_runtime_authority(
    tmp_path: Path,
    backend: str,
) -> None:
    """任何存储模式都不得重新初始化 legacy streams.json 主体权威。"""

    config = LifeEngineConfig()
    config.settings.workspace_path = str(tmp_path)
    global_config = CoreConfig(
        storage=CoreConfig.StorageSection(
            backend=backend,
            backend_generation=(
                "attention-selected-contract-v1" if backend == "mysql" else ""
            ),
            authority_owner_id="attention-selected-contract",
        )
    )

    service = LifeEngineService(
        _DummyPlugin(config=config, global_storage_config=global_config)
    )

    assert service._thought_manager is None
    assert not hasattr(service, "_initialize_legacy_thought_manager")
    assert not (tmp_path / "thoughts").exists()


def test_selected_storage_does_not_create_local_subject_template(
    tmp_path: Path,
) -> None:
    """MySQL selected 模式不得生成无远端 revision 的本地主体文件。"""

    config = LifeEngineConfig()
    config.settings.workspace_path = str(tmp_path)
    global_config = CoreConfig(
        storage=CoreConfig.StorageSection(
            backend="mysql",
            backend_generation="subject-template-contract-v1",
            authority_owner_id="subject-template-contract",
        )
    )
    service = LifeEngineService(
        _DummyPlugin(config=config, global_storage_config=global_config)
    )

    service._ensure_workspace_templates()

    assert not (tmp_path / "USER.md").exists()
    assert not (tmp_path / "runtime").exists()


async def test_local_startup_validation_requires_complete_subject_authority(
    tmp_path: Path,
) -> None:
    """Local startup must fail before acquiring runtime when MEMORY is absent."""

    (tmp_path / "SOUL.md").write_text("SOUL", encoding="utf-8")
    service = _make_service(tmp_path)

    service._ensure_workspace_templates()

    with pytest.raises(
        RuntimeError,
        match=r"^SubjectAuthoritySourceMissing: MEMORY\.md$",
    ):
        await service._validate_local_subject_authority()

    assert (tmp_path / "USER.md").is_file()
    assert not (tmp_path / "MEMORY.md").exists()
    assert service._stop_event is None
    assert service._memory_integration is None


async def test_local_start_fails_before_runtime_acquisition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An incomplete local snapshot must abort before runtime acquisition."""

    (tmp_path / "SOUL.md").write_text("SOUL", encoding="utf-8")
    service = _make_service(tmp_path)

    async def forbidden_runtime_open() -> None:
        raise AssertionError("runtime acquisition must not start")

    monkeypatch.setattr(
        service,
        "_open_selected_storage_runtime",
        forbidden_runtime_open,
    )

    with pytest.raises(
        RuntimeError,
        match=r"^SubjectAuthoritySourceMissing: MEMORY\.md$",
    ):
        await service._start_impl()

    assert service._stop_event is None
    assert service._memory_integration is None


async def test_local_startup_validation_accepts_exact_subject_snapshot(
    tmp_path: Path,
) -> None:
    """A complete local SOUL/USER/MEMORY snapshot passes startup validation."""

    for name in ("SOUL.md", "USER.md", "MEMORY.md"):
        (tmp_path / name).write_text(name, encoding="utf-8")
    service = _make_service(tmp_path)

    await service._validate_local_subject_authority()

    assert service._stop_event is None
    assert service._memory_integration is None


async def test_selected_storage_skips_local_subject_validation(
    tmp_path: Path,
) -> None:
    """MySQL mode validates its remote snapshot after acquiring authority."""

    config = LifeEngineConfig()
    config.settings.workspace_path = str(tmp_path)
    global_config = CoreConfig(
        storage=CoreConfig.StorageSection(
            backend="mysql",
            backend_generation="subject-validation-contract-v1",
            authority_owner_id="subject-validation-contract",
        )
    )
    service = LifeEngineService(
        _DummyPlugin(config=config, global_storage_config=global_config)
    )

    await service._validate_local_subject_authority()

    assert not any(tmp_path.iterdir())


def test_heartbeat_prompt_routes_subject_changes_through_review(tmp_path: Path) -> None:
    """Heartbeat must not instruct generic tools to mutate subject authority."""
    service = _make_service(tmp_path)

    prompt = "\n".join(service._build_prompt_header())

    assert "SOUL.md`、`USER.md`、`MEMORY.md` 共同属于主体权威" in prompt
    assert "nucleus_review_subject_document" in prompt
    assert "通用 file/bash 不能直接修改" in prompt
    assert "可以用文件工具谨慎更新" not in prompt


async def test_memory_maintenance_prompt_emits_once_per_interval(tmp_path: Path) -> None:
    """MEMORY 超限时，维护提醒不应在短时间内重复刷屏。"""
    (tmp_path / "SOUL.md").write_text("SOUL_CONTENT", encoding="utf-8")
    (tmp_path / "USER.md").write_text("USER_CONTENT", encoding="utf-8")
    oversize_item = "很长的叙事内容" * 80
    (tmp_path / "MEMORY.md").write_text(
        "\n".join(
            [
                "# 值得记住的事",
                "",
                "### Durable（持久）",
                *(f"- {oversize_item}{i}" for i in range(90)),
            ]
        ),
        encoding="utf-8",
    )
    service = _make_service(tmp_path)

    first = await service._build_memory_maintenance_prompt_if_due()
    second = await service._build_memory_maintenance_prompt_if_due()

    assert "MEMORY.md 结构复盘信号" in first
    assert "邀请，不是任务" in first
    assert "不要用 file/bash 直接修改 MEMORY.md" in first
    assert second == ""


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
    raw_event = next(item for item in raw_events if item["event_id"] == event.event_id)
    assert raw_event["channel"] == "chat"
    assert raw_event["reply_target"]["stream_id"] == "stream-1"
    assert raw_event["source_instance_id"] == "chat_global"


async def test_enqueue_dfc_message_rejects_empty_message(tmp_path: Path) -> None:
    """空留言必须被拒绝。"""
    service = _make_service(tmp_path)

    with pytest.raises(ValueError, match="message 不能为空"):
        await service.enqueue_dfc_message("   ")


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


async def test_chatter_context_cursor_persists_across_restart(tmp_path: Path) -> None:
    """life_chatter 事件流游标应持久化，避免重启后重复注入旧事件。"""
    service = _make_service(tmp_path)
    service._event_history = [
        LifeEngineEvent(
            event_id="evt-42",
            event_type=EventType.MESSAGE,
            timestamp="2026-04-29T12:00:00+08:00",
            sequence=42,
            source="life_engine",
            source_detail="test",
            content="CURSOR_42",
            content_type="text",
            stream_id="stream-1",
            sender="test",
        )
    ]
    _, high_water = await service.build_chatter_runtime_context(
        SimpleNamespace(stream_id="stream-1")
    )
    delivery = service.get_pending_chatter_runtime_delivery("stream-1")
    assert delivery is not None
    prepared = delivery.prepared_perception
    receipt = PerceptionDeliveryReceipt(
        delivery_id=prepared.delivery_id,
        projection_sha256=prepared.projection_sha256,
        delivered_bytes=prepared.delivered_bytes,
        exact=True,
        transport_request_id="test-request",
    )

    await service.mark_chatter_runtime_context_seen(
        "stream-1",
        high_water,
        receipt=receipt,
    )
    await service._save_runtime_context()

    restored = _make_service(tmp_path)
    await restored._load_runtime_context()

    assert restored._state.chatter_context_cursors["stream-1"] == 42


async def test_chatter_commit_checkpoint_replays_without_prompt_text(
    tmp_path: Path,
) -> None:
    service = _make_service(tmp_path)
    service._event_history = [
        LifeEngineEvent(
            event_id="evt-durable-43",
            event_type=EventType.MESSAGE,
            timestamp="2026-08-05T12:00:00+08:00",
            sequence=43,
            source="life_engine",
            source_detail="test",
            content="DURABLE_CURSOR_43",
            content_type="text",
            stream_id="stream-durable",
            sender="test",
        )
    ]
    suffix, _ = await service.build_chatter_runtime_context(
        SimpleNamespace(stream_id="stream-durable")
    )
    delivery = service.get_pending_chatter_runtime_delivery("stream-durable")
    assert delivery is not None
    effective_suffix = f"<transient_life_context>\n{suffix}\n</transient_life_context>"
    effective_bytes = len(effective_suffix.encode("utf-8"))
    effective_sha256 = hashlib.sha256(effective_suffix.encode("utf-8")).hexdigest()
    checkpoint = service.create_chatter_runtime_commit_checkpoint(
        "stream-durable",
        delivery_id=delivery.delivery_id,
        effective_suffix_sha256=effective_sha256,
        effective_suffix_bytes=effective_bytes,
    )
    receipt = ChatterRuntimeDeliveryReceipt(
        delivery_id=delivery.delivery_id,
        effective_suffix_sha256=effective_sha256,
        effective_suffix_bytes=effective_bytes,
        exact=True,
        transport_request_id="durable-request",
    )

    assert not hasattr(checkpoint, "content")
    assert not hasattr(checkpoint.perception, "content")
    committed = await service.commit_chatter_runtime_delivery(checkpoint, receipt)
    assert committed.event_through_sequence == 43
    assert not service.has_pending_chatter_perception("stream-durable")

    restored = _make_service(tmp_path)
    await restored._load_runtime_context()
    assert restored._state.chatter_context_cursors["stream-durable"] == 43
    replayed = await restored.commit_chatter_runtime_delivery(checkpoint, receipt)
    assert replayed == committed


async def test_chatter_checkpoint_keeps_pending_until_state_is_durable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _make_service(tmp_path)
    service._event_history = [
        LifeEngineEvent(
            event_id="evt-retry-44",
            event_type=EventType.MESSAGE,
            timestamp="2026-08-05T12:01:00+08:00",
            sequence=44,
            source="life_engine",
            source_detail="test",
            content="RETRY_CURSOR_44",
            content_type="text",
            stream_id="stream-retry",
            sender="test",
        )
    ]
    suffix, _ = await service.build_chatter_runtime_context(
        SimpleNamespace(stream_id="stream-retry")
    )
    delivery = service.get_pending_chatter_runtime_delivery("stream-retry")
    assert delivery is not None
    effective_bytes = len(suffix.encode("utf-8"))
    effective_sha256 = hashlib.sha256(suffix.encode("utf-8")).hexdigest()
    checkpoint = service.create_chatter_runtime_commit_checkpoint(
        "stream-retry",
        delivery_id=delivery.delivery_id,
        effective_suffix_sha256=effective_sha256,
        effective_suffix_bytes=effective_bytes,
    )
    receipt = ChatterRuntimeDeliveryReceipt(
        delivery_id=delivery.delivery_id,
        effective_suffix_sha256=effective_sha256,
        effective_suffix_bytes=effective_bytes,
        exact=True,
    )
    real_save = service._save_runtime_context

    async def failed_save() -> None:
        service._state_dirty = True

    monkeypatch.setattr(service, "_save_runtime_context", failed_save)
    with pytest.raises(RuntimeError, match="not durably persisted"):
        await service.commit_chatter_runtime_delivery(checkpoint, receipt)
    assert service.has_pending_chatter_perception("stream-retry")

    monkeypatch.setattr(service, "_save_runtime_context", real_save)
    committed = await service.commit_chatter_runtime_delivery(checkpoint, receipt)
    assert committed.event_through_sequence == 44
    assert not service.has_pending_chatter_perception("stream-retry")


async def test_chatter_checkpoint_rejects_mismatched_final_suffix_receipt(
    tmp_path: Path,
) -> None:
    service = _make_service(tmp_path)
    suffix, _ = await service.build_chatter_runtime_context(
        SimpleNamespace(stream_id="stream-mismatch")
    )
    delivery = service.get_pending_chatter_runtime_delivery("stream-mismatch")
    assert delivery is not None
    effective_bytes = len(suffix.encode("utf-8"))
    effective_sha256 = hashlib.sha256(suffix.encode("utf-8")).hexdigest()
    checkpoint = service.create_chatter_runtime_commit_checkpoint(
        "stream-mismatch",
        delivery_id=delivery.delivery_id,
        effective_suffix_sha256=effective_sha256,
        effective_suffix_bytes=effective_bytes,
    )
    mismatched = ChatterRuntimeDeliveryReceipt(
        delivery_id=delivery.delivery_id,
        effective_suffix_sha256="f" * 64,
        effective_suffix_bytes=effective_bytes,
        exact=True,
    )

    with pytest.raises(PerceptionDeliveryUnverified, match="does not match"):
        await service.commit_chatter_runtime_delivery(checkpoint, mismatched)
    assert service.has_pending_chatter_perception("stream-mismatch")


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


async def test_enqueue_dfc_message_rejects_when_disabled(tmp_path: Path) -> None:
    """life_engine 禁用时不应接受 DFC 留言。"""
    config = LifeEngineConfig()
    config.settings.enabled = False
    config.settings.workspace_path = str(tmp_path)
    service = LifeEngineService(_DummyPlugin(config=config))

    with pytest.raises(RuntimeError, match="life_engine 未启用"):
        await service.enqueue_dfc_message("帮我记一下")


async def test_web_search_accepts_empty_time_range_as_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """旧调用传入 time_range='' 时不应把空值发送给 Tavily。"""
    from plugins.life_engine.tools.web_tools import LifeEngineWebSearchTool

    captured: dict[str, object] = {}

    async def _fake_tavily_post(
        plugin: object,
        endpoint: str,
        payload: dict[str, object],
        timeout_seconds: int,
    ) -> dict[str, object]:
        captured["endpoint"] = endpoint
        captured["payload"] = payload
        return {"results": []}

    monkeypatch.setattr(
        "plugins.life_engine.tools.web_tools._tavily_post_json",
        _fake_tavily_post,
    )

    tool = LifeEngineWebSearchTool(plugin=SimpleNamespace(config=LifeEngineConfig()))
    ok, result = await tool.execute(query="测试", time_range="")

    assert ok is True
    assert result["action"] == "web_search"
    assert captured["endpoint"] == "/search"
    assert "time_range" not in captured["payload"]


async def test_heartbeat_success_consumes_delta_without_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """成功心跳只消费本轮 delta，下一轮不应重放旧事件。"""
    service = _make_service(tmp_path)
    event = service._event_builder.build_dfc_message_event(
        "只应该被心跳看到一次",
        stream_id="stream-1",
        platform="qq",
        chat_type="private",
        sender_name="Ayer",
    )
    await service._queue_pending_event(event)

    contexts: list[str] = []

    async def _fake_model(
        wake_context: str,
        *,
        heartbeat_run_id: str | None = None,
        world_perception: Any = None,
        heartbeat_deadline: float | None = None,
    ) -> HeartbeatModelResult:
        contexts.append(wake_context)
        assert heartbeat_run_id
        return _heartbeat_result("已处理", world_perception)

    monkeypatch.setattr(service, "_run_heartbeat_model", _fake_model)

    first_reply, first_prepared = await service._run_heartbeat_round(
        collect_background_agents=False,
    )
    second_reply, second_prepared = await service._run_heartbeat_round(
        collect_background_agents=False,
    )

    assert first_reply == second_reply == "已处理"
    assert "只应该被心跳看到一次" in contexts[0]
    assert "只应该被心跳看到一次" not in contexts[1]
    assert "chat_global" in contexts[1]
    assert first_prepared.acknowledged_event_ids == [event.event_id]
    assert second_prepared.selected_event_ids == []
    assert event.heartbeat_context_consumed is True
    assert service._state.heartbeat_context_cursor >= event.sequence


async def test_heartbeat_failure_keeps_delta_for_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """模型失败时不推进游标，下一次重试仍能看到原始 delta。"""
    service = _make_service(tmp_path)
    event = service._event_builder.build_dfc_message_event(
        "模型失败后必须重试这条",
        stream_id="stream-1",
    )
    await service._queue_pending_event(event)

    attempts = 0
    retry_contexts: list[str] = []

    async def _fake_model(
        wake_context: str,
        *,
        heartbeat_run_id: str | None = None,
        world_perception: Any = None,
        heartbeat_deadline: float | None = None,
    ) -> HeartbeatModelResult:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("model unavailable")
        retry_contexts.append(wake_context)
        return _heartbeat_result("重试成功", world_perception)

    monkeypatch.setattr(service, "_run_heartbeat_model", _fake_model)

    with pytest.raises(RuntimeError, match="model unavailable"):
        await service._run_heartbeat_round(collect_background_agents=False)

    assert service._state.heartbeat_context_cursor == 0
    assert event.heartbeat_context_consumed is False
    assert any(item.event_id == event.event_id for item in service._event_history)

    reply, prepared = await service._run_heartbeat_round(
        collect_background_agents=False,
    )

    assert reply == "重试成功"
    assert "模型失败后必须重试这条" in retry_contexts[0]
    assert prepared.acknowledged_event_ids == [event.event_id]
    assert service._state.heartbeat_context_cursor >= event.sequence


async def test_heartbeat_without_exact_world_receipt_keeps_delta_pending(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _make_service(tmp_path)
    event = service._event_builder.build_dfc_message_event(
        "没有精确投递证明时不能推进",
        stream_id="stream-1",
    )
    await service._queue_pending_event(event)

    async def _unverified_model(
        wake_context: str,
        *,
        heartbeat_run_id: str | None = None,
        world_perception: Any = None,
        heartbeat_deadline: float | None = None,
    ) -> HeartbeatModelResult:
        assert wake_context and heartbeat_run_id and world_perception is not None
        return HeartbeatModelResult("看似成功", None)

    monkeypatch.setattr(service, "_run_heartbeat_model", _unverified_model)

    with pytest.raises(PerceptionDeliveryUnverified):
        await service._run_heartbeat_round(collect_background_agents=False)

    assert service._state.heartbeat_context_cursor == 0
    assert event.heartbeat_context_consumed is False
    assert any(item.event_id == event.event_id for item in service._event_history)


async def test_heartbeat_arrival_during_model_is_deferred_to_next_round(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """模型执行期间到达的新事件应留给下一轮，而不是被当前提交跳过。"""
    service = _make_service(tmp_path)
    initial_event = service._event_builder.build_dfc_message_event(
        "模型开始前的事件",
        stream_id="stream-1",
    )
    arriving_event = service._event_builder.build_dfc_message_event(
        "模型执行期间到达的事件",
        stream_id="stream-1",
    )
    await service._queue_pending_event(initial_event)

    calls = 0
    contexts: list[str] = []

    async def _fake_model(
        wake_context: str,
        *,
        heartbeat_run_id: str | None = None,
        world_perception: Any = None,
        heartbeat_deadline: float | None = None,
    ) -> HeartbeatModelResult:
        nonlocal calls
        calls += 1
        contexts.append(wake_context)
        if calls == 1:
            await service._queue_pending_event(arriving_event)
        return _heartbeat_result(f"reply-{calls}", world_perception)

    monkeypatch.setattr(service, "_run_heartbeat_model", _fake_model)

    await service._run_heartbeat_round(collect_background_agents=False)

    assert "模型执行期间到达的事件" not in contexts[0]
    assert any(item.event_id == arriving_event.event_id for item in service._pending_events)
    assert arriving_event.heartbeat_context_consumed is False

    await service._run_heartbeat_round(collect_background_agents=False)

    assert "模型执行期间到达的事件" in contexts[1]
    assert service._state.heartbeat_context_cursor >= arriving_event.sequence


async def test_automatic_and_manual_heartbeats_are_serialized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """自动心跳与手动心跳共享同一事务锁，不应并行运行模型。"""
    service = _make_service(tmp_path)
    active_calls = 0
    max_active_calls = 0

    async def _fake_model(
        wake_context: str,
        *,
        heartbeat_run_id: str | None = None,
        world_perception: Any = None,
        heartbeat_deadline: float | None = None,
    ) -> HeartbeatModelResult:
        nonlocal active_calls, max_active_calls
        active_calls += 1
        max_active_calls = max(max_active_calls, active_calls)
        try:
            await asyncio.sleep(0.02)
            return _heartbeat_result("串行完成", world_perception)
        finally:
            active_calls -= 1

    monkeypatch.setattr(service, "_run_heartbeat_model", _fake_model)

    automatic_result, manual_result = await asyncio.gather(
        service._run_heartbeat_round(collect_background_agents=True),
        service.trigger_heartbeat_manually(),
    )

    assert max_active_calls == 1
    assert service._state.heartbeat_count == 2
    assert automatic_result[0] == "串行完成"
    assert manual_result["success"] is True
    assert manual_result["reply"] == "串行完成"


async def test_legacy_heartbeat_summary_migrates_to_subconscious_state(
    tmp_path: Path,
) -> None:
    """v1 用 heartbeat 标记的旧摘要应迁移为规范潜意识摘要。"""
    legacy_summary = {
        "event_id": "summary-old",
        "event_type": EventType.HEARTBEAT.value,
        "timestamp": "2026-07-18T23:00:00+08:00",
        "sequence": 7,
        "source": "system",
        "source_detail": "上下文压缩系统",
        "content": "前天发生过一次重要对话",
        "content_type": "history_summary",
        "heartbeat_index": -1,
    }
    (tmp_path / RUNTIME_CONTEXT_FILE).write_text(
        json.dumps(
            {
                "version": 1,
                "state": {"event_sequence": 7},
                "pending_events": [],
                "event_history": [legacy_summary],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    service = _make_service(tmp_path)
    await service._load_runtime_context()

    summary = service._state.subconscious_summary
    assert summary["covered_through_sequence"] == 7
    assert any(
        entry["text"] == "前天发生过一次重要对话"
        for entry in summary["entries"]
    )
    assert service._event_history == []


async def test_consumed_heartbeat_events_remain_consumed_after_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """成功提交后的消费标记和游标应可从运行时上下文恢复。"""
    service = _make_service(tmp_path)
    event = service._event_builder.build_dfc_message_event(
        "重启后不能重新注入",
        stream_id="stream-1",
    )
    await service._queue_pending_event(event)

    async def _fake_model(
        wake_context: str,
        *,
        heartbeat_run_id: str | None = None,
        world_perception: Any = None,
        heartbeat_deadline: float | None = None,
    ) -> HeartbeatModelResult:
        return _heartbeat_result("已确认", world_perception)

    monkeypatch.setattr(service, "_run_heartbeat_model", _fake_model)
    await service._run_heartbeat_round(collect_background_agents=False)
    await service._save_runtime_context()

    restored = _make_service(tmp_path)
    await restored._load_runtime_context()
    restored_event = next(
        item for item in restored._event_history if item.event_id == event.event_id
    )
    prepared = await restored._prepare_heartbeat_context()

    assert restored._state.heartbeat_context_cursor >= event.sequence
    assert restored_event.heartbeat_context_consumed is True
    assert "重启后不能重新注入" not in prepared.content
    assert "chat_global" in prepared.content


@pytest.mark.parametrize("memory_index_enabled", [True, False])
async def test_memory_index_lifecycle_start_toggle_and_stop_close(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    memory_index_enabled: bool,
) -> None:
    _write_subject_authority(tmp_path)
    service = _make_service(tmp_path)
    config = service.plugin.config
    config.memory_index.enabled = memory_index_enabled
    config.memory_index.run_on_startup = True
    config.memory_witness.enabled = True
    config.memory_witness.run_on_startup = False
    config.memory_witness.migrate_legacy_diaries = False
    config.autonomy.enabled = False
    config.streams.enabled = False
    config.drives.enabled = False
    fake_memory = _FakeMemoryIndexService()
    lifecycle_events: list[str] = []

    async def fake_init_memory(_integration: object) -> None:
        service._memory_service = fake_memory  # type: ignore[assignment]

    async def tracked_close() -> None:
        lifecycle_events.append("memory_close")
        fake_memory.close_calls += 1

    original_await_managed_task = service._await_managed_task

    async def tracked_await_managed_task(
        task_id: str | None,
        *,
        timeout: float,
    ) -> None:
        if task_id is not None and task_id == service._memory_witness_task_id:
            lifecycle_events.append("witness_wait")
        await original_await_managed_task(task_id, timeout=timeout)

    monkeypatch.setattr(fake_memory, "close", tracked_close)
    monkeypatch.setattr(service, "_await_managed_task", tracked_await_managed_task)

    monkeypatch.setattr(
        "plugins.life_engine.service.integrations.MemoryIntegration.init_memory_service",
        fake_init_memory,
    )

    await service.start()
    await asyncio.sleep(0.02)

    if memory_index_enabled:
        assert service._memory_index_task_id is not None
        assert fake_memory.run_calls >= 1
    else:
        assert service._memory_index_task_id is None
        assert fake_memory.run_calls == 0
    assert service._memory_witness_task_id is not None
    assert service._memory_witness_coordinator is not None

    await service.stop()

    assert service._memory_index_task_id is None
    assert service._memory_witness_task_id is None
    assert service._memory_witness_coordinator is None
    assert service._memory_service is None
    assert fake_memory.close_calls == 1
    assert lifecycle_events.index("witness_wait") < lifecycle_events.index("memory_close")


async def test_shared_sync_uses_managed_lifecycle_and_closes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_subject_authority(tmp_path)
    service = _make_service(tmp_path)
    config = service.plugin.config
    config.shared_sync.enabled = True
    config.memory_index.enabled = False
    config.memory_witness.enabled = False
    config.autonomy.enabled = False
    config.streams.enabled = False
    config.drives.enabled = False
    config.learning.enabled = False
    lifecycle: list[str] = []

    class FakeBridge:
        def __init__(self, section: object, store: object) -> None:
            assert section is config.shared_sync
            assert store is service._get_event_bus().store
            lifecycle.append("created")

        async def run(self, stop_event: asyncio.Event) -> None:
            lifecycle.append("running")
            await stop_event.wait()
            lifecycle.append("stopped")

        async def close(self) -> None:
            lifecycle.append("closed")

        def health_snapshot(self) -> dict[str, object]:
            return {"component": "offline_sync", "status": "healthy"}

    async def fake_init_memory(_integration: object) -> None:
        service._memory_service = None

    monkeypatch.setattr(
        "plugins.life_engine.service.shared_sync.SharedSyncBridge",
        FakeBridge,
    )
    monkeypatch.setattr(
        "plugins.life_engine.service.integrations.MemoryIntegration.init_memory_service",
        fake_init_memory,
    )

    await service.start()
    await asyncio.sleep(0)
    assert service._shared_sync_task_id is not None
    assert lifecycle[:2] == ["created", "running"]
    assert service.health()["shared_sync"]["status"] == "healthy"

    await service.stop()

    assert service._shared_sync_task_id is None
    assert service._shared_sync_bridge is None
    assert lifecycle == ["created", "running", "stopped", "closed"]


async def test_memory_archive_sync_uses_managed_lifecycle_and_closes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_subject_authority(tmp_path)
    service = _make_service(tmp_path)
    config = service.plugin.config
    config.memory_archive_sync.enabled = True
    config.memory_index.enabled = False
    config.memory_witness.enabled = False
    config.autonomy.enabled = False
    config.streams.enabled = False
    config.drives.enabled = False
    config.learning.enabled = False
    lifecycle: list[str] = []

    class FakeBridge:
        def __init__(self, section: object, workspace_path: object) -> None:
            assert section is config.memory_archive_sync
            assert workspace_path == config.settings.workspace_path
            lifecycle.append("created")

        async def run(self, stop_event: asyncio.Event) -> None:
            lifecycle.append("running")
            await stop_event.wait()
            lifecycle.append("stopped")

        async def close(self) -> None:
            lifecycle.append("closed")

        def health_snapshot(self) -> dict[str, object]:
            return {
                "component": "unified_memory_archive",
                "status": "healthy",
            }

    async def fake_init_memory(_integration: object) -> None:
        service._memory_service = None

    monkeypatch.setattr(
        "plugins.life_engine.service.memory_archive_sync.MemoryArchiveSyncBridge",
        FakeBridge,
    )
    monkeypatch.setattr(
        "plugins.life_engine.service.integrations.MemoryIntegration.init_memory_service",
        fake_init_memory,
    )

    await service.start()
    await asyncio.sleep(0)
    assert service._memory_archive_sync_task_id is not None
    assert lifecycle[:2] == ["created", "running"]
    assert service.health()["memory_archive_sync"]["status"] == "healthy"

    await service.stop()

    assert service._memory_archive_sync_task_id is None
    assert service._memory_archive_sync_bridge is None
    assert lifecycle == ["created", "running", "stopped", "closed"]


async def test_memory_index_loop_survives_provider_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _make_service(tmp_path)
    service._state.running = True
    service._stop_event = asyncio.Event()

    class FailingOnceMemory(_FakeMemoryIndexService):
        async def run_index_worker(self, **_: object) -> object:
            self.run_calls += 1
            if self.run_calls == 1:
                raise RuntimeError("provider unavailable")
            service._stop_event.set()
            return SimpleNamespace(claimed=0, completed=(), failed=(), stale=())

    fake_memory = FailingOnceMemory()
    service._memory_service = fake_memory  # type: ignore[assignment]
    monkeypatch.setattr(
        service,
        "_memory_index_options",
        lambda: {
            "enabled": True,
            "interval_seconds": 0,
            "batch_size": 2,
            "run_on_startup": True,
            "retry_failed": False,
            "reclaim_after_seconds": 60,
        },
    )

    await asyncio.wait_for(service._memory_index_loop(), timeout=1.0)

    assert fake_memory.run_calls == 2
