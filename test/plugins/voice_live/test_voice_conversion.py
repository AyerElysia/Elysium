from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web

from plugins.voice_live.config import VoiceLiveConfig
from plugins.voice_live.voice_conversion import (
    HttpVoiceConverter,
    _default_gateway_from_route_table,
    create_voice_converter,
    resolve_service_url,
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


def test_wsl_host_alias_uses_linux_default_gateway(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    route_table = (
        "Iface Destination Gateway Flags RefCnt Use Metric Mask MTU Window IRTT\n"
        "eth0 00000000 01D01AAC 0003 0 0 0 00000000 0 0 0\n"
    )
    assert _default_gateway_from_route_table(route_table) == "172.26.208.1"

    class FakeRoute:
        def read_text(self, *, encoding: str) -> str:
            assert encoding == "ascii"
            return route_table

    monkeypatch.setattr(
        "plugins.voice_live.voice_conversion.Path",
        lambda _: FakeRoute(),
    )
    if os.name == "nt":
        assert resolve_service_url("http://wsl-host:17861") == (
            "http://127.0.0.1:17861"
        )
    else:
        assert resolve_service_url("http://wsl-host:17861") == (
            "http://172.26.208.1:17861"
        )


@pytest.mark.asyncio
async def test_http_voice_converter_stream_contract_and_cleanup() -> None:
    observed: list[tuple[str, str, bytes]] = []
    remote_pending = bytearray()

    async def handler(request: web.Request) -> web.StreamResponse:
        if request.path == "/health":
            return web.json_response(
                {
                    "status": "ok",
                    "protocol_version": 2,
                    "profile_id": "elysia",
                    "profile_revision": "profile-revision-1",
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
                    "profile_revision": "profile-revision-1",
                    "input_sample_rate": 24000,
                    "output_sample_rate": 22050,
                    "input_block_samples": 4,
                },
                status=201,
            )
        if request.method == "DELETE":
            return web.json_response({"status": "deleted"})
        operation = request.match_info["operation"]
        if operation == "reset":
            return web.json_response({"status": "reset"})
        if operation == "audio":
            remote_pending.extend(body)
            if len(remote_pending) >= 8:
                payload = bytes(remote_pending[::-1])
                remote_pending.clear()
                block_count = 1
            else:
                payload = b""
                block_count = 0
        else:
            payload = bytes(remote_pending[::-1])
            remote_pending.clear()
            block_count = 1 if payload else 0
        return web.Response(
            body=payload,
            headers={
                "X-Output-Sample-Rate": "22050",
                "X-Block-Count": str(block_count),
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
        assert audio.data == b""
        assert audio.metrics["pending_samples"] == 2
        audio = await converter.process(b"\x03\x00\x04\x00", 24000)
        assert audio.data == b"\x00\x04\x00\x03\x00\x02\x00\x01"
        assert audio.sample_rate == 22050
        assert audio.metrics == {
            "block_count": 1,
            "inference_ms": 12.5,
            "pending_samples": 0,
        }
        partial = await converter.process(b"\x05\x00", 24000)
        assert partial.data == b""
        flushed = await converter.flush()
        assert flushed.data == b"\x00\x05"
        pending_before_reset = await converter.process(b"\x06\x00", 24000)
        assert pending_before_reset.metrics["pending_samples"] == 1
        await converter.reset()
        pending_after_reset = await converter.process(b"\x07\x00", 24000)
        assert pending_after_reset.metrics["pending_samples"] == 1
        flushed_after_reset = await converter.flush()
        assert flushed_after_reset.data == b"\x00\x07"
    finally:
        await converter.close()
        await runner.cleanup()

    assert ("DELETE", "/v1/sessions/svc-1", b"") in observed


@pytest.mark.asyncio
async def test_voice_converter_uses_dedicated_cold_activation_timeout() -> None:
    async def handler(request: web.Request) -> web.Response:
        if request.path == "/health":
            return web.json_response(
                {
                    "status": "ok",
                    "protocol_version": 3,
                    "profile_id": "elysia",
                    "profile_revision": "profile-revision-1",
                }
            )
        if request.method == "DELETE":
            return web.json_response({"status": "deleted"})
        await asyncio.sleep(0.08)
        return web.json_response(
            {
                "session_id": "svc-cold",
                "profile_id": "elysia",
                "profile_revision": "profile-revision-1",
                "input_sample_rate": 24000,
                "output_sample_rate": 22050,
                "input_block_samples": 5760,
            },
            status=201,
        )

    runner, url = await _start_server(handler)
    converter = HttpVoiceConverter(
        url,
        "local-token",
        "elysia",
        connect_timeout=0.03,
        request_timeout=0.03,
        activation_timeout=0.5,
    )
    try:
        result = await converter.connect()
        assert result["session"]["session_id"] == "svc-cold"
    finally:
        await converter.close()
        await runner.cleanup()


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


def test_voice_converter_factory_reads_owner_only_token_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = tmp_path / "seedvc.token"
    token.write_text("local-token\n", encoding="utf-8")
    token.chmod(0o600)
    monkeypatch.delenv("VOICE_CONVERTER_TEST_TOKEN", raising=False)
    config = VoiceLiveConfig()
    config.voice_conversion.enabled = True
    config.voice_conversion.token_env = "VOICE_CONVERTER_TEST_TOKEN"
    config.voice_conversion.token_file = str(token)

    assert isinstance(create_voice_converter(config), HttpVoiceConverter)


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


@pytest.mark.asyncio
async def test_voice_converter_rejects_untraceable_seedvc_service() -> None:
    async def handler(request: web.Request) -> web.Response:
        return web.json_response(
            {
                "status": "ok",
                "profile_id": "elysia",
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
        with pytest.raises(RuntimeError, match="protocol is obsolete"):
            await converter.connect()
    finally:
        await converter.close()
        await runner.cleanup()


@pytest.mark.asyncio
async def test_voice_converter_rejects_profile_revision_drift() -> None:
    async def handler(request: web.Request) -> web.Response:
        if request.path == "/health":
            return web.json_response(
                {
                    "status": "ok",
                    "protocol_version": 2,
                    "profile_id": "elysia",
                    "profile_revision": "profile-revision-1",
                }
            )
        return web.json_response(
            {
                "session_id": "svc-1",
                "profile_id": "elysia",
                "profile_revision": "profile-revision-2",
                "input_sample_rate": 24000,
                "output_sample_rate": 22050,
                "input_block_samples": 4,
            },
            status=201,
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
        with pytest.raises(RuntimeError, match="profile changed"):
            await converter.connect()
    finally:
        await converter.close()
        await runner.cleanup()
