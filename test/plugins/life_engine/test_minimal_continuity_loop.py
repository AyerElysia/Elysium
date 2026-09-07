"""One synthetic engineering chain through ingress, files, compaction and reload.

The handler, service, file tools, subject authority, selected SQL adapters,
checkpoint action and archive reads are real. Only the subject/model decisions
are explicitly scripted substitutes. This does not demonstrate autonomous
learning or subjective memory, and never starts Elysium, a model or Witness.
Every database and file is below pytest's temporary directory.
"""

from __future__ import annotations

import hashlib
import json
import socket
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from plugins.life_engine.core.chatter import LifeChatter
from plugins.life_engine.core.context_stewardship import (
    ARCHIVE_NAMESPACE,
    CHATTER_RUNTIME_KEY,
    LifeAuthorSelfContinuityCheckpointAction,
    LifeReadContextGroupTool,
    LiveContextWindow,
    build_group_manifest,
    current_checkpoint_data,
    get_pending_subject_checkpoint,
    register_live_context,
    reset_pending_subject_checkpoint,
)
from plugins.life_engine.service.event_bus import LifeEventBus
from plugins.life_engine.service.event_handler import LifeEngineMessageCollectorHandler
from plugins.life_engine.storage.event_factory import open_life_event_store
from plugins.life_engine.storage.runtime_factory import open_runtime_state_store
from plugins.life_engine.storage.subject_factory import open_subject_document_store
from plugins.life_engine.tools.file_tools import LifeEngineReadFileTool
from src.core.components.types import EventType as TransportEventType
from src.kernel.event import EventDecision
from src.kernel.llm import ROLE, LLMPayload, Text, ToolCall, ToolResult
from src.kernel.llm.context import LLMContextManager
from src.kernel.storage import canonical_json

from .test_event_stream_simulation import _message
from .test_life_event_storage_contract import _local_store
from .test_minimal_subject_file_continuity import (
    _ACTOR_ID,
    _MEMORY_PATH,
    _OCCURRED_AT,
    _STREAM_ID,
    _bound_write_tool,
    _current_memory_pin,
    _memory_plugin,
)
from .test_raw_event_minimal_recall import _read_complete, _recall_tools
from .test_service import _make_service


@pytest.fixture(autouse=True)
def _isolated_chain(monkeypatch: pytest.MonkeyPatch):
    LifeChatter.reset_global_runtime()
    reset_pending_subject_checkpoint(_ACTOR_ID)
    original_connect = socket.socket.connect

    def forbid_network(sock: socket.socket, address: Any):
        if sock.family in {socket.AF_INET, socket.AF_INET6}:
            raise AssertionError("continuity fixture must not use a network")
        return original_connect(sock, address)

    monkeypatch.setattr(socket.socket, "connect", forbid_network)
    yield
    LifeChatter.reset_global_runtime()
    reset_pending_subject_checkpoint(_ACTOR_ID)


def _chain_service(
    runtime: Any,
    event_store: Any,
    subject_store: Any,
    state_store: Any,
    data_root: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    plugin, service, _ = _memory_plugin(
        subject_store, data_root=data_root, monkeypatch=monkeypatch
    )
    # The subject-file helper deliberately omits lifecycle initialization.
    # Fill missing ordinary service state from the real constructor, while
    # preserving its bound authority, registry and temporary trace store.
    baseline = _make_service(Path(plugin.config.settings.workspace_path))
    for name, value in vars(baseline).items():
        service.__dict__.setdefault(name, value)
    plugin.config.settings.enabled = True
    service._storage_runtime = runtime
    service._life_event_store = event_store
    service._runtime_state_store = state_store
    service._event_bus = LifeEventBus(event_store)
    service._schedule_curiosity_review = lambda *_args, **_kwargs: None
    deferred: list[Any] = []
    service._schedule_message_persist = lambda *args: deferred.append(args)
    assert service._memory_service is None
    assert service._memory_witness_coordinator is None
    assert plugin.config.memory_witness.enabled is False
    return plugin, service, deferred


def _chatter(plugin: Any) -> LifeChatter:
    chatter = LifeChatter.__new__(LifeChatter)
    chatter.plugin = plugin
    chatter.instance_id = _ACTOR_ID
    chatter._get_config = lambda: plugin.config
    chatter._get_life_service = lambda: plugin.service
    return chatter


async def _receive(service: Any, identity: str, content: str):
    message = _message(identity, stream_id=_STREAM_ID, content=content)
    handler = LifeEngineMessageCollectorHandler(
        SimpleNamespace(plugin_name="life_engine", service=service)
    )
    decision, _ = await handler.execute(
        TransportEventType.ON_MESSAGE_RECEIVED.value, {"message": message}
    )
    assert decision is EventDecision.SUCCESS
    return message


async def _scripted_memory_write(
    plugin: Any, response: Any, *, ordinal: int, content: str, input_occurrence: str
):
    """Substitute a model choice, but keep real tool execution and provenance."""
    service = plugin.service
    source = f"synthetic-memory-choice-{ordinal}"
    call_id = f"tool-call:{source}:chosen"
    args = {
        "path": "MEMORY.md",
        "content": content,
        "reason": f"explicit engineering fixture decision {ordinal}",
        "expected_version": await _current_memory_pin(plugin),
    }
    generation = service._event_builder.build_conscious_model_turn_event(
        activity_id=f"{source}:generation",
        transport_request_id=f"synthetic-request-{ordinal}",
        stream_id=_STREAM_ID,
        source_instance_id=_ACTOR_ID,
        turn_occurrence_id=input_occurrence,
        provider_reasoning_content="",
        assistant_message="SCRIPTED SUBSTITUTE, not output from a subject model.",
        tool_call_ids=[call_id],
        surface="scripted-engineering-fixture",
    )
    choice = service._event_builder.build_conscious_tool_call_event(
        "write_file",
        args,
        activity_id=source,
        model_turn_activity_id=f"{source}:generation",
        call_id=call_id,
        stream_id=_STREAM_ID,
        source_instance_id=_ACTOR_ID,
        turn_occurrence_id=input_occurrence,
        surface="scripted-engineering-fixture",
    )
    generation.timestamp = _OCCURRED_AT
    choice.timestamp = _OCCURRED_AT
    await service._publish_raw_events([generation, choice])
    ok, written = await _bound_write_tool(
        plugin, source_id=choice.occurrence_id
    ).execute(**args)
    assert ok, written
    version = await service.read_subject_authority_file("MEMORY.md")
    assert version.content_bytes == content.encode("utf-8")
    assert version.semantic_actor_id == _ACTOR_ID
    assert version.semantic_source_id == choice.occurrence_id
    assert (
        await service._get_life_event_store().get_by_occurrence_id(
            version.semantic_source_id
        )
        is not None
    )
    ok, read_back = await LifeEngineReadFileTool(plugin=plugin).execute(
        "MEMORY.md", limit=0
    )
    assert ok, read_back
    assert read_back["source_authority"] == "subject_document_store"
    assert read_back["subject_version_id"] == version.version_id
    # Follow the real append path: a prior checkpoint's assistant Text must
    # merge with the next scripted call, not become an adjacent assistant frame.
    context_manager = LLMContextManager()
    for payload in (
        [
            LLMPayload(
                ROLE.ASSISTANT,
                [ToolCall(id=call_id, name="write_file", args=args)],
            ),
            LLMPayload(
                ROLE.TOOL_RESULT,
                [ToolResult(value=written, call_id=call_id, name="write_file")],
            ),
            LLMPayload(
                ROLE.ASSISTANT,
                [
                    ToolCall(
                        id=f"read-{call_id}",
                        name="read_file",
                        args={"path": "MEMORY.md", "limit": 0},
                    )
                ],
            ),
            LLMPayload(
                ROLE.TOOL_RESULT,
                [
                    ToolResult(
                        value=read_back, call_id=f"read-{call_id}", name="read_file"
                    )
                ],
            ),
            LLMPayload(
                ROLE.ASSISTANT,
                [Text(f"SCRIPTED completion after file read, fixture turn {ordinal}.")],
            ),
        ]
    ):
        response.payloads = context_manager.add_payload(response.payloads, payload)
    context_manager.validate_for_send(response.payloads)
    return version


async def _checkpoint(chatter: LifeChatter, response: Any, continuity: str):
    manifest = build_group_manifest(response.payloads)
    assert len(manifest.groups) == 1
    released = manifest.groups[0]
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
    action._action_origin_extra = lambda: {"consciousness_instance_id": _ACTOR_ID}
    original = response.payloads
    ok, detail = await action.execute(
        thought="SCRIPTED engineering choice to archive this closed group.",
        continuity_text=continuity,
        source_manifest_sha256=manifest.source_manifest_sha256,
        expected_revision=manifest.current_checkpoint_revision,
        release_through_group_ref=released.group_ref,
        retain_exact_group_refs=[],
    )
    assert ok, detail
    assert response.payloads is original
    assert get_pending_subject_checkpoint(_ACTOR_ID) is not None
    result = await chatter._maybe_compact_runtime_context(response)
    assert result is not None and result.triggered
    assert result.revision == manifest.current_checkpoint_revision + 1
    assert get_pending_subject_checkpoint(_ACTOR_ID) is None
    return released


async def _archive_text(plugin: Any, group_ref: str) -> str:
    tool = LifeReadContextGroupTool.__new__(LifeReadContextGroupTool)
    tool.plugin = plugin
    offset = 0
    chunks: list[str] = []
    for _ in range(100):
        ok, page = await tool.execute(group_ref, offset_bytes=offset, max_bytes=257)
        assert ok, page
        assert page["group_ref"] == group_ref
        assert page["offset_bytes"] == offset
        assert page["delivered_bytes"] == len(page["content"].encode("utf-8"))
        assert page["delivered_bytes"] <= 257
        chunks.append(page["content"])
        if page["complete"]:
            exact = "".join(chunks)
            assert len(exact.encode("utf-8")) == page["original_bytes"]
            assert hashlib.sha256(exact.encode("utf-8")).hexdigest() == (
                group_ref.removeprefix("ctxg_")
            )
            return exact
        assert page["next_offset_bytes"] > offset
        offset = page["next_offset_bytes"]
    raise AssertionError("exact context archive pagination did not terminate")


async def test_same_chain_raw_memory_two_checkpoints_and_fresh_recall(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with _local_store(tmp_path / "selected") as (runtime, events, _, _):
        subjects = await open_subject_document_store(runtime, initialize_schema=True)
        states = await open_runtime_state_store(runtime, initialize_schema=True)
        data_root = tmp_path / "data"
        plugin, service, deferred = _chain_service(
            runtime, events, subjects, states, data_root, monkeypatch
        )
        chatter = _chatter(plugin)
        first_raw = "  MINIMAL-RAW-A: 青色纸鹤，原始细节。\n" + "花瓣🪷" * 300 + "\n "
        second_raw = "MINIMAL-RAW-B: engineering correction input; 纸鹤后来移到书架。"
        third_raw = "MINIMAL-RAW-C: current input must remain in the restored window."
        first_memory = "# Synthetic engineering MEMORY\n纸鹤在抽屉里。\n"
        latest_memory = "# Synthetic engineering MEMORY\n纸鹤后来移到书架。\n"
        await _receive(service, "minimal-input-a", first_raw)
        raw_a = (await events.read_since(0))[0]
        assert raw_a.content == first_raw
        response = SimpleNamespace(payloads=[LLMPayload(ROLE.USER, [Text(first_raw)])])
        version_a = await _scripted_memory_write(
            plugin,
            response,
            ordinal=1,
            content=first_memory,
            input_occurrence=raw_a.occurrence_id,
        )
        await _receive(service, "minimal-input-b", second_raw)
        raw_b = next(
            row for row in await events.read_since(0) if row.content == second_raw
        )
        response.payloads.append(LLMPayload(ROLE.USER, [Text(second_raw)]))
        continuity_a = "SCRIPTED continuity 1: MEMORY has the first fixture version."
        group_a = await _checkpoint(chatter, response, continuity_a)
        assert first_raw not in str(response.payloads)

        version_b = await _scripted_memory_write(
            plugin,
            response,
            ordinal=2,
            content=latest_memory,
            input_occurrence=raw_b.occurrence_id,
        )
        await _receive(service, "minimal-input-c", third_raw)
        response.payloads.append(LLMPayload(ROLE.USER, [Text(third_raw)]))
        continuity_b = (
            "SCRIPTED continuity 2: latest MEMORY is revised; old refs remain."
        )
        group_b = await _checkpoint(chatter, response, continuity_b)
        committed = LifeChatter._snapshot_data_for_payloads(response.payloads)
        before_events = await events.read_since(0)
        before_versions = await subjects.list_history(_MEMORY_PATH)
        assert version_b.parent_version_id == version_a.version_id
        assert len(before_versions) == 2
        assert second_raw not in str(response.payloads)

        # Lose all local pending/rolling objects, then construct fresh adapters,
        # service and chatter over the same exact temporary authoritative DB.
        deferred.clear()
        service._pending_events.clear()
        service._event_history.clear()
        LifeChatter.reset_global_runtime()
        reset_pending_subject_checkpoint(_ACTOR_ID)
        reopened_events = await open_life_event_store(runtime, initialize_schema=False)
        reopened_subjects = await open_subject_document_store(
            runtime, initialize_schema=False
        )
        reopened_states = await open_runtime_state_store(
            runtime, initialize_schema=False
        )
        fresh_plugin, fresh_service, _ = _chain_service(
            runtime,
            reopened_events,
            reopened_subjects,
            reopened_states,
            data_root,
            monkeypatch,
        )
        fresh_chatter = _chatter(fresh_plugin)
        restored = await fresh_chatter._load_rolling_context_snapshot(fresh_service)
        assert LifeChatter._snapshot_data_for_payloads(restored) == committed
        current = current_checkpoint_data(restored)
        assert current is not None
        assert current["revision"] == 2
        assert current["continuity_text"] == continuity_b
        assert current["actor_consciousness_instance_id"] == _ACTOR_ID
        assert current["released_group_refs"] == [group_b.group_ref]
        assert third_raw in str(restored)

        # Follow the latest ref to the exact prior checkpoint, and then follow
        # that archived checkpoint's ref to the original input/tool group.
        exact_b = await _archive_text(fresh_plugin, group_b.group_ref)
        assert exact_b == canonical_json(group_b.record)
        # Archive records and rolling snapshots have distinct serializers;
        # inspect archived Text parts without pretending to replay tool parts.
        archived_texts = [
            LLMPayload(ROLE.ASSISTANT, [Text(part["text"])])
            for item in json.loads(exact_b)["payloads"]
            if item["role"] == ROLE.ASSISTANT.value
            for part in item["content"]
            if part["type"] == "text"
        ]
        prior = current_checkpoint_data(archived_texts)
        assert prior is not None and prior["revision"] == 1
        assert prior["continuity_text"] == continuity_a
        assert prior["released_group_refs"] == [group_a.group_ref]
        exact_a = await _archive_text(fresh_plugin, prior["released_group_refs"][0])
        assert exact_a == canonical_json(group_a.record)
        assert json.loads(exact_a)["payloads"][0]["content"][0]["text"] == first_raw

        grep, reader = _recall_tools(fresh_service)
        ok, result = await grep.execute(
            query="MINIMAL-RAW-A",
            cross_stream=True,
            include_pending=False,
            context_before=0,
            context_after=0,
            max_bytes=8192,
        )
        assert ok, result
        assert result["stats"]["matched_events"] == 1
        assert result["matches"][0]["event"]["occurrence_id"] == raw_a.occurrence_id
        assert await _read_complete(reader, raw_a.occurrence_id) == first_raw
        ok, latest = await LifeEngineReadFileTool(plugin=fresh_plugin).execute(
            "MEMORY.md", limit=0
        )
        assert ok, latest
        assert latest["source_authority"] == "subject_document_store"
        assert latest["subject_version_id"] == version_b.version_id
        assert latest["file_content_sha256"] == version_b.content_hash
        assert "纸鹤后来移到书架" in latest["content"]
        assert await reopened_subjects.get_version(version_a.version_id) == version_a
        assert await reopened_subjects.list_history(_MEMORY_PATH) == before_versions
        assert await reopened_events.read_since(0) == before_events
        assert fresh_service._pending_events == []
        assert fresh_service._event_history == []
        assert not (
            data_root / "life_engine_workspace" / "runtime" / "context_archive"
        ).exists()
