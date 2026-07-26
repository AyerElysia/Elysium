from __future__ import annotations

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.core.models.message import Message, MessageType


_STREAM_MANAGER_PATH = Path(__file__).resolve().parents[3] / "src/core/managers/stream_manager.py"
_STREAM_MANAGER_SPEC = spec_from_file_location("_stream_manager_under_test", _STREAM_MANAGER_PATH)
assert _STREAM_MANAGER_SPEC is not None and _STREAM_MANAGER_SPEC.loader is not None
_STREAM_MANAGER_MODULE = module_from_spec(_STREAM_MANAGER_SPEC)
_STREAM_MANAGER_SPEC.loader.exec_module(_STREAM_MANAGER_MODULE)

StreamManager = _STREAM_MANAGER_MODULE.StreamManager
_serialize_content_for_db = _STREAM_MANAGER_MODULE._serialize_content_for_db


async def test_add_message_strips_all_media_base64_payloads() -> None:
    """入库时不应保留任何媒体 base64，只保留元信息。"""
    manager = StreamManager.__new__(StreamManager)
    manager._streams_crud = SimpleNamespace(
        get_by=AsyncMock(return_value=None),
        create=AsyncMock(return_value=SimpleNamespace(id=1)),
    )
    manager._messages_crud = SimpleNamespace(
        get_by=AsyncMock(return_value=None),
        create=AsyncMock(return_value=SimpleNamespace(id=1)),
    )
    manager._streams = {}
    manager._stream_locks = {}
    manager._resolve_person_id_from_message = lambda _message: "person-001"
    manager._update_stream_active_time = AsyncMock()

    huge_data = "base64|" + ("a" * 2048)
    small_data = "base64|abc123"
    message = Message(
        message_id="msg-001",
        content={
            "text": "[图片]",
            "media": [
                {"type": "image", "data": huge_data, "filename": "huge.png"},
                {"type": "emoji", "data": small_data, "filename": "small.gif"},
            ],
        },
        processed_plain_text="[图片]",
        message_type=MessageType.IMAGE,
        sender_id="user-001",
        sender_name="Alice",
        platform="qq",
        chat_type="private",
        stream_id="stream-001",
    )

    await StreamManager.add_message(manager, message)

    created = manager._messages_crud.create.call_args.args[0]
    assert created["content"] == _serialize_content_for_db(message.content)
    assert "base64|" not in created["content"]
    assert huge_data not in created["content"]
    assert small_data not in created["content"]
    assert "[removed]" in created["content"]
    assert "huge.png" in created["content"]


async def test_add_message_strips_raw_binary_string_content() -> None:
    """纯字符串媒体内容也不应以 base64 形式落库。"""
    manager = StreamManager.__new__(StreamManager)
    manager._streams_crud = SimpleNamespace(
        get_by=AsyncMock(return_value=None),
        create=AsyncMock(return_value=SimpleNamespace(id=1)),
    )
    manager._messages_crud = SimpleNamespace(
        get_by=AsyncMock(return_value=None),
        create=AsyncMock(return_value=SimpleNamespace(id=1)),
    )
    manager._streams = {}
    manager._stream_locks = {}
    manager._resolve_person_id_from_message = lambda _message: "person-001"
    manager._update_stream_active_time = AsyncMock()

    message = Message(
        message_id="msg-002",
        content="base64|QUJD",
        processed_plain_text="[语音]",
        message_type=MessageType.VOICE,
        sender_id="user-001",
        sender_name="Alice",
        platform="qq",
        chat_type="private",
        stream_id="stream-001",
    )

    await StreamManager.add_message(manager, message)

    created = manager._messages_crud.create.call_args.args[0]
    assert created["content"] == "[removed]"
    assert "base64|" not in created["content"]
