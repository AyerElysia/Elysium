"""P3-04 command API authorization and reliability contracts."""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from src.kernel.commands import (
    CommandDispatcher,
    CommandOutcome,
    CommandStatus,
    CommandStore,
    HandlerRegistry,
)

from src.app.api.v1.auth_store import AuthStore
from src.app.api.v1.policy import ADMIN_FRONTEND_AUDIENCE, USER_FRONTEND_AUDIENCE
from src.app.api.v1.runtime import APIContext, create_api_app
from src.app.api.v1.tokens import SignedValueCodec
from src.kernel.concurrency import TaskManager

ORIGIN = "http://localhost:5173"


@pytest.fixture
async def command_context(tmp_path):
    auth_store = AuthStore(installation_id="installation-command")
    command_store = CommandStore(tmp_path / "commands.sqlite3")
    registry = HandlerRegistry()
    dispatcher = CommandDispatcher(
        command_store,
        registry=registry,
        task_manager=TaskManager(),
    )
    context = APIContext(
        store=auth_store,
        codec=SignedValueCodec("x" * 48),
        installation_id="installation-command",
        allowed_origins=(ORIGIN,),
        command_store=command_store,
        command_dispatcher=dispatcher,
    )
    yield context, registry, command_store, dispatcher
    await dispatcher.close()
    command_store.close()
    auth_store.close()


def _token(
    context: APIContext,
    *,
    actor_id: str,
    scopes: tuple[str, ...],
    audience: str = USER_FRONTEND_AUDIENCE,
) -> str:
    challenge = context.store.create_bootstrap_challenge(
        codec=context.codec,
        audience=audience,
        origin=ORIGIN,
        actor_id=actor_id,
        scopes=("auth:session", *scopes),
    )
    response = TestClient(create_api_app(context)).post(
        "/auth/sessions",
        headers={"Origin": ORIGIN},
        json={
            "grant_type": "bootstrap_challenge",
            "audience": audience,
            "bootstrap_challenge": challenge,
            "origin": ORIGIN,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def test_command_openapi_contract_is_explicit(command_context) -> None:
    context, _registry, _store, _dispatcher = command_context
    schema = create_api_app(context).openapi()
    assert schema["paths"]["/commands"]["post"]["operationId"] == "createCommand"
    assert schema["paths"]["/commands"]["get"]["operationId"] == "listCommands"
    assert schema["paths"]["/commands/{command_id}"]["get"]["operationId"] == "getCommand"
    cancel = schema["paths"]["/commands/{command_id}:cancel"]["post"]
    assert cancel["operationId"] == "cancelCommand"
    create = schema["paths"]["/commands"]["post"]
    assert create["responses"]["409"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ErrorResponse"
    }
    assert create["parameters"][0]["name"] == "Idempotency-Key"


def _payload(value: int = 1) -> dict:
    return {
        "command_type": "test.command.run",
        "schema_version": 1,
        "target": {"resource_id": "resource-1"},
        "payload": {"value": value},
        "correlation_id": "correlation-1",
        "expected_revision": None,
    }


@pytest.mark.asyncio
async def test_submission_requires_scope_and_valid_idempotency_key(command_context) -> None:
    context, _registry, _store, _dispatcher = command_context
    read_token = _token(context, actor_id="actor-1", scopes=("jobs:read",))
    write_token = _token(context, actor_id="actor-1", scopes=("jobs:operate",))
    transport = ASGITransport(app=create_api_app(context))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        forbidden = await client.post(
            "/commands",
            headers={
                "Authorization": f"Bearer {read_token}",
                "Idempotency-Key": "command-key-001",
            },
            json=_payload(),
        )
        missing_key = await client.post(
            "/commands",
            headers={"Authorization": f"Bearer {write_token}"},
            json=_payload(),
        )
    assert forbidden.status_code == 403
    assert forbidden.json()["error"]["code"] == "scope_required"
    assert missing_key.status_code == 422
    assert missing_key.json()["error"]["code"] == "idempotency_key_required"


@pytest.mark.asyncio
async def test_idempotent_replay_and_conflict_are_stable(command_context) -> None:
    context, _registry, store, dispatcher = command_context
    token = _token(
        context,
        actor_id="actor-1",
        scopes=("jobs:read", "jobs:operate"),
    )
    transport = ASGITransport(app=create_api_app(context))
    headers = {
        "Authorization": f"Bearer {token}",
        "Idempotency-Key": "command-key-001",
    }
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.post("/commands", headers=headers, json=_payload())
        replay = await client.post("/commands", headers=headers, json=_payload())
        conflict = await client.post("/commands", headers=headers, json=_payload(2))
        for _ in range(100):
            result = await client.get(
                f"/commands/{first.json()['command_id']}",
                headers={"Authorization": f"Bearer {token}"},
            )
            if result.json()["status"] == "rejected":
                break
            await asyncio.sleep(0)
    assert first.status_code == 202
    assert replay.status_code == 200
    assert replay.json()["command_id"] == first.json()["command_id"]
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "idempotency_conflict"
    assert result.json()["error_code"] == "command_type_unavailable"
    assert len(store.list(actor_id="actor-1")) == 1
    assert len(dispatcher._tasks) == 0


@pytest.mark.asyncio
async def test_success_can_be_queried_after_submission_response_is_lost(command_context) -> None:
    context, registry, _store, _dispatcher = command_context
    completed = asyncio.Event()

    async def handler(command):
        completed.set()
        return CommandOutcome(
            status=CommandStatus.SUCCEEDED,
            result={"receipt_id": f"receipt-{command.payload['value']}"},
        )

    registry.register(
        "test.command.run",
        handler,
        required_scopes=frozenset({"jobs:operate"}),
    )
    token = _token(
        context,
        actor_id="actor-1",
        scopes=("jobs:read", "jobs:operate"),
    )
    transport = ASGITransport(app=create_api_app(context))
    headers = {
        "Authorization": f"Bearer {token}",
        "Idempotency-Key": "lost-response-key-01",
    }
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        submitted = await client.post("/commands", headers=headers, json=_payload())
        command_id = submitted.json()["command_id"]
        await asyncio.wait_for(completed.wait(), timeout=1)
        for _ in range(100):
            queried = await client.get(
                f"/commands/{command_id}",
                headers={"Authorization": f"Bearer {token}"},
            )
            if queried.json()["status"] == "succeeded":
                break
            await asyncio.sleep(0)
    assert queried.json()["result"] == {"receipt_id": "receipt-1"}
    assert queried.json()["result_event_id"].startswith(f"command.{command_id}.succeeded")


@pytest.mark.asyncio
async def test_owner_is_hidden_but_administrator_can_query_all(command_context) -> None:
    context, _registry, _store, _dispatcher = command_context
    actor_one = _token(
        context,
        actor_id="actor-1",
        scopes=("jobs:read", "jobs:operate"),
    )
    actor_two = _token(context, actor_id="actor-2", scopes=("jobs:read",))
    administrator = _token(
        context,
        actor_id="admin-1",
        scopes=("jobs:read",),
        audience=ADMIN_FRONTEND_AUDIENCE,
    )
    transport = ASGITransport(app=create_api_app(context))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        submitted = await client.post(
            "/commands",
            headers={
                "Authorization": f"Bearer {actor_one}",
                "Idempotency-Key": "owner-hidden-key-1",
            },
            json=_payload(),
        )
        command_id = submitted.json()["command_id"]
        hidden = await client.get(
            f"/commands/{command_id}",
            headers={"Authorization": f"Bearer {actor_two}"},
        )
        forbidden_filter = await client.get(
            "/commands?actor_id=actor-1",
            headers={"Authorization": f"Bearer {actor_two}"},
        )
        admin_view = await client.get(
            "/commands?actor_id=actor-1",
            headers={"Authorization": f"Bearer {administrator}"},
        )
    assert hidden.status_code == 404
    assert forbidden_filter.status_code == 404
    assert admin_view.status_code == 200
    assert admin_view.json()["commands"][0]["command_id"] == command_id


@pytest.mark.asyncio
async def test_cancel_is_limited_to_owned_cancellable_commands(command_context) -> None:
    context, registry, _store, _dispatcher = command_context

    async def cancellable_handler(_command):
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    registry.register(
        "test.command.run",
        cancellable_handler,
        required_scopes=frozenset({"jobs:operate"}),
        cancellable=True,
    )
    token = _token(
        context,
        actor_id="actor-1",
        scopes=("jobs:read", "jobs:operate"),
    )
    transport = ASGITransport(app=create_api_app(context))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        submitted = await client.post(
            "/commands",
            headers={
                "Authorization": f"Bearer {token}",
                "Idempotency-Key": "cancel-key-0001",
            },
            json=_payload(),
        )
        command_id = submitted.json()["command_id"]
        for _ in range(100):
            current = await client.get(
                f"/commands/{command_id}",
                headers={"Authorization": f"Bearer {token}"},
            )
            if current.json()["status"] == "executing":
                break
            await asyncio.sleep(0)
        cancelled = await client.post(
            f"/commands/{command_id}:cancel",
            headers={"Authorization": f"Bearer {token}"},
            json={},
        )
        for _ in range(100):
            terminal = await client.get(
                f"/commands/{command_id}",
                headers={"Authorization": f"Bearer {token}"},
            )
            if terminal.json()["status"] == "cancelled":
                break
            await asyncio.sleep(0)
    assert cancelled.status_code == 200
    assert cancelled.json()["cancellation_requested"] is True
    assert terminal.json()["status"] == "cancelled"
