"""elysia_generated_emoji service tests."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import aiohttp
import pytest

from plugins.elysia_generated_emoji.config import ElysiaGeneratedEmojiConfig
from plugins.elysia_generated_emoji.service import ElysiaGeneratedEmojiService


class _FakeResponse:
    def __init__(self, body: bytes = b"\x89PNG\r\n\x1a\n") -> None:
        self.status = 200
        self._body = body

    async def __aenter__(self) -> "_FakeResponse":
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def read(self) -> bytes:
        return self._body


class _FailingRequest:
    async def __aenter__(self) -> "_FailingRequest":
        raise aiohttp.ClientError("proxy tls failed")

    async def __aexit__(self, *_args: object) -> None:
        return None


class _FakeSession:
    last: "_FakeSession | None" = None

    def __init__(self, *, fail_first: bool = False, **_kwargs: object) -> None:
        self.fail_first = fail_first
        self.calls: list[dict[str, Any]] = []
        _FakeSession.last = self

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    def post(self, _url: str, **kwargs: Any) -> _FakeResponse | _FailingRequest:
        self.calls.append(kwargs)
        if self.fail_first and len(self.calls) == 1:
            return _FailingRequest()
        return _FakeResponse()


def _make_service(*, proxy: str = "") -> ElysiaGeneratedEmojiService:
    config = ElysiaGeneratedEmojiConfig()
    config.api.proxy = proxy
    plugin = SimpleNamespace(config=config)
    return ElysiaGeneratedEmojiService(plugin=cast(Any, plugin))


async def test_call_novelai_empty_proxy_ignores_proxy_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7890")
    monkeypatch.setattr(aiohttp, "ClientSession", _FakeSession)
    service = _make_service(proxy="")

    image = await service._call_novelai({"model": "nai-test"}, "pst-test")

    assert image == b"\x89PNG\r\n\x1a\n"
    assert _FakeSession.last is not None
    assert len(_FakeSession.last.calls) == 1
    assert "proxy" not in _FakeSession.last.calls[0]


async def test_call_novelai_retries_direct_after_proxy_client_error(monkeypatch: pytest.MonkeyPatch) -> None:
    class FailingFirstSession(_FakeSession):
        def __init__(self, **kwargs: object) -> None:
            super().__init__(fail_first=True, **kwargs)

    monkeypatch.setattr(aiohttp, "ClientSession", FailingFirstSession)
    service = _make_service(proxy="http://127.0.0.1:7890")

    image = await service._call_novelai({"model": "nai-test"}, "pst-test")

    assert image == b"\x89PNG\r\n\x1a\n"
    assert _FakeSession.last is not None
    assert len(_FakeSession.last.calls) == 2
    assert _FakeSession.last.calls[0]["proxy"] == "http://127.0.0.1:7890"
    assert "proxy" not in _FakeSession.last.calls[1]
