"""Heartbeat append-only rolling context and compression-turn contracts."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from plugins.life_engine.core.config import LifeEngineConfig
from plugins.life_engine.core.context_stewardship import (
    ARCHIVE_NAMESPACE,
    CHATTER_RUNTIME_KEY,
    COMPRESSION_REQUIRED_OPEN,
    HEARTBEAT_ARCHIVE_NAMESPACE,
    HEARTBEAT_RUNTIME_KEY,
    archive_target_for_runtime,
    ensure_compression_required_appended,
    has_compression_required_payload,
    install_fail_closed_context_hook,
    is_subject_window_overflow_error,
)
from plugins.life_engine.service.core import LifeEngineService
from plugins.life_engine.service.event_builder import EventType, LifeEngineEvent
from plugins.life_engine.service.heartbeat_rolling import (
    HEARTBEAT_QUIET_TURN_TEXT,
    copy_rolling_payloads,
    ensure_heartbeat_user_turn,
    estimate_payload_chars,
    format_new_events_text,
    format_visible_event,
    load_heartbeat_rolling,
    save_heartbeat_rolling,
    snapshot_dict,
)
from src.core.config.core_config import CoreConfig
from src.kernel.llm import ROLE, LLMPayload, Text
from src.kernel.llm.context import LLMContextManager
from src.kernel.llm.exceptions import LLMContextError
from src.kernel.llm.model_client.registry import ModelClientRegistry
from src.kernel.llm.request import LLMRequest


async def _no_maintenance() -> None:
    return None


class _DummyPlugin:
    def __init__(self, config: LifeEngineConfig) -> None:
        self.config = config
        self.global_storage_config = CoreConfig(
            storage=CoreConfig.StorageSection(backend="local")
        )


def _make_service(tmp_path: Path) -> LifeEngineService:
    config = LifeEngineConfig()
    config.settings.enabled = True
    config.settings.workspace_path = str(tmp_path)
    return LifeEngineService(_DummyPlugin(config))


def _message_event(content: str, *, event_id: str = "evt-1") -> LifeEngineEvent:
    return LifeEngineEvent(
        event_id=event_id,
        event_type=EventType.MESSAGE,
        timestamp="2026-09-04T00:00:00+08:00",
        sequence=1,
        source="qq",
        source_detail="test",
        content=content,
        sender="Ayer",
        stream_id="stream-1",
    )


_SUFFIX_MARKERS = (
    "<transient_world_perception>",
    "<opportunity_page>",
    "<heartbeat_status>",
    "<capability_catalog>",
    "<mechanical_context_omission>",
)


def test_visible_event_text_does_not_dump_protocol_json() -> None:
    event = LifeEngineEvent(
        event_id="act-1",
        event_type=EventType.CONSCIOUS_ACTIVITY,
        timestamp="2026-09-04T00:00:00+08:00",
        sequence=2,
        source="life_engine",
        source_detail="test",
        content="",
        raw_content='{"thought":"我想起那句话","schema":"life.conscious_activity.model_turn.v1"}',
    )
    text = format_visible_event(event)
    assert text == "我想起那句话"
    assert "schema" not in text
    assert "life.conscious_activity" not in text


def test_format_new_events_joins_only_visible_bodies() -> None:
    events = [
        _message_event("你好", event_id="m1"),
        LifeEngineEvent(
            event_id="sum-1",
            event_type=EventType.SUMMARY,
            timestamp="2026-09-04T00:00:00+08:00",
            sequence=3,
            source="life_engine",
            source_detail="test",
            content="基础设施摘要不得进窗口",
        ),
    ]
    rendered = format_new_events_text(events)
    assert "你好" in rendered
    assert "基础设施摘要不得进窗口" not in rendered


async def test_heartbeat_rolling_round_trip_keeps_exact_user_bytes(
    tmp_path: Path,
) -> None:
    service = _make_service(tmp_path)
    original = [
        LLMPayload(ROLE.USER, [Text("第一拍")]),
        LLMPayload(ROLE.ASSISTANT, [Text("记下了")]),
    ]
    await save_heartbeat_rolling(
        original,
        service=service,
        workspace_path=str(tmp_path),
    )
    restored = await load_heartbeat_rolling(
        service=service,
        workspace_path=str(tmp_path),
    )
    assert snapshot_dict(restored) == snapshot_dict(original)


async def test_heartbeat_prepare_has_no_suffix_and_empty_when_quiet(
    tmp_path: Path,
) -> None:
    service = _make_service(tmp_path)
    quiet = await service._prepare_heartbeat_context()
    assert quiet.content == ""
    assert quiet.world_perception is None
    for marker in _SUFFIX_MARKERS:
        assert marker not in quiet.content

    event = service._event_builder.build_direct_message_event(
        "只应出现在本拍 USER",
        stream_id="stream-1",
        platform="qq",
        chat_type="private",
        sender_name="Ayer",
    )
    await service._queue_pending_event(event)
    prepared = await service._prepare_heartbeat_context()
    assert "只应出现在本拍 USER" in prepared.content
    assert "<subconscious_activity_projection" in prepared.content
    for marker in _SUFFIX_MARKERS:
        assert marker not in prepared.content


async def test_heartbeat_request_is_prefix_tools_rolling_without_suffix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "SOUL.md").write_text("SOUL_CONTENT", encoding="utf-8")
    (tmp_path / "USER.md").write_text("USER_CONTENT", encoding="utf-8")
    (tmp_path / "MEMORY.md").write_text("MEMORY_CONTENT", encoding="utf-8")
    service = _make_service(tmp_path)
    captured: dict[str, Any] = {}

    class _Response:
        def __init__(self, payloads: list[Any]) -> None:
            self.payloads = list(payloads)
            self.call_list: list[Any] = []
            self.request_record_id = "resp-1"

        def __await__(self):
            async def _done() -> str:
                return "quiet"

            return _done().__await__()

        def register_context_delivery(self, *_args: object, **_kwargs: object) -> None:
            return None

        def effective_context_receipt(self, delivery_id: str) -> object:
            return SimpleNamespace(
                delivery_id=delivery_id,
                exact_present=True,
                expected_utf8_bytes=1,
                effective_utf8_bytes=1,
                expected_sha256="a" * 64,
                effective_sha256="a" * 64,
            )

    class _Request:
        def __init__(self) -> None:
            self.payloads: list[Any] = []
            self.context_manager = None

        def add_payload(self, payload: Any) -> None:
            self.payloads.append(payload)

        def register_context_delivery(self, *_args: object, **_kwargs: object) -> None:
            return None

        async def send(self, *, stream: bool = False) -> _Response:
            del stream
            captured["payloads"] = list(self.payloads)
            return _Response(self.payloads)

    request = _Request()
    monkeypatch.setattr(
        "plugins.life_engine.service.core.create_llm_request",
        lambda **_kwargs: request,
    )
    monkeypatch.setattr(
        "plugins.life_engine.service.core.get_model_set_by_task",
        lambda _task: [
            {
                "model_identifier": "test-model",
                "timeout": 1.0,
                "max_context": 8_000,
                "max_tokens": 16,
            }
        ],
    )
    monkeypatch.setattr(service, "_run_learning_heartbeat_maintenance", _no_maintenance)
    monkeypatch.setattr(service, "record_tool_call", lambda *_a, **_k: None)
    monkeypatch.setattr(service, "record_tool_result", lambda *_a, **_k: None)

    baseline = [LLMPayload(ROLE.USER, [Text("旧滚动")])]
    await save_heartbeat_rolling(
        baseline,
        service=service,
        workspace_path=str(tmp_path),
    )
    event = service._event_builder.build_direct_message_event(
        "新经历",
        stream_id="stream-1",
        sender_name="Ayer",
    )
    await service._queue_pending_event(event)
    prepared = await service._prepare_heartbeat_context()
    result = await service._run_heartbeat_model(
        prepared.content,
        heartbeat_run_id="hb-test",
        heartbeat_deadline=None,
    )

    payloads = captured["payloads"]
    roles = [getattr(payload.role, "value", payload.role) for payload in payloads]
    assert roles[0] == ROLE.SYSTEM.value
    assert roles[1] == ROLE.TOOL.value
    rolling = payloads[2:]
    assert any(
        isinstance(part, Text) and "旧滚动" in part.text
        for payload in rolling
        for part in payload.content
        if hasattr(payload, "content")
    )
    joined = "\n".join(
        part.text
        for payload in payloads
        for part in getattr(payload, "content", [])
        if isinstance(part, Text)
    )
    for marker in _SUFFIX_MARKERS:
        assert marker not in joined
    restored = await load_heartbeat_rolling(
        service=service,
        workspace_path=str(tmp_path),
    )
    restored_text = str(snapshot_dict(restored))
    assert "旧滚动" in restored_text
    assert "新经历" in restored_text
    assert "<mechanical_context_omission>" not in restored_text
    assert result.compression_unresolved is False


async def test_quiet_heartbeat_does_not_rewrite_rolling_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "SOUL.md").write_text("SOUL_CONTENT", encoding="utf-8")
    (tmp_path / "USER.md").write_text("USER_CONTENT", encoding="utf-8")
    (tmp_path / "MEMORY.md").write_text("MEMORY_CONTENT", encoding="utf-8")
    service = _make_service(tmp_path)

    class _Response:
        def __init__(self, payloads: list[Any]) -> None:
            self.payloads = list(payloads)
            self.call_list: list[Any] = []
            self.request_record_id = "resp-quiet"

        def __await__(self):
            async def _done() -> str:
                return ""

            return _done().__await__()

        def register_context_delivery(self, *_args: object, **_kwargs: object) -> None:
            return None

        def effective_context_receipt(self, delivery_id: str) -> object:
            return SimpleNamespace(
                delivery_id=delivery_id,
                exact_present=True,
                expected_utf8_bytes=1,
                effective_utf8_bytes=1,
                expected_sha256="a" * 64,
                effective_sha256="a" * 64,
            )

    class _Request:
        def __init__(self) -> None:
            self.payloads: list[Any] = []
            self.context_manager = None

        def add_payload(self, payload: Any) -> None:
            self.payloads.append(payload)

        def register_context_delivery(self, *_args: object, **_kwargs: object) -> None:
            return None

        async def send(self, *, stream: bool = False) -> _Response:
            del stream
            return _Response(self.payloads)

    monkeypatch.setattr(
        "plugins.life_engine.service.core.create_llm_request",
        lambda **_kwargs: _Request(),
    )
    monkeypatch.setattr(
        "plugins.life_engine.service.core.get_model_set_by_task",
        lambda _task: [
            {
                "model_identifier": "test-model",
                "timeout": 1.0,
                "max_context": 8_000,
                "max_tokens": 16,
            }
        ],
    )
    monkeypatch.setattr(service, "_run_learning_heartbeat_maintenance", _no_maintenance)

    original = [LLMPayload(ROLE.USER, [Text("保持不动的滚动")])]
    await save_heartbeat_rolling(
        original,
        service=service,
        workspace_path=str(tmp_path),
    )
    before = snapshot_dict(original)
    await service._run_heartbeat_model(
        "",
        heartbeat_run_id="hb-quiet",
        heartbeat_deadline=None,
    )
    after = snapshot_dict(
        await load_heartbeat_rolling(
            service=service,
            workspace_path=str(tmp_path),
        )
    )
    assert after == before


def test_ensure_heartbeat_user_turn_repairs_empty_and_assistant_start() -> None:
    empty = ensure_heartbeat_user_turn([])
    assert len(empty) == 1
    assert empty[0].role == ROLE.USER
    assert empty[0].content[0].text == HEARTBEAT_QUIET_TURN_TEXT

    already = [LLMPayload(ROLE.USER, [Text("已有经历")])]
    assert ensure_heartbeat_user_turn(already) == already

    leading = [LLMPayload(ROLE.ASSISTANT, [Text("旧独白")])]
    repaired = ensure_heartbeat_user_turn(leading)
    assert repaired[0].role == ROLE.USER
    assert repaired[0].content[0].text == HEARTBEAT_QUIET_TURN_TEXT
    assert repaired[1:] == leading

    original = [LLMPayload(ROLE.ASSISTANT, [Text("旧独白")])]
    copied = copy_rolling_payloads(original)
    copied[0].content.append(Text("quiet-reply"))
    assert len(original[0].content) == 1
    assert original[0].content[0].text == "旧独白"

    manager = LLMContextManager()
    with pytest.raises(LLMContextError, match="对话不能以 assistant 开始"):
        manager.add_payload([], LLMPayload(ROLE.ASSISTANT, [Text("hi")]))
    legal = manager.add_payload(
        ensure_heartbeat_user_turn([]),
        LLMPayload(ROLE.ASSISTANT, [Text("hi")]),
    )
    assert [payload.role for payload in legal] == [ROLE.USER, ROLE.ASSISTANT]


class _KernelLikeHeartbeatResponse:
    def __init__(self, payloads: list[Any], context_manager: LLMContextManager) -> None:
        self.payloads = list(payloads)
        self.call_list: list[Any] = []
        self.request_record_id = "resp-kernel"
        self.context_manager = context_manager

    def __await__(self):
        async def _done() -> str:
            return "quiet-reply"

        return _done().__await__()

    def register_context_delivery(self, *_args: object, **_kwargs: object) -> None:
        return None

    def effective_context_receipt(self, delivery_id: str) -> object:
        return SimpleNamespace(
            delivery_id=delivery_id,
            exact_present=True,
            expected_utf8_bytes=1,
            effective_utf8_bytes=1,
            expected_sha256="a" * 64,
            effective_sha256="a" * 64,
        )


class _KernelLikeHeartbeatRequest:
    def __init__(self) -> None:
        self.payloads: list[Any] = []
        self.context_manager = LLMContextManager()
        self.sent_payloads: list[Any] = []

    def add_payload(self, payload: Any) -> None:
        self.payloads = self.context_manager.add_payload(self.payloads, payload)

    def register_context_delivery(self, *_args: object, **_kwargs: object) -> None:
        return None

    async def send(self, *, stream: bool = False) -> _KernelLikeHeartbeatResponse:
        del stream
        self.context_manager.validate_for_send(self.payloads)
        self.sent_payloads = list(self.payloads)
        response = _KernelLikeHeartbeatResponse(
            list(self.payloads),
            self.context_manager,
        )
        response.payloads = self.context_manager.add_payload(
            response.payloads,
            LLMPayload(ROLE.ASSISTANT, [Text("quiet-reply")]),
        )
        return response


def _patch_kernel_like_heartbeat(
    monkeypatch: pytest.MonkeyPatch,
    service: LifeEngineService,
    request: _KernelLikeHeartbeatRequest,
) -> None:
    monkeypatch.setattr(
        "plugins.life_engine.service.core.create_llm_request",
        lambda **_kwargs: request,
    )
    monkeypatch.setattr(
        "plugins.life_engine.service.core.get_model_set_by_task",
        lambda _task: [
            {
                "model_identifier": "test-model",
                "timeout": 1.0,
                "max_context": 8_000,
                "max_tokens": 16,
            }
        ],
    )
    monkeypatch.setattr(service, "_run_learning_heartbeat_maintenance", _no_maintenance)


async def test_quiet_empty_rolling_can_append_assistant_without_persisting_turn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "SOUL.md").write_text("SOUL_CONTENT", encoding="utf-8")
    (tmp_path / "USER.md").write_text("USER_CONTENT", encoding="utf-8")
    (tmp_path / "MEMORY.md").write_text("MEMORY_CONTENT", encoding="utf-8")
    service = _make_service(tmp_path)
    request = _KernelLikeHeartbeatRequest()
    _patch_kernel_like_heartbeat(monkeypatch, service, request)

    await service._run_heartbeat_model(
        "",
        heartbeat_run_id="hb-empty-quiet",
        heartbeat_deadline=None,
    )

    convo = [
        payload
        for payload in request.sent_payloads
        if getattr(payload, "role", None) not in {ROLE.SYSTEM, ROLE.TOOL}
    ]
    assert convo
    assert convo[0].role == ROLE.USER
    assert HEARTBEAT_QUIET_TURN_TEXT in convo[0].content[0].text
    joined = "\n".join(
        part.text
        for payload in request.sent_payloads
        for part in getattr(payload, "content", [])
        if isinstance(part, Text)
    )
    for marker in _SUFFIX_MARKERS:
        assert marker not in joined
    restored = await load_heartbeat_rolling(
        service=service,
        workspace_path=str(tmp_path),
    )
    assert restored == []
    assert HEARTBEAT_QUIET_TURN_TEXT not in str(snapshot_dict(restored))


async def test_quiet_assistant_leading_snapshot_can_append_assistant(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "SOUL.md").write_text("SOUL_CONTENT", encoding="utf-8")
    (tmp_path / "USER.md").write_text("USER_CONTENT", encoding="utf-8")
    (tmp_path / "MEMORY.md").write_text("MEMORY_CONTENT", encoding="utf-8")
    service = _make_service(tmp_path)
    request = _KernelLikeHeartbeatRequest()
    _patch_kernel_like_heartbeat(monkeypatch, service, request)

    original = [LLMPayload(ROLE.ASSISTANT, [Text("旧独白")])]
    await save_heartbeat_rolling(
        original,
        service=service,
        workspace_path=str(tmp_path),
    )
    await service._run_heartbeat_model(
        "",
        heartbeat_run_id="hb-assistant-leading",
        heartbeat_deadline=None,
    )
    convo_roles = [
        getattr(payload.role, "value", payload.role)
        for payload in request.sent_payloads
        if getattr(payload, "role", None) not in {ROLE.SYSTEM, ROLE.TOOL}
    ]
    assert convo_roles[0] == ROLE.USER.value
    assert ROLE.ASSISTANT.value in convo_roles
    after = snapshot_dict(
        await load_heartbeat_rolling(
            service=service,
            workspace_path=str(tmp_path),
        )
    )
    assert after == snapshot_dict(original)


def test_heartbeat_archive_namespace_is_isolated_from_chatter() -> None:
    heartbeat_ns, heartbeat_dir = archive_target_for_runtime(HEARTBEAT_RUNTIME_KEY)
    chatter_ns, chatter_dir = archive_target_for_runtime(CHATTER_RUNTIME_KEY)
    assert heartbeat_ns == HEARTBEAT_ARCHIVE_NAMESPACE
    assert chatter_ns == ARCHIVE_NAMESPACE
    assert heartbeat_ns != chatter_ns
    assert heartbeat_dir != chatter_dir


def test_over_trigger_appends_one_stable_compression_list() -> None:
    payloads = [
        LLMPayload(ROLE.USER, [Text("old-one")]),
        LLMPayload(ROLE.ASSISTANT, [Text("old-two")]),
        LLMPayload(ROLE.USER, [Text("current")]),
    ]
    first = ensure_compression_required_appended(
        payloads,
        estimate=estimate_payload_chars,
        trigger_chars=8,
    )
    second = ensure_compression_required_appended(
        first,
        estimate=estimate_payload_chars,
        trigger_chars=8,
    )
    assert has_compression_required_payload(first)
    assert second == first
    assert first[-1].content[0].text.startswith(COMPRESSION_REQUIRED_OPEN)
    assert "old-one" not in first[-1].content[0].text
    assert first[:-1] == payloads


def test_heartbeat_subject_checkpoint_keeps_continuity_text() -> None:
    from plugins.life_engine.core.context_stewardship import (
        CHECKPOINT_OPEN,
        apply_pending_subject_checkpoint,
        build_group_manifest,
        queue_subject_checkpoint,
        SubjectCheckpointCommand,
    )

    payloads = [
        LLMPayload(ROLE.USER, [Text("old-user-one")]),
        LLMPayload(ROLE.ASSISTANT, [Text("old-assistant-one")]),
        LLMPayload(ROLE.USER, [Text("old-user-two")]),
        LLMPayload(ROLE.ASSISTANT, [Text("old-assistant-two")]),
        LLMPayload(ROLE.USER, [Text("current-user")]),
    ]
    continuity = "这是心跳窗口里我自己写下的连续性。"
    manifest = build_group_manifest(payloads)
    command = SubjectCheckpointCommand(
        actor_consciousness_instance_id="chat_global",
        thought="窗口满了，我来收束。",
        continuity_text=continuity,
        source_manifest_sha256=manifest.source_manifest_sha256,
        expected_revision=manifest.current_checkpoint_revision,
        release_through_group_ref=manifest.groups[0].group_ref,
        retain_exact_group_refs=(),
    )
    queue_subject_checkpoint(command, runtime_key=HEARTBEAT_RUNTIME_KEY)
    result = apply_pending_subject_checkpoint(
        "chat_global",
        payloads,
        runtime_key=HEARTBEAT_RUNTIME_KEY,
        archive_namespace=HEARTBEAT_ARCHIVE_NAMESPACE,
    )
    rendered = str(result.payloads)
    assert result.triggered
    assert CHECKPOINT_OPEN in rendered
    assert continuity in rendered
    assert "old-user-one" not in rendered
    assert "<mechanical_context_omission>" not in rendered
    assert "current-user" in rendered


_WINDOW_OVERFLOW = LLMContextError(
    "subject rolling context exceeds the model window; "
    "mechanical group omission is not a success path. "
    "author_self_continuity_checkpoint must release groups first."
)


class _OverflowHeartbeatRequest:
    def __init__(self) -> None:
        self.payloads: list[Any] = []
        self.context_manager = LLMContextManager()

    def add_payload(self, payload: Any) -> None:
        self.payloads.append(payload)

    def register_context_delivery(self, *_args: object, **_kwargs: object) -> None:
        return None

    async def send(self, *, stream: bool = False) -> Any:
        del stream
        raise _WINDOW_OVERFLOW


async def test_heartbeat_window_overflow_skips_utility_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "SOUL.md").write_text("SOUL_CONTENT", encoding="utf-8")
    (tmp_path / "USER.md").write_text("USER_CONTENT", encoding="utf-8")
    (tmp_path / "MEMORY.md").write_text("MEMORY_CONTENT", encoding="utf-8")
    service = _make_service(tmp_path)
    created: list[str] = []

    def _create_request(**kwargs: Any) -> _OverflowHeartbeatRequest:
        created.append(str(kwargs.get("request_name") or ""))
        return _OverflowHeartbeatRequest()

    monkeypatch.setattr(
        "plugins.life_engine.service.core.create_llm_request",
        _create_request,
    )
    monkeypatch.setattr(
        "plugins.life_engine.service.core.get_model_set_by_task",
        lambda task: [
            {
                "model_identifier": f"test-{task}",
                "timeout": 1.0,
                "max_context": 8_000,
                "max_tokens": 16,
            }
        ],
    )
    monkeypatch.setattr(service, "_run_learning_heartbeat_maintenance", _no_maintenance)

    original = [
        LLMPayload(ROLE.USER, [Text("旧滚动必须留下")]),
        LLMPayload(ROLE.ASSISTANT, [Text("记下了")]),
        LLMPayload(ROLE.USER, [Text("第二组用户")]),
        LLMPayload(ROLE.ASSISTANT, [Text("第二组回复")]),
    ]
    await save_heartbeat_rolling(
        original,
        service=service,
        workspace_path=str(tmp_path),
    )
    result = await service._run_heartbeat_model(
        "超窗经历",
        heartbeat_run_id="hb-overflow",
        heartbeat_deadline=None,
    )

    assert result.compression_unresolved is True
    assert result.text == ""
    assert created == ["life_engine_heartbeat"]
    restored = await load_heartbeat_rolling(
        service=service,
        workspace_path=str(tmp_path),
    )
    assert has_compression_required_payload(restored)
    restored_text = str(snapshot_dict(restored))
    assert "旧滚动必须留下" in restored_text
    assert "超窗经历" not in restored_text


class _RecoveryTrimHeartbeatRequest(_KernelLikeHeartbeatRequest):
    def __init__(self) -> None:
        super().__init__()
        self.trimmed_payloads: list[Any] = []

    async def send(self, *, stream: bool = False) -> _KernelLikeHeartbeatResponse:
        self.trimmed_payloads = self.context_manager.maybe_trim(
            self.payloads,
            max_token_budget=6,
            token_counter=lambda items: len(items),
        )
        return await super().send(stream=stream)


async def test_heartbeat_hard_window_sends_recovery_projection_without_dropping_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "SOUL.md").write_text("SOUL_CONTENT", encoding="utf-8")
    (tmp_path / "USER.md").write_text("USER_CONTENT", encoding="utf-8")
    (tmp_path / "MEMORY.md").write_text("MEMORY_CONTENT", encoding="utf-8")
    service = _make_service(tmp_path)
    request = _RecoveryTrimHeartbeatRequest()
    _patch_kernel_like_heartbeat(monkeypatch, service, request)

    original: list[Any] = []
    for index in range(6):
        original.append(LLMPayload(ROLE.USER, [Text(f"old-user-{index}")]))
        original.append(LLMPayload(ROLE.ASSISTANT, [Text(f"old-asst-{index}")]))
    await save_heartbeat_rolling(
        original,
        service=service,
        workspace_path=str(tmp_path),
    )
    result = await service._run_heartbeat_model(
        "",
        heartbeat_run_id="hb-recovery",
        heartbeat_deadline=None,
    )

    assert request.trimmed_payloads
    assert "old-user-0" not in str(request.trimmed_payloads)
    assert has_compression_required_payload(request.trimmed_payloads)
    restored = await load_heartbeat_rolling(
        service=service,
        workspace_path=str(tmp_path),
    )
    restored_text = str(snapshot_dict(restored))
    assert "old-user-0" in restored_text
    assert "<mechanical_context_omission>" not in restored_text
    assert is_subject_window_overflow_error(_WINDOW_OVERFLOW)
    assert result.compression_unresolved is True


class _RecordingHeartbeatClient:
    def __init__(self) -> None:
        self.calls = 0
        self.sent_payloads: list[list[Any]] = []

    async def create(
        self,
        *,
        model_name: str,
        payloads: list[Any],
        tools: Any,
        request_name: str,
        model_set: Any,
        stream: bool,
    ) -> tuple[str, None, None]:
        del model_name, tools, request_name, model_set, stream
        self.calls += 1
        self.sent_payloads.append(list(payloads))
        return "example-heartbeat-reply", None, None


def _tiny_window_model(task: str) -> list[dict[str, Any]]:
    return [
        {
            "api_provider": "openai",
            "base_url": "https://example.invalid/v1",
            "model_identifier": f"example-{task}",
            "api_key": "sk-test",
            "client_type": "openai",
            "max_retry": 0,
            "timeout": 5.0,
            "retry_interval": 0.5,
            "price_in": 0.0,
            "price_out": 0.0,
            "temperature": 0.0,
            "max_tokens": 8,
            "max_context": 32,
            "context_tokens": 8,
            "tool_call_compat": False,
            "extra_params": {},
        }
    ]


async def test_example_two_oversize_heartbeats_use_kernel_send_without_looping(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """实际例子：走 LLMRequest.send() 的内核裁剪，连打两拍不得空转。"""

    (tmp_path / "SOUL.md").write_text("SOUL_CONTENT", encoding="utf-8")
    (tmp_path / "USER.md").write_text("USER_CONTENT", encoding="utf-8")
    (tmp_path / "MEMORY.md").write_text("MEMORY_CONTENT", encoding="utf-8")
    service = _make_service(tmp_path)

    oversized = [
        LLMPayload(ROLE.SYSTEM, Text("system")),
        LLMPayload(ROLE.USER, Text("old-user-0")),
        LLMPayload(ROLE.ASSISTANT, Text("old-asst-0")),
        LLMPayload(ROLE.USER, Text("old-user-1")),
        LLMPayload(ROLE.ASSISTANT, Text("old-asst-1")),
        LLMPayload(ROLE.USER, Text("current")),
        LLMPayload(ROLE.ASSISTANT, Text("latest")),
    ]
    fail_closed_manager = LLMContextManager()
    install_fail_closed_context_hook(
        SimpleNamespace(context_manager=fail_closed_manager)
    )
    with pytest.raises(LLMContextError, match="mechanical group omission"):
        fail_closed_manager.maybe_trim(
            oversized,
            max_token_budget=4,
            token_counter=lambda items: len(items),
        )
    print(
        "[example] 旧 fail-closed hook 会在内核裁剪时直接抛错，"
        "这就是 16:17 心跳空转的原因",
        flush=True,
    )

    created: list[str] = []
    clients: list[_RecordingHeartbeatClient] = []

    def _create_request(**kwargs: Any) -> LLMRequest:
        name = str(kwargs.get("request_name") or "")
        created.append(name)
        client = _RecordingHeartbeatClient()
        clients.append(client)
        request = LLMRequest(
            model_set=kwargs["model_set"],
            request_name=name,
            clients=ModelClientRegistry(openai=client),
        )
        request.enable_metrics = False
        return request

    monkeypatch.setattr(
        "plugins.life_engine.service.core.create_llm_request",
        _create_request,
    )
    monkeypatch.setattr(
        "plugins.life_engine.service.core.get_model_set_by_task",
        _tiny_window_model,
    )
    monkeypatch.setattr(
        "src.kernel.llm.request.count_payload_tokens",
        lambda payloads, model_identifier=None: len(list(payloads)),
    )
    monkeypatch.setattr(
        "src.kernel.llm.request._trajectory_settings",
        lambda: (False, str(tmp_path / "lake"), 0.05, 500, 3, 0),
    )
    monkeypatch.setattr(service, "_run_learning_heartbeat_maintenance", _no_maintenance)

    original: list[Any] = []
    for index in range(6):
        original.append(LLMPayload(ROLE.USER, [Text(f"old-user-{index}")]))
        original.append(LLMPayload(ROLE.ASSISTANT, [Text(f"old-asst-{index}")]))
    await save_heartbeat_rolling(
        original,
        service=service,
        workspace_path=str(tmp_path),
    )

    results = []
    for beat in range(2):
        result = await service._run_heartbeat_model(
            "本拍新经历不能在压缩未完成时写入",
            heartbeat_run_id=f"example-beat-{beat}",
            heartbeat_deadline=None,
        )
        results.append(result)
        sent = clients[beat].sent_payloads[0] if clients[beat].sent_payloads else []
        print(
            f"[example] beat {beat + 1}: "
            f"unresolved={result.compression_unresolved} "
            f"client_calls={clients[beat].calls} "
            f"sent_payloads={len(sent)} "
            f"omitted_old_user_0={('old-user-0' not in str(sent))} "
            f"request_names={created}",
            flush=True,
        )

    assert [item.compression_unresolved for item in results] == [True, True]
    assert created == ["life_engine_heartbeat", "life_engine_heartbeat"]
    assert all(client.calls == 1 for client in clients)
    assert "old-user-0" not in str(clients[0].sent_payloads[0])
    assert has_compression_required_payload(clients[0].sent_payloads[0])
    restored = await load_heartbeat_rolling(
        service=service,
        workspace_path=str(tmp_path),
    )
    restored_text = str(snapshot_dict(restored))
    assert "old-user-0" in restored_text
    assert "本拍新经历不能在压缩未完成时写入" not in restored_text
    assert "<mechanical_context_omission>" not in restored_text
    captured = capsys.readouterr()
    assert "旧 fail-closed hook" in captured.out
    assert "beat 2:" in captured.out


