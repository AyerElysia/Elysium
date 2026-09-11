"""Raw-event recall contracts in temporary storage, with Witness absent.

These tests exercise real ingress, persistence and public recall tools. They do
not call a model or claim that scripted fixtures demonstrate subjective memory.
The shared lab never starts Elysium and forbids external network connections.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

from plugins.life_engine.service.chat_events import build_chat_message_event
from plugins.life_engine.service.core import LifeEngineService
from plugins.life_engine.service.event_handler import LifeEngineMessageCollectorHandler
from plugins.life_engine.service.subconscious_ingest import (
    SUBCONSCIOUS_INGEST_CONSUMER_ID,
)
from plugins.life_engine.tools.event_grep_tools import (
    LifeEngineGrepEventsTool,
    LifeEngineReadEventTool,
    grep_life_events,
)
from src.core.components.types import EventType as TransportEventType
from src.core.models.media import MediaAttachment, MediaSegmentType
from src.kernel.event import EventDecision
from src.kernel.llm.payload.media import MediaRef

from .test_event_stream_simulation import InteractionLab, _message
from .test_event_stream_simulation import lab as lab


def _bind_service(monkeypatch: pytest.MonkeyPatch, service: Any) -> None:
    """Bind only this test process's public tool lookup; restore it afterwards."""
    monkeypatch.setattr(
        LifeEngineService,
        "get_instance",
        classmethod(lambda cls: service),
    )


@pytest.mark.parametrize("store", [None, SimpleNamespace()])
async def test_unavailable_ledger_does_not_masquerade_as_complete_empty_history(
    monkeypatch,
    store,
):
    service = SimpleNamespace(
        _life_event_store=store,
        _event_bus=None,
        _event_history=[],
        _pending_events=[],
        _lock=asyncio.Lock(),
    )
    service._get_lock = lambda: service._lock
    _bind_service(monkeypatch, service)
    result = await grep_life_events(query="synthetic missing")
    assert result["matches"] == []
    assert result["stats"]["ledger_status"] == "unavailable"
    assert result["stats"]["retrieval_scope"] == "runtime_only_incomplete"


def _recall_tools(
    service: Any,
) -> tuple[LifeEngineGrepEventsTool, LifeEngineReadEventTool]:
    grep = LifeEngineGrepEventsTool(plugin=service.plugin)
    reader = LifeEngineReadEventTool(plugin=service.plugin)
    grep._runtime_task_name = "core"
    reader._runtime_task_name = "core"
    return grep, reader


async def _read_complete(
    reader: LifeEngineReadEventTool,
    occurrence_id: str,
) -> str:
    continuation = ""
    chunks: list[str] = []
    for _ in range(20):
        ok, payload = await reader.execute(
            occurrence_id=f"life-event-occurrence:{occurrence_id}",
            continuation=continuation,
            max_bytes=4096,
        )
        assert ok is True, payload
        assert isinstance(payload, dict)
        assert payload["occurrence_id"] == occurrence_id
        assert payload["delivered_bytes"] <= 4096
        chunks.append(payload["content"])
        continuation = str(payload["continuation"])
        if not continuation:
            return "".join(chunks)
    raise AssertionError("raw event read continuation did not terminate")


async def test_recent_raw_event_recalled_after_restart_without_witness(
    lab: InteractionLab,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = (
        "  RAW-RECENT-FIXTURE: engineering evidence only.\n" + "细节🪷" * 600 + "\n "
    )
    message = _message("raw-recent", content=content)
    service = lab.service
    assert service.plugin.config.memory_witness.enabled is False
    assert service._memory_service is None
    assert service._memory_witness_coordinator is None
    handler = LifeEngineMessageCollectorHandler(
        SimpleNamespace(plugin_name="life_engine", service=service)
    )
    decision, _ = await handler.execute(
        TransportEventType.ON_MESSAGE_RECEIVED.value,
        {"message": message},
    )
    assert decision is EventDecision.SUCCESS
    rows = await lab.store.read_since(0)
    assert len(rows) == 1
    occurrence_id = rows[0].occurrence_id
    assert rows[0].content == content

    # Simulate losing notification/checkpoint work, not losing authoritative data.
    lab.deferred.clear()
    restarted = lab.new_service()
    await restarted._load_runtime_context()
    restarted._event_history.clear()
    restarted._pending_events.clear()
    assert restarted._memory_service is None
    assert restarted._memory_witness_coordinator is None
    assert restarted._memory_witness_task_id is None
    _bind_service(monkeypatch, restarted)
    before = await lab.store.consumer_cursor(SUBCONSCIOUS_INGEST_CONSUMER_ID)
    grep, reader = _recall_tools(restarted)

    ok, result = await grep.execute(
        query="RAW-RECENT-FIXTURE",
        cross_stream=True,
        include_pending=False,
        context_before=0,
        context_after=0,
        max_bytes=8192,
    )
    assert ok is True, result
    assert isinstance(result, dict)
    assert result["stats"]["matched_events"] == 1
    match = result["matches"][0]["event"]
    assert match["occurrence_id"] == occurrence_id
    assert match["ledger_source"] == "ledger"
    assert await _read_complete(reader, occurrence_id) == content
    assert await lab.store.consumer_cursor(SUBCONSCIOUS_INGEST_CONSUMER_ID) == before
    assert len(await lab.store.read_since(0)) == 1


async def test_media_attachment_descriptor_enters_ledger_and_survives_replay(
    lab: InteractionLab,
) -> None:
    attachment = MediaAttachment(
        MediaSegmentType.IMAGE,
        MediaRef.from_bytes(
            b"\x89PNG\r\n\x1a\nRAW-BYTES-NOT-IN-DESCRIPTOR",
            kind="image",
            mime_type="image/png",
            source_message_id="ledger-media-message",
        ),
        resource_id="media-ledger-1",
    )
    message = _message("ledger-media-message", attachments=[attachment])
    await lab.service.record_message(message)
    await lab.flush()

    rows = await lab.store.read_since(0)
    assert len(rows) == 1
    row = rows[0]
    descriptor = row.metadata["chat"]["attachments"][0]
    assert descriptor["metadata"]["resource_id"] == "media-ledger-1"
    assert "RAW-BYTES-NOT-IN-DESCRIPTOR" not in json.dumps(descriptor)
    assert "base64" not in json.dumps(descriptor).lower()

    replayed = await lab.store.get_by_occurrence_id(row.occurrence_id)
    assert replayed is not None
    assert replayed.metadata["chat"]["attachments"][0] == descriptor


async def test_raw_grep_keeps_distinct_occurrences_with_same_source_event_id(
    lab: InteractionLab,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = replace(
        build_chat_message_event(
            _message("shared-provider-id", content="First stream evidence"),
            direction="received",
        ),
        event_id="shared-source-event-id",
    )
    second = replace(
        build_chat_message_event(
            _message(
                "shared-provider-id",
                stream_id="simulation:stream-b",
                content="Second stream evidence",
            ),
            direction="received",
        ),
        event_id="shared-source-event-id",
    )
    assert first.occurrence_id != second.occurrence_id
    await lab.store.append_many([first, second])
    _bind_service(monkeypatch, lab.service)
    grep, reader = _recall_tools(lab.service)

    ok, result = await grep.execute(
        query="stream evidence",
        cross_stream=True,
        include_pending=False,
        context_before=0,
        context_after=0,
        max_bytes=16384,
    )
    assert ok is True, result
    assert isinstance(result, dict)
    assert result["stats"]["matched_events"] == 2
    assert {match["event"]["occurrence_id"] for match in result["matches"]} == {
        first.occurrence_id,
        second.occurrence_id,
    }
    assert await _read_complete(reader, first.occurrence_id) == first.content
    assert await _read_complete(reader, second.occurrence_id) == second.content


async def test_raw_read_prefers_exact_occurrence_over_source_id_alias(
    lab: InteractionLab,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = build_chat_message_event(
        _message("exact-target", content="Exact intended evidence"),
        direction="received",
    )
    alias_collision = replace(
        build_chat_message_event(
            _message("other-event", content="Different occurrence, never return this"),
            direction="received",
        ),
        event_id=target.occurrence_id,
    )
    await lab.store.append_many([target, alias_collision])

    async def forbidden_alias_lookup(_identity: str) -> Any:
        raise AssertionError("read_event must use get_by_occurrence_id when available")

    monkeypatch.setattr(
        lab.store,
        "get_by_event_id",
        forbidden_alias_lookup,
        raising=False,
    )
    _bind_service(monkeypatch, lab.service)
    _, reader = _recall_tools(lab.service)
    assert await _read_complete(reader, target.occurrence_id) == target.content


async def test_legacy_read_alias_cannot_return_a_different_occurrence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wrong = SimpleNamespace(
        occurrence_id="wrong-occurrence",
        content="PRIVATE-WRONG-EVENT",
    )

    class LegacyLookupOnly:
        async def get_by_event_id(self, _identity: str) -> Any:
            return wrong

    service = SimpleNamespace(
        plugin=SimpleNamespace(),
        _get_life_event_store=lambda: LegacyLookupOnly(),
    )
    _bind_service(monkeypatch, service)
    _, reader = _recall_tools(service)
    ok, failure = await reader.execute(
        occurrence_id="wanted-occurrence", max_bytes=4096
    )
    assert ok is False
    assert failure == "读取 Life Event 失败: RuntimeError"
    assert "PRIVATE-WRONG-EVENT" not in failure
