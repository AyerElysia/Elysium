"""Temporary G02 contracts: event attachment references, not subject behavior.

The shared lab denies network access and never starts a formal Elysium runtime.
Media resolution is exercised separately with its own actor/grant authorization.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from plugins.life_engine.service.chat_events import build_chat_message_event
from plugins.life_engine.service.core import LifeEngineService
from plugins.life_engine.tools.event_grep_tools import LifeEngineReadEventTool
from src.app.api.v1.media_objects import (
    ManagedMediaService,
    MediaObjectFailure,
    MediaObjectStore,
)
from src.app.api.v1.schemas.media import MediaUploadCreateRequest
from src.core.models.media import MediaAttachment, MediaSegmentType
from src.kernel.llm.payload.media import MediaRef

from .test_event_stream_simulation import InteractionLab, _message
from .test_event_stream_simulation import lab as lab  # noqa: PLC0414


def _reader(monkeypatch: pytest.MonkeyPatch, service: Any) -> LifeEngineReadEventTool:
    monkeypatch.setattr(
        LifeEngineService, "get_instance", classmethod(lambda cls: service)
    )
    tool = LifeEngineReadEventTool(plugin=service.plugin)
    tool._runtime_task_name = "core"
    return tool


def _attachment(index: int = 0) -> MediaAttachment:
    return MediaAttachment(
        MediaSegmentType.IMAGE,
        MediaRef.from_bytes(
            b"\x89PNG\r\n\x1a\nsynthetic-only",
            kind="image",
            source_message_id="attachment-source",
        ),
        resource_id=f"synthetic-media-{index}",
        filename=f"{index}-细节.png",
        storage_key="private-storage-key-not-for-model",
    )


def test_read_event_schema_exposes_optional_reference_view() -> None:
    schema = LifeEngineReadEventTool.to_schema()["function"]
    parameters = schema["parameters"]
    assert parameters["properties"]["view"]["enum"] == ["content", "attachments"]
    assert "view" not in parameters.get("required", [])
    assert "不返回媒体原件" in schema["description"]


async def test_attachment_view_preserves_identity_without_internal_locator(
    lab: InteractionLab, monkeypatch: pytest.MonkeyPatch
) -> None:
    attachment = _attachment()
    message = _message(
        "attachment-view", content="  原始说明\n", attachments=[attachment]
    )
    await lab.service.record_message(message)
    await lab.flush()
    row = (await lab.store.read_since(0))[0]
    original = json.dumps(row.metadata, sort_keys=True)
    restarted = lab.new_service()
    await restarted._load_runtime_context()
    reader = _reader(monkeypatch, restarted)

    ok, body = await reader.execute(occurrence_id=row.occurrence_id, max_bytes=4096)
    assert ok, body
    assert body["content"] == message.content
    assert body["attachments_read_view"] == "attachments"

    ok, page = await reader.execute(
        occurrence_id=row.occurrence_id, view="attachments", max_bytes=4096
    )
    assert ok, page
    assert page["continuation"] == ""
    assert page["attachment_state"] == "recorded"
    assert page["attachment_count"] == 1
    assert page["original_bytes_included"] is False
    assert page["reference_resolution"] == "not_attempted"
    descriptor = json.loads(page["content"])[0]
    expected = attachment.to_descriptor()
    expected["metadata"].pop("storage_key")
    assert descriptor == expected
    assert "private-storage-key" not in json.dumps(page)
    assert "data" not in descriptor["media_ref"]
    replayed = await lab.store.get_by_occurrence_id(row.occurrence_id)
    assert json.dumps(replayed.metadata, sort_keys=True) == original


@pytest.mark.parametrize(
    ("kind", "mime", "data"),
    [
        ("image", "image/png", b"\x89PNG\r\n\x1a\nsynthetic"),
        ("audio", "audio/wav", b"RIFF\x00\x00\x00\x00WAVEsynthetic"),
        ("video", "video/mp4", b"\x00\x00\x00\x18ftypisomsynthetic"),
        ("file", "application/octet-stream", b"synthetic\x00\xff\xfe"),
    ],
)
async def test_managed_original_reached_from_recalled_descriptor_after_reopen(
    lab: InteractionLab,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    mime: str,
    data: bytes,
) -> None:
    database = tmp_path / "media-api.sqlite3"
    root = tmp_path / "runtime" / "media"
    store = MediaObjectStore(database, root)
    try:
        request = MediaUploadCreateRequest.model_validate(
            {
                "schema_version": 1,
                "kind": kind,
                "mime_type": mime,
                "size_bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "file_name": "synthetic.bin",
            }
        )
        upload = store.create_upload(request, actor_id="owner")
        store.put_upload(upload.upload_id, data, actor_id="owner")
        media = store.complete_upload(upload.upload_id, actor_id="owner")
        store.save(media.media_id, actor_id="owner", grants=())
        attachment = await ManagedMediaService(store).resolve_ready(
            media.media_id, actor_id="owner", expected_type=kind
        )
        await lab.service.record_message(
            _message(
                "media-chain",
                content="Synthetic media fixture",
                attachments=[attachment],
            )
        )
        await lab.flush()
    finally:
        store.close()

    row = (await lab.store.read_since(0))[0]
    restarted = lab.new_service()
    await restarted._load_runtime_context()
    reader = _reader(monkeypatch, restarted)
    ok, page = await reader.execute(
        occurrence_id=row.occurrence_id, view="attachments", max_bytes=4096
    )
    assert ok, page
    descriptor = json.loads(page["content"])[0]
    resource_id = descriptor["metadata"]["resource_id"]
    assert resource_id == media.media_id
    assert descriptor["media_ref"]["sha256"] == hashlib.sha256(data).hexdigest()

    reopened = MediaObjectStore(database, root)
    try:
        assert (
            reopened.get_content(resource_id, actor_id="owner", grants=()).data == data
        )
        with pytest.raises(MediaObjectFailure, match="media_not_found"):
            reopened.get_content(resource_id, actor_id="unrelated", grants=())
    finally:
        reopened.close()


async def test_attachment_pages_bind_event_view_and_descriptor_frontier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attachments = [replace(_attachment(i), filename="大文件名" * 200) for i in range(8)]
    event = build_chat_message_event(
        _message("paged-attachments", attachments=attachments), direction="received"
    )

    class Store:
        async def get_by_occurrence_id(self, identity):
            return event if identity == event.occurrence_id else other

    other = replace(event, occurrence_id="another-immutable-occurrence")
    reader = _reader(
        monkeypatch, SimpleNamespace(plugin=None, _get_life_event_store=lambda: Store())
    )
    ok, first = await reader.execute(
        occurrence_id=event.occurrence_id, view="attachments", max_bytes=4096
    )
    assert ok, first
    assert first["continuation"]
    cursor = first["continuation"]
    chunks = [first["content"]]
    for _ in range(50):
        ok, page = await reader.execute(
            occurrence_id=event.occurrence_id,
            view="attachments",
            max_bytes=4096,
            continuation=cursor,
        )
        assert ok, page
        assert page["delivered_bytes"] <= 4096
        chunks.append(page["content"])
        cursor = page["continuation"]
        if not cursor:
            break
    else:
        pytest.fail("attachment pagination did not terminate")
    expected = [item.to_descriptor() for item in attachments]
    for descriptor in expected:
        descriptor["metadata"].pop("storage_key")
    assert json.loads("".join(chunks)) == expected

    ok, _ = await reader.execute(
        occurrence_id=other.occurrence_id,
        view="attachments",
        continuation=first["continuation"],
        max_bytes=4096,
    )
    assert ok is False

    ok, _ = await reader.execute(
        occurrence_id=event.occurrence_id,
        continuation=first["continuation"],
        max_bytes=4096,
    )
    assert ok is False  # Attachment continuation cannot read the text-body view.
    event.metadata["chat"]["attachments"][0]["metadata"]["filename"] = "changed"
    ok, _ = await reader.execute(
        occurrence_id=event.occurrence_id,
        view="attachments",
        continuation=first["continuation"],
        max_bytes=4096,
    )
    assert ok is False  # Legacy content-only frontiers must also bind descriptors.


@pytest.mark.parametrize(
    "shape", [None, {}, "broken", [{"media_ref": {"data": "secret"}}]]
)
async def test_invalid_recorded_attachments_fail_without_empty_success(
    monkeypatch: pytest.MonkeyPatch, shape: Any
) -> None:
    event = build_chat_message_event(
        _message("invalid-attachments"), direction="received"
    )
    event.metadata["chat"]["attachments"] = shape

    class Store:
        async def get_by_occurrence_id(self, identity):
            return event

    reader = _reader(
        monkeypatch, SimpleNamespace(plugin=None, _get_life_event_store=lambda: Store())
    )
    ok, failure = await reader.execute(
        occurrence_id=event.occurrence_id, view="attachments"
    )
    assert ok is False
    assert "secret" not in failure


@pytest.mark.parametrize("recorded", [False, True])
async def test_missing_and_explicit_empty_attachment_metadata_are_distinct(
    monkeypatch: pytest.MonkeyPatch, recorded: bool
) -> None:
    event = build_chat_message_event(
        _message("empty-attachments"), direction="received"
    )
    if not recorded:
        event.metadata["chat"].pop("attachments")

    class Store:
        async def get_by_occurrence_id(self, identity):
            return event

    reader = _reader(
        monkeypatch, SimpleNamespace(plugin=None, _get_life_event_store=lambda: Store())
    )
    ok, page = await reader.execute(
        occurrence_id=event.occurrence_id, view="attachments"
    )
    assert ok, page
    assert json.loads(page["content"]) == []
    assert page["attachment_state"] == ("recorded" if recorded else "not_recorded")


@pytest.mark.parametrize("metadata", [{"chat": None}, {"chat": []}, "malformed"])
async def test_invalid_chat_container_does_not_hide_attachment_history(
    monkeypatch: pytest.MonkeyPatch, metadata: Any
) -> None:
    event = build_chat_message_event(_message("invalid-chat"), direction="received")
    event = replace(event, metadata=metadata)

    class Store:
        async def get_by_occurrence_id(self, identity):
            return event

    reader = _reader(
        monkeypatch, SimpleNamespace(plugin=None, _get_life_event_store=lambda: Store())
    )
    ok, failure = await reader.execute(
        occurrence_id=event.occurrence_id, view="attachments"
    )
    assert ok is False
    assert "malformed" not in failure
