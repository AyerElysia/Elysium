from __future__ import annotations

from typing import Any

import pytest
from aiohttp import web

from plugins.voice_live.config import VoiceLiveConfig
from plugins.voice_live.voice_conversion import (
    HttpVoiceConverter,
    create_voice_converter,
)


async def _start_server(
    handler: Any,
) -> tuple[web.AppRunner, str]:
    app = web.Application()
    app.router.add_get("/health", handler)
    app.router.add_post("/v1/sessions", handler)
    app.router.add_post("/v1/sessions/{session_id}/{operation}", handler)
    app.router.add_delete("/v1/sessions/{session_id}", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    return runner, f"http://127.0.0.1:{port}"


@pytest.mark.asyncio
async def test_http_voice_converter_stream_contract_and_cleanup() -> None:
    observed: list[tuple[str, str, bytes]] = []

    async def handler(request: web.Request) -> web.StreamResponse:
        if request.path == "/health":
            return web.json_response(
                {
                    "status": "ok",
                    "profile_id": "elysia",
                    "input_sample_rate": 24000,
                    "output_sample_rate": 22050,
                }
            )
        assert request.headers.get("Authorization") == "Bearer local-token"
        body = await request.read()
        observed.append((request.method, request.path, body))
        if request.path == "/v1/sessions":
            return web.json_response(
                {
                    "session_id": "svc-1",
                    "profile_id": "elysia",
                    "input_sample_rate": 24000,
                    "output_sample_rate": 22050,
                },
                status=201,
            )
        if request.method == "DELETE":
            return web.json_response({"status": "deleted"})
        operation = request.match_info["operation"]
        if operation == "reset":
            return web.json_response({"status": "reset"})
        payload = body[::-1] if operation == "audio" else b"\x01\x00"
        return web.Response(
            body=payload,
            headers={
                "X-Output-Sample-Rate": "22050",
                "X-Block-Count": "1",
                "X-Inference-Ms": "12.5",
                "X-Pending-Samples": "0",
            },
        )

    runner, url = await _start_server(handler)
    converter = HttpVoiceConverter(
        url,
        "local-token",
        "elysia",
        connect_timeout=2,
        request_timeout=2,
    )
    try:
        info = await converter.connect()
        assert info["session"]["session_id"] == "svc-1"
        audio = await converter.process(b"\x01\x00\x02\x00", 24000)
        assert audio.data == b"\x00\x02\x00\x01"
        assert audio.sample_rate == 22050
        assert audio.metrics == {
            "block_count": 1,
            "inference_ms": 12.5,
            "pending_samples": 0,
        }
        flushed = await converter.flush()
        assert flushed.data == b"\x01\x00"
        await converter.reset()
    finally:
        await converter.close()
        await runner.cleanup()

    assert ("DELETE", "/v1/sessions/svc-1", b"") in observed


def test_voice_converter_factory_requires_explicit_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = VoiceLiveConfig()
    assert create_voice_converter(config) is None
    config.voice_conversion.enabled = True
    config.voice_conversion.token_env = "VOICE_CONVERTER_TEST_TOKEN"
    monkeypatch.delenv("VOICE_CONVERTER_TEST_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="service token is empty"):
        create_voice_converter(config)


@pytest.mark.asyncio
async def test_voice_converter_rejects_profile_mismatch() -> None:
    async def handler(request: web.Request) -> web.Response:
        return web.json_response(
            {
                "status": "ok",
                "profile_id": "another-voice",
                "input_sample_rate": 24000,
                "output_sample_rate": 22050,
            }
        )

    runner, url = await _start_server(handler)
    converter = HttpVoiceConverter(
        url,
        "local-token",
        "elysia",
        connect_timeout=2,
        request_timeout=2,
    )
    try:
        with pytest.raises(RuntimeError, match="profile mismatch"):
            await converter.connect()
    finally:
        await converter.close()
        await runner.cleanup()
