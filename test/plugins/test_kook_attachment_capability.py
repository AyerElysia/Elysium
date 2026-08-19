from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from plugins.kook_adapter.events import KookEventHandler


class _FileClient:
    def __init__(self, body: bytes) -> None:
        self.body = body
        self.urls: list[str] = []

    async def download_media_bytes(self, url: str) -> bytes:
        self.urls.append(url)
        return self.body


@pytest.mark.asyncio
async def test_kook_file_materializes_content_addressed_reference(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    body = "KOOK 收到的附件".encode()
    client = _FileClient(body)
    handler = KookEventHandler(lambda: None, "bot", client)  # type: ignore[arg-type]
    monkeypatch.chdir(tmp_path)

    segments = await handler._build_file_segments(
        "https://example.invalid/resource",
        {"attachments": {"name": "进度.txt", "size": len(body)}},
    )

    assert client.urls == ["https://example.invalid/resource"]
    assert len(segments) == 1
    assert segments[0]["type"] == "file"
    data = segments[0]["data"]
    assert data["materialized"] is True
    assert data["name"] == "进度.txt"
    assert data["sha256"] == hashlib.sha256(body).hexdigest()
    assert "url" not in data
    assert Path(data["path"]).read_bytes() == body


@pytest.mark.asyncio
async def test_kook_file_failure_preserves_only_content_free_metadata(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class _FailingClient:
        async def download_media_bytes(self, url: str) -> bytes:
            raise TimeoutError("secret resource body")

    handler = KookEventHandler(lambda: None, "bot", _FailingClient())  # type: ignore[arg-type]
    monkeypatch.chdir(tmp_path)

    segments = await handler._build_file_segments(
        "https://example.invalid/secret-token",
        {"attachments": {"name": "notes.zip", "size": 42}},
    )

    assert segments == [
        {
            "type": "file",
            "data": {
                "name": "notes.zip",
                "size": 42,
                "materialized": False,
            },
        }
    ]
