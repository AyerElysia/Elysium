"""Canonical media history contracts using only synthetic, local test data."""

from __future__ import annotations

import base64
import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import Column, MetaData, Table, func, inspect, select
from sqlalchemy.dialects import mysql, sqlite
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import defer

from plugins.life_engine.service.chat_events import build_chat_message_event
from src.app.api.v1.media_objects import ManagedMediaService, MediaObjectStore
from src.app.api.v1.schemas.media import MediaUploadCreateRequest
from src.core.managers.stream_manager import (
    StreamManager,
    _deserialize_attachments_from_db,
    _serialize_attachments_for_db,
)
from src.core.models.media import MediaAttachment, MediaSegmentType
from src.core.models.message import Message, MessageType
from src.core.models.sql_alchemy import Messages
from src.core.utils import schema_sync
from src.kernel.db import CRUDBase
from src.kernel.llm.payload.media import MediaRef

BODY = b"\x89PNG\r\n\x1a\nDO_NOT_PERSIST_MEDIA_BODY"


def _attachment(segment: str = "image", kind: str = "image") -> MediaAttachment:
    bodies = {
        "image": (BODY, "image/png"),
        "audio": (b"RIFF\x24\x00\x00\x00WAVEfmt synthetic", "audio/wav"),
        "video": (b"\x00\x00\x00\x18ftypisomsynthetic", "video/mp4"),
        "file": (b"synthetic opaque file", "application/octet-stream"),
    }
    body, mime_type = bodies[kind]
    return MediaAttachment(
        segment_type=MediaSegmentType(segment),
        media_ref=MediaRef.from_bytes(
            body,
            kind=kind,
            mime_type=mime_type,
            source_message_id="source-message-1",
        ),
        resource_id="media-synthetic-1",
        filename="synthetic.png",
        storage_key="synthetic-key-1",
    )


def _manager(crud) -> StreamManager:
    manager = StreamManager.__new__(StreamManager)
    manager._messages_crud = crud
    manager._streams = {}
    manager._stream_locks = {}
    manager._resolve_person_id_from_message = lambda _message: "person-001"
    manager._update_stream_active_time = AsyncMock()
    return manager


def _message() -> Message:
    return Message(
        message_id="message-1",
        content={"text": "caption"},
        processed_plain_text="caption",
        message_type=MessageType.IMAGE,
        sender_id="user-001",
        sender_name="Alice",
        platform="test",
        chat_type="private",
        stream_id="stream-001",
        attachments=[_attachment()],
    )


async def _restore(manager, row, *, deferred=False):
    return await manager._db_message_to_runtime(
        row,
        defer_content=deferred,
        stream_info={"chat_type": "private"},
        person_cache={"person-001": None},
        bot_info_cache={"test": {"bot_id": "bot", "bot_name": "Test bot"}},
    )


@pytest.mark.parametrize(
    ("segment", "kind"),
    [
        ("image", "image"),
        ("emoji", "image"),
        ("voice", "audio"),
        ("video", "video"),
        ("file", "file"),
    ],
)
def test_descriptor_round_trip_keeps_identity_without_body(segment, kind):
    attachment = _attachment(segment, kind)
    stored = _serialize_attachments_for_db([attachment])
    assert stored is not None
    assert "DO_NOT_PERSIST_MEDIA_BODY" not in stored
    assert base64.b64encode(attachment.media_ref.data).decode() not in stored
    restored = _deserialize_attachments_from_db(stored)
    assert [item.to_descriptor() for item in restored] == [attachment.to_descriptor()]
    assert restored[0].media_ref.data is None
    assert restored[0].resource_id == attachment.resource_id


def test_null_is_legacy_unknown_not_fabricated_attachment():
    assert _serialize_attachments_for_db([]) is None
    assert _deserialize_attachments_from_db(None) == []


@pytest.mark.parametrize(
    "stored",
    [
        "",
        "not-json-private-marker",
        "[]",
        "null",
        "{}",
        '{"version":2,"attachments":[]}',
        '{"version":true,"attachments":[]}',
        '{"version":1.0,"attachments":[]}',
        '{"version":1,"attachments":{},"extra":"private-marker"}',
        '{"version":1,"attachments":{}}',
        '{"version":1,"attachments":[null]}',
        '{"version":1,"attachments":[{"private-marker":"bad"}]}',
    ],
)
def test_non_null_corruption_or_unknown_version_never_becomes_empty(stored):
    with pytest.raises(ValueError) as exc:
        _deserialize_attachments_from_db(stored)
    assert "private-marker" not in str(exc.value)


def test_descriptor_order_and_multiplicity_are_not_deduplicated():
    first = _attachment()
    second = MediaAttachment(
        segment_type=MediaSegmentType.IMAGE,
        media_ref=first.media_ref,
        resource_id="different-media-id",
    )
    restored = _deserialize_attachments_from_db(
        _serialize_attachments_for_db([first, second, first])
    )
    assert [item.resource_id for item in restored] == [
        first.resource_id,
        second.resource_id,
        first.resource_id,
    ]


@pytest.mark.parametrize("dialect", [sqlite.dialect(), mysql.dialect()])
def test_new_column_is_nullable_additive_text_in_supported_dialects(dialect):
    column = Messages.__table__.c.media_attachments
    assert column.nullable and column.server_default is None
    definition = schema_sync._build_column_definition(column, dialect)
    assert definition == "media_attachments TEXT"


@pytest.mark.parametrize("sent", [False, True])
async def test_sqlite_upgrade_reopen_deferred_restore_and_replay(
    tmp_path, monkeypatch, sent
):
    """Use real ORM rows so accidental access to a deferred body fails."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'history.db'}"
    engine = create_async_engine(db_url)
    current_metadata = MetaData()
    Messages.__table__.to_metadata(current_metadata)
    legacy_metadata = MetaData()
    legacy = Table(
        "messages",
        legacy_metadata,
        *(
            Column(
                column.name,
                column.type,
                primary_key=column.primary_key,
                nullable=column.nullable,
            )
            for column in Messages.__table__.columns
            if column.name != "media_attachments"
        ),
    )
    # This is ordinary user text, not an attachment envelope to decode.
    legacy_content = (
        '{"__elysium_message_media_v1":1,"content":"private text","attachments":[]}'
    )
    legacy_values = {
        "message_id": "legacy-1",
        "stream_id": "stream-001",
        "person_id": None,
        "time": 1.0,
        "message_type": "text",
        "content": legacy_content,
        "processed_plain_text": None,
        "platform": "test",
    }
    try:
        async with engine.begin() as connection:
            await connection.run_sync(legacy_metadata.create_all)
            await connection.execute(legacy.insert().values(**legacy_values))
        monkeypatch.setattr(schema_sync, "get_engine", AsyncMock(return_value=engine))
        monkeypatch.setattr(schema_sync, "get_configured_db_type", lambda: "sqlite")
        first = await schema_sync.enforce_database_schema_consistency(current_metadata)
        second = await schema_sync.enforce_database_schema_consistency(current_metadata)
        assert first.columns_added == 1 and second.columns_added == 0
        assert first.columns_removed == second.columns_removed == 0

        sessions = async_sessionmaker(engine, expire_on_commit=False)
        manager = _manager(CRUDBase(Messages, session_factory=sessions))
        method = manager.add_sent_message_to_history if sent else manager.add_message
        message = _message()
        await method(message)
        await method(message)
        async with sessions() as session:
            assert await session.scalar(select(func.count()).select_from(Messages)) == 2
            old = (
                await session.execute(
                    select(Messages).where(Messages.message_id == "legacy-1")
                )
            ).scalar_one()
        old_runtime = await _restore(manager, old)
        assert old_runtime.content == old_runtime.processed_plain_text == legacy_content
        assert old_runtime.attachments == []
    finally:
        await engine.dispose()

    # A fresh engine/session and manager cannot rely on any in-memory attachment.
    reopened = create_async_engine(db_url)
    try:
        sessions = async_sessionmaker(reopened, expire_on_commit=False)
        manager = _manager(CRUDBase(Messages, session_factory=sessions))
        for deferred in (True, False):
            async with sessions() as session:
                query = select(Messages).where(Messages.message_id == "message-1")
                if deferred:
                    query = query.options(defer(Messages.content, raiseload=True))
                row = (await session.execute(query)).scalar_one()
                assert ("content" in inspect(row).unloaded) is deferred
            runtime = await _restore(manager, row, deferred=deferred)
            assert runtime.content == (
                "[Content deferred]" if deferred else "{'text': 'caption'}"
            )
            assert runtime.processed_plain_text == "caption"
            assert (
                runtime.attachments[0].to_descriptor()
                == message.attachments[0].to_descriptor()
            )
            assert runtime.attachments[0].media_ref.data is None
            assert Message.from_dict(runtime.to_dict()).attachments[
                0
            ].to_descriptor() == (message.attachments[0].to_descriptor())
            assert "DO_NOT_PERSIST_MEDIA_BODY" not in row.media_attachments
            assert base64.b64encode(BODY).decode() not in row.media_attachments
    finally:
        await reopened.dispose()


async def test_failed_insert_does_not_publish_volatile_message_and_can_retry():
    crud = SimpleNamespace(
        get_by=AsyncMock(return_value=None),
        create=AsyncMock(
            side_effect=[RuntimeError("synthetic failure"), SimpleNamespace(id=1)]
        ),
    )
    manager = _manager(crud)
    context = SimpleNamespace(add_unread_message=Mock())
    manager._streams["stream-001"] = SimpleNamespace(
        context=context, update_active_time=Mock()
    )
    message = _message()
    with pytest.raises(RuntimeError, match="synthetic failure"):
        await manager.add_message(message)
    context.add_unread_message.assert_not_called()
    manager._update_stream_active_time.assert_not_awaited()
    await manager.add_message(message)
    context.add_unread_message.assert_called_once_with(message)
    stored = crud.create.call_args.args[0]
    assert stored["content"] == "{'text': 'caption'}"
    assert (
        _deserialize_attachments_from_db(stored["media_attachments"])[0].resource_id
        == "media-synthetic-1"
    )


async def test_managed_media_id_survives_attachment_message_and_reopen(tmp_path):
    """The managed original and chat-history descriptor remain separate but linked."""
    media_store = MediaObjectStore(
        tmp_path / "media.sqlite3", tmp_path / "runtime" / "media"
    )
    db_engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'messages.sqlite3'}"
    )
    try:
        async with db_engine.begin() as connection:
            await connection.run_sync(Messages.metadata.create_all)
        data = b"\x89PNG\r\n\x1a\nmanaged-original"
        request = MediaUploadCreateRequest.model_validate(
            {
                "schema_version": 1,
                "kind": "image",
                "mime_type": "image/png",
                "size_bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "file_name": "managed.png",
            }
        )
        upload = media_store.create_upload(request, actor_id="subject-1")
        media_store.put_upload(upload.upload_id, data, actor_id="subject-1")
        descriptor = media_store.complete_upload(upload.upload_id, actor_id="subject-1")
        attachment = await ManagedMediaService(media_store).resolve_ready(
            descriptor.media_id,
            actor_id="subject-1",
            expected_type="image",
        )
        sessions = async_sessionmaker(db_engine, expire_on_commit=False)
        manager = _manager(CRUDBase(Messages, session_factory=sessions))
        message = _message()
        message.attachments = [attachment]
        await manager.add_message(message)
        async with sessions() as session:
            row = (await session.execute(select(Messages))).scalar_one()
        media_store.close()
        reopened_media = MediaObjectStore(
            media_store.database_path, media_store.storage_root
        )
        try:
            manager = _manager(CRUDBase(Messages, session_factory=sessions))
            runtime = await _restore(manager, row)
            assert runtime.attachments[0].resource_id == descriptor.media_id
            assert runtime.attachments[0].media_ref.data is None
            event = build_chat_message_event(runtime, direction="received")
            assert (
                event.metadata["chat"]["attachments"][0]["metadata"]["resource_id"]
                == descriptor.media_id
            )
            resolved = await ManagedMediaService(reopened_media).resolve_ready(
                runtime.attachments[0].resource_id,
                actor_id="subject-1",
                expected_type="image",
            )
            assert resolved.media_ref.data == data
            assert resolved.media_ref.sha256 == runtime.attachments[0].media_ref.sha256
        finally:
            reopened_media.close()
    finally:
        if not media_store._closed:
            media_store.close()
        await db_engine.dispose()
