from __future__ import annotations

import base64
import builtins
from typing import Any

import httpx
import pytest

from plugins.life_engine.core.multimodal import (
    MediaItem,
    _convert_gif_to_png as convert_life_gif_to_png,
    _is_gif_image,
    build_multimodal_content,
)
from plugins.napcat_adapter.src.handlers import utils as napcat_utils
from src.core.managers.media_manager import MediaManager
from src.kernel.llm import Image


_GIF_BASE64 = "R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw=="
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


@pytest.mark.parametrize(
    "image_data",
    [
        _GIF_BASE64,
        f"data:image/gif;base64,{_GIF_BASE64}",
    ],
)
def test_life_multimodal_detects_and_converts_raw_and_data_url_gifs(
    image_data: str,
) -> None:
    pytest.importorskip("PIL")

    assert _is_gif_image(image_data, None)
    content = build_multimodal_content(
        "",
        [MediaItem("image", image_data, "message-1")],
    )
    images = [part for part in content if isinstance(part, Image)]

    assert len(images) == 1
    assert base64.b64decode(images[0].value, validate=True).startswith(_PNG_SIGNATURE)


def test_media_manager_detects_gifs_from_data_url_and_raw_header() -> None:
    data_url = f"data:image/gif;base64,{_GIF_BASE64}"

    assert MediaManager._is_gif_image_data(data_url, "image/gif")
    assert MediaManager._is_gif_image_data(_GIF_BASE64, "image/png")


@pytest.mark.parametrize("image_data", [_GIF_BASE64, f"data:image/gif;base64,{_GIF_BASE64}"])
def test_media_manager_converts_raw_and_data_url_gifs(image_data: str) -> None:
    pytest.importorskip("PIL")

    converted, mime_type = MediaManager._convert_gif_to_png(image_data)

    assert mime_type == "image/png"
    assert base64.b64decode(converted, validate=True).startswith(_PNG_SIGNATURE)


def test_gif_conversion_preserves_original_data_without_pillow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_import = builtins.__import__

    def import_without_pillow(
        name: str,
        globals: dict[str, Any] | None = None,
        locals: dict[str, Any] | None = None,
        fromlist: tuple[str, ...] = (),
        level: int = 0,
    ) -> Any:
        if name == "PIL" or name.startswith("PIL."):
            raise ImportError("Pillow unavailable")
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", import_without_pillow)

    assert convert_life_gif_to_png(_GIF_BASE64) == _GIF_BASE64
    converted, mime_type = MediaManager._convert_gif_to_png(_GIF_BASE64)
    assert converted == _GIF_BASE64
    assert mime_type == "image/gif"


class _FakeResponse:
    def __init__(self, content: bytes, error: Exception | None = None) -> None:
        self.content = content
        self._error = error

    def raise_for_status(self) -> None:
        if self._error is not None:
            raise self._error


class _FakeAsyncClient:
    calls: list[dict[str, Any]] = []
    outcomes: list[_FakeResponse | Exception] = []

    def __init__(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)

    async def __aenter__(self) -> "_FakeAsyncClient":
        return self

    async def __aexit__(self, *args: Any) -> bool:
        return False

    async def get(self, url: str) -> _FakeResponse:
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class _InlineTaskManager:
    async def to_process(self, func: Any, *args: Any, **kwargs: Any) -> Any:
        return func(*args, **kwargs)


def _configure_napcat_fetch(
    monkeypatch: pytest.MonkeyPatch,
    outcomes: list[_FakeResponse | Exception],
) -> None:
    _FakeAsyncClient.calls = []
    _FakeAsyncClient.outcomes = outcomes
    monkeypatch.setattr(napcat_utils.httpx, "AsyncClient", _FakeAsyncClient)
    monkeypatch.setattr(napcat_utils, "get_task_manager", lambda: _InlineTaskManager())


@pytest.mark.parametrize(
    "error_type",
    [httpx.ProxyError, httpx.ConnectError, httpx.ConnectTimeout],
)
async def test_napcat_fetch_retries_direct_only_after_proxy_or_connection_failure(
    monkeypatch: pytest.MonkeyPatch,
    error_type: type[httpx.TransportError],
) -> None:
    request = httpx.Request("GET", "https://example.invalid/image")
    _configure_napcat_fetch(
        monkeypatch,
        [
            error_type("connection unavailable", request=request),
            _FakeResponse(b"image-bytes"),
        ],
    )

    encoded = await napcat_utils.get_image_base64(str(request.url))

    assert encoded == base64.b64encode(b"image-bytes").decode("utf-8")
    assert len(_FakeAsyncClient.calls) == 2
    assert "trust_env" not in _FakeAsyncClient.calls[0]
    assert _FakeAsyncClient.calls[1]["trust_env"] is False


async def test_napcat_fetch_does_not_bypass_proxy_after_http_status_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = httpx.Request("GET", "https://example.invalid/image")
    response = httpx.Response(503, request=request)
    _configure_napcat_fetch(
        monkeypatch,
        [
            _FakeResponse(
                b"",
                httpx.HTTPStatusError(
                    "service unavailable",
                    request=request,
                    response=response,
                ),
            )
        ],
    )

    with pytest.raises(httpx.HTTPStatusError):
        await napcat_utils.get_image_base64(str(request.url))

    assert len(_FakeAsyncClient.calls) == 1
