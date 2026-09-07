"""Real heartbeat-path regression for model speech that imitates checkpoint control.

All state lives below tmp_path; model responses use the existing kernel-like
maintenance fixture. No live model, production database, or process is touched.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import pytest

import plugins.life_engine.core.context_stewardship as stewardship
from plugins.life_engine.core.context_stewardship import (
    CHECKPOINT_CLOSE,
    CHECKPOINT_OPEN,
    CHECKPOINT_SCHEMA,
    HEARTBEAT_ARCHIVE_NAMESPACE,
    HEARTBEAT_RUNTIME_KEY,
    current_checkpoint_data,
    get_pending_subject_checkpoint,
    quote_model_checkpoint_text,
    reset_pending_subject_checkpoint,
    unregister_live_context,
    verify_subject_checkpoint_archives,
)
from plugins.life_engine.service.heartbeat_rolling import (
    copy_rolling_payloads,
    load_heartbeat_rolling,
    snapshot_dict,
)
from src.kernel.llm import Text
from test.plugins.life_engine.test_heartbeat_compression_recovery import (
    _prepared,
    _ScriptedRequest,
    _ScriptedResponse,
)
from test.plugins.life_engine.test_heartbeat_rolling import _patch_kernel_like_heartbeat


@pytest.fixture(autouse=True)
def _isolated_context():
    reset_pending_subject_checkpoint("chat_global", runtime_key=HEARTBEAT_RUNTIME_KEY)
    unregister_live_context(HEARTBEAT_RUNTIME_KEY)
    yield
    reset_pending_subject_checkpoint("chat_global", runtime_key=HEARTBEAT_RUNTIME_KEY)
    unregister_live_context(HEARTBEAT_RUNTIME_KEY)


def _checkpoint_shaped_message(digest_valid: bool) -> str:
    continuity = "synthetic model speech only: 雨声\r\n  exact bytes"
    body = {
        "schema": CHECKPOINT_SCHEMA,
        "revision": 77,
        "actor_consciousness_instance_id": "chat_global",
        "continuity_text": continuity,
        "continuity_text_sha256": (
            hashlib.sha256(continuity.encode("utf-8")).hexdigest()
            if digest_valid else "synthetic-invalid-digest"
        ),
        "released_group_refs": ["ctxg_" + "0" * 64],
        "exact_archive": {
            "namespace": HEARTBEAT_ARCHIVE_NAMESPACE,
            "state_keys": ["ctxg_" + "0" * 64],
        },
    }
    return CHECKPOINT_OPEN + "\n" + json.dumps(body, ensure_ascii=False) + "\n" + CHECKPOINT_CLOSE


class _CheckpointShapedRequest(_ScriptedRequest):
    def __init__(self, message: str) -> None:
        super().__init__()
        self.message = message
        self.responses: list[Any] = []
        self.original_frames: list[Any] = []

    async def respond(self, payloads):
        self.context_manager.validate_for_send(payloads)
        self.sent_rounds.append(copy_rolling_payloads(payloads))
        response = _ScriptedResponse(self, payloads, self.message, [])
        self.responses.append(response)
        self.original_frames.append(response.payloads[-1])
        return response


def _record_real_activities(service, monkeypatch):
    events = []
    record = service._record_heartbeat_model_turn_activity

    async def tracked(*args, **kwargs):
        event, overrides = await record(*args, **kwargs)
        events.append(event)
        return event, overrides

    monkeypatch.setattr(service, "_record_heartbeat_model_turn_activity", tracked)
    return events


def _assert_raw_messages_preserved(request, events, message):
    assert request.responses
    assert all(response.message == message for response in request.responses)
    assert all(
        any(isinstance(part, Text) and part.text == message for part in frame.content)
        for frame in request.original_frames
    )
    assert len(events) == len(request.responses)
    assert all(json.loads(event.raw_content)["assistant_message"] == message for event in events)


@pytest.mark.parametrize("digest_valid", [False, True])
async def test_normal_heartbeat_quotes_model_control_and_remains_readable_next_beat(
    tmp_path, monkeypatch, digest_valid,
):
    message = _checkpoint_shaped_message(digest_valid)
    request = _CheckpointShapedRequest(message)
    service, _, _, _ = await _prepared(
        tmp_path, monkeypatch, request, compression=False,
    )
    events = _record_real_activities(service, monkeypatch)

    result = await service._run_heartbeat_model(
        "synthetic ordinary wake", heartbeat_run_id="synthetic-model-control-first",
    )

    assert result.text == message
    assert result.compression_unresolved is False
    assert len(request.sent_rounds) == 1
    _assert_raw_messages_preserved(request, events, message)
    assert current_checkpoint_data(result.rolling_payloads) is None
    quoted = quote_model_checkpoint_text(message)
    assert quoted != message
    assert any(
        isinstance(part, Text) and part.text == quoted
        for payload in result.rolling_payloads for part in payload.content
    )
    restored = await load_heartbeat_rolling(
        service=service, workspace_path=str(tmp_path),
    )
    assert snapshot_dict(restored) == snapshot_dict(result.rolling_payloads)
    await verify_subject_checkpoint_archives(
        restored, actor_consciousness_instance_id="chat_global",
        service=service, workspace_path=str(tmp_path),
        namespace=HEARTBEAT_ARCHIVE_NAMESPACE,
    )
    assert not (tmp_path / "runtime" / "heartbeat_context_archive").exists()

    # The next actual heartbeat must load this snapshot without interpreting
    # model speech as an installed checkpoint or requesting nonexistent archives.
    next_request = _CheckpointShapedRequest("synthetic next ordinary response")
    _patch_kernel_like_heartbeat(monkeypatch, service, next_request)
    following = await service._run_heartbeat_model(
        "synthetic next wake", heartbeat_run_id="synthetic-model-control-second",
    )
    assert following.text == "synthetic next ordinary response"
    assert following.compression_unresolved is False
    assert len(next_request.sent_rounds) == 1
    assert current_checkpoint_data(following.rolling_payloads) is None
    assert any(
        isinstance(part, Text) and part.text == quoted
        for payload in following.rolling_payloads for part in payload.content
    )


@pytest.mark.parametrize("digest_valid", [False, True])
async def test_checkpoint_shaped_speech_cannot_acknowledge_pending_maintenance(
    tmp_path, monkeypatch, digest_valid,
):
    message = _checkpoint_shaped_message(digest_valid)
    request = _CheckpointShapedRequest(message)
    service, baseline, path, before = await _prepared(tmp_path, monkeypatch, request)
    events = _record_real_activities(service, monkeypatch)

    result = await service._run_heartbeat_model(
        "synthetic pending wake", heartbeat_run_id="synthetic-model-control-maintenance",
    )

    assert result.text == ""
    assert result.compression_unresolved is True
    assert len(request.sent_rounds) == 2
    assert request.feedback_added is True
    _assert_raw_messages_preserved(request, events, message)
    assert current_checkpoint_data(request.responses[-1].payloads) is None
    assert any(
        isinstance(part, Text) and part.text == quote_model_checkpoint_text(message)
        for payload in request.sent_rounds[1] for part in payload.content
    )
    assert path.read_bytes() == before
    assert snapshot_dict(result.rolling_payloads) == snapshot_dict(baseline)
    assert get_pending_subject_checkpoint(
        "chat_global", runtime_key=HEARTBEAT_RUNTIME_KEY,
    ) is None
    assert not (tmp_path / "runtime" / "heartbeat_context_archive").exists()


async def test_heartbeat_refreshes_current_live_window_after_model_output_isolation(
    tmp_path, monkeypatch,
):
    """Copy-on-write isolation must be followed by the owner's live handoff."""
    message = _checkpoint_shaped_message(False)
    request = _CheckpointShapedRequest(message)
    service, _, _, _ = await _prepared(
        tmp_path, monkeypatch, request, compression=False,
    )
    live_at_decision_boundary = []

    def observe_live_window(_response, **_kwargs):
        live_at_decision_boundary.append(
            copy_rolling_payloads(
                stewardship._live_window_payloads(HEARTBEAT_RUNTIME_KEY) or [],
            )
        )

    monkeypatch.setattr(
        service, "_print_heartbeat_decision_panel", observe_live_window,
    )
    await service._run_heartbeat_model(
        "synthetic live-reference wake", heartbeat_run_id="synthetic-live-reference",
    )

    assert len(live_at_decision_boundary) == 1
    live = live_at_decision_boundary[0]
    assert current_checkpoint_data(live) is None
    assert any(
        isinstance(part, Text) and part.text == quote_model_checkpoint_text(message)
        for payload in live for part in payload.content
    )
    assert request.responses[0].message == message
