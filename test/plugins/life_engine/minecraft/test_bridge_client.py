"""Integration tests for the authenticated Minecraft bridge transport."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import socket
from typing import Any

import pytest
from websockets.asyncio.client import connect
from websockets.asyncio.server import ServerConnection, serve

from plugins.life_engine.minecraft.bridge_client import (
    BRIDGE_PROTOCOL,
    BridgeConfig,
    BridgeProtocolError,
    MinecraftBridgeClient,
)
from plugins.life_engine.minecraft.embodiment_contracts import (
    ActionCommand,
    utc_now,
)


def _message(payload: dict[str, Any]) -> str:
    """Encode a compact bridge frame for the test server."""

    return json.dumps(payload, separators=(",", ":"))


def _observation(sequence: int) -> dict[str, Any]:
    """Build one complete test observation message."""

    return {
        "type": "observation",
        "observation": {
            "instance_id": "minecraft-test",
            "sequence": sequence,
            "observed_at": utc_now(),
            "source": "neoforge-test",
            "facts": {"position": {"x": sequence, "y": 64, "z": 0}},
        },
    }


async def test_bridge_authenticates_correlates_and_waits_for_fresh_state() -> None:
    """The client enforces auth, ack, completion, and observation ordering."""

    token = "test-secret"

    async def handler(socket: ServerConnection) -> None:
        """Implement the happy-path side of the bridge contract."""

        nonce = "server-nonce"
        await socket.send(
            _message(
                {
                    "type": "hello",
                    "protocol": BRIDGE_PROTOCOL,
                    "nonce": nonce,
                    "instance_id": "minecraft-test",
                    "body_type": "neoforge-agent",
                    "bridge_version": "0.2.1",
                    "minecraft_version": "1.21.1",
                    "neoforge_version": "21.1.219",
                    "capabilities": ["baritone.goal", "native.input_batch"],
                }
            )
        )
        authentication = json.loads(await socket.recv())
        expected = hmac.new(token.encode(), nonce.encode(), hashlib.sha256).hexdigest()
        assert authentication["digest"] == expected
        await socket.send(_message({"type": "authentication", "accepted": True}))
        await socket.send(_message(_observation(1)))

        command_message = json.loads(await socket.recv())
        command = command_message["command"]
        receipt = {
            "command_id": command["command_id"],
            "intent_id": command["intent_id"],
            "accepted": True,
            "completed": False,
            "interrupted": False,
        }
        await socket.send(_message({"type": "receipt", "receipt": receipt}))
        receipt["completed"] = True
        receipt["facts"] = {"target_position": {"x": 2, "y": 64, "z": 0}}
        await socket.send(_message({"type": "receipt", "receipt": receipt}))
        await socket.send(_message(_observation(2)))
        release = json.loads(await socket.recv())
        assert release["type"] == "release_all"

    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        client = MinecraftBridgeClient(
            BridgeConfig(uri=f"ws://127.0.0.1:{port}", token=token)
        )
        await client.open()
        first = await client.observe()
        command = ActionCommand(
            intent_id="intent-test",
            intent_revision=1,
            operation="baritone.goal",
            parameters={"goal": "walk"},
        )
        receipt = await client.act(command)
        second = await client.observe(after_sequence=first.sequence)
        await client.close()

    assert receipt.completed is True
    assert receipt.facts["target_position"]["x"] == 2
    assert second.sequence == 2
    assert client.capabilities == ("baritone.goal", "native.input_batch")
    assert client.hello_metadata == {
        "body_type": "neoforge-agent",
        "bridge_version": "0.2.1",
        "minecraft_version": "1.21.1",
        "neoforge_version": "21.1.219",
    }


async def test_bridge_rejects_wrong_protocol_before_authentication() -> None:
    """A mismatched protocol cannot enter an execution session."""

    async def handler(socket: ServerConnection) -> None:
        """Send a deliberately incompatible hello."""

        await socket.send(
            _message(
                {
                    "type": "hello",
                    "protocol": "incompatible/9",
                    "nonce": "nonce",
                    "instance_id": "minecraft-test",
                }
            )
        )
        await socket.wait_closed()

    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        client = MinecraftBridgeClient(
            BridgeConfig(uri=f"ws://127.0.0.1:{port}", token="secret")
        )
        with pytest.raises(BridgeProtocolError):
            await client.open()


async def test_close_is_idempotent_after_peer_closes_normally() -> None:
    """Normal game shutdown before cleanup cannot turn close into a failure."""

    token = "peer-close-secret"
    remote_closed = asyncio.Event()

    async def handler(socket_connection: ServerConnection) -> None:
        nonce = "peer-close-nonce"
        await socket_connection.send(
            _message(
                {
                    "type": "hello",
                    "protocol": BRIDGE_PROTOCOL,
                    "nonce": nonce,
                    "instance_id": "minecraft-test",
                    "capabilities": ["movement.input"],
                }
            )
        )
        await socket_connection.recv()
        await socket_connection.send(
            _message({"type": "authentication", "accepted": True})
        )
        await socket_connection.send(_message(_observation(1)))
        await socket_connection.close(code=1000, reason="bridge stopping")
        remote_closed.set()

    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        client = MinecraftBridgeClient(
            BridgeConfig(uri=f"ws://127.0.0.1:{port}", token=token)
        )
        await client.open()
        await client.observe()
        await remote_closed.wait()
        await asyncio.sleep(0)
        await client.close()
        await client.close()


async def test_body_events_are_fifo_deduplicated_and_acknowledged_after_consumption() -> None:
    """A pushed chat event remains pending until the durable consumer acks it."""

    token = "event-secret"
    acked = asyncio.Event()

    async def handler(socket_connection: ServerConnection) -> None:
        nonce = "event-nonce"
        await socket_connection.send(
            _message(
                {
                    "type": "hello",
                    "protocol": BRIDGE_PROTOCOL,
                    "nonce": nonce,
                    "instance_id": "minecraft-event-test",
                    "body_type": "mineflayer-bot",
                    "bridge_version": "0.2.1",
                    "capabilities": ["task.start"],
                    "task_kinds": ["follow_player", "gather_block"],
                }
            )
        )
        authentication = json.loads(await socket_connection.recv())
        expected = hmac.new(token.encode(), nonce.encode(), hashlib.sha256).hexdigest()
        assert authentication["digest"] == expected
        await socket_connection.send(
            _message({"type": "authentication", "accepted": True})
        )
        event = {
            "event_id": "minecraft_event_chat_1",
            "instance_id": "minecraft-event-test",
            "sequence": 41,
            "occurred_at": utc_now(),
            "source": "mineflayer-bot",
            "kind": "minecraft.chat.received",
            "payload": {"username": "AyerElysia", "message": "一起玩吗"},
        }
        await socket_connection.send(_message({"type": "event", "event": event}))
        await socket_connection.send(_message({"type": "event", "event": event}))
        acknowledgement = json.loads(await socket_connection.recv())
        assert acknowledgement == {
            "type": "event_ack",
            "event_id": "minecraft_event_chat_1",
        }
        acked.set()
        release = json.loads(await socket_connection.recv())
        assert release["type"] == "release_all"

    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        client = MinecraftBridgeClient(
            BridgeConfig(uri=f"ws://127.0.0.1:{port}", token=token)
        )
        await client.open()
        first = await client.next_event()
        replayed_peek = await client.next_event()
        assert replayed_peek is first
        await client.acknowledge_event(first.event_id)
        await acked.wait()
        await client.close()

    assert first.kind == "minecraft.chat.received"
    assert first.sequence == 41
    assert first.payload == {"username": "AyerElysia", "message": "一起玩吗"}
    assert client.hello_metadata["task_kinds"] == [
        "follow_player",
        "gather_block",
    ]


async def test_reverse_listener_authenticates_through_transparent_relay() -> None:
    """A Windows-outbound relay preserves authentication and observations."""

    token = "reverse-test-secret"
    release_received = asyncio.Event()

    async def body_handler(socket_connection: ServerConnection) -> None:
        """Serve the body side of a reverse-relayed connection."""

        nonce = "reverse-nonce"
        await socket_connection.send(
            _message(
                {
                    "type": "hello",
                    "protocol": BRIDGE_PROTOCOL,
                    "nonce": nonce,
                    "instance_id": "reverse-minecraft-test",
                    "capabilities": ["native.input_batch"],
                }
            )
        )
        authentication = json.loads(await socket_connection.recv())
        expected = hmac.new(token.encode(), nonce.encode(), hashlib.sha256).hexdigest()
        assert authentication["digest"] == expected
        await socket_connection.send(
            _message({"type": "authentication", "accepted": True})
        )
        observation = _observation(1)
        observation["observation"]["instance_id"] = "reverse-minecraft-test"
        await socket_connection.send(_message(observation))
        release = json.loads(await socket_connection.recv())
        assert release["type"] == "release_all"
        release_received.set()

    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    reverse_port = int(probe.getsockname()[1])
    probe.close()

    async with serve(body_handler, "127.0.0.1", 0) as body_server:
        body_port = body_server.sockets[0].getsockname()[1]
        reverse_uri = f"ws://127.0.0.1:{reverse_port}/elysium"
        client = MinecraftBridgeClient(
            BridgeConfig(
                uri="ws://unused.invalid/elysium",
                token=token,
                listen_uri=reverse_uri,
            )
        )
        open_task = asyncio.create_task(client.open())
        await asyncio.sleep(0.05)

        async def relay() -> None:
            """Forward each complete frame without reading protocol content."""

            async with (
                connect(f"ws://127.0.0.1:{body_port}", proxy=None) as local,
                connect(reverse_uri, proxy=None) as remote,
            ):

                async def pipe(source: Any, destination: Any) -> None:
                    async for message in source:
                        await destination.send(message)

                tasks = {
                    asyncio.create_task(pipe(local, remote)),
                    asyncio.create_task(pipe(remote, local)),
                }
                done, pending = await asyncio.wait(
                    tasks,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                await asyncio.gather(*done)

        relay_task = asyncio.create_task(relay())
        await open_task
        observation = await client.observe()
        await client.close()
        await relay_task

    assert observation.instance_id == "reverse-minecraft-test"
    assert client.capabilities == ("native.input_batch",)
    assert release_received.is_set()


async def test_reverse_listener_recovers_same_body_and_reacks_a_durable_replay() -> None:
    """A transient relay loss keeps the lease contract and exact event delivery."""

    token = "reverse-reconnect-secret"
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    reverse_port = int(probe.getsockname()[1])
    probe.close()
    reverse_uri = f"ws://127.0.0.1:{reverse_port}/elysium"
    client = MinecraftBridgeClient(
        BridgeConfig(
            uri="ws://unused.invalid/elysium",
            token=token,
            listen_uri=reverse_uri,
        )
    )

    async def connect_body(sequence: int) -> Any:
        connection = await connect(reverse_uri, proxy=None)
        nonce = f"reverse-reconnect-{sequence}"
        await connection.send(
            _message(
                {
                    "type": "hello",
                    "protocol": BRIDGE_PROTOCOL,
                    "nonce": nonce,
                    "instance_id": "reverse-reconnect-body",
                    "body_type": "mineflayer-bot",
                    "bridge_version": "0.2.1",
                    "minecraft_version": "1.21.1",
                    "capabilities": ["task.start"],
                    "task_kinds": ["follow_player"],
                }
            )
        )
        authentication = json.loads(await connection.recv())
        expected = hmac.new(token.encode(), nonce.encode(), hashlib.sha256).hexdigest()
        assert authentication["digest"] == expected
        await connection.send(
            _message({"type": "authentication", "accepted": True})
        )
        observation = _observation(sequence)
        observation["observation"]["instance_id"] = "reverse-reconnect-body"
        await connection.send(_message(observation))
        return connection

    event = {
        "event_id": "minecraft_event_reconnect_1",
        "instance_id": "reverse-reconnect-body",
        "sequence": 9,
        "occurred_at": utc_now(),
        "source": "mineflayer-bot",
        "kind": "minecraft.chat.received",
        "payload": {"username": "AyerElysia", "message": "断线后还能听见吗"},
    }
    open_task = asyncio.create_task(client.open())
    await asyncio.sleep(0.05)
    first_connection = await connect_body(1)
    await open_task
    first_observation = await client.observe()
    await first_connection.send(_message({"type": "event", "event": event}))
    first_event = await client.next_event()
    await client.acknowledge_event(first_event.event_id)
    assert json.loads(await first_connection.recv()) == {
        "type": "event_ack",
        "event_id": event["event_id"],
    }
    await first_connection.close()
    for _ in range(100):
        if not client.connected:
            break
        await asyncio.sleep(0.01)
    assert client.connected is False

    reconnect_waiter = asyncio.create_task(client.wait_until_connected())
    second_connection = await connect_body(2)
    await reconnect_waiter
    second_observation = await client.observe(
        after_sequence=first_observation.sequence
    )
    await second_connection.send(_message({"type": "event", "event": event}))
    assert json.loads(await second_connection.recv()) == {
        "type": "event_ack",
        "event_id": event["event_id"],
    }

    await client.close()
    await second_connection.close()

    assert second_observation.sequence == 2
    assert client.instance_id == "reverse-reconnect-body"
