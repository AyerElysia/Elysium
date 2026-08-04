"""P3-01 挂载、耐久恢复、cursor 与签名值契约。"""

import asyncio
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.app.api.v1.auth_store import AuthStore
from src.app.api.v1.chat_commands import ChatCommandService
from src.app.api.v1.mount import mount_api_v1
from src.app.api.v1.policy import (
    ADMIN_FRONTEND_AUDIENCE,
    PLATFORM_SERVICE_AUDIENCE,
    USER_FRONTEND_AUDIENCE,
)
from src.app.api.v1.tokens import SignedValueCodec, SignedValueError
from src.core.config.core_config import CoreConfig
from src.kernel.commands import (
    CommandOutcome,
    CommandStatus,
    CommandStore,
    HandlerRegistry,
)

ENVIRONMENT = {
    "ELYSIUM_APP_API_V1_SIGNING_SECRET": "s" * 48,
    "ELYSIUM_INSTALLATION_ID": "installation-test",
}


def test_app_api_is_disabled_by_default_and_limits_are_bounded() -> None:
    config = CoreConfig.HttpRouterSection()
    assert not config.enable_app_api_v1
    assert config.app_api_v1_database_path.startswith("runtime/")
    assert config.app_api_v1_max_concurrency == 32
    assert config.app_api_v1_max_websocket_connections == 64
    with pytest.raises(ValueError):
        CoreConfig.HttpRouterSection(app_api_v1_max_concurrency=0)
    with pytest.raises(ValueError):
        CoreConfig.HttpRouterSection(app_api_v1_max_websocket_connections=0)


def test_mount_requires_stable_secret_installation_and_exact_origin(
    tmp_path: Path,
) -> None:
    app = FastAPI()
    with pytest.raises(RuntimeError, match="SIGNING_SECRET"):
        mount_api_v1(
            app,
            workspace_root=tmp_path,
            database_path="runtime/api/auth.sqlite3",
            allowed_origins=("http://localhost:5173",),
            max_concurrency=4,
            environ={},
        )
    with pytest.raises(RuntimeError, match="invalid exact Origin"):
        mount_api_v1(
            app,
            workspace_root=tmp_path,
            database_path="runtime/api/auth.sqlite3",
            allowed_origins=("http://*",),
            max_concurrency=4,
            environ=ENVIRONMENT,
        )
    with pytest.raises(RuntimeError, match="runtime"):
        mount_api_v1(
            app,
            workspace_root=tmp_path,
            database_path="outside.sqlite3",
            allowed_origins=("http://localhost:5173",),
            max_concurrency=4,
            environ=ENVIRONMENT,
        )


def test_mount_creates_owned_store_and_api_route(tmp_path: Path) -> None:
    app = FastAPI()
    mounted = mount_api_v1(
        app,
        workspace_root=tmp_path,
        database_path="runtime/api/auth.sqlite3",
        allowed_origins=("http://localhost:5173",),
        max_concurrency=4,
        environ=ENVIRONMENT,
    )
    assert any(getattr(route, "path", None) == "/api/v1" for route in app.routes)
    assert (tmp_path / "runtime" / "api" / "auth.sqlite3").exists()
    mounted.close()
    mounted.close()
    assert not any(getattr(route, "path", None) == "/api/v1" for route in app.routes)

    remounted = mount_api_v1(
        app,
        workspace_root=tmp_path,
        database_path="runtime/api/auth.sqlite3",
        allowed_origins=("http://localhost:5173",),
        max_concurrency=4,
        environ=ENVIRONMENT,
    )
    remounted.close()


def test_mount_factory_registers_chat_routes_and_hides_authorization_snapshot(
    tmp_path: Path,
) -> None:
    app = FastAPI()

    class Factory:
        resolver = None

        def __call__(self, resolver):
            self.resolver = resolver
            return ChatCommandService(
                sender=object(),
                targets=resolver,
                media=None,
                providers=object(),
            )

    factory = Factory()
    mounted = mount_api_v1(
        app,
        workspace_root=tmp_path,
        database_path="runtime/api/auth.sqlite3",
        allowed_origins=("http://localhost:5173",),
        max_concurrency=4,
        chat_command_service_factory=factory,
        environ=ENVIRONMENT,
    )
    try:
        assert factory.resolver is not None
        mounted_app = next(
            route.app
            for route in app.routes
            if getattr(route, "path", None) == "/api/v1"
        )
        openapi = mounted_app.openapi()
        paths = openapi["paths"]
        expected_paths = {
            "/chat/messages:send",
            "/chat/messages/{message_id}:reply",
            "/chat/messages/{message_id}:edit",
            "/chat/messages/{message_id}:recall",
            "/chat/messages/{message_id}/reactions",
            "/chat/messages/{message_id}/reactions/{reaction}",
            "/chat/messages/{message_id}:mark-read",
            "/chat/messages:forward",
            "/chat/streams/{stream_id}/poke",
            "/admin/chat/streams/{stream_id}/announcements",
            "/admin/chat/streams/{stream_id}/announcements/{announcement_id}",
            "/admin/chat/messages/{message_id}:pin",
            "/admin/chat/messages/{message_id}:unpin",
        }
        assert expected_paths <= set(paths)
        operation_ids = {
            operation["operationId"]
            for path in expected_paths
            for operation in paths[path].values()
            if isinstance(operation, dict) and "operationId" in operation
        }
        assert len(operation_ids) == 13

        bootstrap = mounted.store.create_bootstrap_challenge(
            codec=SignedValueCodec(ENVIRONMENT["ELYSIUM_APP_API_V1_SIGNING_SECRET"]),
            audience=USER_FRONTEND_AUDIENCE,
            origin="http://localhost:5173",
            scopes=("auth:session", "chat:admin", "chat:moderate"),
        )
        client = TestClient(mounted_app)
        session = client.post(
            "/auth/sessions",
            headers={"Origin": "http://localhost:5173"},
            json={
                "grant_type": "bootstrap_challenge",
                "audience": USER_FRONTEND_AUDIENCE,
                "bootstrap_challenge": bootstrap,
                "origin": "http://localhost:5173",
            },
        )
        headers = {
            "Authorization": f"Bearer {session.json()['access_token']}",
            "Idempotency-Key": "ordinary-user-admin-command",
        }
        forbidden = client.post(
            "/admin/chat/streams/stream-1/announcements",
            headers=headers,
            json={"content": "notice"},
        )
        assert forbidden.status_code == 403
        assert forbidden.json()["error"]["code"] == "role_required"

        admin_challenge = mounted.store.create_bootstrap_challenge(
            codec=SignedValueCodec(ENVIRONMENT["ELYSIUM_APP_API_V1_SIGNING_SECRET"]),
            audience=ADMIN_FRONTEND_AUDIENCE,
            origin="http://localhost:5173",
            scopes=("auth:session", "chat:admin", "chat:moderate"),
        )
        admin_session = client.post(
            "/auth/sessions",
            headers={"Origin": "http://localhost:5173"},
            json={
                "grant_type": "bootstrap_challenge",
                "audience": ADMIN_FRONTEND_AUDIENCE,
                "bootstrap_challenge": admin_challenge,
                "origin": "http://localhost:5173",
            },
        )
        admin = client.post(
            "/admin/chat/streams/stream-1/announcements",
            headers={
                "Authorization": f"Bearer {admin_session.json()['access_token']}",
                "Idempotency-Key": "administrator-admin-command",
            },
            json={"content": "notice"},
        )
        assert admin.status_code == 202

        service_secret = "platform-service-secret-long-enough"
        mounted.store.add_credential(
            actor_id="platform-service",
            audience=PLATFORM_SERVICE_AUDIENCE,
            role="platform_service",
            secret=service_secret,
            scopes=("chat:admin", "chat:moderate"),
            resource_grants=("chat:*",),
        )
        service_session = client.post(
            "/auth/sessions",
            json={
                "grant_type": "service_credential",
                "audience": PLATFORM_SERVICE_AUDIENCE,
                "service_credential": service_secret,
            },
        )
        service = client.post(
            "/admin/chat/streams/stream-1/announcements",
            headers={
                "Authorization": f"Bearer {service_session.json()['access_token']}",
                "Idempotency-Key": "platform-service-admin-command",
            },
            json={"content": "notice"},
        )
        assert service.status_code == 202

        writer_challenge = mounted.store.create_bootstrap_challenge(
            codec=SignedValueCodec(ENVIRONMENT["ELYSIUM_APP_API_V1_SIGNING_SECRET"]),
            audience=USER_FRONTEND_AUDIENCE,
            origin="http://localhost:5173",
            scopes=("auth:session", "chat:write"),
        )
        writer_session = client.post(
            "/auth/sessions",
            headers={"Origin": "http://localhost:5173"},
            json={
                "grant_type": "bootstrap_challenge",
                "audience": USER_FRONTEND_AUDIENCE,
                "bootstrap_challenge": writer_challenge,
                "origin": "http://localhost:5173",
            },
        ).json()
        first = client.post(
            "/chat/messages:send",
            headers={
                "Authorization": f"Bearer {writer_session['access_token']}",
                "Idempotency-Key": "same-actor-refreshed-session",
            },
            json={
                "stream_id": "stream-1",
                "parts": [{"type": "text", "text": "hello"}],
            },
        )
        refreshed = client.post(
            "/auth/sessions/current:refresh",
            json={"refresh_token": writer_session["refresh_token"]},
        ).json()
        replay = client.post(
            "/chat/messages:send",
            headers={
                "Authorization": f"Bearer {refreshed['access_token']}",
                "Idempotency-Key": "same-actor-refreshed-session",
            },
            json={
                "stream_id": "stream-1",
                "parts": [{"type": "text", "text": "hello"}],
            },
        )
        assert first.status_code == 202
        assert replay.status_code == 200
        assert replay.json()["command"]["command_id"] == first.json()["command"][
            "command_id"
        ]
        assert "_authorization" not in str(first.json())
        stored = mounted.command_store.get(first.json()["command"]["command_id"])
        assert stored.payload["_authorization"]["session_id"].startswith("sess_")
    finally:
        mounted.close()


def test_mount_closes_owned_stores_when_chat_factory_fails(tmp_path: Path) -> None:
    app = FastAPI()

    def fail_factory(_resolver):
        raise RuntimeError("factory failed")

    with pytest.raises(RuntimeError, match="factory failed"):
        mount_api_v1(
            app,
            workspace_root=tmp_path,
            database_path="runtime/api/auth.sqlite3",
            allowed_origins=("http://localhost:5173",),
            max_concurrency=4,
            chat_command_service_factory=fail_factory,
            environ=ENVIRONMENT,
        )

    store = AuthStore(
        tmp_path / "runtime" / "api" / "auth.sqlite3",
        installation_id="installation-test",
    )
    store.close()


def _accept_command(mounted, *, key: str, command_type: str = "test.mount.run"):
    request_hash = mounted.command_store.request_hash(
        command_type=command_type,
        schema_version=1,
        target={},
        payload={"value": 1},
        correlation_id=None,
        expected_revision=None,
    )
    return mounted.command_store.accept(
        idempotency_key=key,
        request_hash=request_hash,
        command_type=command_type,
        schema_version=1,
        actor_id="actor-mount",
        caller_role="user",
        scopes=("jobs:operate", "jobs:read"),
        target={},
        payload={"value": 1},
    )[0]


@pytest.mark.asyncio
async def test_mount_start_recovers_accepted_command(tmp_path: Path) -> None:
    app = FastAPI()
    registry = HandlerRegistry()
    completed = asyncio.Event()

    async def handler(command):
        completed.set()
        return CommandOutcome(
            status=CommandStatus.SUCCEEDED,
            result={"value": command.payload["value"]},
        )

    registry.register(
        "test.mount.run",
        handler,
        required_scopes=frozenset({"jobs:operate"}),
    )
    mounted = mount_api_v1(
        app,
        workspace_root=tmp_path,
        database_path="runtime/api/auth.sqlite3",
        allowed_origins=("http://localhost:5173",),
        max_concurrency=4,
        command_registry=registry,
        environ=ENVIRONMENT,
    )
    command = _accept_command(mounted, key="mount-recovery-key")
    try:
        await mounted.start()
        await asyncio.wait_for(completed.wait(), timeout=1)
        for _ in range(100):
            finished = mounted.command_store.get(command.command_id)
            if finished.status is CommandStatus.SUCCEEDED:
                break
            await asyncio.sleep(0)
        assert finished.status is CommandStatus.SUCCEEDED
    finally:
        await mounted.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("cancellable", "expected_status"),
    [
        (False, CommandStatus.DELIVERY_UNKNOWN),
        (True, CommandStatus.CANCELLED),
    ],
)
async def test_mount_async_close_persists_interrupted_execution(
    tmp_path: Path,
    cancellable: bool,
    expected_status: CommandStatus,
) -> None:
    app = FastAPI()
    registry = HandlerRegistry()
    started = asyncio.Event()

    async def handler(_command):
        started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    registry.register(
        "test.mount.run",
        handler,
        required_scopes=frozenset({"jobs:operate"}),
        cancellable=cancellable,
    )
    mounted = mount_api_v1(
        app,
        workspace_root=tmp_path,
        database_path="runtime/api/auth.sqlite3",
        allowed_origins=("http://localhost:5173",),
        max_concurrency=4,
        command_registry=registry,
        environ=ENVIRONMENT,
    )
    command = _accept_command(
        mounted,
        key=f"mount-close-{cancellable}",
    )
    mounted.command_dispatcher.schedule(command.command_id)
    await asyncio.wait_for(started.wait(), timeout=1)
    assert (
        mounted.command_store.get(command.command_id).status is CommandStatus.EXECUTING
    )

    await mounted.aclose()

    command_store = CommandStore(tmp_path / "runtime" / "api" / "auth.sqlite3")
    try:
        assert command_store.get(command.command_id).status is expected_status
    finally:
        command_store.close()


def test_session_revocation_survives_store_reopen(tmp_path: Path) -> None:
    database = tmp_path / "auth.sqlite3"
    codec = SignedValueCodec("x" * 48)
    first = AuthStore(database, installation_id="installation-test")
    challenge = first.create_bootstrap_challenge(
        codec=codec,
        audience=USER_FRONTEND_AUDIENCE,
        origin="http://localhost:5173",
        scopes=("auth:session",),
    )
    session, access, _ = first.issue_session_from_bootstrap(
        challenge=challenge,
        audience=USER_FRONTEND_AUDIENCE,
        origin="http://localhost:5173",
        codec=codec,
        access_ttl=timedelta(minutes=5),
        refresh_ttl=timedelta(hours=1),
    )
    first.revoke_session(session.session_id)
    first.close()

    reopened = AuthStore(database, installation_id="installation-test")
    try:
        with pytest.raises(ValueError, match="session_revoked"):
            reopened.authenticate_access(access_token=access, codec=codec)
    finally:
        reopened.close()


def test_cursor_is_opaque_ledger_bound_and_tamper_evident() -> None:
    codec = SignedValueCodec("c" * 48)
    cursor = codec.encode_cursor(42, ledger="life-events")
    assert codec.decode_cursor(cursor, ledger="life-events") == 42
    with pytest.raises(SignedValueError, match="cursor_invalid"):
        codec.decode_cursor(cursor, ledger="other-ledger")
    body, signature = cursor.rsplit(".", 1)
    replacement = "A" if signature[0] != "A" else "B"
    altered = f"{body}.{replacement}{signature[1:]}"
    with pytest.raises(SignedValueError, match="signature_invalid"):
        codec.decode_cursor(altered, ledger="life-events")
