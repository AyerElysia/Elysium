"""Content-free manifest diagnostics never repair or replay subject commands."""

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from plugins.life_engine.core import context_stewardship as stewardship
from src.kernel.llm import ROLE, LLMPayload, Text


def _conversation():
    return [
        LLMPayload(ROLE.USER, [Text("SYNTHETIC-PRIVATE-OLD-USER")]),
        LLMPayload(ROLE.ASSISTANT, [Text("SYNTHETIC-PRIVATE-OLD-ANSWER")]),
        LLMPayload(ROLE.USER, [Text("SYNTHETIC-PRIVATE-CURRENT-USER")]),
    ]


def _command(payloads):
    manifest = stewardship.build_group_manifest(payloads)
    return stewardship.SubjectCheckpointCommand(
        actor_consciousness_instance_id="synthetic-manifest-test",
        thought="SYNTHETIC-PRIVATE-THOUGHT",
        continuity_text="SYNTHETIC-PRIVATE-CONTINUITY\r\nexact bytes",
        source_manifest_sha256=manifest.source_manifest_sha256,
        expected_revision=manifest.current_checkpoint_revision,
        release_through_group_ref=manifest.groups[0].group_ref,
        retain_exact_group_refs=(),
    )


def _arguments(command):
    return {
        "thought": command.thought,
        "continuity_text": command.continuity_text,
        "source_manifest_sha256": command.source_manifest_sha256,
        "expected_revision": command.expected_revision,
        "release_through_group_ref": command.release_through_group_ref,
        "retain_exact_group_refs": list(command.retain_exact_group_refs),
    }


def _action(monkeypatch, payloads):
    action = stewardship.LifeAuthorSelfContinuityCheckpointAction.__new__(
        stewardship.LifeAuthorSelfContinuityCheckpointAction
    )
    action.plugin = SimpleNamespace(config=None, service=None)
    action._context_runtime_key = stewardship.HEARTBEAT_RUNTIME_KEY
    action._action_origin_extra = lambda: {
        "consciousness_instance_id": "synthetic-manifest-test"
    }
    monkeypatch.setattr(stewardship, "_live_window_payloads", lambda _: payloads)
    archive = AsyncMock()
    queue = Mock(return_value=False)
    monkeypatch.setattr(stewardship, "archive_context_groups", archive)
    monkeypatch.setattr(stewardship, "queue_subject_checkpoint", queue)
    return action, archive, queue


@pytest.mark.parametrize(
    "bad_hash",
    [
        "a" * 63,
        "a" * 65,
        "A" * 64,
        "g" * 64,
        " " + "a" * 64,
        "a" * 64 + "\n",
        "",
        "SYNTHETIC-PRIVATE-SUPPLIED-HASH",
        None,
        int("1" * 64),
    ],
)
def test_malformed_hash_is_not_misreported_as_stale_or_echoed(bad_hash):
    payloads = _conversation()
    original = _command(payloads)
    command = replace(original, source_manifest_sha256=bad_hash)
    before = repr(payloads)

    with pytest.raises(stewardship.ContextCheckpointManifestError) as caught:
        stewardship.prepare_subject_checkpoint(payloads, command)

    error = caught.value
    assert error.error_code == "source_manifest_sha256_invalid_format"
    assert str(error) == (
        "source_manifest_sha256 must be exactly 64 lowercase hexadecimal characters"
    )
    feedback = error.retry_feedback()
    data = json.loads(feedback)
    assert data["current_manifest"] == {
        "source_manifest_sha256": original.source_manifest_sha256,
        "current_checkpoint_revision": original.expected_revision,
    }
    assert "SYNTHETIC-PRIVATE" not in feedback
    assert data["technical_only"] is True
    assert data["subject_resubmission_required"] is True
    assert command.source_manifest_sha256 == bad_hash
    assert repr(payloads) == before


@pytest.mark.parametrize(
    ("changes", "error_code", "message"),
    [
        (
            {"source_manifest_sha256": "0" * 64},
            "source_manifest_mismatch",
            "context group manifest is stale or mismatched",
        ),
        (
            {"expected_revision": 1},
            "checkpoint_revision_conflict",
            "subject continuity checkpoint revision conflict",
        ),
    ],
)
def test_valid_format_stale_binding_and_revision_keep_exact_rejection(
    changes, error_code, message
):
    payloads = _conversation()
    with pytest.raises(stewardship.ContextStewardshipError, match=message) as caught:
        stewardship.prepare_subject_checkpoint(
            payloads, replace(_command(payloads), **changes)
        )
    assert isinstance(caught.value, stewardship.ContextCheckpointManifestError)
    assert caught.value.error_code == error_code
    assert str(caught.value) == message


def test_retry_identifiers_come_from_frozen_control_prefix_not_new_tail():
    payloads = _conversation()
    command = _command(payloads)
    with_control = stewardship.ensure_compression_required_appended(
        payloads, trigger_chars=1
    )
    extended = [
        *with_control,
        LLMPayload(ROLE.ASSISTANT, [Text("SYNTHETIC-PRIVATE-MAINTENANCE")]),
        LLMPayload(ROLE.USER, [Text("SYNTHETIC-PRIVATE-ARRIVAL")]),
    ]
    assert (
        stewardship.build_group_manifest(extended).source_manifest_sha256
        != command.source_manifest_sha256
    )
    with pytest.raises(stewardship.ContextCheckpointManifestError) as caught:
        stewardship.prepare_subject_checkpoint(
            extended, replace(command, source_manifest_sha256="a" * 63)
        )
    assert caught.value.source_manifest_sha256 == command.source_manifest_sha256
    assert caught.value.current_checkpoint_revision == command.expected_revision
    prepared = stewardship.prepare_subject_checkpoint(extended, command)
    assert prepared.command is command
    assert "SYNTHETIC-PRIVATE-ARRIVAL" in str(prepared.payloads)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", ["malformed", "whitespace", "non_string", "stale", "revision"]
)
async def test_action_returns_only_technical_feedback_without_archive_or_queue(
    monkeypatch, failure
):
    payloads = _conversation()
    command = _command(payloads)
    changes = {
        "malformed": {"source_manifest_sha256": "SYNTHETIC-PRIVATE-BAD-HASH"},
        "non_string": {"source_manifest_sha256": int("1" * 64)},
        "whitespace": {"source_manifest_sha256": " " + command.source_manifest_sha256},
        "stale": {"source_manifest_sha256": "0" * 64},
        "revision": {"expected_revision": command.expected_revision + 1},
    }[failure]
    submitted = replace(command, **changes)
    before = repr(payloads)
    action, archive, queue = _action(monkeypatch, payloads)

    ok, feedback = await action.execute(**_arguments(submitted))

    assert not ok
    data = json.loads(feedback)
    assert (
        data["current_manifest"]["source_manifest_sha256"]
        == command.source_manifest_sha256
    )
    assert (
        data["current_manifest"]["current_checkpoint_revision"]
        == command.expected_revision
    )
    assert data["subject_resubmission_required"] is True
    assert "SYNTHETIC-PRIVATE" not in feedback
    assert "No arguments have been corrected" in data["instruction"]
    assert "no command has been replayed" in data["instruction"]
    archive.assert_not_awaited()
    queue.assert_not_called()
    assert repr(payloads) == before


@pytest.mark.asyncio
async def test_only_new_valid_subject_call_archives_and_queues_exact_command(
    monkeypatch,
):
    payloads = _conversation()
    command = _command(payloads)
    action, archive, queue = _action(monkeypatch, payloads)
    malformed = replace(
        command, source_manifest_sha256=command.source_manifest_sha256[:-1]
    )

    ok, _ = await action.execute(**_arguments(malformed))
    assert not ok
    archive.assert_not_awaited()
    queue.assert_not_called()

    ok, detail = await action.execute(**_arguments(command))

    assert ok, detail
    archive.assert_awaited_once()
    queue.assert_called_once_with(
        command, runtime_key=stewardship.HEARTBEAT_RUNTIME_KEY
    )
    assert queue.call_args.args[0].thought == command.thought
    assert queue.call_args.args[0].continuity_text == command.continuity_text


def test_valid_hash_does_not_relax_release_boundary():
    payloads = _conversation()
    command = replace(_command(payloads), release_through_group_ref="ctxg_" + "0" * 64)
    with pytest.raises(stewardship.ContextStewardshipError, match="not in") as caught:
        stewardship.prepare_subject_checkpoint(payloads, command)
    assert not isinstance(caught.value, stewardship.ContextCheckpointManifestError)
