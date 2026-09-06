"""Small storage-only continuity loop: author, commit, restart, exact recall.

All text, workspaces and stores are synthetic. No service, provider, model,
formal database or background task is started by these tests.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import plugins.life_engine.core.chatter as chatter_module
from plugins.life_engine.core.chatter import LifeChatter
from plugins.life_engine.core.context_stewardship import (
    ARCHIVE_NAMESPACE,
    CHATTER_RUNTIME_KEY,
    LifeAuthorSelfContinuityCheckpointAction,
    LifeReadContextGroupTool,
    LiveContextWindow,
    ContextStewardshipError,
    apply_pending_subject_checkpoint,
    build_group_manifest,
    current_checkpoint_data,
    get_pending_subject_checkpoint,
    register_live_context,
)
from src.kernel.llm import ROLE, LLMPayload, Text
from src.kernel.storage import canonical_json_sha256


@pytest.fixture(autouse=True)
def _isolated_runtime():
    LifeChatter.reset_global_runtime()
    yield
    LifeChatter.reset_global_runtime()


def _chatter(tmp_path: Path, service=None) -> LifeChatter:
    config = SimpleNamespace(
        settings=SimpleNamespace(workspace_path=str(tmp_path)),
        chatter=SimpleNamespace(
            context_stewardship_enabled=True,
            self_continuity_checkpoint_max_bytes=32 * 1024,
        ),
    )
    chatter = LifeChatter.__new__(LifeChatter)
    chatter.plugin = SimpleNamespace(config=config, service=service, _service=service)
    chatter._get_config = lambda: config
    chatter._get_life_service = lambda: service
    return chatter


def _response() -> SimpleNamespace:
    return SimpleNamespace(
        payloads=[
            LLMPayload(ROLE.USER, [Text("synthetic detail: 青色纸鹤 " + "忆" * 300)]),
            LLMPayload(ROLE.ASSISTANT, [Text("synthetic earlier response")]),
            LLMPayload(ROLE.USER, [Text("synthetic current input remains unread")]),
        ]
    )

async def _author(chatter: LifeChatter, response: SimpleNamespace):
    manifest = build_group_manifest(response.payloads)
    register_live_context(
        LiveContextWindow(
            runtime_key=CHATTER_RUNTIME_KEY,
            payloads=response.payloads,
            archive_namespace=ARCHIVE_NAMESPACE,
            local_archive_subdir="context_archive",
            workspace_path=chatter.plugin.config.settings.workspace_path,
        )
    )
    action = LifeAuthorSelfContinuityCheckpointAction.__new__(
        LifeAuthorSelfContinuityCheckpointAction
    )
    action.plugin = chatter.plugin
    action._action_origin_extra = lambda: {"consciousness_instance_id": "chat_global"}
    continuity = "synthetic subject-authored continuity: 纸鹤的具体细节在原文里。"
    ok, detail = await action.execute(
        thought="synthetic subject choice to release the first closed group",
        continuity_text=continuity,
        source_manifest_sha256=manifest.source_manifest_sha256,
        expected_revision=manifest.current_checkpoint_revision,
        release_through_group_ref=manifest.groups[0].group_ref,
        retain_exact_group_refs=[],
    )
    assert ok, detail
    command = get_pending_subject_checkpoint("chat_global")
    assert command is not None
    return command, continuity


async def test_subject_checkpoint_survives_restart_and_exact_paginated_recall(tmp_path):
    chatter = _chatter(tmp_path)
    response = _response()
    await chatter._save_rolling_context_snapshot(response)
    original = response.payloads
    command, continuity = await _author(chatter, response)
    assert response.payloads is original
    result = await chatter._maybe_compact_runtime_context(response)
    assert result.triggered
    assert get_pending_subject_checkpoint("chat_global") is None
    LifeChatter.reset_global_runtime()
    restored = await _chatter(tmp_path)._load_rolling_context_snapshot()
    checkpoint = current_checkpoint_data(restored)
    assert checkpoint is not None
    assert checkpoint["continuity_text"] == continuity
    assert checkpoint["actor_consciousness_instance_id"] == "chat_global"
    assert checkpoint["source_manifest_sha256"] == command.source_manifest_sha256
    assert checkpoint["revision"] == command.expected_revision + 1
    assert checkpoint["released_group_refs"] == [command.release_through_group_ref]
    assert "synthetic current input remains unread" in str(restored)
    tool = LifeReadContextGroupTool.__new__(LifeReadContextGroupTool)
    tool.plugin = chatter.plugin
    offset = 0
    pages: list[str] = []
    for _ in range(20):
        ok, page = await tool.execute(
            command.release_through_group_ref, offset_bytes=offset, max_bytes=257
        )
        assert ok, page
        assert page["offset_bytes"] == offset
        assert page["delivered_bytes"] <= 257
        pages.append(page["content"])
        if page["complete"]:
            break
        assert page["next_offset_bytes"] > offset
        offset = page["next_offset_bytes"]
    else:
        pytest.fail("exact archive pagination did not finish")
    exact_record = json.loads("".join(pages))
    assert "青色纸鹤" in str(exact_record)
    assert "忆" * 300 in str(exact_record)
    assert "synthetic earlier response" in str(exact_record)


@pytest.mark.parametrize("after_replace", [False, True])
async def test_local_commit_failure_preserves_live_chain_and_blocks_old_overwrite(
    tmp_path, monkeypatch, after_replace
):
    chatter = _chatter(tmp_path)
    response = _response()
    await chatter._save_rolling_context_snapshot(response)
    original = response.payloads
    command, continuity = await _author(chatter, response)
    write = chatter_module.write_synced_context_file

    def fail_write(path, text):
        if after_replace:
            write(path, text)
        raise OSError("synthetic lost acknowledgement")

    monkeypatch.setattr(chatter_module, "write_synced_context_file", fail_write)
    with pytest.raises(RuntimeError, match="RollingContextSnapshotSaveFailed"):
        await chatter._maybe_compact_runtime_context(response)
    assert response.payloads is original
    assert get_pending_subject_checkpoint("chat_global") == command
    committed_bytes = chatter._rolling_context_snapshot_path().read_bytes()
    with pytest.raises(RuntimeError, match="RollingContextRecoveryRequired"):
        await chatter._save_rolling_context_snapshot(response)
    assert chatter._rolling_context_snapshot_path().read_bytes() == committed_bytes
    LifeChatter.reset_global_runtime()
    restored = await _chatter(tmp_path)._load_rolling_context_snapshot()
    checkpoint = current_checkpoint_data(restored)
    if after_replace:
        assert checkpoint is not None
        assert checkpoint["continuity_text"] == continuity
    else:
        assert checkpoint is None
        assert "青色纸鹤" in str(restored)
    assert get_pending_subject_checkpoint("chat_global") is None


class _RuntimeStore:
    """An in-memory selected backend with controllable commit acknowledgements."""

    def __init__(self):
        self.records = {}
        self.failure = ""
        self.fail_read_once = False
        self.writes = 0

    async def get_state(self, namespace, state_key):
        if self.fail_read_once and namespace == "life_chatter.rolling_context":
            self.fail_read_once = False
            raise OSError("synthetic readback unavailable")
        return copy.deepcopy(self.records.get((namespace, state_key)))

    async def put_state(self, **kwargs):
        key = (kwargs["namespace"], kwargs["state_key"])
        previous = self.records.get(key)
        revision = previous.revision if previous is not None else 0
        assert revision == kwargs["expected_revision"]
        self.writes += 1
        is_snapshot = key[0] == "life_chatter.rolling_context"
        if is_snapshot and self.failure == "before":
            raise OSError("synthetic rejected write")
        record = SimpleNamespace(revision=revision + 1, payload=copy.deepcopy(kwargs["payload"]))
        self.records[key] = record
        if is_snapshot and self.failure in {"after", "unknown"}:
            self.fail_read_once = self.failure == "unknown"
            raise OSError("synthetic committed without acknowledgement")
        return copy.deepcopy(record)


async def _selected_setup(tmp_path):
    store = _RuntimeStore()
    service = SimpleNamespace(runtime_state_store=lambda: store)
    chatter = _chatter(tmp_path, service)
    response = _response()
    assert await chatter._load_rolling_context_snapshot(service) == []
    await chatter._save_rolling_context_snapshot(response)
    command, continuity = await _author(chatter, response)
    return store, service, chatter, response, command, continuity


async def test_selected_lost_ack_is_verified_before_install(tmp_path):
    store, service, chatter, response, _, continuity = await _selected_setup(tmp_path)
    store.failure = "after"
    result = await chatter._maybe_compact_runtime_context(response)
    assert result.triggered
    assert not LifeChatter._GLOBAL_ROLLING_CONTEXT_RECOVERY_REQUIRED
    assert get_pending_subject_checkpoint("chat_global") is None
    LifeChatter.reset_global_runtime()
    restored = await _chatter(tmp_path, service)._load_rolling_context_snapshot(service)
    assert current_checkpoint_data(restored)["continuity_text"] == continuity
    assert not (tmp_path / "runtime").exists()


async def test_selected_rejected_write_keeps_pending_for_retry(tmp_path):
    store, _, chatter, response, command, _ = await _selected_setup(tmp_path)
    original = response.payloads
    store.failure = "before"
    with pytest.raises(RuntimeError, match="RollingContextSnapshotSaveFailed"):
        await chatter._maybe_compact_runtime_context(response)
    assert response.payloads is original
    assert get_pending_subject_checkpoint("chat_global") == command
    assert not LifeChatter._GLOBAL_ROLLING_CONTEXT_RECOVERY_REQUIRED
    store.failure = ""
    assert (await chatter._maybe_compact_runtime_context(response)).triggered


async def test_selected_unknown_commit_requires_reload_before_any_old_write(tmp_path):
    store, service, chatter, response, command, continuity = await _selected_setup(tmp_path)
    original = response.payloads
    store.failure = "unknown"
    with pytest.raises(OSError, match="readback unavailable"):
        await chatter._maybe_compact_runtime_context(response)
    assert response.payloads is original
    assert get_pending_subject_checkpoint("chat_global") == command
    writes = store.writes
    with pytest.raises(RuntimeError, match="RollingContextRecoveryRequired"):
        await chatter._save_rolling_context_snapshot(response)
    assert store.writes == writes
    LifeChatter.reset_global_runtime()
    restored = await _chatter(tmp_path, service)._load_rolling_context_snapshot(service)
    assert current_checkpoint_data(restored)["continuity_text"] == continuity


async def test_selected_stale_revision_is_not_adopted_to_overwrite_another_head(tmp_path):
    store, _, chatter, response, command, _ = await _selected_setup(tmp_path)
    key = ("life_chatter.rolling_context", "chat_global")
    another = LifeChatter._snapshot_data_for_payloads(
        [LLMPayload(ROLE.USER, [Text("synthetic another owner head")])]
    )
    store.records[key] = SimpleNamespace(revision=2, payload=another)
    writes = store.writes
    with pytest.raises(RuntimeError, match="RollingContextSnapshotRevisionConflict"):
        await chatter._maybe_compact_runtime_context(response)
    assert store.writes == writes
    assert store.records[key].payload == another
    assert get_pending_subject_checkpoint("chat_global") == command


async def test_selected_lease_rejection_cannot_report_checkpoint_installed(tmp_path):
    store, service, chatter, response, command, _ = await _selected_setup(tmp_path)

    async def reject_claim(**_kwargs):
        raise RuntimeError("synthetic writer lease conflict")

    service.storage_runtime = SimpleNamespace(acquire_singleton_writer=reject_claim)
    original = response.payloads
    writes = store.writes
    with pytest.raises(RuntimeError, match="writer lease conflict"):
        await chatter._maybe_compact_runtime_context(response)
    assert response.payloads is original
    assert store.writes == writes
    assert get_pending_subject_checkpoint("chat_global") == command


@pytest.mark.parametrize("damage", ["json", "digest", "part", "archive", "actor"])
async def test_damaged_snapshot_or_archive_never_becomes_an_empty_chain(tmp_path, damage):
    chatter = _chatter(tmp_path)
    response = _response()
    await chatter._save_rolling_context_snapshot(response)
    command, _ = await _author(chatter, response)
    await chatter._maybe_compact_runtime_context(response)
    path = chatter._rolling_context_snapshot_path()
    if damage == "json":
        path.write_text("{", encoding="utf-8")
    elif damage == "archive":
        archive = tmp_path / "runtime" / "context_archive" / f"{command.release_through_group_ref}.json"
        archive.unlink()
    else:
        data = json.loads(path.read_text(encoding="utf-8"))
        if damage == "digest":
            data["payload_digest"] = "0" * 64
        elif damage == "part":
            data["payloads"][0]["content"].append({"type": "unsupported"})
            data["payload_digest"] = canonical_json_sha256(data["payloads"])
        else:
            for payload in data["payloads"]:
                for part in payload["content"]:
                    if part.get("type") == "text":
                        part["text"] = part["text"].replace(
                            '"actor_consciousness_instance_id": "chat_global"',
                            '"actor_consciousness_instance_id": "another-instance"',
                        )
            data["payload_digest"] = canonical_json_sha256(data["payloads"])
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    before_read = path.read_bytes()
    LifeChatter.reset_global_runtime()
    reader = _chatter(tmp_path)
    with pytest.raises(RuntimeError, match="RollingContextRecoveryRequired"):
        await reader._load_rolling_context_snapshot()
    assert path.read_bytes() == before_read
    with pytest.raises(RuntimeError, match="RollingContextRecoveryRequired"):
        await reader._save_rolling_context_snapshot(_response())


async def test_genuinely_absent_snapshot_remains_a_new_empty_window(tmp_path):
    assert await _chatter(tmp_path)._load_rolling_context_snapshot() == []


async def test_legacy_apply_prepare_failure_does_not_spend_pending_intent(tmp_path):
    chatter = _chatter(tmp_path)
    response = _response()
    command, _ = await _author(chatter, response)
    different_payloads = _response().payloads
    different_payloads[0] = LLMPayload(ROLE.USER, [Text("synthetic changed manifest")])
    with pytest.raises(ContextStewardshipError, match="manifest"):
        apply_pending_subject_checkpoint("chat_global", different_payloads)
    assert get_pending_subject_checkpoint("chat_global") == command
