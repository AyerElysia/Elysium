"""P3-09 voice-call API contracts."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from plugins.voice_live.runtime_store import VoiceEpisodeStore
from src.app.api.v1.auth_store import AuthStore
from src.app.api.v1.policy import USER_FRONTEND_AUDIENCE
from src.app.api.v1.runtime import APIContext, create_api_app
from src.app.api.v1.tokens import SignedValueCodec
from src.app.api.v1.voice_calls import (
    VoiceCallAction,
    VoiceCallCommandService,
    VoiceCallQueryService,
)
from src.kernel.commands import (
    CommandDispatcher,
    CommandRecord,
    CommandStatus,
    CommandStore,
    HandlerRegistry,
)

SECRET = "v" * 48
ORIGIN = "http://localhost:5173"


class FakeSession:
    def __init__(self, call_id: str) -> None:
        self.call_id = call_id
        self.state = SimpleNamespace(value="active")
        self.messages: list[dict[str, object]] = []
        self.stops: list[str] = []

    @property
    def is_active(self) -> bool:
        return True

    def snapshot(self) -> dict[str, object]:
        return {
            "session_id": self.call_id,
            "episode_id": self.call_id,
            "state": "active",
            "provider": "fake",
            "input_audio_bytes": 12,
            "output_audio_bytes": 34,
            "interruptions": 1,
            "failure_reason": "",
        }

    async def handle_message(self, payload: dict[str, object]) -> None:
        self.messages.append(payload)

    async def stop(self, *, reason: str) -> None:
        self.stops.append(reason)


class FakeRouter:
    def __init__(self) -> None:
        self.sessions: dict[str, FakeSession] = {}

    def get_session(self, call_id: str) -> FakeSession | None:
        return self.sessions.get(call_id)


class FakeProvider:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.runtime = FakeRouter()

    def router(self) -> FakeRouter:
        return self.runtime

    def store(self, call_id: str) -> VoiceEpisodeStore:
        return VoiceEpisodeStore(self.root, f"voice_{call_id}", call_id)


def _command(action: VoiceCallAction, call_id: str, payload: dict[str, object]) -> CommandRecord:
    now = datetime.now(UTC)
    return CommandRecord(
        command_id="cmd-voice-1",
        idempotency_key="voice-command-key",
        request_hash="hash",
        command_type=action.value,
        schema_version=1,
        actor_id="owner",
        caller_role="user",
        scope_snapshot=("voice_call:operate",),
        target={"domain": "voice_call", "call_id": call_id},
        payload=payload,
        status=CommandStatus.EXECUTING,
        created_at=now,
        accepted_at=now,
        started_at=now,
        finished_at=None,
        result_event_id=None,
        result=None,
        error_code=None,
        safe_error_detail=None,
        correlation_id=None,
        causation_id=None,
        expected_revision=None,
        attempt_count=1,
        cancellation_requested=False,
        task_id="task-voice-1",
    )


@pytest.mark.asyncio
async def test_create_is_durable_without_starting_provider_and_final_transcripts_only(
    tmp_path: Path,
) -> None:
    provider = FakeProvider(tmp_path / "traces")
    service = VoiceCallQueryService(provider, SignedValueCodec(SECRET))

    created = await service.create("owner", "auto")
    assert created.state == "created"
    assert created.connected is False
    assert provider.runtime.sessions == {}

    store = provider.store(created.call_id)
    await store.append_async("transcript.partial", {"role": "user", "text": "par"})
    await store.append_async(
        "transcript.final",
        {"role": "user", "text": "hello", "provider_event_id": "evt-1"},
    )
    page = await service.transcripts(
        created.call_id,
        actor_id="owner",
        grants=(),
        cursor=None,
        limit=20,
    )
    assert [item.text for item in page.transcripts] == ["hello"]

    with pytest.raises(Exception) as exc_info:
        await service.get(created.call_id, actor_id="stranger", grants=())
    assert getattr(exc_info.value, "code", None) == "resource_forbidden"
    granted = await service.get(
        created.call_id,
        actor_id="reader",
        grants=(f"voice_call:{created.call_id}",),
    )
    assert granted.call_id == created.call_id


@pytest.mark.asyncio
async def test_command_service_controls_only_existing_active_call(tmp_path: Path) -> None:
    provider = FakeProvider(tmp_path / "traces")
    queries = VoiceCallQueryService(provider, SignedValueCodec(SECRET))
    call = await queries.create("owner", "auto")
    active = FakeSession(call.call_id)
    provider.runtime.sessions[call.call_id] = active
    service = VoiceCallCommandService(provider)

    interrupt = await service.handle(
        _command(VoiceCallAction.INTERRUPT, call.call_id, {"played_audio_ms": 25})
    )
    assert interrupt.status is CommandStatus.SUCCEEDED
    assert active.messages == [{"type": "interrupt", "played_audio_ms": 25}]

    text = await service.handle(
        _command(VoiceCallAction.TEXT, call.call_id, {"text": "continue"})
    )
    assert text.status is CommandStatus.SUCCEEDED
    assert active.messages[-1] == {"type": "text", "text": "continue"}

    ended = await service.handle(_command(VoiceCallAction.END, call.call_id, {}))
    assert ended.status is CommandStatus.SUCCEEDED
    assert active.stops == ["authenticated API participant end"]


def test_http_create_ticket_scope_resource_and_idempotency(tmp_path: Path) -> None:
    auth = AuthStore(tmp_path / "api.sqlite3", installation_id="test")
    codec = SignedValueCodec(SECRET)
    command_store = CommandStore(tmp_path / "api.sqlite3")
    provider = FakeProvider(tmp_path / "traces")
    registry = HandlerRegistry()
    VoiceCallCommandService(provider).register(registry)
    context = APIContext(
        store=auth,
        codec=codec,
        installation_id="test",
        allowed_origins=(ORIGIN,),
        command_store=command_store,
        command_dispatcher=CommandDispatcher(command_store, registry=registry),
        voice_calls=provider,
    )
    client = TestClient(create_api_app(context))
    challenge = auth.create_bootstrap_challenge(
        codec=codec,
        audience=USER_FRONTEND_AUDIENCE,
        origin=ORIGIN,
        scopes=(
            "auth:session",
            "voice_call:read",
            "voice_call:operate",
            "voice_call:observe",
        ),
    )
    token = client.post(
        "/auth/sessions",
        headers={"Origin": ORIGIN},
        json={
            "grant_type": "bootstrap_challenge",
            "audience": USER_FRONTEND_AUDIENCE,
            "bootstrap_challenge": challenge,
            "origin": ORIGIN,
        },
    ).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}", "Origin": ORIGIN}

    created = client.post("/voice-calls", headers=headers, json={})
    assert created.status_code == 201
    payload = created.json()
    call_id = payload["call"]["call_id"]
    assert payload["call"]["connected"] is False
    assert payload["connection"]["resource"] == f"/api/v1/voice-calls/{call_id}/ws"
    assert payload["connection"]["subprotocol"] == "elysium.voice-call.participant.v1"

    denied_key = client.post(
        f"/voice-calls/{call_id}:resume",
        headers=headers,
        json={},
    )
    assert denied_key.status_code == 422
    accepted = client.post(
        f"/voice-calls/{call_id}:resume",
        headers={**headers, "Idempotency-Key": "resume-call-once"},
        json={},
    )
    assert accepted.status_code == 202
    replay = client.post(
        f"/voice-calls/{call_id}:resume",
        headers={**headers, "Idempotency-Key": "resume-call-once"},
        json={},
    )
    assert replay.status_code == 200
    assert replay.json()["command"]["command_id"] == accepted.json()["command"]["command_id"]

    observer = client.post(
        f"/voice-calls/{call_id}/tickets",
        headers=headers,
        json={"role": "observer", "origin": ORIGIN},
    )
    assert observer.status_code == 200
    assert observer.json()["subprotocol"] == "elysium.voice-call.observer.v1"

    command_store.close()
    auth.close()
