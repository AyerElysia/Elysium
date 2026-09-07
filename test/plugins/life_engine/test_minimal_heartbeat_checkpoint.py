"""Storage-only heartbeat checkpoint commit and fail-closed restart contracts.

Synthetic temporary workspaces only. The service constructor, runtime startup,
real model clients and formal databases are never used.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

import plugins.life_engine.service.core as service_module
import plugins.life_engine.service.heartbeat_rolling as rolling_module
from plugins.life_engine.core.context_stewardship import (
    ContextStewardshipError,
    HEARTBEAT_ARCHIVE_NAMESPACE,
    HEARTBEAT_RUNTIME_KEY,
    LifeAuthorSelfContinuityCheckpointAction,
    build_group_manifest,
    current_checkpoint_data,
    get_pending_subject_checkpoint,
    reset_pending_subject_checkpoint,
    unregister_live_context,
)
from plugins.life_engine.service.core import LifeEngineService
from plugins.life_engine.service.heartbeat_rolling import (
    HEARTBEAT_ROLLING_FILENAME,
    deserialize_rolling_payloads,
    load_heartbeat_rolling,
    save_heartbeat_rolling,
    snapshot_dict,
)
from src.kernel.llm import ROLE, LLMPayload, Text
from src.kernel.storage import canonical_json_sha256


@pytest.fixture(autouse=True)
def _isolated_heartbeat_context():
    reset_pending_subject_checkpoint("chat_global", runtime_key=HEARTBEAT_RUNTIME_KEY)
    unregister_live_context(HEARTBEAT_RUNTIME_KEY)
    yield
    reset_pending_subject_checkpoint("chat_global", runtime_key=HEARTBEAT_RUNTIME_KEY)
    unregister_live_context(HEARTBEAT_RUNTIME_KEY)


def _service(tmp_path):
    config = SimpleNamespace(
        settings=SimpleNamespace(
            workspace_path=str(tmp_path),
            heartbeat_timeout_seconds=10,
            heartbeat_interval_seconds=10,
        ),
        model=SimpleNamespace(task_name="synthetic-heartbeat"),
        chatter=SimpleNamespace(self_continuity_checkpoint_max_bytes=32 * 1024),
    )
    service = LifeEngineService.__new__(LifeEngineService)
    service._cfg = lambda: config
    service.runtime_state_store = lambda: None
    service.plugin = SimpleNamespace(config=config, service=service)
    return service


async def _prepared(tmp_path):
    service = _service(tmp_path)
    response = SimpleNamespace(
        payloads=[
            LLMPayload(ROLE.USER, [Text("synthetic heartbeat old event: 雨声")]),
            LLMPayload(ROLE.ASSISTANT, [Text("synthetic old heartbeat response")]),
            LLMPayload(ROLE.USER, [Text("synthetic heartbeat still-pending event")]),
        ]
    )
    await save_heartbeat_rolling(
        response.payloads, service=service, workspace_path=str(tmp_path)
    )
    service._register_heartbeat_live_context(response.payloads)
    manifest = build_group_manifest(response.payloads)
    action = LifeAuthorSelfContinuityCheckpointAction.__new__(
        LifeAuthorSelfContinuityCheckpointAction
    )
    action.plugin = service.plugin
    action._context_runtime_key = HEARTBEAT_RUNTIME_KEY
    action._action_origin_extra = lambda: {"consciousness_instance_id": "chat_global"}
    continuity = "synthetic self-authored heartbeat continuity: 雨声的细节可回取。"
    ok, detail = await action.execute(
        thought="synthetic choice to release a closed heartbeat group",
        continuity_text=continuity,
        source_manifest_sha256=manifest.source_manifest_sha256,
        expected_revision=manifest.current_checkpoint_revision,
        release_through_group_ref=manifest.groups[0].group_ref,
        retain_exact_group_refs=[],
    )
    assert ok, detail
    command = get_pending_subject_checkpoint(
        "chat_global", runtime_key=HEARTBEAT_RUNTIME_KEY
    )
    assert command is not None
    return service, response, command, continuity


async def test_heartbeat_verifies_and_commits_before_install_and_ack(tmp_path, monkeypatch):
    service, response, command, continuity = await _prepared(tmp_path)
    original = response.payloads
    phases: list[str] = []
    verify = service_module.verify_subject_checkpoint_archives
    save = service_module.save_heartbeat_rolling
    ack = service_module.acknowledge_subject_checkpoint

    async def checked_verify(*args, **kwargs):
        assert response.payloads is original
        await verify(*args, **kwargs)
        phases.append("verified")

    async def checked_save(payloads, **kwargs):
        assert response.payloads is original
        assert get_pending_subject_checkpoint(
            "chat_global", runtime_key=HEARTBEAT_RUNTIME_KEY
        ) == command
        phases.append("saving")
        await save(payloads, **kwargs)
        assert response.payloads is original
        phases.append("saved")

    def checked_ack(*args, **kwargs):
        assert phases == ["verified", "saving", "saved"]
        phases.append("ack")
        return ack(*args, **kwargs)

    monkeypatch.setattr(service_module, "verify_subject_checkpoint_archives", checked_verify)
    monkeypatch.setattr(service_module, "save_heartbeat_rolling", checked_save)
    monkeypatch.setattr(service_module, "acknowledge_subject_checkpoint", checked_ack)
    assert await service._apply_heartbeat_subject_checkpoint(response)
    assert phases == ["verified", "saving", "saved", "ack"]
    assert current_checkpoint_data(response.payloads)["continuity_text"] == continuity
    restored = await load_heartbeat_rolling(service=service, workspace_path=str(tmp_path))
    assert snapshot_dict(restored) == snapshot_dict(response.payloads)
    assert current_checkpoint_data(restored)["exact_archive"]["namespace"] == (
        HEARTBEAT_ARCHIVE_NAMESPACE
    )
    assert get_pending_subject_checkpoint(
        "chat_global", runtime_key=HEARTBEAT_RUNTIME_KEY
    ) is None


async def test_heartbeat_write_failure_keeps_old_snapshot_live_chain_and_pending(
    tmp_path, monkeypatch
):
    service, response, command, _ = await _prepared(tmp_path)
    original = response.payloads
    original_snapshot = snapshot_dict(original)

    async def rejected_save(*_args, **_kwargs):
        raise OSError("synthetic heartbeat write rejected")

    monkeypatch.setattr(service_module, "save_heartbeat_rolling", rejected_save)
    with pytest.raises(OSError, match="write rejected"):
        await service._apply_heartbeat_subject_checkpoint(response)
    assert response.payloads is original
    assert get_pending_subject_checkpoint(
        "chat_global", runtime_key=HEARTBEAT_RUNTIME_KEY
    ) == command
    restored = await load_heartbeat_rolling(service=service, workspace_path=str(tmp_path))
    assert snapshot_dict(restored) == original_snapshot


async def test_heartbeat_lost_ack_installs_only_after_exact_readback(tmp_path, monkeypatch):
    service, response, _, continuity = await _prepared(tmp_path)
    save = service_module.save_heartbeat_rolling

    async def committed_without_ack(payloads, **kwargs):
        await save(payloads, **kwargs)
        raise OSError("synthetic heartbeat commit response lost")

    monkeypatch.setattr(service_module, "save_heartbeat_rolling", committed_without_ack)
    assert await service._apply_heartbeat_subject_checkpoint(response)
    assert current_checkpoint_data(response.payloads)["continuity_text"] == continuity
    assert get_pending_subject_checkpoint(
        "chat_global", runtime_key=HEARTBEAT_RUNTIME_KEY
    ) is None


@pytest.mark.parametrize("committed", [False, True])
async def test_heartbeat_cancellation_is_preserved_before_and_after_commit(
    tmp_path, monkeypatch, committed
):
    service, response, command, continuity = await _prepared(tmp_path)
    original = response.payloads
    save = service_module.save_heartbeat_rolling
    cancellation = asyncio.CancelledError("synthetic heartbeat cancelled")

    async def cancelled_save(payloads, **kwargs):
        if committed:
            await save(payloads, **kwargs)
        raise cancellation

    monkeypatch.setattr(service_module, "save_heartbeat_rolling", cancelled_save)
    with pytest.raises(asyncio.CancelledError) as raised:
        await service._apply_heartbeat_subject_checkpoint(response)
    assert raised.value is cancellation
    pending = get_pending_subject_checkpoint(
        "chat_global", runtime_key=HEARTBEAT_RUNTIME_KEY
    )
    if committed:
        assert pending is None
        assert current_checkpoint_data(response.payloads)["continuity_text"] == continuity
    else:
        assert response.payloads is original
        assert pending == command


async def test_heartbeat_readback_failure_must_not_replace_original_cancellation(
    tmp_path, monkeypatch
):
    service, response, command, _ = await _prepared(tmp_path)
    original = response.payloads
    cancellation = asyncio.CancelledError("synthetic original cancellation")

    async def cancelled_save(*_args, **_kwargs):
        raise cancellation

    async def broken_readback(**_kwargs):
        raise OSError("synthetic readback failed")

    monkeypatch.setattr(service_module, "save_heartbeat_rolling", cancelled_save)
    monkeypatch.setattr(service_module, "load_heartbeat_rolling", broken_readback)
    with pytest.raises(asyncio.CancelledError) as raised:
        await service._apply_heartbeat_subject_checkpoint(response)
    assert raised.value is cancellation
    assert response.payloads is original
    assert get_pending_subject_checkpoint(
        "chat_global", runtime_key=HEARTBEAT_RUNTIME_KEY
    ) == command


async def test_heartbeat_unknown_commit_does_not_install_or_clear_pending(tmp_path, monkeypatch):
    service, response, command, _ = await _prepared(tmp_path)
    original = response.payloads

    async def unavailable_save(*_args, **_kwargs):
        raise OSError("synthetic write result unknown")

    async def unavailable_readback(**_kwargs):
        raise OSError("synthetic readback unavailable")

    monkeypatch.setattr(service_module, "save_heartbeat_rolling", unavailable_save)
    monkeypatch.setattr(service_module, "load_heartbeat_rolling", unavailable_readback)
    with pytest.raises(OSError, match="readback unavailable"):
        await service._apply_heartbeat_subject_checkpoint(response)
    assert response.payloads is original
    assert get_pending_subject_checkpoint(
        "chat_global", runtime_key=HEARTBEAT_RUNTIME_KEY
    ) == command


class _NeverSentRequest:
    def __init__(self):
        self.payloads = []
        self.sent = False

    def add_payload(self, payload):
        self.payloads.append(payload)

    async def send(self, **_kwargs):
        self.sent = True
        pytest.fail("a damaged heartbeat snapshot must fail before any model send")


@pytest.mark.parametrize(
    "damage", ["json", "digest", "unknown_payload", "unknown_part", "runtime", "archive"]
)
async def test_heartbeat_snapshot_damage_stops_before_model_send_or_empty_chain(
    tmp_path, monkeypatch, damage
):
    service, response, command, _ = await _prepared(tmp_path)
    assert await service._apply_heartbeat_subject_checkpoint(response)
    path = tmp_path / "runtime" / HEARTBEAT_ROLLING_FILENAME
    if damage == "json":
        path.write_text("{", encoding="utf-8")
    elif damage == "archive":
        archive = (
            tmp_path / "runtime" / "heartbeat_context_archive"
            / f"{command.release_through_group_ref}.json"
        )
        archive.unlink()
    else:
        data = json.loads(path.read_text(encoding="utf-8"))
        if damage == "digest":
            data["payload_digest"] = "0" * 64
        elif damage == "runtime":
            data["runtime_key"] = "life_chatter.rolling_context"
        elif damage == "unknown_payload":
            data["payloads"][0]["role"] = "unsupported-role"
            data["payload_digest"] = canonical_json_sha256(data["payloads"])
        else:
            data["payloads"][0]["content"].append({"type": "unsupported-part"})
            data["payload_digest"] = canonical_json_sha256(data["payloads"])
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    before = path.read_bytes()
    request = _NeverSentRequest()

    async def synthetic_system_prompt():
        return "synthetic subject prompt for a storage-only test"

    service._build_heartbeat_system_prompt = synthetic_system_prompt
    service._get_nucleus_tools = lambda: []
    service._install_heartbeat_context_recovery_hook = lambda _request: None
    monkeypatch.setattr(service_module, "get_model_set_by_task", lambda _task: [])
    monkeypatch.setattr(service_module, "create_llm_request", lambda **_kwargs: request)
    with pytest.raises((RuntimeError, ContextStewardshipError)):
        await service._run_heartbeat_model("synthetic pending wake")
    assert not request.sent
    assert path.read_bytes() == before


def test_heartbeat_legacy_missing_runtime_key_is_explicitly_compatible():
    payloads = [LLMPayload(ROLE.USER, [Text("synthetic legacy input")])]
    raw = snapshot_dict(payloads)
    raw.pop("runtime_key")
    assert snapshot_dict(deserialize_rolling_payloads(raw)) == snapshot_dict(payloads)


async def test_heartbeat_local_save_reuses_synced_atomic_context_writer(tmp_path, monkeypatch):
    calls: list[str] = []
    write = rolling_module.write_synced_context_file

    def observed_write(path, text):
        calls.append(path.name)
        write(path, text)

    monkeypatch.setattr(rolling_module, "write_synced_context_file", observed_write)
    await save_heartbeat_rolling(
        [LLMPayload(ROLE.USER, [Text("synthetic synced heartbeat")])],
        service=None,
        workspace_path=str(tmp_path),
    )
    assert calls == [HEARTBEAT_ROLLING_FILENAME]


class _SharedHeartbeatStore:
    def __init__(self):
        self.record = None
        self.lose_reply = False

    async def get_state(self, _namespace, _state_key):
        return self.record

    async def put_state(self, **kwargs):
        from plugins.life_engine.storage.runtime_contracts import RuntimeStateConflict

        revision = self.record.revision if self.record is not None else 0
        if kwargs["expected_revision"] != revision:
            raise RuntimeStateConflict("synthetic heartbeat stale revision")
        self.record = SimpleNamespace(revision=revision + 1, payload=kwargs["payload"])
        if self.lose_reply:
            raise OSError("synthetic heartbeat lost commit reply")
        return self.record


async def test_heartbeat_selected_old_reader_cannot_overwrite_newer_head(tmp_path):
    from plugins.life_engine.storage.runtime_contracts import RuntimeStateConflict

    store = _SharedHeartbeatStore()
    first = SimpleNamespace(runtime_state_store=lambda: store)
    second = SimpleNamespace(runtime_state_store=lambda: store)
    assert await load_heartbeat_rolling(service=first, workspace_path=str(tmp_path)) == []
    assert await load_heartbeat_rolling(service=second, workspace_path=str(tmp_path)) == []
    winner = [LLMPayload(ROLE.USER, [Text("synthetic first writer head")])]
    stale = [LLMPayload(ROLE.USER, [Text("synthetic stale second writer head")])]
    await save_heartbeat_rolling(winner, service=first, workspace_path=str(tmp_path))
    with pytest.raises(RuntimeStateConflict, match="stale revision"):
        await save_heartbeat_rolling(stale, service=second, workspace_path=str(tmp_path))
    assert store.record.payload == snapshot_dict(winner)
    assert first._heartbeat_rolling_revision == 1
    assert second._heartbeat_rolling_revision == 0


async def test_heartbeat_lost_commit_reply_does_not_blindly_advance_revision(tmp_path):
    from plugins.life_engine.storage.runtime_contracts import RuntimeStateConflict

    store = _SharedHeartbeatStore()
    service = SimpleNamespace(runtime_state_store=lambda: store)
    payloads = [LLMPayload(ROLE.USER, [Text("synthetic committed head")])]
    assert await load_heartbeat_rolling(service=service, workspace_path=str(tmp_path)) == []
    store.lose_reply = True
    with pytest.raises(OSError, match="lost commit reply"):
        await save_heartbeat_rolling(payloads, service=service, workspace_path=str(tmp_path))
    assert service._heartbeat_rolling_revision == 0
    assert store.record.revision == 1
    store.lose_reply = False
    with pytest.raises(RuntimeStateConflict, match="stale revision"):
        await save_heartbeat_rolling([], service=service, workspace_path=str(tmp_path))
    assert store.record.payload == snapshot_dict(payloads)
    restored = await load_heartbeat_rolling(service=service, workspace_path=str(tmp_path))
    assert snapshot_dict(restored) == snapshot_dict(payloads)
    assert service._heartbeat_rolling_revision == 1
