"""Literal tag citations are subject text, not additional JSON control frames."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from plugins.life_engine.core.context_stewardship import (
    CHECKPOINT_CLOSE,
    CHECKPOINT_OPEN,
    CHECKPOINT_SCHEMA,
    HEARTBEAT_ARCHIVE_NAMESPACE,
    HEARTBEAT_RUNTIME_KEY,
    LifeAuthorSelfContinuityCheckpointAction,
    build_group_manifest,
    checkpoint_data,
    current_checkpoint_data,
    get_pending_subject_checkpoint,
    prepare_subject_checkpoint,
    reset_pending_subject_checkpoint,
    unregister_live_context,
)
from plugins.life_engine.service.heartbeat_rolling import load_heartbeat_rolling
from src.kernel.llm import ROLE, LLMPayload, Text
from test.plugins.life_engine.test_minimal_heartbeat_checkpoint import _service


@pytest.fixture(autouse=True)
def _isolated_context():
    reset_pending_subject_checkpoint("chat_global", runtime_key=HEARTBEAT_RUNTIME_KEY)
    unregister_live_context(HEARTBEAT_RUNTIME_KEY)
    yield
    reset_pending_subject_checkpoint("chat_global", runtime_key=HEARTBEAT_RUNTIME_KEY)
    unregister_live_context(HEARTBEAT_RUNTIME_KEY)


@pytest.mark.parametrize(
    "literal", ["", CHECKPOINT_OPEN, CHECKPOINT_CLOSE, CHECKPOINT_OPEN + CHECKPOINT_CLOSE],
)
async def test_real_checkpoint_retains_literal_tags_and_revision_after_reload(tmp_path, literal):
    service = _service(tmp_path)
    response = SimpleNamespace(payloads=[
        LLMPayload(ROLE.USER, [Text("synthetic old group")]),
        LLMPayload(ROLE.ASSISTANT, [Text("synthetic old response")]),
        LLMPayload(ROLE.USER, [Text("synthetic current group")]),
    ])
    service._register_heartbeat_live_context(response.payloads)
    manifest = build_group_manifest(response.payloads)
    action = LifeAuthorSelfContinuityCheckpointAction.__new__(LifeAuthorSelfContinuityCheckpointAction)
    action.plugin = service.plugin
    action._context_runtime_key = HEARTBEAT_RUNTIME_KEY
    action._action_origin_extra = lambda: {"consciousness_instance_id": "chat_global"}
    continuity = "synthetic subject explanation cites literal markup: " + literal + "\r\n原文字节"
    ok, detail = await action.execute(
        thought="synthetic decision to retain the exact wording",
        continuity_text=continuity,
        source_manifest_sha256=manifest.source_manifest_sha256,
        expected_revision=manifest.current_checkpoint_revision,
        release_through_group_ref=manifest.groups[0].group_ref,
        retain_exact_group_refs=[],
    )
    assert ok, detail
    command = get_pending_subject_checkpoint("chat_global", runtime_key=HEARTBEAT_RUNTIME_KEY)
    assert command is not None
    prepared = prepare_subject_checkpoint(
        response.payloads, command, archive_namespace=HEARTBEAT_ARCHIVE_NAMESPACE,
    )
    assert prepared.revision == 1
    recognition = checkpoint_data(prepared.checkpoint_payload)
    assert recognition is not None
    assert recognition["continuity_text"].encode("utf-8") == continuity.encode("utf-8")
    assert recognition["revision"] == 1
    assert await service._apply_heartbeat_subject_checkpoint(response)
    restored = await load_heartbeat_rolling(service=service, workspace_path=str(tmp_path))
    checkpoint = current_checkpoint_data(restored)
    assert checkpoint is not None
    assert checkpoint["continuity_text"].encode("utf-8") == continuity.encode("utf-8")
    assert checkpoint["revision"] == 1
    assert checkpoint["exact_archive"]["namespace"] == HEARTBEAT_ARCHIVE_NAMESPACE
    assert build_group_manifest(restored).current_checkpoint_revision == 1
    assert get_pending_subject_checkpoint("chat_global", runtime_key=HEARTBEAT_RUNTIME_KEY) is None
    assert "synthetic old group" not in str(restored)
    archive = (
        tmp_path / "runtime" / "heartbeat_context_archive"
        / f"{manifest.groups[0].group_ref}.json"
    )
    assert archive.is_file()
    assert "synthetic old group" in archive.read_text(encoding="utf-8")


@pytest.mark.parametrize("damage", ["double_frame", "extra_json", "quoted_prefix", "suffix", "nested_frame"])
def test_json_checkpoint_framing_still_rejects_multiple_objects_or_external_text(damage):
    body = json.dumps({"schema": CHECKPOINT_SCHEMA, "continuity_text": "synthetic quote"})
    valid = CHECKPOINT_OPEN + "\n" + body + "\n" + CHECKPOINT_CLOSE
    malformed = {
        "double_frame": valid + "\n" + valid,
        "extra_json": CHECKPOINT_OPEN + "\n" + body + "\n" + body + "\n" + CHECKPOINT_CLOSE,
        "quoted_prefix": "synthetic ordinary quotation: " + valid,
        "suffix": valid + "\nsynthetic ordinary suffix",
        "nested_frame": CHECKPOINT_OPEN + "\n" + valid + "\n" + CHECKPOINT_CLOSE,
    }[damage]
    assert checkpoint_data(LLMPayload(ROLE.ASSISTANT, [Text(malformed)])) is None

