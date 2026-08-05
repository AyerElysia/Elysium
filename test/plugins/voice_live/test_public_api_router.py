"""P3-09 public Voice Live gateway contracts."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from plugins.voice_live.config import VoiceLiveConfig
from plugins.voice_live.protocol import SessionState
from plugins.voice_live.router import VoiceLiveRouter


class FakeParticipantSocket:
    def __init__(self) -> None:
        self.messages = [
            {"text": json.dumps({"type": "start", "resume_episode_id": "other-call"})},
            {"type": "websocket.disconnect"},
        ]
        self.json_frames: list[dict[str, object]] = []
        self.binary_frames: list[bytes] = []

    async def receive(self) -> dict[str, object]:
        return self.messages.pop(0)

    async def send_json(self, data: dict[str, object]) -> None:
        self.json_frames.append(data)

    async def send_bytes(self, data: bytes) -> None:
        self.binary_frames.append(data)

    async def close(self, *, code: int, reason: str = "") -> None:
        self.json_frames.append({"closed": code, "reason": reason})


class FakeCallSession:
    instances: list["FakeCallSession"] = []

    def __init__(self, _config: VoiceLiveConfig, session_id: str) -> None:
        self.session_id = session_id
        self.state = SessionState.CREATED
        self.messages: list[dict[str, object]] = []
        self.stop_reasons: list[str] = []
        FakeCallSession.instances.append(self)

    @property
    def is_active(self) -> bool:
        return self.state is SessionState.ACTIVE

    def set_send_callbacks(self, send_json, send_bytes) -> None:
        self.send_json = send_json
        self.send_bytes = send_bytes

    async def handle_audio(self, _data: bytes) -> None:
        raise AssertionError("audio is not part of this test")

    async def handle_message(self, data: dict[str, object]) -> None:
        self.messages.append(data)

    async def stop(self, *, reason: str) -> None:
        self.stop_reasons.append(reason)
        self.state = SessionState.ENDED

    def snapshot(self) -> dict[str, object]:
        return {"session_id": self.session_id, "state": self.state.value}


@pytest.mark.asyncio
async def test_public_participant_forces_url_call_identity(monkeypatch) -> None:
    config = VoiceLiveConfig()
    config.server.idle_timeout_seconds = 5
    router = VoiceLiveRouter(SimpleNamespace(config=config))
    socket = FakeParticipantSocket()
    FakeCallSession.instances.clear()
    monkeypatch.setattr("plugins.voice_live.router.CallSession", FakeCallSession)

    await router.handle_public_participant(socket, "call-fixed")

    session = FakeCallSession.instances[0]
    assert session.session_id == "call-fixed"
    assert session.messages == [
        {"type": "start", "resume_episode_id": "call-fixed"}
    ]
    assert session.stop_reasons == ["disconnect"]
    assert router.get_session("call-fixed") is None
