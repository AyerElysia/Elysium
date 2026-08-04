"""P3-01 公共 schema、认证和错误契约测试。"""

import asyncio
import hashlib
import json
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

from src.app.api.v1.auth_store import AuthStore
from src.app.api.v1.policy import (
    ADMIN_FRONTEND_AUDIENCE,
    ALL_EXPORTED_SCOPES,
    PLATFORM_SERVICE_AUDIENCE,
    USER_FRONTEND_AUDIENCE,
)
from src.app.api.v1.runtime import (
    MAX_BODY_BYTES,
    APIContext,
    APIError,
    WebSocketConnectionBudget,
    create_api_app,
)
from src.app.api.v1.tokens import SignedValueCodec


@pytest.fixture
def auth_context() -> tuple[APIContext, AuthStore, SignedValueCodec]:
    store = AuthStore(installation_id="installation-test")
    codec = SignedValueCodec("x" * 48)
    context = APIContext(
        store=store,
        codec=codec,
        installation_id="installation-test",
        access_ttl=timedelta(minutes=5),
        refresh_ttl=timedelta(hours=1),
        ticket_ttl=timedelta(minutes=1),
        allowed_origins=("http://localhost:5173",),
    )
    yield context, store, codec
    store.close()


def _client(context: APIContext) -> TestClient:
    return TestClient(create_api_app(context))


def _bootstrap(context: APIContext, *, audience: str = USER_FRONTEND_AUDIENCE) -> str:
    return context.store.create_bootstrap_challenge(
        codec=context.codec,
        audience=audience,
        origin="http://localhost:5173",
        actor_id="actor-test",
        scopes=("auth:session", "auth:ticket", "events:read"),
    )


def _session(client: TestClient, context: APIContext) -> dict:
    response = client.post(
        "/auth/sessions",
        headers={"Origin": "http://localhost:5173"},
        json={
            "grant_type": "bootstrap_challenge",
            "audience": USER_FRONTEND_AUDIENCE,
            "bootstrap_challenge": _bootstrap(context),
            "origin": "http://localhost:5173",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_bootstrap_is_origin_and_installation_bound_and_one_time(
    auth_context: tuple[APIContext, AuthStore, SignedValueCodec],
) -> None:
    context, _, _ = auth_context
    client = _client(context)
    challenge = _bootstrap(context)
    payload = {
        "grant_type": "bootstrap_challenge",
        "audience": USER_FRONTEND_AUDIENCE,
        "bootstrap_challenge": challenge,
        "origin": "http://evil.invalid",
    }
    wrong_origin = client.post(
        "/auth/sessions",
        headers={"Origin": "http://evil.invalid"},
        json=payload,
    )
    assert wrong_origin.status_code == 401
    assert wrong_origin.json()["error"]["code"] == "unauthenticated"

    valid = dict(payload, origin="http://localhost:5173")
    headers = {"Origin": "http://localhost:5173"}
    first = client.post("/auth/sessions", headers=headers, json=valid)
    second = client.post("/auth/sessions", headers=headers, json=valid)
    assert first.status_code == 200
    assert second.status_code == 401
    assert second.json()["error"]["code"] == "unauthenticated"


def test_session_refresh_rotates_and_logout_revokes(
    auth_context: tuple[APIContext, AuthStore, SignedValueCodec],
) -> None:
    context, _, _ = auth_context
    client = _client(context)
    session = _session(client, context)
    headers = {"Authorization": f"Bearer {session['access_token']}"}

    refreshed = client.post(
        "/auth/sessions/current:refresh",
        json={"refresh_token": session["refresh_token"]},
    )
    assert refreshed.status_code == 200
    assert refreshed.json()["access_token"] != session["access_token"]
    old_access = client.get("/auth/me", headers=headers)
    assert old_access.status_code == 401

    new_headers = {"Authorization": f"Bearer {refreshed.json()['access_token']}"}
    logout = client.delete("/auth/sessions/current", headers=new_headers)
    assert logout.status_code == 200
    assert logout.json() == {"revoked": True}
    assert client.delete("/auth/sessions/current", headers=new_headers).json() == {
        "revoked": True
    }
    assert client.get("/auth/me", headers=new_headers).status_code == 401
    assert client.post(
        "/auth/sessions/current:refresh",
        json={"refresh_token": refreshed.json()["refresh_token"]},
    ).status_code == 401


def test_service_audience_requires_service_credential_and_revocation(
    auth_context: tuple[APIContext, AuthStore, SignedValueCodec],
) -> None:
    context, store, _ = auth_context
    client = _client(context)
    secret = "service-secret-value"
    credential_id = store.add_credential(
        actor_id="service-test",
        audience=PLATFORM_SERVICE_AUDIENCE,
        role="platform_service",
        secret=secret,
        scopes=ALL_EXPORTED_SCOPES,
    )
    session = client.post(
        "/auth/sessions",
        json={
            "grant_type": "service_credential",
            "audience": PLATFORM_SERVICE_AUDIENCE,
            "service_credential": secret,
        },
    )
    assert session.status_code == 200
    assert session.json()["identity"]["role"] == "platform_service"
    assert session.json()["identity"]["credential_id"] == credential_id
    assert store.revoke_credential(credential_id)
    assert client.get(
        "/auth/me",
        headers={"Authorization": f"Bearer {session.json()['access_token']}"},
    ).status_code == 401


def test_admin_bootstrap_is_not_user_session(
    auth_context: tuple[APIContext, AuthStore, SignedValueCodec],
) -> None:
    context, _, _ = auth_context
    client = _client(context)
    challenge = context.store.create_bootstrap_challenge(
        codec=context.codec,
        audience=ADMIN_FRONTEND_AUDIENCE,
        origin="http://localhost:5173",
        scopes=("auth:session",),
    )
    response = client.post(
        "/auth/sessions",
        headers={"Origin": "http://localhost:5173"},
        json={
            "grant_type": "bootstrap_challenge",
            "audience": ADMIN_FRONTEND_AUDIENCE,
            "bootstrap_challenge": challenge,
            "origin": "http://localhost:5173",
        },
    )
    assert response.status_code == 200
    assert response.json()["identity"]["role"] == "administrator"
    assert response.json()["identity"]["audience"] == ADMIN_FRONTEND_AUDIENCE


def test_ws_ticket_checks_resource_scope_and_replay(
    auth_context: tuple[APIContext, AuthStore, SignedValueCodec],
) -> None:
    context, _, _ = auth_context
    client = _client(context)
    session = _session(client, context)
    headers = {"Authorization": f"Bearer {session['access_token']}"}
    denied = client.post(
        "/auth/ws-tickets",
        headers=headers,
        json={
            "resource": "/api/v1/livestream/stage/ws",
            "subprotocol": "elysium.events.v1",
            "scopes": ["livestream:read"],
            "origin": "http://localhost:5173",
        },
    )
    assert denied.status_code == 403

    issued = client.post(
        "/auth/ws-tickets",
        headers=headers,
        json={
            "resource": "/api/v1/events/ws",
            "subprotocol": "elysium.events.v1",
            "scopes": ["events:read"],
            "origin": "http://localhost:5173",
        },
    )
    assert issued.status_code == 200
    ticket = issued.json()["ticket"]
    consumed = context.store.consume_ws_ticket(
        token=ticket,
        codec=context.codec,
        resource="/api/v1/events/ws",
        subprotocol="elysium.events.v1",
        origin="http://localhost:5173",
    )
    assert consumed.session_id == session["identity"]["session_id"]
    with pytest.raises(ValueError, match="ticket_replayed"):
        context.store.consume_ws_ticket(
            token=ticket,
            codec=context.codec,
            resource="/api/v1/events/ws",
            subprotocol="elysium.events.v1",
            origin="http://localhost:5173",
        )

    live_session = context.store.authenticate_access(
        access_token=session["access_token"],
        codec=context.codec,
    )
    context.store.revoke_session(session["identity"]["session_id"])
    with pytest.raises(ValueError, match="session_revoked"):
        context.store.issue_ws_ticket(
            session=live_session,
            codec=context.codec,
            resource="/api/v1/events/ws",
            subprotocol="elysium.events.v1",
            scopes=("events:read",),
            origin="http://localhost:5173",
            ttl=timedelta(minutes=1),
        )
    assert client.post(
        "/auth/ws-tickets", headers=headers, json={
            "resource": "/api/v1/events/ws",
            "subprotocol": "elysium.events.v1",
            "scopes": ["events:read"],
            "origin": "http://localhost:5173",
        }
    ).status_code == 401


def test_ws_ticket_revocation_race_is_unauthenticated(
    auth_context: tuple[APIContext, AuthStore, SignedValueCodec],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context, _, _ = auth_context
    client = _client(context)
    session = _session(client, context)

    def revoked_during_issue(*_args: object, **_kwargs: object) -> None:
        raise ValueError("session_revoked")

    monkeypatch.setattr(context.store, "issue_ws_ticket", revoked_during_issue)
    response = client.post(
        "/auth/ws-tickets",
        headers={"Authorization": f"Bearer {session['access_token']}"},
        json={
            "resource": "/api/v1/events/ws",
            "subprotocol": "elysium.events.v1",
            "scopes": ["events:read"],
            "origin": "http://localhost:5173",
        },
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthenticated"


def test_errors_are_stable_request_id_and_payload_limit(
    auth_context: tuple[APIContext, AuthStore, SignedValueCodec],
) -> None:
    context, _, _ = auth_context
    app = create_api_app(context)

    @app.get("/test/internal-error", include_in_schema=False)
    async def internal_error() -> None:
        raise RuntimeError("Authorization=secret-token")

    client = TestClient(app, raise_server_exceptions=False)
    missing = client.get("/auth/me")
    assert missing.status_code == 401
    assert missing.headers["X-Request-ID"] == missing.json()["error"]["request_id"]
    assert "Authorization" not in missing.text
    assert "secret" not in missing.text

    oversized = client.post(
        "/auth/sessions",
        headers={"content-length": str(MAX_BODY_BYTES + 1)},
        content=b"{}",
    )
    assert oversized.status_code == 413
    assert oversized.json()["error"]["code"] == "payload_too_large"

    unsupported = client.post(
        "/auth/sessions/current:refresh",
        headers={"Content-Type": "text/plain"},
        content=b"not-json",
    )
    assert unsupported.status_code == 415
    assert unsupported.json()["error"]["code"] == "unsupported_media_type"

    missing_route = client.get("/does-not-exist")
    assert missing_route.status_code == 404
    assert missing_route.json()["error"]["code"] == "resource_not_found"
    wrong_method = client.put("/auth/me", json={})
    assert wrong_method.status_code == 405
    assert wrong_method.json()["error"]["code"] == "method_not_allowed"

    internal = client.get("/test/internal-error")
    assert internal.status_code == 500
    assert internal.json()["error"]["code"] == "internal_error"
    assert "secret-token" not in internal.text
    assert "RuntimeError" not in internal.text


def test_unknown_ws_capability_returns_stable_error(
    auth_context: tuple[APIContext, AuthStore, SignedValueCodec],
) -> None:
    context, _, _ = auth_context
    client = _client(context)
    session = _session(client, context)
    response = client.post(
        "/auth/ws-tickets",
        headers={"Authorization": f"Bearer {session['access_token']}"},
        json={
            "resource": "/api/v1/internal/arbitrary/ws",
            "subprotocol": "elysium.internal.v1",
            "scopes": ["events:read"],
        },
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "capability_disabled"


def test_http_concurrency_limit_rejects_without_queueing(
    auth_context: tuple[APIContext, AuthStore, SignedValueCodec],
) -> None:
    context, _, _ = auth_context
    limited = APIContext(
        store=context.store,
        codec=context.codec,
        installation_id=context.installation_id,
        allowed_origins=context.allowed_origins,
        max_concurrency=1,
    )
    app = create_api_app(limited)
    entered = asyncio.Event()
    release = asyncio.Event()

    @app.get("/test/blocked", include_in_schema=False)
    async def blocked() -> dict[str, bool]:
        entered.set()
        await release.wait()
        return {"completed": True}

    async def exercise() -> None:
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            first = asyncio.create_task(client.get("/test/blocked"))
            await entered.wait()
            overloaded = await client.get("/auth/me")
            assert overloaded.status_code == 429
            assert overloaded.json()["error"]["code"] == "request_overloaded"
            release.set()
            assert (await first).status_code == 200

    asyncio.run(exercise())


def test_websocket_connection_budget_is_strict_and_owned() -> None:
    async def exercise() -> None:
        budget = WebSocketConnectionBudget(1)
        assert budget.available
        await budget.acquire()
        assert not budget.available
        with pytest.raises(APIError, match="connection_limit_reached"):
            await budget.acquire()
        budget.release()
        assert budget.available
        with pytest.raises(RuntimeError, match="without ownership"):
            budget.release()

    asyncio.run(exercise())


def test_openapi_operation_ids_are_stable(
    auth_context: tuple[APIContext, AuthStore, SignedValueCodec],
) -> None:
    context, _, _ = auth_context
    schema = _client(context).get("/openapi.json").json()
    operations = {
        operation["operationId"]
        for path in schema["paths"].values()
        for operation in path.values()
        if isinstance(operation, dict) and "operationId" in operation
    }
    assert operations == {
        "createAuthSession",
        "getAuthMe",
        "refreshAuthSession",
        "logoutAuthSession",
        "createAuthWebsocketTicket",
        "getBootstrap",
        "getCapabilities",
        "getReadiness",
        "getHealth",
    }
    assert "HTTPBearer" in schema["components"]["securitySchemes"]
    for path in schema["paths"].values():
        for operation in path.values():
            if not isinstance(operation, dict) or "operationId" not in operation:
                continue
            assert operation["responses"]["401"]["content"]["application/json"][
                "schema"
            ] == {"$ref": "#/components/schemas/ErrorResponse"}
            assert operation["responses"]["500"]["content"]["application/json"][
                "schema"
            ] == {"$ref": "#/components/schemas/ErrorResponse"}
    normalized = json.dumps(schema, sort_keys=True, separators=(",", ":")).encode()
    assert hashlib.sha256(normalized).hexdigest() == (
        "ceef6a7b0073f2b8795586e6b79226e2d28de64d8a0e8b21dd36184d20719cc4"
    )
