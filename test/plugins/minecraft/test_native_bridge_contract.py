"""Authenticated native identity, render-target frames and reconnect cursor tests."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json

import pytest
from PIL import Image
from websockets.asyncio.server import serve

from plugins.minecraft.bridge_client import (
    BRIDGE_PROTOCOL, BridgeConfig, BridgeProtocolError, MinecraftBridgeClient,
)
from plugins.minecraft.embodiment_contracts import WorldObservation, utc_now
from plugins.minecraft.native_profile import offline_uuid


def _hello():
    return {
        "type": "hello", "protocol": BRIDGE_PROTOCOL, "nonce": "test",
        "instance_id": "native-test", "body_type": "neoforge-agent",
        "bridge_version": "0.3.0", "player_name": "Elysia",
        "player_uuid": offline_uuid("Elysia"),
        "game_directory": r"G:\Game\Minecraft\ElysiaClient",
        "task_kinds": ["follow_player"], "capabilities": ["vision.capture"],
    }


def _config(uri):
    return BridgeConfig(
        uri=uri, token="test-secret", expected_player_name="Elysia",
        expected_player_uuid=offline_uuid("Elysia"),
        expected_game_directory=r"G:\Game\Minecraft\ElysiaClient",
    )


@pytest.mark.parametrize("field,value", [
    ("player_name", "AyerElysia"), ("player_uuid", offline_uuid("AyerElysia")),
    ("game_directory", r"G:\Game\Minecraft\.minecraft"),
])
async def test_wrong_account_or_human_directory_is_rejected_before_auth(field, value):
    async def handler(socket):
        hello = _hello()
        hello[field] = value
        await socket.send(json.dumps(hello))
        await socket.wait_closed()

    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        client = MinecraftBridgeClient(_config(f"ws://127.0.0.1:{port}"))
        with pytest.raises(BridgeProtocolError):
            await client.open()
        assert not client.connected


async def test_native_frame_is_correlated_hash_verified_and_retains_identity():
    buffer = io.BytesIO()
    Image.new("RGB", (8, 4), (20, 60, 90)).save(buffer, format="PNG")
    png = buffer.getvalue()

    async def handler(socket):
        await socket.send(json.dumps(_hello()))
        auth = json.loads(await socket.recv())
        assert auth["last_observation_sequence"] is None
        await socket.send(json.dumps({"type": "authentication", "accepted": True}))
        request = json.loads(await socket.recv())
        assert request["type"] == "frame_request"
        await socket.send(json.dumps({
            "type": "frame", "request_id": request["request_id"], "instance_id": "native-test",
            "frame": {
                "source": "minecraft-render-target", "mime_type": "image/png",
                "width": 8, "height": 4, "captured_at": utc_now(),
                "sha256": hashlib.sha256(png).hexdigest(),
                "data_base64": base64.b64encode(png).decode(),
            },
        }))
        assert json.loads(await socket.recv())["type"] == "release_all"

    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        client = MinecraftBridgeClient(_config(f"ws://127.0.0.1:{port}"))
        await client.open()
        assert await client.capture_frame() == png
        assert client.hello_metadata["player_uuid"] == offline_uuid("Elysia")
        await client.close()


@pytest.mark.parametrize("field,value", [
    ("source", "human-desktop"), ("sha256", "bad-digest"), ("width", 100000),
])
async def test_invalid_native_frames_fail_without_returning_pixels(field, value):
    client = MinecraftBridgeClient(_config("ws://127.0.0.1:1"))
    client._instance_id = "native-test"
    future = asyncio.get_running_loop().create_future()
    client._pending_frame = ("request", future)
    png = b"\x89PNG\r\n\x1a\n"
    frame = {
        "source": "minecraft-render-target", "mime_type": "image/png",
        "width": 1, "height": 1, "sha256": hashlib.sha256(png).hexdigest(),
        "data_base64": base64.b64encode(png).decode(),
    }
    frame[field] = value
    with pytest.raises(BridgeProtocolError):
        client._accept_frame({"request_id": "request", "instance_id": "native-test", "frame": frame})
    assert not future.done()
    future.cancel()


async def test_same_native_instance_reconnect_sends_last_delivered_observation_cursor():
    client = MinecraftBridgeClient(_config("ws://127.0.0.1:1"))
    client._instance_id = "native-test"
    client._latest_observation = WorldObservation(
        instance_id="native-test", sequence=17, observed_at=utc_now(), source="test", facts={},
    )

    class Socket:
        def __init__(self):
            self.messages = iter([_hello(), {"type": "authentication", "accepted": True}])
            self.sent = []
        async def recv(self):
            return json.dumps(next(self.messages))
        async def send(self, message):
            self.sent.append(json.loads(message))

    socket = Socket()
    await client._authenticate(socket)
    assert socket.sent[0]["last_observation_sequence"] == 17
