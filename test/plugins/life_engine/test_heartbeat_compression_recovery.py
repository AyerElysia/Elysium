"""Bounded heartbeat compression maintenance with synthetic local model rounds.

Reuse the kernel-like context protocol fixture, but exercise the real checkpoint
action, exact archive, durable install, and heartbeat loop in a temporary home.
No model client, runtime startup, or production storage is involved.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

import plugins.life_engine.service.core as service_module
from plugins.life_engine.core.context_stewardship import (
    COMPRESSION_REQUIRED_CLOSE,
    COMPRESSION_REQUIRED_OPEN,
    HEARTBEAT_ARCHIVE_NAMESPACE,
    HEARTBEAT_CHECKPOINT_FEEDBACK_TEXT,
    HEARTBEAT_RUNTIME_KEY,
    LifeAuthorSelfContinuityCheckpointAction,
    current_checkpoint_data,
    ensure_compression_required_appended,
    get_pending_subject_checkpoint,
    has_compression_required_payload,
    payloads_require_compression,
    read_context_group_archive,
    reset_pending_subject_checkpoint,
    strip_compression_maintenance_transport,
    unregister_live_context,
)
from plugins.life_engine.service.heartbeat_rolling import (
    HEARTBEAT_ROLLING_FILENAME,
    copy_rolling_payloads,
    estimate_payload_chars,
    load_heartbeat_rolling,
    rolling_payloads_only,
    save_heartbeat_rolling,
    snapshot_dict,
)
from src.kernel.llm import ROLE, LLMPayload, Text, ToolCall
from test.plugins.life_engine.test_heartbeat_rolling import (
    _KernelLikeHeartbeatRequest,
    _KernelLikeHeartbeatResponse,
    _make_service,
    _patch_kernel_like_heartbeat,
)

_FEEDBACK = "<context_checkpoint_feedback"
_CONTINUITY = "synthetic model-authored continuity: 雨声原文可继续回取。"
_NARRATIVE = "synthetic ordinary narrative, not a checkpoint"
_FINAL = "synthetic terminal response after checkpoint install"
_WAKE = "synthetic new experience remains pending until maintenance completes"


@pytest.fixture(autouse=True)
def _isolated_checkpoint_runtime():
    reset_pending_subject_checkpoint("chat_global", runtime_key=HEARTBEAT_RUNTIME_KEY)
    unregister_live_context(HEARTBEAT_RUNTIME_KEY)
    yield
    reset_pending_subject_checkpoint("chat_global", runtime_key=HEARTBEAT_RUNTIME_KEY)
    unregister_live_context(HEARTBEAT_RUNTIME_KEY)


class _ScriptedResponse(_KernelLikeHeartbeatResponse):
    def __init__(self, owner, payloads, message, calls):
        super().__init__(payloads, owner.context_manager)
        self.owner = owner
        self.message = message
        self.call_list = calls
        self.request_record_id = f"synthetic-response-{len(owner.sent_rounds)}"
        parts = ([Text(message)] if message else []) + calls
        self.add_payload(LLMPayload(ROLE.ASSISTANT, parts))

    def __await__(self):
        async def done():
            return self.message

        return done().__await__()

    def add_payload(self, payload):
        self.payloads = self.context_manager.add_payload(self.payloads, payload)
        if _FEEDBACK in str(payload):
            self.owner.feedback_added = True

    async def send(self, *, stream=False):
        del stream
        return await self.owner.respond(self.payloads)


class _ScriptedRequest(_KernelLikeHeartbeatRequest):
    def __init__(
        self, *, authors_checkpoint=False, followup_error=None,
        continuity_text=_CONTINUITY,
    ):
        super().__init__()
        self.authors_checkpoint = authors_checkpoint
        self.followup_error = followup_error
        self.continuity_text = continuity_text
        self.sent_rounds: list[list[Any]] = []
        self.feedback_added = False
        self.checkpoint_args: dict[str, Any] = {}

    async def send(self, *, stream=False):
        del stream
        return await self.respond(self.payloads)

    async def respond(self, payloads):
        self.context_manager.validate_for_send(payloads)
        turn = len(self.sent_rounds)
        self.sent_rounds.append(copy_rolling_payloads(payloads))
        if turn and self.followup_error is not None:
            raise self.followup_error
        calls = []
        message = _NARRATIVE
        if self.authors_checkpoint and turn == 1:
            assert self.feedback_added
            notices = [
                part.text
                for payload in payloads
                for part in payload.content
                if isinstance(part, Text)
                and part.text.startswith(COMPRESSION_REQUIRED_OPEN)
            ]
            assert len(notices) == 1
            manifest = json.loads(
                notices[0].removeprefix(COMPRESSION_REQUIRED_OPEN)
                .removesuffix(COMPRESSION_REQUIRED_CLOSE).strip()
            )
            self.checkpoint_args = {
                "thought": "synthetic model decision to release the first closed group",
                "continuity_text": self.continuity_text,
                "source_manifest_sha256": manifest["source_manifest_sha256"],
                "expected_revision": manifest["current_checkpoint_revision"],
                "release_through_group_ref": manifest["releaseable_groups_in_chronological_order"][0]["group_ref"],
                "retain_exact_group_refs": [],
            }
            calls = [
                ToolCall(
                    id="synthetic-checkpoint-call",
                    name="action-author_self_continuity_checkpoint",
                    args=dict(self.checkpoint_args),
                )
            ]
            message = ""
        elif self.authors_checkpoint and turn >= 2:
            assert (
                current_checkpoint_data(payloads)["continuity_text"]
                == self.continuity_text
            )
            message = _FINAL
        return _ScriptedResponse(self, payloads, message, calls)


async def _prepared(tmp_path, monkeypatch, request, *, compression=True):
    service = _make_service(tmp_path)
    service.plugin.service = service
    _patch_kernel_like_heartbeat(monkeypatch, service, request)

    async def synthetic_prompt():
        return "synthetic heartbeat subject prompt"

    monkeypatch.setattr(service, "_build_heartbeat_system_prompt", synthetic_prompt)
    monkeypatch.setattr(
        service, "_get_nucleus_tools",
        lambda: [LifeAuthorSelfContinuityCheckpointAction],
    )
    baseline = [
        LLMPayload(ROLE.USER, [Text("synthetic-old-first: 雨声\r\nexact bytes")]),
        LLMPayload(ROLE.ASSISTANT, [Text("synthetic old first response")]),
        LLMPayload(ROLE.USER, [Text("synthetic-old-second")]),
        LLMPayload(ROLE.ASSISTANT, [Text("synthetic old second response")]),
        LLMPayload(ROLE.USER, [Text("synthetic retained anchor")]),
    ]
    if compression:
        baseline = ensure_compression_required_appended(
            baseline, estimate=estimate_payload_chars, trigger_chars=120_000,
            force=True,
        )
        assert has_compression_required_payload(baseline)
    await save_heartbeat_rolling(
        baseline, service=service, workspace_path=str(tmp_path),
    )
    path = tmp_path / "runtime" / HEARTBEAT_ROLLING_FILENAME
    return service, baseline, path, path.read_bytes()


def _feedback_parts(payloads):
    return [
        (payload.role, part.text)
        for payload in payloads
        for part in payload.content
        if isinstance(part, Text) and _FEEDBACK in part.text
    ]


@pytest.mark.parametrize("with_control", [False, True])
def test_maintenance_cleanup_keeps_real_user_bytes_and_requires_a_control(
    with_control,
):
    real_text = "synthetic real USER bytes: 雨声\r\n  remains exact"
    similar_text = HEARTBEAT_CHECKPOINT_FEEDBACK_TEXT + "\nsynthetic user quotation"
    prefix = [
        LLMPayload(ROLE.USER, [Text("synthetic old group")]),
        LLMPayload(ROLE.ASSISTANT, [Text("synthetic old response")]),
        LLMPayload(ROLE.USER, [Text("synthetic current anchor")]),
    ]
    if with_control:
        prefix = ensure_compression_required_appended(
            prefix, estimate=estimate_payload_chars, trigger_chars=120_000,
            force=True,
        )
    mixed = LLMPayload(
        ROLE.USER,
        [Text(HEARTBEAT_CHECKPOINT_FEEDBACK_TEXT), Text(real_text), Text(similar_text)],
    )
    payloads = [
        *prefix,
        LLMPayload(ROLE.ASSISTANT, [Text("synthetic maintenance narrative")]),
        mixed,
    ]
    before = snapshot_dict(payloads)
    cleaned = strip_compression_maintenance_transport(payloads)
    assert snapshot_dict(payloads) == before
    if not with_control:
        assert snapshot_dict(cleaned) == before
        return
    assert has_compression_required_payload(cleaned) is False
    assert cleaned[-1].role == ROLE.USER
    assert [part.text for part in cleaned[-1].content] == [real_text, similar_text]
    assert cleaned[-1].content[0].text.encode("utf-8") == real_text.encode("utf-8")
    assert "synthetic maintenance narrative" not in str(snapshot_dict(cleaned))


async def test_narrative_receives_feedback_then_real_subject_checkpoint_installs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _ScriptedRequest(authors_checkpoint=True)
    service, _, path, before = await _prepared(tmp_path, monkeypatch, request)
    deadline = asyncio.get_running_loop().time() + 60.0
    stages = []
    recorded_turns = []
    original_await = service_module._await_with_heartbeat_deadline
    original_record = service._record_heartbeat_model_turn_activity

    async def tracked_await(factory, **kwargs):
        stages.append((kwargs["stage"], kwargs["deadline"]))
        return await original_await(factory, **kwargs)

    async def tracked_record(*args, **kwargs):
        result = await original_record(*args, **kwargs)
        recorded_turns.append(result[0])
        return result

    monkeypatch.setattr(service_module, "_await_with_heartbeat_deadline", tracked_await)
    monkeypatch.setattr(service, "_record_heartbeat_model_turn_activity", tracked_record)
    result = await service._run_heartbeat_model(
        _WAKE, heartbeat_run_id="synthetic-compression-recovery",
        heartbeat_deadline=deadline,
    )

    assert len(request.sent_rounds) == 3
    feedback = _feedback_parts(request.sent_rounds[1])
    assert len(feedback) == 1
    assert feedback[0][0] == ROLE.USER
    assert 'technical_only="true"' in feedback[0][1]
    assert "author_self_continuity_checkpoint" in feedback[0][1]
    assert _CONTINUITY not in feedback[0][1]
    assert "release_through_group_ref" not in feedback[0][1]
    assert result.compression_unresolved is False
    assert result.text == _FINAL
    assert len(recorded_turns) == 3
    assert _NARRATIVE in recorded_turns[0].raw_content
    assert all(value == deadline for _, value in stages)
    assert [stage for stage, _ in stages].count("followup_request") == 2

    restored = await load_heartbeat_rolling(
        service=service, workspace_path=str(tmp_path),
    )
    checkpoint = current_checkpoint_data(restored)
    assert checkpoint["continuity_text"] == _CONTINUITY
    assert checkpoint["revision"] == 1
    assert checkpoint["exact_archive"]["namespace"] == HEARTBEAT_ARCHIVE_NAMESPACE
    assert _WAKE in str(snapshot_dict(restored))
    assert "synthetic-old-first" not in str(snapshot_dict(restored))
    assert _FEEDBACK not in str(snapshot_dict(restored))
    assert path.read_bytes() != before
    archive = await read_context_group_archive(
        request.checkpoint_args["release_through_group_ref"],
        service=service, workspace_path=str(tmp_path),
        namespace=HEARTBEAT_ARCHIVE_NAMESPACE,
        local_subdir="heartbeat_context_archive",
    )
    assert "synthetic-old-first" in str(archive["record"])
    assert get_pending_subject_checkpoint(
        "chat_global", runtime_key=HEARTBEAT_RUNTIME_KEY,
    ) is None


async def test_partial_checkpoint_does_not_acknowledge_remaining_pressure_or_rollback(
    tmp_path, monkeypatch,
):
    continuity = _CONTINUITY + (" synthetic retained detail" * 100)
    request = _ScriptedRequest(
        authors_checkpoint=True, continuity_text=continuity,
    )
    service, _, path, before = await _prepared(tmp_path, monkeypatch, request)
    service._cfg().settings.max_rounds_per_heartbeat = 4
    service._cfg().settings.max_consecutive_tool_stalls_per_heartbeat = 2
    trigger = 512
    monkeypatch.setattr(service, "_heartbeat_compaction_trigger_chars", lambda: trigger)
    original_apply = service._apply_heartbeat_subject_checkpoint
    installed_baselines = []
    stop_events = []

    async def tracked_apply(response):
        installed = await original_apply(response)
        if installed:
            installed_baselines.append(
                copy_rolling_payloads(rolling_payloads_only(response.payloads))
            )
        return installed

    monkeypatch.setattr(service, "_apply_heartbeat_subject_checkpoint", tracked_apply)
    monkeypatch.setattr(
        service_module, "log_heartbeat_event", lambda **event: stop_events.append(event),
    )
    result = await service._run_heartbeat_model(
        _WAKE, heartbeat_run_id="synthetic-partial-checkpoint-maintenance",
    )

    assert len(installed_baselines) == 1
    assert len(request.sent_rounds) == 4
    assert has_compression_required_payload(request.sent_rounds[2])
    assert _feedback_parts(request.sent_rounds[3])
    assert result.compression_unresolved is True
    assert result.text == ""
    assert stop_events[-1]["stop_reason"] == "consecutive_tool_stalls"
    assert stop_events[-1]["model_turns"] == 4
    installed = installed_baselines[0]
    assert payloads_require_compression(
        installed, estimate=estimate_payload_chars, trigger_chars=trigger,
    )
    max_groups, max_bytes = service._heartbeat_compaction_list_limits()
    expected = ensure_compression_required_appended(
        installed, estimate=estimate_payload_chars, trigger_chars=trigger,
        max_groups=max_groups, max_bytes=max_bytes,
    )
    restored = await load_heartbeat_rolling(
        service=service, workspace_path=str(tmp_path),
    )
    assert snapshot_dict(restored) == snapshot_dict(expected)
    assert snapshot_dict(result.rolling_payloads) == snapshot_dict(expected)
    checkpoint = current_checkpoint_data(restored)
    assert checkpoint["continuity_text"] == continuity
    assert checkpoint["revision"] == 1
    assert has_compression_required_payload(restored)
    assert "synthetic-old-first" not in str(snapshot_dict(restored))
    assert "synthetic-old-second" in str(snapshot_dict(restored))
    assert _WAKE in str(snapshot_dict(restored))
    assert _FINAL not in str(snapshot_dict(restored))
    assert path.read_bytes() != before
    assert get_pending_subject_checkpoint(
        "chat_global", runtime_key=HEARTBEAT_RUNTIME_KEY,
    ) is None


@pytest.mark.parametrize(
    ("max_rounds", "stall_limit", "expected_calls", "reason"),
    [(2, 5, 2, "max_model_turns"), (5, 2, 2, "consecutive_tool_stalls")],
)
async def test_narrative_only_maintenance_is_bounded_and_preserves_exact_old_bytes(
    tmp_path, monkeypatch, max_rounds, stall_limit, expected_calls, reason,
):
    request = _ScriptedRequest()
    service, baseline, path, before = await _prepared(tmp_path, monkeypatch, request)
    service._cfg().settings.max_rounds_per_heartbeat = max_rounds
    service._cfg().settings.max_consecutive_tool_stalls_per_heartbeat = stall_limit
    stop_events = []
    monkeypatch.setattr(
        service_module, "log_heartbeat_event", lambda **event: stop_events.append(event),
    )

    result = await service._run_heartbeat_model(
        _WAKE, heartbeat_run_id="synthetic-bounded-maintenance",
    )

    assert len(request.sent_rounds) == expected_calls
    assert _feedback_parts(request.sent_rounds[1])
    assert result.compression_unresolved is True
    assert result.text == ""
    assert path.read_bytes() == before
    assert snapshot_dict(result.rolling_payloads) == snapshot_dict(baseline)
    assert _WAKE not in str(snapshot_dict(result.rolling_payloads))
    assert _NARRATIVE not in str(snapshot_dict(result.rolling_payloads))
    assert not current_checkpoint_data(result.rolling_payloads)
    assert stop_events[-1]["stop_reason"] == reason
    assert stop_events[-1]["last_round_tools"] == [
        "context_checkpoint_required:no_tool_call",
    ]


async def test_normal_quiet_turn_ends_immediately_without_compression_feedback(
    tmp_path, monkeypatch,
):
    request = _ScriptedRequest()
    service, baseline, path, before = await _prepared(
        tmp_path, monkeypatch, request, compression=False,
    )
    result = await service._run_heartbeat_model(
        "", heartbeat_run_id="synthetic-normal-quiet",
    )

    assert len(request.sent_rounds) == 1
    assert request.feedback_added is False
    assert result.compression_unresolved is False
    assert result.text == _NARRATIVE
    assert path.read_bytes() == before
    assert snapshot_dict(result.rolling_payloads) == snapshot_dict(baseline)


async def test_compression_followup_cancellation_propagates_without_changing_old_bytes(
    tmp_path, monkeypatch,
):
    cancellation = asyncio.CancelledError("synthetic compression cancellation")
    request = _ScriptedRequest(followup_error=cancellation)
    service, _, path, before = await _prepared(tmp_path, monkeypatch, request)

    with pytest.raises(asyncio.CancelledError) as raised:
        await service._run_heartbeat_model(
            _WAKE, heartbeat_run_id="synthetic-cancelled-maintenance",
        )

    assert raised.value is cancellation
    assert len(request.sent_rounds) == 2
    assert request.feedback_added is True
    assert path.read_bytes() == before


async def test_compression_followup_uses_exhausted_shared_deadline_without_sending(
    tmp_path, monkeypatch,
):
    request = _ScriptedRequest()
    service, _, path, before = await _prepared(tmp_path, monkeypatch, request)
    original_remaining = service_module._heartbeat_remaining_seconds
    stop_events = []

    def remaining(deadline, **kwargs):
        if request.feedback_added:
            return -1.0
        return original_remaining(deadline, **kwargs)

    monkeypatch.setattr(service_module, "_heartbeat_remaining_seconds", remaining)
    monkeypatch.setattr(
        service_module, "log_heartbeat_event", lambda **event: stop_events.append(event),
    )
    result = await service._run_heartbeat_model(
        _WAKE, heartbeat_run_id="synthetic-expired-maintenance",
        heartbeat_deadline=asyncio.get_running_loop().time() + 60.0,
    )

    assert len(request.sent_rounds) == 1
    assert request.feedback_added is True
    assert result.compression_unresolved is True
    assert result.text == ""
    assert path.read_bytes() == before
    assert stop_events[-1]["stop_reason"] == "deadline_exhausted"
    assert stop_events[-1]["stop_stage"] == "followup_request"


async def test_already_expired_deadline_raises_before_any_model_send(
    tmp_path, monkeypatch,
):
    request = _ScriptedRequest()
    service, _, path, before = await _prepared(tmp_path, monkeypatch, request)

    with pytest.raises(service_module.HeartbeatBudgetExhausted) as raised:
        await service._run_heartbeat_model(
            _WAKE, heartbeat_run_id="synthetic-initial-expired-maintenance",
            heartbeat_deadline=asyncio.get_running_loop().time() - 1.0,
        )

    assert raised.value.stage == "initial_request"
    assert request.sent_rounds == []
    assert path.read_bytes() == before
