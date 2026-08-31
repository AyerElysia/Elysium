"""life_engine 服务测试。"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.exc import OperationalError

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
from plugins.life_engine.service.world_projection import PerceptionCursorConflict
from src.core.config.core_config import CoreConfig
from src.kernel.llm import ROLE, ToolRegistry
from src.kernel.llm.context_delivery import EffectiveContextReceipt


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


def _workspace_file_hashes(workspace: Path) -> dict[str, str]:
    """Return content hashes without interpreting subject-owned text."""

    if not workspace.exists():
        return {}
    return {
        path.relative_to(workspace).as_posix(): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in sorted(workspace.rglob("*"))
        if path.is_file()
    }


@pytest.mark.asyncio
async def test_proactive_delivery_proof_hook_binds_transport_to_authority(
    tmp_path: Path,
) -> None:
    from src.core.transport import multi_writer_hooks as hooks

    service = _make_service(tmp_path)
    authority = SimpleNamespace(
        record_outreach_delivery_proof=AsyncMock(return_value=object())
    )
    service._proactive_authority = authority
    message = SimpleNamespace(
        extra={
            "initiative_outreach_occurrences": [
                "outreach:one",
                "outreach:one",
                "outreach:two",
            ],
            "tool_call_id": "action:one",
        }
    )
    receipt = {
        "schema_version": 1,
        "receipt_kind": "adapter_ack",
        "message_id": "message:one",
        "platform": "qq",
        "adapter_signature": "mock:adapter:qq",
        "provider_receipt": {"status": "ok"},
    }
    saved = hooks._outbound_delivery_proof_hook
    hooks._outbound_delivery_proof_hook = None
    try:
        service._attach_proactive_delivery_proof_hook()
        assert await hooks.invoke_outbound_delivery_proof_hook(
            message,
            receipt,
        ) is True
        assert authority.record_outreach_delivery_proof.await_count == 2
        first = authority.record_outreach_delivery_proof.await_args_list[0].kwargs
        second = authority.record_outreach_delivery_proof.await_args_list[1].kwargs
        assert first["outreach_occurrence_id"] == "outreach:one"
        assert second["outreach_occurrence_id"] == "outreach:two"
        assert first["action_id"] == second["action_id"] == "action:one"
        assert first["delivery_receipt"] == receipt
        assert first["occurred_at"] == second["occurred_at"]
        service._detach_proactive_delivery_proof_hook()
        assert await hooks.invoke_outbound_delivery_proof_hook(
            message,
            receipt,
        ) is None
    finally:
        service._detach_proactive_delivery_proof_hook()
        hooks._outbound_delivery_proof_hook = saved


def _heartbeat_result(
    text: str,
    world_perception: Any,
    wake_context: str,
) -> HeartbeatModelResult:
    delivery_id, _ = LifeEngineService._subconscious_delivery_identity(
        wake_context
    )
    subconscious_receipt = (
        EffectiveContextReceipt(
            delivery_id=delivery_id,
            exact_present=True,
            expected_utf8_bytes=1,
            expected_sha256="a" * 64,
            effective_utf8_bytes=1,
            effective_sha256="a" * 64,
        )
        if delivery_id
        else None
    )
    return HeartbeatModelResult(
        text=text,
        perception_receipt=PerceptionDeliveryReceipt(
            delivery_id=world_perception.delivery_id,
            projection_sha256=world_perception.projection_sha256,
            delivered_bytes=world_perception.delivered_bytes,
            exact=True,
            transport_request_id="test-heartbeat",
        ),
        subconscious_receipt=subconscious_receipt,
    )


class _FakeMemoryIndexService:
    def __init__(self) -> None:
        self.run_calls = 0
        self.close_calls = 0
        self.behavior_health_provider: object | None = None

    async def run_index_worker(self, **_: object) -> object:
        self.run_calls += 1
        return SimpleNamespace(claimed=0, completed=(), failed=(), stale=())

    async def close(self) -> None:
        self.close_calls += 1

    def set_behavior_health_provider(self, provider: object | None) -> None:
        self.behavior_health_provider = provider


def test_memory_service_property_aliases_private_field(tmp_path: Path) -> None:
    """memory_service 公共属性应兼容映射到内部 _memory_service。"""
    service = _make_service(tmp_path)

    assert service.memory_service is None

    sentinel = object()
    service._memory_service = sentinel  # type: ignore[assignment]

    assert service.memory_service is sentinel


async def test_recent_subconscious_context_is_read_only_and_repeatable(
    tmp_path: Path,
) -> None:
    service = _make_service(tmp_path)
    service._event_history = [
        LifeEngineEvent(
            event_id="heartbeat-7",
            event_type=EventType.HEARTBEAT,
            timestamp="2026-08-18T00:00:00+08:00",
            sequence=7,
            source="life_engine",
            source_detail="test",
            content="最近形成的想法",
            content_type="heartbeat_reply",
            heartbeat_context_consumed=True,
        )
    ]
    service._state.heartbeat_context_cursor = 6
    service._state.chatter_context_cursors = {"stream-a": 4}
    before_history = list(service._event_history)

    first = await service.get_recent_subconscious_context(
        group_limit=1,
        max_bytes=1024,
    )
    second = await service.get_recent_subconscious_context(
        group_limit=1,
        max_bytes=1024,
    )

    assert first == second
    assert "最近形成的想法" in first.content
    assert first.event_ids == ("heartbeat-7",)
    assert service._event_history == before_history
    assert service._state.heartbeat_context_cursor == 6
    assert service._state.chatter_context_cursors == {"stream-a": 4}
    assert service._pending_events == []


async def test_chatter_runtime_context_appends_recent_subconscious_once(
    tmp_path: Path,
) -> None:
    service = _make_service(tmp_path)
    service._event_history = [
        LifeEngineEvent(
            event_id="heartbeat-8",
            event_type=EventType.HEARTBEAT,
            timestamp="2026-08-18T00:01:00+08:00",
            sequence=8,
            source="life_engine",
            source_detail="test",
            content="跨场景仍然记得刚才在做什么",
            content_type="heartbeat_reply",
            heartbeat_run_id="run-8",
            heartbeat_context_consumed=True,
        ),
        LifeEngineEvent(
            event_id="tool-call-9",
            event_type=EventType.TOOL_CALL,
            timestamp="2026-08-18T00:01:01+08:00",
            sequence=9,
            source="life_engine",
            source_detail="test",
            content="调用工具: inspect",
            content_type="tool_call",
            heartbeat_run_id="run-8",
            call_id="call-8",
            parent_event_id="heartbeat-8",
            tool_name="inspect",
            tool_args={"path": "notes.txt"},
            heartbeat_context_consumed=True,
        ),
        LifeEngineEvent(
            event_id="tool-result-10",
            event_type=EventType.TOOL_RESULT,
            timestamp="2026-08-18T00:01:02+08:00",
            sequence=10,
            source="life_engine",
            source_detail="test",
            content="完成",
            content_type="tool_result",
            heartbeat_run_id="run-8",
            call_id="call-8",
            parent_event_id="tool-call-9",
            tool_name="inspect",
            tool_success=True,
            heartbeat_context_consumed=True,
        ),
    ]

    context, _ = await service.build_chatter_runtime_context(
        SimpleNamespace(stream_id="stream-a"),
    )

    assert "【潜意识近期上下文】" in context
    assert "跨场景仍然记得刚才在做什么" in context
    assert context.count("TOOL_CALL inspect") == 1
    assert context.count("TOOL_RESULT inspect success") == 1
    assert "当前环境感知（World，仅表示有来源的环境事实，不承担跨意识同步）" in context


async def test_memory_behavior_health_combines_witness_and_continuity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _make_service(tmp_path)
    living = object()

    class _Memory:
        def __init__(self) -> None:
            self.living_memory_store = living

    class _Witness:
        async def health_snapshot(self) -> dict[str, object]:
            return {
                "status": "healthy",
                "component": "memory_witness_pipeline",
                "raw_ingest": {"backlog": 3},
                "author": {"backlog": 2},
                "runtime": {"last_success_at": "2026-08-12T00:00:00+00:00"},
            }

    subject_store = object()
    service._selectable_storage_enabled = True
    service._memory_service = _Memory()  # type: ignore[assignment]
    service._subject_document_store = subject_store  # type: ignore[assignment]
    service._memory_witness_coordinator = _Witness()
    collect = AsyncMock(
        return_value={
            "status": "healthy",
            "component": "memory_continuity",
            "verified_boundary_count": 4,
        }
    )
    monkeypatch.setattr(
        "plugins.life_engine.memory.continuity_health.collect_continuity_memory_health",
        collect,
    )

    class _IdleRecallDelivery:
        def __init__(self, component: str) -> None:
            self._component = component

        def health_snapshot(self) -> dict[str, object]:
            return {
                "status": "healthy",
                "component": self._component,
                "pending_count": 0,
            }

    monkeypatch.setattr(
        "plugins.life_engine.memory.boundary_resolver.get_memory_boundary_recall_coordinator",
        lambda: _IdleRecallDelivery("memory_recall_exact_delivery"),
    )
    monkeypatch.setattr(
        "plugins.life_engine.memory.recall_delivery.get_memory_search_recall_delivery_coordinator",
        lambda: _IdleRecallDelivery("memory_search_recall_delivery"),
    )

    snapshot = await service._memory_behavior_health_snapshot()

    assert snapshot["status"] == "healthy"
    assert snapshot["backlog"] == 5
    assert snapshot["last_success_at"] == "2026-08-12T00:00:00+00:00"
    assert snapshot["continuity"]["verified_boundary_count"] == 4
    assert snapshot["recall_delivery"]["boundary"]["component"] == (
        "memory_recall_exact_delivery"
    )
    assert snapshot["recall_delivery"]["search"]["component"] == (
        "memory_search_recall_delivery"
    )
    collect.assert_awaited_once_with(
        subject_store=subject_store,
        living_store=living,
    )


async def test_memory_behavior_health_marks_missing_selected_runtime_failed(
    tmp_path: Path,
) -> None:
    service = _make_service(tmp_path)
    service._selectable_storage_enabled = True

    snapshot = await service._memory_behavior_health_snapshot()

    assert snapshot["status"] == "failed"
    assert snapshot["witness"]["status"] == "disabled"
    assert snapshot["continuity"]["error_type"] == (
        "ContinuityHealthCoherentRuntimeUnavailable"
    )


async def test_dfc_memory_search_uses_canonical_living_associations() -> None:
    calls: dict[str, object] = {}

    class _Memory:
        async def search_memory(self, query: str, **kwargs: object) -> list[object]:
            calls["search"] = (query, kwargs)
            return [object()]

        async def expand_living_document_associations(
            self,
            results: list[object],
            **kwargs: object,
        ) -> list[object]:
            calls["expand"] = (results, kwargs)
            return []

    service = object.__new__(LifeEngineService)
    service._memory_service = _Memory()  # type: ignore[assignment]

    assert await service.search_actor_memory("shared memory", top_k=3) == ""
    query, search_kwargs = calls["search"]  # type: ignore[misc]
    assert query == "shared memory"
    assert search_kwargs["enable_association"] is False
    _results, expansion_kwargs = calls["expand"]  # type: ignore[misc]
    assert expansion_kwargs["context_key"] == "life_engine/dfc"
    assert expansion_kwargs["limit"] == 3


async def test_learning_maintenance_only_wakes_independent_worker(
    tmp_path: Path,
) -> None:
    """The foreground heartbeat must not execute a learning model phase."""

    service = _make_service(tmp_path)

    class _LearningScheduler:
        wake_count = 0

        def request_maintenance(self) -> None:
            self.wake_count += 1

        async def on_heartbeat(self) -> None:
            raise AssertionError("foreground heartbeat must not execute learning")

    scheduler = _LearningScheduler()
    service._learning_scheduler = scheduler  # type: ignore[assignment]

    await service._run_learning_heartbeat_maintenance()

    assert scheduler.wake_count == 1


async def test_service_uses_learning_specific_model_and_total_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_subject_authority(tmp_path)
    service = _make_service(tmp_path)
    config = service.plugin.config
    config.learning.model_task_name = "learning_test"
    config.learning.llm_timeout_seconds = 777.0
    config.memory_index.enabled = False
    config.memory_witness.enabled = False
    config.autonomy.enabled = False
    config.streams.enabled = False
    config.drives.enabled = False
    captured: dict[str, object] = {}

    class _LearningScheduler:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

        async def initialize(self) -> None:
            return None

        def request_maintenance(self) -> None:
            return None

        async def run(
            self,
            stop_event: asyncio.Event,
            *,
            poll_interval_seconds: float,
        ) -> None:
            del poll_interval_seconds
            await stop_event.wait()

        async def close(self) -> None:
            return None

    async def fake_init_memory(_integration: object) -> None:
        service._memory_service = None

    monkeypatch.setattr(
        "plugins.life_engine.learning.scheduler.LearningScheduler",
        _LearningScheduler,
    )
    monkeypatch.setattr(
        "plugins.life_engine.service.integrations.MemoryIntegration.init_memory_service",
        fake_init_memory,
    )

    await service.start()
    try:
        assert captured["model_task_name"] == "learning_test"
        assert captured["llm_timeout_seconds"] == 777.0
    finally:
        await service.stop()


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


@pytest.mark.parametrize("missing_name", ("SOUL.md", "USER.md", "MEMORY.md"))
async def test_local_subject_authority_missing_is_read_only(
    tmp_path: Path,
    missing_name: str,
) -> None:
    """Missing subject authority must fail without minting replacement text."""

    for name in ("SOUL.md", "USER.md", "MEMORY.md"):
        if name != missing_name:
            (tmp_path / name).write_bytes(f"exact-{name}".encode())
    service = _make_service(tmp_path)
    before = _workspace_file_hashes(tmp_path)

    with pytest.raises(RuntimeError) as exc_info:
        await service.start()

    assert str(exc_info.value) == f"SubjectAuthoritySourceMissing: {missing_name}"
    assert _workspace_file_hashes(tmp_path) == before
    assert not (tmp_path / missing_name).exists()
    assert service._stop_event is None
    assert service._memory_integration is None


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

    assert not hasattr(service, "_thought_manager")
    assert not hasattr(service, "_initialize_legacy_thought_manager")
    assert not (tmp_path / "thoughts").exists()


async def test_local_start_fails_before_runtime_acquisition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Public startup preflight must not create an absent workspace on failure."""

    workspace = tmp_path / "absent-workspace"
    service = _make_service(workspace)
    assert not workspace.exists()

    async def forbidden_runtime_open() -> None:
        raise AssertionError("runtime acquisition must not start")

    monkeypatch.setattr(
        service,
        "_open_selected_storage_runtime",
        forbidden_runtime_open,
    )

    with pytest.raises(
        RuntimeError,
        match=r"^SubjectAuthoritySourceMissing: SOUL\.md$",
    ):
        await service.start()

    assert not workspace.exists()
    assert service._stop_event is None
    assert service._memory_integration is None


async def test_local_startup_validation_accepts_exact_subject_snapshot(
    tmp_path: Path,
) -> None:
    """A complete local SOUL/USER/MEMORY snapshot passes startup validation."""

    for name in ("SOUL.md", "USER.md", "MEMORY.md"):
        (tmp_path / name).write_text(name, encoding="utf-8")
    service = _make_service(tmp_path)
    before = _workspace_file_hashes(tmp_path)

    await service._validate_local_subject_authority()

    assert set(before) == {"SOUL.md", "USER.md", "MEMORY.md"}
    assert _workspace_file_hashes(tmp_path) == before
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


async def test_heartbeat_records_complete_model_turn_args_and_structured_result(
    tmp_path: Path,
) -> None:
    service = _make_service(tmp_path)

    class FullActivityTool:
        def __init__(self, plugin: object) -> None:
            self.plugin = plugin

        async def execute(self, note: str, reason: str = "") -> tuple[bool, dict]:
            return True, {"note": note, "reason": reason, "detail": "完整结果"}

    registry = ToolRegistry()
    registry.register(FullActivityTool, name="nucleus_full_activity")
    call = SimpleNamespace(
        id="heartbeat-call-1",
        name="nucleus_full_activity",
        args={"note": "保留全部参数", "reason": "这是实际生成的选择理由"},
    )
    response = _FakeResponse()
    response.message = "我决定先调用工具核对"
    response.reasoning_content = "完整的 provider reasoning"
    response.request_record_id = "heartbeat-request-1"

    model_event, call_ids = await service._record_heartbeat_model_turn_activity(
        response,
        [call],
        heartbeat_run_id="heartbeat-run-1",
        turn_index=0,
    )
    await service._execute_heartbeat_tool_call(
        call,
        response,
        registry,
        heartbeat_run_id="heartbeat-run-1",
        model_turn_event_id=model_event.event_id,
        call_id_override=call_ids[id(call)],
    )

    pending = list(service._pending_events)
    assert [event.content_type for event in pending] == [
        "conscious_activity_model_turn",
        "tool_call",
        "tool_result",
    ]
    model_raw = json.loads(pending[0].raw_content or "{}")
    call_raw = json.loads(pending[1].raw_content or "{}")
    result_raw = json.loads(pending[2].raw_content or "{}")
    assert model_raw["surface"] == "life_engine_subconscious"
    assert model_raw["provider_reasoning_content"] == (
        "完整的 provider reasoning"
    )
    assert model_raw["assistant_message"] == "我决定先调用工具核对"
    assert call_raw["arguments"]["reason"] == "这是实际生成的选择理由"
    assert pending[1].parent_event_id == model_event.event_id
    assert result_raw["result"] == {
        "note": "保留全部参数",
        "reason": "这是实际生成的选择理由",
        "detail": "完整结果",
    }


async def test_conscious_activity_state_preserves_attribution_and_payload(
    tmp_path: Path,
) -> None:
    service = _make_service(tmp_path)

    event = await service.record_conscious_activity_state(
        stream_id="voice-stream-1",
        source_instance_id="voice_live_episode",
        occurrence_id="voice:episode:interruption:7",
        state_kind="response_interrupted",
        payload={
            "source": "client",
            "response_id": "response-1",
            "item_id": "item-1",
        },
        surface="voice_live",
        causation_id="response-1",
    )

    raw = json.loads(event.raw_content or "{}")
    assert event.event_type == EventType.CONSCIOUS_ACTIVITY
    assert event.content_type == "conscious_activity_state"
    assert event.source_instance_id == "voice_live_episode"
    assert event.stream_id == "voice-stream-1"
    assert event.causation_id == "response-1"
    assert raw["state_kind"] == "response_interrupted"
    assert raw["payload"] == {
        "source": "client",
        "response_id": "response-1",
        "item_id": "item-1",
    }


async def test_conscious_activity_queue_is_authoritative_first_and_idempotent(
    tmp_path: Path,
) -> None:
    service = _make_service(tmp_path)
    event = service._event_builder.build_conscious_activity_state_event(
        activity_id="activity-state-1",
        stream_id="voice-stream-1",
        source_instance_id="voice-instance-1",
        occurrence_id="voice:episode:wait:1",
        state_kind="waiting",
        payload={"reason": "subject_wait"},
        surface="voice_live",
    )
    service._publish_raw_events = AsyncMock(  # type: ignore[method-assign]
        side_effect=RuntimeError("ledger rejected")
    )

    with pytest.raises(RuntimeError, match="ledger rejected"):
        await service._queue_pending_events([event], persist=False)
    assert service._pending_events == []

    service._publish_raw_events = AsyncMock()  # type: ignore[method-assign]
    await service._queue_pending_events([event], persist=False)
    await service._queue_pending_events([event], persist=False)

    assert [item.occurrence_id for item in service._pending_events] == [
        event.occurrence_id
    ]
    assert service._publish_raw_events.await_count == 2


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


async def test_conscious_tool_activity_is_durable_and_paired(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    arguments = {
        "mood": "温柔",
        "decision": "直接回应",
        "expected_response": "对方感到被看见",
        "thought": "先理解，再表达。",
        "content": "我在听。",
    }

    activity_ids = await service.record_conscious_model_turn(
        stream_id="stream-1",
        source_instance_id="chat-instance-1",
        turn_occurrence_id="turn-1",
        transport_request_id="request-1",
        provider_reasoning_content="provider reasoning 原文",
        assistant_message="同轮独白原文",
        calls=[
            {
                "call_id": "call-1",
                "tool_name": "action-life_send_text",
                "arguments": arguments,
            }
        ],
    )
    await service.record_conscious_tool_results(
        stream_id="stream-1",
        source_instance_id="chat-instance-1",
        turn_occurrence_id="turn-1",
        activity_ids=activity_ids,
        results=[
            {
                "call_id": "call-1",
                "tool_name": "action-life_send_text",
                "result": {"status": "delivered"},
                "success": True,
                "technical_outcome": "delivered",
                "delivery_receipt_sha256": "a" * 64,
                "delivery_message_id": "provider-1",
                "delivery_proof_status": "durable",
            }
        ],
    )

    activity_id = activity_ids["call-1"]
    model_turn_activity_id = service._conscious_model_turn_activity_id(
        source_instance_id="chat-instance-1",
        stream_id="stream-1",
        turn_occurrence_id="turn-1",
        transport_request_id="request-1",
    )
    model_turn = next(
        item
        for item in service._pending_events
        if item.event_id == f"{model_turn_activity_id}:generated"
    )
    chosen = next(
        item
        for item in service._pending_events
        if item.event_id == f"{activity_id}:chosen"
    )
    outcome = next(
        item
        for item in service._pending_events
        if item.event_id == f"{activity_id}:result"
    )
    chosen_payload = json.loads(chosen.raw_content or "{}")
    outcome_payload = json.loads(outcome.raw_content or "{}")
    model_turn_payload = json.loads(model_turn.raw_content or "{}")

    assert model_turn.event_type == EventType.CONSCIOUS_ACTIVITY
    assert model_turn_payload["provider_reasoning_content"] == (
        "provider reasoning 原文"
    )
    assert model_turn_payload["assistant_message"] == "同轮独白原文"
    assert model_turn_payload["tool_call_ids"] == ["call-1"]
    assert chosen_payload["arguments"] == arguments
    assert chosen.source_instance_id == "chat-instance-1"
    assert chosen.stream_id == "stream-1"
    assert chosen.correlation_id == "turn-1"
    assert chosen.parent_event_id == model_turn.event_id
    assert chosen.causation_id == model_turn.event_id
    assert outcome.parent_event_id == chosen.event_id
    assert outcome.causation_id == chosen.event_id
    assert outcome_payload["result"] == {"status": "delivered"}
    assert outcome_payload["delivery_proof_status"] == "durable"

    repeated_call_id = await service.record_conscious_model_turn(
        stream_id="stream-1",
        source_instance_id="chat-instance-1",
        turn_occurrence_id="turn-1",
        transport_request_id="request-2",
        provider_reasoning_content="第二个 follow-up",
        assistant_message="",
        calls=[
            {
                "call_id": "call-1",
                "tool_name": "action-life_send_text",
                "arguments": arguments,
            }
        ],
    )
    assert repeated_call_id["call-1"] != activity_id


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
        return _heartbeat_result("已处理", world_perception, wake_context)

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


async def test_heartbeat_perception_cursor_conflict_keeps_model_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """多写者感知游标被其他实例推进时，心跳模型输出必须保留。

    双实例各自维护 heartbeat_count，并行心跳共享 life_engine_subconscious
    感知游标必然交错；commit_perception 抛 PerceptionCursorConflict 只说明
    本实例的感知交付已过时，不应被误判为心跳失败——模型独白照常写入事件
    时间线、delta 照常消费、heartbeat operation 正常提交，而不是丢输出 +
    标 failed + 下轮重放同一 sequence 导致重复模型调用。
    """
    service = _make_service(tmp_path)
    event = service._event_builder.build_dfc_message_event(
        "竞争发生时这条 delta 仍应被消费",
        stream_id="stream-1",
    )
    await service._queue_pending_event(event)

    async def _conflicted_commit(
        prepared: Any,
        receipt: Any = None,
    ) -> tuple[int, int]:
        raise PerceptionCursorConflict(
            "stale perception cursor for 'life_engine_subconscious': "
            "expected (1, 1), actual (2, 2)"
        )

    monkeypatch.setattr(service, "commit_perception", _conflicted_commit)

    async def _fake_model(
        wake_context: str,
        *,
        heartbeat_run_id: str | None = None,
        world_perception: Any = None,
        heartbeat_deadline: float | None = None,
    ) -> HeartbeatModelResult:
        return _heartbeat_result(
            "竞争时保留的独白",
            world_perception,
            wake_context,
        )

    monkeypatch.setattr(service, "_run_heartbeat_model", _fake_model)

    reply, prepared = await service._run_heartbeat_round(
        collect_background_agents=False,
    )

    assert reply == "竞争时保留的独白"
    assert service._state.last_model_error is None
    # 模型独白以 heartbeat 事件进入时间线，未被竞争丢弃
    assert any(
        item.content == "竞争时保留的独白"
        and item.event_type == EventType.HEARTBEAT
        for item in service._event_history
    )
    # 本轮 delta 被正常消费，游标推进
    assert prepared.acknowledged_event_ids == [event.event_id]
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
        return _heartbeat_result("重试成功", world_perception, wake_context)

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


async def test_heartbeat_without_exact_subconscious_receipt_keeps_delta_pending(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _make_service(tmp_path)
    event = service._event_builder.build_dfc_message_event(
        "World 已送达也不能替代整份潜意识投递证明",
        stream_id="stream-1",
    )
    await service._queue_pending_event(event)

    async def _missing_subconscious_receipt(
        wake_context: str,
        *,
        heartbeat_run_id: str | None = None,
        world_perception: Any = None,
        heartbeat_deadline: float | None = None,
    ) -> HeartbeatModelResult:
        assert wake_context and heartbeat_run_id and world_perception is not None
        return HeartbeatModelResult(
            text="看似成功",
            perception_receipt=PerceptionDeliveryReceipt(
                delivery_id=world_perception.delivery_id,
                projection_sha256=world_perception.projection_sha256,
                delivered_bytes=world_perception.delivered_bytes,
                exact=True,
                transport_request_id="test-world-only",
            ),
            subconscious_receipt=None,
        )

    monkeypatch.setattr(
        service,
        "_run_heartbeat_model",
        _missing_subconscious_receipt,
    )

    with pytest.raises(
        PerceptionDeliveryUnverified,
        match="exact subconscious activity delivery proof",
    ):
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
        return _heartbeat_result(
            f"reply-{calls}",
            world_perception,
            wake_context,
        )

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
            return _heartbeat_result("串行完成", world_perception, wake_context)
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
        return _heartbeat_result("已确认", world_perception, wake_context)

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
    assert fake_memory.behavior_health_provider is not None

    await service.stop()

    assert service._memory_index_task_id is None
    assert service._memory_witness_task_id is None
    assert service._memory_witness_coordinator is None
    assert service._memory_service is None
    assert fake_memory.behavior_health_provider is None
    assert fake_memory.close_calls == 1
    assert lifecycle_events.index("witness_wait") < lifecycle_events.index("memory_close")


async def test_selected_storage_disables_legacy_shared_sync_without_bridge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _make_service(tmp_path)
    service._selectable_storage_enabled = True
    service.plugin.config.shared_sync.enabled = True
    service._stop_event = asyncio.Event()

    class ForbiddenBridge:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            raise AssertionError("selected storage constructed the legacy sync bridge")

    monkeypatch.setattr(
        "plugins.life_engine.service.shared_sync.SharedSyncBridge",
        ForbiddenBridge,
    )

    await service._start_shared_sync(service.plugin.config)
    health = service.health()["shared_sync"]

    assert service._shared_sync_bridge is None
    assert service._shared_sync_task_id is None
    assert service._shared_sync_error == ""
    assert health["status"] == "disabled"
    assert health["enabled"] is False
    assert health["configured_enabled"] is True
    assert health["disabled_reason"] == (
        "selected_authoritative_backend_unsupported"
    )


async def test_subject_projection_worker_retries_transient_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _make_service(tmp_path)
    service._stop_event = asyncio.Event()
    calls = 0

    class FailingOnceProjector:
        async def project_one(self) -> object:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("transient storage failure")
            service._stop_event.set()
            return SimpleNamespace(status="superseded")

    async def immediate_timeout(awaitable: object, *, timeout: float) -> object:
        assert timeout == 1.0
        awaitable.close()  # type: ignore[attr-defined]
        raise TimeoutError

    service._subject_workspace_projector = FailingOnceProjector()
    monkeypatch.setattr(
        "plugins.life_engine.service.core.asyncio.wait_for",
        immediate_timeout,
    )

    await service._subject_projection_loop()

    assert calls == 2
    assert service._subject_projection_health["status"] == "healthy"
    assert service._subject_projection_health["last_error_type"] == ""


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


async def test_memory_index_loop_drains_full_batches_without_poll_delay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _make_service(tmp_path)
    service._state.running = True
    service._stop_event = asyncio.Event()

    class BackloggedMemory(_FakeMemoryIndexService):
        async def run_index_worker(self, **_: object) -> object:
            self.run_calls += 1
            if self.run_calls == 1:
                return SimpleNamespace(
                    claimed=2,
                    completed=("job-1", "job-2"),
                    failed=(),
                    stale=(),
                )
            service._stop_event.set()
            return SimpleNamespace(claimed=0, completed=(), failed=(), stale=())

    fake_memory = BackloggedMemory()
    service._memory_service = fake_memory  # type: ignore[assignment]
    monkeypatch.setattr(
        service,
        "_memory_index_options",
        lambda: {
            "enabled": True,
            "interval_seconds": 3600,
            "batch_size": 2,
            "run_on_startup": True,
            "retry_failed": False,
            "reclaim_after_seconds": 60,
        },
    )

    await asyncio.wait_for(service._memory_index_loop(), timeout=1.0)

    assert fake_memory.run_calls == 2


async def test_advance_memory_projection_builds_digest_and_advances(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """回归：_advance_memory_projection 必须把多字段拼成单个字符串再 digest。

    此前把多个位置参数直接传给 _stable_text_digest(text)，导致
    "takes 1 positional argument but 9 were given"，投影永远无法推进。
    """
    service = _make_service(tmp_path)

    advanced: list[dict[str, object]] = []

    class _FakeBridge:
        enabled = True
        node_id = "test-node"

        async def advance_projection(self, **kwargs: object) -> object:
            advanced.append(kwargs)
            return SimpleNamespace(projection_name=kwargs["projection_name"])

    service._multi_writer_bridge = _FakeBridge()  # type: ignore[assignment]

    report = SimpleNamespace(
        claimed=4,
        completed=["a", "b"],
        failed=["c"],
        stale=[],
        model_name="mimo-v2.5",
        dimension=1024,
    )

    await service._advance_memory_projection(report)

    assert len(advanced) == 1
    call = advanced[0]
    assert call["projection_name"] == "memory_index"
    assert call["expected_frontier"] == 0
    assert call["next_frontier"] == 1
    assert isinstance(call["source_digest"], str) and call["source_digest"]
    assert isinstance(call["config_digest"], str) and call["config_digest"]
    assert call["source_digest"] != call["config_digest"]
    # digest 应为 sha256 hex（64 字符）
    assert len(call["source_digest"]) == 64
    assert len(call["config_digest"]) == 64
    assert service._projection_frontier == 1


async def test_advance_memory_projection_config_digest_stable_on_empty_batches(
    tmp_path: Path,
) -> None:
    """空批次（report 无 model/dimension）时 config_digest 必须与真实批次一致。

    此前 config_digest 直接用 report.model_name/dimension，空批次（claimed=0
    或全 stale）时为空/0，与有 embedding 的批次 digest 不同，导致投影推进
    被 ProjectionProgressConflict 永久拒绝（"节点进度保持在 N"）。
    """
    service = _make_service(tmp_path)
    advanced: list[dict[str, object]] = []

    class _FakeBridge:
        enabled = True
        node_id = "test-node"

        async def advance_projection(self, **kwargs: object) -> object:
            advanced.append(kwargs)
            return SimpleNamespace(projection_name=kwargs["projection_name"])

    class _FakeMemory:
        async def read_chunk_index_state(self) -> object:
            return SimpleNamespace(model_name="mimo-v2.5", dimension=1024)

    service._multi_writer_bridge = _FakeBridge()  # type: ignore[assignment]
    service._memory_service = _FakeMemory()  # type: ignore[assignment]

    empty_report = SimpleNamespace(claimed=0, completed=(), failed=(), stale=())
    await service._advance_memory_projection(empty_report)
    digest_empty = advanced[0]["config_digest"]
    assert isinstance(digest_empty, str) and digest_empty

    real_report = SimpleNamespace(
        claimed=4,
        completed=["a"],
        failed=[],
        stale=[],
        model_name="mimo-v2.5",
        dimension=1024,
    )
    await service._advance_memory_projection(real_report)
    digest_real = advanced[1]["config_digest"]

    # 空批次与真实批次的 config_digest 必须一致（来源稳定）。
    assert digest_empty == digest_real


async def test_advance_memory_projection_skips_when_bridge_disabled(
    tmp_path: Path,
) -> None:
    """bridge 未启用时投影推进应直接跳过。"""
    service = _make_service(tmp_path)
    service._multi_writer_bridge = SimpleNamespace(  # type: ignore[assignment]
        enabled=False,
        advance_projection=AsyncMock(),
    )
    report = SimpleNamespace(claimed=0, completed=(), failed=(), stale=())
    await service._advance_memory_projection(report)
    service._multi_writer_bridge.advance_projection.assert_not_awaited()  # type: ignore[attr-defined]


def _mysql_2013() -> OperationalError:
    """构造与线上一致的 MySQL 2013 瞬时断连（FRP 隧道抖动）。"""

    return OperationalError(
        "SELECT meta_value FROM raw_event_ledger_meta WHERE meta_key = %s",
        {},
        Exception(
            2013,
            "Lost connection to MySQL server during query ([WinError 121])",
        ),
    )


@pytest.mark.asyncio
async def test_heartbeat_loop_degrades_transient_mysql_disconnect_before_escalation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """心跳模型瞬时 MySQL 2013 断连：阈值内降级，不刷 ERROR traceback。

    回归线上缺陷（2026-08-13）：FRP 隧道抖动时每轮心跳失败都打完整
    traceback 刷屏。修复后前 8 次瞬时失败只记 debug/warning，第 9 次
    才升级 ERROR（对齐 memory_witness 的竞争降级先例）。
    """

    from plugins.life_engine.service import core as core_module
    from plugins.life_engine.service.core import (
        _HEARTBEAT_TRANSIENT_ERROR_ESCALATION_COUNT,
    )

    service = _make_service(tmp_path)
    service._state.running = True
    service._stop_event = asyncio.Event()
    calls = {"n": 0}

    async def fake_round(**_: object) -> tuple[str, SimpleNamespace]:
        calls["n"] += 1
        if calls["n"] <= _HEARTBEAT_TRANSIENT_ERROR_ESCALATION_COUNT:
            raise _mysql_2013()
        service._state.running = False
        return "reply", SimpleNamespace(content="ok")

    records = {"error": [], "warning": [], "debug": []}

    class _FakeLogger:
        def error(self, message: object) -> None:
            records["error"].append(str(message))

        def warning(self, message: object) -> None:
            records["warning"].append(str(message))

        def debug(self, message: object) -> None:
            records["debug"].append(str(message))

        def info(self, message: object) -> None:
            del message

    monkeypatch.setattr(core_module, "logger", _FakeLogger())
    monkeypatch.setattr(
        core_module,
        "log_error",
        lambda event, error, **kw: records["error"].append(str(error)),
    )
    monkeypatch.setattr(service, "_run_heartbeat_round", fake_round)
    monkeypatch.setattr(service, "_effective_heartbeat_interval", lambda: 0)
    monkeypatch.setattr(service, "_in_sleep_window_now", lambda: (False, "test"))
    monkeypatch.setattr(
        service, "_self_pause_status", lambda: (False, None, None, None)
    )

    await asyncio.wait_for(service._heartbeat_loop(), timeout=3)

    # 阈值内（1..N-1 次）失败后第 N 次成功退出。
    assert calls["n"] == _HEARTBEAT_TRANSIENT_ERROR_ESCALATION_COUNT + 1
    assert records["error"], "第 9 次瞬时失败应升级 ERROR"
    # logger.error 的完整消息带"瞬时数据库断连"前缀（log_error 的是裸异常串）。
    assert any(
        "瞬时数据库断连" in message for message in records["error"]
    ), "ERROR 必须包含瞬时断连降级路径的输出（不能是普通异常路径）"


@pytest.mark.asyncio
async def test_heartbeat_loop_transient_mysql_disconnect_recovers_without_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """瞬时 MySQL 断连在阈值内恢复：全程无 ERROR，成功清零计数。"""

    from plugins.life_engine.service import core as core_module

    service = _make_service(tmp_path)
    service._state.running = True
    service._stop_event = asyncio.Event()
    calls = {"n": 0}

    async def fake_round(**_: object) -> tuple[str, SimpleNamespace]:
        calls["n"] += 1
        if calls["n"] <= 2:
            raise _mysql_2013()
        service._state.running = False
        return "reply", SimpleNamespace(content="ok")

    records = {"error": [], "warning": [], "debug": []}

    class _FakeLogger:
        def error(self, message: object) -> None:
            records["error"].append(str(message))

        def warning(self, message: object) -> None:
            records["warning"].append(str(message))

        def debug(self, message: object) -> None:
            records["debug"].append(str(message))

        def info(self, message: object) -> None:
            del message

    monkeypatch.setattr(core_module, "logger", _FakeLogger())
    monkeypatch.setattr(
        core_module,
        "log_error",
        lambda event, error, **kw: records["error"].append(str(error)),
    )
    monkeypatch.setattr(service, "_run_heartbeat_round", fake_round)
    monkeypatch.setattr(service, "_effective_heartbeat_interval", lambda: 0)
    monkeypatch.setattr(service, "_in_sleep_window_now", lambda: (False, "test"))
    monkeypatch.setattr(
        service, "_self_pause_status", lambda: (False, None, None, None)
    )

    await asyncio.wait_for(service._heartbeat_loop(), timeout=3)

    assert calls["n"] == 3
    assert not records["error"], "阈值内恢复的瞬时断连不应产生 ERROR"
    assert records["warning"], "首次瞬时断连应留下 warning"
    assert all("瞬时数据库断连" in message for message in records["warning"])
