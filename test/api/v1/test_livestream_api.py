"""P3-08 livestream API contracts."""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from plugins.livestream.ledger import LivestreamLedger
from src.app.api.v1.auth_store import AuthStore
from src.app.api.v1.livestream import (
    LivestreamAction,
    LivestreamCommandService,
    LivestreamQueryService,
    StaticLivestreamProvider,
)
from src.app.api.v1.policy import ADMIN_FRONTEND_AUDIENCE, USER_FRONTEND_AUDIENCE
from src.app.api.v1.runtime import APIContext, create_api_app
from src.app.api.v1.tokens import SignedValueCodec
from src.kernel.commands import CommandRecord, CommandStatus, HandlerRegistry

SECRET = "s" * 48


class FakeRuntime:
    def __init__(self) -> None:
        self.state = "running"
        self.session_id = "live-1"
        self.stage = SimpleNamespace()
        self.calls: list[tuple[str, object]] = []

    async def health(self):
        return SimpleNamespace(
            status="running",
            session_id=self.session_id,
            platform_connected=False,
            stage_clients=1,
            primary_stage_connected=True,
            event_backlog=0,
            performance_backlog=0,
            current_utterance_id=None,
            last_platform_event_at=None,
            last_decision_at=None,
            last_playback_completed_at=None,
            degraded_reasons=[],
        )

    async def start(self) -> str:
        self.calls.append(("start", None))
        return self.session_id

    async def stop(self, *, reason: str) -> None:
        self.calls.append(("stop", reason))

    async def interrupt(self, *, reason: str) -> bool:
        self.calls.append(("interrupt", reason))
        return True

    async def manual_say(self, text: str) -> str:
        self.calls.append(("say", text))
        return "utterance-1"

    async def send_danmaku(self, text: str):
        self.calls.append(("danmaku", text))
        return {"receipt_id": "receipt-1", "confirmed": True}


async def _seed_ledger(path: Path) -> None:
    ledger = LivestreamLedger(path)
    await ledger.start()
    await ledger.append(
        record_id="session-started:live-1",
        session_id="live-1",
        kind="session.started",
        source="livestream.runtime",
        payload={"platform": "bilibili", "room_id": "42", "start_mode": "manual"},
        occurred_at=time.time() - 10,
    )
    await ledger.append(
        record_id="platform:bilibili:42:danmaku:event-1",
        session_id="live-1",
        kind="platform.event",
        source="bilibili",
        payload={
            "kind": "danmaku",
            "user_name": "viewer",
            "content": "hello",
            "raw_payload": {"secret": "not exported"},
        },
    )
    await ledger.stop()


@pytest.mark.asyncio
async def test_query_status_history_and_event_projection(tmp_path: Path) -> None:
    path = tmp_path / "livestream.sqlite3"
    await _seed_ledger(path)
    runtime = FakeRuntime()
    service = LivestreamQueryService(
        StaticLivestreamProvider(runtime, path),
        SignedValueCodec(SECRET),
    )

    status = await service.status()
    assert status.status == "degraded"
    assert status.platform_connected is False

    sessions = await service.sessions(cursor=None, limit=20)
    assert sessions.sessions[0].session_id == "live-1"
    assert sessions.sessions[0].state == "running"

    events = await service.events("live-1", cursor=None, limit=20)
    assert [event.event_type for event in events.events] == [
        "livestream.session_started",
        "livestream.platform.danmaku_received",
    ]
    assert "raw_payload" not in events.events[-1].payload


@pytest.mark.asyncio
async def test_livestream_commands_register_and_dispatch() -> None:
    runtime = FakeRuntime()
    service = LivestreamCommandService(StaticLivestreamProvider(runtime, None))
    registry = HandlerRegistry()
    service.register(registry)
    assert set(registry.command_types) == {action.value for action in LivestreamAction}

    now = __import__("datetime").datetime.now(__import__("datetime").UTC)
    command = CommandRecord(
        command_id="cmd-1",
        idempotency_key="idempotency-1",
        request_hash="hash",
        command_type=LivestreamAction.DANMAKU_SEND.value,
        schema_version=1,
        actor_id="admin",
        caller_role="administrator",
        scope_snapshot=("livestream:operate",),
        target={"domain": "livestream"},
        payload={"text": "hello"},
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
        task_id="task-1",
    )
    outcome = await service.handle(command)
    assert outcome.status is CommandStatus.SUCCEEDED
    assert outcome.result == {"receipt_id": "receipt-1", "confirmed": True}
    assert runtime.calls == [("danmaku", "hello")]


def test_routes_require_read_scope_and_operator_role(tmp_path: Path) -> None:
    store = AuthStore(tmp_path / "api.sqlite3", installation_id="test")
    codec = SignedValueCodec(SECRET)
    context = APIContext(
        store=store,
        codec=codec,
        installation_id="test",
        allowed_origins=("http://localhost:5173",),
        command_store=__import__("src.kernel.commands", fromlist=["CommandStore"]).CommandStore(tmp_path / "api.sqlite3"),
        command_dispatcher=None,
    )
    from src.kernel.commands import CommandDispatcher, HandlerRegistry

    registry = HandlerRegistry()
    provider = StaticLivestreamProvider(FakeRuntime(), None)
    LivestreamCommandService(provider).register(registry)
    context = APIContext(
        store=store,
        codec=codec,
        installation_id="test",
        allowed_origins=("http://localhost:5173",),
        command_store=context.command_store,
        command_dispatcher=CommandDispatcher(context.command_store, registry=registry),
        livestream=provider,
    )
    app = create_api_app(context)
    client = TestClient(app)

    user_challenge = store.create_bootstrap_challenge(
        codec=codec,
        audience=USER_FRONTEND_AUDIENCE,
        origin="http://localhost:5173",
        scopes=("auth:session", "livestream:read", "livestream:operate"),
    )
    user = client.post(
        "/auth/sessions",
        headers={"Origin": "http://localhost:5173"},
        json={
            "grant_type": "bootstrap_challenge",
            "audience": USER_FRONTEND_AUDIENCE,
            "bootstrap_challenge": user_challenge,
            "origin": "http://localhost:5173",
        },
    ).json()["access_token"]
    assert client.get("/livestream/status", headers={"Authorization": f"Bearer {user}"}).status_code == 200
    forbidden = client.post(
        "/livestream/session:start",
        headers={"Authorization": f"Bearer {user}", "Idempotency-Key": "user-start-denied"},
        json={},
    )
    assert forbidden.status_code == 403
    assert forbidden.json()["error"]["code"] == "role_required"

    admin_challenge = store.create_bootstrap_challenge(
        codec=codec,
        audience=ADMIN_FRONTEND_AUDIENCE,
        origin="http://localhost:5173",
        scopes=("auth:session", "livestream:read", "livestream:operate"),
    )
    admin = client.post(
        "/auth/sessions",
        headers={"Origin": "http://localhost:5173"},
        json={
            "grant_type": "bootstrap_challenge",
            "audience": ADMIN_FRONTEND_AUDIENCE,
            "bootstrap_challenge": admin_challenge,
            "origin": "http://localhost:5173",
        },
    ).json()["access_token"]
    accepted = client.post(
        "/livestream/session:start",
        headers={"Authorization": f"Bearer {admin}", "Idempotency-Key": "admin-start-command"},
        json={},
    )
    assert accepted.status_code == 202

    context.command_store.close()
    store.close()
