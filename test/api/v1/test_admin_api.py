"""P3-11 管理总览、访问、审计、集成和作业契约。"""

from datetime import timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from src.app.api.v1.admin import AdminFacade
from src.app.api.v1.admin_store import AdminStore
from src.app.api.v1.auth_store import AuthStore
from src.app.api.v1.foundation import FoundationProjection, FoundationSnapshot
from src.app.api.v1.policy import (
    ADMIN_FRONTEND_AUDIENCE,
    PLATFORM_SERVICE_AUDIENCE,
    USER_FRONTEND_AUDIENCE,
)
from src.app.api.v1.runtime import APIContext, create_api_app
from src.app.api.v1.schemas import AdapterStatus, ComponentStatus
from src.app.api.v1.schemas.common import utc_now
from src.app.api.v1.tokens import SignedValueCodec
from src.kernel.commands import CommandStore

ORIGIN = "http://localhost:5173"
ADMIN_SCOPES = (
    "admin:overview",
    "admin:audit",
    "admin:logs",
    "admin:session",
    "admin:credential",
    "admin:settings",
    "metrics:read",
    "sync:read",
    "integration:read",
    "integration:test",
    "jobs:read",
)


def _snapshot() -> FoundationSnapshot:
    return FoundationSnapshot(
        generated_at=utc_now(),
        node_id="node-admin",
        modules=(
            ComponentStatus(
                component="api",
                state="ready",
                enabled=True,
                owner="app_api_v1",
            ),
            ComponentStatus(
                component="remote_sync",
                state="degraded",
                enabled=True,
                owner="life_engine.shared_sync",
                backlog=3,
                degraded_reason="RemoteUnavailable: 远程同步不可用。",
            ),
        ),
        adapters=(
            AdapterStatus(
                provider="feishu",
                component="feishu_adapter:adapter:feishu_adapter",
                state="degraded",
                enabled=True,
                connected=False,
                degraded_reason="Adapter 已加载但当前未连接。",
            ),
        ),
        migration_version="api-v1-schema-1",
    )


def _app(tmp_path: Path):
    auth = AuthStore(
        tmp_path / "auth.sqlite3", installation_id="installation-admin"
    )
    admin = AdminStore(tmp_path / "admin.sqlite3")
    commands = CommandStore(tmp_path / "commands.sqlite3")
    codec = SignedValueCodec("x" * 48)
    foundation = FoundationProjection(
        node_id="node-admin", snapshot_provider=_snapshot
    )
    facade = AdminFacade(
        foundation=foundation,
        auth=auth,
        admin=admin,
        commands=commands,
        integration_tests={
            "feishu": lambda _check: {
                "ok": True,
                "permission": "ready",
                "token": "must-not-leak",
                "path": "must-not-leak",
            }
        },
    )
    context = APIContext(
        store=auth,
        codec=codec,
        installation_id="installation-admin",
        access_ttl=timedelta(minutes=5),
        refresh_ttl=timedelta(hours=1),
        allowed_origins=(ORIGIN,),
        foundation=foundation,
        admin=facade,
    )
    return create_api_app(context), auth, admin, commands, codec


def _token(
    client: TestClient,
    store: AuthStore,
    codec: SignedValueCodec,
    *,
    audience: str,
    scopes: tuple[str, ...],
) -> str:
    challenge = store.create_bootstrap_challenge(
        codec=codec,
        audience=audience,
        origin=ORIGIN,
        actor_id="actor-admin-test",
        scopes=scopes,
    )
    response = client.post(
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


def test_admin_routes_require_administrator_role_even_with_scopes(tmp_path: Path) -> None:
    app, auth, admin, commands, codec = _app(tmp_path)
    client = TestClient(app)
    try:
        user = _token(
            client,
            auth,
            codec,
            audience=USER_FRONTEND_AUDIENCE,
            scopes=ADMIN_SCOPES,
        )
        denied = client.get(
            "/admin/overview",
            headers={"Authorization": f"Bearer {user}"},
        )
        assert denied.status_code == 403
        assert denied.json()["error"]["code"] == "role_required"

        service_secret = "platform-service-secret-long-enough"
        auth.add_credential(
            actor_id="platform-service",
            audience=PLATFORM_SERVICE_AUDIENCE,
            role="platform_service",
            secret=service_secret,
            scopes=ADMIN_SCOPES,
        )
        service = client.post(
            "/auth/sessions",
            json={
                "grant_type": "service_credential",
                "audience": PLATFORM_SERVICE_AUDIENCE,
                "service_credential": service_secret,
            },
        ).json()["access_token"]
        assert client.get(
            "/admin/overview",
            headers={"Authorization": f"Bearer {service}"},
        ).status_code == 403
    finally:
        client.close()
        commands.close()
        admin.close()
        auth.close()


def test_admin_overview_settings_and_credentials_are_safe_and_audited(
    tmp_path: Path,
) -> None:
    app, auth, admin, commands, codec = _app(tmp_path)
    client = TestClient(app)
    try:
        token = _token(
            client,
            auth,
            codec,
            audience=ADMIN_FRONTEND_AUDIENCE,
            scopes=ADMIN_SCOPES,
        )
        headers = {"Authorization": f"Bearer {token}"}

        overview = client.get("/admin/overview", headers=headers)
        assert overview.status_code == 200
        assert overview.json()["state"] == "degraded"
        assert overview.json()["active_incidents"] == 1

        current = client.get("/admin/settings", headers=headers)
        assert current.status_code == 200
        assert current.json()["revision"] == 0
        validation = client.post(
            "/admin/settings:validate",
            headers=headers,
            json={"values": {"unknown.path": 1}},
        )
        assert validation.json()["valid"] is False
        assert client.get("/admin/settings", headers=headers).json()["revision"] == 0

        updated = client.patch(
            "/admin/settings",
            headers=headers,
            json={
                "expected_revision": 0,
                "values": {"api.max_concurrency": 48},
            },
        )
        assert updated.status_code == 200
        assert updated.json()["revision"] == 1
        conflict = client.patch(
            "/admin/settings",
            headers=headers,
            json={
                "expected_revision": 0,
                "values": {"api.max_concurrency": 49},
            },
        )
        assert conflict.status_code == 409

        created = client.post(
            "/admin/credentials",
            headers=headers,
            json={
                "actor_id": "external-platform",
                "scopes": ["system:read", "events:read"],
                "resource_grants": ["events:*"],
            },
        )
        assert created.status_code == 201
        secret = created.json()["secret"]
        credential_id = created.json()["credential"]["credential_id"]
        listing = client.get("/admin/credentials", headers=headers)
        assert listing.status_code == 200
        assert secret not in listing.text
        assert "secret_hash" not in listing.text

        rotated = client.post(
            f"/admin/credentials/{credential_id}:rotate", headers=headers
        )
        assert rotated.status_code == 200
        assert rotated.json()["secret"] != secret

        audits = client.get("/admin/audit-events", headers=headers)
        assert audits.status_code == 200
        assert audits.json()["count"] >= 3
        assert secret not in audits.text
    finally:
        client.close()
        commands.close()
        admin.close()
        auth.close()


def test_integration_test_is_bounded_redacted_and_never_reconnects(
    tmp_path: Path,
) -> None:
    app, auth, admin, commands, codec = _app(tmp_path)
    client = TestClient(app)
    try:
        token = _token(
            client,
            auth,
            codec,
            audience=ADMIN_FRONTEND_AUDIENCE,
            scopes=ADMIN_SCOPES,
        )
        headers = {"Authorization": f"Bearer {token}"}
        response = client.post(
            "/admin/integrations/feishu:test",
            headers=headers,
            json={"check": "permissions"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["reconnect_performed"] is False
        assert body["result"] == {"ok": True, "permission": "ready"}
        assert "token" not in response.text
        assert "path" not in response.text

        missing = client.post(
            "/admin/integrations/unknown:test",
            headers=headers,
            json={"check": "health"},
        )
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "capability_disabled"
    finally:
        client.close()
        commands.close()
        admin.close()
        auth.close()
