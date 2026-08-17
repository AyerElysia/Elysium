from __future__ import annotations

import base64
import hashlib
from pathlib import Path
from types import SimpleNamespace

from plugins.napcat_adapter.events.message import MessageEventHandler
from src.core.transport.received_files import MAX_RECEIVED_FILE_BYTES


async def test_mp3_file_message_is_promoted_to_voice_segment(tmp_path) -> None:
    audio_path = tmp_path / "pink-light.mp3"
    audio_path.write_bytes(b"fake mp3 bytes")
    handler = MessageEventHandler(client=None, get_config=lambda: None)  # type: ignore[arg-type]

    segment = {
        "type": "file",
        "data": {
            "file": "pink-light.mp3",
            "file_size": audio_path.stat().st_size,
            "file_id": "file-1",
            "file_path": str(audio_path),
        },
    }

    result = await handler._handle_file(segment, {"group_id": "100"})

    assert result is not None
    assert result["type"] == "voice"
    assert result["data"]["filename"] == "pink-light.mp3"
    assert base64.b64decode(result["data"]["base64"]) == b"fake mp3 bytes"


def test_audio_file_name_detection() -> None:
    assert MessageEventHandler._is_audio_file("粉色夜灯.mp3") is True
    assert MessageEventHandler._is_audio_file("voice.wav") is True
    assert MessageEventHandler._is_audio_file("movie.mp4") is False


async def test_generic_file_is_materialized_from_napcat_get_file(
    tmp_path,
    monkeypatch,
) -> None:
    body = "从 QQ 收到的文档".encode()
    client = SimpleNamespace(
        get_file=lambda **_kwargs: None,
    )

    async def get_file(*, file_id: str):
        assert file_id == "file-doc-1"
        return {"base64": base64.b64encode(body).decode()}

    client.get_file = get_file
    handler = MessageEventHandler(client=client, get_config=lambda: None)
    monkeypatch.chdir(tmp_path)

    result = await handler._handle_file(
        {
            "type": "file",
            "data": {
                "file": "珍贵记录.txt",
                "file_size": len(body),
                "file_id": "file-doc-1",
            },
        },
        {"message_type": "private"},
    )

    assert result is not None
    assert result["type"] == "file"
    data = result["data"]
    assert data["materialized"] is True
    assert data["name"] == "珍贵记录.txt"
    assert data["size"] == len(body)
    assert data["sha256"] == hashlib.sha256(body).hexdigest()
    assert data["storage_key"].startswith("qq/")
    assert "base64" not in data
    assert Path(data["path"]).read_bytes() == body


async def test_generic_file_over_limit_keeps_metadata_without_download() -> None:
    calls = 0

    async def get_file(*, file_id: str):
        nonlocal calls
        calls += 1
        return {"base64": ""}

    client = SimpleNamespace(get_file=get_file)
    handler = MessageEventHandler(client=client, get_config=lambda: None)

    result = await handler._handle_file(
        {
            "type": "file",
            "data": {
                "file": "huge.bin",
                "file_size": MAX_RECEIVED_FILE_BYTES + 1,
                "file_id": "file-huge",
            },
        }
    )

    assert calls == 0
    assert result == {
        "type": "file",
        "data": {
            "name": "huge.bin",
            "size": MAX_RECEIVED_FILE_BYTES + 1,
            "id": "file-huge",
            "materialized": False,
        },
    }
