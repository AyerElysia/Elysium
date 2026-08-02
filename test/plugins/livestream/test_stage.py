from __future__ import annotations

import asyncio
import time

import pytest

from plugins.livestream.domain import PlaybackReceipt
from plugins.livestream.performance import AudioPacket
from plugins.livestream.stage import StageHub, StageProtocolError

pytestmark = pytest.mark.asyncio


class FakeSocket:
    def __init__(self) -> None:
        self.json_frames: list[dict] = []
        self.binary_frames: list[bytes] = []
        self.closed: list[tuple[int, str | None]] = []

    async def send_json(self, data) -> None:
        self.json_frames.append(data)

    async def send_bytes(self, data: bytes) -> None:
        self.binary_frames.append(data)

    async def close(self, code: int = 1000, reason: str | None = None) -> None:
        self.closed.append((code, reason))


def _receipt(offer: dict, outcome: str = "completed") -> dict:
    payload = offer["payload"]
    return {
        "version": 1,
        "type": "playback.receipt",
        "payload": PlaybackReceipt(
            playback_id=payload["playback_id"],
            utterance_id=payload["utterance_id"],
            chunk_id=payload["chunk_id"],
            outcome=outcome,
            started_at=time.time(),
            ended_at=time.time(),
            played_ms=120,
        ).model_dump(mode="json"),
    }


async def _wait_for_offer(socket: FakeSocket) -> None:
    for _ in range(100):
        if socket.json_frames and socket.binary_frames:
            return
        await asyncio.sleep(0)
    raise AssertionError("stage did not dispatch an offer")


async def test_primary_stage_offer_binary_and_receipt_round_trip() -> None:
    hub = StageHub()
    socket = FakeSocket()
    assert await hub.attach("stage-1", socket, request_primary=True) is True

    play_task = asyncio.create_task(
        hub.play(
            playback_id="playback-1",
            utterance_id="utterance-1",
            chunk_id="chunk-1",
            text="hello",
            audio=AudioPacket(b"audio", "audio/wav"),
            cues={"expression": "smile"},
            timeout_seconds=1,
        )
    )
    await _wait_for_offer(socket)
    assert socket.json_frames[0]["type"] == "audio.offer"
    assert socket.binary_frames == [b"audio"]

    await hub.handle_message("stage-1", _receipt(socket.json_frames[0]))
    receipt = await play_task
    assert receipt.outcome == "completed"

    cached = await hub.play(
        playback_id="playback-1",
        utterance_id="utterance-1",
        chunk_id="chunk-1",
        text="hello",
        audio=AudioPacket(b"audio", "audio/wav"),
        cues={},
        timeout_seconds=1,
    )
    assert cached == receipt
    assert len(socket.binary_frames) == 1


async def test_disconnect_resolves_pending_playback_as_failed() -> None:
    hub = StageHub()
    socket = FakeSocket()
    await hub.attach("stage-1", socket, request_primary=True)
    play_task = asyncio.create_task(
        hub.play(
            playback_id="playback-1",
            utterance_id="utterance-1",
            chunk_id="chunk-1",
            text="hello",
            audio=AudioPacket(b"audio", "audio/wav"),
            cues={},
            timeout_seconds=1,
        )
    )
    await _wait_for_offer(socket)

    await hub.detach("stage-1")
    receipt = await play_task

    assert receipt.outcome == "failed"
    assert hub.primary_client_id is None


async def test_non_primary_or_wrong_version_cannot_acknowledge() -> None:
    hub = StageHub()
    primary = FakeSocket()
    observer = FakeSocket()
    await hub.attach("primary", primary, request_primary=True)
    assert await hub.attach("observer", observer) is False
    payload = {
        "version": 1,
        "type": "playback.receipt",
        "payload": PlaybackReceipt(
            playback_id="unknown",
            utterance_id="u",
            chunk_id="c",
            outcome="completed",
        ).model_dump(mode="json"),
    }

    with pytest.raises(StageProtocolError, match="primary"):
        await hub.handle_message("observer", payload)
    with pytest.raises(StageProtocolError, match="version"):
        await hub.handle_message("primary", {**payload, "version": 999})


async def test_timeout_is_explicit_and_requests_interrupt() -> None:
    hub = StageHub()
    socket = FakeSocket()
    await hub.attach("stage-1", socket, request_primary=True)

    receipt = await hub.play(
        playback_id="playback-1",
        utterance_id="utterance-1",
        chunk_id="chunk-1",
        text="hello",
        audio=AudioPacket(b"audio", "audio/wav"),
        cues={},
        timeout_seconds=0.01,
    )

    assert receipt.outcome == "timed_out"
    assert socket.json_frames[-1]["type"] == "playback.interrupt"


async def test_concurrent_duplicate_playback_is_rejected_explicitly() -> None:
    hub = StageHub()
    socket = FakeSocket()
    await hub.attach("stage-1", socket, request_primary=True)
    kwargs = {
        "playback_id": "playback-1",
        "utterance_id": "utterance-1",
        "chunk_id": "chunk-1",
        "text": "hello",
        "audio": AudioPacket(b"audio", "audio/wav"),
        "cues": {},
        "timeout_seconds": 1,
    }
    first = asyncio.create_task(hub.play(**kwargs))
    await _wait_for_offer(socket)

    with pytest.raises(StageProtocolError, match="already in progress"):
        await hub.play(**kwargs)
    await hub.detach("stage-1")
    assert (await first).outcome == "failed"


async def test_stage_capacity_is_enforced_inside_attach_lock() -> None:
    hub = StageHub(max_clients=1)
    await hub.attach("stage-1", FakeSocket(), request_primary=True)
    with pytest.raises(StageProtocolError, match="capacity"):
        await hub.attach("stage-2", FakeSocket())


async def test_observer_is_not_promoted_when_primary_disconnects() -> None:
    hub = StageHub()
    await hub.attach("stage", FakeSocket(), request_primary=True)
    assert await hub.attach("operator", FakeSocket(), request_primary=False) is False

    await hub.detach("stage")
    assert hub.primary_client_id is None
    assert await hub.attach("stage-reconnected", FakeSocket(), request_primary=True)
